"""A person's embed stops a background text inside the model instead of waiting for it.

The model runs one text at a time on one thread. A background (bulk) text that is
already inside the native call would hold a person's query embed for the rest of
that text: up to the prompt build's deadline, after which the turn loses vector
ranking. Instead the person's job arms the model's abort flag, the bulk text stops at
its next graph node, the person's job runs, and the bulk text starts over and still
finishes with the same vector. The fake model here stands in for llama.cpp: it
raises the binding's ``llama_decode returned 2`` error when the flag is set while a
bulk text is inside it.
"""

from __future__ import annotations

import ctypes
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from kiro_crew import embeddings as emb
from kiro_crew.embeddings_preempt import PreemptFlag, install_preempt_flag
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until


class _SignalledFlag(PreemptFlag):
    """The real flag, plus an event the fake model waits on instead of a graph node."""

    __slots__ = ("wake",)

    def __init__(self) -> None:
        super().__init__(ctypes.create_string_buffer(2), None)
        self.wake = threading.Event()

    def arm(self) -> None:
        super().arm()
        self.wake.set()


class _FakeModel:
    """A text starting with ``bulk`` stays inside the model until stopped or released."""

    def __init__(self) -> None:
        self.kiro_crew_preempt = _SignalledFlag()
        self.calls: list[str] = []
        self.inside = threading.Semaphore(0)
        self.release = threading.Event()
        self._lock = threading.Lock()

    def create_embedding(self, texts):
        text = texts[0]
        with self._lock:
            self.calls.append(text)
        if text.startswith("bulk"):
            self.inside.release()
            flag = self.kiro_crew_preempt
            while not self.release.is_set():
                if flag.fired():
                    flag.wake.clear()
                    raise RuntimeError("llama_decode returned 2")
                assert (
                    flag.wake.wait(FALLBACK_TIMEOUT_SECS) or self.release.is_set()
                ), "never stopped or released"
                if self.release.is_set():
                    break
        return {"data": [{"index": 0, "embedding": [float(len(text))]}]}

    def release_bulk(self) -> None:
        self.release.set()
        self.kiro_crew_preempt.wake.set()


@pytest.fixture
def backend(tmp_path, monkeypatch):
    # duty 1.0: no bulk pacing sleep, so the tests read order, not the clock.
    monkeypatch.setattr(emb, "_read_memory_config", lambda: {"embedding_bulk_duty": 1.0})
    instance = emb.LlamaCppEmbedder(model_path=tmp_path / "unused.gguf", dim=1)
    model = _FakeModel()
    instance._llm = model
    try:
        yield instance, model
    finally:
        model.release_bulk()
        instance.close()


def test_a_person_stops_the_bulk_text_inside_the_model_and_it_starts_over(backend):
    instance, model = backend
    with ThreadPoolExecutor(max_workers=2) as pool:
        bulk = pool.submit(instance.embed, "bulk text", priority=emb.PRIORITY_BULK)
        assert model.inside.acquire(timeout=FALLBACK_TIMEOUT_SECS)
        person = pool.submit(instance.embed, "q", priority=emb.PRIORITY_INTERACTIVE)
        # The person is served while the bulk text is still unreleased.
        assert person.result(timeout=FALLBACK_TIMEOUT_SECS) == [1.0]
        assert model.inside.acquire(
            timeout=FALLBACK_TIMEOUT_SECS
        ), "the bulk text did not start over"
        assert not bulk.done()
        model.release_bulk()
        assert bulk.result(timeout=FALLBACK_TIMEOUT_SECS) == [float(len("bulk text"))]
    assert model.calls == ["bulk text", "q", "bulk text"]


def test_each_text_of_a_bulk_batch_can_be_stopped(backend):
    instance, model = backend
    with ThreadPoolExecutor(max_workers=2) as pool:
        bulk = pool.submit(instance.embed_batch, ["plain", "bulk two"], priority=emb.PRIORITY_BULK)
        assert model.inside.acquire(timeout=FALLBACK_TIMEOUT_SECS)
        assert pool.submit(instance.embed, "q", priority=emb.PRIORITY_INTERACTIVE).result(
            timeout=FALLBACK_TIMEOUT_SECS
        ) == [1.0]
        assert model.inside.acquire(timeout=FALLBACK_TIMEOUT_SECS)
        model.release_bulk()
        assert bulk.result(timeout=FALLBACK_TIMEOUT_SECS) == [[5.0], [8.0]]
    assert model.calls == ["plain", "bulk two", "q", "bulk two"]


def test_after_the_stop_limit_the_text_runs_to_its_end(backend, monkeypatch):
    monkeypatch.setattr(sys_module(), "_MAX_BULK_PREEMPTS", 2)
    instance, model = backend
    with ThreadPoolExecutor(max_workers=2) as pool:
        bulk = pool.submit(instance.embed, "bulk text", priority=emb.PRIORITY_BULK)
        for _ in range(2):
            assert model.inside.acquire(timeout=FALLBACK_TIMEOUT_SECS)
            assert pool.submit(instance.embed, "q", priority=emb.PRIORITY_INTERACTIVE).result(
                timeout=FALLBACK_TIMEOUT_SECS
            ) == [1.0]
        assert model.inside.acquire(timeout=FALLBACK_TIMEOUT_SECS)
        late = pool.submit(instance.embed, "late", priority=emb.PRIORITY_INTERACTIVE)
        _submitted(instance)
        assert not model.kiro_crew_preempt.fired(), "a third stop was asked for"
        assert not late.done()
        model.release_bulk()
        assert bulk.result(timeout=FALLBACK_TIMEOUT_SECS) == [float(len("bulk text"))]
        assert late.result(timeout=FALLBACK_TIMEOUT_SECS) == [4.0]
    assert model.calls == ["bulk text", "q", "bulk text", "q", "bulk text", "late"]


