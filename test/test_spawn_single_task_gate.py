"""Spawn tools state the same concrete-value policy as the orchestration prompts.

The runtime verifies reason shape and supported delivery, not whether work is
independent or valuable. Existing payloads retain their gate compatibility.
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
def test_description_prioritizes_direct_work_and_concrete_value(tool: str) -> None:
    desc = _tools()[tool]["description"]
    assert desc.startswith("Do focused work directly")
    assert "concrete" in desc and "user request" in desc
    assert "ENFORCED" in desc and "refused" in desc
    assert "ignore that advice" not in desc


def test_spawn_run_still_documents_its_mechanics() -> None:
    desc = _tools()["spawn_run"]["description"]
    assert "Returns immediately" in desc
    assert "[Subagent completion event]" in desc
    assert "capacity is a ceiling, not a target" in desc
    assert "Keep dependent tasks for a later batch" in desc


def test_single_task_contract_bans_only_unjustified_transfer() -> None:
    task = _tools()["spawn_run"]["inputSchema"]["properties"]["task"]["description"]
    assert "bounded assignment" in task
    assert "ready inputs, ownership, verifiable outputs and stop conditions" in task
    assert "equivalent worker merely to wait and relay" in task
