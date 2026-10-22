"""POST /api/chat (the chatbot prototype), with the embedder, Qdrant and Gemini faked.

No database either: the log is written through chat_service.log_request, replaced here by
a recorder, so these tests check what is logged without needing Postgres.
"""

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import Settings, get_settings
from app.db import get_session
from app.main import create_app
from app.services import chat_service

PASSAGE = {
    "bookId": "4407",
    "title": "الصحيفة السجادية",
    "volume": "1",
    "authorDeath": "95",
    "sequence": 52,
    "page": "48",
    "chunk": 0,
    "text": "اللهم اجعل لي نوراً أمشي به في الناس",
}
GEMINI_OK = {
    "candidates": [
        {"content": {"parts": [{"text": "يطلب الداعي نوراً يمشي به [1]."}]}, "finishReason": "STOP"}
    ],
    "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 40, "totalTokenCount": 940},
}


def search_result(body, hits):
    """Qdrant's batch answer: one list of hits per search in the request."""
    return {
        "result": [
            [{"id": f"p{n}", "payload": h, "score": 0.71234} for n, h in enumerate(hits)]
            for _ in body["searches"]
        ]
    }


def is_rewrite(body):
    return (body.get("generationConfig") or {}).get("responseMimeType") == "application/json"


def fake_post(gemini_status=200, gemini_body=GEMINI_OK, hits=(PASSAGE,), rewrite=None):
    calls = []

    def post(url, body, headers, timeout):
        calls.append((url, body))
        if url.endswith("/embed"):
            return 200, {"vectors": [[0.0] * 1024 for _ in body["texts"]], "ms": 5}
        if "/points/search" in url:
            return 200, search_result(body, hits)
        if is_rewrite(body):
            return rewrite if rewrite else (500, "no rewrite in this test")
        return gemini_status, gemini_body

    return post, calls


@pytest.fixture
def logged(monkeypatch):
    rows = []

    async def record(session, question, result, client_id, app_version):
        rows.append(
            {"question": question, "result": result, "client": client_id, "version": app_version}
        )
        return len(rows)

    monkeypatch.setattr(chat_service, "log_request", record)
    return rows


