"""add accounts and sync -- email-code sign-in, login sessions, the sync store

Tables for docs/accounts_sync_spec.md: users; email_codes (codes as HMAC hashes, also the
start endpoint's rate-limit counters); verify_calls (the verify endpoint's per-IP limit);
auth_sessions (one per signed-in device, tokens as SHA-256 hashes); used_refresh_tokens
(rotated-away refresh tokens, to detect a copied one); email_sends (each send attempt,
the email hashed, never the code); sync_records with the sync_seq sequence.

Revision ID: 9b3e5d7a2c14
Revises: 8d4f1b2c6e07
Create Date: 2026-10-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9b3e5d7a2c14"
down_revision: str | Sequence[str] | None = "8d4f1b2c6e07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", _TS),
    )
    op.create_table(
        "email_codes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", _TS, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_at", _TS),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("ip", postgresql.INET()),
    )
    op.create_index(
        "ix_email_codes_email_created", "email_codes", ["email", sa.text("created_at DESC")]
    )
    op.create_index("ix_email_codes_ip_created", "email_codes", ["ip", "created_at"])
    op.create_table(
        "verify_calls",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("ip", postgresql.INET()),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_verify_calls_ip_created", "verify_calls", ["ip", "created_at"])
    op.create_table(
        "auth_sessions",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("user_id", _UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("device_name", sa.Text()),
        sa.Column("platform", sa.Text()),
        sa.Column("access_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("refresh_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("access_expires_at", _TS, nullable=False),
        sa.Column("refresh_expires_at", _TS, nullable=False),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", _TS),
    )
    op.create_index("ix_auth_sessions_user", "auth_sessions", ["user_id"])
    op.create_table(
        "used_refresh_tokens",
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column(
            "session_id",
            _UUID,
            sa.ForeignKey("auth_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("used_at", _TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "email_sends",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("email_hash", sa.Text(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "sync_records",
        sa.Column(
            "user_id", _UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("key_lower", sa.Text(), primary_key=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB()),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("updated_at", _TS, nullable=False),
        sa.Column("order_changed_at", _TS),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("device_id", sa.Text()),
    )
    op.create_index("ix_sync_records_user_seq", "sync_records", ["user_id", "seq"])
    op.execute("CREATE SEQUENCE sync_seq")


def downgrade() -> None:
    op.execute("DROP SEQUENCE sync_seq")
    op.drop_table("sync_records")
    op.drop_table("email_sends")
    op.drop_table("used_refresh_tokens")
    op.drop_table("auth_sessions")
    op.drop_table("verify_calls")
    op.drop_table("email_codes")
    op.drop_table("users")
