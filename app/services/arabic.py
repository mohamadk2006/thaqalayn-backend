"""Arabic text normalization for search.

Every search-related comparison in this project runs through `normalize()`: the
`tsvector` generated column, the query parser, the work-grouping heuristic that decides
which volumes belong to the same book, and the category join against Shamela's own
(orthographically inconsistent) collection names.

There is a byte-for-byte identical implementation in SQL — see
`database/sql/arabic_normalize.sql`. The two MUST agree exactly: the index is built by
the SQL version and queried through the Python version, so any divergence silently
produces wrong search results rather than an error. `tests/test_arabic_parity.py`
enforces this against real corpus text.

Why not use a library or ICU folding: neither does what's needed here. Swift's
`.folding(.diacriticInsensitive)` is a no-op on Arabic tashkeel, and ICU's search
collation strips diacritics but does not fold hamza-on-alef — so a search for
"الامام الصادق" fails to match "الإمام الصادق", which is the single most common way
users actually type Arabic queries.

The normalized form is for *matching only*. Original text, with full tashkeel, is what
gets stored and displayed; nothing here ever modifies the source books.
"""

import re
import unicodedata

# ── Marks removed entirely ───────────────────────────────────────────────────────
# Tashkeel (fatha, damma, kasra, shadda, sukun, tanween …) plus the Quranic annotation
# marks that appear throughout this corpus. Measured on the real library: 50.7% of
# paragraphs carry at least one of these, so folding them is what makes an
# undiacriticized query work at all.
_TASHKEEL = "ً-ٰٟۖ-ۭ"

# Tatweel/kashida: a purely typographic stretching character with no phonetic value.
_TATWEEL = "ـ"

# Zero-width and bidi control characters. Persian text uses ZWNJ heavily, and copied
# text routinely carries stray marks and BOMs that would otherwise split a token.
_INVISIBLES = "​-‏‪-‮⁦-⁩﻿"

_STRIP_RE = re.compile(f"[{_TASHKEEL}{_TATWEEL}{_INVISIBLES}]")

# Spelled out rather than using `\s`, because `\s` is not the same set in Python and
# PostgreSQL — Python's is Unicode-aware, PostgreSQL's follows [[:space:]]. An explicit
# class is the only way to guarantee the two implementations agree on, say, NBSP.
_WHITESPACE = "\t\n\v\f\r \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"
_WHITESPACE_RE = re.compile(f"[{_WHITESPACE}]+")

# ── Letters folded to a single canonical form ────────────────────────────────────
# Written as explicit pairs rather than a packed translate() string so each decision is
# individually visible and reviewable. The SQL twin encodes exactly these pairs.
_LETTER_FOLDING = {
    # Alef variants → bare alef. This is the fold that makes "الامام" match "الإمام";
    # measured on the corpus: 64k أ, 29k إ, 5.6k آ against 369k bare ا.
    "أ": "ا",  # أ  alef with hamza above
    "إ": "ا",  # إ  alef with hamza below
    "آ": "ا",  # آ  alef with madda above
    "ٱ": "ا",  # ٱ  alef wasla
    # Alef maqsura → ya. Writers use them interchangeably word-finally (مصطفى/مصطفي);
    # corpus has 29k ى against 166k ي.
    "ى": "ي",  # ى → ي
    # Ta marbuta → ha. Same interchangeable word-final usage (رحمة/رحمه).
    "ة": "ه",  # ة → ه
    # Persian letterforms → Arabic equivalents. The library contains Persian books
    # (e.g. معاني الأخبار (فارسي)), and these codepoints are visually identical to
    # their Arabic counterparts but do not compare equal.
    "ک": "ك",  # ک → ك  Persian kaf
    "ی": "ي",  # ی → ي  Persian yeh
    "ھ": "ه",  # ھ → ه  heh doachashmee
    "ۀ": "ه",  # ۀ → ه  heh with yeh above
}

# Arabic-Indic and Persian digits → ASCII, so "١٤٠٢" and "1402" are the same query.
_DIGIT_FOLDING = {
    **{chr(0x0660 + i): str(i) for i in range(10)},  # ٠-٩
    **{chr(0x06F0 + i): str(i) for i in range(10)},  # ۰-۹
}

_TRANSLATION = str.maketrans({**_LETTER_FOLDING, **_DIGIT_FOLDING})


