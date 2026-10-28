"""Push notifications: the APNs request, the queue and its worker, devices and preferences, the
endpoints, the watch on books, and the new-books digest. APNs is faked with httpx's mock
transport; the rest runs on a real database (rows removed afterwards)."""

import base64
import datetime as dt
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api.errors import ApiError
from app.config import Settings
from app.db import get_sessionmaker
from app.main import create_app
from app.models.push import PushDevice
from app.services import auth_service, push_service
from tests.test_api_search import BOOK_ID, imported  # noqa: F401 -- a published book to watch

KEY = ec.generate_private_key(ec.SECP256R1())
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
SETTINGS = Settings(
    apns_key_id="KEY1234567", apns_team_id="TEAM123456", apns_bundle_id="org.test.app", apns_key=PEM
)
TOKEN_A = "a1" * 32
TOKEN_B = "b2" * 32
EMAILS = ("push-a@push.test.invalid", "push-b@push.test.invalid")


# ── APNs ────────────────────────────────────────────────────────────────────────────


def apns(handler) -> push_service.APNs:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return push_service.APNs(SETTINGS, client)


def device(token=TOKEN_A, environment="production") -> PushDevice:
    return PushDevice(user_id=uuid.uuid4(), device_id="d", token=token, environment=environment)


def test_the_provider_token_is_a_valid_es256_jwt_reused_for_forty_minutes():
    sender = push_service.APNs(SETTINGS)
    token = sender.jwt(now=1_000_000)
    header, claims, signature = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    assert json.loads(base64.urlsafe_b64decode(pad(header))) == {
        "alg": "ES256",
        "kid": "KEY1234567",
    }
    assert json.loads(base64.urlsafe_b64decode(pad(claims))) == {
        "iss": "TEAM123456",
        "iat": 1_000_000,
    }
    raw = base64.urlsafe_b64decode(pad(signature))
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    KEY.public_key().verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert sender.jwt(now=1_000_000 + 39 * 60) == token  # reused
    assert sender.jwt(now=1_000_000 + 41 * 60) != token  # renewed


def test_not_configured_without_the_four_settings():
    assert not push_service.APNs(Settings()).configured
    assert not push_service.APNs(Settings(apns_key_id="k", apns_team_id="t")).configured
    assert push_service.APNs(SETTINGS).configured


async def test_the_request_has_apples_headers_and_a_payload_without_private_text():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), headers=request.headers, body=json.loads(request.content))
        return httpx.Response(200)

    note = push_service.Notification(
        "مكتبة الثقلين",
        "تم تجهيز إجابتك",
        {"type": "chat_answer", "conversationId": "c1"},
        "chat-c1",
        "chat_answer",
    )
    outcome = await apns(handler).send(device(environment="sandbox"), note)
    assert outcome.status == "ok"
    assert seen["url"] == f"https://api.sandbox.push.apple.com/3/device/{TOKEN_A}"
    h = seen["headers"]
    assert h["authorization"].startswith("bearer ") and h["apns-topic"] == "org.test.app"
    assert h["apns-push-type"] == "alert" and h["apns-priority"] == "10"
    assert h["apns-collapse-id"] == "chat-c1" and int(h["apns-expiration"]) > 0
    assert seen["body"]["aps"] == {
        "alert": {"title": "مكتبة الثقلين", "body": "تم تجهيز إجابتك"},
        "sound": "default",
        "interruption-level": "active",
        "thread-id": "chat-c1",
    }
    assert seen["body"]["type"] == "chat_answer" and seen["body"]["conversationId"] == "c1"


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (410, {"reason": "Unregistered"}, "dead"),
        (400, {"reason": "BadDeviceToken"}, "dead"),
        (400, {"reason": "PayloadEmpty"}, "failed"),
        (403, {"reason": "ExpiredProviderToken"}, "retry"),
        (429, {"reason": "TooManyRequests"}, "retry"),
        (503, {"reason": "ServiceUnavailable"}, "retry"),
    ],
)
async def test_apples_answers_become_outcomes(status, body, expected):
    sender = apns(lambda request: httpx.Response(status, json=body))
    assert (await sender.send(device(), push_service.Notification("t", "b"))).status == expected


