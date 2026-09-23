"""Write ownership of a session log -- who may append, and who is refused.

The hazard being pinned: a resume opens a session log with ``repair=True``
while a writer is still live, so the file states that the turn completed as
interrupted and then, further down, completed for real, with one tool call closed
both ``unknown`` and ``completed``. A fold reads two outcomes for one turn and
cannot tell which happened.

Two populations, tested differently. Another PROCESS is a real subprocess, because
that is the case no in-process fixture reproduces. Another DESCRIPTOR is enough
for the rest: an advisory lock belongs to an open file description, so a second
descriptor in this process contends exactly as another process would, which is
also why ownership is refcounted rather than held per handle.
"""

from __future__ import annotations

import contextlib
import gc
import json
import os
import select
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, CrewLogError
from kiro_crew.crew_log.lease import LEASE_FILE
from kiro_crew.platform_compat import file_lock

CHILD = Path(__file__).parent / "fixtures" / "crew_log_lease_child.py"
SESSION = "lease-owner-session"
#: Only an upper bound on a failure. Every wait here returns as soon as the child
#: answers on its pipe, so a passing run does not spend it.
CHILD_TIMEOUT = 60.0

# Windows is skipped for the same reason the real-crash suite is: the property
# asserted by the subprocess tests is the kernel releasing a lock when a process
# ends, and that is asserted on the platforms whose lock semantics this suite can
# reason about. macOS is skipped as the same bisect step the real-crash suite
# documents -- a shard running spawned children there is being isolated from an
# unrelated provisioning race. That darwin half is SCOPED to
# kirodotdev/KiroCrew#10704, which owns removing it: cross-process ownership is
# asserted nowhere else against a second process, so an expiry-free skip would mean
# a macOS regression in it has no test that would catch it.
needs_real_processes = pytest.mark.skipif(
    sys.platform in ("win32", "darwin"),
    reason=(
        "win32: lock semantics differ; "
        "darwin: bisecting shard interference, tracked in kirodotdev/KiroCrew#10704"
    ),
)
locked_lease_swap_unavailable = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows refuses to replace or rename a file while another handle holds it open, "
        "so this locked-file swap cannot be arranged there"
    ),
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _session(unit_id: str = SESSION) -> CrewLog:
    return CrewLog.create(lg.KIND_SESSION, unit_id, owner="qa", agent="kirocrew")


def _types(unit_id: str = SESSION) -> "list[str]":
    """The entry types on disk, read without opening a handle.

    Read as bytes rather than through ``CrewLog``: a handle would be one more
    object whose lifetime affects ownership, which is the thing under test.
    """
    raw = lg.crew_log_path(lg.KIND_SESSION, unit_id).read_text(encoding="utf-8")
    return [json.loads(line)["type"] for line in raw.splitlines()[1:] if line.strip()]


class _Foreign:
    """Another owner of one crew log's writes, holding its own descriptor.

    Stands in for a second process wherever the property does not need one. The
    lock is taken NON-BLOCKING, so a failure to take it is reported here rather
    than turning a test into a wait.
    """

    def __init__(self, unit_id: str = SESSION) -> None:
        self._path = lg.crew_log_dir(lg.KIND_SESSION, unit_id) / LEASE_FILE
        self._stack: "contextlib.ExitStack | None" = None

    def take(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)
        stack = contextlib.ExitStack()
        try:
            handle = stack.enter_context(self._path.open("r+"))
            stack.enter_context(
                file_lock(handle.fileno(), exclusive=True, required=True, wait=False)
            )
        except BaseException:
            stack.close()
            raise
        self._stack = stack

    def can_take(self) -> bool:
        """Whether this owner could take the lock right now. Does not keep it."""
        try:
            self.take()
        except OSError:
            return False
        self.give_up()
        return True

    def give_up(self) -> None:
        if self._stack is not None:
            self._stack.close()
            self._stack = None


# --- another descriptor ----------------------------------------------------


def test_an_append_is_refused_while_another_owner_holds_the_log():
    """The refusal is a code a caller can branch on, and nothing is written.

    The file is asserted BEFORE the code, so a claim removed from ``append`` is
    reported as the entry it let through rather than as a missing exception.

    Mutation guard: dropping the claim from ``append`` lets the entry land and
    reddens the first assertion below.
    """
    handle = _session()
    before = _types()
    foreign = _Foreign()
    foreign.take()
    refused = None
    try:
        handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    except CrewLogError as exc:
        refused = exc.code
    finally:
        foreign.give_up()
    assert _types() == before, f"an append reached a log this process does not own: {_types()}"
    assert refused == lg.CODE_ALREADY_OWNED


