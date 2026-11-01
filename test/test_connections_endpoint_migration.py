"""The one-time move of a Connections entry off a retired registry endpoint.

A profile may hold Atlassian's retired ``v1/mcp/authv2``; the registry installs
``v2/mcp``. These pin what a start does to a profile that connected before the
change: the card's entry and its owner record point at the new URL, no sign-in
file is deleted (Disconnect removes the v1 pair, see
``test_connections_disconnect_retired.py``), every other provider is left alone,
and a second start changes nothing.
"""

from __future__ import annotations

import contextlib
import json
import pathlib

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import mcp_discovery, mcp_grant
from kiro_crew.config import paths as connections_paths
from kiro_crew.config.loader import config_path
from kiro_crew.connections import endpoint_migration
from kiro_crew.connections.registry import get_provider
from kiro_crew.dashboard.handlers import mcp as mcp_handlers
from kiro_crew.dashboard.handlers import mcp_config_files

_V1 = "https://mcp.atlassian.com/v1/mcp/authv2"
_V2 = "https://mcp.atlassian.com/v2/mcp"
_NOTION = "https://mcp.notion.com/mcp"


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    """Every file the migration reads or writes, under ``tmp_path``."""
    mcp_json = tmp_path / "mcp.json"
    agents = tmp_path / "agents"
    agents.mkdir()
    cache = mcp_grant.kiro_oauth_cache_dir(home=tmp_path)
    cache.mkdir(parents=True)
    monkeypatch.setattr(mcp_config_files, "_KIROCREW_MCP_JSON", mcp_json)
    monkeypatch.setattr(mcp_handlers, "_get_mcp_lock_sync", contextlib.nullcontext)
    monkeypatch.setattr(mcp_discovery, "_MCP_SOURCES", ((mcp_json, "kirocrew"),))
    monkeypatch.setattr(mcp_discovery, "_extra_scope_sources", list)
    monkeypatch.setattr(mcp_handlers, "_extra_mcp_scopes", list)
    monkeypatch.setattr(connections_paths, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(mcp_grant, "kiro_oauth_cache_home", lambda: tmp_path)
    rebuilds: list[str] = []

    def _rebuild(**_kwargs: object) -> tuple[pathlib.Path, bool]:
        rebuilds.append("rebuild")
        return agents / "kirocrew.json", True

    monkeypatch.setattr(agent_mod, "rebuild_agent_config_reporting", _rebuild)
    monkeypatch.setattr(endpoint_migration, "_audit", lambda _result: None)
    return {"mcp_json": mcp_json, "agents": agents, "cache": cache, "rebuilds": rebuilds}  # type: ignore[dict-item]


def _write_mcp(home: dict, servers: dict) -> None:
    home["mcp_json"].write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _servers(home: dict) -> dict:
    return json.loads(home["mcp_json"].read_text(encoding="utf-8"))["mcpServers"]


def _grant(home: dict, url: str) -> tuple[pathlib.Path, pathlib.Path]:
    token, registration = mcp_grant.grant_artifact_paths(url, cache_dir=home["cache"])
    token.write_text("{}", encoding="utf-8")
    registration.write_text("{}", encoding="utf-8")
    return token, registration


def _write_owner_record(servers: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"connections": {"owner_mcp_servers": servers}}), encoding="utf-8")


def _start() -> list:
    """What one gateway start runs."""
    return endpoint_migration.migrate_retired_endpoints()


def _owner_record() -> dict:
    return json.loads(config_path().read_text(encoding="utf-8"))["connections"]["owner_mcp_servers"]


def test_the_registry_no_longer_installs_the_retired_endpoint() -> None:
    provider = get_provider("atlassian")
    assert provider is not None
    assert provider["mcp_url"] == _V2
    assert provider["mcp_url"] not in endpoint_migration.RETIRED_ENDPOINTS["atlassian"]


