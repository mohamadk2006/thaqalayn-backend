"""The user's chats on a real database: storing turns, what a follow-up sees, limits,
ownership, deleting and purging, and the /api/chat/conversations endpoints (rows removed
afterwards)."""

import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api.errors import ApiError
from app.config import Settings
from app.db import get_sessionmaker
from app.main import create_app
from app.models.chat import ChatRequest
from app.services import auth_service, chat_history

SOURCE = {
    "n": 1,
    "bookId": "4407",
    "title": "الصحيفة",
    "volume": None,
    "page": "48",
    "sequence": 52,
    "text": "نص المقطع",
    "score": 0.7,
    "foundBy": "meaning",
}
EMAILS = ("chat-a@history.test.invalid", "chat-b@history.test.invalid")


async def _clean():
    async with get_sessionmaker()() as s:
        await s.execute(text("DELETE FROM users WHERE email LIKE '%@history.test.invalid'"))
        await s.execute(text("DELETE FROM chat_requests WHERE message LIKE 'history-test%'"))
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


async def turn(s, user, chat, question, answer="جواب", status="ok"):
    return await chat_history.add_turn(s, user, chat, question, answer, status, [SOURCE], 7)


async def test_the_first_turn_creates_the_chat_and_a_follow_up_sees_the_turns(users):
    a, _ = users
    chat = uuid.uuid4()
    async with get_sessionmaker()() as s:
        row, reply = await turn(s, a, chat, "ما فضل الصبر؟  " + "ا" * 100, "الصبر رأس الإيمان")
        assert row.title.endswith("…") and len(row.title) == chat_history.TITLE_CHARS
        await turn(
            s, a, chat, "وماذا عن الصادق؟", "", "no_results"
        )  # nothing found: no answer text
        await turn(s, a, chat, "سؤال ثالث", "جواب ثالث")
        settings = Settings(chat_history_messages=4)
        earlier = await chat_history.earlier_turns(s, settings, chat)
        # the last 4 messages with text, oldest first; the empty answer is left out
        assert earlier == [
            ("assistant", "الصبر رأس الإيمان"),
            ("user", "وماذا عن الصادق؟"),
            ("user", "سؤال ثالث"),
            ("assistant", "جواب ثالث"),
        ]
        assert await chat_history.earlier_turns(s, settings, None) == []
        assert await chat_history.message_count(s, chat) == 6
        assert reply.sources == [SOURCE] and reply.request_id == 7


async def test_a_chat_is_only_its_owners_and_deleting_hides_it(users):
    a, b = users
    chat = uuid.uuid4()
    async with get_sessionmaker()() as s:
        await turn(s, a, chat, "سؤال")
        await chat_history.get(s, a, chat)
        with pytest.raises(ApiError) as other:
            await chat_history.get(s, b, chat)
        assert other.value.status == 404 and other.value.code == "conversation_not_found"
        with pytest.raises(ApiError):
            await turn(s, b, chat, "سؤال من غيره")  # nobody adds to another user's chat
        await chat_history.delete(s, a, chat)
        with pytest.raises(ApiError):
            await chat_history.get(s, a, chat)
        assert await chat_history.listing(s, a, 10, None) == []


async def test_limits_on_messages_and_chats(users):
    a, _ = users
    chat = uuid.uuid4()
    async with get_sessionmaker()() as s:
        settings = Settings(chat_max_messages=4, chat_max_conversations=2)
        await turn(s, a, chat, "1")
        await chat_history.check_room(s, settings, a, chat)  # 2 messages of 4
        await turn(s, a, chat, "2")
        with pytest.raises(ApiError) as full:
            await chat_history.check_room(s, settings, a, chat)
        assert full.value.status == 409 and full.value.code == "conversation_full"
        await turn(s, a, uuid.uuid4(), "another chat")
        with pytest.raises(ApiError) as many:
            await chat_history.check_room(s, settings, a, None)  # a new chat: 2 of 2 used
        assert many.value.code == "too_many_conversations"


async def test_the_list_pages_newest_first_and_rename(users):
    a, _ = users
    chats = [uuid.uuid4() for _ in range(3)]
    async with get_sessionmaker()() as s:
        for n, chat in enumerate(chats):
            await turn(s, a, chat, f"سؤال {n}")
        first = await chat_history.listing(s, a, 2, None)
        assert [c.title for c in first] == ["سؤال 2", "سؤال 1"]
        older = await chat_history.listing(s, a, 2, first[-1].updated_at)
        assert [c.title for c in older] == ["سؤال 0"]
        renamed = await chat_history.rename(s, a, chats[0], "  عنوان   جديد ")
        assert renamed.title == "عنوان جديد"


