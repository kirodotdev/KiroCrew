#!/usr/bin/env python3
"""GitHub Enterprise as a first-class host: pinned repos, discovery and the
review queue across every configured ``github_hosts`` entry.

Before this, a GHE pull request could only be reviewed by pasting its link; the
pinned-repo store had no host, so a GHE repo could not be picked, and the
picker, the Requests tab and the Reviews tab only ever looked at github.com.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sage_lib import discovery, store

from .test_backend_routes import _load_routes_module

_FAKE_GH = os.path.abspath(os.path.join(os.sep, "usr", "bin", "gh"))
_GHE = "acme.ghe.com"


def _cp(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode,
        stdout=stdout.encode("utf-8"), stderr=stderr.encode("utf-8"))


class _Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        store.ensure_layout()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hosts(self, *hosts: str) -> None:
        cfg_path = store.data_dir() / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["github_hosts"] = list(hosts)
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")


class TestHostHelpers(_Home):
    def test_configured_hosts_default_is_github_only(self):
        self.assertEqual(discovery.configured_hosts(), ["github.com"])

    def test_configured_hosts_puts_github_first_and_folds_www(self):
        self._hosts("zeta.ghe.com", "www.github.com", _GHE)
        self.assertEqual(discovery.configured_hosts(), ["github.com", _GHE, "zeta.ghe.com"])

    def test_ghe_only_config_has_no_github(self):
        self._hosts(_GHE)
        self.assertEqual(discovery.configured_hosts(), [_GHE])

    def test_repo_slug(self):
        self.assertEqual(discovery.repo_slug(None, "o", "r"), "o/r")
        self.assertEqual(discovery.repo_slug("www.github.com", "o", "r"), "o/r")
        self.assertEqual(discovery.repo_slug("ACME.ghe.com", "o", "r"), f"{_GHE}/o/r")

    def test_host_arg_leaves_github_unpinned(self):
        self.assertIsNone(discovery.host_arg(None))
        self.assertIsNone(discovery.host_arg("github.com"))
        self.assertEqual(discovery.host_arg(_GHE), _GHE)


class TestPinnedStore(_Home):
    def test_legacy_rows_read_as_github(self):
        store.atomic_write_locked(discovery.repos_path(), json.dumps(
            {"repos": [{"owner": "o", "repo": "r"}]}).encode())
        rows = discovery.read_repos()
        self.assertEqual(rows[0]["host"], "github.com")
        self.assertEqual(rows[0]["slug"], "o/r")

    def test_rows_on_unconfigured_hosts_are_hidden_but_kept(self):
        self._hosts("github.com", _GHE)
        discovery.add_repo("o", "r", host=_GHE)
        self._hosts("github.com")
        self.assertEqual(discovery.read_repos(), [])
        # An unrelated pin must not silently delete the hidden row.
        discovery.add_repo("x", "y")
        self._hosts("github.com", _GHE)
        self.assertEqual(sorted(r["slug"] for r in discovery.read_repos()),
                         [f"{_GHE}/o/r", "x/y"])

    def test_non_string_host_is_dropped(self):
        store.atomic_write_locked(discovery.repos_path(), json.dumps(
            {"repos": [{"owner": "o", "repo": "r", "host": {"x": 1}}]}).encode())
        self.assertEqual(discovery.read_repos(), [])

    def test_remove_is_host_scoped(self):
        self._hosts("github.com", _GHE)
        discovery.add_repo("o", "r")
        discovery.add_repo("o", "r", host=_GHE)
        left = discovery.remove_repo("o", "r", host=_GHE)
        self.assertEqual([r["slug"] for r in left], ["o/r"])


class TestHostPinnedGhCalls(_Home):
    def _capture(self, fn, stdout: str):
        captured: dict = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return _cp(stdout=stdout)

        with patch.object(discovery, "gh_bin", return_value=_FAKE_GH), \
                patch.object(discovery.subprocess, "run", side_effect=fake_run):
            out = fn()
        return captured["argv"], out

    def test_user_repos_on_ghe_pins_hostname_and_tags_rows(self):
        row = {"owner": "o", "repo": "r", "full_name": "o/r", "pushed_at": "",
               "private": True, "archived": False, "can_push": True}
        argv, (rows, _) = self._capture(
            lambda: discovery.list_user_repos(host=_GHE), json.dumps(row))
        self.assertEqual(argv[argv.index("--hostname") + 1], _GHE)
        self.assertEqual(rows[0]["host"], _GHE)
        self.assertEqual(rows[0]["slug"], f"{_GHE}/o/r")

    def test_user_repos_on_github_stays_unpinned(self):
        row = {"owner": "o", "repo": "r", "full_name": "o/r"}
        argv, _ = self._capture(lambda: discovery.list_user_repos(), json.dumps(row))
        self.assertNotIn("--hostname", argv)

    def test_current_login_on_ghe_pins_hostname(self):
        argv, login = self._capture(lambda: discovery.current_login(host=_GHE), "me\n")
        self.assertEqual(login, "me")
        self.assertEqual(argv[argv.index("--hostname") + 1], _GHE)

    def test_review_queue_on_ghe_uses_rest_search(self):
        hit = {"url": f"https://{_GHE}/o/r/pull/7", "number": 7, "title": "t",
               "repository_url": f"https://{_GHE}/api/v3/repos/o/r",
               "author": "a", "updated_at": "2026-11-01T00:00:00Z", "draft": False,
               "labels": ["x"]}
        argv, (rows, truncated) = self._capture(
            lambda: discovery.list_review_requested(host=_GHE), json.dumps(hit))
        self.assertEqual(argv[1:3], ["api", argv[2]])
        self.assertTrue(argv[2].startswith("search/issues?q="))
        self.assertIn("review-requested:@me", argv[2])
        self.assertEqual(argv[argv.index("--hostname") + 1], _GHE)
        self.assertFalse(truncated)
        self.assertEqual(rows[0]["repo"], f"{_GHE}/o/r")
        self.assertEqual(rows[0]["url"], hit["url"])
        self.assertEqual(rows[0]["labels"], ["x"])
        self.assertFalse(rows[0]["reviewed"])

    def test_review_queue_on_ghe_empty_is_empty(self):
        _argv, (rows, truncated) = self._capture(
            lambda: discovery.list_review_requested(host=_GHE), "")
        self.assertEqual((rows, truncated), ([], False))


class _FakeRequest:
    method = "GET"
    query: dict = {}
    match_info: dict = {}


class TestEndpointsAcrossHosts(_Home, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.mod = _load_routes_module()
        self._hosts("github.com", _GHE)

    @staticmethod
    def _body(resp) -> dict:
        return json.loads(resp.body.decode())

    async def test_my_repos_merges_hosts_and_marks_pins_per_host(self):
        discovery.add_repo("o", "r", host=_GHE)

        def fake(*, host=None, **_kw):
            canon = host or "github.com"
            return ([{"owner": "o", "repo": "r", "full_name": "o/r", "host": canon,
                      "slug": discovery.repo_slug(canon, "o", "r")}], False)

        with patch.object(self.mod.discovery, "list_user_repos", side_effect=fake):
            resp = await self.mod._handle_my_repos(_FakeRequest())
        body = self._body(resp)
        self.assertEqual(resp.status, 200)
        by = {r["slug"]: r["pinned"] for r in body["repos"]}
        self.assertEqual(by, {"o/r": False, f"{_GHE}/o/r": True})

    async def test_one_unauthenticated_host_does_not_blank_the_others(self):
        def fake(*, host=None, **_kw):
            if host == _GHE:
                raise discovery.GhSetupError("not logged in")
            return ([{"owner": "o", "repo": "r", "full_name": "o/r"}], False)

        with patch.object(self.mod.discovery, "list_user_repos", side_effect=fake):
            resp = await self.mod._handle_my_repos(_FakeRequest())
        body = self._body(resp)
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(body["repos"]), 1)
        self.assertIn(_GHE, body["host_errors"])
        self.assertNotIn("setup_required", body)

    async def test_every_host_unauthenticated_is_setup_required(self):
        with patch.object(self.mod.discovery, "list_user_repos",
                          side_effect=discovery.GhSetupError("no")):
            resp = await self.mod._handle_my_repos(_FakeRequest())
        self.assertEqual(resp.status, 200)
        self.assertTrue(self._body(resp)["setup_required"])

    async def test_every_host_down_is_502(self):
        with patch.object(self.mod.discovery, "list_user_repos",
                          side_effect=discovery.GhError("boom")):
            resp = await self.mod._handle_my_repos(_FakeRequest())
        self.assertEqual(resp.status, 502)

    async def test_recent_repos_asks_each_host_for_its_own_login(self):
        seen: list = []

        def login(*, host=None, **_kw):
            seen.append(host)
            return "me-" + (host or "gh")

        def contributed(lg, *, within_days, host=None, **_kw):
            return ([{"owner": "o", "repo": lg, "full_name": f"o/{lg}",
                      "host": host or "github.com"}], False)

        with patch.object(self.mod.discovery, "current_login", side_effect=login), \
                patch.object(self.mod.discovery, "list_contributed_repos",
                             side_effect=contributed):
            resp = await self.mod._handle_recent_repos(_FakeRequest())
        body = self._body(resp)
        self.assertEqual(seen, ["github.com", _GHE])
        self.assertEqual(body["login"], "me-github.com")
        self.assertEqual(body["logins"][_GHE], f"me-{_GHE}")
        self.assertEqual(len(body["repos"]), 2)

    async def test_review_queue_merges_hosts_newest_first(self):
        def fake(*, host=None, **_kw):
            when = "2026-11-02T00:00:00Z" if host == _GHE else "2026-10-01T00:00:00Z"
            h = host or "github.com"
            return ([{"url": f"https://{h}/o/r/pull/1", "updated_at": when,
                      "number": 1}], False)

        with patch.object(self.mod.discovery, "list_review_requested", side_effect=fake):
            resp = await self.mod._handle_review_queue(_FakeRequest())
        body = self._body(resp)
        self.assertEqual(resp.status, 200)
        self.assertEqual([p["url"] for p in body["prs"]],
                         [f"https://{_GHE}/o/r/pull/1", "https://github.com/o/r/pull/1"])
        self.assertTrue(all(p["change_id"] for p in body["prs"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
