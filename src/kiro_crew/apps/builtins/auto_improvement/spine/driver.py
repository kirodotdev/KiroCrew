"""Driver — the external durable while-loop owning git + archive state (spine).

The outer layer of the two-layer architecture (02_architecture.md §1, §6.1; 10_roadmap
M0 "driver — the external while-loop owning durable state (git branch + results/
archive), budget caps, and the quiescence stop"). A plain Python while-loop that is
cron/tmux-restartable and survives Claude restarts, because its ONLY durable state is:

  - the git working branch in the separate push-disabled clone (current best == HEAD), and
  - the ``results/`` archive on disk (the whole candidate population + run metadata).

Per cycle it (02_arch §1 diagram, §6.1):
  1. reads branch HEAD + the top-K archive (evolutionary memory),
  2. invokes ONE per-cycle workflow: discover (A) → propose (B) → gate (C) →
     measure (D) → keep/revert (E),
  3. applies the verdict — commit-on-keep (local only) / leave-on-discard — and
     appends a results row + dedups via the ledger,
  4. drafts a CR on a kept, reproduced win,
  5. loops until budget (``--max-cycles`` / ``--max-hours`` / ``--max-cost``) or
     quiescence (M consecutive cycles with no keep).

SAFETY (M0 exit criterion; 08_safety §1.3): the driver REFUSES TO START unless the
target clone's push is disabled. CRs are draft-only; nothing is published/merged.

The driver is fully target-agnostic: it sees the target only through the loaded
:class:`~.contracts.TargetProfile`. ``--dry-run`` exercises the whole pipeline with a
stub profile (mirrors ``autoloop.py --dry-run``).
"""

from __future__ import annotations

import argparse
import importlib as _importlib
import logging
import os
import subprocess  # noqa: F401
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror
from kiro_crew.platform.context import redact_log_via_context, redact_via_context  # noqa: F401
from kiro_crew.subprocess_utf8 import UTF8_TEXT  # noqa: F401

from . import driver_commit as _driver_commit
from . import driver_publish as _driver_publish
from . import ledger as L
from . import pr_description as D  # noqa: F401
from . import preflight as PF
from .archive import Archive
from .contracts import TRACK_BUG, TRACK_PERF, BugGateResult, Proposal, TargetProfile
from .driver_git import _git
from .gate import Gate
from .git_safety import GIT_SAFE_CONFIG, hook_off_args, require_pinned  # noqa: F401
from .keeper import KEPT, Keeper
from .measurer import Measurer
from .pr_pipeline import CrPipeline
from .preflight import PreflightResult
from .proposer import Proposer
from .push_policy import normalize_branch  # noqa: F401


@dataclass
class BudgetCaps:
    """The clean-stop budget (10_roadmap M0; 08_safety §7). Any cap, or quiescence,
    or a stop signal, ends the run cleanly."""

    max_cycles: int = 1000
    max_hours: float = 10.0
    max_cost_usd: float = 50.0
    quiesce_after: int = 3  # consecutive cycles with no keep (M) → mined out, stop
    cycle_gap_s: float = 0.0  # gentle spacing so a transient failure doesn't hot-spin
    # Optional fan-out overrides — caps takes precedence over env defaults so a caller
    # (e.g. a "validate one CR end-to-end" run) can keep the loop to a single candidate.
    proposer_wide: int | None = None
    proposer_deep: int | None = None
    # Optional measurement-thoroughness overrides (the user-facing "how many times we
    # re-measure each change" knob; UI: measureReps). The A/B VERIFY + REPRODUCE reps are
    # the slowest part of a perf cycle (each is a full project boot). Fewer reps = a faster
    # run that's still reliable when the win is large vs the noise band; more = tighter
    # confidence. None → the Measurer's env override / research-grade default (6 verify,
    # 8 reproduce). caps takes precedence over env so an API/UI value wins.
    measure_reps: int | None = None
    reproduce_reps: int | None = None
    # Optional noise-band CAP (ms): when set (>0), the calibrated band is capped at this
    # value (never below the floor). On a noisy shared host the 2σ term can balloon so wide
    # that even a real known win can't clear it and nothing is ever kept/filed; capping lets
    # a genuine above-cap win register. WEAKENS the anti-noise gate — off by default (None),
    # for a bounded demo/validation run only. UI/config: bandCapMs. (An env-var path exists
    # in calibration/preflight too, but the measurement sandbox scrubs AUTO_IMPROVEMENT_*
    # env, so config→caps is the path that actually reaches the spine.)
    band_cap_ms: float | None = None


@dataclass
class Stats:
    cycles: int = 0
    discovered: int = 0
    deduped: int = 0
    gated_out: int = 0
    not_kept: int = 0
    kept: int = 0
    filed: int = 0
    errors: int = 0
    cost_usd: float = 0.0


class PushEnabledError(RuntimeError):
    """Raised at boot if the clone's push is NOT disabled (do-not-leak invariant,
    08_safety §1.3). The driver refuses to start."""


