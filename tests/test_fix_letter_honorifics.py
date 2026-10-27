"""scripts/convert/fix_letter_honorifics.py: lone Latin letters from the Basmalla font."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "fix_letter_honorifics",
    Path(__file__).resolve().parents[1] / "scripts" / "convert" / "fix_letter_honorifics.py",
)
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)


def test_each_letter_is_spelt_out_with_spaces_like_the_converter():
    assert fix.spell("أمير المؤمنين N، وللقول") == "أمير المؤمنين عليه السلام، وللقول"
    assert fix.spell("رسول اﷲ C وزمن") == "رسول اﷲ صلى الله عليه وآله وسلم وزمن"
    assert fix.spell("المعصومين S.") == "المعصومين عليهم السلام."
    assert fix.spell("السيدة زينب X:") == "السيدة زينب عليها السلام:"
    assert fix.spell("الحسين بن علي J يقول") == "الحسين بن علي عليهما السلام يقول"
    assert fix.spell("قال المفيد R: روى") == "قال المفيد رحمه الله: روى"


def test_a_letter_glued_to_arabic_gets_its_own_spaces():
    assert fix.spell("بقولهL: «اليوم") == "بقوله عز وجل: «اليوم"
    assert fix.spell("فيقول اﷲLإذهب") == "فيقول اﷲ عز وجل إذهب"
    assert fix.spell("زكريا N ربهLفقال") == "زكريا عليه السلام ربه عز وجل فقال"


def test_a_verse_ornament_and_real_latin_words_are_left_alone():
    assert fix.spell("يَخْشَى L﴾(2).") == "يَخْشَى ﴾(2)."  # an ornament, dropped
    assert fix.spell("see NASA and FR and C3 and Nx") == "see NASA and FR and C3 and Nx"
    assert fix.spell("(N)") == "(عليه السلام)"  # a lone letter in brackets is still the honorific


def test_a_whole_book_the_basmala_block_headings_and_contents_list():
    book = {
        "pages": [
            {
                "blocks": [
                    {"id": "p-1-b-1", "type": "text", "text": "a"},
                    {"id": "p-1-b-2", "type": "heading", "text": "خروج الإمام الحسين N والفتح"},
                    {"id": "p-1-b-3", "type": "text", "text": "قال الإمام N لأصحابه"},
                    {"id": "p-1-b-4", "type": "footnotes", "text": "(1) رواه المفيد R"},
                    {"id": "p-1-b-5", "type": "image", "text": "N"},
                ]
            }
        ],
        "toc": [{"title": "خروج الإمام الحسين N والفتح"}],
    }
    counts = fix.fix_book(book)
    texts = [b["text"] for b in book["pages"][0]["blocks"]]
    assert texts == [
        "بسم الله الرحمن الرحيم",
        "خروج الإمام الحسين عليه السلام والفتح",
        "قال الإمام عليه السلام لأصحابه",
        "(1) رواه المفيد رحمه الله",
        "N",  # not a text block: untouched
    ]
    assert book["toc"][0]["title"] == "خروج الإمام الحسين عليه السلام والفتح"
    assert counts["N"] == 3 and counts["R"] == 1 and counts["basmala"] == 1
