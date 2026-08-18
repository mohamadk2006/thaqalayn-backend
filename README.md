# Thaqalayn Library Backend

Self-hosted catalog, download, and Arabic full-text search API for the Thaqalayn
library (~18,000 books converted from Shamela `.abx` sources).

Runs entirely on one machine — a Mac for development, a Linux VPS later — with no
managed cloud services. Python + FastAPI + PostgreSQL.

## Requirements

- Docker Desktop (PostgreSQL runs in a container)
- [uv](https://docs.astral.sh/uv/) (manages Python and dependencies; installs Python 3.13 itself)

## Setup

```bash
cp .env.example .env      # then set POSTGRES_PASSWORD and match it in DATABASE_URL
uv sync
docker compose up -d
uv run alembic upgrade head
```

## Run

```bash
uv run uvicorn app.main:app --reload
```

- API: http://127.0.0.1:8000/api/health
- Interactive docs: http://127.0.0.1:8000/docs

## Test

```bash
uv run pytest
```

## Layout

```
app/            FastAPI application
  api/          route handlers, mounted under /api
  models/       SQLAlchemy ORM models
  schemas/      Pydantic request/response models — the iOS client contract
  services/     business logic; search_service is the only module that knows
                how search is implemented (PostgreSQL now, possibly Typesense later)
database/
  migrations/   Alembic
  sql/          hand-written SQL (the Arabic normalizer lives here)
scripts/
  convert/      Shamela .abx  →  book JSON
  validate/     JSON validation and reporting
  import/       bulk load into PostgreSQL
  search_index/ index maintenance
data/
  books/        converted book JSON (gitignored, ~45 GB at full scale)
  covers/       cover images (gitignored)
tests/
```

## Configuration

All configuration is environment-based (`.env` locally, real environment variables on
the VPS). `.env` is gitignored and must never be committed.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | asyncpg DSN, used by both the API and Alembic |
| `BOOKS_ROOT` | directory holding book JSON files |
| `COVERS_ROOT` | directory holding cover images |
| `POSTGRES_*` | consumed by docker-compose |
| `API_HOST` / `API_PORT` / `LOG_LEVEL` | server settings |

`BOOKS_ROOT` and `COVERS_ROOT` may be relative — they resolve against the repo root, so
the defaults work unchanged on macOS and Linux. Never put an absolute path in `.env.example`.

## Notes on PostgreSQL

The database is created with `UTF8` encoding and the **ICU** locale provider with locale
`ar`, which gives correct Arabic collation for `ORDER BY` on titles (libc locales get
this wrong).

Full-text **search** deliberately does not depend on that locale. It uses the `simple`
text search configuration over text passed through a project-owned Arabic normalizer, so
that matching behaviour is explicit and testable rather than inherited from whatever
locale data the host happens to ship. See `database/sql/arabic_normalize.sql`.

## Arabic normalization

`arabic_normalize(text)` in PostgreSQL and `app.services.arabic.normalize()` in Python
are the same function written twice. The search index is built by the SQL one and
queried through the Python one, so they must agree byte-for-byte — a divergence would
produce silently wrong search results rather than an error.

`tests/test_arabic_parity.py` enforces agreement across 422 strings of real corpus text
covering every rule: diacriticized text, non-NFC text, Persian letterforms, alef and
ta-marbuta variants, plus titles and author names.

The rules, in order: NFC compose → strip tashkeel/tatweel/zero-width/bidi → fold
`أإآٱ→ا`, `ى→ي`, `ة→ه`, Persian `ک→ك` `ی→ي` `ھۀ→ه` → fold Arabic-Indic and Persian
digits to ASCII → lowercase → collapse whitespace.

Normalization is for **matching only**. Original text keeps its full tashkeel and is
what gets stored, served, and displayed; the source books are never modified.

When changing the rules, edit `database/sql/arabic_normalize.sql` and
`app/services/arabic.py` together, then generate a new Alembic revision. Each revision
embeds its own snapshot of the SQL, so history stays accurate as the file evolves.

The container publishes its port on `127.0.0.1` only. PostgreSQL is never reachable from
outside the host, on the Mac or on the VPS — the API is the only public surface.

## Schema notes

`books` is a **volume**; `works` is a **title**. Shamela splits multi-volume works into
one file per جزء, so the 18,798 usable sources collapse to 9,045 works — بحار الأنوار
alone is 110 files. A book is what gets downloaded, a work is what gets browsed.

The search unit is the **page**, not the paragraph: ~6M rows instead of ~76M for the same
text, matching what a pre-download result needs to say (book, section, page, snippet).
Only original text is stored — the normalized form exists solely inside the generated
`search_tsv` column.

Every NOT NULL column with a default declares `server_default`, because the bulk importer
writes with raw INSERT/COPY rather than the ORM, and Python-side defaults are invisible
to those.

## Milestones

| # | Scope | Status |
|---|---|---|
| 1 | Project skeleton, Compose, migrations, `/api/health` | ✅ done |
| 2 | Arabic normalizer (SQL + Python), with tests | ✅ done |
| 3 | Schema: works, books, authors, categories, sections, pages | ✅ done |
| 4 | Converter + validator + importer, on a small sample | next |
| 5 | `GET /api/books`, `/api/books/{id}`, `/api/books/{id}/download` | |
| 6 | `GET /api/search` — Arabic full-text search | |
| 7 | Scale testing, then the full 18,000 | |
