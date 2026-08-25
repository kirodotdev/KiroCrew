"""Tests for the file-explorer viewer endpoints and document search.

Covers the /raw streaming endpoint, /extract structured Office extraction
(office_extract module), the document-content search pass, the markdown-only
POST /write endpoint, and proxy-signature verification for request targets
that are not requote-stable (paths containing spaces).

All Office fixtures are synthetic, generated in-test via zipfile — no binary
fixture files are checked in.
"""

import io
import os
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.apps.builtins.file_explorer import office_extract, server

# ── Synthetic document builders ──

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

_DOCX_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Quarterly Review</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>The unique keyword is </w:t></w:r><w:r><w:t>zanzibar</w:t></w:r></w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Region</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Revenue</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>APAC</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>42</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
"""

_XLSX_WORKBOOK_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""

_XLSX_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
    Target="worksheets/sheet1.xml"/>
</Relationships>
"""

_XLSX_SHARED_STRINGS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="2" uniqueCount="2">
  <si><t>Widget</t></si>
  <si><t>flumplenook</t></si>
</sst>
"""

_XLSX_SHEET_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1" t="s"><v>0</v></c>
      <c r="B1"><v>42</v></c>
      <c r="D1" t="b"><v>1</v></c>
    </row>
    <row r="2">
      <c r="A2" t="s"><v>1</v></c>
      <c r="B2"><f>B1*2</f><v>84</v></c>
      <c r="C2" t="inlineStr"><is><t>inline text</t></is></c>
    </row>
  </sheetData>
</worksheet>
"""

_PPTX_PRESENTATION_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:sldSz cx="9144000" cy="6858000"/>
</p:presentation>
"""

_PPTX_THEME_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="T">
  <a:themeElements>
    <a:clrScheme name="S">
      <a:dk1><a:sysClr val="windowText" lastClr="111111"/></a:dk1>
      <a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>
      <a:dk2><a:srgbClr val="222222"/></a:dk2>
      <a:lt2><a:srgbClr val="EEEEEE"/></a:lt2>
      <a:accent1><a:srgbClr val="FF0000"/></a:accent1>
      <a:accent2><a:srgbClr val="00FF00"/></a:accent2>
    </a:clrScheme>
  </a:themeElements>
</a:theme>
"""

_PPTX_SLIDE1_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:cSld>
    <p:bg><p:bgPr><a:solidFill><a:srgbClr val="161D26"/></a:solidFill></p:bgPr></p:bg>
    <p:spTree>
      <p:sp>
        <p:spPr>
          <a:xfrm><a:off x="914400" y="685800"/><a:ext cx="4572000" cy="1371600"/></a:xfrm>
        </p:spPr>
        <p:txBody>
          <a:p>
            <a:pPr algn="ctr"/>
            <a:r>
              <a:rPr sz="4400" b="1"><a:solidFill><a:schemeClr val="accent1"/></a:solidFill></a:rPr>
              <a:t>Big Title</a:t>
            </a:r>
          </a:p>
          <a:p><a:r><a:rPr sz="1800"/><a:t>kumquat point</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
      <p:pic>
        <p:blipFill><a:blip r:embed="rId2"/></p:blipFill>
        <p:spPr>
          <a:xfrm><a:off x="457200" y="3429000"/><a:ext cx="2286000" cy="1714500"/></a:xfrm>
        </p:spPr>
      </p:pic>
    </p:spTree>
  </p:cSld>
</p:sld>
"""

_PPTX_SLIDE1_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId2"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
    Target="../media/image1.png"/>
</Relationships>
"""


def make_docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", _DOCX_DOCUMENT_XML)


def make_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml", _XLSX_WORKBOOK_XML)
        zf.writestr("xl/_rels/workbook.xml.rels", _XLSX_RELS_XML)
        zf.writestr("xl/sharedStrings.xml", _XLSX_SHARED_STRINGS_XML)
        zf.writestr("xl/worksheets/sheet1.xml", _XLSX_SHEET_XML)


def make_pptx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ppt/presentation.xml", _PPTX_PRESENTATION_XML)
        zf.writestr("ppt/theme/theme1.xml", _PPTX_THEME_XML)
        zf.writestr("ppt/slides/slide1.xml", _PPTX_SLIDE1_XML)
        zf.writestr("ppt/slides/_rels/slide1.xml.rels", _PPTX_SLIDE1_RELS_XML)
        zf.writestr("ppt/media/image1.png", _PNG_BYTES)


