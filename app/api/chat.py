"""POST /api/chat -- the chatbot prototype: answers a question from the library's books.

Every request is logged in chat_requests (see app/models/chat.py), failures included.
Limits: see app/services/chat_limits.py (429 with a `code`).
Statuses: 200 with status "ok" (an answer) or "no_results"; 429 when Gemini's free tier
refuses for its rate limit; 502 when a step fails; 503 when the chatbot is not configured.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.schemas.chat import ChatIn, ChatOut, ChatQuota, ChatSource, ChatUsage
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
    question = body.message.strip()
    if not question:
        return JSONResponse({"detail": "اكتب سؤالاً."}, status_code=422)
    if settings.chat_max_chars and len(question) > settings.chat_max_chars:
        return JSONResponse(
            {"code": "too_long", "detail": f"السؤال أطول من {settings.chat_max_chars} حرفاً."},
            status_code=422,
        )
    who = await asker(request, session, authorization, x_client_id)
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
    if result.status == "rate_limited":
        return JSONResponse(
            {"detail": BUSY, "id": log_id}, status_code=429, headers={"Retry-After": "60"}
        )
    if result.status == "error":
        return JSONResponse({"detail": FAILED, "id": log_id}, status_code=502)
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
