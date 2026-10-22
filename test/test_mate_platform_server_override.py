# SPDX-License-Identifier: Apache-2.0
"""A crewmate's file never carries a platform server it cannot launch.

A template without ``kirocrew-mate`` must not get ``"kirocrew-mate":
{"disabled": false}`` written into the crewmate's file at creation: that is a
server with no command, which a session that ran the file would wait on until
its MCP readiness timed out. The shapes here are the templates the soak run used
(``temp-screenshots/soak/driver/broken-templates/``): ``docs-helper`` (no
``kirocrew-mate``) and the ``Briar`` file it produced.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_member_memory_creation_capability import _app, _environment

import kiro_crew.config.paths as paths_mod
from kiro_crew import mate_worker
from kiro_crew.config import loader

NOTES = {"command": "python3", "args": ["stub_mcp.py", "notes"]}
#: The soak's ``docs-helper`` template: its own prompt, a ``notes`` server, no
#: ``kirocrew-mate``.
DOCS_HELPER = {
    "name": "docs-helper",
    "description": "Keeps the changelog and README current.",
    "prompt": "DOCS-HELPER-V1",
    "tools": ["*"],
    "allowedTools": ["@notes"],
    "includeMcpJson": True,
    "resources": [],
    "mcpServers": {"notes": NOTES},
}
#: The file that template produced: the bare override beside ``notes``.
BRIAR = {
    **DOCS_HELPER,
    "name": "Briar",
    "mcpServers": {"notes": NOTES, "kirocrew-mate": {"disabled": False}},
}


@pytest.fixture
def home(monkeypatch, tmp_path):
    kiro = tmp_path / "kiro"
    (kiro / "agents").mkdir(parents=True)
    (kiro / "mates" / "agents").mkdir(parents=True)
    (kiro / "agents" / "docs-helper.json").write_text(json.dumps(DOCS_HELPER), encoding="utf-8")
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    monkeypatch.setattr(paths_mod, "_agents_dir_override", lambda: kiro / "agents")
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({"docs-helper"}))
    monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset())
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
    return kiro


def _servers(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})


@pytest.mark.asyncio
async def test_creating_from_a_template_without_the_mate_server_writes_no_override(
    home, monkeypatch
):
    await asyncio.to_thread(_environment, monkeypatch, "linux", "kas", "auto", "namespace", False)
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        response = await client.post(
            "/api/agents", json={"name": "briar", "kiro_agent": "docs-helper"}
        )
        assert response.status == 200, await response.text()
    servers = _servers(home / "mates" / "agents" / "briar.json")
    assert "kirocrew-mate" not in servers
    assert servers["notes"] == NOTES


def test_a_template_carrying_the_bare_override_is_copied_without_it(home):
    from kiro_crew.mate_config import write_mate_config

    (home / "agents" / "with-switch.json").write_text(
        json.dumps({**BRIAR, "name": "with-switch"}), encoding="utf-8"
    )
    path = write_mate_config("cove", "with-switch")
    assert set(_servers(path)) == {"notes"}


def test_a_crewmate_file_holding_the_bare_override_is_repaired_at_start(home):
    from kiro_crew.mate_config_migration import repair_partial_platform_servers

    path = home / "mates" / "agents" / "Briar.json"
    path.write_text(json.dumps(BRIAR), encoding="utf-8")
    assert repair_partial_platform_servers() == ["Briar"]
    assert _servers(path) == {"notes": NOTES}
    # Idempotent: a repaired file is not written again.
    assert repair_partial_platform_servers() == []


def test_the_startup_migration_runs_the_repair(home):
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.mate_config_migration import migrate_existing_crewmates

    path = home / "mates" / "agents" / "Briar.json"
    path.write_text(json.dumps(BRIAR), encoding="utf-8")
    migrate_existing_crewmates(KiroCrewConfig())
    assert set(_servers(path)) == {"notes"}


def test_reading_a_crewmate_file_never_hands_on_the_bare_override(home):
    from kiro_crew.acp.harness.kas import resolve_projected_spec

    (home / "mates" / "agents" / "Briar.json").write_text(json.dumps(BRIAR), encoding="utf-8")
    spec, _ = resolve_projected_spec(home / "agents", "Briar", None)
    assert set(spec["mcpServers"]) == {"notes"}


def test_only_a_bare_switch_on_a_platform_server_is_dropped():
    spec = {
        "mcpServers": {
            "kirocrew-mate": {"disabled": False},
            "kirocrew-core": {"disabledTools": ["spawn_run"]},
            "kirocrew-guide": {"command": "kc", "args": ["guide"]},
            "notes": {"disabled": False},
        }
    }
    assert mate_worker.drop_partial_platform_servers(spec) == ["kirocrew-mate"]
    assert set(spec["mcpServers"]) == {"kirocrew-core", "kirocrew-guide", "notes"}
