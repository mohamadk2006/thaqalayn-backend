"""push notifications: devices, preferences, the send queue, announced book versions

Revision ID: e3a7c9d15b48
Revises: d1f4b8a27c35
Create Date: 2026-10-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e3a7c9d15b48"
down_revision: str | Sequence[str] | None = "d1f4b8a27c35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_now = sa.func.now()


def upgrade() -> None:
    user = postgresql.UUID(as_uuid=True)
    op.create_table(
        "push_devices",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", user, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False, server_default="ios"),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False, server_default="production"),
        sa.Column("language", sa.Text(), nullable=False, server_default="ar"),
        sa.Column("timezone", sa.Text(), nullable=False, server_default="UTC"),
        sa.Column("app_version", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now),
        sa.Column("last_ok_at", sa.DateTime(timezone=True)),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("platform", "token", name="uq_push_devices_token"),
        sa.UniqueConstraint("user_id", "device_id", "platform", name="uq_push_devices_login"),
    )
    op.create_index("ix_push_devices_user", "push_devices", ["user_id"])
    op.create_table(
        "push_preferences",
        sa.Column("user_id", user, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now),
        sa.Column("last_sent_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "kind IN ('chat_answer','book_updates','new_books')", name="ck_push_pref_kind"
        ),
    )
    op.create_table(
        "push_outbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", user, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB()),
        sa.Column("collapse_id", sa.Text()),
        sa.Column("dedupe_key", sa.Text(), unique=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("send_after", sa.DateTime(timezone=True), nullable=False, server_default=_now),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint(
            "status IN ('pending','sent','skipped','failed')", name="ck_push_outbox_status"
        ),
    )
    op.create_index("ix_push_outbox_due", "push_outbox", ["status", "send_after"])
    op.create_table(
        "push_book_versions",
        # no foreign key: the row of book 0 marks that the library was recorded at the start
        sa.Column("book_id", sa.Integer(), primary_key=True),
        sa.Column("content_version", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=_now),
    )
    # Who has a book in their data (reading progress, sessions, bookmarks, notes' marks...):
    # every kind that refers to a book keeps its plain id in data.bookId.
    op.execute(
        "CREATE INDEX ix_sync_records_book ON sync_records ((data->>'bookId')) WHERE NOT deleted"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sync_records_book")
    op.drop_table("push_book_versions")
    op.drop_index("ix_push_outbox_due", table_name="push_outbox")
    op.drop_table("push_outbox")
    op.drop_table("push_preferences")
    op.drop_index("ix_push_devices_user", table_name="push_devices")
    op.drop_table("push_devices")
