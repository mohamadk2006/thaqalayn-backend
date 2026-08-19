"""Full-library search: the implementation the API contract deliberately hides.

Matching runs against the GIN-indexed, normalized `search_tsv` column (fast, proven
correct against real diacritized text). Snippets are extracted separately, from each
matched page's *original* text via `find_original_match` -- ts_headline() would return
normalized (tashkeel-stripped) text instead, since that's all search_tsv's source ever
was, and a search result has to show the user real text.

This is the one module search-consuming code should ever import. If PostgreSQL FTS is
ever swapped for something else (Typesense, Meilisearch), this is the only place that
changes -- the router and schema stay as they are.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.search import SearchHit
from app.services.arabic import find_original_match, normalize

# Characters of context kept on each side of the match inside a snippet. Large enough to
# give real context, small enough to keep the response light across up to `limit` hits.
_SNIPPET_RADIUS = 90


def _extract_snippet(text_: str, normalized_query: str) -> tuple[str, int | None, int | None]:
    """Return (snippet, matchStart, matchEnd) with the match's position given in the
    *snippet's own* coordinates, snapped to whitespace so words aren't cut mid-way.

    Falls back to the page's own opening text, unhighlighted, if the regex-based locator
    doesn't find the query in this specific page -- a rare SQL/regex tokenization
    divergence, not something that should make the whole hit disappear.
    """
    match = find_original_match(text_, normalized_query)
    if match is None:
        snippet = text_[: 2 * _SNIPPET_RADIUS].strip()
        return snippet, None, None

    start = max(0, match.start() - _SNIPPET_RADIUS)
    end = min(len(text_), match.end() + _SNIPPET_RADIUS)
    # Snap outward to the nearest whitespace so the snippet doesn't open or close
    # mid-word -- but never past the match itself.
    while start > 0 and not text_[start].isspace():
        start -= 1
    while end < len(text_) and not text_[end].isspace():
        end += 1

    snippet = text_[start:end].strip()
    leading_trim = len(text_[start:end]) - len(text_[start:end].lstrip())
    snippet_start = match.start() - start - leading_trim
    snippet_end = match.end() - start - leading_trim
    return snippet, snippet_start, snippet_end


_SEARCH_SQL = """
    SELECT
        b.id AS book_id, b.work_id, w.title AS work_title, b.title, b.volume,
        a.name AS author, w.subject_id, s.title AS subject_title,
        sec.title AS section_title, p.page_no, p.text,
        ts_rank_cd(p.search_tsv, q) AS score,
        count(*) OVER () AS total_count
    FROM pages p
    JOIN books b ON b.id = p.book_id AND b.is_published
    JOIN works w ON w.id = b.work_id
    LEFT JOIN authors a ON a.id = b.author_id
    LEFT JOIN subjects s ON s.id = w.subject_id
    LEFT JOIN sections sec ON sec.id = p.section_id,
    phraseto_tsquery('simple', :normalized_query) q
    WHERE p.search_tsv @@ q
"""


async def search(
    session: AsyncSession,
    *,
    query: str,
    page: int,
    limit: int,
    subject_id: str | None = None,
    tradition: str | None = None,
    language: str | None = None,
    author_id: int | None = None,
    work_id: int | None = None,
) -> tuple[list[SearchHit], int]:
    normalized_query = normalize(query)
    if not normalized_query:
        return [], 0

    conditions = []
    params: dict = {
        "normalized_query": normalized_query,
        "limit": limit,
        "offset": (page - 1) * limit,
    }
    if subject_id:
        conditions.append("w.subject_id = :subject_id")
        params["subject_id"] = subject_id
    if tradition:
        conditions.append("w.tradition = cast(:tradition as tradition)")
        params["tradition"] = tradition
    if language:
        conditions.append("b.language_code = :language")
        params["language"] = language
    if author_id:
        conditions.append("b.author_id = :author_id")
        params["author_id"] = author_id
    if work_id:
        conditions.append("b.work_id = :work_id")
        params["work_id"] = work_id

    sql = _SEARCH_SQL
    if conditions:
        sql += " AND " + " AND ".join(conditions)
    sql += " ORDER BY score DESC LIMIT :limit OFFSET :offset"

    rows = (await session.execute(text(sql), params)).all()
    if not rows:
        return [], 0

    total = rows[0].total_count
    hits = []
    for row in rows:
        snippet, match_start, match_end = _extract_snippet(row.text, normalized_query)
        hits.append(
            SearchHit(
                bookId=str(row.book_id),
                workId=str(row.work_id),
                workTitle=row.work_title,
                title=row.title,
                author=row.author or "",
                volume=row.volume,
                subjectId=row.subject_id,
                subjectTitle=row.subject_title,
                sectionTitle=row.section_title,
                page=row.page_no,
                snippet=snippet,
                matchStart=match_start,
                matchEnd=match_end,
                score=float(row.score),
            )
        )
    return hits, total
