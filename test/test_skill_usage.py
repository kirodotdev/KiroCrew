"""Tests for the skill-usage ledger (lazy-load hotness ranking)."""

from __future__ import annotations

import gc
import json
import os
import threading
import time
import weakref

import pytest

from kiro_crew import platform_compat, skill_usage
from kiro_crew.skill_usage import (
    _MAX_AGE_SECS,
    SkillUsageLedger,
)


def _ledger(tmp_path):
    return SkillUsageLedger(tmp_path / "skill-usage.json")


def _join_flush_threads(timeout: float = 2.0) -> None:
    """Wait out the background flush thread(s) ``record`` spawned, bounded.

    ``flush()`` snapshots under the ledger lock before any IO, so an overlapping
    write can only carry an older tally — a test that asserts persisted state
    must first let the spawn write finish.
    """
    for thread in threading.enumerate():
        if thread.name == "skill-usage-flush":
            thread.join(timeout)


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

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_a_non_finite_time_is_dropped_on_load(self, tmp_path, literal):
        # No TTL can expire a NaN or Infinity time, so the load drops the entry.
        # json.dumps writes these literals, and json.loads reads them back.
        (tmp_path / "skill-usage.json").write_text(
            '{"version": 1, "keys": {"bad": {"hits": 5, "last_seen": %s}, '
            '"good": {"hits": 1, "last_seen": %r}}}' % (literal, time.time())
        )
        led = _quiet(tmp_path)
        assert led.score("bad") == (0.0, 0.0)
        assert led.score("good")[0] == 1.0

        led.record("good")
        assert led.flush() is True
        saved = json.loads((tmp_path / "skill-usage.json").read_text())
        assert set(saved["keys"]) == {"good"}
        json.dumps(saved, allow_nan=False)

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


def _inside_window(led: SkillUsageLedger) -> SkillUsageLedger:
    """Park the debounce far in the future, so ``record`` never spawns a write.

    Unlike ``_quiet`` this does not depend on how much wall time passes between
    two lines: every ``record`` lands inside the window however slow the runner.
    """
    led._last_flush = time.time() + 3600
    return led


class _ObservedLock:
    """A lock that signals when a second caller starts waiting on it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries = 0
        self.second_waiting = threading.Event()

    def __enter__(self) -> None:
        self._entries += 1
        if self._entries == 2:
            self.second_waiting.set()
        self._lock.acquire()

    def __exit__(self, *_exc) -> None:
        self._lock.release()


class TestShutdownFlush:
    """A hit ``record()`` accepted inside the debounce window must reach disk.

    ``record`` only spawns the flush thread when ``_FLUSH_DEBOUNCE_SECS`` have
    passed since the last write, and that thread is a daemon. The gateway exits
    through ``os._exit``, so a restart or upgrade inside the window dropped every
    hit it had just accepted. ``flush_all_for_shutdown`` is the shutdown write;
    the debounce itself is unchanged.
    """

    def test_records_inside_window_still_do_not_write(self, tmp_path):
        # The debounce is a deliberate write-rate limit; the shutdown flush must
        # not turn record() into a write-per-hit path.
        led = _inside_window(_ledger(tmp_path))
        for i in range(50):
            led.record(f"k{i}")
        assert not (tmp_path / "skill-usage.json").exists()
        assert led.dirty
        led.flush()  # leave nothing dirty behind this test
        assert not led.dirty

    def test_flush_all_for_shutdown_writes_every_live_ledger(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skill_usage, "_live_ledgers", weakref.WeakSet())
        one = _inside_window(SkillUsageLedger(tmp_path / "one.json"))
        two = _inside_window(SkillUsageLedger(tmp_path / "two.json"))
        one.record("x")
        two.record("y")
        assert skill_usage.flush_all_for_shutdown() is True
        assert SkillUsageLedger(tmp_path / "one.json").score("x")[0] == 1.0
        assert SkillUsageLedger(tmp_path / "two.json").score("y")[0] == 1.0

    def test_flush_all_for_shutdown_reports_a_failed_write(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skill_usage, "_live_ledgers", weakref.WeakSet())
        led = _inside_window(_ledger(tmp_path))
        led.record("x")

        def _disk_full(*_a, **_k):
            raise OSError("no space left on device")

        monkeypatch.setattr(skill_usage, "atomic_write", _disk_full)
        assert skill_usage.flush_all_for_shutdown() is False
        assert led.dirty, "a failed write stays dirty so a later flush retries"

    def test_flush_all_for_shutdown_with_no_ledger(self, monkeypatch):
        monkeypatch.setattr(skill_usage, "_live_ledgers", weakref.WeakSet())
        assert skill_usage.flush_all_for_shutdown() is True

    def test_the_registry_does_not_pin_a_ledger(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skill_usage, "_live_ledgers", weakref.WeakSet())
        led = _ledger(tmp_path)
        ref = weakref.ref(led)
        del led
        gc.collect()
        assert ref() is None
        assert len(skill_usage._live_ledgers) == 0

    def test_an_older_inflight_write_cannot_land_after_the_shutdown_flush(
        self, tmp_path, monkeypatch
    ):
        # A daemon flush took its snapshot ({a}) and is mid-write when the
        # shutdown flush runs with {a, b}. Writes are serialized, so the shutdown
        # flush waits and the newer tally is what stays on disk.
        real_write = skill_usage.atomic_write
        first_started = threading.Event()
        release_first = threading.Event()
        calls = []

        def _slow_first(path, data, **kw):
            calls.append(set(json.loads(data)["keys"]))
            if len(calls) == 1:
                first_started.set()
                release_first.wait(5)
            return real_write(path, data, **kw)

        monkeypatch.setattr(skill_usage, "atomic_write", _slow_first)
        led = _inside_window(_ledger(tmp_path))
        lock = _ObservedLock()
        led._write_lock = lock
        led.record("a")
        daemon = threading.Thread(target=led.flush)
        daemon.start()
        assert first_started.wait(5)
        led.record("b")
        shutdown = threading.Thread(target=led.flush)
        shutdown.start()
        assert lock.second_waiting.wait(5)
        assert calls == [{"a"}], "the shutdown flush must not write past the in-flight one"
        release_first.set()
        daemon.join(5)
        shutdown.join(5)
        assert calls == [{"a"}, {"a", "b"}]
        saved = json.loads((tmp_path / "skill-usage.json").read_text())
        assert set(saved["keys"]) == {"a", "b"}
