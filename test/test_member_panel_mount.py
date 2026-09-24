"""Pins the seams the crew-panel member mount rides on.

The sibling of ``test_member_dispatch_mount.py``, and the same five seams:

- ``members.member_panel_session_server`` -- the session-level ``mcpServers``
  element, carrying strict identity via ``KIROCREW_SESSION_KEY`` plus the
  gateway's bound port, through the one writer both member mounts share.
- ``kas_agents.to_client_custom_agent(crew_panel=True)`` -- the KAS wire
  projection widening: the server joins ``tools`` and the panel verbs join the
  ``allowedTools`` input BEFORE the governance ceiling filter.
- ``AcpClient._append_member_panel_server`` -- the claude session-array append,
  honoring the array-level preconditions its dispatch sibling documents.
- ``members.crew_panel_enabled`` -- the operator ceiling, fail-closed.
- the premise: ``kirocrew-panel`` is ``opt_in``, so no spec emits it and this
  mount is the only path to it.

Every branch is pinned in both directions, because the negative is what the
per-session design exists for: mounting through the on-disk template would hand
a panel to every session of the agent.
"""

from __future__ import annotations

from typing import Any

import pytest

import kiro_crew.validation  # noqa: F401 - break the legacy import cycle first
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
)
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.members import (
    MEMBER_DISPATCH_SERVER,
    MEMBER_PANEL_SERVER,
    crew_panel_enabled,
    member_dispatch_session_server,
    member_panel_session_server,
)

MEMBER_KEY = "dashboard_member-autofix"


def _base_servers() -> list[dict]:
    return [{"name": "kirocrew-core", "command": "x", "args": [], "env": [], "type": "stdio"}]


class TestThePremise:
    """Why this mount has to exist at all.

    The panel server is ``opt_in``, so no spec emits it, and the Capabilities
    editor builds its list from CONFIGURED connections rather than from
    host-managed opt-in servers -- so the only remaining grant surface is a
    hand-typed custom connection colliding with the managed name. If either half
    of that premise changes, this feature is the wrong shape and this test says so.
    """

    def test_the_panel_server_is_opt_in(self):
        from kiro_crew.agent import _MANAGED_MCP_SERVERS

        assert _MANAGED_MCP_SERVERS[MEMBER_PANEL_SERVER].get("opt_in") is True

    def test_it_is_in_the_opt_in_managed_set(self):
        from kiro_crew.mcp_cleanup import ALWAYS_ON_BIN_MCP_SERVERS, OPT_IN_BIN_MCP_SERVERS

        assert MEMBER_PANEL_SERVER in OPT_IN_BIN_MCP_SERVERS
        assert MEMBER_PANEL_SERVER not in ALWAYS_ON_BIN_MCP_SERVERS

    def test_identity_travels_to_it(self):
        """A granted server that carries no session identity comes up
        present-but-unusable: it refuses every call as ``identity_unattested``."""
        from kiro_crew.acp.session_mcp import IDENTITY_BOUND_SERVERS

        assert MEMBER_PANEL_SERVER in IDENTITY_BOUND_SERVERS


