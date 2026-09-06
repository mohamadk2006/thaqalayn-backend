"""Pagination tests: the v2 page/block transform and its offset mapping.

Isolated from the importer because this is where a subtle bug would be invisible in the
database but wrong in the app — an offset that doesn't land on a block boundary breaks
post-download deep-linking, and nothing about the row count would reveal it.
"""

from app.services.paging import paginate, paginate_sections


def _page(seq: int, page_number: str, page_type: str, *blocks: tuple[str, str, str]) -> dict:
    """blocks as (id_suffix, type, text), attached to page p-{seq:06d}."""
    page_id = f"p-{seq:06d}"
    return {
        "id": page_id,
        "sequence": seq,
        "pageNumber": page_number,
        "pageType": page_type,
        "isBlank": False,
        "blocks": [
            {"id": f"{page_id}-b-{i + 1:03d}", "type": btype, "order": i + 1, "text": text}
            for i, (_suffix, btype, text) in enumerate(blocks)
        ],
    }


def _book(*pages: dict, toc: list[dict] | None = None) -> dict:
    return {
        "schemaVersion": 2, "bookId": "1", "title": "ك", "author": "م",
        "metadata": {}, "pages": list(pages), "toc": toc or [],
    }


def test_blocks_on_same_page_are_joined():
    book = _book(
        _page(1, "1", "main", ("a", "text", "أول"), ("b", "text", "ثان")),
        _page(2, "2", "main", ("c", "text", "ثالث")),
    )
    pages = paginate(book)
    assert [p.sequence for p in pages] == [1, 2]
    assert pages[0].text == "أول\nثان"
    assert pages[1].text == "ثالث"


def test_offsets_point_at_block_starts():
    book = _book(_page(1, "1", "main", ("a", "text", "أول"), ("b", "text", "ثان")))
    page = paginate(book)[0]
    for offset, expected in zip(page.block_offsets, ["أول", "ثان"], strict=True):
        assert page.text[offset["start"]:].startswith(expected)


def test_offset_ids_match_source_block_ids():
    book = _book(_page(1, "1", "main", ("a", "text", "أول"), ("b", "text", "ثان")))
    page = paginate(book)[0]
    assert [o["id"] for o in page.block_offsets] == ["p-000001-b-001", "p-000001-b-002"]


def test_footnote_and_heading_text_are_included_alongside_text_blocks():
    """A reader searching for something that only lives in a footnote citation should
    still find the page — page.text is not restricted to type: "text" blocks."""
    book = _book(_page(
        1, "1", "main",
        ("a", "heading", "باب أول"), ("b", "text", "متن"), ("c", "footnotes", "حاشية"),
    ))
    page = paginate(book)[0]
    assert page.text == "باب أول\nمتن\nحاشية"


def test_page_number_and_type_pass_through():
    book = _book(
        _page(1, "0.1", "frontMatter", ("a", "text", "مقدمة")),
        _page(2, "1", "main", ("b", "text", "متن")),
    )
    pages = paginate(book)
    assert (pages[0].page_number, pages[0].page_type) == ("0.1", "frontMatter")
    assert (pages[1].page_number, pages[1].page_type) == ("1", "main")


def test_blank_page_flag_passes_through():
    page = _page(1, "1", "main")
    page["isBlank"] = True
    pages = paginate(_book(page))
    assert pages[0].is_blank is True


def test_sections_carry_their_page_sequence_range():
    book = _book(
        _page(1, "1", "main", ("a", "heading", "الباب الأول")),
        _page(2, "2", "main", ("b", "text", "متن")),
        _page(3, "3", "main", ("c", "text", "متن")),
        toc=[{"id": "toc-00001", "order": 1, "title": "الباب الأول", "pageId": "p-000001"}],
    )
    sections = paginate_sections(book)
    assert len(sections) == 1
    assert (sections[0].page_start_sequence, sections[0].page_end_sequence) == (1, 3)
    assert sections[0].ord == 1


def test_two_sections_split_the_range_at_the_next_headings_page():
    book = _book(
        _page(1, "1", "main", ("a", "heading", "أول")),
        _page(2, "2", "main", ("b", "heading", "ثان")),
        toc=[
            {"id": "toc-00001", "order": 1, "title": "أول", "pageId": "p-000001"},
            {"id": "toc-00002", "order": 2, "title": "ثان", "pageId": "p-000002"},
        ],
    )
    sections = paginate_sections(book)
    assert (sections[0].page_start_sequence, sections[0].page_end_sequence) == (1, 1)
    assert (sections[1].page_start_sequence, sections[1].page_end_sequence) == (2, 2)


def test_page_is_tagged_with_the_section_active_as_of_its_sequence():
    book = _book(
        _page(1, "1", "main", ("a", "heading", "أول")),
        _page(2, "2", "main", ("b", "text", "ب")),
        _page(3, "3", "main", ("c", "heading", "ثان")),
        toc=[
            {"id": "toc-00001", "order": 1, "title": "أول", "pageId": "p-000001"},
            {"id": "toc-00002", "order": 2, "title": "ثان", "pageId": "p-000003"},
        ],
    )
    pages = paginate(book)
    assert [p.section_ord for p in pages] == [1, 1, 2]


def test_page_with_two_headings_gets_the_later_section_ordinal():
    book = _book(
        _page(1, "1", "main", ("a", "heading", "أول"), ("b", "heading", "ثان")),
        toc=[
            {"id": "toc-00001", "order": 1, "title": "أول", "pageId": "p-000001"},
            {"id": "toc-00002", "order": 2, "title": "ثان", "pageId": "p-000001"},
        ],
    )
    pages = paginate(book)
    assert pages[0].section_ord == 2


def test_no_toc_means_no_section_assignment():
    pages = paginate(_book(_page(1, "1", "main", ("a", "text", "متن"))))
    assert pages[0].section_ord is None
    assert paginate_sections(_book(_page(1, "1", "main", ("a", "text", "متن")))) == []
