"""An edition-supplied MCP server reaches ``tools`` once, however late it arrives.

``McpToolingProvider.extra_mcp_servers()`` can start returning a server after the
first install (its program is installed later, or a newer edition adds one). The
rebuild seeds the entry into ``mcpServers``; these tests pin that its ``@name``
ref follows once, and that a ref the user then removes stays removed. The last tests
pin that a same-named Kiro-global or store entry cannot replace or drop the
edition's args/env, that a spec that already lost the env gets it back, and that
the store's env reaches the specs derived from the template too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import agent
from kiro_crew.agent_files import (
    AGENT_FILENAME,
    MATE_WORKER_AGENT_FILENAME,
    RESEARCH_AGENT_FILENAME,
    TEAM_LEAD_AGENT_FILENAME,
)
from kiro_crew.agent_materialization import guide_platform, mcp_aliases
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

EXTRA = "edition-late"
EXTRA_REF = f"@{EXTRA}"


@pytest.fixture()
def rebuildable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An agents dir a full ``rebuild_agent_config`` can write into."""
    directory = tmp_path / "agents"
    directory.mkdir()
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: directory)
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", directory)
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: tmp_path / "mates" / "agents")
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
    monkeypatch.setattr("kiro_crew.apps.bridges._mcp_json_path", lambda: directory / AGENT_FILENAME)
    return directory


def _supply(monkeypatch: pytest.MonkeyPatch, extras: dict[str, dict[str, Any]]) -> None:
    monkeypatch.setattr(
        agent, "_extra_mcp_servers", lambda: {k: dict(v) for k, v in extras.items()}
    )


def _extra_spec() -> dict[str, Any]:
    return {"command": agent._KIROCREW_BIN, "args": ["mcp-late"]}


def _spec(agents_dir: Path) -> dict[str, Any]:
    return json.loads((agents_dir / AGENT_FILENAME).read_text(encoding="utf-8"))


