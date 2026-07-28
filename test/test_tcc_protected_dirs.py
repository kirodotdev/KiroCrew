"""Tests for macOS TCC-protected home-directory pruning.

macOS gates ~/Downloads, ~/Documents, ~/Desktop (etc.) behind TCC, and records
consent PER (app, folder) pair — so a single unscoped walk rooted at $HOME
that reaches three of them produces THREE consent modals. The walks that only
touched those folders incidentally (as a $HOME catch-all fallback) prune them.

Walks the user explicitly scoped — a project dir, or ~/Downloads itself — are
NOT pruned: that access is deliberate and macOS's own one-time prompt is the
expected contract.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import platform_compat
from kiro_crew.dashboard.file_index import FileIndex
from kiro_crew.dashboard.handlers import api_file_search

# Folders that must never be walked incidentally from a bare $HOME root.
_TCC_DIRS = ("Downloads", "Documents", "Desktop")


def _home_env(tmp_path) -> dict[str, str]:
    """Env overrides that repoint ``expanduser("~")`` at *tmp_path* on ANY OS.

    ``$HOME`` alone is not enough: on Windows ``os.path`` is ``ntpath``, whose
    ``expanduser`` consults ``%USERPROFILE%`` (then ``HOMEDRIVE``/``HOMEPATH``)
    and ignores ``HOME`` entirely — so a HOME-only patch silently fails to pin
    the fake home on the Windows CI shard and inverts every prune assertion.
    """
    return {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}


def _make_home(tmp_path):
    """Build a fake $HOME containing TCC folders plus a normal project dir."""
    for name in _TCC_DIRS:
        d = tmp_path / name
        d.mkdir()
        (d / "target.txt").write_text("x")
    proj = tmp_path / "code"
    proj.mkdir()
    (proj / "target.txt").write_text("x")
    return proj


class TestTccHelper:
    def test_empty_off_macos(self, tmp_path):
        with patch.object(platform_compat, "IS_MACOS", False):
            assert platform_compat.tcc_protected_dirs_for_walk(tmp_path) == frozenset()

    def test_home_root_returns_protected_set(self, tmp_path):
        with (
            patch.object(platform_compat, "IS_MACOS", True),
            patch.dict(os.environ, _home_env(tmp_path), clear=False),
        ):
            got = platform_compat.tcc_protected_dirs_for_walk(tmp_path)
        assert {"Downloads", "Documents", "Desktop"} <= got

    def test_scoped_subdir_is_not_pruned(self, tmp_path):
        """An explicitly-scoped root under home keeps full visibility."""
        scoped = tmp_path / "Downloads"
        scoped.mkdir()
        with (
            patch.object(platform_compat, "IS_MACOS", True),
            patch.dict(os.environ, _home_env(tmp_path), clear=False),
        ):
            assert platform_compat.tcc_protected_dirs_for_walk(scoped) == frozenset()

    def test_unresolvable_root_prunes_nothing(self):
        with (
            patch.object(platform_compat, "IS_MACOS", True),
            patch.object(platform_compat.os.path, "realpath", side_effect=OSError),
        ):
            assert platform_compat.tcc_protected_dirs_for_walk("/nope") == frozenset()

    def test_null_byte_path_does_not_raise(self):
        """A NUL byte makes realpath raise ValueError — NOT an OSError subclass.

        Left uncaught it escapes the walk and 500s /api/file-search, so the
        helper must swallow it and simply prune nothing.
        """
        with patch.object(platform_compat, "IS_MACOS", True):
            assert platform_compat.tcc_protected_dirs_for_walk("/tmp/a\x00b") == frozenset()

    def test_library_is_not_pruned(self):
        """``~/Library`` is not TCC-gated, and holds CloudStorage project mounts.

        Pruning it would remove zero prompts while hiding OneDrive / Google
        Drive / iCloud project trees from search.
        """
        assert "Library" not in platform_compat.TCC_PROTECTED_HOME_DIRS


class TestFileIndexPruning:
    @pytest.mark.asyncio
    async def test_home_root_skips_tcc_dirs(self, tmp_path):
        _make_home(tmp_path)
        with (
            patch.object(platform_compat, "IS_MACOS", True),
            patch.dict(os.environ, _home_env(tmp_path), clear=False),
        ):
            idx = FileIndex(str(tmp_path))
            entries, _ = await __import__("asyncio").to_thread(idx._walk)
        walked = {os.path.relpath(e[0], tmp_path).split(os.sep)[0] for e in entries}
        for name in _TCC_DIRS:
            assert name not in walked, f"{name} must not be walked from a $HOME root"
        assert "code" in walked, "non-TCC project dirs must still be indexed"

    @pytest.mark.asyncio
    async def test_scoped_tcc_dir_is_still_indexed(self, tmp_path):
        """Indexing ~/Downloads directly is deliberate — index it fully."""
        _make_home(tmp_path)
        scoped = tmp_path / "Downloads"
        with (
            patch.object(platform_compat, "IS_MACOS", True),
            patch.dict(os.environ, _home_env(tmp_path), clear=False),
        ):
            idx = FileIndex(str(scoped))
            entries, _ = await __import__("asyncio").to_thread(idx._walk)
        assert [e for e in entries if e[1] == "target.txt"]

    @pytest.mark.asyncio
    async def test_nested_dir_sharing_a_tcc_name_is_not_pruned(self, tmp_path):
        """Only the TOP level of $HOME is pruned.

        A project's own ``Documents/`` subdirectory is not TCC-gated (TCC covers
        ``~/Documents`` specifically), so a name-only match deeper in the tree
        must stay indexed — otherwise the prune silently hides real project files.
        """
        nested = tmp_path / "code" / "Documents"
        nested.mkdir(parents=True)
        (nested / "deep.txt").write_text("x")
        (tmp_path / "Downloads").mkdir()
        (tmp_path / "Downloads" / "top.txt").write_text("x")
        with (
            patch.object(platform_compat, "IS_MACOS", True),
            patch.dict(os.environ, _home_env(tmp_path), clear=False),
        ):
            idx = FileIndex(str(tmp_path))
            entries, _ = await __import__("asyncio").to_thread(idx._walk)
        rels = {os.path.relpath(e[0], tmp_path) for e in entries}
        assert os.path.join("code", "Documents", "deep.txt") in rels
        assert not any(r.startswith("Downloads") for r in rels)


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-search", api_file_search)
    state = MagicMock()
    state.file_indexes.get.return_value = None  # force the walk fallback
    app["state"] = state
    return app


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


class TestFileSearchPruning:
    @pytest.mark.asyncio
    async def test_home_fallback_skips_tcc_dirs(self, tmp_path, mock_sel):
        _make_home(tmp_path)
        with (
            patch.object(platform_compat, "IS_MACOS", True),
            patch.dict(
                os.environ, {**_home_env(tmp_path), "KIROCREW_PROJECT_DIR": ""}, clear=False
            ),
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/file-search?q=target")
                paths = [r["path"] for r in (await resp.json())["results"]]
        for name in _TCC_DIRS:
            assert not any(
                f"{os.sep}{name}{os.sep}" in p for p in paths
            ), f"{name} must not be searched from the bare $HOME fallback"
        assert any(f"{os.sep}code{os.sep}" in p for p in paths)

    @pytest.mark.asyncio
    async def test_explicit_project_under_tcc_dir_still_searchable(self, tmp_path, mock_sel):
        """A project the user pointed at is scoped — never pruned."""
        _make_home(tmp_path)
        scoped = tmp_path / "Downloads"
        with (
            patch.object(platform_compat, "IS_MACOS", True),
            patch.dict(os.environ, _home_env(tmp_path), clear=False),
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/file-search?q=target&project={scoped}")
                names = {r["name"] for r in (await resp.json())["results"]}
        assert "target.txt" in names

    @pytest.mark.asyncio
    async def test_explicit_home_as_project_is_not_pruned(self, tmp_path, mock_sel):
        """Naming $HOME itself as the project is deliberate — search it in full.

        Only the *unscoped* fallback prunes. Otherwise a user who explicitly
        picked their home directory would silently lose Downloads/Documents
        results, which is the opposite of the documented contract.
        """
        _make_home(tmp_path)
        with (
            patch.object(platform_compat, "IS_MACOS", True),
            patch.dict(os.environ, _home_env(tmp_path), clear=False),
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/file-search?q=target&project={tmp_path}")
                paths = [r["path"] for r in (await resp.json())["results"]]
        assert any(f"{os.sep}Downloads{os.sep}" in p for p in paths)
