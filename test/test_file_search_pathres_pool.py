"""The file-search and listing walks must not queue on the ``mc-pathres`` pool.

``mc-pathres`` is a small FIFO pool (two children by default) sized for the
event loop's own bounded path checks. The per-project file index rebuilds every
30 seconds over up to 100k entries, and the ``/api/file-search`` fallback walk
and the browse listings visit every entry they offer. Each of those already
holds the entry's ``os.path.realpath`` on its own worker thread, so asking the
bounded ``is_sensitive_path`` gate about it submitted one more resolution per
entry to the shared pool: one rebuild or a few concurrent searches filled the
queue, and every latency-critical check on the loop -- including the next
search's own root check -- waited behind it. Identical requests then served in
anywhere from under a second to minutes, depending on what else was queued.

These tests pin the fix by COUNTING pool submissions, not by timing: every walk
asks the thread-aware canonical gate, which matches inline off the loop, so a
walk of any size submits nothing; and the verdicts are unchanged.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew import security
from kiro_crew.dashboard import file_index as file_index_mod
from kiro_crew.dashboard.file_index import FileIndex
from kiro_crew.dashboard.handlers import api_file_search
from kiro_crew.dashboard.handlers import files as files_mod

_ENTRIES = 40


def _tree(root) -> None:
    for i in range(_ENTRIES // 2):
        d = root / f"widget_dir_{i}"
        d.mkdir()
        (d / f"widget_{i}.py").write_text("x", encoding="utf-8")


@pytest.fixture()
def pool_submissions(monkeypatch):
    """Count every resolution submitted to ``mc-pathres``, letting each run."""
    calls: list[str] = []
    real = security.paths._run_resolution_bounded

    def counting(expanded, worker, **kwargs):
        calls.append(expanded)
        return real(expanded, worker, **kwargs)

    monkeypatch.setattr(security.paths, "_run_resolution_bounded", counting)
    # A cold anchor cache, so an anchor rebuild through the pool would count too.
    monkeypatch.setattr(security.paths, "_home_targets_cache", {})
    return calls


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-search", api_file_search)
    state = MagicMock()
    state.file_indexes.get.return_value = None
    state.owner_id = ""
    app["state"] = state
    return as_owner(app)


class TestTheWalksSubmitNothingToThePool:
    def test_control_the_bounded_gate_does_submit(self, tmp_path, pool_submissions) -> None:
        # If this stopped holding, the zero counts below would prove nothing.
        security.is_sensitive_path(os.path.realpath(tmp_path / "x.py"))
        assert pool_submissions

    def test_a_file_index_rebuild(self, tmp_path, pool_submissions) -> None:
        _tree(tmp_path)
        entries, truncated = FileIndex(str(tmp_path))._walk()
        assert len(entries) == _ENTRIES and not truncated
        assert pool_submissions == []

    @pytest.mark.asyncio
    async def test_the_file_search_fallback_walk(self, tmp_path, mock_sel) -> None:
        _tree(tmp_path)
        # The root's own check is one bounded call on the loop, by design: count
        # only what the walk adds, with the root check answered up front.
        real_root = os.path.realpath(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            calls: list[str] = []
            original = security.paths._run_resolution_bounded

            def counting(expanded, worker, **kwargs):
                calls.append(expanded)
                return original(expanded, worker, **kwargs)

            with patch.object(security.paths, "_run_resolution_bounded", counting):
                resp = await client.get(f"/api/file-search?q=widget&limit=50&project={real_root}")
                assert resp.status == 200
                results = (await resp.json())["results"]
        assert len(results) == _ENTRIES
        walked = [c for c in calls if c.startswith(real_root + os.sep)]
        assert walked == []

    def test_the_browse_listings(self, tmp_path, pool_submissions) -> None:
        _tree(tmp_path)
        (tmp_path / "notes.md").write_text("x", encoding="utf-8")
        dirs = files_mod._browse_dirs_sync(str(tmp_path), set())
        fdirs, ffiles = files_mod._browse_files_sync(str(tmp_path), set())
        assert len(dirs) == len(fdirs) == _ENTRIES // 2 and len(ffiles) == 1
        assert pool_submissions == []

    @pytest.mark.asyncio
    async def test_a_rebuild_off_the_loop_while_the_loop_runs(
        self, tmp_path, pool_submissions
    ) -> None:
        # The real shape: the rebuild runs on a worker thread while a loop is
        # live on another thread. The canonical gate decides by the CALLING
        # thread, so the worker still matches inline.
        _tree(tmp_path)
        entries, _ = await asyncio.to_thread(FileIndex(str(tmp_path))._walk)
        assert len(entries) == _ENTRIES
        assert pool_submissions == []


class TestTheVerdictsAreUnchanged:
    """Same fence, different submission path: a fenced real path is still dropped."""

    def test_the_index_drops_a_link_into_a_fenced_tree(self, tmp_path, monkeypatch) -> None:
        _tree(tmp_path)
        fenced = tmp_path / "widget_dir_0"
        os.symlink(str(fenced), str(tmp_path / "widget_link"))
        real = os.path.realpath(fenced)
        seen: list[str] = []

        def gate(resolved: str) -> bool:
            seen.append(resolved)
            return resolved == real or resolved.startswith(real + os.sep)

        monkeypatch.setattr(file_index_mod, "is_sensitive_canonical_path", gate)
        names = {e[1] for e in FileIndex(str(tmp_path))._walk()[0]}
        assert "widget_link" not in names and "widget_dir_0" not in names
        assert [p for p in seen if p != os.path.realpath(p)] == []

    def test_a_real_credential_path_is_still_refused_off_the_loop(self) -> None:
        real = os.path.realpath(os.path.expanduser("~/.aws/credentials"))
        assert security.is_sensitive_canonical_path(real) is True
