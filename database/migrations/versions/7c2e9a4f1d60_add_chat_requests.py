"""add chat_requests -- the chatbot prototype's log of every question

One row per question the app sends to /api/chat: the message, the answer, the status
(ok / no_results / rate_limited / error), Gemini's token counts (split out, and whole in
`usage`), the time of each step, and the passages that were sent. Kept for analysing
cost, speed and quality; nothing else reads it.

Revision ID: 7c2e9a4f1d60
Revises: 5e7a2c9d41b3
Create Date: 2026-10-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7c2e9a4f1d60"
down_revision: str | Sequence[str] | None = "5e7a2c9d41b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_requests",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("client_id", sa.Text()),
        sa.Column("app_version", sa.Text()),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("thoughts_tokens", sa.Integer()),
        sa.Column("cached_tokens", sa.Integer()),
        sa.Column("total_tokens", sa.Integer()),
        sa.Column("finish_reason", sa.Text()),
        sa.Column("usage", postgresql.JSONB()),
        sa.Column("embed_ms", sa.Integer()),
        sa.Column("search_ms", sa.Integer()),
        sa.Column("llm_ms", sa.Integer()),
        sa.Column("total_ms", sa.Integer()),
        sa.Column("sources", postgresql.JSONB()),
        sa.CheckConstraint(
            "status IN ('ok','no_results','rate_limited','error')", name="ck_chat_requests_status"
        ),
    )
    op.create_index("ix_chat_requests_created", "chat_requests", ["created_at"])
    op.create_index("ix_chat_requests_client", "chat_requests", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_requests_client", table_name="chat_requests")
    op.drop_index("ix_chat_requests_created", table_name="chat_requests")
    op.drop_table("chat_requests")
