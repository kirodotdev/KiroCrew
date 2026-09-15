"""Tests for Knowledge global budget and rate controls.

Covers:
- KnowledgeConfig new field defaults
- Global sweep chunk budget enforcement in watcher
- EmbedRateLimiter token bucket
- extraction_model resolution in _install_knowledge_agent
- extraction_pool_size in LLMPool
"""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KnowledgeConfig

# --- Config defaults ---


class TestKnowledgeConfigBudgetDefaults:
    def test_sweep_chunk_budget_default_500(self):
        c = KnowledgeConfig()
        assert c.sweep_chunk_budget == 500

    def test_embed_rate_limit_default_120(self):
        c = KnowledgeConfig()
        assert c.embed_rate_limit == 120

    def test_extraction_model_default_empty(self):
        c = KnowledgeConfig()
        assert c.extraction_model == ""

    def test_extraction_pool_size_default_3(self):
        c = KnowledgeConfig()
        assert c.extraction_pool_size == 3

    def test_sweep_chunk_budget_zero_is_unbounded(self):
        c = KnowledgeConfig(sweep_chunk_budget=0)
        assert c.sweep_chunk_budget == 0

    def test_embed_rate_limit_zero_is_unlimited(self):
        c = KnowledgeConfig(embed_rate_limit=0)
        assert c.embed_rate_limit == 0


# --- EmbedRateLimiter ---

class TestEmbedRateLimiter:
    def test_zero_rate_is_noop(self):
        from kiro_crew.knowledge.ingestion import EmbedRateLimiter
        limiter = EmbedRateLimiter(rate_limit=0)
        # Should not block
        asyncio.run(limiter.acquire())

    def test_high_rate_does_not_block(self):
        from kiro_crew.knowledge.ingestion import EmbedRateLimiter
        limiter = EmbedRateLimiter(rate_limit=10000)
        tokens_before = limiter._tokens
        # "Does not block" means the sleeping slow path is never reached:
        # patch asyncio.sleep and assert it was never awaited (a wall-clock
        # bound here would only measure asyncio.run() setup cost, which flakes
        # under CI load). Note: ingestion.py does a plain `import asyncio`, so
        # this rebinds asyncio.sleep process-wide for the duration of the
        # block; asyncio.run() internals never call asyncio.sleep, and the
        # patch is reverted on exit.
        fake_sleep = AsyncMock()
        with patch("kiro_crew.knowledge.ingestion.asyncio.sleep", fake_sleep):
            asyncio.run(limiter.acquire())
        fake_sleep.assert_not_awaited()
        # The fast path must still consume exactly one token, proving
        # acquire() did real work rather than returning early.
        assert limiter._tokens == tokens_before - 1.0

    def test_rate_limit_setter_resets_bucket(self):
        from kiro_crew.knowledge.ingestion import EmbedRateLimiter
        limiter = EmbedRateLimiter(rate_limit=1)
        limiter.rate_limit = 10000
        assert limiter.rate_limit == 10000

    def test_get_embed_rate_limiter_reads_config(self):
        import kiro_crew.knowledge.ingestion as ing_mod
        from kiro_crew.knowledge.ingestion import get_embed_rate_limiter

        # Reset the singleton
        ing_mod._embed_rate_limiter = None
        with patch("kiro_crew.config.loader.KiroCrewConfig.load") as mock_load:
            mock_load.return_value.knowledge.embed_rate_limit = 200
            limiter = get_embed_rate_limiter()
            assert limiter.rate_limit == 200
        ing_mod._embed_rate_limiter = None


# --- Sweep chunk budget ---

class TestSweepChunkBudget:
    def test_sweep_budget_read_from_config(self):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as mock_cfg:
            mock_cfg.load.return_value.knowledge.sweep_chunk_budget = 1000
            assert KnowledgeWatcher._sweep_chunk_budget() == 1000

    def test_sweep_budget_zero_means_unbounded(self):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as mock_cfg:
            mock_cfg.load.return_value.knowledge.sweep_chunk_budget = 0
            assert KnowledgeWatcher._sweep_chunk_budget() == 0


# --- Single-file (local_file) sweep budget ---
#
# The folder loop checks the global sweep budget before every source and charges
# scan_source's chunks_ingested back after each scan. The single-file local_file
# loop shares that counter: without the gate, a library with many changed
# local_file sources re-ingests everything in one unpaced burst — and a source
# registered with no mtime/content_hash bookkeeping reads as changed to every
# sweep, so the burst repeats until each such source is read.


