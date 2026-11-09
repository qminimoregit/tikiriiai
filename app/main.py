"""tikiriiai: the TIKIRII product line. A Gemini Live phone agent that explains TIKIRII and books demos (language menu, then one language) + admin panel."""

import logging
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, update

from . import auth, languages, sms, whatsapp
from .audio_import import ffmpeg
from .clock import now_utc
from .config import check_settings, get_settings
from .db import ROOT, SessionLocal, init_db
from .models import Call
from .routers import calls, catalog, dashboard, leads, messages, pbx, profile, whatsapp as whatsapp_hook, ws
from .seed import seed_if_empty
from .snapshot import get_profile
from .telephony import ami, asterisk
from .telephony.menu_audio import sync_asterisk_sound
from .telephony.recording import recording_files

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines carry full request URLs
log = logging.getLogger("tikiriiai")


def _startup_db() -> None:
    init_db()
    with SessionLocal() as db:
        seed_if_empty(db)
        # Calls left "in progress" by a crash or restart can never finish now. Keep what they saved while they ran
        # (transcript, language, duration) and link the recording that is on disk.
        for call in db.scalars(select(Call).where(Call.status == "in_progress")):
            call.status, call.end_reason = "interrupted", "server_restart"
            call.ended_at = (call.started_at + timedelta(seconds=call.duration_seconds)
                             if call.duration_seconds is not None else now_utc())
            files = recording_files(call.id)
            call.recording_path = files[0]["name"] if files else ""
        # A transcript check lost in a restart will never finish.
        db.execute(update(Call).where(Call.transcript_check == "checking").values(transcript_check="failed"))
        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    check_settings(s)
    langs = languages.languages()  # a broken language file stops the app here, naming the file and the key
    log.info("languages: %s", ", ".join(l.name for l in langs.values()))
    if not s.session_secret:
        log.warning("SESSION_SECRET is not set: logins won't survive a restart")
    if not s.gemini_api_key:
        log.warning("GEMINI_API_KEY is not set: the admin panel works, but calls reach no agent")
    if problem := sms.setup_problem():
        log.warning("%s: details messages by SMS are saved as 'not sent'", problem)
    if problem := whatsapp.setup_problem():
        log.warning("%s: WhatsApp messages are saved as 'not sent'", problem)
    if not ffmpeg():
        log.warning("ffmpeg not found: only WAV recordings can be uploaded, and call recordings stay WAV")
    s.media_path.mkdir(parents=True, exist_ok=True)
    _startup_db()
    with SessionLocal() as db:
        sync_asterisk_sound(get_profile(db).menu_audio)  # keep the PBX's copy of the menu in step with the upload
    if s.pbx_enabled:
        await asterisk.start_server()
        ami.start()
    yield
    ami.stop()
    asterisk.stop_server()


app = FastAPI(title="tikiriiai", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(auth.AuthMiddleware)

app.include_router(auth.router)
for r in (profile.router, catalog.router, leads.router, calls.router, messages.router, dashboard.router, pbx.router,
          whatsapp_hook.router, ws.router):
    app.include_router(r)


@app.middleware("http")
async def embed_policy(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return response


@app.get("/api/health")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
