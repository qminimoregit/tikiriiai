"""The agent's part of a call: bridges the caller's line to a Gemini Live session (adapted from cmc-new).

call_flow.handle_call() runs the language menu first and then run_agent() with the language the caller picked.
run_agent() doesn't create or finalise the Call row and doesn't hang up; it returns what happened.

  - Long-lived tasks: caller audio -> Gemini, silence watchdog, hang-up after goodbye.
  - Connection loop: open a Live session; stream model audio/transcripts/tool calls to the line.
    Gemini closes a connection after ~10 minutes and warns first with GoAway; the loop then reconnects with the
    latest session-resumption handle (the conversation carries over) while the caller stays on the line.
    Context-window compression lets a session run past the 15-minute audio limit.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
from collections import deque
from dataclasses import dataclass

import numpy as np
from google.genai import types

from .. import languages
from ..clock import now_utc
from ..config import get_settings
from ..snapshot import TikiriiSnapshot
from ..telephony.audio import NoiseSuppressor, Resampler, VoiceGate, level_dbfs, pitch_hz
from ..telephony.base import INPUT_RATE, OUTPUT_RATE, AudioTransport
from ..transcript import Transcript
from .client import get_client
from .prompt_builder import (
    build_system_instruction,
    call_recap,
    goodbye_nudge_note,
    greeting_note,
    no_response_note,
    redo_note,
    resume_note,
    still_there_note,
    wrap_up_note,
)
from .tools import DECLARATIONS, CallContext, dispatch
from .voices import MIN_VOICED_SECONDS, call_voice, pitch_matches, voice_gender

log = logging.getLogger(__name__)

MAX_RECONNECTS = 5
GOODBYE_SETTLE_SECONDS = 1.5  # after end_call, no new audio for this long = the goodbye has been sent
GOODBYE_GRACE_SECONDS = 8.0
GOODBYE_NUDGE_SECONDS = 4.0
WRAP_UP_BEFORE_SECONDS = 60
GOAWAY_MARGIN_SECONDS = 2.0
BUFFER_CHUNKS = 100  # caller audio kept while reconnecting (~2-4 s)
REPEAT_SIMILAR = 0.85  # agent lines this alike count as the same line said again
REPEAT_WARN = 3  # the same line this many times in a row is logged
STALL_GAP_SECONDS = 3.0  # the caller heard nothing for this long in the middle of an agent turn
SHORT_TURN_SECONDS = 1.0  # a finished turn with text but less audio than this
QUIET_DBFS = -50.0  # agent audio quieter than this counts as silence
PITCH_SECONDS = 4  # the start of each agent turn that is measured for pitch
PITCH_RATE = 8000
# The voice is first judged this far into a turn, then again every step until PITCH_SECONDS, so a reply in the wrong
# voice is cut off after a couple of seconds instead of playing to the end (call 43: a whole line in a male voice).
EARLY_PITCH_SECONDS = 2.0
EARLY_PITCH_STEP = 0.5
MAX_VOICE_RENEWALS = 3  # sessions replaced per call because the voice came out as the wrong gender
HOLD_MIN_VOICED = 1.0  # a held reply is judged once this much of it is voiced speech...
HOLD_EXTRA_SECONDS = 1.0  # ...or released unjudged this long after the hold, if it never got that much
GOODBYE_RE = languages.goodbye_regex()  # from every language file's goodbye_phrases

START_SENSITIVITY = {"low": types.StartSensitivity.START_SENSITIVITY_LOW, "high": types.StartSensitivity.START_SENSITIVITY_HIGH}
END_SENSITIVITY = {"low": types.EndSensitivity.END_SENSITIVITY_LOW, "high": types.EndSensitivity.END_SENSITIVITY_HIGH}


# ---------- config ----------

def live_config(instruction: str, snap: TikiriiSnapshot, handle: str | None, lang: str) -> types.LiveConnectConfig:
    s = get_settings()
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        temperature=min(snap.profile.temperature, s.live_max_temperature),
        system_instruction=instruction,
        tools=[{"function_declarations": DECLARATIONS}],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=call_voice(snap.profile.agent_voice))
            )
        ),
        # The language file lists the speech-to-text hints: the call's language, plus English for the English
        # words Sri Lankans mix in. Leaving the other language out keeps Tamil from being heard as Sinhala.
        input_audio_transcription=types.AudioTranscriptionConfig(
            language_codes=list(languages.get(lang).transcription_codes)),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                start_of_speech_sensitivity=START_SENSITIVITY.get(s.vad_start_sensitivity),
                end_of_speech_sensitivity=END_SENSITIVITY.get(s.vad_end_sensitivity),
                prefix_padding_ms=s.vad_prefix_padding_ms or None,
                silence_duration_ms=s.vad_silence_ms or None,
            ),
            turn_coverage=types.TurnCoverage.TURN_INCLUDES_ALL_INPUT if s.vad_turn_coverage_all else None,
        ),
        context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
        session_resumption=types.SessionResumptionConfig(handle=handle),
    )


async def send_note(session, text: str) -> None:
    """A bracketed instruction to the model (greeting cue, still-there prompt...). One place to switch method."""
    if get_settings().live_note_mode == "realtime_text":
        await session.send_realtime_input(text=text)
    else:
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=True
        )


def parse_duration(value: str | None) -> float:
    """'12s' / '1.5s' / '500ms' -> seconds. Unknown -> 0."""
    if not value:
        return 0.0
    m = re.fullmatch(r"\s*([\d.]+)\s*(ms|s)?\s*", str(value))
    if not m:
        return 0.0
    n = float(m.group(1))
    return n / 1000 if m.group(2) == "ms" else n


# ---------- per-call helpers ----------

class Silence:
    """Tracks when the caller last spoke and when the agent's audio will have finished playing."""

    def __init__(self):
        now = time.monotonic()
        self.last_caller = now
        self.agent_until = now  # estimated end of agent playback on the caller's side
        self.last_audio_at = 0.0
        self.turn_audio = False  # agent sent audio in the current turn
        self.awaiting_caller = False  # the agent finished speaking and the caller has not been heard since
        self.prompts = 0
        self.goodbye_text = ""

    def caller_spoke(self) -> None:
        """Gemini transcribed something the caller said (it arrives late, often after they stop)."""
        self.last_caller = time.monotonic()
        self.awaiting_caller = False
        self.prompts = 0
        self.goodbye_text = ""

    def caller_heard(self) -> None:
        """The voice gate hears the caller's voice on the line right now. Quieter than caller_spoke(): it only
        stops the "are you still there?" prompt, so a noise can't cancel a goodbye or the prompt count."""
        self.last_caller = time.monotonic()
        self.awaiting_caller = False

    def agent_finished(self) -> None:
        """The agent's turn is over: it is the caller's turn to speak."""
        if self.turn_audio:
            self.awaiting_caller = True

    def agent_audio(self, nbytes: int) -> None:
        now = time.monotonic()
        self.agent_until = max(now, self.agent_until) + nbytes / 2 / OUTPUT_RATE
        self.last_audio_at = now
        self.turn_audio = True

    def interrupted(self) -> None:
        self.agent_until = time.monotonic()
        self.turn_audio = False
        self.caller_spoke()

    def silent_for(self) -> float:
        return time.monotonic() - max(self.last_caller, self.agent_until)

    def should_prompt(self, prompt_seconds: float, model_busy: bool) -> bool:
        """Is it time to ask "are you still there?" Only once the caller has really had their turn:
          - the agent asked something and the caller has been quiet for `prompt_seconds`;
          - the caller spoke last and the model has not answered yet: wait twice as long (a slow answer is not the
            caller's silence);
          - the model is mid-turn or running a tool: wait three times as long, only to recover a stuck model."""
        factor = 3 if model_busy else 1 if self.awaiting_caller else 2
        return self.silent_for() >= prompt_seconds * factor


