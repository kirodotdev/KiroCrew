"""Issue Radar's list fast paths and cache policy, and the investigation record routes.

This file pins the progressive first paint of ``/issues`` and ``/pulls``: a warm
cache is served whole, a cold one answers one page marked partial without writing
the cache, and only the open state takes that branch. It pins the ``/pulls`` list
cache, which persists only fully enriched rows, the poll probe on both lists, and
the local investigation record: the item-kind parser and the GET and PUT routes.
The model-backed routes live in ``test_issue_radar_routes_ai_coverage.py``
(``/issue-ai``, ``/pull-ai``) and
``test_issue_radar_routes_ai_coverage_recommendations_and_tagging.py``
(``/recommendations``, ``/tagging``), and the shared request builders and patch
helpers in ``issue_radar_routes_ai_coverage_helpers.py``.
"""

import unittest
from unittest import mock
from unittest.mock import AsyncMock

from aiohttp import web
from issue_radar_routes_ai_coverage_helpers import (
    _body,
    _connected,
    _get,
    _json_request,
    _key,
)

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import provider, routes, store

# ── progressive first paint (read-only fast paths) ───────────────────────────


class TestIssuesFirstPage(unittest.IsolatedAsyncioTestCase):
    """A warm cache is served WHOLE and complete; only a cold cache pays a fetch,
    and this branch never writes the cache — persisting a partial would let a
    later poll serve an incomplete list as if it were whole."""

    async def test_a_warm_cache_is_served_complete(self):
        snapshot = {"rows": [{"number": 7}]}
        with (
            mock.patch.object(store, "read_issues_snapshot", return_value=snapshot),
            mock.patch.object(gh, "list_open_issues_first_page") as fetch,
        ):
            response = await routes._handle_issues_first_page(_key(), gh, {})
        payload = _body(response)
        self.assertFalse(payload["partial"])
        self.assertTrue(payload["from_cache"])
        fetch.assert_not_called()

    async def test_a_cold_cache_returns_one_page_marked_partial_without_writing(self):
        with (
            mock.patch.object(store, "read_issues_snapshot", return_value=None),
            mock.patch.object(gh, "list_open_issues_first_page", return_value=[{"number": 7}]),
            mock.patch.object(store, "refresh_issues_cache") as write,
        ):
            response = await routes._handle_issues_first_page(_key(), gh, {})
        payload = _body(response)
        self.assertTrue(payload["partial"])
        self.assertFalse(payload["from_cache"])
        write.assert_not_called()

    async def test_a_provider_failure_carries_a_machine_readable_code(self):
        with (
            mock.patch.object(store, "read_issues_snapshot", return_value=None),
            mock.patch.object(
                gh, "list_open_issues_first_page", side_effect=gh.GhCliError("gh down")
            ),
        ):
            response = await routes._handle_issues_first_page(_key(), gh, {})
        self.assertEqual(response.status, 502)
        self.assertEqual(_body(response)["code"], "provider_error")

    async def test_the_issues_route_dispatches_to_it_for_open_state_only(self):
        with (
            _connected(),
            mock.patch.object(
                routes,
                "_handle_issues_first_page",
                new=AsyncMock(return_value=web.json_response({})),
            ) as branch,
        ):
            await routes._handle_issues(
                _get("issues", {"owner": "o", "repo": "r", "first_page": "1"})
            )
        branch.assert_awaited_once()

    async def test_the_closed_state_never_takes_the_fast_path(self):
        with (
            _connected(),
            mock.patch.object(routes, "_handle_issues_first_page") as branch,
            mock.patch.object(store, "read_issues_snapshot", return_value={"rows": []}),
        ):
            response = await routes._handle_issues(
                _get("issues", {"owner": "o", "repo": "r", "first_page": "1", "state": "closed"})
            )
        branch.assert_not_called()
        self.assertEqual(_body(response)["state"], "closed")


