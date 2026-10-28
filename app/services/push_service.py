"""Push notifications to the iPhone app (Apple's APNs).

Three kinds, each a switch of the user's (PUSH defaults in app/models/push.py):
  chat_answer   "your answer is ready": sent when a streamed chat answer is stored, so a user who
                left the app finds it (the app shows nothing while it is in the foreground);
  book_updates  a book's content was replaced: only users who have that book in their data;
  new_books     a daily digest of books added since the user last heard (off until switched on).
Prayer-time reminders are local notifications on the phone, not sent from here.

Everything goes through the `push_outbox` table: callers enqueue, a background worker sends
(`run_once`), retries with a back-off and drops tokens Apple says are dead. With no APNs key
set, queued notifications are marked skipped. Nothing here ever logs a token, a question or an
answer; a notification carries no private text.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import time
import uuid
import zoneinfo
from dataclasses import dataclass, field

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from sqlalchemy import delete, exists, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models.account import AuthSession
from app.models.push import (
    DEFAULTS,
    KINDS,
    PushDevice,
    PushOutbox,
    PushPreference,
    PushUserBook,
)

log = logging.getLogger(__name__)

HOSTS = {
    "production": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}
LANGUAGES = ("ar", "fa", "en")
MAX_ATTEMPTS = 4
BACKOFF = (60, 300, 1800)  # seconds before the 2nd, 3rd and 4th try
BATCH = 100
QUIET = (22, 8)  # no non-chat push from 22:00 to 08:00 on the user's own clock
DAILY_CAP = 3  # non-chat pushes a day per user
MAX_BOOK_IDS = 20000
PERMISSIONS = ("notDetermined", "denied", "authorized", "provisional")
IN_FLIGHT = 20
KEEP_DAYS = 7  # sent and skipped rows of the queue

# ── what a notification says ────────────────────────────────────────────────────────

TITLE = {"ar": "مكتبة الثقلين", "fa": "کتابخانه ثقلین", "en": "Thaqalayn Library"}
TEXTS = {
    "chat_answer": {
        "ar": "تم تجهيز إجابتك",
        "fa": "پاسخ شما آماده است",
        "en": "Your answer is ready",
    },
    "book_one": {
        "ar": "تم تحديث كتاب «{title}»",
        "fa": "کتاب «{title}» به‌روزرسانی شد",
        "en": 'The book "{title}" was updated',
    },
    "book_many": {
        "ar": "تم تحديث {n} من كتبك",
        "fa": "{n} کتاب از کتاب‌های شما به‌روزرسانی شد",
        "en": "{n} of your books were updated",
    },
    "new_one": {
        "ar": "أُضيف كتاب جديد: «{title}»",
        "fa": "کتاب جدید: «{title}»",
        "en": 'A new book: "{title}"',
    },
    "new_many": {
        "ar": "أُضيفت {n} كتب جديدة إلى المكتبة",
        "fa": "{n} کتاب جدید به کتابخانه اضافه شد",
        "en": "{n} new books were added to the library",
    },
}


def text_for(key: str, language: str, **values) -> str:
    return TEXTS[key][language if language in LANGUAGES else "ar"].format(**values)


# ── APNs ────────────────────────────────────────────────────────────────────────────


@dataclass
class Notification:
    title: str
    body: str
    data: dict = field(default_factory=dict)
    collapse_id: str | None = None
    kind: str = ""


@dataclass
class Outcome:
    status: str  # ok | dead | retry | failed
    reason: str = ""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class APNs:
    """Apple's HTTP/2 provider API with token-based authentication (a .p8 key)."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.key_id = settings.apns_key_id.strip()
        self.team_id = settings.apns_team_id.strip()
        self.topic = settings.apns_bundle_id.strip()
        self.pem = settings.apns_key.replace("\\n", "\n").strip()
        self._client = client
        self._jwt: tuple[float, str] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.key_id and self.team_id and self.topic and self.pem)

    def jwt(self, now: float | None = None) -> str:
        """The provider token: ES256 over {iss: team, iat}, reused for 40 minutes (Apple wants
        it renewed within the hour, and not more often than every 20 minutes)."""
        now = time.time() if now is None else now
        if self._jwt and now - self._jwt[0] < 40 * 60:
            return self._jwt[1]
        header = _b64(json.dumps({"alg": "ES256", "kid": self.key_id}).encode())
        claims = _b64(json.dumps({"iss": self.team_id, "iat": int(now)}).encode())
        key = serialization.load_pem_private_key(self.pem.encode(), password=None)
        der = key.sign(f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        token = f"{header}.{claims}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        self._jwt = (now, token)
        return token

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(http2=True, timeout=15)
        return self._client

    @staticmethod
    def payload(note: Notification) -> dict:
        passive = note.kind in ("book_updates", "new_books")
        aps = {
            "alert": {"title": note.title, "body": note.body},
            "sound": "default",
            "interruption-level": "passive" if passive else "active",
        }
        if note.collapse_id:
            aps["thread-id"] = note.collapse_id
        return {"aps": aps, **note.data}

    async def send(self, device: PushDevice, note: Notification) -> Outcome:
        url = f"{HOSTS.get(device.environment, HOSTS['production'])}/3/device/{device.token}"
        headers = {
            "authorization": f"bearer {self.jwt()}",
            "apns-topic": self.topic,
            "apns-push-type": "alert",
            "apns-priority": "5" if note.kind in ("book_updates", "new_books") else "10",
            "apns-expiration": str(int(time.time()) + 24 * 3600),
        }
        if note.collapse_id:
            headers["apns-collapse-id"] = note.collapse_id[:64]
        try:
            r = await self.client().post(
                url, headers=headers, content=json.dumps(self.payload(note))
            )
        except httpx.HTTPError as exc:
            return Outcome("retry", type(exc).__name__)
        if r.status_code == 200:
            return Outcome("ok")
        try:
            reason = r.json().get("reason", "")
        except ValueError:
            reason = ""
        if r.status_code == 410 or reason in (
            "BadDeviceToken",
            "Unregistered",
            "DeviceTokenNotForTopic",
        ):
            return Outcome("dead", reason or str(r.status_code))
        if r.status_code == 403 and reason == "ExpiredProviderToken":
            self._jwt = None
            return Outcome("retry", reason)
        if r.status_code == 429 or r.status_code >= 500:
            return Outcome("retry", reason or str(r.status_code))
        return Outcome("failed", reason or str(r.status_code))


# ── devices and preferences ─────────────────────────────────────────────────────────


def valid_timezone(name: str) -> str:
    try:
        zoneinfo.ZoneInfo(name)
        return name
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return "UTC"


async def register_device(
    session: AsyncSession,
    user_id: uuid.UUID,
    device_id: str,
    token: str,
    *,
    environment: str = "production",
    language: str = "ar",
    timezone: str = "UTC",
    app_version: str | None = None,
    platform: str = "ios",
    permission: str = "authorized",
) -> None:
    """This login's device can now be pushed at `token`. A token seen under another account
    (the phone changed hands) moves to this one; a login's older token is replaced."""
    await session.execute(
        delete(PushDevice).where(
            PushDevice.platform == platform,
            PushDevice.token == token,
            (PushDevice.user_id != user_id) | (PushDevice.device_id != device_id),
        )
    )
    now = dt.datetime.now(dt.UTC)
    values = {
        "token": token,
        "environment": environment,
        "language": language if language in LANGUAGES else "ar",
        "timezone": valid_timezone(timezone),
        "app_version": app_version,
        "permission": permission if permission in PERMISSIONS else "authorized",
        "updated_at": now,
        "disabled_at": None,
    }
    await session.execute(
        insert(PushDevice)
        .values(user_id=user_id, device_id=device_id, platform=platform, **values)
        .on_conflict_do_update(constraint="uq_push_devices_login", set_=values)
    )
    await session.commit()


async def remove_device(
    session: AsyncSession, user_id: uuid.UUID, device_id: str, commit: bool = True
) -> None:
    await session.execute(
        delete(PushDevice).where(PushDevice.user_id == user_id, PushDevice.device_id == device_id)
    )
    if commit:
        await session.commit()


async def preferences(session: AsyncSession, user_id: uuid.UUID) -> dict[str, bool]:
    rows = await session.execute(
        select(PushPreference.kind, PushPreference.enabled).where(PushPreference.user_id == user_id)
    )
    return {**DEFAULTS, **dict(rows.all())}


async def set_preferences(
    session: AsyncSession, user_id: uuid.UUID, changes: dict[str, bool]
) -> dict[str, bool]:
    for kind, enabled in changes.items():
        if kind not in KINDS:
            continue
        now = dt.datetime.now(dt.UTC)
        await session.execute(
            insert(PushPreference)
            .values(user_id=user_id, kind=kind, enabled=enabled, updated_at=now)
            .on_conflict_do_update(
                index_elements=["user_id", "kind"], set_={"enabled": enabled, "updated_at": now}
            )
        )
    await session.commit()
    return await preferences(session, user_id)


def _eligible(user_id: uuid.UUID):
    """The conditions for a device that may be pushed to: not disabled, notifications not denied
    on the phone, and its login still alive (signed in, not revoked or expired)."""
    return (
        PushDevice.user_id == user_id,
        PushDevice.disabled_at.is_(None),
        PushDevice.permission != "denied",
        exists().where(
            AuthSession.user_id == PushDevice.user_id,
            AuthSession.device_id == PushDevice.device_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.refresh_expires_at > func.now(),
        ),
    )


async def _devices(session: AsyncSession, user_id: uuid.UUID) -> list[PushDevice]:
    rows = await session.scalars(
        select(PushDevice).where(*_eligible(user_id)).order_by(PushDevice.updated_at.desc())
    )
    return list(rows)


async def _latest_device(session: AsyncSession, user_id: uuid.UUID) -> PushDevice | None:
    devices = await _devices(session, user_id)
    return devices[0] if devices else None


async def set_books(session: AsyncSession, user_id: uuid.UUID, ids: list[int]) -> int:
    """The books the user has downloaded: this list replaces the earlier one."""
    wanted = set(ids)
    await session.execute(
        delete(PushUserBook).where(
            PushUserBook.user_id == user_id, PushUserBook.book_id.notin_(wanted or {-1})
        )
    )
    if wanted:
        await session.execute(
            insert(PushUserBook)
            .values([{"user_id": user_id, "book_id": b} for b in wanted])
            .on_conflict_do_nothing()
        )
    await session.commit()
    return len(wanted)


async def patch_books(
    session: AsyncSession, user_id: uuid.UUID, add: list[int], remove: list[int]
) -> int:
    if remove:
        await session.execute(
            delete(PushUserBook).where(
                PushUserBook.user_id == user_id, PushUserBook.book_id.in_(set(remove))
            )
        )
    if add:
        await session.execute(
            insert(PushUserBook)
            .values([{"user_id": user_id, "book_id": b} for b in set(add)])
            .on_conflict_do_nothing()
        )
    await session.commit()
    return await session.scalar(
        select(func.count()).select_from(PushUserBook).where(PushUserBook.user_id == user_id)
    )


def next_allowed(now: dt.datetime, timezone: str) -> dt.datetime | None:
    """None when `now` is outside the quiet hours of the time zone; else the next 08:00 there."""
    zone = zoneinfo.ZoneInfo(valid_timezone(timezone))
    local = now.astimezone(zone)
    start, end = QUIET
    if not (local.hour >= start or local.hour < end):
        return None
    morning = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if local.hour >= start:
        morning += dt.timedelta(days=1)
    return morning.astimezone(dt.UTC)


# ── the queue ───────────────────────────────────────────────────────────────────────


async def enqueue(
    session: AsyncSession,
    user_id: uuid.UUID,
    kind: str,
    text_key: str,
    *,
    data: dict,
    values: dict | None = None,
    collapse_id: str | None = None,
    dedupe_key: str | None = None,
    delay: float = 0,
    commit: bool = True,
) -> bool:
    """Queues a notification for the user, in the language of their latest device; False when
    they switched the kind off or have no device. Never raises for a queue that is closed."""
    device = await _latest_device(session, user_id)
    if device is None or not (await preferences(session, user_id))[kind]:
        return False
    language = device.language
    await session.execute(
        insert(PushOutbox)
        .values(
            user_id=user_id,
            kind=kind,
            title=TITLE[language],
            body=text_for(text_key, language, **(values or {})),
            data=data,
            collapse_id=collapse_id,
            dedupe_key=dedupe_key,
            send_after=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delay),
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
    )
    if commit:
        await session.commit()
    return True


async def chat_answer_ready(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID, request_id: int
) -> None:
    """The answer of a streamed question is stored: tell the user's devices. (The app shows
    nothing while it is in the foreground; the stream is not checked, a half-open connection
    would make the check unreliable.) Never raises: a failed push must not fail a chat."""
    try:
        await enqueue(
            session,
            user_id,
            "chat_answer",
            "chat_answer",
            data={"type": "chat_answer", "conversationId": str(conversation_id)},
            collapse_id=f"chat-{conversation_id}",
            dedupe_key=f"chat:{request_id}",
            delay=1,
        )
    except Exception:  # noqa: BLE001
        log.exception("could not queue a chat push")


async def run_once(sessionmaker: async_sessionmaker[AsyncSession], sender: APNs) -> int:
    """Sends what is due. Returns how many queue rows were handled."""
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(PushOutbox)
                    .where(PushOutbox.status == "pending", PushOutbox.send_after <= func.now())
                    .order_by(PushOutbox.id)
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return 0
        if not sender.configured:
            for row in rows:
                row.status, row.error = "skipped", "push is not configured"
            await session.commit()
            return len(rows)
        gate = asyncio.Semaphore(IN_FLIGHT)

        async def deliver(row: PushOutbox) -> None:
            devices = await _devices(session, row.user_id)
            if devices and row.kind != "chat_answer":  # chat answers are not held back
                now = dt.datetime.now(dt.UTC)
                later = next_allowed(now, devices[0].timezone)
                if later is None and (
                    await session.scalar(
                        select(func.count())
                        .select_from(PushOutbox)
                        .where(
                            PushOutbox.user_id == row.user_id,
                            PushOutbox.kind != "chat_answer",
                            PushOutbox.status == "sent",
                            PushOutbox.sent_at > now - dt.timedelta(days=1),
                        )
                    )
                    >= DAILY_CAP
                ):
                    later = now + dt.timedelta(hours=6)
                if later is not None:  # quiet hours, or enough for today: try again later
                    row.send_after = later
                    return
            note = Notification(row.title, row.body, row.data or {}, row.collapse_id, row.kind)

            async def one(device: PushDevice) -> Outcome:
                async with gate:
                    return await sender.send(device, note)

            outcomes = await asyncio.gather(*(one(d) for d in devices))
            now = dt.datetime.now(dt.UTC)
            for device, outcome in zip(devices, outcomes, strict=True):
                if outcome.status == "ok":
                    device.last_ok_at = now
                elif outcome.status == "dead":
                    device.disabled_at = now
            statuses = {o.status for o in outcomes}
            row.attempts += 1
            if "ok" in statuses:
                row.status, row.sent_at = "sent", now
            elif not devices or statuses == {"dead"}:
                row.status, row.error = "skipped", "no live device"
            elif "retry" in statuses and row.attempts < MAX_ATTEMPTS:
                row.send_after = now + dt.timedelta(seconds=BACKOFF[min(row.attempts - 1, 2)])
                row.error = next(o.reason for o in outcomes if o.status == "retry")
            else:
                row.status = "failed"
                row.error = next((o.reason for o in outcomes if o.reason), "failed")

        # one session, so the rows are handled one after the other; the sends of a row's
        # devices run together
        for row in rows:
            await deliver(row)
        await session.commit()
        return len(rows)


async def cleanup(session: AsyncSession) -> None:
    await session.execute(
        text(
            "DELETE FROM push_outbox WHERE status <> 'pending'"
            f" AND created_at < now() - interval '{KEEP_DAYS} days'"
        )
    )
    await session.execute(
        text("DELETE FROM push_devices WHERE disabled_at < now() - interval '30 days'")
    )
    await session.execute(
        text(
            "DELETE FROM push_book_versions WHERE book_id <> 0"
            " AND book_id NOT IN (SELECT id FROM books)"
        )
    )
    await session.commit()


# ── books: updates and the new-books digest ─────────────────────────────────────────


async def scan_books(session: AsyncSession) -> dict[str, int]:
    """Compares the published books with the versions already announced: the first run only
    records them (nobody is told about the whole library); later a book not recorded is new
    (kept for the digest) and a higher content_version is an update, told at once to the users
    who have the book in their data."""
    known = await session.scalar(
        text("SELECT EXISTS (SELECT 1 FROM push_book_versions WHERE book_id = 0)")
    )
    if not known:
        await session.execute(
            text(
                "INSERT INTO push_book_versions (book_id, content_version, first_seen_at)"
                " SELECT id, content_version, TIMESTAMPTZ 'epoch' FROM books WHERE is_published"
                " UNION ALL SELECT 0, 0, TIMESTAMPTZ 'epoch'"
            )
        )
        await session.commit()
        return {"seeded": 1, "new": 0, "updated": 0}
    new = (
        await session.execute(
            text(
                "INSERT INTO push_book_versions (book_id, content_version)"
                " SELECT b.id, b.content_version FROM books b"
                " LEFT JOIN push_book_versions v ON v.book_id = b.id"
                " WHERE b.is_published AND v.book_id IS NULL RETURNING book_id"
            )
        )
    ).all()
    updated = (
        await session.execute(
            text(
                "UPDATE push_book_versions v SET content_version = b.content_version"
                " FROM books b WHERE b.id = v.book_id AND b.is_published"
                " AND b.content_version > v.content_version RETURNING v.book_id, b.title"
            )
        )
    ).all()
    per_user: dict[uuid.UUID, list[tuple[int, str]]] = {}
    for book_id, title in updated:
        owners = await session.execute(
            text(
                "SELECT user_id FROM sync_records WHERE NOT deleted AND data->>'bookId' = :b"
                " UNION SELECT user_id FROM push_user_books WHERE book_id = :n"
            ),
            {"b": str(book_id), "n": book_id},
        )
        for (user_id,) in owners.all():
            per_user.setdefault(user_id, []).append((book_id, title))
    for user_id, books in per_user.items():
        if len(books) == 1:
            book_id, title = books[0]
            await enqueue(
                session, user_id, "book_updates", "book_one", values={"title": title},
                data={"type": "book_updated", "bookId": str(book_id)},
                collapse_id="book-updates", commit=False,
            )  # fmt: skip
        else:
            await enqueue(
                session, user_id, "book_updates", "book_many", values={"n": len(books)},
                data={"type": "books_updated"}, collapse_id="book-updates", commit=False,
            )  # fmt: skip
    await session.commit()
    return {"seeded": 0, "new": len(new), "updated": len(updated)}


async def send_digests(
    session: AsyncSession, now: dt.datetime | None = None, hour: int = 10
) -> int:
    """The new-books digest, at most once a day, at `hour` o'clock by the user's own clock
    (their latest device's time zone). Counts the works added since the user last heard, or
    since they switched the digest on."""
    now = now or dt.datetime.now(dt.UTC)
    rows = (
        (
            await session.execute(
                select(PushPreference).where(
                    PushPreference.kind == "new_books", PushPreference.enabled.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )
    sent = 0
    for pref in rows:
        device = await _latest_device(session, pref.user_id)
        if device is None:
            continue
        local = now.astimezone(zoneinfo.ZoneInfo(valid_timezone(device.timezone)))
        if local.hour != hour or (
            pref.last_sent_at and now - pref.last_sent_at < dt.timedelta(hours=20)
        ):
            continue
        since = max(pref.last_sent_at or pref.updated_at, pref.updated_at)
        works = (
            await session.execute(
                text(
                    "SELECT w.id, w.title FROM push_book_versions v"
                    " JOIN books b ON b.id = v.book_id JOIN works w ON w.id = b.work_id"
                    " WHERE v.first_seen_at > :since AND b.is_published GROUP BY w.id, w.title"
                ),
                {"since": since},
            )
        ).all()
        if not works:
            continue
        if len(works) == 1:
            ok = await enqueue(
                session, pref.user_id, "new_books", "new_one", values={"title": works[0][1]},
                data={"type": "new_books"}, collapse_id="new-books", commit=False,
            )  # fmt: skip
        else:
            ok = await enqueue(
                session, pref.user_id, "new_books", "new_many", values={"n": len(works)},
                data={"type": "new_books"}, collapse_id="new-books", commit=False,
            )  # fmt: skip
        if ok:
            pref.last_sent_at = now
            sent += 1
    await session.commit()
    return sent


# ── the background loop ─────────────────────────────────────────────────────────────


async def worker(settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Sends the queue every few seconds; every 10 minutes looks at the books, every 15 at the
    digests, once a day tidies up. Every step may fail without stopping the loop."""
    sender = APNs(settings)
    tick = 0
    while True:
        try:
            await run_once(sessionmaker, sender)
            if tick % 120 == 0:  # 10 minutes of 5 s
                async with sessionmaker() as session:
                    await scan_books(session)
            if tick % 180 == 0:  # 15 minutes
                async with sessionmaker() as session:
                    await send_digests(session, hour=settings.push_digest_hour)
            if tick % 17280 == 0:  # a day
                async with sessionmaker() as session:
                    await cleanup(session)
        except Exception:  # noqa: BLE001 -- retried on the next tick
            log.exception("push worker step failed")
        tick += 1
        await asyncio.sleep(5)
