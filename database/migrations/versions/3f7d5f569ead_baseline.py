"""Baseline: establishes the migration chain.

Deliberately empty. The real schema lands in Milestone 3, after the Arabic
normalizer exists — the tsvector generated column depends on it, so the schema
cannot be written first.

Original header: baseline

Revision ID: 3f7d5f569ead
Revises: 
Create Date: 2026-08-18 07:33:43.756770

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3f7d5f569ead'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
