"""multi-category subjects, id = the Arabic category string itself

Replaces the single-category `works.subject_id` column with real many-to-many support
via a new `work_subjects` join table, and replaces the old English-slug `subjects.id`
(e.g. "aqaid-sunni") with the category's own Arabic string (e.g. "مصادر العقائد عند
السنيين") as both id and title -- given directly by the project owner as the definitive
39-category list, replacing the automatic collection-string-regex classification this
project used until now (which produced real, confirmed bugs -- see be55b92 and the
session that led here). The project owner will supply the actual per-work category
assignments (one or more per work) separately; this migration only builds the structure,
leaving `work_subjects` empty except for a best-effort carry-over of each work's existing
single subject (renamed 1:1 from slug to Arabic string, dropped entirely for the old
40th "other" catch-all, which has no equivalent in the real 39).

`shamela_collections.subject_id` is dropped along with the scripts that maintained it
(build_collection_map.py's output, load_collection_map.py, resync_taxonomy.py) -- that
whole pipeline existed to auto-derive a single subject from the raw `< مجموعة >` string,
which is exactly the mechanism being retired.

Revision ID: 258fbe358dd1
Revises: af52336b93fe
Create Date: 2026-09-07

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "258fbe358dd1"
down_revision: str | Sequence[str] | None = "af52336b93fe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (old slug, Arabic string) -- identical list to the one already seeded by
# 42bb31eb450d, used here only to carry existing single-subject assignments over to
# their new id before the old slug id disappears. Order matches the project owner's
# own numbered list (1-39); the old 40th "other" catch-all has no equivalent here and is
# intentionally absent -- works classified "other" end up with zero rows in
# work_subjects, unclassified until the real per-work data arrives.
SLUG_TO_ARABIC = [
    ("aqaid-sunni",           "مصادر العقائد عند السنيين"),
    ("aqaid-shia",            "مصادر العقائد عند الشيعة"),
    ("rijal-sunni",           "مصادر رجال الحديث عند السنة"),
    ("rijal-shia",            "مصادر رجال الحديث عند الشيعة"),
    ("sira",                  "مصادر سيرة النبي والأئمة (ع)"),
    ("fiqh-mustaqilla",       "مصادر فقهية مستقلة"),
    ("mustalahat-fiqhiyya",   "مصطلحات ومفردات فقهية"),
    ("makhtutat",             "مخطوطات"),
    ("tarikh-jughrafia",      "مصادر التاريخ والجغرافيا"),
    ("tafsir-sunni",          "مصادر التفسير عند السنة"),
    ("tafsir-shia",           "مصادر التفسير عند الشيعة"),
    ("hadith-sunni-amm",      "مصادر الحديث السنية - القسم العام"),
    ("hadith-sunni-fiqh",     "مصادر الحديث السنية - قسم الفقه"),
    ("hadith-shia-amm",       "مصادر الحديث الشيعية - القسم العام"),
    ("hadith-shia-fiqh",      "مصادر الحديث الشيعية - قسم الفقه"),
    ("fiqh-hanbali",          "فقه المذهب الحنبلي"),
    ("fiqh-hanafi",           "فقه المذهب الحنفي"),
    ("fiqh-zaydi",            "فقه المذهب الزيدي"),
    ("fiqh-shafii",           "فقه المذهب الشافعي"),
    ("fiqh-zahiri",           "فقه المذهب الظاهري"),
    ("fiqh-maliki",           "فقه المذهب المالكي"),
    ("qadaya-muasira",        "قضايا إسلامية ومعاصرة"),
    ("majallat-munawwaat",    "مجلات ومنوعات"),
    ("mantiq-falsafa",        "المنطق والفلسفة"),
    ("dalil-muallafat",       "دليل المؤلفات وفهارس المكاتب"),
    ("dawawin-shir",          "دواوين الشعر"),
    ("ulum-ukhra",            "علوم أخرى"),
    ("ulum-lugha",            "علوم اللغة العربية"),
    ("fiqh-shia-fatawa",      "فقه الشيعة - فتاوى المراجع"),
    ("fiqh-shia-qabl-thamin", "فقه الشيعة إلى القرن الثامن"),
    ("fiqh-shia-bad-thamin",  "فقه الشيعة من القرن الثامن"),
    ("usul-fiqh-shia",        "أصول الفقه عند الشيعة"),
    ("usul-fiqh-sunni",       "أصول الفقه عند المذاهب السنية"),
    ("akhlaq-irfan",          "الأخلاق والعرفان"),
    ("adiya-ziyarat",         "الأدعية والزيارات"),
    ("ansab-tarajim",         "الأنساب والتراجم"),
    ("tibb",                  "الطب"),
    ("firaq-madhahib",        "الفرق والمذاهب"),
    ("quran-ulum",            "القرآن الكريم وعلومه"),
]


def upgrade() -> None:
    # Carry existing single-subject assignments over to a temp column keyed by the new
    # id, before the FKs to the old slug-keyed subjects.id are dropped.
    op.add_column("works", sa.Column("_new_subject_id", sa.Text(), nullable=True))
    for slug, arabic in SLUG_TO_ARABIC:
        op.execute(
            sa.text("UPDATE works SET _new_subject_id = :arabic WHERE subject_id = :slug")
            .bindparams(arabic=arabic, slug=slug)
        )

    op.drop_index("ix_works_subject", table_name="works")
    op.drop_constraint("works_subject_id_fkey", "works", type_="foreignkey")
    op.drop_column("works", "subject_id")

    op.drop_constraint("shamela_collections_subject_id_fkey", "shamela_collections", type_="foreignkey")
    op.drop_column("shamela_collections", "subject_id")

    # Rebuild subjects: id/title both become the Arabic string, 39 rows, no "other".
    op.execute("DELETE FROM subjects")
    op.alter_column("subjects", "id", type_=sa.Text(), existing_type=sa.String(32))
    subjects = sa.table(
        "subjects",
        sa.column("id", sa.Text()),
        sa.column("title", sa.Text()),
        sa.column("sort_order", sa.Integer()),
    )
    op.bulk_insert(
        subjects,
        [
            {"id": arabic, "title": arabic, "sort_order": i}
            for i, (_, arabic) in enumerate(SLUG_TO_ARABIC, start=1)
        ],
    )

    op.create_table(
        "work_subjects",
        sa.Column("work_id", sa.Integer(), sa.ForeignKey("works.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("subject_id", sa.Text(), sa.ForeignKey("subjects.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_work_subjects_subject", "work_subjects", ["subject_id"])

    op.execute("""
        INSERT INTO work_subjects (work_id, subject_id)
        SELECT id, _new_subject_id FROM works WHERE _new_subject_id IS NOT NULL
    """)
    op.drop_column("works", "_new_subject_id")


def downgrade() -> None:
    ARABIC_TO_SLUG = {arabic: slug for slug, arabic in SLUG_TO_ARABIC}

    op.add_column("works", sa.Column("subject_id", sa.String(32), nullable=True))
    op.execute("""
        UPDATE works w SET subject_id = sub.subject_id
        FROM (
            SELECT DISTINCT ON (work_id) work_id, subject_id
            FROM work_subjects ORDER BY work_id, subject_id
        ) sub
        WHERE w.id = sub.work_id
    """)
    for arabic, slug in ARABIC_TO_SLUG.items():
        op.execute(
            sa.text("UPDATE works SET subject_id = :slug WHERE subject_id = :arabic")
            .bindparams(slug=slug, arabic=arabic)
        )

    op.drop_index("ix_work_subjects_subject", table_name="work_subjects")
    op.drop_table("work_subjects")

    op.execute("DELETE FROM subjects")
    op.alter_column("subjects", "id", type_=sa.String(32), existing_type=sa.Text())
    subjects = sa.table(
        "subjects",
        sa.column("id", sa.String(32)),
        sa.column("title", sa.Text()),
        sa.column("sort_order", sa.Integer()),
    )
    op.bulk_insert(
        subjects,
        [
            {"id": slug, "title": arabic, "sort_order": i}
            for i, (slug, arabic) in enumerate(SLUG_TO_ARABIC, start=1)
        ] + [{"id": "other", "title": "غير مصنف", "sort_order": 40}],
    )

    op.add_column("shamela_collections", sa.Column("subject_id", sa.String(32), nullable=True))
    op.create_foreign_key(
        "shamela_collections_subject_id_fkey", "shamela_collections", "subjects",
        ["subject_id"], ["id"],
    )

    op.create_foreign_key("works_subject_id_fkey", "works", "subjects", ["subject_id"], ["id"])
    op.create_index("ix_works_subject", "works", ["subject_id"])