class TestPullsFirstPage(unittest.IsolatedAsyncioTestCase):
    """The PR counterpart, and the bigger win: a cold ``/pulls`` blocks on both
    the full pagination AND the GraphQL enrichment before rendering."""

    async def test_a_warm_cache_is_served_complete_with_the_bulk_cap(self):
        with (
            mock.patch.object(store, "read_pulls_snapshot", return_value={"rows": [{"number": 1}]}),
            mock.patch.object(gh, "list_open_pulls_first_page") as fetch,
        ):
            response = await routes._handle_pulls_first_page(_key(), gh, {})
        payload = _body(response)
        self.assertFalse(payload["partial"])
        self.assertEqual(payload["bulk_max"], routes._BULK_PR_MAX)
        fetch.assert_not_called()

    async def test_a_cold_cache_returns_one_un_enriched_page_marked_partial(self):
        with (
            mock.patch.object(store, "read_pulls_snapshot", return_value=None),
            mock.patch.object(gh, "list_open_pulls_first_page", return_value=[{"number": 1}]),
            mock.patch.object(gh, "enrich_pulls") as enrich,
            mock.patch.object(store, "write_pulls_cache") as write,
        ):
            response = await routes._handle_pulls_first_page(_key(), gh, {})
        payload = _body(response)
        self.assertTrue(payload["partial"])
        # Enrichment is the other slow leg; paying it here would defeat the point.
        enrich.assert_not_called()
        write.assert_not_called()

    async def test_a_provider_failure_carries_a_machine_readable_code(self):
        with (
            mock.patch.object(store, "read_pulls_snapshot", return_value=None),
            mock.patch.object(
                gh, "list_open_pulls_first_page", side_effect=gh.GhCliError("gh down")
            ),
        ):
            response = await routes._handle_pulls_first_page(_key(), gh, {})
        self.assertEqual(response.status, 502)
        self.assertEqual(_body(response)["code"], "provider_error")

    async def test_the_pulls_route_dispatches_to_it_for_open_state_only(self):
        with (
            _connected(),
            mock.patch.object(
                routes,
                "_handle_pulls_first_page",
                new=AsyncMock(return_value=web.json_response({})),
            ) as branch,
        ):
            await routes._handle_pulls(
                _get("pulls", {"owner": "o", "repo": "r", "first_page": "1"})
            )
        branch.assert_awaited_once()


class TestPullsListCachePolicy(unittest.IsolatedAsyncioTestCase):
    """The list cache has no TTL, so a row whose enrichment failed must NOT be
    persisted — and on a forced refresh the previous entry has to be dropped too,
    or the next plain request would serve those older rows."""

    def _req(self, query: dict | None = None) -> web.Request:
        base = {"owner": "o", "repo": "r"}
        base.update(query or {})
        return _get("pulls", base)

    async def test_a_bad_state_is_400(self):
        response = await routes._handle_pulls(self._req({"state": "merged"}))
        self.assertEqual(response.status, 400)

    async def test_a_missing_repo_is_400(self):
        response = await routes._handle_pulls(_get("pulls", {"owner": "o"}))
        self.assertEqual(response.status, 400)

    async def test_an_unconnected_repo_is_404(self):
        with _connected(False):
            response = await routes._handle_pulls(self._req())
        self.assertEqual(response.status, 404)

    async def test_a_warm_snapshot_is_served_from_cache(self):
        with (
            _connected(),
            mock.patch.object(store, "read_pulls_snapshot", return_value={"rows": [{"number": 1}]}),
            mock.patch.object(gh, "list_open_pulls") as fetch,
        ):
            response = await routes._handle_pulls(self._req())
        self.assertTrue(_body(response)["from_cache"])
        fetch.assert_not_called()

    async def test_a_provider_failure_is_502(self):
        with (
            _connected(),
            mock.patch.object(store, "read_pulls_snapshot", return_value=None),
            mock.patch.object(gh, "list_open_pulls", side_effect=gh.GhCliError("gh down")),
        ):
            response = await routes._handle_pulls(self._req())
        self.assertEqual(response.status, 502)

    async def test_fully_enriched_rows_are_cached(self):
        rows = [{"number": 1}]
        with (
            _connected(),
            mock.patch.object(store, "read_pulls_snapshot", return_value=None),
            mock.patch.object(gh, "list_open_pulls", return_value=rows),
            mock.patch.object(gh, "enrich_pulls", return_value=rows),
            mock.patch.object(gh, "enrichment_complete", return_value=True),
            mock.patch.object(store, "write_pulls_cache") as write,
            mock.patch.object(store, "drop_pulls_cache") as drop,
        ):
            response = await routes._handle_pulls(self._req())
        write.assert_called_once()
        drop.assert_not_called()
        self.assertFalse(_body(response)["from_cache"])

    async def test_incomplete_enrichment_drops_the_stale_entry_instead_of_caching(self):
        rows = [{"number": 1}]
        with (
            _connected(),
            mock.patch.object(store, "read_pulls_snapshot", return_value=None),
            mock.patch.object(gh, "list_open_pulls", return_value=rows),
            mock.patch.object(gh, "enrich_pulls", return_value=rows),
            mock.patch.object(gh, "enrichment_complete", return_value=False),
            mock.patch.object(store, "write_pulls_cache") as write,
            mock.patch.object(store, "drop_pulls_cache") as drop,
        ):
            response = await routes._handle_pulls(self._req({"refresh": "1"}))
        write.assert_not_called()
        drop.assert_called_once()
        # The list itself is still useful without the card decoration.
        self.assertEqual(response.status, 200)

    async def test_a_poll_that_the_probe_invalidates_refetches(self):
        rows = [{"number": 1}]
        with (
            _connected(),
            mock.patch.object(store, "read_pulls_snapshot", return_value={"rows": []}),
            mock.patch.object(
                routes, "_poll_can_serve_cache", new=AsyncMock(return_value=(False, {"n": 1}))
            ),
            mock.patch.object(gh, "list_open_pulls", return_value=rows),
            mock.patch.object(gh, "enrich_pulls", return_value=rows),
            mock.patch.object(gh, "enrichment_complete", return_value=True),
            mock.patch.object(store, "write_pulls_cache") as write,
        ):
            response = await routes._handle_pulls(self._req({"poll": "1"}))
        self.assertFalse(_body(response)["from_cache"])
        # The poll fingerprint rides along so rows and probe land in one write.
        self.assertEqual(write.call_args.kwargs["probe"], {"n": 1})

    async def test_a_poll_the_probe_clears_keeps_serving_the_cache(self):
        with (
            _connected(),
            mock.patch.object(store, "read_pulls_snapshot", return_value={"rows": [{"number": 1}]}),
            mock.patch.object(
                routes, "_poll_can_serve_cache", new=AsyncMock(return_value=(True, None))
            ),
            mock.patch.object(gh, "list_open_pulls") as fetch,
        ):
            response = await routes._handle_pulls(self._req({"poll": "1"}))
        self.assertTrue(_body(response)["from_cache"])
        fetch.assert_not_called()


