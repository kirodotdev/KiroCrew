"""Tests for Issue Radar's ``/ref`` route — the compact summary of one referenced
issue or pull request.

It exists because the in-app cross-reference UI needs two cheap things that
``/issue`` cannot give cheaply: a hover preview (paid on POINTER MOVEMENT, so it
must never page a timeline) and the issue-vs-PR answer for a bare ``#123``, which
GitHub's redirecting ``/issues/{n}`` URL cannot supply.

Coverage is at three levels:
  * ``get_ref_summary`` — the request shape (one call, no timeline);
  * the store cache — that its TTL makes a stale entry read as a MISS, since a
    hover card showing "open" on a merged PR is worse than no card;
  * the HANDLER — validation, the connected-repo gate, cache-first, and
    ``refresh=1``. Without the handler tests, deleting the cache branch leaves
    every other test green while every hover spends a ``gh`` call.

Every test patches the ``gh`` layer, so no subprocess is spawned (and the
POSIX-only ``_gh_bin`` guard is never reached — these run on Windows too).
"""
import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import routes, store

SUMMARY = {
    "number": 533,
    "title": "Tagging dashboard",
    "state": "open",
    "state_reason": None,
    "url": "https://github.com/o/r/issues/533",
    "author": "alice",
    "author_association": "MEMBER",
    "created_at": "2026-07-01T00:00:00Z",
    "updated_at": "2026-07-02T00:00:00Z",
    "closed_at": None,
    "comments": 2,
    "is_pr": False,
    "draft": False,
    "merged_at": None,
    "labels": [],
}


def _get(query: str):
    return make_mocked_request("GET", f"/api/apps/issue-radar/ref?{query}")


async def _call(query: str):
    return await routes._handle_ref_summary(_get(query))


def _body(response):
    return json.loads(response.body.decode("utf-8"))


class GetRefSummaryTest(unittest.TestCase):
    """The gh request itself: one issues-endpoint call, jq-projected."""

    def test_uses_the_issues_endpoint_with_a_coerced_number(self):
        proc = mock.Mock(returncode=0, stdout=json.dumps(SUMMARY), stderr="")
        with mock.patch.object(gh, "_gh_run", return_value=proc) as run:
            out = gh.get_ref_summary("o", "r", 533)
        self.assertEqual(out, SUMMARY)
        argv = run.call_args[0][0]
        # The issues endpoint answers for a PR too, which is what makes one call
        # enough to resolve the kind.
        self.assertIn("repos/o/r/issues/533", argv)
        self.assertIn("--jq", argv)
        # A hover must not pay for a timeline.
        self.assertFalse(any("timeline" in str(a) for a in argv))

    def test_number_cannot_inject_path_segments(self):
        proc = mock.Mock(returncode=0, stdout=json.dumps(SUMMARY), stderr="")
        with mock.patch.object(gh, "_gh_run", return_value=proc) as run:
            # int() coercion is what stops it, and it raises while the argv is
            # being built — so no `gh` process is ever spawned with the payload.
            with self.assertRaises(ValueError):
                gh.get_ref_summary("o", "r", "12/../../secret")  # type: ignore[arg-type]
        run.assert_not_called()

    def test_a_gh_failure_raises_gh_cli_error(self):
        proc = mock.Mock(returncode=1, stdout="", stderr="HTTP 404: Not Found")
        with mock.patch.object(gh, "_gh_run", return_value=proc):
            with self.assertRaises(gh.GhCliError):
                gh.get_ref_summary("o", "r", 999)

    def test_unparseable_output_raises_gh_cli_error(self):
        proc = mock.Mock(returncode=0, stdout="not json", stderr="")
        with mock.patch.object(gh, "_gh_run", return_value=proc):
            with self.assertRaises(gh.GhCliError):
                gh.get_ref_summary("o", "r", 1)

    def test_number_must_be_an_integer(self):
        with self.assertRaises(ValueError):
            gh.get_ref_summary("o", "r", "abc")  # type: ignore[arg-type]


