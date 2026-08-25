"""Converter and validator tests, run against a real .abx source when present.

The converter's parsing logic is inherited from the iOS project's proven version; these
tests pin the behaviours the *backend* port adds — full metadata capture, volume as a
field rather than a title suffix, compact output — plus the validator's severity calls.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "convert"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "validate"))

import shamela_to_json as conv  # noqa: E402
import validate_book as val  # noqa: E402

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


class TestConversion:
    def test_produces_schema_version_1(self, synthetic_file: Path):
        content, _ = conv.convert(synthetic_file, "999")
        assert content["schemaVersion"] == 1
        assert content["bookId"] == "999"

    def test_title_is_clean_without_volume_suffix(self, synthetic_file: Path):
        """The core backend change: volume is a field, not folded into the title the way
        the iOS converter did it."""
        content, manifest = conv.convert(synthetic_file, "999")
        assert content["title"] == "كتاب التجربة"
        assert "الجزء" not in content["title"]
        assert manifest["volume"] == 3

    def test_metadata_is_captured(self, synthetic_file: Path):
        _, manifest = conv.convert(synthetic_file, "999")
        assert manifest["metadata"]["publisher"] == "دار النشر"
        assert manifest["metadata"]["collection"] == "مصادر التفسير عند الشيعة"

    def test_paragraphs_carry_the_right_page(self, synthetic_file: Path):
        content, _ = conv.convert(synthetic_file, "999")
        paras = [p for c in content["chapters"] for s in c["sections"] for p in s["paragraphs"]]
        assert [p["page"] for p in paras] == [5, 5, 6]
        assert paras[0]["text"] == "النص الأول في الصفحة الخامسة."

    def test_page_range_in_manifest(self, synthetic_file: Path):
        _, manifest = conv.convert(synthetic_file, "999")
        assert (manifest["pageFirst"], manifest["pageLast"]) == (5, 6)
        assert manifest["paragraphCount"] == 3

    def test_quran_link_tag_is_stripped_inline(self, tmp_path: Path):
        """Shamela wraps every quoted verse in a cross-reference tag meant for its own
        desktop app's clickable links -- `< ارتباط = SSSSAAA... > verse < / ارتباط = ... >`.
        Left unstripped, this leaks as literal markup into what the reader sees. Only the
        tag goes; the verse text and the following [ سورة : آية ] reference (already
        plain text in the source, never tagged) stay exactly where they were."""
        text = SYNTHETIC.replace(
            "النص الثالث في الصفحة السادسة.",
            "قال تعالى < ارتباط = 0001059024 > هُوَ اللَّهُ الْخالِقُ < / ارتباط = 0001059024 > "
            "[ الحشر : 24 ] .",
        )
        path = tmp_path / "997.abx"
        path.write_text(text, encoding="utf-8")
        content, _ = conv.convert(path, "997")
        paras = [p for c in content["chapters"] for s in c["sections"] for p in s["paragraphs"]]
        assert "ارتباط" not in paras[-1]["text"]
        assert paras[-1]["text"] == "قال تعالى هُوَ اللَّهُ الْخالِقُ [ الحشر : 24 ] ."

    def test_quran_link_tag_spanning_lines_is_stripped(self, tmp_path: Path):
        """The same tag pair can straddle a line break -- an Arabic verse followed by
        its translation on the next line before the tag closes. Both fragments must
        still come through as clean, separate paragraphs with no leftover markup."""
        text = SYNTHETIC.replace(
            "النص الثالث في الصفحة السادسة.",
            "< ارتباط = 0001059024 > هُوَ اللَّهُ الْخالِقُ\n"
            "he is God the creator < / ارتباط = 0001059024 >",
        )
        path = tmp_path / "996.abx"
        path.write_text(text, encoding="utf-8")
        content, _ = conv.convert(path, "996")
        paras = [p for c in content["chapters"] for s in c["sections"] for p in s["paragraphs"]]
        texts = [p["text"] for p in paras]
        assert "ارتباط" not in " ".join(texts)
        assert "هُوَ اللَّهُ الْخالِقُ" in texts
        assert "he is God the creator" in texts

    def test_empty_volume_tag_yields_none(self, tmp_path: Path):
        text = SYNTHETIC.replace("< جزء > 3 < / جزء >", "< جزء > < / جزء >")
        path = tmp_path / "998.abx"
        path.write_text(text, encoding="utf-8")
        _, manifest = conv.convert(path, "998")
        assert manifest["volume"] is None

    def test_missing_body_marker_raises(self, tmp_path: Path):
        path = tmp_path / "bad.abx"
        path.write_text("checksum\n< اسم الكتاب > بلا جسد < / اسم الكتاب >\n", encoding="utf-8")
        with pytest.raises(conv.ConversionError, match="body marker"):
            conv.convert(path, "bad")

    @needs_source
    def test_matches_reference_converter_paragraph_for_paragraph(self):
        """The strongest guarantee: identical paragraph text to the iOS converter's own
        output for the same source, so the port changed nothing about the content."""
        ref_path = Path(
            "/Users/mohamadkachakech/Documents/xcode_projects/SwiftUI/Thaqalayn"
            "/Thaqalayn/Resources/SampleBook/shamela-431-content.json"
        )
        if not ref_path.exists():
            pytest.skip("reference JSON not present")
        content, _ = conv.convert(SAMPLE_ABX, "431")
        ref = json.loads(ref_path.read_text())
        def texts(book: dict) -> list[str]:
            return [
                p["text"]
                for c in book["chapters"]
                for s in c["sections"]
                for p in s["paragraphs"]
            ]

        assert texts(content) == texts(ref)


class TestValidation:
    def _valid_content(self) -> dict:
        return {
            "schemaVersion": 1,
            "bookId": "1",
            "title": "كتاب",
            "author": "مؤلف",
            "chapters": [
                {
                    "id": "ch-001",
                    "title": "كتاب",
                    "order": 1,
                    "sections": [
                        {
                            "id": "sec-001",
                            "title": "باب",
                            "order": 1,
                            "paragraphs": [
                                {"id": "sec-001-p-001", "order": 1, "page": 1, "text": "نص"},
                            ],
                        }
                    ],
                }
            ],
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
        content["chapters"][0]["sections"][0]["paragraphs"] = []
        codes = {(i.severity, i.code) for i in val.validate(content)}
        assert ("error", "no_content") in codes

    def test_bad_page_number_is_an_error(self):
        content = self._valid_content()
        content["chapters"][0]["sections"][0]["paragraphs"][0]["page"] = 0
        codes = {(i.severity, i.code) for i in val.validate(content)}
        assert ("error", "bad_page_number") in codes

    def test_duplicate_paragraph_id_is_an_error(self):
        content = self._valid_content()
        section = content["chapters"][0]["sections"][0]
        section["paragraphs"].append(
            {"id": "sec-001-p-001", "order": 2, "page": 1, "text": "نص آخر"}
        )
        codes = {(i.severity, i.code) for i in val.validate(content)}
        assert ("error", "duplicate_paragraph_id") in codes

    def test_missing_author_is_only_a_warning(self):
        content = self._valid_content()
        content["author"] = ""
        issues = val.validate(content)
        assert all(i.severity == "warning" for i in issues if i.code == "missing_author")
        assert not [i for i in issues if i.severity == "error"]

    def test_page_regression_is_a_warning(self):
        content = self._valid_content()
        section = content["chapters"][0]["sections"][0]
        section["paragraphs"].append(
            {"id": "sec-001-p-002", "order": 2, "page": 1, "text": "نص"}  # 1 after 1 is fine
        )
        section["paragraphs"].append(
            {"id": "sec-001-p-003", "order": 3, "page": 1, "text": "نص"}
        )
        # Now force a real descent: page 1 after page 5.
        section["paragraphs"][0]["page"] = 5
        codes = {(i.severity, i.code) for i in val.validate(content)}
        assert ("warning", "non_monotonic_pages") in codes
        assert not [c for s, c in codes if s == "error"]
