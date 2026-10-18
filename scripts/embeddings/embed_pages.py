"""Vector search pilot: embed books' pages with Gemini and keep them in Qdrant.

    python embed_pages.py index --books 3648,1877      # (or --books-file ids.txt) embed and upsert
    python embed_pages.py ask "ما فضل زيارة الحسين"     # nearest chunks to a question
    python embed_pages.py status                         # what the collection holds

Reads GEMINI_API_KEY, QDRANT_URL and QDRANT_API_KEY from the environment (never from a file in the
repo). A page is cut into chunks of about CHUNK_WORDS words (never across pages); a chunk's point id is
derived from (book, page sequence, chunk number), so a run can be repeated or resumed safely.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

MODEL = "gemini-embedding-2"
DIMENSIONS = 768
COLLECTION = os.environ.get("QDRANT_COLLECTION", "library_pilot")
CHUNK_WORDS = 220
MIN_WORDS = 6  # a chunk shorter than this (a page number, a lone line) is not worth a vector
BATCH = 32
# The free tier allows 100 embedding requests a minute (each text in a batch is one). 0 = no pacing (paid).
PER_MINUTE = int(os.environ.get("EMBED_PER_MINUTE", "95"))
GEMINI = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}"
BOOKS_ROOT = Path(os.environ.get("BOOKS_ROOT", "/app/data/books"))
_MARKS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")  # tashkeel, tatweel
_SPACES = re.compile(r"\s+")


def clean(text: str) -> str:
    return _SPACES.sub(" ", _MARKS.sub("", text)).strip()


def page_chunks(page: dict, size: int = CHUNK_WORDS) -> list[str]:
    """The page's headings and text, grouped paragraph by paragraph up to `size` words. A paragraph
    longer than `size` is split by words. Footnotes are left out."""
    blocks = [clean(b.get("text") or "") for b in page.get("blocks", []) if b.get("type") in ("heading", "text")]
    chunks: list[list[str]] = []
    current: list[str] = []
    for block in (b for b in blocks if b):
        words = block.split()
        if len(words) > size:
            if current:
                chunks.append(current)
                current = []
            for i in range(0, len(words), size):
                chunks.append(words[i:i + size])
            continue
        if len(current) + len(words) > size and current:
            chunks.append(current)
            current = []
        current += words
    if current:
        chunks.append(current)
    return [" ".join(c) for c in chunks if len(c) >= MIN_WORDS]


def book_points(book: dict) -> list[dict]:
    """Everything to embed for one book: {id, doc, payload} per chunk."""
    meta = book.get("metadata") or {}
    volume = meta.get("volume")
    title = clean(book.get("title") or "") or "none"
    points = []
    for page in book.get("pages", []):
        if page.get("isBlank"):
            continue
        for n, text in enumerate(page_chunks(page)):
            points.append({
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"thaqalayn:{book['bookId']}:{page['sequence']}:{n}")),
                "doc": f"title: {title} | text: {text}",
                "payload": {"bookId": book["bookId"], "title": book.get("title"), "volume": volume,
                            "authorDeath": meta.get("authorDeath"), "sequence": page["sequence"],
                            "page": page.get("pageNumber"), "chunk": n, "text": text},
            })
    return points


def http(method: str, url: str, headers: dict, body: dict | None = None) -> tuple[int, dict, str]:
    """One JSON request with the standard library (the API image has no httpx).
    Returns (status, response headers, response text)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


