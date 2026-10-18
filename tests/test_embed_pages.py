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


def sentences(count, each=20):
    """`count` sentences of `each` words, each ending in ' .' like the books' spaced punctuation."""
    return " ".join(words(each - 1, f"ك{i}") + " ." for i in range(count))


def test_a_short_page_is_one_chunk_with_heading_and_text_and_no_footnotes():
    chunks = ep.page_chunks(page(("heading", words(3, "باب")), ("text", words(20)), ("footnotes", words(30, "هامش"))))
    assert len(chunks) == 1
    assert "هامش" not in chunks[0] and chunks[0].startswith("باب")


def test_chunks_end_at_a_sentence_end_and_the_next_one_repeats_the_last_sentence():
    chunks = ep.page_chunks(page(("text", sentences(30))), size=220, overlap=30)  # 600 words
    assert len(chunks) >= 3 and all(len(c.split()) <= 220 for c in chunks)
    assert all(c.endswith(".") for c in chunks)
    first_end = chunks[0].split()[-20:]  # the last sentence of chunk 1 ...
    assert chunks[1].split()[:20] == first_end  # ... opens chunk 2
    assert chunks[-1].split()[-1] == "."  # the whole text is covered


def test_lines_of_a_printed_book_are_joined_not_cut_at_each_line():
    lines = [("text", words(13, f"س{i}") + (" ." if i % 3 == 2 else "")) for i in range(40)]
    chunks = ep.page_chunks(page(*lines), size=100, overlap=20)
    assert len(chunks) >= 5 and all(len(c.split()) <= 100 for c in chunks)
    assert all(c.endswith(".") for c in chunks[:-1])


def test_a_heading_starts_a_new_chunk_once_there_is_enough_text():
    chunks = ep.page_chunks(page(("text", sentences(6)), ("heading", words(4, "باب")), ("text", sentences(2))))
    assert len(chunks) == 2 and chunks[1].startswith("باب")


def test_a_heading_after_little_text_stays_in_the_same_chunk():
    chunks = ep.page_chunks(page(("text", sentences(1)), ("heading", words(4, "باب")), ("text", sentences(2))))
    assert len(chunks) == 1


def test_a_long_text_without_any_sentence_end_is_cut_by_words():
    chunks = ep.page_chunks(page(("text", words(500))), size=220, overlap=30)
    assert all(len(c.split()) <= 220 for c in chunks) and len(chunks) >= 3


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
