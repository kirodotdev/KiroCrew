"""Reproducing test: fan_out overlaps wide and deep slices.

Given candidates=[A,B,C,D,E,F,G], wide=6, deep=1, the top-ranked candidate A
appears in BOTH wide_cands (index 0) and deep_cands (index 0), so fan_out
produces 7 proposals where 2 share the same candidate target — a redundant
worktree and wasted gate/measurement cycle.

Expected: all proposals target DISTINCT candidates (no duplicates).
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

from kiro_crew.apps.builtins.auto_improvement.spine.proposer import Proposer


@dataclass
class FakeCandidate:
    target: str
    signature: str = ""
    hypothesis: str = ""
    kind: str = "perf"


@dataclass
class FakeProfile:
    """Minimal profile stub — propose always returns True (diff produced)."""

    def propose(self, *, candidate, base_sha, worktree, tier):
        # Write a trivial file so _capture_diff produces a non-empty diff
        (worktree / "change.txt").write_text(f"edit for {candidate.target}\n")
        return True


def _fake_new_worktree(self, cand_id, base_sha):
    """Skip real git — just create the directory and return it."""
    wt = self.worktree_root / cand_id
    wt.mkdir(parents=True, exist_ok=True)
    return wt, f"branch/{cand_id}"


def _fake_capture_diff(self, worktree, base_sha):
    """Return a synthetic non-empty diff."""
    return "diff --git a/change.txt b/change.txt\n+edit\n"


def test_fan_out_no_duplicate_targets(tmp_path):
    """fan_out must not propose the same candidate target twice."""
    candidates = [FakeCandidate(target=f"src/mod{i}.py::func{i}") for i in range(7)]

    proposer = Proposer(
        clone=tmp_path / "clone",
        worktree_root=tmp_path / "wt",
        wide=6,
        deep=1,
    )
    (tmp_path / "clone").mkdir()
    (tmp_path / "wt").mkdir()

    with (
        patch.object(Proposer, "_new_worktree", _fake_new_worktree),
        patch.object(Proposer, "_capture_diff", _fake_capture_diff),
    ):
        proposals = proposer.fan_out(
            profile=FakeProfile(),
            candidates=candidates,
            base_sha="abc123",
            cycle=1,
        )

    # With wide=6 + deep=1 and 7 candidates available, we expect 7 DISTINCT proposals
    targets = [p.candidate.target for p in proposals]
    assert len(targets) == len(
        set(targets)
    ), f"Duplicate targets found: {[t for t in targets if targets.count(t) > 1]}"
