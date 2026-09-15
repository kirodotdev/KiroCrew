"""Remote sync and the agent document upsert must take no store connection on the loop.

``KnowledgeStore.db`` is a per-thread autocommit sqlite connection with a 10s
``busy_timeout``. Taken on the gateway event loop, a query that waits on the
writer lock blocks every task -- the watchdog heartbeat included -- for that
whole wait, and past ``dashboard.loop_stall_exit_after_secs`` the watchdog kills
the gateway.

``SyncScheduler.sync_source`` and ``agent_source._add_agent_document`` are both
async bodies scheduled on that loop, and each reached the store through plain
sync helpers (``_get_source``, ``ensure_agent_source``, ``get_state``,
``find_document_by_hash``, ``release_stale_claim``, ``get_job_status``,
``update_source``, ``_record_failure``) -- the connection take one frame down
that the lexical sync-io-in-async gate cannot see. These tests arm the store's
strict switch, which turns the on-loop warning into ``OnLoopStoreError``, and
drive both paths end to end on a running loop; any take that slips back onto
the loop raises instead of logging.

This mirrors ``test_folder_watcher_off_loop_guard.py``: same ``strict_store``
fixture, same mocked-pipeline builder, and the tests' OWN store access goes
through :func:`off` so a test-side take is never mistaken for a regression.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.knowledge.agent_source import (
    add_agent_document,
    document_slug,
    ensure_agent_source,
    set_state,
)
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.knowledge.sync import MAX_FAILURES, SyncScheduler
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


def _pipeline(store: KnowledgeStore) -> IngestionPipeline:
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
    reader.read.return_value = ("some body", {})
    return IngestionPipeline(
        store=store, extractor=extractor, chunker=chunker, reader=reader, embedder=None
    )


def _connector(fetch_side_effect=None) -> MagicMock:
    connector = MagicMock(detect_changes=AsyncMock(return_value=True))
    if fetch_side_effect is not None:
        connector.fetch = AsyncMock(side_effect=fetch_side_effect)
    else:
        connector.fetch = AsyncMock(return_value=("remote body text", {"etag": "1"}))
    return connector


def _props(store: KnowledgeStore, sid: str) -> dict:
    raw = store.db.execute("SELECT properties FROM sources WHERE id = ?", (sid,)).fetchone()[
        "properties"
    ]
    return json.loads(raw or "{}")


def _sync_status(store: KnowledgeStore, sid: str) -> str:
    return store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()[
        "sync_status"
    ]


async def test_strict_guard_is_actually_armed(strict_store):
    """An on-loop take must raise, or every test below passes vacuously."""
    with pytest.raises(OnLoopStoreError):
        strict_store.db
    # ...and the same take off-loop is the sanctioned path.
    assert await off(lambda: strict_store.db) is not None


class TestSyncSourceOffLoop:
    async def test_sync_source_takes_no_connection_on_the_loop(self, strict_store):
        """The whole success path -- source lookup, ingest, status read, source
        update -- runs under the armed guard. ``sync_source`` swallows exceptions
        into ``result['error']``, so the assertion is on a clean result, not on
        "did not raise"."""
        sid = await off(strict_store.add_source, "remote", "webhook", "x://remote")
        sched = SyncScheduler(strict_store, _pipeline(strict_store), {"webhook": _connector()})

        res = await sched.sync_source(sid)

        assert res["error"] is None
        assert res["synced"] is True
        assert res["items_created"] == 1
        assert (await off(_props, strict_store, sid))["consecutive_failures"] == 0

    async def test_a_missing_source_is_reported_without_a_loop_take(self, strict_store):
        sched = SyncScheduler(strict_store, _pipeline(strict_store), {})
        res = await sched.sync_source("nope")
        assert res["error"] == "Source nope not found"
        assert res["synced"] is False

    async def test_a_failed_sync_records_the_failure_off_loop(self, strict_store):
        """The except path takes the store too (``_record_failure`` reads and
        rewrites the source row). An on-loop take THERE raises out of the
        handler, so this test fails loudly on a regression."""
        sid = await off(strict_store.add_source, "remote", "webhook", "x://remote")
        connector = _connector(fetch_side_effect=RuntimeError("boom"))
        sched = SyncScheduler(strict_store, _pipeline(strict_store), {"webhook": connector})

        res = await sched.sync_source(sid)

        assert res["error"] == "boom"
        assert (await off(_props, strict_store, sid))["consecutive_failures"] == 1

    async def test_concurrent_failure_records_lose_no_increment(self, strict_store):
        """Off the loop, the counter's read and write are two steps on two
        threads. Unserialised, two concurrent failures both read N and both
        write N+1, so the count never reaches MAX_FAILURES and a dead source is
        never quiesced. The barrier makes both workers finish the read before
        either writes -- with the lock the second worker waits instead (the
        barrier times out harmlessly), and both increments land."""
        sid = await off(strict_store.add_source, "remote", "webhook", "x://remote")
        sched = SyncScheduler(strict_store, _pipeline(strict_store), {})

        both_read = threading.Barrier(2)
        orig_get = sched._get_source

        def rendezvous_get(source_id):
            row = orig_get(source_id)
            try:
                both_read.wait(timeout=1.0)
            except threading.BrokenBarrierError:
                pass  # serialised execution: the other worker never arrives
            return row

        sched._get_source = rendezvous_get  # type: ignore[method-assign]
        await asyncio.gather(
            asyncio.to_thread(sched._record_failure, sid),
            asyncio.to_thread(sched._record_failure, sid),
        )

        assert (await off(_props, strict_store, sid))["consecutive_failures"] == 2

    async def test_a_stale_failure_cannot_quiesce_a_source_after_a_success(self, strict_store):
        """A failure that read the counter BEFORE a success's reset must not
        land its stale increment -- and a premature 'error' -- AFTER it. Seed
        the counter at MAX_FAILURES - 1 and force exactly that interleaving:
        the failure's read is held until the success write completes.
        Unserialised, the failure then writes MAX_FAILURES + 'error' over the
        fresh success, quiescing a healthy source with no automatic recovery.
        Under the shared outcome lock either order ends non-error: a
        failure-first run stamps 'error' and the success supersedes it with
        'synced'; a success-first run has the failure read the reset counter."""
        sid = await off(strict_store.add_source, "remote", "webhook", "x://remote")
        await off(
            lambda: strict_store.update_source(
                sid, properties={"consecutive_failures": MAX_FAILURES - 1}
            )
        )
        sched = SyncScheduler(strict_store, _pipeline(strict_store), {})

        success_written = threading.Event()
        in_failure = threading.local()
        orig_get = sched._get_source

        def gated_get(source_id):
            row = orig_get(source_id)
            if getattr(in_failure, "active", False):
                # Hold the failure's write until the success write has landed
                # (or the lock has correctly kept the success out: timeout).
                success_written.wait(timeout=1.0)
            return row

        def failure_entry(source_id):
            in_failure.active = True
            sched._record_failure(source_id)

        sched._get_source = gated_get  # type: ignore[method-assign]

        async def run_success():
            await asyncio.to_thread(lambda: sched._record_success(sid, None, completed=True))
            success_written.set()

        await asyncio.gather(run_success(), asyncio.to_thread(failure_entry, sid))

        assert (await off(_sync_status, strict_store, sid)) != "error"

    async def test_a_partial_ingest_does_not_get_stamped_synced(self, strict_store):
        """The ingest's own finalize stamps 'error' on a partial or failed
        ingest and still hands back a job id. The outcome write must not
        overwrite that with 'synced': only a job whose status is 'completed'
        may supersede the column."""
        sid = await off(strict_store.add_source, "remote", "webhook", "x://remote")
        pipeline = _pipeline(strict_store)
        pipeline.ingest_text = AsyncMock(return_value="job-1")  # type: ignore[method-assign]
        pipeline.get_job_status = MagicMock(  # type: ignore[method-assign]
            return_value={"status": "partial", "items_processed": 1}
        )
        sched = SyncScheduler(strict_store, pipeline, {"webhook": _connector()})
        # What the real finalize leaves behind on a partial ingest.
        await off(lambda: strict_store.update_source(sid, sync_status="error"))

        res = await sched.sync_source(sid)

        assert res["error"] is None
        assert (await off(_sync_status, strict_store, sid)) == "error"
        # The counter reset and last_synced still land, as they always have.
        assert (await off(_props, strict_store, sid))["consecutive_failures"] == 0

    async def test_a_cancelled_sync_still_records_the_outcome(self, strict_store):
        """A cancellation landing while the outcome unit is still QUEUED in the
        executor must not drop the write: a dropped success reset leaves a
        stale counter, and one later transient failure trips MAX_FAILURES and
        quiesces a healthy source. Pin the loop to a one-worker executor, park
        a blocker in it so the outcome unit queues behind it, cancel the sync,
        then release -- the drained unit must still land the reset."""
        loop = asyncio.get_running_loop()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        try:
            sid = await off(strict_store.add_source, "remote", "webhook", "x://remote")
            await off(
                lambda: strict_store.update_source(
                    sid, properties={"consecutive_failures": MAX_FAILURES - 1}
                )
            )
            pipeline = _pipeline(strict_store)
            pipeline.get_job_status = MagicMock(  # type: ignore[method-assign]
                return_value={"status": "completed", "items_processed": 1}
            )
            release = threading.Event()
            ingested = asyncio.Event()

            async def _ingest(*_a, **_k):
                # Park a blocker in the single worker, so the outcome unit that
                # follows can only ever QUEUE behind it.
                loop.run_in_executor(None, release.wait)
                ingested.set()
                return "job-1"

            pipeline.ingest_text = _ingest  # type: ignore[method-assign]
            sched = SyncScheduler(strict_store, pipeline, {"webhook": _connector()})

            task = asyncio.ensure_future(sched.sync_source(sid))
            await asyncio.wait_for(ingested.wait(), timeout=10)
            for _ in range(5):  # let the coroutine reach the outcome await
                await asyncio.sleep(0)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)

            assert (await off(_props, strict_store, sid))["consecutive_failures"] == 0
        finally:
            release.set()
            executor.shutdown(wait=False)


class TestAgentDocumentUpsertOffLoop:
    async def test_document_upsert_takes_no_connection_on_the_loop(self, strict_store):
        """The full add -- ensure-source, state read, twin check, stale-claim
        release, real ingest, status read -- on a running loop under the armed
        guard. ``add_agent_document`` has no blanket except, so any on-loop
        take raises straight out."""
        res = await add_agent_document(
            _pipeline(strict_store),
            title="Design doc",
            content="alpha body",
            reason="load-bearing",
            source_uri="https://wiki.example/design",
        )
        assert res["status"] == "added"
        assert res["items"] == 1

    async def test_an_unchanged_re_add_short_circuits_off_loop(self, strict_store):
        """The duplicate shortcut is a second pass over ``ensure_agent_source``
        and ``get_state`` plus the backfill hop; it must stay off-loop too."""
        pipeline = _pipeline(strict_store)
        kwargs = dict(
            title="Design doc",
            content="alpha body",
            reason="",
            source_uri="https://wiki.example/design",
        )
        first = await add_agent_document(pipeline, **kwargs)
        assert first["status"] == "added"

        again = await add_agent_document(pipeline, **kwargs)
        assert again["status"] == "duplicate"
        assert again["reason"] == "unchanged since last add"

    async def test_identical_content_under_a_second_uri_is_refused_off_loop(self, strict_store):
        """The twin check (``find_document_by_hash``) is a store read on the
        pre-ingest path; it must run on a worker thread."""
        pipeline = _pipeline(strict_store)
        first = await add_agent_document(
            pipeline, title="A", content="same text", source_uri="https://x/a"
        )
        assert first["status"] == "added"

        second = await add_agent_document(
            pipeline, title="B", content="same text", source_uri="https://x/b"
        )
        assert second["status"] == "duplicate"
        assert "identical content" in second["reason"]

    async def test_an_edited_document_releases_its_stale_claim_off_loop(self, strict_store):
        """``release_stale_claim`` early-returns unless the row owned nothing and
        the hash moved, so the plain add above never reaches its store take.
        Seed exactly that row -- an empty group with a stale hash, the shape a
        lost dedup leaves -- and re-add with new content, so the release performs
        its real detach under the armed guard."""
        pipeline = _pipeline(strict_store)
        source_id, _ = await off(ensure_agent_source, strict_store)
        uri = "https://wiki.example/edited"
        slug = document_slug(uri)
        await off(
            lambda: set_state(
                strict_store, source_id, slug, "stale-hash", [], "Edited", source_uri=uri
            )
        )

        res = await add_agent_document(
            pipeline, title="Edited", content="fresh body", source_uri=uri
        )
        assert res["status"] == "added"
        assert res["items"] == 1
