"""The knowledge re-embed sweep paces itself; an explicitly-requested one does not.

Locks in the behaviour that keeps an unattended watcher self-heal sweep from
pinning several cores for the whole corpus: every bulk row embeds at
``PRIORITY_BULK`` — the scheduling class that earns it the reduced
``memory.embedding_bulk_threads`` pool — and is followed by an idle window
proportional to the work it just did, taken as an ``await asyncio.sleep`` on the
sweep's own coroutine (holding neither the DB connection nor the model). Both
are keyed on attendance, not corpus size: the dashboard-triggered rebuild a
human is watching runs unpaced at ``PRIORITY_NORMAL`` on the full interactive
pool. Mirrors ``test_vector_memory_bulk_pacing.py`` for the knowledge corpus.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from kiro_crew import embeddings as _embeddings
from kiro_crew.embeddings import PRIORITY_BULK, PRIORITY_NORMAL
from kiro_crew.knowledge import ingestion
from kiro_crew.knowledge.embedder import InProcessEmbedder
from kiro_crew.knowledge.ingestion import rebuild_embeddings
from kiro_crew.knowledge.store import KnowledgeStore

# One sentinel delay for the whole file: every test that arms pacing stubs
# ``bulk_pace_delay`` to return exactly this value, and the recorders below
# act only on it. The patch necessarily replaces the GLOBAL ``asyncio.sleep``
# (``ingestion`` imports the module itself, so there is no per-module alias to
# scope to) — discriminating on the sentinel keeps any unrelated coroutine's
# sleep out of the record and leaves its real delay semantics intact.
_PACE = 0.125


class _RecordingBackend:
    """Shared-backend stub recording the scheduling class of every embed.

    Standing in for the real backend at ``get_shared_embedder`` means the tests
    drive the REAL knowledge embedder, so a priority that is dropped anywhere
    along ``rebuild_embeddings -> embed_for_item -> embed -> backend.embed``
    fails the assertion — a flag merely stored on the way proves nothing.
    """

    model_id = "fake-embed"

    def __init__(self):
        self.priorities: list[int] = []

    def is_ready(self) -> bool:
        return True

    def embed(self, text, *, priority=PRIORITY_NORMAL):
        self.priorities.append(priority)
        return [0.1, 0.2, 0.3, 0.4]


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture()
def backend(monkeypatch):
    b = _RecordingBackend()
    monkeypatch.setattr(_embeddings, "get_shared_embedder", lambda: b)
    return b


@pytest.fixture()
def embedder(backend):
    return InProcessEmbedder()


@pytest.fixture()
def sleeps(monkeypatch):
    """Record pace sleeps (the ``_PACE`` sentinel only) instead of performing them."""
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def _record(delay, *args, **kwargs):
        if delay == _PACE:
            recorded.append(delay)
            await real_sleep(0)
            return
        await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(ingestion.asyncio, "sleep", _record)
    return recorded


def _stale_items(store, n):
    for i in range(n):
        store.add_item(f"stale row {i}", f"body {i}", "document")
    store.db.commit()


@pytest.mark.asyncio
class TestRebuildPacing:
    async def test_each_row_of_an_unattended_sweep_is_paced(
        self, store, embedder, sleeps, monkeypatch
    ):
        monkeypatch.setattr(ingestion, "bulk_pace_delay", lambda elapsed: _PACE)
        _stale_items(store, 3)

        assert await rebuild_embeddings(store, embedder) == 3
        assert sleeps == [_PACE, _PACE, _PACE]

    async def test_pace_false_never_sleeps(self, store, embedder, sleeps, monkeypatch):
        monkeypatch.setattr(ingestion, "bulk_pace_delay", lambda elapsed: _PACE)
        _stale_items(store, 3)

        assert await rebuild_embeddings(store, embedder, pace=False) == 3
        assert sleeps == []

    async def test_zero_delay_does_not_call_sleep(self, store, embedder, sleeps, monkeypatch):
        """Pacing off (duty 1.0) must not add an event-loop hop per row."""
        monkeypatch.setattr(ingestion, "bulk_pace_delay", lambda elapsed: 0.0)
        _stale_items(store, 2)

        assert await rebuild_embeddings(store, embedder) == 2
        assert sleeps == []

    async def test_delay_is_derived_from_the_row_s_own_elapsed_time(
        self, store, embedder, monkeypatch
    ):
        seen: list[float] = []

        def _record(elapsed):
            seen.append(elapsed)
            return 0.0

        monkeypatch.setattr(ingestion, "bulk_pace_delay", _record)
        _stale_items(store, 1)

        await rebuild_embeddings(store, embedder)
        assert len(seen) == 1
        assert seen[0] >= 0.0

    async def test_a_failed_row_is_still_paced_by_measured_time(
        self, store, embedder, backend, sleeps, monkeypatch
    ):
        """A row that returns no vector still ran the model; pace on elapsed, not success."""
        monkeypatch.setattr(ingestion, "bulk_pace_delay", lambda elapsed: _PACE)
        backend.embed = lambda text, *, priority=PRIORITY_NORMAL: None
        _stale_items(store, 2)

        assert await rebuild_embeddings(store, embedder) == 0
        assert sleeps == [_PACE, _PACE]

    async def test_an_unattended_sweep_embeds_at_the_bulk_class(
        self, store, embedder, backend, sleeps, monkeypatch
    ):
        """PRIORITY_BULK is what earns the sweep the reduced thread pool and lets
        an interactive embed jump the shared inference queue ahead of it."""
        monkeypatch.setattr(ingestion, "bulk_pace_delay", lambda elapsed: 0.0)
        _stale_items(store, 2)

        await rebuild_embeddings(store, embedder)
        assert backend.priorities == [PRIORITY_BULK, PRIORITY_BULK]

    async def test_an_attended_sweep_keeps_the_interactive_class(
        self, store, embedder, backend, sleeps
    ):
        """``pace=False`` must switch off the thread dial too, not just the idling —
        otherwise the "full speed" path would still run on the reduced bulk pool."""
        _stale_items(store, 2)

        await rebuild_embeddings(store, embedder, pace=False)
        assert backend.priorities == [PRIORITY_NORMAL, PRIORITY_NORMAL]

    async def test_the_pause_sits_between_a_row_s_inference_and_its_write(
        self, store, embedder, monkeypatch
    ):
        """A sweep killed mid-pause leaves that row's sig stale, and the next sweep
        re-embeds it — so each pause must run BEFORE its row's write lands, and it
        must hold no DB state a concurrent writer could block on."""
        visible: list[int] = []
        real_sleep = asyncio.sleep

        async def _probe(delay, *args, **kwargs):
            if delay != _PACE:
                await real_sleep(delay, *args, **kwargs)
                return
            row = store.db.execute(
                "SELECT count(*) AS n FROM items WHERE embedding_sig IS NOT NULL"
            ).fetchone()
            visible.append(int(row["n"]))
            await real_sleep(0)

        monkeypatch.setattr(ingestion, "bulk_pace_delay", lambda elapsed: _PACE)
        monkeypatch.setattr(ingestion.asyncio, "sleep", _probe)
        _stale_items(store, 2)

        assert await rebuild_embeddings(store, embedder) == 2
        # One pause per row, each seeing only the rows already finished — never
        # its own row half-written.
        assert visible == [0, 1]


@pytest.mark.asyncio
class TestCallerWiring:
    """The two production callers stay on opposite sides of the attendance split."""

    async def test_watcher_self_heal_sweep_is_paced_on_the_bulk_class(
        self, store, embedder, backend, sleeps, monkeypatch
    ):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher

        monkeypatch.setattr(ingestion, "bulk_pace_delay", lambda elapsed: _PACE)
        _stale_items(store, 2)

        class _Pipe:
            pass

        pipe = _Pipe()
        pipe.embedder = embedder
        watcher = KnowledgeWatcher(store, pipe)

        await watcher._maybe_reembed_stale()
        assert watcher._reembed_task is not None
        await watcher._reembed_task

        assert sleeps == [_PACE, _PACE]
        assert backend.priorities == [PRIORITY_BULK, PRIORITY_BULK]

    async def test_dashboard_trigger_runs_unpaced_at_the_interactive_class(
        self, store, embedder, backend, sleeps, monkeypatch
    ):
        from kiro_crew.dashboard.handlers.knowledge import _rebuild_embeddings_job

        monkeypatch.setattr(ingestion, "bulk_pace_delay", lambda elapsed: _PACE)
        _stale_items(store, 2)
        now = datetime.now().isoformat()
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES ('dashpace0001', NULL, 'processing', ?, ?)",
            (now, now),
        )
        store.db.commit()

        await _rebuild_embeddings_job(None, store, embedder, "dashpace0001")

        assert sleeps == []
        assert backend.priorities == [PRIORITY_NORMAL, PRIORITY_NORMAL]
        row = store.db.execute(
            "SELECT status, items_processed FROM ingestion_jobs WHERE id = 'dashpace0001'"
        ).fetchone()
        assert row["status"] == "completed"
        assert row["items_processed"] == 2
