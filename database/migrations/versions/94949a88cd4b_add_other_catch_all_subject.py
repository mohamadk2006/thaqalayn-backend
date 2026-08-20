"""add other catch-all subject

Not one of Shamela's 39 -- a 40th slug this project adds so every book gets a real,
non-NULL classification. Covers the ~11.8% of the library whose raw `< مجموعة >`
string is genuinely tradition-ambiguous (a bare "مصادر الحديث"/"مصادر التفسير"/
"مصادر العقائد" with no سنة/شيعة marker, when every one of the 39 hadith/tafsir/aqaid
slots requires one) -- per an explicit decision to prefer a labeled catch-all over
leaving subject_id NULL.

Revision ID: 94949a88cd4b
Revises: 42bb31eb450d
Create Date: 2026-08-20 13:16:53.034010

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '94949a88cd4b'
down_revision: str | Sequence[str] | None = '42bb31eb450d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO subjects (id, title, sort_order) "
        "VALUES ('other', 'أخرى', 39) ON CONFLICT (id) DO NOTHING"
    )


def downgrade() -> None:
    op.execute("UPDATE works SET subject_id = NULL WHERE subject_id = 'other'")
    op.execute("UPDATE shamela_collections SET subject_id = NULL WHERE subject_id = 'other'")
    op.execute("DELETE FROM subjects WHERE id = 'other'")
