"""A folder file read again keeps a chunk the user edited while its new version was extracted.

A changed folder file is read again and its item group replaced at the end of
the read. A chunk edited or archived through the item PATCH route after the
read recorded when each old chunk was last changed (before extraction) is the
newer act: the old group stays as the user left it, with its include record,
and the file's new text waits for its next change.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from kiro_crew.knowledge.chunker import HeadingAwareChunker
from kiro_crew.knowledge.extractor import EntityExtractor
from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore

#: One section of a version long enough to chunk into several items.
LONG_PART = "SECOND-VERSION words " * 400

REPLY = json.dumps(
    {"title": "Doc", "entities": [], "relations": [], "category": "document", "summary": "s"}
)


class _EditingPool:
    """An extraction pool that edits one stored item, as the PATCH route does, when asked."""

    def __init__(self, store: KnowledgeStore, fields: dict):
        self.store = store
        self.fields = fields
        self.edit_id: str | None = None
        self.extracted = 0

    async def send_batch(self, prompts, timeout=60.0):
        self.extracted += len(prompts)
        if self.edit_id is not None:
            await asyncio.to_thread(self.store.update_item, self.edit_id, **self.fields)
            self.edit_id = None
        return [REPLY for _ in prompts]

    async def send(self, prompt, timeout=60.0):
        return ""


class _SameVectorEmbedder:
    """Embeds every chunk as one vector, so the fuzzy dedup tier pairs same-named files."""

    model, dim, content_budget = "same-vector", 2, 1000

    def embed_for_item(self, title, summary, content, priority=None):
        return [1.0, 0.0]


def _watcher(
    store: KnowledgeStore, fields: dict, embedder=None
) -> tuple[FolderWatcher, _EditingPool]:
    pool = _EditingPool(store, fields)
    pipeline = IngestionPipeline(
        store=store,
        extractor=EntityExtractor(pool=pool),
        chunker=HeadingAwareChunker(),
        reader=FileReader(),
        embedder=embedder,
    )
    return FolderWatcher(store, pipeline), pool


@pytest.fixture
def store(tmp_path, opened) -> KnowledgeStore:
    return opened(KnowledgeStore(str(tmp_path / "knowledge.db")))


def _age(store: KnowledgeStore, source_id: str) -> None:
    """Stamp the stored items in the past, so the edit's stamp differs on any clock."""
    stamp = "2000-01-01T00:00:00"
    store.db.execute(
        "UPDATE items SET created_at = ?, updated_at = ? WHERE source_id = ?",
        (stamp, stamp, source_id),
    )
    store.db.commit()


def _source(store: KnowledgeStore, source_id: str) -> dict:
    return dict(store.db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone())


def _group(store: KnowledgeStore, source_id: str, file_path: str) -> list[str]:
    row = store.db.execute(
        "SELECT item_ids FROM folder_file_state WHERE source_id = ? AND file_path = ?",
        (source_id, file_path),
    ).fetchone()
    return json.loads(row["item_ids"])


def _rewrite(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    later = os.stat(path).st_mtime + 10
    os.utime(path, (later, later))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields", [{"status": "archived"}, {"title": "Kept title"}], ids=["archived", "retitled"]
)
async def test_an_item_edited_during_a_reread_keeps_the_old_group(store, tmp_path, fields):
    folder = tmp_path / "folder"
    folder.mkdir()
    doc = folder / "doc.md"
    doc.write_text("# Doc\n\nFIRST-VERSION text.\n", encoding="utf-8")
    source_id = store.add_source("folder", "local_folder", str(folder))
    watcher, pool = _watcher(store, fields)
    await watcher.scan_source(_source(store, source_id))
    old_ids = _group(store, source_id, str(doc))
    assert old_ids
    _age(store, source_id)

    sections = "".join(f"## Part {i}\n\n{LONG_PART}\n\n" for i in range(3))
    _rewrite(doc, "# Doc\n\n" + sections)
    pool.edit_id = old_ids[0]
    pool.extracted = 0
    stats = await watcher.scan_source(_source(store, source_id), chunk_budget=1000)

    edited = store.get_item(old_ids[0])
    assert edited is not None, "the read deleted the item edited during extraction"
    assert {k: edited[k] for k in fields} == fields
    assert _group(store, source_id, str(doc)) == old_ids
    texts = [
        r["content"]
        for r in store.db.execute(
            "SELECT content FROM items WHERE source_id = ?", (source_id,)
        ).fetchall()
    ]
    assert not any("SECOND-VERSION" in t for t in texts), texts
    # The sweep is charged for the chunks this read extracted, not the kept group's size.
    assert stats["chunks_ingested"] == pool.extracted > len(old_ids)


