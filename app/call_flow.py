"""One call from start to finish: Call row → language menu → agent → finalised Call row.

Phone calls (Asterisk AudioSocket) and the admin Test Call page both come through handle_call(), so a test call in
the browser goes through exactly the same menu and agent as a real caller.

The language menu is one recording the admin uploads ("For Sinhala press 1, for Tamil press 2, for English press
3"). The key picks the language the agent greets in and speaks, and only that language, for the whole call. On a PBX
that cannot pass key presses to AudioSocket the dialplan plays the menu itself with Read() and passes the language in
the call-start hook (pbx_menu); the app then plays nothing.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import languages
from .ai import transcript_check
from .ai.live_session import AgentResult, run_agent
from .clock import now_utc
from .config import get_settings
from .db import SessionLocal
from .models import Call
from .snapshot import TikiriiSnapshot, load_snapshot
from .telephony.base import OUTPUT_RATE, AudioTransport
from .telephony.line import CallLine, Hangup
from .telephony.menu_audio import load_menu_pcm
from .telephony.recording import CallRecorder
from .transcript import Transcript

log = logging.getLogger(__name__)

MENU_WAIT_SECONDS = 5.0  # after the language menu recording ends, how long to wait for a key
MENU_PLAYS = 2  # the menu is played this many times before the main language is used
MENU_CHUNK_BYTES = 4800  # 100 ms at 24 kHz
CHECKPOINT_SECONDS = 2.0  # the call row is saved at most this often while the call goes on


def _recorder(call_id: int) -> CallRecorder | None:
    """A recorder for this call, or None if recording is off."""
    s = get_settings()
    if not s.record_calls:
        return None
    return CallRecorder(s.recordings_path / f"{call_id}")


async def _save_recording(recorder: CallRecorder | None, call_id: int) -> None:
    """Close the recording, remember where it is, then have the caller's lines heard again from it (in the background)."""
    if recorder is None:
        return
    saved = await asyncio.to_thread(recorder.finish)
    log.info("call %s recording: %s", call_id, saved.name if saved else "nothing recorded")
    if saved is None:
        return
    with SessionLocal() as db:
        call = db.get(Call, call_id)
        if call is not None:
            call.recording_path = str(saved.relative_to(get_settings().recordings_path))[:300]
            db.commit()
    transcript_check.check_later(call_id)


def _start_call(channel: str, caller_number: str) -> tuple[int, TikiriiSnapshot, bytes]:
    with SessionLocal() as db:
        snap = load_snapshot(db)
        call = Call(channel=channel, caller_number=caller_number[:60], status="in_progress", started_at=now_utc())
        db.add(call)
        db.commit()
        if get_settings().record_calls:  # the recording grows on disk as the call goes: a crash still leaves it linked
            call.recording_path = f"{call.id}.wav"
            db.commit()
        return call.id, snap, load_menu_pcm(snap.profile.menu_audio)


def _checkpoint(call_id: int, language: str, turns: list[dict]) -> None:
    """Save the conversation so far on a call still in progress, so a crash or restart loses at most a moment of it."""
    with SessionLocal() as db:
        call = db.get(Call, call_id)
        if call is None or call.status != "in_progress":
            return
        call.transcript = list(turns)  # a new list, so the JSON column is seen as changed
        call.language = language
        call.duration_seconds = int((now_utc() - call.started_at).total_seconds())
        db.commit()


async def _checkpointer(call_id: int, language: str, transcript: Transcript) -> None:
    while True:
        await transcript.changed.wait()
        transcript.changed.clear()
        try:
            await asyncio.to_thread(_checkpoint, call_id, language, transcript.turns)
        except Exception:  # noqa: BLE001 - saving early is a safety net, never a reason to end the call
            log.exception("call %s: could not save the call so far", call_id)
        await asyncio.sleep(CHECKPOINT_SECONDS)


def _finalize(call_id: int, language: str, menu_seconds: int | None, agent: AgentResult | None, turns: list[dict],
              reason: str, error: str | None, status: str | None = None) -> str:
    with SessionLocal() as db:
        call = db.get(Call, call_id)
        if call is None:
            return "failed"
        now = now_utc()
        call.transcript = turns
        call.ended_at = now
        call.duration_seconds = int((now - call.started_at).total_seconds())
        call.menu_seconds = menu_seconds
        call.language = language
        call.status = status or ("failed" if error else "completed")
        call.end_reason = reason
        call.reconnects = agent.reconnects if agent else 0
        call.error = (error or "")[:2000]
        db.commit()
        return call.status


async def _play(line: CallLine, pcm: bytes) -> None:
    """Queue a recording for the caller. close() plays out what is still queued before it hangs up."""
    for i in range(0, len(pcm), MENU_CHUNK_BYTES):
        if line.hung_up.is_set():
            return
        await line.send(pcm[i:i + MENU_CHUNK_BYTES])


