"""The orchestrator's first-stage config load must not run on the gateway loop.

``_stage_loop`` is async, but the first time it runs for a slot it has no
``OrchestrationTracker`` yet and builds one from the configured stage timeout.
That read goes through ``KiroCrewConfig.load()``, which stats and reads
``config.json`` plus any ``config.local.json`` overlay, deep-merges them and runs
the full jsonschema validation — all synchronously, on the single event loop the
gateway shares with every other session.

Only the load crosses to a worker. The value is applied and the slot is read back
on the loop, so no live orchestration state is handed to another thread.

The hop is also the loop's FIRST suspension point, ahead of every stage check, so
both cancellation channels can now land while the worker runs, and the tests here
drive each through its real endpoint:

* **Plan Cancel** stops ``slot._orch_tracker``. The tracker is therefore
  published BEFORE the load rather than built from its result, so the press has
  the canonical thing to stop and the check afterwards is the same
  ``_orchestration_stopped`` every advancement gate already uses. Nothing reads
  the stage budget until a stage records its first round, so adjusting it once
  the load returns is safe.
* **Dashboard Stop** can fire AND fully resolve inside the await, leaving
  ``_stopping`` back at False, which is why the loop compares
  ``_stop_generation`` rather than re-reading the flag.

Abandoning the plan is not the same as abandoning the slot: a message the user
queued while the config loaded is held behind the ``_in_stage_execution`` guard,
and only the stage loop's own ``finally`` clears it and hands the queue on. The
abort therefore leaves through that ``finally`` like any other exit.
"""

from __future__ import annotations

import asyncio
import threading
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.context_management import OrchestrationTracker
from kiro_crew.dashboard.state import _ChatSlot


def _state() -> MagicMock:
    state = MagicMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.subagents = MagicMock()
    state.subagents.running_agents_for = MagicMock(return_value=[])
    return state


def _slot(key: str = "cfg-load-slot") -> _ChatSlot:
    slot = _ChatSlot(key, mode="orchestrator")
    slot._auto_run = False
    # `_plan_stage_count` is derived from the titles, not settable. An empty plan
    # drives tracker initialisation — the branch under test — and then leaves the
    # stage loop with nothing to execute, so no model turn is involved.
    slot._stage_titles = []
    slot._orch_tracker = None
    return slot


def _write_config(payload: dict[str, Any]) -> None:
    """Put a real config.json in the per-test KIROCREW_HOME.

    The conftest pins that home to a tmp dir, so the REAL loader runs against
    controlled files rather than the developer's own configuration.
    """
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    import json

    (directory / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def _record_config_loads(monkeypatch: Any, threads: list[int], *, raises: bool = False) -> None:
    """Wrap the loader the orchestrator actually calls.

    ``chat_orchestrator`` binds ``KiroCrewConfig`` at import, so replacing that
    module attribute intercepts the production call site. The wrapper delegates to
    the real classmethod, so the recorded thread is the one that genuinely stats,
    reads, merges and validates the config — not merely a thread that reached a
    call site. It works identically whether the call site is direct or routed
    through ``asyncio.to_thread``.
    """

    def loader() -> KiroCrewConfig:
        threads.append(threading.get_ident())
        if raises:
            raise OSError("config unreadable")
        return KiroCrewConfig.load()

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_orchestrator.KiroCrewConfig",
        types.SimpleNamespace(load=loader),
    )


def _gate_config_load(monkeypatch: Any) -> tuple[asyncio.Event, asyncio.Event]:
    """Pause the real worker hop on loop-owned events, without wall-clock bounds."""
    entered = asyncio.Event()
    release = asyncio.Event()
    real_to_thread = asyncio.to_thread

    async def gated_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_orchestrator.asyncio.to_thread",
        gated_to_thread,
    )
    return entered, release


