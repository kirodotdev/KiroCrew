"""Gate — the deterministic correctness check (Phase C) + mechanical allowlist (spine).

Phase C of the per-cycle workflow (02_architecture.md §1.2, §6.4; 10_roadmap M0
"gate — deterministic build + correctness check, boolean only"). Two mechanical,
non-model checks the agent cannot argue past:

  1. EDIT ALLOWLIST (pre-measure, mechanical): ``git diff --name-only base..cand``;
     auto-reject any candidate touching a path outside the profile's allowlist —
     "cheaper and more reliable than trusting the agent to self-police"
     (08_safety §4.3). Runs BEFORE the build so an off-limits edit never produces a
     number to be tempted by. The globs are the profile's; enforcement is spine.

  2. BUILD + CORRECTNESS (boolean only): the profile's ``build_gate`` runs the
     build/test command and returns green/red + the passing commit sha (recorded
     for Phase D's same-sha assertion). Noise during the build is irrelevant — only
     the boolean matters (02_arch §2.1 Phase C).

For the *bug* track the gate is RED/GREEN instead of A/B (M4;
05_improvement_loop_bugfix.md §2): the reproducing test fails on base (RED, twice —
flake check), passes on the fix (GREEN), and the full suite stays green (STAYGREEN),
preceded by the static-triage ladder (build → lint → collect, §3.2). The orchestration
is the spine's :class:`~.bug_gate.BugGate`, parameterized ONLY by the profile's
``bug_runner`` test-runner primitives (the gate is target-agnostic; the runner is the
profile's — M4 generalization note). The allowlist check applies to bug candidates too.

This module shells out to ``git`` and calls profile callables only. No target token.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .bug_gate import BugGate
from .contracts import (
    BUG_ERROR,
    BUG_FAILED_BUILD,
    BugGateResult,
    GateResult,
    Proposal,
    TargetProfile,
)
from .git_safety import GIT_SAFE_CONFIG, require_pinned

# Module logger — the gate is Phase C, where candidates are mechanically rejected before
# they ever reach measurement. The two highest-value signals for run analysis are the
# ALLOWLIST rejection (off-limits paths — a top effectiveness killer) and the perf
# build/test verdict (green/red + the gated sha). Greppable prefix: "gate:".
_log = logging.getLogger("auto_improvement.spine.gate")

#: Trusted git config prepended to every host-side git call over the agent-writable worktree —
#: identical to the driver's/commit's `_GIT_SAFE_CONFIG`. The gate stages and commits the
#: candidate's edit on the HOST (gateway user) in the very worktree the sandboxed agent wrote to,
#: so a repo-planted hook (`core.hooksPath`) or fsmonitor program would execute host-side, outside
#: the sandbox, on the next `git add`/`commit`. `-c` overrides on OUR argv beat the repo config.
#: Raised by the GPT review.
_GIT_SAFE_CONFIG = GIT_SAFE_CONFIG


def _git_argv(worktree: Path, *args: str) -> list[str]:
    """A hardened ``git -C <worktree> <safe-config> <args…>`` argv. One builder so no call site
    can forget the hook/fsmonitor overrides, and the attributes pin that unbinds
    repository-controlled filter/diff drivers is refreshed here too."""
    require_pinned(worktree)
    return ["git", "-C", str(worktree), *_GIT_SAFE_CONFIG, *args]


def _changed_paths(worktree: Path, base_sha: str) -> list[str]:
    # Stage first so NEW (untracked) files count — a bug fix's reproducing test is a new
    # file, and a bare ``git diff --name-only base`` omits untracked paths. We diff the
    # index (``--cached``) against base after ``git add -A`` so adds are included.
    subprocess.run(_git_argv(worktree, "add", "-A"), capture_output=True, text=True)
    r = subprocess.run(
        _git_argv(worktree, "diff", "--cached", "--name-only", base_sha),
        capture_output=True,
        text=True,
    )
    return [p for p in r.stdout.splitlines() if p.strip()]


def _changed_status_paths(worktree: Path, base_sha: str) -> list[tuple[str, str]]:
    """Like :func:`_changed_paths` but returns ``(status, path)`` pairs where status is
    the git change letter (``A`` added, ``M`` modified, ``D`` deleted, ``R`` renamed).
    Lets the allowlist distinguish an ADDED reproducing test (allowed on the bug track)
    from a MODIFIED existing test (gate-gaming, always forbidden). ``--diff-filter`` is
    not used; we parse ``--name-status`` so all change kinds are visible."""
    subprocess.run(_git_argv(worktree, "add", "-A"), capture_output=True, text=True)
    r = subprocess.run(
        _git_argv(worktree, "diff", "--cached", "--name-status", base_sha),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        # A git failure (bad/nonexistent base_sha, detached/corrupt repo) writes its
        # error to STDERR and leaves STDOUT empty. Treating that empty stdout as a clean
        # (zero-path) diff would make the allowlist fence PASS with nothing inspected —
        # an off-limits/garbage candidate would silently slip past. Fail CLOSED: surface
        # the error so the caller rejects the candidate instead of admitting it.
        raise RuntimeError(
            f"git diff failed (rc={r.returncode}) for base {base_sha!r}: "
            f"{(r.stderr or '').strip()[:200]}"
        )
    out: list[tuple[str, str]] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0][:1]  # 'R100' → 'R'; 'A'/'M'/'D' → itself
        # For renames/copies git emits "<old>\t<new>". The NEW path is what now exists,
        # but the OLD path (the file being moved/copied away) must ALSO be checked
        # against the allowlist — renaming an off-limits file (e.g. an existing test)
        # into an allowed path would otherwise bypass the off-limits / no-test-edit
        # fence. Report BOTH paths so neither escapes the mechanical check.
        if status in ("R", "C") and len(parts) >= 3:
            out.append((status, parts[1]))  # old path (the source of the rename/copy)
            out.append((status, parts[-1]))  # new path (what now exists)
        else:
            out.append((status, parts[-1]))
    return out


def _head_sha(worktree: Path) -> str:
    # The candidate's edit lives uncommitted in the worktree; for the same-sha
    # contract we record the base/HEAD sha the gate built against. We commit the
    # worktree edit so the measured artifact has a stable sha.
    subprocess.run(_git_argv(worktree, "add", "-A"), capture_output=True, text=True)
    c = subprocess.run(
        _git_argv(worktree, "commit", "-q", "-m", "candidate gate snapshot", "--allow-empty"),
        capture_output=True,
        text=True,
    )
    if c.returncode != 0:
        return ""
    return subprocess.run(
        _git_argv(worktree, "rev-parse", "HEAD"), capture_output=True, text=True
    ).stdout.strip()


class Gate:
    """Runs the mechanical allowlist check then the profile's build/correctness
    gate (perf) or the spine's deterministic RED/GREEN bug gate (bug track).

    Both tracks share the allowlist fence (the edit allowlist is identical on both —
    a bug fix is still mechanically confined to allowed paths). The PERF path returns
    a :class:`GateResult` (boolean + the gated sha for Phase D's same-sha assertion);
    the BUG path returns a :class:`BugGateResult` (RED ∧ GREEN ∧ STAYGREEN, the granular
    reason, and the booleans for the CR narrative). The driver routes on
    ``candidate.kind`` (05_*.md §2; 02_arch §1.2)."""

    def __init__(self, *, bug_gate: BugGate | None = None) -> None:
        # The bug gate is target-agnostic — it composes the profile's bug_runner
        # primitives. Injectable for tests; defaults to the standard doubled-RED gate.
        self.bug_gate = bug_gate or BugGate()

    def check_allowlist(
        self, *, profile: TargetProfile, proposal: Proposal, base_sha: str
    ) -> GateResult:
        """Mechanical pre-measure path check (08_safety §4.3). Auto-reject any
        off-limits edit BEFORE the build/measure. Applies to BOTH tracks — a bug
        fix is still confined to the allowlist (05_*.md §4.3 isolation is shared).

        An allowlist MAY expose ``allows_changes(status_paths)`` (status-aware: a list of
        ``(git_status, path)`` pairs) to distinguish an ADDED file from a MODIFIED one —
        the bug track needs this so a candidate may ADD its new reproducing test under
        ``test/`` (the §1.3 ``added_by_candidate`` payload) while still forbidding edits
        to EXISTING tests (gate-gaming). When the allowlist only exposes the plain
        ``allows(paths)`` (perf track, stubs), we fall back to the name-only check."""
        try:
            status_paths = _changed_status_paths(proposal.worktree, base_sha)
        except RuntimeError as exc:
            # A git failure must NOT be reported as a clean (empty) diff — that would
            # silently admit a candidate past the fence with zero paths inspected. Fail
            # CLOSED: reject the candidate and record why (08_safety §4.3 is a hard fence).
            cid = getattr(proposal, "cand_id", None) or getattr(
                getattr(proposal, "candidate", None), "target", "?"
            )
            _log.info("gate: allowlist REJECT (git error) | cand=%s err=%s", cid, exc)
            return GateResult(
                passed=False,
                detail=f"allowlist check failed (git error): {exc}",
                failing_tests=[],
            )
        changes_fn = getattr(profile.edit_allowlist, "allows_changes", None)
        if callable(changes_fn):
            ok, offending = changes_fn(status_paths)
        else:
            ok, offending = profile.edit_allowlist.allows([p for _s, p in status_paths])
        cid = getattr(proposal, "cand_id", None) or getattr(
            getattr(proposal, "candidate", None), "target", "?"
        )
        if not ok:
            # An allowlist rejection is mechanical and final — log the offending paths +
            # the candidate so a run's "off-limits" rejections (a top effectiveness killer)
            # are reconstructable from logs alone.
            _log.info(
                "gate: allowlist REJECT | cand=%s changed=%d offending=%s",
                cid,
                len(status_paths),
                offending[:10],
            )
            return GateResult(
                passed=False,
                detail="off-limits paths: " + ", ".join(offending[:10]),
                failing_tests=[],
            )
        _log.info("gate: allowlist OK | cand=%s changed=%d", cid, len(status_paths))
        return GateResult(passed=True, detail="allowlist ok")

    def run(self, *, profile: TargetProfile, proposal: Proposal, base_sha: str) -> GateResult:
        """Full Phase-C PERF gate: allowlist → build/test. Returns a boolean + the
        gated commit sha (empty sha => no measurable artifact). For the bug track the
        driver calls :meth:`run_bug` instead (it returns a :class:`BugGateResult`)."""
        allow = self.check_allowlist(profile=profile, proposal=proposal, base_sha=base_sha)
        if not allow.passed:
            return allow

        # Snapshot the worktree edit to a stable commit so Phase D can assert the
        # measured artifact is exactly what the gate built (02_arch §2.2).
        commit_sha = _head_sha(proposal.worktree)
        src = proposal.worktree / "src"
        res = profile.build_gate.build_and_test(worktree=proposal.worktree, src=src)
        res.commit_sha = commit_sha or res.commit_sha  # carry the gated sha forward
        cid = getattr(proposal, "cand_id", None) or getattr(
            getattr(proposal, "candidate", None), "target", "?"
        )
        _log.info(
            "gate: build/test %s | cand=%s sha=%s failing=%d detail=%s",
            "PASS" if getattr(res, "passed", False) else "FAIL",
            cid,
            (res.commit_sha or "")[:10],
            len(getattr(res, "failing_tests", []) or []),
            (getattr(res, "detail", "") or "")[:120],
        )
        return res

    def run_bug(
        self, *, profile: TargetProfile, proposal: Proposal, base_sha: str
    ) -> BugGateResult:
        """Bug-track Phase-C gate (M4): allowlist → static triage → RED/GREEN/STAYGREEN.

        Returns a :class:`BugGateResult` — a *boolean* state transition, NOT a measured
        delta (so no noise band/anchor/canary; §2.4). The allowlist runs first (a bug
        fix is still confined). The RED/GREEN orchestration is the spine's
        :class:`~.bug_gate.BugGate`, composing the profile's ``bug_runner`` primitives;
        a profile with no ``bug_runner`` cannot drive the bug track (returns BUG_ERROR).

        ``base_src`` is the unmodified base tree (the clone's src at ``base_sha``);
        ``cand_src`` is the candidate worktree's src with the full fix applied."""
        allow = self.check_allowlist(profile=profile, proposal=proposal, base_sha=base_sha)
        if not allow.passed:
            # An off-limits bug fix is a failed_gate (static-triage class), not a
            # RED/GREEN verify failure — surface it as the T0 build-class reason
            # (BUG_FAILED_BUILD maps to the ledger's failed_gate, §5.3).
            return BugGateResult(
                passed=False,
                reason=BUG_FAILED_BUILD,
                build_ok=False,
                detail="off-limits: " + allow.detail,
            )
        runner = getattr(profile, "bug_runner", None)
        if runner is None:
            return BugGateResult(
                passed=False,
                reason=BUG_ERROR,
                detail=f"profile {getattr(profile, 'id', '?')} has no bug_runner — "
                "cannot drive the bug track",
            )
        # The candidate worktree holds the FULL fix (test + source edit) — that is
        # ``cand_src`` for the GREEN/STAYGREEN runs. For the RED run we need the BASE
        # tree with ONLY the reproducing test applied (the bug still present), so the
        # test FAILS — proving it reproduces the defect. The raw clone src has no test
        # file, so we stage a test-only base tree: a fresh worktree at base_sha with the
        # candidate's NEW test file(s) overlaid (05_*.md §2.2 "apply only the test
        # portion of the diff at BASE"). Without this the RED run hits a collection
        # error (the test doesn't exist at base) and every bug candidate is rejected as
        # test_invalid — the reason no bug CR was ever produced.
        _head_sha(proposal.worktree)
        cand_src = proposal.worktree / "src"
        base_src = self._stage_test_only_base(proposal, base_sha)
        try:
            return self.bug_gate.run(
                runner=runner,
                candidate=proposal.candidate,
                base_src=base_src,
                cand_src=cand_src,
            )
        finally:
            # The staged RED-base snapshot is throwaway — remove it after the gate.
            # One tree was created per bug candidate per cycle and (pre-fix) leaked in
            # the worktree root for the life of the run. The fallback path returns the
            # worktree's own src (base_src == cand_src) — never delete that.
            if base_src != cand_src:

                shutil.rmtree(base_src.parent, ignore_errors=True)

    def _stage_test_only_base(self, proposal: Proposal, base_sha: str) -> Path:
        """Build the RED base tree: the base source (bug still present) + ONLY the
        reproducing test file(s) the candidate added. Returns the base tree's ``src`` dir.

        Strategy: snapshot the candidate worktree (which has test + fix), GIT-ISOLATE the
        snapshot (drop its copied ``.git`` — for a linked worktree that link shares the
        ORIGINAL worktree's git index, so running git inside the snapshot would mutate
        the candidate's staged state), then restore each non-test changed file to its
        base content via ``git show base_sha:<path>`` run in the ORIGINAL worktree —
        reverting the SOURCE fix while KEEPING the new test. The test then runs against
        the still-buggy source → it FAILS (true RED reproduction). Falls back to the
        worktree src on any error (the gate then reports test_invalid — safe + honest).

        TWO candidate shapes produce a valid RED base:
          1. test-file candidates (backend bug track): a NEW reproducing test file is added
             and the source fix is reverted — the test runs against still-buggy source.
          2. NO-test-file candidates (frontend a11y track): the "reproducing test" is a
             bugscan FINDING id, not a file — the candidate is a pure SOURCE edit. Reverting
             that source edit restores the finding on base (the bug reproduces). Without
             handling this case the old code returned ``wt/src`` (== cand_src), so the RED
             scan saw the ALREADY-FIXED tree, the finding was absent, and EVERY frontend
             candidate was rejected ``not_red`` (vacuous) — the reason the UI track filed
             zero CRs. So: stage a redbase whenever there is ANY non-test change to revert,
             regardless of whether a new test file is present."""
        wt = proposal.worktree
        try:
            changed = _changed_paths(wt, base_sha)

            def _is_test(p: str) -> bool:
                return (
                    "/test/" in f"/{p}"
                    or "/tests/" in f"/{p}"
                    or p.startswith(("test/", "tests/"))
                    or "/test_" in f"/{p}"
                    or Path(p).name.startswith("test_")
                )

            test_files = [p for p in changed if _is_test(p)]
            non_test = [p for p in changed if not _is_test(p)]
            # A RED base needs SOMETHING to revert: either a new test file to keep while
            # reverting the source (case 1), or — for a finding-as-test candidate with no
            # test file (case 2, frontend) — at least one non-test source change to revert.
            # Only if there is NOTHING to revert is RED truly unstageable (safe fallback).
            if not test_files and not non_test:
                return wt / "src"  # nothing changed to revert → can't stage a RED
            # Snapshot the worktree (test + fix) to a sibling dir, then revert the source
            # fix files to base inside that snapshot so only the test is "new".
            base_tree = wt.parent / f"{wt.name}__redbase"
            if base_tree.exists():
                shutil.rmtree(base_tree, ignore_errors=True)
            # symlinks=True: copy each symlink AS A LINK, never dereferenced. The worktree is
            # agent-editable and a candidate may (within the edit allowlist) plant a test
            # symlink pointing OUT of the tree — e.g. at `$HOME/.aws/credentials`. The default
            # (`symlinks=False`) would copy the TARGET's contents into `base_tree`, where the
            # repo's own `conftest.py`/tests run against the RED tree and could read and
            # exfiltrate the secret. Copying the link verbatim leaves a dangling/out-of-tree
            # link with no secret content behind it. Raised by the GPT review.
            shutil.copytree(wt, base_tree, symlinks=True)
            # Git-isolate the snapshot: a linked worktree's ``.git`` is a FILE pointing
            # at the shared git dir — git commands in the snapshot would read/write the
            # original worktree's index. The snapshot only needs to be a runnable tree
            # for pytest; it needs no git at all.
            gitlink = base_tree / ".git"
            if gitlink.is_dir():
                shutil.rmtree(gitlink, ignore_errors=True)
            else:
                gitlink.unlink(missing_ok=True)
            for p in non_test:
                # Base content from the ORIGINAL worktree's git (bug restored). A path
                # absent at base (a NEW source file the fix added) is removed instead.
                show = subprocess.run(
                    _git_argv(wt, "show", f"{base_sha}:{p}"), capture_output=True
                )
                dest = base_tree / p
                if show.returncode == 0:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(show.stdout)
                else:
                    dest.unlink(missing_ok=True)
            return base_tree / "src"
        except Exception:  # noqa: BLE001 — never crash the gate on staging
            return wt / "src"


def self_clone_src(proposal: Proposal) -> Path:
    """The base ``src`` tree the RED/GREEN gate compares against — the worktree's
    parent clone src (the unmodified base the candidate worktree was forked off).
    Spine-level default that keeps :class:`Gate` target-agnostic; a profile that
    needs a separate frozen base supplies it through its own ``bug_runner``."""
    # The worktree is <worktree_root>/<cand_id>; the clone src is the proposer's
    # clone/src. The proposer forks the worktree off the clone at base_sha, so the
    # clone's src at HEAD is the base for the RED run (test_only at base).
    return proposal.worktree.parent.parent / "src"
