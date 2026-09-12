"""Regression coverage for the Windows pod stop late-child race."""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.pod import windows as win
from kiro_crew.pod.config import PodConfig


def _cp(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="")


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> PodConfig:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods-env"))
    monkeypatch.setenv("KIROCREW_POD_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    config = PodConfig.load()
    config.pods_dir.mkdir(parents=True, exist_ok=True)
    return config


def test_public_opener_closes_a_recycled_pid_handle(monkeypatch):
    closed: list[int] = []
    monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid: 8001)
    monkeypatch.setattr(
        pc,
        "_windows_process_handle_identity",
        lambda _handle: (4242, 200, None),
    )
    monkeypatch.setattr(pc, "close_process_handle", closed.append)

    assert pc.open_process_termination_handle(4242, "100") is None
    assert closed == [8001]


def test_exact_drain_refuses_when_postkill_terminal_scan_is_pending(monkeypatch):
    root_handle = 8001
    child_handle = 9001
    active = {root_handle, child_handle}
    scans: list[int] = []

    monkeypatch.setattr(win, "process_handle_active", lambda handle: handle in active)
    monkeypatch.setattr(
        win,
        "descendant_termination_handles",
        lambda pid, *_a, **_k: scans.append(pid) or {},
    )
    monkeypatch.setattr(
        win,
        "terminate_process_handle",
        lambda handle: active.discard(handle) is None,
    )

    with pytest.raises(TimeoutError, match="terminal snapshots"):
        win._drain_exact_windows_tree(
            4242,
            root_handle,
            {4300: child_handle},
            timeout=0,
        )

    assert scans == [4242, 4300]
    assert active == set()


def test_stop_closes_retained_handles_when_end_raises(cfg, monkeypatch):
    closed: list[int] = []
    monkeypatch.setattr(win, "IS_WINDOWS", True)
    monkeypatch.setattr(win, "handoff_in_progress", lambda *_a: False)
    monkeypatch.setattr(win, "supervised_pid", lambda *_a: 4242)
    monkeypatch.setattr(win, "process_start_time", lambda _pid: "100")
    monkeypatch.setattr(win, "_read_pid_record", lambda *_a: (4242, "100"))
    monkeypatch.setattr(
        win,
        "open_process_termination_handle",
        lambda _pid, _expected: 8001,
    )
    monkeypatch.setattr(
        win,
        "descendant_termination_handles",
        lambda *_a, **_k: {4300: 9001},
    )
    monkeypatch.setattr(win, "close_process_handle", closed.append)
    monkeypatch.setattr(
        win,
        "schtasks",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("end failed")),
    )

    with pytest.raises(RuntimeError, match="end failed"):
        win.stop(cfg, "demo", timeout=0.1)

    assert sorted(closed) == [8001, 9001]


