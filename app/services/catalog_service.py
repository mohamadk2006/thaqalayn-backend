"""Catalog queries: works, books, authors, subjects, languages.

Deliberately the only module that builds SQL for these reads. Routers call functions
here and shape the result into response schemas — they never touch SQLAlchemy `select()`
directly. That separation is what keeps the API layer swappable later (e.g. adding a
cache, or moving search to a different engine) without router changes.
"""

from __future__ import annotations

from sqlalchemy import Select, and_, case, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.models import Author, Book, Language, Library, Subject, Work
from app.services.arabic import normalize
from app.schemas.catalog import (
    AuthorOut,
    BookOut,
    CategoryOut,
    LanguageOut,
    LibraryOut,
    SubjectOut,
    WorkDetailOut,
    WorkOut,
)


# Persian names are written in Arabic without Persian letters: "الكلبايكاني" for
# "الگلپايگاني". Title and author search treats each Persian letter as its Arabic
# stand-in, on both sides. Only here: in page text, Persian "گل" (flower) must not match
# the ubiquitous Arabic "كل".
_PERSIAN, _ARABIC = "گپچژ", "كبجز"
_PERSIAN_FOLD = str.maketrans(_PERSIAN, _ARABIC)


def _folded(column):
    return func.translate(column, _PERSIAN, _ARABIC)


def _query_words(q: str | None) -> list[str]:
    """The query's words in the same folded form as title_norm/name_norm (diacritics,
    alef/hamza forms, taa marbuta...), so "الكافى" finds "الكافي" and "أصول" finds "اصول"."""
    return normalize(q or "").translate(_PERSIAN_FOLD).split()[:10]


def _matches_title_or_author(words: list[str]):
    """Every word must appear somewhere in the work's title, one of its published
    volumes' titles, or its author's name -- so "الكافي الكليني" finds al-Kafi by
    al-Kulayni, and one word alone matches either."""
    conditions = []
    for w in words:
        conditions.append(or_(
            _folded(Work.title_norm).contains(w, autoescape=True),
            Work.author_id.in_(select(Author.id).where(_folded(Author.name_norm).contains(w, autoescape=True))),
            Work.id.in_(select(Book.work_id).where(
                Book.is_published.is_(True), _folded(Book.title_norm).contains(w, autoescape=True))),
        ))
    return conditions


def _subjects_out(work: Work | None) -> list[SubjectOut]:
    if work is None:
        return []
    return [SubjectOut(id=s.id, title=s.title) for s in work.subjects]


def _libraries_out(work: Work | None) -> list[LibraryOut]:
    if work is None:
        return []
    return [
        LibraryOut(id=str(lib.id), title=lib.title,
                    parentId=str(lib.parent_id) if lib.parent_id else None)
        for lib in work.libraries
    ]


def _book_out(book: Book, work_title: str, collection_raw: str | None) -> BookOut:
    return BookOut(
        bookId=str(book.id),
        workId=str(book.work_id),
        workTitle=work_title,
        volume=book.volume,
        title=book.title,
        author=book.author.name if book.author else "",
        authorDeath=book.author.death_label if book.author else None,
        description=book.description,
        subjects=_subjects_out(book.work),
        libraries=_libraries_out(book.work),
        language=book.language_code,
        publisher=book.publisher,
        shamelaCollection=collection_raw,
        pageFirst=book.page_first,
        pageLast=book.page_last,
        paragraphCount=book.paragraph_count,
        sizeBytes=book.content_bytes or 0,
        downloadBytes=book.content_bytes or 0,  # placeholder — see BookOut docstring
        contentVersion=book.content_version,
    )


def _work_out(work: Work, volume_count: int, total_bytes: int,
              collection_raw: str | None) -> WorkOut:
    return WorkOut(
        workId=str(work.id),
        title=work.title,
        author=work.author.name if work.author else "",
        authorDeath=work.author.death_label if work.author else None,
        subjects=_subjects_out(work),
        libraries=_libraries_out(work),
        language=work.language_code,
        volumeCount=volume_count,
        totalSizeBytes=total_bytes,
        shamelaCollection=collection_raw,
        isFeatured=work.is_featured,
    )


