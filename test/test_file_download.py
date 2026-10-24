"""Tests for /api/file-download — raw byte streaming endpoint that fixes
the binary-file corruption seen when the dashboard download path went
through /api/file-read (UTF-8 decode with errors='replace').

Covers the regression case (docx round-trip preserves original bytes),
the security envelope (path validation, sensitive paths, symlinks, size),
and the text-redaction defense in depth.
"""

from __future__ import annotations

import base64
import io
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


# --- Over the scan threshold: confirm, then stream unscanned ---
#
# The threshold is shrunk instead of writing 50 MB to disk; the file is
# genuinely over the (patched) threshold, so the real branch runs.

_FILES = "kiro_crew.dashboard.handlers.files"


@pytest.fixture
def small_threshold():
    with patch(f"{_FILES}._DOWNLOAD_SCAN_MAX_BYTES", 512), \
         patch(f"{_FILES}._STREAM_CHUNK_BYTES", 100):
        yield 512


async def _get(path, query=""):
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(path)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={path}{query}")
            return resp.status, await resp.read(), resp.headers


def _sel_errors(mock_sel) -> list[str]:
    return [c.kwargs.get("error", "") for c in mock_sel.log_tool_invocation.call_args_list]


@pytest.mark.asyncio
async def test_a_large_file_asks_before_it_sends_a_byte(tmp_path, mock_sel, small_threshold):
    f = tmp_path / "huge.bin"
    f.write_bytes(b"\x00" * 1024)
    status, body, _ = await _get(f)
    assert status == 409
    assert json.loads(body) | {"error": ""} == {
        "error": "",
        "code": "large_file_unscanned",
        "size": 1024,
        "limit": 512,
    }
    assert "large_file_unconfirmed" in _sel_errors(mock_sel)


@pytest.mark.asyncio
async def test_a_confirmed_large_file_streams_whole_and_unscanned(
    tmp_path, mock_sel, small_threshold
):
    """The bytes arrive whole, no content scan runs, and SEL tags the delivery."""
    f = tmp_path / "huge.docx"
    raw = bytes(random.Random(5).randrange(256) for _ in range(2048))
    f.write_bytes(raw)
    with patch(f"{_FILES}.binary_content_is_flagged") as binary, \
         patch(f"{_FILES}.wide_content_is_flagged") as wide, \
         patch(f"{_FILES}.redact") as redact:
        status, body, headers = await _get(f, "&confirm_unscanned=1")
    assert status == 200
    assert body == raw
    assert headers["Content-Length"] == "2048"
    assert headers["Content-Disposition"].startswith("attachment;")
    assert headers["X-Content-Type-Options"] == "nosniff"
    binary.assert_not_called()
    wide.assert_not_called()
    redact.assert_not_called()
    assert "unscanned_large_file" in _sel_errors(mock_sel)


@pytest.mark.asyncio
async def test_a_large_file_is_never_read_whole(tmp_path, mock_sel, small_threshold):
    """Every read is one bounded chunk, never the file."""
    f = tmp_path / "huge.bin"
    f.write_bytes(b"x" * 2048)
    sizes: list[int] = []
    real_open = os.fdopen

    class _Spy(io.BufferedReader):
        def read(self, n=-1):
            sizes.append(n)
            return super().read(n)

    def spy_fdopen(fd, mode="r", *a, **k):
        if mode == "rb":
            return _Spy(io.FileIO(fd, "r"))
        return real_open(fd, mode, *a, **k)

    with patch("os.fdopen", spy_fdopen):
        status, body, _ = await _get(f, "&confirm_unscanned=1")
    assert status == 200
    assert body == b"x" * 2048
    assert sizes and all(0 < n <= 100 for n in sizes)


