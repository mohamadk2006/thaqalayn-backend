"""The chatbot prototype: a question in, an answer from the library's books out.

    1. a cheap model rewrites the message into up to 3 search texts worded like the books
       (told how the index is made: BGE-M3 over ~220-word passages); if it fails, the
       message is searched as typed,
    2. embed the message and those texts (the embedder container, BGE-M3 -- the model the
       library's vectors in Qdrant were made with) and search them all, merging the lists,
    3. send the best passages with the question to Gemini, which answers from them alone,
    4. log the request -- tokens of both models, timings, passages, status -- for analysis.

No reranking yet.
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


REWRITE_PROMPT = """You prepare search texts for a semantic (vector) search over the
Thaqalayn Library: classical Arabic (and some Persian) Islamic books, mostly Shia: hadith,
tafsir, fiqh, history, supplications, theology, ethics.

How the index is made: every book is cut into passages of about 220 words, diacritics removed,
and each passage is embedded with BGE-M3 (a multilingual dense embedding model, 1024 numbers,
cosine similarity). A search text finds a passage when it reads like that passage.

So, from the user's message, write 1 to 3 search texts that:
- state the topic the way a classical book would state it or answer it: its wording, names
  (with their usual titles), technical terms and key phrases;
- leave out instructions ("write an article", "explain", "in short") and anything about format;
- are 5 to 30 words each, in Arabic (in Persian only when the user asks about Persian books);
- differ from each other (another wording, another likely phrasing in the books).

