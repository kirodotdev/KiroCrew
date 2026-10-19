"""Sub-agent recovery from a context-window overflow (``subagent.py``).

The context-overflow marker forces a dedicated runtime, and a first-turn overflow on a
shared runtime retries once on a dedicated one after the rejected session id is forgotten.
The recovery fails closed when that id cannot be retired cleanly, and an overflow stays
terminal on a dedicated first attempt, on a continuation, on a replacement that overflows
again and after activity. The shared helpers come from
``subagent_turn_resilience_helpers.py``.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_turn_resilience_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _close_subagent_managers,
    _complete_event,
    _context_overflow_error,
    _manager,
    _mock_ctx_builder,
    _mock_sessions,
    _text_event,
)

from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.testing.wait import await_until

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def test_context_overflow_marker_forces_a_dedicated_runtime():
    sessions = MagicMock()
    sessions.is_session_sharing_eligible = MagicMock(return_value=True)
    mgr = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder())
    info = SubagentInfo(
        id="overflow-gate",
        task="evaluate bounded evidence",
        parent_session_key="dashboard:parent",
    )
    cfg = SimpleNamespace(agent=SimpleNamespace(session_sharing=True))

    with patch("kiro_crew.subagent.KiroCrewConfig.load", return_value=cfg):
        assert mgr._should_use_session_sharing(info) is True
        info._force_dedicated = True
        assert mgr._should_use_session_sharing(info) is False


@pytest.mark.asyncio
async def test_first_turn_context_overflow_retries_once_on_a_dedicated_runtime():
    sessions = _mock_sessions(lambda _msg: None)
    mgr = _manager(sessions)
    attempts: list[tuple[bool, bool, int | None, float | None]] = []
    events: list[tuple[str, dict]] = []
    timeline: list[str] = []
    shared_provider = MagicMock()
    shared_provider.set_keep_transcript = MagicMock()
    replacement_started = asyncio.Event()
    release_replacement = asyncio.Event()
    flush_entered = asyncio.Event()
    release_flush = asyncio.Event()
    capacity_wait_entered = asyncio.Event()
    release_capacity_wait = asyncio.Event()
    real_sleep = asyncio.sleep
    stale_shared_pid = 4242
    # A first attempt that waited long enough for its start clock to be past
    # the startup deadline by the time the recovery reaches its capacity wait.
    stale_exec_started = time.time() - (mgr._startup_deadline + 1_000)

    async def shutdown_shared_provider() -> None:
        timeline.append("teardown")

    shared_provider.shutdown = AsyncMock(side_effect=shutdown_shared_provider)

    async def on_event(etype: str, _info: SubagentInfo, extra: dict) -> None:
        events.append((etype, dict(extra)))

    mgr._on_event = on_event

    async def capacity_wait_sleep(delay: float) -> None:
        if mgr._max_concurrent == 0 and not capacity_wait_entered.is_set():
            capacity_wait_entered.set()
            # flake-ok: pre-existing mocked-run event wait, moved verbatim; the test sets the event
            await release_capacity_wait.wait()
            return
        await real_sleep(delay)

    def forget_conversation_if_sid(
        _session_key: str, _expected_sid: str
    ) -> tuple[bool, str | None]:
        timeline.append("delete")
        return True, "rejected-sid"

    async def flush_sessions() -> None:
        timeline.append("aflush")
        # The delete is not publishable until this barrier returns. While it is
        # held, recovery must retain the rejected attempt's identity and must
        # not wait for capacity, allocate a task, or emit recovery.
        assert info._session_sharing is True
        assert info._shared_provider is shared_provider
        assert info._pid == stale_shared_pid
        assert info._exec_started == stale_exec_started
        assert info._startup_deadline_stamp == (
            stale_exec_started,
            mgr._startup_deadline,
        )
        assert mgr._tasks.get(info.id) is None
        flush_entered.set()
        # flake-ok: pre-existing mocked-run event wait, moved verbatim; the test sets the event
        await release_flush.wait()

    sessions.forget_conversation_if_sid = MagicMock(side_effect=forget_conversation_if_sid)
    sessions.aflush = AsyncMock(side_effect=flush_sessions)

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        attempts.append(
            (info._force_dedicated, info._session_sharing, info._pid, info._exec_started)
        )
        if len(attempts) == 1:
            info._session_sharing = True
            info._shared_provider = shared_provider
            info._pid = stale_shared_pid
            # ``_run_inner_impl`` captures the acquired session's identity
            # before prompt dispatch; recovery retires exactly that SID.
            info._session_id = "rejected-sid"
            # The first attempt's startup clock, as ``_run_inner_impl`` and the
            # watchdog's deadline stamp leave it on the record.
            info._exec_started = stale_exec_started
            info._startup_deadline_stamp = (stale_exec_started, mgr._startup_deadline)
            # Hold the recovery at its capacity wait. The retired process
            # identity must be cleared before this wait can suspend.
            mgr._max_concurrent = 0
            raise _context_overflow_error()
        timeline.append("replacement")
        replacement_started.set()
        # flake-ok: pre-existing mocked-run event wait, moved verbatim; the test sets the event
        await release_replacement.wait()
        info.result = "recovered on dedicated runtime"
        info.done = True

    mgr._run_inner = AsyncMock(side_effect=run_inner)

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        # flake-ok: pre-existing asyncio.sleep stub scoped to this block, moved verbatim
        patch("kiro_crew.subagent.asyncio.sleep", side_effect=capacity_wait_sleep),
    ):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        first = mgr._tasks[info.id]
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await first

        await await_until(flush_entered, what="the flush being entered")
        assert timeline == ["teardown", "delete", "aflush"]
        assert capacity_wait_entered.is_set() is False
        assert replacement_started.is_set() is False
        assert [etype for etype, _extra in events].count("subagent_recovering") == 0

        release_flush.set()
        await await_until(capacity_wait_entered, what="the capacity wait being entered")
        assert info._session_sharing is False
        assert info._shared_provider is None
        assert info._pid is None
        # Both startup clocks are cleared with the PID, before the wait could
        # suspend: the replacement has not entered ``_run_inner``, so the record
        # reads as a run that has not started and the startup watchdog cannot
        # reap the capacity wait as a stalled start.
        assert info._exec_started is None
        assert info._startup_deadline_stamp is None
        assert info.turns == 0 and info._first_stream_started is None
        now = time.time()
        assert mgr._is_startup_stalled(info, now) is False
        # The same record with the FIRST attempt's clock left in place is what
        # the watchdog would have reaped, so the assertion above is not vacuous.
        stale = SubagentInfo(id="stale-clock", task=info.task, parent_session_key="")
        stale._exec_started = stale_exec_started
        stale._startup_deadline_stamp = (stale_exec_started, mgr._startup_deadline)
        assert mgr._is_startup_stalled(stale, now) is True
        assert replacement_started.is_set() is False

        mgr._max_concurrent = 1
        release_capacity_wait.set()
        await await_until(replacement_started, what="the replacement starting")
        replacement = mgr._tasks.get(info.id)
        assert replacement is not None and replacement is not first
        release_replacement.set()
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await replacement

    assert attempts == [(False, False, None, None), (True, False, None, None)]
    assert timeline == ["teardown", "delete", "aflush", "replacement"]
    sessions.aflush.assert_awaited_once_with()
    assert info.done is True
    assert info.error == ""
    assert info.result == "recovered on dedicated runtime"
    assert info._context_overflow_retry_used is True
    shared_provider.shutdown.assert_awaited_once()
    recovering = [extra for etype, extra in events if etype == "subagent_recovering"]
    assert recovering == [{"attempt": 1}]


@pytest.mark.asyncio
async def test_dedicated_first_attempt_context_overflow_is_terminal():
    """A first spawn whose own dedicated process overflowed before any activity.

    Eligibility is shared-first-attempt only: after a dedicated first attempt
    no transition is evidenced that would be expected to make a replacement's
    envelope fit, so another teardown and spawn cycle is not justified. The
    overflow is terminal without scheduling recovery and without consuming the
    one-shot.
    """
    sessions = _mock_sessions(lambda _msg: None)
    mgr = _manager(sessions)
    attempts = 0
    schedule_recovery = MagicMock()
    mgr._schedule_cancel_recovery = schedule_recovery

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        nonlocal attempts
        attempts += 1
        # A dedicated first attempt: no shared handle, its own process.
        assert info._session_sharing is False and info._shared_provider is None
        assert info.conversation_key == "" and info.turns == 0
        info._pid = 5151
        info._session_id = "rejected-sid"
        raise _context_overflow_error()

    mgr._run_inner = AsyncMock(side_effect=run_inner)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await mgr._tasks[info.id]
        # flake-ok: pre-existing one-tick yield before the asserts, moved verbatim
        await asyncio.sleep(0)

    assert attempts == 1
    assert info.done is True
    assert "exceeded the model window" in info.error
    assert info._context_overflow_retry_used is False
    assert info._force_dedicated is False
    schedule_recovery.assert_not_called()
    assert mgr._tasks.get(f"{info.id}:recovery") is None
    sessions.forget_conversation_if_sid.assert_not_called()
    sessions.aflush.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_turn_context_overflow_forgets_rejected_sid_before_fresh_recovery():
    """A fresh run must not session/load its rejected shared first-attempt SID.

    The first attempt runs on the shared runtime through the real ``_run_inner``
    (the shared session is stubbed where the parent's runtime would create it);
    the replacement is the ordinary dedicated allocation.
    """
    sessions = _mock_sessions(lambda _msg: None)
    mgr = _manager(sessions)
    # Shared for the first attempt, dedicated once the one-shot forces it; the
    # rest of the plan (template, no pin) stays as the real rule reads it.
    mgr._should_use_session_sharing = MagicMock(side_effect=lambda info: not info._force_dedicated)
    provider = sessions._provider
    provider.session_id = ""
    provider.cwd = ""
    provider.set_keep_transcript = MagicMock()
    mapping: dict[str, str] = {}
    timeline: list[str] = []
    wire: list[tuple[str, str]] = []
    events: list[str] = []
    replacement_started = asyncio.Event()
    release_replacement = asyncio.Event()

    async def on_event(etype: str, _info: SubagentInfo, _extra: dict) -> None:
        events.append(etype)

    mgr._on_event = on_event

    async def create_shared_session(info: SubagentInfo, session_key: str, _agent: str):
        # Stands in for ``_bind_shared_handle``: the session is created on the
        # shared runtime, its identity recorded and a resumable mapping persisted.
        mapping[session_key] = "rejected-sid"
        timeline.append("session/new:rejected-sid")
        wire.append(("session/new", "rejected-sid"))
        provider.session_id = "rejected-sid"
        info._session_sharing = True
        info._shared_provider = provider
        return provider

    async def get_or_create(session_key: str, **_kwargs):
        if session_key in mapping:
            sid = mapping[session_key]
            timeline.append(f"session/load:{sid}")
            wire.append(("session/load", sid))
            provider.session_id = sid
            return provider, True, True
        sid = "recovered-sid"
        mapping[session_key] = sid
        timeline.append(f"session/new:{sid}")
        wire.append(("session/new", sid))
        provider.session_id = sid
        return provider, True, False

    async def shutdown_shared() -> None:
        timeline.append(f"shutdown:{provider.session_id}")

    async def flush_sessions() -> None:
        timeline.append("aflush")

    def forget_conversation_if_sid(session_key: str, expected_sid: str) -> tuple[bool, str | None]:
        current_sid = mapping.get(session_key)
        timeline.append(f"forget-if:{expected_sid}:{current_sid or ''}")
        if current_sid is None:
            return False, None
        if current_sid != expected_sid:
            return False, current_sid
        mapping.pop(session_key)
        return True, current_sid

    def stream_factory(_message: str, *_args, **_kwargs):
        sid = provider.session_id

        async def stream():
            if sid == "rejected-sid":
                raise _context_overflow_error()
            replacement_started.set()
            # flake-ok: pre-existing mocked-run event wait, moved verbatim; the test sets the event
            await release_replacement.wait()
            yield _text_event("recovered with fresh native context")
            yield _complete_event()

        return stream()

    mgr._create_shared_session = AsyncMock(side_effect=create_shared_session)
    sessions.get_or_create = AsyncMock(side_effect=get_or_create)
    provider.shutdown = AsyncMock(side_effect=shutdown_shared)
    sessions.forget_conversation_if_sid = MagicMock(side_effect=forget_conversation_if_sid)
    sessions.aflush = AsyncMock(side_effect=flush_sessions)
    provider.stream = MagicMock(side_effect=stream_factory)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        session_key = f"subagent:{info.id}"
        first = mgr._tasks[info.id]
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await first

        await await_until(replacement_started, what="the replacement starting")
        replacement = mgr._tasks.get(info.id)
        assert replacement is not None and replacement is not first
        release_replacement.set()
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await replacement

    assert wire == [
        ("session/new", "rejected-sid"),
        ("session/new", "recovered-sid"),
    ]
    assert timeline.index("shutdown:rejected-sid") < timeline.index(
        "forget-if:rejected-sid:rejected-sid"
    )
    assert timeline.index("forget-if:rejected-sid:rejected-sid") < timeline.index("aflush")
    assert timeline.index("aflush") < timeline.index("session/new:recovered-sid")
    sessions.forget_conversation_if_sid.assert_called_once_with(session_key, "rejected-sid")
    sessions.aflush.assert_awaited_once_with()
    # The first attempt's teardown shut its shared handle; the replacement's
    # own dedicated process is what the later reset belongs to.
    provider.shutdown.assert_awaited_once()
    assert mapping == {session_key: "recovered-sid"}
    assert info.done is True
    assert info.error == ""
    assert info.result == "recovered with fresh native context"
    assert info._session_id == "recovered-sid"
    assert info._session_sharing is False
    assert events.count("subagent_recovering") == 1
    assert events.count("subagent_done") == 1


@pytest.mark.asyncio
async def test_context_overflow_recovery_preserves_a_successor_sid_and_fails_closed():
    sessions = _mock_sessions(lambda _msg: None)
    mgr = _manager(sessions)
    attempts = 0
    shared_provider = MagicMock()
    shared_provider.set_keep_transcript = MagicMock()
    shared_provider.shutdown = AsyncMock()

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        nonlocal attempts
        attempts += 1
        info._session_sharing = True
        info._shared_provider = shared_provider
        info._session_id = "rejected-sid"
        raise _context_overflow_error()

    mgr._run_inner = AsyncMock(side_effect=run_inner)
    sessions.forget_conversation_if_sid = MagicMock(return_value=(False, "successor-sid"))

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        first = mgr._tasks[info.id]
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await first
        recovery = mgr._tasks.get(f"{info.id}:recovery")
        assert recovery is not None
        await await_until(
            asyncio.gather(recovery, return_exceptions=True), what="the recovery ending"
        )

    assert attempts == 1
    sessions.forget_conversation_if_sid.assert_called_once_with(
        f"subagent:{info.id}", "rejected-sid"
    )
    assert info.done is True
    assert info.error == (
        "agent context exceeded the model window and the dedicated-session "
        "recovery could not start"
    )
    assert mgr._tasks.get(info.id) is None
    sessions.aflush.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_overflow_recovery_flush_failure_is_terminal_before_replacement():
    on_done = AsyncMock()
    sessions = _mock_sessions(lambda _msg: None)
    mgr = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        on_done=on_done,
    )
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    attempts = 0
    events: list[str] = []
    shared_provider = MagicMock()
    shared_provider.set_keep_transcript = MagicMock()
    shared_provider.shutdown = AsyncMock()

    async def on_event(etype: str, _info: SubagentInfo, _extra: dict) -> None:
        events.append(etype)

    mgr._on_event = on_event

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            raise AssertionError("replacement allocated after durability failure")
        info._session_sharing = True
        info._shared_provider = shared_provider
        info._session_id = "rejected-sid"
        raise _context_overflow_error()

    mgr._run_inner = AsyncMock(side_effect=run_inner)
    sessions.forget_conversation_if_sid = MagicMock(return_value=(True, "rejected-sid"))
    sessions.aflush = AsyncMock(side_effect=OSError("session map fsync failed"))

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        first = mgr._tasks[info.id]
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await first
        recovery = mgr._tasks.get(f"{info.id}:recovery")
        assert recovery is not None
        await await_until(
            asyncio.gather(recovery, return_exceptions=True), what="the recovery ending"
        )
        replacement = mgr._tasks.get(info.id)
        if replacement is not None:
            # flake-ok: pre-existing unbounded gather of scheduled tasks, moved verbatim
            await asyncio.gather(replacement, return_exceptions=True)

    assert attempts == 1
    sessions.forget_conversation_if_sid.assert_called_once_with(
        f"subagent:{info.id}", "rejected-sid"
    )
    sessions.aflush.assert_awaited_once_with()
    assert mgr._tasks.get(info.id) is None
    assert events.count("subagent_recovering") == 0
    assert info.done is True
    assert info.error == (
        "agent context exceeded the model window and the dedicated-session "
        "recovery could not start"
    )
    assert events.count("subagent_done") == 1
    on_done.assert_awaited_once_with(info)


@pytest.mark.asyncio
async def test_context_overflow_recovery_without_a_rejected_sid_fails_closed():
    """No rejected SID: recovery stops before any capacity wait or allocation.

    The identity capture after session acquisition is best-effort, while the
    allocation may already have persisted a resumable mapping. With no SID to
    compare, recovery cannot tell a mapped rejected attempt from a successor,
    so it must neither touch the mapping nor allocate a replacement that could
    ``session/load`` it.
    """
    on_done = AsyncMock()
    sessions = _mock_sessions(lambda _msg: None)
    mgr = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder(), on_done=on_done)
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    attempts = 0
    events: list[str] = []
    mapping: dict[str, str] = {}
    capacity_wait_entered = asyncio.Event()
    real_sleep = asyncio.sleep
    shared_provider = MagicMock()
    shared_provider.set_keep_transcript = MagicMock()
    shared_provider.shutdown = AsyncMock()

    async def on_event(etype: str, _info: SubagentInfo, _extra: dict) -> None:
        events.append(etype)

    mgr._on_event = on_event

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        nonlocal attempts
        attempts += 1
        # The acquisition persisted a resumable mapping for this key, but the
        # best-effort identity capture never landed on the record.
        assert not getattr(info, "_session_id", "")
        info._session_sharing = True
        info._shared_provider = shared_provider
        mapping[f"subagent:{info.id}"] = "mapped-sid"
        # A full pool: a recovery that reaches its capacity wait would suspend
        # here, so the probe below distinguishes "never waited" from "waited".
        mgr._max_concurrent = 0
        raise _context_overflow_error()

    async def capacity_wait_sleep(delay: float) -> None:
        if mgr._max_concurrent == 0:
            capacity_wait_entered.set()
        await real_sleep(delay)

    def forget_conversation_if_sid(session_key: str, expected_sid: str) -> tuple[bool, str | None]:
        current_sid = mapping.get(session_key)
        if current_sid == expected_sid:
            mapping.pop(session_key)
            return True, current_sid
        return False, current_sid

    mgr._run_inner = AsyncMock(side_effect=run_inner)
    sessions.forget_conversation_if_sid = MagicMock(side_effect=forget_conversation_if_sid)

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        # flake-ok: pre-existing asyncio.sleep stub scoped to this block, moved verbatim
        patch("kiro_crew.subagent.asyncio.sleep", side_effect=capacity_wait_sleep),
    ):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        session_key = f"subagent:{info.id}"
        first = mgr._tasks[info.id]
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await first
        # The overflow was accepted for recovery (not finalized by the run) and
        # the recovery coroutine is pending after the original's teardown.
        assert info._context_overflow_retry_used is True
        recovery = mgr._tasks.get(f"{info.id}:recovery")
        assert recovery is not None
        await await_until(
            asyncio.gather(recovery, return_exceptions=True), what="the recovery ending"
        )

    # Fail-closed BEFORE the capacity wait and before any replacement.
    assert attempts == 1
    assert capacity_wait_entered.is_set() is False
    assert mgr._tasks.get(info.id) is None
    assert mgr._tasks.get(f"{info.id}:recovery") is None
    assert events.count("subagent_recovering") == 0
    # No mapping was deleted or altered: without the rejected SID the mapped
    # entry cannot be proven to be the rejected attempt rather than a successor.
    sessions.forget_conversation_if_sid.assert_not_called()
    sessions.aflush.assert_not_awaited()
    assert mapping == {session_key: "mapped-sid"}
    # Terminal recovery failure, finalized and reported exactly once.
    assert info.done is True
    assert info.error == (
        "agent context exceeded the model window and the dedicated-session "
        "recovery could not start"
    )
    assert events.count("subagent_done") == 1
    on_done.assert_awaited_once_with(info)


@pytest.mark.asyncio
async def test_continuation_context_overflow_before_activity_is_terminal():
    import kiro_crew.subagent_persistence as sp

    await asyncio.to_thread(
        sp.create_agent_folder,
        "original-run",
        memory_mode="persistent",
    )
    await asyncio.to_thread(sp.write_run_agent, "original-run", "")
    sessions = _mock_sessions(lambda _msg: None)
    mgr = _manager(sessions)
    attempts = 0
    schedule_recovery = MagicMock()
    mgr._schedule_cancel_recovery = schedule_recovery

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        nonlocal attempts
        attempts += 1
        assert info.conversation_key == "subagent:original-run"
        assert info.turns == 0
        raise _context_overflow_error()

    mgr._run_inner = AsyncMock(side_effect=run_inner)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn(
            "continue established conversation",
            keep=True,
            conversation_key="subagent:original-run",
        )
        assert info is not None
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await mgr._tasks[info.id]

    assert attempts == 1
    assert info.done is True
    assert "exceeded the model window" in info.error
    assert info._context_overflow_retry_used is False
    assert info._force_dedicated is False
    schedule_recovery.assert_not_called()
    assert mgr._tasks.get(f"{info.id}:recovery") is None


@pytest.mark.asyncio
async def test_dedicated_context_overflow_is_terminal_without_a_retry_loop():
    sessions = _mock_sessions(lambda _msg: None)
    mgr = _manager(sessions)
    attempts = 0
    shared_provider = MagicMock()
    shared_provider.set_keep_transcript = MagicMock()
    shared_provider.shutdown = AsyncMock()
    replacement_started = asyncio.Event()
    release_replacement = asyncio.Event()

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            info._session_sharing = True
            info._shared_provider = shared_provider
            info._session_id = "rejected-sid"
        else:
            replacement_started.set()
            # flake-ok: pre-existing mocked-run event wait, moved verbatim; the test sets the event
            await release_replacement.wait()
        raise _context_overflow_error()

    mgr._run_inner = AsyncMock(side_effect=run_inner)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        first = mgr._tasks[info.id]
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await first

        await await_until(replacement_started, what="the replacement starting")
        replacement = mgr._tasks.get(info.id)
        assert replacement is not None and replacement is not first
        release_replacement.set()
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await replacement
        # flake-ok: pre-existing one-tick yield before the asserts, moved verbatim
        await asyncio.sleep(0)

    assert attempts == 2
    assert info.done is True
    assert info.error.startswith("Dedicated-session recovery also overflowed:")
    assert mgr._tasks.get(f"{info.id}:recovery") is None
    shared_provider.shutdown.assert_awaited_once()


@pytest.mark.parametrize(
    ("field", "value", "side_effecting"),
    [
        ("result", "partial result", False),
        ("streaming_text", "partial stream", False),
        ("tool_count", 1, True),
        ("turns", 1, False),
    ],
)
@pytest.mark.asyncio
async def test_context_overflow_after_activity_is_not_replayed(
    field: str, value: object, side_effecting: bool
):
    sessions = _mock_sessions(lambda _msg: None)
    mgr = _manager(sessions)
    attempts = 0
    shared_provider = MagicMock()
    shared_provider.set_keep_transcript = MagicMock()
    shared_provider.shutdown = AsyncMock()

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        nonlocal attempts
        attempts += 1
        info._session_sharing = True
        info._shared_provider = shared_provider
        setattr(info, field, value)
        raise _context_overflow_error()

    mgr._run_inner = AsyncMock(side_effect=run_inner)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await mgr._tasks[info.id]
        # flake-ok: pre-existing one-tick yield before the asserts, moved verbatim
        await asyncio.sleep(0)

    assert attempts == 1
    assert info.done is True
    assert "was not replayed" in info.error
    if side_effecting:
        # A tool ran: the fresh runtime cannot know what it changed, so the
        # error names the side-effect risk, as the unexpected-cancel gate does.
        assert "after tools executed" in info.error
        assert "could repeat side effects" in info.error
    else:
        # Text, a result or a turn without a tool call changed no state: the
        # error says the work was preserved and replay would duplicate it,
        # and must not claim a side effect that never happened.
        assert "had already produced work" in info.error
        assert "preserved" in info.error
        assert "duplicating" in info.error
        assert "side effect" not in info.error
        assert "tools executed" not in info.error
    assert info._context_overflow_retry_used is False
    assert mgr._tasks.get(f"{info.id}:recovery") is None
    if field == "streaming_text":
        assert info.result == value
    shared_provider.shutdown.assert_awaited_once()
