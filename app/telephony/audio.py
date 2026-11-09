"""Audio helpers for telephony adapters (numpy, no audioop; works on Python 3.13+).

- Resampler: stateful sample-rate converter for streamed audio (anti-alias filter when going down).
- resample_pcm16: one-shot conversion (fine for whole buffers, not for a stream of chunks).
- VoiceGate: optional noise gate for noisy phone lines (copied from serviceai).
- NoiseSuppressor: RNNoise noise cancellation for caller audio (pyrnnoise's bundled librnnoise).
"""

import ctypes
import logging
from collections import deque

import numpy as np

log = logging.getLogger(__name__)


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate or not pcm:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    n_out = int(round(len(x) * dst_rate / src_rate))
    if n_out == 0:
        return b""
    t_out = np.linspace(0, len(x) - 1, n_out)
    y = np.interp(t_out, np.arange(len(x)), x)
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


def _lowpass(cutoff_hz: float, rate: int, taps: int) -> np.ndarray:
    """Windowed-sinc FIR low-pass filter, unity gain at DC."""
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff_hz / rate * n) * np.hamming(taps)
    return (h / h.sum()).astype(np.float32)


class Resampler:
    """Converts a stream of PCM16 chunks between rates without clicks at chunk boundaries.

    Going down (24 kHz -> 8 kHz) the audio is low-pass filtered first, so frequencies the lower rate can't carry
    don't fold back as a harsh buzz. Going up (8 kHz -> 16 kHz) linear interpolation leaves mirrored copies of the
    voice above the old Nyquist frequency, so the result is low-passed there. Filter state and the interpolation
    position carry over between chunks.
    """

    def __init__(self, src_rate: int, dst_rate: int, taps: int = 63):
        self.src, self.dst = src_rate, dst_rate
        self.step = src_rate / dst_rate  # input samples per output sample
        self.fir = _lowpass(0.45 * dst_rate, src_rate, taps) if dst_rate < src_rate else None
        self.zi = np.zeros(taps - 1 if self.fir is not None else 0, dtype=np.float32)
        self.up_fir = _lowpass(0.45 * src_rate, dst_rate, taps) if dst_rate > src_rate else None
        self.up_zi = np.zeros(taps - 1 if self.up_fir is not None else 0, dtype=np.float32)
        self.tail = np.zeros(1, dtype=np.float32)  # last (filtered) sample of the previous chunk
        self.pos = 1.0  # position of the next output sample, in [tail, chunk...] coordinates

    def process(self, pcm: bytes) -> bytes:
        if self.src == self.dst or not pcm:
            return pcm
        x = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2").astype(np.float32)
        if self.fir is not None:
            buf = np.concatenate([self.zi, x])
            self.zi = buf[len(buf) - len(self.zi):]
            x = np.convolve(buf, self.fir, mode="valid").astype(np.float32)
        x = np.concatenate([self.tail, x])
        last = len(x) - 1
        if self.pos > last:
            self.pos -= last
            self.tail = x[-1:]
            return b""
        n = int((last - self.pos) // self.step) + 1
        t = self.pos + self.step * np.arange(n)
        y = np.interp(t, np.arange(len(x)), x)
        self.pos = t[-1] + self.step - last
        self.tail = x[-1:]
        if self.up_fir is not None:
            buf = np.concatenate([self.up_zi, y.astype(np.float32)])
            self.up_zi = buf[len(buf) - len(self.up_zi):]
            y = np.convolve(buf, self.up_fir, mode="valid")
        return np.clip(np.round(y), -32768, 32767).astype("<i2").tobytes()

    def reset(self) -> None:
        """Forget the previous audio (after a barge-in clears the queue)."""
        self.zi[:] = 0
        self.up_zi[:] = 0
        self.tail[:] = 0
        self.pos = 1.0


GATE_FRAME_MS = 20
GATE_LOOKAHEAD_MS = 80  # audio is held back this long, so a word's first sounds pass once the gate opens
GATE_OPEN_FRAMES = 2  # loud frames in a row needed to open; a single click or knock doesn't
NOISE_SECONDS = 2.0  # the room's noise level is the quietest moment in this long (speech always has gaps)
VOICE_FRAMES = 500  # the caller's level comes from their last 10 s of speech
VOICE_MIN_FRAMES = 50  # ... once there's at least 1 s of it
VOICE_PERCENTILE = 90  # loud enough to be the caller, not so rare that a car horn or a shout sets it
VOICE_RANGE_DB = 15.0  # audio this far below the caller's level is someone or something further away
SILENT_DBFS = -80.0


def level_dbfs(samples: np.ndarray) -> float:
    rms = np.sqrt(np.mean(samples.astype(np.float64) ** 2)) if len(samples) else 0.0
    return max(20 * float(np.log10(rms / 32768 + 1e-12)), -100.0)


class VoiceGate:
    """Passes the caller's own voice and silences background sound: room noise, traffic, a TV, other people.

    The caller speaks right into the handset, so their voice is much louder than anything else on the line.
    To pass, audio must be open_db above the room's noise level and not far below the caller's own speaking
    level, which is learned from the audio that passes. That second threshold stays between min_dbfs and
    max_dbfs, so a wrong estimate can never mute a caller speaking at a normal level. The gate stays open
    for hangover_ms after the last loud enough sound, so quieter word endings and short pauses aren't cut.

    Audio that doesn't pass is replaced with zeros rather than dropped, so the stream stays in real time for
    Gemini's end-of-speech detection; the output is the input delayed by GATE_LOOKAHEAD_MS. Mode "log"
    measures but passes the audio through; "off" does nothing.
    """

    def __init__(self, mode: str = "on", open_db: float = 12.0, min_dbfs: float = -45.0, max_dbfs: float = -32.0,
                 hangover_ms: int = 400, rate: int = 16000):
        self.mode = mode
        self.open_db = open_db
        self.min_dbfs, self.max_dbfs = min_dbfs, max_dbfs
        self.frame = rate * GATE_FRAME_MS // 1000
        self.hangover_frames = max(1, hangover_ms // GATE_FRAME_MS)
        self.noise = SILENT_DBFS  # the room's noise level (dBFS)
        self.voice: float | None = None  # the caller's speaking level (dBFS), once known
        self.open = False
        self._loud = 0  # loud frames in a row while closed
        self._hang = 0  # quiet frames left before the gate closes
        self._levels: deque[float] = deque(maxlen=int(NOISE_SECONDS * 1000 / GATE_FRAME_MS))
        self._voiced: deque[float] = deque(maxlen=VOICE_FRAMES)  # levels of the loud enough frames
        held = rate * GATE_LOOKAHEAD_MS // 1000
        self._held = np.zeros(held, dtype="<i2")  # held-back samples, oldest first
        self._pass = np.zeros(held, dtype=bool)  # whether each held-back sample goes out as is
        self._unmeasured = 0  # newest held-back samples not yet measured (less than a frame)
        self.frames = self.passed = self.openings = 0
        self.peak = -100.0

    def process(self, chunk: bytes) -> bytes:
        if self.mode == "off" or len(chunk) % 2:
            return chunk
        x = np.frombuffer(chunk, dtype="<i2")
        self._held = np.concatenate([self._held, x])
        self._pass = np.concatenate([self._pass, np.zeros(len(x), dtype=bool)])
        self._unmeasured += len(x)
        while self._unmeasured >= self.frame:
            end = len(self._held) - self._unmeasured + self.frame
            if self._step(level_dbfs(self._held[end - self.frame:end])):
                self._pass[:end] = True  # just opened: the held-back start of the word goes out too
            elif self.open:
                self._pass[end - self.frame:end] = True
            self._unmeasured -= self.frame
        out, keep = self._held[: len(x)], self._pass[: len(x)]
        self._held, self._pass = self._held[len(x):], self._pass[len(x):]
        if self.mode != "on":
            return chunk
        return np.where(keep, out, 0).astype("<i2").tobytes()

    def voice_threshold(self) -> float:
        """How loud audio must be to count as the caller: a bit below their usual level once it's known."""
        if self.voice is None:
            return self.min_dbfs
        return min(max(self.voice - VOICE_RANGE_DB, self.min_dbfs), self.max_dbfs)

    def _step(self, db: float) -> bool:
        """Takes one frame's level. Returns True if the gate just opened."""
        self.frames += 1
        self.peak = max(self.peak, db)
        db = max(db, SILENT_DBFS)
        self._levels.append(db)
        self.noise = min(self._levels)
        need = max(self.noise + self.open_db, self.voice_threshold())
        loud = db > need
        opened = False
        if self.open:
            self._hang = self.hangover_frames if loud else self._hang - 1
            self.open = self._hang > 0
        else:
            self._loud = self._loud + 1 if loud else 0
            if self._loud >= GATE_OPEN_FRAMES:
                self.open = opened = True
                self._loud, self._hang = 0, self.hangover_frames
                self.openings += 1
        if self.open and db > max(self.noise + self.open_db, self.min_dbfs):
            self._voiced.append(db)  # the caller (or, before they've spoken, whatever is loudest)
            if len(self._voiced) >= VOICE_MIN_FRAMES and len(self._voiced) % 10 == 0:
                self.voice = float(np.percentile(self._voiced, VOICE_PERCENTILE))
        self.passed += self.open
        return opened

    def summary(self) -> str:
        pct = 100 * self.passed / self.frames if self.frames else 0
        voice = f"{self.voice:.0f} dBFS" if self.voice is not None else "not heard"
        return (
            f"{self.mode}: passed {pct:.0f}% of {self.frames * GATE_FRAME_MS / 1000:.0f}s, {self.openings} openings, "
            f"caller {voice}, needs {max(self.noise + self.open_db, self.voice_threshold()):.0f} dBFS, "
            f"room noise {self.noise:.0f} dBFS, "
            f"peak {self.peak:.0f} dBFS"
        )


RNNOISE_RATE = 48000  # RNNoise only works at 48 kHz, on 480-sample (10 ms) frames
NOISE_DELAY_MS = 20  # output delay; covers one RNNoise frame plus the resamplers' wobble


def _load_rnnoise():
    """pyrnnoise's ctypes handle to librnnoise, or None if the package isn't installed."""
    try:
        from pyrnnoise.rnnoise import lib
    except (ImportError, OSError) as e:
        log.warning("noise suppression off: pyrnnoise not available (%s)", e)
        return None
    return lib


class NoiseSuppressor:
    """Removes background noise mixed into the caller's speech (traffic, fans, hum, keyboard) with RNNoise.

    Unlike the VoiceGate, which silences the line between words, this cleans the audio while the caller is
    talking. It doesn't remove other people talking nearby; the gate handles those when they are quieter.

    Audio is resampled to 48 kHz for RNNoise and back. Like the gate, it returns exactly as many samples as it
    is given, delayed by NOISE_DELAY_MS, so the stream stays in real time for Gemini. Mode "off", or a missing
    pyrnnoise package, passes audio through unchanged.
    """

    def __init__(self, mode: str = "on", rate: int = 16000):
        self.lib = _load_rnnoise() if mode != "off" else None
        self.mode = mode if self.lib else "off"
        self.frames = 0
        self.speech = 0.0  # sum of RNNoise's per-frame speech probability
        if not self.lib:
            return
        self.state = ctypes.c_void_p(self.lib.rnnoise_create(None))
        self.size = self.lib.rnnoise_get_frame_size()
        self._up = Resampler(rate, RNNOISE_RATE)
        self._down = Resampler(RNNOISE_RATE, rate)
        self._in = np.zeros(0, dtype=np.float32)  # 48 kHz samples waiting for a full frame
        self._out = np.zeros(rate * NOISE_DELAY_MS // 1000, dtype="<i2")  # denoised samples not yet returned

    def process(self, chunk: bytes) -> bytes:
        if not self.lib or len(chunk) % 2:
            return chunk
        up = np.frombuffer(self._up.process(chunk), dtype="<i2").astype(np.float32)
        self._in = np.concatenate([self._in, up])
        n = len(self._in) // self.size
        if n:
            frames = self._in[: n * self.size].copy()  # RNNoise works in place on 32-bit floats at int16 scale
            self._in = self._in[n * self.size:]
            ptr = ctypes.POINTER(ctypes.c_float)
            for i in range(n):
                frame = frames[i * self.size:(i + 1) * self.size]
                self.speech += self.lib.rnnoise_process_frame(self.state, frame.ctypes.data_as(ptr),
                                                              frame.ctypes.data_as(ptr))
            self.frames += n
            clean = np.clip(np.round(frames), -32768, 32767).astype("<i2").tobytes()
            self._out = np.concatenate([self._out, np.frombuffer(self._down.process(clean), dtype="<i2")])
        want = len(chunk) // 2
        if len(self._out) < want:  # shouldn't happen once running; keep the timing rather than stall
            self._out = np.concatenate([np.zeros(want - len(self._out), dtype="<i2"), self._out])
        out, self._out = self._out[:want], self._out[want:]
        return out.tobytes()

    def summary(self) -> str:
        speech = f"{100 * self.speech / self.frames:.0f}%" if self.frames else "n/a"
        return f"{self.mode}: {self.frames * 10 / 1000:.0f}s cleaned, average speech probability {speech}"

    def __del__(self):
        if getattr(self, "lib", None) and getattr(self, "state", None):
            self.lib.rnnoise_destroy(self.state)
            self.state = None


def pitch_hz(pcm: bytes, rate: int) -> tuple[float, float]:
    """(median pitch in Hz, seconds of voiced speech) of mono PCM16. (0, 0) when nothing voiced is found.

    40 ms frames, autocorrelation over 70-400 Hz; a frame counts only if it is loud enough and clearly periodic.
    Used to tell a female voice (about 160-230 Hz on the phone) from a male one (about 100-130 Hz)."""
    x = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2").astype(np.float64)
    n, hop = int(0.04 * rate), int(0.02 * rate)
    lo, hi = int(rate / 400), int(rate / 70)
    found = []
    for i in range(0, len(x) - n + 1, hop):
        f = x[i:i + n] - x[i:i + n].mean()
        if np.sqrt((f ** 2).mean()) < 500:
            continue
        spec = np.fft.rfft(f, 2 * n)
        ac = np.fft.irfft(spec * np.conj(spec))[:n]
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[0] > 0 and ac[k] / ac[0] > 0.5:
            found.append(rate / k)
    return (float(np.median(found)), len(found) * hop / rate) if found else (0.0, 0.0)
