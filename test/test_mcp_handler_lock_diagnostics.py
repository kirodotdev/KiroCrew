"""The dashboard MCP lock must REPORT why it could not be taken (GH-12668).

``_McpFileLock`` and ``_McpFileLockSync`` guard ``~/.kiro/settings/mcp.json``
with a sidecar ``mcp.lock`` beside it. When that sidecar cannot be opened -- a
read-only settings mount, or a lock file whose own mode denies write -- the open
refuses, and callers that treat the guarded write as best-effort swallow the
refusal at a level no operator reads. A gateway that skipped its mcp.json write
therefore read in the log exactly like one that completed it, the same silence
as its two siblings ``agent.agents_spec_lock`` (GH-11664) and
``apps.bridges._mcp_lock`` (GH-11474).

The property pinned here is NOT that the refusal happens -- it already did,
before any lock syscall -- but that it is REPORTED, at WARNING, naming the lock
path and the filesystem's own reason, and that it still PROPAGATES: a diagnostic
that swallowed the error would hand callers a lock they do not hold.

Two extra distinctions are load-bearing for these two classes specifically:

- The KIRO_HOME remedy the siblings print for their default sidecar must NOT
  appear here: ``_GLOBAL_MCP_JSON`` resolves from a fixed ``Path.home()`` that
  ignores KIRO_HOME, so prescribing it would send an operator to a setting that
  cannot move this lock.
- The async acquire runs in an executor and is deliberately guarded by a
  ``BaseException`` handler for fd-leak safety. A ``CancelledError`` while the
  acquire is pending is the CALLER's lifecycle, not a lock refusal, and must not
  be reported as one.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import stat
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="mode-based unwritable-path cases; Windows ACLs are a separate branch",
)

# Long enough that the holder is reliably up and the acquire really waits, short
# enough that a defect fails instead of stalling the shard.
_TEST_CEILING = 1.0

_KIRO_HOME_REMEDY = "KIRO_HOME"

_LOGGER_NAME = "kiro_crew.dashboard.handlers.mcp"


@pytest.fixture
def settings_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An isolated stand-in for ``~/.kiro/settings``, restored writable on exit.

    Patching BOTH module globals is required: ``_MCP_LOCK_PATH`` is derived from
    ``_GLOBAL_MCP_JSON`` at import time, so patching only the json path would
    leave the lock aimed at the real home.
    """
    d = tmp_path / "settings"
    d.mkdir()
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", d / "mcp.json")
    monkeypatch.setattr(mcp_mod, "_MCP_LOCK_PATH", d / "mcp.lock")
    try:
        yield d
    finally:
        # Restore owner rwx so pytest's tmp_path teardown can remove the tree.
        # 0o700 (not the rule's suggested 0o644) is owner-only AND keeps the
        # traversal bit the cleanup needs.
        os.chmod(d, 0o700)  # nosemgrep: insecure-file-permissions


def _lock_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and "lock" in r.getMessage()
    ]