async def _wait_for_config_gate(task: asyncio.Task[Any], entered: asyncio.Event) -> None:
    """Wait for the gate, or fail immediately if the stage loop exits first."""
    entry_task = asyncio.create_task(entered.wait())
    done, _ = await asyncio.wait({task, entry_task}, return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        entry_task.cancel()
        await asyncio.gather(entry_task, return_exceptions=True)
        await task
        pytest.fail("the stage loop exited before reaching the config worker hop")
    await entry_task


async def _init_tracker(slot: _ChatSlot) -> None:
    from kiro_crew.dashboard.chat import _stage_loop

    await _stage_loop(_state(), slot, auto_run=False)


@pytest.mark.asyncio
async def test_config_load_runs_off_the_loop_thread(monkeypatch: Any) -> None:
    """The first-stage config read executes on a thread other than the loop's."""
    threads: list[int] = []
    _record_config_loads(monkeypatch, threads)

    slot = _slot()
    await _init_tracker(slot)

    assert threads, (
        "the orchestrator never loaded config -- this test no longer exercises "
        "tracker initialisation and would pass vacuously"
    )
    assert threading.get_ident() not in threads, (
        "the orchestrator config was loaded on the event-loop thread; the "
        "filesystem and schema work must be handed to asyncio.to_thread"
    )


@pytest.mark.asyncio
async def test_configured_timeout_reaches_the_tracker(monkeypatch: Any) -> None:
    """Preservation: the configured value is what the tracker is built with."""
    _write_config({"orchestrator": {"stage_timeout_seconds": 42}})
    threads: list[int] = []
    _record_config_loads(monkeypatch, threads)

    slot = _slot()
    await _init_tracker(slot)

    assert slot._orch_tracker is not None
    assert slot._orch_tracker.stage_timeout_seconds == 42


@pytest.mark.asyncio
async def test_zero_timeout_is_preserved_rather_than_defaulted(monkeypatch: Any) -> None:
    """Preservation: a falsy timeout means 'disabled' and must survive intact.

    The stage loop reads it back as ``if tracker.stage_timeout_seconds:``, so
    collapsing 0 onto the 1800 fallback with an ``or`` would silently re-enable a
    ceiling the operator turned off.
    """
    _write_config({"orchestrator": {"stage_timeout_seconds": 0}})
    threads: list[int] = []
    _record_config_loads(monkeypatch, threads)

    slot = _slot()
    await _init_tracker(slot)

    assert slot._orch_tracker is not None
    assert slot._orch_tracker.stage_timeout_seconds == 0


@pytest.mark.asyncio
async def test_loader_failure_falls_back_to_the_default_timeout(monkeypatch: Any) -> None:
    """Preservation: a raising loader still yields a usable tracker at 1800."""
    threads: list[int] = []
    _record_config_loads(monkeypatch, threads, raises=True)

    slot = _slot()
    await _init_tracker(slot)

    assert threads, "the failing loader was never reached"
    assert slot._orch_tracker is not None
    assert slot._orch_tracker.stage_timeout_seconds == 1800


@pytest.mark.asyncio
async def test_existing_tracker_does_not_reload_config(monkeypatch: Any) -> None:
    """Preservation: only the FIRST entry for a slot pays for a config load."""
    from kiro_crew.context_management import OrchestrationTracker

    threads: list[int] = []
    _record_config_loads(monkeypatch, threads)

    slot = _slot()
    existing = OrchestrationTracker(stage_timeout_seconds=77)
    slot._orch_tracker = existing
    await _init_tracker(slot)

    assert threads == [], "a slot that already has a tracker must not load config"
    assert slot._orch_tracker is existing
    assert slot._orch_tracker.stage_timeout_seconds == 77


def _stop_capable_state(slot: _ChatSlot) -> MagicMock:
    """A state stand-in the REAL stop handler can be driven against.

    ``stop_turn`` answers ``"idle"``, which is the outcome that creates the race:
    no ACP turn exists while the config worker runs, so the provider reports
    nothing to cancel and the handler drives ``_stop_state`` back to "idle".
    """
    state = _state()
    state._slots = {slot.key: slot}
    state.sessions.stop_turn = AsyncMock(return_value="idle")
    state.cancel_questions_for_slot = MagicMock(return_value=0)
    return state


async def _press_stop(state: MagicMock, slot: _ChatSlot) -> None:
    """Press Stop through the production endpoint.

    The whole point of the race is the sequence the handler itself performs —
    claim ``soft_pending``, clear ``_auto_run``, then release the posture on an
    "idle" outcome — so a hand-rolled state transition would only prove that a
    test can write the fields the fix reads.
    """
    from aiohttp import web

    from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

    app = web.Application()
    app["state"] = state
    request = MagicMock()
    # A bare MagicMock answers .get("app") with a truthy mock, which the App Kit
    # §5.2 cancel guard would read as an app token. This is a dashboard press.
    request.get = lambda key, default="": default
    request.app = app
    request.match_info = {"slot": slot.key}
    request.query = {}

    with (
        patch("kiro_crew.dashboard.chat_handlers.sel"),
        patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"),
    ):
        await api_chat_slot_stop(request)


async def _press_plan_cancel(state: MagicMock, slot: _ChatSlot) -> dict[str, Any]:
    """Press the plan card's Cancel through the production endpoint.

    Plan Cancel is a DIFFERENT cancellation domain from the dashboard Stop: it
    stops the orchestration tracker rather than the ACP turn, and it is the
    control the plan card actually offers while a plan is running.
    """
    import json as _json

    from aiohttp import web

    from kiro_crew.dashboard.chat_orchestrator import api_chat_plan_action

    app = web.Application()
    app["state"] = state
    request = MagicMock()
    request.app = app
    request.match_info = {"slot": slot.key}
    request.json = AsyncMock(return_value={"action": "cancel"})

    response = await api_chat_plan_action(request)
    return dict(_json.loads(response.body))


@pytest.mark.asyncio
async def test_plan_cancel_during_the_config_load_does_not_start_the_plan(
    monkeypatch: Any,
) -> None:
    """Cancel pressed while the config loads must not leave the plan running.

    The Cancel handler stops the plan by calling ``tracker.stop()``. Build the
    tracker after the load and it finds ``None`` during this window, stops
    nothing, and still reports success and prints "Plan cancelled" to the user.
    ``_auto_run`` cannot stand in for the signal either: ``_stage_loop`` captured
    ``auto_run`` as an argument when the task was created, so a Go All run keeps
    its own True, and a plain Go was already False and so carries no information.

    Publishing the tracker BEFORE the load is what closes it, so this test reads
    the canonical signal — ``tracker.stopped``, which every advancement gate
    already consults through ``_orchestration_stopped`` — rather than a second
    cancellation record kept beside it.
    """
    entered, release = _gate_config_load(monkeypatch)
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.sel", MagicMock())

    turns: list[str] = []

    async def _fake_run_chat(_state: Any, _slot: Any, context: str, **_kwargs: Any) -> None:
        turns.append(context)

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _fake_run_chat)

    from kiro_crew.dashboard.chat import _stage_loop

    slot = _slot("plan-cancel-race-slot")
    slot._stage_titles = ["Collect the evidence"]
    slot._auto_run = True
    state = _stop_capable_state(slot)

    # Go All: auto_run is captured as an argument here, exactly as the plan-action
    # handler creates it, so clearing slot._auto_run later cannot reach it.
    task = asyncio.create_task(_stage_loop(state, slot, auto_run=True))
    slot.task = task

    try:
        await _wait_for_config_gate(task, entered)
        _tracker = slot._orch_tracker
        assert _tracker is not None, (
            "Cancel has nothing to stop while the config loads: the tracker must be "
            "published before the await, or this window needs its own cancel record"
        )
        assert _tracker.stopped is False

        body = await _press_plan_cancel(state, slot)

        assert body.get("cancelled") is True, (
            "the endpoint did not report a cancellation, so this test is not "
            "exercising the state the user was shown"
        )
    finally:
        # Release and reap on every assertion path, so no deferred load or stage
        # task survives fixture teardown.
        release.set()
        await task

    assert turns == [], (
        "a stage turn ran after the user cancelled the plan: the Cancel handler "
        "found no tracker to stop, and the config await let the bootstrap resume"
    )
    assert _tracker.stopped is True, (
        "the press did not reach the tracker, so the plan was abandoned by "
        "something other than the signal the rest of the loop reads"
    )


