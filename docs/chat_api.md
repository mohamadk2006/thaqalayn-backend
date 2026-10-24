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
| 429 | A limit was reached; `code` says which, `Retry-After` is the seconds to wait | `{"code": "daily_limit", "detail": "…", "limit": 10, "remaining": 0, "resetAt": "2026-10-23T00:00:00+00:00"}` |
| 422 | Question longer than 500 characters | `{"code": "too_long", "detail": "…"}` |
| 502 | a step failed | `{"detail": "تعذّرت الإجابة الآن، حاول مرة أخرى.", "id": …}` |
| 503 | chat not enabled on the server | `{"detail": …}` |

A full answer takes a few seconds: show a waiting state. Every request, failed ones too,
is logged on the server with its tokens and timings (table `chat_requests`).


## Limits

Counted per asker: the signed-in user (`Authorization: Bearer`), else `X-Client-Id`, else the address.

| `code` | Meaning | Default |
|---|---|---|
| `daily_limit` | Questions per asker per day, reset at 00:00 UTC | 10 |
| `too_fast` | Questions per asker per minute | 3 |
| `in_progress` | The asker's previous question is still being answered | 1 at a time |
| `service_limit` | Everyone's questions today (protects the free Gemini quota) | 150 |

A 200 answer carries `quota: {limit, remaining, resetAt}` so the app can show the questions left. Failed questions do not count against the asker's daily quota. Settings: `CHAT_DAILY_LIMIT`, `CHAT_PER_MINUTE`, `CHAT_GLOBAL_DAILY_LIMIT`, `CHAT_MAX_CHARS` (0 = no limit).

## Search by meaning (no chatbot)

`GET /api/search/meaning?q=…&limit=20&bookId=4407&bookId=12` (limit 1-100) returns the passages of the library
closest in meaning to `q`, with no Gemini call and no daily quota:

```json
{"query": "…", "items": [{"bookId": "4407", "title": "…", "author": "…", "volume": "1",
  "page": "48", "sequence": 52, "text": "the ~220-word passage", "score": 0.71}]}
```

`bookId` (optional, repeatable, up to 50) limits the search to those books, e.g. "search this book
by meaning". Open the page with `GET /api/books/{bookId}/pages/{sequence}`. Also takes the same filters as `GET /api/search` (`subject`, `library`, `language`, `author`,
`authorName`, `work`), combined the same way; `bookId` narrows further. A filter that selects no
book returns `items: []`. 30 searches a minute per address (429 `too_fast`); the same search is answered from a 5-minute cache. One result per page (its best chunk).


## Showing the steps: POST /api/chat/stream

Same request, headers, limits and errors as `POST /api/chat` (a refused request is a plain JSON
error with its status code, before any stream). When accepted the answer is a stream of
server-sent events (`Content-Type: text/event-stream`), so the app can show where the question is:

```
event: step
data: {"step":"rewrite","state":"start"}

event: step
data: {"step":"rewrite","state":"done","ok":true,"queries":["…"],"keywords":["…"],"authors":["الشيخ المفيد"],"works":[]}

event: step
data: {"step":"search","state":"start"}

event: step
data: {"step":"search","state":"done","candidates":20,"byWords":3}

event: step
data: {"step":"rerank","state":"start"}   …  {"step":"rerank","state":"done","chosen":8}

event: step
data: {"step":"answer","state":"start","passages":8}

event: done
data: { …the same body as POST /api/chat… }
```

Steps, in order (a step can be missing: `rewrite` and `rerank` are skipped when switched off):

| step | meaning | text for the user |
|---|---|---|
| `rewrite` | the question is understood and turned into search texts; `authors`/`works` are the named authors and books the search is limited to | «أفهم سؤالك…» |
| `search` | meaning search and word search in the library; `candidates` = passages found | «أبحث في الكتب…» |
| `rerank` | the best passages are chosen; `chosen` = how many | «أختار أنسب المقاطع…» |
| `answer` | the answer is written from the passages (the longest step, 5-60 s) | «أكتب الإجابة…» |

The last event is `done` (the answer, as `POST /api/chat` returns it) or `error`
(`{"detail": "…", "id": …}`, the same bodies as that endpoint's 429 and 502). Lines starting with
`:` are keep-alive comments: ignore them. A stream that ends with neither is a failure.

Swift sketch (iOS 15+):

```swift
var req = URLRequest(url: URL(string: "https://api.thaqalaynlibrary.com/api/chat/stream")!)
req.httpMethod = "POST"
req.setValue("application/json", forHTTPHeaderField: "Content-Type")
req.httpBody = try JSONEncoder().encode(["message": question])
req.timeoutInterval = 180
let (bytes, response) = try await URLSession.shared.bytes(for: req)
guard (response as? HTTPURLResponse)?.statusCode == 200 else { /* plain JSON error: limits, 503… */ }
var event = ""
for try await line in bytes.lines {
    if line.hasPrefix("event: ") { event = String(line.dropFirst(7)) }
    else if line.hasPrefix("data: ") {
        let data = Data(line.dropFirst(6).utf8)
        switch event {
        case "step": /* decode {step, state, …}: update the progress text */ break
        case "done": /* decode ChatOut: show the answer and sources */ break
        case "error": /* decode {detail, id}: show detail */ break
        default: break
        }
    }
}
```

Named authors and books: when the question names one ("ماذا قال الشيخ المفيد عن…"), the search is
limited to his books (if that finds too little, the whole library is searched). The names matched
arrive in the `rewrite` step's `authors` and `works`, and `searchQueries` in the final body is
unchanged.
