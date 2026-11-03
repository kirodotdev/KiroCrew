"""A sub-agent completion never cancels its parent's live turn.

The cron and channel injection routes claim the parent's session, prompt it,
and on the way out cancel "their" prompt. When an attempt times out, the route
resets the parent first. That reset pops the session and wakes the claimants
parked on its lease, so by the time the route's own cleanup runs the key can
already hold a successor running the parent's NEXT turn (the next cron fire, a
new channel message). A cancel addressed by key alone lands on that turn.

These tests drive the real ``SessionManager`` lease. The parent's next turn is
already waiting for the key while the injection holds it, and the reset wrapper
only lets the injection's cleanup run once that turn owns the successor --
the window a slow provider shutdown inside ``reset`` opens in production.
No sleeps: every hand-off is an event or an awaited task.

A cron parent's completion claims only a session that already exists
(``claim_existing``): the last section drives a parent that has none, one whose
failed turn ends its conversation while the completion waits, and several
completions at once, and pins that no provider is started for any of them.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager
from kiro_crew.subagent import SubagentInfo

#: Ceiling for a wait that only fails on a hang, so a regression fails by name.
_HANG_CEILING_S = 30


def _provider_factory() -> Any:
    def factory(session_key: Any = None, agent: Any = None, channel_id: Any = None, **kwargs: Any):
        m = AsyncMock()
        m.start = AsyncMock()
        m.memory_mode = kwargs.get("memory_mode", "persistent")
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.disown_work_dir = MagicMock()
        m.context_usage_pct = lambda: 0.0
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_abort_target = lambda: None
        m.spawn_identity = ""
        m._runtime = None
        m.cancel = AsyncMock(return_value="acked")
        return m

    return factory


def _gateway(sessions: SessionManager) -> Any:
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gateway = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gateway.sessions = sessions
    gateway.ctx_builder = None
    gateway.conv_log = None
    gateway.dashboard_state = None
    gateway.slack = None
    gateway._owner_id = ""
    gateway._cron_injecting = {}
    gateway._cron_runs_holding = set()
    gateway._inline_parked = {}
    gateway._inline_tasks = set()
    # As service boot wires it: a turn's release settles its inline marks.
    if hasattr(sessions, "parent_turns"):
        sessions.parent_turns.on_released = gateway._settle_inline
    gateway.cron_svc = None
    gateway.subagent_mgr = MagicMock()
    gateway.subagent_mgr.running = []
    gateway.subagent_mgr.queued_count_for_async = AsyncMock(return_value=0)
    gateway._channel_reply_link = MagicMock(return_value=None)
    gateway._deliver_channel_reply = AsyncMock()
    return gateway


class _ParentNextTurn:
    """The parent's next turn: parked on the key, then holding the successor."""

    def __init__(self, sessions: SessionManager, key: str) -> None:
        self._sessions = sessions
        self._key = key
        self.owns_lease = asyncio.Event()
        self.finish = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        await self._sessions.get_or_create(self._key)
        self.owns_lease.set()
        try:
            await self.finish.wait()
        finally:
            self._sessions.release(self._key)


async def _drive(route: str, key: str) -> tuple[Any, Any, _ParentNextTurn, SessionManager]:
    """Time an injection out under *route* while the parent's next turn waits."""
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    gateway = _gateway(sessions)
    # The parent's session is live and idle, as a cron run that deferred its reset
    # for running sub-agents leaves it, or a channel between two messages: a cron
    # completion only ever claims a session that exists.
    await sessions.get_or_create(key)
    sessions.release(key)
    parent_turn = _ParentNextTurn(sessions, key)
    injected_into: list[Any] = []

    async def inject_times_out(info, client, msg, parent_key, label):
        # The injection holds the parent's lease here. The parent's next turn
        # arrives now and parks on it, as a cron fire or channel message does.
        injected_into.append(client)
        parent_turn.start()
        raise asyncio.TimeoutError

    real_reset = sessions.reset

    async def reset_until_successor_is_live(parent_key, **kwargs):
        result = await real_reset(parent_key, **kwargs)
        await asyncio.wait_for(parent_turn.owns_lease.wait(), _HANG_CEILING_S)
        return result

    gateway._inject_with_retry = inject_times_out
    info = SubagentInfo(id="child-1", task="work", parent_session_key=key)
    info.result = "done"
    info.done = True
    with (
        patch.object(sessions, "reset", side_effect=reset_until_successor_is_live),
        patch("kiro_crew.slack.gateway._MAX_INJECT_ATTEMPTS", 1),
    ):
        if route == "cron":
            # A sibling still running keeps the route's after-injection
            # session reset out of this test; that reset is not a cancel.
            gateway.subagent_mgr.running = [
                SubagentInfo(id="child-2", task="more", parent_session_key=key)
            ]
            await asyncio.wait_for(
                gateway._finish_subagent_cron_or_notify(
                    info,
                    announce="child done",
                    body="b",
                    title="t",
                    carried=(info.id,),
                    parent_key=key,
                ),
                _HANG_CEILING_S,
            )
        else:
            await asyncio.wait_for(
                gateway._inject_subagent_into_channel_parent(
                    info,
                    announce="child done",
                    body="b",
                    title="t",
                    carried=(info.id,),
                    parent_key=key,
                ),
                _HANG_CEILING_S,
            )
    assert len(injected_into) == 1
    return injected_into[0], sessions._sessions[key], parent_turn, sessions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "key"),
    [("cron", "cron:job-1"), ("channel", "telegram:chat-1")],
)
async def test_a_timed_out_injection_leaves_the_parents_next_turn_running(route, key):
    injected_provider, successor, parent_turn, sessions = await _drive(route, key)
    try:
        assert successor.provider is not injected_provider, "the reset made a successor"
        assert successor.turn_owner is parent_turn.task
        successor.provider.cancel.assert_not_awaited()
        # The injection's release was owed to the popped session, so the
        # parent's turn still holds its own lease.
        assert successor.semaphore.locked()
        assert not parent_turn.task.done()
    finally:
        parent_turn.finish.set()
        await asyncio.wait_for(parent_turn.task, _HANG_CEILING_S)
        await sessions.close_all()


