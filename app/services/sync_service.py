"""The sync store: pull by cursor, push with the merge rules (docs/accounts_sync_spec.md §4-§6).

Records are opaque JSON under (user, kind, key); the server reads only `updatedAt`,
`deleted`, the order fields and, for notes, the title and text. Every write takes a new
`seq` from the sync_seq sequence, so a pull of "everything after my cursor" is one index
range. Tombstones (deleted: true) are kept forever.

Merge, per pushed item against the stored record (the pure part is `merge`):
  * the newer `updatedAt` wins (a tie goes to the larger deviceId); a delete is a write;
  * for notes, folders, sessions and session books the manual order (sortIndex/position)
    is merged separately, by `orderChangedAt`;
  * a note edited without having seen the latest version keeps the losing text as a copy.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import SyncRecord
from app.services.auth_service import iso, now

KINDS = (
    "note",
    "noteFolder",
    "session",
    "sessionBook",
    "highlight",
    "numberedMark",
    "bookmark",
    "readingProgress",
    "sessionProgress",
)
ORDER_FIELD = {
    "note": "sortIndex",
    "noteFolder": "sortIndex",
    "session": "sortIndex",
    "sessionBook": "position",
}
MAX_ITEMS = 500
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_DATA_BYTES = 256 * 1024
FUTURE_SLACK = dt.timedelta(minutes=5)
EARLIEST = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
COPY_SUFFIX = " (نسخة محفوظة)"


def parse_time(value) -> dt.datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("not a time")
    t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


@dataclass
class Version:
    """One side of a merge: what is stored, or what was pushed."""

    data: dict | None
    deleted: bool
    updated_at: dt.datetime
    order_changed_at: dt.datetime | None
    device_id: str


@dataclass
class Merged:
    status: str  # applied | older_ignored | applied_with_copy | older_ignored_with_copy
    write: Version | None  # the record to store, None when nothing changes
    copy: dict | None = None  # data of the losing note's copy
    extra: dict = field(default_factory=dict)


def _newer(a_time, a_device, b_time, b_device) -> bool:
    """Is a newer than b? None is older than any time; a tie goes to the larger deviceId."""
    if a_time is None:
        return False
    if b_time is None:
        return True
    return a_time > b_time or (a_time == b_time and a_device > b_device)


def _content(v: Version) -> tuple:
    d = v.data or {}
    return (d.get("title") or "", d.get("bodyText") or "")


def merge(
    kind: str,
    stored: Version | None,
    stored_seq: int | None,
    incoming: Version,
    base_seq: int | None,
) -> Merged:
    """The merge of one pushed item with the stored record of the same (user, kind, key)."""
    if stored is None:
        return Merged("applied", incoming)

    content_wins = _newer(
        incoming.updated_at, incoming.device_id, stored.updated_at, stored.device_id
    )
    order_field = ORDER_FIELD.get(kind)
    order_wins = bool(order_field) and _newer(
        incoming.order_changed_at, incoming.device_id, stored.order_changed_at, stored.device_id
    )

    # a note edited on a device that had not seen the latest version: keep the losing text
    copy = None
    if (
        kind == "note"
        and not stored.deleted
        and not incoming.deleted
        and stored_seq is not None
        and stored_seq > (base_seq or 0)
        and _content(stored) != _content(incoming)
    ):
        loser = stored if content_wins else incoming
        copy = dict(loser.data or {})

    if not content_wins and not order_wins:
        return Merged("older_ignored_with_copy" if copy else "older_ignored", None, copy)

    winner = incoming if content_wins else stored
    data = dict(winner.data) if winner.data is not None else None
    order_side = incoming if order_wins else stored
    if order_field and data is not None and not winner.deleted:
        side_data = order_side.data or {}
        if order_field in side_data:
            data[order_field] = side_data[order_field]
        elif order_wins:
            data.pop(order_field, None)
    write = Version(
        data=None if winner.deleted else data,
        deleted=winner.deleted,
        updated_at=winner.updated_at,
        order_changed_at=order_side.order_changed_at if order_field else winner.order_changed_at,
        device_id=incoming.device_id if (content_wins or order_wins) else stored.device_id,
    )
    if copy and not content_wins:
        return Merged("older_ignored_with_copy", write, copy)  # only the order was applied
    return Merged("applied_with_copy" if copy else "applied", write, copy)


def copy_of(original_key: str, data: dict) -> dict:
    """The losing note's text kept as a new note, beside the original."""
    out = dict(data)
    out["title"] = (out.get("title") or "") + COPY_SUFFIX
    out["copyOf"] = original_key
    return out


# ── pull ────────────────────────────────────────────────────────────────────────────


