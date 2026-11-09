"""price list

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-04 12:50:00.000000
"""
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
import yaml
from alembic import op


revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEED = Path(__file__).resolve().parents[2] / "app" / "seed_data" / "tikirii.yaml"
FAQ_QUESTION = "How much does it cost?"

OLD_NOTE = (
    "There is no fixed price list. The setup is made to fit each business: the price depends on the expected call\n"
    "volume, the languages needed and the workflow (for example bookings, orders, reminders or integrations). The way\n"
    "to get a quote is to request a demo; the team then discusses a suitable plan and quote.\n"
)
OLD_FAQ = ("There is no fixed price list. The price depends on the expected call volume, the languages and the "
           "workflow. The way to get a quote is to request a demo.")


def upgrade() -> None:
    data = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    note = data["profile"]["pricing_note"]
    answer = next(f["answer"] for f in data["faqs"] if f["question"] == FAQ_QUESTION)
    _set(note, answer)


def downgrade() -> None:
    _set(OLD_NOTE, OLD_FAQ)


def _set(note: str, answer: str) -> None:
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE agent_profile SET pricing_note = :note WHERE id = 1"), {"note": note})
    bind.execute(sa.text("UPDATE faq_entry SET answer = :answer WHERE question = :q"),
                 {"answer": answer, "q": FAQ_QUESTION})
