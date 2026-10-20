"""The chatbot prototype: a question in, an answer from the library's books out.

    1. embed the question (the embedder container, BGE-M3 -- the model the library's
       vectors in Qdrant were made with),
    2. find the closest passages in Qdrant,
    3. send them with the question to Gemini, which answers from them alone and cites them,
    4. log the request -- tokens, timings, passages, status -- for analysis.

No question rewriting and no reranking yet: this measures the plain pipeline first.
HTTP with the standard library in a worker thread, like the rest of the API.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.chat import ChatRequest

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
PAYLOAD_FIELDS = ["bookId", "title", "volume", "authorDeath", "sequence", "page", "chunk", "text"]

SYSTEM_PROMPT = """أنت مساعد البحث في مكتبة الثقلين، وهي مكتبة كتب إسلامية شيعية.
تُعطى سؤال المستخدم ومقاطع مرقّمة من كتب المكتبة.

القواعد:
- أجب من المقاطع المعطاة وحدها، لا من معرفتك العامة.
- بعد كل معلومة اذكر رقم المقطع الذي أُخذت منه بين معقوفين، مثل [1] أو [2][4].
- إذا لم تكن في المقاطع إجابة عن السؤال فقل ذلك صراحة، ولا تخمّن.
- انقل الآيات والأحاديث كما وردت في المقاطع دون تغيير.
- أجب بلغة السؤال، بإيجاز ووضوح، وبتنسيق Markdown بسيط."""


class ChatUnavailable(Exception):
    """The chatbot is not configured on this server."""


class ChatBusy(Exception):
    """Gemini refused for its rate limit (the free tier's per-minute or per-day quota)."""


@dataclass
class ChatResult:
    answer: str | None
    sources: list[dict]
    status: str
    model: str
    usage: dict = field(default_factory=dict)
    finish_reason: str | None = None
    timings: dict = field(default_factory=dict)
    error: str | None = None


def _post(url: str, body: dict, headers: dict, timeout: float) -> tuple[int, dict | str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"content-type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def build_contents(question: str, passages: list[dict]) -> str:
    """The user turn Gemini sees: the numbered passages, then the question."""
    parts = []
    for n, p in enumerate(passages, 1):
        where = p.get("title") or ""
        if p.get("volume"):
            where += f"، ج{p['volume']}"
        if p.get("page"):
            where += f"، ص{p['page']}"
        parts.append(f"[{n}] ({where})\n{p.get('text', '')}")
    return "المقاطع:\n\n" + "\n\n".join(parts) + f"\n\nالسؤال: {question}"


def read_answer(response: dict) -> tuple[str | None, str | None]:
    """(answer text, finish reason) from a generateContent response."""
    candidates = response.get("candidates") or []
    if not candidates:
        return None, (response.get("promptFeedback") or {}).get("blockReason")
    first = candidates[0]
    text = "".join(
        p.get("text", "")
        for p in (first.get("content") or {}).get("parts", [])
        if not p.get("thought")
    )
    return text.strip() or None, first.get("finishReason")


def _embed(settings: Settings, question: str) -> list[float]:
    status, body = _post(
        settings.embedder_url.rstrip("/") + "/embed", {"texts": [question]}, {}, 30
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"embedder {status}: {str(body)[:200]}")
    return body["vectors"][0]


def _search(settings: Settings, vector: list[float]) -> list[dict]:
    limit = settings.chat_passages
    status, body = _post(
        f"{settings.qdrant_url.rstrip('/')}/collections/{settings.qdrant_collection}/points/search",
        {
            "vector": vector,
            "limit": limit,
            "with_payload": PAYLOAD_FIELDS,
            # binary-quantized vectors in RAM, re-scored with the full ones; ef well above the limit
            "params": {
                "hnsw_ef": max(128, 16 * limit),
                "quantization": {"rescore": True, "oversampling": 3.0},
            },
        },
        {"api-key": settings.qdrant_api_key},
        30,
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"qdrant {status}: {str(body)[:200]}")
    return [{**hit["payload"], "score": round(hit["score"], 4)} for hit in body["result"]]


def _gemini(settings: Settings, question: str, passages: list[dict]) -> tuple[int, dict | str]:
    return _post(
        GEMINI_URL.format(model=settings.gemini_chat_model),
        {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": build_contents(question, passages)}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048},
        },
        {"x-goog-api-key": settings.gemini_api_key},
        120,
    )


def _answer(settings: Settings, question: str) -> ChatResult:
    model = settings.gemini_chat_model
    timings: dict[str, int] = {}
    t = time.monotonic()
    vector = _embed(settings, question)
    timings["embedMs"] = round((time.monotonic() - t) * 1000)

    t = time.monotonic()
    passages = _search(settings, vector)
    timings["searchMs"] = round((time.monotonic() - t) * 1000)
    if not passages:
        return ChatResult(None, [], "no_results", model, timings=timings)

    t = time.monotonic()
    status, body = _gemini(settings, question, passages)
    timings["llmMs"] = round((time.monotonic() - t) * 1000)
    if status == 429:
        return ChatResult(
            None, passages, "rate_limited", model, timings=timings, error=str(body)[:500]
        )
    if status != 200 or not isinstance(body, dict):
        return ChatResult(
            None,
            passages,
            "error",
            model,
            timings=timings,
            error=f"gemini {status}: {str(body)[:500]}",
        )
    answer, finish = read_answer(body)
    return ChatResult(
        answer,
        passages,
        "ok" if answer else "error",
        model,
        usage=body.get("usageMetadata") or {},
        finish_reason=finish,
        timings=timings,
        error=None if answer else f"no answer (finish reason {finish})",
    )


async def answer(settings: Settings, question: str) -> ChatResult:
    if not (settings.gemini_api_key and settings.qdrant_api_key):
        raise ChatUnavailable
    t = time.monotonic()
    try:
        result = await asyncio.to_thread(_answer, settings, question)
    except Exception as exc:  # noqa: BLE001 -- the embedder or Qdrant failing is logged like any error
        result = ChatResult(
            None,
            [],
            "error",
            settings.gemini_chat_model,
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
    result.timings["totalMs"] = round((time.monotonic() - t) * 1000)
    return result


async def log_request(
    session: AsyncSession,
    question: str,
    result: ChatResult,
    client_id: str | None,
    app_version: str | None,
) -> int:
    usage = result.usage
    row = ChatRequest(
        client_id=client_id,
        app_version=app_version,
        message=question,
        answer=result.answer,
        status=result.status,
        error=result.error,
        model=result.model,
        prompt_tokens=usage.get("promptTokenCount"),
        output_tokens=usage.get("candidatesTokenCount"),
        thoughts_tokens=usage.get("thoughtsTokenCount"),
        cached_tokens=usage.get("cachedContentTokenCount"),
        total_tokens=usage.get("totalTokenCount"),
        finish_reason=result.finish_reason,
        usage=usage or None,
        embed_ms=result.timings.get("embedMs"),
        search_ms=result.timings.get("searchMs"),
        llm_ms=result.timings.get("llmMs"),
        total_ms=result.timings.get("totalMs"),
        sources=[
            {k: p.get(k) for k in ("bookId", "sequence", "chunk", "page", "score")}
            for p in result.sources
        ],
    )
    session.add(row)
    await session.commit()
    return row.id
