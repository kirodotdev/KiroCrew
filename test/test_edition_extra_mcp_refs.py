"""An edition-supplied MCP server reaches ``tools`` once, however late it arrives.

``McpToolingProvider.extra_mcp_servers()`` can start returning a server after the
first install (its program is installed later, or a newer edition adds one). The
rebuild seeds the entry into ``mcpServers``; these tests pin that its ``@name``
ref follows once, and that a ref the user then removes stays removed. The last tests
pin that a same-named Kiro-global or store entry cannot replace or drop the
edition's args/env, that a spec that already lost the env gets it back, that
the store's env reaches the specs derived from the template too, that a
same-named entry naming another server is recorded as not used, and that the
placeholder the MCP page writes for the server never grants it in ``allowedTools``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import agent, mcp_dropped_servers
from kiro_crew.agent_files import (
    AGENT_FILENAME,
    MATE_WORKER_AGENT_FILENAME,
    RESEARCH_AGENT_FILENAME,
    TEAM_LEAD_AGENT_FILENAME,
)
from kiro_crew.agent_materialization import guide_platform, mcp_aliases
from kiro_crew.dashboard.side_readonly_spec import _home_relative
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.mcp_utils import mcp_server_alias

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


def _own_program(tmp_path: Path) -> str:
    """A runnable program of the user's, other than the edition's."""
    own = tmp_path / "own-bin" / "own-edition-late"
    own.parent.mkdir(exist_ok=True)
    own.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    own.chmod(0o755)
    return str(own)


def _write_servers(path: Path, servers: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_an_own_entry_naming_another_program_is_recorded_as_unused(
    rebuildable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edition = {"command": agent._KIROCREW_BIN, "args": _EDITION_ARGS}
    _supply(monkeypatch, {EXTRA: edition})
    own = _own_program(tmp_path)
    kiro_global = tmp_path / "kiro-global-mcp.json"
    _write_servers(kiro_global, {EXTRA: {"command": own, "args": ["--all-tools"]}})

    agent.rebuild_agent_config()
    entry = _spec(rebuildable)["mcpServers"][EXTRA]
    # The precedence is unchanged: the edition's invocation runs.
    assert entry["command"] == agent._KIROCREW_BIN
    assert entry["args"] == _EDITION_ARGS
    assert mcp_dropped_servers.load_unused_own() == {
        EXTRA: {"file": _home_relative(kiro_global), "command": own}
    }

    # Renamed, it runs beside the edition's server and the record clears.
    _write_servers(kiro_global, {"own-late": {"command": own}})
    agent.rebuild_agent_config()
    assert _spec(rebuildable)["mcpServers"]["own-late"]["command"] == own
    assert mcp_dropped_servers.load_unused_own() == {}


def test_an_own_entry_under_a_slash_name_is_read_at_its_mount(
    rebuildable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``/`` name runs under its alias, so the comparison reads the entry there."""
    name = "ns/edition-late"
    _supply(monkeypatch, {name: {"command": agent._KIROCREW_BIN, "args": _EDITION_ARGS}})
    own = _own_program(tmp_path)
    kiro_global = tmp_path / "kiro-global-mcp.json"
    _write_servers(kiro_global, {name: {"command": own}})

    agent.rebuild_agent_config()
    servers = _spec(rebuildable)["mcpServers"]
    assert name not in servers
    assert servers[mcp_server_alias(name)]["command"] == agent._KIROCREW_BIN
    assert mcp_dropped_servers.load_unused_own() == {
        name: {"file": _home_relative(kiro_global), "command": own}
    }


