"""How fast does this machine embed Arabic book chunks with BGE-M3?

    .venv/bin/python bench.py            # all 3,000 sample chunks
    .venv/bin/python bench.py --n 1000   # a quicker run

Prints chunks per second and an estimate for the whole library (13.8 million chunks), and writes
result_<machine>.json next to this file. Nothing is uploaded anywhere. The model is read from the
`model/` folder next to this script; if that folder is missing it is downloaded (about 2.3 GB) from Hugging Face.
"""

import argparse
import json
import platform
import socket
import subprocess
import time
from pathlib import Path

import torch
from sentence_transformers import SentenceTransformer

HERE = Path(__file__).resolve().parent
LIBRARY_CHUNKS = 13_800_000  # estimate for all 18,845 books


def machine_name(device: str) -> str:
    try:
        if device == "cuda":
            return torch.cuda.get_device_name(0)
        if platform.system() == "Darwin":
            chip = subprocess.run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
            return chip or "Apple silicon"
    except Exception:  # noqa: BLE001 -- the name is only for the report
        pass
    return platform.processor() or platform.machine()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=3000, help="how many sample chunks to embed")
    parser.add_argument("--device", default="auto", help="auto, cuda, mps or cpu")
    parser.add_argument("--batch", type=int, default=0, help="batch size; 0 = try a few and pick the fastest")
    args = parser.parse_args()

    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
    texts = [json.loads(line)["text"] for line in (HERE / "chunks_sample.jsonl").read_text(encoding="utf-8").splitlines()][:args.n]
    name = machine_name(device)
    print(f"machine: {name} | device: {device} | torch {torch.__version__} | {len(texts)} chunks", flush=True)

    t0 = time.time()
    local = HERE / "model"  # the model shipped next to this script; otherwise it is downloaded from Hugging Face
    model = SentenceTransformer(str(local) if (local / "config.json").exists() else "BAAI/bge-m3", device=device)
    model.max_seq_length = 512
    if device != "cpu":
        model.half()
    print(f"model loaded in {time.time() - t0:.0f}s", flush=True)

    def encode(batch_texts, batch):
        model.encode(batch_texts, batch_size=batch, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
        if device == "cuda":
            torch.cuda.synchronize()
        elif device == "mps":
            torch.mps.synchronize()

    encode(texts[:64], 16)  # warm-up (kernels, caches)
    batch = args.batch
    if not batch:
        best = (0.0, 16)
        for b in (8, 16, 32, 64, 128):
            probe = texts[64:64 + 256]
            try:
                t = time.time()
                encode(probe, b)
                rate = len(probe) / (time.time() - t)
            except Exception as exc:  # noqa: BLE001 -- out of memory at this batch size
                print(f"  batch {b}: failed ({type(exc).__name__})", flush=True)
                break
            print(f"  batch {b}: {rate:.1f} chunks/s", flush=True)
            if rate > best[0]:
                best = (rate, b)
        batch = best[1]
    print(f"using batch size {batch}", flush=True)

    t = time.time()
    done = 0
    for a in range(0, len(texts), 500):
        part = texts[a:a + 500]
        encode(part, batch)
        done += len(part)
        print(f"  {done}/{len(texts)}  {done / (time.time() - t):.1f} chunks/s", flush=True)
    seconds = time.time() - t
    rate = len(texts) / seconds
    hours = LIBRARY_CHUNKS / rate / 3600

    result = {"machine": name, "device": device, "torch": torch.__version__, "chunks": len(texts),
              "seconds": round(seconds, 1), "chunks_per_second": round(rate, 1), "batch": batch,
              "estimated_hours_for_library": round(hours, 1), "estimated_days_for_library": round(hours / 24, 1)}
    (HERE / f"result_{socket.gethostname().split('.')[0]}.json").write_text(json.dumps(result, indent=2))
    print("\n=== RESULT ===")
    print(json.dumps(result, indent=2))
    print(f"\n{rate:.1f} chunks per second -> about {hours:.0f} hours ({hours / 24:.1f} days) for 13.8 million chunks")


if __name__ == "__main__":
    main()
