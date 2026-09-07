"""The deploy lock sidecars must open WRITABLE but NON-TRUNCATING.

``msvcrt.locking`` needs a writable handle, so the lock fd cannot be opened
``"r"``; but ``"w"`` truncates on open, and on Windows a truncating open of a
lock file whose first byte another holder already locked raises a sharing
violation instead of waiting — the contending acquirer crashes before it ever
reaches ``file_lock`` and mutual exclusion silently does not happen. POSIX
``flock`` tolerates the truncate, which is why the defect is invisible on
Linux. Same property the ``work_ledger._open_lock`` and ``session_pid`` fixes
pinned.

Truncation is the direct, platform-independent observable: seed each lock
sidecar with bytes, drive the public function that takes the lock, and assert
the bytes survive. Under ``open(lock_path, "w")`` every one of these fails on
every platform (the file is emptied); under ``touch`` + ``"r+"`` they pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kiro_crew.deploy.pending as pending
import kiro_crew.deploy.profiles as profiles

SENTINEL = b"held by a prior acquirer\n"


@pytest.fixture
def pending_lock(tmp_path, monkeypatch) -> Path:
    store = tmp_path / "deploy" / "pending-deploys.json"
    monkeypatch.setattr(pending, "_store_path", lambda: store)
    lock = store.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(SENTINEL)
    return lock


@pytest.fixture
def registry_lock(tmp_path, monkeypatch) -> Path:
    data_dir = tmp_path / "deploy"
    monkeypatch.setattr(profiles, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(profiles, "_registry_path", lambda: data_dir / "profiles.json")
    lock = data_dir / "profiles.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(SENTINEL)
    return lock


def test_add_pending_does_not_truncate_the_lock_file(pending_lock):
    pending.add_pending({"site_id": "s1"})
    assert pending_lock.read_bytes() == SENTINEL


def test_remove_pending_does_not_truncate_the_lock_file(pending_lock):
    pending.remove_pending("absent-id")
    assert pending_lock.read_bytes() == SENTINEL


def test_claim_pending_does_not_truncate_the_lock_file(pending_lock):
    assert pending.claim_pending("absent-id") is None
    assert pending_lock.read_bytes() == SENTINEL


def test_locked_registry_does_not_truncate_the_lock_file(registry_lock):
    with profiles.locked_registry():
        pass
    assert registry_lock.read_bytes() == SENTINEL


def test_save_registry_does_not_truncate_the_lock_file(registry_lock):
    profiles.save_registry({"version": 2, "profiles": [], "default": ""})
    assert registry_lock.read_bytes() == SENTINEL