async def test_a_network_error_is_a_retry_and_book_updates_are_passive_priority_5():
    def boom(request):
        raise httpx.ConnectError("down")

    assert (await apns(boom).send(device(), push_service.Notification("t", "b"))).status == "retry"
    seen = {}

    def handler(request):
        seen["priority"] = request.headers["apns-priority"]
        seen["level"] = json.loads(request.content)["aps"]["interruption-level"]
        return httpx.Response(200)

    await apns(handler).send(device(), push_service.Notification("t", "b", kind="book_updates"))
    assert seen == {"priority": "5", "level": "passive"}


# ── on a real database ──────────────────────────────────────────────────────────────


async def _clean():
    async with get_sessionmaker()() as s:
        await s.execute(text("DELETE FROM users WHERE email LIKE '%@push.test.invalid'"))
        await s.commit()


async def login(s, user, device_id, *, revoked=False, expired=False):
    """A login on the device (the push goes only to devices whose login is alive)."""
    await s.execute(
        text(
            "INSERT INTO auth_sessions (id, user_id, device_id, access_hash, refresh_hash,"
            " access_expires_at, refresh_expires_at, revoked_at) VALUES (gen_random_uuid(), :u, :d,"
            " :ah, :rh, now() + interval '1 hour',"
            " now() + (CASE WHEN :exp THEN interval '-1 day' ELSE interval '180 days' END),"
            " CASE WHEN :rev THEN now() ELSE NULL END)"
        ),
        {"u": user, "d": device_id, "ah": uuid.uuid4().hex, "rh": uuid.uuid4().hex,
         "exp": expired, "rev": revoked},
    )  # fmt: skip
    await s.commit()


@pytest.fixture
async def users():
    await _clean()
    ids = []
    async with get_sessionmaker()() as s:
        for email in EMAILS:
            ids.append(
                await s.scalar(
                    text(
                        "INSERT INTO users (id, email) VALUES (gen_random_uuid(), :e) RETURNING id"
                    ),
                    {"e": email},
                )
            )
        await s.commit()
    yield ids
    await _clean()


async def test_a_login_has_one_token_and_a_token_moves_with_the_phone(users):
    a, b = users
    async with get_sessionmaker()() as s:
        await push_service.register_device(
            s, a, "dev-1", TOKEN_A, language="fa", timezone="Asia/Beirut"
        )
        await push_service.register_device(
            s, a, "dev-1", TOKEN_B, environment="sandbox"
        )  # refreshed
        rows = (
            await s.execute(
                text("SELECT token, environment, language FROM push_devices WHERE user_id = :u"),
                {"u": a},
            )
        ).all()
        assert [tuple(r) for r in rows] == [(TOKEN_B, "sandbox", "ar")]
        await push_service.register_device(s, b, "dev-9", TOKEN_B)  # the phone changed hands
        owners = (
            (
                await s.execute(
                    text("SELECT user_id FROM push_devices WHERE token = :t"), {"t": TOKEN_B}
                )
            )
            .scalars()
            .all()
        )
        assert owners == [b]
        await push_service.register_device(s, b, "dev-9", TOKEN_B, timezone="Not/AZone")
        assert (
            await s.scalar(text("SELECT timezone FROM push_devices WHERE user_id = :u"), {"u": b})
            == "UTC"
        )
        await push_service.remove_device(s, b, "dev-9")
        assert (
            await s.scalar(text("SELECT count(*) FROM push_devices WHERE user_id = :u"), {"u": b})
            == 0
        )


async def test_preferences_start_with_the_defaults_and_change_one_at_a_time(users):
    a, _ = users
    async with get_sessionmaker()() as s:
        assert await push_service.preferences(s, a) == {
            "chat_answer": True,
            "book_updates": True,
            "new_books": False,
        }
        out = await push_service.set_preferences(s, a, {"new_books": True, "chat_answer": False})
        assert out == {"chat_answer": False, "book_updates": True, "new_books": True}
        assert (await push_service.set_preferences(s, a, {"bogus": True}))["new_books"] is True


