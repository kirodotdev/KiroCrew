"""Regression tests for the content-signature gate on ``/api/knowledge/ingest``.

CWE-434: the ingest handler derives the file type from the attacker-controlled
multipart filename extension, and ``FileReader.read()`` dispatches to a binary
parser (.pdf -> pdfplumber, .docx -> python-docx) purely by that extension. A
file whose bytes don't match its claimed extension (e.g. an HTML/script payload
named ``x.pdf``) must be rejected with HTTP 400 BEFORE it reaches the parser --
mirroring the sibling ``api_upload_file`` magic-byte gate. Genuine text formats
(.md/.txt/.html) have no reliable signature and still ingest.
"""
from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.knowledge import ingest_file


class _FakeStore:
    def __init__(self) -> None:
        self.db = MagicMock()

    def get_source_by_uri(self, uri):
        return None

    def add_source(self, *, name, source_type, uri, properties):
        return "sid1"


def _make_app() -> tuple[web.Application, AsyncMock]:
    app = web.Application()
    app["state"] = SimpleNamespace(knowledge_store=_FakeStore())
    ingest_spy = AsyncMock()
    app["knowledge_pipeline"] = SimpleNamespace(ingest_file=ingest_spy)
    app.router.add_post("/api/knowledge/ingest", ingest_file)
    return app, ingest_spy


def _minimal_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<types/>")
    return buf.getvalue()


def _multi_member_zip_bytes(extra: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<types/>")
        for i in range(extra):
            zf.writestr(f"word/part{i}.xml", "<x/>")
    return buf.getvalue()


async def _post(app: web.Application, data: bytes, filename: str, ctype: str):
    form = aiohttp.FormData()
    form.add_field("file", data, filename=filename, content_type=ctype)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/knowledge/ingest", data=form)
        return resp.status, await resp.json()


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.knowledge.sel") as m:
        m.return_value = MagicMock()
        yield m


@pytest.mark.asyncio
async def test_pdf_with_non_pdf_bytes_rejected_before_parse(mock_sel):
    app, ingest_spy = _make_app()
    status, body = await _post(
        app, b"<html>not a pdf</html>\n", "evil.pdf", "application/pdf")
    assert status == 400, body
    assert "does not match its type" in body["error"]
    # Rejected before the file is handed to the extension-dispatched parser.
    ingest_spy.assert_not_called()


@pytest.mark.asyncio
async def test_docx_with_non_zip_bytes_rejected_before_parse(mock_sel):
    app, ingest_spy = _make_app()
    status, body = await _post(
        app, b"this is not a zip archive\n", "evil.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert status == 400, body
    assert "does not match its type" in body["error"]
    ingest_spy.assert_not_called()


@pytest.mark.asyncio
async def test_valid_pdf_ingests(mock_sel):
    app, _ = _make_app()
    status, body = await _post(
        app, b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n", "doc.pdf",
        "application/pdf")
    assert status == 200, body
    assert body["source_id"] == "sid1"
    assert body["status"] == "processing"


@pytest.mark.asyncio
async def test_valid_docx_ingests(mock_sel):
    app, _ = _make_app()
    status, body = await _post(
        app, _minimal_zip_bytes(), "doc.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert status == 200, body
    assert body["source_id"] == "sid1"


@pytest.mark.asyncio
async def test_text_markdown_has_no_signature_and_ingests(mock_sel):
    """Text formats have no reliable magic; arbitrary bytes under .md pass the
    gate (still bounded by the size cap + downstream text reader)."""
    app, _ = _make_app()
    status, body = await _post(
        app, b"# Title\n\nsome body text\n", "note.md", "text/markdown")
    assert status == 200, body
    assert body["source_id"] == "sid1"


@pytest.mark.asyncio
async def test_docx_zip_bomb_member_count_rejected_before_parse(mock_sel):
    """A valid-signature OOXML archive with too many members is rejected by the
    decompression-bomb guard (CWE-770) before any parser opens it."""
    app, ingest_spy = _make_app()
    data = _multi_member_zip_bytes(5)  # 6 members total, all with valid PK header
    with patch("kiro_crew.dashboard.handlers.knowledge._MAX_INGEST_ARCHIVE_MEMBERS", 2):
        status, body = await _post(
            app, data, "bomb.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert status == 400, body
    assert "archive rejected" in body["error"]
    ingest_spy.assert_not_called()


@pytest.mark.asyncio
async def test_docx_zip_bomb_uncompressed_size_rejected_before_parse(mock_sel):
    """A valid zip whose declared uncompressed total exceeds the cap is rejected
    before parsing (declared-size bomb guard)."""
    app, ingest_spy = _make_app()
    data = _minimal_zip_bytes()  # one member, >1 uncompressed byte
    with patch("kiro_crew.dashboard.handlers.knowledge._MAX_INGEST_ARCHIVE_UNCOMPRESSED", 1):
        status, body = await _post(
            app, data, "bomb.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert status == 400, body
    assert "archive rejected" in body["error"]
    ingest_spy.assert_not_called()
