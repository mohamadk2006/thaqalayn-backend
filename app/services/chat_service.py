"""The chatbot prototype: a question in, an answer from the library's books out.

    1. rewrite: a cheap model turns the message into up to 3 search texts worded like the
       books (it is told how the index is made: BGE-M3 over ~220-word passages) and up to 3
       exact keywords; if it fails, the message is searched as typed,
    2. two searches at once:
       * vectors -- the message and the texts embedded (the embedder container, BGE-M3, the
         model the library's vectors in Qdrant were made with) and searched in one batch,
       * words -- each keyword in the library's own full-text search (names, rare terms and
         the exact words of a narration, which vectors miss),
       their candidates merged by reciprocal rank,
    3. rerank: the cheap model reads the candidates and picks the passages that answer,
    4. answer: the main model answers the message from those passages alone, citing them,
    5. log the request -- tokens and time of every step, passages, status -- for analysis.

Every step but the answer may fail without failing the chat: no rewrite means the message
is searched as typed, no word search means vectors alone, no rerank means the merged order.
HTTP with the standard library in worker threads, like the rest of the API.
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
WORD_HITS_PER_KEYWORD = 6
WORD_SEARCH_TIMEOUT = 8.0  # seconds for all the keywords together
PASSAGE_RADIUS = 650  # characters each side of a word-search match: a passage of ~220 words
RERANK_PASSAGE_CHARS = 1200  # of each candidate shown to the reranker

SYSTEM_PROMPT = """أنت مساعد البحث في مكتبة الثقلين، وهي مكتبة كتب إسلامية شيعية.
تُعطى سؤال المستخدم ومقاطع مرقّمة من كتب المكتبة.

القواعد:
- أجب من المقاطع المعطاة وحدها، لا من معرفتك العامة.
- بعد كل معلومة اذكر رقم المقطع الذي أُخذت منه بين معقوفين، مثل [1] أو [2][4].
- إذا لم تكن في المقاطع إجابة عن السؤال فقل ذلك صراحة، ولا تخمّن.
- انقل الآيات والأحاديث كما وردت في المقاطع دون تغيير.
- أجب بلغة السؤال، بإيجاز ووضوح، وبتنسيق Markdown بسيط."""


REWRITE_PROMPT = """You prepare searches over the Thaqalayn Library: classical Arabic (and
some Persian) Islamic books, mostly Shia: hadith, tafsir, fiqh, history, supplications,
theology, ethics. Two searches run on what you write.

1. A semantic (vector) search. Every book is cut into passages of about 220 words, diacritics
removed, and each passage is embedded with BGE-M3 (a multilingual dense embedding model, 1024
numbers, cosine similarity). A search text finds a passage when it reads like that passage.
Write 1 to 3 "queries" that:
- state the topic the way a classical book would state it or answer it: its wording, names
  (with their usual titles), technical terms and key phrases;
- leave out instructions ("write an article", "explain", "in short") and anything about format;
- are 5 to 30 words each, in Arabic (in Persian only when the user asks about Persian books);
- differ from each other (another wording, another likely phrasing in the books).

2. An exact word search: pages that hold the words side by side, as written (diacritics and
hamza forms do not matter). Write 0 to 3 "keywords", each 1 to 4 words, that the answering
page would contain exactly: a name, a rare term, a book title, a distinctive phrase of a
well-known narration or verse. Never a word found on most pages (الله، قال، النبي، الإمام).