@pytest.mark.asyncio
async def test_a_growing_large_file_stops_at_its_fstat_size(
    tmp_path, mock_sel, small_threshold
):
    """Bytes written after the fstat are not sent: the length is the fstat's."""
    from kiro_crew.dashboard.handlers import files

    f = tmp_path / "huge.log"
    f.write_bytes(b"a" * 1024)
    real = files._stream_unscanned_download

    async def grow_then_stream(request, fobj, path, size, *rest):
        with open(f, "ab") as out:
            out.write(b"b" * 1024)
        return await real(request, fobj, path, size, *rest)

    with patch(f"{_FILES}._stream_unscanned_download", grow_then_stream):
        status, body, _ = await _get(f, "&confirm_unscanned=1")
    assert status == 200
    assert body == b"a" * 1024


async def _get_streamed(path):
    """GET a confirmed large download; returns (status, bytes received, ended short)."""
    import aiohttp

    received = bytearray()
    short = False
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(path)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-download?path={path}&confirm_unscanned=1")
            try:
                async for chunk in resp.content.iter_any():
                    received.extend(chunk)
            except aiohttp.ClientPayloadError:
                short = True
            return resp.status, bytes(received), short


@pytest.mark.asyncio
async def test_a_stalled_chunk_read_holds_no_default_worker_and_ends_the_stream(
    tmp_path, mock_sel, small_threshold
):
    """A read that never returns runs on the transfer pool and is cut off."""
    import threading

    f = tmp_path / "huge.bin"
    f.write_bytes(b"x" * 2048)
    release = threading.Event()
    ran_on: list[str] = []

    def hung_read(fobj, n):
        ran_on.append(threading.current_thread().name)
        release.wait()
        return b""

    try:
        with patch(f"{_FILES}._download_read_chunk", hung_read), \
             patch(f"{_FILES}._DOWNLOAD_CHUNK_STALL_SECS", 0.2):
            status, body, short = await _get_streamed(f)
    finally:
        release.set()
    assert status == 200
    assert short and body == b""
    assert ran_on and ran_on[0].startswith("mc-pathxfer")
    assert "unscanned_large_file_read_stalled" in _sel_errors(mock_sel)


