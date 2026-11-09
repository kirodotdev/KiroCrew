"""The keyed mount-source pass is bounded in work per pass and in passes per interval.

Split from ``test_sandbox_mount_source_sweep.py`` (which keeps the shared helpers)
to keep each file under the line cap.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from test_sandbox_mount_source_sweep import _DEAD_PID, _LIVE_PID, _make_dir, _make_file, _pin

from kiro_crew import sandbox_mount_sweep
from kiro_crew.sandbox import _cleanup_stale_sandbox_mount_sources
from kiro_crew.testing.clock import ManualClock


class TestSweepCostIsBounded:
    """One pass costs a bounded amount of work, and passes come at a bounded rate.

    The pass runs on a maintenance thread inside the gateway process, so its pure
    Python work competes for the interpreter with message delivery. On a host where
    the pile reached ~100k entries a pass spent ~4 s in the pin scan and then a
    ``lstat`` plus two more stats and a liveness probe on EVERY entry; that ran on
    every cleanup tick.
    """

    def test_liveness_is_probed_once_per_pid_not_once_per_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _pin(monkeypatch, set())
        probed: list[int] = []
        monkeypatch.setattr(
            sandbox_mount_sweep.platform_compat,
            "pid_exists",
            lambda pid: probed.append(pid) or False,
        )
        for index in range(6):
            _make_file(tmp_path, f"kirocrew_sb_{_DEAD_PID}_file{index:02d}")

        assert _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)]) == 6
        assert probed == [_DEAD_PID]

    def test_a_pass_examines_at_most_the_entry_cap_and_the_next_one_moves_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Retained entries listed first must not starve the ones behind them."""
        _pin(monkeypatch, set())
        monkeypatch.setattr(sandbox_mount_sweep, "_SWEEP_MAX_ENTRIES_PER_PASS", 2)
        retained = [f"kirocrew_sb_{_LIVE_PID}_live{index}" for index in range(2)]
        for name in retained:
            _make_dir(tmp_path, name)
        stale = f"kirocrew_sb_{_DEAD_PID}_dead0"
        _make_file(tmp_path, stale)
        order = [*retained, stale]
        real_listdir = os.listdir
        monkeypatch.setattr(
            sandbox_mount_sweep.os,
            "listdir",
            lambda path: order if str(path) == str(tmp_path) else real_listdir(path),
        )

        assert _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)]) == 0
        assert (tmp_path / stale).exists()

        assert _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)]) == 1
        assert not (tmp_path / stale).exists()

    def test_the_periodic_pass_is_rate_limited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The default-roots call (the cleanup tick's) runs at most once an interval."""
        _pin(monkeypatch, set())
        clock = ManualClock(start=time.time())
        clock.install(monkeypatch, sandbox_mount_sweep)
        monkeypatch.setattr(
            "kiro_crew.sandbox._mount_source_candidate_roots", lambda: [str(tmp_path)]
        )
        _make_file(tmp_path, f"kirocrew_sb_{_DEAD_PID}_first")
        assert _cleanup_stale_sandbox_mount_sources() == 1

        _make_file(tmp_path, f"kirocrew_sb_{_DEAD_PID}_second")
        clock.advance(sandbox_mount_sweep._SWEEP_MIN_INTERVAL_SECONDS / 2)
        assert _cleanup_stale_sandbox_mount_sources() == 0
        assert (tmp_path / f"kirocrew_sb_{_DEAD_PID}_second").exists()

        clock.advance(sandbox_mount_sweep._SWEEP_MIN_INTERVAL_SECONDS)
        assert _cleanup_stale_sandbox_mount_sources() == 1

    def test_a_liveness_answer_is_not_carried_into_the_next_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A pid probed dead in one root can be reused by a new launcher that stages in
        a later root; that root's listing is newer than the answer, so it re-probes."""
        _pin(monkeypatch, set(), complete=True)
        first, second = tmp_path / "first", tmp_path / "second"
        first.mkdir()
        second.mkdir()
        _make_file(first, f"kirocrew_sb_{_DEAD_PID}_orphan")
        fresh = _make_dir(second, f"kirocrew_sb_{_DEAD_PID}_fresh")
        answers = iter([False, True])  # dead for the first root, reused by the second
        monkeypatch.setattr(
            sandbox_mount_sweep.platform_compat, "pid_exists", lambda pid: next(answers)
        )

        assert _cleanup_stale_sandbox_mount_sources(roots=[str(first), str(second)]) == 1
        assert fresh.exists()

    def test_a_budget_spent_on_the_last_root_still_rotates_the_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Roots A (exactly the cap of retained entries), B (an orphan), C (empty):
        once a pass starts at C, the budget runs out at B's first entry, B is the last
        root visited, and the start must still move on or B is never examined."""
        _pin(monkeypatch, set())
        monkeypatch.setattr(sandbox_mount_sweep, "_SWEEP_MAX_ENTRIES_PER_PASS", 2)
        a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
        for root in (a, b, c):
            root.mkdir()
        for index in range(2):
            _make_dir(a, f"kirocrew_sb_{_LIVE_PID}_live{index}")
        stale = b / f"kirocrew_sb_{_DEAD_PID}_dead0"
        _make_file(b, stale.name)
        roots = [str(a), str(b), str(c)]

        for _ in range(len(roots) + 1):
            _cleanup_stale_sandbox_mount_sources(roots=roots)

        assert not stale.exists()

    def test_a_root_that_spends_the_budget_does_not_starve_the_roots_behind_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The first root alone holds more retained entries than one pass examines;
        the next pass must still reach the second root's reclaimable file."""
        _pin(monkeypatch, set())
        monkeypatch.setattr(sandbox_mount_sweep, "_SWEEP_MAX_ENTRIES_PER_PASS", 2)
        busy, fallback = tmp_path / "busy", tmp_path / "fallback"
        busy.mkdir()
        fallback.mkdir()
        for index in range(6):
            _make_dir(busy, f"kirocrew_sb_{_LIVE_PID}_live{index}")
        stale = fallback / f"kirocrew_sb_{_DEAD_PID}_dead0"
        _make_file(fallback, stale.name)
        roots = [str(busy), str(fallback)]

        assert _cleanup_stale_sandbox_mount_sources(roots=roots) == 0
        assert _cleanup_stale_sandbox_mount_sources(roots=roots) == 1
        assert not stale.exists()
