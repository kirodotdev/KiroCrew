"""A masked harness's startup auth failure names the credential mask.

An enforced adapter that re-exposes ``~/.aws/config`` runs with the rest of
``~/.aws`` hidden on purpose. When such a child fails ``initialize``,
``session/new`` or its first turn because the AWS SDK could not load a
credential, the surfaced error says so once, so the deliberate layout is not
reported as a broken control.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpAuthRequired, AcpError
from kiro_crew.acp.runtime import AcpRuntimeError
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.transport_errors import _raise_acp_error
from kiro_crew.acp.types import ACP_BACKEND_CODEX, ACP_BACKEND_KIRO, ACP_BACKEND_OPENCODE
from kiro_crew.agent_sdk import host_auth
from kiro_crew.agent_sdk.tool_gate import label_for
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.acp_runtime_start import (
    CREDENTIAL_MASK_AUTH_NOTE,
    _with_credential_mask_note,
)
from kiro_crew.testing.ids import UNALLOCATABLE_PID

_AUTH_RPC_ERROR = "RPC error: {'code': -32000, 'message': 'Authentication required'}"
_AWS_STDERR = "ERROR codex: failed to load AWS credentials"


def _note(backend: str = ACP_BACKEND_CODEX) -> str:
    return CREDENTIAL_MASK_AUTH_NOTE.format(label=label_for(backend))


def _aws_mask() -> tuple[str, ...]:
    return (os.path.join(os.path.expanduser("~"), ".aws"),)


def _runtime(
    *,
    backend: str = ACP_BACKEND_CODEX,
    hidden: tuple[str, ...] | None = None,
    stderr: str = "",
    spawn_exc: BaseException | None = None,
    session_exc: BaseException | None = None,
    signed_out: bool = False,
) -> MagicMock:
    runtime = MagicMock()
    runtime.pid = UNALLOCATABLE_PID
    runtime.acp_backend = backend
    runtime.sandbox_hidden_dirs = _aws_mask() if hidden is None else hidden
    runtime.redacted_stderr_tail = MagicMock(return_value=stderr)
    runtime.spawn = AsyncMock(side_effect=spawn_exc)
    runtime.saw_not_logged_in = MagicMock(return_value=signed_out)
    runtime.saw_sandbox_init_failure = MagicMock(return_value=False)
    runtime.saw_delegate_launch_failure = MagicMock(return_value=False)
    runtime.settle_stderr = AsyncMock()
    runtime.kill = AsyncMock()
    handle = MagicMock(session_id="codex-sess-1", set_model=AsyncMock())
    runtime.create_session = AsyncMock(return_value=handle, side_effect=session_exc)
    return runtime


def _provider(backend: str = ACP_BACKEND_CODEX) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    client = MagicMock()
    client.backend = backend
    client._work_dir = "/tmp/ws"
    client._agent = "kirocrew"
    client._sandbox_mode = "auto"
    client._extra_env = {}
    client._mcp_gateway_overlay = None
    client._mcp_gateway_socket = None
    client._resume_session_id = ""
    client._model = "auto"
    provider._client = client
    return provider


async def _start_error(runtime: MagicMock, backend: str = ACP_BACKEND_CODEX) -> BaseException:
    provider = _provider(backend)
    with (
        patch("kiro_crew.providers.acp.AcpRuntime", return_value=runtime),
        patch("kiro_crew.metrics.provider.get_recorder", return_value=MagicMock()),
    ):
        with pytest.raises(BaseException) as caught:
            await provider._start_kiro_runtime()
    return caught.value


@pytest.mark.asyncio
async def test_an_auth_answer_at_initialize_names_the_mask_once():
    error = await _start_error(
        _runtime(stderr=_AWS_STDERR, spawn_exc=AcpRuntimeError(_AUTH_RPC_ERROR))
    )
    assert isinstance(error, AcpRuntimeError)
    text = str(error)
    assert text.startswith(_AUTH_RPC_ERROR)
    assert text.count(_note()) == 1


@pytest.mark.asyncio
async def test_an_auth_answer_at_session_new_names_the_mask_once():
    error = await _start_error(
        _runtime(stderr=_AWS_STDERR, session_exc=AcpRuntimeError(_AUTH_RPC_ERROR))
    )
    assert isinstance(error, AcpRuntimeError)
    assert str(error).count(_note()) == 1


@pytest.mark.asyncio
async def test_an_auth_answer_at_the_respawn_after_a_dead_runtime_names_the_mask_once():
    dead = _runtime()
    dead.is_alive = MagicMock(return_value=False)
    respawned = _runtime(stderr=_AWS_STDERR, spawn_exc=AcpRuntimeError(_AUTH_RPC_ERROR))
    runtimes = iter([dead, respawned])
    provider = _provider()
    with (
        patch("kiro_crew.providers.acp.AcpRuntime", side_effect=lambda **kw: next(runtimes)),
        patch("kiro_crew.metrics.provider.get_recorder", return_value=MagicMock()),
    ):
        with pytest.raises(AcpRuntimeError) as caught:
            await provider._start_kiro_runtime()
    respawned.spawn.assert_awaited_once()
    assert str(caught.value).count(_note()) == 1


@pytest.mark.asyncio
async def test_the_stderr_signature_names_the_mask_on_the_signed_out_path():
    runtime = _runtime(
        spawn_exc=AcpRuntimeError("process exited"),
        stderr=_AWS_STDERR,
        signed_out=True,
    )
    error = await _start_error(runtime)
    assert isinstance(error, AcpAuthRequired)
    text = str(error)
    assert text.startswith(host_auth.signed_out_message(ACP_BACKEND_CODEX))
    assert text.count(_note()) == 1


@pytest.mark.asyncio
async def test_an_unmasked_spawn_gets_no_note():
    runtime = _runtime(hidden=(), stderr=_AWS_STDERR, spawn_exc=AcpRuntimeError(_AUTH_RPC_ERROR))
    error = await _start_error(runtime)
    assert str(error) == _AUTH_RPC_ERROR


@pytest.mark.asyncio
async def test_a_failure_that_is_not_an_auth_answer_gets_no_note():
    error = await _start_error(_runtime(spawn_exc=AcpRuntimeError("connection reset")))
    assert str(error) == "connection reset"


@pytest.mark.asyncio
async def test_a_generic_auth_answer_with_no_aws_cause_gets_no_note():
    # A codex signed out of a non-AWS provider answers with the same -32000 text.
    error = await _start_error(_runtime(spawn_exc=AcpRuntimeError(_AUTH_RPC_ERROR)))
    assert str(error) == _AUTH_RPC_ERROR


@pytest.mark.asyncio
async def test_a_harness_without_the_config_exposure_gets_no_note():
    runtime = _runtime(
        backend=ACP_BACKEND_OPENCODE,
        stderr=_AWS_STDERR,
        spawn_exc=AcpRuntimeError(_AUTH_RPC_ERROR),
    )
    error = await _start_error(runtime, ACP_BACKEND_OPENCODE)
    assert str(error) == _AUTH_RPC_ERROR


def test_the_note_is_never_added_twice():
    runtime = _runtime(stderr=_AWS_STDERR)
    cause = AcpRuntimeError(_AUTH_RPC_ERROR)
    once = _with_credential_mask_note(runtime, _AUTH_RPC_ERROR, cause)
    twice = _with_credential_mask_note(runtime, once, cause)
    assert twice == once
    assert twice.count(_note()) == 1


def _kiro_runtimes(path: str, *, signed_out: bool) -> list[MagicMock]:
    exc = AcpRuntimeError(_AUTH_RPC_ERROR)
    if path == "initialize":
        return [_runtime(backend=ACP_BACKEND_KIRO, spawn_exc=exc, signed_out=signed_out)]
    if path == "session_new":
        return [_runtime(backend=ACP_BACKEND_KIRO, session_exc=exc, signed_out=signed_out)]
    dead = _runtime(backend=ACP_BACKEND_KIRO)
    dead.is_alive = MagicMock(return_value=False)
    respawned = _runtime(backend=ACP_BACKEND_KIRO, spawn_exc=exc, signed_out=signed_out)
    return [dead, respawned]


@pytest.mark.asyncio
@pytest.mark.parametrize("signed_out", [False, True])
@pytest.mark.parametrize("path", ["initialize", "session_new", "respawn"])
async def test_the_kiro_start_path_never_calls_the_mask_helpers(path: str, signed_out: bool):
    # harness-parity H13: kiro compares the backend against the table and stops.
    runtimes = iter(_kiro_runtimes(path, signed_out=signed_out))
    provider = _provider(ACP_BACKEND_KIRO)
    with (
        patch("kiro_crew.providers.acp.AcpRuntime", side_effect=lambda **kw: next(runtimes)),
        patch("kiro_crew.metrics.provider.get_recorder", return_value=MagicMock()),
        patch("kiro_crew.providers.acp_runtime_start._with_credential_mask_note") as note_helper,
        patch("kiro_crew.providers.acp_runtime_start._name_credential_mask") as name_helper,
    ):
        with pytest.raises((AcpAuthRequired, AcpRuntimeError)) as caught:
            await provider._start_kiro_runtime()
    note_helper.assert_not_called()
    name_helper.assert_not_called()
    expected = host_auth.signed_out_message(ACP_BACKEND_KIRO) if signed_out else _AUTH_RPC_ERROR
    assert str(caught.value) == expected


# The frame codex-acp 1.11.0 ends a turn with when Bedrock cannot load a credential
# after session/new succeeded (test/fixtures/acp_frames/README.md, codex row).
_TURN_AWS_FRAME = {
    "code": -32603,
    "message": "Internal error",
    "data": "stream disconnected before completion: failed to load AWS credentials: "
    "the credentials provider was not properly configured",
}
_TURN_OTHER_FRAME = {"code": -32603, "message": "Internal error", "data": "model overloaded"}


def _turn_provider(
    frame: dict,
    *,
    backend: str = ACP_BACKEND_CODEX,
    hidden: tuple[str, ...] | None = None,
    stderr: str = "",
) -> AcpSessionProvider:
    """A real session provider over a handle whose first prompt fails with *frame*."""
    handle = MagicMock()
    handle.session_id = "codex-sess-1"
    handle.memory_mode = "persistent"

    async def prompt(_message: str):
        _raise_acp_error(frame, backend=backend)
        yield  # pragma: no cover - makes this an async generator

    handle.prompt = prompt
    runtime = _runtime(backend=backend, hidden=hidden, stderr=stderr)
    runtime.is_alive.return_value = True
    runtime._process = MagicMock(returncode=None)
    runtime._last_activity = 0.0
    return AcpSessionProvider(handle, runtime)


async def _turn_error(provider: AcpSessionProvider) -> AcpError:
    with pytest.raises(AcpError) as caught:
        async for _event in provider.stream("hello"):
            pass
    return caught.value


@pytest.mark.asyncio
async def test_an_aws_auth_failure_at_the_first_turn_names_the_mask_once():
    error = await _turn_error(_turn_provider(_TURN_AWS_FRAME))
    text = str(error)
    assert text.startswith(_TURN_AWS_FRAME["data"])
    assert text.count(_note()) == 1
    # Only the text changes: the classifier's verdict rides on the same object.
    assert error.transient is False


@pytest.mark.asyncio
async def test_a_turn_failure_that_is_not_an_aws_auth_failure_gets_no_note():
    error = await _turn_error(_turn_provider(_TURN_OTHER_FRAME))
    assert _note() not in str(error)


@pytest.mark.asyncio
async def test_an_old_aws_stderr_line_does_not_name_the_mask_on_a_later_turn_failure():
    # The stderr tail spans the runtime's life; a turn matches its own error only.
    error = await _turn_error(_turn_provider(_TURN_OTHER_FRAME, stderr=_AWS_STDERR))
    assert _note() not in str(error)


@pytest.mark.asyncio
async def test_an_unmasked_turn_failure_gets_no_note():
    error = await _turn_error(_turn_provider(_TURN_AWS_FRAME, hidden=()))
    assert str(error) == _TURN_AWS_FRAME["data"]


@pytest.mark.asyncio
async def test_a_turn_failure_that_already_names_the_mask_is_not_doubled():
    noted = dict(_TURN_AWS_FRAME, data=f"{_TURN_AWS_FRAME['data']} {_note()}")
    error = await _turn_error(_turn_provider(noted))
    assert str(error).count(_note()) == 1


@pytest.mark.asyncio
async def test_the_kiro_turn_path_never_calls_the_mask_helper():
    # harness-parity H13: kiro compares the backend against the table and stops.
    provider = _turn_provider(_TURN_AWS_FRAME, backend=ACP_BACKEND_KIRO)
    with patch("kiro_crew.providers.acp_runtime_start._name_credential_mask") as name_helper:
        error = await _turn_error(provider)
    name_helper.assert_not_called()
    assert _note(ACP_BACKEND_KIRO) not in str(error)
