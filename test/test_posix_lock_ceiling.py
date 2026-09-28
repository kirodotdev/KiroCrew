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
import time
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.subprocess_utf8 import UTF8_TEXT

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
    def test_contended_wait_raises_instead_of_hanging(self, held_lock: Path):
        # Both halves matter: that the call comes back at all, and that it
        # refuses rather than proceeding unserialized.
        fd = os.open(held_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            started = time.monotonic()
            with pytest.raises(OSError) as excinfo:
                with platform_compat.file_lock(fd, exclusive=True, wait=True):
                    pytest.fail("entered the critical section while another process held it")
            waited = time.monotonic() - started
        finally:
            os.close(fd)

        # It genuinely waits (does not degrade to a non-blocking single shot),
        # and gives up near the ceiling rather than at some unrelated time.
        assert waited >= _TEST_CEILING
        assert waited < _TEST_CEILING + 30

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
        assert f"{_TEST_CEILING:g}s" in message
        assert "stuck" in message
        assert "unserialized" in message

    def test_uncontended_acquire_still_succeeds(self, tmp_path: Path):
        # The ceiling must cost the common case nothing: a free lock is taken
        # with no delay and no raise.
        fd = os.open(tmp_path / "free.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            started = time.monotonic()
            with platform_compat.file_lock(fd, exclusive=True, wait=True):
                entered = True
            assert entered
            assert time.monotonic() - started < 5
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

    def test_non_waiting_acquire_still_fails_fast(self, held_lock: Path):
        # wait=False is a DIFFERENT contract (raise at once, BlockingIOError) and
        # the ceiling must not absorb it into a one-second wait.
        fd = os.open(held_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            started = time.monotonic()
            with pytest.raises(BlockingIOError):
                with platform_compat.file_lock(fd, exclusive=True, wait=False):
                    pass
            assert time.monotonic() - started < _TEST_CEILING
        finally:
            os.close(fd)

    def test_bad_fd_surfaces_now_not_at_the_ceiling(self, tmp_path: Path, monkeypatch):
        # Only "someone holds it" is retryable. An error about THIS fd must not
        # be swallowed into the poll loop and reported as a stuck holder -- that
        # would hide a real defect behind a five-minute wait.
        monkeypatch.setattr(platform_compat, "_LOCK_TIMEOUT_SECS", 30.0)
        fd = os.open(tmp_path / "closed.lock", os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        started = time.monotonic()
        with pytest.raises(OSError) as excinfo:
            with platform_compat.file_lock(fd, exclusive=True, wait=True):
                pass
        assert time.monotonic() - started < 5
        assert "stuck" not in str(excinfo.value)


class TestCallerSuppliedCeiling:
    """A holder whose work legitimately outlasts the default ceiling."""

    def test_a_longer_caller_ceiling_outlasts_the_default(self, held_lock: Path):
        # The default ceiling suits a sub-second critical section. A caller that
        # holds the lock across an install or a build must be able to wait for
        # that work, or a contender is refused while the holder is still running.
        caller_ceiling = _TEST_CEILING * 3
        fd = os.open(held_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            started = time.monotonic()
            with pytest.raises(OSError):
                with platform_compat.file_lock(
                    fd, exclusive=True, wait=True, timeout=caller_ceiling
                ):
                    pytest.fail("entered the critical section while another process held it")
            waited = time.monotonic() - started
        finally:
            os.close(fd)

        # Waiting past the patched default is the whole point: a wait that ends
        # at _TEST_CEILING means the caller's value was ignored.
        assert waited >= caller_ceiling
        assert waited < caller_ceiling + 30

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
        assert f"{caller_ceiling:g}s" in message
        assert f"{_TEST_CEILING:g}s" not in message

    def test_omitting_the_ceiling_still_uses_the_module_default(self, held_lock: Path):
        # The parameter is additive: a caller that passes nothing is bounded by
        # the default exactly as before.
        fd = os.open(held_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            started = time.monotonic()
            with pytest.raises(OSError) as excinfo:
                with platform_compat.file_lock(fd, exclusive=True, wait=True):
                    pass
            waited = time.monotonic() - started
        finally:
            os.close(fd)

        assert waited >= _TEST_CEILING
        assert waited < _TEST_CEILING + 30
        assert f"{_TEST_CEILING:g}s" in str(excinfo.value)


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


class TestOnLoopNeverSleeps:
    """A contended acquire on the event-loop thread must not sleep there."""

    def test_a_contended_on_loop_acquire_refuses_at_once(self, held_lock: Path):
        # A poll-sleep on the loop freezes chat and heartbeat for the whole wait,
        # and a freeze long enough to miss a heartbeat is a supervisor kill. So a
        # contended on-loop acquire fails closed instead of waiting.
        slept: list[float] = []

        async def _acquire_on_loop() -> None:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(time, "sleep", lambda s: slept.append(s))
                fd = os.open(held_lock, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    started = time.monotonic()
                    with pytest.raises(OSError):
                        with platform_compat.file_lock(fd, exclusive=True, wait=True):
                            pytest.fail("entered the critical section while held")
                    # Refused promptly, not at the ceiling.
                    assert time.monotonic() - started < _TEST_CEILING
                finally:
                    os.close(fd)

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


class TestPollBackoff:
    """The poll stands in for a zero-cost kernel sleep, so its wakeups are bounded."""

    def test_backoff_caps_the_sleep_and_never_overshoots_the_deadline(self, monkeypatch):
        # No real lock here: the SLEEP SCHEDULE is the subject, so the acquire is
        # stubbed permanently-held and every sleep is recorded instead of taken.
        slept: list[float] = []
        monkeypatch.setattr(platform_compat, "_LOCK_TIMEOUT_SECS", 5.0)

        def _always_held(fd, mode):
            raise OSError(errno.EAGAIN, "held")

        clock = {"now": 0.0}

        def _fake_sleep(secs: float) -> None:
            slept.append(secs)
            clock["now"] += secs

        monkeypatch.setattr(platform_compat.fcntl, "flock", _always_held)
        monkeypatch.setattr(platform_compat.time, "sleep", _fake_sleep)
        monkeypatch.setattr(platform_compat.time, "monotonic", lambda: clock["now"])

        assert platform_compat._posix_acquire_blocking(0, 0) is False

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
            with pytest.raises(OSError, match="stuck"):
                platform_compat.acquire_lock(fd, exclusive=True)
            assert time.monotonic() - started >= _TEST_CEILING
        finally:
            os.close(fd)

    def test_acquire_lock_still_takes_a_free_lock(self, tmp_path: Path):
        fd = os.open(tmp_path / "free2.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            platform_compat.acquire_lock(fd, exclusive=True)
            platform_compat.release_lock(fd)
        finally:
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
            with pytest.raises(OSError, match="stuck"):
                with agents_spec_lock(agents_dir):
                    pytest.fail("took the agent-spec lock while another process held it")
        finally:
            proc.kill()
            proc.wait(timeout=10)
            if proc.stdout is not None:
                proc.stdout.close()

    def test_unwritable_lock_path_fails_fast(self, tmp_path: Path):
        # An unwritable lock path is refused by ``os.open`` before any lock is
        # attempted. Pinned so a "retry until the ceiling" change cannot turn
        # this prompt refusal into a wait.
        from kiro_crew.agent import agents_spec_lock

        agents_dir = tmp_path / "readonly"
        agents_dir.mkdir()
        os.chmod(agents_dir, 0o500)
        try:
            started = time.monotonic()
            with pytest.raises(OSError) as excinfo:
                with agents_spec_lock(agents_dir):
                    pytest.fail("acquired a lock under an unwritable directory")
            assert time.monotonic() - started < 5
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
                with pytest.raises(OSError, match="stuck"):
                    with agents_spec_lock(agents_dir):
                        pytest.fail("took the lock while another process held it")
        finally:
            proc.kill()
            proc.wait(timeout=10)
            if proc.stdout is not None:
                proc.stdout.close()

        messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("stuck" in m for m in messages), messages
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
