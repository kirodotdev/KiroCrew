"""Tests for /api/file-download — raw byte streaming endpoint that fixes
the binary-file corruption seen when the dashboard download path went
through /api/file-read (UTF-8 decode with errors='replace').

Covers the regression case (docx round-trip preserves original bytes),
the security envelope (path validation, sensitive paths, symlinks, size),
and the text-redaction defense in depth.
"""

from __future__ import annotations

import base64
import json
import os
import random
import string
import urllib.parse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from dashboard_owner_helpers import NoConfiguredOwner, as_owner

from kiro_crew import file_delivery_consent
from kiro_crew.dashboard.handlers import api_file_download


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-download", api_file_download)
    return as_owner(app)


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m, \
         patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=False):
        instance = MagicMock()
        m.return_value = instance
        yield instance


# --- Regression: binary content survives the download round-trip ---


# Minimal docx fingerprint: ZIP header + non-UTF-8 bytes. These are exactly
# the kinds of bytes that errors='replace' would mangle into U+FFFD.
_DOCX_LIKE_BYTES = (
    b"PK\x03\x04"  # ZIP local file header magic
    + bytes(range(256))  # full 0x00-0xFF range; lots of non-UTF-8 sequences
    + b"\xef\xbf\xbd"  # an actual U+FFFD that must NOT be confused with corruption
)


@pytest.mark.asyncio
async def test_binary_bytes_survive_round_trip(tmp_path, mock_sel):
    """The regression: docx-like bytes must come back identical."""
    f = tmp_path / "doc.docx"
    f.write_bytes(_DOCX_LIKE_BYTES)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={f}")
            assert resp.status == 200
            body = await resp.read()
            assert body == _DOCX_LIKE_BYTES, "binary bytes must round-trip without UTF-8 mangling"


@pytest.mark.asyncio
async def test_sets_attachment_disposition_and_nosniff(tmp_path, mock_sel):
    f = tmp_path / "Stores Discovery.docx"
    f.write_bytes(_DOCX_LIKE_BYTES)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={f}")
            assert resp.status == 200
            disp = resp.headers["Content-Disposition"]
            assert disp.startswith("attachment;")
            # RFC 5987 percent-encoded filename for the space and the dot
            assert "filename*=UTF-8''" in disp
            assert resp.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
async def test_content_type_for_known_extensions(tmp_path, mock_sel):
    cases = {
        "doc.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "report.pdf": "application/pdf",
        "image.png": "image/png",
        "data.csv": "text/csv",
    }
    for name, expected in cases.items():
        f = tmp_path / name
        f.write_bytes(_DOCX_LIKE_BYTES if not name.endswith(".csv") else b"a,b,c\n1,2,3\n")
        with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/file-download?path={f}")
                assert resp.status == 200, f"failed for {name}"
                assert resp.headers["Content-Type"] == expected, f"wrong type for {name}"


@pytest.mark.asyncio
async def test_unknown_extension_falls_back_to_octet_stream(tmp_path, mock_sel):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\x00\x01\x02")
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={f}")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "application/octet-stream"


# --- Text files: still scanned for sensitive content (defense in depth) ---


@pytest.mark.asyncio
async def test_text_file_served_when_clean(tmp_path, mock_sel):
    f = tmp_path / "notes.txt"
    f.write_text("hello world\nno secrets here")
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={f}")
            assert resp.status == 200
            assert (await resp.read()) == b"hello world\nno secrets here"


@pytest.mark.asyncio
async def test_text_file_redacted_blocks_download(tmp_path, mock_sel):
    """If text content trips the redaction pass, the download is aborted —
    matches the api_outbox_download policy. The handler routes content through
    the context-aware redact() shim (which runs both the exfil-URL and
    credential passes, plus a loaded companion's extra regexes); this test
    forces redact() to mutate the text so the abort path is exercised.
    """
    f = tmp_path / "leaky.txt"
    f.write_text("ok body")
    redact_path = "kiro_crew.dashboard.handlers.files.redact"
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
            patch(redact_path, return_value="ok body REDACTED"):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={f}")
            assert resp.status == 400
            payload = await resp.json()
            assert "redacted" in payload["error"]
            # The scan refusal carries a machine-readable discriminator (the
            # same one file-stream/upload emit) so the client can name it a
            # CREDENTIAL refusal rather than confusing it with the endpoint's
            # other 400s (invalid input, out-of-project path).
            assert payload["code"] == "content_redacted"


