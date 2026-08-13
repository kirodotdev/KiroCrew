"""Tests for ``api_upload_file`` (POST /api/upload/file).

The endpoint itself was shipped earlier; what's new in is the
post-write diagnostic block that compares the bytes received in memory
against the bytes that landed on disk and logs sha256 + magic + zipfile
status. The block is wrapped in try/except so a diagnostic-internal
failure can never break an upload, and is only emitted for binary
extensions (DOC + IMAGE), not text. These tests pin both the success path
(match=True, is_zipfile=True for a real zip) and the rejection path
(unsupported extensions short-circuit before the diagnostic runs).
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.files import _write_file_restricted, api_upload_file


def _make_app() -> web.Application:
    app = web.Application()
    app["state"] = MagicMock()
    app.router.add_post("/api/upload/file", api_upload_file)
    return app


@pytest.fixture
def mock_sel():
    """Patch the late-bound ``_sel()`` in handlers.files so SEL audit
    calls in the upload handler don't blow up on a missing global."""
    with patch("kiro_crew.dashboard.handlers.files._sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``_UPLOAD_DIR`` to a per-test tmp path so uploads don't
    pollute the real ``~/.kirocrew/uploads/`` and don't race other tests."""
    target = tmp_path / "uploads"
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.files._UPLOAD_DIR",
        target,
    )
    return target


