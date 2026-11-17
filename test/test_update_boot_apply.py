"""An armed update applies at service boot, before the gateway serves anything.

The apply point (maintainer rulings 1 to 4 in the decision entry "updates never
restart a running gateway"): nothing runs at shutdown and nothing runs while the
gateway serves. Each case drives ``apply_armed_update_at_boot`` with the child
process and the re-exec replaced by recording stand-ins.
"""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import patch

import pytest

from kiro_crew import update_boot_apply, update_next_start
from kiro_crew.testing.wait import await_until


class _Calls:
    """What the stand-ins saw, and what they answer."""

    def __init__(self) -> None:
        self.child = 0
        self.execs: list[tuple[str | None, str | None]] = []
        self.child_result: tuple[bool, str] = (True, "ok")
        self.installed: list[str] = ["1.0.0"]
        #: Installed version once the apply child has run (``None``: unchanged).
        self.installed_after: str | None = None


@pytest.fixture
def calls(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.delenv(update_boot_apply.DESKTOP_HOSTED_ENV, raising=False)
    update_next_start._differs_memo = None
    rec = _Calls()

    async def child():
        rec.child += 1
        if rec.installed_after is not None:
            rec.installed.append(rec.installed_after)
        return rec.child_result

    async def target():
        return None, "/usr/bin/python3"

    monkeypatch.setattr(update_boot_apply, "_run_apply_child", child)
    monkeypatch.setattr(update_boot_apply, "_resolve_exec_target", target)
    monkeypatch.setattr(
        update_boot_apply, "_reexec", lambda launcher, exe: rec.execs.append((launcher, exe))
    )
    monkeypatch.setattr(update_next_start, "installed_version", lambda: rec.installed[-1])
    monkeypatch.setattr("kiro_crew.__version__", "1.0.0")
    return rec


def _boot() -> bool:
    return asyncio.run(update_boot_apply.apply_armed_update_at_boot())


def test_nothing_armed_means_nothing_runs(calls) -> None:
    assert _boot() is False

    assert calls.child == 0 and calls.execs == []


def test_an_armed_update_runs_the_apply_then_reexecs_onto_the_new_install(calls) -> None:
    update_next_start.arm("2.0.0")
    calls.installed_after = "2.0.0"

    assert _boot() is True

    assert calls.child == 1
    assert calls.execs == [(None, "/usr/bin/python3")]
    assert update_next_start.read().armed is False
    assert update_next_start.read().pending_apply_error == ""


def test_an_install_that_already_moved_only_reexecs(calls) -> None:
    update_next_start.arm("2.0.0")
    calls.installed.append("2.0.0")

    assert _boot() is True

    assert calls.child == 0
    assert len(calls.execs) == 1


def test_a_gateway_the_desktop_hosts_leaves_the_apply_to_the_desktop(calls, monkeypatch) -> None:
    monkeypatch.setenv(update_boot_apply.DESKTOP_HOSTED_ENV, "1")
    update_next_start.arm("2.0.0")

    assert _boot() is False

    assert calls.child == 0 and calls.execs == []
    assert update_next_start.read().armed is True


def test_a_desktop_hosted_gateway_clears_an_arm_the_desktop_already_applied(
    calls, monkeypatch
) -> None:
    monkeypatch.setenv(update_boot_apply.DESKTOP_HOSTED_ENV, "1")
    update_next_start.arm("1.0.0")

    assert _boot() is False

    assert calls.child == 0 and calls.execs == []
    assert update_next_start.read().armed is False


def test_a_desktop_hosted_gateway_clears_an_arm_the_running_version_has_passed(
    calls, monkeypatch
) -> None:
    # Armed 2.0.0, then 3.0.0 landed and the quit installed it: equality never matches.
    monkeypatch.setattr("kiro_crew.__version__", "3.0.0")
    monkeypatch.setenv(update_boot_apply.DESKTOP_HOSTED_ENV, "1")
    update_next_start.arm("2.0.0")

    assert _boot() is False

    assert calls.child == 0 and calls.execs == []
    assert update_next_start.read().armed is False


def test_a_standalone_gateway_still_applies_an_older_arm_a_rollback(calls, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.__version__", "3.0.0")
    calls.installed = ["3.0.0"]
    calls.installed_after = "2.0.0"
    update_next_start.arm("2.0.0")

    assert _boot() is True

    assert calls.child == 1


def test_an_arm_for_a_newer_version_than_the_running_one_stays_for_the_desktop(
    calls, monkeypatch
) -> None:
    monkeypatch.setenv(update_boot_apply.DESKTOP_HOSTED_ENV, "1")
    update_next_start.arm("2.0.0")

    assert _boot() is False

    assert update_next_start.read().armed is True


def test_an_arm_that_names_no_version_is_dropped_at_boot(calls) -> None:
    update_next_start._state_path().write_text(
        json.dumps({"armed_version": "", "armed_at": 1.0, "attempts": 0})
    )
    assert update_next_start.read().armed is True

    assert _boot() is False

    assert calls.child == 0 and calls.execs == []
    assert update_next_start.read().armed is False


@pytest.mark.parametrize("version", ["1.0.0", ""], ids=["already-running", "blank"])
def test_a_record_that_will_not_clear_does_not_stop_the_start(calls, monkeypatch, version) -> None:
    if version:
        update_next_start.arm(version)
    else:
        update_next_start._state_path().write_text(
            json.dumps({"armed_version": "", "armed_at": 1.0, "attempts": 0})
        )

    def _boom(**_kw: object) -> None:
        raise OSError("read-only data home")

    monkeypatch.setattr(update_next_start, "finish", _boom)

    assert _boot() is False

    assert calls.child == 0 and calls.execs == []


def test_an_armed_version_that_already_runs_is_cleared_without_applying(calls) -> None:
    update_next_start.arm("1.0.0")

    assert _boot() is False

    assert calls.child == 0 and calls.execs == []
    assert update_next_start.read().armed is False


def test_a_git_checkout_update_with_no_version_bump_is_applied(calls) -> None:
    """Armed version equals the running one, but the checkout is behind its upstream."""
    update_next_start.arm("1.0.0", behind=3)

    assert _boot() is True

    assert calls.child == 1
    assert len(calls.execs) == 1
    assert update_next_start.read().armed is False


def test_a_git_checkout_restarts_after_a_successful_apply_whether_or_not_the_version_moved(
    calls,
) -> None:
    update_next_start.arm("1.0.0", behind=1)
    calls.installed_after = "1.0.0"

    _boot()

    assert calls.child == 1 and len(calls.execs) == 1


def test_a_stale_apply_error_clears_once_the_install_moved(calls, monkeypatch) -> None:
    """Boot apply failed; the person ran `kirocrew update` themselves and restarted."""
    update_next_start.arm("2.0.0")
    calls.child_result = (False, "boom")
    _boot()
    assert update_next_start.read().pending_apply_error == "boom"

    _boot()
    assert update_next_start.read().pending_apply_error == "boom"

    monkeypatch.setattr("kiro_crew.__version__", "2.0.0")
    _boot()

    assert update_next_start.read().pending_apply_error == ""


def test_a_failed_apply_clears_the_arm_keeps_the_old_version_and_records_the_error(calls) -> None:
    update_next_start.arm("2.0.0")
    calls.child_result = (False, "first line\nthe installer said no")

    assert _boot() is False

    assert calls.execs == []
    state = update_next_start.read()
    assert state.armed is False
    assert state.pending_apply_error == "the installer said no"
    assert update_next_start.status_fields()["pending_apply_error"] == "the installer said no"


def test_an_apply_that_changed_nothing_does_not_reexec(calls) -> None:
    update_next_start.arm("2.0.0")

    assert _boot() is False

    assert calls.child == 1 and calls.execs == []
    assert update_next_start.read().armed is False


def test_the_arm_is_cleared_before_the_exec_so_a_restart_onto_the_same_version_cannot_loop(
    calls, monkeypatch
) -> None:
    update_next_start.arm("2.0.0")
    calls.installed_after = "2.0.0"
    seen: list[bool] = []
    monkeypatch.setattr(
        update_boot_apply,
        "_reexec",
        lambda launcher, exe: seen.append(update_next_start.read().armed),
    )

    _boot()

    assert seen == [False]


def test_a_crash_looping_apply_gives_up_after_its_attempts(calls) -> None:
    update_next_start.arm("2.0.0")
    for _ in range(update_next_start.MAX_BOOT_ATTEMPTS):
        update_next_start.note_boot_attempt()

    assert _boot() is False

    assert calls.child == 0
    state = update_next_start.read()
    assert state.armed is False and "did not complete" in state.pending_apply_error


def test_a_failed_exec_is_an_error_and_the_old_version_keeps_starting(calls, monkeypatch) -> None:
    update_next_start.arm("2.0.0")
    calls.installed_after = "2.0.0"

    def refuse(launcher, exe):
        raise OSError("exec format error")

    monkeypatch.setattr(update_boot_apply, "_reexec", refuse)

    assert _boot() is True

    assert "exec format error" in update_next_start.read().pending_apply_error


class _Proc:
    """A child whose ``communicate`` ends only when told to."""

    returncode = None
    # nosemgrep: kirocrew.mock-pid-allocatable -- process double, never signalled
    pid = 4242

    def __init__(self, exits_on_interrupt: bool, events: list[str]) -> None:
        self._exits = exits_on_interrupt
        self._events = events
        self._done = asyncio.Event()

    def interrupted(self) -> None:
        if self._exits:
            self._done.set()

    async def communicate(self):
        await self._done.wait()
        return b"", None


def _run_child_with(exits_on_interrupt: bool, events: list[str]) -> tuple[bool, str]:
    async def run() -> tuple[bool, str]:
        proc = _Proc(exits_on_interrupt, events)

        async def fake_spawn(*args, **kwargs):
            return proc

        async def fake_kill(p, **kwargs):
            events.append("kill")

        def fake_signal(pgid, sig):
            events.append(f"signal:{sig.name if hasattr(sig, 'name') else sig}:{pgid}")
            proc.interrupted()
            return True

        with (
            patch("asyncio.create_subprocess_exec", fake_spawn),
            patch("kiro_crew.platform_compat.kill_and_reap", fake_kill),
            patch("kiro_crew.platform_compat.kill_process_group", fake_signal),
            patch("kiro_crew.platform_compat.IS_POSIX", True),
            patch.object(update_boot_apply, "APPLY_TIMEOUT_SECS", 0.05),
            patch.object(update_boot_apply, "INTERRUPT_GRACE_SECS", 0.05),
        ):
            return await update_boot_apply._run_apply_child()

    return asyncio.run(run())


def test_the_apply_child_is_interrupted_before_it_is_killed_on_timeout() -> None:
    events: list[str] = []

    ok, reason = _run_child_with(False, events)

    assert ok is False and "did not finish" in reason
    assert events == ["signal:SIGINT:4242", "kill"]


def test_the_interrupt_grace_covers_the_cli_rollback_bound() -> None:
    from kiro_crew.platform.update_provider import INSTALLER_TIMEOUT_TERM_GRACE_SECS

    assert update_boot_apply.INTERRUPT_GRACE_SECS > INSTALLER_TIMEOUT_TERM_GRACE_SECS


def test_an_apply_child_that_unwinds_on_the_interrupt_is_not_killed() -> None:
    events: list[str] = []

    ok, reason = _run_child_with(True, events)

    assert ok is False and "did not finish" in reason
    assert events == ["signal:SIGINT:4242"]


def test_a_manual_update_that_finishes_clears_a_stale_boot_apply_error(
    tmp_path, monkeypatch
) -> None:
    import sys

    from kiro_crew.cli import main

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    update_next_start.finish(error="the update command failed")
    ran: list[bool] = []
    monkeypatch.setattr("kiro_crew.cli_server._update", lambda force=False: ran.append(force))
    monkeypatch.setattr(sys, "argv", ["kirocrew", "update"])

    main()

    assert ran == [False]
    assert update_next_start.read().pending_apply_error == ""


@pytest.mark.skipif(
    sys.platform == "win32", reason="raising SIGTERM in-process ends the process on Windows"
)
def test_a_sigterm_during_the_boot_apply_interrupts_the_child_then_exits_with_its_status(
    calls, monkeypatch
) -> None:
    import signal

    events: list[str] = []
    update_next_start.arm("2.0.0")

    started = asyncio.Event()

    async def child():
        # The real _run_apply_child leaves through _stop_apply_child on a cancel.
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("interrupt-then-grace")
            raise

    monkeypatch.setattr(update_boot_apply, "_run_apply_child", child)
    monkeypatch.setattr(
        update_boot_apply, "_exit_with_sigterm_status", lambda: events.append("exit")
    )
    monkeypatch.setattr(update_boot_apply.platform_compat, "IS_POSIX", True)

    async def run() -> None:
        task = asyncio.ensure_future(update_boot_apply.apply_armed_update_at_boot())
        # The SIGTERM handler is installed before the apply reaches its child.
        await await_until(started, what="the fake apply child to start")
        signal.raise_signal(signal.SIGTERM)
        with pytest.raises(asyncio.CancelledError):
            await task
        # The previous SIGTERM handling is back once the apply is over.
        assert asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM) is False

    asyncio.run(run())

    assert events == ["interrupt-then-grace", "exit"]


def test_the_sigterm_handler_is_restored_after_a_normal_boot_apply(calls) -> None:
    import signal

    before = signal.getsignal(signal.SIGTERM)

    _boot()

    assert signal.getsignal(signal.SIGTERM) == before


# --- The apply child itself, the exec target, and the paths that must never raise ---


class _FinishedProc:
    """A child that has already exited with ``returncode`` and printed ``out``."""

    # nosemgrep: kirocrew.mock-pid-allocatable -- process double, never signalled
    pid = 4242

    def __init__(self, returncode: int, out: bytes) -> None:
        self.returncode = returncode
        self._out = out

    async def communicate(self):
        return self._out, None


def _spawn_finished(returncode: int, out: bytes, spawned: list[tuple]) -> tuple[bool, str]:
    async def fake_spawn(*args, **kwargs):
        spawned.append((args, kwargs))
        return _FinishedProc(returncode, out)

    async def run() -> tuple[bool, str]:
        with patch("asyncio.create_subprocess_exec", fake_spawn):
            return await update_boot_apply._run_apply_child()

    return asyncio.run(run())


def test_the_apply_child_is_kirocrew_update_with_no_stdin_and_its_output_tail_is_kept() -> None:
    spawned: list[tuple] = []

    ok, tail = _spawn_finished(0, b"x" * 2000 + b"\nUpdated to 2.0.0\n\n", spawned)

    assert ok is True
    assert tail.endswith("Updated to 2.0.0")
    assert len(tail) == update_boot_apply._OUTPUT_TAIL_CHARS
    [(argv, kwargs)] = spawned
    assert argv[-3:] == ("-m", "kiro_crew", "update")
    assert kwargs["stdin"] is asyncio.subprocess.DEVNULL
    assert kwargs["stderr"] is asyncio.subprocess.STDOUT


def test_an_apply_child_that_exits_non_zero_is_a_failure_with_its_output() -> None:
    ok, tail = _spawn_finished(1, b"error: the installer said no\n", [])

    assert (ok, tail) == (False, "error: the installer said no")


class _BlockedProc(_Proc):
    """A :class:`_Proc` that says when the apply has started waiting on it."""

    def __init__(self, exits_on_interrupt: bool, events: list[str]) -> None:
        super().__init__(exits_on_interrupt, events)
        self.waiting = asyncio.Event()

    async def communicate(self):
        self.waiting.set()
        return await super().communicate()


def test_a_cancelled_apply_child_is_interrupted_before_the_cancel_propagates() -> None:
    events: list[str] = []

    async def run() -> None:
        proc = _BlockedProc(True, events)

        async def fake_spawn(*args, **kwargs):
            return proc

        async def fake_kill(p, **kwargs):
            events.append("kill")

        def fake_signal(pgid, sig):
            events.append(f"signal:{sig.name}:{pgid}")
            proc.interrupted()
            return True

        with (
            patch("asyncio.create_subprocess_exec", fake_spawn),
            patch("kiro_crew.platform_compat.kill_and_reap", fake_kill),
            patch("kiro_crew.platform_compat.kill_process_group", fake_signal),
            patch("kiro_crew.platform_compat.IS_POSIX", True),
        ):
            task = asyncio.ensure_future(update_boot_apply._run_apply_child())
            await await_until(proc.waiting, what="the apply to wait on its child")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())

    assert events == ["signal:SIGINT:4242"]