class LiveLink:
    """The current Live session (it changes on reconnect) plus what must survive a reconnect."""

    def __init__(self):
        self.session = None
        self.handle: str | None = None
        self.reconnects = 0
        self.voice_renewals = 0
        self.redo: str | None = None  # a reply cut off for its voice (what was heard of it): say it again on renewal
        self.in_turn = False
        self.last_event = "call start"  # what happened just before the current turn, for the log
        self.session_turn = 0  # agent turns since this session opened
        self.go_away_pending = False
        self.reconnect_needed = asyncio.Event()
        self._timer: asyncio.TimerHandle | None = None
        self._buffer: deque[bytes] = deque(maxlen=BUFFER_CHUNKS)
        self._lines: deque[str] = deque(maxlen=REPEAT_WARN)  # the agent's last few lines

    def said(self, text: str) -> int:
        """Remember a line the agent said. Returns how many times in a row it has now said nearly the same line (on
        calls 36, 54 and 60 the agent asked one question four to six times)."""
        line = " ".join(text.split())
        same = 1
        for earlier in reversed(self._lines):
            if difflib.SequenceMatcher(None, earlier, line).ratio() < REPEAT_SIMILAR:
                break
            same += 1
        self._lines.append(line)
        return same

    def context(self) -> str:
        return f"turn {self.session_turn} of this session, after {self.last_event}"

    async def attach(self, session) -> None:
        self.session = session
        self.in_turn = False
        self.session_turn = 0
        self.go_away_pending = False
        self.reconnect_needed.clear()
        mime = f"audio/pcm;rate={INPUT_RATE}"
        while self._buffer:
            await session.send_realtime_input(audio=types.Blob(data=self._buffer.popleft(), mime_type=mime))

    def detach(self) -> None:
        self.session = None
        if self._timer:
            self._timer.cancel()
            self._timer = None

    async def send_audio(self, chunk: bytes) -> None:
        session = self.session
        if session is None:
            self._buffer.append(chunk)
            return
        try:
            await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={INPUT_RATE}"))
        except Exception:  # noqa: BLE001 - the connection is going away; the model loop notices and reconnects
            self._buffer.append(chunk)

    async def note(self, text: str) -> bool:
        if self.session is None:
            return False
        try:
            await send_note(self.session, text)
            self.last_event = f"note {text[:30]!r}"
            return True
        except Exception:  # noqa: BLE001
            log.warning("could not send note to the model", exc_info=True)
            return False

    def go_away(self, time_left: str | None) -> None:
        """Gemini will close this connection soon: reconnect at the next pause, or just before the deadline."""
        self.go_away_pending = True
        self.last_event = "GoAway"
        delay = max(0.0, parse_duration(time_left) - GOAWAY_MARGIN_SECONDS)
        if self._timer is None:
            self._timer = asyncio.get_running_loop().call_later(delay, self.reconnect_needed.set)
        if not self.in_turn:
            self.reconnect_needed.set()

    def turn_done(self) -> None:
        self.in_turn = False
        if self.go_away_pending:
            self.reconnect_needed.set()


