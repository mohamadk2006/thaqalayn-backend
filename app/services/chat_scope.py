"""The authors and books a question names ("what did al-Mufid say about..."), found in the
library, so the chat searches only their books.

The rewrite step names them as the user wrote them; here they are matched with the library's
authors and works. A name that matches too many (or no) authors or works is left out: a wrong
filter would hide the right answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import search_service
from app.services.arabic import normalize

MAX_AUTHORS = 6  # a name matching more authors than this is too ambiguous
MAX_WORKS = 8
# Titles in front of a name that the library may or may not carry.
TITLES = {
    normalize(w)
    for w in (
        "الشيخ",
        "السيد",
        "الإمام",
        "العلامة",
        "المحقق",
        "الميرزا",
        "الحاج",
        "الشهيد",
        "المولى",
        "آية الله",
        "الفقيه",
        "المرحوم",
        "الأستاذ",
        "الدكتور",
    )
}


@dataclass
class Scope:
    book_ids: list[int] | None = None  # None: no limit
    author_ids: list[int] = field(default_factory=list)
    work_ids: list[int] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)  # the library's names that matched
    works: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)  # asked for, but no clear match
    fallback: bool = False  # the limited search found too little, so the whole library was used

    def info(self) -> dict:
        return {
            "authors": self.authors,
            "works": self.works,
            "ignored": self.ignored,
            "books": len(self.book_ids) if self.book_ids is not None else None,
            "fallback": self.fallback,
        }


def core(name: str) -> str:
    """The name folded, without the titles in front of it."""
    words = normalize(name).split()
    while len(words) > 1 and words[0] in TITLES:
        words.pop(0)
    return " ".join(words)


async def _match(session, table: str, column: str, label: str, name: str, most: int):
    """[(id, label)] of the rows named `name`: an exact match first, else the rows holding the
    name as whole words; [] when none or more than `most`."""
    folded, bare = normalize(name), core(name)
    if len(bare) < 3:
        return []
    for pattern, exact in ((folded, True), (f"% {bare} %", False)):
        sql = (
            f"SELECT id, {label} FROM {table} WHERE {column} = :p LIMIT {most + 1}"
            if exact
            else f"SELECT id, {label} FROM {table} WHERE ' ' || {column} || ' ' LIKE :p"
            f" LIMIT {most + 1}"
        )
        rows = (await session.execute(text(sql), {"p": pattern})).all()
        if rows:
            return [tuple(r) for r in rows] if len(rows) <= most else []
    return []


async def resolve(session: AsyncSession, authors: list[str], books: list[str]) -> Scope:
    scope = Scope()
    for name in authors:
        rows = await _match(session, "authors", "name_norm", "name", name, MAX_AUTHORS)
        if rows:
            scope.author_ids += [r[0] for r in rows]
            scope.authors += [r[1] for r in rows]
        else:
            scope.ignored.append(name)
    for title in books:
        rows = await _match(session, "works", "title_norm", "title", title, MAX_WORKS)
        if rows:
            scope.work_ids += [r[0] for r in rows]
            scope.works += [r[1] for r in rows]
        else:
            scope.ignored.append(title)
    if scope.author_ids or scope.work_ids:
        scope.book_ids = await search_service.filtered_book_ids(
            session,
            author_ids=sorted(set(scope.author_ids)) or None,
            work_ids=sorted(set(scope.work_ids)) or None,
        )
    return scope
