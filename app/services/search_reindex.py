"""Rebuild `pages.search_tsv` under a new text-search config, without re-importing books.

`pages` stores no page text (see Page's docstring in app/models/library.py), so a new
search_tsv can only be computed from each book's own JSON file -- through the importer's
own `paginate()`, so the indexed text is exactly what an import would index.

The rebuild writes a complete replacement table, TARGET, in (book_id, sequence) order and
then swaps it in for `pages` in one transaction. Building beside the live table rather than
UPDATE-ing it in place keeps search working throughout, and dropping the old table gives
its space back to the disk immediately (an in-place update of ~8M rows would leave the
old row versions behind as dead space).

Three phases, each safe to re-run:
  1. `book_ids_to_build` + `build_book`  -- the long part; runs while the API serves.
  2. `index_statements`                  -- recreates `pages`' own indexes/constraints
                                            on TARGET under temporary names.
  3. `swap_statements`                   -- after a final `book_ids_to_build` catch-up
                                            (books re-imported since phase 1), swaps.
scripts/search_index/rebuild_exact.py runs 1+2 ahead of time on a big database; the
migration that switches the config runs whatever is left plus 3, so the code change and
the index change land together whichever way the work was split.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.services.arabic import SEARCH_TS_CONFIG
from app.services.paging import paginate

TARGET = "pages_exact"

# Every column of `pages`, in table order; search_tsv is the only one recomputed.
_COPIED_COLUMNS = "id, book_id, sequence, page_number, page_type, is_blank, section_id, block_offsets"

CREATE_TARGET_SQL = f"CREATE TABLE IF NOT EXISTS {TARGET} (LIKE pages INCLUDING DEFAULTS)"

# One statement per book. The join is on sequence, so a page present in `pages` but
# missing from the JSON would silently drop out -- `build_book` checks for that first.
INSERT_BOOK_SQL = f"""
    INSERT INTO {TARGET} ({_COPIED_COLUMNS}, search_tsv)
    SELECT {', '.join('p.' + c.strip() for c in _COPIED_COLUMNS.split(','))},
           to_tsvector('{SEARCH_TS_CONFIG}', arabic_normalize(t.txt))
    FROM pages p
    JOIN unnest(CAST(:seqs AS int[]), CAST(:txts AS text[])) AS t(seq, txt) ON t.seq = p.sequence
    WHERE p.book_id = :bid
    ORDER BY p.sequence
"""

# A book needs (re)building in TARGET when its rows there don't match `pages` exactly:
# absent, or re-imported since (a re-import deletes and re-inserts, so ids change).
BOOKS_TO_BUILD_SQL = f"""
    SELECT p.book_id FROM (
        SELECT book_id, count(*) AS n, min(id) AS lo, max(id) AS hi FROM pages GROUP BY book_id
    ) p
    LEFT JOIN (
        SELECT book_id, count(*) AS n, min(id) AS lo, max(id) AS hi FROM {TARGET} GROUP BY book_id
    ) t USING (book_id)
    WHERE t.book_id IS NULL OR (p.n, p.lo, p.hi) IS DISTINCT FROM (t.n, t.lo, t.hi)
    ORDER BY p.book_id
"""

# Books whose pages were all deleted from `pages` but linger in TARGET.
STALE_IN_TARGET_SQL = f"""
    DELETE FROM {TARGET} t
    WHERE NOT EXISTS (SELECT 1 FROM pages p WHERE p.book_id = t.book_id)
"""

DELETE_BOOK_SQL = f"DELETE FROM {TARGET} WHERE book_id = :bid"
PAGE_SEQUENCES_SQL = "SELECT sequence FROM pages WHERE book_id = :bid"

# `pages`' own indexes and constraints, to recreate on TARGET. Read from the catalog
# rather than hard-coded, so the swapped-in table is identical to the old one.
PAGES_INDEXES_SQL = """
    SELECT c.relname AS name, pg_get_indexdef(i.indexrelid) AS ddl
    FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
    WHERE i.indrelid = 'pages'::regclass
      AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid)
    ORDER BY c.relname
"""
PAGES_CONSTRAINTS_SQL = """
    SELECT conname AS name, pg_get_constraintdef(oid) AS ddl, contype
    FROM pg_constraint
    WHERE conrelid = 'pages'::regclass AND contype IN ('p', 'u', 'f', 'c')
    ORDER BY conname
"""

_NEW_SUFFIX = "__new"


def book_page_texts(books_root: Path, book_id: int) -> dict[int, str] | None:
    """{sequence: text} for one book, from its JSON file; None if the file is unreadable."""
    try:
        content = json.loads((books_root / f"{book_id}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {row.sequence: row.text for row in paginate(content)}


def book_params(book_id: int, texts: dict[int, str]) -> dict:
    seqs = sorted(texts)
    return {"bid": book_id, "seqs": seqs, "txts": [texts[s] for s in seqs]}


def missing_sequences(db_sequences: list[int], texts: dict[int, str]) -> list[int]:
    """Pages the database has for a book that its JSON file doesn't -- these would be
    dropped by INSERT_BOOK_SQL's join, so the caller must treat them as an error."""
    return sorted(set(db_sequences) - set(texts))


def index_statements(existing_indexes: list, existing_constraints: list, target_has: set[str]) -> list[str]:
    """DDL recreating `pages`' indexes and constraints on TARGET under `<name>__new`,
    skipping any already there (so an interrupted run can resume)."""
    statements = []
    for row in existing_indexes:
        new_name = row.name + _NEW_SUFFIX
        if new_name in target_has:
            continue
        statements.append(
            row.ddl.replace(f"INDEX {row.name} ON public.pages ", f"INDEX {new_name} ON public.{TARGET} ", 1)
        )
    for row in existing_constraints:
        new_name = row.name + _NEW_SUFFIX
        if new_name in target_has:
            continue
        statements.append(f"ALTER TABLE {TARGET} ADD CONSTRAINT {new_name} {row.ddl}")
    return statements


def swap_statements(index_names: list[str], constraint_names: list[str]) -> list[str]:
    """Replace `pages` with TARGET. Run in one transaction, after a final catch-up."""
    statements = [
        "LOCK TABLE pages IN ACCESS EXCLUSIVE MODE",
        # pages.id's sequence is owned by the old table: detach it so DROP keeps it.
        "ALTER SEQUENCE pages_id_seq OWNED BY NONE",
        "DROP TABLE pages",
        f"ALTER TABLE {TARGET} RENAME TO pages",
        "ALTER SEQUENCE pages_id_seq OWNED BY pages.id",
    ]
    for name in constraint_names:
        statements.append(f"ALTER TABLE pages RENAME CONSTRAINT {name}{_NEW_SUFFIX} TO {name}")
    for name in index_names:
        statements.append(f"ALTER INDEX {name}{_NEW_SUFFIX} RENAME TO {name}")
    statements.append("ANALYZE pages")
    return statements