def _minimal_docx_bytes() -> bytes:
    """Produce just-enough valid ZIP bytes for ``zipfile.is_zipfile`` to
    return True. We don't need a parseable docx — the diagnostic block
    only calls ``zipfile.is_zipfile`` for the ZIP-check, never opens
    the archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<types/>")
    return buf.getvalue()


def test_write_file_restricted_preserves_binary_bytes_in_windows_text_mode(
    tmp_path: Path,
) -> None:
    """The upload writer must request ``O_BINARY`` on Windows."""
    from windows_sim import windows_text_mode_write

    destination = tmp_path / "payload.bin"
    payload = bytes(range(32))
    assert b"\n" in payload

    with windows_text_mode_write(match=destination.name) as state:
        _write_file_restricted(destination, payload)

    assert destination.read_bytes() == payload
    assert state["translated"] == 0


@pytest.mark.asyncio
async def test_upload_docx_emits_match_true_diagnostic(
    upload_dir: Path,
    caplog: pytest.LogCaptureFixture,
    mock_sel,
) -> None:
    """A normal .docx upload must log the diagnostic line with match=True
    and is_zipfile=True.

    This is the primary success path of the diagnostic block and the
    line we'd grep for in production to confirm the upload pipeline
    preserved bytes exactly. Without this assertion, a refactor that
    silently disabled the diagnostic (e.g. by widening the outer
    try/except or short-circuiting the if-block) would go unnoticed
    until the next time someone needed the log to debug a corruption
    report.
    """
    docx = _minimal_docx_bytes()
    form = aiohttp.FormData()
    form.add_field(
        "file",
        docx,
        filename="probe.docx",
        content_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
    )
    with caplog.at_level(
        logging.INFO, logger="kiro_crew.dashboard.handlers.files",
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/upload/file", data=form)
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body.get("paths"), body
    diagnostics = [
        r for r in caplog.records if "upload.file diagnostic" in r.getMessage()
    ]
    assert diagnostics, (
        "Expected an 'upload.file diagnostic' INFO line to be emitted "
        "after a .docx upload; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    msg = diagnostics[0].getMessage()
    # Sent bytes vs disk bytes must agree on the happy path. If they
    # ever don't, that's the signal that the upload pipeline corrupted
    # bytes between read and write — exactly what the diagnostic was
    # added to catch.
    assert "match=True" in msg, msg
    # ZIP magic ``50 4b 03 04`` ('PK\x03\x04'); pinned because the
    # diagnostic always logs the first 4 bytes hex-encoded and a
    # regression that swapped to a different slice would break grep
    # patterns operators rely on.
    assert "magic=504b0304" in msg, msg
    # A real zip body must report is_zipfile=True; the python literal
    # is what %s renders for a bool, not the lowercase JSON form.
    assert "is_zipfile=True" in msg, msg
    # Extension echoes in the line so parsers don't have to infer it.
    assert "ext=.docx" in msg, msg


@pytest.mark.asyncio
async def test_upload_image_emits_diagnostic_without_zip_check(
    upload_dir: Path,
    caplog: pytest.LogCaptureFixture,
    mock_sel,
) -> None:
    """PNG image upload must log the diagnostic with ``is_zipfile=None``.

    The diagnostic runs for both DOC and IMAGE extensions, but the
    is_zipfile check is gated to only the docx/xlsx/pptx/odt/zip set.
    For images, ``is_zip`` is ``None`` (Python's None renders as 'None'
    in %s formatting), and the log line still has to include match=True
    and the magic bytes so an image-corruption report can be triaged
    the same way a docx report can.
    """
    # 1x1 PNG: 8-byte signature + IHDR + IDAT + IEND. The first 8
    # bytes (89 50 4e 47 0d 0a 1a 0a) are PNG's magic; the diagnostic
    # only logs the first 4, so we'll see ``magic=89504e47``.
    png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    form = aiohttp.FormData()
    form.add_field(
        "file", png, filename="dot.png", content_type="image/png",
    )
    with caplog.at_level(
        logging.INFO, logger="kiro_crew.dashboard.handlers.files",
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/upload/file", data=form)
            assert resp.status == 200, await resp.text()
    diagnostics = [
        r for r in caplog.records if "upload.file diagnostic" in r.getMessage()
    ]
    assert diagnostics, (
        f"Expected diagnostic for image upload; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    msg = diagnostics[0].getMessage()
    assert "match=True" in msg, msg
    # ``%s`` renders Python's None as the literal 'None'; pinning this
    # protects against a refactor that switched to a string sentinel
    # (e.g. 'n/a') and silently broke production log parsers.
    assert "is_zipfile=None" in msg, msg
    assert "magic=89504e47" in msg, msg
    assert "ext=.png" in msg, msg


@pytest.mark.asyncio
async def test_upload_text_skips_diagnostic_block_entirely(
    upload_dir: Path,
    caplog: pytest.LogCaptureFixture,
    mock_sel,
) -> None:
    """A .md upload must not emit the diagnostic — the block is only
    for binary archives where any byte mismatch breaks the file.

    Text uploads write only ASCII/UTF-8, so a sha-mismatch isn't useful
    on its own (and the I/O cost of re-reading every text upload to
    re-hash isn't worth the diagnostic value). This test pins that
    contract: changing the if-block guard would break it.
    """
    form = aiohttp.FormData()
    form.add_field(
        "file",
        b"# Hello\n\nbody\n",
        filename="note.md",
        content_type="text/markdown",
    )
    with caplog.at_level(
        logging.INFO, logger="kiro_crew.dashboard.handlers.files",
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/upload/file", data=form)
            assert resp.status == 200, await resp.text()
    diagnostics = [
        r for r in caplog.records if "upload.file diagnostic" in r.getMessage()
    ]
    assert not diagnostics, (
        f"Did not expect a diagnostic for .md upload; got: "
        f"{[r.getMessage() for r in diagnostics]}"
    )


@pytest.mark.asyncio
async def test_upload_corrupted_docx_emits_zipfile_false_diagnostic(
    upload_dir: Path,
    caplog: pytest.LogCaptureFixture,
    mock_sel,
) -> None:
    """A .docx upload whose bytes aren't a valid zip must log
    is_zipfile=False with match=True.

    This is the actual failure mode the diagnostic was built to catch:
    the file arrived corrupted (or wasn't a real .docx to begin with),
    but the upload pipeline preserved bytes correctly. ``match=True``
    plus ``is_zipfile=False`` is the fingerprint that points at the
    SOURCE of the file (pre-upload corruption, wrong file masquerading
    as .docx) rather than at the upload handler. Without this test,
    the if-block could regress to skip ``zipfile.is_zipfile`` entirely
    and we'd never know the diagnostic stopped surfacing the corrupted
    case it exists to surface.
    """
    # Plausible-but-wrong .docx body: ASCII text, not a zip archive.
    # First 4 bytes are 'this' -> 74686973 hex; matches the
    # _parse_docx error-message test in test_writing_review.py for
    # consistency across the upload + parse code paths.
    bogus = b"this is not a zip archive\n"
    form = aiohttp.FormData()
    form.add_field(
        "file",
        bogus,
        filename="bogus.docx",
        content_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        # A .docx whose bytes aren't a valid zip is now REJECTED at the upload
        # boundary by the magic-byte content gate (CWE-434), before any write —
        # a bogus / masquerading file no longer reaches disk.
        assert resp.status == 400, await resp.text()
        body = await resp.json()
        assert "does not match its type" in body["error"]


@pytest.mark.asyncio
async def test_upload_har_is_accepted_as_plain_text(
    upload_dir: Path,
    caplog: pytest.LogCaptureFixture,
    mock_sel,
) -> None:
    """A ``.har`` upload is accepted exactly like ``.json`` (#2555).

    HAR exports are JSON text, so they ride the text-extension allowlist:
    no magic-byte signature to enforce, and — because HAR files routinely
    carry ``Authorization`` headers, cookies, and session tokens — the
    upload path must NOT log or echo their content. The diagnostic block
    only fires for DOC/IMAGE extensions; this test pins that a .har upload
    succeeds AND stays out of the diagnostic log.
    """
    har_body = (
        b'{"log": {"version": "1.2", "creator": {"name": "devtools"}, '
        b'"entries": []}}'
    )
    form = aiohttp.FormData()
    form.add_field(
        "file",
        har_body,
        filename="session-export.har",
        content_type="application/json",
    )
    with caplog.at_level(
        logging.INFO, logger="kiro_crew.dashboard.handlers.files",
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/upload/file", data=form)
            assert resp.status == 200, await resp.text()
            body = await resp.json()
    # The file landed in the upload dir with its (sanitized) name intact.
    assert body["paths"], body
    saved = Path(body["paths"][0])
    assert saved.name.endswith("_session-export.har")
    assert saved.read_bytes() == har_body
    # No diagnostic (and therefore no content-adjacent logging) for text.
    diagnostics = [
        r for r in caplog.records if "upload.file diagnostic" in r.getMessage()
    ]
    assert not diagnostics, (
        f"Did not expect a diagnostic for .har upload; got: "
        f"{[r.getMessage() for r in diagnostics]}"
    )


@pytest.mark.asyncio
async def test_upload_unrelated_extension_still_rejected(
    upload_dir: Path,
    mock_sel,
) -> None:
    """Adding ``.har`` must not loosen the allowlist: an unrelated
    extension (``.exe``) is still rejected with 400 before any write."""
    form = aiohttp.FormData()
    form.add_field(
        "file",
        b"MZ\x90\x00",
        filename="payload.exe",
        content_type="application/octet-stream",
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 400, await resp.text()
        body = await resp.json()
        assert "Unsupported file type" in body["error"]
    # Nothing reached disk.
    assert not upload_dir.exists() or not any(upload_dir.iterdir())
