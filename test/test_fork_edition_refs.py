"""A fork of ``kirocrew.json`` gains an edition server's ref once, like the default spec.

A crew's private copy of the default spec, made before an edition supplied a
server, mounts that server on every fork refresh, and its sessions can call the
server's tools only once ``tools`` names it. These
tests pin that such a fork gains the ``@name`` ref the default spec got, once; that
a second pass changes nothing; and that a ref the user then removes, a fork made
after the default spec already had the server, a fork of a custom template and a
hand-written custom agent all stay as they are.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import agent, agent_state
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.agent_materialization import guide_platform
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

EXTRA = "edition-late"
EXTRA_REF = f"@{EXTRA}"
FORK = "my-crew"


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


def _supply(monkeypatch: pytest.MonkeyPatch, servers: dict[str, dict[str, Any]]) -> None:
    monkeypatch.setattr(
        agent, "_extra_mcp_servers", lambda: {k: dict(v) for k, v in servers.items()}
    )


def _edition(monkeypatch: pytest.MonkeyPatch) -> None:
    _supply(monkeypatch, {EXTRA: {"command": agent._KIROCREW_BIN, "args": ["mcp-late"]}})


def _read(agents_dir: Path, name: str) -> dict[str, Any]:
    return json.loads((agents_dir / f"{name}.json").read_text(encoding="utf-8"))


def _write(agents_dir: Path, name: str, spec: dict[str, Any]) -> None:
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _bind(name: str) -> None:
    cfg = KiroCrewConfig()
    cfg.agents = {name: KiroCrewAgentConfig(kiro_agent=name)}
    cfg.save()


def _fork_default(agents_dir: Path, *, forked_from: str = "kirocrew") -> None:
    """Copy the default spec as *FORK*'s private copy, as the dashboard fork does."""
    spec = _read(agents_dir, "kirocrew")
    spec["name"] = FORK
    _write(agents_dir, FORK, spec)
    agent_state.prune(FORK)
    agent_state.set_fork_info(FORK, forked_from=forked_from, private_to=FORK)
    guide_platform.record_fork_edition_baseline(FORK)
    _bind(FORK)


def test_a_fork_made_before_the_edition_server_gains_its_ref_once(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _supply(monkeypatch, {})
    agent.rebuild_agent_config()
    _fork_default(rebuildable)
    assert EXTRA_REF not in _read(rebuildable, FORK)["tools"]

    _edition(monkeypatch)
    agent.rebuild_agent_config()
    assert EXTRA_REF in _read(rebuildable, "kirocrew")["tools"]
    fork = _read(rebuildable, FORK)
    assert EXTRA in fork["mcpServers"]
    assert EXTRA_REF in fork["tools"]
    # A ref, never a pre-approval: its calls still reach the PreToolUse gate.
    assert EXTRA_REF not in fork.get("allowedTools", [])

    before = (rebuildable / f"{FORK}.json").read_bytes()
    agent.rebuild_agent_config()
    assert (rebuildable / f"{FORK}.json").read_bytes() == before


def test_a_ref_removed_from_the_fork_stays_removed(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _supply(monkeypatch, {})
    agent.rebuild_agent_config()
    _fork_default(rebuildable)
    _edition(monkeypatch)
    agent.rebuild_agent_config()
    fork = _read(rebuildable, FORK)
    tools_before = [t for t in fork["tools"] if t != EXTRA_REF]
    fork["tools"] = tools_before
    _write(rebuildable, FORK, fork)

    agent.rebuild_agent_config()
    fork = _read(rebuildable, FORK)
    assert EXTRA in fork["mcpServers"]
    assert fork["tools"] == tools_before


def test_a_fork_made_after_the_default_had_the_server_keeps_its_own_choice(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _edition(monkeypatch)
    agent.rebuild_agent_config()
    _fork_default(rebuildable)
    fork = _read(rebuildable, FORK)
    fork["tools"] = [t for t in fork["tools"] if t != EXTRA_REF]
    _write(rebuildable, FORK, fork)

    agent.rebuild_agent_config()
    assert EXTRA_REF not in _read(rebuildable, FORK)["tools"]


def test_a_ref_the_default_spec_lost_is_not_given_to_the_fork(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _supply(monkeypatch, {})
    agent.rebuild_agent_config()
    _fork_default(rebuildable)
    _edition(monkeypatch)
    agent.rebuild_agent_config(refresh_forks=False)
    # The user takes the ref off the default spec before the fork is refreshed.
    default = _read(rebuildable, "kirocrew")
    default["tools"].remove(EXTRA_REF)
    _write(rebuildable, "kirocrew", default)

    agent._refresh_forked_templates(gated_off=frozenset())
    assert EXTRA in _read(rebuildable, FORK)["mcpServers"]
    assert EXTRA_REF not in _read(rebuildable, FORK)["tools"]


def test_a_fork_of_a_custom_template_and_a_custom_agent_stay_as_written(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _supply(monkeypatch, {})
    agent.rebuild_agent_config()
    custom = {"name": "hand", "prompt": "Mine.", "tools": ["fs_read"], "mcpServers": {}}
    _write(rebuildable, "hand", custom)
    _write(rebuildable, FORK, {**custom, "name": FORK})
    agent_state.set_fork_info(FORK, forked_from="hand", private_to=FORK)
    _bind(FORK)

    _edition(monkeypatch)
    agent.rebuild_agent_config()
    assert _read(rebuildable, "hand") == custom
    assert EXTRA_REF not in _read(rebuildable, FORK)["tools"]


def test_a_fork_with_a_wildcard_or_no_tools_list_is_left_alone() -> None:
    for tools in ("*", None):
        config: dict[str, Any] = {"mcpServers": {EXTRA: {}}}
        if tools is not None:
            config["tools"] = tools
        assert guide_platform.grant_fork_edition_refs_once(config, FORK, [EXTRA_REF]) == []
        assert config.get("tools") == tools


@pytest.mark.asyncio
async def test_the_dashboard_fork_records_the_servers_its_copy_already_reflects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers.agents import api_agent_fork

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", agents_dir)
    _write(agents_dir, "mytemplate", {"name": "mytemplate", "tools": ["fs_read"]})
    cfg = KiroCrewConfig()
    cfg.agents = {FORK: KiroCrewAgentConfig(kiro_agent="mytemplate")}
    cfg.save()
    guide_platform.mark_edition_refs_offered([EXTRA])

    request = MagicMock(spec=web.Request)
    request.method = "POST"
    request.match_info = {"name": "mytemplate"}
    request.app = {"state": MagicMock()}

    async def _json() -> dict[str, str]:
        return {"crew": FORK}

    request.json = _json
    resp = await api_agent_fork(request)

    assert resp.status == 200
    assert agent_state.get_edition_refs_offered(FORK) == frozenset({EXTRA})
