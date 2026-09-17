"""add featured_sort_order to works

The admin's "الكتب المختارة" list was always shown alphabetically -- there was no way
to control the order those works appear in on the app's featured shelf. Adds an
explicit integer rank, backfilled from the current alphabetical order so nothing jumps
around on deploy, and only meaningful while is_featured is true.

Revision ID: d0f9852c9c75
Revises: a31f1608a68e
Create Date: 2026-09-17 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd0f9852c9c75'
down_revision: str | Sequence[str] | None = 'a31f1608a68e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "works",
        sa.Column(
            "featured_sort_order", sa.Integer(), nullable=False,
            server_default="0",
        ),
    )
    op.execute("""
        UPDATE works SET featured_sort_order = ranked.rn
        FROM (
            SELECT id, row_number() OVER (ORDER BY title_norm) AS rn
            FROM works WHERE is_featured
        ) AS ranked
        WHERE works.id = ranked.id
    """)


def downgrade() -> None:
    op.drop_column("works", "featured_sort_order")