@pytest.mark.asyncio
async def test_the_owner_fenced_cancel_still_reaches_the_callers_own_turn():
    """The fence narrows the cancel to the caller; it does not disable it."""
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    try:
        provider, _is_new, _resumed = await sessions.get_or_create("cron:job-2")
        outcome = await sessions.cancel_current("cron:job-2", owner_task=asyncio.current_task())
        assert outcome == "acked"
        provider.cancel.assert_awaited_once()
        sessions.release("cron:job-2")
    finally:
        await sessions.close_all()


# ── a cron parent with no session ─────────────────────────────────────────────


def _counting_sessions() -> tuple[SessionManager, list[Any]]:
    factory, started = _provider_factory(), []

    def counting_factory(*args: Any, **kwargs: Any) -> Any:
        started.append(factory(*args, **kwargs))
        return started[-1]

    return SessionManager(KiroCrewConfig(), provider_factory=counting_factory), started


def _cron_gateway(sessions: SessionManager) -> Any:
    gateway = _gateway(sessions)
    gateway.cron_svc = MagicMock(name="cron_svc")
    gateway._inject_with_retry = AsyncMock(return_value="a turn of a new agent")
    return gateway


def _finished(child_id: str, key: str) -> SubagentInfo:
    info = SubagentInfo(id=child_id, task="work", parent_session_key=key)
    info.result = "done"
    info.done = True
    return info