async def test_enqueue_needs_a_device_and_the_kind_switched_on_and_speaks_the_devices_language(
    users,
):
    a, _ = users
    async with get_sessionmaker()() as s:
        assert not await push_service.enqueue(
            s, a, "chat_answer", "chat_answer", data={}
        )  # no device
        await push_service.register_device(s, a, "dev-1", TOKEN_A, language="en")
        assert not await push_service.enqueue(
            s, a, "chat_answer", "chat_answer", data={}
        )  # no login
        await login(s, a, "dev-1")
        assert await push_service.enqueue(
            s, a, "chat_answer", "chat_answer", data={"type": "chat_answer"}, dedupe_key="k1"
        )
        assert await push_service.enqueue(
            s, a, "chat_answer", "chat_answer", data={}, dedupe_key="k1"
        )  # same key: one row
        row = (
            await s.execute(
                text("SELECT title, body, status FROM push_outbox WHERE user_id = :u"), {"u": a}
            )
        ).all()
        assert [tuple(r) for r in row] == [("Thaqalayn Library", "Your answer is ready", "pending")]
        await push_service.set_preferences(s, a, {"chat_answer": False})
        assert not await push_service.enqueue(
            s, a, "chat_answer", "chat_answer", data={}, dedupe_key="k2"
        )


class FakeSender:
    configured = True

    def __init__(self, *outcomes):
        self.outcomes, self.sent = list(outcomes), []

    async def send(self, device, note):
        self.sent.append((device.token, note.body))
        return push_service.Outcome(*self.outcomes.pop(0))


async def _queue(user, key):
    async with get_sessionmaker()() as s:
        await push_service.enqueue(s, user, "chat_answer", "chat_answer", data={}, dedupe_key=key)
        await s.execute(
            text(
                "UPDATE push_outbox SET send_after = now() - interval '1 second' WHERE dedupe_key = :k"  # noqa: E501
            ),
            {"k": key},
        )
        await s.commit()


async def _row(key):
    async with get_sessionmaker()() as s:
        return (
            await s.execute(
                text(
                    "SELECT status, attempts, error, send_after > now() FROM push_outbox WHERE dedupe_key = :k"  # noqa: E501
                ),
                {"k": key},
            )
        ).one()


async def test_the_worker_sends_retries_with_a_backoff_and_drops_dead_tokens(users):
    a, _ = users
    async with get_sessionmaker()() as s:
        await push_service.register_device(s, a, "dev-1", TOKEN_A, language="ar")
        await login(s, a, "dev-1")
    sm = get_sessionmaker()
    await _queue(a, "ok")
    ok = FakeSender(("ok", ""))
    assert await push_service.run_once(sm, ok) == 1
    assert ok.sent == [(TOKEN_A, "تم تجهيز إجابتك")]
    assert tuple(await _row("ok"))[:2] == ("sent", 1)
    assert await push_service.run_once(sm, ok) == 0  # nothing left

    await _queue(a, "busy")
    assert await push_service.run_once(sm, FakeSender(("retry", "429"))) == 1
    status, attempts, error, later = await _row("busy")
    assert (status, attempts, error, later) == (
        "pending",
        1,
        "429",
        True,
    )  # tried again in a minute

    await _queue(a, "dead")
    await push_service.run_once(sm, FakeSender(("dead", "Unregistered")))
    assert (await _row("dead"))[0] == "skipped"
    async with sm() as s:
        assert await s.scalar(
            text("SELECT disabled_at IS NOT NULL FROM push_devices WHERE user_id = :u"), {"u": a}
        )
    async with sm() as s:  # no live device now: nothing is queued
        assert not await push_service.enqueue(
            s, a, "chat_answer", "chat_answer", data={}, dedupe_key="after"
        )


async def test_without_an_apns_key_the_queue_is_marked_skipped(users):
    a, _ = users
    async with get_sessionmaker()() as s:
        await push_service.register_device(s, a, "dev-1", TOKEN_A)
        await login(s, a, "dev-1")
    await _queue(a, "nokey")
    await push_service.run_once(get_sessionmaker(), push_service.APNs(Settings()))
    assert (await _row("nokey"))[:3] == ("skipped", 0, "push is not configured")


