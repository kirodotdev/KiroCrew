"""Tests for the manual goal-loop trigger.

The gap these pin: the loop interval is an IDLE gap, so an operator who already
knows the thing being waited on has changed had no way to tell the loop to look
now. There was no out-of-band fire anywhere on the surface — the autonudge HTTP
API was list / get / start / update / delete, and no MCP tool fired either.

Two properties matter more than the button, and both are asserted here rather
than described in prose:

* **The schedule.** A manual fire must not silently shift the interval. It does
  not, and the reason is that it reuses the delivered-fire bookkeeping: a
  delivered cycle clears ``next_due_ts`` and the re-arm then starts a fresh full
  interval, so the next automatic nudge lands one ``idle_secs`` after the manual
  turn ENDS. Pinned by
  ``test_the_next_automatic_nudge_is_a_full_interval_after_the_manual_turn_ends``.
* **The bounds.** A manual press must not buy a turn past a bound the user
  armed. ``fire_now`` arms the ordinary ``_timer`` body rather than calling
  ``_run_fire_cycle`` directly, so every terminal gate still runs. Pinned per
  gate — the cycle cap and the stop sentinel each get their own test, because a
  single test would only be proven to the first gate that fires.

The refusals are pinned too, and the mid-fire one is load-bearing rather than
defensive: ``_arm_timer`` cancels the existing timer task, and inside the fire
window that task may be parked on ``_persist_locked()`` writing the delivered
cycle.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.dashboard.handlers import autonudge as h
from kiro_crew.monitoring.models import MonitorState


@pytest.fixture(autouse=True)
def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(scope="session")
def svc_base_dir(tmp_path_factory: pytest.TempPathFactory):
    """A SESSION-scoped store directory for every service these tests build.

    Not each test's ``tmp_path``. ``_persist_soon`` dispatches its write through
    ``run_in_executor``, and a thread already inside ``_write_state`` cannot be
    stopped -- cancelling the awaiting task does not reach it. So a late write can
    land after a per-test directory is torn down and RECREATE it, a side effect
    that outlives the test.

    An earlier revision drained those writers at teardown instead. That narrows
    the window but cannot close it, because the cancel branch leaves the thread
    running; the review that pressed on it was right. Giving the writes a
    directory nobody deletes mid-run removes the hazard rather than racing it.
    """
    return tmp_path_factory.mktemp("autonudge-manual-fire")


def _loop(**over: Any) -> NudgeLoop:
    fields: dict[str, Any] = {
        "id": "lp-1",
        "slot_key": "chat-1-111",
        "message": "check the PR",
        "idle_secs": 300,
        "max_cycles": 24,
        "cycle_count": 3,
        # A live loop mid-countdown: the value the press must move.
        "next_due_ts": 9_999_999_999.0,
    }
    fields.update(over)
    return NudgeLoop(**fields)


async def _run_armed_cycle(svc: AutoNudgeService, loop_id: str) -> None:
    """Await the timer task ``fire_now`` just armed, to completion.

    The suite's established shape (``await svc._timers[loop.id]`` in
    ``test_autonudge.py``). Captured by REFERENCE before any await, because the
    gates inside ``_timer`` reach ``update``/``remove``, which pop the entry from
    ``_timers`` — reading the dict afterwards would raise KeyError on exactly
    the paths this file is here to test. Draining "until no tasks remain"
    instead is wrong: ``_persist_soon`` schedules a supervised background write,
    so that condition is never reached.
    """
    task = svc._timers[loop_id]
    await asyncio.gather(task, return_exceptions=True)


# --------------------------------------------------------------------------- #
# AutoNudgeService.fire_now
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_manual_trigger_delivers_the_cycle_through_the_ordinary_fire_path(
    svc_base_dir, monkeypatch
) -> None:
    """The press runs the SAME delivery the timer runs, and counts as a turn.

    ``cycle_count`` is documented as counting DELIVERED TURNS, and a manual
    nudge delivers one and spends a model turn exactly as a scheduled one does,
    so it advances the counter. That falls out of reusing ``_run_fire_cycle``
    rather than being decided here — there is no manual-vs-scheduled branch to
    get wrong.
    """
    fired: list[NudgeLoop] = []

    async def on_fire(loop: NudgeLoop) -> bool:
        fired.append(loop)
        return True

    svc = AutoNudgeService(base_dir=svc_base_dir, on_fire=on_fire)
    loop = _loop()
    svc._loops[loop.id] = loop

    # The press must not TOUCH the deadline. An earlier revision wrote
    # ``next_due_ts = now`` and awaited a durable persist so a restart would
    # resume overdue; that await was a suspension window, and this module has
    # several writers to that field which hold no lock, so five successive races
    # came out of it. The write is gone and the arm is what brings the cycle
    # forward, so the field must read exactly as the loop left it.
    due_at_arm: list[float] = []
    real_arm = svc._arm_timer

    def _arm(lp: NudgeLoop, **kw: Any) -> None:
        due_at_arm.append(lp.next_due_ts)
        real_arm(lp, **kw)

    monkeypatch.setattr(svc, "_arm_timer", _arm)

    armed, error, status = await svc.fire_now(loop.id)

    assert (error, status) == ("", 200)
    assert armed is loop
    assert due_at_arm == [9_999_999_999.0], (
        "fire_now moved the deadline; the write was removed because it cannot be "
        "made durable here without a suspension point"
    )
    await _run_armed_cycle(svc, loop.id)
    assert [lp.id for lp in fired] == ["lp-1"]
    assert loop.cycle_count == 4
    # Cleared by the delivered-fire bookkeeping, which is what makes the re-arm
    # below start a fresh full interval instead of resuming a stale deadline.
    assert loop.next_due_ts == 0.0
    svc.stop()


@pytest.mark.asyncio
async def test_the_next_automatic_nudge_is_a_full_interval_after_the_manual_turn_ends(
    svc_base_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manual trigger's second requirement, and the one a defect would hide in.

    "Reset the countdown to zero, so the next automatic nudge is a full interval
    away from the manual one." Measured from the end of the manual TURN, which
    is when ``notify_turn_complete`` fires — not from the button press, because
    the turn is what the interval is an idle gap between.
    """

    async def on_fire(_loop: NudgeLoop) -> bool:
        return True

    svc = AutoNudgeService(base_dir=svc_base_dir, on_fire=on_fire)
    loop = _loop(idle_secs=300)
    svc._loops[loop.id] = loop

    await svc.fire_now(loop.id)
    await _run_armed_cycle(svc, loop.id)
    assert loop.next_due_ts == 0.0  # delivered

    # Freeze the clock only for the re-arm, so the assertion is an equality
    # rather than a tolerance window a slow host could widen past.
    monkeypatch.setattr("kiro_crew.autonudge.time.time", lambda: 1_700_000_000.0)
    svc.notify_turn_complete(loop.slot_key)

    assert loop.next_due_ts == 1_700_000_000.0 + 300
    svc.stop()