# --- Security envelope ---


@pytest.mark.asyncio
async def test_invalid_path_rejected(mock_sel):
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=None):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-download?path=/etc/passwd")
            assert resp.status == 400
            # A non-scan 400 must NOT carry the content_redacted code, or the
            # client would mislabel a rejected path as a credential refusal.
            assert (await resp.json()).get("code") != "content_redacted"


@pytest.mark.asyncio
async def test_sensitive_path_rejected(tmp_path):
    f = tmp_path / "secret"
    f.write_text("x")
    # Patch is_sensitive_path on the importing module so the alias bound at
    # files.py import-time resolves to the True-returning mock.
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
         patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=True), \
         patch("kiro_crew.sel.sel") as m:
        m.return_value = MagicMock()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={f}")
            assert resp.status == 403


@pytest.mark.asyncio
async def test_missing_file_404(tmp_path, mock_sel):
    missing = tmp_path / "nope.docx"
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(missing)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={missing}")
            assert resp.status == 404


@pytest.mark.asyncio
async def test_symlink_rejected(tmp_path, mock_sel):
    """O_NOFOLLOW must reject symlinked paths atomically, like api_file_raw."""
    target = tmp_path / "real.docx"
    target.write_bytes(_DOCX_LIKE_BYTES)
    link = tmp_path / "linked.docx"
    os.symlink(target, link)
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(link)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={link}")
            assert resp.status == 403
            payload = await resp.json()
            assert "symlinks" in payload["error"]


@pytest.mark.asyncio
async def test_oversize_file_rejected(tmp_path, mock_sel):
    """Files larger than _MAX_UPLOAD_BYTES must be rejected with 413.

    The guard is the BOUNDED READ (read cap+1, refuse when over) rather than an
    fstat pre-check, so a file growing between check and read cannot outrun the
    cap. We shrink the cap instead of writing 50 MB to disk in a unit test; the
    file is genuinely over the (patched) cap, exercising the real guard."""
    f = tmp_path / "huge.docx"
    f.write_bytes(b"\x00" * 1024)  # 1 KiB on disk, over the patched 512-byte cap

    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)), \
         patch("kiro_crew.dashboard.handlers.files._MAX_UPLOAD_BYTES", 512):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={f}")
            assert resp.status == 413


# --- resolve=1 mirrors api_file_read semantics ---


@pytest.mark.asyncio
async def test_resolve_relative_path_within_project(tmp_path, mock_sel, monkeypatch):
    proj = tmp_path / "project"
    proj.mkdir()
    f = proj / "sub" / "doc.docx"
    f.parent.mkdir()
    f.write_bytes(_DOCX_LIKE_BYTES)
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
    # _validate_dashboard_path receives the resolved absolute path
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f.resolve())):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-download?path=sub/doc.docx&resolve=1")
            assert resp.status == 200
            assert (await resp.read()) == _DOCX_LIKE_BYTES


@pytest.mark.asyncio
async def test_resolve_relative_path_outside_project_rejected(tmp_path, mock_sel, monkeypatch):
    proj = tmp_path / "project"
    proj.mkdir()
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
    async with TestClient(TestServer(_make_app())) as client:
        # ../../etc/passwd would resolve outside proj
        resp = await client.get("/api/file-download?path=../../etc/passwd&resolve=1")
        assert resp.status == 400


# --- Flagged content: the owner's file-delivery grant releases it ---


def _owner_request(path, *, user: str = "local-app"):
    """A mocked GET with the claims the token middleware would install."""
    app = web.Application()
    app["state"] = NoConfiguredOwner()
    req = make_mocked_request(
        "GET", f"/api/file-download?path={urllib.parse.quote(str(path))}", app=app
    )
    req["user"] = user
    req["app"] = ""
    return req


def _key_shaped(seed: int, length: int = 40) -> str:
    """A key-shaped run, generated so no literal secret sits in the source."""
    alphabet = string.ascii_letters + string.digits + "+/"
    rng = random.Random(seed)
    return "".join(rng.choice(alphabet) for _ in range(length))