def _single_file_watcher(store, budget: int, chunks_per_file: int = 1, commit: bool = True):
    """A KnowledgeWatcher whose pipeline reports ``chunks_per_file`` per ingest.

    The fake ``ingest_file`` reports extraction progress through ``on_progress``
    (the attempted count the watcher charges) and, when ``commit`` is true, the
    committed chunk ids through ``on_committed`` — the same callbacks the real
    pipeline invokes. ``commit=False`` models a post-extraction partial failure:
    the LLM calls were spent but the pipeline rolled the write back and never
    invoked ``on_committed``.
    """
    from kiro_crew.knowledge.watcher import KnowledgeWatcher

    pipeline = MagicMock()
    pipeline.embedder = None
    ingested_uris: list[str] = []

    async def fake_ingest(path, **kwargs):
        ingested_uris.append(path)
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            for i in range(chunks_per_file):
                on_progress("extracting", i + 1, chunks_per_file)
        if commit:
            on_committed = kwargs.get("on_committed")
            if on_committed is not None:
                on_committed(
                    [f"item-{len(ingested_uris)}-{i}" for i in range(chunks_per_file)])
            # The real finalize hop stamps last_synced on the source; the
            # watcher's least-recently-synced-first ordering rotates on it.
            sid = kwargs.get("source_id")
            if sid:
                store.update_source(
                    sid, last_synced=f"2026-01-01T00:00:{len(ingested_uris):02d}")
        return "job-id"

    pipeline.ingest_file = AsyncMock(side_effect=fake_ingest)
    watcher = KnowledgeWatcher(store=store, pipeline=pipeline)
    watcher._maybe_reembed_stale = AsyncMock()  # type: ignore[method-assign]
    watcher._maybe_dedup_sweep = AsyncMock()  # type: ignore[method-assign]
    watcher._sweep_chunk_budget = lambda: budget  # type: ignore[method-assign]
    return watcher, ingested_uris


def _add_local_files(store, tmp_path, count: int) -> list[str]:
    """Register ``count`` changed local_file sources (no mtime/hash recorded).

    No stored mtime/content_hash is exactly the state a deferred explicit
    import leaves behind, so every one of these reads as changed to the sweep.
    """
    sids = []
    for i in range(count):
        f = tmp_path / f"doc{i}.md"
        f.write_text(f"# doc {i}")
        sids.append(store.add_source(f"doc{i}.md", "local_file", str(f)))
    return sids


def _props_of(store, sid: str) -> dict:
    raw = store.db.execute(
        "SELECT properties FROM sources WHERE id = ?", (sid,)).fetchone()["properties"]
    return json.loads(raw or "{}")


