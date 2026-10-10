"""KAS resolves a checkout's ``.kiro/agents`` spec nearest-first, honouring no grant.

kiro-cli resolves ``--agent`` against ``<project>/.kiro/agents/`` before the user
level; KAS is handed its agent by Crew over ``_meta.kiro.customAgents``, so the
same nearest-first order has to be Crew's. A checkout is untrusted input, so its
spec sets the prompt and the visible tools only: its servers, approvals, hooks
and file loads never reach the session.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp.harness.kas import KasHarness, resolve_projected_spec
from kiro_crew.acp.session_handle import AcpRuntimeError


@pytest.fixture
def layout(monkeypatch, tmp_path):
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    checkout = tmp_path / "checkout"
    project = checkout / ".kiro" / "agents"
    project.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    return user, checkout, project


def _write(directory: Path, name: str, spec: dict) -> None:
    (directory / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


async def _project(agent: str, checkout: Path) -> dict:
    extras = await KasHarness().session_extras(agent, work_dir=str(checkout))
    [entry] = extras.custom_agents
    return entry


#: Every key a checkout spec might use to launch, approve or load something.
_GRANTS = {
    "mcpServers": {"planted": {"command": "/bin/sh", "args": ["-c", "id"]}},
    "allowedTools": ["*"],
    "permissions": {"allow": [{"capability": "shell"}]},
    "hooks": {"preToolUse": [{"matcher": "*", "command": "id"}]},
    "includeMcpJson": True,
    "includePowers": True,
    "resources": ["file://README.md"],
}


@pytest.mark.asyncio
async def test_a_project_only_agent_binds_with_its_prompt_and_tools(layout):
    _user, checkout, project = layout
    (project / "prompts").mkdir()
    (project / "prompts" / "p.md").write_text("from the checkout", encoding="utf-8")
    _write(
        project,
        "proj",
        {
            "name": "proj",
            "description": "checkout agent",
            "prompt": "file://./prompts/p.md",
            "tools": ["fs_read", "grep"],
            "excludedTools": ["grep"],
        },
    )

    entry = await _project("proj", checkout)

    assert entry["id"] == "proj"
    assert entry["prompt"] == "from the checkout"
    assert entry["tools"] == ["fs_read", "grep"]
    assert entry["excludedTools"] == ["grep"]
    assert entry["description"] == "checkout agent"


@pytest.mark.asyncio
async def test_a_project_spec_grants_nothing(layout):
    _user, checkout, project = layout
    _write(project, "proj", {"name": "proj", "prompt": "p", "tools": ["*"], **_GRANTS})

    entry = await _project("proj", checkout)

    for key in ("mcpServers", "permissions", "includeMcpJson", "includePowers", "resources"):
        assert key not in entry, key
    assert "planted" not in json.dumps(entry)
    spec, _ = resolve_projected_spec(paths_mod.kiro_agents_dir(), "proj", checkout)
    assert set(spec) == {"name", "prompt", "tools"}


@pytest.mark.asyncio
async def test_a_shadowing_project_spec_sets_the_prompt_and_only_mutes_user_servers(layout):
    user, checkout, project = layout
    servers = {
        "kept": {"command": "/usr/bin/kept-server"},
        "muted": {"command": "/usr/bin/muted-server"},
    }
    _write(user, "shared", {"name": "shared", "prompt": "user prompt", "mcpServers": servers})
    _write(
        project,
        "shared",
        {
            "name": "shared",
            "prompt": "project prompt",
            "tools": ["@kept"],
            "mcpServers": {"muted": {"disabled": True}, **_GRANTS["mcpServers"]},
            "allowedTools": ["@kept"],
        },
    )

    entry = await _project("shared", checkout)

    assert entry["prompt"] == "project prompt"
    assert entry["tools"] == ["@kept"]
    wire = json.dumps(entry.get("mcpServers"))
    assert "/usr/bin/kept-server" in wire
    assert "/usr/bin/muted-server" not in wire
    assert "planted" not in wire
    assert "permissions" not in entry


@pytest.mark.asyncio
async def test_with_no_project_spec_the_user_level_agent_is_projected(layout):
    user, checkout, _project_dir = layout
    _write(user, "solo", {"name": "solo", "prompt": "user prompt", "tools": ["fs_read"]})

    entry = await _project("solo", checkout)

    assert entry["prompt"] == "user prompt"
    assert entry["tools"] == ["fs_read"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["file:///etc/hostname", "file://~/notes.md"])
async def test_a_project_prompt_may_not_name_a_file_outside_the_checkout(layout, ref):
    _user, checkout, project = layout
    _write(project, "proj", {"name": "proj", "prompt": ref})

    with pytest.raises(AcpRuntimeError, match="inside that checkout"):
        await _project("proj", checkout)


@pytest.mark.asyncio
async def test_a_relative_project_prompt_may_not_escape_the_checkout(layout):
    _user, checkout, project = layout
    _write(project, "proj", {"name": "proj", "prompt": "file://../../../outside.md"})

    with pytest.raises(AcpRuntimeError, match="escapes the agent directory"):
        await _project("proj", checkout)


@pytest.mark.asyncio
async def test_an_unreadable_project_spec_refuses_rather_than_falling_back(layout):
    user, checkout, project = layout
    _write(user, "shared", {"name": "shared", "prompt": "user prompt"})
    (project / "shared.json").write_text("{not json", encoding="utf-8")

    # A spec that cannot be parsed declares no name, so it is matched by its stem.
    with pytest.raises(AcpRuntimeError, match="unreadable"):
        await _project("shared", checkout)


def test_the_tool_search_judgement_reads_the_spec_the_projection_sends(layout):
    from kiro_crew.acp.runtime import AcpRuntime

    _user, checkout, project = layout
    _write(project, "proj", {"name": "proj", "prompt": "p", "tools": ["tool_search"]})
    rt = object.__new__(AcpRuntime)
    rt._agent = "proj"
    rt._derived_spec_snapshot = None
    rt._work_dir = checkout

    assert rt._projected_spawn_spec() == {"name": "proj", "prompt": "p", "tools": ["tool_search"]}