async def test_the_endpoints(users, monkeypatch):
    a, _ = users

    async def caller(session, authorization):
        if not authorization:
            raise ApiError(401, "token_expired")
        return SimpleNamespace(user=SimpleNamespace(id=a), login=SimpleNamespace(device_id="dev-7"))

    monkeypatch.setattr(auth_service, "caller", caller)
    headers = {"Authorization": "Bearer x"}
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        assert (await c.put("/api/push/device", json={"token": TOKEN_A})).status_code == 401
        assert (
            await c.put("/api/push/device", json={"token": "nothex!"}, headers=headers)
        ).status_code == 422
        r = await c.put(
            "/api/push/device",
            json={
                "token": TOKEN_A.upper(),
                "environment": "sandbox",
                "language": "fa",
                "timezone": "Asia/Beirut",
            },
            headers=headers,
        )
        assert r.status_code == 200 and r.json()["registered"] is True
        assert r.json()["preferences"] == {
            "chatAnswer": True,
            "bookUpdates": True,
            "newBooks": False,
        }
        async with get_sessionmaker()() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT device_id, token, environment, language FROM push_devices WHERE user_id = :u"  # noqa: E501
                    ),
                    {"u": a},
                )
            ).one()
        assert tuple(row) == (
            "dev-7",
            TOKEN_A,
            "sandbox",
            "fa",
        )  # the login's device; the token lower-cased
        r = await c.put("/api/push/preferences", json={"newBooks": True}, headers=headers)
        assert r.json() == {"chatAnswer": True, "bookUpdates": True, "newBooks": True}
        assert (await c.get("/api/push/preferences", headers=headers)).json()["newBooks"] is True
        assert (await c.delete("/api/push/device", headers=headers)).status_code == 204
        async with get_sessionmaker()() as s:
            assert (
                await s.scalar(
                    text("SELECT count(*) FROM push_devices WHERE user_id = :u"), {"u": a}
                )
                == 0
            )


async def test_signing_out_and_deleting_the_account_remove_the_device(users):
    a, b = users
    async with get_sessionmaker()() as s:
        await push_service.register_device(s, a, "dev-1", TOKEN_A)
        await push_service.register_device(s, b, "dev-2", TOKEN_B)
        await push_service.enqueue(s, b, "chat_answer", "chat_answer", data={}, dedupe_key="gone")
        login = SimpleNamespace(device_id="dev-1", revoked_at=None)
        await auth_service.logout(s, SimpleNamespace(user=SimpleNamespace(id=a), login=login))
        assert (
            await s.scalar(text("SELECT count(*) FROM push_devices WHERE user_id = :u"), {"u": a})
            == 0
        )
        await auth_service.delete_account(
            s, SimpleNamespace(user=SimpleNamespace(id=b, email=EMAILS[1]))
        )
        assert (
            await s.scalar(text("SELECT count(*) FROM push_devices WHERE user_id = :u"), {"u": b})
            == 0
        )
        assert (
            await s.scalar(text("SELECT count(*) FROM push_outbox WHERE user_id = :u"), {"u": b})
            == 0
        )


# ── the books ───────────────────────────────────────────────────────────────────────


async def test_books_new_and_updated_the_digest_and_who_is_told(users, imported):  # noqa: F811
    a, b = users
    async with get_sessionmaker()() as s:
        await s.execute(text("TRUNCATE push_book_versions"))
        await s.commit()
        assert (await push_service.scan_books(s))["seeded"] == 1  # the first run tells nobody
        assert (
            await s.scalar(
                text("SELECT count(*) FROM push_book_versions WHERE book_id = :i"),
                {"i": int(BOOK_ID)},
            )
            == 1
        )
        assert await push_service.scan_books(s) == {"seeded": 0, "new": 0, "updated": 0}

        await push_service.register_device(
            s, a, "dev-1", TOKEN_A, language="ar", timezone="Etc/GMT-3"
        )
        await push_service.register_device(s, b, "dev-2", TOKEN_B, language="en")
        await login(s, a, "dev-1")
        await login(s, b, "dev-2")
        # a has the book in their data, b does not; a also asks for the new-books digest
        await s.execute(
            text(
                "INSERT INTO sync_records (user_id, kind, key, key_lower, data, deleted, updated_at, seq, device_id) VALUES (:u, 'readingProgress', :k, :k, CAST(:d AS jsonb), false, now(), nextval('sync_seq'), 'x')"  # noqa: E501
            ),
            {
                "u": a,
                "k": BOOK_ID,
                "d": json.dumps(
                    {"bookId": BOOK_ID, "lastLocationId": "p-1", "progressPercent": 0.1}
                ),
            },
        )
        await push_service.set_preferences(s, a, {"new_books": True})

        await s.execute(
            text("UPDATE books SET content_version = content_version + 1 WHERE id = :i"),
            {"i": int(BOOK_ID)},
        )
        await s.commit()
        assert (await push_service.scan_books(s))["updated"] == 1
        rows = (
            await s.execute(
                text(
                    "SELECT user_id, body, data FROM push_outbox WHERE kind = 'book_updates' ORDER BY id"  # noqa: E501
                )
            )
        ).all()
        assert [(r[0], r[1], r[2]) for r in rows] == [
            (a, "تم تحديث كتاب «كتاب اختبار البحث»", {"type": "book_updated", "bookId": BOOK_ID})
        ]
        assert (await push_service.scan_books(s))["updated"] == 0  # told once

        # a book not announced yet is new, for the digest
        await s.execute(
            text("DELETE FROM push_book_versions WHERE book_id = :i"), {"i": int(BOOK_ID)}
        )
        await s.commit()
        assert (await push_service.scan_books(s))["new"] == 1
        morning = dt.datetime(2026, 10, 28, 7, 0, tzinfo=dt.UTC)  # 10:00 at UTC+3
        assert (
            await push_service.send_digests(s, now=dt.datetime(2026, 10, 28, 5, 0, tzinfo=dt.UTC))
            == 0
        )  # 08:00 there
        assert await push_service.send_digests(s, now=morning) == 1
        assert await push_service.send_digests(s, now=morning) == 0  # once a day
        body = await s.scalar(
            text("SELECT body FROM push_outbox WHERE kind = 'new_books' AND user_id = :u"), {"u": a}
        )
        assert body == "أُضيف كتاب جديد: «كتاب اختبار البحث»"
        assert (
            await s.scalar(
                text(
                    "SELECT count(*) FROM push_outbox WHERE user_id = :u AND kind <> 'book_updates'"
                ),
                {"u": b},
            )
            == 0
        )
        await s.execute(text("DELETE FROM sync_records WHERE user_id = :u"), {"u": a})
        await s.commit()


