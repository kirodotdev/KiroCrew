"""Tests for the gateway singleton lock.

Covers the P1 acceptance criteria: a second gateway on the same KIROCREW_HOME is
refused naming the holder pid, a stale lock is reclaimed, and distinct homes
both start.
"""

import os

import pytest

from kiro_crew.gateway_lock import (
    LOCK_FILENAME,
    GatewayLock,
    GatewayLockError,
    _read_pid,
)


def test_acquire_creates_lock_file_with_pid(tmp_path):
    lock = GatewayLock(tmp_path).acquire()
    try:
        lock_file = tmp_path / LOCK_FILENAME
        assert lock_file.is_file()
        assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        lock.release()


def test_second_acquire_refused_and_names_holder(tmp_path):
    first = GatewayLock(tmp_path).acquire()
    try:
        with pytest.raises(GatewayLockError) as excinfo:
            GatewayLock(tmp_path).acquire()
        # flock is per open-file-description, so a second fd in the same process
        # is refused exactly as a second process would be.
        assert excinfo.value.holder_pid == os.getpid()
        assert str(tmp_path) in str(excinfo.value)
    finally:
        first.release()


def test_stale_lock_is_reclaimed(tmp_path):
    # A leftover lock file with a dead holder's pid but no held flock (the prior
    # process died -> the kernel released its lock). Acquire must succeed and
    # stamp our pid over the stale one.
    lock_file = tmp_path / LOCK_FILENAME
    lock_file.write_text("999999\n")  # pid that is not us and (effectively) dead

    lock = GatewayLock(tmp_path).acquire()
    try:
        assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        lock.release()


def test_distinct_homes_both_acquire(tmp_path):
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    lock_a = GatewayLock(home_a).acquire()
    lock_b = GatewayLock(home_b).acquire()
    try:
        assert (home_a / LOCK_FILENAME).is_file()
        assert (home_b / LOCK_FILENAME).is_file()
    finally:
        lock_a.release()
        lock_b.release()


def test_release_allows_reacquire(tmp_path):
    GatewayLock(tmp_path).acquire().release()
    # Lock is free again -> a fresh acquire succeeds.
    lock = GatewayLock(tmp_path).acquire()
    lock.release()


def test_release_is_idempotent(tmp_path):
    lock = GatewayLock(tmp_path).acquire()
    lock.release()
    lock.release()  # no raise


def test_context_manager_releases(tmp_path):
    with GatewayLock(tmp_path):
        with pytest.raises(GatewayLockError):
            GatewayLock(tmp_path).acquire()
    # Block exited -> lock released -> re-acquire works.
    GatewayLock(tmp_path).acquire().release()


def test_acquire_creates_missing_home(tmp_path):
    home = tmp_path / "does" / "not" / "exist"
    lock = GatewayLock(home).acquire()
    try:
        assert (home / LOCK_FILENAME).is_file()
    finally:
        lock.release()


def test_read_pid_handles_garbage(tmp_path):
    lock_file = tmp_path / LOCK_FILENAME
    lock_file.write_text("not-a-pid")
    fd = os.open(lock_file, os.O_RDONLY)
    try:
        assert _read_pid(fd) is None
    finally:
        os.close(fd)
