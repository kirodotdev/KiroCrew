#!/usr/bin/env python3
"""Tests for user-repo discovery: ``discovery.list_user_repos`` + ``/my-repos``.

This is the "list the repos I can actually reach" path, which complements the
event-feed listing: a repo you own but have not pushed to inside the activity
window is invisible to the feed and must still be pickable.
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


def _cp(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _repo(name: str, owner: str = "acme", **over) -> dict:
    row = {
        "owner": owner, "repo": name, "full_name": f"{owner}/{name}",
        "pushed_at": "2026-07-20T00:00:00Z", "private": False,
        "archived": False, "can_push": True,
    }
    row.update(over)
    return row


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


class TestListUserRepos(_Home):
    def _run(self, rows: list[dict], limit: int = 100):
        jsonl = "\n".join(json.dumps(r) for r in rows)
        with patch.object(discovery, "gh_bin", return_value="/usr/bin/gh"), \
                patch.object(discovery.subprocess, "run", return_value=_cp(stdout=jsonl)):
            return discovery.list_user_repos(limit=limit)

    def test_normalizes_rows(self):
        rows, truncated = self._run([_repo("widgets")])
        self.assertFalse(truncated)
        self.assertEqual(rows, [{
            "owner": "acme", "repo": "widgets", "full_name": "acme/widgets",
            "pushed_at": "2026-07-20T00:00:00Z", "private": False,
            "archived": False, "can_push": True,
        }])

    def test_flags_private_archived_and_readonly(self):
        rows, _ = self._run([_repo("secret", private=True, archived=True, can_push=False)])
        self.assertTrue(rows[0]["private"])
        self.assertTrue(rows[0]["archived"])
        self.assertFalse(rows[0]["can_push"])

    def test_drops_rows_missing_identity(self):
        # A row without owner/repo cannot be turned into a reviewable target, so
        # it must be dropped rather than surfaced as a blank picker entry.
        rows, _ = self._run([{"full_name": "no/identity"}, _repo("ok")])
        self.assertEqual([r["repo"] for r in rows], ["ok"])

    def test_truncated_when_page_is_full(self):
        # A full page means repos beyond the cap were NOT listed; the UI must be
        # able to say so instead of implying the list is exhaustive.
        rows, truncated = self._run([_repo(f"r{i}") for i in range(3)], limit=3)
        self.assertEqual(len(rows), 3)
        self.assertTrue(truncated)

    def test_requests_pushed_sort_and_all_affiliations(self):
        captured: dict = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return _cp(stdout=json.dumps(_repo("widgets")))

        with patch.object(discovery, "gh_bin", return_value="/usr/bin/gh"), \
                patch.object(discovery.subprocess, "run", side_effect=fake_run):
            discovery.list_user_repos()
        path = captured["argv"][2]
        self.assertIn("sort=pushed", path)
        self.assertIn("direction=desc", path)
        # Collaborator + org repos are exactly the ones the event feed misses.
        self.assertIn("affiliation=owner,collaborator,organization_member", path)
        self.assertNotIn("shell", captured.get("kw", {}))

    def test_limit_is_clamped(self):
        captured: dict = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return _cp(stdout="")

        with patch.object(discovery, "gh_bin", return_value="/usr/bin/gh"), \
                patch.object(discovery.subprocess, "run", side_effect=fake_run):
            discovery.list_user_repos(limit=99999)
        self.assertIn("per_page=100", captured["argv"][2])


class TestMyReposEndpoint(_Home, unittest.IsolatedAsyncioTestCase):
    """Async test case rather than ``asyncio.run`` inside a helper: the repo's
    spawn audit treats ``asyncio.run`` as a process-spawn call site, and a test
    that merely awaits a handler should not have to be allowlisted as a spawn."""

    def setUp(self):
        super().setUp()
        self.mod = _load_routes_module()

    async def _call(self) -> dict:
        """Invoke the handler directly (it reads no query params or body)."""
        resp = await self.mod._handle_my_repos(_FakeRequest())
        return {"status": resp.status, "body": json.loads(resp.body.decode())}

    async def test_annotates_pinned(self):
        discovery.add_repo("acme", "widgets")
        with patch.object(self.mod.discovery, "list_user_repos",
                          return_value=([_repo("widgets"), _repo("other")], False)):
            out = await self._call()
        self.assertEqual(out["status"], 200)
        by = {r["full_name"]: r for r in out["body"]["repos"]}
        self.assertTrue(by["acme/widgets"]["pinned"])
        self.assertFalse(by["acme/other"]["pinned"])

    async def test_setup_required_is_not_an_error_status(self):
        # No gh is a normal first-run state for this panel; the UI still offers
        # manual entry, so a 502 would be wrong.
        with patch.object(self.mod.discovery, "list_user_repos",
                          side_effect=discovery.GhSetupError("no gh")):
            out = await self._call()
        self.assertEqual(out["status"], 200)
        self.assertTrue(out["body"]["setup_required"])
        self.assertEqual(out["body"]["repos"], [])

    async def test_transient_gh_failure_is_502(self):
        with patch.object(self.mod.discovery, "list_user_repos",
                          side_effect=discovery.GhError("rate limited")):
            out = await self._call()
        self.assertEqual(out["status"], 502)
        self.assertIn("rate limited", out["body"]["error"])

    async def test_passes_truncation_through(self):
        with patch.object(self.mod.discovery, "list_user_repos",
                          return_value=([_repo("a")], True)):
            out = await self._call()
        self.assertTrue(out["body"]["truncated"])


class _FakeRequest:
    """Minimal stand-in: the handler reads no query params or body."""

    method = "GET"
    query: dict = {}
    match_info: dict = {}


class TestMyReposRouteRegistered(_Home):
    def test_route_is_registered(self):
        from aiohttp import web

        mod = _load_routes_module()
        app = web.Application()
        mod.register_routes(app)
        paths = {
            r.resource.canonical
            for r in app.router.routes()
            if r.resource is not None
        }
        self.assertIn("/api/apps/code-review-sage/my-repos", paths)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
