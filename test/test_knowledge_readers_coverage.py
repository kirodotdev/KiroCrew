"""Coverage for the knowledge FileReader: decoding, error sentinels, optional
dependencies, HTML fallback and the AsciiDoc include guards."""

import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

from kiro_crew.knowledge import readers
from kiro_crew.knowledge.readers import FileReader, _AdocIncludes


def _members(*paths):
    return frozenset(os.path.normcase(str(p)) for p in paths)


def test_utf16_bom_is_decoded_both_byte_orders():
    assert readers._decode_text_bytes("hi\nthere".encode("utf-16")) == "hi\nthere"
    assert readers._decode_text_bytes(b"\xfe\xff" + "hi".encode("utf-16-be")) == "hi"


def test_broken_utf16_bom_degrades_to_latin1():
    assert readers._decode_text_bytes(b"\xff\xfeA") == "\xff\xfeA"


def test_utf8_bom_dropped_and_crlf_normalised():
    assert readers._decode_text_bytes(b"\xef\xbb\xbf# H\r\nx\ry") == "# H\nx\ny"


def test_invalid_utf8_falls_back_to_latin1():
    assert readers._decode_text_bytes(b"caf\xe9") == "caf\xe9"


def test_error_and_missing_dependency_sentinels():
    text, meta = readers._read_error(OSError("gone"))
    assert text == "Error reading file: gone"
    assert meta == {"format": "error", "error": "gone"}
    text, meta = readers._missing_dep("PDF", "pdfplumber")
    assert text == "PDF support requires pdfplumber: pip install pdfplumber"
    assert meta["format"] == "error"


def test_sensitive_path_is_refused(tmp_path, monkeypatch):
    doc = tmp_path / "notes.md"
    doc.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(readers, "is_sensitive_path", lambda p: True)
    with pytest.raises(PermissionError, match="Refusing to read sensitive path"):
        FileReader().read(str(doc))


def test_plain_text_reports_format_and_line_count(tmp_path):
    doc = tmp_path / "data.zzz"
    doc.write_bytes(b"one\ntwo")
    text, meta = FileReader().read(str(doc))
    assert text == "one\ntwo"
    assert meta["format"] == "zzz" and meta["line_count"] == 2
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    assert FileReader().read(str(empty))[1]["line_count"] == 0


def test_unreadable_text_becomes_error_result(tmp_path):
    text, meta = FileReader()._read_text(str(tmp_path / "missing.md"), "md")
    assert text.startswith("Error reading file:") and meta["format"] == "error"


def test_pdf_without_pdfplumber_is_a_missing_dependency(tmp_path, monkeypatch):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"%PDF")
    monkeypatch.setattr(readers, "pdfplumber_available", lambda: False)
    text, meta = FileReader().read(str(doc))
    assert "pdfplumber" in text and meta["format"] == "error"


def test_pdf_segments_are_joined_and_truncation_recorded(tmp_path, monkeypatch):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"%PDF")
    outcome = SimpleNamespace(
        failure=None, pages=2, truncated=True, segments=[("p1", "one"), ("p2", "two")]
    )
    monkeypatch.setattr(readers, "pdfplumber_available", lambda: True)
    monkeypatch.setattr(readers, "extract_pdf_segments", lambda *a, **k: outcome)
    text, meta = FileReader().read(str(doc))
    assert text == "one\ntwo"
    assert meta["page_count"] == 2 and meta["truncated"] is True


def test_pdf_extractor_failure_is_an_error(tmp_path, monkeypatch):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"%PDF")
    monkeypatch.setattr(readers, "pdfplumber_available", lambda: True)
    monkeypatch.setattr(
        readers, "extract_pdf_segments", lambda *a, **k: SimpleNamespace(failure="too big")
    )
    text, meta = FileReader().read(str(doc))
    assert "PDF extraction failed: too big" in text and meta["format"] == "error"


def test_pptx_without_python_pptx_is_a_missing_dependency(tmp_path, monkeypatch):
    doc = tmp_path / "deck.pptx"
    doc.write_bytes(b"PK")
    monkeypatch.setattr(readers, "Presentation", None)
    text, meta = FileReader().read(str(doc))
    assert "python-pptx" in text and meta["format"] == "error"


def test_pptx_parse_failure_is_an_error(tmp_path, monkeypatch):
    doc = tmp_path / "deck.pptx"
    doc.write_bytes(b"PK")

    def broken(path):
        raise ValueError("corrupt deck")

    monkeypatch.setattr(readers, "Presentation", broken)
    text, meta = FileReader()._read_pptx(str(doc))
    assert text == "Error reading file: corrupt deck" and meta["format"] == "error"


def test_docx_without_python_docx_is_a_missing_dependency(tmp_path, monkeypatch):
    doc = tmp_path / "a.docx"
    doc.write_bytes(b"PK")
    monkeypatch.setitem(sys.modules, "docx", None)
    text, meta = FileReader()._read_docx(str(doc))
    assert "python-docx" in text and meta["format"] == "error"


def test_docx_parse_failure_is_an_error(tmp_path, monkeypatch):
    doc = tmp_path / "a.docx"
    doc.write_bytes(b"PK")
    fake = ModuleType("docx")
    fake.Document = lambda path: (_ for _ in ()).throw(ValueError("not a zip"))
    monkeypatch.setitem(sys.modules, "docx", fake)
    monkeypatch.setitem(sys.modules, "docx.oxml.ns", SimpleNamespace(qn=lambda t: t))
    monkeypatch.setitem(sys.modules, "docx.text.paragraph", SimpleNamespace(Paragraph=None))
    text, meta = FileReader()._read_docx(str(doc))
    assert "not a zip" in text and meta["format"] == "error"


