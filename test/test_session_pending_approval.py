"""A session parked on a tool approval reads as waiting, not as working.

The turn stays ``running`` while it waits for a person, so a creator that polls
``session_read_message`` or ``session_status`` needs a separate signal to tell
"wait longer" from "someone has to answer an approval".
"""

from __future__ import annotations

import asyncio
import json

import pytest
from chat_test_helpers import _make_state

from kiro_crew import mcp_dashboard
from kiro_crew.crew_log import emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext


@pytest.fixture(autouse=True)
def _enabled(_floor_monkeypatch):
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _tree_on(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crewhome"))
    _floor_monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    _floor_monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    stp.projection().ensure_seeded()
    yield
    stp.reset_for_tests()


class _Future:
    """The one method the read path asks of an approval future."""

    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def _key(slot) -> str:
    return slot_history_key(slot)


def _parked(state, name: str, creator, *, tool: str = "Run git push", done: bool = False):
    """A child of *creator* whose turn is waiting on one tool approval."""
    slot = state.get_or_create_slot(name)
    slot._created_by = creator.key
    row = slot.append("permission", tool, json.dumps({"request_id": "req-1"}))
    slot.register_approval("req-1", _Future(done), row)
    return slot


def _read(state, caller, target: str) -> dict:
    return sc.read_messages(state, caller_session_key=_key(caller), target=target)


def _status_row(state, caller, target: str) -> dict:
    out = asyncio.run(sc.created_session_status(state, caller_session_key=_key(caller)))
    return {r["target"]: r for r in out["sessions"]}[target]


class TestRead:
    def test_an_open_approval_is_reported_with_its_tool(self, tmp_path):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        _parked(state, "chat-2", caller)

        out = _read(state, caller, "chat-2")

        assert out["pending_approval"] is True
        assert out["pending_approval_tool"] == "Run git push"

    def test_an_answered_approval_is_not_reported(self, tmp_path):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        _parked(state, "chat-2", caller, done=True)

        out = _read(state, caller, "chat-2")

        assert "pending_approval" not in out
        assert "pending_approval_tool" not in out

    def test_a_stale_row_does_not_name_a_newer_prompt(self, tmp_path):
        """An unresolved row from an earlier prompt instance is not the one waiting.

        Same rule as the dashboard card: the row must match the live instance.
        """
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        child = _parked(state, "chat-2", caller, tool="old tool")
        child.register_approval("req-1", _Future(False), {"meta": {"mid": "other"}})

        out = _read(state, caller, "chat-2")

        assert out["pending_approval"] is True
        assert "pending_approval_tool" not in out
        assert out["pending_approval"] == child.to_dict()["pending_approval"]

    def test_a_coordinator_approval_counts_and_names_its_tool(self, tmp_path):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        child = state.get_or_create_slot("chat-2")
        state._pending_approvals["spawn:a"] = {"id": "spawn:a", "slot": child.key, "tool": "spawn"}
        state._approval_futures["spawn:a"] = _Future(False)

        out = _read(state, caller, "chat-2")

        assert out["pending_approval"] is True
        assert out["pending_approval_tool"] == "spawn"

    def test_the_tool_title_is_bounded(self, tmp_path):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        _parked(state, "chat-2", caller, tool="x" * 5000)

        tool = _read(state, caller, "chat-2")["pending_approval_tool"]

        assert len(tool) < 5000
        assert tool.startswith("x" * sc.MAX_PENDING_APPROVAL_TOOL_CHARS)


class TestStatus:
    def test_a_parked_child_row_says_so(self, tmp_path):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        _parked(state, "chat-2", caller)

        row = _status_row(state, caller, "chat-2")

        assert row["pending_approval"] is True
        assert row["pending_approval_tool"] == "Run git push"

    def test_an_ordinary_child_row_is_unchanged(self, tmp_path):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        child = state.get_or_create_slot("chat-2")
        child._created_by = caller.key

        assert "pending_approval" not in _status_row(state, caller, "chat-2")


class TestRendering:
    def _ctx(self, route: str, payload: dict) -> ToolContext:
        return ToolContext(InMemoryDashboardClient({route: payload}), Caller.strict("dashboard:c"))

    def test_read_renders_waiting_instead_of_still_working(self):
        payload = {
            "target": "chat-2",
            "title": "worker",
            "running": True,
            "pending_approval": True,
            "pending_approval_tool": "Run git push",
            "messages": [],
            "total": 0,
        }
        ctx = self._ctx("GET /api/session-control/read", payload)

        text = mcp_dashboard.TABLE.call("session_read_message", {"target": "chat-2"}, ctx)

        assert "waiting on a tool approval: Run git push" in text
        assert "still working" not in text

    def test_status_renders_the_waiting_row(self):
        row = {
            "target": "chat-2",
            "title": "worker",
            "status": "working",
            "queue_depth": 0,
            "pending_approval": True,
        }
        payload = {"tree": "readable", "history": "readable", "sessions": [row]}
        ctx = self._ctx("GET /api/session-control/status", payload)

        text = mcp_dashboard.TABLE.call("session_status", {}, ctx)

        assert "waiting on a tool approval" in text
