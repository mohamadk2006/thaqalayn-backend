"""add chat_requests.rewrite -- the cheap model's search texts, tokens and time

The chat now has a cheap model rewrite the message into search texts before the vector
search. `rewrite` keeps that step whole (model, status, ms, the texts, usage);
`rewrite_tokens` and `rewrite_ms` are split out for easy sums.

Revision ID: a4c8e2f61b93
Revises: 9b3e5d7a2c14
Create Date: 2026-10-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a4c8e2f61b93"
down_revision: str | Sequence[str] | None = "9b3e5d7a2c14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_requests", sa.Column("rewrite", postgresql.JSONB()))
    op.add_column("chat_requests", sa.Column("rewrite_tokens", sa.Integer()))
    op.add_column("chat_requests", sa.Column("rewrite_ms", sa.Integer()))


def downgrade() -> None:
    op.drop_column("chat_requests", "rewrite_ms")
    op.drop_column("chat_requests", "rewrite_tokens")
    op.drop_column("chat_requests", "rewrite")
