"""Tests for the skill-usage ledger (lazy-load hotness ranking)."""

from __future__ import annotations

import json
import os
import time

from kiro_crew import platform_compat, skill_usage
from kiro_crew.skill_usage import (
    _MAX_AGE_SECS,
    SkillUsageLedger,
)


def _ledger(tmp_path):
    return SkillUsageLedger(tmp_path / "skill-usage.json")


class TestSkillUsageLedger:
    def test_record_bumps_hits(self, tmp_path):
        led = _ledger(tmp_path)
        led.record("a")
        led.record("a")
        led.record("b")
        assert led.score("a")[0] == 2.0
        assert led.score("b")[0] == 1.0
        assert led.score("never")[0] == 0.0

    def test_score_orders_by_hits(self, tmp_path):
        led = _ledger(tmp_path)
        led.record("hot")
        led.record("hot")
        led.record("cold")
        assert led.score("hot") > led.score("cold")

    def test_recency_boost_lifts_unused(self, tmp_path):
        led = _ledger(tmp_path)
        # An unused skill with a recency boost still outranks an unused skill
        # with none (cold-start protection), but never beats a used one on hits.
        boosted = led.score("new", recency_boost=time.time())
        plain = led.score("stale", recency_boost=0.0)
        assert boosted[0] == plain[0] == 0.0
        assert boosted[1] > plain[1]

    def test_persist_roundtrip(self, tmp_path):
        led = _ledger(tmp_path)
        # Suppress the debounced background-thread flush so this test exercises
        # the explicit flush deterministically (no race between the two writers).
        led._last_flush = time.time()
        led.record("x")
        led.record("x")
        assert led.flush() is True
        # A fresh ledger over the same path restores the tally.
        led2 = _ledger(tmp_path)
        assert led2.score("x")[0] == 2.0

    def test_missing_file_is_empty(self, tmp_path):
        led = _ledger(tmp_path)  # no file yet
        assert led.score("anything")[0] == 0.0

    def test_corrupt_file_is_ignored(self, tmp_path):
        (tmp_path / "skill-usage.json").write_text("{ not json ]")
        led = _ledger(tmp_path)  # must not raise
        assert led.score("anything")[0] == 0.0

    def test_ttl_drops_stale_entries_on_load(self, tmp_path):
        stale_ts = time.time() - _MAX_AGE_SECS - 100
        fresh_ts = time.time()
        (tmp_path / "skill-usage.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "keys": {
                        "old": {"hits": 99, "last_seen": stale_ts},
                        "new": {"hits": 1, "last_seen": fresh_ts},
                    },
                }
            )
        )
        led = _ledger(tmp_path)
        assert led.score("old")[0] == 0.0  # dropped by TTL
        assert led.score("new")[0] == 1.0

    def test_flush_noop_when_clean(self, tmp_path):
        led = _ledger(tmp_path)
        assert led.flush() is False  # nothing recorded → nothing to write


def _quiet(tmp_path) -> SkillUsageLedger:
    """A ledger whose debounce is armed, so the explicit flush is the only writer.

    ``_last_flush`` starts at 0.0, so the first ``record`` would spawn the
    background flush thread and race the assertions below — it snapshots under
    the lock, so it can win the rename carrying a different tally.
    """
    led = _ledger(tmp_path)
    led._last_flush = time.time()
    return led


class TestPersistenceWithoutPosixFchmod:
    """The ledger has to persist where ``os.fchmod`` does not exist.

    The attribute is hidden rather than these tests being confined to Windows, so
    the guard runs on the POSIX matrix that carries most of CI. ``IS_POSIX`` is
    flipped with it because ``fchmod_safe`` only reaches ``os.fchmod`` on POSIX:
    deleting the name alone would exercise a combination no platform is ever in.
    """

    @staticmethod
    def _as_windows(monkeypatch) -> None:
        monkeypatch.delattr(os, "fchmod", raising=False)
        assert not hasattr(os, "fchmod"), "precondition: os.fchmod must be hidden"
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

    def test_flush_persists_when_os_fchmod_is_absent(self, tmp_path, monkeypatch):
        self._as_windows(monkeypatch)
        led = _quiet(tmp_path)
        led.record("x")
        led.record("x")

        assert led.flush() is True, "the tally must reach disk, not be dropped"
        # Assert the RELOAD, not the in-memory tally: a fresh ledger is what the
        # ranking consults on the next run, and only that proves persistence.
        assert _ledger(tmp_path).score("x")[0] == 2.0
        # Cleared before the write and left cleared on success — a re-armed flag
        # here would mean a successful write was recorded as failed.
        assert led._dirty is False

    def test_failed_write_rearms_dirty_so_the_next_flush_retries(self, tmp_path, monkeypatch):
        self._as_windows(monkeypatch)
        real = skill_usage.atomic_write
        attempts: list[object] = []

        def flaky(path, content, **kwargs):
            attempts.append(path)
            if len(attempts) == 1:
                raise OSError("transient write failure")
            return real(path, content, **kwargs)

        monkeypatch.setattr(skill_usage, "atomic_write", flaky)
        led = _quiet(tmp_path)
        led.record("x")

        assert led.flush() is False
        assert led._dirty is True, "a failed write must re-arm, or the tally is lost"
        assert not (tmp_path / "skill-usage.json").exists()

        assert led.flush() is True
        assert len(attempts) == 2
        assert _ledger(tmp_path).score("x")[0] == 1.0
