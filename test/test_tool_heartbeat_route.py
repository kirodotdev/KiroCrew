"""``/api/session-keepalive`` carries a long tool's heartbeat to its own call's timer.

The body names the harness call the tool runs under (``tool_call_id``) and, for a
tool that waits on sub-agents, the sub-agents (``subagent_ids``). The route hands the
call id to the provider, with the sub-agents' own latest activity when they are all
quiet, so a sub-agent call is judged by its sub-agents and not by its own polling.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers.sessions import api_session_keepalive
from kiro_crew.dashboard.handlers.sessions_parts import live_turn


class _Provider:
    def __init__(self) -> None:
        self.beats: list[tuple[str, float | None]] = []

    def touch_activity(self) -> None:
        return None

    def note_tool_heartbeat(self, tool_call_id: str = "", *, activity_at=None) -> int:
        self.beats.append((tool_call_id, activity_at))
        return 1


class _Sessions:
    def __init__(self, provider) -> None:
        self._provider = provider

    def get_provider(self, key):
        return self._provider if key == "known" else None


def _child(**overrides) -> SimpleNamespace:
    fields = {
        "parent_session_key": "known",
        "done": False,
        "_awaiting_approval": False,
        "_exec_started": 1.0,
        "_tool_tracker": SimpleNamespace(any_active=False),
        "_last_activity_mono": 100.0,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _state(children: dict | None = None) -> SimpleNamespace:
    manager = SimpleNamespace(get=lambda agent_id: (children or {}).get(agent_id))
    return SimpleNamespace(subagents=manager)


@pytest.mark.asyncio
async def test_the_route_hands_the_named_call_to_the_provider():
    provider = _Provider()
    state = MagicMock()
    state.sessions = _Sessions(provider)
    req = MagicMock(spec=web.Request)
    req.headers = {"X-Session-Key": "known"}
    req.app = {"state": state}
    req.can_read_body = True
    req.json = AsyncMock(return_value={"tool_call_id": "tc-wait", "phase": "waiting"})

    resp = await api_session_keepalive(req)

    assert resp.status == 200
    assert provider.beats == [("tc-wait", None)]


def test_an_oversized_or_non_string_id_names_no_call():
    provider = _Provider()
    live_turn._note_tool_heartbeat(_state(), "known", provider, {"tool_call_id": "x" * 300})
    live_turn._note_tool_heartbeat(_state(), "known", provider, {"tool_call_id": 7})
    assert provider.beats == [("", None), ("", None)]


def test_a_provider_with_no_per_call_timer_is_left_alone():
    live_turn._note_tool_heartbeat(_state(), "known", object(), {"tool_call_id": "tc"})


def test_a_wave_heartbeat_carries_its_quiet_subagents_latest_activity():
    children = {
        "a1": _child(_last_activity_mono=100.0),
        "a2": _child(_last_activity_mono=150.0),
        # Another session's run and a finished one say nothing about this call.
        "x": _child(parent_session_key="other", _last_activity_mono=900.0),
        "d": _child(done=True, _last_activity_mono=800.0),
    }
    provider = _Provider()
    live_turn._note_tool_heartbeat(
        _state(children),
        "known",
        provider,
        {"tool_call_id": "tc-wave", "phase": "poll", "subagent_ids": ["a1", "a2", "x", "d", 3]},
    )
    assert provider.beats == [("tc-wave", 150.0)]


@pytest.mark.parametrize(
    "busy",
    [
        {"_tool_tracker": SimpleNamespace(any_active=True)},
        {"_awaiting_approval": True},
        {"_exec_started": None},
    ],
    ids=["mid-tool", "waiting-on-a-person", "not-started"],
)
def test_a_subagent_its_own_watchdog_bounds_counts_as_moving_now(busy):
    """A sub-agent mid-tool, waiting on approval or not started yet is bounded by
    its own watchdog or its admission, so the parent's call counts it as moving."""
    children = {"a1": _child(_last_activity_mono=100.0), "a2": _child(**busy)}
    provider = _Provider()
    live_turn._note_tool_heartbeat(
        _state(children), "known", provider, {"tool_call_id": "tc", "subagent_ids": ["a1", "a2"]}
    )
    assert provider.beats == [("tc", None)]


def test_with_nothing_readable_the_heartbeat_itself_is_the_activity():
    provider = _Provider()
    live_turn._note_tool_heartbeat(
        SimpleNamespace(), "known", provider, {"tool_call_id": "tc", "subagent_ids": ["a1"]}
    )
    assert provider.beats == [("tc", None)]


def test_the_shared_runtime_provider_forwards_the_heartbeat_to_its_handle():
    from kiro_crew.acp.session_provider import AcpSessionProvider

    provider = AcpSessionProvider.__new__(AcpSessionProvider)
    provider._handle = MagicMock()
    provider._handle.note_tool_heartbeat.return_value = 1
    assert provider.note_tool_heartbeat("tc", activity_at=5.0) == 1
    provider._handle.note_tool_heartbeat.assert_called_once_with("tc", activity_at=5.0)
