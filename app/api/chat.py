"""POST /api/chat -- the chatbot prototype: answers a question from the library's books.

Every request is logged in chat_requests (see app/models/chat.py), failures included.
Signing in is required (Authorization: Bearer; 401 token_expired without a valid token).
Limits: see app/services/chat_limits.py (429 with a `code`).
Statuses: 200 with status "ok" (an answer) or "no_results"; 429 when Gemini's free tier
refuses for its rate limit; 502 when a step fails; 503 when the chatbot is not configured.

POST /api/chat/stream takes the same request and answers as a stream of server-sent events:
a `conversation` event (the chat's id), a `step` event as each step starts and ends,
`token` events with the answer as it is written, then `done` (the same body as POST /api/chat) or
`error`. A request refused up front (limits, length, not configured) is a plain JSON error,
as for POST /api/chat.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session, get_sessionmaker
from app.schemas.chat import ChatIn, ChatOut, ChatQuota, ChatQuotaOut, ChatSource, ChatUsage
from app.services import auth_service as auth
from app.services import chat_history, chat_limits, chat_service, push_service

router = APIRouter(tags=["chat"])

BUSY = "الخدمة مشغولة الآن، حاول بعد دقيقة."
FAILED = "تعذّرت الإجابة الآن، حاول مرة أخرى."
OFF = "المحادثة غير مفعّلة على هذا الخادم."
LIMIT_TEXTS = {
    "daily_limit": "بلغت حدّك اليومي من الأسئلة، يتجدد غداً.",
    "service_limit": "بلغت الخدمة حدّها اليومي، حاول غداً.",
    "too_fast": "أسئلة كثيرة بسرعة، انتظر دقيقة.",
    "in_progress": "سؤالك السابق لم ينتهِ بعد.",
}


async def asker(session: AsyncSession, authorization: str | None) -> tuple[str, uuid.UUID]:
    """(who, user id): the signed-in user. Without a valid access token the answer is 401
    token_expired (the app refreshes the token, or asks the user to sign in)."""
    user_id = (await auth.caller(session, authorization)).user.id
    return f"u:{user_id}", user_id


_running: set[asyncio.Task] = set()  # streamed chats still being answered


@dataclass
class Admitted:
    question: str
    who: str
    user_id: uuid.UUID
    verdict: object  # chat_limits.Verdict
    conversation_id: uuid.UUID  # the chat the question goes in (new if the request had none)
    is_new: bool
    history: list  # [(role, text)] of the chat so far


async def admit(
    body: ChatIn,
    session: AsyncSession,
    settings: Settings,
    authorization: str | None,
):
    """An Admitted when the question may be asked now; else the JSON response that refuses
    it. Signing in is required (401 otherwise); a chat that is not the user's is a 404, a
    full one a 409."""
    who, user_id = await asker(session, authorization)
    question = body.message.strip()
    if not question:
        return JSONResponse({"detail": "اكتب سؤالاً."}, status_code=422)
    if settings.chat_max_chars and len(question) > settings.chat_max_chars:
        return JSONResponse(
            {"code": "too_long", "detail": f"السؤال أطول من {settings.chat_max_chars} حرفاً."},
            status_code=422,
        )
    if not chat_service.configured(settings):
        return JSONResponse({"detail": OFF}, status_code=503)
    await chat_history.check_room(session, settings, user_id, body.conversationId)
    verdict = await chat_limits.check(session, settings, who)
    if verdict.code:
        return JSONResponse(
            {
                "code": verdict.code,
                "detail": LIMIT_TEXTS[verdict.code],
                "limit": verdict.limit,
                "remaining": verdict.remaining,
                "resetAt": verdict.reset_at.isoformat(),
            },
            status_code=429,
            headers={"Retry-After": str(verdict.retry_after)},
        )
    history = await chat_history.earlier_turns(session, settings, body.conversationId)
    return Admitted(
        question,
        who,
        user_id,
        verdict,
        body.conversationId or uuid.uuid4(),
        body.conversationId is None,
        history,
    )


def failure(result, log_id: int) -> JSONResponse | None:
    """The error response for a chat that did not get an answer, else None."""
    if result.status == "rate_limited":
        return JSONResponse(
            {"detail": BUSY, "id": log_id}, status_code=429, headers={"Retry-After": "60"}
        )
    if result.status == "error":
        return JSONResponse({"detail": FAILED, "id": log_id}, status_code=502)
    return None


def source_list(result) -> list[ChatSource]:
    return [
        ChatSource(
            n=n,
            bookId=str(p["bookId"]),
            title=p.get("title") or "",
            volume=str(p["volume"]) if p.get("volume") not in (None, "") else None,
            page=str(p["page"]) if p.get("page") is not None else None,
            sequence=p["sequence"],
            text=p.get("text") or "",
            score=p.get("score") or 0.0,
            foundBy=p.get("foundBy"),
        )
        for n, p in enumerate(result.sources, 1)
    ]


async def finish(
    session: AsyncSession, ask: Admitted, result, client_id, app_version
) -> tuple[int, ChatOut | JSONResponse]:
    """Logs the question; an answer (or "nothing found") is added to the chat. Returns the log
    id and the response body: a ChatOut, or the error response."""
    log_id = await chat_service.log_request(
        session, ask.question, result, client_id, app_version, ask.who, ask.conversation_id
    )
    failed = failure(result, log_id)
    if failed is not None:
        return log_id, failed
    _, reply = await chat_history.add_turn(
        session,
        ask.user_id,
        ask.conversation_id,
        ask.question,
        result.answer,
        result.status,
        [s.model_dump() for s in source_list(result)],
        log_id,
    )
    return log_id, chat_out(result, log_id, ask.verdict, ask.conversation_id, reply.id)


def chat_out(result, log_id: int, verdict, conversation_id=None, message_id=None) -> ChatOut:
    usage = result.usage
    return ChatOut(
        id=log_id,
        status=result.status,
        answer=result.answer,
        model=result.model,
        sources=source_list(result),
        conversationId=str(conversation_id) if conversation_id else None,
        messageId=message_id,
        usage=ChatUsage(
            promptTokens=usage.get("promptTokenCount"),
            outputTokens=usage.get("candidatesTokenCount"),
            thoughtsTokens=usage.get("thoughtsTokenCount"),
            totalTokens=usage.get("totalTokenCount"),
        ),
        searchQueries=(result.rewrite or {}).get("searched")
        or (result.rewrite or {}).get("queries")
        or [],
        timings=result.timings,
        quota=ChatQuota(
            limit=verdict.limit,
            remaining=max(0, verdict.remaining - 1) if verdict.limit else 0,
            resetAt=verdict.reset_at.isoformat(),
        ),
    )


@router.get("/chat/quota", response_model=ChatQuotaOut)
async def chat_quota(
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """The asker's questions left today and whether one would be accepted now. Costs nothing:
    no question is asked, and it is not counted."""
    who, _ = await asker(session, authorization)
    verdict = await chat_limits.check(session, settings, who)
    return ChatQuotaOut(
        limit=verdict.limit,
        remaining=verdict.remaining,
        resetAt=verdict.reset_at.isoformat(),
        maxChars=settings.chat_max_chars,
        blocked=verdict.code,
    )


@router.post("/chat", response_model=ChatOut)
async def chat(
    body: ChatIn,
    authorization: str | None = Header(None),
    x_client_id: str | None = Header(None, max_length=64),
    x_app_version: str | None = Header(None, max_length=32),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    ask = await admit(body, session, settings, authorization)
    if isinstance(ask, JSONResponse):
        return ask
    chat_limits.begin(ask.who)
    try:
        result = await chat_service.answer(settings, ask.question, history=ask.history)
    except chat_service.ChatUnavailable:
        return JSONResponse({"detail": OFF}, status_code=503)
    finally:
        chat_limits.end(ask.who)
    _, response = await finish(session, ask, result, x_client_id, x_app_version)
    return response


def sse(event: str, data: dict | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/chat/stream")
async def chat_stream(
    body: ChatIn,
    authorization: str | None = Header(None),
    x_client_id: str | None = Header(None, max_length=64),
    x_app_version: str | None = Header(None, max_length=32),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """The same chat as POST /api/chat, told step by step as server-sent events."""
    ask = await admit(body, session, settings, authorization)
    if isinstance(ask, JSONResponse):
        return ask
    queue: asyncio.Queue = asyncio.Queue()
    chat_limits.begin(ask.who)

    async def run() -> None:
        # Not tied to the connection: an answer already paid for is logged and kept in the
        # chat even if the app went away.
        try:
            queue.put_nowait(("conversation", {"id": str(ask.conversation_id), "new": ask.is_new}))
            result = await chat_service.answer(
                settings,
                ask.question,
                emit=lambda ev: queue.put_nowait(
                    ("token", {"text": ev["token"]}) if "token" in ev else ("step", ev)
                ),
                history=ask.history,
            )
            async with get_sessionmaker()() as own_session:
                _, response = await finish(own_session, ask, result, x_client_id, x_app_version)
                if not isinstance(response, JSONResponse):
                    # a user who left the app finds the answer waiting; the app shows nothing
                    # for it while it is in the foreground
                    await push_service.chat_answer_ready(
                        own_session, ask.user_id, ask.conversation_id, response.id
                    )
            if isinstance(response, JSONResponse):
                queue.put_nowait(("error", json.loads(response.body)))
            else:
                queue.put_nowait(("done", response.model_dump_json()))
        except Exception as exc:  # noqa: BLE001 -- whatever broke, the app is told
            queue.put_nowait(("error", {"detail": FAILED, "error": type(exc).__name__}))
        finally:
            chat_limits.end(ask.who)
            queue.put_nowait(None)

    task = asyncio.create_task(run())
    _running.add(task)
    task.add_done_callback(_running.discard)

    async def events():
        yield ": open\n\n"  # sends the headers at once
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=15)
            except TimeoutError:
                yield ": ping\n\n"  # keeps the connection open through proxies
                continue
            if item is None:
                return
            yield sse(*item)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
