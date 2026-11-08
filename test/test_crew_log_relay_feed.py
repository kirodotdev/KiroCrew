"""The board relay decides off the writer, on the board fixed when the entry committed.

Each test runs the real crew-log writer and the real relay consumer on a temp data home,
with a real binding file, so the binding read, the header read and the board's newest
unit are the ones a gateway uses.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import work_relay, work_relay_feed
from kiro_crew.crew_log.emit_runtime import _write as emit_write
from kiro_crew.testing.wait import default_timeout, wait_until
from kiro_crew.work_ledger_writes import _write_binding

LEAD = "chat-relay-feed-lead"
OTHER = "chat-relay-feed-other"
WORKER = "chat-relay-feed-worker"
LEAD_UNIT = "u-feed-lead"
OTHER_UNIT = "u-feed-other"
WORKER_UNIT = "u-feed-worker"


@pytest.fixture(autouse=True)
def _live_crew_log(tmp_path):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        patch.setenv("KIROCREW_CREW_LOG", "1")
        crew_log_emit.reset_caches()
        crew_log_eager.stop_for_tests()
        crew_log.forget_slot_folds()
        work_relay.forget_for_tests()
        yield patch
        crew_log_emit.flush()
        crew_log_eager.stop_for_tests()
        crew_log.forget_slot_folds()
        work_relay.forget_for_tests()
        crew_log_emit.reset_caches()


def _unit(unit: str, slot: str) -> None:
    CrewLog.create(lg.KIND_SESSION, unit, owner="owner", agent="lead", slot=slot)


def _noted_unit(unit: str, slot: str) -> None:
    """A unit the writer knows the slot of, as a unit opened through the emitter is."""
    from kiro_crew.crew_log import writer_groups

    _unit(unit, slot)
    writer_groups.note_unit_slot(unit, slot)


def _settle() -> None:
    assert crew_log_emit.flush(timeout=default_timeout())
    assert crew_log_eager.drain(timeout=default_timeout())


def _boards() -> None:
    for slot, unit in ((LEAD, LEAD_UNIT), (OTHER, OTHER_UNIT)):
        _unit(unit, slot)
        data = {"slot": slot, "actor": "conductor", "by": slot, "action": "goal", "goal": "g"}
        assert crew_log_emit.on_work_recorded(unit, {**data, "round": 1}, timeout=default_timeout())
    _settle()


def _emit_closer(turn: int, credits: float) -> None:
    emit_write(
        WORKER_UNIT,
        "turn/completed",
        {
            "turn": turn,
            "stop_reason": "end_turn",
            "depth": 0,
            "duration_ms": 10,
            "model": "opus",
            "provider": "kiro",
            "credits": credits,
            "tokens": {"input": 5, "output": 1, "cache_read": 0, "cache_write": 0},
        },
    )


def _charges(unit: str = LEAD_UNIT) -> "list[float]":
    log = CrewLog.open(lg.KIND_SESSION, unit)
    return [e.data["credits"] for e in log.iter_from(1) if e.type == "work/charged"]


class _Gate:
    """Park the relay's next decision (that one only) until the test opens the gate."""

    def __init__(self, patch: pytest.MonkeyPatch) -> None:
        self.entered = threading.Event()
        self.opened = threading.Event()
        real = work_relay.decide

        def parked(*args: Any, **kwargs: Any) -> None:
            if not self.entered.is_set():
                self.entered.set()
                assert self.opened.wait(default_timeout()), "the gate was never opened"
            real(*args, **kwargs)

        patch.setattr(work_relay, "decide", parked)

    def wait_parked(self) -> None:
        assert self.entered.wait(default_timeout()), "the relay never reached its decision"

    def open(self) -> None:
        self.opened.set()


def test_the_writer_lane_never_looks_up_the_board_unit(_live_crew_log):
    """The newest-unit lookup (a whole-store listing on a cold start) runs on the relay's
    own thread; a unit the writer opened costs it no header read either."""
    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")
    threads: "list[str]" = []
    real_newest = session_ledger.newest_work_unit

    def newest(board: str, **kwargs: Any) -> str:
        threads.append(threading.current_thread().name)
        return real_newest(board, **kwargs)

    def no_header(unit_id: str) -> str:
        raise AssertionError("the writer read a header for a unit it opened")

    _live_crew_log.setattr(session_ledger, "newest_work_unit", newest)
    _live_crew_log.setattr(work_relay, "_unit_slot", no_header)
    _emit_closer(1, 2.5)
    _settle()
    assert _charges() == [2.5]
    assert threads and set(threads) == {"crew-log-relay"}, threads