class TestPanelGrantSet:
    def test_every_grant_names_the_panel_server(self):
        from kiro_crew.agent import _MEMBER_PANEL_GRANTS

        assert _MEMBER_PANEL_GRANTS
        for grant in _MEMBER_PANEL_GRANTS:
            assert grant.startswith(f"@{MEMBER_PANEL_SERVER}/"), grant

    def test_the_grant_set_covers_the_server_surface(self):
        """Derived from the server's own registration rather than restated, so a
        tool added to ``kirocrew-panel`` later cannot be silently ungranted while
        this file still passes."""
        from kiro_crew.agent import _MEMBER_PANEL_GRANTS
        from kiro_crew.mcp_panel import _tool_definitions

        registered = {t["name"] for t in _tool_definitions()}
        granted = {g.rsplit("/", 1)[-1] for g in _MEMBER_PANEL_GRANTS}
        assert granted == registered, (granted, registered)

    def test_publish_is_approval_free(self):
        """The decision this feature makes, pinned so a later edit is a deliberate
        one: ``panel_publish`` writes the CALLING crew's own panel, takes no crew
        or session argument, and refuses a subagent, so it is granted rather than
        prompted -- an approval prompt would stall the unattended cycle the drawer
        is watched during."""
        from kiro_crew.agent import _MEMBER_PANEL_GRANTS

        assert f"@{MEMBER_PANEL_SERVER}/panel_publish" in _MEMBER_PANEL_GRANTS

    def test_the_server_declares_no_auto_approve(self):
        """The grant travels as ``allowedTools`` through the governance ceiling, so
        the server entry itself must still carry no ``autoApprove`` key -- that key
        is resolved inside the host and skips the ceiling entirely."""
        from kiro_crew.agent import _MANAGED_MCP_SERVERS

        assert "autoApprove" not in _MANAGED_MCP_SERVERS[MEMBER_PANEL_SERVER]


class TestKasPanelProjection:
    SPEC: dict[str, Any] = {"tools": ["@kirocrew-core"], "allowedTools": ["@kirocrew-core"]}

    def test_tools_gains_the_panel_server(self):
        out = to_client_custom_agent("a", dict(self.SPEC), "p", crew_panel=True)
        assert f"@{MEMBER_PANEL_SERVER}" in out["tools"]

    def test_flag_off_projects_nothing(self):
        out = to_client_custom_agent("a", dict(self.SPEC), "p", crew_panel=False)
        assert f"@{MEMBER_PANEL_SERVER}" not in out["tools"]
        perms = out.get("permissions") or {}
        assert not any(MEMBER_PANEL_SERVER in str(v) for v in perms.values()), perms

    def test_default_projection_is_untouched(self):
        out = to_client_custom_agent("a", dict(self.SPEC), "p")
        assert f"@{MEMBER_PANEL_SERVER}" not in out["tools"]
        perms = out.get("permissions") or {}
        assert not any(MEMBER_PANEL_SERVER in str(v) for v in perms.values()), perms

    def test_panel_grants_join_allowed_tools(self):
        from kiro_crew.agent import _MEMBER_PANEL_GRANTS

        out = to_client_custom_agent("a", dict(self.SPEC), "p", crew_panel=True)
        rendered = str(out.get("permissions") or {})
        for grant in _MEMBER_PANEL_GRANTS:
            verb = grant.rsplit("/", 1)[-1]
            assert verb in rendered, (verb, rendered)

    def test_wildcard_tools_pass_through(self):
        out = to_client_custom_agent("a", {"tools": "*"}, "p", crew_panel=True)
        assert out["tools"] == "*"

    def test_spec_is_not_mutated(self):
        spec = {"tools": ["@kirocrew-core"], "allowedTools": ["@kirocrew-core"]}
        to_client_custom_agent("a", spec, "p", crew_panel=True)
        assert spec["tools"] == ["@kirocrew-core"]
        assert spec["allowedTools"] == ["@kirocrew-core"]

    def test_the_two_capabilities_are_independent(self):
        """Two flags rather than one, because the two servers are assigned
        separately and withdrawn by separate operator switches."""
        panel_only = to_client_custom_agent("a", dict(self.SPEC), "p", crew_panel=True)
        assert f"@{MEMBER_PANEL_SERVER}" in panel_only["tools"]
        assert f"@{MEMBER_DISPATCH_SERVER}" not in panel_only["tools"]

        dispatch_only = to_client_custom_agent("a", dict(self.SPEC), "p", member_dispatch=True)
        assert f"@{MEMBER_DISPATCH_SERVER}" in dispatch_only["tools"]
        assert f"@{MEMBER_PANEL_SERVER}" not in dispatch_only["tools"]

        both = to_client_custom_agent(
            "a", dict(self.SPEC), "p", member_dispatch=True, crew_panel=True
        )
        assert f"@{MEMBER_PANEL_SERVER}" in both["tools"]
        assert f"@{MEMBER_DISPATCH_SERVER}" in both["tools"]
        rendered = str(both.get("permissions") or {})
        assert "panel_publish" in rendered
        assert "session_send" in rendered


