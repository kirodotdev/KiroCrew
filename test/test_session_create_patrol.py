"""A ``session_create`` is a dispatch, so it leaves the caller patrolled or says why.

The create route arms the same default work-ledger patrol a ledger bind arms,
through the same outside-arm authorizer. Pinned here:

* an ordinary caller that runs a work ledger and has no loop gets one armed;
* a caller with no work ledger gets nothing armed, and any stopped row stays;
* a caller whose loop the system stopped gets it replaced;
* a caller with an active loop is left alone;
* a crew/member caller is refused (the outside-arm gate is unchanged), and no
  loop is written on it;
* a dry run arms nothing;
* the MCP reply always ends with the patrol outcome.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew import autonudge, autonudge_authz, conductor_patrol, mcp_dashboard
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.monitoring.models import MonitorOutcome


class _Svc:
    def __init__(self) -> None:
        self.loops: dict[str, Any] = {}
        self.added: list[dict[str, Any]] = []

    def get_by_slot(self, slot_key: str) -> Any:
        return self.loops.get(slot_key)

    def _observes_work_ledger(self, loop: Any) -> bool:
        return getattr(loop, "watch", "") == "work-ledger"

    async def add(self, **kw: Any) -> Any:
        existing = self.loops.get(kw["slot_key"])
        if existing is not None and (existing.active or not kw.get("replace_stopped")):
            raise autonudge.MonitorUpdateConflict("session already has an automation")
        self.added.append(kw)
        loop = SimpleNamespace(
            id=f"loop-{len(self.added)}",
            slot_key=kw["slot_key"],
            watch=kw.get("watch", ""),
            active=True,
        )
        self.loops[kw["slot_key"]] = loop
        return loop


@pytest.fixture(autouse=True)
def _env(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    _floor_monkeypatch.setattr(
        autonudge_authz, "sel", lambda: SimpleNamespace(log_tool_invocation=lambda **kw: None)
    )
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


@pytest.fixture
def svc(monkeypatch) -> _Svc:
    store = _Svc()
    monkeypatch.setattr(autonudge, "get_instance", lambda: store)
    return store


@pytest.fixture
def ledger(monkeypatch) -> None:
    """The caller runs a work ledger, as a conductor does before it dispatches."""
    from kiro_crew.dashboard.handlers import work_ledger as ledger_routes

    async def _holds(key: str) -> bool:
        return key == "chat-1"

    monkeypatch.setattr(ledger_routes, "holds_ledger", _holds)


def _create(tmp_path, *, mode: str = "", body: dict[str, Any] | None = None, prep=None):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    if mode:
        caller.mode = mode
    if prep is not None:
        prep(state)
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/create"
    request.method = "POST"
    request.headers = {"X-Session-Key": slot_history_key(caller)}
    request.get = lambda key, default=None: (
        True if key in ("internal_auth", "peer_verified") else default
    )

    async def _json():
        return body or {"title": "worker"}

    request.json = _json
    resp = asyncio.run(handlers_sc.api_session_control_create(request))
    return resp.status, json.loads(resp.body.decode()), state


def test_an_ordinary_caller_with_no_loop_gets_one_armed(tmp_path, svc, ledger):
    status, body, _state = _create(tmp_path)

    assert status == 200, body
    assert body["patrol"] == conductor_patrol.ARMED
    assert body["patrol_reason"] == conductor_patrol.REASON_NO_LOOP
    assert "patrol_note" not in body
    assert [a["slot_key"] for a in svc.added] == ["chat-1"]
    assert svc.added[0]["watch"] == "work-ledger"
    assert svc.added[0]["default_patrol"] is True


def test_the_create_arm_is_audited_as_a_session_create(tmp_path, svc, ledger, monkeypatch):
    seen: list[str] = []
    real = autonudge_authz.authorize_and_add_nudge

    async def _spy(**kw: Any) -> Any:
        seen.append(kw["source"])
        return await real(**kw)

    monkeypatch.setattr(autonudge_authz, "authorize_and_add_nudge", _spy)
    status, body, _state = _create(tmp_path)

    assert status == 200, body
    assert seen == ["session-create"]


def test_a_system_stopped_loop_is_replaced(tmp_path, svc, ledger):
    svc.loops["chat-1"] = SimpleNamespace(
        id="done",
        slot_key="chat-1",
        active=False,
        watch="",
        default_patrol=False,
        stopped_reason="monitor_terminal",
        monitor=SimpleNamespace(outcome=MonitorOutcome.SUCCESS, stopped_reason="monitor_terminal"),
    )
    status, body, _state = _create(tmp_path)

    assert status == 200, body
    assert body["patrol"] == conductor_patrol.ARMED
    assert body["patrol_reason"] == conductor_patrol.REASON_REPLACED
    assert svc.loops["chat-1"].active is True


def test_an_active_loop_is_left_alone(tmp_path, svc, ledger):
    live = SimpleNamespace(id="live", slot_key="chat-1", active=True, watch="work-ledger")
    svc.loops["chat-1"] = live
    status, body, _state = _create(tmp_path)

    assert status == 200, body
    assert body["patrol"] == conductor_patrol.EXISTING
    assert svc.added == []
    assert svc.loops["chat-1"] is live


@pytest.mark.parametrize("mode", ["member", "crew"])
def test_a_crew_or_member_caller_is_refused_and_gets_no_loop(
    tmp_path, svc, ledger, monkeypatch, mode
):
    """The route cannot prove the turn is the slot's own: an outside arm on a
    crew/member slot stays refused, and nothing is written on it."""
    # The create itself is not what this pins; let it succeed for any caller.
    real_create = sc.create_session

    async def _create_any(state, **kw: Any) -> dict[str, Any]:
        return {"ok": True, "target": "chat-9", "title": "worker"}

    monkeypatch.setattr(sc, "create_session", _create_any)
    status, body, _state = _create(tmp_path, mode=mode)
    monkeypatch.setattr(sc, "create_session", real_create)

    assert status == 200, body
    assert body["patrol"] == conductor_patrol.REFUSED
    assert body["patrol_reason"] == conductor_patrol.REASON_OUTSIDE_ARM
    assert body["patrol_note"] == conductor_patrol.ARM_YOURSELF_NOTE
    assert svc.added == []
    assert "chat-1" not in svc.loops


def test_a_caller_with_no_ledger_gets_no_patrol_and_keeps_its_stopped_row(tmp_path, svc):
    """No ledger, nothing to watch: a work-ledger loop would only fire on its timer."""
    merged = SimpleNamespace(
        id="pr-watch",
        slot_key="chat-1",
        active=False,
        watch="",
        default_patrol=False,
        stopped_reason="monitor_terminal",
        monitor=SimpleNamespace(outcome=MonitorOutcome.SUCCESS, stopped_reason="monitor_terminal"),
    )
    svc.loops["chat-1"] = merged
    status, body, _state = _create(tmp_path)

    assert status == 200, body
    assert body["patrol"] == conductor_patrol.UNSUPPORTED
    assert body["patrol_reason"] == conductor_patrol.REASON_NO_LEDGER
    assert body["patrol_note"] == conductor_patrol.NO_LEDGER_NOTE
    assert svc.added == []
    assert svc.loops["chat-1"] is merged


def test_a_caller_with_no_ledger_and_an_active_loop_is_told_nothing(tmp_path, svc):
    """Its active loop (a session or PR watch) is the patrol: no arm, no note."""
    live = SimpleNamespace(id="pr", slot_key="chat-1", active=True, watch="")
    svc.loops["chat-1"] = live
    status, body, _state = _create(tmp_path)

    assert status == 200, body
    assert body["patrol"] == conductor_patrol.EXISTING
    assert body["patrol_reason"] == conductor_patrol.REASON_ACTIVE_OTHER
    assert "patrol_note" not in body
    assert svc.loops["chat-1"] is live


def test_a_dry_run_arms_nothing(tmp_path, svc, ledger):
    status, body, _state = _create(tmp_path, body={"title": "worker", "dry_run": True})

    assert status == 200, body
    assert "patrol" not in body
    assert svc.added == []


@pytest.mark.parametrize(
    ("resp_patrol", "expected"),
    [
        (
            {"patrol": "armed", "patrol_reason": "no-loop"},
            " patrol=armed (no-loop)",
        ),
        (
            {
                "patrol": "refused",
                "patrol_reason": "outside-arm-refused",
                "patrol_note": conductor_patrol.ARM_YOURSELF_NOTE,
            },
            f" patrol=refused (outside-arm-refused). {conductor_patrol.ARM_YOURSELF_NOTE}",
        ),
        ({}, f" patrol=unknown. {conductor_patrol.ARM_YOURSELF_NOTE}"),
    ],
)
def test_the_session_create_reply_ends_with_the_patrol_outcome(resp_patrol, expected):
    client = MagicMock()
    client.post.return_value = {"ok": True, "target": "chat-9", "title": "worker", **resp_patrol}
    ctx = SimpleNamespace(client=client, caller_key="dashboard:chat-1")

    out = mcp_dashboard._run_session_create({"title": "worker"}, ctx)

    assert out.endswith(expected), out
