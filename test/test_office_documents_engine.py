"""Tests for the offline OOXML document engine (W07 · office_documents).

Covers structured read, template creation, targeted in-place edit with
byte-preserving write-back, independent-parser reopen verification, and the
protected/unsupported-format rejection gate with a negative test per category.

Fidelity is asserted two ways: (1) untouched parts survive a round-trip
byte-for-byte, and (2) the written file reopens under a DIFFERENT reader than
the one that wrote it (python-docx for docx; a from-scratch stdlib zip walk for
pptx) and yields the expected content — so the test does not merely re-run the
engine's own code path over its own output.
"""

from __future__ import annotations

import os
import zipfile

# python-docx is a declared dependency and serves as the INDEPENDENT reader
# that reopens docx output the engine wrote through its own part-level path.
import docx as pydocx  # type: ignore[import-untyped]
import pytest

# The independent verification reader re-parses engine OUTPUT (a trusted,
# engine-produced file) via defusedxml.fromstring DIRECTLY — a different read
# path than the engine's own container.parse_xml_part wrapper — to prove the
# output is well-formed and readable outside the write path. (The docx
# independent reader is python-docx, a wholly separate library.)
from defusedxml.ElementTree import fromstring as _indep_fromstring

from kiro_crew.connections.vendors.microsoft import office_documents as od
from kiro_crew.connections.vendors.microsoft.office_documents import constants as C
from kiro_crew.connections.vendors.microsoft.office_documents import (
    container,
)
from kiro_crew.connections.vendors.microsoft.office_documents import docx as docx_mod
from kiro_crew.connections.vendors.microsoft.office_documents import pptx as pptx_mod
from kiro_crew.connections.vendors.microsoft.office_documents import (
    rejection,
)
from kiro_crew.connections.vendors.microsoft.office_documents.errors import (
    DocumentEditError,
    MalformedDocument,
    OfficeDocumentError,
    ProtectedDocument,
    UnsupportedDocument,
)

# ── Fixtures / builders ──


def _make_real_docx(path: str) -> None:
    """Author a real .docx (styles, theme, table) with python-docx."""
    d = pydocx.Document()
    d.add_heading("Original Title", level=1)
    d.add_paragraph("Body paragraph one.")
    d.add_paragraph("Body paragraph two.")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "r0c0"
    table.cell(0, 1).text = "r0c1"
    table.cell(1, 0).text = "r1c0"
    table.cell(1, 1).text = "r1c1"
    d.save(path)


_PPTX_SLIDE = (
    "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
    "<p:sld xmlns:a='%s' xmlns:p='%s'>"
    "<p:cSld><p:spTree><p:sp><p:txBody>"
    "<a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
    "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>" % (C.A_NS, C.P_NS)
)
_PPTX_NOTES = (
    "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
    "<p:notes xmlns:a='%s' xmlns:p='%s'>"
    "<p:cSld><p:spTree><p:sp><p:txBody>"
    "<a:p><a:r><a:t>{notes}</a:t></a:r></a:p>"
    "</p:txBody></p:sp></p:spTree></p:cSld></p:notes>" % (C.A_NS, C.P_NS)
)
_PPTX_SLIDE_RELS = (
    "<?xml version='1.0'?>"
    "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
    "<Relationship Id='rId1' "
    "Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide' "
    "Target='../notesSlides/notesSlide{n}.xml'/></Relationships>"
)


def _make_real_pptx(path: str, slides: list[tuple[str, str | None]]) -> None:
    """Author a .pptx with (body, notes) per slide. notes=None -> no notes part."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            C.CONTENT_TYPES_PART,
            "<?xml version='1.0'?><Types xmlns='%s'/>" % C.CT_NS,
        )
        z.writestr(
            C.PPTX_PRESENTATION_PART,
            "<?xml version='1.0'?><p:presentation xmlns:p='%s'/>" % C.P_NS,
        )
        z.writestr("ppt/theme/theme1.xml", "<theme>original</theme>")
        for i, (body, notes) in enumerate(slides, 1):
            z.writestr(f"ppt/slides/slide{i}.xml", _PPTX_SLIDE.format(text=body))
            if notes is not None:
                z.writestr(
                    f"ppt/slides/_rels/slide{i}.xml.rels",
                    _PPTX_SLIDE_RELS.format(n=i),
                )
                z.writestr(
                    f"ppt/notesSlides/notesSlide{i}.xml",
                    _PPTX_NOTES.format(notes=notes),
                )


def _slide_text_via_stdlib(path: str, n: int) -> str:
    """Independent pptx slide-text reader: bare stdlib, not the engine's code."""
    with zipfile.ZipFile(path) as z:
        raw = z.read(f"ppt/slides/slide{n}.xml")
    root = _indep_fromstring(raw)
    return "".join(t.text or "" for t in root.iter(f"{C.A}t"))


@pytest.fixture()
def docx_path(tmp_path):
    p = str(tmp_path / "doc.docx")
    _make_real_docx(p)
    return p


@pytest.fixture()
def pptx_path(tmp_path):
    p = str(tmp_path / "deck.pptx")
    _make_real_pptx(p, [("Slide one body", "Note one"), ("Slide two body", None)])
    return p


# ── DOCX structured read ──


class TestDocxRead:
    def test_paragraphs_and_styles(self, docx_path):
        content = od.read_document(docx_path)
        texts = [p.text for p in content.paragraphs]
        assert "Original Title" in texts
        assert "Body paragraph one." in texts
        assert content.paragraphs[0].style.startswith("Heading")

    def test_tables(self, docx_path):
        content = od.read_document(docx_path)
        assert len(content.tables) == 1
        assert content.tables[0].rows == [["r0c0", "r0c1"], ["r1c0", "r1c1"]]

    def test_text_property_excludes_tables(self, docx_path):
        content = od.read_document(docx_path)
        assert "Body paragraph one." in content.text
        assert "r0c0" not in content.text

    def test_facade_read_dispatches_docx(self, docx_path):
        content = od.read(docx_path)
        assert isinstance(content, od.DocxContent)

    def test_empty_body_returns_empty(self, tmp_path):
        p = str(tmp_path / "empty.docx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(
                C.DOCX_MAIN_PART,
                "<?xml version='1.0'?><w:document xmlns:w='%s'><w:body/></w:document>" % C.W_NS,
            )
        content = od.read_document(p)
        assert content.paragraphs == []
        assert content.tables == []

    def test_document_without_body(self, tmp_path):
        p = str(tmp_path / "nobody.docx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(
                C.DOCX_MAIN_PART,
                "<?xml version='1.0'?><w:document xmlns:w='%s'/>" % C.W_NS,
            )
        assert od.read_document(p).paragraphs == []


# ── DOCX in-place edit + independent reopen + fidelity ──


