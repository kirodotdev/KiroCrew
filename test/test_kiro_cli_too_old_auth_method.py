"""A Kiro CLI too old for ``--auth-method`` must say "update Kiro CLI", once.

Kiro Crew starts every KAS relay session as ``kiro-cli acp --agent-engine v3
--auth-method cli``. A Kiro CLI that predates the option stops at argument parse:

    error: unexpected argument '--auth-method' found

Before this fix the death reached the user as the generic ``process exited
(rc=None)`` card and the retry ladder respawned into the same wall. The failure
is deterministic (same argv, same binary), so it must be classified once, as a
non-retryable error whose message names the update and the dashboard control
that runs it, and the dashboard must then show that control.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from kiro_prerequisite_helpers import _agents_dir_never_the_real_home  # noqa: F401
from kiro_prerequisite_helpers import _isolate_agent_specs  # noqa: F401
from kiro_prerequisite_helpers import _FakeRuntime, _make_executable, _no_audit

from kiro_crew.acp.transport_errors import (
    DELEGATE_LAUNCH_FAILURE_MESSAGE,
    KIRO_CLI_TOO_OLD_MESSAGE,
    AcpDelegateLaunchFailed,
    AcpError,
    AcpProcessDied,
    delegate_launch_failure_message,
    is_delegate_launch_failure_output,
)
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService, ProcessResult

# The stderr a user reported, trimmed to its lines.
TOO_OLD_STDERR = [
    "error: unexpected argument '--auth-method' found",
    "",
    "  tip: to pass '--auth-method' as a value, use '-- --auth-method'",
    "",
    "Usage: kiro-cli-chat acp [OPTIONS]",
    "",
    "For more information, try '--help'.",
]


# -- The classifier --


def test_the_reported_stderr_is_classified_too_old():
    text = "\n".join(TOO_OLD_STDERR)
    assert delegate_launch_failure_message(text) == KIRO_CLI_TOO_OLD_MESSAGE
    assert is_delegate_launch_failure_output(text) is True


def test_the_message_names_the_update_and_its_dashboard_control():
    assert "too old" in KIRO_CLI_TOO_OLD_MESSAGE
    assert "Update Kiro CLI" in KIRO_CLI_TOO_OLD_MESSAGE
    assert "retrying cannot help" in KIRO_CLI_TOO_OLD_MESSAGE
    # Users have no terminal: a shell command must not be the only way out.
    assert "kiro-cli update" not in KIRO_CLI_TOO_OLD_MESSAGE


@pytest.mark.parametrize(
    "text",
    [
        # Any other unknown option is not the too-old signature this can fix.
        "error: unexpected argument '--agent' found",
        "error: unexpected argument '--auth' found",
        "process exited (rc=None)",
        "Error: Failed to spawn child process",
    ],
    ids=["other-option", "prefix-option", "generic-death", "spawn-failure"],
)
def test_a_control_tail_is_not_too_old(text):
    assert delegate_launch_failure_message(text) is None


def test_the_delegate_launch_signature_keeps_its_own_message():
    assert (
        delegate_launch_failure_message("error: failed to launch kiro-cli-chat")
        == DELEGATE_LAUNCH_FAILURE_MESSAGE
    )


# -- The runtime latch and its translation --


class _FakeStream:
    def __init__(self, lines):
        self._lines = [f"{line}\n".encode() for line in lines]

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _FakeProcess:
    def __init__(self, lines):
        self.stderr = _FakeStream(lines)


def _runtime(lines):
    from kiro_crew.acp.runtime import AcpRuntime

    runtime = AcpRuntime.__new__(AcpRuntime)
    runtime.recording_allowed = True
    runtime._stderr_lines = []
    runtime._saw_auth_failure = False
    runtime._saw_sandbox_init_failure = False
    runtime._saw_delegate_launch_failure = False
    runtime._delegate_launch_failure_message = None
    runtime._initialized = False
    runtime._first_session_ready = False
    runtime._process = _FakeProcess(lines)
    return runtime


@pytest.mark.asyncio
async def test_the_runtime_latches_the_too_old_message():
    runtime = _runtime(TOO_OLD_STDERR)
    await runtime._drain_stderr()
    assert runtime.saw_delegate_launch_failure() is True
    assert runtime.delegate_launch_failure_text() == KIRO_CLI_TOO_OLD_MESSAGE


@pytest.mark.asyncio
async def test_the_provider_raises_one_non_retryable_too_old_error():
    from kiro_crew.llm_helpers import acp_error_is_transient
    from kiro_crew.providers.acp_runtime_start import _delegate_launch_failure

    runtime = _runtime(TOO_OLD_STDERR)
    await runtime._drain_stderr()
    exc = _delegate_launch_failure(runtime)

    assert isinstance(exc, AcpDelegateLaunchFailed)
    assert isinstance(exc, AcpError)
    # Not a process death: the chat surfaces route those into respawn by type.
    assert not isinstance(exc, AcpProcessDied)
    assert exc.transient is False
    assert acp_error_is_transient(exc) is False
    assert str(exc) == KIRO_CLI_TOO_OLD_MESSAGE
    assert exc.kiro_cli_too_old is True


def test_the_install_repair_error_is_not_marked_too_old():
    assert AcpDelegateLaunchFailed().kiro_cli_too_old is False
    assert AcpDelegateLaunchFailed(DELEGATE_LAUNCH_FAILURE_MESSAGE).kiro_cli_too_old is False


@pytest.mark.asyncio
async def test_the_client_transport_reads_the_same_verdict():
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.client_lifecycle import _delegate_launch_failure

    client = AcpClient.__new__(AcpClient)
    client._settle_stderr = AsyncMock()
    client._stderr_lines = list(TOO_OLD_STDERR)
    exc = await _delegate_launch_failure(client)
    assert isinstance(exc, AcpDelegateLaunchFailed)
    assert str(exc) == KIRO_CLI_TOO_OLD_MESSAGE


# -- The dashboard turn: no respawn, the message, and the latch --


@pytest.mark.asyncio
async def test_the_dashboard_turn_does_not_retry_and_offers_the_update(tmp_path):
    from test_dashboard_chat import TestAcpProcessDiedRecovery as _suite

    state, slot, client, run_chat = _suite._make_state_and_slot(_suite(), tmp_path)
    service = MagicMock()
    state.kiro_prerequisite_service = service
    _suite._make_stream_raise(_suite(), client, AcpDelegateLaunchFailed(KIRO_CLI_TOO_OLD_MESSAGE))
    await run_chat(state, slot, "hello")

    errors = [m.get("content", "") for m in slot.messages if m.get("role") == "error"]
    assert slot._acp_pipe_death_retries == 0, "a too-old CLI must not enter the respawn ladder"
    # One error row, the curated one: no "Connection lost -- retrying..." and no
    # "Session stuck" from a respawn ladder.
    assert len(errors) == 1 and KIRO_CLI_TOO_OLD_MESSAGE in errors[0], errors
    service.mark_cli_too_old.assert_called_once_with()


@pytest.mark.asyncio
async def test_an_install_repair_error_does_not_latch_too_old(tmp_path):
    from test_dashboard_chat import TestAcpProcessDiedRecovery as _suite

    state, slot, client, run_chat = _suite._make_state_and_slot(_suite(), tmp_path)
    service = MagicMock()
    state.kiro_prerequisite_service = service
    _suite._make_stream_raise(_suite(), client, AcpDelegateLaunchFailed())
    await run_chat(state, slot, "hello")
    service.mark_cli_too_old.assert_not_called()


# -- The prerequisite latch the setup screen's Update Kiro CLI card reads --


def _service(tmp_path: Path) -> tuple[KiroPrerequisiteService, _FakeRuntime]:
    executable = tmp_path / ".local" / "bin" / "kiro-cli"
    _make_executable(executable)
    runtime = _FakeRuntime(executable)
    runtime.authenticated = True
    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        home=tmp_path,
        data_home=tmp_path / "data-home",
        process_runner=runtime.run,
        audit_writer=_no_audit,
    )
    return service, runtime


@pytest.mark.asyncio
async def test_mark_cli_too_old_shows_the_update_card_without_probing(tmp_path):
    service, runtime = _service(tmp_path)
    await service._probe()
    before = await service.snapshot()
    assert before["ready"] is True and before["acp_supported"] is True
    calls_after_boot = len(runtime.calls)

    service.mark_cli_too_old()

    snapshot = await service.snapshot()
    # `acp_supported === false` is what renders the gate's Update Kiro CLI card.
    assert snapshot["acp_supported"] is False
    assert snapshot["ready"] is False
    # Sign-in is untouched: this is an update problem, not a sign-out.
    assert snapshot["authenticated"] is True
    assert snapshot["initial_setup_complete"] == before["initial_setup_complete"]
    assert len(runtime.calls) == calls_after_boot, "latching must not spawn anything"


@pytest.mark.asyncio
async def test_a_real_probe_clears_the_latch(tmp_path):
    """The card's Update re-probes; a fixed CLI must not stay behind the card."""
    service, _runtime = _service(tmp_path)
    await service._probe()
    service.mark_cli_too_old()
    refreshed = await service.snapshot(force=True)
    assert refreshed["acp_supported"] is True
    assert refreshed["ready"] is True


