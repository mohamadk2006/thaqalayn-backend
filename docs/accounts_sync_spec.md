# Accounts and sync: spec for the backend

Status: built on the backend (2026-10-22); see §11 for the few additions. Written 2026-10-22
from the iOS app's current data model.

The goal is to let a user sign in with their email and get the same notes, highlights,
numbered marks, bookmarks, reading positions and study sessions on every device (iOS,
Android, and later the website).

Decisions already made:

- **Sign-in is by email with a one-time code.** There are no passwords and no Apple or
  Google sign-in for now.
- **Email is sent through a free service** (§2).
- **Notes: the newer edit wins.** The version with the newer `updatedAt` replaces the other.
  The losing text is kept as a separate copy, so nothing is lost silently (§5.4).
- **Each book in a study session is its own record**, not one list inside the session (§5.3).
- **Search, catalog and download endpoints stay public.** The `/api/chat` endpoint also stays
  public. Only `/api/account*` and `/api/sync*` need a token.

---

## 1. Sign-in flow

```
App                                   Server                         Email service
 │ POST /api/auth/email/start {email} │                                │
 │───────────────────────────────────▶│ make 6-digit code, store hash  │
 │                                    │──────── send code ────────────▶│──▶ user's inbox
 │◀──────── 202 {resendAfter} ────────│                                │
 │  user types the code               │                                │
 │ POST /api/auth/email/verify        │                                │
 │   {email, code, deviceId}          │                                │
 │───────────────────────────────────▶│ check code, create user if new │
 │◀── 200 {accessToken, refreshToken, user, isNewAccount} ─────────────│
```

### 1.1 `POST /api/auth/email/start`

```json
{"email": "user@example.com", "language": "ar"}
```

- `language` is one of `ar`, `fa` or `en` and picks the email's language. The default is `ar`.
- **Normalize the email:** trim it and lowercase it. Do not remove dots or `+tags`. Reject it
  with 422 if it isn't a plausible address (one `@`, a dot in the domain, at most 254 characters).
- **Make the code:** 6 digits from a secure random source (`secrets.randbelow(10**6)`,
  zero-padded).
- **Store only a hash of the code:** HMAC-SHA256 keyed with a server secret (`AUTH_CODE_PEPPER`).
  Store it with its expiry (**10 minutes**) and an attempt counter (0).
- A new code replaces any earlier unused code for the same email, so only the latest code works.
- **Always answer 202 with the same body**, whether or not an account exists, so the endpoint
  can't be used to find out who has an account:
  ```json
  {"resendAfter": 60, "expiresIn": 600}
  ```
- **Rate limits** (answer 429 with `Retry-After`):
  - one code per email every **60 s**;
  - **5 codes per email per hour**;
  - **20 codes per IP per hour**.
- If the email service refuses or is over its quota, answer 503 with
  `{"code": "email_unavailable"}` (see §2.3).

### 1.2 `POST /api/auth/email/verify`

```json
{"email": "user@example.com", "code": "042917",
 "deviceId": "3f1c…", "deviceName": "iPhone 16", "platform": "ios"}
```

- `deviceId` is a random id the app makes once per install (up to 64 characters).
  `deviceName` (up to 64) and `platform` (`ios`, `android` or `web`) are informational.
- **Check the code:**
  1. Look up the latest unused code for the email.
  2. Compare the hashes in constant time (`hmac.compare_digest`).
  3. Each wrong try adds one to the attempt counter. **After 5 wrong tries the code is dead**,
     and the user must request a new one.
  4. A used or expired code never works again.
- **On success:**
  1. Mark the code used.
  2. If no user has this email yet, create one (`isNewAccount: true`).
  3. Create a login session (§1.4) and answer 200:
     ```json
     {
       "accessToken": "…", "accessExpiresIn": 3600,
       "refreshToken": "…",
       "user": {"id": "b7e2…", "email": "user@example.com", "createdAt": "2026-10-22T09:00:00Z"},
       "isNewAccount": false
     }
     ```
- **On failure,** answer 400 with one of these codes:
  - `{"code": "code_invalid", "attemptsLeft": 3}`;
  - `{"code": "code_expired"}`;
  - `{"code": "code_locked"}` (after 5 wrong tries).
- **Rate limit:** 30 verify calls per IP per hour, answering 429 when it's exceeded.

### 1.3 Tokens

- **Both tokens are opaque random strings** of 32 bytes from `secrets.token_urlsafe`. Store only
  their SHA-256 hashes. No JWT: one table lookup per request is fine at this size, and revoking a
  token is just deleting a row.
