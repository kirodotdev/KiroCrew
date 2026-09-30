"""A loop whose own delivered cycles keep FAILING stands down.

The prompt loop stood down on three narrow signals -- a structurally rejected
payload, an unanswered tool approval, a cycle that never got a model session --
but not on the generic case: a cycle that reached a session and dispatched, then
DIED (a backend error after retries were spent, a persistent tool error, a
prompt timeout). Such a loop fired every interval, spent a turn, produced
nothing, and stopped only when ``max_cycles`` happened to run out. These tests
pin the streak, the terminal stand-down, and the clearing on a landed turn --
all driven by recorded evidence, so a loop that recovers is never held back.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import (
    CONSECUTIVE_FAILURE_REASON,
    AutoNudgeService,
    NudgeLoop,
)
from kiro_crew.monitoring.models import MonitorState

SLOT = "chat-1-654"


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(autouse=True)
def _no_published_service_outlives_the_test():
    """Unpublish the singleton and bound leftover work after every test.

    ``start()`` publishes the service as the module singleton and only ``stop()``
    clears it, so a service left published hands a later test one bound to a store
    it is finished with. Sync on purpose: this suite's pytest-asyncio pin errors
    on async-generator fixtures at setup.
    """
    yield
    svc = _an.get_instance()
    if svc is None:
        return
    try:
        inflight = getattr(svc, "_inflight_adds", None)
        if inflight is not None:
            for task in list(inflight):
                task.cancel()
            inflight.clear()
        svc.stop()
    finally:
        _an._INSTANCE = None


@pytest.fixture
def store_dir(tmp_path_factory):
    """A store directory owned by the SESSION, not by one test.

    ``_persist_locked`` hands ``_write_state`` to a thread and a thread cannot be
    cancelled, so a write that lands late against a per-test ``tmp_path``
    re-creates a directory pytest already removed.
    """
    return tmp_path_factory.mktemp("autonudge-cycle-failures")


@pytest.fixture
def svc(store_dir):
    return AutoNudgeService(base_dir=store_dir)


@pytest.fixture
def _nosleep(monkeypatch):
    """Collapse the timer's idle wait so ``_timer`` runs synchronously."""

    async def _noop(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _noop)


async def _armed(svc, **kwargs) -> NudgeLoop:
    await svc.start()
    loop = await svc.add(slot_key=SLOT, message="go", idle_secs=600, **kwargs)
    await svc._timers[loop.id]
    return loop


async def _stop_and_drain(svc: AutoNudgeService) -> None:
    timers = list(svc._timers.values())
    svc.stop()
    if timers:
        await asyncio.gather(*timers, return_exceptions=True)
    inflight = list(svc._inflight_adds)
    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)


async def _fail(svc: AutoNudgeService, loop: NudgeLoop) -> None:
    """Charge one failed cycle at the loop's CURRENT id and generation.

    The ordinary case: a cycle that fired under the live loop and generation and
    died. The stale cases (advanced generation, replaced loop) have their own
    tests and pass a mismatched value explicitly.
    """
    await svc.notify_cycle_failed(
        loop.slot_key,
        loop_id=loop.id,
        expected_generation=svc._loops[loop.id].config_generation,
    )


