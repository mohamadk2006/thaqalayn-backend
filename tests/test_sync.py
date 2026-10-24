"""Sync: pull by cursor, push with the merge rules (docs/accounts_sync_spec.md §4-§5).

The merge rules are tested directly (`merge`), and end to end through the API with two
signed-in users on a real database (rows removed afterwards).
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
from app.services.sync_service import COPY_SUFFIX, Version, merge

T0 = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
_ip = itertools.count(1)


def at(minutes: float) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes)


def iso(minutes: float) -> str:
    return at(minutes).isoformat().replace("+00:00", "Z")


def v(data=None, minute=0, order_minute=None, device="A", deleted=False):
    return Version(
        None if deleted else (data or {}),
        deleted,
        at(minute),
        at(order_minute) if order_minute is not None else None,
        device,
    )


# ── the merge rules ─────────────────────────────────────────────────────────────────


def test_newer_write_wins_and_older_is_ignored():
    stored = v({"color": "yellow"}, minute=5)
    assert merge("highlight", stored, 10, v({"color": "blue"}, minute=6), 10).write.data == {
        "color": "blue"
    }
    older = merge("highlight", stored, 10, v({"color": "blue"}, minute=4), 10)
    assert older.status == "older_ignored" and older.write is None


def test_a_tie_goes_to_the_larger_device_id():
    stored = v({"x": 1}, minute=5, device="B")
    assert merge("bookmark", stored, 1, v({"x": 2}, minute=5, device="C"), 1).status == "applied"
    assert (
        merge("bookmark", stored, 1, v({"x": 2}, minute=5, device="A"), 1).status == "older_ignored"
    )


def test_a_delete_is_a_write_like_any_other():
    edit = v({"t": 1}, minute=5)
    newer_delete = merge("bookmark", edit, 1, v(minute=6, deleted=True), 1)
    assert newer_delete.write.deleted and newer_delete.write.data is None
    assert merge("bookmark", edit, 1, v(minute=4, deleted=True), 1).status == "older_ignored"
    deleted = v(minute=5, deleted=True)
    revived = merge("bookmark", deleted, 1, v({"t": 2}, minute=6), 1)
    assert revived.status == "applied" and not revived.write.deleted


def test_order_and_content_merge_separately():
    # phone A renamed the folder at 10:00; phone B moved it at 10:05 but its title is older
    stored = v({"title": "new name", "sortIndex": 1}, minute=10, order_minute=0, device="A")
    moved = v({"title": "old name", "sortIndex": 7}, minute=0, order_minute=5, device="B")
    out = merge("noteFolder", stored, 3, moved, 3)
    assert out.status == "applied"
    assert out.write.data == {"title": "new name", "sortIndex": 7}
    assert out.write.updated_at == at(10) and out.write.order_changed_at == at(5)


def test_session_books_merge_their_position():
    stored = v({"sessionId": "S", "bookId": "1", "position": 1.0}, minute=0, order_minute=0)
    out = merge(
        "sessionBook",
        stored,
        2,
        v({"sessionId": "S", "bookId": "1", "position": 3.5}, minute=0, order_minute=4, device="B"),
        2,
    )
    assert out.write.data["position"] == 3.5


def test_order_does_not_apply_to_other_kinds():
    stored = v({"color": "yellow"}, minute=5, order_minute=0)
    out = merge("highlight", stored, 1, v({"color": "blue"}, minute=1, order_minute=9), 1)
    assert out.status == "older_ignored"


def test_a_stale_note_edit_keeps_the_losing_text_as_a_copy():
    stored = v({"title": "T", "bodyText": "server text"}, minute=5, device="A")
    # B edited at 6, without having seen seq 12
    out = merge(
        "note", stored, 12, v({"title": "T", "bodyText": "phone text"}, minute=6, device="B"), 10
    )
    assert out.status == "applied_with_copy"
    assert out.write.data["bodyText"] == "phone text" and out.copy["bodyText"] == "server text"
    # the stale edit is older: it loses, and its text becomes the copy
    out = merge(
        "note", stored, 12, v({"title": "T", "bodyText": "phone text"}, minute=4, device="B"), 10
    )
    assert out.status == "older_ignored_with_copy" and out.copy["bodyText"] == "phone text"


def test_no_copy_when_up_to_date_unchanged_or_deleted():
    stored = v({"title": "T", "bodyText": "x"}, minute=5)
    assert merge("note", stored, 12, v({"title": "T", "bodyText": "y"}, minute=6), 12).copy is None
    assert merge("note", stored, 12, v({"title": "T", "bodyText": "x"}, minute=6), 3).copy is None
    assert merge("note", stored, 12, v(minute=6, deleted=True), 3).copy is None
    assert merge("note", v(minute=5, deleted=True), 12, v({"title": "T"}, minute=6), 3).copy is None


# ── through the API ─────────────────────────────────────────────────────────────────


async def _clean():
    async with get_sessionmaker()() as s:
        await s.execute(text("DELETE FROM users WHERE email LIKE '%@sync.test.invalid'"))
        await s.execute(text("DELETE FROM email_codes WHERE email LIKE '%@sync.test.invalid'"))
        await s.execute(text("DELETE FROM verify_calls WHERE ip << '198.51.100.0/24'"))
        await s.commit()


class Phone:
    def __init__(self, client, email, device):
        self.client, self.email, self.device, self.auth, self.cursor = (
            client,
            email,
            device,
            None,
            0,
        )

    async def sign_in(self, codes):
        await self.client.post("/api/auth/email/start", json={"email": self.email})
        r = await self.client.post(
            "/api/auth/email/verify",
            json={"email": self.email, "code": codes[-1], "deviceId": self.device},
        )
        self.auth = {"Authorization": f"Bearer {r.json()['accessToken']}"}

    async def push(self, *items):
        r = await self.client.post(
            "/api/sync/push",
            headers=self.auth,
            json={"deviceId": self.device, "items": list(items)},
        )
        assert r.status_code == 200, r.text
        return r.json()["results"]

    async def pull(self, limit=500):
        items = []
        while True:
            r = (
                await self.client.get(
                    f"/api/sync/changes?since={self.cursor}&limit={limit}", headers=self.auth
                )
            ).json()
            items += r["items"]
            self.cursor = r["cursor"]
            if not r["hasMore"]:
                return items


def note(key, body, minute, base=None, **extra):
    return {
        "kind": "note",
        "key": key,
        "deleted": False,
        "updatedAt": iso(minute),
        "baseSeq": base,
        "data": {"title": "N", "bodyText": body, "createdAt": iso(0), **extra},
    }


@pytest.fixture
async def phones(monkeypatch):
    await _clean()
    codes = []

    class Capture:
        name = "fake"

        async def send_code(self, to, code, language):
            codes.append(code)

    monkeypatch.setattr(email_sender, "senders", lambda settings: [Capture()])
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(auth_code_pepper="p")
    ip = f"198.51.100.{next(_ip) % 250 + 1}"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Forwarded-For": ip}
    ) as c:
        a = Phone(c, "user@sync.test.invalid", "A")
        b = Phone(c, "user@sync.test.invalid", "B")
        other = Phone(c, "other@sync.test.invalid", "X")
        await a.sign_in(codes)
        async with get_sessionmaker()() as s:  # past the one-code-a-minute limit
            await s.execute(
                text("UPDATE email_codes SET created_at = created_at - interval '2 minutes'")
            )
            await s.commit()
        await b.sign_in(codes)
        await other.sign_in(codes)
        yield a, b, other
    await _clean()


async def test_sync_needs_a_token(phones):
    a, _, _ = phones
    r = await a.client.get("/api/sync/changes?since=0")
    assert r.status_code == 401 and r.json()["code"] == "token_expired"


async def test_push_then_pull_on_another_device(phones):
    a, b, _ = phones
    res = await a.push(note("6F1C-AAAA", "hello", 1))
    assert res[0]["status"] == "applied" and isinstance(res[0]["seq"], int)
    items = await b.pull()
    assert [(i["kind"], i["key"], i["data"]["bodyText"]) for i in items] == [
        ("note", "6F1C-AAAA", "hello")
    ]
    assert items[0]["updatedAt"] == "2026-10-01T00:01:00.000Z"
    assert items[0]["seq"] == res[0]["seq"]  # a client sends it back as baseSeq


async def test_keys_compare_case_insensitively(phones):
    a, b, _ = phones
    await a.push(note("ABCD-1", "one", 1))
    res = await b.push(note("abcd-1", "two", 2, base=None))
    assert res[0]["status"] in ("applied", "applied_with_copy")
    keys = {i["key"] for i in await a.pull() if i["data"] and i["data"].get("copyOf") is None}
    assert keys == {"ABCD-1"}  # one record, under the key as first sent


async def test_older_ignored_newer_applied_and_future_clamped(phones):
    a, b, _ = phones
    bm = {
        "kind": "bookmark",
        "key": "-|4407|p-000052-b-001",
        "deleted": False,
        "data": {"bookId": "4407", "locationId": "p-000052-b-001"},
    }
    first = (await a.push({**bm, "updatedAt": iso(5)}))[0]
    old = (await b.push({**bm, "updatedAt": iso(4)}))[0]
    assert old["status"] == "older_ignored" and old["seq"] == first["seq"]
    future = (dt.datetime.now(dt.UTC) + dt.timedelta(days=3)).isoformat()
    newer = (await b.push({**bm, "updatedAt": future}))[0]
    assert newer["status"] == "applied" and newer["seq"] > first["seq"]
    stored = [i for i in await a.pull() if i["kind"] == "bookmark"][-1]
    assert dt.datetime.fromisoformat(stored["updatedAt"].replace("Z", "+00:00")) <= dt.datetime.now(
        dt.UTC
    )


async def test_rejections_do_not_stop_the_other_items(phones):
    a, _, _ = phones
    res = await a.push(
        {"kind": "recipe", "key": "x", "updatedAt": iso(1), "data": {}},
        {"kind": "note", "key": "BIG", "updatedAt": iso(1), "data": {"bodyText": "x" * 300_000}},
        {"kind": "note", "key": "OLD", "updatedAt": "2019-12-31T00:00:00Z", "data": {}},
        note("FINE", "ok", 1),
    )
    assert [r["status"] for r in res] == [
        "rejected_kind",
        "rejected_size",
        "rejected_time",
        "applied",
    ]


async def test_too_many_items_is_413(phones):
    a, _, _ = phones
    items = [note(f"K{n}", "x", 1) for n in range(501)]
    r = await a.client.post(
        "/api/sync/push", headers=a.auth, json={"deviceId": "A", "items": items}
    )
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"


async def test_delete_and_revive(phones):
    a, b, _ = phones
    await a.push(note("N1", "text", 1))
    await b.push({"kind": "note", "key": "N1", "deleted": True, "updatedAt": iso(2), "data": None})
    tomb = [i for i in await a.pull() if i["key"] == "N1"][-1]
    assert tomb["deleted"] is True and tomb["data"] is None
    await a.push(note("N1", "back", 3, base=10**9))
    assert [i for i in await b.pull() if i["key"] == "N1"][-1]["data"]["bodyText"] == "back"


async def test_content_on_one_phone_and_order_on_the_other_both_survive(phones):
    a, b, _ = phones
    folder = {"kind": "noteFolder", "key": "F1", "deleted": False}
    await a.push(
        {
            **folder,
            "updatedAt": iso(1),
            "orderChangedAt": iso(1),
            "data": {"title": "old", "sortIndex": 0},
        }
    )
    await b.pull()
    await a.push(
        {
            **folder,
            "updatedAt": iso(5),
            "orderChangedAt": iso(1),
            "data": {"title": "renamed", "sortIndex": 0},
        }
    )
    await b.push(
        {
            **folder,
            "updatedAt": iso(1),
            "orderChangedAt": iso(6),
            "data": {"title": "old", "sortIndex": 3},
        }
    )
    final = [i for i in await a.pull() if i["key"] == "F1"][-1]
    assert final["data"] == {"title": "renamed", "sortIndex": 3}


async def test_a_stale_note_edit_makes_one_copy_both_phones_get(phones):
    a, b, _ = phones
    seq = (await a.push(note("N2", "v1", 1)))[0]["seq"]
    await b.pull()
    await a.push(note("N2", "edited on A", 5, base=seq))
    res = (await b.push(note("N2", "edited on B", 6, base=seq, folderId="F9")))[
        0
    ]  # B never saw A's edit
    assert res["status"] == "applied_with_copy" and res["copyKey"]
    on_a = {i["key"]: i for i in await a.pull()}
    on_b = {i["key"]: i for i in await b.pull()}
    for got in (on_a, on_b):
        assert got["N2"]["data"]["bodyText"] == "edited on B"
        copy = got[res["copyKey"]]["data"]
        assert copy["bodyText"] == "edited on A" and copy["title"] == "N" + COPY_SUFFIX
        assert copy["copyOf"] == "N2"
    copies = [k for k, i in on_a.items() if i["data"] and i["data"].get("copyOf") == "N2"]
    assert copies == [res["copyKey"]]


async def test_pull_pages_in_seq_order(phones):
    a, b, _ = phones
    await a.push(*[note(f"P{n:02}", str(n), 1) for n in range(7)])
    seen, cursors = [], []
    while True:
        r = (
            await b.client.get(f"/api/sync/changes?since={b.cursor}&limit=3", headers=b.auth)
        ).json()
        seen += [i["key"] for i in r["items"]]
        cursors.append(r["cursor"])
        b.cursor = r["cursor"]
        if not r["hasMore"]:
            break
    assert (
        seen == [f"P{n:02}" for n in range(7)] and len(cursors) == 3 and cursors == sorted(cursors)
    )
    again = (await b.client.get(f"/api/sync/changes?since={b.cursor}", headers=b.auth)).json()
    assert again == {"cursor": b.cursor, "hasMore": False, "items": []}


async def test_one_user_never_sees_another_users_records(phones):
    a, _, other = phones
    await a.push(note("SECRET", "mine", 1))
    assert await other.pull() == []
    res = await other.push(note("SECRET", "theirs", 9))
    assert res[0]["status"] == "applied"  # their own record, not a merge with mine
    assert [i["data"]["bodyText"] for i in await a.pull() if i["key"] == "SECRET"] == ["mine"]
