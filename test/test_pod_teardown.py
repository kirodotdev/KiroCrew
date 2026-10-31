"""Pod teardown: ``cleanup_home``, the cgroup drain, Linux ``stop_pod`` ordering, ``pod down``.

The tests of the per-name mutex that serializes ``up`` and ``down`` are here too. Tests that
drive the drain pin a ``ManualClock`` on ``runtime_lifecycle`` alone. The fixtures come from
``pod_helpers.py``.
"""

from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from _clock_isolation import assert_clock_is_private
from pod_helpers import (  # noqa: F401 -- the fixtures register by import
    _cp,
    _fake_node_toolchain,
    _isolate_pod_host_state,
    _systemd_backend,
    cfg_fixture,
    requires_posix_pod_lifecycle,
)

from kiro_crew import platform_compat
from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod import provision as prov
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import runtime_lifecycle
from kiro_crew.pod.config import PodConfig
from kiro_crew.testing.clock import ManualClock


class TestCleanupHome:
    def test_removes_pod_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        victim = c.home_dir("demo")
        victim.mkdir(parents=True)
        (victim / "marker").write_text("x")
        assert rt.cleanup_home(c, "demo") == 0
        assert not victim.exists()
        assert c.pod_root.exists()  # parent untouched

    def test_refuses_dotdot_escape(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pods = tmp_path / "pods"
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pods))
        c = PodConfig.load()
        pods.mkdir(parents=True)
        sentinel = tmp_path / "DO_NOT_DELETE"  # lives in pod_root's PARENT
        sentinel.write_text("precious")
        assert rt.cleanup_home(c, "..") != 0
        assert sentinel.exists() and pods.exists()


class TestCleanupHomeVerifies:
    """``rmtree`` runs with ``ignore_errors`` (a partially-removed tree is still
    progress), so ``cleanup_home``'s return value is the only signal a caller has
    that the HOME is actually gone. A constant 0 is what let ``pod down`` print
    "zero residue" over a directory still on disk."""

    def _held_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PodConfig, Path]:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / "security_events.jsonl").write_text("{}\n")
        # A pod-scoped process still holds the tree (or reopens its audit log in
        # append mode right behind the delete), so the removal achieves nothing
        # and says nothing — exactly what was measured on a live host.
        monkeypatch.setattr(rt.shutil, "rmtree", lambda *a, **k: None)
        return c, home

    def test_reports_a_home_that_survived_the_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c, home = self._held_home(tmp_path, monkeypatch)
        assert rt.cleanup_home(c, "demo") == 1
        out = capsys.readouterr().out
        assert str(home) in out
        # Naming the culprit is the point: a bare failure sends the operator
        # hunting for which writer resurrected the directory.
        assert "security_events.jsonl" in out

    def test_an_unlistable_survivor_still_fails_instead_of_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The survivor listing is diagnostics; it must never turn teardown into a
        traceback on a directory the process cannot read."""
        c, _ = self._held_home(tmp_path, monkeypatch)

        def _boom(self):  # noqa: ANN001 - patched onto Path
            raise PermissionError("nope")

        monkeypatch.setattr(Path, "iterdir", _boom)
        assert rt.cleanup_home(c, "demo") == 1

    def test_a_transiently_locked_home_is_reclaimed_on_a_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Still there" and "cannot be reclaimed" are different states on Windows.

        MEASURED on a native host: killing a process that held a file open and
        deleting immediately leaves the tree in place, because handles of an exited
        process are released ASYNCHRONOUSLY; a retry clears it on the second
        attempt. A control with no holder deletes on the first, and a holder that is
        still alive survives every attempt. So the retry absorbs the OS's release
        latency, and the survivor check keeps its teeth — proven by
        ``test_reports_a_home_that_survived_the_delete``, where every attempt fails
        and the result is still 1.
        """
        # Captured BEFORE _held_home, which patches this same module attribute to a
        # no-op: `rt.shutil` and `shutil` are one module object, so capturing after
        # would hand the no-op back and the "retry" would delete nothing.
        real = shutil.rmtree
        c, home = self._held_home(tmp_path, monkeypatch)
        attempts: list[int] = []

        def flaky(path, *a, **k):
            attempts.append(1)
            if len(attempts) == 1:
                return None  # the OS has not released the handle yet
            real(path, *a, **k)

        monkeypatch.setattr(rt.shutil, "rmtree", flaky)

        assert rt.cleanup_home(c, "demo") == 0, "a tree that CAN be reclaimed must be"
        assert len(attempts) == 2, f"exactly one retry was needed, saw {len(attempts)}"
        assert not home.exists()


class _DrainClock(ManualClock):
    """The drain's poll clock: ``on_sleep(seconds)`` runs after each poll sleep.

    That is the unit's process tree shrinking between two reads of
    ``cgroup.procs``. Installed on :mod:`kiro_crew.pod.runtime_lifecycle` alone,
    the module whose ``drain_cgroup`` polls. Its ``time`` IS the stdlib module, so
    patching ``rt.time.sleep`` ran the hook for every other thread's sleep in the
    worker too.
    """

    def __init__(self, on_sleep=lambda _s: None) -> None:
        super().__init__(monotonic_start=0.0)
        self._on_sleep = on_sleep

    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)
        self._on_sleep(seconds)


def _pin_clock(monkeypatch: pytest.MonkeyPatch, on_sleep=lambda _s: None) -> _DrainClock:
    clock = _DrainClock(on_sleep)
    clock.install(monkeypatch, runtime_lifecycle)
    return clock


def test_another_thread_keeps_the_real_sleep_and_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    assert_clock_is_private(_pin_clock(monkeypatch), runtime_lifecycle)


class TestDrainCgroup:
    """The delete waits for the unit's process tree, because that is the set that
    recreates the HOME behind it."""

    def test_a_vanished_cgroup_counts_as_drained(self, tmp_path: Path) -> None:
        assert rt.drain_cgroup(tmp_path / "gone" / "cgroup.procs") == []

    def test_waits_until_the_last_process_exits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        procs = tmp_path / "cgroup.procs"
        procs.write_text("7\n8\n")
        remaining = ["8\n", ""]
        clock = _pin_clock(monkeypatch, lambda _s: procs.write_text(remaining.pop(0)))
        assert rt.drain_cgroup(procs) == []
        assert remaining == [], "it must poll until empty, not sample once"
        assert len(clock.sleeps) == 2

    def test_reports_the_survivors_when_the_window_expires(self, tmp_path: Path) -> None:
        procs = tmp_path / "cgroup.procs"
        procs.write_text("7\n8\n")
        assert rt.drain_cgroup(procs, timeout=0.0) == ["7", "8"]


