"""Ledger sync against a REAL git repo, not a mocked one.

Every bug these tests pin was invisible to mocked-git unit tests and fatal in practice.
A two-instance roundtrip against a bare remote found four, in the order they bit:

1. **The first push in a fresh process always failed.** The sandbox backend probe defers
   to a background thread on a cold cache and raises a self-described TRANSIENT error
   saying "retry"; ``push`` did not catch it, so the whole first sync errored for a
   condition that resolves in milliseconds.
2. **An instance with a local ledger could never pull.** ``git merge`` refuses when an
   untracked working-tree file would be overwritten, so any install that recorded even
   one lesson before its first pull was permanently unable to receive the team's.
3. **The second teammate to join could never merge.** Each instance runs its own
   ``git init``, so their histories are genuinely unrelated and git refuses outright.
   That is the ORDINARY multi-instance case.
4. **``rotation.yaml`` would never have been committed.** ``push`` ran
   ``git add ledger.jsonl`` only, so the on-call schedule — un-ignored specifically so it
   could sync — would have been committed nowhere and silently never reached anyone.

A FIFTH was found later, and NOT by these tests — by inspecting the owner's live install:

5. **The local repo was never on the configured branch.** ``git init`` ran with no ``-b``,
   so git picked ``master``, and ``branch()`` was used ONLY inside refspecs. Config said  # wokeignore:rule=master
   ``main``, ``.git/HEAD`` said ``master``, ``.git/config`` had no ``[branch]`` section.  # wokeignore:rule=master
   These tests missed it because they only ever asked whether the CONTENT arrived — and it
   did, through those explicit refspecs. What broke was everything around the content: no
   upstream, so the operator's own ``git pull`` / ``git push`` in the ledger directory both
   failed, in exactly the directory where a refused ``rotation.yaml`` has to be fixed by
   hand. "The bytes arrived" is not the whole contract.

These tests are slower than the mocked ones on purpose. The whole feature is "git moves
the text", so the thing worth testing is git.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not __import__('kiro_crew.sandbox', fromlist=['userns_available']).userns_available(),
    reason="requires unprivileged user namespaces (sandbox backend)",
)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, timeout=60, check=False
    )
    return proc.stdout.decode("utf-8", "replace")


class _TwoInstances(unittest.IsolatedAsyncioTestCase):
    """A bare remote plus two independent data homes — two teammates, one repo."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.remote = self.root / "remote.git"
        self.remote.mkdir()
        subprocess.run(
            ["git", "init", "--bare", "-q", "-b", "main", str(self.remote)],
            capture_output=True,
            timeout=60,
            check=True,
        )
        self.home_a = self.root / "a"
        self.home_b = self.root / "b"
        self.home_a.mkdir()
        self.home_b.mkdir()
        self._prev = os.environ.get("KIROCREW_HOME")
        # Snapshot the module table. ``_use`` evicts this app's modules to simulate two
        # separate processes, and WITHOUT restoring them the eviction leaks into every
        # later test in the same process: a sibling that had already imported
        # ``routes``/``ledger_sync`` ends up patching a stale module object while the
        # handler under test resolves a fresh one, so its mock silently never applies.
        # Observed exactly that — four unrelated test_routes failures that passed when
        # that file ran alone. A test that breaks other tests is a bug in the test.
        self._modules = dict(sys.modules)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        # Restore the exact table we started with: put back what we evicted, and drop
        # the replacements we imported, so the next test sees the process as it was.
        for name in list(sys.modules):
            if name not in self._modules:
                del sys.modules[name]
        sys.modules.update(self._modules)
        # `sys.modules` is NOT the only place a submodule is cached. Importing
        # ``kiro_crew.apps.manager`` also SETS ``manager`` as an attribute on the
        # ``kiro_crew.apps`` package object, and that package was never evicted — so
        # restoring the table left the parent still pointing at the replacement.
        #
        # The two are then read by different syntax: ``import a.b as c`` resolves through
        # the parent ATTRIBUTE, while ``from a.b import f`` goes through ``sys.modules``.
        # So a later test doing ``import kiro_crew.apps.manager as manager`` patched the
        # discarded copy while the code under test resolved the restored one, and the mock
        # silently never applied — two `test_app_bridges` failures that passed when that
        # file ran alone. Verified by asserting the two disagree before this loop and agree
        # after. A test that breaks other tests is a bug in the test.
        for name, module in self._modules.items():
            parent_name, _, leaf = name.rpartition(".")
            parent = self._modules.get(parent_name) if parent_name else None
            if parent is not None and getattr(parent, leaf, None) is not module:
                try:
                    setattr(parent, leaf, module)
                except AttributeError:  # pragma: no cover — read-only namespace
                    pass
        shutil.rmtree(self.root, ignore_errors=True)

    def _use(self, home: Path):
        """Point the app at one instance's data home and reload its modules.

        Module reload is required: ``ledger_sync`` resolves the repo root from the data
        home through ``app_data_dir``, which caches. Re-importing is the honest way to
        simulate two separate processes inside one test. ``tearDown`` restores the table.
        """
        os.environ["KIROCREW_HOME"] = str(home)
        # `bridges` must go WITH `manager`: it does `from kiro_crew.apps.manager import …`,
        # so it binds those functions BY VALUE at import time. Evicting only `manager` left
        # a surviving `bridges` holding references into the discarded module — and
        # `test_app_bridges` then patched attributes on the fresh `manager` while the code
        # under test still called the stale ones, so its mocks silently never applied
        # (`KeyError: 'someapp:srv'`, two tests, only when this file ran first).
        #
        # Same class as the eviction-leak this setUp/tearDown pair already documents, one
        # module further out: a partial eviction is worse than none, because it leaves two
        # live copies of one import graph. `tearDown` restores the exact table either way.
        for name in list(sys.modules):
            if (
                "ops_mission_control" in name
                or name.startswith("kiro_crew.apps.manager")
                or name.startswith("kiro_crew.apps.bridges")
            ):
                del sys.modules[name]
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, ledger_sync
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        ledger_sync.set_settings(remote_url=str(self.remote), branch_name="main", enabled=True)
        return ledger, ledger_sync, LedgerEntry