def test_ownership_is_taken_once_the_owner_releases_it():
    """A refusal is about the moment, not the handle: the same handle then writes."""
    handle = _session()
    foreign = _Foreign()
    foreign.take()
    with pytest.raises(CrewLogError):
        handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    foreign.give_up()
    handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    assert _types() == ["turn/started"]


def test_a_read_only_open_takes_no_write_ownership():
    """Readers do not contend. ``open``, ``iter_from`` and ``page`` claim nothing.

    Mutation guard: claiming in ``open`` instead of on the first write reddens
    this, which is the regression that would make every reader wait on the writer.
    """
    writer = _session()
    writer.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    del writer
    gc.collect()

    reader = CrewLog.open(lg.KIND_SESSION, SESSION)
    assert [e.type for e in reader.iter_from(1)] == ["turn/started"]
    assert reader.page().entries
    assert _Foreign().can_take(), "a reader is holding write ownership"


def test_two_handles_in_one_process_share_one_ownership():
    """A second handle is not contention, and the first to go does not release it.

    Two handles for one session is the ordinary case, not an edge: the emitter
    holds a cached handle while a session claim opens its own, and both append.
    Asserted through the kernel rather than through a counter -- a foreign owner
    can take the lock only once BOTH handles are gone.
    """
    first = _session()
    first.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    second = CrewLog.open(lg.KIND_SESSION, SESSION)
    # No refusal: the two share one lock through the reference count.
    second.append("message/chunk", {"turn": 1, "delta": "hi"}, src="acp", ignorable=True)
    assert not _Foreign().can_take(), "two live handles are not holding ownership"

    del first
    gc.collect()
    assert not _Foreign().can_take(), "the surviving handle lost ownership with its peer"

    del second
    gc.collect()
    assert _Foreign().can_take(), "ownership outlived every handle that held it"


def test_a_release_fired_by_the_collector_inside_an_acquire_does_not_deadlock(monkeypatch):
    """A finalizer's release may run on the thread that is inside ``acquire``.

    Ownership is released by ``weakref.finalize`` when a handle is dropped. A
    handle that is only reachable through a reference cycle is freed by the
    cyclic collector, which runs on whatever thread trips the allocation
    threshold -- including one that is inside ``acquire`` for ANOTHER unit, with
    the module lock held. With a non-reentrant lock that release blocks on the
    lock its own thread holds, and the process hangs there until something kills
    it. CI saw exactly that: a Windows shard's worker stuck in ``_take`` with the
    finalizer's ``release`` at the top of the stack, reported as a crashed worker.

    The collector is driven by hand at the one point that matters -- inside
    ``_take``, under the lock -- and automatic collection is off for the window,
    so the interleaving is the test's, not the allocator's. The handle whose
    release fires is a real ``CrewLog`` that claimed ownership through an append,
    parked in a cycle so only the collector can free it.

    Mutation guard: making the lock a plain ``threading.Lock`` hangs the acquire
    below, and the bounded wait reports it.
    """
    from kiro_crew.crew_log import lease

    class Cycle:
        pass

    collected_key = str(lg.crew_log_dir(lg.KIND_SESSION, "collected-owner") / LEASE_FILE)
    owner = _session("collected-owner")
    owner.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    assert collected_key in lease._held
    cycle = Cycle()
    cycle.handle = owner
    cycle.me = cycle
    del owner, cycle

    was_enabled = gc.isenabled()
    gc.disable()  # nothing frees the cycle before the point chosen below
    real_take = lease._take
    released_under_lock = threading.Event()

    def take_with_collection(path):
        gc.collect()  # frees the cycle here, while acquire() holds lease._lock
        if collected_key not in lease._held:
            released_under_lock.set()
        return real_take(path)

    monkeypatch.setattr(lease, "_take", take_with_collection)

    kept: "list[CrewLog]" = []  # keeps the other unit's handle, and so its ownership
    done = threading.Event()

    def other_owner():
        handle = _session("other-unit")
        handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
        kept.append(handle)
        done.set()

    worker = threading.Thread(target=other_owner, name="lease-acquirer", daemon=True)
    worker.start()
    try:
        completed = done.wait(10.0)
        if not completed:
            # Unstick the worker so the rest of this process does not inherit the
            # deadlock: a plain Lock can be released from any thread, and once it
            # is, the finalizer finishes and the acquire runs to completion.
            with contextlib.suppress(RuntimeError):
                lease._lock.release()
            worker.join(5.0)
    finally:
        if was_enabled:
            gc.enable()
    assert (
        released_under_lock.is_set()
    ), "the release never fired inside acquire; nothing was proven"
    assert completed, "acquire deadlocked on a release the collector fired on its own thread"
    assert _Foreign("collected-owner").can_take(), "the collected handle kept its ownership"
    assert not _Foreign("other-unit").can_take(), "the acquire that completed owns nothing"


