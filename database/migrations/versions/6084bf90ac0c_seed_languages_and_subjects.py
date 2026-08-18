"""Seed the controlled vocabularies.

Reference data, not schema: the two languages present in the corpus (~15,100 Arabic,
~3,700 Persian by the source scan) and the 19 curated subjects the catalog browses by.

The subject list is ours, deliberately separate from Shamela's raw `< مجموعة >` strings —
the scan found 530 distinct raw values (344 after normalization), far too messy and too
long-tailed to expose directly. Mapping raw collections onto these subjects happens in
the importer, so a mapping change never requires re-importing book content.

Original header: seed languages and subjects

Revision ID: 6084bf90ac0c
Revises: 813311c1675b
Create Date: 2026-08-18 14:08:35.594070

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '6084bf90ac0c'
down_revision: str | Sequence[str] | None = '813311c1675b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


LANGUAGES = [
    ("ar", "العربية"),
    ("fa", "الفارسية"),
]

# (slug, Arabic display title). Slugs are stable and safe to switch on in clients; the
# Arabic titles are display strings and may be reworded.
SUBJECTS = [
    ("quran",           "القرآن وعلومه"),
    ("tafsir",          "التفسير"),
    ("hadith",          "الحديث"),
    ("rijal",           "الرجال والدراية"),
    ("aqaid",           "العقائد والكلام"),
    ("usul-fiqh",       "أصول الفقه"),
    ("fiqh",            "الفقه"),
    ("rasail-amaliyya", "الرسائل العملية"),
    ("sira",            "السيرة"),
    ("tarikh",          "التاريخ والجغرافيا"),
    ("tarajim",         "التراجم والأنساب"),
    ("adiya",           "الأدعية والزيارات"),
    ("akhlaq",          "الأخلاق والعرفان"),
    ("falsafa",         "الفلسفة والمنطق"),
    ("lugha",           "اللغة والأدب"),
    ("faharis",         "الفهارس والببليوغرافيا"),
    ("tibb",            "الطب"),
    ("qadaya-muasira",  "قضايا إسلامية ومعاصرة"),
    ("munawwaat",       "منوعات"),
]


def upgrade() -> None:
    languages = sa.table(
        "languages", sa.column("code", sa.String), sa.column("name", sa.Text)
    )
    subjects = sa.table(
        "subjects",
        sa.column("id", sa.String),
        sa.column("title", sa.Text),
        sa.column("sort_order", sa.Integer),
    )
    op.bulk_insert(
        languages, [{"code": code, "name": name} for code, name in LANGUAGES]
    )
    op.bulk_insert(
        subjects,
        [
            {"id": slug, "title": title, "sort_order": index}
            for index, (slug, title) in enumerate(SUBJECTS)
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM subjects")
    op.execute("DELETE FROM languages")
