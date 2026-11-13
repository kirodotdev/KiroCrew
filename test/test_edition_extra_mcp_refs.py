"""An edition-supplied MCP server reaches ``tools`` once, however late it arrives.

``McpToolingProvider.extra_mcp_servers()`` can start returning a server after the
first install (its program is installed later, or a newer edition adds one). The
rebuild seeds the entry into ``mcpServers``; these tests pin that its ``@name``
ref follows once, and that a ref the user then removes stays removed. The last test
pins that a same-named Kiro-global entry cannot replace the edition's args/env.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import agent
from kiro_crew.agent_files import AGENT_FILENAME
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
