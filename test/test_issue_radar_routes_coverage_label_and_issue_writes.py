"""Issue Radar's label-creation, settings label-append and issue-state write routes.

This file pins the three write routes that are not pull-request actions. Each answers 400
for a malformed or non-object payload and for a missing field. Label creation and issue
state answer 404 for an unconnected repository before the write-permission check, 403 for
a read-only one before any provider write, and keep the provider error taxonomy; label
creation replaces an invalid colour, normalizes a hash-prefixed one, bounds the description
and adds the created label to the local cache, and issue state refuses a JSON boolean
number, an unknown state or close reason, defaults a close to ``completed``, clears the
reason on reopen and applies a change to the list caches. The settings label append maps an
unknown role to 400 and an unconnected repository to 404 and returns the whole saved
document. The pull-request action routes live in
``test_issue_radar_routes_coverage_pull_actions.py``; the field parsers, the shared gates
and the connect, settings and disconnect routes in
``test_issue_radar_routes_coverage.py``; the pure model-input shapers in
``test_issue_radar_routes_coverage_model_inputs.py``; and the shared request builders and
patch helpers in ``issue_radar_routes_coverage_helpers.py``.
"""

import unittest
from unittest import mock

from issue_radar_routes_coverage_helpers import (
    _body,
    _connected,
    _json_request,
    _writable,
)

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import routes, store

# ── label creation, settings append, issue state ─────────────────────────────


class TestCreateLabel(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body: object):
        return await routes._handle_create_label(_json_request("POST", "labels/create", body))

    def _base(self, **extra) -> dict:
        return {"owner": "o", "repo": "r", "name": "bug", **extra}

    async def test_a_malformed_or_non_object_payload_is_400(self):
        self.assertEqual((await self._call(None)).status, 400)
        self.assertEqual((await self._call(["x"])).status, 400)

    async def test_a_missing_repo_or_name_is_400(self):
        self.assertEqual((await self._call({"name": "bug"})).status, 400)
        self.assertEqual((await self._call({"owner": "o", "repo": "r", "name": " "})).status, 400)

    async def test_an_unconnected_repo_is_404_before_the_permission_gate(self):
        with _connected(False), mock.patch.object(routes, "_repo_can_write") as perms:
            res = await self._call(self._base())
        self.assertEqual(res.status, 404)
        perms.assert_not_called()

    async def test_a_read_only_repo_is_403_before_any_provider_write(self):
        with _connected(), _writable(False), mock.patch.object(gh, "create_label") as create:
            res = await self._call(self._base())
        self.assertEqual(res.status, 403)
        create.assert_not_called()

    async def test_an_invalid_colour_falls_back_rather_than_failing_the_request(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "create_label", return_value={"name": "bug"}) as create,
            mock.patch.object(store, "add_label_to_cache"),
        ):
            await self._call(self._base(color="not-a-colour"))
        self.assertEqual(create.call_args.args[3], "888888")

    async def test_a_hash_prefixed_colour_is_normalized(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "create_label", return_value={"name": "bug"}) as create,
            mock.patch.object(store, "add_label_to_cache"),
        ):
            await self._call(self._base(color="#EE0000"))
        self.assertEqual(create.call_args.args[3], "ee0000")

    async def test_the_description_is_bounded(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "create_label", return_value={"name": "bug"}) as create,
            mock.patch.object(store, "add_label_to_cache"),
        ):
            await self._call(self._base(description="d" * 500))
        self.assertEqual(len(create.call_args.args[4]), 100)

    async def test_a_created_label_lands_in_the_local_cache_for_the_pickers(self):
        label = {"name": "bug", "color": "ee0000"}
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "create_label", return_value=label),
            mock.patch.object(store, "add_label_to_cache") as cache,
        ):
            body = _body(await self._call(self._base()))
        self.assertEqual(body["label"], label)
        self.assertTrue(body["created"])
        cache.assert_called_once()

    async def test_provider_failures_keep_their_taxonomy(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "create_label", side_effect=gh.GhPermissionError("no")),
        ):
            self.assertEqual((await self._call(self._base())).status, 403)
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "create_label", side_effect=gh.GhCliError("net")),
        ):
            self.assertEqual((await self._call(self._base())).status, 502)


