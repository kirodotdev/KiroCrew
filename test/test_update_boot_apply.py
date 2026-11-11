"""An armed update applies at service boot, before the gateway serves anything.

The apply point (maintainer rulings 1 to 4 in the decision entry "updates never
restart a running gateway"): nothing runs at shutdown and nothing runs while the
gateway serves. Each case drives ``apply_armed_update_at_boot`` with the child
process and the re-exec replaced by recording stand-ins.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import patch

import pytest

from kiro_crew import update_boot_apply, update_next_start


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
        await asyncio.wait_for(started.wait(), timeout=30)
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