def test_a_server_supplied_after_install_gains_its_ref_once(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _supply(monkeypatch, {})
    agent.rebuild_agent_config()
    assert EXTRA_REF not in _spec(rebuildable)["tools"]

    _supply(monkeypatch, {EXTRA: _extra_spec()})
    agent.rebuild_agent_config()
    spec = _spec(rebuildable)
    assert EXTRA in spec["mcpServers"]
    assert EXTRA_REF in spec["tools"]
    # Mounted, never pre-approved: its calls still reach the PreToolUse gate.
    assert EXTRA_REF not in spec["allowedTools"]
    marker = json.loads((agent.config_dir() / guide_platform.EDITION_REFS_MARKER).read_text())
    assert marker["servers"] == [EXTRA]


def test_a_ref_the_user_removed_stays_removed(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _supply(monkeypatch, {})
    agent.rebuild_agent_config()
    _supply(monkeypatch, {EXTRA: _extra_spec()})
    agent.rebuild_agent_config()
    spec = _spec(rebuildable)
    spec["tools"].remove(EXTRA_REF)
    (rebuildable / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")

    agent.rebuild_agent_config()
    spec = _spec(rebuildable)
    assert EXTRA in spec["mcpServers"]
    assert EXTRA_REF not in spec["tools"]


def test_a_fresh_install_refs_the_server_and_records_it(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _supply(monkeypatch, {EXTRA: _extra_spec()})
    agent.rebuild_agent_config()
    spec = _spec(rebuildable)
    assert EXTRA_REF in spec["tools"]
    assert EXTRA_REF not in spec["allowedTools"]
    # Recorded, so a later removal is not undone by the existing-spec grant.
    spec["tools"].remove(EXTRA_REF)
    (rebuildable / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent.rebuild_agent_config()
    assert EXTRA_REF not in _spec(rebuildable)["tools"]


def test_a_gated_off_server_gains_no_ref_and_stays_unrecorded(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent_materialization import managed_mcp

    _supply(monkeypatch, {})
    agent.rebuild_agent_config()
    _supply(monkeypatch, {EXTRA: _extra_spec()})
    monkeypatch.setattr(managed_mcp, "_gated_off_servers", lambda: frozenset({EXTRA}))
    agent.rebuild_agent_config()
    assert EXTRA_REF not in _spec(rebuildable)["tools"]
    assert not (agent.config_dir() / guide_platform.EDITION_REFS_MARKER).exists()


@pytest.mark.parametrize("fresh_install", [False, True])
def test_a_slashed_server_gains_its_alias_ref_once(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch, fresh_install: bool
) -> None:
    if not fresh_install:
        _supply(monkeypatch, {})
        agent.rebuild_agent_config()
    name = "ns/late-svc"
    extra = _extra_spec()
    normalized = {"mcpServers": {name: extra}}
    alias = mcp_aliases.normalize_server_keys(normalized, set())[name]
    assert alias != name
    ref = f"@{alias}"

    _supply(monkeypatch, {name: extra})
    agent.rebuild_agent_config()
    spec = _spec(rebuildable)
    assert alias in spec["mcpServers"]
    assert name not in spec["mcpServers"]
    assert spec["tools"].count(ref) == 1
    assert f"@{name}" not in spec["tools"]
    assert ref not in spec["allowedTools"]
    marker_path = agent.config_dir() / guide_platform.EDITION_REFS_MARKER
    assert json.loads(marker_path.read_text(encoding="utf-8"))["servers"] == [name]

    spec["tools"].remove(ref)
    (rebuildable / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent.rebuild_agent_config()
    assert alias in _spec(rebuildable)["mcpServers"]
    assert ref not in _spec(rebuildable)["tools"]
    assert json.loads(marker_path.read_text(encoding="utf-8"))["servers"] == [name]


@pytest.mark.parametrize("extra", [EXTRA, "ns/late-svc"])
def test_a_server_narrowed_to_tool_refs_keeps_them_without_a_record(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch, extra: str
) -> None:
    _supply(monkeypatch, {})
    agent.rebuild_agent_config()
    marker_path = agent.config_dir() / guide_platform.EDITION_REFS_MARKER
    marker_path.unlink(missing_ok=True)
    normalized = {"mcpServers": {extra: _extra_spec()}}
    alias = mcp_aliases.normalize_server_keys(normalized, set())[extra]
    ref = f"@{alias}"
    narrowed = f"{ref}/some_tool"
    spec = _spec(rebuildable)
    spec["tools"].append(narrowed)
    (rebuildable / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")

    _supply(monkeypatch, {extra: _extra_spec()})
    agent.rebuild_agent_config()
    tools = _spec(rebuildable)["tools"]
    assert ref not in tools
    assert tools.count(narrowed) == 1
    assert json.loads(marker_path.read_text(encoding="utf-8"))["servers"] == [extra]


_EDITION_ARGS = ["mcp-late", "--include-tool-tags", "default,code-review"]
_EDITION_ENV = {"TOOL_PERSONALIZATION_ENABLED": "true"}


@pytest.mark.parametrize(
    "global_extra",
    [
        pytest.param({}, id="bare-command"),
        pytest.param({"args": ["--include-tools", "OnlyRead"]}, id="narrowing-args"),
    ],
)
def test_a_kiro_global_entry_does_not_replace_an_edition_servers_invocation(
    rebuildable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, global_extra: dict
) -> None:
    edition = {"command": agent._KIROCREW_BIN, "args": _EDITION_ARGS, "env": _EDITION_ENV}
    _supply(monkeypatch, {EXTRA: edition})
    shared = {EXTRA: {"command": agent._KIROCREW_BIN, "timeout": 5000, **global_extra}}
    (tmp_path / "kiro-global-mcp.json").write_text(
        json.dumps({"mcpServers": shared}), encoding="utf-8"
    )

    # The first pass builds fresh; the second starts from the spec on disk.
    for _ in range(2):
        agent.rebuild_agent_config()
        entry = _spec(rebuildable)["mcpServers"][EXTRA]
        assert entry["args"] == _EDITION_ARGS
        assert entry.get("env", {}).get("TOOL_PERSONALIZATION_ENABLED") == "true"
        # Only the invocation is the edition's: the global's timeout still lands.
        assert entry["timeout"] == 5000


def test_a_spec_that_lost_the_edition_env_gets_it_back(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edition = {"command": agent._KIROCREW_BIN, "args": _EDITION_ARGS, "env": _EDITION_ENV}
    _supply(monkeypatch, {EXTRA: edition})
    agent.rebuild_agent_config()
    # An older build let a same-named hand-added entry replace the env.
    spec = _spec(rebuildable)
    spec["mcpServers"][EXTRA]["env"] = {"USER_KEY": "mine"}
    (rebuildable / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")

    agent.rebuild_agent_config()
    env = _spec(rebuildable)["mcpServers"][EXTRA]["env"]
    assert env == {"USER_KEY": "mine", **_EDITION_ENV}


def test_a_store_entry_keeps_the_edition_env_keys(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edition_env = {"TOOL_PERSONALIZATION_ENABLED": "true", "EDITION_MODE": "full"}
    edition = {"command": agent._KIROCREW_BIN, "args": _EDITION_ARGS, "env": edition_env}
    _supply(monkeypatch, {EXTRA: edition})
    store = agent._user_dir() / "mcp.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store_env = {"USER_KEY": "mine", "TOOL_PERSONALIZATION_ENABLED": "false"}
    store.write_text(
        json.dumps({"mcpServers": {EXTRA: {"command": agent._KIROCREW_BIN, "env": store_env}}}),
        encoding="utf-8",
    )

    # The first pass builds fresh; the second starts from the spec on disk.
    for _ in range(2):
        agent.rebuild_agent_config()
        env = _spec(rebuildable)["mcpServers"][EXTRA]["env"]
        # The store's own keys win; the edition's other keys are not dropped.
        assert env == {"EDITION_MODE": "full", **store_env}


def test_a_key_removed_from_the_store_entry_leaves_the_spec(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edition_env = {"TOOL_PERSONALIZATION_ENABLED": "true", "EDITION_MODE": "full"}
    edition = {"command": agent._KIROCREW_BIN, "args": _EDITION_ARGS, "env": edition_env}
    _supply(monkeypatch, {EXTRA: edition})
    store = agent._user_dir() / "mcp.json"
    store.parent.mkdir(parents=True, exist_ok=True)

    def _write_store(env: dict[str, str]) -> None:
        store.write_text(
            json.dumps({"mcpServers": {EXTRA: {"command": agent._KIROCREW_BIN, "env": env}}}),
            encoding="utf-8",
        )

    _write_store({"USER_KEY": "mine", "TOOL_PERSONALIZATION_ENABLED": "false"})
    agent.rebuild_agent_config()
    # The user drops their key and their override; the rebuild starts from the
    # spec on disk, which still carries both.
    _write_store({"OTHER": "x"})
    agent.rebuild_agent_config()

    env = _spec(rebuildable)["mcpServers"][EXTRA]["env"]
    assert env == {**edition_env, "OTHER": "x"}


def _write_store_env(env: dict[str, str]) -> None:
    store = agent._user_dir() / "mcp.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    entry = {"command": agent._KIROCREW_BIN, "env": env}
    store.write_text(json.dumps({"mcpServers": {EXTRA: entry}}), encoding="utf-8")


def _entries(rebuildable: Path, tmp_path: Path) -> dict[str, dict[str, Any]]:
    """The edition entry in the default spec and in each spec derived from the template."""
    paths = {
        "default": rebuildable / AGENT_FILENAME,
        "mate-worker": tmp_path / "mates" / "agents" / MATE_WORKER_AGENT_FILENAME,
        "research": rebuildable / RESEARCH_AGENT_FILENAME,
        "team-lead": rebuildable / TEAM_LEAD_AGENT_FILENAME,
    }
    return {
        label: json.loads(path.read_text(encoding="utf-8"))["mcpServers"][EXTRA]
        for label, path in paths.items()
    }


def test_a_store_entry_s_env_reaches_every_spec_derived_from_the_template(
    rebuildable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Bot's worker, research and the team lead run the server as the default spec does.

    They are built from ``build_agent_config``, not from the default spec, so the
    store's env must reach them through the template's own edition entry.
    """
    edition_env = {"TOOL_PERSONALIZATION_ENABLED": "true", "EDITION_MODE": "full"}
    edition = {"command": agent._KIROCREW_BIN, "args": _EDITION_ARGS, "env": edition_env}
    _supply(monkeypatch, {EXTRA: edition})
    store_env = {"USER_KEY": "mine", "TOOL_PERSONALIZATION_ENABLED": "false"}
    _write_store_env(store_env)

    agent.rebuild_agent_config()
    for label, entry in _entries(rebuildable, tmp_path).items():
        assert entry["env"] == {"EDITION_MODE": "full", **store_env}, label


def test_a_store_path_is_emitted_in_every_derived_spec_as_in_the_default(
    rebuildable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared ``PATH`` is the effective one everywhere, never the bare declaration."""
    edition = {"command": agent._KIROCREW_BIN, "args": _EDITION_ARGS, "env": _EDITION_ENV}
    _supply(monkeypatch, {EXTRA: edition})
    _write_store_env({"PATH": "/opt/edition-tools/bin"})

    agent.rebuild_agent_config()
    entries = _entries(rebuildable, tmp_path)
    assert entries["default"]["env"]["PATH"] != "/opt/edition-tools/bin"
    for label, entry in entries.items():
        assert entry["env"]["PATH"] == entries["default"]["env"]["PATH"], label


@pytest.mark.parametrize(
    "default_routed",
    [pytest.param(frozenset(), id="unrouted"), pytest.param(frozenset({EXTRA}), id="routed")],
)
def test_a_store_secret_reference_never_reaches_a_derived_spec(
    rebuildable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, default_routed: frozenset
) -> None:
    """A ``secret://`` value stays out of every derived spec, whatever the default routes.

    Some sessions of a derived spec are handed no gateway stub and would start the
    server with the literal reference, so the store's other keys reach the spec and
    that one does not. The default spec still decides it from its own routing.
    """
    from kiro_crew.agent_materialization import mcp_sources
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted
    from kiro_crew.mcp_provenance import is_withheld_mute

    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", lambda: default_routed)
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", dict)
    edition = {"command": agent._KIROCREW_BIN, "args": _EDITION_ARGS, "env": _EDITION_ENV}
    _supply(monkeypatch, {EXTRA: edition})
    _write_store_env({"TOKEN": "secret://edition-token", "USER_KEY": "mine"})

    agent.rebuild_agent_config()
    entries = _entries(rebuildable, tmp_path)
    default = entries.pop("default")
    assert default["env"]["TOKEN"] == "secret://edition-token"
    assert is_withheld_mute(default) == (not default_routed)
    for label, entry in entries.items():
        assert entry["env"] == {**_EDITION_ENV, "USER_KEY": "mine"}, label
        assert not mcp_entry_is_muted(entry), label


@pytest.mark.parametrize(
    "store_entry",
    [
        pytest.param({"args": []}, id="empty-args"),
        pytest.param({"args": ["--include-tools", "OnlyRead"]}, id="narrowing-args"),
        pytest.param({"command": "/elsewhere/edition-late", "args": []}, id="other-command"),
    ],
)
def test_a_store_entry_does_not_replace_an_edition_servers_invocation(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch, store_entry: dict
) -> None:
    edition = {"command": agent._KIROCREW_BIN, "args": _EDITION_ARGS, "env": _EDITION_ENV}
    _supply(monkeypatch, {EXTRA: edition})
    store = agent._user_dir() / "mcp.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    entry = {"command": agent._KIROCREW_BIN, "timeout": 7000, **store_entry}
    store.write_text(json.dumps({"mcpServers": {EXTRA: entry}}), encoding="utf-8")

    # The first pass builds fresh; the second starts from the spec on disk.
    for _ in range(2):
        agent.rebuild_agent_config()
        spec = _spec(rebuildable)["mcpServers"][EXTRA]
        assert spec["command"] == agent._KIROCREW_BIN
        assert spec["args"] == _EDITION_ARGS
        # Only the invocation is the edition's: the store's other keys still land.
        assert spec["timeout"] == 7000
