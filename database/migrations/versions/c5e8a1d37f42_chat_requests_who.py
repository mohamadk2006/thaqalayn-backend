"""add chat_requests.who for the chat limits

Revision ID: c5e8a1d37f42
Revises: b7d1f3a9c205
Create Date: 2026-10-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5e8a1d37f42"
down_revision: str | Sequence[str] | None = "b7d1f3a9c205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_requests", sa.Column("who", sa.Text()))
    op.execute("UPDATE chat_requests SET who = 'c:' || client_id WHERE client_id IS NOT NULL")
    op.create_index("ix_chat_requests_who", "chat_requests", ["who", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_requests_who", table_name="chat_requests")
    op.drop_column("chat_requests", "who")