class TestIssuesListPollPolicy(unittest.IsolatedAsyncioTestCase):
    def _req(self, query: dict | None = None) -> web.Request:
        base = {"owner": "o", "repo": "r"}
        base.update(query or {})
        return _get("issues", base)

    async def test_a_bad_state_is_400(self):
        response = await routes._handle_issues(self._req({"state": "all"}))
        self.assertEqual(response.status, 400)

    async def test_a_poll_that_the_probe_invalidates_refetches_with_the_probe(self):
        with (
            _connected(),
            mock.patch.object(store, "read_issues_snapshot", return_value={"rows": []}),
            mock.patch.object(
                routes, "_poll_can_serve_cache", new=AsyncMock(return_value=(False, {"n": 2}))
            ),
            mock.patch.object(
                store, "refresh_issues_cache", return_value=[{"number": 7}]
            ) as refresh,
        ):
            response = await routes._handle_issues(self._req({"poll": "1"}))
        self.assertFalse(_body(response)["from_cache"])
        self.assertEqual(refresh.call_args.kwargs["probe"], {"n": 2})

    async def test_a_provider_failure_is_502(self):
        with (
            _connected(),
            mock.patch.object(store, "read_issues_snapshot", return_value=None),
            mock.patch.object(store, "refresh_issues_cache", side_effect=gh.GhCliError("gh down")),
        ):
            response = await routes._handle_issues(self._req())
        self.assertEqual(response.status, 502)

    async def test_the_closed_list_uses_the_closed_fetch(self):
        with (
            _connected(),
            mock.patch.object(store, "read_issues_snapshot", return_value=None),
            mock.patch.object(store, "refresh_issues_cache", return_value=[]) as refresh,
        ):
            response = await routes._handle_issues(self._req({"state": "closed"}))
        self.assertEqual(refresh.call_args.kwargs["state"], "closed")
        self.assertEqual(_body(response)["state"], "closed")


# ── investigation record ─────────────────────────────────────────────────────


