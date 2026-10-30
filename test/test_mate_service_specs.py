# SPDX-License-Identifier: Apache-2.0
"""The mate conductor and worker specs live in ``~/.kiro/mates/agents/``.

kiro-cli lists every spec in ``~/.kiro/agents/``, and neither of these is picked by
hand, so the gateway generates them beside the crewmates' own files instead. Every
reader follows them there, a copy an earlier build left at the user level is
removed (or kept, with a warning, when it is not the generated one), and no
crewmate may own a file by either name.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import agent
from kiro_crew.agent_files import (
    MATE_CONDUCTOR_AGENT_FILENAME,
    MATE_SERVICE_AGENT_NAMES,
    MATE_WORKER_AGENT_FILENAME,
)
from kiro_crew.agent_materialization import service_agents

CONDUCTOR = "kirocrew-mate-conductor"
WORKER = "kirocrew-mate-worker"
INSTALLERS = {
    MATE_CONDUCTOR_AGENT_FILENAME: service_agents._install_mate_conductor_agent,
    MATE_WORKER_AGENT_FILENAME: service_agents._install_mate_worker_agent,
}


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    kiro = tmp_path / "kiro"
    user = kiro / "agents"
    user.mkdir(parents=True)
    mates = kiro / "mates" / "agents"
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: user)
    return user, mates


@pytest.mark.parametrize("filename", sorted(INSTALLERS))
def test_the_installer_writes_into_the_crewmate_folder_only(dirs, filename):
    user, mates = dirs
    INSTALLERS[filename]()
    written = json.loads((mates / filename).read_text(encoding="utf-8"))
    assert written["name"] == filename.removesuffix(".json")
    assert not (user / filename).exists()


@pytest.mark.parametrize("filename", sorted(INSTALLERS))
def test_a_copy_an_earlier_build_left_at_the_user_level_is_removed(dirs, filename):
    user, mates = dirs
    INSTALLERS[filename]()
    generated = json.loads((mates / filename).read_text(encoding="utf-8"))
    # What the earlier build wrote: same name and description, older content.
    (user / filename).write_text(
        json.dumps({**generated, "prompt": "an older prompt", "tools": []}), encoding="utf-8"
    )
    INSTALLERS[filename]()
    assert not (user / filename).exists()
    assert (mates / filename).exists()


@pytest.mark.parametrize("filename", sorted(INSTALLERS))
def test_a_user_level_file_that_is_not_the_generated_one_is_kept(dirs, filename, caplog):
    user, _mates = dirs
    mine = {"name": filename.removesuffix(".json"), "description": "my own", "tools": []}
    (user / filename).write_text(json.dumps(mine), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        INSTALLERS[filename]()
    assert json.loads((user / filename).read_text(encoding="utf-8")) == mine
    assert any("is not the spec Kiro Crew generated" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("name", sorted(MATE_SERVICE_AGENT_NAMES))
def test_kas_resolves_both_from_the_crewmate_folder_first(dirs, name):
    from kiro_crew.acp.harness.kas import resolve_projected_spec

    user, mates = dirs
    mates.mkdir(parents=True)
    (mates / f"{name}.json").write_text(json.dumps({"name": name, "prompt": "ours"}), "utf-8")
    spec, where = resolve_projected_spec(user, name, None)
    assert (spec["prompt"], where) == ("ours", mates)
    # A leftover at the user level does not win over the generated spec.
    (user / f"{name}.json").write_text(json.dumps({"name": name, "prompt": "left"}), "utf-8")
    spec, where = resolve_projected_spec(user, name, None)
    assert (spec["prompt"], where) == ("ours", mates)


def test_an_array_backed_host_reads_the_conductor_from_the_crewmate_folder(dirs, monkeypatch):
    from kiro_crew.acp import session_mcp

    _user, mates = dirs
    mates.mkdir(parents=True)
    spec = {"name": CONDUCTOR, "mcpServers": {"kirocrew-core": {"command": "kc"}}}
    (mates / f"{CONDUCTOR}.json").write_text(json.dumps(spec), "utf-8")
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mates)
    assert session_mcp._user_agent_spec(CONDUCTOR)["mcpServers"] == spec["mcpServers"]


def test_the_member_worker_spec_reads_the_moved_worker_base(dirs, monkeypatch):
    from kiro_crew import mate_worker

    _user, mates = dirs
    mates.mkdir(parents=True)
    base = {"name": WORKER, "tools": ["@jira"], "mcpServers": {"jira": {"command": "j"}}}
    (mates / f"{WORKER}.json").write_text(json.dumps(base), "utf-8")
    monkeypatch.setattr(mate_worker, "_roster_member_id", lambda member: "")
    monkeypatch.setattr(mate_worker, "conductor_mcp_servers", lambda: frozenset())
    spec = mate_worker.member_worker_spec("ada")
    assert "jira" in spec["mcpServers"]


def test_the_migration_reads_the_moved_worker_base(dirs):
    from kiro_crew.mate_config_migration import _worker_base

    _user, mates = dirs
    mates.mkdir(parents=True)
    (mates / f"{WORKER}.json").write_text(json.dumps({"name": WORKER, "prompt": "w"}), "utf-8")
    assert _worker_base()["prompt"] == "w"


# ── no crewmate owns a file by either name ──


def test_the_migration_skips_a_crewmate_named_after_a_service_spec(dirs, monkeypatch, caplog):
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.mate_config_migration import _candidates

    cfg = KiroCrewConfig()
    cfg.agents = {
        WORKER: KiroCrewAgentConfig(member_id="w1", kiro_agent="kirocrew"),
        "ada": KiroCrewAgentConfig(member_id="ada", kiro_agent="kirocrew"),
    }
    with caplog.at_level(logging.WARNING):
        assert _candidates(cfg) == {"ada": "kirocrew"}
    assert any("cannot be named after a spec" in r.getMessage() for r in caplog.records)


def test_a_binding_on_the_conductor_owns_no_crewmate_file(dirs, monkeypatch):
    from kiro_crew import execution_context, mate_worker

    _user, mates = dirs
    mates.mkdir(parents=True)
    (mates / f"{CONDUCTOR}.json").write_text(json.dumps({"name": CONDUCTOR}), "utf-8")
    binding = SimpleNamespace(kiro_agent=CONDUCTOR)
    monkeypatch.setattr(execution_context, "member_config_for_id", lambda c, m: ("mate", binding))
    assert mate_worker.mate_file_for_member("mate", config=object()) == ""


def test_the_snapshot_lists_both_as_declared_but_not_as_crewmates(dirs, monkeypatch):
    from kiro_crew.config import loader

    user, mates = dirs
    mates.mkdir(parents=True)
    for name in (CONDUCTOR, WORKER, "ada"):
        (mates / f"{name}.json").write_text(json.dumps({"name": name}), "utf-8")
    monkeypatch.setattr(loader, "kiro_agents_dir", lambda: user, raising=False)
    monkeypatch.setattr(loader, "mate_agents_dir", lambda: mates, raising=False)
    loader.refresh_materialized_agents(heal_default=False)
    assert {CONDUCTOR, WORKER, "ada"} <= set(loader._MATERIALIZED_AGENTS)
    assert "ada" in loader._MATERIALIZED_MATE_ONLY
    assert not (MATE_SERVICE_AGENT_NAMES & loader._MATERIALIZED_MATE_ONLY)
