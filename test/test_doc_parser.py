"""Tests for document text extraction (doc_parser.py)."""

from __future__ import annotations

import logging
import os
import tempfile
import zipfile

import pytest

from kiro_crew.doc_parser import (
    extract_text,
    is_parseable_document,
)

# ── Helpers ──


def _make_docx(paragraphs: list[str]) -> str:
    """Create a minimal .docx file and return its path."""
    body = "\n".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    fd, path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return path


def _make_pptx(slides: list[list[str]]) -> str:
    """Create a minimal .pptx file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".pptx")
    os.close(fd)
    with zipfile.ZipFile(path, "w") as zf:
        for i, texts in enumerate(slides, 1):
            shapes = "\n".join(
                f"<p:sp><p:txBody><a:p><a:r><a:t>{t}</a:t></a:r></a:p></p:txBody></p:sp>"
                for t in texts
            )
            xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
                ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                f"<p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>"
            )
            zf.writestr(f"ppt/slides/slide{i}.xml", xml)
    return path


# ── is_parseable_document ──

class TestIsParseableDocument:
    def test_docx_mimetype(self):
        mt = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert is_parseable_document(mimetype=mt)

    def test_pptx_mimetype(self):
        mt = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        assert is_parseable_document(mimetype=mt)

    def test_pdf_mimetype(self):
        assert is_parseable_document(mimetype="application/pdf")

    def test_extension_docx(self):
        assert is_parseable_document(filename="report.docx")

    def test_extension_pptx(self):
        assert is_parseable_document(filename="deck.pptx")

    def test_extension_pdf(self):
        assert is_parseable_document(filename="paper.pdf")

    def test_text_not_parseable(self):
        assert not is_parseable_document(mimetype="text/plain")

    def test_image_not_parseable(self):
        assert not is_parseable_document(mimetype="image/png")

    def test_empty_not_parseable(self):
        assert not is_parseable_document()


# ── DOCX extraction ──

class TestExtractDocx:
    def test_basic_paragraphs(self):
        path = _make_docx(["Hello World", "Second paragraph"])
        try:
            result = extract_text(path, filename="test.docx")
            assert "Hello World" in result
            assert "Second paragraph" in result
        finally:
            os.unlink(path)

    def test_empty_docx(self):
        fd, path = tempfile.mkstemp(suffix=".docx")
        os.close(fd)
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(
                "word/document.xml",
                '<?xml version="1.0"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body></w:body></w:document>",
            )
        try:
            result = extract_text(path, filename="empty.docx")
            assert result == ""
        finally:
            os.unlink(path)

    def test_missing_document_xml(self):
        fd, path = tempfile.mkstemp(suffix=".docx")
        os.close(fd)
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("other.xml", "<root/>")
        try:
            result = extract_text(path, filename="bad.docx")
            assert result == ""
        finally:
            os.unlink(path)

    def test_mimetype_detection(self):
        path = _make_docx(["Via mimetype"])
        try:
            mt = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            result = extract_text(path, mimetype=mt)
            assert "Via mimetype" in result
        finally:
            os.unlink(path)


# ── PPTX extraction ──

class TestExtractPptx:
    def test_single_slide(self):
        path = _make_pptx([["Title", "Body text"]])
        try:
            result = extract_text(path, filename="deck.pptx")
            assert "Slide 1" in result
            assert "Title" in result
            assert "Body text" in result
        finally:
            os.unlink(path)

    def test_multiple_slides(self):
        path = _make_pptx([["Slide One"], ["Slide Two"]])
        try:
            result = extract_text(path, filename="multi.pptx")
            assert "Slide 1" in result
            assert "Slide 2" in result
            assert "Slide One" in result
            assert "Slide Two" in result
        finally:
            os.unlink(path)

    def test_empty_pptx(self):
        fd, path = tempfile.mkstemp(suffix=".pptx")
        os.close(fd)
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
        try:
            result = extract_text(path, filename="empty.pptx")
            assert result == ""
        finally:
            os.unlink(path)


# ── PDF extraction ──

class TestExtractPdf:
    def test_simple_pdf_text(self):
        """A minimal PDF with uncompressed text."""
        pdf_bytes = (
            b"%PDF-1.0\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"stream\n"
            b"BT /F1 12 Tf (Hello from PDF) Tj ET\n"
            b"endstream\n"
            b"%%EOF"
        )
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(pdf_bytes)
            result = extract_text(path, filename="test.pdf")
            assert "Hello from PDF" in result
        finally:
            os.unlink(path)

    def test_empty_pdf(self):
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(b"%PDF-1.0\n%%EOF")
            result = extract_text(path, filename="empty.pdf")
            assert result == ""
        finally:
            os.unlink(path)


# ── Error handling ──

class TestErrorHandling:
    def test_nonexistent_file(self):
        result = extract_text("/nonexistent/file.docx", filename="file.docx")
        assert result == ""

    def test_corrupt_zip(self):
        fd, path = tempfile.mkstemp(suffix=".docx")
        os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(b"not a zip file")
            result = extract_text(path, filename="corrupt.docx")
            assert result == ""
        finally:
            os.unlink(path)

    def test_unknown_extension(self):
        result = extract_text("/tmp/file.xyz", filename="file.xyz")
        assert result == ""

    def test_unknown_mimetype(self):
        result = extract_text("/tmp/file", mimetype="application/octet-stream")
        assert result == ""

    def test_sensitive_path_rejected(self, caplog):
        """extract_text refuses to read sensitive paths."""
        with caplog.at_level(logging.WARNING):
            result = extract_text(
                os.path.expanduser("~/.aws/credentials"), filename="credentials.docx"
            )
        assert result == ""
        assert "Refusing to read sensitive path" in caplog.text


# ── Decompression bomb guards ──

class TestDecompressionGuards:
    def test_oversized_zip_entry_skipped(self, caplog):
        """A ZIP entry whose actual decompressed content exceeds the limit is skipped."""
        from unittest.mock import patch

        import kiro_crew.doc_parser as dp

        path = _make_docx(["Normal text"])
        try:
            # Temporarily lower the limit so the real entry exceeds it
            with patch.object(dp, "_MAX_ZIP_ENTRY", 5):
                with caplog.at_level(logging.WARNING):
                    result = extract_text(path, filename="bomb.docx")
            assert result == ""
            assert "ZIP entry too large" in caplog.text
        finally:
            os.unlink(path)

    def test_safe_decompress_rejects_oversized(self):
        """_safe_decompress raises on output exceeding max_size."""
        import zlib as _zlib

        from kiro_crew.doc_parser import _safe_decompress

        # Compress 1 MB of zeros
        big = _zlib.compress(b"\x00" * (1024 * 1024))
        # Allow only 100 bytes of output
        with pytest.raises(ValueError, match="exceeds size limit"):
            _safe_decompress(big, max_size=100)