class TestRoundTrip(_TwoInstances):
    async def test_a_lesson_reaches_the_other_instance(self):
        """The whole point of the feature, end to end."""
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="DLQ fills on AccessDenied", fix="fix the policy"))
        self.assertEqual(await sync.sync_safely(direction="push"), "pushed")

        ledger, sync, entry = self._use(self.home_b)
        await sync.sync_safely(direction="pull")
        patterns = [e.pattern for e in ledger.read_entries()]
        self.assertIn("DLQ fills on AccessDenied", patterns)

    async def test_the_second_teammate_can_merge_unrelated_histories(self):
        """Bug 3: each instance runs its own `git init`, so roots are unrelated.

        Both write BEFORE either pulls — the ordinary case of two people installing
        independently. Git refuses this outright without --allow-unrelated-histories.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="A-only lesson here", fix="the A fix"))
        await sync.sync_safely(direction="push")

        ledger, sync, entry = self._use(self.home_b)
        ledger.upsert(entry.create(pattern="B-only lesson here", fix="the B fix"))
        detail = await sync.sync_safely(direction="pull")

        self.assertNotIn("unrelated histories", detail)
        patterns = sorted(e.pattern for e in ledger.read_entries())
        self.assertEqual(
            patterns,
            ["A-only lesson here", "B-only lesson here"],
            "the union must survive — neither side's work may be dropped",
        )

    async def test_a_local_ledger_does_not_block_the_first_pull(self):
        """Bug 2: 'Untracked working tree file would be overwritten by merge'."""
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="team lesson from A", fix="the shared fix"))
        await sync.sync_safely(direction="push")

        ledger, sync, entry = self._use(self.home_b)
        # B has its OWN untracked ledger before ever pulling.
        ledger.upsert(entry.create(pattern="local lesson on B", fix="the local fix"))
        detail = await sync.sync_safely(direction="pull")

        self.assertNotIn("would be overwritten", detail)
        self.assertNotIn("merge failed", detail)
        self.assertEqual(len(ledger.read_entries()), 2)

    async def test_concurrent_writers_converge_without_losing_an_entry(self):
        """The case a shared ledger exists for: both write, neither saw the other."""
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="shared baseline lesson", fix="common fix"))
        await sync.sync_safely(direction="push")

        ledger, sync, entry = self._use(self.home_b)
        await sync.sync_safely(direction="pull")
        ledger.upsert(entry.create(pattern="B-only throttling issue", fix="raise concurrency"))
        await sync.sync_safely(direction="push")

        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="A-only disk full issue", fix="rotate logs"))
        # A is stale: the push MUST be rejected rather than overwrite B's work.
        stale = await sync.sync_safely(direction="push")
        self.assertIn("push failed", stale, "a stale push must not clobber the remote")

        await sync.sync_safely(direction="pull")
        self.assertEqual(await sync.sync_safely(direction="push"), "pushed")
        a_final = sorted(e.pattern for e in ledger.read_entries())
        self.assertEqual(len(a_final), 3)

        ledger, sync, entry = self._use(self.home_b)
        await sync.sync_safely(direction="pull")
        b_final = sorted(e.pattern for e in ledger.read_entries())
        self.assertEqual(a_final, b_final, "both instances must converge on the same ledger")


class TestWhatGetsCommitted(_TwoInstances):
    async def test_the_rotation_schedule_syncs(self):
        """Bug 4: push staged only ledger.jsonl, so the schedule reached nobody."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (  # noqa: E501
            schedule_file,
        )

        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        path = schedule_file.schedule_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: octocat\n",
            encoding="utf-8",
        )
        await sync.sync_safely(direction="push")

        ledger, sync, entry = self._use(self.home_b)
        await sync.sync_safely(direction="pull")
        self.assertTrue(
            schedule_file.schedule_path().exists(),
            "rotation.yaml must reach teammates or the schedule is local-only",
        )
        self.assertIn("octocat", schedule_file.schedule_path().read_text(encoding="utf-8"))

    async def test_the_dispatch_index_is_never_pushed(self):
        """It is not merge-safe, and it is local state. Pushing it would corrupt peers."""
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        index = ledger.ledger_path().parent / "index.json"
        index.write_text('{"local": "state"}', encoding="utf-8")
        await sync.sync_safely(direction="push")

        tracked = _git(ledger.ledger_path().parent, "ls-files")
        self.assertIn("ledger.jsonl", tracked)
        self.assertNotIn("index.json", tracked)

    async def test_provider_config_is_never_pushed(self):
        """Config can name a log group an operator considers private."""
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        cfg = ledger.ledger_path().parent / "config.json"
        cfg.write_text('{"cloudwatch": {"log_groups": ["/private/thing"]}}', encoding="utf-8")
        await sync.sync_safely(direction="push")

        tracked = _git(ledger.ledger_path().parent, "ls-files")
        self.assertNotIn("config.json", tracked)


