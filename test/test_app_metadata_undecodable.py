"""App metadata files whose bytes are not UTF-8 read like files that do not parse.

Each reader below already treats bad JSON as "unreadable" and carries on with a
safe answer. A file that does not decode as UTF-8 is the same unusable file, so
each gives the same answer instead of raising ``UnicodeDecodeError`` into app
registration, app MCP policy, or the App Store listing:

* the stale-resource prune keeps the app's agent configs (it cannot tell the
  agent was removed);
* agent registration skips the agent;
* an app's MCP policy reads as none;
* the manifest cache, the external registry cache and the bundled registry
  read as absent;
* an app agent config on disk reads as absent, and Kiro Crew's own agent
  config (``kirocrew.json``) reads as empty for a read-only caller while a
  read-modify-write caller still refuses it;
* the dev-mode sentinel and grant record read as empty (no grants).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import pytest

from kiro_crew.apps import bridges as bridges_mod
from kiro_crew.apps import bridges_agents
from kiro_crew.apps import execution as execution_mod
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.registry_pipeline import caches, sources

# Not UTF-8: 0xff never starts a UTF-8 sequence.
_UNDECODABLE = b'\xff\xfe{"name": "my-agent"}'
_AGENT_FILE = "my-agent.json"

T = TypeVar("T")


def _call(fn: Callable[[], T]) -> T:
    """Run *fn*, failing the test (not erroring it) when it raises on the bytes."""
    try:
        return fn()
    except UnicodeDecodeError as exc:
        pytest.fail(f"a file that is not UTF-8 raised {exc!r}")


def _make_app_source(tmp_path: Path, name: str = "test-app") -> Path:
    src = tmp_path / "source" / name
    (src / "agents").mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Test App",
        "description": "A test app",
        "author": "tester",
        "agents": [f"agents/{_AGENT_FILE}"],
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    (src / "agents" / _AGENT_FILE).write_text(
        json.dumps({"name": "my-agent", "model": "auto"}), encoding="utf-8"
    )
    return src


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    monkeypatch.setattr(execution_mod, "third_party_execution_allowed", lambda: True)
    mcp_path = tmp_path / "mcp.json"
    monkeypatch.setattr(bridges_mod, "_mcp_json_path", lambda: mcp_path)
    return {"home": home, "kiro_agents": kiro_agents}


@pytest.fixture()
def installed(tmp_path, app_env):
    assert install_app(_make_app_source(tmp_path)).ok
    app_root = app_env["home"] / "apps" / "test-app"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)
    return app_root, manifest


def test_the_prune_keeps_agent_configs_when_an_agent_file_is_not_utf8(installed, app_env):
    app_root, manifest = installed
    assert bridges_mod._register_agents("test-app", manifest, app_root) == ["test-app/my-agent"]
    config = app_env["kiro_agents"] / "test-app--my-agent.json"
    assert config.is_file()
    (app_root / "agents" / _AGENT_FILE).write_bytes(_UNDECODABLE)

    _call(lambda: bridges_mod._prune_stale_app_resources("test-app", manifest, app_root))

    assert config.is_file()


def test_registration_skips_an_agent_file_that_is_not_utf8(installed, app_env):
    app_root, manifest = installed
    (app_root / "agents" / _AGENT_FILE).write_bytes(_UNDECODABLE)

    assert _call(lambda: bridges_mod._register_agents("test-app", manifest, app_root)) == []
    assert not (app_env["kiro_agents"] / "test-app--my-agent.json").exists()


def test_an_mcp_policy_that_is_not_utf8_reads_as_none(installed):
    app_root, _manifest = installed
    policy = app_root / "data" / bridges_agents.AGENT_MCP_POLICY_FILE
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_bytes(_UNDECODABLE)

    assert _call(lambda: bridges_agents._agent_mcp_policy("test-app")) == {}


def test_a_manifest_cache_that_is_not_utf8_reads_as_absent(tmp_path, monkeypatch):
    cached = tmp_path / "manifest.json"
    cached.write_bytes(_UNDECODABLE)
    monkeypatch.setattr(caches, "_manifest_cache_path", lambda _entry: cached)

    assert _call(lambda: caches._read_manifest_cache({"name": "my-app"})) is None


def test_an_external_registry_cache_that_is_not_utf8_reads_as_absent(tmp_path, monkeypatch):
    cached = tmp_path / "registry.json"
    cached.write_bytes(_UNDECODABLE)
    monkeypatch.setattr(caches, "_external_registry_cache_path", lambda _name: cached)
    monkeypatch.setattr(caches, "_remove_legacy_credential_registry_cache", lambda _name: None)

    assert _call(lambda: caches._read_external_registry_cache("corp")) is None


def test_a_bundled_registry_that_is_not_utf8_reads_as_empty(tmp_path, monkeypatch):
    registry = tmp_path / "app-registry.json"
    registry.write_bytes(_UNDECODABLE)
    monkeypatch.setattr(sources, "_REGISTRY_FILE", registry)

    rows = _call(sources._load_registry_file)

    assert isinstance(rows, list)


def test_an_agent_config_that_is_not_utf8_reads_as_absent(tmp_path):
    config = tmp_path / "test-app--my-agent.json"
    config.write_bytes(_UNDECODABLE)

    assert _call(lambda: bridges_agents._read_agent_config(config)) is None


def test_kirocrew_agent_config_that_is_not_utf8_reads_as_empty(tmp_path, monkeypatch):
    from kiro_crew.apps import bridges_mcp_config

    config = tmp_path / "kirocrew.json"
    config.write_bytes(_UNDECODABLE)
    monkeypatch.setattr(bridges_mcp_config, "_mcp_json_path", lambda: config)

    assert _call(bridges_mcp_config._read_mcp_json_unlocked) == {}


def test_a_strict_read_of_kirocrew_agent_config_still_refuses_to_guess(tmp_path, monkeypatch):
    # A read-modify-write caller must not take the file for empty and write
    # over the whole agent config, so the strict read raises as for bad JSON.
    from kiro_crew.apps import bridges_mcp_config

    config = tmp_path / "kirocrew.json"
    config.write_bytes(_UNDECODABLE)
    monkeypatch.setattr(bridges_mcp_config, "_mcp_json_path", lambda: config)

    with pytest.raises(ValueError):
        bridges_mcp_config._read_mcp_json_unlocked(strict=True)
    assert config.read_bytes() == _UNDECODABLE


def test_dev_mode_files_that_are_not_utf8_read_as_empty(app_env):
    from kiro_crew.apps import dev_mode

    dev_mode._sentinel_path().parent.mkdir(parents=True, exist_ok=True)
    dev_mode._sentinel_path().write_bytes(_UNDECODABLE)
    dev_mode._grants_path().write_bytes(_UNDECODABLE)

    assert _call(dev_mode._read_dev_sentinel) == set()
    # Fails closed: an unreadable grant record grants nothing.
    assert _call(dev_mode._read_dev_grants) == {}