async def list_works(
    session: AsyncSession,
    *,
    page: int,
    limit: int,
    subject_id: str | None = None,
    library_id: int | None = None,
    language: str | None = None,
    author_id: int | None = None,
    featured: bool | None = None,
    q: str | None = None,
) -> tuple[list[WorkOut], int]:
    """Paginated, filterable list of works. Each row aggregates its published volumes'
    count and total size — a work with zero published volumes is excluded, since it has
    nothing a client could download. `q` searches titles and author names; results then
    come best match first (the exact title, a title starting with the query, every word
    in the title, then the rest), alphabetically within each."""
    published = Book.is_published.is_(True)

    # Aggregate first, in its own subquery, then join Work back onto the aggregated
    # result: mixing joinedload's extra SELECT columns into a query that also has
    # GROUP BY forces those columns into the GROUP BY too, which PostgreSQL rejects for
    # anything not functionally dependent on the primary key. Aggregating separately
    # sidesteps that entirely, and lets Work.author still be eager-loaded normally on
    # the (now ungrouped) outer query.
    agg: Select = (
        select(
            Book.work_id.label("work_id"),
            func.count(Book.id).label("volume_count"),
            func.coalesce(func.sum(Book.content_bytes), 0).label("total_bytes"),
        )
        .where(published)
        .group_by(Book.work_id)
        .subquery()
    )

    query = (
        select(Work, agg.c.volume_count, agg.c.total_bytes)
        .join(agg, agg.c.work_id == Work.id)
        .options(*_work_load_options())
    )
    if subject_id:
        query = query.where(Work.subjects.any(Subject.id == subject_id))
    if library_id:
        query = query.where(Work.libraries.any(Library.id == library_id))
    if language:
        query = query.where(Work.language_code == language)
    if author_id:
        query = query.where(Work.author_id == author_id)
    if featured:
        query = query.where(Work.is_featured.is_(True))
    words = _query_words(q)
    if words:
        query = query.where(*_matches_title_or_author(words))

    total = await session.scalar(select(func.count()).select_from(query.subquery()))

    # The featured shelf has an explicit, admin-curated order; everywhere else stays
    # alphabetical, since featured_sort_order is meaningless outside that set.
    order = (
        (Work.featured_sort_order, Work.title_norm) if featured else (Work.title_norm,)
    )
    if words:
        phrase = " ".join(words)
        title = _folded(Work.title_norm)
        rank = case(
            (title == phrase, 0),
            (title.startswith(phrase, autoescape=True), 1),
            (title.contains(phrase, autoescape=True), 2),
            (and_(*[title.contains(w, autoescape=True) for w in words]), 3),
            else_=4,  # found through a volume's title or the author's name
        )
        order = (rank, func.length(Work.title_norm), Work.title_norm)
    rows = (
        await session.execute(
            query.order_by(*order).offset((page - 1) * limit).limit(limit)
        )
    ).all()

    work_ids = [w.id for w, _, _ in rows]
    collections = await _work_collection_raw(session, work_ids)

    items = [_work_out(w, vc, tb, collections.get(w.id)) for w, vc, tb in rows]
    return items, total or 0


async def get_work(session: AsyncSession, work_id: int) -> WorkDetailOut | None:
    work = await session.get(Work, work_id, options=_work_load_options())
    if work is None:
        return None

    books = (
        await session.execute(
            select(Book)
            .where(Book.work_id == work_id, Book.is_published.is_(True))
            .order_by(Book.volume.nulls_first())
            .options(*_book_load_options())
        )
    ).scalars().all()

    collections = await _book_collection_raw(session, [b.id for b in books])
    work_collection = next(iter(collections.values()), None)

    # Every volume of a work shares the work's own subjects (a volume has no separate
    # classification of its own), so _book_out's work.subjects lookup already gives each
    # one the right list here without any per-book batch-loading.
    volumes = [_book_out(b, work.title, collections.get(b.id)) for b in books]
    total_bytes = sum(b.content_bytes or 0 for b in books)

    return WorkDetailOut(
        **_work_out(work, len(books), total_bytes, work_collection).model_dump(),
        volumes=volumes,
    )


async def list_books(
    session: AsyncSession,
    *,
    page: int,
    limit: int,
    subject_id: str | None = None,
    library_id: int | None = None,
    language: str | None = None,
    author_id: int | None = None,
    work_id: int | None = None,
) -> tuple[list[BookOut], int]:
    conditions = [Book.is_published.is_(True)]
    if work_id:
        conditions.append(Book.work_id == work_id)
    if language:
        conditions.append(Book.language_code == language)
    if author_id:
        conditions.append(Book.author_id == author_id)

    query = (
        select(Book)
        .join(Work, Work.id == Book.work_id)
        .where(*conditions)
        .options(*_book_load_options())
    )
    if subject_id:
        query = query.where(Work.subjects.any(Subject.id == subject_id))
    if library_id:
        query = query.where(Work.libraries.any(Library.id == library_id))

    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await session.execute(
            query.order_by(Book.title_norm, Book.volume.nulls_first())
            .offset((page - 1) * limit)
            .limit(limit)
        )
    ).scalars().all()

    collections = await _book_collection_raw(session, [b.id for b in rows])
    items = [
        _book_out(b, b.work.title if b.work else "", collections.get(b.id)) for b in rows
    ]
    return items, total or 0


async def get_book(session: AsyncSession, book_id: int) -> BookOut | None:
    book = await session.get(Book, book_id, options=_book_load_options())
    if book is None or not book.is_published:
        return None
    collections = await _book_collection_raw(session, [book.id])
    return _book_out(book, book.work.title if book.work else "", collections.get(book.id))


