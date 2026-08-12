"""Coverage tests for ``kiro_crew.dashboard.handlers.files``.

The handlers in that module were largely exercised only through their happy
paths (or not at all). This file targets the endpoints and error arms the
existing suite leaves untouched:

* ``/api/file-read`` — the ``resolve=1`` project-relative branch, the
  directory-vs-missing 404 discrimination, the HEAD probe, per-extension
  content types, the 512 KB truncation flag, and the read-failure 500.
* ``/api/file-write`` — body/schema rejection, the atomic
  mkstemp-then-``os.replace`` write, and the temp-file cleanup on failure.
* ``/api/file-raw`` — every magic-byte branch of the content sniffer plus the
  size, symlink and unrecognized-format refusals.
* ``/api/file-watch`` — the SSE prelude, the first change event, and the
  post-validation symlink-swap abort.
* ``/api/outbox`` and ``/api/outbox/{filename}`` — listing filters and the
  download refusals.
* ``/api/reveal`` — the ``action=open`` arms and the no-opener fallback.
* ``/api/upload`` and ``/api/screenshot`` — the non-macOS refusal and, with a
  faked ``asyncio.create_subprocess_exec``, the success, cancel and timeout
  arms. No real process is ever spawned.
* ``/api/dashboard/config`` — the PUT field validation matrix and the
  cancellation audit arm.
* ``_content_matches_ext`` / ``_fuzzy_score`` — pure-function branches.

Every test either fakes the subprocess boundary or stays inside ``tmp_path``,
so nothing here depends on this machine having a display server, a sandbox
backend, or an ``open``/``xdg-open`` binary.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from tmpdir_helpers import short_tmp_base

from kiro_crew.dashboard.handlers import files as files_mod

# ``api_file_raw`` and ``api_file_download`` open with ``os.O_NOFOLLOW``, which
# only exists on POSIX; the whole code path is unreachable on Windows.
posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.O_NOFOLLOW does not exist on Windows, so this handler cannot run there",
)


@pytest.fixture()
def mock_sel():
    """Stub the SEL audit sink that every handler in this module writes to."""
    with patch("kiro_crew.dashboard.handlers.files._sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


def _app(method: str, route: str, handler) -> web.Application:
    app = web.Application()
    app.router.add_route(method, route, handler)
    return app


def _get_app(route: str, handler) -> web.Application:
    app = web.Application()
    app.router.add_get(route, handler)  # allow_head=True → HEAD hits the same handler
    return app


# ── /api/file-read ──


# Every test below that hands a real `tmp_path` to the file-read/write/watch
# endpoints is POSIX-only, because the PRODUCT rejects native Windows paths
# before any branch under test is reached: FILE_READ_SCHEMA / FILE_WRITE_SCHEMA
# in validation.py pin `path` to `^[~/][-\w.@~/ ]+$`, which admits neither the
# `C:` drive prefix nor a backslash separator. A Windows tmp_path like
# `C:\Users\runneradmin\AppData\Local\Temp\pytest-...` therefore 400s as
# "invalid input" no matter what the endpoint would otherwise do.
#
# This is the same defect class as deploy's `_LOCAL_DIR_RE` (reported from the
# first coverage wave): a POSIX-shaped allowlist guarding a path that reaches
# real file I/O. Widening it is security-relevant and belongs in its own
# reviewed change, NOT in a coverage PR -- so these classes are skipped on
# win32 and will start covering Windows for free once the schema is fixed.
_WINDOWS_PATH_DEFECT = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "FILE_READ/WRITE_SCHEMA reject native Windows paths (no drive letter or "
        "backslash in the allowed pattern), so every request 400s before the "
        "branch under test -- product defect, not a test defect"
    ),
)


@_WINDOWS_PATH_DEFECT
class TestFileRead:
    @staticmethod
    def _client_app() -> web.Application:
        return _get_app("/api/file-read", files_mod.api_file_read)

    @pytest.mark.asyncio
    async def test_reads_text_and_defaults_to_text_plain(self, tmp_path, mock_sel):
        f = tmp_path / "notes.txt"
        f.write_text("hello there", encoding="utf-8")
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}")
            assert resp.status == 200
            assert resp.content_type == "text/plain"
            assert await resp.text() == "hello there"

    @pytest.mark.asyncio
    async def test_content_type_per_extension(self, tmp_path, mock_sel):
        cases = {
            "a.json": "application/json",
            "a.jsonl": "application/x-ndjson",
            "a.csv": "text/csv",
            "a.md": "text/markdown",
            "a.markdown": "text/markdown",
            "a.py": "text/plain",
        }
        async with TestClient(TestServer(self._client_app())) as client:
            for name, expected in cases.items():
                f = tmp_path / name
                f.write_text("x", encoding="utf-8")
                resp = await client.get(f"/api/file-read?path={f}")
                assert resp.status == 200, name
                assert resp.content_type == expected, name

    @pytest.mark.asyncio
    async def test_truncates_at_cap_and_flags_it(self, tmp_path, mock_sel):
        f = tmp_path / "big.txt"
        f.write_text("a" * 512_001, encoding="utf-8")
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}")
            assert resp.status == 200
            assert resp.headers["X-Truncated"] == "true"
            assert len(await resp.text()) == 512_000

    @pytest.mark.asyncio
    async def test_untruncated_file_has_no_flag(self, tmp_path, mock_sel):
        f = tmp_path / "small.txt"
        f.write_text("short", encoding="utf-8")
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}")
            assert "X-Truncated" not in resp.headers

    @pytest.mark.asyncio
    async def test_head_probe_reports_file_kind(self, tmp_path, mock_sel):
        f = tmp_path / "probe.md"
        f.write_text("# hi", encoding="utf-8")
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.head(f"/api/file-read?path={f}")
            assert resp.status == 200
            assert resp.headers["X-Path-Kind"] == "file"

    @pytest.mark.asyncio
    async def test_directory_is_404_but_labelled_dir(self, tmp_path, mock_sel):
        d = tmp_path / "adir"
        d.mkdir()
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get(f"/api/file-read?path={d}")
            assert resp.status == 404
            assert resp.headers["X-Path-Kind"] == "dir"
            assert (await resp.json())["error"] == "is a directory"

    @pytest.mark.asyncio
    async def test_missing_path_is_404_labelled_missing(self, tmp_path, mock_sel):
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get(f"/api/file-read?path={tmp_path}/nope.txt")
            assert resp.status == 404
            assert resp.headers["X-Path-Kind"] == "missing"
            assert (await resp.json())["error"] == "not found"

    @pytest.mark.asyncio
    async def test_schema_violation_is_400(self, mock_sel):
        # '$' is outside FILE_READ_SCHEMA's allowed character class.
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get("/api/file-read?path=/tmp/$evil")
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid input"

    @pytest.mark.asyncio
    async def test_forbidden_path_is_400(self, tmp_path, mock_sel):
        f = tmp_path / "blocked.txt"
        f.write_text("x", encoding="utf-8")
        with patch.object(files_mod, "_validate_dashboard_path", return_value=None):
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.get(f"/api/file-read?path={f}")
                assert resp.status == 400
                assert (await resp.json())["error"] == "invalid or forbidden path"

    @pytest.mark.asyncio
    async def test_resolve_without_project_dir_is_400(self, mock_sel, monkeypatch):
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get("/api/file-read?resolve=1&path=notes.md")
            assert resp.status == 400
            assert "no project dir" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_resolve_relative_path_against_project(self, tmp_path, mock_sel, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "readme.md").write_text("# in project", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get("/api/file-read?resolve=1&path=readme.md")
            assert resp.status == 200
            assert await resp.text() == "# in project"

    @pytest.mark.asyncio
    async def test_resolve_escaping_project_is_400(self, tmp_path, mock_sel, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        (tmp_path / "outside.md").write_text("secret", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get("/api/file-read?resolve=1&path=../outside.md")
            assert resp.status == 400
            assert (await resp.json())["error"] == "path outside project directory"

    @pytest.mark.asyncio
    async def test_credentials_in_content_are_redacted(self, tmp_path, mock_sel):
        f = tmp_path / "creds.txt"
        secret = "AKIAIOSFODNN7EXAMPLE"
        f.write_text(f"aws_access_key_id={secret}\n", encoding="utf-8", newline="\n")
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get(f"/api/file-read?path={f}")
            assert resp.status == 200
            assert secret not in await resp.text()

    @pytest.mark.asyncio
    async def test_read_failure_is_500(self, tmp_path, mock_sel):
        f = tmp_path / "boom.txt"
        f.write_text("x", encoding="utf-8")
        with patch.object(files_mod, "redact", side_effect=RuntimeError("nope")):
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.get(f"/api/file-read?path={f}")
                assert resp.status == 500
                assert (await resp.json())["error"] == "failed to read file"
        assert any(
            kw.get("outcome") == "failure"
            for _, kw in mock_sel.log_tool_invocation.call_args_list
        )


# ── /api/file-write ──


@_WINDOWS_PATH_DEFECT
class TestFileWrite:
    @staticmethod
    def _client_app() -> web.Application:
        return _app("POST", "/api/file-write", files_mod.api_file_write)

    @pytest.mark.asyncio
    async def test_writes_content_atomically(self, tmp_path, mock_sel):
        f = tmp_path / "doc.md"
        f.write_text("old", encoding="utf-8")
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.post(
                "/api/file-write", json={"path": str(f), "content": "brand new"}
            )
            assert resp.status == 200
            assert await resp.json() == {"ok": True}
        assert f.read_text(encoding="utf-8") == "brand new"
        # The mkstemp scratch file must not survive a successful replace.
        assert [p.name for p in tmp_path.iterdir()] == ["doc.md"]

    @pytest.mark.asyncio
    async def test_invalid_json_body_is_400(self, mock_sel):
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.post(
                "/api/file-write", data="not json", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid JSON body"

    @pytest.mark.asyncio
    async def test_non_object_body_is_400(self, mock_sel):
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.post("/api/file-write", json=["a", "list"])
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid JSON body"

    @pytest.mark.asyncio
    async def test_schema_violation_is_400(self, mock_sel):
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.post(
                "/api/file-write", json={"path": "/tmp/$evil", "content": "x"}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid input"

    @pytest.mark.asyncio
    async def test_forbidden_path_is_400(self, tmp_path, mock_sel):
        f = tmp_path / "x.md"
        f.write_text("x", encoding="utf-8")
        with patch.object(files_mod, "_validate_dashboard_path", return_value=None):
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.post(
                    "/api/file-write", json={"path": str(f), "content": "y"}
                )
                assert resp.status == 400
                assert (await resp.json())["error"] == "invalid or forbidden path"

    @pytest.mark.asyncio
    async def test_missing_file_is_404(self, tmp_path, mock_sel):
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.post(
                "/api/file-write", json={"path": f"{tmp_path}/absent.md", "content": "y"}
            )
            assert resp.status == 404
            assert (await resp.json())["error"] == "not found"

    @pytest.mark.asyncio
    async def test_copymode_failure_does_not_abort_the_write(self, tmp_path, mock_sel):
        """``shutil.copymode`` is best-effort: an OSError must not fail the save."""
        f = tmp_path / "modes.md"
        f.write_text("old", encoding="utf-8")
        with patch.object(shutil, "copymode", side_effect=OSError("no perms")):
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.post(
                    "/api/file-write", json={"path": str(f), "content": "kept"}
                )
                assert resp.status == 200
        assert f.read_text(encoding="utf-8") == "kept"

    @pytest.mark.asyncio
    async def test_replace_failure_is_500_and_cleans_up_temp(self, tmp_path, mock_sel):
        f = tmp_path / "doomed.md"
        f.write_text("original", encoding="utf-8")
        with patch.object(os, "replace", side_effect=OSError("replace failed")):
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.post(
                    "/api/file-write", json={"path": str(f), "content": "never lands"}
                )
                assert resp.status == 500
                assert (await resp.json())["error"] == "failed to write file"
        assert f.read_text(encoding="utf-8") == "original"
        assert [p.name for p in tmp_path.iterdir()] == ["doomed.md"]

    @pytest.mark.asyncio
    async def test_temp_unlink_failure_still_reports_500(self, tmp_path, mock_sel):
        """The cleanup ``os.unlink`` is itself wrapped: its OSError is swallowed
        so the caller still gets the real 500 rather than an unhandled error."""
        f = tmp_path / "twice.md"
        f.write_text("original", encoding="utf-8")
        with patch.object(os, "replace", side_effect=OSError("replace failed")), \
             patch.object(os, "unlink", side_effect=OSError("unlink failed")):
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.post(
                    "/api/file-write", json={"path": str(f), "content": "nope"}
                )
                assert resp.status == 500
        # The scratch file survives here precisely because unlink was blocked.
        leftovers = [p for p in tmp_path.iterdir() if p.name != "twice.md"]
        for p in leftovers:
            p.unlink()


# ── /api/file-raw ──


@posix_only
class TestFileRaw:
    @staticmethod
    def _client_app() -> web.Application:
        return _get_app("/api/file-raw", files_mod.api_file_raw)

    @pytest.mark.asyncio
    async def test_image_magic_bytes_drive_content_type(self, tmp_path, mock_sel):
        cases = {
            "a.png": (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "image/png"),
            "a.jpg": (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
            "a87.gif": (b"GIF87a" + b"\x00" * 8, "image/gif"),
            "a89.gif": (b"GIF89a" + b"\x00" * 8, "image/gif"),
            "a.bmp": (b"BM" + b"\x00" * 12, "image/bmp"),
            "le.tiff": (b"II\x2a\x00" + b"\x00" * 8, "image/tiff"),
            "be.tiff": (b"MM\x00\x2a" + b"\x00" * 8, "image/tiff"),
            "a.ico": (b"\x00\x00\x01\x00" + b"\x00" * 8, "image/x-icon"),
            "a.webp": (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 4, "image/webp"),
            "a.pdf": (b"%PDF-1.7\n" + b"\x00" * 4, "application/pdf"),
        }
        async with TestClient(TestServer(self._client_app())) as client:
            for name, (payload, expected) in cases.items():
                f = tmp_path / name
                f.write_bytes(payload)
                resp = await client.get(f"/api/file-raw?path={f}")
                assert resp.status == 200, name
                assert resp.headers["Content-Type"] == expected, name
                assert resp.headers["X-Content-Type-Options"] == "nosniff"
                assert await resp.read() == payload, name

    @pytest.mark.asyncio
    async def test_bare_svg_gets_script_blocking_csp(self, tmp_path, mock_sel):
        f = tmp_path / "icon.svg"
        f.write_bytes(b"\xef\xbb\xbf  <svg xmlns='http://www.w3.org/2000/svg'/>")
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get(f"/api/file-raw?path={f}")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "image/svg+xml"
            assert resp.headers["Content-Security-Policy"] == (
                "script-src 'none'; style-src 'unsafe-inline'"
            )

    @pytest.mark.asyncio
    async def test_xml_prologue_svg_recognized(self, tmp_path, mock_sel):
        f = tmp_path / "declared.svg"
        f.write_bytes(b"<?xml version='1.0'?>\n<svg width='1' height='1'></svg>")
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get(f"/api/file-raw?path={f}")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "image/svg+xml"

    @pytest.mark.asyncio
    async def test_unrecognized_content_is_403(self, tmp_path, mock_sel):
        f = tmp_path / "plain.png"  # lying extension; content decides
        f.write_bytes(b"just some text, not an image at all")
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get(f"/api/file-raw?path={f}")
            assert resp.status == 403
            assert "not a recognized format" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_forbidden_path_is_400(self, mock_sel):
        with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=None):
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.get("/api/file-raw?path=/tmp/whatever.png")
                assert resp.status == 400

    @pytest.mark.asyncio
    async def test_sensitive_path_is_403(self, tmp_path, mock_sel):
        f = tmp_path / "s.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        with patch("kiro_crew.security.is_sensitive_path", return_value=True):
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.get(f"/api/file-raw?path={f}")
                assert resp.status == 403
                assert (await resp.json())["error"] == "sensitive path blocked"

    @pytest.mark.asyncio
    async def test_missing_path_is_404(self, tmp_path, mock_sel):
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get(f"/api/file-raw?path={tmp_path}/gone.png")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_oversize_file_is_413(self, tmp_path, mock_sel, monkeypatch):
        f = tmp_path / "big.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        monkeypatch.setattr(files_mod, "_MAX_UPLOAD_BYTES", 8)
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get(f"/api/file-raw?path={f}")
            assert resp.status == 413
            assert (await resp.json())["error"] == "file too large"

    @pytest.mark.asyncio
    async def test_symlink_is_refused_by_o_nofollow(self, tmp_path, mock_sel):
        target = tmp_path / "real.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n")
        link = tmp_path / "link.png"
        os.symlink(target, link)
        # Hand the handler the link itself: the real validator would have
        # realpath'd it away, and O_NOFOLLOW is what must catch it.
        with patch(
            "kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(link)
        ):
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.get(f"/api/file-raw?path={link}")
                assert resp.status == 403
                assert (await resp.json())["error"] == "symlinks not allowed"


# ── /api/file-watch ──


@_WINDOWS_PATH_DEFECT
class TestFileWatch:
    @staticmethod
    def _client_app() -> web.Application:
        return _get_app("/api/file-watch", files_mod.api_file_watch)

    @pytest.mark.asyncio
    async def test_schema_violation_is_400(self, mock_sel):
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get("/api/file-watch?path=/tmp/$evil")
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid input"

    @pytest.mark.asyncio
    async def test_forbidden_path_is_400(self, tmp_path, mock_sel):
        f = tmp_path / "w.txt"
        f.write_text("x", encoding="utf-8")
        with patch.object(files_mod, "_validate_dashboard_path", return_value=None):
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.get(f"/api/file-watch?path={f}")
                assert resp.status == 400
                assert (await resp.json())["error"] == "invalid or forbidden path"

    @pytest.mark.asyncio
    async def test_missing_path_is_404(self, tmp_path, mock_sel):
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get(f"/api/file-watch?path={tmp_path}/absent.txt")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_streams_first_change_event(self, tmp_path, mock_sel):
        f = tmp_path / "live.md"
        f.write_text("first revision\n", encoding="utf-8", newline="\n")
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get(f"/api/file-watch?path={f}")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            assert resp.headers["Cache-Control"] == "no-cache"
            chunk = await asyncio.wait_for(resp.content.readuntil(b"\n\n"), timeout=10)
            payload = json.loads(chunk.decode("utf-8").split("data: ", 1)[1].strip())
            assert payload["content"] == "first revision\n"
            assert payload["mtime"] > 0
            resp.close()

    @pytest.mark.asyncio
    async def test_symlink_swapped_after_validation_aborts_stream(self, tmp_path, mock_sel):
        """The watcher re-resolves the path on every change and bails if the
        realpath moved, so a post-validation symlink swap cannot be used to
        stream a different file's contents."""
        f = tmp_path / "swapped.md"
        f.write_text("content\n", encoding="utf-8", newline="\n")
        target = str(f)
        real_realpath = os.path.realpath
        seen: list[str] = []

        def fake_realpath(p, *a, **kw):
            if p == target:
                seen.append(p)
                # First resolution (the baseline) is honest; the second reports a
                # different destination, i.e. the link was repointed.
                return target if len(seen) == 1 else target + ".elsewhere"
            return real_realpath(p, *a, **kw)

        with patch.object(files_mod, "_validate_dashboard_path", return_value=target), \
             patch.object(os.path, "realpath", fake_realpath):
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.get(f"/api/file-watch?path={f}")
                assert resp.status == 200
                body = await asyncio.wait_for(resp.content.read(), timeout=10)
                # Stream ends without ever emitting the file's contents.
                assert body == b""
        assert len(seen) >= 2, "the watcher never re-resolved the path"
        assert any(
            kw.get("tool_name") == "file_watch" and kw.get("outcome") == "denied"
            for _, kw in mock_sel.log_tool_invocation.call_args_list
        )


