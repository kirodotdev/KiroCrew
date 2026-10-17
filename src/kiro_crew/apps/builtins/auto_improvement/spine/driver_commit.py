"""Commit a kept winner on the working branch: stage, commit, amend, or roll back.

Both tracks apply, then draft, then commit (the spec's keep/draft ordering invariant): the
winner's diff is applied and committed with a placeholder message, so the draft push carries
the fix; once the pipeline has reproduced it the commit is amended with the attributable,
redacted message, and when nothing was filed the branch is reset to where the winner found
it. A rollback that fails latches ``_rollback_failed``, stops the run and quarantines the
clone, so nothing is published on top of a commit that was refused.

Each function here whose first parameter is ``self`` is a ``Driver`` method: the class binds
it by name, so it reads and writes the run's state on the instance, and a patch of the method
on the class or on an instance reaches every caller.

Instance fields written: ``_repository_retired``, ``_rollback_failed``.

Instance fields read: ``branch``, ``clone``, ``guardrail_tolerances``, ``log``, ``profile``.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from kiro_crew.platform.context import redact_log_via_context

from . import pr_description as D
from .contracts import Proposal
from .driver_git import _full_head_id, _git
from .push_policy import normalize_branch

if TYPE_CHECKING:
    from .contracts import BugGateResult
    from .driver import Driver


def _publishing_is_halted(self: Driver) -> bool:
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


def _discard_staged(self: Driver, why: str) -> None:
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


def _stage_winner(self: Driver, winner: Proposal) -> bool:
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


def _commit_winner_provisional(self: Driver, winner: Proposal) -> bool:
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


def _reset_provisional(self: Driver, pre_sha: str) -> bool:
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


def _quarantine_unrolled_clone(self: Driver, pre_sha: str) -> None:
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

    marked = _mark_clone_quarantined(self.clone, f"provisional rollback to {pre_sha[:10]} failed")
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
    self: Driver, winner: Proposal, *, verify, reproduce=None, cycle: int, diff_ref: str
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


def _stage_bug_winner(self: Driver, winner: Proposal) -> bool:
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


def _commit_bug_winner_provisional(self: Driver, winner: Proposal) -> bool:
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
    self: Driver, winner: Proposal, *, bug_res: BugGateResult, cycle: int, diff_ref: str
) -> str:
    """Apply the bug fix to the working branch (local commit only). The commit
    message states the defect + the RED→GREEN correctness narrative (not a perf
    metric — 05_*.md §4.2 / 06_*.md §3.1 contrast the bug narrative with the perf A/B)."""
    if winner.diff.strip():
        # AMEND the provisional commit: its tree is what the draft pushed; only the
        # message is replaced (staging + --3way happened in _stage_bug_winner).
        msg = D.bug_commit_message(proposal=winner, bug_res=bug_res, cycle=cycle, diff_ref=diff_ref)
        _git(
            ["commit", "-q", "--amend", "-m", self._redact_commit_message(msg)],
            self.clone,
        )
    return _full_head_id(self.clone)