- **Access token:** lives **1 hour** and is sent as `Authorization: Bearer <token>`.
- **Refresh token:** lives **180 days** and is replaced on every use (rotation). It is never sent
  to anything except `/api/auth/refresh`.
- **Reuse detection:** if an already-used refresh token comes back, it has been copied. Revoke the
  whole login session; the device signs in again.

### 1.4 Other auth endpoints

| Endpoint | Auth | Does |
|---|---|---|
| `POST /api/auth/refresh` `{refreshToken}` | none | Answers with new `accessToken` and `refreshToken` values, or 401 `{"code":"refresh_invalid"}`. |
| `POST /api/auth/logout` | bearer | Revokes this login session. |
| `GET /api/account` | bearer | Returns `{id, email, createdAt, devices:[{deviceId, deviceName, platform, lastSeenAt, current}]}`. |
| `DELETE /api/account/devices/{deviceId}` | bearer | Signs that device out. |
| `DELETE /api/account` | bearer | **Deletes the account and all of its synced data immediately** (Apple requires in-app account deletion). Returns 204. The app asks for confirmation before calling it. |

Any request with a missing or expired access token gets 401 `{"code": "token_expired"}`. The app
then calls refresh once and retries.

### 1.5 App Store review account

Apple's reviewers can't receive our emails. Add one fixed account from settings:

- `REVIEW_EMAIL` (for example `review@thaqalaynlibrary.com`) and `REVIEW_CODE`.
- For that address only: `start` sends no email, and `verify` accepts `REVIEW_CODE`.
- The account behaves like any other in every other way.
- When the settings are empty, the bypass is switched off.

---

## 2. Sending the email: free service

### 2.1 Which service

Use a provider's **free plan**. As of October 2026 (confirm on each provider's pricing page
before signing up):

| Provider | Free plan | Use as |
|---|---|---|
| **Brevo** (transactional API) | about **300 emails/day** | **primary** |
| **Resend** | about **100/day, 3,000/month**, one domain | **fallback** |

- SendGrid ended its free tier in 2025, and Mailgun dropped its free 10,000/month plan. Don't
  pick either for the free route.
- Both providers in the table have a simple HTTPS API, so no SMTP library is needed.
- Use `httpx`, which is already a dev dependency. Move it to the main dependencies.

**What the free cap means:** one sign-in is one email, and most users sign in once per device.
About 300 sign-ins a day is plenty for launch. With the fallback the free ceiling is about 400 a
day. If we outgrow it, moving to a paid plan is a settings change.

### 2.2 Keep the provider behind one small interface

```python
class EmailSender(Protocol):
    async def send_code(self, to: str, code: str, language: str) -> None: ...
```

- Implementations: `BrevoSender`, `ResendSender` and `ConsoleSender`.
- `ConsoleSender` is for development. It writes the code to the server log instead of sending it.
- `send_code` raises `EmailQuotaExceeded` or `EmailSendFailed`.
- Settings:
  - `EMAIL_PROVIDERS="brevo,resend"` lists the providers in the order to try them;
  - `BREVO_API_KEY` and `RESEND_API_KEY` hold the keys;
  - `EMAIL_FROM="مكتبة الثقلين <noreply@thaqalaynlibrary.com>"` is the sender.

### 2.3 Failover

- Try the providers in order. On a quota or 5xx error from one, try the next.
- If all of them fail, answer 503 `{"code": "email_unavailable"}`. Keep the code stored anyway, so
  a later resend from the user works.
- Log every send attempt to an `email_sends` table: provider, ok or error, a hash of the email,
  and the time. Never log the code.

### 2.4 Domain setup (one time, needed by both providers)

Add these DNS records for `thaqalaynlibrary.com`, or a subdomain such as `mail.`, so the codes
don't land in spam:

- the provider's **SPF** include;
- the provider's **DKIM** records;
- a **DMARC** record, starting with `v=DMARC1; p=none; rua=mailto:…`.

Verify the domain in each provider's dashboard before going live.

### 2.5 The email itself

- **Subject:** `رمز الدخول إلى مكتبة الثقلين: 042917`. Putting the code in the subject means it
  shows in the notification.
- **Body:** send both plain text and simple HTML. The HTML is RTL (`dir="rtl"`) and shows the code
  large. Add one line saying it is valid for 10 minutes, and one line saying "if you didn't
  request this, ignore it".
- **No links and no images.** A code-only email looks less like phishing and gets through spam
  filters more easily.
- Prepare Persian and English versions for `language=fa` and `language=en`.

---

## 3. Database tables (Postgres, new Alembic migration)

