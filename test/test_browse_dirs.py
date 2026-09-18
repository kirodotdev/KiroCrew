"""Tests for /api/browse-dirs endpoint."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import platform_compat
from kiro_crew.dashboard.handlers import api_browse_dirs
from kiro_crew.dashboard.handlers.files import (
    _browse_dirs_sync,
    _browse_drives_sync,
    _browse_parent,
)


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/browse-dirs", api_browse_dirs)
    return app


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


class TestBrowseDirs:
    @pytest.mark.asyncio
    async def test_default_path_is_home(self, tmp_path, mock_sel):
        (tmp_path / "projects").mkdir()
        with patch("os.path.expanduser", side_effect=lambda p: p.replace("~", str(tmp_path))):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/browse-dirs")
                data = await resp.json()
                assert data["path"] == str(tmp_path)
                names = {d["name"] for d in data["dirs"]}
                assert "projects" in names

    @pytest.mark.asyncio
    async def test_lists_subdirectories(self, tmp_path, mock_sel):
        (tmp_path / "alpha").mkdir()
        (tmp_path / "beta").mkdir()
        (tmp_path / "file.txt").write_text("x")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/browse-dirs?path={tmp_path}")
            data = await resp.json()
            names = [d["name"] for d in data["dirs"]]
            assert "alpha" in names
            assert "beta" in names
            assert "file.txt" not in names  # files excluded

    @pytest.mark.asyncio
    async def test_sorted_alphabetically(self, tmp_path, mock_sel):
        (tmp_path / "zebra").mkdir()
        (tmp_path / "apple").mkdir()
        (tmp_path / "mango").mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/browse-dirs?path={tmp_path}")
            names = [d["name"] for d in (await resp.json())["dirs"]]
            assert names == ["apple", "mango", "zebra"]

    @pytest.mark.asyncio
    async def test_skips_hidden_and_excluded(self, tmp_path, mock_sel):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "src").mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/browse-dirs?path={tmp_path}")
            names = {d["name"] for d in (await resp.json())["dirs"]}
            assert names == {"src"}

    @pytest.mark.asyncio
    async def test_returns_parent(self, tmp_path, mock_sel):
        child = tmp_path / "child"
        child.mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/browse-dirs?path={child}")
            data = await resp.json()
            assert data["parent"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_invalid_path_returns_400(self, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/browse-dirs?path=/nonexistent_xyz_123")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_permission_error_returns_empty_dirs(self, tmp_path, mock_sel):
        restricted = tmp_path / "restricted"
        restricted.mkdir()
        restricted.chmod(0o000)
        try:
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/browse-dirs?path={restricted}")
                data = await resp.json()
                assert data["dirs"] == []
        finally:
            restricted.chmod(0o755)

    @pytest.mark.asyncio
    async def test_scan_does_not_run_on_the_event_loop(self, tmp_path, mock_sel):
        """The directory walk must execute off the loop thread.

        Moving a scan into a thread preserves behaviour exactly, so no assertion on
        the response body can tell the offload from an inline walk — a plain revert
        keeps every other test in this file green. Record the thread the scan really
        runs on and compare it against the loop's own thread instead.
        """
        (tmp_path / "alpha").mkdir()
        loop_thread = threading.get_ident()
        ran_on: list[int] = []

        def spy(base: str, skip: set[str]) -> list[dict]:
            ran_on.append(threading.get_ident())
            return _browse_dirs_sync(base, skip)

        with patch("kiro_crew.dashboard.handlers.files._browse_dirs_sync", spy):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/browse-dirs?path={tmp_path}")
                assert resp.status == 200
                names = {d["name"] for d in (await resp.json())["dirs"]}
                assert names == {"alpha"}
        assert ran_on, "the scan helper was never called"
        assert ran_on[0] != loop_thread


class TestWindowsDriveRoots:
    """A Windows drive root is the one directory whose parent is not a directory.

    ``ntpath.dirname("C:\\")`` is ``"C:\\"`` -- equal to itself, which the picker
    reads as "top" and hides its Back control on, stranding the user on one
    drive. The endpoint instead answers ``parent: ""`` there and offers the
    mounted drives behind ``?drives=1`` as the virtual level above.
    """

    def test_parent_of_drive_root_is_empty_on_windows(self):
        with patch.object(platform_compat, "IS_WINDOWS", True):
            assert _browse_parent("C:\\") == ""
            assert _browse_parent("d:/") == ""
            assert _browse_parent("C:") == ""

    def test_parent_of_drive_subdir_is_its_dirname_on_windows(self):
        with (
            patch.object(platform_compat, "IS_WINDOWS", True),
            patch("os.path.dirname", return_value="C:\\Users"),
        ):
            assert _browse_parent("C:\\Users\\me") == "C:\\Users"

    def test_drive_shaped_path_is_not_special_off_windows(self):
        # On POSIX "C:" is an ordinary relative name; only the real platform
        # earns the virtual level, never a lookalike string.
        import os as _os

        with patch.object(platform_compat, "IS_WINDOWS", False):
            assert _browse_parent("C:\\") == _os.path.dirname("C:\\")
            assert _browse_parent("/") == "/"
            assert _browse_parent("/srv/proj") == "/srv"

    def test_drives_listing_uses_os_listdrives_when_present(self):
        with patch("os.listdrives", create=True, return_value=["C:\\", "D:\\"]):
            assert _browse_drives_sync() == [
                {"name": "C:\\", "path": "C:\\"},
                {"name": "D:\\", "path": "D:\\"},
            ]

    @pytest.mark.asyncio
    async def test_drives_query_lists_roots_on_windows(self, mock_sel):
        with (
            patch.object(platform_compat, "IS_WINDOWS", True),
            patch(
                "kiro_crew.dashboard.handlers.files._browse_drives_sync",
                return_value=[{"name": "C:\\", "path": "C:\\"}, {"name": "D:\\", "path": "D:\\"}],
            ),
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/browse-dirs?drives=1")
                assert resp.status == 200
                data = await resp.json()
        assert data == {
            "path": "",
            "parent": "",
            "dirs": [{"name": "C:\\", "path": "C:\\"}, {"name": "D:\\", "path": "D:\\"}],
        }
        mock_sel.log_api_access.assert_called_once()
        assert mock_sel.log_api_access.call_args.kwargs["resources"] == "drives"

    @pytest.mark.asyncio
    async def test_drives_listing_runs_off_loop_on_the_transfer_pool(self, mock_sel):
        """The drive walk is filesystem work on a caller-driven path and must go
        through ``_run_path_probe(..., transfer=True)`` like the other listings.
        Calling the helper inline, or on the probe pool, leaves every response
        assertion green -- so pin the dispatch itself: the wrapper is called with
        ``transfer=True`` and the helper really runs off the loop thread.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        loop_thread = threading.get_ident()
        ran_on: list[int] = []
        dispatched: list[tuple[object, object]] = []
        real_probe = files_mod._run_path_probe

        def fake_drives() -> list[dict[str, str]]:
            ran_on.append(threading.get_ident())
            return [{"name": "C:\\", "path": "C:\\"}]

        async def spy(fn, /, *args, **kwargs):
            dispatched.append((fn, kwargs.get("transfer")))
            return await real_probe(fn, *args, **kwargs)

        with (
            patch.object(platform_compat, "IS_WINDOWS", True),
            patch.object(files_mod, "_browse_drives_sync", fake_drives),
            patch.object(files_mod, "_run_path_probe", spy),
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/browse-dirs?drives=1")
                assert resp.status == 200
                assert (await resp.json())["dirs"] == [{"name": "C:\\", "path": "C:\\"}]
        assert dispatched == [(fake_drives, True)]
        assert ran_on and ran_on[0] != loop_thread

    @pytest.mark.skipif(
        not platform_compat.IS_WINDOWS, reason="real drive enumeration needs a Windows host"
    )
    @pytest.mark.asyncio
    async def test_drives_query_lists_real_drives_on_a_windows_host(self, mock_sel):
        """Unmocked: on CI's Windows shards this exercises ``os.listdrives`` (or the
        letter probe) against the machine's real volumes. The system drive is
        always mounted, so it must be in the list, and browsing it must report
        the drive-list level above it.
        """
        import os as _os

        system_root = (_os.environ.get("SystemDrive") or "C:") + "\\"
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/browse-dirs?drives=1")
            assert resp.status == 200
            data = await resp.json()
            assert data["path"] == "" and data["parent"] == ""
            roots = [d["path"] for d in data["dirs"]]
            assert system_root in roots, roots
            resp = await client.get(f"/api/browse-dirs?path={system_root}")
            assert resp.status == 200
            root = await resp.json()
        assert root["path"].upper() == system_root.upper()
        assert root["parent"] == ""

    @pytest.mark.asyncio
    async def test_drives_query_is_400_off_windows(self, mock_sel):
        with (
            patch.object(platform_compat, "IS_WINDOWS", False),
            patch("kiro_crew.dashboard.handlers.files._browse_drives_sync") as lister,
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/browse-dirs?drives=1")
                assert resp.status == 400
        lister.assert_not_called()

    @pytest.mark.asyncio
    async def test_drives_query_ignores_a_path_and_never_touches_it(self, tmp_path, mock_sel):
        # `drives=1` wins over `path=`: the virtual level has no directory to
        # resolve, so the caller-supplied root is never even probed.
        (tmp_path / "alpha").mkdir()
        with (
            patch.object(platform_compat, "IS_WINDOWS", True),
            patch("kiro_crew.dashboard.handlers.files._browse_drives_sync", return_value=[]),
            patch("kiro_crew.dashboard.handlers.files._resolve_search_root") as resolver,
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/browse-dirs?drives=1&path={tmp_path}")
                assert resp.status == 200
                assert (await resp.json())["dirs"] == []
        resolver.assert_not_called()

    @pytest.mark.asyncio
    async def test_browsing_a_drive_root_reports_empty_parent(self, tmp_path, mock_sel):
        # The endpoint's own resolver hands back whatever the filesystem calls
        # the root; fake a Windows answer so the parent rule is exercised end to
        # end through the handler rather than on the helper alone.
        (tmp_path / "alpha").mkdir()
        with (
            patch.object(platform_compat, "IS_WINDOWS", True),
            patch(
                "kiro_crew.dashboard.handlers.files._resolve_search_root",
                return_value=("C:\\", True),
            ),
            patch(
                "kiro_crew.dashboard.handlers.files._browse_dirs_sync",
                return_value=[{"name": "Users", "path": "C:\\Users"}],
            ),
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/browse-dirs?path=C:%5C")
                assert resp.status == 200
                data = await resp.json()
        assert data["path"] == "C:\\"
        assert data["parent"] == ""
        assert data["dirs"] == [{"name": "Users", "path": "C:\\Users"}]
