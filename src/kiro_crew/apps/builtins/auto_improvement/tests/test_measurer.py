"""The interleaved-A/B Measurer — the VERIFY/REPRODUCE measurement discipline.

`spine/measurer.py` is pure measurement logic driven by a profile ruler: it runs
``warmups`` discarded reps then ``reps`` timed ones, aggregates by MEDIAN (never mean, so a
single host-drift outlier cannot swing the keep number), enforces the same-commit-sha
contract up front, and fails cleanly on a ruler error or an all-empty rep set. None of that
needs a real backend — a scripted fake ruler exercises every branch — yet it was uncovered.

The properties worth pinning are the ones a subtle refactor could silently break: warmups
are DISCARDED (not averaged in), the aggregate is the MEDIAN, an error on any rep aborts the
whole measurement, and REPRODUCE really is an independent second run with more reps.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
    Candidate,
    Measurement,
    Proposal,
    StageBreakdown,
    TargetProfile,
)
from kiro_crew.apps.builtins.auto_improvement.spine.measurer import Measurer, SameShaError


class _Ruler:
    """A ruler whose per-rep results are scripted. Records how many times it ran so a
    test can assert warmups + reps were actually requested."""

    direction = "minimize"

    def __init__(self, results: list[Measurement]) -> None:
        self._results = list(results)
        self.calls = 0

    def measure(self, *, base_src, cand_src, commit_sha, scenario) -> Measurement:
        self.calls += 1
        # Repeat the last scripted result once the script runs out, so a test only has to
        # script the reps whose values it cares about.
        idx = min(self.calls - 1, len(self._results) - 1)
        return self._results[idx]


class _Cal:
    noise_band = 5.0


class _Profile:
    def __init__(self, ruler: _Ruler) -> None:
        self.ruler = ruler
        self.calibration = _Cal()


def _prof(ruler: _Ruler) -> TargetProfile:
    """The Measurer touches only profile.ruler + profile.calibration; cast the stub once."""
    return cast(TargetProfile, _Profile(ruler))


def _rep(delta: float, **kw) -> Measurement:
    return Measurement(
        ok=True,
        primary_delta=delta,
        primary_base=kw.pop("base", 100.0),
        primary_cand=kw.pop("cand", 100.0 + delta),
        **kw,
    )


def _proposal(tmp_path: Path) -> Proposal:
    (tmp_path / "cand" / "src").mkdir(parents=True, exist_ok=True)
    return Proposal(
        cand_id="c1",
        candidate=Candidate(kind="perf", target="m.py::f", scenario="bench"),
        worktree=tmp_path / "cand",
        branch="main",
        description="",
        diff="",
    )


def _measurer(tmp_path: Path, **kw) -> Measurer:
    (tmp_path / "base").mkdir(parents=True, exist_ok=True)
    return Measurer(base_src=tmp_path / "base", **kw)


class TestSameShaContract:
    def test_an_empty_gated_sha_fails_before_any_rep(self, tmp_path: Path) -> None:
        """Fail before paying for a boot: an empty sha means Phase C produced no artifact."""
        ruler = _Ruler([_rep(-10.0)])
        m = _measurer(tmp_path, reps=2, warmups=1)
        with pytest.raises(SameShaError):
            m.measure(profile=_prof(ruler), proposal=_proposal(tmp_path), gated_commit_sha="")
        assert ruler.calls == 0, "the ruler ran despite there being no artifact to measure"


class TestWarmupsAndAggregation:
    def test_warmups_are_discarded_and_the_aggregate_is_the_median(self, tmp_path: Path) -> None:
        """Warmups must not enter the aggregate, and the aggregate is the MEDIAN — so a
        single outlier rep cannot swing the keep number."""
        # 1 warmup (value should be ignored) then 3 timed reps -10, -12, -100 (outlier).
        ruler = _Ruler([_rep(-999.0), _rep(-10.0), _rep(-12.0), _rep(-100.0)])
        m = _measurer(tmp_path, reps=3, warmups=1)
        out = m.measure(profile=_prof(ruler), proposal=_proposal(tmp_path), gated_commit_sha="abc")
        assert ruler.calls == 4, "should run warmups + reps"
        assert out.ok
        # median(-10, -12, -100) == -12 — the outlier does NOT dominate, and the discarded
        # warmup (-999) is nowhere near the result.
        assert out.primary_delta == -12.0

    def test_reps_and_warmups_floor_at_two_and_one(self, tmp_path: Path) -> None:
        """A 1-rep A/B has no spread; the constructor floors reps at 2 and trims warmups."""
        m = _measurer(tmp_path, reps=1, warmups=5)
        assert m.reps == 2
        assert m.warmups == 1, "warmups must shrink when reps is tiny, not dominate the run"


class TestFailureModes:
    def test_a_ruler_error_on_any_rep_aborts_the_whole_measurement(self, tmp_path: Path) -> None:
        """One bad rep is a harness failure, not a data point to average around."""
        ruler = _Ruler([_rep(-10.0), Measurement(ok=False, note="boom"), _rep(-10.0)])
        m = _measurer(tmp_path, reps=3, warmups=0)
        out = m.measure(profile=_prof(ruler), proposal=_proposal(tmp_path), gated_commit_sha="abc")
        assert out.ok is False
        assert "boom" in out.note

    def test_reps_that_all_lack_a_delta_are_a_clean_failure(self, tmp_path: Path) -> None:
        """A ruler that returns ``ok`` but never a delta yields no keep number — reported
        as a clean failure, not a crash or a fabricated zero."""
        ruler = _Ruler([Measurement(ok=True, primary_delta=None)])
        m = _measurer(tmp_path, reps=2, warmups=0)
        out = m.measure(profile=_prof(ruler), proposal=_proposal(tmp_path), gated_commit_sha="abc")
        assert out.ok is False
        assert "no timed rep" in out.note.lower()


class TestRewardHackAccumulation:
    def test_one_false_rh_probe_taints_the_whole_measurement(self, tmp_path: Path) -> None:
        """RH-A/RH-B are ANDed across reps: a single rep that shows a silent capability
        shrink makes the aggregate report it, so the keeper discards. A max() or last-wins
        fold would let a one-good-rep candidate through."""
        ruler = _Ruler(
            [
                _rep(-10.0, rh_capability_ok=True, rh_functional_ok=True),
                _rep(-10.0, rh_capability_ok=False, rh_functional_ok=True),
                _rep(-10.0, rh_capability_ok=True, rh_functional_ok=True),
            ]
        )
        m = _measurer(tmp_path, reps=3, warmups=0)
        out = m.measure(profile=_prof(ruler), proposal=_proposal(tmp_path), gated_commit_sha="abc")
        assert out.rh_capability_ok is False, "a shrink in one rep did not taint the result"
        assert out.rh_functional_ok is True


class TestStagesAndGuardrailsAreMedianed:
    def test_stage_and_guardrail_channels_aggregate_by_median(self, tmp_path: Path) -> None:
        ruler = _Ruler(
            [
                _rep(-10.0, stages=StageBreakdown(stages={"io": 2.0}), guardrails={"rss": 1.0}),
                _rep(-10.0, stages=StageBreakdown(stages={"io": 4.0}), guardrails={"rss": 3.0}),
                _rep(-10.0, stages=StageBreakdown(stages={"io": 6.0}), guardrails={"rss": 5.0}),
            ]
        )
        m = _measurer(tmp_path, reps=3, warmups=0)
        out = m.measure(profile=_prof(ruler), proposal=_proposal(tmp_path), gated_commit_sha="abc")
        assert out.stages.stages["io"] == 4.0
        assert out.guardrails["rss"] == 3.0
        assert out.noise_band == 5.0, "the calibrated band must ride along on the result"


class TestReproduceIsAnIndependentSecondRun:
    def test_reproduce_uses_more_reps_than_verify(self, tmp_path: Path) -> None:
        """REPRODUCE is the anti-fluke second A/B: it must run MORE reps than VERIFY, not
        reuse VERIFY's data."""
        ruler = _Ruler([_rep(-10.0)])
        m = _measurer(tmp_path, reps=2, warmups=0, reproduce_reps=4, reproduce_warmups=0)
        m.reproduce(profile=_prof(ruler), proposal=_proposal(tmp_path), gated_commit_sha="abc")
        assert ruler.calls == 4, "reproduce did not run its own (larger) rep set"

    def test_an_env_override_sets_the_rep_counts(self, tmp_path: Path, monkeypatch) -> None:
        """The operator fast-path: env vars lower rep counts when no explicit arg is given,
        and a malformed value falls back to the default rather than crashing."""
        monkeypatch.setenv("AUTO_IMPROVEMENT_VERIFY_REPS", "3")
        monkeypatch.setenv("AUTO_IMPROVEMENT_REPRODUCE_REPS", "not-a-number")
        m = _measurer(tmp_path)
        assert m.reps == 3
        assert m.reproduce_reps == 8, "a garbled env value must fall back to the default"

    def test_an_explicit_arg_outranks_the_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("AUTO_IMPROVEMENT_VERIFY_REPS", "99")
        m = _measurer(tmp_path, reps=4)
        assert m.reps == 4