@pytest.mark.asyncio
async def test_the_hook_records_a_streak_without_stopping_the_loop(svc, _nosleep):
    loop = await _armed(svc)

    await _fail(svc, loop)
    await _fail(svc, loop)

    assert svc._loops[loop.id].consecutive_failed_cycles == 2
    assert svc._loops[loop.id].active is True
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_landed_turn_clears_the_streak(svc, _nosleep):
    """A turn that completed proves the session can make progress.

    Any landed turn counts, a human's as much as a cycle's: the streak only ever
    stops a loop, so clearing it on broader evidence can only keep a working loop
    running.
    """
    loop = await _armed(svc)
    await _fail(svc, loop)
    await _fail(svc, loop)

    svc.notify_cycle_landed(SLOT)

    assert svc._loops[loop.id].consecutive_failed_cycles == 0
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_short_streak_still_fires(svc, _nosleep):
    """Below the stand-down threshold nothing changes: a couple of errors are
    weather, and the loop keeps firing."""
    fired: list[NudgeLoop] = []

    async def on_fire(loop, *_args, **_kwargs):
        fired.append(loop)
        return True

    loop = await _armed(svc)
    svc._on_fire = on_fire
    svc._loops[loop.id].consecutive_failed_cycles = 4

    svc._cancel_timer(loop.id)
    await svc._timer(svc._loops[loop.id])

    assert len(fired) == 1
    assert svc._loops[loop.id].active is True
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_five_failures_stand_the_loop_down(svc, _nosleep):
    """Terminal, in the same shape as the other bounds: deactivate + ``expired``
    and NO fire."""
    fired: list[NudgeLoop] = []

    async def on_fire(loop, *_args, **_kwargs):
        fired.append(loop)
        return True

    events: list[tuple[str, str]] = []
    svc.subscribe(lambda ev, lp: events.append((ev, lp.id if lp else "")))
    loop = await _armed(svc)
    svc._on_fire = on_fire
    svc._loops[loop.id].consecutive_failed_cycles = 5

    svc._cancel_timer(loop.id)
    await svc._timer(svc._loops[loop.id])

    refreshed = svc._loops[loop.id]
    assert refreshed.active is False
    assert refreshed.stopped_reason == CONSECUTIVE_FAILURE_REASON
    assert ("expired", loop.id) in events, f"the stop must be user-visible; got {events}"
    assert fired == []
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_cycle_cap_still_wins(svc, _nosleep):
    """The failure bound must not relabel an existing terminal outcome."""
    loop = await _armed(svc, max_cycles=1)
    svc._loops[loop.id].cycle_count = 1
    svc._loops[loop.id].consecutive_failed_cycles = 9

    svc._cancel_timer(loop.id)
    await svc._timer(svc._loops[loop.id])

    assert svc._loops[loop.id].stopped_reason == "cycle_cap"
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_stand_down_is_re_armable(svc, _nosleep):
    """The remedy is external (the backend or tool recovering), so a directive may
    revive the loop -- and the revival starts a fresh run, so the old streak must
    not survive it."""
    loop = await _armed(svc)
    svc._loops[loop.id].consecutive_failed_cycles = 5
    svc._cancel_timer(loop.id)
    await svc._timer(svc._loops[loop.id])
    assert svc._loops[loop.id].active is False

    revived = await svc.update(loop.id, active=True)

    assert revived is not None and revived.active is True
    assert svc._loops[loop.id].consecutive_failed_cycles == 0
    assert svc._loops[loop.id].stopped_reason == ""
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_manual_pause_is_not_relabelled_by_the_bound(svc, _nosleep):
    """A user pause that lands first must not be overwritten by the failure bound
    firing on an in-flight cycle -- it is in ``_TERMINAL_BOUND_REASONS`` for
    exactly that no-op protection."""
    loop = await _armed(svc)
    svc._loops[loop.id].consecutive_failed_cycles = 5
    await svc.update(loop.id, active=False)  # manual pause, records "manual"

    svc._cancel_timer(loop.id)
    await svc._timer(svc._loops[loop.id])

    assert svc._loops[loop.id].stopped_reason == "manual"
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_hook_ignores_an_inactive_loop(svc, _nosleep):
    """A paused loop is not accruing evidence; recording would stale-stop it."""
    loop = await _armed(svc)
    await svc.update(loop.id, active=False)

    await _fail(svc, loop)

    assert svc._loops[loop.id].consecutive_failed_cycles == 0
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_hook_is_silent_for_an_unknown_slot(svc, _nosleep):
    await _armed(svc)

    # Unknown loop id -> no match, must not raise.
    await svc.notify_cycle_failed("chat-9-nobody", loop_id="nope", expected_generation=0)

    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_matching_generation_charges_the_streak(svc, _nosleep):
    """The charge lands when the fired loop id AND generation still match.

    A fault whose fire-time id and generation equal the live loop's describes the
    current instruction on the current loop, so it counts -- the ordinary case,
    where nothing was revised or replaced between the cycle firing and dying.
    """
    loop = await _armed(svc)
    gen = svc._loops[loop.id].config_generation

    await svc.notify_cycle_failed(SLOT, loop_id=loop.id, expected_generation=gen)

    assert svc._loops[loop.id].consecutive_failed_cycles == 1
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_stale_generation_does_not_charge_the_revised_loop(svc, _nosleep):
    """A fault from a now-superseded generation must not stand the loop down.

    If the loop was revised (A->B->A) between a cycle firing and dying, the fault
    describes the OLD instruction. Charging it would let a stale completion spend
    the revised loop's stand-down budget -- the exact scope the fired generation
    is passed to prevent. The hook is scoped to ``(id, generation)`` just as the
    structural-terminal verdict is, so a stale generation is ignored.
    """
    loop = await _armed(svc)
    stale_gen = svc._loops[loop.id].config_generation
    # Advance the generation the way a revival does, simulating a revise under
    # the in-flight cycle.
    svc._loops[loop.id].config_generation = stale_gen + 1

    await svc.notify_cycle_failed(SLOT, loop_id=loop.id, expected_generation=stale_gen)

    assert svc._loops[loop.id].consecutive_failed_cycles == 0
    assert svc._loops[loop.id].active is True
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_charge_for_a_different_loop_id_does_not_touch_the_live_loop(svc, _nosleep):
    """F1: the charge is scoped by loop id, not slot+generation alone.

    A completion carrying a loop id other than the one live on the slot is a
    stale fault of a predecessor that occupied this slot (same slot, and both
    start at generation 0, so a generation-only match would charge the current
    loop). Requiring the id to match too drops it: the live loop's streak stays
    clean, so a replaced-on-the-same-slot loop never inherits its predecessor's
    failure.
    """
    loop = await _armed(svc)
    live_gen = svc._loops[loop.id].config_generation

    # Same slot, same generation, but the id names a DIFFERENT (gone) loop.
    await svc.notify_cycle_failed(
        SLOT, loop_id="a-predecessor-loop-id", expected_generation=live_gen
    )

    assert svc._loops[loop.id].consecutive_failed_cycles == 0
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_streak_survives_a_restart(svc, store_dir, _nosleep):
    """The cause that fails a cycle (a wedged backend, a broken tool) routinely
    outlives a restart, so a reload that dropped the streak would restart the
    doomed cycles."""
    loop = await _armed(svc)
    await _fail(svc, loop)
    await _fail(svc, loop)
    await svc._persist_locked()
    await _stop_and_drain(svc)
    _an._INSTANCE = None

    reloaded = AutoNudgeService(base_dir=store_dir)
    await reloaded.start()
    try:
        assert reloaded._loops[loop.id].consecutive_failed_cycles == 2
    finally:
        await _stop_and_drain(reloaded)


