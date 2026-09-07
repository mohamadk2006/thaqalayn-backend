"""The library schema.

Shaped by the full survey of all 18,831 source files (scripts/validate/scan_sources.py),
not by guesses from samples. The numbers quoted in comments below come from that scan.

Two structural decisions worth stating up front:

**`books` is a volume, `works` is a title.** Shamela splits every multi-volume work into
one file per جزء, so the 18,798 usable sources collapse to 9,045 distinct works, 2,307 of
them multi-volume (بحار الأنوار alone is 110 files). A book is what gets downloaded; a
work is what gets browsed.

**NOT NULL columns carry server-side defaults, not just Python-side ones.** The bulk
importer writes ~18,800 books with raw INSERT/COPY rather than the ORM, for speed —
and a Python-side `default=` is invisible to those. Every NOT NULL column with a default
therefore declares `server_default` too.

**The search unit is the page, not the paragraph.** Paragraph granularity would mean ~76M
rows; page granularity is ~6.3M for the same text. It also matches what a pre-download
search result needs to say — book, section, page, snippet — and lets page text cross
PostgreSQL's 2 KB TOAST threshold so it compresses on disk, which paragraph rows
(~150 chars) never would. The app already does paragraph-level search locally over
downloaded books; the server does not need to duplicate that.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Language(Base):
    """ar and fa today. Persian is ~3,700 of 18,798 books — a fifth of the library, not
    an edge case."""

    __tablename__ = "languages"

    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)


class Author(Base):
    __tablename__ = "authors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Normalized once at write time so author lookup and the work-grouping heuristic can
    # both match on it without calling arabic_normalize() on every row at query time.
    name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    # Free text on purpose: 'سنة الوفاة' is 100% populated but is not always a number —
    # 'معاصر' (contemporary) is common. The parsed year is stored separately when it parses.
    death_label: Mapped[str | None] = mapped_column(String(64))
    death_year_hijri: Mapped[int | None] = mapped_column(Integer)

    books: Mapped[list[Book]] = relationship(back_populates="author")

    __table_args__ = (
        # NULLS NOT DISTINCT: without it, PostgreSQL treats every NULL death_label as
        # distinct from every other, so ON CONFLICT (name_norm, death_label) silently
        # fails to match for any author with no recorded death date — creating a fresh
        # duplicate author (and, downstream, a fresh duplicate work) on every import of
        # their books. Confirmed with a real test case: an author with no سنة الوفاة
        # value produced two author rows and two works instead of one of each.
        UniqueConstraint(
            "name_norm", "death_label", name="uq_authors_name_death",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_authors_name_norm", "name_norm"),
    )


class Subject(Base):
    """Shamela's own published 39-category list — not a scheme we invented. Distinct
    from the raw Shamela collection string, which is preserved separately on
    ShamelaCollection. This is the single classification dimension: there is no
    separate tradition/madhhab/format any more, because most of that distinction is
    already encoded directly in which of the 39 a book falls under (e.g. فقه المذهب
    الحنبلي already says fiqh + hanbali; مصادر العقائد عند السنيين already says
    aqaid + sunni)."""

    __tablename__ = "subjects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # stable slug
    title: Mapped[str] = mapped_column(Text, nullable=False)       # Arabic display name
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class ShamelaCollection(Base):
    """One row per distinct `< مجموعة >` string in the sources, mapped to a subject.

    The scan found **530 distinct raw values** for the 39 real categories: orthographic
    variants (عربى vs عربي), separator variants (parentheses vs. a dash vs. "قسم"),
    appended language suffixes, and compound/stacked values. subject_id is NULL where
    the raw string genuinely doesn't say enough (e.g. a bare "مصادر الحديث" with no
    سنة/شيعة marker, when every one of the 39 hadith categories is tradition-specific)
    — left unclassified rather than guessed, per an explicit decision.

    Keeping the raw string verbatim means a mapping mistake is always recoverable without
    re-importing, and `normalized` is what the importer actually joins on.
    """

    __tablename__ = "shamela_collections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    normalized: Mapped[str] = mapped_column(Text, nullable=False)

    subject_id: Mapped[str | None] = mapped_column(ForeignKey("subjects.id"))
    # Shamela appends '، فارسى' / '، عربى' to many collection names; that suffix is a
    # usable language signal, though the per-file body marker is authoritative.
    language_hint: Mapped[str | None] = mapped_column(ForeignKey("languages.code"))

    book_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    __table_args__ = (Index("ix_shamela_collections_normalized", "normalized"),)


class Work(Base):
    """A title, independent of how many physical volumes it was split into."""

    __tablename__ = "works"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_norm: Mapped[str] = mapped_column(Text, nullable=False)

    author_id: Mapped[int | None] = mapped_column(ForeignKey("authors.id"))
    subject_id: Mapped[str | None] = mapped_column(ForeignKey("subjects.id"))
    language_code: Mapped[str | None] = mapped_column(ForeignKey("languages.code"))

    volume_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    total_content_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    # A curated "الكتب المختارة" set, toggled from the admin panel. Additive to
    # subject_id, not a replacement -- a featured work keeps browsing under its real
    # category and also shows up here.
    is_featured: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )

    # Set by the importer when the (title, author) grouping heuristic produced something
    # suspicious — a volume sequence with gaps or duplicates. Surfaces a review list
    # instead of silently trusting a heuristic across 9,045 works.
    grouping_warning: Mapped[str | None] = mapped_column(Text)

    author: Mapped[Author | None] = relationship()
    books: Mapped[list[Book]] = relationship(back_populates="work", order_by="Book.volume")

    __table_args__ = (
        UniqueConstraint(
            "title_norm", "author_id", name="uq_works_title_author",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_works_title_norm", "title_norm"),
        Index("ix_works_author", "author_id"),
        Index("ix_works_subject", "subject_id"),
    )


class Book(Base):
    """One source file = one volume = one downloadable unit.

    `id` is the Shamela ID, taken from the source filename (1.abx … 18984.abx). The files
    carry no ID tag internally, so the filename is the only stable identifier the corpus
    provides — and reusing it keeps PostgreSQL, the JSON file, the cover, the search index,
    and the download URL all keyed on one number.
    """

    __tablename__ = "books"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)

    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), nullable=False)
    # Nullable because only 65.3% of source files carry a جزء tag. NULL means "not part of
    # a numbered series" and must not be rendered as 'الجزء 1'.
    volume: Mapped[int | None] = mapped_column(Integer)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_norm: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("authors.id"))
    language_code: Mapped[str | None] = mapped_column(ForeignKey("languages.code"))
    collection_id: Mapped[int | None] = mapped_column(ForeignKey("shamela_collections.id"))

    # Bibliographic metadata, with observed fill rates from the full scan.
    publisher: Mapped[str | None] = mapped_column(Text)        # الناشر     94.3%
    edition: Mapped[str | None] = mapped_column(Text)          # طبعة       83.7%
    published_year: Mapped[str | None] = mapped_column(Text)   # سنة الطبع  81.4%
    printer: Mapped[str | None] = mapped_column(Text)          # مطبعة      18.4%
    editor: Mapped[str | None] = mapped_column(Text)           # تحقيق      36.1%
    isbn: Mapped[str | None] = mapped_column(Text)             # ردمك        8.9%
    source_pdf: Mapped[str | None] = mapped_column(Text)       # ملف مرفق   81.2%
    identity_notes: Mapped[str | None] = mapped_column(Text)   # ملاحظات الهوية 25.4%
    # No description exists anywhere in the sources — this stays NULL unless someone
    # writes one. The iOS client must treat it as optional.
    description: Mapped[str | None] = mapped_column(Text)
    is_verified: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )  # موثوق

    # ── File references ──────────────────────────────────────────────────────────
    # Relative to BOOKS_ROOT / COVERS_ROOT, never absolute: the same rows must work on
    # macOS and on the Linux VPS, and the download endpoint resolves and verifies that the
    # result stays inside the configured root.
    content_path: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    content_bytes: Mapped[int | None] = mapped_column(BigInteger)
    download_bytes: Mapped[int | None] = mapped_column(BigInteger)  # gzipped transfer size
    cover_path: Mapped[str | None] = mapped_column(Text)

    # Bumped when the converted content changes, so the client can detect a stale download.
    content_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    is_published: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )

    # page_first/page_last: the printed number of the book's first/last *main* page
    # (front matter's "0.N" labels are never numeric, so they're excluded — a book that
    # is entirely front matter, none seen in the real corpus, would leave both NULL).
    # Exposed to iOS as BookOut.pageFirst/pageLast; still plain ints since both are
    # always genuine printed numbers, unlike Page.page_number which must be a string.
    page_first: Mapped[int | None] = mapped_column(Integer)
    page_last: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    # Total block count across all pages (text + heading + footnotes) — the closest v2
    # analogue of v1's paragraph count, kept under the same name/field since it served
    # the same "how much content is in this book" role and nothing consumes its exact
    # definition today. Exposed to iOS as BookOut.paragraphCount.
    paragraph_count: Mapped[int | None] = mapped_column(Integer)
    section_count: Mapped[int | None] = mapped_column(Integer)

    imported_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    work: Mapped[Work] = relationship(back_populates="books")
    author: Mapped[Author | None] = relationship(back_populates="books")
    sections: Mapped[list[Section]] = relationship(
        back_populates="book", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("volume IS NULL OR volume > 0", name="ck_books_volume_positive"),
        Index("ix_books_work", "work_id"),
        Index("ix_books_author", "author_id"),
        Index("ix_books_language", "language_code"),
        Index("ix_books_collection", "collection_id"),
        Index("ix_books_title_norm", "title_norm"),
        # Partial index: catalog and search queries always filter to published books, and
        # this stays small while unpublished rows accumulate during a long import.
        Index("ix_books_published", "is_published", postgresql_where="is_published"),
    )


class Section(Base):
    """A heading from the source's فهرس الموضوعات index — one row per v2 `toc[]` entry.

    Nullable relationships everywhere it touches pages, because some Shamela exports have
    no headings at all — 3 of the 10 originally sampled books had exactly one synthetic
    section. Search results for those books can report a page number but no section title.

    `page_start_sequence`/`page_end_sequence` are `Page.sequence` values, not printed page
    numbers — v2 page numbers are strings and not unique within a book (see Page), so they
    cannot anchor a range. A section's range is "from its own page's sequence to the
    sequence right before the next TOC entry's page" (or the book's last page, for the
    final entry) — sequence is always present, always unique per book, and always
    orderable, which is exactly what a range needs.
    """

    __tablename__ = "sections"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_norm: Mapped[str] = mapped_column(Text, nullable=False)
    page_start_sequence: Mapped[int | None] = mapped_column(Integer)
    page_end_sequence: Mapped[int | None] = mapped_column(Integer)

    book: Mapped[Book] = relationship(back_populates="sections")

    __table_args__ = (
        UniqueConstraint("book_id", "ord", name="uq_sections_book_ord"),
        Index("ix_sections_book", "book_id"),
    )


class Page(Base):
    """The search unit: one v2 page (`pages[]` entry) of one book.

    Deliberately does NOT store the page's own text at all -- only the search index
    computed from it. The text already lives once, on disk, in the book's own JSON file
    (which the app downloads directly); storing a second full copy in Postgres, on top of
    the GIN index over it, was found to roughly double this table's footprint for a VPS
    that doesn't have room to spare. `search_tsv` is populated directly at import time
    from the source text, passed as a bind parameter and never persisted as a column --
    only Postgres's own computed tsvector output is stored.

    The real cost of this: a search hit's snippet can no longer be cut straight from a
    `text` column. search_service.py re-opens the book's JSON file for each of the
    (typically <= `limit`) hits actually returned, reconstructs that one page's text the
    same way paging.py does at import time, and extracts the snippet from that -- extra
    per-request I/O, bounded by the page size, not the corpus size, in exchange for not
    duplicating ~30 GB of already-downloadable text inside the database.

    `sequence`, not `page_number`, is the identity column: v2's own `pageNumber` is a
    *string* ("0.1".."0.n" for front matter, the printed number as text for main pages)
    and the schema explicitly does not guarantee it's unique within a book, so it cannot
    carry a uniqueness constraint or serve as a stable join key the way v1's integer
    `page_no` did. `sequence` is v2's own 1..N page ordinal — always present, always
    unique per book, always orderable — and is what `Section` ranges and any future
    deep-link addressing should use. `page_number` survives purely as the display string.
    """

    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[str] = mapped_column(Text, nullable=False)
    page_type: Mapped[str] = mapped_column(Text, nullable=False)  # frontMatter | main
    is_blank: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    section_id: Mapped[int | None] = mapped_column(ForeignKey("sections.id", ondelete="SET NULL"))

    # Character offsets of each v2 *block* within the page's (unstored) combined text, as
    # [{"id": ..., "start": ...}]. Still meaningful without a stored `text` column here --
    # it describes structure, and a client resolves it against the same page's text once
    # downloaded. (Was paragraph_offsets, keyed on v1 paragraph ids — v2 has no paragraph
    # concept, only blocks.)
    block_offsets: Mapped[dict | None] = mapped_column(JSONB)

    # NOT a GENERATED column: that would require a stored source column to generate from,
    # which is exactly what this table doesn't have. The importer computes this directly
    # -- to_tsvector('arabic', arabic_normalize(:text)) -- passing the page text as a bind
    # parameter that is never itself persisted. 'arabic' (Postgres's built-in Snowball
    # stemmer), not 'simple': measured on a real sample, stemming shrunk the index by
    # ~42% and means a query for one inflected form of a word also finds other forms of
    # it, which 'simple' never did.
    search_tsv: Mapped[str] = mapped_column(TSVECTOR, nullable=False)

    __table_args__ = (
        CheckConstraint("page_type IN ('frontMatter','main')", name="ck_pages_page_type"),
        UniqueConstraint("book_id", "sequence", name="uq_pages_book_sequence"),
        Index("ix_pages_book", "book_id"),
        Index("ix_pages_section", "section_id"),
        # GIN over the tsvector is what makes full-library search feasible. Build it AFTER
        # the bulk load: maintaining a GIN index during a 6M-row insert is dramatically
        # slower than creating it once at the end.
        Index("ix_pages_search_tsv", "search_tsv", postgresql_using="gin"),
    )


class ImportLog(Base):
    """One row per book per import attempt.

    The importer must be restartable across ~18,800 files, so failures are recorded rather
    than raised: one malformed book cannot be allowed to stop the run. `content_sha256`
    is what makes re-runs idempotent — unchanged files are skipped.
    """

    __tablename__ = "import_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    book_id: Mapped[int | None] = mapped_column(Integer)
    source_file: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok | skipped | failed
    stage: Mapped[str | None] = mapped_column(String(32))            # validate | metadata | pages
    message: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("status IN ('ok','skipped','failed')", name="ck_import_log_status"),
        Index("ix_import_log_run", "run_id"),
        Index("ix_import_log_status", "status"),
        Index("ix_import_log_book", "book_id"),
    )