@pytest.mark.asyncio
async def test_after_a_stall_the_handler_returns_and_the_close_is_deferred(
    tmp_path, mock_sel, small_threshold
):
    """The abandoned read still holds the file, so its close must not be awaited.

    The fake close blocks until released, as a close does behind a read stuck
    holding the buffer lock: a handler that awaited it would never return.
    """
    import threading

    from kiro_crew.dashboard.handlers import files

    f = tmp_path / "huge.bin"
    f.write_bytes(b"x" * 2048)
    release = threading.Event()
    closing = threading.Event()
    close_threads: list[str] = []
    real_fdopen = os.fdopen

    class _Locked(io.BufferedReader):
        def close(self):
            close_threads.append(threading.current_thread().name)
            closing.set()
            release.wait()
            super().close()

    def locked_fdopen(fd, mode="r", *a, **k):
        if mode == "rb":
            return _Locked(io.FileIO(fd, "r"))
        return real_fdopen(fd, mode, *a, **k)

    def hung_read(fobj, n):
        release.wait()
        return b""

    real_probe = files._run_path_probe
    pooled_closes: list[object] = []

    async def spy_probe(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "close":
            pooled_closes.append(fn)
        return await real_probe(fn, *args, **kwargs)

    try:
        with patch(f"{_FILES}._download_read_chunk", hung_read), \
             patch(f"{_FILES}._DOWNLOAD_CHUNK_STALL_SECS", 0.2), \
             patch(f"{_FILES}._run_path_probe", spy_probe), \
             patch("os.fdopen", locked_fdopen):
            # A handler that awaited the blocked close would never return here;
            # the per-test timeout names that hang.
            status, body, short = await _get_streamed(f)
        assert status == 200 and short and body == b""
        assert pooled_closes == []
        assert closing.wait(5)
        assert close_threads == ["file-download-close"]
    finally:
        release.set()


@pytest.mark.asyncio
async def test_a_small_read_past_its_ceiling_defers_the_close(tmp_path, mock_sel):
    """A whole read that outlives its execution ceiling still holds the file.

    The fake read never returns and the fake close blocks until released, as
    one does behind that read's buffer lock: a handler that awaited the close
    on the pool would never answer.
    """
    import threading

    from kiro_crew.dashboard.handlers import files

    f = tmp_path / "notes.txt"
    f.write_text("hello world")
    release = threading.Event()
    reading = threading.Event()
    closing = threading.Event()
    close_threads: list[str] = []
    real_fdopen = os.fdopen

    class _Hung(io.BufferedReader):
        def read(self, n=-1):
            reading.set()
            release.wait()
            return b""

        def close(self):
            close_threads.append(threading.current_thread().name)
            closing.set()
            release.wait()
            super().close()

    def hung_fdopen(fd, mode="r", *a, **k):
        if mode == "rb":
            return _Hung(io.FileIO(fd, "r"))
        return real_fdopen(fd, mode, *a, **k)

    real_probe = files._run_path_probe
    pooled_closes: list[object] = []

    async def spy_probe(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "close":
            pooled_closes.append(fn)
        return await real_probe(fn, *args, **kwargs)

    try:
        with patch(f"{_FILES}._PATH_PROBE_EXEC_CEILING_SECS", 0.2), \
             patch(f"{_FILES}._run_path_probe", spy_probe), \
             patch("os.fdopen", hung_fdopen):
            # A handler that awaited the blocked close would never return here;
            # the per-test timeout names that hang.
            status, _, _ = await _get(f)
        assert reading.is_set()
        assert status == 500
        assert pooled_closes == []
        assert closing.wait(5)
        assert close_threads == ["file-download-close"]
    finally:
        release.set()


@pytest.mark.asyncio
async def test_a_busy_transfer_pool_ends_the_stream(tmp_path, mock_sel, small_threshold):
    from kiro_crew.dashboard.handlers import files

    f = tmp_path / "huge.bin"
    f.write_bytes(b"x" * 2048)
    real_probe = files._run_path_probe

    async def busy_on_chunk(fn, *args, **kwargs):
        if fn is files._download_read_chunk:
            raise files._PathProbeBusy()
        return await real_probe(fn, *args, **kwargs)

    with patch(f"{_FILES}._run_path_probe", busy_on_chunk):
        status, body, short = await _get_streamed(f)
    assert status == 200
    assert short and body == b""
    assert "unscanned_large_file_pool_busy" in _sel_errors(mock_sel)


@pytest.mark.asyncio
async def test_a_busy_pool_closes_the_file_off_the_event_loop(tmp_path, mock_sel):
    """When the transfer pool refuses the close, it still never runs on the loop."""
    import threading

    from kiro_crew.dashboard.handlers import files

    f = tmp_path / "notes.txt"
    f.write_text("hello world")
    real_probe = files._run_path_probe
    real_fdopen = os.fdopen
    closed_on: list[threading.Thread] = []
    closed = threading.Event()

    class _Spy(io.BufferedReader):
        def close(self):
            closed_on.append(threading.current_thread())
            super().close()
            closed.set()

    def spy_fdopen(fd, mode="r", *a, **k):
        if mode == "rb":
            return _Spy(io.FileIO(fd, "r"))
        return real_fdopen(fd, mode, *a, **k)

    async def busy_on_close(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "close":
            raise files._PathProbeBusy()
        return await real_probe(fn, *args, **kwargs)

    loop_thread = threading.current_thread()
    with patch(f"{_FILES}._run_path_probe", busy_on_close), patch("os.fdopen", spy_fdopen):
        status, body, _ = await _get(f)
    assert status == 200
    assert body == b"hello world"
    assert closed.wait(5)
    assert closed_on and closed_on[0] is not loop_thread


@pytest.mark.asyncio
async def test_a_small_file_ignores_the_flag_and_is_still_scanned(
    tmp_path, mock_sel, small_threshold
):
    f = tmp_path / "env.txt"
    f.write_text(f"aws_secret_access_key = {_key_shaped(9)}\n")
    status, body, _ = await _get(f, "&confirm_unscanned=1")
    assert status == 400
    assert json.loads(body)["code"] == "content_redacted"
    assert "unscanned_large_file" not in _sel_errors(mock_sel)


@pytest.mark.asyncio
async def test_a_sensitive_path_over_the_threshold_is_refused_even_confirmed(
    tmp_path, small_threshold
):
    f = tmp_path / "huge.env"
    f.write_bytes(b"x" * 2048)
    with patch(f"{_FILES}.is_sensitive_path", return_value=True), \
         patch("kiro_crew.sel.sel") as m:
        m.return_value = MagicMock()
        status, _, _ = await _get(f, "&confirm_unscanned=1")
    assert status == 403


@pytest.mark.asyncio
async def test_a_non_owner_is_refused_even_confirmed(tmp_path, mock_sel, small_threshold):
    f = tmp_path / "huge.bin"
    f.write_bytes(b"x" * 2048)
    req = _owner_request(f, user="slack-user-7")
    req = make_mocked_request(
        "GET",
        f"/api/file-download?path={urllib.parse.quote(str(f))}&confirm_unscanned=1",
        app=req.app,
    )
    req["user"] = "slack-user-7"
    req["app"] = ""
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        resp = await api_file_download(req)
    assert resp.status == 403


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


def _telegram_shaped() -> str:
    """A bot-token-shaped run, built so no literal token sits in the source."""
    return "123456789:" + _key_shaped(3, 35).replace("+", "_").replace("/", "-")


class TestBinaryDownloadUsesTheBinaryScan:
    """Non-UTF-8 bytes go through the shared binary scan, not the text redactor."""

    @pytest.mark.asyncio
    async def test_baseline_jpeg_downloads(self, tmp_path, mock_sel):
        # Every baseline JPEG carries the standard Huffman table, whose
        # printable run reads as a bot token to the text redactor.
        image = pytest.importorskip("PIL.Image")
        f = tmp_path / "shot.jpg"
        image.new("RGB", (64, 36), (120, 30, 200)).save(f, "JPEG")
        assert b"456789:CDEFGHIJ" in f.read_bytes()
        resp = await _download(f)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_text_with_telegram_token_is_still_refused(self, tmp_path, mock_sel):
        f = tmp_path / "config.txt"
        f.write_text(f"bot_token = {_telegram_shaped()}\n")
        resp = await _download(f)
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "content_redacted"

    @pytest.mark.asyncio
    async def test_binary_with_telegram_token_is_still_refused(self, tmp_path, mock_sel):
        f = tmp_path / "blob.bin"
        f.write_bytes(b"\xff\xd8\xff\xe0\x80 " + _telegram_shaped().encode() + b" \x80")
        resp = await _download(f)
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "content_redacted"


class TestBinaryScanReadsBothProjections:
    """The shared binary scan reads latin-1 AND the replace-decoded UTF-8."""

    @staticmethod
    def _nbsp_bearer() -> bytes:
        # A stray invalid byte, then a UTF-8 no-break space after "Bearer":
        # latin-1 splits C2 A0 into two characters and breaks the match.
        return b"\xff\n" + f"Authorization: Bearer\u00a0opaque-{_key_shaped(4, 12)}\n".encode()

    def test_nbsp_bearer_is_flagged(self):
        from kiro_crew.platform import binary_content_is_flagged

        assert binary_content_is_flagged(self._nbsp_bearer())

    def test_baseline_jpeg_is_not_flagged(self):
        from kiro_crew.platform import binary_content_is_flagged

        image = pytest.importorskip("PIL.Image")
        buf = io.BytesIO()
        image.new("RGB", (64, 36), (120, 30, 200)).save(buf, "JPEG")
        assert not binary_content_is_flagged(buf.getvalue())

    @pytest.mark.asyncio
    async def test_nbsp_bearer_download_is_refused(self, tmp_path, mock_sel):
        f = tmp_path / "trace.bin"
        f.write_bytes(self._nbsp_bearer())
        resp = await _download(f)
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "content_redacted"
