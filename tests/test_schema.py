"""Schema tests: the structure behaves as the design intends, on real Arabic text.

These are deliberately behavioural rather than structural — asserting that a column
exists proves very little, whereas asserting that an undiacriticized query finds a
diacriticized page proves the generated column, the normalizer, the GIN index, and the
text search configuration are all wired together correctly.
"""

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models import Author, Book, Language, Page, Section, Subject, Work

# A real passage from the corpus, with full tashkeel — the form actually stored.
DIACRITIZED = "قالَ الإمامُ الصادقُ عليه السلام: العلمُ نورٌ يقذفه اللهُ في قلبِ من يشاء"
OTHER_PAGE = "وقال أبو عبد الله عليه السلام: مَن كان عاقلًا كان له دِين"


async def _make_book(session, *, book_id: int = 900001, volume: int | None = 1) -> Book:
    """Get-or-create the author and work, then add a book.

    Authors are unique on (name_norm, death_label) and works on (title_norm, author_id),
    which is correct — the importer relies on exactly that to collapse 18,798 files into
    9,045 works. So a helper that builds several books has to reuse them, the same way the
    importer will.
    """
    author = await session.scalar(
        select(Author).where(Author.name_norm == "ثقه الاسلام الكليني")
    )
    if author is None:
        author = Author(
            name="ثقة الإسلام الكليني",
            name_norm="ثقه الاسلام الكليني",
            death_label="329",
        )
        session.add(author)
        await session.flush()

    work = await session.scalar(
        select(Work).where(Work.title_norm == "الكافي", Work.author_id == author.id)
    )
    if work is None:
        work = Work(title="الكافي", title_norm="الكافي", author_id=author.id, volume_count=1)
        session.add(work)
        await session.flush()

    book = Book(
        id=book_id,
        work_id=work.id,
        volume=volume,
        title="الكافي",
        title_norm="الكافي",
        author_id=author.id,
        language_code="ar",
        is_published=True,
    )
    session.add(book)
    await session.flush()
    return book


class TestSeedData:
    async def test_languages_seeded(self, session):
        result = await session.execute(select(Language.code).order_by(Language.code))
        codes = result.scalars().all()
        assert codes == ["ar", "fa"]

    async def test_subjects_seeded(self, session):
        """Asserts the slugs, not the count. Clients switch on these strings and the
        collection mapping references them as foreign keys, so a renamed or dropped slug
        is a breaking change — whereas adding one is not."""
        result = await session.execute(select(Subject.id))
        assert set(result.scalars()) == {
            "quran", "tafsir", "hadith", "rijal", "aqaid", "usul-fiqh", "fiqh",
            "rasail-amaliyya", "sira", "tarikh", "tarajim", "adiya", "akhlaq",
            "falsafa", "lugha", "faharis", "tibb", "qadaya-muasira", "munawwaat",
            "ulum-ukhra",
        }


class TestGeneratedSearchVector:
    async def test_tsvector_is_populated_automatically(self, session):
        book = await _make_book(session)
        page = Page(book_id=book.id, page_no=1, text=DIACRITIZED)
        session.add(page)
        await session.flush()

        tsv = await session.scalar(select(Page.search_tsv).where(Page.id == page.id))
        assert tsv, "GENERATED column produced an empty tsvector"

    async def test_undiacriticized_query_finds_diacriticized_page(self, session):
        """The end-to-end proof: this is what a user actually types."""
        book = await _make_book(session)
        session.add_all(
            [
                Page(book_id=book.id, page_no=1, text=DIACRITIZED),
                Page(book_id=book.id, page_no=2, text=OTHER_PAGE),
            ]
        )
        await session.flush()

        found = (
            await session.execute(
                select(Page.page_no)
                .where(Page.book_id == book.id)
                .where(
                    Page.search_tsv.op("@@")(
                        func.phraseto_tsquery("simple", func.arabic_normalize("الامام الصادق"))
                    )
                )
            )
        ).scalars().all()
        assert found == [1]

    async def test_tsvector_updates_when_text_changes(self, session):
        """A STORED generated column must track its source; a trigger-based design could
        silently drift."""
        book = await _make_book(session)
        page = Page(book_id=book.id, page_no=1, text="نص أولي")
        session.add(page)
        await session.flush()

        page.text = DIACRITIZED
        await session.flush()

        matches = await session.scalar(
            select(func.count())
            .select_from(Page)
            .where(Page.id == page.id)
            .where(
                Page.search_tsv.op("@@")(
                    func.phraseto_tsquery("simple", func.arabic_normalize("الامام الصادق"))
                )
            )
        )
        assert matches == 1

    async def test_search_uses_the_gin_index(self, session):
        """Guards the property that makes full-library search viable at all. A sequential
        scan over ~6M pages would still return correct results, so only the plan reveals
        a regression here."""
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = (
            await session.execute(
                text(
                    "EXPLAIN SELECT id FROM pages WHERE search_tsv @@ "
                    "phraseto_tsquery('simple', arabic_normalize('الامام الصادق'))"
                )
            )
        ).scalars().all()
        assert any("ix_pages_search_tsv" in line for line in plan), "\n".join(plan)


class TestConstraints:
    async def test_volume_may_be_null(self, session):
        """34.7% of source files carry no جزء tag — NULL must be legal."""
        book = await _make_book(session, volume=None)
        assert book.volume is None

    async def test_volume_zero_is_rejected(self, session):
        with pytest.raises((IntegrityError, DBAPIError)):
            await _make_book(session, book_id=900002, volume=0)
        # A failed statement aborts the PostgreSQL transaction; rolling back explicitly
        # lets the fixture tear down cleanly instead of warning.
        await session.rollback()

    async def test_duplicate_page_number_within_a_book_is_rejected(self, session):
        book = await _make_book(session)
        session.add(Page(book_id=book.id, page_no=5, text="أول"))
        await session.flush()
        session.add(Page(book_id=book.id, page_no=5, text="ثان"))
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.flush()
        await session.rollback()

    async def test_same_page_number_in_different_books_is_fine(self, session):
        first = await _make_book(session, book_id=900003)
        second = await _make_book(session, book_id=900004)
        session.add_all(
            [
                Page(book_id=first.id, page_no=1, text="أول"),
                Page(book_id=second.id, page_no=1, text="ثان"),
            ]
        )
        await session.flush()  # must not raise


class TestCascades:
    async def test_deleting_a_book_removes_its_pages(self, session):
        book = await _make_book(session)
        session.add(Page(book_id=book.id, page_no=1, text=DIACRITIZED))
        await session.flush()

        await session.execute(delete(Book).where(Book.id == book.id))
        await session.flush()

        remaining = await session.scalar(
            select(func.count()).select_from(Page).where(Page.book_id == book.id)
        )
        assert remaining == 0

    async def test_deleting_a_section_keeps_its_pages(self, session):
        """Sections are optional context, not ownership: some sources have no headings at
        all, so a page must survive losing its section."""
        book = await _make_book(session)
        section = Section(
            book_id=book.id, ord=1, title="باب العلم", title_norm="باب العلم"
        )
        session.add(section)
        await session.flush()

        page = Page(book_id=book.id, page_no=1, text=DIACRITIZED, section_id=section.id)
        session.add(page)
        await session.flush()

        await session.execute(delete(Section).where(Section.id == section.id))
        await session.flush()
        await session.refresh(page)
        assert page.section_id is None
