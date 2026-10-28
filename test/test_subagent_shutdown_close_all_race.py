"""A run the shutdown's ``close_all`` ends while ``cancel_all`` still waits is left for the next start.

``_stop_services_and_sessions`` gathers ``SubagentManager.cancel_all()`` with
``SessionManager.close_all()``. While ``cancel_all`` is parked on an await before it
cancels the run tasks -- here the announce of a ``spawn_steer(mode="follow_up")`` message
the shutdown drops, which awaits the parent consumer ``_on_done`` -- the real
``close_all`` reaches a dedicated run first and ends it in one of three ways, each in an
arm of its own:

* ``drain_active_turns`` sends ``session/cancel``; kiro-cli acks and the prompt ends with
  stopReason ``cancelled`` (``_run_inner``'s tail);
* the ack does not come and ``_close_one`` shuts the provider down, killing the process
  under the prompt (``AcpProcessDied``, ``_run``'s general error arm);
* the run's session start is in flight when the manager begins closing, and the start is
  refused (``SessionClosingError``, the general error arm).

Under a dashboard tab each is left for the next start, as the cancel arm leaves a run the
shutdown cancels. An outcome the run reached itself (a failed stop, its deadline), a user
stop and a parent with no dashboard tab keep their tombstone and report.

The in-app restart (``_restart_gateway``) runs the real ``close_all`` with no
``cancel_all`` before it re-execs, so the manager's ``_shutting_down`` stays False and
the session manager's ``final_drain_started`` says the shutdown began; the same three
endings are left there too. So is a turn that ends on the session handle's synthesized
``error: cancel unacked`` stop, the drain's cancel left unanswered while the runtime
outlived it. A run that ends while an update pause closes the manager (it resumes, so
``final_drain_started`` stays False), and a run a reap is already ending, keep their
tombstone and report.

Seam: ``close_all`` is the real ``SessionManager.close_all``. Each run's provider is a
double of one dedicated kiro-cli session, registered in that manager's ``_sessions`` the
way ``get_or_create`` registers it: ``cancel`` stands for the ACP ``session/cancel`` round
trip and ``shutdown`` for the process kill (``AcpSessionHandle._died`` raising
``AcpProcessDied`` into the prompt when its queue is poisoned). A start the double holds
is refused once the real manager is closing, as ``get_or_create`` refuses a start that
spanned the shutdown's snapshot. The subagent manager's sessions facade reports the real
manager's ``admission_closed`` and ``final_drain_started``: in the gateway, the sessions it
holds are that manager.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from subagent_turn_resilience_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _close_subagent_managers,
    _mock_ctx_builder,
    _mock_sessions,
    _text_event,
)

from kiro_crew import session_surface
from kiro_crew import subagent_persistence as sp
from kiro_crew.acp.transport_errors import AcpProcessDied
from kiro_crew.providers.base import EVENT_COMPLETE
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import async_wait_until, await_until, finished

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_DASHBOARD_PARENT = "dashboard:chat-7"
_CHANNEL_PARENT = "slack:C0123ABCD:1700000000.000100"
_FOLLOW_UP_ANNOUNCE = "[follow_up of run "

#: How a turn answers the drain's ``session/cancel``: the stop reason its prompt then ends
#: with, or None for no ack within the drain's budget, after which ``_close_one`` kills
#: the process.
_CANCEL_ACKED = "cancelled"
_NO_ACK = None
#: No ack, and the kill does not reach the turn (a shared runtime's tenancy refuses it, or
#: it lands after the session handle's cancel grace): the handle ends the turn itself.
#: Spelled as the session handle sends it, so the test reads the wire value.
_CANCEL_UNACKED = "error: cancel unacked"
#: A turn that ended on a failed stop of its own as the drain reached it.
_FAILED_STOP = "refusal"

#: How the session manager closes while the run is in flight: the gateway shutdown's
#: gather of ``cancel_all`` and ``close_all``, the in-app restart's ``close_all`` with no
#: ``cancel_all``, or an update pause, which closes it with no final drain and resumes.
_GATEWAY_SHUTDOWN = "gateway-shutdown"
_IN_APP_RESTART = "in-app-restart"
_UPDATE_PAUSE = "update-pause"

_LEFT_FOR_THE_NEXT_START = {
    "tombstone": None,
    "left_for_next_start": True,
    "reported_to_on_done": False,
    "in_list_orphans": True,
    "next_start_announces": True,
}


def _reported_now(tombstone: str) -> dict:
    """The record of a run that keeps its *tombstone* and reports to its parent now."""
    return {
        "tombstone": tombstone,
        "left_for_next_start": False,
        "reported_to_on_done": True,
        "in_list_orphans": False,
        "next_start_announces": False,
    }


class _DedicatedFleet:
    """The real ``SessionManager`` that ``close_all`` runs on, plus one dedicated
    session double per run, registered in its ``_sessions`` at claim time."""

    def __init__(self, *, drain_answer: str | None) -> None:
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        self.smap = SessionManager(KiroCrewConfig())
        self.drain_answer = drain_answer
        # Set before a spawn: that run's session start stays in flight until the
        # manager begins closing, and is then refused.
        self.hold_next_start = False
        self.start_in_flight = asyncio.Event()
        self.providers: dict[str, AsyncMock] = {}
        self._streaming: dict[str, asyncio.Event] = {}
        self.ended_by: dict[str, str] = {}
        self.sessions = _mock_sessions(lambda *a, **k: None)
        self.sessions.get_or_create = AsyncMock(side_effect=self._get_or_create)
        self.sessions.get_provider = MagicMock(side_effect=self.providers.get)
        # Read live off the real manager, as the gateway's subagent manager reads the
        # manager it holds: closed from close_all's start or an update pause's, and
        # in its final drain only from close_all's.
        for state in ("admission_closed", "final_drain_started"):
            setattr(
                type(self.sessions),
                state,
                PropertyMock(side_effect=lambda state=state: getattr(self.smap, state)),
            )

    def streaming(self, session_key: str) -> asyncio.Event:
        return self._streaming.setdefault(session_key, asyncio.Event())

    async def _get_or_create(self, session_key: str, **_kwargs):
        from kiro_crew.session import _Session

        if self.hold_next_start:
            self.hold_next_start = False
            self.start_in_flight.set()
            await async_wait_until(lambda: self.smap._closing, describe=lambda: "close_all begun")
            self.ended_by[session_key] = "session start refused"
            raise SessionClosingError(
                "SessionManager began closing during provider startup; "
                "refusing to register a session behind the shutdown snapshot"
            )
        provider = self.providers.get(session_key)
        if provider is None:
            provider = self.providers[session_key] = self._dedicated_provider(session_key)
            self.smap._sessions[session_key] = _Session(provider=provider)  # type: ignore[arg-type]
        return provider, True, False

    def _dedicated_provider(self, session_key: str) -> AsyncMock:
        provider = AsyncMock()
        provider.start = AsyncMock()
        # The ACP surface identity.py reads (``client`` here): reclaim() is synchronous.
        provider.client.reclaim = MagicMock()
        provider.context_usage_pct = lambda: 0.0
        provider.context_window_tokens = lambda: 0
        provider.context_used_tokens = lambda: 0
        provider.mcp_session_report = MagicMock(return_value=None)
        turn: dict[str, asyncio.Queue] = {}
        streaming = self.streaming(session_key)

        def stream(_msg: str, *_a, **_kw):
            frames: asyncio.Queue = asyncio.Queue()
            turn["frames"] = frames

            async def _prompt():
                try:
                    yield _text_event("partial work ")
                    streaming.set()
                    while True:
                        frame = await frames.get()
                        if frame is None:
                            # AcpSessionHandle's dispatch loop reading the poison
                            # _mark_dead puts in its queue (session_handle_dispatch).
                            self.ended_by[session_key] = "process killed"
                            raise AcpProcessDied(
                                "Runtime process died during prompt — killed (graceful shutdown)"
                            )
                        self.ended_by[session_key] = f"turn ended: {frame.stop_reason}"
                        yield frame
                        if frame.kind == EVENT_COMPLETE:
                            return
                finally:
                    if turn.get("frames") is frames:
                        turn.pop("frames", None)

            return _prompt()

        async def cancel(*, wait_ack_timeout: float = 0.0) -> str:
            frames = turn.get("frames")
            if frames is None:
                return "no_turn"
            if self.drain_answer is None:
                # No ack within the drain's budget (drain_active_turns gives up and
                # proceeds to kill); returned at once instead of after the budget.
                return "timeout"
            frames.put_nowait(
                SimpleNamespace(
                    kind=EVENT_COMPLETE, stop_reason=self.drain_answer, runtime_global=False
                )
            )
            # For _CANCEL_UNACKED the frame stands for the session handle's own stop
            # once its cancel grace is spent: queued ahead of the kill's poison, it
            # ends the turn as when that kill never reaches it.
            return "timeout" if self.drain_answer == _CANCEL_UNACKED else "acked"

        async def shutdown() -> None:
            frames = turn.get("frames")
            if frames is not None:
                frames.put_nowait(None)

        provider.stream = MagicMock(side_effect=stream)
        provider.has_unfinished_turn = MagicMock(side_effect=lambda: "frames" in turn)
        provider.cancel = AsyncMock(side_effect=cancel)
        provider.shutdown = AsyncMock(side_effect=shutdown)
        return provider


def _parked_announce_consumer() -> tuple[AsyncMock, asyncio.Event, asyncio.Event]:
    """``_on_done`` as a recorder that parks only the dropped follow-up's announce.

    Returns the consumer, the event it sets once ``cancel_all`` is parked in that
    announce, and the gate that releases it.
    """
    announce_parked = asyncio.Event()
    gate = asyncio.Event()

    async def consumer(done: SubagentInfo) -> None:
        if done.task.startswith(_FOLLOW_UP_ANNOUNCE):
            announce_parked.set()
            await gate.wait()

    return AsyncMock(side_effect=consumer), announce_parked, gate


def _reported(on_done: AsyncMock, info: SubagentInfo) -> bool:
    return any(call.args and call.args[0] is info for call in on_done.await_args_list)


async def _outcome(info: SubagentInfo, on_done: AsyncMock) -> dict:
    tombstone = sp.read_tombstone(info.id)
    in_orphans = info.id in [state["id"] for state in sp.list_orphans()]
    notify = AsyncMock(return_value=True)
    next_start = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_notify=notify)
    next_start._is_pid_alive = MagicMock(return_value=False)
    with patch("kiro_crew.subagent.sel"):
        await next_start._reconcile_orphans()
    announced = any(
        call.args[:1] == (info.parent_session_key,) and f"Agent `{info.id}`" in call.args[1]
        for call in notify.await_args_list
    )
    return {
        "tombstone": tombstone["cause"] if tombstone else None,
        "left_for_next_start": info._left_for_next_start,
        "reported_to_on_done": _reported(on_done, info),
        "in_list_orphans": in_orphans,
        "next_start_announces": announced,
    }


async def _shut_down(
    *,
    drain_answer: str | None,
    follow_up_on: str | None,
    parent: str = _DASHBOARD_PARENT,
    user_stopped: bool = False,
    victim_starting: bool = False,
    closing: str = _GATEWAY_SHUTDOWN,
    reap_in_flight: bool = False,
) -> tuple[SubagentInfo, AsyncMock, _DedicatedFleet, dict]:
    """One run under *parent* is in flight when the session manager closes, as *closing* says.

    ``follow_up_on`` names the run holding a queued follow-up when the shutdown
    starts: ``"the-run"`` (the victim itself), ``"a-sibling"`` (another run under
    the same parent), or None (nothing in flight: the control). The consumer parks
    the dropped follow-up's announce on a gate, released once the victim's task has
    ended, so ``cancel_all`` is held exactly where it awaits that announce. The
    victim streams, or with ``victim_starting`` its session start is still in flight.
    With ``reap_in_flight`` a deadline reap has begun ending the victim and has not
    reached its own teardown's end when the manager closes.
    """
    assert closing == _GATEWAY_SHUTDOWN or follow_up_on is None, "only cancel_all parks"
    fleet = _DedicatedFleet(drain_answer=drain_answer)
    on_done, announce_parked, gate = _parked_announce_consumer()
    mgr = SubagentManager(sessions=fleet.sessions, ctx_builder=_mock_ctx_builder(), on_done=on_done)
    # The dedicated-process arm: the run's session lives in SessionManager._sessions.
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    observed: dict = {}
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        fleet.hold_next_start = victim_starting
        victim = mgr.spawn("job at shutdown", parent_session_key=parent)
        assert victim is not None
        if victim_starting:
            await await_until(fleet.start_in_flight, what="the victim's session start")
        else:
            await await_until(fleet.streaming(f"subagent:{victim.id}"), what="the victim's prompt")
        holder = None
        if follow_up_on == "the-run":
            holder = victim
        elif follow_up_on == "a-sibling":
            holder = mgr.spawn("sibling job", parent_session_key=parent)
            assert holder is not None
            await await_until(fleet.streaming(f"subagent:{holder.id}"), what="the sibling's prompt")
        if holder is not None:
            ok, detail = await mgr.follow_up_run(holder.id, "then summarise what you found")
            assert ok, detail
        # A Stop the user pressed before the shutdown reached the run.
        victim.user_stopped = user_stopped
        if reap_in_flight:
            # What _reap_once marks before its first await.
            victim._reap_started = True
            victim._reap_reason = "reaped"
            victim._stop_origin = "reaped after 900s (deadline)"
        victim_task = mgr._tasks[victim.id]

        async def release_the_announce() -> None:
            if holder is None:
                return
            await await_until(announce_parked, what="cancel_all parked on the follow-up announce")
            observed["victim_running_when_cancel_all_parked"] = not victim_task.done()
            await async_wait_until(
                victim_task.done,
                describe=lambda: f"victim done={victim.done} error={victim.error!r}",
            )
            gate.set()

        observed["final_drain_started_before"] = fleet.sessions.final_drain_started
        if closing == _GATEWAY_SHUTDOWN:
            # _stop_services_and_sessions: cancel_all() and close_all() in one gather.
            steps = [mgr.cancel_all(), fleet.smap.close_all()]
        elif closing == _IN_APP_RESTART:
            # _restart_gateway: close_all() and no cancel_all() before the exec.
            steps = [fleet.smap.close_all()]
        else:
            # An update apply: the manager closes to new turns, and resumes after.
            steps = [fleet.smap.pause_turn_admission_for_update()]
        observed["closing_results"] = await finished(
            asyncio.gather(*steps, release_the_announce(), return_exceptions=True),
            "the shutdown gather",
        )
        await async_wait_until(
            victim_task.done, describe=lambda: f"victim done={victim.done} error={victim.error!r}"
        )
        observed["final_drain_started_after"] = fleet.sessions.final_drain_started
    assert victim.done is True
    observed["shutting_down"] = mgr._shutting_down
    observed["victim_error"] = victim.error
    observed["victim_ended_by"] = fleet.ended_by.get(f"subagent:{victim.id}", "cancel_all")
    return victim, on_done, fleet, observed


@pytest.mark.asyncio
async def test_with_nothing_parked_cancel_all_cancels_the_run_first_and_leaves_it():
    victim, on_done, fleet, observed = await _shut_down(
        drain_answer=_CANCEL_ACKED, follow_up_on=None
    )

    # close_all's drain reached the run's session as well (session/cancel sent and
    # acked), but cancel_all requested the task cancel before its first await, so
    # the run unwound through the CancelledError arm.
    assert fleet.providers[f"subagent:{victim.id}"].cancel.await_count == 1
    assert victim.error == "cancelled"
    assert observed["victim_ended_by"] == "cancel_all"
    assert await _outcome(victim, on_done) == _LEFT_FOR_THE_NEXT_START


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("drain_answer", "ended_by"),
    [
        pytest.param(_CANCEL_ACKED, "turn ended: cancelled", id="session-cancel-acked"),
        pytest.param(_NO_ACK, "process killed", id="no-ack-process-killed"),
    ],
)
@pytest.mark.parametrize("follow_up_on", ["the-run", "a-sibling"])
async def test_a_run_close_all_ends_while_cancel_all_waits_is_left_for_the_next_start(
    drain_answer, ended_by, follow_up_on
):
    victim, on_done, _fleet, observed = await _shut_down(
        drain_answer=drain_answer, follow_up_on=follow_up_on
    )

    assert observed["victim_running_when_cancel_all_parked"] is True
    assert observed["victim_ended_by"] == ended_by
    outcome = await _outcome(victim, on_done)
    assert (
        outcome == _LEFT_FOR_THE_NEXT_START
    ), f"error={observed['victim_error']!r}; outcome={outcome!r}"


@pytest.mark.asyncio
async def test_a_run_whose_session_start_the_closing_manager_refuses_is_left_for_the_next_start():
    victim, on_done, _fleet, observed = await _shut_down(
        drain_answer=_CANCEL_ACKED, follow_up_on="a-sibling", victim_starting=True
    )

    assert observed["victim_running_when_cancel_all_parked"] is True
    assert observed["victim_ended_by"] == "session start refused"
    assert "SessionClosingError" in victim.error
    assert await _outcome(victim, on_done) == _LEFT_FOR_THE_NEXT_START


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("drain_answer", "victim_starting", "ended_by"),
    [
        pytest.param(_CANCEL_ACKED, False, "turn ended: cancelled", id="session-cancel-acked"),
        pytest.param(_NO_ACK, False, "process killed", id="no-ack-process-killed"),
        pytest.param(_CANCEL_ACKED, True, "session start refused", id="session-start-refused"),
    ],
)
async def test_a_run_the_in_app_restart_ends_is_left_for_the_next_start(
    drain_answer, victim_starting, ended_by
):
    # _restart_gateway runs close_all() and never cancel_all(): the manager is not
    # shutting down, and the session manager's final drain is what says the run's
    # tab dies with this process.
    victim, on_done, _fleet, observed = await _shut_down(
        drain_answer=drain_answer,
        follow_up_on=None,
        victim_starting=victim_starting,
        closing=_IN_APP_RESTART,
    )

    assert observed["shutting_down"] is False
    assert observed["final_drain_started_before"] is False
    assert observed["final_drain_started_after"] is True
    assert observed["victim_ended_by"] == ended_by
    outcome = await _outcome(victim, on_done)
    assert (
        outcome == _LEFT_FOR_THE_NEXT_START
    ), f"error={observed['victim_error']!r}; outcome={outcome!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("closing", "follow_up_on"),
    [
        pytest.param(_GATEWAY_SHUTDOWN, "the-run", id="gateway-shutdown"),
        pytest.param(_IN_APP_RESTART, None, id="in-app-restart"),
    ],
)
async def test_a_turn_the_drain_cancel_leaves_unacked_is_left_for_the_next_start(
    closing, follow_up_on
):
    # The runtime outlived the drain's unanswered cancel, so the session handle ended
    # the turn on its synthesized stop: the shutdown's cancel, not a fault of the run.
    victim, on_done, _fleet, observed = await _shut_down(
        drain_answer=_CANCEL_UNACKED, follow_up_on=follow_up_on, closing=closing
    )

    assert observed["victim_ended_by"] == f"turn ended: {_CANCEL_UNACKED}"
    outcome = await _outcome(victim, on_done)
    assert (
        outcome == _LEFT_FOR_THE_NEXT_START
    ), f"error={observed['victim_error']!r}; outcome={outcome!r}"


@pytest.mark.asyncio
async def test_a_run_whose_start_an_update_pause_refuses_keeps_its_tombstone_and_report():
    # An update apply closes the manager to new turns and resumes it after: the same
    # refusal, with no final drain, so the process and the tab outlive it.
    victim, on_done, fleet, observed = await _shut_down(
        drain_answer=_CANCEL_ACKED,
        follow_up_on=None,
        victim_starting=True,
        closing=_UPDATE_PAUSE,
    )

    assert observed["closing_results"][0] is True, "the update pause was not granted"
    assert fleet.smap.admission_closed is True
    assert observed["final_drain_started_after"] is False
    assert observed["victim_ended_by"] == "session start refused"
    assert await _outcome(victim, on_done) == _reported_now("error")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("closing", "victim_starting", "ended_by", "tombstone"),
    [
        pytest.param(
            _IN_APP_RESTART,
            False,
            "turn ended: cancelled",
            "cancelled",
            id="in-app-restart-session-cancel-acked",
        ),
        pytest.param(
            _IN_APP_RESTART,
            True,
            "session start refused",
            "error",
            id="in-app-restart-session-start-refused",
        ),
        pytest.param(
            _GATEWAY_SHUTDOWN, False, "cancel_all", "cancelled", id="gateway-shutdown-cancel-arm"
        ),
    ],
)
async def test_a_run_a_reap_is_ending_keeps_its_tombstone_and_report_when_close_all_ends_it(
    closing, victim_starting, ended_by, tombstone
):
    # A reap already ending the run owns the ending, so the run is reported now; if the
    # reap's kill fails, it re-writes a tombstone the run has. The in-app restart cancels
    # no reap, so close_all can end the run first; the gateway shutdown's cancel_all can
    # cancel the run's task before that reap has marked it reaped.
    victim, on_done, _fleet, observed = await _shut_down(
        drain_answer=_CANCEL_ACKED,
        follow_up_on=None,
        victim_starting=victim_starting,
        closing=closing,
        reap_in_flight=True,
    )

    assert observed["victim_ended_by"] == ended_by
    assert await _outcome(victim, on_done) == _reported_now(tombstone)


@pytest.mark.asyncio
async def test_a_failed_stop_the_turn_reaches_while_cancel_all_waits_keeps_its_tombstone_and_report():
    # The turn ended on a failed stop of its own: that outcome is the run's, not the
    # shutdown's, so it keeps the error tombstone and the report.
    victim, on_done, _fleet, observed = await _shut_down(
        drain_answer=_FAILED_STOP, follow_up_on="the-run"
    )

    assert observed["victim_running_when_cancel_all_parked"] is True
    assert observed["victim_ended_by"] == "turn ended: refusal"
    assert await _outcome(victim, on_done) == _reported_now("error")


@pytest.mark.asyncio
async def test_a_deadline_the_run_reaches_while_cancel_all_waits_keeps_its_tombstone_and_report():
    on_done, announce_parked, gate = _parked_announce_consumer()
    mgr = SubagentManager(
        sessions=_mock_sessions(lambda *a, **k: None),
        ctx_builder=_mock_ctx_builder(),
        on_done=on_done,
    )
    turn_started = asyncio.Event()

    async def turn_past_its_deadline(_info: SubagentInfo, _session_key: str) -> None:
        turn_started.set()
        await await_until(announce_parked, what="cancel_all parked on the follow-up announce")
        # What wait_for raises where _run awaits the turn once the run's deadline passed.
        raise asyncio.TimeoutError

    with (
        patch.object(mgr, "_run_inner", turn_past_its_deadline),
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
    ):
        victim = mgr.spawn("job at shutdown", parent_session_key=_DASHBOARD_PARENT)
        assert victim is not None
        await await_until(turn_started, what="the run's turn")
        ok, detail = await mgr.follow_up_run(victim.id, "then summarise what you found")
        assert ok, detail
        victim_task = mgr._tasks[victim.id]

        async def release_the_announce() -> None:
            await await_until(announce_parked, what="cancel_all parked on the follow-up announce")
            await async_wait_until(
                victim_task.done, describe=lambda: f"victim error={victim.error!r}"
            )
            gate.set()

        await finished(
            asyncio.gather(mgr.cancel_all(), release_the_announce(), return_exceptions=True),
            "the shutdown gather",
        )

    assert victim.error.startswith("Timed out after")
    assert await _outcome(victim, on_done) == _reported_now("timeout")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("drain_answer", "tombstone"),
    [
        pytest.param(_CANCEL_ACKED, "cancelled", id="session-cancel-acked"),
        pytest.param(_NO_ACK, "error", id="no-ack-process-killed"),
    ],
)
@pytest.mark.parametrize(
    ("parent", "open_tabs"),
    [
        pytest.param("cron:nightly-digest", frozenset({"cron:nightly-digest"}), id="cron"),
        pytest.param("subagent:p1", frozenset({"subagent:p1"}), id="subagent"),
        pytest.param(_CHANNEL_PARENT, frozenset(), id="channel-without-a-tab"),
    ],
)
async def test_a_run_close_all_ends_under_another_kind_of_parent_keeps_its_tombstone_and_report(
    monkeypatch, drain_answer, tombstone, parent, open_tabs
):
    # A cron or subagent parent keeps the report even with the registry listing it, and
    # a channel conversation does with no tab open: the same parents the cancel arm
    # reports to.
    monkeypatch.setattr(session_surface, "_dashboard_surfaced", open_tabs)
    victim, on_done, _fleet, observed = await _shut_down(
        drain_answer=drain_answer, follow_up_on="the-run", parent=parent
    )

    assert observed["victim_running_when_cancel_all_parked"] is True
    assert await _outcome(victim, on_done) == _reported_now(tombstone)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("follow_up_on", "victim_starting", "ended_by"),
    [
        pytest.param("the-run", False, "process killed", id="process-killed"),
        pytest.param("a-sibling", True, "session start refused", id="session-start-refused"),
    ],
)
async def test_a_user_stop_close_all_ends_keeps_its_tombstone_and_report(
    follow_up_on, victim_starting, ended_by
):
    victim, on_done, _fleet, observed = await _shut_down(
        drain_answer=_NO_ACK,
        follow_up_on=follow_up_on,
        user_stopped=True,
        victim_starting=victim_starting,
    )

    assert observed["victim_ended_by"] == ended_by
    assert await _outcome(victim, on_done) == _reported_now("error")


def test_shared_turn_tenancy_refuses_the_shutdown_kill_of_its_runtime():
    """The shared arm: a streaming subagent turn holds a tenancy on its runtime
    (``AcpSessionProvider._claim_shared_turn``), so ``close_all``'s kill of that
    runtime (``release_subagent_runtime`` or the parent's ``shutdown``) is refused by
    ``authorize_runtime_kill`` before ``_mark_dead`` can poison the turn's queue."""
    from kiro_crew.runtime_ownership import (
        authorize_runtime_kill,
        claim_runtime_tenancy,
        release_runtime_tenancy,
    )

    runtime = SimpleNamespace(pid=UNALLOCATABLE_PID, is_alive=lambda: True)
    claim = claim_runtime_tenancy(
        runtime, holder="subagent:subagent:abc", session_key="subagent:abc"
    )
    assert claim is not None
    try:
        assert (
            authorize_runtime_kill(runtime, reason="subagent runtime released", caller="test")
            is False
        )
    finally:
        orphan = release_runtime_tenancy(claim)
    # The refused teardown is owed to the last tenant, which ends the runtime itself.
    assert orphan is runtime
