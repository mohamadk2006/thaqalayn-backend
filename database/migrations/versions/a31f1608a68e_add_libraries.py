"""add libraries -- curated groupings orthogonal to subjects

Adds `libraries` (open-ended, admin-creatable, optionally self-nesting via parent_id)
and `library_works` (the many-to-many a work's library membership is built from) --
the same shape as subjects/work_subjects, but a deliberately separate, independent
system. A work's subject classification (its fixed genre, from the 39-row Subject
table) is untouched by library membership: joining a library never changes or
replaces a work's subjects, and vice versa.

Starts empty. The first library (e.g. "المكتبة الحسينية") and its book assignments
are created afterward via the admin panel, not seeded here -- unlike Subject's fixed
39 rows, libraries are meant to be created and grown over time, not enumerated once
and fixed.

Revision ID: a31f1608a68e
Revises: 258fbe358dd1
Create Date: 2026-09-14

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a31f1608a68e"
down_revision: str | Sequence[str] | None = "258fbe358dd1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "libraries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "parent_id", sa.Integer(),
            sa.ForeignKey("libraries.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "sort_order", sa.Integer(), nullable=False,
            server_default="0",
        ),
    )
    op.create_index("ix_libraries_parent", "libraries", ["parent_id"])

    op.create_table(
        "library_works",
        sa.Column(
            "library_id", sa.Integer(),
            sa.ForeignKey("libraries.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column(
            "work_id", sa.Integer(),
            sa.ForeignKey("works.id", ondelete="CASCADE"), primary_key=True,
        ),
    )
    op.create_index("ix_library_works_work", "library_works", ["work_id"])


def downgrade() -> None:
    op.drop_index("ix_library_works_work", table_name="library_works")
    op.drop_table("library_works")
    op.drop_index("ix_libraries_parent", table_name="libraries")
    op.drop_table("libraries")
