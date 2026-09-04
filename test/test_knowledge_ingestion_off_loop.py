"""``IngestionPipeline`` must take no knowledge-store connection on the loop (#7019).

The store's ``db`` accessor is the one chokepoint every query funnels through, and
since #7640 it reports an on-loop take. ``knowledge/ingestion.py`` was the second
largest holder of those takes: 15 recorded in
``.github/sync-io-in-async-baseline.txt`` plus the interprocedural ones no
name-based AST scan can see (``add_item``, ``add_source_location``,
``get_source_by_uri``, ``add_source`` are plain ``def``\\ s that reach ``self.db``
one frame down). ``add_item`` opens a ``BEGIN`` write transaction, so on the loop it
blocked every other session's turn for the connection's whole 10s busy timeout, and
a stall past ``dashboard.loop_stall_exit_after_secs`` (25s) makes the watchdog kill
the gateway (#1572).

Why this shape of test. Asserting "no exception escaped" would be VACUOUS here: the
per-chunk body catches ``Exception`` and logs it, so a strict-mode
``OnLoopStoreError`` from ``add_item`` is swallowed and the coroutine returns
normally. These tests instead swap in a guard that RECORDS every on-loop take and
proceeds, so a violation is reported with its stack no matter which ``except`` sits
above it -- and each test additionally asserts the work actually landed, so a
pipeline that silently does nothing cannot pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import threading
import time
import traceback
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.knowledge import store as store_mod
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore


class _RecordingGuard:
    """Stands in for the store's module-level guard, recording on-loop takes.

    Deliberately records-and-proceeds rather than raising: a raise would be
    caught by the pipeline's per-chunk ``except Exception`` and the violation
    would vanish. Off-loop takes are the sanctioned path and are ignored, exactly
    as the real guard ignores them -- and so is a take inside an
    ``allow_on_loop()`` block (the store constructor's vetted schema init,
    #8231), mirroring the real guard so construction noise cannot masquerade
    as a pipeline violation.
    """

    def __init__(self) -> None:
        self.takes: list[str] = []
        self._allow: contextvars.ContextVar[bool] = contextvars.ContextVar(
            "recording_guard_allow_on_loop", default=False
        )

    def check(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # off-loop: the sanctioned path
        if self._allow.get():
            return  # inside a vetted allow_on_loop() block, as the real guard
        self.takes.append("".join(traceback.format_stack(limit=8)))

    @contextlib.contextmanager
    def allow_on_loop(self) -> Iterator[None]:
        # Value-based restore, not token reset: semgrep/contextvar-token-reset
        # bans token-based reset under test/** because pytest-xdist shares the
        # worker's main-thread Context across tests. (The production guard
        # keeps the token form -- the rule's own comment calls that canonical.)
        previous = self._allow.get()
        self._allow.set(True)
        try:
            yield
        finally:
            self._allow.set(previous)

    def reset_throttle(self) -> None:  # pragma: no cover - API parity only
        pass

    def report(self, takes: list[str] | None = None) -> str:
        rows = self.takes if takes is None else takes
        return f"{len(rows)} on-loop store take(s):\n\n" + "\n---\n".join(rows)


@pytest.fixture()
def guard(monkeypatch) -> _RecordingGuard:
    """Swap the store's guard for a recorder.

    ``KnowledgeStore.db`` reads the module global, so patching the attribute
    covers every store instance the pipeline touches.
    """
    rec = _RecordingGuard()
    monkeypatch.setattr(store_mod, "_ON_LOOP_DB_GUARD", rec)
    return rec


@pytest.fixture()
def kstore(tmp_path):
    # Built OFF the loop on purpose: __init__ runs schema init, migrations and the
    # graph load through self.db, so constructing one inside an async test would
    # itself be an on-loop take and would mask the thing under test.
    s = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield s
    s.close()


def _two_chunks(text, **kw):
    half = max(1, len(text) // 2)
    return [
        {
            "content": text[:half],
            "chunk_index": 0,
            "section_title": None,
            "line_start": 0,
            "line_end": 0,
        },
        {
            "content": text[half:],
            "chunk_index": 1,
            "section_title": None,
            "line_start": 1,
            "line_end": 1,
        },
    ]


def _extraction():
    return {"category": "document", "summary": "s", "entities": []}


class _Embedder:
    """Minimal stand-in so ``_embed_item`` reaches its UPDATE + commit."""

    model = "test-model"
    content_budget = 128

    def embed_for_item(self, title, summary, content):
        return [0.1, 0.2, 0.3]


@pytest.fixture()
def pipeline(kstore):
    extractor = MagicMock()
    extractor._pool = None
    extractor.extract_batch = AsyncMock(return_value=[_extraction(), _extraction()])
    chunker = MagicMock()
    for attr in ("chunk", "chunk_markdown", "chunk_code", "chunk_slides"):
        getattr(chunker, attr).side_effect = _two_chunks
    return IngestionPipeline(
        store=kstore,
        extractor=extractor,
        chunker=chunker,
        reader=FileReader(),
        embedder=_Embedder(),
    )


def _items(kstore, source_id):
    return kstore.db.execute(
        "SELECT id, embedding FROM items WHERE source_id = ?", (source_id,)
    ).fetchall()


class TestHarness:
    """The recorder must actually catch a take, or every test below is vacuous."""

    @pytest.mark.asyncio
    async def test_recorder_catches_an_on_loop_take(self, guard, kstore):
        kstore.db.execute("SELECT 1").fetchone()
        assert guard.takes, "recorder saw nothing: the guard swap did not take effect"


class TestIngestFile:
    @pytest.mark.asyncio
    async def test_new_source_ingest_stays_off_loop(self, pipeline, kstore, guard, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("# heading\nbody text long enough to split", encoding="utf-8")

        # This async test body sets up through the store, which is itself an
        # on-loop take and not the subject: only what the pipeline does counts.
        guard.takes.clear()
        job_id = await pipeline.ingest_file(str(f))
        # Snapshot BEFORE the outcome assertions: those read the store from this
        # async test body, which is itself an on-loop take and not the subject.
        violations = list(guard.takes)

        assert job_id is not None
        assert (pipeline.get_job_status(job_id) or {}).get("status") == "completed"
        src = kstore.get_source_by_uri(str(f.resolve()))
        assert src is not None, "source row was never created"
        rows = _items(kstore, src["id"])
        assert len(rows) == 2, "both chunks must land, or the assertion above is vacuous"
        assert all(r["embedding"] for r in rows), "_embed_item never stamped the rows"
        assert not violations, guard.report(violations)

    @pytest.mark.asyncio
    async def test_existing_source_ingest_stays_off_loop(self, pipeline, kstore, guard, tmp_path):
        # Exercises the source_id branch: the SELECT on sources, then the whole
        # chunk loop against an existing aggregate source.
        source_id = kstore.add_source("agg", "local_folder", str(tmp_path))
        f = tmp_path / "doc.md"
        f.write_text("# heading\nbody text long enough to split", encoding="utf-8")

        # This async test body sets up through the store, which is itself an
        # on-loop take and not the subject: only what the pipeline does counts.
        guard.takes.clear()
        job_id = await pipeline.ingest_file(str(f), source_id=source_id, old_item_ids=[])
        violations = list(guard.takes)

        assert job_id is not None
        assert (pipeline.get_job_status(job_id) or {}).get("status") == "completed"
        assert len(_items(kstore, source_id)) == 2
        assert not violations, guard.report(violations)

    @pytest.mark.asyncio
    async def test_failure_path_marks_the_job_off_loop(self, pipeline, kstore, guard, tmp_path):
        # The 'failed' stamp is what stops the folder watcher retrying the file
        # every scan, and it used to run two blocking statements on the loop.
        async def _boom(**kw):
            raise RuntimeError("boom")

        pipeline._ingest_file_body = _boom
        f = tmp_path / "doc.md"
        f.write_text("# heading\nbody", encoding="utf-8")

        # This async test body sets up through the store, which is itself an
        # on-loop take and not the subject: only what the pipeline does counts.
        guard.takes.clear()
        with pytest.raises(RuntimeError, match="boom"):
            await pipeline.ingest_file(str(f))
        violations = list(guard.takes)

        src = kstore.get_source_by_uri(str(f.resolve()))
        assert src is not None
        row = kstore.db.execute(
            "SELECT status FROM ingestion_jobs WHERE source_id = ?", (src["id"],)
        ).fetchone()
        assert row is not None and row["status"] == "failed", "the failed stamp never landed"
        assert (
            kstore.db.execute(
                "SELECT sync_status FROM sources WHERE id = ?", (src["id"],)
            ).fetchone()["sync_status"]
            == "error"
        )
        assert not violations, guard.report(violations)


class TestIngestText:
    @pytest.mark.asyncio
    async def test_text_ingest_stays_off_loop(self, pipeline, kstore, guard):
        # This async test body sets up through the store, which is itself an
        # on-loop take and not the subject: only what the pipeline does counts.
        guard.takes.clear()
        job_id = await pipeline.ingest_text("body text long enough to split", "a title")
        violations = list(guard.takes)

        assert job_id is not None
        assert (pipeline.get_job_status(job_id) or {}).get("status") == "completed"
        rows = kstore.db.execute("SELECT id FROM items").fetchall()
        assert len(rows) == 2
        assert not violations, guard.report(violations)


class TestSourceSummary:
    @pytest.mark.asyncio
    async def test_summary_read_and_write_stay_off_loop(self, pipeline, kstore, guard):
        source_id = kstore.add_source("s", "manual", "manual://x")
        kstore.add_item("t", "c", "document", source_id=source_id, summary="a summary")
        pipeline.extractor._pool = MagicMock()
        pipeline.extractor._pool.send = AsyncMock(
            return_value='{"topic": "a topic", "themes": ["x", "y", "z"]}'
        )

        # This async test body sets up through the store, which is itself an
        # on-loop take and not the subject: only what the pipeline does counts.
        guard.takes.clear()
        await pipeline.generate_source_summary(source_id)
        violations = list(guard.takes)

        row = kstore.db.execute(
            "SELECT summary_topic FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        assert row["summary_topic"] == "a topic", "the summary write never landed"
        assert not violations, guard.report(violations)


class TestChunkWriteIsOneUncancellableUnit:
    """The item COMMIT and the record of it must not come apart under cancellation.

    Offloading is not free: ``asyncio.to_thread`` does not stop a worker that has
    already started, so a naive two-hop offload (``add_item``, then append, then
    ``add_source_location``) opens a window the on-loop version never had. A
    gateway shutdown landing between ``add_item``'s COMMIT and the coroutine
    resuming would skip the append and every finalizer above it, leaving a
    searchable item that no rollback can name and the next scan re-ingests --
    duplicating it while the first copy stays untracked.

    ``run_to_completion`` around the whole unit closes it: the hop is drained
    before the cancellation proceeds, so the commit, the append and the location
    write always travel together.
    """

    @pytest.mark.asyncio
    async def test_cancel_after_the_commit_still_writes_the_location(self, pipeline, kstore):
        committed = threading.Event()
        real_add_item = kstore.add_item

        def _add_item_then_stall(*a, **kw):
            # The cancellation is aimed at exactly the gap this test exists for:
            # the item is committed, and the awaiting coroutine has not resumed.
            new_id = real_add_item(*a, **kw)
            committed.set()
            time.sleep(0.3)
            return new_id

        kstore.add_item = _add_item_then_stall

        task = asyncio.create_task(pipeline.ingest_text("body text long enough to split", "t"))
        assert await asyncio.to_thread(committed.wait, 10), "the first chunk never committed"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        items = kstore.db.execute("SELECT id FROM items").fetchall()
        assert items, "add_item's commit vanished, so this test proves nothing"
        located = {
            r["item_id"]
            for r in kstore.db.execute("SELECT item_id FROM source_locations").fetchall()
        }
        assert items[0]["id"] in located, (
            "the write unit was torn in half: the item committed but its source location "
            "was skipped, so nothing can name it"
        )