@pytest.mark.asyncio
async def test_a_kept_group_keeps_the_include_record_its_text_was_read_with(store, tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "part.adoc").write_text("PART-TEXT lives here.\n", encoding="utf-8")
    doc = folder / "doc.adoc"
    doc.write_text("= Doc\n\ninclude::part.adoc[]\n", encoding="utf-8")
    source_id = store.add_source("folder", "local_folder", str(folder))
    watcher, pool = _watcher(store, {"status": "archived"})
    await watcher.scan_source(_source(store, source_id))
    query = "SELECT targets FROM folder_include_targets WHERE source_id = ? AND file_path = ?"
    record = store.db.execute(query, (source_id, str(doc))).fetchone()
    assert record is not None and json.loads(record["targets"])
    _age(store, source_id)

    _rewrite(doc, "= Doc\n\nNo include in this version.\n")
    pool.edit_id = _group(store, source_id, str(doc))[0]
    await watcher.scan_source(_source(store, source_id))

    kept = store.db.execute(query, (source_id, str(doc))).fetchone()
    assert kept is not None, "the kept group lost the record of the part its text inlines"
    assert kept["targets"] == record["targets"]


@pytest.mark.asyncio
async def test_a_kept_group_of_a_shrunk_file_charges_only_the_chunks_extracted(store, tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    doc = folder / "doc.md"
    doc.write_text(
        "# Doc\n\n" + "".join(f"## Part {i}\n\n{LONG_PART}\n\n" for i in range(3)), encoding="utf-8"
    )
    source_id = store.add_source("folder", "local_folder", str(folder))
    watcher, pool = _watcher(store, {"title": "Kept title"})
    await watcher.scan_source(_source(store, source_id))
    old_ids = _group(store, source_id, str(doc))
    _age(store, source_id)

    _rewrite(doc, "# Doc\n\nOne short section.\n")
    pool.edit_id = old_ids[0]
    pool.extracted = 0
    stats = await watcher.scan_source(_source(store, source_id), chunk_budget=1000)

    assert _group(store, source_id, str(doc)) == old_ids
    assert stats["chunks_ingested"] == pool.extracted == 1 < len(old_ids)


@pytest.mark.asyncio
async def test_a_source_deduped_against_a_dropped_new_group_inherits_it(store, tmp_path):
    version_2 = "# Doc\n\n" + "".join(f"## Part {i}\n\n{LONG_PART}\n\n" for i in range(2))
    a_folder, b_folder = tmp_path / "a", tmp_path / "b"
    a_folder.mkdir()
    b_folder.mkdir()
    a_doc, b_doc = a_folder / "doc.md", b_folder / "doc.md"
    a_doc.write_text("# Doc\n\nFIRST-VERSION text.\n", encoding="utf-8")
    a_id = store.add_source("a", "local_folder", str(a_folder))
    b_id = store.add_source("b", "local_folder", str(b_folder))
    a_watcher, _ = _watcher(store, {})
    b_watcher, _ = _watcher(store, {})
    await a_watcher.scan_source(_source(store, a_id))
    old_ids = _group(store, a_id, str(a_doc))
    _age(store, a_id)
    b_doc.write_text(version_2, encoding="utf-8")
    _rewrite(a_doc, version_2)

    loop = asyncio.get_running_loop()
    finalize = store.delete_group_unless_edited
    new_ids: list[str] = []

    def _b_scans_and_an_old_item_is_edited(old_stamps, new_item_ids, owner_source_id):
        # A's new chunks are written and its finalize has not run yet.
        new_ids.extend(new_item_ids)
        scan = b_watcher.scan_source(_source(store, b_id))
        asyncio.run_coroutine_threadsafe(scan, loop).result(timeout=60)
        store.update_item(old_ids[0], title="Kept title")
        return finalize(old_stamps, new_item_ids, owner_source_id)

    store.delete_group_unless_edited = _b_scans_and_an_old_item_is_edited
    await a_watcher.scan_source(_source(store, a_id))
    store.delete_group_unless_edited = finalize

    assert len(new_ids) > 1
    assert _group(store, a_id, str(a_doc)) == old_ids
    assert store.get_item(old_ids[0])["title"] == "Kept title"
    query = "SELECT status FROM folder_file_state WHERE source_id = ? AND file_path = ?"
    assert store.db.execute(query, (b_id, str(b_doc))).fetchone()["status"] == "done"
    assert sorted(_group(store, b_id, str(b_doc))) == sorted(new_ids)
    b_row = "SELECT * FROM folder_file_state WHERE source_id = ?"
    before = dict(store.db.execute(b_row, (b_id,)).fetchone())
    items = [store.get_item(item_id) for item_id in new_ids]
    for item in items:
        assert item is not None and item["status"] == "active" and item["source_id"] == b_id
        assert item["content_hash"] == before["text_hash"]
    assert "SECOND-VERSION" in "".join(item["content"] for item in items)
    await b_watcher.scan_source(_source(store, b_id))
    after = dict(store.db.execute(b_row, (b_id,)).fetchone())
    assert {**after, "last_seen": None} == {**before, "last_seen": None}


@pytest.mark.asyncio
async def test_a_kept_group_is_not_collapsed_into_a_newer_copy_by_the_same_scan(store, tmp_path):
    a_folder, b_folder = tmp_path / "a", tmp_path / "b"
    a_folder.mkdir()
    b_folder.mkdir()
    a_doc, b_doc = a_folder / "doc.md", b_folder / "doc.md"
    a_doc.write_text("# Doc\n\nFIRST-VERSION text.\n", encoding="utf-8")
    a_id = store.add_source("a", "local_folder", str(a_folder))
    b_id = store.add_source("b", "local_folder", str(b_folder))
    a_watcher, _ = _watcher(store, {}, _SameVectorEmbedder())
    b_watcher, _ = _watcher(store, {}, _SameVectorEmbedder())
    await a_watcher.scan_source(_source(store, a_id))
    old_ids = _group(store, a_id, str(a_doc))
    _age(store, a_id)
    _rewrite(a_doc, "# Doc\n\n" + "".join(f"## Part {i}\n\n{LONG_PART}\n\n" for i in range(2)))
    b_mtime = os.stat(a_doc).st_mtime + 10

    loop = asyncio.get_running_loop()
    finalize = store.delete_group_unless_edited

    def _b_indexes_a_newer_copy_and_an_old_item_is_edited(
        old_stamps, new_item_ids, owner_source_id
    ):
        # A's new chunks are written and its finalize has not run yet.
        b_doc.write_text("# Doc\n\nFIRST-VERSION text, copied.\n", encoding="utf-8")
        os.utime(b_doc, (b_mtime, b_mtime))
        scan = b_watcher.scan_source(_source(store, b_id))
        asyncio.run_coroutine_threadsafe(scan, loop).result(timeout=60)
        store.update_item(old_ids[0], title="Kept title")
        return finalize(old_stamps, new_item_ids, owner_source_id)

    store.delete_group_unless_edited = _b_indexes_a_newer_copy_and_an_old_item_is_edited
    await a_watcher.scan_source(_source(store, a_id))
    store.delete_group_unless_edited = finalize

    query = "SELECT status FROM folder_file_state WHERE source_id = ? AND file_path = ?"
    assert store.db.execute(query, (a_id, str(a_doc))).fetchone()["status"] == "done"
    assert _group(store, a_id, str(a_doc)) == old_ids
    edited = store.get_item(old_ids[0])
    assert edited is not None, "the scan's dedup collapsed the group the read kept"
    assert edited["title"] == "Kept title"