@pytest.mark.asyncio
async def test_a_machine_probe_keeps_the_latch(tmp_path):
    """A stale-status poll or the gate's auto-poll must not clear the card first.

    ``acp --help`` still answers on the too-old CLI, so a probe that recomputed
    ``acp_supported`` from scratch would undo the latch before the user saw it.
    """
    service, runtime = _service(tmp_path)
    await service._probe()
    service.mark_cli_too_old()
    calls_before = len(runtime.calls)

    assert await service.verified_ready(max_age_secs=0) is False
    assert len(runtime.calls) > calls_before, "verified_ready must really probe here"
    auto = await service.snapshot(force=True, coalesce=True)
    assert auto["acp_supported"] is False
    assert auto["ready"] is False


@pytest.mark.asyncio
async def test_a_successful_update_clears_the_latch(tmp_path):
    service, runtime = _service(tmp_path)
    fake_run = runtime.run

    async def run(command, args, **kwargs):
        if args == ["update"]:
            runtime.calls.append((command, args))
            return ProcessResult(ok=True)
        return await fake_run(command, args, **kwargs)

    service._run = run
    await service._probe()
    service.mark_cli_too_old()

    result = await service.update_cli()
    assert result["cli_update_error"] == ""
    assert result["acp_supported"] is True
    assert result["ready"] is True


