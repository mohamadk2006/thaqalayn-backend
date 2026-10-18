"""Embedding worker: claim a book on the server, download it, embed it with BGE-M3, upload the vectors.

Three stages run at the same time, so a slow connection does not stall the GPU:
    fetchers  (three) claim the next books and download them (gzipped), keeping a few books ready
    main      cuts the book into chunks (embed_pages.page_chunks) and embeds them
    uploader  sends the vectors (float16, 1024 per chunk) back; the server marks the book done

It talks to the server only through ssh, with a key that can run nothing but the gate script
(scripts/embeddings/worker/server/gate.sh). Restarting is safe: a book is done only when its vectors
have arrived, and unfinished claims are released at start.

    sh run.sh                 # normal use
    sh run.sh --limit 2       # rehearsal: only two books
    touch STOP                # finish the current books and stop
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
IDENTITY = HERE / "identity"
HOST = "187.127.73.238"
NAME = (IDENTITY / "name.txt").read_text().strip()
CHUNKER = hashlib.sha1((HERE / "embed_pages.py").read_bytes()).hexdigest()[:8]
STOP_FILE = HERE / "STOP"
PERMANENT = (1, 2, 3)  # gate answers that retrying will not change (not allowed, bad argument, no such book)

import embed_pages as ep  # noqa: E402  (the chunker, shared with the server's loader)

stop = threading.Event()
lock = threading.Lock()
stats = {"chunks": 0, "books": 0, "failed": 0, "up_bytes": 0, "down_bytes": 0, "started": time.time(), "current": ""}


def log(message: str) -> None:
    line = f"{time.strftime('%F %T')} {message}"
    print(line, flush=True)
    try:
        with (HERE / "worker.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def ssh(command: str, stdin: bytes | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/usr/bin/ssh", "-i", str(IDENTITY / "key"), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
         "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
         "-o", f"UserKnownHostsFile={IDENTITY / 'known_hosts'}", "-o", "StrictHostKeyChecking=yes",
         # one connection is kept open and reused: opening a new one for every step is slow on a weak link
         "-o", "ControlMaster=auto", "-o", f"ControlPath=/tmp/emb-{NAME}-%C", "-o", "ControlPersist=600",
         f"root@{HOST}", command],
        input=stdin, capture_output=True, timeout=timeout)


def call(command: str, stdin: bytes | None = None, what: str = "", timeout: int = 900) -> subprocess.CompletedProcess:
    """Runs a gate command; retries (with a growing pause) until it works. Returns early only for answers
    that retrying cannot change."""
    delay = 5
    while True:
        try:
            r = ssh(command, stdin, timeout)
            if r.returncode == 0 or r.returncode in PERMANENT:
                return r
            log(f"{what or command}: exit {r.returncode} {r.stdout[-40:]!r} {r.stderr[-100:]!r}; retrying in {delay}s")
        except subprocess.TimeoutExpired:
            log(f"{what or command}: timed out; retrying in {delay}s")
        time.sleep(delay)
        delay = min(delay * 2, 300)


FETCHERS = 3  # books are claimed and downloaded this many at a time, so the waiting overlaps


def fetcher(books: queue.Queue, limit: int, state: dict) -> None:
    try:
        while not stop.is_set() and not STOP_FILE.exists():
            with lock:
                if limit and state["claimed"] >= limit:
                    break
                state["claimed"] += 1  # reserved before asking; given back if nothing was claimed
            t0 = time.time()
            word = call(f"claim {NAME}", what="claim").stdout.decode().strip()
            t_claim = time.time() - t0
            if not word.isdigit():
                with lock:
                    state["claimed"] -= 1
                if word == "DONE":
                    log("the server has no more books to give")
                    break
                if word == "PAUSE":
                    log("paused by the server; checking again in 60s")
                    time.sleep(60)
                else:
                    log(f"unexpected answer to claim: {word!r}")
                    time.sleep(30)
                continue
            book_id = int(word)
            t0 = time.time()
            r = call(f"fetch {book_id}", what=f"fetch {book_id}", timeout=1800)
            log(f"book {book_id}: claimed in {t_claim:.1f}s, downloaded {len(r.stdout) / 1e3:.0f} KB in {time.time() - t0:.1f}s")
            try:
                if r.returncode != 0:
                    raise ValueError(f"fetch exit {r.returncode}")
                book = json.loads(gzip.decompress(r.stdout))
            except Exception as exc:  # noqa: BLE001 -- a bad book is given up, not fatal
                log(f"book {book_id}: cannot read it ({exc}); giving it up")
                call(f"fail {book_id} {NAME}")
                continue
            with lock:
                stats["down_bytes"] += len(r.stdout)
            books.put((book_id, book))  # waits while the embedder is behind
    finally:
        with lock:
            state["active"] -= 1
            last = state["active"] == 0
        if last:
            books.put(None)


def uploader(vectors: queue.Queue) -> None:
    while True:
        item = vectors.get()
        if item is None:
            return
        book_id, n, data = item
        while True:
            r = call(f"put {book_id} {n} {CHUNKER} {NAME}", stdin=data, what=f"put {book_id}", timeout=3600)
            if r.returncode == 0 and r.stdout.decode().strip() == "OK":
                break
            log(f"put {book_id}: {r.stdout!r} {r.stderr[-100:]!r}; retrying in 60s")
            time.sleep(60)
        with lock:
            stats["up_bytes"] += len(data)
            stats["books"] += 1
        log(f"book {book_id}: {n} chunks uploaded ({len(data) / 1e6:.1f} MB)")


def heartbeat(books: queue.Queue, vectors: queue.Queue) -> None:
    while not stop.wait(60):
        with lock:
            s = dict(stats)
        rate = s["chunks"] / max(1.0, time.time() - s["started"])
        text = (f"books={s['books']} chunks={s['chunks']} rate={rate:.1f}/s failed={s['failed']} "
                f"up_mb={s['up_bytes'] / 1e6:.0f} down_mb={s['down_bytes'] / 1e6:.0f} "
                f"waiting_books={books.qsize()} waiting_uploads={vectors.qsize()} now={s['current']}")
        try:
            ssh(f"beat {NAME} {text}", timeout=60)
        except Exception:  # noqa: BLE001 -- a missed heartbeat is harmless
            pass
        log("progress: " + text)


def tune(model, texts: list[str], device: str) -> int:
    """The fastest batch size on this machine, tried on the first big book. A big GPU needs bigger batches."""
    import torch

    def sync() -> None:
        if device == "mps":
            torch.mps.synchronize()
        elif device == "cuda":
            torch.cuda.synchronize()

    probe = texts[:256]
    model.encode(probe[:48], batch_size=16, convert_to_numpy=True, show_progress_bar=False)  # warm-up
    best = (0.0, 16)
    for batch in (8, 16, 32, 64, 128):
        try:
            t = time.time()
            model.encode(probe, batch_size=batch, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
            sync()
            rate = len(probe) / (time.time() - t)
        except Exception:  # noqa: BLE001 -- too big for the memory: stop growing
            break
        log(f"batch size {batch}: {rate:.0f} chunks/s")
        if rate > best[0] * 1.03:
            best = (rate, batch)
    log(f"using batch size {best[1]}")
    return best[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="stop after claiming this many books (rehearsal)")
    parser.add_argument("--batch", type=int, default=0, help="batch size; 0 = pick the fastest on the first big book")
    args = parser.parse_args()

    import torch
    from sentence_transformers import SentenceTransformer

    device = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
    log(f"worker {NAME} | device {device} | torch {torch.__version__} | chunker {CHUNKER}")
    model = SentenceTransformer(str(HERE / "model"), device=device)
    model.max_seq_length = 512
    if device != "cpu":
        model.half()

    status = call("status", what="status").stdout.decode().strip().replace("\n", " | ")
    log(f"server: {status}")
    call(f"release {NAME}", what="release")

    books: queue.Queue = queue.Queue(maxsize=4)
    vectors: queue.Queue = queue.Queue(maxsize=6)
    state = {"claimed": 0, "active": FETCHERS}
    threads = [threading.Thread(target=uploader, args=(vectors,)),
               threading.Thread(target=heartbeat, args=(books, vectors), daemon=True)]
    threads += [threading.Thread(target=fetcher, args=(books, args.limit, state), daemon=True) for _ in range(FETCHERS)]
    for t in threads:
        t.start()

    batch = args.batch
    done_books = 0
    while True:
        item = books.get()
        if item is None:
            break
        book_id, book = item
        with lock:
            stats["current"] = str(book_id)
        t_embed = time.time()
        try:
            texts = [p["payload"]["text"] for p in ep.book_points(book)]
            if texts:
                if not batch and len(texts) >= 320:
                    batch = tune(model, texts, device)
                    t_embed = time.time()
                try:
                    out = model.encode(texts, batch_size=batch or 16, normalize_embeddings=True, convert_to_numpy=True,
                                       show_progress_bar=False)
                except Exception:  # noqa: BLE001 -- once more with smaller batches (memory)
                    out = model.encode(texts, batch_size=4, normalize_embeddings=True, convert_to_numpy=True,
                                       show_progress_bar=False)
                data = np.ascontiguousarray(out.astype(np.float16)).tobytes()
                assert len(data) == len(texts) * 2048, "unexpected vector size"
            else:
                data = b""
        except Exception as exc:  # noqa: BLE001 -- one bad book must not stop the night
            log(f"book {book_id}: embedding failed ({type(exc).__name__}: {exc}); giving it up")
            call(f"fail {book_id} {NAME}")
            with lock:
                stats["failed"] += 1
            continue
        with lock:
            stats["chunks"] += len(texts)
        log(f"book {book_id}: {len(texts)} chunks embedded in {time.time() - t_embed:.1f}s")
        vectors.put((book_id, len(texts), data))
        done_books += 1
        if done_books % 5 == 0 and device == "mps":
            torch.mps.empty_cache()
    vectors.put(None)
    threads[0].join()
    stop.set()
    seconds = time.time() - stats["started"]
    log(f"finished: {stats['books']} books, {stats['chunks']} chunks, {stats['failed']} failed, "
        f"{seconds / 3600:.1f} h, {stats['chunks'] / max(1.0, seconds):.1f} chunks/s")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped by you (Ctrl-C); unfinished books are released at the next start")
        sys.exit(130)
