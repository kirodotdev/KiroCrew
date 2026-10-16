"""Auto-improvement regressions: the queued change and its one-click commit.

A queued change is not recorded as filed, its diff travels byte-exact, one-click
commit works inside a push-disabled clone, a landed commit supersedes the filed row,
and the direct-commit queue copy is written where its readers look.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod
from kiro_crew.apps.builtins.auto_improvement.backend import store
from kiro_crew.apps.builtins.auto_improvement.tests.dogfood_learnings_helpers import (  # noqa: F401
    _cwd_is_the_scratch_dir,
)


class TestAQueuedChangeIsNotRecordedAsFiled:
    """`QUEUED:<fp>` means "on disk, no pull request" — recording it as `filed` was terminal.

    `filed` is HARD-terminal in `Ledger.known` ("a filed CR is never re-filed"), so a change
    that only got queued (no `gh`, no network, refused push) was deduped FOREVER and never
    retried; and `filed_crs()` feeds the PR watchers, which were handed a non-URL. Measured
    before the fix: `known()` True and `filed_crs()` `['QUEUED:abc']`.

    Raised by the GPT review of this branch.
    """

    def test_a_queued_reference_stays_retryable(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine import ledger as L

        led = L.Ledger(tmp_path / "l.jsonl", retry_cooldown_s=0.0)
        led.record(
            L.LedgerEntry(
                fp="abc",
                kind="perf",
                target="m.py::f",
                status=L.STATUS_ERROR,
                cr="QUEUED:abc",
                note="queued, not filed",
            )
        )
        assert led.known("abc") is False, "a queued change was deduped as if it were filed"
        assert led.filed_crs() == [], "a non-URL reference was handed to the PR watchers"

    def test_a_real_pr_url_is_still_filed(self, tmp_path) -> None:
        """The fix must not stop recording genuine pull requests."""
        from kiro_crew.apps.builtins.auto_improvement.spine import ledger as L

        led = L.Ledger(tmp_path / "l.jsonl", retry_cooldown_s=0.0)
        led.record(
            L.LedgerEntry(
                fp="def",
                kind="perf",
                target="m.py::g",
                status=L.STATUS_FILED,
                cr="https://github.com/o/r/pull/7",
            )
        )
        assert led.known("def") is True, "a filed PR must stay deduped"
        assert led.filed_crs() == ["https://github.com/o/r/pull/7"]

    def test_the_pipeline_branches_on_the_queued_prefix(self) -> None:
        """Structural, and it pins the subtle half: the KEEP must survive.

        `filed=False` would also roll the provisional commit back and decrement `kept`,
        discarding a change that passed RED x2 -> GREEN -> STAYGREEN merely because `gh` was
        missing. Measured while writing this: flipping it took a bounded run's `kept` 1 -> 0.
        So the ledger status is retryable while `filed` stays True.
        """
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.pr_pipeline import CrPipeline

        src = inspect.getsource(CrPipeline)
        i = src.index('startswith("QUEUED:")')
        branch = src[i : i + 1400]
        assert "STATUS_ERROR" in branch, "a queued change is still recorded as filed"
        assert "filed=True" in branch, "filed=False would throw away the verified win"


class TestALandedCommitSupersedesItsFiledRow:
    """One-click commit pushed the change but left the ledger saying ``filed``.

    The ledger is last-write-wins per fingerprint, and ``filed`` is what ``filed_crs()``
    hands the pull-request watchers and what the UI reads to offer the commit button. So a
    change already on the branch kept reporting as an open PR, and the operator was invited
    to commit it a second time. The loop's own direct-commit path records a ``committed``
    row; the manual path has to agree about the same outcome.

    Raised by the GPT review of this branch.
    """

    def test_the_recorded_status_supersedes_filed(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import ledger_admin as LA

        path = tmp_path / "ledger.jsonl"
        path.write_text(
            json.dumps(
                {
                    "fp": "abc123",
                    "kind": "perf",
                    "target": "m.py::f",
                    "status": "filed",
                    "cr": "QUEUED:abc123",
                    "note": "queued",
                    "ts": 1.0,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(LA.store, "ledger_path", lambda: path)

        assert LA.record_committed("abc123", branch="feat/x", sha="deadbeef") is True

        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 2, "the event must be APPENDED, not rewritten over the history"
        latest = {r["fp"]: r for r in rows}["abc123"]
        assert latest["status"] == LA.STATUS_COMMITTED
        assert latest["cr"] == "deadbeef", "the landed sha must be recorded"
        # kind/target carry over so the row stays identifiable in the timeline.
        assert (latest["kind"], latest["target"]) == ("perf", "m.py::f")
        # `pr` must NOT be written: LedgerEntry(**row) is a fixed-field dataclass, so an
        # unexpected key raises TypeError inside _load()'s torn-line handler and the whole
        # event silently vanishes — the record would stay `filed` after all.
        assert "pr" not in latest

    def test_the_status_constant_matches_the_spine(self) -> None:
        """It is spelled literally here to avoid importing the engine; pin the value."""
        from kiro_crew.apps.builtins.auto_improvement.backend import ledger_admin as LA
        from kiro_crew.apps.builtins.auto_improvement.spine import ledger as real

        assert LA.STATUS_COMMITTED == real.STATUS_COMMITTED

    def test_a_bookkeeping_failure_never_reports_success(self, tmp_path, monkeypatch) -> None:
        """The push already happened, so this must not raise — but it must not lie either."""
        from kiro_crew.apps.builtins.auto_improvement.backend import ledger_admin as LA

        monkeypatch.setattr(LA.store, "ledger_path", lambda: tmp_path / "ledger.jsonl")

        def _boom(_row):
            raise OSError("disk full")

        monkeypatch.setattr(LA, "_append_event", _boom)
        assert LA.record_committed("abc123", branch="b", sha="s") is False

    def test_a_malformed_fingerprint_is_refused(self, tmp_path, monkeypatch) -> None:
        """`validate_fingerprint` RAISES rather than returning falsy — the guard catches it."""
        from kiro_crew.apps.builtins.auto_improvement.backend import ledger_admin as LA

        path = tmp_path / "ledger.jsonl"
        monkeypatch.setattr(LA.store, "ledger_path", lambda: path)
        assert LA.record_committed("../../etc/passwd", branch="b", sha="s") is False
        assert not path.exists(), "a refused fingerprint must write nothing"

    def test_the_route_records_after_a_successful_commit(self) -> None:
        """Structural: the success branch must write the row before returning 200."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes

        src = inspect.getsource(routes._handle_commit)
        assert "record_committed" in src, "a landed commit must supersede its filed row"
        assert src.index("record_committed") < src.index("status=200"), "record, then return"


