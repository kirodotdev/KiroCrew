"""Tests for bulk-embedding pacing and the bulk thread class.

A post-migration re-embed sweep is the longest unattended CPU burn the gateway
does: measured 429 ms/row at 4 threads, so a few thousand imported memory rows
pin ~3.7 cores for tens of minutes and the machine's fans stay up. These lock in
the two dials that spread that work thinner without changing its total.
"""

from __future__ import annotations

import queue

import pytest

from kiro_crew import embeddings as emb


@pytest.fixture(autouse=True)
def _memory_cfg(monkeypatch):
    """Drive the module's raw-config reader instead of writing a config file.

    Also pins ``cpu_count``: both thread readers clamp to the machine's cores, so
    a test asserting an explicit count is otherwise environment-dependent — it
    passes on a 32-core dev host and fails on a 4-core CI runner. Tests that
    exercise the clamp itself override this with their own value.
    """
    cfg: dict = {}
    monkeypatch.setattr(emb, "_read_memory_config", lambda: cfg)
    monkeypatch.setattr(emb.os, "cpu_count", lambda: 16)
    return cfg


# ---------------------------------------------------------------------------
# bulk_duty_cycle
# ---------------------------------------------------------------------------


def test_duty_defaults_to_a_fifth(_memory_cfg):
    """Nothing waits on bulk work, so the default is tuned for invisibility."""
    assert emb.bulk_duty_cycle() == pytest.approx(0.2)


def test_duty_of_one_disables_pacing(_memory_cfg):
    _memory_cfg["embedding_bulk_duty"] = 1.0
    assert emb.bulk_duty_cycle() == 1.0
    assert emb.bulk_pace_delay(0.4) == 0.0


def test_duty_above_one_is_clamped_to_one(_memory_cfg):
    _memory_cfg["embedding_bulk_duty"] = 7
    assert emb.bulk_duty_cycle() == 1.0


def test_duty_typo_cannot_stall_the_sweep(_memory_cfg):
    """0.001 would stretch a multi-hour sweep into weeks; the floor prevents it."""
    _memory_cfg["embedding_bulk_duty"] = 0.001
    assert emb.bulk_duty_cycle() == pytest.approx(emb._MIN_BULK_DUTY)


@pytest.mark.parametrize("bad", [True, False, "0.5", None, [], {}])
def test_duty_rejects_non_numbers_and_bools(_memory_cfg, bad):
    _memory_cfg["embedding_bulk_duty"] = bad
    assert emb.bulk_duty_cycle() == pytest.approx(0.2)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_duty_rejects_non_finite(_memory_cfg, bad):
    """json.load accepts the NaN literal, and NaN slips through max() unclamped —
    which would silently disable pacing instead of falling back to the default."""
    _memory_cfg["embedding_bulk_duty"] = bad
    assert emb.bulk_duty_cycle() == pytest.approx(0.2)
    assert emb.bulk_pace_delay(0.2) == pytest.approx(0.8)


def test_an_integer_too_wide_for_a_double_does_not_abort_the_sweep(_memory_cfg):
    """`float()` on a 309-digit JSON integer raises OverflowError, which would
    propagate out through bulk_pace_delay and abort the sweep mid-corpus —
    leaving every pending row NULL. A config typo must degrade to the default."""
    _memory_cfg["embedding_bulk_duty"] = 10**400
    assert emb.bulk_duty_cycle() == pytest.approx(0.2)
    assert emb.bulk_pace_delay(0.5) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# bulk_pace_delay
# ---------------------------------------------------------------------------


def test_default_duty_idles_four_times_the_work(_memory_cfg):
    assert emb.bulk_pace_delay(1.0) == pytest.approx(4.0)


def test_half_duty_sleeps_as_long_as_it_worked(_memory_cfg):
    _memory_cfg["embedding_bulk_duty"] = 0.5
    assert emb.bulk_pace_delay(0.3) == pytest.approx(0.3)


def test_quarter_duty_sleeps_three_times_the_work(_memory_cfg):
    _memory_cfg["embedding_bulk_duty"] = 0.25
    assert emb.bulk_pace_delay(0.2) == pytest.approx(0.6)


def test_no_work_means_no_sleep(_memory_cfg):
    """A row that failed to embed consumed no time and must not be paced."""
    assert emb.bulk_pace_delay(0.0) == 0.0
    assert emb.bulk_pace_delay(-1.0) == 0.0


def test_the_cap_does_not_bind_an_ordinary_row_at_the_default(_memory_cfg):
    """The cap catches outliers only. If it bound a typical row it would silently
    override the configured duty on every row — measured worst case is ~1.4s of
    inference per row, so ~5.6s of idle must pass through untouched."""
    assert emb.bulk_pace_delay(1.4) == pytest.approx(5.6)
    assert emb.bulk_pace_delay(1.4) < emb._MAX_BULK_PACE_SLEEP


def test_one_pathological_row_cannot_park_the_sweep(_memory_cfg):
    _memory_cfg["embedding_bulk_duty"] = 0.05
    assert emb.bulk_pace_delay(30.0) == pytest.approx(emb._MAX_BULK_PACE_SLEEP)


# ---------------------------------------------------------------------------
# bulk_embed_threads
# ---------------------------------------------------------------------------


def test_bulk_threads_default_to_one(_memory_cfg):
    _memory_cfg["embedding_threads"] = 4
    assert emb.bulk_embed_threads() == 1
    # The interactive lane is untouched — that separation is the point.
    assert emb._embed_threads() == 4


