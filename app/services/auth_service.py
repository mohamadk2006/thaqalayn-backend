"""Sign-in by an emailed one-time code, and the login sessions that follow
(docs/accounts_sync_spec.md §1).

Codes: 6 digits, stored as HMAC-SHA256 (keyed with AUTH_CODE_PEPPER, bound to the email),
valid 10 minutes, dead after 5 wrong tries; only the newest code of an email works.
Tokens: opaque random strings stored as SHA-256 hashes; the access token lives an hour,
the refresh token 180 days and is replaced on every use. A refresh token that comes back
after it was used has been copied, and its whole login session is revoked.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.config import Settings
from app.models.account import AuthSession, EmailCode, UsedRefreshToken, User, VerifyCall
from app.services import email_sender

CODE_TTL = dt.timedelta(minutes=10)
MAX_WRONG_TRIES = 5
RESEND_AFTER = 60  # seconds between two codes for one email
CODES_PER_EMAIL_HOUR = 5
CODES_PER_IP_HOUR = 20
VERIFY_PER_IP_HOUR = 30
ACCESS_TTL = dt.timedelta(hours=1)
REFRESH_TTL = dt.timedelta(days=180)
SEEN_EVERY = dt.timedelta(minutes=5)  # how often a request refreshes last_seen_at
LANGUAGES = ("ar", "fa", "en")
PLATFORMS = ("ios", "android", "web")


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def normalize_email(raw: str) -> str:
    """Trimmed and lowercased (dots and +tags kept). 422 email_invalid when implausible."""
    email = (raw or "").strip().lower()
    local, at, domain = email.partition("@")
    if (
        not at
        or "@" in domain
        or not local
        or len(email) > 254
        or "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
        or any(c.isspace() for c in email)
    ):
        raise ApiError(422, "email_invalid")
    return email


def client_ip(request: Request) -> str | None:
    """The caller's address. Behind the server's proxy (Traefik) the client is the last
    address the proxy added to X-Forwarded-For; earlier entries can be forged by anyone."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip() or None
    return request.client.host if request.client else None


def _code_hash(settings: Settings, email: str, code: str) -> str:
    return hmac.new(
        settings.auth_code_pepper.encode(), f"{email}:{code}".encode(), hashlib.sha256
    ).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _require_pepper(settings: Settings) -> None:
    if not settings.auth_code_pepper:
        raise ApiError(503, "auth_unavailable")


def _is_review(settings: Settings, email: str) -> bool:
    return bool(
        settings.review_email
        and settings.review_code
        and email == settings.review_email.strip().lower()
    )


def _retry_after(since: dt.datetime, window: dt.timedelta) -> int:
    return max(1, int((since + window - now()).total_seconds()) + 1)


# ── start ───────────────────────────────────────────────────────────────────────────


async def start(
    session: AsyncSession,
    settings: Settings,
    raw_email: str,
    language: str,
    ip: str | None,
    providers=None,
) -> dict:
    _require_pepper(settings)
    email = normalize_email(raw_email)
    answer = {"resendAfter": RESEND_AFTER, "expiresIn": int(CODE_TTL.total_seconds())}
    if _is_review(settings, email):
        return answer  # the review account: no email, REVIEW_CODE works at verify

    hour_ago = now() - dt.timedelta(hours=1)
    last = (
        await session.execute(
            select(EmailCode.created_at)
            .where(EmailCode.email == email)
            .order_by(EmailCode.created_at.desc())
            .limit(1)
        )
    ).scalar()
    if last and last > now() - dt.timedelta(seconds=RESEND_AFTER):
        raise ApiError(
            429, "rate_limited", retry_after=_retry_after(last, dt.timedelta(seconds=RESEND_AFTER))
        )
    for column, value, cap in (
        (EmailCode.email, email, CODES_PER_EMAIL_HOUR),
        (EmailCode.ip, ip, CODES_PER_IP_HOUR),
    ):
        if value is None:
            continue
        count, oldest = (
            await session.execute(
                select(func.count(), func.min(EmailCode.created_at)).where(
                    column == value, EmailCode.created_at > hour_ago
                )
            )
        ).one()
        if count >= cap:
            raise ApiError(
                429, "rate_limited", retry_after=_retry_after(oldest, dt.timedelta(hours=1))
            )

    code = f"{secrets.randbelow(10**6):06d}"
    session.add(
        EmailCode(
            email=email,
            code_hash=_code_hash(settings, email, code),
            expires_at=now() + CODE_TTL,
            ip=ip,
            created_at=now(),
        )
    )
    await session.flush()
    try:
        await email_sender.send_code(session, settings, email, code, language, providers)
    except email_sender.EmailUnavailable:
        await session.commit()  # the code is kept: a later resend works
        raise ApiError(503, "email_unavailable") from None
    await session.commit()
    return answer


