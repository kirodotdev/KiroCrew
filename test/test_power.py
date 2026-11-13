"""Tests for the cross-platform sleep inhibitor and the prevent-sleep gate."""

from __future__ import annotations

import asyncio
import os
import select
import subprocess
import time

import pytest

from kiro_crew import platform_compat, power
from kiro_crew.power import SleepInhibitor


class _FakeProc:
    """Minimal Popen stand-in: alive until its process group is signalled.

    Registers itself by ``pid`` so the patched ``kill_process_group`` (see
    ``force_posix``) can mark the right instance terminated — mirroring the
    group-kill the real release path performs. Like ``Popen``, ``poll()`` on an
    exited process reaps it and records ``returncode``.
    """

    _by_pid: "dict[int, _FakeProc]" = {}
    _next_pid = 4000

    def __init__(self) -> None:
        self._alive = True
        # A member of the helper's group other than its leader (the Linux
        # ``sh`` PID-watch), which can outlive the leader.
        self.group_member_alive = False
        self.terminated = False
        self.returncode: "int | None" = None
        _FakeProc._next_pid += 1
        self.pid = _FakeProc._next_pid
        _FakeProc._by_pid[self.pid] = self

    def poll(self):  # type: ignore[no-untyped-def]
        if not self._alive and self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False


@pytest.fixture
def force_posix(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)

    # The fake pids are not real processes, so the group signal and the zombie
    # probe are routed to the fakes and never reach a real process group.
    def _kill_group(pgid, sig=platform_compat.SIGTERM):  # type: ignore[no-untyped-def]
        proc = _FakeProc._by_pid[pgid]
        if not (proc._alive or proc.group_member_alive):
            raise ProcessLookupError(pgid)
        proc.terminated = True
        proc._alive = False
        proc.group_member_alive = False
        return True

    def _is_zombie(pid):  # type: ignore[no-untyped-def]
        return not _FakeProc._by_pid[pid]._alive

    def _no_pid_tree_kill(pid, sig=platform_compat.SIGTERM):  # type: ignore[no-untyped-def]
        raise AssertionError("the helper is stopped through its process group")

    monkeypatch.setattr(platform_compat, "kill_process_group", _kill_group)
    monkeypatch.setattr(platform_compat, "pid_is_zombie", _is_zombie)
    monkeypatch.setattr(platform_compat, "kill_process_tree", _no_pid_tree_kill)


