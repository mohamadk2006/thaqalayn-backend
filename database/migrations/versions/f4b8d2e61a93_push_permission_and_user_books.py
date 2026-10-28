"""push: the device's notification permission, and the books a user has downloaded

Revision ID: f4b8d2e61a93
Revises: e3a7c9d15b48
Create Date: 2026-10-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4b8d2e61a93"
down_revision: str | Sequence[str] | None = "e3a7c9d15b48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "push_devices",
        sa.Column("permission", sa.Text(), nullable=False, server_default="authorized"),
    )
    op.create_table(
        "push_user_books",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("book_id", sa.Integer(), primary_key=True),
    )
    op.create_index("ix_push_user_books_book", "push_user_books", ["book_id"])


def downgrade() -> None:
    op.drop_index("ix_push_user_books_book", table_name="push_user_books")
    op.drop_table("push_user_books")
    op.drop_column("push_devices", "permission")
