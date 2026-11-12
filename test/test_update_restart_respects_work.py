"""An update never restarts the gateway under running work.

Three gaps in one place:

* the dashboard restart (``updates_apply._restart_gateway``) saved slots, called
  ``close_all()`` and exec'd with none of the pause, drain and fence the
  scheduler takes, so any caller could cut a turn mid-reply;
* the idle census reads an open chat between turns as idle, so a person who typed
  two minutes ago was restarted under;
* ``POST /api/update`` ran with no idle gate at all.

``kiro_crew.quiescence`` is the one predicate the scheduler's voluntary apply,
the dashboard update and the restart all read. Every clock here is injected.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from dashboard_updates_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _isolated_module_state,
    _request,
)
from slack_gateway_helpers import _make_orchestrator, _mock_dashboard_state

from kiro_crew import platform_compat, quiescence, restart_admission
from kiro_crew.dashboard import chat as dashboard_chat
from kiro_crew.dashboard.handlers import updates, updates_apply
from kiro_crew.slack import gateway as gw


class _Sessions:
    """The slice of ``SessionManager`` the update paths drive, recording its order."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.closing = False
        self.fenced = False
        self.queued = 0
        self.inbound_callback_count = 0
        #: Callback claims by owner task, and the tasks that own a running turn.
        self.claims: set[object] = set()
        self.turn_owners: list[object] = []

    @property
    def closed(self) -> bool:
        return "close_all" in self.events

    async def pause_turn_admission_for_update(self) -> bool:
        self.events.append("pause")
        self.closing = True
        return True

    def fence_update_restart(self) -> bool:
        self.events.append("fence")
        self.fenced = True
        return True

    async def resume_turn_admission_after_update(self) -> bool:
        self.events.append("resume")
        self.closing = False
        self.fenced = False
        return True

    def _allocation_boundary(self) -> SimpleNamespace:
        sessions = {"k": SimpleNamespace(queue=[object()] * self.queued, turn_owner=None)}
        for i, owner in enumerate(self.turn_owners):
            sessions[f"t{i}"] = SimpleNamespace(queue=[], turn_owner=owner)
        return SimpleNamespace(_sessions=sessions, _inbound_callback_reservations=self.claims)

    def active_providers(self) -> list:
        return []

    async def close_all(self, *_a, **_k) -> None:
        self.events.append("close_all")


class _Gateway:
    """A real orchestrator on a recording session manager and a dashboard state."""

    def __init__(self, monkeypatch) -> None:
        self.sessions = _Sessions()
        self.progress: list[tuple[str, str]] = []
        self.exec = Mock()
        self.state = SimpleNamespace(
            _slots={},
            _gateway_restart_in_progress=False,
            _gateway_restart_task=None,
            _background_tasks=set(),
            owner_id="",
            push_update_progress=lambda step, detail="": self.progress.append((step, detail)),
            push_refresh=lambda *_a, **_k: None,
            sessions=self.sessions,
        )
        orch = _make_orchestrator()
        orch.sessions = self.sessions
        orch.dashboard_state = self.state
        orch._schedule_inbound_replay = Mock()
        self.orch = orch
        restart_admission.wire(self.state, orch)
        monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
        monkeypatch.setattr(platform_compat, "execv_target_available", lambda _exe: True)
        monkeypatch.setattr(updates, "flush_breadcrumb_writes", lambda _timeout: None)
        monkeypatch.setattr(updates, "reexec_python_module", self.exec)
        monkeypatch.setattr(dashboard_chat, "save_all_slots_to_history", lambda _state: None)
        monkeypatch.setattr(
            "kiro_crew.gateway_shutdown_budget.DASHBOARD_TURN_UNWIND_SECS", 0.1, raising=False
        )

    async def restart(self, **kwargs) -> bool:
        return await asyncio.wait_for(
            updates._restart_gateway(self.state, resolver=lambda: "/python", **kwargs), 30
        )

    def running_turn(self) -> asyncio.Future:
        turn: asyncio.Future = asyncio.get_running_loop().create_future()
        self.orch._session_tasks["chat"] = turn
        return turn