def make_pdf(path: Path, text: str) -> None:
    """Write a minimal single-page PDF with an uncompressed text stream."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_at = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n".encode()
    )
    path.write_bytes(out.getvalue())


# ── Fixtures ──


@pytest.fixture
def doc_tree(tmp_path):
    """A directory of synthetic documents inside the allowed roots."""
    make_docx(tmp_path / "report.docx")
    make_xlsx(tmp_path / "figures.xlsx")
    make_pptx(tmp_path / "deck.pptx")
    make_pdf(tmp_path / "paper.pdf", "xylophone finding")
    (tmp_path / "notes.md").write_text("# Notes\n\nplain zanzibar text\n")
    (tmp_path / "data.txt").write_text("just text\n")
    (tmp_path / "photo.png").write_bytes(_PNG_BYTES)
    make_docx(tmp_path / "~$report.docx")  # Office lock file — must be skipped
    (tmp_path / ".ssh").mkdir()
    make_docx(tmp_path / ".ssh" / "secret.docx")
    return tmp_path


@pytest.fixture(autouse=True)
def patch_allowed_roots(doc_tree):
    """Allow the tmp tree in ALLOWED_ROOTS and neutralise audit side effects."""

    def mock_is_sensitive(path_str):
        return any(s in Path(path_str).parts for s in server.SENSITIVE_DIRS)

    with patch.object(server, "ALLOWED_ROOTS", [doc_tree]):
        with patch.object(server, "_HOME", doc_tree):
            with patch.object(server, "is_sensitive_path", mock_is_sensitive):
                with patch.object(server, "sel", MagicMock()):
                    yield


class _ByteCapture:
    """Captures a handler's raw byte response (status, headers, body)."""

    def __init__(self):
        self.status = None
        self.headers = {}
        self.body = io.BytesIO()
        self.json_responses = []


def _byte_request(path, method="GET", post_body=None):
    handler = server.FileExplorerHandler.__new__(server.FileExplorerHandler)
    handler.path = path
    cap = _ByteCapture()
    handler.send_response = lambda code: setattr(cap, "status", code)
    handler.send_header = lambda k, v: cap.headers.__setitem__(k, v)
    handler.end_headers = lambda: None
    handler.wfile = cap.body
    handler._json = lambda code, payload: cap.json_responses.append((code, payload))
    if post_body is not None:
        handler._post_body = post_body
    try:
        handler._dispatch(method)
    except server.PathError as exc:
        handler._json(exc.status, {"error": str(exc)})
    return cap


def _json_request(path, method="GET", post_body=None):
    cap = _byte_request(path, method=method, post_body=post_body)
    assert cap.json_responses, f"expected a JSON response, got bytes {cap.status}"
    return cap.json_responses[0]


# ── /extract: structured Office extraction ──


class TestOfficeExtract:
    def test_docx_blocks(self, doc_tree):
        out = office_extract.extract_structured(doc_tree / "report.docx")
        assert out["kind"] == "docx"
        kinds = [b["type"] for b in out["blocks"]]
        assert kinds[0] == "h1"
        assert out["blocks"][0]["text"] == "Quarterly Review"
        assert any("zanzibar" in b.get("text", "") for b in out["blocks"])
        table = next(b for b in out["blocks"] if b["type"] == "table")
        assert table["rows"][0] == ["Region", "Revenue"]
        assert table["rows"][1] == ["APAC", "42"]

    def test_xlsx_grid(self, doc_tree):
        out = office_extract.extract_structured(doc_tree / "figures.xlsx")
        assert out["kind"] == "xlsx"
        sheet = out["sheets"][0]
        assert sheet["name"] == "Data"
        # shared string, number, column gap (C skipped), boolean
        assert sheet["rows"][0] == ["Widget", "42", "", "TRUE"]
        # shared string, cached formula value, inline string
        assert sheet["rows"][1] == ["flumplenook", "84", "inline text"]
        assert sheet["truncated"] is False

    def test_pptx_slides(self, doc_tree):
        out = office_extract.extract_structured(doc_tree / "deck.pptx")
        assert out["kind"] == "pptx"
        assert out["slideW"] == 9144000
        slide = out["slides"][0]
        assert slide["bg"] == "#161D26"
        text_shape = next(s for s in slide["shapes"] if s["kind"] == "text")
        # 914400/9144000 → 10% of slide width
        assert text_shape["x"] == 10.0
        title_run = text_shape["paras"][0]["runs"][0]
        assert title_run["t"] == "Big Title"
        assert title_run["b"] is True
        assert title_run["sz"] == 44.0
        assert title_run["c"] == "#FF0000"  # accent1 resolved from the theme
        assert text_shape["paras"][0]["algn"] == "ctr"
        image = next(s for s in slide["shapes"] if s["kind"] == "image")
        assert image["member"] == "ppt/media/image1.png"
        assert "Big Title" in slide["lines"]
        assert "kumquat point" in slide["lines"]

    def test_non_office_rejected(self, doc_tree):
        with pytest.raises(office_extract.OfficeExtractError):
            office_extract.extract_structured(doc_tree / "notes.md")

    def test_corrupt_zip_rejected(self, doc_tree):
        bad = doc_tree / "broken.docx"
        bad.write_bytes(b"this is not a zip archive")
        with pytest.raises(office_extract.OfficeExtractError):
            office_extract.extract_structured(bad)

    def test_media_member_ok(self, doc_tree):
        data, fname = office_extract.media_member(doc_tree / "deck.pptx", "ppt/media/image1.png")
        assert data.startswith(b"\x89PNG")
        assert fname == "image1.png"

    def test_media_member_traversal_rejected(self, doc_tree):
        with pytest.raises(office_extract.OfficeExtractError) as exc_info:
            office_extract.media_member(doc_tree / "deck.pptx", "ppt/media/../../etc/passwd")
        assert exc_info.value.status == 403

    def test_media_member_outside_media_rejected(self, doc_tree):
        with pytest.raises(office_extract.OfficeExtractError) as exc_info:
            office_extract.media_member(doc_tree / "deck.pptx", "ppt/slides/slide1.xml")
        assert exc_info.value.status == 403


