"""Coverage tests for Issue Radar's pull-request action routes.

This file pins ``_run_pr_action``, the one place an action name becomes a provider call, and
the pull-request handlers: state, review, comment, auto-merge, merge, the workflow-runs
read, the run action, and the bulk route with its fixed action allowlist, per-row head pins
and per-row failures. It pins the merge route's own readiness check, head pin and refusal
taxonomy; that every mutating pull-request handler answers 404 for an unconnected
repository, 403 for a read-only one and 400 for a malformed payload, and that each
per-pull-request one bounds the number; and the remaining per-route checks, from the merge
method validated before the head read to a rejected review mapped to 502. The shared
preamble, error mapper, stale-head check and field parsers these handlers call live in
``test_issue_radar_routes_coverage.py``; the label-creation, settings label-append and
issue-state routes in ``test_issue_radar_routes_coverage_label_and_issue_writes.py``; the
pure model-input shapers in ``test_issue_radar_routes_coverage_model_inputs.py``; and the
shared request builders and patch helpers in ``issue_radar_routes_coverage_helpers.py``.
"""

import unittest
from unittest import mock

from issue_radar_routes_coverage_helpers import (
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

# ── pull-request actions ─────────────────────────────────────────────────────


class TestRunPrAction(unittest.IsolatedAsyncioTestCase):
    """The single place an action NAME becomes a provider call."""

    async def test_close_and_reopen_map_onto_the_pull_state_endpoint(self):
        with (
            mock.patch.object(gh, "set_pr_state", return_value={"state": "closed"}) as call,
            mock.patch.object(store, "apply_pr_state_change_to_caches") as evict,
        ):
            await routes._run_pr_action(_key(), "close", 7)
        self.assertEqual(call.call_args.args[3], "closed")
        evict.assert_called_once()

        with (
            mock.patch.object(gh, "set_pr_state", return_value={"state": "open"}) as call,
            mock.patch.object(store, "apply_pr_state_change_to_caches"),
        ):
            await routes._run_pr_action(_key(), "reopen", 7)
        self.assertEqual(call.call_args.args[3], "open")

    async def test_each_review_verb_carries_its_provider_event_and_the_pin(self):
        for action, event in (
            ("approve", "APPROVE"),
            ("request_changes", "REQUEST_CHANGES"),
            ("comment_review", "COMMENT"),
        ):
            with (
                mock.patch.object(gh, "submit_pr_review", return_value={}) as submit,
                mock.patch.object(store, "drop_pr_detail_cache"),
            ):
                await routes._run_pr_action(_key(), action, 7, body="lgtm", head_sha=SHA)
            self.assertEqual(submit.call_args.args[3], event)
            self.assertEqual(submit.call_args.args[5], SHA)

    async def test_a_conversation_comment_uses_the_pull_specific_function(self):
        # On GitLab issues and merge requests are different collections with
        # independent numbering, so the generic one would comment elsewhere.
        with (
            mock.patch.object(gh, "add_pr_comment", return_value={}) as call,
            mock.patch.object(store, "drop_pr_detail_cache"),
        ):
            await routes._run_pr_action(_key(), "comment", 7, body="hi")
        call.assert_called_once()

    async def test_a_merge_the_provider_declined_is_raised_not_reported_as_success(self):
        # GitLab answers 200 with a non-merged state when its rules said no.
        with (
            mock.patch.object(
                gh, "merge_pull_request", return_value={"merged": False, "message": "rules said no"}
            ),
            mock.patch.object(store, "apply_pr_state_change_to_caches") as evict,
        ):
            with self.assertRaises(gh.GhCliError) as ctx:
                await routes._run_pr_action(_key(), "merge", 7, head_sha=SHA)
        self.assertIn("rules said no", str(ctx.exception))
        evict.assert_not_called()

    async def test_a_real_merge_evicts_the_pull_request_from_the_open_list(self):
        with (
            mock.patch.object(gh, "merge_pull_request", return_value={"merged": True}),
            mock.patch.object(store, "apply_pr_state_change_to_caches") as evict,
        ):
            await routes._run_pr_action(_key(), "merge", 7, head_sha=SHA)
        self.assertEqual(evict.call_args.args[3], "closed")

    async def test_the_auto_merge_and_run_verbs_reach_their_own_client_calls(self):
        for action, target in (
            ("auto_merge", "enable_auto_merge"),
            ("cancel_auto_merge", "disable_auto_merge"),
            ("cancel_run", "cancel_workflow_run"),
            ("rerun_run", "rerun_workflow_run"),
        ):
            with (
                mock.patch.object(gh, target, return_value={"ok": True}) as call,
                mock.patch.object(store, "drop_pr_detail_cache"),
            ):
                await routes._run_pr_action(_key(), action, 7, run_id=42)
            call.assert_called_once()

    async def test_an_unknown_action_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            await routes._run_pr_action(_key(), "delete_repo", 7)


class TestPullState(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body: dict):
        return await routes._handle_pull_state(_json_request("POST", "pull/state", body))

    async def test_an_unknown_state_is_400(self):
        with _connected(), _writable():
            res = await self._call({"owner": "o", "repo": "r", "number": 7, "state": "merged"})
        self.assertEqual(_body(res)["code"], "invalid_state")

    async def test_the_number_bound_is_enforced_on_the_body(self):
        with _connected(), _writable():
            res = await self._call({"owner": "o", "repo": "r", "number": 0, "state": "closed"})
        self.assertEqual(_body(res)["code"], "invalid_number")

    async def test_a_close_reaches_the_provider_and_echoes_the_identity(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "set_pr_state", return_value={"state": "closed"}),
            mock.patch.object(store, "apply_pr_state_change_to_caches"),
        ):
            body = _body(
                await self._call({"owner": "o", "repo": "r", "number": 7, "state": "CLOSED"})
            )
        self.assertEqual(body["number"], 7)
        self.assertEqual(body["state"], "closed")
        self.assertEqual(body["provider"], "github")

    async def test_a_provider_failure_is_mapped_by_the_shared_taxonomy(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "set_pr_state", side_effect=gh.GhPermissionError("locked")),
        ):
            res = await self._call({"owner": "o", "repo": "r", "number": 7, "state": "closed"})
        self.assertEqual(res.status, 403)


