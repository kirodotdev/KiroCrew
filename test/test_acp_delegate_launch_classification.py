"""A ``kiro-cli-chat`` delegate that will not launch must name the install fix.

The POSIX ``kiro-cli`` is a launcher that ``exec``s a sibling ``kiro-cli-chat``
for ``acp``/``chat``. When that sibling is missing, empty or version-skewed -- a
stale symlink pointing at a removed binary is the field case -- the
launcher itself still answers ``--version``/``whoami`` but the exec fails with
``error: failed to launch kiro-cli-chat`` and the child never starts. The death
reached the caller as the generic ``process exited (rc=None/rc=1)`` card, which
points at retry or model-switch -- neither of which can fix an install problem.

The failure is DETERMINISTIC, which is the whole argument: respawning hits the
same wall. The repo already has the shape for that (``AcpAuthRequired``,
``AcpSandboxInitFailed``), so this is surfaced as a NON-RETRYABLE error whose
fixed wording names the one fix that works -- reinstall/repair Kiro CLI.

A control tail must stay retryable throughout: a bare ``failed to launch`` or a
generic spawn error is ordinary output for an unrelated death a respawn fixes, so
the classifier is anchored on the delegate name.
"""

import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.transport_errors import (
    DELEGATE_LAUNCH_FAILURE_MESSAGE,
    AcpDelegateLaunchFailed,
    AcpError,
    AcpProcessDied,
    is_delegate_launch_failure_output,
)

# The reported burst, verbatim: the launcher's own line naming the sibling it
# could not exec, beside the generic runtime-death wrapper.
DELEGATE_LAUNCH_STDERR = [
    "AcpRuntime dead: process exited (rc=1)",
    "error: failed to launch kiro-cli-chat",
]

# The control. Same generic death, NO delegate-launch token: a missing
# interpreter, a transient fs hiccup, an OOM all look like this, and a respawn
# legitimately fixes them.
RETRYABLE_STDERR = [
    "AcpRuntime dead: process exited (rc=1)",
    "Error: Failed to spawn child process",
]


# -- The classifier --


@pytest.mark.parametrize(
    "lines",
    [
        DELEGATE_LAUNCH_STDERR,
        ["error: failed to launch kiro-cli-chat: No such file or directory"],
        # Case and surrounding prose must not decide it.
        ["  ERROR: FAILED TO LAUNCH KIRO-CLI-CHAT"],
    ],
    ids=["reported", "enoent", "shouty"],
)
def test_every_signature_is_classified(lines):
    assert is_delegate_launch_failure_output("\n".join(lines)) is True


@pytest.mark.parametrize(
    "lines",
    [
        RETRYABLE_STDERR,
        ["Starting kiro-cli 2.33.0", "INFO listening on stdio"],
        ["Access denied: The bearer token included in the request is invalid."],
    ],
    ids=["spawn-failure", "healthy", "auth"],
)
def test_a_control_tail_stays_retryable(lines):
    assert is_delegate_launch_failure_output("\n".join(lines)) is False


def test_a_bare_launch_failure_is_not_enough():
    """The mutation this classifier must not survive.

    Dropping the delegate-name anchor -- matching a bare ``failed to launch`` as
    the reported wrapper line invites -- makes every ordinary launch failure
    permanent. Asserted on its own because it is the single change that would make
    the control above pass for the wrong reason.
    """
    assert is_delegate_launch_failure_output("failed to launch") is False
    assert is_delegate_launch_failure_output("process exited (rc=None)") is False


# -- The latch, written by the drain and read later --


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
    runtime._initialized = False
    runtime._first_session_ready = False
    runtime._process = _FakeProcess(lines)
    return runtime


