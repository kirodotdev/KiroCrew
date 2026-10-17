"""Issue Radar's repository-wide labelling routes, and the list routes' entry guards.

This file pins the label-recommendation routes (the GET that never runs the model,
the POST that caches and stamps a generated set) and the cache-first open-issue
loader they read; the entry guards the issues and pull-detail routes run before any
provider call; the tagging prompt, the guards that turn a malformed model shape into
no suggestions, and the tagging GET and POST routes; and the add-only bulk label
apply, which reports a per-issue failure instead of raising. The proposal
validators and the single-issue apply live in
``test_issue_radar_routes_ai_coverage.py`` and the shared request builders and
patch helpers in ``issue_radar_routes_ai_coverage_helpers.py``.
"""

import unittest
from unittest import mock
from unittest.mock import AsyncMock

from aiohttp import web
from issue_radar_routes_ai_coverage_helpers import (
    LABELS,
    _audits,
    _body,
    _connected,
    _get,
    _json_request,
    _key,
    _oneshot,
    _writable,
)

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import routes, store


class TestGetRecommendationsRoute(unittest.IsolatedAsyncioTestCase):
    """Read-only: this route NEVER runs the model (that is the POST)."""

    async def test_a_missing_repo_is_400(self):
        response = await routes._handle_get_recommendations(_get("recommendations", {"owner": "o"}))
        self.assertEqual(response.status, 400)

    async def test_an_unconnected_repo_is_404(self):
        with _connected(False):
            response = await routes._handle_get_recommendations(
                _get("recommendations", {"owner": "o", "repo": "r"})
            )
        self.assertEqual(response.status, 404)

    async def test_no_cache_answers_null_rather_than_an_empty_list(self):
        # An empty list would read as "analysed, nothing to propose"; null is
        # "never generated", which is what the button state depends on.
        with (
            _connected(),
            mock.patch.object(store, "read_recommendations_cache", return_value=None),
        ):
            response = await routes._handle_get_recommendations(
                _get("recommendations", {"owner": "o", "repo": "r"})
            )
        payload = _body(response)
        self.assertIsNone(payload["recommendations"])
        self.assertIsNone(payload["generated_at"])
        self.assertFalse(payload["from_cache"])

    async def test_a_cached_result_is_served(self):
        cached = {"recommendations": [{"name": "x"}], "generated_at": "2026-01-01T00:00:00Z"}
        with (
            _connected(),
            mock.patch.object(store, "read_recommendations_cache", return_value=cached),
        ):
            response = await routes._handle_get_recommendations(
                _get("recommendations", {"owner": "o", "repo": "r"})
            )
        payload = _body(response)
        self.assertEqual(payload["recommendations"], [{"name": "x"}])
        self.assertTrue(payload["from_cache"])


class TestGenerateRecommendationsRoute(unittest.IsolatedAsyncioTestCase):
    GOOD = {"owner": "o", "repo": "r"}

    async def test_a_malformed_payload_is_400(self):
        for body in (None, [], 7):
            response = await routes._handle_generate_recommendations(
                _json_request("POST", "recommendations", body)
            )
            self.assertEqual(response.status, 400)

    async def test_a_missing_repo_is_400(self):
        response = await routes._handle_generate_recommendations(
            _json_request("POST", "recommendations", {"owner": "o"})
        )
        self.assertEqual(response.status, 400)

    async def test_an_unconnected_repo_is_404(self):
        with _connected(False):
            response = await routes._handle_generate_recommendations(
                _json_request("POST", "recommendations", self.GOOD)
            )
        self.assertEqual(response.status, 404)

    async def test_a_provider_failure_loading_the_inputs_is_502(self):
        with (
            _connected(),
            mock.patch.object(
                routes, "_load_labels_for_ai", new=AsyncMock(side_effect=gh.GhCliError("gh down"))
            ),
        ):
            response = await routes._handle_generate_recommendations(
                _json_request("POST", "recommendations", self.GOOD)
            )
        self.assertEqual(response.status, 502)
        self.assertIn("gh down", _body(response)["error"])

    async def test_a_failed_model_call_is_502(self):
        with (
            _connected(),
            mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=LABELS)),
            mock.patch.object(routes, "_load_open_issues_for_reco", new=AsyncMock(return_value=[])),
            mock.patch.object(
                routes,
                "_compute_label_recommendations",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            response = await routes._handle_generate_recommendations(
                _json_request("POST", "recommendations", self.GOOD)
            )
        self.assertEqual(response.status, 502)
        self.assertIn("gateway logs", _body(response)["error"])

    async def test_a_successful_generate_caches_and_stamps_the_result(self):
        result = {"recommendations": [{"name": "needs-repro"}]}
        with (
            _connected(),
            mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=LABELS)),
            mock.patch.object(routes, "_load_open_issues_for_reco", new=AsyncMock(return_value=[])),
            mock.patch.object(
                routes, "_compute_label_recommendations", new=AsyncMock(return_value=result)
            ),
            mock.patch.object(store, "write_recommendations_cache") as write,
        ):
            response = await routes._handle_generate_recommendations(
                _json_request("POST", "recommendations", self.GOOD)
            )
        write.assert_called_once()
        payload = _body(response)
        self.assertEqual(payload["recommendations"], result["recommendations"])
        self.assertTrue(payload["generated_at"].endswith("Z"))
        self.assertFalse(payload["from_cache"])