@pytest.mark.parametrize("stored", ["3", None, -5, 2.9, float("nan"), 10**400])
@pytest.mark.asyncio
async def test_a_malformed_persisted_streak_is_normalised_at_load(svc, store_dir, stored, _nosleep):
    """The store is agent-writable and the streak is compared with ``>=`` on every
    wake, so a string or ``null`` would raise TypeError inside ``_timer`` and the
    automation would silently never fire again -- surviving every reload."""
    loop = await _armed(svc)
    await svc._persist_locked()
    await _stop_and_drain(svc)
    _an._INSTANCE = None

    raw = json.loads((store_dir / "autonudge.json").read_text(encoding="utf-8"))
    for row in raw["loops"]:
        row["consecutive_failed_cycles"] = stored
    (store_dir / "autonudge.json").write_text(json.dumps(raw), encoding="utf-8")

    reloaded = AutoNudgeService(base_dir=store_dir)
    await reloaded.start()
    try:
        value = reloaded._loops[loop.id].consecutive_failed_cycles
        assert isinstance(value, int) and value >= 0
        # The comparison the guard named must not raise.
        reloaded._cancel_timer(loop.id)
        await reloaded._timer(reloaded._loops[loop.id])
    finally:
        await _stop_and_drain(reloaded)


# --- The chat_runner-side GATE -----------------------------------------------
# The tests above pin the SERVICE (the streak, the stand-down, the clearing).
# These pin the GATE in chat_runner that decides WHICH terminal turns reach the
# service at all -- the part with the regression risk, called from both terminal
# arms. ``_note_cycle_failure`` is that gate as one tested helper; a double
# standing in for the module singleton records what it is asked to charge.

