"""Dialect and capability parity across every registered ACP backend.

Phase 4 converted eleven ``not is_claude`` inferences into positive dialect and
capability tests. These tests exist to stop the inference coming back: each one
is written over *every* registered backend rather than over the two that existed
when the branch was authored, so adding a fifth backend fails here instead of
silently inheriting the kiro arm.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import backends
from kiro_crew.acp.backends import CAP_MID_TURN_STEER, CAP_SESSION_SHARING, Dialect
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_STEER,
    METHOD_SESSION_STEER,
)

ALL_BACKENDS = sorted(ACP_BACKENDS_KNOWN)


def _client(backend: str, tmp_path: Any) -> AcpClient:
    return AcpClient(agent="kirocrew", work_dir=str(tmp_path), acp_backend=backend)


@pytest.mark.parametrize("backend", ALL_BACKENDS)
def test_exactly_one_dialect_holds(backend: str, tmp_path: Any) -> None:
    """A backend speaks exactly one dialect — never both, never neither.

    ``_is_kiro`` and ``_is_spec_adapter`` replaced a single negation, so the two
    together must still partition the backend space. If a future backend
    satisfies neither, code guarded by both predicates is silently skipped.
    """
    client = _client(backend, tmp_path)
    assert client._is_kiro is not client._is_spec_adapter


@pytest.mark.parametrize("backend", ALL_BACKENDS)
def test_dialect_predicates_match_the_descriptor(backend: str, tmp_path: Any) -> None:
    client = _client(backend, tmp_path)
    expected = backends.dialect_of(backend)
    assert client._is_kiro is (expected is Dialect.KIRO)
    assert client._is_spec_adapter is (expected is Dialect.SPEC)


def test_kiro_and_kas_share_the_kiro_dialect(tmp_path: Any) -> None:
    """KAS speaks kiro's dialect, so it must keep taking the kiro arm.

    This is the regression that matters most for the phase: KAS rode eight of
    the eleven negations, so converting them must not move KAS anywhere.
    """
    assert _client(ACP_BACKEND_KIRO, tmp_path)._is_kiro
    assert _client(ACP_BACKEND_KAS, tmp_path)._is_kiro


def test_both_adapters_are_spec_dialect(tmp_path: Any) -> None:
    assert _client(ACP_BACKEND_CLAUDE, tmp_path)._is_spec_adapter
    assert _client(ACP_BACKEND_CODEX, tmp_path)._is_spec_adapter
    assert _client(ACP_BACKEND_GOOSE, tmp_path)._is_spec_adapter
    assert _client(ACP_BACKEND_OPENCODE, tmp_path)._is_spec_adapter
    assert _client(ACP_BACKEND_PI, tmp_path)._is_spec_adapter


@pytest.mark.parametrize("backend", ALL_BACKENDS)
def test_supports_steer_is_declared_not_inferred(backend: str, tmp_path: Any) -> None:
    client = _client(backend, tmp_path)
    assert client.supports_steer is backends.supports(backend, CAP_MID_TURN_STEER)


def test_kas_steer_is_verified(tmp_path: Any) -> None:
    """KAS shares kiro-cli's ``_session/steer``; spec adapters do not."""
    assert _client(ACP_BACKEND_KIRO, tmp_path).supports_steer
    assert _client(ACP_BACKEND_KAS, tmp_path).supports_steer
    assert ACP_BACKEND_KAS in ACP_BACKENDS_STEER
    assert not _client(ACP_BACKEND_CLAUDE, tmp_path).supports_steer
    assert not _client(ACP_BACKEND_GOOSE, tmp_path).supports_steer
    assert not _client(ACP_BACKEND_OPENCODE, tmp_path).supports_steer
    assert not _client(ACP_BACKEND_PI, tmp_path).supports_steer
    for backend in (
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
    ):
        assert backend not in ACP_BACKENDS_STEER


@pytest.mark.asyncio
async def test_kas_steer_sends_the_measured_method(tmp_path: Any) -> None:
    """KAS steer writes ``_session/steer`` and does not await a response."""
    client = _client(ACP_BACKEND_KAS, tmp_path)
    client._session_id = "kas-session"
    client._send_request = AsyncMock(return_value=7)
    assert await client.steer("focus on the tests") is True
    client._send_request.assert_awaited_once_with(
        METHOD_SESSION_STEER,
        {
            "sessionId": "kas-session",
            "message": "<user_message>\nfocus on the tests\n</user_message>",
        },
    )


@pytest.mark.parametrize(
    "backend",
    [
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
    ],
)
@pytest.mark.asyncio
async def test_spec_adapter_steer_refuses_without_sending(backend: str, tmp_path: Any) -> None:
    """A missing ``_session/steer`` is a typed refusal, not a hang."""
    client = _client(backend, tmp_path)
    client._session_id = "adapter-session"
    client._send_request = AsyncMock(return_value=1)
    assert await client.steer("correct the task") is False
    client._send_request.assert_not_awaited()


