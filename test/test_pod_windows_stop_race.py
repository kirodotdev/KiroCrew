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


@pytest.mark.parametrize(
    "evidence",
    [
        "dead_record",
        "malformed_record",
        "unreadable_record",
        "invalid_encoding",
        "missing_token",
        "reused_identity",
        "wrapper_only",
        "result_only",
        "orphaned_handoff",
        "home_only",
        "task_only",
        "settled_handoff",
    ],
)
def test_stop_pod_refuses_missing_root_with_prior_writer_evidence(cfg, monkeypatch, evidence):
    """No live root is not a proof that a prior gateway's MCP child exited."""
    from kiro_crew.pod import runtime as rt

    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(win, "_HANDOFF_SETTLE_TIMEOUT_SECS", 0)
    monkeypatch.setattr(win, "pid_exists", lambda pid: evidence == "reused_identity")
    monkeypatch.setattr(win, "process_start_time", lambda pid: "200")
    task_calls: list[str] = []
    cleanup_calls: list[str] = []
    monkeypatch.setattr(rt, "cleanup_home", lambda _cfg, name: cleanup_calls.append(name) or 0)
    monkeypatch.setattr(rt.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        win,
        "schtasks",
        lambda *args: task_calls.append(args[0])
        or _cp(1 if args[0] == "/Query" and evidence != "task_only" else 0),
    )
    monkeypatch.setattr(
        win,
        "open_process_termination_handle",
        lambda *_a: pytest.fail("a missing root grants no termination authority"),
    )
    record = win.pid_record_path(cfg, "demo")
    paths = {
        "wrapper_only": win.task_script_path(cfg, "demo"),
        "result_only": win.result_path(cfg, "demo"),
        "orphaned_handoff": win.handoff_marker_path(cfg, "demo"),
    }
    if evidence in {"dead_record", "reused_identity", "unreadable_record"}:
        record.write_text("4242\n100\n", encoding="utf-8")
    elif evidence == "malformed_record":
        record.write_text("not-a-pid\n", encoding="utf-8")
    elif evidence == "missing_token":
        record.write_text("4242\n", encoding="utf-8")
    elif evidence == "invalid_encoding":
        record.write_bytes(b"\xff")
    elif evidence in paths:
        paths[evidence].write_text("4242\n100\n", encoding="utf-8")
    elif evidence == "home_only":
        cfg.home_dir("demo").mkdir(parents=True)
    elif evidence == "settled_handoff":
        pending = {"value": True}
        monkeypatch.setattr(win, "handoff_in_progress", lambda *_a: pending["value"])

        def settle(*_a, **_kw):
            pending["value"] = False
            return True

        monkeypatch.setattr(win, "_await_handoff_outcome", settle)
    if evidence == "unreadable_record":
        real_read = win.Path.read_text

        def read(path, *args, **kwargs):
            if path == record:
                raise PermissionError("record unreadable")
            return real_read(path, *args, **kwargs)

        monkeypatch.setattr(win.Path, "read_text", read)
    before = {path: path.read_bytes() for path in cfg.pods_dir.iterdir() if path.is_file()}

    result = rt.stop_pod(cfg, "demo")

    assert result.returncode != 0, "an unproven old writer must block HOME reclamation"
    assert "/End" not in task_calls
    assert "/Delete" not in task_calls
    assert cleanup_calls == []
    assert "preserved" in result.stderr
    assert all(path.read_bytes() == content for path, content in before.items())
    if evidence == "home_only":
        assert cfg.home_dir("demo").is_dir()


def test_stop_accepts_a_never_started_empty_plane(cfg, monkeypatch):
    monkeypatch.setattr(win, "_HANDOFF_SETTLE_TIMEOUT_SECS", 0)
    calls: list[str] = []
    monkeypatch.setattr(win, "schtasks", lambda *args: calls.append(args[0]) or _cp(1))
    monkeypatch.setattr(
        win,
        "open_process_termination_handle",
        lambda *_a: pytest.fail("an empty plane has no process to terminate"),
    )

    result = win.stop(cfg, "demo", timeout=0)

    assert result.returncode == 0, result.stderr


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