async def test_a_device_is_not_pushed_when_notifications_are_denied_or_its_login_ended(users):
    a, b = users
    async with get_sessionmaker()() as s:
        await push_service.register_device(s, a, "dev-1", TOKEN_A, permission="denied")
        await login(s, a, "dev-1")
        assert not await push_service.enqueue(s, a, "chat_answer", "chat_answer", data={})
        await push_service.register_device(s, a, "dev-1", TOKEN_A, permission="authorized")
        assert await push_service.enqueue(
            s, a, "chat_answer", "chat_answer", data={}, dedupe_key="p1"
        )
        await push_service.register_device(s, b, "dev-2", TOKEN_B)
        await login(s, b, "dev-2", revoked=True)
        await login(s, b, "dev-2", expired=True)
        assert not await push_service.enqueue(s, b, "chat_answer", "chat_answer", data={})


def test_quiet_hours_end_at_eight_in_the_users_own_time_zone():
    at = lambda h: dt.datetime(2026, 10, 28, h, 30, tzinfo=dt.UTC)  # noqa: E731
    assert push_service.next_allowed(at(10), "Etc/GMT-3") is None  # 13:30 there
    assert push_service.next_allowed(at(20), "Etc/GMT-3") == dt.datetime(
        2026, 10, 29, 5, 0, tzinfo=dt.UTC
    )  # 23:30 -> 08:00
    assert push_service.next_allowed(at(2), "Etc/GMT-3") == dt.datetime(
        2026, 10, 28, 5, 0, tzinfo=dt.UTC
    )  # 05:30 -> 08:00
    assert push_service.next_allowed(at(7), "UTC") == dt.datetime(2026, 10, 28, 8, 0, tzinfo=dt.UTC)
    assert push_service.next_allowed(at(8), "UTC") is None  # 08:30 is allowed
    assert push_service.next_allowed(at(23), "Not/AZone") == dt.datetime(
        2026, 10, 29, 8, 0, tzinfo=dt.UTC
    )


