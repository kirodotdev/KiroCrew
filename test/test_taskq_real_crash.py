"""Task store restart after a real kill of its owner, and a claim race across processes.

Each restart test ends a real child process with ``SIGKILL`` while it owns a
row, reopens the file as a new incarnation, and runs the production reconcile.
Nothing here writes the damaged state by hand. The child and the parent share
one virtual clock base, so the lease and backoff arithmetic is exact and no
test reads wall time.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from overload_fakes import Clock
from taskq_real_crash import harness
from taskq_real_crash.reopen import BASE_CLOCK, record, reopen_store

from kiro_crew.taskq import model
from kiro_crew.taskq.reconcile import reconcile_on_boot

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="win32: SIGKILL and flock semantics differ"
)

#: Past every lease and recovery backoff a child can leave behind.
AFTER_KILL = 500.0
#: Between two child runs, so the next child's clock is past the previous backoff.
CYCLE_STEP = 2000.0


def _env(**extra: str) -> dict[str, str]:
    env = {"CRASH_CLOCK": str(BASE_CLOCK)}
    env.update(extra)
    return env


def _artifact_probe(directory: Path):
    """The probe a real caller supplies: a result file on disk says the run finished."""

    def probe(rec: model.TaskRecord) -> str | None:
        return model.DONE if (directory / "result.json").exists() else None

    return probe


# -- the harness reports a child that never reached its failpoint ----------------------------


@pytest.mark.parametrize(
    ("store_name", "failpoint", "complaint"),
    [
        pytest.param(
            "taskq",
            "no-such-failpoint",
            "unknown taskq failpoint 'no-such-failpoint'",
            id="unknown-failpoint",
        ),
        pytest.param(
            "no-such-store", "claimed", "unknown store 'no-such-store'", id="unknown-store"
        ),
    ],
)
def test_a_child_that_exits_before_its_failpoint_fails_the_test_with_its_output(
    tmp_path: Path, store_name: str, failpoint: str, complaint: str
) -> None:
    """An early exit is never read as a kill: the harness fails and quotes the child."""
    with pytest.raises(AssertionError, match="before failpoint") as caught:
        harness.run_to_failpoint_and_kill(tmp_path, store_name, failpoint, env=_env())
    assert complaint in str(caught.value)


# -- restart, each with a REAL kill ---------------------------------------------------------


@pytest.mark.parametrize("side_effect_class", [model.SIDE_EFFECT_NONE, model.SIDE_EFFECT_UNKNOWN])
def test_kill_between_claim_and_start_requeues(tmp_path: Path, side_effect_class: str) -> None:
    """A row claimed and never started has no runtime and no side effect: back to the queue."""
    harness.run_to_failpoint_and_kill(
        tmp_path, "taskq", "claimed", env=_env(CRASH_CLASS=side_effect_class)
    )
    clock = Clock(BASE_CLOCK + AFTER_KILL)
    store = reopen_store(tmp_path, clock.t, clock=clock)
    try:
        assert store.state_of("t1") == model.ADMITTED, "the kill should leave the claim in place"
        report = reconcile_on_boot(store)
        assert (report.examined, report.requeued, report.errors) == (1, 1, [])
        assert store.state_of("t1") == model.QUEUED
        assert store.get("t1").lease_owner is None
        clock.advance(CYCLE_STEP)
        assert store.claim("t1") is not None, "a requeued row must be claimable again"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("side_effect_class", "settled"),
    [
        (model.SIDE_EFFECT_UNKNOWN, model.UNKNOWN_SIDE_EFFECT),
        # the healthy side: a replay-safe class is recovered, never parked
        (model.SIDE_EFFECT_NONE, model.RECOVERING),
        (model.SIDE_EFFECT_IDEMPOTENT_KEY, model.RECOVERING),
    ],
)
def test_kill_mid_run_unknown_class_lands_unknown_side_effect(
    tmp_path: Path, side_effect_class: str, settled: str
) -> None:
    harness.run_to_failpoint_and_kill(
        tmp_path, "taskq", "running", env=_env(CRASH_CLASS=side_effect_class)
    )
    clock = Clock(BASE_CLOCK + AFTER_KILL)
    store = reopen_store(tmp_path, clock.t, clock=clock)
    try:
        before = store.get("t1")
        assert before is not None and before.state == model.RUNNING
        assert before.lease_owner is not None, "the dead owner should still hold the lease"
        reconcile_on_boot(store)
        assert store.state_of("t1") == settled
        last = store.events("t1")[-1]
        assert last.data["reconciled"] == "lost_owner"
        if settled == model.UNKNOWN_SIDE_EFFECT:
            clock.advance(10 * CYCLE_STEP)
            assert store.claim("t1") is None, "a parked row must never be re-run"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("failpoint", "settled"),
    [
        ("result-written", model.DONE),
        # negative control: the same kill with no result on disk must NOT settle done
        ("running", model.UNKNOWN_SIDE_EFFECT),
    ],
)
def test_kill_after_result_written_before_ack_settles_done(
    tmp_path: Path, failpoint: str, settled: str
) -> None:
    harness.run_to_failpoint_and_kill(tmp_path, "taskq", failpoint, env=_env())
    store = reopen_store(tmp_path, BASE_CLOCK + AFTER_KILL)
    try:
        assert store.state_of("t1") == model.RUNNING, "the done write never landed"
        reconcile_on_boot(store, artifact_probe=_artifact_probe(tmp_path))
        assert store.state_of("t1") == settled
    finally:
        store.close()


def test_third_lost_owner_ends_failed(tmp_path: Path) -> None:
    """Each real kill of the row's owner spends one recovery; the next one ends it failed."""
    limit = model.RECOVERY_MAX_ATTEMPTS
    for cycle in range(limit + 1):
        now = BASE_CLOCK + CYCLE_STEP * cycle
        harness.run_to_failpoint_and_kill(
            tmp_path,
            "taskq",
            "running",
            env=_env(CRASH_CLASS=model.SIDE_EFFECT_NONE, CRASH_CLOCK=str(now)),
            tag=f"cycle{cycle}",
        )
        store = reopen_store(tmp_path, now + AFTER_KILL)
        try:
            reconcile_on_boot(store)
        finally:
            store.close()
    store = reopen_store(tmp_path, BASE_CLOCK + CYCLE_STEP * (limit + 2))
    try:
        rec = store.get("t1")
        assert rec is not None and rec.state == model.FAILED
        last = store.events("t1")[-1]
        assert last.data["reconciled"] == "lost_owner_limit"
        assert last.data["lost_owner_recoveries"] == limit
        assert reconcile_on_boot(store).examined == 0, "a settled row stays settled"
    finally:
        store.close()


