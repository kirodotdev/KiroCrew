"""R6: pure execution boundaries must not grant new tools or revive deleted Agents."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_byo.agent_projection import AgentProjection
from kiro_byo.session_restore_projection import resolve_restored_agent_projection
from kiro_byo.subagent_tool_policy import filter_isolated_subagent_tools, subagent_system_instruction
from kiro_byo.turn_review_messages import build_review_messages
from kiro_byo.mcp_client import DEFERRED_TOOL_SEARCH_NAME


def tool(name: str):
    return {"type": "function", "function": {"name": name}}


class Native:
    def __init__(self, names=()):
        self.names = frozenset(names)
        self.seen = []

    def has_tool(self, name):
        self.seen.append(name)
        return name in self.names


@pytest.mark.parametrize("writer_mode", [False, True])
def test_child_tool_filter_blocks_search_pseudo_tool_and_preserves_order(writer_mode):
    native = Native({"read", "write", "shell"})
    seen = []
    def is_read_only(name):
        seen.append(name)
        return name == "read"
    entries = [tool("read"), tool(DEFERRED_TOOL_SEARCH_NAME), tool("write"), tool("shell"), tool("read")]
    filtered = filter_isolated_subagent_tools(entries, writer_mode=writer_mode,
                                                native=native, is_gathering_tool=is_read_only)
    assert filtered == ([tool("read"), tool("write"), tool("shell"), tool("read")]
                        if writer_mode else [tool("read"), tool("read")])
    assert DEFERRED_TOOL_SEARCH_NAME not in native.seen + seen
    if writer_mode:
        assert seen == []  # writer never inherits the parent's read-only tool classifier
    else:
        assert native.seen == []  # reader never adopts native writer visibility


def test_child_tool_filter_writer_without_native_exposes_nothing():
    assert filter_isolated_subagent_tools([tool("read"), tool("shell")], writer_mode=True,
                                          native=None, is_gathering_tool=lambda _: True) == []


def test_child_role_instructions_keep_isolation_contract():
    writer = subagent_system_instruction("fix tests", writer_mode=True)
    reader = subagent_system_instruction("audit", writer_mode=False)
    assert "private git worktree" in writer and "never modify the parent workspace" in writer
    assert "read-only" in reader and "Never propose or perform writes" in reader
    assert "fix tests" in writer and "audit" in reader


@pytest.mark.parametrize("role", ["verify", "review"])
def test_detached_review_includes_original_instruction_sequence(role):
    messages = build_review_messages(agent_prompt="agent", runtime_mode_context="mode",
                                     language_instruction="reply in Japanese", role=role,
                                     transcript="tool read result", draft="draft")
    assert [message["role"] for message in messages] == ["system"] * 5 + ["user"]
    assert [m["content"] for m in messages[:3]] == ["agent", "mode", "reply in Japanese"]
    assert messages[-2]["content"] == "reply in Japanese"
    assert messages[-1]["content"] == "TURN TRANSCRIPT:\ntool read result\n\nDRAFT ANSWER:\ndraft"
    assert "Never add actions" in messages[-3]["content"]
    assert ("For verification" in messages[-3]["content"]) == (role == "verify")
    assert "Do not claim tools were used" in messages[-3]["content"]


def test_detached_review_no_optional_prompts():
    messages = build_review_messages(agent_prompt="", runtime_mode_context="",
                                     language_instruction="", role="verify", transcript="", draft="x")
    assert len(messages) == 2 and messages[-1]["role"] == "user"


def test_restore_current_crew_metadata_never_resurrects_saved_agent():
    incoming = AgentProjection()
    record = {"agent": {"id": "old", "prompt": "unsafe old prompt", "tools": ["shell"]},
              "agent_id": "legacy"}
    assert resolve_restored_agent_projection(incoming, record, crew_reprojected_agents=True) is incoming
    current = AgentProjection(agent_id="new", prompt="from crew")
    assert resolve_restored_agent_projection(current, record, crew_reprojected_agents=False) is current


def test_restore_legacy_fallback_does_not_recover_auto_approval():
    record = {"agent": {"id": "old", "prompt": "old", "tools": ["read", "shell"],
                        "excludedTools": ["shell"], "allowedTools": ["shell"]}}
    restored = resolve_restored_agent_projection(AgentProjection(), record, crew_reprojected_agents=False)
    assert restored.agent_id == "old"
    assert restored.tools == ("read", "shell") and restored.excluded_tools == ("shell",)
    assert restored.allowed_tools == ()
    assert not restored.auto_approves_native("shell")


def test_restore_legacy_id_only_and_missing_record():
    fallback = resolve_restored_agent_projection(AgentProjection(),
               {"agent_id": "legacy", "agent_prompt": "context"}, crew_reprojected_agents=False)
    assert (fallback.agent_id, fallback.prompt) == ("legacy", "context")
    incoming = AgentProjection()
    assert resolve_restored_agent_projection(incoming, {}, crew_reprojected_agents=False) is incoming


def test_provider_main_loop_step_continuations_are_not_refactored():
    # The R6 refactor must not touch the retry/advance control-flow path.
    source = (Path(__file__).resolve().parents[1] / "src/kiro_byo/turn_runner.py").read_text()
    tree = ast.parse(source)
    node = next(item for item in ast.walk(tree)
                if isinstance(item, ast.FunctionDef) and item.name == "_run_provider_tool_loop")
    directive_names = {item.attr for item in ast.walk(node) if isinstance(item, ast.Attribute)
                       and isinstance(item.value, ast.Name) and item.value.id == "_LoopDirective"}
    assert {"RETRY_SAME_MODEL", "PRISTINE_FALLBACK", "STEER_AFTER_TOOLS", "RETURN"} <= directive_names
    assert sum(isinstance(item, ast.Continue) for item in ast.walk(node)) == 10