@pytest.mark.asyncio
async def test_a_manual_trigger_cannot_buy_a_turn_past_the_cycle_cap(svc_base_dir) -> None:
    """The cap gate still runs, because the press arms ``_timer`` not the fire.

    An active loop already at its cap is reachable — the cap is checked on the
    tick BEFORE the fire, so a store written at ``cycle_count == max_cycles``
    (or a loop whose last delivery reached it) is live until its next tick. A
    manual press on it must deactivate, exactly as that tick would, rather than
    deliver one more turn.
    """
    fired: list[NudgeLoop] = []

    async def on_fire(loop: NudgeLoop) -> bool:
        fired.append(loop)
        return True

    svc = AutoNudgeService(base_dir=svc_base_dir, on_fire=on_fire)
    loop = _loop(max_cycles=4, cycle_count=4)
    svc._loops[loop.id] = loop

    _armed, error, status = await svc.fire_now(loop.id)
    assert (error, status) == ("", 200)
    await _run_armed_cycle(svc, loop.id)

    assert fired == []
    assert loop.active is False
    assert loop.stopped_reason == "cycle_cap"
    svc.stop()


@pytest.mark.asyncio
async def test_a_manual_trigger_still_honours_the_stop_sentinel(tmp_path, svc_base_dir) -> None:
    """Second gate, second test: the kill switch is not bypassed by the button.

    Its own test rather than an extra assertion on the cap one, because a test
    is only proven to the first gate that fires — the cap returns before the
    sentinel check would ever be reached, so one test cannot cover both.
    """
    fired: list[NudgeLoop] = []

    async def on_fire(loop: NudgeLoop) -> bool:
        fired.append(loop)
        return True

    sentinel = tmp_path / "STOP"
    sentinel.write_text("halt", encoding="utf-8")
    svc = AutoNudgeService(base_dir=svc_base_dir, on_fire=on_fire)
    loop = _loop(stop_sentinel_path=str(sentinel))
    svc._loops[loop.id] = loop

    _armed, error, status = await svc.fire_now(loop.id)
    assert (error, status) == ("", 200)
    await _run_armed_cycle(svc, loop.id)

    assert fired == []
    assert loop.id not in svc._loops
    svc.stop()


