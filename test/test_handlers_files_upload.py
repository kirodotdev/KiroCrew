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
import mimetypes
import os
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
        # A .docx whose bytes aren't a valid zip is REJECTED at the upload
        # boundary by the magic-byte content gate (CWE-434), before any write —
        # a bogus / masquerading file never reaches disk.
        assert resp.status == 400, await resp.text()
        body = await resp.json()
        assert "does not match its type" in body["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extension", "content_type"),
    [
        (".dotx", "application/vnd.openxmlformats-officedocument.wordprocessingml.template"),
        (".xltx", "application/vnd.openxmlformats-officedocument.spreadsheetml.template"),
        (".potx", "application/vnd.openxmlformats-officedocument.presentationml.template"),
    ],
)
async def test_upload_ooxml_template_is_accepted_and_zip_checked(
    upload_dir: Path,
    caplog: pytest.LogCaptureFixture,
    mock_sel,
    extension: str,
    content_type: str,
) -> None:
    """An OOXML template uploads like its base format, under the same ZIP check."""
    async with TestClient(TestServer(_make_app())) as client:
        form = aiohttp.FormData()
        form.add_field(
            "file", _minimal_docx_bytes(), filename=f"brand{extension}", content_type=content_type
        )
        with caplog.at_level(logging.INFO, logger="kiro_crew.dashboard.handlers.files"):
            resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 200, await resp.text()
        saved = Path((await resp.json())["paths"][0])
        assert mimetypes.guess_type(saved.name)[0] == content_type
        diagnostic = [m for r in caplog.records if "upload.file diagnostic" in (m := r.getMessage())]
        assert diagnostic and "is_zipfile=True" in diagnostic[0], diagnostic

        form = aiohttp.FormData()
        form.add_field(
            "file", b"<html>not a zip</html>", filename=f"fake{extension}", content_type=content_type
        )
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 400, await resp.text()
        assert "does not match its type" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_upload_har_is_accepted_as_plain_text(
    upload_dir: Path,
    caplog: pytest.LogCaptureFixture,
    mock_sel,
) -> None:
    """A ``.har`` upload is accepted exactly like ``.json``.

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
async def test_upload_drawio_is_accepted_as_xml_text(
    upload_dir: Path,
    caplog: pytest.LogCaptureFixture,
    mock_sel,
) -> None:
    """A ``.drawio`` upload is accepted exactly like ``.xml``.

    A draw.io / diagrams.net file is an XML ``mxfile`` container, so it rides
    the text-extension allowlist: no magic-byte signature to enforce, and the
    diagnostic block (DOC/IMAGE only) must not fire for it. This test keeps
    draw.io diagrams exported from the composer uploadable.
    """
    drawio_body = (
        b'<mxfile host="app.diagrams.net"><diagram name="Page-1">'
        b'<mxGraphModel><root><mxCell id="0"/></root></mxGraphModel>'
        b"</diagram></mxfile>"
    )
    form = aiohttp.FormData()
    form.add_field(
        "file",
        drawio_body,
        filename="architecture.drawio",
        content_type="application/xml",
    )
    with caplog.at_level(
        logging.INFO, logger="kiro_crew.dashboard.handlers.files",
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/upload/file", data=form)
            assert resp.status == 200, await resp.text()
            body = await resp.json()
    assert body["paths"], body
    saved = Path(body["paths"][0])
    assert saved.name.endswith("_architecture.drawio")
    assert saved.read_bytes() == drawio_body
    # Text extension: the DOC/IMAGE-only diagnostic block must stay silent.
    diagnostics = [
        r for r in caplog.records if "upload.file diagnostic" in r.getMessage()
    ]
    assert not diagnostics, (
        f"Did not expect a diagnostic for .drawio upload; got: "
        f"{[r.getMessage() for r in diagnostics]}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", [".text", ".xwiki"])
async def test_upload_plain_text_alias_is_accepted(
    upload_dir: Path,
    mock_sel,
    extension: str,
) -> None:
    payload = "Plain UTF-8 text\nUnicode: café 日本語\n".encode()
    form = aiohttp.FormData()
    form.add_field(
        "file",
        payload,
        filename=f"notes{extension}",
        content_type="text/plain",
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    saved = Path(body["paths"][0])
    assert saved.name.endswith(f"_notes{extension}")
    assert saved.read_bytes() == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("table-export.tsv", b"Files\tRecords\tCodec\n51000\t204000\tsnappy\n"),
        ("events.jsonl", b'{"event": "start"}\n{"event": "stop"}\n'),
    ],
)
async def test_upload_tsv_and_jsonl_are_accepted(
    upload_dir: Path,
    mock_sel,
    filename: str,
    payload: bytes,
) -> None:
    form = aiohttp.FormData()
    form.add_field(
        "file",
        payload,
        filename=filename,
        content_type="text/plain",
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    saved = Path(body["paths"][0])
    assert saved.name.endswith(f"_{filename}")
    assert saved.read_bytes() == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", [".rem", ".ret"])
async def test_upload_cnab_rem_ret_is_accepted_as_text(
    upload_dir: Path,
    mock_sel,
    extension: str,
) -> None:
    """CNAB remittance (``.rem``) / return (``.ret``) files upload as text.

    These Brazilian banking (FEBRABAN) interchange files are fixed-width
    ASCII/Latin-1 records with no magic-byte signature, so they ride the
    text-extension allowlist. The payload below is Latin-1 (accented names
    appear in detail segments), which the handler must accept and store
    byte-for-byte — the server never re-encodes an upload.
    """
    # A header record (type 0) plus one detail record carrying a Latin-1
    # accented name, newline-terminated. Encoded as ISO-8859-1, which is how
    # banks emit these files.
    payload = (
        "02RETORNO01COBRANCA       EMPRESA EXEMPLO LTDA\n"
        "1 JOSÉ DA CONCEIÇÃO                 000012345\n"
    ).encode("latin-1")
    form = aiohttp.FormData()
    form.add_field(
        "file",
        payload,
        filename=f"cobranca{extension}",
        content_type="application/octet-stream",
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    saved = Path(body["paths"][0])
    assert saved.name.endswith(f"_cobranca{extension}")
    # Stored byte-for-byte: the Latin-1 bytes are not re-encoded or rejected.
    assert saved.read_bytes() == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extension", "payload"),
    [
        (".php", b"<?php echo '<script>alert(1)</script>'; ?>\n"),
        (".sql", b"SELECT id, name FROM users WHERE id = 1;\n"),
        (".kt", b'fun main() { println("hi") }\n'),
        (".swift", b'print("hi")\n'),
        (".cs", b'class P { static void Main() { System.Console.WriteLine("hi"); } }\n'),
        (".vue", b"<template><div>{{ msg }}</div></template>\n<script>export default {}</script>\n"),
        (".toml", b'[package]\nname = "demo"\nversion = "0.1.0"\n'),
    ],
    ids=[".php", ".sql", ".kt", ".swift", ".cs", ".vue", ".toml"],
)
async def test_upload_source_and_config_text_is_accepted_as_text(
    upload_dir: Path,
    mock_sel,
    extension: str,
    payload: bytes,
) -> None:
    """Source and config files with no magic-byte signature upload as text.

    The ``.php`` and ``.vue`` payloads carry markup on purpose: the upload
    boundary stores the bytes exactly as sent and does not interpret them.
    """
    form = aiohttp.FormData()
    form.add_field(
        "file",
        payload,
        filename=f"sample{extension}",
        content_type="application/octet-stream",
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    saved = Path(body["paths"][0])
    assert saved.name.endswith(f"_sample{extension}")
    assert saved.read_bytes() == payload


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


_WEBP_HEAD = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8
_HEIC_HEAD = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 8


@pytest.mark.asyncio
async def test_upload_jpeg_named_webp_body_is_relabelled_not_refused(
    upload_dir: Path,
    mock_sel,
) -> None:
    """A ``.jpeg`` whose bytes are WebP is stored as ``.webp``.

    Browsers keep the URL's extension on "Save image as" while the body is
    whatever the server negotiated, so a WebP wearing ``.jpeg`` is an
    everyday file, not an attack. The bytes pass the same raster allowlist;
    only the label is corrected, and the returned path carries the true
    suffix so the ACP image inliner reads the right mime from it.
    """
    form = aiohttp.FormData()
    form.add_field("file", _WEBP_HEAD, filename="photo.jpeg", content_type="image/jpeg")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    (path,) = body["paths"]
    assert path.endswith("_photo.webp"), path
    assert Path(path).read_bytes() == _WEBP_HEAD
    assert [p.name for p in upload_dir.iterdir()] == [Path(path).name]


@pytest.mark.asyncio
async def test_upload_jpeg_with_matching_bytes_keeps_its_name(
    upload_dir: Path,
    mock_sel,
) -> None:
    """The relabel is a no-op for a truthful filename."""
    jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 8
    form = aiohttp.FormData()
    form.add_field("file", jpeg, filename="photo.jpeg", content_type="image/jpeg")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    assert body["paths"][0].endswith("_photo.jpeg")


@pytest.mark.asyncio
async def test_upload_jpeg_that_is_heic_names_the_conversion_remedy(
    upload_dir: Path,
    mock_sel,
) -> None:
    """An iPhone HEIC photo wearing ``.jpeg`` is refused with a sentence
    that says what it is and what to do, plus a machine-readable code."""
    form = aiohttp.FormData()
    form.add_field("file", _HEIC_HEAD, filename="IMG_0001.jpeg", content_type="image/jpeg")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "content_mismatch"
    assert "HEIC/AVIF" in body["error"]
    assert ".png" in body["error"] and ".jpg" in body["error"]
    assert not upload_dir.exists() or not any(upload_dir.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["IMG_0001.HEIC", "photo.heif"])
async def test_upload_unconverted_heic_names_the_reload_remedy(
    upload_dir: Path,
    mock_sel,
    filename: str,
) -> None:
    """A HEIC the browser did not convert (a page loaded before conversion
    shipped, or another client) is still refused -- no model reads HEIC --
    with the unsupported-type code and a sentence naming the way past it."""
    form = aiohttp.FormData()
    form.add_field("file", _HEIC_HEAD, filename=filename, content_type="image/heic")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "unsupported_file_type"
    assert body["error"].startswith(f"Unsupported file type: {Path(filename).suffix.lower()}.")
    assert "reload the page" in body["error"]
    assert "JPEG" in body["error"]
    assert not upload_dir.exists() or not any(upload_dir.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["deck.key", "Essay.PAGES", "budget.numbers"])
async def test_upload_iwork_document_names_the_export_remedy(
    upload_dir: Path,
    mock_sel,
    filename: str,
) -> None:
    """An Apple iWork document is still refused -- nothing here reads its
    format -- but the refusal names the export that does upload."""
    form = aiohttp.FormData()
    form.add_field("file", _minimal_docx_bytes(), filename=filename)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "unsupported_file_type"
    assert body["error"].startswith(f"Unsupported file type: {Path(filename).suffix.lower()}.")
    assert "File > Export To" in body["error"]
    assert "PowerPoint, Word, Excel or PDF" in body["error"]
    assert not upload_dir.exists() or not any(upload_dir.iterdir())


@pytest.mark.asyncio
async def test_upload_html_under_an_image_extension_is_still_refused(
    upload_dir: Path,
    mock_sel,
) -> None:
    """The CWE-434 property survives the relabel: bytes that are no raster
    at all never reach disk under an image extension."""
    form = aiohttp.FormData()
    form.add_field(
        "file", b"<html><script>1</script></html>", filename="x.png", content_type="image/png"
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "content_mismatch"
    assert "not really a .png image" in body["error"]
    assert "re-export" in body["error"]
    assert not upload_dir.exists() or not any(upload_dir.iterdir())


@pytest.mark.asyncio
async def test_upload_fits_a_file_system_with_a_short_name_limit(
    upload_dir: Path,
    mock_sel,
    monkeypatch,
) -> None:
    """On eCryptfs (NAME_MAX 143) a name under the fixed 200-byte cap still fits."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.files._name_max", lambda _directory: 143, raising=False
    )
    form = aiohttp.FormData()
    form.add_field("file", b"body\n", filename="a" * 111 + ".txt", content_type="text/plain")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 200, await resp.text()
        [stored] = (await resp.json())["paths"]

    stored_name = Path(stored).name
    assert len(stored_name.encode("utf-8")) <= 143
    assert stored_name.endswith(".txt")