async def _language_menu(line: CallLine, pcm: bytes, snap: TikiriiSnapshot) -> tuple[str, str, bool]:
    """Plays the uploaded menu clip and waits for a key. Returns (language, key pressed, caller still there).

    A key of an enabled language stops the clip and picks that language. A wrong key or no key plays the clip again;
    after MENU_PLAYS plays the language is "" (the main language is used).
    """
    keys = {d: l for l, d in snap.profile.menu_keys.items() if l in snap.profile.languages_enabled}
    clip_seconds = len(pcm) / 2 / OUTPUT_RATE
    for _ in range(MENU_PLAYS):  # keys pressed before the clip started (typed ahead) still count
        await _play(line, pcm)
        deadline = time.monotonic() + clip_seconds + MENU_WAIT_SECONDS
        while (left := deadline - time.monotonic()) > 0:
            try:
                key = await line.next_digit(left)
            except Hangup:
                return "", "", False
            if key is None:
                break
            if keys.get(key):
                await line.clear()
                return keys[key], key, True
        await line.clear()
    return "", "", True


async def handle_call(transport: AudioTransport, *, channel: str, caller_number: str = "",
                      max_seconds: int | None = None, menu_lang: str = "", pbx_menu: bool = False) -> int:
    """Run one call to the end. Returns the Call id.

    pbx_menu: the PBX dialplan already played the language menu and `menu_lang` is the language picked ("" = none),
    so the app plays no menu of its own."""
    s = get_settings()
    max_seconds = max_seconds or s.max_call_seconds
    call_id, snap, menu_pcm = await asyncio.to_thread(_start_call, channel, caller_number)
    started = time.monotonic()
    recorder = _recorder(call_id)
    line = CallLine(transport, recorder)
    transcript = Transcript(line, started)
    agent: AgentResult | None = None
    lang = snap.profile.default_language
    menu_seconds: int | None = None
    reason, error = "", None
    checkpointer: asyncio.Task | None = None
    stopped = False  # the server is stopping: this task was cancelled mid-call
    menu_played = bool(pbx_menu or menu_pcm)  # a recording said the call is recorded, so the agent leaves it out

    await line.event({"type": "started", "call_id": call_id, "agent": snap.profile.agent_name})
    log.info("call %s (%s from %r) started", call_id, channel, caller_number)
    try:
        present = True
        picked = ""
        if pbx_menu:
            picked = menu_lang if menu_lang in snap.profile.languages_enabled else ""
        elif menu_pcm:
            picked, key, present = await _language_menu(line, menu_pcm, snap)
            if present:
                await transcript.note("menu", f"Language menu: pressed {key} -> {languages.get(picked).name}"
                                      if picked else "Language menu: no key pressed -> main language")
        if menu_played:
            menu_seconds = round(time.monotonic() - started)
        lang = picked or snap.profile.default_language
        checkpointer = asyncio.create_task(_checkpointer(call_id, lang, transcript))
        transcript.changed.set()  # saves the menu step (and the language) at once
        if not present or line.hung_up.is_set():
            reason = "hung_up_in_menu"
        else:
            line.clear_digits()
            line.start_listening()
            agent = await run_agent(line, call_id=call_id, snap=snap, language=lang, transcript=transcript,
                                    channel=channel, caller_number=caller_number, notice=False,
                                    max_seconds=max_seconds - (time.monotonic() - started))
            reason, error = agent.reason, agent.error
            if getattr(transport, "menu_requested", False):
                reason, error = "back_to_menu", None  # the caller pressed # and is back at the language menu
            if not agent.greeted and error and not line.hung_up.is_set():
                # Gemini never answered (bad key, quota, outage): tell the caller rather than leave them in silence.
                await _play(line, load_menu_pcm(snap.profile.unavailable_audio.get(lang, "")))
    except asyncio.CancelledError:  # the service is stopping or restarting: keep what the call had so far
        stopped, reason = True, "server_stopped"
        log.warning("call %s cancelled (server stopping), saving it as interrupted", call_id)
    except Exception as exc:  # noqa: BLE001 - any failure must still finalise the call row
        log.exception("call %s failed", call_id)
        error = str(exc) or exc.__class__.__name__
        reason = "error"
        await line.event({"type": "error", "message": error})

    if checkpointer is not None:
        checkpointer.cancel()
        await asyncio.gather(checkpointer, return_exceptions=True)
    await transcript.flush_all()
    status = await asyncio.to_thread(_finalize, call_id, lang, menu_seconds, agent, transcript.turns, reason, error,
                                     "interrupted" if stopped else None)
    log.info("call %s ended: %s (%s)", call_id, status, reason)
    if stopped:  # no goodbye to play out: close the recording and leave
        await _save_recording(recorder, call_id)
        raise asyncio.CancelledError
    await line.event({"type": "ended", "status": status, "reason": reason, "call_id": call_id})
    await line.close()  # plays out the goodbye first, so the recording has it
    await _save_recording(recorder, call_id)
    return call_id