def _cron_route(gateway: Any, info: SubagentInfo, key: str) -> Any:
    return gateway._finish_subagent_cron_or_notify(
        info, announce=f"{info.id} done", body="b", title="t", carried=(info.id,), parent_key=key
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", ["never-started", "reset", "ended"])
async def test_a_completion_for_a_cron_parent_with_no_session_starts_none(parent: str) -> None:
    """A ``cron:`` key is stateless, so a session started for a completion holds none
    of the orchestration that spawned the worker and acts on the result as a new,
    unattended agent outside any run. The route starts nothing and prompts nothing:
    the result stays undelivered for reconciliation, no failure text is queued for a
    later prompt, and the finished run's key is retired."""
    key = "cron:job-1:run-1"
    sessions, started = _counting_sessions()
    gateway = _cron_gateway(sessions)
    try:
        if parent != "never-started":
            await sessions.get_or_create(key)
            sessions.release(key)
            await sessions.reset(key, ends_conversation=parent == "ended")
        before = len(started)
        info = _finished("child-1", key)

        await asyncio.wait_for(_cron_route(gateway, info, key), _HANG_CEILING_S)

        assert len(started) == before, "the completion started a session for its parent"
        gateway._inject_with_retry.assert_not_awaited()
        assert not sessions.has_session(key)
        assert info._report_undelivered is True
        assert info._report_delivered is False
        gateway.subagent_mgr.notify_injection_failed.assert_not_called()
        assert gateway._cron_injecting == {}
        gateway.cron_svc.retire_finished_session_key.assert_called_once_with("job-1", key)
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_completion_waiting_on_a_failed_cron_turn_starts_no_session() -> None:
    """The cron turn holds its session while a worker's completion waits for it. The
    turn fails, and ends its conversation before it releases the key
    (``_end_failed_cron_turn``), so the waiting completion finds nothing to claim:
    it neither continues the ending conversation nor starts a new one."""
    from kiro_crew.slack.gateway import _end_failed_cron_turn
    from kiro_crew.testing.wait import async_wait_until

    key = "cron:job-1:run-1"
    sessions, started = _counting_sessions()
    gateway = _cron_gateway(sessions)
    holds, fails = asyncio.Event(), asyncio.Event()

    async def failing_cron_turn() -> None:
        await sessions.get_or_create(key)
        holds.set()
        await fails.wait()
        await _end_failed_cron_turn(sessions, key)
        sessions.release(key)

    try:
        turn = asyncio.create_task(failing_cron_turn())
        await asyncio.wait_for(holds.wait(), _HANG_CEILING_S)
        info = _finished("child-1", key)
        route = asyncio.create_task(_cron_route(gateway, info, key))
        await async_wait_until(
            lambda: route.done() or sessions._has_allocation_reservation(key),
            describe=lambda: "the completion waiting on the cron turn's permit",
        )

        fails.set()
        await asyncio.wait_for(asyncio.gather(turn, route), _HANG_CEILING_S)

        assert len(started) == 1, "the completion started a session after the run failed"
        gateway._inject_with_retry.assert_not_awaited()
        assert not sessions.has_session(key)
        assert info._report_undelivered is True
        assert gateway._cron_injecting == {}
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
async def test_concurrent_completions_for_a_dead_cron_parent_start_nothing() -> None:
    """Several workers finish together under a parent that is gone: every one is
    refused, none starts a session, and the bookkeeping settles to empty."""
    key = "cron:job-1:run-1"
    sessions, started = _counting_sessions()
    gateway = _cron_gateway(sessions)
    infos = [_finished(f"child-{i}", key) for i in range(3)]
    try:
        await asyncio.wait_for(
            asyncio.gather(*(_cron_route(gateway, info, key) for info in infos)),
            _HANG_CEILING_S,
        )

        assert started == []
        gateway._inject_with_retry.assert_not_awaited()
        assert all(info._report_undelivered for info in infos)
        assert gateway._cron_injecting == {}
        assert not sessions._has_allocation_reservation(key)
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
async def test_the_cron_injection_refuses_its_client_a_new_session() -> None:
    """The claim found the parent live, but its process can exit before or between the
    injection's prompts. The prompts run under ``continue_only`` for the claimed
    client, so a direct client refuses to open a new session for them."""
    from kiro_crew.acp.continue_only import refuses_start

    key = "cron:job-1:run-1"
    sessions, _started = _counting_sessions()
    gateway = _cron_gateway(sessions)
    seen: list[bool] = []

    async def inject(_info: Any, client: Any, _msg: Any, _parent: str, _label: str) -> str:
        seen.append(refuses_start(client))
        return "reply"

    gateway._inject_with_retry = inject
    try:
        provider, _is_new, _resumed = await sessions.get_or_create(key)
        sessions.release(key)
        await asyncio.wait_for(
            _cron_route(gateway, _finished("child-1", key), key), _HANG_CEILING_S
        )

        assert seen == [True]
        assert not refuses_start(provider), "the refusal outlived the injection"
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("after", ["torn-down", "another-session", "handed-off"])
async def test_a_failed_cron_turn_that_no_longer_holds_its_session_ends_nothing(
    after: str,
) -> None:
    """Only a failed turn still holding its session ends the conversation. One whose
    session was torn down under it, one whose key another turn now holds, and one
    that gave its session to a retry that then failed leave the key and its runs
    alone: runs are told apart by key only, so ending them could end another turn's."""
    from kiro_crew.slack.gateway import _end_failed_cron_turn

    key = "cron:job-1"
    sessions, _started = _counting_sessions()
    other = _ParentNextTurn(sessions, key)
    end_children = AsyncMock()
    try:
        await sessions.get_or_create(key)  # the failed cron turn's own claim
        if after == "handed-off":
            sessions.release(key)
        else:
            await sessions.reset(key)  # torn down under it
        if after == "another-session":
            other.start()
            await asyncio.wait_for(other.owns_lease.wait(), _HANG_CEILING_S)
        held = sessions._sessions.get(key)

        with patch.object(sessions, "end_children_for", new=end_children):
            await _end_failed_cron_turn(sessions, key)
        if after == "torn-down":
            sessions.release(key)  # owed to the session the reset popped: absorbed

        end_children.assert_not_awaited()
        assert sessions._sessions.get(key) is held
        if after == "another-session":
            assert sessions.turn_holder(key) is other.task
        if after == "handed-off":
            assert held is not None and not sessions.is_busy(key)
    finally:
        other.finish.set()
        if other.task is not None:
            await asyncio.wait_for(other.task, _HANG_CEILING_S)
        await sessions.close_all()
