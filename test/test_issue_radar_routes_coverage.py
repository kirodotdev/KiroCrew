"""Coverage tests for Issue Radar's HTTP route layer (``backend/routes.py``).

The module's existing tests cover a few deep behaviours (the open-list poll probe,
the ``/ref`` hover route, the tagging dashboard, the connect dialog's repo picker).
What was left almost entirely unexercised is the part every one of the ~35 routes
runs FIRST: request validation, the connected-repo gate, the write-permission
gate, and the error-status taxonomy that maps a provider failure onto 400 / 403 /
404 / 409 / 502.

That is what this file covers, at three levels:

  * the **pure field parsers** (``_pr_number_field``, ``_pr_head_sha_field``,
    ``_pr_numbers_field``, ``_pr_head_shas_field``, ``_pr_body_field``,
    ``_pr_merge_method_field``, ``_item_kind``, ``_valid_hex6``,
    ``_short_rationale``, ``_has_write_access``) — each one is the single place a
    bound or an allowlist lives, and a deleted bound is invisible without a test
    that names the refusal;
  * the **shared preamble and error mapper** (``_pr_action_preamble``,
    ``_pr_action_error``, ``_refuse_if_head_moved``) — the security-relevant code
    that every mutating pull-request route delegates to;
  * the **handlers** — that each one actually reaches those checks in the right
    ORDER: a missing ``owner`` answers 400 before any provider call, an
    unconnected repo answers 404 before any cache read, and a read-only repo
    answers 403 before any write.

Everything is patched at the ``github_client`` / ``store`` boundary, so no ``gh``
subprocess runs, no network is touched, and nothing is written outside the
per-test ``KIROCREW_HOME`` that ``conftest.py`` pins. No sleeps and no
wall-clock assertions, so ordering and duration cannot make these flaky.

This file pins the field parsers, the write-permission gate, the shared error mapper,
preamble and stale-head check, the enablement gate and route registration, the member
loader, and the connect, label-list, member, repository, login, settings, disconnect,
item-detail, pull-search and investigation-read handlers. The pull-request action routes,
the workflow-runs read and the shared-gate sweep over every mutating pull-request handler
live in ``test_issue_radar_routes_coverage_pull_actions.py``; the label-creation, settings
label-append and issue-state routes in
``test_issue_radar_routes_coverage_label_and_issue_writes.py``; the pure model-input shapers
(the tagging filter, the pull-request summary's comment rows, fingerprint and lifecycle
phrase, and the issue-triage prompt) in
``test_issue_radar_routes_coverage_model_inputs.py``; and the shared request builders and
patch helpers in ``issue_radar_routes_coverage_helpers.py``.
"""
import unittest
from unittest import mock
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from dashboard_owner_helpers import NoConfiguredOwner
from issue_radar_routes_coverage_helpers import (
    BASE,
    OTHER_SHA,
    SHA,
    _body,
    _connected,
    _get,
    _json_request,
    _key,
    _writable,
)

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import routes, store

# ── pure request-field parsers ───────────────────────────────────────────────


class TestStrField(unittest.TestCase):
    """``_str_field`` exists so a truthy NON-string body value is "missing"
    rather than an AttributeError surfacing as a 500."""

    def test_trims_a_string(self):
        self.assertEqual(routes._str_field({"owner": "  acme "}, "owner"), "acme")

    def test_a_missing_key_is_empty(self):
        self.assertEqual(routes._str_field({}, "owner"), "")

    def test_truthy_non_strings_are_empty_not_coerced(self):
        # str(value) would stringify these into something that passes the
        # "missing" check and then fails a LATER gate — turning a plainly
        # malformed request from a 400 into a 404 or a 500.
        values: tuple[object, ...] = (1, [], {}, ["a"], 0.5, True, None)
        for value in values:
            self.assertEqual(routes._str_field({"owner": value}, "owner"), "")


class TestKeyFromBody(unittest.TestCase):
    def test_builds_a_github_key_by_default(self):
        key = routes._key_from_body({"owner": " o ", "repo": "r"})
        self.assertEqual((key.owner, key.repo), ("o", "r"))
        self.assertEqual(key.provider, "github")

    def test_non_string_owner_does_not_become_a_key(self):
        key = routes._key_from_body({"owner": 7, "repo": ["r"]})
        self.assertEqual((key.owner, key.repo), ("", ""))

    def test_an_unknown_provider_collapses_to_github(self):
        key = routes._key_from_body({"owner": "o", "repo": "r", "provider": "bitbucket"})
        self.assertEqual(key.provider, "github")


class TestAccountKey(unittest.TestCase):
    """``/me`` and ``/recent-repos`` are account-scoped: no repo, host still
    normalized rather than trusted from the client."""

    def test_carries_no_repo(self):
        key = routes._account_key(_get("me"))
        self.assertEqual((key.owner, key.repo), ("", ""))

    def test_a_crafted_host_cannot_ride_on_a_github_key(self):
        key = routes._account_key(_get("me", {"provider": "github", "host": "evil.example"}))
        self.assertEqual(key.host, "github.com")


class TestIdentityEcho(unittest.TestCase):
    def test_every_response_echoes_provider_and_host(self):
        self.assertEqual(
            routes._identity(_key()),
            {"owner": "o", "repo": "r", "provider": "github", "host": "github.com"},
        )


class TestParseItemNumber(unittest.TestCase):
    def test_a_non_integer_is_400(self):
        number, err = routes._parse_item_number("twelve")
        self.assertEqual(number, 0)
        assert err is not None
        self.assertEqual(err.status, 400)

    def test_zero_and_negatives_are_400(self):
        for raw in ("0", "-1"):
            _, err = routes._parse_item_number(raw)
            assert err is not None
            self.assertEqual(err.status, 400)

    def test_the_upper_bound_is_enforced(self):
        _, err = routes._parse_item_number(str(routes.MAX_ITEM_NUMBER + 1))
        assert err is not None
        self.assertEqual(err.status, 400)
        self.assertIn("at most", _body(err)["error"])

    def test_a_valid_number_passes(self):
        self.assertEqual(routes._parse_item_number("533"), (533, None))


