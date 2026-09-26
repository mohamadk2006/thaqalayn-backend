"""Search matches exact words: rebuild pages.search_tsv with the 'simple' config

Revision ID: f41d0c7a9b22
Revises: 7a30c5e1d2b9
Create Date: 2026-09-26

Undoes af52336b93fe's switch to the Snowball 'arabic' stemmer: stemming made a search for
"الاغتسال ليلا" also return "الاغتسال بالليل" and "اغتسالها بالليل", and the library wants
the words as typed (still normalized: tashkeel, hamza forms etc. are folded).

`pages` stores no text, so the new tsvectors come from the books' JSON files under
BOOKS_ROOT (see app/services/search_reindex.py). On the production-sized table that takes
hours, so scripts/search_index/rebuild_exact.py builds and indexes pages_exact beforehand
while the old code keeps serving; this migration then only rebuilds books re-imported
since, and swaps. On a small database it simply does all of it here. Either way the swap
happens in the same deploy that switches the code to SEARCH_TS_CONFIG = 'simple'.

Fails -- leaving `pages` untouched -- if any book's JSON is missing or disagrees with its
rows, rather than swap in an index that silently lost pages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "f41d0c7a9b22"
down_revision: str | Sequence[str] | None = "7a30c5e1d2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from app.config import get_settings
    from app.services import search_reindex as R

    bind = op.get_bind()
    books_root = Path(get_settings().books_root)

    bind.execute(text(R.CREATE_TARGET_SQL))
    bind.execute(text(R.CREATE_TARGET_BOOK_INDEX_SQL))
    bind.execute(text(R.STALE_IN_TARGET_SQL))
    problems = []
    for (book_id,) in bind.execute(text(R.BOOKS_TO_BUILD_SQL)).all():
        texts = R.book_page_texts(books_root, book_id)
        if texts is None:
            problems.append(f"{book_id}: JSON file missing or unreadable under {books_root}")
            continue
        db_seqs = [r[0] for r in bind.execute(text(R.PAGE_SEQUENCES_SQL), {"bid": book_id}).all()]
        missing = R.missing_sequences(db_seqs, texts)
        if missing:
            problems.append(f"{book_id}: {len(missing)} page(s) not in its JSON, e.g. {missing[:5]}")
            continue
        bind.execute(text(R.DELETE_BOOK_SQL), {"bid": book_id})
        bind.execute(text(R.INSERT_BOOK_SQL), R.book_params(book_id, texts))
    if problems:
        raise RuntimeError(
            "exact-word search index: cannot rebuild these books, `pages` left unchanged:\n  "
            + "\n  ".join(problems)
        )

    indexes = bind.execute(text(R.PAGES_INDEXES_SQL)).all()
    constraints = bind.execute(text(R.PAGES_CONSTRAINTS_SQL)).all()
    target_has = {r[0] for r in bind.execute(text(f"""
        SELECT relname FROM pg_class WHERE oid IN (
            SELECT indexrelid FROM pg_index WHERE indrelid = '{R.TARGET}'::regclass)
        UNION SELECT conname FROM pg_constraint WHERE conrelid = '{R.TARGET}'::regclass
    """)).all()}
    for ddl in R.index_statements(indexes, constraints, target_has):
        bind.execute(text(ddl))
    for ddl in R.swap_statements([r.name for r in indexes], [r.name for r in constraints]):
        bind.execute(text(ddl))


def downgrade() -> None:
    # Going back to stemming means recomputing every tsvector from the JSON files again,
    # with the old code's config -- a rebuild, not something a schema downgrade can do.
    raise NotImplementedError(
        "Revert the code to SEARCH_TS_CONFIG = 'arabic' and rebuild with "
        "scripts/search_index/rebuild_exact.py instead."
    )