def test_a_v1_profile_is_moved_to_v2(home: dict) -> None:
    _write_mcp(
        home,
        {
            "atlassian": {"url": _V1, "disabled": True, "autoApprove": ["search"]},
            "notion": {"url": _NOTION},
        },
    )
    _write_owner_record({"atlassian": {"url": _V1}, "notion": {"url": _NOTION}})
    v1 = _grant(home, _V1)
    v2 = _grant(home, _V2)
    notion = _grant(home, _NOTION)

    (moved,) = _start()

    servers = _servers(home)
    # The URL moves; everything else on the entry stays, disabled included.
    assert servers["atlassian"] == {"url": _V2, "disabled": True, "autoApprove": ["search"]}
    assert servers["notion"] == {"url": _NOTION}
    assert _owner_record() == {"atlassian": {"url": _V2}, "notion": {"url": _NOTION}}
    assert all(p.exists() for p in v1), "startup deleted the v1 sign-in pair"
    assert all(p.exists() for p in v2), "the v2 sign-in pair was touched"
    assert all(p.exists() for p in notion), "another provider's sign-in was touched"
    assert home["rebuilds"] == ["rebuild"]
    assert moved.entry_migrated and moved.owner_record_migrated


def test_a_second_run_is_a_no_op(home: dict) -> None:
    _write_mcp(home, {"atlassian": {"url": _V1}})
    _write_owner_record({"atlassian": {"url": _V1}})
    _grant(home, _V1)
    _start()
    mcp_before = home["mcp_json"].read_bytes()
    config_before = config_path().read_bytes()
    cache_before = sorted(p.name for p in home["cache"].iterdir())

    (result,) = _start()

    assert not result.changed
    assert home["mcp_json"].read_bytes() == mcp_before
    assert config_path().read_bytes() == config_before
    assert sorted(p.name for p in home["cache"].iterdir()) == cache_before
    assert home["rebuilds"] == ["rebuild"], "the second run rebuilt the agent spec again"


def test_a_closed_projects_v1_sign_in_survives_startup(home: dict, tmp_path: pathlib.Path) -> None:
    """A project with no open chat is invisible to any census at startup.

    Its own agent spec still signs in with the v1 pair, so a start must not
    delete that pair even after it moves the card's entry.
    """
    _write_mcp(home, {"atlassian": {"url": _V1}})
    project = tmp_path / "closed-checkout"
    (project / ".kiro" / "agents").mkdir(parents=True)
    (project / ".kiro" / "agents" / "team.json").write_text(
        json.dumps({"mcpServers": {"jira": {"url": _V1}}}), encoding="utf-8"
    )
    v1 = _grant(home, _V1)

    (moved,) = _start()

    assert moved.entry_migrated
    assert all(p.exists() for p in v1), "startup deleted a closed project's v1 sign-in"


def test_a_v2_profile_is_left_alone(home: dict) -> None:
    _write_mcp(home, {"atlassian": {"url": _V2}})
    v2 = _grant(home, _V2)
    before = home["mcp_json"].read_bytes()

    (result,) = _start()

    assert not result.changed
    assert home["mcp_json"].read_bytes() == before
    assert all(p.exists() for p in v2)
    assert home["rebuilds"] == []


def test_a_same_named_entry_at_another_url_is_not_rewritten(home: dict) -> None:
    """Only the retired endpoint moves; a user's own server under the name stays."""
    _write_mcp(home, {"atlassian": {"url": "https://jira.example.com/mcp"}})
    before = home["mcp_json"].read_bytes()

    (moved,) = _start()

    assert not moved.entry_migrated
    assert home["mcp_json"].read_bytes() == before


def test_a_v1_spelling_with_its_own_sign_in_key_is_not_rewritten(home: dict) -> None:
    """A trailing slash keeps a separate pair that Disconnect would not revoke.

    Moving that entry to v2 would orphan its pair behind it, so it stays on its URL.
    """
    _write_mcp(home, {"atlassian": {"url": _V1 + "/"}})
    before = home["mcp_json"].read_bytes()

    (moved,) = _start()

    assert not moved.entry_migrated
    assert home["mcp_json"].read_bytes() == before


def test_a_malformed_mcp_json_is_not_rewritten(home: dict) -> None:
    home["mcp_json"].write_text("{broken", encoding="utf-8")

    (moved,) = _start()

    assert not moved.entry_migrated
    assert home["mcp_json"].read_text(encoding="utf-8") == "{broken"


def test_a_failure_is_logged_and_does_not_raise(
    home: dict, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(endpoint_migration, "_migrate_entry", _boom)

    assert _start() == []
    assert "endpoint migration failed for atlassian" in caplog.text
