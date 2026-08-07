"""Unit tests for the V2 file-centric learning system (stage -> candidate ->
AI-merge consolidate -> learned-patterns.md)."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from sage_lib import learning as L  # noqa: N812
from sage_lib import store


def _pattern(title, repo="github.com/o/r", guidance="do the thing carefully", **kw):
    p = {"title": title, "scope": "common", "repo_identity": repo, "dimension": "security",
         "impact": "high", "guidance": guidance, "symptom_why": "it broke prod",
         "example": {"repo": repo, "ref": "#1", "text": "example"}}
    p.update(kw)
    return p


class TestRenderParse(unittest.TestCase):
    def test_roundtrip(self):
        p = _pattern("Reset guard flags on all paths", scope="common", repo=None,
                     provenance_repos=["r1", "r2"], added_at="2026-06-11T00:00:00Z")
        md = L.render_pattern(p)
        parsed = L.parse_patterns(md)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["title"], "Reset guard flags on all paths")
        self.assertEqual(parsed[0]["scope"], "common")
        self.assertEqual(parsed[0]["impact"], "high")
        self.assertEqual(parsed[0]["guidance"], "do the thing carefully")
        # Guidance-only format: no Symptom / Example lines are emitted.
        self.assertNotIn("**Symptom", md)
        self.assertNotIn("**Example", md)


class TestStaging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "apps" / "code-review-sage"
        store.ensure_layout(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_inadmissible_source_rejected(self):
        with self.assertRaises(ValueError):
            L.stage_learning(_pattern("X"), "sage_self", self.root)

    def test_stage_appends_to_candidate_not_common(self):
        L.stage_learning(_pattern("Validate identifiers before path use"), "fix_introduce", self.root)
        # candidate has it; the active (common) file has NO patterns until consolidation
        self.assertEqual(L.candidate_count(self.root), 1)
        self.assertEqual(len(L.list_patterns(root=self.root)), 0)
        self.assertTrue(L.candidate_file(self.root).exists())

    def test_stage_accumulates(self):
        L.stage_learning(_pattern("Lesson A"), "fix_introduce", self.root)
        L.stage_learning(_pattern("Lesson B"), "human_comment", self.root)
        titles = [p["title"] for p in L.list_candidate(self.root)]
        self.assertEqual(titles, ["Lesson A", "Lesson B"])

    def test_clear_candidate(self):
        L.stage_learning(_pattern("Lesson A"), "fix_introduce", self.root)
        self.assertTrue(L.clear_candidate(self.root))
        self.assertEqual(L.candidate_count(self.root), 0)
        self.assertFalse(L.candidate_file(self.root).exists())


class TestConsolidate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "apps" / "code-review-sage"
        store.ensure_layout(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_consolidate_replaces_common_and_clears_candidate(self):
        L.stage_learning(_pattern("Staged lesson"), "fix_introduce", self.root)
        merged = ("# Common learned patterns (cross-repo, warm start)\n\n"
                  + L.render_pattern(_pattern("Merged lesson", scope="common")))
        res = L.consolidate_apply(merged, self.root)
        self.assertTrue(res["ok"])
        self.assertEqual(res["consolidated_from_candidate"], 1)
        self.assertTrue(res["candidate_cleared"])
        # common now holds the merged content; candidate is gone
        pats = L.list_patterns(root=self.root)
        self.assertEqual([p["title"] for p in pats], ["Merged lesson"])
        self.assertEqual(L.candidate_count(self.root), 0)

    def test_consolidate_refuses_empty(self):
        L.stage_learning(_pattern("Staged"), "fix_introduce", self.root)
        res = L.consolidate_apply("   \n  ", self.root)
        self.assertFalse(res["ok"])
        # candidate preserved (not wiped) on refusal
        self.assertEqual(L.candidate_count(self.root), 1)

    def test_consolidate_records_audit(self):
        L.stage_learning(_pattern("S"), "fix_introduce", self.root)
        L.consolidate_apply("# x\n\n" + L.render_pattern(_pattern("M", scope="common")), self.root)
        log = store.data_dir(self.root) / "learnings" / "consolidations.jsonl"
        self.assertTrue(log.exists())
        self.assertIn("consolidated", log.read_text(encoding="utf-8"))


class TestSeed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "apps" / "code-review-sage"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_seed_populates_common(self):
        n = L.seed_common(self.root)
        self.assertEqual(n, len(L.DEFAULT_SEED_PATTERNS))
        pats = L.list_patterns("common", root=self.root)
        self.assertEqual(len(pats), n)
        # idempotent: no re-seed when patterns exist
        self.assertEqual(L.seed_common(self.root), 0)


if __name__ == "__main__":
    unittest.main()


class TestClearCandidateRespectsMultiplicity(unittest.TestCase):
    """Ids are a content hash of title|scope and staging appends without deduping,
    so the same id can appear twice. A set-membership clear deleted EVERY
    occurrence, including one staged after the consolidation snapshot that the
    merge never saw — the exact loss the only_ids path exists to prevent.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _stage(self, title, guidance):
        # title + guidance are the whole persisted rule; `body` is not rendered.
        return L.stage_learning(
            {"title": title, "scope": "common", "guidance": guidance},
            "fix_introduce", self.root)

    def test_a_duplicate_staged_after_the_snapshot_survives(self):
        self._stage("Guard the write path", "first")
        # What consolidation snapshots at dispatch: one element per staged entry.
        snapshot = [p["id"] for p in L.list_candidate(self.root)]
        self.assertEqual(len(snapshot), 1)
        # A later review re-learns the same lesson for the same scope: same id.
        self._stage("Guard the write path", "second, staged behind the merge")
        self.assertEqual(len(L.list_candidate(self.root)), 2)

        L.clear_candidate(self.root, only_ids=snapshot)

        left = L.list_candidate(self.root)
        self.assertEqual(len(left), 1, "the unseen duplicate must survive the clear")
        self.assertIn("behind the merge", left[0].get("guidance", ""))

    def test_every_snapshotted_occurrence_is_cleared(self):
        self._stage("Same lesson", "one")
        self._stage("Same lesson", "two")
        snapshot = [p["id"] for p in L.list_candidate(self.root)]
        self.assertEqual(len(snapshot), 2, "both occurrences are in the snapshot")

        L.clear_candidate(self.root, only_ids=snapshot)

        self.assertEqual(L.list_candidate(self.root), [],
                         "nothing staged after dispatch, so the file clears out")

    def test_an_unrelated_candidate_is_still_kept(self):
        self._stage("Consolidated lesson", "merged")
        snapshot = [p["id"] for p in L.list_candidate(self.root)]
        self._stage("A different lesson", "untouched")

        L.clear_candidate(self.root, only_ids=snapshot)

        left = L.list_candidate(self.root)
        self.assertEqual([p["title"] for p in left], ["A different lesson"])


