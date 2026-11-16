"""A stuck lock holder must fail the waiter, not hang it.

``fcntl.flock`` takes no timeout, so a bare POSIX acquire waits on a holder
without limit while the Windows branch refuses past ``_LOCK_TIMEOUT_SECS``. That
asymmetry turns an unavailable agent-spec lock into a gateway that hangs at boot:
``agents_spec_lock`` -> ``file_lock(wait=True)`` -> ``flock``, leaving a live
process with no port bound, no ``KIROCREW_READY`` line, and nothing logged at
WARNING or ERROR. An operator and a health check both read that as a start which
has not finished, and wait.

The property these tests pin is NOT "a lock is held" -- it is that a waiter which
cannot get the lock RETURNS, with a message naming why. The boot path handles a
raise well (it logs at ERROR, prints the repair command and still binds its
port), so failing closed is what makes the failure reportable at all.

Each test uses a real second PROCESS as the holder: ``flock`` is per-open-file,
so two fds in one process do not contend the way a gateway and a stuck prior
generation do, and an in-process fake would not exercise the syscall that blocks.
The ceiling is monkeypatched down to keep these tests sub-second; the shipped
value is deliberately far longer than any in-tree critical section.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest
from _clock_isolation import assert_clock_is_private

from kiro_crew import platform_compat
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.clock import ManualClock

pytestmark = pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="POSIX flock ceiling; Windows has its own branch"
)

# Long enough that the holder is reliably up and the acquire really does wait,
# short enough that a regression fails the test instead of stalling the shard.
_TEST_CEILING = 1.0


def _spawn_holder(lock_path: Path, hold_secs: float = 60.0) -> subprocess.Popen:
    """Start a separate process holding an exclusive flock on *lock_path*.

    Returns once the child has confirmed the lock is held, so the waiter under
    test is never racing the holder's own acquire.
    """
    script = textwrap.dedent("""
        import fcntl, os, sys, time
        fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        print("held", flush=True)
        time.sleep(float(sys.argv[2]))
        """)
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path), str(hold_secs)],
        stdout=subprocess.PIPE,
        # Explicit cwd rather than inheriting pytest's: the child writes only the
        # absolute path in argv, but an inherited repository CWD is a standing
        # invitation for any later edit here to leave an artifact in the tree.
        cwd=lock_path.parent,
        **UTF8_TEXT,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held", "holder failed to take the lock"
    return proc


@pytest.fixture
def held_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A lock file already held by another process, with a short ceiling."""
    monkeypatch.setattr(platform_compat, "_LOCK_TIMEOUT_SECS", _TEST_CEILING)
    lock_path = tmp_path / "contended.lock"
    proc = _spawn_holder(lock_path)
    try:
        yield lock_path
    finally:
        proc.kill()
        proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()