def test_explicit_zero_inherits_the_interactive_count(_memory_cfg):
    _memory_cfg["embedding_threads"] = 3
    _memory_cfg["embedding_bulk_threads"] = 0
    assert emb.bulk_embed_threads() == 3


@pytest.mark.parametrize("bad", [-4, True, "2", None])
def test_bulk_threads_rejects_junk_without_inheriting(_memory_cfg, bad):
    """Junk falls back to the safe default, NOT to the interactive count — a typo
    must not silently hand a sweep the whole machine."""
    _memory_cfg["embedding_threads"] = 8
    _memory_cfg["embedding_bulk_threads"] = bad
    assert emb.bulk_embed_threads() == 1


def test_bulk_threads_override_is_honoured(_memory_cfg):
    _memory_cfg["embedding_threads"] = 4
    _memory_cfg["embedding_bulk_threads"] = 6
    assert emb.bulk_embed_threads() == 6
    assert emb._embed_threads() == 4


def test_bulk_threads_clamped_to_cores(_memory_cfg, monkeypatch):
    monkeypatch.setattr(emb.os, "cpu_count", lambda: 8)
    _memory_cfg["embedding_bulk_threads"] = 4096
    assert emb.bulk_embed_threads() == 8


# ---------------------------------------------------------------------------
# _apply_thread_class — the per-job reprogramming
# ---------------------------------------------------------------------------


class _FakeCtx:
    def __init__(self):
        self.calls: list[tuple[int, int]] = []

    def set_n_threads(self, n_threads: int, n_threads_batch: int) -> None:
        self.calls.append((n_threads, n_threads_batch))


class _FakeLlm:
    def __init__(self, ctx=None):
        self._ctx = ctx


def _embedder() -> emb.LlamaCppEmbedder:
    """A backend instance with no model load kicked off."""
    return emb.LlamaCppEmbedder.__new__(emb.LlamaCppEmbedder)  # type: ignore[misc]


def _armed_embedder() -> emb.LlamaCppEmbedder:
    inst = _embedder()
    inst._applied_threads = None
    inst._thread_class_unsupported = False
    return inst


def test_bulk_job_programs_the_bulk_pool(_memory_cfg):
    _memory_cfg["embedding_threads"] = 6
    _memory_cfg["embedding_bulk_threads"] = 2
    inst = _armed_embedder()
    ctx = _FakeCtx()
    inst._apply_thread_class(_FakeLlm(ctx), emb.PRIORITY_BULK)
    assert ctx.calls == [(2, 2)]


def test_interactive_job_restores_the_full_pool(_memory_cfg):
    _memory_cfg["embedding_threads"] = 6
    _memory_cfg["embedding_bulk_threads"] = 2
    inst = _armed_embedder()
    ctx = _FakeCtx()
    llm = _FakeLlm(ctx)
    inst._apply_thread_class(llm, emb.PRIORITY_BULK)
    inst._apply_thread_class(llm, emb.PRIORITY_INTERACTIVE)
    assert ctx.calls == [(2, 2), (6, 6)]


def test_unchanged_class_is_not_reprogrammed(_memory_cfg):
    _memory_cfg["embedding_threads"] = 4
    inst = _armed_embedder()
    ctx = _FakeCtx()
    llm = _FakeLlm(ctx)
    for _ in range(3):
        inst._apply_thread_class(llm, emb.PRIORITY_BULK)
    assert ctx.calls == [(1, 1)]


def test_a_backend_without_the_setter_degrades_once(_memory_cfg):
    """Losing the dial must never lose the embedding."""
    inst = _armed_embedder()
    inst._apply_thread_class(_FakeLlm(None), emb.PRIORITY_BULK)
    assert inst._thread_class_unsupported is True
    # And it never probes again, even once a usable context shows up.
    ctx = _FakeCtx()
    inst._apply_thread_class(_FakeLlm(ctx), emb.PRIORITY_BULK)
    assert ctx.calls == []


def test_a_raising_setter_does_not_record_a_count(_memory_cfg):
    class _Boom:
        def set_n_threads(self, *_a):
            raise RuntimeError("no")

    inst = _armed_embedder()
    inst._apply_thread_class(_FakeLlm(_Boom()), emb.PRIORITY_BULK)
    assert inst._applied_threads is None
    assert inst._thread_class_unsupported is True


def test_infer_loop_applies_the_class_before_inference(_memory_cfg):
    """The worker — not the caller — programs the pool, under its own lock."""
    _memory_cfg["embedding_threads"] = 6
    _memory_cfg["embedding_bulk_threads"] = 1
    inst = _armed_embedder()
    import threading

    inst._lock = threading.Lock()
    ctx = _FakeCtx()
    order: list[str] = []

    class _Llm(_FakeLlm):
        def create_embedding(self, texts):
            order.append(f"embed:{ctx.calls[-1][0]}")
            return {"data": [{"embedding": [0.0]}]}

    jobs: "queue.PriorityQueue" = queue.PriorityQueue()
    job = emb._InferJob(_Llm(ctx), ["hello"])
    jobs.put((emb.PRIORITY_BULK, 1, job))
    # Sentinel queued BEHIND the job (same class, later seq) so the worker runs
    # the job and then exits. _PRIORITY_SENTINEL would outrank it and the loop
    # would return having embedded nothing.
    jobs.put((emb.PRIORITY_BULK, 2, None))
    inst._infer_loop(jobs)

    assert job.done.is_set()
    assert order == ["embed:1"], "inference must run on the bulk pool, not the default one"
