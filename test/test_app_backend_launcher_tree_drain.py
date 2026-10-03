"""An app backend's tree is drained even when its launcher is already gone.

Three exits of ``kiro_crew.apps.backend`` signal a spawned backend's tree: the
startup-survival failure branch (the launcher died during the bind window), the
retired-spawn branch (a concurrent start took publication), and
``stop_app_backend`` (disable, uninstall, gateway shutdown -- with the root alive
or already exited). ``kill_process_tree`` alone cannot serve them: on Windows it
is ``taskkill /T /PID <root>``, a walk FROM the root that reaches nothing once
the root has exited, and on POSIX ``getpgid`` raises for a reaped leader. A
launcher that forks its real server and returns 0 is exactly that shape, so each
exit must key on an identity captured while the root was still pinned.

These tests simulate Windows by pointing ``platform_compat.IS_WINDOWS`` at True
on every host and closing the kernel boundary at ``kill_process_tree_pinned``
and ``kill_process_tree``, the same seam ``test_app_backend_stale_reap.py`` uses;
nothing here spawns a process, binds a port or touches the real pidfile.
"""

from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import kiro_crew.apps.backend as bmod
from kiro_crew.apps.backend import AppProcess

SIGTERM = bmod.platform_compat.SIGTERM
SIGKILL = bmod.platform_compat.SIGKILL