class TestExtractEndpoint:
    def test_extract_docx_json(self, doc_tree):
        code, body = _json_request(f"/extract?path={doc_tree}/report.docx")
        assert code == 200
        assert body["kind"] == "docx"

    def test_extract_non_office_415(self, doc_tree):
        code, body = _json_request(f"/extract?path={doc_tree}/notes.md")
        assert code == 415

    def test_extract_oversize_413(self, doc_tree):
        with patch.object(server, "MAX_EXTRACT_BYTES", 10):
            code, body = _json_request(f"/extract?path={doc_tree}/report.docx")
        assert code == 413

    def test_extract_member_streams_image_inline(self, doc_tree):
        cap = _byte_request(f"/extract?path={doc_tree}/deck.pptx&member=ppt/media/image1.png")
        assert cap.status == 200
        assert cap.headers["Content-Type"] == "image/png"
        assert cap.headers["Content-Disposition"] == "inline"
        assert cap.body.getvalue().startswith(b"\x89PNG")

    def test_extract_member_traversal_403(self, doc_tree):
        code, body = _json_request(f"/extract?path={doc_tree}/deck.pptx&member=ppt/media/../secret")
        assert code == 403


# ── /raw: byte streaming ──


class TestRawEndpoint:
    def test_png_inline(self, doc_tree):
        cap = _byte_request(f"/raw?path={doc_tree}/photo.png")
        assert cap.status == 200
        assert cap.headers["Content-Type"] == "image/png"
        assert cap.headers["Content-Disposition"] == "inline"
        assert cap.headers["X-Content-Type-Options"] == "nosniff"
        assert cap.body.getvalue() == _PNG_BYTES

    def test_pdf_inline(self, doc_tree):
        cap = _byte_request(f"/raw?path={doc_tree}/paper.pdf")
        assert cap.status == 200
        assert cap.headers["Content-Type"] == "application/pdf"
        assert cap.headers["Content-Disposition"] == "inline"
        assert cap.body.getvalue().startswith(b"%PDF-")

    def test_non_viewable_forced_to_attachment(self, doc_tree):
        cap = _byte_request(f"/raw?path={doc_tree}/figures.xlsx")
        assert cap.status == 200
        assert cap.headers["Content-Type"] == "application/octet-stream"
        assert "attachment" in cap.headers["Content-Disposition"]

    def test_download_param_forces_attachment(self, doc_tree):
        cap = _byte_request(f"/raw?path={doc_tree}/photo.png&download=1")
        assert "attachment" in cap.headers["Content-Disposition"]
        assert 'filename="photo.png"' in cap.headers["Content-Disposition"]

    def test_oversize_413(self, doc_tree):
        with patch.object(server, "MAX_RAW_BYTES", 4):
            code, body = _json_request(f"/raw?path={doc_tree}/photo.png")
        assert code == 413

    def test_sensitive_denied(self, doc_tree):
        code, body = _json_request(f"/raw?path={doc_tree}/.ssh/secret.docx")
        assert code == 403


# ── /write: markdown editing ──


class TestWriteEndpoint:
    def test_roundtrip(self, doc_tree):
        target = doc_tree / "notes.md"
        code, body = _json_request(f"/write?path={target}", method="POST", post_body=b"# Edited\n")
        assert code == 200
        assert body["ok"] is True
        assert target.read_text() == "# Edited\n"
        assert body["mtime"] == int(target.stat().st_mtime)

    def test_stale_mtime_409(self, doc_tree):
        target = doc_tree / "notes.md"
        original = target.read_text()
        stale = int(target.stat().st_mtime) - 100
        code, body = _json_request(
            f"/write?path={target}&base_mtime={stale}",
            method="POST",
            post_body=b"# Clobber\n",
        )
        assert code == 409
        assert target.read_text() == original

    def test_fresh_mtime_accepted(self, doc_tree):
        target = doc_tree / "notes.md"
        fresh = int(target.stat().st_mtime)
        code, body = _json_request(
            f"/write?path={target}&base_mtime={fresh}",
            method="POST",
            post_body=b"# Fresh\n",
        )
        assert code == 200
        assert target.read_text() == "# Fresh\n"

    def test_non_markdown_415(self, doc_tree):
        code, body = _json_request(
            f"/write?path={doc_tree}/data.txt", method="POST", post_body=b"x"
        )
        assert code == 415

    def test_no_create_404(self, doc_tree):
        code, body = _json_request(
            f"/write?path={doc_tree}/new-file.md", method="POST", post_body=b"x"
        )
        assert code == 404

    def test_sensitive_denied(self, doc_tree):
        (doc_tree / ".ssh" / "s.md").write_text("x")
        code, body = _json_request(
            f"/write?path={doc_tree}/.ssh/s.md", method="POST", post_body=b"y"
        )
        assert code == 403

    def test_unknown_post_route_404(self, doc_tree):
        code, body = _json_request("/frobnicate", method="POST", post_body=b"")
        assert code == 404


# ── Document search pass ──