class Driver:
    """The durable improvement loop. Wires proposer/gate/measurer/keeper/ledger
    around the loaded profile."""

    def __init__(
        self,
        *,
        profile: TargetProfile,
        clone: Path,
        branch: str,
        archive_root: Path,
        ledger_path: Path,
        pr_queue_dir: Path,
        worktree_root: Path,
        caps: BudgetCaps | None = None,
        guardrail_tolerances: dict[str, float] | None = None,
        cost_meter=None,
        boot_callable=None,
        on_progress=None,
        agent_runner=None,
        logger: logging.Logger | None = None,
        retry_cooldown_s: float | None = None,
        canary_advisory: bool = False,
        direct_commit: bool = False,
        prepush_review: bool = False,
    ):
        self.profile = profile
        # F10 DIRECT-COMMIT MODE (ROADMAP F10; operator opt-in per project). When True, a
        # VERIFIED winner is pushed straight to the operator-authorized feature branch
        # instead of filed as a CR. This deliberately relaxes the push-disabled invariant
        # (§4.11) into a narrow, consented shape — but ONLY for a non-protected branch
        # (push_policy.authorize_direct_push is the spine-side, non-overridable gate; a
        # protected/shared branch always falls back to the CR path). Default False = the
        # safe draft-CR path. The authorization is re-checked at push time, never assumed.
        self.direct_commit = bool(direct_commit)
        # F6/F10: require a clean automated reviewer review BEFORE a direct push — an
        # auto-pushed commit gets no human review, so the automated reviewer is its gate
        # (fail-closed: an unavailable/uncertain review BLOCKS the push). Only meaningful
        # with direct_commit on. Default False (no gate) to keep CR-path behavior intact.
        self.prepush_review = bool(prepush_review)
        # When True, a preflight canary that does NOT clear the band WARNS and the run
        # PROCEEDS (instead of RulerNotTrustedError halting Phase 2). Mirrors the backend's
        # advisory calibrate() policy (canaryStrict=false): a noisy band on a short
        # calibration shouldn't block the run — the keeper still gates every real win on
        # the band. The do-not-pollute gate stays HARD. Default False = strict (§7.1).
        self.canary_advisory = canary_advisory
        # OPTIONAL headless agent runner (claude -p) for authoring bug fixes (the one
        # intelligent step). Threaded into the Proposer. None = offline spine (no
        # fabricated fixes; bug candidates without a mechanical seed are skipped).
        self._agent_runner = agent_runner
        # Optional live-progress sink (M7 UI): callable(dict) -> None invoked at each
        # stage boundary (discover/propose/gate/measure/keep/draft_cr) and per-cycle so
        # the dashboard's status poll reflects the loop in real time instead of sitting
        # at cycle 0 until the whole run finishes. Opaque to the spine; the backend
        # runner wires it to update its RunState. No-op default keeps the spine usable
        # headless (CLI/tmux) with zero behavioural change.
        self._on_progress = on_progress if callable(on_progress) else (lambda _e: None)
        self.clone = Path(clone)
        self.branch = branch
        self.archive = Archive(archive_root)
        # Soft-terminal (error / no_defect) loci become retryable after this cooldown so
        # a transient miss never permanently poisons the ledger. None → ledger default.
        self.ledger = (
            L.Ledger(ledger_path, retry_cooldown_s=retry_cooldown_s)
            if retry_cooldown_s is not None
            else L.Ledger(ledger_path)
        )
        self.pr_queue_dir = Path(pr_queue_dir)
        self.caps = caps or BudgetCaps()
        self.guardrail_tolerances = guardrail_tolerances or {}
        # The cost SOURCE the --max-cost budget reads each cycle (04_*.md §5.1; 08_safety
        # §7). A plain Callable[[], float] returning the cumulative USD spend — kept
        # target-agnostic (no model/provider/price named here). The agent-runner injects a
        # real source (e.g. a :class:`.cost.CostMeter` it ``add()``s per candidate, or a
        # tokens×rate accumulator); the default is a safe ``0.0`` that never trips the cap,
        # so wiring a real meter is purely additive. The check at run() fires when
        # ``cost_meter() > caps.max_cost_usd``.
        # Default the cost source to the agent runner's accumulated spend when one is
        # wired (so --max-cost is live over a long run, as in the original framework);
        # otherwise a safe 0.0 that never trips the cap.
        if cost_meter is not None:
            self.cost_meter = cost_meter
        elif agent_runner is not None and hasattr(agent_runner, "total_cost_usd"):
            self.cost_meter = agent_runner.total_cost_usd
        else:
            self.cost_meter = lambda: 0.0
        # The measurement-runtime BOOT callable the Phase-1 do-not-pollute test drives
        # (boot the runtime once + tear down; the spine measures the host-state delta it
        # leaves; 08_safety §2.2; preflight §7.3). Profile/driver-supplied + opaque to the
        # spine. When the caller injects one (e.g. a unit test with a fake boot) it is used
        # verbatim. When NOT injected, preflight() sources the REAL boot from the profile's
        # isolation recipe (``isolation.measurement_boot()``) so a real --go run actually
        # boots the measurement gateway around the snapshot/diff (M7d), not a no-op. The
        # fallback default stays a no-op boot (touches nothing -> zero diff) so the
        # do-not-pollute path is unit-testable even with a profile that has no live boot.
        self._explicit_boot = boot_callable is not None
        self.boot_callable = boot_callable or (lambda: None)
        self.preflight_result: PreflightResult | None = None
        self.log = logger or logging.getLogger("auto_improvement.driver")

        # Fan-out shape (wide cheap + deep strong) — defaults match the original framework.
        # Caps overrides let a caller (the backend runner from API caps, an operator from
        # an env var) run a single-candidate cycle end-to-end without spawning N parallel
        # expensive agent calls; useful for first-CR validation runs.

        _wide = (
            self.caps.proposer_wide
            if self.caps.proposer_wide is not None
            else int(os.environ.get("AUTO_IMPROVEMENT_WIDE", "6"))
        )
        _deep = (
            self.caps.proposer_deep
            if self.caps.proposer_deep is not None
            else int(os.environ.get("AUTO_IMPROVEMENT_DEEP", "1"))
        )
        self.proposer = Proposer(
            clone=self.clone,
            worktree_root=worktree_root,
            agent_runner=self._agent_runner,
            wide=_wide,
            deep=_deep,
        )
        self.gate = Gate()
        #: Sha actually published by the last `_direct_push` — set from the clone AFTER
        #: the push, because a rebase-and-retry rewrites HEAD and the pre-push snapshot
        #: would name a commit that never reached the remote. Read by the ledger.
        self.pushed_sha = ""
        # Measurement thoroughness: caps (from the UI/API "measureReps") wins; else the
        # Measurer falls back to its env override / research-grade default. Only pass kwargs
        # that are set so an unspecified knob keeps the Measurer's own default logic.
        _meas_kw: dict[str, int] = {}
        if self.caps.measure_reps is not None:
            _meas_kw["reps"] = max(2, int(self.caps.measure_reps))
        if self.caps.reproduce_reps is not None:
            _meas_kw["reproduce_reps"] = max(2, int(self.caps.reproduce_reps))
        self.measurer = Measurer(base_src=self.clone / "src", **_meas_kw)
        self.keeper = Keeper()
        # M5: the verify → REPRODUCE → draft-CR → ledger boundary (06_*.md §1.3).
        # The driver runs the per-cycle workflow + keep decision; the pipeline turns a
        # kept/reproduced finding into a draft CR and records the dedup outcome.
        self.pr_pipeline = CrPipeline(
            ledger=self.ledger,
            measurer=self.measurer,
            guardrail_tolerances=self.guardrail_tolerances,
            logger=self.log,
            direct_commit=self.direct_commit,
            retire_if_unsafe=self._retire_if_unsafe,
        )
        self._stop = False
        self._repository_retired = False
        #: Set when a provisional rollback FAILED. HEAD then still carries a commit that
        #: was refused and never published, and the next winner commits ON TOP of it: that
        #: winner's scan reads only its own revision (`<rev>~1..<rev>`) while its push sends the
        #: whole ancestry, so the refused content is published by a scanner that never saw it.
        #: Nothing local can repair that -- the rollback is what was supposed to -- so this
        #: latches and publishing refuses from here on. This flag covers ONE PROCESS; the clone
        #: is also QUARANTINED on disk (`_quarantine_unrolled_clone`), because the refused commit
        #: outlives the run and the clone is reused. Raised by the GPT review of this branch.
        self._rollback_failed = False
        # Terminal latch for a probe whose sandbox launcher crashed: set (then
        # re-raised) by `_retire_if_unsafe`. Some intermediate layers catch
        # broadly to keep a run alive (per-candidate error containment), so
        # `run()` re-raises this before returning stats — otherwise a run
        # aborted by a safety-probe failure would be recorded as STATUS_DONE.
        # Raised by the GPT review of this branch.
        self._probe_failure: Exception | None = None

    def _retire_if_unsafe(self, stage: str) -> bool:
        """Stop and atomically retire the clone if post-agent validation fails."""
        from ..backend.clone_setup import (
            IsolationProbeError,
            _repository_is_isolated,
            _retire_unsafe_clone,
        )

        try:
            isolated = _repository_is_isolated(self.clone)
        except IsolationProbeError as exc:
            # The probe could not RUN — its sandbox launcher died before git
            # executed, which says nothing about the clone. Do NOT retire:
            # retiring renames away a clone whose remotes were never read,
            # destroying good state over an unrelated sandbox failure.
            # The tight signature match in `_launcher_failure_detail` is
            # what keeps this branch unreachable for ambiguous or
            # repository-influenced errors — those return False below and
            # retire. Re-raise after recording: swallowing here would let
            # `driver.run()` return normally, so the supervisor would record
            # STATUS_DONE for a run aborted by a safety-probe failure; the
            # run-loop's catch-all records STATUS_ERROR with this message
            # instead.
            self._stop = True
            self._probe_failure = exc
            self.log.error("isolation probe could not run after %s: %s", stage, exc)
            self._progress(stage="isolation_probe_failed", error=str(exc))
            raise
        if isolated:
            return False
        retained = _retire_unsafe_clone(self.clone)
        self._repository_retired = True
        self._stop = True
        if retained is None:
            self.log.error(
                "repository safety changed after %s; run stopped and clone left unsafe in place",
                stage,
            )
        else:
            self.log.error(
                "repository safety changed after %s; run stopped and clone retained at %s",
                stage,
                retained,
            )
        self._progress(
            stage="repository_unsafe",
            error="repository safety changed after agent-controlled execution",
            retained_clone=str(retained or ""),
        )
        return True

    # ── boot-time safety preconditions (M0 exit criterion) ──────────────

    def assert_push_disabled(self) -> None:
        """Refuse to start unless the clone's push is mechanically disabled
        (08_safety §1.3) — OR F10 direct-commit is authorized for a non-protected branch.
        Delegates the *how* to the profile's isolation recipe; the *policy* is the spine's.

        F10 relaxation (ROADMAP F10): the clone's ``origin`` push URL stays
        ``DISABLED_NO_PUSH`` even in direct-commit mode (so ``push_disabled()`` is still
        True and this passes the normal way) — the direct push targets the real remote
        explicitly for the ONE authorized branch (see :meth:`_direct_push`). So the only
        case this needs to additionally allow is a clone whose push is somehow live AND a
        valid direct-commit authorization; a protected/blank branch is refused by
        :func:`.push_policy.authorize_direct_push` regardless. We fail CLOSED: any
        ambiguity → the original refusal stands. Both probes below propagate
        ``clone_setup.IsolationProbeError`` when their sandbox launcher crashed
        before git executed — a stricter refusal (the run still does not start),
        never a relaxation, surfacing the sandbox failure instead of a
        misleading isolation verdict."""
        from ..backend.clone_setup import _repository_is_safe

        if not _repository_is_safe(self.clone):
            raise PushEnabledError(
                f"SAFETY: repository metadata for clone {self.clone} failed validation — "
                "refusing to start"
            )
        if self.profile.isolation.push_disabled():
            return
        if self.direct_commit:
            from .push_policy import authorize_direct_push

            ok, reason = authorize_direct_push(direct_commit=True, branch=self.branch)
            if ok:
                self.log.warning(
                    "SAFETY: clone push is not disabled, but direct-commit is authorized "
                    "for %r (%s) — proceeding under the scoped push exception",
                    self.branch,
                    reason,
                )
                return
        raise PushEnabledError(
            f"SAFETY: push for clone {self.clone} is not disabled — refusing to start"
        )

    def head_sha(self) -> str:
        """Current best == branch HEAD (02_arch §3.2, §4.2 step 1)."""
        return _git(["rev-parse", "HEAD"], self.clone).stdout.strip()

    # ── Phase-1 pre-flight: the trust gate before the Phase-2 loop ──────────────

    def preflight(self) -> PreflightResult:
        """Run the Phase-1 pre-flight BEFORE the Phase-2 loop and HALT if the ruler is
        not proven (03_metric §0/§11; the whole point of Phase 1).

        Orchestrates the three gates via :func:`.preflight.calibrate_and_prove`:
          1. CALIBRATE the noise band (≈``baseline_reps`` baseline samples -> 2σ band),
          2. force the CANARY — a known/forced win must clear the band, else this RAISES
             :class:`~.preflight.RulerNotTrustedError` ("ruler not trusted — refusing
             Phase 2"; §7.1),
          3. run the DO-NOT-POLLUTE acceptance test — a non-zero host diff RAISES
             :class:`~.preflight.HostPollutionError` and BLOCKS the run (§7.3; 08_safety
             §2.2).
        Only if all three pass does it return a :class:`PreflightResult` (and the caller —
        :meth:`run` for a real run — enters the Phase-2 loop). The baseline tree is the
        current branch HEAD source; the boot callable is the driver's ``boot_callable``.
        It is a standalone method so the pre-flight path is unit-testable with fakes.

        The boot callable the do-not-pollute test drives is resolved here (M7d, 08_safety
        §2.2 step 2/3): when the caller injected an explicit ``boot_callable`` (a fake boot
        in a unit test), it is used verbatim. Otherwise the spine asks the profile's
        isolation recipe for the REAL measurement boot (``isolation.measurement_boot()``),
        so a real run snapshots the host paths, BOOTS THE MEASUREMENT GATEWAY ONCE, and
        re-diffs — refusing Phase 2 on any non-zero host diff. The boot is opaque to the
        spine (the profile owns HOW to boot + tear down); the spine owns the snapshot/diff/
        block machinery (:mod:`.pollute`)."""
        self.log.info("preflight: proving the ruler before Phase 2 (03_metric §0/§11)…")
        boot = self.boot_callable if self._explicit_boot else self._resolve_measurement_boot()
        # Duck-wire a stop_check onto the ruler so a clean-stop request can interrupt
        # the ~30-boot calibration loop between reps (without it a Stop click waits out
        # the entire preflight — the "stuck in phase2_perf" symptom). The
        # ruler treats it as optional; partial samples surface as CalibrationError.
        try:
            self.profile.ruler.stop_check = lambda: self._stop  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — a frozen/slotted ruler just runs to completion
            pass
        res = PF.calibrate_and_prove(
            self.profile,
            base_src=self.clone / "src",
            boot=boot,
            logger=self.log,
            canary_advisory=self.canary_advisory,
            band_cap_ms=self.caps.band_cap_ms,
        )
        self.preflight_result = res
        # Tell the PR pipeline whether the ruler was PROVEN, now that preflight knows. The
        # pipeline is constructed in __init__, before this runs, so it cannot be a
        # constructor argument. Only matters in advisory mode: strict mode raises above
        # rather than reaching here, so a surviving run there always cleared the canary.
        self.pr_pipeline.ruler_proven = bool(res.canary_cleared)
        # Adopt the (possibly capped) calibrated band into the profile so the KEEPER gates
        # each candidate on the SAME band the canary was judged against. Without this the
        # keeper reads profile.calibration.noise_band (initial 0.0 / stale) and the cap never
        # reaches the per-candidate accept test. dataclasses are frozen → setattr defensively.
        try:
            object.__setattr__(self.profile.calibration, "noise_band", res.noise_band)
        except (
            Exception
        ):  # noqa: BLE001 — best-effort; measurement still carries res band via the ruler
            try:
                self.profile.calibration.noise_band = res.noise_band  # type: ignore[misc]
            except Exception:  # noqa: BLE001
                self.log.debug("could not adopt calibrated band into profile", exc_info=True)
        # Adopt the ruler's DERIVED guardrail tolerances (absolute allowances computed
        # from the just-calibrated baseline medians — e.g. response ≤ +5% of base,
        # boot ≤ max(+10% of base, 2σ)). Without this the keeper's default tolerance
        # is 0 for every guardrail, and a ruler reporting any positive regression-
        # magnitude — even within normal jitter — rejects every candidate. Explicit
        # caller-provided tolerances still win (setdefault).
        tol_fn = getattr(self.profile.ruler, "guardrail_tolerances", None)
        if callable(tol_fn):
            try:
                for name, allowed in (tol_fn() or {}).items():
                    self.guardrail_tolerances.setdefault(name, float(allowed))
                if self.guardrail_tolerances:
                    self.log.info("guardrail tolerances: %s", self.guardrail_tolerances)
            except Exception:  # noqa: BLE001 — tolerances are an enhancement, never a halt
                self.log.debug("ruler guardrail_tolerances failed", exc_info=True)
        self.log.info("preflight PASSED: %s", res.note)
        return res

    def _resolve_measurement_boot(self):
        """Source the do-not-pollute boot callable from the profile's isolation recipe
        (M7d; 08_safety §2.2). When the recipe exposes ``measurement_boot`` (the M7d seam),
        the spine drives THAT — a real boot of the measurement gateway once + teardown — so
        the snapshot/diff brackets a real boot. A recipe without it (an older fake in a
        test, or a profile with no live runtime) falls back to the driver's default
        ``boot_callable`` (a no-op), which still yields a true zero-diff over the path set.
        The spine never inspects what the boot does; it only measures the host-state delta
        the boot leaves around :meth:`do_not_pollute_paths`."""
        recipe = self.profile.isolation
        boot_factory = getattr(recipe, "measurement_boot", None)
        if callable(boot_factory):
            boot = boot_factory()
            if callable(boot):
                return boot
        return self.boot_callable

    # ── one per-cycle workflow (Phases A–E) ─────────────────────────────

    def _progress(self, **fields) -> None:
        """Push a live-progress event to the UI sink (no-op headless). Best-effort:
        a sink that raises must never break the loop."""
        try:
            self._on_progress(fields)
        except Exception:  # noqa: BLE001
            self.log.debug("on_progress sink failed", exc_info=True)

    def _fan_out_checked(self, *, fresh_candidates: list, base_sha: str, cycle: int) -> list | None:
        """Run proposer agents, then validate before re-raising any post-agent error."""
        proposals: list = []
        error: Exception | None = None
        try:
            proposals = self.proposer.fan_out(
                profile=self.profile,
                candidates=fresh_candidates,
                base_sha=base_sha,
                cycle=cycle,
                stop_check=lambda: self._stop,
            )
        except Exception as exc:  # noqa: BLE001 - validate before interpreting
            error = exc
        if self._retire_if_unsafe("proposal"):
            return None
        if error is not None:
            raise error
        return proposals

    def _work_one_proposal_checked(
        self,
        prop: Proposal,
        *,
        base_sha: str,
        cycle: int,
        proposals: list,
        perf_survivors: list,
        bug_winners: list,
        gated_sha: dict,
    ) -> bool:
        """Run one candidate and validate before fallible error bookkeeping."""
        error: Exception | None = None
        try:
            self._work_one_proposal(
                prop,
                base_sha=base_sha,
                cycle=cycle,
                proposals=proposals,
                perf_survivors=perf_survivors,
                bug_winners=bug_winners,
                gated_sha=gated_sha,
            )
        except Exception as exc:  # noqa: BLE001 - one candidate must not kill the run
            error = exc
        if self._retire_if_unsafe("candidate gate/measure"):
            return False
        if error is not None:
            self.log.error(
                "cycle %d: candidate %s errored: %s: %s",
                cycle,
                prop.cand_id,
                type(error).__name__,
                error,
            )
            self.stats.errors += 1
            self._record(prop, L.STATUS_ERROR, f"{type(error).__name__}: {error}")
        return True

    def run_cycle(self, cycle: int) -> int:
        """Run one Profile→Propose→Gate→Measure→Keep pass. Returns the number of
        FRESH (not-yet-seen) candidates this cycle (drives quiescence)."""
        base_sha = self.head_sha()
        top_k = self.archive.top_k()
        known = sorted(
            {L.fingerprint(kind=e.kind, target=e.target) for e in self.ledger._seen.values()}
        )
        # SKIP-LIST for agent discovery: the human-readable targets of loci already terminal
        # in the ledger, so the discovery agent doesn't waste its read budget re-proposing
        # surfaces that will be deduped downstream (operator: discovery re-emits already-
        # terminal candidates every cycle = wasted LLM cost). Set as a profile attribute
        # (read by the backend profile's agent-discovery call) to avoid churning every
        # profile's discover() signature; profiles that ignore it keep prior behavior.
        try:
            track = getattr(self.profile, "track", TRACK_PERF)
            self.profile._skip_targets = self.ledger.terminal_targets(  # type: ignore[attr-defined]
                kind=track if track == TRACK_BUG else None
            )
            # The cycle index rotates agent-discovery's focus ordering WITHIN each value tier
            # so a per-cycle read budget samples a different slice of the FULL changed-file
            # surface each cycle (operator directive: do not limit the search space
            # — rotate coverage across all files instead of capping to the same top-N).
            self.profile._discovery_rotate = cycle  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — skip-list is a cost optimization, never fatal
            pass

        # Phase A — discover. Hand the profile the run's agent runner so a track that
        # supports agent-driven discovery (the bug track) can use the model as a
        # first-class discovery source; a profile that ignores it keeps prior behavior.
        self._progress(cycle=cycle, stage="profile")
        disc = self.profile.discover(
            base_sha=base_sha,
            top_k=top_k,
            known_loci=known,
            agent_runner=self._agent_runner,
        )
        if self._retire_if_unsafe("discovery"):
            return 0
        self.stats.discovered += len(disc.candidates)
        fresh_candidates = []
        for cand in disc.candidates:
            fp = L.fingerprint(kind=cand.kind, target=cand.target)
            if self.ledger.known(fp):
                self.stats.deduped += 1
                self.log.debug("skip (already %s): %s", self.ledger.status_of(fp), cand.target)
                continue
            fresh_candidates.append(cand)
        self.log.info(
            "cycle %d: %d candidate(s), %d fresh",
            cycle,
            len(disc.candidates),
            len(fresh_candidates),
        )
        self._progress(
            cycle=cycle,
            stage="propose",
            discovered=len(disc.candidates),
            fresh=len(fresh_candidates),
        )
        if not fresh_candidates:
            self._progress(cycle=cycle, stage="", fresh=0)
            return 0

        # mark fresh candidates as seen before working them (crash-safe dedup).
        for cand in fresh_candidates:
            fp = L.fingerprint(kind=cand.kind, target=cand.target)
            self.ledger.record(
                L.LedgerEntry(fp=fp, kind=cand.kind, target=cand.target, status=L.STATUS_SEEN)
            )

        # Phase B — propose (fan-out wide + deep; each in its own worktree).
        # The stop_check lets a clean-stop request abort the fan-out mid-loop, so we
        # don't keep spawning expensive agent subprocesses after the user clicked Stop.
        proposals = self._fan_out_checked(
            fresh_candidates=fresh_candidates,
            base_sha=base_sha,
            cycle=cycle,
        )
        if proposals is None:
            return 0
        self._progress(
            cycle=cycle,
            stage="gate",
            proposers={"fanned": len(proposals), "survived_gate": 0, "measuring": 0},
        )

        # Survivors that reach Phase D (perf, with a measurement). Bug candidates do
        # NOT measure — their RED/GREEN gate IS the verdict (05_*.md §2; §3.3 "the test
        # transition IS the objective"), so they are handled inline and never enter the
        # A/B/noise-band keeper path.
        perf_survivors: list[tuple[Proposal, object, object]] = []
        bug_winners: list[tuple[Proposal, BugGateResult]] = []
        # Phase-C gated sha per survivor cand_id — the REPRODUCE A/B (M5) must re-measure
        # the SAME gated artifact VERIFY measured (the same-sha contract, 02_arch §2.2).
        gated_sha: dict[str, str] = {}
        try:
            for prop in proposals:
                if self._stop:
                    break
                if not self._work_one_proposal_checked(
                    prop,
                    base_sha=base_sha,
                    cycle=cycle,
                    proposals=proposals,
                    perf_survivors=perf_survivors,
                    bug_winners=bug_winners,
                    gated_sha=gated_sha,
                ):
                    return 0

            # Phase E — perf keep / revert (one decision; archive all perf survivors).
            self._progress(cycle=cycle, stage="keep")
            verdict, archived = self.keeper.decide(
                survivors=perf_survivors,  # type: ignore[arg-type]  # tuples are (Proposal, GateResult, Measurement) at runtime
                guardrail_tolerances=self.guardrail_tolerances,
                # The RULER owns which way is better; the keeper must not assume.
                # Without this the noise-band comparison is hardcoded to minimize, so a
                # ``maximize`` metric (throughput, hit rate) is judged with an INVERTED
                # test: a real win reads as noise and a regression reads as a win. The
                # keeper defaults to "minimize", so a profile that omits a direction
                # behaves exactly as before.
                direction=self._metric_direction(),
            )

            kept_count = self._apply_verdict(
                cycle, base_sha, verdict, archived, len(fresh_candidates), gated_sha
            )
            # Each accepted bug fix on a DISTINCT locus is its own keep — file a draft CR
            # per locus (a bug cycle can accept multiple independent fixes; the perf
            # keeper instead picks ONE). Two winners on the SAME fingerprint (e.g. the
            # wide + deep proposer both fixing one surface) are the SAME finding — file
            # the first, record the rest as ``duplicate`` so no duplicate CR is filed
            # (05_*.md §5.1 dedup invariant, §5.3 ``duplicate`` outcome).
            filed_fps: set[str] = set()
            for prop, bug_res in bug_winners:
                fp = L.fingerprint(kind=prop.candidate.kind, target=prop.candidate.target)
                if fp in filed_fps:
                    # A second winner on a locus already filed THIS cycle (e.g. the wide
                    # + deep proposer both fixed one surface) is the same finding — skip
                    # it so no duplicate CR is filed. We do NOT overwrite the ``filed``
                    # ledger row with ``duplicate`` (the file is the authoritative
                    # outcome); the dup is just dropped. The CR-pipeline's own ledger-side
                    # dedup guard covers the CROSS-RESTART case (a fp already terminal on
                    # disk); this set covers the SAME-CYCLE double-keep where the fp is
                    # still only ``seen``. (05_*.md §5.1/§5.3; 06_*.md §1.3/§2.4.)
                    self.stats.deduped += 1
                    self.log.debug("bug dedup: %s already filed this cycle", prop.candidate.target)
                    continue
                filed_fps.add(fp)
                self._apply_bug_winner(cycle, prop, bug_res)
            return kept_count
        finally:
            if not self._repository_retired:
                for prop in proposals:
                    self.proposer.teardown(prop)
            else:
                self.log.warning("proposal teardown skipped because repository safety failed")

    def _work_one_proposal(
        self,
        prop: Proposal,
        *,
        base_sha: str,
        cycle: int,
        proposals: list,
        perf_survivors: list,
        bug_winners: list,
        gated_sha: dict,
    ) -> None:
        """Run ONE proposal through its track's gate/measure path, appending to
        ``perf_survivors`` / ``bug_winners`` / ``gated_sha``. Split out of
        :meth:`run_cycle` so the caller can isolate per-candidate exceptions —
        an exception here is recorded as ``error`` and the loop continues."""
        if prop.skipped:
            # Record an explicit terminal status for the locus so it does not stay
            # at STATUS_SEEN forever. The proposer tells us WHY it skipped via
            # ``skip_status``: a real exception → ``error`` (counts toward stats.errors,
            # short retry cooldown); an honest no-diff investigation → ``no_defect``
            # (does NOT inflate the error stat). Both are SOFT-terminal in the ledger,
            # so the surface becomes retryable after a cooldown instead of being
            # permanently poisoned (the bug: speculative seeds recorded ``error`` idled
            # the loop forever at "0 fresh").
            status = prop.skip_status or L.STATUS_NO_DEFECT
            self.log.debug("proposal %s skipped (%s): %s", prop.cand_id, status, prop.skip_reason)
            if status == L.STATUS_ERROR:
                self.stats.errors += 1
            self._record(prop, status, prop.skip_reason or "no diff produced")
            return

        if prop.candidate.kind == TRACK_BUG:
            # ── BUG TRACK — deterministic RED/GREEN, no A/B (M4) ─────────
            bug_res = self.gate.run_bug(profile=self.profile, proposal=prop, base_sha=base_sha)
            if bug_res.passed:
                # RED ∧ GREEN ∧ STAYGREEN held — accept (the gate is the verdict).
                bug_winners.append((prop, bug_res))
            else:
                # Map the granular BUG_* reason onto the shared ledger status
                # (failed_gate / failed_verify / error) — never discarded_noise
                # (the bug track has no noise band; 05_*.md §5.3).
                status = L.map_bug_reason_to_status(bug_res.reason)
                if status == L.STATUS_FAILED_GATE:
                    self.stats.gated_out += 1
                else:
                    self.stats.not_kept += 1
                self._record(prop, status, f"{bug_res.reason}: {bug_res.detail}")
            return

        # ── PERF TRACK — Phase C gate → Phase D measure ─────────────────
        gate_res = self.gate.run(profile=self.profile, proposal=prop, base_sha=base_sha)
        if not gate_res.passed:
            self.stats.gated_out += 1
            self._record(prop, L.STATUS_FAILED_GATE, gate_res.detail)
            return
        # Phase D — measure (STRICTLY SERIAL, one survivor at a time) — VERIFY.
        gated_sha[prop.cand_id] = gate_res.commit_sha
        self._progress(
            cycle=cycle,
            stage="measure",
            proposers={
                "fanned": len(proposals),
                "survived_gate": len(perf_survivors) + 1,
                "measuring": 1,
            },
        )
        meas = self.measurer.measure(
            profile=self.profile,
            proposal=prop,
            gated_commit_sha=gate_res.commit_sha,
        )
        # Capture a PROFILE for this candidate, if the profile offers one. Deliberately
        # AFTER measure() and never inside a timed arm: a profiler's instrumentation
        # overhead is exactly the variance the noise band exists to exclude, so
        # profiling a measured run would corrupt the number it is meant to explain.
        # Optional by getattr — a profile with no profiler is unaffected, and a capture
        # failure must never lose a measured candidate.
        self._capture_profile(prop)
        perf_survivors.append((prop, gate_res, meas))

    def _capture_profile(self, proposal: Proposal) -> None:
        """Best-effort per-candidate profile capture (feeds ``GET /profile/{fp}``).

        The normalizer and both endpoints already existed but nothing ever CALLED a
        capture, so the profiler views were permanently empty — the app shipped a
        flame/icicle surface with no data path. This is that missing call.

        Fully optional and non-fatal: the profile must expose ``capture_profile(fp,
        worktree)``; anything else (absent hook, raise, None) leaves the run untouched.
        """
        hook = getattr(self.profile, "capture_profile", None)
        if not callable(hook):
            return
        cand = proposal.candidate
        try:
            fp = L.fingerprint(kind=cand.kind, target=cand.target)
            out = hook(fp=fp, worktree=proposal.worktree)
            if out:
                self.log.info("captured profile for %s (%s)", cand.target, fp[:12])
        except Exception:  # noqa: BLE001 — observability must never fail a run
            self.log.debug("profile capture failed for %s", cand.target, exc_info=True)

    #: Attempts for a direct push: the first try, then one rebase-and-retry.
    _PUSH_ATTEMPTS = 2

    # Publish a verified winner to its authorized branch (F10): ``spine/driver_publish.py``.
    #: The object the last push sent, recorded by ``_push_with_rebase`` for ``_direct_push``.
    _pushed_object: str
    _reverify_head = _driver_publish._reverify_head
    _revision_scans_clean = _driver_publish._revision_scans_clean
    _restore_branch = _driver_publish._restore_branch
    _push_with_rebase = _driver_publish._push_with_rebase
    _prepush_review_clean = _driver_publish._prepush_review_clean
    _build_test_pre_push_clean = _driver_publish._build_test_pre_push_clean
    _head_is_the_committed_sha = _driver_publish._head_is_the_committed_sha
    _direct_push = _driver_publish._direct_push

    def _metric_direction(self) -> str:
        """The improving direction of the profile's PRIMARY metric.

        Read off the ruler (``profile.ruler.direction``) because the ruler defines the
        metric, and normalized to the two values the keeper understands. Anything
        unrecognized or absent falls back to ``"minimize"`` — the historical behavior
        and the safe default: it can only make the band test STRICTER for a maximize
        metric, never wrongly accept a regression.
        """
        raw = getattr(getattr(self.profile, "ruler", None), "direction", "") or ""
        return "maximize" if str(raw).strip().lower() == "maximize" else "minimize"

    def _record(self, proposal: Proposal, status: str, note: str) -> None:
        cand = proposal.candidate
        fp = L.fingerprint(kind=cand.kind, target=cand.target)
        self.ledger.record(
            L.LedgerEntry(fp=fp, kind=cand.kind, target=cand.target, status=status, note=note[:200])
        )

    @staticmethod
    def _metric_blob(meas) -> dict:
        """The structured per-candidate metric object the archive row carries (so the UI
        data-store recovers the absolute primary value + stages + guardrails + secondary
        metrics from the archive, never recomputing — data_store.read_progress §0.1). It
        is target-agnostic: the spine names no metric, it only forwards what the ruler
        measured (primary_*, the stage/guardrail/secondary dicts, and the RH booleans)."""
        return {
            "primary_delta": meas.primary_delta,
            "primary_base": meas.primary_base,
            "primary_cand": meas.primary_cand,
            "noise_band": meas.noise_band,
            "stages": dict(meas.stages.stages),
            "guardrails": dict(meas.guardrails),
            "secondary": dict(meas.secondary),
            "rh_capability_ok": meas.rh_capability_ok,
            "rh_functional_ok": meas.rh_functional_ok,
        }

    def _apply_verdict(self, cycle, base_sha, verdict, archived, fresh_count, gated_sha) -> int:
        if self._publishing_is_halted():
            return 0
        # Archive ALL survivors (the whole population is evolutionary memory). The kept
        # winner's diff_ref is reused as the CR's ``diff-ref`` (06_*.md §3.1/§3.2).
        winner_diff_ref = ""
        for prop, status, meas in archived:
            diff_ref = self.archive.save_candidate(
                cand_id=prop.cand_id,
                diff=prop.diff,
                detail={"proposal": prop, "status": status, "measurement": meas},
            )
            if verdict.winner is not None and prop.cand_id == verdict.winner.cand_id:
                winner_diff_ref = diff_ref
            self.archive.append_row(
                {
                    "cycle": cycle,
                    "cand_id": prop.cand_id,
                    "commit": "-",
                    "status": status,
                    "tests_pass": True,
                    "reps": self.measurer.reps,
                    "primary_delta": (meas.primary_delta if meas else ""),
                    "noise_band": (meas.noise_band if meas else ""),
                    "description": prop.description,
                    "diff_ref": diff_ref,
                    # The structured metric blob (primary_cand/base + stages + guardrails +
                    # the NON-BLOCKING secondary metrics) so the data-store reader recovers
                    # the per-candidate absolute value + secondary columns from the archive
                    # (data_store.read_progress reads row["metric"]["primary_cand"]); the
                    # ``note`` string stays available under "note" for the greppable TSV.
                    "metric": (self._metric_blob(meas) if meas else ""),
                    "note": (meas.note if meas else ""),
                    # Flattened secondary metrics (rss/cpu/throughput) so they land as their
                    # own greppable results.tsv columns beyond the control set (METRICS.md §6).
                    "secondary": (dict(meas.secondary) if meas else {}),
                }
            )
            if status != KEPT:
                # Map the keeper's real discard reason to the correct ledger status — NOT a
                # blanket ``discarded_noise``. Only a delta inside the band is noise; a
                # guardrail/tests/RH failure is a verification failure (failed_verify) and a
                # measurement error is retryable (error). Hard-coding discarded_noise here
                # mislabeled the row AND permanently dedup-blocked a transient RH-probe miss.
                self._record(prop, L.map_perf_discard_to_status(status), status)

        if not verdict.keep or verdict.winner is None:
            self.stats.not_kept += 1
            self.log.info("cycle %d: no keep (%s)", cycle, verdict.reason)
            return fresh_count

        winner = verdict.winner
        self.stats.kept += 1
        self.log.info(
            "cycle %d: KEPT %s (%s) — running REPRODUCE", cycle, winner.cand_id, verdict.reason
        )

        # M5 PIPELINE: VERIFY (the keeper, above) → REPRODUCE (second independent A/B) →
        # DRAFT CR → ledger (06_*.md §1.3). The CR pipeline owns the reproduce + draft +
        # record boundary; the driver owns the commit-on-keep. Only a delta that survives
        # the SECOND independent A/B becomes a CR — a first-run fluke is recorded as
        # ``failed_verify`` and does NOT advance the branch (06_*.md §1.1).
        # COMMIT the winner into the shared clone BEFORE drafting. Staging alone is not
        # enough: ``git push HEAD:refs/heads/<b>`` sends the COMMIT that HEAD points at, and
        # `git apply` + `git add -A` only touch the index — verified against a local bare
        # repo, where a staged-but-uncommitted fix pushed the ORIGINAL file content. So the
        # fix has to be a commit, not just staged. Raised by review of this branch after a
        # first attempt that only staged.
        #
        # The commit message needs ``outcome.reproduce``, which only the pipeline produces,
        # so this lands a PLACEHOLDER message and `_finalize_winner_commit` amends it with
        # the real numbers once the pipeline returns — and resets the branch if nothing was
        # filed, so a fluke never advances HEAD (06_*.md §1.1).
        pre_sha = _git(["rev-parse", "HEAD"], self.clone).stdout.strip()
        if not self._commit_winner_provisional(winner):
            self.ledger.record(
                L.LedgerEntry(
                    fp=L.fingerprint(
                        kind=winner.candidate.kind,
                        target=winner.candidate.target,
                        signature=winner.candidate.signature or "",
                    ),
                    kind=winner.candidate.kind,
                    target=winner.candidate.target,
                    status=L.STATUS_ERROR,
                    note="winner diff did not apply to the working branch",
                )
            )
            return fresh_count

        outcome = self.pr_pipeline.emit_perf(
            profile=self.profile,
            winner=winner,
            verify=verdict.measurement,
            cycle=cycle,
            gated_commit_sha=gated_sha.get(winner.cand_id, ""),
            diff_ref=winner_diff_ref,
            base_anchor=f"{self.branch} @ {base_sha[:12]}",
        )
        if outcome.repository_retired:
            self.stats.kept -= 1
            return fresh_count
        if outcome.filed or outcome.committed_ready:
            # AMEND the provisional commit with the §2.4 attributable message, derived from
            # the SAME measured numbers as the CR (§3.2 end). The pipeline's INDEPENDENT
            # reproduce measurement (outcome.reproduce) is what makes the commit message and
            # the CR agree (06_*.md §3.1/§3.2; CrOutcome.reproduce) — it does not exist until
            # the pipeline has run, which is why the commit is amended rather than authored
            # here.
            committed = self._finalize_winner_commit(
                winner,
                verify=verdict.measurement,
                reproduce=outcome.reproduce,
                cycle=cycle,
                diff_ref=winner_diff_ref,
            )
            if outcome.committed_ready:
                # F10 direct-commit: push the verified commit to the authorized branch and
                # record ``committed`` with the real sha (only on a successful push — a
                # refused/failed push already recorded ``error`` and nothing left the sandbox).
                pushed = self._direct_push(
                    fp=outcome.fp, kind="perf", target=winner.candidate.target, sha=committed
                )
                if pushed is True:
                    # `pushed_sha`, not `committed`: a rebase-and-retry inside the push
                    # rewrites HEAD, and recording the pre-rebase sha would point the
                    # ledger at a commit that is not in the remote's history.
                    landed = self.pushed_sha or committed
                    self.ledger.record(
                        L.LedgerEntry(
                            fp=outcome.fp,
                            kind="perf",
                            target=winner.candidate.target,
                            status=L.STATUS_COMMITTED,
                            cr=landed,
                            note=f"direct-pushed to {self.branch} ({landed})"[:200],
                        )
                    )
                    self.stats.filed += 1
                    self.log.info(
                        "cycle %d: COMMITTED %s → %s (%s)",
                        cycle,
                        winner.cand_id,
                        self.branch,
                        landed,
                    )
                elif pushed is False:
                    # ROLL BACK the refused commit. Leaving it at HEAD is a credential LEAK,
                    # not just untidy bookkeeping: the direct-push scan range is
                    # `HEAD~1..HEAD` (one commit), so the NEXT winner's scan does not see this
                    # commit while its push publishes both. Measured on a real repo: candidate
                    # A refused for a planted `AKIAIOSFODNN7EXAMPLE`, candidate B's scan range
                    # showed the credential = False while its pushed range showed it = True.
                    # Raised by the GPT review of this branch.
                    self._reset_provisional(pre_sha)
                    self.stats.kept -= 1  # push refused/failed → not a realized outcome
                else:
                    # The clone was atomically retired; any Git rollback would trust
                    # metadata that just failed validation.
                    self.stats.kept -= 1
                return fresh_count
            self.stats.filed += 1
            self.log.info(
                "cycle %d: FILED %s cr=%s commit=%s", cycle, winner.cand_id, outcome.cr, committed
            )
            # Announce the filed CR so the app can start a watcher session that keeps it
            # mergeable + drives it to passing-all-checks (tasks #21/#24). Opaque to the
            # spine — the backend's on_progress sink decides what to do with it.
            self._progress(
                cr_filed={
                    "fp": outcome.fp,
                    "cr": outcome.cr,
                    "kind": "perf",
                    "target": winner.candidate.target,
                    "title": getattr(winner, "description", ""),
                    "base_ref": getattr(self.profile.isolation, "base_ref", ""),
                    "branch": getattr(winner, "branch", ""),
                }
            )
            # DELIBERATELY NOT reset here, unlike the bug track's filed path.
            #
            # Review asked for `_reset_provisional(pre_sha)` after this progress event, because
            # a filed perf winner stays on the local branch and a LATER cycle's PR therefore
            # carries it (measured: pushing whole HEAD for PR#2 included cycle 1's `FIX_1`).
            # The observation is correct, but the remedy would break the perf track's premise:
            # this loop is EVOLUTIONARY — "current best == HEAD" is its documented durable state
            # (see the module docstring), `base_sha = self.head_sha()` is re-read every cycle,
            # and every measurement is reported as "Δ vs current best". Resetting would make
            # each cycle re-measure against the ORIGINAL base, so a second improvement to the
            # same hot path could never be seen as an improvement at all.
            #
            # The bug track has no such property (independent loci, one PR each), which is why
            # resetting there was right and resetting here is not the same change.
            #
            # A per-winner branch rebuilt from the remote base would satisfy both goals in
            # principle. Measured: it is not a safe drop-in — two cycles improving the SAME
            # line produce a patch that does not apply to the untouched base, and the rebuild
            # silently yielded a branch containing NEITHER fix. Doing it properly needs a
            # cherry-pick with conflict handling and a decision about what to publish when the
            # replay fails, which is a design change rather than a bug fix.
            #
            # Recorded as a known limitation instead (see the module spec). It is also latent
            # rather than live: the perf track has never kept a measured win on a real
            # repository, so no perf PR has been filed for a second cycle to contaminate.
            # Raised by the GPT review of this branch.
        else:
            # Not reproduced / duplicate / draft error → do NOT advance the branch; the
            # ledger already carries the terminal outcome (the pipeline recorded it).
            # Roll the PROVISIONAL commit back: it exists only so the draft could push a
            # HEAD containing the fix, and a non-win must leave the branch where it was.
            self._reset_provisional(pre_sha)
            self.stats.kept -= 1  # the "keep" did not become a real, reproduced win
            self.log.info("cycle %d: %s NOT filed (%s)", cycle, winner.cand_id, outcome.status)
        return fresh_count

    # Commit a kept winner, amend it or roll it back: ``spine/driver_commit.py``.
    _publishing_is_halted = _driver_commit._publishing_is_halted
    _redact_commit_message = staticmethod(_driver_commit._redact_commit_message)
    _discard_staged = _driver_commit._discard_staged
    _stage_winner = _driver_commit._stage_winner
    _commit_winner_provisional = _driver_commit._commit_winner_provisional
    _reset_provisional = _driver_commit._reset_provisional
    _quarantine_unrolled_clone = _driver_commit._quarantine_unrolled_clone
    _finalize_winner_commit = _driver_commit._finalize_winner_commit
    _stage_bug_winner = _driver_commit._stage_bug_winner
    _commit_bug_winner_provisional = _driver_commit._commit_bug_winner_provisional
    _finalize_bug_winner_commit = _driver_commit._finalize_bug_winner_commit

    # ── bug-track keep/draft (M4; 05_improvement_loop_bugfix.md §4) ───────────

    def _apply_bug_winner(self, cycle: int, winner: Proposal, bug_res: BugGateResult) -> None:
        """Accept one bug fix that passed RED ∧ GREEN ∧ STAYGREEN: archive it,
        commit-on-keep locally, draft a DRAFT-only CR with the correctness narrative,
        and record ``filed`` in the shared ledger (05_*.md §4.2; 02_arch §3.2).

        This is the bug-track analogue of :meth:`_apply_verdict`'s keep path, but the
        CR trigger is the boolean RED/GREEN gate (the doubled-RED flake check is the
        reproduction analogue, §4.1) — there is no second A/B and no noise band."""
        if self._publishing_is_halted():
            return
        diff_ref = self.archive.save_candidate(
            cand_id=winner.cand_id,
            diff=winner.diff,
            detail={"proposal": winner, "status": KEPT, "bug_gate": bug_res},
        )
        self.archive.append_row(
            {
                "cycle": cycle,
                "cand_id": winner.cand_id,
                "commit": "-",
                "status": KEPT,
                "tests_pass": True,
                "reps": 0,  # no A/B reps for a bug fix (deterministic boolean gate)
                "primary_delta": "",  # bug track has no measured delta (§6.1)
                "noise_band": "",
                "description": winner.description,
                "diff_ref": diff_ref,
                "metric": f"RED→GREEN→STAYGREEN ({bug_res.reason})",
            }
        )
        # M5 PIPELINE (bug track): the RED/GREEN gate already verified+reproduced (the
        # doubled-RED flake check IS the reproduce analogue, 06_*.md §1.1) — so emit the
        # draft CR with the RED→GREEN correctness narrative (§4.2) via the same pipeline
        # boundary. The pipeline dedups (defense-in-depth), authors the description, files
        # the draft, and records the terminal ledger row.
        # COMMIT first — same reason as the perf track: the recipe pushes this clone's
        # HEAD, and HEAD is a COMMIT pointer, so a merely-staged fix is invisible to the
        # draft. Provisional message; amended once the pipeline returns, reset if nothing
        # was filed.
        pre_sha = _git(["rev-parse", "HEAD"], self.clone).stdout.strip()
        if not self._commit_bug_winner_provisional(winner):
            self.ledger.record(
                L.LedgerEntry(
                    fp=L.fingerprint(
                        kind=winner.candidate.kind,
                        target=winner.candidate.target,
                        signature=winner.candidate.signature or "",
                    ),
                    kind=winner.candidate.kind,
                    target=winner.candidate.target,
                    status=L.STATUS_ERROR,
                    note="bug fix diff did not apply to the working branch",
                )
            )
            return

        outcome = self.pr_pipeline.emit_bug(
            profile=self.profile,
            winner=winner,
            bug_res=bug_res,
            cycle=cycle,
            diff_ref=diff_ref,
            # `pre_sha`, NOT `head_sha()`: the provisional fix commit above has already
            # advanced HEAD, so `head_sha()` here is the FIX commit. The base anchor is the
            # durable "tested against" provenance a reviewer reads, so recording the fix as its
            # own base is self-referential nonsense. `pre_sha` is the HEAD captured before the
            # commit — the revision the RED→GREEN gate actually ran against. The perf twin
            # already anchors on its own `base_sha` for the same reason. Raised by the GPT review.
            base_anchor=f"{self.branch} @ {pre_sha[:12]}",
        )
        if outcome.filed or outcome.committed_ready:
            committed = self._finalize_bug_winner_commit(
                winner, bug_res=bug_res, cycle=cycle, diff_ref=diff_ref
            )
            if outcome.committed_ready:
                # F10 direct-commit (bug track): push the verified RED→GREEN fix to the
                # authorized branch; record ``committed`` only on a successful push.
                pushed = self._direct_push(
                    fp=outcome.fp, kind="bug", target=winner.candidate.target, sha=committed
                )
                if pushed is True:
                    # See the perf track: record the sha that LANDED, not the pre-rebase one.
                    landed = self.pushed_sha or committed
                    self.ledger.record(
                        L.LedgerEntry(
                            fp=outcome.fp,
                            kind="bug",
                            target=winner.candidate.target,
                            status=L.STATUS_COMMITTED,
                            cr=landed,
                            note=f"direct-pushed bug fix to {self.branch} ({landed})"[:200],
                        )
                    )
                    self.stats.kept += 1
                    self.stats.filed += 1
                    self.log.info(
                        "cycle %d: BUG FIX COMMITTED %s → %s (%s)",
                        cycle,
                        winner.cand_id,
                        self.branch,
                        landed,
                    )
                elif pushed is False:
                    # Same rollback as the perf twin, and for the same reason: a refused
                    # commit left at HEAD is invisible to the NEXT winner's `HEAD~1..HEAD`
                    # scan but still published by its push. This branch had no `else` at
                    # all — it fell straight through to `return` with the commit intact.
                    # Roll back the provisional commit (as the perf twin does) so a refused
                    # push leaves nothing at HEAD for the next winner's range to inherit. Do
                    # NOT decrement `kept`: unlike the perf path, which increments `kept`
                    # EAGERLY on keep (before the push) and so must reverse it on failure, the
                    # bug path only increments `kept` inside the SUCCESS arm above (`+= 1` after
                    # a landed push). Decrementing here subtracts from a counter this path never
                    # added to, driving `stats.kept` negative or undercounted in the `/run`
                    # result. Raised by the GPT review.
                    self._reset_provisional(pre_sha)
                return  # _apply_bug_winner returns None (no fresh_count in this scope)
            self.stats.kept += 1
            self.stats.filed += 1
            self.log.info(
                "cycle %d: BUG FIX FILED %s cr=%s commit=%s",
                cycle,
                winner.cand_id,
                outcome.cr,
                committed,
            )
            # Announce the filed bug CR (tasks #21/#24) — the backend starts a watcher.
            self._progress(
                cr_filed={
                    "fp": outcome.fp,
                    "cr": outcome.cr,
                    "kind": "bug",
                    "target": winner.candidate.target,
                    "title": getattr(winner.candidate, "signature", "")
                    or getattr(winner, "description", ""),
                    "base_ref": getattr(self.profile.isolation, "base_ref", ""),
                    "branch": getattr(winner, "branch", ""),
                }
            )
            # Roll back HERE TOO, after a SUCCESSFUL file. A bug cycle can accept several
            # independent fixes and files one draft PR per locus, all from this ONE shared
            # clone — so leaving a filed winner's commit at HEAD makes the NEXT winner's
            # branch start from it, and its PR then carries the earlier, unrelated fix.
            # Measured on a real repo: winner B's `base...HEAD` range contained `FIX_A` as
            # well as `FIX_B`. The provisional commit exists ONLY so the draft push had a
            # HEAD containing this fix; that push has already happened and the work is safe
            # on its own generated branch, so HEAD must return to where this winner found it.
            #
            # Review suggested capping the cycle at ONE bug winner instead. That discards
            # verified, reproduced work for a bookkeeping problem — each fix is on a distinct
            # locus and has passed RED x2 -> GREEN -> STAYGREEN independently. Resetting keeps
            # every winner AND keeps each PR to its own change.
            # Raised by the GPT review of this branch.
            self._reset_provisional(pre_sha)
        else:
            # Roll the PROVISIONAL commit back — it exists only so the draft could push a
            # HEAD containing the fix, and a not-filed candidate must leave HEAD where it
            # was (06_*.md §1.1). flake8 caught this path being unwired.
            self._reset_provisional(pre_sha)
            self.log.info(
                "cycle %d: bug fix %s NOT filed (%s)", cycle, winner.cand_id, outcome.status
            )

    def _preflight_checked(self) -> PreflightResult | None:
        """Run perf preflight, then attest/retire before any later host Git."""
        result: PreflightResult | None = None
        error: Exception | None = None
        try:
            result = self.preflight()
        except Exception as exc:  # noqa: BLE001 - attest before interpreting
            error = exc
        if self._retire_if_unsafe("perf preflight"):
            return None
        if error is not None:
            raise error
        assert result is not None
        return result

    # ── the durable loop ────────────────────────────────────────────────

    def run(self, *, dry_run: bool = False, preflight: bool | None = None) -> Stats:
        self.stats = Stats()
        self.assert_push_disabled()

        # Phase-1 PRE-FLIGHT trust gate (03_metric §0/§11): a real (non-dry-run) run must
        # PROVE the ruler before entering the Phase-2 loop — calibrate the band, the canary
        # must clear it, and the do-not-pollute test must be zero-diff; any failure HALTS
        # the run (RulerNotTrustedError / HostPollutionError / CalibrationError propagate).
        # ``--dry-run`` keeps its fast path (stub profile, no pre-flight); ``preflight`` is
        # an explicit override so the pre-flight branch is unit-testable with fakes
        # (preflight=True forces it on a dry run; preflight=False skips it). Default: run
        # the pre-flight iff this is a real run.
        run_preflight = (not dry_run) if preflight is None else preflight
        # The bug track has NO noise band — its RED→GREEN regression gate IS the verdict
        # (05_*.md §2/§3.3; mirrors the original framework's bug mode, which skipped
        # perf calibration entirely). Calibrating a 2σ band + forcing a canary would be
        # both meaningless and a long, blocking boot loop before the bug loop could even
        # start. So skip the Phase-1 ruler pre-flight for the bug track; the perf tracks
        # still prove the ruler before entering the loop.
        if run_preflight and getattr(self.profile, "track", TRACK_PERF) == TRACK_BUG:
            self.log.info(
                "preflight: skipped for bug track (RED→GREEN gate is the verdict; no noise band)"
            )
            run_preflight = False
        if run_preflight:
            res = self._preflight_checked()
            if res is None:
                return self.stats
            # Surface the MEASURED calibration results (the band, the baseline rep
            # count, the canary's observed delta, the per-guardrail baseline medians)
            # to the progress sink so the UI's measurement battery can show real
            # numbers — not just the metric names (doc 12 §2 "what was measured").
            gb_fn = getattr(self.profile.ruler, "guardrail_baselines", None)
            self._progress(
                preflight={
                    "noise_band": res.noise_band,
                    "baseline_n": res.baseline_n,
                    "canary_delta": res.canary_delta,
                    "guardrail_baselines": (gb_fn() or {}) if callable(gb_fn) else {},
                }
            )

        self.archive.write_meta(
            {
                "profile_id": self.profile.id,
                "track": self.profile.track,
                "branch": self.branch,
                "base_sha": self.head_sha() if (self.clone / ".git").exists() else "",
                "noise_band": self.profile.calibration.noise_band,
                "canary_id": self.profile.calibration.canary_id,
            }
        )
        self.log.info(
            "ledger: %s (filed so far: %s)", self.ledger.counts(), self.ledger.filed_crs()
        )

        t0 = time.monotonic()
        # resume: recompute the cycle index from the archive (not held in memory).
        start_cycle = self.archive.cycle_count() + 1
        no_keep_streak = 0
        try:
            cycle = start_cycle
            while not self._stop and self.stats.cycles < self.caps.max_cycles:
                hours_used = (time.monotonic() - t0) / 3600.0
                if hours_used > self.caps.max_hours:
                    self.log.info("time budget reached")
                    break
                self.stats.cost_usd = self.cost_meter()
                if self.stats.cost_usd > self.caps.max_cost_usd:
                    self.log.info("cost budget reached ($%.2f)", self.stats.cost_usd)
                    break

                self.stats.cycles += 1
                kept_before = self.stats.kept
                self.run_cycle(cycle)
                cycle += 1

                if dry_run:
                    break  # one cycle exercises the whole pipeline

                # Quiescence = M CONSECUTIVE cycles with no keep (10_roadmap M0/M5).
                # Use a PER-CYCLE keep flag (kept this cycle?) rather than the
                # cumulative self.stats.kept counter, so an early keep does not
                # permanently suppress quiescence for the rest of the run. A cycle
                # that found nothing fresh to work (fresh == 0) is also a no-keep
                # cycle and counts toward the streak (but only once).
                kept_this_cycle = self.stats.kept > kept_before
                no_keep_streak = 0 if kept_this_cycle else no_keep_streak + 1
                # Live budget/quiescence for the UI: time spent against the cap, cycles
                # done, cost so far, and the no-keep streak. Without this the dashboard's
                # "Time used" / "Dry cycles" cards stay frozen at 0 for the whole run.
                self._progress(
                    cycle=cycle - 1,
                    budget={
                        "hours_used": round((time.monotonic() - t0) / 3600.0, 2),
                        "max_hours": self.caps.max_hours,
                        "cycles_used": self.stats.cycles,
                        "max_cycles": self.caps.max_cycles,
                        "cost_usd": round(self.stats.cost_usd, 2),
                    },
                    quiescence={
                        "cyclesSinceKeep": no_keep_streak,
                        "stopAt": self.caps.quiesce_after,
                    },
                )
                # A non-positive quiesce_after means "never quiesce" (only the
                # cycle/time/cost budgets stop the run) — without this guard a
                # quiesce_after of 0 makes ``no_keep_streak >= 0`` true after the
                # very first cycle and silently kills the loop after one cycle.
                if self.caps.quiesce_after > 0 and no_keep_streak >= self.caps.quiesce_after:
                    self.log.info(
                        "quiescence: %d cycles no keep — stopping", self.caps.quiesce_after
                    )
                    break
                if self.caps.cycle_gap_s:
                    time.sleep(self.caps.cycle_gap_s)
        finally:
            self.log.info(
                "run summary: cycles=%d discovered=%d deduped=%d gated_out=%d "
                "not_kept=%d kept=%d filed=%d errors=%d",
                self.stats.cycles,
                self.stats.discovered,
                self.stats.deduped,
                self.stats.gated_out,
                self.stats.not_kept,
                self.stats.kept,
                self.stats.filed,
                self.stats.errors,
            )
        if self._probe_failure is not None:
            # A safety probe that could not run aborted this run; per-candidate
            # error containment may have swallowed the in-flight raise, so
            # re-raise here where the supervisor's catch-all records
            # STATUS_ERROR instead of reading the early stop as DONE.
            raise self._probe_failure
        return self.stats

    def request_stop(self) -> None:
        """Ctrl-C / SIGTERM handler hook: finish the current candidate, then exit."""
        self._stop = True


