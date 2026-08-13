"""Reproducing test: Keeper.decide produces '±None' in Verdict.reason when
Measurement.noise_band is None.
"""

from pathlib import Path

from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
    TRACK_PERF,
    Candidate,
    GateResult,
    Measurement,
    Proposal,
)
from kiro_crew.apps.builtins.auto_improvement.spine.keeper import Keeper


def test_verdict_reason_no_none_string():
    """When noise_band is None the Verdict.reason must show a numeric value, not 'None'."""
    keeper = Keeper()

    proposal = Proposal(
        cand_id="test-1",
        candidate=Candidate(kind=TRACK_PERF, target="mod::func"),
        worktree=Path("/tmp/fake"),
        branch="b",
        description="test proposal",
        diff="--- a\n+++ b\n",
    )
    gate = GateResult(passed=True, commit_sha="abc123")
    measurement = Measurement(
        ok=True,
        primary_delta=-0.5,
        noise_band=None,  # <-- the bug trigger
    )

    verdict, _archived = keeper.decide(
        survivors=[(proposal, gate, measurement)],
    )

    assert verdict.keep is True
    # The reason string must NOT contain the literal 'None'
    assert "None" not in verdict.reason, f"Verdict.reason contains 'None': {verdict.reason!r}"
