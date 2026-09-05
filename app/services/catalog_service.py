"""Catalog queries: works, books, authors, subjects, languages.

Deliberately the only module that builds SQL for these reads. Routers call functions
here and shape the result into response schemas — they never touch SQLAlchemy `select()`
directly. That separation is what keeps the API layer swappable later (e.g. adding a
cache, or moving search to a different engine) without router changes.
"""

from __future__ import annotations

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import Author, Book, Language, Subject, Work
from app.schemas.catalog import (
    AuthorOut,
    BookOut,
    LanguageOut,
    SubjectOut,
    WorkDetailOut,
    WorkOut,
)


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
        subjectId=book.work.subject_id if book.work else None,
        subjectTitle=None,  # filled by caller when the subject is joined/loaded
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


def _work_out(work: Work, volume_count: int, total_bytes: int, subject_title: str | None,
              collection_raw: str | None) -> WorkOut:
    return WorkOut(
        workId=str(work.id),
        title=work.title,
        author=work.author.name if work.author else "",
        authorDeath=work.author.death_label if work.author else None,
        subjectId=work.subject_id,
        subjectTitle=subject_title,
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
    language: str | None = None,
    author_id: int | None = None,
    featured: bool | None = None,
) -> tuple[list[WorkOut], int]:
    """Paginated, filterable list of works. Each row aggregates its published volumes'
    count and total size — a work with zero published volumes is excluded, since it has
    nothing a client could download."""
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
        query = query.where(Work.subject_id == subject_id)
    if language:
        query = query.where(Work.language_code == language)
    if author_id:
        query = query.where(Work.author_id == author_id)
    if featured:
        query = query.where(Work.is_featured.is_(True))

    total = await session.scalar(select(func.count()).select_from(query.subquery()))

    rows = (
        await session.execute(
            query.order_by(Work.title_norm).offset((page - 1) * limit).limit(limit)
        )
    ).all()

    work_ids = [w.id for w, _, _ in rows]
    subject_titles = await _subject_titles(
        session, {w.subject_id for w, _, _ in rows if w.subject_id}
    )
    collections = await _work_collection_raw(session, work_ids)

    items = [
        _work_out(w, vc, tb, subject_titles.get(w.subject_id), collections.get(w.id))
        for w, vc, tb in rows
    ]
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

    subject_titles = await _subject_titles(
        session, {work.subject_id} if work.subject_id else set()
    )
    collections = await _book_collection_raw(session, [b.id for b in books])
    work_collection = next(iter(collections.values()), None)

    volumes = []
    for b in books:
        out = _book_out(b, work.title, collections.get(b.id))
        # _book_out leaves subjectTitle unset by design (it's filled in by the caller
        # once subjects are batch-loaded) — list_books/get_book already do this; this
        # loop was the one caller that forgot to, leaving every volume's subjectTitle
        # null despite subjectId being populated right next to it.
        out.subjectTitle = subject_titles.get(out.subjectId)
        volumes.append(out)
    total_bytes = sum(b.content_bytes or 0 for b in books)

    return WorkDetailOut(
        **_work_out(
            work, len(books), total_bytes, subject_titles.get(work.subject_id), work_collection
        ).model_dump(),
        volumes=volumes,
    )


async def list_books(
    session: AsyncSession,
    *,
    page: int,
    limit: int,
    subject_id: str | None = None,
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
        query = query.where(Work.subject_id == subject_id)

    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await session.execute(
            query.order_by(Book.title_norm, Book.volume.nulls_first())
            .offset((page - 1) * limit)
            .limit(limit)
        )
    ).scalars().all()

    subject_titles = await _subject_titles(
        session, {b.work.subject_id for b in rows if b.work and b.work.subject_id}
    )
    collections = await _book_collection_raw(session, [b.id for b in rows])

    items = []
    for b in rows:
        out = _book_out(b, b.work.title if b.work else "", collections.get(b.id))
        out.subjectTitle = subject_titles.get(out.subjectId)
        items.append(out)
    return items, total or 0


async def get_book(session: AsyncSession, book_id: int) -> BookOut | None:
    book = await session.get(Book, book_id, options=_book_load_options())
    if book is None or not book.is_published:
        return None
    subject_titles = await _subject_titles(
        session,
        {book.work.subject_id} if book.work and book.work.subject_id else set(),
    )
    collections = await _book_collection_raw(session, [book.id])
    out = _book_out(book, book.work.title if book.work else "", collections.get(book.id))
    out.subjectTitle = subject_titles.get(out.subjectId)
    return out


async def list_authors(session: AsyncSession) -> list[AuthorOut]:
    rows = (await session.execute(select(Author).order_by(Author.name_norm))).scalars().all()
    return [AuthorOut(id=str(a.id), name=a.name, deathLabel=a.death_label) for a in rows]


async def list_subjects(session: AsyncSession) -> list[SubjectOut]:
    rows = (await session.execute(select(Subject).order_by(Subject.sort_order))).scalars().all()
    return [SubjectOut(id=s.id, title=s.title) for s in rows]


async def list_languages(session: AsyncSession) -> list[LanguageOut]:
    rows = (await session.execute(select(Language).order_by(Language.code))).scalars().all()
    return [LanguageOut(code=lang.code, name=lang.name) for lang in rows]


# ── internals ──────────────────────────────────────────────────────────────────────

def _work_load_options():
    return [joinedload(Work.author)]


def _book_load_options():
    """Every field _book_out reads off `book.author` or `book.work` must be eager-loaded
    here — there is no other query path that populates them."""
    return [joinedload(Book.author), joinedload(Book.work)]


async def _subject_titles(session: AsyncSession, subject_ids: set[str]) -> dict[str, str]:
    if not subject_ids:
        return {}
    rows = (
        await session.execute(
            select(Subject.id, Subject.title).where(Subject.id.in_(subject_ids))
        )
    ).all()
    return dict(rows)


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