def test_a_rebind_before_the_relay_decides_keeps_the_closer_on_its_own_board(_live_crew_log):
    """Bound to the lead when it committed: rebinding first does not move or drop it."""
    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")
    gate = _Gate(_live_crew_log)
    _emit_closer(1, 1.0)
    gate.wait_parked()
    _write_binding(WORKER, OTHER, "it_0000000b")
    gate.open()
    _settle()
    assert _charges(LEAD_UNIT) == [1.0]
    assert _charges(OTHER_UNIT) == []


def test_a_bind_after_the_commit_does_not_charge_the_closer(_live_crew_log):
    """Unbound when it committed: a bind before the relay decides charges nothing."""
    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _emit_closer(1, 1.0)
    _settle()
    _write_binding(WORKER, LEAD, "it_0000000a")
    _emit_closer(2, 2.0)
    _settle()
    assert _charges() == [2.0]


def test_a_full_queue_is_decided_inline_in_order_and_nothing_is_dropped(_live_crew_log):
    """With the consumer parked on closer 1, closer 2 queues and closer 3 finds the queue
    full: the writer decides it inline, after closer 2, once the consumer lets go."""
    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")
    _live_crew_log.setattr(work_relay_feed, "QUEUE_LIMIT", 1)
    gate = _Gate(_live_crew_log)
    deciders: "list[str]" = []
    parked = work_relay.decide

    def noted(*args: Any, **kwargs: Any) -> None:
        deciders.append(threading.current_thread().name)
        parked(*args, **kwargs)

    _live_crew_log.setattr(work_relay, "decide", noted)
    _emit_closer(1, 1.0)
    gate.wait_parked()
    _emit_closer(2, 2.0)
    wait_until(lambda: len(work_relay_feed._queue) == 1, describe=lambda: work_relay_feed._queue)
    inline = threading.Event()
    real_batch = work_relay_feed._decide_batch

    def batch(also: Any = None) -> None:
        if also is not None:
            inline.set()
        real_batch(also)

    _live_crew_log.setattr(work_relay_feed, "_decide_batch", batch)
    # The append job can run on the caller's thread, and an inline decision waits for
    # the parked consumer, so closer 3 is written from its own thread.
    third = threading.Thread(target=_emit_closer, args=(3, 3.0))
    third.start()
    assert inline.wait(default_timeout()), "closer 3 was not decided inline"
    # The inline decision waits for the parked one: still running after a short look.
    third.join(0.2)  # flake-ok: alive is certain while the consumer holds the lock
    assert third.is_alive(), "the inline decision did not wait for the one in progress"
    gate.open()
    third.join(default_timeout())
    _settle()
    assert _charges() == [1.0, 2.0, 3.0]
    assert len(deciders) == 3
    assert deciders[0] == "crew-log-relay"
    assert any(name != "crew-log-relay" for name in deciders), "nothing was decided inline"


def test_copies_decided_together_are_written_with_one_append_many(_live_crew_log):
    """Three closers decided in one batch reach the board unit in one write, in order."""
    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")
    gate = _Gate(_live_crew_log)
    _emit_closer(1, 1.0)
    gate.wait_parked()
    gate.open()
    _settle()
    calls: "list[int]" = []
    real = CrewLog.append_many

    def counted(self: Any, items: Any, **kwargs: Any) -> Any:
        items = list(items)
        if self.id == LEAD_UNIT:
            calls.append(len(items))
        return real(self, items, **kwargs)

    _live_crew_log.setattr(CrewLog, "append_many", counted)
    second = _Gate(_live_crew_log)
    _emit_closer(2, 2.0)
    second.wait_parked()
    _emit_closer(3, 3.0)
    _emit_closer(4, 4.0)
    _settle_writer_only()
    second.open()
    _settle()
    assert _charges() == [1.0, 2.0, 3.0, 4.0]
    assert calls == [1, 2] or calls == [3], calls


def _settle_writer_only() -> None:
    from kiro_crew.crew_log import emit_runtime

    assert emit_runtime._writer().flush(timeout=default_timeout())


