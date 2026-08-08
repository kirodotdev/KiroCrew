#!/usr/bin/env python3
"""Pasting a PULL REQUEST link into the add-repo field.

That field is the only place in the app you can type, so a URL from the clipboard
lands there whatever it points at. It used to reject a PR link and tell the user to
use the paste-PR box — which only exists once a repo is already picked, i.e. the
thing they were trying to do. Now the PR's repo is pinned and the PR is reported
back so the caller can open it.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from aiohttp import web
from backend import routes
from sage_lib import discovery, store


class _FakeRequest:
    """Minimal stand-in: the handler reads the method and the JSON body."""

    def __init__(self, body: dict, method: str = "POST"):
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
        # The caller previously guessed repos[0], which is only right if the store
        # happens to prepend.
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