class TestOneElementWriter:
    """Both member mounts are composed by one writer, so neither can later be
    built without the home override, the identity or the bound port."""

    def _keys(self, entry: dict) -> set[str]:
        return {pair["name"] for pair in entry["env"]}

    def test_the_two_entries_agree_on_env(self):
        panel = member_panel_session_server(MEMBER_KEY, "t" * 64)
        dispatch = member_dispatch_session_server(MEMBER_KEY, "t" * 64)
        assert panel is not None and dispatch is not None
        assert self._keys(panel) == self._keys(dispatch)

    def test_they_differ_only_in_name_and_invocation(self):
        panel = member_panel_session_server(MEMBER_KEY)
        dispatch = member_dispatch_session_server(MEMBER_KEY)
        assert panel is not None and dispatch is not None
        assert panel["name"] == MEMBER_PANEL_SERVER
        assert dispatch["name"] == MEMBER_DISPATCH_SERVER
        assert panel["args"] != dispatch["args"]
        assert panel["type"] == dispatch["type"] == "stdio"

    def test_the_element_carries_this_session(self):
        entry = member_panel_session_server(MEMBER_KEY, "t" * 64)
        assert entry is not None
        assert {"name": "KIROCREW_SESSION_KEY", "value": MEMBER_KEY} in entry["env"]
        assert {"name": STUB_SESSION_TOKEN_ENV, "value": "t" * 64} in entry["env"]

    def test_the_token_is_optional(self):
        entry = member_panel_session_server(MEMBER_KEY)
        assert entry is not None
        assert STUB_SESSION_TOKEN_ENV not in self._keys(entry)


class TestCrewPanelCeiling:
    """``agent.crew_panel``, fail-closed in both directions the ceiling can lose
    the operator's value."""

    def _config(self, monkeypatch, *, value: bool, degraded: set[str] | None = None):
        from types import SimpleNamespace

        from kiro_crew.config import loader as loader_mod

        fake = SimpleNamespace(
            agent=SimpleNamespace(crew_panel=value),
            degraded_sections=frozenset(degraded or set()),
        )
        monkeypatch.setattr(
            loader_mod.KiroCrewConfig, "load", classmethod(lambda cls: fake), raising=True
        )

    def test_default_is_on(self):
        from kiro_crew.config.sections import AgentConfig

        assert AgentConfig().crew_panel is True

    def test_true_is_read_as_granted(self, monkeypatch):
        self._config(monkeypatch, value=True)
        assert crew_panel_enabled() is True

    def test_false_withdraws_the_grant(self, monkeypatch):
        self._config(monkeypatch, value=False)
        assert crew_panel_enabled() is False

    def test_a_degraded_agent_section_withdraws_the_grant(self, monkeypatch):
        """``load()`` coerces a malformed section away and falls back to the
        PERMISSIVE default, so trusting that default would be a fail-open."""
        self._config(monkeypatch, value=True, degraded={"agent"})
        assert crew_panel_enabled() is False

    def test_a_raising_read_withdraws_the_grant(self, monkeypatch):
        from kiro_crew.config import loader as loader_mod

        def _boom(cls):
            raise OSError("config unreadable")

        monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", classmethod(_boom), raising=True)
        assert crew_panel_enabled() is False


