"""Auto-improvement regressions: what a cycle commits, publishes and records.

The provisional commit carries no model text and fails closed, the winner is in the
tree before drafting, each bug PR carries only its own fix and records the base it was
tested on, the credential scan cannot self-diff, unresolved threads block publication,
a draft or a failed push leaves no commit or counter behind, publication survives a
ledger failure, and only the commit the pipeline made is published.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
from kiro_crew.apps.builtins.auto_improvement.tests.dogfood_learnings_helpers import (  # noqa: F401
    _cwd_is_the_scratch_dir,
)


class TestProvisionalCommitMessageCarriesNoModelText:
    """`wip(auto-improvement): staging {cand_id}` published unscanned model output.

    `cand_id` embeds the model-chosen `candidate.target`, and `_short` only restricts to
    alnum/`_`/`-` — exactly the character class of an AWS key id or a `ghp_` token. Measured:
    `src/m.py::glpat-IOSFODNN7EXAMPLE` produced `c1_wide_m_py_glpat-IOSFODNN7EXAMPLE_d469bc5b`,
    and `redact()` confirms that substring IS credential-shaped.

    This message is what the DRAFT PUSH publishes: the ordering is provisional commit ->
    draft/push -> redacted amend, so the amend cannot save it. A pushed commit message cannot
    be edited without rewriting history. Raised by the GPT review of this branch.
    """

    def test_a_credential_shaped_symbol_survives_into_cand_id(self) -> None:
        """The premise: sanitising to alnum/_/- does NOT remove a key-shaped token."""
        from kiro_crew.apps.builtins.auto_improvement.spine.proposer import _disambig, _short
        from kiro_crew.security import redact

        target = "src/m.py::glpat-IOSFODNN7EXAMPLE"
        cand_id = f"c1_wide_{_short(target)}_{_disambig(target)}"
        assert "glpat-IOSFODNN7EXAMPLE" in cand_id, "sanitisation removed it; premise moot"
        assert "REDACTED" in redact(cand_id), "the token is not credential-shaped"

    def test_both_provisional_commits_use_a_fixed_message(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        for meth in (Driver._commit_winner_provisional, Driver._commit_bug_winner_provisional):
            src = inspect.getsource(meth)
            # Match the ARGV, not prose: the comment above the fix names `winner.cand_id` to
            # explain the hazard (the same false-positive trap as the earlier `abs(observed)`
            # and `recipe.draft(` matches in this branch).
            assert (
                'f"wip(auto-improvement): staging {winner.cand_id}"' not in src
            ), f"{meth.__name__} publishes unscanned model text in a pushed commit message"
            assert '"wip(auto-improvement): staging a verified candidate"' in src


class TestEachBugPRCarriesOnlyItsOwnFix:
    """A bug cycle files one draft PR per locus from ONE shared clone.

    Leaving a FILED winner's provisional commit at HEAD made the next winner's branch start
    from it, so the second PR carried the first, unrelated fix. Measured on a real repo:
    winner B's `base...HEAD` range contained `FIX_A` as well as `FIX_B`. The not-filed path
    already rolled back; the success path did not.

    Review suggested capping the cycle at one bug winner instead — that discards verified,
    independently reproduced work for a bookkeeping problem. Resetting keeps every winner and
    keeps each PR to its own change. Raised by the GPT review of this branch.
    """

    def test_the_filed_path_also_rolls_the_provisional_commit_back(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        src = inspect.getsource(Driver._apply_bug_winner)
        # Assert the reset appears on the SUCCESS path, not merely that some threshold of
        # resets exists. A `count(...) >= 2` check passed before the fix too — there were
        # already resets on the direct-push-failed and not-filed paths — so it proved nothing.
        # Anchor on the `cr_filed` progress announcement, which only the filed path emits.
        filed_arm = src[
            src.index("cr_filed={") : src.index("        else:", src.index("cr_filed={"))
        ]
        assert "self._reset_provisional(pre_sha)" in filed_arm, (
            "a successfully filed bug winner leaves its commit at HEAD, so the NEXT winner's "
            "branch starts from it and its PR carries this fix too"
        )

    def test_a_chained_head_would_contaminate_the_next_branch(self, tmp_path) -> None:
        """The mechanism, on real git: a left-behind commit lands in the next PR's range."""
        clone = tmp_path / "c"
        clone.mkdir()
        self._git("init", "-q", "-b", "work", cwd=clone)
        self._git("config", "user.email", "a@b.c", cwd=clone)
        self._git("config", "user.name", "T", cwd=clone)
        (clone / "base.py").write_text("base = 1\n", encoding="utf-8")
        self._git("add", "-A", cwd=clone)
        self._git("commit", "-qm", "base", cwd=clone)
        base = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"],
            cwd=clone,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

        (clone / "a.py").write_text("FIX_A = 1\n", encoding="utf-8")
        self._git("add", "-A", cwd=clone)
        self._git("commit", "-qm", "winner A", cwd=clone)
        (clone / "b.py").write_text("FIX_B = 1\n", encoding="utf-8")
        self._git("add", "-A", cwd=clone)
        self._git("commit", "-qm", "winner B", cwd=clone)

        chained = subprocess.run(
            ["git", "-C", str(clone), "diff", f"{base}...HEAD"],
            cwd=clone,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
        assert "FIX_A" in chained and "FIX_B" in chained, "the chaining premise no longer holds"

        # After the reset the fix is gone from the range — what the driver now does.
        self._git("reset", "--hard", base, cwd=clone)
        (clone / "b.py").write_text("FIX_B = 1\n", encoding="utf-8")
        self._git("add", "-A", cwd=clone)
        self._git("commit", "-qm", "winner B alone", cwd=clone)
        isolated = subprocess.run(
            ["git", "-C", str(clone), "diff", f"{base}...HEAD"],
            cwd=clone,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
        assert "FIX_B" in isolated
        assert "FIX_A" not in isolated, "the reset did not isolate the second winner"

    @staticmethod
    def _git(*args: str, cwd) -> None:
        subprocess.run(["git", "-C", str(cwd), *args], cwd=cwd, check=True, capture_output=True)


class TestTheCredentialScanCannotSelfDiff:
    """A LOCAL `base_ref` made `base...HEAD` empty, so the scan saw nothing.

    `base_ref` is `config["branch"]`, which an operator may set to a plain local name. With
    `base_ref="work"` and HEAD *on* `work`, `git diff work...HEAD` is EMPTY, so
    `scan_content_for_secrets("")` reports clean and an agent-authored credential is pushed
    unscanned. Measured on a real bare repo: 0 bytes with the local name (credential
    invisible) vs 132 bytes with `origin/work` (caught).

    Same self-diffing failure already fixed in `Driver._direct_push`; the recipe had its own
    copy. Raised by the GPT review of this branch.
    """

    @staticmethod
    def _repo(tmp_path: Path) -> tuple[Path, Path]:
        remote = tmp_path / "r.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], cwd=remote.parent, check=True)
        clone = tmp_path / "c"
        subprocess.run(
            ["git", "clone", "-q", str(remote), str(clone)],
            cwd=clone.parent,
            check=True,
            capture_output=True,
        )
        for k, v in (("user.email", "a@b.c"), ("user.name", "T")):
            subprocess.run(["git", "-C", str(clone), "config", k, v], cwd=clone, check=True)
        (clone / "m.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(clone), "add", "-A"], cwd=clone, check=True)
        subprocess.run(["git", "-C", str(clone), "commit", "-qm", "base"], cwd=clone, check=True)
        subprocess.run(["git", "-C", str(clone), "branch", "-M", "work"], cwd=clone, check=True)
        subprocess.run(
            ["git", "-C", str(clone), "push", "-q", "origin", "work"], cwd=clone, check=True
        )
        # The winner commit carries a credential.
        (clone / "m.py").write_text("x = 1\nAWS_KEY = 'glpat-IOSFODNN7EXAMPLE'\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(clone), "commit", "-qam", "fix"], cwd=clone, check=True)
        return remote, clone

    def _recipe(self, tmp_path: Path, base_ref: str):
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.pr_recipe import (
            GitHubPRRecipe,
        )

        remote, clone = self._repo(tmp_path)
        return GitHubPRRecipe(
            user="u",
            clone_path=clone,
            pr_queue_dir=tmp_path / "q",
            base_ref=base_ref,
            fetch_url=str(remote),
        )

    def test_a_local_base_name_still_scans_the_real_range(self, tmp_path) -> None:
        recipe = self._recipe(tmp_path, "work")
        assert recipe._scannable_base() == "origin/work", "the local name was not resolved"
        ok, note = recipe._scan_pushable_content()
        assert ok is False, "the credential was invisible to the scan"
        assert "credential" in note
        assert "glpat-IOSFODNN7EXAMPLE" not in note, "the note quoted the secret"

    def test_an_explicit_remote_base_is_unchanged(self, tmp_path) -> None:
        recipe = self._recipe(tmp_path, "origin/work")
        assert recipe._scannable_base() == "origin/work"
        assert recipe._scan_pushable_content()[0] is False

    def test_an_unresolvable_base_refuses(self, tmp_path) -> None:
        """Refusing beats falling back to the narrower single-commit scan."""
        recipe = self._recipe(tmp_path, "no-such-branch")
        assert recipe._scannable_base() is None
        ok, note = recipe._scan_pushable_content()
        assert ok is False and "could not resolve the base" in note


class TestProvisionalCommitFailsClosed:
    """The provisional commit ignored `git commit`'s return code and always returned True.

    A failed commit (rejecting hook, gpg failure, empty index) then left HEAD on the
    PREVIOUS commit while the method reported success, so the pipeline drafted/pushed a
    HEAD that did not contain the fix — or carried a prior cycle's commit. Both the perf
    and bug provisional paths must fail closed. Raised by the GPT review of this branch.
    """

    @staticmethod
    def _git(*args: str, cwd) -> None:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(
            ["git", "-C", str(cwd), *args], cwd=cwd, check=True, capture_output=True, env=env
        )

    def _clone_on_a_branch(self, tmp_path: Path) -> Path:
        clone = tmp_path / "clone"
        clone.mkdir()
        self._git("init", "-q", "-b", "main", cwd=clone)
        (clone / "f.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "-A", cwd=clone)
        self._git("commit", "-qm", "base", cwd=clone)
        return clone

    def test_a_failing_commit_returns_false_not_true(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as drv_mod
        from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
            TRACK_PERF,
            Candidate,
            Proposal,
        )
        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        clone = self._clone_on_a_branch(tmp_path)

        drv = Driver.__new__(Driver)
        drv.clone = clone
        drv.branch = "main"
        drv.log = logging.getLogger("test")

        winner = Proposal(
            cand_id="c1",
            candidate=Candidate(kind=TRACK_PERF, target="f.txt::x"),
            worktree=clone,
            branch="main",
            description="",
            diff="diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-base\n+fix\n",
        )
        head_before = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"],
            cwd=clone,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

        # Force ONLY the `git commit` to fail, letting real staging/reset run. A pre-commit
        # hook cannot do this: D-120 hardens every host-side git with
        # `-c core.hooksPath=<devnull>` so a repo-controlled hook never executes host-side, and
        # making `.git/objects` read-only would also break the `git add` that must succeed
        # first. Wrapping the real `_git` and failing the `commit` subcommand is the
        # hook-independent equivalent of a rejecting hook, and exercises the same return-code
        # check.
        real_git = drv_mod._git

        def _git_fail_commit(args, cwd):
            if args and args[0] == "commit":
                return subprocess.CompletedProcess(args, 1, "", "simulated commit rejection")
            return real_git(args, cwd)

        monkeypatch.setattr(drv_mod, "_git", _git_fail_commit)

        ok = drv._commit_winner_provisional(winner)
        assert ok is False, "a rejected commit reported success"
        head_after = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"],
            cwd=clone,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        assert head_after == head_before, "HEAD moved despite the commit failing"

    def test_a_rejected_commit_leaves_no_diff_staged_for_the_next_candidate(
        self, tmp_path, monkeypatch
    ) -> None:
        """The REJECTED diff must not be inherited by whoever commits next.

        Measured on a real repo before fixing: with a rejecting `pre-commit` hook,
        candidate A's diff stayed staged (`M  f.txt`) and candidate B's later commit —
        which stages with `add -A` — contained A's rejected, never-verified change
        alongside B's own file. Publishing unmeasured code is exactly what this pipeline
        exists to prevent. Raised by the GPT review of this branch.
        """
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as drv_mod
        from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
            TRACK_PERF,
            Candidate,
            Proposal,
        )
        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        clone = self._clone_on_a_branch(tmp_path)

        drv = Driver.__new__(Driver)
        drv.clone = clone
        drv.branch = "main"
        drv.log = logging.getLogger("test")

        # Candidate A both EDITS a tracked file and ADDS a new one: `reset --hard` alone
        # would leave the added file untracked on disk, where the next `add -A` re-stages it.
        winner = Proposal(
            cand_id="a1",
            candidate=Candidate(kind=TRACK_PERF, target="f.txt::x"),
            worktree=clone,
            branch="main",
            description="",
            diff=(
                "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n"
                "@@ -1 +1 @@\n-base\n+REJECTED_A\n"
                "diff --git a/added.txt b/added.txt\nnew file mode 100644\n"
                "--- /dev/null\n+++ b/added.txt\n@@ -0,0 +1 @@\n+REJECTED_A\n"
            ),
        )
        # Fail ONLY `git commit`, letting the real `apply`/`add -A`/`reset --hard` run so the
        # staged-diff cleanup this test is about is genuinely exercised. A pre-commit hook can
        # force this: D-120 hardens host-side git with `-c core.hooksPath=<devnull>`,
        # so a repo hook is inert. Wrapping `_git` to reject the `commit` subcommand is the
        # hook-independent equivalent.
        real_git = drv_mod._git

        def _git_fail_commit(args, cwd):
            if args and args[0] == "commit":
                return subprocess.CompletedProcess(args, 1, "", "simulated commit rejection")
            return real_git(args, cwd)

        monkeypatch.setattr(drv_mod, "_git", _git_fail_commit)

        assert drv._commit_winner_provisional(winner) is False

        dirty = subprocess.run(
            ["git", "-C", str(clone), "status", "--porcelain"],
            cwd=clone,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        assert dirty == "", f"the rejected diff was left behind: {dirty!r}"
        assert not (clone / "added.txt").exists(), "a file the rejected patch created survived"
        assert (clone / "f.txt").read_text(encoding="utf-8") == "base\n"

    def test_both_provisional_paths_discard_on_failure(self) -> None:
        """Structural: the bug twin shares the perf path's cleanup (same leak, same fix)."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        for meth in (Driver._commit_winner_provisional, Driver._commit_bug_winner_provisional):
            src = inspect.getsource(meth)
            assert "_discard_staged" in src, f"{meth.__name__} leaks the rejected diff"


class TestWinnerIsInTheTreeBeforeDrafting:
    """``pr_recipe._push_fix_branch`` pushes the SHARED CLONE's ``HEAD``, so the winner has
    to be in that tree before the pipeline drafts.

    Raised by review of this branch and traced end-to-end before fixing: the queue copy
    carries ``winner.diff`` (correct), ``gated_commit_sha`` feeds the reproduce MEASUREMENT
    rather than the draft, and ``gate_res.commit_sha`` is the throwaway WORKTREE's head —
    none of them put the fix in the shared clone. Drafting first therefore pushed a branch
    without the fix, or one carrying a previous cycle's commit.

    Committing earlier instead would have been the wrong fix: the commit MESSAGE needs
    ``outcome.reproduce``, which only the pipeline produces, so that ordering would have
    silently degraded every kept-commit message to echoing VERIFY. Hence
    apply → draft → commit, which this pins.
    """

    def test_staging_is_split_out_of_committing(self) -> None:
        """Structural: both tracks must have a stage step the draft path can call."""
        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        for name in (
            "_stage_winner",
            "_stage_bug_winner",
            "_commit_winner_provisional",
            "_commit_bug_winner_provisional",
            "_finalize_winner_commit",
            "_finalize_bug_winner_commit",
            "_reset_provisional",
        ):
            assert hasattr(Driver, name), f"Driver.{name} missing"

    def test_both_emit_paths_stage_before_drafting(self) -> None:
        """The ordering itself: in each track the stage call precedes the emit call."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        for src_fn, stage, emit in (
            (Driver._apply_verdict, "_commit_winner_provisional(", "emit_perf("),
            (Driver._apply_bug_winner, "_commit_bug_winner_provisional(", "emit_bug("),
        ):
            src = inspect.getsource(src_fn)
            assert stage in src, f"{src_fn.__name__} never commits the winner before drafting"
            assert emit in src, f"{src_fn.__name__} never emits"
            assert src.index(stage) < src.index(
                emit
            ), f"{src_fn.__name__} drafts before the winner is in the tree"

    def test_a_diff_that_does_not_apply_never_reaches_the_pipeline(self, tmp_path) -> None:
        """Bail before the expensive reproduce A/B, not after."""
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        subprocess.run(["git", "init", "-q", str(tmp_path)], cwd=tmp_path, check=True)
        for k, v in (("user.email", "t@e"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(tmp_path), "config", k, v], cwd=tmp_path, check=True)
        (tmp_path / "a.txt").write_text("x")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-qm", "init"], cwd=tmp_path, check=True
        )

        drv = Driver.__new__(Driver)  # no full wiring needed for the staging step
        drv.clone = tmp_path
        drv.branch = "HEAD"
        drv.log = logging.getLogger("test")

        # A REAL Proposal, not a stub: the staging methods are typed for it, and a
        # duck-typed stand-in would only be caught later by mypy.
        from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
            TRACK_BUG,
            Candidate,
            Proposal,
        )

        winner = Proposal(
            cand_id="c1_w_nope",
            candidate=Candidate(kind=TRACK_BUG, target="nope.txt::f"),
            worktree=tmp_path,
            branch="HEAD",
            description="",
            diff=(
                "diff --git a/nope.txt b/nope.txt\n"
                "--- a/nope.txt\n+++ b/nope.txt\n@@ -1 +1 @@\n-a\n+b\n"
            ),
        )
        assert drv._stage_winner(winner) is False
        assert drv._stage_bug_winner(winner) is False

    def test_staging_stays_on_the_local_branch_across_cycles(self, tmp_path) -> None:
        """`self.branch` is the CONFIG form (``origin/main``), and ``git checkout
        origin/main`` DETACHES HEAD onto the remote-tracking ref — so a commit made in one
        cycle is orphaned and the next cycle's checkout throws it away. Measured against a
        bare repo: after a second checkout the prior winner is not an ancestor of HEAD.
        The stage step checks out ``normalize_branch(self.branch)`` (the local branch
        ``runner`` already created), so cycle N+1 builds ON cycle N.
        """
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
            TRACK_BUG,
            Candidate,
            Proposal,
        )
        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }

        def git(*args: str, cwd) -> None:
            subprocess.run(
                ["git", "-C", str(cwd), *args], cwd=cwd, check=True, capture_output=True, env=env
            )

        # A clone with a LOCAL `main` tracking `origin/main` — exactly the state
        # `clone_setup.checkout_branch` leaves before the driver runs.
        bare = tmp_path / "up.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], cwd=bare.parent, check=True)
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(bare), str(clone)],
            cwd=clone.parent,
            check=True,
            capture_output=True,
        )
        (clone / "f.txt").write_text("base\n", encoding="utf-8")
        git("add", "-A", cwd=clone)
        git("commit", "-qm", "base", cwd=clone)
        git("branch", "-M", "main", cwd=clone)
        git("push", "-q", "origin", "main", cwd=clone)

        drv = Driver.__new__(Driver)
        drv.clone = clone
        drv.branch = "origin/main"  # the config form — the whole point of the bug
        drv.log = logging.getLogger("test")

        def _winner(n: int, diff: str) -> Proposal:
            return Proposal(
                cand_id=f"c{n}",
                candidate=Candidate(kind=TRACK_BUG, target="f.txt::x"),
                worktree=clone,
                branch="origin/main",
                description="",
                diff=diff,
            )

        # Cycle 1: stage a winner (base -> cycle1) and commit it on the branch.
        cycle1_diff = (
            "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-base\n+cycle1\n"
        )
        assert drv._stage_winner(_winner(1, cycle1_diff)) is True
        git("commit", "-qm", "cycle1 winner", cwd=clone)
        c1 = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"],
            cwd=clone,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

        # Cycle 2: the next stage checks out the branch again. An empty-diff winner
        # isolates the CHECKOUT — which is where the detach bug lived — from diff-apply
        # mechanics: if the checkout detaches, c1 is orphaned regardless of the diff.
        assert drv._stage_winner(_winner(2, "")) is True

        head_ref = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=clone,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        assert (
            head_ref == "main"
        ), f"HEAD detached to {head_ref!r} — cycle-2 checkout orphaned the branch"
        # The load-bearing assertion: cycle 1's commit is still on the branch.
        ancestor = subprocess.run(
            ["git", "-C", str(clone), "merge-base", "--is-ancestor", c1, "HEAD"], cwd=clone
        ).returncode
        assert ancestor == 0, "cycle-1 kept commit was discarded by cycle-2's checkout"