class TestAddSettingsLabel(unittest.IsolatedAsyncioTestCase):
    """The append endpoint that exists because the settings PUT replaces the
    WHOLE document, so a read-then-write can only serialize itself."""

    async def _call(self, body: object):
        return await routes._handle_add_settings_label(_json_request("POST", "settings/role", body))

    async def test_a_malformed_or_non_object_payload_is_400(self):
        self.assertEqual((await self._call(None)).status, 400)
        self.assertEqual((await self._call([1])).status, 400)

    async def test_a_missing_repo_role_or_label_is_400(self):
        self.assertEqual((await self._call({"role": "triage", "label": "bug"})).status, 400)
        self.assertEqual(
            (await self._call({"owner": "o", "repo": "r", "label": "bug"})).status, 400
        )
        self.assertEqual(
            (await self._call({"owner": "o", "repo": "r", "role": "triage"})).status, 400
        )

    async def test_an_unknown_role_is_400(self):
        with mock.patch.object(store, "add_setting_label", side_effect=ValueError("bad role")):
            res = await self._call({"owner": "o", "repo": "r", "role": "nope", "label": "bug"})
        self.assertEqual(res.status, 400)

    async def test_an_unconnected_repo_is_404(self):
        with mock.patch.object(store, "add_setting_label", side_effect=KeyError("o/r")):
            res = await self._call({"owner": "o", "repo": "r", "role": "triage", "label": "bug"})
        self.assertEqual(res.status, 404)

    async def test_a_successful_append_returns_the_whole_document(self):
        with mock.patch.object(
            store, "add_setting_label", return_value={"revision": 5, "triage_labels": ["bug"]}
        ):
            body = _body(
                await self._call({"owner": "o", "repo": "r", "role": "triage", "label": "bug"})
            )
        self.assertEqual(body["settings"]["triage_labels"], ["bug"])


class TestIssueState(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body: object):
        return await routes._handle_issue_state(_json_request("POST", "issue/state", body))

    def _base(self, **extra) -> dict:
        return {"owner": "o", "repo": "r", "number": 5, "state": "closed", **extra}

    async def test_a_malformed_or_non_object_payload_is_400(self):
        self.assertEqual((await self._call(None)).status, 400)
        self.assertEqual((await self._call("x")).status, 400)

    async def test_a_missing_repo_is_400(self):
        self.assertEqual((await self._call({"number": 5, "state": "closed"})).status, 400)

    async def test_a_json_boolean_is_not_issue_one(self):
        res = await self._call(self._base(number=True))
        self.assertEqual(res.status, 400)

    async def test_an_unknown_state_is_400(self):
        self.assertEqual((await self._call(self._base(state="merged"))).status, 400)

    async def test_an_unknown_close_reason_is_400(self):
        res = await self._call(self._base(state_reason="wontfix"))
        self.assertEqual(res.status, 400)

    async def test_closing_defaults_the_reason_to_completed(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(
                gh,
                "set_issue_state",
                return_value={"state": "closed", "state_reason": "completed"},
            ) as call,
            mock.patch.object(store, "apply_state_change_to_caches"),
        ):
            body = _body(await self._call(self._base()))
        self.assertEqual(call.call_args.args[4], "completed")
        self.assertEqual(body["state_reason"], "completed")

    async def test_reopening_clears_any_reason(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "set_issue_state", return_value={"state": "open"}) as call,
            mock.patch.object(store, "apply_state_change_to_caches"),
        ):
            await self._call(self._base(state="open", state_reason="not_planned"))
        self.assertIsNone(call.call_args.args[4])

    async def test_an_unconnected_repo_is_404_before_the_permission_gate(self):
        with _connected(False), mock.patch.object(routes, "_repo_can_write") as perms:
            res = await self._call(self._base())
        self.assertEqual(res.status, 404)
        perms.assert_not_called()

    async def test_a_read_only_repo_is_403_before_any_provider_write(self):
        with _connected(), _writable(False), mock.patch.object(gh, "set_issue_state") as call:
            res = await self._call(self._base())
        self.assertEqual(res.status, 403)
        call.assert_not_called()

    async def test_provider_failures_keep_their_taxonomy(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "set_issue_state", side_effect=gh.GhPermissionError("no")),
        ):
            self.assertEqual((await self._call(self._base())).status, 403)
        with (
            _connected(),
            _writable(),
            mock.patch.object(gh, "set_issue_state", side_effect=gh.GhCliError("net")),
        ):
            self.assertEqual((await self._call(self._base())).status, 502)

    async def test_a_successful_change_is_applied_to_the_list_caches(self):
        with (
            _connected(),
            _writable(),
            mock.patch.object(
                gh,
                "set_issue_state",
                return_value={"state": "closed", "state_reason": "not_planned"},
            ),
            mock.patch.object(store, "apply_state_change_to_caches") as apply_,
        ):
            body = _body(await self._call(self._base(state_reason="not_planned")))
        self.assertEqual(body["state"], "closed")
        apply_.assert_called_once()
