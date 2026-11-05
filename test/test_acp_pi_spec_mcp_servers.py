"""A pi member session carries the agent spec's own MCP servers.

pi is ``ProjectionKind.BROKER_ONLY`` with no mirror, so the client composes its
``session/new`` array itself in ``_mirrorless_member_mounts``; the array is what
the sealed handoff (``acp.harness.pi.pi_mcp_handoff_payload``) carries to the gate
extension, since pi-acp drops the wire copy. The servers a spec declares are the
same set a mirrored harness (opencode) mounts, held to the spec's ``tools``
allowlist and whole-server ``disabled`` the same way.

Fixtures come from ``acp_session_mcp_helpers``: ``agents_dir`` points the spec
resolver at a temp dir, ``_write_spec`` writes one, ``_by_name`` keys an element
list. A member session key (``dashboard:member-<slug>``) makes the session a
crewmate DM, so Crew's own member mounts ride beside the spec servers.
"""

from __future__ import annotations

from acp_session_mcp_helpers import _installed_claude_adapter_version  # noqa: F401
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401
from acp_session_mcp_helpers import _by_name, _write_spec

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_OPENCODE, ACP_BACKEND_PI

_MEMBER_KEY = "dashboard:member-kirocrew"


def _member_client(tmp_path, backend):
    client = AcpClient(
        work_dir=tmp_path,
        agent="kirocrew",
        acp_backend=backend,
        session_key=_MEMBER_KEY,
    )
    client.member_context = True
    return client


class TestPiCarriesSpecDeclaredServers:
    def test_a_spec_declared_server_reaches_the_pi_session(self, tmp_path, agents_dir):
        # The spec declares its OWN server (foo ~ gitlab) and kirocrew-core, with a
        # tools allowlist referencing both. mcp_gateway off (fixture default): no
        # broker stubs, so the spec translation is the only channel for these names.
        _write_spec(
            agents_dir,
            servers={
                "foo": {"command": "/bin/foo", "args": ["serve"]},
                "kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]},
            },
            tools=["@foo", "@kirocrew-core"],
        )
        client = _member_client(tmp_path, ACP_BACKEND_PI)
        names = _by_name(client._session_mcp_servers())
        assert "foo" in names
        assert "kirocrew-core" in names

    def test_pi_carries_the_same_spec_set_as_opencode(self, tmp_path, agents_dir):
        _write_spec(
            agents_dir,
            servers={
                "foo": {"command": "/bin/foo", "args": ["serve"]},
                "kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]},
            },
            tools=["@foo", "@kirocrew-core"],
        )
        pi_names = set(_by_name(_member_client(tmp_path, ACP_BACKEND_PI)._session_mcp_servers()))
        oc_names = set(
            _by_name(_member_client(tmp_path, ACP_BACKEND_OPENCODE)._session_mcp_servers())
        )
        # The spec-declared servers pi now carries match the mirrored harness's.
        assert {"foo", "kirocrew-core"} <= pi_names
        assert {"foo", "kirocrew-core"} <= oc_names

    def test_kirocrew_core_carries_this_session_identity(self, tmp_path, agents_dir):
        # Crew's control plane rides the live session identity, not the spec's env.
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]}},
            tools=["@kirocrew-core"],
        )
        client = _member_client(tmp_path, ACP_BACKEND_PI)
        core = _by_name(client._session_mcp_servers())["kirocrew-core"]
        env = {p["name"]: p["value"] for p in core.get("env") or []}
        assert env.get("KIROCREW_SESSION_KEY") == _MEMBER_KEY


class TestSpecRestrictionsAreHonoured:
    def test_a_server_the_tools_allowlist_does_not_name_is_withheld(self, tmp_path, agents_dir):
        # foo is referenced; bar is not. The allowlist that filters opencode's array
        # filters pi's the same way: bar never reaches the pi session.
        _write_spec(
            agents_dir,
            servers={
                "foo": {"command": "/bin/foo"},
                "bar": {"command": "/bin/bar"},
            },
            tools=["@foo"],
        )
        names = _by_name(_member_client(tmp_path, ACP_BACKEND_PI)._session_mcp_servers())
        assert "foo" in names
        assert "bar" not in names

    def test_a_whole_server_disabled_entry_is_withheld(self, tmp_path, agents_dir):
        # `disabled: true` on an entry withholds it whole; pi has no channel to
        # refuse a call to a server it was handed, so it must not be mounted.
        _write_spec(
            agents_dir,
            servers={
                "foo": {"command": "/bin/foo"},
                "bar": {"command": "/bin/bar", "disabled": True},
            },
            tools=["@foo", "@bar"],
        )
        names = _by_name(_member_client(tmp_path, ACP_BACKEND_PI)._session_mcp_servers())
        assert "foo" in names
        assert "bar" not in names


class TestNoSpecServersIsUnchanged:
    def test_a_spec_that_declares_no_servers_mounts_no_spec_server(self, tmp_path, agents_dir):
        # A member spec with an empty mcpServers and no tools reference adds nothing
        # from the spec: only Crew's own member mounts (if any) remain, and no spec
        # server named foo/bar/etc. appears.
        _write_spec(agents_dir, servers={}, tools=[])
        client = _member_client(tmp_path, ACP_BACKEND_PI)
        names = set(_by_name(client._session_mcp_servers()))
        # No agent-spec server was invented; the control plane also stays out,
        # because an empty tools list grants nothing.
        assert "kirocrew-core" not in names
        assert names <= {"kirocrew-dashboard", "kirocrew-panel"}