@pytest.fixture
def gateway(monkeypatch):
    return _Gateway(monkeypatch)


def _refused_detail(gw: _Gateway) -> str:
    step, detail = gw.progress[-1]
    assert step == "error", gw.progress
    return detail


# -- the restart path takes the scheduler's pause, drain and fence ---------------------


@pytest.mark.asyncio
async def test_a_restart_with_a_turn_in_flight_is_refused_and_admission_reopens(gateway):
    gateway.running_turn()

    assert await gateway.restart() is False

    assert not gateway.sessions.closed, "close_all() cut a turn that was mid-reply"
    gateway.exec.assert_not_called()
    assert gateway.sessions.events == ["pause", "resume"]
    assert gateway.sessions.closing is False
    assert "turns 1" in _refused_detail(gateway)
    assert gateway.state._gateway_restart_in_progress is False


@pytest.mark.asyncio
async def test_an_idle_restart_pauses_drains_fences_then_closes(gateway):
    assert await gateway.restart() is True

    assert gateway.sessions.events == ["pause", "fence", "close_all"]
    gateway.exec.assert_called_once()


@pytest.mark.asyncio
async def test_a_forced_restart_runs_under_a_turn_but_still_pauses_and_fences(gateway):
    gateway.running_turn()

    assert await gateway.restart(force=True) is True

    assert gateway.sessions.events == ["pause", "fence", "close_all"]
    gateway.exec.assert_called_once()


def _live_chat_slot(gateway: _Gateway) -> asyncio.Task:
    """A dashboard chat slot whose REAL turn task is running, as a live turn is."""
    task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
    gateway.state._slots["s1"] = SimpleNamespace(task=task, _queue=[], _approval_futures={})
    return task


@pytest.mark.asyncio
async def test_a_forced_restart_does_not_wait_on_a_live_chat_turn_task(gateway):
    # The real drain, not a stub: a confirmed restart under a running chat turn
    # must not wait on the turn's own task, which ended "callbacks_not_drained".
    task = _live_chat_slot(gateway)
    gateway.orch._UPDATE_DRAIN_TIMEOUT_SECS = 0.2
    try:
        assert await gateway.restart(force=True) is True
    finally:
        task.cancel()

    assert gateway.sessions.events == ["pause", "fence", "close_all"]
    gateway.exec.assert_called_once()


@pytest.mark.asyncio
async def test_the_drain_waits_on_turn_tasks_unless_told_not_to(gateway):
    task = _live_chat_slot(gateway)
    try:
        assert await gateway.orch._drain_update_callback_work(timeout=0) is False
        assert (
            await gateway.orch._drain_update_callback_work(timeout=0, include_turns=False) is True
        )
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_a_forced_restart_still_waits_on_an_inbound_callback_reservation(gateway):
    gateway.sessions.inbound_callback_count = 1
    gateway.orch._UPDATE_DRAIN_TIMEOUT_SECS = 0.1

    assert await gateway.restart(force=True) is False

    assert not gateway.sessions.closed
    assert "did not settle" in _refused_detail(gateway)


@pytest.mark.asyncio
async def test_a_forced_restart_does_not_wait_on_a_claim_held_by_a_running_turn(gateway):
    """A messaging turn holds its inbound-callback claim for the whole turn.

    ``close_all`` cuts that turn and so releases the claim; a confirmed restart that
    waited for the claim first would time out on exactly the turn it is about to cut.
    """
    from kiro_crew.session_allocation import _CallbackToken

    turn = asyncio.ensure_future(asyncio.Event().wait())
    try:
        gateway.sessions.inbound_callback_count = 1
        gateway.sessions.claims.add(_CallbackToken(turn))
        gateway.sessions.turn_owners.append(turn)
        gateway.orch._UPDATE_DRAIN_TIMEOUT_SECS = 0.1

        assert await gateway.restart(force=True) is True

        assert gateway.sessions.closed
    finally:
        turn.cancel()