```
users            id uuid pk, email text unique (normalized), created_at, deleted_at null
email_codes      id, email, code_hash, expires_at, attempts int, used_at null, created_at,
                 ip inet          -- index (email, created_at desc)
auth_sessions    id uuid pk, user_id fk→users on delete cascade, device_id, device_name,
                 platform, refresh_hash unique, access_hash unique, access_expires_at,
                 refresh_expires_at, created_at, last_seen_at, revoked_at null
email_sends      id, provider, email_hash, ok bool, error text null, created_at
sync_records     user_id fk→users on delete cascade, kind text, key text,
                 data jsonb null, deleted bool, updated_at timestamptz,
                 order_changed_at timestamptz null, seq bigint, device_id text,
                 primary key (user_id, kind, key)
                 index (user_id, seq)
sync_seq         a Postgres SEQUENCE: every write to sync_records takes nextval()
```

- Rate limits can count rows in `email_codes` (per email or IP over the last hour), so no Redis
  is needed.
- Delete old `email_codes` rows (older than a day) and old `email_sends` rows (older than
  90 days) in a daily job.

---

## 4. Sync API

The server stores each record as an opaque JSON blob (`data`) under `(kind, key)`. It reads only
a few fields: `updatedAt`, `deleted`, and the note and order fields used in §5. This keeps the
server simple, and new fields can be added by the apps without a server change.

### 4.1 Pull: `GET /api/sync/changes?since=<cursor>&limit=500`

- `since` is the `cursor` from the previous pull, or `0` for everything.
- Returns this user's records with `seq > since`, in `seq` order:

```json
{
  "cursor": 18342,
  "hasMore": false,
  "items": [
    {"kind": "note", "key": "6F1C…", "deleted": false,
     "updatedAt": "2026-10-22T09:12:03.120Z", "orderChangedAt": null,
     "data": { … see §6 … }},
    {"kind": "highlight", "key": "A93D…", "deleted": true,
     "updatedAt": "2026-10-22T09:13:00Z", "data": null}
  ]
}
```

- `limit` is at most 1000. The app keeps pulling while `hasMore` is true.
- Deleted records come back with `deleted: true` and no data; these are the tombstones.
- **Keep tombstones forever.** They are tiny, and dropping them would let an old offline device
  bring deleted things back.

### 4.2 Push: `POST /api/sync/push`

```json
{
  "deviceId": "3f1c…",
  "items": [
    {"kind": "note", "key": "6F1C…", "deleted": false,
     "updatedAt": "2026-10-22T09:12:03.120Z", "orderChangedAt": null,
     "baseSeq": 18200,
     "data": { … }}
  ]
}
```

- At most **500 items and 2 MB per request**. Each item's `data` can be at most **256 KB**.
- `baseSeq` is the `seq` of this record the device last pulled, or `null` if the device has never
  seen it from the server. The server uses it only for the note copy rule (§5.4).
- Apply each item using the rules in §5, all in one transaction per request.
- Answer:

```json
{
  "results": [
    {"kind": "note", "key": "6F1C…", "status": "applied", "seq": 18343},
    {"kind": "bookmark", "key": "-|4407|p-000052-b-001", "status": "older_ignored", "seq": 18001},
    {"kind": "note", "key": "77AB…", "status": "applied_with_copy", "seq": 18344,
     "copyKey": "D0E5…"}
  ]
}
```

- After a push the app pulls, which also brings back its own writes and any copies.
- Unknown `kind`s get the status `rejected_kind`. Data that is too big gets `rejected_size`.
  Other items in the same request still apply.

### 4.3 Order of operations in the app (for reference)

1. On sign-in: pull from `0`, merge, then push every local record. The device's existing data is
   merged into the account; it is never thrown away.
2. Then sync on launch, on returning to the foreground, and a few seconds after local edits
   (batched).
3. On sign-out the app asks whether to keep or remove this device's local copy. The server is not
   involved.

---

## 5. Merge rules (server side)

Each pushed item is compared with the stored record under the same `(user, kind, key)`.

### 5.1 Clock safety

- `updatedAt` comes from the device clock.
- If it is more than **5 minutes in the future** of server time, clamp it to server time.
- Reject times before 2020 with `rejected_time`.

### 5.2 The default rule: the newer write wins (all kinds)

- No stored record: insert it.
- Incoming `updatedAt` > stored `updatedAt`: replace `data` and `deleted`. Give the record a new
  `seq`. The status is `applied`.
- Otherwise: keep the stored record. The status is `older_ignored`.
- A tie is decided by `deviceId` in string order, so every server makes the same choice.
- **A deletion is just a write with `deleted: true`.** It wins or loses by time like any other
  write. So a newer edit can bring back a record that was deleted earlier, and a newer delete
  removes an edit.