# ── CLI entry point (mirrors autoloop.py --dry-run / loop.py --self-test) ────


def _build_logger() -> logging.Logger:
    log = logging.getLogger("auto_improvement.driver")
    if not log.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
    return log


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="auto-improvement spine driver (target-agnostic)")
    ap.add_argument(
        "--go", action="store_true", help="run for real (requires a configured profile)"
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="exercise the full pipeline with a stub profile"
    )
    ap.add_argument("--max-cycles", type=int, default=1000)
    ap.add_argument("--max-hours", type=float, default=10.0)
    ap.add_argument("--max-cost", type=float, default=50.0, help="USD budget ceiling (hard stop)")
    ap.add_argument("--quiesce", type=int, default=3, help="stop after N cycles with no keep")
    ap.add_argument("--clone", type=Path, help="path to the push-disabled target clone")
    ap.add_argument("--branch", default="auto_improvement/trunk-base")
    ap.add_argument(
        "--data-dir",
        type=Path,
        default=Path("/tmp/auto_improvement_run"),
        help="where the archive/ledger/pr_queue live",
    )
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    log = _build_logger()
    caps = BudgetCaps(
        max_cycles=args.max_cycles,
        max_hours=args.max_hours,
        max_cost_usd=args.max_cost,
        quiesce_after=args.quiesce,
    )

    if not (args.go or args.dry_run):
        print("\n[auto-improvement] DRY PLAN — pass --dry-run (stub) or --go (real profile).")
        print(f"  caps: {caps.max_cycles} cycles / {caps.max_hours}h / ${caps.max_cost_usd}")
        print("  each verified, reproduced win -> DRAFT (unpublished) CR; dedup via the ledger.")
        return 0

    if args.dry_run:
        # --dry-run wires the stub profile against an ephemeral clone so the full
        # control flow runs without any real target (M0 exit criterion).
        return _run_dry(args, caps, log)

    print(
        "[auto-improvement] --go requires a configured Target Profile (M2/M3). "
        "M0 ships the spine + the stub profile (--dry-run)."
    )
    return 0


