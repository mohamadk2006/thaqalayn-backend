# Website developer brief: study sessions, sign-in and sync, meaning search, AI chat

For the developer of www.thaqalaynlibrary.com. Everything below is live on the API unless it says
otherwise. The iPhone app already uses (or is about to use) the same endpoints, so the website and the
phone share one account and one set of data.

Read section 0 first. It has the rules that every part depends on. The parts are independent after that,
but the order below is the order I recommend building them in:

1. Sign-in (section 1): everything else needs it, except meaning search.
2. Meaning search (section 3): no account needed, quick win.
3. Sync (section 2) and study sessions (section 4): the user's data follows them between phone and website.
4. AI chat (section 5): needs sign-in, has limits.

Related documents in the backend repo: `docs/accounts_sync_spec.md` (the accounts and sync spec, the
authority on merge rules), `docs/chat_api.md` (the chat contract).

---

## 0. Ground rules

**Base URL:** `https://api.thaqalaynlibrary.com/api` (all paths below are relative to it).
JSON in and out, UTF-8, Arabic text is right-to-left.

**CORS.** The API accepts browser calls from `https://www.thaqalaynlibrary.com`,
`https://thaqalaynlibrary.com` and `http://localhost:<any port>` / `http://127.0.0.1:<any port>`.
Allowed methods: GET, HEAD, POST, PATCH, DELETE. Credentials/cookies are NOT used: authentication is a
bearer token in the `Authorization` header. Do not set `credentials: "include"`. The response headers
`ETag` and `Retry-After` are readable from JavaScript.
(Backend note: the POST/PATCH/DELETE permission was added on 2026-10-24 and goes live with the next API
deploy. If a preflight fails with a CORS error on a POST, the deploy has not happened yet: ask the
backend owner.)

**Errors.** Account, sync and chat errors are JSON with a stable machine `code` and a human `detail`
(Arabic, safe to show), plus extra fields for some codes:

```json
{"code": "code_invalid", "detail": "الرمز غير صحيح.", "attemptsLeft": 3}
```

Write your logic on `code` and the HTTP status. Show `detail` to the user. A few errors (FastAPI's own
validation, 422) have a different shape: `{"detail": [...]}`. Treat any non-2xx without a `code` as a
generic failure.

**Authorization.** `Authorization: Bearer <accessToken>`. Any request with a missing or expired token gets
`401 {"code": "token_expired"}`. Your HTTP layer handles it in one place: refresh the token once (see 1.3),
retry the original request once, and if it still fails, treat the user as signed out.

**429.** Always carries a `Retry-After` header (seconds). Show a countdown, do not retry in a loop.

**Time.** ISO-8601 UTC with milliseconds, `new Date().toISOString()` is right ("2026-10-24T09:12:03.120Z").

**IDs.** Record ids are UUID strings in UPPER CASE (the iPhone app writes them that way). Generate new ones
with `crypto.randomUUID().toUpperCase()`. Always compare ids case-insensitively.

**Never** log or persist tokens anywhere except the storage described in 1.3. Never put a token or an
email in a URL.

---

## 1. Sign-in (email + one-time code)

There are no passwords and no Apple/Google sign-in. The user types their email, receives a 6-digit code,
types it, and is signed in. The first sign-in with a new email creates the account.

### 1.1 The device id

Create a random id once per browser and keep it in `localStorage` (key e.g. `thaqalayn.deviceId`):
`crypto.randomUUID()`, at most 64 characters. Send it with every sign-in. It identifies this browser in the
account's device list and in sync. Platform is always `"web"`. `deviceName` (at most 64 characters) is shown
to the user in the device list, for example "Chrome on macOS" built from the user agent.

### 1.2 The two calls

**Step 1: ask for a code.** `POST /auth/email/start`

```json
{"email": "user@example.com", "language": "ar"}
```

`language` is `ar`, `fa` or `en` (the language of the email; default `ar`). Send the email as typed (the
server trims and lowercases it). Answer is always `202` (it does not reveal whether the account exists):

```json
{"resendAfter": 60, "expiresIn": 600}
```

Show the code field, a 60-second countdown before "send again" is enabled, and say the code is valid for
10 minutes. A new code cancels the earlier one: only the latest email works.