@pytest.mark.parametrize(
    ("survivor", "expected"),
    [
        pytest.param(9001, (False, [4300]), id="child-survives"),
        pytest.param(8001, (True, []), id="root-survives"),
    ],
)
def test_exact_drain_names_a_known_live_survivor_instead_of_a_snapshot_failure(
    monkeypatch, survivor, expected
):
    """A process still active at the deadline is a survivor with a pid to report.

    The TimeoutError above is reserved for the OTHER way of not finishing: nothing
    is live but a terminal snapshot is still outstanding. Folding a live survivor
    into that raise would turn `pod down`'s refusal into "enumeration failed" and
    drop the pid diagnostics the operator needs to act on it.
    """
    root_handle = 8001
    child_handle = 9001
    active = {root_handle, child_handle}

    monkeypatch.setattr(win, "process_handle_active", lambda handle: handle in active)
    monkeypatch.setattr(win, "descendant_termination_handles", lambda *_a, **_k: {})

    def _terminate(handle):
        if handle != survivor:
            active.discard(handle)  # the survivor ignores termination
        return True

    monkeypatch.setattr(win, "terminate_process_handle", _terminate)

    result = win._drain_exact_windows_tree(4242, root_handle, {4300: child_handle}, timeout=0)

    assert result == expected


@pytest.mark.parametrize("recycled", [False, True])
def test_stop_takes_the_exact_handle_path_on_every_host(cfg, monkeypatch, recycled):
    """The exact-handle teardown is the ONLY stop path; there is no token fallback.

    Pinned with the module flag reading as a non-Windows host, which is what the
    Linux and macOS unit runners see: production only dispatches this backend on
    win32 (runtime.stop_pod), so a platform-gated fallback in here could only ever
    run under test -- and a test that passed through it proved nothing about the
    code Windows runs. Both token-walk primitives are armed to fail the test.
    """
    monkeypatch.setattr(win, "IS_WINDOWS", False)
    monkeypatch.setattr(win, "handoff_in_progress", lambda *_a: False)
    active = {8001}
    monkeypatch.setattr(win, "pid_exists", lambda pid: pid == 4242 and (8001 in active or recycled))
    monkeypatch.setattr(win, "supervised_pid", lambda *_a: 4242 if 8001 in active else None)
    monkeypatch.setattr(win, "process_start_time", lambda _pid: "100")
    monkeypatch.setattr(win, "_read_pid_record", lambda *_a: (4242, "100"))
    opened: list[tuple[int, str]] = []
    monkeypatch.setattr(
        win,
        "open_process_termination_handle",
        lambda pid, expected: opened.append((pid, expected)) or 8001,
    )
    monkeypatch.setattr(win, "descendant_termination_handles", lambda *_a, **_k: {})
    monkeypatch.setattr(win, "process_handle_active", lambda handle: handle in active)
    monkeypatch.setattr(win, "terminate_process_handle", lambda handle: active.discard(handle))
    monkeypatch.setattr(win, "close_process_handle", lambda _handle: None)
    monkeypatch.setattr(
        win,
        "attributed_descendants",
        lambda *_a, **_k: pytest.fail("stop must not walk descendants by numeric pid"),
    )
    monkeypatch.setattr(
        win,
        "kill_process_tree_pinned",
        lambda *_a, **_k: pytest.fail("stop must not terminate by numeric pid"),
    )

    def schtasks(*args):
        if args[0] == "/End":
            active.discard(8001)
        return _cp()

    monkeypatch.setattr(win, "schtasks", schtasks)
    win.write_task_script(cfg, "demo")

    result = win.stop(cfg, "demo", timeout=0.1)

    assert result.returncode == (1 if recycled else 0), result.stderr
    assert opened == [(4242, "100")]
    assert win.task_script_path(cfg, "demo").exists() is recycled
    if recycled:
        assert "ALIVE but does not carry" in result.stderr


def test_stop_closes_retained_handles_when_end_raises(cfg, monkeypatch):
    closed: list[int] = []
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


