"""Per-session mounting of the dashboard session-control server for crew members.

Pins the four seams the member-dispatch mount rides on:

- ``members.member_dispatch_session_server`` — the session-level ``mcpServers``
  element, carrying strict identity via ``KIROCREW_SESSION_KEY`` in its env.
- ``kas_agents.to_client_custom_agent(member_dispatch=True)`` — the KAS wire
  projection widening: the server joins ``tools`` and the conductor's
  approval-free dashboard verbs join the ``allowedTools`` input BEFORE the
  governance ceiling filter.
- ``AcpClient._append_member_dispatch_server`` — the claude session-array
  append, honoring the permission-surface precondition.
- ``AcpRuntime._kas_custom_agents`` / ``create_session`` threading — the member
  flag reaches the projection.

The mount is deliberately session-scoped: every test here also pins the
negative (a non-member session gains nothing), because THAT is the property the
per-session design exists for — mounting through the on-disk template would
hand session control to every session of the agent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kiro_crew.validation  # noqa: F401 - break the legacy import cycle first
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_MEMBER_DISPATCH,
)
from kiro_crew.members import (
    MEMBER_DISPATCH_SERVER,
    is_member_session_key,
    member_dispatch_session_server,
)

MEMBER_KEY = "dashboard_member-autofix"


class TestCapabilitySet:
    def test_exactly_the_wire_capable_backends(self):
        """kiro v2 reads its template from disk and exposes no per-session
        channel, so it must never be in the set: a member session on it runs as
        plain chat rather than mounted-and-refused."""
        assert ACP_BACKENDS_MEMBER_DISPATCH == frozenset({ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS})
        assert ACP_BACKEND_KIRO not in ACP_BACKENDS_MEMBER_DISPATCH


class TestMemberDispatchSessionServer:
    def test_entry_shape(self):
        entry = member_dispatch_session_server(MEMBER_KEY)
        assert entry is not None
        assert entry["name"] == MEMBER_DISPATCH_SERVER
        assert entry["type"] == "stdio"
        assert entry["command"]
        assert isinstance(entry["args"], list)

    def test_identity_env_carries_the_session_key(self):
        """The env pair IS the identity channel: the dashboard server's strict
        resolver reads ``KIROCREW_SESSION_KEY``, and the session-level param is
        the one path the KAS projection's env stripping never touches."""
        entry = member_dispatch_session_server(MEMBER_KEY)
        assert entry is not None
        assert {"name": "KIROCREW_SESSION_KEY", "value": MEMBER_KEY} in entry["env"]

    def test_unresolvable_command_degrades_to_none(self, monkeypatch):
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "_kirocrew_mcp_invocation", lambda _sub: ("", []))
        assert member_dispatch_session_server(MEMBER_KEY) is None


class TestIsMemberSessionKey:
    @pytest.mark.parametrize(
        "key,expected",
        [
            ("member-autofix", True),
            ("dashboard_member-autofix", True),
            ("dashboard:member-autofix", True),
            ("dashboard_abc123", False),
            ("dashboard:abc123", False),
            ("cron-xyz", False),
            ("", False),
            (None, False),
        ],
    )
    def test_spellings(self, key, expected):
        assert is_member_session_key(key) is expected


class TestKasMemberProjection:
    SPEC = {"tools": ["@kirocrew-core"], "allowedTools": ["@kirocrew-core"]}

    def test_tools_gains_the_dashboard_server(self):
        out = to_client_custom_agent("a", dict(self.SPEC), "p", member_dispatch=True)
        assert "@kirocrew-dashboard" in out["tools"]

    def test_default_projection_is_untouched(self):
        out = to_client_custom_agent("a", dict(self.SPEC), "p")
        assert "@kirocrew-dashboard" not in out["tools"]
        perms = out.get("permissions") or {}
        assert not any("kirocrew-dashboard" in str(v) for v in perms.values()), perms

    def test_wildcard_tools_pass_through(self):
        out = to_client_custom_agent("a", {"tools": "*"}, "p", member_dispatch=True)
        assert out["tools"] == "*"

    def test_member_grants_join_allowed_tools(self):
        """The member grant set (conductor's verbs plus the created_by-bounded
        write verbs), merged BEFORE the ceiling filter so it crosses the same
        governance gate as every other grant."""
        from kiro_crew.agent import _MEMBER_DASHBOARD_GRANTS

        out = to_client_custom_agent("a", dict(self.SPEC), "p", member_dispatch=True)
        rendered = str(out.get("permissions") or {})
        for grant in _MEMBER_DASHBOARD_GRANTS:
            verb = grant.rsplit("/", 1)[-1]
            assert verb in rendered, (verb, rendered)
        # The dispatch loop's write verbs specifically — without these the loop
        # stalls on an approval prompt at its second step.
        assert "session_send" in rendered
        assert "session_stop" in rendered

    def test_spec_is_not_mutated(self):
        spec = {"tools": ["@kirocrew-core"], "allowedTools": ["@kirocrew-core"]}
        to_client_custom_agent("a", spec, "p", member_dispatch=True)
        assert spec["tools"] == ["@kirocrew-core"]
        assert spec["allowedTools"] == ["@kirocrew-core"]