@pytest.mark.asyncio
async def test_a_forced_restart_still_waits_on_a_claim_no_running_turn_owns(gateway):
    """A claim whose task owns no turn is still admitting a message: that is awaited."""
    from kiro_crew.session_allocation import _CallbackToken

    admitting = asyncio.ensure_future(asyncio.Event().wait())
    try:
        gateway.sessions.inbound_callback_count = 1
        gateway.sessions.claims.add(_CallbackToken(admitting))
        gateway.orch._UPDATE_DRAIN_TIMEOUT_SECS = 0.1

        assert await gateway.restart(force=True) is False

        assert not gateway.sessions.closed
    finally:
        admitting.cancel()


@pytest.mark.asyncio
async def test_the_restart_endpoint_refuses_before_the_200_when_admission_is_held(
    gateway, monkeypatch
):
    restarted = _restart_endpoint(gateway, monkeypatch)
    gateway.orch._update_pause_holder = "auto_update"

    resp = await updates.api_gateway_restart(_wired_request(gateway))

    assert resp.status == 409
    assert _json(resp)["code"] == restart_admission.REASON_PAUSE_HELD
    restarted.assert_not_called()


@pytest.mark.asyncio
async def test_a_queued_channel_message_refuses_the_restart(gateway):
    gateway.sessions.queued = 2

    assert await gateway.restart() is False

    assert not gateway.sessions.closed
    assert "queued messages 2" in _refused_detail(gateway)


@pytest.mark.asyncio
async def test_a_queued_dashboard_send_refuses_the_restart(gateway):
    gateway.state._slots["s1"] = SimpleNamespace(task=None, _queue=[{"id": "q1"}])

    assert await gateway.restart() is False

    assert "queued messages 1" in _refused_detail(gateway)


@pytest.mark.asyncio
async def test_a_pending_approval_card_refuses_the_restart(gateway):
    waiting = asyncio.get_running_loop().create_future()
    gateway.state._slots["s1"] = SimpleNamespace(
        task=None, _queue=[], _approval_futures={"a": waiting}
    )

    assert await gateway.restart() is False

    assert not gateway.sessions.closed
    assert "pending approvals 1" in _refused_detail(gateway)


@pytest.mark.asyncio
async def test_callbacks_that_do_not_drain_defer_the_restart(gateway):
    gateway.orch._drain_update_callback_work = AsyncMock(return_value=False)

    assert await gateway.restart() is False

    assert not gateway.sessions.closed
    assert gateway.sessions.events == ["pause", "resume"]
    assert "did not settle" in _refused_detail(gateway)


@pytest.mark.asyncio
async def test_a_pause_another_update_step_holds_is_not_taken_or_released(gateway):
    gateway.orch._update_pause_holder = "auto_update"

    assert await gateway.restart() is False

    assert gateway.sessions.events == []
    assert gateway.orch._update_pause_holder == "auto_update"


@pytest.mark.asyncio
async def test_a_gateway_with_no_session_manager_still_refuses_under_work(gateway):
    gateway.orch.sessions = None
    gateway.running_turn()

    assert (await restart_admission.admit_restart(gateway.orch)).reason == "work_in_flight"
    assert (await restart_admission.admit_restart(gateway.orch, force=True)).admitted is True


