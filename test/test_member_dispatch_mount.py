"""Per-session mounting of the dashboard session-control server for crew members.

Pins the four seams the member-dispatch mount rides on:

- ``members.member_dispatch_session_server`` — the session-level ``mcpServers``
  element, carrying strict identity via ``KIROCREW_SESSION_KEY`` in its env
  plus the gateway's bound port and its ``KIROCREW_HOME`` override, both of
  which the from-scratch env would otherwise drop.
- ``kas_agents.to_client_custom_agent(member_dispatch=True)`` — the KAS wire
  projection widening: the server joins ``tools`` and the conductor's
  approval-free dashboard verbs join the ``allowedTools`` input BEFORE the
  governance ceiling filter.
- ``AcpClient._append_member_dispatch_server`` — the claude session-array append,
  honoring the permission-surface precondition.
- ``AcpProvider._member_session_key`` — the set membership that decides whether the
  runtime's session/new and session/load paths mount anything at all, which is the
  only route a codex member session has.
- ``AcpRuntime._kas_custom_agents`` / ``create_session`` threading — the member
  flag reaches the projection.

The mount is deliberately session-scoped: every test here also pins the
negative (a non-member session gains nothing), because THAT is the property the
per-session design exists for — mounting through the on-disk template would
hand session control to every session of the agent.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.validation  # noqa: F401 - break the legacy import cycle first
from kiro_crew import acp_tool_gate
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_MEMBER_DISPATCH,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
)
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.members import (
    MEMBER_DISPATCH_SERVER,
    is_member_session_key,
    member_dispatch_session_server,
)
from kiro_crew.providers.acp import AcpProvider

MEMBER_KEY = "dashboard_member-autofix"


@pytest.fixture
def goose_agents_dir(tmp_path, monkeypatch):
    """A goose agent spec on disk, declaring the control plane AND the dashboard server.

    The documented seam for driving a mirror over a spec: materialization would
    otherwise rebuild the managed default and overwrite the spec under test. Module
    level rather than on a class, because both goose classes below need the same tree
    and a fixture defined on one class is not reachable from another.

    The spec DECLARING ``kirocrew-dashboard`` is load-bearing: the projection withholds
    a spec-described control-plane server (``mirrors.identity``), so whatever appears
    in the array is the entry the member mount placed.
    """
    import kiro_crew.agent as agent_mod
    from kiro_crew.acp import session_mcp

    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    managed = {
        "kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]},
        MEMBER_DISPATCH_SERVER: {"command": "/opt/kirocrew", "args": ["mcp-dashboard"]},
    }
    monkeypatch.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name: dict(managed[name]) if name in managed else None,
    )
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    (d / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "mcpServers": {
                    "kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]},
                    MEMBER_DISPATCH_SERVER: {
                        "command": "/opt/kirocrew",
                        "args": ["mcp-dashboard"],
                    },
                },
                "tools": ["@kirocrew-core", "@" + MEMBER_DISPATCH_SERVER],
            }
        ),
        encoding="utf-8",
    )
    return d


def _goose_client(tmp_path: Path, session_key: str):
    """A minimally constructed ``AcpClient`` on the goose backend.

    Only the attributes ``_resolve_session_mcp_servers`` and the identity check read.
    Driving a real goose session would activate the harness, which needs a provider
    this test host does not have.
    """
    from kiro_crew.acp.client import AcpClient as _C

    client = object.__new__(_C)
    # ``backend`` is a read-only property over this attribute on the real class.
    client._acp_backend = ACP_BACKEND_GOOSE
    client._mcp_gateway_overlay = None  # shared gateway off: no broker stubs
    client._agent = "kirocrew"
    client._work_dir = tmp_path / "work"
    client._session_key = session_key
    client._channel_id = "dashboard"
    client._stub_session_token = "9" * 64
    client._claude_settings_authored = False
    return client


class TestCapabilitySet:
    def test_exactly_the_wire_capable_backends(self):
        """kiro v2 reads its template from disk and exposes no per-session
        channel, so it must never be in the set: a member session on it runs as
        plain chat rather than mounted-and-refused."""
        assert ACP_BACKENDS_MEMBER_DISPATCH == frozenset(
            {
                ACP_BACKEND_CLAUDE,
                ACP_BACKEND_KAS,
                ACP_BACKEND_CODEX,
                ACP_BACKEND_OPENCODE,
                ACP_BACKEND_GOOSE,
            }
        )
        assert ACP_BACKEND_KIRO not in ACP_BACKENDS_MEMBER_DISPATCH

    def test_codex_holds_both_things_membership_needs(self):
        """The mount it rides, and the gate that makes the mount safe.

        Without the array set a codex session gets no Crew array at all; without an
        ENFORCED routing a session that cannot be gated would still run, and session
        control is the one tool set that must not reach one.
        """
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_MEMBER_DISPATCH
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_SESSION_MCP_ARRAY
        assert acp_tool_gate.is_enforced(ACP_BACKEND_CODEX) is True

    def test_the_precondition_is_read_from_the_routing(self):
        """Opposite answers from one question, and claude's answer must not move.

        The client's owned-file fallback answers for an UNENFORCED routing alone.
        claude's is declared and unenforced, so owning ``settings.local.json`` stands
        in for the read-back this core does not have. opencode's IS enforced, and its
        mirror documents ``permission_surface_owned`` as accepted-and-ignored, so
        asking for an owned file there would withhold every member's tools on a
        condition that cannot describe the backend.
        """
        assert acp_tool_gate.is_enforced(ACP_BACKEND_CLAUDE) is False
        assert acp_tool_gate.is_enforced(ACP_BACKEND_OPENCODE) is True
        assert acp_tool_gate.is_enforced(ACP_BACKEND_CODEX) is True
        assert acp_tool_gate.is_enforced(ACP_BACKEND_GOOSE) is True

    def test_opencode_holds_both_things_membership_needs(self):
        """The mount it rides, and the gate that makes the mount safe.

        Its routing is ``VERIFIED_SEEDED_SETTINGS`` rather than codex's
        ``SESSION_CONFIG``, and H6 means codex's membership establishes nothing here
        -- so both facts are asserted for THIS harness rather than inherited.
        """
        assert ACP_BACKEND_OPENCODE in ACP_BACKENDS_MEMBER_DISPATCH
        assert ACP_BACKEND_OPENCODE in ACP_BACKENDS_SESSION_MCP_ARRAY
        assert acp_tool_gate.is_enforced(ACP_BACKEND_OPENCODE) is True

    def test_goose_holds_both_things_membership_needs(self):
        """The mount it rides, and the gate that makes the mount safe.

        Its mount is recorded as a ROUND TRIP rather than advertised, and its routing
        is ``VERIFIED_SEEDED_SETTINGS`` rather than codex's ``SESSION_CONFIG``. H6
        means neither other member's membership establishes anything here, so both
        facts are asserted for THIS harness.
        """
        assert ACP_BACKEND_GOOSE in ACP_BACKENDS_MEMBER_DISPATCH
        assert ACP_BACKEND_GOOSE in ACP_BACKENDS_SESSION_MCP_ARRAY
        assert acp_tool_gate.is_enforced(ACP_BACKEND_GOOSE) is True

    def test_the_round_trip_the_goose_rationale_names_is_committed(self):
        """The evidence is a file in the tree, so a rename cannot leave the membership
        resting on something gone. The frames carry the ``tools/call`` that reached the
        named child and the identity field a dispatch call arrives on.
        """
        corpus = (
            Path(__file__).parent
            / "fixtures"
            / "acp_frames"
            / "goose"
            / "mcp-stdio-mount-live.jsonl"
        )
        body = corpus.read_text(encoding="utf-8")
        assert "tools/call" in body
        assert '"extensionName"' in body


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

    def test_env_carries_the_exported_bound_port(self, monkeypatch):
        """A chat session's MCP child inherits the gateway's environment; this
        entry is built from scratch, so the port has to be handed over
        explicitly or the child dials the default one."""
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "7779")
        entry = member_dispatch_session_server(MEMBER_KEY)
        assert entry is not None
        assert {"name": "KIROCREW_BOUND_PORT", "value": "7779"} in entry["env"]

    def test_env_falls_back_to_the_serving_resolver(self, monkeypatch):
        """No export (a gateway started outside the normal path) still yields a
        port: the serving resolver's remaining order — configured, marker,
        default — decides it, and the member child inherits that answer instead
        of re-deriving it without lsof."""
        from kiro_crew import port_resolution

        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
        monkeypatch.setattr(port_resolution, "resolve_serving_port", lambda: 6123)
        entry = member_dispatch_session_server(MEMBER_KEY)
        assert entry is not None
        assert {"name": "KIROCREW_BOUND_PORT", "value": "6123"} in entry["env"]

    def test_port_does_not_displace_the_identity_pair(self, monkeypatch):
        """Both env pairs, and no KIROCREW_PORT: that name means "the port an
        operator asked for" and is persisted, so exporting it here would let a
        transient binding outlive this process."""
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "7779")
        entry = member_dispatch_session_server(MEMBER_KEY)
        assert entry is not None
        names = [pair["name"] for pair in entry["env"]]
        assert "KIROCREW_SESSION_KEY" in names
        assert "KIROCREW_BOUND_PORT" in names
        assert "KIROCREW_PORT" not in names

    def test_env_carries_the_gateway_home_override(self, monkeypatch):
        """On an install with ``KIROCREW_HOME`` set (a pod, a second profile) the
        server must resolve THIS gateway from that home. Without the override it
        would present the member's identity to the default home's gateway, where
        the member slot does not exist, and every verb would be refused as
        ``caller_unidentified``. Same helper the managed Crew servers use, so the
        two cannot drift."""
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "_managed_mcp_env", lambda: {"KIROCREW_HOME": "/pods/x"})
        entry = member_dispatch_session_server(MEMBER_KEY)
        assert entry is not None
        assert {"name": "KIROCREW_HOME", "value": "/pods/x"} in entry["env"]
        assert {"name": "KIROCREW_SESSION_KEY", "value": MEMBER_KEY} in entry["env"]

    def test_default_install_carries_no_home_override(self, monkeypatch):
        """No ``KIROCREW_HOME`` override on a default install: the env is exactly
        the identity pair plus the bound port — byte-identical to before the
        override was threaded through."""
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "_managed_mcp_env", lambda: {})
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "7779")
        entry = member_dispatch_session_server(MEMBER_KEY)
        assert entry is not None
        assert entry["env"] == [
            {"name": "KIROCREW_SESSION_KEY", "value": MEMBER_KEY},
            {"name": "KIROCREW_BOUND_PORT", "value": "7779"},
        ]

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
    """The attributes ``_append_member_dispatch_server`` reads.

    ``_stub_session_token`` joined them when the dashboard element started
    carrying this session's signed identity token beside its session key: the
    real ``AcpClient`` mints one in ``__init__``, so a double without it models a
    client that cannot exist.
    """

    backend = ACP_BACKEND_CLAUDE
    _session_key = MEMBER_KEY
    _claude_settings_authored = True
    _stub_session_token = "e" * 64
    # The real predicate, not a double: it is the thing that decides whether a
    # restricted server may be re-added, and a stubbed answer would test the stub.
    _withhold_is_the_only_deny_channel = AcpClient._withhold_is_the_only_deny_channel
    # The real guard, for the same reason: it is where the disabled, restricted and
    # permission-surface preconditions are decided, and both member mounts read it.
    _member_mount_withheld = AcpClient._member_mount_withheld


def _base_servers() -> list[dict]:
    return [{"name": "kirocrew-core", "command": "x", "args": [], "env": [], "type": "stdio"}]


class TestClaudeMemberAppend:
    def _run(self, stub) -> list[dict]:
        return AcpClient._append_member_dispatch_server(stub, _base_servers())

    def test_member_session_gains_the_entry(self):
        out = self._run(_ClientStub())
        assert [e["name"] for e in out][-1] == MEMBER_DISPATCH_SERVER
        assert {"name": "KIROCREW_SESSION_KEY", "value": MEMBER_KEY} in out[-1]["env"]
        # ...and this session's signed identity token, which the strict resolver
        # prefers because a rekey cannot leave it stale.
        assert {
            "name": STUB_SESSION_TOKEN_ENV,
            "value": _ClientStub._stub_session_token,
        } in out[
            -1
        ]["env"]

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


class TestCodexMemberMount:
    """What actually mounts session control on a codex member DM.

    NOT ``_append_member_dispatch_server``: that helper serves the backends whose
    array the CLIENT composes, and codex is not one of them -- its array is built by
    ``AcpRuntime._mirrored_session_mcp`` and the runtime appends the member entry
    itself, keyed only on a non-empty ``member_session_key``. So the codex-specific
    fact is the one asserted here, the seam that decides whether that key is empty.
    """

    @staticmethod
    def _provider(backend: str, session_key: str) -> AcpProvider:
        provider = object.__new__(AcpProvider)
        provider._client = SimpleNamespace(backend=backend, _session_key=session_key)
        return provider

    def _key(self, backend: str, session_key: str) -> str:
        return AcpProvider._member_session_key(self._provider(backend, session_key))

    def test_a_codex_member_session_yields_its_key(self):
        """Non-empty is the whole mount: both runtime establishment paths read this
        one value, and the block they gate on is backend-agnostic."""
        assert self._key(ACP_BACKEND_CODEX, MEMBER_KEY) == MEMBER_KEY

    def test_an_ordinary_codex_session_yields_nothing(self):
        """The mount is SESSION-scoped, which is the whole reason it is not in the
        agent template: another session on the same agent gains nothing."""
        assert self._key(ACP_BACKEND_CODEX, "dashboard_abc123") == ""

    def test_a_member_session_off_the_set_yields_nothing(self):
        """kiro-cli reads its template from disk, so its member threads stay plain
        chat -- and this is the value that keeps the runtime from mounting anything."""
        assert self._key(ACP_BACKEND_KIRO, MEMBER_KEY) == ""

    def test_the_mounted_entry_survives_codex_transport_narrowing(self):
        """The harness has the last word on the array, and it drops what it did not
        advertise -- so an entry the runtime appends is only mounted if it survives
        here. ``member_dispatch_session_server`` builds a stdio element, which
        codex-acp 1.11.0 advertises; a remote-transport entry would be dropped with
        no error anywhere.
        """
        from kiro_crew.acp.harness.codex import CodexHarness

        entry = member_dispatch_session_server(MEMBER_KEY, "f" * 64)
        assert entry is not None
        kept = CodexHarness().session_mcp_servers(
            [*_base_servers(), entry],
            agent_capabilities={"mcpCapabilities": {"acp": False, "http": True, "sse": False}},
        )
        assert [e["name"] for e in kept] == ["kirocrew-core", MEMBER_DISPATCH_SERVER]
        assert {"name": "KIROCREW_SESSION_KEY", "value": MEMBER_KEY} in kept[-1]["env"]


class TestOpencodeMemberAppend:
    """An opencode member session mounts without owning any permission file.

    Same precondition shape as codex, reached through a DIFFERENT routing:
    ``VERIFIED_SEEDED_SETTINGS`` is enforced because the seeded value is read back
    from the harness's own config resolution before the first prompt. The mutation
    these carry is the precondition itself -- read claude's flag on this backend and
    the first test withholds the entry, which is the state the mirror's own docstring
    says must not happen (``permission_surface_owned`` is accepted and ignored here,
    so no opencode session can ever satisfy it).
    """

    @staticmethod
    def _opencode_stub(**over):
        stub = _ClientStub()
        stub.backend = ACP_BACKEND_OPENCODE
        # opencode writes no ``settings.local.json`` and claude's writer is the only
        # thing that sets this flag, so a stub leaving it True models a client that
        # cannot exist on this backend.
        stub._claude_settings_authored = False
        for name, value in over.items():
            setattr(stub, name, value)
        return stub

    def _run(self, stub) -> list[dict]:
        return AcpClient._append_member_dispatch_server(stub, _base_servers())

    def test_member_session_gains_the_entry(self):
        out = self._run(self._opencode_stub())
        assert [e["name"] for e in out] == ["kirocrew-core", MEMBER_DISPATCH_SERVER]
        env = out[-1]["env"]
        assert {"name": "KIROCREW_SESSION_KEY", "value": MEMBER_KEY} in env
        token = {"name": STUB_SESSION_TOKEN_ENV, "value": _ClientStub._stub_session_token}
        assert token in env

    def test_non_member_session_is_untouched(self):
        """The mount is SESSION-scoped, which is why it is not in the agent
        template: an ordinary opencode session on the same agent gains nothing."""
        stub = self._opencode_stub(_session_key="dashboard_abc123")
        assert self._run(stub) == _base_servers()

    def test_an_empty_session_key_is_untouched(self):
        """A pooled child claimed later has no key yet, and a mount with no identity
        would answer ``identity_unattested`` to every verb."""
        stub = self._opencode_stub(_session_key="")
        assert self._run(stub) == _base_servers()


class TestOpencodeSessionArray:
    """The whole array an opencode ``session/new`` would carry, not just the append.

    The claim membership makes is about the ARRAY: a member DM thread on this harness
    holds the session-control tools. So these drive
    ``AcpClient._resolve_session_mcp_servers`` with the REAL opencode mirror over a
    real agent spec, and read the elements that come out. Nothing here asserts set
    membership -- that would be the claim proving itself.

    The spec's own ``kirocrew-dashboard`` is the case the projection must NOT supply:
    ``mirrors.identity.identity_bound_crew_servers`` withholds it because a
    spec-described element carries no session identity, so the entry in the array has
    to be the one this session's mount places.
    """

    @staticmethod
    def _client(agents_dir: Path, tmp_path: Path, session_key: str):
        from kiro_crew.acp.client import AcpClient as _C

        client = object.__new__(_C)
        # ``backend`` is a read-only property over this attribute on the real class.
        client._acp_backend = ACP_BACKEND_OPENCODE
        client._mcp_gateway_overlay = None  # shared gateway off: no broker stubs
        client._agent = "kirocrew"
        client._work_dir = tmp_path / "work"
        client._session_key = session_key
        client._channel_id = "dashboard"
        client._stub_session_token = "f" * 64
        client._claude_settings_authored = False
        return client

    @pytest.fixture
    def agents_dir(self, tmp_path, monkeypatch):
        """The documented seam for driving a mirror over a spec on disk.

        Same one ``test_opencode_session_mcp.py`` uses: materialization would
        otherwise rebuild the managed default and overwrite the spec under test.
        """
        import kiro_crew.agent as agent_mod
        from kiro_crew.acp import session_mcp

        d = tmp_path / "agents"
        d.mkdir()
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
        monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
        monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
        managed = {
            "kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]},
            "kirocrew-dashboard": {"command": "/opt/kirocrew", "args": ["mcp-dashboard"]},
        }
        monkeypatch.setattr(
            session_mcp,
            "managed_mcp_spec_entry",
            lambda name: dict(managed[name]) if name in managed else None,
        )
        monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
        (d / "kirocrew.json").write_text(
            json.dumps(
                {
                    "name": "kirocrew",
                    "mcpServers": {
                        "kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]},
                        MEMBER_DISPATCH_SERVER: {
                            "command": "/opt/kirocrew",
                            "args": ["mcp-dashboard"],
                        },
                    },
                    "tools": ["@kirocrew-core", "@" + MEMBER_DISPATCH_SERVER],
                }
            ),
            encoding="utf-8",
        )
        return d

    def test_a_member_session_carries_the_dashboard_element_last(
        self, agents_dir, tmp_path, monkeypatch
    ):
        client = self._client(agents_dir, tmp_path, MEMBER_KEY)
        out = client._resolve_session_mcp_servers()
        names = [e["name"] for e in out]
        assert MEMBER_DISPATCH_SERVER in names, names
        assert names.count(MEMBER_DISPATCH_SERVER) == 1, names
        assert "kirocrew-core" in names, names
        # Located by NAME rather than by position: a member session also carries
        # the panel element, so which appended entry sits at the end of the array
        # says nothing. Uniqueness plus the identity env is what the mount
        # guarantees, and no consumer reads this array's order -- precedence is by
        # declaration SITE (session-level over agent block).
        mounted = next(e for e in out if e["name"] == MEMBER_DISPATCH_SERVER)
        env = {p["name"]: p["value"] for p in mounted["env"]}
        assert env["KIROCREW_SESSION_KEY"] == MEMBER_KEY
        assert env[STUB_SESSION_TOKEN_ENV] == client._stub_session_token

    def test_an_ordinary_session_carries_no_dashboard_element(
        self, agents_dir, tmp_path, monkeypatch
    ):
        """And the spec DECLARED it, which is the point: the projection withholds a
        spec-described control-plane server, so an ordinary session on this very
        template cannot reach session control."""
        client = self._client(agents_dir, tmp_path, "dashboard_abc123")
        names = [e["name"] for e in client._resolve_session_mcp_servers()]
        assert MEMBER_DISPATCH_SERVER not in names, names
        assert "kirocrew-core" in names, names

    def test_a_disabled_dashboard_server_withholds_the_mount(
        self, agents_dir, tmp_path, monkeypatch
    ):
        """``disabled`` is the stronger switch and binds on every backend.

        A per-tool narrowing can be honoured by a harness that refuses the call; a
        whole-server disable has no per-call form at all, so nothing downstream can
        refuse a call to a server it was handed. The ``tools`` allowlist keeps a
        disabled server out of the spec-described half of the array, but this entry is
        appended after that filter and would otherwise walk straight past it.
        """
        (tmp_path / "settings-mcp.json").write_text(
            json.dumps({"mcpServers": {MEMBER_DISPATCH_SERVER: {"disabled": True}}}),
            encoding="utf-8",
        )
        client = self._client(agents_dir, tmp_path, MEMBER_KEY)
        names = [e["name"] for e in client._resolve_session_mcp_servers()]
        assert MEMBER_DISPATCH_SERVER not in names, names
        assert "kirocrew-core" in names, names

    def test_a_switched_off_dashboard_tool_withholds_the_whole_mount(
        self, agents_dir, tmp_path, monkeypatch
    ):
        """The one way this mount can make a session WORSE than no mount at all.

        opencode's declared per-tool deny is ``WHOLE_SERVER``: there is no deny slot on
        the element, no file of Crew's, and no structured identity on a tool call for
        the client to refuse by, so withholding the narrowed server IS the enforcement.
        Re-adding it for a member would put a tool the operator switched off back within
        reach, and nothing downstream would refuse the call. The thread runs as plain
        chat instead.

        The restriction is written the way the dashboard's tool-off action writes it --
        to the global MCP settings file, which for Crew's own managed servers is the only
        place it can live.
        """
        (tmp_path / "settings-mcp.json").write_text(
            json.dumps(
                {"mcpServers": {MEMBER_DISPATCH_SERVER: {"disabledTools": ["session_stop"]}}}
            ),
            encoding="utf-8",
        )
        client = self._client(agents_dir, tmp_path, MEMBER_KEY)
        names = [e["name"] for e in client._resolve_session_mcp_servers()]
        assert MEMBER_DISPATCH_SERVER not in names, names
        assert "kirocrew-core" in names, names


class TestRestrictedServerIsNotReAdded:
    """The append may not un-withhold a narrowed server -- but only where that would
    actually widen anything.

    Three answers from one declaration (``registry.PerToolDeny``), which is why the
    client reads that rather than a membership set of its own: withholding is the whole
    enforcement on opencode, while codex still refuses the call at permission time from
    ``denied_tools`` and claude's ``permissions.deny`` rules refuse it inside the
    adapter. Withholding the mount on either of those two would cost a member thread
    its tools and buy nothing.
    """

    @staticmethod
    def _run(backend: str, restricted):
        stub = _ClientStub()
        stub.backend = backend
        stub._claude_settings_authored = True  # so claude reaches the second check
        return AcpClient._append_member_dispatch_server(stub, _base_servers(), restricted)

    @pytest.mark.parametrize("backend", [ACP_BACKEND_OPENCODE, ACP_BACKEND_GOOSE])
    def test_a_whole_server_deny_backend_withholds(self, backend):
        out = self._run(backend, frozenset({MEMBER_DISPATCH_SERVER}))
        assert out == _base_servers()

    def test_opencode_still_mounts_when_another_server_is_the_restricted_one(self):
        """Scoped to the name being mounted: a third-party server's restriction says
        nothing about this one."""
        out = self._run(ACP_BACKEND_OPENCODE, frozenset({"some-third-party"}))
        assert [e["name"] for e in out][-1] == MEMBER_DISPATCH_SERVER

    @pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE])
    def test_a_backend_with_a_second_deny_channel_keeps_its_mount(self, backend):
        out = self._run(backend, frozenset({MEMBER_DISPATCH_SERVER}))
        assert [e["name"] for e in out][-1] == MEMBER_DISPATCH_SERVER

    def test_the_channel_question_is_answered_from_the_declaration(self):
        """And it is the declaration the projection itself acts on, so the two cannot
        disagree about one backend."""
        from kiro_crew.providers.mirrors import PerToolDeny, projection_for

        assert projection_for(ACP_BACKEND_OPENCODE).per_tool_deny is PerToolDeny.WHOLE_SERVER
        assert projection_for(ACP_BACKEND_GOOSE).per_tool_deny is PerToolDeny.WHOLE_SERVER
        assert projection_for(ACP_BACKEND_CODEX).per_tool_deny is PerToolDeny.PER_CALL
        assert projection_for(ACP_BACKEND_CLAUDE).per_tool_deny is PerToolDeny.SETTINGS_FILE

    def test_an_undeclared_backend_fails_closed(self):
        """``projection_for`` raises for a backend with no declaration, and an unknown
        deny channel cannot be shown to make a re-add safe. The cost is plain chat."""
        stub = _ClientStub()
        stub.backend = "no-such-backend"
        assert AcpClient._withhold_is_the_only_deny_channel(stub) is True


class TestDisabledServerIsNeverMounted:
    """``disabled`` stops the client's mount with no backend condition.

    The contrast with :class:`TestRestrictedServerIsNotReAdded` is the point: there,
    codex and claude keep the mount because each still refuses the CALL. A whole-server
    disable has no per-call form for either of them to refuse by, so there is nothing
    to weigh and no backend is exempt on this path.

    Scope worth stating, since these parametrize over backends: they pin the CLIENT
    path, which is opencode's. The other composer is pinned beside its own code, in
    ``test_acp_runtime.py::TestRuntimeMemberDispatchDisabled`` -- both of
    ``AcpRuntime``'s paths, create and resume.
    """

    @staticmethod
    def _run(backend: str, disabled):
        stub = _ClientStub()
        stub.backend = backend
        stub._claude_settings_authored = True
        return AcpClient._append_member_dispatch_server(
            stub, _base_servers(), frozenset(), disabled
        )

    @pytest.mark.parametrize(
        "backend",
        [ACP_BACKEND_OPENCODE, ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE, ACP_BACKEND_GOOSE],
    )
    def test_every_dispatch_backend_withholds(self, backend):
        assert self._run(backend, frozenset({MEMBER_DISPATCH_SERVER})) == _base_servers()

    @pytest.mark.parametrize(
        "backend",
        [ACP_BACKEND_OPENCODE, ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE, ACP_BACKEND_GOOSE],
    )
    def test_another_disabled_server_does_not_withhold_this_one(self, backend):
        out = self._run(backend, frozenset({"some-third-party"}))
        assert [e["name"] for e in out][-1] == MEMBER_DISPATCH_SERVER

    def test_the_parse_reads_both_sources_and_exempts_nothing(self):
        """Including the control plane: ``disabled`` there is the user saying so, and
        this function's job is to report it rather than to judge it."""
        from kiro_crew.acp.session_mcp import session_mcp_disabled_servers

        spec = {"mcpServers": {"from-spec": {"disabled": True}, "on": {}}}
        settings = {"mcpServers": {"kirocrew-core": {"disabled": True}}}
        assert session_mcp_disabled_servers(spec, settings) == frozenset(
            {"from-spec", "kirocrew-core"}
        )
        assert session_mcp_disabled_servers(None, "not a dict") == frozenset()