class TestPrNumberField(unittest.TestCase):
    """The BODY counterpart of ``_parse_item_number``."""

    def test_a_json_boolean_is_not_pull_request_one(self):
        # bool subclasses int, so `true` would otherwise validate as #1 and act on
        # a real pull request the caller never named.
        _, err = routes._pr_number_field({"number": True})
        assert err is not None
        self.assertEqual(err.status, 400)
        self.assertEqual(_body(err)["code"], "invalid_number")

    def test_non_integers_and_non_positives_are_refused(self):
        for value in ("5", 0, -2, None, 1.5, [5]):
            _, err = routes._pr_number_field({"number": value})
            assert err is not None
            self.assertEqual(err.status, 400)

    def test_an_out_of_range_number_has_its_own_code(self):
        _, err = routes._pr_number_field({"number": routes.MAX_ITEM_NUMBER + 1})
        assert err is not None
        self.assertEqual(_body(err)["code"], "number_out_of_range")

    def test_a_valid_number_passes(self):
        self.assertEqual(routes._pr_number_field({"number": 12}), (12, None))


class TestPrHeadShaField(unittest.TestCase):
    def test_a_missing_sha_is_refused(self):
        _, err = routes._pr_head_sha_field({})
        assert err is not None
        self.assertEqual(err.status, 400)
        self.assertEqual(_body(err)["code"], "head_sha_required")

    def test_a_non_hex_or_too_short_sha_is_refused(self):
        for value in ("zzzzzzz", "abc", "a" * 65, 12, None):
            _, err = routes._pr_head_sha_field({"head_sha": value})
            assert err is not None
            self.assertEqual(err.status, 400)

    def test_an_abbreviated_and_a_full_sha_both_pass(self):
        self.assertEqual(routes._pr_head_sha_field({"head_sha": "abcdef1"}), ("abcdef1", None))
        self.assertEqual(routes._pr_head_sha_field({"head_sha": SHA}), (SHA, None))


class TestPrNumbersField(unittest.TestCase):
    def test_a_missing_or_empty_array_is_refused(self):
        bodies: tuple[dict, ...] = ({}, {"numbers": []}, {"numbers": "1,2"}, {"numbers": 5})
        for body in bodies:
            _, err = routes._pr_numbers_field(body)
            assert err is not None
            self.assertEqual(err.status, 400)
            self.assertEqual(_body(err)["code"], "numbers_required")

    def test_the_batch_size_is_capped(self):
        _, err = routes._pr_numbers_field({"numbers": list(range(1, routes._BULK_PR_MAX + 2))})
        assert err is not None
        self.assertEqual(_body(err)["code"], "too_many_pulls")

    def test_a_boolean_entry_is_not_pull_request_one(self):
        _, err = routes._pr_numbers_field({"numbers": [1, True]})
        assert err is not None
        self.assertEqual(_body(err)["code"], "invalid_number")

    def test_non_positive_and_non_integer_entries_are_refused(self):
        for entry in (0, -1, "3", None, 2.0):
            _, err = routes._pr_numbers_field({"numbers": [entry]})
            assert err is not None
            self.assertEqual(_body(err)["code"], "invalid_number")

    def test_an_entry_over_the_bound_is_refused(self):
        _, err = routes._pr_numbers_field({"numbers": [routes.MAX_ITEM_NUMBER + 1]})
        assert err is not None
        self.assertEqual(_body(err)["code"], "number_out_of_range")

    def test_duplicates_are_dropped_while_order_is_preserved(self):
        # A repeated number would otherwise be acted on twice — a wasted call and
        # a confusing duplicate row in the per-PR response.
        numbers, err = routes._pr_numbers_field({"numbers": [7, 3, 7, 9, 3]})
        self.assertIsNone(err)
        self.assertEqual(numbers, [7, 3, 9])


class TestPrHeadShasField(unittest.TestCase):
    def test_the_map_is_required_for_a_pinned_action(self):
        _, err = routes._pr_head_shas_field({}, [1])
        assert err is not None
        self.assertEqual(_body(err)["code"], "head_shas_required")

    def test_an_array_is_refused_because_the_pairing_must_be_by_number(self):
        # A client that reorders or filters its selection would otherwise pair a
        # sha with the wrong pull request.
        _, err = routes._pr_head_shas_field({"head_shas": [SHA]}, [1])
        assert err is not None
        self.assertEqual(err.status, 400)

    def test_every_requested_number_must_be_present_and_valid(self):
        for raw in ({"1": SHA}, {"1": SHA, "2": "nope"}, {"1": SHA, "2": 7}):
            _, err = routes._pr_head_shas_field({"head_shas": raw}, [1, 2])
            assert err is not None
            self.assertEqual(_body(err)["code"], "head_shas_required")

    def test_a_complete_map_is_keyed_back_by_int(self):
        out, err = routes._pr_head_shas_field({"head_shas": {"1": SHA, "2": f" {OTHER_SHA} "}}, [1, 2])
        self.assertIsNone(err)
        self.assertEqual(out, {1: SHA, 2: OTHER_SHA})


class TestPrBodyField(unittest.TestCase):
    def test_an_oversized_body_is_refused(self):
        _, err = routes._pr_body_field({"body": "x" * (routes._PR_BODY_MAX_CHARS + 1)})
        assert err is not None
        self.assertEqual(_body(err)["code"], "body_too_long")

    def test_a_bounded_body_passes_and_is_trimmed(self):
        self.assertEqual(routes._pr_body_field({"body": "  hi "}), ("hi", None))

    def test_the_field_name_is_configurable(self):
        _, err = routes._pr_body_field({"note": "y" * (routes._PR_BODY_MAX_CHARS + 1)}, "note")
        assert err is not None
        self.assertIn("'note'", _body(err)["error"])


class TestPrMergeMethodField(unittest.TestCase):
    def test_it_defaults_to_squash(self):
        self.assertEqual(routes._pr_merge_method_field({}, _key()), ("SQUASH", None))

    def test_a_lowercase_method_is_accepted(self):
        self.assertEqual(routes._pr_merge_method_field({"method": "rebase"}, _key()), ("REBASE", None))

    def test_an_unknown_method_is_refused(self):
        _, err = routes._pr_merge_method_field({"method": "cherry-pick"}, _key())
        assert err is not None
        self.assertEqual(_body(err)["code"], "invalid_merge_method")

    def test_the_allowlist_comes_from_the_keys_own_client(self):
        # Reaching for github_client directly worked only by coincidence — the two
        # providers' tuples happen to match today.
        self.assertEqual(
            routes._pr_merge_method_field({"method": "merge"}, _key())[0],
            "MERGE",
        )


class TestItemKind(unittest.TestCase):
    def test_absent_and_empty_default_to_issue(self):
        self.assertEqual(routes._item_kind(None), "issue")
        self.assertEqual(routes._item_kind(""), "issue")

    def test_the_known_kinds_pass_through(self):
        self.assertEqual(routes._item_kind("pull"), "pull")
        self.assertEqual(routes._item_kind("issue"), "issue")

    def test_anything_else_is_invalid_rather_than_silently_issue(self):
        # Quietly falling back would resume the WRONG session on GitLab, where
        # issues and merge requests have independent number sequences.
        raws: tuple[object, ...] = ("mr", "PULL", 7, [], True)
        for raw in raws:
            self.assertIsNone(routes._item_kind(raw))