# ── /api/outbox ──


@pytest.fixture()
def outbox(tmp_path):
    """A short, low-entropy outbox dir.

    ``tmp_path`` is not usable here: on macOS it carries high-entropy directory
    ids that trip ``redact_credentials()`` on the echoed path, so the handlers
    reject the fixture rather than the code under test. Same reasoning as
    ``test_outbox_binary.py``.
    """
    base = Path(tempfile.mkdtemp(dir=short_tmp_base()))
    odir = base / "outbox"
    odir.mkdir()
    with patch("kiro_crew.config.loader.outbox_dir", return_value=odir):
        try:
            yield odir
        finally:
            shutil.rmtree(base, ignore_errors=True)


class TestOutboxList:
    @staticmethod
    def _client_app() -> web.Application:
        return _get_app("/api/outbox", files_mod.api_outbox_list)

    @pytest.mark.asyncio
    async def test_absent_outbox_returns_empty_list(self, outbox, mock_sel):
        outbox.rmdir()
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get("/api/outbox")
            assert resp.status == 200
            assert await resp.json() == {"files": []}

    @pytest.mark.asyncio
    async def test_lists_files_newest_first_and_skips_dirs(self, outbox, mock_sel):
        (outbox / "old.txt").write_text("old", encoding="utf-8")
        (outbox / "new.txt").write_text("new", encoding="utf-8")
        (outbox / "a_subdir").mkdir()
        os.utime(outbox / "old.txt", (1_600_000_000, 1_600_000_000))
        os.utime(outbox / "new.txt", (1_700_000_000, 1_700_000_000))
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get("/api/outbox")
            files = (await resp.json())["files"]
        assert [f["filename"] for f in files] == ["new.txt", "old.txt"]
        assert files[0]["size"] == len("new")
        assert files[0]["modified"] == 1_700_000_000

    @pytest.mark.asyncio
    async def test_filename_that_looks_like_a_credential_is_hidden(self, outbox, mock_sel):
        # A GitHub PAT-shaped name is rewritten by redact(), so the entry is
        # dropped rather than advertised over the API.
        secretish = "ghp_" + "a" * 36
        (outbox / secretish).write_text("x", encoding="utf-8")
        (outbox / "ordinary.txt").write_text("x", encoding="utf-8")
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.get("/api/outbox")
            names = {f["filename"] for f in (await resp.json())["files"]}
        assert names == {"ordinary.txt"}

    @pytest.mark.asyncio
    async def test_entry_removed_mid_scan_is_skipped(self, outbox, mock_sel):
        (outbox / "racey.txt").write_text("x", encoding="utf-8")
        (outbox / "stable.txt").write_text("x", encoding="utf-8")
        real_stat = Path.stat

        def flaky_stat(self, *a, **kw):
            if self.name == "racey.txt":
                raise FileNotFoundError(self.name)
            return real_stat(self, *a, **kw)

        with patch.object(Path, "stat", flaky_stat):
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.get("/api/outbox")
                names = {f["filename"] for f in (await resp.json())["files"]}
        assert names == {"stable.txt"}