@pytest.mark.asyncio
async def test_a_restart_cancelled_before_it_commits_reopens_admission(gateway, monkeypatch):
    def _cancelled(_state):
        raise asyncio.CancelledError

    monkeypatch.setattr(dashboard_chat, "save_all_slots_to_history", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await gateway.restart()

    assert gateway.sessions.events == ["pause", "fence", "resume"]
    assert gateway.sessions.closing is False
    assert gateway.state._gateway_restart_in_progress is False


# -- the shared predicate --------------------------------------------------------------


def _orchestrator(sessions: _Sessions | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        sessions=sessions or _Sessions(),
        dashboard_state=None,
        _in_flight_work_counts=lambda: (0, 0),
    )


def test_a_session_with_input_two_minutes_ago_is_not_quiescent():
    orch = _orchestrator()
    quiescence.note_user_input(orch.sessions, now=1000.0)

    census = quiescence.assess(orch, now=1120.0)

    assert census.quiescent is False
    assert census.busy == 0
    assert census.quiet_remaining_secs == pytest.approx(quiescence.QUIET_WINDOW_SECS - 120.0)
    assert quiescence.is_quiescent(orch, now=1120.0) is False


def test_quiet_for_the_whole_window_is_quiescent():
    orch = _orchestrator()
    quiescence.note_user_input(orch.sessions, now=1000.0)

    assert quiescence.is_quiescent(orch, now=1000.0 + quiescence.QUIET_WINDOW_SECS - 1) is False
    assert quiescence.is_quiescent(orch, now=1000.0 + quiescence.QUIET_WINDOW_SECS) is True


def test_nobody_having_typed_since_boot_is_quiescent():
    assert quiescence.is_quiescent(_orchestrator(), now=5.0) is True


def test_a_caller_that_is_the_person_drops_only_the_typing_window():
    orch = _orchestrator()
    quiescence.note_user_input(orch.sessions, now=1000.0)
    orch.sessions.queued = 1

    assert quiescence.is_quiescent(orch, quiet_window_secs=0.0, now=1001.0) is False
    orch.sessions.queued = 0
    after_settle = 1001.0 + quiescence.SETTLE_SECS
    assert quiescence.is_quiescent(orch, quiet_window_secs=0.0, now=after_settle) is True


def test_a_census_that_just_cleared_stays_unsettled_until_the_hysteresis_passes():
    orch = _orchestrator()
    orch._in_flight_work_counts = lambda: (1, 0)
    assert quiescence.assess(orch, now=100.0).quiescent is False
    orch._in_flight_work_counts = lambda: (0, 0)

    cleared = quiescence.assess(orch, now=100.0 + quiescence.SETTLE_SECS - 1)

    assert cleared.busy == 0
    assert cleared.settle_remaining_secs == pytest.approx(1.0)
    assert cleared.quiescent is False
    assert quiescence.is_quiescent(orch, now=100.0 + quiescence.SETTLE_SECS) is True


def test_two_gateways_do_not_share_a_typing_clock():
    typed, other = _orchestrator(), _orchestrator()
    quiescence.note_user_input(typed.sessions, now=1000.0)

    assert quiescence.is_quiescent(typed, now=1001.0) is False
    assert quiescence.is_quiescent(other, now=1001.0) is True


def test_a_session_manager_double_never_breaks_the_ledger():
    quiescence.note_user_input(None)
    quiescence.note_user_input(object())


@pytest.mark.asyncio
async def test_channel_admission_records_the_message_even_when_it_refuses():
    from kiro_crew.messaging import dispatch_admission

    sessions = _Sessions()
    sessions.reserve_inbound_callback = lambda: None
    sessions.update_restart_fenced = True

    admitted = await dispatch_admission.admit_inbound_callback(
        sessions, channel_type="slack", route=None, restricted=True
    )

    assert admitted is False
    assert sessions._user_input_ledger.last_input_at is not None


@pytest.mark.asyncio
async def test_a_held_callback_scope_records_the_message():
    from kiro_crew.messaging import dispatch_admission

    sessions = _Sessions()
    sessions.reserve_inbound_callback = lambda: SimpleNamespace(release=lambda: None)

    async with dispatch_admission.hold_inbound_callback(
        sessions, channel_type="slack", route=None
    ) as admitted:
        assert admitted is True

    assert sessions._user_input_ledger.last_input_at is not None


@pytest.mark.asyncio
async def test_a_dashboard_chat_send_records_the_message():
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.chat_handlers import api_chat

    sessions = _Sessions()
    app = web.Application()
    app["state"] = SimpleNamespace(sessions=sessions)
    app.router.add_post("/api/chat", api_chat)
    async with TestClient(TestServer(app)) as client:
        # Rejected for its type, which is decided after the message is recorded.
        resp = await client.post("/api/chat", json={"message": 5})
        assert resp.status == 400
    assert sessions._user_input_ledger.last_input_at is not None


# -- the scheduler's apply reads the same predicate ------------------------------------


async def _prepare(gw: _Gateway, *, mandatory: bool) -> bool:
    return await gw.orch._prepare_auto_update_apply(mandatory=mandatory)


# -- POST /api/update waits for quiescence ---------------------------------------------


def _wired_request(gateway: _Gateway, body: object = None):
    req = _request(body)
    req.can_read_body = body is not None
    req.app["state"] = gateway.state
    return req


def _json(resp) -> dict:
    return json.loads(resp.body.decode())


# -- POST /api/restart -----------------------------------------------------------------


def _restart_endpoint(gateway: _Gateway, monkeypatch) -> AsyncMock:
    restarted = AsyncMock(return_value=True)
    monkeypatch.setattr(updates_apply, "_restart_gateway", restarted)
    monkeypatch.setattr(
        updates_apply, "_resolve_restart_target", AsyncMock(return_value=(None, "/python", None))
    )
    return restarted


@pytest.mark.asyncio
async def test_a_restart_click_is_the_persons_own_act_and_goes_ahead_under_work(
    gateway, monkeypatch
):
    gateway.running_turn()
    restarted = _restart_endpoint(gateway, monkeypatch)

    resp = await updates.api_gateway_restart(_wired_request(gateway))
    await asyncio.gather(*gateway.state._background_tasks)

    assert resp.status == 200
    assert restarted.await_args.kwargs == {"force": True}


@pytest.mark.asyncio
async def test_the_restart_endpoint_refuses_under_work_when_asked_not_to_interrupt(
    gateway, monkeypatch
):
    gateway.running_turn()
    restarted = _restart_endpoint(gateway, monkeypatch)

    resp = await updates.api_gateway_restart(_wired_request(gateway, {"force": False}))

    assert resp.status == 409
    body = _json(resp)
    assert body["code"] == "work_in_flight"
    assert body["turns"] == 1
    assert body["force_available"] is True
    restarted.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_restart_endpoint_ignores_the_typing_window(gateway, monkeypatch):
    quiescence.note_user_input(gateway.sessions)
    restarted = _restart_endpoint(gateway, monkeypatch)

    resp = await updates.api_gateway_restart(_wired_request(gateway, {"force": False}))
    await asyncio.gather(*gateway.state._background_tasks)

    assert resp.status == 200
    assert restarted.await_args.kwargs == {"force": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"force": "no"}, {"force": 0}, {"other": 1}])
