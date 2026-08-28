"""Delivering Kiro Crew's own MCP servers to a spec adapter.

This is the parity item. kiro-cli receives Crew's servers through its ``--agent``
spec, read off disk. A spec adapter reads no Crew config, so the ``mcpServers``
array on ``session/new`` is the only channel — and it was empty, which made every
claude/codex/goose session INERT: no memory, no cron, no spawn, no artifacts, just
a vendor agent with Crew as a chat transport.

The shaping code existed in acp/codex.py the whole time with ZERO callers, and was
wrong in three ways that only mattered once something called it: it omitted the
schema-required ``env`` when empty, ignored ``kirocrew-computer``'s ``spec_gate``,
and delivered the ``opt_in`` ``kirocrew-dashboard`` set to every session.

The security boundaries asserted here are the reason this is not simply "inject the
table".
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.acp import spec_servers
from kiro_crew.acp.codex import Verdict
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_PI,
)


def _client(backend: str, work_dir):
    from kiro_crew.acp.client import AcpClient

    return AcpClient(work_dir=str(work_dir), acp_backend=backend)


class TestManagedServerShaping:
    def test_every_entry_satisfies_the_required_stdio_fields(self) -> None:
        """``McpServerStdio`` requires name, command, args AND env.

        A missing field is not a per-entry problem: a strict deserializer rejects
        the WHOLE session/new, so the failure surfaces as an opaque protocol error.
        """
        entries = spec_servers.managed_spec_servers()
        assert entries, "the always-on servers must be delivered"
        for entry in entries:
            assert spec_servers.entry_is_spec_legal(entry), entry

    def test_env_is_present_even_when_there_is_nothing_to_pin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default-install case, which is the common one."""
        monkeypatch.setattr("kiro_crew.agent._managed_mcp_env", lambda: {}, raising=True)
        for entry in spec_servers.managed_spec_servers():
            assert entry["env"] == []

    def test_the_core_tools_are_delivered(self) -> None:
        """Without these the crew is present but inert."""
        names = {e["name"] for e in spec_servers.managed_spec_servers()}
        assert {"kirocrew-core", "kirocrew-cron"} <= names

    def test_an_opt_in_server_is_withheld(self) -> None:
        """kirocrew-dashboard writes the operator's session layout.

        It is an assignable set. Delivering it to every adapter session grants a
        capability nobody assigned.
        """
        names = {e["name"] for e in spec_servers.managed_spec_servers()}
        assert "kirocrew-dashboard" not in names

    def test_a_gate_closed_server_is_withheld(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.agent._gated_off_servers",
            lambda: frozenset({"kirocrew-core"}),
            raising=True,
        )
        names = {e["name"] for e in spec_servers.managed_spec_servers()}
        assert "kirocrew-core" not in names

    def test_gate_evaluation_failure_withholds_rather_than_guesses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail CLOSED, matching _gated_off_servers' own posture."""

        def boom() -> frozenset[str]:
            raise RuntimeError("keystone unreadable")

        monkeypatch.setattr("kiro_crew.agent._gated_off_servers", boom, raising=True)
        assert spec_servers.managed_spec_servers() == []

    def test_auto_approve_can_never_survive_the_reduction(self) -> None:
        """An auto-approved MCP tool never reaches HookManager.on_tool_call.

        That is the only place the bundled deny rules, the sensitive-path block and
        the governance ceiling run, so the key must not reach an adapter.
        """
        reduced = spec_servers.reduce_to_spec_keys(
            {
                "name": "x",
                "command": "c",
                "args": [],
                "env": [],
                "autoApprove": ["everything"],
                "timeout": 30,
                "vendorKey": True,
            }
        )
        assert "autoApprove" not in reduced
        assert set(reduced) == {"name", "command", "args", "env"}

    def test_the_reducer_supplies_the_required_fields_when_absent(self) -> None:
        """A source entry may legitimately omit args/env; the schema may not.

        Caught by a surviving mutation: the earlier reduction test passed an entry
        that already had both fields, so removing the defaulting changed nothing
        and the test still passed. A pooled stub or an overlay entry can arrive
        without them, and the adapter then rejects the whole session/new.
        """
        reduced = spec_servers.reduce_to_spec_keys({"name": "x", "command": "c"})
        assert reduced == {"name": "x", "command": "c", "args": [], "env": []}
        assert spec_servers.entry_is_spec_legal(reduced)

    def test_a_pooled_stub_wins_a_name_collision(self) -> None:
        """The stub is the addressing layer MCP Apps callbacks route through."""
        managed = [{"name": "kirocrew-core", "command": "direct", "args": [], "env": []}]
        pooled = [{"name": "kirocrew-core", "command": "stub", "args": [], "env": []}]
        merged = spec_servers.merge_session_servers(managed, pooled)
        assert len(merged) == 1
        assert merged[0]["command"] == "stub"

    def test_pooled_entries_are_reduced_too(self) -> None:
        """Operator passthrough keys are copied verbatim onto stubs by the overlay.

        One of them is enough to make a strict deserializer reject the entire
        session/new, so the stubs must be reduced as well as the managed entries.
        """
        pooled = [{"name": "s", "command": "c", "args": [], "env": [], "autoApprove": ["*"]}]
        merged = spec_servers.merge_session_servers([], pooled)
        assert "autoApprove" not in merged[0]