class TestPullReview(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body: dict):
        return await routes._handle_pull_review(_json_request("POST", "pull/review", body))

    def _base(self, **extra) -> dict:
        return {"owner": "o", "repo": "r", "number": 7, "head_sha": SHA, **extra}

    async def test_an_unknown_event_is_400(self):
        with _connected(), _writable():
            res = await self._call(self._base(event="merge"))
        self.assertEqual(_body(res)["code"], "invalid_event")

    async def test_the_head_pin_is_required(self):
        with _connected(), _writable():
            res = await self._call({"owner": "o", "repo": "r", "number": 7, "event": "approve"})
        self.assertEqual(_body(res)["code"], "head_sha_required")

    async def test_an_oversized_body_is_refused_before_the_pin_is_read(self):
        with _connected(), _writable():
            res = await self._call(
                self._base(event="comment", body="x" * (routes._PR_BODY_MAX_CHARS + 1))
            )
        self.assertEqual(_body(res)["code"], "body_too_long")

    async def test_a_verdict_on_a_moved_head_is_409(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "get_pr_detail", return_value={"head_sha": OTHER_SHA}),
            mock.patch.object(gh, "submit_pr_review") as submit,
        ):
            res = await self._call(self._base(event="approve"))
        self.assertEqual(res.status, 409)
        submit.assert_not_called()

    async def test_a_plain_comment_review_skips_the_head_check(self):
        # It records no verdict, so it stays valid prose no matter what the head
        # does — refusing it would only cost the user their typing.
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "get_pr_detail") as detail,
            mock.patch.object(gh, "submit_pr_review", return_value={"id": 1}),
            mock.patch.object(store, "drop_pr_detail_cache"),
        ):
            res = await self._call(self._base(event="comment", body="a note"))
        self.assertEqual(res.status, 200)
        detail.assert_not_called()

    async def test_a_verdict_on_the_reviewed_head_is_submitted(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "get_pr_detail", return_value={"head_sha": SHA}),
            mock.patch.object(gh, "submit_pr_review", return_value={"id": 2}) as submit,
            mock.patch.object(store, "drop_pr_detail_cache"),
        ):
            body = _body(await self._call(self._base(event="request_changes", body="no")))
        self.assertEqual(body["id"], 2)
        self.assertEqual(submit.call_args.args[3], "REQUEST_CHANGES")