class TestSingleFileSweepBudget:
    @pytest.fixture()
    def store(self, tmp_path):
        from kiro_crew.knowledge.store import KnowledgeStore

        s = KnowledgeStore(str(tmp_path / "knowledge.db"))
        yield s
        s.close()

    @pytest.mark.asyncio
    async def test_sweep_stops_at_budget(self, store, tmp_path):
        """A sweep over many changed local_file sources stops at the budget."""
        _add_local_files(store, tmp_path, 5)
        watcher, ingested = _single_file_watcher(store, budget=2, chunks_per_file=1)

        await watcher._scan()

        assert len(ingested) == 2

    @pytest.mark.asyncio
    async def test_attempted_chunk_count_is_charged(self, store, tmp_path):
        """The charge is the per-file attempted chunk count, not one per file.

        Budget 10 with 6 chunks per file: after the first file 6 < 10 so the
        second still runs; after the second 12 >= 10 so the third is deferred.
        (On the commit path attempted == committed, so this also pins the
        committed count.)
        """
        _add_local_files(store, tmp_path, 3)
        watcher, ingested = _single_file_watcher(store, budget=10, chunks_per_file=6)

        await watcher._scan()

        assert len(ingested) == 2

    @pytest.mark.asyncio
    async def test_deferred_sources_keep_no_bookkeeping_and_resume(self, store, tmp_path):
        """Deferral records no mtime/content_hash, so the next sweep resumes.

        Asserted as a partition (exactly two ingested, the rest untouched)
        rather than by identity: the driving query has no ORDER BY, so which
        two rows a sweep reaches first is a query-plan detail, not a contract.
        """
        sids = _add_local_files(store, tmp_path, 4)
        watcher, ingested = _single_file_watcher(store, budget=2, chunks_per_file=1)

        await watcher._scan()

        assert len(ingested) == 2
        with_bookkeeping = [sid for sid in sids if "mtime" in _props_of(store, sid)]
        assert len(with_bookkeeping) == 2
        for sid in sids:
            props = _props_of(store, sid)
            if sid in with_bookkeeping:
                assert "content_hash" in props
            else:
                assert "mtime" not in props, "a deferred source must stay resumable"
                assert "content_hash" not in props

        # The next sweep (fresh budget) picks up where this one stopped.
        await watcher._scan()
        assert len(ingested) == 4

    @pytest.mark.asyncio
    async def test_folder_loop_consumption_defers_single_files(self, store, tmp_path):
        """Both loops share one counter: a folder that spends the whole budget
        leaves nothing for the single-file loop's ingests that sweep."""
        folder = tmp_path / "vault"
        folder.mkdir()
        store.add_source("vault", "local_folder", str(folder))
        _add_local_files(store, tmp_path, 2)

        watcher, ingested = _single_file_watcher(store, budget=5, chunks_per_file=1)
        watcher._folder_watcher.scan_source = AsyncMock(  # type: ignore[method-assign]
            return_value={"chunks_ingested": 5})

        await watcher._scan()

        assert ingested == []

    @pytest.mark.asyncio
    async def test_zero_budget_leaves_the_sweep_unbounded(self, store, tmp_path):
        """budget=0 disables the bound, matching the folder loop's contract."""
        _add_local_files(store, tmp_path, 4)
        watcher, ingested = _single_file_watcher(store, budget=0, chunks_per_file=100)

        await watcher._scan()

        assert len(ingested) == 4

    @pytest.mark.asyncio
    async def test_exhausted_budget_still_marks_vanished_files_missing(self, store, tmp_path):
        """Zero-cost status upkeep survives budget exhaustion.

        The gate defers reads and ingests with a per-row ``continue`` below the
        existence check, never a loop-level ``break``, so a vanished file's
        'missing' marker still lands on a sweep whose folder sources spent the
        whole budget.
        """

        folder = tmp_path / "vault"
        folder.mkdir()
        store.add_source("vault", "local_folder", str(folder))
        gone = tmp_path / "gone.md"
        gone.write_text("# gone")
        sid = store.add_source("gone.md", "local_file", str(gone))
        store.db.execute("UPDATE sources SET sync_status = 'synced' WHERE id = ?", (sid,))
        store.db.commit()
        gone.unlink()

        watcher, ingested = _single_file_watcher(store, budget=5, chunks_per_file=1)
        watcher._folder_watcher.scan_source = AsyncMock(  # type: ignore[method-assign]
            return_value={"chunks_ingested": 5})

        await watcher._scan()

        assert ingested == []
        status = store.db.execute(
            "SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()["sync_status"]
        assert status == "missing"

    @pytest.mark.asyncio
    async def test_contended_sweeps_rotate_across_sources(self, store, tmp_path):
        """Under sustained contention every source makes progress.

        The sweep orders local_file rows least-recently-synced first, and a
        served source's fresh ``last_synced`` moves it behind rows still
        waiting — so even when every source changes every sweep and the budget
        admits one file per sweep, three sweeps serve three DIFFERENT sources
        rather than re-serving whichever row a stable query order puts first.
        """
        paths = [tmp_path / f"doc{i}.md" for i in range(3)]
        _add_local_files(store, tmp_path, 3)
        watcher, ingested = _single_file_watcher(store, budget=1, chunks_per_file=1)

        for sweep in range(3):
            # Every file changes before every sweep: new content, newer mtime.
            for p in paths:
                p.write_text(f"# rev {sweep} of {p.name}")
                os.utime(p, (2_000_000_000 + sweep, 2_000_000_000 + sweep))
            await watcher._scan()

        assert len(ingested) == 3
        assert len(set(ingested)) == 3, (
            "a contended sweep must rotate, not re-serve the same source")

    @pytest.mark.asyncio
    async def test_folder_churn_cannot_permanently_starve_single_files(self, store, tmp_path):
        """Sustained folder churn delays a changed local_file by at most one sweep.

        The two populations alternate which spends the shared budget first, so
        a folder that consumes the entire budget on every sweep still leaves
        the next sweep's first claim to the single-file rows — deferral is
        recoverable without waiting for the folder churn to subside.
        """
        folder = tmp_path / "vault"
        folder.mkdir()
        store.add_source("vault", "local_folder", str(folder))
        _add_local_files(store, tmp_path, 1)

        watcher, ingested = _single_file_watcher(store, budget=5, chunks_per_file=1)
        # The folder consumes the WHOLE budget on every single sweep.
        watcher._folder_watcher.scan_source = AsyncMock(  # type: ignore[method-assign]
            return_value={"chunks_ingested": 5})

        await watcher._scan()  # folders first: budget exhausted, file deferred
        assert ingested == []
        await watcher._scan()  # single files first: the deferred file ingests

        assert len(ingested) == 1

    @pytest.mark.asyncio
    async def test_bookkeeping_merges_against_concurrent_writes(self, store, tmp_path):
        """The persist is a read-merge-write of the CURRENT row, not a snapshot.

        The watcher reads a source's properties at the top of the sweep, and a
        full ingest can run between that read and the bookkeeping write. A key
        a concurrent writer (a manual sync) commits in that window must
        survive the watcher's write — a whole-blob write of the sweep's
        snapshot would silently clobber it.
        """
        sids = _add_local_files(store, tmp_path, 1)
        watcher, ingested = _single_file_watcher(store, budget=0, chunks_per_file=1)

        orig_ingest = watcher.pipeline.ingest_file.side_effect

        async def ingest_with_concurrent_write(path, **kwargs):
            # A concurrent manual sync lands mid-ingest.
            row = store.db.execute(
                "SELECT properties FROM sources WHERE id = ?", (sids[0],)).fetchone()
            props = json.loads(row["properties"] or "{}")
            props["concurrent_key"] = "must-survive"
            store.update_source(sids[0], properties=json.dumps(props))
            return await orig_ingest(path, **kwargs)

        watcher.pipeline.ingest_file = AsyncMock(side_effect=ingest_with_concurrent_write)

        await watcher._scan()

        props = _props_of(store, sids[0])
        assert props.get("concurrent_key") == "must-survive"
        assert "mtime" in props and "content_hash" in props
        assert "sweep_attempted_at" in props

    @pytest.mark.asyncio
    async def test_non_dict_properties_do_not_crash_the_sweep(self, store, tmp_path):
        """A legacy row whose properties JSON parses to a non-dict reads as {}.

        ``_parse_props`` feeds the sort key, which chains ``.get`` onto the
        result — a stored ``"[]"`` would otherwise raise ``AttributeError``
        inside ``sorted()`` and abort the whole sweep every interval with no
        self-correction. The poison row participates as if it had no
        properties, and every other source still ingests.
        """
        legacy = tmp_path / "legacy.md"
        legacy.write_text("# legacy")
        sid = store.add_source("legacy.md", "local_file", str(legacy))
        store.db.execute("UPDATE sources SET properties = '[]' WHERE id = ?", (sid,))
        store.db.commit()
        _add_local_files(store, tmp_path, 1)
        watcher, ingested = _single_file_watcher(store, budget=0, chunks_per_file=1)

        await watcher._scan()

        assert len(ingested) == 2, "both sources ingest; the sweep must not crash"

    @pytest.mark.asyncio
    async def test_corrupt_attempt_stamp_does_not_crash_the_sweep(self, store, tmp_path):
        """A non-string ``sweep_attempted_at`` degrades to the fallback key.

        ``store.import_bundle`` accepts a source's properties JSON verbatim
        from external content, so the stamp's type is untrusted: a numeric
        value must not poison the mixed-type sort and abort the sweep before
        any single-file reconciliation runs — it reads as never-attempted and
        the row sorts by ``last_synced`` instead.
        """
        poisoned = tmp_path / "poisoned.md"
        poisoned.write_text("# poisoned")
        store.add_source(
            "poisoned.md", "local_file", str(poisoned),
            properties={"sweep_attempted_at": 123})
        _add_local_files(store, tmp_path, 1)
        watcher, ingested = _single_file_watcher(store, budget=0, chunks_per_file=1)

        await watcher._scan()

        assert len(ingested) == 2, "both sources ingest; the sweep must not crash"

    @pytest.mark.asyncio
    async def test_failing_source_does_not_starve_later_sources(self, store, tmp_path):
        """A persistently failing row rotates like any served row.

        A partial-ingest failure charges its spent extraction calls but never
        advances ``last_synced`` (only the committed branch writes it), so an
        ordering keyed on last_synced alone re-serves the failing row first
        every sweep while its charge exhausts the budget — later changed
        sources never run. The attempt stamp is what breaks that: the failed
        row's retry waits its turn behind the rows still queued.
        """
        _add_local_files(store, tmp_path, 3)
        watcher, ingested = _single_file_watcher(
            store, budget=1, chunks_per_file=1, commit=False)

        for _ in range(3):
            await watcher._scan()

        assert len(ingested) == 3
        assert len(set(ingested)) == 3, (
            "each contended sweep must serve a source the failing row was not")

    @pytest.mark.asyncio
    async def test_partial_failure_charges_and_stays_resumable(self, store, tmp_path):
        """A rolled-back ingest still charges its spent extraction calls, and
        keeps no bookkeeping so the next sweep can retry it.

        The pipeline invokes ``on_committed`` only on the fully-committed
        branch; extraction already spent one LLM call per chunk by then. If the
        watcher charged only committed chunks, repeated post-extraction
        failures would spend without bound; if it persisted mtime/content_hash
        anyway, the changed file would never be re-read.
        """
        sids = _add_local_files(store, tmp_path, 3)
        watcher, ingested = _single_file_watcher(
            store, budget=4, chunks_per_file=2, commit=False)

        await watcher._scan()

        # Charged 2 attempted chunks per failed ingest: 2, then 4 >= 4 — the
        # third source is deferred, so the failure loop is budget-bounded.
        assert len(ingested) == 2
        # And nothing was recorded, so every source stays retryable.
        for sid in sids:
            assert "mtime" not in _props_of(store, sid)
            assert "content_hash" not in _props_of(store, sid)


# --- Pool size from config ---

class TestPoolSizeConfig:
    def test_default_pool_size(self):
        from kiro_crew.knowledge.llm_pool import DEFAULT_POOL_SIZE, _get_pool_size
        assert _get_pool_size({}) == DEFAULT_POOL_SIZE

    def test_configured_pool_size(self):
        from kiro_crew.knowledge.llm_pool import _get_pool_size
        config = {"knowledge": {"extraction_pool_size": 5}}
        assert _get_pool_size(config) == 5

    def test_pool_size_clamped_to_max_10(self):
        from kiro_crew.knowledge.llm_pool import DEFAULT_POOL_SIZE, _get_pool_size
        config = {"knowledge": {"extraction_pool_size": 99}}
        assert _get_pool_size(config) == DEFAULT_POOL_SIZE

    def test_pool_size_clamped_to_min_1(self):
        from kiro_crew.knowledge.llm_pool import DEFAULT_POOL_SIZE, _get_pool_size
        config = {"knowledge": {"extraction_pool_size": 0}}
        assert _get_pool_size(config) == DEFAULT_POOL_SIZE


# --- Extraction model resolution ---

class TestExtractionModelResolution:
    def test_empty_extraction_model_uses_agent_model(self):
        """When extraction_model is empty, _install_knowledge_agent uses agent.model."""
        with patch("kiro_crew.config.loader.KiroCrewConfig.load") as mock_load:
            mock_load.return_value.knowledge.extraction_model = ""
            mock_load.return_value.agent.model = "claude-sonnet-4.5"
            with patch("kiro_crew.agent._atomic_json_write") as mock_write:
                with patch("kiro_crew.agent.kiro_agents_dir_path") as mock_path:
                    mock_path.return_value = Path("/tmp/agents")
                    from kiro_crew.agent import _install_knowledge_agent
                    _install_knowledge_agent()
                    written = mock_write.call_args[0][1]
                    assert written["model"] == "claude-sonnet-4.5"

    def test_explicit_extraction_model_overrides(self):
        """When extraction_model is set, it overrides agent.model."""
        with patch("kiro_crew.config.loader.KiroCrewConfig.load") as mock_load:
            mock_load.return_value.knowledge.extraction_model = "claude-haiku-4.5"
            mock_load.return_value.agent.model = "claude-sonnet-4.5"
            with patch("kiro_crew.agent._atomic_json_write") as mock_write:
                with patch("kiro_crew.agent.kiro_agents_dir_path") as mock_path:
                    mock_path.return_value = Path("/tmp/agents")
                    from kiro_crew.agent import _install_knowledge_agent
                    _install_knowledge_agent()
                    written = mock_write.call_args[0][1]
                    assert written["model"] == "claude-haiku-4.5"


# --- Explicit-import cross-file chunk budget ---
#
# The watcher path has per-sweep and global chunk budgets; the explicit import
# routes (single-file add, agent add, direct text ingest, remote sync) are bounded
# by a cross-file ceiling instead. These cover the ImportChunkBudget limiter, its
# config key, and its enforcement at the pipeline entry.


class TestImportChunkBudgetConfig:
    def test_import_chunk_budget_default_off_opt_in(self):
        # Default MUST be 0 (opt-in): a policy control that ships on would throttle
        # every existing user's imports without their choosing it. Opt in first,
        # default-on later with data. If this reddens to 500, the switch flipped.
        c = KnowledgeConfig()
        assert c.import_chunk_budget == 0

    def test_import_chunk_budget_stored_verbatim(self):
        c = KnowledgeConfig(import_chunk_budget=200)
        assert c.import_chunk_budget == 200

    def test_import_chunk_budget_zero_is_unbounded(self):
        c = KnowledgeConfig(import_chunk_budget=0)
        assert c.import_chunk_budget == 0

    def test_loader_clamps_absurd_value_to_ceiling(self, tmp_path):
        import json
        import unittest.mock

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.sections import IMPORT_CHUNK_BUDGET_MAX

        (tmp_path / "config.json").write_text(
            json.dumps({"knowledge": {"import_chunk_budget": IMPORT_CHUNK_BUDGET_MAX * 100}}),
            encoding="utf-8",
        )
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_dir", return_value=tmp_path
        ):
            cfg = KiroCrewConfig.load()
        assert cfg.knowledge.import_chunk_budget == IMPORT_CHUNK_BUDGET_MAX


