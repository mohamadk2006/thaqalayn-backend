"""The conversion workbench end to end: an employee uploads a Word file, it is converted,
edited and submitted; the admin panel lists it, sends it back or publishes it into the
library. Uses a real .docx, the real converter, validator and importer and the real
database; only LibreOffice (the original-page images) is absent here, which the
workbench must report rather than fail on."""

import io
from pathlib import Path
from urllib.parse import unquote

import docx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import get_settings
from app.db import get_sessionmaker
from app.main import create_app
from app.services import drafts
from workbench import main as wb

TITLE = "كتاب اختبار المحوّل"
AUTHOR = "مؤلف اختبار المحوّل"


def make_docx() -> bytes:
    """Three pages split by manual page breaks, two chapter headings, and a footnote
    under the separator rule -- the structure the converter reads."""
    d = docx.Document()
    d.add_heading("الباب الأول في العلم", level=1)
    d.add_paragraph("قال الإمامُ الصادقُ عليه السلام: العلمُ نورٌ.")
    d.add_paragraph("____________")
    d.add_paragraph("(1) الكافي ج 1 ص 1.")
    d.add_page_break()
    d.add_paragraph("تتمة الكلام في فضل العلم والعلماء.")
    d.add_page_break()
    d.add_heading("الباب الثاني في العمل", level=1)
    d.add_paragraph("العمل ثمرة العلم.")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


@pytest.fixture
def workbench_root(tmp_path: Path) -> Path:
    return tmp_path / "workbench"


@pytest.fixture
async def employee(workbench_root, monkeypatch):
    settings = wb.WorkbenchSettings(workbench_root=workbench_root, workbench_users="emp:secret,other:pw2")
    monkeypatch.setattr(wb, "get_settings", lambda: settings)
    monkeypatch.setattr(wb.conversion, "renderer_available", lambda: False)
    async with AsyncClient(transport=ASGITransport(app=wb.app), base_url="http://wb", auth=("emp", "secret")) as c:
        yield c


@pytest.fixture
async def admin(workbench_root, tmp_path, monkeypatch):
    base = get_settings()
    patched = base.model_copy(update={"books_root": tmp_path / "books", "workbench_root": workbench_root,
                                      "workbench_url": "https://convert.example"})
    monkeypatch.setattr("app.api.admin.get_settings", lambda: patched)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test",
                           auth=(base.admin_username, base.admin_password)) as c:
        yield c
    async with get_sessionmaker()() as s:
        for t in (TITLE, "عنوان صحيح بعد التصحيح"):
            await s.execute(text("DELETE FROM books WHERE title_norm = :t"), {"t": t})
            await s.execute(text("DELETE FROM works WHERE title_norm = :t"), {"t": t})
        await s.execute(text("DELETE FROM authors WHERE name = :a"), {"a": AUTHOR})
        await s.commit()


async def upload(client, name="كتاب.docx", data=None):
    return await client.post("/api/drafts", files={"file": (name, data or make_docx())})


def issue_codes(meta):
    return {i["code"] for i in meta["issues"] if i["severity"] == "error"}


class TestAccess:
    async def test_login_is_required(self, employee):
        async with AsyncClient(transport=ASGITransport(app=wb.app), base_url="http://wb") as anon:
            assert (await anon.get("/api/drafts")).status_code == 401
            assert (await anon.get("/")).status_code == 401
        async with AsyncClient(transport=ASGITransport(app=wb.app), base_url="http://wb", auth=("emp", "wrong")) as bad:
            assert (await bad.get("/api/drafts")).status_code == 401

    async def test_page_is_served(self, employee):
        r = await employee.get("/")
        assert r.status_code == 200 and "محوّل الكتب" in r.text

    async def test_malformed_draft_id_is_404(self, employee):
        assert (await employee.get("/api/drafts/..%2F..%2Fetc")).status_code == 404
        assert (await employee.get("/api/drafts/20260101-zzzzzz")).status_code == 404

    async def test_only_word_files(self, employee):
        r = await employee.post("/api/drafts", files={"file": ("x.pdf", b"%PDF")})
        assert r.status_code == 400


