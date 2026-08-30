"""Tests for /api/file-search endpoint."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_file_search


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-search", api_file_search)
    # api_file_search reads app["state"].file_indexes for the index fast path
    state = MagicMock()
    state.file_indexes.get.return_value = None  # no index → always use walk fallback
    app["state"] = state
    return app


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


def _populate(tmp_path):
    """Create a small file tree for search tests."""
    (tmp_path / "hello.py").write_text("x")
    (tmp_path / "hello_world.py").write_text("xx")
    (tmp_path / "readme.md").write_text("# hi")
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "hello_util.py").write_text("y")
    (sub / ".secret").write_text("s")
    # excluded dirs
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "hello_dep.py").write_text("z")
    git = tmp_path / ".git"
    git.mkdir()
    (git / "hello_obj").write_text("g")


class TestFileSearch:
    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-search?q=")
            assert resp.status == 200
            data = await resp.json()
            assert data["results"] == []

    @pytest.mark.asyncio
    async def test_short_query_returns_empty(self, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-search?q=a")
            assert resp.status == 200
            assert (await resp.json())["results"] == []

    @pytest.mark.asyncio
    async def test_basic_match(self, tmp_path, mock_sel):
        _populate(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=hello&project={tmp_path}")
            data = await resp.json()
            names = {r["name"] for r in data["results"]}
            assert "hello.py" in names
            assert "hello_world.py" in names
            assert "hello_util.py" in names
            # Check fields present
            r0 = data["results"][0]
            assert "path" in r0 and "size" in r0 and "mtime" in r0

    @pytest.mark.asyncio
    async def test_skips_hidden_files(self, tmp_path, mock_sel):
        _populate(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=secret&project={tmp_path}")
            names = {r["name"] for r in (await resp.json())["results"]}
            assert ".secret" not in names

    @pytest.mark.asyncio
    async def test_skips_excluded_dirs(self, tmp_path, mock_sel):
        _populate(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=hello&project={tmp_path}")
            paths = [r["path"] for r in (await resp.json())["results"]]
            assert not any("node_modules" in p for p in paths)
            assert not any(".git" in p for p in paths)

    @pytest.mark.asyncio
    async def test_workspace_scoping(self, tmp_path, mock_sel):
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        (ws_dir / "target.py").write_text("t")
        (tmp_path / "target_outside.py").write_text("o")
        with patch("kiro_crew.config.loader.workspace_dir_for", return_value=ws_dir):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/file-search?q=target&workspace=myws")
                names = {r["name"] for r in (await resp.json())["results"]}
                assert "target.py" in names
                assert "target_outside.py" not in names

    @pytest.mark.asyncio
    async def test_max_results_capped(self, tmp_path, mock_sel):
        for i in range(20):
            (tmp_path / f"match_{i:02d}.txt").write_text("x")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=match&project={tmp_path}")
            results = (await resp.json())["results"]
            assert len(results) == 15

    @pytest.mark.asyncio
    async def test_sort_shorter_name_first(self, tmp_path, mock_sel):
        (tmp_path / "ab.py").write_text("x")
        (tmp_path / "abcdef.py").write_text("x")
        (tmp_path / "abc.py").write_text("x")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=ab&project={tmp_path}")
            names = [r["name"] for r in (await resp.json())["results"]]
            assert names[0] == "ab.py"
            assert names[1] == "abc.py"
            assert names[2] == "abcdef.py"

    @pytest.mark.asyncio
    async def test_fallback_does_not_use_home(self, tmp_path, mock_sel):
        """$HOME is not an implicit fallback root.

        Walking it reached every TCC-gated folder on macOS -- one consent dialog
        each -- for a query the caller scoped nowhere. Asking for home
        explicitly (``?project=``) still works and is covered separately.
        """
        (tmp_path / "findme.txt").write_text("x")
        with patch.dict(os.environ, {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}, clear=False), \
             patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": ""}, clear=False):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/file-search?q=findme")
                names = {r["name"] for r in (await resp.json())["results"]}
                assert "findme.txt" not in names

    @pytest.mark.asyncio
    async def test_explicit_home_project_still_searched(self, tmp_path, mock_sel):
        """Naming home is deliberate and stays fully searchable."""
        (tmp_path / "findme.txt").write_text("x")
        with patch.dict(os.environ, {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}, clear=False):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/file-search?q=findme&project={tmp_path}")
                names = {r["name"] for r in (await resp.json())["results"]}
                assert "findme.txt" in names

    @pytest.mark.asyncio
    async def test_project_scoping(self, tmp_path, mock_sel):
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        (proj_dir / "target.py").write_text("t")
        (tmp_path / "target_outside.py").write_text("o")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=target&project={proj_dir}")
            data = await resp.json()
            names = {r["name"] for r in data["results"]}
            assert "target.py" in names
            assert "target_outside.py" not in names
            assert data["root"] == str(proj_dir)

    @pytest.mark.asyncio
    async def test_project_sensitive_path_rejected(self, tmp_path, mock_sel):
        sensitive_dir = tmp_path / "secret"
        sensitive_dir.mkdir()
        with patch("kiro_crew.security.is_sensitive_path", return_value=True):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(
                    f"/api/file-search?q=test&project={sensitive_dir}"
                )
                assert resp.status == 403
                data = await resp.json()
                assert data["error"] == "Access denied"
                # The UI keys per-cause copy on `code`, never on the human string.
                assert data["code"] == "access_denied"

    @pytest.mark.asyncio
    async def test_path_match_ranked_below_name_match(self, tmp_path, mock_sel):
        sub = tmp_path / "myfeature"
        sub.mkdir()
        (sub / "utils.py").write_text("x")          # path matches "myfeature"
        (tmp_path / "myfeature.py").write_text("x")  # filename matches "myfeature"
        async with TestClient(TestServer(_make_app())) as client:
            # kinds=files: this asserts name-match vs path-match ranking among
            # files. The "myfeature" directory itself is a legitimate hit under
            # the default kinds=all, so it is excluded here to keep the
            # assertion about the ranking rule under test.
            resp = await client.get(
                f"/api/file-search?q=myfeature&kinds=files&project={tmp_path}"
            )
            results = (await resp.json())["results"]
            assert results[0]["name"] == "myfeature.py"  # filename match ranked first
            assert any(r["name"] == "utils.py" for r in results)  # path match included

    @pytest.mark.asyncio
    async def test_project_not_found_returns_404(self, tmp_path, mock_sel):
        missing = tmp_path / "nonexistent"
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=test&project={missing}")
            assert resp.status == 404
            data = await resp.json()
            assert data["error"] == "Project directory not found"
            assert data["code"] == "project_not_found"
            assert data["results"] == []

    @pytest.mark.asyncio
    async def test_fuzzy_char_order_match(self, tmp_path, mock_sel):
        """Typing 'flspy' should match 'files.py' (chars in order)."""
        (tmp_path / "files.py").write_text("x")
        (tmp_path / "readme.md").write_text("x")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=flspy&project={tmp_path}")
            names = {r["name"] for r in (await resp.json())["results"]}
            assert "files.py" in names
            assert "readme.md" not in names

    @pytest.mark.asyncio
    async def test_exact_name_beats_substring(self, tmp_path, mock_sel):
        """Exact filename match should rank above substring match."""
        (tmp_path / "config.py").write_text("x")
        (tmp_path / "config_loader.py").write_text("x")
        (tmp_path / "myconfig.py").write_text("x")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=config&project={tmp_path}")
            results = (await resp.json())["results"]
            # Exact stem match "config" should be first
            assert results[0]["name"] == "config.py"

    @pytest.mark.asyncio
    async def test_prefix_beats_infix(self, tmp_path, mock_sel):
        """Filename starting with query should rank above contains-in-middle."""
        (tmp_path / "test_utils.py").write_text("x")
        (tmp_path / "my_test.py").write_text("x")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=test&project={tmp_path}")
            results = (await resp.json())["results"]
            assert results[0]["name"] == "test_utils.py"

    @pytest.mark.asyncio
    async def test_no_score_field_in_response(self, tmp_path, mock_sel):
        """Internal _score field must not leak into API response."""
        (tmp_path / "hello.py").write_text("x")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=hello&project={tmp_path}")
            for r in (await resp.json())["results"]:
                assert "_score" not in r

    @pytest.mark.asyncio
    async def test_fuzzy_no_match_excluded(self, tmp_path, mock_sel):
        """Query chars not all present in order should return no results."""
        (tmp_path / "abc.py").write_text("x")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=xyz&project={tmp_path}")
            assert (await resp.json())["results"] == []

    # ---- caller-supplied ``limit`` (#5639) ----------------------------------

    @pytest.mark.asyncio
    async def test_limit_param_honoured(self, tmp_path, mock_sel):
        """?limit= raises the page size past the default of 15."""
        for i in range(25):
            (tmp_path / f"match_{i:02d}.txt").write_text("x")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=match&project={tmp_path}&limit=20")
            assert len((await resp.json())["results"]) == 20

    @pytest.mark.asyncio
    async def test_limit_clamped_at_server_ceiling(self, tmp_path, mock_sel):
        """SECURITY: an absurd limit is clamped to _SEARCH_LIMIT_CEILING, never
        passed to the walk -- max_collect multiplies it 10x, so an unclamped
        value would amplify server-side filesystem work."""
        from kiro_crew.dashboard.handlers.files import _SEARCH_LIMIT_CEILING
        for i in range(_SEARCH_LIMIT_CEILING + 10):
            (tmp_path / f"match_{i:03d}.txt").write_text("x")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=match&project={tmp_path}&limit=999999")
            assert len((await resp.json())["results"]) == _SEARCH_LIMIT_CEILING

    @pytest.mark.asyncio
    async def test_limit_non_integer_falls_back_to_default(self, tmp_path, mock_sel):
        """A non-integer limit is rejected in favour of the default, not 500."""
        for i in range(20):
            (tmp_path / f"match_{i:02d}.txt").write_text("x")
        async with TestClient(TestServer(_make_app())) as client:
            for bad in ("abc", "1e9", "15.5", ""):
                resp = await client.get(f"/api/file-search?q=match&project={tmp_path}&limit={bad}")
                assert resp.status == 200
                assert len((await resp.json())["results"]) == 15, bad

    @pytest.mark.asyncio
    async def test_limit_negative_or_zero_clamped_to_floor(self, tmp_path, mock_sel):
        """A negative/zero limit is clamped to 1, never passed to the walk."""
        for i in range(5):
            (tmp_path / f"match_{i}.txt").write_text("x")
        async with TestClient(TestServer(_make_app())) as client:
            for bad in ("-5", "0"):
                resp = await client.get(f"/api/file-search?q=match&project={tmp_path}&limit={bad}")
                assert len((await resp.json())["results"]) == 1, bad
