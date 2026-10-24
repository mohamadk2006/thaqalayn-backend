"""Request and response of POST /api/chat (the chatbot prototype)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    # Continue this chat (the model sees its earlier messages). Omit to start a new one: its
    # id comes back in the answer.
    conversationId: uuid.UUID | None = None


class ChatSource(BaseModel):
    n: int  # the number the answer cites it by: [n]
    bookId: str
    title: str
    volume: str | None = None  # as the book's metadata gives it
    page: str | None = None  # the printed page label
    sequence: int  # the page's position in the book: GET /api/books/{bookId}/pages/{sequence}
    text: str  # the passage itself (a ~220-word chunk of that page)
    score: float
    foundBy: str | None = None  # "meaning" (vector search) or "words" (exact word search)


class ChatUsage(BaseModel):
    promptTokens: int | None = None
    outputTokens: int | None = None
    thoughtsTokens: int | None = None
    totalTokens: int | None = None


class ChatQuota(BaseModel):
    limit: int  # questions per day (0 = no limit)
    remaining: int  # left today, after this one
    resetAt: str  # when the day's count starts again (UTC, ISO 8601)


class ChatQuotaOut(ChatQuota):
    """GET /api/chat/quota: what the asker may do now, without asking a question."""

    maxChars: int  # longest question (0 = no limit)
    blocked: str | None = None  # why a question would be refused now (the 429 `code`), else null


class ChatOut(BaseModel):
    id: int  # the log row: quote it when reporting a bad answer
    status: str  # ok | no_results
    answer: str | None
    sources: list[ChatSource]
    model: str
    usage: ChatUsage
    # What was searched besides the message itself (the rewrite step's texts)
    searchQueries: list[str] = []
    timings: dict[str, int]
    quota: ChatQuota | None = None
    conversationId: str | None = None  # the chat this question and answer were added to
    messageId: int | None = None  # the answer's message in that chat


class ConversationOut(BaseModel):
    id: str
    title: str
    createdAt: str
    updatedAt: str


class ConversationList(BaseModel):
    items: list[ConversationOut]
    # Pass as `before` to get the next (older) page; null at the end.
    nextBefore: str | None = None


class MessageOut(BaseModel):
    id: int
    role: str  # user | assistant
    text: str  # an assistant message with status "no_results" has an empty text
    status: str | None = None  # assistant: ok | no_results
    sources: list[ChatSource] = []
    createdAt: str


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class RenameIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
