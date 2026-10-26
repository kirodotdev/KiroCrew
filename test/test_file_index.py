"""Tests for FileIndex and FileIndexRegistry."""

from __future__ import annotations

import os
import time

import pytest

from kiro_crew.dashboard.file_index import FileIndex, FileIndexRegistry


def _populate(tmp_path):
    """Create a small file tree for index tests."""
    (tmp_path / "hello.py").write_text("x")
    (tmp_path / "hello_world.py").write_text("xx")
    (tmp_path / "readme.md").write_text("# hi")
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "hello_util.py").write_text("y")
    (sub / ".secret").write_text("s")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "hello_dep.py").write_text("z")
    git = tmp_path / ".git"
    git.mkdir()
    (git / "hello_obj").write_text("g")


def _scorer(q, name, rel):
    """Simple substring scorer for tests."""
    nl = name.lower()
    if q in nl:
        return 10.0
    if q in rel.lower():
        return 5.0
    return 0.0


class TestFileIndex:
    @pytest.mark.asyncio
    async def test_index_builds_and_searches(self, tmp_path):
        _populate(tmp_path)
        idx = FileIndex(str(tmp_path))
        await idx.build()
        try:
            assert idx.is_ready
            assert idx.entry_count >= 3  # hello.py, hello_world.py, readme.md, src/hello_util.py
            results = idx.search("hello", _scorer)
            names = {r["name"] for r in results}
            assert "hello.py" in names
            assert "hello_world.py" in names
            assert "hello_util.py" in names
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_index_excludes_hidden_and_skip_dirs(self, tmp_path):
        _populate(tmp_path)
        idx = FileIndex(str(tmp_path))
        await idx.build()
        try:
            all_names = {e[1] for e in idx._entries}  # name is at index 1
            assert ".secret" not in all_names
            assert "hello_dep.py" not in all_names  # node_modules
            assert "hello_obj" not in all_names  # .git
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_search_returns_empty_for_no_match(self, tmp_path):
        _populate(tmp_path)
        idx = FileIndex(str(tmp_path))
        await idx.build()
        try:
            results = idx.search("zzzzz", _scorer)
            assert results == []
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_search_respects_max_results(self, tmp_path):
        for i in range(20):
            (tmp_path / f"match_{i:02d}.txt").write_text("x")
        idx = FileIndex(str(tmp_path))
        await idx.build()
        try:
            results = idx.search("match", _scorer, max_results=5)
            assert len(results) == 5
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_maybe_refresh_rebuilds_when_stale(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        idx = FileIndex(str(tmp_path))
        await idx.build()
        try:
            from kiro_crew.dashboard import file_index as fi_mod

            # Fresh (built just now): no rebuild is scheduled.
            idx._built_at = time.monotonic()
            idx.maybe_refresh()
            assert idx._task is None
            # Age the index past the TTL by backdating its build stamp, add a
            # file, and a rebuild is scheduled and completes. Driving staleness
            # through the index's own stamp keeps the fake out of the stdlib
            # clock, which every thread and the event loop share.
            idx._built_at = time.monotonic() - (fi_mod._REFRESH_TTL_SECS + 1)
            (tmp_path / "b.py").write_text("y")
            idx.maybe_refresh()
            assert idx._task is not None
            await idx._task
            names = {e[1] for e in idx._entries}
            assert "b.py" in names
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_truncated_index_is_not_refreshed(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        idx = FileIndex(str(tmp_path))
        await idx.build()
        try:
            idx._truncated = True
            idx._built_at = time.monotonic() - 10_000
            idx.maybe_refresh()
            assert idx._task is None  # the search endpoint never uses it
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_inflight_rebuild(self, tmp_path):
        import asyncio
        import contextlib

        (tmp_path / "a.py").write_text("x")
        idx = FileIndex(str(tmp_path))
        await idx.build()
        idx._built_at = time.monotonic() - 10_000
        idx.maybe_refresh()
        task = idx._task
        assert task is not None
        idx.stop()
        assert idx._task is None
        # Let the cancelled task settle so its executor job does not outlive the
        # test on the shared path-transfer pool.
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestFileIndexRegistry:
    @pytest.mark.asyncio
    async def test_ensure_builds_in_background_first_call_returns_none(self, tmp_path):
        (tmp_path / "test.py").write_text("x")
        reg = FileIndexRegistry()
        root = os.path.realpath(str(tmp_path))
        try:
            # First call starts the build and returns None (caller walks meanwhile).
            assert await reg.ensure(str(tmp_path)) is None
            idx = reg._indexes[root]
            assert idx is not None and idx._task is not None
            await idx._task
            # Once ready, ensure returns the index.
            assert await reg.ensure(str(tmp_path)) is idx
            assert idx.is_ready
            assert idx.entry_count >= 1
        finally:
            reg.stop_all()

    @pytest.mark.asyncio
    async def test_ensure_same_root_shares_index(self, tmp_path):
        (tmp_path / "test.py").write_text("x")
        reg = FileIndexRegistry()
        root = os.path.realpath(str(tmp_path))
        try:
            await reg.ensure(str(tmp_path))
            idx = reg._indexes[root]
            await idx._task
            # A second ensure for the same root returns the same instance.
            assert await reg.ensure(str(tmp_path)) is idx
            assert reg._indexes[root] is idx
        finally:
            reg.stop_all()

    @pytest.mark.asyncio
    async def test_ensure_triggers_ttl_refresh_once_ready(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        reg = FileIndexRegistry()
        root = os.path.realpath(str(tmp_path))
        from kiro_crew.dashboard import file_index as fi_mod

        try:
            await reg.ensure(str(tmp_path))
            idx = reg._indexes[root]
            await idx._task
            # Backdate the build stamp past the TTL, add a file, ensure() again:
            # a refresh runs. Driving staleness through the index's own stamp
            # keeps the fake out of the shared stdlib clock.
            idx._built_at = time.monotonic() - (fi_mod._REFRESH_TTL_SECS + 1)
            (tmp_path / "b.py").write_text("y")
            assert await reg.ensure(str(tmp_path)) is idx
            assert idx._task is not None
            await idx._task
            assert "b.py" in {e[1] for e in idx._entries}
        finally:
            reg.stop_all()

    @pytest.mark.asyncio
    async def test_stop_all_clears_everything(self, tmp_path):
        (tmp_path / "test.py").write_text("x")
        reg = FileIndexRegistry()
        await reg.ensure(str(tmp_path))
        reg.stop_all()
        assert reg._indexes == {}

    @pytest.mark.asyncio
    async def test_ensure_failed_build_marks_walk_only_and_does_not_retry(
        self, tmp_path, monkeypatch
    ):
        """A failed or timed-out build marks the root walk-only, serves from the
        walk, and never retries -- so a slow/dead mount cannot pile abandoned
        walks onto the shared pool with one more per search."""
        reg = FileIndexRegistry()
        root = os.path.realpath(str(tmp_path))
        from kiro_crew.dashboard import file_index as fi_mod

        calls = {"n": 0}

        async def _fail_rebuild(self):
            calls["n"] += 1
            raise TimeoutError("walk exceeded the ceiling")

        monkeypatch.setattr(fi_mod.FileIndex, "_rebuild", _fail_rebuild)
        try:
            # First ensure starts the build and returns None (caller walks).
            assert await reg.ensure(str(tmp_path)) is None
            idx = reg._indexes[root]
            await idx._task  # _refresh_once swallows the error and sets walk-only
            assert idx._walk_only is True
            assert idx.usable is False
            # Backdate so the TTL alone would allow a refresh; walk-only must
            # still suppress it, and repeated ensure() must schedule no new walk.
            idx._built_at = time.monotonic() - (fi_mod._REFRESH_TTL_SECS + 1)
            assert await reg.ensure(str(tmp_path)) is None
            assert await reg.ensure(str(tmp_path)) is None
            # The finished task reference may remain, but no NEW walk was
            # scheduled: the rebuild ran exactly once.
            assert idx._task is None or idx._task.done()
            assert calls["n"] == 1, "the walk ran exactly once; no retry storm"
        finally:
            reg.stop_all()

    @pytest.mark.asyncio
    async def test_ensure_evicts_least_recently_searched_past_cap(self, tmp_path, monkeypatch):
        """The registry holds at most _MAX_CACHED_ROOTS indexes, LRU-evicting."""
        from kiro_crew.dashboard import file_index as fi_mod

        monkeypatch.setattr(fi_mod, "_MAX_CACHED_ROOTS", 2)
        reg = FileIndexRegistry()
        roots = []
        for name in ("a", "b", "c"):
            d = tmp_path / name
            d.mkdir()
            (d / "f.py").write_text("x")
            roots.append(os.path.realpath(str(d)))
        try:
            # Build a and b, then touch a so b is the least-recently-searched.
            await reg.ensure(roots[0])
            a_idx = reg._indexes[roots[0]]
            await reg.ensure(roots[1])
            b_idx = reg._indexes[roots[1]]
            await reg.ensure(roots[0])  # a is now most-recently-searched
            # Adding c evicts b (the LRU), keeping the cache at 2.
            await reg.ensure(roots[2])
            c_idx = reg._indexes[roots[2]]
            assert set(reg._indexes) == {roots[0], roots[2]}
            assert roots[1] not in reg._indexes
            # Drain every scheduled build (b's was cancelled by eviction) so none
            # outlives the test on the shared path-transfer pool.
            import asyncio
            import contextlib

            for idx in (a_idx, b_idx, c_idx):
                if idx._task is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await idx._task
        finally:
            reg.stop_all()


class TestFileIndexTruncation:
    @pytest.mark.asyncio
    async def test_walk_truncates_at_max_entries(self, tmp_path, monkeypatch):
        """Index must set truncated=True when entry cap is hit."""
        from kiro_crew.dashboard import file_index as fi_mod

        monkeypatch.setattr(fi_mod, "_MAX_ENTRIES", 3)
        for i in range(10):
            (tmp_path / f"file_{i}.py").write_text("x")
        idx = FileIndex(str(tmp_path))
        await idx.build()
        try:
            assert idx.truncated
            assert idx.entry_count == 3
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_not_truncated_under_cap(self, tmp_path):
        """Index must not be truncated for small projects."""
        (tmp_path / "a.py").write_text("x")
        idx = FileIndex(str(tmp_path))
        await idx.build()
        try:
            assert not idx.truncated
        finally:
            idx.stop()
