"""A fork may keep its parent's memory mode or take a stricter one, never a looser one.

The order is ``execution_context.MEMORY_MODES`` (persistent, incognito, temporary).
The dashboard's ``POST /api/chat/slots/{slot}/fork`` reaches the rule through
``chat_fork.fork_slot``. The agent's ``session_fork`` takes no ``memory_mode``: the
validator refuses the field as unknown, and ``POST /api/session-control/fork`` never
reads it, so an agent's child is always persistent and stays reachable by
``session_send``, ``session_read_message`` and ``session_close``. "Keep this chat"
(``.../promote``) is the one route that loosens a mode, and it ignores the field.
"""

from __future__ import annotations

import asyncio
import json
from itertools import product
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_fork, create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.execution_context import MEMORY_MODES, read_session_execution
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext
from kiro_crew.validation import SESSION_FORK_SCHEMA, ValidationError, validate_tool_args

_PAIRS = list(product(MEMORY_MODES, repeat=2))
_ALLOWED = [(p, c) for p, c in _PAIRS if MEMORY_MODES.index(c) >= MEMORY_MODES.index(p)]
_REFUSED = [(p, c) for p, c in _PAIRS if MEMORY_MODES.index(c) < MEMORY_MODES.index(p)]


def test_the_order_has_three_allowed_tightenings_and_three_refused_loosenings() -> None:
    """Self-check: the parametrized cases below cover every pair, split as expected."""
    # The dashboard's fork menu mirrors this order (website/src/utils/forkMemoryModes.ts).
    assert MEMORY_MODES == (
        "persistent",
        "incognito",
        "temporary",
    )  # flake-ok: contract with the fork menu order
    assert len(_ALLOWED) == 6 and len(_REFUSED) == 3
    assert ("incognito", "persistent") in _REFUSED
    assert ("persistent", "temporary") in _ALLOWED


@pytest.fixture
def mock_sel(monkeypatch) -> MagicMock:
    recorder = MagicMock()
    monkeypatch.setattr(chat_fork, "sel", lambda: recorder)
    monkeypatch.setattr("kiro_crew.dashboard.chat_fork_memory_mode.sel", lambda: recorder)
    return recorder


def _parent(state, mode: str):
    slot = state.get_or_create_slot("src", memory_mode=mode)
    slot.append("user", "what broke", "msg msg-u")
    slot.append("assistant", "the MCP server did not load", "msg msg-a")
    slot.drain()
    return slot


async def _post(state, path: str, payload: dict) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(path, json=payload)
        return resp.status, await resp.json()


# ── the dashboard route ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_mode,child_mode", _ALLOWED)
async def test_a_fork_may_keep_or_tighten_the_parents_mode(
    tmp_path, mock_sel, parent_mode, child_mode
) -> None:
    state = _make_state(tmp_path)
    _parent(state, parent_mode)

    status, data = await _post(state, "/api/chat/slots/src/fork", {"memory_mode": child_mode})

    assert status == 200, data
    assert data["memory_mode"] == child_mode
    child = state._slots[data["key"]]
    assert child.memory_mode == child_mode
    # Restricted at birth, so no memory gate ever sees the child as persistent.
    restricted = child_mode != "persistent"
    assert (f"dashboard:{data['key']}" in state._restricted_keys) is restricted
    assert child.blocks_reads is (child_mode == "temporary")
    bound = read_session_execution(effective_session_key(child))
    assert bound is not None and bound.memory_mode == child_mode
    assert [m["content"] for m in child.messages] == ["what broke", "the MCP server did not load"]
    allowed = [c[1] for c in mock_sel.log_api_access.call_args_list if c[1]["outcome"] == "allowed"]
    assert len(allowed) == 1 and f"memory_mode={child_mode}" in allowed[0]["resources"]


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_mode,child_mode", _REFUSED)
async def test_a_fork_may_not_loosen_the_parents_mode(
    tmp_path, mock_sel, monkeypatch, parent_mode, child_mode
) -> None:
    state = _make_state(tmp_path)
    _parent(state, parent_mode)
    before = set(state._slots)

    def _no_read(*_args, **_kwargs):
        raise AssertionError("a refused mode must be decided before the transcript is read")

    monkeypatch.setattr(chat_fork, "read_consistent_transcript", _no_read)

    status, data = await _post(state, "/api/chat/slots/src/fork", {"memory_mode": child_mode})

    assert status == 409
    assert data["code"] == "fork_memory_mode_looser"
    assert parent_mode in data["error"] and child_mode in data["error"]
    assert set(state._slots) == before, "a refused fork allocates no child"
    denied = [c[1] for c in mock_sel.log_api_access.call_args_list if c[1]["outcome"] == "denied"]
    assert len(denied) == 1
    assert denied[0]["operation"] == "chat.slot_fork"
    assert f"memory_mode={parent_mode},requested={child_mode}" in denied[0]["resources"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["sideways", "", 7, True, ["incognito"]])
async def test_a_value_outside_the_modes_is_refused(tmp_path, mock_sel, value) -> None:
    state = _make_state(tmp_path)
    _parent(state, "persistent")
    before = set(state._slots)

    status, data = await _post(state, "/api/chat/slots/src/fork", {"memory_mode": value})

    assert status == 400
    assert data["code"] == "invalid_memory_mode"
    assert set(state._slots) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_mode", MEMORY_MODES)