@pytest.mark.asyncio
async def test_a_plan_started_after_a_cancel_still_runs(monkeypatch: Any) -> None:
    """A cancel must not poison the NEXT plan.

    This is the risk any sticky cancel signal carries, and the reason not to
    keep a second one on the slot: a flag that outlives the run it cancelled has
    to be reset by whoever starts the next plan, and a missed reset silently
    disables orchestration for the rest of the slot's life.

    ``tracker.stopped`` needs no reset because the tracker does not outlive its
    plan. Current main also latches a Cancel that beats tracker creation, so
    arming the next plan must go through ``_reset_auto_run_for_new_plan`` to clear
    both records before the bootstrap publishes a fresh, unstopped tracker.
    """
    threads: list[int] = []
    _record_config_loads(monkeypatch, threads)
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.sel", MagicMock())

    turns: list[str] = []

    async def _fake_run_chat(_state: Any, _slot: Any, context: str, **_kwargs: Any) -> None:
        turns.append(context)

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _fake_run_chat)

    from kiro_crew.dashboard.chat import _stage_loop
    from kiro_crew.dashboard.chat_title import _reset_auto_run_for_new_plan

    slot = _slot("cancel-then-replan-slot")
    slot._stage_titles = ["Collect the evidence"]
    state = _stop_capable_state(slot)

    # An earlier plan ran and was cancelled, so a stopped tracker is on the slot.
    slot._orch_tracker = OrchestrationTracker()
    body = await _press_plan_cancel(state, slot)
    assert body.get("cancelled") is True
    assert slot._orch_tracker.stopped is True, "the cancel never reached the tracker"

    # Drive the real arm seam: it drops the stopped tracker and clears main's
    # pending-Go cancel latch together.
    _reset_auto_run_for_new_plan(slot)
    slot._auto_run = True
    await _stage_loop(state, slot, auto_run=True)

    assert turns, (
        "a plan started after an earlier cancel never executed: the cancel "
        "signal is being read as sticky rather than as a generation"
    )
    assert slot._orch_tracker is not None