class TestRuntimeMemberThreading:
    """The member flag must reach the KAS projection through the runtime."""

    @staticmethod
    def _runtime(monkeypatch, seen):
        from kiro_crew.acp import runtime as runtime_mod

        rt = object.__new__(runtime_mod.AcpRuntime)
        rt._acp_backend = ACP_BACKEND_KAS
        rt._mcp_gateway_overlay = None

        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _a: None)
        import kiro_crew.acp.kas_agents as kas_agents_mod
        import kiro_crew.config.paths as paths_mod
        import kiro_crew.mcp_gateway.session_servers as session_servers_mod

        monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: Path("/agents"))
        monkeypatch.setattr(kas_agents_mod, "load_agent_spec", lambda _dir, agent: {"name": agent})
        monkeypatch.setattr(
            session_servers_mod, "injection_server_names", lambda _o, _a: frozenset()
        )

        def _capture(
            _dir,
            agent,
            _spec,
            *,
            stub_server_names=frozenset(),
            member_dispatch=False,
            crew_panel=False,
            session_key="",
        ):
            seen.append(member_dispatch)
            return [{"id": agent}]

        monkeypatch.setattr(kas_agents_mod, "build_kas_custom_agents", _capture)
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
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _a: None)
        import kiro_crew.acp.kas_agents as kas_agents_mod
        import kiro_crew.config.paths as paths_mod
        import kiro_crew.mcp_gateway.session_servers as session_servers_mod

        monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: Path("/agents"))
        monkeypatch.setattr(kas_agents_mod, "load_agent_spec", lambda _dir, agent: {"name": agent})
        monkeypatch.setattr(
            session_servers_mod, "injection_server_names", lambda _o, _a: frozenset()
        )
        seen: list[frozenset] = []

        def _capture(
            _dir,
            agent,
            _spec,
            *,
            stub_server_names=frozenset(),
            member_dispatch=False,
            crew_panel=False,
            session_key="",
        ):
            seen.append(frozenset(stub_server_names))
            return [{"id": agent}]

        monkeypatch.setattr(kas_agents_mod, "build_kas_custom_agents", _capture)
        await rt._kas_custom_agents("kirocrew", member_dispatch=True)
        assert seen == [frozenset({MEMBER_DISPATCH_SERVER})]

    @pytest.mark.asyncio
    async def test_non_member_set_is_unchanged(self, monkeypatch):
        from kiro_crew.acp import runtime as runtime_mod

        rt = object.__new__(runtime_mod.AcpRuntime)
        rt._acp_backend = ACP_BACKEND_KAS
        rt._mcp_gateway_overlay = None
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _a: None)
        import kiro_crew.acp.kas_agents as kas_agents_mod
        import kiro_crew.config.paths as paths_mod
        import kiro_crew.mcp_gateway.session_servers as session_servers_mod

        monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: Path("/agents"))
        monkeypatch.setattr(kas_agents_mod, "load_agent_spec", lambda _dir, agent: {"name": agent})
        monkeypatch.setattr(
            session_servers_mod, "injection_server_names", lambda _o, _a: frozenset()
        )
        seen: list[frozenset] = []

        def _capture(
            _dir,
            agent,
            _spec,
            *,
            stub_server_names=frozenset(),
            member_dispatch=False,
            crew_panel=False,
            session_key="",
        ):
            seen.append(frozenset(stub_server_names))
            return [{"id": agent}]

        monkeypatch.setattr(kas_agents_mod, "build_kas_custom_agents", _capture)
        await rt._kas_custom_agents("kirocrew")
        assert seen == [frozenset()]


