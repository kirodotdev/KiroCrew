"""Which agent and which reading of its opt-in decide a session's project MCP holds.

kiro-cli reads a spec with no ``includeMcpJson`` as reading the folder's file, and
the card offer asks about the agent the session runs, not the slot's own.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import project_mcp_approvals as approvals
from kiro_crew.agent_sdk import project_mcp_holds as holds
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.project_mcp_files import KIRO_PROJECT_MCP_FILE

_PLANTED = {"command": "node", "args": ["planted.js"]}


@pytest.fixture
def folder(tmp_path, monkeypatch) -> Path:
    crew = tmp_path / "crew-home"
    crew.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    first = tmp_path / "first"
    first.mkdir()
    approvals.pending_servers(first)  # the one-time record, before the file exists
    repo = tmp_path / "repo"
    path = repo / KIRO_PROJECT_MCP_FILE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"planted": _PLANTED}}), encoding="utf-8")
    return repo


def _spec(monkeypatch, spec: dict[str, Any] | None) -> None:
    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.harness import kas

    monkeypatch.setattr(session_mcp, "agent_spec_snapshot", lambda agent, work_dir=None: spec)
    monkeypatch.setattr(kas, "resolve_projected_spec", lambda directory, *_a: (spec, directory))


def _held(backend: str, folder: Path) -> list[str]:
    return sorted(s.name for s in holds.held_project_servers(backend, "a", folder))


@pytest.mark.parametrize("spec", [{"name": "a"}, {"name": "a", "includeMcpJson": "yes"}, None])
def test_kiro_holds_the_file_for_a_spec_that_does_not_state_the_opt_in(folder, monkeypatch, spec):
    _spec(monkeypatch, spec)
    assert _held(ACP_BACKEND_KIRO, folder) == ["planted"]


def test_kiro_holds_nothing_for_a_spec_that_opts_out(folder, monkeypatch):
    _spec(monkeypatch, {"name": "a", "includeMcpJson": False})
    assert _held(ACP_BACKEND_KIRO, folder) == []


def test_kas_holds_nothing_for_a_spec_that_does_not_state_the_opt_in(folder, monkeypatch):
    from kiro_crew.acp import kas_mcp_json_legacy

    monkeypatch.setattr(kas_mcp_json_legacy, "legacy_mcp_json", lambda *_a: False)
    _spec(monkeypatch, {"name": "a"})
    assert _held(ACP_BACKEND_KAS, folder) == []


@pytest.mark.asyncio
async def test_the_card_offer_asks_about_the_agent_the_session_runs(tmp_path, monkeypatch):
    from kiro_crew.agent_sdk import capabilities
    from kiro_crew.dashboard.project_mcp_cards import offer_project_mcp_cards

    asked: list[str | None] = []

    def _held_for(backend, agent, work_dir):
        asked.append(agent)
        return approvals.FolderCheck()

    monkeypatch.setattr(holds, "project_hold", _held_for)
    monkeypatch.setattr(
        capabilities, "capabilities_of", lambda _client: SimpleNamespace(backend=ACP_BACKEND_KIRO)
    )
    slot = SimpleNamespace(key="chat-1", agent="slot-agent", project=str(tmp_path))
    client = SimpleNamespace(cwd=str(tmp_path))
    await offer_project_mcp_cards(object(), slot, client, "sk", "crewmate")
    assert asked == ["crewmate"]
