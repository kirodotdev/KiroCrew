"""The migration path almost every crewmate takes: Built from ``kirocrew``.

The old agent and the worker base are not fixtures here: they are what this
build's own generator writes (``agent.rebuild_agent_config``, the code that writes
``~/.kiro/agents/kirocrew.json`` and ``kirocrew-mate-worker.json``), with their
real servers, ``allowedTools`` and ``autoApprove``. A change to either that would
make the dry run keep a ``kirocrew`` crewmate where it is turns this red.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import agent
from kiro_crew.agent_files import (
    AGENT_FILENAME,
    MATE_CONDUCTOR_AGENT_FILENAME,
    MATE_WORKER_AGENT_FILENAME,
)
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.mate_config_migration import dry_run, migrate_existing_crewmates, migration_file


@pytest.fixture()
def generated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """This build's own ``kirocrew`` and worker base, written by the real generator."""
    # The generator may resolve the default data home; keep that in tmp_path too.
    monkeypatch.setattr(paths_mod, "_resolve_default_home", lambda: tmp_path / "default")
    kiro = tmp_path / "home"
    user = kiro / "agents"
    user.mkdir(parents=True)
    mates = kiro / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: user)
    # The generator writes the mate conductor and worker into the crewmate folder.
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", user)
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: SPEC_PERMISSIONS_MIN_VERSION
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    launcher = bindir / "kirocrew"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setattr(agent, "_KIROCREW_BIN", str(launcher))
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", tmp_path / "kiro-global-mcp.json")
    monkeypatch.setattr(agent, "_DEFAULT_KIRO_HOOKS_DIR", tmp_path / "hooks")
    monkeypatch.setattr("kiro_crew.apps.bridges._mcp_json_path", lambda: user / AGENT_FILENAME)
    agent.rebuild_agent_config()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    return user, mates


def _spec(directory: Path, filename: str) -> dict:
    return json.loads((directory / filename).read_text(encoding="utf-8"))


def test_the_generated_templates_are_the_real_shapes(generated):
    """Guard against a vacuous pass: both files exist and carry grants."""
    user, _mates = generated
    kirocrew = _spec(user, AGENT_FILENAME)
    base = _spec(_mates, MATE_WORKER_AGENT_FILENAME)
    assert kirocrew["allowedTools"] and kirocrew["mcpServers"]
    assert base["mcpServers"] and base.get("excludedTools")
    # The real rebuild writes the mate conductor and worker beside the crewmates,
    # never into the folder kiro-cli lists.
    assert (_mates / MATE_CONDUCTOR_AGENT_FILENAME).exists()
    assert not (user / MATE_WORKER_AGENT_FILENAME).exists()
    assert not (user / MATE_CONDUCTOR_AGENT_FILENAME).exists()


def test_a_kirocrew_crewmate_passes_the_dry_run(generated):
    from kiro_crew.mate_config_migration import _governed_source, _worker_base

    base = _worker_base()
    source = _governed_source("kirocrew")
    planned = migration_file("josh", base, source)

    assert dry_run(base, source, planned) == ""


def test_a_kirocrew_crewmate_moves_with_a_tiny_kit(generated, monkeypatch):
    _user, mates = generated
    cfg = KiroCrewConfig()
    cfg.agents = {"josh": KiroCrewAgentConfig(member_id="josh", kiro_agent="kirocrew")}
    monkeypatch.setattr(
        "kiro_crew.mate_config_migration.KiroCrewConfig",
        type("Shim", (), {"load": staticmethod(lambda: cfg)}),
    )
    monkeypatch.setattr("kiro_crew.memory_stores.persist_member_config", lambda c, n, **kw: None)

    report = migrate_existing_crewmates(cfg)

    assert report.dry_run_failed == {} and report.copy_failed == {}
    assert report.migrated == ["josh"]
    written = json.loads((mates / "josh.json").read_text(encoding="utf-8"))
    # What kirocrew adds to the worker base: nothing a crewmate owns but its name.
    assert written.get("prompt", "") == ""
    assert not written.get("skills") and not written.get("resources")
    assert len(written.get("mcpServers", {})) <= 6


#: What an older install left in ``kirocrew.json`` beyond this build's fresh one,
#: read off a live install's file: the rebuild keeps a user's grants, while
#: the worker base is re-derived fresh, so these are exactly what the dry run met.
_OLDER_INSTALL_GRANTS = ("@kirocrew-cron", "@kirocrew-guide/rename_self")


def test_an_upgraded_kirocrew_still_moves(generated, monkeypatch):
    """The live shape: the older install's grants survive the rebuild, the base has none."""
    user, mates = generated
    path = user / AGENT_FILENAME
    spec = _spec(user, AGENT_FILENAME)
    spec["allowedTools"] = [*spec["allowedTools"], *_OLDER_INSTALL_GRANTS]
    path.write_text(json.dumps(spec), encoding="utf-8")
    agent.rebuild_agent_config()
    kept = set(_spec(user, AGENT_FILENAME)["allowedTools"])
    base = set(_spec(mates, MATE_WORKER_AGENT_FILENAME).get("allowedTools", []))
    assert set(_OLDER_INSTALL_GRANTS) <= kept - base  # the shape Ray's dry run skipped
    cfg = KiroCrewConfig()
    cfg.agents = {"josh": KiroCrewAgentConfig(member_id="josh", kiro_agent="kirocrew")}
    monkeypatch.setattr(
        "kiro_crew.mate_config_migration.KiroCrewConfig",
        type("Shim", (), {"load": staticmethod(lambda: cfg)}),
    )
    monkeypatch.setattr("kiro_crew.memory_stores.persist_member_config", lambda c, n, **kw: None)

    report = migrate_existing_crewmates(cfg)

    assert report.dry_run_failed == {}
    assert report.migrated == ["josh"]
    written = json.loads((mates / "josh.json").read_text(encoding="utf-8"))
    assert written.get("prompt", "") == ""
