"""Test doubles: a transport with scripted keys, and a scripted Gemini Live client (no network)."""

import asyncio
from pathlib import Path

from google.genai import types

AUDIO = b"\x00\x00" * 2400  # 100 ms at 24 kHz
CALLER_CHUNK = b"\x00\x00" * 320  # 20 ms at 16 kHz


class FakeTransport:
    """A caller who keeps sending (silent) audio, presses `keys` (pre-queued: typed ahead) and hangs up after
    `hang_up_after` seconds, if given."""

    def __init__(self, keys: str = "", hang_up_after: float | None = None):
        self.dtmf: asyncio.Queue[str] = asyncio.Queue()
        for k in keys:
            self.dtmf.put_nowait(k)
        self.events: list[dict] = []
        self.sent = 0
        self.closed = False
        self.cleared = 0
        self.hang_up_after = hang_up_after
        self._start = None

    async def receive(self):
        loop = asyncio.get_running_loop()
        if self._start is None:
            self._start = loop.time()
        if self.hang_up_after is not None and loop.time() - self._start >= self.hang_up_after:
            return None
        await asyncio.sleep(0.02)
        return CALLER_CHUNK

    def press(self, keys: str) -> None:
        for k in keys:
            self.dtmf.put_nowait(k)

    async def send(self, pcm):
        self.sent += len(pcm)

    async def clear(self):
        self.cleared += 1

    async def event(self, data):
        self.events.append(data)

    async def close(self):
        self.closed = True

    def notes(self) -> list[str]:
        return [e["text"] for e in self.events if e["type"] == "transcript" and e["role"] == "menu"]


# ---------- Gemini Live ----------

def audio_msg(text: str = "") -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(
        model_turn=types.Content(parts=[types.Part(inline_data=types.Blob(data=AUDIO, mime_type="audio/pcm"))]),
        output_transcription=types.Transcription(text=text) if text else None,
    ))


def caller_said(text: str) -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(input_transcription=types.Transcription(text=text)))


TURN_DONE = types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))


def handle(h: str) -> types.LiveServerMessage:
    return types.LiveServerMessage(session_resumption_update=types.LiveServerSessionResumptionUpdate(new_handle=h, resumable=True))


def go_away(left: str = "5s") -> types.LiveServerMessage:
    return types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left=left))


def tool(name: str, **args) -> types.LiveServerMessage:
    return types.LiveServerMessage(tool_call=types.LiveServerToolCall(
        function_calls=[types.FunctionCall(id=f"id-{name}", name=name, args=args)]))


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.notes: list[str] = []
        self.audio_in = 0
        self.tool_responses = []

    async def receive(self):
        # Like the SDK: yields until a turn completes, then the iterator ends.
        while True:
            if not self.script:
                await asyncio.sleep(3600)
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, float):
                await asyncio.sleep(item)
                continue
            yield item
            if item.server_content and item.server_content.turn_complete:
                return

    async def send_client_content(self, turns, turn_complete=True):
        self.notes.append(turns.parts[0].text)

    async def send_realtime_input(self, audio=None, text=None):
        if audio is not None:
            self.audio_in += 1
        if text is not None:
            self.notes.append(text)

    async def send_tool_response(self, function_responses):
        self.tool_responses.extend(function_responses)


class FakeLive:
    def __init__(self, sessions):
        self.sessions = list(sessions)
        self.configs = []
        self.used: list[FakeSession] = []

    def connect(self, model, config):
        live = self

        class CM:
            async def __aenter__(self):
                live.configs.append(config)
                item = live.sessions.pop(0)
                if isinstance(item, BaseException):
                    raise item
                live.used.append(item)
                return item

            async def __aexit__(self, *exc):
                return False

        return CM()


class FakeClient:
    def __init__(self, sessions):
        self.aio = type("Aio", (), {})()
        self.aio.live = FakeLive(sessions)


def wav_bytes(seconds: float = 1.0, rate: int = 48000, channels: int = 2, hz: float = 440.0) -> bytes:
    """A small WAV file (a tone), as an upload would be."""
    import io
    import wave

    import numpy as np

    t = np.arange(int(rate * seconds)) / rate
    tone = (np.sin(2 * np.pi * hz * t) * 8000).astype("<i2")
    data = np.repeat(tone[:, None], channels, axis=1).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)
    return buf.getvalue()


def write_wav_file(path: Path, **kw) -> Path:
    path.write_bytes(wav_bytes(**kw))
    return path