class TestLoadOpenIssuesForReco(unittest.IsolatedAsyncioTestCase):
    async def test_it_is_cache_first_by_default(self):
        with (
            mock.patch.object(store, "read_issues_cache", return_value=[{"number": 7}]),
            mock.patch.object(store, "refresh_issues_cache") as refresh,
        ):
            got = await routes._load_open_issues_for_reco(_key())
        self.assertEqual(got, [{"number": 7}])
        refresh.assert_not_called()

    async def test_refresh_skips_the_cache_read_entirely(self):
        # The Tagging queue needs this: labels get added on GitHub itself, and a
        # cache-first read keeps reporting those issues as untagged.
        with (
            mock.patch.object(store, "read_issues_cache") as read,
            mock.patch.object(store, "refresh_issues_cache", return_value=[]) as refresh,
        ):
            await routes._load_open_issues_for_reco(_key(), refresh=True)
        read.assert_not_called()
        refresh.assert_called_once()

    async def test_a_cache_miss_fetches_and_stores_under_one_lock(self):
        with (
            mock.patch.object(store, "read_issues_cache", return_value=None),
            mock.patch.object(store, "refresh_issues_cache", return_value=[]) as refresh,
        ):
            await routes._load_open_issues_for_reco(_key())
        refresh.assert_called_once()


# ── remaining guards on the list, tagging and bulk-apply routes ──────────────


class TestListRouteEntryGuards(unittest.IsolatedAsyncioTestCase):
    """The first two checks every list/item route runs, before any provider call."""

    async def test_the_issues_route_refuses_a_missing_repo(self):
        with mock.patch.object(store, "is_repo_connected") as gate:
            response = await routes._handle_issues(_get("issues", {"owner": "o"}))
        self.assertEqual(response.status, 400)
        gate.assert_not_called()

    async def test_the_issues_route_refuses_an_unconnected_repo(self):
        with _connected(False), mock.patch.object(store, "read_issues_snapshot") as read:
            response = await routes._handle_issues(_get("issues", {"owner": "o", "repo": "r"}))
        self.assertEqual(response.status, 404)
        read.assert_not_called()

    async def test_the_pull_detail_route_refuses_a_bad_number(self):
        with mock.patch.object(store, "is_repo_connected") as gate:
            response = await routes._handle_pull_detail(
                _get("pull", {"owner": "o", "repo": "r", "number": "0"})
            )
        self.assertEqual(response.status, 400)
        gate.assert_not_called()


class TestBuildTaggingPrompt(unittest.TestCase):
    def test_a_long_body_is_truncated_and_newlines_collapse(self):
        prompt = routes._build_tagging_prompt(
            "o", "r", LABELS, [{"number": 7, "title": "t", "body": "a\r\nb" + "c" * 900}]
        )
        self.assertIn("…", prompt)
        self.assertIn("#7 t — a b", prompt)

    def test_an_empty_batch_is_named(self):
        self.assertIn("(no untagged issues)", routes._build_tagging_prompt("o", "r", LABELS, []))


