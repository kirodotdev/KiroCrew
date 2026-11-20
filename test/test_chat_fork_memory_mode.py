"""A fork may keep its parent's memory mode or take a stricter one, never a looser one.

The order is ``execution_context.MEMORY_MODES`` (persistent, incognito, temporary).
Both fork routes reach the rule through ``chat_fork.fork_slot``: the dashboard's
``POST /api/chat/slots/{slot}/fork`` and the agent's ``session_fork``. "Keep this
chat" (``.../promote``) is the one route that loosens a mode, and it ignores the field.
"""

from __future__ import annotations

import asyncio
from itertools import product
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_fork, create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
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


@pytest.mark.parametrize("child_mode", MEMORY_MODES)
def test_session_fork_takes_the_mode_it_is_given(tmp_path, _session_control, child_mode) -> None:
    """A session-control caller is always persistent (``ephemeral_caller``), and so is
    any source it may read (``ephemeral_target``), so every mode is a keep or a tighten."""
    state = _make_state(tmp_path)
    caller = _caller(state)

    result = asyncio.run(
        sc.fork_session(state, caller_session_key=slot_history_key(caller), memory_mode=child_mode)
    )

    assert result["memory_mode"] == child_mode
    child = state.get_slot(result["target"])
    assert child is not None and child.memory_mode == child_mode
    assert (f"dashboard:{child.key}" in state._restricted_keys) is (child_mode != "persistent")


def test_session_fork_without_the_field_keeps_the_sources_mode(tmp_path, _session_control):
    state = _make_state(tmp_path)
    caller = _caller(state)

    result = asyncio.run(sc.fork_session(state, caller_session_key=slot_history_key(caller)))

    assert result["memory_mode"] == "persistent"


def test_session_fork_refuses_a_looser_mode_under_the_forks_own_code(
    tmp_path, _session_control, monkeypatch
):
    """The rule is the fork core's, so the agent route reports the same code as the
    dashboard route. Reached here with a source whose frozen execution is stricter
    than the mode asked for."""
    state = _make_state(tmp_path)
    caller = _caller(state)
    real_resolve = chat_fork.resolve_fork_source

    async def _incognito_source(*args, **kwargs):
        source = await real_resolve(*args, **kwargs)
        return chat_fork.ForkSource(
            slot=source.slot,
            execution=source.execution.with_mode("incognito"),
            identity=source.identity,
        )

    monkeypatch.setattr(sc, "resolve_fork_source", _incognito_source)
    before = set(state._slots)
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.fork_session(
                state, caller_session_key=slot_history_key(caller), memory_mode="persistent"
            )
        )

    assert exc.value.code == "fork_memory_mode_looser"
    assert exc.value.status == 409
    assert set(state._slots) == before


def test_session_fork_refuses_an_unknown_mode(tmp_path, _session_control):
    state = _make_state(tmp_path)
    caller = _caller(state)
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.fork_session(state, caller_session_key=slot_history_key(caller), memory_mode="x")
        )
    assert exc.value.code == "invalid_memory_mode"
    assert exc.value.status == 400


# ── the schema and the tool ─────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", MEMORY_MODES)
def test_the_schema_accepts_each_mode(mode) -> None:
    assert validate_tool_args({"memory_mode": mode}, SESSION_FORK_SCHEMA)["memory_mode"] == mode


@pytest.mark.parametrize("value", ["sideways", 7, True])
def test_the_schema_refuses_anything_else(value) -> None:
    with pytest.raises(ValidationError):
        validate_tool_args({"memory_mode": value}, SESSION_FORK_SCHEMA)


def test_the_tool_schema_offers_the_same_modes_as_the_validator() -> None:
    tool = next(t for t in TABLE if t.name == "session_fork")
    assert tool.schema["properties"]["memory_mode"]["enum"] == list(MEMORY_MODES)


_OK = {
    "ok": True,
    "target": "chat-9",
    "title": "↳ Fork of Gamma review",
    "source": "chat-1",
    "messages": 12,
    "folder_id": None,
    "memory_mode": "incognito",
}


def _tool(args: dict) -> tuple[str, InMemoryDashboardClient]:
    dash = InMemoryDashboardClient({"POST /api/session-control/fork": _OK})
    caller = Caller.strict("dashboard:chat-1-100")
    return TABLE.call("session_fork", args, ToolContext(dash, caller)), dash


def test_the_tool_forwards_the_mode_and_names_it_in_the_reply() -> None:
    out, dash = _tool({"memory_mode": "incognito"})
    assert dash.requests[-1].body == {"source": "", "title": "", "memory_mode": "incognito"}
    assert "(↳ Fork of Gamma review) as an incognito session carrying 12 message(s)" in out


def test_the_tool_sends_no_mode_when_none_is_asked_for() -> None:
    out, dash = _tool({})
    assert "memory_mode" not in dash.requests[-1].body
    assert "incognito" not in out
