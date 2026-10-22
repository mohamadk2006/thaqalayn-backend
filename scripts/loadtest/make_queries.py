"""Builds the load test's search pool from the library's own text: real words and phrases,
common and rare, so few searches repeat and the result cache can't flatter the test.

    python scripts/loadtest/make_queries.py [BOOKS_ROOT] [-o scripts/loadtest/queries.json]

From a random sample of books: words picked by how often they occur (the common ones,
as most real searches), words picked evenly from the vocabulary (mostly rare ones), and
2-4 word phrases cut from random pages.
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import re
from pathlib import Path

MARKS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")
WORD = re.compile(r"[ء-ي]{3,12}")


def page_words(page: dict) -> list[str]:
    text = " ".join(b.get("text", "") for b in page.get("blocks", []) if b.get("type") in ("text", "heading"))
    return WORD.findall(MARKS.sub("", text))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("books", nargs="?", default="data/books", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path(__file__).with_name("queries.json"))
    ap.add_argument("--books-sampled", type=int, default=400)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rnd = random.Random(args.seed)

    files = sorted(args.books.glob("*.json"))
    counts: collections.Counter = collections.Counter()
    pages: list[list[str]] = []
    for path in rnd.sample(files, min(args.books_sampled, len(files))):
        try:
            book = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for page in book.get("pages", []):
            words = page_words(page)
            if len(words) >= 8:
                counts.update(words)
                pages.append(words)

    vocab = list(counts)
    weights = [counts[w] for w in vocab]
    common = set(rnd.choices(vocab, weights=weights, k=1200))     # as words occur: common
    rare = set(rnd.sample(vocab, min(1500, len(vocab))))           # evenly: mostly rare
    phrases = set()
    while len(phrases) < 1200:
        words = rnd.choice(pages)
        n = rnd.choice((2, 2, 3, 3, 4))
        start = rnd.randrange(0, len(words) - n)
        phrases.add(" ".join(words[start:start + n]))

    out = {"common": sorted(common), "rare": sorted(rare - common), "phrase": sorted(phrases)}
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"{len(pages)} pages from {args.books_sampled} books: "
          + ", ".join(f"{k} {len(v)}" for k, v in out.items()) + f" -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
