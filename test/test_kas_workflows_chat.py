"""``kas_workflows.chat``: the switch a chat starts with, its tools, and the rewind guard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.config import live
from kiro_crew.kas_workflows import chat
from kiro_crew.kas_workflows import router as router_mod
from kiro_crew.kas_workflows import wire


def _cfg(backend: str = ACP_BACKEND_KAS, on: bool = True) -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(acp_backend=backend, kas_workflows=on))


# ── The switch ──


def test_the_switch_reads_the_live_config(monkeypatch):
    monkeypatch.setattr(live, "snapshot", lambda: _cfg(on=True))
    assert chat.chat_workflows_enabled() is True
    monkeypatch.setattr(live, "snapshot", lambda: _cfg(on=False))
    assert chat.chat_workflows_enabled() is False
    monkeypatch.setattr(live, "snapshot", lambda: _cfg(backend="claude", on=True))
    assert chat.chat_workflows_enabled() is False


def test_the_switch_falls_back_to_the_saved_config(monkeypatch):
    from kiro_crew.config import KiroCrewConfig

    monkeypatch.setattr(live, "snapshot", lambda: None)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _cfg(on=True)))
    assert chat.chat_workflows_enabled() is True


def test_an_unreadable_config_leaves_the_switch_off(monkeypatch):
    def broken():
        raise OSError("config unreadable")

    monkeypatch.setattr(live, "snapshot", broken)
    assert chat.chat_workflows_enabled() is False


def test_only_update_workflow_is_a_plan_handoff():
    assert chat.is_plan_handoff(wire.TOOL_UPDATE_WORKFLOW)
    assert not chat.is_plan_handoff("run_workflow")
    assert not chat.is_plan_handoff("")


@pytest.mark.asyncio
async def test_a_chat_start_records_whether_its_session_has_workflows(monkeypatch):
    monkeypatch.setattr(chat, "_ENABLED_CHATS", {})
    monkeypatch.setattr(chat, "chat_workflows_enabled", lambda: True)
    assert await chat.wants_workflows("dashboard:chat-1") is True
    # Both spellings of one chat are the same chat.
    assert chat.has_kas_workflows("chat-1") and chat.has_kas_workflows("dashboard:chat-1")
    monkeypatch.setattr(chat, "chat_workflows_enabled", lambda: False)
    assert await chat.wants_workflows("chat-1") is False
    assert not chat.has_kas_workflows("chat-1")
    # A worker or channel session never asks.
    assert await chat.wants_workflows("worker:abc") is False
    assert not chat.has_kas_workflows("")


@pytest.mark.asyncio
async def test_the_record_of_chats_with_workflows_is_bounded(monkeypatch):
    monkeypatch.setattr(chat, "_ENABLED_CHATS", {})
    monkeypatch.setattr(chat, "_MAX_ENABLED_CHATS", 2)
    monkeypatch.setattr(chat, "chat_workflows_enabled", lambda: True)
    for n in range(3):
        assert await chat.wants_workflows(f"chat-{n}")
    assert list(chat._ENABLED_CHATS) == ["chat-1", "chat-2"]


# ── What a session is sent ──


def test_the_active_agent_gets_the_workflow_tools_and_no_other_does():
    agents = [
        {"id": "crew", "tools": ["fs_read", "run_workflow"]},
        {"id": "other", "tools": ["fs_read"]},
        {"id": "star", "tools": "*"},
    ]
    out = chat.with_workflow_tools(agents, "crew")
    assert out is not None
    assert out[0]["tools"] == ["fs_read", "run_workflow"] + [
        t for t in chat.WORKFLOW_CHAT_TOOLS if t != "run_workflow"
    ]
    assert out[1] == agents[1] and out[2] == agents[2]
    assert agents[0]["tools"] == ["fs_read", "run_workflow"], "the batch is copied"
    assert chat.with_workflow_tools(None, "crew") is None
    assert chat.with_workflow_tools([], "crew") == []
    star = chat.with_workflow_tools([{"id": "crew", "tools": "*"}], "crew")
    assert star == [{"id": "crew", "tools": "*"}]


def test_the_setting_merges_into_whatever_meta_is_there():
    params: dict = {"_meta": {"kiro": {"customAgents": [1], "settings": {"other": True}}}}
    chat.attach_workflow_settings(params)
    assert params["_meta"]["kiro"] == {
        "customAgents": [1],
        "settings": {"other": True, "workflows": {"enabled": True}},
    }
    bare: dict = {"_meta": "junk"}
    chat.attach_workflow_settings(bare, enabled=False)
    assert bare == {"_meta": {"kiro": {"settings": {"workflows": {"enabled": False}}}}}
    odd: dict = {"_meta": {"kiro": {"settings": {"workflows": "x"}}}}
    chat.attach_workflow_settings(odd)
    assert odd["_meta"]["kiro"]["settings"]["workflows"] == {"enabled": True}


# ── The rewind guard ──


@pytest.mark.parametrize(
    "value, expected",
    [
        (12.5, 12.5),
        (3, 3.0),
        ("1970-01-01T00:00:10Z", 10.0),
        ("1970-01-01T00:00:10", 10.0),
        ("42", 42.0),
        ("not a time", None),
        ("", None),
        (True, None),
        (None, None),
    ],
)
def test_times_are_read_as_epoch_seconds(value, expected):
    assert chat._epoch(value) == expected


class _Router:
    def __init__(self, rows):
        self._rows = rows

    def rows(self):
        if isinstance(self._rows, Exception):
            raise self._rows
        return self._rows


def _row(wid, status, created, slot="chat-1"):
    return {
        "workflowId": wid,
        "name": wid.upper(),
        "status": status,
        "createdAt": created,
        "slotKey": slot,
    }


def test_runs_since_splits_this_chats_later_runs_by_status(monkeypatch):
    rows = [
        _row("early", wire.STATUS_RUNNING, 5.0),
        _row("live", wire.STATUS_RUNNING, 20.0),
        _row("done", "completed", 30.0),
        _row("unknown-start", wire.STATUS_RUNNING, None),
        _row("elsewhere", wire.STATUS_RUNNING, 40.0, slot="chat-2"),
    ]
    monkeypatch.setattr(router_mod, "_ROUTER", _Router(rows))
    live_runs, ended = chat.runs_since("dashboard:chat-1", 10.0)
    assert [r["workflowId"] for r in live_runs] == ["live", "unknown-start"]
    assert ended == [{"workflowId": "done", "name": "DONE"}]
    # A message time it cannot read rules nothing out.
    live_runs, _ = chat.runs_since("chat-1", "garbled")
    assert [r["workflowId"] for r in live_runs] == ["early", "live", "unknown-start"]


def test_no_router_or_no_chat_means_no_runs(monkeypatch):
    monkeypatch.setattr(router_mod, "_ROUTER", None)
    assert chat.runs_since("chat-1", 0) == ([], [])
    monkeypatch.setattr(router_mod, "_ROUTER", _Router([_row("live", wire.STATUS_RUNNING, 1.0)]))
    assert chat.runs_since("", 0) == ([], [])


def test_the_rewind_is_refused_while_a_later_run_is_live(monkeypatch):
    monkeypatch.setattr(router_mod, "_ROUTER", _Router([_row("live", wire.STATUS_RUNNING, 20.0)]))
    refusal = chat.rewind_refusal("chat-1", 10.0)
    assert refusal == {
        "error": chat.REWIND_REFUSED_MESSAGE,
        "code": chat.REWIND_REFUSED_CODE,
        "runs": [{"workflowId": "live", "name": "LIVE"}],
    }
    monkeypatch.setattr(router_mod, "_ROUTER", _Router([_row("done", "completed", 20.0)]))
    assert chat.rewind_refusal("chat-1", 10.0) is None


def test_unreadable_rows_refuse_the_rewind(monkeypatch):
    monkeypatch.setattr(router_mod, "_ROUTER", _Router(RuntimeError("rows broke")))
    with pytest.raises(chat.RunsUnreadable):
        chat.runs_since("chat-1", 0)
    assert chat.rewind_refusal("chat-1", 0) == {
        "error": chat.REWIND_UNREADABLE_MESSAGE,
        "code": chat.REWIND_REFUSED_CODE,
        "runs": [],
    }
