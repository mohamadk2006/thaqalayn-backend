"""Question embedder for the chatbot: BGE-M3 kept loaded, one small HTTP endpoint.

The library's vectors in Qdrant were made with BGE-M3 (1024 numbers, normalized, plain
chunk text), so a question must be embedded by the same model to be compared with them.
Runs as its own container on the server's internal network (see Dockerfile.embedder),
never inside the API: the model needs ~2.2 GB of RAM and torch, which the API image
should not carry.

    POST /embed   {"texts": ["..."]}  ->  {"vectors": [[1024 floats]], "ms": 412}
    GET  /health  ->  {"status": "ok"}

Standard library HTTP; one request embeds at a time (a lock) so a burst queues instead of
multiplying the model's memory and CPU.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
from sentence_transformers import SentenceTransformer

MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
PORT = int(os.environ.get("PORT", "8080"))
MAX_TEXTS = 16
MAX_CHARS = 4000

torch.set_num_threads(int(os.environ.get("EMBED_THREADS", "2")))
model = SentenceTransformer(
    MODEL, device="cpu", cache_folder=os.environ.get("MODEL_CACHE", "/models")
)
model.max_seq_length = 512
model.encode(["تهيئة"])  # warm-up, so the first real question is not the slow one
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, {"status": "ok", "model": MODEL})
        else:
            self._send(404, {})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/embed":
            return self._send(404, {})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))))
            texts = body["texts"]
            if not (
                isinstance(texts, list)
                and 0 < len(texts) <= MAX_TEXTS
                and all(isinstance(t, str) and t.strip() for t in texts)
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return self._send(
                400, {"error": f'send {{"texts": [1..{MAX_TEXTS} non-empty strings]}}'}
            )
        t = time.time()
        with lock:
            vectors = model.encode(
                [s[:MAX_CHARS] for s in texts], normalize_embeddings=True, convert_to_numpy=True
            )
        self._send(200, {"vectors": vectors.tolist(), "ms": round((time.time() - t) * 1000)})

    def log_message(self, *args) -> None:  # requests are logged by the API, with the question
        pass


if __name__ == "__main__":
    print(f"embedder ready: {MODEL} on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
