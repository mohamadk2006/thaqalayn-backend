"""Full-library search: the implementation the API contract deliberately hides.

Matching runs against the GIN-indexed `search_tsv` column (fast, proven correct against
real diacritized text). Words match exactly as typed, after normalization only
(tashkeel/hamza/etc., see app/services/arabic.py) -- no stemming, so "الاغتسال ليلا" does
not also find "الاغتسال بالليل". Snippets are
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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.catalog import LibraryOut, SubjectOut
from app.schemas.search import SearchHit
from app.services.arabic import SEARCH_TS_CONFIG, find_original_match, normalize
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


# A phrase on up to this many pages is found, sorted and counted exactly; on more, it is a
# common word and the total shown is this number.
_MATCH_LIMIT = 10000

# The select over the pages chosen, kept in the order c.rn, c.sequence.
_FINAL_SELECT = """    SELECT
        b.id AS book_id, b.work_id, w.title AS work_title, b.title, b.volume,
        a.name AS author,
        -- A work can belong to more than one of the 39 subjects (see WorkSubject) --
        -- a correlated subquery keeps this a JSON array per hit without joining
        -- work_subjects directly into the main query, which would multiply rows (and
        -- corrupt count(*) OVER () / ts_rank_cd-based ordering) for any work with 2+.
        (SELECT json_agg(json_build_object('id', s.id, 'title', s.title) ORDER BY s.sort_order)
         FROM work_subjects ws JOIN subjects s ON s.id = ws.subject_id
         WHERE ws.work_id = w.id) AS subjects_json,
        -- Same correlated-subquery reasoning as subjects_json, for the independent
        -- library system (see app.models.library.Library) -- a work can belong to any
        -- number of libraries regardless of its subjects.
        (SELECT json_agg(json_build_object('id', lib.id, 'title', lib.title,
                                            'parentId', lib.parent_id) ORDER BY lib.sort_order)
         FROM library_works lw JOIN libraries lib ON lib.id = lw.library_id
         WHERE lw.work_id = w.id) AS libraries_json,
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
    library_ids: list[int] | None = None,
    languages: list[str] | None = None,
    author_ids: list[int] | None = None,
    author_names: list[str] | None = None,
    work_ids: list[int] | None = None,
) -> tuple[list[SearchHit], int]:
    """Pages that contain the phrase, in one fixed order: the author's death year (oldest
    first; no year, last), then the work's title, the volume and the page -- so a phrase
    found in al-Kafi's volumes and in Bihar al-Anwar reads al-Kafi 1, 1, 2, then Bihar.

    The books are taken in that order, a batch at a time, until the page is full: a very
    common word fills it from the first batch, where sorting every match of it first
    timed out at 90 seconds on the real library. A rare phrase just goes through every
    batch, each one a GIN lookup."""
    normalized_query = normalize(query)
    if not normalized_query:
        return [], 0

    # AND across filter kinds (subject/language/author), OR within each -- a book must
    # match at least one selected value per kind, but all kinds that were given.
    conditions = []
    params: dict = {"normalized_query": normalized_query}
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
        # The app tracks authors by name only (no ID concept), so match on the same
        # name_norm the authors table was written with -- normalize() here must be the
        # identical fold used to populate that column, or every row would miss.
        conditions.append("a.name_norm IN :author_names")
        params["author_names"] = [normalize(name) for name in author_names]
        expanding.append("author_names")
    if work_ids:
        conditions.append("b.work_id IN :work_ids")
        params["work_ids"] = work_ids
        expanding.append("work_ids")

    extra = (" AND " + " AND ".join(conditions)) if conditions else ""
    match_from = f"""
        FROM pages p
        JOIN books b ON b.id = p.book_id AND b.is_published
        JOIN works w ON w.id = b.work_id
        LEFT JOIN authors a ON a.id = b.author_id,
        phraseto_tsquery('{SEARCH_TS_CONFIG}', :normalized_query) q
        WHERE p.search_tsv @@ q{extra}"""

    def statement(sql: str):
        stmt = text(sql)
        if expanding:
            stmt = stmt.bindparams(*(bindparam(name, expanding=True) for name in expanding))
        return stmt

    # A query that is merely slow gets this long rather than failing.
    await session.execute(text("SET LOCAL statement_timeout = '30000'"))
    order = await _book_order(session)
    rank = {book_id: n for n, book_id in enumerate(order)}
    wanted = page * limit

    # 1. Most phrases match few enough pages to find them all through the index and sort
    #    them here: exact order, exact total. Words that are each common but rarely
    #    adjacent ("العلم نور": 112,650 pages hold both, 534 hold the phrase) make the index
    #    scan recheck every page that holds both words: 8 seconds the first time, 0.2 after.
    #    A search that takes over 15 seconds is made again with gin_fuzzy_search_limit, which
    #    thins that scan at random and so finds only some of the pages (206 of the 534).
    match_sql = statement(
        f"SELECT p.id, p.book_id, p.sequence {match_from} LIMIT {_MATCH_LIMIT + 1}")
    try:
        async with session.begin_nested():
            await session.execute(text("SET LOCAL statement_timeout = '15000'"))
            await session.execute(text("SET LOCAL gin_fuzzy_search_limit = 0"))
            found = (await session.execute(match_sql, params)).all()
    except DBAPIError as exc:
        if "statement timeout" not in str(exc):
            raise
        await session.execute(text("SET LOCAL gin_fuzzy_search_limit = 100000"))
        found = (await session.execute(match_sql, params)).all()
    if len(found) <= _MATCH_LIMIT:
        found.sort(key=lambda r: (rank.get(r.book_id, len(rank)), r.sequence))
        chosen = [r.id for r in found[(page - 1) * limit:wanted]]
        if not chosen:
            return [], 0
        rows = (await session.execute(text(f"""
            WITH candidates AS (
                SELECT p.id, p.book_id, p.sequence, p.page_number, p.section_id, 0 AS rn,
                       ts_rank_cd(p.search_tsv, q) AS score
                FROM pages p, phraseto_tsquery('{SEARCH_TS_CONFIG}', :normalized_query) q
                WHERE p.id = ANY(:ids)
            )
            {_FINAL_SELECT}"""), {"normalized_query": normalized_query, "ids": chosen})).all()
        by_id = {}  # the select does not keep the order chosen
        for row in rows:
            by_id[(row.book_id, row.sequence)] = row
        wanted_order = {r.id: (r.book_id, r.sequence) for r in found}
        ordered = [by_id[wanted_order[i]] for i in chosen if wanted_order[i] in by_id]
        return _build_hits(ordered, normalized_query, books_root), len(found)

    # 2. A phrase on more pages than that is a common word: its first matches in the order
    #    are found by taking the books a batch at a time until the page is full, where
    #    sorting all of them timed out at 90 seconds. The total is then the count so far.
    await session.execute(text("SET LOCAL gin_fuzzy_search_limit = 0"))
    batch_sql = f"""
        WITH candidates AS (
            SELECT p.id, p.book_id, p.sequence, p.page_number, p.section_id, u.rn,
                   ts_rank_cd(p.search_tsv, q) AS score
            FROM unnest(CAST(:ids AS bigint[])) WITH ORDINALITY AS u(book_id, rn)
            JOIN pages p ON p.book_id = u.book_id
            JOIN books b ON b.id = p.book_id AND b.is_published
            JOIN works w ON w.id = b.work_id
            LEFT JOIN authors a ON a.id = b.author_id,
            phraseto_tsquery('{SEARCH_TS_CONFIG}', :normalized_query) q
            WHERE p.search_tsv @@ q{extra}
            ORDER BY u.rn, p.sequence
            LIMIT :take
        )
        {_FINAL_SELECT}
        ORDER BY c.rn, c.sequence"""
    rows: list = []
    start, size = 0, _FIRST_BATCH
    while start < len(order) and len(rows) < wanted:
        got = await session.execute(statement(batch_sql), {
            **params, "ids": order[start:start + size], "take": wanted - len(rows)})
        rows.extend(got.all())
        start += size
        size = min(size * 2, _MAX_BATCH)
    rows = rows[(page - 1) * limit:wanted]
    if not rows:
        return [], 0
    return _build_hits(rows, normalized_query, books_root), max(_MATCH_LIMIT, len(rows))


