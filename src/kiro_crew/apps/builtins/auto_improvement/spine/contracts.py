"""Typed input/output contracts for the spine (the per-phase exchange DTOs).

This module holds the leaf dataclasses every spine module (driver, proposer, gate,
measurer, keeper) exchanges across phases A–E — :class:`Candidate`,
:class:`DiscoveryResult`, :class:`Proposal`, :class:`GateResult`,
:class:`StageBreakdown`, :class:`Measurement`, :class:`Verdict`. These are the
spine's *internal* I/O contracts, fixed in M0 so the engine can be written without
naming any target.

The **Target Profile seam** itself — the six typed fields the spine calls
(:class:`~.profile.Ruler`, :class:`~.profile.BuildGate`,
:class:`~.profile.EditAllowlist`, :class:`~.profile.IsolationRecipe`,
:class:`~.profile.PRRecipe`, :class:`~.profile.CalibrationParams`) and
:class:`~.profile.TargetProfile` — lives in :mod:`.profile` (milestone M1). It is
re-exported from the bottom of this module so existing ``from .contracts import
TargetProfile`` call-sites keep working unchanged.

Spine vs Profile (02_architecture.md §6, §7; 07_*.md §4 audit):
  - SPINE owns the *discipline* of each phase (fan-out shape, deterministic gate,
    serial-pinned-interleaved measure, accept-predicate structure, dedup) and these
    exchange DTOs.
  - PROFILE supplies the *payload*: the ruler (metric + harness), the build/test
    command, the edit allowlist, the isolation recipe, the CR recipe, the
    calibration params — each a NAMED field on :class:`~.profile.TargetProfile`.

CRITICAL (10_implementation_roadmap.md M0 exit / M1): the spine references the
profile ONLY through the seam in :mod:`.profile`. None of the target-environment
tokens the M0/M1 exit grep targets (build tools, the model provider, config flags,
target paths) appear anywhere in the spine — those live exclusively inside a concrete
profile implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Track kinds. Perf candidates carry a behavior-preserving change + a hypothesized
# stage win; bug candidates carry a reproducing test + a fix (RED/GREEN). The
# ledger fingerprints on ``kind`` so the two dedup independently (02_arch §6.9,
# 05_improvement_loop_bugfix.md, M4).
TRACK_PERF = "perf"
TRACK_BUG = "bug"


# Bug-track gate outcome reasons (05_improvement_loop_bugfix.md §2.2, §3.2, §5.3).
# These are the granular RED/GREEN/STAYGREEN + static-triage reasons the spine's
# bug gate emits; the driver maps each onto the shared ledger's outcome vocabulary
# (§5.3): the static-triage reasons map to ``failed_gate``; the RED/GREEN/flake
# reasons map to ``failed_verify``; ``bug_filed`` maps to ``filed``.
BUG_FILED = "bug_filed"  # RED ∧ GREEN ∧ STAYGREEN → draft CR  (→ filed)
BUG_FAILED_BUILD = "failed_build"  # T0 build/imports smoke failed       (→ failed_gate)
BUG_FAILED_LINT = "failed_lint"  # T1 new lint/static violation        (→ failed_gate)
BUG_TEST_INVALID = "test_invalid"  # T2 repro test does not collect       (→ failed_gate)
BUG_NOT_RED = "not_red"  # repro test PASSED on base (vacuous)  (→ failed_verify)
BUG_TEST_FLAKY = "test_flaky"  # repro test FAIL-then-PASS on base    (→ failed_verify)
BUG_NOT_GREEN = "not_green"  # fix did NOT turn the repro test green (→ failed_verify)
BUG_REGRESSED = "regressed"  # full suite regressed under the fix   (→ failed_verify)
BUG_ERROR = "bug_error"  # harness/tooling error during the gate (→ error)


# ── Phase A: discovery output → Phase B input ───────────────────────────────


@dataclass
class BugReproducingTest:
    """The reproducing-test contract a bug candidate carries (05_*.md §1.3).

    All fields are SPINE-OPAQUE strings the profile's bug runner interprets — the
    spine never parses a test id or a path. ``test_id`` names the single test the
    RED/GREEN gate runs in isolation (it must FAIL on base, PASS on fix). The gate
    is target-agnostic; what a ``test_id`` *means* (a test-runner nodeid, a spec id, …)
    is the profile's business (05_*.md §1.3 "Kiro Crew-specific vs general").
    """

    test_id: str = ""  # e.g. "test/test_x.py::test_error_frame_surfaces" (opaque)
    test_path: str = ""  # file the test lives in (for the T2 collection check)
    added_by_candidate: bool = True  # the test is part of the candidate's diff
    expected_on_base: str = "FAIL"  # RED precondition (documentation/assertion)
    expected_on_fix: str = "PASS"  # GREEN postcondition (documentation/assertion)


@dataclass
class Candidate:
    """One idea seed emitted by discovery (Phase A), consumed by the proposer.

    A *perf* candidate is a hypothesis ("change X, expect stage Y to drop"); a
    *bug* candidate carries a reproducing test locus. The ``target`` string is
    the opaque code locus the ledger fingerprints on — the spine never parses it.

    The bug-track fields (``reproducing_test`` / ``fix_diff`` / ``blast_radius`` /
    ``severity_note``) are populated ONLY for ``kind == TRACK_BUG`` candidates and
    carry exactly the §1.3 bug-candidate shape: a reproducing test + a fix diff +
    the files touched + the user-visible symptom. A perf candidate leaves them at
    their defaults. The spine treats them opaquely (05_*.md §1.3).
    """

    kind: str  # TRACK_PERF | TRACK_BUG
    target: str  # opaque "<path>::<symbol>" locus (profile-shaped; spine-opaque)
    signature: str = ""  # short human description of the inefficiency/bug
    hypothesis: str = ""  # what to change / why it should help
    evidence: str = ""  # profiler / static-analysis evidence backing the idea
    scenario: str = ""  # which ruler scenario/prompt to measure against
    confidence: float = 0.0

    # ── bug-track payload (05_*.md §1.3); empty/None for perf candidates ──────
    reproducing_test: BugReproducingTest | None = None
    fix_diff: str = ""  # unified diff: test + source change (the candidate's fix)
    blast_radius: list[str] = field(default_factory=list)  # files touched (review/dedup)
    severity_note: str = ""  # user-visible symptom (e.g. "errors look like empty replies")


@dataclass
class DiscoveryResult:
    """Phase A output: a ranked candidate list + the per-stage breakdown context
    that feeds the next cycle's proposer prompts (top-K evolutionary memory)."""

    candidates: list[Candidate] = field(default_factory=list)
    notes: str = ""


