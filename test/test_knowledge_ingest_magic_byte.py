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

import asyncio
import contextlib
import io
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.knowledge import ingest_file
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.store import KnowledgeStore


class _FakeStore:
    """A store that remembers the one source the handler adds, so the
    background task's re-read under the gate finds the row it is about to
    fill (a missing row reads as deleted in the gap and skips the ingest)."""

    def __init__(self) -> None:
        self.db = MagicMock()
        self._rows: dict[str, dict] = {}

    def get_source_by_uri(self, uri):
        return self._rows.get(uri)

    def add_source(self, *, name, source_type, uri, properties):
        self._rows[uri] = {"id": "sid1"}
        return "sid1"


def _make_app() -> tuple[web.Application, AsyncMock]:
    app = web.Application()
    app["state"] = SimpleNamespace(knowledge_store=_FakeStore())
    ingest_spy = AsyncMock()
    # The handler reserves explicit-import admission before it answers, so the
    # fake pipeline has to offer that surface too. None is the budget-disabled
    # answer, which is the default and what these signature tests want.
    app["knowledge_pipeline"] = SimpleNamespace(
        ingest_file=ingest_spy,
        reserve_import_budget=AsyncMock(return_value=None),
        release_import_budget=MagicMock(),
        # The background task holds the store's ingestion gate across its
        # lookup and ingest; the fake store has no gate, so this is a no-op.
        ingestion_in_flight=contextlib.nullcontext,
    )
    app.router.add_post("/api/knowledge/ingest", ingest_file)
    return app, ingest_spy


async def _await_background_ingest(ingest_spy) -> None:
    """Wait until the upload's background task has actually called ``ingest_file``.

    A single ``asyncio.sleep(0)`` is not enough: the task stamps the row 'syncing'
    off the loop first, so reaching ``ingest_file`` costs a worker-thread hop, not
    one event-loop tick. Polling asserts what these tests are about -- what the
    ingest was called WITH -- instead of which await happens to come first.
    """
    for _ in range(200):
        if ingest_spy.await_args is not None:
            return
        await asyncio.sleep(0.01)


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


@pytest.mark.asyncio
async def test_exhausted_import_budget_refuses_with_429_before_accepting(mock_sel, tmp_path):
    """An exhausted cross-file window must refuse, not accept then discard.

    This route answers 'processing' and ingests in the background, and the staged
    temp file is the only server-side copy -- the background task's ``finally``
    unlinks it. So a refusal found after the response would throw away a file the
    client was told had been accepted. Admission is reserved before the response
    instead, which makes 429 the answer and leaves nothing staged.
    """
    from kiro_crew.knowledge.ingestion import ImportChunkBudgetError

    app, ingest_spy = _make_app()
    app["knowledge_pipeline"].reserve_import_budget = AsyncMock(
        side_effect=ImportChunkBudgetError(budget=50, window_secs=60.0, spent=60))

    before = set(p.name for p in Path(tempfile.gettempdir()).glob("kn_*"))
    status, body = await _post(app, b"# notes\n", "notes.md", "text/markdown")

    assert status == 429, body
    assert "import_chunk_budget" in body["error"]
    # Refused before acceptance: no ingest was started ...
    ingest_spy.assert_not_called()
    # ... and the staged copy was not left behind.
    assert set(p.name for p in Path(tempfile.gettempdir()).glob("kn_*")) == before


@pytest.mark.asyncio
async def test_admission_token_is_handed_to_the_background_ingest(mock_sel):
    """A granted token travels to ``ingest_file`` so admission cannot be re-taken."""
    app, ingest_spy = _make_app()
    app["knowledge_pipeline"].reserve_import_budget = AsyncMock(return_value=7)

    status, body = await _post(app, b"# notes\n", "notes.md", "text/markdown")

    assert status == 200, body
    await _await_background_ingest(ingest_spy)
    assert ingest_spy.await_args is not None, "background ingest never ran"
    assert ingest_spy.await_args.kwargs["import_budget_token"] == 7


@pytest.mark.asyncio
async def test_an_admitted_upload_never_reserves_a_second_time(mock_sel):
    """A disabled budget admits with a token of ``None``, and that is still an
    admission.

    Keying the handover on the token's value would read ``None`` as "nothing
    supplied" and reserve again inside the background task -- and a budget enabled
    between the two config reads would then refuse an upload already accepted,
    marking it errored and deleting its only staged copy. So the route declares
    ``count_toward_import_budget=False`` and the flag, not the token, is what
    decides.
    """
    app, ingest_spy = _make_app()
    app["knowledge_pipeline"].reserve_import_budget = AsyncMock(return_value=None)

    status, body = await _post(app, b"# notes\n", "notes.md", "text/markdown")

    assert status == 200, body
    await _await_background_ingest(ingest_spy)
    assert ingest_spy.await_args is not None, "background ingest never ran"
    kwargs = ingest_spy.await_args.kwargs
    assert kwargs["count_toward_import_budget"] is False, (
        "an already-admitted upload must not re-enter the budget")
    assert kwargs["import_budget_token"] is None


@pytest.mark.asyncio
async def test_the_background_task_holds_the_gate_from_its_lookup_through_the_ingest(mock_sel, tmp_path, monkeypatch):
    """A re-upload ingests into an existing row, and an itemless row with a
    terminal status is what the orphan sweep reclaims. The handler holds the
    store's ingestion gate from its lookup until the background task holds its
    own, and the task holds it across its re-read and the ingest, so a
    maintenance window cannot open at any of those points and can once the task
    is done."""
    import kiro_crew.dashboard.handlers.knowledge as kn

    store = KnowledgeStore(str(tmp_path / "k.db"))
    try:
        sid = store.add_source(name="notes.md", source_type="local_file",
                               uri="upload://notes.md", properties={})
        store.update_source(sid, sync_status="error")
        pipeline = IngestionPipeline.__new__(IngestionPipeline)
        pipeline.store = store
        seen: list[bool] = []
        real_lookup = store.get_source_by_uri
        real_row = kn._source_row

        def _lookup(uri):
            with store.maintenance_window(timeout=0.05) as quiescent:
                seen.append(quiescent)
            return real_lookup(uri)

        def _row(store_, source_id):
            with store.maintenance_window(timeout=0.05) as quiescent:
                seen.append(quiescent)
            return real_row(store_, source_id)

        async def _ingest(*_args, **_kwargs):
            with store.maintenance_window(timeout=0.05) as quiescent:
                seen.append(quiescent)
            return "job"

        store.get_source_by_uri = _lookup  # type: ignore[method-assign]
        monkeypatch.setattr(kn, "_source_row", _row)
        pipeline.ingest_file = _ingest  # type: ignore[method-assign]
        pipeline.reserve_import_budget = AsyncMock(return_value=None)  # type: ignore[method-assign]
        pipeline.release_import_budget = MagicMock()  # type: ignore[method-assign]
        app = web.Application()
        app["state"] = SimpleNamespace(knowledge_store=store)
        app["knowledge_pipeline"] = pipeline
        app.router.add_post("/api/knowledge/ingest", ingest_file)

        status, body = await _post(app, b"# notes\n", "notes.md", "text/markdown")

        assert status == 200, body
        assert body["source_id"] == sid
        await asyncio.gather(*app["_bg_tasks"])
        # The handler's own lookup (the unique insert's recovery read), the
        # task's re-read and the ingest all run under a hold: the handler holds
        # the gate from its lookup until the task holds its own.
        assert seen == [False, False, False]
        with store.maintenance_window(timeout=0.05) as quiescent:
            assert quiescent is True, "the gate stayed held after the task finished"
    finally:
        store.close()