@pytest.mark.asyncio
async def test_fire_now_refuses_an_inactive_loop(svc_base_dir) -> None:
    """One condition covers every terminal bound, so none is restated.

    A cap, a spent runtime budget, an approval stall and a sentinel removal all
    leave the loop inactive, so refusing on ``active`` refuses all of them
    without a second copy of the list to drift.
    """
    fired: list[NudgeLoop] = []

    async def on_fire(loop: NudgeLoop) -> bool:
        fired.append(loop)
        return True

    svc = AutoNudgeService(base_dir=svc_base_dir, on_fire=on_fire)
    loop = _loop(active=False)
    svc._loops[loop.id] = loop

    armed, error, status = await svc.fire_now(loop.id)

    assert armed is None
    assert status == 409
    assert error == "loop is not active"
    assert loop.id not in svc._timers
    assert fired == []
    svc.stop()


@pytest.mark.asyncio
async def test_fire_now_refuses_a_loop_that_is_already_firing(svc_base_dir) -> None:
    """Load-bearing, not defensive: arming would cancel the firing task.

    ``_arm_timer`` cancels the existing timer before it creates a new one, and
    inside the fire window that task may be parked on ``_persist_locked()``
    writing the delivered cycle — cancelling it there loses the ``cycle_count``
    bump. The assertion is therefore that the IN-FLIGHT TASK SURVIVES, not
    merely that a 409 came back.
    """
    svc = AutoNudgeService(base_dir=svc_base_dir)
    loop = _loop()
    svc._loops[loop.id] = loop

    started = asyncio.Event()
    release = asyncio.Event()

    async def in_flight() -> None:
        started.set()
        await release.wait()

    task = asyncio.create_task(in_flight())
    await started.wait()
    svc._timers[loop.id] = task
    svc._firing.add(loop.id)

    armed, error, status = await svc.fire_now(loop.id)

    assert armed is None
    assert status == 409
    assert error == "loop is already firing"
    assert svc._timers[loop.id] is task
    assert not task.cancelled()
    assert not task.done()

    release.set()
    await task
    svc._firing.discard(loop.id)
    svc.stop()


@pytest.mark.asyncio
async def test_fire_now_refuses_a_loop_the_service_does_not_hold(svc_base_dir) -> None:
    """404, resolved through the shared ``get_by_id`` accessor."""
    svc = AutoNudgeService(base_dir=svc_base_dir)

    armed, error, status = await svc.fire_now("no-such-loop")

    assert armed is None
    assert status == 404
    assert error == "loop not found"
    svc.stop()


# --------------------------------------------------------------------------- #
# POST /api/autonudge/{loop_id}/fire
# --------------------------------------------------------------------------- #


