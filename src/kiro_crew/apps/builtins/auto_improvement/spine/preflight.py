"""Phase-1 pre-flight — the trust gate the driver runs BEFORE the Phase-2 loop (spine).

03_metric_design_and_calibration.md is the whole point of Phase 1: "the metric is designed
after code analysis and the ruler is proven before any optimization loop is permitted to
score a candidate" (§0). This module is the spine orchestration of that proof; the driver
calls it at the top of a real run and HALTS Phase 2 if the ruler is not proven.

The three gates, in order (03_metric §0 / §11 / §11.1; 08_safety §2.2):

  1. CALIBRATE the noise band — collect ≈``baseline_reps`` samples of the UNTOUCHED
     baseline via the ruler and set ``noise_band = max(2σ, floor)`` (§5.1/§5.2). Too few
     samples (< 2) is a calibration error (a single sample has no spread — the harness is
     broken; §5).
  2. CANARY — force the KNOWN/forced win and measure it through the FULL ruler; it MUST
     clear the calibrated band, else HALT: "ruler not trusted — refusing Phase 2"
     ("the evaluator is the project", §7.1, §11.1).
  3. DO-NOT-POLLUTE — snapshot host paths -> boot the runtime once -> diff; a non-zero
     diff BLOCKS the run (§7.3; 08_safety §2.2). The path set is the profile's; the
     snapshot/boot/diff machinery is :mod:`.pollute` (spine).

Only if ALL THREE pass does the driver enter the Phase-2 loop (§0 ordering: canary clears
band -> perf/bug tracks; canary fails -> HALT, fix the ruler).

SPINE vs PROFILE: the spine RUNS the procedure (collect, compute the band, check the
canary clears it, run the pollute test); the PROFILE supplies the deterministic payloads
(``ruler.baseline_samples`` / ``ruler.measure_canary``) and the host path set
(``isolation.do_not_pollute_paths``) — the same split as Phase-D measurement (§0.2). This
module names no target token; the band math ``max(2σ, floor)`` is the general §5.2 rule.

Docs: 03_metric_design_and_calibration.md §5 (calibration), §7 (canary), §7.3 + §11.1
(do-not-pollute), §0/§11 (ordering); 08_safety_isolation_and_guardrails.md §2.2, §9 T1.2.
"""

from __future__ import annotations

import logging
import os
import statistics
from dataclasses import dataclass
from pathlib import Path

from . import pollute
from .contracts import Measurement, TargetProfile
from .pollute import BootCallable, PolluteResult


class RulerNotTrustedError(RuntimeError):
    """Raised when the canary CANNOT clear the calibrated band: "ruler not trusted —
    refusing Phase 2" (03_metric §7.1, §11.1). The driver does NOT enter the Phase-2 loop.
    The ruler is too noisy/insensitive (or measuring the wrong path; §5.4) — surface for
    human inspection before any optimization."""


class HostPollutionError(RuntimeError):
    """Raised when the do-not-pollute acceptance test finds a non-zero host-state diff:
    the run is BLOCKED until the leak is fixed (03_metric §11.1; 08_safety §2.2 / §9
    T1.2). The runtime is not hermetic — it wrote to a real host path during boot."""


class CalibrationError(RuntimeError):
    """Raised when the noise band cannot be calibrated (e.g. < 2 baseline samples — a
    single sample has no spread, so the harness/calibration is broken; 03_metric §5).
    The spine never defaults a band silently — an undefined band would hide a broken
    ruler and let sub-jitter "wins" through."""


