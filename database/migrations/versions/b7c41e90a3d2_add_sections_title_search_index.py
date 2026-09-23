"""add a full-text index on section headings

Searching every book's table of contents at once ("فهرس المكتبة") needs to find headings
by word across ~5.4 million sections. `sections.title_norm` already holds each heading
folded by app.services.arabic.normalize, so a plain 'simple' tsvector over it (no
stemming -- headings are matched by word prefix, not by root) is enough. The real index
is 119 MB. IF NOT EXISTS because the production index was built beforehand with
CREATE INDEX CONCURRENTLY, so this deploy does not hold the API down while it builds.

The expression here must match the one in toc_search_service exactly, or the planner
cannot use the index.

Revision ID: b7c41e90a3d2
Revises: d0f9852c9c75
Create Date: 2026-09-23 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b7c41e90a3d2'
down_revision: str | Sequence[str] | None = 'd0f9852c9c75'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sections_title_fts ON sections "
        "USING gin (to_tsvector('simple', title_norm))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sections_title_fts")
