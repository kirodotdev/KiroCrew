"""Every writer of one source row's ``properties`` blob comes through the store's
write-locked take, so none of them can overwrite another's change.

``sources.properties`` is a whole-column rewrite. Three writers land on the same
row from worker threads with no ordering between them: the sync scheduler's
success reset, its failure increment, and the ingest pipeline's finalize, which
stamps the new ``content_hash`` (and the reader's metadata). Any of them working
from a snapshot it read earlier and rewriting the whole blob resurrects that
snapshot over whatever the others committed since: a finalize landing after an
outcome write puts a stale counter back, an outcome write landing after a
finalize puts the OLD content hash back, and the next scan re-ingests the file.

``KnowledgeStore.revise_source_properties`` takes the write lock BEFORE reading
(``BEGIN IMMEDIATE``) and applies each writer's revision to the row's CURRENT
blob, guarding the UPDATE with the blob it read. These tests force the
interleavings and check that both writers' keys survive, from both directions.

The store's strict on-loop guard is armed, as in
``test_knowledge_agent_sync_off_loop.py``, so a take that slips onto the loop
fails loudly instead of passing by accident.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.knowledge.sync import MAX_FAILURES, SyncScheduler
from kiro_crew.on_loop_db import STORE_STRICT_ENV

pytestmark = pytest.mark.asyncio

BODY = "the body of the file"
BODY_HASH = hashlib.sha256(BODY.encode()).hexdigest()


async def off(fn, *args, **kwargs):
    """Run a test-side store access on a worker thread."""
    return await asyncio.to_thread(lambda: fn(*args, **kwargs))


@pytest.fixture()
def strict_store(monkeypatch, tmp_path):
    monkeypatch.setenv(STORE_STRICT_ENV, "1")
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield store
    store.close()


def _pipeline(store: KnowledgeStore, reader_meta: dict | None = None) -> IngestionPipeline:
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
    reader.read.return_value = (BODY, dict(reader_meta or {}))
    return IngestionPipeline(
        store=store, extractor=extractor, chunker=chunker, reader=reader, embedder=None
    )


def _row(store: KnowledgeStore, sid: str) -> dict:
    row = store.db.execute(
        "SELECT properties, sync_status, last_synced FROM sources WHERE id = ?", (sid,)
    ).fetchone()
    return {
        "props": json.loads(row["properties"] or "{}"),
        "sync_status": row["sync_status"],
        "last_synced": row["last_synced"],
    }


class TestFinalizeAgainstOutcomeWriters:
    async def test_finalize_keeps_an_outcome_written_during_the_ingest(
        self, strict_store, tmp_path
    ):
        """The finalize's source-row write lands AFTER a whole ingest -- chunking,
        extraction, embedding -- during which the scheduler's success write
        can commit to the same row. The finalize must stamp its content hash
        as a delta onto that row, not rewrite the blob from the properties it
        read at ingest start: a snapshot rewrite would put the stale counter
        back and drop the metadata the sync recorded."""
        path = tmp_path / "doc.txt"
        path.write_text(BODY)
        sid = await off(
            strict_store.add_source,
            "doc",
            "local_file",
            str(path.resolve()),
            properties={"content_hash": "stale", "consecutive_failures": 2},
        )
        pipeline = _pipeline(strict_store, reader_meta={"pages": 3})
        sched = SyncScheduler(strict_store, pipeline, {})

        async def extract_then_record_success(chunks):
            # Runs between the snapshot read and the finalize hop.
            await asyncio.to_thread(
                lambda: sched._record_success(sid, {"etag": "fresh"}, completed=True)
            )
            return [{"category": "document", "summary": "s", "entities": []}]

        pipeline.extractor.extract_batch = AsyncMock(side_effect=extract_then_record_success)

        job_id = await pipeline.ingest_file(str(path), source_id=sid)

        assert job_id is not None
        assert (await off(pipeline.get_job_status, job_id))["status"] == "completed"
        row = await off(_row, strict_store, sid)
        # The finalize's own keys landed...
        assert row["props"]["content_hash"] == BODY_HASH
        assert row["props"]["pages"] == 3
        assert row["sync_status"] == "synced"
        assert row["last_synced"] is not None
        # ...and the outcome the sync wrote in the meantime survived them.
        assert row["props"]["consecutive_failures"] == 0
        assert row["props"]["metadata"] == {"etag": "fresh"}

    async def test_a_new_source_is_finalized_the_same_way(self, strict_store, tmp_path):
        """A file whose source the ingest creates itself takes the same finalize
        path: the hash and reader metadata are on the row and the status is
        stamped, with nothing else written to the blob."""
        path = tmp_path / "new.txt"
        path.write_text(BODY)
        pipeline = _pipeline(strict_store, reader_meta={"pages": 1})

        job_id = await pipeline.ingest_file(str(path))

        assert job_id is not None
        sid = (await off(strict_store.get_source_by_uri, str(path.resolve())))["id"]
        row = await off(_row, strict_store, sid)
        assert row["props"] == {"content_hash": BODY_HASH, "pages": 1}
        assert row["sync_status"] == "synced"
        assert row["last_synced"] is not None

    async def test_an_outcome_write_cannot_land_over_a_finalize_it_did_not_see(self, strict_store):
        """The other direction: the success write reads the row, and a finalize
        commits the new content hash before the success writes. Under the
        write-locked take the finalize cannot get in between -- it waits for
        the success to commit, then stamps its hash onto the reset row. Both
        keys survive; on a plain read-then-write the success would put the old
        hash back and the next scan would re-ingest the file."""
        sid = await off(
            strict_store.add_source,
            "remote",
            "webhook",
            "x://remote",
            properties={"content_hash": "old", "consecutive_failures": 1},
        )
        sched = SyncScheduler(strict_store, _pipeline(strict_store), {})

        success_read = threading.Event()
        finalize_done = threading.Event()
        real = strict_store.revise_source_properties

        def held(source_id, revise, **kw):
            def revise_after_hold(props):
                success_read.set()
                # Give the finalize every chance to land in the window. Under
                # the lock it cannot, so this times out and the write proceeds.
                finalize_done.wait(timeout=1.0)
                return revise(props)

            return real(source_id, revise_after_hold, **kw)

        strict_store.revise_source_properties = held  # type: ignore[method-assign]

        def finalize_stamp():
            success_read.wait(timeout=5.0)
            # What the ingest finalize writes for this row, straight at the store.
            real(sid, lambda props: props.update({"content_hash": "new"}) or "synced")
            finalize_done.set()

        await asyncio.gather(
            asyncio.to_thread(lambda: sched._record_success(sid, None, completed=True)),
            asyncio.to_thread(finalize_stamp),
        )

        row = await off(_row, strict_store, sid)
        assert row["props"]["content_hash"] == "new"
        assert row["props"]["consecutive_failures"] == 0
        assert row["sync_status"] == "synced"


class TestReviseSourceProperties:
    async def test_revision_reads_the_current_blob_and_stamps_status_and_timestamp(
        self, strict_store
    ):
        sid = await off(
            strict_store.add_source,
            "s",
            "webhook",
            "x://s",
            properties={"consecutive_failures": MAX_FAILURES - 1, "keep": "me"},
        )

        def bump(props):
            props["consecutive_failures"] += 1
            return "error" if props["consecutive_failures"] >= MAX_FAILURES else None

        persisted = await off(
            strict_store.revise_source_properties, sid, bump, last_synced="2000-01-01T00:00:00"
        )

        assert persisted == {"consecutive_failures": MAX_FAILURES, "keep": "me"}
        row = await off(_row, strict_store, sid)
        assert row["props"] == persisted
        assert row["sync_status"] == "error"
        assert row["last_synced"] == "2000-01-01T00:00:00"

    async def test_a_none_status_leaves_the_column_alone(self, strict_store):
        sid = await off(strict_store.add_source, "s", "webhook", "x://s", properties={})
        await off(lambda: strict_store.update_source(sid, sync_status="paused"))

        await off(strict_store.revise_source_properties, sid, lambda props: props.update(k=1))

        row = await off(_row, strict_store, sid)
        assert row["props"] == {"k": 1}
        assert row["sync_status"] == "paused"
        assert row["last_synced"] is None

    async def test_a_status_key_in_the_blob_is_stripped_not_applied(self, strict_store):
        """The column is the single source of truth; a revision that puts a
        ``sync_status`` key into the blob gets it dropped, and only the
        returned value reaches the column."""
        sid = await off(strict_store.add_source, "s", "webhook", "x://s", properties={})

        await off(
            strict_store.revise_source_properties,
            sid,
            lambda props: props.update(sync_status="error"),
        )

        row = await off(_row, strict_store, sid)
        assert "sync_status" not in row["props"]
        assert row["sync_status"] == "pending"

    async def test_a_missing_row_returns_none_and_never_calls_revise(self, strict_store):
        calls = []
        assert (
            await off(strict_store.revise_source_properties, "no-such-source", calls.append)
        ) is None
        assert calls == []

    async def test_merge_is_the_fixed_delta_form(self, strict_store):
        """``merge_source_properties`` is the same take with the delta fixed up
        front, ``last_synced`` included."""
        sid = await off(
            strict_store.add_source, "s", "webhook", "x://s", properties={"a": 1, "b": 2}
        )

        persisted = await off(
            lambda: strict_store.merge_source_properties(
                sid,
                set_keys={"c": 3},
                remove_keys=("b",),
                sync_status="synced",
                last_synced="2000-01-01T00:00:00",
            )
        )

        assert persisted == {"a": 1, "c": 3}
        row = await off(_row, strict_store, sid)
        assert row["props"] == persisted
        assert row["sync_status"] == "synced"
        assert row["last_synced"] == "2000-01-01T00:00:00"
