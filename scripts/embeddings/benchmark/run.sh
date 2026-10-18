#!/bin/sh
# Everything is in this folder: Python, libraries, the BGE-M3 model and the test data. No internet needed.
cd "$(dirname "$0")" || exit 1

# Files copied from another Mac carry a "downloaded from the internet" flag that can stop programs from starting.
xattr -cr . 2>/dev/null
chmod -R u+x python/bin 2>/dev/null

export PYTHONPATH="$PWD/packages"
export PYTHONNOUSERSITE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
exec python/bin/python3 -s bench.py "$@"
