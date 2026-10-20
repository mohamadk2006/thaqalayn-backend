"""POST /api/chat -- the chatbot prototype: answers a question from the library's books.

Every request is logged in chat_requests (see app/models/chat.py), failures included.
Statuses: 200 with status "ok" (an answer) or "no_results"; 429 when Gemini's free tier
refuses for its rate limit; 502 when a step fails; 503 when the chatbot is not configured.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.schemas.chat import ChatIn, ChatOut, ChatSource, ChatUsage
from app.services import chat_service

router = APIRouter(tags=["chat"])

BUSY = "الخدمة مشغولة الآن، حاول بعد دقيقة."
FAILED = "تعذّرت الإجابة الآن، حاول مرة أخرى."
OFF = "المحادثة غير مفعّلة على هذا الخادم."


@router.post("/chat", response_model=ChatOut)
async def chat(
    body: ChatIn,
    x_client_id: str | None = Header(None, max_length=64),
    x_app_version: str | None = Header(None, max_length=32),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    question = body.message.strip()
    if not question:
        return JSONResponse({"detail": "اكتب سؤالاً."}, status_code=422)
    try:
        result = await chat_service.answer(settings, question)
    except chat_service.ChatUnavailable:
        return JSONResponse({"detail": OFF}, status_code=503)
    log_id = await chat_service.log_request(session, question, result, x_client_id, x_app_version)
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
                score=p["score"],
            )
            for n, p in enumerate(result.sources, 1)
        ],
        usage=ChatUsage(
            promptTokens=usage.get("promptTokenCount"),
            outputTokens=usage.get("candidatesTokenCount"),
            thoughtsTokens=usage.get("thoughtsTokenCount"),
            totalTokens=usage.get("totalTokenCount"),
        ),
        timings=result.timings,
    )
