"""Reproducer: Proposal.skip_status defaults to '' but docstring says 'no_defect'."""

from pathlib import Path

from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
    TRACK_BUG,
    Candidate,
    Proposal,
)


def test_skipped_proposal_skip_status_defaults_to_no_defect():
    """A skipped Proposal with no explicit skip_status should default to 'no_defect'."""
    c = Candidate(kind=TRACK_BUG, target="mod::func")
    p = Proposal(
        cand_id="x",
        candidate=c,
        worktree=Path("."),
        branch="b",
        description="",
        skipped=True,
    )
    assert p.skip_status == "no_defect", (
        f"Expected skip_status to default to 'no_defect', got {p.skip_status!r}"
    )