class TestImportChunkBudgetLimiter:
    def test_zero_budget_never_refuses(self):
        from kiro_crew.knowledge.ingestion import ImportChunkBudget

        b = ImportChunkBudget(budget=0)
        # Disabled: reserve returns None (no token) and never raises.
        assert b.reserve() is None
        b.settle(None, 10_000)  # no-op
        b.release(None)  # no-op

    def test_reserve_refuses_once_window_reaches_budget(self):
        import kiro_crew.knowledge.ingestion as ing
        from kiro_crew.knowledge.ingestion import ImportChunkBudget, ImportChunkBudgetError

        now = [1000.0]
        with patch.object(ing._time, "monotonic", lambda: now[0]):
            b = ImportChunkBudget(budget=50)
            t1 = b.reserve()          # empty window: ok, books 50 (per-file max)
            b.settle(t1, 30)          # reconcile down to 30
            t2 = b.reserve()          # 30 < 50: ok, books 50 -> spent 80
            b.settle(t2, 30)          # reconcile to 30 -> spent 60 >= 50
            # The accepted files completed; the NEXT reserve trips.
            try:
                b.reserve()
                raised = False
            except ImportChunkBudgetError as e:
                raised = True
                assert e.budget == 50
                assert e.spent >= 50
            assert raised, "reserve() must refuse once the window is at/over budget"

    def test_concurrent_reservations_do_not_overrun(self):
        # The TOCTOU fix: a reservation is booked INTO the window at reserve()
        # time, before settle(), so simultaneous imports see each other. With a
        # budget of 50 and the per-file placeholder of 50, the first reserve books
        # 50 and the second is refused -- N concurrent imports cannot each pass.
        import kiro_crew.knowledge.ingestion as ing
        from kiro_crew.knowledge.ingestion import ImportChunkBudget, ImportChunkBudgetError

        now = [1000.0]
        with patch.object(ing._time, "monotonic", lambda: now[0]):
            b = ImportChunkBudget(budget=50)
            t1 = b.reserve()  # books 50 immediately, BEFORE any settle
            assert t1 is not None
            # Second concurrent import (its settle has not run yet) is refused.
            try:
                b.reserve()
                overran = True
            except ImportChunkBudgetError:
                overran = False
            assert not overran, "a second concurrent reserve must not pass before the first settles"

    def test_release_frees_a_failed_reservation(self):
        import kiro_crew.knowledge.ingestion as ing
        from kiro_crew.knowledge.ingestion import ImportChunkBudget

        now = [1000.0]
        with patch.object(ing._time, "monotonic", lambda: now[0]):
            b = ImportChunkBudget(budget=50)
            t1 = b.reserve()   # books 50 -> at budget
            b.release(t1)      # failed import frees it
            # Window is empty again: a fresh reserve succeeds.
            assert b.reserve() is not None

    def test_release_reclaims_a_noop_reservation_no_leak(self):
        # The dedup / content-unchanged success paths return before any chunk
        # work, so they never settle. A finally-release must reclaim the
        # placeholder or ten no-op re-ingests would strand 10x50=500 chunks and
        # falsely refuse genuine imports at the default budget.
        import kiro_crew.knowledge.ingestion as ing
        from kiro_crew.knowledge.ingestion import ImportChunkBudget

        now = [1000.0]
        with patch.object(ing._time, "monotonic", lambda: now[0]):
            b = ImportChunkBudget(budget=500)
            for _ in range(20):          # 20 > 500/50, would exhaust if leaked
                t = b.reserve()          # books 50
                b.release(t)             # no-op path: reclaim it
            # Nothing accumulated: a genuine import still reserves fine.
            assert b.reserve() is not None

    def test_release_is_noop_after_settle_no_double_count(self):
        # settle() consumes the token; a following release() (from the wrapper's
        # finally) must NOT drop the settled real count.
        import kiro_crew.knowledge.ingestion as ing
        from kiro_crew.knowledge.ingestion import ImportChunkBudget, ImportChunkBudgetError

        now = [1000.0]
        with patch.object(ing._time, "monotonic", lambda: now[0]):
            b = ImportChunkBudget(budget=50)
            t = b.reserve()
            b.settle(t, 50)      # real cost 50 -> at budget
            b.release(t)         # finally-release: must be a no-op here
            # The settled 50 still stands, so the next reserve is refused.
            try:
                b.reserve()
                refused = False
            except ImportChunkBudgetError:
                refused = True
            assert refused, "release after settle must not drop the settled count"

    def test_window_rolls_over_and_reopens(self):
        import kiro_crew.knowledge.ingestion as ing
        from kiro_crew.knowledge.ingestion import ImportChunkBudget

        now = [1000.0]
        with patch.object(ing._time, "monotonic", lambda: now[0]):
            b = ImportChunkBudget(budget=50)
            t1 = b.reserve()
            b.settle(t1, 60)   # 60 >= 50 within window
            try:
                b.reserve()
                refused = True
            except ing.ImportChunkBudgetError:
                refused = True
            else:
                refused = False
            # (either the reserve above raised, or we set refused False)
            assert refused
            now[0] += 61.0     # advance past the 60s window
            assert b.reserve() is not None  # pruned, reopened

    def test_an_open_reservation_outlives_the_window(self):
        # A file slower than the window keeps its slot until it settles. If
        # pruning expired a live reservation, a concurrent import would be
        # admitted past the very concurrency ceiling the placeholder enforces.
        import kiro_crew.knowledge.ingestion as ing
        from kiro_crew.knowledge.ingestion import ImportChunkBudget, ImportChunkBudgetError

        now = [1000.0]
        with patch.object(ing._time, "monotonic", lambda: now[0]):
            b = ImportChunkBudget(budget=50)
            t = b.reserve()                  # books 50 -> at budget
            assert t is not None
            now[0] += ing._IMPORT_CHUNK_BUDGET_WINDOW_SECS + 1.0   # still ingesting
            try:
                b.reserve()
                refused = False
            except ImportChunkBudgetError:
                refused = True
            assert refused, "an in-flight reservation must still occupy the ceiling"
            # Settling closes it, so its entry expires by age like any record.
            b.settle(t, 1)
            assert b.reserve() is not None

    def test_error_message_is_ascii_and_actionable(self):
        from kiro_crew.knowledge.ingestion import ImportChunkBudgetError

        msg = str(ImportChunkBudgetError(budget=50, window_secs=60.0, spent=60))
        assert msg.isascii()
        assert "import_chunk_budget" in msg


