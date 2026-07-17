"""Tests for FileIndex and FileIndexRegistry."""

from __future__ import annotations

import os

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
        await idx.start()
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
        await idx.start()
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
        await idx.start()
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
        await idx.start()
        try:
            results = idx.search("match", _scorer, max_results=5)
            assert len(results) == 5
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_refresh(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        idx = FileIndex(str(tmp_path))
        await idx.start()
        assert idx._task is not None
        idx.stop()
        assert idx._task is None


class TestFileIndexRegistry:
    @pytest.mark.asyncio
    async def test_acquire_creates_index(self, tmp_path):
        (tmp_path / "test.py").write_text("x")
        reg = FileIndexRegistry()
        try:
            idx = await reg.acquire(str(tmp_path))
            assert idx.is_ready
            assert idx.entry_count >= 1
        finally:
            reg.stop_all()

    @pytest.mark.asyncio
    async def test_acquire_same_root_shares_index(self, tmp_path):
        (tmp_path / "test.py").write_text("x")
        reg = FileIndexRegistry()
        try:
            idx1 = await reg.acquire(str(tmp_path))
            idx2 = await reg.acquire(str(tmp_path))
            assert idx1 is idx2
        finally:
            reg.stop_all()

    @pytest.mark.asyncio
    async def test_release_stops_index_at_zero_refcount(self, tmp_path):
        (tmp_path / "test.py").write_text("x")
        reg = FileIndexRegistry()
        idx = await reg.acquire(str(tmp_path))
        root = os.path.realpath(str(tmp_path))
        assert reg.get(root) is idx
        await reg.release(root)
        assert reg.get(root) is None

    @pytest.mark.asyncio
    async def test_release_keeps_index_with_remaining_refs(self, tmp_path):
        (tmp_path / "test.py").write_text("x")
        reg = FileIndexRegistry()
        try:
            await reg.acquire(str(tmp_path))
            await reg.acquire(str(tmp_path))  # refcount = 2
            root = os.path.realpath(str(tmp_path))
            await reg.release(root)  # refcount = 1
            assert reg.get(root) is not None
        finally:
            reg.stop_all()

    @pytest.mark.asyncio
    async def test_stop_all_clears_everything(self, tmp_path):
        (tmp_path / "test.py").write_text("x")
        reg = FileIndexRegistry()
        await reg.acquire(str(tmp_path))
        reg.stop_all()
        assert reg.get(os.path.realpath(str(tmp_path))) is None

    @pytest.mark.asyncio
    async def test_acquire_failure_cleans_up(self, tmp_path, monkeypatch):
        """Failed start() must not leave orphan entries in registry."""
        reg = FileIndexRegistry()
        from kiro_crew.dashboard import file_index as fi_mod

        async def _fail_start(self):
            raise RuntimeError("boom")

        monkeypatch.setattr(fi_mod.FileIndex, "start", _fail_start)
        with pytest.raises(RuntimeError, match="boom"):
            await reg.acquire(str(tmp_path))
        root = os.path.realpath(str(tmp_path))
        assert reg.get(root) is None
        assert reg._refcounts.get(root) is None

    @pytest.mark.asyncio
    async def test_release_on_never_acquired_root_is_safe(self, tmp_path):
        """release() on a root that was never acquire()'d must not corrupt state."""
        reg = FileIndexRegistry()
        (tmp_path / "a.py").write_text("x")
        try:
            idx = await reg.acquire(str(tmp_path))
            root = os.path.realpath(str(tmp_path))
            other = os.path.realpath(str(tmp_path / "nonexistent"))
            # Release a root that was never acquired
            await reg.release(other)
            # Original index must be unaffected
            assert reg.get(root) is idx
            assert reg._refcounts[root] == 1
        finally:
            reg.stop_all()

    @pytest.mark.asyncio
    async def test_slot_delete_releases_index(self, tmp_path):
        """Simulates slot delete path: release must decrement refcount to 0."""
        reg = FileIndexRegistry()
        (tmp_path / "a.py").write_text("x")
        root = os.path.realpath(str(tmp_path))
        await reg.acquire(root)
        assert reg.get(root) is not None
        # Simulate what api_chat_slot_delete now does
        await reg.release(root)
        assert reg.get(root) is None


class TestFileIndexTruncation:
    @pytest.mark.asyncio
    async def test_walk_truncates_at_max_entries(self, tmp_path, monkeypatch):
        """Index must set truncated=True when entry cap is hit."""
        from kiro_crew.dashboard import file_index as fi_mod

        monkeypatch.setattr(fi_mod, "_MAX_ENTRIES", 3)
        for i in range(10):
            (tmp_path / f"file_{i}.py").write_text("x")
        idx = FileIndex(str(tmp_path))
        await idx.start()
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
        await idx.start()
        try:
            assert not idx.truncated
        finally:
            idx.stop()