def _item(r: SyncRecord) -> dict:
    return {
        "kind": r.kind,
        "key": r.key,
        "deleted": r.deleted,
        "updatedAt": iso(r.updated_at),
        "orderChangedAt": iso(r.order_changed_at),
        "seq": r.seq,
        "data": None if r.deleted else r.data,
    }


async def pull(session: AsyncSession, user_id: uuid.UUID, since: int, limit: int) -> dict:
    rows = (
        (
            await session.execute(
                select(SyncRecord)
                .where(SyncRecord.user_id == user_id, SyncRecord.seq > since)
                .order_by(SyncRecord.seq)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    page = rows[:limit]
    return {
        "cursor": page[-1].seq if page else since,
        "hasMore": len(rows) > limit,
        "items": [_item(r) for r in page],
    }


# ── push ────────────────────────────────────────────────────────────────────────────


async def _next_seq(session: AsyncSession) -> int:
    return (await session.execute(text("SELECT nextval('sync_seq')"))).scalar_one()


def _result(kind: str, key: str, status: str, seq: int | None = None, **extra) -> dict:
    out = {"kind": kind, "key": key, "status": status}
    if seq is not None:
        out["seq"] = seq
    return {**out, **extra}


async def push(
    session: AsyncSession, user_id: uuid.UUID, device_id: str, items: list[dict]
) -> list[dict]:
    """Applies the items in one transaction; one result per item, in order."""
    # one push at a time per user: the merges read and write the same records
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:u))"), {"u": str(user_id)})
    results = []
    for item in items:
        kind, key = item.get("kind"), item.get("key")
        if kind not in KINDS:
            results.append(_result(str(kind), str(key), "rejected_kind"))
            continue
        if not isinstance(key, str) or not key or len(key) > 512:
            results.append(_result(kind, str(key), "rejected_key"))
            continue
        data = item.get("data")
        deleted = bool(item.get("deleted"))
        if (data is not None and not isinstance(data, dict)) or (
            data is not None and len(json.dumps(data, ensure_ascii=False).encode()) > MAX_DATA_BYTES
        ):
            results.append(_result(kind, key, "rejected_size"))
            continue
        try:
            updated_at = parse_time(item.get("updatedAt"))
            order_changed_at = parse_time(item.get("orderChangedAt"))
        except ValueError:
            updated_at = None
        if updated_at is None or updated_at < EARLIEST:
            results.append(_result(kind, key, "rejected_time"))
            continue
        limit = now() + FUTURE_SLACK
        updated_at = min(updated_at, now()) if updated_at > limit else updated_at
        if order_changed_at is not None and order_changed_at > limit:
            order_changed_at = now()
        if not deleted and data is None:
            data = {}
        incoming = Version(
            None if deleted else data, deleted, updated_at, order_changed_at, device_id
        )

        row = (
            await session.execute(
                select(SyncRecord).where(
                    SyncRecord.user_id == user_id,
                    SyncRecord.kind == kind,
                    SyncRecord.key_lower == key.lower(),
                )
            )
        ).scalar()
        stored = (
            None
            if row is None
            else Version(
                row.data, row.deleted, row.updated_at, row.order_changed_at, row.device_id or ""
            )
        )
        base_seq = item.get("baseSeq")
        merged = merge(
            kind,
            stored,
            row.seq if row else None,
            incoming,
            base_seq if isinstance(base_seq, int) else None,
        )

        seq = row.seq if row else None
        if merged.write is not None:
            seq = await _next_seq(session)
            w = merged.write
            if row is None:
                session.add(
                    SyncRecord(
                        user_id=user_id,
                        kind=kind,
                        key_lower=key.lower(),
                        key=key,
                        data=w.data,
                        deleted=w.deleted,
                        updated_at=w.updated_at,
                        order_changed_at=w.order_changed_at,
                        seq=seq,
                        device_id=w.device_id,
                    )
                )
            else:
                row.data, row.deleted, row.updated_at = w.data, w.deleted, w.updated_at
                row.order_changed_at, row.seq, row.device_id = w.order_changed_at, seq, w.device_id
        extra = {}
        if merged.copy is not None:
            copy_key = str(uuid.uuid4()).upper()
            session.add(
                SyncRecord(
                    user_id=user_id,
                    kind="note",
                    key_lower=copy_key.lower(),
                    key=copy_key,
                    data=copy_of(row.key if row else key, merged.copy),
                    deleted=False,
                    updated_at=now(),
                    order_changed_at=None,
                    seq=await _next_seq(session),
                    device_id=device_id,
                )
            )
            extra["copyKey"] = copy_key
        await session.flush()
        results.append(_result(kind, key, merged.status, seq, **extra))
    await session.commit()
    return results
