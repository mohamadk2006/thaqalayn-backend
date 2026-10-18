#!/bin/sh
# Starts the embedding worker. Everything it needs is in this folder (Python, libraries, model, key).
# It restarts itself after a crash and stops by itself when the server has no books left.
cd "$(dirname "$0")" || exit 1

# Files copied from another Mac carry a "downloaded from the internet" flag that can stop programs from starting.
xattr -cr . 2>/dev/null
chmod -R u+x python/bin 2>/dev/null
chmod 600 identity/key 2>/dev/null
rm -f STOP

export PYTHONPATH="$PWD/packages:$PWD"
export PYTHONNOUSERSITE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false

while true; do
  # caffeinate keeps the Mac awake while the worker runs
  /usr/bin/caffeinate -dimsu python/bin/python3 -s worker.py "$@"
  code=$?
  [ "$code" -eq 0 ] && break           # finished, or you asked it to stop
  [ "$code" -eq 130 ] && break         # Ctrl-C
  echo "worker stopped with code $code; starting again in 30 seconds"
  sleep 30
done
