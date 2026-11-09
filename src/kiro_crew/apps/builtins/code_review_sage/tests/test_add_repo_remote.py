#!/usr/bin/env python3
"""A git remote or a GitLab repo typed into the add-repo field.

``git remote -v`` prints an SSH remote as ``git@host:group/repo.git``. A
github.com remote pins its repo. A GitLab repo is refused, and the refusal says
GitLab instead of listing Sage's GitHub hosts, which can never make it
reviewable. The open-PR listing reads the same parser and says the same.
"""

from __future__ import annotations

import json
import unittest

from aiohttp import web
from backend import routes
from sage_lib import discovery, store

from kiro_crew.apps.builtins.code_review_sage.tests.fixtures import OwnerRequest
from kiro_crew.config.loader_locations import config_path

#: A self-managed GitLab instance the operator listed in ``dashboard.gitlab_hosts``.
_GITLAB_HOST = "code.example.dev"


class _FakeRequest(OwnerRequest):
    """Minimal stand-in: the handlers read the method, the query and the JSON body."""

    def __init__(self, body: dict | None = None, *, query: dict | None = None):
        super().__init__()
        self._body = body or {}
        self.query = query or {}
        self.method = "POST" if body is not None else "GET"

    async def json(self):
        return self._body


def _payload(resp: web.Response) -> dict:
    return json.loads(resp.text or "{}")


class TestAddRepoFieldTakesARemote(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The rootdir conftest pins KIROCREW_HOME to a fresh directory per test, so
        # both config files written here are this test's own.
        store.ensure_layout()
        config_path().write_text(
            json.dumps({"dashboard": {"gitlab_hosts": [_GITLAB_HOST]}}), encoding="utf-8"
        )

    async def test_a_github_remote_pins_its_repo(self):
        resp = await routes._handle_repos(
            _FakeRequest({"repo": "git@github.com:octo-org/hello-world.git"})
        )
        self.assertEqual(resp.status, 200, resp.text)
        self.assertEqual(_payload(resp)["added"], {"owner": "octo-org", "repo": "hello-world"})
        pinned = [f"{r['owner']}/{r['repo']}" for r in discovery.read_repos()]
        self.assertIn("octo-org/hello-world", pinned)

    async def test_a_gitlab_repo_is_refused_naming_gitlab(self):
        for repo in (
            f"https://{_GITLAB_HOST}/grp/sub/repo",
            f"git@{_GITLAB_HOST}:grp/repo.git",
            "git@gitlab.com:grp/repo.git",
        ):
            resp = await routes._handle_repos(_FakeRequest({"repo": repo}))
            self.assertEqual(resp.status, 400, repo)
            body = _payload(resp)
            self.assertEqual(body["code"], "invalid_repo_url", repo)
            self.assertIn("GitLab", body["error"], repo)
            self.assertNotIn("expected a repo URL on one of", body["error"], repo)
        self.assertEqual(discovery.read_repos(), [])

    async def test_the_open_pr_listing_refuses_a_gitlab_repo_naming_gitlab(self):
        resp = await routes._handle_repo_prs(
            _FakeRequest(query={"repo": f"https://{_GITLAB_HOST}/grp/sub/repo"})
        )
        self.assertEqual(resp.status, 400)
        body = _payload(resp)
        self.assertEqual(body["code"], "invalid_repo_url")
        self.assertIn("GitLab", body["error"])
        self.assertNotIn("expected a repo URL on one of", body["error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
