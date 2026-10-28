# Push notifications (iPhone app)

The server sends notifications through Apple's APNs. Three kinds, each a switch the user controls.
Prayer-time reminders are **local notifications on the phone**: the server is not involved.

| Kind (`type` in the payload) | When | Default | Payload keys |
|---|---|---|---|
| `chat_answer` | a streamed chat answer was stored (the user may have left the app) | on | `conversationId` |
| `book_updated` / `books_updated` | a book's content was replaced, and only for users who have that book in their data (reading progress, a session, a bookmark, marks) | on | `bookId` (one book) |
| `new_books` | a daily digest at 10:00 on the user's own clock (the device's time zone), at most once a day, of works added since the user last heard | **off** | none |

Notifications carry no private text: «تم تجهيز إجابتك», «تم تحديث كتاب «…»», «أُضيفت N كتب جديدة إلى المكتبة»
(Persian and English too, by the device's language). The chat push is sent for every answered question
even when the app is open: **the app should show nothing for a `chat_answer` push while it is in the
foreground** (the default of iOS when `willPresent` is not implemented). The same `collapse id`
(`chat-<conversationId>`, `book-updates`, `new-books`) makes a newer banner replace an older one;
`thread-id` is set to it too.

## Endpoints (signed in; `Authorization: Bearer`)

`PUT /api/push/device` — register or refresh this login's device. Call it after sign-in, and again when
the token, the language or the time zone changes:

```json
{"token": "<APNs device token, hex>", "environment": "production" | "sandbox",
 "permission": "notDetermined" | "denied" | "authorized" | "provisional",
 "language": "ar" | "fa" | "en", "timeZone": "Asia/Beirut", "appVersion": "1.4"}
```

→ `{"registered": true, "pushEnabled": true|false, "preferences": {...}}`. `environment` is `sandbox` for builds
run from Xcode and `production` for TestFlight and the App Store. `pushEnabled: false` means the server has no
APNs key yet (nothing is sent). The device is the one the login was made on (the `deviceId` of sign-in);
a login has one token (a new one replaces the old); a token seen under another account moves to it.
`timezone` is accepted as well as `timeZone`. `permission` is what iOS says about notifications for the app
(default `authorized`): with `denied` the server sends nothing to that device. A device is pushed only while
its login is alive (signed in, not revoked, not expired). `DELETE /api/push/device` → 204 stops pushes to this login's device. Signing out, signing a device out and
deleting the account do it too.

`GET /api/push/preferences` → `{"chatAnswer": true, "bookUpdates": true, "newBooks": false}`;
`PUT /api/push/preferences` with any of those booleans changes them (the others stay).

`PUT /api/push/books` `{"ids": [4407, 4408, ...]}` replaces the list of books downloaded on the user's
devices (up to 20,000); `PATCH /api/push/books` `{"add": [...], "remove": [...]}` changes it. Ids are the
catalog's book ids as numbers. A book update is told to the users who have the book in their synced data
(reading progress, a session, a bookmark, marks) **or** in this list. Both answer `{"count": N}`.

**Limits (kinds other than chat answers):** none from 22:00 to 08:00 on the user's own clock (held until
08:00), and at most 3 a day per user (the rest wait). Chat answers are never held back.

## Server settings (Coolify)
`APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID`, `APNS_KEY` (the text of the `.p8` file). With any of them
empty, queued notifications are marked skipped. `PUSH_WORKER=false` switches the background loop off;
`PUSH_DIGEST_HOUR` (default 10) is the local hour of the digest.

## How it works
Notifications are queued in `push_outbox`; a background loop sends them every 5 seconds, retries with a
back-off (1, 5, 30 minutes, 4 tries), and disables a token Apple reports dead (410, `BadDeviceToken`).
Every 10 minutes it compares the published books with the versions it announced (the first run only records
them: nobody is told about the whole library). Tokens are never logged.
