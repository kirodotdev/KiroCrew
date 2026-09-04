"""Tests for the gateway.lock refusal diagnosis.

The point of the diagnosis is that the pid written INSIDE the lock file is not
necessarily the pid holding it. ``flock`` survives in a forked child after its
parent dies, so a crashed gateway can leave the home locked by a process the
file never names. Three real incidents were recovered by hand for exactly that
reason.

``test_flock_is_held_by_a_fork_orphan`` pins that limitation deliberately. It is
NOT a bug report against this module -- it documents the state the diagnosis
exists to explain, and it will start failing if someone swaps the primitive for a
POSIX record lock. That swap is unsafe here: record locks are keyed by
(process, inode), so any unrelated ``open()``/``close()`` of the lock path inside
the gateway -- including via its authenticated file-read endpoint -- would
silently release the guard and let a second gateway start.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not hasattr(os, "fork"), reason="fork-inheritance semantics are POSIX-only"
)

# Parent takes the lock, forks a child that wedges BEFORE exec while holding the
# inherited fd, then dies immediately -- the crashed-gateway shape, minimised.
# Run out-of-process so the parent's death is a real process death. The parent
# writes the wedged child's pid to a FILE (argv[3]) so the test can reap that
# exact process on every platform: a /proc sweep is Linux-only, and a stdout pipe
# would not do either, because the wedged child inherits the pipe and reading it
# would block for the child's whole lifetime instead of the parent's.
_ORPHANING_HOLDER = """
import os, sys, time
sys.path[:0] = {syspath!r}
from pathlib import Path
from kiro_crew.gateway_lock import GatewayLock
home, pid_file = Path(sys.argv[1]), sys.argv[2]
GatewayLock(home).acquire()
child = os.fork()
if child == 0:
    time.sleep(60)   # wedged pre-exec child, still holding the inherited fd
    os._exit(0)
with open(pid_file, "w") as fh:
    fh.write(str(child))