class TestASuccessfulManualDraftLeavesNoCommitBehind:
    """The manual draft route reset the clone on every FAILURE path but not on success.

    `GitHubPRRecipe.draft` publishes with `git push HEAD:refs/heads/<generated>`, which never
    moves the local branch — so after a successful draft the checked-out branch still carries
    the candidate commit. Measured on a real bare repo: after drafting finding-1 and then
    finding-2, finding-2's pushed branch contained BOTH commits and its diff touched
    finding-1's file as well as its own.

    A second manual draft happens to rescue itself, because `materialize_queued_diff` does
    `checkout -B <branch> <freshly-fetched-base>`. The path that does NOT recover is a later
    RUN: `clone_setup.checkout_branch` early-returns "already on <branch>" when HEAD is
    already there, without resetting, so the run adopts the leftover commit as its baseline
    and every subsequent measurement is taken against a tree that silently contains a filed
    fix. That is D-70's defect in the operator-triggered route, and unlike D-71's perf case
    there is no cumulative-measurement argument for keeping it: a manual draft is one
    discrete "publish this queued finding" action, not an evolutionary loop.

    Raised by the GPT review.
    """

    def test_the_success_path_rolls_back_like_every_failure_path(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        src = inspect.getsource(routes_mod._handle_draft_pr)
        # Anchor on the SUCCESS arm specifically. A bare count of `_rollback()` calls would
        # pass on the pre-fix code, which already had three of them on failure paths.
        marker = "ledger_admin_record(fp, ref)"
        assert marker in src, "the draft success arm changed shape"
        after_record = src[src.index(marker) + len(marker) :]
        # The reset moved into `finally` (D-91), which is STRICTER than the original
        # success-arm placement this test was written against: it now also survives a raising
        # ledger append. Assert on the `finally`, since scanning up to an `else:` would look
        # for a branch that does not exist.
        success_arm = after_record[: after_record.index("return {")]
        assert "finally:" in success_arm, "the reset is no longer unconditional"
        assert "_rollback()" in success_arm, (
            "a successful draft leaves its commit on the checked-out branch; the next RUN "
            "then adopts it as the baseline (`checkout_branch` returns early when HEAD is "
            "already on the branch, without resetting)"
        )

    def test_the_ledger_row_is_written_before_the_reset(self) -> None:
        """Ordering matters: the reset must not be able to lose the reference. The ledger row
        is the only durable record that the PR exists, so it is written first."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        src = inspect.getsource(routes_mod._handle_draft_pr)
        assert src.index("ledger_admin_record(fp, ref)") < src.index(
            "_rollback()", src.index("ledger_admin_record(fp, ref)")
        ), "the reset runs before the pull-request reference is recorded"


class TestPublicationSurvivesALedgerFailure:
    """A pull request is IRREVERSIBLE — it exists on GitHub the moment `draft()` returns. Both
    publish paths then appended a ledger row with nothing catching a failure, so a full disk
    (or any OSError) after publication turned a SUCCESSFUL publish into a raised exception:

      * `pr_pipeline`: the raise propagates out of the cycle, so the run reports an error and
        the PR it just opened is recorded nowhere — the next cycle re-discovers the same locus
        and files a DUPLICATE, because the ledger is the only dedup store.
      * `routes` (manual draft): worse, because D-79 deliberately put the ledger write BEFORE
        `_rollback()` so a reset could never run in the row's place. A raising ledger therefore
        skips the rollback too, leaving the commit on the checked-out branch — the exact defect
        D-79 fixed, reachable again through a different door.

    So the ledger write is now best-effort AFTER an irreversible action, and the manual path's
    rollback moved into `finally`. Losing a ledger row is bad; losing it AND the response AND
    the reset is worse. Raised by the GPT review.
    """

    def test_the_manual_draft_still_resets_when_the_ledger_raises(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        src = inspect.getsource(routes_mod._handle_draft_pr)
        marker = "ledger_admin_record(fp, ref)"
        assert marker in src, "the draft success arm changed shape"
        # The rollback must not be reachable only on the ledger's success path.
        after = src[src.index(marker) :]
        assert "finally:" in after or "except Exception" in after, (
            "a raising ledger append skips `_rollback()`, stranding the commit on the branch "
            "— the D-79 defect through a different door"
        )

    def test_the_pipeline_keeps_the_filed_outcome_when_the_ledger_raises(self) -> None:
        """Behavioral: the PR reference must survive, because the PR itself does."""
        from typing import Any, cast

        from kiro_crew.apps.builtins.auto_improvement.spine import ledger as L
        from kiro_crew.apps.builtins.auto_improvement.spine import pr_pipeline as pp

        class _Boom:
            def record(self, _entry):
                raise OSError("No space left on device")

            def known(self, *_a, **_k):
                return False

        pipe = pp.CrPipeline.__new__(pp.CrPipeline)
        # A raising stand-in for the real Ledger; `cast` because the point is the
        # failure behavior, not protocol conformance.
        pipe.ledger = cast(Any, _Boom())
        pipe.log = logging.getLogger("test")
        outcome = pipe._record_filed(
            fp="fp1", kind="bug", target="src/m.py::f", cr="https://github.com/o/r/pull/1", note="n"
        )
        assert (
            outcome.status == L.STATUS_FILED
        ), "a ledger failure erased a pull request that already exists on GitHub"
        assert outcome.cr == "https://github.com/o/r/pull/1", "the PR reference was lost"
        assert outcome.filed is True


class TestAFailedBugPushDoesNotCorruptTheKeptCounter:
    """`_apply_bug_winner`'s F10 direct-commit path incremented `stats.kept` ONLY inside the
    success arm (after a landed push), but the push-failed `else` still ran `stats.kept -= 1`.

    That decrement was copied from the perf twin, where it IS correct — the perf path increments
    `kept` EAGERLY on keep (before the push), so a refused push must reverse it. The bug path has
    no such eager increment, so the decrement subtracts from a counter this path never added to,
    driving `stats.kept` negative or undercounted in the `/run` result. The `_reset_provisional`
    rollback beside it is the load-bearing part and stays; only the bookkeeping was wrong.
    Raised by the GPT review.
    """

    def test_the_push_failed_arm_does_not_decrement_kept(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        src = inspect.getsource(Driver._apply_bug_winner)
        # The failed-push arm must still roll back the provisional commit …
        assert "_reset_provisional(pre_sha)" in src, (
            "the rollback on a refused bug push was removed — a commit left at HEAD leaks into "
            "the next winner's push range"
        )
        # … but must NOT decrement kept anywhere in this method: it is only ever incremented
        # inside the two success arms here, never eagerly, so a decrement is always unbalanced.
        assert "stats.kept -= 1" not in src, (
            "the bug path decrements `stats.kept` on a failed push, but never incremented it on "
            "that path — the counter goes negative/undercounted"
        )

    def test_the_perf_twin_still_decrements_because_it_increments_eagerly(self) -> None:
        """Guard the fix's premise: the perf path's `kept -= 1` is correct precisely BECAUSE it
        does an eager `kept += 1` on keep. If that eager increment ever moves, the perf
        decrement would become as unbalanced as the bug one was."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        src = inspect.getsource(Driver._apply_verdict)
        assert "self.stats.kept += 1" in src, "the perf path no longer increments kept eagerly"
        assert "stats.kept -= 1" in src, (
            "the perf path's balancing decrement is gone — the eager increment is now unreversed "
            "on a failed push"
        )
        # The eager increment must come BEFORE the push branch that decrements it.
        assert src.index("self.stats.kept += 1") < src.index(
            "stats.kept -= 1"
        ), "the perf increment must precede its balancing decrement"


class TestUnresolvedThreadsActuallyBlockPublish:
    """The `autoPublish` gate's "no unresolved review comments" condition never fired, and its
    backstop was dead too — two independent key mismatches on the same fact.

    1. `auto_publish_gate` read `status["unresolvedComments"]`, but `fetch_pr_status` emits
       `unresolvedThreads` (`pr_checks.py:214`) and never that key. An absent key is falsy, so
       the guard was structurally unreachable.
    2. The value it *should* have read is itself always 0: `_count_unresolved` tests
       `comment.get("isResolved") is False`, while the provider's normalized comments carry
       `resolved` / `resolvable` (`source_providers.py`). So `derive_verdict` also never
       returned PROGRESS for open threads.

    Consequence with `watcherAutoStart` + `autoPublish` both on: a green draft carrying an
    unresolved reviewer thread runs `GET /watchers` → reconcile → `publish_if_authorized` →
    gate allows → `gh pr ready`, marking it ready-for-review with reviewer comments
    outstanding. Fail-OPEN on the one control whose whole job is to not publish over a human's
    open question. Raised by the Opus 5 review.
    """

    def test_the_gate_reads_the_key_the_status_actually_emits(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend.pr_watchers import (
            auto_publish_gate,
        )

        status = {
            "verdict": "READY",
            "draft": True,
            "mergeable": "MERGEABLE",
            "checks": {"failing": 0, "pending": 0, "total": 3},
            "unresolvedThreads": 2,
        }
        ok, why = auto_publish_gate(status)
        assert ok is False, "a pull request with 2 unresolved threads was cleared for publish"
        assert "unresolved" in why.lower(), why

    def test_a_clean_pull_request_still_publishes(self) -> None:
        """The gate must not become unconditionally closed — that would silently disable the
        opt-in feature rather than fix it."""
        from kiro_crew.apps.builtins.auto_improvement.backend.pr_watchers import (
            auto_publish_gate,
        )

        status = {
            "verdict": "READY",
            "draft": True,
            "mergeable": "MERGEABLE",
            "checks": {"failing": 0, "pending": 0, "total": 3},
            "unresolvedThreads": 0,
        }
        ok, why = auto_publish_gate(status)
        assert ok is True, f"a green, thread-free draft was refused: {why}"

    def test_unresolved_counting_matches_the_provider_shape(self) -> None:
        """`_count_unresolved` fed `unresolvedThreads`; it must read the keys the provider
        really writes (`resolved`/`resolvable`), not `isResolved`."""
        from kiro_crew.apps.builtins.auto_improvement.backend.pr_checks import _count_unresolved

        pr = {
            "comments": [
                {"resolvable": True, "resolved": False},  # an open thread
                {"resolvable": True, "resolved": True},  # settled
                {"resolvable": False, "resolved": False},  # not a thread at all
            ]
        }
        assert _count_unresolved(pr) == 1, (
            "the unresolved-thread count does not match the provider's comment shape, so "
            "`unresolvedThreads` is always 0 and every open-thread guard is dead"
        )

    def test_the_verdict_reports_progress_for_an_open_thread(self) -> None:
        """End of the chain: with counting fixed, an open reviewer thread must stop the verdict
        being READY.

        `derive_verdict` reads a PRE-COMPUTED `unresolvedThreads`, which `fetch_pr_status`
        injects from `_count_unresolved` before calling it — so the enrichment is composed here
        rather than passing a raw payload. (My first version of this test called `derive_verdict`
        on a bare `pr` dict and asserted PROGRESS; that failed because it skipped the very step
        under test, not because the code was wrong.)
        """
        from kiro_crew.apps.builtins.auto_improvement.backend.pr_checks import (
            _count_unresolved,
            derive_verdict,
        )

        pr = {
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "comments": [{"resolvable": True, "resolved": False}],
        }
        enriched = {**pr, "unresolvedThreads": _count_unresolved(pr)}
        assert enriched["unresolvedThreads"] == 1, "the enrichment step lost the open thread"
        verdict, reason = derive_verdict(enriched, {"failing": 0, "pending": 0, "total": 2})
        assert verdict != "READY", f"an open reviewer thread was reported READY ({reason})"
        assert "unresolved" in reason.lower(), reason


class TestBugPrRecordsTheTestedBaseNotTheFix:
    """The bug PR's `base_anchor` — the durable "Base: <branch> @ <sha>" provenance a reviewer
    reads to know what the fix was tested against — recorded the WRONG commit.

    Sequence in `_apply_verdict`'s bug arm: capture `pre_sha` (HEAD before the fix) →
    `_commit_bug_winner_provisional` runs `git commit`, advancing HEAD to the FIX commit →
    `emit_bug(base_anchor=f"{branch} @ {head_sha()[:12]}")`. By then `head_sha()` IS the fix,
    so the evidence claimed the fix was tested against itself. The perf twin (line ~894)
    already uses `base_sha` correctly; only the bug path read post-commit HEAD.

    The tested base is exactly `pre_sha`, which is already captured two statements earlier, so
    the fix is to anchor on it. Raised by the GPT review.
    """

    def test_the_bug_base_anchor_uses_pre_sha_not_head(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        src = inspect.getsource(Driver._apply_bug_winner)
        # The bug-arm emit_bug call must anchor on the pre-commit sha, not the live HEAD.
        assert 'base_anchor=f"{self.branch} @ {self.head_sha()[:12]}"' not in src, (
            "the bug PR anchors its base on post-commit HEAD — the fix commit — so its "
            "provenance names the wrong revision"
        )
        assert 'base_anchor=f"{self.branch} @ {pre_sha[:12]}"' in src, (
            "the bug base anchor should use pre_sha, the HEAD captured before the provisional "
            "fix commit"
        )

    def test_pre_sha_is_captured_before_the_provisional_commit(self) -> None:
        """The premise: `pre_sha` must be read BEFORE HEAD moves, or anchoring on it is no
        better than anchoring on HEAD."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        src = inspect.getsource(Driver._apply_bug_winner)
        i_pre = src.index("pre_sha = _git")
        i_commit = src.index("_commit_bug_winner_provisional")
        assert (
            i_pre < i_commit
        ), "pre_sha is captured after the provisional commit, so it already reflects the fix"

    def test_the_perf_twin_still_anchors_on_its_base(self) -> None:
        """Guard against a copy-paste 'fix' that breaks the perf path, which was already
        correct (it anchors on `base_sha`)."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        src = inspect.getsource(Driver._apply_verdict)
        assert (
            'base_anchor=f"{self.branch} @ {base_sha[:12]}"' in src
        ), "the perf path's base anchor changed shape — it must keep using base_sha"


class TestOnlyTheCommitThePipelineMadeIsPublished:
    """Only the commit the pipeline made is published: do not push what a worker did not commit.

    The direct push sent ``HEAD:refs/heads/<dest>`` while the pipeline verified, measured
    and recorded ONE specific commit. Between the two sits the pre-push review gate, which
    runs an agent in the SAME clone with ``Bash`` for up to 30 turns, and whose git denylist
    covers only ``push`` and ``remote set-url`` -- so ``git commit --amend`` is permitted there.
    That prompt is now report-only and its ``Edit`` grant is withdrawn, but neither is an
    enforcement boundary: a prompt is an instruction, and a shell can write files whatever the
    tool list says. Nothing compared the published commit against the verified one, so "what the
    pipeline verified" and "what got pushed" could come apart with nothing flagging it.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- a class-scoped autouse pin moved verbatim with its class; no dogfood-learnings module calls monkeypatch.undo()
    def _safe_repository(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda _clone: True)
        monkeypatch.setattr(clone_setup, "_push_disabled", lambda _clone: True)
        monkeypatch.setattr(clone_setup, "_repository_is_isolated", lambda _clone: True)

    def test_the_report_only_reviewer_has_no_edit_grant(self) -> None:
        """The prompt lost the sentence sanctioning an in-place fix; the CAPABILITY has to go
        with it. What decides whether a reviewer can modify the clone is the tool list, not the
        prose telling it what to do, so removing the instruction while keeping `Edit` leaves a
        reviewer that may still edit with no sanctioned reason to -- a grant with no consumer.

        This is least privilege, not the boundary: `Bash` stays, because the review has to run
        git to read the diff, and a shell can write files and commit. The boundary is the
        identity check that compares the published object against the verified one -- which is
        why this test asserts the grant is gone AND that the check is still what refuses a moved
        HEAD. Raised by the first-principles review of this branch."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        src = inspect.getsource(Driver._prepush_review_clean)
        assert '"Edit"' not in src, "the report-only reviewer may still edit the clone"
        assert '"Write"' not in src, "an edit-capable grant came back under another name"
        assert '"Bash"' in src, (
            "the review needs a shell to read the diff; removing it would make the gate "
            "vacuous rather than safer"
        )

    @staticmethod
    def _repo(tmp_path: Path) -> tuple[Path, str]:
        """A real one-commit clone; returns ``(clone, full 40-hex object id)``.

        REAL git rather than a stubbed ``_git``: the publish path validates that its anchor
        is a full forty-hex object id and refuses anything else, and a stub cannot exercise
        that -- a text comparison would pass against canned output while refusing every real
        push. The anchor is the id ``git rev-parse HEAD`` produces, never an abbreviation.
        """
        clone = tmp_path / "clone"
        clone.mkdir()

        def git(*args: str) -> str:
            done = subprocess.run(
                ["git", "-C", str(clone), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=clone,
                check=True,
            )
            return done.stdout.strip()

        git("init", "-q", "-b", "work")
        git("config", "user.email", "pipeline@example.invalid")
        git("config", "user.name", "pipeline")
        git("config", "commit.gpgsign", "false")
        (clone / "f.py").write_text("def g():\n    return 2\n", encoding="utf-8")
        git("add", "f.py")
        git("commit", "-q", "-m", "the verified change")
        return clone, git("rev-parse", "HEAD")

    @staticmethod
    def _driver(clone: Path, reviewer: object):  # type: ignore[no-untyped-def]
        """A Driver whose only stub is the PUBLISH call, so the gate itself stays real.

        ``_push_with_rebase`` is recorded rather than executed: it is what publishing IS,
        and the gate under test sits upstream of it. Everything the gate reads -- the clone,
        its git, the abbreviated sha -- is real.
        """
        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        recorded: list = []
        publishes: list[tuple[str, str, str, str]] = []

        def _publish(  # type: ignore[no-untyped-def]
            fetch_url: str, dest: str, target: str, src: str = "HEAD"
        ):
            # `src` is recorded, not ignored: WHICH REVISION is handed to the push is the
            # whole subject of this class, so a spy that dropped it could not tell publishing the
            # verified object from publishing whatever `HEAD` happens to be.
            publishes.append((fetch_url, dest, target, src))
            return subprocess.CompletedProcess(args=["push"], returncode=0, stdout="", stderr="")

        d = object.__new__(Driver)
        d.clone = clone  # type: ignore[attr-defined]
        d.log = logging.getLogger("t")  # type: ignore[attr-defined]
        d.branch = "work"  # type: ignore[attr-defined]
        d.direct_commit = True  # type: ignore[attr-defined]
        d.prepush_review = reviewer is not None  # type: ignore[attr-defined]
        d._agent_runner = reviewer  # type: ignore[attr-defined]
        d.pushed_sha = ""  # type: ignore[attr-defined]
        d.ledger = SimpleNamespace(record=recorded.append)  # type: ignore[assignment]
        d.profile = SimpleNamespace(  # type: ignore[attr-defined]
            pr_recipe=SimpleNamespace(fetch_url="https://example.invalid/y.git"),
        )
        d._push_with_rebase = _publish  # type: ignore[method-assign]
        return d, recorded, publishes

    def test_a_reviewer_amend_between_commit_and_push_refuses_to_publish(
        self, tmp_path: Path
    ) -> None:
        """The real fault at the real seam: the pre-push reviewer amends the clone."""
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        clone, verified = self._repo(tmp_path)

        class _AmendingReviewer:
            """Takes the pre-push prompt's own invitation to edit the clone."""

            def run(self, _prompt: str, **_kw: object):  # type: ignore[no-untyped-def]
                (clone / "f.py").write_text("def g():\n    return 99\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(clone), "add", "f.py"], cwd=clone, check=True)
                subprocess.run(
                    ["git", "-C", str(clone), "commit", "-q", "--amend", "--no-edit"],
                    cwd=clone,
                    check=True,
                )
                return SimpleNamespace(text="REVIEW: clean")

        d, recorded, publishes = self._driver(clone, _AmendingReviewer())
        out = D.Driver._direct_push(d, fp="fp", kind="bug", target="tgt", sha=verified)

        # The injection APPLIED: an assertion about a fault that never occurred is vacuous.
        head_now = subprocess.run(
            ["git", "-C", str(clone), "rev-list", "-1", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=clone,
            check=True,
        ).stdout.strip()
        assert head_now != verified, "the reviewer stub did not move HEAD"

        assert out is False
        assert publishes == [], "a commit the pipeline never verified must not be published"
        notes = [getattr(e, "note", "") for e in recorded]
        # Pin WHICH gate refused. There are two identity checks -- one before the credential
        # scan, one after it -- and the later one would absorb this case silently if the
        # earlier one were removed, so asserting only "HEAD moved" would leave the pre-scan
        # gate unmeasured. It must be the PRE-SCAN one: an amend during the review has
        # already happened by then, and refusing before the scan means the scanner never
        # reads a substituted commit and the recorded reason is the true one rather than
        # whatever that commit's content happens to trip.
        assert any(n.startswith("direct-push refused: HEAD moved") for n in notes), notes
        assert not any("after the scan" in n for n in notes), notes

    def test_an_unmoved_head_still_publishes(self, tmp_path: Path) -> None:
        """The accepting case: a gate with no reachable yes is an outage, not a guard, and
        only this direction can tell the two apart."""
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        clone, verified = self._repo(tmp_path)

        class _CleanReviewer:
            def run(self, _prompt: str, **_kw: object):  # type: ignore[no-untyped-def]
                return SimpleNamespace(text="REVIEW: clean")

        d, recorded, publishes = self._driver(clone, _CleanReviewer())
        out = D.Driver._direct_push(d, fp="fp", kind="bug", target="tgt", sha=verified)
        assert out is True, [getattr(e, "note", "") for e in recorded]
        assert len(publishes) == 1
        assert publishes[0][1] == "work"
        # It publishes the FULL OBJECT ID, not `HEAD`. A symbolic ref is resolved by the push
        # itself, which is after every check; an id names one immutable commit.
        full = subprocess.run(
            ["git", "-C", str(clone), "rev-list", "-1", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=clone,
            check=True,
        ).stdout.strip()
        assert publishes[0][3] == full, publishes[0]
        assert publishes[0][3] != "HEAD"

    def test_a_move_after_the_scan_cannot_change_what_is_published(self, tmp_path: Path) -> None:
        """The residual time-of-check/time-of-use window: the identity check passes, and only
        THEN do the credential scan and the push run. Re-reading ``HEAD`` at each step opens
        this window: a background process the reviewer left behind could move it in between --
        the scan would read one commit and the push would publish another, neither of them
        verified.

        Two things close it, and this test pins both. The push is handed the retained OBJECT
        ID, which cannot be repointed. And the identity is re-checked after the scan, so a
        move during the scan window is refused rather than published unscanned. Here the move
        is injected by the credential scanner itself, which is the last thing to run before
        the push. Raised by the GPT review of this branch."""
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        clone, verified = self._repo(tmp_path)

        class _CleanReviewer:
            def run(self, _prompt: str, **_kw: object):  # type: ignore[no-untyped-def]
                return SimpleNamespace(text="REVIEW: clean")

        d, recorded, publishes = self._driver(clone, _CleanReviewer())
        before = subprocess.run(
            ["git", "-C", str(clone), "rev-list", "-1", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=clone,
            check=True,
        ).stdout.strip()

        def _scan_then_amend(_blob: str):  # type: ignore[no-untyped-def]
            """Stands in for a background amend landing while the scan runs."""
            (clone / "f.py").write_text("def g():\n    return 99\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(clone), "add", "f.py"], cwd=clone, check=True)
            subprocess.run(
                ["git", "-C", str(clone), "commit", "-q", "--amend", "--no-edit"],
                cwd=clone,
                check=True,
            )
            return True, 0

        # Patched on `push_policy`, not on `driver`: `_direct_push` imports the scanner
        # INSIDE the function, so the name it binds is looked up on the source module at
        # call time and a `driver`-level attribute would never be consulted.
        from kiro_crew.apps.builtins.auto_improvement.spine import push_policy as PP

        monkey = PP.scan_content_for_secrets
        try:
            PP.scan_content_for_secrets = _scan_then_amend  # type: ignore[assignment]
            out = D.Driver._direct_push(d, fp="fp", kind="bug", target="tgt", sha=verified)
        finally:
            PP.scan_content_for_secrets = monkey  # type: ignore[assignment]

        after = subprocess.run(
            ["git", "-C", str(clone), "rev-list", "-1", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=clone,
            check=True,
        ).stdout.strip()
        assert after != before, "the injected move did not apply, so this proves nothing"

        assert out is False
        assert publishes == [], "a move during the scan window must not reach the push"
        notes = [getattr(e, "note", "") for e in recorded]
        assert any("after the scan" in n for n in notes), notes

    def test_the_credential_scan_reads_the_committed_object_not_head(self, tmp_path: Path) -> None:
        """The scan must read the object that gets published, bound by ID rather than by
        timing. While it read ``HEAD``, a concurrent writer in the clone could point HEAD at a
        clean decoy for the duration of the ``git diff`` and restore it afterwards: the
        scanner saw the decoy, every identity check still passed, and the real commit was
        published having never been scanned. A later re-check cannot close that -- it is
        itself a check-then-use pair -- so the range is the object id.

        The move is injected here between the pre-scan gate and the scan, which is exactly the
        window a backgrounded process would use. Raised by the GPT review of this branch."""
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D
        from kiro_crew.apps.builtins.auto_improvement.spine import push_policy as PP

        clone, verified = self._repo(tmp_path)

        class _CleanReviewer:
            def run(self, _prompt: str, **_kw: object):  # type: ignore[no-untyped-def]
                return SimpleNamespace(text="REVIEW: clean")

        d, recorded, publishes = self._driver(clone, _CleanReviewer())
        # The move must land INSIDE the window -- after the pre-scan gate, before the scan --
        # so the seam is the gate's own return. Two seams were tried and rejected:
        # `driver.normalize_branch` is imported INSIDE `_direct_push`, so patching it on the
        # driver module is inert (the injection never fired and this test passed against a
        # deliberately broken gate -- caught by the mutation, not by the green run); patching
        # it on `push_policy` fires too EARLY, because `authorize_direct_push` calls it before
        # the gate, which refuses and the scan never runs.
        real_gate = D.Driver._head_is_the_committed_sha
        real_scan = PP.scan_content_for_secrets
        scanned: list[str] = []
        gate_calls: list[int] = []

        def _gate_then_swap_head(committed_id: str):  # type: ignore[no-untyped-def]
            out = real_gate(d, committed_id)
            gate_calls.append(1)
            if len(gate_calls) == 1:  # only after the PRE-scan gate, not the post-scan one
                (clone / "f.py").write_text("def g():\n    return 99\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(clone), "add", "f.py"], cwd=clone, check=True)
                subprocess.run(
                    ["git", "-C", str(clone), "commit", "-q", "--amend", "--no-edit"],
                    cwd=clone,
                    check=True,
                )
            return out

        def _record(blob: str):  # type: ignore[no-untyped-def]
            scanned.append(blob)
            return True, 0

        d._head_is_the_committed_sha = _gate_then_swap_head  # type: ignore[method-assign]
        try:
            PP.scan_content_for_secrets = _record  # type: ignore[assignment]
            D.Driver._direct_push(d, fp="fp", kind="bug", target="tgt", sha=verified)
        finally:
            PP.scan_content_for_secrets = real_scan  # type: ignore[assignment]

        head_now = subprocess.run(
            ["git", "-C", str(clone), "rev-list", "-1", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=clone,
            check=True,
        ).stdout.strip()
        original = subprocess.run(
            ["git", "-C", str(clone), "rev-list", "-1", f"{verified}^{{commit}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=clone,
            check=True,
        ).stdout.strip()
        assert gate_calls, "the pre-scan gate never ran, so nothing was injected"
        assert head_now != original, "the injection did not move HEAD, so this proves nothing"
        assert scanned, "the credential scan never ran, so this proves nothing"
        # The scan reads the commit OBJECT and its tree content as separate passes, so the
        # assertion covers everything handed to the scanner rather than one pass by index.
        blob = "\n".join(scanned)
        assert "return 2" in blob, f"the scan did not read the verified object: {blob!r}"
        assert "return 99" not in blob, f"the scan read the decoy at HEAD instead: {blob!r}"

    def test_a_ref_named_the_abbreviation_cannot_shadow_the_committed_object(
        self, tmp_path: Path
    ) -> None:
        """The anchor is a full object id from the start, so no abbreviation is ever resolved
        and there is no second reading for a ref to capture. Git resolves a revision through
        ref names before abbreviated object ids, so if the anchor were a short sha a reviewer
        could amend and then create a branch named that short sha, and a late re-resolution
        would point at the amended HEAD -- both sides equal, gate satisfied, unverified commit
        published. Two things now hold. A planted ref named the short sha changes nothing,
        because the publish path anchors on the full id the finalizer produced. And a short
        sha handed in as the anchor is REFUSED rather than resolved, so the abbreviation never
        becomes a lookup a ref could shadow."""
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        clone, verified = self._repo(tmp_path)
        short = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=clone,
            check=True,
        ).stdout.strip()

        class _ShadowingReviewer:
            """Amends, then points a ref named the OLD short sha at the new commit."""

            def run(self, _prompt: str, **_kw: object):  # type: ignore[no-untyped-def]
                (clone / "f.py").write_text("def g():\n    return 99\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(clone), "add", "f.py"], cwd=clone, check=True)
                subprocess.run(
                    ["git", "-C", str(clone), "commit", "-q", "--amend", "--no-edit"],
                    cwd=clone,
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", str(clone), "branch", short, "HEAD"],
                    cwd=clone,
                    check=True,
                )
                return SimpleNamespace(text="REVIEW: clean")

        d, recorded, publishes = self._driver(clone, _ShadowingReviewer())
        out = D.Driver._direct_push(d, fp="fp", kind="bug", target="tgt", sha=verified)

        # Both halves of the injection APPLIED: the ref named the short sha exists AND it
        # resolves to the amended commit, which is what would defeat a late re-resolution of
        # an abbreviation.
        shadow = subprocess.run(
            ["git", "-C", str(clone), "rev-list", "-1", f"{short}^{{commit}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=clone,
            check=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "-C", str(clone), "rev-list", "-1", "HEAD^{commit}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=clone,
            check=True,
        ).stdout.strip()
        assert shadow == head, "the shadow ref did not capture the abbreviation"

        # The anchor is the full id, so the planted ref changes nothing: the gate compares the
        # full id against the amended HEAD, they differ, and the publish is refused.
        assert out is False
        assert publishes == [], "a shadowed abbreviation must not authorize a publish"
        notes = [getattr(e, "note", "") for e in recorded]
        # The PRE-SCAN gate must be the one that refuses: the post-scan re-check would
        # otherwise absorb this case and leave the pre-scan gate unmeasured.
        assert any(n.startswith("direct-push refused: HEAD moved") for n in notes), notes
        assert not any("after the scan" in n for n in notes), notes

        # And the new validation: a SHORT sha handed in as the anchor is refused rather than
        # resolved, so an abbreviation never becomes a lookup a ref could shadow. A fresh clone
        # keeps the reviewer from mattering here -- the point is the anchor's shape.
        second = tmp_path / "second"
        second.mkdir()
        clean_clone, _full = self._repo(second)
        clean_short = subprocess.run(
            ["git", "-C", str(clean_clone), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=clean_clone,
            check=True,
        ).stdout.strip()

        class _CleanReviewer:
            def run(self, _prompt: str, **_kw: object):  # type: ignore[no-untyped-def]
                return SimpleNamespace(text="REVIEW: clean")

        d2, recorded2, publishes2 = self._driver(clean_clone, _CleanReviewer())
        out2 = D.Driver._direct_push(d2, fp="fp", kind="bug", target="tgt", sha=clean_short)
        assert out2 is False, "a short sha anchor was resolved instead of refused"
        assert publishes2 == [], "a short sha anchor must not authorize a publish"
        notes2 = [getattr(e, "note", "") for e in recorded2]
        assert any(n.startswith("direct-push refused: cannot resolve") for n in notes2), notes2

    def test_a_replacement_ref_cannot_substitute_what_the_scan_reads(self, tmp_path: Path) -> None:
        """Git substitutes objects named by ``refs/replace/<oid>`` transparently in READS --
        ``diff`` and ``show`` included -- while the push transport sends the ORIGINAL object.
        A reviewer can run ``git replace`` (its runner's denylist covers only ``push`` and
        ``remote set-url``), point a replacement at clean content, and have the credential
        scan read the decoy while the real object is transferred. The scan therefore reads
        with replace refs disabled. Real git, because this is git behaviour rather than
        anything a stub could express. Raised by the GPT review of this branch."""
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D
        from kiro_crew.apps.builtins.auto_improvement.spine import push_policy as PP

        clone, verified = self._repo(tmp_path)

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(clone), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=clone,
                check=True,
            ).stdout.strip()

        # A DECOY commit with clean-looking content, then a replacement ref pointing the
        # verified object at it.
        original_id = git("rev-list", "-1", f"{verified}^{{commit}}")
        git("checkout", "-q", "--detach", original_id)
        (clone / "f.py").write_text("def g():\n    return 777\n", encoding="utf-8")
        git("add", "f.py")
        git("commit", "-q", "--amend", "--no-edit")
        decoy_id = git("rev-list", "-1", "HEAD^{commit}")
        git("checkout", "-q", "work")
        git("replace", "-f", original_id, decoy_id)
        assert git("rev-parse", "--verify", f"refs/replace/{original_id}"), "no replacement ref"

        class _CleanReviewer:
            def run(self, _prompt: str, **_kw: object):  # type: ignore[no-untyped-def]
                return SimpleNamespace(text="REVIEW: clean")

        d, recorded, publishes = self._driver(clone, _CleanReviewer())
        real_scan = PP.scan_content_for_secrets
        scanned: list[str] = []

        def _record(blob: str):  # type: ignore[no-untyped-def]
            scanned.append(blob)
            return True, 0

        try:
            PP.scan_content_for_secrets = _record  # type: ignore[assignment]
            D.Driver._direct_push(d, fp="fp", kind="bug", target="tgt", sha=verified)
        finally:
            PP.scan_content_for_secrets = real_scan  # type: ignore[assignment]

        # Control: the replacement really is in effect for an ordinary read.
        replaced_read = subprocess.run(
            ["git", "-C", str(clone), "show", "--format=", "--root", original_id],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=clone,
            check=True,
        ).stdout
        assert (
            "777" in replaced_read
        ), "the replacement ref is not in effect, so this proves nothing"

        assert scanned, "the credential scan never ran"
        # The scan reads the commit OBJECT and its tree content as separate passes, so the
        # assertion covers everything handed to the scanner rather than one pass by index.
        blob = "\n".join(scanned)
        assert "return 2" in blob, f"the scan read the replacement, not the real object: {blob!r}"
        assert "return 777" not in blob, f"the scan read the substituted content: {blob!r}"

    def test_an_unresolvable_committed_revision_fails_closed(self, tmp_path: Path) -> None:
        """Not being able to look must not read as having looked and found nothing wrong:
        an amended-away commit can be pruned, which is exactly when this check matters
        most."""
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        clone, _verified = self._repo(tmp_path)

        class _CleanReviewer:
            def run(self, _prompt: str, **_kw: object):  # type: ignore[no-untyped-def]
                return SimpleNamespace(text="REVIEW: clean")

        d, recorded, publishes = self._driver(clone, _CleanReviewer())
        out = D.Driver._direct_push(d, fp="fp", kind="bug", target="tgt", sha="0" * 12)
        assert out is False
        assert publishes == []
        notes = [getattr(e, "note", "") for e in recorded]
        # Pre-scan gate again: an unresolvable object should stop the push before the
        # credential scan spends a git diff on a revision that does not exist.
        assert any(n.startswith("direct-push refused: cannot resolve") for n in notes), notes
        assert not any("after the scan" in n for n in notes), notes

    def test_a_credential_in_the_committer_identity_refuses_to_publish(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A commit carries HEADER BYTES as well as content, and the push transfers both.

        `git rebase` takes the committer identity from CONFIGURATION rather than from the
        commit it replays, so a process able to write the clone's git config chooses those
        bytes -- the same actor this path already refuses to trust for `git replace`, whose
        runner denylist covers only `push` and `remote set-url`. A scan reading only the tree
        diff cannot see them, and the root-commit branch suppresses headers outright with
        `--format=`, so a key-shaped identity would reach the remote unread. Exposure to a
        remote is not recoverable, which is why this fails closed rather than warning.

        The plant goes through the ENVIRONMENT here because this suite's conftest pins
        ``GIT_COMMITTER_EMAIL`` for hermetic runs and that takes precedence over config. Which
        of the two carries it is incidental to what is being proved: the bytes land in the
        commit object either way, and the question is whether the scanner reads them.
        """
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        clone, _verified = self._repo(tmp_path)

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(clone), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=clone,
                check=True,
            ).stdout.strip()

        # The tree content stays CLEAN; only the identity is poisoned. A commit that carried
        # the key in its content would be caught by the pre-existing content scan and would
        # prove nothing about metadata.
        (clone / "f.py").write_text("def g():\n    return 3\n", encoding="utf-8")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "glpat-IOSFODNN7EXAMPLE@example.invalid")
        git("add", "f.py")
        git("commit", "-q", "-m", "a clean-looking change")
        poisoned = git("rev-parse", "HEAD")

        # Control 1: the identity really did land in the object's committer line.
        raw = git("cat-file", "commit", poisoned)
        assert "glpat-IOSFODNN7EXAMPLE" in raw, f"the identity did not reach the object: {raw!r}"

        # Control 2: the TREE DIFF is clean, so a refusal cannot come from the content path.
        # Without this the test would pass even if metadata were still unscanned.
        diff_only = subprocess.run(
            ["git", "-C", str(clone), "diff", "--no-ext-diff", f"{poisoned}~1..{poisoned}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=clone,
            check=True,
        ).stdout
        assert "glpat-IOSFODNN7EXAMPLE" not in diff_only, "the content path would catch this"

        d = object.__new__(D.Driver)
        d.clone = clone  # type: ignore[attr-defined]
        clean, note = D.Driver._revision_scans_clean(d, poisoned)
        assert clean is False, "a credential in the committer identity was scanned as clean"
        # The note is a fixed literal from `describe_scan`, so nothing scanned reaches a log.
        assert "glpat-IOSFODNN7EXAMPLE" not in note, note
