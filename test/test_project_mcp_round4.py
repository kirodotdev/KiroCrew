"""Project MCP holds follow the host's spec and tolerate partial approval records."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def layout(tmp_path, monkeypatch):
    from kiro_crew import agent, template_mcp_additions
    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.harness import kas
    from kiro_crew.config import paths

    user = tmp_path / "agents"
    user.mkdir()
    folder = tmp_path / "repo"
    folder.mkdir()
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: user)
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _agent: None)
    monkeypatch.setattr(session_mcp, "require_fresh_derived_spec", lambda *_a: None)
    monkeypatch.setattr(template_mcp_additions, "merge_at_spawn", lambda _agent, spec: spec)
    monkeypatch.setattr(kas, "with_default_crew_servers", lambda _agent, spec, **_kw: spec)
    return user, folder


@pytest.mark.parametrize("user_opt_in", [True, False])
def test_each_host_reads_its_own_spec_opt_in(layout, user_opt_in):
    from kiro_crew.agent_sdk.project_mcp_holds import project_hold
    from kiro_crew.project_mcp_files import KIRO_PROJECT_MCP_FILE

    user, folder = layout
    _write(user / "a.json", {"name": "a", "includeMcpJson": user_opt_in})
    _write(
        folder / ".kiro/agents/a.json",
        {"name": "a", "includeMcpJson": not user_opt_in},
    )
    _write(folder / KIRO_PROJECT_MCP_FILE, {"mcpServers": {"weather": {"command": "weather"}}})

    kiro = project_hold(ACP_BACKEND_KIRO, "a", folder)
    assert kiro.unreadable == ()
    assert [s.name for s in kiro.pending] == ([] if user_opt_in else ["weather"])
    kas = project_hold(ACP_BACKEND_KAS, "a", folder)
    assert kas.unreadable == ()
    assert [s.name for s in kas.pending] == (["weather"] if user_opt_in else [])


def test_each_host_uses_its_own_spec_server_names(layout):
    from kiro_crew.agent_sdk.project_mcp_holds import project_hold
    from kiro_crew.project_mcp_files import KIRO_PROJECT_MCP_FILE

    user, folder = layout
    common = {"name": "a", "includeMcpJson": True}
    entry = {"command": "weather"}
    _write(user / "a.json", {**common, "mcpServers": {"user-server": entry}})
    _write(folder / ".kiro/agents/a.json", {**common, "mcpServers": {"folder-server": entry}})
    _write(
        folder / KIRO_PROJECT_MCP_FILE,
        {"mcpServers": {"user-server": entry, "folder-server": entry}},
    )

    kiro = project_hold(ACP_BACKEND_KIRO, "a", folder)
    assert kiro.unreadable == ()
    assert [s.name for s in kiro.pending] == ["user-server"]
    kas = project_hold(ACP_BACKEND_KAS, "a", folder)
    assert kas.unreadable == ()
    assert [s.name for s in kas.pending] == ["folder-server"]


@pytest.mark.asyncio
@pytest.mark.parametrize("folder_record", [None, {".mcp.json": ["invalid"]}])
async def test_partial_record_keeps_servers_pending_and_allows_approval(
    tmp_path, monkeypatch, folder_record
):
    from kiro_crew import project_mcp_approvals as approvals
    from kiro_crew.agent_sdk import capabilities
    from kiro_crew.dashboard.handlers import change_cards_propose
    from kiro_crew.dashboard.project_mcp_cards import offer_project_mcp_cards
    from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE

    folder = tmp_path / "repo"
    entry = {"command": "weather"}
    _write(folder / CLAUDE_PROJECT_MCP_FILE, {"mcpServers": {"weather": entry}})
    key = str(folder.resolve())
    _write(approvals.store_path(), {"version": 1, "folders": {key: folder_record}})
    check = approvals.check_folder(folder)
    assert check.unreadable == ()
    [held] = check.pending
    assert (held.name, held.status) == ("weather", approvals.STATUS_NEW)

    raised = []

    async def propose(_state, _slot_key, _session_key, **kwargs):
        raised.append(kwargs)
        return {}

    monkeypatch.setattr(change_cards_propose, "propose_in_slot", propose)
    monkeypatch.setattr(
        capabilities, "capabilities_of", lambda _: SimpleNamespace(backend="claude")
    )
    slot = SimpleNamespace(key="chat-1", project=str(folder))
    await offer_project_mcp_cards(object(), slot, SimpleNamespace(cwd=str(folder)), "sk", "a")
    [card] = raised
    assert card["kind"] == "mcp.project_server"
    assert card["params"] == {
        "folder": key,
        "file": CLAUDE_PROJECT_MCP_FILE,
        "server": "weather",
        "fingerprint": held.fingerprint,
    }
    approvals.approve(key, CLAUDE_PROJECT_MCP_FILE, "weather", held.fingerprint)
    assert approvals.pending_servers(folder) == []