class TestImportBudgetPipelineGate:
    """The gate lives at the pipeline entry; watcher + artifact-sync paths are exempt."""

    def _pipeline(self):
        from unittest.mock import MagicMock

        from kiro_crew.knowledge.ingestion import IngestionPipeline

        return IngestionPipeline(
            store=MagicMock(), extractor=MagicMock(), chunker=MagicMock(),
            reader=MagicMock(),
        )

    def test_enter_budget_refuses_explicit_call_when_exhausted(self):
        import asyncio

        from kiro_crew.knowledge.ingestion import ImportChunkBudgetError

        p = self._pipeline()
        with patch("kiro_crew.knowledge.ingestion._import_chunk_budget", return_value=50):
            p._import_budget.set_budget(50)
            t = p._import_budget.reserve()   # book 50 -> at budget
            assert t is not None
            try:
                asyncio.run(p._enter_import_budget(count_toward_import_budget=True))
                raised = False
            except ImportChunkBudgetError:
                raised = True
        assert raised, "an explicit call must be refused once the window is exhausted"

    def test_watcher_path_is_exempt_even_when_exhausted(self):
        import asyncio

        p = self._pipeline()
        with patch("kiro_crew.knowledge.ingestion._import_chunk_budget", return_value=50):
            p._import_budget.set_budget(50)
            p._import_budget.reserve()  # exhaust
            # count_toward_import_budget=False (watcher/artifact-sync) must not gate.
            token = asyncio.run(p._enter_import_budget(count_toward_import_budget=False))
        assert token is None

    def test_enter_budget_returns_token_when_enabled(self):
        import asyncio

        p = self._pipeline()
        with patch("kiro_crew.knowledge.ingestion._import_chunk_budget", return_value=500):
            token = asyncio.run(p._enter_import_budget(count_toward_import_budget=True))
        assert token is not None  # a reservation to settle/release later

    def test_enter_budget_returns_none_when_config_zero(self):
        import asyncio

        p = self._pipeline()
        with patch("kiro_crew.knowledge.ingestion._import_chunk_budget", return_value=0):
            token = asyncio.run(p._enter_import_budget(count_toward_import_budget=True))
        assert token is None  # disabled -> nothing to settle