class TestFaultTolerance(_TwoInstances):
    async def test_an_unreachable_remote_is_survived(self):
        """Sync is a convenience; it must never raise into a caller."""
        ledger, sync, entry = self._use(self.home_a)
        sync.set_settings(remote_url=str(self.root / "does-not-exist.git"))
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        detail = await sync.sync_safely(direction="push")
        self.assertIsInstance(detail, str, "must return a string, never raise")

    async def test_unconfigured_sync_is_a_quiet_noop(self):
        ledger, sync, entry = self._use(self.home_a)
        sync.set_settings(enabled=False)
        self.assertEqual(await sync.sync_safely(direction="pull"), "")

    async def test_pushing_twice_is_idempotent(self):
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        self.assertEqual(await sync.sync_safely(direction="push"), "pushed")
        again = await sync.sync_safely(direction="push")
        self.assertEqual(again, "nothing to push")

    async def test_a_locally_committed_but_unpushed_entry_is_not_stranded(self):
        """A clean tree is not proof everything is shared.

        If a previous run committed and then failed to reach the remote, an early
        'nothing to push' on a clean tree would strand that commit forever.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        # Commit locally, but point at a dead remote so the push cannot land.
        sync.set_settings(remote_url=str(self.root / "dead.git"))
        await sync.sync_safely(direction="push")
        # Now the real remote comes back: the tree is clean but HEAD is unpushed.
        sync.set_settings(remote_url=str(self.remote))
        self.assertEqual(await sync.sync_safely(direction="push"), "pushed")

        ledger, sync, entry = self._use(self.home_b)
        await sync.sync_safely(direction="pull")
        self.assertEqual(len(ledger.read_entries()), 1, "the stranded entry arrived")


if __name__ == "__main__":
    unittest.main()


class TestScheduleConflicts(_TwoInstances):
    """A conflicted `rotation.yaml` is far more dangerous than a conflicted ledger.

    Conflict markers make the YAML unparseable, and an unparseable schedule means NO
    instance can tell whether it is on call. Found by a real three-teammate run through a
    private GitHub repo: an early sync pushed a schedule containing markers, and from then
    on every teammate's pull faithfully received a file that could not be parsed —
    `team=[]` for everyone, and under fail-open every instance re-armed. That is the exact
    double-claim a shared schedule exists to prevent, and no downstream conflict handling
    can recover it, because "theirs" is already corrupt.
    """

    def _schedule(self, ledger_mod, body: str):
        path = ledger_mod.ledger_path().parent / "rotation.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    async def test_push_refuses_a_conflicted_schedule(self):
        """The guard that stops one operator breaking the whole team's gating."""
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        self._schedule(
            ledger,
            "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n"
            "<<<<<<< HEAD\n    who: alice\n=======\n    who: bob\n>>>>>>> origin/main\n",
        )
        detail = await sync.sync_safely(direction="push")
        self.assertIn("refused", detail)
        self.assertIn("rotation.yaml", detail)

    async def test_a_clean_schedule_still_pushes(self):
        """The refusal must not become a blanket block on publishing a schedule."""
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        self._schedule(
            ledger, "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: alice\n"
        )
        self.assertEqual(await sync.sync_safely(direction="push"), "pushed")

    async def test_detector_is_scoped_to_the_schedule(self):
        """`has_conflict` is ledger-only; the schedule needed its own detector.

        Sharing one would have made a conflicted ledger trigger the schedule refusal and
        blocked every push.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        self._schedule(
            ledger, "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: alice\n"
        )
        self.assertFalse(sync.schedule_has_conflict())
        self._schedule(ledger, "<<<<<<< HEAD\nshifts: []\n=======\nshifts: []\n>>>>>>> x\n")
        self.assertTrue(sync.schedule_has_conflict())

    async def test_no_schedule_is_not_a_conflict(self):
        """The common single-user case must not read as conflicted."""
        _, sync, _ = self._use(self.home_a)
        self.assertFalse(sync.schedule_has_conflict())

    async def test_a_conflicting_schedule_edit_resolves_to_the_remote(self):
        """Two teammates editing the same shift: converge on what the team already sees.

        A shift is a single-owner fact, so there is no union to compute — one edit must
        lose. Taking the remote keeps every instance's view of who is on call identical,
        which is the property that makes the file usable as a lock.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        self._schedule(
            ledger, "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: alice\n"
        )
        await sync.sync_safely(direction="push")

        ledger, sync, entry = self._use(self.home_b)
        ledger.upsert(entry.create(pattern="b lesson to carry", fix="b fix"))
        self._schedule(ledger, "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: bob\n")
        detail = await sync.sync_safely(direction="pull")

        text = (ledger.ledger_path().parent / "rotation.yaml").read_text(encoding="utf-8")
        self.assertNotIn("<<<<<<<", text, "markers must never survive a pull")
        self.assertIn("alice", text, "the remote's version wins")
        self.assertIn("schedule conflict", detail)


