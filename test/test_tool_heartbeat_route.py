"""``/api/session-keepalive`` carries a long tool's heartbeat to its own call's timer.

The body names the harness call the tool runs under (``tool_call_id``) and, for a
tool that waits on sub-agents, the sub-agents (``subagent_ids``). The route hands the
call id to the provider, with the sub-agents' own latest activity when they are all
quiet, so a sub-agent call is judged by its sub-agents and not by its own polling.
"""

from __future__ import annotations

import time
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


def _ago(secs: float) -> float:
    """A monotonic stamp *secs* back on the clock the reader itself reads; every
    margin in this file is minutes wide, so the call's own elapsed time is noise."""
    return time.monotonic() - secs  # flake-ok: stamp relative to the reader's own clock


#: A stamp far enough back to be past the model-wait window (1800 s by default).
_LONG_AGO = _ago(10_000.0)


def _child(**overrides) -> SimpleNamespace:
    fields = {
        "parent_session_key": "known",
        "done": False,
        "_awaiting_approval": False,
        "_exec_started": 1.0,
        "_tool_tracker": SimpleNamespace(any_active=False),
        "_last_activity_mono": _LONG_AGO,
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
        "a1": _child(_last_activity_mono=_LONG_AGO),
        "a2": _child(_last_activity_mono=_LONG_AGO + 50.0),
        # Another session's run and a finished one say nothing about this call.
        "x": _child(parent_session_key="other", _last_activity_mono=_LONG_AGO + 800.0),
        "d": _child(done=True, _last_activity_mono=_LONG_AGO + 700.0),
    }
    provider = _Provider()
    live_turn._note_tool_heartbeat(
        _state(children),
        "known",
        provider,
        {"tool_call_id": "tc-wave", "phase": "poll", "subagent_ids": ["a1", "a2", "x", "d", 3]},
    )
    assert provider.beats == [("tc-wave", _LONG_AGO + 50.0)]


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
    children = {"a1": _child(_last_activity_mono=_LONG_AGO), "a2": _child(**busy)}
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


def test_a_member_past_the_old_heartbeat_cap_still_counts_as_moving_now():
    """A wave may name SPAWN_BATCH_MEMBERS_MAX members, so a queued member past the
    first 64 must reach the reader and exempt the parent's call."""
    from kiro_crew.validation_parts.core_agent_schemas import SPAWN_BATCH_MEMBERS_MAX

    ids = [f"a{n}" for n in range(SPAWN_BATCH_MEMBERS_MAX)]
    children = {agent_id: _child(_last_activity_mono=_LONG_AGO) for agent_id in ids}
    children[ids[-1]] = _child(_exec_started=None)
    provider = _Provider()
    live_turn._note_tool_heartbeat(
        _state(children), "known", provider, {"tool_call_id": "tc", "subagent_ids": ids}
    )
    assert provider.beats == [("tc", None)]


def test_a_subagent_running_prompt_hooks_counts_as_moving_now():
    """Prompt hooks run after ``_exec_started`` is set and are bounded by their own
    timeout, so the parent's silence timer must not judge them as child silence."""
    children = {"a1": _child(_last_activity_mono=_LONG_AGO, _prompt_hooks_running=True)}
    provider = _Provider()
    live_turn._note_tool_heartbeat(
        _state(children), "known", provider, {"tool_call_id": "tc", "subagent_ids": ["a1"]}
    )
    assert provider.beats == [("tc", None)]


def test_a_subagent_waiting_on_the_model_within_its_own_window_counts_as_moving_now():
    """A child in a long reasoning think streams nothing and has no tool open, so its
    last activity is older than the parent's sub-agent bound (600 s) while it is
    still inside its own model window (1800 s). The parent's call must not read that
    quiet as the child's silence."""
    quiet_for = 700.0
    children = {"a1": _child(_last_activity_mono=_ago(quiet_for))}
    provider = _Provider()
    live_turn._note_tool_heartbeat(
        _state(children), "known", provider, {"tool_call_id": "tc", "subagent_ids": ["a1"]}
    )
    assert provider.beats == [("tc", None)]


def test_a_subagent_quiet_past_its_model_window_reports_its_stale_stamp():
    """Past ``watchdog.model_silent_probe_secs`` the quiet is not a think the
    window bounds, so the parent's call reads the child's last activity."""
    moved = _ago(1900.0)
    children = {"a1": _child(_last_activity_mono=moved)}
    provider = _Provider()
    live_turn._note_tool_heartbeat(
        _state(children), "known", provider, {"tool_call_id": "tc", "subagent_ids": ["a1"]}
    )
    assert provider.beats == [("tc", moved)]


@pytest.mark.asyncio
async def test_a_wave_heartbeat_loads_no_config_on_the_event_loop(monkeypatch):
    """The keepalive route runs on the gateway's event loop, and a waiting
    ``spawn_sub_agents`` call reaches it about every 30 s naming its children.
    Reading the model-wait window there must not load config.json: a load reads,
    parses and validates the file, and can write it back under the config lock."""
    from kiro_crew.config.loader import KiroCrewConfig

    load = MagicMock(side_effect=AssertionError("KiroCrewConfig.load() on the event loop"))
    monkeypatch.setattr(KiroCrewConfig, "load", load)
    provider = _Provider()
    state = MagicMock()
    state.sessions = _Sessions(provider)
    state.subagents = SimpleNamespace(get=lambda agent_id: {"a1": _child()}.get(agent_id))
    req = MagicMock(spec=web.Request)
    req.headers = {"X-Session-Key": "known"}
    req.app = {"state": state}
    req.can_read_body = True
    req.json = AsyncMock(
        return_value={"tool_call_id": "tc-wave", "phase": "poll", "subagent_ids": ["a1"]}
    )

    resp = await api_session_keepalive(req)

    assert resp.status == 200
    assert load.call_count == 0
    assert provider.beats == [("tc-wave", _LONG_AGO)]


def test_the_model_window_is_the_session_handles_watchdog_snapshot():
    """The window is ``watchdog.model_silent_probe_secs`` as the live handle's
    ``WatchdogSettings`` snapshot holds it, the one a config reload rebinds: at 600 s a
    child quiet 700 s reports its stamp, where the 1800 s default would count it as
    moving. With no live handle the window is that snapshot's own default."""
    from kiro_crew.acp.session_handle_watchdog import WatchdogSettings

    handle = SimpleNamespace(
        rebind_watchdog=lambda *_: None,
        _watchdog=WatchdogSettings(model_silent_probe_secs=600.0),
    )
    provider = _Provider()
    provider._client = SimpleNamespace(_handle=handle)
    moved = _ago(700.0)
    live_turn._note_tool_heartbeat(
        _state({"a1": _child(_last_activity_mono=moved)}),
        "known",
        provider,
        {"tool_call_id": "tc", "subagent_ids": ["a1"]},
    )
    assert provider.beats == [("tc", moved)]
    assert live_turn._model_think_window(_Provider()) == WatchdogSettings().model_silent_probe_secs
