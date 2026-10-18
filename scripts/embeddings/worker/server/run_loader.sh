#!/bin/sh
# Starts the Qdrant loader as its own small, low-priority container on the server (run as root there).
#   run_loader.sh --max-books 3                  # a few books, in the foreground
#   run_loader.sh --watch --max-books 1000 -d    # keep loading in the background (-d must be last)
set -e
DETACH=""
ARGS=""
for a in "$@"; do
  if [ "$a" = "-d" ]; then DETACH="-d"; else ARGS="$ARGS $a"; fi
done
docker rm -f thaqalayn-loader >/dev/null 2>&1 || true
exec docker run $DETACH --name thaqalayn-loader --network coolify \
  --cpus 0.5 --memory 1g --cpu-shares 32 \
  -v /root/embed:/embed -v /data/thaqalayn/books:/books:ro \
  -e QDRANT_URL=http://thaqalayn-qdrant:6333 -e QDRANT_API_KEY="$(cat /root/.qdrant-api-key)" \
  -e COLLECTION="${COLLECTION:-library_bge_m3}" -e PYTHONPATH=/embed \
  python:3.12-slim python -u /embed/loader.py $ARGS
