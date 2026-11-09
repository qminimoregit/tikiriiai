"""The company profile and the agent's settings: what TIKIRII is, voice, languages, greetings, the language-menu recording,
the "unavailable" apology recordings and a preview of exactly what the agent knows."""

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import audio_import, languages, notify
from ..ai.prompt_builder import build_system_instruction, greeting_note
from ..ai.tools import DECLARATIONS
from ..ai.voices import VOICES, call_voice
from ..clock import now_utc
from ..config import get_settings
from ..db import get_db
from ..schemas import LANGS, ProfileIn, ProfileOut
from ..snapshot import get_profile, load_snapshot
from ..telephony.menu_audio import delete_menu_audio, menu_path, save_menu_audio, sync_asterisk_sound, wav_to_pcm24k

router = APIRouter(prefix="/api", tags=["profile"])

TOKEN_WARNING = 30000
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@router.get("/profile", response_model=ProfileOut)
def read_profile(db: Session = Depends(get_db)):
    return get_profile(db)


@router.put("/profile", response_model=ProfileOut)
def update_profile(data: ProfileIn, db: Session = Depends(get_db)):
    profile = get_profile(db)
    for key, value in data.model_dump().items():
        setattr(profile, key, value)
    db.commit()
    db.refresh(profile)
    return profile


async def _upload_pcm(file: UploadFile) -> bytes:
    """An uploaded recording as PCM 24 kHz: a WAV is read directly; MP3, M4A and the like need ffmpeg on the server."""
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "The file is too large (10 MB at most).")
    try:
        if data[:4] == b"RIFF":
            return wav_to_pcm24k(data)
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "in", Path(tmp) / "out.wav"
            src.write_bytes(data)
            audio_import.convert(src, dst, file.filename or "")
            return wav_to_pcm24k(dst.read_bytes())
    except (ValueError, audio_import.AudioImportError) as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/profile/menu-audio", response_model=ProfileOut)
async def upload_menu_audio(file: UploadFile, db: Session = Depends(get_db)):
    """The recording played at the start of every call: "For Sinhala press 1, for Tamil press 2, for English press 3"."""
    pcm = await _upload_pcm(file)
    profile = get_profile(db)
    old = profile.menu_audio
    profile.menu_audio = save_menu_audio(pcm)
    db.commit()
    sync_asterisk_sound(profile.menu_audio)
    delete_menu_audio(old)  # only after the new one is saved
    db.refresh(profile)
    return profile


@router.get("/profile/menu-audio")
def read_menu_audio(db: Session = Depends(get_db)):
    path = menu_path(get_profile(db).menu_audio)
    if path is None:
        raise HTTPException(404, "No language menu recording")
    return FileResponse(path, media_type="audio/wav", content_disposition_type="inline")


@router.delete("/profile/menu-audio", response_model=ProfileOut)
def delete_menu(db: Session = Depends(get_db)):
    profile = get_profile(db)
    old, profile.menu_audio = profile.menu_audio, ""
    db.commit()
    sync_asterisk_sound("")
    delete_menu_audio(old)
    db.refresh(profile)
    return profile


def _lang(code: str) -> str:
    if code not in LANGS:
        raise HTTPException(404, "Unknown language")
    return code


@router.post("/profile/unavailable-audio/{lang}", response_model=ProfileOut)
async def upload_unavailable_audio(lang: str, file: UploadFile, db: Session = Depends(get_db)):
    """Played to a caller when the AI cannot start (bad key, outage), so they hear an apology instead of silence."""
    lang = _lang(lang)
    pcm = await _upload_pcm(file)
    profile = get_profile(db)
    clips = dict(profile.unavailable_audio or {})
    old = clips.get(lang, "")
    clips[lang] = save_menu_audio(pcm, prefix=f"unavailable-{lang}")
    profile.unavailable_audio = clips  # a new dict: SQLAlchemy only notices a replaced JSON value
    db.commit()
    delete_menu_audio(old)
    db.refresh(profile)
    return profile


@router.get("/profile/unavailable-audio/{lang}")
def read_unavailable_audio(lang: str, db: Session = Depends(get_db)):
    path = menu_path((get_profile(db).unavailable_audio or {}).get(_lang(lang), ""))
    if path is None:
        raise HTTPException(404, "No recording")
    return FileResponse(path, media_type="audio/wav", content_disposition_type="inline")


@router.delete("/profile/unavailable-audio/{lang}", response_model=ProfileOut)
def delete_unavailable_audio(lang: str, db: Session = Depends(get_db)):
    lang = _lang(lang)
    profile = get_profile(db)
    clips = dict(profile.unavailable_audio or {})
    delete_menu_audio(clips.pop(lang, ""))
    profile.unavailable_audio = clips
    db.commit()
    db.refresh(profile)
    return profile


@router.get("/voices")
def voices():
    return {
        "voices": [{"name": n, "gender": g, "style": st} for n, (g, st) in VOICES.items()],
        "default": call_voice(""),
        "default_greetings": {c: l.greeting for c, l in languages.languages().items()},
        "default_recording_notices": {c: l.recording_notice for c, l in languages.languages().items()},
        "language_names": {c: l.native_name for c, l in languages.languages().items()},
        "record_calls": get_settings().record_calls,
    }


@router.get("/messaging")
def messaging():
    """Why SMS or WhatsApp can't send right now ("" = it can), for the admin panel."""
    return notify.problems()


@router.get("/profile/prompt-preview")
def prompt_preview(language: str = "", channel: str = "pbx", caller_number: str = "0771234567",
                   db: Session = Depends(get_db)):
    """Exactly what a call starting now would get: same snapshot loader and builder as live calls."""
    snap = load_snapshot(db)
    lang = language if language in languages.codes() else snap.profile.default_language
    text = build_system_instruction(snap, now_utc(), language=lang, channel=channel,
                                    caller_number=caller_number if channel == "pbx" else "")
    approx_tokens = len(text) // 3  # rough: Sinhala/Tamil script costs more tokens per character than English
    return {
        "language": lang,
        "system_instruction": text,
        "greeting_note": greeting_note(snap, lang, notice=False),
        "tools": [d["name"] for d in DECLARATIONS],
        "chars": len(text),
        "approx_tokens": approx_tokens,
        "warning": "The prompt is very long; consider trimming industries, FAQs or extra info."
                   if approx_tokens > TOKEN_WARNING else "",
        "model": get_settings().gemini_live_model,
        "voice": call_voice(snap.profile.agent_voice),
    }