@pytest.mark.parametrize(
    "backend",
    [
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
    ],
)
@pytest.mark.asyncio
async def test_spec_backends_set_model_via_config_option(backend: str, tmp_path: Any) -> None:
    client = _client(backend, tmp_path)
    client._session_id = "session-1"
    client.set_config_option = AsyncMock()
    client._send_request = AsyncMock()

    await client.set_model("backend-model")

    client.set_config_option.assert_awaited_once_with("model", "backend-model")
    client._send_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_kiro_set_model_keeps_the_native_method(tmp_path: Any) -> None:
    client = _client(ACP_BACKEND_KIRO, tmp_path)
    client._session_id = "session-1"
    client.set_config_option = AsyncMock()
    client._send_request = AsyncMock(return_value=1)

    await client.set_model("kiro-model")

    client.set_config_option.assert_not_awaited()
    client._send_request.assert_awaited_once()


class TestRuntimeCapabilityGate:
    """AcpRuntime serves the backends routed to it, and refuses the rest.

    The gate asks about membership in ACP_BACKENDS_ACP_RUNTIME, NOT about
    CAP_SESSION_SHARING. Those are different questions and the sets differ on
    purpose: KAS runs on this runtime while being held out of session sharing
    until keep-aware teardown lands. Gating on the capability took KAS out
    entirely — `is_acp_runtime_backend` routed it here and the guard then refused
    it — so the tests below pin membership, and the KAS case is called out
    explicitly because a capability-shaped guard passes every other backend and
    fails only that one.
    """

    @pytest.mark.parametrize(
        "backend",
        [b for b in ALL_BACKENDS if b not in ACP_BACKENDS_ACP_RUNTIME],
    )
    @pytest.mark.asyncio
    async def test_spawn_refuses_a_backend_it_does_not_serve(
        self, backend: str, tmp_path: Any
    ) -> None:
        runtime = AcpRuntime(agent="kirocrew", work_dir=str(tmp_path), acp_backend=backend)
        with pytest.raises(AcpRuntimeError, match="not served by AcpRuntime"):
            await runtime.spawn()

    def test_kas_is_not_refused_despite_lacking_session_sharing(self) -> None:
        """The regression this gate caused, pinned without side effects.

        KAS is in ACP_BACKENDS_ACP_RUNTIME and NOT in ACP_BACKENDS_SESSION_SHARING,
        so it is the one backend a capability-shaped guard gets wrong — every other
        backend gives the same answer either way, which is why this went unnoticed.
        Asserted by source inspection because exercising spawn() would exec a real
        process.
        """
        import inspect

        assert ACP_BACKEND_KAS in ACP_BACKENDS_ACP_RUNTIME
        assert not backends.supports(ACP_BACKEND_KAS, CAP_SESSION_SHARING)

        source = inspect.getsource(AcpRuntime._spawn_admitted)
        assert (
            "ACP_BACKENDS_ACP_RUNTIME" in source
        ), "the runtime gate must ask about membership in the set it serves"
        # The CALL, not the word: the comment above the guard names the constant
        # precisely to explain why it is the wrong question.
        assert (
            "supports(" not in source
        ), "gating spawn on a capability lookup refuses KAS, which is routed here"

    @pytest.mark.asyncio
    async def test_gate_refuses_before_touching_the_filesystem(self, tmp_path: Any) -> None:
        """The refusal must precede any side effect.

        A gate that fires after the work dir is created or the binary resolved
        leaves residue for a session that was never allowed to start.
        """
        work_dir = tmp_path / "never-created"
        runtime = AcpRuntime(
            agent="kirocrew", work_dir=str(work_dir), acp_backend=ACP_BACKEND_CODEX
        )
        with pytest.raises(AcpRuntimeError):
            await runtime.spawn()
        assert not work_dir.exists()


class TestStartArmAgreesWithEligibility:
    """start() routes on eligibility, so the two can never disagree."""

    @pytest.mark.parametrize("backend", ALL_BACKENDS)
    def test_eligibility_matches_the_capability(self, backend: str) -> None:
        """Exercises the real property against a stub client.

        Read through the descriptor rather than restating the expected value per
        backend, so a capability change in the table is picked up here instead of
        needing this test edited alongside it.
        """
        from kiro_crew.providers.acp import AcpProvider

        client = MagicMock()
        client.backend = backend
        eligible = AcpProvider.is_session_sharing_eligible.fget(  # type: ignore[attr-defined]
            _StubProvider(client)
        )
        assert eligible is backends.supports(backend, CAP_SESSION_SHARING)

    def test_start_routes_on_a_named_set_not_a_negation(self) -> None:
        """start() must route on positive membership, never on "not claude".

        This REPLACES an earlier assertion that start() had to read
        ``is_session_sharing_eligible`` itself. That coupling is wrong: main
        documents ``ACP_BACKENDS_ACP_RUNTIME`` as a deliberate SUPERSET of
        ``ACP_BACKENDS_SESSION_SHARING`` — running on the shared runtime is
        necessary for sharing but not sufficient, and KAS runs there while being
        held out of sharing until keep-aware teardown lands. Forcing the two
        expressions to agree would stop KAS starting on the shared runtime.

        What must still hold is harness parity H5: the arm is chosen by a named
        set, so a backend added later cannot inherit the kiro-family path by
        being "not claude". Pinned by source inspection because exercising
        start() would spawn a subprocess.
        """
        import inspect

        from kiro_crew.providers.acp import AcpProvider

        source = inspect.getsource(AcpProvider.start)
        assert "is_acp_runtime_backend" in source
        assert "is_claude_backend" not in source


class _StubProvider:
    """Minimal stand-in exposing only what the property under test reads."""

    def __init__(self, client: Any) -> None:
        self._client = client