class TestPullComment(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body: dict):
        return await routes._handle_pull_comment(_json_request("POST", "pull/comment", body))

    async def test_an_empty_body_is_refused(self):
        with _connected(), _writable():
            res = await self._call({"owner": "o", "repo": "r", "number": 7, "body": "  "})
        self.assertEqual(_body(res)["code"], "body_required")

    async def test_a_comment_is_posted_and_the_detail_cache_dropped(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "add_pr_comment", return_value={"id": 9}),
            mock.patch.object(store, "drop_pr_detail_cache") as drop,
        ):
            body = _body(
                await self._call({"owner": "o", "repo": "r", "number": 7, "body": "hello"})
            )
        self.assertEqual(body["id"], 9)
        drop.assert_called_once()

    async def test_a_provider_failure_is_502(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "add_pr_comment", side_effect=gh.GhCliError("net")),
        ):
            res = await self._call({"owner": "o", "repo": "r", "number": 7, "body": "hi"})
        self.assertEqual(res.status, 502)


class TestPullAutoMerge(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body: dict):
        return await routes._handle_pull_auto_merge(_json_request("POST", "pull/auto-merge", body))

    async def test_enabled_must_be_a_boolean(self):
        with _connected(), _writable():
            res = await self._call({"owner": "o", "repo": "r", "number": 7, "enabled": "yes"})
        self.assertEqual(_body(res)["code"], "invalid_enabled")

    async def test_an_unknown_merge_method_is_refused(self):
        with _connected(), _writable():
            res = await self._call(
                {
                    "owner": "o",
                    "repo": "r",
                    "number": 7,
                    "enabled": True,
                    "method": "octopus",
                }
            )
        self.assertEqual(_body(res)["code"], "invalid_merge_method")

    async def test_arming_and_disarming_reach_different_client_calls(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "enable_auto_merge", return_value={"armed": True}) as arm,
            mock.patch.object(store, "drop_pr_detail_cache"),
        ):
            res = await self._call({"owner": "o", "repo": "r", "number": 7, "enabled": True})
        self.assertEqual(res.status, 200)
        arm.assert_called_once()

        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "disable_auto_merge", return_value={"armed": False}) as off,
            mock.patch.object(store, "drop_pr_detail_cache"),
        ):
            await self._call({"owner": "o", "repo": "r", "number": 7, "enabled": False})
        off.assert_called_once()

    async def test_a_provider_refusal_is_relayed_verbatim(self):
        # A repo with no branch rule (or 'Allow auto-merge' off) cannot arm it, and
        # the provider's own text names which.
        with (
            _connected(),
            _writable(),
            mock.patch.object(
                gh, "enable_auto_merge", side_effect=gh.GhCliError("auto-merge is disabled")
            ),
        ):
            res = await self._call({"owner": "o", "repo": "r", "number": 7, "enabled": True})
        self.assertEqual(res.status, 502)
        self.assertIn("auto-merge is disabled", _body(res)["error"])


