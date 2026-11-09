"""Turns an uploaded recording into what calls play: a mono 24 kHz 16-bit WAV at an even loudness.

With ffmpeg (on PATH, or FFMPEG_BIN) any common format works and loudness is levelled with ffmpeg's loudnorm, so
recordings made on different days sound alike. Without ffmpeg only 16-bit WAV files are accepted; they are
converted with numpy and peak-normalised.
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

from .config import get_settings
from .telephony.audio import Resampler
from .telephony.base import OUTPUT_RATE as SAMPLE_RATE

ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".webm"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_SECONDS = 90
MIN_SECONDS = 0.3
LOUDNESS_FILTER = "loudnorm=I=-18:TP=-2:LRA=11"
PEAK_TARGET = 0.7  # numpy fallback: about -3 dBFS


class AudioImportError(ValueError):
    pass


def write_wav(path: Path, pcm: bytes, rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    tmp.replace(path)  # a call reading the old file never sees half a new one


def ffmpeg() -> str | None:
    configured = get_settings().ffmpeg_bin
    return configured if configured and Path(configured).exists() else shutil.which(configured or "ffmpeg")


def _wav_info(path: Path) -> tuple[int, float]:
    with wave.open(str(path), "rb") as w:
        return w.getframerate(), w.getnframes() / w.getframerate()


def _convert_with_ffmpeg(binary: str, src: Path, dst: Path) -> None:
    tmp = dst.with_suffix(".converting.wav")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [binary, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-ac", "1",
           "-af", LOUDNESS_FILTER, "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(tmp)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except subprocess.CalledProcessError as exc:
        tmp.unlink(missing_ok=True)
        detail = (exc.stderr or b"").decode(errors="replace").strip().splitlines()
        raise AudioImportError(f"Could not read this audio file ({detail[-1] if detail else 'unknown format'})")
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        raise AudioImportError("Converting the file took too long")
    tmp.replace(dst)


def _convert_wav_with_numpy(src: Path, dst: Path) -> None:
    try:
        with wave.open(str(src), "rb") as w:
            rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as exc:
        raise AudioImportError(f"Not a WAV file this server can read ({exc}). Upload a 16-bit PCM WAV, "
                               "or install ffmpeg to accept MP3 and other formats.")
    if width != 2:
        raise AudioImportError("Only 16-bit WAV files can be read without ffmpeg. Install ffmpeg, or export the "
                               "recording as 16-bit PCM WAV.")
    x = np.frombuffer(frames, dtype="<i2").astype(np.float32)
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak > 0:
        x *= PEAK_TARGET * 32767 / peak
    pcm = np.clip(x, -32768, 32767).astype("<i2").tobytes()
    write_wav(dst, Resampler(rate, SAMPLE_RATE).process(pcm) if rate != SAMPLE_RATE else pcm)


def convert(src: Path, dst: Path, original_name: str = "") -> int:
    """Convert `src` into the prompt file `dst`. Returns the duration in milliseconds."""
    ext = Path(original_name or src.name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise AudioImportError(f"Upload an audio file ({', '.join(sorted(ALLOWED_EXTENSIONS))})")
    if src.stat().st_size > MAX_UPLOAD_BYTES:
        raise AudioImportError(f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    binary = ffmpeg()
    if binary:
        _convert_with_ffmpeg(binary, src, dst)
    elif ext == ".wav":
        _convert_wav_with_numpy(src, dst)
    else:
        raise AudioImportError("This server has no ffmpeg, so it only accepts WAV files. Upload a WAV, or install "
                               "ffmpeg (sudo apt install ffmpeg) to accept MP3, M4A and OGG.")
    rate, seconds = _wav_info(dst)
    if seconds > MAX_SECONDS:
        dst.unlink(missing_ok=True)
        raise AudioImportError(f"The recording is {seconds:.0f} s long; recordings can be at most {MAX_SECONDS} s")
    if seconds < MIN_SECONDS:
        dst.unlink(missing_ok=True)
        raise AudioImportError("The recording is empty or too short")
    return int(round(seconds * 1000))
