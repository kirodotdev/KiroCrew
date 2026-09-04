"""Reproducing test: Keeper.evaluate_one rejects valid maximize-direction wins.

Given a Measurement with primary_delta=+5.0 and noise_band=2.0 (all RH/guardrails
OK, gate passed), evaluate_one should return (True, 'kept') for a maximize-direction
metric. Instead it returns (False, 'discard_noise') because the noise-band check
hardcodes minimize semantics (delta >= -band rejects).
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
    Candidate,
    GateResult,
    Measurement,
    Proposal,
)
from kiro_crew.apps.builtins.auto_improvement.spine.keeper import KEPT, Keeper


def _make_proposal() -> Proposal:
    return Proposal(
        cand_id="test-maximize",
        candidate=Candidate(kind="perf", target="test::target"),
        worktree=Path("/tmp/fake"),
        branch="test-branch",
        description="test proposal",
        diff="--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
    )


def test_maximize_positive_delta_clears_band() -> None:
    """A +5.0 delta with band=2.0 in maximize direction SHOULD be kept."""
    keeper = Keeper()
    gate = GateResult(passed=True, commit_sha="abc123")
    measurement = Measurement(
        ok=True,
        primary_delta=5.0,
        noise_band=2.0,
        rh_capability_ok=True,
        rh_functional_ok=True,
    )
    keep, status = keeper.evaluate_one(
        proposal=_make_proposal(),
        gate=gate,
        measurement=measurement,
        direction="maximize",
    )
    assert keep is True, f"Expected kept, got ({keep}, {status})"
    assert status == KEPT


def test_minimize_negative_delta_clears_band() -> None:
    """A -5.0 delta with band=2.0 in minimize direction SHOULD be kept (existing behavior)."""
    keeper = Keeper()
    gate = GateResult(passed=True, commit_sha="abc123")
    measurement = Measurement(
        ok=True,
        primary_delta=-5.0,
        noise_band=2.0,
        rh_capability_ok=True,
        rh_functional_ok=True,
    )
    keep, status = keeper.evaluate_one(
        proposal=_make_proposal(),
        gate=gate,
        measurement=measurement,
        direction="minimize",
    )
    assert keep is True, f"Expected kept, got ({keep}, {status})"
    assert status == KEPT


def test_maximize_delta_within_band_rejected() -> None:
    """A +1.0 delta with band=2.0 in maximize direction should NOT be kept."""
    keeper = Keeper()
    gate = GateResult(passed=True, commit_sha="abc123")
    measurement = Measurement(
        ok=True,
        primary_delta=1.0,
        noise_band=2.0,
        rh_capability_ok=True,
        rh_functional_ok=True,
    )
    keep, status = keeper.evaluate_one(
        proposal=_make_proposal(),
        gate=gate,
        measurement=measurement,
        direction="maximize",
    )
    assert keep is False
    assert status == "discard_noise"