class TestDeliveryIsGatedOnRouting:
    """Crew's control plane is not handed to an adapter Crew cannot govern.

    The tool gate already refuses to START an ungoverned session unless the
    operator sets the named opt-out. But that opt-out accepts the adapter running
    ITS OWN tools ungated, which is a much smaller thing than handing it
    spawn_run/cron_add/learn_add/artifacts to run ungated too. So on the opt-out
    path the session still starts and the adapter still works — it just receives no
    Crew servers.
    """

    def test_kiro_receives_nothing_from_this_seam(self, tmp_path) -> None:
        """Its servers arrive via --agent; the kiro path must pay nothing here."""
        client = _client(ACP_BACKEND_KIRO, tmp_path)
        assert client._spec_session_mcp_servers() == []

    def test_a_routed_spec_adapter_receives_the_servers(self, tmp_path) -> None:
        client = _client(ACP_BACKEND_PI, tmp_path)
        with patch(
            "kiro_crew.acp.tool_gate.resolve_verdict",
            return_value=(Verdict.ROUTED, "delegates"),
        ):
            names = {e["name"] for e in client._spec_session_mcp_servers()}
        assert "kirocrew-core" in names

    @pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_GOOSE])
    def test_post_session_routing_never_receives_servers_preflight(self, backend, tmp_path) -> None:
        client = _client(backend, tmp_path)
        with patch(
            "kiro_crew.acp.tool_gate.resolve_verdict",
            return_value=(Verdict.ROUTED, "planned post-session route"),
        ):
            assert client._spec_session_mcp_servers() == []

    @pytest.mark.parametrize("verdict", [Verdict.INDETERMINATE, Verdict.BYPASSED])
    def test_an_ungoverned_adapter_receives_none(self, verdict, tmp_path) -> None:
        client = _client(ACP_BACKEND_CODEX, tmp_path)
        with patch("kiro_crew.acp.tool_gate.resolve_verdict", return_value=(verdict, "why")):
            assert client._spec_session_mcp_servers() == []

    def test_an_unavailable_verdict_withholds_rather_than_assuming(self, tmp_path) -> None:
        """A verdict that cannot be computed is not a licence to deliver."""
        client = _client(ACP_BACKEND_CODEX, tmp_path)
        with patch(
            "kiro_crew.acp.tool_gate.resolve_verdict",
            side_effect=RuntimeError("cannot read config"),
        ):
            assert client._spec_session_mcp_servers() == []