class TestSelectProviderBackend:
    """The per-session half of the one backend-selection gate (H3/H13)."""

    def test_member_route(self):
        from kiro_crew.members import select_provider_backend

        assert select_provider_backend(MEMBER_KEY, "kas", "") == "kas"

    def test_non_member_gets_the_configured_default_unresolved(self):
        """The default arm passes the configured value through UNCHANGED —
        normalization already happened at config load, and re-resolving here
        would be the second check H3 forbids."""
        from kiro_crew.members import select_provider_backend

        assert select_provider_backend("dashboard_abc", "kas", "") == ""

    def test_denied_member_backend_degrades_to_kiro(self):
        from kiro_crew.members import select_provider_backend

        assert select_provider_backend(MEMBER_KEY, "no-such-backend", "") == ""


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


class TestGooseMemberAppend:
    """A goose member session mounts without owning any permission file.

    Same precondition shape as opencode's, asserted for this harness because H6 lets
    neither stand in for the other. The mutation these carry is the precondition
    itself: read claude's flag on this backend and the first test withholds the entry.
    """

    @staticmethod
    def _goose_stub(**over):
        stub = _ClientStub()
        stub.backend = ACP_BACKEND_GOOSE
        # goose writes no ``settings.local.json`` and claude's writer is the only thing
        # that sets this flag, so a stub leaving it True models a client that cannot
        # exist on this backend.
        stub._claude_settings_authored = False
        for name, value in over.items():
            setattr(stub, name, value)
        return stub

    def _run(self, stub) -> list[dict]:
        return AcpClient._append_member_dispatch_server(stub, _base_servers())

    def test_member_session_gains_the_entry(self):
        out = self._run(self._goose_stub())
        assert [e["name"] for e in out] == ["kirocrew-core", MEMBER_DISPATCH_SERVER]
        env = out[-1]["env"]
        assert {"name": "KIROCREW_SESSION_KEY", "value": MEMBER_KEY} in env
        token = {"name": STUB_SESSION_TOKEN_ENV, "value": _ClientStub._stub_session_token}
        assert token in env

    def test_non_member_session_is_untouched(self):
        """The mount is SESSION-scoped, which is why it is not in the agent template:
        an ordinary goose session on the same agent gains nothing."""
        stub = self._goose_stub(_session_key="dashboard_abc123")
        assert self._run(stub) == _base_servers()

    def test_an_empty_session_key_is_untouched(self):
        """A pooled child claimed later has no key yet, and a mount with no identity
        would answer ``identity_unattested`` to every verb."""
        stub = self._goose_stub(_session_key="")
        assert self._run(stub) == _base_servers()


