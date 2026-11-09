# SPDX-License-Identifier: Apache-2.0
"""The team lead exposes the tools of every edition server it mounts.

Ultra Mode switches a chat to ``kirocrew-team-lead``. Its spec is written from
``build_agent_config``, which adds each ``McpToolingProvider.extra_mcp_servers()``
entry to ``mcpServers`` only, and kiro-cli exposes only servers named in
``tools``. So the lead mounted the edition's servers and could call none of them.
It now references each one in ``tools``, and never in ``allowedTools``, so
every call still prompts.
"""

from __future__ import annotations

import dataclasses
import json

from kiro_crew import agent
from kiro_crew.agent_files import TEAM_LEAD_AGENT_FILENAME
from kiro_crew.agent_materialization import service_agents
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import build_default_context, set_context
from kiro_crew.platform.context import reset_context

EDITION_SERVERS = {
    "edition-tools": {"command": "edition-tools", "args": ["--stdio"]},
    "edition-creds": {"command": "/opt/edition/creds", "args": []},
}


class _EditionTooling:
    def extra_mcp_servers(self):
        return {name: dict(spec) for name, spec in EDITION_SERVERS.items()}

    def extra_skills(self):
        return []


def _lead(tmp_path, monkeypatch, tooling) -> dict:
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    base = build_default_context(KiroCrewConfig())
    set_context(base if tooling is None else dataclasses.replace(base, mcp_tooling=tooling))
    try:
        service_agents._install_team_lead_agent()
    finally:
        reset_context()
    return json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))


def test_the_lead_references_every_edition_server_it_mounts(tmp_path, monkeypatch):
    spec = _lead(tmp_path, monkeypatch, _EditionTooling())
    for name in EDITION_SERVERS:
        assert name in spec["mcpServers"]
        assert spec["tools"].count(f"@{name}") == 1


def test_the_refs_expose_the_tools_without_approving_them(tmp_path, monkeypatch):
    allowed = _lead(tmp_path, monkeypatch, _EditionTooling()).get("allowedTools") or []
    for name in EDITION_SERVERS:
        assert not any(t == f"@{name}" or t.startswith(f"@{name}/") for t in allowed)


def test_the_dispatch_refs_are_kept(tmp_path, monkeypatch):
    tools = _lead(tmp_path, monkeypatch, _EditionTooling())["tools"]
    assert "@kirocrew-dashboard" in tools
    assert "@kirocrew-work" in tools


def test_a_standalone_lead_gains_no_ref(tmp_path, monkeypatch):
    spec = _lead(tmp_path, monkeypatch, None)
    for name in EDITION_SERVERS:
        assert f"@{name}" not in spec["tools"]