def _spawn_holder(lock_path: Path, hold_secs: float = 60.0) -> subprocess.Popen:
    """Start a separate process holding an exclusive flock on *lock_path*.

    A real second process, not a second fd here: ``flock`` is per-open-file, so
    two fds in one process do not contend the way a gateway and a stuck prior
    generation do. Returns only once the child confirms the lock is held.
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
        # Explicit cwd rather than inheriting pytest's, so no later edit here
        # can leave an artifact in the repository tree.
        cwd=lock_path.parent,
        **UTF8_TEXT,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held", "holder failed to take the lock"
    return proc


def _acquire_async_lock() -> None:
    """Enter and exit ``_McpFileLock`` once, from a fresh event loop."""

    async def _use() -> None:
        async with mcp_mod._McpFileLock():
            pass

    asyncio.run(_use())


class TestUnwritableLockPathIsReported:
    """The read-only-mount half of the report: refusing was never the defect."""

    def test_async_unwritable_directory_is_reported_at_warning(
        self, settings_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # No sidecar yet, so creating it is what the filesystem refuses -- the
        # shape a read-only ~/.kiro/settings presents on a fresh install.
        os.chmod(settings_dir, stat.S_IRUSR | stat.S_IXUSR)  # 0o500
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            with pytest.raises(OSError) as exc_info:
                _acquire_async_lock()
        assert exc_info.value.errno in (errno.EACCES, errno.EROFS, errno.EPERM)
        warnings = _lock_warnings(caplog)
        assert warnings, (
            "an unwritable mcp config lock path was refused with nothing logged at "
            "WARNING, so a gateway that skipped its mcp.json write is "
            "indistinguishable in the log from one that completed it"
        )
        msg = warnings[0]
        assert str(settings_dir / "mcp.lock") in msg, (
            "the report must name the lock path: which path is unwritable is the "
            "whole of what the operator has to act on"
        )
        assert (
            str(settings_dir / "mcp.json") in msg
        ), "the message must name the config the lock guards"
        assert os.strerror(exc_info.value.errno) in msg, (
            "the report must carry the filesystem's own reason -- 'Read-only file "
            "system' and 'Permission denied' call for different operator actions"
        )
        assert _KIRO_HOME_REMEDY not in msg, (
            "this lock resolves from a fixed Path.home() that ignores KIRO_HOME, so "
            "prescribing KIRO_HOME names a setting that cannot move it"
        )

    def test_sync_unwritable_directory_is_reported_at_warning(
        self, settings_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        os.chmod(settings_dir, stat.S_IRUSR | stat.S_IXUSR)  # 0o500
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            with pytest.raises(OSError) as exc_info:
                with mcp_mod._McpFileLockSync():
                    pytest.fail("acquired a lock on an unwritable path")
        assert exc_info.value.errno in (errno.EACCES, errno.EROFS, errno.EPERM)
        warnings = _lock_warnings(caplog)
        assert warnings, "the sync class refused an unwritable path silently"
        msg = warnings[0]
        assert str(settings_dir / "mcp.lock") in msg
        assert _KIRO_HOME_REMEDY not in msg

    def test_async_unwritable_lock_file_is_reported_at_warning(
        self, settings_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A PRESENT sidecar whose own mode denies write: the lock fd must be
        # writable (Windows msvcrt.locking fails EACCES on a read-only handle),
        # so an existing file does not rescue this path.
        lock_path = settings_dir / "mcp.lock"
        lock_path.touch()
        os.chmod(lock_path, stat.S_IRUSR)  # 0o400
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            with pytest.raises(OSError):
                _acquire_async_lock()
        warnings = _lock_warnings(caplog)
        assert warnings, "an unwritable sidecar was refused silently"
        assert str(lock_path) in warnings[0]

    def test_sync_unwritable_lock_file_is_reported_at_warning(
        self, settings_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        lock_path = settings_dir / "mcp.lock"
        lock_path.touch()
        os.chmod(lock_path, stat.S_IRUSR)  # 0o400
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            with pytest.raises(OSError):
                with mcp_mod._McpFileLockSync():
                    pytest.fail("acquired a lock on an unwritable sidecar")
        warnings = _lock_warnings(caplog)
        assert warnings, "the sync class refused an unwritable sidecar silently"
        assert str(lock_path) in warnings[0]

    def test_async_uncreatable_parent_is_reported(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The mkdir is the third place this can refuse on a read-only mount,
        # and it must report through the same path as the open.
        root = tmp_path / "ro-root"
        root.mkdir()
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", root / "settings" / "mcp.json")
        monkeypatch.setattr(mcp_mod, "_MCP_LOCK_PATH", root / "settings" / "mcp.lock")
        os.chmod(root, stat.S_IRUSR | stat.S_IXUSR)  # 0o500
        try:
            with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
                with pytest.raises(OSError):
                    _acquire_async_lock()
            warnings = _lock_warnings(caplog)
            assert warnings, "an uncreatable lock parent was refused silently"
            assert str(root / "settings" / "mcp.lock") in warnings[0]
        finally:
            os.chmod(root, 0o700)  # nosemgrep: insecure-file-permissions

    def test_the_refusal_still_propagates(self, settings_dir: Path) -> None:
        # Reporting must not become handling. A swallowed refusal would run the
        # caller's read-modify-write with no lock held, which is the exact
        # fail-open that loses a concurrent writer's entry.
        os.chmod(settings_dir, stat.S_IRUSR | stat.S_IXUSR)  # 0o500
        entered = False

        async def _use() -> None:
            nonlocal entered
            async with mcp_mod._McpFileLock():
                entered = True

        with pytest.raises(OSError):
            asyncio.run(_use())
        assert not entered, "the critical section ran without the lock"


class TestRemediesStayDistinct:
    """Two failures, two operator actions; one message for both helps nobody."""

    def test_async_stuck_holder_is_reported_without_a_path_remedy(
        self,
        settings_dir: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(platform_compat, "_LOCK_TIMEOUT_SECS", _TEST_CEILING)
        lock_path = settings_dir / "mcp.lock"
        holder = _spawn_holder(lock_path)
        try:
            with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
                with pytest.raises(OSError) as exc_info:
                    _acquire_async_lock()
        finally:
            holder.kill()
            holder.wait(timeout=10)
        assert "refusing to proceed unserialized" in str(exc_info.value)
        warnings = _lock_warnings(caplog)
        assert warnings, "a bounded-acquire refusal was logged nowhere"
        msg = warnings[0]
        assert str(lock_path) in msg
        assert _KIRO_HOME_REMEDY not in msg, (
            "a stuck holder is not fixed by moving anything on disk; sending the "
            "operator there wastes the one diagnostic they get"
        )

    def test_sync_stuck_holder_is_reported(
        self,
        settings_dir: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(platform_compat, "_LOCK_TIMEOUT_SECS", _TEST_CEILING)
        lock_path = settings_dir / "mcp.lock"
        holder = _spawn_holder(lock_path)
        try:
            with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
                with pytest.raises(OSError) as exc_info:
                    with mcp_mod._McpFileLockSync():
                        pytest.fail("acquired a lock another process holds")
        finally:
            holder.kill()
            holder.wait(timeout=10)
        assert "refusing to proceed unserialized" in str(exc_info.value)
        warnings = _lock_warnings(caplog)
        assert warnings, "the sync class logged a bounded-acquire refusal nowhere"
        assert str(lock_path) in warnings[0]


class TestNotEverythingIsALockProblem:
    """The report must cover the setup and the acquire alone."""

    def test_a_caller_body_error_is_not_reported_as_a_lock_failure(
        self, settings_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # An ENOSPC on the caller's own atomic write, inside a lock that was
        # taken cleanly. Logged as a lock failure it would send an operator
        # after a holder that does not exist while the disk is full.
        async def _use() -> None:
            async with mcp_mod._McpFileLock():
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            with pytest.raises(OSError):
                asyncio.run(_use())
        assert not _lock_warnings(caplog), (
            "a caller-body error was reported as a lock problem, which points the "
            "operator at the lock instead of at the disk"
        )

    def test_sync_caller_body_error_is_not_reported(
        self, settings_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            with pytest.raises(OSError):
                with mcp_mod._McpFileLockSync():
                    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        assert not _lock_warnings(caplog)

    def test_a_cancelled_pending_acquire_is_not_reported(
        self,
        settings_dir: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A CancelledError while the executor acquire is pending is the
        # caller's lifecycle -- request teardown, shutdown -- not a lock
        # refusal. Reporting it would mislabel every cancelled request as a
        # stuck holder, in the very diagnostic this report exists to add.
        monkeypatch.setattr(platform_compat, "_LOCK_TIMEOUT_SECS", _TEST_CEILING)
        lock_path = settings_dir / "mcp.lock"
        holder = _spawn_holder(lock_path)

        async def _cancel_mid_acquire() -> None:
            async def _use() -> None:
                async with mcp_mod._McpFileLock():
                    pytest.fail("acquired a lock another process holds")

            task = asyncio.ensure_future(_use())
            # Let the task reach the executor wait before cancelling it.
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        try:
            with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
                asyncio.run(_cancel_mid_acquire())
        finally:
            holder.kill()
            holder.wait(timeout=10)
        assert not _lock_warnings(caplog), (
            "a cancellation while the acquire was pending was reported as a lock "
            "failure, sending an operator after a holder that is not the fault"
        )


class TestHealthyAcquireIsUnchanged:
    """A diagnostic that changed the working path would be a regression."""

    def test_async_normal_acquire_is_silent(
        self, settings_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            _acquire_async_lock()
        assert not _lock_warnings(caplog), "a successful acquire logged a lock warning"
        assert (settings_dir / "mcp.lock").exists()

    def test_sync_normal_acquire_is_silent(
        self, settings_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            with mcp_mod._McpFileLockSync():
                pass
        assert not _lock_warnings(caplog), "a successful sync acquire logged a lock warning"
        assert (settings_dir / "mcp.lock").exists()

    def test_the_sidecar_is_not_truncated(self, settings_dir: Path) -> None:
        # GH-9248: a truncating open empties the lock file BEFORE the lock is
        # held, so a contender can observe it flicker empty. Pinned here because
        # this acquire's failure handling was rewritten.
        lock_path = settings_dir / "mcp.lock"
        lock_path.write_text("holder-pid 1234")
        _acquire_async_lock()
        with mcp_mod._McpFileLockSync():
            pass
        assert lock_path.read_text() == "holder-pid 1234"