def test_normal_work_does_not_stop_a_bulk_text(backend):
    instance, model = backend
    with ThreadPoolExecutor(max_workers=2) as pool:
        bulk = pool.submit(instance.embed, "bulk text", priority=emb.PRIORITY_BULK)
        assert model.inside.acquire(timeout=FALLBACK_TIMEOUT_SECS)
        normal = pool.submit(instance.embed, "n", priority=emb.PRIORITY_NORMAL)
        _submitted(instance)
        assert not model.kiro_crew_preempt.fired()
        model.release_bulk()
        assert bulk.result(timeout=FALLBACK_TIMEOUT_SECS) == [float(len("bulk text"))]
        assert normal.result(timeout=FALLBACK_TIMEOUT_SECS) == [1.0]
    assert model.calls == ["bulk text", "n"]


def test_a_person_joining_the_running_text_lets_it_finish(backend):
    instance, model = backend
    work = emb._work_for_priority(emb.PRIORITY_BULK)  # the product's own deadline-free bulk work
    with ThreadPoolExecutor(max_workers=2) as pool:

        def bulk_call():
            previous = emb.embedding_work.get()
            emb.embedding_work.set(work)
            try:
                return instance.embed("bulk text", priority=emb.PRIORITY_BULK)
            finally:
                emb.embedding_work.set(previous)

        bulk = pool.submit(bulk_call)
        assert model.inside.acquire(timeout=FALLBACK_TIMEOUT_SECS)
        instance.promote_pending(work, emb.PRIORITY_INTERACTIVE)
        other = pool.submit(instance.embed, "q", priority=emb.PRIORITY_INTERACTIVE)
        _submitted(instance)
        assert not model.kiro_crew_preempt.fired()
        model.release_bulk()
        assert bulk.result(timeout=FALLBACK_TIMEOUT_SECS) == [float(len("bulk text"))]
        assert other.result(timeout=FALLBACK_TIMEOUT_SECS) == [1.0]
    assert model.calls == ["bulk text", "q"]


def test_a_person_joining_a_queued_text_stops_the_one_inside_the_model(backend):
    """promote_pending on a QUEUED bulk text arms the other bulk text that is running."""
    instance, model = backend
    queued_work = emb._work_for_priority(emb.PRIORITY_BULK)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(instance.embed, "bulk running", priority=emb.PRIORITY_BULK)
        assert model.inside.acquire(timeout=FALLBACK_TIMEOUT_SECS)

        def queued_call():
            previous = emb.embedding_work.get()
            emb.embedding_work.set(queued_work)
            try:
                return instance.embed("q2", priority=emb.PRIORITY_BULK)
            finally:
                emb.embedding_work.set(previous)

        queued = pool.submit(queued_call)
        _submitted(instance)
        assert not model.kiro_crew_preempt.fired()
        instance.promote_pending(queued_work, emb.PRIORITY_INTERACTIVE)
        # The promoted text is served before the running one finishes.
        assert queued.result(timeout=FALLBACK_TIMEOUT_SECS) == [2.0]
        assert model.inside.acquire(
            timeout=FALLBACK_TIMEOUT_SECS
        ), "the stopped text did not start over"
        model.release_bulk()
        assert running.result(timeout=FALLBACK_TIMEOUT_SECS) == [float(len("bulk running"))]
    assert model.calls == ["bulk running", "q2", "bulk running"]


def test_a_failure_nobody_asked_for_still_fails_the_bulk_text(backend):
    instance, model = backend

    def broken(texts):
        raise RuntimeError("llama_decode returned -1")

    model.create_embedding = broken
    assert instance.embed("bulk text", priority=emb.PRIORITY_BULK) is None


def test_a_bulk_text_whose_backend_returns_none_runs_once(backend):
    """Only the stop sentinel restarts a text: a backend's own None result is a result."""
    instance, model = backend
    calls: list[list[str]] = []
    model.create_embedding = lambda texts: calls.append(texts)
    job = instance._submit_infer(model, ["bulk text"], emb.PRIORITY_BULK)
    assert job.result is None and job.error is None
    assert calls == [["bulk text"]]


def test_no_flag_is_installed_on_a_model_without_a_native_context():
    class _NoContext:
        pass

    assert install_preempt_flag(_NoContext()) is None


def _submitted(instance) -> None:
    """Wait until one job is queued AND its submitter left the dispatch section.

    ``_submit_infer`` queues the job and decides whether to arm the flag inside one
    hold of ``_dispatch_lock``; taking that lock once afterwards means the decision
    has been made, so a test reading the flag reads the outcome, not a gap.
    """
    wait_until(lambda: instance._jobs.qsize() == 1, timeout=FALLBACK_TIMEOUT_SECS)
    with instance._dispatch_lock:
        pass


def sys_module():
    import sys

    return sys.modules["kiro_crew.embeddings_llama"]