class TestValidHex6(unittest.TestCase):
    def test_accepts_either_case(self):
        self.assertTrue(routes._valid_hex6("ee0000"))
        self.assertTrue(routes._valid_hex6("EE00Ff"))

    def test_rejects_wrong_length_or_non_hex(self):
        for raw in ("", "fff", "ee00000", "#ee0000", "gg0000"):
            self.assertFalse(routes._valid_hex6(raw))


class TestShortRationale(unittest.TestCase):
    def test_a_parenthetical_citation_is_removed_as_one_unit(self):
        # Matching the refs individually left the opening fragment behind.
        self.assertEqual(routes._short_rationale("crashes (see #12, #34)"), "crashes")

    def test_bare_references_outside_brackets_are_stripped(self):
        self.assertEqual(routes._short_rationale("flaky on Windows #91"), "flaky on Windows")

    def test_only_the_first_sentence_survives(self):
        self.assertEqual(
            routes._short_rationale("Startup path is slow. It also logs too much."),
            "Startup path is slow",
        )

    def test_a_decimal_does_not_end_the_sentence(self):
        self.assertEqual(routes._short_rationale("needs 3.11 or newer"), "needs 3.11 or newer")

    def test_the_result_is_bounded(self):
        self.assertLessEqual(
            len(routes._short_rationale("word " * 200)), routes._RATIONALE_MAX_CHARS
        )

    def test_non_strings_do_not_raise(self):
        self.assertEqual(routes._short_rationale(None), "")


# ── write-permission gate ────────────────────────────────────────────────────


class TestHasWriteAccess(unittest.TestCase):
    def test_triage_is_the_floor(self):
        for role in ("triage", "push", "maintain", "admin"):
            self.assertTrue(routes._has_write_access({role: True}), role)

    def test_read_only_and_malformed_objects_are_denied(self):
        self.assertFalse(routes._has_write_access({"pull": True}))
        self.assertFalse(routes._has_write_access({}))
        self.assertFalse(routes._has_write_access(None))


class TestRepoCanWrite(unittest.TestCase):
    def test_stored_permissions_answer_without_a_provider_call(self):
        with mock.patch.object(
            store, "find_connected_repo", return_value={"permissions": {"push": True}}
        ), mock.patch.object(gh, "get_repo_permissions") as fetch:
            self.assertTrue(routes._repo_can_write(_key()))
        fetch.assert_not_called()

    def test_a_legacy_entry_without_permissions_self_heals(self):
        with mock.patch.object(store, "find_connected_repo", return_value={}), \
                mock.patch.object(gh, "get_repo_permissions", return_value={"triage": True}), \
                mock.patch.object(store, "set_repo_permissions") as heal:
            self.assertTrue(routes._repo_can_write(_key()))
        heal.assert_called_once()

    def test_a_provider_failure_is_none_which_callers_treat_as_denied(self):
        # Fail-closed: a transient permissions-read failure shows the repo as
        # read-only rather than allowing an unauthorized write.
        with mock.patch.object(store, "find_connected_repo", return_value=None), \
                mock.patch.object(gh, "get_repo_permissions", side_effect=gh.GhCliError("boom")):
            self.assertIsNone(routes._repo_can_write(_key()))


# ── shared error mapper + preamble ───────────────────────────────────────────


class TestPrActionError(unittest.TestCase):
    def test_a_permission_error_is_403(self):
        res = routes._pr_action_error("pull_merge", "o/r#1", gh.GhPermissionError("nope"))
        self.assertEqual(res.status, 403)
        self.assertEqual(_body(res)["code"], "provider_forbidden")

    def test_anything_else_upstream_is_502(self):
        res = routes._pr_action_error("pull_merge", "o/r#1", gh.GhCliError("timeout"))
        self.assertEqual(res.status, 502)
        self.assertEqual(_body(res)["code"], "provider_error")


class TestPrActionPreamble(unittest.IsolatedAsyncioTestCase):
    """The checks EVERY mutating pull-request route delegates to."""

    async def _call(self, body: object):
        return await routes._pr_action_preamble(_json_request("POST", "pull/state", body), "op")

    async def test_a_malformed_payload_is_400(self):
        _, _, early = await self._call(None)
        assert early is not None
        self.assertEqual(early.status, 400)
        self.assertEqual(_body(early)["code"], "invalid_json")

    async def test_a_non_object_payload_is_400(self):
        _, _, early = await self._call([1, 2])
        assert early is not None
        self.assertEqual(_body(early)["code"], "invalid_json")

    async def test_a_missing_repo_is_400_before_the_connected_gate(self):
        with mock.patch.object(store, "is_repo_connected") as gate:
            _, _, early = await self._call({"number": 1})
        assert early is not None
        self.assertEqual(_body(early)["code"], "missing_repo")
        gate.assert_not_called()

    async def test_an_unconnected_repo_is_404_before_the_permission_gate(self):
        with _connected(False), mock.patch.object(routes, "_repo_can_write") as perms:
            _, _, early = await self._call({"owner": "o", "repo": "r"})
        assert early is not None
        self.assertEqual(early.status, 404)
        self.assertEqual(_body(early)["code"], "repo_not_connected")
        perms.assert_not_called()

    async def test_a_read_only_repo_is_403(self):
        with _connected(), _writable(False):
            _, _, early = await self._call({"owner": "o", "repo": "r"})
        assert early is not None
        self.assertEqual(early.status, 403)
        self.assertEqual(_body(early)["code"], "repo_read_only")

    async def test_an_unknown_permission_state_is_also_403(self):
        # ``None`` means "could not tell", and ``is not True`` must deny it.
        with _connected(), _writable(None):
            _, _, early = await self._call({"owner": "o", "repo": "r"})
        assert early is not None
        self.assertEqual(early.status, 403)

    async def test_a_writable_connected_repo_passes_the_body_through(self):
        with _connected(), _writable(True):
            body, key, early = await self._call({"owner": "o", "repo": "r", "number": 4})
        self.assertIsNone(early)
        self.assertEqual(body["number"], 4)
        self.assertEqual(key.slug, "o/r")


