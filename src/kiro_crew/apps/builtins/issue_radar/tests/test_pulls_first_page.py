"""The progressive first-paint fast path of ``GET /pulls?first_page=1``.

The PR twin of ``test_issues_first_page``, and the bigger latency win: a cold
``/pulls`` blocks on BOTH the full pagination AND the GraphQL enrichment before
it can return, so a busy repo sits on a skeleton for seconds. Exercised
END-TO-END through the aiohttp handler, for the same reason the issues fast-path
test gives: a route that forgets ``partial``, writes the cache, or accidentally
enriches raises or lies on every request and no client/store test can see it.

The behaviour that carries real correctness:
  * a WARM cache is served whole with ``partial: false`` and does NO fetch;
  * a COLD cache fetches ONE page (the non-paginated client method), returns it
    ``partial: true``, and — critically — does NOT enrich it (enrichment is the
    other slow leg, so paying it here would defeat the fast path);
  * the fast path never WRITES the cache — the full fetch owns the durable cache,
    and persisting an un-enriched partial would let a later poll serve it whole;
  * ``first_page`` is ignored for the closed filter (open-state optimization).
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import github_client, routes, store


class TestPullsFirstPageRoute(unittest.IsolatedAsyncioTestCase):
    async def _call(self, query: str):
        request = make_mocked_request("GET", f"/api/apps/issue-radar/pulls?{query}")
        return await routes._handle_pulls(request)

    async def test_cold_cache_fetches_one_page_and_marks_it_partial(self):
        rows = [{"number": 1, "title": "newest", "labels": []}]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(routes, "_scope", return_value=Path(tmp)), \
                    mock.patch.object(routes, "_connected", return_value=True), \
                    mock.patch.object(
                        github_client, "list_open_pulls_first_page", return_value=rows,
                    ) as first, \
                    mock.patch.object(github_client, "list_open_pulls") as full, \
                    mock.patch.object(github_client, "enrich_pulls") as enrich:
                resp = await self._call("owner=acme&repo=widget&first_page=1")

        self.assertEqual(resp.status, 200)
        self.assertIn(b'"partial": true', resp.body)
        self.assertIn(b'"from_cache": false', resp.body)
        first.assert_called_once()
        # The fast path must NOT trigger the fully-paginated fetch...
        full.assert_not_called()
        # ...and must NOT enrich — enrichment is the OTHER slow leg the fast path
        # exists to skip. The authoritative fetch enriches; the first page paints raw.
        enrich.assert_not_called()

    async def test_cold_cache_does_not_write_the_cache(self):
        rows = [{"number": 1, "title": "newest", "labels": []}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(routes, "_scope", return_value=root), \
                    mock.patch.object(routes, "_connected", return_value=True), \
                    mock.patch.object(
                        github_client, "list_open_pulls_first_page", return_value=rows,
                    ):
                await self._call("owner=acme&repo=widget&first_page=1")
            # The durable cache is owned by the full fetch; the un-enriched partial
            # must not have written it, or a later poll could serve an incomplete list.
            self.assertIsNone(store.read_pulls_cache("acme", "widget", root=root, state="open"))

    async def test_warm_cache_is_served_whole_without_fetching(self):
        cached = [{"number": 7, "title": "complete", "labels": [], "checks_counts": {}}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store.write_pulls_cache("acme", "widget", cached, root=root, state="open")
            with mock.patch.object(routes, "_scope", return_value=root), \
                    mock.patch.object(routes, "_connected", return_value=True), \
                    mock.patch.object(
                        github_client, "list_open_pulls_first_page",
                    ) as first:
                resp = await self._call("owner=acme&repo=widget&first_page=1")

        self.assertEqual(resp.status, 200)
        self.assertIn(b'"partial": false', resp.body)
        self.assertIn(b'"from_cache": true', resp.body)
        self.assertIn(b'"number": 7', resp.body)
        # A warm cache means no reason to spend a request on a first page.
        first.assert_not_called()

    async def test_first_page_is_ignored_for_the_closed_filter(self):
        # first_page is an open-state optimization; closed already fetches one page.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(routes, "_scope", return_value=Path(tmp)), \
                    mock.patch.object(routes, "_connected", return_value=True), \
                    mock.patch.object(
                        github_client, "list_open_pulls_first_page",
                    ) as first, \
                    mock.patch.object(github_client, "list_closed_pulls", return_value=[]), \
                    mock.patch.object(github_client, "enrich_pulls", return_value=[]), \
                    mock.patch.object(github_client, "enrichment_complete", return_value=True), \
                    mock.patch.object(store, "write_pulls_cache"):
                resp = await self._call("owner=acme&repo=widget&first_page=1&state=closed")

        self.assertEqual(resp.status, 200)
        # It fell through to the ordinary path, not the fast one.
        first.assert_not_called()

    async def test_unconnected_repo_is_refused(self):
        with mock.patch.object(routes, "_connected", return_value=False):
            resp = await self._call("owner=acme&repo=widget&first_page=1")
        self.assertEqual(resp.status, 404)

    async def test_provider_error_carries_a_machine_readable_code(self):
        # The error-code contract: every non-2xx JSON body must carry a `code`.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(routes, "_scope", return_value=Path(tmp)), \
                    mock.patch.object(routes, "_connected", return_value=True), \
                    mock.patch.object(
                        github_client, "list_open_pulls_first_page",
                        side_effect=github_client.GhCliError("boom"),
                    ):
                resp = await self._call("owner=acme&repo=widget&first_page=1")
        self.assertEqual(resp.status, 502)
        self.assertIn(b'"code": "provider_error"', resp.body)


class TestPullsFirstPageClient(unittest.TestCase):
    def test_first_page_is_a_single_unpaginated_request_of_the_full_shape(self):
        # Same JQ (full PR shape) and sort as list_open_pulls, but paginate OFF —
        # so the first page appends behind the full set without reordering.
        with mock.patch.object(github_client, "_run_gh_api", return_value=[]) as run:
            github_client.list_open_pulls_first_page("acme", "widget")
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertFalse(kwargs["paginate"])
        path = args[0]
        self.assertIn("state=open", path)
        self.assertIn("sort=updated", path)
        self.assertIn("per_page=100", path)


class TestEnrichPullsParallel(unittest.TestCase):
    """The two GraphQL families run concurrently, and each still fails independently."""

    def test_both_families_run_and_are_merged(self):
        pulls = [{"number": 1}, {"number": 2}]
        with mock.patch.object(
            github_client, "_enrich_summaries", return_value={1: {"additions": 5}},
        ) as summ, mock.patch.object(
            github_client, "_enrich_readiness", return_value={1: "clean"},
        ) as ready:
            out = github_client.enrich_pulls("acme", "widget", pulls, "open")
        summ.assert_called_once()
        ready.assert_called_once()
        # _apply_summaries merged both families onto the rows.
        self.assertEqual(out[0]["additions"], 5)

    def test_readiness_failure_does_not_sink_summaries(self):
        # Each family swallows its own GhCliError internally, so one raising must
        # not lose the other — the best-effort contract enrich_pulls documents.
        pulls = [{"number": 1}]
        with mock.patch.object(
            github_client, "fetch_pr_summaries", return_value={1: {"additions": 9}},
        ), mock.patch.object(
            github_client, "fetch_pr_readiness",
            side_effect=github_client.GhCliError("readiness down"),
        ), mock.patch.object(
            github_client, "fetch_pr_readiness_by_number", return_value={},
        ):
            out = github_client.enrich_pulls("acme", "widget", pulls, "open")
        # Summaries survived the readiness failure.
        self.assertEqual(out[0]["additions"], 9)


if __name__ == "__main__":
    unittest.main()