class _FakeProc:
    """``Popen`` stand-in: a pid plus a controllable exit status."""

    def __init__(self, pid: int = 4242, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.wait_raises = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.wait_raises:
            raise subprocess.TimeoutExpired(cmd="app", timeout=timeout or 0)
        return self.returncode or 0


class _FakeSock:
    def __init__(self, connect_exc: BaseException | None) -> None:
        self._connect_exc = connect_exc

    def __enter__(self) -> _FakeSock:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def settimeout(self, _timeout: float) -> None:
        return None

    def bind(self, _addr: Any) -> None:
        return None

    def connect(self, _addr: Any) -> None:
        if self._connect_exc is not None:
            raise self._connect_exc


def _fake_proc(pid: int = 4242, returncode: int | None = None) -> Any:
    """A ``Popen`` stand-in typed as ``Any`` so ``Popen``-typed parameters accept it."""
    return _FakeProc(pid=pid, returncode=returncode)


def _manifest(entry_point: str = "server.py") -> SimpleNamespace:
    return SimpleNamespace(
        backend=SimpleNamespace(entryPoint=entry_point, port="auto", healthCheck="/health", type="")
    )


@pytest.fixture(autouse=True)
def _isolated_module_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A clean process table, a private pidfile and a silent audit sink per test."""
    home = tmp_path / "kirocrew-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(bmod, "_pidfile_path", lambda: tmp_path / "app_backends.pids.json")
    monkeypatch.setattr(bmod, "sel", lambda: MagicMock())
    with bmod._lock:
        bmod._processes.clear()
        bmod._allocated_ports.clear()
        bmod._restart_attempts.clear()
        bmod._lifecycle_generation.clear()
    yield
    with bmod._lock:
        bmod._processes.clear()
        bmod._allocated_ports.clear()
        bmod._restart_attempts.clear()
        bmod._lifecycle_generation.clear()


@pytest.fixture()
def spawn_root(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """An app root wired so ``_start_app_backend_body`` runs without side effects."""
    root = tmp_path / "app-root"
    root.mkdir()
    (root / "server.py").write_text("x = 1\n")
    monkeypatch.setattr(bmod, "app_dir", lambda _name: root)
    monkeypatch.setattr(bmod, "app_execution_denied", lambda _name, **_kw: None)
    monkeypatch.setattr(bmod, "wrap_argv", lambda argv, **_kw: (list(argv), None))
    monkeypatch.setattr(bmod, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(bmod, "_health_check_loop", lambda *_a, **_k: None)
    monkeypatch.setattr(bmod, "_start_health_supervisor", lambda *_a: None)
    monkeypatch.setattr(
        bmod,
        "socket",
        SimpleNamespace(
            AF_INET=2,
            SOCK_STREAM=1,
            socket=lambda *_a, **_k: _FakeSock(OSError("connection refused")),
        ),
    )
    return root


@pytest.fixture()
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate Windows at the platform seam; every kernel call is closed below."""
    monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(bmod.platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(
        bmod.platform_compat,
        "kill_process_tree",
        lambda *_a, **_k: pytest.fail("the numeric taskkill must not be the first resort"),
    )


def _pinned_recorder(
    monkeypatch: pytest.MonkeyPatch, *, result: bool = True
) -> list[tuple[int, str, int]]:
    calls: list[tuple[int, str, int]] = []

    def _pinned(pid: int, start_time: str, sig: int = SIGTERM, **_kw: Any) -> bool:
        calls.append((pid, start_time, sig))
        return result

    monkeypatch.setattr(bmod.platform_compat, "kill_process_tree_pinned", _pinned)
    return calls


# ---------------------------------------------------------------------------
# Startup-survival failure: the launcher exited, its tree must still be drained
# ---------------------------------------------------------------------------


class TestFailedLauncherOnWindows:
    def test_the_tree_is_drained_by_the_identity_read_before_the_survival_check(
        self, spawn_root: Any, windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact-handle drain is keyed on the root's creation identity, and
        that identity is only probeable while the root is still ours to open.
        The probe here answers only BEFORE the survival check reports the exit;
        reading it any later hands the drain nothing."""
        launcher = _FakeProc(pid=4242, returncode=0)
        exited = {"seen": False}

        def _start_time(pid: int) -> str | None:
            if pid == 4242 and not exited["seen"]:
                return "ST-7"
            return None

        def _survived(_proc: Any, _port: int | None = None) -> bool:
            exited["seen"] = True
            return False

        monkeypatch.setattr(bmod, "_proc_start_time", _start_time)
        monkeypatch.setattr(bmod, "_survived_spawn", _survived)
        monkeypatch.setattr(bmod, "popen_limited", lambda *_a, **_k: launcher)
        drained = _pinned_recorder(monkeypatch)

        assert bmod._start_app_backend_body("forky", _manifest()) is None
        assert drained == [(4242, "ST-7", SIGTERM)]
        assert "forky" not in bmod._processes
        assert bmod._read_pidfile() == {}, "a failed spawn still records no pidfile row"

    def test_a_capacity_refusal_is_logged_and_the_spawn_still_fails_cleanly(
        self,
        spawn_root: Any,
        windows: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No fallback to the numeric taskkill: the root it would walk from is the
        exited one, so the tree is left to the cleanup registry's maintenance --
        the same answer the stale reaper gives a refusal."""
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "ST-7")
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _p, _port=None: False)
        monkeypatch.setattr(bmod, "popen_limited", lambda *_a, **_k: _FakeProc(returncode=0))

        def _refused(*_a: Any, **_k: Any) -> bool:
            raise bmod.platform_compat.WindowsCleanupCapacityError("root capacity exhausted")

        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree_pinned", _refused)
        with caplog.at_level(logging.WARNING):
            assert bmod._start_app_backend_body("forky", _manifest()) is None
        assert any(
            "capacity refused" in r.getMessage() and "left to maintenance" in r.getMessage()
            for r in caplog.records
        )

    def test_a_drain_that_raises_does_not_mask_the_startup_failure(
        self, spawn_root: Any, windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "ST-7")
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _p, _port=None: False)
        monkeypatch.setattr(bmod, "popen_limited", lambda *_a, **_k: _FakeProc(returncode=1))

        def _incomplete(*_a: Any, **_k: Any) -> bool:
            raise OSError("Windows process tree did not drain")

        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree_pinned", _incomplete)
        assert bmod._start_app_backend_body("forky", _manifest()) is None
        assert "forky" not in bmod._processes

    def test_no_identity_means_no_signal(
        self,
        spawn_root: Any,
        windows: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An unprobeable root is one whose tree cannot be pinned; nothing is
        aimed at a bare number."""
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: None)
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _p, _port=None: False)
        monkeypatch.setattr(bmod, "popen_limited", lambda *_a, **_k: _FakeProc(returncode=0))
        drained = _pinned_recorder(monkeypatch)
        with caplog.at_level(logging.WARNING):
            assert bmod._start_app_backend_body("forky", _manifest()) is None
        assert drained == []
        assert any("identity was not captured" in r.getMessage() for r in caplog.records)


class TestFailedLauncherOnPosix:
    """POSIX has no root handle to pin. The group outlives its leader and its id is
    the leader's pid, but ``getpgid`` raises for a reaped pid, so the drain takes
    the stale reaper's route: members vouched by this spawn's instance token."""

    @pytest.fixture(autouse=True)
    def _posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(bmod.platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(bmod, "_REAP_SIGTERM_GRACE", 0.0)
        monkeypatch.setattr(bmod, "_REAP_POLL_INTERVAL", 0.0)
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree_pinned",
            lambda *_a, **_k: pytest.fail("the Windows drain is not a POSIX primitive"),
        )
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree",
            lambda *_a, **_k: pytest.fail("getpgid on a reaped leader raises; never called"),
        )

    def test_vouched_members_take_sigterm_and_the_stubborn_ones_sigkill(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probes: list[int] = []

        def _probe(pid: int) -> str | None:
            probes.append(pid)
            return None

        monkeypatch.setattr(bmod, "_proc_start_time", _probe)
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _p, _port=None: False)
        monkeypatch.setattr(bmod, "popen_limited", lambda *_a, **_k: _FakeProc(returncode=0))
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)
        alive = {5001: True, 5002: True}
        monkeypatch.setattr(bmod, "_pid_alive", lambda pid: alive.get(pid, False))
        signals: list[tuple[int, int, str, Any]] = []

        def _signal(pgid: int, sig: int, instance: str, *, expected: Any = None) -> Any:
            signals.append((pgid, sig, instance, expected))
            if sig == SIGTERM:
                alive[5002] = False  # one member honours SIGTERM, one ignores it
                return {5001: "s1", 5002: "s2"}, {5001: "s1", 5002: "s2"}
            return {5001: "s1"}, {5001: "s1"}

        monkeypatch.setattr(bmod, "signal_orphaned_spawn_group", _signal)
        monkeypatch.setattr(bmod.platform_compat, "pgroup_exists", lambda _pgid: False)
        assert bmod._start_app_backend_body("forky", _manifest()) is None
        assert probes == [], "POSIX pays no extra identity probe on the failure path"
        assert [s[:2] for s in signals] == [(4242, SIGTERM), (4242, SIGKILL)]
        instance = signals[0][2]
        assert instance and signals[1][2] == instance, "both passes vouch by the SAME token"
        assert signals[1][3] == {5001: "s1", 5002: "s2"}, "SIGKILL only reaches SIGTERM takers"

    def test_the_final_census_runs_even_when_every_sigterm_taker_is_gone(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A server whose SIGTERM handler forks a replacement into the same group
        produces a member the opening census could not contain; only a final
        reading can see it, so that reading is never skipped."""
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _p, _port=None: False)
        monkeypatch.setattr(bmod, "popen_limited", lambda *_a, **_k: _FakeProc(returncode=0))
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)
        monkeypatch.setattr(bmod, "_pid_alive", lambda pid: pid == 5009)
        signals: list[int] = []

        def _signal(pgid: int, sig: int, instance: str, *, expected: Any = None) -> Any:
            signals.append(sig)
            if sig == SIGTERM:
                return {5001: "s1"}, {5001: "s1"}
            assert expected == {5001: "s1"}
            return {5009: "s9"}, {}  # the replacement, first seen now: observed, not signalled

        monkeypatch.setattr(bmod, "signal_orphaned_spawn_group", _signal)
        monkeypatch.setattr(
            bmod.platform_compat, "pgroup_exists", lambda _p: pytest.fail("a live member decides")
        )
        assert bmod._start_app_backend_body("forky", _manifest()) is None
        assert signals == [SIGTERM, SIGKILL]

    def test_a_host_that_cannot_vouch_signals_nothing(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _p, _port=None: False)
        monkeypatch.setattr(bmod, "popen_limited", lambda *_a, **_k: _FakeProc(returncode=0))
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: False)
        monkeypatch.setattr(
            bmod,
            "signal_orphaned_spawn_group",
            lambda *_a, **_k: pytest.fail("nothing may be signalled without a vouch"),
        )
        with caplog.at_level(logging.INFO):
            assert bmod._start_app_backend_body("forky", _manifest()) is None
        assert any("cannot vouch" in r.getMessage() for r in caplog.records)

    def test_a_vouch_that_raises_is_a_warning_not_a_crash(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _p, _port=None: False)
        monkeypatch.setattr(bmod, "popen_limited", lambda *_a, **_k: _FakeProc(returncode=0))
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("/proc scan failed mid-listing")

        monkeypatch.setattr(bmod, "signal_orphaned_spawn_group", _boom)
        with caplog.at_level(logging.WARNING):
            assert bmod._start_app_backend_body("forky", _manifest()) is None
        assert any("/proc scan failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Live stops: disable / uninstall / gateway shutdown / retired spawn
# ---------------------------------------------------------------------------


def _track(
    name: str, proc: Any, start_time: str | None, spawn_instance: str | None = "inst-1"
) -> AppProcess:
    ap = AppProcess(
        app_name=name,
        port=9100,
        pid=proc.pid,
        proc=proc,
        pid_start_time=start_time,
        spawn_instance=spawn_instance,
        healthy=True,
    )
    with bmod._lock:
        bmod._processes[name] = ap
        bmod._allocated_ports[name] = ap.port
    return ap


class TestStopOnWindows:
    def test_stop_drains_through_the_identity_recorded_at_spawn(
        self, windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _FakeProc(pid=6100)
        _track("svc", proc, "ST-9")
        drained = _pinned_recorder(monkeypatch)
        assert bmod.stop_app_backend("svc") is True
        assert drained == [(6100, "ST-9", SIGTERM)]
        assert "svc" not in bmod._processes

    def test_an_unpinnable_identity_falls_back_to_the_numeric_taskkill(
        self, windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stop is never weaker than it was: the exact-handle path declining
        (False) hands the live root to ``taskkill /T`` exactly as before."""
        proc = _FakeProc(pid=6100)
        _track("svc", proc, "ST-9")
        _pinned_recorder(monkeypatch, result=False)
        legacy: list[tuple[int, int]] = []
        monkeypatch.setattr(
            bmod.platform_compat, "kill_process_tree", lambda pid, sig: legacy.append((pid, sig))
        )
        assert bmod.stop_app_backend("svc") is True
        assert legacy == [(6100, SIGTERM)]

    def test_a_capacity_refusal_on_a_live_stop_falls_back_to_the_numeric_taskkill(
        self, windows: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        proc = _FakeProc(pid=6100)
        _track("svc", proc, "ST-9")

        def _refused(*_a: Any, **_k: Any) -> bool:
            raise bmod.platform_compat.WindowsCleanupCapacityError("root capacity exhausted")

        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree_pinned", _refused)
        legacy: list[tuple[int, int]] = []
        monkeypatch.setattr(
            bmod.platform_compat, "kill_process_tree", lambda pid, sig: legacy.append((pid, sig))
        )
        with caplog.at_level(logging.WARNING):
            assert bmod.stop_app_backend("svc") is True
        assert legacy == [(6100, SIGTERM)]
        assert any("capacity refused" in r.getMessage() for r in caplog.records)

    def test_a_record_without_an_identity_uses_the_numeric_taskkill(
        self, windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``pid_start_time`` is written after publication; a stop racing that
        write has nothing to pin and keeps today's behaviour."""
        proc = _FakeProc(pid=6100)
        _track("svc", proc, None)
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree_pinned",
            lambda *_a, **_k: pytest.fail("no identity, nothing to pin"),
        )
        legacy: list[tuple[int, int]] = []
        monkeypatch.setattr(
            bmod.platform_compat, "kill_process_tree", lambda pid, sig: legacy.append((pid, sig))
        )
        assert bmod.stop_app_backend("svc") is True
        assert legacy == [(6100, SIGTERM)]

    def test_the_sigkill_escalation_stays_numeric_after_a_fallback(
        self, windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A drain that returned True has confirmed the root's exit, so the wait
        cannot time out after it; the escalation is therefore only reachable
        behind a fallback and keeps the pre-existing numeric SIGKILL."""
        proc = _FakeProc(pid=6100)
        proc.wait_raises = True
        _track("svc", proc, "ST-9")
        pinned = _pinned_recorder(monkeypatch, result=False)
        legacy: list[tuple[int, int]] = []
        monkeypatch.setattr(
            bmod.platform_compat, "kill_process_tree", lambda pid, sig: legacy.append((pid, sig))
        )
        assert bmod.stop_app_backend("svc") is True
        assert pinned == [(6100, "ST-9", SIGTERM)]
        assert legacy == [(6100, SIGTERM), (6100, SIGKILL)]

    def test_adopted_records_are_never_tree_drained(
        self, windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Scope safety: an adopted backend has no root we spawned. Its stop path
        signals each recorded PID pinned to its own identity, and the tree drain
        is not offered a number to aim at."""
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree_pinned",
            lambda *_a, **_k: pytest.fail("an adopted record has no spawned root to drain"),
        )
        killed: list[tuple[int, int]] = []

        def _pinned_kill(pid: int, _st: str, sig: int) -> bool:
            killed.append((pid, sig))
            return True

        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", _pinned_kill)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: False)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st")
        monkeypatch.setattr(bmod, "_wait_for_pids", lambda _pids, timeout=2.0: None)
        with bmod._lock:
            bmod._processes["ext"] = AppProcess(
                app_name="ext",
                port=9100,
                pid=0,
                proc=None,
                adopted_pids=[111],
                adopted_start_times={111: "st"},
                healthy=True,
            )
        assert bmod.stop_app_backend("ext") is True
        assert killed == [(111, SIGTERM)]

    def test_a_retired_spawn_is_drained_by_its_own_identity(
        self, windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: f"ST-{pid}")
        drained = _pinned_recorder(monkeypatch)
        log_fh = MagicMock()
        bmod._terminate_retired_spawn("late", _fake_proc(pid=6200, returncode=0), log_fh)
        assert drained == [(6200, "ST-6200", SIGTERM)]
        log_fh.close.assert_called_once()


class TestStopOfAnExitedRoot:
    """The tracked root died AFTER startup while the server it forked kept serving,
    and a stop (disable, ceiling revocation, shutdown) arrives before the health
    supervisor replaces the record. The live-root group signal has nothing to
    resolve from, so the record's drop must drain the tree the root left behind."""

    def test_windows_drains_the_exited_root_by_its_recorded_identity(
        self, windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _FakeProc(pid=6100, returncode=0)
        _track("svc", proc, "ST-9")
        drained = _pinned_recorder(monkeypatch)
        assert bmod.stop_app_backend("svc") is True
        assert drained == [(6100, "ST-9", SIGTERM)]
        assert "svc" not in bmod._processes

    def test_posix_drains_the_exited_root_by_its_spawn_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(bmod.platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(bmod, "_REAP_SIGTERM_GRACE", 0.0)
        monkeypatch.setattr(bmod, "_REAP_POLL_INTERVAL", 0.0)
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)
        monkeypatch.setattr(bmod, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree",
            lambda *_a, **_k: pytest.fail("getpgid on a reaped leader raises; never called"),
        )
        signals: list[tuple[int, int, str]] = []

        def _signal(pgid: int, sig: int, instance: str, *, expected: Any = None) -> Any:
            signals.append((pgid, sig, instance))
            return {7001: "s"}, {7001: "s"}

        monkeypatch.setattr(bmod, "signal_orphaned_spawn_group", _signal)
        monkeypatch.setattr(bmod.platform_compat, "pgroup_exists", lambda _pgid: False)
        _track("svc", _FakeProc(pid=6100, returncode=0), "ST-9", spawn_instance="inst-42")
        assert bmod.stop_app_backend("svc") is True
        assert signals == [(6100, SIGTERM, "inst-42"), (6100, SIGKILL, "inst-42")]

    def test_posix_without_a_spawn_instance_signals_nothing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No token means nothing can vouch the group; the number is never signalled."""
        monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(bmod.platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)
        monkeypatch.setattr(
            bmod, "signal_orphaned_spawn_group", lambda *_a, **_k: pytest.fail("no vouch")
        )
        monkeypatch.setattr(
            bmod.platform_compat, "kill_process_tree", lambda *_a, **_k: pytest.fail("no root")
        )
        _track("svc", _FakeProc(pid=6100, returncode=0), "ST-9", spawn_instance=None)
        with caplog.at_level(logging.INFO):
            assert bmod.stop_app_backend("svc") is True
        assert any("no spawn instance" in r.getMessage() for r in caplog.records)

    def test_windows_needs_no_spawn_instance_to_drain_by_identity(
        self, windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _track("svc", _FakeProc(pid=6100, returncode=0), "ST-9", spawn_instance=None)
        drained = _pinned_recorder(monkeypatch)
        assert bmod.stop_app_backend("svc") is True
        assert drained == [(6100, "ST-9", SIGTERM)]

    def test_the_restart_drains_the_dead_root_before_spawning_its_replacement(
        self, windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The health supervisor drops the same record; its replacement must not
        come up beside a server the dead launcher left holding the app's files."""
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(
            bmod, "shutdown_event", SimpleNamespace(is_set=lambda: False, wait=lambda _d: False)
        )
        monkeypatch.setattr(
            bmod,
            "_activation_denied",
            lambda _n, _k: SimpleNamespace(denied="policy", transient=False),
        )
        ap = _track("svc", _FakeProc(pid=6100, returncode=1), "ST-9")
        drained = _pinned_recorder(monkeypatch)
        assert bmod._restart_exited_backend(ap, 1) is False
        assert drained == [(6100, "ST-9", SIGTERM)]
        assert "svc" not in bmod._processes

    def test_the_spawn_records_its_instance_on_the_tracked_record(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "_survived_spawn", lambda _p, _port=None: True)
        monkeypatch.setattr(bmod, "popen_limited", lambda *_a, **_k: _FakeProc(pid=777))
        recorded: list[str | None] = []
        monkeypatch.setattr(
            bmod,
            "_record_app_pid",
            lambda _name, _pid, _port, instance=None: recorded.append(instance),
        )
        ap = bmod._start_app_backend_body("okapp", _manifest())
        assert ap is not None
        assert ap.spawn_instance and recorded == [
            ap.spawn_instance
        ], "the record and the pidfile row must vouch by the SAME token"


class TestOrdinaryStopOfALiveRootKeepsTheRowOnlyForASurvivor:
    """An ordinary stop (no withdrawn ceiling) of a LIVE root signals the group,
    the root exits on the SIGTERM, and the wait returns. The pidfile recovery row
    is put back ONLY when a descendant actually outlived the root -- the sole
    evidence of which is the port still accepting connections. A clean stop (the
    port is dead) drops the row, so a backend that genuinely stopped leaves no row
    for a later start to re-adopt instead of launching fresh."""

    @pytest.fixture(autouse=True)
    def _posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(bmod.platform_compat, "IS_POSIX", True)
        # Forcing IS_POSIX makes the pidfile WRITE path (atomic_write ->
        # fchmod_safe) take its POSIX branch, which calls os.fchmod -- a name that
        # does not exist on a real Windows runner. The permission bit is irrelevant
        # to what this test asserts (the stop's drop/keep decision), so neutralise
        # fchmod_safe to its documented Windows no-op and keep the test OS-independent.
        monkeypatch.setattr(bmod.platform_compat, "fchmod_safe", lambda *_a, **_k: None)
        # The root exits on SIGTERM, so the signal and wait are both no-ops here;
        # only the port probe decides the row's fate.
        monkeypatch.setattr(bmod, "_signal_backend_tree", lambda *_a, **_k: None)

    def _seed_row(self) -> None:
        # Write the row DIRECTLY with an explicit start_time rather than through
        # _record_app_pid, whose start_time comes from a platform probe we would
        # otherwise have to stub per-OS. The seeded start_time must equal the tracked
        # record's, or the conditional forget declines and the row would persist for
        # an unrelated reason -- masking what this test checks.
        bmod._write_pidfile(
            {"svc": {"pid": 6100, "start_time": "ST-9", "port": 9100, "spawn_instance": "inst-1"}}
        )
        row = bmod._read_pidfile().get("svc")
        assert (
            row is not None and row.get("start_time") == "ST-9"
        ), "precondition: the row is seeded with the tracked start_time"

    def test_a_clean_stop_drops_the_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._seed_row()
        monkeypatch.setattr(bmod, "_port_is_listening", lambda _port: False)
        _track("svc", _FakeProc(pid=6100, returncode=None), "ST-9")
        assert bmod.stop_app_backend("svc") is True
        assert "svc" not in bmod._processes
        assert "svc" not in bmod._read_pidfile(), (
            "a clean ordinary stop (nothing left on the port) must DROP the recovery row, "
            "not re-persist it -- a stale row could be re-adopted by a later start"
        )

    def test_a_survivor_keeps_the_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._seed_row()
        monkeypatch.setattr(bmod, "_port_is_listening", lambda _port: True)
        _track("svc", _FakeProc(pid=6100, returncode=None), "ST-9")
        assert bmod.stop_app_backend("svc") is True
        assert "svc" not in bmod._processes
        assert "svc" in bmod._read_pidfile(), (
            "a descendant still answering the port is TOLERATED, and its recovery row is "
            "kept so a later re-enable can re-attribute the surviving listener"
        )


class TestOrdinaryStopOfAnExitedRootKeepsTheRowForAListeningSurvivor:
    """GPT F3: on an ordinary stop of an EXITED root, the recovery row must be kept
    whenever a descendant is still serving -- and the drain's original-group verdict
    is not the only evidence of that. A backend that daemonizes its listener into a
    NEW process group leaves ``_drain_exited_root_tree`` reporting True (the group it
    watched is gone) while the detached child keeps the port, so gating the restore
    only on ``gone is False`` would drop that survivor's one recovery handle. The
    port probe closes the gap, symmetric with the live-root tolerate path."""

    @pytest.fixture(autouse=True)
    def _posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(bmod.platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(bmod.platform_compat, "fchmod_safe", lambda *_a, **_k: None)
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)

    def _seed_row(self) -> None:
        bmod._write_pidfile(
            {"svc": {"pid": 6100, "start_time": "ST-9", "port": 9100, "spawn_instance": "inst-1"}}
        )

    def test_a_detached_listener_keeps_the_row_even_when_the_drain_says_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_row()
        # The drain finds the original group gone (gone is True), but a detached child
        # re-grouped elsewhere still answers the port.
        monkeypatch.setattr(bmod, "_drain_exited_root_tree", lambda *_a, **_k: True)
        monkeypatch.setattr(bmod, "_port_is_listening", lambda _port: True)
        _track("svc", _FakeProc(pid=6100, returncode=0), "ST-9")
        assert bmod.stop_app_backend("svc") is True
        assert "svc" not in bmod._processes
        assert "svc" in bmod._read_pidfile(), (
            "a detached listener still answering the port must KEEP the recovery row "
            "even when the original-group drain reports the tree gone"
        )

    def test_a_clean_exited_root_stop_still_drops_the_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_row()
        # Drain says gone AND the port is silent: a genuinely clean stop drops the row.
        monkeypatch.setattr(bmod, "_drain_exited_root_tree", lambda *_a, **_k: True)
        monkeypatch.setattr(bmod, "_port_is_listening", lambda _port: False)
        _track("svc", _FakeProc(pid=6100, returncode=0), "ST-9")
        assert bmod.stop_app_backend("svc") is True
        assert (
            "svc" not in bmod._read_pidfile()
        ), "a clean exited-root stop (drain gone, port silent) must DROP the row"


class TestStopOfAnExitedRootUnderAWithdrawnCeiling:
    """``_retry_if_serving`` is the ceiling-revocation caller's stricter reading. The
    live-root branch refuses and restores tracking when something still serves;
    the exited-root branch must reach the same verdict from its own evidence."""

    @pytest.fixture(autouse=True)
    def _posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(bmod.platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(bmod, "_REAP_SIGTERM_GRACE", 0.0)
        monkeypatch.setattr(bmod, "_REAP_POLL_INTERVAL", 0.0)
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)

    def _survivor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod, "_pid_alive", lambda pid: pid == 7002)
        monkeypatch.setattr(
            bmod,
            "signal_orphaned_spawn_group",
            lambda pgid, sig, instance, expected=None: ({7002: "s"}, {7002: "s"}),
        )

    def test_a_positive_survivor_refuses_the_stop_and_restores_tracking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._survivor(monkeypatch)
        monkeypatch.setattr(
            bmod, "_health_probe", lambda *_a: pytest.fail("positive evidence needs no probe")
        )
        ap = _track("svc", _FakeProc(pid=6100, returncode=0), "ST-9")
        assert bmod.stop_app_backend("svc", _retry_if_serving="/health") is False
        assert bmod._processes["svc"] is ap
        assert bmod._allocated_ports["svc"] == ap.port

    def test_an_ordinary_stop_tolerates_the_survivor_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._survivor(monkeypatch)
        _track("svc", _FakeProc(pid=6100, returncode=0), "ST-9")
        with caplog.at_level(logging.WARNING):
            assert bmod.stop_app_backend("svc") is True
        assert "svc" not in bmod._processes
        assert any("survived the drain" in r.getMessage() for r in caplog.records)

    def test_an_inconclusive_drain_is_settled_by_the_port_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A host that cannot read the vouch concludes nothing; the ceiling then
        asks the port, exactly as the live-root branch does."""
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: False)
        probes: list[tuple[int, str]] = []

        def _probe(port: int, path: str) -> Any:
            probes.append((port, path))
            return SimpleNamespace(healthy=True)

        monkeypatch.setattr(bmod, "_health_probe", _probe)
        ap = _track("svc", _FakeProc(pid=6100, returncode=0), "ST-9")
        assert bmod.stop_app_backend("svc", _retry_if_serving="/health") is False
        assert probes == [(ap.port, "/health")]
        assert bmod._processes["svc"] is ap

    def test_a_confirmed_empty_group_passes_the_ceiling_without_a_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(bmod, "signal_orphaned_spawn_group", lambda *_a, **_k: ({}, {}))
        monkeypatch.setattr(bmod.platform_compat, "pgroup_exists", lambda _pgid: False)
        monkeypatch.setattr(
            bmod, "_health_probe", lambda *_a: pytest.fail("a confirmed-gone tree needs no probe")
        )
        _track("svc", _FakeProc(pid=6100, returncode=0), "ST-9")
        assert bmod.stop_app_backend("svc", _retry_if_serving="/health") is True
        assert "svc" not in bmod._processes

    def test_an_empty_census_with_a_live_group_is_not_absence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every vouch read is fail-open, so an empty reading is settled by the
        probe that cannot fail open: a group that still exists keeps the ceiling
        asking the port."""
        monkeypatch.setattr(bmod, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(bmod, "signal_orphaned_spawn_group", lambda *_a, **_k: ({}, {}))
        monkeypatch.setattr(bmod.platform_compat, "pgroup_exists", lambda _pgid: True)
        monkeypatch.setattr(bmod, "_health_probe", lambda *_a: SimpleNamespace(healthy=True))
        _track("svc", _FakeProc(pid=6100, returncode=0), "ST-9")
        assert bmod.stop_app_backend("svc", _retry_if_serving="/health") is False


class TestStopOnPosix:
    def test_stop_and_retired_spawn_keep_the_group_signal_byte_identical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(bmod.platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree_pinned",
            lambda *_a, **_k: pytest.fail("POSIX never takes the Windows drain"),
        )
        kills: list[tuple[int, int]] = []
        monkeypatch.setattr(
            bmod.platform_compat, "kill_process_tree", lambda pid, sig: kills.append((pid, sig))
        )
        _track("svc", _FakeProc(pid=6100), "ST-9")
        assert bmod.stop_app_backend("svc") is True
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "ST-x")
        bmod._terminate_retired_spawn("late", _fake_proc(pid=6200, returncode=0), MagicMock())
        assert kills == [(6100, SIGTERM), (6200, SIGTERM)]