@pytest.mark.parametrize("phase", ["before_end", "after_end"])
@pytest.mark.parametrize(
    "failure", ["unopenable", "unreadable_identity", "snapshot_error", "vanished_parent"]
)
def test_stop_pod_preserves_home_and_task_on_incomplete_handle_proof(
    cfg, monkeypatch, phase, failure
):
    from kiro_crew.pod import runtime as rt

    closed: list[int] = []
    task_calls: list[str] = []
    active = {8001, 9001, 9002}
    ended = False
    missing_open = False
    partial_handles: list[int] = []
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    monkeypatch.setattr(win, "handoff_in_progress", lambda *_a: False)
    monkeypatch.setattr(win, "supervised_pid", lambda *_a: 4242)
    monkeypatch.setattr(win, "process_start_time", lambda _pid: "100")
    monkeypatch.setattr(win, "_read_pid_record", lambda *_a: (4242, "100"))
    monkeypatch.setattr(win, "descendant_termination_handles", pc.descendant_termination_handles)
    monkeypatch.setattr(win, "process_handle_active", lambda handle: handle in active)
    monkeypatch.setattr(win, "terminate_process_handle", lambda handle: active.discard(handle))
    monkeypatch.setattr(win, "close_process_handle", closed.append)
    monkeypatch.setattr(pc, "close_process_handle", closed.append)
    monkeypatch.setattr(pc, "pid_exists", lambda _pid: False)
    monkeypatch.setattr(pc, "_windows_process_query_diagnostic", lambda _pid: "identity=None")
    monkeypatch.setattr(
        rt, "cleanup_home", lambda *_a: pytest.fail("an incomplete proof must preserve HOME")
    )

    def snapshot():
        if missing_open and failure == "snapshot_error":
            raise OSError("fresh snapshot unavailable")
        result = {4300: 4242}
        if ended or phase == "before_end":
            result[4301] = 4242
        if missing_open and failure == "vanished_parent":
            result.pop(4301)
            result.update({4302: 4301, 4303: 4302})
        return result

    def open_handle(pid, **_kwargs):
        nonlocal missing_open
        if pid == 4301:
            if failure != "unreadable_identity":
                missing_open = True
                return None
            handle = 9002 + len(partial_handles)
            partial_handles.append(handle)
            return handle
        return {4242: 8001, 4300: 9001}[pid]

    def identity(handle):
        return {
            8001: (4242, 100, 200 if ended else None),
            9001: (4300, 110, None),
        }.get(handle)

    def schtasks(*args):
        nonlocal ended
        task_calls.append(args[0])
        assert args[0] == "/End", "failed proof must not delete the registered task"
        ended = True
        active.discard(8001)
        return _cp()

    monkeypatch.setattr(pc, "_windows_process_parent_map", snapshot)
    monkeypatch.setattr(pc, "_open_process_termination_handle", open_handle)
    monkeypatch.setattr(pc, "_windows_process_handle_identity", identity)
    monkeypatch.setattr(win, "schtasks", schtasks)
    home = cfg.home_dir("demo")
    home.mkdir(parents=True)
    state = home / "state.json"
    state.write_text("{}", encoding="utf-8")
    script = win.write_task_script(cfg, "demo")

    result = rt.stop_pod(cfg, "demo")

    assert result.returncode == 1
    assert "preserved" in result.stderr
    assert "NOT proven zero-residue" in result.stderr
    assert state.read_text(encoding="utf-8") == "{}"
    assert script.exists()
    assert task_calls == ([] if phase == "before_end" else ["/End"])
    # New partial handles are closed by discovery; retained ones only by stop.
    assert sorted(closed) == [8001, 9001, *partial_handles]


@pytest.mark.parametrize("recycled", [False, True])
def test_stop_terminally_scans_an_exited_root_before_reporting_zero_residue(
    cfg, monkeypatch, recycled
):
    """The child appears only after the pre-/End snapshot and root exit."""

    root_pid = 4242
    child_pid = 4300
    root_handle = 8001
    child_handle = 9001
    active_handles = {root_handle, child_handle}
    supervised = {"pid": root_pid}
    monkeypatch.setattr(
        win,
        "pid_exists",
        lambda pid: (pid == root_pid and (root_handle in active_handles or recycled))
        or (pid == child_pid and child_handle in active_handles),
    )
    root_scans: list[bool] = []
    terminated: list[int] = []
    closed: list[int] = []

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

    assert result.returncode == (1 if recycled else 0), result.stderr
    if recycled:
        assert "ALIVE but does not carry" in result.stderr
    assert root_scans[0] is True
    assert False in root_scans, "the exited root must receive a terminal snapshot"
    assert child_handle in terminated
    assert sorted(closed) == [root_handle, child_handle]