@pytest.mark.asyncio
async def test_a_relabelled_upload_still_fits_a_short_name_limit(
    upload_dir: Path,
    mock_sel,
    monkeypatch,
) -> None:
    """A .jpg whose bytes are WebP is stored as .webp, one byte longer, and must still fit."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.files._name_max", lambda _directory: 143, raising=False
    )
    form = aiohttp.FormData()
    form.add_field("file", _WEBP_HEAD, filename="p" * 130 + ".jpg", content_type="image/jpeg")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 200, await resp.text()
        [stored] = (await resp.json())["paths"]

    stored_name = Path(stored).name
    assert stored_name.endswith(".webp")
    assert len(stored_name.encode("utf-8")) <= 143


@pytest.mark.asyncio
async def test_a_streamed_video_temp_name_fits_a_short_name_limit(
    upload_dir: Path,
    mock_sel,
    monkeypatch,
) -> None:
    """A streamed part is written under the stored name plus ".part"; that must fit too."""
    from kiro_crew.dashboard import part_stream

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.files._name_max", lambda _directory: 143, raising=False
    )
    temp_names: list[str] = []
    real_init = part_stream._TempSink.__init__

    def recording_init(self, dest: Path) -> None:
        real_init(self, dest)
        temp_names.append(self.tmp.name)

    monkeypatch.setattr(part_stream._TempSink, "__init__", recording_init)
    mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00" + bytes(range(256)) * 4
    form = aiohttp.FormData()
    form.add_field("file", mp4, filename="v" * 120 + ".mp4", content_type="video/mp4")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 200, await resp.text()

    [temp_name] = temp_names
    assert len(temp_name.encode("utf-8")) <= 143


def test_the_stored_name_cap_follows_the_file_system_limit() -> None:
    from kiro_crew.dashboard.file_api.uploads import _fit_stored_name

    # 33 bytes of UUID prefix before the tail, 5 for a streamed part's ".part" after it.
    assert len(_fit_stored_name("a" * 150 + ".txt", 143)) == 143 - 33 - 5
    assert _fit_stored_name("a" * 150 + ".txt", 1024) == "a" * 150 + ".txt"
    assert len(_fit_stored_name("a" * 230 + ".txt", 1024)) == 200


def test_name_max_falls_back_when_the_file_system_cannot_say(tmp_path: Path, monkeypatch) -> None:
    from kiro_crew.dashboard.file_api.uploads import _name_max

    def refuse(_path: str, _name: str) -> int:
        raise OSError("not supported")

    assert _name_max(tmp_path) > 0
    monkeypatch.setattr(os, "pathconf", refuse, raising=False)
    assert _name_max(tmp_path) == 255
    monkeypatch.delattr(os, "pathconf", raising=False)
    assert _name_max(tmp_path) == 255


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename",
    ["a" * 230 + ".txt", "文件名" * 27 + ".md"],
    ids=["ascii-230", "cjk-243-bytes"],
)
async def test_upload_with_a_name_past_the_file_system_limit_is_stored_shortened(
    upload_dir: Path,
    mock_sel,
    filename: str,
) -> None:
    """A long name is shortened to fit, keeping its extension, instead of a 500."""
    form = aiohttp.FormData()
    form.add_field("file", b"body\n", filename=filename, content_type="text/plain")
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=form)
        assert resp.status == 200, await resp.text()
        [stored] = (await resp.json())["paths"]

    stored_name = Path(stored).name
    assert len(stored_name.encode("utf-8")) <= 255
    assert stored_name.endswith(Path(filename).suffix)
    assert Path(stored).read_bytes() == b"body\n"


def test_a_short_name_is_kept_as_it_is() -> None:
    from kiro_crew.dashboard.file_api.uploads import _fit_stored_name

    assert _fit_stored_name("notes.md") == "notes.md"
    assert _fit_stored_name("é" * 150 + ".txt") == "é" * 98 + ".txt"


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("cafe\u0301.txt", "caf\u00e9.txt"),  # NFD from macOS: composed, not "cafe_"
        ("Bjo\u0308rk n\u0303.png", "Bj\u00f6rk_\u00f1.png"),  # every composable accent
        # A mark with no composed form is kept: the image-path grammar reads it.
        ("\u0939\u093f\u0902\u0926\u0940.png", "\u0939\u093f\u0902\u0926\u0940.png"),
        ("a\ufe0f\u034f\U000e0100.png", "a___.png"),  # a mark that draws nothing is not kept
        ("my report (v2).pdf", "my_report__v2_.pdf"),  # replaced exactly as before
    ],
    ids=["nfd-latin", "nfd-several", "devanagari-kept", "invisible-marks", "ascii-punctuation"],
)
def test_upload_name_composes_accents(filename: str, expected: str) -> None:
    from kiro_crew.dashboard.file_api.uploads import _safe_upload_name

    assert _safe_upload_name(filename) == expected


@pytest.mark.parametrize(
    "filename",
    ["cafe\u0301.png", "\u0939\u093f\u0902\u0926\u0940.png", "\u6587\u4ef6.jpg", "x\u0338.png"],
)
@pytest.mark.parametrize(
    ("grammar", "uploads"),
    [
        ("_POSIX_PATH_RE", "/home/u/.kiro/crew/uploads/"),
        ("_WINDOWS_PATH_RE", "C:\\Users\\u\\.kiro\\crew\\uploads\\"),
    ],
    ids=["posix", "windows"],
)
def test_a_sanitized_image_name_is_still_read_as_an_image_path(
    filename: str, grammar: str, uploads: str
) -> None:
    """Each host's path grammar must read the stored path, or the picture never reaches the model."""
    from kiro_crew import image_refs
    from kiro_crew.dashboard.file_api.uploads import _safe_upload_name

    stored = f"{uploads}{'0' * 32}_{_safe_upload_name(filename)}"

    match = getattr(image_refs, grammar).search(stored)
    assert match is not None and match.group(1) == stored, ascii(stored)


