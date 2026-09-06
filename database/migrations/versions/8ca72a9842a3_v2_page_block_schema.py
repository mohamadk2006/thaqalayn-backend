"""v2 page/block schema for pages and sections

Revision ID: 8ca72a9842a3
Revises: 572df4ce6f73
Create Date: 2026-09-06

Replaces the v1 chapters/sections/paragraphs shape's assumptions baked into `pages` and
`sections`: an integer, book-unique `page_no` and integer paragraph-based section ranges.
v2's own page numbers are strings ("0.1".."0.n" for front matter) and are explicitly not
guaranteed unique within a book, so they can no longer be the join/uniqueness key — v2's
own page `sequence` (always present, always unique per book, always orderable) takes over
that role, and the printed number survives purely as a display string.

Both tables are dropped and recreated rather than altered in place: their rows are a
derived search index over the converted book JSON, not a source of truth, and every row
is about to be replaced wholesale by the v2 corpus reimport regardless of whether the
column shapes changed underneath it.
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8ca72a9842a3"
down_revision: str | Sequence[str] | None = "572df4ce6f73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_pages_search_tsv", table_name="pages", postgresql_using="gin")
    op.drop_index("ix_pages_section", table_name="pages")
    op.drop_index("ix_pages_book", table_name="pages")
    op.drop_table("pages")
    op.drop_index("ix_sections_book", table_name="sections")
    op.drop_table("sections")

    op.create_table(
        "sections",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("book_id", sa.Integer(), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_norm", sa.Text(), nullable=False),
        sa.Column("page_start_sequence", sa.Integer(), nullable=True),
        sa.Column("page_end_sequence", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("book_id", "ord", name="uq_sections_book_ord"),
    )
    op.create_index("ix_sections_book", "sections", ["book_id"], unique=False)

    op.create_table(
        "pages",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("book_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Text(), nullable=False),
        sa.Column("page_type", sa.Text(), nullable=False),
        sa.Column(
            "is_blank", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("section_id", sa.BigInteger(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("block_offsets", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', arabic_normalize(text))", persisted=True),
            nullable=False,
        ),
        sa.CheckConstraint("page_type IN ('frontMatter','main')", name="ck_pages_page_type"),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["section_id"], ["sections.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("book_id", "sequence", name="uq_pages_book_sequence"),
    )
    op.create_index("ix_pages_book", "pages", ["book_id"], unique=False)
    op.create_index("ix_pages_section", "pages", ["section_id"], unique=False)
    op.create_index(
        "ix_pages_search_tsv", "pages", ["search_tsv"], unique=False, postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_pages_search_tsv", table_name="pages", postgresql_using="gin")
    op.drop_index("ix_pages_section", table_name="pages")
    op.drop_index("ix_pages_book", table_name="pages")
    op.drop_table("pages")
    op.drop_index("ix_sections_book", table_name="sections")
    op.drop_table("sections")

    op.create_table(
        "sections",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("book_id", sa.Integer(), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_norm", sa.Text(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("book_id", "ord", name="uq_sections_book_ord"),
    )
    op.create_index("ix_sections_book", "sections", ["book_id"], unique=False)

    op.create_table(
        "pages",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("book_id", sa.Integer(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("section_id", sa.BigInteger(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("paragraph_offsets", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', arabic_normalize(text))", persisted=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["section_id"], ["sections.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("book_id", "page_no", name="uq_pages_book_page"),
    )
    op.create_index("ix_pages_book", "pages", ["book_id"], unique=False)
    op.create_index("ix_pages_section", "pages", ["section_id"], unique=False)
    op.create_index(
        "ix_pages_search_tsv", "pages", ["search_tsv"], unique=False, postgresql_using="gin"
    )
