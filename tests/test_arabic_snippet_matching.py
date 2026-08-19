"""Tests for build_match_pattern / find_original_match: locating a normalized-query
match directly in original, fully-diacritized text.

This is the piece that lets search results show a real snippet with tashkeel intact,
rather than the stripped text ts_headline() would return if run on search_tsv's own
normalized source. Correctness here matters as much as the normalizer itself: a wrong
match position would highlight the wrong words in a result the user actually reads.
"""

import pytest

from app.services.arabic import build_match_pattern, find_original_match, normalize


class TestFindsRealCorpusPatterns:
    """Every case here is representative of an actual pattern in the corpus (not
    invented edge cases) — the same variety test_arabic_parity.py's fixture covers."""

    def test_diacritized_text_matches_undiacritized_query(self):
        text = "قال الإمامُ الصادقُ عليه السلام"
        match = find_original_match(text, normalize("الامام الصادق"))
        assert match is not None
        assert match.group() == "الإمامُ الصادق"

    def test_heavily_diacritized_text(self):
        text = "عن الإِمَام الصَّادِق في تفسير هذه الآية"
        match = find_original_match(text, normalize("الامام الصادق"))
        assert match is not None
        assert "الإِمَام" in match.group()

    def test_alef_maqsura_vs_ya(self):
        assert find_original_match("مصطفى بن محمد", normalize("مصطفي")).group() == "مصطفى"

    def test_ta_marbuta_vs_ha(self):
        assert find_original_match("رحمة الله", normalize("رحمه")).group() == "رحمة"

    def test_persian_letterform(self):
        text = "چهارده معصوم ( ع )"
        assert find_original_match(text, normalize("معصوم")).group() == "معصوم"

    def test_arabic_indic_digits(self):
        assert find_original_match("سنة ١٤٠٢ هـ", normalize("1402")).group() == "١٤٠٢"

    def test_persian_digits(self):
        assert find_original_match("سال ۱۴۰۲", normalize("1402")).group() == "۱۴۰۲"


class TestMultiWordPhrase:
    def test_extra_whitespace_between_words_still_matches(self):
        text = "قال الإمام   الصادق عليه السلام"
        match = find_original_match(text, normalize("الامام الصادق"))
        assert match is not None

    def test_word_order_matters(self):
        """Mirrors phraseto_tsquery's own phrase semantics -- words out of order should
        not match, since the SQL side wouldn't have matched this page for this query."""
        text = "الصادق ثم الإمام"
        assert find_original_match(text, normalize("الامام الصادق")) is None


class TestNoFalseMatches:
    def test_unrelated_text_does_not_match(self):
        assert find_original_match("نص لا علاقة له بالبحث", normalize("الامام الصادق")) is None

    def test_partial_word_does_not_match(self):
        """"امام" (without ال) must not match inside "الإمام" -- the query's own ال is
        part of the pattern, so a bare stem shouldn't over-match a definite-article form
        it wasn't given."""
        text = "بحث عن امامة الأمة"
        match = find_original_match(text, normalize("الامام"))
        assert match is None


class TestEdgeCases:
    def test_empty_query_returns_none(self):
        assert find_original_match("أي نص هنا", "") is None

    def test_pattern_is_reusable_across_many_texts(self):
        """The service layer will build one pattern per query and reuse it across many
        matched pages, rather than rebuilding per page -- confirm that actually works."""
        pattern = build_match_pattern(normalize("الامام الصادق"))
        assert pattern.search("قال الإمامُ الصادقُ") is not None
        assert pattern.search("عن الإِمَام الصَّادِق") is not None
        assert pattern.search("نص غير ذي صلة") is None

    @pytest.mark.parametrize("query", ["a", "١٢٣", "test بحث"])
    def test_does_not_raise_on_mixed_or_latin_input(self, query: str):
        find_original_match("بعض النص العربي هنا", normalize(query))