# -- claims -------------------------------------------------------------------------------


def test_claim_has_one_winner_across_processes(tmp_path: Path) -> None:
    rows, racers = 12, ["a", "b", "c"]
    seed = reopen_store(tmp_path, BASE_CLOCK)
    seed.accept([record(f"r{i}") for i in range(rows)])
    seed.close()
    harness.race(tmp_path, "taskq", "claim-race", racers, env=_env(CRASH_IDS=str(rows)))
    wins = {n: json.loads((tmp_path / f"won-{n}.json").read_text()) for n in racers}
    flat = [tid for ids in wins.values() for tid in ids]
    assert sorted(flat) == sorted(f"r{i}" for i in range(rows)), "every row claimed exactly once"
    assert len(flat) == len(set(flat))
    store = reopen_store(tmp_path, BASE_CLOCK)
    try:
        for who, ids in wins.items():
            for tid in ids:
                rec = store.get(tid)
                assert (rec.state, rec.generation, rec.attempts) == (model.ADMITTED, 1, 1)
                assert rec.lease_owner == who
    finally:
        store.close()


def test_running_row_with_expired_lease_is_not_reclaimable(tmp_path: Path) -> None:
    """Lease expiry gates ``claim`` only: a dead owner's running row waits for reconcile."""
    harness.run_to_failpoint_and_kill(
        tmp_path, "taskq", "running", env=_env(CRASH_CLASS=model.SIDE_EFFECT_NONE)
    )
    store = reopen_store(tmp_path, BASE_CLOCK + 100 * AFTER_KILL)
    try:
        rec = store.get("t1")
        assert rec is not None and rec.state == model.RUNNING
        assert rec.lease_expires_at is not None and rec.lease_expires_at < store.now()
        assert store.claim("t1") is None, "an expired lease must not let a running row be stolen"
        assert store.get("t1").state == model.RUNNING
        reconcile_on_boot(store)  # the control: reconcile is what reclaims it
        assert store.state_of("t1") == model.RECOVERING
    finally:
        store.close()


# -- cancellation -------------------------------------------------------------------------


def test_cancelled_row_never_revives_after_restart(tmp_path: Path) -> None:
    """A cancel after a real kill stays a cancel through a restart, whatever the probe says."""
    harness.run_to_failpoint_and_kill(tmp_path, "taskq", "running", env=_env())
    store = reopen_store(tmp_path, BASE_CLOCK + AFTER_KILL)
    try:
        assert store.cancel("t1", reason="user_stop") == model.RUNNING
        store.close()
        clock = Clock(BASE_CLOCK + 2 * AFTER_KILL)
        store = reopen_store(tmp_path, clock.t, clock=clock)
        reconcile_on_boot(store, artifact_probe=lambda rec: model.DONE)
        assert store.state_of("t1") == model.CANCELLED
        clock.advance(10 * CYCLE_STEP)
        assert store.claim("t1") is None
    finally:
        store.close()
