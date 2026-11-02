#!/usr/bin/env python3
"""Pasting a PULL REQUEST link into the add-repo field.

That field is the only place in the app you can type, so a URL from the clipboard
lands there whatever it points at. A PR link pins that PR's repo and reports the
PR back so the caller can open it, rather than being refused in favour of the
paste-PR box — which only exists once a repo is already picked, i.e. the thing the
user is trying to do.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from aiohttp import web
from backend import routes
from sage_lib import discovery, store

from kiro_crew.apps.builtins.code_review_sage.tests.fixtures import OwnerRequest


class _FakeRequest(OwnerRequest):
    """Minimal stand-in: the handler reads the method and the JSON body."""

    def __init__(self, body: dict, method: str = "POST"):
        super().__init__()
        self._body = body
        self.method = method

    async def json(self):
        return self._body


def _text(resp: web.Response) -> str:
    return resp.text or ""


class TestPullRequestRef(unittest.TestCase):
    def test_parses_a_pull_request_url(self):
        ref = routes._pull_request_ref(
            "https://github.com/kirodotdev/KiroCrew/pull/777")
        assert ref is not None
        self.assertEqual(ref["owner"], "kirodotdev")
        self.assertEqual(ref["repo"], "KiroCrew")  # brand-ok: repo name
        self.assertEqual(ref["number"], 777)
        # The change id has to match the one the review path writes, or the opened
        # PR would not line up with its own review.
        self.assertEqual(ref["change_id"], "GH-kirodotdev-KiroCrew-777")

    def test_a_repo_url_is_not_a_pull_request(self):
        self.assertIsNone(
            routes._pull_request_ref("https://github.com/kirodotdev/KiroCrew"))

    def test_a_bare_slug_is_not_a_pull_request(self):
        self.assertIsNone(routes._pull_request_ref("kirodotdev/KiroCrew"))

    def test_a_malformed_pull_request_url_falls_through(self):
        # Returning None lets the caller try repo-URL parsing and report ITS error,
        # rather than this helper raising on input it does not own.
        self.assertIsNone(routes._pull_request_ref("https://github.com/pull/777"))
        self.assertIsNone(routes._pull_request_ref("https://evil.test/o/r/pull/1"))

    def test_extra_path_segments_do_not_confuse_it(self):
        ref = routes._pull_request_ref(
            "https://github.com/kirodotdev/KiroCrew/pull/777/files#r123")
        assert ref is not None
        self.assertEqual(ref["number"], 777)
        # Normalised, not echoed: the url is rebuilt from validated parts.
        self.assertEqual(ref["url"],
                         "https://github.com/kirodotdev/KiroCrew/pull/777")


class TestRepoEndpointWithPullRequest(unittest.IsolatedAsyncioTestCase):
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

    async def test_pins_the_repo_and_reports_the_pull_request(self):
        resp = await routes._handle_repos(_FakeRequest(  # type: ignore[arg-type]
            {"repo": "https://github.com/kirodotdev/KiroCrew/pull/777"}))
        self.assertEqual(resp.status, 200)
        body = _text(resp)
        self.assertIn("kirodotdev", body)
        self.assertIn("pull_request", body)
        # And it really is pinned, not just echoed.
        pinned = [f"{r['owner']}/{r['repo']}" for r in discovery.read_repos()]
        self.assertIn("kirodotdev/KiroCrew", pinned)

    async def test_a_repo_url_reports_no_pull_request(self):
        resp = await routes._handle_repos(_FakeRequest(  # type: ignore[arg-type]
            {"repo": "https://github.com/kirodotdev/KiroCrew"}))
        self.assertEqual(resp.status, 200)
        self.assertNotIn("pull_request", _text(resp))

    async def test_reports_which_repo_was_added(self):
        # repos[0] is only the added repo if the store happens to prepend, so the
        # response names it.
        await routes._handle_repos(_FakeRequest(  # type: ignore[arg-type]
            {"repo": "https://github.com/first/one"}))
        resp = await routes._handle_repos(_FakeRequest(  # type: ignore[arg-type]
            {"repo": "https://github.com/second/two"}))
        self.assertIn('"added"', _text(resp))
        self.assertIn("second", _text(resp))

    async def test_a_hostile_host_is_still_refused(self):
        resp = await routes._handle_repos(_FakeRequest(  # type: ignore[arg-type]
            {"repo": "https://evil.test/kirodotdev/KiroCrew/pull/777"}))
        self.assertEqual(resp.status, 400)

    def _allow_ghe_host(self):
        # Write the opt-in host into THIS test's isolated config.json, exercising
        # the real config path the adapters read.
        cfg_path = store.data_dir() / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["github_hosts"] = ["github.com", "acme.ghe.com"]
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    async def test_a_ghe_pull_request_pins_its_repo_with_the_host(self):
        # The pinned row carries its host, so the picker later lists PRs on the
        # right instance instead of the gh default host.
        self._allow_ghe_host()
        resp = await routes._handle_repos(_FakeRequest(  # type: ignore[arg-type]
            {"repo": "https://acme.ghe.com/o/r/pull/9"}))
        self.assertEqual(resp.status, 200)
        body = json.loads(_text(resp))
        self.assertEqual(body["added"], {
            "owner": "o", "repo": "r", "host": "acme.ghe.com", "slug": "acme.ghe.com/o/r"})
        self.assertEqual(body["pull_request"]["url"], "https://acme.ghe.com/o/r/pull/9")
        pinned = routes.discovery.read_repos()
        self.assertEqual([(p["host"], p["slug"]) for p in pinned],
                         [("acme.ghe.com", "acme.ghe.com/o/r")])

    async def test_a_ghe_repo_url_pins_with_the_host(self):
        self._allow_ghe_host()
        resp = await routes._handle_repos(_FakeRequest(  # type: ignore[arg-type]
            {"repo": "https://acme.ghe.com/o/r"}))
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(_text(resp))["added"]["host"], "acme.ghe.com")

    async def test_same_slug_on_two_hosts_are_two_pins(self):
        self._allow_ghe_host()
        await routes._handle_repos(_FakeRequest(  # type: ignore[arg-type]
            {"repo": "https://github.com/o/r"}))
        await routes._handle_repos(_FakeRequest(  # type: ignore[arg-type]
            {"repo": "https://acme.ghe.com/o/r"}))
        slugs = sorted(p["slug"] for p in routes.discovery.read_repos())
        self.assertEqual(slugs, ["acme.ghe.com/o/r", "o/r"])

    async def test_owner_repo_pair_with_unconfigured_host_is_refused(self):
        resp = await routes._handle_repos(_FakeRequest(  # type: ignore[arg-type]
            {"owner": "o", "repo": "r", "host": "evil.example"}))
        self.assertEqual(resp.status, 400)
        self.assertIn("invalid_repo", _text(resp))

    async def test_owner_repo_pair_with_configured_host_pins(self):
        self._allow_ghe_host()
        resp = await routes._handle_repos(_FakeRequest(  # type: ignore[arg-type]
            {"owner": "o", "repo": "r", "host": "ACME.ghe.com"}))
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(_text(resp))["added"]["host"], "acme.ghe.com")

    async def test_pull_request_ref_keeps_the_ghe_host(self):
        self._allow_ghe_host()
        ref = routes._pull_request_ref("https://acme.ghe.com/o/r/pull/9")
        assert ref is not None
        self.assertEqual(ref["host"], "acme.ghe.com")
        self.assertEqual(ref["url"], "https://acme.ghe.com/o/r/pull/9")
        self.assertEqual(ref["change_id"], "GH-acme.ghe.com-o-r-9")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