### 5.3 Order is separate from content (notes, folders, sessions, session books)

- The apps keep the user's manual order as `sortIndex` (or `position`), with its own time
  `orderChangedAt`.
- Rearranging must not count as an edit. Merge the two parts separately:
  - content fields come from whichever side has the newer `updatedAt`;
  - `sortIndex` or `position` comes from whichever side has the newer `orderChangedAt`.
- The stored record ends up as the combination, with a new `seq`.

**Study session books** (`kind = sessionBook`) are **one record per book in a session**, not a
list on the session. So adding a book on one phone and removing a different book on another
both survive.

### 5.4 Notes: keep the losing text as a copy

This applies only to `kind = note`, when all three of these are true:

1. a stored record exists, is not deleted, and its `seq` > the item's `baseSeq` (the device didn't
   see the latest version before editing); and
2. the incoming item is not a deletion; and
3. `title` or `bodyText` differ between the two.

Then:

- The newer side wins under §5.2, as usual.
- The losing side's content is saved as **a new note record**:
  - a new UUID `key`, and the same `sessionId` and `folderId`;
  - `title` set to the original title followed by ` (نسخة محفوظة)`;
  - `copyOf` set to the original key;
  - `updatedAt` set to now.
- The status is `applied_with_copy` with `copyKey`. Every device receives the copy on its next
  pull.
- If the incoming item is the one that lost, the copy is still made from its content, and the
  status is `older_ignored_with_copy`.

There is no line-by-line merging for now, by decision. Numbered marks link to note lines by
number (`N. …`), so merging lines from two devices could point marks at the wrong lines.

### 5.5 What the server does not do

- **It does not cascade deletes.** When a session is deleted, the app sends tombstones for its
  books, notes, folders, highlights, marks, bookmarks and progress in the same push.
- **It does not check that a `bookId` exists.** A book can leave the catalog while the user's
  marks on it remain.

---

## 6. Record kinds and their data

- All ids are UUID strings in upper case, as the iOS app writes them. Compare them
  case-insensitively.
- Times are ISO-8601 UTC with milliseconds.
- `bookId` is always the **plain catalog book id**, never the app's local `session:…` key.
- `sessionId` is `null` for the main library, or a session's id. Something made inside a study
  session belongs only to that session.

| `kind` | `key` | `data` fields | `updatedAt` means |
|---|---|---|---|
| `note` | note id | `title`, `bodyText`, `hasCustomTitle`, `sessionId?`, `folderId?`, `sortIndex?`, `createdAt`, `copyOf?` | last text edit |
| `noteFolder` | folder id | `title`, `sessionId?`, `sortIndex`, `createdAt` | last rename |
| `session` | session id | `title`, `sortIndex?`, `createdAt` (**no book list**) | last rename |
| `sessionBook` | `{sessionId}\|{bookId}` | `sessionId`, `bookId`, `position` (Double, sort ascending) | added (later: position only through `orderChangedAt`) |
| `highlight` | highlight id | `bookId`, `sessionId?`, `paragraphId`, `start`, `end`, `color` (`yellow\|green\|blue\|pink\|purple`), `quotedText?`, `contentVersion?`, `createdAt` | creation (never edited in place) |
| `numberedMark` | mark id | `bookId`, `sessionId?`, `paragraphId`, `offset`, `number`, `noteId`, `spans:[{paragraphId,start,end}]`, `quotedText?`, `contentVersion?`, `createdAt` | last renumber |
| `bookmark` | `{sessionId or "-"}\|{bookId}\|{locationId}` | `bookId`, `sessionId?`, `locationId`, `createdAt` | creation |
| `readingProgress` | `{bookId}` | `bookId`, `lastLocationId`, `progressPercent` (0…1), `pageScrollFraction?` | `lastReadAt` |
| `sessionProgress` | `{sessionId}\|{bookId}` | same fields as `readingProgress`, plus `sessionId` | `lastReadAt` |

Notes on the data:

- **Positions:**
  - `paragraphId` and `locationId` look like `p-000052-b-001`; the first number is the page
    sequence.
  - `start`, `end` and `offset` are UTF-16 offsets inside that paragraph's text.
  - Android and the web must count in UTF-16 too: JavaScript strings already do, and so do Java
    and Kotlin strings.
- **One highlight per paragraph piece:** a highlight that crosses paragraphs is several
  `highlight` records, one per paragraph.
- **`quotedText` and `contentVersion`** record the text a highlight or mark covered and the
  catalog `contentVersion` of the book at the time. When a book's text is updated on the server,
  the apps use them to place the mark again. The server only stores them.
