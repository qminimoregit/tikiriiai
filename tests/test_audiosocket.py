import asyncio
import struct
import uuid

import numpy as np

from app.telephony import asterisk
from app.telephony.asterisk import HEADER, AsteriskTransport, pick_caller, pop_call, read_packet, register_caller


def packet(t: int, payload: bytes = b"") -> bytes:
    return HEADER.pack(t, len(payload)) + payload


class FakeWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, b):
        self.data.extend(b)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    def get_extra_info(self, name):
        return ("127.0.0.1", 5555)


def reader_with(data: bytes, eof: bool = True) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    if eof:
        r.feed_eof()
    return r


def packets(data: bytes) -> list[tuple[int, int]]:
    out, i = [], 0
    while i < len(data):
        typ, ln = struct.unpack(">BH", data[i:i + 3])
        out.append((typ, ln))
        i += 3 + ln
    return out


async def test_read_packet_and_eof():
    r = reader_with(packet(0x01, b"x" * 16) + packet(0x10, b"\x00" * 320))
    assert await read_packet(r) == (0x01, b"x" * 16)
    t, p = await read_packet(r)
    assert t == 0x10 and len(p) == 320
    assert await read_packet(r) is None


async def test_keys_go_to_the_dtmf_queue():
    frame = b"\x00\x00" * 160
    r = reader_with(packet(0x10, frame) + packet(0x03, b"5") + packet(0x03, b"#") + packet(0x03, b"x") + packet(0x00))
    t = AsteriskTransport(r, FakeWriter(), 0x10)
    assert await t.receive() is not None
    assert await t.receive() is None  # the keys are read on the way to the hang-up
    assert [t.dtmf.get_nowait(), t.dtmf.get_nowait()] == ["5", "#"] and t.dtmf.empty()  # "x" isn't a key
    t._sender.cancel()


async def test_transport_rates_and_hangup():
    tone = (np.sin(np.arange(320) / 5) * 8000).astype("<i2").tobytes()  # 20 ms at 16 kHz
    r = reader_with(packet(0x12, tone) + packet(0x00))
    w = FakeWriter()
    t = AsteriskTransport(r, w, 0x12, first_audio=tone)
    assert t.rate == 16000 and t.frame_bytes == 640
    first = await t.receive()
    assert len(first) == len(tone)  # 16 kHz in = 16 kHz to the agent
    assert await t.receive() is not None
    assert await t.receive() is None  # hangup
    await t.send(b"\x00\x00" * 2400)  # 100 ms at 24 kHz -> 5 frames at 16 kHz
    await t.close()
    # Asterisk hung up first, so we must not send our own hangup packet.
    assert set(packets(bytes(w.data))) == {(0x12, 640)}


async def test_agent_hangup_sends_hangup_packet():
    r = asyncio.StreamReader()  # caller still on the line
    w = FakeWriter()
    t = AsteriskTransport(r, w, 0x10)
    await t.send(b"\x00\x00" * 480)  # 20 ms at 24 kHz -> one 8 kHz frame (320 bytes)
    await t.close()
    assert bytes(w.data).startswith(HEADER.pack(0x10, 320))
    assert bytes(w.data).endswith(HEADER.pack(0x00, 0)) and w.closed


async def test_silence_keeps_the_line_up():
    # AudioSocket hangs up after 2 s without a frame either way: with nothing to play we send silence.
    w = FakeWriter()
    t = AsteriskTransport(asyncio.StreamReader(), w, 0x10)
    await asyncio.sleep(1.2)
    t._sender.cancel()
    sent = packets(bytes(w.data))
    assert len(sent) == 2 and set(sent) == {(0x10, 320)}  # one 20 ms silent frame every 0.5 s


async def test_clear_drops_queued_audio():
    w = FakeWriter()
    t = AsteriskTransport(reader_with(b""), w, 0x10)
    await t.send(b"\x01\x00" * 24000)  # 1 s
    await t.clear()
    assert t._queue.empty() and not t._buf
    t._sender.cancel()


def test_caller_registry_and_pick():
    u = str(uuid.uuid4())
    register_caller(u.upper(), "0771234567", "ta", True)
    assert pop_call(u) == ("0771234567", "ta", True) and pop_call(u) == ("", "", False)
    assert pick_caller("ucmtrunk", "<sip:+94771234567@10.0.0.1>", "") == "+94771234567"
    assert pick_caller("1001", "", "") == "1001"
    # a PBX can put its trunk name where the number should be (live calls 2026-10-08): never a caller number
    assert pick_caller("ucmtrunk2", "", "") == ""
    assert pick_caller("ucmtrunk2", "", "0771234567") == "0771234567"
    assert pick_caller("ucmtrunk2", "<sip:anonymous@10.0.0.1>", "") == ""