def test_stop_closes_retained_handles_when_later_enumeration_fails(cfg, monkeypatch):
    closed: list[int] = []
    calls = {"count": 0}
    monkeypatch.setattr(win, "IS_WINDOWS", True)
    monkeypatch.setattr(win, "handoff_in_progress", lambda *_a: False)
    monkeypatch.setattr(win, "supervised_pid", lambda *_a: 4242)
    monkeypatch.setattr(win, "process_start_time", lambda _pid: "100")
    monkeypatch.setattr(win, "_read_pid_record", lambda *_a: (4242, "100"))
    monkeypatch.setattr(
        win,
        "open_process_termination_handle",
        lambda _pid, _expected: 8001,
    )

    def _descendants(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return {4300: 9001}
        raise OSError("snapshot failed")

    monkeypatch.setattr(win, "descendant_termination_handles", _descendants)
    monkeypatch.setattr(win, "process_handle_active", lambda _handle: False)
    monkeypatch.setattr(win, "terminate_process_handle", lambda _handle: False)
    monkeypatch.setattr(win, "close_process_handle", closed.append)
    monkeypatch.setattr(win, "schtasks", lambda *_a: _cp())

    result = win.stop(cfg, "demo", timeout=0.1)

    assert result.returncode == 1
    assert "snapshot failed" in result.stderr
    assert sorted(closed) == [8001, 9001]


def test_stop_terminally_scans_an_exited_root_before_reporting_zero_residue(cfg, monkeypatch):
    """The child appears only after the pre-/End snapshot and root exit."""

    root_pid = 4242
    child_pid = 4300
    root_handle = 8001
    child_handle = 9001
    active_handles = {root_handle, child_handle}
    supervised = {"pid": root_pid}
    root_scans: list[bool] = []
    terminated: list[int] = []
    closed: list[int] = []

    monkeypatch.setattr(win, "IS_WINDOWS", True)
    monkeypatch.setattr(win, "handoff_in_progress", lambda *_a: False)
    monkeypatch.setattr(win, "supervised_pid", lambda *_a: supervised["pid"])
    monkeypatch.setattr(win, "process_start_time", lambda pid: "100" if pid == root_pid else None)
    monkeypatch.setattr(win, "_read_pid_record", lambda *_a: (root_pid, "100"))
    monkeypatch.setattr(
        win,
        "open_process_termination_handle",
        lambda pid, expected: root_handle,
        raising=False,
    )
    monkeypatch.setattr(
        win, "process_handle_active", lambda handle: handle in active_handles, raising=False
    )
    monkeypatch.setattr(win, "close_process_handle", closed.append, raising=False)
    monkeypatch.setattr(win.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        win,
        "kill_process_tree_pinned",
        lambda *_a, **_k: pytest.fail("exact handles, not numeric PIDs, must terminate the tree"),
    )

    def descendants(pid, _retained=None, root_handle=None):
        if pid == root_pid:
            root_active = root_handle in active_handles
            root_scans.append(root_active)
            return {} if root_active else {child_pid: child_handle}
        assert pid == child_pid
        assert root_handle == child_handle
        return {}

    def terminate(handle):
        terminated.append(handle)
        active_handles.discard(handle)
        return True

    def schtasks(*args):
        if args[0] == "/End":
            # This is the exact missed ordering: the child is born after the
            # initial snapshot and the root exits before /End returns, so the
            # normal wait loop never observes a live root again.
            active_handles.discard(root_handle)
            supervised["pid"] = None
        return _cp()

    monkeypatch.setattr(win, "descendant_termination_handles", descendants, raising=False)
    monkeypatch.setattr(win, "terminate_process_handle", terminate, raising=False)
    monkeypatch.setattr(win, "schtasks", schtasks)

    result = win.stop(cfg, "demo", timeout=0.1)

    assert result.returncode == 0, result.stderr
    assert root_scans[0] is True
    assert False in root_scans, "the exited root must receive a terminal snapshot"
    assert child_handle in terminated
    assert sorted(closed) == [root_handle, child_handle]


def test_stop_refuses_before_end_when_the_live_root_cannot_be_anchored(cfg, monkeypatch):
    """No exact root handle means no safe post-exit attribution proof."""

    monkeypatch.setattr(win, "IS_WINDOWS", True)
    monkeypatch.setattr(win, "handoff_in_progress", lambda *_a: False)
    monkeypatch.setattr(win, "supervised_pid", lambda *_a: 4242)
    monkeypatch.setattr(win, "process_start_time", lambda _pid: "100")
    monkeypatch.setattr(win, "_read_pid_record", lambda *_a: (4242, "100"))
    monkeypatch.setattr(
        win,
        "open_process_termination_handle",
        lambda _pid, _expected: None,
        raising=False,
    )
    monkeypatch.setattr(
        win,
        "schtasks",
        lambda *_a: pytest.fail("an unanchored root must be rejected before /End"),
    )

    result = win.stop(cfg, "demo", timeout=0.1)

    assert result.returncode == 1
    assert "could not be anchored" in result.stderr
    assert "preserved" in result.stderr


def test_stop_refuses_known_windows_root_when_record_disappears(cfg, monkeypatch):
    monkeypatch.setattr(win, "IS_WINDOWS", True)
    monkeypatch.setattr(win, "handoff_in_progress", lambda *_a: False)
    monkeypatch.setattr(win, "supervised_pid", lambda *_a: 4242)
    monkeypatch.setattr(win, "process_start_time", lambda _pid: "100")
    monkeypatch.setattr(win, "_read_pid_record", lambda *_a: None)
    monkeypatch.setattr(
        win,
        "schtasks",
        lambda *_a: pytest.fail("a known unrecorded Windows root must fail before /End"),
    )

    result = win.stop(cfg, "demo", timeout=0.1)

    assert result.returncode == 1
    assert "does not match its authoritative creation identity" in result.stderr


@pytest.mark.skipif(not win.IS_WINDOWS, reason="requires real Windows process handles and Toolhelp")
def test_native_child_spawned_before_root_exit_is_reaped(cfg, monkeypatch, tmp_path):
    """Force spawn -> root exit -> /End return with no scheduler timing guess."""

    trigger = tmp_path / "spawn-now"
    child_pid_path = tmp_path / "child.pid"
    parent_code = """
import subprocess
import sys
import time
from pathlib import Path

trigger = Path(sys.argv[1])
child_pid_path = Path(sys.argv[2])
workdir = sys.argv[3]
while not trigger.exists():
    time.sleep(0.01)
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    cwd=workdir,
)
child_pid_path.write_text(str(child.pid), encoding="utf-8")
"""
    interpreter = getattr(sys, "_base_executable", sys.executable)
    parent = subprocess.Popen(
        [interpreter, "-c", parent_code, str(trigger), str(child_pid_path), str(tmp_path)],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pid: int | None = None
    child_token: str | None = None
    try:
        root_token = win.process_start_time(parent.pid)
        assert root_token, "the native test needs an exact root creation identity"
        win.record_supervised_pid(cfg, "demo", parent.pid)
        monkeypatch.setattr(win, "handoff_in_progress", lambda *_a: False)
        monkeypatch.setattr(
            win,
            "supervised_pid",
            lambda *_a: parent.pid if parent.poll() is None else None,
        )

        def schtasks(*args):
            nonlocal child_pid, child_token
            if args[0] == "/End":
                trigger.write_text("go", encoding="utf-8")
                parent.wait(timeout=5)
                deadline = time.monotonic() + 5
                while not child_pid_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert child_pid_path.exists(), "the controlled parent did not report its child"
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                child_token = win.process_start_time(child_pid)
                assert child_token
            return _cp()

        monkeypatch.setattr(win, "schtasks", schtasks)

        result = win.stop(cfg, "demo", timeout=2.0)

        assert result.returncode == 0, result.stderr
        assert child_pid is not None
        assert not win.pid_exists(child_pid), (
            "the child was created after the initial snapshot and its parent "
            "exited before /End returned, but stop left it alive"
        )
    finally:
        if parent.poll() is None:
            parent.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            parent.wait(timeout=5)
        if child_pid is None and child_pid_path.exists():
            with contextlib.suppress(ValueError, OSError):
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                child_token = win.process_start_time(child_pid)
        if child_pid is not None and child_token and win.pid_exists(child_pid):
            with contextlib.suppress(OSError):
                win.kill_process_tree_pinned(child_pid, child_token, win.SIGTERM)
            deadline = time.monotonic() + 5
            while win.pid_exists(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