def test_a_repair_is_refused_while_another_owner_holds_the_log():
    """The repair path claims ownership too, and writes no closer when refused.

    ``open(repair=True)`` appends closers, so it is a writer. Refused, it raises
    and the open turn is left exactly as the live writer has it.
    """
    handle = _session()
    handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    handle.append(
        "tool/called",
        {"turn": 1, "call_id": "tc-1", "name": "fs_write", "server": "", "kind": ""},
        src="acp",
    )
    del handle
    gc.collect()

    foreign = _Foreign()
    foreign.take()
    refused = None
    try:
        CrewLog.open(lg.KIND_SESSION, SESSION, repair=True)
    except CrewLogError as exc:
        refused = exc.code
    finally:
        foreign.give_up()
    types = _types()
    assert types == ["turn/started", "tool/called"], f"a refused repair closed the turn: {types}"
    assert refused == lg.CODE_ALREADY_OWNED


# --- the locked inode is the file at the path -------------------------------


def _replacing_fstat(path: Path, limit: int):
    """An ``os.fstat`` that replaces the file at *path* for its first *limit* stats.

    Narrow on purpose: it acts only when the descriptor being stat'd IS the file
    at *path*, so nothing else in the process is affected. Replacing it there
    reproduces the one case the inode check exists for -- a lock held on an inode
    that differs from the file standing at the lock's own path.
    """
    real = os.fstat
    seen = {"n": 0}

    def _fstat(fd, *args, **kwargs):
        result = real(fd, *args, **kwargs)
        try:
            standing = path.stat().st_ino
        except OSError:
            return result
        if seen["n"] < limit and result.st_ino == standing:
            seen["n"] += 1
            path.unlink()
            path.touch()
        return result

    return _fstat


@locked_lease_swap_unavailable
def test_a_lock_whose_inode_moved_is_taken_again_on_the_file_that_stands():
    """The lock has to name the file a writer is about to append to.

    A lock belongs to an inode, so one taken on a path that was replaced under it
    guards an orphan and excludes nobody. Reaching for it again against whatever
    now stands there is what makes the lock mean something.
    """
    handle = _session()
    path = lg.crew_log_dir(lg.KIND_SESSION, SESSION) / LEASE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "fstat", _replacing_fstat(path, 1))
        handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    assert _types() == ["turn/started"]


@locked_lease_swap_unavailable
def test_a_lease_file_that_keeps_moving_is_refused_rather_than_trusted():
    """Failing closed: an unverifiable lock is not ownership.

    Mutation guard: accepting the lock without comparing the inode reddens this,
    which is the state in which one process believes it owns a log another can
    write at the same time.
    """
    handle = _session()
    path = lg.crew_log_dir(lg.KIND_SESSION, SESSION) / LEASE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "fstat", _replacing_fstat(path, 99))
        with pytest.raises(CrewLogError) as excinfo:
            handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    assert excinfo.value.code == lg.CODE_ALREADY_OWNED
    assert "keeps being replaced" in str(excinfo.value), "the cause is reported as contention"
    assert _types() == [], "an append landed without ownership"


# --- another process -------------------------------------------------------


