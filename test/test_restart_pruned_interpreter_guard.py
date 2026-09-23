"""The pruned-interpreter restart guard: refuse BEFORE the drain, never after it.

An ``apply_command`` that installs into a new versioned tree and prunes the old
one deletes the interpreter the running gateway was launched from. Both restart
consumers then had nothing to re-enter, and the damage was done by ORDERING
rather than by the missing file: the orchestrator saved, fenced, closed every
session and only then raised ``ENOENT`` from ``os.execv``. Admission itself comes
back, but the closed sessions do not and the pending respawn is already cleared,
so what survived was a gateway with no sessions, running a different version from
the install on disk.

The fix is to ask BEFORE the point of no return. There is deliberately no
operator-declared exec target: a second pathname to re-enter cannot be validated
without either exec'ing an arbitrary binary to see what happens, or trusting
bytes that do not decide what the kernel does. So this guard does not try to find
another thing to run -- it declines to drain when the restart cannot succeed, and
leaves the operator a repair-then-relaunch they can actually perform.

Asking early cannot make the exec infallible, though, and the residual is the
same defect: the target can be replaced between the check and the call, and a
present, executable file can still be an image this kernel refuses. So the exec
itself is the second half of the fix -- a refusal there exits the process instead
of returning into a gateway that cannot serve.

These tests pin the three halves that matter: an unusable interpreter refuses
with every session still answerable, a healthy one restarts exactly once, and a
kernel refusal past the drain exits rather than survives.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kiro_crew import platform_compat
from kiro_crew.dashboard.handlers import updates
from kiro_crew.slack import gateway as slack_gateway


def _executable(tmp_path, name: str = "shim") -> str:
    """A real NATIVE executable, copied rather than written.

    A genuine binary from this host, so "executable" means what the kernel means
    rather than what a ``#!`` line claims.

    Only the CONTENTS are copied, which is why this is ``copyfile`` and not
    ``copy2``. ``copy2`` also replays the source's BSD file flags, and on macOS a
    system binary carries ``SF_RESTRICTED`` from System Integrity Protection;
    ``shutil.copystat`` re-raises every ``chflags`` error other than
    ``EOPNOTSUPP``/``ENOTSUP``, so replaying that one flag fails with
    ``PermissionError: [Errno 1]`` naming the DESTINATION. Linux exposes no
    ``chflags`` at all, so only a macOS runner can see it. The single piece of
    metadata these tests need is the exec bit, set on the next line.
    """
    source = next(
        (c for c in ("/bin/true", "/usr/bin/true", "/bin/cat") if os.path.exists(c)),
        None,
    )
    if source is None:  # pragma: no cover - a POSIX host without any of these
        pytest.skip("no native binary available to copy")
    target = tmp_path / name
    shutil.copyfile(source, target)
    platform_compat.chmod_safe(target, 0o700)
    return str(target)


def _state():
    return SimpleNamespace(
        _gateway_restart_in_progress=False,
        push_update_progress=Mock(),
        sessions=SimpleNamespace(close_all=AsyncMock()),
    )


def _orchestrator(dashboard_state=None):
    return SimpleNamespace(
        _pending_update_respawn=None,
        _update_apply_deferred=False,
        _UPDATE_DRAIN_TIMEOUT_SECS=slack_gateway.GatewayOrchestrator._UPDATE_DRAIN_TIMEOUT_SECS,
        dashboard_state=dashboard_state,
        sessions=SimpleNamespace(close_all=AsyncMock(), fence_update_restart=Mock()),
        _drain_update_callback_work=AsyncMock(return_value=True),
    )


class TestTheDashboardRefusesBeforeDraining:
    """``_restart_gateway`` must answer before it becomes unable to serve."""

    @pytest.mark.asyncio
    async def test_a_pruned_interpreter_refuses_without_draining(self, monkeypatch, tmp_path):
        """The regression itself: refuse while every session is still answerable.

        ``close_all`` is the point of no return, so asserting it was never awaited
        is the whole test -- a refusal that arrives after the drain is the bug,
        not the fix.
        """
        execv = Mock()
        monkeypatch.setattr(os, "execv", execv)
        state = _state()

        assert (
            await updates._restart_gateway(
                state, resolver=lambda: str(tmp_path / "pruned" / "python")
            )
            is False
        )

        execv.assert_not_called()
        state.sessions.close_all.assert_not_awaited()
        assert state.push_update_progress.call_args.args[0] == "error"

    @pytest.mark.asyncio
    async def test_the_refusal_names_the_interpreter(self, monkeypatch, tmp_path):
        """The operator is sent to the install, the only thing they can repair."""
        monkeypatch.setattr(os, "execv", Mock())
        state = _state()

        await updates._restart_gateway(state, resolver=lambda: str(tmp_path / "pruned" / "python"))

        message = state.push_update_progress.call_args.args[1]
        assert "python executable" in message.lower()

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason=(
            "The mode bit carries no exec meaning here: os.access does not consult "
            "X_OK on Windows, so a file with no execute permission is accepted and "
            "there is nothing for this case to assert. The repo states that at "
            "papyrus/backend/tectonic.py:298 and gates it the same way at "
            "preview_tools.py:156 and transcribe.py:688. The half that DOES hold on "
            "both platforms is isfile, which is the pruned-interpreter state itself, "
            "and test_a_pruned_interpreter_refuses_without_draining covers it "
            "everywhere."
        ),
    )
    @pytest.mark.asyncio
    async def test_a_present_but_non_executable_interpreter_also_refuses(
        self, monkeypatch, tmp_path
    ):
        """``isfile`` alone is not the question on POSIX: an unexecutable file fails exec."""
        dud = tmp_path / "python"
        dud.write_bytes(b"")
        platform_compat.chmod_safe(dud, 0o400)
        monkeypatch.setattr(os, "execv", Mock())
        state = _state()

        assert await updates._restart_gateway(state, resolver=lambda: str(dud)) is False
        state.sessions.close_all.assert_not_awaited()


class TestAHealthyInstallIsUntouched:
    """The guard must cost a working restart nothing, and must not double-exec."""

    @pytest.mark.asyncio
    async def test_a_healthy_interpreter_restarts_exactly_once(self, monkeypatch, tmp_path):
        """One re-entry, through the interpreter path, with no second execution."""
        exe = _executable(tmp_path, "python")
        module_exec = Mock()
        launcher_exec = Mock()
        monkeypatch.setattr(updates, "reexec_python_module", module_exec)
        monkeypatch.setattr(updates, "reexec_launcher", launcher_exec)
        monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
        state = _state()

        await updates._restart_gateway(state, resolver=lambda: exe)

        module_exec.assert_called_once()
        launcher_exec.assert_not_called()
        assert "error" not in [c.args[0] for c in state.push_update_progress.call_args_list]


class TestTheOrchestratorDefersBeforeDraining:
    """The Slack path keeps its sessions and retries once the install is repaired."""

    @pytest.mark.asyncio
    async def test_a_pruned_interpreter_defers_without_draining(self, monkeypatch, tmp_path):
        execv = Mock()
        monkeypatch.setattr(os, "execv", execv)
        monkeypatch.setattr(slack_gateway, "resolve_restart_launcher", lambda: None)
        orch = _orchestrator()

        await slack_gateway.GatewayOrchestrator._restart_after_update(
            orch, respawn=lambda: str(tmp_path / "pruned" / "python")
        )

        assert orch._update_apply_deferred is True
        execv.assert_not_called()
        orch.sessions.close_all.assert_not_awaited()

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason=(
            "The mode bit carries no exec meaning here: os.access does not consult "
            "X_OK on Windows, so a file with no execute permission is accepted and "
            "there is nothing for this case to assert. Stated by the repo at "
            "papyrus/backend/tectonic.py:298 and gated the same way at "
            "preview_tools.py:156 and transcribe.py:688."
        ),
    )
    @pytest.mark.asyncio
    async def test_a_present_but_non_executable_interpreter_also_defers(
        self, monkeypatch, tmp_path
    ):
        """``isfile`` alone is not the question: an unexecutable file fails exec.

        Without this the orchestrator's ``os.access`` term is unpinned -- a pruned
        path is refused by ``isfile`` on its own, so deleting the access check
        would cost the suite nothing.
        """
        dud = tmp_path / "python"
        dud.write_bytes(b"")
        platform_compat.chmod_safe(dud, 0o400)
        execv = Mock()
        monkeypatch.setattr(os, "execv", execv)
        monkeypatch.setattr(slack_gateway, "resolve_restart_launcher", lambda: None)
        orch = _orchestrator()

        await slack_gateway.GatewayOrchestrator._restart_after_update(
            orch, respawn=lambda: str(dud)
        )

        assert orch._update_apply_deferred is True
        execv.assert_not_called()
        orch.sessions.close_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_directory_is_not_an_interpreter(self, monkeypatch, tmp_path):
        """And ``os.access`` alone is not the question either.

        A traversable directory answers True to ``X_OK``, so the ``isfile`` term is
        what refuses this one. Pinning it keeps both halves of the guard
        load-bearing rather than one covering for the other.
        """
        execv = Mock()
        monkeypatch.setattr(os, "execv", execv)
        monkeypatch.setattr(slack_gateway, "resolve_restart_launcher", lambda: None)
        orch = _orchestrator()

        await slack_gateway.GatewayOrchestrator._restart_after_update(
            orch, respawn=lambda: str(tmp_path)
        )

        assert orch._update_apply_deferred is True
        execv.assert_not_called()
        orch.sessions.close_all.assert_not_awaited()


class _Exited(BaseException):
    """Stands in for ``os._exit``, which the real call cannot let a test observe.

    Derived from ``BaseException`` so the production code's own ``except
    Exception`` handlers cannot swallow it, which keeps this double as
    unrecoverable as the call it replaces.
    """


def _capture_exit(monkeypatch) -> list[int]:
    """Record the exit status instead of ending the test interpreter."""
    codes: list[int] = []

    def fake_exit(code: int) -> None:
        codes.append(code)
        raise _Exited()

    monkeypatch.setattr(platform_compat.os, "_exit", fake_exit)
    return codes


def _enoexec(*_args, **_kwargs):
    """The kernel refusing the image itself -- the residual no check removes."""
    raise OSError(errno.ENOEXEC, "Exec format error")


class TestAKernelRefusalPastTheDrainExits:
    """The half the pre-drain guard cannot cover: ``execv`` itself failing.

    Every test here asserts ``close_all`` WAS awaited. That is the control: it
    proves the case reached the point of no return rather than being caught by
    the guard, which is the only state where exiting is the right answer.
    """

    @pytest.mark.asyncio
    async def test_the_orchestrator_exits_instead_of_unwinding(self, monkeypatch, tmp_path):
        """Returning here hands a stranded gateway back to the update coordinator.

        ``_run_update_checks`` catches ``Exception``, logs and sleeps, so an
        unwinding ``OSError`` becomes a loop that serves nothing.
        """
        exe = _executable(tmp_path, "python")
        monkeypatch.setattr(slack_gateway, "resolve_restart_launcher", lambda: None)
        monkeypatch.setattr(slack_gateway, "flush_breadcrumb_writes", Mock())
        monkeypatch.setattr(platform_compat, "reexec_python_module", _enoexec)
        codes = _capture_exit(monkeypatch)
        orch = _orchestrator()

        with pytest.raises(_Exited):
            await slack_gateway.GatewayOrchestrator._restart_after_update(orch, respawn=lambda: exe)

        assert codes == [1]
        orch.sessions.close_all.assert_awaited()

    @pytest.mark.asyncio
    async def test_the_launcher_branch_exits_too(self, monkeypatch, tmp_path):
        """A declared launcher is validated by its own resolver, not by this guard."""
        launcher = _executable(tmp_path, "kirocrew")
        monkeypatch.setattr(slack_gateway, "resolve_restart_launcher", lambda: launcher)
        monkeypatch.setattr(slack_gateway, "flush_breadcrumb_writes", Mock())
        monkeypatch.setattr(platform_compat, "reexec_launcher", _enoexec)
        codes = _capture_exit(monkeypatch)
        orch = _orchestrator()

        with pytest.raises(_Exited):
            await slack_gateway.GatewayOrchestrator._restart_after_update(
                orch, respawn=lambda: "/unused"
            )

        assert codes == [1]
        orch.sessions.close_all.assert_awaited()

    @pytest.mark.asyncio
    async def test_the_dashboard_exits_instead_of_reporting_success(self, monkeypatch, tmp_path):
        """``return True`` would report a restart that did not happen."""
        exe = _executable(tmp_path, "python")
        monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
        monkeypatch.setattr(updates, "reexec_python_module", _enoexec)
        codes = _capture_exit(monkeypatch)
        state = _state()

        with pytest.raises(_Exited):
            await updates._restart_gateway(state, resolver=lambda: exe)

        assert codes == [1]
        state.sessions.close_all.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_successful_exec_does_not_exit(self, monkeypatch, tmp_path):
        """The control for all three above: the exit belongs to the failure only."""
        exe = _executable(tmp_path, "python")
        monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
        monkeypatch.setattr(updates, "reexec_python_module", Mock())
        codes = _capture_exit(monkeypatch)
        state = _state()

        assert await updates._restart_gateway(state, resolver=lambda: exe) is True
        assert codes == []


class TestTheFatalExitCannotBeHeldUp:
    """Bookkeeping runs because ``os._exit`` skips ``atexit`` -- never to gate the exit."""

    @pytest.mark.asyncio
    async def test_a_wedged_drain_does_not_stop_the_exit(self, monkeypatch):
        from kiro_crew import cli as kiro_cli
        from kiro_crew import eventlog_hooks

        def wedged(*_args, **_kwargs):
            raise RuntimeError("disk is not answering")

        monkeypatch.setattr(eventlog_hooks, "drain_for_shutdown", wedged)
        monkeypatch.setattr(kiro_cli, "_stop_log_queue_listener", wedged)
        codes = _capture_exit(monkeypatch)

        with pytest.raises(_Exited):
            await platform_compat.exit_after_failed_restart_exec("/opt/kirocrew/bin/python3")

        assert codes == [1]

    @pytest.mark.asyncio
    async def test_the_critical_line_names_the_target(self, monkeypatch, caplog):
        """The exit status is generic, so the log line carries the whole diagnosis."""
        codes = _capture_exit(monkeypatch)

        with caplog.at_level(logging.CRITICAL, logger="kiro_crew.platform_compat"):
            with pytest.raises(_Exited):
                await platform_compat.exit_after_failed_restart_exec("/opt/kirocrew/bin/python3")

        assert codes == [1]
        assert "/opt/kirocrew/bin/python3" in caplog.text


class TestTheseChecksDoNotBlockTheEventLoop:
    """`no-blocking-call-on-event-loop`: every syscall this guard adds is offloaded.

    Each asserts the THREAD the probe body actually ran on, not which executor an
    offload was handed: an assertion of the latter passes even with the call
    inline whenever another offload in the same function used that executor.
    """

    @pytest.mark.asyncio
    async def test_the_interpreter_probe_does_not_run_on_the_event_loop(
        self, monkeypatch, tmp_path
    ):
        """The pathname comes from the install being replaced, so it can be a mount
        that is not answering. Stat'ing it on the loop thread would stall every chat
        and the heartbeat rather than one restart.
        """
        loop_thread = threading.current_thread()
        probe_threads: list[threading.Thread] = []
        real_probe = platform_compat.execv_target_available

        def recording_probe(path):
            probe_threads.append(threading.current_thread())
            return real_probe(path)

        monkeypatch.setattr(platform_compat, "execv_target_available", recording_probe)
        monkeypatch.setattr(os, "execv", Mock())
        monkeypatch.setattr(slack_gateway, "resolve_restart_launcher", lambda: None)
        orch = _orchestrator()

        await slack_gateway.GatewayOrchestrator._restart_after_update(
            orch, respawn=lambda: str(tmp_path / "pruned" / "python")
        )

        assert orch._update_apply_deferred is True
        assert probe_threads, "the guard never consulted the probe"
        assert loop_thread not in probe_threads

    @pytest.mark.asyncio
    async def test_both_flushes_leave_the_loop_thread(self, monkeypatch):
        """BOTH, counted rather than sampled: this exit exists to release the port,
        and a flush left inline holds every remaining task for its own timeout. One
        offloaded and one inline still freezes the loop, so a single recorded thread
        would pass while the defect stands.
        """
        from kiro_crew import cli as kiro_cli
        from kiro_crew import eventlog_hooks

        loop_thread = threading.current_thread()
        flush_threads: list[threading.Thread] = []

        def recording_flush(*_args, **_kwargs):
            flush_threads.append(threading.current_thread())

        monkeypatch.setattr(eventlog_hooks, "drain_for_shutdown", recording_flush)
        monkeypatch.setattr(kiro_cli, "_stop_log_queue_listener", recording_flush)
        codes = _capture_exit(monkeypatch)

        with pytest.raises(_Exited):
            await platform_compat.exit_after_failed_restart_exec("/opt/kirocrew/bin/python3")

        assert codes == [1]
        assert len(flush_threads) == 2, flush_threads
        assert loop_thread not in flush_threads

    @pytest.mark.asyncio
    async def test_a_flush_that_never_gets_a_worker_cannot_hold_the_exit(self, monkeypatch):
        """Off-loop is not the same as bounded.

        Each flush bounds its own WORK -- 5.0s for the event log, 2.0s for the log
        queue -- but neither can bound the wait for a thread to run that work on. A
        saturated pool leaves the await unresumed, and then the exit never happens:
        the loop is free and the process still answers nothing on a port it holds.

        BOTH flushes wedge here, so only a deadline can end either wait -- but they
        answer to DIFFERENT ceilings, which is why the elapsed bound is loose. The
        event log's is this module's, patched small below. The gateway.log tail is
        drained by ``cli.drain_log_queue_before_hard_exit``, which carries its own,
        so a few seconds of the measured time are that contract holding rather than
        this one.
        """
        from kiro_crew import cli as kiro_cli
        from kiro_crew import eventlog_hooks

        started = threading.Event()
        released = threading.Event()

        def wedged_flush(*_args, **_kwargs):
            started.set()
            released.wait(10)

        monkeypatch.setattr(eventlog_hooks, "drain_for_shutdown", wedged_flush)
        monkeypatch.setattr(kiro_cli, "_stop_log_queue_listener", wedged_flush)
        monkeypatch.setattr(platform_compat, "_EXIT_FLUSH_DEADLINE_SECS", 0.05)
        codes = _capture_exit(monkeypatch)

        began = time.monotonic()
        try:
            with pytest.raises(_Exited):
                await platform_compat.exit_after_failed_restart_exec("/opt/kirocrew/bin/python3")
        finally:
            released.set()
        elapsed = time.monotonic() - began

        assert codes == [1]
        assert started.is_set(), "the flush never ran, so nothing was bounded"
        assert elapsed < 5.0, elapsed

    @pytest.mark.asyncio
    async def test_the_dashboard_probe_does_not_run_on_the_event_loop(self, monkeypatch, tmp_path):
        """The same predicate, the same stalled mount, the other restart path.

        The helper's docstring promises callers that BOTH syscalls go to a worker in
        one hop, because offloading one caller and leaving the other inline still
        freezes the loop. The orchestrator has its own case above; this is the
        dashboard's, and without it that site could return to a bare stat with no
        test objecting.
        """
        loop_thread = threading.current_thread()
        probe_threads: list[threading.Thread] = []
        real_probe = platform_compat.execv_target_available

        def recording_probe(path):
            probe_threads.append(threading.current_thread())
            return real_probe(path)

        monkeypatch.setattr(platform_compat, "execv_target_available", recording_probe)
        monkeypatch.setattr(os, "execv", Mock())
        state = _state()

        assert (
            await updates._restart_gateway(
                state, resolver=lambda: str(tmp_path / "pruned" / "python")
            )
            is False
        )

        assert probe_threads, "the dashboard guard never consulted the probe"
        assert loop_thread not in probe_threads