class TestComputeTaggingSuggestionsShapeGuards(unittest.IsolatedAsyncioTestCase):
    """The model's SHAPE is untrusted too, not just its values: a scalar where a
    list belongs must yield "no suggestions", never a TypeError the route reports
    to the user as a 502."""

    BATCH = [{"number": 7, "title": "crash", "body": "boom"}]

    async def _compute(self, text: str) -> dict:
        with _oneshot(text):
            return await routes._compute_tagging_suggestions(
                _get("tagging"), "o", "r", LABELS, self.BATCH
            )

    async def test_a_non_object_assignment_is_skipped(self):
        got = await self._compute('{"assignments": ["nope", 7, null]}')
        self.assertEqual(got, {})

    async def test_a_non_numeric_string_number_is_skipped(self):
        got = await self._compute('{"assignments": [{"number": "seven", "labels": ["bug"]}]}')
        self.assertEqual(got, {})

    async def test_a_numeric_string_number_is_accepted(self):
        got = await self._compute('{"assignments": [{"number": "7", "labels": ["bug"]}]}')
        self.assertEqual(got, {"7": [{"name": "bug", "reason": ""}]})

    async def test_a_junk_label_entry_is_skipped(self):
        got = await self._compute('{"assignments": [{"number": 7, "labels": [7, null, []]}]}')
        self.assertEqual(got, {})

    async def test_a_non_string_label_name_is_skipped(self):
        got = await self._compute('{"assignments": [{"number": 7, "labels": [{"name": 5}]}]}')
        self.assertEqual(got, {})


class TestGetTaggingRoute(unittest.IsolatedAsyncioTestCase):
    """Never runs the model, so opening the dashboard costs nothing."""

    async def test_a_missing_repo_is_400(self):
        response = await routes._handle_get_tagging(_get("tagging", {"owner": "o"}))
        self.assertEqual(response.status, 400)

    async def test_a_provider_failure_loading_the_queue_is_502(self):
        with (
            _connected(),
            mock.patch.object(
                routes,
                "_load_open_issues_for_reco",
                new=AsyncMock(side_effect=gh.GhCliError("gh down")),
            ),
        ):
            response = await routes._handle_get_tagging(
                _get("tagging", {"owner": "o", "repo": "r"})
            )
        self.assertEqual(response.status, 502)
        self.assertIn("gh down", _body(response)["error"])

    async def test_a_non_object_row_in_the_open_list_does_not_break_the_counts(self):
        issues = [
            {"number": 7, "title": "a", "labels": [], "created_at": "2"},
            {"number": 8, "title": "b", "labels": ["bug", "", 7], "created_at": "1"},
            "not-a-dict",
        ]
        with (
            _connected(),
            mock.patch.object(
                routes, "_load_open_issues_for_reco", new=AsyncMock(return_value=issues)
            ),
            mock.patch.object(store, "read_tagging_cache", return_value=None),
        ):
            response = await routes._handle_get_tagging(
                _get("tagging", {"owner": "o", "repo": "r"})
            )
        payload = _body(response)
        self.assertEqual(payload["untagged"], [7])
        self.assertEqual(payload["label_counts"], {"bug": 1})
        self.assertEqual(payload["bulk_max"], routes._TAG_BULK_MAX)