os._exit(0)          # the parent "crashes" while the child lives on
"""


@pytest.fixture
def reap():
    """Collects pids to SIGKILL at teardown, so no test leaks a wedged child."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _orphan_the_lock(home, reap: list[int]) -> int:
    """Leave *home* locked by a forked child whose parent has died.

    Returns (and registers for teardown) the wedged child's pid.
    """
    src = _ORPHANING_HOLDER.format(syspath=[p for p in sys.path if p])
    pid_file = home / "holder.pid"
    proc = subprocess.run(
        [sys.executable, "-c", src, str(home), str(pid_file)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    assert proc.returncode == 0, f"lock holder exited {proc.returncode}"
    pid = int(pid_file.read_text().strip())
    reap.append(pid)
    # The parent is reaped; the child keeps sleeping with the inherited fd.
    time.sleep(0.5)
    return pid


def test_flock_is_held_by_a_fork_orphan(tmp_path, reap):
    """The documented limitation: a dead parent's flock lives on in its child.

    Fails if the primitive is swapped for a POSIX record lock -- which would
    trade this loud, recoverable wedge for a silent loss of the guard.
    """
    from kiro_crew.gateway_lock import GatewayLock, GatewayLockError

    home = tmp_path / "home"
    home.mkdir()
    orphan = _orphan_the_lock(home, reap)

    # Platform-neutral: the lock is still held, so startup is refused.
    with pytest.raises(GatewayLockError) as excinfo:
        GatewayLock(home).acquire()

    # Identity claims need Linux /proc. On other platforms the diagnosis
    # correctly degrades to the recorded pid, so asserting identity there would
    # compare the dead parent against the surviving child and fail.
    if sys.platform != "linux":
        pytest.skip("holder identity needs /proc")
    from kiro_crew import platform_compat

    if platform_compat.pids_holding_file(home / "gateway.lock") is None:
        pytest.skip("/proc/<pid>/fd not readable here")

    # /proc/locks names the DEAD acquirer, never the inheritor -- that asymmetry
    # is the whole reason the message has to be written carefully.
    lock_path = home / "gateway.lock"
    if not Path("/proc/locks").exists():
        pytest.skip("/proc/locks unavailable")
    acquirer = platform_compat.flock_owner_pid(lock_path)
    # Asserted, NOT guarded: a None here means the owner lookup regressed, which
    # is exactly the failure this test exists to catch. Guarding it would let the
    # regression pass silently. (Filesystems with an anonymous s_dev, e.g. btrfs,
    # legitimately return None -- so this pins the behaviour on the ext4/xfs
    # tmp_path that CI and dev machines actually use.)
    assert acquirer is not None
    assert not platform_compat.pid_exists(acquirer)
    assert acquirer != orphan
    assert excinfo.value.holder_pid == acquirer
    # And the orphan is offered as the likely inheritor, by pid -- it is
    # single-threaded, reparented to init, and serving nothing.
    assert platform_compat.parent_pid(orphan) == 1
    assert f"kill -9 {orphan}" in str(excinfo.value)


def test_pids_holding_file_finds_the_real_holder(tmp_path):
    """Holder resolution comes from /proc, not from the pid inside the file."""
    if sys.platform != "linux":
        pytest.skip("/proc scanning is Linux-only")
    from kiro_crew import platform_compat

    path = tmp_path / "gateway.lock"
    path.write_text("999999\n")  # a pid that is not us
    fd = os.open(path, os.O_RDWR)
    try:
        if platform_compat.pids_holding_file(path) is None:
            pytest.skip("/proc/<pid>/fd not readable here")
        assert platform_compat.pids_holding_file(path) == [os.getpid()]
    finally:
        os.close(fd)
    # fd closed -> no holders, while the file still names the stale pid.
    assert platform_compat.pids_holding_file(path) == []
    assert path.read_text().strip() == "999999"


# --- refusal message rendering -------------------------------------------
#
# These drive the diagnosis directly (the lock is forced to appear taken) so the
# wording is pinned without needing a real second holder. The message is the
# whole point of the change: the old one named a pid that no longer existed.


@pytest.fixture
def refused_lock(monkeypatch, tmp_path):
    """A home whose lock always appears held, plus a stale pid on disk."""
    from kiro_crew import gateway_lock, platform_compat

    (tmp_path / gateway_lock.LOCK_FILENAME).write_text("4242\n", encoding="utf-8")
    monkeypatch.setattr(platform_compat, "try_acquire_lock", lambda *a, **k: False)
    return tmp_path


def _refusal(home, port=None):
    from kiro_crew.gateway_lock import GatewayLock, GatewayLockError

    with pytest.raises(GatewayLockError) as excinfo:
        GatewayLock(home, port=port).acquire()
    return excinfo.value


def test_refusal_names_the_live_acquirer_from_proc_locks(monkeypatch, refused_lock):
    """A live acquirer is authoritative: name it, and never suggest killing it."""
    from kiro_crew import gateway_lock, platform_compat

    monkeypatch.setattr(platform_compat, "flock_owner_pid", lambda _p: 16968)
    monkeypatch.setattr(platform_compat, "pid_exists", lambda _p: True)
    monkeypatch.setattr(platform_compat, "pids_holding_file", lambda _p: [16968])
    monkeypatch.setattr(platform_compat, "process_thread_count", lambda _p: 118)
    monkeypatch.setattr(platform_compat, "find_listening_pids", lambda _p: [16968])
    monkeypatch.setattr(gateway_lock, "_port_answers_http", lambda *_a, **_k: True)

    err = _refusal(refused_lock, port=5477)
    text = str(err)
    assert err.holder_pid == 16968  # from /proc/locks, NOT the 4242 on disk
    assert "118 threads" in text and "port 5477, answering HTTP" in text
    assert "records pid 4242 -- stale" in text
    assert "kill" not in text.lower()  # never offer up a live gateway


def test_refusal_names_the_dead_acquirer_and_the_single_inheritor(monkeypatch, refused_lock):
    """The wedge: acquirer dead, one opener, parent gone, serving nothing."""
    from kiro_crew import gateway_lock, platform_compat

    monkeypatch.setattr(platform_compat, "flock_owner_pid", lambda _p: 23184)
    monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(platform_compat, "pids_holding_file", lambda _p: [23185])
    monkeypatch.setattr(platform_compat, "process_thread_count", lambda _p: 1)
    monkeypatch.setattr(platform_compat, "parent_pid", lambda _p: 1)
    monkeypatch.setattr(platform_compat, "find_listening_pids", lambda _p: [23185])
    monkeypatch.setattr(gateway_lock, "_port_answers_http", lambda *_a, **_k: False)

    err = _refusal(refused_lock, port=5477)
    text = str(err)
    assert err.holder_pid == 23184  # the acquirer we can prove, even though dead
    assert "pid 23184) no longer exists" in text
    assert "inherited that descriptor" in text
    assert "parent (pid 1) is gone" in text and "kill -9 23185" in text


def test_refusal_withholds_kill_when_the_candidates_parent_is_alive(monkeypatch, refused_lock):
    """A live parent means this may be a gateway that just started: do not kill it.

    This is the shape a healthy starting gateway has -- one thread, no listener
    yet -- so the parent is the fact that discriminates it from an orphan.
    """
    from kiro_crew import gateway_lock, platform_compat

    monkeypatch.setattr(platform_compat, "flock_owner_pid", lambda _p: 23184)
    monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: pid == 9001)
    monkeypatch.setattr(platform_compat, "pids_holding_file", lambda _p: [23185])
    monkeypatch.setattr(platform_compat, "process_thread_count", lambda _p: 1)
    monkeypatch.setattr(platform_compat, "parent_pid", lambda _p: 9001)
    monkeypatch.setattr(platform_compat, "find_listening_pids", lambda _p: [])
    monkeypatch.setattr(gateway_lock, "_port_answers_http", lambda *_a, **_k: False)

    text = str(_refusal(refused_lock, port=5477))
    assert "parent is pid 9001, still alive" in text
    assert "just started and not yet bound its port" in text
    assert "kill -9" not in text


def test_refusal_withholds_kill_from_a_candidate_that_is_serving_http(monkeypatch, refused_lock):
    """Answering HTTP proves a live gateway, whatever /proc/locks says."""
    from kiro_crew import gateway_lock, platform_compat

    monkeypatch.setattr(platform_compat, "flock_owner_pid", lambda _p: 23184)
    monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(platform_compat, "pids_holding_file", lambda _p: [23185])
    monkeypatch.setattr(platform_compat, "process_thread_count", lambda _p: 1)
    monkeypatch.setattr(platform_compat, "parent_pid", lambda _p: 1)
    monkeypatch.setattr(platform_compat, "find_listening_pids", lambda _p: [23185])
    monkeypatch.setattr(gateway_lock, "_port_answers_http", lambda *_a, **_k: True)

    text = str(_refusal(refused_lock, port=5477))
    assert "IS serving HTTP" in text and "kirocrew stop" in text
    assert "kill -9" not in text


def test_refusal_refuses_to_guess_between_multiple_openers(monkeypatch, refused_lock):
    """Two openers -> ambiguous. Offer no kill command for either."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "flock_owner_pid", lambda _p: 23184)
    monkeypatch.setattr(platform_compat, "pid_exists", lambda _p: False)
    monkeypatch.setattr(platform_compat, "pids_holding_file", lambda _p: [23185, 30001])
    monkeypatch.setattr(platform_compat, "process_thread_count", lambda _p: 1)
    monkeypatch.setattr(platform_compat, "find_listening_pids", lambda _p: [])

    text = str(_refusal(refused_lock, port=5477))
    assert "pid 23185" in text and "pid 30001" in text
    assert "ambiguous" in text
    assert "kill -9" not in text  # an opener is not proof of ownership


def test_refusal_degrades_honestly_without_proc_locks(monkeypatch, refused_lock):
    """No /proc/locks (non-Linux): fall back to the recorded pid, say it may be stale."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "flock_owner_pid", lambda _p: None)
    monkeypatch.setattr(platform_compat, "pids_holding_file", lambda _p: None)

    err = _refusal(refused_lock, port=5477)
    text = str(err)
    assert err.holder_pid == 4242
    assert "could not be identified" in text
    assert "may be stale" in text
    assert "kill" not in text.lower()  # we cannot name anyone -- do not guess
