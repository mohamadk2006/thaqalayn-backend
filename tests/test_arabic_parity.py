"""Proves the SQL and Python normalizers agree, against real corpus text.

This is the most important test in the project. The search index is built by the SQL
function (via the pages.search_tsv generated column) and queried through the Python one.
If they ever disagree, searches silently return wrong results — no error, no crash, just
missing hits that are extremely hard to trace back to a normalization mismatch.

The fixture is real text pulled from the converted library, bucketed to cover every rule
the normalizer implements: diacriticized text, non-NFC text, Persian letterforms, alef
and ta-marbuta variants, plus book titles and author names (which run through the same
normalizer for work-grouping and the category join).
"""

import json
import pathlib

import pytest
from sqlalchemy import text

from app.db import get_sessionmaker
from app.services.arabic import normalize

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "arabic_corpus_samples.json"
CORPUS: list[str] = json.loads(FIXTURE.read_text(encoding="utf-8"))


def _describe(s: str) -> str:
    """Codepoints, so a failure message is actually diagnosable — the difference between
    two normalized Arabic strings is usually invisible when printed."""
    return " ".join(f"U+{ord(c):04X}" for c in s)


async def _sql_normalize_all(values: list[str]) -> list[str]:
    """One round trip for the whole batch; per-row queries would dominate the runtime."""
    async with get_sessionmaker()() as session:
        result = await session.execute(
            text(
                "SELECT arabic_normalize(v) "
                "FROM unnest(cast(:values as text[])) WITH ORDINALITY AS t(v, i) "
                "ORDER BY i"
            ),
            {"values": values},
        )
        return [row[0] for row in result]


async def test_sql_and_python_agree_on_the_whole_corpus():
    expected = [normalize(s) for s in CORPUS]
    actual = await _sql_normalize_all(CORPUS)

    assert len(actual) == len(expected) == len(CORPUS)

    mismatches = [
        (original, py, sql)
        for original, py, sql in zip(CORPUS, expected, actual, strict=True)
        if py != sql
    ]
    if mismatches:
        original, py, sql = mismatches[0]
        pytest.fail(
            f"{len(mismatches)} of {len(CORPUS)} strings normalized differently.\n\n"
            f"First mismatch:\n"
            f"  input : {original[:120]!r}\n"
            f"  python: {py[:120]!r}\n"
            f"          {_describe(py[:40])}\n"
            f"  sql   : {sql[:120]!r}\n"
            f"          {_describe(sql[:40])}"
        )


@pytest.mark.parametrize(
    "value",
    [
        "الإمام الصادق",
        "الامام الصادق",
        "مُحَمَّد",
        "مصطفى",
        "رحمة",
        "معاني الأخبار ( فارسي )",
        "١٤٠٢",
        "ISBN 964-440-062-3",
        "  spaced\tout\ntext  ",
        "",
        "ًٌٍ",
    ],
)
async def test_sql_and_python_agree_on_targeted_cases(value: str):
    """Explicit cases alongside the corpus sweep, so a regression names the broken rule
    instead of just reporting 'the corpus disagrees'."""
    (actual,) = await _sql_normalize_all([value])
    assert actual == normalize(value)


async def test_sql_function_is_immutable():
    """Guards a hard requirement, not a preference: pages.search_tsv is a GENERATED
    column, and PostgreSQL only allows immutable functions in one. If a future revision
    makes this stable or volatile, the schema migration fails far from the cause."""
    async with get_sessionmaker()() as session:
        result = await session.execute(
            # provolatile is PostgreSQL's internal "char" type, which asyncpg hands back as
            # bytes; cast in SQL so the assertion compares plain text.
            text("SELECT provolatile::text FROM pg_proc WHERE proname = 'arabic_normalize'")
        )
        assert result.scalar_one() == "i"


async def test_sql_normalization_is_idempotent():
    once = await _sql_normalize_all(CORPUS)
    twice = await _sql_normalize_all(once)
    assert once == twice