class TurnAudio:
    """What one agent turn sent, for the log. On call 64 Gemini sent the greeting as text three times but almost no
    audio: this tells apart no audio, silent audio and audio that arrived too slowly to play without a gap.

    A gap is time the caller heard nothing in the middle of the turn: a chunk arriving after everything before it
    had already played out, or the turn ending long after the last audio. Time spent in a tool call doesn't count."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.pitch: tuple[float, float] | None = None  # (Hz, voiced seconds) of the turn that just ended
        self.reset()

    def reset(self) -> None:
        self.started: float | None = None  # first text or audio of the turn
        self.first_audio: float | None = None
        self.play_until: float | None = None  # when the audio sent so far ends on the caller's side
        self.seconds = self.quiet = self.gap = 0.0
        self.chars = 0
        self._low = Resampler(OUTPUT_RATE, PITCH_RATE)
        self._pitch_pcm = bytearray()  # the first seconds of the turn, at PITCH_RATE
        self._next_check = EARLY_PITCH_SECONDS
        self.checked = False  # early_pitch() has judged the voice of this turn

    def _now(self) -> float:
        now = self._clock()
        if self.started is None:
            self.started = now
        return now

    def text(self, text: str) -> None:
        self._now()
        self.chars += len(text.strip())

    def audio(self, pcm: bytes) -> None:
        now = self._now()
        length = len(pcm) / 2 / OUTPUT_RATE
        if self.first_audio is None:
            self.first_audio = now
        if self.play_until is not None and now > self.play_until:
            self.gap = max(self.gap, now - self.play_until)
        self.play_until = max(now, self.play_until or now) + length
        self.seconds += length
        if len(self._pitch_pcm) < PITCH_SECONDS * PITCH_RATE * 2:
            self._pitch_pcm += self._low.process(pcm)
        if level_dbfs(np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2")) < QUIET_DBFS:
            self.quiet += length

    def tool_call(self) -> None:
        """The model is waiting for a tool: the pause until its next audio isn't a gap."""
        self.play_until = None

    def early_pitch(self) -> tuple[float, float] | None:
        """(Hz, voiced seconds) of the turn so far, while it is still playing: at EARLY_PITCH_SECONDS of audio and
        every EARLY_PITCH_STEP after, up to PITCH_SECONDS. None between checkpoints and when too little of it is
        voiced to judge."""
        have = len(self._pitch_pcm) / 2 / PITCH_RATE
        if have < self._next_check or self._next_check > PITCH_SECONDS:
            return None
        self._next_check += EARLY_PITCH_STEP
        hz, voiced = pitch_hz(bytes(self._pitch_pcm), PITCH_RATE)
        if voiced < MIN_VOICED_SECONDS:
            return None
        self.checked = True
        return hz, voiced

    def pitch_now(self) -> tuple[float, float] | None:
        """(Hz, voiced seconds) of the turn so far, or None when nothing is voiced yet."""
        hz, voiced = pitch_hz(bytes(self._pitch_pcm), PITCH_RATE) if self._pitch_pcm else (0.0, 0.0)
        return (hz, voiced) if voiced else None

    def end(self, how: str) -> tuple[str, bool] | None:
        """The turn ended (complete | interrupted): (log line, whether it looks broken), or None if it said nothing."""
        self.pitch = None
        if self.started is None:
            return None
        self.pitch = pitch_hz(bytes(self._pitch_pcm), PITCH_RATE) if self._pitch_pcm else None
        now = self._clock()
        if self.play_until is not None:
            self.gap = max(self.gap, now - self.play_until)
        elif self.first_audio is None:
            self.gap = max(self.gap, now - self.started)
        first = f"{self.first_audio - self.started:.1f} s" if self.first_audio is not None else "never"
        line = (f"{self.seconds:.1f} s audio ({self.quiet:.1f} s quiet), first audio after {first}, longest gap "
                f"{self.gap:.1f} s, {self.chars} chars, {how}"
                + (f", pitch {self.pitch[0]:.0f} Hz" if self.pitch and self.pitch[1] else ""))
        broken = (self.gap >= STALL_GAP_SECONDS or (self.chars > 0 and self.seconds == 0)
                  or (how == "complete" and self.chars > 0 and self.seconds < SHORT_TURN_SECONDS))
        self.reset()
        return line, broken