async def test_the_restart_endpoint_rejects_a_malformed_confirmation(gateway, monkeypatch, body):
    restarted = _restart_endpoint(gateway, monkeypatch)

    resp = await updates.api_gateway_restart(_wired_request(gateway, body))

    assert resp.status == 400
    restarted.assert_not_awaited()


# -- the confirmation reaches the restart, never more ---------------------------------


def _policy_install(gateway: _Gateway, monkeypatch, *, work_arrives: bool) -> None:
    """A policy update whose install finishes, with a turn starting while it ran."""
    from kiro_crew.platform import update_provider

    monkeypatch.setattr(
        update_provider, "resolve_provider", lambda: SimpleNamespace(can_apply=lambda: True)
    )
    monkeypatch.setattr(updates, "_snapshot_memory_or_refuse", AsyncMock(return_value=None))

    async def _installed(_apply) -> bool:
        if work_arrives:
            gateway.running_turn()
        return True

    monkeypatch.setattr(updates, "_run_tracked_policy_apply", _installed)


# -- a typed approval prompt and a source that cannot answer ---------------------------


@pytest.mark.asyncio
async def test_a_typed_approval_prompt_refuses_the_restart(gateway, monkeypatch):
    """The reply-in-chat approval is a different source from a dashboard card."""
    from kiro_crew.messaging import approval

    waiting = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(approval, "_PENDING", {"k": SimpleNamespace(future=waiting)})

    assert quiescence.assess(gateway.orch).pending_approvals == 1
    assert await gateway.restart() is False

    assert not gateway.sessions.closed
    assert "pending approvals 1" in _refused_detail(gateway)


