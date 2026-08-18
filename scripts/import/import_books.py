#!/usr/bin/env python3
"""Import converted books into PostgreSQL.

Ties together every earlier piece: the converter reads a source .abx, the validator gates
it, paginate() shapes its pages, and the collection map (already in the database) supplies
the taxonomy. Designed for a 18,798-file run, so three properties are non-negotiable:

  **Restartable + idempotent.** Each book's content is hashed (content_sha256). A re-run
  skips any book whose source hasn't changed, so an interrupted run resumes cheaply and a
  completed run is a no-op. Changed books are re-imported in place.

  **Fault-isolated.** One malformed book must never stop the run. Every outcome — ok,
  skipped, failed — is recorded in import_log with the stage it reached, and processing
  continues. The run's exit code reflects whether anything failed, but a failure is data,
  not a crash.

  **Memory-bounded.** Books are processed one at a time and committed per book, so peak
  memory is one book, not the library, and a crash loses at most the book in flight.

Usage:
    python scripts/import/import_books.py <source-dir> [--limit N] [--ids 1,2,3]
                                          [--books-root data/books] [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "convert"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "validate"))

import shamela_to_json as conv  # noqa: E402
import validate_book as val  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import dispose_engine, get_sessionmaker  # noqa: E402
from app.services.arabic import normalize  # noqa: E402
from app.services.paging import paginate, paginate_sections  # noqa: E402


def _hijri_year(death_label: str) -> int | None:
    digits = "".join(ch for ch in death_label if ch.isdigit())
    return int(digits) if digits else None


async def _get_or_create_author(session: AsyncSession, name: str, death: str | None) -> int | None:
    if not name.strip():
        return None
    return await session.scalar(
        text("""
            INSERT INTO authors (name, name_norm, death_label, death_year_hijri)
            VALUES (:name, :norm, :death, :year)
            ON CONFLICT (name_norm, death_label) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
        """),
        {"name": name, "norm": normalize(name), "death": death or None,
         "year": _hijri_year(death or "")},
    )


async def _get_or_create_work(
    session: AsyncSession, title: str, author_id: int | None, collection: dict | None,
    language: str,
) -> int:
    """Group volumes into a work by (normalized title, author). The collection row
    already carries the taxonomy, so a work inherits subject/tradition/etc. from the
    first volume that names its collection.

    `language` is the *book's own* resolved language (body marker, falling back to the
    collection hint only if that's absent — see the caller) — not the collection hint
    alone. The collection hint is populated on only 2 of 530 collections, so relying on
    it exclusively would leave nearly every work's language NULL. ON CONFLICT updates it
    too: a work's volumes should agree on language, and the most recently imported volume
    is as good a source of truth as any for that.
    """
    return await session.scalar(
        text("""
            INSERT INTO works (title, title_norm, author_id, subject_id, tradition,
                               madhhab, format, language_code)
            VALUES (:title, :norm, :author, :subject, cast(:tradition as tradition),
                    :madhhab, cast(:format as book_format), :lang)
            ON CONFLICT (title_norm, author_id) DO UPDATE SET
                title = EXCLUDED.title, language_code = EXCLUDED.language_code
            RETURNING id
        """),
        {
            "title": title, "norm": normalize(title), "author": author_id,
            "subject": (collection or {}).get("subject_id"),
            "tradition": (collection or {}).get("tradition"),
            "madhhab": (collection or {}).get("madhhab"),
            "format": (collection or {}).get("format"),
            "lang": language,
        },
    )


async def _lookup_collection(session: AsyncSession, raw: str | None) -> dict | None:
    if not raw:
        return None
    row = (await session.execute(
        text("""SELECT id, subject_id, tradition::text, madhhab, format::text, language_hint
                FROM shamela_collections WHERE raw = :raw"""),
        {"raw": raw},
    )).first()
    if row is None:
        return None
    return {"id": row[0], "subject_id": row[1], "tradition": row[2],
            "madhhab": row[3], "format": row[4], "language_hint": row[5]}


async def _existing_hash(session: AsyncSession, book_id: int) -> str | None:
    return await session.scalar(
        text("SELECT content_sha256 FROM books WHERE id = :id"), {"id": book_id}
    )


async def import_one(
    session: AsyncSession, source: Path, run_id: str, books_root: Path, force: bool
) -> str:
    """Import a single .abx. Returns 'ok' | 'skipped' | 'failed'. Never raises: every
    failure is logged and swallowed so the batch continues."""
    book_id_str = source.stem
    stage = "convert"

    async def log(status: str, message: str | None, sha: str | None) -> None:
        await session.execute(
            text("""INSERT INTO import_log (run_id, book_id, source_file, status, stage,
                                            message, content_sha256)
                    VALUES (:run, :bid, :src, :status, :stage, :msg, :sha)"""),
            {"run": run_id, "bid": int(book_id_str) if book_id_str.isdigit() else None,
             "src": source.name, "status": status, "stage": stage,
             "msg": message, "sha": sha},
        )

    try:
        content, manifest = conv.convert(source, book_id_str)

        stage = "validate"
        issues = val.validate(content)
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            await log("failed", "; ".join(f"{i.code}: {i.detail}" for i in errors), None)
            await session.commit()
            return "failed"

        # Compact JSON, hashed for idempotency. The hash is over the exact bytes written to
        # disk, so "unchanged source" and "unchanged output" are the same question.
        payload = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        sha = hashlib.sha256(payload).hexdigest()

        if not force:
            stage = "idempotency"
            if await _existing_hash(session, int(book_id_str)) == sha:
                await log("skipped", "unchanged", sha)
                await session.commit()
                return "skipped"

        stage = "write_file"
        books_root.mkdir(parents=True, exist_ok=True)
        content_path = books_root / f"{book_id_str}.json"
        content_path.write_bytes(payload)

        stage = "metadata"
        md = manifest["metadata"]
        collection = await _lookup_collection(session, md.get("collection"))
        author_id = await _get_or_create_author(session, manifest["author"], md.get("death"))

        # Language: the per-file body marker is authoritative; fall back to the collection
        # hint; default Arabic. Resolved before work creation so the work (not just the
        # book) gets a language, since the collection hint alone covers almost nothing.
        lang = "fa" if "فارسي" in manifest["title"] or "فارسى" in manifest["title"] else None
        if lang is None and collection:
            lang = collection.get("language_hint")
        lang = lang or "ar"

        work_id = await _get_or_create_work(
            session, manifest["title"], author_id, collection, lang
        )

        stage = "book"
        pages = paginate(content)
        await session.execute(
            text("""
                INSERT INTO books (id, work_id, volume, title, title_norm, author_id,
                    language_code, collection_id, publisher, edition, published_year,
                    printer, editor, isbn, source_pdf, identity_notes, is_verified,
                    content_path, content_sha256, content_bytes, page_first, page_last,
                    page_count, paragraph_count, section_count, is_published, imported_at)
                VALUES (:id, :work, :vol, :title, :norm, :author, :lang, :cid, :pub, :ed,
                    :py, :pr, :editor, :isbn, :pdf, :notes, :verified, :cpath, :sha, :bytes,
                    :pf, :pl, :pc, :parc, :sc, true, :now)
                ON CONFLICT (id) DO UPDATE SET
                    work_id=EXCLUDED.work_id, volume=EXCLUDED.volume, title=EXCLUDED.title,
                    title_norm=EXCLUDED.title_norm, author_id=EXCLUDED.author_id,
                    language_code=EXCLUDED.language_code, collection_id=EXCLUDED.collection_id,
                    content_sha256=EXCLUDED.content_sha256, content_bytes=EXCLUDED.content_bytes,
                    content_path=EXCLUDED.content_path, page_first=EXCLUDED.page_first,
                    page_last=EXCLUDED.page_last, page_count=EXCLUDED.page_count,
                    paragraph_count=EXCLUDED.paragraph_count, section_count=EXCLUDED.section_count,
                    content_version=books.content_version + 1, imported_at=EXCLUDED.imported_at,
                    updated_at=now()
            """),
            {
                "id": int(book_id_str), "work": work_id, "vol": manifest["volume"],
                "title": manifest["title"], "norm": normalize(manifest["title"]),
                "author": author_id, "lang": lang,
                "cid": collection["id"] if collection else None,
                "pub": md.get("publisher"), "ed": md.get("edition"),
                "py": md.get("published_year"), "pr": md.get("printer"),
                "editor": md.get("editor"), "isbn": md.get("isbn"),
                "pdf": md.get("source_pdf"), "notes": md.get("identity_notes"),
                "verified": md.get("verified") == "1",
                # Stored relative to BOOKS_ROOT so the same row resolves on macOS and the
                # VPS; the download endpoint joins it back onto the configured root.
                "cpath": content_path.relative_to(books_root).as_posix(),
                "sha": sha, "bytes": len(payload),
                "pf": manifest["pageFirst"], "pl": manifest["pageLast"],
                "pc": manifest["pageCount"], "parc": manifest["paragraphCount"],
                "sc": manifest["sectionCount"], "now": datetime.now(UTC),
            },
        )

        stage = "sections"
        # Replace children on re-import; pages first because they reference sections.
        bid = int(book_id_str)
        await session.execute(text("DELETE FROM pages WHERE book_id = :id"), {"id": bid})
        await session.execute(text("DELETE FROM sections WHERE book_id = :id"), {"id": bid})

        ord_to_section_id: dict[int, int] = {}
        for sec in paginate_sections(content):
            section_id = await session.scalar(
                text("""INSERT INTO sections (book_id, ord, title, title_norm,
                                              page_start, page_end)
                        VALUES (:bid, :ord, :title, :norm, :ps, :pe) RETURNING id"""),
                {"bid": bid, "ord": sec.ord, "title": sec.title,
                 "norm": normalize(sec.title), "ps": sec.page_start, "pe": sec.page_end},
            )
            ord_to_section_id[sec.ord] = section_id

        stage = "pages"
        if pages:
            await session.execute(
                text("""INSERT INTO pages (book_id, page_no, text, paragraph_offsets, section_id)
                        VALUES (:bid, :pno, :text, cast(:offs as jsonb), :sid)"""),
                [{"bid": bid, "pno": p.page_no, "text": p.text,
                  "offs": json.dumps(p.paragraph_offsets, ensure_ascii=False),
                  "sid": ord_to_section_id.get(p.section_ord)} for p in pages],
            )

        await log("ok", None, sha)
        await session.commit()
        return "ok"

    except conv.ConversionError as exc:
        await session.rollback()
        await log("failed", str(exc), None)
        await session.commit()
        return "failed"
    except Exception as exc:  # noqa: BLE001 — the whole point is that no book aborts the run
        await session.rollback()
        await log("failed", f"unexpected in {stage}: {exc}", None)
        await session.commit()
        return "failed"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("--limit", type=int, help="import at most N files")
    parser.add_argument("--ids", help="comma-separated list of specific book ids")
    parser.add_argument("--books-root", type=Path, help="override BOOKS_ROOT")
    parser.add_argument("--force", action="store_true", help="re-import even if unchanged")
    parser.add_argument(
        "--min-free-gb", type=float, default=3.0,
        help="stop cleanly if free disk space on BOOKS_ROOT's filesystem drops below "
             "this many GB (default 3.0) -- a full run's storage cost is close to the "
             "estimated free space it will run against, so this is what turns an "
             "unattended out-of-disk failure into a clean, resumable stop instead of an "
             "unpredictable one partway through a write.",
    )
    args = parser.parse_args()

    books_root = args.books_root or get_settings().books_root

    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",")}
        sources = sorted(p for p in args.source_dir.glob("*.abx") if p.stem in wanted)
    else:
        sources = sorted(
            args.source_dir.glob("*.abx"),
            key=lambda p: int(p.stem) if p.stem.isdigit() else 0,
        )
    if args.limit:
        sources = sources[: args.limit]

    if not sources:
        print(f"no .abx files matched in {args.source_dir}", file=sys.stderr)
        return 1

    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    print(f"run {run_id}: {len(sources)} files → {books_root}")

    counts = {"ok": 0, "skipped": 0, "failed": 0}
    started = datetime.now(UTC)
    stopped_early = False
    sm = get_sessionmaker()
    async with sm() as session:
        for n, source in enumerate(sources, 1):
            result = await import_one(session, source, run_id, books_root, args.force)
            counts[result] += 1
            if n % 50 == 0 or n == len(sources):
                elapsed = (datetime.now(UTC) - started).total_seconds()
                print(f"  {n}/{len(sources)}  ok={counts['ok']} "
                      f"skipped={counts['skipped']} failed={counts['failed']}  "
                      f"({n / elapsed:.1f}/s)")

                # Every book so far is already committed individually, so stopping here
                # loses nothing -- a later re-run with the same source directory skips
                # every already-imported book via content_sha256 and simply continues
                # from wherever this run left off.
                usage_target = books_root if books_root.exists() else books_root.parent
                free_gb = shutil.disk_usage(usage_target).free / 1e9
                if free_gb < args.min_free_gb:
                    print(
                        f"\nstopping: only {free_gb:.1f} GB free on BOOKS_ROOT's "
                        f"filesystem (< --min-free-gb {args.min_free_gb}). "
                        f"{n}/{len(sources)} files processed so far are safely committed "
                        f"-- free up space and re-run the same command to resume."
                    )
                    stopped_early = True
                    break

    await dispose_engine()
    print(
        f"\ndone: {counts['ok']} imported, {counts['skipped']} skipped, "
        f"{counts['failed']} failed"
    )
    if stopped_early:
        return 2
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
