"""Bug gate — the deterministic RED/GREEN reproducing-test gate (spine, M4).

The bug track's *verify gate* — the deterministic replacement for the perf track's
A/B measurement (05_improvement_loop_bugfix.md §2; 10_implementation_roadmap.md "M4 —
Bug track"). It is **target-agnostic**: the orchestration (the static-triage ladder,
the RED→GREEN→STAYGREEN transition, the doubled-RED flake check, the cheapest-first
fail-fast ordering) lives HERE in the spine, parameterized only by the profile's
:class:`~.profile.BugRunner` test-runner primitives. *What* command to invoke (the
target's build/test toolchain) and *what counts as a failure surface* are
profile-provided; the discipline is the spine's (M4 generalization note).

Two pieces, run cheapest-first / fail-fast (05_*.md §3.2 ladder, then §2.2 gate):

  STATIC TRIAGE LADDER (§3.2) — the bug track's cheap pre-filter, the analogue of the
  perf track's smoke build/imports pre-gate (autoloop/README.md L24):
    T0  build / imports smoke   — candidate diff builds and modules import → else
                                  BUG_FAILED_BUILD (cheapest signal first).
    T1  lint / static clean     — no NEW lint violations introduced by the diff → else
                                  BUG_FAILED_LINT (catch obviously-bad fixes pre-suite).
    T2  test collection         — the reproducing test file COLLECTS → else
                                  BUG_TEST_INVALID (a non-collecting test cannot be RED).

  RED / GREEN / STAYGREEN (§2.2) — the three boolean checks, ALL of which must hold:
    RED        the reproducing test (test portion only, applied at BASE) FAILS — run
               TWICE; both FAIL (the §2.5 flake check, the bug-track analogue of the
               perf track's independent second A/B, autoloop/README.md L26). A PASS on
               base → BUG_NOT_RED (vacuous test); a FAIL-then-PASS → BUG_TEST_FLAKY.
    GREEN      the same test, with the FULL fix applied, PASSES → else BUG_NOT_GREEN.
    STAYGREEN  the full suite (or the documented smoke subset — a Target-Profile
               choice, §8) is green under the full fix → else BUG_REGRESSED.

  ACCEPT ⟺ RED ∧ GREEN ∧ STAYGREEN  (§2.4) — purely boolean: no metric, no noise band,
  no anchors, no canary (§2.4, §6.1). The agent proposes; Python decides (§2.1).

This module shells out to NOTHING target-specific — it calls only the
:class:`~.profile.BugRunner` primitives. No target token appears here.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .contracts import (
    BUG_ERROR,
    BUG_FAILED_BUILD,
    BUG_FAILED_LINT,
    BUG_FILED,
    BUG_NOT_GREEN,
    BUG_NOT_RED,
    BUG_REGRESSED,
    BUG_TEST_FLAKY,
    BUG_TEST_INVALID,
    BugGateResult,
    Candidate,
)
from .profile import BugRunner

# Module logger — the bug gate is where bug fixes most often die (test_invalid / not_red /
# not_green); logging every verdict + the stage it failed at makes runs analyzable and
# pinpoints the effectiveness bottleneck (operator goal: useful + analyzable). Prefix "bug_gate:".
_log = logging.getLogger("auto_improvement.spine.bug_gate")


class BugGate:
    """The deterministic RED/GREEN gate, parameterized by a profile's BugRunner.

    A single :meth:`run` per candidate returns a :class:`BugGateResult` whose
    ``passed`` is True iff the static-triage ladder cleared AND RED ∧ GREEN ∧
    STAYGREEN held. The granular ``reason`` (a ``BUG_*`` constant) lets the driver
    map onto the shared ledger's outcome vocabulary (05_*.md §5.3).
    """

    def __init__(self, *, red_reps: int = 2) -> None:
        # RED is confirmed TWICE (the §2.5 doubled-RED flake check). A test that is
        # FAIL then PASS (or errors intermittently) is rejected as flaky — this is
        # the bug-track analogue of the perf track's independent second A/B.
        self.red_reps = max(2, red_reps)

    # ── static-triage ladder (§3.2): cheapest-first, fail-fast ───────────────

    def _triage(
        self, *, runner: BugRunner, base_src: Path, cand_src: Path, test_path: str
    ) -> BugGateResult | None:
        """Run T0 (build/imports) → T1 (lint) → T2 (collect) on the candidate diff.

        Returns a *failing* :class:`BugGateResult` on the first gate that fails (so a
        candidate that fails T0 never pays for T1, etc.), or ``None`` if all three
        pass (proceed to RED/GREEN). Ordering matters — it keeps cycle wall-clock low
        so the budget caps buy more candidates (§3.2)."""
        # T0 — build / imports smoke: the candidate diff builds and modules import.
        if not runner.build_imports_ok(src=cand_src):
            return BugGateResult(
                passed=False,
                reason=BUG_FAILED_BUILD,
                build_ok=False,
                detail="T0: candidate fix does not build / import",
            )
        # T1 — lint / static clean: no NEW violation introduced by the diff. The
        # "what is a new violation" baseline is the BASE src; the profile diffs.
        if not runner.lint_clean(base_src=base_src, cand_src=cand_src):
            return BugGateResult(
                passed=False,
                reason=BUG_FAILED_LINT,
                build_ok=True,
                lint_ok=False,
                detail="T1: candidate fix introduces a new lint/static violation",
            )
        # T2 — test collection: the reproducing test file collects (no import error).
        if not runner.test_collects(src=cand_src, test_path=test_path):
            return BugGateResult(
                passed=False,
                reason=BUG_TEST_INVALID,
                build_ok=True,
                lint_ok=True,
                collected=False,
                detail="T2: reproducing test does not collect (cannot be RED)",
            )
        return None  # all three triage gates clear → proceed to RED/GREEN

    # ── RED → GREEN → STAYGREEN (§2.2) ───────────────────────────────────────

    def run(
        self, *, runner: BugRunner, candidate: Candidate, base_src: Path, cand_src: Path
    ) -> BugGateResult:
        """Run the full bug gate for one candidate, logging the verdict.

        Thin wrapper over :meth:`_run_inner` that logs the granular verdict (the stage
        the candidate cleared/failed: triage T0/T1/T2 → RED → GREEN → STAYGREEN) so a run
        can be analyzed from logs alone — bug fixes most often die here and the reason is
        the key effectiveness signal."""
        tgt = getattr(candidate, "target", None) or "?"
        res = self._run_inner(
            runner=runner, candidate=candidate, base_src=base_src, cand_src=cand_src
        )
        _log.info(
            "bug_gate: %s reason=%s | target=%s build_ok=%s lint_ok=%s collected=%s detail=%s",
            "PASS" if getattr(res, "passed", False) else "FAIL",
            getattr(res, "reason", "?"),
            tgt,
            getattr(res, "build_ok", None),
            getattr(res, "lint_ok", None),
            getattr(res, "collected", None),
            (getattr(res, "detail", "") or "")[:120],
        )
        return res

    def _run_inner(
        self, *, runner: BugRunner, candidate: Candidate, base_src: Path, cand_src: Path
    ) -> BugGateResult:
        """Run the full bug gate for one candidate.

        ``base_src`` is the unmodified base tree; ``cand_src`` is the tree with the
        full fix (test + source) applied (the spine prepares both worktrees before
        calling — see :mod:`.gate`). The reproducing test's id/path come from the
        candidate's :class:`~.contracts.BugReproducingTest`.
        """
        rt = candidate.reproducing_test
        if rt is None or not rt.test_id:
            # A bug candidate with no reproducing test cannot be RED/GREEN'd — this is
            # a malformed candidate, not a code defect; record it as test_invalid.
            return BugGateResult(
                passed=False,
                reason=BUG_TEST_INVALID,
                detail="bug candidate carries no reproducing test",
            )

        try:
            # ── static triage ladder (§3.2), fail-fast ──────────────────────
            triage_fail = self._triage(
                runner=runner,
                base_src=base_src,
                cand_src=cand_src,
                test_path=rt.test_path or rt.test_id,
            )
            if triage_fail is not None:
                return triage_fail

            # ── RED (§2.2 step 1 + §2.5 flake check) ────────────────────────
            # Apply ONLY the test portion of the diff at BASE and run JUST the
            # reproducing test. It MUST be FAIL, run TWICE (both FAIL). A PASS means
            # the test does not reproduce the bug (vacuous → BUG_NOT_RED); a
            # FAIL-then-PASS means it is flaky (→ BUG_TEST_FLAKY).
            base_results = [
                runner.run_reproducing_test(src=base_src, test_id=rt.test_id, test_only=True)
                for _ in range(self.red_reps)
            ]
            if all(r is True for r in base_results):
                # the reproducing test PASSED on base on every rep → vacuous test.
                return BugGateResult(
                    passed=False,
                    reason=BUG_NOT_RED,
                    build_ok=True,
                    lint_ok=True,
                    collected=True,
                    detail="RED check failed: reproducing test PASSES on base (vacuous)",
                )
            if any(r is True for r in base_results):
                # FAIL on one rep, PASS on another → flaky; RED→GREEN would be noisy.
                return BugGateResult(
                    passed=False,
                    reason=BUG_TEST_FLAKY,
                    build_ok=True,
                    lint_ok=True,
                    collected=True,
                    detail=f"RED flake check failed: base results not all-FAIL ({base_results})",
                )
            # all reps are FAIL (None == error is treated as not-a-clean-FAIL above,
            # but a clean repeated FAIL is `False`); confirm none ERRORED.
            if any(r is None for r in base_results):
                return BugGateResult(
                    passed=False,
                    reason=BUG_TEST_INVALID,
                    build_ok=True,
                    lint_ok=True,
                    collected=True,
                    detail="RED check errored on base (collection/import error, not an assertion)",
                )
            # RED confirmed (all reps FAIL, none errored).

            # ── GREEN (§2.2 step 2) ─────────────────────────────────────────
            # Apply the FULL fix diff (test + source) and run JUST the reproducing
            # test. It MUST be PASS.
            green = runner.run_reproducing_test(src=cand_src, test_id=rt.test_id, test_only=False)
            if green is not True:
                return BugGateResult(
                    passed=False,
                    reason=BUG_NOT_GREEN,
                    red=True,
                    build_ok=True,
                    lint_ok=True,
                    collected=True,
                    detail="GREEN check failed: fix did NOT turn the reproducing test green",
                )

            # ── STAYGREEN (§2.2 step 3) ─────────────────────────────────────
            # Run the full suite (or documented smoke subset — Profile choice) with
            # the full fix applied. Any PREVIOUSLY-PASSING test now failing →
            # the fix regressed something → discard.
            suite_ok, failing = runner.run_suite(src=cand_src)
            if not suite_ok:
                # BASE-RELATIVE regression check: a test that ALSO fails on the base tree
                # is a PRE-EXISTING failure (slow real-LLM/sandbox-fork integration test,
                # flaky test, already-broken test), NOT a regression THIS fix caused.
                # Only failures NEW under the fix are real regressions. Without this, an
                # unrelated pre-existing timeout sinks a correct fix (observed live: the
                # team_manager async integration tests time out at 120s on base too).
                # The check is cost-bounded — we re-run ONLY the failing subset on base.
                real_regressions = list(failing or [])
                # A not-green suite with NO parseable failing nodeid (an empty list, or
                # only the "<unparsed-suite-failure>" sentinel) cannot be pinned to a
                # pre-existing base failure — the base re-check below has nothing to
                # re-run, so we could never prove the failure was pre-existing. Treat it
                # conservatively as a regression: a suite that is NOT green must never
                # fall through to ACCEPT as STAYGREEN. (The real backend runner's
                # non-pytest build path returns (False, []) whenever the build/test
                # command fails but no output line says 'FAILED'/'ERROR' — e.g. a
                # mypy/compile error or a crash with different wording.)
                parseable = [t for t in real_regressions if t != "<unparsed-suite-failure>"]
                if not parseable:
                    return BugGateResult(
                        passed=False,
                        reason=BUG_REGRESSED,
                        red=True,
                        green=True,
                        staygreen=False,
                        build_ok=True,
                        lint_ok=True,
                        collected=True,
                        failing_tests=list(failing or []),
                        detail="STAYGREEN check failed: suite is NOT green but reported "
                        "no identifiable failing test (build/compile/crash failure)",
                    )
                check = getattr(runner, "run_named_tests", None)
                if callable(check) and failing and "<unparsed-suite-failure>" not in failing:
                    try:
                        base_failures = check(src=base_src, test_ids=list(failing))
                        real_regressions = [t for t in failing if t not in base_failures]
                    except (
                        Exception
                    ):  # noqa: BLE001 — if the base re-check errors, stay conservative
                        real_regressions = list(failing)
                if real_regressions:
                    return BugGateResult(
                        passed=False,
                        reason=BUG_REGRESSED,
                        red=True,
                        green=True,
                        staygreen=False,
                        build_ok=True,
                        lint_ok=True,
                        collected=True,
                        failing_tests=real_regressions,
                        detail="STAYGREEN check failed: the fix regressed "
                        f"{len(real_regressions)} test(s) not failing on base",
                    )
                # All suite failures were pre-existing on base → the fix regressed
                # nothing. STAYGREEN holds.

            # ACCEPT ⟺ RED ∧ GREEN ∧ STAYGREEN (§2.4).
            return BugGateResult(
                passed=True,
                reason=BUG_FILED,
                red=True,
                green=True,
                staygreen=True,
                build_ok=True,
                lint_ok=True,
                collected=True,
                detail="RED (x2) → GREEN → STAYGREEN (suite green)",
            )
        except Exception as e:  # noqa: BLE001 — a harness/tooling error is BUG_ERROR (§5.3)
            return BugGateResult(
                passed=False,
                reason=BUG_ERROR,
                detail=f"bug gate error: {type(e).__name__}: {e}",
            )