class TestPullMerge(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body: dict):
        return await routes._handle_pull_merge(_json_request("POST", "pull/merge", body))

    def _base(self, **extra) -> dict:
        return {"owner": "o", "repo": "r", "number": 7, "head_sha": SHA, **extra}

    async def test_the_head_pin_is_required(self):
        with _connected(), _writable():
            res = await self._call({"owner": "o", "repo": "r", "number": 7})
        self.assertEqual(_body(res)["code"], "head_sha_required")

    async def test_an_unsatisfied_merge_state_is_refused_by_the_app_itself(self):
        # The provider 405s an ordinary user but HONOURS an admin holding
        # bypass-branch-protection, so "the provider adjudicates" stops being true
        # exactly for the account that can do the most damage.
        for state in ("blocked", "unstable", "dirty", "behind", "", "can_be_merged"):
            with (
                _connected(),
                _writable(),
                mock.patch.object(
                    gh,
                    "get_pr_detail",
                    return_value={"mergeable_state": state, "head_sha": SHA},
                ),
                mock.patch.object(gh, "merge_pull_request") as merge,
            ):
                res = await self._call(self._base())
            self.assertEqual(res.status, 409, state)
            self.assertEqual(_body(res)["code"], "merge_not_ready")
            merge.assert_not_called()

    async def test_each_satisfied_state_is_allowed(self):
        for state in sorted(routes._MERGE_ALLOWED_STATES):
            with (
                _connected(),
                _writable(),
                mock.patch.object(
                    gh,
                    "get_pr_detail",
                    return_value={"mergeable_state": state, "head_sha": SHA},
                ),
                mock.patch.object(gh, "merge_pull_request", return_value={"merged": True}),
                mock.patch.object(store, "apply_pr_state_change_to_caches"),
            ):
                res = await self._call(self._base())
            self.assertEqual(res.status, 200, state)

    async def test_a_moved_head_is_409_even_when_the_state_is_clean(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(
                gh,
                "get_pr_detail",
                return_value={"mergeable_state": "clean", "head_sha": OTHER_SHA},
            ),
            mock.patch.object(gh, "merge_pull_request") as merge,
        ):
            res = await self._call(self._base())
        self.assertEqual(res.status, 409)
        self.assertEqual(_body(res)["code"], "merge_conflict")
        merge.assert_not_called()

    async def test_a_failed_state_read_is_relayed_rather_than_guessed(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "get_pr_detail", side_effect=gh.GhCliError("net")),
        ):
            res = await self._call(self._base())
        self.assertEqual(res.status, 502)

    async def test_a_405_reads_as_the_repositorys_rules_not_a_broken_request(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(
                gh,
                "get_pr_detail",
                return_value={"mergeable_state": "clean", "head_sha": SHA},
            ),
            mock.patch.object(
                gh, "merge_pull_request", side_effect=gh.GhCliError("HTTP 405 not allowed")
            ),
        ):
            res = await self._call(self._base())
        self.assertEqual(res.status, 409)
        self.assertEqual(_body(res)["code"], "merge_not_allowed")

    async def test_a_409_from_the_provider_is_the_stale_head_message(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(
                gh,
                "get_pr_detail",
                return_value={"mergeable_state": "clean", "head_sha": SHA},
            ),
            mock.patch.object(
                gh, "merge_pull_request", side_effect=gh.GhCliError("HTTP 409 conflict")
            ),
        ):
            res = await self._call(self._base())
        self.assertEqual(_body(res)["code"], "merge_conflict")

    async def test_a_permission_error_is_403(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(
                gh,
                "get_pr_detail",
                return_value={"mergeable_state": "clean", "head_sha": SHA},
            ),
            mock.patch.object(gh, "merge_pull_request", side_effect=gh.GhPermissionError("nope")),
        ):
            res = await self._call(self._base())
        self.assertEqual(res.status, 403)

    async def test_an_async_refusal_keeps_githubs_reason(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(
                gh,
                "get_pr_detail",
                return_value={"mergeable_state": "clean", "head_sha": SHA},
            ),
            mock.patch.object(
                gh,
                "merge_pull_request",
                side_effect=gh.GhMergeRefusedError("Required status check is failing"),
            ),
        ):
            res = await self._call(self._base())
        self.assertEqual(res.status, 409)
        self.assertEqual(_body(res)["code"], "merge_not_allowed")
        self.assertIn("Required status check", _body(res)["error"])

    async def test_a_merge_still_running_is_409_pending_not_success(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(
                gh,
                "get_pr_detail",
                return_value={"mergeable_state": "clean", "head_sha": SHA},
            ),
            mock.patch.object(
                gh,
                "merge_pull_request",
                return_value={"merged": False, "pending": True, "message": "still merging"},
            ),
        ):
            res = await self._call(self._base())
        self.assertEqual(res.status, 409)
        self.assertEqual(_body(res)["code"], "merge_pending")

    async def test_any_other_failure_keeps_the_shared_502(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(
                gh,
                "get_pr_detail",
                return_value={"mergeable_state": "clean", "head_sha": SHA},
            ),
            mock.patch.object(gh, "merge_pull_request", side_effect=gh.GhCliError("timeout")),
        ):
            res = await self._call(self._base())
        self.assertEqual(res.status, 502)


