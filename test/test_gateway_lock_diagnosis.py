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

Owner diagnosis is best effort. Some kernels keep the inherited flock while
dropping the dead acquirer from the child's fdinfo record (a blank ``lock:``
row, or owner 0) and omitting it from /proc/locks. The orphan test reads that
kernel evidence itself, whole and unfiltered, and only a positively observed
populated-then-discarded acquirer permits the honest unknown-owner refusal.
Kernels that retain the acquirer's PID must name it exactly. With
``KIROCREW_LOCK_OWNER_STRICT=1`` the discarded branch is refused outright: the
hosted lane that sets it exists to prove the named-owner diagnosis on a kernel
that can show it, so an environmental pass there is a failure.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_gateway_lock import _lock_failure_evidence, kernel_lock_home  # noqa: F401

pytestmark = pytest.mark.skipif(
    not hasattr(os, "fork"), reason="fork-inheritance semantics are POSIX-only"
)

_STRICT_ENV = "KIROCREW_LOCK_OWNER_STRICT"

# Parent takes the lock, forks a child that wedges BEFORE exec while holding the
# inherited fd, publishes the child's pid, then holds at a bounded handshake so
# the test can read the LIVE acquirer's kernel record first. Once the release
# file appears it dies -- the crashed-gateway shape, minimised. Run
# out-of-process so the parent's death is a real process death. The child's pid
# travels through a FILE (argv[2]) so the test can reap that exact process on
# every platform: a /proc sweep is Linux-only, and a stdout pipe would not do
# either, because the wedged child inherits the pipe and reading it would block
# for the child's whole lifetime instead of the parent's.
_ORPHANING_HOLDER = """
import os, sys, time
sys.path[:0] = {syspath!r}
from pathlib import Path
from kiro_crew.gateway_lock import GatewayLock
home, pid_file, release = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
GatewayLock(home).acquire()
child = os.fork()
if child == 0:
    time.sleep(60)   # wedged pre-exec child, still holding the inherited fd
    os._exit(0)
staged = pid_file.with_name(pid_file.name + ".tmp")
staged.write_text(str(child))
os.replace(staged, pid_file)
deadline = time.monotonic() + 30
while not release.exists():
    if time.monotonic() > deadline:
        os._exit(3)
    time.sleep(0.02)
os._exit(0)          # the parent "crashes" while the child lives on
"""


def _strict() -> bool:
    return os.environ.get(_STRICT_ENV) == "1"


def _step_aside(reason: str) -> None:
    """A skip in ordinary runs; strict mode has no environment to step aside for."""
    if _strict():
        pytest.fail(f"{_STRICT_ENV}=1 forbids stepping aside: {reason}")
    pytest.skip(reason)


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


@dataclass
class _OrphaningHolder:
    home: Path
    parent: subprocess.Popen
    child: int
    release: Path

    @property
    def lock_path(self) -> Path:
        return self.home / "gateway.lock"

    def crash_parent(self) -> None:
        """Let the parent exit and reap it; the child keeps the inherited fd."""
        self.release.touch()
        assert self.parent.wait(timeout=30) == 0, f"lock holder exited {self.parent.returncode}"