class TestRemoteSyncSurfacesDeferral:
    """Remote sync surfaces a budget deferral without counting it as a failure."""

    def test_sync_source_surfaces_deferred_and_records_no_failure(self, tmp_path):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.knowledge.ingestion import ImportChunkBudgetError
        from kiro_crew.knowledge.store import KnowledgeStore
        from kiro_crew.knowledge.sync import SyncScheduler

        store = KnowledgeStore(str(tmp_path / "sync.db"))
        try:
            sid = store.add_source(name="remote", source_type="webhook", uri="x://remote")
            connector = MagicMock()
            connector.detect_changes = AsyncMock(return_value=True)
            connector.fetch = AsyncMock(return_value=("body text", {}))
            pipeline = MagicMock()
            pipeline.ingest_text = AsyncMock(
                side_effect=ImportChunkBudgetError(budget=500, window_secs=60.0, spent=500))

            sched = SyncScheduler(store, pipeline, {"webhook": connector})
            res = asyncio.run(sched.sync_source(sid))

            assert "deferred" in res and "import_chunk_budget" in res["deferred"]
            assert res["synced"] is False
            # A deferral is not a failure: consecutive_failures must stay 0.
            row = store.db.execute(
                "SELECT properties FROM sources WHERE id = ?", (sid,)).fetchone()
            import json as _json
            props = _json.loads(row["properties"] or "{}")
            assert props.get("consecutive_failures", 0) == 0
        finally:
            store.close()