@pytest.mark.asyncio
async def test_stop_during_the_config_load_does_not_start_the_plan(monkeypatch: Any) -> None:
    """A Stop that fires AND resolves during the await must not leave the plan running.

    Ordering is forced with events rather than sleeps: the loader blocks inside
    the worker until the Stop has been pressed and has settled all the way back
    to "idle". At that moment ``_stopping`` reads False, so a stage loop that
    re-checked only that field would resume the plan the user just cancelled.
    """
    entered, release = _gate_config_load(monkeypatch)
    # SEL writes are irrelevant here and only the stopped-plan path would emit
    # them; silence them so the assertions read against a quiet slot.
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.sel", MagicMock())

    turns: list[str] = []

    async def _fake_run_chat(_state: Any, _slot: Any, context: str, **_kwargs: Any) -> None:
        turns.append(context)

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _fake_run_chat)

    from kiro_crew.dashboard.chat import _stage_loop

    slot = _slot("stop-race-slot")
    # A real plan, so reaching stage execution is observable — the empty plan the
    # other tests use has nothing to run and would pass either way.
    slot._stage_titles = ["Collect the evidence"]
    slot._auto_run = True
    state = _stop_capable_state(slot)

    task = asyncio.create_task(_stage_loop(state, slot, auto_run=True))
    # The stage loop IS the slot's task in production, and the stop handler
    # no-ops on a slot that is not running.
    slot.task = task
    generation_before = slot._stop_generation

    try:
        await _wait_for_config_gate(task, entered)

        await _press_stop(state, slot)

        assert (
            slot._stop_generation == generation_before + 1
        ), "the Stop never initiated, so this test is not exercising the race"
        assert slot._stop_state == "idle", (
            "the stop must RESOLVE back to idle -- that is precisely what hides it "
            "from a plain _stopping re-read, and without it the race is not modelled"
        )
        assert slot._auto_run is False
    finally:
        # Release and reap on every assertion path, so no deferred load or stage
        # task survives fixture teardown.
        release.set()
        await task

    assert turns == [], (
        "a stage turn ran after the user stopped the plan: the config await let a "
        "Stop fire and resolve unobserved"
    )
    assert (
        slot._in_stage_execution is False
    ), "the abandoned bootstrap left the stage-execution guard set"


