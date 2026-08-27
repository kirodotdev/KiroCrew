"""Tests for the shared JSONL rotation helper (``jsonl_util.rotate_jsonl_at``).

The helper owns ONLY the rotate-by-rename step its call sites share (the MCP
stub's fallback log, the subagents' slow-command log, member activity logs);
each site keeps its own append and error contract. These tests pin the
helper's contract directly; the per-site behavior stays pinned by each site's
own rotation tests.
"""

from __future__ import annotations

import os
import threading

from kiro_crew import platform_compat
from kiro_crew.jsonl_util import rotate_jsonl_at

CAP = 100  # bytes — small enough to cross with one write


class TestRotateAtCap:
    def test_rotates_once_the_cap_is_reached(self, tmp_path):
        live = tmp_path / "log.jsonl"
        live.write_bytes(b"x" * CAP)
        rotate_jsonl_at(live, CAP)
        rotated = tmp_path / "log.jsonl.1"
        assert rotated.exists(), "previous generation not kept"
        assert rotated.stat().st_size == CAP
        assert not live.exists(), "live file must restart empty via the caller's append"

    def test_does_not_rotate_under_the_cap(self, tmp_path):
        live = tmp_path / "log.jsonl"
        live.write_bytes(b"x" * (CAP - 1))
        rotate_jsonl_at(live, CAP)
        assert not (tmp_path / "log.jsonl.1").exists()
        assert live.stat().st_size == CAP - 1

    def test_keeps_exactly_one_generation(self, tmp_path):
        """A second rotation replaces ``.1`` — total disk stays ~2x the cap."""
        live = tmp_path / "log.jsonl"
        rotated = tmp_path / "log.jsonl.1"
        live.write_bytes(b"a" * CAP)
        rotate_jsonl_at(live, CAP)
        live.write_bytes(b"b" * CAP)
        rotate_jsonl_at(live, CAP)
        assert rotated.read_bytes() == b"b" * CAP
        assert not live.exists()


class TestBestEffort:
    """NEVER raises: any failure degrades to not rotating, so the caller's
    append still lands the record. Failures are induced with REAL ``OSError``s
    (a directory squatting on the target path) — no stdlib patching, which
    would leak process-wide to concurrent renamers."""

    def test_missing_live_file_is_a_no_op(self, tmp_path):
        rotate_jsonl_at(tmp_path / "absent.jsonl", CAP)  # must not raise
        assert not (tmp_path / "absent.jsonl.1").exists()

    def test_rename_failure_does_not_raise(self, tmp_path):
        live = tmp_path / "log.jsonl"
        live.write_bytes(b"x" * (CAP + 10))
        (tmp_path / "log.jsonl.1").mkdir()
        rotate_jsonl_at(live, CAP)  # must not raise
        assert (tmp_path / "log.jsonl.1").is_dir()
        assert live.exists(), "a failed rotation must leave the live file appendable"

    def test_lock_open_failure_does_not_raise(self, tmp_path):
        """An unopenable lock file (fd exhaustion, restrictive dir ACL) must
        degrade to not rotating — fd/disk exhaustion is a leading cause of the
        incidents these logs diagnose, so that is exactly when the caller's
        append must still be reachable."""
        live = tmp_path / "log.jsonl"
        live.write_bytes(b"x" * (CAP + 10))
        (tmp_path / "log.jsonl.lock").mkdir()
        rotate_jsonl_at(live, CAP)  # must not raise
        assert not (tmp_path / "log.jsonl.1").exists()
        assert live.exists()

    def test_unusable_path_value_does_not_raise(self, tmp_path):
        """The contract covers ``ValueError`` too (e.g. an embedded NUL, which
        ``os.open`` rejects as a value, not an OS failure) — the call sites'
        own handlers narrow to ``OSError``, so the promise must hold here."""
        rotate_jsonl_at(tmp_path / "log\x00name.jsonl", CAP)  # must not raise


class TestTryLock:
    def test_held_lock_skips_rotation_without_blocking(self, tmp_path):
        """A caller that loses the try-lock skips rotating and never waits —
        a blocking acquire would stall the caller's event loop for the
        duration of another writer's rotation."""
        live = tmp_path / "log.jsonl"
        live.write_bytes(b"x" * (CAP + 10))
        lock_fd = os.open(tmp_path / "log.jsonl.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            assert platform_compat.try_acquire_lock(lock_fd, exclusive=True)
            done = threading.Event()

            def rotate() -> None:
                rotate_jsonl_at(live, CAP)
                done.set()

            t = threading.Thread(target=rotate, daemon=True)
            t.start()
            t.join(timeout=10)
            assert done.is_set(), "rotation blocked on a held try-lock"
            assert not (tmp_path / "log.jsonl.1").exists()
        finally:
            platform_compat.release_lock(lock_fd)
            os.close(lock_fd)

    def test_lock_is_released_after_rotation(self, tmp_path):
        """The winner releases the lock: a second writer can rotate next."""
        live = tmp_path / "log.jsonl"
        live.write_bytes(b"x" * CAP)
        rotate_jsonl_at(live, CAP)
        lock_fd = os.open(tmp_path / "log.jsonl.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            assert platform_compat.try_acquire_lock(lock_fd, exclusive=True)
        finally:
            platform_compat.release_lock(lock_fd)
            os.close(lock_fd)
