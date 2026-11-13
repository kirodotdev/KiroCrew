"""A failed folder ingest is charged for the chunks it spent."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.mark.asyncio
async def test_failed_ingests_count_against_the_chunk_budget(tmp_path: Path, opened) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    for name in ("a.md", "b.md"):
        (vault / name).write_text(f"body of {name}", encoding="utf-8")

    store = opened(KnowledgeStore(tmp_path / "knowledge.db"))
    reached: list[str] = []

    async def _extract_then_fail(path, **kw):
        # Extracts every chunk, then commits nothing: the partial-failure shape.
        reached.append(path)
        progress = kw.get("on_progress")
        for done in (1, 2, 3):
            if progress:
                progress("extracting", done, 3)
        return "job"

    pipeline = MagicMock()
    pipeline._dedup_enabled = False
    pipeline.ingest_file = _extract_then_fail
    fw = FolderWatcher(store, pipeline)
    source_id = store.add_source("t", "local_folder", str(vault), properties={})
    source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}

    stats = await fw.scan_source(source, chunk_budget=3)

    assert stats["failed"] == 1
    assert len(reached) == 1, "the budget the first file spent stops the sweep"
    assert stats["budget_reached"] == 1
    assert stats["chunks_ingested"] == 3