@pytest.mark.asyncio
async def test_a_message_queued_during_the_config_load_is_still_handed_off(
    monkeypatch: Any,
) -> None:
    """An abandoned bootstrap still owes the slot the exit every other path takes.

    While the config loads, the stage loop owns ``slot.task``, so ``api_chat``
    queues a user message instead of starting a turn — and
    ``_start_next_queued_turn`` HOLDS it there behind the
    ``_in_stage_execution`` guard rather than draining it, so it can never run
    concurrently with a plan. The stage loop's ``finally`` is the single thing
    that clears that guard and hands the queue on.

    Leaving the bootstrap with a bare ``return`` skipped all of it. The guard
    stayed set on a slot with no loop left to clear it, and the message stayed
    parked behind it: a later prompt could overtake it, and on restart it could
    be dropped entirely. So the abort must exit the way a cancel at stage 0
    does, not by stepping around the contract.
    """
    entered, release = _gate_config_load(monkeypatch)
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.sel", MagicMock())

    turns: list[str] = []

    async def _fake_run_chat(_state: Any, _slot: Any, context: str, **_kwargs: Any) -> None:
        turns.append(context)

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _fake_run_chat)

    # The handoff itself is the subject, so it is observed at the seam the loop
    # actually calls rather than by running a real turn.
    handoffs: list[str] = []

    async def _fake_handoff(_state: Any, _slot: Any) -> bool:
        handoffs.append(_slot.key)
        return False

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_orchestrator._start_next_queued_turn", _fake_handoff
    )

    from kiro_crew.dashboard.chat import _stage_loop

    slot = _slot("queued-message-slot")
    slot._stage_titles = ["Collect the evidence"]
    slot._auto_run = True
    state = _stop_capable_state(slot)

    task = asyncio.create_task(_stage_loop(state, slot, auto_run=True))
    slot.task = task

    try:
        await _wait_for_config_gate(task, entered)

        # The user types while the plan is still booting. Queued rather than run,
        # exactly as api_chat does for a slot whose task is live.
        queue_id = slot.queue_append("what about the tests?")
        assert slot.queue_depth == 1
        # Recorded, not asserted here: the handoff below is the contract, and a
        # precondition that fails first would mask it.
        guard_while_booting = slot._in_stage_execution

        body = await _press_plan_cancel(state, slot)
        assert body.get("cancelled") is True
    finally:
        # Release and reap on every assertion path, so no deferred load or stage
        # task survives fixture teardown.
        release.set()
        await task

    assert turns == [], "the cancelled plan executed a stage"
    assert handoffs == [slot.key], (
        "the message the user queued during the config load was never handed "
        f"off; it is stranded on the slot (queue={queue_id}) with no loop left "
        "to release it"
    )
    assert (
        slot._in_stage_execution is False
    ), "the stage-execution guard survived the abandoned bootstrap"
    # The boot window is INSIDE the guarded region, which is what makes the
    # handoff above load-bearing: _start_next_queued_turn holds a user message
    # while it is set, so nothing but this loop's own exit can release one
    # queued here.
    assert guard_while_booting is True, (
        "the config load ran outside _in_stage_execution, so a message queued "
        "during it was never covered by the guard this loop clears"
    )


