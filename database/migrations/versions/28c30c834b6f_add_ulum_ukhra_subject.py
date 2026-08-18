"""Add the علوم أخرى subject.

Missed in the original seed. The automatic collection mapping surfaced ~25 books under
collections like علم النجوم and تربية that belong to no other subject, and subject_id is
a foreign key — without this row the import would fail on them.

Original header: add ulum-ukhra subject

Revision ID: 28c30c834b6f
Revises: 4339354bfb0d
Create Date: 2026-08-18 14:22:33.273829

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '28c30c834b6f'
down_revision: str | Sequence[str] | None = '4339354bfb0d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None



def upgrade() -> None:
    op.execute(
        "INSERT INTO subjects (id, title, sort_order) "
        "VALUES ('ulum-ukhra', 'علوم أخرى', 19) ON CONFLICT (id) DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM subjects WHERE id = 'ulum-ukhra'")
