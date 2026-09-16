"""Exactly-once boundaries: atomic claim, lease, generation fencing, cancel races."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from overload_fakes import Clock, open_task_store
from overload_fakes import task_record as _rec

from kiro_crew.taskq import model
from kiro_crew.taskq.store import DEFAULT_DISPATCH_WINDOW, TaskStore


@pytest.fixture
def clock() -> Clock:
    return Clock(5000.0)


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> TaskStore:
    yield from open_task_store(
        tmp_path, clock, name="t.db", window=DEFAULT_DISPATCH_WINDOW, lease_secs=60.0
    )


# ── claim ─────────────────────────────────────────────────────────────────────


def test_claim_is_atomic_exactly_one_winner(store: TaskStore, clock: Clock) -> None:
    store.accept([_rec("c")])
    first = store.claim("c", owner="w1")
    second = store.claim("c", owner="w2")
    assert first is not None and second is None
    rec = store.get("c")
    assert rec is not None
    assert rec.state == model.ADMITTED
    assert rec.lease_owner == "w1"
    assert rec.lease_expires_at == clock.t + 60.0
    assert rec.generation == 1 and rec.attempts == 1


def test_claim_race_between_two_workers_from_threads(tmp_path: Path) -> None:
    """Two real threads on separate connections: exactly one wins each row."""
    path = tmp_path / "race.db"
    seed = TaskStore(path, network_fs=False).open()
    seed.accept([_rec(f"r{i}") for i in range(40)])
    seed.close()
    wins: dict[str, list[str]] = {"w1": [], "w2": []}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        s = TaskStore(path, network_fs=False, busy_timeout_secs=5.0).open()
        try:
            barrier.wait()
            for i in range(40):
                got = s.claim(f"r{i}", owner=name)
                if got is not None:
                    wins[name].append(got.record.id)
        finally:
            s.close()

    t1, t2 = threading.Thread(target=worker, args=("w1",)), threading.Thread(
        target=worker, args=("w2",)
    )
    t1.start()
    t2.start()
    t1.join(30)
    t2.join(30)
    assert not t1.is_alive() and not t2.is_alive()
    assert sorted(wins["w1"] + wins["w2"], key=lambda x: int(x[1:])) == [f"r{i}" for i in range(40)]
    assert not (set(wins["w1"]) & set(wins["w2"]))


def test_claim_next_is_fifo_and_skips_excluded(store: TaskStore, clock: Clock) -> None:
    for i in range(3):
        clock.t += 1
        store.accept([_rec(f"n{i}")])
    got = store.claim_next(model.KIND_SUBAGENT, exclude_ids=["n0"])
    assert got is not None and got.record.id == "n1"
    assert store.claim_next(model.KIND_SUBAGENT, exclude_ids=["n0"]).record.id == "n2"
    assert store.claim_next(model.KIND_SUBAGENT, exclude_ids=["n0"]) is None


def test_claim_respects_next_run_at_and_lease(store: TaskStore, clock: Clock) -> None:
    store.accept([_rec("d")])
    store.defer("d", clock.t + 10, reason="pressure")
    assert store.claim("d") is None
    clock.t += 10
    assert store.claim("d") is not None
    # a claimed (admitted) row is never re-claimed, lease or no lease
    clock.t += 1000
    assert store.claim("d") is None


def test_recovering_row_with_expired_lease_is_reclaimable(store: TaskStore, clock: Clock) -> None:
    store.accept([_rec("rec")])
    g = store.claim("rec").generation
    assert store.transition("rec", model.STARTING, generation=g)
    assert store.transition("rec", model.RUNNING, generation=g)
    assert store.transition("rec", model.RECOVERING, generation=g, next_run_at=clock.t + 5)
    # transition to a claimable state clears the lease; not yet eligible
    assert store.claim("rec") is None
    clock.t += 5
    again = store.claim("rec")
    assert again is not None and again.generation == g + 1


def test_renew_lease_only_for_current_generation(store: TaskStore, clock: Clock) -> None:
    store.accept([_rec("l")])
    g = store.claim("l").generation
    clock.t += 20
    assert store.renew_lease("l", g) is True
    assert store.get("l").lease_expires_at == clock.t + 60
    assert store.renew_lease("l", g - 1) is False
    assert store.release_lease("l") is True
    assert store.get("l").lease_owner is None


# ── generation fencing ────────────────────────────────────────────────────────


def test_stale_generation_result_is_rejected_and_logged(store: TaskStore, clock: Clock) -> None:
    store.accept([_rec("g")])
    old = store.claim("g").generation
    assert store.transition("g", model.STARTING, generation=old)
    # the runtime is lost; the row is recovered and re-dispatched
    assert store.transition("g", model.RECOVERING, generation=old, next_run_at=None)
    new = store.claim("g").generation
    assert new == old + 1
    assert store.transition("g", model.STARTING, generation=new)
    assert store.transition("g", model.RUNNING, generation=new)
    # the OLD worker wakes up and reports: fenced out, nothing changes
    assert store.finish("g", model.FAILED, generation=old, error="late") is False
    assert store.state_of("g") == model.RUNNING
    kinds = [e.kind for e in store.events("g")]
    assert "stale_result" in kinds
    stale = [e for e in store.events("g") if e.kind == "stale_result"][-1]
    assert stale.data == {"from_generation": old, "current": new, "wanted": model.FAILED}
    # the current worker's result lands
    assert store.finish("g", model.DONE, generation=new, result_ref="/r") is True
    assert store.get("g").result_ref == "/r"


def test_terminal_never_regresses(store: TaskStore) -> None:
    store.accept([_rec("t")])
    g = store.claim("t").generation
    store.transition("t", model.STARTING, generation=g)
    store.transition("t", model.RUNNING, generation=g)
    assert store.finish("t", model.DONE, generation=g) is True
    assert store.finish("t", model.FAILED, generation=g) is False
    assert store.transition("t", model.RUNNING, generation=g) is False
    assert store.cancel("t") is None
    assert store.state_of("t") == model.DONE
    assert [e.kind for e in store.events("t")].count("rejected_transition") == 2


def test_duplicate_completion_is_a_noop(store: TaskStore) -> None:
    store.accept([_rec("dup")])
    g = store.claim("dup").generation
    store.transition("dup", model.STARTING, generation=g)
    store.transition("dup", model.RUNNING, generation=g)
    assert store.finish("dup", model.DONE, generation=g) is True
    assert store.finish("dup", model.DONE, generation=g) is False


def test_finish_requires_terminal_state(store: TaskStore) -> None:
    store.accept([_rec("f")])
    with pytest.raises(model.InvalidTransition):
        store.finish("f", model.RUNNING)


def test_transition_on_unknown_row_is_false(store: TaskStore) -> None:
    assert store.transition("ghost", model.ADMITTED) is False
    assert store.cancel("ghost") is None


# ── cancel races ──────────────────────────────────────────────────────────────


def test_cancel_before_claim_wins(store: TaskStore) -> None:
    store.accept([_rec("q")])
    assert store.cancel("q", reason="user_stop") == model.QUEUED
    assert store.claim("q") is None
    assert store.state_of("q") == model.CANCELLED


def test_cancel_after_claim_fences_the_dispatcher(store: TaskStore) -> None:
    """Claim landed first; the cancel still wins because it bumps the generation
    and the dispatcher's next fenced write fails, so it must not start the run."""
    store.accept([_rec("q2")])
    g = store.claim("q2").generation
    assert store.cancel("q2", reason="user_stop") == model.ADMITTED
    assert store.transition("q2", model.STARTING, generation=g) is False
    assert store.state_of("q2") == model.CANCELLED
    assert store.get("q2").generation == g + 1


def test_cancel_during_run_beats_late_completion(store: TaskStore) -> None:
    store.accept([_rec("r")])
    g = store.claim("r").generation
    store.transition("r", model.STARTING, generation=g)
    store.transition("r", model.RUNNING, generation=g)
    assert store.cancel("r") == model.RUNNING
    # the run finishes anyway and reports done with its old generation
    assert store.finish("r", model.DONE, generation=g) is False
    assert store.state_of("r") == model.CANCELLED


def test_cancel_clears_lease_and_bumps_generation(store: TaskStore) -> None:
    store.accept([_rec("c")])
    store.claim("c")
    store.cancel("c")
    rec = store.get("c")
    assert rec.lease_owner is None and rec.lease_expires_at is None
    assert rec.generation == 2
