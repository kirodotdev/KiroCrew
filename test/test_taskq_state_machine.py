"""The taskq state machine: one table, every edge validated, terminals never regress."""

from __future__ import annotations

import pytest

from kiro_crew.taskq import model
from kiro_crew.taskq.model import (
    ACTIVE,
    CANCELLED,
    CLAIMABLE,
    STATES,
    TERMINAL,
    TRANSITIONS,
    InvalidTransition,
    TaskRecord,
    check_transition,
    recovery_backoff_secs,
)


def test_fifteen_states_and_every_state_has_a_row() -> None:
    # 13 (RFC §3.3) + the addendum's ``waiting_dependency`` and ``waiting_input``.
    assert len(STATES) == 15
    assert {model.WAITING_DEPENDENCY, model.WAITING_INPUT} <= STATES
    assert model.SUCCEEDED == model.DONE
    assert set(TRANSITIONS) == STATES
    for state, targets in TRANSITIONS.items():
        assert targets <= STATES, state


def test_terminal_states_have_no_exits_except_unknown_side_effect_resolution() -> None:
    for state in TERMINAL - {model.UNKNOWN_SIDE_EFFECT}:
        assert TRANSITIONS[state] == frozenset(), state
    assert TRANSITIONS[model.UNKNOWN_SIDE_EFFECT] == {model.DONE, model.FAILED}


def test_cancelled_beats_every_non_terminal_state() -> None:
    for state in STATES - TERMINAL:
        check_transition(state, CANCELLED)


def test_failed_reachable_from_every_non_terminal_state() -> None:
    for state in STATES - TERMINAL:
        check_transition(state, model.FAILED)


def test_done_and_unknown_side_effect_reachable_only_from_owned_states() -> None:
    for state in ACTIVE:
        check_transition(state, model.DONE)
        check_transition(state, model.UNKNOWN_SIDE_EFFECT)
    for state in (model.QUEUED, model.RETRY_WAIT, model.WAITING_INFRA):
        with pytest.raises(InvalidTransition):
            check_transition(state, model.DONE)


@pytest.mark.parametrize(
    "old,new",
    [
        (model.DONE, model.RUNNING),
        (model.CANCELLED, model.QUEUED),
        (model.FAILED, model.DONE),
        (model.QUEUED, model.RUNNING),  # must go through admitted/starting
        (model.QUEUED, model.DONE),
        (model.RETRY_WAIT, model.RUNNING),
        (model.RETRY_WAIT, model.DONE),
        (model.WAITING_INFRA, model.DONE),
    ],
)
def test_forbidden_edges_raise(old: str, new: str) -> None:
    with pytest.raises(InvalidTransition):
        check_transition(old, new)


def test_unknown_state_names_raise() -> None:
    with pytest.raises(InvalidTransition):
        check_transition("queued", "sleeping")
    with pytest.raises(InvalidTransition):
        check_transition("bogus", "queued")


def test_happy_path_chain_is_allowed() -> None:
    chain = [model.QUEUED, model.ADMITTED, model.STARTING, model.RUNNING, model.DONE]
    for old, new in zip(chain, chain[1:]):
        check_transition(old, new)


def test_recovery_chain_is_allowed() -> None:
    check_transition(model.RUNNING, model.RECOVERING)
    check_transition(model.RECOVERING, model.ADMITTED)
    check_transition(model.STARTING, model.RETRY_WAIT)
    check_transition(model.RETRY_WAIT, model.ADMITTED)
    check_transition(model.ADMITTED, model.WAITING_INFRA)
    check_transition(model.WAITING_INFRA, model.RETRY_WAIT)


def test_steps_to_replays_a_missed_step_without_widening_the_table() -> None:
    """``admitted -> running`` is the case: a posted ``starting`` write a locked
    database refused must not strand a LIVE row in the one state reconcile
    requeues blind, and the replay uses the table's own steps."""
    assert model.steps_to(model.ADMITTED, model.STARTING) == (model.STARTING,)
    assert model.steps_to(model.ADMITTED, model.RUNNING) == (model.STARTING, model.RUNNING)
    assert model.steps_to(model.ADMITTED, model.RECOVERING) == (model.STARTING, model.RECOVERING)
    for old, new in ((model.ADMITTED, model.RUNNING), (model.ADMITTED, model.RECOVERING)):
        with pytest.raises(InvalidTransition):
            check_transition(old, new)  # no direct edge: the replay is why
    for old in STATES:
        for new in STATES:
            steps = model.steps_to(old, new)
            assert len(steps) <= 2, (old, new, steps)
            for step_from, step_to in zip((old, *steps), steps):
                check_transition(step_from, step_to)  # every step is a legal edge
            # An intermediate is never an outcome, and no path invents a terminal.
            assert not (set(steps[:-1]) & TERMINAL), (old, new, steps)


