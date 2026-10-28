"""A user's chats: the conversations and their messages, and the earlier turns a follow-up
question is understood with.

A conversation belongs to one user; asking for another user's (or a deleted) one is the same
404 as for one that does not exist. Deleting hides a chat at once; `purge` removes it for
good a month later, and blanks the text kept in the chat log for analysis.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.config import Settings
from app.models.chat import ChatConversation, ChatMessage, ChatRequest

TITLE_CHARS = 60
KEEP_DELETED_DAYS = 30


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def title_of(question: str) -> str:
    line = " ".join(question.split())
    return line if len(line) <= TITLE_CHARS else line[: TITLE_CHARS - 1].rstrip() + "…"


async def get(session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID):
    row = await session.scalar(
        select(ChatConversation).where(
            ChatConversation.id == conversation_id,
            ChatConversation.user_id == user_id,
            ChatConversation.deleted_at.is_(None),
        )
    )
    if row is None:
        raise ApiError(404, "conversation_not_found")
    return row


async def message_count(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
    )


async def check_room(
    session: AsyncSession, settings: Settings, user_id: uuid.UUID, conversation_id: uuid.UUID | None
) -> None:
    """409 when the chat is full, or (for a new one) the user has too many chats."""
    if conversation_id is not None:
        await get(session, user_id, conversation_id)
        if await message_count(session, conversation_id) >= settings.chat_max_messages:
            raise ApiError(409, "conversation_full")
        return
    chats = await session.scalar(
        select(func.count())
        .select_from(ChatConversation)
        .where(ChatConversation.user_id == user_id, ChatConversation.deleted_at.is_(None))
    )
    if chats >= settings.chat_max_conversations:
        raise ApiError(409, "too_many_conversations")


async def earlier_turns(
    session: AsyncSession, settings: Settings, conversation_id: uuid.UUID | None
) -> list[tuple[str, str]]:
    """[(role, text)] of the last messages of the chat, oldest first (answers not found left
    out)."""
    if conversation_id is None or settings.chat_history_messages <= 0:
        return []
    rows = (
        await session.execute(
            select(ChatMessage.role, ChatMessage.text)
            .where(ChatMessage.conversation_id == conversation_id, ChatMessage.text != "")
            .order_by(ChatMessage.id.desc())
            .limit(settings.chat_history_messages)
        )
    ).all()
    return [(r, t) for r, t in reversed(rows)]


async def add_turn(
    session: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    question: str,
    answer: str | None,
    status: str,
    sources: list[dict],
    request_id: int,
) -> tuple[ChatConversation, ChatMessage]:
    """Stores a question and its answer; the chat is created by its first turn. Returns the
    chat and the answer's message."""
    chat = await session.get(ChatConversation, conversation_id)
    if chat is None:
        chat = ChatConversation(id=conversation_id, user_id=user_id, title=title_of(question))
        session.add(chat)
        await session.flush()
    elif chat.user_id != user_id:
        raise ApiError(404, "conversation_not_found")
    session.add(ChatMessage(conversation_id=chat.id, role="user", text=question))
    reply = ChatMessage(
        conversation_id=chat.id,
        role="assistant",
        text=answer or "",
        status=status,
        sources=sources,
        request_id=request_id,
    )
    session.add(reply)
    chat.updated_at = now()
    await session.commit()
    return chat, reply


async def listing(
    session: AsyncSession, user_id: uuid.UUID, limit: int, before: dt.datetime | None
) -> list[ChatConversation]:
    query = select(ChatConversation).where(
        ChatConversation.user_id == user_id, ChatConversation.deleted_at.is_(None)
    )
    if before is not None:
        query = query.where(ChatConversation.updated_at < before)
    rows = await session.scalars(query.order_by(ChatConversation.updated_at.desc()).limit(limit))
    return list(rows)


async def messages(session: AsyncSession, conversation_id: uuid.UUID) -> list[ChatMessage]:
    rows = await session.scalars(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.id)
    )
    return list(rows)


async def rename(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID, title: str
) -> ChatConversation:
    chat = await get(session, user_id, conversation_id)
    chat.title = " ".join(title.split())[:120] or chat.title
    await session.commit()
    return chat


async def delete(session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
    chat = await get(session, user_id, conversation_id)
    chat.deleted_at = now()
    await session.commit()


async def purge(session: AsyncSession) -> None:
    """Chats deleted more than KEEP_DELETED_DAYS ago are removed, and the text of their
    questions blanked in the chat log (the tokens and timings stay for analysis). The text of
    requests that failed (no answer, so no chat holds them) is blanked after the same time."""
    old = select(ChatConversation.id).where(
        ChatConversation.deleted_at < now() - dt.timedelta(days=KEEP_DELETED_DAYS)
    )
    await session.execute(
        update(ChatRequest)
        .where(ChatRequest.conversation_id.in_(old))
        .values(message="", answer=None, rewrite=None, word_search=None, rerank=None, sources=None)
    )
    await session.execute(
        update(ChatRequest)
        .where(
            ChatRequest.status.in_(("error", "rate_limited")),
            ChatRequest.created_at < now() - dt.timedelta(days=KEEP_DELETED_DAYS),
            ChatRequest.message != "",
        )
        .values(message="", answer=None, rewrite=None, word_search=None, rerank=None, sources=None)
    )
    await session.execute(ChatConversation.__table__.delete().where(ChatConversation.id.in_(old)))
    await session.commit()
