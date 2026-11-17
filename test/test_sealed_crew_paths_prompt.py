# SPDX-License-Identifier: Apache-2.0
"""The agent is told what a sandbox refusal on ``config.json`` or ``work-ledger`` means.

The OS sandbox seals ``config.json`` and ``config.local.json`` read-only and hides
``work-ledger``, so a script that touches them gets a bare errno that no Kiro Crew
code sees. The prompts of the agents that run such scripts, and of the root
conductor that reads their reports, are the only place that explains the refusal
and names the route that works. Each prompt names only routes its own agent can
reach, so a tool it names must be mounted on the spec that reads it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_agent_refactor_spec_bytes import materialize

import kiro_crew
from kiro_crew import mcp_guide, mcp_work, sandbox_leaves
from kiro_crew.agent_parts import spec_prompts

#: What every copy of the rule names: the sealed paths and the errors they raise.
_SHARED = (
    "config.json",
    "config.local.json",
    "work-ledger",
    "Operation not permitted",
    "Read-only file system",
    "Device or resource busy",
    "Settings page",
)

#: The server each tool a copy may name is mounted from.
_TOOL_SERVER = {
    **{name: "kirocrew-work" for name in (*mcp_work.WORKER_TOOLS, *mcp_work.CONDUCTOR_TOOLS)},
    **{name: "kirocrew-guide" for name in mcp_guide._TOOL_NAMES},
}


def _default_rule() -> str:
    """The sealed-path bullet of the shipped default contract."""
    shipped = Path(kiro_crew.__file__).parent / "config" / "prompt.md"
    lines = shipped.read_text(encoding="utf-8").splitlines()
    (bullet,) = [line for line in lines if "Inside the sandbox" in line]
    return bullet


#: Each copy, the spec files whose agents read it, the routes that copy names, and
#: whether every tool it names must also be pre-approved. A dispatched conductor or
#: worker runs in a child session nobody watches, so a gated call there is an
#: approval stall; the default agent's person answers its own prompts.
_COPIES = {
    "default-contract": (
        _default_rule,
        ("kirocrew.json",),
        ("setting.change", "propose_change", "Dashboard tab"),
        False,
    ),
    "root-conductor": (
        lambda: spec_prompts._SEALED_CREW_PATHS_RULE,
        ("kirocrew-conductor.json", "kirocrew-ledger-conductor.json"),
        ("work_ledger_read", "work_brief", "work_report"),
        True,
    ),
    "worker": (
        lambda: spec_prompts._WORKER_SEALED_CREW_PATHS_RULE,
        ("kirocrew-worker.json",),
        ("work_brief", "work_report"),
        True,
    ),
}


def _named_tools(text: str) -> set[str]:
    """Every backticked token in *text* that is a known MCP tool name."""
    return {token for token in text.split("`")[1::2] if token in _TOOL_SERVER}


def _mounted_servers(spec: dict) -> set[str]:
    servers = set(spec.get("mcpServers") or {})
    tools = spec.get("tools") or []
    return servers | {
        t[1:].split("/", 1)[0] for t in tools if isinstance(t, str) and t.startswith("@")
    }


def _granted(spec: dict, server: str, tool: str) -> bool:
    """Whether *tool* on *server* runs without an approval prompt for *spec*."""
    allowed = set(spec.get("allowedTools") or [])
    if allowed & {f"@{server}", f"@{server}/*", f"@{server}/{tool}"}:
        return True
    auto = ((spec.get("mcpServers") or {}).get(server) or {}).get("autoApprove") or []
    return tool in auto or "*" in auto


@pytest.mark.parametrize("copy", sorted(_COPIES))
def test_each_copy_explains_the_refusal_and_its_own_routes(copy: str) -> None:
    rule, _specs, routes, _needs_grant = _COPIES[copy]
    text = " ".join(rule().split())
    missing = [token for token in (*_SHARED, *routes) if token not in text]
    assert missing == []


def test_the_default_contract_offers_no_ledger_tool() -> None:
    """The default agent mounts no ``kirocrew-work``: it points the person at the
    conductor's Dashboard tab instead of a tool it cannot call."""
    named = _named_tools(_default_rule())
    assert named & set(mcp_work.CONDUCTOR_TOOLS + mcp_work.WORKER_TOOLS) == set()


def test_each_dispatched_prompt_carries_its_own_copy() -> None:
    assert spec_prompts._SEALED_CREW_PATHS_RULE in spec_prompts._CONDUCTOR_SYSTEM_PROMPT
    assert spec_prompts._WORKER_SEALED_CREW_PATHS_RULE in spec_prompts._WORKER_SYSTEM_PROMPT
    assert spec_prompts._SEALED_CREW_PATHS_RULE not in spec_prompts._WORKER_SYSTEM_PROMPT


@pytest.mark.skipif(sys.platform == "win32", reason="the spec rig pins POSIX paths")
@pytest.mark.parametrize("copy", sorted(_COPIES))
def test_every_tool_a_copy_names_is_mounted_on_its_agents(
    copy: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule, specs, _routes, needs_grant = _COPIES[copy]
    named = _named_tools(rule())
    assert named, "the copy names no tool, so this check proves nothing"
    files = materialize(tmp_path, monkeypatch, "fresh").files
    for spec_name in specs:
        spec = json.loads(files[spec_name])
        assert " ".join(rule().split()) in " ".join(spec.get("prompt", "").split()) or (
            spec_name == "kirocrew.json"
        ), f"{spec_name} does not carry the {copy} copy"
        mounted = _mounted_servers(spec)
        unreachable = sorted(t for t in named if _TOOL_SERVER[t] not in mounted)
        assert unreachable == [], f"{spec_name} cannot call {unreachable}"
        if needs_grant:
            gated = sorted(t for t in named if not _granted(spec, _TOOL_SERVER[t], t))
            assert gated == [], f"{spec_name} would wait on approval for {gated}"


def test_the_named_paths_are_the_ones_the_sandbox_seals() -> None:
    """The prompt names real fences: if a leaf moves, the sentence is wrong."""
    assert "config.json" in sandbox_leaves._CREW_READONLY_LEAVES
    assert "config.local.json" in sandbox_leaves._CREW_READONLY_LEAVES
    assert "work-ledger" in sandbox_leaves._CREW_HIDDEN_LEAVES