class TestConvertAndEdit:
    async def test_upload_converts_the_book(self, employee, workbench_root):
        r = await upload(employee)
        assert r.status_code == 200, r.text
        meta = r.json()
        assert meta["status"] == "editing" and meta["createdBy"] == "emp"
        assert meta["report"]["pages"] == 3 and meta["report"]["tocEntries"] == 2
        assert meta["render"]["status"] in {"pending", "unavailable"}
        book = (await employee.get(f"/api/drafts/{meta['id']}/book")).json()
        headings = [b["text"] for p in book["pages"] for b in p["blocks"] if b["type"] == "heading"]
        assert headings == ["الباب الأول في العلم", "الباب الثاني في العمل"]
        assert any(b["type"] == "footnotes" for b in book["pages"][0]["blocks"])
        # Without LibreOffice the original can't be shown -- reported, not an error.
        fresh = (await employee.get(f"/api/drafts/{meta['id']}")).json()
        assert fresh["render"]["status"] == "unavailable"
        assert (await employee.get(f"/api/drafts/{meta['id']}/original/1")).status_code == 404

    async def test_save_edit_is_kept_and_validated(self, employee):
        meta = (await upload(employee)).json()
        book = (await employee.get(f"/api/drafts/{meta['id']}/book")).json()
        book["author"] = AUTHOR
        book["pages"][1]["blocks"][0]["text"] = "نص مصحح"
        saved = (await employee.put(f"/api/drafts/{meta['id']}/book", json=book)).json()
        assert saved["author"] == AUTHOR and saved["updatedBy"] == "emp"
        again = (await employee.get(f"/api/drafts/{meta['id']}/book")).json()
        assert again["pages"][1]["blocks"][0]["text"] == "نص مصحح"

        book["pages"][1]["pageNumber"] = ""  # a main page without its printed number
        saved = (await employee.put(f"/api/drafts/{meta['id']}/book", json=book)).json()
        assert issue_codes(saved), "an invalid book is saved but its errors are reported"

    async def test_reconvert_keeps_title_author_and_metadata(self, employee):
        meta = (await upload(employee)).json()
        book = (await employee.get(f"/api/drafts/{meta['id']}/book")).json()
        book.update(title=TITLE, author=AUTHOR)
        book["metadata"]["publisher"] = "دار الاختبار"
        book["pages"][0]["blocks"][1]["text"] = "تعديل سيُستبدل"
        await employee.put(f"/api/drafts/{meta['id']}/book", json=book)

        r = await employee.post(f"/api/drafts/{meta['id']}/reconvert", json={"blankPages": "2", "useToc": True})
        assert r.status_code == 200, r.text
        assert r.json()["options"]["blankPages"] == [2]
        book = (await employee.get(f"/api/drafts/{meta['id']}/book")).json()
        assert (book["title"], book["author"]) == (TITLE, AUTHOR)
        assert book["metadata"]["publisher"] == "دار الاختبار"
        assert len(book["pages"]) == 4  # the stated blank page was inserted
        assert "تعديل سيُستبدل" not in [b["text"] for p in book["pages"] for b in p["blocks"]]

    async def test_a_broken_file_fails_softly(self, employee):
        r = await upload(employee, "broken.docx", b"not a word file")
        assert r.status_code == 200
        meta = r.json()
        assert meta["status"] == "failed" and meta["error"]
        assert (await employee.get(f"/api/drafts/{meta['id']}/book")).status_code == 404

    async def test_delete(self, employee, workbench_root):
        meta = (await upload(employee)).json()
        assert (await employee.delete(f"/api/drafts/{meta['id']}")).status_code == 200
        assert not (workbench_root / "drafts" / meta["id"]).exists()


