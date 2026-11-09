"""After a call: hears the caller's and the agent's lines again from the call's recording.

Gemini Live writes the caller's side of the transcript with a quick live speech-to-text that often gets Sinhala and
Tamil wrong: wrong words, lost names, letters from other scripts inside words. The agent hears the audio itself and
is usually right, so mostly the saved transcript suffers. Once the call has ended and its recording is saved, a
regular Gemini model (GEMINI_TRANSCRIBE_MODEL) listens to the whole recording and rewrites each caller line.

The agent's lines are the text Gemini meant to say, not a check of what was spoken: once (call 15) the goodbye ran on
into the greeting's question while the transcript still showed the goodbye. So the agent's lines are heard again too,
but only replaced when what was said differs a lot from the saved line (spelling variants are not differences).

The live transcript stays the skeleton: turn order, times, menu steps and tool notes are kept. A line the model changed
keeps the live text as "live", so staff can compare. The model's own timestamps are seconds off, so lines are matched
by number, not by time. Nothing here runs while the caller is on the line.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from pathlib import Path

from google.genai import errors, types
from pydantic import BaseModel

from .. import languages
from ..config import get_settings
from ..db import SessionLocal
from ..models import Call
from ..telephony.recording import recording_files
from .client import get_client

log = logging.getLogger(__name__)

INLINE_LIMIT = 14 * 1024 * 1024  # Gemini takes about 20 MB per request once the audio is base64-encoded
TIMEOUT_SECONDS = 300
RETRY_WAITS = (30, 120)  # seconds to wait before the 2nd and 3rd try when Gemini answers 429 or 5xx
UNCLEAR = "[unclear]"
AGENT_SAME = 0.8  # an agent line this similar to the saved one counts as said as written

_checking: set[int] = set()  # call ids being checked now
_tasks: set[asyncio.Task] = set()  # keeps background checks from being garbage-collected


class CallerLine(BaseModel):  # a numbered line of either side
    n: int
    text: str


def enabled() -> bool:
    s = get_settings()
    return bool(s.gemini_transcribe_model.strip() and s.gemini_api_key)


def check_later(call_id: int) -> bool:
    """Check the call in the background (it has ended and its recording is saved). False if checking is off or this
    call is already being checked. Needs a running event loop."""
    if not enabled() or call_id in _checking:
        return False
    _checking.add(call_id)
    task = asyncio.get_running_loop().create_task(_run(call_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


async def _run(call_id: int) -> None:
    try:
        await check_call(call_id)
    finally:
        _checking.discard(call_id)


# ---------- what the model is asked ----------

def build_request(turns: list[dict], language: str, parts: int = 1) -> tuple[str, dict[int, int]]:
    """The instructions, with the live transcript's caller and agent lines numbered. Returns the text and
    {line number: index of that turn in `turns`}."""
    lang = languages.get(language) if language in languages.codes() else None
    name = lang.name if lang else "Sinhala, Tamil or English"
    numbered: dict[int, int] = {}
    rows = []
    for i, turn in enumerate(turns):
        t = turn.get("t")
        at = f"{t:.0f}s" if isinstance(t, (int, float)) else "?"
        if turn.get("role") in ("caller", "agent"):
            numbered[len(numbered) + 1] = i
            rows.append(f"[{len(numbered)}] {at} {turn['role'].upper()}: {turn.get('text', '')}")
        elif turn.get("role") == "menu":
            rows.append(f"    {at} MENU: {turn.get('text', '')}")
    L = [
        f"This is the recording of a phone call to TIKIRII's product line in Sri Lanka, in {name}. It may start with a "
        "recorded language menu; then TIKIRII's AI voice agent talks with the caller about the product. Sri Lankan "
        f"callers mix English words into {name} and say business types, product names (WhatsApp, Google Calendar), "
        "personal and business names and phone numbers.",
        "",
        "Below is the transcript written live during the call. The menu steps are right. The CALLER lines were "
        "written by a quick live speech-to-text and are often wrong. The AGENT lines are the text the agent meant to "
        "say, and are usually what it said, but it sometimes says something else (it can run on into another "
        "sentence, or stop short). Every CALLER and AGENT line is numbered [1], [2] and so on. Listen to the "
        "recording and write what was really said in each numbered line.",
        "",
        "Rules:",
        f"- Word for word, as spoken. Write {name} words in {name}'s own script only, never with letters of another "
        "script. Keep English words the caller used in English (Latin letters). Write phone numbers and other "
        "numbers as digits.",
        "- Do not translate, correct, tidy up or summarise what was said. For a CALLER line never copy words from "
        "the agent's lines.",
        "- Each caller line is what the caller said just before the agent's next line: use the agent's lines to find "
        "it in the recording.",
        "- For an AGENT line, write everything the agent said in that turn, including a sentence it added or the "
        "greeting it repeated, and leave out what it did not say. If it said what the line says, copy the line "
        "unchanged.",
        f"- If nobody said anything there, or you cannot make it out, write {UNCLEAR}.",
        "- Return one entry for every numbered line, with its number.",
    ]
    if lang is not None and lang.subjects:
        L.append("- Subject names callers use (English: " + name + "): "
                 + "; ".join(f"{en}: {word}" for en, word in lang.subjects.items()) + ".")
    L.append("")
    L.append("Times are seconds from the start of the call, roughly when each line ended.")
    if parts > 1:
        L.append(f"The recording comes in {parts} parts, one after the other: the caller was put through to a number "
                 "that didn't answer and came back to the agent. The times count from the start of the first part.")
    L += ["", "Transcript:", *rows]
    return "\n".join(L), numbered


async def _hear(files: list[Path], prompt: str) -> list[CallerLine]:
    """Ask the model for the numbered lines. Raises on any failure."""
    parts = []
    for f in files:
        data = await asyncio.to_thread(f.read_bytes)
        parts.append(types.Part.from_bytes(data=data, mime_type="audio/mp3" if f.suffix == ".mp3" else "audio/wav"))
    for attempt, wait in enumerate((*RETRY_WAITS, None)):
        try:
            r = await asyncio.wait_for(get_client().aio.models.generate_content(
                model=get_settings().gemini_transcribe_model,
                contents=[*parts, prompt],
                config=types.GenerateContentConfig(
                    temperature=0, response_mime_type="application/json", response_schema=list[CallerLine],
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)),
            ), TIMEOUT_SECONDS)
            break
        except errors.APIError as exc:  # the model is busy (503) or rate-limited (429): try again a little later
            if wait is None or not (exc.code == 429 or 500 <= (exc.code or 0) < 600):
                raise
            log.warning("transcript check: %s, trying again in %ss (%s of %s)", exc.code, wait, attempt + 1,
                        len(RETRY_WAITS) + 1)
            await asyncio.sleep(wait)
    if not isinstance(r.parsed, list):
        raise ValueError(f"unexpected answer: {(r.text or '')[:200]!r}")
    return r.parsed


# ---------- the check ----------

def _begin(call_id: int) -> tuple[str, list[dict]] | None:
    """Mark the call as being checked. Returns its language and transcript; None if there is nothing to check."""
    with SessionLocal() as db:
        call = db.get(Call, call_id)
        if call is None or call.status == "in_progress":
            return None
        turns = list(call.transcript or [])
        if not any(t.get("role") == "caller" for t in turns):  # nothing was said by the caller: nothing to hear
            return None
        call.transcript_check = "checking"
        db.commit()
        return call.language, turns


def _mark(call_id: int, state: str) -> None:
    with SessionLocal() as db:
        call = db.get(Call, call_id)
        if call is not None:
            call.transcript_check = state
            db.commit()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _save(call_id: int, sent: list[dict], numbered: dict[int, int], heard: list[CallerLine]) -> tuple[str, int]:
    """Put the heard lines into the transcript, unless it changed while the model listened.
    Returns (done | skipped, how many lines now differ from the live transcript)."""
    by_number = {line.n: _clean(line.text) for line in heard}
    with SessionLocal() as db:
        call = db.get(Call, call_id, with_for_update=True)
        if call is None:
            return "skipped", 0
        if call.status == "in_progress" or list(call.transcript or []) != sent:
            # The call came back from a transfer nobody answered: the check when it ends again covers everything.
            if call.status == "in_progress":
                call.transcript_check = ""
                db.commit()
            return "skipped", 0
        turns = [dict(t) for t in sent]
        for n, i in numbered.items():
            text = by_number.get(n, "")
            if not text or text == UNCLEAR:
                continue  # the model couldn't make it out: keep what was heard live
            turn = turns[i]
            live = turn.get("live", turn["text"])
            if turn.get("role") == "agent" and difflib.SequenceMatcher(None, live, text).ratio() >= AGENT_SAME:
                text = live  # said as written (only spelling differs)
            turn["text"] = text
            if text != live:
                turn["live"] = live
            else:
                turn.pop("live", None)
        call.transcript = turns
        call.transcript_check = "done"
        db.commit()
        return "done", sum(1 for t in turns if "live" in t)


async def check_call(call_id: int) -> str:
    """Hear the caller and agent lines of a finished call again from its recording and save them.
    Returns done | skipped | failed; never raises."""
    files = [get_settings().recordings_path / f["name"] for f in recording_files(call_id)]
    if not files:
        return "skipped"
    if sum(f.stat().st_size for f in files) > INLINE_LIMIT:
        log.warning("call %s: recording too big to check (with ffmpeg installed recordings are MP3s, small enough)",
                    call_id)
        await asyncio.to_thread(_mark, call_id, "failed")
        return "failed"
    begun = await asyncio.to_thread(_begin, call_id)
    if begun is None:
        return "skipped"
    language, turns = begun
    prompt, numbered = build_request(turns, language, parts=len(files))
    try:
        heard = await _hear(files, prompt)
        result, changed = await asyncio.to_thread(_save, call_id, turns, numbered, heard)
    except Exception:  # noqa: BLE001 - the live transcript stays; staff can try again from the call page
        log.exception("call %s: lines could not be checked against the recording", call_id)
        await asyncio.to_thread(_mark, call_id, "failed")
        return "failed"
    log.info("call %s: lines checked against the recording: %s (%d differ from the live transcript)",
             call_id, result, changed)
    return result
