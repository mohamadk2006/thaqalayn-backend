#!/usr/bin/env python3
"""Validate a converted BookContent JSON before it is imported.

Validation is separate from conversion on purpose: conversion is about parsing a messy
source format, validation is about guaranteeing the invariants the importer and the iOS
reader both rely on. A book that converts without error can still be unfit to import — no
title, empty content, a broken page sequence — and the importer must be able to reject it
by inspecting the JSON alone, without re-reading the source.

Returns a list of Issues. Severity 'error' means do not import; 'warning' means import but
flag. The importer treats the two differently; this module only decides which is which.
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" | "warning"
    code: str
    detail: str


def validate(content: dict) -> list[Issue]:
    issues: list[Issue] = []

    def error(code: str, detail: str) -> None:
        issues.append(Issue("error", code, detail))

    def warn(code: str, detail: str) -> None:
        issues.append(Issue("warning", code, detail))

    # ── Identity and top-level shape ──────────────────────────────────────────────
    if content.get("schemaVersion") != 1:
        error("schema_version", f"expected schemaVersion 1, got {content.get('schemaVersion')!r}")
    if not str(content.get("bookId", "")).strip():
        error("missing_book_id", "bookId is empty")
    if not content.get("title", "").strip():
        error("missing_title", "title is empty")
    if not content.get("author", "").strip():
        warn("missing_author", "author is empty")  # some genuine sources lack an author

    chapters = content.get("chapters")
    if not chapters:
        error("no_chapters", "book has no chapters")
        return issues  # nothing further to check

    # ── Content and structure ─────────────────────────────────────────────────────
    paragraph_count = 0
    all_pages: list[int] = []
    seen_para_ids: set[str] = set()
    empty_paras = 0
    non_nfc = 0

    for chapter in chapters:
        sections = chapter.get("sections", [])
        if not sections:
            warn("empty_chapter", f"chapter {chapter.get('id')} has no sections")
        for section in sections:
            for para in section.get("paragraphs", []):
                paragraph_count += 1
                pid = para.get("id")
                if pid in seen_para_ids:
                    error("duplicate_paragraph_id", f"paragraph id repeated: {pid}")
                seen_para_ids.add(pid)

                text = para.get("text", "")
                if not text.strip():
                    empty_paras += 1
                elif unicodedata.normalize("NFC", text) != text:
                    non_nfc += 1

                page = para.get("page")
                if not isinstance(page, int) or page < 1:
                    error("bad_page_number", f"paragraph {pid} has invalid page {page!r}")
                else:
                    all_pages.append(page)

    if paragraph_count == 0:
        error("no_content", "book has no paragraphs")
    if empty_paras:
        warn("empty_paragraphs", f"{empty_paras} empty paragraph(s)")
    if non_nfc:
        # Not an error: the importer's normalizer composes to NFC anyway. But a high count
        # is worth surfacing — it was 23.6% across the corpus, and a spike is a red flag.
        warn("non_nfc_text", f"{non_nfc} paragraph(s) not in NFC form")

    # Page numbers should be non-decreasing in reading order. A decrease usually means a
    # mis-parsed < صفحة > marker, which would scatter a search result to the wrong page.
    descents = sum(1 for a, b in zip(all_pages, all_pages[1:], strict=False) if b < a)
    if descents:
        warn("non_monotonic_pages", f"page number decreases {descents} time(s) in reading order")

    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_file", type=Path)
    args = parser.parse_args()

    try:
        content = json.loads(args.json_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"✗ {args.json_file.name}: unreadable — {exc}", file=sys.stderr)
        return 2

    issues = validate(content)
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    if not issues:
        print(f"✓ {args.json_file.name}: valid")
    else:
        mark = "✗" if errors else "⚠"
        print(f"{mark} {args.json_file.name}: {len(errors)} error(s), {len(warnings)} warning(s)")
        for issue in issues:
            symbol = "✗" if issue.severity == "error" else "⚠"
            print(f"    {symbol} {issue.code}: {issue.detail}")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
