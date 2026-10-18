"""Server-side loader: puts the vectors that the Mac workers uploaded (/embed/inbox) into Qdrant.

Runs in its own small container (see run_loader.sh), never inside the live API: low CPU/memory limits, one book at a
time, pauses while the server is busy. Standard library only. For each finished book it
  1. reads inbox/<id>.meta and checks the chunker hash (same chunking as the worker) and the byte count,
  2. cuts the book file again with embed_pages.book_points and checks the chunk count is the same,
  3. upserts one point per chunk (id, 1024 float16 -> floats, payload with the chunk text) and marks loaded/<id>.
Resumable: books with a loaded/<id> marker are skipped. Books are taken in manifest order.

    python loader.py --watch --max-books 1000     # keep loading new books, stop after 1000 loaded
    python loader.py --max-books 3                # a few, then stop
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import time
import urllib.request
from pathlib import Path

import embed_pages as ep

BASE = Path(os.environ.get("EMBED_BASE", "/embed"))
BOOKS = Path(os.environ.get("BOOKS_ROOT", "/books"))
COLLECTION = os.environ.get("COLLECTION", "library_bge_m3")
QDRANT = os.environ["QDRANT_URL"].rstrip("/")
HEADERS = {"api-key": os.environ["QDRANT_API_KEY"]}
DIM = 1024
BATCH = 128
MAX_LOAD = float(os.environ.get("MAX_LOAD", "2.0"))  # 1-minute load average above which we wait (the server has 4 CPUs)
CHUNKER = hashlib.sha1((Path(ep.__file__)).read_bytes()).hexdigest()[:8]


def log(message: str) -> None:
    print(f"{time.strftime('%F %T')} {message}", flush=True)


def qdrant(method: str, path: str, body: dict | None = None, missing_ok: bool = False) -> dict | None:
    status, _, text = ep.http(method, QDRANT + path, HEADERS, body)
    if status == 404 and missing_ok:
        return None
    if status >= 400:
        raise RuntimeError(f"qdrant {method} {path}: {status} {text[:300]}")
    return json.loads(text)


def ensure_collection() -> None:
    if qdrant("GET", f"/collections/{COLLECTION}", missing_ok=True) is not None:
        return
    qdrant("PUT", f"/collections/{COLLECTION}", {
        "vectors": {"size": DIM, "distance": "Cosine", "on_disk": True},
        "hnsw_config": {"m": 16, "ef_construct": 100},
        "optimizers_config": {"max_optimization_threads": 1},
        "on_disk_payload": True,
    })
    qdrant("PUT", f"/collections/{COLLECTION}/index", {"field_name": "bookId", "field_schema": "integer"})
    log(f"collection {COLLECTION} created")


API_PROBE = os.environ.get("API_PROBE", "https://api.thaqalaynlibrary.com/api/search?q=%D8%AE%D8%B2%D8%A7%D8%A6%D9%86")
MAX_SEARCH_S = float(os.environ.get("MAX_SEARCH_S", "2.5"))  # a live search slower than this pauses the loader
_probe = {"at": 0.0, "ok": True}


def live_search_is_fast() -> bool:
    """The site comes first: time a real search (at most every 10 s) and pause while it is slow."""
    if time.time() - _probe["at"] >= 10:
        t = time.time()
        try:
            urllib.request.urlopen(API_PROBE, timeout=40).read()
            _probe["ok"] = time.time() - t <= MAX_SEARCH_S
        except Exception:  # noqa: BLE001 -- an unanswered probe counts as slow
            _probe["ok"] = False
        _probe["at"] = time.time()
        if not _probe["ok"]:
            log(f"the live search took {time.time() - t:.1f}s (limit {MAX_SEARCH_S}s)")
    return _probe["ok"]


def wait_for_quiet_server() -> None:
    while os.getloadavg()[0] > MAX_LOAD or not live_search_is_fast():
        log(f"server busy (load {os.getloadavg()[0]:.1f}); waiting")
        time.sleep(30)


def read_meta(book_id: str) -> dict:
    return dict(kv.split("=", 1) for kv in (BASE / "inbox" / f"{book_id}.meta").read_text().split())


def load_book(book_id: str) -> int:
    meta = read_meta(book_id)
    n = int(meta["chunks"])
    if meta["chunker"] != CHUNKER:
        raise ValueError(f"chunker {meta['chunker']} differs from {CHUNKER}")
    data = (BASE / "inbox" / f"{book_id}.f16").read_bytes()
    if len(data) != n * DIM * 2:
        raise ValueError(f"{len(data)} bytes for {n} chunks")
    points = ep.book_points(json.loads((BOOKS / f"{book_id}.json").read_text(encoding="utf-8")))
    if len(points) != n:
        raise ValueError(f"book now cuts into {len(points)} chunks, vectors are for {n}")
    for a in range(0, n, BATCH):
        part = points[a:a + BATCH]
        floats = struct.unpack(f"<{len(part) * DIM}e", data[a * DIM * 2:(a + len(part)) * DIM * 2])
        qdrant("PUT", f"/collections/{COLLECTION}/points?wait=true", {"points": [
            {"id": p["id"], "vector": list(floats[i * DIM:(i + 1) * DIM]), "payload": p["payload"]}
            for i, p in enumerate(part)]})
        time.sleep(0.05)
    return n


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="keep waiting for new books")
    parser.add_argument("--max-books", type=int, default=0, help="stop once this many books are loaded in total")
    args = parser.parse_args()
    (BASE / "loaded").mkdir(exist_ok=True)
    ensure_collection()
    manifest = (BASE / "manifest.txt").read_text().split()
    log(f"loader started; chunker {CHUNKER}; collection {COLLECTION}")
    started = time.time()
    chunks = 0
    while True:
        loaded = {p.name for p in (BASE / "loaded").iterdir()}
        if args.max_books and len(loaded) >= args.max_books:
            log(f"{len(loaded)} books loaded: the limit is reached")
            return
        ready = [b for b in manifest if (BASE / "inbox" / f"{b}.f16").exists() and b not in loaded
                 and not (BASE / "loaded" / f"{b}.bad").exists()]
        if not ready:
            if not args.watch:
                break
            time.sleep(60)
            continue
        book_id = ready[0]
        wait_for_quiet_server()
        t = time.time()
        try:
            n = load_book(book_id)
        except Exception as exc:  # noqa: BLE001 -- a book that cannot be loaded is set aside, not retried forever
            log(f"book {book_id}: NOT loaded ({type(exc).__name__}: {exc})")
            (BASE / "loaded" / f"{book_id}.bad").write_text(str(exc))
            continue
        (BASE / "loaded" / book_id).write_text(str(int(time.time())))
        chunks += n
        log(f"book {book_id}: {n} chunks loaded in {time.time() - t:.1f}s "
            f"(total {chunks} chunks, {chunks / max(1.0, time.time() - started):.0f}/s)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