class TestRefuseIfHeadMoved(unittest.IsolatedAsyncioTestCase):
    """The stale-verdict check neither provider will make for us."""

    async def _call(self, detail: dict, reviewed: str = SHA):
        with mock.patch.object(gh, "get_pr_detail", return_value=detail):
            return await routes._refuse_if_head_moved(_key(), 7, reviewed, "pull_review")

    async def test_a_matching_head_is_not_a_conflict(self):
        self.assertIsNone(await self._call({"head_sha": SHA}))

    async def test_the_comparison_is_case_insensitive(self):
        self.assertIsNone(await self._call({"head_sha": SHA.upper()}))

    async def test_an_unreported_head_is_not_turned_into_a_refusal(self):
        # "unknown" must not block every review on a provider that omits it.
        self.assertIsNone(await self._call({}))

    async def test_a_moved_head_is_409(self):
        res = await self._call({"head_sha": OTHER_SHA})
        assert res is not None
        self.assertEqual(res.status, 409)
        self.assertEqual(_body(res)["code"], "review_conflict")

    async def test_a_failed_read_keeps_its_own_taxonomy_rather_than_becoming_409(self):
        # "we could not check" and "the head moved" are different answers.
        with mock.patch.object(gh, "get_pr_detail", side_effect=gh.GhCliError("down")):
            res = await routes._refuse_if_head_moved(_key(), 7, SHA, "pull_review")
        assert res is not None
        self.assertEqual(res.status, 502)

    async def test_it_does_not_pay_the_mergeability_retry(self):
        # The default path sleeps ~1.5s and issues a second call to resolve the
        # lazy merge state — once per verdict AND per row of a bulk approve.
        with mock.patch.object(gh, "get_pr_detail", return_value={"head_sha": SHA}) as detail:
            await routes._refuse_if_head_moved(_key(), 7, SHA, "pull_review")
        self.assertFalse(detail.call_args.kwargs["resolve_mergeable"])


# ── enablement gate + route registration ─────────────────────────────────────


class TestRequireEnabled(unittest.IsolatedAsyncioTestCase):
    """Routes are registered once at startup, so a default-disabled app would
    otherwise stay callable."""

    async def test_a_disabled_app_answers_403_without_running_the_handler(self):
        handler = AsyncMock()
        with mock.patch.object(routes, "is_app_enabled", return_value=False):
            res = await routes._require_enabled(handler)(_get("repos"))
        self.assertEqual(res.status, 403)
        handler.assert_not_awaited()

    async def test_an_enabled_app_runs_the_handler(self):
        inner = web.json_response({"ok": True})
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            res = await routes._require_enabled(AsyncMock(return_value=inner))(_get("repos"))
        self.assertIs(res, inner)

    async def test_the_wrapper_keeps_the_handlers_identity(self):
        async def _h(request: web.Request) -> web.Response:
            return web.json_response({})

        self.assertEqual(routes._require_enabled(_h).__name__, "_h")


class TestRegisterRoutes(unittest.TestCase):
    def setUp(self):
        self.app = web.Application()
        routes.register_routes(self.app)
        self.registered = {
            (r.method, r.get_info().get("path"))
            for r in self.app.router.routes()
        }

    def test_every_documented_route_is_registered(self):
        for method, path in (
            ("POST", f"{BASE}/connect"),
            ("GET", f"{BASE}/issues"),
            ("GET", f"{BASE}/issue"),
            ("GET", f"{BASE}/pulls"),
            ("GET", f"{BASE}/pulls/search"),
            ("GET", f"{BASE}/pull"),
            ("GET", f"{BASE}/ref"),
            ("GET", f"{BASE}/labels"),
            ("GET", f"{BASE}/members"),
            ("GET", f"{BASE}/repos"),
            ("DELETE", f"{BASE}/repos"),
            ("GET", f"{BASE}/recent-repos"),
            ("GET", f"{BASE}/me"),
            ("GET", f"{BASE}/settings"),
            ("PUT", f"{BASE}/settings"),
            ("POST", f"{BASE}/settings/role"),
            ("GET", f"{BASE}/issue-ai"),
            ("GET", f"{BASE}/pull-ai"),
            ("POST", f"{BASE}/labels/apply"),
            ("POST", f"{BASE}/labels/apply-bulk"),
            ("POST", f"{BASE}/labels/create"),
            ("POST", f"{BASE}/issue/state"),
            ("POST", f"{BASE}/pull/state"),
            ("POST", f"{BASE}/pull/review"),
            ("POST", f"{BASE}/pull/comment"),
            ("POST", f"{BASE}/pull/merge"),
            ("POST", f"{BASE}/pull/auto-merge"),
            ("GET", f"{BASE}/pull/runs"),
            ("POST", f"{BASE}/pull/run"),
            ("POST", f"{BASE}/pulls/bulk"),
            ("GET", f"{BASE}/investigation"),
            ("PUT", f"{BASE}/investigation"),
            ("GET", f"{BASE}/recommendations"),
            ("POST", f"{BASE}/recommendations"),
            ("GET", f"{BASE}/tagging"),
            ("POST", f"{BASE}/tagging"),
        ):
            self.assertIn((method, path), self.registered, f"{method} {path}")

    def test_there_is_deliberately_no_bulk_merge(self):
        self.assertNotIn(("POST", f"{BASE}/pulls/merge"), self.registered)
        self.assertNotIn("merge", routes._BULK_PR_ACTIONS)

    def test_the_watcher_lifecycle_hooks_are_registered(self):
        from kiro_crew.apps.builtins.issue_radar.backend import watch

        self.assertIn(watch.start_watcher, self.app.on_startup)
        self.assertIn(watch.stop_watcher, self.app.on_cleanup)

    def test_a_hook_append_failure_cannot_break_gateway_startup(self):
        app = web.Application()
        with mock.patch.object(
            type(app.on_startup), "append", side_effect=RuntimeError("frozen")
        ):
            routes.register_routes(app)  # must not raise


# ── /connect ─────────────────────────────────────────────────────────────────


