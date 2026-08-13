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
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import ledger as L
from . import pr_description as D
from . import preflight as PF
from .archive import Archive
from .contracts import TRACK_BUG, TRACK_PERF, BugGateResult, Proposal, TargetProfile
from .gate import Gate
from .git_safety import GIT_SAFE_CONFIG, require_pinned
from .keeper import KEPT, Keeper
from .measurer import Measurer
from .pr_pipeline import CrPipeline
from .preflight import PreflightResult
from .proposer import Proposer
from .push_policy import normalize_branch


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


#: Trusted git config injected on EVERY host-side git invocation over an agent-writable tree.
#: The agent runs inside a sandbox, but these git commands run on the HOST as the gateway user
#: against the same worktree/clone the agent edits — so a repository instruction that has the
#: auto-approved shell write a hook and point `core.hooksPath` at it would get that hook
#: EXECUTED host-side, outside the sandbox, on the next add/commit/checkout. `core.hooksPath` to
#: os.devnull disables every hook; `core.fsmonitor=false` disables the fsmonitor daemon, a second
#: repo-controlled exec vector (a repo can set it to an arbitrary program git then spawns). These
#: are `-c` overrides on OUR argv, which take precedence over anything in the repo's own config,
#: and they are placed BEFORE the subcommand so git applies them. Raised by the GPT review.
_GIT_SAFE_CONFIG = GIT_SAFE_CONFIG


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    require_pinned(cwd)
    # ``errors="replace"``: callers run `diff`/`show`, which print file CONTENT, and a repo
    # legitimately holds non-UTF-8 bytes. A strict decode raises inside
    # ``subprocess.communicate``, so the failure cannot be read off ``returncode`` — the
    # direct-push path would abort on any tree containing a PNG. See ``pr_watchers._git``.
    return subprocess.run(
        ["git", "-C", str(cwd), *_GIT_SAFE_CONFIG, *args],
        capture_output=True,
        text=True,
        errors="replace",
    )


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
        )
        self._stop = False

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
        ambiguity → the original refusal stands."""
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
        # the ~30-boot calibration loop between reps (previously a Stop click had to
        # wait out the entire preflight — the "stuck in phase2_perf" symptom). The
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
            # surface each cycle (operator directive 2026-06-18: do not limit the search space
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
        proposals = self.proposer.fan_out(
            profile=self.profile,
            candidates=fresh_candidates,
            base_sha=base_sha,
            cycle=cycle,
            stop_check=lambda: self._stop,
        )
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
                except Exception as e:  # noqa: BLE001 — one bad candidate must NEVER
                    # kill the whole run (the original autoloop recorded status=error and
                    # continued; without this a gate/measure exception aborts the run).
                    self.log.error(
                        "cycle %d: candidate %s errored: %s: %s",
                        cycle,
                        prop.cand_id,
                        type(e).__name__,
                        e,
                    )
                    self.stats.errors += 1
                    self._record(prop, L.STATUS_ERROR, f"{type(e).__name__}: {e}")

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
            for prop in proposals:
                self.proposer.teardown(prop)

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

    def _reverify_head(self) -> bool:
        """Re-run the profile's build gate on the clone's CURRENT tree. Fail-closed.

        Called only after a rebase rewrote HEAD. The gate result we hold was measured
        against the PRE-rebase base, so it says nothing about the replayed tree: a rebase
        can apply cleanly and still produce a combination that was never built or tested
        (our patch plus whatever landed on the branch meanwhile). Publishing on the
        strength of the stale result would break the app's core promise — that nothing
        reaches a shared branch unless a measurement on THAT tree passed.

        Any error re-verifying is a refusal, not a pass: an unverifiable tree is exactly
        the case this gate exists for.
        """
        try:
            res = self.profile.build_gate.build_and_test(
                worktree=self.clone, src=self.clone / "src"
            )
        except Exception as exc:  # noqa: BLE001 — an unverifiable tree must not publish
            self.log.error("direct-push: could not re-verify the rebased tree: %s", exc)
            return False
        if not getattr(res, "passed", False):
            self.log.warning(
                "direct-push: rebased tree FAILED re-verification (%s) — not pushing",
                getattr(res, "detail", "") or "no detail",
            )
            return False
        return True

    def _push_with_rebase(self, fetch_url: str, dest: str, target: str):
        """Push HEAD to ``dest``, rebasing ONCE onto the remote if it moved meanwhile.

        A run takes tens of minutes, so the branch can legitimately advance between the
        clone's fetch and the winner's push — and a bare push then dies
        ``! [rejected] ... (fetch first)``, stranding a fully verified fix. Measured on
        this app's own dogfood: 3 of 6 gate survivors were lost this way, every one of
        them work that had already passed RED x2 -> GREEN -> STAYGREEN.

        The retry is deliberately narrow and safe:
          * only on a NON-FAST-FORWARD rejection — any other failure (auth, no such
            ref, protected branch) is returned untouched, because retrying those just
            hides the real error;
          * ``git rebase`` REPLAYS our single verified commit on top of the new remote
            tip. A conflict aborts the rebase and returns the original failure rather
            than pushing a half-merged tree;
          * the REPLAYED tree is RE-VERIFIED through the profile's build gate before it
            is pushed (:meth:`_reverify_head`). A clean rebase is a statement about
            TEXT, not about behaviour: our verified patch combined with whatever landed
            on the branch meanwhile is a tree nothing has ever built or tested. Without
            this the retry published an unverified commit — the one thing the whole
            measurement-first pipeline exists to prevent. Raised by the GPT review of
            this branch;
          * never ``--force``. If the second push is also rejected, we stop — a losing
            race is a signal, not something to overwrite.

        The caller must read the pushed sha from the clone AFTER this returns, not from
        its own pre-push snapshot: a rebase rewrites HEAD, so the pre-rebase sha names a
        commit that does not exist on the remote.
        """
        require_pinned(self.clone)
        push = subprocess.run(
            ["git", "-C", str(self.clone), *_GIT_SAFE_CONFIG, "push", fetch_url, f"HEAD:refs/heads/{dest}"],
            capture_output=True,
            text=True,
        )
        for _ in range(self._PUSH_ATTEMPTS - 1):
            if push.returncode == 0:
                return push
            blob = f"{push.stdout or ''}\n{push.stderr or ''}"
            if "non-fast-forward" not in blob and "fetch first" not in blob:
                return push  # a different failure — do not mask it with a retry
            self.log.info("direct-push: %s moved under us; rebasing and retrying", dest)
            if _git(["fetch", fetch_url, dest], self.clone).returncode != 0:
                return push
            reb = _git(["rebase", "FETCH_HEAD"], self.clone)
            if reb.returncode != 0:
                _git(["rebase", "--abort"], self.clone)
                self.log.warning("direct-push: rebase onto %s conflicted — not pushing", dest)
                return push
            if not self._reverify_head():
                return push  # rebased tree is unverified — return the original rejection
            require_pinned(self.clone)
            push = subprocess.run(
                ["git", "-C", str(self.clone), *_GIT_SAFE_CONFIG, "push", fetch_url, f"HEAD:refs/heads/{dest}"],
                capture_output=True,
                text=True,
            )
        return push

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
                if self._direct_push(
                    fp=outcome.fp, kind="perf", target=winner.candidate.target, sha=committed
                ):
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
                else:
                    # ROLL BACK the refused commit. Leaving it at HEAD is a credential LEAK,
                    # not just untidy bookkeeping: the direct-push scan range is
                    # `HEAD~1..HEAD` (one commit), so the NEXT winner's scan does not see this
                    # commit while its push publishes both. Measured on a real repo: candidate
                    # A refused for a planted `AKIAIOSFODNN7EXAMPLE`, candidate B's scan range
                    # showed the credential = False while its pushed range showed it = True.
                    # Raised by the GPT review of this branch.
                    self._reset_provisional(pre_sha)
                    self.stats.kept -= 1  # push refused/failed → not a realized outcome
                return fresh_count
            self.stats.filed += 1
            self.log.info(
                "cycle %d: FILED %s cr=%s commit=%s", cycle, winner.cand_id, outcome.cr, committed
            )
            # Announce the filed CR so the app can start a watcher session that keeps it
            # mergable + drives it to passing-all-checks (tasks #21/#24). Opaque to the
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

    @staticmethod
    def _redact_commit_message(msg: str) -> str:
        """Strip credentials / exfiltration URLs from a commit message before it becomes
        PERMANENT git metadata. The message is built from agent-authored content (proposal
        signature/description), which is untrusted (CLAUDE.md) — and metadata-leak
        guidance is explicit that git commit messages "stay forever in
        the repository", so a leaked secret there is unwipeable. Applies to BOTH the CR-path
        local commit and the F10 direct-push commit. Best-effort: if the redaction helpers
        are unavailable, the message passes through (the commit still happens)."""
        try:
            # Kiro Crew's core redactor: one call, string return, both the
            # credential and exfiltration-URL passes. (The port originally
            # referenced a vendored module that does not exist here, so this
            # silently no-op'd on every commit — a real leak risk, now fixed.)
            from kiro_crew.security import redact

            msg = redact(msg)
        except Exception:  # noqa: BLE001 — a commit message is permanent, pushed git history
            # FAIL CLOSED (same as backend/commit.py): a message that cannot be scanned must
            # not be committed verbatim, since it becomes unwipeable once pushed. Fall back
            # to a fixed, prose-free subject. Raised by the GPT review of this branch.
            logging.getLogger("auto_improvement.driver").warning(
                "commit-message redaction failed; using a fixed subject"
            )
            return "auto-improvement: apply verified change"
        return msg

    def _prepush_review_clean(self, *, target: str, base_ref: str) -> tuple[bool, str]:
        """F10 + F6: run a REAL automated reviewer review on the just-committed fix
        diff BEFORE the direct push, and return ``(clean, note)``.

        A direct-pushed commit gets NO human review — so the automated reviewer must clear
        it first (the user's pre-push-gate decision; the F6 roadmap item). We run the SAME
        automated reviewer the post-CR watcher uses, but as a one-shot REVIEW-only verdict on
        ``base_ref...HEAD`` in the clone, via this driver's agent runner. The agent emits a
        final ``REVIEW: clean`` / ``REVIEW: <N> open`` / ``REVIEW: unavailable`` line.

        AUTHORIZATION (operator directive 2026-06-15): the push is allowed when the fix has
        a clean POSITIVE signal — a clean review verdict OR (when the review is INCONCLUSIVE)
        a clean full build/test (``_build_test_pre_push_clean``). CONCRETE review open
        findings still BLOCK (a green build does not excuse a real review finding).
        FAIL-CLOSED: if the gate is required (``self.prepush_review``) and NEITHER signal
        is provably clean — open findings, OR (review inconclusive AND build/test red),
        no agent runner, or any error — we return ``(False, …)`` so the push is BLOCKED. An
        auto-pushed, un-reviewed, unproven commit is what the gate exists to prevent. When
        the gate is OFF (default), returns ``(True, "gate disabled")`` without running."""
        if not getattr(self, "prepush_review", False):
            return True, "pre-push review gate disabled"
        runner = self._agent_runner
        if runner is None:
            return False, "pre-push review REQUIRED but no agent runner — blocking push"
        try:
            # Self-contained review instruction. The upstream version shelled out to a
            # host-specific reviewer skill discovered on disk; that coupling is gone, so
            # the diff itself is the whole input and the reviewer is the session agent.
            prompt = (
                "You are the PRE-PUSH review gate for an autonomous bug-fix loop. A fix was\n"
                "just committed locally and is about to be PUSHED to a shared feature branch\n"
                "with NO human review, so you are the only review it will get.\n\n"
                f"Review the diff of `{base_ref}...HEAD` in this repository. Read the changed\n"
                "files for real context — do not review the diff hunks in isolation.\n\n"
                "Look for defects that would matter in review: incorrect logic, unhandled\n"
                "errors, resource leaks, race conditions, security issues, and behaviour\n"
                "changes the commit does not mention. Ignore style preferences.\n\n"
                "Do NOT push. Do NOT open a pull request. Review only — you may fix a trivial\n"
                "finding in the clone and re-review, but the last line MUST be the verdict.\n\n"
                "End your reply with EXACTLY one line: `REVIEW: clean` if there are no open\n"
                "findings on the added or changed lines, else `REVIEW: <N> open`. If you could\n"
                "not actually review the diff, say `REVIEW: unavailable` rather than guessing —\n"
                "an unfounded `clean` is the one answer that defeats this gate."
            )
            res = runner.run(
                prompt,
                cwd=str(self.clone),
                allowed_tools=["Bash", "Read", "Edit", "Grep", "Glob"],
                max_turns=30,
                timeout_s=420,
            )
            out = (getattr(res, "text", "") or "").strip()
            # Parse the LAST REVIEW verdict line (the review may print intermediate ones).
            verdict = ""
            for ln in out.splitlines():
                s = ln.strip()
                if s.upper().startswith("REVIEW:"):
                    verdict = s
            low = verdict.lower()
            if "clean" in low:
                return True, "prepush_review clean"
            # the review found CONCRETE open findings → a real defect signal → BLOCK (a clean
            # build does NOT excuse open review findings; this path stays fail-closed).
            if "open" in low:
                return False, f"prepush_review found open findings: {verdict[:120]}"
            # the review was INCONCLUSIVE (unavailable / no parseable verdict). Per the operator
            # decision, an inconclusive review must NOT permanently block a fix that is
            # otherwise provably safe — fall back to a clean POSITIVE build/test signal
            # (a fresh full `bb release` / pytest suite green on the committed fix). The push
            # is authorized iff the review is clean OR the build/test is clean; it stays
            # fail-closed only when BOTH are inconclusive (06_*.md F6/F10; operator directive
            # 2026-06-15: "clean prepush_review + bb release or other clean autotest should allow
            # push to the remote").
            reason = (
                "prepush_review unavailable"
                if "unavailable" in low
                else "prepush_review produced no clear verdict"
            )
            build_ok, build_note = self._build_test_pre_push_clean(target=target)
            if build_ok:
                return True, f"{reason}; authorized by clean build/test ({build_note})"
            return (
                False,
                f"{reason} AND build/test not clean ({build_note}) — blocking push (fail-closed)",
            )
        except Exception as e:  # noqa: BLE001 — a gate error must BLOCK, never silently pass
            return False, f"prepush_review gate error ({type(e).__name__}) — blocking push"

    def _build_test_pre_push_clean(self, *, target: str) -> tuple[bool, str]:
        """Fallback pre-push signal: is the committed fix's tree a clean full build/test?

        Returns ``(clean, note)``. Runs the profile's full-suite gate (``bug_runner
        .run_suite`` — the profile-supplied build/test command, the SAME check STAYGREEN
        uses) against the clone's working tree (HEAD = the just-committed fix). A green
        suite is an independent, deterministic positive signal that the fix did not break
        the build — the operator-approved alternative to a clean review verdict when the
        autonomous review is inconclusive. Fail-closed: any missing primitive /
        error / red suite returns ``(False, …)`` so the push is still blocked unless the
        suite is PROVABLY green. (The concrete build command is the profile's concern, not
        the spine's — kept target-agnostic here.)"""
        runner = getattr(self.profile, "bug_runner", None)
        run_suite = getattr(runner, "run_suite", None)
        if not callable(run_suite):
            return False, "no build/test gate available"
        src = self.clone / "src"
        if not src.exists():
            src = self.clone
        try:
            green, failing = run_suite(src=src)
        except Exception as e:  # noqa: BLE001 — a gate error blocks, never silently passes
            return False, f"build/test gate error ({type(e).__name__})"
        if green:
            return True, "full suite green"
        return False, f"{len(failing)} failing test(s): {', '.join(failing[:3])}"

    def _direct_push(self, *, fp: str, kind: str, target: str, sha: str) -> bool:
        """F10: push the just-committed verified change to the operator-authorized branch.

        Returns True iff the push succeeded. Re-checks authorization at push time (never
        assumes the start-time check still holds): a protected/blank branch is refused by
        :func:`.push_policy.authorize_direct_push` — the spine-side, non-overridable gate.
        The push target is the bare branch name (``origin/x`` → ``x``), pushed to ``origin``
        explicitly via the clone's FETCH url (the push *remote* stays ``DISABLED_NO_PUSH``;
        we push to the real fetch url for this ONE ref so the global push-disable holds for
        everything else). A failed/ refused push is logged and recorded as ``error`` — the
        verified commit stays local (recoverable), nothing escapes the sandbox silently.

        Before pushing, the pre-push review gate (:meth:`_prepush_review_clean`) must pass
        when enabled — an auto-pushed commit gets no human review, so the automated reviewer
        is its gate (F6/F10; fail-closed)."""
        from .push_policy import (
            authorize_direct_push,
            describe_scan,
            normalize_branch,
            scan_content_for_secrets,
        )

        ok, reason = authorize_direct_push(direct_commit=self.direct_commit, branch=self.branch)
        if not ok:
            self.log.warning("direct-push refused for %s: %s", target, reason)
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=kind,
                    target=target,
                    status=L.STATUS_ERROR,
                    note=f"direct-push refused: {reason}"[:200],
                )
            )
            return False
        # PRE-PUSH REVIEW GATE (fail-closed): a direct-pushed commit gets no human review,
        # so the automated reviewer must clear it before it lands on the shared branch.
        clean, note = self._prepush_review_clean(target=target, base_ref=self.branch)
        if not clean:
            self.log.warning("direct-push BLOCKED by review gate for %s: %s", target, note)
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=kind,
                    target=target,
                    status=L.STATUS_ERROR,
                    note=f"pre-push review gate blocked: {note}"[:200],
                )
            )
            return False
        if not sha or sha == "-":
            self.log.error("direct-push: no commit sha for %s — skipping push", target)
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=kind,
                    target=target,
                    status=L.STATUS_ERROR,
                    note="direct-push: winner diff did not apply",
                )
            )
            return False
        dest = normalize_branch(self.branch)
        # Resolve the real remote URL the clone FETCHES from (push remote is disabled). We
        # push HEAD (the verified commit we just made on self.branch) to the authorized ref.
        # Prefer the url the PROFILE was given (carried in config), because the clone's
        # own remote urls are both neutralized so agent-run Bash inside it cannot find a
        # push target. Falling back to the clone keeps older configs working: it yields
        # the DISABLED sentinel, which the check below refuses — fail closed, never a
        # silent unguarded push.
        fetch_url = str(getattr(getattr(self.profile, "pr_recipe", None), "fetch_url", "") or "")
        if not fetch_url:
            fetch_url = _git(["remote", "get-url", "origin"], self.clone).stdout.strip()
        if not fetch_url or "DISABLED" in fetch_url.upper():
            self.log.error("direct-push: no usable fetch url for %s — refusing", target)
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=kind,
                    target=target,
                    status=L.STATUS_ERROR,
                    note="direct-push: no usable remote url",
                )
            )
            return False

        # Scan the CONTENT before it leaves the host. `_redact_commit_message` covers the
        # message; this covers the commit itself, which is equally unwipeable once pushed
        # and is agent-authored. Detect-and-refuse: the commit stays local and the ledger
        # records why, rather than publishing a silently-rewritten patch.
        # Scan the RANGE THAT WILL BE PUSHED, which is the verified commit itself: HEAD and
        # its parent. The earlier `{dest}..HEAD` form was silently EMPTY — `dest` is the
        # local branch this commit sits on the tip of, so `git diff <branch>..HEAD` diffs a
        # ref against itself and returns nothing, which `scan_content_for_secrets` reads as
        # "clean" and the fail-closed credential gate is SKIPPED. Measured against a real
        # repo: a commit adding an AWS key produced a 0-byte `<branch>..HEAD` diff and a
        # 144-byte `HEAD~1..HEAD` diff carrying the key. (This regressed when the checkout
        # was moved to the local branch; before that `dest` was the stale `origin/<branch>`
        # tracking ref, which happened to differ.) Raised by the GPT review of this branch.
        #
        # `HEAD~1..HEAD` for a normal commit; `--root` shows a root commit that has no
        # parent. The git call's EXIT STATUS is load-bearing, not just its stdout: `_git`
        # does not raise, and a failed diff exits non-zero with EMPTY stdout — which the
        # scanner would read as "nothing to scan". Both sibling call sites refuse on a
        # non-zero status; this one must too.
        has_parent = (
            _git(["rev-parse", "--verify", "--quiet", "HEAD~1"], self.clone).returncode == 0
        )
        proc = (
            _git(["diff", "HEAD~1..HEAD"], self.clone)
            if has_parent
            else _git(["show", "--format=", "--root", "HEAD"], self.clone)
        )
        if proc.returncode != 0:
            self.log.error(
                "direct-push REFUSED for %s: could not read the pushable diff (git exit %s)",
                target,
                proc.returncode,
            )
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=kind,
                    target=target,
                    status=L.STATUS_ERROR,
                    note="direct-push refused: could not read the pushable diff",
                )
            )
            return False
        clean, scan_code = scan_content_for_secrets(proc.stdout or "")
        if not clean:
            # `describe_scan` maps a fixed code to a fixed literal, so nothing derived
            # from the scanned content reaches this log line or the ledger row below.
            scan_note = describe_scan(scan_code)
            self.log.error("direct-push REFUSED for %s: %s", target, scan_note)
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=kind,
                    target=target,
                    status=L.STATUS_ERROR,
                    note=f"direct-push refused: {scan_note}",
                )
            )
            return False

        push = self._push_with_rebase(fetch_url, dest, target)
        # Read the sha back from the clone: a rebase inside `_push_with_rebase` rewrites
        # HEAD, so the caller's pre-push snapshot would name a commit that is NOT on the
        # remote. `self.pushed_sha` is what the ledger records. Fall back to the snapshot
        # only when rev-parse fails, so a reporting hiccup cannot blank a real sha.
        head_after = _git(["rev-parse", "HEAD"], self.clone)
        self.pushed_sha = (head_after.stdout or "").strip() or sha
        if push.returncode != 0:
            self.log.error("direct-push FAILED for %s: %s", target, (push.stderr or "")[:300])
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=kind,
                    target=target,
                    status=L.STATUS_ERROR,
                    note=f"direct-push failed: {(push.stderr or '')[:150]}",
                )
            )
            return False
        self.log.info("direct-push OK: %s → origin/%s (%s)", target, dest, self.pushed_sha)
        return True

    def _discard_staged(self, why: str) -> None:
        """Throw away the applied-but-not-committed diff so nothing inherits it.

        A provisional commit that FAILS (a rejecting hook, gpg/signing trouble) leaves the
        candidate's diff sitting in the index, and the next candidate's ``git commit`` —
        which stages with ``add -A`` — silently absorbs it. Measured on a real repo with a
        rejecting ``pre-commit`` hook: candidate B's commit contained candidate A's
        REJECTED, never-verified diff in ``m.py`` alongside B's own file. Publishing an
        unmeasured change is the one thing this pipeline must not do. Raised by the GPT
        review of this branch.

        ``reset --hard`` alone is not enough: files the patch CREATED were staged by
        ``add -A``, and a reset leaves them untracked on disk where the next ``add -A``
        picks them straight back up. So the added paths are collected first (from the
        index, before the reset) and removed individually — a targeted cleanup rather
        than a blanket ``git clean``, which would also delete unrelated build output.
        """
        added = _git(["diff", "--cached", "--name-only", "--diff-filter=A"], self.clone)
        paths = [p for p in (added.stdout or "").splitlines() if p.strip()]
        reset = _git(["reset", "--hard", "HEAD"], self.clone)
        if reset.returncode != 0:
            # Nothing else is safe to do here, but the operator must see it: the next
            # candidate may inherit this tree.
            self.log.error(
                "could not discard the staged diff after %s: %s",
                why,
                (reset.stderr or "")[:200],
            )
        for rel in paths:
            try:
                (self.clone / rel).unlink(missing_ok=True)
            except OSError as exc:  # a directory or a permission problem — log, continue
                self.log.warning("could not remove %s left by %s: %s", rel, why, exc)

    def _stage_winner(self, winner: Proposal) -> bool:
        """Apply the winner's diff to the working branch and stage it. Returns False when
        the diff does not apply (the caller records an error and does not draft).

        SPLIT OUT of :meth:`_commit_winner` so the winner is in the shared clone's tree
        BEFORE the PR pipeline drafts. ``pr_recipe._push_fix_branch`` pushes the clone's
        ``HEAD``, so drafting first meant pushing a branch that did not contain the fix —
        or contained a PREVIOUS cycle's commit. Raised by review of this branch; traced
        through: the queue copy carries ``winner.diff`` (correct), ``gated_commit_sha``
        feeds the reproduce MEASUREMENT rather than the draft, and ``gate_res.commit_sha``
        is the throwaway WORKTREE's head — none of them put the fix in the shared clone.

        Committing earlier instead would have been wrong: the commit MESSAGE needs
        ``outcome.reproduce``, which only the pipeline produces, and reordering that way
        would silently degrade every kept-commit message to echoing VERIFY (06_*.md
        §3.1/§3.2). Apply-then-draft-then-commit keeps both properties.
        """
        _git(["checkout", normalize_branch(self.branch)], self.clone)
        if not winner.diff.strip():
            return True
        ap = subprocess.run(
            ["git", "-C", str(self.clone), "apply"],
            input=winner.diff,
            capture_output=True,
            text=True,
        )
        if ap.returncode != 0:
            self.log.error("winner diff did not apply: %s", ap.stderr[:200])
            return False
        _git(["add", "-A"], self.clone)
        return True

    def _commit_winner_provisional(self, winner: Proposal) -> bool:
        """Apply the winner and commit it with a PLACEHOLDER message. False if it will not
        apply.

        A real commit, not just a staged index: ``git push HEAD:refs/heads/<b>`` sends the
        commit HEAD points at, so a staged-but-uncommitted fix is invisible to the draft.
        The final message needs the pipeline's reproduce measurement, so it is amended by
        :meth:`_finalize_winner_commit` once that exists, and rolled back by
        :meth:`_reset_provisional` when nothing is filed.
        """
        if not self._stage_winner(winner):
            return False
        if not winner.diff.strip():
            return True
        # CHECK the commit return code. `_git` does not raise, so a failed commit (a
        # rejecting hook, gpg failure, or an empty index) would otherwise leave HEAD on the
        # PREVIOUS commit while this returns True — the pipeline then drafts/pushes a commit
        # that does not contain the fix (or a prior cycle's). Fail closed instead. Raised by
        # the GPT review of this branch.
        commit = _git(
            # FIXED message — never `winner.cand_id`. `cand_id` embeds the model-chosen
            # `candidate.target`, and `_short` only restricts to alnum/`_`/`-`, which is
            # exactly the character class of an AWS key id or a `ghp_` token. Measured:
            # `src/m.py::AKIAIOSFODNN7EXAMPLE` produced
            # `c1_wide_m_py_AKIAIOSFODNN7EXAMPLE_d469bc5b`. This message is what the
            # DRAFT PUSH publishes — the redacted amend happens AFTER the push (871 ->
            # 887 -> 903) — so an unscanned cand_id lands in GitHub history, which cannot
            # be edited without rewriting it. The cand_id is still in the run archive and
            # the ledger, where it belongs. Raised by the GPT review of this branch.
            ["commit", "-q", "-m", "wip(auto-improvement): staging a verified candidate"],
            self.clone,
        )
        if commit.returncode != 0:
            self.log.error(
                "provisional commit failed for %s: %s",
                winner.cand_id,
                (commit.stderr or "")[:200],
            )
            self._discard_staged(f"a failed provisional commit for {winner.cand_id}")
            return False
        return True

    def _reset_provisional(self, pre_sha: str) -> None:
        """Roll the branch back to ``pre_sha`` after a provisional commit that was not
        filed, so a fluke, duplicate or error never advances HEAD (06_*.md §1.1)."""
        if not pre_sha:
            return
        head = _git(["rev-parse", "HEAD"], self.clone).stdout.strip()
        if head == pre_sha:
            return  # nothing was committed (empty diff)
        res = _git(["reset", "--hard", pre_sha], self.clone)
        if res.returncode != 0:
            self.log.error(
                "could not roll back the provisional commit to %s: %s",
                pre_sha[:10],
                (res.stderr or "").strip()[:160],
            )

    def _finalize_winner_commit(
        self, winner: Proposal, *, verify, reproduce=None, cycle: int, diff_ref: str
    ) -> str:
        """Apply the winner's diff to the working branch (local commit only), with the
        §2.4 attributable commit message (stage breakdown + guardrails + reproduce + RH
        guards + diff-ref). Every kept commit is independently reviewable (06_*.md §3.1).

        ``reproduce`` is the pipeline's SECOND independent A/B :class:`Measurement` (the one
        the CR description used); the commit message renders its REAL delta on the
        ``reproduce:`` line so commit and CR never disagree (06_*.md §3.1/§3.2). It falls
        back to ``verify`` only if the pipeline did not carry a reproduce measurement (it
        always does on a filed perf win) — never silently re-using VERIFY when the real
        numbers are available."""
        if not winner.diff.strip():
            return _git(["rev-parse", "--short", "HEAD"], self.clone).stdout.strip()
        if True:
            # Use the INDEPENDENT reproduce measurement (carried back from the pipeline) for
            # the ``reproduce:`` line so the kept-commit message states the real second-A/B
            # delta the CR cited, not an echo of VERIFY (06_*.md §3.1/§3.2; CrOutcome.reproduce).
            msg = D.perf_commit_message(
                proposal=winner,
                verify=verify,
                reproduce=reproduce if reproduce is not None else verify,
                cycle=cycle,
                primary_name=self.profile.ruler.primary_name,
                unit=self.profile.ruler.unit,
                diff_ref=diff_ref,
                guardrail_tolerances=self.guardrail_tolerances,
            )
            # AMEND: the provisional commit already carries the winner's tree (that is what
            # the draft pushed); only its message is replaced.
            _git(
                ["commit", "-q", "--amend", "-m", self._redact_commit_message(msg)],
                self.clone,
            )
        return _git(["rev-parse", "--short", "HEAD"], self.clone).stdout.strip()

    # ── bug-track keep/draft (M4; 05_improvement_loop_bugfix.md §4) ───────────

    def _apply_bug_winner(self, cycle: int, winner: Proposal, bug_res: BugGateResult) -> None:
        """Accept one bug fix that passed RED ∧ GREEN ∧ STAYGREEN: archive it,
        commit-on-keep locally, draft a DRAFT-only CR with the correctness narrative,
        and record ``filed`` in the shared ledger (05_*.md §4.2; 02_arch §3.2).

        This is the bug-track analogue of :meth:`_apply_verdict`'s keep path, but the
        CR trigger is the boolean RED/GREEN gate (the doubled-RED flake check is the
        reproduction analogue, §4.1) — there is no second A/B and no noise band."""
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
                if self._direct_push(
                    fp=outcome.fp, kind="bug", target=winner.candidate.target, sha=committed
                ):
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
                else:
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

    def _stage_bug_winner(self, winner: Proposal) -> bool:
        """Apply + stage the bug fix on the working branch. False when it will not apply.

        Split out for the same reason as :meth:`_stage_winner`: the recipe pushes the shared
        clone's HEAD, so the fix has to be in this tree BEFORE the pipeline drafts.
        """
        _git(["checkout", normalize_branch(self.branch)], self.clone)
        if not winner.diff.strip():
            return True

        # Apply with --3way: the diff was authored in a throwaway WORKTREE forked off a
        # base sha that may have drifted from the clone's branch HEAD (a prior candidate
        # landed, a clone-sync moved HEAD, or the agent touched an artifact like uv.lock
        # that already exists here). A plain ``git apply`` fails outright on any context
        # mismatch or "already exists in working directory" — the observed committed=0
        # cause (2026-06-17: "bug fix diff did not apply: error: uv.lock: already exists").
        # --3way falls back to a blob-level 3-way merge, which reconciles drift and
        # absorbs an already-present file instead of aborting. We retry plain-apply first
        # (cheapest, no index churn) and only fall back to 3-way so behavior is unchanged
        # when the base matches.
        def _apply(extra: list[str]):
            return subprocess.run(
                ["git", "-C", str(self.clone), "apply", *extra],
                input=winner.diff,
                capture_output=True,
                text=True,
            )

        ap = _apply([])
        if ap.returncode != 0:
            self.log.info(
                "bug fix plain-apply failed (%s) — retrying with --3way",
                (ap.stderr or "").strip()[:120],
            )
            ap = _apply(["--3way"])
        if ap.returncode != 0:
            self.log.error("bug fix diff did not apply (even --3way): %s", ap.stderr[:200])
            return False
        _git(["add", "-A"], self.clone)
        return True

    def _commit_bug_winner_provisional(self, winner: Proposal) -> bool:
        """Apply + commit the bug fix with a placeholder message. See
        :meth:`_commit_winner_provisional` for why a commit rather than a staged index."""
        if not self._stage_bug_winner(winner):
            return False
        if not winner.diff.strip():
            return True
        # Same as the perf twin: a failed commit must not report success, or the draft/push
        # publishes a HEAD that lacks the fix. Raised by the GPT review of this branch.
        commit = _git(
            # FIXED message — never `winner.cand_id`. `cand_id` embeds the model-chosen
            # `candidate.target`, and `_short` only restricts to alnum/`_`/`-`, which is
            # exactly the character class of an AWS key id or a `ghp_` token. Measured:
            # `src/m.py::AKIAIOSFODNN7EXAMPLE` produced
            # `c1_wide_m_py_AKIAIOSFODNN7EXAMPLE_d469bc5b`. This message is what the
            # DRAFT PUSH publishes — the redacted amend happens AFTER the push (871 ->
            # 887 -> 903) — so an unscanned cand_id lands in GitHub history, which cannot
            # be edited without rewriting it. The cand_id is still in the run archive and
            # the ledger, where it belongs. Raised by the GPT review of this branch.
            ["commit", "-q", "-m", "wip(auto-improvement): staging a verified candidate"],
            self.clone,
        )
        if commit.returncode != 0:
            self.log.error(
                "provisional bug commit failed for %s: %s",
                winner.cand_id,
                (commit.stderr or "")[:200],
            )
            self._discard_staged(f"a failed provisional bug commit for {winner.cand_id}")
            return False
        return True

    def _finalize_bug_winner_commit(
        self, winner: Proposal, *, bug_res: BugGateResult, cycle: int, diff_ref: str
    ) -> str:
        """Apply the bug fix to the working branch (local commit only). The commit
        message states the defect + the RED→GREEN correctness narrative (not a perf
        metric — 05_*.md §4.2 / 06_*.md §3.1 contrast the bug narrative with the perf A/B)."""
        if winner.diff.strip():
            # AMEND the provisional commit: its tree is what the draft pushed; only the
            # message is replaced (staging + --3way happened in _stage_bug_winner).
            msg = D.bug_commit_message(
                proposal=winner, bug_res=bug_res, cycle=cycle, diff_ref=diff_ref
            )
            _git(
                ["commit", "-q", "--amend", "-m", self._redact_commit_message(msg)],
                self.clone,
            )
        return _git(["rev-parse", "--short", "HEAD"], self.clone).stdout.strip()

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
            res = self.preflight()  # raises (HALT/BLOCK) if the ruler is not proven
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


if __name__ == "__main__":
    raise SystemExit(main())
