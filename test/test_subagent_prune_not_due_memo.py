"""A steady-state tombstone sweep costs what changed, not how many runs it keeps.

A run inside its retention window ends its full evaluation in ``continue`` with no
side effect, so a sweep that sees its tombstone unchanged skips it without reading
it. These tests pin the cost (call counts on the product's own seams, never wall
time), that on a forward clock skipping changes no outcome, and that a run that
became due is pruned on the sweep where it became due.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import kiro_crew.subagent_persistence as sp
from kiro_crew.subagent_persistence_layout import _atomic_write
from kiro_crew.testing.clock import ManualClock

DAY = 86400
WEEK_DAYS = 7
HOUR = 3600


def _seed_retained(count: int, prefix: str = "kept") -> list[str]:
    """*count* abnormal-exit runs that died just now, through the product writer."""
    agent_ids = [f"{prefix}-{n:05d}" for n in range(count)]
    for agent_id in agent_ids:
        sp._agent_dir(agent_id).mkdir(parents=True)
        sp.write_tombstone(agent_id, cause="timeout", recovery_action="none")
    return agent_ids


def _seed_delivered(agent_id: str) -> None:
    """A delivered run with one protected cleanup generation, dead just now."""
    sp.create_agent_folder(agent_id, task=agent_id)
    sp.update_state(agent_id, session_id=f"sid-{agent_id}", provider="acp", keep=False)
    sp.remember_live_cleanup_identity(
        agent_id, session_id=f"sid-{agent_id}", provider="acp", keep=False
    )
    sp.write_tombstone(agent_id, cause="delivered", recovery_action="none")


def _sweep(delivered_window: int = HOUR) -> int:
    return sp.prune_stale_tombstones(WEEK_DAYS, delivered_window)


class _Reads:
    """Tombstone reads and ``Path.resolve`` calls made inside the ``with`` block."""

    def __init__(self) -> None:
        self.tombstones: Counter[str] = Counter()
        self.resolves = 0

    def __enter__(self) -> _Reads:
        real_read = sp.read_tombstone
        real_resolve = Path.resolve

        def counting_read(agent_id: str) -> dict | None:
            self.tombstones[agent_id] += 1
            return real_read(agent_id)

        def counting_resolve(path: Path, *args: object, **kwargs: object) -> Path:
            self.resolves += 1
            return real_resolve(path, *args, **kwargs)

        self._patches = [
            patch.object(sp, "read_tombstone", counting_read),
            patch.object(Path, "resolve", counting_resolve),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


def _steady_and_one_changed(count: int, prefix: str) -> tuple[_Reads, _Reads]:
    agent_ids = _seed_retained(count, prefix)
    assert _sweep() == 0  # the warm pass evaluates every run in full
    with _Reads() as steady:
        assert _sweep() == 0
    sp.write_tombstone(agent_ids[count // 2], cause="timeout", recovery_action="none")
    with _Reads() as changed:
        assert _sweep() == 0
    return steady, changed


def test_a_steady_sweep_costs_the_same_at_any_number_of_retained_runs() -> None:
    # 25 and 400 rather than the issue's 250 and 4,000: each fsynced write costs
    # milliseconds, and a 16-fold spread already separates flat from linear.
    small_steady, small_changed = _steady_and_one_changed(25, "small")
    for agent_id in sp._subagents_dir().iterdir():
        sp.delete_agent_folder(agent_id.name)
    large_steady, large_changed = _steady_and_one_changed(400, "large")

    assert small_steady.tombstones == large_steady.tombstones == Counter()
    assert small_steady.resolves == large_steady.resolves
    # One rewritten tombstone is one more read, at either size.
    assert small_changed.tombstones == Counter({"small-00012": 1})
    assert large_changed.tombstones == Counter({"large-00200": 1})
    assert large_changed.resolves - large_steady.resolves == (
        small_changed.resolves - small_steady.resolves
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        os.fspath(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_skipping_a_run_inside_its_window_changes_no_outcome() -> None:
    """On a forward clock a sweep that remembers gives what a full evaluation gives.

    Every kind of not-yet-due run is seeded, including those the full pass leaves
    at an earlier ``continue`` (a corrupt tombstone, a leaked process, a run the
    caller's *keep* holds). The same sweep then runs with the memo and without it.
    """
    _seed_retained(3, "timeout")
    _seed_delivered("delivered-fresh")
    sp.create_agent_folder("leaked", task="leaked")
    sp.write_tombstone("leaked", cause="timeout", recovery_action="none", leaked_process=True)
    sp.create_agent_folder("corrupt", task="corrupt")
    (sp._agent_dir("corrupt") / "tombstone.json").write_text("{corrupt")
    # Dated by the fallback (the file's own modification time), still inside its window.
    sp.create_agent_folder("bad-died", task="bad-died")
    _atomic_write(sp._agent_dir("bad-died") / "tombstone.json", {"died": "not-a-time"})
    sp.create_agent_folder("held", task="held")
    sp.write_tombstone("held", cause="refused", recovery_action="none")
    kept: list[str] = []

    def keep(agent_id: str, _ts: dict) -> bool:
        kept.append(agent_id)
        return agent_id == "held"

    root = sp._subagents_dir().parent
    with patch.object(sp, "_cleanup_session_files_sync", return_value=True) as cleanup:
        assert sp.prune_stale_tombstones(WEEK_DAYS, HOUR, keep) == 0
        before = _tree(root)

        with _Reads() as remembered:
            assert sp.prune_stale_tombstones(WEEK_DAYS, HOUR, keep) == 0
        after_remembered = _tree(root)

        sp._NOT_DUE_MEMO.clear()  # what a restart leaves: every run in full
        with _Reads() as full:
            assert sp.prune_stale_tombstones(WEEK_DAYS, HOUR, keep) == 0
        after_full = _tree(root)

    cleanup.assert_not_called()
    assert before == after_remembered == after_full
    # The remembered pass skipped exactly the runs that reached the window check.
    reached_the_window = {
        "timeout-00000",
        "timeout-00001",
        "timeout-00002",
        "delivered-fresh",
        "bad-died",
    }
    assert set(full.tombstones) - set(remembered.tombstones) == reached_the_window
    assert set(remembered.tombstones) == {"leaked", "corrupt", "held"}


def test_a_run_that_becomes_due_later_is_pruned_on_that_sweep(monkeypatch) -> None:
    (agent_id,) = _seed_retained(1, "ages")
    assert _sweep() == 0
    assert agent_id in sp._NOT_DUE_MEMO

    ManualClock(time.time() + (WEEK_DAYS + 1) * DAY).install(monkeypatch, sp)
    with patch.object(sp, "_cleanup_session_files_sync", return_value=True):
        assert _sweep() == 1

    assert not sp._agent_dir(agent_id).exists()
    assert agent_id not in sp._NOT_DUE_MEMO


def test_a_lowered_delivered_ttl_prunes_on_the_next_sweep() -> None:
    _seed_delivered("ttl-cut")
    assert _sweep(HOUR) == 0
    assert "ttl-cut" in sp._NOT_DUE_MEMO

    with patch.object(sp, "_cleanup_session_files_sync", return_value=True) as cleanup:
        assert _sweep(0) == 1

    cleanup.assert_called_once_with("sid-ttl-cut", "acp", cwd="")
    assert not sp._agent_dir("ttl-cut").exists()


def test_a_tombstone_rewritten_past_its_window_is_pruned_on_the_next_sweep() -> None:
    _seed_delivered("rewritten")
    assert _sweep() == 0
    ts_path = sp._agent_dir("rewritten") / "tombstone.json"
    tombstone = json.loads(ts_path.read_text())
    # flake-ok: back-dated on the live clock, far past the one-hour window
    tombstone["died"] = time.time() - 2 * DAY
    _atomic_write(ts_path, tombstone)  # how every product writer replaces it

    with patch.object(sp, "_cleanup_session_files_sync", return_value=True):
        assert _sweep() == 1

    assert not sp._agent_dir("rewritten").exists()


def test_a_run_that_left_the_directory_is_forgotten() -> None:
    (agent_id,) = _seed_retained(1, "leaves")
    assert _sweep() == 0
    assert agent_id in sp._NOT_DUE_MEMO

    sp.delete_agent_folder(agent_id)
    assert _sweep() == 0

    assert agent_id not in sp._NOT_DUE_MEMO
