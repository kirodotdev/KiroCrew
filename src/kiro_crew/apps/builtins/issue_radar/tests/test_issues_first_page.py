"""The progressive first-paint fast path of ``GET /issues?first_page=1``.

Exercised END-TO-END through the aiohttp handler (not just the client), for the
same reason ``TestRefSummaryRoute`` gives: a route that passes ``root=`` twice, or
forgets ``partial``, raises or lies on every request and no client/store test can
see it, because nothing calls the handler.

The behaviour that carries real correctness:
  * a WARM cache is served whole with ``partial: false`` and does NO fetch — the
    fast path must not add a `gh` call when the full list is already cached;
  * a COLD cache fetches ONE page (the non-paginated client method) and returns it
    with ``partial: true``;
  * the fast path never WRITES the cache — the full fetch owns the durable cache,
    and persisting a partial would let a later poll serve an incomplete list.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import github_client, routes, store


class TestIssuesFirstPageRoute(unittest.IsolatedAsyncioTestCase):
    async def _call(self, query: str):
        request = make_mocked_request("GET", f"/api/apps/issue-radar/issues?{query}")
        return await routes._handle_issues(request)

    async def test_cold_cache_fetches_one_page_and_marks_it_partial(self):
        rows = [{"number": 1, "title": "newest", "labels": []}]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(routes, "_scope", return_value=Path(tmp)), \
                    mock.patch.object(routes, "_connected", return_value=True), \
                    mock.patch.object(
                        github_client, "list_open_issues_first_page", return_value=rows,
                    ) as first, \
                    mock.patch.object(github_client, "list_open_issues") as full:
                resp = await self._call("owner=acme&repo=widget&first_page=1")

        self.assertEqual(resp.status, 200)
        self.assertIn(b'"partial": true', resp.body)
        self.assertIn(b'"from_cache": false', resp.body)
        first.assert_called_once()
        # The fast path must NOT trigger the fully-paginated fetch.
        full.assert_not_called()

    async def test_cold_cache_does_not_write_the_cache(self):
        rows = [{"number": 1, "title": "newest", "labels": []}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(routes, "_scope", return_value=root), \
                    mock.patch.object(routes, "_connected", return_value=True), \
                    mock.patch.object(
                        github_client, "list_open_issues_first_page", return_value=rows,
                    ):
                await self._call("owner=acme&repo=widget&first_page=1")
            # The durable cache is owned by the full fetch; the partial must not
            # have written it, or a later poll could serve an incomplete list.
            self.assertIsNone(store.read_issues_cache("acme", "widget", root=root, state="open"))

    async def test_warm_cache_is_served_whole_without_fetching(self):
        cached = [{"number": 7, "title": "complete", "labels": []}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store.write_issues_cache("acme", "widget", cached, root=root, state="open")
            with mock.patch.object(routes, "_scope", return_value=root), \
                    mock.patch.object(routes, "_connected", return_value=True), \
                    mock.patch.object(
                        github_client, "list_open_issues_first_page",
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
                        github_client, "list_open_issues_first_page",
                    ) as first, \
                    mock.patch.object(
                        store, "refresh_issues_cache", return_value=[],
                    ):
                resp = await self._call("owner=acme&repo=widget&first_page=1&state=closed")

        self.assertEqual(resp.status, 200)
        # It fell through to the ordinary path, not the fast one.
        first.assert_not_called()

    async def test_unconnected_repo_is_refused(self):
        with mock.patch.object(routes, "_connected", return_value=False):
            resp = await self._call("owner=acme&repo=widget&first_page=1")
        self.assertEqual(resp.status, 404)


class TestFirstPageClient(unittest.TestCase):
    def test_first_page_is_a_single_unpaginated_request_of_the_full_shape(self):
        # Same JQ (full issue shape) and sort as list_open_issues, but paginate
        # OFF — so the first page appends behind the full set without reordering.
        with mock.patch.object(github_client, "_run_gh_api", return_value=[]) as run:
            github_client.list_open_issues_first_page("acme", "widget")
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertFalse(kwargs["paginate"])
        path = args[0]
        self.assertIn("state=open", path)
        self.assertIn("sort=updated", path)
        self.assertIn("per_page=100", path)


if __name__ == "__main__":
    unittest.main()