class Gemini:
    def __init__(self, key: str) -> None:
        self.headers = {"x-goog-api-key": key}
        self.sent: list[tuple[float, int]] = []  # (time, texts) of the last minute

    def _pace(self, n: int) -> None:
        while PER_MINUTE:
            now = time.time()
            self.sent = [(t, k) for t, k in self.sent if now - t < 60]
            if sum(k for _, k in self.sent) + n <= PER_MINUTE:
                break
            time.sleep(max(1.0, 60 - (now - self.sent[0][0]) + 0.5))
        self.sent.append((time.time(), n))

    def embed(self, texts: list[str]) -> list[list[float]]:
        body = {"requests": [{"model": f"models/{MODEL}", "content": {"parts": [{"text": t}]},
                              "outputDimensionality": DIMENSIONS} for t in texts]}
        for attempt in range(8):
            self._pace(len(texts))
            status, headers, text = http("POST", f"{GEMINI}:batchEmbedContents", self.headers, body)
            if status == 200:
                vectors = [e["values"] for e in json.loads(text)["embeddings"]]
                if len(vectors) != len(texts):
                    raise RuntimeError(f"asked for {len(texts)} vectors, got {len(vectors)}")
                return vectors
            if status in (429, 500, 503):
                told = re.search(r"retry in ([0-9.]+)s", text)
                wait = float(headers.get("Retry-After") or (float(told.group(1)) + 1 if told else min(60, 2 ** attempt * 2)))
                print(f"  gemini {status}: waiting {wait:.0f}s", file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError(f"gemini {status}: {text[:300]}")
        raise RuntimeError("gemini: still rate-limited after 8 tries")


class Qdrant:
    def __init__(self, url: str, key: str) -> None:
        self.base, self.headers = url.rstrip("/"), {"api-key": key}

    def call(self, method: str, path: str, body: dict | None = None, ok_missing: bool = False) -> dict | None:
        status, _, text = http(method, self.base + path, self.headers, body)
        if status == 404 and ok_missing:
            return None
        if status >= 400:
            raise RuntimeError(f"qdrant {method} {path}: {status} {text[:300]}")
        return json.loads(text)

    def ensure_collection(self) -> None:
        if self.call("GET", f"/collections/{COLLECTION}", ok_missing=True) is not None:
            return
        self.call("PUT", f"/collections/{COLLECTION}", {"vectors": {"size": DIMENSIONS, "distance": "Cosine"}})
        self.call("PUT", f"/collections/{COLLECTION}/index", {"field_name": "bookId", "field_schema": "integer"})

    def count(self, book_id: int | None = None) -> int:
        body: dict = {"exact": True}
        if book_id is not None:
            body["filter"] = {"must": [{"key": "bookId", "match": {"value": book_id}}]}
        found = self.call("POST", f"/collections/{COLLECTION}/points/count", body, ok_missing=True)
        return found["result"]["count"] if found else 0

    def upsert(self, points: list[dict], vectors: list[list[float]]) -> None:
        body = {"points": [{"id": p["id"], "vector": v, "payload": p["payload"]} for p, v in zip(points, vectors)]}
        self.call("PUT", f"/collections/{COLLECTION}/points?wait=true", body)

    def search(self, vector: list[float], limit: int) -> list[dict]:
        return self.call("POST", f"/collections/{COLLECTION}/points/search",
                         {"vector": vector, "limit": limit, "with_payload": True})["result"]


def connect() -> tuple[Gemini, Qdrant]:
    need = ["GEMINI_API_KEY", "QDRANT_URL", "QDRANT_API_KEY"]
    missing = [n for n in need if not os.environ.get(n)]
    if missing:
        sys.exit(f"missing environment: {', '.join(missing)}")
    return Gemini(os.environ["GEMINI_API_KEY"]), Qdrant(os.environ["QDRANT_URL"], os.environ["QDRANT_API_KEY"])


def index(book_ids: list[int]) -> None:
    gemini, qdrant = connect()
    qdrant.ensure_collection()
    started = time.time()
    for book_id in book_ids:
        path = BOOKS_ROOT / f"{book_id}.json"
        if not path.exists():
            print(f"book {book_id}: no file, skipped")
            continue
        points = book_points(json.loads(path.read_text(encoding="utf-8")))
        have = qdrant.count(book_id)
        if have >= len(points):
            print(f"book {book_id}: {have}/{len(points)} already there, skipped")
            continue
        for i in range(0, len(points), BATCH):
            batch = points[i:i + BATCH]
            qdrant.upsert(batch, gemini.embed([p["doc"] for p in batch]))
        print(f"book {book_id}: {len(points)} chunks embedded ({time.time() - started:.0f}s so far)", flush=True)
    print(f"collection {COLLECTION}: {qdrant.count()} points")


def ask(question: str, limit: int) -> None:
    gemini, qdrant = connect()
    vector = gemini.embed([f"task: search result | query: {clean(question)}"])[0]
    for hit in qdrant.search(vector, limit):
        p = hit["payload"]
        vol = f" ج{p['volume']}" if p.get("volume") else ""
        print(f"{hit['score']:.3f}  {p['title']}{vol} ص{p['page']}  (book {p['bookId']}, seq {p['sequence']})")
        print(f"       {p['text'][:200]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    idx = sub.add_parser("index")
    idx.add_argument("--books", default="")
    idx.add_argument("--books-file")
    q = sub.add_parser("ask")
    q.add_argument("question")
    q.add_argument("--limit", type=int, default=8)
    sub.add_parser("status")
    args = parser.parse_args()
    if args.cmd == "index":
        raw = args.books + "," + (Path(args.books_file).read_text() if args.books_file else "")
        index([int(x) for x in re.split(r"[,\s]+", raw) if x])
    elif args.cmd == "ask":
        ask(args.question, args.limit)
    else:
        print(connect()[1].count(), "points in", COLLECTION)


if __name__ == "__main__":
    main()
