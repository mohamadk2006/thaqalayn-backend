"""ORM models. Importing this package registers everything on Base.metadata, which is
what Alembic's autogenerate diffs against."""

from app.models.chat import ChatRequest
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
    "Author",
    "Book",
    "ChatRequest",
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
