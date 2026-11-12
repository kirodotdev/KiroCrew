"""An armed update applies at service boot, before the gateway serves anything.

The apply point (maintainer rulings 1 to 4 in the decision entry "updates never
restart a running gateway"): nothing runs at shutdown and nothing runs while the
gateway serves. Each case drives ``apply_armed_update_at_boot`` with the child
process and the re-exec replaced by recording stand-ins.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat, update_boot_apply, update_next_start
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
        #: Set once something waits for the child to end.
        self.awaited = asyncio.Event()

    def interrupted(self) -> None:
        if self._exits:
            self._done.set()

    async def communicate(self):
        self.awaited.set()
        await self._done.wait()
        return b"", None


@contextlib.contextmanager
def _child_doubles(proc: _Proc, events: list[str]):
    """Spawn ``proc`` instead of a real child, and record what stops it."""

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
    ):
        yield


def _run_child_with(exits_on_interrupt: bool, events: list[str]) -> tuple[bool, str]:
    async def run() -> tuple[bool, str]:
        with (
            _child_doubles(_Proc(exits_on_interrupt, events), events),
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
            await await_until(task, what="the apply to end after the SIGTERM")
        # The previous SIGTERM handling is back once the apply is over.
        assert asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM) is False

    asyncio.run(run())

    assert events == ["interrupt-then-grace", "exit"]


def test_the_sigterm_handler_is_restored_after_a_normal_boot_apply(calls) -> None:
    before = signal.getsignal(signal.SIGTERM)

    _boot()

    assert signal.getsignal(signal.SIGTERM) == before


class _EndedProc:
    """A child that has already ended with ``code`` and ``output``."""

    def __init__(self, code: int, output: bytes) -> None:
        self.returncode = code
        self._output = output

    async def communicate(self):
        return self._output, None


def _run_ended_child(monkeypatch, code: int, output: bytes) -> tuple[tuple[bool, str], dict]:
    """``_run_apply_child``'s answer for an ended child, and how that child was spawned."""
    spawned: dict = {}

    async def fake_spawn(*args, **kwargs):
        spawned.update(args=args, **kwargs)
        return _EndedProc(code, output)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    return asyncio.run(update_boot_apply._run_apply_child()), spawned


def test_the_apply_child_runs_the_update_command_in_its_own_session_with_no_terminal(
    monkeypatch,
) -> None:
    result, spawned = _run_ended_child(monkeypatch, 0, b"installed 2.0.0\n")

    assert result == (True, "installed 2.0.0")
    assert spawned["args"][-3:] == ("-m", "kiro_crew", "update")
    # Nobody can answer a prompt at boot, and a group signal must reach the child alone.
    assert spawned["stdin"] == asyncio.subprocess.DEVNULL
    assert spawned["start_new_session"] is platform_compat.IS_POSIX


def test_an_apply_child_that_exits_non_zero_fails_with_what_it_printed(monkeypatch) -> None:
    result, _ = _run_ended_child(monkeypatch, 3, b"step one\n\xffno space left\n")

    assert result == (False, "step one\n\ufffdno space left")


def test_the_apply_output_is_redacted_and_only_its_tail_is_kept(monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.security.redact", lambda text: text.replace("hunter2", "[redacted]")
    )
    output = b"y" * (update_boot_apply._OUTPUT_TAIL_CHARS * 2) + b" password hunter2\n"

    (ok, tail), _ = _run_ended_child(monkeypatch, 0, output)

    assert ok is True
    assert len(tail) == update_boot_apply._OUTPUT_TAIL_CHARS
    assert tail.endswith(" password [redacted]")


def test_cancelling_the_apply_interrupts_its_child_and_the_cancel_propagates() -> None:
    events: list[str] = []

    async def run() -> None:
        proc = _Proc(True, events)
        with _child_doubles(proc, events):
            task = asyncio.ensure_future(update_boot_apply._run_apply_child())
            await await_until(proc.awaited, what="the apply to wait for its child")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await await_until(task, what="the cancelled apply to unwind")

    asyncio.run(run())

    assert events == ["signal:SIGINT:4242"]


