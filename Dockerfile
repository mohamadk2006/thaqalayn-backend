# Runs the API only — PostgreSQL is a separate service (see docker-compose.yml).
# Book/cover content is mounted at BOOKS_ROOT/COVERS_ROOT, never baked into the image:
# the same image works for local dev data and the full ~24GB VPS library.

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependencies first, so an app-code-only change doesn't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app/ app/
COPY database/migrations database/migrations
COPY database/sql database/sql
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

EXPOSE 8000

# Calling the venv's binaries directly, not `uv run` -- `uv run` re-checks the
# lockfile against the environment on every invocation and will try to sync
# missing packages (observed pulling in dev-only tools at container *startup*),
# which is both wasteful and something that would fail outright with no network
# access. The image is already a complete, immutable environment; nothing should
# reach out at runtime to build it further.
#
# Migrations run on every start, not just the first deploy: they're idempotent
# (Alembic no-ops once a revision is applied), and this is what lets a Coolify
# redeploy carry a new migration without a separate manual step.
CMD ["sh", "-c", ".venv/bin/alembic upgrade head && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"]