def _run_dry(args, caps: BudgetCaps, log: logging.Logger) -> int:
    """Run one ``--dry-run`` cycle with the stub profile against a throwaway clone.

    Builds a real (tiny) git repo so the worktree/commit plumbing exercises real git,
    then runs the driver for one cycle. Mirrors ``autoloop.py --dry-run``."""
    from .stub_profile import StubProfile

    tmp = Path(tempfile.mkdtemp(prefix="auto_improvement_dry_"))
    clone = tmp / "clone"
    (clone / "src" / "mesh_pkg").mkdir(parents=True)
    (clone / "src" / "mesh_pkg" / "__init__.py").write_text("# stub package\n")
    _git(["init", "-q", "-b", "auto_improvement/trunk-base"], clone)
    _git(["config", "user.email", "dry@example.com"], clone)
    _git(["config", "user.name", "dry"], clone)
    _git(["add", "-A"], clone)
    _git(["commit", "-q", "-m", "stub base"], clone)
    # disable push the way a profile would (no-op URL); the stub reports disabled.
    _git(["remote", "add", "origin", "DISABLED_NO_PUSH"], clone)

    # Honor --data-dir so the documented flag is live for --dry-run too: the spine
    # writes its archive/ledger/pr_queue ONLY under this data dir (08_safety §6.3 —
    # the dedup ledger lives at <data>/state/ledger.jsonl). When the caller did not
    # pass --data-dir, fall back to a throwaway dir inside the ephemeral run root so
    # a bare ``--dry-run`` stays self-cleaning. The clone/worktrees always live in the
    # ephemeral root (never under the persisted data dir).
    data = args.data_dir if getattr(args, "data_dir", None) else tmp / "data"
    profile = StubProfile(clone_path=clone, queue_dir=data / "pr_queue")
    driver = Driver(
        profile=profile,  # type: ignore[arg-type]  # dev/CLI smoke stub duck-types TargetProfile
        clone=clone,
        branch="auto_improvement/trunk-base",
        archive_root=data / "results",
        ledger_path=data / "state" / "ledger.jsonl",
        pr_queue_dir=data / "pr_queue",
        worktree_root=tmp / "worktrees",
        caps=caps,
        logger=log,
    )
    stats = driver.run(dry_run=True)
    print(f"\n[auto-improvement] --dry-run complete: {stats}")
    print(f"  archive: {data / 'results'}")
    print(f"  ledger:  {data / 'state' / 'ledger.jsonl'}")
    print(f"  pr_queue:{data / 'pr_queue'}")
    return 0