class TestOutboxDownload:
    """Driven by calling the handler directly: the refusals under test are keyed
    off ``match_info``, and routing a literal ``..`` through the client would be
    normalized away by the URL layer before the handler ever sees it."""

    @staticmethod
    def _request(filename: str):
        req = MagicMock()
        req.match_info = {"filename": filename}
        return req

    @pytest.mark.asyncio
    async def test_traversal_out_of_outbox_is_403(self, outbox, mock_sel):
        (outbox.parent / "escape.txt").write_text("secret", encoding="utf-8")
        resp = await files_mod.api_outbox_download(self._request("../escape.txt"))
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "forbidden"

    @pytest.mark.asyncio
    async def test_oversize_file_is_413(self, outbox, mock_sel):
        from kiro_crew.hooks import FileTooLargeError

        (outbox / "huge.txt").write_text("x", encoding="utf-8")
        with patch(
            "kiro_crew.hooks.safe_read_file_bytes",
            side_effect=FileTooLargeError("file exceeds 50 MB"),
        ):
            resp = await files_mod.api_outbox_download(self._request("huge.txt"))
        assert resp.status == 413
        assert "50 MB" in json.loads(resp.body)["error"]

    @pytest.mark.asyncio
    async def test_unreadable_file_is_403(self, outbox, mock_sel):
        (outbox / "denied.txt").write_text("x", encoding="utf-8")
        with patch("kiro_crew.hooks.safe_read_file_bytes", return_value=None):
            resp = await files_mod.api_outbox_download(self._request("denied.txt"))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_credential_bearing_text_aborts_download(self, outbox, mock_sel):
        (outbox / "leak.txt").write_text("ghp_" + "b" * 36, encoding="utf-8")
        resp = await files_mod.api_outbox_download(self._request("leak.txt"))
        assert resp.status == 400
        assert "redacted" in json.loads(resp.body)["error"]

    @pytest.mark.asyncio
    async def test_text_is_served_as_attachment(self, outbox, mock_sel):
        (outbox / "report.md").write_text("# clean report\n", encoding="utf-8", newline="\n")
        resp = await files_mod.api_outbox_download(self._request("report.md"))
        assert resp.status == 200
        assert resp.headers["Content-Disposition"].startswith("attachment;")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.body == b"# clean report\n"

    @pytest.mark.asyncio
    async def test_svg_is_never_served_inline(self, outbox, mock_sel):
        (outbox / "logo.svg").write_text("<svg/>", encoding="utf-8")
        resp = await files_mod.api_outbox_download(self._request("logo.svg"))
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/svg+xml"
        assert resp.headers["Content-Disposition"].startswith("attachment;")