LOOP_ID = "loop-abc"


class _RecordingSvc:
    # start() publishes the service as the module singleton and the autouse
    # teardown unpublishes whatever is published, so a stand-in has to satisfy
    # that contract (an _inflight_adds set and a stop()) too. The hook is async
    # and awaited by the gate (the terminal arm is async), so the double is too.
    _inflight_adds: set = set()

    def __init__(self) -> None:
        self.charged: list[tuple[str, str, int]] = []

    async def notify_cycle_failed(
        self, slot_key: str, *, loop_id: str, expected_generation: int
    ) -> None:
        self.charged.append((slot_key, loop_id, expected_generation))

    def stop(self) -> None:
        return None


@pytest.mark.asyncio
async def test_the_gate_charges_a_self_wake_dispatched_death(monkeypatch):
    """The generic case the whole bound exists for: a self-wake cycle that reached
    a session and died with a plain error charges the streak, carrying the fired
    loop's id and generation through so the service can scope it."""
    from kiro_crew.dashboard import chat_runner

    svc = _RecordingSvc()
    monkeypatch.setattr(_an, "_INSTANCE", svc, raising=False)

    await chat_runner._note_cycle_failure(
        SLOT,
        RuntimeError("backend 503 after retries"),
        self_wake=True,
        loop_id=LOOP_ID,
        expected_generation=4,
    )

    assert svc.charged == [(SLOT, LOOP_ID, 4)]


@pytest.mark.asyncio
async def test_the_gate_ignores_a_human_turn(monkeypatch):
    """A human turn that happened to error on a slot that also carries a loop must
    not spend the loop's stand-down budget -- the self-wake guard is the whole
    reason a human's failure cannot stop the agent's loop."""
    from kiro_crew.dashboard import chat_runner

    svc = _RecordingSvc()
    monkeypatch.setattr(_an, "_INSTANCE", svc, raising=False)

    await chat_runner._note_cycle_failure(
        SLOT, RuntimeError("boom"), self_wake=False, loop_id=LOOP_ID, expected_generation=1
    )

    assert svc.charged == []


@pytest.mark.asyncio
async def test_the_gate_ignores_a_fire_with_no_loop_identity(monkeypatch):
    """A self-wake with no fired loop id (the non-self-wake default of "") has
    nothing to scope the charge to, so it records nothing rather than guess."""
    from kiro_crew.dashboard import chat_runner

    svc = _RecordingSvc()
    monkeypatch.setattr(_an, "_INSTANCE", svc, raising=False)

    await chat_runner._note_cycle_failure(
        SLOT, RuntimeError("boom"), self_wake=True, loop_id="", expected_generation=1
    )

    assert svc.charged == []


@pytest.mark.asyncio
async def test_the_gate_excludes_a_structural_rejection(monkeypatch):
    """A structurally rejected payload is one deterministic turn with its own
    terminal stop; counting it here would double-charge the same fault."""
    from kiro_crew.dashboard import chat_runner

    svc = _RecordingSvc()
    monkeypatch.setattr(_an, "_INSTANCE", svc, raising=False)

    exc = RuntimeError("malformed request")
    exc.structural_terminal = True  # type: ignore[attr-defined]
    await chat_runner._note_cycle_failure(
        SLOT, exc, self_wake=True, loop_id=LOOP_ID, expected_generation=1
    )

    assert svc.charged == []