class _FakeSvc:
    """Only what the fire route calls."""

    def __init__(self, loops: list[NudgeLoop]) -> None:
        self.loops = loops
        self.fired: list[str] = []
        self.result: tuple[NudgeLoop | None, str, int] | None = None

    def get_by_id(self, loop_id: str) -> NudgeLoop | None:
        return next((lp for lp in self.loops if lp.id == loop_id), None)

    async def fire_now(self, loop_id: str) -> tuple[NudgeLoop | None, str, int]:
        self.fired.append(loop_id)
        if self.result is not None:
            return self.result
        return self.get_by_id(loop_id), "", 200


def _svc_with_loop(svc_base_dir: Any) -> tuple[AutoNudgeService, NudgeLoop]:
    """A real service holding one live loop, for the service-level assertions.

    Built inline the way every other service test here builds it, rather than as
    a fixture: these tests patch instance methods (``_persist_locked``,
    ``_arm_timer``) and a shared fixture would hide which one each test replaced.
    """

    async def _on_fire(loop: NudgeLoop) -> bool:
        return True

    svc = AutoNudgeService(base_dir=svc_base_dir, on_fire=_on_fire)
    loop = _loop()
    svc._loops[loop.id] = loop
    return svc, loop


@pytest.fixture(autouse=True)
def sel_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    sink = MagicMock()
    monkeypatch.setattr(h, "sel", lambda: sink)
    return sink


def _mk(loop_id: str, *, slot: Any = None) -> web.Request:
    app = web.Application()
    state = MagicMock()
    state.get_slot = MagicMock(return_value=slot)
    app["state"] = state
    req = make_mocked_request(
        "POST",
        f"/api/autonudge/{loop_id}/fire",
        app=app,
        match_info={"loop_id": loop_id},
    )
    req["user"] = "local-app"
    return req


def _body(response: web.StreamResponse) -> dict:
    assert isinstance(response, web.Response)
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _slot(*, running: bool = False, in_stage: bool = False) -> MagicMock:
    slot = MagicMock()
    slot.running = running
    # Modelled explicitly: a bare MagicMock attribute is truthy and would trip
    # the busy guard on every test.
    slot._in_stage_execution = in_stage
    return slot


@pytest.mark.asyncio
async def test_route_fires_and_returns_the_updated_loop(monkeypatch) -> None:
    loop = NudgeLoop(id="lp-1", slot_key="chat-1-111", message="check", idle_secs=300)
    svc = _FakeSvc([loop])
    monkeypatch.setattr(h, "_autonudge_get", lambda: svc)

    resp = await h.api_autonudge_fire(_mk("lp-1", slot=_slot()))

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True
    assert body["loop"]["id"] == "lp-1"
    assert svc.fired == ["lp-1"]


@pytest.mark.asyncio
async def test_route_refuses_when_the_session_already_has_a_turn_in_flight(monkeypatch) -> None:
    """Refused, not queued — and the fire path already decided that.

    Its own comment states the reason: queueing "would stack identical 3KB+
    nudges and blow up the context window". So this is the repository's recorded
    answer being surfaced as a 409, not a new product decision.
    """
    loop = NudgeLoop(id="lp-1", slot_key="chat-1-111", message="check", idle_secs=300)
    svc = _FakeSvc([loop])
    monkeypatch.setattr(h, "_autonudge_get", lambda: svc)

    resp = await h.api_autonudge_fire(_mk("lp-1", slot=_slot(running=True)))

    assert resp.status == 409
    assert _body(resp)["code"] == "session_busy"
    assert svc.fired == []


@pytest.mark.asyncio
async def test_route_refuses_between_the_stages_of_a_multi_stage_plan(monkeypatch) -> None:
    """``slot.running`` alone reads False in that window.

    Which is exactly why the canonical predicate is two-term. Without the
    ``_in_stage_execution`` half this press would land a concurrent turn on top
    of a plan that is mid-flight.
    """
    loop = NudgeLoop(id="lp-1", slot_key="chat-1-111", message="check", idle_secs=300)
    svc = _FakeSvc([loop])
    monkeypatch.setattr(h, "_autonudge_get", lambda: svc)

    resp = await h.api_autonudge_fire(_mk("lp-1", slot=_slot(running=False, in_stage=True)))

    assert resp.status == 409
    assert _body(resp)["code"] == "session_busy"
    assert svc.fired == []


