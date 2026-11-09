"""A folder scan must not treat a file it could not SEE as a file that was deleted.

``os.walk`` skips a directory it cannot list, and the walk skips a file it cannot
``stat``. Either can be transient (a permission change, a network mount that drops
out mid-walk), and a scan that read the gap as a deletion removed the indexed
items, which only a fresh, paid re-ingest brings back.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.knowledge import folder_watcher
from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture()
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "sub").mkdir(parents=True)
    (root / "note1.md").write_text("# One\nfirst note")
    (root / "sub" / "deep.md").write_text("# Deep\nnested note")
    return root


async def _indexed(store, vault):
    """A watcher whose first scan has indexed every file, and its source."""
    pipeline = MagicMock()
    fw = FolderWatcher(store, pipeline)
    source_id = store.add_source("test", "local_folder", str(vault))
    source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}

    async def fake_ingest(path, *, on_committed=None, **kwargs):
        item_id = store.add_item("title", "content", "doc", source_id=source_id)
        if on_committed is not None:
            on_committed([item_id])
        return "job1"

    pipeline.ingest_file = AsyncMock(side_effect=fake_ingest)
    first = await fw.scan_source(source)
    assert first["new"] == 2
    return fw, source


@pytest.mark.asyncio
async def test_a_directory_that_fails_to_list_keeps_its_files(store, vault, monkeypatch):
    fw, source = await _indexed(store, vault)
    real_walk = os.walk
    sub = os.path.join(str(vault), "sub")

    def walk_without_sub(top, onerror=None, **kwargs):
        # The listing of ``sub`` fails, as os.walk reports it: through onerror,
        # with the directory left out of the walk.
        for dirpath, dirnames, filenames in real_walk(top, **kwargs):
            if dirpath == sub:
                continue
            if "sub" in dirnames and dirpath == str(vault):
                dirnames.remove("sub")
                if onerror is not None:
                    onerror(PermissionError(13, "Permission denied", sub))
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(folder_watcher.os, "walk", walk_without_sub)
    stats = await fw.scan_source(source)

    assert stats["deleted"] == 0
    kept = {
        r["file_path"]
        for r in store.db.execute(
            "SELECT file_path FROM folder_file_state WHERE source_id = ?", (source["id"],)
        )
    }
    assert os.path.join(sub, "deep.md") in kept


@pytest.mark.asyncio
async def test_a_file_that_fails_to_stat_keeps_its_items(store, vault, monkeypatch):
    fw, source = await _indexed(store, vault)
    real_stat = os.stat
    target = os.path.join(str(vault), "note1.md")

    def stat_failing_on_target(path, *args, **kwargs):
        if os.fspath(path) == target:
            raise PermissionError(13, "Permission denied", target)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(folder_watcher.os, "stat", stat_failing_on_target)
    stats = await fw.scan_source(source)

    assert stats["deleted"] == 0
    kept = {
        r["file_path"]
        for r in store.db.execute(
            "SELECT file_path FROM folder_file_state WHERE source_id = ?", (source["id"],)
        )
    }
    assert target in kept


@pytest.mark.asyncio
async def test_an_unseen_include_target_keeps_the_includer_text(store, vault, monkeypatch):
    fw, source = await _indexed(store, vault)
    includer = os.path.join(str(vault), "note1.md")
    target = os.path.join(str(vault), "sub", "deep.md")
    fw._write_include_record(source["id"], includer, {os.path.normcase(os.path.realpath(target))})
    store.db.commit()
    real_stat = os.stat

    def stat_failing_on_target(path, *args, **kwargs):
        if os.fspath(path) == target:
            raise PermissionError(13, "Permission denied", target)
        return real_stat(path, *args, **kwargs)

    def includer_items():
        row = store.db.execute(
            "SELECT item_ids FROM folder_file_state WHERE source_id = ? AND file_path = ?",
            (source["id"], includer),
        ).fetchone()
        return json.loads(row["item_ids"])

    before = includer_items()
    ingests = fw.pipeline.ingest_file.await_count
    monkeypatch.setattr(folder_watcher.os, "stat", stat_failing_on_target)
    await fw.scan_source(source)

    # Neither retired nor paid for again: the same items, and no new ingest.
    assert includer_items() == before
    assert fw.pipeline.ingest_file.await_count == ingests


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
@pytest.mark.asyncio
async def test_a_dangling_symlink_is_removed(store, tmp_path):
    root = tmp_path / "linked"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Out\nlinked note")
    (root / "link.md").symlink_to(outside)
    (root / "plain.md").write_text("# Plain\nplain note")
    fw, source = await _indexed(store, root)
    outside.unlink()

    stats = await fw.scan_source(source)

    assert stats["deleted"] == 1


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
@pytest.mark.asyncio
async def test_a_symlink_whose_target_directory_became_a_file_is_removed(store, tmp_path):
    # stat answers ENOTDIR, not ENOENT, when a directory on the target's path is
    # now a regular file; the target is just as gone.
    root = tmp_path / "linked"
    root.mkdir()
    target_dir = tmp_path / "targets"
    target_dir.mkdir()
    (target_dir / "out.md").write_text("# Out\nlinked note")
    (root / "link.md").symlink_to(target_dir / "out.md")
    (root / "plain.md").write_text("# Plain\nplain note")
    fw, source = await _indexed(store, root)
    (target_dir / "out.md").unlink()
    target_dir.rmdir()
    target_dir.write_text("now a file")

    stats = await fw.scan_source(source)

    assert stats["deleted"] == 1


@pytest.mark.asyncio
async def test_a_really_deleted_file_is_still_removed(store, vault):
    fw, source = await _indexed(store, vault)
    (vault / "sub" / "deep.md").unlink()

    stats = await fw.scan_source(source)

    assert stats["deleted"] == 1
