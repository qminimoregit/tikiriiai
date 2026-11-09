"""Records a whole call (menu prompts, caller and agent) into one mono 16 kHz file for the admin call page.

The caller and the agent are kept on separate tracks laid out on the call's own clock and mixed when written:
  - the caller's audio arrives in real time, so each chunk goes where it was spoken;
  - the agent's audio (and the menu prompts) arrive faster than real time, so each chunk goes right after the
    previous one, never earlier than now: the same timeline the caller hears it on;
  - when the caller interrupts, agent audio that was queued but never played is dropped.
Only the last couple of seconds stay in memory; older audio is appended to <stem>.wav as the call goes on. finish()
turns the WAV into an MP3 when ffmpeg is available. Recording must never break a call: errors are logged only.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import get_settings
from .audio import Resampler
from .base import OUTPUT_RATE

log = logging.getLogger(__name__)

RATE = 16000
WINDOW_SECONDS = 2.0  # audio newer than this stays in memory (an interruption can still drop agent audio)
GAP_SECONDS = 0.5  # caller audio that arrives later than this after the previous chunk starts a new stretch
MP3_BITRATE = "32k"
FILE_PATTERN = re.compile(r"^(\d+)(?:-(\d+))?\.(mp3|wav)$")


class CallRecorder:
    def __init__(self, stem: Path, clock: Callable[[], float] = time.monotonic):
        self.stem = Path(stem)
        self._clock = clock
        self._t0 = clock()
        self._caller = np.zeros(0, dtype=np.int16)  # samples from self._base on
        self._agent = np.zeros(0, dtype=np.int16)
        self._base = 0  # the first sample not yet written to the file
        self._caller_end = 0
        self._agent_end = 0
        self._down = Resampler(OUTPUT_RATE, RATE)
        self._wav: wave.Wave_write | None = None
        self.failed = False

    # ---------- what the line feeds in ----------

    def _now(self) -> int:
        return int((self._clock() - self._t0) * RATE)

    def caller_audio(self, pcm16k: bytes) -> None:
        self._guard(self._caller_audio, pcm16k)

    def agent_audio(self, pcm24k: bytes) -> None:
        self._guard(self._agent_audio, pcm24k)

    def cleared(self) -> None:
        """The caller interrupted: agent audio that was queued but not yet played will never be heard."""
        self._guard(self._cleared)

    def _guard(self, fn, *args) -> None:
        if self.failed:
            return
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - a recording problem must never end a call
            log.exception("recording %s failed, recording stops", self.stem)
            self.failed = True

    def _caller_audio(self, pcm: bytes) -> None:
        x = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2")
        now = self._now()
        start = self._caller_end
        if now - len(x) - start > GAP_SECONDS * RATE:  # the line was quiet for a while: put it where it was heard
            start = now - len(x)
        self._put(self._caller, start, x, "_caller")
        self._caller_end = start + len(x)
        self._flush(now - int(WINDOW_SECONDS * RATE))

    def _agent_audio(self, pcm: bytes) -> None:
        x = np.frombuffer(self._down.process(pcm), dtype="<i2")
        start = max(self._now(), self._agent_end)
        self._put(self._agent, start, x, "_agent")
        self._agent_end = start + len(x)

    def _cleared(self) -> None:
        now = self._now()
        if self._agent_end > now:
            keep = max(0, now - self._base)
            self._agent = self._agent[:keep]
            self._agent_end = max(now, self._base)
        self._down.reset()

    def _put(self, track: np.ndarray, start: int, x: np.ndarray, name: str) -> None:
        if len(x) == 0 or start + len(x) <= self._base:
            return
        if start < self._base:  # already written: only the newer part can still be placed
            x, start = x[self._base - start:], self._base
        at = start - self._base
        if len(track) < at + len(x):
            track = np.concatenate([track, np.zeros(at + len(x) - len(track), dtype=np.int16)])
        track[at: at + len(x)] = x
        setattr(self, name, track)

    # ---------- the file ----------

    def _flush(self, upto: int, everything: bool = False) -> None:
        """Append everything before sample `upto` to the WAV (in blocks of about a second, unless `everything`)."""
        if upto - self._base < (1 if everything else RATE):
            return
        n = upto - self._base
        mixed = np.zeros(n, dtype=np.int32)
        for track in (self._caller, self._agent):
            part = track[:n]
            mixed[: len(part)] += part
        out = np.clip(mixed, -32768, 32767).astype("<i2")
        if self._wav is None:
            self.stem.parent.mkdir(parents=True, exist_ok=True)
            self._wav = wave.open(str(self.stem.with_suffix(".wav")), "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(RATE)
        self._wav.writeframes(out.tobytes())
        self._caller, self._agent = self._caller[n:], self._agent[n:]
        self._base = upto

    def finish(self) -> Path | None:
        """Write what is left and close the file (MP3 if ffmpeg is there). Returns the file, or None if nothing was
        recorded. Blocking: run it in a thread."""
        try:
            if not self.failed:
                end = min(max(self._caller_end, self._agent_end), self._now() + RATE // 2)
                self._flush(end, everything=True)
        except Exception:  # noqa: BLE001
            log.exception("recording %s could not be finished", self.stem)
            self.failed = True
        wav_path = self.stem.with_suffix(".wav")
        if self._wav is not None:
            try:
                self._wav.close()
            except Exception:  # noqa: BLE001
                log.exception("recording %s could not be closed", self.stem)
            self._wav = None
        if not wav_path.exists():
            return None
        from ..audio_import import ffmpeg  # imported here: audio_import itself needs the telephony package

        binary = ffmpeg()
        if binary is None:
            return wav_path
        mp3 = self.stem.with_suffix(".mp3")
        try:
            subprocess.run([binary, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav_path), "-ac", "1",
                            "-b:a", MP3_BITRATE, str(mp3)], check=True, capture_output=True, timeout=300)
        except Exception:  # noqa: BLE001 - keep the WAV rather than lose the call
            log.exception("recording %s could not be converted to MP3, keeping the WAV", self.stem)
            mp3.unlink(missing_ok=True)
            return wav_path
        wav_path.unlink(missing_ok=True)
        return mp3


# ---------- finding recordings on disk ----------

def recording_files(call_id: int) -> list[dict]:
    """The recordings of a call, oldest part first: [{"name": "5.mp3", "part": 1}, {"name": "5-2.mp3", "part": 2}]."""
    folder = get_settings().recordings_path
    if not folder.is_dir():
        return []
    parts: dict[int, str] = {}
    for f in folder.iterdir():
        m = FILE_PATTERN.match(f.name)
        if m and int(m.group(1)) == call_id:
            part = int(m.group(2) or 1)
            if part not in parts or f.suffix == ".mp3":  # an MP3 wins over a leftover WAV
                parts[part] = f.name
    return [{"name": parts[p], "part": p} for p in sorted(parts)]


def recording_path(call_id: int, name: str) -> Path | None:
    """The file for `name` if it is one of this call's recordings, else None (never a path outside the folder)."""
    for f in recording_files(call_id):
        if f["name"] == name:
            return get_settings().recordings_path / name
    return None


def delete_recordings(call_id: int) -> None:
    for f in recording_files(call_id):
        (get_settings().recordings_path / f["name"]).unlink(missing_ok=True)
    folder = get_settings().recordings_path
    if folder.is_dir():  # a leftover WAV next to an MP3
        for f in folder.iterdir():
            m = FILE_PATTERN.match(f.name)
            if m and int(m.group(1)) == call_id:
                f.unlink(missing_ok=True)
