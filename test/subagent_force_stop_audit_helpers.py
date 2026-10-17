"""Shared fixtures and fakes for the subagent force-stop audit suites.

``test_subagent_force_stop_audit.py`` and its ``test_subagent_force_stop_audit_<topic>.py``
siblings import these by name: the three autouse pins of the signal seams (the kill seam, the
tree behind a gone leader, and group leadership), and the session, run and audit fakes the
reaper tests share.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.subagent import SubagentInfo, SubagentManager

# The start id the fake client records at spawn (``platform_compat.get_process_start_id``
# reads ``/proc/<pid>/stat`` field 22 on Linux); the kill re-reads it before signalling.
_START_ID = "4821903"
_REFUSAL = ValueError("kill_process_group: refusing broadcast/self process group 4242")


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the test_subagent_force_stop_audit suites' autouse pin; none of them calls monkeypatch.undo()
def _kill_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kill path is POSIX-shaped here unless a test pins ``IS_WINDOWS`` itself.

    The cron reaper's tests hold the same pin (``cron_reaper_helpers.py::_kill_seam``):
    ``kill_verified_process`` reads ``platform_compat.IS_WINDOWS`` at call time --
    on POSIX the live root's captured group is signalled through
    ``kill_process_group``, which the POSIX-shaped tests here patch to land or
    raise; on Windows through ``kill_process_tree_pinned``, which a fabricated pid
    can never pin -- so unpinned, every POSIX-shaped test read differently on a
    Windows runner than on Linux. A test of the Windows shape sets ``IS_WINDOWS``
    True in its own body, after this fixture, and wins.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)


@pytest.fixture(autouse=True)
def _no_tree_behind_the_leader() -> Iterator[None]:
    """The leader's process group is empty and no Windows tree cleanup is pending.

    Pinned for every test: the shared kill asks a gone or recycled leader's
    tree before calling it gone, and the probes are real otherwise. A test that
    asserts on either probe patches it again inside its own ``with`` (the inner
    patch wins).
    """
    with (
        patch("kiro_crew.platform_compat.pgroup_exists", return_value=False),
        patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=False),
    ):
        yield


@pytest.fixture(autouse=True)
def _leads_its_own_group() -> Iterator[None]:
    """The fabricated root leads its own POSIX group: ``getpgid(pid) == pid``, ours is 1.

    The same pin the cron reaper's tests hold (``_isolated_leader``): the capture
    site ``isolated_group_of`` reads both names through ``getattr`` and they are
    CREATED on a runner that lacks them, so the handle captures the group on
    every platform the POSIX-shaped tests run on, and the kill then addresses
    THAT captured id through ``kill_process_group`` -- which these tests pin --
    never a group resolved from the pid at signal time. A Windows-shaped test
    (``IS_WINDOWS`` patched) captures no group and goes through the pinned tree
    drain instead.
    """
    with (
        patch("os.getpgid", side_effect=lambda pid: pid, create=True),
        patch("os.getpgrp", return_value=1, create=True),
    ):
        yield


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock(return_value=True)
    sessions.tearing_down = MagicMock(return_value=[])
    sessions._sessions = {}
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = False
    return ctx


def _session_with_pid(
    manager: SubagentManager,
    session_key: str,
    pid: int | None,
    *,
    start_id: str | None = _START_ID,
    child_pids: dict[Any, Any] | None = None,
) -> MagicMock:
    """Register a session under ``session_key`` whose ACP client reports ``pid``.

    The client carries the start id the recycled-pid check compares, so a test
    whose start-id read answers the same value drives ``_sigkill_session`` all
    the way to the group kill.
    """
    client = MagicMock()
    client._pid = pid
    client._child_pids = dict(child_pids or {})
    client._start_time = start_id
    session = MagicMock()
    session.provider._client = client
    manager._sessions._sessions[session_key] = session
    return client


def _overdue_run(
    agent_id: str, *, pid: int | None = 4242, user_stopped: bool = False
) -> tuple[SubagentManager, SubagentInfo, str]:
    """A manager with one overdue run whose session process reports ``pid``."""
    manager = SubagentManager(
        sessions=_mock_sessions(),
        ctx_builder=_mock_ctx_builder(),
        on_done=AsyncMock(),
        on_event=AsyncMock(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(
        id=agent_id,
        task="stuck task",
        parent_session_key="dashboard:test-slot",
        started=time.time() - 7200,
        user_stopped=user_stopped,
    )
    manager._agents[agent_id] = info
    manager._running_count = 1
    session_key = f"subagent:{agent_id}"
    if pid is not None:
        _session_with_pid(manager, session_key, pid)
    return manager, info, session_key


def _hanging_reset(manager: SubagentManager) -> asyncio.Event:
    """A reset that never returns: the caller's ``wait_for`` times out.

    Returns the event the reset sets once it is parked, for a test that must
    cancel the teardown inside it.
    """
    entered = asyncio.Event()

    async def _hang(session_key: str, **_: Any) -> bool:
        entered.set()
        await asyncio.Event().wait()  # parked until the caller's bound or a cancel
        return True

    manager._sessions.reset = AsyncMock(side_effect=_hang)
    return entered


def _kill_path_stubs() -> tuple[Any, Any, Any, Any]:
    """The child-tree probe, the liveness probe, the start-id read and the sweep stubbed.

    Only the kill decides: the pid exists and its start id reads back as the
    recorded one, so the root is this run's and the signal is reached.
    """
    return (
        patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
        patch("kiro_crew.platform_compat.pid_exists", return_value=True),
        patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
        patch("kiro_crew.acp.client._kill_escaped_children"),
    )


def _audit(mock_sel: MagicMock, tool_name: str) -> dict[str, Any]:
    """The kwargs of the one SEL audit row written under ``tool_name``."""
    rows = [
        call.kwargs
        for call in mock_sel().log_tool_invocation.call_args_list
        if call.kwargs.get("tool_name") == tool_name
    ]
    assert len(rows) == 1, f"expected one {tool_name} audit row, saw {len(rows)}"
    return rows[0]


def _handle_of(manager: SubagentManager, agent_id: str) -> Any:
    """The kill handle the teardown paths take before their reset and hand to the kill."""
    handles = manager._retain_process_handles(agent_id, f"subagent:{agent_id}")
    assert handles, f"no session registered for {agent_id}"
    return handles[0]