def test_without_process_groups_the_apply_child_is_killed_without_an_interrupt() -> None:
    events: list[str] = []

    async def fake_kill(p, **kwargs):
        events.append("kill")

    def fake_signal(pgid, sig):
        events.append("signal")
        return True

    async def run() -> None:
        with (
            patch("kiro_crew.platform_compat.kill_and_reap", fake_kill),
            patch("kiro_crew.platform_compat.kill_process_group", fake_signal),
            patch("kiro_crew.platform_compat.IS_POSIX", False),
        ):
            await update_boot_apply._stop_apply_child(_Proc(True, events))

    asyncio.run(run())

    assert events == ["kill"]


def test_an_update_that_asked_a_question_at_boot_tells_the_person_to_run_it(calls) -> None:
    update_next_start.arm("2.0.0")
    calls.child_result = (False, "Discard local changes? [y/N]\nTraceback ...\nEOFError")

    assert _boot() is False

    assert "needs your confirmation" in update_next_start.read().pending_apply_error


def test_an_update_that_failed_silently_still_leaves_a_reason(calls) -> None:
    update_next_start.arm("2.0.0")
    calls.child_result = (False, "\n  \n")

    _boot()

    assert update_next_start.read().pending_apply_error == "the update command failed"


def _resolve_with(monkeypatch, launcher: str | None, available: bool) -> tuple:
    import kiro_crew.gateway_restart as gateway_restart
    import kiro_crew.platform.wheel_engine as wheel_engine

    asked: list[str] = []

    def check(exe: str) -> bool:
        asked.append(exe)
        return available

    monkeypatch.setattr(gateway_restart, "resolve_restart_launcher", lambda: launcher)
    monkeypatch.setattr(wheel_engine, "respawn_executable", lambda: "/venv/bin/python3")
    monkeypatch.setattr(update_boot_apply.platform_compat, "execv_target_available", check)
    return asyncio.run(update_boot_apply._resolve_exec_target()), asked


