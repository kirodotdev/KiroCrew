"""App bridge tests: the prompt-only ``kirocrew-apps`` server in materialized app agent configs."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from app_bridges_helpers import _stub_venv_probe  # noqa: F401
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME


class TestPromptOnlyManagedServer:
    @staticmethod
    def _make_host_ungoverned(monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.platform.context.current_context",
            lambda: SimpleNamespace(governance=None),
        )
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.any_configured_profile_governs",
            lambda _ref: False,
        )

    def test_mount_detector_is_casefolded_and_tolerates_malformed_shapes(self) -> None:
        from kiro_crew.platform.governance import (
            config_mounts_prompt_only_server,
            withhold_prompt_only_wildcards,
        )

        assert config_mounts_prompt_only_server(
            {"mcpServers": {"KIROCREW-APPS": {"command": "x"}}, "tools": ["*"]}
        )
        assert not config_mounts_prompt_only_server(
            {
                "mcpServers": {"kirocrew-apps": {"command": "x"}},
                "tools": ["@ordinary-server"],
            }
        )
        assert config_mounts_prompt_only_server({"tools": ["@KIROCREW-APPS/list_notes"]})
        assert not config_mounts_prompt_only_server(
            {"mcpServers": ["kirocrew-apps"], "tools": "@kirocrew-apps"}
        )
        assert not config_mounts_prompt_only_server(
            {"mcpServers": {7: {}}, "tools": [None, 7, "@ordinary-server"]}
        )
        malformed = {
            "allowedTools": "*",
            "mcpServers": {"kirocrew-apps": {"command": "x"}},
        }
        assert withhold_prompt_only_wildcards(malformed) == []
        assert malformed["allowedTools"] == "*"

    @pytest.mark.parametrize(
        ("global_on", "local_on", "tools", "include_mcp_json", "withheld"),
        [
            pytest.param(True, None, ["*"], None, True, id="list-wildcard"),
            pytest.param(True, None, "*", None, True, id="whole-value-wildcard"),
            pytest.param(True, None, ["*"], False, False, id="inheritance-disabled"),
            pytest.param(True, None, ["@other"], None, False, id="unrelated-tools"),
            pytest.param(None, None, ["*"], None, False, id="server-absent"),
            pytest.param(False, None, ["*"], None, False, id="server-disabled"),
            pytest.param(None, True, ["@other"], None, False, id="local-declaration"),
            pytest.param(
                None, True, ["@kirocrew-apps"], None, True, id="local-declaration-selected"
            ),
            pytest.param(None, True, ["*"], None, True, id="local-declaration-wildcard"),
            pytest.param(True, False, ["*"], None, False, id="muted-local-wildcard"),
            pytest.param(True, False, ["@kirocrew-apps"], None, False, id="muted-local-ref"),
            pytest.param(True, None, ["@KIROCREW-APPS/x"], None, True, id="global-ref"),
            pytest.param(True, None, ["@kirocrew-apps"], False, True, id="ref-no-inherit"),
            pytest.param(None, None, ["@kirocrew-apps"], None, True, id="ref-no-global"),
        ],
    )
    def test_inherited_prompt_only_server_mount_detection(
        self, tmp_path, monkeypatch, global_on, local_on, tools, include_mcp_json, withheld
    ) -> None:
        from kiro_crew import agent
        from kiro_crew.platform.governance import withhold_prompt_only_wildcards

        entry = {"command": "kirocrew", "disabled": global_on is False}
        servers = {"other": {}} if global_on is None else {"kirocrew-apps": entry}
        settings = tmp_path / "settings-mcp.json"
        settings.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        monkeypatch.setattr(agent, "_KIRO_MCP_JSON", settings)
        allowed = ["@*", "fs_read"] if global_on else ["*", "fs_read"]
        config: dict[str, object] = {"tools": tools, "allowedTools": list(allowed)}
        if local_on is not None:
            config["mcpServers"] = {"kirocrew-apps": {"disabled": not local_on}}
        if include_mcp_json is not None:
            config["includeMcpJson"] = include_mcp_json
        expected_withheld = [allowed[0]] if withheld else []
        assert withhold_prompt_only_wildcards(config) == expected_withheld
        assert config["allowedTools"] == (allowed[1:] if withheld else allowed)

    def test_final_pass_keeps_bare_wildcard_for_a_declared_prompt_only_server(
        self, monkeypatch
    ) -> None:
        from kiro_crew.agent_materialization import auto_approve

        self._make_host_ungoverned(monkeypatch)
        ordinary_grant_check = auto_approve._may_auto_approve
        monkeypatch.setattr(
            auto_approve,
            "_may_auto_approve",
            lambda ref: ref == "fs_read" or ordinary_grant_check(ref),
        )
        config = {
            "tools": ["*"],
            "allowedTools": ["*", "fs_read"],
            "mcpServers": {"kirocrew-apps": {"command": "x"}},
        }

        auto_approve.final_ceiling_pass(config)

        assert config["allowedTools"] == ["*", "fs_read"]

    def test_final_pass_keeps_mcp_patterns_for_a_selected_prompt_only_server(
        self, monkeypatch
    ) -> None:
        from kiro_crew.agent_materialization import auto_approve

        self._make_host_ungoverned(monkeypatch)
        ordinary_grant_check = auto_approve._may_auto_approve
        monkeypatch.setattr(
            auto_approve,
            "_may_auto_approve",
            lambda ref: ref == "fs_read" or ordinary_grant_check(ref),
        )
        config = {
            "tools": ["@kirocrew-apps"],
            "allowedTools": ["@*", "@kirocrew-*", "fs_read"],
        }

        auto_approve.final_ceiling_pass(config)

        assert config["allowedTools"] == ["@*", "@kirocrew-*", "fs_read"]

    def test_final_pass_keeps_wildcards_without_a_prompt_only_server(self, monkeypatch) -> None:
        from kiro_crew.agent_materialization import auto_approve

        self._make_host_ungoverned(monkeypatch)
        config = {"tools": ["@ordinary-server"], "allowedTools": ["*", "@*"]}

        auto_approve.final_ceiling_pass(config)

        assert config["allowedTools"] == ["*", "@*"]

    def test_template_ceiling_leaves_prompt_only_patterns_to_the_launch_copy(
        self, monkeypatch
    ) -> None:
        from kiro_crew.agent_materialization import auto_approve

        self._make_host_ungoverned(monkeypatch)
        monkeypatch.setattr(
            auto_approve,
            "_may_auto_approve",
            lambda ref: ref in {"*", "web_fetch"},
        )
        mounted = {
            "tools": ["*"],
            "allowedTools": ["*", "web_fetch"],
            "mcpServers": {"kirocrew-apps": {"command": "x"}},
        }
        ordinary = {
            "tools": ["*"],
            "allowedTools": ["*", "web_fetch"],
            "mcpServers": {},
        }

        auto_approve._apply_allowed_tools_ceiling(mounted, source="test", audit=False)
        auto_approve._apply_allowed_tools_ceiling(ordinary, source="test", audit=False)

        assert mounted["allowedTools"] == ["*", "web_fetch"]
        assert ordinary["allowedTools"] == ["*", "web_fetch"]

    def test_native_skill_view_withholds_wildcard_only_for_prompt_only_server(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.acp.skill_projection import project_agent_view

        self._make_host_ungoverned(monkeypatch)
        base = {
            "name": "custom",
            "tools": ["*"],
            "allowedTools": ["*", "web_fetch"],
        }
        mounted_view, _search, mounted_refusal = project_agent_view(
            {
                **base,
                "mcpServers": {"kirocrew-apps": {}},
            },
            tmp_path / "custom.json",
            "custom",
            crew_home_id="test-home",
            inherits_skills=False,
            per_session_element=True,
            registry_ceiling=True,
        )
        ordinary_view, _search, ordinary_refusal = project_agent_view(
            {**base, "mcpServers": {}},
            tmp_path / "custom.json",
            "custom",
            crew_home_id="test-home",
            inherits_skills=False,
            per_session_element=True,
            registry_ceiling=True,
        )

        assert mounted_refusal is None
        assert mounted_view is not None
        assert mounted_view["allowedTools"] == ["web_fetch"]
        assert ordinary_refusal is None
        assert ordinary_view is not None
        assert ordinary_view["allowedTools"] == ["*", "web_fetch"]

    def test_native_skill_view_ignores_unselected_prompt_only_declaration(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.acp.skill_projection import project_agent_view

        self._make_host_ungoverned(monkeypatch)
        spec = {
            "name": "custom",
            "tools": ["@ordinary-server"],
            "allowedTools": ["*"],
            "mcpServers": {
                "kirocrew-apps": {"command": "kirocrew"},
                "ordinary-server": {"command": "ordinary"},
            },
        }
        ordinary_view, _search, ordinary_refusal = project_agent_view(
            spec,
            tmp_path / "custom.json",
            "custom",
            crew_home_id="test-home",
            inherits_skills=False,
            per_session_element=True,
            registry_ceiling=True,
        )
        mounted_view, _search, mounted_refusal = project_agent_view(
            {**spec, "tools": ["@ordinary-server", "@kirocrew-apps"]},
            tmp_path / "custom.json",
            "custom",
            crew_home_id="test-home",
            inherits_skills=False,
            per_session_element=True,
            registry_ceiling=True,
        )

        assert ordinary_refusal is None
        assert ordinary_view is not None
        assert ordinary_view["allowedTools"] == ["*"]
        assert mounted_refusal is None
        assert mounted_view is not None
        assert mounted_view["allowedTools"] == []

    @pytest.mark.parametrize(
        ("servers", "expected_allowed", "expected_events"),
        [({"kirocrew-apps": {}}, [], 1), ({}, ["@*"], 0)],
    )
    def test_native_skill_view_audits_only_withheld_prompt_only_wildcards(
        self, tmp_path, monkeypatch, servers, expected_allowed, expected_events
    ) -> None:
        from kiro_crew import agent
        from kiro_crew.acp.skill_projection import project_agent_view
        from kiro_crew.agent_materialization import shared_template_view

        self._make_host_ungoverned(monkeypatch)
        events: list[dict[str, object]] = []
        monkeypatch.setattr(shared_template_view, "_REPORTED", set())
        monkeypatch.setattr(
            agent,
            "sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        )

        view, _search, refusal = project_agent_view(
            {
                "name": "prompt-only-audit-probe",
                "tools": ["*"],
                "allowedTools": ["@*"],
                "mcpServers": servers,
            },
            tmp_path / "prompt-only-audit-probe.json",
            "prompt-only-audit-probe",
            crew_home_id="test-home",
            inherits_skills=False,
            per_session_element=True,
            registry_ceiling=True,
        )

        assert refusal is None
        assert view is not None
        assert view["allowedTools"] == expected_allowed
        withheld = [
            event for event in events if event.get("operation") == shared_template_view.WITHHELD_OP
        ]
        assert len(withheld) == expected_events
        if withheld:
            assert withheld[0]["source"] == "shared-template-view:prompt-only-audit-probe"
            assert "@*" in str(withheld[0]["resources"])
            assert "prompt-only server requires approval on every call" in str(
                withheld[0]["resources"]
            )

    def test_whole_config_sanitizer_leaves_prompt_only_patterns_to_the_launch_copy(
        self, monkeypatch
    ) -> None:
        from kiro_crew.platform import governance

        self._make_host_ungoverned(monkeypatch)
        ordinary_grant_check = governance.may_skip_gate_now
        monkeypatch.setattr(
            governance,
            "may_skip_gate_now",
            lambda ref: ref == "fs_read" or ordinary_grant_check(ref),
        )
        mounted: dict[str, object] = {
            "tools": ["*"],
            "allowedTools": ["*", "fs_read"],
            "mcpServers": {"kirocrew-apps": {"command": "x"}},
        }
        ordinary: dict[str, object] = {
            "tools": ["@ordinary-server"],
            "allowedTools": ["*", "@*"],
        }

        governance.sanitize_agent_config_governance(mounted, audit=False)
        governance.sanitize_agent_config_governance(ordinary, audit=False)

        assert mounted["allowedTools"] == ["*", "fs_read"]
        assert ordinary["allowedTools"] == ["*", "@*"]

    def test_kas_projection_withholds_wildcards_only_when_prompt_only_server_is_mounted(
        self, monkeypatch
    ) -> None:
        from kiro_crew.acp import kas_agents

        self._make_host_ungoverned(monkeypatch)
        wildcards = ["*", "@*", "@kirocrew-*", "web_fetch"]

        def permit_all(_ref: str) -> bool:
            return True

        mounted = kas_agents._ceiling_permitted(wildcards, "a", permit_all, mounts_prompt_only=True)
        ordinary = kas_agents._ceiling_permitted(wildcards, "a", permit_all)

        assert mounted == ["web_fetch"]
        assert ordinary == wildcards

    @pytest.mark.parametrize(
        ("servers", "tools", "expected"),
        [
            ({"kirocrew-apps": {"command": "x"}}, ["*"], True),
            ({}, ["*"], False),
            ({"kirocrew-apps": {"command": "x"}}, ["@other"], False),
        ],
    )
    def test_kas_projection_reads_the_mount_from_the_spec(
        self, monkeypatch, servers, tools, expected
    ) -> None:
        from kiro_crew.acp import kas_agents

        self._make_host_ungoverned(monkeypatch)
        seen: list[bool] = []
        real_filter = kas_agents._ceiling_permitted

        def recording_filter(allowed, agent_id, permits, *, mounts_prompt_only=False):
            seen.append(mounts_prompt_only)
            return real_filter(allowed, agent_id, permits, mounts_prompt_only=mounts_prompt_only)

        monkeypatch.setattr(kas_agents, "_ceiling_permitted", recording_filter)
        spec = {"tools": tools, "allowedTools": ["*"], "mcpServers": servers}

        kas_agents.to_client_custom_agent("a", spec, "p")

        assert seen == [expected]

    @pytest.mark.parametrize(
        ("packaged", "kept"),
        [
            (["@kirocrew-apps"], []),
            (["@kirocrew-apps/list_notes", "@other"], ["@other"]),
            (["*"], ["*"]),
            (["@*", "web_fetch"], ["@*", "web_fetch"]),
            (["@kirocrew-*"], ["@kirocrew-*"]),
        ],
    )
    def test_packaged_grant_keeps_access_but_loses_auto_approve(
        self, app_env, tmp_path, monkeypatch, packaged, kept
    ) -> None:
        from kiro_crew.apps import bridges
        from kiro_crew.apps.manifest import AppManifest

        monkeypatch.setattr(
            "kiro_crew.platform.context.current_context",
            lambda: SimpleNamespace(governance=None),
        )
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.any_configured_profile_governs",
            lambda _ref: False,
        )
        src = _make_app_source(tmp_path, name="prompted-app")
        (src / "agents" / "my-agent.json").write_text(
            json.dumps(
                {
                    "name": "my-agent",
                    "model": "auto",
                    "tools": ["@kirocrew-apps"],
                    "allowedTools": packaged,
                }
            )
        )
        manifest = AppManifest.from_json_file(src / APP_MANIFEST_FILENAME)

        bridges._register_agents("prompted-app", manifest, src)

        written = json.loads((app_env["kiro_agents"] / "prompted-app--my-agent.json").read_text())
        assert "@kirocrew-apps" in written["tools"]
        assert written["allowedTools"] == kept
        assert "kirocrew-apps" in written["mcpServers"]

    @pytest.mark.parametrize("mount_only", [True, False])
    def test_a_policy_grant_leaves_packaged_patterns_to_the_launch_copy(
        self, monkeypatch, mount_only
    ) -> None:
        from kiro_crew.apps import bridges

        self._make_host_ungoverned(monkeypatch)
        monkeypatch.setattr(bridges, "_global_mcp_specs", lambda: {})
        monkeypatch.setattr(
            bridges, "_managed_mcp_spec", lambda name: {"command": "kirocrew", "args": [name]}
        )
        agent = {"name": "a", "tools": ["fs_read"], "allowedTools": ["@*", "web_fetch"]}
        grant = {"mountOnly": True} if mount_only else {}
        out = bridges._apply_agent_mcp_policy(
            agent, "a", {"agents": {"a": {"servers": {"kirocrew-apps": grant}}}}
        )
        assert "@kirocrew-apps" in out["tools"]
        assert out["allowedTools"] == ["@*", "web_fetch"]

    def test_a_declared_server_under_a_bare_wildcard_leaves_it_for_the_launch_copy(
        self, monkeypatch
    ) -> None:
        from kiro_crew.apps import bridges

        self._make_host_ungoverned(monkeypatch)
        monkeypatch.setattr(bridges, "_global_mcp_specs", lambda: {})
        monkeypatch.setattr(
            bridges, "_managed_mcp_spec", lambda name: {"command": "kirocrew", "args": [name]}
        )
        agent = {
            "name": "a",
            "tools": ["*"],
            "allowedTools": ["*", "web_fetch"],
            "mcpServers": {"kirocrew-apps": {"command": "x"}},
        }
        out = bridges._apply_agent_mcp_policy(agent, "a", {})
        assert out["allowedTools"] == ["*", "web_fetch"]

    def test_policy_merge_honors_disabled_mcp_json(self, tmp_path, monkeypatch) -> None:
        from kiro_crew import agent
        from kiro_crew.apps import bridges

        self._make_host_ungoverned(monkeypatch)
        settings = tmp_path / "settings-mcp.json"
        settings.write_text(
            json.dumps({"mcpServers": {"kirocrew-apps": {"command": "kirocrew"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(agent, "_KIRO_MCP_JSON", settings)
        monkeypatch.setattr(bridges, "_managed_mcp_spec", lambda _name: {"command": "ordinary"})
        agent = dict(name="a", tools=["*"], allowedTools=["*"], includeMcpJson=False)
        policy = {"agents": {"a": {"servers": {"ordinary-server": {}}}}}

        out = bridges._apply_agent_mcp_policy(agent, "a", policy)

        assert "@ordinary-server" in out["tools"]
        assert "*" in out["allowedTools"]

    def test_a_match_all_grant_stands_on_an_agent_without_the_server(
        self, app_env, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.apps import bridges
        from kiro_crew.apps.manifest import AppManifest

        monkeypatch.setattr(
            "kiro_crew.platform.context.current_context",
            lambda: SimpleNamespace(governance=None),
        )
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.any_configured_profile_governs",
            lambda _ref: False,
        )
        src = _make_app_source(tmp_path, name="plain-app")
        (src / "agents" / "my-agent.json").write_text(
            json.dumps(
                {"name": "my-agent", "model": "auto", "tools": ["*"], "allowedTools": ["@*"]}
            )
        )
        manifest = AppManifest.from_json_file(src / APP_MANIFEST_FILENAME)

        bridges._register_agents("plain-app", manifest, src)

        written = json.loads((app_env["kiro_agents"] / "plain-app--my-agent.json").read_text())
        assert written["allowedTools"] == ["@*"]