# ---------- tasks ----------

async def _pump_caller_audio(line: AudioTransport, link: LiveLink, ctx: CallContext, silence: Silence,
                             denoiser: NoiseSuppressor | None, gate: VoiceGate | None) -> None:
    while True:
        chunk = await line.receive()
        if chunk is None:
            return
        if ctx.winding_down:
            # Keep reading so a hang-up still ends the call, but the model must not hear "thanks, bye" after
            # end_call (or "yes" while the caller is put through) and start another reply.
            continue
        if denoiser:
            chunk = denoiser.process(chunk)
        if gate:
            chunk = gate.process(chunk)
            if gate.open:  # the caller is talking: don't ask "are you still there?" over them
                silence.caller_heard()
        await link.send_audio(chunk)


def _wrong_voice(pitch: tuple[float, float], link: LiveLink, ctx: CallContext,
                 min_voiced: float = MIN_VOICED_SECONDS) -> str | None:
    """Gemini sometimes speaks a whole session in the other gender's voice. Such a session is replaced by a fresh one
    (the call so far is carried over) up to MAX_VOICE_RENEWALS times per call. Returns the gender the voice should
    have when this session is to be replaced, else None."""
    voice = ctx.snap.profile.agent_voice
    hz, voiced = pitch
    if pitch_matches(voice, hz, voiced, min_voiced):
        return None
    expected = "female" if voice_gender(voice) == "female" else "male"
    log.warning("call %s: the voice sounds wrong for a %s voice (%.0f Hz over %.1f s, %s)", ctx.call_id, expected, hz,
                voiced, link.context())
    if link.voice_renewals >= MAX_VOICE_RENEWALS:
        log.error("call %s: the voice is still wrong after %s renewals, keeping it", ctx.call_id, MAX_VOICE_RENEWALS)
        return None
    link.voice_renewals += 1
    link.handle = None  # a resumed session would keep the same voice: start a fresh one
    return expected


async def _check_voice(pitch: tuple[float, float], link: LiveLink, ctx: CallContext, transcript: Transcript) -> None:
    """A finished turn too short to judge while it played: renew the session; the caller already heard the turn."""
    if expected := _wrong_voice(pitch, link, ctx):
        await transcript.note("note", f"Voice sounded wrong for a {expected} voice ({pitch[0]:.0f} Hz, {link.context()}): "
                                      "connection renewed")
        link.reconnect_needed.set()