class TestStatusMatchesWhatPushWillDo(_TwoInstances):
    """``status()`` is the only thing the operator sees. It must not contradict ``push``.

    Against a real repo because the point is agreement between two functions that read the
    same working tree: a mocked git can make either one say anything.
    """

    def _schedule(self, ledger_mod, body: str):
        path = ledger_mod.ledger_path().parent / "rotation.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    async def test_a_clean_repo_reports_syncing(self):
        """The baseline the conflict cases are measured against."""
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        self._schedule(
            ledger, "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: alice\n"
        )
        self.assertEqual(await sync.sync_safely(direction="push"), "pushed")

        status = sync.status()
        self.assertTrue(status["ready"])
        self.assertTrue(status["initialized"])
        self.assertFalse(status["conflict"])
        self.assertFalse(status["schedule_conflict"])
        self.assertIn("Syncing", status["detail"])

    async def test_status_reports_the_refusal_push_actually_makes(self):
        """The silent-stop this closes: refused pushes while the card read "Syncing …".

        The refusal reached the log and a SEL audit line only, and ``sync_safely`` swallows
        it into a warning, so the operator's single source of truth kept claiming sync was
        working. Nothing new reached the team for as long as the conflict lasted.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        self._schedule(
            ledger,
            "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n"
            "<<<<<<< HEAD\n    who: alice\n=======\n    who: bob\n>>>>>>> origin/main\n",
        )

        detail = await sync.sync_safely(direction="push")
        status = sync.status()

        self.assertIn("refused", detail)
        self.assertTrue(status["schedule_conflict"], "the refusal must be visible in Settings")
        self.assertIn("refused", status["detail"])
        self.assertNotIn("Syncing", status["detail"])

    async def test_a_conflicted_ledger_still_reports_as_publishing(self):
        """A ledger conflict must NOT read like the schedule refusal.

        ``push`` reconciles a conflicted ledger and publishes; saying "refused" here would
        send the operator hand-editing a file the app has already fixed.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        path = ledger.ledger_path()
        path.write_text(
            path.read_text(encoding="utf-8")
            + '<<<<<<< HEAD\n{"entry_id": "x"}\n=======\n>>>>>>> origin/main\n',
            encoding="utf-8",
        )

        status = sync.status()
        self.assertTrue(status["conflict"])
        self.assertFalse(status["schedule_conflict"])
        self.assertNotIn("refused", status["detail"])
        self.assertEqual(await sync.sync_safely(direction="push"), "pushed")