class TestPullRuns(unittest.IsolatedAsyncioTestCase):
    async def _call(self, query: dict):
        return await routes._handle_pull_runs(_get("pull/runs", query))

    async def test_a_missing_sha_is_400(self):
        res = await self._call({"owner": "o", "repo": "r", "number": "7"})
        self.assertEqual(_body(res)["code"], "missing_params")

    async def test_the_echoed_number_is_still_validated(self):
        res = await self._call({"owner": "o", "repo": "r", "sha": SHA, "number": "-3"})
        self.assertEqual(res.status, 400)

    async def test_the_number_is_optional(self):
        with _connected(), mock.patch.object(gh, "list_pr_workflow_runs", return_value=[]):
            body = _body(await self._call({"owner": "o", "repo": "r", "sha": SHA}))
        self.assertEqual(body["number"], 0)

    async def test_an_unconnected_repo_is_404(self):
        with _connected(False):
            res = await self._call({"owner": "o", "repo": "r", "sha": SHA})
        self.assertEqual(_body(res)["code"], "repo_not_connected")

    async def test_the_runs_are_returned_for_the_head_commit(self):
        runs = [{"id": 1, "cancellable": True}]
        with _connected(), mock.patch.object(gh, "list_pr_workflow_runs", return_value=runs):
            body = _body(await self._call({"owner": "o", "repo": "r", "sha": SHA, "number": "7"}))
        self.assertEqual(body["runs"], runs)
        self.assertEqual(body["number"], 7)

    async def test_a_provider_failure_is_502(self):
        with (
            _connected(),
            mock.patch.object(gh, "list_pr_workflow_runs", side_effect=gh.GhCliError("net")),
        ):
            res = await self._call({"owner": "o", "repo": "r", "sha": SHA})
        self.assertEqual(res.status, 502)
        self.assertEqual(_body(res)["code"], "provider_error")

    async def test_the_502_body_withholds_the_raw_cli_diagnostic(self):
        raw = "gh api repos/o/r/actions/runs failed (exit 1): connection refused"
        with (
            _connected(),
            mock.patch.object(gh, "list_pr_workflow_runs", side_effect=gh.GhCliError(raw)),
        ):
            res = await self._call({"owner": "o", "repo": "r", "sha": SHA})
        body = _body(res)
        self.assertEqual(body["error"], "upstream provider error")
        self.assertNotIn(raw, body["error"])
        self.assertNotIn("gh ", body["error"])
        self.assertNotIn("exit ", body["error"])


class TestPullRunAction(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body: dict):
        return await routes._handle_pull_run_action(_json_request("POST", "pull/run", body))

    def _base(self, **extra) -> dict:
        return {"owner": "o", "repo": "r", "number": 7, "run_id": 42, "action": "cancel", **extra}

    async def test_the_run_id_must_be_a_positive_integer(self):
        for value in (0, -1, "42", True, None):
            with _connected(), _writable():
                res = await self._call(self._base(run_id=value))
            self.assertEqual(_body(res)["code"], "invalid_run_id")

    async def test_the_run_id_has_its_own_larger_ceiling(self):
        # A global provider sequence rather than a per-repo one, but still bounded:
        # it reaches a PATH segment in the provider argv.
        with _connected(), _writable():
            res = await self._call(self._base(run_id=routes.MAX_RUN_ID + 1))
        self.assertEqual(_body(res)["code"], "run_id_out_of_range")
        self.assertGreater(routes.MAX_RUN_ID, routes.MAX_ITEM_NUMBER)

    async def test_an_unknown_action_is_400(self):
        with _connected(), _writable():
            res = await self._call(self._base(action="delete"))
        self.assertEqual(_body(res)["code"], "invalid_action")

    async def test_failed_only_must_be_a_boolean(self):
        with _connected(), _writable():
            res = await self._call(self._base(action="rerun", failed_only="1"))
        self.assertEqual(_body(res)["code"], "invalid_failed_only")

    async def test_a_cancel_reaches_the_cancel_call(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "cancel_workflow_run", return_value={"ok": True}) as call,
            mock.patch.object(store, "drop_pr_detail_cache"),
        ):
            res = await self._call(self._base())
        self.assertEqual(res.status, 200)
        self.assertEqual(call.call_args.args[2], 42)

    async def test_a_rerun_forwards_the_failed_only_flag(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "rerun_workflow_run", return_value={"ok": True}) as call,
            mock.patch.object(store, "drop_pr_detail_cache"),
        ):
            await self._call(self._base(action="rerun", failed_only=True))
        self.assertTrue(call.call_args.kwargs["failed_only"])

    async def test_a_provider_failure_is_mapped(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "cancel_workflow_run", side_effect=gh.GhCliError("net")),
        ):
            res = await self._call(self._base())
        self.assertEqual(res.status, 502)