class TestBackgroundDeferralStaysRetryable:
    """A budget deferral on a background path must not land in 'error'.

    ``SyncScheduler.sync_all`` skips a source whose ``sync_status`` is 'error', so
    writing that state over a window that clears in a minute would quiesce the
    source for good. Both background writers whose content is still on disk mark
    'pending' instead, which the sweep still visits.
    """

    @staticmethod
    def _recording_store():
        class _Cursor:
            # The sync claim decides whether it won the row from rowcount, so the
            # fake has to answer it. 1 = this call took the claim, which is what
            # puts the task on the path these tests are about.
            rowcount = 1

        class _DB:
            def __init__(self):
                self.statements: list[tuple[str, tuple]] = []

            def execute(self, sql, params=()):
                self.statements.append((sql, tuple(params)))
                return _Cursor()

            def commit(self):
                pass

        class _Store:
            def __init__(self):
                self.db = _DB()

        return _Store()

    _TERMINAL_STATES = ("pending", "error", "synced")

    @classmethod
    def _states(cls, store):
        """The terminal states written, as VALUES rather than as SQL text.

        Status writes go through one parameterized helper, so the state is in the
        parameters. The 'syncing' claim is not a terminal state and carries its
        value as a literal, so it does not appear here.
        """
        return [
            value
            for sql, params in store.db.statements
            if "sync_status" in sql
            for value in params
            if value in cls._TERMINAL_STATES
        ]

    def test_local_file_ingest_defers_to_pending(self, tmp_path):
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers import knowledge as kh
        from kiro_crew.knowledge.ingestion import ImportChunkBudgetError

        doc = tmp_path / "doc.md"
        doc.write_text("body", encoding="utf-8")
        store = self._recording_store()
        pipeline = SimpleNamespace(ingest_file=AsyncMock(
            side_effect=ImportChunkBudgetError(budget=50, window_secs=60.0, spent=60)))

        asyncio.run(kh._ingest_local_file_task(pipeline, store, str(doc), "src-1"))

        assert self._states(store) == ["pending"], store.db.statements

    def test_local_file_ingest_still_errors_on_a_real_failure(self, tmp_path):
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers import knowledge as kh

        doc = tmp_path / "doc.md"
        doc.write_text("body", encoding="utf-8")
        store = self._recording_store()
        pipeline = SimpleNamespace(ingest_file=AsyncMock(side_effect=RuntimeError("disk gone")))

        asyncio.run(kh._ingest_local_file_task(pipeline, store, str(doc), "src-1"))

        assert self._states(store) == ["error"], store.db.statements