class TestDocumentSearch:
    def test_finds_word_inside_docx(self, doc_tree):
        results = server._search_documents(doc_tree, "zanzibar", "", "", 50)
        docx_hits = [r for r in results if r["file"].endswith("report.docx")]
        assert docx_hits, results
        assert "zanzibar" in docx_hits[0]["preview"]

    def test_finds_word_inside_xlsx_with_label(self, doc_tree):
        results = server._search_documents(doc_tree, "flumplenook", "", "", 50)
        assert results
        assert results[0]["file"].endswith("figures.xlsx")
        assert results[0]["preview"].startswith("[Data r")

    def test_finds_word_inside_pptx_with_slide_label(self, doc_tree):
        results = server._search_documents(doc_tree, "kumquat", "", "", 50)
        assert results
        assert results[0]["file"].endswith("deck.pptx")
        assert results[0]["preview"].startswith("[slide 1]")

    def test_finds_word_inside_pdf(self, doc_tree):
        pytest.importorskip("pdfminer")
        results = server._search_documents(doc_tree, "xylophone", "", "", 50)
        pdf_hits = [r for r in results if r["file"].endswith("paper.pdf")]
        assert pdf_hits, results

    def test_lock_files_skipped(self, doc_tree):
        results = server._search_documents(doc_tree, "zanzibar", "", "", 50)
        assert not any("~$" in r["file"] for r in results)

    def test_sensitive_documents_skipped(self, doc_tree):
        results = server._search_documents(doc_tree, "zanzibar", "", "", 50)
        assert not any(".ssh" in r["file"] for r in results)

    def test_result_shape_matches_text_search(self, doc_tree):
        results = server._search_documents(doc_tree, "zanzibar", "", "", 50)
        for r in results:
            assert set(r) >= {"file", "line", "col", "preview"}

    def test_cache_avoids_reextraction(self, doc_tree):
        server._DOC_TEXT_CACHE.clear()
        with patch.object(
            server.office_extract,
            "extract_structured",
            wraps=office_extract.extract_structured,
        ) as spy:
            server._search_documents(doc_tree, "zanzibar", "", "", 50)
            first = spy.call_count
            server._search_documents(doc_tree, "different query", "", "", 50)
            assert spy.call_count == first  # cache hit — no re-extraction

    def test_search_dispatcher_appends_doc_hits(self, doc_tree):
        results = server._search(doc_tree, "zanzibar")
        files = {Path(r["file"]).name for r in results}
        assert "notes.md" in files  # plain text engine
        assert "report.docx" in files  # document pass

    def test_doc_pass_failure_never_breaks_plain_search(self, doc_tree):
        with patch.object(server, "_search_documents", side_effect=RuntimeError("boom")):
            results = server._search(doc_tree, "zanzibar")
        assert any(r["file"].endswith("notes.md") for r in results)


# ── Rich PPTX: groups, tables, pictures, inheritance, backgrounds ──

_RICH_SLIDE1_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:cSld>
    <p:spTree>
      <p:sp>
        <p:nvSpPr><p:nvPr><p:ph type="ctrTitle"/></p:nvPr></p:nvSpPr>
        <p:spPr/>
        <p:txBody>
          <a:p>
            <a:pPr algn="ctr" lvl="0">
              <a:buNone/>
              <a:defRPr sz="3200" b="1">
                <a:solidFill><a:schemeClr val="tx1"/></a:solidFill>
              </a:defRPr>
            </a:pPr>
            <a:r>
              <a:rPr sz="4400" i="1">
                <a:solidFill>
                  <a:schemeClr val="accent1"><a:lumMod val="40000"/><a:lumOff val="10000"/>
                  </a:schemeClr>
                </a:solidFill>
              </a:rPr>
              <a:t>Inherited Title</a:t>
            </a:r>
            <a:br/>
            <a:fld id="{X}" type="slidenum"><a:t>1</a:t></a:fld>
          </a:p>
          <a:p>
            <a:pPr><a:buChar char="-"/></a:pPr>
            <a:r><a:rPr><a:solidFill><a:sysClr val="windowText" lastClr="333333"/>
            </a:solidFill></a:rPr><a:t>bulleted item</a:t></a:r>
          </a:p>
        </p:txBody>
      </p:sp>
      <p:sp>
        <p:nvSpPr><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>
        <p:spPr>
          <a:gradFill><a:gsLst><a:gs pos="0"><a:srgbClr val="ABCDEF"/></a:gs></a:gsLst>
          </a:gradFill>
        </p:spPr>
      </p:sp>
      <p:pic>
        <p:blipFill><a:blip r:embed="rId2"/></p:blipFill>
        <p:spPr>
          <a:xfrm><a:off x="100" y="200"/><a:ext cx="3000" cy="4000"/></a:xfrm>
        </p:spPr>
      </p:pic>
      <p:graphicFrame>
        <p:xfrm><a:off x="500" y="600"/><a:ext cx="7000" cy="8000"/></p:xfrm>
        <a:tbl>
          <a:tr><a:tc><a:txBody><a:p><a:r><a:t>H1</a:t></a:r></a:p></a:txBody></a:tc>
              <a:tc><a:txBody><a:p><a:r><a:t>H2</a:t></a:r></a:p></a:txBody></a:tc></a:tr>
          <a:tr><a:tc><a:txBody><a:p><a:r><a:t>c1</a:t></a:r></a:p></a:txBody></a:tc>
              <a:tc><a:txBody><a:p><a:r><a:t>c2</a:t></a:r></a:p></a:txBody></a:tc></a:tr>
        </a:tbl>
      </p:graphicFrame>
      <p:grpSp>
        <p:grpSpPr>
          <a:xfrm>
            <a:off x="1000000" y="1000000"/><a:ext cx="2000000" cy="2000000"/>
            <a:chOff x="0" y="0"/><a:chExt cx="1000000" cy="1000000"/>
          </a:xfrm>
        </p:grpSpPr>
        <p:sp>
          <p:spPr>
            <a:xfrm><a:off x="0" y="0"/><a:ext cx="500000" cy="500000"/></a:xfrm>
            <a:solidFill><a:srgbClr val="00AA00"/></a:solidFill>
          </p:spPr>
          <p:txBody><a:p><a:r><a:t>grouped text</a:t></a:r></a:p></p:txBody>
        </p:sp>
      </p:grpSp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""