class TestPullsBulk(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body: dict):
        return await routes._handle_pulls_bulk(_json_request("POST", "pulls/bulk", body))

    def _base(self, **extra) -> dict:
        return {"owner": "o", "repo": "r", "numbers": [7, 8], "action": "close", **extra}

    async def test_the_action_allowlist_is_fixed(self):
        # Not a generic fan-out: a future action must not silently become
        # mass-appliable.
        with _connected(), _writable():
            res = await self._call(self._base(action="merge"))
        self.assertEqual(_body(res)["code"], "invalid_action")

    async def test_the_numbers_array_is_validated(self):
        with _connected(), _writable():
            res = await self._call(self._base(numbers=[]))
        self.assertEqual(_body(res)["code"], "numbers_required")

    async def test_a_bulk_comment_requires_a_body(self):
        with _connected(), _writable():
            res = await self._call(self._base(action="comment"))
        self.assertEqual(_body(res)["code"], "body_required")

    async def test_a_pinned_bulk_action_requires_a_sha_for_every_row(self):
        with _connected(), _writable():
            res = await self._call(self._base(action="approve", head_shas={"7": SHA}))
        self.assertEqual(_body(res)["code"], "head_shas_required")

    async def test_the_unpinned_verbs_need_no_shas(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "set_pr_state", return_value={"state": "closed"}),
            mock.patch.object(store, "apply_pr_state_change_to_caches"),
        ):
            body = _body(await self._call(self._base()))
        self.assertEqual([r["number"] for r in body["applied"]], [7, 8])
        self.assertEqual(body["failed"], [])

    async def test_one_moved_head_fails_only_its_own_row(self):
        # Both rows were rendered at SHA, but #8's branch has since been pushed to.
        def _detail(owner, repo, number, **kwargs):
            return {"head_sha": SHA if number == 7 else OTHER_SHA}

        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "get_pr_detail", side_effect=_detail),
            mock.patch.object(gh, "submit_pr_review", return_value={"id": 1}),
            mock.patch.object(store, "drop_pr_detail_cache"),
        ):
            body = _body(
                await self._call(self._base(action="approve", head_shas={"7": SHA, "8": SHA}))
            )
        self.assertEqual([r["number"] for r in body["applied"]], [7])
        self.assertEqual([r["number"] for r in body["failed"]], [8])
        self.assertIn("head branch moved", body["failed"][0]["error"])

    async def test_a_permission_refusal_is_a_per_row_failure_not_a_dead_batch(self):
        def _state(owner, repo, number, state, **kwargs):
            if number == 7:
                raise gh.GhPermissionError("locked")
            return {"state": "closed"}

        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "set_pr_state", side_effect=_state),
            mock.patch.object(store, "apply_pr_state_change_to_caches"),
        ):
            body = _body(await self._call(self._base()))
        self.assertEqual([r["number"] for r in body["failed"]], [7])
        self.assertEqual([r["number"] for r in body["applied"]], [8])

    async def test_an_upstream_failure_is_also_a_per_row_failure(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "set_pr_state", side_effect=gh.GhCliError("net")),
            mock.patch.object(store, "apply_pr_state_change_to_caches"),
        ):
            body = _body(await self._call(self._base()))
        self.assertEqual(len(body["failed"]), 2)
        self.assertEqual(body["applied"], [])

    async def test_duplicates_are_acted_on_once(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "set_pr_state", return_value={"state": "closed"}) as call,
            mock.patch.object(store, "apply_pr_state_change_to_caches"),
        ):
            await self._call(self._base(numbers=[7, 7, 8]))
        self.assertEqual(call.call_count, 2)