@pytest.mark.asyncio
async def test_the_gate_excludes_a_session_start_failure(monkeypatch):
    """A cycle that never obtained a session has its own streak
    (``notify_cycle_start_failed``); the two bounds must not both charge it."""
    from kiro_crew.dashboard import chat_runner

    svc = _RecordingSvc()
    monkeypatch.setattr(_an, "_INSTANCE", svc, raising=False)

    exc = RuntimeError("session/new timed out")
    exc.session_start_failed = True  # type: ignore[attr-defined]
    await chat_runner._note_cycle_failure(
        SLOT, exc, self_wake=True, loop_id=LOOP_ID, expected_generation=1
    )

    assert svc.charged == []


@pytest.mark.parametrize("code", ["memory_unavailable", "materialization_changed"])
@pytest.mark.asyncio
async def test_the_gate_excludes_a_pre_dispatch_fault(monkeypatch, code):
    """The two faults the generic arm resolves that never reached a session: a
    memory store that would not open and a member agent file changed out of band.
    The stand-down (and the operator notice naming a backend/tool/timeout cause)
    must not fire for a turn that never dispatched -- the maintainer's point 1."""
    from kiro_crew.dashboard import chat_runner

    svc = _RecordingSvc()
    monkeypatch.setattr(_an, "_INSTANCE", svc, raising=False)

    await chat_runner._note_cycle_failure(
        SLOT,
        RuntimeError("could not open store"),
        self_wake=True,
        loop_id=LOOP_ID,
        expected_generation=1,
        err_meta={"code": code},
    )

    assert svc.charged == []


@pytest.mark.asyncio
async def test_the_gate_still_charges_a_plain_internal_bug_with_meta(monkeypatch):
    """A plain internal bug is NOT a pre-dispatch class: a self-wake cycle that
    dispatches and raises the same bug every interval is the no-progress waste the
    bound ends, so a row meta that is not one of the two excluded codes charges."""
    from kiro_crew.dashboard import chat_runner

    svc = _RecordingSvc()
    monkeypatch.setattr(_an, "_INSTANCE", svc, raising=False)

    await chat_runner._note_cycle_failure(
        SLOT,
        RuntimeError("unexpected KeyError in a tool"),
        self_wake=True,
        loop_id=LOOP_ID,
        expected_generation=2,
        err_meta={"code": "rejected_model"},
    )

    assert svc.charged == [(SLOT, LOOP_ID, 2)]


@pytest.mark.asyncio
async def test_the_gate_charges_the_timeout_family_that_lands_in_the_generic_arm(monkeypatch):
    """A prompt timeout and a wedged session descend from ``AcpRuntimeError``, not
    ``AcpError``, so they reach the generic terminal arm. A bare prompt timeout
    DID reach a session and then died, so it charges; a SESSION-START timeout
    carries ``session_start_failed`` (set in its ``__init__``) and the
    start-failure bound owns it instead."""
    from kiro_crew.acp.runtime import AcpSessionStartTimeout
    from kiro_crew.acp.session_handle import AcpRequestTimeout
    from kiro_crew.dashboard import chat_runner

    svc = _RecordingSvc()
    monkeypatch.setattr(_an, "_INSTANCE", svc, raising=False)

    # A dispatched prompt that timed out: no start tag -> charges.
    prompt_timeout = AcpRequestTimeout("prompt timed out")
    assert getattr(prompt_timeout, "session_start_failed", False) is False
    await chat_runner._note_cycle_failure(
        SLOT, prompt_timeout, self_wake=True, loop_id=LOOP_ID, expected_generation=3
    )
    assert svc.charged == [(SLOT, LOOP_ID, 3)]

    # A session-start timeout tags itself -> excluded (the start bound owns it).
    svc.charged.clear()
    start_timeout = AcpSessionStartTimeout("session/new timed out", collector=None)
    assert start_timeout.session_start_failed is True
    await chat_runner._note_cycle_failure(
        SLOT, start_timeout, self_wake=True, loop_id=LOOP_ID, expected_generation=3
    )
    assert svc.charged == []


