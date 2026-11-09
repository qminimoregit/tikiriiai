"""Uploaded recordings become mono 24 kHz 16-bit WAVs, with or without ffmpeg."""

import subprocess
import wave

import pytest

from app import audio_import
from app.audio_import import AudioImportError, convert
from tests.fakes import write_wav_file

needs_ffmpeg = pytest.mark.skipif(not audio_import.ffmpeg(), reason="ffmpeg is not installed")


def info(path):
    with wave.open(str(path), "rb") as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes() / w.getframerate()


@needs_ffmpeg
def test_stereo_48k_wav_becomes_mono_24k(tmp_path):
    src = write_wav_file(tmp_path / "in.wav", seconds=1.5, rate=48000, channels=2)
    ms = convert(src, tmp_path / "out.wav", "menu.wav")
    channels, width, rate, seconds = info(tmp_path / "out.wav")
    assert (channels, width, rate) == (1, 2, 24000) and abs(seconds - 1.5) < 0.05 and abs(ms - 1500) < 50


@needs_ffmpeg
def test_mp3_is_converted(tmp_path):
    src = write_wav_file(tmp_path / "in.wav", seconds=1.0)
    mp3 = tmp_path / "in.mp3"
    subprocess.run([audio_import.ffmpeg(), "-loglevel", "error", "-y", "-i", str(src), str(mp3)], check=True)
    convert(mp3, tmp_path / "out.wav", "recording.mp3")
    assert info(tmp_path / "out.wav")[:3] == (1, 2, 24000)


@needs_ffmpeg
def test_too_long_and_not_audio_are_refused(tmp_path):
    src = write_wav_file(tmp_path / "long.wav", seconds=91, rate=8000, channels=1)
    with pytest.raises(AudioImportError, match="at most 90 s"):
        convert(src, tmp_path / "out.wav", "long.wav")
    assert not (tmp_path / "out.wav").exists()
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"this is not audio")
    with pytest.raises(AudioImportError, match="Could not read this audio file"):
        convert(junk, tmp_path / "out2.wav", "junk.mp3")


def test_other_file_types_are_refused(tmp_path):
    doc = tmp_path / "notes.txt"
    doc.write_text("hello")
    with pytest.raises(AudioImportError, match="Upload an audio file"):
        convert(doc, tmp_path / "out.wav", "notes.txt")


def test_without_ffmpeg_wav_still_works_and_mp3_is_explained(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_import, "ffmpeg", lambda: None)
    src = write_wav_file(tmp_path / "in.wav", seconds=1.0, rate=44100, channels=2)
    convert(src, tmp_path / "out.wav", "in.wav")
    assert info(tmp_path / "out.wav")[:3] == (1, 2, 24000)
    with pytest.raises(AudioImportError, match="only accepts WAV"):
        convert(src, tmp_path / "out.wav", "in.mp3")
