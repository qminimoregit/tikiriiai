"""When does the agent ask "are you still there?" The caller talking over a slow transcript must never trigger it."""

import asyncio
import time
from types import SimpleNamespace

import numpy as np

from app.ai.live_session import LiveLink, Silence, TurnAudio, _pump_caller_audio
from app.telephony.base import OUTPUT_RATE

PROMPT = 10.0


def silence_after(seconds: float, *, agent_asked: bool) -> Silence:
    """A Silence whose agent finished a spoken turn `seconds` ago."""
    s = Silence()
    s.turn_audio = True
    s.agent_finished() if agent_asked else None
    s.last_caller = s.agent_until = time.monotonic() - seconds
    return s


def test_the_caller_is_asked_after_the_agent_has_waited_for_them():
    assert silence_after(PROMPT + 1, agent_asked=True).should_prompt(PROMPT, model_busy=False)
    assert not silence_after(PROMPT - 3, agent_asked=True).should_prompt(PROMPT, model_busy=False)


def test_hearing_the_caller_holds_the_prompt_back_without_waiting_for_a_transcript():
    s = silence_after(PROMPT + 5, agent_asked=True)
    assert s.should_prompt(PROMPT, model_busy=False)
    s.caller_heard()  # the voice gate is open: they are talking right now
    assert not s.should_prompt(PROMPT, model_busy=False)
    assert s.awaiting_caller is False


def test_hearing_the_caller_does_not_cancel_the_prompt_count_or_a_goodbye():
    s = Silence()
    s.prompts, s.goodbye_text = 1, "bye"
    s.caller_heard()  # a noise on the line is not an answer
    assert (s.prompts, s.goodbye_text) == (1, "bye")
    s.caller_spoke()  # a real transcription is
    assert (s.prompts, s.goodbye_text) == (0, "")


def test_a_model_that_has_not_answered_yet_gets_twice_as_long():
    s = silence_after(PROMPT + 1, agent_asked=True)
    s.caller_spoke()
    s.last_caller = s.agent_until = time.monotonic() - (PROMPT + 1)  # the caller spoke last, 11 s ago
    assert not s.should_prompt(PROMPT, model_busy=False)
    s.last_caller = s.agent_until = time.monotonic() - (2 * PROMPT + 1)
    assert s.should_prompt(PROMPT, model_busy=False)


def test_a_model_in_the_middle_of_a_turn_is_not_interrupted_until_it_looks_stuck():
    s = silence_after(2 * PROMPT + 1, agent_asked=True)
    assert not s.should_prompt(PROMPT, model_busy=True)
    s.last_caller = s.agent_until = time.monotonic() - (3 * PROMPT + 1)
    assert s.should_prompt(PROMPT, model_busy=True)


def test_the_agent_is_only_waiting_for_the_caller_after_it_spoke():
    s = Silence()
    s.agent_finished()  # a turn with only a tool call and no speech
    assert s.awaiting_caller is False
    s.agent_audio(4800)
    s.agent_finished()
    assert s.awaiting_caller is True
    s.interrupted()  # the caller cut in
    assert s.awaiting_caller is False


class _Line:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def receive(self):
        return self.chunks.pop(0) if self.chunks else None


class _Gate:
    """Open for the chunks listed in `open_for`; passes audio through."""

    def __init__(self, open_for):
        self.open_for, self.n, self.open = open_for, 0, False

    def process(self, chunk):
        self.open = self.n in self.open_for
        self.n += 1
        return chunk


def test_the_audio_pump_tells_the_watchdog_when_the_gate_hears_the_caller():
    sent = []
    link = SimpleNamespace(send_audio=lambda c: _append(sent, c))
    ctx = SimpleNamespace(winding_down=False)
    silence = silence_after(PROMPT + 5, agent_asked=True)
    asyncio.run(_pump_caller_audio(_Line([b"\0\0"] * 3), link, ctx, silence, None, _Gate({1})))
    assert len(sent) == 3  # audio always goes on to the model
    assert not silence.should_prompt(PROMPT, model_busy=False)  # chunk 1 was the caller's voice