def compute_noise_band(
    samples: list[float], *, floor: float = 0.0, cap: float | None = None
) -> float:
    """The general §5.2 noise-band rule: ``noise_band = max(2σ, floor)`` over the
    UNTOUCHED-baseline samples (σ = population stdev, the spread of the baseline jitter,
    not an inferential interval — same estimator the reference profiles use). A FLOOR
    keeps a deceptively quiet calibration window from accepting sub-jitter "wins"
    (03_metric §5.2 "max(2σ, floor)").

    Sample-count handling:
      - 0 samples: the harness produced no measurement at all → genuinely broken, raise.
      - 1 sample: a single boot has no measurable spread, so σ is undefined; fall back to
        the FLOOR band. This is the deliberate fast-calibration path (a 1-rep setup for
        quick adoption) — the band is conservative (floor-only) but honest, not fabricated.
      - ≥2 samples: the real ``max(2σ, floor)``.
    """
    if not samples:
        raise CalibrationError(
            "no baseline samples produced — the harness/calibration is broken "
            "(every measurement boot failed to yield a delta)"
        )
    if len(samples) < 2:
        # Single sample → no spread to measure; the floor is the honest band. Clamp
        # to >= 0 so a negative floor can never yield a NEGATIVE band — the same
        # "band is never negative" invariant the ≥2-sample max(2σ, floor) path holds
        # (2σ >= 0). A negative band would invert the canary verdict and let a
        # regression "clear" the band (03_metric §5.2).
        return max(floor, 0.0)
    band = max(2.0 * statistics.pstdev(samples), floor)
    # OPERATOR OVERRIDE (reversible). On a NOISY shared host the 2σ term can balloon so wide
    # that even a real known win can't clear it and every candidate is discarded as noise →
    # the loop never files a CR. Capping the band lets a genuine above-cap win register.
    # WEAKENS the anti-noise gate, so OFF by default; for a bounded demo/validation run only.
    # PRECEDENCE: explicit ``cap`` arg (config bandCapMs → BudgetCaps, the path that survives
    # the measurement sandbox's env scrub) > AUTO_IMPROVEMENT_BAND_CAP_MS env (operator
    # fast-path, only effective where env is preserved). Never below the floor.
    cap_val = cap if cap is not None else _band_cap_from_env()
    if cap_val is not None and cap_val > 0 and cap_val < band:
        band = max(cap_val, floor)
    return band


def _band_cap_from_env() -> float | None:
    """Parse ``AUTO_IMPROVEMENT_BAND_CAP_MS`` (positive float) or None. NOTE: the measurement
    sandbox scrubs ``AUTO_IMPROVEMENT_*`` from the app-backend env, so this env path is only
    effective in non-sandboxed contexts — the reliable path is the ``cap`` arg (config)."""
    raw = os.environ.get("AUTO_IMPROVEMENT_BAND_CAP_MS")
    if not raw:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


@dataclass
class PreflightResult:
    """The Phase-1 pre-flight verdict the driver records before entering Phase 2.

    ``ok`` is True iff ALL THREE gates passed (calibration produced a band, the canary
    cleared it, and the do-not-pollute diff was zero) — the single boolean the driver
    keys "enter Phase 2" on. The component fields are kept so the driver can log/persist
    the proof (the calibrated band, the canary delta, the pollute result)."""

    ok: bool
    noise_band: float = 0.0
    baseline_n: int = 0
    canary_delta: float | None = None
    canary_cleared: bool = False
    pollute: PolluteResult | None = None
    note: str = ""


def _canary_clears_band(canary: Measurement, *, band: float, direction: str) -> bool:
    """The mandatory canary verdict (03_metric §7.1): the forced KNOWN win must clear the
    calibrated band in the IMPROVING direction. For ``minimize`` (the common case) a real
    win is a NEGATIVE delta and clears iff ``delta < -band``; for ``maximize`` it clears
    iff ``delta > band``. A delta inside the band (or pointing the wrong way) means the
    ruler cannot resolve a win it is CONFIDENT is large — so the ruler is broken."""
    if not canary.ok or canary.primary_delta is None:
        return False
    if direction == "maximize":
        return canary.primary_delta > band
    return canary.primary_delta < -band  # minimize: improvement is more-negative