If the message is not about the books' content (a greeting, a question about you), return
both lists empty. Return only JSON: {"queries": ["..."], "keywords": ["..."]}"""


RERANK_PROMPT = """You choose passages for answering a question from a library of classical
Islamic books. You get the user's question and numbered candidate passages, each with its
book. Pick the passages that help answer the question: they state the answer, a narration or
verse it asks about, or facts the answer needs. Leave out passages that only share words with
the question. Order them from most to least useful, at most {top}. Return only JSON:
{{"best": [numbers]}} -- an empty list if none helps."""


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
    # The rewrite step: {"model", "status", "ms", "queries", "keywords", "usage"}.
    rewrite: dict = field(default_factory=dict)
    # The word search: {"keywords", "hits", "ms", "error"}.
    words: dict = field(default_factory=dict)
    # The rerank step: {"model", "status", "ms", "candidates", "best", "usage"}.
    rerank: dict = field(default_factory=dict)


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


def _where(p: dict) -> str:
    where = p.get("title") or ""
    if p.get("volume"):
        where += f"، ج{p['volume']}"
    if p.get("page"):
        where += f"، ص{p['page']}"
    return where


def build_contents(question: str, passages: list[dict]) -> str:
    """The user turn Gemini sees: the numbered passages, then the question."""
    parts = [f"[{n}] ({_where(p)})\n{p.get('text', '')}" for n, p in enumerate(passages, 1)]
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


def _json_call(
    settings: Settings, model: str, system: str, user: str, max_tokens: int, timeout: float
) -> tuple[dict | None, dict]:
    """A cheap-model call that must return JSON: (the JSON or None, log record)."""
    t = time.monotonic()
    status, body = _post(
        GEMINI_URL.format(model=model),
        {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        },
        {"x-goog-api-key": settings.gemini_api_key},
        timeout,
    )
    info = {"model": model, "status": status, "ms": round((time.monotonic() - t) * 1000)}
    if status != 200 or not isinstance(body, dict):
        return None, {**info, "error": str(body)[:300]}
    info["usage"] = body.get("usageMetadata") or {}
    text, _ = read_answer(body)
    try:
        out = json.loads(text or "")
    except ValueError:
        return None, {**info, "error": f"not JSON: {(text or '')[:200]}"}
    if not isinstance(out, dict):
        return None, {**info, "error": f"not a JSON object: {(text or '')[:200]}"}
    return out, info


def _strings(value, most: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()][:most]


# ── 1. rewrite ──────────────────────────────────────────────────────────────────────


def _rewrite(settings: Settings, question: str) -> tuple[list[str], list[str], dict]:
    """(search texts, keywords, log record). Never raises."""
    out, info = _json_call(
        settings, settings.gemini_rewrite_model, REWRITE_PROMPT, question, 500, 20
    )
    if out is None:
        return [], [], info
    queries, keywords = _strings(out.get("queries"), 3), _strings(out.get("keywords"), 3)
    return queries, keywords, {**info, "queries": queries, "keywords": keywords}


# ── 2a. vector search ───────────────────────────────────────────────────────────────


def _embed(settings: Settings, texts: list[str]) -> list[list[float]]:
    status, body = _post(settings.embedder_url.rstrip("/") + "/embed", {"texts": texts}, {}, 30)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"embedder {status}: {str(body)[:200]}")
    return body["vectors"]


def rrf(lists: list[list[dict]], key=lambda p: p["id"]) -> list[dict]:
    """Lists merged by reciprocal rank: an item near the top of several lists comes first.
    The first copy of an item is kept; `score` becomes the best of its copies."""
    merged: dict = {}
    for items in lists:
        for rank, item in enumerate(items):
            k = key(item)
            entry = merged.setdefault(k, [dict(item), 0.0])
            entry[1] += 1 / (60 + rank + 1)
            if (item.get("score") or 0) > (entry[0].get("score") or 0):
                entry[0]["score"] = item["score"]
    return [e[0] for e in sorted(merged.values(), key=lambda e: e[1], reverse=True)]


def _search(settings: Settings, vectors: list[list[float]], limit: int | None = None) -> list[dict]:
    """The passages closest to any of the vectors: one Qdrant batch request, the lists merged
    by reciprocal rank."""
    limit = limit or settings.chat_passages
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
                        "hnsw_ef": max(128, 8 * limit),
                        "quantization": {"rescore": True, "oversampling": 2.0},
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
    lists = [
        [
            {"id": h["id"], **h["payload"], "score": round(h["score"], 4), "foundBy": "meaning"}
            for h in hits
        ]
        for hits in body["result"]
    ]
    return rrf(lists)[:limit]


# ── 2b. word search ─────────────────────────────────────────────────────────────────


async def _word_search(settings: Settings, keywords: list[str]) -> tuple[list[dict], dict]:
    """Pages holding each keyword exactly (the library's own full-text search), each cut to a
    passage around the match. Never raises; gives up after WORD_SEARCH_TIMEOUT seconds."""
    if not keywords:
        return [], {}
    from app.db import get_sessionmaker
    from app.services import search_service
    from app.services.arabic import find_original_match, normalize

    t = time.monotonic()

    async def one(keyword: str) -> list[dict]:
        async with get_sessionmaker()() as session:
            hits, _ = await search_service.search(
                session,
                query=keyword,
                page=1,
                limit=WORD_HITS_PER_KEYWORD,
                books_root=settings.books_root,
            )
        cache: dict = {}
        out = []
        for h in hits:
            text = search_service._load_page_text(
                settings.books_root, int(h.bookId), h.pageSequence, cache
            )
            if not text:
                continue
            m = find_original_match(text, normalize(keyword))
            mid = m.start() if m else 0
            start, end = max(0, mid - PASSAGE_RADIUS), min(len(text), mid + PASSAGE_RADIUS)
            while start > 0 and not text[start - 1].isspace():
                start -= 1
            while end < len(text) and not text[end].isspace():
                end += 1
            out.append(
                {
                    "id": f"w:{h.bookId}:{h.pageSequence}",
                    "bookId": h.bookId,
                    "title": h.title,
                    "volume": h.volume,
                    "page": h.page,
                    "sequence": h.pageSequence,
                    "chunk": None,
                    "text": text[start:end].strip(),
                    "score": 0.0,
                    "foundBy": "words",
                }
            )
        return out

    try:
        lists = await asyncio.wait_for(
            asyncio.gather(*(one(k) for k in keywords)), WORD_SEARCH_TIMEOUT
        )
        info = {"keywords": keywords, "hits": sum(len(x) for x in lists)}
    except Exception as exc:  # noqa: BLE001 -- a slow or failed word search: vectors alone
        lists, info = (
            [],
            {"keywords": keywords, "hits": 0, "error": f"{type(exc).__name__}: {exc}"[:200]},
        )
    info["ms"] = round((time.monotonic() - t) * 1000)
    return rrf(lists), info


def merge_candidates(meaning: list[dict], words: list[dict]) -> list[dict]:
    """The two searches' candidates in one list, by reciprocal rank; a page found by both
    keeps the vector passage."""
    return rrf([meaning, words], key=lambda p: (str(p["bookId"]), p["sequence"]))


# ── 3. rerank ───────────────────────────────────────────────────────────────────────


def _rerank(settings: Settings, question: str, candidates: list[dict]) -> tuple[list[dict], dict]:
    """The candidates that answer, best first, chosen by the cheap model; on any failure the
    merged order."""
    top = settings.chat_passages
    listing = "\n\n".join(
        f"[{n}] ({_where(p)})\n{(p.get('text') or '')[:RERANK_PASSAGE_CHARS]}"
        for n, p in enumerate(candidates, 1)
    )
    out, info = _json_call(
        settings,
        settings.gemini_rerank_model,
        RERANK_PROMPT.format(top=top),
        f"السؤال: {question}\n\nالمقاطع:\n\n{listing}",
        200,
        30,
    )
    info["candidates"] = len(candidates)
    best = []
    if out is not None and isinstance(out.get("best"), list):
        for n in out["best"]:
            if isinstance(n, int) and 1 <= n <= len(candidates) and n - 1 not in best:
                best.append(n - 1)
    info["best"] = [n + 1 for n in best]
    if not best:  # failed, or "none helps": keep the merged order, the answer will say so
        return candidates[:top], info
    return [candidates[i] for i in best[:top]], info


# ── 4. answer ───────────────────────────────────────────────────────────────────────

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


def _generate(settings: Settings, question: str, passages: list[dict], result: ChatResult) -> None:
    """The answer, from the first model that gives one; fills `result`."""
    models = chat_models(settings)
    llm_start = time.monotonic()
    status, body, model = 0, "", models[0]
    for model in models:
        t = time.monotonic()
        status, body = _gemini(settings, model, question, passages)
        result.attempts.append(
            {"model": model, "status": status, "ms": round((time.monotonic() - t) * 1000)}
        )
        if status not in RETRY_STATUSES:
            break
    result.timings["llmMs"] = round((time.monotonic() - llm_start) * 1000)
    result.model = model
    if status in RETRY_STATUSES:  # every model was busy or failed
        busy = any(a["status"] in (429, 503) for a in result.attempts)
        result.status = "rate_limited" if busy else "error"
        result.error = f"gemini {status}: {str(body)[:500]}"
        return
    if status != 200 or not isinstance(body, dict):
        result.status, result.error = "error", f"gemini {status}: {str(body)[:500]}"
        return
    answer, finish = read_answer(body)
    result.answer, result.finish_reason = answer, finish
    result.usage = body.get("usageMetadata") or {}
    result.status = "ok" if answer else "error"
    result.error = None if answer else f"no answer (finish reason {finish})"


# ── the whole chat ──────────────────────────────────────────────────────────────────


def _ms(t: float) -> int:
    return round((time.monotonic() - t) * 1000)


async def _ask(settings: Settings, question: str, result: ChatResult) -> None:
    timings = result.timings
    queries, keywords = [], []
    if settings.chat_rewrite:
        queries, keywords, result.rewrite = await asyncio.to_thread(_rewrite, settings, question)
        timings["rewriteMs"] = result.rewrite["ms"]
    texts = list(dict.fromkeys([question, *queries]))  # the message itself is always searched

    async def meaning() -> list[dict]:
        t = time.monotonic()
        vectors = await asyncio.to_thread(_embed, settings, texts)
        timings["embedMs"] = _ms(t)
        t = time.monotonic()
        found = await asyncio.to_thread(_search, settings, vectors, settings.chat_candidates)
        timings["searchMs"] = _ms(t)
        return found

    words_task = (
        _word_search(settings, keywords)
        if settings.chat_word_search
        else asyncio.sleep(0, result=([], {}))
    )
    found, (worded, result.words) = await asyncio.gather(meaning(), words_task)
    if result.words:
        timings["wordMs"] = result.words["ms"]
    candidates = merge_candidates(found, worded)
    if not candidates:
        result.status = "no_results"
        return

    passages = candidates[: settings.chat_passages]
    if settings.chat_rerank and len(candidates) > 1:
        passages, result.rerank = await asyncio.to_thread(_rerank, settings, question, candidates)
        timings["rerankMs"] = result.rerank["ms"]
    result.sources = passages
    await asyncio.to_thread(_generate, settings, question, passages, result)


async def answer(settings: Settings, question: str) -> ChatResult:
    if not (settings.gemini_api_key and settings.qdrant_api_key):
        raise ChatUnavailable
    t = time.monotonic()
    result = ChatResult(None, [], "error", chat_models(settings)[0])
    try:
        await _ask(settings, question, result)
    except Exception as exc:  # noqa: BLE001 -- the embedder or Qdrant failing is logged like any error
        result.status, result.answer = "error", None
        result.error = f"{type(exc).__name__}: {exc}"[:500]
    result.timings["totalMs"] = _ms(t)
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
        word_search=result.words or None,
        word_ms=result.words.get("ms"),
        rerank=result.rerank or None,
        rerank_tokens=(result.rerank.get("usage") or {}).get("totalTokenCount"),
        rerank_ms=result.rerank.get("ms"),
        sources=[
            {k: p.get(k) for k in ("bookId", "sequence", "chunk", "page", "score", "foundBy")}
            for p in result.sources
        ],
    )
    session.add(row)
    await session.commit()
    return row.id
