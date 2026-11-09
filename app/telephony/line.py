"""CallLine: the one reader of a call's transport, from the first menu to the last word of the agent.

A transport's receive() must have a single reader for the whole call: an AudioSocket packet is read in two steps,
so cancelling a read halfway would lose a packet header. CallLine reads continuously in its own task:
  - during the menus, caller audio is dropped (only keys matter) and keys go to next_digit();
  - after start_listening(), caller audio is queued for the agent, which reads it through receive().
It passes send / clear / event / close straight to the transport, so the agent's session code takes a CallLine
wherever it took a transport. If given a CallRecorder it also feeds it everything caller and agent hear, menus included.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .base import AudioTransport
from .recording import CallRecorder

log = logging.getLogger(__name__)

AUDIO_QUEUE_LIMIT = 500  # ~10-20 s of caller audio; only reached if the agent stops reading


class Hangup(Exception):
    """The caller hung up."""


class CallLine:
    def __init__(self, transport: AudioTransport, recorder: CallRecorder | None = None):
        self.transport = transport
        self.recorder = recorder
        self.listening = False
        self.hung_up = asyncio.Event()
        self._audio: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read())

    @property
    def dtmf(self) -> asyncio.Queue[str]:
        return self.transport.dtmf

    async def _read(self) -> None:
        try:
            while True:
                chunk = await self.transport.receive()
                if chunk is None:
                    return
                if self.recorder:
                    self.recorder.caller_audio(chunk)
                if self.listening:
                    if self._audio.qsize() >= AUDIO_QUEUE_LIMIT:
                        self._audio.get_nowait()
                    self._audio.put_nowait(chunk)
        except Exception:  # noqa: BLE001 - a broken transport is a hang-up, not a crash
            log.exception("reading the call failed")
        finally:
            self.hung_up.set()
            self._audio.put_nowait(None)

    # ---------- keys (menus) ----------

    def pending_digit(self) -> str | None:
        """A key already pressed (typed ahead), without waiting."""
        return None if self.dtmf.empty() else self.dtmf.get_nowait()

    def clear_digits(self) -> None:
        while not self.dtmf.empty():
            self.dtmf.get_nowait()

    async def next_digit(self, timeout: float | None) -> str | None:
        """The next key the caller presses, or None after `timeout` seconds (0 = only a key already pressed).
        Raises Hangup if the caller hangs up first."""
        if (digit := self.pending_digit()) is not None:
            return digit
        if self.hung_up.is_set():
            raise Hangup
        if timeout is not None and timeout <= 0:
            return None
        get = asyncio.ensure_future(self.dtmf.get())
        hang = asyncio.ensure_future(self.hung_up.wait())
        try:
            done, _ = await asyncio.wait({get, hang}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (get, hang):
                if not t.done():
                    t.cancel()
        if get in done:
            return get.result()
        if hang in done:
            raise Hangup
        return None

    # ---------- audio (agent) ----------

    def start_listening(self) -> None:
        """From now on caller audio is kept for receive() (the agent is about to start)."""
        self.listening = True

    def stop_listening(self) -> None:
        """The agent is done (the caller is back in the menus): drop the caller audio it didn't read. A hang-up marker stays."""
        self.listening = False
        hung = False
        while not self._audio.empty():
            hung = self._audio.get_nowait() is None or hung
        if hung or self.hung_up.is_set():
            self._audio.put_nowait(None)

    async def receive(self) -> bytes | None:
        chunk = await self._audio.get()
        if chunk is None:
            self._audio.put_nowait(None)  # every later receive() also sees the hang-up
        return chunk

    # ---------- passed through ----------

    async def send(self, pcm24k: bytes) -> None:
        if not self.hung_up.is_set():
            if self.recorder:
                self.recorder.agent_audio(pcm24k)
            await self.transport.send(pcm24k)

    async def clear(self) -> None:
        if self.recorder:
            self.recorder.cleared()
        await self.transport.clear()

    async def event(self, data: dict) -> None:
        await self.transport.event(data)

    async def close(self) -> None:
        await self.transport.close()
        if not self._reader.done():
            # The transport has hung up, so receive() ends on its own; don't wait forever if it doesn't.
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(self._reader), 1.0)
            self._reader.cancel()