@pytest.mark.asyncio
async def test_a_typed_approval_prompt_that_was_answered_no_longer_counts(gateway, monkeypatch):
    from kiro_crew.messaging import approval

    answered = asyncio.get_running_loop().create_future()
    answered.set_result("approve")
    monkeypatch.setattr(approval, "_PENDING", {"k": SimpleNamespace(future=answered)})

    assert quiescence.assess(gateway.orch).pending_approvals == 0


def test_a_typed_approval_source_that_raises_counts_as_busy(monkeypatch):
    from kiro_crew.messaging import approval

    monkeypatch.setattr(approval, "pending_count", Mock(side_effect=RuntimeError("boom")))

    assert quiescence.pending_approval_count(_orchestrator()) == 1


def test_a_channel_queue_that_cannot_be_read_counts_as_one_waiting_message():
    sessions = _Sessions()
    sessions._allocation_boundary = Mock(side_effect=RuntimeError("boom"))

    assert quiescence.queued_message_count(_orchestrator(sessions)) == 1


def test_a_slot_queue_that_cannot_be_read_counts_as_one_waiting_message():
    class _Slot:
        @property
        def _queue(self):
            raise RuntimeError("boom")

    orch = _orchestrator()
    orch.dashboard_state = SimpleNamespace(_slots={"s": _Slot()})

    assert quiescence.queued_message_count(orch) == 1


def test_a_slot_approval_probe_that_raises_counts_as_busy(monkeypatch):
    from kiro_crew.dashboard import slot_retention

    monkeypatch.setattr(slot_retention, "has_pending_approval", Mock(side_effect=RuntimeError))
    orch = _orchestrator()
    orch.dashboard_state = SimpleNamespace(_slots={"s": SimpleNamespace()})

    assert quiescence.pending_approval_count(orch) == 1


# -- the boot wires the real state -----------------------------------------------------


@pytest.mark.asyncio
async def test_the_boot_wires_the_restart_fence_and_census_onto_the_dashboard_state():
    """Unwired, the dashboard restart is a bare close_all() and Update never defers."""
    orch = _make_orchestrator()
    orch._cfg.dashboard.url = "http://127.0.0.1:5476"
    sessions = _Sessions()
    orch.sessions = sessions
    orch.cron_svc = MagicMock()
    orch.subagent_mgr = None
    orch.task_runner = None
    orch.slack = None
    orch._schedule_inbound_replay = Mock()
    runner = MagicMock()
    runner.addresses = [("127.0.0.1", 54321)]
    ds = _mock_dashboard_state()

    with patch.object(gw, "LessonStore", MagicMock()):
        with patch.object(gw, "start_dashboard", AsyncMock(return_value=(runner, ds))):
            await orch._init_dashboard()

    # The orchestrator's own census and fence, not a mock's auto-attributes.
    assert isinstance(ds._quiescence_probe(quiet_window_secs=0.0), quiescence.Quiescence)
    admission = await ds._restart_admission(force=False)
    assert isinstance(admission, restart_admission.RestartAdmission) and admission.admitted
    assert sessions.events == ["pause", "fence"]
    await ds._restart_release()
    assert sessions.events == ["pause", "fence", "resume"]


# -- the mandatory floor is never forced -----------------------------------------------