Errors: `422 email_invalid`; `429` (`code: "rate_limited"`, with `Retry-After`; limits are one code per
email per 60 s, 5 per email per hour, 20 per IP per hour); `503 email_unavailable` ("could not send the
email, try again shortly"); `503 auth_unavailable` (sign-in is switched off on the server).

**Step 2: check the code.** `POST /auth/email/verify`

```json
{"email": "user@example.com", "code": "042917",
 "deviceId": "3f1c…", "deviceName": "Chrome on macOS", "platform": "web"}
```

Send the code as a string (it can start with zero). Answer `200`:

```json
{"accessToken": "…", "accessExpiresIn": 3600, "refreshToken": "…",
 "user": {"id": "b7e2…", "email": "user@example.com", "createdAt": "2026-10-22T09:00:00Z"},
 "isNewAccount": false}
```

Errors, all `400`: `code_invalid` (with `attemptsLeft`: show "wrong code, N tries left"), `code_expired`
(offer "send a new code"), `code_locked` (5 wrong tries kill the code: the user must request a new one).
`429` after 30 verify calls per IP per hour.

On success: store the tokens (1.3), store the user, then run the first sync (section 2.5).

### 1.3 Tokens: this is where websites get it wrong

- Both tokens are opaque strings, not JWTs. Do not try to decode them.
- **Access token**: valid 1 hour (`accessExpiresIn` seconds). Keep it in memory (a module variable) and
  re-create it from a refresh after a page reload. Send it as the bearer token.
- **Refresh token**: valid 180 days, and **it changes every time you use it** (rotation). Store it in
  `localStorage` (the API is on another origin, so an httpOnly cookie is not an option). Because it sits in
  `localStorage`, the site needs a strict Content-Security-Policy and no inline script injection.
- **Refresh**: `POST /auth/refresh` with `{"refreshToken": "…"}` (no Authorization header). Answer:
  `{"accessToken", "accessExpiresIn", "refreshToken"}`. **Save the new refresh token immediately**,
  before doing anything else.
- **Reuse detection:** if an already-used refresh token is sent again, the server assumes it was stolen and
  **revokes the whole login**: the user has to sign in again. So two tabs must never refresh at the same time
  with the same token. Use a single-flight lock across tabs:

```ts
async function refreshAccessToken(): Promise<string> {
  return navigator.locks.request("thaqalayn-refresh", async () => {
    // another tab may have refreshed while we waited for the lock: if the stored refresh
    // token is not the one we started with, just use its fresh access token instead
    const stored = localStorage.getItem("thaqalayn.refreshToken");
    if (!stored) throw new SignedOut();
    if (memory.accessToken && memory.accessTokenFor === stored) return memory.accessToken;
    const r = await fetch(`${API}/auth/refresh`, {
      method: "POST", headers: {"content-type": "application/json"},
      body: JSON.stringify({refreshToken: stored}),
    });
    if (!r.ok) { signOutLocally(); throw new SignedOut(); }   // 401 refresh_invalid
    const t = await r.json();
    localStorage.setItem("thaqalayn.refreshToken", t.refreshToken);
    memory.accessToken = t.accessToken; memory.accessTokenFor = t.refreshToken;
    return t.accessToken;
  });
}
```

  Other tabs learn about sign-in/sign-out and new tokens through the `storage` event or a
  `BroadcastChannel`; a tab that finds a new refresh token in storage must obtain its access token with
  `refreshAccessToken()` rather than reuse its own stale one.
- `401 refresh_invalid` means the login ended (signed out elsewhere, expired, or reuse detected): clear the
  local tokens and show the sign-in screen. Keep the local data (see 2.6).

### 1.4 The account screen

- `GET /account` returns `{id, email, createdAt, devices: [{deviceId, deviceName, platform, lastSeenAt,
  current}]}`. Show the signed-in email and the list of devices ("Chrome on macOS, this device", "iPhone 16,
  last seen …"). The device with `current: true` is this browser.
- `DELETE /account/devices/{deviceId}` signs that device out (204). Offer it for every other device.
  `404 device_not_found` if it is already gone.
- `POST /auth/logout` (bearer) ends this login (204). After it, ask the user whether to keep or remove this
  browser's local copy of their data (2.6).
- `DELETE /account` deletes the account and ALL its synced data on the server immediately (204). It cannot
  be undone. Put it behind a clear confirmation (type the email or a second confirm button). Then clear
  local tokens and, after asking, local data.

---

## 2. Sync (notes, highlights, marks, bookmarks, reading progress, study sessions)

The server stores each synced item as a **record** identified by `(kind, key)`, with an opaque JSON `data`.
The website keeps its own local copy (use IndexedDB), works offline, and exchanges changes with the server
by **pull** (get what changed) and **push** (send what changed). All sync calls need the bearer token.

### 2.1 What the website must store for each record

- `kind`, `key`, `data` (the fields from section 2.3), `deleted` (bool), `updatedAt` (ISO string),
  `orderChangedAt` (ISO string or null; only for kinds that have a manual order),
- `seq`: the server's sequence number the last time this record came from the server (null if it never did).
  You send it back as `baseSeq` when you push a change.
- a "dirty" flag: changed locally and not pushed yet.
- one **cursor** per account (an integer from the last pull, starting at 0).

### 2.2 The two calls

**Pull:** `GET /sync/changes?since=<cursor>&limit=500`

```json
{"cursor": 18342, "hasMore": false,
 "items": [
  {"kind": "note", "key": "6F1C…", "deleted": false, "updatedAt": "2026-10-22T09:12:03.120Z",
   "orderChangedAt": null, "seq": 18300, "data": {"title": "…", "bodyText": "…"}},
  {"kind": "highlight", "key": "A93D…", "deleted": true, "updatedAt": "2026-10-22T09:13:00.000Z",
   "seq": 18342, "data": null}
 ]}
```

Start with `since=0`. Keep pulling while `hasMore` is true, each time with the `cursor` from the previous
answer; save the final cursor. A record with `deleted: true` is a tombstone: remove it locally (but keep
the tombstone row so an old edit cannot bring it back). Limit is at most 1000.

**Push:** `POST /sync/push`

```json
{"deviceId": "3f1c…",
 "items": [
  {"kind": "note", "key": "6F1C…", "deleted": false, "updatedAt": "2026-10-24T09:12:03.120Z",
   "orderChangedAt": null, "baseSeq": 18300, "data": {"title": "…", "bodyText": "…"}}
 ]}
```

At most 500 items and 2 MB per request; at most 256 KB for one item's `data`. Send a deletion as the same
item with `"deleted": true` and `"data": null` (a deletion is just a write). `baseSeq` is the `seq` this
record had when you last pulled it (`null` if it never came from the server). Answer:

```json
{"results": [
  {"kind": "note", "key": "6F1C…", "status": "applied", "seq": 18343},
  {"kind": "bookmark", "key": "-|4407|p-000052-b-001", "status": "older_ignored", "seq": 18001},
  {"kind": "note", "key": "77AB…", "status": "applied_with_copy", "seq": 18344, "copyKey": "D0E5…"}
]}
```

Statuses: `applied`; `older_ignored` (the server has a newer version: fine, the next pull brings it);
`applied_with_copy` and `older_ignored_with_copy` (a note conflict: the losing text was saved as a new
note, `copyKey`, which arrives on the next pull); `rejected_kind` (unknown kind), `rejected_size`,
`rejected_key` (empty or longer than 512 characters), `rejected_time` (updatedAt before 2020). One rejected
item does not stop the others. Errors: `413 payload_too_large`, `422 body_invalid`, `401 token_expired`.
After every push, **pull again**: that brings back your own writes with their new `seq`, plus any copies.
Update each pushed record's `seq` from its result and clear its dirty flag (also for `older_ignored`).

### 2.3 The record kinds

| `kind` | `key` | `data` fields | `updatedAt` is |
|---|---|---|---|
| `note` | note id | `title`, `bodyText`, `hasCustomTitle`, `sessionId?`, `folderId?`, `sortIndex?`, `createdAt`, `copyOf?` | last text edit |
| `noteFolder` | folder id | `title`, `sessionId?`, `sortIndex`, `createdAt` | last rename |
| `session` | session id | `title`, `sortIndex?`, `createdAt` (no book list) | last rename |
| `sessionBook` | `{sessionId}\|{bookId}` | `sessionId`, `bookId`, `position` (number, ascending) | when added |
| `highlight` | highlight id | `bookId`, `sessionId?`, `paragraphId`, `start`, `end`, `color` (`yellow` `green` `blue` `pink` `purple`), `quotedText?`, `contentVersion?`, `createdAt` | creation (never edited in place) |
| `numberedMark` | mark id | `bookId`, `sessionId?`, `paragraphId`, `offset`, `number`, `noteId`, `spans: [{paragraphId, start, end}]`, `quotedText?`, `contentVersion?`, `createdAt` | last renumber |
| `bookmark` | `{sessionId or "-"}\|{bookId}\|{locationId}` | `bookId`, `sessionId?`, `locationId`, `createdAt` | creation |
| `readingProgress` | `{bookId}` | `bookId`, `lastLocationId`, `progressPercent` (0 to 1), `pageScrollFraction?` | last read |
| `sessionProgress` | `{sessionId}\|{bookId}` | same fields as `readingProgress`, plus `sessionId` | last read |

Rules for the data (get these exactly right, or the iPhone will show wrong positions):

- `bookId` is always the plain catalog book id as a string (the id in `/books/{id}`), never an app-specific
  key. `sessionId` is `null`/absent for the main library and the session's id for something made inside a
  study session. A thing made inside a session belongs only to that session.
- **Positions.** `paragraphId` and `locationId` are the page block ids that `GET /books/{id}/pages/{seq}`
  returns in `page.blocks[].id`, for example `p-000052-b-001` (the number after `p-` is the page sequence).
  `start`, `end` and `offset` count **UTF-16 code units** inside that block's `text`. JavaScript string
  indexes already are UTF-16, so use them directly (do NOT convert to code points or use `Intl.Segmenter`
  indexes). The text they index into is the block text exactly as the API returns it (with diacritics).
- A highlight that crosses blocks is several `highlight` records, one per block.
- Store `quotedText` (the selected text) and `contentVersion` (the page response's `contentVersion` at that
  moment). If a book's text is later updated, they let a client place the mark again.
- Keys and ids are compared case-insensitively; send them the way you created them.
- Keep unknown extra fields in `data` untouched when you edit and push a record: other apps may add fields.
- Not synced: downloads, search history, reader settings.

### 2.4 Rules the website must follow when changing a record

- Every local change sets `updatedAt = now` (ISO, UTC). Deleting a record = keep it locally as a tombstone
  with `deleted: true` and `updatedAt = now`, and push it. Do not remove the row until the server has
  accepted it.
- **Newer write wins** (by `updatedAt`), per record. The server decides on push; the answer tells you.
  Your clock matters: if `updatedAt` is more than 5 minutes ahead of the server it is clamped.
- **Manual order is separate from content.** Records with an order (`note`, `noteFolder`, `session`, and the
  `position` of a `sessionBook`) have `orderChangedAt`. When the user only drags things into a new order,
  update `sortIndex`/`position` and `orderChangedAt` and DO NOT touch `updatedAt`. When the user edits
  content, update `updatedAt` and leave `orderChangedAt` alone. The server merges the two separately, so an
  edit on one device and a reorder on another both survive.
- **Notes: conflicts make a copy.** If someone edits a note on the phone and in the browser at the same time,
  the newer text wins and the older text is kept as a separate note titled "original title (نسخة محفوظة)"
  with `copyOf` set to the original id. It reaches you on the next pull as an ordinary new note. Nothing to
  do except to display it normally (and not hide notes whose title ends with "(نسخة محفوظة)"). For this to
  work you MUST send a correct `baseSeq` with every note you push (see brief 2, section 7: a record changed
  locally and also changed on the server keeps its OLD `seq` when pushed).
- **The server does not cascade deletes.** Deleting a study session means you also push tombstones for its
  `sessionBook`s, `sessionProgress`, notes, folders, highlights, numbered marks and bookmarks that have that
  `sessionId`, in the same push.

### 2.5 When to sync

1. **On sign-in:** pull everything from cursor 0 and merge it into the local data, THEN push every local
   record that has never been pushed. The browser's existing local data (what the user made before signing
   in) is merged into the account, never thrown away. Merging a pulled record with a local one is the same
   rule as on the server: the newer `updatedAt` wins, ties by `deviceId` in string order.
2. **Then:** on page load, whenever the tab becomes visible again, on `online`, and about 3 seconds after
   the last local edit (batch edits: one push for many records). A periodic pull every few minutes while the
   tab is open is enough; there is no push notification.
3. Never run two syncs at once in one browser (use a lock/queue). If a sync fails, retry later with
   back-off; local data is never lost. On 401, refresh the token once and retry.

### 2.6 Signing out

On sign-out ask: "Keep this browser's copy of my data, or remove it?" The server is not involved. If the
user keeps it, the records stay local, the cursor is reset, and a later sign-in merges again (2.5.1). If a
DIFFERENT account signs in on this browser, never merge the previous account's local data into it: ask to
remove the old local data first.

---

## 3. Meaning search

Finds passages that mean the same as the query, even with different words. Public: no sign-in, no quota.
It complements the existing word search (`GET /search`), it does not replace it.

`GET /search/meaning?q=<text>&limit=50`

- `q`: 2 to 500 characters, a question or a topic in words.
- **`limit`: send `50`** (the server accepts 1 to 100; default 20). Use 50 for the results page.
- Optional filters, exactly the same as `GET /search` (combined the same way: AND between kinds, OR within
  one kind; `work` is added to what the others select): `subject`, `library`, `language`, `author`,
  `authorName`, `work`. All repeatable (`?subject=a&subject=b`). Pass whatever filters the user chose on
  the search screen.
- Optional `bookId` (repeatable, at most 50): search only those books, for a "search this book by meaning"
  box in the reader. If both filters and `bookId` are given, the result is limited to their intersection.
- No paging: ask for `limit` and show the list.

```json
{"query": "…",
 "items": [{"bookId": "4407", "title": "الصحيفة السجادية", "author": "الإمام زين العابدين",
            "volume": "1", "page": "48", "sequence": 52,
            "text": "the passage, about 220 words", "score": 0.71}]}
```

`author`, `volume` and `page` can be `null`. `page` is the printed page label; `sequence` is the page's
position in the book. **Open a result with `sequence`**: `GET /books/{bookId}/pages/{sequence}`, not with
the printed `page`. One result per page, best first; `score` (0 to 1) is for ordering, do not show it as a
percentage. The passage `text` is a piece of that page: in the reader, scroll to it or highlight it if you
can find it in the page text.

Errors: `429 {"code": "too_fast"}` (30 searches a minute per IP; `Retry-After`); `422` (q too short/long);
`502`/`503` ("search is unavailable now"). A filter that matches no book returns `items: []`.

UI: put a "By meaning" toggle (or second tab) beside the word search. Meaning search takes 1 to 3 seconds:
show a loader and search on submit, not on every keystroke. Say clearly that it finds related passages, not
exact words, so users do not expect their exact phrase to be in every result.

---

## 4. Study sessions (the "Sessions" tab)

A study session is a named group of books that the user studies together, with its own notes, folders,
highlights, numbered marks, bookmarks and reading progress. The iPhone app has this feature; the website
does not yet. The backend already stores everything (sync kinds `session`, `sessionBook`, `sessionProgress`
and the `sessionId` field on notes, folders, highlights, marks and bookmarks, see 2.3). The website needs a
"Sessions" tab that reads and writes the same records.

**What the data model guarantees (build to this):**

- A session = one `session` record (`title`, optional `sortIndex`, `createdAt`). It has NO book list.
- Its books are separate `sessionBook` records, one per book: key `{sessionId}|{bookId}`, `position` sorts
  them ascending. Adding a book on the phone and removing a different book on the website both survive.
  Reordering books changes only `position` and `orderChangedAt`.
- Everything the user makes while reading inside a session (notes, note folders, highlights, numbered marks,
  bookmarks) has that session's `sessionId` and belongs only to it. The same book read in the main library
  has `sessionId` null and a separate set of notes/highlights/marks/bookmarks. Reading position inside a
  session is `sessionProgress` (key `{sessionId}|{bookId}`); in the main library it is `readingProgress`
  (key `{bookId}`).
- A session's list order is `sortIndex` with `orderChangedAt` (rule 2.4).
- Deleting a session also deletes (tombstones) everything that has its `sessionId` (rule 2.4).

**Screens to build** (mirror the iPhone app's behaviour and wording; ask the iPhone developer for its
screens and Arabic labels so both feel the same):

1. **Sessions list**: all sessions by `sortIndex` (then `createdAt`), each with its title and number of
   books; create, rename, reorder, delete (with confirmation: it deletes the session's notes and marks too).
2. **A session**: its books by `position` (add a book from the library search or catalog, remove, reorder),
   the session's notes and note folders, and a "continue reading" per book from `sessionProgress`.
3. **Reading inside a session**: the normal reader, but highlights, numbered marks, bookmarks and notes are
   created with this `sessionId`, and only this session's are shown. Progress writes `sessionProgress`.
4. **Notes**: a note has a `title` (`hasCustomTitle` true once the user edits the title, otherwise the title
   is derived from the first line) and `bodyText`; numbered marks link a place in a book to a note line by
   number (`noteId`, `number`: a line "N. …" in the note refers to mark N). The exact note format and rules are in brief 2.

**Notes, highlights, numbered marks, folders and the session screens in detail** (exact behaviour taken from
the iPhone app, algorithms, labels, colours, examples, known gaps): see **`docs/website_notes_marks_brief.md`**
(brief 2). Build sections 4 and the notes features from that document; this section only gives the overview.

---

## 5. AI chat

A chatbot that answers questions from the library's books, with citations. **Signing in is required.**
Each user has a daily quota. The full contract is in `docs/chat_api.md`; this section is what a web client
must do.

### 5.1 What the user sees

A chat screen like a messenger: a list of the user's chats (with "new chat"), the messages of the open chat,
an input with a character counter, and under the input "N of M questions left today". Answers are Markdown
(headings, lists, bold) in Arabic RTL, with citation markers like `[1]`, `[2][4]` that link to the sources
list under the answer (book title, volume, page, the passage text, and a link that opens the book page).

### 5.2 Calls (all need the bearer token)

| Call | Does |
|---|---|
| `GET /chat/quota` | `{"limit": 10, "remaining": 6, "resetAt": "2026-10-25T00:00:00+00:00", "maxChars": 500, "blocked": null}`. Free, not counted. Call when the chat screen opens and after a refusal. `limit` 0 = no limit. `blocked` = why a question would be refused now (`daily_limit`, `service_limit`, `too_fast`, `in_progress`) or null. `resetAt` is UTC midnight: show it in the user's local time. |
| `POST /chat/stream` | Ask a question and get the answer as a live stream (5.3). Use this one. |
| `POST /chat` | The same without streaming (waits for the whole answer, 10 to 60 s). Only as a fallback. |
| `GET /chat/conversations?limit=30&before=<nextBefore>` | The user's chats, newest first: `{"items": [{id, title, createdAt, updatedAt}], "nextBefore": "…" or null}`. Use `nextBefore` as `before` for the next page. |
| `GET /chat/conversations/{id}` | One chat with `messages` oldest first: `{id, role: "user" or "assistant", text, status, sources, createdAt}`. An assistant message with `status: "no_results"` has an empty `text`: show "لم أجد ما يخص سؤالك في الكتب". |
| `PATCH /chat/conversations/{id}` | `{"title": "…"}` renames. |
| `DELETE /chat/conversations/{id}` | 204. Gone at once (removed for good after 30 days). Ask the user to confirm. |

Request body of `POST /chat/stream` and `POST /chat`:

```json
{"message": "ما فضل الصبر؟", "conversationId": "6f1c1f0e-5d1e-4b6a-9d58-0a1f3a3b9c11"}
```

- `message`: at most 500 characters (`maxChars` from the quota call). Count characters in the input and do not
  allow sending more. The server answers `422 {"code": "too_long"}` otherwise.
- `conversationId`: omit it for the first question of a new chat; send the chat's id for every later
  question in that chat. **Do not send the earlier messages**: the server keeps the history and understands
  follow-ups («وماذا عن الإمام الصادق؟»). Optional header `X-App-Version` (any string, logged).

### 5.3 Reading the stream (use fetch, not EventSource)

`EventSource` only does GET and cannot send the Authorization header, so use `fetch` with the response
body reader. The response is `text/event-stream`: blocks separated by a blank line, each with
`event: <name>` and `data: <one-line JSON>`. Lines starting with `:` are keep-alive pings: ignore them.

```ts
async function ask(message: string, conversationId: string | null, on: Handlers, signal: AbortSignal) {
  const res = await authedFetch(`${API}/chat/stream`, {
    method: "POST", signal,
    headers: {"content-type": "application/json", accept: "text/event-stream"},
    body: JSON.stringify(conversationId ? {message, conversationId} : {message}),
  });
  if (!res.ok) throw await chatRefusal(res);          // 401 (after one refresh), 404, 409, 422, 429, 503: plain JSON
  const reader = res.body!.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const {value, done} = await reader.read();
    if (done) break;
    buffer += value;
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, end); buffer = buffer.slice(end + 2);
      let event = "message", data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) on[event]?.(JSON.parse(data));        // conversation | step | token | done | error
    }
  }
}
```

**A request can be refused before the stream starts.** Then the status is not 200 and the body is plain JSON
(no stream), see 5.5. Check `res.ok` first.

Events, in order:

1. `conversation`: `{"id": "<uuid>", "new": true}`. The chat's id (new or the one you sent). Remember it and
   send it with the next question.
2. `step` (several): `{"step": "rewrite" | "search" | "rerank" | "answer", "state": "start" | "done", …}`.
   Show a status line for the step that last started: `rewrite` «أفهم سؤالك…», `search` «أبحث في الكتب…»,
   `rerank` «أختار أنسب المقاطع…», `answer` «أكتب الإجابة…». Extra fields: on `rewrite` done:
   `queries`, `keywords`, `authors`, `works` (when the question named an author or book the search is
   limited to, you may show «أبحث في كتب: الشيخ المفيد»); on `search` done: `candidates`, `byWords`; on
   `rerank` done: `chosen`; on `answer` start: `passages`. A step can be missing (never rely on all of them)
   and details may be missing.
3. `token` (many): `{"text": "…"}`. A piece of the answer: append it to the answer view (Markdown) as it
   arrives. The first one comes after the model's thinking, a few seconds into the `answer` step. Gemini
   may send the whole answer as one piece; that is normal. If pieces are big, animate them in smoothly.
4. `done`: the complete answer. Replace the typed text with its `answer` (they are the same text, trimmed),
   then render the sources and update the quota. Body:

```json
{"id": 123, "status": "ok", "answer": "…[1]…", "model": "gemini-3.6-flash",
 "sources": [{"n": 1, "bookId": "4407", "title": "…", "volume": "1", "page": "48", "sequence": 52,
              "text": "the passage", "score": 0.71, "foundBy": "meaning"}],
 "usage": {"promptTokens": 2264, "outputTokens": 187, "thoughtsTokens": 889, "totalTokens": 3340},
 "searchQueries": ["…"], "timings": {"totalMs": 18015},
 "quota": {"limit": 10, "remaining": 5, "resetAt": "2026-10-25T00:00:00+00:00"},
 "conversationId": "6f1c…", "messageId": 456}
```

   `status` is `"ok"`, or `"no_results"` (nothing relevant found: `answer` is null, `sources` empty: show
   "لم أجد ما يخص سؤالك في الكتب"). `sources[n]` is cited in the text as `[n]`; link each `[n]` in the answer
   to that source. `foundBy` is `meaning` or `words` (informational). Do not show `usage`/`timings` to users.
5. `error`: `{"detail": "<Arabic text to show>", "id": 123}`. The stream failed (the model provider is busy,
   or something broke). Discard any partial text. `detail` "الخدمة مشغولة الآن، حاول بعد دقيقة" means the
   provider is busy (a retry button; it does not use the user's quota); otherwise a generic "try again".

If the connection ends with neither `done` nor `error`, treat it as a failure (generic message, retry).
Typical total time is 10 to 60 seconds, so set no short timeout on the request. Add a "stop" button with
`AbortController`: the answer is still generated, stored in the chat and counted against the quota
(refresh the chat from `GET /chat/conversations/{id}` later).

While a question is running, **disable the send button** (the server allows one question at a time per
user, and answers `429 in_progress` otherwise). Also across tabs of the same user.

### 5.4 After an answer

- Update "N questions left" from `quota`; if `remaining` is 0, disable the input and show when it resets
  («يتجدد حدّك الساعة …», `resetAt` in local time).
- A chat is created by its first successful answer. If the first question fails, nothing is stored: ask
  again without a `conversationId`.
- "New chat" = stop sending the id. Opening a chat from the list = `GET /chat/conversations/{id}`, show its
  messages (assistant messages carry their own `sources`, with `n`, `text`, and so on), and send the next
  question with that id.
- Sources open the book page: `GET /books/{bookId}/pages/{sequence}` (the reader), scrolling to or
  highlighting the passage text if you can find it in the page.

### 5.5 Refusals and errors (JSON, with the status code)

| Status | `code` | When | What to show |
|---|---|---|---|
| 401 | `token_expired` | no/expired token | refresh once and retry; else sign in |
| 404 | `conversation_not_found` | unknown, deleted, or not the user's chat | drop the id, offer a new chat |
| 409 | `conversation_full` | the chat has 40 messages | «المحادثة طويلة، ابدأ محادثة جديدة» + a new-chat button |
| 409 | `too_many_conversations` | 100 chats | ask the user to delete some |
| 422 | `too_long` (or empty message) | over 500 characters | the counter should have stopped it |
| 429 | `daily_limit` | the daily quota (10 questions) is used | `detail`; disable the input until `resetAt` (local time) |
| 429 | `too_fast` | more than 3 questions in a minute | countdown of `Retry-After`, then re-enable |
| 429 | `in_progress` | a previous question is still running | wait `Retry-After` (15 s) and call `GET /chat/quota` |
| 429 | `service_limit` | the whole service reached its daily cap | `detail`; "try again tomorrow" (not the user's fault, not their quota) |
| 503 | none | chat switched off on this server | `detail` |

All refusals above come with an Arabic `detail`, and the 429s with `resetAt`/`limit`/`remaining` where it
makes sense. A 429 without a `code` inside an `error` event, or a 502, is a provider problem: it does not
use the user's quota; offer a retry.

**How limits are counted:** per signed-in user, across all their devices (so the phone and the website share
one daily quota). A day runs from 00:00 UTC. Each question that gets an answer (or "no results") counts as
one; failed questions do not count against the daily quota. The numbers (10 a day, 3 a minute, 500
characters, 40 messages a chat, 100 chats) can change on the server: always read them from `GET /chat/quota`
(`limit`, `maxChars`) and from the responses, never hard-code them in the UI.

### 5.6 Presentation notes

- Render the answer as Markdown with a sanitizer (no raw HTML). The text is Arabic: set `dir="auto"` on each
  message and keep citations like `[1]` readable inside RTL text.
- The sources list shows, for each cited `n`: the book title, volume and page, and the passage text.
- Put a short note near the input that the assistant answers only from the library's books and is not a
  source of fatwas.

---

## 6. Test checklist (what to verify before release)

Sign-in: wrong code, expired code, 5 wrong tries, resend countdown, 429 countdown; refresh in two tabs at
once does not sign the user out; signing out one device from another; account deletion.
Sync: edit a note offline on the website and on the phone, then reconnect both: the newer text wins and the
older text appears as a copy; reorder on one device and edit on the other: both survive; delete on one,
the other loses it; a highlight made on the phone lands on the same words on the website (UTF-16 offsets,
diacritics); first sign-in merges the browser's existing data without losing any.
Sessions: create a session on the website, add books, create notes and highlights inside it; they appear in
the same session on the phone, and not in the main library; deleting a session removes its notes and marks
on both.
Meaning search: with and without filters, `limit=50`, an empty result, a 429.
Chat: first question, follow-up in the same chat, a second chat, quota counter going down, the daily limit
screen, the 40-message chat, stopping an answer midway, a failed answer, opening an old chat, deleting a chat.
