"""add chat_requests word search and rerank columns

The chat now also searches the rewrite's exact keywords in the library's word search, and a
cheap model reranks the merged candidates. `word_search` and `rerank` keep each step whole;
`word_ms`, `rerank_tokens` and `rerank_ms` are split out for easy sums.

Revision ID: b7d1f3a9c205
Revises: a4c8e2f61b93
Create Date: 2026-10-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d1f3a9c205"
down_revision: str | Sequence[str] | None = "a4c8e2f61b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_requests", sa.Column("word_search", postgresql.JSONB()))
    op.add_column("chat_requests", sa.Column("word_ms", sa.Integer()))
    op.add_column("chat_requests", sa.Column("rerank", postgresql.JSONB()))
    op.add_column("chat_requests", sa.Column("rerank_tokens", sa.Integer()))
    op.add_column("chat_requests", sa.Column("rerank_ms", sa.Integer()))


def downgrade() -> None:
    for column in ("rerank_ms", "rerank_tokens", "rerank", "word_ms", "word_search"):
        op.drop_column("chat_requests", column)