@pytest.mark.asyncio
async def test_a_round_recorded_during_the_config_load_cannot_skip_a_stage(
    monkeypatch: Any,
) -> None:
    """Publishing the tracker early does not expose ``start_idx`` to a late round.

    ``start_idx`` is what decides which stage the loop resumes at, and the worry
    about publishing the tracker before the load is that some other actor
    records a round on it first, moving ``current_stage`` and skipping stage 1.

    The arithmetic is real -- the control below proves it -- but the window is
    not. ``start_idx`` is frozen into a local ``int`` BEFORE the loop's first
    ``await``; between the publication and that read there is nothing but two
    attribute reads, and on a single-threaded loop nothing else can execute
    there. Everything the config await opens up happens strictly after the value
    is already captured.

    So this drives the widest window the offload creates: the loop is held
    suspended in the config load, and the exact call the other ``record_round``
    caller makes (``slack/gateway.py``: ``stage = tracker.current_stage`` then
    ``tracker.record_round(stage)``) is injected on the freshly published
    tracker. Both stages must still run.
    """
    entered, release = _gate_config_load(monkeypatch)
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.sel", MagicMock())

    turns: list[str] = []

    async def _fake_run_chat(_state: Any, _slot: Any, context: str, **_kwargs: Any) -> None:
        turns.append(context)

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _fake_run_chat)

    from kiro_crew.dashboard.chat import _stage_loop

    slot = _slot("late-round-slot")
    slot._stage_titles = ["Collect the evidence", "Write it up"]
    slot._auto_run = True
    state = _stop_capable_state(slot)

    task = asyncio.create_task(_stage_loop(state, slot, auto_run=True))
    slot.task = task

    try:
        await _wait_for_config_gate(task, entered)
        published = slot._orch_tracker
        assert published is not None, "the tracker is not published during the load"
        # Verbatim shape of the only other record_round caller in the repo.
        stage = published.current_stage
        published.record_round(stage)
        assert published._stage_rounds, "the injected round did not land on the tracker"
    finally:
        release.set()
        await task

    assert len(turns) == 2, (
        "a round recorded during the config load skipped a stage: start_idx was "
        f"read after the window opened, not before it (ran {len(turns)} of 2)"
    )


@pytest.mark.asyncio
async def test_a_round_recorded_before_loop_entry_does_skip_a_stage(
    monkeypatch: Any,
) -> None:
    """The positive control for the test above -- the arithmetic IS real.

    A tracker that already carries a round at loop ENTRY makes ``start_idx``
    non-zero and stage 1 is skipped. That is the resume path: ``_orch_tracker``
    is already set, so nothing is published, ``_bootstrapping`` is False and no
    config load runs at all. Keeping this here is what stops the test above from
    reading as "record_round does nothing".
    """
    threads: list[int] = []
    _record_config_loads(monkeypatch, threads)
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.sel", MagicMock())

    turns: list[str] = []

    async def _fake_run_chat(_state: Any, _slot: Any, context: str, **_kwargs: Any) -> None:
        turns.append(context)

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _fake_run_chat)

    from kiro_crew.dashboard.chat import _stage_loop

    slot = _slot("resume-path-slot")
    slot._stage_titles = ["Collect the evidence", "Write it up"]
    slot._auto_run = True
    state = _stop_capable_state(slot)

    resumed = OrchestrationTracker()
    resumed.record_round(resumed.current_stage)
    slot._orch_tracker = resumed

    await _stage_loop(state, slot, auto_run=True)

    assert len(turns) == 1, "the resume path should start at stage 2, running one stage"
    assert threads == [], "the resume path must not load the config at all"