class TestStagingRefusesPlantedLinks(unittest.TestCase):
    """A worker-reachable store is read through the no-link guard, not `read_text`."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_staging_does_not_follow_a_planted_symlink(self):
        secret = self.root / "credentials"
        secret.write_text("aws_secret_access_key = SHOULD-NEVER-BE-READ\n",
                          encoding="utf-8")

        # Prime the namespace, then swap the candidate file for a link to the secret.
        L.stage_learning(_pattern("first pattern"), "fix_introduce", self.root)
        cf = L.candidate_file(self.root, None)
        cf.unlink()
        cf.symlink_to(secret)

        L.stage_learning(_pattern("second pattern"), "fix_introduce", self.root)

        # The guard refuses the read, so the link's target is not appended -- and the
        # file the app rewrites is a regular file again, not the credential store.
        body = cf.read_text(encoding="utf-8")
        self.assertNotIn("SHOULD-NEVER-BE-READ", body)
        self.assertIn("second pattern", body)
        # The secret itself is untouched: staging never wrote through the link.
        self.assertIn("SHOULD-NEVER-BE-READ", secret.read_text(encoding="utf-8"))


class TestCandidateLockSpansProcesses(unittest.TestCase):
    """Learnings staged by concurrent review PROCESSES must not overwrite each other."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_concurrent_processes_both_land(self):
        import subprocess
        import sys

        # Each child stages one learning; a process-local lock would let the two
        # read the same "before" text and the last atomic write would drop one.
        prog = (
            "import sys; sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "from sage_lib import learning as L\n"
            "L.stage_learning({'id': sys.argv[2], 'title': sys.argv[2],\n"
            "                  'body': 'b', 'multiplicity': 1},\n"
            "                 'fix_introduce', Path(sys.argv[1]))\n"
        ) % str(Path(L.__file__).parent.parent)

        procs = [
            subprocess.Popen([sys.executable, "-c", prog, str(self.root), f"L{i}"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for i in range(6)
        ]
        for pr in procs:
            out, err = pr.communicate(timeout=60)
            self.assertEqual(pr.returncode, 0, err.decode()[-400:])

        staged = {str(e.get("title")) for e in L.list_candidate(self.root)}
        self.assertEqual(staged, {f"L{i}" for i in range(6)},
                         "every process's learning must survive")


class TestGuardedCandidateReads(unittest.TestCase):
    """Every candidate read goes through the no-link guard, not `read_text`."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _plant(self):
        """Point the catalog at a file whose contents PARSE as catalog entries.

        The link target's contents are attacker-chosen, so the faithful plant is one
        that survives `parse_patterns` -- raw credential text would be dropped as
        non-conforming and an unguarded read would look harmless."""
        secret = self.root / "credentials"
        secret.write_text(L.render_pattern(_pattern("NEVER-READ")) + "\n",
                          encoding="utf-8")
        L.stage_learning(_pattern("real"), "fix_introduce", self.root)
        cf = L.candidate_file(self.root, None)
        cf.unlink()
        cf.symlink_to(secret)
        return cf

    def test_list_candidate_does_not_publish_a_planted_link(self):
        """This feeds the dashboard, so an unguarded read is an egress path."""
        self._plant()
        rendered = repr(L.list_candidate(self.root))
        self.assertNotIn("NEVER-READ", rendered)

    def test_selective_clear_does_not_reserialize_a_planted_link(self):
        cf = self._plant()
        L.clear_candidate(self.root, None, only_ids=["nope"])
        # Refused read -> no entries -> nothing kept -> the catalog is removed. What
        # matters is that the link's target was never written back into it.
        body = cf.read_text(encoding="utf-8") if cf.exists() else ""
        self.assertNotIn("NEVER-READ", body)
        # And the clear did not write through the link to the secret either.
        self.assertIn("NEVER-READ",
                      (self.root / "credentials").read_text(encoding="utf-8"))


class TestCandidateLockRefusesPlantedLockFile(unittest.TestCase):
    """The lock file is opened, so it is also an attack surface."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _lock_path(self):
        ns_dir = self.root / "data" / "learnings" / "common"
        ns_dir.mkdir(parents=True, exist_ok=True)
        return ns_dir / "candidate.md.lock"

    def test_a_symlinked_lock_file_does_not_truncate_its_target(self):
        victim = self.root / "precious.txt"
        victim.write_text("KEEP-ME\n", encoding="utf-8")
        self._lock_path().symlink_to(victim)

        with self.assertRaises(OSError):
            L.stage_learning(_pattern("x"), "fix_introduce", self.root)

        self.assertEqual(victim.read_text(encoding="utf-8"), "KEEP-ME\n",
                         "acquiring the lock must not write through a planted link")

    def test_a_hardlinked_lock_file_is_refused(self):
        """O_NOFOLLOW does not catch a hardlink -- the link count does."""
        victim = self.root / "precious.txt"
        victim.write_text("KEEP-ME\n", encoding="utf-8")
        os.link(victim, self._lock_path())

        with self.assertRaises(OSError):
            L.stage_learning(_pattern("x"), "fix_introduce", self.root)

        self.assertEqual(victim.read_text(encoding="utf-8"), "KEEP-ME\n")

    def test_the_ordinary_path_still_takes_the_lock(self):
        """The guard must not wedge staging shut."""
        out = L.stage_learning(_pattern("ordinary"), "fix_introduce", self.root)
        self.assertTrue(out.get("ok"), out)


class TestConsolidationSnapshot(unittest.TestCase):
    """The pre-merge ruleset survives an apply that silently drops rules."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        store.ensure_layout(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _live(self):
        return L.common_file(self.root, None)

    def test_lossy_merge_is_recoverable_from_the_snapshot(self):
        # Two rules go in; the "merge" returns one. It is non-empty and parses, so every
        # guard in consolidate_apply passes and the write lands. The dropped rule exists
        # nowhere else -- consolidations.jsonl records counts, not content.
        keep = L.render_pattern(_pattern("Keep quotes out of shell arguments"))
        lose = L.render_pattern(_pattern("Close every file handle you open"))
        L.consolidate_apply("# Patterns\n\n" + keep + "\n" + lose, self.root)
        self.assertIn("Close every file handle", self._live().read_text(encoding="utf-8"))

        res = L.consolidate_apply("# Patterns\n\n" + keep, self.root)
        self.assertTrue(res["ok"])
        self.assertNotIn("Close every file handle",
                         self._live().read_text(encoding="utf-8"))

        backup = self._live().with_name(self._live().name + ".pre-consolidation")
        self.assertTrue(backup.exists(), "the pre-merge ruleset was not preserved")
        recovered = backup.read_text(encoding="utf-8")
        self.assertIn("Close every file handle", recovered)
        self.assertIn("Keep quotes out of shell arguments", recovered)

    def test_the_snapshot_path_is_reported_to_the_caller(self):
        # An operator who notices the loss has to be able to find the copy.
        first = L.render_pattern(_pattern("Prefer explicit timeouts"))
        L.consolidate_apply("# Patterns\n\n" + first, self.root)
        res = L.consolidate_apply("# Patterns\n\n" + first, self.root)
        self.assertTrue(res["ok"])
        self.assertTrue(res["backup"].endswith(".pre-consolidation"))
        self.assertTrue(Path(res["backup"]).exists())

    def test_nothing_to_snapshot_leaves_no_backup(self):
        # A catalog with no rules yet has nothing a merge could lose, so no copy is
        # taken. The apply still succeeds and the caller sees the absent key.
        res = L.consolidate_apply(
            "# Patterns\n\n" + L.render_pattern(_pattern("Start from something")),
            self.root,
        )
        self.assertTrue(res["ok"])
        self.assertNotIn("backup", res)


class TestCandidateLockPortability:
    """The lock must work on a platform without O_NOFOLLOW, and still refuse a
    symlink there."""

    def test_lock_works_when_the_platform_lacks_o_nofollow(self, tmp_path, monkeypatch):
        # Windows has no os.O_NOFOLLOW; naming it unconditionally raised
        # AttributeError and took down every staging / consolidation call.
        monkeypatch.delattr(L.os, "O_NOFOLLOW", raising=False)
        with L._candidate_lock(tmp_path, "default"):
            pass
        lock = L._namespace_dir("default", tmp_path) / "candidate.md.lock"
        assert lock.is_file()

    def test_symlink_is_still_refused_without_the_flag(self, tmp_path, monkeypatch):
        monkeypatch.delattr(L.os, "O_NOFOLLOW", raising=False)
        ns = L._namespace_dir("default", tmp_path)
        ns.mkdir(parents=True, exist_ok=True)
        victim = tmp_path / "victim.txt"
        victim.write_text("precious", encoding="utf-8")
        (ns / "candidate.md.lock").symlink_to(victim)
        raised = False
        try:
            with L._candidate_lock(tmp_path, "default"):
                pass
        except OSError:
            raised = True
        assert raised, "a symlinked lock path must be refused"
        # The point of the refusal: the pointed-at file is untouched.
        assert victim.read_text(encoding="utf-8") == "precious"
