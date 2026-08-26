"""The Target Profile interface — the generalization seam (milestone M1).

This module is the *one* place the six target-specific touchpoints are named as a
typed interface. The spine (driver / proposer / gate / measurer / keeper / ledger)
consumes a target ONLY through :class:`TargetProfile`; nothing else in the spine
references a build tool, a model provider, a config flag, or a target path — those
live exclusively inside a concrete profile implementation. (10_implementation_roadmap.md
"M1 — Target Profile interface" exit criterion; 07_generalization_and_target_profiles.md
§1; 00_VISION_PRD §6.)

The six fields are exactly the seam enumerated in 07_*.md §1.0 / the M1 field table:

  ① ``ruler``          metric (primary label/unit/direction + sub-stages + guardrails
                       + RH guards + measurement-constants) + the measurement-harness
                       adapter the spine shells out to in Phase D.   (07_*.md §1.1)
  ② ``build_gate``     how to build + run tests, returning a boolean + commit sha;
                       and (bug track) the RED/GREEN assertion.       (07_*.md §1.2)
  ③ ``edit_allowlist`` which paths the agent may touch; everything else is a
                       mechanical reject (``allowed`` + ``off_limits``).  (07_*.md §1.3)
  ④ ``isolation``      push-disabled clone + ephemeral / do-not-pollute runtime.
                                                                       (07_*.md §1.4)
  ⑤ ``pr_recipe``      how to draft a CR in the target's review system (draft-only).
                                                                       (07_*.md §1.5)
  ⑥ ``calibration``    noise-band reps, anchors, canary win, held-out scenarios.
                                                                       (07_*.md §1.6)

WHY ``Protocol`` and not ``ABC``: a profile is a *bundle of adapters*, often realized
as plain objects assembled at config-load time (07_*.md §1: "a declarative manifest
plus a small set of adapter callables"). Structural typing lets a profile satisfy the
seam without inheriting from the spine — which keeps the dependency arrow pointing the
right way (profiles depend on the spine's shapes, the spine never imports a profile).
The protocols are ``runtime_checkable`` so the loader can ``isinstance``-validate a
loaded profile object before the driver trusts it.

Spine-vs-profile audit: 07_*.md §4. Every row marked **Profile** maps to a field here;
every row marked **Spine** stays in the engine and is identical across all profiles.

The leaf exchange dataclasses the seam references (:class:`Candidate`,
:class:`Measurement`, :class:`GateResult`, …) live in :mod:`.contracts` and are
re-exported there alongside this seam, so existing ``from .contracts import
TargetProfile`` call-sites keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

# Import only the leaf exchange DTOs the seam protocols reference. The import arrow
# is one-directional (profile -> contracts); contracts re-exports this seam at the
# bottom of its module, after those DTOs are defined, so there is no cycle.
from .contracts import (
    TRACK_PERF,
    Candidate,
    DiscoveryResult,
    GateResult,
    Measurement,
    StageBreakdown,
)
from .pollute import BootCallable

__all__ = [
    "CalibrationParams",
    "Ruler",
    "BuildGate",
    "BugRunner",
    "EditAllowlist",
    "IsolationRecipe",
    "PRRecipe",
    "ProfileFieldAliases",
    "TargetProfile",
    "StubProfile",
    "StubRuler",
    "StubBuildGate",
    "StubBugRunner",
    "StubAllowlist",
    "StubIsolation",
    "StubPRRecipe",
]


# ── ⑥ calibration params (the Phase-1 knobs that make the ruler trustworthy) ─


@dataclass
class CalibrationParams:
    """Field ⑥ — the Phase-1 knobs that make the ruler trustworthy *before* any
    optimization (07_*.md §1.6; 03_metric_design_and_calibration.md §5). The spine
    runs the calibration procedure (≈baseline_reps of the untouched baseline → 2σ
    band, then the canary); the profile supplies these parameters.

    The **canary is mandatory** — a known/forced win that MUST clear the band or the
    ruler is rejected and the spine HALTS before Phase 2 ("the evaluator is the
    project", 00_VISION_PRD §3 Phase 1 step 4). ``canary_id`` names it; how to force
    it is the profile's business (e.g. flip a known-disabled flag, or insert-then-
    remove an artificial cost).
    """

    baseline_reps: int = 30  # reps of the untouched baseline to characterize jitter
    noise_band: float = 0.0  # max(2σ, floor) below which a delta is no-change
    floor: float = 0.0  # absolute floor for the band (units are the ruler's primary)
    canary_id: str = ""  # the known/forced win that MUST clear the band, else HALT
    anchors: list[str] = field(default_factory=list)  # frozen reference measurements
    drift_reanchor_cycles: int = 5  # re-best every K cycles to catch host drift
    heldout: list[str] = field(default_factory=list)  # unseen scenarios (anti-overfit)


# ── ① ruler adapter (metric + measurement harness) ──────────────────────────


@runtime_checkable
class Ruler(Protocol):
    """Field ① — the metric + its measurement harness adapter (the single most
    target-specific thing, and the one the agent may NEVER edit; 07_*.md §1.1;
    00_VISION_PRD §5 non-goals). The strictly-serial / pinned / interleaved-A/B
    discipline is enforced by the spine's :mod:`.measurer`; the ruler only supplies
    the deterministic, no-LLM measurement payload.

    Declares the metric (``primary_name`` label, ``unit``, minimize/maximize
    ``direction``), the attributable ``substages`` so a win is pinned to a named
    stage, the ``guardrails`` that must not regress, the ``rh_guards`` that defeat
    metric-gaming the gate can't see, and the ``measurement_constants`` held
    byte-identical (off-limits). The harness emits, per timed rep, a structured row
    keyed on STABLE MARKERS — not source ``file:line`` (candidate diffs shift line
    numbers each cycle; 02_architecture.md §6.2; 07_*.md §1.1). That marker contract
    is spine-enforced; the profile guarantees the markers exist.
    """

    #: human-readable id of the primary metric (used in commit messages / logs).
    primary_name: str
    #: unit string for the primary metric (e.g. "ms", "bytes"); display only.
    unit: str
    #: "minimize" | "maximize" — the spine reads a *negative* delta as an
    #: improvement for "minimize" (the common case); profiles set this once.
    direction: str
    #: attributable sub-stage marker names (a win must be pinned to one of these).
    substages: list[str]
    #: metric names that must not regress beyond tolerance (07_*.md §1.1 guardrails).
    guardrails: list[str]
    #: target-specific reward-hacking guards (e.g. "no capability shrink",
    #: "functional probe passes") — names of the checks the measurement reports.
    rh_guards: list[str]
    #: the byte-identical incidental conditions held fixed (off-limits to the agent).
    measurement_constants: dict[str, str]

    def measure(
        self, *, base_src: Path, cand_src: Path, commit_sha: str, scenario: str
    ) -> Measurement:
        """Run the ruler ONCE (the measurer drives interleaving/repetition).

        Implementations MUST be deterministic (no creative LLM), MUST assert the
        candidate artifact is ``commit_sha`` (the same-sha contract, 02_arch §2.2),
        and MUST emit a paired primary delta + attributable stages + guardrails + RH
        probe results. ``cand_src`` / ``base_src`` are the bind-mountable trees.
        """
        ...

    # ── Phase-1 calibration hooks (03_metric_design_and_calibration.md §5/§7) ─────
    # The spine's PRE-FLIGHT (driver.preflight) RUNS the calibration procedure (collect
    # the untouched-baseline samples -> 2sigma band, then force the canary A/B); the
    # ruler SUPPLIES the deterministic, no-LLM payload for each step. This is the same
    # spine-runs / profile-supplies split as :meth:`measure` (03_metric §5.1; 07_*.md
    # §1.6). Both are part of the off-limits ruler the agent may never edit.

    def baseline_samples(self, *, base_src: Path, reps: int) -> list[float]:
        """Phase-1 calibration §5: measure the UNTOUCHED baseline ``reps`` times and
        return the per-rep PRIMARY-metric samples (serial, pinned — the discipline is
        the spine's; the timing is the ruler's). The spine computes
        ``noise_band = max(2sigma, floor)`` over this array (03_metric §5.1/§5.2). An
        empty/short list (< 2 samples) is a calibration error the spine surfaces (a
        single sample has no spread — the harness is broken)."""
        ...

    def measure_canary(self, *, base_src: Path) -> Measurement:
        """Phase-1 canary §7: force the KNOWN/forced win named by
        ``calibration.canary_id`` and measure it through the FULL ruler exactly as a
        normal candidate (the same paired primary delta). The spine then checks the
        canary clears the calibrated band (``|delta| > band`` in the improving
        direction); if it CANNOT, the ruler is too noisy/insensitive and the spine
        HALTS before Phase 2 ("the evaluator is the project"; 03_metric §7.1, §11.1).
        The ruler owns HOW to force the win (flip a known-disabled flag, etc.)."""
        ...


# ── ② build / test gate ──────────────────────────────────────────────────────


@runtime_checkable
class BuildGate(Protocol):
    """Field ② — how to build + run tests, returning a boolean + commit sha (Phase
    C; 07_*.md §1.2). The discipline (deterministic, boolean-only, records the
    passing sha, runs unmodified tests) is spine; the *command* is the profile's.

    ``single_environment`` keys the host-build / container-measure split: when False
    the build and the measure run in DIFFERENT environments and the spine asserts the
    same commit sha between them (02_arch §2.2); when True the gate and measurement
    are co-located and the spine skips that assertion (07_*.md §1.2 — "a capability of
    the schema, not a spine assumption").
    """

    #: True iff build+measure run in the same environment (skip the same-sha assert).
    single_environment: bool

    def build_and_test(self, *, worktree: Path, src: Path) -> GateResult:
        """Build the candidate + run the (unmodified) test scope. Boolean only;
        record the passing commit sha for Phase D's same-sha assertion."""
        ...


# ── ②b bug runner — the RED/GREEN test-runner primitives (M4) ────────────────


@runtime_checkable
class BugRunner(Protocol):
    """Field ②b — the bug track's deterministic test-runner primitives (M4;
    05_improvement_loop_bugfix.md §2, §3). The RED → GREEN → STAYGREEN orchestration,
    the static-triage ladder ordering, and the doubled-RED flake check are ALL in the
    spine (:class:`~.bug_gate.BugGate`) — target-agnostic; this protocol supplies only
    the deterministic primitives the gate composes. *What* command runs the test (the
    target's build/test toolchain) is the profile's; the gate discipline is the spine's
    (M4 generalization note: "it only needs the profile's test runner").

    No method is an LLM step — each is deterministic code the spine shells out to and
    cannot argue past (05_*.md §2.1). The primitives compose into the gate ladder:
      build_imports_ok      → T0 static triage (build/imports smoke, §3.2)
      lint_clean            → T1 static triage (no new lint violation, §3.2)
      test_collects         → T2 static triage (the repro test collects, §3.2)
      run_reproducing_test  → RED (test-only on base, twice) + GREEN (full fix), §2.2
      run_suite             → STAYGREEN (full suite / documented smoke subset), §2.2
    """

    def build_imports_ok(self, *, src: Path) -> bool:
        """T0: the candidate fix builds and its modules import (the cheapest signal,
        the bug-track analogue of the perf-track smoke gate). Boolean only."""
        ...

    def lint_clean(self, *, base_src: Path, cand_src: Path) -> bool:
        """T1: the candidate diff introduces NO new lint/static violations vs the
        base (the profile's linter; what counts as a "new" violation is profile-defined
        relative to ``base_src``). Boolean only."""
        ...

    def test_collects(self, *, src: Path, test_path: str) -> bool:
        """T2: the reproducing test file COLLECTS (no import/collection error) — a
        non-collecting test cannot be RED. Boolean only."""
        ...

    def run_reproducing_test(self, *, src: Path, test_id: str, test_only: bool) -> bool | None:
        """Run JUST the named reproducing test against ``src``. Returns ``True`` if it
        PASSED, ``False`` if it FAILED (a clean assertion failure), or ``None`` if it
        ERRORED (collection/import error — not an assertion failure; §2.2 distinguishes
        these). ``test_only`` is True for the RED run (apply only the test portion of
        the diff at base) and False for the GREEN run (the full fix is applied)."""
        ...

    def run_suite(self, *, src: Path) -> tuple[bool, list[str]]:
        """STAYGREEN: run the full suite (or the documented smoke subset — a
        Target-Profile choice, §8) against ``src``. Returns ``(all_green, failing)``
        where ``failing`` lists any previously-passing test now failing (exact pytest
        nodeids when available, so the gate can re-check them on base).

        OPTIONAL companion (NOT part of this structural protocol so a minimal runner
        still satisfies ``isinstance``): ``run_named_tests(*, src, test_ids) -> set[str]``
        runs ONLY ``test_ids`` against ``src`` and returns the subset that FAIL/ERROR.
        The gate calls it via ``getattr`` for the base-relative STAYGREEN check — a suite
        failure that ALSO fails on base is pre-existing, not a regression the fix caused
        (§8) — and degrades gracefully (treats all suite failures as regressions) when a
        runner doesn't provide it."""
        ...


# ── ③ edit allowlist / off-limits (mechanical path fence) ────────────────────


@runtime_checkable
class EditAllowlist(Protocol):
    """Field ③ — which paths the agent may touch (07_*.md §1.3; 08_safety §4.3).
    Enforcement is **mechanical** — a ``git diff --name-only`` path check in the
    spine's :class:`.gate.Gate` rejects any candidate touching a path outside the
    allowlist (``status=discard_offlimits``), "cheaper and more reliable than trusting
    the agent to self-police." The globs are the profile's; the spine also
    default-denies the four invariant categories (ruler/harness, tests, auth/security,
    measurement constants) the profile may only EXTEND, never relax.

    The two declarative lists are exposed so the loader/UI can render them and the
    spine can verify the profile does not try to relax a default-deny category.
    """

    #: glob patterns the agent MAY edit (only the layer under optimization).
    allowed: list[str]
    #: glob patterns strictly forbidden (auto-reject before measurement). Extends —
    #: never relaxes — the spine's four invariant default-deny categories.
    off_limits: list[str]

    def allows(self, changed_paths: list[str]) -> tuple[bool, list[str]]:
        """Return ``(ok, offending_paths)``. ``ok`` is False if ANY changed path is
        outside ``allowed``, hits ``off_limits``, or hits a spine default-deny
        category."""
        ...


# ── ④ isolation recipe (push-disabled clone + ephemeral runtime) ─────────────


@runtime_checkable
class IsolationRecipe(Protocol):
    """Field ④ — the push-disabled clone + ephemeral, do-not-pollute runtime for this
    target (07_*.md §1.4; 08_safety §1–§3 — the dominant safety mechanism). The spine
    enforces the *policy* (refuse to run unless push is disabled; the do-not-pollute
    test must diff to zero); the profile supplies the *paths* (clone location, the
    pinned base ref, host paths to snapshot, the frozen components, the ephemeral and
    read-only mounts). The general pattern is "freeze everything except the layer under
    optimization" — the spine never assumes any specific component is the frozen one.
    """

    #: filesystem location of the separate, push-disabled clone. MUST live OUTSIDE the
    #: app's ``data/`` dir (app guide §1); the spine writes only under ``data/``.
    clone_path: Path
    #: the pinned base ref the working branch forks off (recorded in run metadata).
    base_ref: str
    #: components held byte-identical for THIS target (the frozen layer).
    frozen_components: list[str]

    def push_disabled(self) -> bool:
        """True iff the clone's push remote is mechanically disabled. The driver
        REFUSES TO START otherwise (08_safety §1.3; M0 exit criterion). The profile
        owns *how* push is disabled; the spine owns the *refusal*."""
        ...

    def do_not_pollute_paths(self) -> list[Path]:
        """Host paths to snapshot/diff around a measurement boot; the spine requires
        the diff to be ZERO before any autonomous run (08_safety §2.2)."""
        ...

    def measurement_boot(self) -> BootCallable:
        """The profile-supplied "boot the measurement runtime once + tear it down"
        callable the spine's do-not-pollute acceptance test drives (08_safety §2.2 step
        2/3; §0.1 control table — the spine "runs the snapshot/boot/diff test", the
        profile supplies "the boot callable to boot+tear-down the measurement runtime").

        The returned callable boots the SAME measurement runtime a real Phase-D
        measurement boots (so the test proves the actual measured runtime is hermetic),
        then tears it down. It returns nothing the spine inspects — the WHOLE point is
        the spine measures the host-state delta the boot leaves behind (around
        :meth:`do_not_pollute_paths`), not anything the boot reports. The spine wraps
        this between a snapshot BEFORE and a snapshot AFTER and BLOCKS on any non-zero
        diff (the driver's pre-flight; 08_safety §2.2). It MUST NOT fabricate a clean
        boot — a runtime that cannot reach READY is a hard stop the callable surfaces by
        raising (the pollute machinery still records the partial-leak snapshot).

        A profile with no measurement runtime to boot (or whose isolation cannot be
        exercised in-process) returns a no-op callable (``lambda: None``); the test then
        degenerates to "the spine touched nothing", which is still a true zero-diff. The
        spine owns the snapshot/diff/block MACHINERY; the profile owns HOW to boot."""
        ...


# ── ⑤ CR recipe (draft a CR in the target's review system) ───────────────────


@runtime_checkable
class PRRecipe(Protocol):
    """Field ⑤ — how to draft a CR in the target's review system (07_*.md §1.5; M5).
    The verify → reproduce → draft → ledger pipeline is spine; the draft command +
    namespace are the profile's. The spine enforces draft-only / never-merge
    (the draft-only non-goal) — the *how* (an internal ``review --new
    --no-open`` vs. ``gh pr create --draft``) is opaque to the spine.
    """

    #: the user's personal namespace draft reviews land in (e.g. ``share/<user>``);
    #: display/metadata only — the spine never parses it.
    namespace: str

    def draft(
        self,
        *,
        summary: str,
        description: str,
        diff: str,
        fingerprint: str,
        parent_ref: str | None = None,
    ) -> str:
        """Create a DRAFT / unpublished review on the user's personal namespace.
        Returns a CR id (or a queue path if drafting is unavailable). MUST NOT publish
        or merge — the draft-only policy is enforced by the spine, realized here.

        ``parent_ref`` (optional) scopes the review to an explicit parent branch when the
        recipe supports it; older recipes that predate the param raise ``TypeError`` and the
        pipeline retries without it (the back-compat path in :mod:`.pr_pipeline`)."""
        ...


# ── the Target Profile (the six fields assembled) ────────────────────────────


# ── field-name aliases: isolation_recipe / calibration_params (G3b) ──────────


class ProfileFieldAliases:
    """Backward-compatible read-only aliases for two of the six profile fields.

    The 10_implementation_roadmap.md M1 table and 07_*.md §1.0 name two fields
    ``isolation_recipe`` and ``calibration_params``; the code (and the
    :class:`TargetProfile` protocol below) names them ``isolation`` and
    ``calibration`` for brevity (the ``Recipe``/``Params`` suffix is already in the
    field's TYPE — :class:`IsolationRecipe`, :class:`CalibrationParams`). To keep BOTH
    spellings working — so neither the docs' names nor the established call-sites/tests
    break — concrete profiles mix this in and gain ``isolation_recipe`` /
    ``calibration_params`` properties that resolve to the SAME objects as ``isolation``
    / ``calibration`` (G3b; 07_*.md §1.0 naming note).

    These are *aliases*, never a second source of truth: the property simply reads the
    canonical attribute, so ``profile.isolation is profile.isolation_recipe`` and
    ``profile.calibration is profile.calibration_params`` always hold. The spine itself
    keeps reading the short names; the aliases exist for doc-aligned external callers.
    """

    @property
    def isolation_recipe(self) -> "IsolationRecipe":
        """Alias for ``isolation`` (07_*.md / M1 table field ④ ``isolation_recipe``)."""
        return self.isolation  # type: ignore[attr-defined]

    @property
    def calibration_params(self) -> "CalibrationParams":
        """Alias for ``calibration`` (07_*.md / M1 table field ⑥ ``calibration_params``)."""
        return self.calibration  # type: ignore[attr-defined]


@runtime_checkable
class TargetProfile(Protocol):
    """The pluggable bundle that adapts the spine to one target — the six fields of
    00_VISION_PRD §6 / 07_*.md §1.0 (ruler, build gate, edit allowlist, isolation,
    CR recipe, calibration). A grep of the spine shows it consumes the profile ONLY
    through this interface (M1 exit criterion).

    The concrete reference profiles are **Backend-TTFT** (M2, from
    ``../auto_improvement/``) and **Frontend-shell** (M3, from
    ``../auto_improvement_frontend/``) — the two v1 reference Target Profiles
    (00_VISION_PRD §6). The M1 stub (:class:`StubProfile`) implements every field as
    a no-op so the spine runs ``--dry-run`` end-to-end without naming any target.

    NAMING (G3b; 07_*.md §1.0): fields ④/⑥ are named ``isolation`` / ``calibration``
    here for brevity; the doc / M1 table call them ``isolation_recipe`` /
    ``calibration_params``. Concrete profiles mix in :class:`ProfileFieldAliases` so
    BOTH spellings resolve to the same object. The spine reads the short names.

    Beyond the six DATA fields, a profile also supplies two CALLABLES the spine invokes
    at Phase A / Phase B: ``discover()`` (ranked candidate / reproducing-test seeds) and
    ``propose()`` (realize one candidate edit in a worktree). These are legitimate parts
    of the seam — the spine drives the loop discipline; the profile supplies the
    target-specific discovery and edit realization (07_*.md §1.0 / §4 audit rows 19–20).
    """

    #: a stable id (e.g. "backend-ttft", "frontend-shell"); used in run metadata/logs.
    id: str
    #: TRACK_PERF | TRACK_BUG — the default track this profile drives.
    track: str

    ruler: Ruler  # ①
    build_gate: BuildGate  # ②
    #: ②b — the bug-track test runner (M4). Optional: a perf-only profile may set it
    #: to ``None`` (the spine then refuses to run a bug candidate through that profile).
    #: When present, the spine's RED/GREEN gate composes its primitives (the gate is
    #: spine, the runner is profile — M4 generalization note).
    bug_runner: BugRunner | None
    edit_allowlist: EditAllowlist  # ③
    isolation: IsolationRecipe  # ④ (alias: ``isolation_recipe`` via ProfileFieldAliases)
    pr_recipe: PRRecipe  # ⑤
    calibration: CalibrationParams  # ⑥ (alias: ``calibration_params``)

    def discover(
        self,
        *,
        base_sha: str,
        top_k: list[dict],
        known_loci: list[str],
        agent_runner=None,
    ) -> DiscoveryResult:
        """Phase A. Profile-seeded discovery: return ranked candidates + idea seeds
        (perf) or reproducing-test candidates (bug). ``top_k`` is the archive's
        evolutionary memory; ``known_loci`` lets the profile pre-dedup.

        ``agent_runner`` (optional, keyword-only) is the run's model invoker. A profile
        MAY use it as a first-class discovery source (the bug track reads the code/diff
        and hypothesises testable defects); a profile that ignores it — or a run with no
        runner wired (offline) — behaves exactly as before. Implementations MUST accept
        and default it to None so the spine can always pass it."""
        ...

    def propose(self, *, candidate: Candidate, base_sha: str, worktree: Path, tier: str) -> bool:
        """Phase B. Apply ONE candidate's edit inside ``worktree`` (off ``base_sha``)
        and self-build it. Return True iff a real diff was produced. The fan-out shape
        (cheap×N + strong×1), worktree isolation, and teardown are spine — this only
        realizes a single edit."""
        ...


# ── the M1 stub Target Profile (drives one full --dry-run cycle) ─────────────
#
# A no-op profile that satisfies every TargetProfile field with a deterministic stub:
# a build gate that always passes, a constant ruler that reports a fixed band-clearing
# win, an allow-everything (empty) allowlist, an isolation recipe that reports push as
# disabled, and a CR recipe that writes a queue file instead of opening a review. This
# is the M1 exit artifact — "a stub Target Profile (no-op build gate that always passes,
# constant ruler, empty allowlist) drives one full spine --dry-run cycle"
# (10_implementation_roadmap.md M1). It carries NO target token; it is the reference for
# what a profile looks like, not a real target. The real profiles are Backend-TTFT (M2)
# and Frontend-shell (M3).


class StubRuler:
    """A constant ruler: reports a fixed, band-clearing improvement so the keeper
    accepts the candidate in a dry run. Deterministic, no LLM."""

    primary_name = "stub_metric"
    unit = "units"
    direction = "minimize"
    substages = ["stub_stage"]
    guardrails = ["stub_guardrail"]
    rh_guards = ["stub_capability", "stub_functional"]
    measurement_constants: dict[str, str] = {"stub_constant": "fixed"}

    def measure(
        self, *, base_src: Path, cand_src: Path, commit_sha: str, scenario: str
    ) -> Measurement:
        # A fixed -50 delta against a band of 10 (set in CalibrationParams below) so
        # the dry-run candidate is a clean keep with no guardrail regression.
        # `import` the leaf type lazily-free: it is already imported at module top via
        # contracts, so build the payload directly.
        return Measurement(
            ok=True,
            primary_delta=-50.0,
            primary_base=100.0,
            primary_cand=50.0,
            stages=StageBreakdown(stages={"stub_stage": -50.0}),
            guardrails={"stub_guardrail": 0.0},
            rh_capability_ok=True,
            rh_functional_ok=True,
            note="stub measurement",
        )

    def baseline_samples(self, *, base_src: Path, reps: int) -> list[float]:
        # A low-variance synthetic baseline so the spine computes a small 2sigma band
        # (alternating +/-1 around 100 -> a defined, tiny spread). Deterministic, no LLM.
        return [100.0 + (1.0 if i % 2 else -1.0) for i in range(max(reps, 2))]

    def measure_canary(self, *, base_src: Path) -> Measurement:
        # A large, clearly-band-clearing forced win so the canary gate passes in a dry run
        # (delta -50 vs the stub band of ~10 from CalibrationParams below).
        return Measurement(
            ok=True,
            primary_delta=-50.0,
            primary_base=100.0,
            primary_cand=50.0,
            stages=StageBreakdown(stages={"stub_stage": -50.0}),
            note="stub canary (forced known win)",
        )


class StubBuildGate:
    """A build gate that always passes (boolean only)."""

    single_environment = True  # build+measure co-located -> spine skips the sha assert

    def build_and_test(self, *, worktree: Path, src: Path) -> GateResult:
        return GateResult(passed=True, detail="stub gate (always green)")


class StubBugRunner:
    """A bug runner that drives a clean RED → GREEN → STAYGREEN in a dry run.

    The static-triage primitives all pass; the reproducing test FAILS on base (RED)
    and PASSES on the fix (GREEN); the suite stays green (STAYGREEN). Deterministic,
    no LLM — so the spine's :class:`~.bug_gate.BugGate` reports ``BUG_FILED`` and the
    dry-run bug cycle drafts a CR (M4 exit-criterion exercise)."""

    def build_imports_ok(self, *, src: Path) -> bool:
        return True

    def lint_clean(self, *, base_src: Path, cand_src: Path) -> bool:
        return True

    def test_collects(self, *, src: Path, test_path: str) -> bool:
        return True

    def run_reproducing_test(self, *, src: Path, test_id: str, test_only: bool) -> bool | None:
        # test_only==True is the RED run at BASE → FAIL; the GREEN run (full fix) → PASS.
        return False if test_only else True

    def run_suite(self, *, src: Path) -> tuple[bool, list[str]]:
        return True, []


class StubAllowlist:
    """Allows everything (the M1 'empty allowlist' stub — nothing is off-limits)."""

    allowed: list[str] = []  # empty == the M1 "empty allowlist"; allows() lets all pass
    off_limits: list[str] = []

    def allows(self, changed_paths: list[str]) -> tuple[bool, list[str]]:
        return True, []


class StubIsolation:
    """Reports push as disabled and an empty do-not-pollute path list so the dry run's
    safety preconditions pass without touching any real clone."""

    base_ref = "auto_improvement/trunk-base"
    frozen_components: list[str] = []

    def __init__(self, clone_path: Path):
        self.clone_path = Path(clone_path)

    def push_disabled(self) -> bool:
        return True

    def do_not_pollute_paths(self) -> list[Path]:
        return []

    def measurement_boot(self) -> "BootCallable":
        # No real runtime to boot in a dry run; a no-op boot touches nothing, so the
        # do-not-pollute test trivially passes (zero diff over the empty path set).
        return lambda: None


class StubPRRecipe:
    """Writes a queue marker instead of opening a review (never publishes/merges)."""

    namespace = "stub/queue"

    def __init__(self, queue_dir: Path):
        self.queue_dir = Path(queue_dir)

    def draft(
        self,
        *,
        summary: str,
        description: str,
        diff: str,
        fingerprint: str,
        parent_ref: str | None = None,
    ) -> str:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        (self.queue_dir / f"{fingerprint}.diff").write_text(diff or "")
        # `.pr.md` to match the real recipe and the display reader (routes/commit read
        # `<fp>.pr.md`); the stub's copy is never displayed, but keeping the name in step
        # avoids teaching a future maintainer the wrong convention.
        (self.queue_dir / f"{fingerprint}.pr.md").write_text(
            f"# stub CR: {summary}\n\n{description}\n"
        )
        return f"QUEUED:{fingerprint}"


class StubProfile(ProfileFieldAliases):
    """The no-op profile that drives the spine ``--dry-run`` (M1 exit artifact).

    Mixes in :class:`ProfileFieldAliases` so ``isolation_recipe`` /
    ``calibration_params`` resolve to ``isolation`` / ``calibration`` (G3b)."""

    id = "stub"
    track = TRACK_PERF

    def __init__(self, *, clone_path: Path, queue_dir: Path):
        self.ruler = StubRuler()
        self.build_gate = StubBuildGate()
        self.bug_runner = StubBugRunner()  # ②b — drives a clean RED/GREEN in --dry-run
        self.edit_allowlist = StubAllowlist()
        self.isolation = StubIsolation(clone_path)
        self.pr_recipe = StubPRRecipe(queue_dir)
        self.calibration = CalibrationParams(
            baseline_reps=5,
            noise_band=10.0,
            floor=10.0,
            canary_id="stub_canary",
            anchors=["stub_anchor"],
            drift_reanchor_cycles=5,
            heldout=[],
        )

    def discover(
        self,
        *,
        base_sha: str,
        top_k: list[dict],
        known_loci: list[str],
        agent_runner=None,
    ) -> DiscoveryResult:
        # One deterministic candidate per dry-run cycle. (Stub ignores agent_runner.)
        return DiscoveryResult(
            candidates=[
                Candidate(
                    kind=TRACK_PERF,
                    target="stub_module.py::stub_symbol",
                    signature="stub inefficiency",
                    hypothesis="stub fix",
                    evidence="stub evidence",
                    scenario="stub_scenario",
                    confidence=1.0,
                )
            ],
            notes="stub discovery",
        )

    def propose(self, *, candidate: Candidate, base_sha: str, worktree: Path, tier: str) -> bool:
        # Realize a trivial real edit to a TRACKED file so ``git diff base`` shows it
        # (an untracked marker would not appear in the diff). The dry-run base repo
        # carries ``src/<pkg>/__init__.py`` (see driver._run_dry); append a
        # deterministic line so a real diff is produced.
        for init in worktree.glob("src/*/__init__.py"):
            init.write_text(init.read_text() + f"\n# stub edit {candidate.target} ({tier})\n")
            return True
        return False
