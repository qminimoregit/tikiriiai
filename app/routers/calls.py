from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import Call, Lead, SmsMessage
from ..schemas import CallDetail, CallOut, LeadOut, SmsOut

router = APIRouter(prefix="/api/calls", tags=["calls"])

PAGE_SIZE = 50
AUDIO_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav"}


@router.get("")
def list_calls(channel: str | None = None, page: int = 1, db: Session = Depends(get_db)):
    q = select(Call)
    if channel:
        q = q.where(Call.channel == channel)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Call.started_at.desc()).offset((max(page, 1) - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    sent = dict(db.execute(
        select(SmsMessage.call_id, func.count()).where(SmsMessage.call_id.in_([r.id for r in rows]))
        .group_by(SmsMessage.call_id)).all()) if rows else {}
    items = []
    for r in rows:
        item = CallOut.model_validate(r).model_dump(mode="json")
        item["messages_sent"] = sent.get(r.id, 0)
        item["turns"] = sum(1 for t in (r.transcript or []) if t.get("role") in ("caller", "agent"))
        items.append(item)
    return {"items": items, "total": total, "page": page, "page_size": PAGE_SIZE}


@router.get("/{cid}", response_model=CallDetail)
def call_detail(cid: int, db: Session = Depends(get_db)):
    call = db.get(Call, cid)
    if call is None:
        raise HTTPException(404, "Call not found")
    out = CallDetail.model_validate(call)
    out.messages = [SmsOut.model_validate(m) for m in db.scalars(
        select(SmsMessage).where(SmsMessage.call_id == cid).order_by(SmsMessage.id))]
    out.leads = [LeadOut.model_validate(x) for x in db.scalars(select(Lead).where(Lead.call_id == cid).order_by(Lead.id))]
    return out


def _recording_file(call: Call):
    """The call's recording file, or None. Never a path outside the recordings folder."""
    if not call.recording_path:
        return None
    root = get_settings().recordings_path.resolve()
    path = (root / call.recording_path).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


@router.get("/{cid}/recording")
def call_recording(cid: int, db: Session = Depends(get_db)):
    call = db.get(Call, cid)
    path = _recording_file(call) if call else None
    if path is None:
        raise HTTPException(404, "No recording for this call")
    return FileResponse(path, media_type=AUDIO_TYPES.get(path.suffix, "application/octet-stream"),
                        filename=f"call-{cid}{path.suffix}", content_disposition_type="inline")


@router.delete("/{cid}")
def delete_call(cid: int, db: Session = Depends(get_db)):
    call = db.get(Call, cid)
    if call is None:
        raise HTTPException(404, "Call not found")
    for m in db.scalars(select(SmsMessage).where(SmsMessage.call_id == cid)):
        m.call_id = None  # keep the sent-message record, drop the link
    for lead in db.scalars(select(Lead).where(Lead.call_id == cid)):
        lead.call_id = None  # a lead outlives the call it came from
    path = _recording_file(call)
    db.delete(call)
    db.commit()
    if path is not None:
        path.unlink(missing_ok=True)
    return {"ok": True}
