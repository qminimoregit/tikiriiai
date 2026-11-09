"""Recordings an admin uploads (the language menu, and the "unavailable" apology per language): stored as 24 kHz mono
WAV in MEDIA_DIR and played to callers."""

from __future__ import annotations

import io
import logging
import time
import wave
from pathlib import Path

import numpy as np

from .audio import resample_pcm16
from .base import OUTPUT_RATE

log = logging.getLogger(__name__)

MIN_SECONDS = 0.5
MAX_SECONDS = 60


def media_root() -> Path:
    from ..config import get_settings

    return get_settings().media_path.resolve()


def menu_path(name: str) -> Path | None:
    """The file for a stored name, or None if it is blank, outside the media folder or missing."""
    if not name:
        return None
    root = media_root()
    path = (root / name).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def wav_to_pcm24k(data: bytes) -> bytes:
    """A 16-bit PCM WAV of any rate / channel count -> PCM16 mono 24 kHz. ValueError with a readable message if unusable."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            if width != 2:
                raise ValueError("The recording must be a 16-bit WAV file.")
            raw = w.readframes(frames)
    except (wave.Error, EOFError):
        raise ValueError("That is not a WAV file. Upload it from the admin page, which converts MP3/M4A for you.") from None
    if not rate or not channels:
        raise ValueError("The WAV file has no audio.")
    seconds = frames / rate
    if seconds < MIN_SECONDS:
        raise ValueError("The recording is too short (under half a second).")
    if seconds > MAX_SECONDS:
        raise ValueError(f"The recording is too long (over {MAX_SECONDS} seconds).")
    x = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        x = x[: len(x) // channels * channels].reshape(-1, channels).astype(np.int32).mean(axis=1).astype("<i2")
    return resample_pcm16(x.tobytes(), rate, OUTPUT_RATE)


def save_menu_audio(pcm24k: bytes, prefix: str = "language-menu") -> str:
    """Writes the clip under a fresh name (so browsers never show a cached older one) and returns that name.
    `prefix` tells the clips apart in the folder: the language menu, or "unavailable-si" and so on."""
    root = media_root()
    root.mkdir(parents=True, exist_ok=True)
    name = f"{prefix}-{int(time.time() * 1000)}.wav"
    with wave.open(str(root / name), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(OUTPUT_RATE)
        w.writeframes(pcm24k)
    return name


def delete_menu_audio(name: str) -> None:
    if path := menu_path(name):
        path.unlink(missing_ok=True)


def load_menu_pcm(name: str) -> bytes:
    """The stored clip as PCM16 24 kHz, or b"" (logged) when there is none or the file is missing."""
    if not name:
        return b""
    path = menu_path(name)
    if path is None:
        log.warning("language menu file %r is missing; calls start without the menu", name)
        return b""
    try:
        with wave.open(str(path), "rb") as w:
            return w.readframes(w.getnframes())
    except Exception:  # noqa: BLE001
        log.warning("language menu file %r could not be read", name, exc_info=True)
        return b""


ASTERISK_NAME = "lang-menu"  # the dialplan plays <sounds dir name>/lang-menu


def sync_asterisk_sound(name: str) -> None:
    """Writes (or, when name is blank, removes) lang-menu.wav in ASTERISK_SOUNDS_DIR for the dialplan to play."""
    from ..config import get_settings

    folder = get_settings().asterisk_sounds_dir
    if not folder:
        return
    target = Path(folder) / f"{ASTERISK_NAME}.wav"
    try:
        pcm = load_menu_pcm(name)
        if not pcm:
            target.unlink(missing_ok=True)
            return
        tmp = target.with_suffix(".tmp")
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(resample_pcm16(pcm, OUTPUT_RATE, 8000))
        tmp.chmod(0o644)
        tmp.replace(target)  # atomic: a call never plays a half-written file
    except OSError:
        log.warning("could not write the language menu for Asterisk to %s", folder, exc_info=True)
