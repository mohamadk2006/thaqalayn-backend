"""The account and sync endpoints' error format: a stable machine `code` and a human
`detail` the app may show, plus any extra fields (docs/accounts_sync_spec.md §7).

    {"code": "code_invalid", "detail": "الرمز غير صحيح.", "attemptsLeft": 3}
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

DETAILS = {
    "code_invalid": "الرمز غير صحيح.",
    "code_expired": "انتهت صلاحية الرمز، اطلب رمزاً جديداً.",
    "code_locked": "تجاوزت عدد المحاولات، اطلب رمزاً جديداً.",
    "token_expired": "انتهت الجلسة.",
    "refresh_invalid": "سجّل الدخول من جديد.",
    "payload_too_large": "البيانات المرسلة أكبر من المسموح.",
    "email_invalid": "عنوان البريد الإلكتروني غير صحيح.",
    "rate_limited": "محاولات كثيرة، حاول لاحقاً.",
    "email_unavailable": "تعذّر إرسال البريد الآن، حاول بعد قليل.",
    "auth_unavailable": "تسجيل الدخول غير مفعّل على هذا الخادم.",
    "device_not_found": "هذا الجهاز غير موجود في الحساب.",
    "conversation_not_found": "هذه المحادثة غير موجودة.",
    "conversation_full": "المحادثة طويلة، ابدأ محادثة جديدة.",
    "too_many_conversations": "لديك محادثات كثيرة، احذف بعضها أولاً.",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, *, retry_after: int | None = None, **extra):
        self.status, self.code, self.retry_after, self.extra = status, code, retry_after, extra


def register(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        if exc.status == 401:
            headers = {**(headers or {}), "WWW-Authenticate": "Bearer"}
        body = {"code": exc.code, "detail": DETAILS.get(exc.code, exc.code), **exc.extra}
        return JSONResponse(body, status_code=exc.status, headers=headers)