async def test_purge_removes_old_deleted_chats_and_blanks_their_log_text(users):
    a, _ = users
    old, recent = uuid.uuid4(), uuid.uuid4()
    async with get_sessionmaker()() as s:
        await turn(s, a, old, "قديم")
        await turn(s, a, recent, "حديث")
        s.add(
            ChatRequest(
                message="history-test secret question",
                status="ok",
                answer="a",
                conversation_id=old,
                total_tokens=50,
            )
        )
        await s.commit()
        await chat_history.delete(s, a, old)
        await chat_history.delete(s, a, recent)
        await s.execute(
            text(
                "UPDATE chat_conversations SET deleted_at = now() - interval '31 days' "
                "WHERE id = :i"
            ),
            {"i": old},
        )
        await s.commit()
        await chat_history.purge(s)
        gone = await s.scalar(
            text("SELECT count(*) FROM chat_conversations WHERE id = :i"), {"i": old}
        )
        kept = await s.scalar(
            text("SELECT count(*) FROM chat_conversations WHERE id = :i"), {"i": recent}
        )
        assert (gone, kept) == (0, 1)
        row = (
            await s.execute(
                text(
                    "SELECT message, answer, total_tokens FROM chat_requests "
                    "WHERE conversation_id = :i"
                ),
                {"i": old},
            )
        ).one()
        assert row == ("", None, 50)  # the text is gone, the tokens stay


async def test_the_endpoints(users, monkeypatch):
    a, b = users
    me = {"id": a}

    async def caller(session, authorization):
        if not authorization:
            raise ApiError(401, "token_expired")
        return SimpleNamespace(user=SimpleNamespace(id=me["id"]))

    monkeypatch.setattr(auth_service, "caller", caller)
    chat = uuid.uuid4()
    async with get_sessionmaker()() as s:
        await turn(s, a, chat, "ما فضل الصبر؟", "رأس الإيمان [1]")
    headers = {"Authorization": "Bearer x"}
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        assert (await c.get("/api/chat/conversations")).status_code == 401
        listing = (await c.get("/api/chat/conversations", headers=headers)).json()
        assert [i["id"] for i in listing["items"]] == [str(chat)] and listing["nextBefore"] is None
        detail = (await c.get(f"/api/chat/conversations/{chat}", headers=headers)).json()
        assert detail["title"] == "ما فضل الصبر؟"
        assert [(m["role"], m["text"]) for m in detail["messages"]] == [
            ("user", "ما فضل الصبر؟"),
            ("assistant", "رأس الإيمان [1]"),
        ]
        renamed = await c.patch(
            f"/api/chat/conversations/{chat}", json={"title": "الصبر"}, headers=headers
        )
        assert renamed.json()["title"] == "الصبر"
        me["id"] = b  # another user cannot see, rename or delete it
        for call in (
            c.get(f"/api/chat/conversations/{chat}", headers=headers),
            c.delete(f"/api/chat/conversations/{chat}", headers=headers),
        ):
            assert (await call).status_code == 404
        assert (await c.get("/api/chat/conversations", headers=headers)).json()["items"] == []
        me["id"] = a
        assert (
            await c.delete(f"/api/chat/conversations/{chat}", headers=headers)
        ).status_code == 204
        assert (await c.get(f"/api/chat/conversations/{chat}", headers=headers)).status_code == 404


async def test_deleting_the_account_removes_the_chat_log_and_old_failures_are_blanked(users):
    a, b = users
    chat = uuid.uuid4()
    async with get_sessionmaker()() as s:
        await turn(s, a, chat, "سؤال المستخدم أ")
        for who, conversation, message in (
            (f"u:{a}", chat, "history-test a in a chat"),
            (f"u:{a}", None, "history-test a failed, no chat"),
            (f"u:{b}", None, "history-test b"),
        ):
            s.add(ChatRequest(message=message, status="ok", who=who, conversation_id=conversation))
        await s.commit()
        await auth_service.delete_account(
            s, SimpleNamespace(user=SimpleNamespace(id=a, email=EMAILS[0]))
        )
        left = (
            (
                await s.execute(
                    text("SELECT message FROM chat_requests WHERE message LIKE 'history-test%'")
                )
            )
            .scalars()
            .all()
        )
        assert left == ["history-test b"]  # the other user's log is untouched
        gone = await s.scalar(
            text("SELECT count(*) FROM chat_conversations WHERE user_id = :u"), {"u": a}
        )
        assert gone == 0


async def test_a_failed_request_loses_its_text_after_a_month(users):
    age_sql = text(
        "UPDATE chat_requests SET created_at = now() - make_interval(days => :d) WHERE id = :i"
    )
    ids = {}
    async with get_sessionmaker()() as s:
        for name, status, age in (
            ("old failure", "error", 40),
            ("recent failure", "error", 5),
            ("old answer", "ok", 40),
        ):
            row = ChatRequest(message=f"history-test {name}", status=status, answer="a")
            s.add(row)
            await s.flush()
            await s.execute(age_sql, {"d": age, "i": row.id})
            ids[name] = row.id
        await s.commit()
        await chat_history.purge(s)
        messages = {
            name: await s.scalar(text("SELECT message FROM chat_requests WHERE id = :i"), {"i": i})
            for name, i in ids.items()
        }
        await s.execute(
            text("DELETE FROM chat_requests WHERE id = ANY(:ids)"), {"ids": list(ids.values())}
        )
        await s.commit()
    assert messages == {
        "old failure": "",  # blanked
        "recent failure": "history-test recent failure",
        "old answer": "history-test old answer",  # answers are kept until their chat is deleted
    }
