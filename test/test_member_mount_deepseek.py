"""A deepseek member DM session holds the member session-control tools and the panel.

deepseek has a mirror (``providers/mirrors/deepseek.py``) that translates the agent
spec through ``place_single_binary_array``, the same path goose and opencode take.
These drive the REAL resolver over a real agent spec and read the elements that come
out. Driving a live deepseek session would need the harness binary and a model, which
this test host does not have; the mount itself is pinned by the committed round trip in
``test/fixtures/acp_frames/deepseek/mcp-stdio-mount-live.jsonl``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.validation  # noqa: F401 - break the legacy import cycle first
from kiro_crew import acp_tool_gate
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import (
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_PI,
    ACP_BACKENDS_MEMBER_DISPATCH,
    ACP_BACKENDS_MEMBER_PANEL,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
)
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.members import MEMBER_DISPATCH_SERVER, MEMBER_PANEL_SERVER

MEMBER_KEY = "dashboard_member-autofix"
FIXTURES = Path(__file__).parent / "fixtures" / "acp_frames" / "deepseek"

#: The control-plane server the agent spec declares; the mirror includes it in the
#: array because it is not identity-bound (CONTROL_PLANE_SERVERS are not withheld
#: by ``identity_bound_crew_servers``).
CONTROL_PLANE_SERVER = "kirocrew-core"


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """An agent spec on disk that DECLARES the dashboard server, and the panel on."""
    import kiro_crew.agent as agent_mod
    import kiro_crew.members as members_mod
    from kiro_crew.acp import session_mcp

    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    monkeypatch.setattr(members_mod, "crew_panel_enabled", lambda: True)
    (d / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "mcpServers": {
                    CONTROL_PLANE_SERVER: {"command": "/opt/kirocrew", "args": ["mcp-core"]},
                    MEMBER_DISPATCH_SERVER: {
                        "command": "/opt/kirocrew",
                        "args": ["mcp-dashboard"],
                    },
                },
                "tools": ["@" + CONTROL_PLANE_SERVER, "@" + MEMBER_DISPATCH_SERVER],
            }
        ),
        encoding="utf-8",
    )
    return d


def _client(tmp_path: Path, session_key: str, backend: str = ACP_BACKEND_DEEPSEEK):
    """A minimally constructed ``AcpClient``: only what the resolver reads."""
    client = object.__new__(AcpClient)
    client._acp_backend = backend
    client._mcp_gateway_overlay = None  # shared gateway off: no broker stubs
    client._agent = "kirocrew"
    client._work_dir = tmp_path / "work"
    client._session_key = session_key
    client._channel_id = "dashboard"
    client._stub_session_token = "9" * 64
    client._claude_settings_authored = False
    client._session_mcp_cache = None
    return client


def _names(out: list[dict]) -> list[str]:
    return [e["name"] for e in out]


class TestTheDecision:
    def test_deepseek_holds_both_things_membership_needs(self):
        """The mount it rides, and the gate that makes the mount safe, for THIS harness."""
        assert ACP_BACKEND_DEEPSEEK in ACP_BACKENDS_MEMBER_DISPATCH
        assert ACP_BACKEND_DEEPSEEK in ACP_BACKENDS_MEMBER_PANEL
        assert ACP_BACKEND_DEEPSEEK in ACP_BACKENDS_SESSION_MCP_ARRAY
        assert acp_tool_gate.is_enforced(ACP_BACKEND_DEEPSEEK) is True

    def test_the_round_trip_the_rationale_names_is_committed(self):
        body = (FIXTURES / "mcp-stdio-mount-live.jsonl").read_text(encoding="utf-8")
        assert "tools/call" in body
        assert "mcp__crew-probe__crew_probe_echo" in body


class TestDeepseekSessionArray:
    def test_a_member_session_carries_both_member_elements(self, agents_dir, tmp_path):
        client = _client(tmp_path, MEMBER_KEY)
        out = client._resolve_session_mcp_servers()
        # The mirror processes the spec: kirocrew-core (control plane, not identity-bound)
        # appears at position 0; kirocrew-dashboard is withheld from the spec-described half
        # (identity-bound) then re-added with identity by _append_member_dispatch_server.
        assert _names(out) == [CONTROL_PLANE_SERVER, MEMBER_DISPATCH_SERVER, MEMBER_PANEL_SERVER]
        # Every element carries session identity (opencode_elements places it on all entries).
        for element in out:
            env = {p["name"]: p["value"] for p in element["env"]}
            assert env["KIROCREW_SESSION_KEY"] == MEMBER_KEY
            assert env[STUB_SESSION_TOKEN_ENV] == client._stub_session_token
        assert client.member_dispatch_mounted is True

    def test_the_wire_accessor_hands_out_the_same_array(self, agents_dir, tmp_path):
        client = _client(tmp_path, MEMBER_KEY)
        assert _names(client._deepseek_session_mcp_servers()) == [
            CONTROL_PLANE_SERVER,
            MEMBER_DISPATCH_SERVER,
            MEMBER_PANEL_SERVER,
        ]

    def test_an_ordinary_session_carries_spec_servers_only(self, agents_dir, tmp_path):
        """The spec's control-plane server is included; member mounts are not."""
        client = _client(tmp_path, "dashboard_abc123")
        # The mirror processes the spec: kirocrew-core appears; no member mounts.
        assert _names(client._resolve_session_mcp_servers()) == [CONTROL_PLANE_SERVER]
        assert client.member_dispatch_mounted is False

    def test_a_disabled_dashboard_server_withholds_only_that_mount(self, agents_dir, tmp_path):
        (tmp_path / "settings-mcp.json").write_text(
            json.dumps({"mcpServers": {MEMBER_DISPATCH_SERVER: {"disabled": True}}}),
            encoding="utf-8",
        )
        client = _client(tmp_path, MEMBER_KEY)
        assert _names(client._resolve_session_mcp_servers()) == [
            CONTROL_PLANE_SERVER,
            MEMBER_PANEL_SERVER,
        ]

    def test_a_switched_off_dashboard_tool_withholds_the_whole_mount(self, agents_dir, tmp_path):
        """The mirror has no per-tool deny channel, so the narrowed server stays off."""
        (tmp_path / "settings-mcp.json").write_text(
            json.dumps(
                {"mcpServers": {MEMBER_DISPATCH_SERVER: {"disabledTools": ["session_stop"]}}}
            ),
            encoding="utf-8",
        )
        client = _client(tmp_path, MEMBER_KEY)
        assert _names(client._resolve_session_mcp_servers()) == [
            CONTROL_PLANE_SERVER,
            MEMBER_PANEL_SERVER,
        ]

    def test_the_panel_switch_withholds_only_the_panel(self, agents_dir, tmp_path, monkeypatch):
        import kiro_crew.members as members_mod

        monkeypatch.setattr(members_mod, "crew_panel_enabled", lambda: False)
        client = _client(tmp_path, MEMBER_KEY)
        assert _names(client._resolve_session_mcp_servers()) == [
            CONTROL_PLANE_SERVER,
            MEMBER_DISPATCH_SERVER,
        ]

    def test_an_unreadable_spec_still_mounts_the_member_elements(self, agents_dir, tmp_path):
        """A spec that cannot be parsed reads as no spec; the member mounts still land."""
        (agents_dir / "kirocrew.json").write_text("{not json", encoding="utf-8")
        client = _client(tmp_path, MEMBER_KEY)
        names = _names(client._resolve_session_mcp_servers())
        assert CONTROL_PLANE_SERVER in names
        assert names[-2:] == [MEMBER_DISPATCH_SERVER, MEMBER_PANEL_SERVER]
        assert client._spec_zero_tools is False

    def test_a_stale_derived_spec_refuses_the_session(self, agents_dir, tmp_path, monkeypatch):
        """The freshness gate's refusal reaches the spawn rather than a zero-tool session.

        Driven through the real gate: the default spec drops an auto-approval, the
        heartbeat copy keeps it, and the re-derive that would bring it current fails.
        """
        import kiro_crew.agent as agent_mod

        def default_spec(auto_approve: list[str]) -> str:
            core = {"command": "/opt/kirocrew", "args": ["mcp-core"], "autoApprove": auto_approve}
            return json.dumps(
                {
                    "name": "kirocrew",
                    "tools": ["@" + CONTROL_PLANE_SERVER],
                    "mcpServers": {CONTROL_PLANE_SERVER: core},
                }
            )

        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents_dir)
        (agents_dir / "kirocrew.json").write_text(
            default_spec(["learn_add", "cron_add"]), encoding="utf-8"
        )
        agent_mod._install_heartbeat_agent()
        (agents_dir / "kirocrew.json").write_text(default_spec(["learn_add"]), encoding="utf-8")

        def failing() -> None:
            raise OSError("heartbeat spec is not writable")

        monkeypatch.setattr(agent_mod, "_install_heartbeat_agent", failing)
        client = _client(tmp_path, MEMBER_KEY)
        client._agent = "kirocrew-heartbeat"
        with pytest.raises(agent_mod.DerivedSpecStale):
            client._resolve_session_mcp_servers()

    def test_pi_takes_session_control_and_the_panel(self, agents_dir, tmp_path):
        """The mirror-less branch is gated per membership: pi holds dispatch and panel."""
        client = _client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_PI)
        assert _names(client._resolve_session_mcp_servers()) == [
            MEMBER_DISPATCH_SERVER,
            MEMBER_PANEL_SERVER,
        ]


