"""Transport-agnostic interface. Plug a telephony provider in by implementing AudioTransport.

Audio contract with the AI:
  - receive(): caller audio as PCM16 little-endian, mono, 16 kHz
  - send():    agent audio (and menu prompts) as PCM16 little-endian, mono, 24 kHz
A provider adapter (Asterisk AudioSocket at 8 kHz, Twilio mu-law, ...) converts to/from this format using the
helpers in telephony/audio.py.

Keypresses: while receive() reads, a transport puts every key the caller presses ("0"-"9", "*", "#") into its
`dtmf` queue. CallLine (telephony/line.py) is the only code that calls receive(), for the whole call.
"""

import asyncio
from typing import Protocol

INPUT_RATE = 16000
OUTPUT_RATE = 24000
DTMF_KEYS = frozenset("0123456789*#")


class AudioTransport(Protocol):
    dtmf: asyncio.Queue[str]

    async def receive(self) -> bytes | None:
        """Next chunk of caller audio, or None when the caller hung up."""

    async def send(self, pcm24k: bytes) -> None:
        """Play a chunk of agent audio to the caller."""

    async def clear(self) -> None:
        """Drop any queued agent audio (the caller interrupted)."""

    async def event(self, data: dict) -> None:
        """Optional side-channel for UI events (transcript, status). May be a no-op."""

    async def close(self) -> None:
        """Let queued audio finish, then hang up."""