# ── the owner family ─────────────────────────────────────────────────────
#
# This module is the facade of the spine driver and the path every caller imports. ``Driver``
# keeps its whole interface here: the members whose bodies live in an owner are bound on the
# class by name, so a patch of ``Driver.<member>``, or of one instance's member, reaches every
# caller. New work goes to the owner of its responsibility:
#
#   ``driver_git`` -- the host-side ``_git`` chokepoint and the object reads built on it
#   ``driver_publish`` -- the F10 direct push, its gates and its one rebase-and-retry
#   ``driver_commit`` -- the winner's provisional commit: stage, commit, amend, roll back
#
# A name this module binds is read here, and a git helper that moved to ``driver_git`` is
# served from it. A write or delete through this module -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every owner that binds the same object (``kiro_crew._facade_mirror``),
# so a patch of ``driver._git`` or ``driver.subprocess`` reaches the moved code as it did when
# this was one module. The imports at the top stay bound even where this module's own code
# does not read them (``# noqa: F401``): they are its surface, which callers read and tests
# patch. Owners never import this module at run time. ``test/test_driver_owner_bindings.py``
# pins the bindings, the mirror and that direction.
#: The owner modules, lowest layer first: the first holder of a name answers its reads.
#: Named from this module's own package, so the family also loads under the app loader's name.
_OWNERS = (
    f"{__package__}.driver_git",
    f"{__package__}.driver_publish",
    f"{__package__}.driver_commit",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it. Its
#: surface is what it bound before the split: its own names and the git helpers it defined.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(
    _facade_mirror.install(
        sys.modules[__name__],
        _OWNERS,
        excluded=(),
        surface=frozenset(globals()) | frozenset(vars(sys.modules[_OWNERS[0]])),
    ).served
)
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.apps.builtins.auto_improvement.spine.driver_git import (  # noqa: F401
        _ATTRIBUTION_FORMAT,
        _GIT_SAFE_CONFIG,
        _commit_attribution,
        _commit_tree,
        _committer_identity,
        _expected_replay_tree,
        _fetched_tip_oid,
        _full_head_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
