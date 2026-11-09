from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import notify
from ..clock import now_utc, to_local
from ..config import get_settings
from ..db import get_db
from ..models import Call, Lead, SmsMessage
from ..schemas import CallOut, LeadOut
from ..snapshot import load_snapshot

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    s = get_settings()
    snap = load_snapshot(db)
    now = now_utc()
    local = to_local(now, snap.tz)
    # Start of today in the company's timezone, as naive UTC like the DB.
    day_start_utc = now - (local.replace(tzinfo=None) - local.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None))

    def count(model, column, since):
        return db.scalar(select(func.count()).select_from(model).where(column >= since)) or 0

    recent_calls = db.scalars(select(Call).order_by(Call.started_at.desc()).limit(6)).all()
    new_leads = db.scalars(select(Lead).where(Lead.status == "new").order_by(Lead.created_at.desc()).limit(6)).all()
    return {
        "company": snap.profile.company,
        "agent": snap.profile.agent_name,
        "calls_today": count(Call, Call.started_at, day_start_utc),
        "calls_7d": count(Call, Call.started_at, now - timedelta(days=7)),
        "active_calls": db.scalar(select(func.count()).select_from(Call).where(Call.status == "in_progress")) or 0,
        "leads_7d": count(Lead, Lead.created_at, now - timedelta(days=7)),
        "leads_new": db.scalar(select(func.count()).select_from(Lead).where(Lead.status == "new")) or 0,
        "messages_sent_7d": count(SmsMessage, SmsMessage.created_at, now - timedelta(days=7)),
        "new_leads": [LeadOut.model_validate(x).model_dump(mode="json") for x in new_leads],
        "recent_calls": [CallOut.model_validate(c).model_dump(mode="json") for c in recent_calls],
        "messaging": notify.problems(),
        "lead_alerts": bool(s.lead_notify_phone.strip()),
        "pbx_enabled": s.pbx_enabled,
        "pbx_port": s.pbx_audiosocket_port,
        "model": s.gemini_live_model,
        "api_key_set": bool(s.gemini_api_key),
    }