def test_an_apply_that_asked_a_question_at_boot_sends_the_person_to_a_terminal(calls) -> None:
    update_next_start.arm("2.0.0")
    calls.child_result = (False, "Discard local changes? [y/N]\nEOFError: EOF when reading a line")

    assert _boot() is False

    assert (
        update_next_start.read().pending_apply_error
        == "The update needs your confirmation. Run `kirocrew update` in a terminal."
    )


def test_a_failed_apply_that_printed_nothing_still_records_a_reason(calls) -> None:
    update_next_start.arm("2.0.0")
    calls.child_result = (False, " \n")

    assert _boot() is False

    assert update_next_start.read().pending_apply_error == "the update command failed"


def _resolve_target(
    monkeypatch, *, launcher: str | None, interpreter_usable: bool
) -> tuple[str | None, str | None]:
    """The real ``_resolve_exec_target`` over a stand-in launcher and interpreter."""
    monkeypatch.setattr("kiro_crew.gateway_restart.resolve_restart_launcher", lambda: launcher)
    monkeypatch.setattr(
        "kiro_crew.platform.wheel_engine.respawn_executable", lambda: "/opt/crew/bin/python"
    )
    monkeypatch.setattr(
        platform_compat,
        "execv_target_available",
        lambda path: interpreter_usable and path == "/opt/crew/bin/python",
    )
    return asyncio.run(update_boot_apply._resolve_exec_target())


def test_an_edition_launcher_is_the_exec_target_ahead_of_the_interpreter(monkeypatch) -> None:
    target = _resolve_target(monkeypatch, launcher="/opt/crew/launcher", interpreter_usable=True)

    assert target == ("/opt/crew/launcher", None)


def test_without_a_launcher_the_exec_target_is_the_interpreter_a_restart_would_use(
    monkeypatch,
) -> None:
    target = _resolve_target(monkeypatch, launcher=None, interpreter_usable=True)

    assert target == (None, "/opt/crew/bin/python")


def test_an_interpreter_that_cannot_be_execed_leaves_no_exec_target(monkeypatch) -> None:
    target = _resolve_target(monkeypatch, launcher=None, interpreter_usable=False)

    assert target == (None, None)


@pytest.mark.parametrize(
    ("launcher", "exe", "expected"),
    [
        (
            "/opt/crew/launcher",
            None,
            ("launcher", "/opt/crew/launcher", ["gateway", "--port", "1"]),
        ),
        (
            None,
            "/opt/crew/bin/python",
            ("module", "kiro_crew", ["gateway", "--port", "1"], "/opt/crew/bin/python"),
        ),
    ],
)
def test_the_reexec_keeps_this_process_arguments_on_either_target(
    monkeypatch, launcher, exe, expected
) -> None:
    seen: list[tuple] = []
    monkeypatch.setattr(sys, "argv", ["kirocrew", "gateway", "--port", "1"])
    monkeypatch.setattr(
        platform_compat,
        "reexec_launcher",
        lambda path, args: seen.append(("launcher", path, list(args))),
    )
    monkeypatch.setattr(
        platform_compat,
        "reexec_python_module",
        lambda module, args, executable=None: seen.append(
            ("module", module, list(args), executable)
        ),
    )

    update_boot_apply._reexec(launcher, exe)

    assert seen == [expected]


@pytest.mark.parametrize(
    "posix",
    [
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="the Windows event loop takes no signal handlers"
            ),
        ),
        False,
    ],
)
def test_sigterm_is_rerouted_during_the_apply_only_where_the_platform_has_signals(
    monkeypatch, posix
) -> None:
    before = signal.getsignal(signal.SIGTERM)
    during: list[object] = []

    async def apply() -> bool:
        during.append(signal.getsignal(signal.SIGTERM))
        return False

    monkeypatch.setattr(update_boot_apply, "_apply_armed_update_at_boot", apply)

    # Flipped only around a stand-in apply: real state writes follow the same flag.
    with patch.object(platform_compat, "IS_POSIX", posix):
        assert asyncio.run(update_boot_apply.apply_armed_update_at_boot()) is False

    assert len(during) == 1
    assert (during[0] != before) is posix


