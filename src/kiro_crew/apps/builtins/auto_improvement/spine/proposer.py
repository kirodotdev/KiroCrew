"""Proposer — wide-cheap + deep-strong candidate generation in git worktrees (spine).

Phase B of the per-cycle workflow (02_architecture.md §1.2, §6.3). Generation is
embarrassingly parallel: each proposer edits its own git worktree branched off the
current best, so candidates never collide. The spine owns:

  - the fan-out SHAPE: N "wide" proposers (breadth) + 1 "deep" proposer (exploit a
    top-K near-miss). The two-tier model routing (cheap-wide / strong-deep) is the
    spine's default search policy; the *tiers themselves* are not a profile field
    (07_*.md §4 audit row 30 — proposer topology is target-agnostic).
  - worktree isolation + teardown: only the winner is ever applied to the branch,
    so "revert" == "don't apply" and discards are torn down (02_arch §2.2, §3.2).
  - the self-build requirement: each candidate self-builds in its worktree before
    being emitted (the profile's ``propose`` realizes the edit AND the build).

The actual EDIT (which file, what change) is the profile's ``propose`` callable;
the worktree machinery here is the same for every target.

NOTE: this module shells out to ``git worktree`` only. It contains no target token.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path

from . import ledger as L
from .contracts import TRACK_BUG, Candidate, Proposal, TargetProfile
from .git_safety import GIT_SAFE_CONFIG, require_pinned

# Module logger — Phase B is where the "no diff produced" outcome (a top effectiveness
# killer: the agent investigated and authored nothing) originates. Logging every
# proposal's outcome (diff produced / skipped-no-defect / propose-error / agent-authored)
# makes the discover→propose funnel analyzable from logs alone. Greppable prefix: "proposer:".
_log = logging.getLogger("auto_improvement.spine.proposer")

#: Trusted git config for host-side git over the agent-writable worktree — same as the sibling
#: helpers (driver/gate/commit/pr_recipe/agent_runner/pr_watchers). The proposer runs `add -A`
#: and `diff` on the HOST over the worktree the agent wrote to, and both consult (and can SPAWN)
#: `core.fsmonitor`; any git also runs `core.hooksPath` hooks. `-c` overrides on OUR argv beat
#: the repo config. Part of the D-120 hook-hardening class (Opus 5 review pressed on
#: completeness). `worktree`/`branch` subcommands take no untrusted tree but share the helper.
_GIT_SAFE_CONFIG = GIT_SAFE_CONFIG


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    require_pinned(cwd)
    return subprocess.run(
        ["git", "-C", str(cwd), *_GIT_SAFE_CONFIG] + args, capture_output=True, text=True
    )


class Proposer:
    """Drives Phase B: lay down worktrees, ask the profile to realize each edit,
    capture diffs, and hand survivors to the gate."""

    def __init__(
        self, *, clone: Path, worktree_root: Path, wide: int = 6, deep: int = 1, agent_runner=None
    ):
        self.clone = Path(clone)
        self.worktree_root = Path(worktree_root)
        self.wide = wide  # N cheap parallel proposers (breadth)
        self.deep = deep  # strong refiners exploiting top-K near-misses
        # OPTIONAL headless agent runner (claude -p). When wired, bug candidates whose
        # profile ``propose`` returns False (= "awaiting the agent edit", no fabricated
        # fix) are authored by the agent in their worktree. None keeps the spine offline:
        # bug candidates simply produce no diff and are skipped (honest, no fake fixes).
        self.agent_runner = agent_runner

    def _new_worktree(self, cand_id: str, base_sha: str) -> tuple[Path, str]:
        wt = self.worktree_root / cand_id
        branch = f"cand/{cand_id}"
        # idempotent cleanup of any orphan from a crashed prior cycle (02_arch §4.2 step 5)
        _git(["worktree", "remove", "--force", str(wt)], self.clone)
        _git(["branch", "-D", branch], self.clone)
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        r = _git(["worktree", "add", "-q", "-b", branch, str(wt), base_sha], self.clone)
        if r.returncode != 0:
            raise RuntimeError(f"worktree add failed for {cand_id}: {r.stderr.strip()}")
        return wt, branch

    def teardown(self, proposal: Proposal) -> None:
        """Drop a candidate worktree + branch. Called after the verdict for every
        non-winner (and for the winner once its diff is applied to the branch)."""
        _git(["worktree", "remove", "--force", str(proposal.worktree)], self.clone)
        _git(["branch", "-D", proposal.branch], self.clone)

    def _capture_diff(self, worktree: Path, base_sha: str) -> str:
        # FAIL-CLOSED-PIN the attributes first: both git calls below (`add -A`, `diff
        # --cached`) run host-side over the AGENT-WRITABLE worktree, and `_GIT_SAFE_CONFIG`
        # alone does NOT stop an attribute-bound `filter.<n>.clean`/`diff.<n>.textconv` — only
        # `.git/info/attributes` does, which `require_pinned` writes (proven by
        # `test_a_planted_clean_filter_does_not_run`). These two calls bypass `_git` (they set
        # `capture_output` and a custom pathspec), so they must pin explicitly like every
        # sibling helper. Missed here because the structural test only checked `proposer._git`.
        # Raised by the Opus review.
        require_pinned(worktree)
        # Stage everything first so NEW (untracked) files are included in the diff. A bug
        # fix's reproducing test is always a new file; ``git diff base_sha`` alone omits
        # untracked files, which silently dropped the test from every bug CR (and starved
        # the RED stage of the test to stage at base). ``git add -A`` then a diff of the
        # index against base captures adds + modifications + deletions.
        subprocess.run(
            ["git", "-C", str(worktree), *_GIT_SAFE_CONFIG, "add", "-A"],
            capture_output=True,
            text=True,
        )
        # EXCLUDE stray build/dependency artifacts the agent's tooling may drop in the
        # worktree (e.g. ``uv.lock`` from a ``uv`` invocation, ``.venv``, caches). They are
        # NEVER part of a legitimate code fix, and including them breaks the downstream
        # ``git apply`` onto the clone ("uv.lock: already exists in working directory" — the
        # observed committed=0 / failed-CR cause, 2026-06-17). Pathspec exclusions keep the
        # diff to real source changes (the fix + its reproducing test).
        return subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                *_GIT_SAFE_CONFIG,
                "diff",
                "--cached",
                base_sha,
                "--",
                ".",
                ":(exclude)uv.lock",
                ":(exclude)**/uv.lock",
                ":(exclude).venv/**",
                ":(exclude)**/__pycache__/**",
                ":(exclude)*.pyc",
                # Agent-tooling settings the session writes into its own worktree.
                # Observed live (2026-07-31): the first filed PR carried a spurious
                # ``.kiro/settings/cli.json`` hunk, which is noise in a reviewer's
                # diff and can collide on ``git apply`` onto a clone that has its own.
                ":(exclude).kiro/**",
                ":(exclude).claude/**",
                ":(exclude).pytest_cache/**",
                ":(exclude).ruff_cache/**",
                ":(exclude).mypy_cache/**",
            ],
            capture_output=True,
            text=True,
        ).stdout

    def propose_one(
        self, *, profile: TargetProfile, candidate: Candidate, base_sha: str, cycle: int, tier: str
    ) -> Proposal:
        """Lay down a worktree, ask the profile to realize the edit + self-build,
        and capture the diff. A proposer that produces no diff is marked skipped
        (the driver records it as ``no_candidate`` / drops it)."""
        # cand_id keys the worktree path AND branch, so it MUST be unique per
        # candidate within a cycle+tier. ``_short`` keeps only the target's basename
        # for readability, so two distinct loci sharing a basename (a/util.py::f vs
        # b/util.py::f) would otherwise collapse to the same path — and the idempotent
        # ``worktree remove --force`` below would then wipe the FIRST candidate's
        # still-live worktree. Append a short digest of the FULL target so distinct
        # loci never collide while the readable token is preserved.
        cand_id = f"c{cycle}_{tier}_{_short(candidate.target)}_{_disambig(candidate.target)}"
        wt, branch = self._new_worktree(cand_id, base_sha)
        try:
            produced = profile.propose(
                candidate=candidate, base_sha=base_sha, worktree=wt, tier=tier
            )
            # Bug candidates carry no mechanical seed — the profile's propose returns
            # False ("awaiting the agent edit"). When an agent runner is wired, author
            # the fix (reproducing test + minimal source edit) in the worktree now; the
            # spine's RED→GREEN gate then verifies the boolean transition (no trust in
            # the agent's word — the gate decides). Offline (no runner) it stays False,
            # so the candidate is skipped with no fabricated fix.
            if not produced and self.agent_runner is not None:
                from .agent_runner import author_bug_fix, author_perf_fix

                # Hand the agent the gate's own known-good test command so it doesn't
                # waste ~20 min rediscovering which interpreter has the deps. Optional:
                # only the concrete bug runner exposes ``agent_test_hint``; a stub/None
                # runner just leaves the hint empty (agent falls back to its own probing).
                bug_runner = getattr(profile, "bug_runner", None)
                hint_fn = getattr(bug_runner, "agent_test_hint", None)
                test_cmd_hint = hint_fn(wt) if callable(hint_fn) else None
                # Dispatch by TRACK. The perf branch used to be missing entirely, which
                # dead-ended the whole track: a profile with no mechanical seed returns
                # False from propose(), and with no agent escalation a perf candidate
                # produced no diff and was recorded no_defect — so the loop could never
                # keep or file a perf win. Both tracks now author through the model and
                # are judged by their own deterministic gate (RED→GREEN for a bug, A/B
                # against the noise band for perf).
                author = author_bug_fix if candidate.kind == TRACK_BUG else author_perf_fix
                produced = author(
                    self.agent_runner,
                    candidate=candidate,
                    worktree=wt,
                    test_cmd_hint=test_cmd_hint,
                )
        except Exception as e:  # noqa: BLE001
            _log.info(
                "proposer: ERROR | cand=%s target=%s tier=%s kind=%s err=%s",
                cand_id,
                candidate.target,
                tier,
                candidate.kind,
                f"{type(e).__name__}: {e}"[:160],
            )
            return Proposal(
                cand_id=cand_id,
                candidate=candidate,
                worktree=wt,
                branch=branch,
                description=candidate.signature,
                tier=tier,
                skipped=True,
                skip_reason=f"propose error: {type(e).__name__}: {e}",
                skip_status=L.STATUS_ERROR,  # a REAL failure — record as error
            )
        diff = self._capture_diff(wt, base_sha) if produced else ""
        has_diff = bool(produced and diff.strip())
        # Log the proposal outcome: a produced diff (with its size) advances to the gate;
        # "no diff produced" is the honest no-defect funnel exit that dominates rejections.
        _log.info(
            "proposer: %s | cand=%s target=%s tier=%s kind=%s diff_lines=%d",
            "DIFF" if has_diff else "SKIP(no_defect)",
            cand_id,
            candidate.target,
            tier,
            candidate.kind,
            len(diff.splitlines()) if has_diff else 0,
        )
        return Proposal(
            cand_id=cand_id,
            candidate=candidate,
            worktree=wt,
            branch=branch,
            description=candidate.hypothesis or candidate.signature,
            diff=diff,
            tier=tier,
            skipped=not has_diff,
            skip_reason="" if has_diff else "no diff produced",
            # No diff = the agent investigated and found nothing to fix (or no runner
            # was wired). That is an honest NO-DEFECT outcome, NOT an error — recording
            # it as ``error`` polluted the stats and (pre-cooldown) blocked re-discovery.
            skip_status="" if has_diff else L.STATUS_NO_DEFECT,
        )

    def fan_out(
        self,
        *,
        profile: TargetProfile,
        candidates: list[Candidate],
        base_sha: str,
        cycle: int,
        stop_check=None,
    ) -> list[Proposal]:
        """Generate proposals for a cycle: assign the first candidates to ``wide``
        cheap proposers and reserve the strongest top-K candidate(s) for ``deep``.

        Concurrency is conceptual here (each gets its own worktree; an executor can
        run them in parallel). Phase B is the only fan-out; measurement (Phase D) is
        strictly serial — "parallelize creation, serialize measurement" (02_arch §2).

        ``stop_check`` (optional, callable -> bool) is polled before each agent-bearing
        proposal so a clean-stop request aborts the fan-out instead of finishing every
        candidate (a real bug-track cycle can spawn N expensive ``claude -p`` calls,
        each minutes long; without this a stop would only land between cycles).
        """
        proposals: list[Proposal] = []
        wide_cands = candidates[: self.wide]
        # DISJOINT from wide, per this method's own contract ("reserve the strongest
        # top-K candidate(s) for deep"). Both slices previously started at index 0, so
        # with wide=1/deep=1 the two proposers authored THE SAME candidate: two full
        # agent passes, two worktrees and two gate ladders spent to answer one question,
        # and the second was then discarded as a same-cycle duplicate. Observed live —
        # every `c<N>_wide_*` had a matching `c<N>_deep_*` on the identical locus.
        deep_cands = candidates[self.wide : self.wide + self.deep]
        for c in wide_cands:
            if stop_check is not None and stop_check():
                break
            proposals.append(
                self.propose_one(
                    profile=profile, candidate=c, base_sha=base_sha, cycle=cycle, tier="wide"
                )
            )
        for c in deep_cands:
            if stop_check is not None and stop_check():
                break
            proposals.append(
                self.propose_one(
                    profile=profile, candidate=c, base_sha=base_sha, cycle=cycle, tier="deep"
                )
            )
        return proposals


def _short(target: str) -> str:
    """A filesystem-safe short token from a code locus, for worktree/branch names."""
    base = target.split("/")[-1].replace("::", "_")
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in base)
    return (safe or "cand")[:48]


def _disambig(target: str) -> str:
    """A short deterministic digest of the FULL target locus, so two distinct loci
    that share a basename (and thus the same ``_short`` token) still produce distinct
    worktree/branch names instead of colliding onto one path."""
    return hashlib.sha256(target.encode("utf-8")).hexdigest()[:8]
