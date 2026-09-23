"""add the "القرآن الكريم وعلومه عند السنة" subject

"القرآن الكريم وعلومه" is being split by school: it stays for the Shia works and this new
subject holds the Sunni ones (the same pairing as مصادر التفسير عند السنة/الشيعة). Placed
at position 8, right after "مصطلحات ومفردات فقهية", where the app's "الكتب الأخرى"
section begins. Every subject from position 8 down moves one place; only the order
changes -- ids, titles and work assignments are untouched. The new subject starts empty;
works are moved into it separately.

Revision ID: c93a5d17e8b4
Revises: b7c41e90a3d2
Create Date: 2026-09-23 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c93a5d17e8b4'
down_revision: str | Sequence[str] | None = 'b7c41e90a3d2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_ID = "القرآن الكريم وعلومه عند السنة"
POSITION = 8


def upgrade() -> None:
    # Guarded so a re-run can never shift the order twice.
    op.execute(f"""
        UPDATE subjects SET sort_order = sort_order + 1
        WHERE sort_order >= {POSITION}
          AND NOT EXISTS (SELECT 1 FROM subjects WHERE id = '{NEW_ID}')
    """)
    op.execute(f"""
        INSERT INTO subjects (id, title, sort_order)
        VALUES ('{NEW_ID}', '{NEW_ID}', {POSITION}) ON CONFLICT (id) DO NOTHING
    """)


def downgrade() -> None:
    # work_subjects rows for this subject cascade away with it.
    op.execute(f"DELETE FROM subjects WHERE id = '{NEW_ID}'")
    op.execute(f"UPDATE subjects SET sort_order = sort_order - 1 WHERE sort_order > {POSITION}")
