"""An LTS knowledge-pool refusal fails the ingest instead of storing it empty.

In an LTS build the knowledge pool never falls back to the Kiro CLI harness
(RFC D6): it raises :class:`KnowledgeHarnessUnavailable` instead. That refusal must
reach the ingest, which ends the job ``failed`` with the refusal's remedy, writes no
item, forgets the content hash (so the unchanged document is ingested again later),
and keeps a replaced document's old items. Any other extraction error keeps its
existing path, so an ``oss`` build is unchanged.
"""

from __future__ import annotations

import asyncio
import gc
import json
import weakref

import pytest

from kiro_crew.knowledge.chunker import HeadingAwareChunker
from kiro_crew.knowledge.extractor import EntityExtractor
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.llm_pool import (
    KnowledgeHarnessUnavailable,
    KnowledgeSpecUnconfirmed,
    LLMPool,
    LLMPoolClosed,
    Worker,
    _kiro_fallback,
)
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.testing.wait import async_wait_until, await_until, finished, until_parked


def _record_loop_errors() -> list[dict]:
    """Collect what the running loop reports, e.g. 'Task exception was never retrieved'."""
    contexts: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: contexts.append(ctx))
    return contexts


def _collect(tasks: list[asyncio.Task], refusal: BaseException) -> None:
    """Free the finished *tasks* so asyncio would now log any unretrieved exception.

    asyncio logs it only when the task is garbage-collected, so the test drops its
    own references (and the refusal's traceback, whose frames hold the batch's task
    list) and checks that every task is really gone.
    """
    assert tasks and all(t.done() for t in tasks)
    refs = [weakref.ref(t) for t in tasks]
    tasks.clear()
    refusal.__traceback__ = None
    gc.collect()
    assert [r for r in refs if r() is not None] == []


_REPLY = json.dumps(
    {"title": "t", "entities": [], "relations": [], "category": "document", "summary": "s"}
)


