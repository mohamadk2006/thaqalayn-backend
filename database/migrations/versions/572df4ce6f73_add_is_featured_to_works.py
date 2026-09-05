"""add is_featured to works

A curated "الكتب المختارة" set: a small number of hand-picked works the admin panel
flags, shown alongside their real category rather than replacing it. Deliberately a
boolean on `works`, not a second subject_id -- a work already has exactly one category,
and this needs to add to that, not compete with it.

Revision ID: 572df4ce6f73
Revises: 94949a88cd4b
Create Date: 2026-09-05 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '572df4ce6f73'
down_revision: str | Sequence[str] | None = '94949a88cd4b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "works",
        sa.Column(
            "is_featured", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_works_featured", "works", ["is_featured"],
        postgresql_where=sa.text("is_featured"),
    )


def downgrade() -> None:
    op.drop_index("ix_works_featured", table_name="works")
    op.drop_column("works", "is_featured")