class TestTheSwitchReachesALiveSession:
    """The withdrawal is immediate, not mount-time only.

    A mount answers for a session being ESTABLISHED. A member whose session was
    already running when the switch flipped would keep the grant until that
    session ended, and ``agent.crew_panel``'s own description promises the
    withdrawal reaches every member at once. The two sibling switches in this
    subsystem keep that promise by reading at the gate, and so does this one.
    """

    def test_the_publish_gate_reads_the_switch(self):
        import inspect

        from kiro_crew.dashboard.handlers import agent_panel as handler

        source = inspect.getsource(handler._resolve_publishing_crew)
        assert "crew_panel_enabled" in source

    def test_both_routes_pass_through_that_gate(self):
        """One gate, so neither verb can be left reachable while the other is not."""
        import inspect

        from kiro_crew.dashboard.handlers import agent_panel as handler

        for route in (handler.api_agent_panel_publish, handler.api_agent_panel_templates):
            assert "_resolve_publishing_crew" in inspect.getsource(route), route.__name__

    def test_the_refusal_names_the_switch(self):
        import inspect

        from kiro_crew.dashboard.handlers import agent_panel as handler

        source = inspect.getsource(handler._resolve_publishing_crew)
        assert "crew_panel_disabled" in source


class _ClientStub:
    """The attributes ``_append_member_panel_server`` reads."""

    backend = ACP_BACKEND_CLAUDE
    _session_key = MEMBER_KEY
    _claude_settings_authored = True
    _stub_session_token = "e" * 64
    # The real predicate, not a double: it is the thing that decides whether a
    # restricted server may be re-added, and a stubbed answer would test the stub.
    _withhold_is_the_only_deny_channel = AcpClient._withhold_is_the_only_deny_channel
    _member_mount_withheld = AcpClient._member_mount_withheld


@pytest.fixture
def panel_granted(monkeypatch):
    """The ceiling open, so these tests judge the ARRAY-level preconditions.

    Patched on ``kiro_crew.members`` because the append imports it from there at
    call time, which is also what keeps the config read off this module's import.
    """
    import kiro_crew.members as members_mod

    monkeypatch.setattr(members_mod, "crew_panel_enabled", lambda: True)
    return members_mod