def test_an_edition_launcher_is_the_exec_target_ahead_of_the_interpreter(monkeypatch) -> None:
    target, asked = _resolve_with(monkeypatch, "/opt/kirocrew/bin/kirocrew", True)

    assert target == ("/opt/kirocrew/bin/kirocrew", None)
    assert asked == []


def test_without_a_launcher_the_restart_interpreter_is_the_exec_target(monkeypatch) -> None:
    target, asked = _resolve_with(monkeypatch, None, True)

    assert target == (None, "/venv/bin/python3")
    assert asked == ["/venv/bin/python3"]


def test_an_interpreter_that_cannot_be_exec_d_leaves_no_target(monkeypatch) -> None:
    target, _ = _resolve_with(monkeypatch, None, False)

    assert target == (None, None)


def test_the_reexec_keeps_the_gateway_arguments_for_a_launcher_or_an_interpreter(
    monkeypatch,
) -> None:
    seen: list[tuple] = []
    monkeypatch.setattr(sys, "argv", ["kirocrew", "serve", "--port", "7001"])
    monkeypatch.setattr(
        update_boot_apply.platform_compat,
        "reexec_launcher",
        lambda launcher, args: seen.append(("launcher", launcher, args)),
    )
    monkeypatch.setattr(
        update_boot_apply.platform_compat,
        "reexec_python_module",
        lambda module, args, executable: seen.append(("python", module, args, executable)),
    )

    update_boot_apply._reexec("/opt/kirocrew/bin/kirocrew", None)
    update_boot_apply._reexec(None, "/venv/bin/python3")

    assert seen == [
        ("launcher", "/opt/kirocrew/bin/kirocrew", ["serve", "--port", "7001"]),
        ("python", "kiro_crew", ["serve", "--port", "7001"], "/venv/bin/python3"),
    ]


