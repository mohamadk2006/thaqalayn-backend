#!/bin/bash
# The ONLY thing the embedding workers' SSH keys can run (forced command in authorized_keys).
# A worker can: claim a book, fetch it, put its vectors, give a claim up, send a heartbeat, ask for the status.
# It cannot get a shell, read anything but book files, or write anywhere but /root/embed.
#
#   claim <name>                    -> next book id to embed | PAUSE | DONE
#   release <name>                  -> gives up this worker's unfinished claims (after a restart)
#   fetch <id>                      -> the book file, gzipped, on stdout
#   put <id> <chunks> <ver> <name>  -> <chunks> * 2048 bytes (float16 x 1024) on stdin; marks the book done
#   fail <id> <name>                -> gives the claim up for good (recorded in failed/)
#   beat <name> <text...>           -> heartbeat
#   status
set -u
BASE=/root/embed
BOOKS=/data/thaqalayn/books
MIN_FREE_KB=$((25 * 1024 * 1024))   # refuse uploads when the disk has less than 25 GB free
STALE_MIN=180                       # a claim with no result after this long is released

read -r -a A <<< "${SSH_ORIGINAL_COMMAND:-}"
cmd=${A[0]:-}
id_ok()   { [[ "${1:-}" =~ ^[0-9]{1,6}$ ]]; }
num_ok()  { [[ "${1:-}" =~ ^[0-9]{1,7}$ ]]; }
name_ok() { [[ "${1:-}" =~ ^[A-Za-z0-9_-]{1,20}$ ]]; }
echo "$(date '+%F %T') ${A[0]:-} ${A[1]:-} ${A[2]:-} ${A[3]:-} ${A[4]:-}" >> "$BASE/gate.log"

case "$cmd" in
claim)
  name_ok "${A[1]:-}" || exit 2
  [ -e "$BASE/PAUSE" ] && { echo PAUSE; exit 0; }
  exec 9> "$BASE/claim.lock"; flock 9
  find "$BASE/claims" -mindepth 1 -maxdepth 1 -mmin +"$STALE_MIN" | while read -r f; do
    [ -e "$BASE/done/$(basename "$f")" ] || rm -rf "$f"
  done
  next=$(awk 'FILENAME==ARGV[1]{s[$1]; next} !($1 in s){print $1; exit}' \
         <(ls "$BASE/claims"; ls "$BASE/done"; ls "$BASE/failed") "$BASE/manifest.txt")
  [ -n "$next" ] || { echo DONE; exit 0; }
  mkdir "$BASE/claims/$next" && echo "${A[1]} $(date +%s)" > "$BASE/claims/$next/owner"
  echo "$next"
  ;;
release)
  name_ok "${A[1]:-}" || exit 2
  exec 9> "$BASE/claim.lock"; flock 9
  for d in "$BASE"/claims/*/; do
    [ -d "$d" ] || continue
    b=$(basename "$d")
    if [ ! -e "$BASE/done/$b" ] && [ "$(cut -d' ' -f1 "$d/owner" 2>/dev/null)" = "${A[1]}" ]; then rm -rf "$d"; fi
  done
  echo OK
  ;;
fetch)
  id_ok "${A[1]:-}" || exit 2
  f="$BOOKS/${A[1]}.json"
  [ -f "$f" ] || exit 3
  exec gzip -c "$f"
  ;;
put)
  id_ok "${A[1]:-}" || exit 2
  num_ok "${A[2]:-}" || exit 2
  name_ok "${A[4]:-}" || exit 2
  [[ "${A[3]:-}" =~ ^[A-Za-z0-9._-]{1,40}$ ]] || exit 2
  free=$(df --output=avail -k "$BASE" | tail -1 | tr -d ' ')
  [ "$free" -gt "$MIN_FREE_KB" ] || { echo BUSY; exit 75; }
  bytes=$(( ${A[2]} * 2048 ))
  tmp="$BASE/inbox/${A[1]}.tmp"
  head -c "$bytes" > "$tmp"
  [ "$(stat -c %s "$tmp")" -eq "$bytes" ] || { rm -f "$tmp"; echo SHORT; exit 4; }
  mv "$tmp" "$BASE/inbox/${A[1]}.f16"
  echo "chunks=${A[2]} chunker=${A[3]} worker=${A[4]} at=$(date +%s)" > "$BASE/inbox/${A[1]}.meta"
  date +%s > "$BASE/done/${A[1]}"
  rm -rf "$BASE/claims/${A[1]}"
  echo OK
  ;;
fail)
  id_ok "${A[1]:-}" || exit 2
  name_ok "${A[2]:-}" || exit 2
  echo "${A[2]} $(date +%s)" >> "$BASE/failed/${A[1]}"
  rm -rf "$BASE/claims/${A[1]}"
  echo OK
  ;;
beat)
  name_ok "${A[1]:-}" || exit 2
  echo "$(date +%s) $(echo "${A[*]:2}" | tr -cd 'A-Za-z0-9 ._=,/-' | cut -c1-200)" > "$BASE/heartbeat/${A[1]}"
  echo OK
  ;;
status)
  echo "manifest=$(wc -l < "$BASE/manifest.txt") claimed=$(ls "$BASE/claims" | wc -l) done=$(ls "$BASE/done" | wc -l) failed=$(ls "$BASE/failed" | wc -l)"
  echo "inbox_gb=$(du -s --block-size=1G "$BASE/inbox" | cut -f1) free_gb=$(( $(df --output=avail -k "$BASE" | tail -1) / 1048576 )) paused=$([ -e "$BASE/PAUSE" ] && echo yes || echo no)"
  ;;
*)
  echo "not allowed" >&2
  exit 1
  ;;
esac