class TestUploadRefusesBeforeAccepting:
    """An exhausted window must refuse the upload, never accept then discard it.

    The multipart route answers 'processing' and ingests in the background, and
    the staged temp file is the only server-side copy -- its ``finally`` unlinks
    it. So admission is reserved before the response and the token handed to
    ``ingest_file``: a refusal becomes a 429 the client can act on, and a token
    that never reaches ``ingest_file`` is reclaimed instead of stranding a
    placeholder in the window.
    """

    @staticmethod
    def _pipeline():
        from unittest.mock import MagicMock

        from kiro_crew.knowledge.ingestion import IngestionPipeline

        return IngestionPipeline(
            store=MagicMock(), extractor=MagicMock(), chunker=MagicMock(),
            reader=MagicMock(),
        )

    def test_reserve_import_budget_raises_when_the_window_is_exhausted(self):
        from kiro_crew.knowledge.ingestion import ImportChunkBudgetError

        p = self._pipeline()
        with patch("kiro_crew.knowledge.ingestion._import_chunk_budget", return_value=50):
            p._import_budget.set_budget(50)
            p._import_budget.reserve()          # exhaust the window
            try:
                asyncio.run(p.reserve_import_budget())
                raised = False
            except ImportChunkBudgetError:
                raised = True
        assert raised, "an exhausted window must refuse admission before acceptance"

    def test_reserve_import_budget_returns_none_when_disabled(self):
        p = self._pipeline()
        with patch("kiro_crew.knowledge.ingestion._import_chunk_budget", return_value=0):
            assert asyncio.run(p.reserve_import_budget()) is None

    def test_a_handed_over_token_settles_without_booking_a_second_entry(self):
        """`ingest_file` consumes the caller's token rather than booking another."""
        from kiro_crew.knowledge.ingestion import ImportChunkBudget

        b = ImportChunkBudget(budget=50)
        token = b.reserve()                      # the route's own admission
        assert token is not None
        before = len(b._events)
        b.settle(token, 1)
        assert len(b._events) == before, "settling must reconcile, not add an entry"

    def test_release_import_budget_reclaims_an_unused_token(self):
        p = self._pipeline()
        with patch("kiro_crew.knowledge.ingestion._import_chunk_budget", return_value=50):
            p._import_budget.set_budget(50)
            token = asyncio.run(p.reserve_import_budget())
            assert token is not None
            p.release_import_budget(token)       # handler failed before handover
            # Reclaimed, so a genuine import still passes.
            assert asyncio.run(p.reserve_import_budget()) is not None

    def test_ingest_file_honours_an_admitted_caller_in_every_combination(self):
        """Whether `ingest_file` enters the budget again, across all four inputs.

        A DISABLED budget admits with a token of ``None``, so "no token" cannot
        mean "not admitted": reserving on that would enter the budget a second
        time, and a budget enabled between the two config reads would refuse an
        upload already accepted. The flag is what says admission is settled, and a
        caller holding a token is honoured either way rather than stranded.
        """
        from unittest.mock import AsyncMock

        from kiro_crew.knowledge.ingestion import ImportChunkBudgetError

        def run(flag, token):
            p = self._pipeline()
            with patch("kiro_crew.knowledge.ingestion._import_chunk_budget", return_value=50):
                p._import_budget.set_budget(50)
                p._import_budget.reserve()          # exhaust the window
                impl = AsyncMock(return_value="job")
                with patch.object(p, "_ingest_file_impl", impl):
                    try:
                        asyncio.run(p.ingest_file(
                            "/tmp/x.md", count_toward_import_budget=flag,
                            import_budget_token=token))
                    except ImportChunkBudgetError:
                        return "refused"
                    return impl.await_args.kwargs["budget_token"]

        # Ordinary explicit path: no admission yet, so an exhausted window refuses.
        assert run(True, None) == "refused"
        # Admitted with a token (budget enabled): used, never re-reserved.
        assert run(False, 7) == 7
        # Admitted with the budget disabled: None is the admission, not its absence.
        assert run(False, None) is None
        # A caller holding a token is honoured rather than having it stranded.
        assert run(True, 7) == 7

    def test_release_import_budget_tolerates_none(self):
        p = self._pipeline()
        p.release_import_budget(None)            # nothing reserved; must not raise
