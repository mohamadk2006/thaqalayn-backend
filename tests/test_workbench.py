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
        assert page_map(book, pdf) == [[1, 2], [3, 3], [4, 4]]

    def test_pages_without_text_sit_between_their_neighbours(self):
        from workbench.conversion import page_map, text_words
        p1, p3 = self.words(20, "ا"), self.words(20, "ج")
        pdf = [text_words(p1), [], text_words(p3)]
        book = {"pages": [self.page(p1), self.page(""), self.page(p3)]}
        assert page_map(book, pdf) == [[1, 1], [2, 2], [3, 3]]

    def test_a_repeated_phrase_far_ahead_does_not_derail_the_rest(self):
        from workbench.conversion import page_map, text_words
        common = "قال رسول الله صلى الله عليه واله"
        pages = [f"{self.words(30, t)} {common}" for t in "ابجد"]
        pdf = [text_words(p) for p in pages]
        # Page 2's ending is garbled in the render (as two-column verse extracts): its end
        # can't be found nearby, and must not be matched to a later page's same phrase.
        pdf[1] = text_words(self.words(30, "ب")) + ["مختلف", "تماما", "هنا"]
        book = {"pages": [self.page(p) for p in pages]}
        assert page_map(book, pdf) == [[1, 1], [2, 2], [3, 3], [4, 4]]

    def test_diacritics_and_presentation_forms_still_match(self):
        from workbench.conversion import page_map, text_words
        page_text = "قالَ الإمامُ الصادقُ عليه السلامُ العلمُ نورٌ يقذفه اللهُ"
        # PDF extraction yields presentation forms; the converted page carries tashkeel.
        import unicodedata
        extracted = "ﻗﺎﻝ ﺍﻻﻣﺎﻡ ﺍﻟﺼﺎﺩﻕ ﻋﻠﻴﻪ ﺍﻟﺴﻼﻡ ﺍﻟﻌﻠﻢ ﻧﻮﺭ ﻳﻘﺬﻓﻪ ﺍﻟﻠﻪ"
        assert unicodedata.normalize("NFKC", extracted) != extracted
        assert page_map({"pages": [self.page(page_text)]}, [text_words(extracted)]) == [[1, 1]]

    async def test_endpoint_is_empty_until_the_original_is_rendered(self, employee):
        meta = (await upload(employee)).json()
        r = await employee.get(f"/api/drafts/{meta['id']}/pagemap")
        assert r.status_code == 200 and r.json() == {"pages": []}
