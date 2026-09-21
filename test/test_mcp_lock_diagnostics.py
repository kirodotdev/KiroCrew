"""``bridges._mcp_lock`` must REPORT why it could not take its sidecar (GH-11474).

The lock guarding ``~/.kiro/agents/kirocrew.json`` is a sidecar
``kirocrew.lock`` beside it. When that path cannot be opened -- a read-only
``~/.kiro/agents`` mount, or a lock file whose own mode was tightened -- the
open refuses, and every caller that catches the refusal treats this as
best-effort work at a level no operator reads:
``registered_app_mcp_servers`` returns ``{}`` on ANY exception and logs nothing
at all, and the boot path arrives through ``agent._install_worker_agent``, whose
failure is caught at ``logger.debug``. A gateway that skipped its agent-config
write therefore read in the log exactly like one that completed it, which is the
silence in the issue report.

The property pinned here is NOT that the refusal happens -- it already did, in
0.001s, before any lock syscall -- but that it is REPORTED, at WARNING, naming
the lock path and the filesystem's own reason, and that it still PROPAGATES: a
diagnostic that swallowed the error would hand callers a lock they do not hold.

Two remedies must stay distinguishable, because they send an operator to
different places: an unwritable path is fixed by moving ``KIRO_HOME``, while a
stuck holder is fixed by finding the process still holding the lock. And a
caller-body error must not be reported as a lock problem at all.
"""

from __future__ import annotations

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
from kiro_crew.apps import bridges
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="mode-based unwritable-path cases; Windows ACLs are a separate branch",
)

# Long enough that the holder is reliably up and the acquire really waits, short
# enough that a regression fails instead of stalling the shard.
_TEST_CEILING = 1.0

_KIRO_HOME_REMEDY = "Point KIRO_HOME at a writable directory"


