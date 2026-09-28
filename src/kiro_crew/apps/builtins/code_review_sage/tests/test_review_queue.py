#!/usr/bin/env python3
"""Tests for the "awaiting my review" queue: ``discovery.list_review_requested``
and the ``/review-queue`` route."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from sage_lib import discovery

from .test_backend_routes import _load_routes_module
from .test_user_repos import _FAKE_GH, _cp, _FakeRequest, _Home


def _hit(n: int, repo: str = "acme/widgets", **over) -> dict:
    row = {
        "number": n,
        "title": f"PR {n}",
        "url": f"https://github.com/{repo}/pull/{n}",
        "repository": {"name": repo.split("/")[1], "nameWithOwner": repo},
        "author": {"login": "octo"},
        "updatedAt": "2026-09-20T00:00:00Z",
        "isDraft": False,
        "labels": [{"name": "bug"}],
    }
    row.update(over)
    return row


class TestListReviewRequested(_Home):
    def _run(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        captured: dict = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw
            return _cp(stdout=stdout, returncode=returncode, stderr=stderr)

        with (
            patch.object(discovery, "gh_bin", return_value=_FAKE_GH),
            patch.object(discovery.subprocess, "run", side_effect=fake_run),
        ):
            result = discovery.list_review_requested()
        return result, captured

    def test_argv_is_a_fixed_list(self):
        _, cap = self._run(stdout="[]")
        argv = cap["argv"]
        self.assertEqual(argv[:3], [_FAKE_GH, "search", "prs"])
        self.assertIn("--review-requested=@me", argv)
        self.assertIn("--state=open", argv)
        self.assertEqual(
            argv[argv.index("--json") + 1],
            "number,title,url,repository,author,updatedAt,isDraft,labels",
        )
        # One past the cap, so truncation is observed rather than guessed.
        self.assertEqual(argv[argv.index("--limit") + 1], "101")
        self.assertFalse(cap["kw"].get("shell"))

    def test_rows_take_the_repo_pr_list_shape(self):
        # No head SHA in search results, so nothing may read as reviewed.
        (rows, truncated), _ = self._run(stdout=json.dumps([_hit(7, isDraft=True)]))
        self.assertFalse(truncated)
        self.assertEqual(
            rows,
            [
                {
                    "url": "https://github.com/acme/widgets/pull/7",
                    "number": 7,
                    "title": "PR 7",
                    "repo": "acme/widgets",
                    "author": "octo",
                    "updated_at": "2026-09-20T00:00:00Z",
                    "draft": True,
                    "labels": ["bug"],
                    "head_sha": "",
                    "reviewed": False,
                    "reviewed_stale": False,
                }
            ],
        )

    def test_drops_rows_without_url(self):
        (rows, _), _ = self._run(stdout=json.dumps([_hit(1, url=""), _hit(2)]))
        self.assertEqual([r["number"] for r in rows], [2])

    def test_a_full_cap_is_not_truncated_but_one_more_is(self):
        (rows, truncated), _ = self._run(stdout=json.dumps([_hit(i) for i in range(100)]))
        self.assertEqual((len(rows), truncated), (100, False))
        (rows, truncated), _ = self._run(stdout=json.dumps([_hit(i) for i in range(101)]))
        self.assertEqual((len(rows), truncated), (100, True))

    def test_every_retained_field_is_bounded(self):
        big = "x" * 100_000
        hit = _hit(
            1,
            title=big,
            url="https://github.com/acme/widgets/pull/1" + big,
            author={"login": big},
            repository={"nameWithOwner": big},
            updatedAt=big,
            labels=[{"name": big}] * 1000,  # more than GitHub allows
        )
        (rows, _), _ = self._run(stdout=json.dumps([hit]))
        row = rows[0]
        for key in ("url", "title", "repo", "author", "updated_at"):
            self.assertLessEqual(len(row[key]), discovery._QUEUE_MAX_TEXT, key)
        self.assertLessEqual(len(row["labels"]), discovery._QUEUE_MAX_LABELS)
        self.assertTrue(all(len(lb) <= discovery._QUEUE_MAX_TEXT for lb in row["labels"]))

    def test_a_gh_that_cannot_start_is_gh_error_not_a_crash(self):
        with (
            patch.object(discovery, "gh_bin", return_value=_FAKE_GH),
            patch.object(
                discovery.subprocess, "run", side_effect=PermissionError("not executable")
            ),
        ):
            with self.assertRaises(discovery.GhError) as ctx:
                discovery.list_review_requested()
        self.assertNotIsInstance(ctx.exception, discovery.GhSetupError)

    def test_unauthenticated_is_setup_error(self):
        with self.assertRaises(discovery.GhSetupError):
            self._run(returncode=1, stderr="To get started run: gh auth login")

    def test_other_failure_is_gh_error(self):
        with self.assertRaises(discovery.GhError) as ctx:
            self._run(returncode=1, stderr="HTTP 502")
        self.assertNotIsInstance(ctx.exception, discovery.GhSetupError)

    def test_unparseable_output_is_gh_error(self):
        with self.assertRaises(discovery.GhError):
            self._run(stdout="not json")


class TestReviewQueueEndpoint(_Home, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.mod = _load_routes_module()

    async def _call(self) -> dict:
        resp = await self.mod._handle_review_queue(_FakeRequest())
        return {"status": resp.status, "body": json.loads(resp.body.decode())}

    async def test_rows_carry_the_real_change_id_and_truncation(self):
        # An empty change id would let the detail pane post a whole multi-PR
        # run's findings, so each row gets the id the repo list uses.
        url = "https://github.com/acme/widgets/pull/7"
        with patch.object(
            self.mod.discovery,
            "list_review_requested",
            return_value=([{"url": url, "number": 7}], True),
        ):
            out = await self._call()
        self.assertEqual(out["status"], 200)
        self.assertTrue(out["body"]["truncated"])
        cid = out["body"]["prs"][0]["change_id"]
        self.assertTrue(cid)
        self.assertEqual(cid, self.mod.review_driver.change_id_for(url))

    async def test_setup_required_is_not_an_error_status(self):
        with patch.object(
            self.mod.discovery, "list_review_requested", side_effect=discovery.GhSetupError("no gh")
        ):
            out = await self._call()
        self.assertEqual(out["status"], 200)
        self.assertTrue(out["body"]["setup_required"])

    async def test_transient_failure_is_502_without_provider_text(self):
        with patch.object(
            self.mod.discovery,
            "list_review_requested",
            side_effect=discovery.GhError("token ghp_secret"),
        ):
            out = await self._call()
        self.assertEqual(out["status"], 502)
        self.assertEqual(out["body"]["code"], "provider_unavailable")
        self.assertNotIn("ghp_secret", json.dumps(out["body"]))

    def test_route_is_registered(self):
        from aiohttp import web

        app = web.Application()
        self.mod.register_routes(app)
        paths = {r.resource.canonical for r in app.router.routes() if r.resource is not None}
        self.assertIn("/api/apps/code-review-sage/review-queue", paths)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