class TestClaudePanelAppend:
    def _run(self, stub, restricted=frozenset(), disabled=frozenset()) -> list[dict]:
        return AcpClient._append_member_panel_server(stub, _base_servers(), restricted, disabled)

    def test_member_session_gains_the_entry(self, panel_granted):
        out = self._run(_ClientStub())
        assert [e["name"] for e in out][-1] == MEMBER_PANEL_SERVER
        assert {"name": "KIROCREW_SESSION_KEY", "value": MEMBER_KEY} in out[-1]["env"]
        assert {
            "name": STUB_SESSION_TOKEN_ENV,
            "value": _ClientStub._stub_session_token,
        } in out[
            -1
        ]["env"]

    def test_non_member_session_is_untouched(self, panel_granted):
        stub = _ClientStub()
        stub._session_key = "dashboard_abc123"
        assert self._run(stub) == _base_servers()

    def test_the_switch_off_withholds(self, monkeypatch):
        import kiro_crew.members as members_mod

        monkeypatch.setattr(members_mod, "crew_panel_enabled", lambda: False)
        assert self._run(_ClientStub()) == _base_servers()

    def test_unowned_permission_surface_withholds(self, panel_granted):
        stub = _ClientStub()
        stub._claude_settings_authored = False
        assert self._run(stub) == _base_servers()

    def test_kiro_backend_is_untouched(self, panel_granted):
        stub = _ClientStub()
        stub.backend = ACP_BACKEND_KIRO
        assert self._run(stub) == _base_servers()

    def test_same_named_entry_is_replaced_not_duplicated(self, panel_granted):
        stub = _ClientStub()
        servers = _base_servers() + [
            {"name": MEMBER_PANEL_SERVER, "command": "old", "args": [], "env": []}
        ]
        out = AcpClient._append_member_panel_server(stub, servers)
        matches = [e for e in out if e["name"] == MEMBER_PANEL_SERVER]
        assert len(matches) == 1
        assert matches[0]["command"] != "old"

    @pytest.mark.parametrize(
        "backend",
        [ACP_BACKEND_OPENCODE, ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE, ACP_BACKEND_GOOSE],
    )
    def test_a_disabled_panel_server_is_never_mounted(self, backend, panel_granted):
        """``disabled`` has no per-call form, so no backend is exempt."""
        stub = _ClientStub()
        stub.backend = backend
        assert self._run(stub, disabled=frozenset({MEMBER_PANEL_SERVER})) == _base_servers()

    @pytest.mark.parametrize("backend", [ACP_BACKEND_OPENCODE, ACP_BACKEND_GOOSE])
    def test_a_whole_server_deny_backend_withholds_a_restricted_server(
        self, backend, panel_granted
    ):
        stub = _ClientStub()
        stub.backend = backend
        assert self._run(stub, restricted=frozenset({MEMBER_PANEL_SERVER})) == _base_servers()

    @pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE])
    def test_a_backend_with_a_second_deny_channel_keeps_its_mount(self, backend, panel_granted):
        stub = _ClientStub()
        stub.backend = backend
        out = self._run(stub, restricted=frozenset({MEMBER_PANEL_SERVER}))
        assert [e["name"] for e in out][-1] == MEMBER_PANEL_SERVER

    def test_a_restriction_on_another_server_says_nothing(self, panel_granted):
        stub = _ClientStub()
        stub.backend = ACP_BACKEND_OPENCODE
        out = self._run(stub, restricted=frozenset({MEMBER_DISPATCH_SERVER}))
        assert [e["name"] for e in out][-1] == MEMBER_PANEL_SERVER

    def test_the_dispatch_server_is_withheld_on_its_own_terms(self, panel_granted):
        """Each mount answers for itself: disabling session control must not take
        the drawer with it, and the operator's switch for the drawer is
        ``agent.crew_panel``."""
        stub = _ClientStub()
        out = AcpClient._append_member_panel_server(
            stub, _base_servers(), frozenset(), frozenset({MEMBER_DISPATCH_SERVER})
        )
        assert [e["name"] for e in out][-1] == MEMBER_PANEL_SERVER


class TestPanelBackendSetIsItsOwnDecision:
    """H6: membership is opted into PER CAPABILITY.

    Session-control support establishes nothing about the panel, so the panel gate
    must read a set argued for the panel. The two memberships coincide today; the
    DECISION must not, which is what makes a backend added to one set stay out of
    the other until someone argues it.
    """

    def test_the_set_exists_and_is_distinct(self):
        from kiro_crew.acp.types import ACP_BACKENDS_MEMBER_DISPATCH, ACP_BACKENDS_MEMBER_PANEL

        assert ACP_BACKENDS_MEMBER_PANEL is not ACP_BACKENDS_MEMBER_DISPATCH

    def test_the_client_gate_reads_the_panel_set(self):
        """Read off the source, because the two sets are equal today: an equality
        assertion could not tell which one the gate consults."""
        import inspect

        source = inspect.getsource(AcpClient._append_member_panel_server)
        assert "ACP_BACKENDS_MEMBER_PANEL" in source
        assert "ACP_BACKENDS_MEMBER_DISPATCH" not in source

    def test_kiro_is_excluded(self):
        from kiro_crew.acp.types import ACP_BACKENDS_MEMBER_PANEL

        assert ACP_BACKEND_KIRO not in ACP_BACKENDS_MEMBER_PANEL

    @pytest.mark.parametrize(
        "backend",
        [ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS, ACP_BACKEND_CODEX, ACP_BACKEND_OPENCODE],
    )
    def test_the_argued_members_are_present(self, backend):
        from kiro_crew.acp.types import ACP_BACKENDS_MEMBER_PANEL

        assert backend in ACP_BACKENDS_MEMBER_PANEL