@pytest.fixture
def app() -> FastAPI:
    app = create_app()
    settings = Settings(
        gemini_api_key="g", qdrant_api_key="q", chat_rewrite=False, chat_rerank=False
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = lambda: None
    return app


async def _ask(app, message, headers=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/chat", json={"message": message}, headers=headers or {})


async def test_answer_with_sources_usage_and_log(app, logged, monkeypatch):
    post, calls = fake_post()
    monkeypatch.setattr(chat_service, "_post", post)
    r = await _ask(app, "ما دعاء النور؟", {"X-Client-Id": "dev-1", "X-App-Version": "1.4"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["answer"].endswith("[1].")
    assert body["sources"][0] == {
        "n": 1,
        "bookId": "4407",
        "title": "الصحيفة السجادية",
        "volume": "1",
        "page": "48",
        "sequence": 52,
        "text": PASSAGE["text"],
        "score": 0.7123,
        "foundBy": "meaning",
    }
    assert body["usage"] == {
        "promptTokens": 900,
        "outputTokens": 40,
        "thoughtsTokens": None,
        "totalTokens": 940,
    }
    assert set(body["timings"]) == {"embedMs", "searchMs", "llmMs", "totalMs"}
    # Gemini got the passage, numbered, and the question
    sent = calls[-1][1]["contents"][0]["parts"][0]["text"]
    assert "[1] (الصحيفة السجادية، ج1، ص48)" in sent and sent.endswith("السؤال: ما دعاء النور؟")
    assert logged[0]["client"] == "dev-1" and logged[0]["version"] == "1.4"
    assert logged[0]["result"].usage["totalTokenCount"] == 940


async def test_rate_limit_is_429_and_still_logged(app, logged, monkeypatch):
    post, _ = fake_post(gemini_status=429, gemini_body="RESOURCE_EXHAUSTED")
    monkeypatch.setattr(chat_service, "_post", post)
    r = await _ask(app, "سؤال")
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    assert logged[0]["result"].status == "rate_limited"


async def test_nothing_found_skips_gemini(app, logged, monkeypatch):
    post, calls = fake_post(hits=())
    monkeypatch.setattr(chat_service, "_post", post)
    r = await _ask(app, "سؤال")
    assert r.status_code == 200 and r.json()["status"] == "no_results"
    assert not any("generateContent" in url for url, _ in calls)


async def test_a_failing_step_is_502_and_logged(app, logged, monkeypatch):
    def broken(url, body, headers, timeout):
        return 500, "embedder down"

    monkeypatch.setattr(chat_service, "_post", broken)
    r = await _ask(app, "سؤال")
    assert r.status_code == 502
    assert logged[0]["result"].status == "error" and "embedder 500" in logged[0]["result"].error


async def test_not_configured_is_503_without_a_log(logged):
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(gemini_api_key="", qdrant_api_key="")
    app.dependency_overrides[get_session] = lambda: None
    r = await _ask(app, "سؤال")
    assert r.status_code == 503 and not logged


async def test_empty_message_is_refused(app, logged):
    assert (await _ask(app, "   ")).status_code == 422
    assert not logged


def test_thought_parts_are_not_part_of_the_answer():
    response = {
        "candidates": [
            {
                "content": {"parts": [{"text": "تفكير", "thought": True}, {"text": "الجواب"}]},
                "finishReason": "STOP",
            }
        ]
    }
    assert chat_service.read_answer(response) == ("الجواب", "STOP")


async def test_a_busy_model_hands_over_to_the_next(app, logged, monkeypatch):
    tried = []

    def post(url, body, headers, timeout):
        if url.endswith("/embed"):
            return 200, {"vectors": [[0.0] * 1024 for _ in body["texts"]], "ms": 5}
        if "/points/search" in url:
            return 200, search_result(body, (PASSAGE,))
        model = url.split("/models/")[1].split(":")[0]
        tried.append(model)
        return (503, "high demand") if len(tried) == 1 else (200, GEMINI_OK)

    monkeypatch.setattr(chat_service, "_post", post)
    r = await _ask(app, "سؤال")
    assert r.status_code == 200
    assert tried == ["gemini-3.8-flash", "gemini-3.6-flash"]
    assert r.json()["model"] == "gemini-3.6-flash"
    attempts = logged[0]["result"].attempts
    assert [(a["model"], a["status"]) for a in attempts] == [
        ("gemini-3.8-flash", 503),
        ("gemini-3.6-flash", 200),
    ]


async def test_all_models_busy_is_429_with_every_attempt_logged(app, logged, monkeypatch):
    post, calls = fake_post(gemini_status=503, gemini_body="high demand")
    monkeypatch.setattr(chat_service, "_post", post)
    r = await _ask(app, "سؤال")
    assert r.status_code == 429
    assert [a["model"] for a in logged[0]["result"].attempts] == [
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
    ]


def test_model_order_has_no_repeats():
    s = Settings(gemini_chat_model="gemini-3.6-flash", gemini_fallback_models="gemini-3.6-flash, x")
    assert chat_service.chat_models(s) == ["gemini-3.6-flash", "x"]


# ── the rewrite step ────────────────────────────────────────────────────────────────

REWRITE_OK = (
    200,
    {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": '{"queries": ["فضل الصبر عن أبي عبد الله", "الصبر رأس الإيمان"]}'}
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 300,
            "candidatesTokenCount": 30,
            "totalTokenCount": 330,
        },
    },
)


@pytest.fixture
def rewriting_app() -> FastAPI:
    app = create_app()
    settings = Settings(
        gemini_api_key="g",
        qdrant_api_key="q",
        chat_rewrite=True,
        chat_rerank=False,
        chat_word_search=False,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = lambda: None
    return app


async def test_the_rewrite_texts_are_searched_with_the_message(rewriting_app, logged, monkeypatch):
    post, calls = fake_post(rewrite=REWRITE_OK)
    monkeypatch.setattr(chat_service, "_post", post)
    r = await _ask(rewriting_app, "اكتب لي مقالة عن فضل الصبر")
    assert r.status_code == 200
    body = r.json()
    assert body["searchQueries"] == ["فضل الصبر عن أبي عبد الله", "الصبر رأس الإيمان"]
    assert "rewriteMs" in body["timings"]
    rewrite_call = next(b for u, b in calls if "generateContent" in u and is_rewrite(b))
    assert "BGE-M3" in rewrite_call["systemInstruction"]["parts"][0]["text"]
    assert "gemini-3.5-flash-lite" in next(
        u for u, b in calls if "generateContent" in u and is_rewrite(b)
    )
    embedded = next(b for u, b in calls if u.endswith("/embed"))["texts"]
    assert embedded == [
        "اكتب لي مقالة عن فضل الصبر",
        "فضل الصبر عن أبي عبد الله",
        "الصبر رأس الإيمان",
    ]
    assert len(next(b for u, b in calls if "/points/search" in u)["searches"]) == 3
    # the answer is to the user's own message
    answer_call = [b for u, b in calls if "generateContent" in u and not is_rewrite(b)][-1]
    assert answer_call["contents"][0]["parts"][0]["text"].endswith(
        "السؤال: اكتب لي مقالة عن فضل الصبر"
    )
    result = logged[0]["result"]
    assert (
        result.rewrite["queries"] == body["searchQueries"]
        and result.rewrite["usage"]["totalTokenCount"] == 330
    )


async def test_a_failed_rewrite_searches_the_message_as_typed(rewriting_app, logged, monkeypatch):
    post, calls = fake_post(rewrite=(503, "high demand"))
    monkeypatch.setattr(chat_service, "_post", post)
    r = await _ask(rewriting_app, "ما فضل الصبر؟")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["searchQueries"] == []
    assert next(b for u, b in calls if u.endswith("/embed"))["texts"] == ["ما فضل الصبر؟"]
    assert logged[0]["result"].rewrite["status"] == 503


async def test_rewrite_that_is_not_json_is_ignored(rewriting_app, logged, monkeypatch):
    bad = (
        200,
        {"candidates": [{"content": {"parts": [{"text": "sorry"}]}, "finishReason": "STOP"}]},
    )
    post, calls = fake_post(rewrite=bad)
    monkeypatch.setattr(chat_service, "_post", post)
    assert (await _ask(rewriting_app, "سؤال")).status_code == 200
    assert "not JSON" in logged[0]["result"].rewrite["error"]


def test_merged_lists_put_a_passage_found_by_several_texts_first(monkeypatch):
    a, b, c = ({**PASSAGE, "chunk": n} for n in range(3))

    def post(url, body, headers, timeout):
        return 200, {
            "result": [
                [{"id": "a", "payload": a, "score": 0.9}, {"id": "c", "payload": c, "score": 0.5}],
                [{"id": "b", "payload": b, "score": 0.8}, {"id": "c", "payload": c, "score": 0.6}],
            ]
        }

    monkeypatch.setattr(chat_service, "_post", post)
    out = chat_service._search(Settings(qdrant_api_key="q", chat_passages=2), [[0.0], [0.0]])
    assert [p["chunk"] for p in out] == [2, 0] and out[0]["score"] == 0.6


# ── word search and rerank ──────────────────────────────────────────────────────────


def _passage(n, book="1"):
    return {**PASSAGE, "bookId": book, "sequence": n, "chunk": 0, "text": f"نص المقطع {n}"}


def test_candidates_found_by_both_searches_come_first_and_keep_the_vector_passage():
    meaning = [
        {**_passage(1), "id": "a", "foundBy": "meaning"},
        {**_passage(2), "id": "b", "foundBy": "meaning"},
    ]
    words = [
        {**_passage(2), "id": "w2", "text": "word text", "foundBy": "words"},
        {**_passage(3), "id": "w3", "foundBy": "words"},
    ]
    merged = chat_service.merge_candidates(meaning, words)
    assert [p["sequence"] for p in merged] == [2, 1, 3]
    assert merged[0]["foundBy"] == "meaning" and merged[0]["text"] == "نص المقطع 2"


def rerank_reply(best):
    return 200, {
        "candidates": [
            {"content": {"parts": [{"text": json.dumps({"best": best})}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"totalTokenCount": 5000},
    }


def test_rerank_picks_and_orders_the_passages(monkeypatch):
    seen = {}

    def post(url, body, headers, timeout):
        seen["prompt"] = body["contents"][0]["parts"][0]["text"]
        return rerank_reply([3, 1, 3, 99])  # a repeat and an unknown number are dropped

    monkeypatch.setattr(chat_service, "_post", post)
    cands = [_passage(n) for n in (1, 2, 3)]
    chosen, info = chat_service._rerank(Settings(gemini_api_key="g"), "سؤال", cands)
    assert [p["sequence"] for p in chosen] == [3, 1] and info["best"] == [3, 1]
    assert "[2] (الصحيفة السجادية، ج1، ص48)" in seen["prompt"] and seen["prompt"].startswith(
        "السؤال: سؤال"
    )


def test_a_failed_or_empty_rerank_keeps_the_merged_order(monkeypatch):
    cands = [_passage(n) for n in range(1, 12)]
    for reply in ((503, "busy"), rerank_reply([])):
        monkeypatch.setattr(chat_service, "_post", lambda *a, reply=reply: reply)
        chosen, _ = chat_service._rerank(Settings(gemini_api_key="g", chat_passages=8), "q", cands)
        assert [p["sequence"] for p in chosen] == list(range(1, 9))


async def test_the_whole_chat_with_word_search_and_rerank(monkeypatch, logged):
    app = create_app()
    settings = Settings(gemini_api_key="g", qdrant_api_key="q")  # every step on
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = lambda: None
    word_hit = {**_passage(9, book="77"), "id": "w:77:9", "foundBy": "words", "score": 0.0}

    async def fake_words(s, keywords):
        assert keywords == ["الصبر رأس الإيمان"]
        return [word_hit], {"keywords": keywords, "hits": 1, "ms": 12}

    def post(url, body, headers, timeout):
        if url.endswith("/embed"):
            return 200, {"vectors": [[0.0] for _ in body["texts"]], "ms": 5}
        if "/points/search" in url:
            return 200, search_result(body, (PASSAGE,))
        system = body["systemInstruction"]["parts"][0]["text"]
        if system.startswith("You prepare searches"):
            return 200, {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "queries": ["فضل الصبر"],
                                            "keywords": ["الصبر رأس الإيمان"],
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ],
                "usageMetadata": {"totalTokenCount": 300},
            }
        if system.startswith("You choose passages"):
            return rerank_reply([2])  # the word-search page only
        return 200, GEMINI_OK

    monkeypatch.setattr(chat_service, "_word_search", fake_words)
    monkeypatch.setattr(chat_service, "_post", post)
    r = await _ask(app, "ما فضل الصبر؟")
    body = r.json()
    assert r.status_code == 200 and [s["bookId"] for s in body["sources"]] == ["77"]
    assert body["sources"][0]["foundBy"] == "words"
    assert {"rewriteMs", "embedMs", "searchMs", "wordMs", "rerankMs", "llmMs", "totalMs"} <= set(
        body["timings"]
    )
    result = logged[0]["result"]
    assert result.rerank["best"] == [2] and result.rerank["usage"]["totalTokenCount"] == 5000
    assert result.words["hits"] == 1