@pytest.mark.asyncio
async def test_the_gate_is_silent_when_no_service_is_published(monkeypatch):
    """Best-effort: a monitoring convenience never changes how the turn is reported,
    so a missing singleton is swallowed, not raised, out of the terminal arm."""
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setattr(_an, "_INSTANCE", None, raising=False)

    # Must not raise.
    await chat_runner._note_cycle_failure(
        SLOT, RuntimeError("boom"), self_wake=True, loop_id=LOOP_ID, expected_generation=1
    )


@pytest.mark.asyncio
async def test_a_structured_monitor_loop_fault_is_not_charged(svc, _nosleep):
    """A structured monitor loop's ``_timer`` returns at its own
    ``is_structured_monitor_loop`` guard before the only reader of
    ``consecutive_failed_cycles`` (the consecutive-failure stand-down), so
    charging one would grow a write-only counter no bound can act on. The hook
    must skip it -- monitor faults are bounded by the monitor's own
    ``consecutive_provider_errors`` budget, not this stand-down.
    """
    loop = await _armed(svc)
    # Mark the live loop a structured monitor: ``monitor`` set, ``gate`` false
    # is exactly what ``is_structured_monitor_loop`` keys on.
    svc._loops[loop.id].monitor = MonitorState(
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        created_ts=1_000.0,
    )
    svc._loops[loop.id].gate = False

    await _fail(svc, loop)
    await _fail(svc, loop)

    # Skipped, not charged -- the counter stays at its initial zero.
    assert svc._loops[loop.id].consecutive_failed_cycles == 0
    assert svc._loops[loop.id].active is True
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_failed_durable_write_rolls_the_increment_back(svc, _nosleep):
    """Persist before you publish: the live count never leads the store.

    The increment is staged and the durable write awaited under the lock; if the
    write raises, the hook rolls ``consecutive_failed_cycles`` back to what the
    store last accepted and re-raises. A near-threshold streak the store never
    saw must not survive in memory to stand the loop down early.
    """
    loop = await _armed(svc)
    await _fail(svc, loop)
    assert svc._loops[loop.id].consecutive_failed_cycles == 1  # one durable charge

    class _WriteBoom(RuntimeError):
        pass

    async def _boom(payload=None):
        raise _WriteBoom

    svc._write_monitor_snapshot_locked = _boom  # type: ignore[method-assign]

    with pytest.raises(_WriteBoom):
        await _fail(svc, loop)

    # Rolled back to the last durably-accepted value, not left at 2.
    assert svc._loops[loop.id].consecutive_failed_cycles == 1
    assert svc._loops[loop.id].active is True
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_cancel_during_the_committed_write_keeps_the_increment(svc, _nosleep):
    """A cancellation whose write already landed must NOT erase the charge.

    ``_write_monitor_snapshot_locked`` shields its executor write and re-raises
    ``CancelledError`` only AFTER the future has settled, so a ``CancelledError``
    reaching the hook means the durable write DID complete. Rolling the increment
    back there would erase a failure the store already accepted and leave the
    live count one short of the durable one -- so the committed increment must
    stand even as the cancellation propagates. (Contrast the real-write-error
    path, which rolls back because nothing landed.)
    """
    loop = await _armed(svc)
    await _fail(svc, loop)
    assert svc._loops[loop.id].consecutive_failed_cycles == 1

    async def _write_then_cancel(payload=None):
        # Mirror the real writer's post-settle re-raise: the write is observed
        # as complete, THEN the cancellation surfaces.
        raise asyncio.CancelledError

    svc._write_monitor_snapshot_locked = _write_then_cancel  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await _fail(svc, loop)

    # The committed increment stands at 2 -- the cancel did not undo it.
    assert svc._loops[loop.id].consecutive_failed_cycles == 2
    assert svc._loops[loop.id].active is True
    await _stop_and_drain(svc)