# ── Phase B: proposer output (one per candidate) ────────────────────────────


@dataclass
class Proposal:
    """A candidate edit produced in its own git worktree (Phase B).

    The proposer self-builds the edit in the worktree before emitting; the diff
    is captured vs the cycle's base. ``worktree`` / ``branch`` are torn down by
    the spine after the verdict (only the winner is applied to the branch).
    """

    cand_id: str
    candidate: Candidate
    worktree: Path
    branch: str
    description: str
    diff: str = ""  # unified diff vs the cycle base (filled by the proposer)
    tier: str = "wide"  # "wide" (cheap×N) | "deep" (strong×1)
    skipped: bool = False  # e.g. seed anchor missing — measured as no_candidate
    skip_reason: str = ""
    # WHY the proposal was skipped, as a ledger-status hint the driver records verbatim:
    # "" (not skipped) | "no_defect" (agent investigated, found nothing → no diff) |
    # "error" (a real exception during propose). Lets the driver distinguish an honest
    # no-find from a tooling failure WITHOUT parsing ``skip_reason`` text. Defaults to
    # "no_defect" because a clean no-diff is the common, non-error skip.
    skip_status: str = "no_defect"


# ── Phase C: correctness gate ───────────────────────────────────────────────


@dataclass
class GateResult:
    """Deterministic correctness gate verdict (Phase C). Boolean only — noise is
    irrelevant here. ``commit_sha`` is recorded for the §2.2 same-sha assertion
    Phase D performs before measuring."""

    passed: bool
    commit_sha: str = ""
    detail: str = ""
    failing_tests: list[str] = field(default_factory=list)


@dataclass
class BugGateResult:
    """The bug-track RED/GREEN gate verdict (05_improvement_loop_bugfix.md §2).

    A *boolean* state transition (RED → GREEN → STAYGREEN) — not a measured delta,
    so there is no noise band, anchor, or canary here (§2.4, §6.1). ``passed`` is
    True iff all three boolean checks held; ``reason`` is one of the granular
    ``BUG_*`` reasons (which the driver maps onto the shared ledger's outcome
    vocabulary, §5.3). The three booleans + the static-triage flags are carried so
    the CR's *correctness narrative* (§4.2) can state RED-on-base / GREEN-on-fix /
    suite-stayed-green explicitly.
    """

    passed: bool
    # one of the BUG_* reasons; BUG_FILED iff passed. The default is the
    # conservative BUG_ERROR (-> 'error' bucket), NOT BUG_FILED: a result built
    # without an explicit reason (e.g. ``BugGateResult(passed=False)``) must
    # never be misclassified as a successfully filed bug.
    reason: str = BUG_ERROR
    # the three RED/GREEN/STAYGREEN booleans (for the CR correctness narrative §4.2)
    red: bool = False  # repro test FAILED on base (twice — flake check §2.5)
    green: bool = False  # repro test PASSED on the fix
    staygreen: bool = False  # the full suite stayed green under the fix
    # static-triage ladder results (T0 build / T1 lint / T2 collect, §3.2)
    build_ok: bool = False
    lint_ok: bool = False
    collected: bool = False
    failing_tests: list[str] = field(default_factory=list)  # suite regressions, if any
    detail: str = ""


