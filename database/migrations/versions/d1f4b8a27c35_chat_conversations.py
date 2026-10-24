"""chat conversations and messages

A chat is a list of questions and answers that the model sees again as context, kept per
user. chat_requests.conversation_id links each logged question to its chat.

Revision ID: d1f4b8a27c35
Revises: c5e8a1d37f42
Create Date: 2026-10-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d1f4b8a27c35"
down_revision: str | Sequence[str] | None = "c5e8a1d37f42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_chat_conversations_user", "chat_conversations", ["user_id", "updated_at"])
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.Text()),
        sa.Column("sources", postgresql.JSONB()),
        sa.Column("request_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("role IN ('user','assistant')", name="ck_chat_messages_role"),
    )
    op.create_index("ix_chat_messages_conversation", "chat_messages", ["conversation_id", "id"])
    op.add_column("chat_requests", sa.Column("conversation_id", postgresql.UUID(as_uuid=True)))
    op.create_index("ix_chat_requests_conversation", "chat_requests", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_requests_conversation", table_name="chat_requests")
    op.drop_column("chat_requests", "conversation_id")
    op.drop_index("ix_chat_messages_conversation", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_conversations_user", table_name="chat_conversations")
    op.drop_table("chat_conversations")
