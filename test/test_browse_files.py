"""Tests for ``GET /api/browse-files`` — activity-panel file browser.

Mirrors test_browse_dirs.py but covers the additional ``files`` array,
dirs-first sorting, build-artifact skip set, hidden-file filtering, and the
realpath-based symlink check that closes the symlink-bypass for sensitive
paths (added).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_browse_files


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/browse-files", api_browse_files)
    return app


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


class TestBrowseFiles:
    @pytest.mark.asyncio
    async def test_default_path_is_home(self, tmp_path, mock_sel):
        (tmp_path / "projects").mkdir()
        (tmp_path / "notes.md").write_text("x")
        with patch("os.path.expanduser", side_effect=lambda p: p.replace("~", str(tmp_path))):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/browse-files")
                data = await resp.json()
                assert data["path"] == str(tmp_path)
                assert "dirs" in data and "files" in data
                # File appears in files, directory appears in dirs.
                assert any(f["name"] == "notes.md" for f in data["files"])
                assert any(d["name"] == "projects" for d in data["dirs"])

    @pytest.mark.asyncio
    async def test_lists_files_alongside_dirs(self, tmp_path, mock_sel):
        (tmp_path / "alpha").mkdir()
        (tmp_path / "readme.md").write_text("hello")
        (tmp_path / "code.py").write_text("pass")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/browse-files?path={tmp_path}")
            data = await resp.json()
            file_names = {f["name"] for f in data["files"]}
            dir_names = {d["name"] for d in data["dirs"]}
            assert file_names == {"readme.md", "code.py"}
            assert dir_names == {"alpha"}

    @pytest.mark.asyncio
    async def test_dirs_first_then_alphabetical(self, tmp_path, mock_sel):
        (tmp_path / "zzz_file.txt").write_text("x")
        (tmp_path / "aaa_dir").mkdir()
        (tmp_path / "mango").mkdir()
        (tmp_path / "apple_file.txt").write_text("x")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/browse-files?path={tmp_path}")
            data = await resp.json()
            # dirs listed first, then files; each group sorted case-insensitively.
            dir_names = [d["name"] for d in data["dirs"]]
            file_names = [f["name"] for f in data["files"]]
            assert dir_names == ["aaa_dir", "mango"]
            assert file_names == ["apple_file.txt", "zzz_file.txt"]

    @pytest.mark.asyncio
    async def test_hidden_files_skipped(self, tmp_path, mock_sel):
        (tmp_path / ".secret_dir").mkdir()
        (tmp_path / ".hidden.txt").write_text("x")
        (tmp_path / "visible.txt").write_text("y")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/browse-files?path={tmp_path}")
            data = await resp.json()
            file_names = {f["name"] for f in data["files"]}
            dir_names = {d["name"] for d in data["dirs"]}
            assert file_names == {"visible.txt"}
            assert dir_names == set()

    @pytest.mark.asyncio
    async def test_build_artifact_dirs_skipped(self, tmp_path, mock_sel):
        for d in ["node_modules", "__pycache__", ".cache", "build", "dist", ".next", ".kirocrew"]:
            (tmp_path / d).mkdir()
        (tmp_path / "src").mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/browse-files?path={tmp_path}")
            data = await resp.json()
            assert {d["name"] for d in data["dirs"]} == {"src"}

    @pytest.mark.asyncio
    async def test_invalid_path_returns_400(self, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/browse-files?path=/nonexistent_xyz_browse_files")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_returns_parent(self, tmp_path, mock_sel):
        child = tmp_path / "child"
        child.mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/browse-files?path={child}")
            data = await resp.json()
            assert data["parent"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_sensitive_base_path_returns_403(self, tmp_path, mock_sel):
        # is_sensitive_path should reject the base path and never list contents.
        (tmp_path / "secret.txt").write_text("AKIA...")
        with patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=True):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/browse-files?path={tmp_path}")
                assert resp.status == 403

    @pytest.mark.asyncio
    async def test_symlink_to_sensitive_path_filtered(self, tmp_path, mock_sel):
        """Symlink in a benign dir pointing at ~/.aws must not leak through.

        Pre-fix, the sensitivity check ran on entry.path (the link itself), not
        the realpath, so a symlink named ``creds`` pointing at ``~/.aws/credentials``
        would surface in the listing. This test pins the realpath fix.
        """
        secret_target = tmp_path / "secret_target.ini"
        secret_target.write_text("aws_access_key_id=AKIAIOSFODNN7EXAMPLE")
        link = tmp_path / "credentials_link"
        os.symlink(secret_target, link)
        # Mark only the secret_target as sensitive — realpath resolution
        # should bubble the sensitivity onto the link.

        def is_sens(p: str) -> bool:
            return os.path.realpath(p) == str(secret_target)

        with patch(
            "kiro_crew.dashboard.handlers.files.is_sensitive_path",
            side_effect=is_sens,
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/browse-files?path={tmp_path}")
                data = await resp.json()
                names = {f["name"] for f in data["files"]} | {d["name"] for d in data["dirs"]}
                assert "credentials_link" not in names
                assert "secret_target.ini" not in names

    @pytest.mark.asyncio
    async def test_permission_error_returns_empty_lists(self, tmp_path, mock_sel):
        restricted = tmp_path / "restricted"
        restricted.mkdir()
        restricted.chmod(0o000)
        try:
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/browse-files?path={restricted}")
                data = await resp.json()
                assert data["dirs"] == []
                assert data["files"] == []
        finally:
            restricted.chmod(0o755)

    @pytest.mark.asyncio
    async def test_entries_include_mtime(self, tmp_path, mock_sel):
        """Each dir and file entry carries an integer mtime so the activity
        panel can offer a sort-by-date option."""
        (tmp_path / "alpha").mkdir()
        (tmp_path / "readme.md").write_text("hello")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/browse-files?path={tmp_path}")
            data = await resp.json()
            for entry in data["dirs"] + data["files"]:
                assert "mtime" in entry, entry
                assert isinstance(entry["mtime"], int)
                assert entry["mtime"] > 0

    @pytest.mark.asyncio
    async def test_entry_unstattable_mtime_degrades_to_zero(self, tmp_path, mock_sel):
        """A scandir entry whose stat() raises OSError (file removed mid-scan,
        permission race, broken symlink under follow_symlinks=True) still
        appears in the listing with mtime == 0; the directory listing must
        degrade gracefully rather than 500. Pins the OSError->mtime=0 contract
        that the happy-path test_entries_include_mtime does not exercise.
        """
        target = tmp_path / "racey.md"
        target.write_text("x")

        class _OSErrorEntry:
            name = "racey.md"
            path = str(target)

            def is_dir(self, follow_symlinks: bool = True) -> bool:
                return False

            def is_file(self, follow_symlinks: bool = True) -> bool:
                return True

            def stat(self, follow_symlinks: bool = True):
                raise OSError("stat raced (entry removed mid-scan)")

        with patch(
            "kiro_crew.dashboard.handlers.files.os.scandir",
            return_value=[_OSErrorEntry()],
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/browse-files?path={tmp_path}")
                # The unstattable entry must not break the whole listing.
                assert resp.status == 200
                data = await resp.json()
                entry = next(e for e in data["files"] if e["name"] == "racey.md")
                assert entry["mtime"] == 0