@pytest.mark.asyncio
async def test_route_refuses_a_structured_monitor(monkeypatch) -> None:
    """Same 409 code ``PATCH`` gives: those records belong to the monitor API."""
    loop = NudgeLoop(id="mon-1", slot_key="chat-1-111", message="check", idle_secs=300)
    loop.monitor = MonitorState(
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        created_ts=1.0,
    )
    svc = _FakeSvc([loop])
    monkeypatch.setattr(h, "_autonudge_get", lambda: svc)

    resp = await h.api_autonudge_fire(_mk("mon-1", slot=_slot()))

    assert resp.status == 409
    assert _body(resp)["code"] == "structured_monitor_requires_monitor_api"
    assert svc.fired == []


@pytest.mark.asyncio
async def test_route_reports_404_for_an_unknown_loop(monkeypatch) -> None:
    svc = _FakeSvc([])
    monkeypatch.setattr(h, "_autonudge_get", lambda: svc)

    resp = await h.api_autonudge_fire(_mk("nope", slot=_slot()))

    assert resp.status == 404
    assert _body(resp)["code"] == "autonudge_not_found"


@pytest.mark.asyncio
async def test_route_reports_503_when_the_feature_is_off(monkeypatch) -> None:
    monkeypatch.setattr(h, "_autonudge_get", lambda: None)

    resp = await h.api_autonudge_fire(_mk("lp-1"))

    assert resp.status == 503
    assert _body(resp)["code"] == "autonudge_disabled"


@pytest.mark.asyncio
async def test_route_surfaces_a_service_refusal_and_audits_it_as_denied(
    monkeypatch, sel_mock: MagicMock
) -> None:
    """A denied press is audited too, not only a successful one.

    A delivered cycle spends a model turn, so both outcomes belong in the audit
    for the same reason ``DELETE`` records its own.
    """
    loop = NudgeLoop(id="lp-1", slot_key="chat-1-111", message="check", idle_secs=300)
    svc = _FakeSvc([loop])
    svc.result = (None, "loop is already firing", 409)
    monkeypatch.setattr(h, "_autonudge_get", lambda: svc)

    resp = await h.api_autonudge_fire(_mk("lp-1", slot=_slot()))

    assert resp.status == 409
    assert _body(resp)["error"] == "loop is already firing"
    kwargs = sel_mock.log_tool_invocation.call_args.kwargs
    assert kwargs["tool_name"] == "autonudge_fire"
    assert kwargs["outcome"] == "denied"
    assert kwargs["session_key"] == "chat-1-111"


@pytest.mark.asyncio
async def test_route_skips_the_busy_check_for_a_channel_bound_loop(monkeypatch) -> None:
    """No dashboard slot exists for a ``slack:`` key, so that transport answers.

    Reading a missing slot as "not busy" is the correct fallthrough here: the
    channel fire paths carry their own ``is_busy`` guard, and refusing on a slot
    lookup that can never succeed would make the route permanently unusable for
    those loops.
    """
    loop = NudgeLoop(id="lp-1", slot_key="slack:C1.123", message="check", idle_secs=300)
    svc = _FakeSvc([loop])
    monkeypatch.setattr(h, "_autonudge_get", lambda: svc)

    resp = await h.api_autonudge_fire(_mk("lp-1", slot=None))

    assert resp.status == 200
    assert svc.fired == ["lp-1"]