@pytest.mark.asyncio
async def test_the_runtime_latches_the_failure_past_the_ring_buffer():
    """The latch, for the same reason the sandbox/auth latches exist.

    ``_stderr_lines`` is a 20-line ring and nothing asks why the runtime died
    until a request has already failed; on a chatty startup the delegate line is
    evicted by then, so a detector that re-scans the buffer answers "no delegate
    problem" -- indistinguishable from a real negative.
    """
    runtime = _runtime(DELEGATE_LAUNCH_STDERR + [f"noise {i}" for i in range(40)])
    await runtime._drain_stderr()

    assert len(runtime._stderr_lines) == 20, "precondition: the ring must have trimmed"
    assert not any(
        "failed to launch kiro-cli-chat" in line for line in runtime._stderr_lines
    ), "precondition: the delegate line must have been evicted, or this proves nothing"
    assert runtime.saw_delegate_launch_failure() is True


@pytest.mark.asyncio
async def test_a_restricted_runtime_still_latches():
    """Retention off keeps no buffer to scan -- and the answer still lands."""
    runtime = _runtime(DELEGATE_LAUNCH_STDERR)
    runtime.recording_allowed = False
    await runtime._drain_stderr()

    assert runtime._stderr_lines == []
    assert runtime.saw_delegate_launch_failure() is True


@pytest.mark.asyncio
async def test_a_healthy_runtime_never_latches():
    runtime = _runtime(["Starting kiro-cli 2.33.0"] + RETRYABLE_STDERR)
    await runtime._drain_stderr()
    assert runtime.saw_delegate_launch_failure() is False


@pytest.mark.asyncio
async def test_a_line_after_startup_does_not_latch():
    """The latch arms only while startup is unfinished.

    A ``failed to launch`` line after a healthy start belongs to some later
    unrelated death, not to the agent process's own startup -- latching it would
    turn the next broken pipe or OOM into a permanent "reinstall Kiro CLI".
    """
    runtime = _runtime(DELEGATE_LAUNCH_STDERR)
    runtime._first_session_ready = True
    await runtime._drain_stderr()
    assert runtime.saw_delegate_launch_failure() is False


# -- The translation, and its verdict --


def test_the_translation_is_non_retryable_and_names_the_fix():
    from kiro_crew.providers.acp_runtime_start import _delegate_launch_failure

    runtime = MagicMock()
    runtime.saw_delegate_launch_failure.return_value = True
    exc = _delegate_launch_failure(runtime)
    assert isinstance(exc, AcpDelegateLaunchFailed)
    assert isinstance(exc, AcpError)
    # NOT a process death: the chat surfaces route AcpProcessDied into their
    # respawn-and-requeue arms by TYPE, without reading ``transient``.
    assert not isinstance(exc, AcpProcessDied)
    assert exc.transient is False, (
        "transient must be an explicit False: every retry ladder reads the verdict "
        "off the exception, and an unclassified None retries it"
    )
    # Not the start-failure tag: that card offers Resume and, on a repeat, a
    # gateway restart -- neither repairs an install.
    assert getattr(exc, "session_start_failed", False) is False
    assert "kiro-cli-chat" in str(exc)
    assert "reinstall" in str(exc).lower() or "repair" in str(exc).lower()
    assert DELEGATE_LAUNCH_FAILURE_MESSAGE == str(exc)


def test_an_unlatched_runtime_is_not_translated():
    from kiro_crew.providers.acp_runtime_start import _delegate_launch_failure

    runtime = MagicMock()
    runtime.saw_delegate_launch_failure.return_value = False
    assert _delegate_launch_failure(runtime) is None


def test_the_retry_deciders_all_refuse_it():
    """One assertion for every consumer, through the shared predicate.

    ``acp_error_is_transient`` is what the dashboard turn, the cron tick, the
    subagent run and the workflow step all ask, so pinning it here pins them all.
    """
    from kiro_crew.llm_helpers import acp_error_is_transient

    assert acp_error_is_transient(AcpDelegateLaunchFailed()) is False


