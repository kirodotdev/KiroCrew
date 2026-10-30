# SPDX-License-Identifier: Apache-2.0
"""A crewmate's switch-off of a worker-base server survives into its worker.

If ``worker_additions`` dropped every file entry whose name the base declares,
a whole-server ``disabled: true`` on a base server would be lost and the worker
would launch it. The switch-off is a restriction: the merge mutes the base's own entry
(never taking the file's transport), the migration keeps it, and the dry run reads
it as neither a loss nor a gain.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import mate_worker
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.mate_config_migration import (
    _read_back,
    dry_run,
    migrate_existing_crewmates,
    migration_file,
)

CORE = {"command": "kc", "args": ["core"]}
CRON = {"command": "kc", "args": ["cron"]}
CREDS = {"command": "creds-mcp", "args": ["serve"]}

BASE = {
    "name": "kirocrew-mate-worker",
    "prompt": "worker base prompt",
    "tools": ["fs_read", "@kirocrew-core", "@kirocrew-cron", "@creds"],
    "allowedTools": ["fs_read", "@kirocrew-cron", "@creds/list"],
    "mcpServers": {"kirocrew-core": CORE, "kirocrew-cron": CRON, "creds": CREDS},
}


def _copy(value):
    return json.loads(json.dumps(value))


def _source(**servers) -> dict:
    """The old agent: the base as a template carries it, with *servers* overriding entries."""
    spec = {k: _copy(v) for k, v in BASE.items() if k != "name"}
    spec["name"] = "builder"
    spec["mcpServers"].update(_copy(servers))
    return spec


def _merged(mate_spec: dict) -> dict:
    return mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, mate_spec))


class TestMerge:
    def test_a_base_server_the_file_switches_off_is_muted_in_the_worker(self):
        merged = _merged({"mcpServers": {"creds": {"disabled": True}}})
        assert merged["mcpServers"]["creds"] == {**CREDS, "disabled": True}
        assert "@creds/list" not in merged["allowedTools"]
        # The other base servers are untouched.
        assert merged["mcpServers"]["kirocrew-core"] == CORE
        assert "@kirocrew-cron" in merged["allowedTools"]

    def test_the_switch_off_never_brings_the_files_transport(self):
        merged = _merged({"mcpServers": {"creds": {"command": "evil", "disabled": True}}})
        assert merged["mcpServers"]["creds"]["command"] == "creds-mcp"
        assert merged["mcpServers"]["creds"]["disabled"] is True

    def test_a_non_boolean_switch_reads_as_off(self):
        merged = _merged({"mcpServers": {"creds": {"disabled": "yes"}}})
        assert merged["mcpServers"]["creds"]["disabled"] is True

    def test_an_explicit_on_changes_nothing(self):
        merged = _merged({"mcpServers": {"creds": {"disabled": False}}})
        assert merged["mcpServers"]["creds"] == CREDS

    def test_a_per_tool_limit_on_a_base_server_still_excludes_the_tool(self):
        merged = _merged({"mcpServers": {"creds": {"disabledTools": ["list"]}}})
        assert "@creds/list" in merged["excludedTools"]
        assert "@creds/list" not in merged["allowedTools"]

    def test_the_kas_projection_does_not_declare_the_muted_server(self):
        from kiro_crew.acp.kas_agents import _project_mcp_servers

        merged = _merged({"mcpServers": {"creds": {"disabled": True}}})
        projected = _project_mcp_servers(merged, "kirocrew-mate-worker", frozenset())
        assert "creds" not in projected
        assert "kirocrew-cron" in projected


def test_a_switched_off_platform_entry_survives_the_partial_entry_repair(monkeypatch):
    monkeypatch.setattr(
        mate_worker, "conductor_mcp_servers", lambda: frozenset({"kirocrew-cron", "kirocrew-mate"})
    )
    spec = {
        "mcpServers": {"kirocrew-cron": {"disabled": True}, "kirocrew-mate": {"disabled": False}}
    }
    assert mate_worker.drop_partial_platform_servers(spec) == ["kirocrew-mate"]
    assert spec["mcpServers"] == {"kirocrew-cron": {"disabled": True}}


@pytest.fixture
def layout(monkeypatch, tmp_path):
    kiro = tmp_path / "home"
    user = kiro / "agents"
    user.mkdir(parents=True)
    mates = kiro / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    monkeypatch.setattr(
        mate_worker, "conductor_mcp_servers", lambda: frozenset({"kirocrew-core", "kirocrew-cron"})
    )
    (mates / "kirocrew-mate-worker.json").write_text(json.dumps(BASE), encoding="utf-8")
    return user, mates


def _migrate(user: Path, source: dict, monkeypatch):
    (user / "builder.json").write_text(json.dumps(source), encoding="utf-8")
    cfg = KiroCrewConfig()
    cfg.agents = {"zxc": KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder")}
    monkeypatch.setattr(
        "kiro_crew.mate_config_migration.KiroCrewConfig",
        type("Shim", (), {"load": staticmethod(lambda: cfg)}),
    )
    monkeypatch.setattr("kiro_crew.memory_stores.persist_member_config", lambda *a, **k: None)
    return migrate_existing_crewmates(cfg)


@pytest.mark.parametrize("server", ["creds", "kirocrew-cron"])
def test_the_migration_keeps_a_switched_off_base_server(layout, monkeypatch, server):
    user, mates = layout
    report = _migrate(
        user, _source(**{server: {**BASE["mcpServers"][server], "disabled": True}}), monkeypatch
    )

    assert report.dry_run_failed == {}
    assert report.migrated == ["zxc"]
    written = json.loads((mates / "zxc.json").read_text(encoding="utf-8"))
    assert written["mcpServers"] == {server: {"disabled": True}}
    # The worker the file projects (read back as the worker merge reads it) keeps it off.
    worker = _merged(_read_back("zxc"))
    assert worker["mcpServers"][server] == {**BASE["mcpServers"][server], "disabled": True}


def test_the_dry_run_reads_the_switch_off_as_neither_loss_nor_gain(layout):
    source = _source(creds={**CREDS, "disabled": True})
    assert dry_run(BASE, source, migration_file("zxc", BASE, source)) == ""


def test_the_dry_run_refuses_a_file_that_would_start_the_server_again(layout):
    source = _source(creds={**CREDS, "disabled": True})
    planned = migration_file("zxc", BASE, source)
    planned["mcpServers"].pop("creds")

    reason = dry_run(BASE, source, planned)
    assert "would start" in reason and "'creds'" in reason
