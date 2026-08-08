"""Paired A/B comparison for benchmark metrics.

The statistical protocol is lifted from ``auto_improvement/spine/measurer.py``,
which is the one place in this repo that already gets measurement discipline
right: warmups discarded, at least two reps, **median never mean**, arms measured
**interleaved** so host drift cancels in the paired delta, and a mandatory
sensitivity check that must clear the noise band before any result is believed.
What is not reusable is its code — it is shaped around a ``Proposal`` carrying a
git worktree, and its metric is a duration to be *minimized*. Here the metric is a
quality score in [0, 1] to be *maximized*, and the arms are two configurations or
two commits, not two worktrees. So the protocol is reused and the plumbing is not.

One adaptation matters more than the rest. The retrieval ruler is **deterministic**
— local embedder, deterministic ranker, no sampling — so its delta is exact and a
single rep is not an approximation of the truth, it *is* the truth. Running reps
against it would produce identical numbers and a spread of zero, which reads as
false precision. The end-to-end answer scorers are the opposite: Kiro Crew threads
no ``temperature`` or ``seed`` through its provider stack, so those scores are
random variables and a delta smaller than the noise band means nothing.

Conflating the two is how a harness starts reporting confidence it has not earned,
so :class:`ArmResult` carries ``deterministic`` and the comparison refuses to
attach a noise band to an exact number, or to omit one from a noisy one.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Callable, Sequence

#: Floor from measurer.py's own rep handling: a single-rep arm has no spread and
#: cannot be trusted, so a stochastic comparison needs at least two.
MIN_REPS = 2
DEFAULT_REPS = 5
DEFAULT_WARMUPS = 1


@dataclass(frozen=True)
class ArmResult:
    """One arm's measurements of one metric.

    ``values`` holds the per-rep scores in rep order, warmups already discarded.
    """

    name: str
    metric: str
    values: tuple[float, ...]
    deterministic: bool

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError(f"arm {self.name!r} has no measurements")
        if self.deterministic and len(set(self.values)) > 1:
            raise ValueError(
                f"arm {self.name!r} was declared deterministic but produced "
                f"{len(set(self.values))} distinct values {sorted(set(self.values))}. "
                "Either the metric is not deterministic or the arms are not "
                "isolated — both invalidate the comparison, so this refuses rather "
                "than averaging the discrepancy away."
            )

    @property
    def center(self) -> float:
        """Median, not mean. A single slow or unlucky rep must not move the result."""
        return statistics.median(self.values)

    @property
    def spread(self) -> float:
        """Half the interquartile-ish range; 0.0 for a deterministic arm."""
        if self.deterministic or len(self.values) < 2:
            return 0.0
        return (max(self.values) - min(self.values)) / 2.0


@dataclass
class Comparison:
    """The verdict, with the reason it is or is not believable attached."""

    metric: str
    baseline: ArmResult
    candidate: ArmResult
    noise_band: float
    higher_is_better: bool = True
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def delta(self) -> float:
        return self.candidate.center - self.baseline.center

    @property
    def relative(self) -> float | None:
        base = self.baseline.center
        return (self.delta / base) if base else None

    @property
    def deterministic(self) -> bool:
        return self.baseline.deterministic and self.candidate.deterministic

    @property
    def verdict(self) -> str:
        """One of ``improved`` / ``regressed`` / ``unchanged`` / ``inconclusive``.

        ``unchanged`` and ``inconclusive`` are deliberately different words.
        A deterministic comparison with a zero delta really is unchanged. A noisy
        comparison whose delta sits inside the noise band is *inconclusive* — the
        fix may well have helped by less than this instrument can see, and calling
        that "unchanged" would license the wrong conclusion.
        """
        if self.delta == 0:
            return "unchanged"
        if not self.deterministic and abs(self.delta) <= self.noise_band:
            return "inconclusive"
        better = self.delta > 0 if self.higher_is_better else self.delta < 0
        return "improved" if better else "regressed"

    def summary(self) -> str:
        arrow = "+" if self.delta >= 0 else ""
        band = (
            "exact (deterministic metric)"
            if self.deterministic
            else f"noise band ±{self.noise_band:.4f}"
        )
        rel = f" ({arrow}{self.relative * 100:.2f}%)" if self.relative is not None else ""
        return (
            f"{self.metric}: {self.baseline.center:.4f} → {self.candidate.center:.4f}  "
            f"{arrow}{self.delta:.4f}{rel}  [{self.verdict}; {band}]"
        )


def noise_band_from(values: Sequence[float]) -> float:
    """Two standard deviations of an untouched baseline, as measurer.py does.

    A band derived from the baseline's own repeated measurements answers "how big a
    difference can this instrument see on this host today", which is the only
    question that makes a delta interpretable. Returns 0.0 for fewer than two
    samples rather than a fabricated number.
    """
    if len(values) < 2:
        return 0.0
    return 2.0 * statistics.stdev(values)


def measure_arm(
    name: str,
    metric: str,
    run: Callable[[], float],
    *,
    deterministic: bool,
    reps: int = DEFAULT_REPS,
    warmups: int = DEFAULT_WARMUPS,
) -> ArmResult:
    """Run one arm.

    A deterministic metric is measured exactly once and no warmup is run: repeating
    an exact computation buys nothing, and a warmup would only be discarding an
    identical value. A stochastic metric gets ``warmups`` discarded reps first,
    because the first call through a cold provider carries process-start and
    model-load cost that is not part of what is being compared.
    """
    if deterministic:
        return ArmResult(name=name, metric=metric, values=(run(),), deterministic=True)

    reps = max(MIN_REPS, reps)
    for _ in range(max(0, warmups)):
        run()
    return ArmResult(
        name=name,
        metric=metric,
        values=tuple(run() for _ in range(reps)),
        deterministic=False,
    )


def compare_interleaved(
    metric: str,
    baseline: Callable[[], float],
    candidate: Callable[[], float],
    *,
    deterministic: bool,
    higher_is_better: bool = True,
    reps: int = DEFAULT_REPS,
    warmups: int = DEFAULT_WARMUPS,
) -> Comparison:
    """Measure both arms interleaved, baseline first in each pair.

    Interleaving is the load-bearing part. Measuring all of arm A then all of arm B
    lets any drift over the run — a busy host, a provider slowing under load, a
    model rollout mid-run — land entirely on one arm and masquerade as the effect
    being measured. Alternating puts each pair of measurements in the same
    conditions, so drift cancels in the paired delta instead of becoming the result.
    """
    if deterministic:
        base = ArmResult(metric=metric, name="baseline", values=(baseline(),), deterministic=True)
        cand = ArmResult(metric=metric, name="candidate", values=(candidate(),), deterministic=True)
        return Comparison(
            metric=metric,
            baseline=base,
            candidate=cand,
            noise_band=0.0,
            higher_is_better=higher_is_better,
            notes=("deterministic metric: single pass, exact delta, no band",),
        )

    reps = max(MIN_REPS, reps)
    for _ in range(max(0, warmups)):
        baseline()
        candidate()

    base_vals: list[float] = []
    cand_vals: list[float] = []
    for _ in range(reps):
        base_vals.append(baseline())
        cand_vals.append(candidate())

    base = ArmResult(metric=metric, name="baseline", values=tuple(base_vals), deterministic=False)
    cand = ArmResult(metric=metric, name="candidate", values=tuple(cand_vals), deterministic=False)
    band = noise_band_from(base_vals)
    notes: list[str] = [f"{reps} interleaved paired reps, {warmups} warmup(s) discarded"]
    if band == 0.0:
        notes.append(
            "baseline produced zero spread across reps; the band is 0 so any "
            "non-zero delta will read as conclusive — treat with suspicion rather "
            "than confidence, it usually means too few reps"
        )
    return Comparison(
        metric=metric,
        baseline=base,
        candidate=cand,
        noise_band=band,
        higher_is_better=higher_is_better,
        notes=tuple(notes),
    )


def sensitivity_check(
    metric: str,
    run: Callable[[], float],
    degraded: Callable[[], float],
    *,
    noise_band: float,
    reps: int = DEFAULT_REPS,
) -> tuple[bool, str]:
    """Prove the instrument can see a difference that is known to exist.

    measurer.py calls this a canary and refuses to believe a result without one,
    which is the single most valuable habit in that module. The argument is simple:
    if a deliberately degraded configuration does not score measurably worse, then
    the ruler cannot resolve changes of that size and a null result from it means
    nothing. A ruler that reports "no change" because it is blind looks exactly like
    a ruler that reports "no change" because there was none.

    ``degraded`` should be a configuration known to be worse — for the retrieval
    ruler, asking for a top-1 window instead of top-10 is a good one.
    """
    good = measure_arm("canary_good", metric, run, deterministic=False, reps=reps, warmups=0)
    bad = measure_arm("canary_bad", metric, degraded, deterministic=False, reps=reps, warmups=0)
    drop = good.center - bad.center
    if drop <= noise_band:
        return False, (
            f"canary failed: degrading the configuration moved {metric} by only "
            f"{drop:.4f}, which does not clear the noise band of {noise_band:.4f}. "
            "This ruler cannot resolve a change of that size on this host, so a "
            "null result from it is not evidence of no change."
        )
    return True, (
        f"canary cleared: a known degradation moved {metric} by {drop:.4f} "
        f"(band {noise_band:.4f}), so the ruler resolves at least that much"
    )
