"""Pagination tests: the paragraph→page transform and its offset mapping.

Isolated from the importer because this is where a subtle bug would be invisible in the
database but wrong in the app — an offset that doesn't land on a paragraph boundary breaks
post-download deep-linking, and nothing about the row count would reveal it.
"""

from app.services.paging import paginate, paginate_sections


def _book(*paragraphs: tuple[str, int, str]) -> dict:
    """paragraphs as (id, page, text), all under one section."""
    return {
        "schemaVersion": 1,
        "bookId": "1",
        "title": "ك",
        "author": "م",
        "chapters": [
            {
                "id": "ch-001",
                "title": "ك",
                "order": 1,
                "sections": [
                    {
                        "id": "sec-001",
                        "title": "باب",
                        "order": 1,
                        "paragraphs": [
                            {"id": pid, "order": i + 1, "page": page, "text": text}
                            for i, (pid, page, text) in enumerate(paragraphs)
                        ],
                    }
                ],
            }
        ],
    }


def test_paragraphs_on_same_page_are_joined():
    pages = paginate(_book(("p1", 1, "أول"), ("p2", 1, "ثان"), ("p3", 2, "ثالث")))
    assert [p.page_no for p in pages] == [1, 2]
    assert pages[0].text == "أول\nثان"
    assert pages[1].text == "ثالث"


def test_offsets_point_at_paragraph_starts():
    pages = paginate(_book(("p1", 1, "أول"), ("p2", 1, "ثان")))
    page = pages[0]
    # Each recorded offset must slice back to that paragraph's own text.
    for offset, expected in zip(page.paragraph_offsets, ["أول", "ثان"], strict=True):
        assert page.text[offset["start"]:].startswith(expected)


def test_offset_ids_match_source_paragraph_ids():
    pages = paginate(_book(("p1", 1, "أول"), ("p2", 1, "ثان")))
    assert [o["id"] for o in pages[0].paragraph_offsets] == ["p1", "p2"]


def test_page_order_follows_reading_order_not_numeric_order():
    """Pages are emitted in the order they appear in the text, which is what the reader
    and the section linkage rely on — not sorted by page number."""
    pages = paginate(_book(("p1", 5, "خمسة"), ("p2", 3, "ثلاثة")))
    assert [p.page_no for p in pages] == [5, 3]


def test_sections_carry_their_page_range():
    book = _book(("p1", 10, "أ"), ("p2", 12, "ب"))
    sections = paginate_sections(book)
    assert len(sections) == 1
    assert (sections[0].page_start, sections[0].page_end) == (10, 12)
    assert sections[0].ord == 1


def test_page_is_tagged_with_the_section_it_opens_in():
    book = {
        "schemaVersion": 1, "bookId": "1", "title": "ك", "author": "م",
        "chapters": [{"id": "ch-001", "title": "ك", "order": 1, "sections": [
            {"id": "sec-001", "title": "أول", "order": 1,
             "paragraphs": [{"id": "a", "order": 1, "page": 1, "text": "أ"}]},
            {"id": "sec-002", "title": "ثان", "order": 2,
             "paragraphs": [{"id": "b", "order": 1, "page": 2, "text": "ب"}]},
        ]}],
    }
    pages = paginate(book)
    assert [(p.page_no, p.section_ord) for p in pages] == [(1, 1), (2, 2)]
