"""The folder scan must take no knowledge-store connection on the event loop.

``KnowledgeStore.db`` is a per-thread autocommit sqlite connection with a 10s
``busy_timeout``. Taken on the event loop, a query that waits on the writer lock
(a concurrent ``import_bundle``, a dedup collapse) blocks every task -- the
watchdog heartbeat included -- for that whole wait, and past
``dashboard.loop_stall_exit_after_secs`` the watchdog kills the gateway.

The store's accessor carries an on-loop guard for exactly this
(``kiro_crew.on_loop_db``). In production it only WARNS, so a regression here is
one log line an operator never reads. These tests arm the store's strict switch,
which turns the warning into ``OnLoopStoreError``, and then drive a scan through
every state transition the watcher persists (new -> done, unchanged, changed,
deleted, failed, paused). Any ``folder_file_state`` read or write that slipped
back onto the loop raises.

This is the behavioral half of the ``test_knowledge_ingest_scan_off_loop`` AST
ratchets: those see a query only where it is written lexically inside an
``async def``, and every call in the scan path is one frame down inside a sync
helper -- the interprocedural gap the runtime guard exists to close.

The tests' OWN store access goes through :func:`off` for the same reason: under
the armed guard a test-side ``store.db`` on the loop would raise and be
indistinguishable from a watcher regression.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.on_loop_db import STORE_STRICT_ENV, OnLoopStoreError

pytestmark = pytest.mark.asyncio


async def off(fn, *args):
    """Run a test-side store access on a worker thread."""
    return await asyncio.to_thread(fn, *args)


@pytest.fixture()
def strict_store(monkeypatch, tmp_path):
    """A store whose on-loop guard RAISES. Built and closed off-loop (sync fixture)."""
    monkeypatch.setenv(STORE_STRICT_ENV, "1")
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield store
    store.close()


def _pipeline(store: KnowledgeStore, *, fail: bool = False) -> IngestionPipeline:
    extractor = MagicMock()
    extractor._pool = None
    extractor.extract_batch = AsyncMock(
        return_value=[{"category": "document", "summary": "s", "entities": []}]
    )
    chunker = MagicMock()
    _one_chunk = lambda text, **kw: [  # noqa: E731
        {"content": text, "chunk_index": 0, "section_title": None, "line_start": 0, "line_end": 0}
    ]
    chunker.chunk.side_effect = _one_chunk
    chunker.chunk_markdown.side_effect = _one_chunk
    reader = MagicMock()
    if fail:
        reader.read.side_effect = RuntimeError("reader exploded")
    else:
        reader.read.return_value = ("some body", {})
    return IngestionPipeline(
        store=store, extractor=extractor, chunker=chunker, reader=reader, embedder=None
    )


def _add_source(store: KnowledgeStore, folder) -> dict:
    source_id = store.add_source("folder", "local_folder", str(folder))
    return {"id": source_id, "uri": str(folder), "source_type": "local_folder", "properties": "{}"}


def _rows(store: KnowledgeStore, source_id: str) -> dict[str, dict]:
    return {
        r["file_path"]: dict(r)
        for r in store.db.execute(
            "SELECT file_path, status, error_message FROM folder_file_state WHERE source_id = ?",
            (source_id,),
        ).fetchall()
    }


def _pause(store: KnowledgeStore, source_id: str) -> None:
    store.db.execute(
        "UPDATE sources SET properties = ? WHERE id = ?",
        (json.dumps({"scan_paused": True}), source_id),
    )
    store.db.commit()


async def test_strict_guard_is_actually_armed(strict_store):
    """An on-loop take must raise, or every test below passes vacuously."""
    with pytest.raises(OnLoopStoreError):
        strict_store.db
    # ...and the same take off-loop is the sanctioned path.
    assert await off(lambda: strict_store.db) is not None


async def test_full_scan_lifecycle_takes_no_connection_on_the_loop(strict_store, tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "a.md").write_text("alpha", encoding="utf-8")
    (folder / "b.md").write_text("beta", encoding="utf-8")
    source = await off(_add_source, strict_store, folder)
    watcher = FolderWatcher(strict_store, _pipeline(strict_store))

    # new -> done
    stats = await watcher.scan_source(source)
    assert stats["new"] == 2 and stats["failed"] == 0

    # unchanged: the last_seen batch path
    stats = await watcher.scan_source(source)
    assert stats["new"] == 0 and stats["changed"] == 0

    # changed: release_stale_claim + re-ingest
    (folder / "a.md").write_text("alpha, revised", encoding="utf-8")
    future = time.time() + 5
    os.utime(folder / "a.md", (future, future))
    stats = await watcher.scan_source(source)
    assert stats["changed"] == 1

    # deleted: _handle_deleted
    (folder / "b.md").unlink()
    stats = await watcher.scan_source(source)
    assert stats["deleted"] == 1

    rows = await off(_rows, strict_store, source["id"])
    assert set(rows) == {str(folder / "a.md")}
    assert rows[str(folder / "a.md")]["status"] == "done"


async def test_failed_ingest_records_its_reason_off_the_loop(strict_store, tmp_path):
    """The 'failed' branch reads the recorded reason back and re-writes the row.

    Both used to run on the loop, and the read feeds the write across what is
    now a single worker hop -- the reason must survive that move intact.
    """
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "bad.md").write_text("boom", encoding="utf-8")
    source = await off(_add_source, strict_store, folder)
    watcher = FolderWatcher(strict_store, _pipeline(strict_store, fail=True))

    stats = await watcher.scan_source(source)
    assert stats["failed"] == 1

    rows = await off(_rows, strict_store, source["id"])
    row = rows[str(folder / "bad.md")]
    assert row["status"] == "failed"
    assert "reader exploded" in (row["error_message"] or ""), (
        "the reason _ingest_file recorded was lost when the terminal 'failed' row "
        f"was re-written: {row['error_message']!r}"
    )


async def test_pause_check_takes_no_connection_on_the_loop(strict_store, tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "a.md").write_text("alpha", encoding="utf-8")
    source = await off(_add_source, strict_store, folder)
    await off(_pause, strict_store, source["id"])
    watcher = FolderWatcher(strict_store, _pipeline(strict_store))

    stats = await watcher.scan_source(source)
    assert stats.get("status") == "paused"
    assert stats["new"] == 0