async def _cut_wrong_voice(pitch: tuple[float, float], link: LiveLink, ctx: CallContext, transcript: Transcript,
                           line: AudioTransport, min_voiced: float = MIN_VOICED_SECONDS) -> bool:
    """A turn still playing in the wrong voice: drop what the caller hasn't heard yet and renew the session, which
    then says the reply again (run_agent sends redo_note). True when the reply was cut off."""
    if not (expected := _wrong_voice(pitch, link, ctx, min_voiced)):
        return False
    log.info("call %s: cutting the reply off, the new session says it again", ctx.call_id)
    await line.clear()
    said = await transcript.discard("agent")  # not a turn the caller heard: kept out of the recap
    await transcript.note("note", f"Voice sounded wrong for a {expected} voice ({pitch[0]:.0f} Hz, {link.context()}): "
                                  "reply cut off and said again" + (f": {said}" if said else ""))
    link.redo = said
    link.reconnect_needed.set()
    return True


async def _pump_model(session, line: AudioTransport, link: LiveLink, ctx: CallContext, transcript: Transcript,
                      silence: Silence) -> None:
    turn = TurnAudio()
    cut = False  # a reply was cut off for its voice: this session is being replaced
    hold = get_settings().voice_hold_seconds
    held: list[bytes] = []  # the start of the current reply, kept back until its voice has been checked
    released = hold <= 0  # the current reply is playing (its voice was judged, or holding is off)

    async def play(data: bytes) -> None:
        silence.agent_audio(len(data))
        await line.send(data)

    async def release() -> None:
        nonlocal released
        released = True
        for data in held:
            await play(data)
        held.clear()

    async def judge_held() -> bool:
        """Judge the held start of a reply: True when it was in the wrong voice and was thrown away (the session is
        being replaced); otherwise it is played. Too little voiced speech to judge is played unjudged."""
        nonlocal cut
        pitch = turn.pitch_now()
        if pitch and pitch[1] >= HOLD_MIN_VOICED:
            turn.checked = True  # judged: no second check when the turn ends
        if pitch and pitch[1] >= HOLD_MIN_VOICED and \
                await _cut_wrong_voice(pitch, link, ctx, transcript, line, min_voiced=HOLD_MIN_VOICED):
            held.clear()
            cut = True
            return True
        await release()
        return False

    def turn_over(how: str) -> None:
        checked = turn.checked  # judged while it played: no second check now
        nonlocal released
        held.clear()  # a held start that was never judged is dropped with an interrupted or cut turn
        released = hold <= 0
        if (ended := turn.end(how)) is not None:
            line, broken = ended
            (log.warning if broken else log.info)("call %s agent turn: %s (turn %s, after %s)", ctx.call_id, line,
                                                  link.session_turn, link.last_event)
        if turn.pitch and not checked and not ctx.winding_down:
            asyncio.get_running_loop().create_task(_check_voice(turn.pitch, link, ctx, transcript))

    while True:
        async for msg in session.receive():
            if cut:
                # Until run_agent swaps the session, only the caller's words still count: no more of the cut reply,
                # no tool calls, and no resumption handle (resuming would bring the wrong voice back).
                sc = msg.server_content
                if sc and sc.input_transcription and sc.input_transcription.text:
                    await transcript.add("caller", sc.input_transcription.text)
                continue
            if msg.session_resumption_update:
                upd = msg.session_resumption_update
                if upd.resumable and upd.new_handle:
                    link.handle = upd.new_handle
            if msg.go_away:
                log.info("call %s: GoAway, time left %s", ctx.call_id, msg.go_away.time_left)
                link.go_away(msg.go_away.time_left)

            sc = msg.server_content
            if sc:
                if sc.input_transcription and sc.input_transcription.text:
                    await transcript.add("caller", sc.input_transcription.text)
                    if sc.input_transcription.text.strip():
                        silence.caller_spoke()
                if sc.model_turn and sc.model_turn.parts:
                    await transcript.flush("caller")
                    if not link.in_turn:
                        link.session_turn += 1
                    link.in_turn = True
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            data = part.inline_data.data
                            turn.audio(data)
                            if ctx.winding_down:  # the goodbye (or hold line) is never held back
                                await release()
                            if not released:
                                held.append(data)
                                if turn.seconds >= hold and (turn.seconds >= hold + HOLD_EXTRA_SECONDS or
                                                             (p := turn.pitch_now()) and p[1] >= HOLD_MIN_VOICED):
                                    if await judge_held():
                                        break
                                continue
                            silence.agent_audio(len(data))
                            if not ctx.winding_down and (pitch := turn.early_pitch()) and \
                                    await _cut_wrong_voice(pitch, link, ctx, transcript, line):
                                cut = True
                                break
                            await line.send(data)
                    if cut:
                        turn_over("cut")
                        continue
                if sc.output_transcription and sc.output_transcription.text:
                    await transcript.flush("caller")
                    await transcript.add("agent", sc.output_transcription.text)
                    turn.text(sc.output_transcription.text)
                # After end_call nothing cuts the goodbye, and nothing cuts the hold line while the caller is put
                # through: the call hangs up (or moves on) once it has played.
                if sc.interrupted and not ctx.winding_down:
                    link.last_event = "interrupted"
                    silence.interrupted()
                    await line.clear()
                    await transcript.flush("agent", interrupted=True)
                    link.turn_done()
                    turn_over("interrupted")
                if sc.turn_complete:
                    if held and await judge_held():  # a short reply that never reached the hold time
                        turn_over("cut")
                        continue
                    await transcript.flush("caller")
                    agent_text = await transcript.flush("agent")
                    if agent_text and not ctx.winding_down:
                        silence.goodbye_text = agent_text if GOODBYE_RE.search(agent_text) else ""
                        if (times := link.said(agent_text)) >= REPEAT_WARN:
                            log.warning("call %s: the agent said nearly the same line %s times in a row: %r",
                                        ctx.call_id, times, agent_text[:120])
                    if not ctx.winding_down:  # after end_call the waiting task needs to know it played
                        silence.agent_finished()
                        silence.turn_audio = False
                    link.turn_done()
                    turn_over("complete")

            if msg.tool_call and msg.tool_call.function_calls:
                if held and await judge_held():
                    turn_over("cut")
                    continue
                await transcript.flush("caller")
                if not link.in_turn:
                    link.session_turn += 1
                link.in_turn = True
                link.last_event = "tool " + ", ".join(fc.name for fc in msg.tool_call.function_calls)
                turn.tool_call()
                # end_call hands back the goodbye to say, unless the agent already said one in this turn.
                ctx.said_goodbye = bool(GOODBYE_RE.search(transcript.pending("agent")))
                responses = []
                for fc in msg.tool_call.function_calls:
                    args = dict(fc.args or {})
                    result = await dispatch(fc.name, args, ctx)
                    log.info("call %s tool %s(%s) -> %s", ctx.call_id, fc.name, args, result)
                    responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
                    await transcript.note("tool", _tool_summary(fc.name, args, result))
                    await line.event({"type": "tool", "name": fc.name, "args": args, "result": result})
                await session.send_tool_response(function_responses=responses)
                if ctx.ended:
                    ctx.ended_event.set()