def normalize(text: str) -> str:
    """Fold `text` into its canonical searchable form.

    Order matters. NFC composition runs first so that a decomposed sequence such as
    alef + combining hamza becomes أ before the letter folding stage sees it — 23.6% of
    real corpus paragraphs are not in NFC form, so skipping this would leave a
    substantial fraction of the library unmatchable.
    """
    if not text:
        return ""

    text = unicodedata.normalize("NFC", text)
    text = _STRIP_RE.sub("", text)
    text = text.translate(_TRANSLATION)
    text = text.lower()
    return _WHITESPACE_RE.sub(" ", text).strip()


# ── Inverse mapping, for locating matches in the ORIGINAL (undiacritized-search-but-
# fully-diacritized-display) text ──────────────────────────────────────────────────
#
# The GIN index and phraseto_tsquery() find *which pages* match against normalized
# text — that part is settled and fast. But a search result has to show the user a
# snippet of the real page, with full tashkeel, not the stripped/folded text the index
# is built from (ts_headline() would return exactly that stripped text, since it has no
# way to see the original). So snippet extraction runs as a second, separate step: take
# the already-matched page's original text and locate the same match directly in it.
#
# _REVERSE_FOLDING inverts _LETTER_FOLDING: for each normalized letter, every original
# spelling that folds to it. Building a regex from this — one alternation group per
# letter of the query, with an optional tashkeel/tatweel run allowed between every pair
# — matches any original-text spelling that would normalize to the query, without
# needing a character-by-character position map between normalized and original text
# (which NFC composition and mark-stripping make non-trivial to maintain).
_REVERSE_FOLDING: dict[str, str] = {}
for _original, _norm in _LETTER_FOLDING.items():
    _REVERSE_FOLDING.setdefault(_norm, set()).add(_original)
for _norm in list(_REVERSE_FOLDING):
    _REVERSE_FOLDING[_norm].add(_norm)  # the normalized form is always itself a valid original

_REVERSE_DIGITS: dict[str, str] = {}
for _original, _digit in _DIGIT_FOLDING.items():
    _REVERSE_DIGITS.setdefault(_digit, set()).add(_original)
for _digit in list(_REVERSE_DIGITS):
    _REVERSE_DIGITS[_digit].add(_digit)

_MARK_GAP = f"[{_TASHKEEL}{_TATWEEL}{_INVISIBLES}]*"


def build_match_pattern(normalized_query: str) -> re.Pattern[str]:
    """Build a regex that finds `normalized_query` (already NFC + folded + lowercased,
    i.e. the output of `normalize()`) directly in ORIGINAL, undiacritized-in-neither-
    direction source text.

    One alternation group per significant character (covering every original spelling
    that folds to it), with an optional run of marks allowed between each — so
    `normalize("الامام")` matches "اَلْإِمَامُ" in the real, displayed page text at
    whatever exact position it occurs, without ever normalizing (and thereby losing
    the tashkeel of) that displayed text itself.

    Multi-word queries are joined on whitespace with a flexible-whitespace separator,
    matching how `normalize()` collapses whitespace runs before the SQL side ever sees
    the query — so a query that matched as a phrase via phraseto_tsquery finds the same
    phrase here.
    """
    words = normalized_query.split(" ")
    word_patterns = []
    for word in words:
        char_patterns = []
        for ch in word:
            if ch in _REVERSE_FOLDING:
                options = "".join(sorted(_REVERSE_FOLDING[ch]))
                char_patterns.append(f"[{re.escape(options)}]")
            elif ch in _REVERSE_DIGITS:
                options = "".join(sorted(_REVERSE_DIGITS[ch]))
                char_patterns.append(f"[{re.escape(options)}]")
            else:
                char_patterns.append(re.escape(ch))
        word_patterns.append(_MARK_GAP.join(char_patterns))
    # Between words: at least one whitespace/mark character, collapsed by normalize()
    # from what could be any run of real whitespace in the original.
    pattern = rf"[\s{_TASHKEEL}{_TATWEEL}]+".join(word_patterns)
    return re.compile(pattern)


def find_original_match(original_text: str, normalized_query: str) -> re.Match[str] | None:
    """Locate `normalized_query`'s first occurrence in `original_text`, returning a
    Match over the ORIGINAL string's own coordinates (so callers can slice `original_text`
    directly for a snippet). None if the SQL/GIN side's tokenization diverged enough from
    this regex that no direct match is found — callers should fall back to showing the
    start of the page rather than fail the whole search result.
    """
    if not normalized_query.strip():
        return None
    return build_match_pattern(normalized_query).search(original_text)