def test_steps_to_refuses_to_walk_a_live_row_backwards_into_a_claim() -> None:
    """Direction is the HELPER's, not a docstring's request of its callers.

    ``len(steps) <= 2`` plus edge legality blesses ``running -> retry_wait ->
    queued`` and ``running -> recovering -> admitted``: two legal steps that end
    with a LIVE row in a state a dispatcher takes -- the re-dispatch-after-work
    class the only caller of this helper exists to close. So the helper inserts
    exactly ONE intermediate, ``starting``, and cannot express the rest.
    """
    assert model.steps_to(model.RUNNING, model.QUEUED) == ()
    assert model.steps_to(model.RUNNING, model.ADMITTED) == ()
    assert model.steps_to(model.WAITING_INPUT, model.QUEUED) == ()
    assert model.steps_to(model.STARTING, model.QUEUED) == ()
    # Each of those pairs IS joined by two legal edges: the refusal is this
    # helper's, so a widened table cannot quietly re-open them.
    for old, mid, new in (
        (model.RUNNING, model.RETRY_WAIT, model.QUEUED),
        (model.RUNNING, model.RECOVERING, model.ADMITTED),
    ):
        check_transition(old, mid)
        check_transition(mid, new)
    # Every surviving two-step path starts at ``admitted``: a ``starting`` write
    # lost between the claim and the first mark is the one missed step a live
    # row can carry.
    two_step = {
        (old, new) for old in STATES for new in STATES if len(model.steps_to(old, new)) == 2
    }
    assert two_step == {
        (model.ADMITTED, model.RUNNING),
        (model.ADMITTED, model.RECOVERING),
        (model.ADMITTED, model.RETRY_WAIT),
    }
    for _, new in two_step:
        assert model.steps_to(model.ADMITTED, new)[0] == model.STARTING


def test_the_recovering_mark_needs_a_claimable_target_from_a_non_claimable_row() -> None:
    """Why the refusal above is spelled as "one intermediate" and NOT as
    "refuse a CLAIMABLE target from a non-CLAIMABLE ``old``".

    ``taskq_mark(info, "recovering")`` fires on a ``session/new`` timeout, which
    happens BEFORE the run's first stream event -- exactly when the row may still
    be ``admitted`` because its ``starting`` write was lost. ``recovering`` is in
    CLAIMABLE and ``admitted`` is not, so that rule would refuse the very mark
    this replay exists for and re-open the class from the other side.
    """
    assert model.RECOVERING in CLAIMABLE and model.ADMITTED not in CLAIMABLE
    assert model.steps_to(model.ADMITTED, model.RECOVERING) == (model.STARTING, model.RECOVERING)


def test_steps_to_has_no_identity_and_no_unknown_names() -> None:
    for state in STATES:
        assert model.steps_to(state, state) == ()
    assert model.steps_to(model.QUEUED, "sleeping") == ()
    assert model.steps_to("bogus", model.QUEUED) == ()
    # A terminal row never moves except through the one documented resolution.
    assert model.steps_to(CANCELLED, model.QUEUED) == ()
    assert model.steps_to(model.UNKNOWN_SIDE_EFFECT, model.DONE) == (model.DONE,)


def test_claimable_and_active_partition_the_live_states() -> None:
    assert CLAIMABLE & ACTIVE == {model.RECOVERING}
    assert CLAIMABLE | ACTIVE | TERMINAL | {model.WAITING_INFRA} == STATES
    # recovering is both re-claimable (dead owner) and owned (live owner);
    # the lease decides which, so it appears in both sets.
    assert model.RECOVERING in CLAIMABLE and model.RECOVERING in ACTIVE


def test_record_validates_kind_state_and_side_effect_class() -> None:
    with pytest.raises(ValueError):
        TaskRecord(id="", kind="subagent")
    with pytest.raises(ValueError):
        TaskRecord(id="x", kind="nope")
    with pytest.raises(ValueError):
        TaskRecord(id="x", kind="subagent", state="sleeping")
    with pytest.raises(ValueError):
        TaskRecord(id="x", kind="subagent", side_effect_class="maybe")
    rec = TaskRecord(id="x", kind="subagent", parent_id="p")
    assert rec.root_id == "p"
    assert TaskRecord(id="y", kind="subagent").root_id == "y"


def test_record_row_round_trip_preserves_every_column() -> None:
    rec = TaskRecord(
        id="r1",
        kind="subagent",
        session_key="dash:1",
        parent_id="p1",
        params={"task": "do", "batch_id": "b"},
        scope_ref={"memory_store": "crew-a"},
        state=model.RETRY_WAIT,
        attempts=2,
        next_run_at=123.5,
        lease_owner="inc",
        lease_expires_at=200.0,
        generation=3,
        progress={"step": 4},
        result_ref="/tmp/r",
        deadline_at=999.0,
        idempotency_key="k",
        side_effect_class=model.SIDE_EFFECT_NONE,
        created_at=1.0,
        updated_at=2.0,
    )
    row = dict(zip(TaskRecord.COLUMNS, rec.to_row()))
    back = TaskRecord.from_row(row)
    assert back == rec
    assert "params" not in rec.public() and rec.public()["terminal"] is False


def test_recovery_backoff_is_exponential_and_capped() -> None:
    assert recovery_backoff_secs(0) == 2.0
    assert recovery_backoff_secs(1) == 4.0
    assert recovery_backoff_secs(3) == 16.0
    assert recovery_backoff_secs(10) == 120.0
    assert recovery_backoff_secs(-5) == 2.0


def test_a_wait_is_never_reachable_from_starting() -> None:
    """The two closed edges the "posted mark, inline wait" defect depended on.

    ``WAITING`` means "a live runtime is parked", which a ``starting`` row has
    not established yet, so the repair for a wait that arrives too early is to
    order the writes -- never to open ``starting -> waiting_*``. And a row the
    coordinator PARKED because its wait was refused must not look completable:
    ``retry_wait -> done`` staying closed is what makes a refused wait a loud
    failure rather than a row that quietly re-runs finished work.
    """
    assert model.WAITING & TRANSITIONS[model.STARTING] == frozenset()
    assert model.WAITING <= TRANSITIONS[model.RUNNING]
    assert model.DONE not in TRANSITIONS[model.RETRY_WAIT]
    assert model.RUNNING not in TRANSITIONS[model.RETRY_WAIT]
