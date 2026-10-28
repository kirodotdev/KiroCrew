"""``agent.kas_creator_effort``: the effort KAS gives a chat's workflow-creator step.

KAS reads ``settings.workflows.creatorEffort`` from a session's ``session/new``
and every ``session/load`` and keeps the last value one carried
(``packages/acp-type-covenant/settings/index.ts``, ``WORKFLOW_CREATOR_EFFORT_LEVELS``).
So the setting rides on both, only for a dashboard chat with chat workflows on,
and never on a sub-agent, cron or task-runner session.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.harness import SessionExtras
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_SESSION_LOAD, METHOD_SESSION_NEW
from kiro_crew.kas_workflows import chat
from kiro_crew.testing.ids import UNALLOCATABLE_PID


@pytest.fixture(autouse=True)
def _clean_enabled_chats(_floor_monkeypatch):
    _floor_monkeypatch.setattr(chat, "_ENABLED_CHATS", {})


@pytest.fixture(autouse=True)
def _fast_runtime_start(_floor_monkeypatch):
    import kiro_crew.acp.session_handle as sh
    from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

    _floor_monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 0.05, raising=False)
    _floor_monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: SPEC_PERMISSIONS_MIN_VERSION
    )


def _kas_runtime(monkeypatch):
    rt = AcpRuntime(work_dir="/tmp")
    proc = MagicMock()
    proc.stdout = asyncio.StreamReader()
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = UNALLOCATABLE_PID
    rt._initialized = True
    rt._can_load_session = True
    rt._acp_backend = ACP_BACKEND_KAS
    rt._workflow_sink = MagicMock()
    sent: list[tuple[str, dict]] = []

    async def _fake_send(method, params, timeout=None):
        sent.append((method, json.loads(json.dumps(params))))
        if method == METHOD_SESSION_LOAD:
            return {"modes": {"currentModeId": "kirocrew"}, "models": []}
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-new", "modes": {"currentModeId": "kirocrew"}, "models": []}
        return {}

    async def _fake_agents(agent, *, member_dispatch=False, crew_panel=False, session_key=""):
        return SessionExtras(custom_agents=[{"id": agent, "prompt": "p", "tools": []}])

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)
    return rt, sent


def _workflows_meta(params: dict):
    return params.get("_meta", {}).get("kiro", {}).get("settings", {}).get("workflows")


def _start_params(sent: list[tuple[str, dict]], method: str) -> dict:
    return next(p for m, p in sent if m == method)


async def _start(rt, path: str, session_key: str) -> None:
    if path == "load":
        await rt.load_session("", "sid-1", cwd="/work", agent="kirocrew", session_key=session_key)
    else:
        await rt.create_session(cwd="/work", agent="kirocrew", session_key=session_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
async def test_a_set_effort_rides_on_new_and_load(monkeypatch, path):
    monkeypatch.setattr(chat, "chat_workflows_enabled", lambda: True)
    monkeypatch.setattr(chat, "creator_effort_setting", lambda: "xhigh")
    rt, sent = _kas_runtime(monkeypatch)
    await _start(rt, path, "chat-abc")
    method = METHOD_SESSION_LOAD if path == "load" else METHOD_SESSION_NEW
    assert _workflows_meta(_start_params(sent, method)) == {
        "enabled": True,
        "creatorEffort": "xhigh",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
async def test_an_empty_effort_is_left_out(monkeypatch, path):
    monkeypatch.setattr(chat, "chat_workflows_enabled", lambda: True)
    monkeypatch.setattr(chat, "creator_effort_setting", lambda: "")
    rt, sent = _kas_runtime(monkeypatch)
    await _start(rt, path, "chat-abc")
    method = METHOD_SESSION_LOAD if path == "load" else METHOD_SESSION_NEW
    assert _workflows_meta(_start_params(sent, method)) == {"enabled": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "load"])
@pytest.mark.parametrize("session_key", ["subagent:abc", "cron:daily", "taskrunner_1"])
async def test_never_sent_on_a_non_chat_session(monkeypatch, path, session_key):
    monkeypatch.setattr(chat, "chat_workflows_enabled", lambda: True)
    monkeypatch.setattr(chat, "creator_effort_setting", lambda: "max")
    rt, sent = _kas_runtime(monkeypatch)
    await _start(rt, path, session_key)
    method = METHOD_SESSION_LOAD if path == "load" else METHOD_SESSION_NEW
    assert _workflows_meta(_start_params(sent, method)) is None


@pytest.mark.asyncio
async def test_not_sent_while_chat_workflows_are_off(monkeypatch):
    monkeypatch.setattr(chat, "chat_workflows_enabled", lambda: False)
    monkeypatch.setattr(chat, "creator_effort_setting", lambda: "max")
    rt, sent = _kas_runtime(monkeypatch)
    await _start(rt, "load", "chat-abc")
    # Gap 5b: a chat whose switch is off sends enabled:false explicitly, but
    # creatorEffort never rides along with it.
    assert _workflows_meta(_start_params(sent, METHOD_SESSION_LOAD)) == {"enabled": False}


def test_attach_adds_creator_effort_only_when_set():
    params: dict = {}
    chat.attach_workflow_settings(params, creator_effort="low")
    assert _workflows_meta(params) == {"enabled": True, "creatorEffort": "low"}
    params = {}
    chat.attach_workflow_settings(params)
    assert _workflows_meta(params) == {"enabled": True}


# ── The setting ──


def test_the_setting_is_empty_by_default_and_patchable_as_kas_levels():
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.config.sections import KAS_CREATOR_EFFORT_LEVELS
    from kiro_crew.dashboard.handlers.core import _editable_config_spec

    assert KiroCrewConfig().agent.kas_creator_effort == ""
    # kiro-agent acp-type-covenant settings WORKFLOW_CREATOR_EFFORT_LEVELS.
    assert KAS_CREATOR_EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")
    assert _editable_config_spec("agent.kas_creator_effort") == {
        "type": "enum",
        "values": ["", *KAS_CREATOR_EFFORT_LEVELS],
    }


@pytest.mark.parametrize(("raw", "expected"), [("high", "high"), ("huge", ""), (3, ""), (None, "")])
def test_the_loader_keeps_only_a_kas_level(raw, expected):
    from kiro_crew.config.loader import _build_agent_config

    agent: dict = {} if raw is None else {"kas_creator_effort": raw}
    assert _build_agent_config(agent).kas_creator_effort == expected


def test_creator_effort_setting_reads_the_live_config(monkeypatch):
    from kiro_crew.config import KiroCrewConfig, live

    cfg = KiroCrewConfig()
    cfg.agent.kas_creator_effort = "medium"
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    assert chat.creator_effort_setting() == "medium"
    cfg.agent.kas_creator_effort = "bogus"
    assert chat.creator_effort_setting() == ""
