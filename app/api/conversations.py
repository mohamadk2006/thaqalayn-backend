"""The user's chats: GET/PATCH/DELETE /api/chat/conversations (signed in; a chat that is not
the user's is a 404). Chats are made by asking: POST /api/chat with no conversationId."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat import asker
from app.db import get_session
from app.schemas.chat import (
    ChatSource,
    ConversationDetail,
    ConversationList,
    ConversationOut,
    MessageOut,
    RenameIn,
)
from app.services import chat_history

router = APIRouter(prefix="/chat/conversations", tags=["chat"])


def out(chat) -> ConversationOut:
    return ConversationOut(
        id=str(chat.id),
        title=chat.title,
        createdAt=chat.created_at.isoformat(),
        updatedAt=chat.updated_at.isoformat(),
    )


@router.get("", response_model=ConversationList)
async def conversations(
    limit: int = Query(30, ge=1, le=100),
    before: dt.datetime | None = Query(None, description="`nextBefore` of the previous page"),
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
):
    """The user's chats, the most recently used first."""
    _, user_id = await asker(session, authorization)
    chats = await chat_history.listing(session, user_id, limit, before)
    return ConversationList(
        items=[out(c) for c in chats],
        nextBefore=chats[-1].updated_at.isoformat() if len(chats) == limit else None,
    )


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def conversation(
    conversation_id: uuid.UUID,
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
):
    """One chat with all its messages, oldest first, answers with their sources."""
    _, user_id = await asker(session, authorization)
    chat = await chat_history.get(session, user_id, conversation_id)
    rows = await chat_history.messages(session, chat.id)
    return ConversationDetail(
        **out(chat).model_dump(),
        messages=[
            MessageOut(
                id=m.id,
                role=m.role,
                text=m.text,
                status=m.status,
                sources=[ChatSource(**s) for s in (m.sources or [])],
                createdAt=m.created_at.isoformat(),
            )
            for m in rows
        ],
    )


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def rename(
    conversation_id: uuid.UUID,
    body: RenameIn,
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
):
    _, user_id = await asker(session, authorization)
    return out(await chat_history.rename(session, user_id, conversation_id, body.title))


@router.delete("/{conversation_id}", status_code=204)
async def delete(
    conversation_id: uuid.UUID,
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
):
    """Hides the chat at once; it is removed for good after a month."""
    _, user_id = await asker(session, authorization)
    await chat_history.delete(session, user_id, conversation_id)
    return Response(status_code=204)
