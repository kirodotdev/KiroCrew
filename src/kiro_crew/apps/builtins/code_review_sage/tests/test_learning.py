"""Unit tests for the V2 file-centric learning system (stage -> candidate ->
AI-merge consolidate -> learned-patterns.md)."""
import hashlib
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


class TestPatternId(unittest.TestCase):
    """`pattern_id` is a content-derived identifier, not a security digest.

    It must stay deterministic and content-addressed, and it must not reach for a
    broken hash: SHA-1 here made a scanner read an identifier as a cryptographic
    use and report a high-severity alert on every PR touching this file.
    """

    def test_is_deterministic_and_content_addressed(self):
        a = L.pattern_id("Reset guard flags on all paths", "common")
        self.assertEqual(a, L.pattern_id("Reset guard flags on all paths", "common"))
        # Normalisation is part of the contract: case and surrounding whitespace
        # must not produce a second id for the same pattern.
        self.assertEqual(a, L.pattern_id("  reset GUARD flags on all paths  ", "common"))
        # Title and scope are both part of the identity.
        self.assertNotEqual(a, L.pattern_id("Reset guard flags on all paths", "repo"))
        self.assertNotEqual(a, L.pattern_id("Something else entirely", "common"))

    def test_is_a_16_char_hex_handle(self):
        pid = L.pattern_id("Reset guard flags on all paths", "common")
        self.assertEqual(len(pid), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in pid), pid)

    def test_does_not_use_a_broken_hash(self):
        """Pin the digest away from SHA-1/MD5 so the scanner finding cannot return."""
        payload = "reset guard flags on all paths|common".encode()
        self.assertEqual(L.pattern_id("Reset guard flags on all paths", "common"),
                         hashlib.sha256(payload).hexdigest()[:16])
        for broken in ("sha1", "md5"):
            self.assertNotEqual(
                L.pattern_id("Reset guard flags on all paths", "common"),
                hashlib.new(broken, payload).hexdigest()[:16],
                f"pattern_id must not be a truncated {broken} digest")

    def test_parsed_patterns_get_ids_recomputed(self):
        """Ids are derived on parse, never read back from the file.

        This is what makes changing the digest safe without a migration.
        """
        p = _pattern("Reset guard flags on all paths", repo=None,
                     added_at="2026-06-11T00:00:00Z")
        parsed = L.parse_patterns(L.render_pattern(p))[0]
        self.assertEqual(parsed["id"], L.pattern_id(p["title"], p["scope"]))
        # The rendered markdown carries no id of its own to go stale.
        self.assertNotIn(parsed["id"], L.render_pattern(p))


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