@pytest.mark.parametrize(
    "error",
    [NotImplementedError(), RuntimeError("no signal support"), ValueError("not the main thread")],
)
def test_a_loop_that_refuses_a_signal_handler_still_applies_the_update(
    calls, monkeypatch, error
) -> None:
    update_next_start.arm("2.0.0")
    calls.installed_after = "2.0.0"
    asked: list[int] = []

    def refuse(sig, callback, *args):
        asked.append(sig)
        raise error

    async def run() -> bool:
        monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", refuse)
        return await update_boot_apply.apply_armed_update_at_boot()

    assert asyncio.run(run()) is True

    assert calls.child == 1 and len(calls.execs) == 1
    # A platform without signals never asks for a handler; the others ask once.
    assert asked == ([signal.SIGTERM] if platform_compat.IS_POSIX else [])


def test_a_cancel_that_is_not_a_sigterm_propagates_and_leaves_the_update_armed(
    calls, monkeypatch
) -> None:
    exits: list[str] = []
    update_next_start.arm("2.0.0")
    started = asyncio.Event()

    async def child():
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(update_boot_apply, "_run_apply_child", child)
    monkeypatch.setattr(
        update_boot_apply, "_exit_with_sigterm_status", lambda: exits.append("exit")
    )

    async def run() -> None:
        task = asyncio.ensure_future(update_boot_apply.apply_armed_update_at_boot())
        await await_until(started, what="the fake apply child to start")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await await_until(task, what="the cancelled boot apply to unwind")

    asyncio.run(run())

    assert exits == []
    assert update_next_start.read().armed is True


def test_ending_with_the_sigterm_status_restores_the_default_handling_then_signals_itself() -> None:
    steps: list[tuple] = []

    with (
        patch.object(
            update_boot_apply.signal,
            "signal",
            lambda sig, handler: steps.append(("handling", sig, handler)),
        ),
        patch.object(update_boot_apply.os, "kill", lambda pid, sig: steps.append((pid, sig))),
    ):
        update_boot_apply._exit_with_sigterm_status()

    # In this order: with the loop's handler still installed the signal would be swallowed.
    assert steps == [
        ("handling", signal.SIGTERM, signal.SIG_DFL),
        (os.getpid(), signal.SIGTERM),
    ]


def test_an_unreadable_armed_state_starts_the_gateway_without_applying(calls, monkeypatch) -> None:
    def unreadable():
        raise OSError("the state file is a directory")

    monkeypatch.setattr(update_next_start, "read", unreadable)

    assert _boot() is False

    assert calls.child == 0 and calls.execs == []


def test_an_install_with_nothing_to_exec_records_the_error_and_keeps_the_old_version(
    calls, monkeypatch
) -> None:
    update_next_start.arm("2.0.0")
    calls.installed_after = "2.0.0"

    async def nothing():
        return None, None

    monkeypatch.setattr(update_boot_apply, "_resolve_exec_target", nothing)

    assert _boot() is False

    assert calls.execs == []
    state = update_next_start.read()
    assert state.armed is False
    assert "no usable interpreter" in state.pending_apply_error


def test_a_launcher_the_edition_refuses_is_recorded_and_the_gateway_still_starts(
    calls, monkeypatch
) -> None:
    update_next_start.arm("2.0.0")
    calls.installed_after = "2.0.0"

    async def refused():
        raise ValueError("Cannot restart: invalid gateway launcher path")

    monkeypatch.setattr(update_boot_apply, "_resolve_exec_target", refused)

    assert _boot() is False

    assert calls.execs == []
    state = update_next_start.read()
    assert state.armed is False
    assert state.pending_apply_error == "Cannot restart: invalid gateway launcher path"


def test_a_failure_with_no_message_is_recorded_by_its_type(calls, monkeypatch) -> None:
    update_next_start.arm("2.0.0")

    def broken():
        raise RuntimeError

    monkeypatch.setattr(update_next_start, "installed_version", broken)

    assert _boot() is False

    assert calls.child == 0
    assert update_next_start.read().pending_apply_error == "RuntimeError"


def test_an_arm_that_cannot_be_cleared_is_never_execed_onto_and_does_not_stop_the_start(
    calls, monkeypatch
) -> None:
    update_next_start.arm("2.0.0")
    calls.installed_after = "2.0.0"

    def read_only(*, error: str = "") -> None:
        raise OSError("read-only home")

    monkeypatch.setattr(update_next_start, "finish", read_only)

    assert _boot() is False

    assert calls.child == 1 and calls.execs == []
