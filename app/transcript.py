"""One transcript per call: language-menu steps (role "menu"), then the conversation (caller / agent) and tool notes.

Streamed transcription fragments are collected into turns and mirrored to the transport, so the Test Call page
shows them live. Times are seconds from the start of the call.
"""

from __future__ import annotations

import asyncio
import re
import time


class Transcript:
    def __init__(self, transport, started: float | None = None):
        self.transport = transport
        self.started = started if started is not None else time.monotonic()
        self.turns: list[dict] = []
        self._pending = {"caller": "", "agent": ""}
        self.changed = asyncio.Event()  # set whenever a turn is added: the call row is then saved (call_flow)

    def _t(self) -> float:
        return round(time.monotonic() - self.started, 1)

    async def add(self, role: str, text: str) -> None:
        self._pending[role] += text
        await self.transport.event({"type": "partial", "role": role, "text": self._clean(self._pending[role])})

    def pending(self, role: str) -> str:
        """What `role` has said so far in the current, unfinished turn."""
        return self._clean(self._pending[role])

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    async def flush(self, role: str, interrupted: bool = False) -> str:
        text = self._clean(self._pending[role])
        self._pending[role] = ""
        if not text:
            return ""
        turn = {"role": role, "text": text, "t": self._t()}
        if interrupted:
            turn["interrupted"] = True
        self.turns.append(turn)
        self.changed.set()
        await self.transport.event({"type": "transcript", **turn})
        return text

    async def discard(self, role: str) -> str:
        """Drop `role`'s unfinished turn without saving it (a reply cut off before the caller heard it). Returns it."""
        text = self._clean(self._pending[role])
        self._pending[role] = ""
        await self.transport.event({"type": "partial", "role": role, "text": ""})
        return text

    async def flush_all(self) -> str:
        await self.flush("caller")
        return await self.flush("agent")

    async def note(self, role: str, text: str) -> None:
        turn = {"role": role, "text": text, "t": self._t()}
        self.turns.append(turn)
        self.changed.set()
        await self.transport.event({"type": "transcript", **turn})