def _tool_summary(name: str, args: dict, result: dict) -> str:
    if "error" in result:
        return f"{name}: {result['error']}"
    if name == "send_details":
        return f"Details ({', '.join(args.get('topics') or [])}) being sent to {result.get('phone', '')}" \
            if "phone" in result else f"Details: {result.get('result', '')}"
    if name == "check_phone":
        return f"Phone number: {result.get('phone', '')}"
    if name == "book_demo":
        who = " · ".join(x for x in (args.get("name"), args.get("business_name"), args.get("industry")) if x)
        done = "updated" if "corrected" in result.get("result", "") else "saved"
        return f"Demo request {done}: {who} ({result.get('phone', '')})" if "say" in result and "phone" in result \
            else f"Demo request: {result.get('result', '')}"
    if name == "end_call":
        return f"Agent ended the call ({args.get('reason', '')})"
    return name


async def _hang_up_after_goodbye(ctx: CallContext, silence: Silence) -> None:
    """After end_call, return once the goodbye audio has been sent: closing the line then plays it out. The goodbye
    comes in the reply to end_call, so only audio sent after end_call counts (or none, if it was said before)."""
    await ctx.ended_event.wait()
    start = time.monotonic()
    while time.monotonic() - start < GOODBYE_GRACE_SECONDS:
        heard_after = silence.last_audio_at > start
        if (heard_after or ctx.said_goodbye) and silence.turn_audio and \
                time.monotonic() - silence.last_audio_at >= GOODBYE_SETTLE_SECONDS:
            return
        await asyncio.sleep(0.1)
    log.info("call %s: no goodbye audio after end_call, hanging up", ctx.call_id)


