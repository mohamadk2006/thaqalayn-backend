"""Search the table-of-contents headings of every published book at once.

Matches against `sections.title_norm` (each heading already folded by
app.services.arabic.normalize when the book was imported), through the GIN index on
`to_tsvector('simple', title_norm)`. The query is folded with the same normalize(), so
"الامام" finds "الإمام", and every word is matched as a prefix -- "الطها" finds "الطهارة"
while the user is still typing.

Like search_service, only the first _CANDIDATE_CAP matches are ranked: a word such as
"باب" matches ~3.5% of all ~5.4 million headings. The cap keeps the worst case bounded, and
filters (work/subject/library/...) are applied inside the capped set, so a filtered search
stays exact.
"""

from __future__ import annotations

import re

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.search import TocHit
from app.services.arabic import normalize

_CANDIDATE_CAP = 5000
_MAX_TERMS = 8

_TERM_RE = re.compile(r"[^\W_]+")

_SQL = f"""
    WITH candidates AS (
        SELECT s.id, s.book_id, s.ord, s.title, s.title_norm, s.page_start_sequence
        FROM sections s
        JOIN books b ON b.id = s.book_id AND b.is_published
        JOIN works w ON w.id = b.work_id
        LEFT JOIN authors a ON a.id = b.author_id
        WHERE to_tsvector('simple', s.title_norm) @@ to_tsquery('simple', :tsquery)
        {{filters}}
        LIMIT {_CANDIDATE_CAP}
    ), ranked AS (
        SELECT c.*,
               count(*) OVER () AS total_count,
               row_number() OVER (
                   ORDER BY (c.title_norm = :phrase) DESC,
                            (c.title_norm LIKE :phrase_prefix) DESC,
                            length(c.title_norm), c.book_id, c.ord
               ) AS rn
        FROM candidates c
    )
    SELECT r.book_id, r.ord, r.title AS heading, r.page_start_sequence, r.total_count,
           b.work_id, w.title AS work_title, b.title, b.volume, a.name AS author,
           p.page_number
    FROM ranked r
    JOIN books b ON b.id = r.book_id
    JOIN works w ON w.id = b.work_id
    LEFT JOIN authors a ON a.id = b.author_id
    LEFT JOIN pages p ON p.book_id = r.book_id AND p.sequence = r.page_start_sequence
    WHERE r.rn > :offset AND r.rn <= :offset + :limit
    ORDER BY r.rn
"""


def _terms(query: str) -> list[str]:
    return _TERM_RE.findall(normalize(query))[:_MAX_TERMS]


async def search_toc(
    session: AsyncSession,
    *,
    query: str,
    page: int,
    limit: int,
    subject_ids: list[str] | None = None,
    library_ids: list[int] | None = None,
    languages: list[str] | None = None,
    author_ids: list[int] | None = None,
    author_names: list[str] | None = None,
    work_ids: list[int] | None = None,
) -> tuple[list[TocHit], int, bool]:
    """Returns (hits, total, capped). `total` counts the ranked candidates, not every
    heading in the library, and `capped` is true when that hit the cap."""
    terms = _terms(query)
    if not terms:
        return [], 0, False

    phrase = " ".join(terms)
    params: dict = {
        # Every term is only letters/digits (see _TERM_RE), so this is a safe tsquery.
        "tsquery": " & ".join(f"{t}:*" for t in terms),
        "phrase": phrase,
        "phrase_prefix": phrase + "%",
        "limit": limit,
        "offset": (page - 1) * limit,
    }

    conditions: list[str] = []
    expanding: list[str] = []
    if subject_ids:
        conditions.append(
            "EXISTS (SELECT 1 FROM work_subjects ws "
            "WHERE ws.work_id = w.id AND ws.subject_id IN :subject_ids)"
        )
        params["subject_ids"] = subject_ids
        expanding.append("subject_ids")
    if library_ids:
        conditions.append(
            "EXISTS (SELECT 1 FROM library_works lw "
            "WHERE lw.work_id = w.id AND lw.library_id IN :library_ids)"
        )
        params["library_ids"] = library_ids
        expanding.append("library_ids")
    if languages:
        conditions.append("b.language_code IN :languages")
        params["languages"] = languages
        expanding.append("languages")
    if author_ids:
        conditions.append("b.author_id IN :author_ids")
        params["author_ids"] = author_ids
        expanding.append("author_ids")
    if author_names:
        conditions.append("a.name_norm IN :author_names")
        params["author_names"] = [normalize(name) for name in author_names]
        expanding.append("author_names")
    if work_ids:
        conditions.append("b.work_id IN :work_ids")
        params["work_ids"] = work_ids
        expanding.append("work_ids")

    sql = _SQL.replace(
        "{filters}", "".join(f" AND {c}" for c in conditions)
    )
    stmt = text(sql)
    if expanding:
        stmt = stmt.bindparams(*(bindparam(name, expanding=True) for name in expanding))

    await session.execute(text("SET LOCAL statement_timeout = '10000'"))
    rows = (await session.execute(stmt, params)).all()
    if not rows:
        return [], 0, False

    total = rows[0].total_count
    hits = [
        TocHit(
            bookId=str(r.book_id), workId=str(r.work_id), workTitle=r.work_title,
            title=r.title, author=r.author or "", volume=r.volume,
            heading=r.heading, tocId=f"toc-{r.ord:05d}", order=r.ord,
            page=r.page_number or "", pageSequence=r.page_start_sequence,
        )
        for r in rows
    ]
    return hits, total, total >= _CANDIDATE_CAP
