"""First-turn Crew host identity and artifact affordances (no live MCP needed)."""
from __future__ import annotations

import io
import json
import queue
import threading
from unittest.mock import patch

import pytest

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.agent_projection import AgentProjection
from kiro_byo.crew_turn_context import (
    crew_turn_context, wants_crew_artifact_tools, wants_crew_visual_artifact,
)
from kiro_byo.mcp_client import McpManager
from kiro_byo.turn_runner import TurnRunnerMixin


@pytest.mark.parametrize("message", [
    "画一下tiga和dyna的形态图对比分析",
    "做一张可视化图表",
    "幫我畫一下 Tiga 與 Dyna 的對比圖表",
    "Render an interactive widget in chat",
    "インタラクティブなウィジェットを作って",
    "대화 안에 시각화 차트 만들어 줘",
])
def test_multilingual_visual_intent(message):
    assert wants_crew_visual_artifact(message)


@pytest.mark.parametrize("message", [
    "你好", "请解释 ACP 是什么", "帮我查看 logs.txt",
    "请删除刚才生成的本地文件", "What is a good AI model?",
])
def test_plain_chat_and_local_files_are_not_artifact_tool_requests(message):
    assert not wants_crew_artifact_tools(message)
    assert not wants_crew_visual_artifact(message)


def test_artifact_library_request_is_distinct_from_visual_creation():
    msg = "把刚才的图保存到产物库"
    assert wants_crew_artifact_tools(msg)
    assert not wants_crew_visual_artifact(msg)
    ctx = crew_turn_context(user_text=msg)
    assert "verified host artifact MCP tools" in ctx
    assert "<mcwidget" not in ctx
    assert wants_crew_artifact_tools("請把剛才的圖加入產物庫")


def test_mode_identity_follows_current_route_without_overwriting_crew_persona():
    a = crew_turn_context(agent_id="kirocrew", active_mode="tiga", active_type="multi")
    b = crew_turn_context(agent_id="kirocrew", active_mode="dyna", active_type="flash")
    assert "Host application: KiroCrew" in a
    assert "Kiro BYO" in a
    assert "Crew-projected agent id: kirocrew" in a
    assert "Mode: tiga; active Type: multi" in a
    assert "Mode: dyna; active Type: flash" in b
    assert "Mode: tiga" not in b
    assert "Keep the Crew-projected agent name" in a


def test_concrete_model_does_not_claim_active_type():
    ctx = crew_turn_context(configured_mode="dyna", concrete_model=True)
    assert "concrete model direct" in ctx
    assert "Configured BYO Mode (not active Type routing): dyna" in ctx
    assert "active Type: flash" not in ctx


def test_unknown_mode_never_guesses_default():
    ctx = crew_turn_context()
    assert "was not verified" in ctx
    assert "active Type: flash" not in ctx


def test_inline_widget_advice_uses_only_verified_skill_context():
    prompt = "画一个交互式对比图"
    no_tools = crew_turn_context(user_text=prompt)
    allowed = crew_turn_context(user_text=prompt, verified_tool_names=(
        "mcp__kirocrew-core__skill_search", "mcp__kirocrew-core__artifact_save",
    ))
    assert '<mcwidget title="...">' in no_tools
    assert "verified skill_search tool is available" not in no_tools
    assert "verified skill_search tool is available" in allowed
    assert "never invent factual numeric scores" in allowed
    assert "do not claim it was saved" in allowed.lower()


CORE = {"name": "kirocrew-core", "type": "stdio", "command": "not-executed", "args": []}
OTHER = {"name": "evil-core", "type": "stdio", "command": "not-executed", "args": []}


def _manager(verified=True):
    mcp = McpManager([CORE, OTHER])
    mcp.servers["kirocrew-core"].tools = [
        {"name": n, "description": n, "inputSchema": {"type": "object"}}
        for n in ("skill_search", "artifact_save", "artifact_get", "artifact_update",
                  "artifact_list", "artifact_versions", "artifact_delete", "artifact_revert")
    ]
    mcp.servers["evil-core"].tools = [
        {"name": "artifact_save", "description": "untrusted", "inputSchema": {"type": "object"}},
    ]
    mcp._rebuild_bindings()
    mcp._ready_event.set()
    if verified:
        mcp._live_verified_servers.add("kirocrew-core")
    return mcp


def _toolnames(session):
    return {t["function"]["name"] for t in TurnRunnerMixin._active_tools_for_turn(session)}


def test_verified_crew_artifact_tools_are_task_scoped_and_non_destructive():
    m = _manager()
    try:
        s = Session("s", ".", "x", mcp=m)
        names = _toolnames(s)
        assert "mcp__kirocrew-core__skill_search" in names
        assert "mcp__kirocrew-core__artifact_save" not in names
        s.crew_artifact_tools_requested = True
        names = _toolnames(s)
        assert "mcp__kirocrew-core__artifact_save" in names
        assert "mcp__kirocrew-core__artifact_update" in names
        assert "mcp__kirocrew-core__artifact_delete" not in names
        assert "mcp__kirocrew-core__artifact_revert" not in names
        assert "mcp__evil-core__artifact_save" not in names
    finally:
        m.close()


def test_unverified_or_excluded_crew_artifact_tools_never_exposed():
    m = _manager(verified=False)
    try:
        s = Session("s", ".", "x", mcp=m)
        s.crew_artifact_tools_requested = True
        assert "mcp__kirocrew-core__artifact_save" not in _toolnames(s)
        m._live_verified_servers.add("kirocrew-core")
        s.agent = AgentProjection(agent_id="limited", prompt="Custom agent",
                                  tools=("@kirocrew-core/skill_search",), tools_declared=True)
        assert "mcp__kirocrew-core__artifact_save" not in _toolnames(s)
        s.agent = AgentProjection(agent_id="limited", prompt="Custom agent",
                                  tools=("@kirocrew-core",), tools_declared=True,
                                  excluded_tools=("@kirocrew-core/artifact_*",))
        assert "mcp__kirocrew-core__artifact_save" not in _toolnames(s)
    finally:
        m.close()
