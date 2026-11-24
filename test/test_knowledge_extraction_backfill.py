"""A folder file whose chunks were all stored with no extraction is read again once.

An extraction outage stores chunks with no entities and no summary, and the file's
row is written ``done`` under its content hash, so an unchanged file would never be
read again. Each sweep with a pool selects only untouched active chunks for a
re-read. A cutoff on the source excludes replacement chunks from later sweeps,
and the source is stamped once no eligible files remain. Each sweep rechecks
archive status and whether the user edited any chunk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew.knowledge.chunker import HeadingAwareChunker
from kiro_crew.knowledge.extractor import EntityExtractor
from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore

#: The stamp a checked source carries (``folder_extraction_backfill.EXTRACTION_BACKFILL_KEY``).
EXTRACTION_BACKFILL_KEY = "extraction_backfill"

GOOD_REPLY = json.dumps(
    {
        "title": "Billing storage",
        "entities": [{"name": "DynamoDB", "type": "technology", "description": "db"}],
        "relations": [],
        "category": "design_doc",
        "summary": "Billing stores invoices in DynamoDB.",
    }
)


class _Pool:
    def __init__(self, reply: str):
        self.reply = reply
        self.batch_prompts: list[str] = []

    async def send_batch(self, prompts, timeout=60.0):
        self.batch_prompts.extend(prompts)
        return [self.reply for _ in prompts]

    async def send(self, prompt, timeout=60.0):
        return ""


@pytest.fixture
def kstore(tmp_path):
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield store
    store.close()


def _pipeline(store, pool) -> IngestionPipeline:
    return IngestionPipeline(
        store=store,
        extractor=EntityExtractor(pool=pool),
        chunker=HeadingAwareChunker(),
        reader=FileReader(),
        embedder=None,
    )


def _source(store, source_id: str) -> dict:
    return dict(store.db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone())


def _summaries(store, source_id: str) -> list[str]:
    return [
        r["summary"] or ""
        for r in store.db.execute(
            "SELECT summary FROM items WHERE source_id = ? AND status = 'active'", (source_id,)
        ).fetchall()
    ]


async def _scan(store, source_id: str, pool) -> None:
    await FolderWatcher(store, _pipeline(store, pool)).scan_source(_source(store, source_id))


@pytest.mark.asyncio
async def test_an_outage_file_is_read_again_once_and_extracted_files_are_left(kstore, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "good.md").write_text("# Good\n\nGOOD-FILE-TEXT is here.\n")
    source_id = kstore.add_source("docs", "local_folder", str(folder))
    await _scan(kstore, source_id, _Pool(GOOD_REPLY))

    # The outage: the new file's chunks get no usable reply.
    (folder / "outage.md").write_text("# Outage\n\nOUTAGE-FILE-TEXT is here.\n")
    await _scan(kstore, source_id, _Pool("not json"))
    assert "" in _summaries(kstore, source_id)

    # A database written before this build carries no stamp.
    kstore.merge_source_properties(
        source_id, remove_keys=(EXTRACTION_BACKFILL_KEY, "extraction_backfill_since")
    )

    pool = _Pool(GOOD_REPLY)
    await _scan(kstore, source_id, pool)

    assert any("OUTAGE-FILE-TEXT" in p for p in pool.batch_prompts)
    assert not any("GOOD-FILE-TEXT" in p for p in pool.batch_prompts)
    assert "" not in _summaries(kstore, source_id)
    row = kstore.db.execute(
        "SELECT status, content_hash FROM folder_file_state WHERE file_path = ?",
        (str(folder / "outage.md"),),
    ).fetchone()
    assert row["status"] == "done" and row["content_hash"]

    # Stamped: a file that still extracts to nothing is not re-read every sweep.
    (folder / "nothing.md").write_text("# Nothing\n\nNOTHING-FILE-TEXT is here.\n")
    await _scan(kstore, source_id, _Pool("not json"))
    later = _Pool(GOOD_REPLY)
    await _scan(kstore, source_id, later)
    assert later.batch_prompts == []


@pytest.mark.asyncio
async def test_no_pool_leaves_the_source_unstamped(kstore, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.md").write_text("# A\n\nA-FILE-TEXT is here.\n")
    source_id = kstore.add_source("docs", "local_folder", str(folder))

    await _scan(kstore, source_id, None)
    props = json.loads(_source(kstore, source_id)["properties"] or "{}")
    assert EXTRACTION_BACKFILL_KEY not in props

    # The first sweep that can extract re-reads the file.
    pool = _Pool(GOOD_REPLY)
    await _scan(kstore, source_id, pool)
    assert any("A-FILE-TEXT" in p for p in pool.batch_prompts)
    assert "" not in _summaries(kstore, source_id)


@pytest.mark.asyncio
async def test_a_file_with_an_archived_chunk_is_not_read_again(kstore, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "old.md").write_text("# Old\n\nARCHIVED-FILE-TEXT is here.\n")
    source_id = kstore.add_source("docs", "local_folder", str(folder))
    await _scan(kstore, source_id, _Pool("not json"))
    kstore.merge_source_properties(
        source_id, remove_keys=(EXTRACTION_BACKFILL_KEY, "extraction_backfill_since")
    )
    kstore.db.execute("UPDATE items SET status = 'archived' WHERE source_id = ?", (source_id,))
    kstore.db.commit()

    pool = _Pool(GOOD_REPLY)
    await _scan(kstore, source_id, pool)

    assert pool.batch_prompts == []
    assert _summaries(kstore, source_id) == []


@pytest.mark.asyncio
async def test_a_user_tagged_outage_file_is_not_read_again(kstore, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "tagged.md").write_text("# Tagged\n\nTAGGED-FILE-TEXT is here.\n", encoding="utf-8")
    source_id = kstore.add_source("docs", "local_folder", str(folder))
    await _scan(kstore, source_id, None)
    items = kstore.db.execute(
        "SELECT id, summary FROM items WHERE source_id = ?", (source_id,)
    ).fetchall()
    assert items and all(not item["summary"] for item in items)
    kstore.db.execute(
        "UPDATE items SET created_at = ?, updated_at = ? WHERE source_id = ?",
        ("2000-01-01T00:00:00", "2000-01-01T00:00:00", source_id),
    )
    kstore.db.commit()
    for item in items:
        kstore.update_item(item["id"], tags=["mine"])

    pool = _Pool(GOOD_REPLY)
    await _scan(kstore, source_id, pool)

    assert pool.batch_prompts == []
    saved = kstore.db.execute(
        "SELECT id, tags, status FROM items WHERE source_id = ?", (source_id,)
    ).fetchall()
    assert {item["id"] for item in saved} == {item["id"] for item in items}
    assert all(
        json.loads(item["tags"]) == ["mine"] and item["status"] == "active" for item in saved
    )
    assert json.loads(_source(kstore, source_id)["properties"])[EXTRACTION_BACKFILL_KEY]


@pytest.mark.asyncio
async def test_a_budget_stop_rechecks_archives_before_the_next_sweep(kstore, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    files = {str(folder / f"{name}.md"): f"{name.upper()}-OUTAGE-TEXT" for name in ("a", "b")}
    for path, marker in files.items():
        Path(path).write_text(f"# Outage\n\n{marker} is here.\n", encoding="utf-8")
    source_id = kstore.add_source("docs", "local_folder", str(folder))
    await _scan(kstore, source_id, None)
    assert _summaries(kstore, source_id) == ["", ""]
    hashes = {
        row["file_path"]: row["content_hash"]
        for row in kstore.db.execute(
            "SELECT file_path, content_hash FROM folder_file_state WHERE source_id = ?",
            (source_id,),
        ).fetchall()
    }
    assert all(hashes.values())

    pool = _Pool(GOOD_REPLY)
    stats = await FolderWatcher(kstore, _pipeline(kstore, pool)).scan_source(
        _source(kstore, source_id), chunk_budget=1
    )
    assert stats["budget_reached"] == 1 and stats["changed"] == 1
    assert len(pool.batch_prompts) == 1
    assert EXTRACTION_BACKFILL_KEY not in json.loads(_source(kstore, source_id)["properties"])
    remaining = [path for path, marker in files.items() if marker not in pool.batch_prompts[0]]
    assert len(remaining) == 1
    row = kstore.db.execute(
        "SELECT content_hash, item_ids FROM folder_file_state WHERE source_id = ? AND file_path = ?",
        (source_id, remaining[0]),
    ).fetchone()
    assert row["content_hash"] == hashes[remaining[0]]
    old_ids = json.loads(row["item_ids"])
    assert old_ids
    for item_id in old_ids:
        kstore.db.execute("UPDATE items SET status = 'archived' WHERE id = ?", (item_id,))
    kstore.db.commit()

    later = _Pool(GOOD_REPLY)
    await _scan(kstore, source_id, later)

    assert later.batch_prompts == []
    assert json.loads(_source(kstore, source_id)["properties"])[EXTRACTION_BACKFILL_KEY]
    archived = kstore.db.execute(
        "SELECT id FROM items WHERE source_id = ? AND status = 'archived'", (source_id,)
    ).fetchall()
    assert {item["id"] for item in archived} == set(old_ids)


@pytest.mark.asyncio
async def test_a_title_edited_outage_file_is_not_read_again(kstore, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "edited.md").write_text("# Edited\n\nEDITED-FILE-TEXT is here.\n", encoding="utf-8")
    source_id = kstore.add_source("docs", "local_folder", str(folder))
    await _scan(kstore, source_id, None)
    items = kstore.db.execute(
        "SELECT id, summary FROM items WHERE source_id = ?", (source_id,)
    ).fetchall()
    assert len(items) == 1 and not items[0]["summary"]
    item_id = items[0]["id"]
    kstore.db.execute(
        "UPDATE items SET created_at = ?, updated_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00", "2000-01-01T00:00:00", item_id),
    )
    kstore.db.commit()
    kstore.update_item(item_id, title="My title")

    pool = _Pool(GOOD_REPLY)
    await _scan(kstore, source_id, pool)

    assert pool.batch_prompts == []
    saved = kstore.get_item(item_id)
    assert saved is not None and saved["title"] == "My title"
    assert saved["status"] == "active" and not saved["summary"]
    props = json.loads(_source(kstore, source_id)["properties"])
    assert props[EXTRACTION_BACKFILL_KEY]
    assert "extraction_backfill_since" not in props


@pytest.mark.asyncio
async def test_a_budget_stop_reaches_each_outage_file_once_even_when_extraction_stays_empty(
    kstore, tmp_path
):
    folder = tmp_path / "docs"
    folder.mkdir()
    markers = {"A-OUTAGE-TEXT", "B-OUTAGE-TEXT"}
    for index, marker in enumerate(sorted(markers)):
        path = folder / f"{marker}.md"
        path.write_text(f"# Outage\n\n{marker} is here.\n", encoding="utf-8")
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
    source_id = kstore.add_source("docs", "local_folder", str(folder))
    await _scan(kstore, source_id, None)
    assert _summaries(kstore, source_id) == ["", ""]
    kstore.db.execute(
        "UPDATE items SET created_at = ?, updated_at = ? WHERE source_id = ?",
        ("2000-01-01T00:00:00", "2000-01-01T00:00:00", source_id),
    )
    kstore.db.commit()

    sent: list[str] = []
    cutoff = None
    for _ in range(2):
        pool = _Pool("not json")
        stats = await FolderWatcher(kstore, _pipeline(kstore, pool)).scan_source(
            _source(kstore, source_id), chunk_budget=1
        )
        assert stats["budget_reached"] == 1 and stats["changed"] == 1
        assert len(pool.batch_prompts) == 1
        sent.extend(marker for marker in markers if marker in pool.batch_prompts[0])
        props = json.loads(_source(kstore, source_id)["properties"])
        assert EXTRACTION_BACKFILL_KEY not in props
        if cutoff is None:
            cutoff = props["extraction_backfill_since"]
        assert props["extraction_backfill_since"] == cutoff

    assert len(sent) == 2 and set(sent) == markers
    assert _summaries(kstore, source_id) == ["", ""]
    later = _Pool("not json")
    await FolderWatcher(kstore, _pipeline(kstore, later)).scan_source(
        _source(kstore, source_id), chunk_budget=1
    )
    assert later.batch_prompts == []
    props = json.loads(_source(kstore, source_id)["properties"])
    assert props[EXTRACTION_BACKFILL_KEY]
    assert "extraction_backfill_since" not in props