class TestConnect(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body: object):
        return await routes._handle_connect(_json_request("POST", "connect", body))

    async def test_a_malformed_payload_is_400(self):
        res = await self._call(None)
        self.assertEqual(res.status, 400)

    async def test_a_non_object_payload_is_400(self):
        self.assertEqual((await self._call(["x"])).status, 400)

    async def test_a_missing_url_is_400_before_any_parse(self):
        with mock.patch.object(routes.provider, "parse_repo_url") as parse:
            res = await self._call({"url": "   "})
        self.assertEqual(res.status, 400)
        parse.assert_not_called()

    async def test_an_unsupported_url_is_400(self):
        with mock.patch.object(
            routes.provider, "parse_repo_url", side_effect=gh.RepoUrlError("not a repo URL")
        ):
            res = await self._call({"url": "https://example.com/x"})
        self.assertEqual(res.status, 400)
        self.assertIn("not a repo URL", _body(res)["error"])

    async def test_an_upstream_failure_is_502_not_400(self):
        with mock.patch.object(routes.provider, "parse_repo_url", return_value=_key()), \
                mock.patch.object(gh, "verify_repo_access", side_effect=gh.GhCliError("no auth")), \
                mock.patch.object(store, "add_connected_repo") as add:
            res = await self._call({"url": "https://github.com/o/r"})
        self.assertEqual(res.status, 502)
        add.assert_not_called()

    async def test_a_verified_repo_is_persisted_with_its_permissions(self):
        summary = {
            "full_name": "o/r", "private": True, "open_issues_count": 12,
            "permissions": {"push": True},
        }
        with mock.patch.object(routes.provider, "parse_repo_url", return_value=_key()), \
                mock.patch.object(gh, "verify_repo_access", return_value=summary), \
                mock.patch.object(store, "add_connected_repo") as add:
            res = await self._call({"url": "https://github.com/o/r"})
        self.assertEqual(res.status, 200)
        body = _body(res)
        self.assertEqual(body["full_name"], "o/r")
        self.assertTrue(body["private"])
        self.assertEqual(body["open_issues_count"], 12)
        self.assertEqual(add.call_args.kwargs["permissions"], {"push": True})

    async def test_missing_summary_fields_fall_back_rather_than_raising(self):
        with mock.patch.object(routes.provider, "parse_repo_url", return_value=_key()), \
                mock.patch.object(gh, "verify_repo_access", return_value={}), \
                mock.patch.object(store, "add_connected_repo"):
            body = _body(await self._call({"url": "https://github.com/o/r"}))
        self.assertEqual(body["full_name"], "o/r")
        self.assertFalse(body["private"])
        self.assertEqual(body["open_issues_count"], 0)


# ── /labels, /members, /repos, /me ───────────────────────────────────────────


class TestLabelsRoute(unittest.IsolatedAsyncioTestCase):
    async def _call(self, query: dict):
        return await routes._handle_labels(_get("labels", query))

    async def test_missing_owner_or_repo_is_400(self):
        self.assertEqual((await self._call({"owner": "o"})).status, 400)
        self.assertEqual((await self._call({"repo": "r"})).status, 400)

    async def test_an_unconnected_repo_is_404_before_any_cache_read(self):
        with _connected(False), mock.patch.object(store, "read_labels_cache") as read:
            res = await self._call({"owner": "o", "repo": "r"})
        self.assertEqual(res.status, 404)
        read.assert_not_called()

    async def test_a_cache_hit_is_served_without_a_provider_call(self):
        labels = [{"name": "bug", "color": "ee0000"}]
        with _connected(), mock.patch.object(store, "read_labels_cache", return_value=labels), \
                mock.patch.object(gh, "list_repo_labels") as fetch:
            res = await self._call({"owner": "o", "repo": "r"})
        self.assertEqual(_body(res)["labels"], labels)
        self.assertTrue(_body(res)["from_cache"])
        fetch.assert_not_called()

    async def test_refresh_bypasses_the_cache(self):
        with _connected(), mock.patch.object(store, "read_labels_cache") as read, \
                mock.patch.object(store, "refresh_labels_cache", return_value=[]):
            res = await self._call({"owner": "o", "repo": "r", "refresh": "1"})
        self.assertEqual(res.status, 200)
        self.assertFalse(_body(res)["from_cache"])
        read.assert_not_called()

    async def test_a_provider_failure_is_502(self):
        with _connected(), mock.patch.object(store, "read_labels_cache", return_value=None), \
                mock.patch.object(store, "refresh_labels_cache", side_effect=gh.GhCliError("x")):
            self.assertEqual((await self._call({"owner": "o", "repo": "r"})).status, 502)

    async def test_the_store_call_is_scoped_to_the_keys_data_root(self):
        # A per-repo store call without ``root=`` silently reads the GitHub tree
        # for a GitLab project.
        with _connected(), mock.patch.object(store, "read_labels_cache", return_value=[]) as read:
            await self._call({"owner": "o", "repo": "r"})
        self.assertIn("root", read.call_args.kwargs)


class TestLoadMembers(unittest.TestCase):
    def test_the_collaborators_roster_is_preferred_and_sorted(self):
        collaborators = [
            {"login": "Zoe", "role_name": "admin"},
            {"login": "alice", "role_name": "triage"},
            {"login": ""},  # dropped: no login
        ]
        with mock.patch.object(gh, "list_repo_collaborators", return_value=collaborators), \
                mock.patch.object(store, "write_members_cache") as write:
            members, source = routes._load_members(_key())
        self.assertEqual(source, "collaborators")
        self.assertEqual([m["login"] for m in members], ["alice", "Zoe"])
        self.assertEqual(write.call_args.kwargs["source"], "collaborators")

    def test_a_missing_role_falls_back_to_member(self):
        with mock.patch.object(gh, "list_repo_collaborators", return_value=[{"login": "a"}]), \
                mock.patch.object(store, "write_members_cache"):
            members, _ = routes._load_members(_key())
        self.assertEqual(members[0]["role"], "member")

    def test_a_read_only_repo_degrades_to_the_issue_derived_set(self):
        with mock.patch.object(
            gh, "list_repo_collaborators", side_effect=gh.GhPermissionError("403")
        ), mock.patch.object(store, "read_issues_cache", return_value=None), \
                mock.patch.object(
                    gh, "derive_members", return_value=[{"login": "bob", "association": "MEMBER"}]
                ), mock.patch.object(store, "write_members_cache") as write:
            members, source = routes._load_members(_key())
        self.assertEqual(source, "derived")
        self.assertEqual(members, [{"login": "bob", "role": "MEMBER"}])
        self.assertEqual(write.call_args.kwargs["source"], "derived")

    def test_a_non_permission_failure_propagates_instead_of_degrading(self):
        with mock.patch.object(gh, "list_repo_collaborators", side_effect=gh.GhCliError("net")), \
                mock.patch.object(store, "write_members_cache") as write:
            with self.assertRaises(gh.GhCliError):
                routes._load_members(_key())
        write.assert_not_called()