async def _silence_watchdog(link: LiveLink, ctx: CallContext, silence: Silence, deadline: float) -> None:
    """Nudge the model when the line goes quiet, wrap up before the time limit, and make sure a goodbye ends
    the call even if the model forgets end_call. Returns when the call should end."""
    s = get_settings()
    loop = asyncio.get_running_loop()
    wrapped_up = False
    while True:
        await asyncio.sleep(0.5)
        if ctx.winding_down or link.session is None:
            continue
        if not wrapped_up and deadline - loop.time() <= WRAP_UP_BEFORE_SECONDS:
            wrapped_up = await link.note(wrap_up_note(ctx.language))
            continue
        if silence.goodbye_text and silence.silent_for() >= GOODBYE_NUDGE_SECONDS:
            text, silence.goodbye_text = silence.goodbye_text, ""
            log.info("call %s: goodbye without end_call, nudging the model", ctx.call_id)
            await link.note(goodbye_nudge_note())
            await asyncio.sleep(GOODBYE_GRACE_SECONDS)
            if not ctx.ended and not silence.goodbye_text and time.monotonic() - silence.last_caller > GOODBYE_GRACE_SECONDS:
                log.info("call %s: still no end_call after goodbye (%r), hanging up", ctx.call_id, text[:60])
                ctx.ended, ctx.end_reason = True, "caller_goodbye"
                return
            continue
        if not silence.should_prompt(s.silence_prompt_seconds, link.in_turn):
            continue
        if silence.prompts < s.silence_max_prompts:
            silence.prompts += 1
            silence.last_caller = time.monotonic()  # wait a full interval (plus the agent's question) again
            silence.awaiting_caller = True
            log.info("call %s: caller silent, still-there prompt %s", ctx.call_id, silence.prompts)
            await link.note(still_there_note(ctx.language))
            continue
        log.info("call %s: no response after %s prompts, ending call", ctx.call_id, silence.prompts)
        await link.note(no_response_note(ctx.language))
        await asyncio.sleep(GOODBYE_GRACE_SECONDS)
        if not ctx.ended:
            ctx.ended, ctx.end_reason = True, "no_response"
            return
        # end_call arrived: _hang_up_after_goodbye finishes the call once the goodbye has played


RETRYABLE_CLOSE_CODES = {1000, 1001, 1006, 1011, 1012, 1013, 1014}


def _retryable(exc: BaseException) -> bool:
    """Connection drops are worth a reconnect; bad config / auth / quota errors are not.

    The SDK turns a closed Live socket into errors.APIError carrying the WebSocket close code (1000, 1006,
    1011...). 1007/1008 and 4xx mean an invalid request, key or model, so retrying would just fail again.
    """
    from google.genai import errors
    from websockets.exceptions import ConnectionClosed

    if isinstance(exc, errors.APIError):
        return exc.code in RETRYABLE_CLOSE_CODES or 500 <= (exc.code or 0) < 600
    if isinstance(exc, ConnectionClosed):
        code = exc.rcvd.code if exc.rcvd else 1006
        return code in RETRYABLE_CLOSE_CODES
    return isinstance(exc, (ConnectionError, TimeoutError))


class _CallSideError(Exception):
    """An error from the caller's side (line / watchdog): ends the call, never triggers a reconnect."""

    def __init__(self, inner: BaseException):
        super().__init__(str(inner))
        self.inner = inner


# ---------- the agent ----------

@dataclass
class AgentResult:
    reason: str = ""
    error: str | None = None
    reconnects: int = 0
    greeted: bool = False  # the Live session opened and the greeting was sent