class TestEveryMutatingRouteDelegatesToTheSharedGates(unittest.IsolatedAsyncioTestCase):
    """The preamble is factored out because a per-handler copy is how one of them
    eventually ships without the permission check — so assert every route
    actually reaches it, rather than trusting that it was wired in."""

    HANDLERS = (
        ("pull/state", routes._handle_pull_state),
        ("pull/review", routes._handle_pull_review),
        ("pull/comment", routes._handle_pull_comment),
        ("pull/merge", routes._handle_pull_merge),
        ("pull/auto-merge", routes._handle_pull_auto_merge),
        ("pull/run", routes._handle_pull_run_action),
        ("pulls/bulk", routes._handle_pulls_bulk),
    )

    async def test_an_unconnected_repo_is_404_on_every_one(self):
        for path, handler in self.HANDLERS:
            with _connected(False):
                res = await handler(_json_request("POST", path, {"owner": "o", "repo": "r"}))
            self.assertEqual(res.status, 404, path)

    async def test_a_read_only_repo_is_403_on_every_one(self):
        for path, handler in self.HANDLERS:
            with _connected(), _writable(False):
                res = await handler(_json_request("POST", path, {"owner": "o", "repo": "r"}))
            self.assertEqual(res.status, 403, path)

    async def test_a_malformed_payload_is_400_on_every_one(self):
        for path, handler in self.HANDLERS:
            res = await handler(_json_request("POST", path, None))
            self.assertEqual(res.status, 400, path)

    async def test_the_number_bound_is_enforced_on_every_per_pull_request_route(self):
        # The bulk route takes an array instead, and is covered by TestPullsBulk.
        for path, handler in self.HANDLERS[:-1]:
            with _connected(), _writable():
                res = await handler(
                    _json_request("POST", path, {"owner": "o", "repo": "r", "number": 0})
                )
            self.assertEqual(_body(res)["code"], "invalid_number", path)


class TestRemainingValidationOrder(unittest.IsolatedAsyncioTestCase):
    """The per-route field checks that the class-level tests above do not reach."""

    async def test_pull_merge_validates_the_method_before_reading_the_head(self):
        with _connected(), _writable(), mock.patch.object(gh, "get_pr_detail") as detail:
            res = await routes._handle_pull_merge(
                _json_request(
                    "POST",
                    "pull/merge",
                    {"owner": "o", "repo": "r", "number": 7, "method": "octopus"},
                )
            )
        self.assertEqual(_body(res)["code"], "invalid_merge_method")
        detail.assert_not_called()

    async def test_pull_comment_bounds_the_body(self):
        with _connected(), _writable():
            res = await routes._handle_pull_comment(
                _json_request(
                    "POST",
                    "pull/comment",
                    {
                        "owner": "o",
                        "repo": "r",
                        "number": 7,
                        "body": "x" * (routes._PR_BODY_MAX_CHARS + 1),
                    },
                )
            )
        self.assertEqual(_body(res)["code"], "body_too_long")

    async def test_bulk_bounds_the_body_and_validates_the_method(self):
        with _connected(), _writable():
            res = await routes._handle_pulls_bulk(
                _json_request(
                    "POST",
                    "pulls/bulk",
                    {
                        "owner": "o",
                        "repo": "r",
                        "numbers": [1],
                        "action": "comment",
                        "body": "x" * (routes._PR_BODY_MAX_CHARS + 1),
                    },
                )
            )
        self.assertEqual(_body(res)["code"], "body_too_long")

        with _connected(), _writable():
            res = await routes._handle_pulls_bulk(
                _json_request(
                    "POST",
                    "pulls/bulk",
                    {
                        "owner": "o",
                        "repo": "r",
                        "numbers": [1],
                        "action": "close",
                        "method": "octopus",
                    },
                )
            )
        self.assertEqual(_body(res)["code"], "invalid_merge_method")

    async def test_a_review_the_provider_rejected_is_mapped_not_reported_as_done(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "get_pr_detail", return_value={"head_sha": SHA}),
            mock.patch.object(gh, "submit_pr_review", side_effect=gh.GhCliError("net")),
        ):
            res = await routes._handle_pull_review(
                _json_request(
                    "POST",
                    "pull/review",
                    {"owner": "o", "repo": "r", "number": 7, "event": "approve", "head_sha": SHA},
                )
            )
        self.assertEqual(res.status, 502)
