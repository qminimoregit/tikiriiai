"""First-start data: what TIKIRII is and does (app/seed_data/tikirii.yaml, taken from the marketing site), so a test
call works straight away. Everything is editable in the admin panel. Runs only on an empty database."""

from __future__ import annotations

from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AgentProfile, Capability, FaqEntry, Industry, Integration

SEED_FILE = Path(__file__).resolve().parent / "seed_data" / "tikirii.yaml"


def seed_from(db: Session, data: dict) -> None:
    db.add(AgentProfile(id=1, **data["profile"]))
    for key, model in (("capabilities", Capability), ("industries", Industry), ("integrations", Integration),
                       ("faqs", FaqEntry)):
        for i, row in enumerate(data.get(key, [])):
            db.add(model(sort_order=i, **row))
    db.commit()


def seed_if_empty(db: Session) -> None:
    if db.scalar(select(AgentProfile.id).limit(1)) is not None:
        return
    seed_from(db, yaml.safe_load(SEED_FILE.read_text(encoding="utf-8")))
