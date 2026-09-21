#!/usr/bin/env python3
"""Convert a Word file exactly as the admin "رفع ملف Word" form would, without touching
the database: prints what came out (pages, sections, validation issues, a text preview)
and writes the v2 JSON next to the .docx (or --out) so it can be inspected or edited.

Uses the admin panel's own .docx reader and the same converter/validator the importer
runs, so a clean result here means the real upload will convert the same way.

Usage:
    python scripts/convert/docx_dry_run.py book.docx --title "اسم الكتاب" [--author "..."]
                                            [--death "..."] [--out out.json] [--preview 3]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "convert"))
sys.path.insert(0, str(ROOT / "scripts" / "validate"))

import shamela_to_json_v2 as conv  # noqa: E402
import validate_book_v2 as val  # noqa: E402

from app.api.admin import _abx_source_text, _docx_body_lines  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("docx", type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--author", default="")
    parser.add_argument("--death", default="")
    parser.add_argument("--book-id", default="900001")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--preview", type=int, default=2, help="pages of text to print")
    args = parser.parse_args()

    try:
        lines = _docx_body_lines(args.docx.read_bytes())
    except Exception as exc:  # python-docx raises plain Exception on a bad/legacy file
        print(f"✗ cannot read {args.docx.name}: {exc} (must be .docx, not legacy .doc)", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"{args.book_id}.abx"
        src.write_text(_abx_source_text(args.title, args.author, args.death, lines), encoding="utf-8")
        try:
            content = conv.convert(src, args.book_id)
        except conv.ConversionError as exc:
            print(f"✗ conversion failed: {exc}", file=sys.stderr)
            return 1

    import docx as _docx

    tables = _docx.Document(str(args.docx)).tables
    table_chars = sum(len(c.text) for t in tables for r in t.rows for c in r.cells)
    if table_chars:
        print(f"⚠ WARNING: this file has {len(tables)} tables holding {table_chars} characters of text "
              "(verses, columns...). This preview and the admin Word upload SKIP tables, so that text "
              "is missing below. Use scripts/convert/doc_to_json_v2.py, which reads tables in order.\n")

    pages = content.get("pages", [])
    toc = content.get("toc", [])
    blocks = sum(len(p.get("blocks", [])) for p in pages)
    print(f"title:   {content.get('title')}")
    print(f"author:  {content.get('author')!r}")
    print(f"pages:   {len(pages)}   blocks: {blocks}   sections (headings): {len(toc)}")
    if len(pages) == 1:
        print("note:    1 page -- the file has no manual page breaks (Ctrl+Enter), so all text "
              "sits on one page.")
    if not toc:
        print("note:    no sections -- the file has no Heading/Title-styled paragraphs.")

    if toc:
        print("\nsections:")
        for e in toc[:15]:
            print(f"  {e['order']:>3}. {e['title']}  (page {e.get('pageNumber')})")
        if len(toc) > 15:
            print(f"  … {len(toc) - 15} more")

    for page in pages[: args.preview]:
        print(f"\n── page {page['pageNumber']} ({page['pageType']}) ──")
        for b in page["blocks"][:8]:
            tag = "#" if b["type"] == "heading" else " "
            print(f" {tag} {b['text'][:110]}")

    issues = val.validate(content)
    errors = [i for i in issues if i.severity == "error"]
    print(f"\nvalidation: {len(errors)} error(s), {len(issues) - len(errors)} warning(s)")
    for i in issues:
        print(f"  {'✗' if i.severity == 'error' else '⚠'} {i.code}: {i.detail}")

    out = args.out or args.docx.with_suffix(".json")
    out.write_text(json.dumps(content, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"\nwrote {out}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
