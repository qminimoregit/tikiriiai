"""Demo requests the agent took on calls (book_demo), for the sales team to follow up."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Lead
from ..schemas import LeadOut, LeadUpdate

router = APIRouter(prefix="/api/leads", tags=["leads"])

PAGE_SIZE = 50


@router.get("")
def list_leads(status: str | None = None, page: int = 1, db: Session = Depends(get_db)):
    q = select(Lead)
    if status:
        q = q.where(Lead.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Lead.created_at.desc(), Lead.id.desc())
                      .offset((max(page, 1) - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    return {"items": [LeadOut.model_validate(r).model_dump(mode="json") for r in rows], "total": total,
            "page": page, "page_size": PAGE_SIZE}


@router.put("/{lid}", response_model=LeadOut)
def update_lead(lid: int, data: LeadUpdate, db: Session = Depends(get_db)):
    row = db.get(Lead, lid)
    if row is None:
        raise HTTPException(404, "Lead not found")
    row.status, row.staff_notes = data.status, data.staff_notes
    db.commit()
    return row


@router.delete("/{lid}")
def delete_lead(lid: int, db: Session = Depends(get_db)):
    row = db.get(Lead, lid)
    if row is None:
        raise HTTPException(404, "Lead not found")
    db.delete(row)
    db.commit()
    return {"ok": True}