If the message is not about the books' content (a greeting, a question about you), return no
search texts. Return only JSON: {"queries": ["...", "..."]}"""


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
    # Each Gemini model tried, in order: {"model", "status" (HTTP, 0 = no answer in time), "ms"}.
    attempts: list = field(default_factory=list)
    # The rewrite step: {"model", "status", "ms", "queries", "usage"} (empty when switched off).
    rewrite: dict = field(default_factory=dict)


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
    except (urllib.error.URLError, TimeoutError) as e:  # no answer in time, or unreachable
        return 0, f"{type(e).__name__}: {e}"


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


def _rewrite(settings: Settings, question: str) -> tuple[list[str], dict]:
    """(search texts, log record). Never raises: a failed rewrite means no extra texts."""
    model, t = settings.gemini_rewrite_model, time.monotonic()
    status, body = _post(
        GEMINI_URL.format(model=model),
        {
            "systemInstruction": {"parts": [{"text": REWRITE_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": question}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 400,
                "responseMimeType": "application/json",
            },
        },
        {"x-goog-api-key": settings.gemini_api_key},
        20,
    )
    info = {"model": model, "status": status, "ms": round((time.monotonic() - t) * 1000)}
    if status != 200 or not isinstance(body, dict):
        return [], {**info, "error": str(body)[:300]}
    info["usage"] = body.get("usageMetadata") or {}
    text, _ = read_answer(body)
    try:
        queries = json.loads(text or "{}").get("queries") or []
    except (ValueError, AttributeError):
        return [], {**info, "error": f"not JSON: {(text or '')[:200]}"}
    queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()][:3]
    return queries, {**info, "queries": queries}


def _embed(settings: Settings, texts: list[str]) -> list[list[float]]:
    status, body = _post(settings.embedder_url.rstrip("/") + "/embed", {"texts": texts}, {}, 30)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"embedder {status}: {str(body)[:200]}")
    return body["vectors"]


def _search(settings: Settings, vectors: list[list[float]]) -> list[dict]:
    """The passages closest to any of the vectors: one Qdrant batch request, the lists merged
    by reciprocal rank (a passage near the top of several lists comes first)."""
    limit = settings.chat_passages
    status, body = _post(
        f"{settings.qdrant_url.rstrip('/')}/collections/{settings.qdrant_collection}"
        "/points/search/batch",
        {
            "searches": [
                {
                    "vector": v,
                    "limit": limit,
                    "with_payload": PAYLOAD_FIELDS,
                    # binary-quantized vectors in RAM, re-scored with the full ones;
                    # ef well above the limit
                    "params": {
                        "hnsw_ef": max(128, 16 * limit),
                        "quantization": {"rescore": True, "oversampling": 3.0},
                    },
                }
                for v in vectors
            ]
        },
        {"api-key": settings.qdrant_api_key},
        30,
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"qdrant {status}: {str(body)[:200]}")
    merged: dict[str, dict] = {}
    for hits in body["result"]:
        for rank, hit in enumerate(hits):
            entry = merged.setdefault(hit["id"], {**hit["payload"], "score": 0.0, "_rrf": 0.0})
            entry["_rrf"] += 1 / (60 + rank + 1)
            entry["score"] = max(entry["score"], round(hit["score"], 4))
    best = sorted(merged.values(), key=lambda e: e["_rrf"], reverse=True)[:limit]
    return [{k: v for k, v in e.items() if k != "_rrf"} for e in best]


# Gemini answers that another model may not share: the model is overloaded (503), over our
# free-tier quota (429), failing (500), or did not answer in time (0).
RETRY_STATUSES = (0, 429, 500, 503)


def chat_models(settings: Settings) -> list[str]:
    """The model to ask first, then the fallbacks in order (no repeats)."""
    names = [settings.gemini_chat_model, *settings.gemini_fallback_models.split(",")]
    return list(dict.fromkeys(n.strip() for n in names if n.strip()))


def _gemini(
    settings: Settings, model: str, question: str, passages: list[dict]
) -> tuple[int, dict | str]:
    return _post(
        GEMINI_URL.format(model=model),
        {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": build_contents(question, passages)}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048},
        },
        {"x-goog-api-key": settings.gemini_api_key},
        settings.gemini_attempt_timeout,
    )


def _answer(settings: Settings, question: str) -> ChatResult:
    models = chat_models(settings)
    timings: dict[str, int] = {}
    queries, rewrite = ([], {})
    if settings.chat_rewrite:
        queries, rewrite = _rewrite(settings, question)
        timings["rewriteMs"] = rewrite["ms"]
    texts = list(dict.fromkeys([question, *queries]))  # the message itself is always searched

    t = time.monotonic()
    vectors = _embed(settings, texts)
    timings["embedMs"] = round((time.monotonic() - t) * 1000)

    t = time.monotonic()
    passages = _search(settings, vectors)
    timings["searchMs"] = round((time.monotonic() - t) * 1000)
    if not passages:
        return ChatResult(None, [], "no_results", models[0], timings=timings, rewrite=rewrite)

    # The first model that answers; a busy or failing one hands over to the next.
    attempts: list[dict] = []
    llm_start = time.monotonic()
    status, body, model = 0, "", models[0]
    for model in models:
        t = time.monotonic()
        status, body = _gemini(settings, model, question, passages)
        attempts.append(
            {"model": model, "status": status, "ms": round((time.monotonic() - t) * 1000)}
        )
        if status not in RETRY_STATUSES:
            break
    timings["llmMs"] = round((time.monotonic() - llm_start) * 1000)

    if status in RETRY_STATUSES:  # every model was busy or failed
        state = "rate_limited" if any(a["status"] in (429, 503) for a in attempts) else "error"
        return ChatResult(
            None,
            passages,
            state,
            model,
            timings=timings,
            attempts=attempts,
            rewrite=rewrite,
            error=f"gemini {status}: {str(body)[:500]}",
        )
    if status != 200 or not isinstance(body, dict):
        return ChatResult(
            None,
            passages,
            "error",
            model,
            timings=timings,
            attempts=attempts,
            rewrite=rewrite,
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
        attempts=attempts,
        rewrite=rewrite,
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
        attempts=result.attempts or None,
        rewrite=result.rewrite or None,
        rewrite_tokens=(result.rewrite.get("usage") or {}).get("totalTokenCount"),
        rewrite_ms=result.rewrite.get("ms"),
        sources=[
            {k: p.get(k) for k in ("bookId", "sequence", "chunk", "page", "score")}
            for p in result.sources
        ],
    )
    session.add(row)
    await session.commit()
    return row.id