- **Not synced:**
  - downloads (device-specific);
  - search history;
  - reader settings;
  - legacy `NoteExcerpt` rows;
  - retired `NoteBookLink` rows.

---

## 7. Error format

Every error has a JSON body with a stable machine `code` and a human `detail`, which the app may
show:

```json
{"code": "code_invalid", "detail": "الرمز غير صحيح.", "attemptsLeft": 3}
```

| HTTP | `code` |
|---|---|
| 400 | `code_invalid`, `code_expired`, `code_locked` |
| 401 | `token_expired`, `refresh_invalid` |
| 413 | `payload_too_large` (push over 500 items or 2 MB) |
| 422 | `email_invalid`, plus FastAPI's own validation errors |
| 429 | `rate_limited`, with a `Retry-After` header |
| 503 | `email_unavailable` |

---

## 8. Security checklist

- HTTPS only. Send HSTS on the API domain.
- Store codes and tokens only as hashes. Compare them in constant time.
- Never log codes, tokens or raw emails. Log a hash of the email instead.
- `AUTH_CODE_PEPPER` and the provider API keys live in the environment, never in the repo.
- Every sync query filters by `user_id` from the token. Never take a user id from the request
  body.
- `DELETE /api/account` removes `users`, `auth_sessions` and `sync_records` rows for that user in
  one transaction.

---

## 9. Tests to write

- **Start:**
  - an unknown email and a known email get identical 202 answers;
  - the 60 s cooldown, the 5-per-hour limit and the IP limit each return 429 with
    `Retry-After`.
- **Verify:**
  - a right code works once only;
  - an expired code fails;
  - a 6th wrong try locks the code;
  - a newer code replaces an older one.
- **Tokens:**
  - refresh rotates the tokens;
  - reusing an old refresh token revokes the whole login session.
- **Review account:** `REVIEW_CODE` works only for `REVIEW_EMAIL`, and not when the setting is
  empty.
- **Email failover:**
  - a Brevo quota error falls through to Resend;
  - when both fail, the answer is 503 and a resend works later.
- **Push merge rules:**
  - an older write is ignored;
  - a newer write is applied;
  - a tie is decided by `deviceId`;
  - a future time is clamped;
  - a delete beats an older edit, and a newer edit beats an older delete.
- **Order merge:** content edited on A and the order changed on B both survive.
- **Note copy:** an edit from a device with a stale `baseSeq` creates one copy with the
  ` (نسخة محفوظة)` title, and both devices receive it.
- **Pull:** pages by cursor in `seq` order, `hasMore` is correct, and one user never sees another
  user's records.
- **Account deletion** removes everything, and the old tokens stop working.

---

## 10. What the iOS app still needs (so you know what's coming)

The app's local data is already prepared:

- deleted records leave tombstones;
- highlights, marks and bookmarks have stable `syncId`s;
- `updatedAt`, `orderChangedAt`, `plainBookId` and `sessionId` are stored.

Still to do on the app side:

- turn the session book list (`StudySession.bookIds`) into per-book records, with a migration;
- add `sessionBook` to the app's sync kinds;
- the sign-in screens;
- the sync engine itself.

Once the endpoints above exist, nothing in them should need to change for the app.

---

## 11. As built: additions to this spec

Everything above is implemented as written (`app/api/account.py`, `app/api/sync.py`,
`app/services/{auth,sync}_service.py`, `app/services/email_sender.py`; tests in
`tests/test_accounts.py` and `tests/test_sync.py`). The additions:

- **Two more tables.** `verify_calls` counts verify calls for the per-IP limit, and
  `used_refresh_tokens` keeps rotated-away refresh tokens so that reuse can be detected.
- **More error codes:**
  - 503 `auth_unavailable`, when `AUTH_CODE_PEPPER` isn't set;
  - 404 `device_not_found`, from `DELETE /api/account/devices/{id}`;
  - 422 `body_invalid`, for a push body that isn't `{deviceId, items: [...]}`.
- **More push status:** `rejected_key`, for an empty key or one over 512 characters.
- **Keys:** a key is matched case-insensitively. It is returned the way it was first pushed.
- **Signing in again:** signing in again on the same `deviceId` ends that device's older login.
- **Email:** sent with the standard library over HTTPS, not `httpx`, so there is no new
  dependency. With `EMAIL_PROVIDERS=console` the code is written to the server log instead of
  being sent, which is for testing only.
- **Client IP:** taken from the last `X-Forwarded-For` entry, which is the one added by the
  server's proxy.
- **Daily cleanup:** a job in the API process deletes:
  - codes and verify calls older than a day;
  - email send logs older than 90 days;
  - login sessions that ended or expired over 30 days ago.