class TestDocxEdit:
    def test_edit_reopens_under_independent_parser(self, docx_path, tmp_path):
        out = str(tmp_path / "out.docx")
        od.edit_in_place(docx_path, out, {1: "Rewritten body."})
        reopened = [p.text for p in pydocx.Document(out).paragraphs]
        assert "Rewritten body." in reopened
        assert "Body paragraph two." in reopened  # untouched paragraph survives
        assert "Original Title" in reopened

    def test_untouched_parts_preserved_byte_for_byte(self, docx_path, tmp_path):
        out = str(tmp_path / "out.docx")
        src_zip = zipfile.ZipFile(docx_path)
        before = {n: src_zip.read(n) for n in src_zip.namelist() if n != C.DOCX_MAIN_PART}
        od.edit_in_place(docx_path, out, {1: "Changed."})
        out_zip = zipfile.ZipFile(out)
        for name, data in before.items():
            assert out_zip.read(name) == data, f"part {name} not preserved"

    def test_style_preserved_on_edited_paragraph(self, docx_path, tmp_path):
        out = str(tmp_path / "out.docx")
        od.edit_in_place(docx_path, out, {0: "New Heading Text"})
        reopened = pydocx.Document(out)
        assert reopened.paragraphs[0].text == "New Heading Text"
        assert reopened.paragraphs[0].style.name.startswith("Heading")

    def test_in_place_same_path(self, docx_path):
        od.edit_in_place(docx_path, docx_path, {1: "In place."})
        assert "In place." in [p.text for p in pydocx.Document(docx_path).paragraphs]

    def test_edit_out_of_range_raises_before_write(self, docx_path, tmp_path):
        out = str(tmp_path / "out.docx")
        with pytest.raises(DocumentEditError):
            od.edit_in_place(docx_path, out, {99: "nope"})
        assert not os.path.exists(out)

    def test_edit_illegal_xml_char_raises_before_write(self, docx_path, tmp_path):
        # A NUL (XML-1.0-forbidden) in replacement text must be refused before
        # any bytes are written, not serialised into unreopenable output.
        out = str(tmp_path / "out.docx")
        with pytest.raises(DocumentEditError):
            od.edit_in_place(docx_path, out, {0: "bad\x00text"})
        assert not os.path.exists(out)

    def test_no_op_edit_produces_faithful_copy(self, docx_path, tmp_path):
        out = str(tmp_path / "copy.docx")
        docx_mod.replace_paragraph_text(docx_path, out, {})
        assert [p.text for p in pydocx.Document(out).paragraphs] == [
            p.text for p in pydocx.Document(docx_path).paragraphs
        ]

    def test_whitespace_preserved(self, docx_path, tmp_path):
        out = str(tmp_path / "ws.docx")
        od.edit_in_place(docx_path, out, {1: "  leading and trailing  "})
        assert "  leading and trailing  " in [p.text for p in pydocx.Document(out).paragraphs]


# ── DOCX template create ──


class TestDocxTemplate:
    def test_create_carries_parts_and_fills(self, docx_path, tmp_path):
        out = str(tmp_path / "fromtpl.docx")
        kind = od.create_from_template(docx_path, out, {0: "Filled Title"})
        assert kind == "docx"
        reopened = pydocx.Document(out)
        assert reopened.paragraphs[0].text == "Filled Title"
        # Template's styles part carried over.
        assert "word/styles.xml" in zipfile.ZipFile(out).namelist()

    def test_create_without_edits_is_faithful_copy(self, docx_path, tmp_path):
        out = str(tmp_path / "copy.docx")
        od.create_from_template(docx_path, out)
        assert [p.text for p in pydocx.Document(out).paragraphs] == [
            p.text for p in pydocx.Document(docx_path).paragraphs
        ]


# ── PPTX structured read (stdlib) ──


class TestPptxRead:
    def test_slides_and_notes(self, pptx_path):
        content = od.read_presentation(pptx_path)
        assert [s.number for s in content.slides] == [1, 2]
        assert content.slides[0].text == "Slide one body"
        assert content.slides[0].notes == "Note one"
        assert content.slides[1].notes == ""  # no notes part

    def test_facade_read_dispatches_pptx(self, pptx_path):
        assert isinstance(od.read(pptx_path), od.PptxContent)

    def test_text_property(self, pptx_path):
        content = od.read_presentation(pptx_path)
        assert "Slide one body" in content.text
        assert "Slide two body" in content.text

    def test_slides_sorted_numerically(self, tmp_path):
        # slide10 must sort after slide2, not lexically before it.
        p = str(tmp_path / "many.pptx")
        _make_real_pptx(p, [(f"body{i}", None) for i in range(1, 11)])
        content = od.read_presentation(p)
        assert [s.number for s in content.slides] == list(range(1, 11))


# ── PPTX edit + independent reopen + fidelity ──


