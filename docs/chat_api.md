# Chat API (prototype)

`POST https://api.thaqalaynlibrary.com/api/chat`

The app sends a question; the server finds the closest passages in the library (vector
search over all books), Gemini answers from those passages only, and the answer comes
back with its sources. No question rewriting and no reranking yet.

## Request

```http
POST /api/chat
Content-Type: application/json
X-Client-Id: 3f1c…            (optional: an anonymous install id, max 64 chars)
X-App-Version: 1.4.0           (optional, max 32 chars)

{"message": "ما فضل الصبر؟"}
```

`message`: 1–2000 characters.

## Response 200

```json
{
  "id": 123,
  "status": "ok",
  "answer": "الصبر … [1][3].",
  "sources": [
    {"n": 1, "bookId": "4407", "title": "…", "volume": "1", "page": "48",
     "sequence": 52, "text": "…the passage…", "score": 0.7123}
  ],
  "model": "gemini-3.5-flash-lite",
  "usage": {"promptTokens": 2900, "outputTokens": 180, "thoughtsTokens": null, "totalTokens": 3080},
  "timings": {"embedMs": 195, "searchMs": 400, "llmMs": 2100, "totalMs": 2700}
}
```

- `answer` is Markdown; `[n]` refers to `sources[].n`.
- Open a source with `GET /api/books/{bookId}/pages/{sequence}` (`page` is only the printed label).
- `status: "no_results"`: nothing was found; `answer` is null.
- `id`: the server's log entry for this question (useful when reporting a bad answer).

## Errors

| HTTP | Meaning | Body |
|------|---------|------|
| 422 | empty or too long message | `{"detail": …}` |
| 429 | Gemini's free-tier limit reached; retry after `Retry-After` seconds | `{"detail": "الخدمة مشغولة الآن، حاول بعد دقيقة.", "id": …}` |
| 502 | a step failed | `{"detail": "تعذّرت الإجابة الآن، حاول مرة أخرى.", "id": …}` |
| 503 | chat not enabled on the server | `{"detail": …}` |

A full answer takes a few seconds: show a waiting state. Every request, failed ones too,
is logged on the server with its tokens and timings (table `chat_requests`).
