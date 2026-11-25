"""The MCP Servers routes tell the open chats about the change they just wrote.

A KAS session takes an MCP config change at its next turn only when it is marked
(``acp/session_provider_mcp_reinject.py``): nothing in that engine watches the agent
file. Each route that switches a server or a tool on or off, removes one or registers
one therefore marks every open and pre-warmed session that applies a change at its next
turn, and bumps the session manager's warm-pool fence. It resets nothing, and a kiro-cli
session, which watches its agent file, is not marked. A write that did not land marks
nothing.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from handlers_mcp_coverage_helpers import sandbox_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _request, _write_global

from kiro_crew.dashboard.handlers import mcp as mcp_mod


class _Sessions:
    """The session manager a route reaches: one KAS chat, one kiro-cli chat, one warm KAS."""

    final_drain_started = False

    def __init__(self) -> None:
        self.kas = SimpleNamespace(
            mcp_config_wire_reinject=True, mark_mcp_config_changed=MagicMock()
        )
        self.kiro = SimpleNamespace(mcp_config_hot_reload=True, mark_mcp_config_changed=MagicMock())
        self.warm = SimpleNamespace(
            mcp_config_wire_reinject=True, mark_mcp_config_changed=MagicMock()
        )
        self.mark_mcp_config_changed = MagicMock()

    def active_providers(self) -> list[Any]:
        return [self.kas, self.kiro]

    def warm_providers(self) -> list[Any]:
        return [self.warm]

    def marks(self) -> tuple[int, int, int, int]:
        """(KAS chat, kiro-cli chat, warm KAS, pool fence) call counts."""
        return (
            self.kas.mark_mcp_config_changed.call_count,
            self.kiro.mark_mcp_config_changed.call_count,
            self.warm.mark_mcp_config_changed.call_count,
            self.mark_mcp_config_changed.call_count,
        )


def _state(sessions: _Sessions) -> SimpleNamespace:
    return SimpleNamespace(owner_id="", _background_tasks=set(), sessions=sessions)


@pytest.fixture(autouse=True)
def no_companion(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """No capability manager, as on a vanilla machine (remove and apply ask for one)."""
    from kiro_crew.dashboard.handlers import _shared

    _floor_monkeypatch.setattr(
        _shared, "_capability_manager", lambda: MagicMock(**{"available.return_value": False})
    )


@pytest.fixture
def apply_seams(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """What ``POST /api/mcp/apply`` needs beyond the sandbox: the server's spec and a rebuild.

    Only the apply tests take it: ``_find_server_spec_anywhere`` is imported by value
    into ``mcp_custom``, which the register route loads on first use, so a patch live
    during that first import would outlast this test.
    """
    monkeypatch.setattr(mcp_mod, "_find_server_spec_anywhere", lambda n: {"command": "x"})
    rebuild = MagicMock(return_value=(Path(), True))
    monkeypatch.setattr(mcp_mod, "rebuild_agent_config_reporting", rebuild)
    return rebuild


#: (route, request body, request keyword arguments) for each switch, remove and register.
_WRITES = {
    "toggle off": (mcp_mod.api_mcp_toggle, {"name": "srv", "enabled": False}, {}),
    "toggle on": (mcp_mod.api_mcp_toggle, {"name": "srv", "enabled": True}, {}),
    "toggle a tool": (
        mcp_mod.api_mcp_toggle_tool,
        {"server": "srv", "tool": "t", "enabled": False},
        {},
    ),
    "toggle all": (mcp_mod.api_mcp_toggle_all, {"enabled": False}, {}),
    "remove": (mcp_mod.api_mcp_remove, {"name": "srv"}, {}),
    "register": (
        mcp_mod.api_mcp_server_detail,
        {"command": "node"},
        {"match_info": {"name": "srv"}, "method": "PUT"},
    ),
    "unregister": (
        mcp_mod.api_mcp_server_detail,
        None,
        {"match_info": {"name": "srv"}, "method": "DELETE"},
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("write", sorted(_WRITES))
async def test_a_landed_write_marks_every_session_that_takes_it_next_turn(
    write: str, sandbox: SimpleNamespace
) -> None:
    route, body, kwargs = _WRITES[write]
    _write_global(sandbox, {"srv": {"command": "x"}})
    sessions = _Sessions()
    resp = await route(_request(body, state=_state(sessions), **kwargs))
    assert resp.status == 200
    assert sessions.marks() == (1, 0, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_an_apply_marks_every_session_that_takes_it_next_turn(
    enabled: bool, sandbox: SimpleNamespace, apply_seams: MagicMock
) -> None:
    """The MCP Servers tab's Apply, switching the Kiro Crew badge on or off."""
    _write_global(sandbox, {"srv": {"command": "x"}})
    sessions = _Sessions()
    body = {"changes": [{"name": "srv", "kirocrew": enabled, "kiroGlobal": True}]}
    resp = await mcp_mod.api_mcp_apply(_request(body, state=_state(sessions)))
    assert resp.status == 200
    assert sessions.marks() == (1, 0, 1, 1)


@pytest.mark.asyncio
async def test_a_refused_write_marks_nothing(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.mcp_discovery as disc

    monkeypatch.setattr(disc, "list_servers", lambda *a, **k: [])  # configured nowhere
    _write_global(sandbox, {})
    sessions = _Sessions()
    resp = await mcp_mod.api_mcp_toggle(
        _request({"name": "ghost", "enabled": False}, state=_state(sessions))
    )
    assert resp.status == 404
    assert sessions.marks() == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_an_apply_whose_spec_was_not_rewritten_marks_nothing(
    sandbox: SimpleNamespace, apply_seams: MagicMock
) -> None:
    """The open chats would re-load the same spec: there is nothing new to take."""
    apply_seams.return_value = (Path(), False)
    _write_global(sandbox, {"srv": {"command": "x"}})
    sessions = _Sessions()
    body = {"changes": [{"name": "srv", "kirocrew": True, "kiroGlobal": True}]}
    resp = await mcp_mod.api_mcp_apply(_request(body, state=_state(sessions)))
    assert resp.status == 200
    assert sessions.marks() == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_a_mark_that_fails_still_answers_the_write(sandbox: SimpleNamespace) -> None:
    """The write already landed, so the route says so even when no session can be read."""
    _write_global(sandbox, {"srv": {"command": "x"}})
    state = SimpleNamespace(owner_id="", _background_tasks=set(), sessions=SimpleNamespace())
    resp = await mcp_mod.api_mcp_toggle(_request({"name": "srv", "enabled": False}, state=state))
    assert resp.status == 200
    assert sandbox.synced == [("srv", False, False)]
