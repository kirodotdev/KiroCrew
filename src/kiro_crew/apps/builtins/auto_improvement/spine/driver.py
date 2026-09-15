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

from kiro_crew.platform.context import redact_log_via_context, redact_via_context
from kiro_crew.subprocess_utf8 import UTF8_TEXT

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


def _git(
    args: list[str], cwd: Path, *, config: tuple[str, ...] = ()
) -> subprocess.CompletedProcess:
    require_pinned(cwd)
    # ``config`` carries EXTRA ``-c`` pairs for the one caller that needs them, spliced into the
    # flag section so *args* still begins with the subcommand. Keeping that position stable is
    # deliberate: this module's own callers and its test doubles both read argv by its leading
    # verb, and a flag pushed in front of it would make the same call unrecognisable to them.
    # ``core.useReplaceRefs=false`` on EVERY call in this module, not just the credential scan.
    # Git substitutes objects named by ``refs/replace/<oid>`` transparently in reads while the
    # push transport sends the ORIGINAL, and `git replace` is not on the pre-push reviewer's
    # denylist -- so any read here that a replacement could redirect is a place where what this
    # code inspects and what it publishes come apart. Two instances were found one at a time
    # (the scan, then the rebase); disabling it at the single chokepoint every read goes through
    # closes the class FOR THIS MODULE'S READS instead of the next
    # instance. It is not repository-wide: `pr_recipe` and `backend/commit.py` do their own
    # git reads and still honour `refs/replace/*`, so their scans remain substitutable the
    # same way. Out of scope here, named so the guarantee is not read as broader than it is. Raised by the GPT review of this branch.
    # ``errors="replace"``: callers run `diff`/`show`, which print file CONTENT, and a repo
    # legitimately holds non-UTF-8 bytes. A strict decode raises inside
    # ``subprocess.communicate``, so the failure cannot be read off ``returncode`` — the
    # direct-push path would abort on any tree containing a PNG. See ``pr_watchers._git``.
    return subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            *_GIT_SAFE_CONFIG,
            "-c",
            "core.useReplaceRefs=false",
            *config,
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _fetched_tip_oid(porcelain_stdout: str) -> str:
    """The object id ``git fetch --porcelain`` reports for the ref it just fetched.

    ``FETCH_HEAD`` is a mutable FILE in the clone, not a value, so keying the rebase or the
    replay count on that NAME lets anything else operating in the clone substitute what gets
    replayed -- and because only the replayed commit is scanned, a substituted parent's content
    rides along to the push through a scanner that believed it had looked. ``--porcelain`` makes
    the fetch report the id on its OWN stdout, one line per ref as
    ``<flag> <old-oid> <new-oid> <local-ref>``, so the id never comes from a ref at all. A
    forty-hex object id cannot be substituted; a ref name can. Raised by the GPT review of this
    branch, which prescribed exactly this: capture the id from the fetch operation itself.

    Returns ``""`` when no line carries a usable id -- including the all-zero null id, which
    means the ref was DELETED rather than fetched, and the case where ``--porcelain`` is not
    understood at all (an older git fails the fetch outright, so the caller never gets here).
    The caller must treat ``""`` as a refusal: an id it could not obtain is the absence of the
    check, not a pass.
    """
    for line in (porcelain_stdout or "").splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        new_oid = fields[2]
        if len(new_oid) == 40 and all(c in "0123456789abcdef" for c in new_oid):
            if new_oid == "0" * 40:
                continue  # a deletion, not a tip anything can be rebased onto
            return new_oid
    return ""


def _expected_replay_tree(base: str, src: str, cwd: Path) -> str:
    """The tree a replay of *src* onto *base* must produce. ``""`` if it cannot be computed.

    A rebase replays a commit onto a new parent, so the replay is a DIFFERENT object id
    carrying the same change -- an id captured from ambient ``HEAD`` afterwards cannot be
    checked against the authorized input by equality. What CAN be stated exactly is the
    RESULT: ``git merge-tree --write-tree`` performs the same three-way merge the rebase does
    and prints the resulting tree's object id on its own stdout, computed from two immutable
    inputs and touching no ref. Comparing that against the replay's own tree authorizes the
    published content itself rather than a property of it.

    A TREE BINDS LOCATION; A PATCH IDENTITY DOES NOT. ``git patch-id`` deliberately ignores
    hunk line numbers -- that is what lets it recognise a change replayed onto a moved base --
    so a commit whose added and removed LINES match the authorized change while sitting
    elsewhere in the file hashes identically to it. A tree names the exact content of every
    path, so the same lines in a different place is a different tree. Raised by the GPT review
    of this branch, which asked for exactly this: authorize the replay against an expected
    tree rather than a patch identity.

    Returns ``""`` on any non-zero exit, which covers a merge that CONFLICTS (the tree printed
    then records a conflict, it is not an authorization) and a git too old to know
    ``--write-tree``. The caller treats ``""`` as a refusal: a result it could not compute is
    the absence of the check, not a pass.
    """
    out = _git(["merge-tree", "--write-tree", base, src], cwd)
    if out.returncode != 0:
        return ""
    tree = ((out.stdout or "").splitlines() or [""])[0].strip()
    if len(tree) != 40 or any(c not in "0123456789abcdef" for c in tree):
        return ""
    return tree


_ATTRIBUTION_FORMAT = "%an%x00%ae%x00%cn%x00%ce%x00%B"


def _commit_attribution(rev: str, cwd: Path) -> str:
    """*rev*'s author, committer and message as one comparable string, or ``""``.

    DATES ARE DELIBERATELY ABSENT. A replay commits at a new wall-clock time, so including a
    date would make every honest retry compare unequal; the fields here are the ones a replay
    is supposed to carry over unchanged. NUL separates them so a name holding the delimiter
    cannot forge a match by spanning two fields.

    ``%B`` is the raw body, last because it is the only field that may contain newlines.
    """
    out = _git(["log", "-1", f"--format={_ATTRIBUTION_FORMAT}", rev], cwd)
    if out.returncode != 0:
        return ""
    return out.stdout or ""


def _committer_identity(rev: str, cwd: Path) -> tuple[str, str]:
    """*rev*'s committer name and email, or ``("", "")``.

    READ OFF AN IMMUTABLE OBJECT, which is the whole point: the identity a replay should carry
    is the one the authorized commit already holds, recorded when the pipeline created it and
    therefore before any reviewing agent ran. Configuration cannot be trusted for this at
    replay time -- the agent's shell can write the repository's own config, and nothing masks a
    global one either.
    """
    out = _git(["log", "-1", "--format=%cn%x00%ce", rev], cwd)
    if out.returncode != 0:
        return "", ""
    parts = (out.stdout or "").rstrip("\n").split("\0")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return "", ""
    return parts[0], parts[1]


