# Embedding speed test (BGE-M3) — Mac, fully offline

Measures how many Arabic book chunks per second this Mac embeds, to estimate the time for the whole library
(about 13.8 million chunks). Everything it needs is in this folder (Python, libraries, model, data):
no internet, no installation. Nothing is uploaded.

## Run it

1. Copy this whole `benchmark` folder to the Mac Studio (if it came as a zip, unzip it in Terminal: `unzip embedding-benchmark.zip`).
2. Open **Terminal**, type `cd ` (with a space), drag the `benchmark` folder into the window, press Enter.
3. Run:

```sh
sh run.sh
```

Takes about 3 to 10 minutes. Keep the Mac awake and plugged in. For a shorter first try: `sh run.sh --n 500`.

## What to send back

The last lines of the output (from `=== RESULT ===`), or the file `result_<machine>.json` written in this folder.

Reference, same 3,000 chunks on a MacBook with an Apple M5 (16 GB): **70 chunks per second** (about 55 hours for the library).

## Notes

- Apple silicon only (M1 and newer). The bundled Python and libraries are for `macosx arm64`.
- Do not commit this folder (2.4 GB, holds library text): `.gitignore` already excludes the big parts.