async def test_connection_refused_from_unknown_ip(monkeypatch):
    class W(FakeWriter):
        def get_extra_info(self, name):
            return ("203.0.113.9", 1)
    w = W()
    await asterisk.handle_connection(reader_with(b""), w)
    assert w.closed


async def test_phone_call_through_the_language_menu_to_the_agent(db, monkeypatch):
    """Real AudioSocket server, language menu and agent, fake Gemini: the caller presses 2 (Tamil), hears the agent,
    and hangs up. Keys arrive as 0x03 packets, as Asterisk 22.4+ sends them."""
    import app.ai.live_session as ls
    import app.call_flow as cf
    from app.models import Call
    from tests.fakes import TURN_DONE, FakeClient, FakeSession, audio_msg

    real_start = cf._start_call
    monkeypatch.setattr(cf, "_start_call", lambda ch, caller: (*real_start(ch, caller)[:2], b"\x00\x00" * 1200))
    session = FakeSession([audio_msg("வணக்கம்! நான் Nethmi."), TURN_DONE])
    client = FakeClient([session])
    monkeypatch.setattr(ls, "get_client", lambda: client)
    server = await asyncio.start_server(asterisk.handle_connection, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    call_uuid = uuid.uuid4()
    register_caller(str(call_uuid), "0771234567")

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(packet(0x01, call_uuid.bytes))
    writer.write(packet(0x10, b"\x00\x00" * 160))
    writer.write(packet(0x03, b"2"))  # typed ahead: Tamil
    await writer.drain()

    async def keep_talking():
        while True:
            writer.write(packet(0x10, b"\x00\x00" * 160))
            await writer.drain()
            await asyncio.sleep(0.02)

    talker = asyncio.create_task(keep_talking())
    got = bytearray()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not session.notes and loop.time() < deadline:  # until the agent has been greeted
        p = await asyncio.wait_for(read_packet(reader), 3)
        if p and p[0] == 0x10:
            got.extend(p[1])
    while len(got) < 320 * 3 and loop.time() < deadline:
        p = await asyncio.wait_for(read_packet(reader), 3)
        if p and p[0] == 0x10:
            got.extend(p[1])
    talker.cancel()
    writer.write(packet(0x00))
    await writer.drain()
    await asyncio.sleep(0.5)
    server.close()

    assert len(got) >= 320 * 3
    assert "Greet them now in Tamil" in session.notes[0]
    call = db.query(Call).order_by(Call.id.desc()).first()
    db.refresh(call)
    assert call.channel == "pbx" and call.caller_number == "0771234567" and call.language == "ta"
    assert call.status == "completed" and call.end_reason == "caller_hung_up"
    roles = [t["role"] for t in call.transcript]
    assert roles[0] == "menu" and "agent" in roles


async def test_phone_call_with_the_menu_played_by_the_dialplan(db, monkeypatch):
    """The PBX played the menu itself (Read() in the dialplan) and passed lang=en: the app plays no menu and starts
    the agent in English straight away."""
    import app.ai.live_session as ls
    from app.models import Call
    from tests.fakes import TURN_DONE, FakeClient, FakeSession, audio_msg

    session = FakeSession([audio_msg("Hello!"), TURN_DONE])
    monkeypatch.setattr(ls, "get_client", lambda: FakeClient([session]))
    server = await asyncio.start_server(asterisk.handle_connection, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    call_uuid = uuid.uuid4()
    register_caller(str(call_uuid), "0771234567", "en", True)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(packet(0x01, call_uuid.bytes) + packet(0x10, b"\x00\x00" * 160))
    await writer.drain()

    async def keep_talking():
        while True:
            writer.write(packet(0x10, b"\x00\x00" * 160))
            await writer.drain()
            await asyncio.sleep(0.02)

    talker = asyncio.create_task(keep_talking())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not session.notes and loop.time() < deadline:
        await asyncio.wait_for(read_packet(reader), 3)
    talker.cancel()
    writer.write(packet(0x00))
    await writer.drain()
    await asyncio.sleep(0.5)
    server.close()
    assert "Greet them now in English" in session.notes[0] and "recorded" not in session.notes[0]
    call = db.query(Call).order_by(Call.id.desc()).first()
    db.refresh(call)
    assert call.language == "en"