class TestQueuedDiffTransport:
    @pytest.fixture
    def git_calls(self, monkeypatch):
        calls: list[tuple[str, ...]] = []

        def _git(clone, *args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(commit_mod, "resolve_origin_url", lambda config: "")
        monkeypatch.setattr(commit_mod, "_git", _git)
        return calls

    @pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
    def test_patch_stdin_preserves_line_endings_and_non_utf8_bytes(
        self, tmp_path, monkeypatch, git_calls, line_ending
    ) -> None:
        raw = line_ending.join(
            [b"--- a/app.py", b"+++ b/app.py", b"@@ -1 +1 @@", b"-old", b"+caf\xc3\xa9\xff", b""]
        )
        captured: dict[str, object] = {}

        def _run(argv, **kwargs):
            captured.update(kwargs)
            assert argv == ["git", "-C", str(tmp_path), "apply", "--index", "-"]
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(commit_mod.subprocess, "run", _run)
        out = commit_mod.materialize_queued_diff(
            clone=tmp_path,
            branch="work",
            config={},
            diff_text=raw.decode("utf-8", errors="surrogateescape"),
        )
        assert out == {"ok": True, "base": "origin/work"}
        assert captured == {
            "input": raw,
            "capture_output": True,
            "timeout": commit_mod._GIT_TIMEOUT_S,
        }, "text-mode stdin can translate patch newlines on Windows"
        assert git_calls[-1] == ("checkout", "-B", "work", "origin/work")

    @pytest.mark.parametrize("stderr", [b"private\xff" + b"x" * 200, b""])
    def test_apply_failure_decodes_then_redacts_bounds_and_rolls_back(
        self, tmp_path, monkeypatch, git_calls, stderr
    ) -> None:
        monkeypatch.setattr(
            commit_mod.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, b"", stderr),
        )
        decoded: list[str] = []
        redacted = "[redacted]" + "x" * 200

        def _redact(text):
            decoded.append(text)
            return redacted

        monkeypatch.setattr(commit_mod, "redact_via_context", _redact)
        out = commit_mod.materialize_queued_diff(
            clone=tmp_path, branch="work", config={}, diff_text="patch\n"
        )
        assert decoded == [stderr.decode("utf-8", errors="replace")]
        assert out == {"ok": False, "error": f"the queued diff did not apply: {redacted[:160]}"}
        assert git_calls[-1] == ("reset", "--hard", "origin/work")


