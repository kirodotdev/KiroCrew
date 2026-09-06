"""Tests for the repo-review feature: repo-URL parsing, open-PR enumeration
(gh CLI, mocked), and the durable reviewed-index dedup store."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
from sage_lib import adapters, pipeline, results, review_driver, store

from kiro_crew import platform_compat

_needs_gh = pytest.mark.skipif(shutil.which("gh") is None, reason="gh CLI not installed")


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

    def test_rejection_names_the_accepted_hosts(self):
        # The error must say what IS accepted now that the set is configurable.
        with self.assertRaises(adapters.UnsupportedPlatform) as ctx:
            adapters.parse_repo_ref("https://evil.example/o/r", config={})
        self.assertIn("github.com", str(ctx.exception))

    def test_parse_repo_ref_accepts_configured_ghe_host(self):
        cfg = {"github_hosts": ["github.com", "acme.ghe.com"]}
        self.assertEqual(
            adapters.parse_repo_ref("https://acme.ghe.com/octo/hello", config=cfg),
            ("acme.ghe.com", "octo", "hello"))
        # Spoofable shapes stay rejected (exact parsed-hostname match only).
        for url in ("https://notacme.ghe.com/o/r",
                    "https://acme.ghe.com.evil.example/o/r"):
            with self.assertRaises(adapters.UnsupportedPlatform):
                adapters.parse_repo_ref(url, config=cfg)


@_needs_gh
class TestListOpenPrs(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(pipeline.discovery, "gh_bin", return_value="/resolved-gh/gh")
        self._mock_gh_bin = patcher.start()
        self.addCleanup(patcher.stop)

    def _cp(self, returncode=0, stdout="", stderr=""):
        """BYTES streams: ``run_gh`` decodes them itself, strictly as UTF-8."""
        return subprocess.CompletedProcess(
            args=["gh"],
            returncode=returncode,
            stdout=stdout.encode("utf-8") if isinstance(stdout, str) else stdout,
            stderr=stderr.encode("utf-8") if isinstance(stderr, str) else stderr,
        )

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
                                  "updated_at": "2026-07-01T00:00:00Z", "draft": False,
                                  "labels": []})
        # Fields absent from the payload degrade to empty/false, never KeyError —
        # the picker renders them directly.
        self.assertEqual(prs[1]["author"], "")
        self.assertFalse(prs[1]["draft"])
        # Same contract for labels: a PR with none, and a payload with the key
        # missing entirely, both read as an empty list. The picker narrows on this
        # without a presence check, so a missing key would be a TypeError there.
        self.assertEqual(prs[1]["labels"], [])

    def test_labels_are_projected_and_kept_in_order(self):
        """The whole point of the label filter: names arrive with the PR list,
        in GitHub's order, with no second request."""
        jsonl = json.dumps({
            "url": "https://github.com/o/r/pull/7", "number": 7,
            "head_sha": "abc", "title": "seven", "author": "ann",
            "updated_at": "2026-07-01T00:00:00Z", "draft": False,
            "labels": ["readiness: checking", "fork", "area: apps"],
        })
        with patch.object(pipeline.subprocess, "run", return_value=self._cp(stdout=jsonl)):
            prs = pipeline.list_open_prs("o", "r")
        self.assertEqual(prs[0]["labels"], ["readiness: checking", "fork", "area: apps"])

    def test_label_projection_is_requested_in_the_same_call(self):
        """No extra request and no repo-labels endpoint: `.labels` must be asked
        for inside the ONE `gh api` argv that lists the PRs, or the filter would
        cost a call per repo (or per PR) to populate."""
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return self._cp(stdout="")

        with patch.object(pipeline.subprocess, "run", side_effect=fake_run):
            pipeline.list_open_prs("o", "r")
        argv = captured["argv"]
        jq = argv[argv.index("--jq") + 1]
        self.assertIn("labels", jq)
        # One invocation, and the PR list path is the only path requested — a
        # second `gh` call would not show up in this argv at all, so the argv is
        # asserted to still be the pulls list.
        self.assertEqual(len([a for a in argv if a.startswith("repos/")]), 1)
        self.assertTrue(argv[2].startswith("repos/o/r/pulls?state=open"))

    def test_a_non_list_labels_value_narrows_to_empty_not_to_a_truthy_value(self):
        """Fail toward showing the PR. A malformed `labels` must not become a
        truthy value, because a labelled view narrows on membership — a wrong
        value there HIDES a pull request from review, which is the one direction
        that loses work rather than merely showing too much."""
        rows = [
            json.dumps({"url": "u1", "number": 1, "labels": "docs"}),        # string
            json.dumps({"url": "u2", "number": 2, "labels": {"name": "x"}}),  # object
            json.dumps({"url": "u3", "number": 3, "labels": None}),           # null
            json.dumps({"url": "u4", "number": 4, "labels": ["ok", "", 5, None]}),
        ]
        with patch.object(pipeline.subprocess, "run",
                          return_value=self._cp(stdout="\n".join(rows))):
            prs = pipeline.list_open_prs("o", "r")
        self.assertEqual([p["labels"] for p in prs[:3]], [[], [], []])
        # Non-strings and empty strings are dropped; a usable name survives.
        self.assertEqual(prs[3]["labels"], ["ok"])

    def test_spawn_carries_the_minimal_env(self):
        """The bare-subprocess regression class this suite exists to keep out:
        the spawn must hand the child the shared runner's minimal gh env, never
        the gateway's full environment (AWS/Slack/SSH secrets included)."""
        captured = {}

        def fake_run(argv, **kw):
            captured["kw"] = kw
            return self._cp(stdout="")

        with unittest.mock.patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "aws-secret",
                                                   "GH_TOKEN": "gho_token"}), \
                patch.object(pipeline.discovery, "gh_bin", return_value="/resolved-gh/gh"), \
                patch.object(pipeline.subprocess, "run", side_effect=fake_run):
            pipeline.list_open_prs("o", "r")
        env = captured["kw"].get("env")
        # A missing env= means the child inherits the FULL parent environment.
        self.assertIsInstance(env, dict)
        assert env is not None  # narrow for the type checker
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertEqual(env.get("GH_TOKEN"), "gho_token")

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
        # github.com is pinned EXPLICITLY too: omitting --hostname would let
        # the gh CLI's configured default host (GH_HOST) decide the instance.
        i = captured["argv"].index("--hostname")
        self.assertEqual(captured["argv"][i + 1], "github.com")

    def test_ghe_host_routes_to_that_instance(self):
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return self._cp(stdout="")
        with patch.object(pipeline.subprocess, "run", side_effect=fake_run):
            pipeline.list_open_prs("o", "r", host="acme.ghe.com")
        # A GHE repo is enumerated against ITS instance's API, not api.github.com.
        i = captured["argv"].index("--hostname")
        self.assertEqual(captured["argv"][i + 1], "acme.ghe.com")

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