class TestTheArrayHasOneOwner:
    """Two builders assembling the array separately is how they drift."""

    @pytest.mark.asyncio
    async def test_kiro_gets_the_pooled_list_unchanged(self, tmp_path) -> None:
        """Byte-identical to the previous behaviour on the kiro path."""
        client = _client(ACP_BACKEND_KIRO, tmp_path)
        pooled = [{"name": "stub", "command": "c", "args": [], "env": []}]
        with patch.object(client, "_pooled_mcp_servers", return_value=pooled):
            assert await client._session_mcp_servers() == pooled

    @pytest.mark.asyncio
    async def test_a_spec_adapter_gets_managed_plus_pooled_once(self, tmp_path) -> None:
        """The stubs must not be double-counted.

        Splatting the seam and the pooled list separately at each call site added
        the stubs twice for a spec adapter, and two entries sharing a name is
        undefined in the ACP schema.
        """
        client = _client(ACP_BACKEND_PI, tmp_path)
        pooled = [{"name": "stub", "command": "c", "args": [], "env": []}]
        with patch.object(client, "_pooled_mcp_servers", return_value=pooled):
            with patch(
                "kiro_crew.acp.tool_gate.resolve_verdict",
                return_value=(Verdict.ROUTED, "delegates"),
            ):
                out = await client._session_mcp_servers()
        names = [e["name"] for e in out]
        assert len(names) == len(set(names)), f"duplicate server names: {names}"
        assert "stub" in names
        assert "kirocrew-core" in names

    @pytest.mark.asyncio
    async def test_spec_delivery_resolution_runs_off_the_event_loop(self, tmp_path) -> None:
        """Routing verdict and managed-server discovery read and stat files."""
        import threading

        client = _client(ACP_BACKEND_PI, tmp_path)
        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []

        def delivery() -> tuple[bool, list]:
            seen.append(threading.current_thread())
            return True, []

        with (
            patch.object(client, "_pooled_mcp_servers", return_value=[]),
            patch.object(client, "_spec_session_mcp_delivery", side_effect=delivery),
        ):
            await client._session_mcp_servers()

        assert seen
        assert all(thread is not loop_thread for thread in seen)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_GOOSE])
    async def test_post_session_route_withholds_pooled_stubs_too(self, backend, tmp_path) -> None:
        """No gateway-backed tool may precede the permission-route acknowledgement."""
        client = _client(backend, tmp_path)
        pooled = [{"name": "stub", "command": "c", "args": [], "env": []}]
        with patch.object(client, "_pooled_mcp_servers", return_value=pooled):
            assert await client._session_mcp_servers() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verdict", [Verdict.INDETERMINATE, Verdict.BYPASSED])
    async def test_ungoverned_route_withholds_pooled_stubs_too(self, verdict, tmp_path) -> None:
        """The routing verdict governs the complete control-plane array."""
        client = _client(ACP_BACKEND_PI, tmp_path)
        pooled = [{"name": "stub", "command": "c", "args": [], "env": []}]
        with patch.object(client, "_pooled_mcp_servers", return_value=pooled):
            with patch(
                "kiro_crew.acp.tool_gate.resolve_verdict",
                return_value=(verdict, "not routed"),
            ):
                assert await client._session_mcp_servers() == []

    def test_the_historical_seam_name_still_binds(self, tmp_path) -> None:
        """An internal companion overrides _claude_session_mcp_servers by name."""
        client = _client(ACP_BACKEND_KIRO, tmp_path)
        assert client._claude_session_mcp_servers() == []


class TestUserServersAreNotTransmitted:
    """The load-bearing boundary, and the reason this is not "inject everything".

    A user MCP entry's ``env`` routinely holds tokens and API keys.
    mcp_gateway/session_servers documents the rule: a non-poolable user server is
    left to the agent spec precisely so those never leave the file they were
    declared in. kiro-cli reads them off disk; a spec adapter can only be told over
    the wire, so delivering them would transmit the operator's secrets through a
    third-party binary's stdin.
    """

    def test_only_managed_names_are_shaped(self) -> None:
        managed = spec_servers.managed_spec_servers()
        reserved = spec_servers.reserved_managed_names()
        for entry in managed:
            assert entry["name"] in reserved, (
                f"{entry['name']} is not one of Kiro Crew's managed servers; user "
                "servers must not be transmitted to a third-party adapter"
            )

    def test_a_user_server_cannot_impersonate_a_managed_name(self) -> None:
        """Sanitising must not let a user entry land on a trusted name.

        Fails closed: with nothing reserved, a user-configured ``kirocrew core``
        sanitises to ``kirocrew-core`` and inherits whatever trust that name
        carries.
        """
        reserved = spec_servers.reserved_managed_names()
        assert "kirocrew-core" in reserved
        taken = set(reserved)
        assert spec_servers.safe_server_name("kirocrew core", taken) != "kirocrew-core"
