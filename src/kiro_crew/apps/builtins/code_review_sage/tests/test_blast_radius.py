"""Unit tests for the deterministic blast-radius extractor."""
import unittest

from sage_lib import blast_radius as BR  # noqa: N812
from sage_lib.store import DEFAULT_SENSITIVE_GLOBS

from tests.fixtures import SENSITIVE_FILES, SENSITIVE_TINY_FILES, SMALL_FILES


class TestGlobMatch(unittest.TestCase):
    def test_double_star_dir(self):
        self.assertTrue(BR.glob_match("src/auth/session.py", "**/auth/**"))

    def test_basename_prefix(self):
        self.assertTrue(BR.glob_match("src/auth/session.py", "**/session*"))

    def test_no_cross_segment_for_single_star(self):
        # "*auth*" must match within a single path segment only
        self.assertFalse(BR.glob_match("src/auth/session.py", "**/*auth*"))
        self.assertTrue(BR.glob_match("src/oauth_helper.py", "**/*auth*"))

    def test_extension(self):
        self.assertTrue(BR.glob_match("infra/main.tf", "**/*.tf"))

    def test_no_match(self):
        self.assertFalse(BR.glob_match("docs/readme.md", "**/auth/**"))


class TestSignals(unittest.TestCase):
    def setUp(self):
        self.files = SENSITIVE_FILES
        self.result = BR.analyze(self.files, DEFAULT_SENSITIVE_GLOBS)

    def test_loc_counts(self):
        s = self.result["signals"]
        self.assertEqual(s["loc_added"], 5)    # 3 in server + 2 in format
        self.assertEqual(s["loc_removed"], 3)  # 2 in server + 1 in format

    def test_guard_removals(self):
        # server.py removed an "if not ...:" and a "return" -> 2 guard removals
        self.assertEqual(self.result["signals"]["guard_removals"], 2)

    def test_import_fanout(self):
        # one "+import logging"
        self.assertEqual(self.result["signals"]["import_fanout"], 1)

    def test_sensitive_hit(self):
        hits = [h["path"] for h in self.result["signals"]["sensitive_hits"]]
        self.assertIn("src/kiro_crew/gateway/server.py", hits)
        self.assertNotIn("src/kiro_crew/util/format.py", hits)

    def test_rating_large(self):
        # sensitive touch + guard removals -> LARGE
        self.assertEqual(self.result["rating"], "LARGE")


class TestRatingBands(unittest.TestCase):
    def test_small(self):
        r = BR.analyze(SMALL_FILES, DEFAULT_SENSITIVE_GLOBS)
        self.assertEqual(r["rating"], "SMALL")

    def test_sensitive_tiny_is_medium(self):
        # one-line change but on a sensitive path, no guards -> MEDIUM
        r = BR.analyze(SENSITIVE_TINY_FILES, DEFAULT_SENSITIVE_GLOBS)
        self.assertEqual(len(r["signals"]["sensitive_hits"]), 1)
        self.assertEqual(r["rating"], "MEDIUM")

    def test_empty(self):
        r = BR.analyze([], DEFAULT_SENSITIVE_GLOBS)
        self.assertEqual(r["rating"], "SMALL")


if __name__ == "__main__":
    unittest.main()
