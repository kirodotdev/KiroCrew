"""Memory startup at gateway boot.

Covers ``_start_embeddings``, the background auto-migration of legacy memory with
its re-embed sweep, and the sweep probing for work before it loads the embedding
model.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _make_orchestrator,
)

from kiro_crew.slack import gateway as gw

# ═══════════════════════════════════════════════════════════════════════════
# Tests: _start_embeddings
# ═══════════════════════════════════════════════════════════════════════════


class TestStartEmbeddings:
    """In-process embedding wiring + background model download kick."""

    @pytest.mark.asyncio
    async def test_model_present_binds_embed_fn_immediately(self):
        orch = _make_orchestrator()
        orch.vector_memory = MagicMock(embed_fn=None, embed_fn_factory=None)
        fake_embed_fn = lambda text: [0.1]  # noqa: E731
        with (
            patch("kiro_crew.slack.gateway.model_file_present", return_value=True),
            patch(
                "kiro_crew.slack.gateway.make_sync_embed_fn", return_value=fake_embed_fn
            ) as mock_make,
            patch(
                "kiro_crew.slack.gateway.start_background_model_download", return_value=None
            ) as mock_start,
        ):
            await orch._start_embeddings()
        # Factory wired unconditionally (lazy rebind), fn bound immediately.
        assert orch.vector_memory.embed_fn_factory is mock_make
        assert orch.vector_memory.embed_fn is fake_embed_fn
        mock_start.assert_called_once_with()
        assert orch._model_download_task is None

    @pytest.mark.asyncio
    async def test_model_absent_defers_embed_fn_and_kicks_download(self):
        orch = _make_orchestrator()
        orch.vector_memory = MagicMock(embed_fn=None, embed_fn_factory=None)
        fake_task = MagicMock()
        with (
            patch("kiro_crew.slack.gateway.model_file_present", return_value=False),
            patch(
                "kiro_crew.slack.gateway.start_background_model_download", return_value=fake_task
            ) as mock_start,
        ):
            await orch._start_embeddings()
        # embed_fn stays unbound (lazy rebind picks it up once the model lands)
        # but the factory is wired and the background download task is stored.
        assert orch.vector_memory.embed_fn is None
        assert orch.vector_memory.embed_fn_factory is not None
        mock_start.assert_called_once_with()
        assert orch._model_download_task is fake_task


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _auto_migrate_memory (boot-time auto-migration + re-embed sweep)
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoMigrateMemory:
    """Background auto-migration of legacy markdown + re-embed sweep at boot."""

    def _orch_with_store(self, *, migrated: bool):
        orch = _make_orchestrator()
        orch._cfg.memory.migrated = migrated
        store = MagicMock()
        store.embed_fn = None
        store.migrate_from_markdown = MagicMock(
            return_value={"semantic": 3, "episodic": 5, "skipped": 1}
        )
        store.backfill_missing_embeddings = MagicMock(return_value=0)
        store._log_event = MagicMock()
        orch.vector_memory = store
        orch.consolidator = MagicMock(_migrated=False)
        return orch, store

    @staticmethod
    def _ready_embedder():
        """A shared embedder whose model is loaded (wait_ready -> True)."""
        return MagicMock(
            wait_ready=MagicMock(return_value=True), is_ready=MagicMock(return_value=True)
        )

    @pytest.mark.asyncio
    async def test_migrates_when_not_migrated_and_legacy_present(self):
        orch, store = self._orch_with_store(migrated=False)
        set_migrated = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.model_file_present", return_value=True),
            patch("kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])),
            patch(
                "kiro_crew.slack.gateway.get_shared_embedder", return_value=self._ready_embedder()
            ),
            patch("kiro_crew.memory.legacy_memory_present", return_value=True),
            patch.object(orch, "_set_memory_migrated", set_migrated),
        ):
            await orch._auto_migrate_memory()
        store.migrate_from_markdown.assert_called_once()
        set_migrated.assert_awaited_once_with(True)
        assert orch._cfg.memory.migrated is True
        assert orch.consolidator._migrated is True
        # Ack: audit event with counts summary.
        store._log_event.assert_called_once()
        assert store._log_event.call_args[0][0] == "migration"
        # Model present + loaded → re-embed sweep runs.
        store.backfill_missing_embeddings.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_migrate_when_already_migrated(self):
        orch, store = self._orch_with_store(migrated=True)
        set_migrated = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.model_file_present", return_value=True),
            patch("kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])),
            patch(
                "kiro_crew.slack.gateway.get_shared_embedder", return_value=self._ready_embedder()
            ),
            patch("kiro_crew.memory.legacy_memory_present", return_value=True),
            patch.object(orch, "_set_memory_migrated", set_migrated),
        ):
            await orch._auto_migrate_memory()
        store.migrate_from_markdown.assert_not_called()
        set_migrated.assert_not_awaited()
        # Phase 2 sweep still runs (independent of the migrated flag).
        store.backfill_missing_embeddings.assert_called_once()

    @pytest.mark.asyncio
    async def test_sweep_deferred_when_model_not_ready(self):
        # GGUF present on disk but the in-memory load hasn't finished:
        # wait_ready() -> False, so the sweep is deferred (not run with a cold
        # model that would embed zero rows).
        orch, store = self._orch_with_store(migrated=True)
        not_ready = MagicMock(
            wait_ready=MagicMock(return_value=False), is_ready=MagicMock(return_value=False)
        )
        with (
            patch("kiro_crew.slack.gateway.model_file_present", return_value=True),
            patch("kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])),
            patch("kiro_crew.slack.gateway.get_shared_embedder", return_value=not_ready),
            patch("kiro_crew.memory.legacy_memory_present", return_value=True),
            patch.object(orch, "_set_memory_migrated", AsyncMock()),
        ):
            await orch._auto_migrate_memory()
        not_ready.wait_ready.assert_called_once()
        store.backfill_missing_embeddings.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_install_no_legacy_still_flips_migrated(self):
        orch, store = self._orch_with_store(migrated=False)
        set_migrated = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.model_file_present", return_value=True),
            patch("kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])),
            patch("kiro_crew.memory.legacy_memory_present", return_value=False),
            patch.object(orch, "_set_memory_migrated", set_migrated),
        ):
            await orch._auto_migrate_memory()
        # No legacy → don't parse markdown, but still flip the flag + ack (0 counts).
        store.migrate_from_markdown.assert_not_called()
        set_migrated.assert_awaited_once_with(True)
        assert orch._cfg.memory.migrated is True
        store._log_event.assert_called_once()
        assert "semantic=0 episodic=0 skipped=0" in store._log_event.call_args[0][4]

    @pytest.mark.asyncio
    async def test_model_absent_awaits_download_then_sweeps(self):
        orch, store = self._orch_with_store(migrated=False)
        # Model absent at migrate time, present after the download task resolves.
        presence = iter([False, False, True, True])
        # flake-ok: pre-existing event-loop yield standing in for the download task, moved verbatim; tracked for a de-flake pass
        orch._model_download_task = asyncio.ensure_future(asyncio.sleep(0))
        with (
            patch(
                "kiro_crew.slack.gateway.model_file_present",
                side_effect=lambda: next(presence, True),
            ),
            patch("kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])),
            patch(
                "kiro_crew.slack.gateway.get_shared_embedder", return_value=self._ready_embedder()
            ),
            patch("kiro_crew.memory.legacy_memory_present", return_value=True),
            patch.object(orch, "_set_memory_migrated", AsyncMock()),
        ):
            await orch._auto_migrate_memory()
        # Migrated even though the model was absent; sweep ran after the wait.
        store.migrate_from_markdown.assert_called_once()
        store.backfill_missing_embeddings.assert_called_once()

    @pytest.mark.asyncio
    async def test_migrate_error_leaves_flag_false_and_survives(self):
        orch, store = self._orch_with_store(migrated=False)
        store.migrate_from_markdown.side_effect = RuntimeError("boom")
        set_migrated = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.model_file_present", return_value=True),
            patch("kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])),
            patch("kiro_crew.memory.legacy_memory_present", return_value=True),
            patch.object(orch, "_set_memory_migrated", set_migrated),
        ):
            # Must not raise — boot survives.
            await orch._auto_migrate_memory()
        set_migrated.assert_not_awaited()
        assert orch._cfg.memory.migrated is False

    @pytest.mark.asyncio
    async def test_no_vector_store_returns_without_raising(self):
        """A boot where ``_init_services`` never ran must not raise.

        The task is fire-and-forget, so an escaping AttributeError would only
        surface later as an unretrieved-task error, logged far from its cause.
        """
        orch = _make_orchestrator()
        orch._cfg.memory.migrated = False
        assert not hasattr(orch, "vector_memory")
        set_migrated = AsyncMock()
        with patch.object(orch, "_set_memory_migrated", set_migrated):
            await orch._auto_migrate_memory()
        set_migrated.assert_not_awaited()
        assert orch._cfg.memory.migrated is False


class _LoadRecordingEmbedder:
    """A backend that records whether the model load was kicked.

    Mirrors ``LlamaCppEmbedder``: ``wait_ready()`` kicks the background load (the
    ~700MB GGUF mmap plus its KV/compute buffers) before joining the loader
    thread, so a call to ``wait_ready`` IS the cost this sweep must avoid paying
    on a boot with nothing to embed. ``model_id``/``dim`` are set at construction
    and readable without a load, which is what lets the staleness probe run
    ahead of it.
    """

    def __init__(self, *, model_id: str = "qwen3-embedding:0.6b", dim: int = 1024) -> None:
        self.model_id = model_id
        self.dim = dim
        self.load_kicks = 0
        self.wait_ready_calls = 0

    def _kick_background_load(self) -> None:
        self.load_kicks += 1

    def wait_ready(self, timeout: float | None = None) -> bool:
        self.wait_ready_calls += 1
        self._kick_background_load()
        return True

    def is_ready(self) -> bool:
        return self.load_kicks > 0


class TestReembedSweepDefersTheModelLoad:
    """The sweep must probe with SQL before it loads a ~700MB embedding model.

    ``wait_ready()`` is not a free question: it kicks the GGUF load, costing
    ~1GB RSS for the process's lifetime (measured: VmRSS +1069 MiB — RssAnon
    +455 MiB private buffers, RssFile +614 MiB mmap'd weights). Steady state has
    nothing to embed, so a boot that loads the model to discover that is pure
    waste. These tests pin the load itself, not a proxy for it.
    """

    def _orch(self, *, pending: bool):
        orch = _make_orchestrator()
        orch._cfg.memory.migrated = True  # phase 1 already done
        store = MagicMock()
        store.embed_fn = None
        store.has_pending_embeddings = MagicMock(return_value=pending)
        store.backfill_missing_embeddings = MagicMock(return_value=0)
        orch.vector_memory = store
        orch.consolidator = None
        return orch, store

    @pytest.mark.asyncio
    async def test_a_boot_with_no_work_never_loads_the_model(self):
        orch, store = self._orch(pending=False)
        embedder = _LoadRecordingEmbedder()
        with (
            patch.object(gw, "get_shared_embedder", return_value=embedder),
            patch.object(gw, "model_file_present", return_value=True),
            patch.object(gw, "make_sync_embed_fn", return_value=lambda s: [0.0]),
            patch.object(gw, "store_embedding_space_is_stale", return_value=False),
            patch.object(gw, "reconcile_store_embedding_space") as reconcile,
        ):
            await orch._auto_migrate_memory()
        store.has_pending_embeddings.assert_called_once_with()
        assert embedder.load_kicks == 0, "the model must not be loaded for a no-op sweep"
        assert embedder.wait_ready_calls == 0
        store.backfill_missing_embeddings.assert_not_called()
        # No destructive reconcile either: nothing was cleared, so nothing needs
        # re-embedding, and the store's recorded space already matches.
        reconcile.assert_not_called()

    @pytest.mark.asyncio
    async def test_pending_rows_do_load_the_model_and_sweep(self):
        orch, store = self._orch(pending=True)
        embedder = _LoadRecordingEmbedder()
        stale = MagicMock(return_value=False)
        with (
            patch.object(gw, "get_shared_embedder", return_value=embedder),
            patch.object(gw, "model_file_present", return_value=True),
            patch.object(gw, "make_sync_embed_fn", return_value=lambda s: [0.0]),
            patch.object(gw, "store_embedding_space_is_stale", stale),
            patch.object(gw, "reconcile_store_embedding_space") as reconcile,
        ):
            await orch._auto_migrate_memory()
        assert embedder.wait_ready_calls == 1
        assert embedder.load_kicks == 1, "pending rows must still load the model"
        reconcile.assert_called_once_with(store)
        store.backfill_missing_embeddings.assert_called_once()
        # Short-circuit: pending work needs no staleness question.
        stale.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stale_vector_space_loads_the_model_with_no_pending_rows(self):
        """A model swap leaves every row embedded — and every vector wrong.

        ``has_pending_embeddings()`` is False here (no NULL vectors yet), so
        without the staleness arm the store would never reconcile and search
        would keep scoring old-space vectors against new-space queries.
        """
        orch, store = self._orch(pending=False)
        embedder = _LoadRecordingEmbedder()
        with (
            patch.object(gw, "get_shared_embedder", return_value=embedder),
            patch.object(gw, "model_file_present", return_value=True),
            patch.object(gw, "make_sync_embed_fn", return_value=lambda s: [0.0]),
            patch.object(gw, "store_embedding_space_is_stale", return_value=True),
            patch.object(gw, "reconcile_store_embedding_space") as reconcile,
        ):
            await orch._auto_migrate_memory()
        assert embedder.load_kicks == 1, "a stale space must still load and re-embed"
        reconcile.assert_called_once_with(store)
        store.backfill_missing_embeddings.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_store_without_the_probe_keeps_its_sweep(self):
        """A foreign/stub store must not silently lose the sweep.

        The probe is an optimisation; its absence has to fail toward doing the
        work, not toward skipping it forever.
        """
        orch = _make_orchestrator()
        orch._cfg.memory.migrated = True
        store = MagicMock(spec=["embed_fn", "backfill_missing_embeddings"])
        store.embed_fn = None
        store.backfill_missing_embeddings = MagicMock(return_value=0)
        orch.vector_memory = store
        orch.consolidator = None
        embedder = _LoadRecordingEmbedder()
        with (
            patch.object(gw, "get_shared_embedder", return_value=embedder),
            patch.object(gw, "model_file_present", return_value=True),
            patch.object(gw, "make_sync_embed_fn", return_value=lambda s: [0.0]),
            patch.object(gw, "reconcile_store_embedding_space"),
        ):
            await orch._auto_migrate_memory()
        assert not hasattr(store, "has_pending_embeddings")
        assert embedder.load_kicks == 1
        store.backfill_missing_embeddings.assert_called_once()

    def test_binding_embed_fn_does_not_load_the_model(self):
        """The lazy path this fix relies on: binding is not loading.

        ``_start_embeddings`` binds ``embed_fn``/``embed_fn_factory`` at boot. If
        that bind loaded the model, deferring the sweep's load would buy nothing.
        ``make_sync_embed_fn`` returns a closure and the load is kicked inside
        ``embed_batch`` only when it finds no resident model.
        """
        import inspect

        from kiro_crew import embeddings as emb

        src = inspect.getsource(emb.make_sync_embed_fn)
        assert "_kick_background_load" not in src
        assert "wait_ready" not in src
        # The kick lives on the embed path instead.
        assert "_kick_background_load()" in inspect.getsource(emb.LlamaCppEmbedder.embed_batch)
        # And wait_ready() is what makes the sweep's question expensive.
        assert "_kick_background_load()" in inspect.getsource(emb.LlamaCppEmbedder.wait_ready)