class TestMembersRoute(unittest.IsolatedAsyncioTestCase):
    async def _call(self, query: dict):
        return await routes._handle_members(_get("members", query))

    async def test_missing_params_is_400(self):
        self.assertEqual((await self._call({})).status, 400)

    async def test_an_unconnected_repo_is_404(self):
        with _connected(False):
            self.assertEqual((await self._call({"owner": "o", "repo": "r"})).status, 404)

    async def test_a_cache_hit_carries_its_source_marker(self):
        cached = {"members": [{"login": "a", "role": "admin"}], "source": "derived"}
        with _connected(), mock.patch.object(store, "read_members_cache", return_value=cached), \
                mock.patch.object(routes, "_load_members") as load:
            body = _body(await self._call({"owner": "o", "repo": "r"}))
        self.assertEqual(body["source"], "derived")
        self.assertTrue(body["from_cache"])
        load.assert_not_called()

    async def test_a_cache_miss_loads_the_roster(self):
        with _connected(), mock.patch.object(store, "read_members_cache", return_value=None), \
                mock.patch.object(routes, "_load_members", return_value=([], "collaborators")):
            body = _body(await self._call({"owner": "o", "repo": "r"}))
        self.assertEqual(body["source"], "collaborators")
        self.assertFalse(body["from_cache"])

    async def test_a_provider_failure_is_502(self):
        with _connected(), mock.patch.object(store, "read_members_cache", return_value=None), \
                mock.patch.object(routes, "_load_members", side_effect=gh.GhCliError("net")):
            self.assertEqual((await self._call({"owner": "o", "repo": "r"})).status, 502)


class TestReposRoute(unittest.IsolatedAsyncioTestCase):
    async def _call(self):
        return await routes._handle_repos(_get("repos"))

    async def test_rows_with_permissions_are_returned_untouched(self):
        rows = [{"owner": "o", "repo": "r", "permissions": {"push": True}}]
        with mock.patch.object(store, "list_connected_repos", return_value=rows), \
                mock.patch.object(gh, "verify_repo_access") as verify:
            body = _body(await self._call())
        self.assertEqual(body["repos"], rows)
        verify.assert_not_called()

    async def test_a_legacy_row_self_heals_its_permissions(self):
        rows = [{"owner": "o", "repo": "r"}]
        with mock.patch.object(store, "list_connected_repos", return_value=rows), \
                mock.patch.object(
                    gh, "verify_repo_access", return_value={"permissions": {"triage": True}}
                ), mock.patch.object(store, "set_repo_permissions") as heal:
            body = _body(await self._call())
        self.assertEqual(body["repos"][0]["permissions"], {"triage": True})
        heal.assert_called_once()

    async def test_one_unreadable_repo_does_not_fail_the_batch(self):
        rows = [{"owner": "o", "repo": "bad"}, {"owner": "o", "repo": "good"}]

        def _verify(owner, repo, **kwargs):
            if repo == "bad":
                raise gh.GhCliError("gone")
            return {"permissions": {"push": True}}

        with mock.patch.object(store, "list_connected_repos", return_value=rows), \
                mock.patch.object(gh, "verify_repo_access", side_effect=_verify), \
                mock.patch.object(store, "set_repo_permissions"):
            body = _body(await self._call())
        healed = {r["repo"]: r.get("permissions") for r in body["repos"]}
        self.assertIsNone(healed["bad"])
        self.assertEqual(healed["good"], {"push": True})


class TestMeRoute(unittest.IsolatedAsyncioTestCase):
    async def test_it_returns_the_login_with_its_provider_scope(self):
        with mock.patch.object(gh, "get_current_login", return_value="octocat"):
            body = _body(await routes._handle_me(_get("me")))
        self.assertEqual(body["login"], "octocat")
        self.assertEqual(body["provider"], "github")
        self.assertEqual(body["host"], "github.com")

    async def test_an_unresolvable_login_is_null_rather_than_an_error(self):
        # The UI just hides the "requested/assigned to me" filters.
        with mock.patch.object(gh, "get_current_login", side_effect=gh.GhCliError("no auth")):
            res = await routes._handle_me(_get("me"))
        self.assertEqual(res.status, 200)
        self.assertIsNone(_body(res)["login"])


# ── /settings and /repos DELETE ──────────────────────────────────────────────


class TestGetSettings(unittest.IsolatedAsyncioTestCase):
    async def _call(self, query: dict):
        return await routes._handle_get_settings(_get("settings", query))

    async def test_missing_params_is_400(self):
        self.assertEqual((await self._call({"owner": "o"})).status, 400)

    async def test_an_unconnected_repo_is_404(self):
        with _connected(False):
            self.assertEqual((await self._call({"owner": "o", "repo": "r"})).status, 404)

    async def test_a_connected_repo_gets_its_settings(self):
        with _connected(), mock.patch.object(
            store, "read_repo_settings", return_value={"revision": 3}
        ):
            body = _body(await self._call({"owner": "o", "repo": "r"}))
        self.assertEqual(body["settings"], {"revision": 3})


class TestPutSettings(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body: object):
        return await routes._handle_put_settings(_json_request("PUT", "settings", body))

    async def test_a_malformed_or_non_object_payload_is_400(self):
        self.assertEqual((await self._call(None)).status, 400)
        self.assertEqual((await self._call([1])).status, 400)

    async def test_a_missing_repo_is_400(self):
        self.assertEqual((await self._call({"settings": {"revision": 0}})).status, 400)

    async def test_a_non_object_settings_field_is_400(self):
        res = await self._call({"owner": "o", "repo": "r", "settings": "nope"})
        self.assertEqual(res.status, 400)

    async def test_the_revision_is_mandatory_not_optional(self):
        # A missing revision is indistinguishable from a stale client that never
        # sent one, and that path could erase newer settings.
        for settings in ({}, {"revision": "3"}, {"revision": True}, {"revision": -1}):
            res = await self._call({"owner": "o", "repo": "r", "settings": settings})
            self.assertEqual(res.status, 400)
            self.assertIn("revision", _body(res)["error"])

    async def test_a_stale_write_is_refused_with_the_current_document(self):
        conflict = store.SettingsConflict({"revision": 9})
        with mock.patch.object(store, "write_repo_settings", side_effect=conflict):
            res = await self._call({"owner": "o", "repo": "r", "settings": {"revision": 3}})
        self.assertEqual(res.status, 409)
        self.assertEqual(_body(res)["settings"], {"revision": 9})

    async def test_an_unconnected_repo_is_404(self):
        with mock.patch.object(store, "write_repo_settings", side_effect=KeyError("o/r")):
            res = await self._call({"owner": "o", "repo": "r", "settings": {"revision": 0}})
        self.assertEqual(res.status, 404)

    async def test_a_valid_write_returns_the_saved_document(self):
        with mock.patch.object(
            store, "write_repo_settings", return_value={"revision": 4}
        ) as write:
            res = await self._call({"owner": "o", "repo": "r", "settings": {"revision": 3}})
        self.assertEqual(res.status, 200)
        self.assertEqual(_body(res)["settings"], {"revision": 4})
        self.assertEqual(write.call_args.kwargs["expected_revision"], 3)