def _build_hits(rows, normalized_query: str, books_root: Path) -> list[SearchHit]:
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
        libraries_raw = row.libraries_json
        if isinstance(libraries_raw, str):
            libraries_raw = json.loads(libraries_raw)
        hits.append(
            SearchHit(
                bookId=str(row.book_id),
                workId=str(row.work_id),
                workTitle=row.work_title,
                title=row.title,
                author=row.author or "",
                volume=row.volume,
                subjects=[SubjectOut(**s) for s in (subjects_raw or [])],
                libraries=[
                    LibraryOut(
                        id=str(lib["id"]), title=lib["title"],
                        parentId=str(lib["parentId"]) if lib["parentId"] is not None else None,
                    )
                    for lib in (libraries_raw or [])
                ],
                sectionTitle=row.section_title,
                page=row.page_number,
                pageSequence=row.sequence,
                snippet=snippet,
                matchStart=match_start,
                matchEnd=match_end,
                score=float(row.score),
            )
        )
    return hits


# ── the order of the books ──────────────────────────────────────────────────────

# Hijri years above this are placeholders (one author has 99999), not death years.
_LAST_PLAUSIBLE_DEATH_YEAR = 1500
# A death known only as a century ("قرن 3", "ق 12": 316 authors) is stored as the century's
# number, so its year is not that number: it is the middle of the century (250, 1150).
# An author with no death year ('معاصر', contemporary, or none recorded) comes last.
_BOOK_ORDER_SQL = (
    "(CASE "
    "WHEN a.death_label ~ '^(قرن|ق)[[:space:]]*[0-9]+' "
    "THEN substring(a.death_label from '[0-9]+')::int * 100 - 50 "
    f"WHEN a.death_year_hijri BETWEEN 1 AND {_LAST_PLAUSIBLE_DEATH_YEAR} THEN a.death_year_hijri "
    "ELSE 99999 END), w.title_norm, w.id, b.volume NULLS FIRST, b.id"
)
_ORDER_TTL_SECONDS = 60
# (when, the published books' (count, newest id) it was made for, book ids in order)
_order_cache: tuple[float, tuple, list[int]] | None = None
_FIRST_BATCH = 300
_MAX_BATCH = 4000


async def _book_order(session: AsyncSession) -> list[int]:
    """Published book ids in order. Kept for a minute (edits of a title or a death year
    show by then), and made again at once when a book is added or unpublished."""
    global _order_cache
    import time

    signature = tuple((await session.execute(text(
        "SELECT count(*), coalesce(max(id), 0) FROM books WHERE is_published"))).one())
    fresh = _order_cache and time.monotonic() - _order_cache[0] < _ORDER_TTL_SECONDS
    if fresh and _order_cache[1] == signature:
        return _order_cache[2]
    rows = await session.execute(text(
        "SELECT b.id FROM books b JOIN works w ON w.id = b.work_id "
        "LEFT JOIN authors a ON a.id = b.author_id WHERE b.is_published "
        f"ORDER BY {_BOOK_ORDER_SQL}"))
    ids = [r[0] for r in rows]
    _order_cache = (time.monotonic(), signature, ids)
    return ids
