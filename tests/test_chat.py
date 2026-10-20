"""POST /api/chat (the chatbot prototype), with the embedder, Qdrant and Gemini faked.

No database either: the log is written through chat_service.log_request, replaced here by
a recorder, so these tests check what is logged without needing Postgres.
"""

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


def fake_post(gemini_status=200, gemini_body=GEMINI_OK, hits=(PASSAGE,)):
    calls = []

    def post(url, body, headers, timeout):
        calls.append((url, body))
        if url.endswith("/embed"):
            return 200, {"vectors": [[0.0] * 1024], "ms": 5}
        if "/points/search" in url:
            return 200, {"result": [{"payload": h, "score": 0.71234} for h in hits]}
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
    settings = Settings(gemini_api_key="g", qdrant_api_key="q")
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