_RICH_SLIDE2_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>
    <p:spTree>
      <p:sp>
        <p:spPr/>
        <p:txBody><a:p><a:r><a:t>second slide</a:t></a:r></a:p></p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""

_RICH_SLIDE1_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
    Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
    Target="../media/image1.png"/>
</Relationships>
"""

_RICH_LAYOUT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:sp>
        <p:nvSpPr><p:nvPr><p:ph type="ctrTitle"/></p:nvPr></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="914400" y="457200"/><a:ext cx="7315200" cy="1143000"/></a:xfrm>
        </p:spPr>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sldLayout>
"""

_RICH_LAYOUT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/base"
    Target="/{BASE_PART}"/>
</Relationships>
"""

# The extractor matches this OOXML part name's own spelling; it cannot be renamed.
_BASE_PART = "ppt/slideMasters/slideMaster1.xml"  # wokeignore:rule=master
_RICH_LAYOUT_RELS_XML = _RICH_LAYOUT_RELS_XML.replace("{BASE_PART}", _BASE_PART)

_RICH_BASE_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldBase xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:bg><p:bgPr><a:solidFill><a:srgbClr val="101820"/></a:solidFill></p:bgPr></p:bg>
    <p:spTree>
      <p:sp>
        <p:nvSpPr><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="914400" y="1828800"/><a:ext cx="7315200" cy="3657600"/></a:xfrm>
        </p:spPr>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sldBase>
"""


def make_rich_pptx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ppt/presentation.xml", _PPTX_PRESENTATION_XML)
        zf.writestr("ppt/theme/theme1.xml", _PPTX_THEME_XML)
        zf.writestr("ppt/slides/slide1.xml", _RICH_SLIDE1_XML)
        zf.writestr("ppt/slides/slide2.xml", _RICH_SLIDE2_XML)
        zf.writestr("ppt/slides/_rels/slide1.xml.rels", _RICH_SLIDE1_RELS_XML)
        zf.writestr("ppt/slideLayouts/slideLayout1.xml", _RICH_LAYOUT_XML)
        zf.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", _RICH_LAYOUT_RELS_XML)
        zf.writestr(_BASE_PART, _RICH_BASE_XML)
        zf.writestr("ppt/media/image1.png", _PNG_BYTES)


