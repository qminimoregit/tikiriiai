"""CallRecorder: caller and agent audio laid out on the call's clock, mixed into one file."""

import wave

import numpy as np
import pytest

from app.config import get_settings
from app.telephony import recording
from app.telephony.recording import RATE, CallRecorder, delete_recordings, recording_files, recording_path


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def tone(value: int, seconds: float, rate: int) -> bytes:
    return np.full(int(rate * seconds), value, dtype="<i2").tobytes()


def read(path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, RATE)
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")


def level(x: np.ndarray, start: float, end: float) -> int:
    return int(np.median(x[int(start * RATE): int(end * RATE)]))


@pytest.fixture(autouse=True)
def _no_ffmpeg(monkeypatch):
    monkeypatch.setattr("app.audio_import.ffmpeg", lambda: None)  # keep the WAV; the MP3 step has its own test


def play_caller(rec, clock, value, seconds):
    for _ in range(int(seconds / 0.02)):
        clock.now += 0.02
        rec.caller_audio(tone(value, 0.02, RATE))


def test_caller_and_agent_land_where_they_were_heard(tmp_path):
    clock = Clock()
    rec = CallRecorder(tmp_path / "1", clock)
    play_caller(rec, clock, 1000, 1.0)  # the caller talks for the first second
    rec.agent_audio(tone(2000, 1.0, 24000))  # the agent answers: sent at 1.0 s, played from 1.0 s
    clock.now = 5.0
    out = read(rec.finish())
    assert abs(len(out) / RATE - 2.0) < 0.1
    assert level(out, 0.2, 0.8) == 1000 and level(out, 1.2, 1.8) == 2000


def test_agent_audio_sent_ahead_of_time_plays_after_the_previous_chunk(tmp_path):
    clock = Clock()
    rec = CallRecorder(tmp_path / "1", clock)
    rec.agent_audio(tone(1000, 1.0, 24000))  # both sent at once (faster than real time) ...
    rec.agent_audio(tone(3000, 1.0, 24000))  # ... the second plays after the first
    clock.now = 4.0
    out = read(rec.finish())
    assert abs(len(out) / RATE - 2.0) < 0.1
    assert level(out, 0.3, 0.7) == 1000 and level(out, 1.3, 1.7) == 3000


def test_audio_the_caller_cut_off_is_not_in_the_recording(tmp_path):
    clock = Clock()
    rec = CallRecorder(tmp_path / "1", clock)
    rec.agent_audio(tone(2000, 4.0, 24000))  # 4 s sent at once
    clock.now = 1.0
    rec.cleared()  # the caller interrupts after 1 s: the rest is never played
    clock.now = 3.0
    out = read(rec.finish())
    assert abs(len(out) / RATE - 1.0) < 0.1


def test_both_voices_are_mixed_when_they_overlap(tmp_path):
    clock = Clock()
    rec = CallRecorder(tmp_path / "1", clock)
    rec.agent_audio(tone(2000, 1.0, 24000))
    play_caller(rec, clock, 500, 1.0)  # the caller talks over the agent
    clock.now = 3.0
    out = read(rec.finish())
    assert level(out, 0.3, 0.7) == 2500


def test_a_long_call_is_written_as_it_goes_and_memory_stays_small(tmp_path):
    clock = Clock()
    rec = CallRecorder(tmp_path / "1", clock)
    play_caller(rec, clock, 800, 12.0)
    assert (tmp_path / "1.wav").exists()  # already on disk before the call ends
    assert len(rec._caller) < 3.5 * RATE
    out = read(rec.finish())
    assert abs(len(out) / RATE - 12.0) < 0.1 and level(out, 0, 12) == 800


def test_a_quiet_stretch_on_the_line_is_kept_as_silence(tmp_path):
    clock = Clock()
    rec = CallRecorder(tmp_path / "1", clock)
    play_caller(rec, clock, 1000, 0.5)
    clock.now += 4.0  # nothing arrives for 4 s
    play_caller(rec, clock, 1000, 0.5)
    out = read(rec.finish())
    assert abs(len(out) / RATE - 5.0) < 0.1
    assert level(out, 1.0, 4.0) == 0 and level(out, 4.7, 5.0) == 1000


def test_nothing_is_saved_for_a_call_with_no_audio(tmp_path):
    assert CallRecorder(tmp_path / "1", Clock()).finish() is None
    assert not list(tmp_path.iterdir())


def test_a_broken_recorder_never_raises_into_the_call(tmp_path):
    rec = CallRecorder(tmp_path / "missing" / "deep" / "1", Clock())
    (tmp_path / "missing").write_text("a file where the folder should be")
    for _ in range(100):
        rec.caller_audio(tone(1, 0.02, RATE))
    rec.agent_audio(tone(1, 0.1, 24000))
    rec.cleared()
    rec.finish()  # no exception


def test_the_wav_becomes_an_mp3_when_ffmpeg_is_there(tmp_path, monkeypatch):
    import shutil
    binary = shutil.which("ffmpeg")
    if not binary:
        pytest.skip("ffmpeg is not installed")
    monkeypatch.setattr("app.audio_import.ffmpeg", lambda: binary)
    clock = Clock()
    rec = CallRecorder(tmp_path / "7", clock)
    play_caller(rec, clock, 1000, 1.0)
    saved = rec.finish()
    assert saved == tmp_path / "7.mp3" and saved.stat().st_size > 0
    assert not (tmp_path / "7.wav").exists()


# ---------- finding the files ----------

@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "recordings_dir", str(tmp_path))
    return tmp_path


def test_recordings_are_found_by_call_id(folder):
    for name in ("5.mp3", "5-2.mp3", "5-2.wav", "51.mp3", "6.wav", "notes.txt", "5.mp3.bak"):
        (folder / name).write_bytes(b"x")
    assert recording_files(5) == [{"name": "5.mp3", "part": 1}, {"name": "5-2.mp3", "part": 2}]  # an MP3 beats a WAV
    assert recording_files(6) == [{"name": "6.wav", "part": 1}] and recording_files(7) == []
    assert recording_path(5, "5-2.mp3") == folder / "5-2.mp3"
    assert recording_path(5, "51.mp3") is None and recording_path(5, "../5.mp3") is None
    delete_recordings(5)
    assert sorted(p.name for p in folder.iterdir()) == ["5.mp3.bak", "51.mp3", "6.wav", "notes.txt"]


def test_no_folder_means_no_recordings(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "recordings_dir", str(tmp_path / "nope"))
    assert recording_files(1) == []