@pytest.mark.asyncio
async def test_every_early_refusal_is_audited_as_denied(monkeypatch, sel_mock: MagicMock) -> None:
    """No exit from this route is un-audited, including the four early guards.

    The first revision audited only after ``fire_now`` returned, so a request
    denied by an earlier guard left no SEL event at all -- a denied permission
    decision invisible to the audit trail, which is the one thing that trail
    exists to record. Asserted as a LOOP over every refusing guard rather than
    one test per guard on purpose: the property is "there is no un-audited way
    out", and a guard added later that forgets the event should fail an existing
    test rather than wait for someone to remember to write a new one.

    ``sel_mock`` is reset between cases so each assertion is about its own
    request; a cumulative call count would pass even if one case emitted two
    events and another emitted none.
    """
    disabled = object()  # sentinel: the 503 arm never touches the service

    cases: list[tuple[str, Any, int, str, str]] = [
        # (label, service, expected status, expected code, expected audited key)
        ("feature disabled", disabled, 503, "autonudge_disabled", ""),
        ("unknown loop", _FakeSvc([]), 404, "autonudge_not_found", ""),
        (
            "structured monitor",
            _FakeSvc(
                [
                    NudgeLoop(
                        id="lp-1",
                        slot_key="chat-1-111",
                        message="check",
                        idle_secs=300,
                        monitor=MonitorState(
                            kind="github_pull_request",
                            target="https://github.com/acme/widgets/pull/7",
                            objective="review_ready",
                            created_ts=1.0,
                        ),
                    )
                ]
            ),
            409,
            "structured_monitor_requires_monitor_api",
            "chat-1-111",
        ),
    ]

    for label, svc, want_status, want_code, want_session in cases:
        sel_mock.reset_mock()
        monkeypatch.setattr(
            h, "_autonudge_get", (lambda: None) if svc is disabled else (lambda s=svc: s)
        )

        resp = await h.api_autonudge_fire(_mk("lp-1", slot=_slot()))

        assert resp.status == want_status, label
        assert _body(resp)["code"] == want_code, label
        assert sel_mock.log_tool_invocation.call_count == 1, f"{label}: one event"
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "autonudge_fire", label
        assert kwargs["outcome"] == "denied", label
        assert kwargs["session_key"] == want_session, label
        # The loop id is recorded even on the disabled path, which is why it is
        # read before the service lookup rather than after it.
        assert kwargs["metadata"]["loop_id"] == "lp-1", label
        assert kwargs["metadata"]["error"] == want_code, label


@pytest.mark.asyncio
async def test_the_busy_refusal_is_audited_as_denied(monkeypatch, sel_mock: MagicMock) -> None:
    """The busy guard specifically -- it is the one a reviewer flagged.

    Kept separate from the loop above because it needs a slot in a distinct
    state rather than a distinct service, and because it is the refusal an
    operator actually hits: a mid-turn agent is a common state, not an edge one,
    so this is the audit event most likely to matter in a real trail.
    """
    loop = NudgeLoop(id="lp-1", slot_key="chat-1-111", message="check", idle_secs=300)
    svc = _FakeSvc([loop])
    monkeypatch.setattr(h, "_autonudge_get", lambda: svc)

    resp = await h.api_autonudge_fire(_mk("lp-1", slot=_slot(running=True)))

    assert resp.status == 409
    assert _body(resp)["code"] == "session_busy"
    assert svc.fired == [], "a refused press must not fire"
    assert sel_mock.log_tool_invocation.call_count == 1
    kwargs = sel_mock.log_tool_invocation.call_args.kwargs
    assert kwargs["outcome"] == "denied"
    assert kwargs["session_key"] == "chat-1-111"
    assert kwargs["metadata"]["error"] == "session_busy"


