"""The embedding runtime's threads: how many llama.cpp may use, and who waits.

``memory.embedding_threads`` pins llama.cpp's compute pools instead of letting
them size from the host core count; an embed reports its queue wait apart from
the model's own cost; and a short interactive embed never waits behind a queued
bulk sweep.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import embeddings_helpers
import pytest
from embeddings_helpers import _DIM, _make_fake_llama_class, _write_model_file

import kiro_crew.embeddings as embeddings_mod
from kiro_crew.embeddings import LlamaCppEmbedder
from kiro_crew.testing.wait import wait_until

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_reset_embedding_singletons = embeddings_helpers._reset_embedding_singletons


# ═══════════════════════════════════════════════════════════════════════════
# Embedding thread pinning (memory.embedding_threads)
# ═══════════════════════════════════════════════════════════════════════════


def _pin_cores(monkeypatch, count: int | None) -> None:
    """Pin every core-count source this platform offers.

    ``_embed_threads`` asks the platform how many CPUs the process may use, and
    WHICH call answers is a platform property: ``os.sched_getaffinity`` where it
    exists, ``os.cpu_count`` otherwise. Pinning both, and removing the affinity
    call for an unknown count, states the host without assuming Linux.
    """
    monkeypatch.setattr(os, "cpu_count", lambda: count)
    if not hasattr(os, "sched_getaffinity"):
        return
    if count is None:
        monkeypatch.delattr(os, "sched_getaffinity")
    else:
        monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(count)))


class TestEmbedThreads:
    """llama.cpp must not size its compute pools from the host core count.

    Left unset, ``n_threads_batch`` defaults to the CPU count, so even a
    few-token embed fans out across every core and competes with the rest of the
    gateway for CPU. These lock the resolver's clamping and prove the resolved
    value actually reaches the ``Llama`` constructor.
    """

    def test_default_when_unset(self, monkeypatch) -> None:
        # The resolver clamps to the core count, so a host with fewer cores than
        # the default answers with its own core count and the assertion would pin
        # the runner. Pinned above the default, the same way
        # ``test_clamped_to_the_core_count`` below pins it under one.
        _pin_cores(monkeypatch, 8)
        monkeypatch.setattr(embeddings_mod, "_read_memory_config", lambda: {})
        assert embeddings_mod._DEFAULT_EMBED_THREADS == 4
        assert embeddings_mod._embed_threads() == 4

    def test_configured_value_is_used(self, monkeypatch) -> None:
        monkeypatch.setattr(embeddings_mod, "_read_memory_config", lambda: {"embedding_threads": 6})
        _pin_cores(monkeypatch, 8)
        assert embeddings_mod._embed_threads() == 6

    @pytest.mark.parametrize("bad", [0, -1, True, False, "4", 2.5, None])
    def test_invalid_values_fall_back_to_the_default(self, monkeypatch, bad) -> None:
        """Booleans are rejected explicitly: ``True`` would coerce to 1 thread."""
        _pin_cores(monkeypatch, 8)
        monkeypatch.setattr(
            embeddings_mod, "_read_memory_config", lambda: {"embedding_threads": bad}
        )
        assert embeddings_mod._embed_threads() == 4

    @pytest.mark.parametrize("cores,expected", [(1, 1), (8, 8)])
    def test_clamped_to_core_count(self, monkeypatch, cores, expected) -> None:
        monkeypatch.setattr(
            embeddings_mod, "_read_memory_config", lambda: {"embedding_threads": 9999}
        )
        _pin_cores(monkeypatch, cores)
        assert embeddings_mod._embed_threads() == expected

    @pytest.mark.parametrize("cores,expected", [(1, 1), (2, 1), (4, 3), (16, 4)])
    def test_unset_leaves_one_core_free(self, monkeypatch, cores, expected) -> None:
        """Unset means llama.cpp never gets the whole box.

        Handing every core to the batch pool starves the event loop the gateway
        answers on, which a 2-vCPU host feels hardest. The 16-core expectation
        is the one that matters twice: the cap is a CEILING on the four-thread
        default, so a big host answers 4, not 15.
        """
        monkeypatch.setattr(embeddings_mod, "_read_memory_config", lambda: {})
        _pin_cores(monkeypatch, cores)
        assert embeddings_mod._embed_threads() == expected
        if cores > 1:
            assert embeddings_mod._embed_threads() < cores

    @pytest.mark.parametrize("cores,expected", [(1, 1), (2, 1), (4, 3), (16, 4)])
    def test_the_declared_default_in_config_is_not_operator_intent(
        self, monkeypatch, cores, expected
    ) -> None:
        """A whole-document config save materializes the declared default.

        ``MemoryConfig.embedding_threads`` is a dataclass field defaulting to 4
        and ``KiroCrewConfig.save()`` publishes every field, so a fresh
        install's ``config.json`` carries a 4 nobody typed. Reading that as a
        choice hands the whole box to the very hosts the cap protects, so a raw
        value equal to the default takes the same ceiling as an absent one.
        """
        assert embeddings_mod._DEFAULT_EMBED_THREADS == 4, (
            "Raising this past 4 reinterprets every config.json already carrying 4: "
            "it stops matching the declared default, becomes an explicit choice, and "
            "hands back the full core count on the small hosts this cap protects. "
            "Migrate those files before changing it."
        )
        monkeypatch.setattr(
            embeddings_mod,
            "_read_memory_config",
            lambda: {"embedding_threads": embeddings_mod._DEFAULT_EMBED_THREADS},
        )
        _pin_cores(monkeypatch, cores)
        assert embeddings_mod._embed_threads() == expected

    @pytest.mark.parametrize("cores,configured", [(1, 1), (2, 2), (4, 3), (16, 8)])
    def test_a_value_other_than_the_default_is_honoured_unclamped(
        self, monkeypatch, cores, configured
    ) -> None:
        """An operator asking for every core on a small host still gets it."""
        monkeypatch.setattr(
            embeddings_mod, "_read_memory_config", lambda: {"embedding_threads": configured}
        )
        _pin_cores(monkeypatch, cores)
        assert embeddings_mod._embed_threads() == configured

    def test_a_cpu_set_decides_the_cap_not_the_machine(self, monkeypatch) -> None:
        """Two cores out of sixty-four means two cores.

        ``os.cpu_count`` reports the whole host inside a cpuset, so reading it
        would hand llama.cpp four threads on a two-core allowance. Skipped where
        the platform cannot narrow affinity at all -- a probe of ``os``, so a
        reader that stopped consulting affinity fails here rather than skipping.
        """
        if not hasattr(os, "sched_getaffinity"):
            pytest.skip("this platform has no os.sched_getaffinity to narrow")
        monkeypatch.setattr(embeddings_mod, "_read_memory_config", lambda: {})
        monkeypatch.setattr(os, "cpu_count", lambda: 64)
        monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1})
        assert embeddings_mod._embed_threads() == 1

    def test_an_operator_value_is_clamped_to_the_cpu_set(self, monkeypatch) -> None:
        """An explicit value is still honoured, up to what the process may use."""
        if not hasattr(os, "sched_getaffinity"):
            pytest.skip("this platform has no os.sched_getaffinity to narrow")
        monkeypatch.setattr(embeddings_mod, "_read_memory_config", lambda: {"embedding_threads": 8})
        monkeypatch.setattr(os, "cpu_count", lambda: 64)
        monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1})
        assert embeddings_mod._embed_threads() == 2

    def test_the_host_count_answers_without_an_affinity_api(self, monkeypatch) -> None:
        """macOS and Windows have no affinity call, and keep the old reading."""
        monkeypatch.setattr(embeddings_mod, "_read_memory_config", lambda: {})
        monkeypatch.delattr(os, "sched_getaffinity", raising=False)
        monkeypatch.setattr(os, "cpu_count", lambda: 8)
        assert embeddings_mod._embed_threads() == 4

    def test_unknown_core_count_keeps_the_flat_default(self, monkeypatch) -> None:
        """No count means no core to subtract, so the default is not reduced."""
        monkeypatch.setattr(embeddings_mod, "_read_memory_config", lambda: {})
        _pin_cores(monkeypatch, None)
        assert embeddings_mod._embed_threads() == embeddings_mod._DEFAULT_EMBED_THREADS

    def test_threads_reach_the_llama_constructor(self, tmp_path: Path, monkeypatch) -> None:
        """BOTH pools are pinned, not only the batch pool that runs inference."""
        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        monkeypatch.setattr(embeddings_mod, "_read_memory_config", lambda: {"embedding_threads": 3})
        _pin_cores(monkeypatch, 8)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
        assert emb.wait_ready(timeout=5)
        kwargs = fake_cls.instances[0].kwargs
        assert kwargs["n_threads"] == 3
        assert kwargs["n_threads_batch"] == 3

    def test_physical_batch_bounds_scratch_without_reducing_context(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
        assert emb.wait_ready(timeout=5)
        kwargs = fake_cls.instances[0].kwargs
        assert kwargs["n_ctx"] == embeddings_mod._N_CTX
        assert kwargs["n_batch"] == embeddings_mod._N_CTX
        assert kwargs["n_ubatch"] == embeddings_mod._N_UBATCH
        assert kwargs["n_ubatch"] < kwargs["n_batch"]


# ═══════════════════════════════════════════════════════════════════════════
# Embed timing + inference queue priority
# ═══════════════════════════════════════════════════════════════════════════


class TestEmbedQueueTiming:
    """Queue wait must be reported separately from the model's own cost.

    The process shares ONE model on ONE thread, so a short embed can be slow
    purely because it queued behind a long one. That is a different defect from
    slow inference, with a different fix, and these assert the instrumentation
    tells the two apart.
    """

    def test_a_queued_embed_reports_wait_not_inference(self, tmp_path: Path, monkeypatch) -> None:
        """The split is checked as an ORDER of events, never as a stopwatch.

        What ``_emit_embed_timing`` is handed is a pair of instants per call:
        ``t0`` (submitted) and ``t_started`` (the worker entered the model).
        The property is where the hold lands between them: the bulk embed was
        already inside the model before the short one was submitted, so nothing
        it reports as wait is queueing; the short embed entered the model only
        after the bulk one was released, so the whole hold is its wait and none
        of it is its inference. Both are facts about ordering, so they hold on
        any runner. A millisecond ceiling on the bulk embed's wait would not:
        that interval is the first submission spawning and scheduling the worker
        thread, which a loaded host stretches arbitrarily and a coarse monotonic
        clock (Windows ticks at ~15.6 ms) rounds further still.
        """
        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
        assert emb.wait_ready(timeout=5)

        # (t0, t_started, chars) as handed to the real emitter, so the assertions
        # read the same instants the log line is computed from.
        emitted: list[tuple[float, float, int]] = []
        real_emit = embeddings_mod._emit_embed_timing

        def _spy(t0, t_started, texts, *, priority=embeddings_mod.PRIORITY_NORMAL, failed=False):
            emitted.append((t0, t_started, sum(len(t) for t in texts)))
            real_emit(t0, t_started, texts, priority=priority, failed=failed)

        monkeypatch.setattr(embeddings_mod, "_emit_embed_timing", _spy)

        holding = threading.Event()
        release = threading.Event()

        def _create(texts):
            if sum(len(t) for t in texts) > 100:
                holding.set()
                release.wait(timeout=5)
            return {"data": [{"embedding": [0.1] * _DIM} for _ in texts]}

        fake_cls.instances[0].create_embedding = _create

        bulk = threading.Thread(target=lambda: emb.embed("x" * 4000))
        bulk.start()
        assert holding.wait(timeout=5), "bulk embed never started"

        # Patch the queue only NOW: the first submission REPLACES self._jobs with
        # a fresh PriorityQueue when it spawns the worker, so a wrapper installed
        # before that point lands on the discarded original and never fires.
        # Deterministic barrier: releasing before the short embed is enqueued
        # would let it run unqueued and the wait under test would never exist.
        queued = threading.Event()
        real_put = emb._jobs.put

        def _put(item):
            real_put(item)
            _prio, _seq, job = item
            if job is not None and sum(len(t) for t in job.texts) == 2:
                queued.set()

        monkeypatch.setattr(emb._jobs, "put", _put)

        short = threading.Thread(target=lambda: emb.embed("hi"))
        short.start()
        assert queued.wait(timeout=5), "short embed never reached the queue"
        released_at = time.monotonic()
        release.set()
        bulk.join(timeout=10)
        short.join(timeout=10)

        big = [e for e in emitted if e[2] == 4000]
        small = [e for e in emitted if e[2] == 2]
        assert len(big) == 1 and len(small) == 1
        _big_t0, big_started, _ = big[0]
        small_t0, small_started, _ = small[0]
        # The bulk embed was in the model before the short one was submitted:
        # its reported wait holds only the worker's start-up, never a queue.
        assert big_started <= small_t0, (
            f"bulk entered the model {big_started - small_t0:.3f}s after the short "
            "embed was submitted, so its wait would include queueing"
        )
        # The short embed entered the model only once the hold was released, so
        # the hold is reported as wait and none of it as inference.
        assert small_started >= released_at, (
            f"short embed entered the model {released_at - small_started:.3f}s "
            "before the bulk one was released"
        )

    def test_chars_bucket_is_low_cardinality(self) -> None:
        assert embeddings_mod._chars_bucket(2) == "<=128"
        assert embeddings_mod._chars_bucket(400) == "<=512"
        assert embeddings_mod._chars_bucket(4000) == "<=6000"
        assert embeddings_mod._chars_bucket(99_999) == ">6000"


class TestEmbedPriority:
    """A short interactive embed must not wait behind a queued bulk sweep.

    A CALLER that holds ``_lock`` across submit+wait makes this impossible:
    every other caller blocks before it can enqueue and at most one job is
    ever queued. The lock moved to the worker precisely so ordering can exist.
    """

    def _gated(self, tmp_path: Path, monkeypatch):
        """Start a gated worker and return (emb, order, release, queued, threads).

        The gate job is already RUNNING on return, which matters for two reasons:
        the first submission replaces ``self._jobs`` with a fresh queue when it
        spawns the worker (so the recording wrapper has to be installed after
        that, not before), and ``queued`` then counts only the submissions a test
        actually cares about. ``queued`` is what lets a test release on a
        deterministic barrier instead of a fixed sleep: on a loaded runner a sleep
        can release before every submitter has enqueued, and the ordering
        assertion would then measure scheduling rather than priority.
        """
        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
        assert emb.wait_ready(timeout=5)
        order: list[str] = []
        holding = threading.Event()
        release = threading.Event()

        def _create(texts):
            label = texts[0]
            if label == "gate":
                holding.set()
                release.wait(timeout=5)
            order.append(label)
            return {"data": [{"embedding": [0.1] * _DIM} for _ in texts]}

        fake_cls.instances[0].create_embedding = _create

        threads = [threading.Thread(target=lambda: emb.embed("gate"))]
        threads[0].start()
        assert holding.wait(timeout=5), "worker never entered the gate job"

        queued: list[str] = []
        queued_lock = threading.Lock()
        real_put = emb._jobs.put

        def _put(item):
            real_put(item)
            _prio, _seq, job = item
            if job is not None:
                with queued_lock:
                    queued.append(job.texts[0])

        monkeypatch.setattr(emb._jobs, "put", _put)
        return emb, order, release, queued, threads

    @staticmethod
    def _await_queued(queued: list, expected: int, timeout: float = 5.0) -> None:
        """Block until *expected* submissions have reached the queue."""
        wait_until(
            lambda: len(queued) >= expected,
            timeout=timeout,
            describe=lambda: f"only {len(queued)} of {expected} enqueued: {queued}",
        )

    def test_interactive_preempts_queued_bulk(self, tmp_path: Path, monkeypatch) -> None:
        emb, order, release, queued, threads = self._gated(tmp_path, monkeypatch)

        # Bulk is submitted FIRST and still must lose to the later query.
        for i in range(3):
            t = threading.Thread(
                target=lambda i=i: emb.embed(f"bulk{i}", priority=embeddings_mod.PRIORITY_BULK)
            )
            t.start()
            threads.append(t)
        self._await_queued(queued, 3)
        q = threading.Thread(
            target=lambda: emb.embed("query", priority=embeddings_mod.PRIORITY_INTERACTIVE)
        )
        q.start()
        threads.append(q)
        self._await_queued(queued, 4)
        release.set()
        for t in threads:
            t.join(timeout=10)

        assert order[0] == "gate"
        assert order[1] == "query", f"interactive must preempt queued bulk: {order}"
        assert set(order[2:]) == {"bulk0", "bulk1", "bulk2"}

    def test_equal_priority_stays_fifo(self, tmp_path: Path, monkeypatch) -> None:
        """The seq tiebreaker keeps ordering stable, so priority is not a lottery."""
        emb, order, release, queued, threads = self._gated(tmp_path, monkeypatch)
        for i in range(4):
            t = threading.Thread(target=lambda i=i: emb.embed(f"n{i}"))
            t.start()
            threads.append(t)
            # Confirm each submission is enqueued before starting the next, so
            # FIFO order is well-defined: with all four racing, any interleaving
            # is a legal seq order and the assertion would be testing the
            # scheduler rather than the tiebreaker.
            self._await_queued(queued, i + 1)
        release.set()
        for t in threads:
            t.join(timeout=10)
        assert order == ["gate", "n0", "n1", "n2", "n3"], order

    def test_close_does_not_abandon_a_queued_caller(self, tmp_path: Path, monkeypatch) -> None:
        """A job already queued when the sentinel arrives is failed, not hung.

        Retirement and dispatch now share a lock, so a LATE submission fails fast
        instead of queueing. This covers the other half: a job legally enqueued
        before ``close()`` still has to be failed by the drain.
        """
        emb, _order, release, queued, threads = self._gated(tmp_path, monkeypatch)
        results: list[object] = []
        late = threading.Thread(target=lambda: results.append(emb.embed("late")))
        late.start()
        self._await_queued(queued, 1)  # 'late' is genuinely on the queue now

        release.set()
        closer = threading.Thread(target=emb.close)
        closer.start()
        closer.join(timeout=15)
        assert not closer.is_alive(), "close() must not block on the in-flight job"
        late.join(timeout=10)
        assert not late.is_alive(), "a queued caller must never wait forever"
        for t in threads:
            t.join(timeout=10)
        assert len(results) == 1

    def test_a_retired_backend_fails_fast_instead_of_hanging(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A submission after close() must not queue into a space nothing drains.

        This is the hang the dispatch lock exists to prevent: close() swaps BOTH
        the worker and the queue, so a caller that passed the liveness check and
        then enqueued would wait on ``job.done`` forever.
        """
        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
        assert emb.wait_ready(timeout=5)
        llm = fake_cls.instances[0]
        emb.close()

        # Submitting the STALE llm handle is exactly what a caller holding a
        # pre-close reference does; it must fail, not block.
        done = threading.Event()
        out: list = []

        def _submit():
            out.append(emb._submit_infer(llm, ["stale"]))
            done.set()

        threading.Thread(target=_submit, daemon=True).start()
        assert done.wait(timeout=5), "_submit_infer hung on a retired backend"
        assert out[0].error is not None
        assert "retired" in str(out[0].error)
