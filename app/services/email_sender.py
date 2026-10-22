"""Sending the sign-in code by email, through a free provider plan, with failover.

Providers (EMAIL_PROVIDERS, tried in order): Brevo's transactional API, Resend, and
"console", which only writes the code to the server log (development and testing). Each
send attempt is logged in email_sends with a hash of the address, never the code.

The email has no links and no images (a code-only email reads less like phishing): the
code is in the subject, so it shows in the notification, and large in an RTL body.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.account import EmailSend

log = logging.getLogger(__name__)


class EmailQuotaExceeded(Exception):
    """The provider refused for its plan's limit."""


class EmailSendFailed(Exception):
    """The provider failed or refused for another reason."""


class EmailUnavailable(Exception):
    """No provider could send the email."""


class EmailSender(Protocol):
    name: str

    async def send_code(self, to: str, code: str, language: str) -> None: ...


# ── the message ─────────────────────────────────────────────────────────────────────

TEXTS = {
    "ar": {
        "subject": "رمز الدخول إلى مكتبة الثقلين: {code}",
        "intro": "رمز الدخول إلى مكتبة الثقلين:",
        "valid": "الرمز صالح لمدة 10 دقائق.",
        "ignore": "إذا لم تطلب هذا الرمز فتجاهل هذه الرسالة.",
        "dir": "rtl",
    },
    "fa": {
        "subject": "کد ورود به کتابخانه ثقلین: {code}",
        "intro": "کد ورود به کتابخانه ثقلین:",
        "valid": "این کد به مدت ۱۰ دقیقه معتبر است.",
        "ignore": "اگر این کد را درخواست نکرده‌اید، این پیام را نادیده بگیرید.",
        "dir": "rtl",
    },
    "en": {
        "subject": "Your Thaqalayn Library sign-in code: {code}",
        "intro": "Your Thaqalayn Library sign-in code:",
        "valid": "The code is valid for 10 minutes.",
        "ignore": "If you didn't request this code, ignore this email.",
        "dir": "ltr",
    },
}


def compose(code: str, language: str) -> tuple[str, str, str]:
    """(subject, plain text, HTML) of the code email."""
    t = TEXTS.get(language, TEXTS["ar"])
    subject = t["subject"].format(code=code)
    plain = f"{t['intro']}\n\n{code}\n\n{t['valid']}\n{t['ignore']}\n"
    html = (
        f'<div dir="{t["dir"]}" '
        'style="font-family:Tahoma,Arial,sans-serif;font-size:16px;color:#222">'
        f"<p>{t['intro']}</p>"
        f'<p style="font-size:34px;font-weight:bold;letter-spacing:6px;margin:16px 0" dir="ltr">'
        f"{code}</p>"
        f'<p>{t["valid"]}</p><p style="color:#777">{t["ignore"]}</p></div>'
    )
    return subject, plain, html


def parse_from(value: str) -> tuple[str, str]:
    """("name", "address") from 'Name <address>' (or a bare address)."""
    m = re.match(r"\s*(.*?)\s*<([^>]+)>\s*$", value)
    return (m.group(1), m.group(2)) if m else ("", value.strip())


def email_hash(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()


# ── providers ───────────────────────────────────────────────────────────────────────


def _post(url: str, body: dict, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"content-type": "application/json", "accept": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, f"{type(e).__name__}: {e}"


def _raise_for(provider: str, status: int, body: str) -> None:
    if 200 <= status < 300:
        return
    if status in (402, 429):
        raise EmailQuotaExceeded(f"{provider} {status}: {body[:200]}")
    raise EmailSendFailed(f"{provider} {status}: {body[:200]}")


class BrevoSender:
    name = "brevo"
    url = "https://api.brevo.com/v3/smtp/email"

    def __init__(self, api_key: str, sender: str):
        self.api_key, (self.from_name, self.from_email) = api_key, parse_from(sender)

    async def send_code(self, to: str, code: str, language: str) -> None:
        subject, plain, html = compose(code, language)
        body = {
            "sender": {"name": self.from_name, "email": self.from_email},
            "to": [{"email": to}],
            "subject": subject,
            "textContent": plain,
            "htmlContent": html,
        }
        status, text_ = await asyncio.to_thread(_post, self.url, body, {"api-key": self.api_key})
        _raise_for(self.name, status, text_)


class ResendSender:
    name = "resend"
    url = "https://api.resend.com/emails"

    def __init__(self, api_key: str, sender: str):
        self.api_key, self.sender = api_key, sender

    async def send_code(self, to: str, code: str, language: str) -> None:
        subject, plain, html = compose(code, language)
        body = {"from": self.sender, "to": [to], "subject": subject, "text": plain, "html": html}
        status, text_ = await asyncio.to_thread(
            _post, self.url, body, {"authorization": f"Bearer {self.api_key}"}
        )
        _raise_for(self.name, status, text_)


class ConsoleSender:
    """Writes the code to the server log instead of sending it: development and testing only."""

    name = "console"

    async def send_code(self, to: str, code: str, language: str) -> None:
        log.warning("sign-in code for %s: %s (console email sender: no email was sent)", to, code)


def senders(settings: Settings) -> list[EmailSender]:
    """The configured providers in order; one without its key is left out."""
    out: list[EmailSender] = []
    for name in (n.strip().lower() for n in settings.email_providers.split(",")):
        if name == "brevo" and settings.brevo_api_key:
            out.append(BrevoSender(settings.brevo_api_key, settings.email_from))
        elif name == "resend" and settings.resend_api_key:
            out.append(ResendSender(settings.resend_api_key, settings.email_from))
        elif name == "console":
            out.append(ConsoleSender())
    return out


async def send_code(
    session: AsyncSession,
    settings: Settings,
    to: str,
    code: str,
    language: str,
    providers: list[EmailSender] | None = None,
) -> str:
    """Sends through the first provider that works; returns its name. Every attempt is
    logged. Raises EmailUnavailable when none could send."""
    tried = 0
    for sender in providers if providers is not None else senders(settings):
        tried += 1
        try:
            await sender.send_code(to, code, language)
        except (EmailQuotaExceeded, EmailSendFailed) as exc:
            session.add(
                EmailSend(
                    provider=sender.name, email_hash=email_hash(to), ok=False, error=str(exc)[:500]
                )
            )
            log.warning("email via %s failed: %s", sender.name, exc)
            continue
        session.add(EmailSend(provider=sender.name, email_hash=email_hash(to), ok=True))
        return sender.name
    raise EmailUnavailable("no email provider configured" if not tried else "every provider failed")