class _ClientStub:
    """The four attributes ``_append_member_dispatch_server`` reads."""

    backend = ACP_BACKEND_CLAUDE
    _session_key = MEMBER_KEY
    _claude_settings_authored = True


def _base_servers() -> list[dict]:
    return [{"name": "kirocrew-core", "command": "x", "args": [], "env": [], "type": "stdio"}]


class TestClaudeMemberAppend:
    def _run(self, stub) -> list[dict]:
        return AcpClient._append_member_dispatch_server(stub, _base_servers())

    def test_member_session_gains_the_entry(self):
        out = self._run(_ClientStub())
        assert [e["name"] for e in out][-1] == MEMBER_DISPATCH_SERVER
        assert {"name": "KIROCREW_SESSION_KEY", "value": MEMBER_KEY} in out[-1]["env"]

    def test_non_member_session_is_untouched(self):
        stub = _ClientStub()
        stub._session_key = "dashboard_abc123"
        assert self._run(stub) == _base_servers()

    def test_unowned_permission_surface_withholds(self):
        """Appending session control onto a permission surface Crew does not
        own would hand a pre-approvable file exactly the tools the mirror's
        withhold exists to keep off it."""
        stub = _ClientStub()
        stub._claude_settings_authored = False
        assert self._run(stub) == _base_servers()

    def test_kiro_backend_is_untouched(self):
        stub = _ClientStub()
        stub.backend = ACP_BACKEND_KIRO
        assert self._run(stub) == _base_servers()

    def test_same_named_entry_is_replaced_not_duplicated(self):
        stub = _ClientStub()
        servers = _base_servers() + [
            {"name": MEMBER_DISPATCH_SERVER, "command": "old", "args": [], "env": []}
        ]
        out = AcpClient._append_member_dispatch_server(stub, servers)
        matches = [e for e in out if e["name"] == MEMBER_DISPATCH_SERVER]
        assert len(matches) == 1
        assert matches[0]["command"] != "old"


class TestRuntimeMemberThreading:
    """The member flag must reach the KAS projection through the runtime."""

    @staticmethod
    def _runtime(monkeypatch, seen):
        from kiro_crew.acp import runtime as runtime_mod

        rt = object.__new__(runtime_mod.AcpRuntime)
        rt._acp_backend = ACP_BACKEND_KAS
        rt._mcp_gateway_overlay = None

        monkeypatch.setattr(runtime_mod, "ensure_agent_materialized", lambda _a: None)
        monkeypatch.setattr(runtime_mod, "kiro_agents_dir", lambda: Path("/agents"))
        monkeypatch.setattr(runtime_mod, "injection_server_names", lambda _o, _a: frozenset())

        def _capture(_dir, agent, *, stub_server_names=frozenset(), member_dispatch=False):
            seen.append(member_dispatch)
            return [{"id": agent}]

        monkeypatch.setattr(runtime_mod, "build_kas_custom_agents", _capture)
        return rt

    @pytest.mark.asyncio
    async def test_member_flag_is_forwarded(self, monkeypatch):
        seen: list[bool] = []
        rt = self._runtime(monkeypatch, seen)
        await rt._kas_custom_agents("kirocrew", member_dispatch=True)
        assert seen == [True]

    @pytest.mark.asyncio
    async def test_default_is_off(self, monkeypatch):
        seen: list[bool] = []
        rt = self._runtime(monkeypatch, seen)
        await rt._kas_custom_agents("kirocrew")
        assert seen == [False]


class TestPoolBypass:
    """A member session must never take a pooled provider.

    A pooled child was spawned with no session key on the factory's default
    backend, so a warm hit skips the member backend route AND the per-session
    mount — exactly the pair of failures this feature's e2e first surfaced.
    """

    def test_member_key_is_recognized_for_bypass(self):
        from kiro_crew.session_allocation import SessionAllocationService

        assert SessionAllocationService._is_member_key("dashboard:member-x")
        assert SessionAllocationService._is_member_key("dashboard_member-x")
        assert not SessionAllocationService._is_member_key("dashboard_plain")


