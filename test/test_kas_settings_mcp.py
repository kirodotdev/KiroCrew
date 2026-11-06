"""kiro-cli's mcp.json / cli.json settings carried onto the KAS agent block.

Covers ``useLegacyMcpJson`` (P1-17), glob ``disabledTools`` on the mcp.json
servers KAS starts (P0-14), that those servers keep starting whether or not the
agent opted in, the 120 s default and ``mcp.initTimeout`` floor (P0-21, P1-37), and the
legacy ``.amazonq/mcp.json`` fallback (P1-35).
"""

from __future__ import annotations

import json

import pytest
from kas_agents_helpers import _spec

from kiro_crew import agent as agent_mod
from kiro_crew.acp._dispatch import attach_kas_custom_agents
from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.acp.kas_permissions import CREW_SETTINGS_MCP_KEY
from kiro_crew.acp.kas_settings_mcp import (
    finish_settings_mcp,
    stamp_settings_mcp,
    with_legacy_mcp_json,
)
from kiro_crew.agent_materialization.auto_approve import govern_spec


@pytest.fixture
def home(tmp_path, monkeypatch):
    settings = tmp_path / "home" / ".kiro" / "settings"
    settings.mkdir(parents=True)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", settings / "mcp.json")
    monkeypatch.delenv("ASBX_KIRO_MANDATORY_MCPS", raising=False)
    return settings


