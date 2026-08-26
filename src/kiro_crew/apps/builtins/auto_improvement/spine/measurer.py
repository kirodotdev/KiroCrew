"""Measurer — strictly-serial, pinned, interleaved-A/B harness invocation (spine).

Phase D of the per-cycle workflow (02_architecture.md §1.2 Phase D, §6.5; 10_roadmap
M0 "measurer — strictly-serial, pinned, interleaved-A/B harness invocation (no LLM)").

This is the spine EXECUTOR that enforces the measurement discipline; it INVOKES the
profile's ruler (the payload). They are split deliberately (02_arch §6.5):

  - SPINE (here): one resource at a time (no noisy neighbors); warmups discarded;
    N timed reps; **median + spread, never mean**; **interleaved A/B** so slow host
    drift cancels in the paired delta; **same-commit-sha assertion** before the
    arms are measured; instrument off STRUCTURED MARKERS, not file:line.
  - PROFILE (ruler): the thing being timed (the metric + harness adapter).

It is NOT an LLM step — it is deterministic code the workflow shells out to. The
measurer never proposes or decides; it only produces numbers the keeper judges.

No target token appears here; the ruler is reached only through the contract.
"""

from __future__ import annotations

import logging
import os
import statistics
from pathlib import Path

from .contracts import Measurement, Proposal, StageBreakdown, TargetProfile

# Module logger — Phase D produces the numbers the keeper judges. Logging the aggregated
# VERIFY/REPRODUCE result (median delta vs noise band, RH booleans) + the early-exit
# failures (same-sha, ruler error, no-delta) makes the measure→keep decision analyzable
# from logs alone, and pinpoints measure-error rejections. Greppable prefix: "measurer:".
_log = logging.getLogger("auto_improvement.spine.measurer")


def _env_reps(var: str, default: int) -> int:
    """Read a positive rep-count override from ``var``; fall back to ``default`` if unset
    or malformed. Floors at 2 (a 1-rep A/B has no spread and can't be trusted)."""
    raw = os.environ.get(var)
    if not raw:
        return default
    try:
        v = int(raw)
        return max(2, v)
    except (TypeError, ValueError):
        return default


class SameShaError(RuntimeError):
    """Raised if Phase D is asked to measure an artifact whose commit sha does not
    match the sha Phase C built and passed (02_arch §2.2 same-sha contract)."""


