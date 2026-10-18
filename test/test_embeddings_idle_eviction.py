"""The embedding runtime's idle eviction, and a model that loads on demand.

An idle model is unloaded and the next interactive embed loads it again, without
freeing a context a live worker still uses; a model that can load on demand reads
as available without loading, and a missing or failed one does not.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import embeddings_helpers
import pytest
from embeddings_helpers import (
    _live_embed_threads,
    _make_failing_llama_class,
    _make_fake_llama_class,
    _model_bytes,
    _write_model_file,
)

import kiro_crew.embeddings as embeddings_mod
from kiro_crew.embeddings import LlamaCppEmbedder
from kiro_crew.testing.wait import wait_until

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_reset_embedding_singletons = embeddings_helpers._reset_embedding_singletons


# ═══════════════════════════════════════════════════════════════════════════
# Idle eviction and the cold-load wait
# ═══════════════════════════════════════════════════════════════════════════


class TestIdleEviction:
    """An idle model is unloaded; the next interactive embed loads it again."""

    def _loaded(self, tmp_path: Path, monkeypatch):
        fake_cls = _make_fake_llama_class()
        closed: list[object] = []
        fake_cls.close = lambda self: closed.append(self)
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
        assert emb.embed("warm") is not None
        # The embed returns as soon as the loader PUBLISHES the model, but the
        # loader thread still runs its epilogue (_start_evictor) after that.
        # evict_if_idle refuses while a loader thread is alive, and the evictor
        # does not exist until the epilogue ran, so join the loader before any
        # test reads either.
        assert emb.wait_ready(timeout=5)
        assert emb._load_thread is not None and not emb._load_thread.is_alive()
        return emb, fake_cls, closed

    def test_eviction_refuses_while_the_loader_is_still_finishing(
        self, tmp_path: Path, monkeypatch
    ):
        """A first embed can return before its loader thread has exited.

        The loader publishes the model, then starts the evictor. An embed that
        waited for the cold load returns on the publish, so a caller can reach
        evict_if_idle while the loader is still alive, and is refused. The
        epilogue is held here so the order is fixed, not left to the scheduler.
        """
        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
        epilogue = threading.Event()
        real_start_evictor = emb._start_evictor

        def _held_start_evictor() -> None:
            assert epilogue.wait(timeout=30), "the test never released the loader"
            real_start_evictor()

        monkeypatch.setattr(emb, "_start_evictor", _held_start_evictor)
        try:
            assert emb.embed("warm") is not None
            assert emb._load_thread is not None and emb._load_thread.is_alive()
            emb._last_used -= 100
            assert emb.evict_if_idle(50) is False  # the loader is still alive
            assert emb.is_ready()
            epilogue.set()
            assert emb.wait_ready(timeout=5)
            assert not emb._load_thread.is_alive()
            assert emb.evict_if_idle(50) is True
            assert not emb.is_ready()
        finally:
            epilogue.set()
            emb.close()

    def test_evict_if_idle_unloads_and_frees_the_model(self, tmp_path: Path, monkeypatch):
        emb, fake_cls, closed = self._loaded(tmp_path, monkeypatch)
        try:
            emb._last_used -= 100
            assert emb.evict_if_idle(50) is True
            assert not emb.is_ready()
            assert closed == [fake_cls.instances[0]]
            # The next interactive embed waits for the reload and gets a vector.
            assert emb.embed("again") is not None
            assert len(fake_cls.instances) == 2
        finally:
            emb.close()

    def test_eviction_never_frees_a_context_a_late_worker_still_holds(
        self, tmp_path: Path, monkeypatch
    ):
        """A worker started after eviction began must keep its context alive.

        Prefetch-only load: no worker exists when eviction starts. One is
        started (a first embed) just as close() takes the inference lock, and
        it outlives close()'s join. Freeing the context then would be a native
        use-after-free, so it must be left alone.
        """
        fake_cls = _make_fake_llama_class()
        closed: list[object] = []
        fake_cls.close = lambda self: closed.append(self)
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        monkeypatch.setattr(embeddings_mod, "_INFER_STOP_TIMEOUT_SECS", 0.05)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
        release = threading.Event()
        # flake-ok: pre-existing stub worker parked on release.wait until the finally sets it; a daemon that close() joins under the patched 0.05 s bound; moved verbatim, tracked for a de-flake pass
        late_worker = threading.Thread(target=release.wait, daemon=True)
        real_lock = emb._lock
        try:
            emb.prefetch()
            assert emb.wait_ready(timeout=5)
            assert emb._infer_thread is None
            emb._last_used -= 100

            class _LockThatStartsAWorker:
                """Stands in for the dispatch that starts the worker mid-eviction."""

                acquisitions = 0

                def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
                    type(self).acquisitions += 1
                    if type(self).acquisitions == 2:  # close() taking it
                        late_worker.start()
                        emb._infer_thread = late_worker
                    return real_lock.acquire(blocking, timeout)

                def release(self) -> None:
                    real_lock.release()

                def __enter__(self):
                    self.acquire()
                    return self

                def __exit__(self, *exc) -> None:
                    self.release()

            emb._lock = _LockThatStartsAWorker()
            assert emb.evict_if_idle(50) is True
            assert late_worker.is_alive()
            assert closed == []  # not freed under the live worker
        finally:
            release.set()
            emb._lock = real_lock
            emb.close()

    def test_eviction_never_frees_a_model_a_concurrent_retire_already_took(
        self, tmp_path: Path, monkeypatch
    ):
        """A retire() that wins the race owns the model; the evictor must not free it.

        The evictor passes its idle checks, then a model swap retires the embedder
        first: it clears the model and the worker, whose job still holds the
        context and outlives retire()'s join. The evictor's own close() then
        retires nothing, so freeing its saved snapshot would be a native
        use-after-free under that surviving worker.
        """
        fake_cls = _make_fake_llama_class()
        closed: list[object] = []
        fake_cls.close = lambda self: closed.append(self)
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        monkeypatch.setattr(embeddings_mod, "_INFER_STOP_TIMEOUT_SECS", 0.05)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
        release = threading.Event()
        # flake-ok: pre-existing stub worker parked on release.wait until the finally sets it; a daemon that close() joins under the patched 0.05 s bound; moved verbatim, tracked for a de-flake pass
        surviving_worker = threading.Thread(target=release.wait, daemon=True)
        real_lock = emb._lock
        try:
            emb.prefetch()
            assert emb.wait_ready(timeout=5)
            emb._last_used -= 100

            class _LockThatLetsARetireWin:
                """Stands in for a retire() that takes the lock just before the evictor."""

                acquisitions = 0

                def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
                    type(self).acquisitions += 1
                    if type(self).acquisitions == 2:  # the evictor's close() taking it
                        # What the winning retire() left behind: model and worker
                        # cleared, the worker still alive on its dequeued job.
                        surviving_worker.start()
                        emb._closed = True
                        emb._llm = None
                        emb._infer_thread = None
                    return real_lock.acquire(blocking, timeout)

                def release(self) -> None:
                    real_lock.release()

                def __enter__(self):
                    self.acquire()
                    return self

                def __exit__(self, *exc) -> None:
                    self.release()

            emb._lock = _LockThatLetsARetireWin()
            emb.evict_if_idle(50)
            assert surviving_worker.is_alive()
            assert closed == []  # the retired model is not freed under its worker
        finally:
            release.set()
            emb._lock = real_lock
            emb.close()

    def test_reload_refuses_weights_changed_on_disk(self, tmp_path: Path, monkeypatch):
        """A reload must map the weights model_id was computed from, or nothing."""
        emb, fake_cls, _closed = self._loaded(tmp_path, monkeypatch)
        try:
            emb._last_used -= 100
            assert emb.evict_if_idle(50) is True
            # Same path, same width, different bytes: the in-place swap case.
            _write_model_file(emb._model_path, payload=_model_bytes() + b"\x00" * 16)
            assert emb.embed("again") is None
            assert len(fake_cls.instances) == 1  # never constructed a second model
            assert not emb.is_ready()
            assert emb.can_load_on_demand() is False
        finally:
            emb.close()

    def test_recent_use_is_not_evicted(self, tmp_path: Path, monkeypatch):
        emb, _fake_cls, closed = self._loaded(tmp_path, monkeypatch)
        try:
            assert emb.evict_if_idle(50) is False
            assert emb.is_ready()
            assert closed == []
        finally:
            emb.close()

    def test_running_inference_is_not_evicted(self, tmp_path: Path, monkeypatch):
        emb, _fake_cls, closed = self._loaded(tmp_path, monkeypatch)
        try:
            emb._last_used -= 100
            with emb._lock:  # what the worker holds while a native call runs
                assert emb.evict_if_idle(50) is False
            assert emb.is_ready()
            assert closed == []
        finally:
            emb.close()

    def test_evictor_thread_unloads_after_the_configured_idle_time(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(embeddings_mod, "embedding_idle_evict_secs", lambda: 0.2)
        threads_before = _live_embed_threads()
        emb, _fake_cls, _closed = self._loaded(tmp_path, monkeypatch)
        wait_until(
            lambda: not emb.is_ready(), describe=lambda: "the idle evictor unloads the model"
        )
        emb.close()
        assert not (_live_embed_threads() - threads_before)

    def test_zero_disables_eviction(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            embeddings_mod, "_read_memory_config", lambda: {"embedding_idle_evict_secs": 0}
        )
        assert embeddings_mod.embedding_idle_evict_secs() == 0.0
        monkeypatch.setattr(
            embeddings_mod, "_read_memory_config", lambda: {"embedding_idle_evict_secs": "soon"}
        )
        assert embeddings_mod.embedding_idle_evict_secs() == embeddings_mod._DEFAULT_IDLE_EVICT_SECS

    @pytest.mark.parametrize("configured", [0, 120])
    def test_the_config_loader_keeps_the_configured_value(self, configured: int):
        """`config get` exports what is configured, so `config set --file` keeps it."""
        from kiro_crew.config.section_builders import _build_memory_config

        built = _build_memory_config({"embedding_idle_evict_secs": configured})
        assert built.embedding_idle_evict_secs == configured
        assert _build_memory_config({}).embedding_idle_evict_secs == 3600

    def test_close_stops_the_evictor(self, tmp_path: Path, monkeypatch):
        threads_before = _live_embed_threads()
        emb, _fake_cls, _closed = self._loaded(tmp_path, monkeypatch)
        assert emb._evict_thread is not None and emb._evict_thread.is_alive()
        emb.close()
        assert not (_live_embed_threads() - threads_before)


class TestColdLoadWait:
    def test_a_loadable_model_reads_available_without_loading(self, tmp_path, monkeypatch):
        from kiro_crew.knowledge.embedder import InProcessEmbedder

        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        backend = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
        monkeypatch.setattr(embeddings_mod, "get_shared_embedder", lambda: backend)
        try:
            assert backend.can_load_on_demand()
            kb = InProcessEmbedder()
            assert kb.is_available() is True
            assert fake_cls.init_attempts == 0  # asking did not load it
            # The search's own embed waits for the load and gets a vector.
            assert kb.embed("release plan") is not None
            assert fake_cls.init_attempts == 1
        finally:
            backend.close()

    def test_a_missing_file_is_not_loadable(self, tmp_path):
        backend = LlamaCppEmbedder(model_path=tmp_path / "missing.gguf")
        assert backend.can_load_on_demand() is False

    def test_a_failed_load_is_not_loadable_after_its_cooldown(self, tmp_path, monkeypatch):
        """A model that never yields vectors (bad dim, corrupt file) is not 'available'."""
        fail_cls = _make_failing_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fail_cls)
        backend = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
        assert backend.wait_ready(timeout=5) is False
        assert fail_cls.init_attempts == 1
        backend._load_failed_at -= embeddings_mod._LLM_LOAD_RETRY_SECS + 1  # cooldown over
        assert backend.can_load_on_demand() is False

    def test_watcher_residency_check_never_loads_an_unloaded_model(self, tmp_path, monkeypatch):
        """The watcher asks on every pass; asking must not undo an idle unload."""
        from kiro_crew.knowledge.embedder import InProcessEmbedder

        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        backend = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
        monkeypatch.setattr(embeddings_mod, "get_shared_embedder", lambda: backend)
        try:
            assert asyncio.run(InProcessEmbedder().model_resident_async()) is False
            assert fake_cls.init_attempts == 0
            assert backend._load_thread is None
        finally:
            backend.close()

    def test_event_loop_callers_never_wait_for_the_load(self, tmp_path: Path, monkeypatch):
        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))

        async def _on_loop():
            return emb.embed("hello")

        try:
            assert asyncio.run(_on_loop()) is None  # kicked, not awaited
            assert emb.wait_ready(timeout=5)
        finally:
            emb.close()

    def test_wait_is_bounded_by_the_callers_deadline(self, tmp_path: Path, monkeypatch):
        gate = threading.Event()
        fake_cls = _make_fake_llama_class()
        real_init = fake_cls.__init__

        def _slow_init(self, **kwargs):
            gate.wait(60)
            real_init(self, **kwargs)

        fake_cls.__init__ = _slow_init
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
        previous = embeddings_mod.embedding_work.get()
        # flake-ok: pre-existing 200 ms caller deadline; the loader stays gated until the finally, so the embed can only return on the deadline; moved verbatim, tracked for a de-flake pass
        embeddings_mod.embedding_work.set(embeddings_mod.EmbeddingWork(time.monotonic() + 0.2))
        try:
            assert emb.embed("hello") is None
            # It returned on the caller's deadline, not on the load: the load
            # is still held at the gate.
            assert not gate.is_set()
            assert emb._load_thread is not None and emb._load_thread.is_alive()
            assert not emb.is_ready()
        finally:
            embeddings_mod.embedding_work.set(previous)
            gate.set()
            emb.wait_ready(timeout=5)
            emb.close()