def test_a_refused_copy_in_a_run_costs_only_itself(_live_crew_log):
    """A run the store refuses whole is written one at a time; only the bad copy is lost."""
    from kiro_crew.crew_log.errors import CrewLogError

    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")
    real_many, real_one = CrewLog.append_many, CrewLog.append

    def refuse_many(self: Any, items: Any, **kwargs: Any) -> Any:
        items = list(items)
        if self.id == LEAD_UNIT and len(items) > 1:
            raise CrewLogError("one member is oversize", code="too_large")
        return real_many(self, items, **kwargs)

    def refuse_two(self: Any, entry_type: str, data: Any, **kwargs: Any) -> Any:
        if self.id == LEAD_UNIT and data.get("credits") == 2.0:
            raise CrewLogError("oversize", code="too_large")
        return real_one(self, entry_type, data, **kwargs)

    _live_crew_log.setattr(CrewLog, "append_many", refuse_many)
    _live_crew_log.setattr(CrewLog, "append", refuse_two)
    gate = _Gate(_live_crew_log)
    _emit_closer(1, 1.0)
    gate.wait_parked()
    _emit_closer(2, 2.0)
    _emit_closer(3, 3.0)
    _settle_writer_only()
    gate.open()
    _settle()
    assert 2.0 not in _charges()
    assert _charges()[-1] == 3.0


def test_flush_waits_for_a_copy_the_relay_hands_over_after_the_writer_drained(
    _live_crew_log,
):
    """The relay hands its copy to the writer just after the writer drained: still waited."""
    from kiro_crew.crew_log import emit_runtime

    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")
    real_hand_over = work_relay.hand_over
    writer_drained = threading.Event()
    handed = threading.Event()

    def late_hand_over(copies: Any) -> None:
        assert writer_drained.wait(default_timeout())
        real_hand_over(copies)
        handed.set()

    _live_crew_log.setattr(work_relay, "hand_over", late_hand_over)
    writer = emit_runtime._writer()
    real_flush = writer.flush
    first = {"done": False}

    def flush_then_let_the_relay_finish(timeout: Any = 5.0) -> bool:
        drained = real_flush(timeout=timeout)
        if not first["done"]:
            first["done"] = True
            writer_drained.set()
            assert handed.wait(default_timeout())
        return drained

    _live_crew_log.setattr(writer, "flush", flush_then_let_the_relay_finish)
    _emit_closer(1, 1.0)
    assert crew_log_emit.flush(timeout=default_timeout())
    assert _charges() == [1.0]


def test_the_shutdown_drain_writes_what_the_relay_had_queued(_live_crew_log):
    """``drain_for_shutdown`` decides the queued wakes first, so their copies land."""
    from kiro_crew.crew_log import emit_runtime

    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")
    gate = _Gate(_live_crew_log)
    _emit_closer(1, 1.0)
    gate.wait_parked()
    _emit_closer(2, 2.0)
    threading.Timer(0.0, gate.open).start()
    assert emit_runtime.drain_for_shutdown(default_timeout())
    assert _charges() == [1.0, 2.0]


SUB_WORKER = "chat-relay-feed-sub-worker"
SUB_WORKER_UNIT = "u-feed-sub-worker"


