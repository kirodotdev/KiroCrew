"""OpenCode projection candidates never stand in for execution-gate proof."""

from __future__ import annotations

import copy
import json
import sys
from unittest.mock import Mock

import pytest

from kiro_crew.acp import session_mcp
from kiro_crew.acp_backends import ACP_BACKEND_OPENCODE
from kiro_crew.providers.mirrors import Concern, Disposition, mirror_for
from kiro_crew.providers.mirrors import opencode as opencode_mirror
from kiro_crew.providers.mirrors.opencode import (
    OpenCodeMirror,
    candidate_session_servers,
    project_managed_servers,
)

_CORE = "kirocrew-core"
_CRON = "kirocrew-cron"


@pytest.fixture
def managed_entries():
    return {
        _CORE: {"command": sys.executable, "args": ["mcp-core"], "env": {"PORT": 1234}},
        _CRON: {"command": sys.executable, "args": ["mcp-cron"]},
    }


@pytest.fixture
def full_spec():
    return {
        "name": "test-agent",
        "tools": [f"@{_CORE}", f"@{_CRON}"],
        "mcpServers": {
            _CORE: {"command": "stale-core", "args": ["stale"]},
            _CRON: {"command": "stale-cron"},
        },
    }


class TestDormantMirror:
    def test_registry_resolves_the_declared_mirror(self):
        mirror = mirror_for(ACP_BACKEND_OPENCODE)
        assert isinstance(mirror, OpenCodeMirror)
        assert mirror.backend == ACP_BACKEND_OPENCODE

    @pytest.mark.parametrize("ownership", [False, True, "true", object()])
    def test_a_caller_cannot_certify_enforcement_with_a_flag(self, monkeypatch, ownership):
        candidate = Mock(side_effect=AssertionError("projection must stay disconnected"))
        monkeypatch.setattr(opencode_mirror, "candidate_session_servers", candidate)
        assert OpenCodeMirror().session_params(
            "test-agent", permission_surface_owned=ownership, enforcement_verified=True
        ) == {"mcpServers": []}
        candidate.assert_not_called()

    def test_the_default_wire_face_performs_no_spec_read(self, monkeypatch):
        reader = Mock(side_effect=AssertionError("dormant mirror must not read the host"))
        monkeypatch.setattr(session_mcp, "_agent_spec_for", reader)
        assert OpenCodeMirror().session_params("test-agent") == {"mcpServers": []}
        reader.assert_not_called()

    def test_the_file_face_creates_no_native_configuration(self, tmp_path):
        OpenCodeMirror().write_files("test-agent", work_dir=tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_every_concern_is_honest_about_the_gate_gap(self):
        rulings = OpenCodeMirror().rulings()
        assert set(rulings) == set(Concern)
        assert rulings[Concern.PERMISSION_MODE].disposition is Disposition.NO_CHANNEL
        assert "execution" in rulings[Concern.PERMISSION_MODE].channel
        for concern in (Concern.MCP_SERVERS, Concern.TOOL_ALLOWLIST, Concern.DENIED_TOOLS):
            assert rulings[concern].disposition is Disposition.WITHHELD
        assert rulings[Concern.AUTO_APPROVE].disposition is Disposition.WITHHELD


class TestManagedProjection:
    def test_stdio_descriptors_are_fresh_portable_and_not_preapproved(
        self, full_spec, managed_entries
    ):
        full_spec["mcpServers"][_CORE].update(
            {
                "env": {"SECRET": "must-not-copy"},
                "url": "https://example.invalid/override",
                "autoApprove": ["*"],
                "timeout": 1,
                "type": "registry",
            }
        )
        result = project_managed_servers(full_spec, managed_entries)
        assert result == [
            {
                "name": _CORE,
                "command": sys.executable,
                "args": ["mcp-core"],
                "env": [{"name": "PORT", "value": "1234"}],
            },
            {"name": _CRON, "command": sys.executable, "args": ["mcp-cron"], "env": []},
        ]

    def test_user_app_computer_and_opt_in_servers_are_never_projected(
        self, full_spec, managed_entries
    ):
        excluded = ("user-added", "app:tool", "kirocrew-computer", "kirocrew-work")
        for name in excluded:
            full_spec["mcpServers"][name] = {"command": "untrusted"}
            managed_entries[name] = {"command": "untrusted"}
        full_spec["tools"] = ["*"]
        assert [entry["name"] for entry in project_managed_servers(full_spec, managed_entries)] == [
            _CORE,
            _CRON,
        ]

    @pytest.mark.parametrize(
        "tools, expected",
        [
            ([f"@{_CORE}"], [_CORE]),
            ([f"@{_CORE}/one_tool"], []),
            (["*"], [_CORE, _CRON]),
            (["@*"], []),
            (["@builtin"], []),
            ([], []),
            (None, []),
            (f"@{_CORE}", []),
            ({_CORE: True}, []),
            ([None, False, 17, {}], []),
        ],
    )
    def test_only_whole_server_grants_are_representable(
        self, full_spec, managed_entries, tools, expected
    ):
        full_spec["tools"] = tools
        assert [entry["name"] for entry in project_managed_servers(full_spec, managed_entries)] == (
            expected
        )

    def test_missing_tools_is_not_a_default_grant(self, full_spec, managed_entries):
        del full_spec["tools"]
        assert project_managed_servers(full_spec, managed_entries) == []

    @pytest.mark.parametrize("disabled_tools", [["one_tool"], "one_tool", {}, None, False, 0])
    def test_disabled_or_malformed_tool_filter_withholds_the_whole_server(
        self, full_spec, managed_entries, disabled_tools
    ):
        full_spec["mcpServers"][_CORE]["disabledTools"] = disabled_tools
        assert [entry["name"] for entry in project_managed_servers(full_spec, managed_entries)] == [
            _CRON
        ]

    def test_explicit_empty_disabled_tools_does_not_narrow(self, full_spec, managed_entries):
        full_spec["mcpServers"][_CORE]["disabledTools"] = []
        assert [entry["name"] for entry in project_managed_servers(full_spec, managed_entries)] == [
            _CORE,
            _CRON,
        ]

    @pytest.mark.parametrize(
        "flags",
        [{"disabled": True}, {"enabled": False}, {"disabled": "false"}, {"enabled": None}],
    )
    def test_disabled_and_ambiguous_enabled_state_is_not_dropped(
        self, full_spec, managed_entries, flags
    ):
        full_spec["mcpServers"][_CORE].update(flags)
        assert [entry["name"] for entry in project_managed_servers(full_spec, managed_entries)] == [
            _CRON
        ]

    @pytest.mark.parametrize("spec", [None, [], "test-agent", {}, {"tools": ["*"]}])
    def test_no_valid_spec_means_no_candidates(self, spec, managed_entries):
        assert project_managed_servers(spec, managed_entries) == []

    def test_missing_declared_entry_is_not_recreated(self, full_spec, managed_entries):
        del full_spec["mcpServers"][_CORE]
        assert [entry["name"] for entry in project_managed_servers(full_spec, managed_entries)] == [
            _CRON
        ]

    @pytest.mark.parametrize(
        "authority_entry",
        [None, {}, {"command": ""}, {"url": "https://example.invalid/remote"}],
    )
    def test_unavailable_or_non_stdio_authority_cannot_fall_back_to_spec_commands(
        self, full_spec, managed_entries, authority_entry
    ):
        managed_entries[_CORE] = authority_entry
        assert [entry["name"] for entry in project_managed_servers(full_spec, managed_entries)] == [
            _CRON
        ]

    def test_projection_does_not_mutate_its_inputs(self, full_spec, managed_entries):
        before = copy.deepcopy((full_spec, managed_entries))
        result = project_managed_servers(full_spec, managed_entries)
        result[0]["args"].append("changed")
        result[0]["env"][0]["value"] = "changed"
        assert (full_spec, managed_entries) == before


class TestWireNameCollisions:
    def test_normalization_preserves_portable_characters_and_replaces_others(self):
        assert opencode_mirror.opencode_mcp_wire_name("Core-tools_1:extra/path") == (
            "Core-tools_1_extra_path"
        )

    @pytest.mark.parametrize("reserved", [_CORE, f"{_CORE}.nested", f"{_CORE}/nested"])
    def test_another_injection_reserves_exact_or_overlapping_names(
        self, full_spec, managed_entries, reserved
    ):
        result = project_managed_servers(
            full_spec, managed_entries, reserved_server_names=[reserved]
        )
        assert [entry["name"] for entry in result] == [_CRON]

    @pytest.mark.parametrize(
        "names", [("core:tools", "core/tools"), ("core", "core_more"), ("core", "core")]
    )
    def test_both_sides_of_a_wire_collision_are_withheld(self, names):
        servers = [{"name": name} for name in (*names, "unrelated")]
        assert opencode_mirror._without_ambiguous_names(servers, ()) == [{"name": "unrelated"}]


class TestSharedSpecResolution:
    def test_the_candidate_reuses_the_shared_reader_and_managed_authority(
        self, monkeypatch, tmp_path, full_spec, managed_entries
    ):
        reader = Mock(return_value=full_spec)
        authority = Mock(side_effect=managed_entries.get)
        monkeypatch.setattr(session_mcp, "_agent_spec_for", reader)
        monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", authority)
        result = candidate_session_servers("test-agent", work_dir=tmp_path)
        reader.assert_called_once_with("test-agent", tmp_path)
        assert {call.args[0] for call in authority.call_args_list} == {_CORE, _CRON}
        assert [entry["name"] for entry in result] == [_CORE, _CRON]

    @pytest.mark.parametrize("agent", [None, ""])
    def test_no_agent_does_not_read_or_materialize_anything(self, monkeypatch, agent):
        reader = Mock(side_effect=AssertionError("no agent was requested"))
        monkeypatch.setattr(session_mcp, "_agent_spec_for", reader)
        assert candidate_session_servers(agent) == []
        reader.assert_not_called()

    def test_a_rejected_spec_does_not_resolve_invocations(self, monkeypatch):
        monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *_args: None)
        authority = Mock(side_effect=AssertionError("a rejected spec cannot expose tools"))
        monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", authority)
        assert candidate_session_servers("test-agent") == []
        authority.assert_not_called()

    def test_a_real_project_spec_keeps_its_restriction_ahead_of_the_user_spec(
        self, monkeypatch, tmp_path, full_spec, managed_entries
    ):
        user_spec = tmp_path / "user.json"
        user_spec.write_text(json.dumps(full_spec), encoding="utf-8")
        project = tmp_path / "checkout"
        project_agents = project / ".kiro" / "agents"
        project_agents.mkdir(parents=True)
        full_spec["tools"] = [f"@{_CORE}"]
        full_spec["mcpServers"][_CORE]["disabledTools"] = ["one_tool"]
        (project_agents / "other-file-name.json").write_text(
            json.dumps(full_spec), encoding="utf-8"
        )
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro-home"))
        monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _agent: True)
        monkeypatch.setattr(session_mcp, "agent_spec_path", lambda _agent: user_spec)
        monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", managed_entries.get)

        # The user-level positive control proves that the empty project result
        # comes from its nearer restriction, not from an unresolvable fixture.
        assert len(candidate_session_servers("test-agent")) == 2
        assert candidate_session_servers("test-agent", work_dir=project) == []

    @pytest.mark.parametrize("raw", [b"\xff", b"[]", b"{malformed"])
    def test_the_shared_bounded_reader_rejects_unusable_content(self, monkeypatch, tmp_path, raw):
        path = tmp_path / "broken.json"
        path.write_bytes(raw)
        monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _agent: True)
        monkeypatch.setattr(session_mcp, "agent_spec_path", lambda _agent: path)
        assert candidate_session_servers("test-agent") == []