def _force_macos(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(platform_compat, "IS_MACOS", True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)


def test_set_active_engages_once_and_releases(monkeypatch, force_posix):  # type: ignore[no-untyped-def]
    _force_macos(monkeypatch)
    procs: list[_FakeProc] = []
    monkeypatch.setattr(
        power, "_spawn_posix_inhibitor", lambda reason: procs.append(_FakeProc()) or procs[-1]
    )

    inh = SleepInhibitor()
    assert inh.active is False

    inh.set_active(True)
    assert inh.active is True
    assert len(procs) == 1

    # Idempotent: a second engage does not spawn a second helper.
    inh.set_active(True)
    assert len(procs) == 1

    inh.set_active(False)
    assert inh.active is False
    assert procs[0].terminated is True

    # Idempotent release: no error, no re-terminate churn.
    inh.set_active(False)
    assert inh.active is False


def test_macos_spawns_caffeinate_with_pid_watch(monkeypatch, force_posix):  # type: ignore[no-untyped-def]
    _force_macos(monkeypatch)
    monkeypatch.setattr(power, "_resolve_trusted", lambda paths: paths[0])
    captured: dict[str, object] = {}

    class _P:
        def __init__(self, argv, **kw):  # type: ignore[no-untyped-def]
            captured["argv"] = argv
            captured["kw"] = kw

        def poll(self):  # type: ignore[no-untyped-def]
            return None

        def terminate(self) -> None:
            pass

    monkeypatch.setattr(power.subprocess, "Popen", _P)

    inh = SleepInhibitor()
    inh.set_active(True)

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "/usr/bin/caffeinate"
    assert "-i" in argv  # block idle system sleep
    assert "-w" in argv  # auto-exit when the gateway PID dies
    assert inh.active is True


def test_linux_spawns_systemd_inhibit(monkeypatch, force_posix):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(platform_compat, "IS_MACOS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(power, "_resolve_trusted", lambda paths: paths[0])
    captured: dict[str, object] = {}

    class _P:
        def __init__(self, argv, **kw):  # type: ignore[no-untyped-def]
            captured["argv"] = argv

        def poll(self):  # type: ignore[no-untyped-def]
            return None

        def terminate(self) -> None:
            pass

    monkeypatch.setattr(power.subprocess, "Popen", _P)

    inh = SleepInhibitor()
    inh.set_active(True)

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "/usr/bin/systemd-inhibit"
    assert any("idle:sleep" in a for a in argv)
    # A PID-watch command so a crashed gateway auto-releases the inhibitor lock.
    assert any("kill -0" in a for a in argv)
    # The shell is the absolute /bin/sh, never PATH-resolved.
    assert "/bin/sh" in argv
    # The PID-watch runs an absolute `sleep`, never PATH-resolved by sh.
    assert any("/usr/bin/sleep " in a for a in argv)
    assert inh.active is True


def test_missing_backend_binary_is_noop(monkeypatch, force_posix):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(platform_compat, "IS_MACOS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(power, "_resolve_trusted", lambda paths: None)

    inh = SleepInhibitor()
    inh.set_active(True)
    # No systemd-inhibit on this host: stays inactive rather than raising.
    assert inh.active is False


def test_windows_uses_execution_state(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    calls: list[bool] = []
    monkeypatch.setattr(
        power,
        "_set_windows_execution_state",
        lambda keep_awake: calls.append(keep_awake) or True,
    )

    inh = SleepInhibitor()
    inh.set_active(True)
    assert calls == [True]
    assert inh.active is True

    inh.set_active(False)
    assert calls == [True, False]
    assert inh.active is False


def test_respawn_when_posix_helper_dies(monkeypatch, force_posix):  # type: ignore[no-untyped-def]
    _force_macos(monkeypatch)
    procs: list[_FakeProc] = []
    monkeypatch.setattr(
        power, "_spawn_posix_inhibitor", lambda reason: procs.append(_FakeProc()) or procs[-1]
    )

    inh = SleepInhibitor()
    inh.set_active(True)
    assert len(procs) == 1

    # The helper dies (killed, or its watched PID was reused). A repeated
    # "keep awake" must respawn rather than trust the dead process.
    procs[0]._alive = False
    inh.set_active(True)
    assert len(procs) == 2
    assert inh.active is True


def test_gives_up_after_repeated_immediate_exits(monkeypatch, force_posix):  # type: ignore[no-untyped-def]
    _force_macos(monkeypatch)
    procs: list[_FakeProc] = []

    def _spawn_dead(reason):  # type: ignore[no-untyped-def]
        p = _FakeProc()
        p._alive = False  # dead on arrival (e.g. systemd-inhibit with no logind session)
        procs.append(p)
        return p

    monkeypatch.setattr(power, "_spawn_posix_inhibitor", _spawn_dead)

    inh = SleepInhibitor()
    # A helper that always dies immediately must NOT be re-forked every poll.
    for _ in range(10):
        inh.set_active(True)
    assert len(procs) <= power._MAX_HELPER_RESPAWNS + 1  # 1 engage + capped respawns
    assert inh.active is False

    # A release is an idle boundary that re-arms it — the next turn retries once.
    before = len(procs)
    inh.set_active(False)
    inh.set_active(True)
    assert len(procs) == before + 1


def test_windows_engage_failure_preserves_prior_applied_state(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    # The execution-state request always fails on this run.
    monkeypatch.setattr(power, "_set_windows_execution_state", lambda keep_awake: False)

    inh = SleepInhibitor()
    # Simulate a prior release that FAILED to clear (request still set + tracked).
    inh._win_applied = True

    inh.set_active(True)  # engage fails too
    assert inh.active is False
    # The still-active request must stay tracked so a later release retries it,
    # instead of being clobbered to False (which would strand it set forever).
    assert inh._win_applied is True


def test_posix_release_retains_handle_when_kill_fails(monkeypatch, force_posix):  # type: ignore[no-untyped-def]
    _force_macos(monkeypatch)
    procs: list[_FakeProc] = []
    monkeypatch.setattr(
        power, "_spawn_posix_inhibitor", lambda reason: procs.append(_FakeProc()) or procs[-1]
    )

    inh = SleepInhibitor()
    inh.set_active(True)
    assert len(procs) == 1

    # A genuine signal error (not already-exited) must NOT discard the handle.
    def _boom(pid, sig=platform_compat.SIGTERM):  # type: ignore[no-untyped-def]
        raise OSError("EPERM")

    monkeypatch.setattr(platform_compat, "kill_process_group", _boom)
    inh.set_active(False)
    assert inh.active is False
    assert inh._proc is procs[0]  # retained so a later release/shutdown can retry

    # A subsequent release whose kill succeeds finally clears the handle.
    def _ok(pid, sig=platform_compat.SIGTERM):  # type: ignore[no-untyped-def]
        procs[0]._alive = False
        return True

    monkeypatch.setattr(platform_compat, "kill_process_group", _ok)
    inh.set_active(False)
    assert inh._proc is None


def _leader_died_leaving_its_pid_watch(proc: _FakeProc) -> None:
    """``systemd-inhibit`` exits on its own; its ``sh`` keeps running in the group."""
    proc._alive = False
    proc.group_member_alive = True


def test_respawn_stops_the_dead_helpers_pid_watch(monkeypatch, force_posix):  # type: ignore[no-untyped-def]
    procs: list[_FakeProc] = []
    monkeypatch.setattr(
        power, "_spawn_posix_inhibitor", lambda reason: procs.append(_FakeProc()) or procs[-1]
    )

    inh = SleepInhibitor()
    inh.set_active(True)
    _leader_died_leaving_its_pid_watch(procs[0])

    inh.set_active(True)  # the next busy poll respawns the helper
    assert len(procs) == 2
    assert procs[0].terminated is True
    assert procs[0].group_member_alive is False
    assert procs[0].returncode is not None  # reaped only after the group signal


def test_release_stops_the_pid_watch_of_a_helper_that_already_exited(monkeypatch, force_posix):  # type: ignore[no-untyped-def]
    procs: list[_FakeProc] = []
    monkeypatch.setattr(
        power, "_spawn_posix_inhibitor", lambda reason: procs.append(_FakeProc()) or procs[-1]
    )

    inh = SleepInhibitor()
    inh.set_active(True)
    _leader_died_leaving_its_pid_watch(procs[0])

    inh.set_active(False)  # the turn ends before any poll noticed the death
    assert procs[0].group_member_alive is False
    assert inh._proc is None


def test_an_emptied_group_reads_as_gone(monkeypatch, force_posix):  # type: ignore[no-untyped-def]
    proc = _FakeProc()
    proc._alive = False  # an unreaped leader and no other member left
    assert power._terminate(proc) is True  # type: ignore[arg-type]
    assert proc.returncode is not None


def test_a_reaped_helper_is_never_group_signalled(monkeypatch, force_posix):  # type: ignore[no-untyped-def]
    proc = _FakeProc()
    _leader_died_leaving_its_pid_watch(proc)
    proc.poll()  # reaped: the pid, and the group id it named, may be reused
    # Another process now leads a live group under that id.
    monkeypatch.setattr(platform_compat, "pgroup_exists", lambda pgid: True)

    def _must_not_signal(pgid, sig=platform_compat.SIGTERM):  # type: ignore[no-untyped-def]
        raise AssertionError("signalled the group of a reaped helper")

    monkeypatch.setattr(platform_compat, "kill_process_group", _must_not_signal)
    assert power._terminate(proc) is True  # type: ignore[arg-type]


def test_dead_helper_is_detected_without_reaping(monkeypatch, force_posix):  # type: ignore[no-untyped-def]
    proc = _FakeProc()
    _leader_died_leaving_its_pid_watch(proc)
    assert power._helper_exited(proc) is True  # type: ignore[arg-type]
    assert proc.returncode is None  # still unreaped, so its group can be signalled


def test_unreadable_process_state_falls_back_to_poll(monkeypatch, force_posix):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(platform_compat, "pid_is_zombie", lambda pid: None)
    proc = _FakeProc()
    assert power._helper_exited(proc) is False  # type: ignore[arg-type]
    proc._alive = False
    assert power._helper_exited(proc) is True  # type: ignore[arg-type]


def test_a_group_signal_error_keeps_the_handle(monkeypatch, force_posix):  # type: ignore[no-untyped-def]
    proc = _FakeProc()
    _leader_died_leaving_its_pid_watch(proc)

    def _eperm(pgid, sig=platform_compat.SIGTERM):  # type: ignore[no-untyped-def]
        raise PermissionError("EPERM")

    monkeypatch.setattr(platform_compat, "kill_process_group", _eperm)
    assert power._terminate(proc) is False  # type: ignore[arg-type]


_REAL_HELPER_SKIP = pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="the helper's process group is POSIX-only"
)
# How long a real child is given to start, or to exit once signalled: a bound
# on the wait, never a lifetime the child runs out on its own.
_REAL_CHILD_WAIT_SECS = 10.0


def _spawn_real_helper(tmp_path, *, leader_exits: bool):  # type: ignore[no-untyped-def]
    """Spawn a faithful stand-in for the Linux helper in its own session.

    The leader plays ``systemd-inhibit``: it starts a background ``sh`` that
    plays the PID-watch. That ``sh`` holds the FIFO ``barrier`` open and blocks
    reading it; nothing writes to it, so it runs until a signal stops it. With
    *leader_exits* the leader exits at once, as an OOM-killed
    ``systemd-inhibit`` would, leaving the PID-watch in its group.

    The pipes carry the signals the test waits on: only the leader holds
    stderr (the PID-watch closes its copy), so EOF there means the leader has
    exited; stdout is held by both, and the PID-watch writes ``started`` to it
    once it is blocked, so EOF there after the leader exited means the
    PID-watch has exited too. An exited process holds no descriptors, so an
    unreaped zombie does not keep either pipe open.
    """
    barrier = tmp_path / "barrier"
    os.mkfifo(barrier)
    tail = "exit 0" if leader_exits else "wait"
    script = f'(exec 2>&-; exec 3<>"{barrier}"; echo started; read _ <&3) & {tail}'
    proc = subprocess.Popen(
        ["/bin/sh", "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    return proc, barrier


def _read_until(fd: int, *, line: bool) -> bool:
    """Read *fd* to a newline (*line*) or to EOF; False if the bound passes first.

    ``select`` wakes the moment data or EOF arrives, so this waits on the
    child's own signal and the bound only caps a child that never sends it.
    """
    deadline = time.monotonic() + _REAL_CHILD_WAIT_SECS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            return False
        chunk = os.read(fd, 4096)
        if not chunk:
            return not line
        if line and b"\n" in chunk:
            return True


def _at_eof_now(fd: int) -> bool:
    readable, _, _ = select.select([fd], [], [], 0)
    return bool(readable) and not os.read(fd, 4096)


def _stop_leftovers(proc, barrier) -> None:  # type: ignore[no-untyped-def]
    """Clean up whatever a failed test left running, without a stale group signal.

    The group is signalled only while the leader is unreaped. A PID-watch still
    blocked on the barrier is released by writing to it, which reaches it
    whether or not its leader was reaped.
    """
    if proc.returncode is None:
        try:
            platform_compat.kill_process_group(proc.pid, platform_compat.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=_REAL_CHILD_WAIT_SECS)
    try:
        fd = os.open(barrier, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        pass
    else:
        try:
            os.write(fd, b"\n")
        finally:
            os.close(fd)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


@_REAL_HELPER_SKIP
def test_real_helper_whose_leader_exited_has_its_pid_watch_stopped(tmp_path):  # type: ignore[no-untyped-def]
    proc, barrier = _spawn_real_helper(tmp_path, leader_exits=True)
    out, err = proc.stdout.fileno(), proc.stderr.fileno()
    try:
        assert _read_until(out, line=True)  # the PID-watch is blocked
        # The leader exits and is left unreaped, as _verify_alive leaves it.
        assert _read_until(err, line=False)
        assert proc.returncode is None
        assert not _at_eof_now(out)  # the leader is gone, the PID-watch is not

        assert power._terminate(proc) is True
        assert _read_until(out, line=False)  # the PID-watch has exited
    finally:
        _stop_leftovers(proc, barrier)


@_REAL_HELPER_SKIP
def test_real_running_helper_release_stops_leader_and_pid_watch(tmp_path):  # type: ignore[no-untyped-def]
    proc, barrier = _spawn_real_helper(tmp_path, leader_exits=False)
    out = proc.stdout.fileno()
    try:
        assert _read_until(out, line=True)
        assert proc.poll() is None

        assert power._terminate(proc) is True
        assert _read_until(out, line=False)  # leader and PID-watch have both exited
        proc.wait(timeout=_REAL_CHILD_WAIT_SECS)
    finally:
        _stop_leftovers(proc, barrier)


# ── Gateway desired-state gate ───────────────────────────────────────────────


class _FakeSessions:
    def __init__(self, active: bool) -> None:
        self._active = active

    def any_active_turn(self) -> bool:
        return self._active


class _FakeState:
    def __init__(self, sessions: object) -> None:
        self.sessions = sessions


def _patch_prevent_sleep_flag(monkeypatch, enabled: bool):  # type: ignore[no-untyped-def]
    from kiro_crew.dashboard import server

    class _Dash:
        prevent_sleep = enabled

    class _Cfg:
        dashboard = _Dash()

    monkeypatch.setattr(server.KiroCrewConfig, "load", classmethod(lambda cls: _Cfg()))
    return server


def test_should_prevent_sleep_off_by_config(monkeypatch):  # type: ignore[no-untyped-def]
    server = _patch_prevent_sleep_flag(monkeypatch, enabled=False)
    # Even with an active turn, an opted-out user's machine may sleep.
    assert asyncio.run(server._should_prevent_sleep(_FakeState(_FakeSessions(True)), 0)) is False


def test_should_prevent_sleep_requires_active_turn(monkeypatch):  # type: ignore[no-untyped-def]
    server = _patch_prevent_sleep_flag(monkeypatch, enabled=True)
    assert asyncio.run(server._should_prevent_sleep(_FakeState(_FakeSessions(False)), 0)) is False
    assert asyncio.run(server._should_prevent_sleep(_FakeState(_FakeSessions(True)), 0)) is True


def test_should_prevent_sleep_no_sessions(monkeypatch):  # type: ignore[no-untyped-def]
    server = _patch_prevent_sleep_flag(monkeypatch, enabled=True)
    assert asyncio.run(server._should_prevent_sleep(_FakeState(None), 0)) is False


def test_prevent_sleep_is_editable_and_defaults_off():
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    assert _EDITABLE_CONFIG.get("dashboard.prevent_sleep") == {"type": "bool"}
    assert KiroCrewConfig().dashboard.prevent_sleep is False
