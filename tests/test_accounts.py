"""Sign-in by email code, tokens and the account endpoints (docs/accounts_sync_spec.md §1-§2).

Real database; every row made here uses an @test.invalid email or a 203.0.113.x address
and is removed afterwards. Emails go to a fake provider that records the codes.
"""

import datetime as dt
import itertools

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.main import create_app
from app.services import email_sender

_ip = itertools.count(1)


class FakeSender:
    def __init__(self, name="fake", fail=None):
        self.name, self.fail, self.sent = name, fail, []

    async def send_code(self, to, code, language):
        if self.fail:
            raise self.fail(f"{self.name} refused")
        self.sent.append((to, code, language))


@pytest.fixture
def mail(monkeypatch):
    sender = FakeSender()
    monkeypatch.setattr(email_sender, "senders", lambda settings: [sender])
    return sender


async def _clean():
    async with get_sessionmaker()() as s:
        await s.execute(text("DELETE FROM users WHERE email LIKE '%@test.invalid'"))
        await s.execute(text("DELETE FROM email_codes WHERE email LIKE '%@test.invalid'"))
        await s.execute(text("DELETE FROM verify_calls WHERE ip << '203.0.113.0/24'"))
        await s.execute(text("DELETE FROM email_sends WHERE provider LIKE 'fake%'"))
        await s.commit()


@pytest.fixture
async def client():
    await _clean()
    app = create_app()
    settings = Settings(
        auth_code_pepper="test-pepper", review_email="review@test.invalid", review_code="424242"
    )
    app.dependency_overrides[get_settings] = lambda: settings
    ip = f"203.0.113.{next(_ip) % 250 + 1}"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Forwarded-For": ip}
    ) as c:
        yield c
    await _clean()


async def start(c, email, **kw):
    return await c.post("/api/auth/email/start", json={"email": email, **kw})


async def verify(c, email, code, device="dev-A", **kw):
    return await c.post(
        "/api/auth/email/verify", json={"email": email, "code": code, "deviceId": device, **kw}
    )


async def sign_in(c, mail, email="a@test.invalid", device="dev-A"):
    assert (await start(c, email)).status_code == 202
    r = await verify(c, email, mail.sent[-1][1], device)
    assert r.status_code == 200, r.text
    return r.json()


async def age_codes(email, seconds):
    async with get_sessionmaker()() as s:
        await s.execute(
            text(
                "UPDATE email_codes SET created_at = created_at - make_interval(secs => :s) "
                "WHERE email = :e"
            ),
            {"s": seconds, "e": email},
        )
        await s.commit()


# ── start ───────────────────────────────────────────────────────────────────────────


async def test_known_and_unknown_emails_get_the_same_answer(client, mail):
    await sign_in(client, mail, "known@test.invalid")
    await age_codes("known@test.invalid", 61)
    a = await start(client, "known@test.invalid")
    b = await start(client, "nobody@test.invalid")
    assert a.status_code == b.status_code == 202
    assert a.json() == b.json() == {"resendAfter": 60, "expiresIn": 600}


async def test_email_is_normalized_and_bad_ones_refused(client, mail):
    assert (await start(client, "  Mixed.Case+tag@Test.Invalid ")).status_code == 202
    assert mail.sent[-1][0] == "mixed.case+tag@test.invalid"
    for bad in (
        "no-at.test.invalid",
        "a@nodot",
        "two@@test.invalid",
        "a@" + "x" * 260 + ".invalid",
    ):
        r = await start(client, bad)
        assert r.status_code == 422 and r.json()["code"] == "email_invalid", bad


async def test_code_email_language_and_content():
    subject, plain, html = email_sender.compose("042917", "ar")
    assert subject == "رمز الدخول إلى مكتبة الثقلين: 042917"
    assert "042917" in plain and 'dir="rtl"' in html and "href" not in html and "<img" not in html
    assert email_sender.compose("1", "fa")[0].startswith("کد ورود")
    assert email_sender.compose("1", "en")[0].startswith("Your Thaqalayn")