class TestSubmitReviewPublish:
    async def test_submit_requires_author(self, employee):
        meta = (await upload(employee)).json()
        r = await employee.post(f"/api/drafts/{meta['id']}/submit", data={"note": ""})
        assert r.status_code == 422
        assert "اسم المؤلف فارغ" in r.json()["detail"]["problems"]

    async def test_full_flow(self, employee, admin):
        meta = (await upload(employee)).json()
        draft_id = meta["id"]
        book = (await employee.get(f"/api/drafts/{draft_id}/book")).json()
        book.update(title=TITLE, author=AUTHOR)
        await employee.put(f"/api/drafts/{draft_id}/book", json=book)

        r = await employee.post(f"/api/drafts/{draft_id}/submit", data={"note": "جاهز"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "submitted"
        # Submitted: read-only for the employee until withdrawn or returned.
        assert (await employee.put(f"/api/drafts/{draft_id}/book", json=book)).status_code == 409

        page = await admin.get("/admin/drafts")
        assert page.status_code == 200 and TITLE in page.text and "بانتظار المراجعة (1)" in page.text
        detail = await admin.get(f"/admin/drafts/{draft_id}")
        assert "جاهز" in detail.text and "https://convert.example/#/d/" in detail.text

        # Sent back with a note, fixed, resubmitted.
        r = await admin.post(f"/admin/drafts/{draft_id}/return", data={"note": "صحح العنوان"})
        assert r.status_code == 303
        returned = (await employee.get(f"/api/drafts/{draft_id}")).json()
        assert returned["status"] == "returned" and returned["reviewNote"] == "صحح العنوان"
        book["title"] = "عنوان صحيح بعد التصحيح"
        assert (await employee.put(f"/api/drafts/{draft_id}/book", json=book)).status_code == 200
        assert (await employee.post(f"/api/drafts/{draft_id}/submit", data={})).status_code == 200

        r = await admin.post(f"/admin/drafts/{draft_id}/publish", data={})
        assert r.status_code == 303, r.text
        assert "?ok=" in unquote(r.headers["location"])
        published = (await employee.get(f"/api/drafts/{draft_id}")).json()
        assert published["status"] == "published"
        async with get_sessionmaker()() as s:
            row = (await s.execute(text("SELECT id, page_count, is_published FROM books WHERE id = :i"),
                                   {"i": published["publishedBookId"]})).one()
        assert row.page_count == 3 and row.is_published
        # Published: can't be edited, deleted or published twice.
        assert (await employee.delete(f"/api/drafts/{draft_id}")).status_code == 409
        again = await admin.post(f"/admin/drafts/{draft_id}/publish", data={})
        assert "err=" in again.headers["location"]

    async def test_withdraw_reopens_for_editing(self, employee):
        meta = (await upload(employee)).json()
        book = (await employee.get(f"/api/drafts/{meta['id']}/book")).json()
        book["author"] = AUTHOR
        await employee.put(f"/api/drafts/{meta['id']}/book", json=book)
        await employee.post(f"/api/drafts/{meta['id']}/submit", data={})
        r = await employee.post(f"/api/drafts/{meta['id']}/withdraw")
        assert r.json()["status"] == "editing"


def test_draft_ids_cannot_escape_the_folder(tmp_path):
    for bad in ["../x", "20260101-abcdef/../../x", "", "20260101-ABCDEF"]:
        with pytest.raises(drafts.DraftNotFound):
            drafts.draft_dir(tmp_path, bad)


class TestPageMap:
    """Converted pages located in the rendered original by their words: LibreOffice breaks
    pages differently from Word (a 156-page book rendered as 278), so page N of the render
    is not page N of the book."""

    @staticmethod
    def page(text):
        return {"blocks": [{"type": "text", "text": text}]}

    def words(self, n, tag):
        return " ".join(f"كلمة{tag}{i}" for i in range(n))

    def test_each_page_spans_its_own_rendered_pages(self):
        from workbench.conversion import page_map, text_words
        p1, p2, p3 = self.words(40, "ا"), self.words(40, "ب"), self.words(10, "ج")
        # The render: page 1's text spills onto a second rendered page, page 2 fits.
        w1 = text_words(p1)
        pdf = [w1[:30], w1[30:], text_words(p2), text_words(p3)]
        book = {"pages": [self.page(p1), self.page(p2), self.page(p3)]}
        assert page_map(book, pdf) == [[1, 2, 1], [3, 3, 3], [4, 4, 4]]

    def test_the_page_shown_is_the_one_holding_most_of_the_text(self):
        """The PDF breaks a few lines earlier than the conversion: page 2's first lines sit
        at the bottom of PDF page 1. Page 2 is still PDF page 2, not 1."""
        from workbench.conversion import page_map, text_words
        p1, p2 = self.words(40, "ا"), self.words(40, "ب")
        w1, w2 = text_words(p1), text_words(p2)
        pdf = [w1 + w2[:5], w2[5:]]
        assert page_map({"pages": [self.page(p1), self.page(p2)]}, pdf) == [[1, 1, 1], [1, 2, 2]]

    def test_fragmented_extraction_still_finds_the_page(self):
        """PDF text of justified Arabic comes out split mid-word ("وس ار قاص دا"); the
        page whose text it is must still be the one shown."""
        from workbench.conversion import page_map, text_words
        texts = ["وسار قاصدا كربلاء لقتال الامام الحسين في جيش عظيم من اهل الكوفة " * 3,
                 "فان صدقوا فيما يقولون انني ساعطيهم الامان واكتب الى الامير بذلك " * 3,
                 "ثم نزل الحسين بارض كربلاء في اليوم الثاني من المحرم سنة احدى وستين " * 3]
        def fragment(t):  # break every word after its second letter, as extraction does
            return " ".join(w[:2] + " " + w[2:] if len(w) > 3 else w for w in t.split())
        pdf = [text_words(fragment(t)) for t in texts]
        book = {"pages": [self.page(t) for t in texts]}
        assert [x[2] for x in page_map(book, pdf)] == [1, 2, 3]

    def test_pages_without_text_sit_between_their_neighbours(self):
        from workbench.conversion import page_map, text_words
        p1, p3 = self.words(20, "ا"), self.words(20, "ج")
        pdf = [text_words(p1), [], text_words(p3)]
        book = {"pages": [self.page(p1), self.page(""), self.page(p3)]}
        assert page_map(book, pdf) == [[1, 1, 1], [2, 2, 2], [3, 3, 3]]

    def test_a_repeated_phrase_far_ahead_does_not_derail_the_rest(self):
        from workbench.conversion import page_map, text_words
        common = "قال رسول الله صلى الله عليه واله"
        pages = [f"{self.words(30, t)} {common}" for t in "ابجد"]
        pdf = [text_words(p) for p in pages]
        # Page 2's ending is garbled in the render (as two-column verse extracts): its end
        # can't be found nearby, and must not be matched to a later page's same phrase.
        pdf[1] = text_words(self.words(30, "ب")) + ["مختلف", "تماما", "هنا"]
        book = {"pages": [self.page(p) for p in pages]}
        assert page_map(book, pdf) == [[1, 1, 1], [2, 2, 2], [3, 3, 3], [4, 4, 4]]

    def test_diacritics_and_presentation_forms_still_match(self):
        from workbench.conversion import page_map, text_words
        page_text = "قالَ الإمامُ الصادقُ عليه السلامُ العلمُ نورٌ يقذفه اللهُ"
        # PDF extraction yields presentation forms; the converted page carries tashkeel.
        import unicodedata
        extracted = "ﻗﺎﻝ ﺍﻻﻣﺎﻡ ﺍﻟﺼﺎﺩﻕ ﻋﻠﻴﻪ ﺍﻟﺴﻼﻡ ﺍﻟﻌﻠﻢ ﻧﻮﺭ ﻳﻘﺬﻓﻪ ﺍﻟﻠﻪ"
        assert unicodedata.normalize("NFKC", extracted) != extracted
        assert page_map({"pages": [self.page(page_text)]}, [text_words(extracted)]) == [[1, 1, 1]]

    async def test_endpoint_is_empty_until_the_original_is_rendered(self, employee):
        meta = (await upload(employee)).json()
        r = await employee.get(f"/api/drafts/{meta['id']}/pagemap")
        assert r.status_code == 200 and r.json() == {"pages": []}


class TestFollowsWord:
    def test_page_break_right_after_a_new_page_section_starts_no_extra_page(self, tmp_path):
        """Word starts one page, not two, for a next-page section break followed by a
        manual page break -- a real book gained a blank page (and every later page number
        shifted by one) when both were counted."""
        from docx.enum.section import WD_SECTION
        from workbench.conversion import converter
        d = docx.Document()
        d.add_paragraph("الصفحة الأولى")
        d.add_section(WD_SECTION.NEW_PAGE)
        d.add_page_break()
        d.add_paragraph("الصفحة الثانية")
        d.add_page_break()
        d.add_paragraph("الصفحة الثالثة")
        d.save(tmp_path / "b.docx")
        c = converter()
        pages = c._split_pages(c.read_docx(tmp_path / "b.docx"))
        texts = [" ".join(p.text for p in pg) for pg in pages]
        assert texts == ["الصفحة الأولى", "الصفحة الثانية", "الصفحة الثالثة"]

    def test_continuous_section_break_starts_no_page(self, tmp_path):
        """The break after a section is decided by the section that follows it: a
        continuous one goes on on the same page."""
        from docx.enum.section import WD_SECTION
        from workbench.conversion import converter
        d = docx.Document()
        d.add_paragraph("أول الصفحة")
        d.add_section(WD_SECTION.CONTINUOUS)
        d.add_paragraph("آخر الصفحة")
        d.add_page_break()
        d.add_paragraph("الصفحة الثانية")
        d.save(tmp_path / "b.docx")
        c = converter()
        pages = c._split_pages(c.read_docx(tmp_path / "b.docx"))
        assert [" ".join(p.text for p in pg) for pg in pages] == ["أول الصفحة آخر الصفحة", "الصفحة الثانية"]

    def test_doc_section_marks_of_continuous_sections_are_not_pages(self):
        """In a .doc a section mark is the same \\x0c as a page break; only the section
        table (PlcfSed + each SEPX's sprmSBkc) tells them apart."""
        import struct
        from workbench.conversion import converter
        c = converter()
        # Three sections ending at CPs 10, 20, 30: the second is continuous, the third
        # new-page, so only the mark ending the first section (CP 9) is inline.
        sepx_at = 0x200
        sepx = struct.pack("<HHB", 3, 0x3009, 0)  # cb=3, sprmSBkc = continuous
        wd = bytearray(0x300)
        wd[sepx_at:sepx_at + len(sepx)] = sepx
        cps = struct.pack("<4I", 0, 10, 20, 30)
        seds = b"".join(struct.pack("<hIhI", 0, fc, 0, 0) for fc in (0xFFFFFFFF, sepx_at, 0xFFFFFFFF))
        tbl = cps + seds
        struct.pack_into("<II", wd, 0x00CA, 0, len(tbl))
        assert c._inline_section_marks(bytes(wd), tbl) == {9}

    def test_index_line_matching_a_sentence_far_away_is_ignored(self):
        """An index entry whose words begin a sentence hundreds of pages on numbered a real
        book's last pages 159, 160, ... after 557. Anchors must form a possible sequence;
        the wrong one is dropped and its sentence is not made a heading."""
        from workbench.conversion import converter
        c = converter()
        P = c.Para
        pages = [[P(f"نص الصفحة رقم {k} من الكتاب", "Normal")] for k in range(1, 81)]
        pages[1].insert(0, P("الفصل الأول في العلم", "Heading 1"))
        pages[4].insert(0, P("الفصل الثاني في العمل", "Heading 1"))
        sentence = P("الخاتمة في ذكر الشهادة كما رواه الثقات", "Normal")
        pages[74].append(sentence)
        pages[78] = [P("الفصل الأول في العلم 2", "TOC 2"), P("الفصل الثاني في العمل 5", "TOC 2")]
        pages[79] = [P("الخاتمة في ذكر الشهادة 9", "TOC 2")]
        anchors, _, _ = c.anchor_toc(pages)
        assert (75, 9) not in anchors
        assert c.page_labels(len(pages), anchors) == list(range(1, 81))
        assert not sentence.heading

    def test_alphabetical_indexes_styled_toc_are_not_the_contents(self):
        """A real book styled its hadith/verse/name indexes as TOC lines too (2,700 of
        them) and had a short summary at the front: only the full contents, whose numbers
        rise through the book, is used."""
        from workbench.conversion import converter
        c = converter()
        summary = [("s", n, "s") for n in (9, 15, 145, 233, 371)]
        index = [("i", n, "i") for n in (245, 12, 388, 97, 150, 3, 410, 77, 260, 31, 199, 5)]
        contents = [("c", n, "c") for n in range(2, 400, 12)]
        misprint = [("c", 121, "c"), ("c", 65, "c")]  # one line out of order inside it
        entries = summary + index + contents[:15] + misprint + contents[15:]
        assert c._contents_run(entries) == contents[:15] + misprint + contents[15:]

    def test_contents_at_the_front_is_searched_for_in_the_pages_after_it(self):
        from workbench.conversion import converter
        c = converter()
        P = c.Para
        pages = [[P(f"نص الصفحة رقم {k} من الكتاب", "Normal")] for k in range(1, 21)]
        pages[1] = [P("الفصل الأول في العلم 5", "TOC 2"), P("الفصل الثاني في العمل 12", "TOC 2")]
        pages[4].insert(0, P("الفصل الأول في العلم", "Normal"))
        pages[11].insert(0, P("الفصل الثاني في العمل", "Normal"))
        anchors, _, _ = c.anchor_toc(pages)
        assert anchors == [(5, 5), (12, 12)]
        assert pages[4][0].heading and pages[11][0].heading

    def test_page_checks_point_at_likely_break_mistakes(self):
        """Pages come only from the Word file's breaks, so a typesetter's slip is copied into
        the book; the checks point the employee at it (and nowhere else)."""
        from workbench.conversion import page_checks
        full = "نص " * 330  # a normal page of about 1000 letters

        def page(n, *blocks):
            return {"pageType": "main", "pageNumber": str(n), "blocks": [{"type": t, "text": x} for t, x in blocks]}

        pages = [page(n, ("text", full)) for n in range(1, 31)]
        pages[5] = page(6, ("text", full * 2))                                # two pages in one
        pages[12] = page(13, ("text", "سطر قصير"))                             # half a page
        pages[20] = page(21, ("text", "آخر الفصل"))                            # a chapter's end ...
        pages[21] = page(22, ("text", "الفصل الثاني في العمل"), ("text", full))  # ... then a chapter
        pages[25] = page(26, ("text", "قليل"), ("footnotes", "(1) " + full * 2))  # mostly footnotes
        for k in range(27, 30):
            pages[k] = page(k + 2, ("text", full))                              # 27 then 29: a skip
        warnings = {(w["code"], w["page"]) for w in page_checks({"pages": pages})}
        assert warnings == {("long-page", 5), ("short-page", 12), ("number-skip", 27)}

    def test_doc_poem_table_becomes_one_block_per_verse(self):
        """A .doc poem is a table of half-verse cells in reading order; glued together it
        was one run-on paragraph. Each cell's own style counts, not the paragraph's after
        the table (often a heading, which made the whole poem a heading)."""
        from workbench.conversion import converter
        c = converter()
        part = ("لو كانَ يقعدُ فوقَ الشمسِ من كرمٍ  \x07\x07قومٌ بأوّلهم أو مجدهم قعدوا  \x07\x07"
                "قومٌ أبوهم سنانٌ حين تنسِبُهم\x07\x07طابوا وطابَ من الأولادِ ما ولدوا\x07\x07فقال عمر : أحسن")
        paras = c._table_paras(part, "Heading 1", ["rfdPoem"] * 8)
        assert [(p.text, p.style) for p in paras] == [
            ("لو كانَ يقعدُ فوقَ الشمسِ من كرمٍ * قومٌ بأوّلهم أو مجدهم قعدوا", "rfdPoem"),
            ("قومٌ أبوهم سنانٌ حين تنسِبُهم * طابوا وطابَ من الأولادِ ما ولدوا", "rfdPoem"),
            ("فقال عمر : أحسن", "Heading 1"),
        ]

    def test_index_table_cells_are_not_paired_as_verses(self):
        from workbench.conversion import converter
        c = converter()
        paras = c._table_paras("للصحن العباسيّ\x07223\x07\x07أنصاب الحرم\x0741\x07\x07", "rfdVar0", ["rfdVar0"] * 6)
        assert [p.text for p in paras] == ["للصحن العباسيّ", "223", "أنصاب الحرم", "41"]

    def test_poem_lines_kept_and_footnote_poems_stay_footnotes(self):
        from workbench.conversion import converter
        c = converter()
        P = c.Para
        poem = P("فادح شبَّ في الحشى بأوار  ومصاب قد حَطّ كُلّ مناري  يوم نادى العلاء والدمع جاري",
                 "rfdPoemFootnoteCenter")
        prose = P("سطر أول سطر ثان", "rfdNormal0")
        abx = c.build_abx([[P("نص الصفحة", "rfdNormal0"), prose, poem]], "t", "a", 0, [1], c.HEADING_STYLE_RE)
        assert "سطر أول سطر ثان" in abx  # a soft break in prose is layout
        for line in ("فادح شبَّ في الحشى بأوار", "ومصاب قد حَطّ كُلّ مناري", "يوم نادى العلاء والدمع جاري"):
            assert f"< هامش > {line} < / هامش >" in abx

    def test_toc_line_with_a_page_range_points_at_its_first_page(self):
        from workbench.conversion import converter
        c = converter()
        m = c._TOC_RANGE_RE.match("البابُ الأوّل : هويّةُ العَبّاس الشَّخْصِيَّةُ 17 ـ 127")
        assert m and m.group(2) == "17"

    def test_consistent_anchors_allow_drift_only_by_empty_pages(self):
        from workbench.conversion import converter
        c = converter()
        P = c.Para
        pages = [[P("نص", "Normal")] for _ in range(20)]
        pages[9] = []  # one empty page between file pages 5 and 15
        # The file runs one page ahead after the empty page: fine. Two ahead: impossible.
        assert c._consistent_anchors(pages, [(5, 5), (15, 14)]) == [(5, 5), (15, 14)]
        assert len(c._consistent_anchors(pages, [(5, 5), (15, 13)])) == 1
        # Numbers going backwards never survive.
        assert c._consistent_anchors(pages, [(2, 2), (5, 5), (8, 3), (12, 12)]) == [(2, 2), (5, 5), (12, 12)]

    def test_two_manual_breaks_still_make_a_blank_page(self, tmp_path):
        from workbench.conversion import converter
        d = docx.Document()
        d.add_paragraph("قبل")
        d.add_page_break()
        d.add_page_break()
        d.add_paragraph("بعد")
        d.save(tmp_path / "b.docx")
        c = converter()
        assert len(c._split_pages(c.read_docx(tmp_path / "b.docx"))) == 3

    def test_preview_stretches_page_height_only(self, tmp_path):
        import re
        import zipfile
        from workbench.conversion import stretch_pages
        d = docx.Document()
        d.add_paragraph("نص")
        d.save(tmp_path / "a.docx")
        stretch_pages(tmp_path / "a.docx", tmp_path / "b.docx", 1.5)
        def size(path):
            xml = zipfile.ZipFile(path).read("word/document.xml").decode()
            return re.search(r'<w:pgSz[^>]*w:w="(\d+)"[^>]*w:h="(\d+)"', xml).groups()
        (w1, h1), (w2, h2) = size(tmp_path / "a.docx"), size(tmp_path / "b.docx")
        assert w1 == w2 and int(h2) == round(int(h1) * 1.5)
        assert docx.Document(tmp_path / "b.docx").paragraphs[0].text == "نص"


class TestComparePdf:
    """An uploaded PDF as the original to compare against -- one saved from Word itself
    matches Word exactly, which a LibreOffice render can't."""

    @pytest.fixture
    def fake_pdf_tools(self, monkeypatch):
        # pdfinfo/pdftotext aren't installed on dev machines; the workbench's own logic is
        # what's under test here (the image has the real tools).
        def use_pdf(folder, data):
            if not data.startswith(b"%PDF"):
                raise ValueError("الملف ليس PDF")
            (folder / "original.pdf").write_bytes(data)
            return 3
        monkeypatch.setattr(wb.conversion, "use_pdf", use_pdf)
        monkeypatch.setattr(wb.conversion, "extract_original_words",
                            lambda folder, pages: (folder / "original_words.json").write_text("[[], [], []]"))

    async def test_upload_a_pdf_for_an_existing_book(self, employee, fake_pdf_tools):
        meta = (await upload(employee)).json()
        r = await employee.post(f"/api/drafts/{meta['id']}/pdf", files={"file": ("كتاب.pdf", b"%PDF-1.4 x")})
        assert r.status_code == 200, r.text
        render = (await employee.get(f"/api/drafts/{meta['id']}")).json()["render"]
        assert render == {"status": "done", "pages": 3, "error": None, "source": "pdf", "pdfName": "كتاب.pdf"}

    async def test_pdf_given_with_the_word_file(self, employee, fake_pdf_tools):
        r = await employee.post("/api/drafts", files={"file": ("كتاب.docx", make_docx()),
                                                      "pdf": ("كتاب.pdf", b"%PDF-1.4 x")})
        assert r.status_code == 200, r.text
        assert r.json()["report"]["pages"] == 3
        render = (await employee.get(f"/api/drafts/{r.json()['id']}")).json()["render"]
        assert render["source"] == "pdf" and render["status"] == "done"

    async def test_not_a_pdf_is_refused(self, employee, fake_pdf_tools):
        meta = (await upload(employee)).json()
        r = await employee.post(f"/api/drafts/{meta['id']}/pdf", files={"file": ("x.pdf", b"hello")})
        assert r.status_code == 400

    async def test_back_to_the_word_render(self, employee, fake_pdf_tools):
        meta = (await upload(employee)).json()
        await employee.post(f"/api/drafts/{meta['id']}/pdf", files={"file": ("x.pdf", b"%PDF-1.4 x")})
        r = await employee.post(f"/api/drafts/{meta['id']}/render")
        assert r.json()["render"]["source"] == "word"

    def test_scanned_pdf_pairs_pages_by_number(self):
        from workbench.conversion import page_map
        book = {"pages": [{"blocks": [{"type": "text", "text": " ".join(["كلمة"] * 200)}]}] * 3}
        assert page_map(book, [[], [], ["غلاف"]]) == []  # no text to match: by number instead