def _commit_tree(rev: str, cwd: Path) -> str:
    """The tree object id *rev* points at, or ``""``.

    ``log -1 --format=%T`` rather than ``rev-parse <rev>^{tree}`` so this question carries its
    own verb: ``rev-parse`` in this module already answers an unrelated one (does a parent
    exist), and a caller keyed on argv cannot tell two questions apart under one prefix.
    """
    out = _git(["log", "-1", "--format=%T", rev], cwd)
    tree = (out.stdout or "").strip()
    if out.returncode != 0 or len(tree) != 40 or any(c not in "0123456789abcdef" for c in tree):
        return ""
    return tree


def _full_head_id(cwd: Path) -> str:
    """The FULL object id at ``HEAD``, validated, or ``""``.

    WHAT A FINALIZER HANDS BACK IS A TRUST ANCHOR, so it is a full object id and never an
    abbreviation. A short sha is an ambiguous NAME: git resolves a revision through ref names
    before abbreviated object ids, and a prefix stops naming one object as soon as a second
    object shares it. Anything able to write the repository between a read and the anchor's
    capture can arrange either reading, which makes the id that gets verified and the id that
    gets published two different things -- the substitution this whole path exists to refuse.
    A forty-hex id has no second reading, so the class is absent rather than defended against.
    Raised by the GPT review of this branch.

    ``HEAD`` is safe to resolve here, and that is the distinction: no ref can shadow ``HEAD``,
    and this runs immediately after the commit it names, before any gate hands control to an
    agent. Callers that want a short form derive it from the returned id for DISPLAY and never
    feed it back into a lookup.

    Fails CLOSED: an unreadable or malformed answer returns ``""``, which every caller treats
    as "no committed object" and refuses to publish.
    """
    out = _git(["rev-parse", "HEAD"], cwd)
    sha = (out.stdout or "").strip()
    if out.returncode != 0 or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        return ""
    return sha


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

    def _revision_scans_clean(self, rev: str) -> tuple[bool, str]:
        """Credential-scan ONE revision's own object -- metadata and content. ``(clean, note)``.

        BOTH HALVES OF THE OBJECT ARE SCANNED. The tree diff covers what the commit changes;
        the commit's header lines are separate bytes the push transfers just the same, and the
        committer identity among them comes from configuration rather than from the replayed
        commit, so a process able to write the clone's git config chooses it.

        Shared by both publish paths so the scan cannot be attached to one of them and
        forgotten on the other -- which is exactly what happened: the scan lived inline in
        :meth:`_direct_push` and covered the first push's object, while the rebase retry in
        :meth:`_push_with_rebase` published a REPLAYED object that had been build-verified and
        never credential-scanned. A non-fast-forward retry is not an edge case here; the
        retry's own docstring records losing 3 of 6 gate survivors to that race. Raised by the
        GPT review of this branch.

        *rev* must be a FULL OBJECT ID, never ``HEAD``: the point is that the object scanned
        here and the object pushed are the same one by construction rather than by timing.

        The git call's EXIT STATUS is load-bearing, not just its stdout: ``_git`` does not
        raise, and a failed read exits non-zero with EMPTY stdout -- which the scanner would
        read as "nothing to scan". Every failure here therefore refuses. ``--root`` lets one
        listing cover a root commit, which has no parent to compare against.

        HISTORY WORTH KEEPING, because the shape of the range is what went wrong before: an
        earlier form diffed ``{dest}..HEAD``, which was silently EMPTY -- ``dest`` is the local
        branch the commit sits on the tip of, so that diffs a ref against itself, the scanner
        read "clean", and the fail-closed credential gate was SKIPPED entirely. Measured
        against a real repo at the time: a commit adding an AWS key produced a 0-byte
        ``{dest}..HEAD`` diff and a 144-byte single-commit diff carrying the key.
        """
        # Imported here, not at module scope, to match `_direct_push`'s existing pattern: the
        # name is then resolved on `push_policy` at call time, which is also the seam the
        # tests patch.
        from .push_policy import describe_scan, scan_content_for_secrets

        # `core.useReplaceRefs=false` is load-bearing, not hygiene. Git substitutes objects
        # named by `refs/replace/<oid>` transparently in READS -- `diff` and `show` included --
        # while the push transport sends the ORIGINAL object. So a reviewer that runs
        # `git replace` (not on that runner's denylist, which covers only `push` and
        # `remote set-url`) could point a replacement at clean content, have this scan read the
        # decoy, and still have the credential-bearing object transferred. Reading with replace
        # refs disabled makes what is scanned and what is sent the same bytes. Raised by the
        # GPT review of this branch.
        # Only `diff.external=` here: `_git` already injects `core.useReplaceRefs=false` on
        # every call it makes, so spelling it again is the same guarantee written twice, and
        # only the chokepoint spelling is test-pinned. Dropped on the first-principles review.
        _NO_REPLACE = ("-c", "diff.external=")
        # SCAN THE COMMIT OBJECT ITSELF, not only the content it carries. A tree diff shows
        # what the commit CHANGES; the commit's own header lines are separate bytes that the
        # push transfers just the same, and `--format=` on the root-commit branch suppresses
        # them outright. `git rebase` takes the COMMITTER identity from configuration rather
        # than from the commit it replays, so a process able to write the clone's git config
        # decides those bytes -- the same actor this method already refuses to trust for
        # `git replace` above, since the runner's denylist covers only `push` and
        # `remote set-url`. Measured on a real repository: a replayed commit whose config set
        # a key-shaped `user.email` carries it in the committer line while the diff branch and
        # the `--format=` branch both print nothing but the tree change. `cat-file commit`
        # yields the raw object -- tree, parent, author, committer, then the message -- so the
        # header bytes and the message reach the scanner as the transport sees them, and no
        # diff-shaping flag applies. Raised by the GPT review of this branch.
        meta = _git(["cat-file", "commit", rev], self.clone)
        if meta.returncode != 0:
            return False, "could not read the pushable commit object"
        clean, scan_code = scan_content_for_secrets(meta.stdout or "")
        if not clean:
            return False, describe_scan(scan_code)
        # SCAN THE RAW BLOBS THE PUSH TRANSFERS, not a rendered diff. `git diff` is a REPORT
        # for humans: for a blob it decides is binary it prints "Binary files a and b differ"
        # and no content at all, so a credential inside such a blob is invisible to a scan that
        # reads diff output -- and a NUL byte anywhere in a file is enough to trigger that,
        # which whatever writes the tree chooses freely. Measured on a real repository: a blob
        # holding NUL, then a key, then NUL renders as that one summary line with zero
        # occurrences of the key, while `cat-file blob` on the same object yields it. The push
        # sends objects rather than a diff, so enumerating the revision's own changed blobs and
        # reading each one is what makes the scanned bytes and the transferred bytes the same
        # bytes. This also scans a WHOLE blob rather than the changed hunks, which is again what
        # is published: a hunk is a view of an object, and it is the object that lands on the
        # remote. Raised by the GPT review of this branch.
        #
        # `--root` covers a root commit, which has no parent to diff against, so both cases go
        # through one listing rather than a branch whose second half was the weaker reading.
        listing = _git(
            ["diff-tree", "-r", "--root", "--no-commit-id", rev],
            self.clone,
            config=_NO_REPLACE,
        )
        if listing.returncode != 0:
            return False, "could not list the pushable blobs"
        for line in (listing.stdout or "").splitlines():
            # ``:<src mode> <dst mode> <src sha> <dst sha> <status>\t<path>``
            if not line.startswith(":"):
                continue
            meta_fields = line[1:].partition("\t")[0].split()
            if len(meta_fields) < 5:
                return False, "could not read a pushable blob entry"
            dst_mode, dst_sha, status = meta_fields[1], meta_fields[3], meta_fields[4]
            # A DELETION publishes no content, and a gitlink is a commit pointer with no blob
            # behind it -- `cat-file blob` on either is a failure that would fail this closed
            # for a revision carrying nothing to read.
            if status.startswith("D") or dst_mode == "160000" or set(dst_sha) == {"0"}:
                continue
            blob = _git(["cat-file", "blob", dst_sha], self.clone)
            if blob.returncode != 0:
                return False, "could not read a pushable blob"
            clean, scan_code = scan_content_for_secrets(blob.stdout or "")
            if not clean:
                # `describe_scan` maps a fixed code to a fixed literal, so nothing derived from
                # the scanned content reaches a log line or a ledger row.
                return False, describe_scan(scan_code)
        return True, ""

    def _restore_branch(self, branch: str, *, promote: str = "") -> bool:
        """Put the clone back on *branch*, moving it to *promote* first when given. ``ok``.

        RETURNS A STATUS because the caller must be able to abort on it. Logging a failed
        restore and continuing would publish with the clone detached or its durable branch
        stale -- a ref lock or a checkout failure is exactly the case where the repository is
        not in the state the push assumes. Raised by the GPT review of this branch.

        The rebase retry DETACHES HEAD in order to replay the authorized object rather than
        whatever the branch points at, so every exit from that path has to leave the clone
        attached again. :meth:`_reset_provisional` rolls a provisional commit back with
        ``git reset --hard``, which on a detached HEAD moves the detachment and leaves the
        BRANCH still carrying that commit -- a rollback that silently does not roll back.

        PROMOTION IS CONDITIONAL BY CONSTRUCTION. ``git branch -f`` is only correct when the
        thing it moves onto is the verified, scanned replay, so the caller passes *promote*
        at exactly one place: after the replay has passed re-verification, the HEAD-identity
        check and the credential scan. A failed or aborted rebase therefore cannot force the
        branch onto whatever HEAD happens to be -- it takes the no-argument form, which
        re-attaches without moving anything. Raised in review of this branch.
        """
        was = ""
        if promote:
            # TRANSACTIONAL: remember where the branch was, because `git branch -f` lands
            # BEFORE the checkout can fail. A concurrent index lock that fails the checkout
            # would otherwise leave the branch promoted to a replay this method then reports as
            # unrestored -- the push aborts, the caller rolls back the detached HEAD, and the
            # unpushed commit stays on the durable branch. Raised by the GPT review.
            was = _git(["rev-parse", branch], self.clone).stdout.strip()
            if _git(["branch", "-f", branch, promote], self.clone).returncode != 0:
                self.log.error(
                    "direct-push: could not move %s onto the replayed commit %s",
                    branch,
                    promote[:12],
                )
                return False
        if _git(["checkout", branch], self.clone).returncode != 0:
            self.log.error("direct-push: could not re-attach the clone to %s", branch)
            if promote and was:
                # Undo the promotion so the branch is not left carrying an unpushed commit.
                if _git(["branch", "-f", branch, was], self.clone).returncode != 0:
                    self.log.error(
                        "direct-push: could not put %s back on %s after a failed checkout",
                        branch,
                        was[:12],
                    )
            return False
        return True

    def _push_with_rebase(
        self, fetch_url: str, dest: str, target: str, src: str
    ) -> subprocess.CompletedProcess | None:
        """Push *src* to ``dest``, rebasing ONCE onto the remote if it moved meanwhile.

        *src* is the revision the FIRST push sends, and it is REQUIRED: an object id cannot be
        repointed, so once this is called, nothing running in the clone can change which commit
        gets published. Pushing the symbolic ``HEAD`` left a time-of-check/time-of-use window --
        the identity check happens, then the credential scan and the push each re-read ``HEAD``
        -- and a background process left behind by the pre-push reviewer could move it in
        between. Raised by the GPT review of this branch.

        It began as an optional parameter defaulting to ``HEAD`` "for callers that have nothing
        more specific". There were none: the sole production caller resolves the committed
        object first and passes it. That default kept three arms alive that fired only for a
        symbolic source -- skipping the pre-fetch tamper check, replaying the BRANCH rather than
        the object, and reading ``HEAD`` after the push for the ledger -- each of them the very
        check-then-use shape this change exists to remove. Requiring the argument deletes all
        three. Raised by the first-principles review of this branch, which asked for the
        subtraction rather than more guards.

        THE RETRY REPUBLISHES BY ID TOO. A rebase REPLAYS the commit as a NEW object, so *src*
        names the pre-rebase commit -- not what the rebase produced, and non-fast-forward
        anyway -- but the answer is to resolve the REPLACEMENT to a full id, credential-scan
        that object, and push it. An earlier revision of this method pushed ``HEAD`` there and
        justified it with "``_reverify_head`` has just run", which was the same
        check-then-use mistake this branch fixed twice elsewhere: the build gate is a check,
        the push is a later use, and ``HEAD`` is re-resolved in between. It also published an
        object no credential scan had ever covered, because the caller scans the PRE-rebase
        object. Raised by the GPT review of this branch.

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

        The caller must record ``self._pushed_object``, which this sets to whatever it actually
        sent, and NOT its own pre-push snapshot: a rebase rewrites HEAD, so the pre-rebase sha
        names a commit that does not exist on the remote. Reading the sha back from the clone is
        wrong for the same reason -- HEAD can move between the push and the read.
        """
        if self._retire_if_unsafe("direct-push preflight"):
            return None
        require_pinned(self.clone)
        # Remember WHICH OBJECT was sent, so the caller records that rather than re-reading
        # `HEAD` after the push: HEAD can move between the push and the read, which would put
        # an unrelated sha in the ledger for a commit that did land. Raised by the GPT review.
        self._pushed_object = src
        push = subprocess.run(
            [
                "git",
                "-C",
                str(self.clone),
                *_GIT_SAFE_CONFIG,
                "push",
                fetch_url,
                f"{src}:refs/heads/{dest}",
            ],
            capture_output=True,
            **UTF8_TEXT,
        )
        for _ in range(self._PUSH_ATTEMPTS - 1):
            if push.returncode == 0:
                return push
            blob = f"{push.stdout or ''}\n{push.stderr or ''}"
            if "non-fast-forward" not in blob and "fetch first" not in blob:
                return push  # a different failure — do not mask it with a retry
            self.log.info("direct-push: %s moved under us; rebasing and retrying", dest)
            # `git rebase` replays whatever the CURRENT BRANCH points at, not *src* -- so if
            # HEAD has moved since the caller's checks, the retry would replay the moved commit
            # and then bind its own capture, verify and scan to THAT, consistently passing while
            # publishing content that never descended from the verified object. Refuse instead:
            # the rebase input has to be the object this run authorized. Skipped when *src* is
            # the default symbolic `HEAD`, where there is no retained id to compare against.
            # Raised by the GPT review of this branch.
            # A TAMPER DETECTOR, not the guarantee it once was. The rebase below names the
            # authorized object EXPLICITLY, so what gets replayed does not depend on where
            # HEAD points and this check is not what makes that safe. It stays because a HEAD
            # that has moved by now means something wrote the clone after the caller's checks,
            # and a clone being written by an unknown actor is not one to publish from. On its
            # own it is defeatable: it sits before the `fetch`, a network operation lasting
            # seconds, so relied on alone it would only see the branch as it was before that
            # window. Raised by the GPT review of this branch.
            src_ok, src_note = self._head_is_the_committed_sha(src)
            if not src_ok:
                self.log.error(
                    "direct-push: HEAD is no longer the authorized source (%s) -- not rebasing",
                    src_note,
                )
                return push
            # CAPTURE THE FETCHED TIP AS AN OBJECT ID, FROM THE FETCH ITSELF. `FETCH_HEAD` is a
            # mutable file in the clone, and the pre-push reviewer's shell can rewrite it between
            # the fetch and the rebase: the replay then lands on a SUBSTITUTED parent whose
            # content nothing scanned (the scan covers only the replayed commit), and the push
            # carries it. Keying both the rebase input and the replayed-commit count on that same
            # name made them corroborate the substitution rather than catch it. Measured on a
            # throwaway repo: with `.git/FETCH_HEAD` rewritten to a prepared child,
            # `rev-list --count FETCH_HEAD..HEAD` still reported 1 while the truth against the
            # real tip was 2, the scanned range held only our own file, and a bare remote ACCEPTED
            # the push -- the foreign file landed. With the id taken from `--porcelain` stdout the
            # same attack replays onto the real tip and the foreign file is gone from the
            # published range. Raised by the GPT review of this branch.
            fetched = _git(["fetch", "--porcelain", fetch_url, dest], self.clone)
            if fetched.returncode != 0:
                return push
            base = _fetched_tip_oid(fetched.stdout)
            if not base:
                self.log.error(
                    "direct-push: the fetch did not report the tip's object id -- not rebasing"
                )
                return push
            # PIN THE COMMITTER FROM THE AUTHORIZED OBJECT, not from configuration. A replay
            # writes a NEW commit object, and git takes its committer identity from config
            # rather than from the commit being replayed -- while author and message carry
            # over. The reviewing agent's shell can write the repository's config (its git
            # denylist covers only `push` and `remote set-url`) and nothing masks a global
            # one, so a replay left to resolve identity itself publishes attribution that a
            # distrusted actor chose. `src` already holds the identity the pipeline committed
            # under, recorded before any agent ran, and it is an immutable object: reading the
            # answer off it needs no new state and no trust in any config file. Raised by the
            # GPT review of this branch.
            pin_name, pin_email = _committer_identity(src, self.clone)
            if not pin_name or not pin_email:
                self._restore_branch(dest)
                self.log.error(
                    "direct-push: cannot read the authorized commit's own identity -- not"
                    " rebasing"
                )
                return push
            # REPLAY THE RETAINED OBJECT ONTO THE CAPTURED BASE. `git rebase <base> <rev>` checks
            # out *rev* detached and replays it, so BOTH ends are immutable object ids and nothing
            # that rewrites a ref in between can substitute either one. There is no no-argument
            # form any more: *src* is required, so the branch is never the thing replayed.
            reb = _git(
                ["rebase", base, src],
                self.clone,
                config=("-c", f"user.name={pin_name}", "-c", f"user.email={pin_email}"),
            )
            if reb.returncode != 0:
                _git(["rebase", "--abort"], self.clone)
                self._restore_branch(dest)
                self.log.warning("direct-push: rebase onto %s conflicted — not pushing", dest)
                return push
            # The rebase REPLAYED the commit, so the object about to be published is a new one
            # that nothing has scanned: the caller's credential scan covered the PRE-rebase
            # object. Resolve the replacement to a full id, scan THAT, and push THAT -- so this
            # path holds the same invariant as the first push instead of trusting `HEAD`, which
            # is re-resolved by the push itself and can be moved in between by a process the
            # reviewer left behind. Raised by the GPT review of this branch.
            #
            # RESOLVED BEFORE `_reverify_head`, DELIBERATELY. That gate runs the
            # repository-under-improvement's OWN test suite, i.e. arbitrary code from the tree
            # being published, so resolving afterwards let a test teardown move HEAD and have
            # the moved commit captured, scanned and pushed as though the gate had measured it:
            # credentials still covered, but the build-verification invariant broken with no
            # recheck. Capturing first and asserting HEAD is still that object afterwards makes
            # the thing the gate measured and the thing published the same object. Raised by
            # the GPT review of this branch, which found the earlier ordering.
            rebased_id = (
                _git(["rev-list", "-1", "HEAD^{commit}"], self.clone).stdout or ""
            ).strip()
            verified = self._reverify_head()
            if self._retire_if_unsafe("post-rebase verification"):
                return None
            # Every refusal from here re-attaches the clone WITHOUT moving the branch: the
            # rebase left HEAD detached at the replay, and `_reset_provisional` rolls back with
            # `git reset --hard`, which on a detached HEAD would move the detachment and leave
            # the branch still carrying the provisional commit. Leaving the branch where it was
            # is also the conservative outcome -- it never points at a replay that failed a
            # check.
            if not verified:
                self._restore_branch(dest)
                return push  # rebased tree is unverified — return the original rejection
            # The emptiness refusal is deliberately HERE rather than at the capture above: the
            # capture has to precede the gate, but refusing there would preempt the retire and
            # re-verify checks and swallow the atomic clone retirement (`return None`), which is
            # a stronger outcome than a failed push. Same shape as `_direct_push`: resolve
            # early, carry the value forward, refuse in the original order.
            if not rebased_id:
                self.log.error("direct-push: cannot resolve the rebased commit -- not pushing")
                self._restore_branch(dest)
                return push
            # THE CAPTURED REPLAY MUST CARRY THE AUTHORIZED CHANGE. Everything below binds to
            # *rebased_id*, and *rebased_id* comes from ambient `HEAD` -- so a process that
            # moves HEAD in the window between the rebase returning and that read gets its own
            # commit measured, scanned, counted and published, with every check agreeing
            # because they all agree ABOUT THE SUBSTITUTE. The HEAD-equality check below cannot
            # catch it: it compares HEAD against the same captured id, so a substitution that
            # happened BEFORE the capture is corroborated rather than refused. Nothing in the
            # chain reached back to *src*, the object the caller authorized and already
            # scanned.
            #
            # Equality of ids is not available -- a replay is a new object by definition -- so
            # the binding is the RESULT: the tree a replay of *src* onto *base* must produce,
            # computed from those two immutable ids by git's own merge, against the tree the
            # captured commit actually carries. That closes the window by making the published
            # CONTENT provably the authorized content rather than by trying to make the window
            # empty; HEAD is the only handle the rebase leaves, so the window cannot be
            # removed, only made harmless.
            #
            # A TREE, NOT A PATCH IDENTITY, because a patch identity ignores hunk positions:
            # the same added and removed lines placed elsewhere in the file carry the same
            # identity and a different tree. Raised by the GPT review of this branch.
            #
            # FAIL-CLOSED, and the cost is a skipped publish, never a bad one: this cycle's
            # commit stays on the branch and the next cycle re-pushes it.
            want_tree = _expected_replay_tree(base, src, self.clone)
            have_tree = _commit_tree(rebased_id, self.clone)
            if not want_tree or not have_tree or want_tree != have_tree:
                self.log.error(
                    "direct-push: the rebased commit %s does not carry the tree a replay of the"
                    " authorized %s produces -- not pushing",
                    rebased_id[:12],
                    src[:12],
                )
                self._restore_branch(dest)
                return push
            want_attr = _commit_attribution(src, self.clone)
            have_attr = _commit_attribution(rebased_id, self.clone)
            if not want_attr or not have_attr or want_attr != have_attr:
                # THE PIN IS NOT THE GUARANTEE, this is. A `-c` override loses to the
                # ``GIT_COMMITTER_*`` environment, and author and message are carried over by
                # the replay rather than pinned at all, so the published object's attribution
                # is checked against the authorized one instead of assumed. Comparing objects
                # to each other keeps configuration out of the answer entirely.
                self.log.error(
                    "direct-push: the rebased commit %s does not carry the attribution of the"
                    " authorized %s -- not pushing",
                    rebased_id[:12],
                    src[:12],
                )
                self._restore_branch(dest)
                return push
            head_ok, head_note = self._head_is_the_committed_sha(rebased_id)
            if not head_ok:
                self.log.error(
                    "direct-push: HEAD moved while the build gate ran (%s) -- not pushing",
                    head_note,
                )
                self._restore_branch(dest)
                return push
            scan_ok, scan_note = self._revision_scans_clean(rebased_id)
            if not scan_ok:
                self.log.error(
                    "direct-push: the rebased commit %s did not scan clean (%s) -- not pushing",
                    rebased_id[:12],
                    scan_note,
                )
                self._restore_branch(dest)
                return push
            # THE REBASE MUST HAVE REPLAYED EXACTLY ONE COMMIT. If the remote already carries an
            # equivalent patch, `git rebase` drops ours as already-applied and leaves HEAD at
            # the remote tip -- which every check below would then bind to, consistently, and
            # the ledger would record an unrelated commit as the one this pipeline landed. A
            # count of 0 is that case; anything above 1 means the replay is not the single
            # commit this method is documented to handle. Raised by the GPT review.
            #
            # BOTH ENDPOINTS ARE CAPTURED IDS. The base is the tip the fetch reported, not
            # `FETCH_HEAD` -- that name is a mutable file, so a count keyed on it measures the
            # same substituted state the rebase would have replayed onto, and agrees with it.
            # The far end is *rebased_id*, not ambient `HEAD`: `HEAD` resolves to whatever the
            # working tree points at when this line runs, and the build gate above executes the
            # target repository's OWN test suite, so a teardown can move it in between. The
            # equality check above proves HEAD was still the replay a moment ago, which is a
            # check-then-use pair -- naming the id removes the dependency instead of timing it.
            # It also makes the range counted identical to the range PUBLISHED: the push sends
            # *rebased_id*, so `base..rebased_id` is exactly what this adds to the remote.
            # Raised by the GPT review of this branch, one step along from the same defect in
            # the rebase input.
            replayed = (
                _git(["rev-list", "--count", f"{base}..{rebased_id}"], self.clone).stdout or ""
            ).strip()
            if replayed != "1":
                self.log.error(
                    "direct-push: the rebase replayed %s commits, not 1 -- not pushing",
                    replayed or "an unreadable number of",
                )
                self._restore_branch(dest)
                return push
            # THE ONE PLACE THE BRANCH IS PROMOTED, and it is reached only after the replay has
            # passed re-verification, the HEAD-identity check and the credential scan. Doing it
            # any earlier would force the branch onto whatever HEAD happened to be if a later
            # check refused -- silently, with the branch looking healthy. Raised in review.
            # A FAILED restore ABORTS the push: publishing while the clone is detached or its
            # durable branch is stale is exactly what the restore exists to prevent, so its
            # status is checked rather than logged. Raised by the GPT review.
            if not self._restore_branch(dest, promote=rebased_id):
                self.log.error("direct-push: branch restore failed -- not pushing %s", dest)
                return push
            require_pinned(self.clone)
            self._pushed_object = rebased_id
            push = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.clone),
                    *_GIT_SAFE_CONFIG,
                    "push",
                    fetch_url,
                    f"{rebased_id}:refs/heads/{dest}",
                ],
                capture_output=True,
                **UTF8_TEXT,
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

    def _publishing_is_halted(self) -> bool:
        """Whether a failed provisional rollback has made this clone unsafe to publish from.

        A failed rollback leaves HEAD carrying a commit that was refused and never published.
        Anything that publishes afterwards sends that commit as an ANCESTOR while its own scan
        looks at one revision, so the refused content lands through a scanner that never saw it.
        The rollback IS the repair, so a rollback that cannot be trusted to have happened must
        not be followed by work that assumes it did -- and the answer is to stop, not to retry:
        a retry would be one more thing whose failure has to be handled.

        Checked at three places so the halt is UNCONDITIONAL rather than narrowed:

        * here, at the top of each winner-applying method, which is BEFORE the PR pipeline's
          `emit_*` -- that path reaches ``pr_recipe._push_fix_branch``, which pushes ``HEAD``
          and knows nothing about this latch, and it runs before the direct push does;
        * at :meth:`_direct_push`, the publish gate itself;
        * and through ``request_stop``, which the cycle loop reads, so no later cycle starts.

        One cycle can hold SEVERAL bug winners (``for prop, bug_res in bug_winners``), so a
        latch read only at cycle boundaries would let the next winner in the SAME cycle publish.
        Raised by the GPT review of this branch; the unconditional form was the conductor's
        requirement.
        """
        if not getattr(self, "_rollback_failed", False):
            return False
        self.log.error(
            "refusing to apply any further winner: a provisional rollback failed, so HEAD"
            " carries a commit that was refused and never published"
        )
        return True

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

        AUTHORIZATION (operator directive): the push is allowed when the fix has
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
                "Do NOT push. Do NOT open a pull request. Do NOT commit or amend anything:\n"
                "this is REPORT-ONLY. A commit here replaces the object the pipeline measured\n"
                "and reproduced, which the publish gate then refuses -- so a well-meant fix\n"
                "discards the verified change. Report the finding instead. The last line MUST\n"
                "be the verdict.\n\n"
                "End your reply with EXACTLY one line: `REVIEW: clean` if there are no open\n"
                "findings on the added or changed lines, else `REVIEW: <N> open`. If you could\n"
                "not actually review the diff, say `REVIEW: unavailable` rather than guessing —\n"
                "an unfounded `clean` is the one answer that defeats this gate."
            )
            res = runner.run(
                prompt,
                cwd=str(self.clone),
                # NO `Edit`. The prompt above is report-only and gives a reviewer no
                # sanctioned reason to modify the clone. What actually determines whether a
                # reviewer CAN modify it is the tool list, not the prose, so withholding the
                # capability is what matters here. Raised by the first-principles review of
                # this branch.
                #
                # This is least privilege, NOT the enforcement boundary: `Bash` remains (the
                # review has to run git to read the diff), and a shell can write files and
                # commit. That is precisely why the identity checks below exist rather than
                # relying on what the reviewer was told or granted.
                allowed_tools=["Bash", "Read", "Grep", "Glob"],
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
            # fail-closed only when BOTH are inconclusive (06_*.md F6/F10; operator
            # directive: "clean prepush_review + bb release or other clean autotest
            # should allow push to the remote").
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

    def _head_is_the_committed_sha(self, committed_id: str) -> tuple[bool, str]:
        """Is the clone's HEAD still the commit this pipeline committed? ``(ok, note)``.

        This gate holds one invariant: do not push what a worker did not commit.
        *committed_id* is the FULL object id of what :meth:`_finalize_winner_commit` /
        :meth:`_finalize_bug_winner_commit` produced -- the commit that was measured,
        reproduced and written to the ledger -- resolved by the caller BEFORE the review gate
        runs. Nothing holds the verified commit and the published one together, and the window
        between them is not empty: :meth:`_prepush_review_clean` runs an agent in THIS clone
        with ``Bash`` for up to 30 turns, and that runner's git denylist covers only ``push``
        and ``remote set-url``, so ``git commit --amend`` is permitted there. That prompt is
        report-only and grants no ``Edit``, but neither is an ENFORCEMENT BOUNDARY -- a prompt
        is an instruction to an agent that may be
        carrying an injected diff, and a shell can write files whatever the tool list says -- so
        a moved HEAD stays reachable and this check is what refuses it.

        THE ANCHOR IS A FULL OBJECT ID, which is what makes this comparison mean anything. A
        short sha is an ambiguous NAME: git reads a revision as a ref name before an
        abbreviated object id, and a prefix stops naming one object once a second object shares
        it. Either reading is arrangeable by whatever can write the repository, so a gate that
        re-resolved an abbreviation would compare two values the distrusted actor chose. The
        finalizers therefore return a validated forty-hex id and nothing downstream resolves by
        prefix; a short form is derived for DISPLAY only. Raised by the GPT review of this
        branch.

        Resolved with ``rev-list -1`` rather than ``rev-parse --verify`` because this method
        ALREADY asks a different question with ``rev-parse --verify --quiet HEAD~1`` -- "does
        a parent exist", a boolean that picks the credential scan's range. One verb carrying
        two unrelated questions is indistinguishable to a reader and to any caller keyed on
        the argv, and ``test_ai_spine_driver_coverage``'s git double is keyed on exactly that:
        its own docstring says prefixes are scripted "so a caller can pin
        ``rev-parse --verify`` separately from ``rev-parse HEAD``". One prefix, one call site.

        FAIL-CLOSED. A revision that does not resolve is the ABSENCE of the check, not a
        pass: an amended-away commit can be pruned, and "I could not look" must never read
        as "I looked and it was fine".
        """
        if not committed_id:
            return False, "cannot resolve the committed object -- refusing to publish"
        have = _git(["rev-list", "-1", "HEAD^{commit}"], self.clone)
        have_sha = (have.stdout or "").strip()
        if have.returncode != 0 or not have_sha:
            return False, "cannot resolve the clone's HEAD -- refusing to publish"
        if committed_id != have_sha:
            return False, (
                f"HEAD moved after the pipeline committed {committed_id[:12]}"
                f" (HEAD is now {have_sha[:12]}): the commit that would be published is"
                " not the one this run verified"
            )
        return True, f"HEAD is the committed revision {have_sha[:12]}"

    def _direct_push(self, *, fp: str, kind: str, target: str, sha: str) -> bool | None:
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
            normalize_branch,
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
        # A FAILED ROLLBACK IS TERMINAL FOR PUBLISHING. HEAD still carries a commit that was
        # refused and never published, so this winner's scan (`<rev>~1..<rev>`) reads one
        # revision while its push sends the whole ancestry -- publishing the refused content
        # through a scanner that never looked at it. Checked HERE and not only via the run's
        # stop flag because that flag is read at cycle boundaries, and a cycle can hold SEVERAL
        # bug winners (`for prop, bug_res in bug_winners`), each reaching this method. Raised by
        # the GPT review of this branch.
        if getattr(self, "_rollback_failed", False):
            reason = "a provisional rollback failed, so HEAD carries an unpublished commit"
            self.log.error("direct-push refused for %s: %s", target, reason)
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
        # THE FINALIZER HANDS BACK A FULL OBJECT ID, so nothing is resolved by prefix here. A
        # short sha is an ambiguous name -- git reads a revision as a ref name before an
        # abbreviated object id, and a prefix stops naming one object once a second object
        # shares it -- so a lookup keyed on one lets whatever can write the repository decide
        # which object this path anchors to. Validating the id the finalizer already produced
        # leaves no lookup to aim at. An empty or malformed value is carried forward and
        # refused below rather than raised, so a missing sha keeps its own existing message.
        # Raised by the GPT review of this branch.
        committed_id = ""
        if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
            committed_id = sha
        # PRE-PUSH REVIEW GATE (fail-closed): a direct-pushed commit gets no human review,
        # so the automated reviewer must clear it before it lands on the shared branch.
        clean, note = self._prepush_review_clean(target=target, base_ref=self.branch)
        if self._retire_if_unsafe("pre-push review"):
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=kind,
                    target=target,
                    status=L.STATUS_ERROR,
                    note="direct-push refused: repository safety changed after review",
                )
            )
            return None
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
        # Publish only what this pipeline committed. Placed HERE
        # deliberately -- AFTER the review gate, so a reviewer that edited and amended the
        # clone is caught, and BEFORE the credential scan and the push, so an unverified
        # commit is neither scanned as though it were the verified one nor published. NOT
        # wrapped around the push itself: `_push_with_rebase` rewrites HEAD on purpose and
        # re-verifies the replayed tree, so a check there would refuse that legitimate path.
        # Refusing returns False, the same disposition every other gate in this method uses:
        # the commit stays local and recoverable and the caller rolls the provisional back.
        head_ok, head_note = self._head_is_the_committed_sha(committed_id)
        if not head_ok:
            self.log.error("direct-push REFUSED for %s: %s", target, head_note)
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=kind,
                    target=target,
                    status=L.STATUS_ERROR,
                    note=f"direct-push refused: {head_note}"[:200],
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
        # records why, rather than publishing a silently-rewritten patch. The range mechanics
        # and the history behind them live in `_revision_scans_clean`.
        #
        # Scan the COMMITTED OBJECT BY ID, never through `HEAD`. Binding this to the symbolic
        # ref left a hole that no amount of re-checking closes: a background process can point
        # HEAD at a clean decoy for the duration of the `git diff` and restore it before any
        # later check, so the scanner reads the decoy, every check passes, and the real commit
        # is published having never been scanned. An id names one immutable object, so the
        # thing scanned here and the thing pushed below are the same object by construction
        # rather than by timing. `_push_with_rebase` calls the SAME helper on the replayed
        # object if it has to rebase, so neither publish path can go out unscanned.
        # Raised by the GPT review of this branch.
        scan_ok, scan_note = self._revision_scans_clean(committed_id)
        if not scan_ok:
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

        # A TAMPER DETECTOR, not the thing that binds the scan -- that is the object id used
        # above and below, and this check is deliberately not load-bearing for it. Relying on
        # a post-scan re-check to prove the scan had covered the published object would be
        # wrong: the re-check is itself a check-then-use pair, so a process that swapped HEAD
        # during the diff and restored it before this line defeats both. What this still
        # catches is worth keeping: HEAD
        # differing here means something wrote the clone after the review returned, and a
        # clone that is being written by an unknown actor is not one to publish from, even
        # when the object about to be published is provably the verified one.
        head_ok, head_note = self._head_is_the_committed_sha(committed_id)
        if not head_ok:
            self.log.error("direct-push REFUSED for %s after the scan: %s", target, head_note)
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=kind,
                    target=target,
                    status=L.STATUS_ERROR,
                    note=f"direct-push refused after the scan: {head_note}"[:200],
                )
            )
            return False

        # Publish the OBJECT ID, not ``HEAD``: an id cannot be repointed, so no process in
        # the clone can change what lands on the branch between here and the push itself.
        push = self._push_with_rebase(fetch_url, dest, target, src=committed_id)
        if push is None:
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=kind,
                    target=target,
                    status=L.STATUS_ERROR,
                    note="direct-push refused: repository retired after safety change",
                )
            )
            return None
        # Record the OBJECT THAT WAS SENT, which `_push_with_rebase` reports -- on the fast path
        # the retained id, on the retry path the replayed one. Re-reading `HEAD` here was wrong
        # for the same reason it is wrong everywhere else on this path: HEAD can move between
        # the push and the read, and the ledger would then name an unrelated commit for a change
        # that really did land. The HEAD read is GONE rather than kept as a fallback: `src` is
        # required now, so there is no caller for whom the sent object is unknown, and a fallback
        # that re-reads HEAD is the defect wearing a smaller hat. There is no fallback to the
        # caller's own snapshot either, because it is unreachable: the ONLY return in
        # `_push_with_rebase` that precedes `self._pushed_object = src` is the retire path's
        # `return None`, and that routes to this method's own `return None` above -- before this
        # line. Raised by the GPT review, then narrowed twice by the first-principles review.
        sent = str(getattr(self, "_pushed_object", "") or "")
        self.pushed_sha = sent
        if push.returncode != 0:
            # Redact BEFORE the bound (here and at every stderr slice below): git
            # echoes the authenticated remote URL on an auth failure, and slicing
            # first can cut the credential into a fragment no later pass matches.
            # Log lines use the companion-aware log redactor; the persisted ledger
            # note keeps the baseline redact-then-bound helper.
            self.log.error(
                "direct-push FAILED for %s: %s",
                target,
                redact_log_via_context(push.stderr or "")[:300],
            )
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=kind,
                    target=target,
                    status=L.STATUS_ERROR,
                    note=f"direct-push failed: {redact_via_context(push.stderr or '')[:150]}",
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
                redact_log_via_context(reset.stderr or "")[:200],
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
            # surrogateescape re-encodes the captured payload back to the exact
            # bytes git produced.
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        )
        if ap.returncode != 0:
            self.log.error(
                "winner diff did not apply: %s", redact_log_via_context(ap.stderr or "")[:200]
            )
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
                redact_log_via_context(commit.stderr or "")[:200],
            )
            self._discard_staged(f"a failed provisional commit for {winner.cand_id}")
            return False
        return True

    def _reset_provisional(self, pre_sha: str) -> bool:
        """Roll the AUTHORIZED branch back to ``pre_sha`` after a provisional commit that was
        not filed, so a fluke, duplicate or error never advances HEAD (06_*.md section 1.1).

        Returns whether the branch is back where it belongs. A FAILED rollback is not a log
        line: HEAD keeps the refused commit, the next winner commits on top of it, and that
        winner's single-revision scan cannot see the parent its own push would publish. So a
        failure latches ``_rollback_failed`` and stops the run, at this chokepoint rather than
        at the five call sites, because a caller that forgets to check is the whole defect.

        ONE COMMAND, and that is the whole point. ``git reset --hard`` acts on WHATEVER IS
        CHECKED OUT, and this runs after the pre-push reviewer has had a shell in the clone --
        whose denylist covers only ``push`` and ``remote set-url``, so ``git checkout`` is
        permitted there. Two ways that bites:

        * the reviewer checks out a DIFFERENT branch, and the rollback hard-resets that branch
          to a sha from an unrelated run, destroying its commits;
        * HEAD is DETACHED, and the reset moves the detachment while the branch keeps carrying
          the provisional commit -- a rollback that silently does not roll back.

        An earlier fix read ``rev-parse --abbrev-ref HEAD``, checked the branch out if it
        differed, then reset -- which was the same check-then-use pair one level up: a
        backgrounded ``setsid git checkout victim`` landing between the check and the reset put
        the reset back on the wrong branch. ``git checkout -f -B <branch> <pre_sha>`` is a
        single invocation that NAMES the branch, moves it to *pre_sha* and checks it out, so
        there is no window and no ambiguity about which ref moves. ``-f`` keeps the old
        ``reset --hard`` semantics of discarding the working tree. Fail closed on error: a
        provisional commit left behind is resolved by the next cycle's stage step, while
        touching a ref that was never this run's to touch is not recoverable. Raised across
        three rounds of the GPT review of this branch.
        """
        if not pre_sha:
            return True
        if getattr(self, "_repository_retired", False):
            # The clone was already renamed out from under us; there is nothing to roll back
            # and no path to roll it back on.
            return False
        branch = normalize_branch(self.branch)
        res = _git(["checkout", "-f", "-B", branch, pre_sha], self.clone)
        if res.returncode != 0:
            self.log.error(
                "could not roll back the provisional commit to %s on %s: %s -- halting: HEAD"
                " carries a commit that was refused and never published",
                pre_sha[:10],
                branch,
                redact_log_via_context((res.stderr or "").strip())[:160],
            )
            self._rollback_failed = True
            self.request_stop()
            self._quarantine_unrolled_clone(pre_sha)
            return False
        return True

    def _quarantine_unrolled_clone(self, pre_sha: str) -> None:
        """Rename the clone out of its canonical name after a rollback failure.

        THE GUARD HAS TO OUTLIVE THE PROCESS, because the thing it guards does. The in-memory
        latch stops this run, but the un-rolled-back commit is on DISK and the clone is REUSED:
        the next run starts with a clear latch on a clone that still carries the refused commit,
        commits the next winner on top of it, and publishes an ancestor its single-revision scan
        never looked at. A guard scoped to a process lifetime is checking something shorter-lived
        than the hazard. Raised by the GPT review of this branch; the conductor asked for
        quarantine over a persisted latch, because a persisted latch raises the question of when
        it clears and one that clears on the wrong event is worse than none.

        Retirement is the app's OWN primitive for exactly this, not a new mechanism:
        ``_retire_unsafe_clone`` renames the root directory aside, and its docstring already
        says it "prevents a later run from adopting a rejected provisional commit". Reuse in
        ``_setup_safe_clone`` hinges on the canonical ``<dest>/.git`` being a directory, so a
        retired clone is not reused -- the next run clones fresh. Nothing about how clones are
        located or named changes, and the bytes are preserved for diagnosis.

        Unconditional: unlike :meth:`_retire_if_unsafe`, this does not consult the isolation
        probe first. The trigger is the failed rollback itself, which is already known, so a
        probe verdict could only talk us out of quarantining a clone we know is poisoned.

        RETIREMENT ITSELF CAN FAIL, and the reason it fails is correlated with the reason the
        rollback failed rather than independent of it: a Windows process still holding handles
        inside the tree makes both the force-checkout and the rename fail from that one cause.
        Logging "MUST NOT be reused" is not a guard -- the next run does not read this log --
        so a marker is persisted at a masked crew-home leaf (``_mark_clone_quarantined``) and
        ``_setup_safe_clone`` refuses a marked clone. Reuse there attests git metadata and
        remotes and never inspects the branch tip, so without the marker the poisoned tree
        passes every existing check. The marker deliberately does NOT live beside the clone:
        the scratch tree is one the agent works in, so a marker there could be truncated
        through a pre-placed hardlink or simply deleted.

        THE MARKER IS WRITTEN FIRST, BEFORE THE RENAME IS ATTEMPTED. Ordering it after made
        the durable record depend on a path reached only once two things had already failed,
        and a transient failure of the marker write itself then left the clone reusable with
        nothing recording that it must not be -- fail-open at the exact moment the guard is
        needed. Written first, the record exists before anything is disturbed, and a rename
        that then SUCCEEDS makes it stale rather than wrong: the marker names a directory, so
        it stops reporting once that directory is gone and is pruned on the way past.

        THREE LEVERS, AND THE CANONICAL NAME MUST NOT SURVIVE ALL THREE. Recording a marker
        and renaming the tree aside fail from independent causes -- an unwritable marker root
        against a held file handle -- so either alone can be the one that works. When BOTH
        fail the clone is still sitting at the name the next run looks up with nothing saying
        it must not be, so the last lever is to remove the tree outright: a partially removed
        clone presents no ``.git`` directory, and ``_setup_safe_clone`` refuses a destination
        that exists and is not a git repository. It can fail too, from the same
        held handle that defeated the rename, and then the log is all that is left -- but the
        reuse check has one more independent reason to refuse by then, because a marker root
        that could not take the write cannot pass ``_quarantine_root``'s writability probe
        either, and that makes the guard fail closed for every clone. All three halves raised
        by the GPT review of this branch.
        """
        from kiro_crew.platform_compat import rmtree_force

        from ..backend.clone_setup import _mark_clone_quarantined, _retire_unsafe_clone

        marked = _mark_clone_quarantined(
            self.clone, f"provisional rollback to {pre_sha[:10]} failed"
        )
        if marked is None:
            self.log.error(
                "could not record a quarantine marker for %s before retiring it; a later run"
                " has nothing to read, so retirement is now the only durable guard",
                self.clone,
            )
        retained = _retire_unsafe_clone(self.clone)
        self._repository_retired = True
        if retained is None:
            self.log.error(
                "could not retire the clone after the failed rollback to %s; it is left unsafe"
                " in place and MUST NOT be reused (quarantine marker: %s)",
                pre_sha[:10],
                marked or "could not be written either",
            )
            if marked is None and not rmtree_force(self.clone):
                self.log.error(
                    "could not remove %s either; every guard against reusing it has failed and"
                    " it must be deleted by hand before another run starts",
                    self.clone,
                )
        else:
            self.log.error(
                "clone retired to %s after the failed rollback to %s, so no later run can adopt"
                " the commit that was refused",
                retained,
                pre_sha[:10],
            )
        self._progress(
            stage="rollback_failed",
            error="provisional rollback failed; clone quarantined so it is not reused",
            retained_clone=str(retained or ""),
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
            return _full_head_id(self.clone)
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
        return _full_head_id(self.clone)

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
        # cause ("bug fix diff did not apply: error: uv.lock: already exists").
        # --3way falls back to a blob-level 3-way merge, which reconciles drift and
        # absorbs an already-present file instead of aborting. We retry plain-apply first
        # (cheapest, no index churn) and only fall back to 3-way so behavior is unchanged
        # when the base matches.
        def _apply(extra: list[str]):
            return subprocess.run(
                ["git", "-C", str(self.clone), "apply", *extra],
                input=winner.diff,
                capture_output=True,
                # Same byte-exact payload round-trip as the plain apply above.
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
            )

        ap = _apply([])
        if ap.returncode != 0:
            self.log.info(
                "bug fix plain-apply failed (%s) — retrying with --3way",
                redact_log_via_context((ap.stderr or "").strip())[:120],
            )
            ap = _apply(["--3way"])
        if ap.returncode != 0:
            self.log.error(
                "bug fix diff did not apply (even --3way): %s",
                redact_log_via_context(ap.stderr or "")[:200],
            )
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
                redact_log_via_context(commit.stderr or "")[:200],
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
        return _full_head_id(self.clone)

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


if __name__ == "__main__":
    raise SystemExit(main())