# -- Every surface latches: the error's build site, not only the dashboard turn --


@pytest.fixture
def _registered_service(monkeypatch):
    import kiro_crew.kiro_prerequisite_service as service_module

    service = MagicMock()
    monkeypatch.setattr(service_module, "_turn_auth_service", lambda: service)
    return service


@pytest.mark.asyncio
async def test_the_provider_build_site_latches_for_any_surface(_registered_service):
    from kiro_crew.providers.acp_runtime_start import _delegate_launch_failure

    runtime = _runtime(TOO_OLD_STDERR)
    await runtime._drain_stderr()
    _delegate_launch_failure(runtime)
    _registered_service.mark_cli_too_old.assert_called_once_with()


@pytest.mark.asyncio
async def test_the_client_build_site_latches_for_any_surface(_registered_service):
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.client_lifecycle import _delegate_launch_failure

    client = AcpClient.__new__(AcpClient)
    client._settle_stderr = AsyncMock()
    client._stderr_lines = list(TOO_OLD_STDERR)
    await _delegate_launch_failure(client)
    _registered_service.mark_cli_too_old.assert_called_once_with()


@pytest.mark.asyncio
async def test_an_install_repair_failure_does_not_latch_at_the_build_site(_registered_service):
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.client_lifecycle import _delegate_launch_failure

    client = AcpClient.__new__(AcpClient)
    client._settle_stderr = AsyncMock()
    client._stderr_lines = ["failed to launch kiro-cli-chat: No such file or directory"]
    exc = await _delegate_launch_failure(client)
    assert exc is not None and exc.kiro_cli_too_old is False
    _registered_service.mark_cli_too_old.assert_not_called()


def test_the_registered_latch_is_a_no_op_without_a_service(monkeypatch):
    import kiro_crew.kiro_prerequisite_service as service_module

    monkeypatch.setattr(service_module, "_turn_auth_service", None)
    service_module.mark_registered_cli_too_old()
