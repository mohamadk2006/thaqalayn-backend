#!/usr/bin/env python3
"""Validate a v2-converted BookContent JSON before it is imported.

Validation is separate from conversion on purpose: conversion is about parsing a messy
source format, validation is about guaranteeing the invariants the importer relies on. A
book that converts without error can still be unfit to import — no title, empty content,
a broken page sequence — and the importer must be able to reject it by inspecting the JSON
alone, without re-reading the source.

Returns a list of Issues. Severity 'error' means do not import; 'warning' means import but
flag. The importer treats the two differently; this module only decides which is which.

v1's validate_book.py checked the chapters/sections/paragraphs shape and is kept as-is for
any v1 content still on disk; this is its v2 counterpart, checking pages/blocks/toc.
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
    if content.get("schemaVersion") != 2:
        error("schema_version", f"expected schemaVersion 2, got {content.get('schemaVersion')!r}")
    if not str(content.get("bookId", "")).strip():
        error("missing_book_id", "bookId is empty")
    if not content.get("title", "").strip():
        error("missing_title", "title is empty")
    if not content.get("author", "").strip():
        warn("missing_author", "author is empty")  # some genuine sources lack an author

    pages = content.get("pages")
    if not pages:
        error("no_pages", "book has no pages")
        return issues  # nothing further to check

    toc = content.get("toc", [])

    # ── Structural integrity: sequence/id correctness, block ordering ─────────────
    seen_page_ids: set[str] = set()
    seen_block_ids: set[str] = set()
    seen_toc_ids: set[str] = set()
    front_counter = 0
    empty_blocks = 0
    non_nfc = 0
    total_blocks = 0
    main_printed_numbers: list[int] = []

    for i, page in enumerate(sorted(pages, key=lambda p: p.get("sequence", 0)), start=1):
        if page.get("sequence") != i:
            error("page_sequence_gap", f"expected sequence {i}, got {page.get('sequence')!r}")
        expected_id = f"p-{i:06d}"
        if page.get("id") != expected_id:
            error("page_id_mismatch", f"expected {expected_id}, got {page.get('id')!r}")
        if page.get("id") in seen_page_ids:
            error("duplicate_page_id", f"duplicate page id {page.get('id')!r}")
        seen_page_ids.add(page.get("id"))

        page_type = page.get("pageType")
        if page_type not in ("frontMatter", "main"):
            error("bad_page_type", f"page {page.get('id')} has pageType {page_type!r}")

        page_number = page.get("pageNumber")
        if not isinstance(page_number, str) or not page_number:
            error("bad_page_number", f"page {page.get('id')} has pageNumber {page_number!r}")
        elif page_type == "frontMatter":
            front_counter += 1
            expected_number = f"0.{front_counter}"
            if page_number != expected_number:
                error(
                    "front_matter_numbering",
                    f"page {page.get('id')} pageNumber {page_number!r} != "
                    f"expected {expected_number!r}",
                )
        elif page_type == "main" and page_number.isdigit():
            main_printed_numbers.append(int(page_number))

        for j, block in enumerate(
            sorted(page.get("blocks", []), key=lambda b: b.get("order", 0)), start=1
        ):
            total_blocks += 1
            if block.get("order") != j:
                error("block_order_gap", f"page {page.get('id')} block order gap at {j}")
            expected_bid = f"{page['id']}-b-{j:03d}"
            if block.get("id") != expected_bid:
                error(
                    "block_id_mismatch",
                    f"expected {expected_bid}, got {block.get('id')!r}",
                )
            if block.get("id") in seen_block_ids:
                error("duplicate_block_id", f"duplicate block id {block.get('id')!r}")
            seen_block_ids.add(block.get("id"))

            if block.get("type") not in ("text", "heading", "footnotes"):
                error("bad_block_type", f"block {block.get('id')} has type {block.get('type')!r}")
            if block.get("type") == "heading" and "tocId" not in block:
                error("heading_missing_toc_id", f"heading block {block.get('id')} has no tocId")

            text = block.get("text", "")
            if not text.strip():
                empty_blocks += 1
            elif unicodedata.normalize("NFC", text) != text:
                non_nfc += 1

    # ── TOC <-> page consistency ────────────────────────────────────────────────
    for k, entry in enumerate(sorted(toc, key=lambda e: e.get("order", 0)), start=1):
        if entry.get("order") != k:
            error("toc_order_gap", f"toc order gap at index {k}")
        if entry.get("id") in seen_toc_ids:
            error("duplicate_toc_id", f"duplicate toc id {entry.get('id')!r}")
        seen_toc_ids.add(entry.get("id"))
        if entry.get("pageId") not in seen_page_ids:
            error(
                "toc_dangling_page_ref",
                f"toc {entry.get('id')} references missing pageId {entry.get('pageId')!r}",
            )

    heading_toc_ids = {
        b["tocId"]
        for p in pages
        for b in p.get("blocks", [])
        if b.get("type") == "heading" and "tocId" in b
    }
    toc_ids = {e.get("id") for e in toc}
    if heading_toc_ids != toc_ids:
        error(
            "toc_heading_mismatch",
            f"heading tocIds and toc entries disagree: missing from toc="
            f"{heading_toc_ids - toc_ids}, orphaned toc entries={toc_ids - heading_toc_ids}",
        )

    # ── Content-quality signals (warnings, not blockers) ───────────────────────────
    if total_blocks == 0:
        error("no_content", "book has no blocks")
    if empty_blocks:
        warn("empty_blocks", f"{empty_blocks} empty block(s)")
    if non_nfc:
        # Not an error: the importer's normalizer composes to NFC anyway. But a high count
        # is worth surfacing, mirroring v1's same check.
        warn("non_nfc_text", f"{non_nfc} block(s) not in NFC form")

    # Printed main-page numbers should be non-decreasing in reading order. A decrease
    # usually means a mis-parsed < صفحة > marker or a genuine multi-volume pagination
    # reset our classifier doesn't model — either way, worth a human glance, not a reject.
    descents = sum(
        1 for a, b in zip(main_printed_numbers, main_printed_numbers[1:], strict=False)
        if b < a
    )
    if descents:
        warn("non_monotonic_pages", f"printed page number decreases {descents} time(s)")

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
