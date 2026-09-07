"""Shape a v2-converted book's pages/toc into the search-index rows the schema stores.

The one piece of import logic worth isolating and testing on its own: it turns the
page/block/toc BookContent structure into the `pages`/`sections` rows the schema stores,
and it is where the block->page offset mapping that enables post-download deep-linking is
built. Both the importer and its tests use this, so they can never drift apart.

`page_text_and_offsets()` is also reused by search_service.py: `pages` doesn't store the
page's own text (see Page's docstring in app/models/library.py), so a search hit
reconstructs it, on demand, straight from the book's own JSON file -- the identical
block-join this module uses at import time to build the value search_tsv is computed
from. The two must never drift apart either, or a search match's snippet could disagree
with what the match was actually found in.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass


def page_text_and_offsets(page: dict) -> tuple[str, list[dict]]:
    """A page's blocks (text, heading, and footnote text alike — a reader searching for a
    phrase that only appears in a footnote citation should still find the page), joined
    with '\\n' in reading order, plus each block's character offset within that joined
    text (for resolving a search hit back to an exact block after download)."""
    text = ""
    offsets: list[dict] = []
    for block in sorted(page.get("blocks", []), key=lambda b: b.get("order", 0)):
        block_text = block.get("text", "")
        start = len(text) + (1 if text else 0)  # account for the joining '\n'
        offsets.append({"id": block["id"], "start": start})
        text = f"{text}\n{block_text}" if text else block_text
    return text, offsets


@dataclass
class PageRow:
    sequence: int
    page_number: str
    page_type: str
    is_blank: bool
    text: str  # used to compute search_tsv at import time; not itself persisted
    block_offsets: list[dict]  # [{"id": str, "start": int}], char offset within text
    section_ord: int | None    # 1-based ordinal of the section this page opens in


@dataclass
class SectionRow:
    ord: int
    title: str
    page_start_sequence: int | None
    page_end_sequence: int | None


def paginate_sections(content: dict) -> list[SectionRow]:
    """Section rows with the page-sequence range each spans, for the `sections` table.

    A section's range runs from its own page's sequence to the sequence right before the
    next TOC entry's page (or the book's last page, for the final entry). Two TOC entries
    can share a page (sequence range of length zero) — sections index the text, they do
    not partition it, same as v1.
    """
    toc = sorted(content.get("toc", []), key=lambda e: e.get("order", 0))
    if not toc:
        return []

    page_sequence = {page["id"]: page["sequence"] for page in content.get("pages", [])}
    last_sequence = max(page_sequence.values(), default=None)

    rows: list[SectionRow] = []
    for i, entry in enumerate(toc):
        start = page_sequence.get(entry["pageId"])
        if i + 1 < len(toc):
            next_start = page_sequence.get(toc[i + 1]["pageId"])
            end = (next_start - 1) if (next_start is not None and start is not None) else start
        else:
            end = last_sequence
        rows.append(SectionRow(
            ord=i + 1, title=entry.get("title", ""),
            page_start_sequence=start, page_end_sequence=end,
        ))
    return rows


def paginate(content: dict) -> list[PageRow]:
    """One row per v2 page, in reading order.

    A page's blocks (text, heading, and footnote text alike — a reader searching for a
    phrase that only appears in a footnote citation should still find the page) are joined
    with '\\n', and each block's character offset within the joined text is recorded so a
    search hit can later resolve back to an exact block after download. A page is tagged
    with the section active as of its own sequence — the count of TOC entries at or before
    it — which is what a search result needs to show "which chapter" without storing a
    section per block. Using a count rather than a boolean "does this page open a section"
    flag matters when a page carries more than one heading: each TOC entry still gets its
    own ordinal in paginate_sections, and a page's section_ord must land on the *last* of
    those ordinals to point at the section actually covering the page, not just whichever
    one happened to be first.
    """
    rows: list[PageRow] = []
    page_sequence = {p["id"]: p["sequence"] for p in content.get("pages", [])}
    toc = sorted(content.get("toc", []), key=lambda e: e.get("order", 0))
    # Non-decreasing by construction: the converter appends a heading to toc in the same
    # document-order pass that assigns page sequences, so an earlier toc entry can never
    # point at a later page than one that follows it.
    toc_page_sequences = [
        page_sequence[e["pageId"]] for e in toc if e["pageId"] in page_sequence
    ]

    for page in sorted(content.get("pages", []), key=lambda p: p["sequence"]):
        section_ord = bisect.bisect_right(toc_page_sequences, page["sequence"])
        text, offsets = page_text_and_offsets(page)

        rows.append(PageRow(
            sequence=page["sequence"],
            page_number=page["pageNumber"],
            page_type=page["pageType"],
            is_blank=page.get("isBlank", False),
            text=text,
            block_offsets=offsets,
            section_ord=section_ord if section_ord else None,
        ))

    return rows