class TestItemKind(unittest.TestCase):
    """``None`` means INVALID, so the caller answers 400 rather than silently
    reading the wrong record: on GitLab the kind is part of an item's identity."""

    def test_absent_and_empty_default_to_issue(self):
        self.assertEqual(routes._item_kind(None), "issue")
        self.assertEqual(routes._item_kind(""), "issue")

    def test_the_known_kinds_pass_through(self):
        for kind in provider.ITEM_KINDS:
            self.assertEqual(routes._item_kind(kind), kind)

    def test_anything_else_is_invalid(self):
        for raw in ("Issue", "merge_request", 7, [], True):
            self.assertIsNone(routes._item_kind(raw))


class TestPutInvestigationRoute(unittest.IsolatedAsyncioTestCase):
    """Local triage state only — nothing is written to the provider. The number
    becomes part of the record's FILENAME, so the bound matters more here than on
    a read."""

    GOOD = {"owner": "o", "repo": "r", "number": 7}

    async def test_a_malformed_payload_is_400(self):
        for body in (None, [], "x"):
            response = await routes._handle_put_investigation(
                _json_request("PUT", "investigation", body)
            )
            self.assertEqual(response.status, 400)

    async def test_a_missing_repo_is_400(self):
        response = await routes._handle_put_investigation(
            _json_request("PUT", "investigation", {"owner": "o", "number": 7})
        )
        self.assertEqual(response.status, 400)

    async def test_a_json_boolean_is_not_item_one(self):
        response = await routes._handle_put_investigation(
            _json_request("PUT", "investigation", {**self.GOOD, "number": True})
        )
        self.assertEqual(response.status, 400)

    async def test_an_absurd_number_is_refused_with_its_own_code(self):
        response = await routes._handle_put_investigation(
            _json_request(
                "PUT", "investigation", {**self.GOOD, "number": routes.MAX_ITEM_NUMBER + 1}
            )
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(_body(response)["code"], "item_number_out_of_range")

    async def test_an_unconnected_repo_is_404(self):
        with _connected(False):
            response = await routes._handle_put_investigation(
                _json_request("PUT", "investigation", self.GOOD)
            )
        self.assertEqual(response.status, 404)

    async def test_an_unknown_kind_is_400(self):
        with _connected():
            response = await routes._handle_put_investigation(
                _json_request("PUT", "investigation", {**self.GOOD, "kind": "merge_request"})
            )
        self.assertEqual(response.status, 400)

    async def test_only_the_known_keys_are_merged_into_the_record(self):
        with (
            _connected(),
            mock.patch.object(
                store, "write_investigation", return_value={"status": "in_progress"}
            ) as write,
        ):
            response = await routes._handle_put_investigation(
                _json_request(
                    "PUT",
                    "investigation",
                    {
                        **self.GOOD,
                        "status": "in_progress",
                        "findings": "it is a dupe",
                        "slot_key": "s1",
                        "folder_id": "f1",
                        "verdict": "ignored",
                    },
                )
            )
        patch = write.call_args[0][3]
        self.assertEqual(sorted(patch), ["findings", "folder_id", "slot_key", "status"])
        self.assertEqual(_body(response)["investigation"], {"status": "in_progress"})

    async def test_an_empty_patch_is_valid(self):
        with (
            _connected(),
            mock.patch.object(store, "write_investigation", return_value={}) as write,
        ):
            response = await routes._handle_put_investigation(
                _json_request("PUT", "investigation", self.GOOD)
            )
        self.assertEqual(write.call_args[0][3], {})
        self.assertEqual(response.status, 200)


class TestGetInvestigationRoute(unittest.IsolatedAsyncioTestCase):
    async def test_missing_query_parameters_are_400(self):
        response = await routes._handle_get_investigation(
            _get("investigation", {"owner": "o", "repo": "r"})
        )
        self.assertEqual(response.status, 400)

    async def test_an_unknown_kind_is_400_after_the_connected_gate(self):
        with _connected():
            response = await routes._handle_get_investigation(
                _get("investigation", {"owner": "o", "repo": "r", "number": "7", "kind": "mr"})
            )
        self.assertEqual(response.status, 400)

    async def test_a_never_investigated_item_answers_null(self):
        with _connected(), mock.patch.object(store, "read_investigation", return_value=None):
            response = await routes._handle_get_investigation(
                _get("investigation", {"owner": "o", "repo": "r", "number": "7"})
            )
        payload = _body(response)
        self.assertIsNone(payload["investigation"])
        self.assertEqual(payload["kind"], "issue")
