"""Request and response of POST /api/chat (the chatbot prototype)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


class ChatSource(BaseModel):
    n: int  # the number the answer cites it by: [n]
    bookId: str
    title: str
    volume: str | None = None  # as the book's metadata gives it
    page: str | None = None  # the printed page label
    sequence: int  # the page's position in the book: GET /api/books/{bookId}/pages/{sequence}
    text: str  # the passage itself (a ~220-word chunk of that page)
    score: float


class ChatUsage(BaseModel):
    promptTokens: int | None = None
    outputTokens: int | None = None
    thoughtsTokens: int | None = None
    totalTokens: int | None = None


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