def test_html_fallback_strips_scripts_styles_and_tags(tmp_path, monkeypatch):
    doc = tmp_path / "page.html"
    doc.write_text(
        "<style>p{}</style><script>var x=1;</script><p>Hello <b>there</b></p>",
        encoding="utf-8",
    )
    monkeypatch.setattr(readers, "_html2text_mod", None)
    text, meta = FileReader().read(str(doc))
    assert text == "Hello there" and meta["format"] == "html"


def test_html_uses_html2text_when_installed(tmp_path, monkeypatch):
    seen = []

    class FakeH2T:
        def __init__(self):
            seen.append(self)
            self.ignore_links = None
            self.ignore_images = None

        def handle(self, html):
            return "converted:" + html

    doc = tmp_path / "page.htm"
    doc.write_text("<p>x</p>", encoding="utf-8")
    monkeypatch.setattr(readers, "_html2text_mod", SimpleNamespace(HTML2Text=FakeH2T))
    text, meta = FileReader().read(str(doc))
    assert text == "converted:<p>x</p>" and meta["format"] == "html"
    assert seen[0].ignore_links is False and seen[0].ignore_images is True


def test_asciidoc_read_error_is_reported(tmp_path):
    text, meta = FileReader()._read_asciidoc(str(tmp_path / "nope.adoc"), frozenset(), None)
    assert text.startswith("Error reading file:") and meta["format"] == "error"


def test_include_guards_leave_directive_as_written(tmp_path, monkeypatch):
    (tmp_path / "part.adoc").write_text("0123456789", encoding="utf-8")
    inc = _AdocIncludes(str(tmp_path), _members(tmp_path / "part.adoc"))
    for target in ("a\x00b", "{attr}.adoc", "https://h/x.adoc", str(tmp_path / "part.adoc")):
        assert inc._load(target, str(tmp_path / "doc.adoc"), ()) is None
    inc.chars = readers._ADOC_MAX_CHARS
    assert inc._load("part.adoc", str(tmp_path / "doc.adoc"), ()) is None
    monkeypatch.setattr(readers, "_ADOC_MAX_INCLUDE_BYTES", 4)
    inc = _AdocIncludes(str(tmp_path), _members(tmp_path / "part.adoc"))
    assert inc._load("part.adoc", str(tmp_path / "doc.adoc"), ()) is None


def test_include_limits_depth_count_and_cycles(tmp_path, monkeypatch):
    (tmp_path / "a.adoc").write_text("top\ninclude::b.adoc[]", encoding="utf-8")
    (tmp_path / "b.adoc").write_text("bee\ninclude::a.adoc[]", encoding="utf-8")
    members = _members(tmp_path / "a.adoc", tmp_path / "b.adoc")
    reader = FileReader()
    text, _ = reader._read_asciidoc(str(tmp_path / "a.adoc"), members, None)
    assert text == "top\nbee\ninclude::a.adoc[]"
    monkeypatch.setattr(readers, "_ADOC_MAX_INCLUDE_DEPTH", 0)
    text, _ = reader._read_asciidoc(str(tmp_path / "a.adoc"), members, None)
    assert text == "top\ninclude::b.adoc[]"
    # Depth back at its default, so only the include count can stop the include now.
    monkeypatch.setattr(readers, "_ADOC_MAX_INCLUDE_DEPTH", 8)
    monkeypatch.setattr(readers, "_ADOC_MAX_INCLUDES", 0)
    text, _ = reader._read_asciidoc(str(tmp_path / "a.adoc"), members, None)
    assert text == "top\ninclude::b.adoc[]"


def test_allowed_refuses_outside_hidden_unlisted_and_relative_roots(tmp_path):
    (tmp_path / ".hidden").mkdir()
    hidden = tmp_path / ".hidden" / "x.adoc"
    hidden.write_text("x", encoding="utf-8")
    listed = tmp_path / "ok.adoc"
    listed.write_text("ok", encoding="utf-8")
    inc = _AdocIncludes(str(tmp_path), _members(hidden, listed))
    assert inc._allowed(str(hidden)) is False
    assert inc._allowed(str(listed)) is True
    assert inc._allowed(str(tmp_path / "other.adoc")) is False
    assert inc._allowed("/elsewhere/x.adoc") is False
    assert _AdocIncludes("relative-root", frozenset())._allowed("/abs/x.adoc") is False


def test_kernel_path_judged_lexically_and_recorded(tmp_path):
    part = tmp_path / "part.adoc"
    part.write_text("p", encoding="utf-8")
    inc = _AdocIncludes(str(tmp_path), _members(part))
    assert inc._refuse_opened(str(part)) is False
    assert inc.opened == [str(part)]
    assert inc._refuse_opened("/elsewhere/part.adoc") is True
    assert inc._read_pinned(str(tmp_path / "missing.adoc")) is None


def test_included_set_records_real_paths_inlined(tmp_path):
    (tmp_path / "part.adoc").write_text("body", encoding="utf-8")
    doc = tmp_path / "doc.adoc"
    doc.write_text("include::part.adoc[]", encoding="utf-8")
    included = set()
    text, _ = FileReader().read(
        str(doc), include_paths=_members(tmp_path / "part.adoc"), included=included
    )
    assert text == "body"
    assert included == {os.path.normcase(os.path.realpath(tmp_path / "part.adoc"))}
