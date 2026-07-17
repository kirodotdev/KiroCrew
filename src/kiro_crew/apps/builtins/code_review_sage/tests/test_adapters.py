"""Unit tests for the source adapters (GitHub + platform detection)."""
import json
import unittest

from sage_lib import adapters as A  # noqa: N812

from tests.fixtures import GITHUB_PAYLOAD


class TestPlatformDetection(unittest.TestCase):
    def test_github(self):
        self.assertEqual(A.detect_platform("https://github.com/org/repo/pull/5"), "github")

    def test_crux_link_unsupported(self):
        with self.assertRaises(A.UnsupportedPlatform):
            A.detect_platform("https://code.amazon.com/reviews/CR-12345678")

    def test_unsupported(self):
        with self.assertRaises(A.UnsupportedPlatform):
            A.detect_platform("ftp://example.com/x")

    def test_empty(self):
        with self.assertRaises(A.UnsupportedPlatform):
            A.detect_platform("")


class TestFailFast(unittest.TestCase):
    def test_garbage_json(self):
        with self.assertRaises(A.AdapterParseError):
            A.parse_github_payload("{not json", link="https://github.com/o/r/pull/1")

    def test_non_object(self):
        with self.assertRaises(A.AdapterParseError):
            A.parse_github_payload(json.dumps([1, 2, 3]), link="https://github.com/o/r/pull/1")

    def test_empty_payload(self):
        with self.assertRaises(A.AdapterParseError):
            A.parse_github_payload({}, link="https://github.com/o/r/pull/1")

    def test_github_normalize_routes(self):
        t = A.normalize("https://github.com/org/repo/pull/5",
                        {"number": 5, "body": "hello", "html_url":
                         "https://github.com/org/repo/pull/5"})
        self.assertEqual(t.platform, "github")
        self.assertEqual(t.change_id, "GH-org-repo-5")


class TestGithubParse(unittest.TestCase):
    def setUp(self):
        self.t = A.parse_github_payload(GITHUB_PAYLOAD)

    def test_identity(self):
        self.assertEqual(self.t.platform, "github")
        self.assertEqual(self.t.change_id, "GH-kiro_team-kiro_cli-3361")
        self.assertEqual(self.t.repo_identity, "github.com/kiro-team/kiro-cli")
        self.assertEqual(self.t.url, "https://github.com/kiro-team/kiro-cli/pull/3361")

    def test_metadata(self):
        self.assertEqual(self.t.author, "zejiangg")
        self.assertEqual(self.t.target_branch, "main")
        # head SHA is the commit_id used to anchor draft comments.
        self.assertEqual(self.t.revision, "fb58081a1c0ffee0000000000000000000000000")
        self.assertTrue(self.t.is_fix)
        self.assertEqual(self.t.linked_issue, "#3250")

    def test_files(self):
        self.assertEqual(len(self.t.files), 2)
        self.assertEqual(self.t.files[0]["path"], "crates/kiro-cli/src/cli/chat/mod.rs")
        self.assertIn("respawn", self.t.files[0]["diff"])

    def test_comments(self):
        self.assertEqual(len(self.t.existing_comments), 1)

    def test_parse_from_json_string(self):
        t = A.parse_github_payload(json.dumps(GITHUB_PAYLOAD))
        self.assertEqual(t.change_id, "GH-kiro_team-kiro_cli-3361")

    def test_owner_repo_from_link_when_missing(self):
        payload = {"number": 7, "body": "x", "files": [{"path": "a", "diff": "d"}]}
        t = A.parse_github_payload(payload, link="https://github.com/o/r/pull/7")
        self.assertEqual(t.change_id, "GH-o-r-7")
        self.assertEqual(t.repo_identity, "github.com/o/r")

    def test_number_ignores_github_db_id(self):
        # GitHub's `id` is the internal DB id (not the PR number). The change_id
        # must come from the URL's PR number so it matches the driver's _cid.
        payload = {"id": 1847293847, "body": "x", "files": [{"path": "a", "diff": "d"}]}
        t = A.parse_github_payload(payload, link="https://github.com/o/r/pull/5")
        self.assertEqual(t.change_id, "GH-o-r-5")

    def test_change_id_is_filesystem_safe(self):
        # A dot-containing org/repo must not yield path separators, and '-' must
        # be replaced (it is the delimiter) so segments can't collide.
        cid = A.github_change_id("my.org", "re/po", 9)
        self.assertNotIn("/", cid)
        self.assertEqual(cid, "GH-my.org-re_po-9")

    def test_change_id_no_owner_repo_collision(self):
        # The regression the delimiter fix prevents: different owner/repo pairs
        # must NOT map to the same change_id (would collide result files).
        a = A.github_change_id("a-b", "c", 1)
        b = A.github_change_id("a", "b-c", 1)
        self.assertEqual((a, b), ("GH-a_b-c-1", "GH-a-b_c-1"))
        self.assertNotEqual(a, b)

    def test_fail_fast_no_files_no_desc(self):
        with self.assertRaises(A.AdapterParseError):
            A.parse_github_payload({"number": 1},
                                   link="https://github.com/o/r/pull/1")

    def test_fail_fast_no_identity(self):
        with self.assertRaises(A.AdapterParseError):
            A.parse_github_payload({"body": "x", "files": [{"path": "a", "diff": "d"}]})


if __name__ == "__main__":
    unittest.main()