class TestGenerateTaggingRoute(unittest.IsolatedAsyncioTestCase):
    GOOD = {"owner": "o", "repo": "r"}
    ISSUES = [{"number": 7, "title": "crash", "body": "boom", "labels": [], "created_at": "1"}]

    async def test_a_malformed_payload_is_400(self):
        for body in (None, [], 7):
            response = await routes._handle_generate_tagging(_json_request("POST", "tagging", body))
            self.assertEqual(response.status, 400)

    async def test_a_non_array_numbers_field_is_400(self):
        response = await routes._handle_generate_tagging(
            _json_request("POST", "tagging", {**self.GOOD, "numbers": 7})
        )
        self.assertEqual(response.status, 400)
        self.assertIn("must be an array", _body(response)["error"])

    async def test_an_unconnected_repo_is_404(self):
        with _connected(False):
            response = await routes._handle_generate_tagging(
                _json_request("POST", "tagging", self.GOOD)
            )
        self.assertEqual(response.status, 404)

    async def test_a_provider_failure_loading_the_inputs_is_502(self):
        with (
            _connected(),
            mock.patch.object(
                routes, "_load_labels_for_ai", new=AsyncMock(side_effect=gh.GhCliError("gh down"))
            ),
        ):
            response = await routes._handle_generate_tagging(
                _json_request("POST", "tagging", self.GOOD)
            )
        self.assertEqual(response.status, 502)

    async def test_a_failed_model_call_is_502(self):
        with (
            _connected(),
            mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=LABELS)),
            mock.patch.object(
                routes, "_load_open_issues_for_reco", new=AsyncMock(return_value=self.ISSUES)
            ),
            mock.patch.object(store, "read_tagging_cache", return_value=None),
            mock.patch.object(
                routes,
                "_compute_tagging_suggestions",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            response = await routes._handle_generate_tagging(
                _json_request("POST", "tagging", self.GOOD)
            )
        self.assertEqual(response.status, 502)
        self.assertIn("gateway logs", _body(response)["error"])


class TestLabelsApplyBulkRoute(unittest.IsolatedAsyncioTestCase):
    """Add-only, write-gated, and partial failure is REPORTED rather than
    swallowed: every issue that did succeed stays applied."""

    def _req(self, body: object) -> web.Request:
        return _json_request("POST", "labels/apply-bulk", body)

    async def test_a_malformed_payload_is_400(self):
        for body in (None, [], "x"):
            response = await routes._handle_labels_apply_bulk(self._req(body))
            self.assertEqual(response.status, 400)

    async def test_a_non_object_change_is_400(self):
        response = await routes._handle_labels_apply_bulk(
            self._req({"owner": "o", "repo": "r", "changes": ["nope"]})
        )
        self.assertEqual(response.status, 400)
        self.assertIn("must be a JSON object", _body(response)["error"])

    async def test_a_change_without_an_add_array_is_400(self):
        response = await routes._handle_labels_apply_bulk(
            self._req({"owner": "o", "repo": "r", "changes": [{"number": 7, "add": "docs"}]})
        )
        self.assertEqual(response.status, 400)
        self.assertIn("'add' array", _body(response)["error"])

    async def test_a_read_only_repo_is_403_and_audited(self):
        with _connected(), _writable(False), _audits() as audit:
            response = await routes._handle_labels_apply_bulk(
                self._req({"owner": "o", "repo": "r", "changes": [{"number": 7, "add": ["docs"]}]})
            )
        self.assertEqual(response.status, 403)
        self.assertEqual(audit.call_args[0][2], "denied")

    async def test_a_provider_failure_reading_the_label_set_is_502(self):
        with (
            _connected(),
            _writable(),
            _audits(),
            mock.patch.object(
                routes,
                "_load_labels_for_ai",
                new=AsyncMock(side_effect=gh.GhCliError("gh down")),
            ),
        ):
            response = await routes._handle_labels_apply_bulk(
                self._req({"owner": "o", "repo": "r", "changes": [{"number": 7, "add": ["docs"]}]})
            )
        self.assertEqual(response.status, 502)

    async def test_a_per_issue_permission_failure_is_reported_not_raised(self):
        def _apply(key, number, add, remove):
            if number == 8:
                raise gh.GhPermissionError("locked")
            return [{"name": "docs"}]

        with (
            _connected(),
            _writable(),
            _audits(),
            mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=LABELS)),
            mock.patch.object(routes, "_apply_label_change", side_effect=_apply),
            mock.patch.object(store, "drop_tagging_suggestions") as prune,
        ):
            response = await routes._handle_labels_apply_bulk(
                self._req(
                    {
                        "owner": "o",
                        "repo": "r",
                        "changes": [
                            {"number": 7, "add": ["docs"]},
                            {"number": 8, "add": ["docs"]},
                        ],
                    }
                )
            )
        payload = _body(response)
        self.assertEqual([row["number"] for row in payload["applied"]], [7])
        self.assertEqual(payload["failed"], [{"number": 8, "error": "locked"}])
        # Only the issue that actually got labelled leaves the queue.
        self.assertEqual(prune.call_args[0][2], [7])

    async def test_a_failed_prune_does_not_fail_the_applied_batch(self):
        with (
            _connected(),
            _writable(),
            _audits(),
            mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=LABELS)),
            mock.patch.object(routes, "_apply_label_change", return_value=[{"name": "docs"}]),
            mock.patch.object(store, "drop_tagging_suggestions", side_effect=OSError("disk full")),
        ):
            response = await routes._handle_labels_apply_bulk(
                self._req({"owner": "o", "repo": "r", "changes": [{"number": 7, "add": ["docs"]}]})
            )
        self.assertEqual(response.status, 200)
        self.assertEqual(len(_body(response)["applied"]), 1)