def _inline_png_html() -> bytes:
    """A self-contained HTML page with a base64 PNG, the reported file shape."""
    rng = random.Random(17404)
    pixels = b"\x89PNG\r\n\x1a\n" + bytes(rng.randrange(256) for _ in range(900))
    b64 = base64.b64encode(pixels).decode("ascii")
    return f'<html><body><img src="data:image/png;base64,{b64}"></body></html>'.encode()


def _grant() -> None:
    file_delivery_consent.record_grant(
        file_delivery_consent.CLASS_OWNER_DASHBOARD, granted_at="2026-10-07T00:00:00+00:00"
    )


@pytest.fixture(autouse=True)
def isolated_consent_store(tmp_path, _floor_monkeypatch):
    """Point the consent store at a tmp file so the host's real grant cannot leak in."""
    store = tmp_path / "consent" / "file_delivery_consent.json"
    store.parent.mkdir()
    _floor_monkeypatch.setattr(
        file_delivery_consent, "file_delivery_consent_path", lambda: store, raising=True
    )
    return store


async def _download(path, **kwargs):
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(path)):
        return await api_file_download(_owner_request(path, **kwargs))


class TestFlaggedDownloadHonoursOwnerGrant:
    """A flagged file is refused by default and released to the owner by the grant."""

    @pytest.mark.asyncio
    async def test_inline_png_html_is_refused_without_a_grant(self, tmp_path, mock_sel):
        f = tmp_path / "report.html"
        f.write_bytes(_inline_png_html())
        resp = await _download(f)
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "content_redacted"

    @pytest.mark.asyncio
    async def test_inline_png_html_is_delivered_to_the_owner_under_a_grant(
        self, tmp_path, mock_sel
    ):
        f = tmp_path / "report.html"
        raw = _inline_png_html()
        f.write_bytes(raw)
        _grant()
        with patch.object(file_delivery_consent, "audit_decision") as audit:
            resp = await _download(f)
        assert resp.status == 200
        assert resp.body == raw
        audit.assert_called_once_with(
            file_delivery_consent.CLASS_OWNER_DASHBOARD,
            outcome="delivered",
            detail="download: report.html",
        )

    @pytest.mark.asyncio
    async def test_grant_does_not_release_to_a_non_owner(self, tmp_path, mock_sel):
        """The grant's own owner conjunct refuses, even past an admitting front gate."""
        f = tmp_path / "report.html"
        f.write_bytes(_inline_png_html())
        _grant()
        with patch(
            "kiro_crew.dashboard.handlers.files.require_owner_dashboard_request",
            new=AsyncMock(return_value=None),
        ), patch.object(file_delivery_consent, "audit_refusal") as refusal:
            resp = await _download(f, user="slack-user-7")
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "content_redacted"
        assert refusal.call_args.kwargs["reason"] == "flagged content, non-owner caller"
        assert refusal.call_args.kwargs["caller"] == "slack-user-7"

    @pytest.mark.asyncio
    async def test_non_owner_is_still_stopped_at_the_front_gate(self, tmp_path, mock_sel):
        f = tmp_path / "report.html"
        f.write_bytes(_inline_png_html())
        _grant()
        resp = await _download(f, user="slack-user-7")
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_real_key_is_refused_without_a_grant(self, tmp_path, mock_sel):
        f = tmp_path / "env.txt"
        f.write_text(f"aws_secret_access_key = {_key_shaped(1)}\n")
        with patch.object(file_delivery_consent, "audit_refusal") as refusal:
            resp = await _download(f)
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "content_redacted"
        assert refusal.call_args.kwargs["reason"] == "flagged content, no grant"

    @pytest.mark.asyncio
    async def test_wide_encoded_key_is_refused_without_a_grant(self, tmp_path, mock_sel):
        """The wide pass: a UTF-16 key decodes cleanly yet matches no narrow detector."""
        f = tmp_path / "notes.txt"
        f.write_bytes(f"aws_secret_access_key = {_key_shaped(2)}".encode("utf-16-le"))
        resp = await _download(f)
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "content_redacted"

    @pytest.mark.asyncio
    async def test_clean_file_never_reads_the_grant(self, tmp_path, mock_sel):
        f = tmp_path / "notes.txt"
        f.write_text("hello world")
        with patch.object(file_delivery_consent, "is_granted") as is_granted:
            resp = await _download(f)
        assert resp.status == 200
        is_granted.assert_not_called()