def _start_orphaning_holder(home: Path, reap: list[int]) -> _OrphaningHolder:
    """Lock *home* in a parent that waits, with a wedged child holding the fd.

    Returns once the child's pid is published; the parent is still alive and
    still the kernel's acquirer. Registers the child for teardown.
    """
    src = _ORPHANING_HOLDER.format(syspath=[p for p in sys.path if p])
    pid_file = home / "holder.pid"
    release = home / "release"
    proc = subprocess.Popen(
        [sys.executable, "-c", src, str(home), str(pid_file), str(release)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    while not pid_file.exists():
        if proc.poll() is not None:
            pytest.fail(f"lock holder exited {proc.returncode} before publishing its child")
        if time.monotonic() > deadline:
            proc.kill()
            proc.wait(timeout=10)
            pytest.fail("lock holder did not publish its child within 30s")
        time.sleep(0.02)
    child = int(pid_file.read_text().strip())
    reap.append(child)
    return _OrphaningHolder(home=home, parent=proc, child=child, release=release)


@pytest.fixture
def orphaning_holder(kernel_lock_home, reap):  # noqa: F811 -- the imported fixture, by name
    home = kernel_lock_home / "home"
    home.mkdir()
    holder = _start_orphaning_holder(home, reap)
    yield holder
    if holder.parent.poll() is None:
        holder.parent.kill()
        holder.parent.wait(timeout=10)


def _device_inode_key(path: Path) -> str:
    info = path.stat()
    return f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"


def _flock_rows(rows: list[str], key: str) -> list[list[str]]:
    """``/proc/locks`` FLOCK owner rows for *key*; blocked waiters own nothing."""
    kept = []
    for row in rows:
        fields = row.split()
        if (
            len(fields) >= 6
            and "->" not in fields[:2]
            and fields[1] == "FLOCK"
            and fields[5] == key
        ):
            kept.append(fields)
    return kept


def _kernel_lock_records(lock_path: Path, orphan: int) -> dict:
    """Every kernel record for this lock file's inode, read whole and unfiltered.

    The orphan's descriptors are matched to the lock file by ``(st_dev,
    st_ino)``; each matching descriptor's complete fdinfo lock rows are kept,
    and the whole of ``/proc/locks`` is scanned for the inode key with no owner
    filter. Each read that fails is recorded under ``errors``: the classifier
    treats a recorded error as a measurement that did not happen, never as
    absence.
    """
    key = _device_inode_key(lock_path)
    info = lock_path.stat()
    records: dict = {"key": key, "orphan": orphan, "fdinfo": [], "proc_locks": [], "errors": []}
    fd_dir = Path(f"/proc/{orphan}/fd")
    try:
        descriptors = sorted(fd_dir.iterdir())
    except OSError as error:
        records["errors"].append(f"{fd_dir}: {error!r}")
        descriptors = []
    for fd in descriptors:
        try:
            opened = fd.stat()
        except OSError as error:
            records["errors"].append(f"{fd}: {error!r}")
            continue
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            continue
        fdinfo = Path(f"/proc/{orphan}/fdinfo/{fd.name}")
        try:
            text = fdinfo.read_text(encoding="utf-8")
        except OSError as error:
            records["errors"].append(f"{fdinfo}: {error!r}")
            continue
        rows = [row for row in text.splitlines() if row.startswith("lock:")]
        records["fdinfo"].append({"fd": fd.name, "rows": rows})
    try:
        rows = Path("/proc/locks").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        records["errors"].append(f"/proc/locks: {error!r}")
    else:
        records["proc_locks"] = [" ".join(fields) for fields in _flock_rows(rows, key)]
    return records


def _classify_kernel_records(records: dict, parent: int) -> str:
    """``"retained"`` or ``"discarded"``; anything else is a failed measurement.

    Retained: the orphan's one descriptor on the lock file carries one FLOCK
    record naming the dead parent on the exact inode key, and ``/proc/locks``
    names that same parent for the key and nobody else.

    Discarded: that one record is blank (``lock:`` with nothing after it) or
    names owner 0 on the exact key, and ``/proc/locks`` has no FLOCK owner row
    for the key at all. A descriptor with no lock record, a record on another
    inode, a record naming anyone else, a disagreement between the two
    surfaces, or any read error is neither -- the caller fails with the records.
    """
    detail = json.dumps(records, sort_keys=True)
    if records["errors"]:
        pytest.fail(f"kernel lock records could not be read completely: {detail}")
    if len(records["fdinfo"]) != 1:
        pytest.fail(f"expected exactly one orphan descriptor on the lock file: {detail}")
    rows = records["fdinfo"][0]["rows"]
    if len(rows) != 1:
        pytest.fail(f"expected exactly one lock record on the inherited descriptor: {detail}")
    fields = rows[0].split()
    key = records["key"]
    owners = [row.split()[4] for row in records["proc_locks"]]
    populated = (
        len(fields) >= 7 and fields[2:5] == ["FLOCK", "ADVISORY", "WRITE"] and fields[6] == key
    )
    if populated and fields[5] == str(parent):
        if owners == [str(parent)]:
            return "retained"
        pytest.fail(f"fdinfo names the acquirer but /proc/locks disagrees: {detail}")
    if fields == ["lock:"] or (populated and fields[5] == "0"):
        if not owners:
            return "discarded"
        pytest.fail(f"/proc/locks names an owner the descriptor record lacks: {detail}")
    pytest.fail(f"unrecognised lock record on the inherited descriptor: {detail}")


def _expected_acquirer(verdict: str, parent: int, *, strict: bool) -> int | None:
    """What the production lookup must report for *verdict*.

    Strict mode accepts only a retained acquirer: the lane that sets it runs on
    a kernel expected to name the dead parent, so a discarded record there is a
    red, not a different expectation.
    """
    if strict and verdict != "retained":
        pytest.fail(f"{_STRICT_ENV}=1 requires the kernel to name the dead acquirer; got {verdict}")
    return parent if verdict == "retained" else None


def _assert_independent_contention(lock_path: Path) -> None:
    """A fresh open file description cannot take the flock: the real errno."""
    import fcntl

    fd = os.open(lock_path, os.O_RDWR)
    try:
        with pytest.raises(OSError) as excinfo:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert excinfo.value.errno in (errno.EWOULDBLOCK, errno.EAGAIN), excinfo.value
    finally:
        os.close(fd)


def _run_orphan_scenario(holder: _OrphaningHolder, *, strict: bool) -> str:
    """The two-phase protocol; returns the kernel verdict it observed."""
    from kiro_crew import platform_compat
    from kiro_crew.gateway_lock import (
        GatewayLock,
        GatewayLockError,
        LockHolder,
        LockProbeError,
        lock_holder,
    )

    home, lock_path, parent, orphan = holder.home, holder.lock_path, holder.parent.pid, holder.child

    # Phase one: the acquirer is alive. Startup is refused everywhere; on Linux
    # the kernel names the live acquirer and so must the production lookup.
    with pytest.raises(GatewayLockError):
        GatewayLock(home).acquire()
    if sys.platform == "linux":
        key = _device_inode_key(lock_path)
        try:
            rows = Path("/proc/locks").read_text(encoding="utf-8").splitlines()
        except OSError as error:
            _step_aside(f"/proc/locks unreadable: {error!r}")
        live = [fields[4] for fields in _flock_rows(rows, key)]
        assert live == [str(parent)], f"kernel record for the live acquirer: {live}"
        assert platform_compat.flock_owner_pid(lock_path) == parent, "live acquirer lookup"
        assert lock_holder(home) == LockHolder(pid=parent, alive=True, source="flock_owner")

    # Phase two: the parent dies and is reaped; the child keeps the descriptor.
    holder.crash_parent()
    _assert_independent_contention(lock_path)
    with pytest.raises(GatewayLockError) as excinfo:
        GatewayLock(home).acquire()

    # Identity claims need Linux /proc. On other platforms the diagnosis
    # correctly degrades to the recorded pid, so asserting identity there would
    # compare the dead parent against the surviving child and fail.
    if sys.platform != "linux":
        _step_aside("holder identity needs /proc")
    if platform_compat.pids_holding_file(lock_path) is None:
        _step_aside("/proc/<pid>/fd not readable here")

    records = _kernel_lock_records(lock_path, orphan)
    verdict = _classify_kernel_records(records, parent)
    acquirer = platform_compat.flock_owner_pid(lock_path)
    assert acquirer == _expected_acquirer(verdict, parent, strict=strict), (
        f"production lookup {acquirer!r} disagrees with the kernel records "
        f"({verdict}): {json.dumps(records, sort_keys=True)}"
    )
    text = str(excinfo.value)
    recorded = int(lock_path.read_text().strip())
    assert recorded == parent
    assert not platform_compat.pid_exists(parent)
    assert excinfo.value.holder_pid == parent
    if verdict == "retained":
        # /proc/locks names the DEAD acquirer, never the inheritor -- and the
        # orphan is offered as the likely inheritor, by pid: it is
        # single-threaded, reparented to init, and serving nothing.
        assert acquirer != orphan
        assert platform_compat.parent_pid(orphan) == 1
        assert f"kill -9 {orphan}" in text, _lock_failure_evidence(lock_path, (orphan,))
        with pytest.raises(LockProbeError, match=f"pid {parent}"):
            lock_holder(home)
        return verdict
    assert f"the pid it records ({recorded}) no longer exists" in text
    assert "inherited that descriptor" in text
    assert "may be stale" not in text
    assert "held by pid" not in text
    assert "kill" not in text.lower()
    # Held by a process nothing can name: indeterminate, never "nobody".
    with pytest.raises(LockProbeError):
        lock_holder(home)
    return verdict


def test_flock_is_held_by_a_fork_orphan(orphaning_holder):
    """The documented limitation: a dead parent's flock lives on in its child.

    Fails if the primitive is swapped for a POSIX record lock -- which would
    trade this loud, recoverable wedge for a silent loss of the guard. The
    owner lookup is asserted, not guarded: the live phase pins the named
    acquirer on every Linux kernel, and after the parent's death only the
    kernel's own complete records decide whether the dead acquirer must still
    be named or the honest unknown-owner refusal is due.
    """
    _run_orphan_scenario(orphaning_holder, strict=_strict())


@pytest.mark.skipif(sys.platform != "linux", reason="the owner lookup reads /proc")
def test_a_lookup_that_never_names_the_acquirer_fails_while_it_is_alive(
    orphaning_holder, monkeypatch
):
    """The always-None mutant is caught in phase one, on every kernel."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "flock_owner_pid", lambda _p: None)
    with pytest.raises(AssertionError, match="live acquirer lookup"):
        _run_orphan_scenario(orphaning_holder, strict=False)
    assert orphaning_holder.parent.poll() is None, "phase one must fail before the parent dies"


@pytest.mark.skipif(sys.platform != "linux", reason="the owner lookup reads /proc")
def test_a_lookup_that_forgets_a_dead_acquirer_fails_the_strict_lane(orphaning_holder, monkeypatch):
    """The dead-only mutant passes phase one and must be caught after the reap.

    On a kernel that retains the acquirer the mutant's ``None`` disagrees with
    the kernel record; on one that discards it strict mode refuses the verdict
    itself. Either way the strict lane is red.
    """
    from kiro_crew import platform_compat

    real = platform_compat.flock_owner_pid

    def dead_only(path):
        pid = real(path)
        return pid if pid is not None and platform_compat.pid_exists(pid) else None

    monkeypatch.setattr(platform_compat, "flock_owner_pid", dead_only)
    with pytest.raises((AssertionError, pytest.fail.Exception)) as excinfo:
        _run_orphan_scenario(orphaning_holder, strict=True)
    assert orphaning_holder.parent.poll() is not None, "the mutant must survive phase one"
    assert "production lookup None" in str(excinfo.value) or _STRICT_ENV in str(excinfo.value)


@pytest.mark.parametrize("strict", [False, True])
def test_step_aside_skips_only_outside_strict_mode(monkeypatch, strict):
    if strict:
        monkeypatch.setenv(_STRICT_ENV, "1")
        with pytest.raises(pytest.fail.Exception, match="forbids stepping aside"):
            _step_aside("no /proc here")
    else:
        monkeypatch.delenv(_STRICT_ENV, raising=False)
        with pytest.raises(pytest.skip.Exception):
            _step_aside("no /proc here")


_KEY = "00:29:140371"
_PARENT = 4242
_RETAINED_ROW = f"lock:\t1: FLOCK ADVISORY WRITE {_PARENT} {_KEY} 0 EOF"
_PROC_LOCKS_ROW = f"1: FLOCK ADVISORY WRITE {_PARENT} {_KEY} 0 EOF"


def _records(fdinfo_rows, proc_locks=(), errors=()):
    return {
        "key": _KEY,
        "orphan": 4243,
        "fdinfo": [{"fd": "3", "rows": list(rows)} for rows in fdinfo_rows],
        "proc_locks": list(proc_locks),
        "errors": list(errors),
    }


@pytest.mark.parametrize(
    "records, verdict",
    [
        (_records([[_RETAINED_ROW]], [_PROC_LOCKS_ROW]), "retained"),
        (_records([["lock:\t"]]), "discarded"),
        (_records([[f"lock:\t1: FLOCK ADVISORY WRITE 0 {_KEY} 0 EOF"]]), "discarded"),
    ],
)
def test_classifier_accepts_only_a_positively_observed_record(records, verdict):
    assert _classify_kernel_records(records, _PARENT) == verdict


@pytest.mark.parametrize(
    "records, reason",
    [
        pytest.param(_records([]), "one orphan descriptor", id="no-descriptor"),
        pytest.param(_records([[]]), "one lock record", id="open-but-unlocked"),
        pytest.param(_records([["lock:\t"], ["lock:\t"]]), "one orphan descriptor", id="two-fds"),
        pytest.param(
            _records([["lock:\t1: FLOCK ADVISORY WRITE 0 ff:ff:1 0 EOF"]]),
            "unrecognised",
            id="owner-0-on-another-inode",
        ),
        pytest.param(
            _records([[f"lock:\t1: FLOCK ADVISORY WRITE {_PARENT} ff:ff:1 0 EOF"]]),
            "unrecognised",
            id="acquirer-on-another-inode",
        ),
        pytest.param(
            _records([[f"lock:\t1: FLOCK ADVISORY WRITE 9999 {_KEY} 0 EOF"]]),
            "unrecognised",
            id="someone-else",
        ),
        pytest.param(
            _records([[f"lock:\t1: POSIX ADVISORY WRITE {_PARENT} {_KEY} 0 EOF"]]),
            "unrecognised",
            id="record-lock-not-flock",
        ),
        pytest.param(_records([[_RETAINED_ROW]]), "disagrees", id="fdinfo-only"),
        pytest.param(_records([["lock:\t"]], [_PROC_LOCKS_ROW]), "lacks", id="blank-but-listed"),
        pytest.param(
            _records([[_RETAINED_ROW]], [_PROC_LOCKS_ROW, _PROC_LOCKS_ROW.replace("4242", "77")]),
            "disagrees",
            id="second-owner",
        ),
        pytest.param(
            _records([["lock:\t"]], errors=["/proc/locks: PermissionError()"]),
            "could not be read",
            id="unreadable",
        ),
        pytest.param(
            _records([[_RETAINED_ROW, "lock:\t"]], [_PROC_LOCKS_ROW]),
            "one lock record",
            id="two-records",
        ),
    ],
)
def test_classifier_fails_closed_on_evidence_that_qualifies_neither_branch(records, reason):
    with pytest.raises(pytest.fail.Exception, match=reason):
        _classify_kernel_records(records, _PARENT)


def test_flock_rows_scan_the_whole_listing_without_an_owner_filter():
    rows = [
        _PROC_LOCKS_ROW,
        f"2: -> FLOCK ADVISORY WRITE 5 {_KEY} 0 EOF",
        f"3: FLOCK ADVISORY WRITE 77 {_KEY} 0 EOF",
        "4: FLOCK ADVISORY WRITE 8 ff:ff:1 0 EOF",
        f"5: POSIX ADVISORY WRITE 9 {_KEY} 0 EOF",
        "garbage",
    ]
    assert [fields[4] for fields in _flock_rows(rows, _KEY)] == [str(_PARENT), "77"]


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("verdict", ["retained", "discarded"])
def test_expected_acquirer_names_a_retained_owner_and_refuses_a_strict_discard(verdict, strict):
    if strict and verdict == "discarded":
        with pytest.raises(pytest.fail.Exception, match=_STRICT_ENV):
            _expected_acquirer(verdict, _PARENT, strict=True)
        return
    expected = _expected_acquirer(verdict, _PARENT, strict=strict)
    assert expected == (_PARENT if verdict == "retained" else None)


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
# wording is pinned without needing a real second holder. The message must name a
# pid that actually holds the lock, never a stale one read from the pid file.


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


# --- no owner surface: the macOS and Windows shape ------------------------
#
# ``flock_owner_pid`` and ``pids_holding_file`` both read ``/proc`` and return
# ``None`` off Linux, and macOS has no way to identify an flock owner at all:
# ``F_GETLK`` reports ``l_pid = -1`` for a conflicting flock and ``lsof`` leaves
# the lock field blank. Naming the owner is not what the operator needs, though.
# Liveness and port ownership work everywhere, so the recorded pid's own facts
# decide which of four things the refusal says.


@pytest.mark.parametrize("openers", [None, [23185]])
def test_refusal_keeps_the_hedge_when_there_is_no_port_to_weigh(monkeypatch, refused_lock, openers):
    """No owner surface and no port supplied: the recorded pid stays a hedge.

    This is the ``--port auto`` / ``--slack-only`` shape. Even a known opener
    cannot establish ownership, because an opener may merely have read the file.
    """
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "flock_owner_pid", lambda _p: None)
    monkeypatch.setattr(platform_compat, "pids_holding_file", lambda _p: openers)
    monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: pid == 4242)

    err = _refusal(refused_lock)
    text = str(err)
    assert err.holder_pid == 4242
    assert "could not be identified" in text
    assert "may be stale" in text
    assert "kill" not in text.lower()  # we cannot name anyone -- do not guess


def test_refusal_names_a_live_port_holding_pid_without_an_owner_surface(monkeypatch, refused_lock):
    """Running and holding the dashboard port: that is the gateway, so say so."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "flock_owner_pid", lambda _p: None)
    monkeypatch.setattr(platform_compat, "pids_holding_file", lambda _p: None)
    monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: pid == 4242)
    monkeypatch.setattr(platform_compat, "find_listening_pids", lambda _p: [4242])

    err = _refusal(refused_lock, port=5477)
    text = str(err)
    assert err.holder_pid == 4242
    assert "held by pid 4242" in text
    assert "running and holds port 5477" in text
    assert "may be stale" not in text  # sending the operator after a phantom
    assert "kirocrew stop" in text
    assert "kill" not in text.lower()


def test_refusal_hedges_when_the_recorded_pid_holds_no_port(monkeypatch, refused_lock):
    """Running but portless: name the missing fact instead of deciding either way."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "flock_owner_pid", lambda _p: None)
    monkeypatch.setattr(platform_compat, "pids_holding_file", lambda _p: None)
    monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: pid == 4242)
    monkeypatch.setattr(platform_compat, "find_listening_pids", lambda _p: [])

    text = str(_refusal(refused_lock, port=5477))
    assert "does not hold port 5477" in text
    assert "reused the number" in text
    assert "kill" not in text.lower()


def test_refusal_reports_an_inherited_lock_when_the_recorded_pid_is_gone(monkeypatch, refused_lock):
    """A dead recorded pid is not a stale number -- something inherited the fd."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "flock_owner_pid", lambda _p: None)
    monkeypatch.setattr(platform_compat, "pids_holding_file", lambda _p: None)
    monkeypatch.setattr(platform_compat, "pid_exists", lambda _p: False)

    text = str(_refusal(refused_lock, port=5477))
    assert "no longer exists" in text
    assert "inherited that descriptor" in text
    assert "may be stale" not in text
    assert "lsof " in text
    assert "kill" not in text.lower()