async def run_agent(line: AudioTransport, *, call_id: int, snap: TikiriiSnapshot, language: str,
                    transcript: Transcript, channel: str, caller_number: str = "", max_seconds: float | None = None,
                    notice: bool = True) -> AgentResult:
    """Talk to the caller in `language` until the call ends. The line must already be listening. `notice`: say the
    call is recorded in the greeting (False when the menu recording already said so)."""
    s = get_settings()
    max_seconds = max_seconds or s.max_call_seconds
    lang = language if language in languages.codes() else snap.profile.default_language
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    ctx = CallContext(call_id=call_id, channel=channel, caller_number=caller_number, snap=snap, language=lang,
                      deadline=deadline)
    instruction = build_system_instruction(snap, now_utc(), language=lang, channel=channel,
                                           caller_number=caller_number)
    silence = Silence()
    link = LiveLink()
    denoiser = NoiseSuppressor(s.noise_suppression, rate=INPUT_RATE) if s.noise_suppression != "off" else None
    # The gate also tells the silence watchdog when the caller is talking, so with VOICE_GATE=off it still runs, in
    # "log" mode: it measures and passes the audio through unchanged.
    gate = VoiceGate("log" if s.voice_gate == "off" else s.voice_gate, rate=INPUT_RATE)
    result = AgentResult()

    await line.event({"type": "agent", "language": lang})
    log.info("call %s: agent starts in %s, model %s, voice %s, temperature %s", call_id,
             languages.get(lang).name, s.gemini_live_model, call_voice(snap.profile.agent_voice),
             min(snap.profile.temperature, s.live_max_temperature))

    upstream = asyncio.create_task(_pump_caller_audio(line, link, ctx, silence, denoiser, gate))
    watchdog = asyncio.create_task(_silence_watchdog(link, ctx, silence, deadline))
    hang_up = asyncio.create_task(_hang_up_after_goodbye(ctx, silence))
    background = {upstream, watchdog, hang_up}
    try:
        client = get_client()
        while True:
            fresh = link.handle is None
            try:
                # A half-said line is still pending: save it, then give the new session the whole call so far.
                await transcript.flush_all()
                recap = call_recap(transcript.turns) if result.greeted else ""
                async with client.aio.live.connect(model=s.gemini_live_model,
                                                   config=live_config(instruction + recap, snap, link.handle,
                                                                      lang)) as session:
                    await link.attach(session)
                    link.last_event = "fresh session" if fresh else "resumed session"
                    if link.reconnects:
                        log.info("call %s: Live reconnected (#%d, resumed=%s, voice %s)", call_id, link.reconnects,
                                 not fresh, call_voice(snap.profile.agent_voice))
                    if not result.greeted:
                        await send_note(session, greeting_note(snap, lang, notice=notice))
                        result.greeted = True
                    elif link.redo is not None:
                        # The last reply was cut off for its voice: say it again (the greeting, if that was it).
                        spoke = any(t["role"] == "agent" for t in transcript.turns)
                        await send_note(session, redo_note(link.redo) if spoke else
                                        greeting_note(snap, lang, notice=notice))
                        link.redo = None
                    elif fresh:
                        await send_note(session, resume_note())
                    downstream = asyncio.create_task(_pump_model(session, line, link, ctx, transcript, silence))
                    renew = asyncio.create_task(link.reconnect_needed.wait())
                    done, _ = await asyncio.wait(
                        {downstream, renew, *background}, timeout=max(0.0, deadline - loop.time()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    link.detach()
                    for t in (downstream, renew):
                        if t not in done:
                            t.cancel()
                    await asyncio.gather(downstream, renew, return_exceptions=True)

                    if done & background:
                        for t in done & background:
                            if (exc := t.exception()) is not None:
                                raise _CallSideError(exc)
                        if upstream in done and not ctx.ended:
                            result.reason = "caller_hung_up"
                        break
                    if not done:
                        log.warning("call %s hit the %ss limit", call_id, max_seconds)
                        result.reason = "time_limit"
                        break
                    if renew in done:
                        link.reconnects += 1
                        log.info("call %s: renewing the Live connection (resume=%s)", call_id, bool(link.handle))
                        continue
                    exc = downstream.exception() if downstream in done and not downstream.cancelled() else None
                    if exc is not None:
                        raise exc
                    raise ConnectionError("Live session closed")
            except _CallSideError:
                raise
            except Exception as exc:  # noqa: BLE001
                if result.greeted and not ctx.ended and _retryable(exc) and link.reconnects < MAX_RECONNECTS:
                    link.reconnects += 1
                    log.warning("call %s: Live connection dropped (%s), reconnecting (resume=%s)", call_id, exc,
                                bool(link.handle))
                    await asyncio.sleep(0.5 * link.reconnects)
                    continue
                raise
    except Exception as exc:  # noqa: BLE001 - any failure must still end the agent cleanly
        if isinstance(exc, _CallSideError):
            exc = exc.inner
        log.exception("call %s: agent failed", call_id, exc_info=exc)
        result.error = str(exc) or exc.__class__.__name__
        result.reason = "error"
        await line.event({"type": "error", "message": result.error})
    finally:
        for t in background:
            t.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        link.detach()

    await transcript.flush_all()
    result.reason = ctx.end_reason or result.reason or "completed"
    result.reconnects = link.reconnects
    if denoiser:
        log.info("call %s noise suppression %s", call_id, denoiser.summary())
    if s.voice_gate != "off":
        log.info("call %s voice gate %s", call_id, gate.summary())
    return result
