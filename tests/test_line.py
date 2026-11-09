"""CallLine: one reader for the whole call; keys for the menus, audio for the agent."""

import asyncio

import pytest

from app.telephony.line import CallLine, Hangup
from tests.fakes import FakeTransport


async def test_audio_is_dropped_until_the_agent_listens():
    t = FakeTransport()
    line = CallLine(t)
    await asyncio.sleep(0.1)
    assert line._audio.empty()  # menus: caller audio isn't kept
    line.start_listening()
    chunk = await asyncio.wait_for(line.receive(), 1)
    assert chunk is not None
    line._reader.cancel()


async def test_keys_typed_ahead_and_timeouts():
    t = FakeTransport(keys="12")
    line = CallLine(t)
    assert line.pending_digit() == "1"
    assert await line.next_digit(0) == "2"
    assert await line.next_digit(0) is None
    assert await line.next_digit(0.05) is None
    asyncio.get_running_loop().call_later(0.02, t.press, "#")
    assert await line.next_digit(1) == "#"
    line._reader.cancel()


async def test_hang_up_ends_waiting_for_a_key_and_the_audio():
    t = FakeTransport(hang_up_after=0.05)
    line = CallLine(t)
    with pytest.raises(Hangup):
        await asyncio.wait_for(line.next_digit(5), 2)
    line.start_listening()
    assert await asyncio.wait_for(line.receive(), 1) is None
    assert await asyncio.wait_for(line.receive(), 1) is None  # every later read sees the hang-up too


async def test_a_key_pressed_just_before_hanging_up_is_still_read():
    t = FakeTransport(keys="5", hang_up_after=0.0)
    line = CallLine(t)
    await asyncio.sleep(0.05)
    assert await line.next_digit(1) == "5"
    with pytest.raises(Hangup):
        await line.next_digit(1)


async def test_the_recorder_gets_the_caller_the_agent_and_the_interruptions():
    class Rec:
        def __init__(self):
            self.log = []

        def caller_audio(self, pcm):
            self.log.append("caller")

        def agent_audio(self, pcm):
            self.log.append("agent")

        def cleared(self):
            self.log.append("cleared")

    rec = Rec()
    line = CallLine(FakeTransport(), rec)
    await asyncio.sleep(0.1)  # the menus: caller audio is recorded even though nobody reads it
    await line.send(b"\x00\x00" * 240)
    await line.clear()
    line._reader.cancel()
    assert {"caller", "agent", "cleared"} <= set(rec.log)
    assert rec.log.index("agent") < rec.log.index("cleared")


async def test_stop_listening_drops_the_audio_the_agent_did_not_read():
    from tests.fakes import FakeTransport
    t = FakeTransport()
    line = CallLine(t)
    line.start_listening()
    await asyncio.sleep(0.1)
    assert line._audio.qsize() > 0
    line.stop_listening()
    assert line._audio.empty() and not line.listening
    line._reader.cancel()
