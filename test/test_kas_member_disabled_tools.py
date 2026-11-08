"""A saved member spec's per-tool ``disabledTools`` holds on KAS.

The KAS customAgents wire discards ``disabledTools`` from a server entry
(``kas_agents._KAS_DISCARDED_ENTRY_KEYS``), so the projection re-expresses each
switched-off tool as ``@<server>/<tool>`` in the agent's ``excludedTools``, the
spelling KAS tags MCP tools with and the one the crewmate worker's spec already
uses (``mate_worker.disabled_tool_refs``). With that carrier KAS joins
``ACP_BACKENDS_MEMBER_TOOL_LIMITS``, so the start gate lets such a spec start
there, and refuses it on harnesses outside the set. The dashboard's switch, which
lives in the global settings file only, joins the same field at session start.
"""

from __future__ import annotations

import json

import pytest
from kas_agents_helpers import _spec

from kiro_crew import agent as agent_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.kas_agents import KasAgentTranslationError, to_client_custom_agent
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_PI,
    ACP_BACKENDS_MEMBER_TOOL_LIMITS,
)
from kiro_crew.agent_capabilities import member_backend_gap, tool_limited_servers
from kiro_crew.config import paths as paths_mod
from kiro_crew.mate_worker import disabled_tool_refs
from kiro_crew.mcp_gateway import session_servers as session_servers_mod


def _project(servers, **over):
    return to_client_custom_agent("member", _spec(mcpServers=servers, **over), "p")


class TestDisabledToolsBecomeExclusions:
    def test_each_switched_off_tool_is_excluded_by_its_tag(self):
        out = _project({"docs": {"command": "x", "disabledTools": ["wipe", "purge"]}})
        assert out["excludedTools"] == ["@docs/wipe", "@docs/purge"]

    def test_the_spelling_is_the_worker_paths(self):
        servers = {
            "docs": {"command": "x", "disabledTools": ["wipe"]},
            "notes": {"url": "https://example.test/mcp", "disabledTools": ["drop_all"]},
        }
        assert _project(servers)["excludedTools"] == disabled_tool_refs(servers)

    def test_names_are_kept_as_declared(self):
        out = _project({"My-Docs": {"command": "x", "disabledTools": ["Wipe_All"]}})
        assert out["excludedTools"] == ["@My-Docs/Wipe_All"]

    def test_the_spec_exclusions_are_kept_and_joined_without_duplicates(self):
        out = _project(
            {"docs": {"command": "x", "disabledTools": ["wipe", "wipe"]}},
            excludedTools=["fs_write", "@docs/wipe"],
        )
        excluded = out["excludedTools"]
        assert excluded[0] == "fs_write"
        assert "delete_file" in excluded  # the kiro-cli exclusion expansion still runs
        assert excluded.count("@docs/wipe") == 1

    def test_a_muted_server_stays_muted_and_adds_nothing(self):
        out = _project(
            {
                "docs": {"command": "x", "disabledTools": ["wipe"], "disabled": True},
                "odd": {"command": "x", "disabledTools": ["wipe"], "disabled": "yes"},
            }
        )
        assert "docs" not in out.get("mcpServers", {})
        assert "odd" not in out.get("mcpServers", {})
        assert "excludedTools" not in out

    def test_the_server_is_still_declared(self):
        out = _project({"docs": {"command": "x", "disabledTools": ["wipe"]}})
        assert "docs" in out["mcpServers"]

    def test_a_stubbed_server_keeps_its_exclusion(self):
        # A stubbed name arrives as a session-level entry with the same server
        # name, so the tag the exclusion matches is the same.
        out = to_client_custom_agent(
            "member",
            _spec(mcpServers={"docs": {"command": "x", "disabledTools": ["wipe"]}}),
            "p",
            stub_server_names=frozenset({"docs"}),
        )
        assert "docs" not in out.get("mcpServers", {})
        assert out["excludedTools"] == ["@docs/wipe"]

    @pytest.mark.parametrize("disabled", [[], None], ids=["empty", "none"])
    def test_an_empty_value_adds_nothing(self, disabled):
        out = _project({"docs": {"command": "x", "disabledTools": disabled}})
        assert "excludedTools" not in out

    @pytest.mark.parametrize(
        "disabled",
        ["wipe", {"wipe": True}, [1], ["wipe", None], ["  "]],
        ids=["string", "object", "number", "partly-unusable", "blank"],
    )
    def test_a_value_that_names_no_tools_is_refused(self, disabled):
        # The start gate counts any set value as a limit, so a value nothing can
        # carry must not start the server with every tool on.
        with pytest.raises(KasAgentTranslationError, match="docs"):
            _project({"docs": {"command": "x", "disabledTools": disabled}})

    def test_a_muted_server_with_an_unusable_value_still_projects(self):
        out = _project({"docs": {"command": "x", "disabledTools": "wipe", "disabled": True}})
        assert "docs" not in out.get("mcpServers", {})

    def test_a_spec_without_limits_is_unchanged(self):
        assert "excludedTools" not in _project({"docs": {"command": "x"}})


class TestTheStartGate:
    _LIMITED = tool_limited_servers(
        {"mcpServers": {"docs": {"command": "x", "disabledTools": ["wipe"]}}}
    )

    def test_the_members_are_kiro_kas_and_claude(self):
        assert ACP_BACKENDS_MEMBER_TOOL_LIMITS == frozenset(
            {ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE}
        )

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE])
    def test_members_start_a_limited_spec(self, backend):
        assert self._LIMITED == ("docs",)
        assert member_backend_gap(backend, self._LIMITED) is None

    @pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_DEEPSEEK, ACP_BACKEND_PI])
    def test_other_member_backends_still_refuse(self, backend):
        assert member_backend_gap(backend, self._LIMITED) == "tool_limits_unsupported"


@pytest.mark.asyncio
async def test_a_switch_in_the_global_settings_file_is_excluded(monkeypatch, tmp_path):
    # The dashboard's tool-off writes only the global file, and the spec rebuild
    # never copies it onto Crew's server entry.
    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(
        session_servers_mod, "injection_server_names", lambda overlay, agent: frozenset()
    )
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: tmp_path)
    settings = tmp_path / "settings" / "mcp.json"
    settings.parent.mkdir()
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", settings)
    spec = _spec(name="a", mcpServers={"kirocrew-core": {"command": "x"}})
    (tmp_path / "a.json").write_text(json.dumps(spec), encoding="utf-8")
    switch = {"mcpServers": {"kirocrew-core": {"disabledTools": ["memory_store"]}}}
    settings.write_text(json.dumps(switch), encoding="utf-8")

    extras = await harness_for(ACP_BACKEND_KAS).session_extras("a", work_dir=str(tmp_path))

    assert "@kirocrew-core/memory_store" in extras.custom_agents[0].get("excludedTools", [])