class _GonePool:
    """The writer pool as interpreter exit leaves it: refuses every new job."""

    def submit(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("cannot schedule new futures after shutdown")


def _nested_boards() -> None:
    """Lead board <- worker's own sub-board <- the sub-board's worker."""
    _unit(LEAD_UNIT, LEAD)
    _noted_unit(WORKER_UNIT, WORKER)
    _noted_unit(SUB_WORKER_UNIT, SUB_WORKER)
    for slot, unit in ((LEAD, LEAD_UNIT), (WORKER, WORKER_UNIT)):
        data = {"slot": slot, "actor": "conductor", "by": slot, "action": "goal", "goal": "g"}
        assert crew_log_emit.on_work_recorded(unit, {**data, "round": 1}, timeout=default_timeout())
    _write_binding(WORKER, LEAD, "it_0000000a")
    _write_binding(SUB_WORKER, WORKER, "it_0000000b")
    _settle()


def _hand_over_after_each_drain(patch: pytest.MonkeyPatch) -> threading.Event:
    """Hold a hand-over decided while the writer's shutdown drain runs until that drain has
    returned, so each such copy is the late one the drain did not see."""
    from kiro_crew.crew_log import emit_runtime

    writer = emit_runtime._writer()
    returned = threading.Event()
    returned.set()  # no drain running yet
    real_drain, real_hand_over = writer.drain_for_shutdown, work_relay.hand_over

    def drain(timeout: float) -> Any:
        returned.clear()
        try:
            return real_drain(timeout)
        finally:
            returned.set()

    def late(copies: Any) -> None:
        assert returned.wait(default_timeout())
        real_hand_over(copies)

    patch.setattr(writer, "drain_for_shutdown", drain)
    patch.setattr(work_relay, "hand_over", late)
    return returned  # clear while a writer drain runs


def _sub_closer(credits: float) -> None:
    """The sub-board worker's closer, left buffered for the shutdown drain to write."""
    from kiro_crew.crew_log import emit_runtime

    with pytest.MonkeyPatch.context() as buffered:
        buffered.setattr(emit_runtime._writer(), "_claim_inline", lambda unit: None)
        _emit_sub_closer(credits)


def _emit_sub_closer(credits: float) -> None:
    emit_write(
        SUB_WORKER_UNIT,
        "turn/completed",
        {
            "turn": 1,
            "stop_reason": "end_turn",
            "depth": 0,
            "duration_ms": 10,
            "credits": credits,
            "tokens": {"input": 5, "output": 1, "cache_read": 0, "cache_write": 0},
        },
    )


def _sub_charges(unit: str) -> "list[str]":
    log = CrewLog.open(lg.KIND_SESSION, unit)
    return [e.data["by"] for e in log.iter_from(1) if e.type == "work/charged"]


def test_the_shutdown_drain_lands_a_nested_boards_parent_copy_once(_live_crew_log):
    """With the writer pool gone, the drain writes the closer, then the sub-board's copy,
    then the parent's copy the relay made from it: each round queues the next."""
    from kiro_crew.crew_log import emit_runtime

    _nested_boards()
    _live_crew_log.setattr(emit_runtime._writer(), "_executor", lambda: _GonePool())
    _hand_over_after_each_drain(_live_crew_log)
    _sub_closer(1.5)
    assert emit_runtime.drain_for_shutdown(default_timeout())
    assert _sub_charges(WORKER_UNIT) == [SUB_WORKER]
    assert _sub_charges(LEAD_UNIT) == [SUB_WORKER]


def test_a_relay_first_loaded_during_the_shutdown_drain_is_settled(_live_crew_log):
    """The drain's own first relayable append is what loads the relay module."""
    import sys

    from kiro_crew.crew_log import emit_runtime

    _nested_boards()
    name = "kiro_crew.crew_log.work_relay_feed"
    _live_crew_log.setattr(emit_runtime._writer(), "_executor", lambda: _GonePool())
    _live_crew_log.delitem(sys.modules, name)
    real_note = work_relay_feed.note
    no_drain = _hand_over_after_each_drain(_live_crew_log)

    def loading_note(wake: Any) -> None:
        if not no_drain.is_set():
            sys.modules[name] = work_relay_feed  # the first import, inside the drain
        real_note(wake)

    _live_crew_log.setattr(work_relay_feed, "note", loading_note)
    _sub_closer(1.5)
    assert emit_runtime.drain_for_shutdown(default_timeout())
    assert _sub_charges(LEAD_UNIT) == [SUB_WORKER]


def test_a_shutdown_drain_that_runs_out_with_relay_work_left_says_so(_live_crew_log, caplog):
    """Never True with work owed: a relay that stays busy ends the rounds with False and
    a WARNING naming what is left."""
    import logging

    from kiro_crew.crew_log import emit_runtime

    _nested_boards()
    caplog.set_level(logging.WARNING, logger=emit_runtime.logger.name)
    with pytest.MonkeyPatch.context() as busy:
        busy.setattr(work_relay_feed, "idle", lambda: False)
        busy.setattr(work_relay_feed, "pending", lambda: 3)
        _sub_closer(1.5)
        assert emit_runtime.drain_for_shutdown(default_timeout()) is False
    assert "3 relay decision(s) pending" in caplog.text


def test_the_writer_shutdown_drain_can_run_again_after_more_is_written(_live_crew_log):
    """A second ``drain_for_shutdown`` on the same writer writes what was queued since."""
    from kiro_crew.crew_log import emit_runtime

    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    writer = emit_runtime._writer()
    _live_crew_log.setattr(writer, "_executor", lambda: _GonePool())
    _emit_closer(1, 1.0)
    assert writer.drain_for_shutdown(default_timeout()).drained
    _emit_closer(2, 2.0)
    assert writer.drain_for_shutdown(default_timeout()).drained
    log = CrewLog.open(lg.KIND_SESSION, WORKER_UNIT)
    assert [e.data["turn"] for e in log.iter_from(1) if e.type == "turn/completed"] == [1, 2]


def test_a_wake_is_decided_inline_only_when_no_consumer_can_start(_live_crew_log):
    """Normal runs decide on the relay thread; when a thread cannot be created (the
    interpreter is exiting), the caller decides the wake itself."""
    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")
    deciders: "list[str]" = []
    real = work_relay.decide

    def noted(*args: Any, **kwargs: Any) -> None:
        deciders.append(threading.current_thread().name)
        real(*args, **kwargs)

    _live_crew_log.setattr(work_relay, "decide", noted)
    _emit_closer(1, 1.0)
    _settle()
    assert deciders == ["crew-log-relay"]
    _live_crew_log.setattr(work_relay_feed, "_thread", None)

    def refused(self: Any) -> None:
        raise RuntimeError("can't create new thread at interpreter shutdown")

    _live_crew_log.setattr(threading.Thread, "start", refused)
    _emit_closer(2, 2.0)
    _settle()
    assert _charges() == [1.0, 2.0]
    assert deciders[1] != "crew-log-relay"


def test_flush_without_a_deadline_waits_for_the_relayed_copy(_live_crew_log):
    """``flush(timeout=None)`` waits with no deadline, as the writer's own flush does."""
    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")
    _emit_closer(1, 1.0)
    assert crew_log_emit.flush(timeout=None)
    assert _charges() == [1.0]


def _mid_import(
    patch: pytest.MonkeyPatch,
    reader: Any = None,
    name: str = "kiro_crew.crew_log.work_relay_feed",
) -> list[str]:
    """List *name* as another thread's import leaves it before its body ran.

    ``import_module`` is what waits for that import; it is stood in for here on
    *reader* (the module that looks *name* up), returning the finished module, so the
    test needs no second thread racing the first import.
    """
    import importlib
    import sys
    import types

    from kiro_crew.crew_log import emit_runtime

    finished = types.SimpleNamespace(generation=lambda: 0, drain=lambda _t: True, idle=lambda: True)
    asked: list[str] = []

    def _import(target: str) -> Any:
        asked.append(target)
        return finished if target == name else importlib.import_module(target)

    patch.setitem(sys.modules, name, types.ModuleType(name))
    stand_in = types.SimpleNamespace(import_module=_import)
    patch.setattr(reader or emit_runtime, "importlib", stand_in, raising=False)
    return asked


def test_a_flush_waits_for_the_relay_module_another_thread_is_importing():
    """A half-imported relay module has no ``generation`` yet; reading it raised."""
    from kiro_crew.crew_log import emit_runtime

    emit_runtime._writer()
    # Undone before the fixture's own teardown flush, which must see the real module.
    with pytest.MonkeyPatch.context() as patch:
        asked = _mid_import(patch)
        flushed = crew_log_emit.flush(timeout=default_timeout())

    assert flushed
    assert "kiro_crew.crew_log.work_relay_feed" in asked


def test_the_shutdown_drain_waits_for_the_relay_module_another_thread_is_importing():
    import time

    from kiro_crew.crew_log import emit_runtime

    writer = emit_runtime._writer()
    with pytest.MonkeyPatch.context() as patch:
        asked = _mid_import(patch)
        drained = emit_runtime._drain_writer_and_relay(writer, time.monotonic(), default_timeout())

    assert drained
    assert "kiro_crew.crew_log.work_relay_feed" in asked


def test_the_eager_barrier_waits_for_the_prime_module_its_worker_is_importing():
    """The same gap on ``slot_prime``, which the eager worker imports lazily."""
    name = "kiro_crew.crew_log.slot_prime"
    with pytest.MonkeyPatch.context() as patch:
        asked = _mid_import(patch, crew_log_eager, name)
        drained = crew_log_eager.drain(timeout=default_timeout())

    assert drained
    assert name in asked