class TestPptxEdit:
    def test_edit_reopens_via_stdlib(self, pptx_path, tmp_path):
        out = str(tmp_path / "out.pptx")
        od.edit_in_place(pptx_path, out, {1: "Edited one"})
        assert _slide_text_via_stdlib(out, 1) == "Edited one"
        # Second slide untouched.
        assert _slide_text_via_stdlib(out, 2) == "Slide two body"

    def test_notes_and_other_parts_preserved(self, pptx_path, tmp_path):
        out = str(tmp_path / "out.pptx")
        src = zipfile.ZipFile(pptx_path)
        before = {n: src.read(n) for n in src.namelist() if n != "ppt/slides/slide1.xml"}
        od.edit_in_place(pptx_path, out, {1: "Edited"})
        out_zip = zipfile.ZipFile(out)
        for name, data in before.items():
            assert out_zip.read(name) == data, f"part {name} not preserved"
        # Notes still readable and unchanged via the engine too.
        assert od.read_presentation(out).slides[0].notes == "Note one"
        # Theme preserved.
        assert out_zip.read("ppt/theme/theme1.xml") == b"<theme>original</theme>"

    def test_unknown_slide_raises_before_write(self, pptx_path, tmp_path):
        out = str(tmp_path / "out.pptx")
        with pytest.raises(DocumentEditError):
            od.edit_in_place(pptx_path, out, {9: "nope"})
        assert not os.path.exists(out)

    def test_edit_illegal_xml_char_raises_before_write(self, pptx_path, tmp_path):
        out = str(tmp_path / "out.pptx")
        with pytest.raises(DocumentEditError):
            od.edit_in_place(pptx_path, out, {1: "bad\x0bctrl"})
        assert not os.path.exists(out)

    def test_slide_with_no_run_raises(self, tmp_path):
        p = str(tmp_path / "empty_slide.pptx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(C.PPTX_PRESENTATION_PART, "<p:presentation xmlns:p='%s'/>" % C.P_NS)
            z.writestr(
                "ppt/slides/slide1.xml",
                "<p:sld xmlns:a='%s' xmlns:p='%s'><p:cSld><p:spTree/></p:cSld></p:sld>"
                % (C.A_NS, C.P_NS),
            )
        with pytest.raises(DocumentEditError):
            od.edit_in_place(p, str(tmp_path / "o.pptx"), {1: "x"})

    def test_no_op_edit_faithful_copy(self, pptx_path, tmp_path):
        out = str(tmp_path / "copy.pptx")
        pptx_mod.replace_slide_text(pptx_path, out, {})
        assert _slide_text_via_stdlib(out, 1) == "Slide one body"

    def test_create_from_template(self, pptx_path, tmp_path):
        out = str(tmp_path / "tpl.pptx")
        kind = od.create_from_template(pptx_path, out, {1: "Templated"})
        assert kind == "pptx"
        assert _slide_text_via_stdlib(out, 1) == "Templated"


# ── Rejection gate: one negative test per category ──


class TestRejectionGate:
    def _write(self, tmp_path, name, data: bytes) -> str:
        p = str(tmp_path / name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_legacy_ole2_doc(self, tmp_path):
        p = self._write(tmp_path, "old.doc", C.OLE2_MAGIC + b"\x00" * 64)
        v = od.classify(p)
        assert not v.ok and v.reason == UnsupportedDocument.reason
        with pytest.raises(UnsupportedDocument):
            rejection.ensure_editable(p)

    def test_encrypted_ooxml_wrapper(self, tmp_path):
        p = self._write(tmp_path, "enc.docx", C.OLE2_MAGIC + b"\x00" * 64)
        v = od.classify(p)
        assert not v.ok and v.reason == ProtectedDocument.reason
        with pytest.raises(ProtectedDocument):
            rejection.ensure_editable(p)

    def test_macro_enabled_extension(self, tmp_path):
        p = str(tmp_path / "m.docm")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(C.DOCX_MAIN_PART, "<a/>")
        assert od.classify(p).reason == ProtectedDocument.reason

    def test_vba_project_regardless_of_extension(self, tmp_path):
        p = str(tmp_path / "v.docx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(C.DOCX_MAIN_PART, "<a/>")
            z.writestr(C.VBA_PROJECT_PART, b"macro")
        assert od.classify(p).reason == ProtectedDocument.reason

    def test_digital_signature_presence(self, tmp_path):
        p = str(tmp_path / "s.docx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(C.DOCX_MAIN_PART, "<a/>")
            z.writestr(C.SIGNATURE_ORIGIN_PART, b"sig")
        v = od.classify(p)
        assert v.reason == ProtectedDocument.reason
        assert "validity" in v.detail  # presence-only, validity not asserted

    def test_irm_protection(self, tmp_path):
        p = str(tmp_path / "i.docx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(C.DOCX_MAIN_PART, "<a/>")
            z.writestr("\x06DataSpaces/DataSpaceMap.xml", b"drm")
        assert od.classify(p).reason == ProtectedDocument.reason

    def test_encrypted_package_stream_member(self, tmp_path):
        p = str(tmp_path / "ep.docx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(C.DOCX_MAIN_PART, "<a/>")
            z.writestr("EncryptedPackage", b"blob")
        assert od.classify(p).reason == ProtectedDocument.reason

    def test_not_a_zip(self, tmp_path):
        p = self._write(tmp_path, "g.docx", b"this is not a zip")
        v = od.classify(p)
        assert v.reason == MalformedDocument.reason
        with pytest.raises(MalformedDocument):
            rejection.ensure_editable(p)

    def test_empty_archive(self, tmp_path):
        p = self._write(tmp_path, "e.docx", C.ZIP_EMPTY_MAGIC + b"\x00" * 18)
        assert od.classify(p).reason == MalformedDocument.reason

    def test_zip_without_main_part(self, tmp_path):
        p = str(tmp_path / "x.docx")
        with zipfile.ZipFile(p, "w") as z:
            # A part that is none of the three recognised main parts.
            z.writestr("customXml/item1.xml", "<a/>")
        assert od.classify(p).reason == MalformedDocument.reason

    def test_missing_file(self, tmp_path):
        with pytest.raises(MalformedDocument):
            rejection.ensure_editable(str(tmp_path / "nope.docx"))

    def test_wrong_expected_kind(self, pptx_path):
        with pytest.raises(UnsupportedDocument):
            rejection.ensure_editable(pptx_path, expected_kind="docx")

    def test_read_document_rejects_protected(self, tmp_path):
        p = str(tmp_path / "v.docx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(C.DOCX_MAIN_PART, "<a/>")
            z.writestr(C.VBA_PROJECT_PART, b"macro")
        with pytest.raises(ProtectedDocument):
            od.read_document(p)

    def test_legacy_extension_without_magic(self, tmp_path):
        # A .ppt name whose bytes are not OLE2 is still refused on extension.
        p = self._write(tmp_path, "x.ppt", b"garbage bytes")
        assert od.classify(p).reason == UnsupportedDocument.reason


# ── XLSX kind: valid workbook classifies, and the WHOLE gate applies to it ──


class TestXlsxRejectionGate:
    def _write(self, tmp_path, name, data: bytes) -> str:
        p = str(tmp_path / name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def _valid_xlsx(self, path, extra=None):
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(C.CONTENT_TYPES_PART, "<?xml version='1.0'?><Types xmlns='%s'/>" % C.CT_NS)
            z.writestr(C.XLSX_WORKBOOK_PART, "<workbook/>")
            for name, data in (extra or {}).items():
                z.writestr(name, data)
        return path

    # ── positive: a real workbook is recognised as xlsx ──
    def test_valid_workbook_classifies_as_xlsx(self, tmp_path):
        p = self._valid_xlsx(str(tmp_path / "book.xlsx"))
        v = od.classify(p)
        assert v.ok is True
        assert v.kind == "xlsx"
        assert rejection.ensure_editable(p) == "xlsx"

    def test_wrong_expected_kind_rejects_xlsx(self, tmp_path):
        p = self._valid_xlsx(str(tmp_path / "book.xlsx"))
        with pytest.raises(UnsupportedDocument):
            rejection.ensure_editable(p, expected_kind="docx")

    # ── every protection/rejection category must still fire for xlsx ──
    def test_xlsx_encrypted_ole2_wrapper(self, tmp_path):
        p = self._write(tmp_path, "enc.xlsx", C.OLE2_MAGIC + b"\x00" * 64)
        assert od.classify(p).reason == ProtectedDocument.reason

    def test_xlsx_legacy_ole2_xls(self, tmp_path):
        p = self._write(tmp_path, "old.xls", C.OLE2_MAGIC + b"\x00" * 64)
        assert od.classify(p).reason == UnsupportedDocument.reason

    def test_xlsx_macro_enabled_xlsm(self, tmp_path):
        p = self._valid_xlsx(str(tmp_path / "m.xlsm"))
        assert od.classify(p).reason == ProtectedDocument.reason

    def test_xlsx_vba_project(self, tmp_path):
        p = self._valid_xlsx(str(tmp_path / "v.xlsx"), {"xl/vbaProject.bin": b"macro"})
        assert od.classify(p).reason == ProtectedDocument.reason

    def test_xlsx_digital_signature(self, tmp_path):
        p = self._valid_xlsx(str(tmp_path / "s.xlsx"), {C.SIGNATURE_ORIGIN_PART: b"sig"})
        assert od.classify(p).reason == ProtectedDocument.reason

    def test_xlsx_irm(self, tmp_path):
        p = self._valid_xlsx(str(tmp_path / "i.xlsx"), {"\x06DataSpaces/DataSpaceMap.xml": b"drm"})
        assert od.classify(p).reason == ProtectedDocument.reason

    def test_xlsx_encrypted_package_member(self, tmp_path):
        p = self._valid_xlsx(str(tmp_path / "ep.xlsx"), {"EncryptedPackage": b"blob"})
        assert od.classify(p).reason == ProtectedDocument.reason

    def test_xlsx_not_a_zip(self, tmp_path):
        p = self._write(tmp_path, "g.xlsx", b"not a zip")
        assert od.classify(p).reason == MalformedDocument.reason

    def test_xlsx_empty_archive(self, tmp_path):
        p = self._write(tmp_path, "e.xlsx", C.ZIP_EMPTY_MAGIC + b"\x00" * 18)
        assert od.classify(p).reason == MalformedDocument.reason

    def test_xlsx_duplicate_member_via_rewrite(self, tmp_path):
        # A workbook naming one part twice is refused by the shared container path.
        dup = str(tmp_path / "dup.xlsx")
        with zipfile.ZipFile(dup, "w") as z:
            z.writestr(C.XLSX_WORKBOOK_PART, "<workbook/>")
            z.writestr("xl/styles.xml", "<a/>")
            z.writestr("xl/styles.xml", "<b/>")  # duplicate
        with pytest.raises(MalformedDocument):
            container.rewrite_parts(dup, str(tmp_path / "o.xlsx"), {C.XLSX_WORKBOOK_PART: b"<x/>"})

    def test_xlsx_duplicate_member_rejected_by_classify(self, tmp_path):
        # classify itself rejects a duplicate member name — the FULL rejection
        # gate covers duplicate-member at classify time (not only rewrite time),
        # so a valid-looking workbook that smuggles a repeated part is refused
        # before any read/edit. Uses the RAW namelist, since set() would collapse
        # the duplicate silently.
        dup = str(tmp_path / "dupclassify.xlsx")
        with zipfile.ZipFile(dup, "w") as z:
            z.writestr(C.XLSX_WORKBOOK_PART, "<workbook/>")
            z.writestr("xl/styles.xml", "<a/>")
            z.writestr("xl/styles.xml", "<b/>")  # duplicate
        v = od.classify(dup)
        assert v.ok is False
        assert v.reason == MalformedDocument.reason

    def test_xlsx_unc_path_refused(self):
        assert od.classify(r"\\server\share\book.xlsx").reason == "sensitive_path"
        with pytest.raises(OfficeDocumentError):
            rejection.ensure_editable("//server/share/book.xlsx")


# ── Container primitives ──


class TestContainer:
    def test_read_part_missing(self, docx_path):
        with pytest.raises(MalformedDocument):
            container.read_part(docx_path, "no/such/part.xml")

    def test_read_part_size_cap(self, tmp_path):
        p = str(tmp_path / "big.docx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(C.DOCX_MAIN_PART, b"x" * 100)
        with pytest.raises(MalformedDocument):
            container.read_part(p, C.DOCX_MAIN_PART, max_size=10)

    def test_rewrite_replace_absent_part_raises(self, docx_path, tmp_path):
        out = str(tmp_path / "o.docx")
        with pytest.raises(MalformedDocument):
            container.rewrite_parts(docx_path, out, {"no/such.xml": b"x"})
        assert not os.path.exists(out)

    def test_rewrite_rejects_duplicate_member_names(self, docx_path, tmp_path):
        # A container that names one part twice is refused rather than copied
        # (the second entry would silently shadow the first for any reader).
        dup = str(tmp_path / "dup.docx")
        with zipfile.ZipFile(docx_path) as src, zipfile.ZipFile(dup, "w") as dst:
            for info in src.infolist():
                dst.writestr(info, src.read(info.filename))
            # Append a second entry with a name already present.
            dst.writestr("word/styles.xml", b"<duplicate/>")
        out = str(tmp_path / "o.docx")
        with pytest.raises(MalformedDocument):
            container.rewrite_parts(dup, out, {C.DOCX_MAIN_PART: b"<x/>"})
        assert not os.path.exists(out)

    def test_parse_xml_part(self, docx_path):
        root = container.parse_xml_part(docx_path, C.DOCX_MAIN_PART)
        assert root.tag == f"{C.W}document"

    def test_malformed_xml_part(self, tmp_path):
        p = str(tmp_path / "bad.docx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(C.DOCX_MAIN_PART, b"<not well formed")
        with pytest.raises(MalformedDocument):
            container.parse_xml_part(p, C.DOCX_MAIN_PART)


# ── Error hierarchy ──


class TestConstants:
    def test_find_illegal_xml_char_passes_legal_text(self):
        assert C.find_illegal_xml_char("hello\tworld\r\nyay 🎉") is None

    def test_find_illegal_xml_char_flags_forbidden_control(self):
        assert C.find_illegal_xml_char("bad\x00nul") == "\x00"
        assert C.find_illegal_xml_char("vtab\x0bhere") == "\x0b"


class TestErrors:
    def test_protected_is_unsupported(self):
        assert issubclass(ProtectedDocument, UnsupportedDocument)
        assert issubclass(UnsupportedDocument, OfficeDocumentError)

    def test_reason_and_message(self):
        e = ProtectedDocument("locked file")
        assert e.reason == "protected_document"
        assert e.message == "locked file"

    def test_custom_reason(self):
        e = OfficeDocumentError("boom", reason="custom")
        assert e.reason == "custom"


# ── Parser-independence of the round-trip (fidelity matrix) ──


class TestFidelityMatrix:
    def test_docx_full_round_trip_all_parts(self, docx_path, tmp_path):
        """Every part except the edited one is byte-identical after edit."""
        out = str(tmp_path / "rt.docx")
        src = zipfile.ZipFile(docx_path)
        original = {n: src.read(n) for n in src.namelist()}
        od.edit_in_place(docx_path, out, {2: "Third paragraph changed."})
        result = zipfile.ZipFile(out)
        assert set(result.namelist()) == set(original)
        for name in original:
            if name == C.DOCX_MAIN_PART:
                continue
            assert result.read(name) == original[name]

    def test_pptx_round_trip_preserves_member_set(self, pptx_path, tmp_path):
        out = str(tmp_path / "rt.pptx")
        before = set(zipfile.ZipFile(pptx_path).namelist())
        od.edit_in_place(pptx_path, out, {1: "changed"})
        assert set(zipfile.ZipFile(out).namelist()) == before

    def test_double_round_trip_stable(self, docx_path, tmp_path):
        """Editing twice does not accrete drift in untouched parts."""
        out1 = str(tmp_path / "r1.docx")
        out2 = str(tmp_path / "r2.docx")
        od.edit_in_place(docx_path, out1, {1: "first"})
        od.edit_in_place(out1, out2, {1: "second"})
        z1, z2 = zipfile.ZipFile(out1), zipfile.ZipFile(out2)
        for name in z1.namelist():
            if name == C.DOCX_MAIN_PART:
                continue
            assert z1.read(name) == z2.read(name)
        assert "second" in [p.text for p in pydocx.Document(out2).paragraphs]


# ── Container edge paths ──


class TestContainerEdges:
    def test_sensitive_path_refused(self, docx_path, monkeypatch):
        monkeypatch.setattr(container, "is_sensitive_path", lambda p: True)
        with pytest.raises(OfficeDocumentError) as exc:
            container.read_part(docx_path, C.DOCX_MAIN_PART)
        assert exc.value.reason == "sensitive_path"

    def test_bad_zip_open_is_malformed(self, tmp_path):
        # A file that passes the vet's tail scan poorly / is not a real zip.
        p = str(tmp_path / "bad.docx")
        with open(p, "wb") as fh:
            fh.write(b"PK\x03\x04" + b"\x00" * 4 + b"corrupt")
        with pytest.raises(MalformedDocument):
            container.part_names(p)

    def test_rewrite_rejects_zip_bomb_member_on_copy(self, docx_path, tmp_path):
        # A container with one highly-compressible oversized member passes the
        # inventory vet (member count / CD bytes are small) but must be refused
        # on the byte-preserving copy path, not inflated into memory.
        bomb = str(tmp_path / "bomb.docx")
        big = b"\x00" * (C.MAX_PART_BYTES + 1024)
        with (
            zipfile.ZipFile(docx_path) as src,
            zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as dst,
        ):
            for info in src.infolist():
                dst.writestr(info, src.read(info.filename))
            dst.writestr("word/media/bomb.bin", big)
        out = str(tmp_path / "o.docx")
        with pytest.raises(MalformedDocument):
            container.rewrite_parts(bomb, out, {C.DOCX_MAIN_PART: b"<x/>"})
        assert not os.path.exists(out)

    def test_rewrite_cleanup_on_write_failure(self, docx_path, tmp_path, monkeypatch):
        out = str(tmp_path / "o.docx")
        # Force the write to blow up mid-build; the temp artifact must be gone
        # and the destination untouched.
        real_writestr = zipfile.ZipFile.writestr

        def boom(self, *a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(zipfile.ZipFile, "writestr", boom)
        with pytest.raises(RuntimeError):
            container.rewrite_parts(docx_path, out, {C.DOCX_MAIN_PART: b"<x/>"})
        monkeypatch.setattr(zipfile.ZipFile, "writestr", real_writestr)
        assert not os.path.exists(out)
        # No leftover temp files in the destination dir.
        leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".ooxml-")]
        assert leftovers == []

    def test_parse_without_defusedxml_refuses(self, docx_path, monkeypatch):
        monkeypatch.setattr(container, "_xml_fromstring", None)
        with pytest.raises(MalformedDocument):
            container.parse_xml_part(docx_path, C.DOCX_MAIN_PART)

    def test_corrupt_member_read_raises_typed_error(self, docx_path, monkeypatch):
        # GPT F (:117): a member whose deflate stream/CRC is corrupt raises
        # BadZipFile from the read. It must be translated to MalformedDocument,
        # not escape the engine's typed boundary as a raw zip exception.
        def boom_open(self, name, *a, **k):
            raise zipfile.BadZipFile("bad CRC")

        monkeypatch.setattr(zipfile.ZipFile, "open", boom_open)
        with pytest.raises(MalformedDocument):
            container.read_part(docx_path, C.DOCX_MAIN_PART)

    def test_malformed_edit_xml_raises_typed_error(self, tmp_path):
        # GPT F (:180 / pptx :263): a main part that is not well-formed XML must
        # surface as MalformedDocument from the edit path, not a raw parser
        # exception a caller catching OfficeDocumentError would miss.
        p = str(tmp_path / "badxml.docx")
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(C.CONTENT_TYPES_PART, "<?xml version='1.0'?><Types xmlns='%s'/>" % C.CT_NS)
            z.writestr(C.DOCX_MAIN_PART, "<w:document NOT well formed")
        with pytest.raises(OfficeDocumentError):
            od.edit_in_place(p, str(tmp_path / "o.docx"), {0: "x"})


# ── PPTX notes-resolution edge paths ──


class TestPptxNotesEdges:
    def test_notes_target_with_parent_ref(self, tmp_path):
        # Target uses ../notesSlides/ which must normalize correctly.
        p = str(tmp_path / "n.pptx")
        _make_real_pptx(p, [("body", "the note")])
        assert od.read_presentation(p).slides[0].notes == "the note"

    def test_notes_rel_pointing_at_missing_part(self, tmp_path):
        p = str(tmp_path / "dangling.pptx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(C.PPTX_PRESENTATION_PART, "<p:presentation xmlns:p='%s'/>" % C.P_NS)
            z.writestr("ppt/slides/slide1.xml", _PPTX_SLIDE.format(text="hi"))
            z.writestr(
                "ppt/slides/_rels/slide1.xml.rels",
                _PPTX_SLIDE_RELS.format(n=1),  # points at notesSlide1 which is absent
            )
        # A notes relationship IS stated but its target part is absent (dangling).
        # GPT F (:189): this must FAIL CLOSED, not silently report "no notes" —
        # a slide that HAS notes would otherwise be read as having none.
        with pytest.raises(MalformedDocument):
            od.read_presentation(p)

    def test_replace_slide_no_slides_raises(self, tmp_path):
        p = str(tmp_path / "noslides.pptx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(C.PPTX_PRESENTATION_PART, "<p:presentation xmlns:p='%s'/>" % C.P_NS)
        with pytest.raises(DocumentEditError):
            pptx_mod.replace_slide_text(p, str(tmp_path / "o.pptx"), {})

    def test_normalize_rel_target(self):
        assert (
            pptx_mod._normalize_rel_target("ppt/slides/", "../notesSlides/notesSlide1.xml")
            == "ppt/notesSlides/notesSlide1.xml"
        )
        assert (
            pptx_mod._normalize_rel_target("ppt/slides/", "./slideLayout1.xml")
            == "ppt/slides/slideLayout1.xml"
        )
        # A target that climbs above the package root names no real part.
        assert pptx_mod._normalize_rel_target("ppt/slides/", "../../../../etc") == ""
        assert pptx_mod._normalize_rel_target("ppt/slides/", "../..") == ""


# ── PPTX presentation order (sldIdLst, not filename number) ──


def _make_reordered_pptx(path: str, slide_bodies_in_file_order, sldid_order):
    """Author a .pptx where sldIdLst order differs from slideN.xml filename order.

    *slide_bodies_in_file_order* maps 1-based file number -> body text.
    *sldid_order* is the list of file numbers in the order the id list presents
    them (i.e. true presentation order).
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(C.CONTENT_TYPES_PART, "<?xml version='1.0'?><Types xmlns='%s'/>" % C.CT_NS)
        # presentation.xml carries a sldIdLst whose entries reference rIds.
        sld_ids = "".join(
            "<p:sldId id='%d' r:id='rId%d'/>" % (256 + i, n) for i, n in enumerate(sldid_order)
        )
        z.writestr(
            C.PPTX_PRESENTATION_PART,
            "<?xml version='1.0'?><p:presentation xmlns:p='%s' xmlns:r='%s'>"
            "<p:sldIdLst>%s</p:sldIdLst></p:presentation>" % (C.P_NS, C.R_NS, sld_ids),
        )
        # presentation.xml.rels maps each rId to its slide part.
        rels = "".join(
            "<Relationship Id='rId%d' "
            "Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide' "
            "Target='slides/slide%d.xml'/>" % (n, n)
            for n in sldid_order
        )
        z.writestr(
            "ppt/_rels/presentation.xml.rels",
            "<?xml version='1.0'?><Relationships "
            "xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>%s"
            "</Relationships>" % rels,
        )
        for n, body in slide_bodies_in_file_order.items():
            z.writestr("ppt/slides/slide%d.xml" % n, _PPTX_SLIDE.format(text=body))


class TestPptxPresentationOrder:
    def test_read_follows_sldidlst_not_filename(self, tmp_path):
        # File slide1="A", slide2="B", but the id list presents 2 then 1.
        p = str(tmp_path / "reordered.pptx")
        _make_reordered_pptx(p, {1: "A", 2: "B"}, sldid_order=[2, 1])
        texts = [s.text for s in od.read_presentation(p).slides]
        assert texts == ["B", "A"]

    def test_edit_position_targets_presentation_order(self, tmp_path):
        # Editing position 1 must hit the slide the id list shows first (file 2).
        p = str(tmp_path / "reordered.pptx")
        _make_reordered_pptx(p, {1: "A", 2: "B"}, sldid_order=[2, 1])
        out = str(tmp_path / "o.pptx")
        od.edit_in_place(p, out, {1: "EDITED"})
        assert [s.text for s in od.read_presentation(out).slides] == ["EDITED", "A"]

    def test_dangling_sldid_fails_closed(self, tmp_path):
        # A sldIdLst rId with no matching rel is a STATED-but-BROKEN order. We
        # refuse (MalformedDocument) rather than GUESS filename order — guessing
        # would silently redirect a read/edit to the wrong slide. Fail-closed on
        # a present-but-invalid order, consistent with the gate.
        p = str(tmp_path / "dangling.pptx")
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(C.CONTENT_TYPES_PART, "<?xml version='1.0'?><Types xmlns='%s'/>" % C.CT_NS)
            z.writestr(
                C.PPTX_PRESENTATION_PART,
                "<?xml version='1.0'?><p:presentation xmlns:p='%s' xmlns:r='%s'>"
                "<p:sldIdLst><p:sldId id='256' r:id='rNope'/></p:sldIdLst>"
                "</p:presentation>" % (C.P_NS, C.R_NS),
            )
            z.writestr(
                "ppt/_rels/presentation.xml.rels",
                "<?xml version='1.0'?><Relationships "
                "xmlns='http://schemas.openxmlformats.org/package/2006/relationships'/>",
            )
            z.writestr("ppt/slides/slide1.xml", _PPTX_SLIDE.format(text="only"))
        with pytest.raises(MalformedDocument):
            od.read_presentation(p)

    def test_malformed_present_presentation_fails_closed(self, tmp_path):
        # GPT F (:132): presentation.xml + rels are PRESENT but presentation.xml
        # is not well-formed. A parse failure of a present ordering part is a
        # broken package -> MalformedDocument (fail closed), NOT a fallback to
        # filename order that could target the wrong slide.
        p = str(tmp_path / "badpres.pptx")
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(C.CONTENT_TYPES_PART, "<?xml version='1.0'?><Types xmlns='%s'/>" % C.CT_NS)
            z.writestr(C.PPTX_PRESENTATION_PART, "<p:presentation NOT well formed")
            z.writestr(
                "ppt/_rels/presentation.xml.rels",
                "<?xml version='1.0'?><Relationships "
                "xmlns='http://schemas.openxmlformats.org/package/2006/relationships'/>",
            )
            z.writestr("ppt/slides/slide1.xml", _PPTX_SLIDE.format(text="only"))
        with pytest.raises(MalformedDocument):
            od.read_presentation(p)

    def test_empty_sldidlst_does_not_fall_back_to_orphans(self, tmp_path):
        # GPT F (:163): a PRESENT but EMPTY <p:sldIdLst> states "no slides in
        # order". An orphan slide1.xml on disk must NOT be read/edited via a
        # filename-order fallback (that fallback is only for an ABSENT list).
        p = str(tmp_path / "emptyidlist.pptx")
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(C.CONTENT_TYPES_PART, "<?xml version='1.0'?><Types xmlns='%s'/>" % C.CT_NS)
            z.writestr(
                C.PPTX_PRESENTATION_PART,
                "<?xml version='1.0'?><p:presentation xmlns:p='%s' xmlns:r='%s'>"
                "<p:sldIdLst/></p:presentation>" % (C.P_NS, C.R_NS),
            )
            z.writestr(
                "ppt/_rels/presentation.xml.rels",
                "<?xml version='1.0'?><Relationships "
                "xmlns='http://schemas.openxmlformats.org/package/2006/relationships'/>",
            )
            z.writestr("ppt/slides/slide1.xml", _PPTX_SLIDE.format(text="orphan"))
        # The deck states no ordered slides: read yields no slides (the orphan is
        # not surfaced), and an edit targeting slide 1 is rejected.
        assert od.read_presentation(p).slides == []
        with pytest.raises(DocumentEditError):
            od.edit_in_place(p, str(tmp_path / "o.pptx"), {1: "x"})

    def test_no_sldidlst_falls_back_to_filename_order(self, tmp_path):
        # No <p:sldIdLst> stated at all -> there is no authority to contradict,
        # so filename-number order is the legitimate fallback (NOT fail-closed).
        p = str(tmp_path / "noidlist.pptx")
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(C.CONTENT_TYPES_PART, "<?xml version='1.0'?><Types xmlns='%s'/>" % C.CT_NS)
            z.writestr(
                C.PPTX_PRESENTATION_PART,
                "<?xml version='1.0'?><p:presentation xmlns:p='%s' xmlns:r='%s'/>"
                % (C.P_NS, C.R_NS),
            )
            z.writestr("ppt/slides/slide1.xml", _PPTX_SLIDE.format(text="first"))
            z.writestr("ppt/slides/slide2.xml", _PPTX_SLIDE.format(text="second"))
        assert [s.text for s in od.read_presentation(p).slides] == ["first", "second"]


class TestSensitivePathRejection:
    def test_classify_refuses_sensitive_path(self, tmp_path, monkeypatch):
        p = str(tmp_path / "secret.docx")
        with open(p, "wb") as fh:
            fh.write(b"PK\x03\x04ignored")
        monkeypatch.setattr(
            "kiro_crew.connections.vendors.microsoft.office_documents.rejection."
            "is_sensitive_path",
            lambda path: True,
        )
        verdict = rejection.classify(p)
        assert verdict.ok is False
        assert verdict.reason == "sensitive_path"

    def test_ensure_editable_raises_on_sensitive_path(self, tmp_path, monkeypatch):
        p = str(tmp_path / "secret.docx")
        with open(p, "wb") as fh:
            fh.write(b"PK\x03\x04ignored")
        monkeypatch.setattr(
            "kiro_crew.connections.vendors.microsoft.office_documents.rejection."
            "is_sensitive_path",
            lambda path: True,
        )
        with pytest.raises(OfficeDocumentError):
            rejection.ensure_editable(p)


# ── Round-2 hardening: embedded-content preservation, mode carry, remote paths ──


class TestDocxEmbeddedContentPreserved:
    def _docx_with_drawing_in_para(self, path):
        # Author document.xml where paragraph 0 has a text run AND a drawing,
        # and paragraph 1 has a bookmark. python-docx cannot easily add a raw
        # drawing, so write the part directly through the container.
        import docx as _pd

        d = _pd.Document()
        d.add_paragraph("original text")
        d.add_paragraph("second")
        d.save(path)
        # Inject a <w:drawing/> and a <w:bookmarkStart/> into the parts.
        raw = container.read_part(path, C.DOCX_MAIN_PART).decode("utf-8")
        w = C.W_NS
        # Add a drawing run to the first paragraph and a bookmark to it too.
        inject = (
            f"<w:r xmlns:w='{w}'><w:drawing><w:inline/></w:drawing></w:r>"
            f"<w:bookmarkStart xmlns:w='{w}' w:id='1' w:name='bm'/>"
        )
        raw2 = raw.replace("</w:p>", inject + "</w:p>", 1)
        container.rewrite_parts(path, path, {C.DOCX_MAIN_PART: raw2.encode("utf-8")})

    def test_edit_keeps_drawing_and_bookmark(self, tmp_path):
        p = str(tmp_path / "embed.docx")
        self._docx_with_drawing_in_para(p)
        out = str(tmp_path / "out.docx")
        od.edit_in_place(p, out, {0: "REPLACED"})
        edited = container.read_part(out, C.DOCX_MAIN_PART).decode("utf-8")
        # Text replaced...
        assert "REPLACED" in edited
        assert "original text" not in edited
        # ...but the embedded drawing and bookmark survive.
        assert "drawing" in edited
        assert "bookmarkStart" in edited

    def test_edit_keeps_drawing_inside_the_edited_run(self, tmp_path):
        # GPT F: a single run carries BOTH <w:t> text AND a <w:drawing> (a
        # text-and-picture run). Collapsing that run to just its new text must
        # NOT delete the drawing. Author document.xml with such a run directly.
        p = str(tmp_path / "mixedrun.docx")
        w = C.W_NS
        doc = (
            f"<?xml version='1.0'?><w:document xmlns:w='{w}'><w:body>"
            f"<w:p><w:r><w:t>original</w:t>"
            f"<w:drawing><w:inline w:distT='0'/></w:drawing></w:r></w:p>"
            f"</w:body></w:document>"
        )
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(C.CONTENT_TYPES_PART, "<?xml version='1.0'?><Types xmlns='%s'/>" % C.CT_NS)
            z.writestr(C.DOCX_MAIN_PART, doc)
        out = str(tmp_path / "mixedrun-out.docx")
        od.edit_in_place(p, out, {0: "REPLACED"})
        edited = container.read_part(out, C.DOCX_MAIN_PART).decode("utf-8")
        assert "REPLACED" in edited
        assert "original" not in edited
        # The drawing embedded in the SAME run as the edited text is preserved.
        assert "drawing" in edited
        assert "inline" in edited

    def test_edit_keeps_drawing_in_a_later_run(self, tmp_path):
        # GPT F (:231): a LATER text-bearing run (text_runs[1:]) that also carries
        # a <w:drawing> must not be removed wholesale — strip its text, keep the
        # graphic. Two text runs: run 1 plain text, run 2 text + drawing.
        p = str(tmp_path / "laterrun.docx")
        w = C.W_NS
        doc = (
            f"<?xml version='1.0'?><w:document xmlns:w='{w}'><w:body>"
            f"<w:p>"
            f"<w:r><w:t>first</w:t></w:r>"
            f"<w:r><w:t>second</w:t><w:drawing><w:inline w:distT='0'/></w:drawing></w:r>"
            f"</w:p>"
            f"</w:body></w:document>"
        )
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(C.CONTENT_TYPES_PART, "<?xml version='1.0'?><Types xmlns='%s'/>" % C.CT_NS)
            z.writestr(C.DOCX_MAIN_PART, doc)
        out = str(tmp_path / "laterrun-out.docx")
        od.edit_in_place(p, out, {0: "REPLACED"})
        edited = container.read_part(out, C.DOCX_MAIN_PART).decode("utf-8")
        assert "REPLACED" in edited
        assert "second" not in edited  # the later run's text is gone
        # ...but the drawing that shared that later run survives.
        assert "drawing" in edited
        assert "inline" in edited


class TestContainerModePreserved:
    def test_in_place_edit_preserves_file_mode(self, docx_path):
        # The invariant is PRESERVATION: an in-place edit must not silently
        # tighten the file's mode to mkstemp's private 0600. Assert before ==
        # after portably; only POSIX maps a chmod(0o644) back to 0o644 (Windows
        # normalizes modes, so a literal-octal assert is POSIX-only).
        import stat

        os.chmod(docx_path, 0o644)
        before = stat.S_IMODE(os.stat(docx_path).st_mode)
        od.edit_in_place(docx_path, docx_path, {0: "x"})
        after = stat.S_IMODE(os.stat(docx_path).st_mode)
        assert after == before  # not downgraded to mkstemp's 0600
        if os.name == "posix":
            assert after == 0o644

    def test_chown_runs_before_chmod(self, docx_path, tmp_path, monkeypatch):
        # OPUS FINDING (:654): on Linux os.chown clears S_ISUID/S_ISGID, so the
        # metadata carry must chown FIRST and chmod LAST — otherwise editing a
        # setgid/setuid document silently strips those bits. Record call order.
        cm = "kiro_crew.connections.vendors.microsoft.office_documents.container"
        order: list[str] = []
        import os as _os

        if not hasattr(_os, "chown"):
            pytest.skip("platform has no os.chown")
        real_chmod = _os.chmod

        def rec_chmod(path, mode, *a, **k):
            order.append("chmod")
            return real_chmod(path, mode, *a, **k)

        def rec_chown(path, uid, gid, *a, **k):
            order.append("chown")
            # Do not actually chown (needs privilege); just record the order.

        monkeypatch.setattr(f"{cm}.os.chmod", rec_chmod)
        monkeypatch.setattr(f"{cm}.os.chown", rec_chown)
        out = str(tmp_path / "out.docx")
        od.edit_in_place(docx_path, out, {0: "x"})
        assert order[:2] == ["chown", "chmod"], f"expected chown before chmod, got {order}"


class TestContainerXattrCarry:
    def test_edit_refuses_when_an_xattr_cannot_be_carried(self, docx_path, tmp_path, monkeypatch):
        # GPT F (origin: validation): the atomic swap must not silently drop an
        # access restriction. If the source carries an xattr (e.g. a POSIX ACL)
        # that we cannot re-set on the replacement, refuse BEFORE os.replace so
        # the destination is left untouched rather than published with the
        # restriction lost. Simulate portably: report one xattr on the model and
        # make setxattr fail, then assert the edit aborts and dst is unchanged.
        cm = "kiro_crew.connections.vendors.microsoft.office_documents.container"
        real_os = __import__("os")
        if not all(hasattr(real_os, a) for a in ("listxattr", "getxattr", "setxattr")):
            pytest.skip("platform has no os.*xattr API")
        out = str(tmp_path / "out.docx")
        # Seed dst with sentinel bytes so we can prove it is NOT overwritten.
        with open(out, "wb") as fh:
            fh.write(b"SENTINEL-UNTOUCHED")
        monkeypatch.setattr(
            f"{cm}.os.listxattr",
            lambda path, follow_symlinks=False: ["system.posix_acl_access"],
        )
        monkeypatch.setattr(
            f"{cm}.os.getxattr",
            lambda path, attr, follow_symlinks=False: b"acl-bytes",
        )

        def _refuse_setxattr(path, attr, value, follow_symlinks=False):
            raise OSError("operation not supported")

        monkeypatch.setattr(f"{cm}.os.setxattr", _refuse_setxattr)
        with pytest.raises(DocumentEditError) as ei:
            od.edit_in_place(docx_path, out, {0: "REPLACED"})
        assert ei.value.reason == "metadata_not_carried"
        # Destination untouched: the failed edit did not publish a replacement.
        with open(out, "rb") as fh:
            assert fh.read() == b"SENTINEL-UNTOUCHED"

    def test_edit_refuses_when_a_listed_xattr_cannot_be_read(
        self, docx_path, tmp_path, monkeypatch
    ):
        # GPT F (:333): an attribute that is LISTED but unREADABLE must not be
        # silently skipped — it could be an access-control xattr (a POSIX ACL),
        # so dropping it could publish a replacement missing a control. Fail
        # closed with the destination untouched.
        cm = "kiro_crew.connections.vendors.microsoft.office_documents.container"
        real_os = __import__("os")
        if not all(hasattr(real_os, a) for a in ("listxattr", "getxattr", "setxattr")):
            pytest.skip("platform has no os.*xattr API")
        out = str(tmp_path / "out.docx")
        with open(out, "wb") as fh:
            fh.write(b"SENTINEL-UNTOUCHED")
        monkeypatch.setattr(
            f"{cm}.os.listxattr",
            lambda path, follow_symlinks=False: ["system.posix_acl_access"],
        )

        def _refuse_getxattr(path, attr, follow_symlinks=False):
            raise OSError("permission denied reading xattr")

        monkeypatch.setattr(f"{cm}.os.getxattr", _refuse_getxattr)
        with pytest.raises(DocumentEditError) as ei:
            od.edit_in_place(docx_path, out, {0: "REPLACED"})
        assert ei.value.reason == "metadata_not_carried"
        with open(out, "rb") as fh:
            assert fh.read() == b"SENTINEL-UNTOUCHED"

    def test_edit_succeeds_when_a_nonacl_xattr_cannot_be_carried(
        self, docx_path, tmp_path, monkeypatch
    ):
        # DESIGN WATCH / SUGGESTIONS: a NON-access-control xattr (security.selinux,
        # a kernel-computed label present on every file on an SELinux-enforcing
        # host and not re-settable by an unprivileged process) must be BEST
        # EFFORT — the edit succeeds, the replacement gets the correct default
        # label. Failing here would break every in-place edit on RHEL/SELinux.
        cm = "kiro_crew.connections.vendors.microsoft.office_documents.container"
        real_os = __import__("os")
        if not all(hasattr(real_os, a) for a in ("listxattr", "getxattr", "setxattr")):
            pytest.skip("platform has no os.*xattr API")
        out = str(tmp_path / "out.docx")
        monkeypatch.setattr(
            f"{cm}.os.listxattr",
            lambda path, follow_symlinks=False: ["security.selinux"],
        )
        monkeypatch.setattr(
            f"{cm}.os.getxattr",
            lambda path, attr, follow_symlinks=False: b"system_u:object_r:tmp_t:s0",
        )

        def _refuse_setxattr(path, attr, value, follow_symlinks=False):
            raise OSError("operation not permitted")

        monkeypatch.setattr(f"{cm}.os.setxattr", _refuse_setxattr)
        # No exception: the edit completes despite the unsettable SELinux label.
        od.edit_in_place(docx_path, out, {0: "REPLACED"})
        assert "REPLACED" in container.read_part(out, C.DOCX_MAIN_PART).decode("utf-8")


class TestRemotePathRejection:
    def test_unc_backslash_path_refused(self):
        v = rejection.classify(r"\\server\share\doc.docx")
        assert v.ok is False and v.reason == "sensitive_path"

    def test_double_slash_path_refused(self):
        v = rejection.classify("//server/share/doc.docx")
        assert v.ok is False and v.reason == "sensitive_path"

    def test_url_scheme_path_refused(self):
        v = rejection.classify("smb://host/doc.docx")
        assert v.ok is False and v.reason == "sensitive_path"

    def test_plain_local_path_not_flagged_as_remote(self, docx_path):
        # A normal local docx still classifies ok (control for the remote check).
        assert rejection.classify(docx_path).ok is True

    def test_write_refuses_remote_destination(self, docx_path):
        # OPUS BLOCKING (:545): the WRITE path must refuse a remote dst_path the
        # same way the read path refuses a remote source. A remote dst would run
        # mkstemp(dir=remote)+os.replace -> outbound SMB/NTLM write leaking the
        # NTLM hash on Windows. rewrite_parts guards dst before any mkstemp.
        raw = container.read_part(docx_path, C.DOCX_MAIN_PART)
        with pytest.raises(OfficeDocumentError) as ei:
            container.rewrite_parts(
                docx_path, r"\\attacker\share\out.docx", {C.DOCX_MAIN_PART: raw}
            )
        assert ei.value.reason == "sensitive_path"

    def test_create_from_template_refuses_remote_destination(self, docx_path):
        # The entry point that composes src+dst must refuse a remote destination
        # too (the reviewer's exact PoC shape).
        with pytest.raises(OfficeDocumentError) as ei:
            od.create_from_template(docx_path, r"\\attacker\share\out.docx")
        assert ei.value.reason == "sensitive_path"

    def test_write_refuses_url_scheme_destination(self, docx_path):
        raw = container.read_part(docx_path, C.DOCX_MAIN_PART)
        with pytest.raises(OfficeDocumentError) as ei:
            container.rewrite_parts(docx_path, "smb://host/out.docx", {C.DOCX_MAIN_PART: raw})
        assert ei.value.reason == "sensitive_path"


# ── Round-3 hardening: rPr preservation, corrupt-notes surfacing, UNC-before-stat, concurrent pin ──


class TestDocxRunFormattingPreserved:
    def _docx_with_formatted_run(self, path):
        import docx as _pd

        d = _pd.Document()
        d.add_paragraph("plain")
        d.save(path)
        # Give paragraph 0's run an <w:rPr> with bold, and add a hyperlink-nested
        # run so the edit must find runs below <w:p> too.
        raw = container.read_part(path, C.DOCX_MAIN_PART).decode("utf-8")
        w = C.W_NS
        rpr = f"<w:rPr xmlns:w='{w}'><w:b/></w:rPr>"
        # Inject rPr as the first child of the first run.
        raw2 = raw.replace("<w:r>", "<w:r>" + rpr, 1)
        container.rewrite_parts(path, path, {C.DOCX_MAIN_PART: raw2.encode("utf-8")})

    def test_edit_keeps_run_properties(self, tmp_path):
        p = str(tmp_path / "fmt.docx")
        self._docx_with_formatted_run(p)
        out = str(tmp_path / "out.docx")
        od.edit_in_place(p, out, {0: "BOLDNEW"})
        edited = container.read_part(out, C.DOCX_MAIN_PART)
        # Assert by NAMESPACE (Clark notation), not literal prefix: the serializer
        # may emit ns0:/ns1: prefixes, which are namespace-equivalent to w:.
        root = _indep_fromstring(edited)
        assert any(t.text == "BOLDNEW" for t in root.iter(f"{C.W}t"))
        # The run's <w:rPr>/<w:b> (bold) survived the text replacement.
        assert root.iter(f"{C.W}rPr") is not None
        assert any(True for _ in root.iter(f"{C.W}b"))

    def test_edit_removes_extra_runs_including_hyperlink_nested(self, tmp_path):
        # A paragraph with a second text run nested in <w:hyperlink>: the edit
        # keeps the first run's text (replaced) and removes the extra run wherever
        # it sits (exercises _parent_of + the text_runs[1:] removal branch).
        import docx as _pd

        p = str(tmp_path / "multi.docx")
        d = _pd.Document()
        d.add_paragraph("first")
        d.save(p)
        raw = container.read_part(p, C.DOCX_MAIN_PART).decode("utf-8")
        w = C.W_NS
        extra = f"<w:hyperlink xmlns:w='{w}'><w:r><w:t>LINKTEXT</w:t></w:r></w:hyperlink>"
        raw2 = raw.replace("</w:p>", extra + "</w:p>", 1)
        container.rewrite_parts(p, p, {C.DOCX_MAIN_PART: raw2.encode("utf-8")})
        out = str(tmp_path / "o.docx")
        od.edit_in_place(p, out, {0: "ONLYTEXT"})
        texts = [para.text for para in pydocx.Document(out).paragraphs]
        assert "ONLYTEXT" in texts[0]
        # The extra hyperlink-nested run's text was removed (collapsed to one run).
        assert "LINKTEXT" not in texts[0]


class TestPptxCorruptNotesSurfaces:
    def test_corrupt_notes_part_raises_not_blanked(self, tmp_path):
        p = str(tmp_path / "corruptnotes.pptx")
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(C.CONTENT_TYPES_PART, "<?xml version='1.0'?><Types xmlns='%s'/>" % C.CT_NS)
            z.writestr(C.PPTX_PRESENTATION_PART, "<p:presentation xmlns:p='%s'/>" % C.P_NS)
            z.writestr("ppt/slides/slide1.xml", _PPTX_SLIDE.format(text="body"))
            z.writestr("ppt/slides/_rels/slide1.xml.rels", _PPTX_SLIDE_RELS.format(n=1))
            # notesSlide1.xml exists (rels resolves) but is not well-formed XML.
            z.writestr("ppt/notesSlides/notesSlide1.xml", "<not well formed")
        with pytest.raises(MalformedDocument):
            od.read(p)


class TestUncBeforeStat:
    def test_ensure_editable_refuses_unc_without_stat(self, monkeypatch):
        # ensure_editable must reject a UNC path BEFORE os.path.exists (which on
        # Windows would itself probe SMB). Fail the test if exists() is reached.
        import kiro_crew.connections.vendors.microsoft.office_documents.rejection as rj

        def boom(_):
            raise AssertionError("os.path.exists reached for a UNC path")

        monkeypatch.setattr(rj.os.path, "exists", boom)
        with pytest.raises(OfficeDocumentError):
            rj.ensure_editable(r"\\server\share\x.docx")
        with pytest.raises(OfficeDocumentError):
            rj.ensure_editable("//server/share/x.docx")


class TestSymlinkSourceRefused:
    def test_symlinked_source_refused_by_nofollow(self, docx_path, tmp_path):
        # GPT F (:80): the source open is O_NOFOLLOW-pinned, so a symlink swapped
        # in at the name after the sensitive-path screen is refused at open time
        # (TOCTOU close). A symlink pointing at a real docx must not be followed.
        if not hasattr(os, "symlink") or not getattr(os, "O_NOFOLLOW", 0):
            pytest.skip("platform has no O_NOFOLLOW/symlink")
        link = str(tmp_path / "link.docx")
        try:
            os.symlink(docx_path, link)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create symlink on this platform")
        # Reading through the symlink is refused as a malformed/unopenable
        # container rather than silently followed into the target.
        with pytest.raises(MalformedDocument):
            container.read_part(link, C.DOCX_MAIN_PART)


class TestConcurrentWriterPin:
    def test_stale_signature_aborts_replace(self, docx_path, tmp_path):
        # A source that changed since the pinned signature must abort the swap.
        # A concurrent-writer race is an environmental WRITE-TIME failure, not a
        # broken container, so it raises DocumentEditError (reason=source_changed)
        # — NOT MalformedDocument, which is reserved for actually-broken bytes.
        raw = container.read_part(docx_path, C.DOCX_MAIN_PART)
        stale = (123456789, 111111111)  # a signature that cannot match the file
        with pytest.raises(DocumentEditError) as ei:
            container.rewrite_parts(
                docx_path, docx_path, {C.DOCX_MAIN_PART: raw}, expect_source_signature=stale
            )
        assert ei.value.reason == "source_changed"
        # Destination untouched (still opens and reads).
        assert container.read_part(docx_path, C.DOCX_MAIN_PART)

    def test_matching_signature_allows_replace(self, docx_path):
        sig = container.source_signature(docx_path)
        raw = container.read_part(docx_path, C.DOCX_MAIN_PART)
        container.rewrite_parts(
            docx_path, docx_path, {C.DOCX_MAIN_PART: raw}, expect_source_signature=sig
        )
        assert container.read_part(docx_path, C.DOCX_MAIN_PART)