def test_the_store_entry_is_the_one_named_when_both_files_declare_the_name(
    rebuildable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _supply(monkeypatch, {EXTRA: {"command": agent._KIROCREW_BIN, "args": _EDITION_ARGS}})
    _write_servers(tmp_path / "kiro-global-mcp.json", {EXTRA: {"command": "global-program"}})
    store = agent._user_dir() / "mcp.json"
    _write_servers(store, {EXTRA: {"url": "https://mcp.example.test/mcp"}})

    agent.rebuild_agent_config()
    assert mcp_dropped_servers.load_unused_own() == {
        # A URL is stored cut to its scheme and host: any other part can carry a token.
        EXTRA: {"file": _home_relative(store), "command": "https://mcp.example.test"}
    }


@pytest.mark.parametrize("source", ["kiro-global-url", "provider-global-command"])
def test_an_own_entry_in_either_global_is_recorded_with_its_file(
    rebuildable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    _supply(monkeypatch, {EXTRA: {"command": agent._KIROCREW_BIN, "args": _EDITION_ARGS}})
    if source == "kiro-global-url":
        file, declared = tmp_path / "kiro-global-mcp.json", "https://mcp.example.test/mcp"
        _write_servers(file, {EXTRA: {"url": declared}})
    else:
        file, declared = tmp_path / "provider-global.json", _own_program(tmp_path)
        _write_servers(file, {EXTRA: {"command": declared}})
        monkeypatch.setattr(agent, "_extra_mcp_scope_globals", lambda: [file])

    agent.rebuild_agent_config()
    entry = _spec(rebuildable)["mcpServers"][EXTRA]
    assert entry["command"] == agent._KIROCREW_BIN
    assert "url" not in entry
    shown = "https://mcp.example.test" if source == "kiro-global-url" else declared
    assert mcp_dropped_servers.load_unused_own() == {
        EXTRA: {"file": _home_relative(file), "command": shown}
    }


@pytest.mark.parametrize(
    "own_entry",
    [
        pytest.param({"args": ["--include-tools", "OnlyRead"]}, id="same-program-other-args"),
        pytest.param({"disabledTools": ["write"]}, id="no-declaration"),
    ],
)
def test_an_own_entry_for_the_same_server_is_not_recorded(
    rebuildable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_entry: dict
) -> None:
    _supply(monkeypatch, {EXTRA: {"command": agent._KIROCREW_BIN, "args": _EDITION_ARGS}})
    declared = {"command": "kirocrew", **own_entry} if "args" in own_entry else own_entry
    _write_servers(tmp_path / "kiro-global-mcp.json", {EXTRA: declared})

    agent.rebuild_agent_config()
    assert mcp_dropped_servers.load_unused_own() == {}


def test_an_own_entry_the_rebuild_falls_back_to_is_not_recorded(
    rebuildable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The edition's program is not on this host, so resolution runs the user's.
    missing = str(tmp_path / "absent" / "edition-late")
    _supply(monkeypatch, {EXTRA: {"command": missing, "args": _EDITION_ARGS}})
    own = _own_program(tmp_path)
    _write_servers(tmp_path / "kiro-global-mcp.json", {EXTRA: {"command": own}})

    agent.rebuild_agent_config()
    assert _spec(rebuildable)["mcpServers"][EXTRA]["command"] == own
    assert mcp_dropped_servers.load_unused_own() == {}


@pytest.mark.parametrize(
    ("entry", "launches", "installs"),
    [
        pytest.param({"command": "x"}, True, True, id="command"),
        pytest.param({"url": "https://x.test/mcp"}, True, True, id="url"),
        # The admin's catalog launches it: an install, with no launch of its own.
        pytest.param({"type": "registry"}, False, True, id="registry-pointer"),
        pytest.param({}, False, False, id="empty"),
        pytest.param({"disabledTools": ["x"]}, False, False, id="tool-off"),
        pytest.param({"disabled": True}, False, False, id="muted"),
        pytest.param({"command": ""}, False, False, id="blank-command"),
        pytest.param("x", False, False, id="not-a-mapping"),
    ],
)
def test_only_an_entry_that_launches_or_points_at_the_catalog_installs_a_server(
    entry: Any, launches: bool, installs: bool
) -> None:
    from kiro_crew.mcp_cleanup import mcp_entry_installs_server, mcp_entry_names_launch

    assert mcp_entry_names_launch(entry) is launches
    assert mcp_entry_installs_server(entry) is installs


def _write_global(tmp_path: Path, servers: dict[str, Any]) -> None:
    (tmp_path / "kiro-global-mcp.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )


def _edition_grants(agents_dir: Path) -> list[str]:
    """The default spec's ``allowedTools`` entries naming the edition server."""
    return [
        ref
        for ref in _spec(agents_dir)["allowedTools"]
        if ref == EXTRA_REF or str(ref).startswith(f"{EXTRA_REF}/")
    ]


@pytest.mark.parametrize(
    "placeholder",
    [
        pytest.param({"disabledTools": ["write_note"]}, id="one-tool-off"),
        pytest.param({}, id="empty"),
    ],
)
def test_a_page_placeholder_never_grants_the_edition_server(
    rebuildable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, placeholder: dict
) -> None:
    """Turning one tool off on the MCP page writes a placeholder to the Kiro global.

    It names no launch, so it is not the user installing the server: the server
    stays mounted, and no tool of it is auto-approved by that entry.
    """
    _supply(monkeypatch, {EXTRA: _extra_spec()})
    agent.rebuild_agent_config()
    _write_global(tmp_path, {EXTRA: placeholder})

    for _ in range(2):
        agent.rebuild_agent_config()
        assert EXTRA_REF in _spec(rebuildable)["tools"]
        assert _edition_grants(rebuildable) == []


def test_a_grant_the_spec_holds_beside_a_placeholder_stays_and_a_removed_one_stays_gone(
    rebuildable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grant in ``allowedTools`` is the user's (or another tool's), whoever wrote it.

    The rebuild cannot tell it from one it added, so it keeps it in both
    spellings; once the user deletes it, no rebuild writes it back.
    """
    _supply(monkeypatch, {EXTRA: _extra_spec()})
    _write_global(tmp_path, {EXTRA: {"disabledTools": ["write_note"]}})
    agent.rebuild_agent_config()
    spec = _spec(rebuildable)
    spec["allowedTools"] += [EXTRA_REF, f"{EXTRA_REF}/read_note"]
    (rebuildable / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")

    agent.rebuild_agent_config()
    assert _edition_grants(rebuildable) == [EXTRA_REF, f"{EXTRA_REF}/read_note"]

    spec = _spec(rebuildable)
    spec["allowedTools"].remove(EXTRA_REF)
    (rebuildable / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent.rebuild_agent_config()
    assert EXTRA_REF in _spec(rebuildable)["tools"]
    assert _edition_grants(rebuildable) == [f"{EXTRA_REF}/read_note"]


def test_a_real_kiro_global_entry_keeps_the_whole_server_grant(
    rebuildable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry that launches the server is the user installing it, so it is granted."""
    _supply(monkeypatch, {EXTRA: _extra_spec()})
    _write_global(
        tmp_path, {EXTRA: {"command": agent._KIROCREW_BIN, "disabledTools": ["write_note"]}}
    )

    agent.rebuild_agent_config()
    spec = _spec(rebuildable)
    assert _edition_grants(rebuildable) == [EXTRA_REF]
    assert spec["mcpServers"][EXTRA]["disabledTools"] == ["write_note"]


def test_a_placeholder_beside_an_installing_store_entry_keeps_its_grant(
    rebuildable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server the store installs keeps its grant when the Kiro global holds a placeholder."""
    _supply(monkeypatch, {})
    _write_global(tmp_path, {"own-notes": {"disabledTools": ["write_note"]}})
    store = agent._user_dir() / "mcp.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps({"mcpServers": {"own-notes": {"command": agent._KIROCREW_BIN}}}),
        encoding="utf-8",
    )

    for _ in range(2):
        agent.rebuild_agent_config()
        spec = _spec(rebuildable)
        assert "@own-notes" in spec["tools"]
        assert "@own-notes" in spec["allowedTools"]


def test_a_registry_pointer_keeps_the_whole_server_grant(
    rebuildable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry pointer is the user installing the server from the admin's catalog."""
    _supply(monkeypatch, {})
    _write_global(tmp_path, {"catalog-notes": {"type": "registry"}})

    agent.rebuild_agent_config()
    spec = _spec(rebuildable)
    assert "@catalog-notes" in spec["tools"]
    assert "@catalog-notes" in spec["allowedTools"]
