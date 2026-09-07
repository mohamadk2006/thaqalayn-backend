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

from app.schemas.catalog import SubjectOut
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


# A phrase as common as an Imam's name can match tens of thousands of pages out of the
# corpus's ~7.9M -- with no index that supports ranking by score directly, Postgres has
# to rank and sort every candidate before it can return the top ones. Measured on the
# real full corpus: uncapped, that took 60-70+ seconds for such a phrase. Capping the
# candidate set *before* the joins and ts_rank_cd (inside the CTE, so the planner pushes
# the LIMIT into the bitmap scan itself and stops early) brought the same query down to
# ~1.8 seconds. This trades exact ranking/counts on the rare very-broad query for
# reasonable, bounded latency on every query -- the right tradeoff for a single small VPS
# with no dedicated search engine. Realistic multi-word queries (what users actually
# type) match far fewer pages and are unaffected by the cap.
_CANDIDATE_CAP = 5000

# Filter conditions (subject/language/author/work) MUST apply inside this CTE, before
# the cap -- not after it on the outer query. A LIMIT with no ORDER BY stops the bitmap
# scan as soon as it has _CANDIDATE_CAP matches in physical heap order; a book filter
# applied only afterward would silently see nothing if that book's matches don't happen
# to fall within that first arbitrary batch (a real risk for a book added after most of
# the corpus, since new rows land at the physical end of the table). Pushing the filter
# into the CTE narrows what the scan is even looking for, so a filtered search stays both
# fast and exact -- only a broad, unfiltered, very-common-phrase search is subject to the
# cap's approximation at all.
_CANDIDATES_CTE = """
    WITH candidates AS (
        SELECT p.id, p.book_id, p.sequence, p.page_number, p.section_id,
               ts_rank_cd(p.search_tsv, q) AS score
        FROM pages p
        JOIN books b ON b.id = p.book_id AND b.is_published
        JOIN works w ON w.id = b.work_id
        LEFT JOIN authors a ON a.id = b.author_id,
        phraseto_tsquery('arabic', :normalized_query) q
        WHERE p.search_tsv @@ q
"""

_SEARCH_SQL_TAIL = f"""
        LIMIT {_CANDIDATE_CAP}
    )
    SELECT
        b.id AS book_id, b.work_id, w.title AS work_title, b.title, b.volume,
        a.name AS author,
        -- A work can belong to more than one of the 39 subjects (see WorkSubject) --
        -- a correlated subquery keeps this a JSON array per hit without joining
        -- work_subjects directly into the main query, which would multiply rows (and
        -- corrupt count(*) OVER () / ts_rank_cd-based ordering) for any work with 2+.
        (SELECT json_agg(json_build_object('id', s.id, 'title', s.title) ORDER BY s.sort_order)
         FROM work_subjects ws JOIN subjects s ON s.id = ws.subject_id
         WHERE ws.work_id = w.id) AS subjects_json,
        sec.title AS section_title, c.page_number, c.sequence,
        c.score,
        count(*) OVER () AS total_count
    FROM candidates c
    JOIN books b ON b.id = c.book_id
    JOIN works w ON w.id = b.work_id
    LEFT JOIN authors a ON a.id = b.author_id
    LEFT JOIN sections sec ON sec.id = c.section_id
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
        conditions.append(
            "EXISTS (SELECT 1 FROM work_subjects ws "
            "WHERE ws.work_id = w.id AND ws.subject_id IN :subject_ids)"
        )
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

    sql = _CANDIDATES_CTE
    if conditions:
        sql += " AND " + " AND ".join(conditions)
    sql += _SEARCH_SQL_TAIL
    sql += " ORDER BY c.score DESC LIMIT :limit OFFSET :offset"

    stmt = text(sql)
    if expanding:
        stmt = stmt.bindparams(*(bindparam(name, expanding=True) for name in expanding))

    # Defense in depth on top of _CANDIDATE_CAP: a filter combination the cap doesn't
    # anticipate well should fail loudly with a 500 in seconds, not hold a pooled
    # connection (and, at high enough concurrency, the whole pool) hostage for minutes.
    await session.execute(text("SET LOCAL statement_timeout = '8000'"))
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
        subjects_raw = row.subjects_json
        if isinstance(subjects_raw, str):
            subjects_raw = json.loads(subjects_raw)
        hits.append(
            SearchHit(
                bookId=str(row.book_id),
                workId=str(row.work_id),
                workTitle=row.work_title,
                title=row.title,
                author=row.author or "",
                volume=row.volume,
                subjects=[SubjectOut(**s) for s in (subjects_raw or [])],
                sectionTitle=row.section_title,
                page=row.page_number,
                snippet=snippet,
                matchStart=match_start,
                matchEnd=match_end,
                score=float(row.score),
            )
        )
    return hits, total