# ── /api/reveal ──


class TestRevealPath:
    @staticmethod
    def _client_app() -> web.Application:
        return _app("POST", "/api/reveal", files_mod.api_reveal_path)

    @pytest.mark.asyncio
    async def test_invalid_json_body_is_400(self, mock_sel):
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.post(
                "/api/reveal", data="{", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid JSON body"

    @pytest.mark.asyncio
    async def test_traversal_in_path_is_400(self, mock_sel):
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.post("/api/reveal", json={"path": "/tmp/../etc/hosts"})
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid path"

    @pytest.mark.asyncio
    async def test_open_action_rejects_non_regular_file(self, tmp_path, mock_sel):
        d = tmp_path / "adir"
        d.mkdir()
        async with TestClient(TestServer(self._client_app())) as client:
            resp = await client.post("/api/reveal", json={"path": str(d), "action": "open"})
            assert resp.status == 400
            assert (await resp.json())["error"] == "not a regular file"

    @pytest.mark.asyncio
    async def test_open_action_on_macos_uses_open(self, tmp_path, mock_sel):
        f = tmp_path / "doc.pdf"
        f.write_text("x", encoding="utf-8")
        with patch("sys.platform", "darwin"), patch("subprocess.Popen") as popen:
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.post(
                    "/api/reveal", json={"path": str(f), "action": "open"}
                )
                assert resp.status == 200
        popen.assert_called_once_with(["open", str(f)])

    @pytest.mark.asyncio
    async def test_open_action_on_linux_uses_xdg_open(self, tmp_path, mock_sel):
        f = tmp_path / "doc.pdf"
        f.write_text("x", encoding="utf-8")
        with patch("sys.platform", "linux"), \
             patch("shutil.which", return_value="/usr/bin/xdg-open"), \
             patch("subprocess.Popen") as popen:
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.post(
                    "/api/reveal", json={"path": str(f), "action": "open"}
                )
                assert resp.status == 200
        popen.assert_called_once_with(["xdg-open", str(f)])

    @pytest.mark.asyncio
    async def test_open_action_without_opener_returns_copy_path(self, tmp_path, mock_sel):
        f = tmp_path / "doc.pdf"
        f.write_text("x", encoding="utf-8")
        with patch("sys.platform", "linux"), patch("shutil.which", return_value=None):
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.post(
                    "/api/reveal", json={"path": str(f), "action": "open"}
                )
                assert resp.status == 200
                assert await resp.json() == {"ok": True, "copy": str(f)}

    @pytest.mark.asyncio
    async def test_reveal_on_macos_uses_dash_r(self, tmp_path, mock_sel):
        f = tmp_path / "doc.pdf"
        f.write_text("x", encoding="utf-8")
        with patch("sys.platform", "darwin"), patch("subprocess.Popen") as popen:
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.post("/api/reveal", json={"path": str(f)})
                assert resp.status == 200
        popen.assert_called_once_with(["open", "-R", str(f)])

    @pytest.mark.asyncio
    async def test_reveal_on_linux_opens_parent_dir(self, tmp_path, mock_sel):
        f = tmp_path / "doc.pdf"
        f.write_text("x", encoding="utf-8")
        with patch("sys.platform", "linux"), \
             patch("shutil.which", return_value="/usr/bin/xdg-open"), \
             patch("subprocess.Popen") as popen:
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.post("/api/reveal", json={"path": str(f)})
                assert resp.status == 200
        popen.assert_called_once_with(["xdg-open", str(tmp_path)])

    @pytest.mark.asyncio
    async def test_reveal_without_opener_returns_copy_path(self, tmp_path, mock_sel):
        f = tmp_path / "doc.pdf"
        f.write_text("x", encoding="utf-8")
        with patch("sys.platform", "linux"), patch("shutil.which", return_value=None):
            async with TestClient(TestServer(self._client_app())) as client:
                resp = await client.post("/api/reveal", json={"path": str(f)})
                assert resp.status == 200
                assert await resp.json() == {"ok": True, "copy": str(f)}


# ── /api/upload and /api/screenshot (native pickers) ──


class _FakeProc:
    """Stand-in for ``asyncio.subprocess.Process``.

    No process is spawned. ``fail_first`` makes the first await raise
    ``TimeoutError`` so the handler's ``asyncio.wait_for`` arm is reached without
    a real 120-second wait; ``kill_raises`` drives the ``ProcessLookupError``
    branch of the cleanup.
    """

    def __init__(self, stdout: bytes = b"", *, fail_first: bool = False,
                 kill_raises: bool = False) -> None:
        self._stdout = stdout
        self._fail_first = fail_first
        self._kill_raises = kill_raises
        self.killed = False
        self.awaits = 0

    async def communicate(self):
        self.awaits += 1
        if self._fail_first and self.awaits == 1:
            raise asyncio.TimeoutError
        return self._stdout, b""

    async def wait(self):
        self.awaits += 1
        if self._fail_first and self.awaits == 1:
            raise asyncio.TimeoutError
        return 0

    def kill(self):
        self.killed = True
        if self._kill_raises:
            raise ProcessLookupError


class TestNativePickers:
    @pytest.mark.asyncio
    async def test_upload_is_refused_off_macos(self, mock_sel):
        with patch("sys.platform", "linux"):
            async with TestClient(
                TestServer(_app("POST", "/api/upload", files_mod.api_upload))
            ) as client:
                resp = await client.post("/api/upload")
                assert resp.status == 400
                assert "only available on macOS" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_upload_returns_selected_paths(self, mock_sel):
        proc = _FakeProc(stdout=b"/Users/x/a.png\n\n/Users/x/b.txt\n")
        with patch("sys.platform", "darwin"), \
             patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as spawn:
            async with TestClient(
                TestServer(_app("POST", "/api/upload", files_mod.api_upload))
            ) as client:
                resp = await client.post("/api/upload")
                assert resp.status == 200
                assert (await resp.json())["paths"] == ["/Users/x/a.png", "/Users/x/b.txt"]
        assert spawn.await_args.args[0] == "osascript"

    @pytest.mark.asyncio
    async def test_upload_cancelled_dialog_returns_no_paths(self, mock_sel):
        proc = _FakeProc(stdout=b"\n  \n")
        with patch("sys.platform", "darwin"), \
             patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            async with TestClient(
                TestServer(_app("POST", "/api/upload", files_mod.api_upload))
            ) as client:
                resp = await client.post("/api/upload")
                assert await resp.json() == {"paths": []}

    @pytest.mark.asyncio
    async def test_upload_timeout_kills_dialog_and_returns_504(self, mock_sel):
        proc = _FakeProc(fail_first=True, kill_raises=True)
        with patch("sys.platform", "darwin"), \
             patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            async with TestClient(
                TestServer(_app("POST", "/api/upload", files_mod.api_upload))
            ) as client:
                resp = await client.post("/api/upload")
                assert resp.status == 504
                assert (await resp.json())["error"] == "Finder dialog timed out"
        assert proc.killed

    @pytest.mark.asyncio
    async def test_screenshot_is_refused_off_macos(self, mock_sel):
        with patch("sys.platform", "linux"):
            async with TestClient(
                TestServer(_app("POST", "/api/screenshot", files_mod.api_screenshot))
            ) as client:
                resp = await client.post("/api/screenshot")
                assert resp.status == 400
                assert "only available on macOS" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_screenshot_returns_captured_path(self, tmp_path, mock_sel, monkeypatch):
        shots = tmp_path / "screenshots"
        monkeypatch.setattr(files_mod, "_SCREENSHOT_DIR", shots)
        captured: dict[str, tuple] = {}

        async def fake_exec(*argv, **kwargs):
            captured["argv"] = argv
            # screencapture writes the file itself; mimic that side effect.
            Path(argv[2]).write_bytes(b"\x89PNG\r\n\x1a\n")
            return _FakeProc()

        with patch("sys.platform", "darwin"), \
             patch("asyncio.create_subprocess_exec", fake_exec):
            async with TestClient(
                TestServer(_app("POST", "/api/screenshot", files_mod.api_screenshot))
            ) as client:
                resp = await client.post("/api/screenshot")
                assert resp.status == 200
                path = (await resp.json())["path"]
        assert captured["argv"][:2] == ("screencapture", "-i")
        assert Path(path).parent == shots
        assert Path(path).read_bytes().startswith(b"\x89PNG")

    @pytest.mark.asyncio
    async def test_screenshot_user_cancel_returns_empty_path(self, tmp_path, mock_sel,
                                                             monkeypatch):
        monkeypatch.setattr(files_mod, "_SCREENSHOT_DIR", tmp_path / "shots")
        with patch("sys.platform", "darwin"), \
             patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_FakeProc())):
            async with TestClient(
                TestServer(_app("POST", "/api/screenshot", files_mod.api_screenshot))
            ) as client:
                resp = await client.post("/api/screenshot")
                # No file was written, so the user dismissed the crosshair.
                assert await resp.json() == {"path": ""}

    @pytest.mark.asyncio
    async def test_screenshot_timeout_returns_504(self, tmp_path, mock_sel, monkeypatch):
        monkeypatch.setattr(files_mod, "_SCREENSHOT_DIR", tmp_path / "shots")
        proc = _FakeProc(fail_first=True)
        with patch("sys.platform", "darwin"), \
             patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            async with TestClient(
                TestServer(_app("POST", "/api/screenshot", files_mod.api_screenshot))
            ) as client:
                resp = await client.post("/api/screenshot")
                assert resp.status == 504
                assert (await resp.json())["error"] == "screenshot timed out"
        assert proc.killed


