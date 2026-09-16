"""The spawn tools must say, in their own descriptions, when NOT to use them.

The base kiro-cli prompt tells the model to "delegate to a sub-agent" for
investigation and to preserve context. In a Kiro Crew session the only tools
named sub-agent are ``spawn_run`` / ``spawn_sub_agents``, so that advice landed
straight on them: a single task, a code read, a small fix -- each spawned one
sub-agent for no parallelism gain, on every crew host.

``prompt.md`` already says "delegate for hard problems, not just multi-step",
but the model reads the TOOL description at decision time. So the gate lives
there: only 2+ independent parallel tasks or bulk-data isolation qualify, and
the description must explicitly disown the base prompt's "sub-agent".
"""

from __future__ import annotations

import types
from unittest.mock import patch

import pytest

from kiro_crew.mcp_tools import spawn as spawn_tools


def _tools() -> dict[str, dict]:
    roster = [types.SimpleNamespace(name="kirocrew")]
    with patch.object(spawn_tools.mcp_core, "list_agents", return_value=roster):
        return {t["name"]: t for t in spawn_tools.schemas()}


@pytest.mark.parametrize("tool", ["spawn_run", "spawn_sub_agents"])
def test_description_opens_with_the_gate(tool: str) -> None:
    desc = _tools()[tool]["description"]
    # The gate is the FIRST thing read, not a caveat after the sales pitch.
    assert desc.startswith("GATE:")
    assert "2+ independent" in desc
    # The one single-task case that IS legitimate is named in the gate itself,
    # so the gate and the `task` parameter never contradict each other.
    assert "different agent/model" in desc
    # A single task is named as a non-reason, in words the base prompt uses.
    assert "single task" in desc
    assert "do" in desc and "yourself" in desc


@pytest.mark.parametrize("tool", ["spawn_run", "spawn_sub_agents"])
def test_description_disowns_the_base_prompt_sub_agent(tool: str) -> None:
    """The base prompt's 'delegate to a sub-agent' must not resolve to this tool."""
    desc = _tools()[tool]["description"]
    assert "NOT the 'sub-agent' your base instructions" in desc


def test_spawn_run_still_documents_its_mechanics() -> None:
    """The gate is prepended; the operating contract behind it is unchanged."""
    desc = _tools()["spawn_run"]["description"]
    assert "Returns immediately" in desc
    assert "[Subagent completion event]" in desc


def test_single_task_parameter_is_discouraged() -> None:
    task = _tools()["spawn_run"]["inputSchema"]["properties"]["task"]["description"]
    assert "Discouraged" in task
    assert "no parallelism gain" in task
    # ...but its two legitimate uses stay named, so it is not read as forbidden.
    assert "agent/model" in task
    assert "bulk" in task