async def test_one_code_a_minute(client, mail):
    assert (await start(client, "a@test.invalid")).status_code == 202
    r = await start(client, "a@test.invalid")
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    assert 1 <= int(r.headers["retry-after"]) <= 61


async def test_five_codes_an_hour_per_email(client, mail):
    for _ in range(5):
        assert (await start(client, "a@test.invalid")).status_code == 202
        await age_codes("a@test.invalid", 61)
    r = await start(client, "a@test.invalid")
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 60


async def test_twenty_codes_an_hour_per_ip(client, mail):
    for n in range(20):
        assert (await start(client, f"u{n}@test.invalid")).status_code == 202
    r = await start(client, "u20@test.invalid")
    assert r.status_code == 429 and "retry-after" in r.headers


async def test_no_pepper_means_sign_in_is_off(mail):
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(auth_code_pepper="")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await start(c, "a@test.invalid")
    assert r.status_code == 503 and r.json()["code"] == "auth_unavailable"


# ── verify ──────────────────────────────────────────────────────────────────────────


async def test_a_right_code_works_once(client, mail):
    await start(client, "a@test.invalid")
    code = mail.sent[-1][1]
    assert len(code) == 6 and code.isdigit()
    first = await verify(client, "a@test.invalid", code)
    assert first.status_code == 200
    body = first.json()
    assert body["isNewAccount"] is True and body["user"]["email"] == "a@test.invalid"
    assert body["accessExpiresIn"] == 3600 and body["accessToken"] and body["refreshToken"]
    again = await verify(client, "a@test.invalid", code)
    assert again.status_code == 400 and again.json()["code"] == "code_expired"


async def test_second_sign_in_is_not_a_new_account(client, mail):
    first = await sign_in(client, mail)
    await age_codes("a@test.invalid", 61)
    second = await sign_in(client, mail)
    assert second["isNewAccount"] is False and second["user"]["id"] == first["user"]["id"]


async def test_an_expired_code_fails(client, mail):
    await start(client, "a@test.invalid")
    async with get_sessionmaker()() as s:
        await s.execute(
            text(
                "UPDATE email_codes SET expires_at = now() - interval '1 second' "
                "WHERE email = 'a@test.invalid'"
            )
        )
        await s.commit()
    r = await verify(client, "a@test.invalid", mail.sent[-1][1])
    assert r.status_code == 400 and r.json()["code"] == "code_expired"


async def test_the_sixth_wrong_try_is_locked_even_with_the_right_code(client, mail):
    await start(client, "a@test.invalid")
    right = mail.sent[-1][1]
    wrong = "000000" if right != "000000" else "111111"
    for left in (4, 3, 2, 1, 0):
        r = await verify(client, "a@test.invalid", wrong)
        assert r.status_code == 400 and r.json() == {
            "code": "code_invalid",
            "detail": "الرمز غير صحيح.",
            "attemptsLeft": left,
        }
    r = await verify(client, "a@test.invalid", right)
    assert r.status_code == 400 and r.json()["code"] == "code_locked"


async def test_a_newer_code_replaces_the_older(client, mail):
    await start(client, "a@test.invalid")
    old = mail.sent[-1][1]
    await age_codes("a@test.invalid", 61)
    await start(client, "a@test.invalid")
    new = mail.sent[-1][1]
    if old != new:
        r = await verify(client, "a@test.invalid", old)
        assert r.status_code == 400
    assert (await verify(client, "a@test.invalid", new)).status_code == 200


async def test_verify_is_limited_per_ip(client, mail):
    for _ in range(30):
        await verify(client, "nobody@test.invalid", "123456")
    r = await verify(client, "nobody@test.invalid", "123456")
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"


# ── the review account ──────────────────────────────────────────────────────────────


async def test_review_code_works_only_for_the_review_email(client, mail):
    assert (await start(client, "review@test.invalid")).status_code == 202
    assert mail.sent == []  # no email for the review account
    ok = await verify(client, "Review@test.invalid", "424242")
    assert ok.status_code == 200 and ok.json()["user"]["email"] == "review@test.invalid"
    other = await verify(client, "someone@test.invalid", "424242")
    assert other.status_code == 400