class TestDisconnect(unittest.IsolatedAsyncioTestCase):
    async def _call(self, query: dict):
        req = make_mocked_request(
            "DELETE", f"{BASE}/repos?{urlencode(query)}" if query else f"{BASE}/repos"
        )
        req.app["state"] = NoConfiguredOwner()
        req["user"] = "local-app"
        req["app"] = ""
        return await routes._handle_disconnect(req)

    async def test_missing_params_is_400(self):
        self.assertEqual((await self._call({})).status, 400)

    async def test_disconnecting_a_repo_that_is_not_connected_is_404(self):
        with mock.patch.object(store, "remove_connected_repo", return_value=False):
            self.assertEqual((await self._call({"owner": "o", "repo": "r"})).status, 404)

    async def test_a_successful_disconnect_is_local_only(self):
        with mock.patch.object(store, "remove_connected_repo", return_value=True):
            body = _body(await self._call({"owner": "o", "repo": "r"}))
        self.assertTrue(body["ok"])


# ── item detail routes ───────────────────────────────────────────────────────


class TestIssueDetail(unittest.IsolatedAsyncioTestCase):
    async def _call(self, query: dict):
        return await routes._handle_issue_detail(_get("issue", query))

    async def test_missing_params_is_400(self):
        for query in ({"owner": "o", "repo": "r"}, {"owner": "o", "number": "1"}):
            self.assertEqual((await self._call(query)).status, 400)

    async def test_a_bad_number_is_400_before_the_connected_gate(self):
        with mock.patch.object(store, "is_repo_connected") as gate:
            res = await self._call({"owner": "o", "repo": "r", "number": "x"})
        self.assertEqual(res.status, 400)
        gate.assert_not_called()

    async def test_an_unconnected_repo_is_404(self):
        with _connected(False):
            res = await self._call({"owner": "o", "repo": "r", "number": "5"})
        self.assertEqual(res.status, 404)

    async def test_a_cache_hit_skips_both_provider_calls(self):
        cached = {"detail": {"number": 5}, "timeline": [{"kind": "comment"}]}
        with _connected(), mock.patch.object(store, "read_issue_detail_cache", return_value=cached), \
                mock.patch.object(gh, "get_issue_detail") as detail:
            body = _body(await self._call({"owner": "o", "repo": "r", "number": "5"}))
        self.assertTrue(body["from_cache"])
        self.assertEqual(body["timeline"], cached["timeline"])
        detail.assert_not_called()

    async def test_a_cache_entry_with_no_detail_is_treated_as_a_miss(self):
        with _connected(), \
                mock.patch.object(store, "read_issue_detail_cache", return_value={"detail": None}), \
                mock.patch.object(gh, "get_issue_detail", return_value={"number": 5}), \
                mock.patch.object(gh, "list_issue_timeline", return_value=[]), \
                mock.patch.object(store, "write_issue_detail_cache") as write:
            body = _body(await self._call({"owner": "o", "repo": "r", "number": "5"}))
        self.assertFalse(body["from_cache"])
        write.assert_called_once()

    async def test_a_provider_failure_is_502_and_writes_nothing(self):
        with _connected(), mock.patch.object(store, "read_issue_detail_cache", return_value=None), \
                mock.patch.object(gh, "get_issue_detail", side_effect=gh.GhCliError("net")), \
                mock.patch.object(store, "write_issue_detail_cache") as write:
            res = await self._call({"owner": "o", "repo": "r", "number": "5"})
        self.assertEqual(res.status, 502)
        write.assert_not_called()


class TestPullDetail(unittest.IsolatedAsyncioTestCase):
    async def _call(self, query: dict):
        return await routes._handle_pull_detail(_get("pull", query))

    async def test_missing_params_is_400(self):
        self.assertEqual((await self._call({"owner": "o", "repo": "r"})).status, 400)

    async def test_an_unconnected_repo_is_404(self):
        with _connected(False):
            self.assertEqual(
                (await self._call({"owner": "o", "repo": "r", "number": "7"})).status, 404
            )

    async def test_a_cache_hit_recomputes_the_check_summary_for_the_card(self):
        cached = {"detail": {"number": 7}, "timeline": [], "checks": [{"name": "ci"}]}
        with _connected(), mock.patch.object(store, "read_pr_detail_cache", return_value=cached), \
                mock.patch.object(gh, "summarize_checks", return_value={"total": 1}) as summarize:
            body = _body(await self._call({"owner": "o", "repo": "r", "number": "7"}))
        self.assertTrue(body["from_cache"])
        self.assertEqual(body["checks_summary"], {"total": 1})
        summarize.assert_called_once_with(cached["checks"])

    async def test_the_cache_read_is_bounded_by_the_routes_own_ttl(self):
        # Freshness is the route's property, not something each caller has to ask
        # for with refresh=1.
        with _connected(), mock.patch.object(store, "read_pr_detail_cache") as read:
            read.return_value = {"detail": {"number": 7}}
            with mock.patch.object(gh, "summarize_checks", return_value={}):
                await self._call({"owner": "o", "repo": "r", "number": "7"})
        self.assertEqual(
            read.call_args.kwargs.get("max_age_sec"), store.PR_DETAIL_CACHE_TTL_SEC
        )

    async def test_a_cold_read_fetches_checks_and_patches_the_list_row(self):
        with _connected(), mock.patch.object(store, "read_pr_detail_cache", return_value=None), \
                mock.patch.object(gh, "get_pr_detail", return_value={"head_sha": SHA}), \
                mock.patch.object(gh, "list_pr_timeline", return_value=[]), \
                mock.patch.object(gh, "list_pr_checks", return_value=[{"name": "ci"}]) as checks, \
                mock.patch.object(gh, "summarize_checks", return_value={"total": 1}), \
                mock.patch.object(store, "write_pr_detail_cache"), \
                mock.patch.object(store, "apply_pr_checks_to_list_cache") as patch_row:
            body = _body(await self._call({"owner": "o", "repo": "r", "number": "7"}))
        self.assertFalse(body["from_cache"])
        checks.assert_called_once()
        patch_row.assert_called_once()

    async def test_a_pull_request_with_no_head_sha_simply_has_no_checks(self):
        # A deleted fork branch leaves no head commit to hang checks off.
        with _connected(), mock.patch.object(store, "read_pr_detail_cache", return_value=None), \
                mock.patch.object(gh, "get_pr_detail", return_value={"head_sha": None}), \
                mock.patch.object(gh, "list_pr_timeline", return_value=[]), \
                mock.patch.object(gh, "list_pr_checks") as checks, \
                mock.patch.object(gh, "summarize_checks", return_value={}), \
                mock.patch.object(store, "write_pr_detail_cache"), \
                mock.patch.object(store, "apply_pr_checks_to_list_cache"):
            body = _body(await self._call({"owner": "o", "repo": "r", "number": "7"}))
        self.assertEqual(body["checks"], [])
        checks.assert_not_called()

    async def test_a_provider_failure_is_502(self):
        with _connected(), mock.patch.object(store, "read_pr_detail_cache", return_value=None), \
                mock.patch.object(gh, "get_pr_detail", side_effect=gh.GhCliError("net")), \
                mock.patch.object(gh, "list_pr_timeline", return_value=[]):
            res = await self._call({"owner": "o", "repo": "r", "number": "7"})
        self.assertEqual(res.status, 502)