def _child(home: Path, tmp_path: Path) -> "subprocess.Popen[str]":
    """Start the owner child under a DECLARED environment, and wait for its claim.

    Inheriting ``os.environ`` wholesale would make the child depend on whatever
    else the shard it landed in exported -- another test's ``KIROCREW_*``, a
    proxy, a ``TMPDIR`` -- so the same test could pass in one shard and fail in
    another for reasons unrelated to what it asserts.
    """
    env = {
        "KIROCREW_HOME": str(home),
        "KIROCREW_CREW_LOG": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": str(tmp_path),
    }
    if sys.platform == "win32":  # pragma: no cover - the suite skips Windows
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(CHILD), SESSION],
        env=env,
        # Confined to the test's directory: a relative path written by the child
        # must not land in the checkout and dirty the working tree.
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert child.stdout is not None
    # The ordering between the two processes, with no clock in it: this returns
    # the instant the child announces. The child is reaped HERE rather than by
    # the caller, because the caller's ``finally`` only begins once this
    # RETURNS -- a handshake that never completes would leave it running with
    # nothing left holding a reference to kill it. The ceiling is a watchdog for
    # a wedged host, not a delay the handshake waits out.
    try:
        ready, _, _ = select.select([child.stdout], [], [], CHILD_TIMEOUT)
        assert ready, "the child never announced, so its pipe stayed silent"
        announced = child.stdout.readline().strip()
        assert announced == "owned", f"the child never claimed the log: {announced!r}"
    except BaseException:
        child.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=CHILD_TIMEOUT)
        raise
    return child


def _release(child: "subprocess.Popen[str]") -> None:
    """Let the child exit, and wait for the kernel to have taken its lock back."""
    assert child.stdin is not None
    child.stdin.write("release\n")
    child.stdin.flush()
    try:
        assert child.wait(timeout=CHILD_TIMEOUT) == 0, child.stderr.read() if child.stderr else ""
    except BaseException:
        child.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=CHILD_TIMEOUT)
        raise


def test_release_reaps_child_when_wait_times_out(tmp_path, monkeypatch):
    child = subprocess.Popen(  # noqa: S603 - fixed Python probe, no shell
        [sys.executable, "-c", "import sys; sys.stdin.readline(); sys.stdin.readline()"],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    real_wait = child.wait
    wait_calls = 0

    def _timeout_once(timeout=None):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            raise subprocess.TimeoutExpired(child.args, timeout)
        return real_wait(timeout=timeout)

    monkeypatch.setattr(child, "wait", _timeout_once)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            _release(child)
        assert child.poll() is not None, "the timed-out child was not reaped"
        assert wait_calls == 2, "the timeout path did not wait after killing the child"
    finally:
        if child.poll() is None:
            child.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                real_wait(timeout=CHILD_TIMEOUT)


@needs_real_processes
def test_a_resume_in_another_process_cannot_close_a_live_writers_turn(tmp_path):
    """The hazard itself: a repair is refused while another process is writing.

    The child owns the log with a turn and a tool call open. A resume here asks to
    close them, which is exactly what a second gateway does when it claims a
    session id whose writer is still alive. It is refused, and the file keeps the
    live writer's account of the turn -- one outcome, not two.

    Mutation guard: dropping the claim from ``open``'s repair branch lets the
    closers land while the child still holds the turn, and the two assertions on
    the file's contents redden with the shape a fold cannot interpret.
    """
    child = _child(tmp_path / "home", tmp_path)
    try:
        refused = None
        try:
            CrewLog.open(lg.KIND_SESSION, SESSION, repair=True)
        except CrewLogError as exc:
            refused = exc.code
        # The damage first: under the mutation this names the shape a fold cannot
        # interpret -- a closed turn followed by the rest of the live one.
        types = _types()
        assert types == [
            "turn/started",
            "tool/called",
        ], f"a repair wrote into a log another process is still writing: {types}"
        assert refused == lg.CODE_ALREADY_OWNED
    finally:
        _release(child)


@needs_real_processes
def test_ownership_returns_when_the_owning_process_exits(tmp_path):
    """A writer that is gone blocks nobody: the kernel released its lock.

    This is why ownership carries no expiry. It does not need one for the case it
    is for -- a crash, a kill, an eviction -- and an expiry would let a resume
    expropriate a writer that is merely slow.
    """
    child = _child(tmp_path / "home", tmp_path)
    _release(child)

    repaired = CrewLog.open(lg.KIND_SESSION, SESSION, repair=True)
    types = _types()
    assert "tool/completed" in types, f"the successor could not close the call: {types}"
    assert types[-1] == "turn/completed", f"the successor could not close the turn: {types}"
    # And it owns the log now, so it can keep writing.
    repaired.append("session/closed", {"reason": "shutdown"}, src="gateway")
