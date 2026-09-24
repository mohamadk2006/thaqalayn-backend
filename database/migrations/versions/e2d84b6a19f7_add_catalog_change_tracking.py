"""catalog change tracking for the apps' sync endpoints

Powers GET /api/catalog/version and GET /api/books/changes. Done with database
triggers, not application code, so every kind of write is caught: the importer, the admin
panel, scripts, and hand-run SQL alike.

  book_changes   one row per book, holding the sequence number of its latest change.
                 A row for a book that no longer exists (or is unpublished) is its
                 "deleted" record; the feed reports a book's state as of read time.
  catalog_stamps a sequence number per category / library / "categories" / "libraries" /
                 "featured", stamped by the rarely-written structure tables. A
                 category's version is the newest of its own stamp and its books' changes.
  catalog_meta   `tombstone_floor`: the highest sequence number ever purged, so a client
                 with an older cursor is told to reload instead of missing a deletion.

`xid` on each row is the writing transaction's id. Two transactions can commit in the
opposite order to their sequence numbers; serving only rows whose transaction is older
than every still-running one (the reader's snapshot xmin) stops a client from moving its
cursor past a change that has not committed yet.

Which columns matter is exactly what BookOut is built from (see catalog_service._book_out):
book columns, the work's title, the author's name/death label, the work's subjects and
libraries with their titles, and the collection. `imported_at` / `updated_at` are
deliberately not watched, so a reimport that changes nothing does not look like a change.

Revision ID: e2d84b6a19f7
Revises: c93a5d17e8b4
Create Date: 2026-09-24 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e2d84b6a19f7'
down_revision: str | Sequence[str] | None = 'c93a5d17e8b4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BOOK_COLUMNS = (
    "work_id, volume, title, author_id, language_code, publisher, collection_id, "
    "page_first, page_last, paragraph_count, content_bytes, description, "
    "content_version, is_published"
)


def _row(prefix: str) -> str:
    return "(" + ", ".join(f"{prefix}.{c.strip()}" for c in BOOK_COLUMNS.split(",")) + ")"


def upgrade() -> None:
    op.execute("CREATE SEQUENCE catalog_seq")
    op.execute("""
        CREATE TABLE book_changes (
            book_id    integer     PRIMARY KEY,
            seq        bigint      NOT NULL,
            xid        xid8        NOT NULL,
            changed_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX ix_book_changes_seq ON book_changes (seq)")
    op.execute("""
        CREATE TABLE catalog_stamps (
            scope text   NOT NULL,
            key   text   NOT NULL DEFAULT '',
            seq   bigint NOT NULL,
            xid   xid8   NOT NULL,
            PRIMARY KEY (scope, key)
        )
    """)
    op.execute("CREATE TABLE catalog_meta (key text PRIMARY KEY, value bigint NOT NULL)")
    op.execute("INSERT INTO catalog_meta (key, value) VALUES ('tombstone_floor', 0)")

    op.execute("""
        CREATE FUNCTION catalog_touch_book(bid integer) RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO book_changes (book_id, seq, xid)
            VALUES (bid, nextval('catalog_seq'), pg_current_xact_id())
            ON CONFLICT (book_id) DO UPDATE
                SET seq = EXCLUDED.seq, xid = EXCLUDED.xid, changed_at = now();
        END $$
    """)
    op.execute("""
        CREATE FUNCTION catalog_stamp(sc text, k text) RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO catalog_stamps (scope, key, seq, xid)
            VALUES (sc, k, nextval('catalog_seq'), pg_current_xact_id())
            ON CONFLICT (scope, key) DO UPDATE SET seq = EXCLUDED.seq, xid = EXCLUDED.xid;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION catalog_touch_work_books(wid integer) RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM catalog_touch_book(id) FROM books WHERE work_id = wid;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION catalog_stamp_work_groups(wid integer) RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM catalog_stamp('subject', subject_id) FROM work_subjects WHERE work_id = wid;
            PERFORM catalog_stamp('library', CAST(library_id AS text))
                FROM library_works WHERE work_id = wid;
        END $$
    """)

    # ── books ───────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE FUNCTION trg_catalog_books() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM catalog_touch_book(OLD.id);
                -- the join that derives a category's version no longer sees this book
                PERFORM catalog_stamp_work_groups(OLD.work_id);
                RETURN OLD;
            END IF;
            PERFORM catalog_touch_book(NEW.id);
            IF TG_OP = 'UPDATE' AND OLD.work_id IS DISTINCT FROM NEW.work_id THEN
                PERFORM catalog_stamp_work_groups(OLD.work_id);
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER catalog_books_insdel AFTER INSERT OR DELETE ON books
        FOR EACH ROW EXECUTE FUNCTION trg_catalog_books()
    """)
    op.execute(f"""
        CREATE TRIGGER catalog_books_upd AFTER UPDATE ON books
        FOR EACH ROW WHEN ({_row('OLD')} IS DISTINCT FROM {_row('NEW')})
        EXECUTE FUNCTION trg_catalog_books()
    """)

    # ── works ───────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE FUNCTION trg_catalog_works() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.is_featured THEN PERFORM catalog_stamp('featured', ''); END IF;
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                IF OLD.is_featured THEN PERFORM catalog_stamp('featured', ''); END IF;
                RETURN OLD;
            END IF;
            IF OLD.title IS DISTINCT FROM NEW.title THEN
                PERFORM catalog_touch_work_books(NEW.id);
            END IF;
            IF OLD.is_featured IS DISTINCT FROM NEW.is_featured
               OR OLD.featured_sort_order IS DISTINCT FROM NEW.featured_sort_order THEN
                PERFORM catalog_stamp('featured', '');
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER catalog_works_insdel AFTER INSERT OR DELETE ON works
        FOR EACH ROW EXECUTE FUNCTION trg_catalog_works()
    """)
    op.execute("""
        CREATE TRIGGER catalog_works_upd AFTER UPDATE ON works
        FOR EACH ROW WHEN ((OLD.title, OLD.is_featured, OLD.featured_sort_order)
                           IS DISTINCT FROM (NEW.title, NEW.is_featured, NEW.featured_sort_order))
        EXECUTE FUNCTION trg_catalog_works()
    """)

    # ── authors ─────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE FUNCTION trg_catalog_authors() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM catalog_touch_book(id) FROM books WHERE author_id = NEW.id;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER catalog_authors_upd AFTER UPDATE ON authors
        FOR EACH ROW WHEN ((OLD.name, OLD.death_label) IS DISTINCT FROM (NEW.name, NEW.death_label))
        EXECUTE FUNCTION trg_catalog_authors()
    """)

    # ── memberships ─────────────────────────────────────────────────────────────
    op.execute("""
        CREATE FUNCTION trg_catalog_work_subjects() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM catalog_stamp('subject', OLD.subject_id);
                PERFORM catalog_touch_work_books(OLD.work_id);
                RETURN OLD;
            END IF;
            PERFORM catalog_stamp('subject', NEW.subject_id);
            PERFORM catalog_touch_work_books(NEW.work_id);
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER catalog_work_subjects_chg AFTER INSERT OR DELETE ON work_subjects
        FOR EACH ROW EXECUTE FUNCTION trg_catalog_work_subjects()
    """)
    op.execute("""
        CREATE FUNCTION trg_catalog_library_works() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM catalog_stamp('library', CAST(OLD.library_id AS text));
                PERFORM catalog_touch_work_books(OLD.work_id);
                RETURN OLD;
            END IF;
            PERFORM catalog_stamp('library', CAST(NEW.library_id AS text));
            PERFORM catalog_touch_work_books(NEW.work_id);
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER catalog_library_works_chg AFTER INSERT OR DELETE ON library_works
        FOR EACH ROW EXECUTE FUNCTION trg_catalog_library_works()
    """)

    # ── categories and libraries themselves ─────────────────────────────────────
    op.execute("""
        CREATE FUNCTION trg_catalog_subjects() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM catalog_stamp('categories', '');
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            IF TG_OP = 'UPDATE' AND OLD.title IS DISTINCT FROM NEW.title THEN
                -- every book carries its categories' titles
                PERFORM catalog_stamp('subject', NEW.id);
                PERFORM catalog_touch_book(b.id)
                    FROM books b JOIN work_subjects ws ON ws.work_id = b.work_id
                    WHERE ws.subject_id = NEW.id;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER catalog_subjects_insdel AFTER INSERT OR DELETE ON subjects
        FOR EACH ROW EXECUTE FUNCTION trg_catalog_subjects()
    """)
    op.execute("""
        CREATE TRIGGER catalog_subjects_upd AFTER UPDATE ON subjects
        FOR EACH ROW WHEN ((OLD.title, OLD.sort_order) IS DISTINCT FROM (NEW.title, NEW.sort_order))
        EXECUTE FUNCTION trg_catalog_subjects()
    """)
    op.execute("""
        CREATE FUNCTION trg_catalog_libraries() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM catalog_stamp('libraries', '');
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            IF TG_OP = 'UPDATE' AND (OLD.title IS DISTINCT FROM NEW.title
                                     OR OLD.parent_id IS DISTINCT FROM NEW.parent_id) THEN
                PERFORM catalog_stamp('library', CAST(NEW.id AS text));
                PERFORM catalog_touch_book(b.id)
                    FROM books b JOIN library_works lw ON lw.work_id = b.work_id
                    WHERE lw.library_id = NEW.id;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER catalog_libraries_insdel AFTER INSERT OR DELETE ON libraries
        FOR EACH ROW EXECUTE FUNCTION trg_catalog_libraries()
    """)
    op.execute("""
        CREATE TRIGGER catalog_libraries_upd AFTER UPDATE ON libraries
        FOR EACH ROW WHEN ((OLD.title, OLD.parent_id, OLD.sort_order)
                           IS DISTINCT FROM (NEW.title, NEW.parent_id, NEW.sort_order))
        EXECUTE FUNCTION trg_catalog_libraries()
    """)

    # Every existing book starts with a sequence number, so a client's very first
    # `since` (there is none) resets and one taken after this point is meaningful.
    op.execute("""
        INSERT INTO book_changes (book_id, seq, xid)
        SELECT id, nextval('catalog_seq'), pg_current_xact_id()
        FROM (SELECT id FROM books ORDER BY id) b
    """)


def downgrade() -> None:
    for trigger, table in [
        ("catalog_books_insdel", "books"), ("catalog_books_upd", "books"),
        ("catalog_works_insdel", "works"), ("catalog_works_upd", "works"),
        ("catalog_authors_upd", "authors"),
        ("catalog_work_subjects_chg", "work_subjects"),
        ("catalog_library_works_chg", "library_works"),
        ("catalog_subjects_insdel", "subjects"), ("catalog_subjects_upd", "subjects"),
        ("catalog_libraries_insdel", "libraries"), ("catalog_libraries_upd", "libraries"),
    ]:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for func in [
        "trg_catalog_books()", "trg_catalog_works()", "trg_catalog_authors()",
        "trg_catalog_work_subjects()", "trg_catalog_library_works()",
        "trg_catalog_subjects()", "trg_catalog_libraries()",
        "catalog_stamp_work_groups(integer)", "catalog_touch_work_books(integer)",
        "catalog_stamp(text, text)", "catalog_touch_book(integer)",
    ]:
        op.execute(f"DROP FUNCTION IF EXISTS {func}")
    op.execute("DROP TABLE catalog_meta")
    op.execute("DROP TABLE catalog_stamps")
    op.execute("DROP TABLE book_changes")
    op.execute("DROP SEQUENCE catalog_seq")
