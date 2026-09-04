"""issue_radar lock sidecars must be opened WRITABLE but WITHOUT truncation.

Regression for #9263 (part of the #9248 sweep). Every lock context manager in
``store.py`` and ``crew_store.py`` used ``open(lock_path, "w")`` and then handed
the descriptor to ``platform_compat.file_lock(..., exclusive=True)``. ``"w"``
truncates at open, BEFORE the lock is held. ``msvcrt.locking`` needs a writable
handle, so ``"r"`` is not an option either; but on Windows a truncating open of a
lock file whose first byte another holder already locked raises a sharing
violation instead of waiting, so the second contending acquirer crashes before it
ever reaches ``file_lock`` and the critical section is never mutually excluded.
POSIX ``flock`` tolerates the truncate, which is why the defect is Windows-only.

Truncation is the direct, platform-independent observable, exactly as the
work_ledger regression (#9237) pins it: seed the lock file with bytes, acquire
and release the lock, and assert the bytes survived. Under the old
``open(path, "w")`` these fail on every platform (the file is emptied); under the
``touch`` + ``"r+"`` open they pass, and the same non-truncating open is what
stops the Windows sharing violation.

The cases below cover both modules and all three path expressions the sweep
touched: a bound ``lock_path`` variable, a ``.with_suffix(".json.lock")`` cache
sidecar, and a per-item ``.lock`` name.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from kiro_crew.apps.builtins.issue_radar.backend import crew_store, store

SENTINEL = b"held by a prior acquirer\n"

OWNER = "o"
REPO = "r"


class TestLockOpenDoesNotTruncate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _assert_survives(self, lock_path: Path, run):
        """Seed *lock_path* with bytes, run the critical section, assert survival."""
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(SENTINEL)
        run()
        self.assertEqual(
            lock_path.read_bytes(),
            SENTINEL,
            f"acquiring the lock truncated {lock_path.name}",
        )

    # ── store.py ──────────────────────────────────────────────────────────

    def test_config_lock_does_not_truncate(self):
        lock_path = store.data_dir(self.tmp) / "config.json.lock"

        def run():
            with store._config_lock(self.tmp):
                pass

        self._assert_survives(lock_path, run)

    def test_issues_cache_lock_does_not_truncate(self):
        path = store.issues_cache_path(OWNER, REPO, self.tmp, "open")
        lock_path = path.with_suffix(".json.lock")

        def run():
            with store.issues_cache_lock(OWNER, REPO, self.tmp, state="open"):
                pass

        self._assert_survives(lock_path, run)

    def test_issue_write_lock_does_not_truncate(self):
        lock_path = store.repo_data_dir(OWNER, REPO, self.tmp) / "issue-7.write.lock"

        def run():
            with store.issue_write_lock(OWNER, REPO, 7, self.tmp):
                pass

        self._assert_survives(lock_path, run)

    # ── crew_store.py ─────────────────────────────────────────────────────

    def test_skip_lock_does_not_truncate(self):
        lock_path = crew_store._skip_lock_path(OWNER, REPO, 7, self.tmp)

        def run():
            with crew_store._skip_lock(OWNER, REPO, 7, self.tmp):
                pass

        self._assert_survives(lock_path, run)

    def test_write_settings_lock_does_not_truncate(self):
        lock_path = crew_store.crews_dir(OWNER, REPO, self.tmp) / "settings.lock"

        def run():
            crew_store.write_settings(OWNER, REPO, {}, self.tmp)

        self._assert_survives(lock_path, run)


if __name__ == "__main__":
    unittest.main()