class TestPullsSearch(unittest.IsolatedAsyncioTestCase):
    async def _call(self, query: dict):
        return await routes._handle_pulls_search(_get("pulls/search", query))

    async def test_missing_params_is_400(self):
        self.assertEqual((await self._call({"owner": "o"})).status, 400)

    async def test_an_unconnected_repo_is_404_before_the_search(self):
        with _connected(False), mock.patch.object(gh, "search_pulls") as search:
            res = await self._call({"owner": "o", "repo": "r", "author": "me"})
        self.assertEqual(res.status, 404)
        search.assert_not_called()

    async def test_a_bad_state_or_missing_person_is_a_400_client_error(self):
        with _connected(), mock.patch.object(
            gh, "search_pulls", side_effect=gh.PrSearchError("no person qualifier")
        ):
            res = await self._call({"owner": "o", "repo": "r"})
        self.assertEqual(res.status, 400)

    async def test_an_upstream_failure_is_502(self):
        with _connected(), mock.patch.object(gh, "search_pulls", side_effect=gh.GhCliError("net")):
            res = await self._call({"owner": "o", "repo": "r", "author": "me"})
        self.assertEqual(res.status, 502)

    async def test_it_asks_for_one_more_than_the_cap_to_answer_truncated_by_fact(self):
        rows = [{"number": n} for n in range(gh.PR_SEARCH_MAX + 1)]
        with _connected(), mock.patch.object(gh, "search_pulls", return_value=rows) as search, \
                mock.patch.object(gh, "enrich_pulls_by_number", side_effect=lambda o, r, p: p):
            body = _body(await self._call({"owner": "o", "repo": "r", "author": "me"}))
        self.assertEqual(search.call_args.kwargs["limit"], gh.PR_SEARCH_MAX + 1)
        self.assertTrue(body["truncated"])
        self.assertEqual(len(body["pulls"]), gh.PR_SEARCH_MAX)

    async def test_exactly_the_cap_is_not_labelled_truncated(self):
        rows = [{"number": n} for n in range(gh.PR_SEARCH_MAX)]
        with _connected(), mock.patch.object(gh, "search_pulls", return_value=rows), \
                mock.patch.object(gh, "enrich_pulls_by_number", side_effect=lambda o, r, p: p):
            body = _body(await self._call({"owner": "o", "repo": "r", "author": "me"}))
        self.assertFalse(body["truncated"])

    async def test_the_person_filters_are_forwarded_and_blanks_become_none(self):
        with _connected(), mock.patch.object(gh, "search_pulls", return_value=[]) as search, \
                mock.patch.object(gh, "enrich_pulls_by_number", return_value=[]):
            await self._call({
                "owner": "o", "repo": "r", "state": "MERGED", "author": " alice ",
                "assignee": "  ", "review_requested": "bob",
            })
        kwargs = search.call_args.kwargs
        self.assertEqual(kwargs["state"], "merged")
        self.assertEqual(kwargs["author"], "alice")
        self.assertIsNone(kwargs["assignee"])
        self.assertEqual(kwargs["review_requested"], "bob")

    async def test_search_rows_are_enriched_by_number_not_by_state(self):
        # A search hit can rank outside the recently-updated window, so the cards
        # would lose their diff-size + check row.
        with _connected(), mock.patch.object(gh, "search_pulls", return_value=[{"number": 3}]), \
                mock.patch.object(
                    gh, "enrich_pulls_by_number", return_value=[{"number": 3, "additions": 1}]
                ) as enrich:
            body = _body(await self._call({"owner": "o", "repo": "r", "author": "me"}))
        enrich.assert_called_once()
        self.assertEqual(body["pulls"][0]["additions"], 1)
        self.assertEqual(body["bulk_max"], routes._BULK_PR_MAX)


# ── investigation record read ────────────────────────────────────────────────


class TestGetInvestigation(unittest.IsolatedAsyncioTestCase):
    async def _call(self, query: dict):
        return await routes._handle_get_investigation(_get("investigation", query))

    async def test_missing_params_is_400(self):
        self.assertEqual((await self._call({"owner": "o", "repo": "r"})).status, 400)

    async def test_a_bad_number_is_400(self):
        self.assertEqual(
            (await self._call({"owner": "o", "repo": "r", "number": "x"})).status, 400
        )

    async def test_an_unconnected_repo_is_404(self):
        with _connected(False):
            res = await self._call({"owner": "o", "repo": "r", "number": "5"})
        self.assertEqual(res.status, 404)

    async def test_an_unknown_kind_is_400_rather_than_silently_issue(self):
        with _connected():
            res = await self._call({"owner": "o", "repo": "r", "number": "5", "kind": "mr"})
        self.assertEqual(res.status, 400)

    async def test_a_never_investigated_item_reads_as_null(self):
        with _connected(), mock.patch.object(store, "read_investigation", return_value=None):
            body = _body(await self._call({"owner": "o", "repo": "r", "number": "5"}))
        self.assertIsNone(body["investigation"])
        self.assertEqual(body["kind"], "issue")

    async def test_the_record_is_namespaced_by_kind(self):
        with _connected(), mock.patch.object(
            store, "read_investigation", return_value={"status": "done"}
        ) as read:
            body = _body(await self._call(
                {"owner": "o", "repo": "r", "number": "5", "kind": "pull"}
            ))
        self.assertEqual(body["kind"], "pull")
        # GitHub draws issues and PRs from ONE sequence, so the historical
        # namespace is correct there and nothing needs migrating.
        self.assertEqual(read.call_args.kwargs["kind"], "issue")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
