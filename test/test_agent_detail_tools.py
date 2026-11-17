"""The custom agent detail reports the same projected tools as the chat notice."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard import agent_no_tools_notice as notice
from kiro_crew.dashboard.handlers import agents
from kiro_crew.template_mcp_additions import add_addition


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend",
    [ACP_BACKEND_KAS, ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE],
    ids=["kas", "kiro", "claude"],
)
@pytest.mark.parametrize(
    "tools, addition, kas_expected, other_expected",
    [
        ([], False, False, False),
        ([], True, True, False),
        ([""], False, True, False),
        (["fs_read"], True, True, True),
    ],
    ids=["no-tools", "approved-addition", "default-crew-servers", "authored-tool"],
)
async def test_detail_reports_projected_tools_without_changing_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    tools,
    addition: bool,
    kas_expected: bool,
    other_expected: bool,
) -> None:
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = backend
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    expected = kas_expected if backend == ACP_BACKEND_KAS else other_expected
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agent_dir)
    monkeypatch.setattr("kiro_crew.agent_state.get_capabilities", lambda agent: None)
    monkeypatch.setattr("kiro_crew.apps.manager.owning_app_for_agent", lambda agent: None)
    monkeypatch.setattr("kiro_crew.acp.default_crew_servers._registry_mode", lambda: False)
    monkeypatch.setattr(
        "kiro_crew.agent.managed_mcp_spec_entry",
        lambda server: {"command": "test-mcp", "args": [server]},
    )
    monkeypatch.setattr(agents, "agent_skill_views", lambda *args: ([], []))
    spec = {"name": "empty", "tools": tools}
    path = agent_dir / "empty.json"
    original = json.dumps(spec)
    path.write_text(original, encoding="utf-8")
    if addition:
        add_addition(
            "empty",
            "probe",
            {"command": "mcp-probe", "args": []},
            approved_by="owner",
            card_id="test-card",
        )
    request = MagicMock(spec=web.Request)
    request.method = "GET"
    request.match_info = {"name": "empty"}
    request.headers = {}
    request.app = {"state": MagicMock()}

    response = await agents.api_agent_detail(request)

    assert response.status == 200
    body = json.loads(response.text)
    assert body["session_lists_tools"] is expected
    assert notice.editable_agent_without_tools("empty") is not kas_expected
    assert body["tools"] == tools
    assert "mcpServers" not in body
    assert path.read_text(encoding="utf-8") == original