class TestGooseSessionArray:
    """The whole array a goose ``session/new`` would carry, not just the append.

    The claim membership makes is about the ARRAY: a member DM thread on this harness
    holds the session-control tools. So these drive
    ``AcpClient._resolve_session_mcp_servers`` with the REAL goose mirror over a real
    agent spec and read the elements that come out. Nothing here asserts set
    membership -- that would be the claim proving itself.

    goose reads no ``~/.kiro/agents/<name>.json``, so this array is the only channel
    any tool set reaches the session on.
    """

    def test_a_member_session_carries_the_dashboard_element_last(self, goose_agents_dir, tmp_path):
        client = _goose_client(tmp_path, MEMBER_KEY)
        out = client._resolve_session_mcp_servers()
        names = [e["name"] for e in out]
        assert MEMBER_DISPATCH_SERVER in names, names
        assert names.count(MEMBER_DISPATCH_SERVER) == 1, names
        assert "kirocrew-core" in names, names
        # By NAME, not by position: see the opencode twin of this test.
        mounted = next(e for e in out if e["name"] == MEMBER_DISPATCH_SERVER)
        env = {p["name"]: p["value"] for p in mounted["env"]}
        assert env["KIROCREW_SESSION_KEY"] == MEMBER_KEY
        assert env[STUB_SESSION_TOKEN_ENV] == client._stub_session_token

    def test_an_ordinary_session_carries_no_dashboard_element(self, goose_agents_dir, tmp_path):
        """And the spec DECLARED it, which is the point: the projection withholds a
        spec-described control-plane server, so an ordinary session on this very
        template cannot reach session control."""
        client = _goose_client(tmp_path, "dashboard_abc123")
        names = [e["name"] for e in client._resolve_session_mcp_servers()]
        assert MEMBER_DISPATCH_SERVER not in names, names
        assert "kirocrew-core" in names, names

    def test_a_disabled_dashboard_server_withholds_the_mount(self, goose_agents_dir, tmp_path):
        """``disabled`` is the stronger switch and binds on every backend: it has no
        per-call form, so nothing downstream can refuse a call to a server it was
        handed, and the ``tools`` allowlist does not reach an appended element."""
        (tmp_path / "settings-mcp.json").write_text(
            json.dumps({"mcpServers": {MEMBER_DISPATCH_SERVER: {"disabled": True}}}),
            encoding="utf-8",
        )
        client = _goose_client(tmp_path, MEMBER_KEY)
        names = [e["name"] for e in client._resolve_session_mcp_servers()]
        assert MEMBER_DISPATCH_SERVER not in names, names
        assert "kirocrew-core" in names, names

    def test_a_switched_off_dashboard_tool_withholds_the_whole_mount(
        self, goose_agents_dir, tmp_path
    ):
        """goose's declared per-tool deny is ``WHOLE_SERVER``, so withholding the
        narrowed server IS the enforcement; re-adding it for a member would put a tool
        the operator switched off back within reach."""
        (tmp_path / "settings-mcp.json").write_text(
            json.dumps(
                {"mcpServers": {MEMBER_DISPATCH_SERVER: {"disabledTools": ["session_stop"]}}}
            ),
            encoding="utf-8",
        )
        client = _goose_client(tmp_path, MEMBER_KEY)
        names = [e["name"] for e in client._resolve_session_mcp_servers()]
        assert MEMBER_DISPATCH_SERVER not in names, names
        assert "kirocrew-core" in names, names