class TestMemberServerJoinsSubtraction:
    """member_dispatch must union the dashboard server into the stubbed set.

    An agent spec that already declares kirocrew-dashboard (the opt-in
    assignable set) would otherwise be projected alongside the session-level
    injection — double registration, with the identity-less spec declaration
    able to shadow the member-keyed entry.
    """

    @pytest.mark.asyncio
    async def test_stubbed_set_gains_the_member_server(self, monkeypatch):
        from kiro_crew.acp import runtime as runtime_mod

        rt = object.__new__(runtime_mod.AcpRuntime)
        rt._acp_backend = ACP_BACKEND_KAS
        rt._mcp_gateway_overlay = None
        monkeypatch.setattr(runtime_mod, "ensure_agent_materialized", lambda _a: None)
        monkeypatch.setattr(runtime_mod, "kiro_agents_dir", lambda: Path("/agents"))
        monkeypatch.setattr(runtime_mod, "injection_server_names", lambda _o, _a: frozenset())
        seen: list[frozenset] = []

        def _capture(_dir, agent, *, stub_server_names=frozenset(), member_dispatch=False):
            seen.append(frozenset(stub_server_names))
            return [{"id": agent}]

        monkeypatch.setattr(runtime_mod, "build_kas_custom_agents", _capture)
        await rt._kas_custom_agents("kirocrew", member_dispatch=True)
        assert seen == [frozenset({MEMBER_DISPATCH_SERVER})]

    @pytest.mark.asyncio
    async def test_non_member_set_is_unchanged(self, monkeypatch):
        from kiro_crew.acp import runtime as runtime_mod

        rt = object.__new__(runtime_mod.AcpRuntime)
        rt._acp_backend = ACP_BACKEND_KAS
        rt._mcp_gateway_overlay = None
        monkeypatch.setattr(runtime_mod, "ensure_agent_materialized", lambda _a: None)
        monkeypatch.setattr(runtime_mod, "kiro_agents_dir", lambda: Path("/agents"))
        monkeypatch.setattr(runtime_mod, "injection_server_names", lambda _o, _a: frozenset())
        seen: list[frozenset] = []

        def _capture(_dir, agent, *, stub_server_names=frozenset(), member_dispatch=False):
            seen.append(frozenset(stub_server_names))
            return [{"id": agent}]

        monkeypatch.setattr(runtime_mod, "build_kas_custom_agents", _capture)
        await rt._kas_custom_agents("kirocrew")
        assert seen == [frozenset()]


class TestSelectProviderBackend:
    """The per-session half of the one backend-selection gate (H3/H13)."""

    def test_explicit_pick_wins(self):
        from kiro_crew.members import select_provider_backend

        assert select_provider_backend("kas", MEMBER_KEY, "claude", "") == "kas"

    def test_member_route(self):
        from kiro_crew.members import select_provider_backend

        assert select_provider_backend(None, MEMBER_KEY, "kas", "") == "kas"

    def test_non_member_gets_the_configured_default_unresolved(self):
        """The default arm passes the configured value through UNCHANGED —
        normalization already happened at config load, and re-resolving here
        would be the second check H3 forbids."""
        from kiro_crew.members import select_provider_backend

        assert select_provider_backend(None, "dashboard_abc", "kas", "") == ""

    def test_denied_member_backend_degrades_to_kiro(self):
        from kiro_crew.members import select_provider_backend

        assert select_provider_backend(None, MEMBER_KEY, "no-such-backend", "") == ""


class TestSessionHistoryWriteProtected:
    """created_by feeds authorize_target, so its storage must not be
    agent-writable — the transcript is otherwise a lower-integrity input to an
    authorization decision (forge a victim's created_by, wait one restart,
    gain send/read/stop over the victim session)."""

    def test_sessions_dir_is_write_protected(self):
        from kiro_crew.security import write_protected_home_paths

        assert any(p.endswith("/sessions") for p in write_protected_home_paths())

    def test_sessions_dir_stays_readable(self):
        """Write-protected, NOT sensitive: transcripts are the user's own
        conversations and reading them is routine."""
        from kiro_crew.security import sensitive_home_dirs

        assert not any(p.endswith("/sessions") for p in sensitive_home_dirs())