class TestOfficeExtractCoverage:
    """Edge and error branches: rich pptx geometry, xlsx/docx corners, errors."""

    @pytest.fixture
    def rich_deck(self, tmp_path):
        p = tmp_path / "rich.pptx"
        make_rich_pptx(p)
        return p

    def test_rich_pptx_shapes(self, rich_deck):
        out = office_extract.extract_structured(rich_deck)
        s1 = out["slides"][0]
        kinds = [sh["kind"] for sh in s1["shapes"]]
        assert kinds.count("text") >= 2 and "image" in kinds and "table" in kinds
        table = next(sh for sh in s1["shapes"] if sh["kind"] == "table")
        assert table["rows"] == [["H1", "H2"], ["c1", "c2"]]
        assert any("H1 | H2" in ln for ln in s1["lines"])

    def test_rich_pptx_placeholder_inheritance(self, rich_deck):
        out = office_extract.extract_structured(rich_deck)
        s1 = out["slides"][0]
        title = next(
            sh
            for sh in s1["shapes"]
            if any("Inherited" in r.get("t", "") for p in sh.get("paras", []) for r in p["runs"])
        )
        # box came from the layout placeholder, not the (empty) slide spPr
        assert title["x"] == pytest.approx(10.0, abs=0.1)
        fill_only = next(sh for sh in s1["shapes"] if sh.get("fill") == "#ABCDEF")
        # box for the fill-only body placeholder came from the base slide part
        assert fill_only["y"] == pytest.approx(26.67, abs=0.1)

    def test_rich_pptx_group_transform_and_runs(self, rich_deck):
        out = office_extract.extract_structured(rich_deck)
        s1 = out["slides"][0]
        grouped = next(
            sh
            for sh in s1["shapes"]
            if any(r.get("t") == "grouped text" for p in sh.get("paras", []) for r in p["runs"])
        )
        # child (0,0,500k,500k) in 1M child space -> mapped into the 2M group at 1M offset
        assert grouped["x"] == pytest.approx(1000000 / 9144000 * 100, abs=0.1)
        assert grouped["fill"] == "#00AA00"
        title_para = out["slides"][0]["shapes"][0]["paras"][0]
        run = title_para["runs"][0]
        assert run["i"] is True and run["sz"] == 44.0
        assert run["c"] != "#ff0000"  # lumMod/lumOff adjusted away from raw accent1
        assert {"t": "\n"} in title_para["runs"]  # <a:br/>
        assert any(r.get("t") == "1" for r in title_para["runs"])  # <a:fld>
        bullet_para = out["slides"][0]["shapes"][0]["paras"][1]
        assert bullet_para["bullet"] is True
        assert bullet_para["runs"][0]["c"] == "#333333"  # sysClr in a run

    def test_rich_pptx_backgrounds(self, rich_deck):
        out = office_extract.extract_structured(rich_deck)
        # slide1: no own bg, layout has none -> inherited from the base slide part
        assert out["slides"][0]["bg"] == "#101820"
        # slide2: bgRef with schemeClr bg1 -> lt1 -> white
        assert out["slides"][1]["bg"] == "#FFFFFF"

    def test_lum_adjust_invalid_hex_passthrough(self, rich_deck):
        out = office_extract.extract_structured(rich_deck)
        assert out["kind"] == "pptx"  # extraction exercised _lum_adjust already

    def test_pptx_without_slides_rejected(self, tmp_path):
        p = tmp_path / "empty.pptx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("ppt/presentation.xml", _PPTX_PRESENTATION_XML)
        with pytest.raises(office_extract.OfficeExtractError, match="no slides"):
            office_extract.extract_structured(p)

    def test_bad_zip_rejected(self, tmp_path):
        p = tmp_path / "junk.docx"
        p.write_bytes(b"this is not a zip archive at all")
        with pytest.raises(office_extract.OfficeExtractError, match="not a readable"):
            office_extract.extract_structured(p)
        with pytest.raises(office_extract.OfficeExtractError, match="not a readable"):
            office_extract.media_member(p, "word/media/image1.png")

    def test_unsupported_extension_rejected(self, tmp_path):
        p = tmp_path / "notes.txt"
        p.write_text("hello")
        with pytest.raises(office_extract.OfficeExtractError, match="not supported"):
            office_extract.extract_structured(p)

    def test_media_member_missing_is_404(self, tmp_path):
        p = tmp_path / "deck.pptx"
        make_pptx(p)
        with pytest.raises(office_extract.OfficeExtractError) as exc:
            office_extract.media_member(p, "ppt/media/absent.png")
        assert exc.value.status == 404

    def test_docx_missing_document_xml(self, tmp_path):
        p = tmp_path / "empty.docx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("word/other.xml", "<x/>")
        with pytest.raises(office_extract.OfficeExtractError, match="missing word/document"):
            office_extract.extract_structured(p)

    def test_docx_body_missing(self, tmp_path):
        p = tmp_path / "nobody.docx"
        doc = (
            "<w:document xmlns:w="
            '"http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
        )
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("word/document.xml", doc)
        with pytest.raises(office_extract.OfficeExtractError, match="empty .docx body"):
            office_extract.extract_structured(p)

    def test_xlsx_absolute_rel_target_and_missing_sheet(self, tmp_path):
        p = tmp_path / "abs.xlsx"
        wb = _XLSX_WORKBOOK_XML.replace(
            '<sheet name="Data" sheetId="1" r:id="rId1"/>',
            '<sheet name="Data" sheetId="1" r:id="rId1"/>'
            '<sheet name="Ghost" sheetId="2" r:id="rId2"/>',
        )
        rels = _XLSX_RELS_XML.replace(
            'Target="worksheets/sheet1.xml"/>',
            'Target="/xl/worksheets/sheet1.xml"/>\n  <Relationship Id="rId2" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/worksheet" Target="worksheets/ghost.xml"/>',
        )
        sheet = _XLSX_SHEET_XML.replace(
            '<c r="A1" t="s"><v>0</v></c>',
            '<c r="A1" t="s"><v>99</v></c><c t="n"><v>7</v></c>',
        )
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("xl/workbook.xml", wb)
            zf.writestr("xl/_rels/workbook.xml.rels", rels)
            zf.writestr("xl/sharedStrings.xml", _XLSX_SHARED_STRINGS_XML)
            zf.writestr("xl/worksheets/sheet1.xml", sheet)
        out = office_extract.extract_structured(p)
        assert [s["name"] for s in out["sheets"]] == ["Data"]  # ghost sheet skipped
        row1 = out["sheets"][0]["rows"][0]
        assert row1[0] == "99"  # out-of-range shared index left as-is
        assert "7" in row1  # ref-less cell appended positionally

    def test_xlsx_col_index_multi_letter(self):
        assert office_extract._xlsx_col_index("AA10") == 26
        assert office_extract._xlsx_col_index("BC12") == 54
        assert office_extract._xlsx_col_index("") == 0

    def test_member_text_missing_returns_none(self, tmp_path):
        p = tmp_path / "z.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("a.txt", "hi")
        with zipfile.ZipFile(p) as zf:
            assert office_extract._member_text(zf, "missing.xml") is None
            assert office_extract._member_text(zf, "a.txt") == "hi"


