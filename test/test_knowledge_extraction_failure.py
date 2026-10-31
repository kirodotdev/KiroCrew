"""A failed extraction call is recorded and retried, never stored as "found nothing".

The pool turns a failed call (timeout, HTTP error, dead worker) into ``""``. Read as
the empty payload a chunk with no entities produces, that failure would be stored as
a processed chunk under a 'completed' job and a 'done' folder row keyed by the file's
hash -- so the scan would skip the file for good and its chunks would never get
entities or a summary.

These tests pin the contract instead: the chunk is still stored (its text stays
searchable) with a failed-extraction mark, the job row says how many chunks came
back unextracted, and the folder watcher re-extracts ONLY the marked chunks of an
unchanged file, in place, with a backoff, up to ``MAX_SCAN_ATTEMPTS`` times. A retry
never touches a chunk that extracted, and never retires the file. Built on the
strict store from ``test_folder_watcher_off_loop_guard`` so the new writes are also
proven to stay off the event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import MagicMock

import pytest

from kiro_crew.knowledge.dedup import _build_doc_for, _collapse_doc
from kiro_crew.knowledge.extractor import EXTRACTION_FAILED_KEY, EntityExtractor
from kiro_crew.knowledge.folder_watcher import MAX_SCAN_ATTEMPTS, FolderWatcher
from kiro_crew.knowledge.ingestion import EXTRACTION_INCOMPLETE_PREFIX, IngestionPipeline
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.on_loop_db import STORE_STRICT_ENV

pytestmark = pytest.mark.asyncio

#: Long enough ago that any backoff has elapsed.
_LONG_AGO = "2000-01-01T00:00:00"


def _ok(chunk: str) -> dict:
    return {
        "title": f"title of {chunk}",
        "category": "design_doc",
        "summary": f"summary of {chunk}",
        "entities": [
            {"name": f"entity of {chunk}", "type": "concept", "description": "d"},
            {"name": f"other of {chunk}", "type": "concept", "description": "d"},
        ],
        "relations": [
            {
                "source": f"entity of {chunk}",
                "target": f"other of {chunk}",
                "type": "uses",
                "description": "r",
            }
        ],
    }


_FAILED = {
    "title": "",
    "category": "document",
    "summary": "",
    "entities": [],
    "relations": [],
    EXTRACTION_FAILED_KEY: True,
}


class _Extractor:
    """Answers per chunk: a chunk whose text ends with a failing part index fails."""

    can_extract = True
    _pool = None  # generate_source_summary reads it; None makes that a no-op

    def __init__(self, failing: set[int]):
        self.failing = failing
        self.calls: list[list[str]] = []
        self.raises = False

    async def extract_batch(self, chunks: list[str]) -> list[dict]:
        self.calls.append(list(chunks))
        if self.raises:
            raise RuntimeError("pool exploded")
        return [
            dict(_FAILED) if int(c.rsplit(" ", 1)[1]) in self.failing else _ok(c) for c in chunks
        ]


async def off(fn, *args):
    return await asyncio.to_thread(fn, *args)


@pytest.fixture()
def strict_store(monkeypatch, tmp_path):
    monkeypatch.setenv(STORE_STRICT_ENV, "1")
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield store
    store.close()


def _pipeline(store: KnowledgeStore, failing: set[int]) -> IngestionPipeline:
    chunker = MagicMock()

    def _two_chunks(text, **_kw):
        return [
            {
                "content": f"{text} part {i}",
                "chunk_index": i,
                "section_title": None,
                "line_start": 0,
                "line_end": 0,
            }
            for i in range(2)
        ]

    chunker.chunk.side_effect = _two_chunks
    chunker.chunk_markdown.side_effect = _two_chunks
    reader = MagicMock()
    reader.read.return_value = ("some body", {})
    return IngestionPipeline(
        store=store, extractor=_Extractor(failing), chunker=chunker, reader=reader, embedder=None
    )


def _add_source(store: KnowledgeStore, folder) -> dict:
    source_id = store.add_source("folder", "local_folder", str(folder))
    return {"id": source_id, "uri": str(folder), "source_type": "local_folder", "properties": "{}"}


def _row(store: KnowledgeStore, source_id: str) -> dict:
    return dict(
        store.db.execute(
            "SELECT status, error_message, attempts, item_ids FROM folder_file_state WHERE source_id = ?",
            (source_id,),
        ).fetchone()
    )


def _items(store: KnowledgeStore, source_id: str) -> list[dict]:
    return [
        dict(r)
        for r in store.db.execute(
            "SELECT id, chunk_index, title, item_type, summary, extraction_failed_at FROM items "
            "WHERE source_id = ? ORDER BY chunk_index",
            (source_id,),
        ).fetchall()
    ]


def _mentions(store: KnowledgeStore, item_id: str) -> int:
    return store.db.execute(
        "SELECT COUNT(*) FROM mentions WHERE item_id = ?", (item_id,)
    ).fetchone()[0]


def _relations(store: KnowledgeStore, item_id: str) -> int:
    return store.db.execute(
        "SELECT COUNT(*) FROM entity_relations WHERE source_item_id = ?", (item_id,)
    ).fetchone()[0]


def _age_failures(store: KnowledgeStore) -> None:
    store.db.execute(
        "UPDATE items SET extraction_failed_at = ? WHERE extraction_failed_at IS NOT NULL",
        (_LONG_AGO,),
    )
    store.db.commit()


def _jobs(store: KnowledgeStore) -> list[dict]:
    return [
        dict(r)
        for r in store.db.execute(
            "SELECT status, items_processed, error FROM ingestion_jobs ORDER BY created_at"
        ).fetchall()
    ]


@pytest.fixture()
def folder(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "a.md").write_text("alpha", encoding="utf-8")
    return folder


async def test_failed_chunks_are_stored_marked_and_the_job_says_so(strict_store, folder):
    pipeline = _pipeline(strict_store, failing={1})
    source_id = await off(lambda: strict_store.add_source("s", "local_folder", str(folder)))
    calls: list[str] = []

    await pipeline.ingest_file(
        str(folder / "a.md"),
        source_id=source_id,
        old_item_ids=[],
        on_extraction_failed=lambda failed, total: calls.append(f"failed {failed}/{total}"),
        on_committed=lambda ids: calls.append(f"committed {len(ids)}"),
        count_toward_import_budget=False,
    )

    # Before on_committed, so a caller can record both in one state write.
    assert calls == ["failed 1/2", "committed 2"]
    assert await off(_jobs, strict_store) == [
        {
            "status": "completed",
            "items_processed": 2,
            "error": f"{EXTRACTION_INCOMPLETE_PREFIX}1 of 2 chunks",
        }
    ]
    items = await off(_items, strict_store, source_id)
    assert len(items) == 2  # the unextracted chunk stays searchable
    assert [i["extraction_failed_at"] is not None for i in items] == [False, True]


async def test_a_clean_ingest_does_not_report_failures(strict_store, folder):
    pipeline = _pipeline(strict_store, failing=set())
    source_id = await off(lambda: strict_store.add_source("s", "local_folder", str(folder)))
    calls: list[tuple[int, int]] = []

    await pipeline.ingest_file(
        str(folder / "a.md"),
        source_id=source_id,
        old_item_ids=[],
        on_extraction_failed=lambda failed, total: calls.append((failed, total)),
        count_toward_import_budget=False,
    )

    assert calls == []
    assert (await off(_jobs, strict_store))[0]["error"] is None
    assert all(
        i["extraction_failed_at"] is None for i in await off(_items, strict_store, source_id)
    )


async def test_a_retry_reextracts_only_the_failed_chunk_in_place(strict_store, folder):
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)
    before = await off(_items, strict_store, source["id"])

    await off(_age_failures, strict_store)
    pipeline.extractor.failing.clear()
    await watcher.scan_source(source)

    # One call, for the failed chunk only: the file was not re-ingested.
    assert pipeline.extractor.calls[-1] == ["some body part 1"]
    after = await off(_items, strict_store, source["id"])
    assert [i["id"] for i in after] == [i["id"] for i in before]
    assert after[0] == before[0]
    assert after[1]["summary"] == "summary of some body part 1"
    assert after[1]["title"] == "title of some body part 1"
    assert after[1]["item_type"] == "design_doc"
    assert after[1]["extraction_failed_at"] is None
    assert await off(_mentions, strict_store, after[1]["id"]) == 2
    row = await off(_row, strict_store, source["id"])
    assert (row["status"], row["error_message"], row["attempts"]) == ("done", None, 0)

    await watcher.scan_source(source)
    assert len(pipeline.extractor.calls) == 2  # clean now: nothing left to retry


async def test_a_failed_retry_never_costs_a_chunk_that_extracted(strict_store, folder):
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)
    good = (await off(_items, strict_store, source["id"]))[0]
    assert await off(_mentions, strict_store, good["id"]) == 2

    # The outage outlasts the first ingest: every call fails from now on.
    pipeline.extractor.failing.update({0, 1})
    await off(_age_failures, strict_store)
    await watcher.scan_source(source)

    after = await off(_items, strict_store, source["id"])
    assert after[0] == good
    assert await off(_mentions, strict_store, good["id"]) == 2
    assert after[1]["extraction_failed_at"] not in (None, _LONG_AGO)  # re-stamped
    row = await off(_row, strict_store, source["id"])
    assert (row["status"], row["attempts"]) == ("done", 2)
    assert row["error_message"] == f"{EXTRACTION_INCOMPLETE_PREFIX}1 of 2 chunks"


async def test_a_retry_waits_out_the_backoff(strict_store, folder):
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)

    await watcher.scan_source(source)  # the failure is moments old
    assert len(pipeline.extractor.calls) == 1
    assert (await off(_row, strict_store, source["id"]))["attempts"] == 1

    await off(_age_failures, strict_store)
    await watcher.scan_source(source)
    assert len(pipeline.extractor.calls) == 2


async def test_retries_stop_when_the_budget_is_spent(strict_store, folder):
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)

    for attempt in range(2, MAX_SCAN_ATTEMPTS + 1):
        await off(_age_failures, strict_store)
        await watcher.scan_source(source)
        assert (await off(_row, strict_store, source["id"]))["attempts"] == attempt
    assert len(pipeline.extractor.calls) == MAX_SCAN_ATTEMPTS

    await off(_age_failures, strict_store)
    await watcher.scan_source(source)
    assert len(pipeline.extractor.calls) == MAX_SCAN_ATTEMPTS
    row = await off(_row, strict_store, source["id"])
    assert (row["status"], row["attempts"]) == ("done", MAX_SCAN_ATTEMPTS)
    assert row["error_message"].startswith(EXTRACTION_INCOMPLETE_PREFIX)


async def test_a_retry_that_raises_never_retires_the_file(strict_store, folder):
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)
    before = await off(_items, strict_store, source["id"])

    pipeline.extractor.raises = True
    await off(_age_failures, strict_store)
    await watcher.scan_source(source)

    row = await off(_row, strict_store, source["id"])
    assert (row["status"], row["attempts"]) == ("done", 2)
    assert row["error_message"].startswith(EXTRACTION_INCOMPLETE_PREFIX)
    assert [i["id"] for i in await off(_items, strict_store, source["id"])] == [
        i["id"] for i in before
    ]


async def test_touching_a_spent_file_keeps_the_marker(strict_store, folder):
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)
    for _ in range(MAX_SCAN_ATTEMPTS - 1):
        await off(_age_failures, strict_store)
        await watcher.scan_source(source)

    path = folder / "a.md"
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))  # mtime moves, bytes do not
    await off(_age_failures, strict_store)
    await watcher.scan_source(source)

    assert len(pipeline.extractor.calls) == MAX_SCAN_ATTEMPTS
    row = await off(_row, strict_store, source["id"])
    assert row["error_message"].startswith(EXTRACTION_INCOMPLETE_PREFIX)
    assert row["attempts"] == MAX_SCAN_ATTEMPTS


async def test_editing_a_spent_file_starts_a_fresh_budget(strict_store, folder):
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)
    for _ in range(MAX_SCAN_ATTEMPTS - 1):
        await off(_age_failures, strict_store)
        await watcher.scan_source(source)

    path = folder / "a.md"
    path.write_text("alpha, edited", encoding="utf-8")
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))
    await watcher.scan_source(source)

    # A full ingest of the new content, both chunks, with a fresh count.
    assert pipeline.extractor.calls[-1] == ["some body part 0", "some body part 1"]
    assert (await off(_row, strict_store, source["id"]))["attempts"] == 1


async def test_state_row_json_is_the_committed_group(strict_store, folder):
    # The incomplete branch rewrites the row from the caller; it must name the
    # group that was committed, exactly as the clean 'done' write does.
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)

    row = await off(_row, strict_store, source["id"])
    ids = {i["id"] for i in await off(_items, strict_store, source["id"])}
    assert set(json.loads(row["item_ids"])) == ids


async def test_no_pool_reextracts_nothing(strict_store, folder):
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)

    # Without a pool every chunk comes back empty, never sent: that must not
    # read as a successful re-extraction and clear the mark.
    pipeline.extractor.can_extract = False
    await off(_age_failures, strict_store)
    await watcher.scan_source(source)

    assert len(pipeline.extractor.calls) == 1
    items = await off(_items, strict_store, source["id"])
    assert items[1]["extraction_failed_at"] is not None


async def test_an_interrupted_reextraction_stores_each_relation_once(
    strict_store, folder, monkeypatch
):
    # The entities of an attempt land, then completing it raises (the same state a
    # shutdown between the two leaves): the chunk stays marked, and the next
    # attempt must not add its relations a second time.
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)
    pipeline.extractor.failing.clear()

    real_complete = strict_store.complete_reextraction

    def _complete_fails_once(*args, **kwargs):
        monkeypatch.setattr(strict_store, "complete_reextraction", real_complete)
        raise RuntimeError("store error after the entities were written")

    monkeypatch.setattr(strict_store, "complete_reextraction", _complete_fails_once)
    await off(_age_failures, strict_store)
    await watcher.scan_source(source)
    item = (await off(_items, strict_store, source["id"]))[1]
    assert item["extraction_failed_at"] is not None
    assert await off(_relations, strict_store, item["id"]) == 1

    await off(_age_failures, strict_store)
    await watcher.scan_source(source)

    item = (await off(_items, strict_store, source["id"]))[1]
    assert item["extraction_failed_at"] is None
    assert await off(_relations, strict_store, item["id"]) == 1
    assert await off(_mentions, strict_store, item["id"]) == 2


async def test_a_retry_that_spends_the_chunk_budget_ends_the_sweep(strict_store, folder):
    # The newer, unchanged file's retry costs the whole budget, so the older new file
    # waits for the next sweep instead of being ingested past the allowance.
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)
    pipeline.extractor.failing.clear()
    older = folder / "b.md"
    older.write_text("beta", encoding="utf-8")
    newer_mtime = os.stat(folder / "a.md").st_mtime
    os.utime(older, (newer_mtime - 3600, newer_mtime - 3600))
    await off(_age_failures, strict_store)

    stats = await watcher.scan_source(source, chunk_budget=1)

    assert stats.get("budget_reached") == 1
    assert len(pipeline.extractor.calls) == 2  # the first ingest, then the retry alone
    assert pipeline.extractor.calls[1] == ["some body part 1"]
    rows = await off(
        lambda: strict_store.db.execute(
            "SELECT file_path FROM folder_file_state WHERE source_id = ?", (source["id"],)
        ).fetchall()
    )
    assert [os.path.basename(r["file_path"]) for r in rows] == ["a.md"]


@pytest.mark.parametrize("reply", ["{}", '{"error": "temporarily unavailable"}'])
async def test_a_reply_with_no_payload_never_clears_a_marked_chunks_links(
    strict_store, folder, monkeypatch, reply
):
    # An interrupted attempt left the chunk marked but holding its links. A retry whose
    # reply is JSON with no extraction field is a failed call, not "found nothing": the
    # links stay and the mark stays, so a later sweep still recovers the chunk.
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)
    pipeline.extractor.failing.clear()
    real_complete = strict_store.complete_reextraction

    def _complete_fails_once(*args, **kwargs):
        monkeypatch.setattr(strict_store, "complete_reextraction", real_complete)
        raise RuntimeError("store error after the entities were written")

    monkeypatch.setattr(strict_store, "complete_reextraction", _complete_fails_once)
    await off(_age_failures, strict_store)
    await watcher.scan_source(source)

    async def _payload_free(chunks):
        return [EntityExtractor()._parse_response(reply) for _ in chunks]

    monkeypatch.setattr(pipeline.extractor, "extract_batch", _payload_free)
    await off(_age_failures, strict_store)
    await watcher.scan_source(source)

    item = (await off(_items, strict_store, source["id"]))[1]
    assert item["extraction_failed_at"] is not None
    assert await off(_relations, strict_store, item["id"]) == 1
    assert await off(_mentions, strict_store, item["id"]) == 2


def _set_sig(store: KnowledgeStore, item_id: str, sig: str) -> None:
    store.db.execute("UPDATE items SET embedding_sig = ? WHERE id = ?", (sig, item_id))
    store.db.commit()


def _sig(store: KnowledgeStore, item_id: str) -> str | None:
    return store.db.execute("SELECT embedding_sig FROM items WHERE id = ?", (item_id,)).fetchone()[
        0
    ]


async def test_a_recovered_chunk_is_left_stale_for_the_reembed_sweep(strict_store, folder):
    # Its vector was built from the old title and summary. With no embedder (as
    # with a bulk embed while the model is unloaded) nothing re-embeds it here,
    # so the cleared signature is what makes the sig-gated sweep pick it up.
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)
    items = await off(_items, strict_store, source["id"])
    for item in items:
        await off(_set_sig, strict_store, item["id"], "current-sig")

    pipeline.extractor.failing.clear()
    await off(_age_failures, strict_store)
    await watcher.scan_source(source)

    assert await off(_sig, strict_store, items[1]["id"]) is None
    assert await off(_sig, strict_store, items[0]["id"]) == "current-sig"


async def test_the_source_summary_is_rebuilt_only_when_a_chunk_came_back(
    strict_store, folder, monkeypatch
):
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)
    rebuilt: list[str] = []

    async def _record(source_id):
        rebuilt.append(source_id)

    monkeypatch.setattr(pipeline, "generate_source_summary", _record)

    await off(_age_failures, strict_store)
    await watcher.scan_source(source)  # still failing: nothing to rebuild from
    assert rebuilt == []

    pipeline.extractor.failing.clear()
    await off(_age_failures, strict_store)
    await watcher.scan_source(source)
    assert rebuilt == [source["id"]]


async def test_both_committed_row_writers_keep_an_incomplete_reason(strict_store, folder):
    # The commit callback's write and the scan's fallback `_settle_done` record an
    # extraction-incomplete group the same way: 'done', the reason, and the
    # attempt the 'scanning' marker counted -- the re-extraction budget.
    store = strict_store
    watcher = FolderWatcher(store, MagicMock())
    source_id = (await off(_add_source, store, folder))["id"]
    path = str(folder / "a.md")
    reason = f"{EXTRACTION_INCOMPLETE_PREFIX}1 of 2 chunks"

    def _write_both() -> tuple[dict, dict, dict]:
        item_id = store.add_item("T", "body", "document", source_id=source_id)
        watcher._update_state(source_id, path, "h", 1.0, "[]", "now", "scanning", attempts=2)
        watcher._mark_committed(source_id, path, [item_id], None, reason)
        by_callback = _row(store, source_id)
        watcher._update_state(
            source_id, path, "h", 1.0, "[]", "now", "scanning", reason, attempts=2
        )
        watcher._settle_done(source_id, path, "h", 1.0, [item_id], "now", None, incomplete=True)
        by_scan = _row(store, source_id)
        watcher._update_state(
            source_id, path, "h", 1.0, "[]", "now", "scanning", reason, attempts=2
        )
        watcher._settle_done(source_id, path, "h", 1.0, [item_id], "now", None)
        clean = _row(store, source_id)
        return by_callback, by_scan, clean

    by_callback, by_scan, clean = await off(_write_both)
    for row in (by_callback, by_scan):
        assert (row["status"], row["error_message"], row["attempts"]) == ("done", reason, 2)
    assert (clean["status"], clean["error_message"], clean["attempts"]) == ("done", None, 0)


def _collapse(store: KnowledgeStore, loser_source_id: str, winner_source_id: str) -> list[str]:
    # A dedup sweep collapsing the loser's copy of a.md onto the winner's, through
    # the real `_collapse_doc`. Returns the loser's item ids, which it deletes.
    def _doc(source_id: str):
        path = store.db.execute(
            "SELECT file_path FROM folder_file_state WHERE source_id = ?", (source_id,)
        ).fetchone()["file_path"]
        return _build_doc_for(store, source_id, path)

    loser, winner = _doc(loser_source_id), _doc(winner_source_id)
    _collapse_doc(store, loser, winner)
    return list(loser.item_ids)


def _verdict(store: KnowledgeStore, source_id: str) -> tuple:
    row = store.db.execute(
        "SELECT status, item_ids, merged_into_source_id FROM folder_file_state "
        "WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    return tuple(row)


async def _winning_copy(store: KnowledgeStore, tmp_path) -> str:
    # Other text, so the pre-ingest gate does not refuse it as the same document.
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "a.md").write_text("alpha", encoding="utf-8")
    pipeline = _pipeline(store, failing=set())
    pipeline.reader.read.return_value = ("winning body", {})
    winner = await off(_add_source, store, copy)
    await FolderWatcher(store, pipeline).scan_source(winner)
    return winner["id"]


async def test_a_dedup_collapse_during_a_retry_keeps_its_verdict(strict_store, folder, tmp_path):
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)
    winner_id = await _winning_copy(strict_store, tmp_path)
    await off(_age_failures, strict_store)
    pipeline.extractor.failing.clear()
    extract = pipeline.extractor.extract_batch
    collapsed: list[str] = []

    async def _collapse_while_extracting(chunks):
        if not collapsed:
            collapsed.extend(await off(_collapse, strict_store, source["id"], winner_id))
        return await extract(chunks)

    pipeline.extractor.extract_batch = _collapse_while_extracting
    await watcher.scan_source(source)

    assert collapsed, "the retry never reached the extractor"
    assert await off(_verdict, strict_store, source["id"]) == ("deduped", "[]", winner_id)
    for item_id in collapsed:
        assert await off(_mentions, strict_store, item_id) == 0


async def test_a_dedup_collapse_during_a_touch_keeps_its_verdict(strict_store, folder, tmp_path):
    pipeline = _pipeline(strict_store, failing=set())
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)
    winner_id = await _winning_copy(strict_store, tmp_path)
    path = folder / "a.md"
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))
    hash_file = watcher._hash_file

    def _collapse_while_hashing(file_path):
        _collapse(strict_store, source["id"], winner_id)
        return hash_file(file_path)

    watcher._hash_file = _collapse_while_hashing
    await watcher.scan_source(source)

    assert await off(_verdict, strict_store, source["id"]) == ("deduped", "[]", winner_id)


async def test_a_retry_holds_the_ingestion_gate_against_the_orphan_sweep(strict_store, folder):
    # The deferred orphan sweep runs in a maintenance window that waits for every
    # ingestion holder; a retry re-linking entities must be one of them.
    pipeline = _pipeline(strict_store, failing={1})
    watcher = FolderWatcher(strict_store, pipeline)
    source = await off(_add_source, strict_store, folder)
    await watcher.scan_source(source)
    await off(_age_failures, strict_store)
    pipeline.extractor.failing.clear()
    extract = pipeline.extractor.extract_batch
    quiescent: list[bool] = []

    def _window_now() -> bool:
        with strict_store.maintenance_window(timeout=0) as quiet:
            return quiet

    async def _probe_while_extracting(chunks):
        quiescent.append(await off(_window_now))
        return await extract(chunks)

    pipeline.extractor.extract_batch = _probe_while_extracting
    await watcher.scan_source(source)

    assert quiescent == [False], "the orphan sweep could run during the retry"
    assert await off(_window_now) is True