class RefSummaryCacheTest(unittest.TestCase):
    """The cache owns freshness, so a stale entry must read as a miss."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_round_trips(self):
        store.write_ref_summary_cache("o", "r", 533, SUMMARY, root=self.root)
        self.assertEqual(store.read_ref_summary_cache("o", "r", 533, self.root), SUMMARY)

    def test_absent_entry_is_a_miss(self):
        self.assertIsNone(store.read_ref_summary_cache("o", "r", 1, self.root))

    def test_entry_older_than_max_age_is_a_miss(self):
        store.write_ref_summary_cache("o", "r", 533, SUMMARY, root=self.root)
        path = store.ref_summary_cache_path("o", "r", 533, self.root)
        old = time.time() - 3600
        os.utime(path, (old, old))
        self.assertIsNone(
            store.read_ref_summary_cache("o", "r", 533, self.root, max_age_sec=300.0)
        )
        # Without a ceiling the same file still reads.
        self.assertEqual(store.read_ref_summary_cache("o", "r", 533, self.root), SUMMARY)

    def test_corrupt_entry_is_a_miss(self):
        path = store.ref_summary_cache_path("o", "r", 533, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(store.read_ref_summary_cache("o", "r", 533, self.root))


class RefSummaryHandlerTest(unittest.TestCase):
    """The route: validation, the connected gate, cache-first, refresh=1."""

    def test_missing_params_are_rejected(self):
        for query in ("", "owner=o", "owner=o&repo=r"):
            res = asyncio.run(_call(query))
            self.assertEqual(res.status, 400, query)

    def test_non_numeric_and_non_positive_numbers_are_rejected(self):
        for query in ("owner=o&repo=r&number=abc", "owner=o&repo=r&number=0",
                      "owner=o&repo=r&number=-3"):
            res = asyncio.run(_call(query))
            self.assertEqual(res.status, 400, query)

    def test_an_oversized_number_is_rejected_not_crashed(self):
        # An unbounded int reaches the filesystem: the cache is named
        # ``ref-{n}.json``, so a several-hundred-digit number makes the cache
        # probe raise ENAMETOOLONG and the route 500s on what is really bad input.
        # Patched to be reachable regardless of the connected-repo gate, and to
        # prove the number is rejected BEFORE any cache or gh work.
        with mock.patch.object(store, "is_repo_connected", return_value=True), \
                mock.patch.object(store, "read_ref_summary_cache") as read, \
                mock.patch.object(gh, "get_ref_summary") as fetch:
            res = asyncio.run(_call(f"owner=o&repo=r&number={'9' * 247}"))
        self.assertEqual(res.status, 400)
        self.assertIn("at most", _body(res)["error"])
        read.assert_not_called()
        fetch.assert_not_called()

    def test_the_largest_supported_number_is_accepted(self):
        with mock.patch.object(store, "is_repo_connected", return_value=True), \
                mock.patch.object(store, "read_ref_summary_cache", return_value=SUMMARY):
            res = asyncio.run(_call(f"owner=o&repo=r&number={routes.MAX_ITEM_NUMBER}"))
        self.assertEqual(res.status, 200)

    def test_an_unconnected_repo_is_refused_before_any_gh_call(self):
        with mock.patch.object(store, "is_repo_connected", return_value=False), \
                mock.patch.object(gh, "get_ref_summary") as fetch:
            res = asyncio.run(_call("owner=o&repo=r&number=5"))
        self.assertEqual(res.status, 404)
        fetch.assert_not_called()

    def test_serves_a_fresh_cache_without_calling_gh(self):
        with mock.patch.object(store, "is_repo_connected", return_value=True), \
                mock.patch.object(store, "read_ref_summary_cache", return_value=SUMMARY), \
                mock.patch.object(gh, "get_ref_summary") as fetch:
            res = asyncio.run(_call("owner=o&repo=r&number=533"))
        self.assertEqual(res.status, 200)
        body = _body(res)
        self.assertTrue(body["from_cache"])
        self.assertEqual(body["summary"], SUMMARY)
        fetch.assert_not_called()

    def test_fetches_and_writes_through_on_a_cache_miss(self):
        with mock.patch.object(store, "is_repo_connected", return_value=True), \
                mock.patch.object(store, "read_ref_summary_cache", return_value=None), \
                mock.patch.object(store, "write_ref_summary_cache") as write, \
                mock.patch.object(gh, "get_ref_summary", return_value=SUMMARY) as fetch:
            res = asyncio.run(_call("owner=o&repo=r&number=533"))
        self.assertEqual(res.status, 200)
        self.assertFalse(_body(res)["from_cache"])
        fetch.assert_called_once_with("o", "r", 533)
        write.assert_called_once()

    def test_refresh_bypasses_the_cache(self):
        with mock.patch.object(store, "is_repo_connected", return_value=True), \
                mock.patch.object(store, "read_ref_summary_cache") as read, \
                mock.patch.object(store, "write_ref_summary_cache"), \
                mock.patch.object(gh, "get_ref_summary", return_value=SUMMARY):
            res = asyncio.run(_call("owner=o&repo=r&number=533&refresh=1"))
        self.assertEqual(res.status, 200)
        read.assert_not_called()

    def test_a_plain_read_passes_the_ttl_to_the_cache(self):
        with mock.patch.object(store, "is_repo_connected", return_value=True), \
                mock.patch.object(store, "read_ref_summary_cache", return_value=SUMMARY) as read:
            asyncio.run(_call("owner=o&repo=r&number=533"))
        self.assertEqual(
            read.call_args.kwargs.get("max_age_sec"), store.REF_SUMMARY_CACHE_TTL_SEC
        )

    def test_a_gh_failure_becomes_a_502(self):
        with mock.patch.object(store, "is_repo_connected", return_value=True), \
                mock.patch.object(store, "read_ref_summary_cache", return_value=None), \
                mock.patch.object(gh, "get_ref_summary", side_effect=gh.GhCliError("nope")):
            res = asyncio.run(_call("owner=o&repo=r&number=533"))
        self.assertEqual(res.status, 502)
        self.assertIn("error", _body(res))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