def test_an_install_with_no_usable_interpreter_records_why_and_keeps_starting(
    calls, monkeypatch
) -> None:
    async def nothing():
        return None, None

    monkeypatch.setattr(update_boot_apply, "_resolve_exec_target", nothing)
    update_next_start.arm("2.0.0")
    calls.installed_after = "2.0.0"

    assert _boot() is False

    assert calls.execs == []
    assert "no usable interpreter" in update_next_start.read().pending_apply_error


def test_an_unreadable_arm_starts_the_gateway_without_applying(calls, monkeypatch) -> None:
    def unreadable():
        raise OSError("state file is corrupt")

    monkeypatch.setattr(update_next_start, "read", unreadable)

    assert _boot() is False

    assert calls.child == 0 and calls.execs == []


def test_an_unexpected_apply_error_is_recorded_and_never_raised(calls, monkeypatch) -> None:
    def refuse():
        raise RuntimeError("state directory is read-only")

    monkeypatch.setattr(update_next_start, "note_boot_attempt", refuse)
    update_next_start.arm("2.0.0")

    assert _boot() is False

    assert calls.child == 0
    assert update_next_start.read().pending_apply_error == "state directory is read-only"


def test_an_apply_error_that_cannot_even_be_recorded_still_never_raises(calls, monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise RuntimeError()

    update_next_start.arm("2.0.0")
    monkeypatch.setattr(update_next_start, "note_boot_attempt", refuse)
    monkeypatch.setattr(update_next_start, "finish", refuse)

    assert _boot() is False

    assert calls.child == 0 and calls.execs == []


def _block_in_child(monkeypatch) -> asyncio.Event:
    started = asyncio.Event()

    async def child():
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(update_boot_apply, "_run_apply_child", child)
    return started


def test_a_cancel_that_is_not_a_sigterm_propagates_without_ending_the_process(
    calls, monkeypatch
) -> None:
    import signal

    exits: list[str] = []
    monkeypatch.setattr(update_boot_apply, "_exit_with_sigterm_status", lambda: exits.append("x"))
    update_next_start.arm("2.0.0")
    before = signal.getsignal(signal.SIGTERM)

    async def run() -> None:
        started = _block_in_child(monkeypatch)
        task = asyncio.ensure_future(update_boot_apply.apply_armed_update_at_boot())
        await await_until(started, what="the fake apply child to start")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert exits == []
    # Whichever path the platform took, the SIGTERM handling is back as it was.
    assert signal.getsignal(signal.SIGTERM) == before


def test_a_boot_apply_off_the_main_thread_still_applies(calls) -> None:
    """On POSIX ``add_signal_handler`` refuses off the main thread; the apply runs anyway."""
    from concurrent.futures import ThreadPoolExecutor

    update_next_start.arm("2.0.0")
    calls.installed_after = "2.0.0"

    with ThreadPoolExecutor(max_workers=1) as pool:
        applied = pool.submit(_boot).result()

    assert applied is True
    assert calls.child == 1 and len(calls.execs) == 1


def test_ending_with_the_sigterm_status_drops_any_handler_then_signals_itself(
    monkeypatch,
) -> None:
    import os
    import signal

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(update_boot_apply.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    previous = signal.signal(signal.SIGTERM, lambda *_: None)
    try:
        update_boot_apply._exit_with_sigterm_status()

        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert sent == [(os.getpid(), signal.SIGTERM)]
