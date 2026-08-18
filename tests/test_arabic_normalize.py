"""Unit tests for the Arabic normalizer's individual folding rules.

These pin down *what* the normalizer does. `test_arabic_parity.py` separately pins down
that the SQL implementation does exactly the same thing.
"""

import pytest

from app.services.arabic import normalize


class TestTheMotivatingCase:
    def test_undiacriticized_query_matches_diacriticized_text(self):
        """The requirement this whole module exists for: a user typing plain Arabic must
        find text written with full hamza and tashkeel."""
        assert normalize("الإمام الصادق") == normalize("الامام الصادق")

    def test_swifts_folding_would_not_have_done_this(self):
        """Documents why we don't use ICU/`folding(.diacriticInsensitive)`: it strips
        diacritics but leaves hamza-on-alef, so these two would NOT have matched."""
        assert normalize("الإمام") == "الامام"


class TestDiacritics:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("مُحَمَّد", "محمد"),
            ("الْحَمْدُ لِلَّهِ", "الحمد لله"),
            ("عَلِيّ", "علي"),
            ("بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ", "بسم الله الرحمن الرحيم"),
        ],
    )
    def test_tashkeel_is_removed(self, text: str, expected: str):
        assert normalize(text) == expected

    def test_tatweel_is_removed(self):
        assert normalize("محـــمد") == "محمد"


class TestLetterFolding:
    @pytest.mark.parametrize("alef", ["أ", "إ", "آ", "ٱ", "ا"])
    def test_all_alef_variants_collapse(self, alef: str):
        assert normalize(alef) == "ا"

    def test_alef_maqsura_folds_to_ya(self):
        assert normalize("مصطفى") == normalize("مصطفي") == "مصطفي"

    def test_ta_marbuta_folds_to_ha(self):
        assert normalize("رحمة") == normalize("رحمه") == "رحمه"

    @pytest.mark.parametrize(
        ("persian", "arabic"),
        [("ک", "ك"), ("ی", "ي"), ("ھ", "ه"), ("ۀ", "ه")],
    )
    def test_persian_letterforms_fold_to_arabic(self, persian: str, arabic: str):
        """Visually identical, different codepoints — the library contains Persian books."""
        assert normalize(persian) == normalize(arabic)


class TestDigits:
    def test_arabic_indic_digits_fold_to_ascii(self):
        assert normalize("١٤٠٢") == "1402"

    def test_persian_digits_fold_to_ascii(self):
        assert normalize("۱۴۰۲") == "1402"

    def test_hijri_death_year_matches_either_script(self):
        assert normalize("ت ٣٢٩هـ") == normalize("ت 329هـ")


class TestUnicodeForm:
    def test_decomposed_input_is_composed_before_folding(self):
        """23.6% of real corpus paragraphs are not NFC. A decomposed alef+hamza must
        reach the folding stage as أ, or it would survive as a bare alef + stray mark."""
        decomposed = "أ"  # alef + combining hamza above
        assert normalize(decomposed) == "ا"

    def test_zero_width_and_bidi_marks_are_removed(self):
        assert normalize("محمد‌‏") == "محمد"

    def test_bom_is_removed(self):
        assert normalize("﻿محمد") == "محمد"


class TestWhitespace:
    def test_runs_collapse_and_edges_trim(self):
        assert normalize("  الإمام    الصادق  ") == "الامام الصادق"

    def test_nbsp_is_treated_as_whitespace(self):
        """Explicitly covered because Python's \\s and PostgreSQL's differ here, which
        would otherwise be a silent parity break."""
        assert normalize("الإمام الصادق") == "الامام الصادق"

    def test_newlines_and_tabs_collapse(self):
        assert normalize("الإمام\n\tالصادق") == "الامام الصادق"


class TestEdgeCases:
    def test_empty_string(self):
        assert normalize("") == ""

    def test_whitespace_only(self):
        assert normalize("   \n  ") == ""

    def test_pure_diacritics_reduce_to_empty(self):
        assert normalize("ًٌٍ") == ""

    def test_latin_is_lowercased(self):
        assert normalize("ISBN 964-440-062-3") == "isbn 964-440-062-3"

    def test_normalize_is_idempotent(self):
        once = normalize("الإمام الصادق عليه السلام")
        assert normalize(once) == once