def test_every_startup_site_consults_the_latch():
    """Wiring, asserted on the source: three startup sites, all three converted.

    Counted against the sandbox latch it sits beside, so adding a fourth startup
    path without the delegate check -- which would report a delegate-launch
    failure as a generic death and let the pool respawn into the same wall --
    fails here.
    """
    import kiro_crew.providers.acp as provider_mod

    source = "\n".join(
        Path(sys.modules[name].__file__).read_text()
        for name in (provider_mod.__name__, *provider_mod._ACP_PROVIDER_OWNERS)
    )
    sandbox_sites = len(re.findall(r"sandbox_init_failure_for_runtime\(runtime\)", source))
    delegate_sites = len(re.findall(r"_delegate_launch_failure\(runtime\)", source))
    assert sandbox_sites == 3, f"the sandbox latch is read at {sandbox_sites} sites, not 3"
    assert delegate_sites == sandbox_sites, (
        f"{delegate_sites} of {sandbox_sites} startup sites classify a delegate-launch "
        "failure; a site left behind reports it as a generic death and the pool respawns"
    )


def test_the_client_transport_classifies_it_too():
    """The ``AcpClient`` startup arm consults the same signature beside the sandbox one."""
    import kiro_crew.acp.client_start as client_start

    source = Path(client_start.__file__).read_text()
    assert source.count("self._classify_failed_start(self._sandbox_init_failure)") == 1
    assert source.count("self._classify_failed_start(self._delegate_launch_failure)") == 1


@pytest.mark.asyncio
async def test_the_client_reads_it_off_the_ring():
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.client_lifecycle import _delegate_launch_failure

    client = AcpClient.__new__(AcpClient)
    client._settle_stderr = AsyncMock()
    client._stderr_lines = list(DELEGATE_LAUNCH_STDERR)
    assert isinstance(await _delegate_launch_failure(client), AcpDelegateLaunchFailed)
    client._stderr_lines = list(RETRYABLE_STDERR)
    assert await _delegate_launch_failure(client) is None
    client._stderr_lines = []
    assert await _delegate_launch_failure(client) is None


# -- The dashboard turn: no respawn, and the message reaches the user --


def _run_dashboard_turn(tmp_path, exc):
    from test_dashboard_chat import TestAcpProcessDiedRecovery as _suite

    state, slot, client, run_chat = _suite._make_state_and_slot(_suite(), tmp_path)
    _suite._make_stream_raise(_suite(), client, exc)
    return state, slot, run_chat


@pytest.mark.asyncio
async def test_the_dashboard_turn_does_not_respawn_and_shows_the_fix(tmp_path):
    state, slot, run_chat = _run_dashboard_turn(tmp_path, AcpDelegateLaunchFailed())
    await run_chat(state, slot, "hello")

    errors = [m.get("content", "") for m in slot.messages if m.get("role") == "error"]
    assert (
        slot._acp_pipe_death_retries == 0
    ), "a delegate-launch failure must not enter the respawn ladder"
    assert not any("retrying" in e.lower() or "stuck" in e.lower() for e in errors), errors
    assert any("kiro-cli-chat" in e and "Reinstall or repair Kiro CLI" in e for e in errors), errors


@pytest.mark.asyncio
async def test_the_old_process_death_shape_did_respawn(tmp_path):
    """The control that makes the test above meaningful: the shape this replaced.

    An ``AcpProcessDied`` carrying the same message and ``transient=False`` is
    still routed into the respawn arm, which is exactly why the type had to change.
    """
    died = AcpProcessDied(DELEGATE_LAUNCH_FAILURE_MESSAGE, transient=False, user_worded=True)
    state, slot, run_chat = _run_dashboard_turn(tmp_path, died)
    await run_chat(state, slot, "hello")

    errors = [m.get("content", "") for m in slot.messages if m.get("role") == "error"]
    assert slot._acp_pipe_death_retries == 1
    assert any("retrying" in e.lower() for e in errors), errors


def test_the_error_card_is_not_the_start_failure_card():
    """The start-failure row offers Resume and, repeated, a gateway restart.

    Neither repairs an install, and both would contradict the message, so this
    error must render as a plain terminal error row, as a sandbox refusal does.
    """
    from kiro_crew.dashboard.chat_turn.recovery import _terminal_error_meta

    assert _terminal_error_meta(AcpDelegateLaunchFailed()) is None
