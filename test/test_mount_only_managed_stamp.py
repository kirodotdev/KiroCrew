"""A mount-only record of an opt-in managed server can be taken away again.

``template_mcp_additions.reconcile_default_spec`` stamps each entry it merges into
``kirocrew.json`` with ``MERGED_MARK`` and removes a stamped entry once its record
is gone. For an opt-in managed server (``kirocrew-dashboard``, ``kirocrew-debug``)
the next rebuild's managed refresh runs ``_enforce_managed_mcp_ownership`` over
the entry, which drops every key outside the managed allow-list. Pinned here: the
refresh keeps the stamp on an opt-in entry, so removing the record unmounts the
server; an unstamped opt-in entry and an always-on entry are refreshed as before;
and the worker never gets the server.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import template_mcp_additions as additions
from kiro_crew.agent_files import WORKER_AGENT_FILENAME

DASH = "kirocrew-dashboard"
DEBUG = "kirocrew-debug"


@pytest.fixture()
def env(tmp_path, monkeypatch) -> Path:
    """A throwaway project, data home and agents dir for ``rebuild_agent_config``."""
    from kiro_crew.apps import bridges

    project = tmp_path / "project" / "agents"
    project.mkdir(parents=True)
    (project / "defaults.json").write_text(
        json.dumps({"name": "kirocrew", "tools": ["@kirocrew-core"]}), encoding="utf-8"
    )
    (project / "prompt.md").write_text("prompt", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "project"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))

    kiro_dir = tmp_path / ".kiro" / "agents"
    kiro_dir.mkdir(parents=True)
    spec_path = kiro_dir / "kirocrew.json"
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: kiro_dir)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "absent-kiro.json")
    monkeypatch.setattr(agent_mod, "_CC_MCP_JSON", tmp_path / "absent-cc.json")
    monkeypatch.setattr(bridges, "_mcp_json_path", lambda: spec_path)
    agent_mod.rebuild_agent_config()
    return spec_path


def _spec(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, spec: dict) -> None:
    path.write_text(json.dumps(spec), encoding="utf-8")


def _refs(spec: dict, key: str, server: str) -> list[str]:
    return [r for r in spec.get(key, []) if str(r).startswith(f"@{server}")]


def _record(server: str, subcommand: str) -> None:
    additions.add_addition(
        additions.DEFAULT_TEMPLATE,
        server,
        agent_mod._managed_opt_in_entry(subcommand),
        approved_by="test",
        card_id="test",
        mount_only=True,
    )


def test_both_servers_are_opt_in_managed_servers():
    for name in (DASH, DEBUG):
        assert agent_mod._MANAGED_MCP_SERVERS[name].get("opt_in") is True


def test_a_recorded_opt_in_set_mounts_with_no_grant_and_keeps_its_stamp(env):
    _record(DASH, "mcp-dashboard")
    agent_mod.rebuild_agent_config()
    agent_mod.rebuild_agent_config()
    spec = _spec(env)
    entry = spec["mcpServers"][DASH]
    assert entry.get(additions.MERGED_MARK) is True
    assert f"@{DASH}" in spec["tools"]
    assert "autoApprove" not in entry
    assert _refs(spec, "allowedTools", DASH) == []


def test_a_recorded_opt_in_set_leaves_once_its_record_is_gone(env):
    """Removing the record unmounts the set even after the managed refresh ran over it."""
    _record(DASH, "mcp-dashboard")
    agent_mod.rebuild_agent_config()
    agent_mod.rebuild_agent_config()
    assert additions.remove_addition(additions.DEFAULT_TEMPLATE, DASH) is True
    agent_mod.rebuild_agent_config()
    spec = _spec(env)
    assert DASH not in spec["mcpServers"]
    assert _refs(spec, "tools", DASH) == []


def test_the_worker_never_gets_a_recorded_opt_in_set(env):
    _record(DASH, "mcp-dashboard")
    _record(DEBUG, "mcp-debug")
    agent_mod.rebuild_agent_config()
    agent_mod.rebuild_agent_config()
    worker = _spec(env.parent / WORKER_AGENT_FILENAME)
    for name in (DASH, DEBUG):
        assert name not in worker.get("mcpServers", {})
        assert _refs(worker, "tools", name) == []


def test_the_stamp_does_not_withhold_the_per_session_element(env):
    """The stamp is Crew's own bookkeeping, so a stamped entry still mounts per session."""
    from kiro_crew.acp import session_mcp

    _record(DASH, "mcp-dashboard")
    agent_mod.rebuild_agent_config()
    agent_mod.rebuild_agent_config()
    entry = _spec(env)["mcpServers"][DASH]
    assert entry.get(additions.MERGED_MARK) is True
    assert session_mcp.native_mount_withholding(DASH, entry, []) is None


def test_an_unstamped_opt_in_entry_is_refreshed_as_before(env):
    spec = _spec(env)
    entry = agent_mod._managed_opt_in_entry("mcp-debug")
    entry["cwd"] = "/tmp"
    spec["mcpServers"][DEBUG] = entry
    spec["tools"].append(f"@{DEBUG}")
    _write(env, spec)
    agent_mod.rebuild_agent_config()
    spec = _spec(env)
    assert "cwd" not in spec["mcpServers"][DEBUG]
    assert additions.MERGED_MARK not in spec["mcpServers"][DEBUG]
    assert f"@{DEBUG}" in spec["tools"]


def test_an_always_on_entry_does_not_keep_a_planted_stamp(env):
    spec = _spec(env)
    spec["mcpServers"]["kirocrew-core"][additions.MERGED_MARK] = True
    _write(env, spec)
    agent_mod.rebuild_agent_config()
    assert additions.MERGED_MARK not in _spec(env)["mcpServers"]["kirocrew-core"]
