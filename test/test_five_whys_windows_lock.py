"""The five-whys log lock waits out a held lock on Windows and refuses past a ceiling.

``msvcrt.locking(LK_LOCK)`` is not a wait. It retries about ten times at one-second
intervals and then raises ``EDEADLOCK`` for a lock that is only held, so a second
five-whys command against the same log died with an unhandled ``OSError`` instead of
serializing behind the first. ``five_whys._flock`` polls the non-blocking code, retries
only on contention, and refuses at a ceiling rather than running unserialized.

The script is stdlib-only and runs as a bare file, so these tests load it by path and
drive its Windows branch on any host: a fake CRT models what ``_locking`` does against a
virtual clock, so the ten-second boundary is exercised without waiting for it.
"""

from __future__ import annotations

import ast
import errno
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script
from source_corpus import parsed_candidates

from kiro_crew import platform_lock_compat

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "five-whys"
    / "scripts"
    / "five_whys.py"
)

EDEADLOCK = getattr(errno, "EDEADLOCK", errno.EDEADLK)
# msvcrt's blocking codes give up after this many one-second retries.
LK_LOCK_RETRIES = 10
# A wait that has not ended by this virtual time never will; fail instead of spinning.
RUNAWAY_SECS = 1000.0


def _mod():
    return load_skill_script("_five_whys_under_test", SCRIPT)