class TestPooledStubsBesideTheMount:
    def test_pooled_mcp_servers_is_empty_for_mirror_backends(
        self, agents_dir, tmp_path, monkeypatch
    ):
        """deepseek is a mirror backend: _pooled_mcp_servers returns [] for it.

        The broker stubs go through _resolve_session_mcp_servers → the mirror's
        stub_elements arg, not through _pooled_mcp_servers. A stub the mirror
        withheld (because the spec withheld that server) is not re-added here.
        """
        client = _client(tmp_path, MEMBER_KEY)
        client._session_mcp_cache = client._resolve_session_mcp_servers()
        stubs = [{"name": MEMBER_DISPATCH_SERVER}, {"name": CONTROL_PLANE_SERVER}]
        monkeypatch.setattr(AcpClient, "_pooled_broker_stubs", lambda self: list(stubs))
        # Mirror backends: _pooled_mcp_servers always returns [].
        assert client._pooled_mcp_servers() == []

    def test_a_same_named_stub_is_not_remounted_via_pooled(self, agents_dir, tmp_path, monkeypatch):
        """The mirror path places stubs itself; the pooled path stays empty."""
        client = _client(tmp_path, MEMBER_KEY)
        client._session_mcp_cache = client._resolve_session_mcp_servers()
        stubs = [{"name": MEMBER_DISPATCH_SERVER}, {"name": CONTROL_PLANE_SERVER}]
        monkeypatch.setattr(AcpClient, "_pooled_broker_stubs", lambda self: list(stubs))
        assert client._pooled_mcp_servers() == []


def test_deepseek_mirror_keeps_crew_servers(agents_dir, tmp_path):
    """After the move to a mirror, a DeepSeek session keeps Crew's own servers.

    Before the mirror, every DeepSeek session received Crew's servers through
    the pooled broker path. After the move, that path returns [] for mirror
    backends, and the mirror itself is responsible for including the servers
    the spec declares. This test verifies that a non-member ordinary session
    still gets the control-plane server declared in the spec.
    """
    client = _client(tmp_path, "dashboard_abc123")
    out = client._resolve_session_mcp_servers()
    names = _names(out)
    assert (
        CONTROL_PLANE_SERVER in names
    ), f"kirocrew-core missing from the array after the mirror move: {names}"
