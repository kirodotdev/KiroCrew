"""goose is ROUTED via session/request_permission; unverified adapters still refuse.

goose's ACP path asks per privileged tool. Kiro Crew does not advertise ``fs/*``
or ``terminal/*``, so file I/O stays in-process, but the permission frame still
reaches PreToolUse. That is ``Routing.PERMISSION_REQUEST``, not UNVERIFIED.

The fail-closed default for every adapter Kiro Crew has not verified stays: a
synthesised descriptor (not a registered backend) is UNVERIFIED, resolves
INDETERMINATE, and refuses unless the operator sets the one named opt-out.

Governability cannot be discovered from ``initialize``. Both a ROUTED adapter
and an unverified one can advertise the same capability shape, which is why
the unverified default has to refuse rather than probe and hope.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import backends, tool_gate
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.codex import Verdict
from kiro_crew.acp.types import (
    ACP_BACKEND_GOOSE,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SELECTABLE,
    ACP_BACKENDS_SESSION_SHARING,
    ACP_BACKENDS_STEER,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
)

_SYNTHETIC_UNVERIFIED = "example-acp"


class TestGooseDescriptor:
    def test_goose_is_known_but_withheld_from_the_initial_preview(self) -> None:
        assert ACP_BACKEND_GOOSE in ACP_BACKENDS_KNOWN
        assert ACP_BACKEND_GOOSE not in ACP_BACKENDS_SELECTABLE

    def test_goose_routing_is_permission_request(self) -> None:
        descriptor = backends.descriptor_for(ACP_BACKEND_GOOSE)
        assert descriptor.routing is backends.Routing.PERMISSION_REQUEST

    def test_goose_speaks_the_spec_dialect_not_kiros(self) -> None:
        """``_meta.kiro`` extensions are kiro-cli's, not a third party's."""
        assert backends.dialect_of(ACP_BACKEND_GOOSE) is backends.Dialect.SPEC

    def test_goose_gets_no_seatbelt_waiver(self) -> None:
        """The one capability that fails OPEN (harness parity H7).

        Membership makes ``sandbox.wrap_argv`` skip Kiro Crew's own confinement in
        favour of the harness's internal sandbox. goose does not ship one, so
        granting this would leave the agent process unconfined.
        """
        assert ACP_BACKEND_GOOSE not in ACP_BACKENDS_INTERNAL_SANDBOX

    def test_goose_claims_no_kiro_family_capability(self) -> None:
        """It runs one process per session and implements no steer extension."""
        assert ACP_BACKEND_GOOSE not in ACP_BACKENDS_SESSION_SHARING
        assert ACP_BACKEND_GOOSE not in ACP_BACKENDS_STEER

    def test_goose_needs_no_adapter_install(self) -> None:
        """`goose acp` is served by the goose binary, not a separate package."""
        assert backends.descriptor_for(ACP_BACKEND_GOOSE).install_command == ""

    def test_native_resume_is_unavailable_as_the_handshake_reports(self) -> None:
        """Confirmed on the wire against goose 1.46.0, not guessed.

        Its ``initialize`` advertises ``sessionCapabilities`` of ``list`` and
        ``close`` only — no ``resume`` and no ``fork``, where claude offers both.
        The descriptor level has to match what the adapter actually serves, because
        a resume attempted against an adapter that cannot do it fails mid-session
        rather than at selection.
        """
        assert (
            backends.level(ACP_BACKEND_GOOSE, backends.CAP_NATIVE_RESUME)
            is backends.Level.UNAVAILABLE
        )

    def test_goose_profiles_work_differently_but_tool_search_is_absent(self) -> None:
        """Prompts and skills are injected; restricted tool profiles are refused."""
        assert (
            backends.level(ACP_BACKEND_GOOSE, backends.CAP_AGENT_PROFILES)
            is backends.Level.DEGRADED
        )
        assert (
            backends.level(ACP_BACKEND_GOOSE, backends.CAP_TOOL_SEARCH)
            is backends.Level.UNAVAILABLE
        )


class TestGooseIsRoutedViaPermissionRequest:
    def test_verdict_is_routed(self, tmp_path: Path) -> None:
        verdict, reason = tool_gate.resolve_verdict(ACP_BACKEND_GOOSE, tmp_path)
        assert verdict is Verdict.ROUTED
        assert "pins goose session mode approve" in reason

    def test_enforce_succeeds_without_the_opt_out(self, tmp_path: Path) -> None:
        tool_gate.enforce(ACP_BACKEND_GOOSE, tmp_path, allow_ungated=False)

    def test_the_named_opt_out_still_permits_startup(self, tmp_path: Path) -> None:
        tool_gate.enforce(ACP_BACKEND_GOOSE, tmp_path, allow_ungated=True)

    def test_nothing_is_written_into_the_work_dir(self, tmp_path: Path) -> None:
        tool_gate.resolve_verdict(ACP_BACKEND_GOOSE, tmp_path)
        assert list(tmp_path.iterdir()) == []


class TestUnverifiedAdapterFailsClosed:
    """The generic path for the adapters Kiro Crew has NOT verified.

    Exercised through a SYNTHESISED descriptor rather than a registered backend
    id, because registering one would defeat the point: the case under test is
    precisely an adapter absent from the hand-written table. ``pi-acp`` is no
    longer usable here — ``canonical_backend_id`` maps it onto the hand-written
    ``pi`` descriptor.
    """

    def test_synthesised_descriptor_is_unverified(self) -> None:
        descriptor = backends.descriptor_for_registry_adapter(_SYNTHETIC_UNVERIFIED, "example ACP")
        assert descriptor.routing is backends.Routing.UNVERIFIED

    def test_it_marks_every_capability_unverified(self) -> None:
        """A capability is a claim about observed behaviour; nothing was observed."""
        descriptor = backends.descriptor_for_registry_adapter(_SYNTHETIC_UNVERIFIED)
        for capability in backends.ALL_CAPABILITIES:
            assert descriptor.capabilities[capability] is backends.Level.UNVERIFIED

    def test_it_never_receives_the_seatbelt_waiver(self) -> None:
        """Third-party code of unknown provenance is always wrapped.

        Holds by construction: the waiver is granted by explicit listing in
        acp/types.py, and a synthesised descriptor is listed nowhere.
        """
        descriptor = backends.descriptor_for_registry_adapter("anything-at-all")
        assert descriptor.id not in ACP_BACKENDS_INTERNAL_SANDBOX
        assert descriptor.id not in ACP_BACKENDS_KNOWN

    def test_verdict_is_indeterminate_never_bypassed(self, unverified: str) -> None:
        """Kiro Crew does not claim the adapter ignores permissions.

        It claims only that it has no evidence — and absent evidence, the gate
        must not be reported as armed.
        """
        verdict, reason = tool_gate.resolve_verdict(unverified, "/tmp")
        assert verdict is Verdict.INDETERMINATE
        assert "not established" in reason

    def test_enforce_refuses_by_default(self, unverified: str, tmp_path: Path) -> None:
        with pytest.raises(tool_gate.ToolGateUnroutable) as excinfo:
            tool_gate.enforce(unverified, tmp_path, allow_ungated=False)
        message = str(excinfo.value)
        # The refusal must say what is not being enforced and name the one opt-out,
        # or an operator cannot tell a policy refusal from a crash.
        assert "denied-command" in message
        assert "sensitive-path" in message
        assert "acp_backend_allow_ungated_tools" in message

    def test_the_named_opt_out_still_works(self, unverified: str, tmp_path: Path) -> None:
        """One documented escape hatch, so an operator is never simply stuck."""
        tool_gate.enforce(unverified, tmp_path, allow_ungated=True)

    def test_remediation_does_not_invent_a_setting(self, unverified: str) -> None:
        """There is no config change that verifies an adapter, so promise none."""
        hint = tool_gate.remediation_for(unverified, "/tmp")
        assert "verified" in hint
        assert "approval_policy" not in hint
        assert "defaultMode" not in hint


@pytest.fixture
def unverified(monkeypatch) -> str:
    """Make ``descriptor_for`` resolve one synthesised, unverified adapter.

    Only that id is intercepted; every other backend still resolves through the
    real table, so a test cannot pass because lookups were broken wholesale.
    """
    descriptor = backends.descriptor_for_registry_adapter(_SYNTHETIC_UNVERIFIED, "example ACP")
    original = backends.descriptor_for

    def fake(
        backend: str,
        *,
        registry_adapters: object = None,
    ) -> backends.BackendDescriptor:
        return (
            descriptor
            if backend == descriptor.id
            else original(backend, registry_adapters=registry_adapters)
        )

    monkeypatch.setattr(backends, "descriptor_for", fake)
    return descriptor.id


@pytest.mark.asyncio
async def test_goose_skips_session_load_when_native_resume_is_unavailable(
    tmp_path: Path,
) -> None:
    """A handshake that advertises loadSession must still not attempt load.

    goose 1.47 advertises loadSession and session/load can succeed, but
    transcript restore is unmeasured. Crew skips load so spawn_continue
    fail-closes on ``resumed is False`` with a named reason instead of
    starting a blank child.
    """
    client = AcpClient(agent="kirocrew", work_dir=str(tmp_path), acp_backend=ACP_BACKEND_GOOSE)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc
    client._resume_session_id = "goose-sid"
    sent: list[str] = []

    async def fake_send(method: str, params: dict) -> int:
        sent.append(method)
        return len(sent)

    async def fake_wait(
        req_id: int,
        timeout: float = 50.0,
        *,
        method: str = "",
        expected_mcp: object = None,
    ) -> dict:
        if req_id == 1:
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
        return {"sessionId": "fresh-goose"}

    client._send_request = AsyncMock(side_effect=fake_send)
    client._wait_for_response = AsyncMock(side_effect=fake_wait)
    client._drain_notifications = AsyncMock()
    client._session_mcp_servers = AsyncMock(return_value=[])

    await client._initialize_session()

    assert METHOD_SESSION_LOAD not in sent
    assert METHOD_SESSION_NEW in sent
    assert client._resumed is False
    assert client._session_id == "fresh-goose"


@pytest.mark.asyncio
async def test_unverified_adapter_skips_session_load_even_when_advertised(
    unverified: str,
    tmp_path: Path,
) -> None:
    """An advertised RPC does not prove that transcript restoration works."""
    client = AcpClient(agent="kirocrew", work_dir=str(tmp_path), acp_backend=unverified)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc
    client._resume_session_id = "unverified-sid"
    sent: list[str] = []

    async def fake_send(method: str, params: dict) -> int:
        sent.append(method)
        return len(sent)

    async def fake_wait(
        req_id: int,
        timeout: float = 50.0,
        *,
        method: str = "",
        expected_mcp: object = None,
    ) -> dict:
        if req_id == 1:
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
        if sent[-1] == METHOD_SESSION_LOAD:
            return {"modes": {"availableModes": []}}
        return {"sessionId": "fresh-unverified"}

    client._send_request = AsyncMock(side_effect=fake_send)
    client._wait_for_response = AsyncMock(side_effect=fake_wait)
    client._drain_notifications = AsyncMock()
    client._session_mcp_servers = AsyncMock(return_value=[])

    await client._initialize_session()

    assert METHOD_SESSION_LOAD not in sent
    assert METHOD_SESSION_NEW in sent
    assert client._resumed is False
    assert client._session_id == "fresh-unverified"


def test_goose_spawn_continue_names_the_harness_when_resume_is_unavailable() -> None:
    """spawn_continue fail-closes with a named reason, not a blank child."""
    from kiro_crew.subagent import _resume_failed_message

    client = MagicMock()
    client.backend = ACP_BACKEND_GOOSE
    message = _resume_failed_message(client, "subagent:child-1")
    assert message.startswith("resume_failed:")
    assert "does not support native resume" in message
    assert "subagent:child-1" in message
    assert "goose" in message.lower()