@pytest.fixture
def lts(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")


@pytest.fixture()
def kstore(tmp_path):
    s = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield s
    s.close()


class _Pool:
    """A pool stand-in: refuses the way the LTS pool does, or replies."""

    def __init__(self) -> None:
        self.refuse = True
        self.error: Exception | None = None

    async def send(self, prompt: str, timeout: float = 0) -> str:
        if self.error is not None:
            raise self.error
        if self.refuse:
            _kiro_fallback("kas cannot run the knowledge pool's client")
        return _REPLY

    async def send_batch(self, prompts: list[str], timeout: float = 0) -> list[str]:
        return [await self.send(p, timeout=timeout) for p in prompts]


@pytest.fixture()
def pool():
    return _Pool()


@pytest.fixture()
def pipeline(kstore, pool):
    return IngestionPipeline(
        store=kstore,
        extractor=EntityExtractor(pool=pool),  # type: ignore[arg-type]
        chunker=HeadingAwareChunker(),
        reader=FileReader(),
        embedder=None,
        dedup_enabled=False,
    )


def _job(kstore, source_id):
    return kstore.db.execute(
        "SELECT status, error FROM ingestion_jobs WHERE source_id = ? ORDER BY rowid DESC",
        (source_id,),
    ).fetchone()


def _items(kstore, source_id):
    rows = kstore.db.execute("SELECT id FROM items WHERE source_id = ?", (source_id,))
    return {r["id"] for r in rows.fetchall()}


def _props(kstore, source_id):
    row = kstore.db.execute("SELECT properties FROM sources WHERE id = ?", (source_id,)).fetchone()
    return json.loads(row["properties"] or "{}")


class TestThePoolAndExtractorPassTheRefusalOn:
    def test_send_batch_raises_the_refusal(self, lts):
        pool = LLMPool.__new__(LLMPool)

        async def _send(prompt, timeout=0, *, should_stop=None):
            _kiro_fallback("no client")

        pool.send = _send  # type: ignore[method-assign]
        with pytest.raises(KnowledgeHarnessUnavailable):
            asyncio.run(pool.send_batch(["a", "b"]))

    def test_send_batch_still_blanks_any_other_failure(self):
        pool = LLMPool.__new__(LLMPool)

        async def _send(prompt, timeout=0, *, should_stop=None):
            raise RuntimeError("worker died")

        pool.send = _send  # type: ignore[method-assign]
        assert asyncio.run(pool.send_batch(["a", "b"])) == ["", ""]

    @pytest.mark.parametrize(
        "refusal_type", [KnowledgeHarnessUnavailable, KnowledgeSpecUnconfirmed, LLMPoolClosed]
    )
    def test_a_refusal_skips_unsent_items_and_finishes_in_flight_items(self, refusal_type):
        async def _run():
            pool = LLMPool.__new__(LLMPool)
            refusal = refusal_type() if refusal_type is LLMPoolClosed else refusal_type("refused")
            b_started = asyncio.Event()
            release_b = asyncio.Event()
            reached_send: list[str] = []
            completed: list[str] = []
            cancelled: list[str] = []
            b_task: list[asyncio.Task] = []

            async def _send(prompt, timeout=0, *, should_stop=None):
                reached_send.append(prompt)
                if prompt == "a":
                    await await_until(b_started, what="b in flight before the refusal")
                    raise refusal
                if prompt == "b":
                    b_task.append(asyncio.current_task())  # type: ignore[arg-type]
                    b_started.set()
                    try:
                        await await_until(release_b, what="b released after the refusal")
                    except asyncio.CancelledError:
                        cancelled.append(prompt)
                        raise
                completed.append(prompt)
                return "reply"

            pool.send = _send  # type: ignore[method-assign]
            # b parks first; a refuses without yielding once b has started, so
            # c reaches the batch's pre-send check only after the refusal.
            try:
                with pytest.raises(refusal_type) as info:
                    await finished(pool.send_batch(["b", "a", "c"]), "the refused batch")
                assert info.value is refusal
                assert completed == []  # the batch did not wait for b
            finally:
                release_b.set()
            (b,) = b_task
            await await_until(b, what="b's turn after the batch raised")
            assert reached_send == ["b", "a"]
            assert completed == ["b"]
            assert cancelled == []
            assert not b.cancelled()

        asyncio.run(_run())

    @pytest.mark.parametrize("dead_after_refusal", [True, False])
    def test_queued_items_raise_the_first_refusal_at_checkout(
        self, monkeypatch, dead_after_refusal
    ):
        async def _run():
            pool = LLMPool(pool_size=1)
            refusal = KnowledgeHarnessUnavailable("refused")
            reached_send = []
            raised = {}
            created = []
            created_at_refusal = []

            class _Worker(Worker):
                alive = True

                async def start(self):
                    pass

                async def shutdown(self):
                    self.alive = False

                def is_alive(self):
                    return self.alive

                async def reset_conversation(self):
                    pass

                async def send_message(self, prompt, timeout=0):
                    reached_send.append(prompt)
                    if prompt == "a":
                        await until_parked(pool._semaphore, count=2)
                        created_at_refusal.append(len(created))
                        self.alive = not dead_after_refusal
                        raise refusal
                    return "reply"

            async def _create_worker():
                worker = _Worker()
                created.append(worker)
                return worker

            real_send = pool.send
            item_tasks: list[asyncio.Task] = []

            async def _send(prompt, **kwargs):
                item_tasks.append(asyncio.current_task())  # type: ignore[arg-type]
                try:
                    return await real_send(prompt, **kwargs)
                except KnowledgeHarnessUnavailable as exc:
                    raised[prompt] = exc
                    raise

            monkeypatch.setattr("kiro_crew.knowledge.llm_pool._read_config", lambda: {})
            monkeypatch.setattr(pool, "_create_worker", _create_worker)
            monkeypatch.setattr(pool, "send", _send)
            loop_errors = _record_loop_errors()
            try:
                await pool.start()
                with pytest.raises(KnowledgeHarnessUnavailable) as info:
                    await finished(pool.send_batch(["a", "b", "c"]), "queued refusal batch")
                assert info.value is refusal
                del info
                # The batch raised at a's refusal; b and c stop at checkout after it.
                await async_wait_until(
                    lambda: len(item_tasks) == 3 and all(t.done() for t in item_tasks),
                    describe=lambda: item_tasks,
                )
                assert not any(t.cancelled() for t in item_tasks)
                _collect(item_tasks, refusal)
                assert loop_errors == []
                assert len(created) == created_at_refusal[0] == 1
                assert reached_send == ["a"]
                assert set(raised) == {"a", "b", "c"}
                assert all(exc is refusal for exc in raised.values())
                assert pool._in_use == 0
                assert pool._available.qsize() == 1
                assert created[0].calls_since_reset == 1
                # Every stopped checkout returned its slot; an ordinary send still works.
                created[0].alive = True
                assert await finished(pool.send("d"), "send after refused batch") == "reply"
            finally:
                await pool.shutdown()

        asyncio.run(_run())

    def test_cold_pool_waiters_raise_the_first_startup_refusal(self, monkeypatch):
        async def _run():
            pool = LLMPool(pool_size=1)
            refusal = KnowledgeHarnessUnavailable("refused")
            created = 0

            async def _create_worker():
                nonlocal created
                created += 1
                if created == 1:
                    await until_parked(pool._start_lock, count=2)
                raise refusal

            real_send = pool.send
            item_tasks: list[asyncio.Task] = []

            async def _send(prompt, **kwargs):
                item_tasks.append(asyncio.current_task())  # type: ignore[arg-type]
                return await real_send(prompt, **kwargs)

            monkeypatch.setattr("kiro_crew.knowledge.llm_pool._read_config", lambda: {})
            monkeypatch.setattr(pool, "_create_worker", _create_worker)
            monkeypatch.setattr(pool, "send", _send)
            loop_errors = _record_loop_errors()
            try:
                with pytest.raises(KnowledgeHarnessUnavailable) as info:
                    await finished(pool.send_batch(["a", "b", "c"]), "cold pool refusal batch")
                assert info.value is refusal
                del info
                # b and c were parked on the startup lock; they stop under it.
                await async_wait_until(
                    lambda: len(item_tasks) == 3 and all(t.done() for t in item_tasks),
                    describe=lambda: item_tasks,
                )
                assert not any(t.cancelled() for t in item_tasks)
                _collect(item_tasks, refusal)
                assert loop_errors == []
                assert created == 1
                assert not pool._started
                assert pool._workers == []
                assert pool._available.empty()
                assert pool._in_use == 0
                assert not pool._semaphore.locked()
                assert not pool._start_lock.locked()
            finally:
                await pool.shutdown()

        asyncio.run(_run())

    def test_stop_before_start_holds_no_slot(self, monkeypatch):
        async def _run():
            pool = LLMPool(pool_size=1)
            refusal = KnowledgeHarnessUnavailable("refused")

            async def _create_worker():
                pytest.fail("a stopped checkout must not start a worker")

            monkeypatch.setattr(pool, "_create_worker", _create_worker)
            try:
                with pytest.raises(KnowledgeHarnessUnavailable) as info:
                    await pool.send("a", should_stop=lambda: refusal)
                assert info.value is refusal
                assert not pool._started
                assert pool._in_use == 0
                assert pool._available.empty()
                assert not pool._semaphore.locked()
            finally:
                await pool.shutdown()

        asyncio.run(_run())

    def test_real_pool_finishes_an_in_flight_turn_after_refusal(self, monkeypatch):
        async def _run():
            pool = LLMPool(pool_size=2)
            refusal = KnowledgeHarnessUnavailable("refused")
            b_started = asyncio.Event()
            release_b = asyncio.Event()
            completed = []
            cancelled = []
            item_tasks: dict[str, asyncio.Task] = {}

            class _Worker(Worker):
                async def start(self):
                    pass

                async def shutdown(self):
                    pass

                def is_alive(self):
                    return True

                async def reset_conversation(self):
                    pass

                async def send_message(self, prompt, timeout=0):
                    if prompt == "a":
                        await await_until(b_started, what="b's backend turn started")
                        raise refusal
                    b_started.set()
                    try:
                        await await_until(release_b, what="b's backend turn released")
                    except asyncio.CancelledError:
                        cancelled.append(prompt)
                        raise
                    completed.append(prompt)
                    return "reply"

            async def _create_worker():
                return _Worker()

            real_send = pool.send

            async def _send(prompt, **kwargs):
                item_tasks[prompt] = asyncio.current_task()  # type: ignore[assignment]
                return await real_send(prompt, **kwargs)

            monkeypatch.setattr("kiro_crew.knowledge.llm_pool._read_config", lambda: {})
            monkeypatch.setattr(pool, "_create_worker", _create_worker)
            monkeypatch.setattr(pool, "send", _send)
            loop_errors = _record_loop_errors()
            try:
                try:
                    with pytest.raises(KnowledgeHarnessUnavailable) as info:
                        await finished(pool.send_batch(["a", "b"]), "the refused batch")
                    assert info.value is refusal
                    del info
                    # The batch raised while b's backend turn still runs.
                    b = item_tasks["b"]
                    assert not b.done()
                    assert completed == []
                    assert pool._in_use == 1
                finally:
                    release_b.set()
                await await_until(b, what="b's backend turn after the batch raised")
                assert completed == ["b"]
                assert cancelled == []
                assert not b.cancelled()
                # b released its worker: no slot held, both indices back in the queue.
                assert pool._in_use == 0
                assert sorted(pool._available._queue) == [0, 1]
                del b
                tasks = list(item_tasks.values())
                item_tasks.clear()
                _collect(tasks, refusal)
                assert loop_errors == []
            finally:
                await pool.shutdown()

        asyncio.run(_run())

    def test_extractor_raises_the_refusal(self, lts, pool):
        extractor = EntityExtractor(pool=pool)  # type: ignore[arg-type]
        with pytest.raises(KnowledgeHarnessUnavailable):
            asyncio.run(extractor.extract_batch(["text"]))
        with pytest.raises(KnowledgeHarnessUnavailable):
            asyncio.run(extractor.extract("text"))


class TestLtsRefusalFailsTheIngest:
    def test_new_file_job_fails_and_the_hash_is_not_kept(
        self, lts, pipeline, pool, kstore, tmp_path
    ):
        doc = tmp_path / "doc.md"
        doc.write_text("# Heading\n\nSome body text for the document.", encoding="utf-8")
        with pytest.raises(KnowledgeHarnessUnavailable):
            asyncio.run(pipeline.ingest_file(str(doc)))
        source = kstore.get_source_by_uri(str(doc.resolve()))
        job = _job(kstore, source["id"])
        assert job["status"] == "failed"
        assert "kiro-cli update" in job["error"]
        assert _items(kstore, source["id"]) == set()
        assert "content_hash" not in _props(kstore, source["id"])

        # Once the harness is fixed the unchanged document is ingested, not skipped.
        pool.refuse = False
        assert asyncio.run(pipeline.ingest_file(str(doc))) is not None
        assert _items(kstore, source["id"])

    def test_hash_cleanup_is_a_locked_delta_that_keeps_other_properties(
        self, lts, pipeline, pool, kstore, tmp_path, monkeypatch
    ):
        doc = tmp_path / "doc.md"
        doc.write_text("# Heading\n\nSome body text for the document.", encoding="utf-8")
        calls = []
        real = kstore.revise_source_properties

        def _revise(source_id, revise, **kw):
            # A pause lands on the row just before the cleanup's locked take.
            real(source_id, lambda p: p.update(scan_paused=True))
            calls.append(source_id)
            return real(source_id, revise, **kw)

        monkeypatch.setattr(kstore, "revise_source_properties", _revise)
        with pytest.raises(KnowledgeHarnessUnavailable):
            asyncio.run(pipeline.ingest_file(str(doc)))
        source = kstore.get_source_by_uri(str(doc.resolve()))
        assert calls == [source["id"]]
        props = _props(kstore, source["id"])
        assert props.get("scan_paused") is True
        assert "content_hash" not in props

    def test_replace_ingest_keeps_the_old_items(self, lts, pipeline, pool, kstore, tmp_path):
        doc = tmp_path / "doc.md"
        doc.write_text("# Heading\n\nThe first version.", encoding="utf-8")
        pool.refuse = False
        asyncio.run(pipeline.ingest_file(str(doc)))
        source = kstore.get_source_by_uri(str(doc.resolve()))
        old = _items(kstore, source["id"])
        assert old

        doc.write_text("# Heading\n\nThe second version.", encoding="utf-8")
        pool.refuse = True
        with pytest.raises(KnowledgeHarnessUnavailable):
            asyncio.run(pipeline.ingest_file(str(doc)))
        assert _items(kstore, source["id"]) == old
        assert _job(kstore, source["id"])["status"] == "failed"

    def test_text_job_fails_and_the_hash_is_not_kept(self, lts, pipeline, pool, kstore):
        with pytest.raises(KnowledgeHarnessUnavailable):
            asyncio.run(pipeline.ingest_text("Some body text for the note.", "note"))
        source = kstore.db.execute("SELECT id FROM sources WHERE name = 'note'").fetchone()
        job = _job(kstore, source["id"])
        assert job["status"] == "failed"
        assert "kiro-cli update" in job["error"]
        assert _items(kstore, source["id"]) == set()
        assert "content_hash" not in _props(kstore, source["id"])

        pool.refuse = False
        assert asyncio.run(pipeline.ingest_text("Some body text for the note.", "note")) is not None


class TestOssIsUnchanged:
    def test_oss_never_refuses(self, pipeline, kstore, tmp_path):
        doc = tmp_path / "doc.md"
        doc.write_text("# Heading\n\nSome body text.", encoding="utf-8")
        # In oss the fallback is Kiro CLI, not a refusal.
        assert _kiro_fallback("any") == ""

    def test_oss_other_extraction_error_still_stores_the_document(
        self, pipeline, pool, kstore, tmp_path
    ):
        pool.error = RuntimeError("worker died")
        doc = tmp_path / "doc.md"
        doc.write_text("# Heading\n\nSome body text.", encoding="utf-8")
        assert asyncio.run(pipeline.ingest_file(str(doc))) is not None
        source = kstore.get_source_by_uri(str(doc.resolve()))
        assert _job(kstore, source["id"])["status"] == "completed"
        assert _props(kstore, source["id"]).get("content_hash")
