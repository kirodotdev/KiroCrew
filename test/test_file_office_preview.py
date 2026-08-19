"""Tests for /api/file-office-preview — inline text extraction for .docx/.pptx.

Pins the endpoint's security envelope on the enforcing side (sensitive-path
403, unsupported-format 415 with SEL audit, resolve=1 via the shared
_resolve_project_relative helper) plus the response contract the frontend
relies on (text/truncated, no format/supported/empty fields) and the
aggregate extraction budget (a many-slide deck cannot accumulate unbounded
text — doc_parser stops at the caller's cap).
"""

from __future__ import annotations

import asyncio
import os
import zipfile
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from kiro_crew.dashboard.handlers import api_file_office_preview
from kiro_crew.dashboard.handlers.files import _OFFICE_PREVIEW_CAP


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-office-preview", api_file_office_preview)
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m, \
         patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=False):
        instance = MagicMock()
        m.return_value = instance
        yield instance


def _write_docx(path: str, paragraphs: list[str]) -> None:
    body = "\n".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)


# --- Happy path: contract the frontend relies on ---


@pytest.mark.asyncio
async def test_docx_preview_returns_text_and_truncated_only(tmp_path, mock_sel):
    f = tmp_path / "report.docx"
    _write_docx(str(f), ["Introduction", "First paragraph of the document."])
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 200
            body = await resp.json()
    assert "Introduction" in body["text"]
    assert body["truncated"] is False
    # Zero-consumer fields must NOT come back (review: dropped surface).
    assert "format" not in body
    assert "supported" not in body
    assert "empty" not in body


@pytest.mark.asyncio
async def test_truncation_flag_set_and_text_capped(tmp_path, mock_sel):
    f = tmp_path / "huge.docx"
    # One paragraph larger than the cap: extraction budget (cap + 1) keeps
    # the truncation detectable while the response text is cut to the cap.
    _write_docx(str(f), ["x" * (_OFFICE_PREVIEW_CAP + 100)])
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 200
            body = await resp.json()
    assert body["truncated"] is True
    assert len(body["text"]) == _OFFICE_PREVIEW_CAP


# --- Security envelope ---


@pytest.mark.asyncio
async def test_unsupported_extension_415_with_sel_audit(tmp_path, mock_sel):
    f = tmp_path / "legacy.xls"
    f.write_bytes(b"\xd0\xcf\x11\xe0old-ole-junk")
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 415
            body = await resp.json()
    assert body["code"] == "unsupported_preview_format"
    # The denial must leave an SEL record (review: audit gap).
    denied = [
        c for c in mock_sel.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "denied"
        and c.kwargs.get("error") == "unsupported_preview_format"
    ]
    assert denied, "unsupported-format 415 must be SEL-audited"


@pytest.mark.asyncio
async def test_sensitive_path_403(tmp_path, mock_sel):
    f = tmp_path / "secrets.docx"
    _write_docx(str(f), ["top secret"])
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
         patch(
             "kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=True,
         ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 403
            body = await resp.json()
    assert body["code"] == "sensitive_path"


@pytest.mark.asyncio
async def test_forbidden_path_400(mock_sel):
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=None):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-office-preview?path=/etc/passwd.docx")
            assert resp.status == 400
            body = await resp.json()
    assert body["code"] == "forbidden_path"


@pytest.mark.asyncio
async def test_resolve_uses_shared_helper(tmp_path, mock_sel):
    """resolve=1 goes through _resolve_project_relative (review: no inline copy)."""
    f = tmp_path / "proj" / "doc.docx"
    f.parent.mkdir()
    _write_docx(str(f), ["hello from project"])
    with patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": str(f.parent)}), \
         patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-office-preview?path=doc.docx&resolve=1")
            assert resp.status == 200
            body = await resp.json()
    assert "hello from project" in body["text"]


@pytest.mark.asyncio
async def test_resolve_outside_project_denied_and_audited(tmp_path, mock_sel):
    proj = tmp_path / "proj"
    proj.mkdir()
    with patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": str(proj)}):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/file-office-preview?path=../outside.docx&resolve=1"
            )
            assert resp.status == 400
            body = await resp.json()
    assert body["code"] == "path_outside_project"
    denied = [
        c for c in mock_sel.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "denied"
        and c.kwargs.get("error") == "outside_project"
    ]
    assert denied, "resolve denial must be SEL-audited"


@pytest.mark.asyncio
async def test_oversized_file_413_before_any_parsing(tmp_path, mock_sel):
    """The size gate runs BEFORE zipfile ever opens the archive."""
    f = tmp_path / "huge.docx"
    _write_docx(str(f), ["small real content"])
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
         patch("os.path.getsize", return_value=51 * 1024 * 1024):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 413
            body = await resp.json()
    assert body["code"] == "file_too_large"
    denied = [
        c for c in mock_sel.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "denied" and c.kwargs.get("error") == "file_too_large"
    ]
    assert denied, "oversized preview request must be SEL-audited"


@pytest.mark.asyncio
async def test_cancellation_is_sel_audited_and_reraised(tmp_path, mock_sel):
    """CancelledError during extraction records the access, then propagates."""
    f = tmp_path / "doc.docx"
    _write_docx(str(f), ["content"])
    request = make_mocked_request("GET", f"/api/file-office-preview?path={f}")
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
         patch(
             "kiro_crew.dashboard.handlers.files.asyncio.to_thread",
             side_effect=asyncio.CancelledError(),
         ):
        with pytest.raises(asyncio.CancelledError):
            await api_file_office_preview(request)
    cancelled = [
        c for c in mock_sel.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "cancelled"
    ]
    assert cancelled, "cancelled extraction must still leave an SEL record"


@pytest.mark.asyncio
async def test_redaction_runs_before_truncation(tmp_path, mock_sel):
    """A credential straddling the cap boundary must not leak as a prefix.

    Redaction must see the FULL extracted text: slicing first would cut the
    secret mid-token so the redactor no longer matches it.
    """
    f = tmp_path / "creds.docx"
    # One paragraph: filler that ends 10 chars before the cap, then a fake
    # AKIA credential ID that straddles the boundary.
    secret = "AKIAIOSFODNN7EXAMPLE"
    filler = "x" * (_OFFICE_PREVIEW_CAP - 10)
    _write_docx(str(f), [filler + secret])
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 200
            body = await resp.json()
    assert body["truncated"] is True
    # Neither the full secret nor its cap-cut prefix may appear.
    assert secret not in body["text"]
    assert secret[:10] not in body["text"]