class TestRuntimePanelMountAsksThePerToolSwitch:
    """A ``disabledTools`` naming a panel verb withholds the mount AND the grant.

    The runtime mount is the only path KAS takes, and the grant that follows it
    puts ``panel_publish`` into ``allowedTools`` approval-free. KAS has no wire
    slot for hooks, so there is no later point at which the switched-off verb
    could be refused: withholding is the only faithful answer.
    """

    @staticmethod
    def _runtime(monkeypatch, *, narrowed):
        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.acp.types import ACP_BACKEND_KAS as _KAS

        rt = object.__new__(runtime_mod.AcpRuntime)
        rt._acp_backend = _KAS
        monkeypatch.setattr(runtime_mod, "session_mcp_server_is_disabled", lambda *a, **k: False)
        monkeypatch.setattr(
            runtime_mod,
            "session_mcp_disabled_tools",
            lambda *a, **k: frozenset(narrowed),
        )
        import kiro_crew.members as members_mod

        monkeypatch.setattr(members_mod, "crew_panel_enabled", lambda: True)
        return rt

    def _run(self, rt):
        import asyncio

        return asyncio.run(
            rt._mount_member_panel(
                _base_servers(),
                member_session_key=MEMBER_KEY,
                agent_name="kirocrew",
                session_work_dir="/work",
                stub_token="t" * 64,
            )
        )

    def test_a_narrowed_panel_verb_withholds_mount_and_grant(self, monkeypatch):
        rt = self._runtime(monkeypatch, narrowed={(MEMBER_PANEL_SERVER, "panel_publish")})
        servers, granted = self._run(rt)
        assert servers == _base_servers()
        assert granted is False

    def test_an_unnarrowed_server_mounts_and_grants(self, monkeypatch):
        rt = self._runtime(monkeypatch, narrowed=set())
        servers, granted = self._run(rt)
        assert [e["name"] for e in servers][-1] == MEMBER_PANEL_SERVER
        assert granted is True

    def test_a_narrowing_on_another_server_says_nothing(self, monkeypatch):
        rt = self._runtime(monkeypatch, narrowed={(MEMBER_DISPATCH_SERVER, "session_send")})
        servers, granted = self._run(rt)
        assert [e["name"] for e in servers][-1] == MEMBER_PANEL_SERVER
        assert granted is True


class TestPanelCompositionStaysInsideTheMemberBranch:
    """H13: the Kiro construction path gains no conditional and no awaited step.

    A kiro session can never carry a member key, so the composition must not sit
    on the path it walks. Asserted on the call site's INDENTATION rather than on
    behaviour, because the property is about what the non-member path traverses
    at all, and a behavioural probe of "did not traverse" cannot distinguish an
    early return from an absent call.
    """

    @pytest.mark.parametrize("method", ["create_session", "load_session"])
    def test_every_call_site_is_nested_under_a_branch(self, method):
        import inspect
        import textwrap

        from kiro_crew.acp.runtime import AcpRuntime

        source = textwrap.dedent(inspect.getsource(getattr(AcpRuntime, method)))
        sites = [
            line
            for line in source.splitlines()
            if "_mount_member_panel(" in line and "def " not in line
        ]
        assert sites, f"{method} must compose the panel somewhere"
        for line in sites:
            indent = len(line) - len(line.lstrip())
            # A statement at the method body's own level is 4 after dedent; anything
            # deeper is inside a block.
            assert indent > 4, (method, indent, line)

    @pytest.mark.parametrize("method", ["create_session", "load_session"])
    def test_the_non_member_answer_needs_no_await(self, method):
        """``panel_mounted`` is set to False by a plain assignment, so a session
        with no member key reaches no part of the composition."""
        import inspect

        from kiro_crew.acp.runtime import AcpRuntime

        source = inspect.getsource(getattr(AcpRuntime, method))
        assert "panel_mounted = False" in source
        before = source.split("panel_mounted = False", 1)[0]
        assert "_mount_member_panel(" not in before
