"""Two fail-OPEN holes the Windows pod backend closes.

Both are the same shape: a Windows-only path answered "nothing is running here"
when it could not tell, and teardown then deleted state a live gateway owned.
They are asserted on every platform because both fixes are platform-neutral
Python over one primitive, and the primitive's own per-platform behaviour is
already covered by ``test_platform_compat``.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import types

import pytest

from kiro_crew import platform_compat
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import windows as win
from kiro_crew.pod.config import EXIT_REFUSED_UNRECOVERABLE, PodConfig


def _no_successor(tmp_path):
    """A gateway pid sidecar path with nothing at it.

    ``supervise_gateway`` reads that sidecar after the gateway it spawned exits,
    to see whether an in-app restart (``os.execv``, which on Windows spawns a
    successor and exits the caller) left a replacement serving. An absent file is
    the "no successor" answer, which is what these tests mean: they assert the
    ordinary spawn-and-exit contract, not the restart handover.
    """
    return tmp_path / "no-such-gateway.pid"


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> PodConfig:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods-env"))
    monkeypatch.setenv("KIROCREW_POD_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    c = PodConfig.load()
    c.pods_dir.mkdir(parents=True, exist_ok=True)
    return c


# --------------------------------------------------------------------------
# The per-name mutex is a real lock on every platform
# --------------------------------------------------------------------------
def test_the_mutex_locks_through_the_shared_cross_platform_helper(cfg, monkeypatch):
    """Pinned at the primitive, because that is what makes Windows serialize.

    A pod plane on Windows is live, so an unguarded `pod up` racing a `pod down`
    on one name lets teardown stop the replacement and delete its home. The lock
    has to come from the helper that implements both platforms, and the fd has to
    come from the non-truncating open: `open(path, "w")` empties the file before
    any lock is held, which on Windows leaves a contender locking an emptied file.
    """
    seen: dict[str, object] = {}
    real_open = rt.open_lock_file
    real_lock = rt.file_lock

    def spy_open(path):
        seen["path"] = str(path)
        return real_open(path)

    def spy_lock(fd, **kwargs):
        seen["fd"] = fd
        seen["exclusive"] = kwargs.get("exclusive")
        return real_lock(fd, **kwargs)

    monkeypatch.setattr(rt, "open_lock_file", spy_open)
    monkeypatch.setattr(rt, "file_lock", spy_lock)
    with rt.pod_name_mutex(cfg, "demo"):
        pass
    assert seen["path"] == str(cfg.pods_dir / f"{cfg.unit_prefix}@demo.lock")
    assert isinstance(seen["fd"], int)
    assert seen["exclusive"] is True


def test_two_contenders_on_one_name_serialize_under_windows_semantics(cfg, monkeypatch):
    """The property the fix exists for, driven through the Windows branch.

    ``file_lock`` chooses its implementation from ``IS_POSIX``, so forcing that
    False runs the win32 acquire path: a spin on the non-blocking primitive that
    fails CLOSED. Two threads cross a barrier before either enters, and the
    critical sections must not interleave.
    """
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    holders: list[int] = []
    events: list[str] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def fake_win_acquire(fd, timeout=None):
        # Stand in for msvcrt.locking's byte-range lock, which Linux cannot run.
        # The argv shape is pinned: the acquire takes the fd it was handed.
        assert isinstance(fd, int)
        deadline = 200
        while deadline:
            if lock.acquire(blocking=False):
                return True
            deadline -= 1
            import time as _t

            _t.sleep(0.005)
        return False

    monkeypatch.setattr(platform_compat, "_win_acquire_blocking", fake_win_acquire)
    # msvcrt is imported only on win32, so the release path needs a stand-in here.
    monkeypatch.setattr(
        platform_compat,
        "msvcrt",
        types.SimpleNamespace(locking=lambda *a: None, LK_UNLCK=0),
        raising=False,
    )

    def contend(tag: str) -> None:
        barrier.wait(timeout=10)
        with rt.pod_name_mutex(cfg, "demo"):
            events.append(f"enter:{tag}")
            holders.append(len(holders) + 1)
            import time as _t

            _t.sleep(0.05)
            events.append(f"exit:{tag}")
        lock.release()

    threads = [threading.Thread(target=contend, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads), "both contenders must finish"
    # No interleaving: every enter is immediately followed by its own exit.
    assert [e.split(":")[0] for e in events] == ["enter", "exit", "enter", "exit"], events


def test_the_mutex_stays_reentrant_within_one_thread(cfg):
    """The CLI holds it across a transaction while start_pod re-acquires inside."""
    with rt.pod_name_mutex(cfg, "demo"):
        with rt.pod_name_mutex(cfg, "demo"):
            pass
    assert (cfg.pods_dir / f"{cfg.unit_prefix}@demo.lock").exists()


def test_a_stuck_holder_refuses_rather_than_running_unserialized(cfg, monkeypatch):
    """``file_lock`` fails CLOSED, and the mutex must not swallow that."""
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "_win_acquire_blocking", lambda fd, timeout=None: False)
    with pytest.raises(OSError, match="refusing to proceed unserialized"):
        with rt.pod_name_mutex(cfg, "demo"):
            pytest.fail("the critical section must not be entered without the lock")


# --------------------------------------------------------------------------
# A pid record that cannot be written is a boot failure
# --------------------------------------------------------------------------
def test_an_unwritable_pid_record_terminates_the_child_and_exits_nonzero(
    cfg, monkeypatch, tmp_path, capsys
):
    """The record IS the pod's liveness, so a pod without one must not run.

    With no record every reader calls the pod stopped, and the first ``pod down``
    deletes its task and isolated HOME while the gateway serves, reporting rc=0.
    """
    killed: list[tuple[int, str]] = []
    reaped: list[str] = []

    class FakeProc:
        pid = os.getpid()

        def kill(self):
            killed.append((self.pid, "popen"))

        def wait(self, timeout=None):
            reaped.append("wait")
            return 0

    monkeypatch.setattr(win.subprocess, "Popen", lambda argv, **kw: FakeProc())
    # Pinned alongside the fake Popen: on macOS ``process_start_time`` shells out
    # to ``ps`` through the same ``subprocess.Popen`` and would receive FakeProc.
    monkeypatch.setattr(win, "process_start_time", lambda pid: "1234567")
    monkeypatch.setattr(win, "apply_windows_resource_ceiling", lambda pid: True)
    monkeypatch.setattr(win, "resume_process_main_thread", lambda pid: True)
    monkeypatch.setattr(
        win,
        "record_supervised_pid",
        lambda c, n, p: (_ for _ in ()).throw(OSError("read-only file system")),
    )
    monkeypatch.setattr(
        win,
        "kill_process_tree_pinned",
        lambda pid, token, sig=None: killed.append((pid, "tree")) or True,
    )
    monkeypatch.setattr(
        win, "stop", lambda *a, **k: pytest.fail("the boot path must not delete the task")
    )

    rc = win.supervise_gateway(
        cfg,
        "demo",
        tmp_path / "kirocrew",
        ["gateway"],
        {},
        gateway_pid_record=_no_successor(tmp_path),
    )

    assert rc == EXIT_REFUSED_UNRECOVERABLE
    assert any(kind == "tree" for _pid, kind in killed), "the tree kill must be pinned, not bare"
    assert reaped, "the terminated child must be reaped"
    out = capsys.readouterr().out
    assert str(win.pid_record_path(cfg, "demo")) in out, "the message must name the record path"
    assert "read-only file system" in out
    # No record is left for a reader to trust, and nothing was torn down.
    assert win.supervised_pid(cfg, "demo") is None
    assert not win.pid_record_path(cfg, "demo").exists()


def _adoption_harness(
    cfg,
    monkeypatch,
    tmp_path,
    *,
    sidecar,
    children,
    record_raises_for=None,
    killed=None,
):
    """Drive `supervise_gateway` through one gateway exit. Returns recorded pids.

    *sidecar* is what the pod's own gateway pid sidecar reports (a ``(pid, token)``
    pair or ``None``); *children* is what the parent map lists under the exited
    gateway. Together they are the two signals the successor handover reads.

    *record_raises_for* makes ``record_supervised_pid`` fail for that pid, and
    *killed* collects ``(pid, token)`` from every pinned tree kill, which is how
    the unrecordable-successor path is observed.
    """
    alive = {4242, 4300}
    tokens = {4242: "1000", 4300: "2000"}
    recorded: list[int] = []

    class FakeProc:
        pid = 4242

        def kill(self):
            return None

        def wait(self, timeout=None):
            alive.discard(4242)
            return 0

    def _record(c, n, p):
        if record_raises_for is not None and p == record_raises_for:
            raise OSError("record refused")
        recorded.append(p)

    def _kill(pid, token, sig):
        if killed is not None:
            killed.append((pid, token))
        alive.discard(pid)
        return True

    monkeypatch.setattr(win.subprocess, "Popen", lambda argv, **kw: FakeProc())
    monkeypatch.setattr(win, "process_start_time", lambda pid: tokens.get(pid))
    monkeypatch.setattr(win, "pid_exists", lambda pid: pid in alive)
    monkeypatch.setattr(win, "apply_windows_resource_ceiling", lambda pid: True)
    monkeypatch.setattr(win, "resume_process_main_thread", lambda pid: True)
    monkeypatch.setattr(
        win, "attributed_descendants", lambda pid, token: children if pid == 4242 else []
    )
    monkeypatch.setattr(win, "record_supervised_pid", _record)
    monkeypatch.setattr(win, "clear_supervised_pid", lambda c, n: None)
    monkeypatch.setattr(win, "kill_process_tree_pinned", _kill)
    monkeypatch.setattr(win.run_marker, "read_pid_record_path", lambda path: sidecar)
    monkeypatch.setattr(win.run_marker, "pid_start_token", lambda pid: tokens.get(pid, ""))
    # Every sleep in the handover retires the successor, so the supervision loop
    # makes exactly one pass instead of blocking on a process that never dies.
    monkeypatch.setattr(win.time, "sleep", lambda s: alive.discard(4300))

    win.supervise_gateway(
        cfg, "demo", tmp_path / "kirocrew", ["gateway"], {}, gateway_pid_record=tmp_path / "gw.pid"
    )
    return recorded


def _stepping_wait(alive, sidecar, tokens):
    """A ``_wait_for_pid`` that retires the awaited process and stages its successor.

    Replaces the real poll so the loop makes deterministic progress: waiting on
    4300 ends it and hands the pod to 4400, whose sidecar write lands one read
    LATE; waiting on 4400 ends the chain.
    """

    def _wait(pid, token, *, on_poll=None):
        # The real wait runs `on_poll` while the target lives; exercising it here
        # keeps the marker refresh on the path these tests drive.
        if on_poll is not None:
            on_poll()
        alive.discard(pid)
        if pid == 4300:
            alive.add(4400)
            sidecar["pid"] = 4400
            sidecar["stale_reads"] = 1

    return _wait


def test_a_second_restart_is_adopted_too_because_the_anchor_advances(cfg, monkeypatch, tmp_path):
    """Two restarts in one pod lifetime is ordinary, and a fixed anchor loses the pod.

    Both of ``_await_successor``'s signals are relative to the process just reaped:
    the sidecar read excludes THAT pid as a leftover, and the tree pre-check asks
    whether THAT pid still has a live attributed child. Anchoring on the original
    gateway forever therefore breaks on the SECOND handover -- the intermediate is
    already dead, so it has no children to find, and the window closes instantly
    while the new successor is still booting. The ``finally`` then clears the only
    record that reports this pod alive, and the next ``pod down`` ``rmtree``s a
    serving gateway's isolated HOME.

    Pinned with the second successor's sidecar write deliberately LATE, because
    that is the read the anchor decides. A successor quick enough to claim the
    sidecar before the first read is adopted even with a fixed anchor, so a test
    without the delay would pass either way and prove nothing.
    """
    alive = {4242}
    tokens = {4242: "1000", 4300: "2000", 4400: "3000"}
    children = {4242: [4300], 4300: [4400]}
    recorded: list[int] = []
    sidecar = {"pid": 4300, "stale_reads": 0}

    class FakeProc:
        pid = 4242

        def kill(self):
            return None

        def wait(self, timeout=None):
            alive.discard(4242)
            alive.add(4300)
            return 0

    def _read_sidecar(path):
        if sidecar["stale_reads"] > 0:
            sidecar["stale_reads"] -= 1
            return None
        pid = sidecar["pid"]
        return (pid, tokens[pid])

    monkeypatch.setattr(win.subprocess, "Popen", lambda argv, **kw: FakeProc())
    monkeypatch.setattr(win, "process_start_time", lambda pid: tokens.get(pid, ""))
    monkeypatch.setattr(win, "pid_exists", lambda pid: pid in alive)
    monkeypatch.setattr(win, "apply_windows_resource_ceiling", lambda pid: True)
    monkeypatch.setattr(win, "resume_process_main_thread", lambda pid: True)
    monkeypatch.setattr(win, "attributed_descendants", lambda pid, token: children.get(pid, []))
    monkeypatch.setattr(win, "record_supervised_pid", lambda c, n, p: recorded.append(p))
    monkeypatch.setattr(win, "clear_supervised_pid", lambda c, n: None)
    monkeypatch.setattr(win.run_marker, "read_pid_record_path", _read_sidecar)
    monkeypatch.setattr(win.run_marker, "pid_start_token", lambda pid: tokens.get(pid, ""))
    monkeypatch.setattr(win.time, "sleep", lambda s: None)
    monkeypatch.setattr(win, "_wait_for_pid", _stepping_wait(alive, sidecar, tokens))

    win.supervise_gateway(
        cfg, "demo", tmp_path / "kirocrew", ["gateway"], {}, gateway_pid_record=tmp_path / "gw.pid"
    )

    assert recorded == [4242, 4300, 4400], (
        "each successor in turn must be recorded as this pod's gateway; stopping at "
        "[4242, 4300] means the second restart was read as a stop"
    )


def test_a_successor_that_cannot_be_recorded_is_ended_not_left_serving(cfg, monkeypatch, tmp_path):
    """An untrackable successor must die, because the alternative is unrecoverable.

    If the record write fails, the successor is serving under a pid no verb can
    see: ``is_active`` reports the pod stopped, and ``stop`` then takes neither the
    survivor snapshot nor the still-alive guard, so the next ``pod down`` deletes
    the task and ``rmtree``s the isolated HOME beneath a live writer. Advising the
    operator to run ``pod down`` would therefore name the harm as the remedy.

    Ending it inverts that: the cost is one lost restart, which a boot recovers,
    and ``pod down`` becomes safe to advise because it is then acting on a pod that
    really is gone. The kill is PINNED to the creation time read for the adoption,
    so a pid recycled between the sighting and the kill cannot be signalled.
    """
    killed: list[tuple[int, str]] = []
    recorded = _adoption_harness(
        cfg,
        monkeypatch,
        tmp_path,
        sidecar=(4300, "2000"),
        children=[4300],
        record_raises_for=4300,
        killed=killed,
    )

    assert recorded == [4242], "the successor must not be reported as this pod's gateway"
    assert killed == [(4300, "2000")], "the unrecordable successor must be ended, tree and all"


def test_an_in_app_restart_is_adopted_instead_of_read_as_a_stop(cfg, monkeypatch, tmp_path):
    """The pod did not stop, so its record must not be cleared as if it had.

    Windows has no exec: `POST /api/restart`, the update path and the
    stale-assets reload all reach ``reexec_python_module``, whose ``os.execv``
    spawns a SUCCESSOR and exits the caller — measured on this platform, with the
    predecessor reporting exit code 0 so the code cannot tell a restart from a
    stop. ``proc.wait()`` therefore returns while the pod is still serving under a
    new pid. Clearing the record there is the fail-OPEN direction: the record is
    the only thing that reports this pod alive on Windows, so `is_active` would
    call it stopped, `stop` would take neither the survivor snapshot nor the
    still-alive guard, and the next `pod down` would `rmtree` a serving gateway's
    isolated HOME.
    """
    recorded = _adoption_harness(
        cfg, monkeypatch, tmp_path, sidecar=(4300, "2000"), children=[4300]
    )

    assert recorded[0] == 4242, "the gateway it spawned is recorded first, as before"
    assert recorded[1:] == [4300], "the restart successor must be adopted as the pod's gateway"


def test_an_ordinary_child_is_not_adopted_as_the_gateway(cfg, monkeypatch, tmp_path):
    """A live child is not a successor, and the tree cannot tell them apart.

    A gateway legitimately spawns children (MCP servers, agent sessions), and on
    this platform a restart successor is also just a child, so the parent map
    answers "a live child exists" for both. Claiming the pod's gateway sidecar is
    what makes a process the gateway, so adoption reads THAT — otherwise a pod
    whose gateway really did exit would keep an MCP server recorded as its
    gateway and never report itself stopped.
    """
    recorded = _adoption_harness(cfg, monkeypatch, tmp_path, sidecar=None, children=[4300])

    assert recorded == [4242], "only the process that claimed the gateway sidecar may be adopted"


def test_a_fragment_left_by_a_failed_record_write_is_cleared(cfg, monkeypatch, tmp_path):
    """A partial record is worse than none: it names a pid nothing can attribute."""
    fragment = win.pid_record_path(cfg, "demo")

    class FakeProc:
        pid = os.getpid()

        def kill(self):
            return None

        def wait(self, timeout=None):
            return 0

    def half_write(c, n, p):
        fragment.parent.mkdir(parents=True, exist_ok=True)
        fragment.write_text(f"{p}\n")
        raise OSError("no space left on device")

    monkeypatch.setattr(win.subprocess, "Popen", lambda argv, **kw: FakeProc())
    # Pinned alongside the fake Popen: on macOS ``process_start_time`` shells out
    # to ``ps`` through the same ``subprocess.Popen`` and would receive FakeProc.
    monkeypatch.setattr(win, "process_start_time", lambda pid: "1234567")
    monkeypatch.setattr(win, "apply_windows_resource_ceiling", lambda pid: True)
    monkeypatch.setattr(win, "resume_process_main_thread", lambda pid: True)
    monkeypatch.setattr(win, "record_supervised_pid", half_write)
    monkeypatch.setattr(win, "kill_process_tree_pinned", lambda pid, token, sig=None: True)

    assert (
        win.supervise_gateway(
            cfg,
            "demo",
            tmp_path / "kirocrew",
            ["gateway"],
            {},
            gateway_pid_record=_no_successor(tmp_path),
        )
        != 0
    )
    assert not fragment.exists()


def test_record_supervised_pid_raises_instead_of_swallowing(cfg, monkeypatch):
    """The primitive itself must surface the failure to its one caller."""

    def boom(*a, **k):
        raise OSError("access is denied")

    monkeypatch.setattr(win.Path, "write_text", boom)
    with pytest.raises(OSError, match="access is denied"):
        win.record_supervised_pid(cfg, "demo", os.getpid())


# --------------------------------------------------------------------------
# stop(): the symmetric case
# --------------------------------------------------------------------------
def test_stop_refuses_when_a_recorded_pid_is_alive_but_unattributable(cfg, monkeypatch):
    """ "Cannot tell" must not be rendered as "not running" on the teardown path.

    A record naming a LIVE pid whose creation-time identity does not match is
    what a fragment or a recycled pid looks like. Deleting the task there hands
    the caller an rc=0 it reclaims the HOME on, out from under a live process.
    """
    win.pid_record_path(cfg, "demo").write_text(f"{os.getpid()}\nnot-the-real-token\n")
    calls: list[str] = []
    monkeypatch.setattr(win, "schtasks", lambda *a: calls.append(a[0]) or _cp())
    win.write_task_script(cfg, "demo")

    cp = win.stop(cfg, "demo")

    assert cp.returncode == 1
    assert "does not carry the creation-time identity" in cp.stderr
    assert str(win.pid_record_path(cfg, "demo")) in cp.stderr
    assert str(os.getpid()) in cp.stderr
    assert "/Delete" not in calls, "the task must not be deleted on an unprovable stop"
    assert win.task_script_path(cfg, "demo").exists(), "per-pod state must be preserved"


def test_stop_still_reclaims_a_stale_record_for_a_dead_pid(cfg, monkeypatch):
    """The ordinary hard-stop leftover must keep passing through.

    A ``/End`` reaps the wrapper before its cleanup runs, so a record naming a
    pid that is GONE is routine. Refusing on that would block every `pod down`
    after a hard stop.
    """
    win.pid_record_path(cfg, "demo").write_text("999999999\nstale-token\n")
    monkeypatch.setattr(win, "schtasks", lambda *a: _cp())
    monkeypatch.setattr(win, "pid_exists", lambda pid: False)
    win.write_task_script(cfg, "demo")

    cp = win.stop(cfg, "demo")

    assert cp.returncode == 0
    assert not win.task_script_path(cfg, "demo").exists()
    assert not win.pid_record_path(cfg, "demo").exists()


def test_stop_refuses_when_the_task_wrapper_cannot_be_deleted(cfg, monkeypatch):
    wrapper = win.task_script_path(cfg, "demo")
    win.write_task_script(cfg, "demo")
    monkeypatch.setattr(win, "schtasks", lambda *a: _cp())
    monkeypatch.setattr(win, "supervised_pid", lambda c, n: None)
    monkeypatch.setattr(win, "handoff_in_progress", lambda c, n: False)
    monkeypatch.setattr(win, "process_start_time", lambda pid: None)
    monkeypatch.setattr(win.time, "sleep", lambda seconds: None)
    real_unlink = win.Path.unlink

    def _unlink(path, *args, **kwargs):
        if path == wrapper:
            raise OSError("access is denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(win.Path, "unlink", _unlink)

    cp = win.stop(cfg, "demo")

    assert cp.returncode == 1
    assert str(wrapper) in cp.stderr
    assert "access is denied" in cp.stderr
    assert wrapper.exists(), "the caller must preserve state after a stop failure"


def test_stop_with_no_record_at_all_is_the_plain_stopped_path(cfg, monkeypatch):
    """A cleanly stopped pod unlinks its record, so absence is not ambiguity."""
    monkeypatch.setattr(win, "schtasks", lambda *a: _cp())
    win.write_task_script(cfg, "demo")
    assert win.stop(cfg, "demo").returncode == 0


def test_the_unattributable_probe_ignores_a_provable_record(cfg):
    """A record that PROVES itself is the live-pod path, handled before this."""
    win.record_supervised_pid(cfg, "demo", os.getpid())
    assert win._unattributable_live_pid(cfg, "demo") is None
    assert win.supervised_pid(cfg, "demo") == os.getpid()


def test_the_unattributable_probe_ignores_a_junk_record(cfg):
    win.pid_record_path(cfg, "demo").write_text("not-a-pid\n")
    assert win._unattributable_live_pid(cfg, "demo") is None


def test_stop_ends_the_gateways_children_after_the_gateway_itself(cfg, monkeypatch):
    """The pid record proves the gateway exited and nothing about its children.

    A kiro-cli session or MCP server that outlives the gateway would keep
    writing into the HOME `pod down` is about to delete, so the children are
    snapshotted before `/End`, ended pinned by their creation identity after the
    gateway is gone, and only then is the pod stopped.
    """
    win.write_task_script(cfg, "demo")
    tokens = {4242: "1000", 4300: "1010", 4301: "1020"}
    alive = {4242, 4300, 4301}
    monkeypatch.setattr(win, "schtasks", lambda *a: _cp())
    monkeypatch.setattr(
        win, "attributed_descendants", lambda pid, token: [4300, 4301] if pid == 4242 else []
    )
    monkeypatch.setattr(
        win, "process_start_time", lambda pid: tokens[pid] if pid in alive else None
    )
    # `alive` is this test's model of liveness, so it must drive BOTH facets the
    # backend reads. A creation token is an IDENTITY: on Windows it stays
    # readable for as long as a handle to the exited process is open, so the
    # backend asks `pid_exists` first and only then narrows the live pid with the
    # token. Stubbing the token alone would model a host where identity implies
    # existence, which is the confusion `_still_alive` exists to remove.
    monkeypatch.setattr(win, "pid_exists", lambda pid: pid in alive)

    def _supervised(c, n):
        return 4242 if 4242 in alive else None

    monkeypatch.setattr(win, "supervised_pid", _supervised)
    killed: list[tuple[int, str]] = []

    def _kill(pid, token, sig=None):
        killed.append((pid, token))
        alive.discard(pid)
        return True

    monkeypatch.setattr(win, "kill_process_tree_pinned", _kill)

    cp = win.stop(cfg, "demo", timeout=0.5)

    assert cp.returncode == 0, cp.stderr
    assert (4300, "1010") in killed and (4301, "1020") in killed
    assert alive == set()


def test_stop_keeps_the_pod_when_a_child_survives_the_gateway(cfg, monkeypatch):
    win.write_task_script(cfg, "demo")
    tokens = {4242: "1000", 4300: "1010"}
    alive = {4242, 4300}
    calls: list[str] = []
    monkeypatch.setattr(win, "schtasks", lambda *a: calls.append(a[0]) or _cp())
    monkeypatch.setattr(
        win, "attributed_descendants", lambda pid, token: [4300] if pid == 4242 else []
    )
    monkeypatch.setattr(
        win, "process_start_time", lambda pid: tokens[pid] if pid in alive else None
    )
    # `alive` is this test's model of liveness, so it must drive BOTH facets the
    # backend reads. A creation token is an IDENTITY: on Windows it stays
    # readable for as long as a handle to the exited process is open, so the
    # backend asks `pid_exists` first and only then narrows the live pid with the
    # token. Stubbing the token alone would model a host where identity implies
    # existence, which is the confusion `_still_alive` exists to remove.
    monkeypatch.setattr(win, "pid_exists", lambda pid: pid in alive)
    monkeypatch.setattr(win, "supervised_pid", lambda c, n: 4242 if 4242 in alive else None)

    def _kill(pid, token, sig=None):
        if pid == 4242:
            alive.discard(pid)
        return True  # the child ignores it and stays alive

    monkeypatch.setattr(win, "kill_process_tree_pinned", _kill)
    monkeypatch.setattr(win.time, "sleep", lambda s: alive.discard(4242))

    cp = win.stop(cfg, "demo", timeout=0.5)

    assert cp.returncode == 1
    assert "child processes are still running" in cp.stderr and "4300" in cp.stderr
    assert "/Delete" not in calls, "a pod with a live child must not be deleted"
    assert win.task_script_path(cfg, "demo").exists()


def test_stop_treats_a_child_that_vanished_under_the_kill_as_gone(cfg, monkeypatch):
    """taskkill reports rc=128 when a tree member exits between the snapshot and
    the kill; that raises ProcessLookupError, and the child being gone is the
    outcome wanted, so the stop must finish rather than traceback."""
    win.write_task_script(cfg, "demo")
    tokens = {4242: "1000", 4300: "1010"}
    alive = {4242, 4300}
    monkeypatch.setattr(win, "schtasks", lambda *a: _cp())
    monkeypatch.setattr(
        win, "attributed_descendants", lambda pid, token: [4300] if pid == 4242 else []
    )
    monkeypatch.setattr(
        win, "process_start_time", lambda pid: tokens[pid] if pid in alive else None
    )
    # `alive` is this test's model of liveness, so it must drive BOTH facets the
    # backend reads. A creation token is an IDENTITY: on Windows it stays
    # readable for as long as a handle to the exited process is open, so the
    # backend asks `pid_exists` first and only then narrows the live pid with the
    # token. Stubbing the token alone would model a host where identity implies
    # existence, which is the confusion `_still_alive` exists to remove.
    monkeypatch.setattr(win, "pid_exists", lambda pid: pid in alive)
    monkeypatch.setattr(win, "supervised_pid", lambda c, n: 4242 if 4242 in alive else None)

    def _kill(pid, token, sig=None):
        alive.discard(pid)
        if pid == 4300:
            raise ProcessLookupError("[taskkill rc=128] no running instance of the task")
        return True

    monkeypatch.setattr(win, "kill_process_tree_pinned", _kill)

    cp = win.stop(cfg, "demo", timeout=0.5)

    assert cp.returncode == 0, cp.stderr


def test_stop_leaves_alone_a_stray_the_parent_map_lists_under_a_recycled_pid(cfg, monkeypatch):
    """`stop` acts on the ATTRIBUTED walk and on nothing else.

    Windows keeps a dead parent's pid on its children, so when that pid is recycled
    to the gateway an unrelated process shows up as the gateway's child. Telling the
    two apart is `platform_compat.attributed_descendants`' job -- it validates every
    parent-child edge, and `test_platform_compat.py` pins the exclusion against the
    primitive itself, where the parent map is the input.

    What THIS test pins is the pod backend's half of the contract: whatever the walk
    excludes is neither killed nor allowed to hold the stop open. 7777 is present and
    alive in the model below and absent from the walk; a `stop` that reached past the
    walk -- to a raw descendant list, say -- would kill it or block on it, and both
    show up here."""
    win.write_task_script(cfg, "demo")
    tokens = {4242: "1000", 4300: "1010", 7777: "900"}  # 7777 predates the gateway
    alive = {4242, 4300, 7777}
    monkeypatch.setattr(win, "schtasks", lambda *a: _cp())
    monkeypatch.setattr(
        win, "attributed_descendants", lambda pid, token: [4300] if pid == 4242 else []
    )
    monkeypatch.setattr(
        win, "process_start_time", lambda pid: tokens[pid] if pid in alive else None
    )
    # `alive` is this test's model of liveness, so it must drive BOTH facets the
    # backend reads. A creation token is an IDENTITY: on Windows it stays
    # readable for as long as a handle to the exited process is open, so the
    # backend asks `pid_exists` first and only then narrows the live pid with the
    # token. Stubbing the token alone would model a host where identity implies
    # existence, which is the confusion `_still_alive` exists to remove.
    monkeypatch.setattr(win, "pid_exists", lambda pid: pid in alive)
    monkeypatch.setattr(win, "supervised_pid", lambda c, n: 4242 if 4242 in alive else None)
    killed: list[int] = []

    def _kill(pid, token, sig=None):
        killed.append(pid)
        alive.discard(pid)
        return True

    monkeypatch.setattr(win, "kill_process_tree_pinned", _kill)

    cp = win.stop(cfg, "demo", timeout=0.5)

    assert cp.returncode == 0, cp.stderr
    assert 7777 not in killed and 7777 in alive
    assert 4300 in killed


def test_a_failed_tree_kill_still_runs_the_fallback_kill(cfg, monkeypatch):
    """A best-effort step must not be able to cancel the step that covers for it.

    The pinned tree kill is one access-denied member away from raising, and this
    teardown runs on a gateway that could not be RECORDED -- so nothing else will
    ever come back for it. If the raise escaped, ``proc.kill()`` and the reap below
    it would be skipped and the caller's boot-failure handling would be replaced by
    an OSError, leaving a live gateway against a HOME the caller is about to reclaim.
    """
    calls: list[str] = []

    def _raising_tree_kill(pid, token, sig):
        calls.append("tree")
        raise OSError(5, "Access is denied")

    monkeypatch.setattr(win, "kill_process_tree_pinned", _raising_tree_kill)
    monkeypatch.setattr(win, "process_start_time", lambda pid: "2000")

    class _Proc:
        pid = 4321

        def kill(self):
            calls.append("kill")

        def wait(self, timeout=None):
            calls.append("wait")
            return 0

    win._terminate_unrecorded_child(_Proc())  # must not raise

    assert calls == ["tree", "kill", "wait"], (
        "the fallback kill and the reap must both still run after the tree kill "
        f"raises; got {calls}"
    )


def test_start_refuses_when_the_stale_result_cannot_be_removed(cfg, monkeypatch):
    """A start that leaves a stale FAILURE result behind is worse than no start.

    ``unit_state`` reads that file, so until it is gone the pod booting right now
    reports as already failed -- and a caller acting on a failed boot reclaims the
    pod's state, deleting the isolated HOME of a gateway that is coming up. So the
    removal is a PRECONDITION, refused before ``/Create``, not a best-effort step.

    Pinned on both halves: no task is created, and the refusal names the file so an
    operator can act on it.
    """
    created: list[tuple] = []
    monkeypatch.setattr(win, "write_task_script", lambda c, n: c.home_dir(n) / "run.cmd")
    monkeypatch.setattr(win, "schtasks", lambda *a: created.append(a) or _cp())

    def _locked(self, missing_ok=False):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(win.Path, "unlink", _locked)

    result = win.start(cfg, "demo")

    assert result.returncode != 0, "a start that cannot clear the stale result must refuse"
    assert created == [], "no task may be created once the start has been refused"
    assert (
        str(win.result_path(cfg, "demo")) in result.stderr
    ), "the refusal must name the file, since removing it is the operator's move"


class TestHandoffWindow:
    """The gap between reaping a predecessor and recording its successor.

    `supervised_pid` fails closed on a dead pid, so during a restart handoff the pod
    reads STOPPED while a successor is booting into its HOME. Teardown must not spend
    that reading, because the spend is an irreversible rmtree.
    """

    def test_stop_refuses_while_a_handoff_marker_is_fresh(self, cfg, monkeypatch):
        win.write_task_script(cfg, "demo")
        win.handoff_marker_path(cfg, "demo").write_text("handoff\n", encoding="utf-8")
        monkeypatch.setattr(win, "supervised_pid", lambda c, n: None)
        monkeypatch.setattr(
            win, "schtasks", lambda *a: pytest.fail("teardown must not reach schtasks")
        )

        cp = win.stop(cfg, "demo", timeout=0.5)

        assert cp.returncode != 0, "a handoff in flight must not read as a clean stop"
        assert "handing off" in cp.stderr
        assert "kirocrew pod down demo" in cp.stderr, "the refusal must name the retry"

    def test_the_marker_the_stop_itself_caused_is_waited_out_not_refused(self, cfg, monkeypatch):
        """The reap teardown CAUSES publishes a marker; refusing on it fails the stop.

        `/End` kills the gateway, the supervisor's very next statement after the reap
        publishes the handoff marker, and `/End` then reaps the supervisor too -- so
        nothing retracts it. Reading that orphan as "a restart is in flight" made
        `pod down` refuse a pod it had just stopped and, worse, skip `/Delete`, so the
        scheduled task stayed registered on the host. That is the CI failure this
        pins: teardown must wait for the supervisor's verdict, and recognise a marker
        whose publisher is gone as no verdict at all.
        """
        win.write_task_script(cfg, "demo")
        calls: list[str] = []
        live = {"pid": 4242}

        def _schtasks(*argv):
            calls.append(argv[0])
            if argv[0] == "/End":
                # The reap: the gateway dies, the supervisor publishes as its next
                # statement, and is then reaped itself before it can retract.
                dead_supervisor = f"{2**22 + 91}\n1000\n"
                win.handoff_marker_path(cfg, "demo").write_text(dead_supervisor, encoding="utf-8")
                live["pid"] = None
            return _cp()

        monkeypatch.setattr(win, "schtasks", _schtasks)
        monkeypatch.setattr(win, "supervised_pid", lambda c, n: live["pid"])
        monkeypatch.setattr(win, "process_start_time", lambda pid: "1000" if pid == 4242 else None)
        monkeypatch.setattr(win, "attributed_descendants", lambda pid, token: [])
        monkeypatch.setattr(win, "_unattributable_live_pid", lambda c, n: None)

        cp = win.stop(cfg, "demo", timeout=1.0)

        assert cp.returncode == 0, cp.stderr
        assert calls == ["/End", "/Delete"], "teardown must reach /Delete, not refuse"
        assert not win.task_script_path(cfg, "demo").exists()
        assert not win.handoff_marker_path(
            cfg, "demo"
        ).exists(), "a settled teardown must not leave per-pod state behind"

    def test_a_live_publisher_still_blocks_teardown_until_its_bound(self, cfg, monkeypatch):
        """The protection is intact when a supervisor really is deciding.

        The publisher check is not "ignore markers"; it asks whether the process that
        claimed the window still exists. This one does (it is the test process), and
        it never retracts, so teardown must refuse rather than reclaim -- and it must
        refuse without reaching `/Delete`.
        """
        win.write_task_script(cfg, "demo")
        calls: list[str] = []
        assert win._begin_handoff(cfg, "demo") is True

        monkeypatch.setattr(win, "schtasks", lambda *a: calls.append(a[0]) or _cp())
        monkeypatch.setattr(win, "supervised_pid", lambda c, n: None)

        cp = win.stop(cfg, "demo", timeout=0.4)

        assert cp.returncode != 0
        assert "handing off" in cp.stderr
        assert "/Delete" not in calls, "an undecided handoff must not delete the task"
        assert win.task_script_path(cfg, "demo").exists()

    def test_a_successor_recorded_during_the_wait_is_reported_as_running(self, cfg, monkeypatch):
        """The adopted-successor branch must not claim it waited out the ceiling.

        A pid reappearing means the supervisor DECIDED and is serving again. That is a
        different refusal from "undecided", and it is available immediately, so the
        wait must end on it rather than run to its bound.
        """
        win.write_task_script(cfg, "demo")
        assert win._begin_handoff(cfg, "demo") is True
        seen = {"polls": 0}

        def _pid(c, n):
            seen["polls"] += 1
            return 7788 if seen["polls"] > 1 else None

        monkeypatch.setattr(win, "supervised_pid", _pid)
        started = time.monotonic()

        assert win._await_handoff_outcome(cfg, "demo", bound=30.0) is False
        assert time.monotonic() - started < 5.0, "must not run to the bound"

    def test_stop_settles_a_missing_pid_before_deleting_the_task(self, cfg, monkeypatch):
        """A handoff marker can appear just after the predecessor pid dies."""
        win.write_task_script(cfg, "demo")
        calls: list[str] = []
        clock = {"now": 0.0, "handoff": False, "publish": True}

        monkeypatch.setattr(win, "schtasks", lambda *a: calls.append(a[0]) or _cp())
        monkeypatch.setattr(win, "supervised_pid", lambda c, n: None)
        monkeypatch.setattr(win, "handoff_in_progress", lambda c, n: clock["handoff"])
        monkeypatch.setattr(win, "process_start_time", lambda pid: None)
        monkeypatch.setattr(win.time, "monotonic", lambda: clock["now"])

        def _sleep(seconds):
            clock["now"] += seconds
            if clock["publish"]:
                clock["handoff"] = True

        monkeypatch.setattr(win.time, "sleep", _sleep)

        refused = win.stop(cfg, "demo", timeout=0.5)

        assert refused.returncode == 1
        assert calls == [], "a marker seen during settling must block task deletion"
        assert win.task_script_path(cfg, "demo").exists()

        clock.update(now=0.0, handoff=False, publish=False)
        stopped = win.stop(cfg, "demo", timeout=0.5)

        assert stopped.returncode == 0
        assert calls == ["/End", "/Delete"]
        assert clock["now"] == pytest.approx(win._HANDOFF_SETTLE_TIMEOUT_SECS, abs=0.001)
        assert not win.task_script_path(cfg, "demo").exists()

    def test_a_stale_marker_does_not_wedge_teardown_forever(self, cfg, monkeypatch):
        """A supervisor killed mid-handoff cannot retract its own marker.

        Honouring it indefinitely would trade a narrow unsafe window for a permanent
        inability to reclaim the pod, which is worse: the marker is judged by
        freshness, with the supervisor's own adoption ceiling as the bound.
        """
        marker = win.handoff_marker_path(cfg, "demo")
        marker.write_text("handoff\n", encoding="utf-8")
        stale = time.time() - (win.SUCCESSOR_ADOPT_TIMEOUT_SECS + 3600)
        os.utime(marker, (stale, stale))

        assert win.handoff_in_progress(cfg, "demo") is False

    def test_an_old_marker_tracks_its_live_publisher(self, cfg, monkeypatch):
        """A live publisher keeps an old handoff active across suspend time."""
        marker = win.handoff_marker_path(cfg, "demo")
        marker.write_text("4242\nlive-token\n", encoding="utf-8")
        stale = time.time() - (win.SUCCESSOR_ADOPT_TIMEOUT_SECS + 3600)
        os.utime(marker, (stale, stale))

        monkeypatch.setattr(win, "pid_exists", lambda pid: True)
        monkeypatch.setattr(win, "process_start_time", lambda pid: "live-token")
        assert win.handoff_in_progress(cfg, "demo") is True

        # A recycled pid (live, different creation token) is not the publisher.
        monkeypatch.setattr(win, "process_start_time", lambda pid: "replacement-token")
        assert win.handoff_in_progress(cfg, "demo") is False

    def test_a_young_marker_from_a_dead_publisher_is_not_a_handoff(self, cfg, monkeypatch):
        """An orphaned marker (its supervisor reaped by /End before the retract)
        must not make pod down refuse the pod it has itself just stopped, however
        fresh the marker is."""
        win.handoff_marker_path(cfg, "demo").write_text("4242\ngone\n", encoding="utf-8")
        monkeypatch.setattr(win, "pid_exists", lambda pid: False)
        monkeypatch.setattr(win, "process_start_time", lambda pid: None)
        assert win.handoff_in_progress(cfg, "demo") is False

    def test_an_unreadable_marker_counts_as_a_live_handoff(self, cfg, monkeypatch):
        """Fail CLOSED on a stat error: the alternative is deleting a serving pod."""
        marker = win.handoff_marker_path(cfg, "demo")
        marker.write_text("handoff\n", encoding="utf-8")

        real_stat = win.Path.stat

        def _refusing_stat(self, *a, **k):
            if self == marker:
                raise OSError("stat refused")
            return real_stat(self, *a, **k)

        monkeypatch.setattr(win.Path, "stat", _refusing_stat)

        assert win.handoff_in_progress(cfg, "demo") is True

    def test_the_marker_is_restamped_on_every_handover_not_just_the_first(
        self, cfg, monkeypatch, tmp_path
    ):
        """A pod's SECOND in-app restart must be covered too.

        The freshness bound exists so a supervisor killed mid-handoff cannot wedge
        teardown forever, and it reads the marker's mtime. Stamping only once, before
        the successor loop, therefore leaves every handover after the first
        uncovered: on the second one the mtime dates from the FIRST reap, so a pod
        restarting more than the bound later reads as "no handoff" while
        `supervised_pid` is None -- and a concurrent `pod down` deletes the isolated
        HOME under the booting successor. Two restarts in one pod lifetime is
        ordinary, so this is the common case, not a corner.

        Asserted by COUNT rather than by mtime: a filesystem whose timestamp
        granularity is coarser than the test's runtime would make an mtime
        comparison flaky, while "stamped once per gap" is the actual contract.
        """
        stamps: list[int] = []
        tokens = {4242: "1000", 4300: "2000", 4400: "3000"}
        children = {4242: [4300], 4300: [4400]}
        alive = {4242}
        sidecar = {"pid": 4300}

        class FakeProc:
            pid = 4242

            def kill(self):
                return None

            def wait(self, timeout=None):
                alive.discard(4242)
                alive.add(4300)
                return 0

        def _wait_for(pid, token, *, on_poll=None):
            if on_poll is not None:
                on_poll()
            alive.discard(pid)
            if pid == 4300:
                alive.add(4400)
                sidecar["pid"] = 4400

        monkeypatch.setattr(win.subprocess, "Popen", lambda argv, **kw: FakeProc())
        monkeypatch.setattr(win, "process_start_time", lambda pid: tokens.get(pid, ""))
        monkeypatch.setattr(win, "pid_exists", lambda pid: pid in alive)
        monkeypatch.setattr(win, "apply_windows_resource_ceiling", lambda pid: True)
        monkeypatch.setattr(win, "resume_process_main_thread", lambda pid: True)
        monkeypatch.setattr(win, "attributed_descendants", lambda pid, token: children.get(pid, []))
        monkeypatch.setattr(win, "record_supervised_pid", lambda c, n, p: None)
        monkeypatch.setattr(win, "clear_supervised_pid", lambda c, n: None)
        monkeypatch.setattr(
            win.run_marker,
            "read_pid_record_path",
            lambda path: (sidecar["pid"], tokens[sidecar["pid"]]),
        )
        monkeypatch.setattr(win.run_marker, "pid_start_token", lambda pid: tokens.get(pid, ""))
        monkeypatch.setattr(win.time, "sleep", lambda s: None)
        monkeypatch.setattr(win, "_wait_for_pid", _wait_for)
        monkeypatch.setattr(win, "_begin_handoff", lambda c, n: bool(stamps.append(1)) or True)

        win.supervise_gateway(
            cfg,
            "demo",
            tmp_path / "kirocrew",
            ["gateway"],
            {},
            gateway_pid_record=tmp_path / "gw.pid",
        )

        assert len(stamps) >= 2, (
            "the marker must be re-stamped for each handover; one stamp leaves the "
            "second restart uncovered once the first mtime goes stale"
        )

    def test_a_successor_too_slow_to_adopt_is_ended_before_the_state_is_dropped(
        self, cfg, monkeypatch, tmp_path
    ):
        """The adoption window closing is not proof that nothing is running.

        `_await_successor` gives up after SUCCESSOR_ADOPT_TIMEOUT_SECS. A successor
        that booted but took longer than that to claim the sidecar is ALIVE and was
        never adopted, and the exits below then clear the record and retract the
        marker — leaving it serving with neither. Same unrecoverable shape as a
        successor that could not be recorded, so the same answer: end it, and end it
        BEFORE the covering state goes away.
        """
        killed: list[int] = []
        alive = {4242, 4300}
        tokens = {4242: "1000", 4300: "2000"}

        class FakeProc:
            pid = 4242

            def kill(self):
                return None

            def wait(self, timeout=None):
                alive.discard(4242)
                return 0

        monkeypatch.setattr(win.subprocess, "Popen", lambda argv, **kw: FakeProc())
        monkeypatch.setattr(win, "process_start_time", lambda pid: tokens.get(pid, ""))
        monkeypatch.setattr(win, "pid_exists", lambda pid: pid in alive)
        monkeypatch.setattr(win, "apply_windows_resource_ceiling", lambda pid: True)
        monkeypatch.setattr(win, "resume_process_main_thread", lambda pid: True)
        monkeypatch.setattr(win, "record_supervised_pid", lambda c, n, p: None)
        monkeypatch.setattr(win, "clear_supervised_pid", lambda c, n: None)
        # A live attributed child exists throughout, but the sidecar is NEVER
        # claimed, so `_await_successor` times out with 4300 still running.
        monkeypatch.setattr(win, "_live_children_of", lambda pid, token: [4300])
        monkeypatch.setattr(win.run_marker, "read_pid_record_path", lambda path: None)
        monkeypatch.setattr(win, "_terminate_unrecorded_pid", lambda pid, **k: killed.append(pid))
        monkeypatch.setattr(win, "SUCCESSOR_ADOPT_TIMEOUT_SECS", 0.05)
        monkeypatch.setattr(win.time, "sleep", lambda s: None)

        win.supervise_gateway(
            cfg,
            "demo",
            tmp_path / "kirocrew",
            ["gateway"],
            {},
            gateway_pid_record=tmp_path / "gw.pid",
        )

        assert killed == [4300], (
            "a live successor that missed the adoption window must be ended, not left "
            "serving with no record and no marker"
        )

    def test_the_anchor_is_the_gateway_not_the_launcher(self, cfg, monkeypatch, tmp_path):
        """A successor hangs off the GATEWAY, which can be the launcher's child.

        `bin_path` may spawn the interpreter that becomes the gateway, and an in-app
        restart execs inside THAT process, so the successor is the launcher's
        grandchild. Anchoring the search on the launcher does not merely make it
        harder -- per-edge attribution DROPS a child whose intermediate is dead, and
        the intermediate is precisely the process that exec'd away. The window would
        then close having found nothing, the record would be cleared, and `pod down`
        would rmtree the HOME under a live gateway.

        Asserted on the anchor the search is given, because that is the decision:
        the pid the gateway's own sidecar named, not the pid this supervisor spawned.
        """
        anchors: list[tuple[int, str]] = []
        alive = {4242}
        tokens = {4242: "1000", 4300: "2000", 4400: "3000"}
        # The launcher (4242) spawned the gateway (4300); the gateway has exec'd and
        # is gone, leaving the successor (4400) as the launcher's grandchild.
        sidecar = {"pid": 4300, "token": "2000"}

        class FakeProc:
            pid = 4242

            def kill(self):
                return None

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(win.subprocess, "Popen", lambda argv, **kw: FakeProc())
        monkeypatch.setattr(win, "process_start_time", lambda pid: tokens.get(pid, ""))
        monkeypatch.setattr(win, "pid_exists", lambda pid: pid in alive)
        monkeypatch.setattr(win, "apply_windows_resource_ceiling", lambda pid: True)
        monkeypatch.setattr(win, "resume_process_main_thread", lambda pid: True)
        monkeypatch.setattr(win, "record_supervised_pid", lambda c, n, p: None)
        monkeypatch.setattr(win, "clear_supervised_pid", lambda c, n: None)
        monkeypatch.setattr(win, "_begin_handoff", lambda c, n: True)
        monkeypatch.setattr(win, "_end_handoff", lambda c, n: None)
        monkeypatch.setattr(win, "_live_children_of", lambda pid, token: [])
        monkeypatch.setattr(
            win.run_marker,
            "read_pid_record_path",
            lambda path: (sidecar["pid"], sidecar["token"]),
        )
        monkeypatch.setattr(win.time, "sleep", lambda s: None)

        def _await(rec, pid, token):
            anchors.append((pid, token))
            return None

        monkeypatch.setattr(win, "_await_successor", _await)

        win.supervise_gateway(
            cfg,
            "demo",
            tmp_path / "kirocrew",
            ["gateway"],
            {},
            gateway_pid_record=tmp_path / "gw.pid",
        )

        assert anchors == [(4300, "2000")], (
            "the search must be anchored on the reaped GATEWAY from its own sidecar; "
            f"anchoring on the launcher hides the successor entirely (got {anchors})"
        )

    def test_a_marker_that_cannot_be_published_ends_the_successor(self, cfg, monkeypatch, tmp_path):
        """No marker means no protection, and an unprotected window must not be entered.

        Publication is allowed to fail without crashing the supervisor, but a
        failure must not read as a published marker: `handoff_in_progress` would
        report nothing in flight while the record still names a pid that is gone,
        so a concurrent `pod down` would `/Delete` the task and rmtree the isolated
        HOME under a booting successor. Losing an in-app restart is recoverable;
        losing the HOME under a live gateway is not — so the successor is ended and
        the adoption is never attempted.
        """
        killed: list[int] = []
        awaited: list[int] = []
        alive = {4242, 4300}
        tokens = {4242: "1000", 4300: "2000"}

        class FakeProc:
            pid = 4242

            def kill(self):
                return None

            def wait(self, timeout=None):
                alive.discard(4242)
                return 0

        def _no_marker(path, *a, **k):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(win.subprocess, "Popen", lambda argv, **kw: FakeProc())
        monkeypatch.setattr(win, "process_start_time", lambda pid: tokens.get(pid, ""))
        monkeypatch.setattr(win, "pid_exists", lambda pid: pid in alive)
        monkeypatch.setattr(win, "apply_windows_resource_ceiling", lambda pid: True)
        monkeypatch.setattr(win, "resume_process_main_thread", lambda pid: True)
        monkeypatch.setattr(win, "record_supervised_pid", lambda c, n, p: None)
        monkeypatch.setattr(win, "clear_supervised_pid", lambda c, n: None)
        monkeypatch.setattr(win, "_live_children_of", lambda pid, token: [4300])
        monkeypatch.setattr(win, "_terminate_unrecorded_pid", lambda pid, **k: killed.append(pid))

        # Returns a successor ONCE, then nothing: an adoption that is attempted at
        # all must still terminate, so a regression shows up as a failed assertion
        # rather than as a hung test.
        def _await(rec, pid, token):
            awaited.append(pid)
            return 4300 if len(awaited) == 1 else None

        monkeypatch.setattr(win, "_await_successor", _await)
        # Patched so that an adoption which IS attempted still finishes: the real
        # wait would poll a pid this test keeps alive, and a regression should read
        # as a failed assertion rather than as a hung suite.
        monkeypatch.setattr(win, "_wait_for_pid", lambda pid, token, **kw: alive.discard(pid))
        monkeypatch.setattr(win.Path, "write_text", _no_marker)
        monkeypatch.setattr(win.time, "sleep", lambda s: None)

        win.supervise_gateway(
            cfg,
            "demo",
            tmp_path / "kirocrew",
            ["gateway"],
            {},
            gateway_pid_record=tmp_path / "gw.pid",
        )

        assert awaited == [], (
            "with no marker published the adoption must not even be attempted; the "
            "window it would run in has no protection"
        )
        assert killed == [4300], (
            "the successor must be ended rather than left serving while a concurrent "
            "teardown is free to delete its HOME"
        )

    def test_no_marker_means_no_handoff(self, cfg):
        assert win.handoff_in_progress(cfg, "demo") is False
