"""remove the Starter package

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-05 10:00:00.000000
"""
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
import yaml
from alembic import op


revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEED = Path(__file__).resolve().parents[2] / "app" / "seed_data" / "tikirii.yaml"
FAQ_QUESTION = "How much does it cost?"


def upgrade() -> None:
    data = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    note = data["profile"]["pricing_note"]
    answer = next(f["answer"] for f in data["faqs"] if f["question"] == FAQ_QUESTION)
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE agent_profile SET pricing_note = :note WHERE id = 1"), {"note": note})
    bind.execute(sa.text("UPDATE faq_entry SET answer = :answer WHERE question = :q"),
                 {"answer": answer, "q": FAQ_QUESTION})


def downgrade() -> None:
    pass  # the Starter text is no longer in the code