class Measurer:
    """Drives strictly-serial interleaved-A/B measurement of survivors.

    The interleave pattern is ``B C B C ...`` (base, candidate, ...); the paired
    delta is the keep number. Reps run one-at-a-time on a single pinned resource;
    warmups are discarded. The aggregate uses the MEDIAN of per-rep paired deltas.
    """

    def __init__(
        self,
        *,
        base_src: Path,
        reps: int | None = None,
        warmups: int = 2,
        reproduce_reps: int | None = None,
        reproduce_warmups: int = 3,
    ):
        self.base_src = Path(base_src)
        # Measurement thoroughness — the A/B measurement is the slowest part of a perf cycle
        # (VERIFY runs ``reps`` interleaved A/B reps and REPRODUCE runs ``reproduce_reps``
        # more, each a full project boot). On a host where a real win is LARGE relative to
        # the (capped) noise band, fewer reps still cleanly separate the win from the band,
        # so lowering these makes a run finish faster.
        # PRECEDENCE: explicit arg (from BudgetCaps → the UI "measureReps" knob) > env
        # override (AUTO_IMPROVEMENT_VERIFY_REPS / _REPRODUCE_REPS) > research-grade default
        # (6 / 8). So a UI value always wins; the env var is the operator fast-path when no
        # caps value is supplied. Warmups scale down with low rep counts so a 2-rep run
        # isn't dominated by discarded warmups.
        reps = int(reps) if reps is not None else _env_reps("AUTO_IMPROVEMENT_VERIFY_REPS", 6)
        reproduce_reps = (
            int(reproduce_reps)
            if reproduce_reps is not None
            else _env_reps("AUTO_IMPROVEMENT_REPRODUCE_REPS", 8)
        )
        reps = max(2, reps)
        reproduce_reps = max(2, reproduce_reps)
        if reps <= 2:
            warmups = min(warmups, 1)
        if reproduce_reps <= 2:
            reproduce_warmups = min(reproduce_warmups, 1)
        self.reps = reps
        self.warmups = warmups
        # REPRODUCE is a SECOND, INDEPENDENT A/B with MORE reps/warmups so a first-run
        # fluke that rode host drift cannot survive twice (06_cr_generation_and_dedup.md
        # §1.1 "kills first-run flukes"; ported from autoloop/verify.reproduce_perf which
        # bumps reps 6->8, warmups 2->3). It is the CR-stage analogue of the backend's
        # interleaved A/B + drift re-best (ARCHITECTURE.md §6.4/§6.5).
        self.reproduce_reps = reproduce_reps
        self.reproduce_warmups = reproduce_warmups

    def measure(
        self,
        *,
        profile: TargetProfile,
        proposal: Proposal,
        gated_commit_sha: str,
        scenario: str = "",
    ) -> Measurement:
        """Measure ONE survivor strictly serially with interleaved A/B reps (VERIFY).

        Asserts the same-commit-sha contract, runs warmups (discarded) + ``reps``
        interleaved timed reps via the profile ruler, and aggregates by median.
        """
        # Same fallback REPRODUCE uses (L143): the driver calls measure() with no
        # scenario, so default to the candidate's intended scenario. Otherwise VERIFY
        # would measure the EMPTY scenario while REPRODUCE measures the candidate's —
        # breaking the same-scenario anti-fluke comparison in pr_pipeline._reproduces.
        scenario = scenario or proposal.candidate.scenario
        return self._run_ab(
            profile=profile,
            proposal=proposal,
            gated_commit_sha=gated_commit_sha,
            scenario=scenario,
            reps=self.reps,
            warmups=self.warmups,
            phase="VERIFY",
        )

    def reproduce(
        self,
        *,
        profile: TargetProfile,
        proposal: Proposal,
        gated_commit_sha: str,
        scenario: str = "",
    ) -> Measurement:
        """REPRODUCE — a SECOND, INDEPENDENT interleaved A/B with MORE reps/warmups.

        The CR-stage anti-fluke check (06_cr_generation_and_dedup.md §1.1): a candidate
        that beat the band once may have ridden host drift; only a delta that survives a
        *second independent* A/B becomes a CR. This is a fresh measurement run (its own
        warmups, more reps) so it does not share state with VERIFY — ported from
        ``autoloop/verify.reproduce_perf`` (reps 6->8, warmups 2->3). The keeper / CR
        pipeline confirms the reproduce delta is the SAME DIRECTION and STILL beats the
        band before drafting (it never re-derives the keep on its own)."""
        scenario = scenario or proposal.candidate.scenario
        return self._run_ab(
            profile=profile,
            proposal=proposal,
            gated_commit_sha=gated_commit_sha,
            scenario=scenario,
            reps=self.reproduce_reps,
            warmups=self.reproduce_warmups,
            phase="REPRODUCE",
        )

    # ── the shared interleaved-A/B rep loop (VERIFY and REPRODUCE share it) ───

    def _run_ab(
        self,
        *,
        profile: TargetProfile,
        proposal: Proposal,
        gated_commit_sha: str,
        scenario: str,
        reps: int,
        warmups: int,
        phase: str = "AB",
    ) -> Measurement:
        """Run ``warmups`` discarded + ``reps`` timed interleaved-A/B reps via the
        profile ruler and aggregate by MEDIAN. The discipline (serial, pinned, warmups
        discarded, median-not-mean, same-sha) is identical for VERIFY and REPRODUCE —
        only the rep/warmup counts differ — so both call this one loop."""
        cand_src = proposal.worktree / "src"
        cid = getattr(proposal, "cand_id", None) or getattr(
            getattr(proposal, "candidate", None), "target", "?"
        )

        # SAME-SHA assertion UP FRONT: an empty gated sha means Phase C produced no
        # artifact — fail before paying for a single (expensive) boot, not after the
        # first rep already ran.
        if not gated_commit_sha:
            _log.info("measurer: %s same-sha FAIL | cand=%s — no gated artifact", phase, cid)
            raise SameShaError("no gated commit sha — Phase C produced no artifact")

        deltas: list[float] = []
        bases: list[float] = []
        cands: list[float] = []
        stage_acc: dict[str, list[float]] = {}
        guard_acc: dict[str, list[float]] = {}
        secondary_acc: dict[str, list[float]] = {}
        rh_cap_ok = True
        rh_func_ok = True
        note = ""

        # Warmups first (discarded), then timed reps — all interleaved B/C and
        # serial (one resource at a time; no noisy neighbors, 02_arch §2.1 D).
        total = warmups + reps
        for i in range(total):
            m = profile.ruler.measure(
                base_src=self.base_src,
                cand_src=cand_src,
                commit_sha=gated_commit_sha,
                scenario=scenario,
            )
            if not m.ok:
                _log.info(
                    "measurer: %s ruler ERROR | cand=%s rep=%d/%d note=%s",
                    phase,
                    cid,
                    i,
                    total,
                    (m.note or "")[:120],
                )
                return Measurement(ok=False, note=f"ruler error on rep {i}: {m.note}")
            if i < warmups:
                continue  # discard warmups
            if m.primary_delta is not None:
                deltas.append(m.primary_delta)
            if m.primary_base is not None:
                bases.append(m.primary_base)
            if m.primary_cand is not None:
                cands.append(m.primary_cand)
            for k, v in m.stages.stages.items():
                stage_acc.setdefault(k, []).append(v)
            for k, v in m.guardrails.items():
                guard_acc.setdefault(k, []).append(v)
            # Aggregate the NON-BLOCKING secondary metrics (sampled RSS/CPU/throughput)
            # by median too, so the kept Measurement carries the per-candidate resource
            # cost the archive surfaces in results.tsv (the keeper never reads them).
            for k, v in m.secondary.items():
                secondary_acc.setdefault(k, []).append(v)
            rh_cap_ok = rh_cap_ok and m.rh_capability_ok
            rh_func_ok = rh_func_ok and m.rh_functional_ok
            note = m.note or note

        if not deltas:
            _log.info("measurer: %s no-delta | cand=%s — no timed rep produced a delta", phase, cid)
            return Measurement(ok=False, note="no timed reps produced a delta")

        # Median, never mean (02_arch §6.5) — robust to a single host-drift outlier.
        primary_delta = statistics.median(deltas)
        band = profile.calibration.noise_band
        # Log the aggregated result: median delta vs the noise band tells the keeper's
        # primary decision (delta < -band == a real win), plus the RH booleans (a False
        # here is a silent-capability-shrink discard the keeper will make next).
        _log.info(
            "measurer: %s result | cand=%s reps=%d median_delta=%.3f band=%s beats_band=%s "
            "rh_cap=%s rh_func=%s",
            phase,
            cid,
            len(deltas),
            primary_delta,
            band,
            (primary_delta < -(band or 0.0)),
            rh_cap_ok,
            rh_func_ok,
        )
        return Measurement(
            ok=True,
            primary_delta=primary_delta,
            primary_base=statistics.median(bases) if bases else None,
            primary_cand=statistics.median(cands) if cands else None,
            noise_band=band,
            stages=StageBreakdown(stages={k: statistics.median(v) for k, v in stage_acc.items()}),
            guardrails={k: statistics.median(v) for k, v in guard_acc.items()},
            secondary={k: statistics.median(v) for k, v in secondary_acc.items()},
            rh_capability_ok=rh_cap_ok,
            rh_functional_ok=rh_func_ok,
            note=note or f"{reps} interleaved reps; median Δ={primary_delta:.3f}",
        )
