"""add chat_requests.attempts -- every Gemini model tried for a question

The chat now falls back to other models when the first is busy (503), over its free-tier
quota (429) or too slow. `attempts` records each try ({"model", "status", "ms"}) so the
analysis can see how often each model was available; `model` stays the one that answered.

Revision ID: 8d4f1b2c6e07
Revises: 7c2e9a4f1d60
Create Date: 2026-10-21

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8d4f1b2c6e07"
down_revision: str | Sequence[str] | None = "7c2e9a4f1d60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_requests", sa.Column("attempts", postgresql.JSONB()))


def downgrade() -> None:
    op.drop_column("chat_requests", "attempts")
