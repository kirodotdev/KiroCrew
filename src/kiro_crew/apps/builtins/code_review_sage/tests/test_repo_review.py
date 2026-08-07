"""Tests for the repo-review feature: repo-URL parsing, open-PR enumeration
(gh CLI, mocked), and the durable reviewed-index dedup store."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sage_lib import adapters, pipeline, results, review_driver, store

from kiro_crew import platform_compat


class TestParseRepoUrl(unittest.TestCase):
    def test_plain_repo_url(self):
        self.assertEqual(adapters.parse_repo_url("https://github.com/octo/hello"),
                         ("octo", "hello"))

    def test_trailing_git_and_slash(self):
        self.assertEqual(adapters.parse_repo_url("https://github.com/octo/hello.git"),
                         ("octo", "hello"))
        self.assertEqual(adapters.parse_repo_url("https://github.com/octo/hello/"),
                         ("octo", "hello"))

    def test_www_host_allowed(self):
        self.assertEqual(adapters.parse_repo_url("https://www.github.com/o/r"),
                         ("o", "r"))

    def test_rejects_non_github_host(self):
        # github.com in the PATH (not the host) must be rejected (SSRF/allowlist).
        with self.assertRaises(adapters.UnsupportedPlatform):
            adapters.parse_repo_url("https://evil.example/github.com/o/r")

    def test_rejects_missing_repo_segment(self):
        with self.assertRaises(adapters.AdapterParseError):
            adapters.parse_repo_url("https://github.com/octo")

    def test_rejects_empty(self):
        with self.assertRaises(adapters.UnsupportedPlatform):
            adapters.parse_repo_url("")

    def test_rejects_pr_url(self):
        # A PR URL is not a repo URL — route the user to the paste flow.
        with self.assertRaises(adapters.AdapterParseError):
            adapters.parse_repo_url("https://github.com/o/r/pull/5")

    def test_rejects_bad_segment_chars(self):
        with self.assertRaises(adapters.AdapterParseError):
            adapters.parse_repo_url("https://github.com/../r")


class TestListOpenPrs(unittest.TestCase):
    def _cp(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(
            args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)

    def test_parses_jsonl(self):
        jsonl = "\n".join([
            json.dumps({"url": "https://github.com/o/r/pull/1", "number": 1,
                        "head_sha": "abc", "title": "one", "author": "ann",
                        "updated_at": "2026-07-01T00:00:00Z", "draft": False}),
            json.dumps({"url": "https://github.com/o/r/pull/2", "number": 2,
                        "head_sha": "def", "title": "two"}),
            "",  # trailing blank line tolerated
        ])
        with patch.object(pipeline.subprocess, "run", return_value=self._cp(stdout=jsonl)):
            prs = pipeline.list_open_prs("o", "r")
        self.assertEqual(len(prs), 2)
        self.assertEqual(prs[0], {"url": "https://github.com/o/r/pull/1", "number": 1,
                                  "head_sha": "abc", "title": "one", "author": "ann",
                                  "updated_at": "2026-07-01T00:00:00Z", "draft": False})
        # Fields absent from the payload degrade to empty/false, never KeyError —
        # the picker renders them directly.
        self.assertEqual(prs[1]["author"], "")
        self.assertFalse(prs[1]["draft"])

    def test_uses_list_argv_no_shell(self):
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw
            return self._cp(stdout="")
        with patch.object(pipeline.subprocess, "run", side_effect=fake_run):
            pipeline.list_open_prs("o", "r")
        self.assertIsInstance(captured["argv"], list)     # never a shell string
        self.assertNotIn("shell", captured["kw"])         # never shell=True
        # argv[0] is a VALIDATED absolute gh path (shared with the dashboard PR
        # panel's resolver), deliberately not a bare "gh" off PATH.
        self.assertTrue(os.path.isabs(captured["argv"][0]), captured["argv"][0])
        self.assertEqual(os.path.basename(captured["argv"][0]), "gh")
        self.assertEqual(captured["argv"][1:3],
                         ["api", "repos/o/r/pulls?state=open&per_page=100"])

    def test_nonzero_exit_raises(self):
        with patch.object(pipeline.subprocess, "run",
                          return_value=self._cp(returncode=1, stderr="gh: not logged in")):
            with self.assertRaises(RuntimeError) as ctx:
                pipeline.list_open_prs("o", "r")
        self.assertIn("not logged in", str(ctx.exception))

    def test_missing_gh_raises(self):
        with patch.object(pipeline.subprocess, "run", side_effect=FileNotFoundError()):
            with self.assertRaises(RuntimeError):
                pipeline.list_open_prs("o", "r")

    def test_timeout_raises(self):
        with patch.object(pipeline.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("gh", 60)):
            with self.assertRaises(RuntimeError):
                pipeline.list_open_prs("o", "r")

    def test_unparseable_nonempty_output_raises(self):
        # gh exit 0 but non-empty, non-JSONL output must NOT masquerade as "no PRs".
        with patch.object(pipeline.subprocess, "run",
                          return_value=self._cp(stdout="{\n  \"url\": \"x\"\n}\n")):
            with self.assertRaises(RuntimeError):
                pipeline.list_open_prs("o", "r")

    def test_truly_empty_output_returns_empty(self):
        with patch.object(pipeline.subprocess, "run", return_value=self._cp(stdout="")):
            self.assertEqual(pipeline.list_open_prs("o", "r"), [])


class TestReviewedIndex(unittest.TestCase):
    def test_roundtrip_and_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "apps" / "code-review-sage"
            store.ensure_layout(root)
            self.assertEqual(results.read_reviewed(root), {})   # missing -> {}

            results.write_reviewed(
                {"GH-o-r-1": {"head_sha": "abc", "reviewed_at": "t0", "run_id": "R1"}}, root)
            idx = results.read_reviewed(root)
            self.assertEqual(idx["GH-o-r-1"]["head_sha"], "abc")

            # mark_reviewed upserts without clobbering existing keys
            results.mark_reviewed(
                {"GH-o-r-2": {"head_sha": "def", "reviewed_at": "t1", "run_id": "R2"}}, root)
            idx = results.read_reviewed(root)
            self.assertEqual(set(idx), {"GH-o-r-1", "GH-o-r-2"})

            # updating an existing key overwrites it (re-review at a new head)
            results.mark_reviewed(
                {"GH-o-r-1": {"head_sha": "xyz", "reviewed_at": "t2", "run_id": "R3"}}, root)
            idx = results.read_reviewed(root)
            self.assertEqual(idx["GH-o-r-1"]["head_sha"], "xyz")

    @unittest.skipUnless(
        platform_compat.IS_POSIX,
        "POSIX mode bits are unobservable on Windows: the owner-only lockdown there is an "
        "ACL (platform_compat.restrict_to_owner), and st_mode always reports 0o666.",
    )
    def test_index_file_is_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "apps" / "code-review-sage"
            store.ensure_layout(root)
            p = results.write_reviewed({"GH-o-r-1": {"head_sha": "abc"}}, root)
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_corrupt_index_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "apps" / "code-review-sage"
            store.ensure_layout(root)
            results.reviewed_path(root).write_text("{not json", encoding="utf-8")
            self.assertEqual(results.read_reviewed(root), {})


class TestReviewedKeyCollision(unittest.TestCase):
    """Two repos differing only by '-' vs '_' with the same PR number must NOT
    share one durable reviewed-index key. The filesystem-safe change-id sanitizes
    '-'->'_' (so it CAN collide); the reviewed key must not."""

    def test_change_id_collides_but_reviewed_key_does_not(self):
        u1 = "https://github.com/acme/service-api/pull/5"
        u2 = "https://github.com/acme/service_api/pull/5"
        # Lossy change-id (also a filename) collapses '-'->'_': the two collide.
        self.assertEqual(review_driver.change_id_for(u1),
                         review_driver.change_id_for(u2))
        # Collision-free reviewed key keeps distinct repos distinct.
        self.assertNotEqual(review_driver.reviewed_key_for(u1),
                            review_driver.reviewed_key_for(u2))

    def test_reviewed_key_case_insensitive_canonical(self):
        self.assertEqual(
            review_driver.reviewed_key_for("https://github.com/Acme/Repo/pull/7"),
            review_driver.reviewed_key_for("https://github.com/acme/repo/pull/7"),
        )

    def test_reviewed_key_format(self):
        self.assertEqual(
            adapters.github_review_key("Octo", "Hello-World", 42),
            "github.com/octo/hello-world#42",
        )


class TestMaxConcurrentDefault(unittest.TestCase):
    def test_default_config_has_max_concurrent(self):
        self.assertEqual(store.DEFAULT_CONFIG["review"]["max_concurrent"], 5)


if __name__ == "__main__":
    unittest.main()
