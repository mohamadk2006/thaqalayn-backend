"""Group a converted book's paragraphs into pages.

The one piece of import logic worth isolating and testing on its own: it turns the
paragraph-level BookContent structure into the page-level rows the schema stores, and it
is where the paragraph→page offset mapping that enables post-download deep-linking is
built. Both the importer and its tests use this, so they can never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PageRow:
    page_no: int
    text: str
    paragraph_offsets: list[dict]  # [{"id": str, "start": int}], char offset within text
    section_ord: int | None        # 1-based ordinal of the section this page opens in


@dataclass
class SectionRow:
    ord: int
    title: str
    page_start: int | None
    page_end: int | None


def paginate_sections(content: dict) -> list[SectionRow]:
    """Section rows with the page range each spans, for the sections table.

    A section's range is the first and last printed page of its paragraphs. Ranges can
    overlap between consecutive sections (two headings can share a page), which is
    expected — sections index the text, they do not partition it.
    """
    rows: list[SectionRow] = []
    ordinal = 0
    for chapter in sorted(content.get("chapters", []), key=lambda c: c.get("order", 0)):
        for section in sorted(chapter.get("sections", []), key=lambda s: s.get("order", 0)):
            ordinal += 1
            pages = [p["page"] for p in section.get("paragraphs", [])]
            rows.append(SectionRow(
                ord=ordinal,
                title=section.get("title", ""),
                page_start=min(pages) if pages else None,
                page_end=max(pages) if pages else None,
            ))
    return rows


def paginate(content: dict) -> list[PageRow]:
    """Collapse paragraphs into one row per (page number) in reading order.

    Paragraphs on the same printed page are joined with '\\n', and each paragraph's
    character offset within the joined text is recorded so a search hit can later resolve
    back to an exact paragraph. A page is tagged with the section it *opens* in — the
    section active at its first paragraph — which is what a search result needs to show
    "which chapter" without storing a section per paragraph.
    """
    pages: dict[int, PageRow] = {}
    order: list[int] = []
    section_ord = 0

    for chapter in sorted(content.get("chapters", []), key=lambda c: c.get("order", 0)):
        for section in sorted(chapter.get("sections", []), key=lambda s: s.get("order", 0)):
            section_ord += 1
            for para in sorted(section.get("paragraphs", []), key=lambda p: p.get("order", 0)):
                page_no = para["page"]
                text = para.get("text", "")

                row = pages.get(page_no)
                if row is None:
                    row = PageRow(
                        page_no=page_no, text="", paragraph_offsets=[],
                        section_ord=section_ord,
                    )
                    pages[page_no] = row
                    order.append(page_no)

                start = len(row.text) + (1 if row.text else 0)  # account for the joining '\n'
                row.paragraph_offsets.append({"id": para["id"], "start": start})
                row.text = f"{row.text}\n{text}" if row.text else text

    return [pages[n] for n in order]