# ── verify ──────────────────────────────────────────────────────────────────────────


@dataclass
class Device:
    device_id: str
    device_name: str | None = None
    platform: str | None = None


async def verify(
    session: AsyncSession,
    settings: Settings,
    raw_email: str,
    code: str,
    device: Device,
    ip: str | None,
) -> dict:
    _require_pepper(settings)
    email = normalize_email(raw_email)
    if ip is not None:
        calls = (
            await session.execute(
                select(func.count(), func.min(VerifyCall.created_at)).where(
                    VerifyCall.ip == ip, VerifyCall.created_at > now() - dt.timedelta(hours=1)
                )
            )
        ).one()
        if calls[0] >= VERIFY_PER_IP_HOUR:
            raise ApiError(
                429, "rate_limited", retry_after=_retry_after(calls[1], dt.timedelta(hours=1))
            )
    session.add(VerifyCall(ip=ip, created_at=now()))
    await session.flush()

    code = (code or "").strip()
    if not (_is_review(settings, email) and hmac.compare_digest(code, settings.review_code)):
        row = (
            await session.execute(
                select(EmailCode)
                .where(EmailCode.email == email)
                .order_by(EmailCode.created_at.desc(), EmailCode.id.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar()
        if row is None or row.used_at is not None or row.expires_at <= now():
            await session.commit()
            raise ApiError(400, "code_expired")
        if row.attempts >= MAX_WRONG_TRIES:
            await session.commit()
            raise ApiError(400, "code_locked")
        if not hmac.compare_digest(row.code_hash, _code_hash(settings, email, code)):
            row.attempts += 1
            await session.commit()
            raise ApiError(400, "code_invalid", attemptsLeft=MAX_WRONG_TRIES - row.attempts)
        row.used_at = now()

    inserted = (
        await session.execute(
            insert(User)
            .values(id=uuid.uuid4(), email=email, created_at=now())
            .on_conflict_do_nothing(index_elements=[User.email])
            .returning(User.id)
        )
    ).scalar()
    user = (await session.execute(select(User).where(User.email == email))).scalar_one()
    tokens = await _new_login(session, user, device)
    await session.commit()
    return {**tokens, "user": user_out(user), "isNewAccount": inserted is not None}


def user_out(user: User) -> dict:
    return {"id": str(user.id), "email": user.email, "createdAt": iso(user.created_at)}


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _tokens() -> tuple[str, str]:
    return secrets.token_urlsafe(32), secrets.token_urlsafe(32)


def _token_answer(access: str, refresh: str) -> dict:
    return {
        "accessToken": access,
        "accessExpiresIn": int(ACCESS_TTL.total_seconds()),
        "refreshToken": refresh,
    }


async def _new_login(session: AsyncSession, user: User, device: Device) -> dict:
    """A fresh login session for this device; an earlier one of the same device ends."""
    await session.execute(
        update(AuthSession)
        .where(
            AuthSession.user_id == user.id,
            AuthSession.device_id == device.device_id,
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=now())
    )
    access, refresh = _tokens()
    session.add(
        AuthSession(
            user_id=user.id,
            device_id=device.device_id,
            device_name=device.device_name,
            platform=device.platform,
            access_hash=token_hash(access),
            refresh_hash=token_hash(refresh),
            access_expires_at=now() + ACCESS_TTL,
            refresh_expires_at=now() + REFRESH_TTL,
            created_at=now(),
            last_seen_at=now(),
        )
    )
    return _token_answer(access, refresh)


# ── refresh, logout, the signed-in caller ───────────────────────────────────────────


async def refresh(session: AsyncSession, refresh_token: str) -> dict:
    h = token_hash(refresh_token or "")
    login = (
        await session.execute(
            select(AuthSession).where(AuthSession.refresh_hash == h).with_for_update()
        )
    ).scalar()
    if login and login.revoked_at is None and login.refresh_expires_at > now():
        session.add(UsedRefreshToken(token_hash=h, session_id=login.id, used_at=now()))
        access, new_refresh = _tokens()
        login.access_hash, login.refresh_hash = token_hash(access), token_hash(new_refresh)
        login.access_expires_at = now() + ACCESS_TTL
        login.refresh_expires_at = now() + REFRESH_TTL
        login.last_seen_at = now()
        await session.commit()
        return _token_answer(access, new_refresh)
    used = await session.get(UsedRefreshToken, h)
    if used is not None:  # a copied token: end that whole login session
        await session.execute(
            update(AuthSession)
            .where(AuthSession.id == used.session_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now())
        )
        await session.commit()
    raise ApiError(401, "refresh_invalid")


@dataclass
class Caller:
    user: User
    login: AuthSession


async def caller(session: AsyncSession, authorization: str | None) -> Caller:
    """The signed-in user of a request (Authorization: Bearer <access token>), or 401."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(401, "token_expired")
    row = (
        await session.execute(
            select(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .where(
                AuthSession.access_hash == token_hash(token.strip()),
                AuthSession.revoked_at.is_(None),
                AuthSession.access_expires_at > now(),
            )
        )
    ).first()
    if row is None:
        raise ApiError(401, "token_expired")
    login, user = row
    if login.last_seen_at < now() - SEEN_EVERY:
        login.last_seen_at = now()
        await session.commit()
    return Caller(user, login)


async def logout(session: AsyncSession, who: Caller) -> None:
    who.login.revoked_at = now()
    await session.commit()


async def account(session: AsyncSession, who: Caller) -> dict:
    logins = (
        (
            await session.execute(
                select(AuthSession)
                .where(
                    AuthSession.user_id == who.user.id,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.refresh_expires_at > now(),
                )
                .order_by(AuthSession.last_seen_at.desc())
            )
        )
        .scalars()
        .all()
    )
    devices, seen = [], set()
    for login in logins:
        if login.device_id in seen:
            continue
        seen.add(login.device_id)
        devices.append(
            {
                "deviceId": login.device_id,
                "deviceName": login.device_name,
                "platform": login.platform,
                "lastSeenAt": iso(login.last_seen_at),
                "current": login.id == who.login.id,
            }
        )
    return {**user_out(who.user), "devices": devices}


async def sign_out_device(session: AsyncSession, who: Caller, device_id: str) -> None:
    done = await session.execute(
        update(AuthSession)
        .where(
            AuthSession.user_id == who.user.id,
            AuthSession.device_id == device_id,
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=now())
    )
    if not done.rowcount:
        raise ApiError(404, "device_not_found")
    await session.commit()


async def delete_account(session: AsyncSession, who: Caller) -> None:
    """The user and everything of theirs, at once: login sessions, sync records and chats go
    with the user row (ON DELETE CASCADE); the sign-in codes and the chatbot's request log
    (the questions and answers kept for analysis) are removed too."""
    from sqlalchemy import text

    await session.execute(
        text(
            "DELETE FROM chat_requests WHERE who = :who OR conversation_id IN"
            " (SELECT id FROM chat_conversations WHERE user_id = :user)"
        ),
        {"who": f"u:{who.user.id}", "user": who.user.id},
    )
    await session.execute(delete(EmailCode).where(EmailCode.email == who.user.email))
    await session.execute(delete(User).where(User.id == who.user.id))
    await session.commit()


async def cleanup(session: AsyncSession) -> None:
    """Daily: old codes and verify calls (a day), the email send log (90 days), and login
    sessions long ended or expired (30 days)."""
    from sqlalchemy import text

    await session.execute(
        text("DELETE FROM email_codes WHERE created_at < now() - interval '1 day'")
    )
    await session.execute(
        text("DELETE FROM verify_calls WHERE created_at < now() - interval '1 day'")
    )
    await session.execute(
        text("DELETE FROM email_sends WHERE created_at < now() - interval '90 days'")
    )
    await session.execute(
        text(
            "DELETE FROM auth_sessions"
            " WHERE coalesce(revoked_at, refresh_expires_at) < now() - interval '30 days'"
        )
    )
    await session.commit()
