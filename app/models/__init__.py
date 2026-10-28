"""ORM models. Importing this package registers everything on Base.metadata, which is
what Alembic's autogenerate diffs against."""

from app.models.account import (
    AuthSession,
    EmailCode,
    EmailSend,
    SyncRecord,
    UsedRefreshToken,
    User,
    VerifyCall,
)
from app.models.chat import ChatConversation, ChatMessage, ChatRequest
from app.models.push import PushBookVersion, PushDevice, PushOutbox, PushPreference
from app.models.library import (
    Author,
    Book,
    ImportLog,
    Language,
    Library,
    LibraryWork,
    Page,
    Section,
    ShamelaCollection,
    Subject,
    Work,
    WorkSubject,
)

__all__ = [
    "AuthSession",
    "EmailCode",
    "EmailSend",
    "SyncRecord",
    "UsedRefreshToken",
    "User",
    "VerifyCall",
    "Author",
    "Book",
    "ChatConversation",
    "ChatMessage",
    "ChatRequest",
    "PushBookVersion",
    "PushDevice",
    "PushOutbox",
    "PushPreference",
    "ImportLog",
    "Language",
    "Library",
    "LibraryWork",
    "Page",
    "Section",
    "ShamelaCollection",
    "Subject",
    "Work",
    "WorkSubject",
]