def test_stop_refuses_before_end_when_the_live_root_cannot_be_anchored(cfg, monkeypatch):
    """No exact root handle means no safe post-exit attribution proof."""

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


@pytest.mark.parametrize("outcome", ["live_successor", "dead_successor", "cleared_record"])
def test_stop_pod_refuses_a_handoff_not_covered_by_the_drained_root(cfg, monkeypatch, outcome):
    """A post-drain handoff settling does not prove its successor tree stopped."""
    from kiro_crew.pod import runtime as rt

    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(rt.time, "sleep", lambda _seconds: None)
    active = {8001}
    state = {"pid": 4242, "record": (4242, "100"), "handoff": False}
    calls: list[str] = []
    cleanup: list[str] = []
    monkeypatch.setattr(rt, "cleanup_home", lambda _cfg, name: cleanup.append(name) or 0)
    monkeypatch.setattr(win, "supervised_pid", lambda *_a: state["pid"])
    monkeypatch.setattr(win, "_read_pid_record", lambda *_a: state["record"])
    monkeypatch.setattr(win, "handoff_in_progress", lambda *_a: state["handoff"])
    monkeypatch.setattr(win, "process_start_time", lambda _pid: "100")
    monkeypatch.setattr(win, "pid_exists", lambda _pid: False)
    monkeypatch.setattr(win, "open_process_termination_handle", lambda *_a: 8001)
    monkeypatch.setattr(win, "descendant_termination_handles", lambda *_a, **_kw: {})
    monkeypatch.setattr(win, "process_handle_active", lambda handle: handle in active)
    monkeypatch.setattr(win, "terminate_process_handle", lambda handle: active.discard(handle))
    monkeypatch.setattr(win, "close_process_handle", lambda _handle: None)

    def schtasks(*args):
        calls.append(args[0])
        if args[0] == "/End":
            active.clear()
            state.update(pid=None, handoff=True)
        return _cp()

    def settle(*_a, **_kw):
        state.update(
            pid=4300 if outcome == "live_successor" else None,
            record=None if outcome == "cleared_record" else (4300, "200"),
            handoff=False,
        )
        return True

    monkeypatch.setattr(win, "schtasks", schtasks)
    monkeypatch.setattr(win, "_await_handoff_outcome", settle)
    wrapper = win.write_task_script(cfg, "demo")

    result = rt.stop_pod(cfg, "demo")

    assert result.returncode != 0
    assert "/Delete" not in calls
    assert cleanup == []
    assert wrapper.exists()


def test_stop_anchors_a_live_root_after_initial_handoff_settles(cfg, monkeypatch):
    state = {"pid": None, "handoff": True}
    active = {9001}
    opened: list[tuple[int, str]] = []
    calls: list[str] = []
    monkeypatch.setattr(win, "supervised_pid", lambda *_a: state["pid"])
    monkeypatch.setattr(win, "handoff_in_progress", lambda *_a: state["handoff"])
    monkeypatch.setattr(win, "_read_pid_record", lambda *_a: (4300, "200"))
    monkeypatch.setattr(win, "process_start_time", lambda _pid: "200")
    monkeypatch.setattr(win, "pid_exists", lambda _pid: bool(active))
    monkeypatch.setattr(
        win,
        "open_process_termination_handle",
        lambda pid, token: opened.append((pid, token)) or 9001,
    )
    monkeypatch.setattr(win, "descendant_termination_handles", lambda *_a, **_kw: {})
    monkeypatch.setattr(win, "process_handle_active", lambda handle: handle in active)
    monkeypatch.setattr(win, "close_process_handle", lambda _handle: None)

    def settle(*_a, **_kw):
        state.update(pid=4300, handoff=False)
        return True

    def schtasks(*args):
        calls.append(args[0])
        if args[0] == "/End":
            active.clear()
            state["pid"] = None
        return _cp()

    monkeypatch.setattr(win, "_await_handoff_outcome", settle)
    monkeypatch.setattr(win, "schtasks", schtasks)
    wrapper = win.write_task_script(cfg, "demo")

    result = win.stop(cfg, "demo", timeout=0)

    assert result.returncode == 0, result.stderr
    assert opened == [(4300, "200")]
    assert calls == ["/End", "/Delete"]
    assert not wrapper.exists()
