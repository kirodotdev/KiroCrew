"""RFC *mate-owned-agent-config* Phase 2: migrating existing crewmates.

Proves the dry-run-before-repoint order the RFC's migration plan requires:
every candidate's copy is written and checked for payload equivalence BEFORE
any pointer moves, and a crewmate whose copy does not project identically is
left on its shared pointer rather than forced through.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.mate_config_migration import migrate_existing_crewmates


@pytest.fixture
def layout(monkeypatch, tmp_path):
    """A user-level agents dir and its sibling ``mates/agents`` dir."""
    kiro = tmp_path / "home"
    user = kiro / "agents"
    user.mkdir(parents=True)
    mates = kiro / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    return user, mates


def _write(directory: Path, name: str, spec: dict) -> None:
    (directory / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _config_with(agents: dict[str, KiroCrewAgentConfig]) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agents = agents
    return cfg


def test_a_crewmate_on_a_shared_template_is_migrated(layout, monkeypatch):
    user, mates = layout
    _write(
        user,
        "builder",
        {"name": "builder", "prompt": "builder prompt", "tools": ["fs_read"]},
    )
    cfg = _config_with({"zxc": KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder")})

    saved = {}
    monkeypatch.setattr(
        "kiro_crew.mate_config_migration.KiroCrewConfig",
        type("Shim", (), {"load": staticmethod(lambda: cfg)}),
    )
    monkeypatch.setattr(
        "kiro_crew.memory_stores.persist_member_config",
        lambda c, name, **kw: saved.setdefault(name, kw),
    )

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == ["zxc"]
    assert not report.copy_failed and not report.equivalence_failed and not report.repoint_failed
    written = json.loads((mates / "zxc.json").read_text(encoding="utf-8"))
    assert written["name"] == "zxc"
    assert written["prompt"] == "builder prompt"
    assert cfg.agents["zxc"].kiro_agent == "zxc"
    assert saved["zxc"]["changed_fields"] == {"kiro_agent"}


def test_an_already_owned_crewmate_is_not_a_candidate(layout, monkeypatch):
    user, mates = layout
    _write(mates, "qwe", {"name": "qwe", "prompt": "already mine"})
    cfg = _config_with({"qwe": KiroCrewAgentConfig(member_id="qwe", kiro_agent="qwe")})
    monkeypatch.setattr(
        "kiro_crew.mate_config_migration.KiroCrewConfig",
        type("Shim", (), {"load": staticmethod(lambda: cfg)}),
    )

    before = (mates / "qwe.json").read_bytes()
    report = migrate_existing_crewmates(cfg)

    assert report.migrated == []
    assert (mates / "qwe.json").read_bytes() == before  # untouched


def test_a_crewmate_with_no_kiro_agent_is_not_a_candidate(layout):
    cfg = _config_with({"ghost": KiroCrewAgentConfig(member_id="ghost", kiro_agent="")})
    report = migrate_existing_crewmates(cfg)
    assert report.migrated == []
    assert report.skipped_no_source == []
    assert report.copy_failed == {}


def test_an_unreadable_source_is_skipped_not_migrated(layout, monkeypatch):
    _user, _mates = layout
    cfg = _config_with({"zxc": KiroCrewAgentConfig(member_id="zxc", kiro_agent="does-not-exist")})
    monkeypatch.setattr(
        "kiro_crew.mate_config_migration.KiroCrewConfig",
        type("Shim", (), {"load": staticmethod(lambda: cfg)}),
    )

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == []
    assert report.skipped_no_source == ["zxc"]
    assert cfg.agents["zxc"].kiro_agent == "does-not-exist"  # unchanged


def test_running_twice_is_a_no_op_the_second_time(layout, monkeypatch):
    user, mates = layout
    _write(user, "builder", {"name": "builder", "prompt": "p", "tools": ["fs_read"]})
    cfg = _config_with({"zxc": KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder")})
    monkeypatch.setattr(
        "kiro_crew.mate_config_migration.KiroCrewConfig",
        type("Shim", (), {"load": staticmethod(lambda: cfg)}),
    )
    monkeypatch.setattr(
        "kiro_crew.memory_stores.persist_member_config",
        lambda c, name, **kw: setattr(c.agents[name], "kiro_agent", name),
    )

    first = migrate_existing_crewmates(cfg)
    assert first.migrated == ["zxc"]

    second = migrate_existing_crewmates(cfg)
    assert second.migrated == []  # already owns its id, not a candidate


def test_a_pointer_only_moves_after_equivalence_is_proven(layout, monkeypatch):
    """If the copy somehow projected differently, the pointer must NOT move."""
    user, mates = layout
    _write(user, "builder", {"name": "builder", "prompt": "p", "tools": ["fs_read"]})
    cfg = _config_with({"zxc": KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder")})
    monkeypatch.setattr(
        "kiro_crew.mate_config_migration.KiroCrewConfig",
        type("Shim", (), {"load": staticmethod(lambda: cfg)}),
    )
    monkeypatch.setattr("kiro_crew.mate_config_migration._equivalent", lambda before, after: False)
    repointed = []
    monkeypatch.setattr(
        "kiro_crew.mate_config_migration._repoint", lambda name: repointed.append(name)
    )

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == []
    assert "zxc" in report.equivalence_failed
    assert repointed == []  # the gate held: pass 2 never ran for this member
    assert cfg.agents["zxc"].kiro_agent == "builder"  # pointer untouched