class TestRound7Hardening:
    """Review-round fixes: gated-bytes parsing, SVG inline ban, write guards."""

    def test_extract_structured_parses_given_bytes_not_the_path(self, tmp_path):
        real = tmp_path / "swap.pptx"
        make_pptx(real)
        blob = real.read_bytes()
        # Swap the file after "validation": bytes-mode must ignore the swap.
        real.write_bytes(b"not a zip anymore")
        out = office_extract.extract_structured(real, data=blob)
        assert out["kind"] == "pptx" and out["slides"]

    def test_media_member_bytes_mode(self, tmp_path):
        real = tmp_path / "swap2.pptx"
        make_pptx(real)
        blob = real.read_bytes()
        real.write_bytes(b"junk")
        data, fname = office_extract.media_member(real, "ppt/media/image1.png", data=blob)
        assert fname == "image1.png" and data.startswith(b"\x89PNG")

    def test_xlsx_forged_column_reference_is_skipped(self, tmp_path):
        p = tmp_path / "bomb.xlsx"
        sheet = _XLSX_SHEET_XML.replace(
            '<c r="A1" t="s"><v>0</v></c>',
            '<c r="ZZZZZZ1"><v>evil</v></c><c r="A1" t="s"><v>0</v></c>',
        )
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("xl/workbook.xml", _XLSX_WORKBOOK_XML)
            zf.writestr("xl/_rels/workbook.xml.rels", _XLSX_RELS_XML)
            zf.writestr("xl/sharedStrings.xml", _XLSX_SHARED_STRINGS_XML)
            zf.writestr("xl/worksheets/sheet1.xml", sheet)
        out = office_extract.extract_structured(p)
        row1 = out["sheets"][0]["rows"][0]
        assert "evil" not in row1  # forged ref skipped, no giant padding
        assert len(row1) < office_extract.XLSX_MAX_COLS

    def test_raw_svg_is_attachment_never_inline(self, doc_tree):
        svg = doc_tree / "evil.svg"
        svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>")
        cap = _byte_request(f"/raw?path={svg}")
        assert cap.status == 200
        assert cap.headers.get("Content-Type") == "application/octet-stream"
        assert "attachment" in cap.headers.get("Content-Disposition", "")

    def test_extract_member_svg_is_attachment(self, doc_tree):
        deck = doc_tree / "svgdeck.pptx"
        with zipfile.ZipFile(deck, "w") as zf:
            zf.writestr("ppt/presentation.xml", _PPTX_PRESENTATION_XML)
            zf.writestr("ppt/slides/slide1.xml", _PPTX_SLIDE1_XML)
            zf.writestr("ppt/media/vector.svg", "<svg/>")
        cap = _byte_request(f"/extract?path={deck}&member=ppt/media/vector.svg")
        assert cap.status == 200
        assert cap.headers.get("Content-Type") == "application/octet-stream"
        assert "attachment" in cap.headers.get("Content-Disposition", "")

    def test_write_refuses_oversize_file(self, doc_tree):
        big = doc_tree / "big.md"
        big.write_text("x")
        with patch.object(server, "MAX_READ_BYTES", 0):
            code, body = _json_request(f"/write?path={big}", "POST", b"tail-eating buffer")
        assert code == 413
        assert big.read_text() == "x"  # untouched

    def test_write_ns_token_conflict_and_success(self, doc_tree):
        md = doc_tree / "note2.md"
        md.write_text("v1")
        stale = md.stat().st_mtime_ns - 1
        code, _ = _json_request(f"/write?path={md}&base_token={stale}", "POST", b"v2")
        assert code == 409
        good = md.stat().st_mtime_ns
        code, body = _json_request(f"/write?path={md}&base_token={good}", "POST", b"v2")
        assert code == 200
        assert md.read_text() == "v2"
        assert "mtime_ns" in body

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX permission bits do not exist on Windows"
    )
    def test_write_preserves_file_mode(self, doc_tree):
        md = doc_tree / "exec.md"
        md.write_text("v1")
        os.chmod(md, 0o640)
        tok = md.stat().st_mtime_ns
        code, _ = _json_request(f"/write?path={md}&base_token={tok}", "POST", b"v2")
        assert code == 200
        assert (md.stat().st_mode & 0o777) == 0o640

    def test_read_meta_carries_ns_token(self, doc_tree):
        md = doc_tree / "tok.md"
        md.write_text("hello")
        code, body = _json_request(f"/read?path={md}")
        assert code == 200
        assert body["mtime_ns"] == md.stat().st_mtime_ns


