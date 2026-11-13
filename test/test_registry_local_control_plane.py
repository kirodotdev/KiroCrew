"""Registry-governed hosts: Crew's own session identity is an admin opt-in, off by default.

Under ``agent.mcp_registry_mode`` kiro-cli launches ``kirocrew-core`` and
``kirocrew-cron`` from the admin's catalog, which carries no session identity. The
per-session identity element ``kiro_control_plane_servers`` would inject is launched
as given, past that catalog, so it is mounted only when the admin's security policy
sets ``mcp_registry_local_control_plane: true`` -- and then for those two servers
alone. Off, identity-scoped calls stay refused ``identity_unattestable`` and the
refusal names the cause and the field. The field lives in ``security_policy.json``,
which the agent can neither write through its file tools nor through the sandbox.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp_shared_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _framing_undetected,
    _LoopHarness,
    _tools_call_with_caller,
)

import kiro_crew.agent as agent_mod
import kiro_crew.mcp_shared as mcp_shared
import kiro_crew.mcp_shared_policy as mcp_shared_policy
import kiro_crew.platform.governance as governance_mod
from kiro_crew.acp import session_mcp
from kiro_crew.platform.governance import (
    PlatformCompositionError,
)
from kiro_crew.platform.governance import _intersect_ceilings as intersect_ceilings
from kiro_crew.platform.governance import (
    parse_policy,
    parse_profile,
)

_FIELD = "mcp_registry_local_control_plane"


def _doc(**extra: object) -> dict:
    return {"version": 1, "boot": {}, **extra}


# ── the policy field ────────────────────────────────────────────────────


class TestPolicyField:
    def test_absent_is_off(self):
        assert parse_policy(_doc()).mcp_registry_local_control_plane is False

    @pytest.mark.parametrize("value", [True, False])
    def test_a_json_boolean_is_taken_as_written(self, value):
        assert parse_policy(_doc(**{_FIELD: value})).mcp_registry_local_control_plane is value

    @pytest.mark.parametrize("bad", ["true", 1, 0, None, [], {}])
    def test_any_other_value_fails_the_load(self, bad):
        if bad is None:
            # ``null`` is indistinguishable from absent at the JSON level.
            assert parse_policy(_doc(**{_FIELD: bad})).mcp_registry_local_control_plane is False
            return
        with pytest.raises(PlatformCompositionError, match=_FIELD):
            parse_policy(_doc(**{_FIELD: bad}))

    def test_a_profile_cannot_carry_it(self):
        with pytest.raises(PlatformCompositionError, match=_FIELD):
            parse_profile({"name": "p", _FIELD: True})

    def test_a_lower_tier_cannot_supply_it(self):
        off = parse_policy(_doc())
        on = parse_policy(_doc(**{_FIELD: True}))
        assert intersect_ceilings(off, on).mcp_registry_local_control_plane is False
        assert intersect_ceilings(on, off).mcp_registry_local_control_plane is True

    def test_the_refusal_names_the_field_the_parser_reads(self):
        assert mcp_shared_policy.REGISTRY_LOCAL_CONTROL_PLANE_FIELD == _FIELD


# ── who can write it ────────────────────────────────────────────────────


class TestOnlyTheAdminCanWriteIt:
    @pytest.mark.parametrize("prefix", ["~/.kiro/crew", "~/.kirocrew"])
    def test_the_agent_file_gate_refuses_a_write(self, prefix):
        from kiro_crew.security import is_sensitive_write_path

        assert is_sensitive_write_path(f"{prefix}/security_policy.json")

    def test_the_sandbox_mounts_the_policy_read_only(self):
        from kiro_crew import sandbox_leaves

        assert "security_policy.json" in sandbox_leaves._CREW_READONLY_LEAVES


# ── the permission read ─────────────────────────────────────────────────


class TestPermissionRead:
    def _context(self, monkeypatch, governance):
        from kiro_crew.platform import context

        monkeypatch.setattr(
            context, "current_context", lambda: SimpleNamespace(governance=governance)
        )

    def test_no_policy_is_off(self, monkeypatch):
        self._context(monkeypatch, None)
        assert session_mcp._registry_local_control_plane_permitted() is False

    def test_policy_without_the_field_is_off(self, monkeypatch):
        self._context(monkeypatch, parse_policy(_doc()))
        assert session_mcp._registry_local_control_plane_permitted() is False

    def test_policy_with_the_field_is_on(self, monkeypatch):
        self._context(monkeypatch, parse_policy(_doc(**{_FIELD: True})))
        assert session_mcp._registry_local_control_plane_permitted() is True

    def test_an_unreadable_ceiling_is_off(self, monkeypatch):
        from kiro_crew.platform import context

        def broken():
            raise RuntimeError("boom")

        monkeypatch.setattr(context, "current_context", broken)
        assert session_mcp._registry_local_control_plane_permitted() is False


# ── the mount ───────────────────────────────────────────────────────────

_GRANTED = ("kirocrew-core", "kirocrew-cron", "kirocrew-dashboard", "mochi:mochi")


def _mount(monkeypatch, tmp_path, *, registry: bool, permitted: bool) -> list[str]:
    managed = {"command": "test-crew", "args": ["mcp"]}
    marker = {"type": "registry"} if registry else {}
    spec = {
        "tools": [f"@{name}" for name in _GRANTED],
        "mcpServers": {name: {**managed, **marker} for name in _GRANTED},
    }
    monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *a, **k: spec)
    monkeypatch.setattr(session_mcp, "_global_settings", lambda **kwargs: {})
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: registry)
    monkeypatch.setattr(session_mcp, "_registry_local_control_plane_permitted", lambda: permitted)
    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", lambda name, **_kw: managed)
    monkeypatch.setattr(
        session_mcp,
        "builtin_app_identity_launches",
        lambda: {"mochi:mochi": {"command": "test-app", "args": ["serve"]}},
    )
    mount = session_mcp.kiro_control_plane_servers("kirocrew", work_dir=tmp_path)
    return [element["name"] for element in mount.elements]


class TestMount:
    def test_registry_mode_with_the_switch_off_mounts_nothing(self, monkeypatch, tmp_path):
        assert _mount(monkeypatch, tmp_path, registry=True, permitted=False) == []

    def test_registry_mode_with_the_switch_on_mounts_core_and_cron_only(
        self, monkeypatch, tmp_path
    ):
        names = _mount(monkeypatch, tmp_path, registry=True, permitted=True)
        assert names == ["kirocrew-core", "kirocrew-cron"]

    def test_outside_registry_mode_the_switch_changes_nothing(self, monkeypatch, tmp_path):
        on = _mount(monkeypatch, tmp_path, registry=False, permitted=True)
        off = _mount(monkeypatch, tmp_path, registry=False, permitted=False)
        assert on == off
        assert set(_GRANTED) <= set(off)

    def test_registry_mode_does_not_consult_the_switch_when_no_agent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(session_mcp, "_registry_mode", lambda: True)
        monkeypatch.setattr(session_mcp, "_registry_local_control_plane_permitted", lambda: True)
        assert session_mcp.kiro_control_plane_servers(None, work_dir=tmp_path).elements == []


# ── the refusal ─────────────────────────────────────────────────────────


class TestRefusalText:
    @pytest.fixture
    def registry_without_opt_in(self, monkeypatch):
        monkeypatch.setattr(agent_mod, "_mcp_registry_mode", lambda: True)
        monkeypatch.setattr(governance_mod, "registry_local_control_plane_permitted", lambda: False)

    def test_the_note_is_empty_outside_registry_mode(self, monkeypatch):
        monkeypatch.setattr(agent_mod, "_mcp_registry_mode", lambda: False)
        assert mcp_shared_policy.registry_identity_note("kirocrew-core") == ""

    @pytest.mark.parametrize("server", ["kirocrew-core", "kirocrew-cron"])
    def test_the_note_names_the_cause_and_the_exact_field(self, registry_without_opt_in, server):
        note = mcp_shared_policy.registry_identity_note(server)
        assert "registry is blocking Kiro Crew's own tools" in note
        assert f'"{_FIELD}": true' in note
        assert "security_policy.json" in note

    @pytest.mark.parametrize("server", ["kirocrew-dashboard", "mochi:mochi", "test-server"])
    def test_the_note_is_empty_on_a_server_the_field_does_not_mount(
        self, registry_without_opt_in, server
    ):
        assert mcp_shared_policy.registry_identity_note(server) == ""

    def test_the_note_is_empty_once_the_admin_has_opted_in(self, monkeypatch):
        monkeypatch.setattr(agent_mod, "_mcp_registry_mode", lambda: True)
        monkeypatch.setattr(governance_mod, "registry_local_control_plane_permitted", lambda: True)
        assert mcp_shared_policy.registry_identity_note("kirocrew-core") == ""

    @pytest.mark.parametrize(
        ("server", "registry", "noted"),
        [
            ("kirocrew-core", True, True),
            ("kirocrew-core", False, False),
            ("kirocrew-dashboard", True, False),
        ],
    )
    def test_an_unattestable_call_is_refused_with_the_note_only_where_the_field_helps(
        self, monkeypatch, server, registry, noted
    ):
        ran: list[str] = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok", server_name=server)
        monkeypatch.setattr(agent_mod, "_mcp_registry_mode", lambda: registry)
        monkeypatch.setattr(governance_mod, "registry_local_control_plane_permitted", lambda: False)
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattestable"),
        )
        try:
            harness.send(_tools_call_with_caller(61, "artifact_save", "dashboard:chat-61"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
            body = json.dumps(harness.responses[0][1])
            assert "identity_unattestable" in body
            assert (_FIELD in body) is noted
        finally:
            harness.close()
