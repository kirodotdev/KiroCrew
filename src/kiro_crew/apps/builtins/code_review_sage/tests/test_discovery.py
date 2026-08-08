"""Tests for ``sage_lib/discovery.py`` — the repo picker's gh-backed discovery
and the app-local pinned-repo list.

Two surfaces:
  * ``run_gh_json`` / ``list_contributed_repos`` — parse ``gh api`` output. These
    patch ``subprocess.run`` and ``gh_bin`` so no real ``gh`` is required, and
    lock in the argv-is-a-LIST / no-``shell=True`` contract, the JSONL parsing
    rules (skip blanks, raise on wholly-unparseable / non-zero exit, map an
    auth-failure stderr to ``GhSetupError``), and the contribution filtering /
    day-window / dedup / truncation behaviour.
  * ``read_repos`` / ``add_repo`` / ``remove_repo`` — the pinned list, exercised
    against a tmp ``KIROCREW_HOME`` for a real round-trip (idempotent +
    case-insensitive, newest-first, tolerant of a missing/corrupt file).
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import types
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from sage_lib import discovery, store  # noqa: E402  (app root added to sys.path above)


def _proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestRunGhJson(unittest.TestCase):
    def test_parses_jsonl_and_skips_blank_lines(self):
        out = ('{"type": "PushEvent", "repo": "a/b"}\n'
               '\n'
               '   \n'
               '{"type": "PullRequestEvent", "repo": "c/d"}\n')
        with unittest.mock.patch.object(discovery, "gh_bin", return_value="/usr/bin/gh"), \
             unittest.mock.patch.object(discovery.subprocess, "run",
                                        return_value=_proc(stdout=out)):
            rows = discovery.run_gh_json("users/x/events", jq=".[]")
        self.assertEqual([r["repo"] for r in rows], ["a/b", "c/d"])

    def test_uses_list_argv_without_shell(self):
        captured = {}

        def _fake_run(argv, *args, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _proc(stdout='{"repo": "a/b"}\n')

        with unittest.mock.patch.object(discovery, "gh_bin", return_value="/usr/bin/gh"), \
             unittest.mock.patch.object(discovery.subprocess, "run", side_effect=_fake_run):
            discovery.run_gh_json("users/x/events", jq=".[]")
        self.assertIsInstance(captured["argv"], list)
        self.assertEqual(captured["argv"][:3], ["/usr/bin/gh", "api", "users/x/events"])
        self.assertIn("--jq", captured["argv"])
        # never a shell string
        self.assertNotIn("shell", captured["kwargs"])
        self.assertNotEqual(captured["kwargs"].get("shell"), True)

    def test_raises_on_wholly_unparseable_output(self):
        with unittest.mock.patch.object(discovery, "gh_bin", return_value="/usr/bin/gh"), \
             unittest.mock.patch.object(discovery.subprocess, "run",
                                        return_value=_proc(stdout="not json\nstill not json")):
            with self.assertRaises(discovery.GhError):
                discovery.run_gh_json("users/x/events", jq=".[]")

    def test_raises_on_non_zero_exit(self):
        with unittest.mock.patch.object(discovery, "gh_bin", return_value="/usr/bin/gh"), \
             unittest.mock.patch.object(discovery.subprocess, "run",
                                        return_value=_proc(returncode=1, stderr="boom")):
            with self.assertRaises(discovery.GhError):
                discovery.run_gh_json("users/x/events", jq=".[]")

    def test_auth_failure_maps_to_setup_error(self):
        with unittest.mock.patch.object(discovery, "gh_bin", return_value="/usr/bin/gh"), \
             unittest.mock.patch.object(
                discovery.subprocess, "run",
                return_value=_proc(returncode=1,
                                   stderr="gh: To get started with GitHub CLI, "
                                          "please run: gh auth login")):
            with self.assertRaises(discovery.GhSetupError):
                discovery.run_gh_json("users/x/events", jq=".[]")


class TestListContributedRepos(unittest.TestCase):
    def test_filters_windows_dedups_and_counts(self):
        now = datetime.now(timezone.utc)
        events = [
            {"type": "PushEvent", "repo": "acme/api",
             "created_at": _iso(now - timedelta(days=1))},          # newest for acme/api
            {"type": "PushEvent", "repo": "acme/api",
             "created_at": _iso(now - timedelta(days=2))},          # older dup -> count 2
            {"type": "WatchEvent", "repo": "acme/watched",
             "created_at": _iso(now)},                              # non-contribution -> dropped
            {"type": "PullRequestEvent", "repo": "acme/web",
             "created_at": _iso(now - timedelta(days=3))},
            {"type": "PushEvent", "repo": "acme/old",
             "created_at": _iso(now - timedelta(days=400))},        # outside 30-day window
        ]
        with unittest.mock.patch.object(discovery, "run_gh_json", return_value=events):
            rows, truncated = discovery.list_contributed_repos("octocat", within_days=30)
        by_name = {r["full_name"]: r for r in rows}
        self.assertIn("acme/api", by_name)
        self.assertIn("acme/web", by_name)
        self.assertNotIn("acme/watched", by_name)     # non-contribution type filtered
        self.assertNotIn("acme/old", by_name)         # aged out of the window
        self.assertEqual(by_name["acme/api"]["contribution_count"], 2)
        # newest timestamp kept for the deduped repo
        self.assertEqual(by_name["acme/api"]["last_contributed_at"],
                         _iso(now - timedelta(days=1)))
        # newest-contribution-first ordering
        self.assertEqual(rows[0]["full_name"], "acme/api")
        self.assertFalse(truncated)                    # only 5 events < page size

    def test_truncated_true_when_page_full(self):
        now = datetime.now(timezone.utc)
        full_page = [
            {"type": "PushEvent", "repo": f"acme/r{i}", "created_at": _iso(now)}
            for i in range(discovery._EVENT_PAGE_SIZE)
        ]
        with unittest.mock.patch.object(discovery, "run_gh_json", return_value=full_page):
            _rows, truncated = discovery.list_contributed_repos("octocat", within_days=30)
        self.assertTrue(truncated)


class TestPinnedRepos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        store.ensure_layout()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_repos_missing_file(self):
        # ensure_layout does not create repos.json, so it is genuinely absent
        self.assertFalse(discovery.repos_path().exists())
        self.assertEqual(discovery.read_repos(), [])

    def test_read_repos_corrupt_file(self):
        discovery.repos_path().write_text("}{ not json", encoding="utf-8")
        self.assertEqual(discovery.read_repos(), [])

    def test_add_repo_is_idempotent_case_insensitive_newest_first(self):
        discovery.add_repo("acme", "widget")
        repos = discovery.add_repo("beta", "tool")
        self.assertEqual(repos[0]["full_name"], "beta/tool")   # newest first
        # re-add the first with different casing — must not duplicate, moves to front
        repos = discovery.add_repo("ACME", "WIDGET")
        self.assertEqual(repos[0]["full_name"], "ACME/WIDGET")
        matches = [r for r in repos if r["full_name"].lower() == "acme/widget"]
        self.assertEqual(len(matches), 1)

    def test_remove_repo_is_case_insensitive(self):
        discovery.add_repo("acme", "widget")
        repos = discovery.remove_repo("ACME", "WIDGET")
        self.assertEqual(repos, [])


class TestPinnedRepoReadIsGuarded(unittest.TestCase):
    """`read_repos` feeds the sidebar straight from a worker-writable file."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        store.ensure_layout(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, payload):
        path = discovery.repos_path(self.root)
        path.write_text(payload, encoding="utf-8")
        return path

    def test_a_credential_in_a_field_is_redacted(self):
        cred = "ghp_0123456789abcdefghijklmnopqrstuvwxyzA"
        self._write(json.dumps({"repos": [
            {"owner": "acme", "repo": "widgets", "note": "token " + cred},
        ]}))
        rows = discovery.read_repos(self.root)
        self.assertEqual(len(rows), 1)
        self.assertNotIn(cred, json.dumps(rows))
        # The fields the app keys on survive intact -- a real owner/repo never
        # matches a credential shape, so redaction is a no-op for them.
        self.assertEqual(rows[0]["owner"], "acme")
        self.assertEqual(rows[0]["repo"], "widgets")

    def test_a_row_with_a_non_string_identity_is_dropped(self):
        """`owner`/`repo` are checked for TYPE, not truthiness.

        A worker owns this file, and a planted dict or list is truthy, survives
        redaction (which walks strings), and would reach the client as an object
        React cannot render -- taking the whole page down instead of showing one
        bad row."""
        self._write(json.dumps({"repos": [
            {"owner": {"nested": "obj"}, "repo": "widgets"},
            {"owner": "acme", "repo": ["list"]},
            {"owner": 7, "repo": "widgets"},
            {"owner": "", "repo": "widgets"},
            {"owner": "good", "repo": "row"},
        ]}))
        rows = discovery.read_repos(self.root)
        self.assertEqual([(r["owner"], r["repo"]) for r in rows], [("good", "row")])

    def test_a_credential_in_a_key_is_redacted(self):
        """A worker writing this file controls key names as well as values."""
        cred = "ghp_0123456789abcdefghijklmnopqrstuvwxyzA"
        self._write(json.dumps({"repos": [
            {"owner": "acme", "repo": "widgets", cred: "planted in the key"},
        ]}))
        rows = discovery.read_repos(self.root)
        self.assertNotIn(cred, json.dumps(rows))

    def test_a_credential_nested_in_a_value_is_redacted(self):
        """A dict or list value must not smuggle a string past a str check."""
        cred = "ghp_0123456789abcdefghijklmnopqrstuvwxyzA"
        self._write(json.dumps({"repos": [
            {"owner": "acme", "repo": "widgets",
             "meta": {"inner": ["see " + cred]}},
        ]}))
        rows = discovery.read_repos(self.root)
        self.assertNotIn(cred, json.dumps(rows))

    def test_a_symlink_planted_at_the_path_is_refused(self):
        """The reader must not dereference a link a worker planted.

        Without the no-link guard a plain read hands back attacker-chosen JSON from
        anywhere the gateway can read, under the shape the sidebar trusts.
        """
        outside = self.root.parent / "elsewhere.json"
        outside.write_text(json.dumps({"repos": [
            {"owner": "evil", "repo": "payload"}]}), encoding="utf-8")
        path = discovery.repos_path(self.root)
        if path.exists() or path.is_symlink():
            path.unlink()
        try:
            path.symlink_to(outside)
        except OSError:                 # pragma: no cover - platform without symlinks
            self.skipTest("symlinks unavailable")
        rows = discovery.read_repos(self.root)
        self.assertEqual(rows, [], "followed the plant: " + repr(rows))
        outside.unlink()


if __name__ == "__main__":
    unittest.main()
