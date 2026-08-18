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
    Computed,
    DateTime,
    Enum,
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

# ── Controlled vocabularies ──────────────────────────────────────────────────────
# Native PostgreSQL enums rather than free text: these are small, stable sets that the
# API exposes as filters, and a typo in an import script should fail loudly rather than
# silently create a category nobody can browse to.

TraditionEnum = Enum(
    "shia", "sunni", "zaydi", "shared",
    name="tradition",
    create_type=True,
)

FormatEnum = Enum(
    "book", "manuscript", "journal", "diwan", "dictionary", "index",
    name="book_format",
    create_type=True,
)


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
        UniqueConstraint("name_norm", "death_label", name="uq_authors_name_death"),
        Index("ix_authors_name_norm", "name_norm"),
    )


class Subject(Base):
    """Our curated taxonomy — the browsable one. Distinct from the raw Shamela collection
    string, which is preserved separately on ShamelaCollection."""

    __tablename__ = "subjects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # stable slug
    title: Mapped[str] = mapped_column(Text, nullable=False)       # Arabic display name
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class ShamelaCollection(Base):
    """One row per distinct `< مجموعة >` string in the sources, mapped to our taxonomy.

    The scan found **530 distinct raw values**, not the ~39 a curated screenshot suggests.
    They carry orthographic variants (عربى vs عربي), separator variants, appended language
    suffixes, and compound values. Normalizing collapses them to 344, with a long tail of
    162 groups holding only 236 books between them.

    Keeping the raw string verbatim means a mapping mistake is always recoverable without
    re-importing, and `normalized` is what the importer actually joins on.
    """

    __tablename__ = "shamela_collections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    normalized: Mapped[str] = mapped_column(Text, nullable=False)

    subject_id: Mapped[str | None] = mapped_column(ForeignKey("subjects.id"))
    tradition: Mapped[str | None] = mapped_column(TraditionEnum)
    # Only meaningful for Sunni fiqh (حنفي/مالكي/شافعي/حنبلي/ظاهري). Free text rather than
    # an enum until the full corpus confirms the closed set.
    madhhab: Mapped[str | None] = mapped_column(String(32))
    format: Mapped[str | None] = mapped_column(FormatEnum)
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
    tradition: Mapped[str | None] = mapped_column(TraditionEnum)
    madhhab: Mapped[str | None] = mapped_column(String(32))
    format: Mapped[str | None] = mapped_column(FormatEnum)
    language_code: Mapped[str | None] = mapped_column(ForeignKey("languages.code"))

    volume_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    total_content_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    # Set by the importer when the (title, author) grouping heuristic produced something
    # suspicious — a volume sequence with gaps or duplicates. Surfaces a review list
    # instead of silently trusting a heuristic across 9,045 works.
    grouping_warning: Mapped[str | None] = mapped_column(Text)

    author: Mapped[Author | None] = relationship()
    books: Mapped[list[Book]] = relationship(back_populates="work", order_by="Book.volume")

    __table_args__ = (
        UniqueConstraint("title_norm", "author_id", name="uq_works_title_author"),
        Index("ix_works_title_norm", "title_norm"),
        Index("ix_works_author", "author_id"),
        Index("ix_works_subject", "subject_id"),
        Index("ix_works_tradition", "tradition"),
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

    page_first: Mapped[int | None] = mapped_column(Integer)
    page_last: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
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
    """A heading from the source's فهرس الموضوعات index.

    Nullable relationships everywhere it touches pages, because some Shamela exports have
    no headings at all — 3 of the 10 originally sampled books had exactly one synthetic
    section. Search results for those books can report a page number but no section title.
    """

    __tablename__ = "sections"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_norm: Mapped[str] = mapped_column(Text, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)

    book: Mapped[Book] = relationship(back_populates="sections")

    __table_args__ = (
        UniqueConstraint("book_id", "ord", name="uq_sections_book_ord"),
        Index("ix_sections_book", "book_id"),
    )


class Page(Base):
    """The search unit: one print page of one book.

    Only the **original** text is stored, with full tashkeel. The normalized form exists
    solely inside `search_tsv`, which is derived — storing a second normalized copy would
    add roughly 10 GB across the library for text nothing ever displays.

    A consequence for Milestone 6: `ts_headline` over normalized text returns a normalized
    snippet (stripped of hamza and tashkeel), which is wrong to show a reader. Snippets
    must therefore be cut from `text` here, with match offsets mapped back from normalized
    space — not taken from PostgreSQL's pre-marked headline.
    """

    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    section_id: Mapped[int | None] = mapped_column(ForeignKey("sections.id", ondelete="SET NULL"))

    text: Mapped[str] = mapped_column(Text, nullable=False)

    # Character offsets of each paragraph within `text`, as [{"id": ..., "start": ...}].
    # Lets a search hit resolve back to a paragraph for deep-linking after download,
    # without storing paragraph rows or a second copy of the text.
    paragraph_offsets: Mapped[dict | None] = mapped_column(JSONB)

    # GENERATED ... STORED: PostgreSQL maintains this on write, so the index can never
    # drift out of sync with the text the way a trigger-maintained column can. This is
    # why arabic_normalize() must be IMMUTABLE — PostgreSQL rejects anything else here.
    search_tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', arabic_normalize(text))", persisted=True),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("book_id", "page_no", name="uq_pages_book_page"),
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
