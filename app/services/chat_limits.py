"""Who may ask the chatbot now: limits that keep the free Gemini quota from running out.

Each question costs about three Gemini calls (rewrite, rerank, answer), so the limits are
counted in questions, from the chat_requests log. Per asker (a signed-in user, else the app
install's X-Client-Id, else the address): a daily quota, a per-minute rate and one question
at a time. For everyone together: a daily total. All are settings (CHAT_*), 0 = no limit.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.chat import ChatRequest

_in_flight: set[str] = set()  # askers with a question being answered (this process)


@dataclass
class Counts:
    day: int  # this asker's answered questions today (UTC)
    minute: int  # this asker's questions in the last minute
    everyone: int  # all questions today, failed ones too (they used Gemini calls)


@dataclass
class Verdict:
    code: str | None  # None: allowed
    detail: str = ""
    retry_after: int = 0
    limit: int = 0
    remaining: int = 0
    reset_at: dt.datetime | None = None


def day_start(now: dt.datetime) -> dt.datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def counts(session: AsyncSession, who: str, now: dt.datetime) -> Counts:
    today, minute_ago = day_start(now), now - dt.timedelta(minutes=1)
    answered = ChatRequest.status.in_(("ok", "no_results"))
    row = (
        await session.execute(
            select(
                func.count().filter((ChatRequest.who == who) & answered),
                func.count().filter(
                    (ChatRequest.who == who) & (ChatRequest.created_at >= minute_ago)
                ),
                func.count(),
            ).where(ChatRequest.created_at >= today)
        )
    ).one()
    return Counts(day=row[0], minute=row[1], everyone=row[2])


def decide(c: Counts, settings: Settings, now: dt.datetime, busy: bool) -> Verdict:
    reset = day_start(now) + dt.timedelta(days=1)
    wait_day = max(1, int((reset - now).total_seconds()))
    daily = settings.chat_daily_limit
    remaining = max(0, daily - c.day) if daily else 0
    base = {"limit": daily, "remaining": remaining, "reset_at": reset}
    if settings.chat_global_daily_limit and c.everyone >= settings.chat_global_daily_limit:
        return Verdict("service_limit", retry_after=wait_day, **base)
    if daily and c.day >= daily:
        return Verdict("daily_limit", retry_after=wait_day, **base)
    if busy:
        return Verdict("in_progress", retry_after=15, **base)
    if settings.chat_per_minute and c.minute >= settings.chat_per_minute:
        return Verdict("too_fast", retry_after=60, **base)
    return Verdict(None, **base)


async def check(session: AsyncSession, settings: Settings, who: str) -> Verdict:
    now = dt.datetime.now(dt.UTC)
    return decide(await counts(session, who, now), settings, now, who in _in_flight)


def begin(who: str) -> None:
    _in_flight.add(who)


def end(who: str) -> None:
    _in_flight.discard(who)