@pytest.mark.parametrize(
    "filename",
    [
        "../../etc/passwd",
        "..",
        "a\\b.txt",
        "..\\..\\x.txt",
        "x\x00y.txt",
        "x\ny.txt",
        "evil\u202etxt.exe",  # right-to-left override
        "a\u2066b\u2069.txt",  # bidi isolates
        "a\u200bb.txt",  # zero-width space
        "\uff0e\uff0e\uff0fx.txt",  # fullwidth dots and solidus
        "\u0338.txt",  # a combining overlay with no base letter
    ],
)
def test_upload_name_keeping_marks_still_drops_separators_and_controls(
    filename: str, tmp_path: Path
) -> None:
    """Keeping combining marks must not let a separator, control or bidi character through."""
    import unicodedata

    from kiro_crew.dashboard.file_api.uploads import _safe_upload_name

    out = _safe_upload_name(Path(filename).name)

    assert "/" not in out and "\\" not in out
    assert not any(unicodedata.category(ch)[0] in "CZ" for ch in out), ascii(out)
    assert unicodedata.normalize("NFC", out) == out
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    dest = upload_dir / f"{'0' * 32}_{out}"
    assert dest.resolve().parent == upload_dir.resolve()


@pytest.mark.asyncio
async def test_upload_with_a_decomposed_accent_is_stored_composed(upload_dir: Path, mock_sel) -> None:
    # Built by hand: a browser sends the name as raw UTF-8, while
    # aiohttp.FormData would percent-encode it before it reached the handler.
    body = (
        b'--b0undary\r\nContent-Disposition: form-data; name="file"; filename="'
        + "cafe\u0301.txt".encode("utf-8")
        + b'"\r\nContent-Type: text/plain\r\n\r\nbody\n\r\n--b0undary--\r\n'
    )
    headers = {"Content-Type": "multipart/form-data; boundary=b0undary"}
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/upload/file", data=body, headers=headers)
        assert resp.status == 200, await resp.text()
        [stored] = (await resp.json())["paths"]

    assert Path(stored).name.endswith("caf\u00e9.txt"), ascii(Path(stored).name)