# ── Phase D: measurement (the ruler payload) ────────────────────────────────


@dataclass
class StageBreakdown:
    """Attributable per-stage decomposition keyed on STABLE MARKERS (not
    file:line — diffs shift line numbers each cycle; 02_arch §6.2)."""

    stages: dict[str, float] = field(default_factory=dict)


@dataclass
class Measurement:
    """Phase D output: the paired primary delta + stages + guardrails + RH probe.

    The spine reads ``primary_delta`` against the noise band and ``guardrails``
    against tolerances; the ruler (profile) produces all of it. A *negative*
    ``primary_delta`` means the candidate IMPROVED the minimized metric.

    ``guardrails`` are BLOCKING: the keeper rejects a candidate whose guardrail
    value exceeds its tolerance (a positive value is a regression magnitude). Use
    this channel ONLY for metrics that must not regress (the profile's G-set).

    ``secondary`` is NON-BLOCKING observability: extra per-candidate metrics the
    ruler sampled (e.g. resource cost / throughput) that the archive surfaces in
    ``results.tsv`` + the progress data for attribution, but that the keeper NEVER
    reads as an accept/reject term. This is where a measurement's sampled resource
    cost (RSS / CPU) and streaming throughput live — they are tracked, not gated,
    so a candidate is never rejected merely for using memory.
    """

    ok: bool  # measurement ran cleanly (vs an error / harness failure)
    primary_delta: float | None = None  # paired delta vs current best (the keep number)
    primary_base: float | None = None
    primary_cand: float | None = None
    noise_band: float | None = None
    stages: StageBreakdown = field(default_factory=StageBreakdown)
    # BLOCKING: metric -> candidate value/delta; the keeper rejects value > tolerance.
    guardrails: dict[str, float] = field(default_factory=dict)
    # NON-BLOCKING observability: metric -> sampled value (resource cost / throughput).
    secondary: dict[str, float] = field(default_factory=dict)
    rh_capability_ok: bool = True  # RH-A: capability set >= baseline (no silent shrink)
    rh_functional_ok: bool = True  # RH-B: functional probe exercised the capability and passed
    note: str = ""


# ── Phase E: keeper verdict ─────────────────────────────────────────────────


@dataclass
class Verdict:
    """Phase E decision the driver applies to the branch. ``keep`` advances the
    branch HEAD with this proposal's diff; otherwise revert == "don't apply"."""

    keep: bool
    status: str  # "kept" | "discard_<reason>" (see keeper)
    winner: Proposal | None = None
    measurement: Measurement | None = None
    reason: str = ""


# ── The Target Profile seam (re-exported from .profile; the M1 fields) ───────
#
# The seam — CalibrationParams, the five field protocols (Ruler, BuildGate,
# EditAllowlist, IsolationRecipe, PRRecipe), and TargetProfile — is DEFINED in
# .profile (milestone M1). It is imported here so the established call-sites
# (`from .contracts import TargetProfile`, etc.) and the package __init__ keep
# working unchanged. The import lives at the BOTTOM of this module, after the leaf
# DTOs above are defined, so the one-directional arrow profile -> contracts has no
# cycle (profile imports only the DTOs, which are already bound by the time this
# line runs).
from .profile import (  # noqa: E402  (intentional bottom import to break the cycle)
    BugRunner,
    BuildGate,
    CalibrationParams,
    EditAllowlist,
    IsolationRecipe,
    PRRecipe,
    Ruler,
    TargetProfile,
)

__all__ = [
    # leaf exchange DTOs (defined here)
    "Candidate",
    "BugReproducingTest",
    "DiscoveryResult",
    "Proposal",
    "GateResult",
    "BugGateResult",
    "StageBreakdown",
    "Measurement",
    "Verdict",
    "TRACK_PERF",
    "TRACK_BUG",
    # bug-track gate reasons (mapped to ledger statuses by the driver, §5.3)
    "BUG_FILED",
    "BUG_FAILED_BUILD",
    "BUG_FAILED_LINT",
    "BUG_TEST_INVALID",
    "BUG_NOT_RED",
    "BUG_TEST_FLAKY",
    "BUG_NOT_GREEN",
    "BUG_REGRESSED",
    "BUG_ERROR",
    # the Target Profile seam (defined in .profile, re-exported)
    "Ruler",
    "BuildGate",
    "BugRunner",
    "EditAllowlist",
    "IsolationRecipe",
    "PRRecipe",
    "CalibrationParams",
    "TargetProfile",
]
