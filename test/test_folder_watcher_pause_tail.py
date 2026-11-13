"""A scan the user pauses mid-way still accounts for the files it ingested."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.knowledge import folder_watcher
from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.mark.asyncio
async def test_a_pause_mid_scan_reports_chunks_and_dedups_ingested_files(
    tmp_path: Path, opened, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    for name in ("a.md", "b.md"):
        (vault / name).write_text(f"body of {name}", encoding="utf-8")

    store = opened(KnowledgeStore(tmp_path / "knowledge.db"))
    pipeline = MagicMock()
    pipeline._dedup_enabled = True
    fw = FolderWatcher(store, pipeline)
    source_id = store.add_source("t", "local_folder", str(vault), properties={})
    source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}

    async def _ingest(file_path, *_a, **_kw):
        return [f"{Path(file_path).name}-{i}" for i in range(3)], "done"

    # The pause lands at the second file's re-check: one file is in.
    checks = iter([False, True])
    monkeypatch.setattr(folder_watcher, "_PAUSE_RECHECK_FILES", 1)
    monkeypatch.setattr(fw, "_is_paused", lambda _sid: next(checks))
    monkeypatch.setattr(fw, "_ingest_file", _ingest)
    deduped: list[list[str]] = []
    monkeypatch.setattr(fw, "_dedup_ingested", lambda _sid, paths: deduped.append(list(paths)))

    stats = await fw.scan_source(source, chunk_budget=100)

    assert stats["status"] == "paused"
    assert stats["new"] == 1
    assert stats["chunks_ingested"] == 3, "the sweep charges the shared budget from this key"
    assert (
        len(deduped) == 1 and len(deduped[0]) == 1
    ), "the file ingested before the pause still gets its targeted dedup"
