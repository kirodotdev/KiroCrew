"""A start nobody prompted is not resumed, and a process with MCP off waits for no report.

Two costs a switch on an unused chat paid before its first reply:

* An agent, model or workspace switch on a chat whose session no turn ever
  prompted (an eager prewarm) reset that session but kept its resume pointer. The
  replacement then refused its own eager start as a resume and the first turn
  tried a ``session/load`` of a session with no turn in it, which the load
  refuses, before starting fresh. ``reset(forget_unused_start=True)`` drops that
  pointer, and only for a session still holding its fresh first-turn observation.
* A KAS process spawned for a tool-less agent has MCP switched off, so no server
  can report; the post-``set_mode`` drain still held for a first report until its
  ceiling. It now passes no ceiling for such a process.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import runtime_session_load
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import ACP_BACKEND_KAS
from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import FirstTurnState, SessionManager, _Session
from kiro_crew.start_priority import StartPriority

_KEY = "dashboard:unused-start"


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.is_process_alive = lambda: True
    provider.is_alive = lambda: True
    provider.has_active_turn = lambda: False
    provider.has_unfinished_turn = lambda: False
    provider.shutdown = AsyncMock()
    return provider


def _manager_with(first_turn: FirstTurnState) -> SessionManager:
    manager = SessionManager(KiroCrewConfig(), provider_factory=lambda **_: _provider())
    manager._session_map.set(_KEY, "sid-of-the-start")
    manager._sessions[_KEY] = _Session(provider=_provider(), first_turn=first_turn)
    return manager


def _stored_sid(manager: SessionManager) -> str | None:
    return (manager._session_map._data.get(_KEY) or {}).get("sid") or None


@pytest.mark.asyncio
async def test_a_start_no_turn_prompted_loses_its_resume_pointer() -> None:
    manager = _manager_with(FirstTurnState.FRESH)

    assert await manager.reset(_KEY, skip_if_busy=True, forget_unused_start=True) is True

    assert _stored_sid(manager) is None
    # Nothing was thrown away, so the successor's first turn is not made amnesiac.
    assert _KEY not in manager._lifecycle_boundary()._suppress_replay


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_turn",
    [FirstTurnState.NOTHING_ARMED, FirstTurnState.RESUMED],
    ids=["a-turn-claimed-it", "a-resumed-conversation"],
)
async def test_a_session_with_a_conversation_keeps_its_resume_pointer(
    first_turn: FirstTurnState,
) -> None:
    manager = _manager_with(first_turn)

    assert await manager.reset(_KEY, skip_if_busy=True, forget_unused_start=True) is True

    assert _stored_sid(manager) == "sid-of-the-start"


@pytest.mark.asyncio
async def test_a_plain_reset_keeps_the_pointer_of_an_unused_start() -> None:
    manager = _manager_with(FirstTurnState.FRESH)

    assert await manager.reset(_KEY, skip_if_busy=True) is True

    assert _stored_sid(manager) == "sid-of-the-start"


@pytest.mark.asyncio
async def test_a_declined_reset_keeps_the_pointer() -> None:
    manager = _manager_with(FirstTurnState.FRESH)
    await manager._sessions[_KEY].semaphore.acquire()  # a turn holds the session

    assert await manager.reset(_KEY, skip_if_busy=True, forget_unused_start=True) is False

    assert _stored_sid(manager) == "sid-of-the-start"


_NO_SERVERS: dict[str, Any] = {
    "mcpServers": [],
    "_meta": {"kiro": {"customAgents": [{"id": "lite", "tools": [], "includeMcpJson": False}]}},
}


@pytest.fixture
def runtime(tmp_path, monkeypatch) -> AcpRuntime:
    monkeypatch.setattr(runtime_session_load, "_declared_governed", lambda: False)
    return AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off", acp_backend=ACP_BACKEND_KAS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("switched_off", "ceiling"),
    [(True, 0.0), (False, None)],
    ids=["mcp-switched-off", "mcp-on"],
)
async def test_the_drain_holds_for_a_report_only_where_one_can_come(
    runtime: AcpRuntime, switched_off: bool, ceiling: float | None
) -> None:
    runtime._mcp_switched_off = switched_off
    handle = MagicMock()
    handle.drain_init = AsyncMock()

    await runtime._wait_managed_mcp(handle, _NO_SERVERS, "lite", 1.0, 0, StartPriority.FOREGROUND)

    handle.drain_init.assert_awaited_once_with(stale_report_frames=0, no_report_ceiling=ceiling)