def calibrate_and_prove(
    profile: TargetProfile,
    *,
    base_src: Path,
    boot: BootCallable,
    logger: logging.Logger | None = None,
    canary_advisory: bool = False,
    band_cap_ms: float | None = None,
) -> PreflightResult:
    """Run the three Phase-1 gates and return the verdict (03_metric §0/§11).

    1) calibrate the band from ``profile.ruler.baseline_samples`` (HALT-free: a
       calibration error raises :class:`CalibrationError`); 2) force the canary via
       ``profile.ruler.measure_canary`` and require it clears the band (else
       :class:`RulerNotTrustedError`); 3) run the do-not-pollute test over
       ``profile.isolation.do_not_pollute_paths()`` with ``boot`` and require a zero diff
       (else :class:`HostPollutionError`). Returns a :class:`PreflightResult` only when
       ALL THREE pass — the caller (driver) treats any raise as "do NOT enter Phase 2".

    ``canary_advisory`` (default False = strict, the §7.1 contract): when True, a canary
    that does NOT clear the band WARNS and the run PROCEEDS (``canary_cleared=False`` in
    the result) instead of raising :class:`RulerNotTrustedError`. This mirrors the
    backend's advisory ``calibrate()`` policy (``canaryStrict=false``) — a noisy band on a
    short (few-rep) calibration shouldn't HALT the whole run, since the per-candidate
    keeper still requires each real win to clear the band anyway. The do-not-pollute gate
    stays HARD regardless (a host leak always blocks — safety, not sensitivity).

    ``base_src`` is the untouched baseline tree the ruler calibrates against; ``boot`` is
    the profile/driver-supplied measurement-runtime boot callable (opaque to the spine).
    """
    log = logger or logging.getLogger("auto_improvement.preflight")
    cal = profile.calibration

    # ── (1) calibrate the noise band (§5) ─────────────────────────────────────
    samples = profile.ruler.baseline_samples(base_src=base_src, reps=cal.baseline_reps)
    band = compute_noise_band(samples, floor=cal.floor, cap=band_cap_ms)
    log.info(
        "preflight: calibrated noise band = %.3f (max(2σ, floor=%.3f) over %d reps)",
        band,
        cal.floor,
        len(samples),
    )

    # ── (2) canary — the ruler must SEE a known win, or HALT (§7) ──────────────
    canary = profile.ruler.measure_canary(base_src=base_src)
    cleared = _canary_clears_band(canary, band=band, direction=profile.ruler.direction)
    if not cleared:
        msg = (
            f"the canary ({cal.canary_id or 'forced known win'}) did not clear the band "
            f"(delta={canary.primary_delta}, band=±{band:.3f}, ok={canary.ok}). "
            "The ruler is too noisy/insensitive (03_metric §7.1)."
        )
        if not canary_advisory:
            raise RulerNotTrustedError("ruler not trusted — refusing Phase 2: " + msg)
        # Advisory: warn + proceed. The keeper still gates every real win on the band, so a
        # noisy canary degrades sensitivity but does not invalidate a clean above-band win.
        log.warning("preflight: CANARY did NOT clear the band (advisory, proceeding): %s", msg)
    else:
        log.info(
            "preflight: CANARY cleared the band (delta=%.3f, band=±%.3f) — ruler proven",
            canary.primary_delta,
            band,
        )

    # ── (3) do-not-pollute — the runtime must be hermetic, or BLOCK (§7.3) ─────
    paths = list(profile.isolation.do_not_pollute_paths())
    # Optional, profile-supplied: subpaths to ignore inside a snapshot root the
    # ORCHESTRATOR shares with the host (the app's own data dir under the data home — the
    # host writes there by design during a run; it is not the measured runtime's
    # footprint). A recipe without this method snapshots everything (back-compat).
    excl_fn = getattr(profile.isolation, "do_not_pollute_excludes", None)
    exclude = list(excl_fn()) if callable(excl_fn) else None
    pol = pollute.run_do_not_pollute(paths=paths, boot=boot, exclude=exclude)
    if pol.blocked:
        raise HostPollutionError(
            "do-not-pollute test FAILED — run BLOCKED: the runtime wrote to "
            f"{len(pol.changed_paths)} real host path(s) during boot "
            f"({', '.join(pol.changed_paths[:5])}). Fix the leak before any autonomous "
            "run (08_safety §2.2)."
        )
    log.info(
        "preflight: do-not-pollute test PASSED (zero host-state diff over %d paths)",
        pol.snapshotted,
    )

    return PreflightResult(
        ok=True,
        noise_band=band,
        baseline_n=len(samples),
        canary_delta=canary.primary_delta,
        canary_cleared=cleared,
        pollute=pol,
        note=(
            "ruler proven (band calibrated, canary cleared) + runtime hermetic"
            if cleared
            else "runtime hermetic; canary advisory (did not clear band — sensitivity warning)"
        ),
    )