class _VirtualClock:
    """A clock that moves only when the product sleeps; records each sleep.

    Stands in for the script's own ``time`` binding, so every name it does not define
    (none is read today) answers from the real module.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __getattr__(self, name: str):
        return getattr(time, name)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.slept.append(secs)
        self.now += secs
        if self.now > RUNAWAY_SECS:
            raise AssertionError("the wait never ended")


class _FakeCrt:
    """``msvcrt`` as ``_locking`` behaves, with the lock held until ``release_at``.

    The non-blocking code fails with ``busy_errno`` while the range is held. The blocking
    codes retry once a second and give up with ``EDEADLOCK`` after ``LK_LOCK_RETRIES``.
    An unlock of a range this process does not hold fails ``EACCES``, as ``_locking``
    does. ``fail_with`` makes every call fail with that errno.
    """

    LK_UNLCK = 0
    LK_LOCK = 1
    LK_NBLCK = 2
    LK_RLCK = 3

    def __init__(
        self,
        clock: _VirtualClock,
        release_at: float = 0.0,
        busy_errno: int = errno.EACCES,
    ) -> None:
        self.clock = clock
        self.release_at = release_at
        self.busy_errno = busy_errno
        self.fail_with: int | None = None
        self.calls: list[int] = []
        self.fds: list[int] = []
        self.held = False

    def locking(self, fd: int, mode: int, _nbytes: int) -> None:
        self.calls.append(mode)
        self.fds.append(fd)
        if self.fail_with is not None:
            raise OSError(self.fail_with, os.strerror(self.fail_with))
        if mode == self.LK_UNLCK:
            if not self.held:
                raise OSError(errno.EACCES, "unlock of a range that is not locked")
            self.held = False
            return
        if mode in (self.LK_LOCK, self.LK_RLCK):
            for _ in range(LK_LOCK_RETRIES):
                if self.clock.now >= self.release_at:
                    self.held = True
                    return
                self.clock.now += 1.0
            raise OSError(EDEADLOCK, "locking violation after 10 attempts")
        if self.clock.now >= self.release_at:
            self.held = True
            return
        raise OSError(self.busy_errno, os.strerror(self.busy_errno))


@pytest.fixture
def lock_fd(tmp_path):
    fd = os.open(str(tmp_path / "log.jsonl.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    yield fd
    try:
        os.close(fd)
    except OSError:
        pass


def _on_windows(monkeypatch, mod, crt, clock):
    """Point ``mod``'s own bindings at the fake CRT and clock, as win32."""
    monkeypatch.setattr(mod, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(mod, "msvcrt", crt, raising=False)
    monkeypatch.setattr(mod, "time", clock, raising=False)


class TestWindowsAcquireWaits:
    @pytest.mark.parametrize("hold_secs", [0.0, 2.0, 13.0, 120.0])
    def test_a_held_lock_is_waited_out_past_the_lk_lock_ceiling(
        self, monkeypatch, lock_fd, hold_secs
    ):
        # 13 s and 120 s outlast what LK_LOCK waits for. 0 s and 2 s are the control
        # where LK_LOCK also succeeds, so the fake is not rigged against it.
        mod, clock = _mod(), _VirtualClock()
        crt = _FakeCrt(clock, release_at=hold_secs)
        _on_windows(monkeypatch, mod, crt, clock)

        mod._flock(lock_fd, True)

        assert crt.held is True
        assert clock.now >= hold_secs

    def test_the_acquire_never_passes_a_blocking_code(self, monkeypatch, lock_fd):
        mod, clock = _mod(), _VirtualClock()
        crt = _FakeCrt(clock, release_at=3.0)
        _on_windows(monkeypatch, mod, crt, clock)

        mod._flock(lock_fd, True)

        assert crt.calls
        assert set(crt.calls) == {crt.LK_NBLCK}

    @pytest.mark.parametrize("busy", [errno.EACCES, EDEADLOCK])
    def test_both_contention_errnos_are_polled_through(self, monkeypatch, lock_fd, busy):
        # Held past the 10 s a blocking code waits, so a call that gives up there is red.
        mod, clock = _mod(), _VirtualClock()
        crt = _FakeCrt(clock, release_at=15.0, busy_errno=busy)
        _on_windows(monkeypatch, mod, crt, clock)

        mod._flock(lock_fd, True)

        assert crt.held is True
        assert clock.now >= 15.0

    @pytest.mark.parametrize("fatal", [errno.EBADF, errno.EINVAL])
    def test_an_error_about_the_descriptor_surfaces_at_once(self, monkeypatch, lock_fd, fatal):
        mod, clock = _mod(), _VirtualClock()
        crt = _FakeCrt(clock)
        crt.fail_with = fatal
        _on_windows(monkeypatch, mod, crt, clock)

        with pytest.raises(OSError) as excinfo:
            mod._flock(lock_fd, True)

        assert excinfo.value.errno == fatal
        assert len(crt.calls) == 1
        assert clock.slept == []

    def test_a_lock_that_is_never_released_refuses_at_the_ceiling(self, monkeypatch, lock_fd):
        mod, clock = _mod(), _VirtualClock()
        crt = _FakeCrt(clock, release_at=float("inf"))
        _on_windows(monkeypatch, mod, crt, clock)

        with pytest.raises(SystemExit) as excinfo:
            mod._flock(lock_fd, True)

        message = str(excinfo.value)
        assert "still held" in message
        assert "refusing to proceed unserialized" in message
        assert crt.held is False
        # The refusal lands at the ceiling, within a second of it, not at the CRT's ten.
        assert mod._LOCK_TIMEOUT_SECS <= clock.now <= mod._LOCK_TIMEOUT_SECS + 1.0

    def test_the_release_unlocks_without_polling(self, monkeypatch, lock_fd):
        mod, clock = _mod(), _VirtualClock()
        crt = _FakeCrt(clock)
        _on_windows(monkeypatch, mod, crt, clock)
        mod._flock(lock_fd, True)
        crt.calls.clear()

        mod._flock(lock_fd, False)

        assert crt.calls == [crt.LK_UNLCK]
        assert crt.held is False
        assert clock.slept == []


class TestMirrorsTheSharedHelper:
    def test_the_wait_uses_the_helpers_poll_and_ceiling(self):
        # The script runs outside the package and cannot import the helper, so its copy
        # of the two values is pinned to the helper's here.
        mod = _mod()

        assert mod._LOCK_POLL_SECS == platform_lock_compat._LOCK_POLL_SECS
        assert mod._LOCK_TIMEOUT_SECS == platform_lock_compat._LOCK_TIMEOUT_SECS


class TestLogLockFailurePath:
    def test_a_refused_acquire_unlocks_nothing_and_closes_the_descriptor(
        self, monkeypatch, tmp_path
    ):
        # Unlocking a range this process never held fails EACCES on Windows, which
        # would replace the refusal the caller needs to see.
        mod, clock = _mod(), _VirtualClock()
        crt = _FakeCrt(clock, release_at=float("inf"))
        _on_windows(monkeypatch, mod, crt, clock)

        with pytest.raises(SystemExit) as excinfo:
            with mod._log_lock(tmp_path / "log.jsonl"):
                pytest.fail("the critical section ran without the lock")

        assert "refusing to proceed unserialized" in str(excinfo.value)
        assert crt.LK_UNLCK not in crt.calls
        with pytest.raises(OSError):
            os.fstat(crt.fds[0])

    def test_a_taken_lock_is_released_when_the_transaction_raises(self, monkeypatch, tmp_path):
        mod, clock = _mod(), _VirtualClock()
        crt = _FakeCrt(clock)
        _on_windows(monkeypatch, mod, crt, clock)

        with pytest.raises(RuntimeError):
            with mod._log_lock(tmp_path / "log.jsonl"):
                raise RuntimeError("append failed")

        assert crt.calls[-1] == crt.LK_UNLCK
        assert crt.held is False
        with pytest.raises(OSError):
            os.fstat(crt.fds[0])


class TestPosixBranchIsUnchanged:
    def test_flock_blocks_in_the_kernel_and_never_polls(self, monkeypatch, lock_fd):
        mod, clock = _mod(), _VirtualClock()
        flock_calls = []
        fake_fcntl = types.SimpleNamespace(
            LOCK_EX=2, LOCK_UN=8, flock=lambda fd, op: flock_calls.append(op)
        )
        monkeypatch.setattr(mod, "sys", types.SimpleNamespace(platform="linux"))
        monkeypatch.setattr(mod, "fcntl", fake_fcntl, raising=False)
        monkeypatch.setattr(mod, "time", clock, raising=False)

        mod._flock(lock_fd, True)
        mod._flock(lock_fd, False)

        assert flock_calls == [fake_fcntl.LOCK_EX, fake_fcntl.LOCK_UN]
        assert clock.slept == []


class _KernelLockCrt:
    """A ``msvcrt`` stand-in over real kernel locks, for a host with no ``msvcrt``.

    ``flock`` conflicts between two open descriptions even inside one process, which is
    the property the Windows byte-range lock has between two handles. The blocking code
    gives up at once on a held lock, as ``_locking`` does once its retries are spent.
    """

    LK_UNLCK = 0
    LK_LOCK = 1
    LK_NBLCK = 2

    def locking(self, fd: int, mode: int, _nbytes: int) -> None:
        import fcntl

        if mode == self.LK_UNLCK:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            busy = EDEADLOCK if mode == self.LK_LOCK else errno.EACCES
            raise OSError(busy, os.strerror(busy)) from None


class _ObservedCrt:
    """Wraps a CRT and signals once the contender has been refused ``after`` times.

    ``errnos`` keeps what the CRT reported for each refusal, so a test can check that
    the errno of a real held range is one the wait reads as contention.
    """

    def __init__(self, inner, after: int) -> None:
        self.inner = inner
        self.after = after
        self.errnos: list[int | None] = []
        self.waiting = threading.Event()
        self.LK_UNLCK = inner.LK_UNLCK
        self.LK_LOCK = inner.LK_LOCK
        self.LK_NBLCK = inner.LK_NBLCK

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        try:
            self.inner.locking(fd, mode, nbytes)
        except OSError as exc:
            self.errnos.append(exc.errno)
            if len(self.errnos) >= self.after:
                self.waiting.set()
            raise


def _real_or_kernel_crt():
    if sys.platform == "win32":
        import msvcrt

        return msvcrt
    return _KernelLockCrt()


class TestRealContention:
    """Two descriptors on one lock file, with the holder released on a signal.

    On a Windows host the CRT is the real ``msvcrt``, so the errno a real held
    byte-range lock reports is the one the wait classifies as contention.
    """

    def test_a_contender_waits_for_the_holder_then_takes_the_lock(self, monkeypatch, tmp_path):
        mod = _mod()
        crt = _ObservedCrt(_real_or_kernel_crt(), after=3)
        monkeypatch.setattr(mod, "sys", types.SimpleNamespace(platform="win32"))
        monkeypatch.setattr(mod, "msvcrt", crt, raising=False)
        lock = str(tmp_path / "log.jsonl.lock")
        holder_fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        contender_fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        outcome: dict[str, object] = {}
        # The holder takes the lock through the unobserved CRT, so only the contender's
        # refusals are counted.
        os.lseek(holder_fd, 0, os.SEEK_SET)
        crt.inner.locking(holder_fd, crt.LK_NBLCK, 1)

        def contend():
            try:
                mod._flock(contender_fd, True)
                outcome["acquired"] = True
            except BaseException as exc:  # reported through the assertion below
                outcome["error"] = exc
                crt.waiting.set()

        thread = threading.Thread(target=contend)
        thread.start()
        try:
            assert crt.waiting.wait(timeout=60), "the contender was never refused"
            assert outcome == {}, "the contender gave up while the lock was held: %r" % outcome
            assert set(crt.errnos) <= set(mod._BUSY_ERRNOS), crt.errnos
            os.lseek(holder_fd, 0, os.SEEK_SET)
            crt.inner.locking(holder_fd, crt.LK_UNLCK, 1)
            thread.join(timeout=60)
            assert not thread.is_alive(), "the contender never took the released lock"
            assert outcome == {"acquired": True}
        finally:
            thread.join(timeout=60)
            os.close(holder_fd)
            os.close(contender_fd)


# Every blocking lock code of ``msvcrt``: each gives up with EDEADLOCK instead of waiting.
BLOCKING_CODES = frozenset({"LK_LOCK", "LK_RLCK"})


def _blocking_code_uses(tree: ast.AST) -> list[int]:
    """Line numbers of every reference to a blocking ``msvcrt`` lock code."""
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in BLOCKING_CODES
    ]


class TestNoCallerPassesABlockingCode:
    def test_no_module_under_src_passes_a_blocking_lock_code(self):
        offenders = [
            f"{path.name}:{line}"
            for path, _text, tree in parsed_candidates(require_any=tuple(BLOCKING_CODES))
            for line in _blocking_code_uses(tree)
        ]

        assert offenders == []

    @pytest.mark.parametrize(
        "source",
        [
            "msvcrt.locking(fd, msvcrt.LK_LOCK, 1)",
            "msvcrt.locking(fd, msvcrt.LK_LOCK if acquire else msvcrt.LK_UNLCK, 1)",
            "m.locking(fd, m.LK_RLCK, 1)",
        ],
    )
    def test_the_scan_flags_each_spelling_of_a_blocking_code(self, source):
        assert _blocking_code_uses(ast.parse(source)) != []

    def test_the_scan_passes_the_non_blocking_codes(self):
        source = "msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)\nmsvcrt.locking(fd, msvcrt.LK_UNLCK, 1)"

        assert _blocking_code_uses(ast.parse(source)) == []
