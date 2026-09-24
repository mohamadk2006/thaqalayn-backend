"""category order, section and pinned books from the app

The app owned the category order; this moves it to the server so the app can fetch it
(GET /api/categories -> `order`, `section`, `pinnedBookIds`). `section` splits the list in
two -- "shia" (الكتب الشيعية, positions 1-13) and "other" (الكتب الأخرى, 14-40) -- and a
pinned book is one the app shows first inside its category (for القرآن الكريم وعلومه,
book 3930). The order below is the app's own, taken from its category-order file, so
nothing moves on the phone when it switches to the server's values.

A change to any of these counts as a change to the category list, so `categories.version`
in GET /api/catalog/version moves with them (triggers below).

The migration refuses to run unless all 40 categories are matched, so a renamed or missing
category cannot leave the order half applied. downgrade() removes the new columns and table
but does not restore the previous sort order.

Revision ID: 7a30c5e1d2b9
Revises: e2d84b6a19f7
Create Date: 2026-09-24 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '7a30c5e1d2b9'
down_revision: str | Sequence[str] | None = 'e2d84b6a19f7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (category id, position, section)
ORDER = [
    ('القرآن الكريم وعلومه', 1, 'shia'),
    ('مصادر العقائد عند الشيعة', 2, 'shia'),
    ('مصادر سيرة النبي والأئمة (ع)', 3, 'shia'),
    ('مصادر التفسير عند الشيعة', 4, 'shia'),
    ('أصول الفقه عند الشيعة', 5, 'shia'),
    ('مصادر الحديث الشيعية - القسم العام', 6, 'shia'),
    ('مصادر الحديث الشيعية - قسم الفقه', 7, 'shia'),
    ('مصادر رجال الحديث عند الشيعة', 8, 'shia'),
    ('فقه الشيعة إلى القرن الثامن', 9, 'shia'),
    ('فقه الشيعة من القرن الثامن', 10, 'shia'),
    ('فقه الشيعة - فتاوى المراجع', 11, 'shia'),
    ('الأدعية والزيارات', 12, 'shia'),
    ('مصطلحات ومفردات فقهية', 13, 'shia'),
    ('القرآن الكريم وعلومه عند السنة', 14, 'other'),
    ('فقه المذهب الزيدي', 15, 'other'),
    ('فقه المذهب الشافعي', 16, 'other'),
    ('فقه المذهب المالكي', 17, 'other'),
    ('فقه المذهب الحنفي', 18, 'other'),
    ('فقه المذهب الحنبلي', 19, 'other'),
    ('فقه المذهب الظاهري', 20, 'other'),
    ('مصادر فقهية مستقلة', 21, 'other'),
    ('مصادر الحديث السنية - قسم الفقه', 22, 'other'),
    ('مصادر الحديث السنية - القسم العام', 23, 'other'),
    ('مصادر التفسير عند السنة', 24, 'other'),
    ('أصول الفقه عند المذاهب السنية', 25, 'other'),
    ('مصادر رجال الحديث عند السنة', 26, 'other'),
    ('مصادر العقائد عند السنيين', 27, 'other'),
    ('الفرق والمذاهب', 28, 'other'),
    ('مصادر التاريخ والجغرافيا', 29, 'other'),
    ('قضايا إسلامية ومعاصرة', 30, 'other'),
    ('علوم اللغة العربية', 31, 'other'),
    ('دواوين الشعر', 32, 'other'),
    ('الأنساب والتراجم', 33, 'other'),
    ('دليل المؤلفات وفهارس المكاتب', 34, 'other'),
    ('الأخلاق والعرفان', 35, 'other'),
    ('المنطق والفلسفة', 36, 'other'),
    ('الطب', 37, 'other'),
    ('مجلات ومنوعات', 38, 'other'),
    ('علوم أخرى', 39, 'other'),
    ('مخطوطات', 40, 'other'),
]

# (category id, book id, position among that category's pinned books)
PINS = [
    ('القرآن الكريم وعلومه', 3930, 1),
]


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE subjects
            ADD COLUMN section text NOT NULL DEFAULT 'other',
            ADD CONSTRAINT ck_subjects_section CHECK (section IN ('shia', 'other'))
    """)
    op.execute("""
        CREATE TABLE subject_pinned_books (
            subject_id text    NOT NULL REFERENCES subjects (id) ON DELETE CASCADE ON UPDATE CASCADE,
            book_id    integer NOT NULL REFERENCES books (id) ON DELETE CASCADE,
            position   integer NOT NULL,
            PRIMARY KEY (subject_id, book_id)
        )
    """)

    # Any change to these is a change to the category list.
    op.execute("DROP TRIGGER catalog_subjects_upd ON subjects")
    op.execute("""
        CREATE TRIGGER catalog_subjects_upd AFTER UPDATE ON subjects
        FOR EACH ROW WHEN ((OLD.title, OLD.sort_order, OLD.section)
                           IS DISTINCT FROM (NEW.title, NEW.sort_order, NEW.section))
        EXECUTE FUNCTION trg_catalog_subjects()
    """)
    op.execute("""
        CREATE FUNCTION trg_catalog_subject_pins() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM catalog_stamp('categories', '');
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER catalog_subject_pins_chg
        AFTER INSERT OR UPDATE OR DELETE ON subject_pinned_books
        FOR EACH ROW EXECUTE FUNCTION trg_catalog_subject_pins()
    """)

    order_values = ", ".join(f"({_sql_str(i)}, {n}, {_sql_str(s)})" for i, n, s in ORDER)
    op.execute(f"""
        DO $$
        DECLARE matched integer;
        BEGIN
            WITH wanted (id, ord, section) AS (VALUES {order_values}),
                 done AS (
                     UPDATE subjects s SET sort_order = w.ord, section = w.section
                     FROM wanted w WHERE s.id = w.id RETURNING s.id
                 )
            SELECT count(*) INTO matched FROM done;
            IF matched <> 40 THEN
                RAISE EXCEPTION 'expected to place 40 categories, matched %', matched;
            END IF;
        END $$
    """)

    # Only pins whose book exists (a fresh database has none of the corpus).
    pin_values = ", ".join(f"({_sql_str(s)}, {b}, {p})" for s, b, p in PINS)
    op.execute(f"""
        INSERT INTO subject_pinned_books (subject_id, book_id, position)
        SELECT p.subject_id, p.book_id, p.position
        FROM (VALUES {pin_values}) AS p (subject_id, book_id, position)
        WHERE EXISTS (SELECT 1 FROM books b WHERE b.id = p.book_id)
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS catalog_subject_pins_chg ON subject_pinned_books")
    op.execute("DROP FUNCTION IF EXISTS trg_catalog_subject_pins()")
    op.execute("DROP TABLE subject_pinned_books")
    op.execute("DROP TRIGGER catalog_subjects_upd ON subjects")
    op.execute("""
        CREATE TRIGGER catalog_subjects_upd AFTER UPDATE ON subjects
        FOR EACH ROW WHEN ((OLD.title, OLD.sort_order) IS DISTINCT FROM (NEW.title, NEW.sort_order))
        EXECUTE FUNCTION trg_catalog_subjects()
    """)
    op.execute("ALTER TABLE subjects DROP CONSTRAINT ck_subjects_section, DROP COLUMN section")
