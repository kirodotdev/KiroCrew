"""The folder ingest's committed group is recorded once, and never over a verdict.

The commit callback (``on_committed``) writes the 'done' row inside the
pipeline's finalize hop. From then on the row is visible to the dedup sweep,
which may collapse the group and mark the row 'deduped' before the scan's own
terminal write runs. That later write must not restore the captured ids over
the sweep's verdict. And a callback write that loses the writer lock once is
retried under ``BEGIN IMMEDIATE`` so the row does not stay 'scanning'.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.store import KnowledgeStore


def _row(store: KnowledgeStore, source_id: str, file_path: str) -> dict | None:
    row = store.db.execute(
        "SELECT status, item_ids FROM folder_file_state WHERE source_id = ? AND file_path = ?",
        (source_id, file_path),
    ).fetchone()
    return dict(row) if row else None


@pytest.mark.asyncio
async def test_scan_does_not_restore_a_group_the_sweep_collapsed(tmp_path):
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        folder = tmp_path / "folder"
        folder.mkdir()
        doc = folder / "doc.md"
        doc.write_text("body", encoding="utf-8")
        source_id = store.add_source("folder", "local_folder", str(folder))
        other_id = store.add_source("other", "local_folder", str(tmp_path / "other"))
        committed: list[str] = []

        async def ingest_file(path, *, on_committed=None, **kwargs):
            def _commit_then_sweep() -> None:
                item_id = store.add_item("T", "body", "document", source_id=source_id)
                committed.append(item_id)
                if on_committed is not None:
                    on_committed([item_id])
                # A dedup sweep running after the callback's write: it sees the
                # 'done' row, deletes the loser's items and marks the row.
                store.delete_items_batch([item_id])
                store.db.execute(
                    "UPDATE folder_file_state SET status = 'deduped', item_ids = '[]', "
                    "merged_into_source_id = ? WHERE source_id = ? AND file_path = ?",
                    (other_id, source_id, str(doc)),
                )
                store.db.commit()

            await asyncio.to_thread(_commit_then_sweep)
            return "job-1"

        pipeline = MagicMock()
        pipeline._dedup_enabled = False
        pipeline.ingest_file = ingest_file
        watcher = FolderWatcher(store, pipeline)

        await watcher.scan_source(
            {"id": source_id, "uri": str(folder), "source_type": "local_folder", "properties": "{}"}
        )

        row = _row(store, source_id, str(doc))
        assert row is not None and committed
        assert (
            row["status"] == "deduped"
        ), f"the scan's terminal write replaced the sweep's verdict with {row['status']!r}"
        assert (
            json.loads(row["item_ids"] or "[]") == []
        ), f"the row names {row['item_ids']}, items the sweep already deleted"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_callback_write_retries_once_under_the_writer_lock(tmp_path):
    attempts = {"update": 0}

    class _FlakyConn:
        """Real connection; the first folder_file_state UPDATE hits a lock."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args):
            if sql.lstrip().startswith("UPDATE folder_file_state"):
                attempts["update"] += 1
                if attempts["update"] == 1:
                    raise sqlite3.OperationalError("database is locked")
            return self._real.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._real, name)

    class _Store:
        def __init__(self, real):
            self._real = real

        @property
        def db(self):
            return _FlakyConn(self._real.db)

        def __getattr__(self, name):
            return getattr(self._real, name)

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        doc = tmp_path / "doc.md"
        doc.write_text("body", encoding="utf-8")
        source_id = store.add_source("folder", "local_folder", str(tmp_path))
        seen: dict = {}

        async def ingest_file(path, *, on_committed=None, **kwargs):
            def _commit() -> None:
                item_id = store.add_item("T", "body", "document", source_id=source_id)
                seen["item_id"] = item_id
                if on_committed is not None:
                    on_committed([item_id])
                seen["row"] = _row(store, source_id, str(doc))

            await asyncio.to_thread(_commit)
            return "job-1"

        pipeline = MagicMock()
        pipeline.ingest_file = ingest_file
        watcher = FolderWatcher(_Store(store), pipeline)
        watcher._update_state(
            source_id, str(doc), "hash", 1.0, "[]", "2026-01-01T00:00:00", "scanning", attempts=1
        )

        item_ids, outcome = await watcher._ingest_file(str(doc), source_id, "default", {}, [])

        assert outcome == "done" and item_ids == [seen["item_id"]]
        assert attempts["update"] == 2, "the failed callback write was not retried"
        row = seen["row"]
        assert (
            row is not None and row["status"] == "done"
        ), f"after one lock failure the callback left status={row and row['status']!r}"
        assert json.loads(row["item_ids"]) == [seen["item_id"]]
    finally:
        store.close()


def test_settle_done_writes_only_over_the_scanning_marker(tmp_path):
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        source_id = store.add_source("folder", "local_folder", str(tmp_path))
        watcher = FolderWatcher(store, MagicMock())
        path = str(tmp_path / "doc.md")
        item_id = store.add_item("T", "body", "document", source_id=source_id)

        # The callback's write did not land: the marker is still there.
        watcher._update_state(source_id, path, "h", 1.0, "[]", "now", "scanning", attempts=1)
        assert watcher._settle_done(source_id, path, "h", 1.0, [item_id], "now", None) is True
        assert _row(store, source_id, path) == {"status": "done", "item_ids": json.dumps([item_id])}

        # Someone else settled the row: it stays as they left it.
        watcher._update_state(source_id, path, "h", 1.0, "[]", "now", "deduped")
        assert watcher._settle_done(source_id, path, "h", 1.0, [item_id], "now", None) is False
        assert _row(store, source_id, path) == {"status": "deduped", "item_ids": "[]"}
    finally:
        store.close()