@pytest.fixture
def work(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return root


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _agent(**over):
    base = {"id": "a", "tools": ["*"], "mcpServers": {"own": {"command": "x"}}}
    base.update(over)
    return base


def _finish(agent, work, session=()):
    (stamped,) = stamp_settings_mcp([agent], "a", work)
    return finish_settings_mcp(stamped, list(session))


class TestLegacySpelling:
    def test_use_legacy_mcp_json_travels_as_include_mcp_json(self):
        spec = _spec(useLegacyMcpJson=True)
        del spec["includeMcpJson"]
        assert to_client_custom_agent("a", spec, "p")["includeMcpJson"] is True

    def test_include_mcp_json_wins_over_the_alias(self):
        out = to_client_custom_agent("a", _spec(includeMcpJson=False, useLegacyMcpJson=True), "p")
        assert out["includeMcpJson"] is False


class TestUndeclaredFileServers:
    @pytest.mark.parametrize("opt_in", [{}, {"includeMcpJson": False}, {"includeMcpJson": True}])
    def test_undeclared_servers_keep_starting_with_their_glob_disabled_tools(
        self, home, work, opt_in
    ):
        _write(
            home / "mcp.json",
            {"mcpServers": {"extra": {"disabledTools": ["write_*", "exact", "de?"]}, "own": {}}},
        )
        _write(work / ".kiro" / "settings" / "mcp.json", {"mcpServers": {"proj": {"command": "p"}}})
        out = _finish(_agent(**opt_in), work, [{"name": "stubbed"}])
        assert out["excludedTools"] == ["@extra/write_*", "@extra/de?"]

    def test_no_server_is_excluded_whole(self, home, work):
        _write(home / "mcp.json", {"mcpServers": {"extra": {}, "proj": {"command": "p"}}})
        assert "excludedTools" not in _finish(_agent(), work)

    def test_declared_and_session_level_servers_get_no_file_globs(self, home, work):
        _write(
            home / "mcp.json",
            {
                "mcpServers": {
                    "own": {"disabledTools": ["a*"]},
                    "stubbed": {"disabledTools": ["b*"]},
                }
            },
        )
        assert "excludedTools" not in _finish(_agent(), work, [{"name": "stubbed"}])

    def test_existing_exclusions_are_kept(self, home, work):
        _write(home / "mcp.json", {"mcpServers": {"extra": {"disabledTools": ["w*"]}}})
        out = _finish(_agent(excludedTools=["fs_write"]), work)
        assert out["excludedTools"] == ["fs_write", "@extra/w*"]

    def test_workspace_entry_overrides_the_global_one(self, home, work):
        _write(home / "mcp.json", {"mcpServers": {"extra": {"disabledTools": ["a*"]}}})
        _write(
            work / ".kiro" / "settings" / "mcp.json",
            {"mcpServers": {"extra": {"disabledTools": ["b*"]}}},
        )
        out = _finish(_agent(), work)
        assert out["excludedTools"] == ["@extra/b*"]

    def test_only_the_active_entry_is_stamped_and_an_unstamped_one_passes(self, home, work):
        other = _agent(id="other")
        stamped = stamp_settings_mcp([other, _agent()], "a", work)
        assert stamped[0] is other
        assert CREW_SETTINGS_MCP_KEY in stamped[1]
        assert finish_settings_mcp(other, []) is other

    def test_the_wire_envelope_applies_the_stamp_and_strips_it(self, home, work):
        _write(home / "mcp.json", {"mcpServers": {"extra": {"disabledTools": ["w*"]}}})
        params = {"cwd": str(work), "mcpServers": [{"name": "stubbed"}]}
        attach_kas_custom_agents(params, stamp_settings_mcp([_agent()], "a", work))
        (sent,) = params["_meta"]["kiro"]["customAgents"]
        assert CREW_SETTINGS_MCP_KEY not in sent
        assert sent["excludedTools"] == ["@extra/w*"]
        assert sent["mcpServers"]["own"]["timeout"] == 120_000


class TestIncludeMcpJsonDefault:
    """An absent ``includeMcpJson`` is not forwarded, and the KAS projection
    excludes no ``mcp.json`` server for it.

    Crew's own readers (``agent_capabilities``, ``client_mcp_array``,
    ``fork_refresh``, ``auto_approve``) read absent as ``true``; they agree with
    the projection because every spec Crew writes goes through ``govern_spec``,
    which pins the key. This pins both halves.
    """

    def test_absent_is_not_forwarded_and_excludes_nothing(self, home, work):
        spec = _spec()
        del spec["includeMcpJson"]
        assert "includeMcpJson" not in to_client_custom_agent("a", spec, "p")
        _write(home / "mcp.json", {"mcpServers": {"extra": {}}})
        assert "excludedTools" not in _finish(_agent(), work)

    def test_crews_writer_pins_false_so_the_readers_default_never_applies(self, home, work):
        config = {"name": "a", "tools": ["*"], "mcpServers": {"own": {"command": "x"}}}
        govern_spec(config, agent_filename="a.json", source="test")
        assert config["includeMcpJson"] is False
        projected = to_client_custom_agent("a", config, "p")
        assert projected["includeMcpJson"] is False


class TestTimeout:
    def test_unset_timeout_gets_kiro_cli_default(self, home, work):
        out = _finish(_agent(mcpServers={"s": {"command": "x"}, "r": {"url": "https://h"}}), work)
        assert out["mcpServers"]["s"]["timeout"] == 120_000
        assert out["mcpServers"]["r"]["timeout"] == 120_000

    def test_set_timeout_is_kept_and_alias_is_mapped(self, home, work):
        servers = {
            "s": {"command": "x", "timeout": 5000},
            "t": {"command": "y", "timeoutMs": 90000},
        }
        out = _finish(_agent(mcpServers=servers), work)
        assert out["mcpServers"]["s"] == {"command": "x", "timeout": 5000}
        assert out["mcpServers"]["t"] == {"command": "y", "timeout": 90000}

    @pytest.mark.parametrize(
        ("init", "want"), [(300_000, 300_000), (900_000, 600_000), (10, 120_000)]
    )
    def test_init_timeout_raises_the_default(self, home, work, init, want):
        _write(home / "cli.json", {"mcp.initTimeout": init})
        out = _finish(_agent(), work)
        assert out["mcpServers"]["own"]["timeout"] == want

    def test_workspace_cli_json_overrides_global(self, home, work):
        _write(home / "cli.json", {"mcp.initTimeout": 300_000})
        _write(work / ".kiro" / "settings" / "cli.json", {"mcp.initTimeout": 200_000})
        assert _finish(_agent(), work)["mcpServers"]["own"]["timeout"] == 200_000


class TestLegacyAmazonqFile:
    def _legacy(self, work):
        _write(
            work / ".amazonq" / "mcp.json",
            {"mcpServers": {"old": {"command": "o"}, "kirocrew-core": {"command": "evil"}}},
        )

    def test_merged_when_opted_in_and_no_kiro_dir(self, work):
        self._legacy(work)
        out = with_legacy_mcp_json(_spec(includeMcpJson=True), work)
        assert out["mcpServers"] == {"old": {"command": "o"}, "kirocrew-core": {"command": "x"}}

    def test_a_merged_entry_loses_its_auto_approve(self, work):
        _write(
            work / ".amazonq" / "mcp.json",
            {"mcpServers": {"old": {"command": "o", "autoApprove": ["run"]}}},
        )
        out = with_legacy_mcp_json(_spec(includeMcpJson=True), work)
        assert out["mcpServers"]["old"] == {"command": "o"}

    def test_alias_opts_in_too(self, work):
        self._legacy(work)
        spec = _spec(useLegacyMcpJson=True)
        del spec["includeMcpJson"]
        assert "old" in with_legacy_mcp_json(spec, work)["mcpServers"]

    def test_not_merged_without_opt_in_or_with_a_kiro_dir(self, work):
        self._legacy(work)
        spec = _spec()
        assert with_legacy_mcp_json(spec, work) is spec
        (work / ".kiro").mkdir()
        opted = _spec(includeMcpJson=True)
        assert with_legacy_mcp_json(opted, work) is opted
