"""Asterisk AudioSocket transport: one TCP connection per phone call from the PBX.

Packets are a 1-byte type, a 2-byte big-endian length, then the payload. Asterisk sends the call UUID first,
then 20 ms frames of signed-linear mono audio, and a 0x03 packet (one ASCII byte) for every key the caller presses
(Asterisk 22.4+; also in the 20 and 21 branches since the March 2025 backport). The audio packet type gives the
sample rate (0x10 = 8 kHz, 0x12 = 16 kHz, ...); we reply with the same type and rate, paced in real time, and send
a zero-length hangup packet to end the call.

AudioSocket hangs up after 2 s without a frame in either direction. A trunk that suppresses silence sends nothing
while the caller is quiet, so whenever we have nothing to play we send a 20 ms silent frame every 0.5 s: while the
caller listens to a menu and thinks, and while the agent waits for an answer.
"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
import time
import uuid as uuidlib

from ..config import get_settings
from . import ami
from .audio import Resampler
from .base import DTMF_KEYS, INPUT_RATE, OUTPUT_RATE

log = logging.getLogger(__name__)

HEADER = struct.Struct(">BH")
T_HANGUP, T_UUID, T_DTMF, T_ERROR = 0x00, 0x01, 0x03, 0xFF
SLIN_RATES = {0x10: 8000, 0x11: 12000, 0x12: 16000, 0x13: 24000, 0x14: 32000, 0x15: 44100, 0x16: 48000}
FRAME_SECONDS = 0.02
PREBUFFER_SECONDS = 0.06  # held back at the start of each talkspurt so network jitter doesn't cause gaps
KEEPALIVE_SECONDS = 0.5  # with nothing to play, a silent frame this often (AudioSocket's timeout is 2 s)
GOODBYE_DRAIN_SECONDS = 10
FIRST_AUDIO_TIMEOUT = 2.0
CALLER_TTL_SECONDS = 60

# The dialplan registers the caller ID here (via /api/pbx/call-start) just before AudioSocket connects, and, when the
# dialplan played the language menu itself, the language the caller picked: {uuid: (caller, time, lang, menu)}.
_callers: dict[str, tuple[str, float, str, bool]] = {}
# where the call is in the dialplan, so a key press (#) can send it back to the menu: {uuid: (channel, context, exten)}
_routes: dict[str, tuple[str, str, str]] = {}


def pick_caller(*candidates: str) -> str:
    """The caller's phone number from the dialplan's caller-ID fields (From number, P-Asserted-Identity, From name).

    A trunk can put its own username in From and the real number in another field, so the first one holding a
    phone number wins; otherwise the first non-empty one (an internal extension such as "1001"). Names with letters
    are never a number: CMC's UCM sends its trunk name ("ucmtrunk2"), which the agent must not read out as the
    caller's number. With nothing else, the caller ID is empty and the agent asks for a number.
    """
    users = []
    for value in candidates:
        m = re.search(r"(?:sips?|tel):([^@;>]+)", value or "")
        user = (m.group(1) if m else value or "").strip()
        if user and re.fullmatch(r"\+?[\d\s()\-]+", user):
            users.append(user)
    return next((u for u in users if len(re.sub(r"\D", "", u)) >= 9), users[0] if users else "")


def register_caller(call_uuid: str, caller: str, lang: str = "", menu: bool = False,
                    channel: str = "", context: str = "", exten: str = "") -> None:
    now = time.monotonic()
    for key, (_, t, _l, _m) in list(_callers.items()):
        if now - t > CALLER_TTL_SECONDS:
            _callers.pop(key, None)
            _routes.pop(key, None)
    _callers[call_uuid.lower()] = (caller, now, lang, menu)
    if channel and exten:
        _routes[call_uuid.lower()] = (channel, context or "from-ucm-trunk", exten)


def pop_call(call_uuid: str) -> tuple[str, str, bool]:
    """(caller number, menu language, the dialplan played the menu) registered for this call; empty if the dialplan
    sent nothing."""
    item = _callers.pop(call_uuid.lower(), None)
    return (item[0], item[2], item[3]) if item else ("", "", False)


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    try:
        msg_type, length = HEADER.unpack(await reader.readexactly(HEADER.size))
        return msg_type, (await reader.readexactly(length) if length else b"")
    except (asyncio.IncompleteReadError, ConnectionError):
        return None


def ip_allowed(ip: str, allowed: list[str]) -> bool:
    return "*" in allowed or ip in allowed


def dtmf_digit(payload: bytes) -> str | None:
    digit = payload[:1].decode("ascii", "ignore")
    return digit if digit in DTMF_KEYS else None


class AsteriskTransport:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, audio_type: int = 0x10,
                 first_audio: bytes = b"", early_digits: list[str] | None = None, call_uuid: str = ""):
        self.call_uuid = call_uuid
        self.reader = reader
        self.writer = writer
        self.audio_type = audio_type
        self.rate = SLIN_RATES.get(audio_type, 8000)
        self.frame_bytes = int(self.rate * FRAME_SECONDS) * 2
        self.closed = False
        self.menu_requested = False  # the caller pressed # and was sent back to the language menu
        self.dtmf: asyncio.Queue[str] = asyncio.Queue()
        for d in early_digits or []:
            self.dtmf.put_nowait(d)
        self._pending_first = first_audio
        self._up = Resampler(self.rate, INPUT_RATE)
        self._down = Resampler(OUTPUT_RATE, self.rate)
        self._buf = bytearray()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._sender = asyncio.create_task(self._send_loop())

    async def receive(self) -> bytes | None:
        if self._pending_first:
            first, self._pending_first = self._pending_first, b""
            return self._up.process(first)
        while not self.closed:
            packet = await read_packet(self.reader)
            if packet is None or packet[0] in (T_HANGUP, T_ERROR):
                self.closed = True
                return None
            msg_type, payload = packet
            if msg_type in SLIN_RATES:
                return self._up.process(payload)
            if msg_type == T_DTMF and (digit := dtmf_digit(payload)):
                log.info("DTMF %r", digit)
                self.dtmf.put_nowait(digit)
            # other packet types are ignored
        return None

    async def send(self, pcm24k: bytes) -> None:
        self._buf.extend(self._down.process(pcm24k))
        while len(self._buf) >= self.frame_bytes:
            self._queue.put_nowait(bytes(self._buf[: self.frame_bytes]))
            del self._buf[: self.frame_bytes]

    async def clear(self) -> None:
        self._buf.clear()
        self._down.reset()
        while not self._queue.empty():
            self._queue.get_nowait()

    async def event(self, data: dict) -> None:
        pass  # no UI side-channel on a phone call

    def _write(self, frame: bytes) -> None:
        self.writer.write(HEADER.pack(self.audio_type, len(frame)) + frame)

    async def _send_loop(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        keepalive = bytes(self.frame_bytes)
        while True:
            try:
                if self._queue.empty():
                    try:
                        frame = await asyncio.wait_for(self._queue.get(), KEEPALIVE_SECONDS)
                    except asyncio.TimeoutError:
                        self._write(keepalive)
                        await self.writer.drain()
                        continue
                    # Start of a new talkspurt: let a few frames build up so the pacing below doesn't run dry.
                    await asyncio.sleep(PREBUFFER_SECONDS)
                    deadline = loop.time()
                else:
                    frame = self._queue.get_nowait()
                deadline = max(deadline + FRAME_SECONDS, loop.time())
                await asyncio.sleep(deadline - loop.time())
                self._write(frame)
                await self.writer.drain()
            except ConnectionError:
                self.closed = True
                return

    async def _play_out(self) -> None:
        """Let everything queued reach the caller."""
        if self._buf:
            self._queue.put_nowait(bytes(self._buf) + bytes(self.frame_bytes - len(self._buf)))
            self._buf.clear()
        for _ in range(int(GOODBYE_DRAIN_SECONDS / 0.1)):
            if self.closed or self._queue.empty():
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(2 * FRAME_SECONDS + PREBUFFER_SECONDS)  # the last frame may still be in flight

    async def close(self) -> None:
        # Let the goodbye finish playing before hanging up.
        await self._play_out()
        self._sender.cancel()
        if not self.closed:
            self.closed = True
            try:
                self.writer.write(HEADER.pack(T_HANGUP, 0))
                await self.writer.drain()
            except ConnectionError:
                pass
        self.writer.close()


async def _first_audio(reader: asyncio.StreamReader, digits: list[str]) -> tuple[int, bytes] | None:
    """The first audio packet, which tells us the sample rate. None if the caller hung up first. Keys pressed
    before it are collected in `digits`."""
    while True:
        packet = await read_packet(reader)
        if packet is None or packet[0] in (T_HANGUP, T_ERROR):
            return None
        if packet[0] in SLIN_RATES:
            return packet
        if packet[0] == T_DTMF and (digit := dtmf_digit(packet[1])):
            digits.append(digit)


async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    from ..call_flow import handle_call

    peer = writer.get_extra_info("peername")
    ip = peer[0] if peer else ""
    if not ip_allowed(ip, get_settings().pbx_allowed_ip_list):
        log.warning("AudioSocket connection from %s refused (not in PBX_ALLOWED_IPS)", ip)
        writer.close()
        return
    try:
        try:
            packet = await asyncio.wait_for(read_packet(reader), 15)
        except asyncio.TimeoutError:
            packet = None
        if not packet or packet[0] != T_UUID or len(packet[1]) != 16:
            log.warning("AudioSocket connection without a UUID packet, dropping it")
            writer.close()
            return
        call_uuid = str(uuidlib.UUID(bytes=packet[1]))
        caller, menu_lang, pbx_menu = pop_call(call_uuid)
        digits: list[str] = []
        try:
            first = await asyncio.wait_for(_first_audio(reader, digits), FIRST_AUDIO_TIMEOUT)
        except asyncio.TimeoutError:
            first = (0x10, b"")  # nothing yet: assume 8 kHz, Asterisk's default
        if first is None:
            log.info("PBX call %s hung up before any audio", call_uuid)
            writer.close()
            return
        audio_type, payload = first
        log.info("PBX call %s from %r at %s Hz", call_uuid, caller, SLIN_RATES[audio_type])
        transport = AsteriskTransport(reader, writer, audio_type, payload, digits, call_uuid=call_uuid)
        route = _routes.pop(call_uuid.lower(), None)
        if route:
            ami.track(route[0], transport, route[1], route[2])
        try:
            await handle_call(transport, channel="pbx", caller_number=caller, menu_lang=menu_lang, pbx_menu=pbx_menu)
        finally:
            if route:
                ami.untrack(route[0])
    except Exception:  # noqa: BLE001 - log and drop this call, keep the server running
        log.exception("PBX call failed")
        writer.close()


_server: asyncio.Server | None = None


async def start_server() -> None:
    global _server
    s = get_settings()
    _server = await asyncio.start_server(handle_connection, s.pbx_audiosocket_host, s.pbx_audiosocket_port)
    log.info("PBX AudioSocket listening on %s:%s", s.pbx_audiosocket_host, s.pbx_audiosocket_port)


def stop_server() -> None:
    global _server
    if _server:
        _server.close()
        _server = None
