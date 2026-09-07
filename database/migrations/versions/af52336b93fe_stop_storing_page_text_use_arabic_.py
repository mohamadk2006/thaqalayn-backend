"""Stop storing page text; switch search to the Arabic-stemmed config

Revision ID: af52336b93fe
Revises: 8ca72a9842a3
Create Date: 2026-09-07

Two changes to `pages`, made together because both were found while chasing the same
problem -- the pages table (text + its GIN index) outgrowing the VPS's disk:

1. Drop the `text` column entirely. The page's text already lives once, on disk, in the
   book's own downloadable JSON file; storing a second full copy in Postgres just to
   build search snippets from was found to roughly double this table's footprint.
   `search_tsv` becomes a plain column the importer populates directly -- passing the
   page text as a bind parameter that is never itself persisted -- instead of a
   GENERATED column, which requires a stored source column to generate from.

2. Switch the tsvector config from 'simple' to 'arabic' (Postgres's built-in Snowball
   stemmer). Measured on a real 20k-row sample of this corpus's actual text: stemming
   shrinks the index by ~42%, and as a bonus means a query for one inflected form of an
   Arabic word also finds other forms of the same word, which 'simple' never did.

TRUNCATEs pages and sections rather than altering them in place: disk is critically low
on the target VPS right now, and an in-place ALTER/DROP COLUMN on a ~55 GB table would
not reclaim space immediately (Postgres doesn't reclaim dropped-column storage until a
rewrite), risking not having room for the very reimport this migration exists to enable.
Both tables are about to be fully repopulated from the already-converted JSON corpus
regardless -- they're a derived search index, not a source of truth -- so nothing here
is lost that a reimport doesn't immediately replace.
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "af52336b93fe"
down_revision: str | Sequence[str] | None = "8ca72a9842a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Reclaim space immediately -- see module docstring. Sections first: pages has no FK
    # to worry about here (ON DELETE SET NULL), but truncating in dependency order is
    # the clearer statement of intent regardless.
    op.execute("TRUNCATE TABLE pages, sections")

    op.drop_index("ix_pages_search_tsv", table_name="pages", postgresql_using="gin")
    op.drop_column("pages", "search_tsv")
    op.drop_column("pages", "text")
    op.add_column("pages", sa.Column("search_tsv", postgresql.TSVECTOR(), nullable=False))
    op.create_index(
        "ix_pages_search_tsv", "pages", ["search_tsv"], unique=False, postgresql_using="gin"
    )


def downgrade() -> None:
    op.execute("TRUNCATE TABLE pages, sections")

    op.drop_index("ix_pages_search_tsv", table_name="pages", postgresql_using="gin")
    op.drop_column("pages", "search_tsv")
    op.add_column("pages", sa.Column("text", sa.Text(), nullable=False))
    op.add_column(
        "pages",
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', arabic_normalize(text))", persisted=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_pages_search_tsv", "pages", ["search_tsv"], unique=False, postgresql_using="gin"
    )
