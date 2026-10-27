"""Spell out honorifics that a converted book still shows as plain Latin letters.

Books typeset in the "Basmalla" font draw «عليه السلام» and the like from the letters N, C, S,
X, J, R, L and the basmala from "a". The converter reads that font off the Word file
(FONT_HONORIFICS in doc_to_json_v2.py), but a book converted before it knew the font, and
then corrected by hand, cannot be converted again without losing the corrections. This
reads the finished book.json instead: a letter standing alone (no other Latin letter or digit
next to it) is the honorific, and a block whose whole text is "a" is the basmala.

    python scripts/convert/fix_letter_honorifics.py book.json fixed.json

Only `text`, `heading` and `footnotes` blocks and the contents list's titles are changed, and
the same text, page and block ids stay. A letter that is a Qur'anic ornament in a verse
(the "L" after a space, as in "يَخْشَى L﴾") is dropped.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

LETTERS = {
    "N": "عليه السلام",
    "C": "صلى الله عليه وآله وسلم",
    "S": "عليهم السلام",
    "X": "عليها السلام",
    "J": "عليهما السلام",
    "R": "رحمه الله",
    "L": "عز وجل",
}
BASMALA = "بسم الله الرحمن الرحيم"
_ALONE = re.compile(r"(?<![A-Za-z0-9])([NCSXJRL])(?![A-Za-z0-9])")


def spell(text: str, counts: collections.Counter | None = None) -> str:
    """`text` with each lone honorific letter spelt out, spaced like the converter does."""

    def one(m: re.Match) -> str:
        letter = m.group(1)
        before = text[m.start() - 1] if m.start() else ""
        after = text[m.end()] if m.end() < len(text) else ""
        if letter == "L" and before.isspace():  # a verse ornament, not "عز وجل"
            if counts is not None:
                counts["ornament"] += 1
            return ""
        if counts is not None:
            counts[letter] += 1
        lead = " " if before and not before.isspace() and before not in "(«[﴿" else ""
        trail = " " if after and (after.isalnum() or after in "(«﴿") else ""
        return lead + LETTERS[letter] + trail

    out = _ALONE.sub(one, text)
    return re.sub(r" {2,}", " ", out) if out != text else out


def fix_book(book: dict) -> collections.Counter:
    """Changes `book` in place; returns how many of each letter were spelt out."""
    counts: collections.Counter = collections.Counter()
    for page in book.get("pages", []):
        for block in page.get("blocks", []):
            text = block.get("text")
            if not isinstance(text, str):
                continue
            if block.get("type") == "text" and text.strip() == "a":
                block["text"] = BASMALA
                counts["basmala"] += 1
            elif block.get("type") in ("text", "heading", "footnotes"):
                block["text"] = spell(text, counts)
    for entry in book.get("toc", []):
        if isinstance(entry.get("title"), str):
            entry["title"] = spell(entry["title"], counts)
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("book", type=Path)
    ap.add_argument("out", type=Path)
    args = ap.parse_args()
    book = json.loads(args.book.read_text(encoding="utf-8"))
    counts = fix_book(book)
    args.out.write_text(json.dumps(book, ensure_ascii=False, indent=1), encoding="utf-8")
    print(dict(counts), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
