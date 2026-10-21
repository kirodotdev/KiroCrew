# SPDX-License-Identifier: Apache-2.0
"""Every way a crewmate is made ends with its chat on the conductor.

The chat agent is chosen from the in-memory dispatch snapshot
(``mate_worker.is_mate_owned_agent``). A crewmate file the snapshot does not
know yet made the chat run as the file itself -- a full copy of the template,
shell and file writer included -- and its record then named that file, so its
hand-offs opened the wrong agent. Each creation path is driven with a snapshot
that is already warm and does not know the new file, which is the state a
running gateway is in when a crewmate is created.
"""

from __future__ import annotations

import argparse
import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_member_memory_creation_capability import _app, _environment

import kiro_crew.config.paths as paths_mod
from kiro_crew import mate_worker
from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

TEMPLATE = {
    "name": "docs-helper",
    "prompt": "Keep the docs current.",
    "tools": ["*"],
    "allowedTools": ["fs_read", "execute_bash", "fs_write"],
}


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A kiro home holding one template, and a warm snapshot that knows only it."""
    kiro = tmp_path / "kiro"
    (kiro / "agents").mkdir(parents=True)
    (kiro / "mates" / "agents").mkdir(parents=True)
    (kiro / "agents" / "docs-helper.json").write_text(json.dumps(TEMPLATE), encoding="utf-8")
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    monkeypatch.setattr(paths_mod, "_agents_dir_override", lambda: kiro / "agents")
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({"docs-helper", "kirocrew"}))
    monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset())
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
    # No rescan may rescue the answer: what the writer published is what counts.
    monkeypatch.setattr(loader, "refresh_materialized_agents", lambda **_: None)
    monkeypatch.setattr(loader, "schedule_materialized_agents_refresh", lambda: None)
    return kiro


def _chats_on(name: str) -> str:
    binding = KiroCrewConfig.load().agents[name]
    return mate_worker.member_chat_agent(binding.kiro_agent)


@pytest.mark.asyncio
async def test_a_crewmate_created_through_the_create_route_chats_on_the_conductor(
    home, monkeypatch
):
    await asyncio.to_thread(_environment, monkeypatch, "linux", "kas", "auto", "namespace", False)
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        response = await client.post(
            "/api/agents", json={"name": "atlas", "kiro_agent": "docs-helper"}
        )
        assert response.status == 200, await response.text()
    assert (home / "mates" / "agents" / "atlas.json").is_file()
    assert await asyncio.to_thread(_chats_on, "atlas") == MATE_CONDUCTOR_AGENT


def test_a_crewmate_created_on_the_command_line_chats_on_the_conductor(home, monkeypatch):
    from kiro_crew.cli_commands import _handle_agent

    _environment(monkeypatch, "linux", "kas", "auto", "namespace", False)
    _handle_agent(
        argparse.Namespace(
            agent_action="create",
            name="cove",
            kiro_agent="docs-helper",
            workspace="default",
            memory_store="default",
        )
    )
    assert KiroCrewConfig.load().agents["cove"].kiro_agent == "cove"
    assert _chats_on("cove") == MATE_CONDUCTOR_AGENT


def test_a_crewmate_moved_by_the_startup_migration_chats_on_the_conductor(home, monkeypatch):
    from kiro_crew.mate_config_migration import migrate_existing_crewmates

    worker = {"name": "kirocrew-mate-worker", "prompt": "worker", "tools": ["fs_read"]}
    (home / "mates" / "agents" / "kirocrew-mate-worker.json").write_text(
        json.dumps(worker), encoding="utf-8"
    )
    builder = {"name": "builder", "prompt": "worker\n\nShip notes.", "tools": ["fs_read"]}
    (home / "agents" / "builder.json").write_text(json.dumps(builder), encoding="utf-8")
    cfg = KiroCrewConfig()
    cfg.agents = {"briar": KiroCrewAgentConfig(member_id="briar", kiro_agent="builder")}
    monkeypatch.setattr(
        "kiro_crew.mate_config_migration.KiroCrewConfig",
        type("Shim", (), {"load": staticmethod(lambda: cfg)}),
    )
    monkeypatch.setattr("kiro_crew.memory_stores.persist_member_config", lambda *a, **k: None)
    report = migrate_existing_crewmates(cfg)
    assert report.migrated == ["briar"], report
    assert mate_worker.member_chat_agent("briar") == MATE_CONDUCTOR_AGENT


def test_the_first_crewmate_file_written_at_start_chats_on_the_conductor(home, monkeypatch):
    """The first crewmate is bound to the conductor and gets its file at start."""
    from kiro_crew.mate_config_migration import migrate_existing_crewmates

    worker = {"name": "kirocrew-mate-worker", "prompt": "worker", "tools": ["fs_read"]}
    (home / "mates" / "agents" / "kirocrew-mate-worker.json").write_text(
        json.dumps(worker), encoding="utf-8"
    )
    (home / "agents" / "kirocrew.json").write_text(
        json.dumps({"name": "kirocrew", "prompt": "worker", "tools": ["fs_read"]}),
        encoding="utf-8",
    )
    cfg = KiroCrewConfig()
    cfg.agents = {"mate": KiroCrewAgentConfig(member_id="mate", kiro_agent=MATE_CONDUCTOR_AGENT)}
    monkeypatch.setattr(
        "kiro_crew.mate_config_migration.KiroCrewConfig",
        type("Shim", (), {"load": staticmethod(lambda: cfg)}),
    )
    monkeypatch.setattr("kiro_crew.memory_stores.persist_member_config", lambda *a, **k: None)
    migrate_existing_crewmates(cfg)
    assert (home / "mates" / "agents" / "mate.json").is_file()
    assert mate_worker.member_chat_agent(cfg.agents["mate"].kiro_agent) == MATE_CONDUCTOR_AGENT


def test_a_record_naming_the_crewmates_own_file_is_its_conductor(monkeypatch):
    """A thread whose record was written before the snapshot knew the file."""
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({"atlas"}))
    monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset({"atlas"}))
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
    stale = ExecutionContext(
        member_id="atlas",
        store=MemoryStoreRef("member-atlas", "atlas"),
        selection_kind="member",
        template_id="atlas",
    )
    assert mate_worker.is_conductor_execution(stale)
    worker = ExecutionContext(
        member_id="atlas",
        store=MemoryStoreRef("member-atlas", "atlas"),
        selection_kind="template",
        template_id="atlas",
    )
    assert not mate_worker.is_conductor_execution(worker)


def test_an_id_less_crewmates_stale_record_still_dispatches_as_that_crewmate(monkeypatch):
    from kiro_crew.dashboard.session_control_parts.session_create import _dispatching_crewmate

    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({"pixel"}))
    monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset({"pixel"}))
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
    cfg = KiroCrewConfig()
    cfg.agents = {"pixel": KiroCrewAgentConfig(kiro_agent="pixel")}
    stale = ExecutionContext(
        member_id=None,
        store=MemoryStoreRef("default"),
        selection_kind="member",
        template_id="pixel",
        selection_name="pixel",
    )
    assert _dispatching_crewmate(cfg, None, stale) is cfg.agents["pixel"]
