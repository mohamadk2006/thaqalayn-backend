"""POST /api/chat -- the chatbot prototype: answers a question from the library's books.

Every request is logged in chat_requests (see app/models/chat.py), failures included.
Limits: see app/services/chat_limits.py (429 with a `code`).
Statuses: 200 with status "ok" (an answer) or "no_results"; 429 when Gemini's free tier
refuses for its rate limit; 502 when a step fails; 503 when the chatbot is not configured.

POST /api/chat/stream takes the same request and answers as a stream of server-sent events:
a `step` event as each step starts and ends, `token` events with the answer as it is
written, then `done` (the same body as POST /api/chat) or
`error`. A request refused up front (limits, length, not configured) is a plain JSON error,
as for POST /api/chat.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session, get_sessionmaker
from app.schemas.chat import ChatIn, ChatOut, ChatQuota, ChatQuotaOut, ChatSource, ChatUsage
from app.services import auth_service as auth
from app.services import chat_limits, chat_service

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


async def asker(request: Request, session: AsyncSession, authorization, client_id) -> str:
    """Who is asking, for the limits: the signed-in user, else the install, else the address."""
    if authorization:
        return f"u:{(await auth.caller(session, authorization)).user.id}"
    if client_id:
        return f"c:{client_id}"
    return f"ip:{auth.client_ip(request)}"


_running: set[asyncio.Task] = set()  # streamed chats still being answered


async def admit(
    body: ChatIn,
    request: Request,
    session: AsyncSession,
    settings: Settings,
    authorization: str | None,
    client_id: str | None,
):
    """(question, who, limits verdict) when the question may be asked now; else the JSON
    response that refuses it."""
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
    who = await asker(request, session, authorization, client_id)
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
    return question, who, verdict


def failure(result, log_id: int) -> JSONResponse | None:
    """The error response for a chat that did not get an answer, else None."""
    if result.status == "rate_limited":
        return JSONResponse(
            {"detail": BUSY, "id": log_id}, status_code=429, headers={"Retry-After": "60"}
        )
    if result.status == "error":
        return JSONResponse({"detail": FAILED, "id": log_id}, status_code=502)
    return None


def chat_out(result, log_id: int, verdict) -> ChatOut:
    usage = result.usage
    return ChatOut(
        id=log_id,
        status=result.status,
        answer=result.answer,
        model=result.model,
        sources=[
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
        ],
        usage=ChatUsage(
            promptTokens=usage.get("promptTokenCount"),
            outputTokens=usage.get("candidatesTokenCount"),
            thoughtsTokens=usage.get("thoughtsTokenCount"),
            totalTokens=usage.get("totalTokenCount"),
        ),
        searchQueries=(result.rewrite or {}).get("queries") or [],
        timings=result.timings,
        quota=ChatQuota(
            limit=verdict.limit,
            remaining=max(0, verdict.remaining - 1) if verdict.limit else 0,
            resetAt=verdict.reset_at.isoformat(),
        ),
    )


@router.get("/chat/quota", response_model=ChatQuotaOut)
async def chat_quota(
    request: Request,
    authorization: str | None = Header(None),
    x_client_id: str | None = Header(None, max_length=64),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """The asker's questions left today and whether one would be accepted now. Costs nothing:
    no question is asked, and it is not counted."""
    who = await asker(request, session, authorization, x_client_id)
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
    request: Request,
    authorization: str | None = Header(None),
    x_client_id: str | None = Header(None, max_length=64),
    x_app_version: str | None = Header(None, max_length=32),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    admitted = await admit(body, request, session, settings, authorization, x_client_id)
    if isinstance(admitted, JSONResponse):
        return admitted
    question, who, verdict = admitted
    chat_limits.begin(who)
    try:
        result = await chat_service.answer(settings, question)
    except chat_service.ChatUnavailable:
        return JSONResponse({"detail": OFF}, status_code=503)
    finally:
        chat_limits.end(who)
    log_id = await chat_service.log_request(
        session, question, result, x_client_id, x_app_version, who
    )
    return failure(result, log_id) or chat_out(result, log_id, verdict)


def sse(event: str, data: dict | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/chat/stream")
async def chat_stream(
    body: ChatIn,
    request: Request,
    authorization: str | None = Header(None),
    x_client_id: str | None = Header(None, max_length=64),
    x_app_version: str | None = Header(None, max_length=32),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """The same chat as POST /api/chat, told step by step as server-sent events."""
    admitted = await admit(body, request, session, settings, authorization, x_client_id)
    if isinstance(admitted, JSONResponse):
        return admitted
    question, who, verdict = admitted
    queue: asyncio.Queue = asyncio.Queue()
    chat_limits.begin(who)

    async def run() -> None:
        # Not tied to the connection: an answer already paid for is logged even if the
        # app went away.
        try:
            result = await chat_service.answer(
                settings,
                question,
                emit=lambda ev: queue.put_nowait(
                    ("token", {"text": ev["token"]}) if "token" in ev else ("step", ev)
                ),
            )
            async with get_sessionmaker()() as logging_session:
                log_id = await chat_service.log_request(
                    logging_session, question, result, x_client_id, x_app_version, who
                )
            failed = failure(result, log_id)
            if failed is not None:
                queue.put_nowait(("error", json.loads(failed.body)))
            else:
                queue.put_nowait(("done", chat_out(result, log_id, verdict).model_dump_json()))
        except Exception as exc:  # noqa: BLE001 -- whatever broke, the app is told
            queue.put_nowait(("error", {"detail": FAILED, "error": type(exc).__name__}))
        finally:
            chat_limits.end(who)
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
