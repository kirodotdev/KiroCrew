"""Tests for the cross-platform sleep inhibitor and the prevent-sleep gate."""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew import platform_compat, power
from kiro_crew.power import SleepInhibitor


class _FakeProc:
    """Minimal Popen stand-in: alive until its process tree is killed.

    Registers itself by ``pid`` so the patched ``kill_process_tree`` (see
    ``force_posix``) can mark the right instance terminated — mirroring the
    group-kill the real release path performs.
    """

    _by_pid: "dict[int, _FakeProc]" = {}
    _next_pid = 4000

    def __init__(self) -> None:
        self._alive = True
        self.terminated = False
        _FakeProc._next_pid += 1
        self.pid = _FakeProc._next_pid
        _FakeProc._by_pid[self.pid] = self

    def poll(self):  # type: ignore[no-untyped-def]
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False


@pytest.fixture
def force_posix(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)

    # Release group-kills the helper's process tree; route it to the fake so
    # tests observe the terminate without signalling a real process group.
    def _kill_tree(pid, sig=platform_compat.SIGTERM):  # type: ignore[no-untyped-def]
        proc = _FakeProc._by_pid.get(pid)
        if proc is not None:
            proc.terminated = True
            proc._alive = False
        return True

    monkeypatch.setattr(platform_compat, "kill_process_tree", _kill_tree)


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

    monkeypatch.setattr(platform_compat, "kill_process_tree", _boom)
    inh.set_active(False)
    assert inh.active is False
    assert inh._proc is procs[0]  # retained so a later release/shutdown can retry

    # A subsequent release whose kill succeeds finally clears the handle.
    def _ok(pid, sig=platform_compat.SIGTERM):  # type: ignore[no-untyped-def]
        procs[0]._alive = False
        return True

    monkeypatch.setattr(platform_compat, "kill_process_tree", _ok)
    inh.set_active(False)
    assert inh._proc is None


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
    assert asyncio.run(server._should_prevent_sleep(_FakeState(_FakeSessions(True)))) is False


def test_should_prevent_sleep_requires_active_turn(monkeypatch):  # type: ignore[no-untyped-def]
    server = _patch_prevent_sleep_flag(monkeypatch, enabled=True)
    assert asyncio.run(server._should_prevent_sleep(_FakeState(_FakeSessions(False)))) is False
    assert asyncio.run(server._should_prevent_sleep(_FakeState(_FakeSessions(True)))) is True


def test_should_prevent_sleep_no_sessions(monkeypatch):  # type: ignore[no-untyped-def]
    server = _patch_prevent_sleep_flag(monkeypatch, enabled=True)
    assert asyncio.run(server._should_prevent_sleep(_FakeState(None))) is False


def test_prevent_sleep_is_editable_and_defaults_off():
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    assert _EDITABLE_CONFIG.get("dashboard.prevent_sleep") == {"type": "bool"}
    assert KiroCrewConfig().dashboard.prevent_sleep is False
