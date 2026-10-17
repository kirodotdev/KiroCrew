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


class TestShutdownFlush:
    """A hit ``record()`` accepted inside the debounce window must reach disk.

    ``record`` only spawns the flush thread when ``_FLUSH_DEBOUNCE_SECS`` have
    passed since the last write, and that thread is a daemon — so a gateway that
    exits inside the window (restart, upgrade) dropped every hit it had just
    accepted: nothing flushed on shutdown, and the tally lived only in memory
    until some later ``record`` fell outside the window. These tests pin the
    shutdown flush without weakening the write-rate limit the debounce exists
    for.
    """

    def test_exit_inside_debounce_window_keeps_accepted_hits(self, tmp_path):
        led = _ledger(tmp_path)
        led.record("a")
        _join_flush_threads()  # the first record always flushes (_last_flush starts at 0.0)
        assert _ledger(tmp_path).score("a")[0] == 1.0

        # Arm the debounce, so the next hit lands inside the 60 s window.
        led._last_flush = time.time()
        led.record("b")
        saved = json.loads((tmp_path / "skill-usage.json").read_text())
        assert set(saved["keys"]) == {"a"}, "precondition: b is debounced, not written"

        # The gateway exits inside the window; no later record() falls outside it.
        led.close()
        assert (
            _ledger(tmp_path).score("b")[0] == 1.0
        ), "a hit record() accepted must survive an exit inside the debounce window"

    def test_close_is_idempotent(self, tmp_path):
        led = _ledger(tmp_path)
        led.record("a")
        _join_flush_threads()
        led.close()
        led.close()  # must not raise or double-write
        assert _ledger(tmp_path).score("a")[0] == 1.0

    def test_deferred_flush_writes_when_window_expires(self, tmp_path, monkeypatch):
        # A hit inside the window arms a deferred flush for the rest of it, so
        # the tally reaches disk even if no further record() ever falls outside
        # the window (and at a hard-kill only hits of the last window are lost).
        monkeypatch.setattr(skill_usage, "_FLUSH_DEBOUNCE_SECS", 0.2)
        led = _quiet(tmp_path)
        led.record("x")
        timer = led._deferred_timer
        assert timer is not None, "a hit inside the window must arm the deferred flush"
        timer.join(5)
        assert not timer.is_alive()
        assert led._dirty is False, "the deferred write completed"
        assert _ledger(tmp_path).score("x")[0] == 1.0

    def test_records_inside_window_do_not_write_per_hit(self, tmp_path):
        # The debounce is a deliberate write-rate limit (at most one write per
        # window); arming the deferred flush must not turn record() into a
        # write-per-hit path.
        led = _ledger(tmp_path)
        led.record("a")
        _join_flush_threads()
        led._last_flush = time.time()
        for i in range(50):
            led.record(f"k{i}")
        saved = json.loads((tmp_path / "skill-usage.json").read_text())
        assert set(saved["keys"]) == {"a"}
        assert led._dirty is True, "the window's hits wait for the deferred flush"
        assert led._deferred_timer is not None

    def test_atexit_hook_flushes_dirty_hits(self, tmp_path):
        led = _quiet(tmp_path)
        led.record("x")
        led._flush_at_exit()  # the interpreter-exit hook, registered in __init__
        assert _ledger(tmp_path).score("x")[0] == 1.0

    def test_close_releases_the_atexit_hook(self, tmp_path):
        # __init__ registers one exit hook per ledger, and the registration pins
        # the ledger for the whole process. A host that closes one loader per
        # call — the unsigned MCP fallback (mcp_tools/skills.py) — would pin one
        # ledger per call on a long-running gateway, so close() must release its
        # hook. An unclosed ledger keeps its: that registration IS its exit
        # path, which is the design.
        unclosed = _ledger(tmp_path)
        unclosed_ref = weakref.ref(unclosed)
        del unclosed
        gc.collect()
        assert (
            unclosed_ref() is not None
        ), "precondition: an unclosed ledger stays pinned by its exit hook"

        closed = _ledger(tmp_path)
        closed_ref = weakref.ref(closed)
        closed.close()  # the shutdown flush; the hook is redundant from here on
        del closed
        gc.collect()
        assert (
            closed_ref() is None
        ), "a close()d ledger must be collectable, not pinned by its exit hook"