@requires_posix_pod_lifecycle
class TestLinuxTeardownOrdering:
    """Reclamation lives on the ``down`` path, not in an ``ExecStopPost`` hook that
    systemd runs BEFORE the final kill of the unit's cgroup. So ``stop_pod`` owns
    the ordering: stop, drain, delete, verify."""

    def test_the_cgroup_is_read_before_the_stop(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """systemd reports an empty ``ControlGroup`` for an inactive unit, so asking
        after the stop returns nothing and the drain silently degrades to a no-op."""
        order: list[str] = []

        def _read_cgroup(c: PodConfig, n: str) -> None:
            order.append("read-cgroup")
            return None

        def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            order.append(args[0])
            return _cp()

        monkeypatch.setattr(rt, "cgroup_procs_file", _read_cgroup)
        monkeypatch.setattr(rt, "systemctl", _systemctl)
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        # No teardown hook loaded, so no refresh interleaves with the ordering.
        monkeypatch.setattr(rt, "loaded_teardown_hook", lambda c, n: False)
        assert rt.stop_pod(cfg, "demo").returncode == 0
        assert order == ["read-cgroup", "stop"]

    def test_a_stale_unit_is_refreshed_before_the_stop(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A unit installed by an older build still carries the destructive
        ExecStopPost, and `systemctl stop` runs it BEFORE our drain — so the first
        `down` after an upgrade would delete the HOME under the pod's own live
        processes, losing its sessions and config. Refreshing must therefore happen
        before the stop, not just before a start."""
        order: list[str] = []

        def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            order.append(args[0])
            return _cp()

        monkeypatch.setattr(rt, "loaded_teardown_hook", lambda c, n: True)
        monkeypatch.setattr(rt.unit_mod, "install_unit", lambda c: order.append("re-render"))
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: None)
        monkeypatch.setattr(rt, "systemctl", _systemctl)
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        assert rt.stop_pod(cfg, "demo").returncode == 0
        assert order == ["re-render", "daemon-reload", "stop"]

    def test_a_current_unit_is_not_re_rendered_on_every_stop(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reverse guard: re-rendering unconditionally would daemon-reload on
        every single `pod down`."""
        rendered: list[str] = []
        monkeypatch.setattr(rt.unit_mod, "unit_is_current", lambda c: True)
        monkeypatch.setattr(rt.unit_mod, "install_unit", lambda c: rendered.append("re-render"))
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: None)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        assert rt.stop_pod(cfg, "demo").returncode == 0
        assert rendered == []

    def _stale_unit_with_failing_reload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> list[str]:
        """A stale unit on disk (it carries the removed directive) plus a
        daemon-reload that fails. Returns the list systemctl verbs are recorded in."""
        unit_file = tmp_path / "pod@.service"
        unit_file.write_text("[Service]\nExecStart=/x\nExecStopPost=/y pod _cleanup %i\n")
        monkeypatch.setattr(rt.unit_mod, "unit_path", lambda c: unit_file)
        monkeypatch.setattr(rt.unit_mod, "_kirocrew_argv", lambda: (sys.executable,))
        issued: list[str] = []

        def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            issued.append(args[0])
            if args[0] == "daemon-reload":
                return _cp(returncode=1, stderr="Failed to reload daemon")
            if args[0] == "show":  # systemd has the destructive hook loaded
                return _cp(stdout="{ path=/x ; argv[]=pod _cleanup x }\n")
            return _cp()

        monkeypatch.setattr(rt, "systemctl", _systemctl)
        return issued

    def test_the_stop_trusts_systemd_not_the_unit_file(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disk freshness is not proof of a load. A hookless file can sit in front
        of a cached definition that still deletes the HOME — reached by ANY writer
        whose reload failed, `pod install` included. So the stop asks systemd what
        it will execute, and refreshes on that."""
        order: list[str] = []

        def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            order.append(args[0])
            if args[0] == "show":
                # systemd still has the destructive hook loaded.
                return _cp(stdout="{ path=/usr/bin/kirocrew ; argv[]=pod _cleanup x }\n")
            return _cp()

        # The unit file on disk looks perfectly current — the old gate's signal.
        monkeypatch.setattr(rt.unit_mod, "unit_is_current", lambda c: True)
        monkeypatch.setattr(rt.unit_mod, "install_unit", lambda c: order.append("re-render"))
        monkeypatch.setattr(rt, "systemctl", _systemctl)
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: None)
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        assert rt.stop_pod(cfg, "demo").returncode == 0
        assert order == ["show", "re-render", "daemon-reload", "stop"]

    def test_an_unanswerable_hook_query_refreshes_anyway(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Cannot tell" must read as "the hook is there"; the other way round
        silently ships the defect on any host where the query fails."""
        order: list[str] = []

        def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            order.append(args[0])
            return (
                _cp(returncode=1, stderr="Failed to get properties") if args[0] == "show" else _cp()
            )

        monkeypatch.setattr(rt.unit_mod, "install_unit", lambda c: order.append("re-render"))
        monkeypatch.setattr(rt, "systemctl", _systemctl)
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: None)
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        assert rt.stop_pod(cfg, "demo").returncode == 0
        assert "re-render" in order

    def test_no_refresh_when_systemd_reports_no_hook(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reverse guard: refreshing unconditionally would daemon-reload on
        every single `pod down`."""
        order: list[str] = []

        def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            order.append(args[0])
            return _cp(stdout="\n") if args[0] == "show" else _cp()

        monkeypatch.setattr(rt.unit_mod, "install_unit", lambda c: order.append("re-render"))
        monkeypatch.setattr(rt, "systemctl", _systemctl)
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: None)
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        assert rt.stop_pod(cfg, "demo").returncode == 0
        assert order == ["show", "stop"]
        assert "daemon-reload" not in order

    def test_a_failed_reload_refuses_the_stop_and_removes_the_fresh_unit(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed reload splits disk from systemd: the fresh hookless file is on
        disk, but systemd still executes the OLD definition. Leaving that file
        behind is the real damage — `unit_is_current` reads disk, so every later
        call would report "current" while a crash restart still wipes the HOME.
        So remove it, and do not stop a pod whose loaded unit still has the hook."""
        issued = self._stale_unit_with_failing_reload(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: None)
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        cp = rt.stop_pod(cfg, "demo")
        assert cp.returncode != 0
        assert "pod install" in cp.stderr
        assert "stop" not in issued, "must not stop while systemd runs the old hook"
        assert not (tmp_path / "pod@.service").exists()
        # The invariant that matters: staleness must not read as current.
        assert rt.unit_mod.unit_is_current(cfg) is False

    def test_a_failed_reload_refuses_the_start_and_removes_the_fresh_unit(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issued = self._stale_unit_with_failing_reload(tmp_path, monkeypatch)
        cp = rt.start_pod(cfg, "demo")
        assert cp.returncode != 0
        assert "pod install" in cp.stderr
        assert "start" not in issued
        assert not (tmp_path / "pod@.service").exists()

    def test_the_delete_waits_for_the_process_tree(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        procs = tmp_path / "cgroup.procs"
        procs.write_text("4242\n")
        order: list[str] = []
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: procs)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())

        def _last_process_exits(_s: float) -> None:
            order.append("drained")
            procs.write_text("")

        def _delete(c: PodConfig, n: str) -> int:
            order.append("deleted")
            return 0

        _pin_clock(monkeypatch, _last_process_exits)
        monkeypatch.setattr(rt, "cleanup_home", _delete)
        assert rt.stop_pod(cfg, "demo").returncode == 0
        assert order == ["drained", "deleted"], "deleting first is the original race"

    def test_both_teardown_messages_name_the_same_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A failed teardown prints twice — once from `stop_pod`, once from
        `cleanup_home` — and the two must name the HOME identically. `pod_home`
        returns it unresolved while `cleanup_home` resolves before deleting, so on
        a host whose home is a symlink (the standard dev-desktop layout) the same
        directory appeared as `/home/...` and `/local/home/...` and read as two
        locations. Reproduced here with a symlinked pod root."""
        real = tmp_path / "real-pods"
        real.mkdir()
        link = tmp_path / "linked-pods"
        link.symlink_to(real)
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(link))
        c = PodConfig.load()
        (c.home_dir("demo")).mkdir(parents=True)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda cc, n: None)
        monkeypatch.setattr(rt.shutil, "rmtree", lambda *a, **k: None)
        cp = rt.stop_pod(c, "demo")
        assert cp.returncode != 0
        resolved = str((real / "demo").resolve())
        assert resolved in cp.stderr
        assert resolved in capsys.readouterr().out, "cleanup_home must agree"
        assert str(link / "demo") not in cp.stderr, "the second spelling is the bug"

    def test_a_failed_stop_leaves_a_possibly_live_pods_state_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / "kirocrew.db").write_text("live")
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=1, stderr="job failed"))
        cp = rt.stop_pod(c, "demo")
        assert cp.returncode != 0
        assert (home / "kirocrew.db").read_text() == "live"

    def test_a_process_outliving_the_drain_blocks_the_delete_entirely(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deleting after the drain EXPIRES would be the original defect wearing a
        verification: the live writer recreates the directory in append mode right
        behind the delete, which lands after the check, so `down` reports zero
        residue over a HOME that comes back. Refuse to delete instead."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / "kirocrew.db").write_text("a writer still has this")
        deleted: list[str] = []
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda cc, n: tmp_path / "cgroup.procs")
        monkeypatch.setattr(rt, "drain_cgroup", lambda procs, **k: ["4242", "4243"])
        monkeypatch.setattr(rt, "cleanup_home", lambda cc, n: deleted.append(n) or 0)
        cp = rt.stop_pod(c, "demo")
        assert cp.returncode != 0
        assert deleted == [], "must not delete a HOME a live process is still in"
        assert (home / "kirocrew.db").read_text() == "a writer still has this"
        assert str(home) in cp.stderr
        assert "NOT zero-residue" in cp.stderr
        # The pids that would not exit are the actionable part of the report.
        assert "4242" in cp.stderr and "4243" in cp.stderr

    def test_a_surviving_home_is_reported_not_called_zero_residue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drained cleanly, but the delete still did not take (permissions, or a
        writer outside the cgroup). The verification is what catches this."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda cc, n: tmp_path / "cgroup.procs")
        monkeypatch.setattr(rt, "drain_cgroup", lambda procs, **k: [])
        monkeypatch.setattr(rt.shutil, "rmtree", lambda *a, **k: None)
        cp = rt.stop_pod(c, "demo")
        assert cp.returncode != 0
        assert str(home) in cp.stderr
        assert "NOT zero-residue" in cp.stderr


class TestPodNameMutexCrossPlatform:
    """The name mutex serializes on every host.

    Since the ``fcntl``-only implementation (a no-op without ``fcntl``) was
    migrated to :func:`file_lock`, the primitive is real on Windows too, so
    these pins run unskipped everywhere.
    """

    def test_acquiring_the_mutex_does_not_truncate_the_lock_file(self, cfg: PodConfig) -> None:
        """The lock open must be non-truncating (GH-9248).

        A ``"w"`` open erases the file before the acquire; on Windows the
        subsequent ``msvcrt.locking`` acquire then races contenders watching
        an empty file. The content is meaningless to the lock itself, but its
        survival pins the non-truncating open on every platform.
        """
        lock_file = cfg.pods_dir / f"{cfg.unit_prefix}@demo.lock"
        cfg.pods_dir.mkdir(parents=True, exist_ok=True)
        lock_file.write_bytes(b"sentinel")
        with rt.pod_name_mutex(cfg, "demo"):
            pass
        assert lock_file.read_bytes() == b"sentinel"

    def test_nested_acquire_in_one_thread_does_not_deadlock(self, cfg: PodConfig) -> None:
        """Same-thread re-entry takes the in-thread counter, never the OS lock."""
        with rt.pod_name_mutex(cfg, "demo"):
            with rt.pod_name_mutex(cfg, "demo"):
                pass


@requires_posix_pod_lifecycle
class TestPodNameMutexOnLinux:
    """Linux teardown runs on the ``down`` path, so Linux has the same down/up race
    the launchd backend needs the flock for: the mutex must not be a no-op there."""

    def test_start_and_stop_hold_it(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        import contextlib as _ctx

        held: list[str] = []

        @_ctx.contextmanager
        def _fake(c: PodConfig, n: str):
            held.append(f"enter:{n}")
            yield
            held.append(f"exit:{n}")

        monkeypatch.setattr(rt, "pod_name_mutex", _fake)
        monkeypatch.setattr(rt.unit_mod, "unit_is_current", lambda c: True)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())
        rt.start_pod(cfg, "demo")
        assert held == ["enter:demo", "exit:demo"]

        held.clear()
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        rt.stop_pod(cfg, "demo")
        assert held == ["enter:demo", "exit:demo"], "the sweep must run INSIDE the mutex"

    def test_it_is_a_real_lock(self, cfg: PodConfig) -> None:
        with rt.pod_name_mutex(cfg, "demo"):
            pass
        assert (cfg.pods_dir / f"{cfg.unit_prefix}@demo.lock").exists()

    def test_boot_never_takes_the_lock(self) -> None:
        """`up` holds the mutex across the health wait, and the process it is
        waiting for is the pod's own `pod _run` -> boot(). If boot ever acquired
        the same per-name lock, `up` would wait for a gateway that is blocked on
        `up` — a deadlock no unit test would notice from either side alone.

        Checked TRANSITIVELY and in both call spellings: a bare
        `pod_name_mutex(...)`, an `rt.pod_name_mutex(...)` attribute call, or any
        module-level helper `boot` reaches that takes the lock would all deadlock
        identically, so pinning only the direct bare-name call would pin the letter
        of the rule rather than the property.

        Read across the whole pod runtime -- ``runtime.py`` and every
        ``runtime_*.py`` owner beside it -- because boot and the helpers it reaches
        live in several of those modules, and a walk confined to one file would stop
        at the first call that crosses into another.
        """
        funcs: dict[str, list[ast.AST]] = {}
        for source in sorted(Path(rt.__file__).parent.glob("runtime*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcs.setdefault(n.name, []).append(n)

        def called_names(fn: ast.AST) -> set[str]:
            out: set[str] = set()
            for n in ast.walk(fn):
                if not isinstance(n, ast.Call):
                    continue
                if isinstance(n.func, ast.Name):
                    out.add(n.func.id)
                elif isinstance(n.func, ast.Attribute):
                    out.add(n.func.attr)
            return out

        reached: set[str] = set()
        stack = ["boot"]
        while stack:
            name = stack.pop()
            if name in reached or name not in funcs:
                continue
            reached.add(name)
            for fn in funcs[name]:
                stack.extend(called_names(fn))

        assert "boot" in reached, "boot must exist for this guard to mean anything"
        assert "pod_name_mutex" not in reached
        # The lock is only ever taken by these two, so neither may be reachable
        # from boot either — that is the same deadlock one level removed.
        assert "start_pod" not in reached
        assert "stop_pod" not in reached


class TestDownSamplesStateUnderTheLock:
    """`was_up` / `had_home` decide whether a failed stop is fatal, so sampling
    them before taking the lock let a concurrent `up` invalidate the answer: we
    saw "not running, nothing to reclaim", waited on the lock, and then judged a
    REAL failure against that stale reading — swallowing it and deleting the live
    pod's checkout pin."""

    def test_state_is_read_after_the_lock_is_held(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import contextlib as _ctx

        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        rt.pin_checkout(c, "demo", tmp_path / "co")
        order: list[str] = []

        @_ctx.contextmanager
        def _mutex(cc: PodConfig, n: str):
            order.append("lock")
            # What a concurrent `up` completes while we are blocked on the lock.
            c.home_dir(n).mkdir(parents=True, exist_ok=True)
            yield

        def _is_active(cc: PodConfig, n: str) -> bool:
            order.append("sample")
            return False

        monkeypatch.setattr(rt, "pod_name_mutex", _mutex)
        monkeypatch.setattr(rt, "is_active", _is_active)
        monkeypatch.setattr(rt, "stop_pod", lambda cc, n: _cp(returncode=1, stderr="boom"))
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)

        # The failure must be fatal, judged against state read INSIDE the lock.
        with pytest.raises(SystemExit):
            pod_cli._down(c, argparse.Namespace(name="demo"))
        assert order == ["lock", "sample"], "state sampled before the lock is stale"
        assert c.env_file("demo").exists(), "a live pod's checkout pin must survive"


@requires_posix_pod_lifecycle
class TestDownReclaimsResidue:
    """``pod down`` is the reclaim command the orphan report points at, so it has
    to work on a pod that is not running."""

    def _orphan(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PodConfig, Path]:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / "config.json").write_text("{}")
        rt.pin_checkout(c, "demo", tmp_path / "co")
        monkeypatch.setattr(rt, "is_active", lambda cc, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        return c, home

    def test_down_reclaims_a_home_left_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c, home = self._orphan(tmp_path, monkeypatch)
        pod_cli._down(c, argparse.Namespace(name="demo"))
        assert not home.exists()
        assert not c.env_file("demo").exists()
        out = capsys.readouterr().out
        assert "reclaimed the isolated HOME" in out
        assert "nothing to stop" not in out

    def test_down_on_a_never_used_name_is_still_a_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """`systemctl stop` on an instance of a template that was never installed
        reports "unit not loaded" (rc 5). With no HOME to reclaim and the pod not
        running there is nothing at stake, so `pod down <name>` must stay the
        documented no-op rather than exiting 1."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        monkeypatch.setattr(rt, "is_active", lambda cc, n: False)
        monkeypatch.setattr(
            rt,
            "systemctl",
            lambda *a, **k: _cp(returncode=5, stderr="Unit kirocrew-pod@demo.service not loaded."),
        )
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        pod_cli._down(c, argparse.Namespace(name="demo"))  # must not SystemExit
        assert "nothing to stop" in capsys.readouterr().out

    def test_down_still_fails_loudly_when_a_home_is_there_to_reclaim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mirror of the no-op above: the same failed stop, but now there IS
        residue, so swallowing it would be the silent success being fixed here."""
        c, home = self._orphan(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=5, stderr="nope"))
        with pytest.raises(SystemExit):
            pod_cli._down(c, argparse.Namespace(name="demo"))
        assert home.exists()
        assert c.env_file("demo").exists()

    def test_down_fails_loudly_when_the_reclaim_could_not_finish(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A silent success here is the whole bug: the operator is told zero
        residue while the directory (and its checkout pin) are still there."""
        c, home = self._orphan(tmp_path, monkeypatch)
        monkeypatch.setattr(rt.shutil, "rmtree", lambda *a, **k: None)
        with pytest.raises(SystemExit):
            pod_cli._down(c, argparse.Namespace(name="demo"))
        assert home.exists()
        assert c.env_file("demo").exists(), "the pin must survive a failed teardown"


class TestStopNamesMissingUnit:
    """The classifier that separates "the unit is absent" from every stop
    failure that may have left the unit live — only the former may go on to a
    delete."""

    @pytest.mark.parametrize(
        "stderr,expected",
        [
            (
                "Failed to stop kirocrew-pod@x.service: " "Unit kirocrew-pod@x.service not loaded.",
                True,
            ),
            (
                "Failed to stop kirocrew-pod@x.service: " "Unit kirocrew-pod@x.service not found.",
                True,
            ),
            ("Job for kirocrew-pod@x.service failed.", False),
            ("Failed to connect to bus: No medium found", False),
            ("", False),
        ],
    )
    def test_only_an_absent_unit_is_classified(self, stderr: str, expected: bool) -> None:
        assert rt._stop_names_missing_unit(_cp(returncode=5, stderr=stderr)) is expected


@requires_posix_pod_lifecycle
class TestReclaimBlocker:
    """The pod-derived liveness judgment behind the unit-less reclaim. Each
    signal must be able to block on its own, a provably foreign responder must
    not block, and not knowing must block — an unreadable signal defaulting to
    "delete" is the one direction with no retry."""

    def _plane(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        c.home_dir("demo").mkdir(parents=True)
        # Signals staged to "provably dead"; each test flips exactly one.
        monkeypatch.setattr(rt, "derive_port", lambda cc, n: 7867)
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cc, n, p: None)
        monkeypatch.setattr(rt, "_probe_health", lambda port, timeout=3: 0)
        monkeypatch.setattr(rt, "_home_holders", lambda home, **k: [])
        return c

    def test_a_silent_dead_pod_is_reclaimable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._plane(tmp_path, monkeypatch)
        assert rt.reclaim_blocker(c, "demo") is None

    def test_a_proven_fresh_pid_record_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cc, n, p: 4242)
        blocker = rt.reclaim_blocker(c, "demo")
        assert blocker is not None and "pid record" in blocker

    def test_an_unattributed_responder_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Something answers on the derived port and cannot be attributed either
        way — it may be this pod's gateway, so the delete is refused."""
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "_probe_health", lambda port, timeout=3: 200)
        monkeypatch.setattr(rt, "port_owner", lambda cc, n, p: rt.OWNER_UNPROVEN)
        blocker = rt.reclaim_blocker(c, "demo")
        assert blocker is not None and "answers" in blocker

    def test_a_provably_foreign_responder_does_not_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """199 derived-port slots make collisions ordinary: a recycled port
        proves somebody ELSE is serving there, never that this pod is — treating
        it as liveness would make every collided orphan unreclaimable."""
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "_probe_health", lambda port, timeout=3: 200)
        monkeypatch.setattr(rt, "port_owner", lambda cc, n, p: rt.OWNER_FOREIGN)
        assert rt.reclaim_blocker(c, "demo") is None

    def test_a_process_holding_the_home_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "_home_holders", lambda home, **k: [123, 456])
        blocker = rt.reclaim_blocker(c, "demo")
        assert blocker is not None and "files under its HOME" in blocker and "123" in blocker

    @staticmethod
    def _judge_with_timeout(c: PodConfig, name: str) -> str | None:
        """Run the judgment on a worker so a blocking open fails the test
        instead of hanging the suite."""
        out: list[str | None] = []
        worker = threading.Thread(target=lambda: out.append(rt.reclaim_blocker(c, name)))
        worker.daemon = True
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), "the judgment blocked on the pin"
        return out[0]

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
    def test_a_fifo_at_the_pin_blocks_without_hanging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        os.mkfifo(c.env_file("demo"))
        blocker = self._judge_with_timeout(c, "demo")
        assert blocker is not None and "not a readable regular file" in blocker

    def test_a_link_at_the_pin_is_refused_not_followed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._plane(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside.env"
        outside.write_text("PORT='7999'\n")
        os.symlink(outside, c.env_file("demo"))
        blocker = self._judge_with_timeout(c, "demo")
        assert blocker is not None and "not a readable regular file" in blocker

    @pytest.mark.parametrize("name", ["bad\udcff", "../escape", ".hidden", "x" * 80])
    def test_a_name_that_is_not_a_pod_name_touches_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "_bootable_checkout", lambda cc, n: pytest.fail("reached a path"))
        assert rt.reclaim_blocker(c, name) == "its directory name is not a valid pod name"

    @pytest.mark.skipif(not platform_compat.IS_LINUX, reason="byte-named directories")
    def test_orphan_homes_reports_an_undecodable_directory_without_judging_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "active_names", lambda cc: set())
        os.mkdir(os.fsencode(c.pod_root) + b"/bad\xff")
        monkeypatch.setattr(
            rt,
            "reclaim_blocker",
            lambda cc, n: pytest.fail(f"judged {n!r}") if n != "demo" else None,
        )
        assert "bad\udcff" in rt.orphan_homes(c)

    def test_an_unreadable_signal_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._plane(tmp_path, monkeypatch)

        def _boom(port: int, timeout: int = 3) -> int:
            raise OSError("cannot probe")

        monkeypatch.setattr(rt, "_probe_health", _boom)
        blocker = rt.reclaim_blocker(c, "demo")
        assert blocker is not None and "could not be judged" in blocker


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
class TestHomeHolders:
    def test_an_open_descriptor_under_the_home_is_reported(self, tmp_path: Path) -> None:
        home = (tmp_path / "pods" / "demo").resolve()
        home.mkdir(parents=True)
        with open(home / "audit.log", "w"):
            assert os.getpid() in rt._home_holders(home)
        assert os.getpid() not in rt._home_holders(home)

    def _staged_proc(
        self,
        tmp_path: Path,
        home: Path,
        *,
        uid: int,
        names_home: bool,
        fd_mode: int,
        ppid: int | None = None,
        parent_uid: int | None = None,
        state: str = "S (sleeping)",
    ) -> Path:
        """One fake ``/proc`` entry (pid 123) in the non-dumpable shape: status
        and cmdline readable, links present, fd dir at *fd_mode* — a real
        credential transition is what produces this, which a test cannot do.
        *ppid*/*parent_uid* stage its parent as pid ``ppid`` owned by that uid."""
        proc = tmp_path / "proc"
        entry = proc / "123"
        (entry / "fd").mkdir(parents=True)
        status = f"Name:\tagent\nState:\t{state}\n"
        if ppid is not None:
            status += f"PPid:\t{ppid}\n"
        status += f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        (entry / "status").write_text(status)
        args = f"agent\0--homedir\0{home}\0" if names_home else "agent\0--daemon\0"
        (entry / "cmdline").write_bytes(args.encode())
        for special in ("cwd", "root", "exe"):
            os.symlink("/", entry / special)
        (entry / "fd").chmod(fd_mode)
        if ppid is not None and parent_uid is not None:
            parent = proc / str(ppid)
            parent.mkdir()
            pu = parent_uid
            (parent / "status").write_text(f"Name:\tsshd\nPPid:\t1\nUid:\t{pu}\t{pu}\t{pu}\t{pu}\n")
        return proc

    def test_a_nondumpable_holder_named_in_cmdline_blocks(self, tmp_path: Path) -> None:
        """A same-user process whose links are unreadable still exposes its
        command line, and one pointed into the HOME (a --homedir, a socket
        path) is a holder the delete must not run behind."""
        home = (tmp_path / "pods" / "demo").resolve()
        home.mkdir(parents=True)
        proc = self._staged_proc(tmp_path, home, uid=os.getuid(), names_home=True, fd_mode=0o000)
        try:
            assert rt._home_holders(home, proc_root=str(proc)) == [123]
        finally:
            (proc / "123" / "fd").chmod(0o700)

    def test_an_sd_pam_shaped_unreadable_process_does_not_block(self, tmp_path: Path) -> None:
        """systemd's own `(sd-pam)`, a daemonized ssh-agent, and every ssh
        session's sshd are same-user processes whose links cannot be read.
        Nothing they hold can be decided from an unprivileged /proc, and
        refusing on the shape refuses every reclaim on an ordinary host, so only
        positive evidence blocks: this pins that posture."""
        home = (tmp_path / "pods" / "demo").resolve()
        home.mkdir(parents=True)
        proc = self._staged_proc(
            tmp_path,
            home,
            uid=os.getuid(),
            names_home=False,
            fd_mode=0o000,
            ppid=77,
            parent_uid=os.getuid(),
        )
        (proc / "123" / "cmdline").write_bytes(b"(sd-pam)\0")
        for special in ("cwd", "root", "exe"):
            (proc / "123" / special).unlink()
        try:
            assert rt._home_holders(home, proc_root=str(proc)) == []
        finally:
            (proc / "123" / "fd").chmod(0o700)

    def test_a_live_thread_of_an_exited_leader_holding_the_home_blocks(
        self, tmp_path: Path
    ) -> None:
        """A leader that exits through pthread_exit leaves a zombie entry whose
        own links are gone while a worker thread still holds its files."""
        home = (tmp_path / "pods" / "demo").resolve()
        (home / "workspace").mkdir(parents=True)
        proc = tmp_path / "proc"
        entry = proc / "123"
        uid = os.getuid()
        (entry / "task" / "124" / "fd").mkdir(parents=True)
        (entry / "task" / "123").mkdir()
        (entry / "status").write_text(
            f"Name:\tagent\nState:\tZ (zombie)\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        )
        (entry / "cmdline").write_bytes(b"")
        os.symlink(home / "workspace", entry / "task" / "124" / "cwd")
        assert rt._home_holders(home, proc_root=str(proc)) == [123]

    def test_an_exited_leaders_descriptor_is_read_from_a_live_thread(self, tmp_path: Path) -> None:
        """Threads share one fd table, so it is read from the leader once -- or,
        when the leader has exited, from a live thread."""
        home = (tmp_path / "pods" / "demo").resolve()
        home.mkdir(parents=True)
        (home / "state.db").write_text("")
        proc = tmp_path / "proc"
        entry = proc / "123"
        uid = platform_compat.local_user_id()
        (entry / "task" / "124" / "fd").mkdir(parents=True)
        (entry / "status").write_text(
            f"Name:\tagent\nState:\tZ (zombie)\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        )
        (entry / "cmdline").write_bytes(b"")
        os.symlink("/", entry / "task" / "124" / "cwd")
        os.symlink(home / "state.db", entry / "task" / "124" / "fd" / "3")
        assert rt._home_holders(home, proc_root=str(proc)) == [123]

    @pytest.mark.skipif(platform_compat.local_user_id() == 0, reason="needs a non-root scan")
    def test_a_root_process_seen_holding_the_home_blocks(self, tmp_path: Path) -> None:
        """A pod's sudo descendant is root; when its links ARE readable (or its
        command line names the HOME) that positive evidence blocks."""
        home = (tmp_path / "pods" / "demo").resolve()
        home.mkdir(parents=True)
        proc = self._staged_proc(tmp_path, home, uid=0, names_home=True, fd_mode=0o000)
        try:
            assert rt._home_holders(home, proc_root=str(proc)) == [123]
        finally:
            (proc / "123" / "fd").chmod(0o700)

    def test_an_argument_naming_a_sibling_pod_with_the_same_prefix_does_not_block(
        self, tmp_path: Path
    ) -> None:
        """A process tailing ``pr-12``'s log names a path that starts with
        ``pr-1``'s HOME as a string; it must not hold ``pr-1``."""
        pods = (tmp_path / "pods").resolve()
        (pods / "pr-1").mkdir(parents=True)
        proc = tmp_path / "proc"
        entry = proc / "123"
        (entry / "fd").mkdir(parents=True)
        uid = platform_compat.local_user_id()
        (entry / "status").write_text(f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        for special in ("cwd", "root", "exe"):
            os.symlink("/", entry / special)
        (entry / "cmdline").write_bytes(f"tail\0-f\0{pods}/pr-12/logs/a.log\0".encode())
        assert rt._home_holders(pods / "pr-1", proc_root=str(proc)) == []
        (entry / "cmdline").write_bytes(f"tail\0-f\0{pods}/pr-1/logs/a.log\0".encode())
        assert rt._home_holders(pods / "pr-1", proc_root=str(proc)) == [123]

    def test_one_snapshot_serves_every_judgment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`pod ls` and `pod prune` judge many orphans from one /proc pass."""
        passes: list[str] = []
        real = rt._proc_snapshot

        def _counting(proc_root: str = "/proc"):
            passes.append(proc_root)
            return real(proc_root)

        monkeypatch.setattr(rt, "_proc_snapshot", _counting)
        snap = rt._proc_snapshot()
        for n in ("a", "b", "c"):
            rt._home_holders(tmp_path / n, snapshot=snap)
            rt._booting_pids(n, snapshot=snap)
        assert len(passes) == 1

    def test_a_foreign_users_process_is_skipped(self, tmp_path: Path) -> None:
        """A pod HOME is 0700, so another non-root user cannot hold a file
        under it — ownership is read from status, which stays truthful for a
        non-dumpable process whose /proc entries are re-owned to root."""
        home = (tmp_path / "pods" / "demo").resolve()
        home.mkdir(parents=True)
        proc = self._staged_proc(
            tmp_path, home, uid=os.getuid() + 1, names_home=True, fd_mode=0o000
        )
        try:
            assert rt._home_holders(home, proc_root=str(proc)) == []
        finally:
            (proc / "123" / "fd").chmod(0o700)

    def test_a_vanished_process_is_skipped(self, tmp_path: Path) -> None:
        home = (tmp_path / "pods" / "demo").resolve()
        home.mkdir(parents=True)
        proc = tmp_path / "proc"
        entry = proc / "123"
        entry.mkdir(parents=True)
        uid = os.getuid()
        (entry / "status").write_text(f"Name:\tagent\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (entry / "cmdline").write_bytes(b"agent\0")
        assert rt._home_holders(home, proc_root=str(proc)) == []


@requires_posix_pod_lifecycle
class TestStopPodToleratesAMissingUnit:
    """An orphaned HOME has no loaded unit, so its stop step fails with `Unit …
    not loaded` — the one stop failure that cannot mean a live unit. The reclaim
    proceeds for a pod proven dead by its own evidence, refuses for anything
    less, and every other stop failure keeps aborting before the delete."""

    _NOT_LOADED = (
        "Failed to stop kirocrew-pod@demo.service: " "Unit kirocrew-pod@demo.service not loaded."
    )

    def _systemctl_with_absent_unit(self):
        def _fake(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            if args[0] == "stop":
                return _cp(returncode=5, stderr=self._NOT_LOADED)
            return _cp()

        return _fake

    def test_a_missing_unit_no_longer_blocks_the_reclaim(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rt, "systemctl", self._systemctl_with_absent_unit())
        monkeypatch.setattr(rt, "reclaim_blocker", lambda c, n: None)
        deleted: list[str] = []
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: deleted.append(n) or 0)
        cp = rt.stop_pod(cfg, "demo")
        assert cp.returncode == 0
        assert deleted == ["demo"]

    def test_a_pod_the_blocker_calls_live_is_refused(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rt, "systemctl", self._systemctl_with_absent_unit())
        monkeypatch.setattr(rt, "reclaim_blocker", lambda c, n: "its gateway is still serving")
        monkeypatch.setattr(
            rt, "cleanup_home", lambda c, n: pytest.fail("deleted a possibly live pod's HOME")
        )
        cp = rt.stop_pod(cfg, "demo")
        assert cp.returncode != 0
        assert "its gateway is still serving" in cp.stderr
        assert "refusing to delete" in cp.stderr
        assert "kirocrew pod down demo" in cp.stderr

    def test_any_other_stop_failure_still_aborts_before_the_delete(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stop job that genuinely failed may have left the unit live — judging
        pod liveness there would substitute a weaker signal for a stronger one."""

        def _fake(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            if args[0] == "stop":
                return _cp(returncode=1, stderr="Job for kirocrew-pod@demo.service failed.")
            return _cp()

        monkeypatch.setattr(rt, "systemctl", _fake)
        monkeypatch.setattr(
            rt, "reclaim_blocker", lambda c, n: pytest.fail("judged a possibly live unit")
        )
        monkeypatch.setattr(
            rt, "cleanup_home", lambda c, n: pytest.fail("deleted behind a failed stop")
        )
        cp = rt.stop_pod(cfg, "demo")
        assert cp.returncode == 1
        assert "Job for" in cp.stderr


@requires_posix_pod_lifecycle
class TestUnitLessReclaimPosture:
    """The unit-less reclaim runs only when no boot can reach the HOME and no
    positive sign of a live pod is seen. Every boot reads the pin before the
    HOME, and the pin cannot change while the reclaim holds the name mutex, so
    a boot that starts during the reclaim refuses on the same pin."""

    _NOT_LOADED = (
        "Failed to stop kirocrew-pod@demo.service: " "Unit kirocrew-pod@demo.service not loaded."
    )

    @staticmethod
    def _bootable_checkout(tmp_path: Path) -> Path:
        """A checkout whose venv binary exists and is executable."""
        co = tmp_path / "co"
        bin_path = prov.venv_bin(co)
        bin_path.parent.mkdir(parents=True)
        bin_path.write_text("#!/bin/sh\n")
        bin_path.chmod(0o755)
        return co

    def _absent_unit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            if args[0] == "stop":
                return _cp(returncode=5, stderr=self._NOT_LOADED)
            return _cp()

        monkeypatch.setattr(rt, "systemctl", _fake)

    @staticmethod
    def _signals_dead(monkeypatch: pytest.MonkeyPatch) -> None:
        """Stage every positive liveness signal to "nothing seen", so the real
        reclaim_blocker runs and only bootability decides."""
        monkeypatch.setattr(rt, "_booting_pids", lambda n, **k: [])
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cc, n, p: None)
        monkeypatch.setattr(rt, "_probe_health", lambda port, timeout=3: 0)
        monkeypatch.setattr(rt, "_home_holders", lambda home, **k: [])

    def test_a_runnable_pinned_venv_refuses_the_reclaim_with_its_reason(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pin whose checkout can still boot -- from any checkout version --
        could rebuild or reuse the HOME behind the delete,
        so the reclaim refuses and says what to do."""
        rt.pin_checkout(cfg, "demo", self._bootable_checkout(tmp_path))
        self._absent_unit(monkeypatch)
        self._signals_dead(monkeypatch)
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: pytest.fail("deleted a bootable pod"))
        cp = rt.stop_pod(cfg, "demo")
        assert cp.returncode != 0
        assert "still has a runnable venv" in cp.stderr
        assert str(cfg.env_file("demo")) in cp.stderr, "the refusal names what to remove"
        assert cfg.env_file("demo").exists(), "a refused reclaim must leave the pin alone"
        assert rt.reclaim_refusal(cfg, "demo") == rt.reclaim_blocker(cfg, "demo")

    @pytest.mark.parametrize("pin", ["absent", "no-checkout", "checkout-without-venv"])
    def test_a_pin_that_cannot_boot_lets_the_reclaim_proceed(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pin: str
    ) -> None:
        """The reporter's case: the worktree is gone, so no boot can get past its
        venv check, and the orphan is reclaimed."""
        if pin == "no-checkout":
            rt.write_env_file(cfg, "demo", {"PORT": "7867"})
        elif pin == "checkout-without-venv":
            rt.pin_checkout(cfg, "demo", tmp_path / "gone")
        self._absent_unit(monkeypatch)
        self._signals_dead(monkeypatch)
        deleted: list[str] = []
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: deleted.append(n) or 0)
        assert rt.stop_pod(cfg, "demo").returncode == 0
        assert deleted == ["demo"]

    @pytest.mark.parametrize(
        "signal, reason",
        [
            ("_booting_pids", "`pod _run demo` is running"),
            ("_pod_recorded_pid", "pid record still names a live process"),
            ("_probe_health", "something answers on its port"),
            ("_home_holders", "hold files under its HOME"),
        ],
    )
    def test_each_positive_signal_refuses(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, signal: str, reason: str
    ) -> None:
        self._absent_unit(monkeypatch)
        self._signals_dead(monkeypatch)
        live = {
            "_booting_pids": lambda n, **k: [4242],
            "_pod_recorded_pid": lambda cc, n, p: 4242,
            "_probe_health": lambda port, timeout=3: 200,
            "_home_holders": lambda home, **k: [4242],
        }[signal]
        monkeypatch.setattr(rt, signal, live)
        monkeypatch.setattr(rt, "port_owner", lambda cc, n, p: rt.OWNER_UNPROVEN)
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: pytest.fail("deleted a live pod"))
        cp = rt.stop_pod(cfg, "demo")
        assert cp.returncode != 0 and reason in cp.stderr

    def test_a_running_pod_run_of_the_name_blocks(self, tmp_path: Path) -> None:
        """A boot of any checkout version runs as `pod _run <name>` until it
        execs the gateway, which a pre-gate boot announces in no other way."""
        proc = tmp_path / "proc"
        uid = os.getuid()
        for pid, argv in (
            ("200", b"python\0-m\0kiro_crew\0pod\0_run\0demo\0"),
            ("201", b"kirocrew\0pod\0_run\0demo2\0"),
            ("202", b"vim\0_run\0demo\0"),
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "status").write_text(f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
            (proc / pid / "cmdline").write_bytes(argv)
        assert rt._booting_pids("demo", proc_root=str(proc)) == [200]


@requires_posix_pod_lifecycle
class TestDownAndPruneReclaimAnOrphan:
    """`pod ls` prints `reclaim: kirocrew pod down <name>` beside every orphan
    and `pod prune` is the bulk form of the same delete; both must actually
    reclaim a HOME whose unit is gone, or the CLI advertises a command that
    always fails."""

    _NOT_LOADED = (
        "Failed to stop kirocrew-pod@demo.service: " "Unit kirocrew-pod@demo.service not loaded."
    )

    def _plane(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / "config.json").write_text("{}")
        rt.pin_checkout(c, "demo", tmp_path / "co")

        def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            if args[0] == "stop":
                return _cp(returncode=5, stderr=self._NOT_LOADED)
            return _cp()

        monkeypatch.setattr(rt, "systemctl", _systemctl)
        monkeypatch.setattr(rt, "is_active", lambda cc, n: False)
        # The pod is provably dead: no fresh pid record, a silent port, no
        # process holding the HOME. Staged at the signal level so the blocker
        # itself runs for real through the whole verb.
        monkeypatch.setattr(rt, "derive_port", lambda cc, n: 7867)
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cc, n, p: None)
        monkeypatch.setattr(rt, "_probe_health", lambda port, timeout=3: 0)
        monkeypatch.setattr(rt, "_home_holders", lambda home, **k: [])
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        return c

    def test_down_reclaims_an_orphan_whose_unit_is_gone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch)
        pod_cli._down(c, argparse.Namespace(name="demo"))
        assert not c.home_dir("demo").exists()
        assert not c.env_file("demo").exists()
        assert "reclaimed the isolated HOME" in capsys.readouterr().out

    def test_prune_reclaims_an_orphan_whose_unit_is_gone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "require_backend", lambda: None)
        monkeypatch.setattr(rt, "active_names", lambda cc: set())
        monkeypatch.setattr(rt, "unit_state", lambda cc, n: ("inactive", 0))
        pod_cli._prune(
            c,
            argparse.Namespace(older_than="3d", json=False, dry_run=False, prune_all=True),
        )
        assert not c.home_dir("demo").exists()
        assert "pruned: 1 reclaimed" in capsys.readouterr().out

    def test_down_refuses_when_the_pod_may_be_alive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The missing unit is the only thing standing between a live unit-less
        pod and its advertised delete — the pod-derived judgment must be what
        stands there instead."""
        c = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cc, n, p: 4242)
        with pytest.raises(SystemExit):
            pod_cli._down(c, argparse.Namespace(name="demo"))
        assert c.home_dir("demo").exists()
        assert c.env_file("demo").exists(), "a live pod's checkout pin must survive"