class TestFileLockCeiling:
    def test_contended_wait_raises_instead_of_hanging(self, held_lock: Path, monkeypatch):
        # Both halves matter: that the call comes back at all, and that it
        # refuses rather than proceeding unserialized.
        clock = _pin_clock(monkeypatch)
        flocks = _refuse_kernel_blocking_flock(monkeypatch)
        fd = os.open(held_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(OSError) as excinfo:
                with platform_compat.file_lock(fd, exclusive=True, wait=True):
                    pytest.fail("entered the critical section while another process held it")
        finally:
            os.close(fd)

        # It genuinely waits (polls rather than degrading to a non-blocking single
        # shot), and gives up at the ceiling rather than at some unrelated time:
        # the poll sleeps, on a clock only they move, add up to the ceiling.
        _assert_polled_to(clock, flocks, _TEST_CEILING)

        # A BlockingIOError here would mean the waiting path had collapsed into
        # the wait=False contract, which is a different (and wrong) behavior.
        assert not isinstance(excinfo.value, BlockingIOError)

    def test_refusal_names_the_ceiling_and_the_reason(self, held_lock: Path):
        # A caller that catches this as best-effort work reports nothing else, so
        # the message must say what was waited and why it stopped, not merely
        # that something failed.
        fd = os.open(held_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(OSError) as excinfo:
                with platform_compat.file_lock(fd, exclusive=True, wait=True):
                    pass
        finally:
            os.close(fd)

        message = str(excinfo.value)
        assert "could not acquire" in message
        assert f"limit {_TEST_CEILING:g}s" in message
        assert "still held after waiting" in message
        assert "stuck" not in message
        assert "unserialized" in message

    def test_uncontended_acquire_still_succeeds(self, tmp_path: Path, monkeypatch):
        # The ceiling must cost the common case nothing: a free lock is taken
        # with no delay and no raise. "No delay" is asserted as the waits the
        # acquire takes on the lock helpers' clock: none.
        clock = _pin_clock(monkeypatch)
        fd = os.open(tmp_path / "free.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with platform_compat.file_lock(fd, exclusive=True, wait=True):
                entered = True
            assert entered
            assert clock.sleeps == (), f"a free lock was polled for: {clock.sleeps}"
        finally:
            os.close(fd)

    def test_shared_acquire_is_still_shared(self, tmp_path: Path):
        # Two shared holders must coexist: the bounded acquire must not silently
        # promote LOCK_SH to LOCK_EX while adding the ceiling.
        path = tmp_path / "shared.lock"
        first = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        second = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with platform_compat.file_lock(first, exclusive=False, wait=True):
                with platform_compat.file_lock(second, exclusive=False, wait=True):
                    both = True
            assert both
        finally:
            os.close(first)
            os.close(second)

    def test_non_waiting_acquire_still_fails_fast(self, held_lock: Path, monkeypatch):
        # wait=False is a DIFFERENT contract (raise at once, BlockingIOError) and
        # the ceiling must not absorb it into a one-second wait. "At once" is
        # asserted as the waits the acquire takes: no poll sleep on the lock
        # helpers' clock, and no kernel-blocking flock on this thread.
        clock = _pin_clock(monkeypatch)
        flocks = _refuse_kernel_blocking_flock(monkeypatch)
        fd = os.open(held_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(BlockingIOError):
                with platform_compat.file_lock(fd, exclusive=True, wait=False):
                    pass
        finally:
            os.close(fd)
        assert clock.sleeps == (), f"a non-waiting acquire polled: {clock.sleeps}"
        assert len(flocks.ops) == 1, f"expected one single-shot flock: {flocks.ops!r}"

    def test_bad_fd_surfaces_now_not_at_the_ceiling(self, tmp_path: Path, monkeypatch):
        # Only "someone holds it" is retryable. An error about THIS fd must not
        # be swallowed into the poll loop and reported as a stuck holder -- that
        # would hide a real defect behind a five-minute wait.
        # "Now" is asserted as the waits the acquire takes on the lock helpers'
        # clock: none, and the error raised is the fd's own EBADF.
        monkeypatch.setattr(platform_compat, "_LOCK_TIMEOUT_SECS", 30.0)
        clock = _pin_clock(monkeypatch)
        fd = os.open(tmp_path / "closed.lock", os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with pytest.raises(OSError) as excinfo:
            with platform_compat.file_lock(fd, exclusive=True, wait=True):
                pass
        assert clock.sleeps == (), f"a bad fd was retried as contention: {clock.sleeps}"
        assert excinfo.value.errno == errno.EBADF, excinfo.value
        assert "stuck" not in str(excinfo.value)


class TestCallerSuppliedCeiling:
    """A holder whose work legitimately outlasts the default ceiling."""

    def test_a_longer_caller_ceiling_outlasts_the_default(self, held_lock: Path, monkeypatch):
        # The default ceiling suits a sub-second critical section. A caller that
        # holds the lock across an install or a build must be able to wait for
        # that work, or a contender is refused while the holder is still running.
        caller_ceiling = _TEST_CEILING * 3
        clock = _pin_clock(monkeypatch)
        flocks = _refuse_kernel_blocking_flock(monkeypatch)
        fd = os.open(held_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(OSError):
                with platform_compat.file_lock(
                    fd, exclusive=True, wait=True, timeout=caller_ceiling
                ):
                    pytest.fail("entered the critical section while another process held it")
        finally:
            os.close(fd)

        # Waiting past the patched default is the whole point: a wait that ends
        # at _TEST_CEILING means the caller's value was ignored.
        _assert_polled_to(clock, flocks, caller_ceiling)

    def test_the_refusal_names_the_caller_ceiling_not_the_default(self, held_lock: Path):
        # The message is the only thing an operator sees, so it must report the
        # ceiling actually waited rather than the module default.
        caller_ceiling = _TEST_CEILING * 3
        fd = os.open(held_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(OSError) as excinfo:
                with platform_compat.file_lock(
                    fd, exclusive=True, wait=True, timeout=caller_ceiling
                ):
                    pass
        finally:
            os.close(fd)

        message = str(excinfo.value)
        assert f"limit {caller_ceiling:g}s" in message
        assert f"limit {_TEST_CEILING:g}s" not in message

    def test_omitting_the_ceiling_still_uses_the_module_default(self, held_lock: Path, monkeypatch):
        # The parameter is additive: a caller that passes nothing is bounded by
        # the default exactly as before.
        clock = _pin_clock(monkeypatch)
        flocks = _refuse_kernel_blocking_flock(monkeypatch)
        fd = os.open(held_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(OSError) as excinfo:
                with platform_compat.file_lock(fd, exclusive=True, wait=True):
                    pass
        finally:
            os.close(fd)

        _assert_polled_to(clock, flocks, _TEST_CEILING)
        assert f"limit {_TEST_CEILING:g}s" in str(excinfo.value)


class TestStagingLockOutlastsItsOwnWork:
    """The frontend staging lock is held across an install and a build."""

    def test_the_ceiling_covers_the_install_and_build_it_spans(self):
        # Derived from the bounds it must outlast rather than compared with a
        # literal, so raising either subprocess timeout without raising the lock
        # ceiling fails here instead of silently refusing live contenders.
        from kiro_crew import frontend

        assert frontend._STAGING_LOCK_TIMEOUT >= (
            frontend._INSTALL_TIMEOUT + frontend._BUILD_TIMEOUT
        )
        # And it must actually exceed the module default, which is what makes it
        # necessary at all.
        assert frontend._STAGING_LOCK_TIMEOUT > platform_compat._LOCK_TIMEOUT_SECS

    def test_the_staging_lock_passes_its_ceiling_to_the_primitive(self, tmp_path: Path):
        # Asserted by capturing the value the primitive receives, not by reading
        # the source: a call site can keep the identifier and still be inert.
        from kiro_crew import frontend

        seen: dict[str, object] = {}

        @contextlib.contextmanager
        def _capture(fd: int, **kwargs: object):
            seen.update(kwargs)
            yield

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(frontend.platform_compat, "file_lock", _capture)
            with frontend._staging_lock(tmp_path / "static"):
                pass

        assert seen.get("timeout") == frontend._STAGING_LOCK_TIMEOUT


def _pin_clock(monkeypatch) -> ManualClock:
    """A clock only the lock helpers read, starting at 0.

    Installed on ``platform_compat``'s own binding. Its ``time`` IS the stdlib
    module, so patching ``platform_compat.time.sleep`` (or the stdlib ``time``
    itself) would also record, and count against the deadline, any other
    thread's sleep in the worker.

    The lock owners in ``kiro_crew.platform_lock_compat`` read the clock through a
    function-local ``from kiro_crew.platform_compat import ... time``
    (``_posix_acquire_blocking``, ``_win_acquire_blocking``, ``file_lock`` and
    ``acquire_lock``), so this one install clocks all of them, including a
    ``file_lock`` that another thread left running in this worker takes while
    the test runs.

    It also clocks ``platform_compat``'s own readers of that binding, such as
    ``terminate_and_reap_sync`` (its POSIX process-group wait) and
    ``_drain_windows_process_tree``. A thread left inside one of those while
    the test runs would still record its poll sleep here. That window is
    acceptable: it needs a leftover thread in one of a few cleanup loops for
    the length of one test, where a stdlib patch would record every sleeping
    thread in the worker.
    """
    clock = ManualClock(monotonic_start=0.0)
    clock.install(monkeypatch, platform_compat)
    return clock


class _NonBlockingFlockOnly:
    """``fcntl`` as the lock helpers see it, refusing a kernel-blocking ``flock``.

    A ``flock`` without ``LOCK_NB`` sleeps in the kernel until the holder lets go,
    which no patched clock records. On the thread that installed it, such a call
    fails by name instead and every ``flock`` operation is recorded; other threads
    and every other ``fcntl`` name reach the module it wraps.
    """

    def __init__(self, wrapped: object) -> None:
        import fcntl

        self._wrapped = wrapped
        self._non_blocking = fcntl.LOCK_NB | fcntl.LOCK_UN
        self._thread_id = threading.get_ident()
        self.ops: list[int] = []

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)

    def flock(self, fd: int, operation: int) -> None:
        if threading.get_ident() == self._thread_id:
            self.ops.append(operation)
            if not operation & self._non_blocking:
                raise AssertionError(
                    f"kernel-blocking flock({operation:#x}) on the thread under test"
                )
        self._wrapped.flock(fd, operation)


def _refuse_kernel_blocking_flock(monkeypatch) -> _NonBlockingFlockOnly:
    """Install :class:`_NonBlockingFlockOnly` on ``platform_compat``'s ``fcntl`` binding.

    The lock owners read ``fcntl`` through a function-local
    ``from kiro_crew.platform_compat import fcntl``, so this one binding covers them.
    """
    spy = _NonBlockingFlockOnly(platform_compat.fcntl)
    monkeypatch.setattr(platform_compat, "fcntl", spy)
    return spy


def _assert_polled_to(clock: ManualClock, flocks: _NonBlockingFlockOnly, ceiling: float) -> None:
    """The refused acquire polled ``LOCK_NB`` until *ceiling*, and not past it.

    The poll sleeps are the only thing that moves *clock*, so their sum is the
    wait the acquire took; a kernel-blocking ``flock`` already failed by name.
    """
    slept = clock.sleeps
    assert slept, "the contended acquire never polled: a single-shot try, not a wait"
    assert len(flocks.ops) == len(slept) + 1, f"one try per poll: {flocks.ops!r} {slept!r}"
    assert sum(slept) == pytest.approx(
        ceiling, abs=1e-6
    ), f"gave up after {sum(slept):.3f}s of polling, not at the {ceiling:g}s ceiling"


def test_another_thread_keeps_the_real_sleep_and_clock(monkeypatch):
    assert_clock_is_private(_pin_clock(monkeypatch), platform_compat)


class TestOnLoopNeverSleeps:
    """A contended acquire on the event-loop thread must not sleep there."""

    def test_a_contended_on_loop_acquire_refuses_at_once(self, held_lock: Path):
        # A poll-sleep on the loop freezes chat and heartbeat for the whole wait,
        # and a freeze long enough to miss a heartbeat is a supervisor kill. So a
        # contended on-loop acquire fails closed instead of waiting.
        slept: list[float] = []

        async def _acquire_on_loop() -> None:
            with pytest.MonkeyPatch.context() as mp:
                clock = _pin_clock(mp)
                flocks = _refuse_kernel_blocking_flock(mp)
                fd = os.open(held_lock, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    with pytest.raises(OSError) as excinfo:
                        with platform_compat.file_lock(fd, exclusive=True, wait=True):
                            pytest.fail("entered the critical section while held")
                    # Refused promptly, not at the ceiling: one non-blocking try
                    # (a kernel-blocking flock fails by name above), and no poll
                    # sleep (asserted on ``slept`` below).
                    assert len(flocks.ops) == 1, f"expected one single-shot try: {flocks.ops!r}"
                    # And the refusal says so: no ceiling it never applied, no
                    # claimed cause, and where a caller that must wait goes.
                    message = str(excinfo.value)
                    assert "never waits" in message
                    assert "worker thread" in message
                    assert f"{_TEST_CEILING:g}s" not in message
                    assert "stuck" not in message
                finally:
                    os.close(fd)
                slept.extend(clock.sleeps)

        asyncio.run(_acquire_on_loop())
        # The assertion that matters: zero sleeps on the loop thread. A patched
        # sleep also means a regression here fails fast instead of stalling.
        assert slept == []

    def test_an_uncontended_on_loop_acquire_still_succeeds(self, tmp_path: Path):
        # Single-shot must not mean "always refuse": a free lock is still taken.
        taken: list[bool] = []

        async def _acquire_free() -> None:
            fd = os.open(tmp_path / "free-on-loop.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                with platform_compat.file_lock(fd, exclusive=True, wait=True):
                    taken.append(True)
            finally:
                os.close(fd)

        asyncio.run(_acquire_free())
        assert taken == [True]

    def test_off_loop_still_polls_and_waits(self, held_lock: Path):
        # The single-shot rule is scoped to the loop thread: off it, a waiter must
        # still wait out a legitimately long holder rather than racing it.
        fd = os.open(held_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            started = time.monotonic()
            with pytest.raises(OSError):
                with platform_compat.file_lock(fd, exclusive=True, wait=True):
                    pass
            waited = time.monotonic() - started
        finally:
            os.close(fd)

        assert waited >= _TEST_CEILING


class TestInProcessOverlapOffTheLoop:
    """A coroutine that must wait for the lock takes it from a worker thread.

    ``flock`` counts a second descriptor in the SAME process as a competing
    holder, so a sibling thread's brief critical section refuses an on-loop
    acquire. Offloaded with ``asyncio.to_thread`` the acquire is a real bounded
    wait and the loop keeps running.
    """

    @staticmethod
    @contextlib.contextmanager
    def _hold_in_thread(lock_path: Path) -> Iterator[threading.Event]:
        """Hold the lock on a thread until the yielded event is set, never on a timer.

        Leaving the block always releases and joins the holder, including when the
        holder never took the lock in time.
        """
        held = threading.Event()
        stop = threading.Event()

        def _run() -> None:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                with platform_compat.file_lock(fd, exclusive=True):
                    held.set()
                    stop.wait()
            finally:
                os.close(fd)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        try:
            assert held.wait(5), "holder thread failed to take the lock"
            yield stop
        finally:
            stop.set()
            thread.join(5)

    @staticmethod
    def _locked_section(lock_path: Path, timeout: float) -> float:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            started = time.monotonic()
            with platform_compat.file_lock(fd, exclusive=True, timeout=timeout):
                return time.monotonic() - started
        finally:
            os.close(fd)

    def test_a_brief_in_process_holder_is_waited_out(self, tmp_path: Path, monkeypatch):
        lock_path = tmp_path / "overlap.lock"
        hold_secs = 0.05
        # Only a safety ceiling: the waiter is released long before it, so a
        # starved runner must not turn a slow wake-up into a refusal.
        waiter_ceiling = 10.0
        with self._hold_in_thread(lock_path) as stop:
            # The holder lets go on a signal, not after a fixed hold: a fixed hold
            # races the waiter's own start, and a waiter scheduled after it ends
            # finds the lock already free. The signal is the waiter's own refused
            # attempt on THIS lock file, so no other thread's flock can send it.
            lock_inode = os.stat(lock_path).st_ino
            met = threading.Event()
            real_flock = platform_compat.fcntl.flock

            def _flock(fd: int, op: int) -> None:
                try:
                    real_flock(fd, op)
                except BlockingIOError:
                    if op & platform_compat.fcntl.LOCK_NB and os.fstat(fd).st_ino == lock_inode:
                        met.set()
                    raise

            monkeypatch.setattr(platform_compat.fcntl, "flock", _flock)

            async def _main() -> float:
                waiter = asyncio.create_task(
                    asyncio.to_thread(self._locked_section, lock_path, waiter_ceiling)
                )
                watcher = asyncio.create_task(asyncio.to_thread(met.wait, 5))
                # Release inside the coroutine: asyncio.run joins its executor
                # before an outer finally could, and the waiter cannot finish
                # while the lock is held.
                try:
                    await asyncio.wait({waiter, watcher}, return_when=asyncio.FIRST_COMPLETED)
                    if waiter.done():
                        return await waiter  # its own error, not "never met"
                    assert watcher.result(), "waiter never met the lock"
                    await asyncio.sleep(hold_secs)
                finally:
                    stop.set()
                    met.set()
                return await waiter

            waited = asyncio.run(_main())

        # Refused at first, then entered (a refusal at the ceiling would have
        # raised) no earlier than the release: a real wait, not a free lock.
        assert waited >= hold_secs

    def test_a_holder_that_never_releases_is_refused_at_the_timeout(self, tmp_path: Path):
        lock_path = tmp_path / "held-forever.lock"

        async def _main() -> tuple[OSError, float]:
            started = time.monotonic()
            with pytest.raises(OSError) as excinfo:
                await asyncio.to_thread(self._locked_section, lock_path, 2.0)
            return excinfo.value, time.monotonic() - started

        with self._hold_in_thread(lock_path):
            error, waited = asyncio.run(_main())

        assert not isinstance(error, BlockingIOError)
        assert 2.0 <= waited < 3.0
        message = str(error)
        # The real wait and the limit, and no claimed cause.
        assert "still held after waiting 2." in message
        assert "limit 2s" in message
        assert "stuck" not in message


class TestPollBackoff:
    """The poll stands in for a zero-cost kernel sleep, so its wakeups are bounded."""

    def test_backoff_caps_the_sleep_and_never_overshoots_the_deadline(self, monkeypatch):
        # No real lock here: the SLEEP SCHEDULE is the subject, so the acquire is
        # stubbed permanently-held and every sleep is recorded instead of taken.
        monkeypatch.setattr(platform_compat, "_LOCK_TIMEOUT_SECS", 5.0)

        def _always_held(fd, mode):
            raise OSError(errno.EAGAIN, "held")

        monkeypatch.setattr(platform_compat.fcntl, "flock", _always_held)
        clock = _pin_clock(monkeypatch)

        assert platform_compat._posix_acquire_blocking(0, 0) is False
        slept = clock.sleeps

        # Bounded wakeups: a flat 10ms poll over 5s would be ~500 sleeps.
        assert len(slept) < 60, f"too many wakeups: {len(slept)}"
        # Tight at first, so a brief holder is still picked up promptly.
        assert slept[0] == pytest.approx(platform_compat._LOCK_POLL_SECS)
        # Capped, and never sleeping past the ceiling.
        assert max(slept) <= platform_compat._LOCK_POLL_MAX_SECS
        assert sum(slept) == pytest.approx(5.0, abs=0.01)


class TestAcquireLockCeiling:
    def test_acquire_lock_refuses_a_stuck_holder(self, held_lock: Path):
        # The fd-handoff path (acquire now, release later) shares the same
        # unbounded-wait hazard as the context manager.
        fd = os.open(held_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            started = time.monotonic()
            with pytest.raises(OSError, match="still held"):
                platform_compat.acquire_lock(fd, exclusive=True)
            assert time.monotonic() - started >= _TEST_CEILING
        finally:
            os.close(fd)

    def test_acquire_lock_still_takes_a_free_lock(self, tmp_path: Path):
        fd = os.open(tmp_path / "free2.lock", os.O_CREAT | os.O_RDWR, 0o600)
        other = os.open(tmp_path / "free2.lock", os.O_RDWR)
        try:
            platform_compat.acquire_lock(fd, exclusive=True)
            # The lock is really held: a second open of the same file cannot take it.
            assert platform_compat.try_acquire_lock(other, exclusive=True) is False
            platform_compat.release_lock(fd)
            assert platform_compat.try_acquire_lock(other, exclusive=True) is True
            platform_compat.release_lock(other)
        finally:
            os.close(other)
            os.close(fd)


class TestAgentSpecLockDoesNotHangBoot:
    def test_agents_spec_lock_refuses_when_another_process_holds_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # End to end on the lock the boot path takes: the gateway-startup hang
        # reduced to a single call.
        from kiro_crew.agent import agents_spec_lock

        monkeypatch.setattr(platform_compat, "_LOCK_TIMEOUT_SECS", _TEST_CEILING)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        proc = _spawn_holder(agents_dir / ".kirocrew-agents.lock")
        try:
            with pytest.raises(OSError, match="still held"):
                with agents_spec_lock(agents_dir):
                    pytest.fail("took the agent-spec lock while another process held it")
        finally:
            proc.kill()
            proc.wait(timeout=10)
            if proc.stdout is not None:
                proc.stdout.close()

    def test_unwritable_lock_path_fails_fast(self, tmp_path: Path, monkeypatch):
        # An unwritable lock path is refused by ``os.open`` before any lock is
        # attempted. Pinned so a "retry until the ceiling" change cannot turn
        # this prompt refusal into a wait: the lock path is opened once, and
        # nothing sleeps on the lock helpers' clock.
        from kiro_crew.agent import agents_spec_lock

        agents_dir = tmp_path / "readonly"
        agents_dir.mkdir()
        lock_name = str(agents_dir / ".kirocrew-agents.lock")
        opens: list[str] = []
        real_open = os.open

        def recording_open(path, *args, **kwargs):
            if os.fspath(path) == lock_name:
                opens.append(lock_name)
            return real_open(path, *args, **kwargs)

        os.chmod(agents_dir, 0o500)
        try:
            with monkeypatch.context() as mp:
                clock = _pin_clock(mp)
                mp.setattr(os, "open", recording_open)
                with pytest.raises(OSError) as excinfo:
                    with agents_spec_lock(agents_dir):
                        pytest.fail("acquired a lock under an unwritable directory")
            assert opens == [lock_name], f"the lock path was opened {len(opens)} times"
            assert clock.sleeps == (), f"the refusal waited: {clock.sleeps}"
            # An unwritable path is not a stuck holder and must not claim to be.
            assert "stuck" not in str(excinfo.value)
        finally:
            os.chmod(agents_dir, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- restoring a dir this test alone created from 0o500 to owner-only so tmp_path cleanup can traverse it; Semgrep's suggested 0o644 would grant world-read AND drop the traversal bit. lockdown-ok.  # noqa: E501  # fmt: skip


class TestLockFailureIsReported:
    """Failing fast is only useful if the reason reaches the operator.

    Several callers catch this install work at ``logger.debug`` as best-effort,
    so a refusal reported only by raising is indistinguishable from success in
    the log. The lock names its own path before propagating.
    """

    def test_unwritable_path_is_logged_with_path_and_reason(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        from kiro_crew.agent import agents_spec_lock

        agents_dir = tmp_path / "ro"
        agents_dir.mkdir()
        os.chmod(agents_dir, 0o500)
        try:
            with caplog.at_level("WARNING", logger="kiro_crew.agent"):
                with pytest.raises(OSError):
                    with agents_spec_lock(agents_dir):
                        pass
        finally:
            os.chmod(agents_dir, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- restoring a dir this test alone created from 0o500 to owner-only so tmp_path cleanup can traverse it; Semgrep's suggested 0o644 would grant world-read AND drop the traversal bit. lockdown-ok.  # noqa: E501  # fmt: skip

        records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert records, "an unwritable lock path was refused with no WARNING"
        message = records[-1].getMessage()
        # The PATH is what makes the log actionable: a bare "permission denied"
        # does not say which mount to fix.
        assert ".kirocrew-agents.lock" in message
        assert str(agents_dir) in message
        # And the remedy that resolves it.
        assert "KIRO_HOME" in message

    def test_stuck_holder_is_logged_without_the_readonly_remedy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        # A stuck holder needs a different action than an unwritable mount, so
        # the two must not be reported with the same remedy.
        from kiro_crew.agent import agents_spec_lock

        monkeypatch.setattr(platform_compat, "_LOCK_TIMEOUT_SECS", _TEST_CEILING)
        agents_dir = tmp_path / "contended-agents"
        agents_dir.mkdir()
        proc = _spawn_holder(agents_dir / ".kirocrew-agents.lock")
        try:
            with caplog.at_level("WARNING", logger="kiro_crew.agent"):
                with pytest.raises(OSError, match="still held"):
                    with agents_spec_lock(agents_dir):
                        pytest.fail("took the lock while another process held it")
        finally:
            proc.kill()
            proc.wait(timeout=10)
            if proc.stdout is not None:
                proc.stdout.close()

        messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("still held" in m for m in messages), messages
        assert any(".kirocrew-agents.lock" in m for m in messages), messages
        # The read-only remedy would be wrong advice here: the path IS writable.
        assert not any("KIRO_HOME" in m for m in messages), messages

    def test_a_caller_body_error_is_not_reported_as_a_lock_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        # The lock is acquired fine here; the CALLER's work fails. Reporting that
        # as an agent-spec lock problem would send an operator hunting a stuck
        # holder while the real fault is the disk or a permission, so the report
        # must cover the acquire alone and let a body error through untouched.
        from kiro_crew.agent import agents_spec_lock

        agents_dir = tmp_path / "body-error"
        agents_dir.mkdir()
        with caplog.at_level("WARNING", logger="kiro_crew.agent"):
            with pytest.raises(OSError, match="no space left"):
                with agents_spec_lock(agents_dir):
                    # Same exception TYPE the acquire raises, so only scoping
                    # distinguishes them -- not the class.
                    raise OSError(errno.ENOSPC, "no space left on device")

        messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert not any("agent-spec lock" in m for m in messages), messages
        assert not any("stuck" in m for m in messages), messages