async def test_other_pushes_wait_for_the_morning_and_stop_at_three_a_day_but_chat_answers_do_not(
    users,
):
    a, _ = users
    sm = get_sessionmaker()
    async with sm() as s:
        await push_service.register_device(s, a, "dev-1", TOKEN_A)
        await login(s, a, "dev-1")
        # whatever the hour is, make it quiet by choosing the zone where it is about 03:00
        hour = dt.datetime.now(dt.UTC).hour
        offset = (3 - hour) % 24
        offset = offset - 24 if offset > 12 else offset
        quiet_zone = f"Etc/GMT{'-' if offset >= 0 else '+'}{abs(offset)}"
        await s.execute(
            text("UPDATE push_devices SET timezone = :z WHERE user_id = :u"),
            {"z": quiet_zone, "u": a},
        )
        await push_service.set_preferences(s, a, {"book_updates": True})
        for kind, key in (("book_updates", "quiet-book"), ("chat_answer", "quiet-chat")):
            await push_service.enqueue(s, a, kind, "chat_answer", data={}, dedupe_key=key)
        await s.execute(
            text(
                "UPDATE push_outbox SET send_after = now() - interval '1 second' WHERE user_id = :u"
            ),
            {"u": a},
        )
        await s.commit()
    sender = FakeSender(("ok", ""), ("ok", ""))
    await push_service.run_once(sm, sender)
    assert (await _row("quiet-chat"))[0] == "sent"  # chat answers are never held back
    book = await _row("quiet-book")
    assert (
        book[0] == "pending" and book[3] is True and book[1] == 0
    )  # waits for 08:00, not an attempt
    assert len(sender.sent) == 1

    async with sm() as s:  # daytime now, but three non-chat pushes were already sent today
        await s.execute(
            text("UPDATE push_devices SET timezone = 'UTC' WHERE user_id = :u"), {"u": a}
        )
        for n in range(3):
            await s.execute(
                text(
                    "INSERT INTO push_outbox (user_id, kind, title, body, status, sent_at, dedupe_key) VALUES (:u, 'new_books', 't', 'b', 'sent', now(), :k)"  # noqa: E501
                ),
                {"u": a, "k": f"cap-{n}"},
            )
        await s.execute(
            text(
                "UPDATE push_outbox SET send_after = now() - interval '1 second' WHERE dedupe_key = 'quiet-book'"  # noqa: E501
            )
        )
        await s.commit()
    if (
        8 <= dt.datetime.now(dt.UTC).hour < 22
    ):  # the cap only matters outside the quiet hours of UTC
        sender = FakeSender(("ok", ""))
        await push_service.run_once(sm, sender)
        assert (await _row("quiet-book"))[0] == "pending" and sender.sent == []  # enough for today


async def test_downloaded_books_are_told_about_even_without_synced_data_and_the_books_endpoints(
    users,
    imported,  # noqa: F811
    monkeypatch,
):
    a, b = users

    async def caller(session, authorization):
        return SimpleNamespace(user=SimpleNamespace(id=a), login=SimpleNamespace(device_id="dev-1"))

    monkeypatch.setattr(auth_service, "caller", caller)
    headers = {"Authorization": "Bearer x"}
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        r = await c.put(
            "/api/push/device",
            json={"token": TOKEN_A, "timeZone": "Asia/Beirut", "permission": "provisional"},
            headers=headers,
        )
        assert r.status_code == 200
        async with get_sessionmaker()() as s:
            row = (
                await s.execute(
                    text("SELECT timezone, permission FROM push_devices WHERE user_id = :u"),
                    {"u": a},
                )
            ).one()
        assert tuple(row) == ("Asia/Beirut", "provisional")  # `timeZone` is accepted too
        assert (
            await c.put("/api/push/books", json={"ids": [int(BOOK_ID), 5, 6]}, headers=headers)
        ).json() == {"count": 3}
        assert (
            await c.patch("/api/push/books", json={"add": [7], "remove": [5, 6]}, headers=headers)
        ).json() == {"count": 2}
        assert (
            await c.put("/api/push/books", json={"ids": [int(BOOK_ID)]}, headers=headers)
        ).json() == {"count": 1}
        assert (
            await c.put("/api/push/books", json={"ids": ["x"]}, headers=headers)
        ).status_code == 422
    async with get_sessionmaker()() as s:
        await login(s, a, "dev-1")
        await s.execute(text("TRUNCATE push_book_versions"))
        await s.commit()
        await push_service.scan_books(s)  # seeded
        await s.execute(
            text("UPDATE books SET content_version = content_version + 1 WHERE id = :i"),
            {"i": int(BOOK_ID)},
        )
        await s.commit()
        assert (await push_service.scan_books(s))["updated"] == 1
        body = await s.scalar(
            text("SELECT body FROM push_outbox WHERE user_id = :u AND kind = 'book_updates'"),
            {"u": a},
        )
        assert (
            body == "تم تحديث كتاب «كتاب اختبار البحث»"
        )  # a has no synced record of the book, only the download