class TestOneClickCommitWorksInAPushDisabledClone:
    """One-click commit ran `git fetch origin` inside a clone whose origin is neutralized.

    `clone_setup._disable_push` rewrites BOTH origin urls to `DISABLED_NO_PUSH` (a
    deliberate control — pushing by url ignores the push url, so disabling one is not
    enough). Every clone the loop works in is therefore in that state, which made
    `git fetch --quiet origin <branch>` exit 128 and `commit_finding` return
    "could not fetch" before it applied anything. Measured against a local bare repo:
    fetch by remote name succeeded before the neutralization and failed with
    "'DISABLED_NO_PUSH' does not appear to be a git repository" after it.

    Every pre-existing test for this function exercised a REFUSAL path (no queued diff,
    protected branch), so all of them passed while the success path was dead. That is the
    gap this test closes: it drives the real thing end to end against a real bare repo,
    with the origin neutralized exactly as production leaves it.

    Raised by the GPT review of this branch.
    """

    @staticmethod
    def _git(*args: str, cwd: Path) -> None:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert result.returncode == 0, (
            f"git {' '.join(args)} failed with rc={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def _upstream_and_clone(self, tmp_path: Path) -> tuple[Path, Path, str]:
        """A bare 'remote', a seeded branch, and a clone with BOTH urls neutralized."""
        upstream = tmp_path / "upstream.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(upstream)], cwd=upstream.parent, check=True
        )

        seed = tmp_path / "seed"
        subprocess.run(
            ["git", "clone", "-q", str(upstream), str(seed)],
            cwd=seed.parent,
            check=True,
            capture_output=True,
        )
        # The literal patches below target an LF blob, even when autocrlf is disabled.
        (seed / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8", newline="\n")
        self._git("add", "-A", cwd=seed)
        self._git("commit", "-qm", "init", cwd=seed)
        self._git("branch", "-M", "work", cwd=seed)
        self._git("push", "-q", "origin", "work", cwd=seed)

        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(upstream), str(clone)],
            cwd=clone.parent,
            check=True,
            capture_output=True,
        )
        # Exactly what production leaves behind.
        clone_setup._disable_push(clone)
        return upstream, clone, "work"

    def test_the_queued_diff_is_committed_and_pushed(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "data")
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        upstream, clone, branch = self._upstream_and_clone(tmp_path)

        # Quick check: the clone really cannot reach its own origin. If this ever stops being
        # true the test below proves nothing.
        probe = subprocess.run(
            ["git", "-C", str(clone), "fetch", "--quiet", "origin", branch],
            cwd=clone,
            capture_output=True,
        )
        assert probe.returncode != 0, "origin is still reachable — _disable_push regressed"

        # Config FIRST: `pr_queue_dir()` is derived from it (per-repo subtree), so a diff
        # written before the config lands in a different repo's queue and the function
        # correctly reports "no queued change".
        store.write_json_atomic(
            store.config_path(),
            {"clone": str(clone), "branch": branch, "origin_url": str(upstream)},
        )
        q = store.pr_queue_dir()
        q.mkdir(parents=True, exist_ok=True)
        (q / "fp1.diff").write_text(
            "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
            " def f():\n-    return 1\n+    return 2\n",
            encoding="utf-8",
        )
        (q / "fp1.pr.md").write_text("# fix: return 2\n", encoding="utf-8")

        out = commit_mod.commit_finding("fp1")
        assert out["ok"] is True, f"one-click commit failed: {out.get('error')}"

        # The change is really on the remote, not just in the local clone.
        show = subprocess.run(
            ["git", "-C", str(upstream), "show", f"{branch}:app.py"],
            cwd=upstream,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert "return 2" in show.stdout, f"the commit never reached the remote: {show.stdout!r}"

    def test_drafting_an_older_finding_does_not_publish_a_later_one(
        self, tmp_path, monkeypatch
    ) -> None:
        """The manual DRAFT path must materialize its own queued diff.

        `recipe.draft(diff=...)` only WRITES the queue copy, and `_push_fix_branch` pushes
        the clone's `HEAD` — so drafting an OLDER queued finding published whatever a LATER
        cycle had left at HEAD. Measured against a real bare repo before fixing: finding A's
        queued diff adds `FINDING_A`, and the branch pushed for A contained `FINDING_B`
        instead, so the PR's metadata and its content disagreed. The loop path never hit
        this because `_stage_winner` applies the winner into the clone first.

        Raised by the GPT review of this branch.
        """
        monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "data")
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        upstream, clone, branch = self._upstream_and_clone(tmp_path)

        store.write_json_atomic(
            store.config_path(),
            {"clone": str(clone), "branch": branch, "origin_url": str(upstream)},
        )

        # Finding A is queued as a diff only — never committed.
        diff_a = (
            "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
            " def f():\n-    return 1\n+    return 111  # FINDING_A\n"
        )

        # A LATER cycle leaves an unrelated commit at the clone's HEAD.
        self._git("checkout", "-q", "-B", branch, f"origin/{branch}", cwd=clone)
        (clone / "later.py").write_text("FINDING_B = True\n", encoding="utf-8")
        self._git("add", "-A", cwd=clone)
        self._git("commit", "-qm", "a later cycle's work", cwd=clone)

        staged = commit_mod.materialize_queued_diff(
            clone=clone,
            branch=branch,
            config=store.read_json(store.config_path(), {}),
            diff_text=diff_a,
        )
        assert staged.get("ok") is True, f"staging failed: {staged.get('error')}"

        # COMMIT it: `apply --index` stages but does not move HEAD, and the recipe pushes
        # `HEAD`. An earlier version of this test asserted only the WORKTREE and therefore
        # passed while the pushed branch still carried the base — see the push assertion
        # below, which is what actually pins the published content.
        body = tmp_path / "fp.pr.md"
        body.write_text("# fix: return 111\n", encoding="utf-8")
        done = commit_mod.commit_staged_for_draft(clone=clone, body_path=body, fp="aaaa")
        assert done.get("ok") is True, f"commit failed: {done.get('error')}"

        # Assert what would actually be PUBLISHED. `_push_fix_branch` pushes
        # `HEAD:refs/heads/<branch>`, so reproduce that push and read the content back off
        # the remote — the worktree agreeing is not enough, and asserting only the worktree
        # is precisely how the uncommitted-HEAD bug survived the first version of this test.
        self._git(
            "push",
            "-q",
            "--force-with-lease",
            str(upstream),
            "HEAD:refs/heads/auto/fix-aaaa",
            cwd=clone,
        )
        published = subprocess.run(
            ["git", "-C", str(upstream), "show", "auto/fix-aaaa:app.py"],
            cwd=upstream,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
        assert "FINDING_A" in published, "the queued fix never reached the pushed branch"
        assert "FINDING_B" not in published, "a later cycle's change was published instead"
        # The base is the REMOTE branch, so the later LOCAL commit is not carried along.
        assert (
            "later.py"
            not in subprocess.run(
                ["git", "-C", str(upstream), "ls-tree", "--name-only", "auto/fix-aaaa"],
                cwd=upstream,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).stdout
        ), "the later cycle's commit rode along into the draft"

    def test_a_rollback_restores_the_branch_to_its_fetched_base(self, tmp_path) -> None:
        """Behavioural counterpart to the structural rollback test.

        Proves the reset actually returns the branch to the base a failed draft started
        from, so the next run's `checkout_branch` does not adopt an unfiled commit as its
        baseline. Measured before the fix: local `work` sat 1 commit ahead of a remote it
        was never pushed to. Raised by the GPT review of this branch.
        """
        from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod

        upstream, clone, branch = self._upstream_and_clone(tmp_path)
        store.write_json_atomic(
            store.config_path(),
            {"clone": str(clone), "branch": branch, "origin_url": str(upstream)},
        )
        staged = commit_mod.materialize_queued_diff(
            clone=clone,
            branch=branch,
            config=store.read_json(store.config_path(), {}),
            diff_text=(
                "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
                " def f():\n-    return 1\n+    return 2\n"
            ),
        )
        assert staged.get("ok") is True, f"staging failed: {staged.get('error')}"
        base = str(staged["base"])
        base_sha = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", base],
            cwd=clone,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

        body = tmp_path / "fp.pr.md"
        body.write_text("# fix\n", encoding="utf-8")
        assert commit_mod.commit_staged_for_draft(clone=clone, body_path=body, fp="fp").get("ok")
        moved = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"],
            cwd=clone,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        assert moved != base_sha, "the commit did not move HEAD — nothing to roll back"

        # What the route's `_rollback()` does when the draft publishes nothing.
        commit_mod._git(clone, "reset", "--hard", base)

        back = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"],
            cwd=clone,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        assert back == base_sha, "the branch was not restored to its fetched base"
        assert "return 1" in (clone / "app.py").read_text(encoding="utf-8")
        # And nothing is left staged for a later `add -A` to absorb.
        assert (
            subprocess.run(
                ["git", "-C", str(clone), "status", "--porcelain"],
                cwd=clone,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).stdout.strip()
            == ""
        )

    def test_two_concurrent_commits_do_not_merge_into_one(self, tmp_path, monkeypatch) -> None:
        """Two operator clicks must not interleave in the shared clone.

        The run-status gate stops these paths racing the LOOP, not each other: the dashboard's
        commit icon had no `disabled` while pending, so clicking two `filed` rows started two
        mutations, each in its own `asyncio.to_thread` thread against the same clone. Measured
        on a real bare repo: A stages its diff, B's `checkout -B <branch> <base>` does NOT
        discard it (the branch is already at base, so no files change), B's `apply --index`
        stacks on top, and B's commit contains BOTH findings — the commit recorded as B
        publishes A's change too. Serialized on `commit.clone_lock()`.

        Raised by the Opus 5 review of this branch.
        """
        import contextlib
        import threading

        monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "data")
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        upstream, clone, branch = self._upstream_and_clone(tmp_path)
        # Two independent files so each finding's diff applies cleanly on the base. Pushed via
        # a THROWAWAY clone: the harness's own clone has both origin urls neutralized (that is
        # what it exists to reproduce), so it cannot push. Then refresh the working clone's
        # tracking ref by url, since `origin` there is `DISABLED_NO_PUSH`.
        seed = tmp_path / "seed2"
        subprocess.run(
            ["git", "clone", "-q", "-b", branch, str(upstream), str(seed)],
            cwd=seed.parent,
            check=True,
            capture_output=True,
        )
        (seed / "a.py").write_text("A = 0\n", encoding="utf-8")
        (seed / "b.py").write_text("B = 0\n", encoding="utf-8")
        self._git("add", "-A", cwd=seed)
        self._git("commit", "-qm", "two files", cwd=seed)
        self._git("push", "-q", "origin", branch, cwd=seed)
        self._git(
            "fetch",
            "-q",
            str(upstream),
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            cwd=clone,
        )

        store.write_json_atomic(
            store.config_path(),
            {"clone": str(clone), "branch": branch, "origin_url": str(upstream)},
        )
        q = store.pr_queue_dir()
        q.mkdir(parents=True, exist_ok=True)
        for fp, path, marker in (("fpA", "a.py", "A"), ("fpB", "b.py", "B")):
            (q / f"{fp}.diff").write_text(
                f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n"
                f"-{marker} = 0\n+{marker} = 1  # FINDING_{marker}\n",
                encoding="utf-8",
            )
            (q / f"{fp}.pr.md").write_text(f"# fix {marker}\n", encoding="utf-8")

        # FORCE the interleaving instead of hoping the scheduler produces it. A plain
        # two-thread race reproduced the bug only ~1 run in 3 (measured with the lock
        # removed), which is too flaky to be a regression test. `A` parks *after* staging its
        # diff — the exact window where B's `checkout -B` can carry A's change into B's
        # commit.
        #
        # A's park ends when B has PROVED the clone lock is contended, not when B
        # finishes: B cannot finish while A holds the lock, so waiting for B's
        # completion parks A against the very lock under test and always burns the
        # whole timeout. Anything that lets B past the gate — no lock, or a lock
        # that is not actually shared — leaves `b_blocked` unset, so A stays parked
        # in the window for the full timeout and the interleaving reproduces.
        results: dict[str, dict] = {}
        staged_a = threading.Event()
        b_blocked = threading.Event()
        contended: list[str] = []
        b_thread_name = "committer-B"
        real_apply = commit_mod.materialize_queued_diff
        real_clone_lock = commit_mod.clone_lock

        @contextlib.contextmanager
        def _clone_lock_reporting_contention():
            lock = real_clone_lock()
            if threading.current_thread().name == b_thread_name:
                # A non-blocking acquire that FAILS is proof A still holds this
                # exact lock. One that succeeds means the lock is not shared, so
                # say nothing and let A keep the window open.
                if lock.acquire(blocking=False):
                    lock.release()
                else:
                    contended.append(b_thread_name)
                    b_blocked.set()
            with lock:
                yield

        monkeypatch.setattr(commit_mod, "clone_lock", _clone_lock_reporting_contention)

        def _apply_with_park(**kwargs):
            out = real_apply(**kwargs)
            if kwargs.get("diff_text", "").find("FINDING_A") >= 0:
                staged_a.set()
                b_blocked.wait(timeout=30)
            return out

        monkeypatch.setattr(commit_mod, "materialize_queued_diff", _apply_with_park)

        def _run(fp: str) -> None:
            try:
                results[fp] = commit_mod.commit_finding(fp)
            finally:
                if fp == "fpB":
                    b_blocked.set()

        t_a = threading.Thread(target=_run, args=("fpA",))
        t_b = threading.Thread(target=_run, args=("fpB",), name=b_thread_name)
        t_a.start()
        staged_a.wait(timeout=30)  # A is now parked mid-mutation (or already done)
        t_b.start()
        for t in (t_b, t_a):
            t.join(timeout=90)
        b_blocked.set()

        # Whatever landed, no single commit may contain BOTH findings.
        log = subprocess.run(
            ["git", "-C", str(upstream), "log", "--format=%H", branch],
            cwd=upstream,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.split()
        for sha in log:
            body = subprocess.run(
                ["git", "-C", str(upstream), "show", "--format=", "--unified=0", sha],
                cwd=upstream,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).stdout
            assert not (
                "FINDING_A" in body and "FINDING_B" in body
            ), f"commit {sha[:8]} published both findings — the mutations interleaved"

        # And it held because B queued behind A, not because the scheduler happened
        # to keep them apart: A's park only ends once B has proved the lock is held.
        assert contended == [b_thread_name], "B never had to wait for A's clone lock"

    def test_the_clone_mutations_share_one_lock(self) -> None:
        """Structural: the draft route must hold the lock across its WHOLE sequence.

        Locking each helper individually would not help — the race is between the steps.
        """
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes

        assert "clone_lock" in inspect.getsource(commit_mod.commit_finding)
        draft_src = inspect.getsource(routes._handle_draft_pr)
        assert "clone_lock()" in draft_src, "the draft route can still race a commit"

    def test_one_click_safety_gate_precedes_every_git_mutation(self) -> None:
        """A failed safety check cannot be followed by Git rollback over that unsafe repo.

        The production route holds `clone_lock` and proves the runner idle. Validate once
        before materialization; a second post-commit check creates an impossible branch:
        trusting Git to reset is unsafe, while not resetting leaks the rejected commit.
        """
        import inspect

        src = inspect.getsource(commit_mod._commit_finding_locked)
        gate = src.index("if not _repository_is_isolated(clone):")
        assert gate < src.index("materialize_queued_diff(")
        assert gate < src.index('"commit", "-m", message')
        assert src.count("_repository_is_isolated(clone)") == 1

    def test_a_failed_push_leaves_no_commit_behind(self, tmp_path, monkeypatch) -> None:
        """One-click commit's LAST two exits returned with the commit still on the branch.

        `checkout_branch` prefers an existing local branch, so the next run would start from
        that unpushed commit and treat the queued change as already-landed baseline. The
        earlier failure points in `commit_finding` already reset; the no-pushable-remote and
        push-failed exits did not. Same class as the draft route's rollback, in the sibling
        path. Raised by the GPT review of this branch.
        """
        monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "data")
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        upstream, clone, branch = self._upstream_and_clone(tmp_path)
        store.write_json_atomic(
            store.config_path(),
            {"clone": str(clone), "branch": branch, "origin_url": str(upstream)},
        )
        q = store.pr_queue_dir()
        q.mkdir(parents=True, exist_ok=True)
        (q / "fp1.diff").write_text(
            "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def f():\n-    return 1\n+    return 2\n",
            encoding="utf-8",
        )
        (q / "fp1.pr.md").write_text("# fix: return 2\n", encoding="utf-8")

        before = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", f"origin/{branch}"],
            cwd=clone,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

        # Make the PUSH fail while everything before it succeeds.
        real_git = commit_mod._git

        def _git_failing_push(clone_arg, *args, **kw):
            if args and args[0] == "push":
                return subprocess.CompletedProcess(args, 1, "", "remote rejected")
            return real_git(clone_arg, *args, **kw)

        monkeypatch.setattr(commit_mod, "_git", _git_failing_push)

        out = commit_mod.commit_finding("fp1")
        assert out["ok"] is False and "push failed" in str(out["error"])

        head = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"],
            cwd=clone,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        assert head == before, "the unpushed commit was left on the branch"
        assert "return 1" in (clone / "app.py").read_text(encoding="utf-8")
        # The durable queue copy must survive so a retry still has the change.
        assert (q / "fp1.diff").is_file(), "the queued diff was lost on rollback"

    def test_the_draft_route_materializes_before_drafting(self) -> None:
        """Structural: the route must stage, and bail out when staging fails."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes

        src = inspect.getsource(routes._handle_draft_pr)
        # Match the CALL, not the prose: the docstring/comment names the helper too, so a
        # bare `.index("materialize_queued_diff")` finds the explanation rather than the code.
        call = src.index("commit_mod.materialize_queued_diff(")
        assert call < src.index("recipe.draft("), "the diff must be staged before drafting"
        assert 'staged.get("ok")' in src, "a failed staging must bail out, not draft anyway"

    def test_a_stale_local_ref_is_not_used_when_a_url_is_configured(
        self, tmp_path, monkeypatch
    ) -> None:
        """The base must come from the REMOTE, not from the clone's frozen tracking ref.

        `origin/<branch>` in a neutralized clone is a snapshot from clone time that will
        never update again. Committing on it would silently revert whatever landed
        upstream in the meantime — a lost-update bug that looks like a successful commit.
        """
        monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "data")
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        upstream, clone, branch = self._upstream_and_clone(tmp_path)

        # Someone else advances the branch AFTER our clone was made and frozen.
        other = tmp_path / "other"
        subprocess.run(
            ["git", "clone", "-q", "-b", branch, str(upstream), str(other)],
            cwd=other.parent,
            check=True,
            capture_output=True,
        )
        (other / "NEW.md").write_text("landed upstream\n", encoding="utf-8")
        self._git("add", "-A", cwd=other)
        self._git("commit", "-qm", "upstream work", cwd=other)
        self._git("push", "-q", "origin", branch, cwd=other)

        store.write_json_atomic(
            store.config_path(),
            {"clone": str(clone), "branch": branch, "origin_url": str(upstream)},
        )
        q = store.pr_queue_dir()
        q.mkdir(parents=True, exist_ok=True)
        (q / "fp2.diff").write_text(
            "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
            " def f():\n-    return 1\n+    return 3\n",
            encoding="utf-8",
        )

        out = commit_mod.commit_finding("fp2")
        assert out["ok"] is True, f"one-click commit failed: {out.get('error')}"

        # Both the other commit's file AND ours are present => we committed on the fresh
        # base. If we had used the frozen tracking ref, NEW.md would have been dropped.
        listing = subprocess.run(
            ["git", "-C", str(upstream), "ls-tree", "--name-only", branch],
            cwd=upstream,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert "NEW.md" in listing.stdout, "committed on a stale base — upstream work was lost"
        show = subprocess.run(
            ["git", "-C", str(upstream), "show", f"{branch}:app.py"],
            cwd=upstream,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert "return 3" in show.stdout


class TestDirectCommitQueueCopyIsReadable:
    """The direct-commit path wrote its description to ``<fp>.cr.md`` while every reader
    opens ``<fp>.pr.md`` (the `.cr.md → .pr.md` rename recorded in store.py never reached
    the spine writer). So a direct-committed fix's description was written to a filename
    nothing reads and silently never rendered in the CR-detail panel. Raised by the GPT
    review of this branch.
    """

    def test_the_queue_copy_lands_where_the_display_reader_looks(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.pr_pipeline import CrPipeline

        class _Recipe:
            pr_queue_dir = tmp_path / "queue"

        class _Profile:
            pr_recipe = _Recipe()

        pipe = CrPipeline.__new__(CrPipeline)
        import logging as _logging

        pipe.log = _logging.getLogger("test")
        pipe._write_queue_copy(
            # A minimal stub: _write_queue_copy reads only profile.pr_recipe.pr_queue_dir.
            profile=_Profile(),  # type: ignore[arg-type]
            fp="fpabc",
            summary="fix: the thing",
            description="why and how",
            diff="--- a\n+++ b\n",
        )

        qdir = tmp_path / "queue"
        # The name the readers (routes._handle_finding_detail, backend/commit.py) open.
        assert (qdir / "fpabc.pr.md").is_file(), "the description is not where the reader looks"
        assert not (qdir / "fpabc.cr.md").is_file(), "still writing the dead .cr.md name"
        assert (qdir / "fpabc.diff").is_file()
        assert "why and how" in (qdir / "fpabc.pr.md").read_text(encoding="utf-8")

    def test_the_spine_writers_and_backend_readers_agree_on_the_extension(self) -> None:
        """Structural: this drifted because the writer and reader named the file in two
        different modules. Pin that no non-test source writes the dead ``.cr.md`` name —
        the readers are fixed on ``.pr.md`` (store.py documents the rename)."""
        from pathlib import Path

        app = Path(__file__).resolve().parent.parent
        offenders = []
        for f in app.rglob("*.py"):
            rel = str(f.relative_to(app))
            if "tests/" in rel:
                continue
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                # A write to `<fp>.cr.md` — the extension no reader opens. Docstrings that
                # merely mention it are fine; a `.write_text` / f-string path is not.
                if ".cr.md" in line and (
                    ".write_text" in line or 'f"{' in line and "cr.md" in line
                ):
                    offenders.append(f"{rel}:{i}")
        assert offenders == [], f"these still write the unread .cr.md name: {offenders}"
