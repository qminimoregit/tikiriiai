"""Resampler: no mirrored copies of the voice when going up, no clicks between chunks."""

import numpy as np

from app.telephony.audio import Resampler


def _tone(freq: float, rate: int, seconds: float = 1.0, amp: float = 8000) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype("<i2").tobytes()


def _power(pcm: bytes, rate: int, lo: float, hi: float) -> float:
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    freqs = np.fft.rfftfreq(len(x), 1 / rate)
    return float(spec[(freqs >= lo) & (freqs <= hi)].sum())


def test_upsampling_removes_the_mirror_image():
    # 3 kHz at 8 kHz: linear interpolation alone leaves a copy at 5 kHz (8 - 3).
    out = Resampler(8000, 16000).process(_tone(3000, 8000))
    assert _power(out, 16000, 4500, 5500) < 0.01 * _power(out, 16000, 2700, 3300)


def test_upsampling_keeps_speech_band_and_length():
    r = Resampler(8000, 16000)
    src = _tone(1000, 8000)
    out = b"".join(r.process(src[i:i + 320]) for i in range(0, len(src), 320))
    assert abs(len(out) / 2 - 16000) <= 2
    x = np.frombuffer(out, dtype="<i2")[2000:-2000]
    assert 7000 < np.abs(x).max() < 9000


def test_chunked_equals_whole():
    src = _tone(1500, 8000, 0.5)
    whole = np.frombuffer(Resampler(8000, 16000).process(src), dtype="<i2")
    r = Resampler(8000, 16000)
    parts = np.frombuffer(b"".join(r.process(src[i:i + 320]) for i in range(0, len(src), 320)), dtype="<i2")
    n = min(len(whole), len(parts))
    assert np.abs(whole[:n].astype(int) - parts[:n]).max() <= 2


def _voiced(f0: float, rate: int = 8000, seconds: float = 1.0) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    x = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 8))  # a voice-like set of harmonics
    return (4000 * x).astype("<i2").tobytes()


def test_pitch_hz_tells_a_low_voice_from_a_high_one():
    from app.telephony.audio import pitch_hz
    low, secs = pitch_hz(_voiced(115), 8000)
    high, _ = pitch_hz(_voiced(190), 8000)
    assert abs(low - 115) < 5 and abs(high - 190) < 8 and secs > 0.8


def test_pitch_hz_finds_nothing_in_silence_or_noise():
    from app.telephony.audio import pitch_hz
    assert pitch_hz(bytes(16000), 8000) == (0.0, 0.0)
    noise = (np.random.default_rng(1).normal(0, 3000, 8000)).astype("<i2").tobytes()
    assert pitch_hz(noise, 8000)[1] < 0.2
