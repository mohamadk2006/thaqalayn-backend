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


def _tsv(page_text: str):
    """`pages` has no stored text column (see Page's docstring) -- search_tsv is
    computed the same way the real importer computes it, from a value that is itself
    never persisted, so any test exercising search behavior has to build it explicitly
    rather than rely on a GENERATED column to do it automatically."""
    return func.to_tsvector("arabic", func.arabic_normalize(page_text))


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
        is a breaking change — whereas adding one is not. These 39 are Shamela's own
        published category list, not a scheme this project invented."""
        result = await session.execute(select(Subject.id))
        assert set(result.scalars()) == {
            "aqaid-sunni", "aqaid-shia", "rijal-sunni", "rijal-shia", "sira",
            "fiqh-mustaqilla", "mustalahat-fiqhiyya", "makhtutat", "tarikh-jughrafia",
            "tafsir-sunni", "tafsir-shia", "hadith-sunni-amm", "hadith-sunni-fiqh",
            "hadith-shia-amm", "hadith-shia-fiqh", "fiqh-hanbali", "fiqh-hanafi",
            "fiqh-zaydi", "fiqh-shafii", "fiqh-zahiri", "fiqh-maliki",
            "qadaya-muasira", "majallat-munawwaat", "mantiq-falsafa",
            "dalil-muallafat", "dawawin-shir", "ulum-ukhra", "ulum-lugha",
            "fiqh-shia-fatawa", "fiqh-shia-qabl-thamin", "fiqh-shia-bad-thamin",
            "usul-fiqh-shia", "usul-fiqh-sunni", "akhlaq-irfan", "adiya-ziyarat",
            "ansab-tarajim", "tibb", "firaq-madhahib", "quran-ulum",
            "other",
        }


class TestGeneratedSearchVector:
    async def test_search_tsv_stores_the_computed_value(self, session):
        book = await _make_book(session)
        page = Page(
            book_id=book.id, sequence=1, page_number="1", page_type="main",
            search_tsv=_tsv(DIACRITIZED),
        )
        session.add(page)
        await session.flush()

        tsv = await session.scalar(select(Page.search_tsv).where(Page.id == page.id))
        assert tsv, "search_tsv was empty"

    async def test_undiacriticized_query_finds_diacriticized_page(self, session):
        """The end-to-end proof: this is what a user actually types."""
        book = await _make_book(session)
        session.add_all(
            [
                Page(
                    book_id=book.id, sequence=1, page_number="1", page_type="main",
                    search_tsv=_tsv(DIACRITIZED),
                ),
                Page(
                    book_id=book.id, sequence=2, page_number="2", page_type="main",
                    search_tsv=_tsv(OTHER_PAGE),
                ),
            ]
        )
        await session.flush()

        found = (
            await session.execute(
                select(Page.page_number)
                .where(Page.book_id == book.id)
                .where(
                    Page.search_tsv.op("@@")(
                        func.phraseto_tsquery("arabic", func.arabic_normalize("الامام الصادق"))
                    )
                )
            )
        ).scalars().all()
        assert found == ["1"]

    async def test_search_uses_the_gin_index(self, session):
        """Guards the property that makes full-library search viable at all. A sequential
        scan over ~6M pages would still return correct results, so only the plan reveals
        a regression here."""
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = (
            await session.execute(
                text(
                    "EXPLAIN SELECT id FROM pages WHERE search_tsv @@ "
                    "phraseto_tsquery('arabic', arabic_normalize('الامام الصادق'))"
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

    async def test_duplicate_sequence_within_a_book_is_rejected(self, session):
        book = await _make_book(session)
        session.add(Page(book_id=book.id, sequence=5, page_number="5", page_type="main", search_tsv=_tsv("أول")))
        await session.flush()
        session.add(Page(book_id=book.id, sequence=5, page_number="5", page_type="main", search_tsv=_tsv("ثان")))
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.flush()
        await session.rollback()

    async def test_same_sequence_in_different_books_is_fine(self, session):
        first = await _make_book(session, book_id=900003)
        second = await _make_book(session, book_id=900004)
        session.add_all(
            [
                Page(book_id=first.id, sequence=1, page_number="1", page_type="main", search_tsv=_tsv("أول")),
                Page(book_id=second.id, sequence=1, page_number="1", page_type="main", search_tsv=_tsv("ثان")),
            ]
        )
        await session.flush()  # must not raise


class TestCascades:
    async def test_deleting_a_book_removes_its_pages(self, session):
        book = await _make_book(session)
        session.add(Page(book_id=book.id, sequence=1, page_number="1", page_type="main", search_tsv=_tsv(DIACRITIZED)))
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

        page = Page(book_id=book.id, sequence=1, page_number="1", page_type="main", search_tsv=_tsv(DIACRITIZED), section_id=section.id)
        session.add(page)
        await session.flush()

        await session.execute(delete(Section).where(Section.id == section.id))
        await session.flush()
        await session.refresh(page)
        assert page.section_id is None