async def test_review_bypass_is_off_when_not_set(mail):
    await _clean()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        auth_code_pepper="p", review_email="review@test.invalid"
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Forwarded-For": "203.0.113.251"},
    ) as c:
        await start(c, "review@test.invalid")
        assert mail.sent  # a real code was sent: no bypass without REVIEW_CODE
        r = await verify(c, "review@test.invalid", "")
        assert r.status_code == 400
    await _clean()


# ── tokens ──────────────────────────────────────────────────────────────────────────


async def test_access_token_opens_the_account(client, mail):
    tokens = await sign_in(client, mail)
    r = await client.get(
        "/api/account", headers={"Authorization": f"Bearer {tokens['accessToken']}"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "a@test.invalid" and body["devices"][0]["deviceId"] == "dev-A"
    assert body["devices"][0]["current"] is True
    for bad in ({}, {"Authorization": "Bearer nonsense"}, {"Authorization": tokens["accessToken"]}):
        r = await client.get("/api/account", headers=bad)
        assert r.status_code == 401 and r.json()["code"] == "token_expired"


async def test_an_expired_access_token_is_refused(client, mail):
    tokens = await sign_in(client, mail)
    async with get_sessionmaker()() as s:
        await s.execute(
            text(
                "UPDATE auth_sessions SET access_expires_at = now() - interval '1 second'"
                " FROM users WHERE users.id = auth_sessions.user_id"
                " AND users.email = 'a@test.invalid'"
            )
        )
        await s.commit()
    r = await client.get(
        "/api/account", headers={"Authorization": f"Bearer {tokens['accessToken']}"}
    )
    assert r.status_code == 401 and r.json()["code"] == "token_expired"


async def test_refresh_rotates_both_tokens(client, mail):
    tokens = await sign_in(client, mail)
    r = await client.post("/api/auth/refresh", json={"refreshToken": tokens["refreshToken"]})
    assert r.status_code == 200
    new = r.json()
    assert (
        new["accessToken"] != tokens["accessToken"]
        and new["refreshToken"] != tokens["refreshToken"]
    )
    old_access = await client.get(
        "/api/account", headers={"Authorization": f"Bearer {tokens['accessToken']}"}
    )
    assert old_access.status_code == 401
    assert (
        await client.get("/api/account", headers={"Authorization": f"Bearer {new['accessToken']}"})
    ).status_code == 200


async def test_a_reused_refresh_token_ends_the_whole_login(client, mail):
    tokens = await sign_in(client, mail)
    new = (
        await client.post("/api/auth/refresh", json={"refreshToken": tokens["refreshToken"]})
    ).json()
    reused = await client.post("/api/auth/refresh", json={"refreshToken": tokens["refreshToken"]})
    assert reused.status_code == 401 and reused.json()["code"] == "refresh_invalid"
    # the copy's discovery signed the device out: even the newest tokens stop working
    assert (
        await client.get("/api/account", headers={"Authorization": f"Bearer {new['accessToken']}"})
    ).status_code == 401
    assert (
        await client.post("/api/auth/refresh", json={"refreshToken": new["refreshToken"]})
    ).status_code == 401


async def test_logout_and_signing_out_a_device(client, mail):
    a = await sign_in(client, mail, device="dev-A")
    await age_codes("a@test.invalid", 61)
    b = await sign_in(client, mail, device="dev-B")
    auth_a = {"Authorization": f"Bearer {a['accessToken']}"}
    devices = (await client.get("/api/account", headers=auth_a)).json()["devices"]
    assert {d["deviceId"] for d in devices} == {"dev-A", "dev-B"}
    assert (await client.delete("/api/account/devices/dev-B", headers=auth_a)).status_code == 204
    assert (
        await client.get("/api/account", headers={"Authorization": f"Bearer {b['accessToken']}"})
    ).status_code == 401
    assert (await client.delete("/api/account/devices/dev-B", headers=auth_a)).status_code == 404
    assert (await client.post("/api/auth/logout", headers=auth_a)).status_code == 204
    assert (await client.get("/api/account", headers=auth_a)).status_code == 401


async def test_signing_in_again_on_a_device_ends_its_older_login(client, mail):
    first = await sign_in(client, mail, device="dev-A")
    await age_codes("a@test.invalid", 61)
    await sign_in(client, mail, device="dev-A")
    r = await client.get(
        "/api/account", headers={"Authorization": f"Bearer {first['accessToken']}"}
    )
    assert r.status_code == 401


async def test_deleting_the_account_removes_everything(client, mail):
    tokens = await sign_in(client, mail)
    auth = {"Authorization": f"Bearer {tokens['accessToken']}"}
    push = {
        "deviceId": "dev-A",
        "items": [
            {
                "kind": "bookmark",
                "key": "-|1|p-000001-b-001",
                "deleted": False,
                "updatedAt": "2026-10-01T00:00:00Z",
                "data": {"bookId": "1"},
            }
        ],
    }
    assert (await client.post("/api/sync/push", json=push, headers=auth)).status_code == 200
    assert (await client.delete("/api/account", headers=auth)).status_code == 204
    assert (await client.get("/api/account", headers=auth)).status_code == 401
    assert (
        await client.post("/api/auth/refresh", json={"refreshToken": tokens["refreshToken"]})
    ).status_code == 401
    async with get_sessionmaker()() as s:
        left = (
            await s.execute(
                text(
                    "SELECT (SELECT count(*) FROM users WHERE email = 'a@test.invalid'),"
                    " (SELECT count(*) FROM email_codes WHERE email = 'a@test.invalid'),"
                    " (SELECT count(*) FROM sync_records r"
                    "  LEFT JOIN users u ON u.id = r.user_id WHERE u.id IS NULL)"
                )
            )
        ).one()
    assert tuple(left) == (0, 0, 0)


# ── email failover ──────────────────────────────────────────────────────────────────


async def test_a_quota_error_falls_through_to_the_next_provider(client, monkeypatch):
    brevo = FakeSender("fake-brevo", email_sender.EmailQuotaExceeded)
    resend = FakeSender("fake-resend")
    monkeypatch.setattr(email_sender, "senders", lambda settings: [brevo, resend])
    assert (await start(client, "a@test.invalid")).status_code == 202
    assert resend.sent and resend.sent[0][0] == "a@test.invalid"
    async with get_sessionmaker()() as s:
        log = (
            await s.execute(
                text(
                    "SELECT provider, ok, error IS NOT NULL, email_hash FROM email_sends "
                    "WHERE provider LIKE 'fake%' ORDER BY id"
                )
            )
        ).all()
    assert [(r[0], r[1], r[2]) for r in log] == [
        ("fake-brevo", False, True),
        ("fake-resend", True, False),
    ]
    assert all(
        "@" not in r[3] and resend.sent[0][1] not in r[3] for r in log
    )  # no address, no code


async def test_all_providers_failing_is_503_and_a_resend_works_later(client, monkeypatch):
    down = [
        FakeSender("fake-brevo", email_sender.EmailQuotaExceeded),
        FakeSender("fake-resend", email_sender.EmailSendFailed),
    ]
    monkeypatch.setattr(email_sender, "senders", lambda settings: down)
    r = await start(client, "a@test.invalid")
    assert r.status_code == 503 and r.json()["code"] == "email_unavailable"
    up = FakeSender("fake-brevo")
    monkeypatch.setattr(email_sender, "senders", lambda settings: [up])
    await age_codes("a@test.invalid", 61)
    assert (await start(client, "a@test.invalid")).status_code == 202
    assert (await verify(client, "a@test.invalid", up.sent[-1][1])).status_code == 200


def test_providers_without_keys_are_skipped():
    s = Settings(email_providers="brevo,resend,console", brevo_api_key="", resend_api_key="k")
    assert [p.name for p in email_sender.senders(s)] == ["resend", "console"]
    assert email_sender.parse_from("مكتبة الثقلين <noreply@x.com>") == (
        "مكتبة الثقلين",
        "noreply@x.com",
    )


def test_iso_times_are_utc_with_milliseconds():
    from app.services.auth_service import iso

    assert iso(dt.datetime(2026, 10, 22, 9, 0, tzinfo=dt.UTC)) == "2026-10-22T09:00:00.000Z"
