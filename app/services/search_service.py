"""Full-library search: the implementation the API contract deliberately hides.

Matching runs against the GIN-indexed `search_tsv` column (fast, proven correct against
real diacritized text, stemmed via Postgres's built-in 'arabic' config). Snippets are
extracted separately, from each matched page's *original* text via `find_original_match`
-- ts_headline() would return normalized (tashkeel-stripped, stemmed) text instead, and a
search result has to show the user real text.

`pages` does not store that text (see Page's docstring in app/models/library.py) -- only
the search index computed from it, to avoid duplicating ~30 GB of already-downloadable
text inside the database. So a hit's original text is read back from the book's own JSON
file on BOOKS_ROOT, on demand, for each of the (at most `limit`) rows a query actually
returns -- never for the full set of candidates the GIN index narrows down, so the extra
I/O this adds is bounded by page size and result count, not corpus size.

This is the one module search-consuming code should ever import. If PostgreSQL FTS is
ever swapped for something else (Typesense, Meilisearch), this is the only place that
changes -- the router and schema stay as they are.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.search import SearchHit
from app.services.arabic import find_original_match, normalize
from app.services.paging import page_text_and_offsets

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


def _load_page_text(books_root: Path, book_id: int, sequence: int, cache: dict) -> str | None:
    """The one place a search hit re-reads its book's JSON file. `cache` is per-call
    (one search() invocation, keyed by book_id) so several hits landing in the same book
    -- not unusual for a common phrase -- parse that file once, not once per hit.

    Returns None if the file is missing or the page can't be found in it (the DB row
    should always have a matching JSON page, but a search result disappearing a snippet
    is a far better failure than a 500 over a data mismatch that shouldn't happen).
    """
    if book_id not in cache:
        path = books_root / f"{book_id}.json"
        try:
            cache[book_id] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache[book_id] = None
    content = cache[book_id]
    if content is None:
        return None
    for page in content.get("pages", []):
        if page.get("sequence") == sequence:
            text_, _offsets = page_text_and_offsets(page)
            return text_
    return None


_SEARCH_SQL = """
    SELECT
        b.id AS book_id, b.work_id, w.title AS work_title, b.title, b.volume,
        a.name AS author, w.subject_id, s.title AS subject_title,
        sec.title AS section_title, p.page_number, p.sequence,
        ts_rank_cd(p.search_tsv, q) AS score,
        count(*) OVER () AS total_count
    FROM pages p
    JOIN books b ON b.id = p.book_id AND b.is_published
    JOIN works w ON w.id = b.work_id
    LEFT JOIN authors a ON a.id = b.author_id
    LEFT JOIN subjects s ON s.id = w.subject_id
    LEFT JOIN sections sec ON sec.id = p.section_id,
    phraseto_tsquery('arabic', :normalized_query) q
    WHERE p.search_tsv @@ q
"""


async def search(
    session: AsyncSession,
    *,
    query: str,
    page: int,
    limit: int,
    books_root: Path,
    subject_ids: list[str] | None = None,
    languages: list[str] | None = None,
    author_ids: list[int] | None = None,
    author_names: list[str] | None = None,
    work_id: int | None = None,
) -> tuple[list[SearchHit], int]:
    normalized_query = normalize(query)
    if not normalized_query:
        return [], 0

    # AND across filter kinds (subject/language/author), OR within each -- a book must
    # match at least one selected value per kind, but all kinds that were given.
    conditions = []
    params: dict = {
        "normalized_query": normalized_query,
        "limit": limit,
        "offset": (page - 1) * limit,
    }
    expanding: list[str] = []
    if subject_ids:
        conditions.append("w.subject_id IN :subject_ids")
        params["subject_ids"] = subject_ids
        expanding.append("subject_ids")
    if languages:
        conditions.append("b.language_code IN :languages")
        params["languages"] = languages
        expanding.append("languages")
    if author_ids:
        conditions.append("b.author_id IN :author_ids")
        params["author_ids"] = author_ids
        expanding.append("author_ids")
    if author_names:
        # The app tracks authors by name only (no ID concept), so match on the same
        # name_norm the authors table was written with -- normalize() here must be the
        # identical fold used to populate that column, or every row would miss.
        conditions.append("a.name_norm IN :author_names")
        params["author_names"] = [normalize(name) for name in author_names]
        expanding.append("author_names")
    if work_id:
        conditions.append("b.work_id = :work_id")
        params["work_id"] = work_id

    sql = _SEARCH_SQL
    if conditions:
        sql += " AND " + " AND ".join(conditions)
    sql += " ORDER BY score DESC LIMIT :limit OFFSET :offset"

    stmt = text(sql)
    if expanding:
        stmt = stmt.bindparams(*(bindparam(name, expanding=True) for name in expanding))

    rows = (await session.execute(stmt, params)).all()
    if not rows:
        return [], 0

    total = rows[0].total_count
    hits = []
    page_text_cache: dict[int, dict | None] = {}
    for row in rows:
        original_text = _load_page_text(books_root, row.book_id, row.sequence, page_text_cache)
        if original_text is None:
            # Should not happen (a DB row with no matching JSON page), but a missing
            # snippet is a far better failure than a 500 over a data mismatch that
            # shouldn't exist -- the hit still carries a real book, page, and score.
            snippet, match_start, match_end = "", None, None
        else:
            snippet, match_start, match_end = _extract_snippet(original_text, normalized_query)
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
                page=row.page_number,
                snippet=snippet,
                matchStart=match_start,
                matchEnd=match_end,
                score=float(row.score),
            )
        )
    return hits, total