class TestGooseIdentityDrift:
    """The server this session mounted must not be refused as a DRIFTED one.

    goose is the only member of ``ACP_BACKENDS_META_IDENTITY``, so it is the only
    member of the dispatch set whose every tool approval runs
    ``AcpClient._refuse_identity_drift``. That refusal rejects a trusted identity
    naming a server Crew never placed on the array, and ``kirocrew-dashboard`` is
    neither the harness's builtin extension (``developer``) nor a server the template
    supplies -- the projection withholds the spec-described copy. So a mount that
    landed in one array while the identity check read another would refuse EVERY
    dispatch call instead of degrading to plain chat, which is strictly worse than not
    mounting at all.

    These drive the real chain rather than a stubbed array: the elements come from
    ``_resolve_session_mcp_servers``, the cache the check enumerates is that same
    list, and the identity is the one goose publishes for a mounted element -- the
    element's own name in ``_meta.goose.toolCall.extensionName``, the field
    ``mcp-stdio-mount-live.jsonl`` pins for ``crew-probe``.
    """

    @staticmethod
    def _event(server: str, tool: str):
        from kiro_crew.acp.types import AcpEvent

        return AcpEvent(
            kind="permission_request",
            request_id="p",
            mcp_server_name=server,
            tool_name=tool,
            mcp_identity_trusted=True,
        )

    @staticmethod
    def _mounted_client(tmp_path, session_key=MEMBER_KEY):
        client = _goose_client(tmp_path, session_key)
        # The cache the permission path reads, filled from the array the session/new
        # path composes -- the two halves the refusal requires to agree.
        client._session_mcp_cache = client._resolve_session_mcp_servers()
        return client

    def test_a_dispatch_call_is_not_foreign(self, goose_agents_dir, tmp_path):
        client = self._mounted_client(tmp_path)
        placed = [e["name"] for e in client._session_mcp_servers()]
        assert MEMBER_DISPATCH_SERVER in placed, placed
        event = self._event(MEMBER_DISPATCH_SERVER, "session_stop")
        assert client._foreign_mcp_identity(event) is False

    def test_the_check_still_refuses_a_drifted_server(self, goose_agents_dir, tmp_path):
        """The control: the mount widens the placed set by exactly one name, so a
        renamed builtin is still refused on the very session that carries the mount."""
        client = self._mounted_client(tmp_path)
        assert client._foreign_mcp_identity(self._event("dev", "shell")) is True

    def test_an_unmounted_session_would_refuse_that_same_identity(self, goose_agents_dir, tmp_path):
        """Why the two halves must read ONE array, as a fact rather than a docstring:
        on a session that did not mount it, this identity IS foreign."""
        client = self._mounted_client(tmp_path, session_key="dashboard_abc123")
        event = self._event(MEMBER_DISPATCH_SERVER, "session_stop")
        assert client._foreign_mcp_identity(event) is True

    def test_the_whole_refusal_leaves_a_dispatch_approval_alone(self, goose_agents_dir, tmp_path):
        """Through ``_refuse_identity_drift`` itself rather than only its predicate:
        the unclassified-call check runs first and must not catch this either."""
        import asyncio

        client = self._mounted_client(tmp_path)
        client._session_id = "goose-session-1"
        client._tool_call_unclassified = {}

        async def _never(_request_id: str) -> None:  # pragma: no cover - must not run
            raise AssertionError("a mounted dispatch server must not be refused")

        client.reject_tool = _never  # type: ignore[method-assign]
        event = self._event(MEMBER_DISPATCH_SERVER, "session_stop")
        assert asyncio.run(client._refuse_identity_drift(event)) is False
