# Embedding worker (Mac, fully offline)

Takes books from the Thaqalayn server, embeds them with BGE-M3, and sends the vectors back. Everything it
needs is inside this folder (Python, libraries, model, a restricted key): nothing to install or download.

## Start (one command)

Open **Terminal**, then:

    cd worker && sh run.sh

(If you have only the zip: `unzip embedding-worker-<machine>.zip && cd worker && sh run.sh`.)

Leave it running. It keeps the Mac awake, restarts itself after a crash, and stops by itself when the
server has no books left. You will see a line per finished book, and a "progress:" line every minute.

## Stop / restart

- Stop: press `Ctrl-C`, or in another Terminal tab run `touch STOP` inside this folder (it finishes its books first).
- Start again any time with `sh run.sh`: finished books are never redone; the unfinished one is released and redone.

## Notes

- Apple silicon Macs only. Plug the Mac in. A slow or dropped connection is fine: it retries until it works.
- Uploads go to a folder on the server, not into the live search. Nothing here can change the website.
- The key in `identity/` can only claim books, download book files and upload vectors (a gate script on the server
  allows nothing else). Delete this folder when the job is finished.