@pytest.mark.asyncio
async def test_a_press_that_cannot_be_audited_does_not_fire(
    monkeypatch, sel_mock: MagicMock
) -> None:
    """AUDIT-OR-DENY: no record, no turn. The load-bearing half of the gate.

    A delivered cycle spends a model turn with nobody watching, so "which press
    started this turn, and when" is the only evidence it happened. The default
    SEL path merely ENQUEUES, and on the event loop an enqueue failure drops the
    event with a warning -- so auditing before the fire is not enough on its own.
    ``critical=True`` is what makes it a gate rather than a hope, and this test
    fails if the pre-fire write is ever downgraded to best-effort.

    Modelled as an ASYMMETRIC sink, exactly as the repository's other
    audit-or-deny tests do: the queued (non-critical) refusal writes succeed and
    only the critical one raises. A stub that failed every write would pass even
    against a route that never asked for durability.
    """
    loop = NudgeLoop(id="lp-1", slot_key="chat-1-111", message="check", idle_secs=300)
    svc = _FakeSvc([loop])
    monkeypatch.setattr(h, "_autonudge_get", lambda: svc)

    def _log(**kw: Any) -> None:
        if kw.get("critical"):
            raise OSError("audit sink is unwritable")

    sel_mock.log_tool_invocation.side_effect = _log

    resp = await h.api_autonudge_fire(_mk("lp-1", slot=_slot()))

    assert resp.status == 503
    assert _body(resp)["code"] == "audit_unavailable"
    # The whole point: the loop is exactly as the operator left it.
    assert svc.fired == [], "fired despite an unwritable audit sink"


@pytest.mark.asyncio
async def test_the_pre_fire_audit_is_critical_and_lands_before_the_fire(
    monkeypatch, sel_mock: MagicMock
) -> None:
    """Ordering AND durability, asserted together rather than assumed.

    Two separate ways this could be wrong, so both are pinned: a critical write
    placed AFTER ``fire_now`` would satisfy "is critical" while still arming the
    timer first, and a correctly-placed write that is not critical would satisfy
    "is first" while dropping the record on a full disk. The ``order`` list
    interleaves the audit calls with the fire, so the assertion reads the real
    sequence instead of trusting the source layout.
    """
    loop = NudgeLoop(id="lp-1", slot_key="chat-1-111", message="check", idle_secs=300)
    order: list[str] = []

    class _Svc(_FakeSvc):
        async def fire_now(self, loop_id: str):  # type: ignore[override]
            order.append("fire")
            return await super().fire_now(loop_id)

    svc = _Svc([loop])
    monkeypatch.setattr(h, "_autonudge_get", lambda: svc)
    sel_mock.log_tool_invocation.side_effect = lambda **kw: order.append(
        f"audit:{kw['outcome']}:critical={bool(kw.get('critical'))}"
    )

    resp = await h.api_autonudge_fire(_mk("lp-1", slot=_slot()))

    assert resp.status == 200
    assert order == [
        "audit:invoked:critical=True",
        "fire",
        "audit:success:critical=False",
    ], order


@pytest.mark.asyncio
async def test_fire_now_never_suspends_which_is_what_makes_it_race_free(svc_base_dir) -> None:
    """THE invariant. Five race classes were closed by deleting the await, not by
    guarding it, so this asserts the absence directly.

    A coroutine with no ``await`` inside completes on its FIRST ``send`` -- it
    raises ``StopIteration`` carrying the return value rather than yielding a
    future to the loop. So this is a mechanical check that no suspension point
    exists between reading the loop and arming it, which is precisely why a
    concurrent ``remove``, a cancelled caller, a countdown entering ``_firing``,
    a quiet-tick reschedule and a half-committed deadline are all impossible
    here rather than merely handled.

    Written against the raw coroutine on purpose: ``await svc.fire_now(...)``
    would pass whether or not it suspends, so it cannot distinguish the two.
    Anyone reintroducing an ``await`` in this method fails this test with a
    message that says what it costs.
    """
    svc, loop = _svc_with_loop(svc_base_dir)
    coro = svc.fire_now(loop.id)
    try:
        coro.send(None)
    except StopIteration as done:
        result, error, status = done.value
        assert (error, status) == ("", 200)
        assert result is loop
    else:
        coro.close()
        raise AssertionError(
            "fire_now suspended. Any await between the guards and the arm reopens "
            "the remove race, the cancellation window, the mid-persist _firing "
            "window and the quiet-tick deadline overwrite -- none of which can be "
            "closed here, because those writers take no lock."
        )
    svc.stop()