class TestTheLocalBranchIsTheConfiguredBranch(_TwoInstances):
    """A fifth bug of the same species, found in the owner's LIVE install, not by a test.

    `git init` ran with no `-b`, so git picked its own default (`master`) and nothing ever  # wokeignore:rule=master
    moved HEAD onto `ledger_sync_branch` or wrote tracking config. `branch()` was used ONLY
    inside refspecs — `fetch origin <b>`, `merge origin/<b>`, `push HEAD:<b>`,
    `rev-list origin/<b>..HEAD` — so sync worked *by accident of those refspecs* while local
    HEAD and the configured branch were permanently different refs. Config said `main`,
    `.git/HEAD` said `master`, and `.git/config` had no `[branch]` section at all.  # wokeignore:rule=master

    None of that showed up here, because these tests only ever asked whether the CONTENT
    arrived — and it did. What it cost was everything around the content: `status()` claimed
    "on branch main" while HEAD was elsewhere, and the operator's two obvious recovery
    commands both failed outright ("no tracking information for the current branch" /
    "the current branch master has no upstream branch"). That is not academic: the push  # wokeignore:rule=master
    guard REFUSES a conflicted `rotation.yaml` and tells the operator to fix it by hand, in
    exactly the directory where `git pull` did not work.
    """

    async def test_the_local_repo_ends_up_on_the_configured_branch(self):
        """The test that would have caught it. `git init` picks `master`; config says `main`."""  # wokeignore:rule=master
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        self.assertEqual(await sync.sync_safely(direction="push"), "pushed")

        root = ledger.ledger_path().parent
        self.assertEqual(
            _git(root, "branch", "--show-current").strip(),
            "main",
            "the repo must be ON the branch the operator configured, not git's default",
        )

    async def test_the_configured_branch_gets_an_upstream(self):
        """Without tracking, the operator's own `git pull` / `git push` both fail.

        That is the whole cost of the bug: a conflicted `rotation.yaml` must be resolved by
        hand in this directory (push refuses it), and the two commands anyone would reach
        for did not work.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        await sync.sync_safely(direction="push")

        root = ledger.ledger_path().parent
        self.assertEqual(_git(root, "config", "--get", "branch.main.remote").strip(), "origin")
        self.assertEqual(
            _git(root, "config", "--get", "branch.main.merge").strip(),
            "refs/heads/main",
            "`git branch -m` migrates .remote but leaves .merge on the OLD ref, so this "
            "has to be written explicitly after the rename",
        )
        self.assertEqual(
            _git(root, "rev-parse", "--abbrev-ref", "main@{upstream}").strip(),
            "origin/main",
            "git itself must agree the branch is tracked",
        )

    async def test_an_empty_remote_still_gets_an_aligned_branch(self):
        """The FIRST-sync state, which is also where the obvious fix would have failed.

        A fresh team repo has no commits, so `origin/main` does not exist and
        `git branch --set-upstream-to=origin/main` fails ("the requested upstream branch
        does not exist"); on an unborn local branch it fails differently ("no commit on
        branch 'main' yet"). Alignment must work before either exists, which is why it
        writes `branch.<n>.remote` / `.merge` directly.
        """
        _, sync, _ = self._use(self.home_a)
        # Pull first, against a remote that has nothing: the early-return path.
        detail = await sync.sync_safely(direction="pull")
        self.assertNotIn("failed", detail)

        root = sync._repo_root()
        self.assertEqual(_git(root, "branch", "--show-current").strip(), "main")
        self.assertEqual(_git(root, "config", "--get", "branch.main.remote").strip(), "origin")

    async def test_changing_the_branch_later_moves_the_local_repo_too(self):
        """The operator can change `ledger_sync_branch`; HEAD must follow.

        Before this, fetch/merge/push silently re-pointed at the new remote ref while HEAD
        kept accumulating on the old one — so HEAD carried both branches' history and the
        first push to the new branch either got rejected non-fast-forward or published the
        old branch's history onto it.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        await sync.sync_safely(direction="push")

        sync.set_settings(branch_name="team-ledger")
        await sync.sync_safely(direction="push")

        root = ledger.ledger_path().parent
        self.assertEqual(_git(root, "branch", "--show-current").strip(), "team-ledger")
        self.assertEqual(
            _git(root, "config", "--get", "branch.team-ledger.merge").strip(),
            "refs/heads/team-ledger",
        )

    async def test_alignment_leaves_a_dirty_tree_and_its_commit_alone(self):
        """Renaming must not touch content. `git checkout`/`switch` would.

        `git branch -m` keeps the same sha and does not touch the working tree; a checkout
        onto a divergent ref against a dirty tree auto-merges and can conflict — which here
        means corrupting the live ledger.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        await sync.sync_safely(direction="push")

        root = ledger.ledger_path().parent
        before = _git(root, "rev-parse", "HEAD").strip()
        # A local edit that has not been committed yet, plus an untracked file.
        ledger.upsert(entry.create(pattern="an uncommitted second lesson", fix="another fix"))
        (root / "scratch.txt").write_text("local only", encoding="utf-8")

        sync.set_settings(branch_name="renamed-branch")
        self.assertEqual(await sync._align_branch(), "")

        self.assertEqual(_git(root, "rev-parse", "HEAD").strip(), before, "the sha must not move")
        self.assertEqual(len(ledger.read_entries()), 2, "the uncommitted lesson must survive")
        self.assertTrue((root / "scratch.txt").exists())

    async def test_an_existing_divergent_branch_is_refused_not_overwritten(self):
        """`git branch -M` would DELETE it. Two lines of ledger work, silently collapsed.

        Refusing costs the operator one manual merge. Guessing costs a teammate's lesson,
        which is the exact outcome this change exists to prevent.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        await sync.sync_safely(direction="push")

        root = ledger.ledger_path().parent
        # A second branch holding a commit only IT has.
        _git(root, "branch", "other-work")
        other_sha = _git(root, "rev-parse", "other-work").strip()

        sync.set_settings(branch_name="other-work")
        reason = await sync._align_branch()
        self.assertIn("already exists", reason)
        self.assertEqual(_git(root, "branch", "--show-current").strip(), "main", "left in place")
        self.assertEqual(
            _git(root, "rev-parse", "other-work").strip(),
            other_sha,
            "the other branch and its commits must still be there",
        )

    async def test_a_detached_head_is_reported_and_left_alone(self):
        """A detached HEAD means a merge or rebase went sideways; moving refs can lose it."""
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        await sync.sync_safely(direction="push")

        root = ledger.ledger_path().parent
        _git(root, "checkout", "-q", "--detach", "HEAD")
        head_before = (root / ".git" / "HEAD").read_text(encoding="utf-8")

        reason = await sync._align_branch()
        self.assertIn("detached", reason)
        self.assertEqual(
            (root / ".git" / "HEAD").read_text(encoding="utf-8"),
            head_before,
            "nothing may be moved under a detached HEAD",
        )

    async def test_alignment_never_turns_into_a_sync_failure(self):
        """This fix must not be able to make the app worse than it was.

        Publishing has always worked through explicit refspecs, with or without a local
        branch. So even when alignment refuses, the push must still land.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        await sync.sync_safely(direction="push")

        root = ledger.ledger_path().parent
        _git(root, "checkout", "-q", "--detach", "HEAD")
        ledger.upsert(entry.create(pattern="a lesson recorded while detached", fix="a fix"))
        self.assertEqual(await sync.sync_safely(direction="push"), "pushed")

        ledger, sync, entry = self._use(self.home_b)
        await sync.sync_safely(direction="pull")
        patterns = [e.pattern for e in ledger.read_entries()]
        self.assertIn("a lesson recorded while detached", patterns)


class TestStatusDoesNotOverstateTheBranch(_TwoInstances):
    """`status()` is the only thing the operator sees, and it named a ref HEAD was not on.

    This is the UI-facing half of the bug: the card said "Syncing <url> on branch main" on
    an install whose HEAD was `master`. An operator who trusted it had no way to learn that  # wokeignore:rule=master
    their own `git pull` in that directory would fail.
    """

    async def test_an_aligned_repo_reports_a_match(self):
        """The baseline the mismatch cases are measured against."""
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        await sync.sync_safely(direction="push")

        status = sync.status()
        self.assertEqual(status["branch"], "main")
        self.assertEqual(status["local_branch"], "main")
        self.assertTrue(status["branch_matches"])
        self.assertFalse(status["detached"])
        self.assertIn("Syncing", status["detail"])

    async def test_a_mismatch_is_named_instead_of_claimed_to_be_syncing(self):
        """Reproduces the live install exactly: HEAD on `master`, config on `main`."""  # wokeignore:rule=master
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        await sync.sync_safely(direction="push")

        root = ledger.ledger_path().parent
        _git(root, "branch", "-m", "master")  # wokeignore:rule=master

        status = sync.status()
        self.assertEqual(status["branch"], "main", "the CONFIGURED branch")
        self.assertEqual(
            status["local_branch"], "master", "what .git/HEAD actually points at"
        )  # wokeignore:rule=master
        self.assertFalse(status["branch_matches"])
        self.assertNotIn(
            "Syncing git",
            status["detail"],
            "it must not claim to be syncing ON a branch this repo is not on",
        )
        self.assertIn(
            "master", status["detail"], "name the branch it is actually on"
        )  # wokeignore:rule=master

    async def test_a_detached_head_says_so(self):
        """A mismatch and a detached HEAD need DIFFERENT remedies, so they read differently.

        A mismatch the next sync repairs by itself; a detached HEAD is deliberately left for
        the operator, so telling them to wait would leave them waiting forever.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        await sync.sync_safely(direction="push")

        root = ledger.ledger_path().parent
        _git(root, "checkout", "-q", "--detach", "HEAD")

        status = sync.status()
        self.assertTrue(status["detached"])
        self.assertEqual(status["local_branch"], "", "a bare sha is not a branch name")
        self.assertFalse(status["branch_matches"])
        self.assertIn("detached", status["detail"])

    async def test_an_uninitialized_repo_is_not_reported_as_a_mismatch(self):
        """There is nothing yet to disagree with, and a warning here trains the operator
        to ignore the one field that means something."""
        _, sync, _ = self._use(self.home_a)
        status = sync.status()
        self.assertFalse(status["initialized"])
        self.assertTrue(status["branch_matches"])
        self.assertFalse(status["detached"])
        self.assertEqual(status["local_branch"], "")

    async def test_a_conflicted_ledger_does_not_hide_a_branch_mismatch(self):
        """The ledger-conflict sentence outranks the branch one, so it must not overstate.

        Its old wording ended "syncing <url> on branch <b>" — the same false claim, on a
        path that takes precedence.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        await sync.sync_safely(direction="push")
        root = ledger.ledger_path().parent
        _git(root, "branch", "-m", "master")  # wokeignore:rule=master
        path = ledger.ledger_path()
        path.write_text(
            path.read_text(encoding="utf-8")
            + '<<<<<<< HEAD\n{"entry_id": "x"}\n=======\n>>>>>>> x\n',
            encoding="utf-8",
        )

        status = sync.status()
        self.assertTrue(status["conflict"])
        self.assertFalse(status["branch_matches"])
        self.assertNotIn("on branch main.", status["detail"])

    async def test_an_option_like_branch_falls_back_instead_of_reaching_git(self):
        """`set_settings` bypasses the route's validation, and config.json is hand-editable.

        Verified hazards this guards: `git init -b '-x'` creates `refs/heads/-x`, and
        `git symbolic-ref HEAD refs/heads/--upload-pack=evil` succeeds with NO validation.
        """
        _, sync, _ = self._use(self.home_a)
        for bad in ("--upload-pack=evil", "-x", "main evil", "with\nnewline"):
            with self.subTest(branch=bad):
                sync.set_settings(branch_name=bad)
                self.assertEqual(sync.branch(), "main")


class TestPushRefusesCredentialMaterial(_TwoInstances):
    """`ledger.jsonl` is the ONE artifact this app publishes, so it gets two defences.

    `POST /ledger` redacts on the write path, which is where it belongs — the entry is on
    local disk and in the vector index long before any sync runs. This is the second
    layer, for what the first cannot reach: an entry written by an older build, or one
    that arrived by some path other than that route.

    The asymmetry is what justifies belt AND braces. Refusing costs one operator a push
    they fix by hand; publishing costs a history rewrite across every teammate's clone,
    and the secret is already fetched by then.
    """

    async def test_push_refuses_when_the_ledger_carries_a_credential(self):
        ledger, sync, entry = self._use(self.home_a)
        # Written directly, bypassing the route's redactor — which is precisely the case
        # this layer exists for.
        ledger.upsert(
            entry.create(
                pattern="cross-account assume-role denied",
                fix="aws sts assume-role --access-key AKIAIOSFODNN7EXAMPLE",
            )
        )
        detail = await sync.sync_safely(direction="push")
        self.assertIn("refused", detail)
        self.assertIn("ledger.jsonl", detail)

    async def test_the_refusal_names_lines_not_the_secret(self):
        """Reporting the matched text would copy the secret into the log and the console."""
        ledger, sync, entry = self._use(self.home_a)
        secret = "AKIAIOSFODNN7EXAMPLE"
        ledger.upsert(entry.create(pattern="assume-role denied", fix=f"key {secret}"))
        detail = await sync.sync_safely(direction="push")
        self.assertNotIn(secret, detail)

    async def test_push_refuses_a_provider_token_the_core_patterns_do_not_know(self):
        """The scan used ONLY `security.get_credential_patterns()`, and that is not a superset.

        Measured before fixing, the gap runs BOTH ways: the core patterns carry the AKIA/ASIA
        shapes but not a prefixed Datadog application key (`ddapp_…`), while this app's own
        `redact_tokens` catches `ddapp_…` and misses `AKIAIOSFODNN7EXAMPLE`. So either detector
        alone lets a real credential reach the team's shared remote, and the scan now takes the
        UNION. Review found it.

        Subtests so a future third shape is one line, and so a failure names WHICH shape leaked.
        """
        ledger, sync, entry = self._use(self.home_a)
        shapes = {
            "datadog application key": "ddapp_0123456789abcdef0123456789abcdef01234567",
            "bearer header": "curl -H 'Authorization: Bearer abcdefghijklmnop1234567890'",
        }
        for label, material in shapes.items():
            with self.subTest(shape=label):
                ledger.upsert(entry.create(pattern=f"probe {label}", fix=material))
                detail = await sync.sync_safely(direction="push")
                self.assertIn("refused", detail, f"{label} was published")
                self.assertNotIn(material, detail, "the refusal must not echo the secret")

    async def test_the_scan_stays_in_step_with_the_write_path(self):
        """Structural: the union must be read from the two shared detectors, not re-listed.

        A private regex copy here would drift the moment a provider shape is added to
        `redact_tokens` — the write path would redact it while the push guard kept publishing
        it, which is the silent half of the failure. Asserting on the CALLS keeps the two
        layers coupled by construction.
        """
        import inspect

        # Through `_use`, like every other test here: the module is re-imported per instance
        # (see its docstring), so a module-level reference would read a stale copy.
        _ledger, sync, _entry = self._use(self.home_a)
        source = inspect.getsource(sync._credential_bearing_lines)
        self.assertIn("get_credential_patterns", source)
        self.assertIn("redact_tokens", source)

    async def test_an_ordinary_lesson_still_pushes(self):
        """The scan must not become a blanket block on publishing knowledge.

        A `fix` naturally holds command shapes, so a scan that fires on ordinary ops prose
        would make the whole team-sync feature unusable — which is a worse outcome than
        the risk it guards, because an operator who cannot sync simply turns sync off.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(
            entry.create(
                pattern="checkout p99 latency breach",
                fix="drain the stuck SQS consumer, then scale the checkout ASG to 6",
            )
        )
        self.assertEqual(await sync.sync_safely(direction="push"), "pushed")

    async def test_a_missing_ledger_is_not_reported_as_leaky(self):
        """An unreadable ledger must not turn into an unexplained refusal."""
        _ledger, sync, _entry = self._use(self.home_a)
        self.assertEqual(sync._credential_bearing_lines(), [])