async def get_books_by_ids(session: AsyncSession, book_ids: list[int]) -> dict[int, BookOut]:
    """Published books among `book_ids`, in the exact shape GET /api/books returns them.
    An id that is missing from the result does not exist or is not published."""
    if not book_ids:
        return {}
    rows = (
        await session.execute(
            select(Book)
            .where(Book.id.in_(book_ids), Book.is_published.is_(True))
            .options(*_book_load_options())
        )
    ).scalars().all()
    collections = await _book_collection_raw(session, [b.id for b in rows])
    return {
        b.id: _book_out(b, b.work.title if b.work else "", collections.get(b.id))
        for b in rows
    }


async def list_authors(session: AsyncSession, q: str | None = None) -> list[AuthorOut]:
    """All authors by name; with `q`, only those whose name holds every word of it."""
    query = select(Author)
    words = _query_words(q)
    for w in words:
        query = query.where(_folded(Author.name_norm).contains(w, autoescape=True))
    if words:
        phrase = " ".join(words)
        query = query.order_by(case((_folded(Author.name_norm).startswith(phrase, autoescape=True), 0), else_=1),
                               Author.name_norm)
    else:
        query = query.order_by(Author.name_norm)
    rows = (await session.execute(query)).scalars().all()
    return [AuthorOut(id=str(a.id), name=a.name, deathLabel=a.death_label) for a in rows]


async def list_subjects(session: AsyncSession) -> list[CategoryOut]:
    rows = (await session.execute(select(Subject).order_by(Subject.sort_order))).scalars().all()
    pins: dict[str, list[str]] = {}
    for r in (await session.execute(
        text("SELECT subject_id, book_id FROM subject_pinned_books ORDER BY subject_id, position")
    )).all():
        pins.setdefault(r.subject_id, []).append(str(r.book_id))
    return [
        CategoryOut(id=s.id, title=s.title, order=s.sort_order, section=s.section,
                    pinnedBookIds=pins.get(s.id, []))
        for s in rows
    ]


async def list_libraries(session: AsyncSession) -> list[LibraryOut]:
    """Flat list, ordered so a parent always precedes its own children (parents sort
    before children at the same sort_order, since NULLS come first by default) -- the
    client groups by parentId to build whatever tree it wants to display."""
    rows = (
        await session.execute(
            select(Library).order_by(Library.parent_id.nulls_first(), Library.sort_order)
        )
    ).scalars().all()
    return [
        LibraryOut(id=str(lib.id), title=lib.title,
                    parentId=str(lib.parent_id) if lib.parent_id else None)
        for lib in rows
    ]


async def list_languages(session: AsyncSession) -> list[LanguageOut]:
    rows = (await session.execute(select(Language).order_by(Language.code))).scalars().all()
    return [LanguageOut(code=lang.code, name=lang.name) for lang in rows]


# ── internals ──────────────────────────────────────────────────────────────────────

def _work_load_options():
    # subjects/libraries are both to-many: selectinload issues its own separate query
    # instead of joining, so it never multiplies rows in a query that also paginates
    # with LIMIT/OFFSET the way a joinedload on a collection would.
    return [joinedload(Work.author), selectinload(Work.subjects), selectinload(Work.libraries)]


def _book_load_options():
    """Every field _book_out reads off `book.author` or `book.work` (including
    `book.work.subjects`/`book.work.libraries`) must be eager-loaded here — there is no
    other query path that populates them."""
    return [
        joinedload(Book.author),
        joinedload(Book.work).selectinload(Work.subjects),
        joinedload(Book.work).selectinload(Work.libraries),
    ]


async def _book_collection_raw(session: AsyncSession, book_ids: list[int]) -> dict[int, str]:
    if not book_ids:
        return {}
    from app.models.library import ShamelaCollection

    rows = (
        await session.execute(
            select(Book.id, ShamelaCollection.raw)
            .join(ShamelaCollection, ShamelaCollection.id == Book.collection_id)
            .where(Book.id.in_(book_ids))
        )
    ).all()
    return dict(rows)


async def _work_collection_raw(session: AsyncSession, work_ids: list[int]) -> dict[int, str]:
    """One representative collection string per work — its lowest-volume book's, since a
    work's volumes usually share a collection and 'first volume' is a stable pick."""
    if not work_ids:
        return {}

    book_ids = (
        await session.execute(
            select(func.min(Book.id))
            .where(Book.work_id.in_(work_ids))
            .group_by(Book.work_id)
        )
    ).scalars().all()
    per_book = await _book_collection_raw(session, list(book_ids))
    work_of_book = dict(
        (await session.execute(select(Book.id, Book.work_id).where(Book.id.in_(book_ids)))).all()
    )
    return {work_of_book[bid]: raw for bid, raw in per_book.items() if bid in work_of_book}
