"""The chunking of the vector-search pilot (scripts/embeddings/embed_pages.py); no network."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "embed_pages", Path(__file__).resolve().parents[1] / "scripts" / "embeddings" / "embed_pages.py")
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)


def page(*blocks, seq=1, blank=False):
    return {"sequence": seq, "isBlank": blank, "pageNumber": str(seq),
            "blocks": [{"type": t, "text": x} for t, x in blocks]}


def words(n, w="كلمة"):
    return " ".join([w] * n)


def test_diacritics_and_tatweel_are_removed_and_spaces_collapsed():
    assert ep.clean("الْعِلْمُ   نُـــورٌ") == "العلم نور"


def test_a_short_page_is_one_chunk_with_heading_and_text_and_no_footnotes():
    chunks = ep.page_chunks(page(("heading", words(3, "باب")), ("text", words(20)), ("footnotes", words(30, "هامش"))))
    assert len(chunks) == 1
    assert "هامش" not in chunks[0] and chunks[0].startswith("باب")


def test_paragraphs_are_grouped_up_to_the_size_and_never_split_when_they_fit():
    chunks = ep.page_chunks(page(("text", words(100, "أ")), ("text", words(100, "ب")), ("text", words(100, "ج"))), size=220)
    assert [len(c.split()) for c in chunks] == [200, 100]


def test_a_long_paragraph_is_split_by_words():
    chunks = ep.page_chunks(page(("text", words(500))), size=220)
    assert [len(c.split()) for c in chunks] == [220, 220, 60]


def test_tiny_chunks_are_dropped():
    assert ep.page_chunks(page(("text", "ص 5"))) == []


def test_points_have_stable_ids_and_a_payload_to_show_the_hit():
    book = {"bookId": 7, "title": "الكافي", "metadata": {"volume": "2", "authorDeath": "329"},
            "pages": [page(("text", words(30)), seq=1), page(("text", words(30)), seq=2, blank=True),
                      page(("text", words(30)), seq=3)]}
    first = ep.book_points(book)
    assert [p["payload"]["sequence"] for p in first] == [1, 3]
    assert first[0]["doc"].startswith("title: الكافي | text: ")
    assert first[0]["payload"]["volume"] == "2" and first[0]["payload"]["page"] == "1"
    assert [p["id"] for p in first] == [p["id"] for p in ep.book_points(book)]
    assert len({p["id"] for p in first}) == 2