# ── /api/dashboard/config ──


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


@pytest.fixture()
def config_client_app(cfg_file, mock_sel) -> web.Application:
    app = web.Application()
    app.router.add_get("/api/dashboard/config", files_mod.api_dashboard_config)
    app.router.add_put("/api/dashboard/config", files_mod.api_dashboard_config)
    return app


class TestDashboardConfigPut:
    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, config_client_app):
        async with TestClient(TestServer(config_client_app)) as client:
            resp = await client.put(
                "/api/dashboard/config",
                data="{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_non_object_body_is_400(self, config_client_app):
        async with TestClient(TestServer(config_client_app)) as client:
            resp = await client.put("/api/dashboard/config", json=[1, 2, 3])
            assert resp.status == 400
            assert "must be a JSON object" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_unknown_field_is_rejected(self, config_client_app):
        async with TestClient(TestServer(config_client_app)) as client:
            resp = await client.put("/api/dashboard/config", json={"nope": 1})
            assert resp.status == 400
            assert "Unknown fields" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_deprecated_and_read_only_keys_are_dropped_not_rejected(
        self, config_client_app
    ):
        """The settings UI PUTs back everything the GET returned, so a removed
        key and a read-only key must be ignored rather than 400 the whole save."""
        async with TestClient(TestServer(config_client_app)) as client:
            resp = await client.put(
                "/api/dashboard/config",
                json={
                    "tail_fork_head_handling": "whatever",
                    "gitlab_hosts": ["gitlab.example.com"],
                    "jira_hosts": ["jira.example.com"],
                    "session_grid": True,
                },
            )
            assert resp.status == 200
            got = await (await client.get("/api/dashboard/config")).json()
        assert got["session_grid"] is True
        # Read-only fields were not persisted from the PUT body.
        assert got["gitlab_hosts"] == []
        assert got["jira_hosts"] == []

    @pytest.mark.asyncio
    async def test_boolean_fields_reject_non_booleans(self, config_client_app):
        bool_fields = [
            "restore_sessions",
            "merge_queued_messages",
            "tail_fork_enabled",
            "folder_suggestions_enabled",
            "link_previews",
            "quick_send",
            "session_grid",
            "mcp_app_panel",
        ]
        async with TestClient(TestServer(config_client_app)) as client:
            for field in bool_fields:
                resp = await client.put("/api/dashboard/config", json={field: "yes"})
                assert resp.status == 400, field
                assert field in (await resp.json())["error"], field

    @pytest.mark.asyncio
    async def test_coded_boolean_errors_carry_a_machine_code(self, config_client_app):
        coded = {
            "folder_suggestions_enabled": "invalid_folder_suggestions_enabled",
            "link_previews": "invalid_link_previews",
            "mcp_app_panel": "invalid_mcp_app_panel",
        }
        async with TestClient(TestServer(config_client_app)) as client:
            for field, code in coded.items():
                resp = await client.put("/api/dashboard/config", json={field: 1})
                assert (await resp.json())["code"] == code, field

    @pytest.mark.asyncio
    async def test_restore_window_minutes_is_clamped(self, config_client_app):
        async with TestClient(TestServer(config_client_app)) as client:
            resp = await client.put(
                "/api/dashboard/config", json={"restore_window_minutes": 99_999}
            )
            assert resp.status == 200
            assert (await (await client.get("/api/dashboard/config")).json())[
                "restore_window_minutes"
            ] == 1440

            resp = await client.put(
                "/api/dashboard/config", json={"restore_window_minutes": -5}
            )
            assert resp.status == 200
            assert (await (await client.get("/api/dashboard/config")).json())[
                "restore_window_minutes"
            ] == 0

    @pytest.mark.asyncio
    async def test_restore_window_minutes_rejects_non_integer(self, config_client_app):
        async with TestClient(TestServer(config_client_app)) as client:
            resp = await client.put(
                "/api/dashboard/config", json={"restore_window_minutes": "soon"}
            )
            assert resp.status == 400
            assert "must be an integer" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_enum_fields_reject_unknown_values(self, config_client_app):
        async with TestClient(TestServer(config_client_app)) as client:
            resp = await client.put("/api/dashboard/config", json={"widget_density": "medium"})
            assert resp.status == 400
            assert "widget_density" in (await resp.json())["error"]

            resp = await client.put("/api/dashboard/config", json={"verbosity": "loud"})
            assert resp.status == 400
            assert "verbosity" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_full_valid_put_round_trips_through_get(self, config_client_app):
        payload = {
            "restore_sessions": True,
            "restore_window_minutes": 30,
            "merge_queued_messages": True,
            "widget_density": "less",
            "verbosity": "ultra",
            "quick_send": True,
            "session_grid": True,
            "mcp_app_panel": True,
            "tail_fork_enabled": True,
            "link_previews": True,
            "folder_suggestions_enabled": True,
        }
        async with TestClient(TestServer(config_client_app)) as client:
            resp = await client.put("/api/dashboard/config", json=payload)
            assert resp.status == 200
            got = await (await client.get("/api/dashboard/config")).json()
        for key, want in payload.items():
            assert got[key] == want, key

    @pytest.mark.asyncio
    async def test_cancellation_mid_load_is_still_audited(self, mock_sel):
        """A client disconnect while the config load is off-loop must not leave
        the authorized access absent from the SEL chain."""
        for method, tool in (("PUT", "dashboard_config_write"),
                             ("GET", "dashboard_config_read")):
            mock_sel.reset_mock()
            req = MagicMock()
            req.method = method
            with patch("asyncio.to_thread", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await files_mod.api_dashboard_config(req)
            mock_sel.log_tool_invocation.assert_called_once_with(
                session_key="dashboard",
                tool_name=tool,
                outcome="failure",
                error="request_cancelled",
            )


# ── pure helpers ──


class TestContentMatchesExt:
    def test_zip_container_requires_pk_signature(self):
        assert files_mod._content_matches_ext(".docx", b"PK\x03\x04rest")
        assert files_mod._content_matches_ext(".zip", b"PK\x05\x06")
        assert files_mod._content_matches_ext(".odt", b"PK\x07\x08")
        assert not files_mod._content_matches_ext(".xlsx", b"<html>nope</html>")

    def test_webp_needs_the_compound_riff_signature(self):
        assert files_mod._content_matches_ext(".webp", b"RIFF\x00\x00\x00\x00WEBPmore")
        assert not files_mod._content_matches_ext(".webp", b"RIFF\x00\x00\x00\x00WAVEmore")

    def test_known_prefixes_are_enforced(self):
        assert files_mod._content_matches_ext(".png", b"\x89PNG\r\n\x1a\n")
        assert not files_mod._content_matches_ext(".png", b"\xff\xd8\xffnot a png")
        assert files_mod._content_matches_ext(".gif", b"GIF89a")
        assert files_mod._content_matches_ext(".pdf", b"%PDF-1.4")
        assert files_mod._content_matches_ext(".gz", b"\x1f\x8b\x08")

    def test_unsignable_extensions_pass_through(self):
        # No reliable magic for text or SVG: the extension allowlist is the gate.
        assert files_mod._content_matches_ext(".md", b"# anything")
        assert files_mod._content_matches_ext(".svg", b"<svg/>")


class TestFuzzyScore:
    def test_exact_and_stem_matches_outrank_everything(self):
        exact = files_mod._fuzzy_score("readme.md", "readme.md", "docs/readme.md")
        stem = files_mod._fuzzy_score("readme", "readme.md", "docs/readme.md")
        prefix = files_mod._fuzzy_score("read", "readme.md", "docs/readme.md")
        infix = files_mod._fuzzy_score("adme", "readme.md", "docs/readme.md")
        assert exact > prefix > infix
        assert stem > prefix

    def test_path_only_match_scores_below_name_match(self):
        in_path = files_mod._fuzzy_score("docs", "readme.md", "docs/readme.md")
        in_name = files_mod._fuzzy_score("adme", "readme.md", "docs/readme.md")
        assert 0 < in_path < in_name

    def test_subsequence_match_on_filename(self):
        # 'rdm' appears in order inside 'readme.md' but is not a substring.
        assert files_mod._fuzzy_score("rdm", "readme.md", "readme.md") > 0

    def test_subsequence_falls_back_to_the_relative_path(self):
        # 'dcsx' is not a subsequence of the filename, but is one of the path.
        assert files_mod._fuzzy_score("dcsx", "readme.md", "docs/extra/readme.md") > 0

    def test_unmatched_query_scores_zero(self):
        assert files_mod._fuzzy_score("zzqq", "readme.md", "docs/readme.md") == 0.0

    def test_shorter_names_get_the_brevity_bonus(self):
        short = files_mod._fuzzy_score("log", "log.txt", "log.txt")
        long = files_mod._fuzzy_score("log", "log_with_a_very_long_name.txt",
                                      "log_with_a_very_long_name.txt")
        assert short > long
