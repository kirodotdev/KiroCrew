"""Agent Plugin servers as an MCP source of the default agent spec.

These drive the real rebuild (``install_agent``) through the shared merge harness,
with a powers home and a data home of their own, so the plugin source runs end to
end: discovery, the owner's record, the projection and the stale reconcile.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from agent_plugins_helpers import install, mcp
from mcp_merge_helpers import bundled_defaults, run_install_mcp_merge

from kiro_crew.agent_materialization import mcp_sources
from kiro_crew.agent_plugins import consent
from kiro_crew.agent_plugins.launch import LAUNCH_MODULE

SERVERS = {
    "local": {"type": "stdio", "command": "./bin/run", "args": ["${PLUGIN_ROOT}"]},
    "remote": {"type": "streamable-http", "url": "https://example.com/mcp"},
    "events": {"type": "sse", "url": "https://example.com/sse", "headers": {"X-A": "b"}},
}
KEYS = {"plugin-demo:local", "plugin-demo:remote", "plugin-demo:events"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    powers = tmp_path / "powers"
    crew = tmp_path / "crew"
    crew.mkdir()
    monkeypatch.setenv("KIRO_POWERS_HOME", str(powers))
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    root = install(powers, "demo", **{"mcp.json": mcp(SERVERS), "bin__run": "#!/bin/sh\n"})
    return tmp_path, bundled_defaults(tmp_path), root


def _rebuild(tmp_path: Path, cfg_dir: Path, kiro_servers: dict | None = None) -> dict:
    return run_install_mcp_merge(tmp_path, cfg_dir, cc_servers={}, kiro_servers=kiro_servers or {})


def _plugin_keys(spec: dict) -> set[str]:
    return {k for k in spec.get("mcpServers", {}) if k.startswith("plugin-")}


def test_an_off_plugin_adds_nothing(env):
    tmp_path, cfg_dir, _root = env
    spec = _rebuild(tmp_path, cfg_dir)
    assert _plugin_keys(spec) == set()
    assert not [t for t in spec.get("tools", []) if "plugin-" in t]
    # A rebuild over an existing spec re-pins both include flags off.
    assert _rebuild(tmp_path, cfg_dir)["includePowers"] is False


def test_an_on_plugin_is_keyed_projected_and_exposed(env):
    tmp_path, cfg_dir, root = env
    consent.set_enabled("demo", True)
    spec = _rebuild(tmp_path, cfg_dir)
    servers = spec["mcpServers"]
    assert _plugin_keys(spec) == KEYS
    local = servers["plugin-demo:local"]
    assert local["command"] == sys.executable
    assert local["args"][-4:] == ["-m", LAUNCH_MODULE, "demo", "local"]
    assert "env" not in local and "cwd" not in local
    assert servers["plugin-demo:remote"] == {"url": "https://example.com/mcp"}
    assert servers["plugin-demo:events"] == {
        "url": "https://example.com/sse",
        "type": "sse",
        "headers": {"X-A": "b"},
    }
    assert {f"@{k}" for k in KEYS} <= set(spec["tools"])


def test_editing_an_on_plugin_keeps_its_servers(env):
    tmp_path, cfg_dir, root = env
    consent.set_enabled("demo", True)
    assert _plugin_keys(_rebuild(tmp_path, cfg_dir)) == KEYS
    (root / "bin" / "run").write_text("#!/bin/sh\necho edited\n", encoding="utf-8")
    spec = _rebuild(tmp_path, cfg_dir)
    assert _plugin_keys(spec) == KEYS
    assert {f"@{k}" for k in KEYS} <= set(spec["tools"])


def test_turning_a_plugin_off_drops_its_servers(env):
    tmp_path, cfg_dir, root = env
    consent.set_enabled("demo", True)
    assert _plugin_keys(_rebuild(tmp_path, cfg_dir)) == KEYS
    consent.set_enabled("demo", False)
    assert _plugin_keys(_rebuild(tmp_path, cfg_dir)) == set()


def test_a_plugin_entry_outranks_a_shared_global_leftover(env):
    """Position pin: the plugin source runs before the Kiro global ``mcp.json``."""
    tmp_path, cfg_dir, root = env
    consent.set_enabled("demo", True)
    shared = {"plugin-demo:remote": {"url": "https://attacker.example/mcp"}}
    spec = _rebuild(tmp_path, cfg_dir, kiro_servers=shared)
    assert spec["mcpServers"]["plugin-demo:remote"] == {"url": "https://example.com/mcp"}


def test_an_app_entry_outranks_a_plugin_entry(env):
    """Position pin: the plugin source runs after the app source."""
    tmp_path, cfg_dir, root = env
    consent.set_enabled("demo", True)
    app = {"plugin-demo:remote": {"url": "https://app.example/mcp"}}
    with patch.object(mcp_sources, "_collect_app_mcp_servers", lambda **_: dict(app)):
        config: dict = {}
        mcp_sources.merge_mcp_sources(config)
    assert config["mcpServers"]["plugin-demo:remote"] == {"url": "https://app.example/mcp"}
    assert config["mcpServers"]["plugin-demo:local"]["args"][-2:] == ["demo", "local"]


def test_the_source_never_raises(env, monkeypatch):
    import kiro_crew.agent_plugins as ap

    def boom(*_a, **_k):
        raise RuntimeError("broken tree")

    monkeypatch.setattr(ap, "discover_plugins", boom)
    assert mcp_sources._collect_plugin_mcp_servers() == {}


def test_server_names_are_made_ref_safe():
    assert mcp_sources.plugin_mcp_key("demo", "a/b c") == "plugin-demo:a-b-c"
    assert mcp_sources._PLUGIN_KEY_RE.match(mcp_sources.plugin_mcp_key("demo", "a/b c"))


def test_the_written_spec_has_no_plugin_key_in_its_json_when_off(env):
    tmp_path, cfg_dir, _root = env
    text = json.dumps(_rebuild(tmp_path, cfg_dir))
    assert "plugin-demo" not in text
