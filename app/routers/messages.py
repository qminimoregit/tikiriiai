"""The messages (SMS and WhatsApp) the agent sent to callers, sent or not."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import SmsMessage
from ..schemas import SmsOut

router = APIRouter(prefix="/api/messages", tags=["messages"])

PAGE_SIZE = 50


@router.get("")
def list_messages(status: str | None = None, page: int = 1, db: Session = Depends(get_db)):
    q = select(SmsMessage)
    if status:
        q = q.where(SmsMessage.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(SmsMessage.created_at.desc(), SmsMessage.id.desc())
                      .offset((max(page, 1) - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    return {"items": [SmsOut.model_validate(r).model_dump(mode="json") for r in rows], "total": total,
            "page": page, "page_size": PAGE_SIZE}


@router.delete("/{mid}")
def delete_message(mid: int, db: Session = Depends(get_db)):
    row = db.get(SmsMessage, mid)
    if row is None:
        raise HTTPException(404, "Message not found")
    db.delete(row)
    db.commit()
    return {"ok": True}
