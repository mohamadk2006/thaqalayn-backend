"""The chatbot's request log: one row per question asked, kept for analysis.

Every request is recorded, failed ones too (rate-limited by Gemini, nothing found, an
error), with the tokens Gemini reports and the time each step took, so cost, speed and
quality can be studied from real use.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

CHAT_STATUSES = ("ok", "no_results", "rate_limited", "error")


class ChatRequest(Base):
    __tablename__ = "chat_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Sent by the app (X-Client-Id / X-App-Version headers): an anonymous install id, to
    # count questions per device. Never a name or an account.
    client_id: Mapped[str | None] = mapped_column(Text)
    app_version: Mapped[str | None] = mapped_column(Text)

    message: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    model: Mapped[str | None] = mapped_column(Text)
    # Gemini's usageMetadata, split out for easy sums; `usage` keeps it whole.
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    thoughts_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    finish_reason: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict | None] = mapped_column(JSONB)

    # Milliseconds per step: embedding the question, the vector search, Gemini, and all.
    embed_ms: Mapped[int | None] = mapped_column(Integer)
    search_ms: Mapped[int | None] = mapped_column(Integer)
    llm_ms: Mapped[int | None] = mapped_column(Integer)
    total_ms: Mapped[int | None] = mapped_column(Integer)

    # Each Gemini model tried, in order: [{"model", "status", "ms"}]; `model` is the last one.
    attempts: Mapped[list | None] = mapped_column(JSONB)

    # The rewrite step (model, status, ms, the search texts, usage); its tokens and time split out.
    rewrite: Mapped[dict | None] = mapped_column(JSONB)
    rewrite_tokens: Mapped[int | None] = mapped_column(Integer)
    rewrite_ms: Mapped[int | None] = mapped_column(Integer)

    # The word search (keywords, hits, ms, error) and the rerank step (model, status, ms,
    # candidates, the numbers it chose, usage); time and tokens split out.
    word_search: Mapped[dict | None] = mapped_column(JSONB)
    word_ms: Mapped[int | None] = mapped_column(Integer)
    rerank: Mapped[dict | None] = mapped_column(JSONB)
    rerank_tokens: Mapped[int | None] = mapped_column(Integer)
    rerank_ms: Mapped[int | None] = mapped_column(Integer)

    # The passages sent to Gemini, in order: [{"bookId", "sequence", "chunk", "page", "score"}].
    sources: Mapped[list | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            "status IN ('ok','no_results','rate_limited','error')", name="ck_chat_requests_status"
        ),
        Index("ix_chat_requests_created", "created_at"),
        Index("ix_chat_requests_client", "client_id"),
    )
