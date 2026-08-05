"""Tests for Mochi's MCP reach policy and the framework merge that applies it.

Every case here pins a behaviour whose failure mode is SILENT: a grant that
never reaches the agent config, or a deny that reads like a deny and behaves
like an allow.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from kiro_crew.apps.bridges import _apply_agent_mcp_policy
from kiro_crew.apps.builtins.mochi.agent_policy import (
    BG_AGENT,
    CHAT_AGENT,
    build_policy,
    write_policy,
)


class TestBuildPolicy:
    def test_audience_maps_chat_and_bg_to_real_agent_names(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.mochi.agent_policy._ambient_servers", lambda: {}
        )
        pol = build_policy(
            {
                "extraMcpServers": [
                    {"name": "a", "agents": ["chat"]},
                    {"name": "b", "agents": ["bg"]},
                    {"name": "c", "agents": ["chat", "bg"]},
                ]
            }
        )
        assert sorted(pol["agents"][CHAT_AGENT]["servers"]) == ["a", "c"]
        assert sorted(pol["agents"][BG_AGENT]["servers"]) == ["b", "c"]

    def test_string_entry_defaults_to_chat_only(self, monkeypatch):
        """Legacy settings stored bare strings; they must not silently grant bg."""
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.mochi.agent_policy._ambient_servers", lambda: {}
        )
        pol = build_policy({"extraMcpServers": ["legacy"]})
        assert "legacy" in pol["agents"][CHAT_AGENT]["servers"]
        assert "legacy" not in pol["agents"][BG_AGENT]["servers"]

    def test_ungranted_ambient_server_is_neutralized_with_its_real_tools(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.mochi.agent_policy._ambient_servers",
            lambda: {"ambient": ["t1", "t2"]},
        )
        pol = build_policy({"extraMcpServers": []})
        assert pol["agents"][CHAT_AGENT]["neutralize"] == {"ambient": ["t1", "t2"]}

    def test_unprobed_server_is_neutralized_not_left_ambient(self, monkeypatch):
        """A server whose tools are not yet probed (empty list) must still be
        neutralized. The bridge writes server-level ``disabled: true``, so an
        empty tool list fully denies it — leaving it out (the old audit-only
        ``pending`` path) was a fail-open that kept Mochi's ambient access.
        """
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.mochi.agent_policy._ambient_servers",
            lambda: {"unprobed": []},
        )
        pol = build_policy({"extraMcpServers": []})
        assert pol["agents"][CHAT_AGENT]["neutralize"] == {"unprobed": []}
        assert "pendingNeutralize" not in pol["agents"][CHAT_AGENT]

    def test_granted_server_is_never_also_neutralized(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.mochi.agent_policy._ambient_servers",
            lambda: {"shared": ["t1"]},
        )
        pol = build_policy({"extraMcpServers": [{"name": "shared", "agents": ["chat"]}]})
        assert "shared" in pol["agents"][CHAT_AGENT]["servers"]
        assert "shared" not in pol["agents"][CHAT_AGENT]["neutralize"]
        # ...but the bg agent, which was NOT granted it, still gets it denied.
        assert "shared" in pol["agents"][BG_AGENT]["neutralize"]

    def test_own_server_is_never_neutralized(self, monkeypatch):
        """The app's own MCP server is the pet's reason to exist."""
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.mochi.agent_policy._ambient_servers",
            lambda: {"mochi:mochi": ["perform_pet_action"], "other": ["x"]},
        )
        pol = build_policy({"extraMcpServers": []})
        for agent in (CHAT_AGENT, BG_AGENT):
            assert "mochi:mochi" not in pol["agents"][agent]["neutralize"]
            assert "other" in pol["agents"][agent]["neutralize"]

    def test_discovery_failure_does_not_raise(self, monkeypatch, tmp_path):
        def boom():
            raise RuntimeError("probe exploded")

        monkeypatch.setattr("kiro_crew.mcp_discovery.list_servers", boom)
        # HOME must be isolated: the second enumeration source is the real global
        # mcp.json, so without this the assertion depends on the dev's own config.
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        pol = build_policy({"extraMcpServers": []})
        # Empty because this tmp HOME configures NO servers — not because the
        # failure was swallowed. A failure with servers configured must still deny
        # them: see TestAmbientEnumerationFailsClosed.
        assert pol["agents"][CHAT_AGENT]["neutralize"] == {}

    def test_write_policy_lands_where_the_framework_reads_it(self, tmp_path, monkeypatch):
        from kiro_crew.apps import bridges

        monkeypatch.setattr(
            "kiro_crew.apps.builtins.mochi.agent_policy._ambient_servers", lambda: {}
        )
        write_policy(tmp_path, {"extraMcpServers": ["x"]})
        written = tmp_path / bridges.AGENT_MCP_POLICY_FILE
        assert written.is_file()
        assert "x" in json.loads(written.read_text())["agents"][CHAT_AGENT]["servers"]


class TestApplyAgentMcpPolicy:
    def _policy(self, **per_agent):
        return {"agents": {CHAT_AGENT: per_agent}}

    def test_grant_adds_server_and_tool_reference(self, monkeypatch):
        # A grant must produce a COMPLETE server spec: the policy carries only
        # POLICY, so the launch command comes from the ambient MCP config. A
        # command-less entry never launches, so the tool is simply absent and
        # the agent reports "not available" with nothing logged anywhere.
        from kiro_crew.apps import bridges

        monkeypatch.setattr(
            bridges, "_global_mcp_specs", lambda: {"srv": {"command": "srv-cmd", "args": []}}
        )
        out = _apply_agent_mcp_policy(
            {"name": CHAT_AGENT, "tools": ["fs_read"], "allowedTools": ["fs_read"]},
            CHAT_AGENT,
            self._policy(servers={"srv": {"autoApprove": ["a"], "disabledTools": []}}),
        )
        assert out["mcpServers"]["srv"]["autoApprove"] == ["a"]
        assert out["mcpServers"]["srv"]["command"] == "srv-cmd"
        assert "@srv" in out["tools"]
        # allowedTools too: a server only in `tools` still prompts per call,
        # which for an unattended agent resolves to "rejected".
        assert "@srv" in out["allowedTools"]

    def test_grant_without_any_launch_spec_is_skipped(self, monkeypatch):
        from kiro_crew.apps import bridges

        monkeypatch.setattr(bridges, "_global_mcp_specs", lambda: {})
        out = _apply_agent_mcp_policy(
            {"name": CHAT_AGENT, "tools": ["fs_read"]},
            CHAT_AGENT,
            self._policy(servers={"ghost": {"autoApprove": [], "disabledTools": []}}),
        )
        assert "ghost" not in (out.get("mcpServers") or {})
        assert "@ghost" not in out["tools"]

    def test_neutralize_declares_the_server_and_removes_its_tool_reference(self, monkeypatch):
        # The entry must carry the FULL spec (copied from the global mcp.json):
        # kiro-cli's strict agent loader rejects the whole file over a
        # command-less mcpServers entry, unregistering the agent instead of
        # denying the server.
        from kiro_crew.apps import bridges

        monkeypatch.setattr(
            bridges, "_global_mcp_specs", lambda: {"amb": {"command": "amb-cmd", "args": []}}
        )
        out = _apply_agent_mcp_policy(
            {"name": CHAT_AGENT, "tools": ["fs_read", "@amb"]},
            CHAT_AGENT,
            self._policy(neutralize={"amb": ["t1", "t2"]}),
        )
        assert out["mcpServers"]["amb"]["disabledTools"] == ["t1", "t2"]
        assert out["mcpServers"]["amb"]["command"] == "amb-cmd"
        assert "@amb" not in out["tools"]

    def test_policy_for_another_agent_is_ignored(self):
        original = {"name": BG_AGENT, "tools": ["fs_read"]}
        out = _apply_agent_mcp_policy(dict(original), BG_AGENT, self._policy(servers={"srv": {}}))
        assert out.get("mcpServers", {}) == {} or "srv" not in out["mcpServers"]
        assert out["tools"] == ["fs_read"]

    def test_empty_policy_leaves_the_config_untouched(self):
        cfg = {"name": CHAT_AGENT, "tools": ["fs_read"], "mcpServers": {"k": {}}}
        assert _apply_agent_mcp_policy(dict(cfg), CHAT_AGENT, {}) == cfg


class TestQueuedPetActionIsExecutable:
    """A pet action queued by the MCP server must be executable by the poller.

    The MCP server runs as a SEPARATE process (``kirocrew app mcp mochi``) and can
    only hand work over through the queue file, so the file's contract is the only
    thing keeping the two halves in step. It previously wrote the payload without
    ``execute_after`` / ``id`` / ``urgent``, and every one of those omissions fails
    SILENTLY: get_executable_tasks() treats a missing execute_after as not-due, so
    the action sat in the queue and the pet simply never moved.
    """

    def _queue(self, monkeypatch, tmp_path, args):
        from kiro_crew.apps.builtins.mochi import mcp_server as ms

        monkeypatch.setattr(ms, "_data_dir", lambda: tmp_path)
        ms._tool_perform_pet_action(args)
        from kiro_crew.apps.builtins.mochi import queue_file as qf

        return qf, ms, qf.read_queue(str(tmp_path / ms._QUEUE_FILE))

    def test_move_is_due_immediately(self, monkeypatch, tmp_path):
        qf, ms, queue = self._queue(
            monkeypatch, tmp_path, {"action": "move", "waypoints": [{"x": 1, "y": 2}]}
        )
        due = qf.get_executable_tasks(queue, ms._now_ms())
        assert len(due) == 1, "queued move is not due — the pet would never move"
        assert due[0]["type"] == "move"

    def test_task_carries_an_id_so_it_can_be_marked_done(self, monkeypatch, tmp_path):
        _, _, queue = self._queue(monkeypatch, tmp_path, {"action": "mood", "mood": "happy"})
        assert queue["tasks"][0].get("id"), "no id — the poller re-executes it every second"

    def test_task_is_urgent_so_the_stale_skip_cannot_drop_it(self, monkeypatch, tmp_path):
        _, _, queue = self._queue(monkeypatch, tmp_path, {"action": "move", "x": 5, "y": 6})
        assert queue["tasks"][0].get("urgent") is True

    def test_payload_survives_into_the_task(self, monkeypatch, tmp_path):
        _, _, queue = self._queue(
            monkeypatch, tmp_path, {"action": "move", "behavior": "hide_left", "interrupt": False}
        )
        task = queue["tasks"][0]
        assert task["behavior"] == "hide_left" and task["interrupt"] is False

    def test_query_does_not_queue_anything(self, monkeypatch, tmp_path):
        from kiro_crew.apps.builtins.mochi import mcp_server as ms

        monkeypatch.setattr(ms, "_data_dir", lambda: tmp_path)
        out = ms._tool_perform_pet_action({"action": "query"})
        assert "displays" in out
        assert not (tmp_path / ms._QUEUE_FILE).exists()


class TestAmbientEnumerationFailsClosed:
    """A server the probe cache never reported must still be neutralized.

    ``_ambient_servers`` used to read the probe cache ALONE, so an enumeration
    failure returned ``{}``, every ``neutralize`` map came back empty, and an
    empty map is indistinguishable from "there is nothing to deny": Mochi kept
    ambient reach to servers the user never granted it, silently. The global MCP
    config is a second, independent source of NAMES — and a name with no tools
    denies completely, because the bridge disables at the server level.
    """

    @staticmethod
    def _global_config(monkeypatch, tmp_path, servers):
        """Point HOME at a tmp dir carrying a global mcp.json."""
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True, exist_ok=True)
        (settings / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    @staticmethod
    def _discovery_raises(monkeypatch):
        """Make the probe-cache import itself fail, the way GPT's case does."""
        import builtins as _builtins

        real_import = _builtins.__import__

        def boom(name, *args, **kwargs):
            if name == "kiro_crew.mcp_discovery":
                raise RuntimeError("probe cache unavailable")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(_builtins, "__import__", boom)

    def test_discovery_failure_still_neutralizes_configured_servers(self, monkeypatch, tmp_path):
        self._global_config(monkeypatch, tmp_path, {"stripe": {"command": "x"}})
        self._discovery_raises(monkeypatch)

        policy = build_policy({"extraMcpServers": []})
        for agent in (CHAT_AGENT, BG_AGENT):
            assert "stripe" in policy["agents"][agent]["neutralize"], (
                f"{agent} kept ambient reach to an ungranted server after a "
                "discovery failure — the fail-open this guards"
            )

    def test_a_granted_server_is_still_granted_via_the_config_source(self, monkeypatch, tmp_path):
        self._global_config(monkeypatch, tmp_path, {"stripe": {"command": "x"}})
        self._discovery_raises(monkeypatch)

        policy = build_policy({"extraMcpServers": ["stripe"]})
        chat = policy["agents"][CHAT_AGENT]
        assert "stripe" in chat["servers"] and "stripe" not in chat["neutralize"]

    def test_configured_but_unprobed_server_is_neutralized(self, monkeypatch, tmp_path):
        """The cache also LAGS: a just-added server is ambient (kiro-cli loads it)
        before anything has probed it, so a SUCCESSFUL probe that simply does not
        mention it must not leave it reachable."""
        self._global_config(monkeypatch, tmp_path, {"probed": {}, "fresh": {}})

        class _Server:
            def __init__(self, name, tools):
                self.name, self.tools = name, tools

        monkeypatch.setitem(
            sys.modules,
            "kiro_crew.mcp_discovery",
            types.SimpleNamespace(list_servers=lambda: [_Server("probed", ["a", "b"])]),
        )

        neutralize = build_policy({"extraMcpServers": []})["agents"][CHAT_AGENT]["neutralize"]
        assert neutralize["probed"] == ["a", "b"], "probe tools must still enrich the entry"
        assert neutralize["fresh"] == [], "an unprobed configured server must still be denied"

    def test_absent_global_config_is_not_treated_as_unreadable(self, monkeypatch, tmp_path):
        """No file means no global servers — an empty deny list is CORRECT here,
        not a fail-open, so this must not invent entries."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        self._discovery_raises(monkeypatch)

        policy = build_policy({"extraMcpServers": []})
        assert policy["agents"][CHAT_AGENT]["neutralize"] == {}

    def test_malformed_global_config_does_not_raise(self, monkeypatch, tmp_path):
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        (settings / "mcp.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        policy = build_policy({"extraMcpServers": []})
        assert CHAT_AGENT in policy["agents"]


class TestPolicyMaterializationFailsClosed:
    """`apply_policy` must not report success when the deny policy did not reach
    the agent configs.

    kiro-cli loads every globally configured MCP server into an agent regardless
    of that agent's own config, so until the `neutralize` entries are materialized
    the agent HAS ambient reach. The failure used to be a `logger.warning` — and
    the caller (`on_startup`) then started the pet anyway.
    """

    @staticmethod
    def _manifest(agents):
        return types.SimpleNamespace(agents=agents)

    def test_a_raising_refresh_is_not_swallowed(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.mochi import agent_policy as ap

        monkeypatch.setattr(ap, "_ambient_servers", lambda: {})
        monkeypatch.setattr(
            "kiro_crew.apps.bridges.refresh_app_agents",
            lambda _app: (_ for _ in ()).throw(OSError("disk gone")),
        )
        with pytest.raises(ap.PolicyNotMaterialized):
            ap.apply_policy(tmp_path, {"extraMcpServers": []})

    def test_a_partial_refresh_is_a_failure(self, tmp_path, monkeypatch):
        """The agent that got skipped is the one left holding ambient reach."""
        from kiro_crew.apps.builtins.mochi import agent_policy as ap

        monkeypatch.setattr(ap, "_ambient_servers", lambda: {})
        monkeypatch.setattr("kiro_crew.apps.bridges.refresh_app_agents", lambda _app: ["mochi"])
        monkeypatch.setattr(
            "kiro_crew.apps.bridges.get_app_manifest",
            lambda _app: self._manifest(["agents/mochi.json", "agents/mochi-bg.json"]),
        )
        with pytest.raises(ap.PolicyNotMaterialized, match="1 of 2"):
            ap.apply_policy(tmp_path, {"extraMcpServers": []})

    def test_no_registered_agents_is_not_a_failure(self, tmp_path, monkeypatch):
        """An empty refresh ALSO means "nothing registered to write to" — and then
        no Mochi agent config exists for kiro-cli to load, so nothing holds
        ambient reach. Treating that as a failure fired in every environment where
        the app is not registry-installed."""
        from kiro_crew.apps.builtins.mochi import agent_policy as ap

        monkeypatch.setattr(ap, "_ambient_servers", lambda: {})
        monkeypatch.setattr("kiro_crew.apps.bridges.refresh_app_agents", lambda _app: [])
        monkeypatch.setattr(
            "kiro_crew.apps.bridges.get_app_manifest", lambda _app: self._manifest([])
        )
        policy = ap.apply_policy(tmp_path, {"extraMcpServers": []})
        assert CHAT_AGENT in policy["agents"]

    def test_the_policy_still_lands_on_disk_when_materialization_fails(
        self, tmp_path, monkeypatch
    ):
        """The boot reconcile reads the file, so persisting it is the recovery."""
        from kiro_crew.apps import bridges
        from kiro_crew.apps.builtins.mochi import agent_policy as ap

        monkeypatch.setattr(ap, "_ambient_servers", lambda: {})
        monkeypatch.setattr(
            "kiro_crew.apps.bridges.refresh_app_agents",
            lambda _app: (_ for _ in ()).throw(OSError("disk gone")),
        )
        with pytest.raises(ap.PolicyNotMaterialized):
            ap.apply_policy(tmp_path, {"extraMcpServers": ["x"]})
        written = tmp_path / bridges.AGENT_MCP_POLICY_FILE
        assert written.is_file()
        assert "x" in json.loads(written.read_text())["agents"][CHAT_AGENT]["servers"]
