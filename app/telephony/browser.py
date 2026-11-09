"""WebSocket transport for the admin Test Call page.

Browser -> server: binary frames of PCM16 16 kHz mono; text frames {"type": "dtmf", "digit": "5"} for the
on-screen keypad and {"type": "hangup"}.
Server -> browser: binary frames of PCM16 24 kHz mono (menu prompts and the agent); JSON text frames for events.

A hang-up from the browser ends the caller's audio but keeps the socket open, so the final {"type": "ended"}
event still reaches the page.
"""

import asyncio
import json

from fastapi import WebSocket, WebSocketDisconnect

from .base import DTMF_KEYS


class BrowserTransport:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.hangup_requested = False
        self.socket_closed = False
        self.dtmf: asyncio.Queue[str] = asyncio.Queue()

    async def receive(self) -> bytes | None:
        while not (self.hangup_requested or self.socket_closed):
            try:
                msg = await self.ws.receive()
            except (WebSocketDisconnect, RuntimeError):
                self.socket_closed = True
                return None
            if msg["type"] == "websocket.disconnect":
                self.socket_closed = True
                return None
            if msg.get("bytes"):
                return msg["bytes"]
            if msg.get("text"):
                try:
                    data = json.loads(msg["text"])
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "hangup":
                    self.hangup_requested = True
                    return None
                if data.get("type") == "dtmf" and str(data.get("digit", "")) in DTMF_KEYS:
                    self.dtmf.put_nowait(str(data["digit"]))
        return None

    async def send(self, pcm24k: bytes) -> None:
        if not (self.socket_closed or self.hangup_requested):
            try:
                await self.ws.send_bytes(pcm24k)
            except (WebSocketDisconnect, RuntimeError):
                self.socket_closed = True

    async def clear(self) -> None:
        await self.event({"type": "interrupted"})

    async def event(self, data: dict) -> None:
        if not self.socket_closed:
            try:
                await self.ws.send_text(json.dumps(data, ensure_ascii=False, default=str))
            except (WebSocketDisconnect, RuntimeError):
                self.socket_closed = True

    async def close(self) -> None:
        # The page plays what it already has, then closes itself after the "ended" event.
        if not self.socket_closed:
            self.socket_closed = True
            try:
                await self.ws.close()
            except RuntimeError:
                pass
