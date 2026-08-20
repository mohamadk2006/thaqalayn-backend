"""replace taxonomy with shamela 39 categories

The 20-subject scheme plus separate tradition/madhhab/format columns is replaced by
Shamela's own published 39-category list, given directly by the project owner as
ground truth (not a scheme this project invented). Most of what tradition/madhhab/
format captured is already encoded in which of the 39 a work falls under (e.g. فقه
المذهب الحنبلي already says fiqh + hanbali), and the project owner asked for the 39
to be the single source of classification truth instead.

This migration only reshapes schema. The actual per-work reclassification is a
separate, non-Alembic step (scripts/import/load_collection_map.py followed by
scripts/import/resync_taxonomy.py against the regenerated collection_map.json) — the
same pattern this project already uses for every taxonomy revision, so that changing
the mapping never requires re-importing book content.

Revision ID: 42bb31eb450d
Revises: 5352cd4b99ea
Create Date: 2026-08-20 02:57:16.615954

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '42bb31eb450d'
down_revision: str | Sequence[str] | None = '5352cd4b99ea'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (slug, Arabic display title) — Shamela's own 39, in the project owner's given order.
OLD_SUBJECTS = [
    "quran", "tafsir", "hadith", "rijal", "aqaid", "usul-fiqh", "fiqh",
    "rasail-amaliyya", "sira", "tarikh", "tarajim", "adiya", "akhlaq",
    "falsafa", "lugha", "faharis", "tibb", "qadaya-muasira", "munawwaat",
    "ulum-ukhra",
]

NEW_SUBJECTS = [
    ("aqaid-sunni",          "مصادر العقائد عند السنيين"),
    ("aqaid-shia",           "مصادر العقائد عند الشيعة"),
    ("rijal-sunni",          "مصادر رجال الحديث عند السنة"),
    ("rijal-shia",           "مصادر رجال الحديث عند الشيعة"),
    ("sira",                 "مصادر سيرة النبي والأئمة (ع)"),
    ("fiqh-mustaqilla",      "مصادر فقهية مستقلة"),
    ("mustalahat-fiqhiyya",  "مصطلحات ومفردات فقهية"),
    ("makhtutat",            "مخطوطات"),
    ("tarikh-jughrafia",     "مصادر التاريخ والجغرافيا"),
    ("tafsir-sunni",         "مصادر التفسير عند السنة"),
    ("tafsir-shia",          "مصادر التفسير عند الشيعة"),
    ("hadith-sunni-amm",     "مصادر الحديث السنية - القسم العام"),
    ("hadith-sunni-fiqh",    "مصادر الحديث السنية - قسم الفقه"),
    ("hadith-shia-amm",      "مصادر الحديث الشيعية - القسم العام"),
    ("hadith-shia-fiqh",     "مصادر الحديث الشيعية - قسم الفقه"),
    ("fiqh-hanbali",         "فقه المذهب الحنبلي"),
    ("fiqh-hanafi",          "فقه المذهب الحنفي"),
    ("fiqh-zaydi",           "فقه المذهب الزيدي"),
    ("fiqh-shafii",          "فقه المذهب الشافعي"),
    ("fiqh-zahiri",          "فقه المذهب الظاهري"),
    ("fiqh-maliki",          "فقه المذهب المالكي"),
    ("qadaya-muasira",       "قضايا إسلامية ومعاصرة"),
    ("majallat-munawwaat",   "مجلات ومنوعات"),
    ("mantiq-falsafa",       "المنطق والفلسفة"),
    ("dalil-muallafat",      "دليل المؤلفات وفهارس المكاتب"),
    ("dawawin-shir",         "دواوين الشعر"),
    ("ulum-ukhra",           "علوم أخرى"),
    ("ulum-lugha",           "علوم اللغة العربية"),
    ("fiqh-shia-fatawa",     "فقه الشيعة - فتاوى المراجع"),
    ("fiqh-shia-qabl-thamin","فقه الشيعة إلى القرن الثامن"),
    ("fiqh-shia-bad-thamin", "فقه الشيعة من القرن الثامن"),
    ("usul-fiqh-shia",       "أصول الفقه عند الشيعة"),
    ("usul-fiqh-sunni",      "أصول الفقه عند المذاهب السنية"),
    ("akhlaq-irfan",         "الأخلاق والعرفان"),
    ("adiya-ziyarat",        "الأدعية والزيارات"),
    ("ansab-tarajim",        "الأنساب والتراجم"),
    ("tibb",                 "الطب"),
    ("firaq-madhahib",       "الفرق والمذاهب"),
    ("quran-ulum",           "القرآن الكريم وعلومه"),
]


def upgrade() -> None:
    # FKs must be cleared before the rows they point at can be deleted.
    op.execute("UPDATE works SET subject_id = NULL")
    op.execute("UPDATE shamela_collections SET subject_id = NULL")
    op.execute("DELETE FROM subjects")

    subjects = sa.table(
        "subjects",
        sa.column("id", sa.String),
        sa.column("title", sa.Text),
        sa.column("sort_order", sa.Integer),
    )
    op.bulk_insert(
        subjects,
        [
            {"id": slug, "title": title, "sort_order": index}
            for index, (slug, title) in enumerate(NEW_SUBJECTS)
        ],
    )

    op.drop_index("ix_works_tradition", table_name="works")
    op.drop_column("works", "tradition")
    op.drop_column("works", "madhhab")
    op.drop_column("works", "format")
    op.drop_column("shamela_collections", "tradition")
    op.drop_column("shamela_collections", "madhhab")
    op.drop_column("shamela_collections", "format")

    op.execute("DROP TYPE tradition")
    op.execute("DROP TYPE book_format")


def downgrade() -> None:
    tradition_enum = sa.Enum(
        "shia", "sunni", "zaydi", "shared", name="tradition", create_type=True
    )
    format_enum = sa.Enum(
        "book", "manuscript", "journal", "diwan", "dictionary", "index",
        name="book_format", create_type=True,
    )
    tradition_enum.create(op.get_bind())
    format_enum.create(op.get_bind())

    op.add_column("works", sa.Column("tradition", tradition_enum, nullable=True))
    op.add_column("works", sa.Column("madhhab", sa.String(32), nullable=True))
    op.add_column("works", sa.Column("format", format_enum, nullable=True))
    op.create_index("ix_works_tradition", "works", ["tradition"])

    op.add_column("shamela_collections", sa.Column("tradition", tradition_enum, nullable=True))
    op.add_column("shamela_collections", sa.Column("madhhab", sa.String(32), nullable=True))
    op.add_column("shamela_collections", sa.Column("format", format_enum, nullable=True))

    op.execute("UPDATE works SET subject_id = NULL")
    op.execute("UPDATE shamela_collections SET subject_id = NULL")
    op.execute("DELETE FROM subjects")

    subjects = sa.table(
        "subjects",
        sa.column("id", sa.String),
        sa.column("title", sa.Text),
        sa.column("sort_order", sa.Integer),
    )
    old_titles = {
        "quran": "القرآن وعلومه", "tafsir": "التفسير", "hadith": "الحديث",
        "rijal": "الرجال والدراية", "aqaid": "العقائد والكلام",
        "usul-fiqh": "أصول الفقه", "fiqh": "الفقه",
        "rasail-amaliyya": "الرسائل العملية", "sira": "السيرة",
        "tarikh": "التاريخ والجغرافيا", "tarajim": "التراجم والأنساب",
        "adiya": "الأدعية والزيارات", "akhlaq": "الأخلاق والعرفان",
        "falsafa": "الفلسفة والمنطق", "lugha": "اللغة والأدب",
        "faharis": "الفهارس والببليوغرافيا", "tibb": "الطب",
        "qadaya-muasira": "قضايا إسلامية ومعاصرة", "munawwaat": "منوعات",
        "ulum-ukhra": "علوم أخرى",
    }
    op.bulk_insert(
        subjects,
        [
            {"id": slug, "title": old_titles[slug], "sort_order": index}
            for index, slug in enumerate(OLD_SUBJECTS)
        ],
    )
