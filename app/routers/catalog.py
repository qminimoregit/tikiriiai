"""Capabilities, industries, integrations and FAQ entries: what the agent tells callers. Same CRUD + reorder shape."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Capability, FaqEntry, Industry, Integration
from ..schemas import (CapabilityIn, CapabilityOut, FaqIn, FaqOut, IndustryIn, IndustryOut, IntegrationIn,
                       IntegrationOut, ReorderIn)

router = APIRouter(prefix="/api", tags=["catalog"])


def _crud(path: str, model, schema_in, schema_out, label: str) -> None:
    order = (model.sort_order, model.id)

    @router.get(f"/{path}", response_model=list[schema_out], name=f"list_{path}")
    def list_items(db: Session = Depends(get_db)):
        return db.scalars(select(model).order_by(*order)).all()

    @router.post(f"/{path}", response_model=schema_out, status_code=201, name=f"create_{path}")
    def create_item(data: schema_in, db: Session = Depends(get_db)):
        last = db.scalar(select(func.max(model.sort_order)))
        row = model(**data.model_dump(), sort_order=(last if last is not None else -1) + 1)
        db.add(row)
        db.commit()
        return row

    @router.put(f"/{path}/{{item_id}}", response_model=schema_out, name=f"update_{path}")
    def update_item(item_id: int, data: schema_in, db: Session = Depends(get_db)):
        row = db.get(model, item_id)
        if row is None:
            raise HTTPException(404, f"{label} not found")
        for k, v in data.model_dump().items():
            setattr(row, k, v)
        db.commit()
        return row

    @router.delete(f"/{path}/{{item_id}}", name=f"delete_{path}")
    def delete_item(item_id: int, db: Session = Depends(get_db)):
        row = db.get(model, item_id)
        if row is None:
            raise HTTPException(404, f"{label} not found")
        db.delete(row)
        db.commit()
        return {"ok": True}

    @router.post(f"/{path}/reorder", name=f"reorder_{path}")
    def reorder(data: ReorderIn, db: Session = Depends(get_db)):
        rows = {r.id: r for r in db.scalars(select(model))}
        if set(data.ids) != set(rows):
            raise HTTPException(400, "The list changed; reload the page and try again")
        for i, item_id in enumerate(data.ids):
            rows[item_id].sort_order = i
        db.commit()
        return {"ok": True}


_crud("capabilities", Capability, CapabilityIn, CapabilityOut, "Capability")
_crud("industries", Industry, IndustryIn, IndustryOut, "Industry")
_crud("integrations", Integration, IntegrationIn, IntegrationOut, "Integration")
_crud("faqs", FaqEntry, FaqIn, FaqOut, "FAQ entry")
