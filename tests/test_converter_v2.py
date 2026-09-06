"""v2 converter and validator tests, run against a real .abx source when present.

Converts to the page/block/toc shape (v2), not v1's chapters/sections/paragraphs. These
tests pin the behaviours a full 18,831-file corpus run surfaced as bugs: inline tags
(a footnote/heading/page marker mid-line, surrounded by ordinary prose) must not leak
raw markup or swallow unrelated text, and repeated page markers with the same printed
number must merge into one real page.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "convert"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "validate"))

import shamela_to_json_v2 as conv  # noqa: E402
import validate_book_v2 as val  # noqa: E402

SAMPLE_ABX = Path("/Users/mohamadkachakech/Downloads/Exported/431.abx")
needs_source = pytest.mark.skipif(not SAMPLE_ABX.exists(), reason="source .abx not present")

# A minimal but complete Shamela file, so the core tests need no external fixture.
SYNTHETIC = """checksum-line-ignored
< اسم الكتاب > كتاب التجربة < / اسم الكتاب >
< اسم المؤلف > مؤلف مجهول < / اسم المؤلف >
< جزء > 3 < / جزء >
< مجموعة > مصادر التفسير عند الشيعة < / مجموعة >
< الناشر > دار النشر < / الناشر >
< الكتاب >
< صفحة > 5 < / صفحة >
< فهرس الموضوعات >
الباب الأول
< / فهرس الموضوعات >
النص الأول في الصفحة الخامسة.
النص الثاني.
< صفحة > 6 < / صفحة >
النص الثالث في الصفحة السادسة.
< / الكتاب >
"""


@pytest.fixture
def synthetic_file(tmp_path: Path) -> Path:
    path = tmp_path / "999.abx"
    path.write_text(SYNTHETIC, encoding="utf-8")
    return path


def _all_text_blocks(content: dict) -> list[dict]:
    return [b for p in content["pages"] for b in p["blocks"]]


class TestConversion:
    def test_produces_schema_version_2(self, synthetic_file: Path):
        content = conv.convert(synthetic_file, "999")
        assert content["schemaVersion"] == 2
        assert content["bookId"] == "999"

    def test_title_is_clean_without_volume_suffix(self, synthetic_file: Path):
        """volume is a field, not folded into the title."""
        content = conv.convert(synthetic_file, "999")
        assert content["title"] == "كتاب التجربة"
        assert "الجزء" not in content["title"]
        assert content["metadata"]["volume"] == "3"

    def test_metadata_is_captured(self, synthetic_file: Path):
        content = conv.convert(synthetic_file, "999")
        assert content["metadata"]["publisher"] == "دار النشر"
        assert content["metadata"]["collection"] == "مصادر التفسير عند الشيعة"

    def test_pages_carry_the_right_number_and_blocks(self, synthetic_file: Path):
        content = conv.convert(synthetic_file, "999")
        assert [p["pageNumber"] for p in content["pages"]] == ["5", "6"]
        page5_text = [b["text"] for b in content["pages"][0]["blocks"] if b["type"] == "text"]
        assert page5_text == ["النص الأول في الصفحة الخامسة.", "النص الثاني."]

    def test_heading_becomes_a_toc_entry(self, synthetic_file: Path):
        content = conv.convert(synthetic_file, "999")
        assert len(content["toc"]) == 1
        assert content["toc"][0]["title"] == "الباب الأول"
        assert content["toc"][0]["pageNumber"] == "5"

    def test_empty_volume_tag_yields_no_field(self, tmp_path: Path):
        text = SYNTHETIC.replace("< جزء > 3 < / جزء >", "< جزء > < / جزء >")
        path = tmp_path / "998.abx"
        path.write_text(text, encoding="utf-8")
        content = conv.convert(path, "998")
        assert "volume" not in content["metadata"]

    def test_missing_body_marker_raises(self, tmp_path: Path):
        path = tmp_path / "bad.abx"
        path.write_text("checksum\n< اسم الكتاب > بلا جسد < / اسم الكتاب >\n", encoding="utf-8")
        with pytest.raises(conv.ConversionError, match="body marker"):
            conv.convert(path, "bad")

    def test_inline_footnote_does_not_swallow_surrounding_text(self, tmp_path: Path):
        """The corpus-wide bug: a footnote tag mid-sentence, with real prose both before
        and after it on the same line, must not leak raw tag markup or lose the
        surrounding text to a mislabeled footnote block."""
        text = SYNTHETIC.replace(
            "النص الثالث في الصفحة السادسة.",
            "قبل الحاشية < هامش > نص الحاشية < / هامش > بعد الحاشية .",
        )
        path = tmp_path / "995.abx"
        path.write_text(text, encoding="utf-8")
        content = conv.convert(path, "995")
        blocks = _all_text_blocks(content)
        assert [b["type"] for b in blocks if b["text"].strip()][-3:] == [
            "text", "footnotes", "text",
        ]
        joined = " ".join(b["text"] for b in blocks)
        assert "هامش" not in joined
        assert "قبل الحاشية" in joined and "بعد الحاشية" in joined

    def test_repeated_page_marker_with_same_number_merges(self, tmp_path: Path):
        """Shamela tags every verse/paragraph with its own < صفحة > marker even when
        several share one real printed page (seen in the Qur'an) -- consecutive markers
        with the same printed number must merge into one page, not one each."""
        text = SYNTHETIC.replace(
            "< صفحة > 6 < / صفحة >\nالنص الثالث في الصفحة السادسة.",
            "< صفحة > 5 < / صفحة >\nمقطع إضافي على نفس الصفحة.",
        )
        path = tmp_path / "994.abx"
        path.write_text(text, encoding="utf-8")
        content = conv.convert(path, "994")
        assert [p["pageNumber"] for p in content["pages"]] == ["5"]

    def test_generic_tag_is_deleted_not_treated_as_a_block(self, tmp_path: Path):
        """A tag with no schema meaning (poetry, commentary, inline language switch, ...)
        is stripped in place -- its content keeps flowing as ordinary text, including any
        real tag nested inside it."""
        text = SYNTHETIC.replace(
            "النص الثالث في الصفحة السادسة.",
            "قبل < لغة النص = انجليزي > English < / لغة النص = انجليزي > بعد .",
        )
        path = tmp_path / "993.abx"
        path.write_text(text, encoding="utf-8")
        content = conv.convert(path, "993")
        joined = " ".join(b["text"] for b in _all_text_blocks(content))
        assert "لغة النص" not in joined
        assert "English" in joined

    @needs_source
    def test_real_source_converts_without_error(self):
        content = conv.convert(SAMPLE_ABX, "431")
        assert content["pages"]
        assert not val.validate(content) or all(
            i.severity == "warning" for i in val.validate(content)
        )


class TestValidation:
    def _valid_content(self) -> dict:
        return {
            "schemaVersion": 2,
            "bookId": "1",
            "title": "كتاب",
            "author": "مؤلف",
            "metadata": {},
            "pages": [
                {
                    "id": "p-000001", "sequence": 1, "pageNumber": "1", "pageType": "main",
                    "isBlank": False,
                    "blocks": [
                        {"id": "p-000001-b-001", "type": "text", "order": 1, "text": "نص"},
                    ],
                },
            ],
            "toc": [],
        }

    def test_valid_book_has_no_issues(self):
        assert val.validate(self._valid_content()) == []

    def test_missing_title_is_an_error(self):
        content = self._valid_content()
        content["title"] = ""
        codes = {(i.severity, i.code) for i in val.validate(content)}
        assert ("error", "missing_title") in codes

    def test_no_content_is_an_error(self):
        content = self._valid_content()
        content["pages"] = []
        codes = {(i.severity, i.code) for i in val.validate(content)}
        assert ("error", "no_pages") in codes

    def test_bad_page_type_is_an_error(self):
        content = self._valid_content()
        content["pages"][0]["pageType"] = "bogus"
        codes = {(i.severity, i.code) for i in val.validate(content)}
        assert ("error", "bad_page_type") in codes

    def test_duplicate_block_id_is_an_error(self):
        content = self._valid_content()
        content["pages"][0]["blocks"].append(
            {"id": "p-000001-b-001", "type": "text", "order": 2, "text": "نص آخر"}
        )
        codes = {(i.severity, i.code) for i in val.validate(content)}
        assert ("error", "duplicate_block_id") in codes

    def test_missing_author_is_only_a_warning(self):
        content = self._valid_content()
        content["author"] = ""
        issues = val.validate(content)
        assert all(i.severity == "warning" for i in issues if i.code == "missing_author")
        assert not [i for i in issues if i.severity == "error"]

    def test_page_regression_among_main_pages_is_a_warning(self):
        content = self._valid_content()
        content["pages"].append({
            "id": "p-000002", "sequence": 2, "pageNumber": "0", "pageType": "main",
            "isBlank": False,
            "blocks": [{"id": "p-000002-b-001", "type": "text", "order": 1, "text": "نص"}],
        })
        codes = {(i.severity, i.code) for i in val.validate(content)}
        assert ("warning", "non_monotonic_pages") in codes
        assert not [c for s, c in codes if s == "error"]

    def test_wrong_schema_version_is_an_error(self):
        content = self._valid_content()
        content["schemaVersion"] = 1
        codes = {(i.severity, i.code) for i in val.validate(content)}
        assert ("error", "schema_version") in codes