async def test_without_the_field_the_child_keeps_the_parents_mode(
    tmp_path, mock_sel, parent_mode
) -> None:
    state = _make_state(tmp_path)
    _parent(state, parent_mode)

    status, data = await _post(state, "/api/chat/slots/src/fork", {})

    assert status == 200
    assert data["memory_mode"] == parent_mode
    assert state._slots[data["key"]].memory_mode == parent_mode


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_mode", ["incognito", "temporary"])
async def test_keep_this_chat_ignores_the_field_and_still_makes_a_persistent_copy(
    tmp_path, mock_sel, parent_mode
) -> None:
    """Promotion forces its body empty, so the new field cannot narrow or block it."""
    state = _make_state(tmp_path)
    _parent(state, parent_mode)

    status, data = await _post(state, "/api/chat/slots/src/promote", {"memory_mode": "temporary"})

    assert status == 200, data
    assert data["memory_mode"] == "persistent"
    child = state._slots[data["key"]]
    assert child.memory_mode == "persistent"
    assert f"dashboard:{data['key']}" not in state._restricted_keys


# ── the agent route: ``session_fork`` ───────────────────────────────────────────


@pytest.fixture
def _session_control(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    monkeypatch.setattr(sc, "sel", lambda: MagicMock())
    monkeypatch.setattr(chat_fork, "sel", lambda: MagicMock())
    monkeypatch.setattr("kiro_crew.dashboard.chat_fork_memory_mode.sel", lambda: MagicMock())
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


def _caller(state):
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "question", "msg msg-u")
    slot.append("assistant", "answer", "msg msg-a")
    slot.drain()
    _save_slot_to_history(state, slot, closed=False)
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    return slot


_EPHEMERAL = [m for m in MEMORY_MODES if m != "persistent"]


def test_session_fork_without_the_field_keeps_the_sources_mode(tmp_path, _session_control):
    state = _make_state(tmp_path)
    caller = _caller(state)

    result = asyncio.run(sc.fork_session(state, caller_session_key=slot_history_key(caller)))

    assert state.get_slot(result["target"]).memory_mode == "persistent"


# ── the schema and the tool ─────────────────────────────────────────────────────


@pytest.mark.parametrize("value", MEMORY_MODES)
def test_the_schema_refuses_memory_mode_as_an_unknown_field(value) -> None:
    """Even ``persistent``: the agent route has no such field, so a private child
    cannot be asked for and the child keeps the persistent source's mode."""
    with pytest.raises(ValidationError) as exc:
        validate_tool_args({"memory_mode": value}, SESSION_FORK_SCHEMA)
    assert exc.value.field == "memory_mode"
    assert "unknown field" in exc.value.message


def test_the_tool_schema_has_no_memory_mode() -> None:
    tool = next(t for t in TABLE if t.name == "session_fork")
    assert "memory_mode" not in tool.schema["properties"]


_OK = {
    "ok": True,
    "target": "chat-9",
    "title": "↳ Fork of Gamma review",
    "source": "chat-1",
    "messages": 12,
    "folder_id": None,
}


def _tool(args: dict) -> tuple[str, InMemoryDashboardClient]:
    dash = InMemoryDashboardClient({"POST /api/session-control/fork": _OK})
    caller = Caller.strict("dashboard:chat-1-100")
    return TABLE.call("session_fork", args, ToolContext(dash, caller)), dash


@pytest.mark.parametrize("child_mode", _EPHEMERAL)
def test_the_tool_refuses_a_mode_before_the_wire(child_mode) -> None:
    out, dash = _tool({"memory_mode": child_mode})
    assert dash.requests == [], "nothing is sent for a field the tool does not take"
    assert "memory_mode" in out and "unknown field" in out
    assert "Forked" not in out


def test_the_tool_sends_no_mode_when_none_is_asked_for() -> None:
    out, dash = _tool({})
    assert "memory_mode" not in dash.requests[-1].body
    assert "incognito" not in out


# ── down to the wire: the tool's request answered by the real route ─────────────


def _wire(state, caller, body: dict) -> tuple[int, dict]:
    """POST ``body`` to ``api_session_control_fork`` as the tool's loopback would."""
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/fork"
    request.method = "POST"
    request.headers = {"X-Session-Key": slot_history_key(caller)}
    request.get = lambda key, default=None: (
        True if key in ("internal_auth", "peer_verified") else default
    )

    async def _json():
        return body

    request.json = _json
    resp = asyncio.run(handlers_sc.api_session_control_fork(request))
    return resp.status, json.loads(resp.body.decode())


@pytest.mark.parametrize("child_mode", _EPHEMERAL)
def test_the_route_ignores_the_field_and_forks_persistent(
    tmp_path, _session_control, child_mode
) -> None:
    """A request that never passed the tool still gets a persistent child: the
    route does not read the field, so an agent never gets a child it cannot reach."""
    state = _make_state(tmp_path)
    caller = _caller(state)

    status, data = _wire(state, caller, {"memory_mode": child_mode})

    assert status == 200, data
    child = state.get_slot(data["target"])
    assert child is not None and child.memory_mode == "persistent"
    assert f"dashboard:{child.key}" not in state._restricted_keys


def test_the_tool_and_route_together_fork_persistent(tmp_path, _session_control) -> None:
    """Every layer: the tool's validator and body, the HTTP handler, the session-control
    core and the fork core, with the tool's own request body on the wire."""
    state = _make_state(tmp_path)
    caller = _caller(state)
    seen: list[int] = []

    def _route(request):
        status, data = _wire(state, caller, request.body)
        seen.append(status)
        return data

    dash = InMemoryDashboardClient({"POST /api/session-control/fork": _route})
    out = TABLE.call("session_fork", {}, ToolContext(dash, Caller.strict("dashboard:chat-1-100")))

    assert seen == [200], out
    assert "memory_mode" not in dash.requests[-1].body
    child_key = next(k for k in state._slots if k != caller.key)
    assert state._slots[child_key].memory_mode == "persistent"
    assert f"dashboard:{child_key}" not in state._restricted_keys
    assert f"Forked `{caller.key}` into `{child_key}`" in out