@pytest.fixture
def agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An isolated stand-in for ``~/.kiro/agents``, restored writable on exit.

    The module-level override is the same hook every other agent-dir caller
    honours, so this never reaches the real home even when a case leaves the
    directory unwritable mid-test.
    """
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", d)
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


class TestUnwritableLockPathIsReported:
    """The read-only-mount half of the report: refusing was never the defect."""

    def test_unwritable_directory_is_reported_at_warning(
        self, agents_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # No sidecar yet, so creating it is what the filesystem refuses -- the
        # shape a read-only ~/.kiro/agents presents on a fresh install.
        os.chmod(agents_dir, stat.S_IRUSR | stat.S_IXUSR)  # 0o500
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.apps.bridges"):
            with pytest.raises(OSError) as exc_info:
                with bridges._mcp_lock():
                    pytest.fail("acquired a lock on an unwritable path")
        assert exc_info.value.errno in (errno.EACCES, errno.EROFS, errno.EPERM)
        warnings = _lock_warnings(caplog)
        assert warnings, (
            "an unwritable agent-config lock path was refused with nothing logged at "
            "WARNING, so a gateway that skipped its agent-config write is "
            "indistinguishable in the log from one that completed it"
        )
        msg = warnings[0]
        assert str(agents_dir / "kirocrew.lock") in msg, (
            "the report must name the lock path: which path is unwritable is the "
            "whole of what the operator has to act on"
        )
        assert _KIRO_HOME_REMEDY in msg
        assert os.strerror(exc_info.value.errno) in msg, (
            "the report must carry the filesystem's own reason -- 'Read-only file "
            "system' and 'Permission denied' call for different operator actions"
        )

    def test_unwritable_lock_file_is_reported_at_warning(
        self, agents_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A PRESENT sidecar whose own mode denies write: the lock fd must be
        # writable (Windows msvcrt.locking fails EACCES on a read-only handle),
        # so an existing file does not rescue this path.
        lock_path = agents_dir / "kirocrew.lock"
        lock_path.touch()
        os.chmod(lock_path, stat.S_IRUSR)  # 0o400
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.apps.bridges"):
            with pytest.raises(OSError):
                with bridges._mcp_lock():
                    pytest.fail("acquired a lock on an unwritable sidecar")
        warnings = _lock_warnings(caplog)
        assert warnings, "an unwritable sidecar was refused silently"
        assert str(lock_path) in warnings[0]

    def test_the_refusal_still_propagates(self, agents_dir: Path) -> None:
        # Reporting must not become handling. A swallowed refusal would run the
        # caller's read-modify-write with no lock held, which is the exact
        # fail-open that loses a concurrent writer's entry.
        os.chmod(agents_dir, stat.S_IRUSR | stat.S_IXUSR)  # 0o500
        entered = False
        with pytest.raises(OSError):
            with bridges._mcp_lock():
                entered = True
        assert not entered, "the critical section ran without the lock"

    def test_a_shared_read_lock_is_reported_too(
        self, agents_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # exclusive=False is the read path (_read_mcp_json). It needs the same
        # writable fd, so it fails identically and must report identically --
        # otherwise the silence just moves to whichever caller reads first.
        os.chmod(agents_dir, stat.S_IRUSR | stat.S_IXUSR)  # 0o500
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.apps.bridges"):
            with pytest.raises(OSError):
                with bridges._mcp_lock(exclusive=False):
                    pytest.fail("acquired a shared lock on an unwritable path")
        assert _lock_warnings(caplog), "the shared-lock refusal was silent"


class TestRemediesStayDistinct:
    """Two failures, two operator actions; one message for both helps nobody."""

    def test_a_stuck_holder_is_reported_without_the_kiro_home_remedy(
        self, agents_dir: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform_compat, "_LOCK_TIMEOUT_SECS", _TEST_CEILING)
        lock_path = agents_dir / "kirocrew.lock"
        holder = _spawn_holder(lock_path)
        try:
            with caplog.at_level(logging.DEBUG, logger="kiro_crew.apps.bridges"):
                with pytest.raises(OSError) as exc_info:
                    with bridges._mcp_lock():
                        pytest.fail("acquired a lock another process holds")
        finally:
            holder.kill()
            holder.wait(timeout=10)
        assert "refusing to proceed unserialized" in str(exc_info.value)
        warnings = _lock_warnings(caplog)
        assert warnings, "a bounded-acquire refusal was logged nowhere"
        msg = warnings[0]
        assert str(lock_path) in msg
        assert _KIRO_HOME_REMEDY not in msg, (
            "a stuck holder is not fixed by moving KIRO_HOME; sending the operator "
            "there wastes the one diagnostic they get"
        )

    def test_an_explicit_target_omits_the_kiro_home_remedy(
        self, agents_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The legacy shared mcp.json resolves from a fixed ``Path.home()`` that
        # ignores KIRO_HOME, so prescribing KIRO_HOME for it names a setting
        # that cannot move its lock. The config the lock guards is what stays
        # true for both targets, so that is what the message carries.
        target_dir = tmp_path / "settings"
        target_dir.mkdir()
        target = target_dir / "mcp.json"
        (target_dir / "mcp.lock").touch()
        os.chmod(target_dir / "mcp.lock", stat.S_IRUSR)  # 0o400
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.apps.bridges"):
            with pytest.raises(OSError):
                with bridges._mcp_lock(target=target):
                    pytest.fail("acquired a lock on an unwritable sidecar")
        warnings = _lock_warnings(caplog)
        assert warnings, "an explicit target's refusal was silent"
        msg = warnings[0]
        assert str(target_dir / "mcp.lock") in msg
        assert str(target) in msg, "the message must name the config the lock guards"
        assert (
            _KIRO_HOME_REMEDY not in msg
        ), "KIRO_HOME cannot move a lock resolved from a fixed Path.home()"


class TestNotEverythingIsALockProblem:
    """The report must cover the ACQUIRE alone."""

    def test_a_caller_body_error_is_not_reported_as_a_lock_failure(
        self, agents_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # An ENOSPC on the caller's own atomic write, inside a lock that was
        # taken cleanly. Logged as a lock failure it would send an operator
        # after a holder that does not exist while the disk is full.
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.apps.bridges"):
            with pytest.raises(OSError):
                with bridges._mcp_lock():
                    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        assert not _lock_warnings(caplog), (
            "a caller-body error was reported as a lock problem, which points the "
            "operator at the lock instead of at the disk"
        )

    def test_a_readonly_spec_file_does_not_trip_the_lock_report(
        self, agents_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The config itself unwritable while the sidecar is fine: the LOCK is
        # healthy, and claiming otherwise would misdirect the one permission
        # case an operator is most likely to hit.
        spec = agents_dir / "kirocrew.json"
        spec.write_text('{"mcpServers": {}}\n', encoding="utf-8")
        os.chmod(spec, stat.S_IRUSR)  # 0o400
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.apps.bridges"):
            with bridges._mcp_lock():
                pass
        assert not _lock_warnings(caplog)


class TestHealthyAcquireIsUnchanged:
    """A diagnostic that changed the working path would be a regression."""

    def test_normal_acquire_is_silent(
        self, agents_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.apps.bridges"):
            with bridges._mcp_lock():
                pass
        assert not _lock_warnings(caplog), "a successful acquire logged a lock warning"
        assert (agents_dir / "kirocrew.lock").exists()

    def test_the_sidecar_is_not_truncated(self, agents_dir: Path) -> None:
        # GH-9248: a truncating open empties the lock file BEFORE the lock is
        # held, so a contender can observe it flicker empty. Pinned here because
        # this acquire was rewritten.
        lock_path = agents_dir / "kirocrew.lock"
        lock_path.write_text("holder-pid 1234")
        with bridges._mcp_lock():
            pass
        assert lock_path.read_text() == "holder-pid 1234"

    def test_an_explicit_target_locks_its_own_sidecar(
        self, agents_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The legacy shared mcp.json sits under a DIFFERENT sidecar, and its
        # parent may not exist yet -- the acquire creates it.
        target = tmp_path / "settings" / "mcp.json"
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.apps.bridges"):
            with bridges._mcp_lock(target=target):
                pass
        assert (tmp_path / "settings" / "mcp.lock").exists()
        assert not (
            agents_dir / "kirocrew.lock"
        ).exists(), "an explicit target must not fall back to the default sidecar"
        assert not _lock_warnings(caplog)

    def test_an_unwritable_target_parent_is_reported(
        self, agents_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The mkdir is the third place this can refuse on a read-only mount,
        # and it must report through the same path as the open.
        root = tmp_path / "ro-root"
        root.mkdir()
        os.chmod(root, stat.S_IRUSR | stat.S_IXUSR)  # 0o500
        try:
            with caplog.at_level(logging.DEBUG, logger="kiro_crew.apps.bridges"):
                with pytest.raises(OSError):
                    with bridges._mcp_lock(target=root / "settings" / "mcp.json"):
                        pytest.fail("acquired a lock under an uncreatable parent")
            warnings = _lock_warnings(caplog)
            assert warnings, "an uncreatable lock parent was refused silently"
            assert str(root / "settings" / "mcp.lock") in warnings[0]
        finally:
            os.chmod(root, 0o700)  # nosemgrep: insecure-file-permissions