class TestRound9Hardening:
    """Regression tests for the round-9 review findings (GPT 5.6 / Opus 4.8):
    header-injection-proof download names, doc-search symlink containment,
    xlsx render/total cell caps, and the locked write re-check."""

    def test_disposition_neutralizes_crlf_injection(self):
        d = server._disposition("attachment", "evil\r\nSet-Cookie: pwn=1;.md")
        assert "\r" not in d and "\n" not in d
        # The real name survives RFC 5987-encoded; the CRLF is percent-escaped.
        assert "filename*=UTF-8''" in d and "%0D%0A" in d
        # The plain token is squashed to safe ASCII.
        assert 'filename="evil_Set-Cookie_pwn_1_.md"' in d

    def test_disposition_empty_name_falls_back(self):
        d = server._disposition("attachment", "")
        assert 'filename="download"' in d

    def test_raw_download_header_is_injection_proof(self, doc_tree):
        # POSIX forbids only "/" and NUL in a basename — CR/LF is legal.
        evil = doc_tree / "report\r\nX-Injected: 1.md"
        try:
            evil.write_text("payload")
        except OSError:
            pytest.skip("filesystem rejects control chars in names")
        from urllib.parse import quote

        cap = _byte_request(f"/raw?path={quote(str(evil))}&download=1")
        assert cap.status == 200
        disp = cap.headers.get("Content-Disposition", "")
        assert "\r" not in disp and "\n" not in disp
        assert "X-Injected: 1" not in disp  # header form neutralized (squashed)

    def test_doc_search_skips_symlink_escaping_roots(self, doc_tree):
        outside = doc_tree.parent / "outside_docs"
        outside.mkdir(exist_ok=True)
        make_docx(outside / "leak.docx")  # contains the zanzibar keyword
        (doc_tree / "leaklink.docx").symlink_to(outside / "leak.docx")
        results = server._search_documents(doc_tree, "zanzibar", "", "", 50)
        assert results  # the legit in-root report.docx still matches
        assert not any("leaklink" in r["file"] for r in results)

    def test_xlsx_far_right_reference_capped_and_marked_truncated(self, tmp_path):
        p = tmp_path / "wide.xlsx"
        sheet = _XLSX_SHEET_XML.replace(
            '<c r="A1" t="s"><v>0</v></c>',
            '<c r="A1" t="s"><v>0</v></c><c r="KZ1"><v>far</v></c>',
        )
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("xl/workbook.xml", _XLSX_WORKBOOK_XML)
            zf.writestr("xl/_rels/workbook.xml.rels", _XLSX_RELS_XML)
            zf.writestr("xl/sharedStrings.xml", _XLSX_SHARED_STRINGS_XML)
            zf.writestr("xl/worksheets/sheet1.xml", sheet)
        out = office_extract.extract_structured(p)
        first = out["sheets"][0]
        # col KZ (index 311) exceeds the 256-column render cap: no padding
        # to it, the value is dropped, and the sheet is flagged truncated.
        assert all(len(row) <= office_extract.MAX_XLSX_RENDER_COLS for row in first["rows"])
        assert not any("far" in row for row in first["rows"])
        assert first["truncated"] is True

    def test_xlsx_total_cell_cap_stops_row_ingestion(self, tmp_path):
        p = tmp_path / "many.xlsx"
        rows_xml = "".join(
            f'<row r="{i}">'
            + "".join(f'<c r="{chr(65 + j)}{i}"><v>{i * 10 + j}</v></c>' for j in range(3))
            + "</row>"
            for i in range(1, 5)
        )
        sheet = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{rows_xml}</sheetData></worksheet>"
        )
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("xl/workbook.xml", _XLSX_WORKBOOK_XML)
            zf.writestr("xl/_rels/workbook.xml.rels", _XLSX_RELS_XML)
            zf.writestr("xl/worksheets/sheet1.xml", sheet)
        with patch.object(office_extract, "MAX_XLSX_TOTAL_CELLS", 5):
            out = office_extract.extract_structured(p)
        first = out["sheets"][0]
        # Ingestion stops once the workbook-wide cell budget is spent.
        assert len(first["rows"]) == 2
        assert first["truncated"] is True

    def _write_handler(self):
        handler = server.FileExplorerHandler.__new__(server.FileExplorerHandler)
        responses: list[tuple] = []
        handler._json = lambda code, payload: responses.append((code, payload))
        return handler, responses

    def test_locked_write_stale_token_is_authoritative_409(self, doc_tree):
        md = doc_tree / "race.md"
        md.write_text("v1")
        handler, responses = self._write_handler()
        stale = str(md.stat().st_mtime_ns - 1)
        # Direct call = the in-lock re-check (the pre-lock check is bypassed):
        # a writer that raced past the fast check still cannot clobber.
        with pytest.raises(server.PathError) as exc:
            handler._locked_write(md, b"v2", stale, "")
        assert exc.value.status == 409
        assert md.read_text() == "v1"
        assert not responses

    def test_locked_write_good_token_replaces_atomically(self, doc_tree):
        md = doc_tree / "race2.md"
        md.write_text("v1")
        handler, responses = self._write_handler()
        handler._locked_write(md, b"v2", str(md.stat().st_mtime_ns), "")
        assert md.read_text() == "v2"
        assert responses and responses[0][0] == 200
        assert responses[0][1]["mtime_ns"] == md.stat().st_mtime_ns

    def test_write_endpoint_serializes_under_lock(self, doc_tree):
        # The endpoint path acquires _WRITE_LOCK; a held lock blocks a second
        # writer instead of letting check-then-write interleave.
        md = doc_tree / "serial.md"
        md.write_text("v1")
        assert server._WRITE_LOCK.acquire(timeout=1)
        try:
            import threading

            result: list = []
            tok = md.stat().st_mtime_ns

            def attempt():
                result.append(_json_request(f"/write?path={md}&base_token={tok}", "POST", b"v2"))

            t = threading.Thread(target=attempt, daemon=True)
            t.start()
            t.join(timeout=0.3)
            assert t.is_alive()  # blocked on the lock, not writing
            assert md.read_text() == "v1"
        finally:
            server._WRITE_LOCK.release()
        # Lock released: the queued write completes normally.
        import time as _time

        for _ in range(50):
            if result:
                break
            _time.sleep(0.05)
        assert result and result[0][0] == 200
        assert md.read_text() == "v2"
