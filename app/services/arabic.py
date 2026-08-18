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
