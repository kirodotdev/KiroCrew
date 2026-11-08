"""``ctx.call_tool`` starts a server only under the launch rules every other path keeps.

``_resolve_mcp_server`` turns a server name into what ``McpToolClient`` spawns, and
``KeptMcpServers.call_tool`` is the one door both ``ScriptContext.call_tool`` and
``kirocrew cron preview`` go through. The rules pinned here are the ones the
session, gateway and KAS paths already apply: a server the user muted is not
launched, a registry or remote entry is not started as a local process, a tool the
user switched off is not called, and ``${VAR}`` in a server's ``env`` is resolved
the way kiro-cli resolves it.

Every case writes Crew's agent spec and the two core MCP config scopes under this
test's own ``tmp_path``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.cron_script as cron_script
from kiro_crew.cron_script import KeptMcpServers, McpToolError, _resolve_mcp_server


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Crew's agent spec and the crew-store and Kiro-global scopes, all under tmp."""
    from kiro_crew import mcp_discovery

    agents = tmp_path / "agents"
    store = tmp_path / "crew" / "mcp.json"
    kiro_global = tmp_path / "kiro" / "settings" / "mcp.json"
    for directory in (agents, store.parent, kiro_global.parent):
        directory.mkdir(parents=True)
    monkeypatch.setattr(cron_script, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(
        mcp_discovery, "_MCP_SOURCES", ((store, "kirocrew"), (kiro_global, "kiroGlobal"))
    )
    monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", None)
    # The file a chat session's switched-off tools are read from.
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", kiro_global)
    _resolve_mcp_server.cache_clear()
    yield {"spec": agents / "kirocrew.json", "store": store, "kiro_global": kiro_global}
    _resolve_mcp_server.cache_clear()


def _write_servers(path, servers: dict[str, Any]) -> None:
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


@pytest.fixture
def started(monkeypatch):
    """The server names a ``McpToolClient`` was constructed for, in order."""
    names: list[str] = []

    class _Client:
        def __init__(self, server_name: str, session_key: str = "") -> None:
            names.append(server_name)

        def call_tool(self, name: str, arguments: dict) -> str:
            return f"ran {name}"

        def is_running(self) -> bool:
            return True

        def close(self) -> None:
            pass

    monkeypatch.setattr(cron_script, "McpToolClient", _Client)
    return names


class TestMutedServer:
    @pytest.mark.parametrize("flag", [True, None, "false"], ids=["true", "null", "string"])
    def test_a_muted_entry_is_refused_not_launched(self, home, flag):
        _write_servers(
            home["spec"], {"pippin-mcp": {"command": "node", "args": ["s.js"], "disabled": flag}}
        )

        with pytest.raises(RuntimeError, match="'pippin-mcp' is disabled"):
            _resolve_mcp_server("pippin-mcp")

    def test_a_mute_in_a_config_scope_refuses_an_unmuted_spec_entry(self, home):
        """The toggle writes the mute to a scope file; a spec the rebuild has not
        stamped still declares the server unmuted."""
        _write_servers(home["spec"], {"pippin-mcp": {"command": "node", "args": ["s.js"]}})
        _write_servers(home["kiro_global"], {"pippin-mcp": {"disabled": True}})

        with pytest.raises(RuntimeError, match="'pippin-mcp' is disabled"):
            _resolve_mcp_server("pippin-mcp")

    def test_an_enabled_entry_still_resolves(self, home):
        _write_servers(
            home["spec"], {"pippin-mcp": {"command": "node", "args": ["s.js"], "disabled": False}}
        )

        assert _resolve_mcp_server("pippin-mcp") == (("node", "s.js"), {})


class TestUnlaunchableEntry:
    @pytest.mark.parametrize(
        "entry",
        [
            {"url": "https://example.invalid/mcp"},
            {"url": "https://example.invalid/mcp", "command": "placeholder"},
        ],
        ids=["url-only", "url-with-command"],
    )
    def test_a_remote_entry_is_refused_by_name(self, home, entry):
        _write_servers(home["spec"], {"github": entry})

        with pytest.raises(RuntimeError, match="'github' is a remote"):
            _resolve_mcp_server("github")

    def test_a_registry_entry_is_refused(self, home):
        _write_servers(home["spec"], {"catalog": {"type": "registry", "command": "x"}})

        with pytest.raises(RuntimeError, match="'catalog' is governed by the admin registry"):
            _resolve_mcp_server("catalog")

    def test_an_entry_with_no_command_is_refused_by_name(self, home):
        _write_servers(home["spec"], {"half": {"args": ["s.js"]}})

        with pytest.raises(RuntimeError, match="'half' declares no command"):
            _resolve_mcp_server("half")

    def test_a_non_object_entry_is_not_found(self, home):
        _write_servers(home["spec"], {"bare": "node"})

        assert _resolve_mcp_server("bare") is None


def test_env_placeholders_resolve_as_kiro_cli_resolves_them(home, monkeypatch):
    """Both spellings resolve from the environment; a secret-named variable is never
    dereferenced, so its reference stays literal."""
    monkeypatch.setenv("CRON_T", "v")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-forward")
    _write_servers(
        home["spec"],
        {
            "srv": {
                "command": "node",
                "env": {"A": "${CRON_T}", "B": "${env:CRON_T}", "C": "${AWS_SECRET_ACCESS_KEY}"},
            }
        },
    )

    assert _resolve_mcp_server("srv") == (
        ("node",),
        {"A": "v", "B": "v", "C": "${AWS_SECRET_ACCESS_KEY}"},
    )


def test_a_granted_or_cron_denied_variable_is_never_dereferenced(home, monkeypatch):
    """A granted run's launcher puts the vault value in ``os.environ`` and names it in
    ``_GRANTED_ENV_KEYS``, which ``_clean_cron_env`` keeps from the server. A
    reference to it, or to a ``_CRON_ENV_DENY`` name, stays literal, so neither value
    reaches the server under a name of the entry's choosing, while an ordinary
    variable still resolves."""
    monkeypatch.setattr(cron_script, "_GRANTED_ENV_KEYS", {"GITHUB_TOKEN"})
    monkeypatch.setenv("GITHUB_TOKEN", "vault-value")
    monkeypatch.setenv("KIROCREW_INTERNAL_SECRET", "internal-value")
    monkeypatch.setenv("CRON_T", "v")
    _write_servers(
        home["spec"],
        {
            "srv": {
                "command": "node",
                "env": {
                    "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}",
                    "S": "${env:KIROCREW_INTERNAL_SECRET}",
                    "A": "${CRON_T}",
                },
            }
        },
    )

    assert _resolve_mcp_server("srv") == (
        ("node",),
        {
            "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}",
            "S": "${KIROCREW_INTERNAL_SECRET}",
            "A": "v",
        },
    )


class TestSwitchedOffTool:
    def test_a_tool_switched_off_in_the_spec_is_refused_before_a_server_starts(self, home, started):
        _write_servers(home["spec"], {"srv": {"command": "x", "disabledTools": ["t"]}})
        servers = KeptMcpServers()

        with pytest.raises(McpToolError, match="tool 't' on MCP server 'srv' is switched off"):
            servers.call_tool("srv", "t", {})
        assert started == []

        assert servers.call_tool("srv", "u", {}) == "ran u"
        assert started == ["srv"]
        servers.close()

    def test_a_tool_switched_off_in_the_kiro_global_settings_is_refused(self, home, started):
        """The dashboard's tool-off action writes ``disabledTools`` to the Kiro global
        settings file and nowhere else."""
        _write_servers(home["spec"], {"srv": {"command": "x"}})
        _write_servers(home["kiro_global"], {"srv": {"disabledTools": ["t"]}})
        servers = KeptMcpServers()

        with pytest.raises(McpToolError, match="switched off"):
            servers.call_tool("srv", "t", {})
        assert started == []
        servers.close()

    def test_a_tool_switched_off_under_the_server_s_alias_is_refused(self, home, started):
        """The tool-off action writes the key discovery shows, the alias of the spec's key."""
        _write_servers(home["spec"], {"npm:@playwright/mcp": {"command": "x"}})
        _write_servers(home["kiro_global"], {"playwright-mcp": {"disabledTools": ["nav"]}})
        servers = KeptMcpServers()

        with pytest.raises(McpToolError, match="switched off"):
            servers.call_tool("npm:@playwright/mcp", "nav", {})
        assert started == []
        servers.close()

    @pytest.mark.parametrize(
        ("entry", "tool", "refused"),
        [
            ("delete_*", "delete_repo", True),
            ("get_?", "get_x", True),
            ("delete_*", "list_repos", False),
            ("{a,b}_*", "list_repos", True),
        ],
        ids=["star", "question-mark", "glob-misses", "unread-glob-refuses"],
    )
    def test_a_glob_entry_is_matched_as_kiro_cli_matches_it(
        self, home, started, entry, tool, refused
    ):
        _write_servers(home["spec"], {"srv": {"command": "x", "disabledTools": [entry]}})
        servers = KeptMcpServers()

        if refused:
            with pytest.raises(McpToolError, match="switched off"):
                servers.call_tool("srv", tool, {})
            assert started == []
        else:
            assert servers.call_tool("srv", tool, {}) == f"ran {tool}"
        servers.close()