def test_the_audio_pump_without_a_voice_leaves_the_silence_alone():
    link = SimpleNamespace(send_audio=lambda c: _append([], c))
    ctx = SimpleNamespace(winding_down=False)
    silence = silence_after(PROMPT + 5, agent_asked=True)
    asyncio.run(_pump_caller_audio(_Line([b"\0\0"] * 3), link, ctx, silence, None, _Gate(set())))
    assert silence.should_prompt(PROMPT, model_busy=False)


async def _append(lst, chunk):
    lst.append(chunk)


def test_the_same_line_said_again_and_again_is_counted():
    """Calls 36, 54 and 60: one question asked four to six times in a row."""
    link = LiveLink()
    ask = "කරුණාකර ඔබේ ශ්‍රේණිය සඳහන් කරන්න."
    assert [link.said(ask) for _ in range(3)] == [1, 2, 3]
    assert link.said("කරුණාකර ඔබේ ගම සඳහන් කරන්න.") == 1  # a different question starts again
    assert link.said(ask + " ") == 1


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def speech(seconds, level=8000):
    return np.full(int(seconds * OUTPUT_RATE), level, dtype="<i2").tobytes()


def test_a_turn_whose_audio_stops_after_the_first_word_is_flagged():
    clock = FakeClock()
    turn = TurnAudio(clock)
    turn.text("ආයුබෝවන්! ප්‍රමිල සර්ගේ පන්ති ඇමතීම ගැන ස්තූතියි.")
    clock.t += 0.1
    turn.audio(speech(0.9))  # call 64: the first word, then nothing until the caller said "Hello"
    clock.t += 6.4
    line, broken = turn.end("interrupted")
    assert broken and "0.9 s audio" in line and "longest gap 5.5 s" in line and "interrupted" in line
    assert turn.end("complete") is None  # the turn_complete after an interruption logs nothing more


def test_audio_sent_faster_than_it_plays_is_a_normal_turn():
    clock = FakeClock()
    turn = TurnAudio(clock)
    for _ in range(10):
        turn.audio(speech(0.5))
        clock.t += 0.1
    turn.text("දහය වසර තියරි පන්තිය සෙනසුරාදා උදේ අටට. එය නිවැරදිද?")
    line, broken = turn.end("complete")
    assert not broken and "5.0 s audio (0.0 s quiet)" in line and "first audio after 0.0 s" in line
    assert "longest gap 0.0 s" in line


def voiced(f0, seconds):
    t = np.arange(int(OUTPUT_RATE * seconds)) / OUTPUT_RATE
    return (4000 * sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 8))).astype("<i2").tobytes()


def test_the_voice_is_judged_while_the_turn_plays():
    turn = TurnAudio(FakeClock())
    turn.audio(voiced(115, 1.5))
    assert turn.early_pitch() is None and not turn.checked  # too early to judge
    turn.audio(voiced(115, 0.5))
    hz, secs = turn.early_pitch()  # 2 s in: judged while the rest still plays
    assert 105 < hz < 125 and secs >= 1.5 and turn.checked
    assert turn.early_pitch() is None  # once per checkpoint
    for _ in range(4):
        turn.audio(voiced(115, 0.5))
        turn.early_pitch()
    turn.audio(voiced(115, 0.5))
    assert turn.early_pitch() is None  # past PITCH_SECONDS: nothing more to measure
    turn.end("complete")
    assert not turn.checked  # the next turn starts unjudged


def test_text_without_any_audio_and_silent_audio_are_flagged_but_a_tool_wait_is_not_a_gap():
    clock = FakeClock()
    turn = TurnAudio(clock)
    turn.text("ආයුබෝවන්!")
    clock.t += 1.0
    assert turn.end("complete")[1]  # no audio at all

    turn.audio(speech(2.0, level=0))
    line, broken = turn.end("complete")
    assert "2.0 s audio (2.0 s quiet)" in line and not broken

    turn.audio(speech(1.0))
    turn.tool_call()
    clock.t += 5.0  # the tool runs; the caller hears the end of the first sentence, then a short pause
    turn.audio(speech(1.0))
    line, broken = turn.end("complete")
    assert not broken and "longest gap 0.0 s" in line
