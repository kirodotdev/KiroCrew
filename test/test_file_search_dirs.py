"""Tests for directory results in FileIndex and /api/file-search.

Covers the folder @-mention feature: directory entries carry ``kind: "dir"``,
the ``kinds`` query param filters the result set, files outrank directories on
an otherwise equal score, and symlinked directories are resolved before the
sensitivity check.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.file_index import FileIndex
from kiro_crew.dashboard.handlers import api_file_search
from kiro_crew.dashboard.handlers import files as files_mod


def _make_app(index=None) -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-search", api_file_search)
    state = MagicMock()
    state.file_indexes.get.return_value = index
    app["state"] = state
    return app


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


def _populate(tmp_path):
    """Tree with directories and files sharing the "widget" stem."""
    (tmp_path / "widgets.py").write_text("x")
    wdir = tmp_path / "widgets"
    wdir.mkdir()
    (wdir / "button.py").write_text("y")
    nested = wdir / "widgetsinner"
    nested.mkdir()
    (nested / "leaf.py").write_text("z")
    # An empty directory: proof dirs are walked, not derived from file paths.
    (tmp_path / "widgetsempty").mkdir()
    # Excluded: dot-prefixed and skip-listed dirs
    (tmp_path / ".widgetshidden").mkdir()
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "widgetsdep").mkdir()


def _scorer(q, name, rel):
    nl = name.lower()
    if q in nl:
        return 10.0
    if q in rel.lower():
        return 5.0
    return 0.0


class TestFileIndexDirs:
    @pytest.mark.asyncio
    async def test_index_includes_dir_entries(self, tmp_path):
        _populate(tmp_path)
        idx = FileIndex(str(tmp_path))
        await idx.start()
        try:
            results = idx.search("widgets", _scorer)
            by_name = {r["name"]: r for r in results}
            assert by_name["widgets"]["kind"] == "dir"
            assert by_name["widgets.py"]["kind"] == "file"
            # Directory entries report size 0 and a real mtime.
            assert by_name["widgets"]["size"] == 0
            assert by_name["widgets"]["mtime"] > 0
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_index_includes_empty_dir(self, tmp_path):
        """An empty dir has no files, so it can only appear if dirs are walked."""
        _populate(tmp_path)
        idx = FileIndex(str(tmp_path))
        await idx.start()
        try:
            names = {r["name"] for r in idx.search("widgetsempty", _scorer)}
            assert "widgetsempty" in names
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_index_kinds_filter(self, tmp_path):
        _populate(tmp_path)
        idx = FileIndex(str(tmp_path))
        await idx.start()
        try:
            dirs = idx.search("widgets", _scorer, 15, "dirs")
            assert dirs, "expected at least one directory hit"
            assert all(r["kind"] == "dir" for r in dirs)

            files = idx.search("widgets", _scorer, 15, "files")
            assert files, "expected at least one file hit"
            assert all(r["kind"] == "file" for r in files)

            both = {r["kind"] for r in idx.search("widgets", _scorer, 15, "all")}
            assert both == {"dir", "file"}
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_index_excludes_hidden_and_skip_dirs(self, tmp_path):
        _populate(tmp_path)
        idx = FileIndex(str(tmp_path))
        await idx.start()
        try:
            names = {r["name"] for r in idx.search("widgets", _scorer, 50, "dirs")}
            assert ".widgetshidden" not in names
            assert "widgetsdep" not in names
            assert "node_modules" not in names
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_index_files_outrank_dirs_on_equal_score(self, tmp_path):
        """Equal score and equal name length: the file must sort first."""
        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha.x").write_text("x")

        def flat_scorer(q, name, rel):
            return 10.0 if q in name.lower() else 0.0

        idx = FileIndex(str(tmp_path))
        await idx.start()
        try:
            results = idx.search("alpha", flat_scorer)
            kinds = [r["kind"] for r in results]
            assert kinds[0] == "file", f"expected file first, got {results}"
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_index_dir_symlink_resolved_before_sensitive_check(self, tmp_path):
        """A symlink into a sensitive tree must be rejected on its real path."""
        _populate(tmp_path)
        link = tmp_path / "widgetslink"
        os.symlink(str(tmp_path / "widgets"), str(link))

        idx = FileIndex(str(tmp_path))
        real = os.path.realpath(str(link))

        def fake_sensitive(p):
            return os.path.realpath(p) == real

        with patch("kiro_crew.dashboard.file_index.is_sensitive_path", side_effect=fake_sensitive):
            entries, _ = idx._walk()
        names = {e[1] for e in entries if e[5] == "dir"}
        assert "widgetslink" not in names
        assert "widgets" not in names, "the symlink target shares the real path and must also be filtered"

    @pytest.mark.asyncio
    async def test_index_file_symlink_resolved_before_sensitive_check(self, tmp_path):
        """A symlinked FILE into a sensitive tree is rejected on its real path."""
        _populate(tmp_path)
        link = tmp_path / "widgetslink.py"
        os.symlink(str(tmp_path / "widgets.py"), str(link))

        idx = FileIndex(str(tmp_path))
        real = os.path.realpath(str(link))

        def fake_sensitive(p):
            return os.path.realpath(p) == real

        with patch("kiro_crew.dashboard.file_index.is_sensitive_path", side_effect=fake_sensitive):
            entries, _ = idx._walk()
        names = {e[1] for e in entries if e[5] == "file"}
        assert "widgetslink.py" not in names
        assert "widgets.py" not in names, "the symlink target shares the real path"


class TestApiFileSearchDirs:
    @pytest.mark.asyncio
    async def test_walk_fallback_returns_dirs(self, tmp_path, mock_sel):
        _populate(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=widgets&project={tmp_path}")
            assert resp.status == 200
            results = (await resp.json())["results"]
            by_name = {r["name"]: r for r in results}
            assert by_name["widgets"]["kind"] == "dir"
            assert by_name["widgets"]["size"] == 0
            assert by_name["widgets.py"]["kind"] == "file"

    @pytest.mark.asyncio
    async def test_walk_fallback_kinds_dirs_only(self, tmp_path, mock_sel):
        _populate(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                f"/api/file-search?q=widgets&kinds=dirs&project={tmp_path}"
            )
            results = (await resp.json())["results"]
            assert results
            assert all(r["kind"] == "dir" for r in results)

    @pytest.mark.asyncio
    async def test_walk_fallback_kinds_files_only(self, tmp_path, mock_sel):
        _populate(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                f"/api/file-search?q=widgets&kinds=files&project={tmp_path}"
            )
            results = (await resp.json())["results"]
            assert results
            assert all(r["kind"] == "file" for r in results)

    @pytest.mark.asyncio
    async def test_unknown_kinds_value_falls_back_to_all(self, tmp_path, mock_sel):
        _populate(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                f"/api/file-search?q=widgets&kinds=bogus&project={tmp_path}"
            )
            assert resp.status == 200
            kinds = {r["kind"] for r in (await resp.json())["results"]}
            assert kinds == {"dir", "file"}

    @pytest.mark.asyncio
    async def test_walk_fallback_excludes_hidden_and_skip_dirs(self, tmp_path, mock_sel):
        _populate(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                f"/api/file-search?q=widgets&kinds=dirs&project={tmp_path}"
            )
            names = {r["name"] for r in (await resp.json())["results"]}
            assert ".widgetshidden" not in names
            assert "widgetsdep" not in names

    @pytest.mark.asyncio
    async def test_index_fast_path_forwards_kinds(self, tmp_path, mock_sel):
        """The fast path must pass the kinds param through to FileIndex.search."""
        _populate(tmp_path)
        idx = FileIndex(str(tmp_path))
        await idx.start()
        try:
            app = _make_app(index=idx)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get(
                    f"/api/file-search?q=widgets&kinds=dirs&project={tmp_path}"
                )
                data = await resp.json()
                assert data["root"] == os.path.realpath(str(tmp_path))
                assert data["results"]
                assert all(r["kind"] == "dir" for r in data["results"])
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_walk_fallback_file_symlink_resolved_before_sensitive_check(
        self, tmp_path, mock_sel
    ):
        """A symlinked FILE into a sensitive tree is rejected on its real path.

        The directory branch already resolved symlinks first; files must match so
        the two branches cannot disagree about what counts as sensitive.
        """
        _populate(tmp_path)
        link = tmp_path / "widgetslink.py"
        os.symlink(str(tmp_path / "widgets.py"), str(link))
        real = os.path.realpath(str(link))

        def fake_sensitive(p):
            return os.path.realpath(p) == real

        with patch(
            # api_file_search imports is_sensitive_path locally from
            # kiro_crew.security, so the source module is the patch target.
            "kiro_crew.security.is_sensitive_path",
            side_effect=fake_sensitive,
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(
                    f"/api/file-search?q=widgets&kinds=files&project={tmp_path}"
                )
                names = {r["name"] for r in (await resp.json())["results"]}
        assert "widgetslink.py" not in names
        assert "widgets.py" not in names, "the symlink target shares the real path"

    @pytest.mark.asyncio
    async def test_many_matching_dirs_do_not_starve_files(self, tmp_path, mock_sel):
        """Directories must not consume the whole candidate budget.

        A shared collection cap let a burst of matching directories fill it
        before any file was examined, so the best-scoring file was never even a
        candidate. The directories here score LOWER than the file, so a correct
        implementation returns the file regardless of ranking: the only way to
        lose it is to never collect it.
        """
        # max_collect is max_results * 10 = 150, so 400 matching directories
        # would previously exhaust it before the file was reached.
        for i in range(400):
            (tmp_path / f"zzz_widgets{i:03d}").mkdir()
        (tmp_path / "widgets.py").write_text("x")

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=widgets&project={tmp_path}")
            results = (await resp.json())["results"]
        names = [r["name"] for r in results]
        assert "widgets.py" in names, "the file was crowded out by directory candidates"

    @pytest.mark.asyncio
    async def test_files_and_dirs_have_independent_scan_budgets(self, tmp_path, mock_sel):
        """A file-heavy root must not starve the directory scan.

        A single shared scan counter let files spend the whole budget before any
        directory was examined, so directory search returned nothing even though
        it was enabled. Non-matching files are used so only the SCAN budget (not
        the candidate cap) is under test.
        """
        for i in range(5_000):
            (tmp_path / f"zz{i:04d}.py").write_text("x")
        (tmp_path / "widgets_dir").mkdir()

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=widgets&project={tmp_path}")
            results = (await resp.json())["results"]
        names = [r["name"] for r in results]
        assert "widgets_dir" in names, "directory candidates were starved by the file scan"

    @pytest.mark.asyncio
    async def test_walk_stops_at_overall_scan_ceiling(self, tmp_path, mock_sel):
        """The walk must stop descending once the dirs-visited ceiling is hit.

        The per-kind budgets do not terminate the walk: on `kinds=all` the dir
        counter advances per directory NAME, so a deep tree with few dirs per
        level exhausts the file budget at level one while the dir half of the
        done-check stays false forever, and `os.walk` descends the whole tree.

        The tree must be DEEP AND NARROW. A wide tree does not reproduce it --
        the root's many directory names exhaust the dir budget immediately, which
        is why the flat 5,000-file budget test could not catch this.
        """
        cur = tmp_path
        levels = 30
        for d in range(levels):
            # A handful of files per level is enough to spend the (shrunk) file
            # budget; the bug is about depth, not file volume.
            for f in range(3):
                (cur / f"zz{d:02d}_{f}.py").write_text("x")
            cur = cur / f"n{d:02d}"
            cur.mkdir()

        real_walk = os.walk
        calls = {"n": 0}

        def counting_walk(*a, **kw):
            # Count yielded levels, not the call: os.walk is a generator, so this
            # measures how far the traversal actually got. Patched on the
            # handler's `os` -- patching os.scandir does nothing, since os.walk
            # bound it at import time.
            for item in real_walk(*a, **kw):
                calls["n"] += 1
                yield item

        with patch.object(files_mod, "_WALK_MAX_SCAN_SCOPED", 10_000), \
                patch.object(files_mod, "_WALK_MAX_DIRS_VISITED", 10), \
                patch.object(files_mod.os, "walk", counting_walk):
            async with TestClient(TestServer(_make_app())) as client:
                # Matches nothing, so no candidate cap can end the walk.
                resp = await client.get(f"/api/file-search?q=qqqnomatch&project={tmp_path}")
                assert resp.status == 200

        # The per-kind budget is left out of reach on purpose: a budget below the
        # level count would be exhausted by the directory names themselves and
        # mask the bug. So the ceiling (10) is the only thing that can stop the
        # walk. Unbounded, it descends all 31 levels.
        assert calls["n"] <= 12, (
            f"walk descended {calls['n']} directories of {levels + 1} -- the "
            "dirs-visited ceiling did not stop the traversal"
        )
