"""Issue Radar — the assignee editor's write route (``POST /issue/assignees``).

The route REPLACES an issue's whole assignee set rather than applying an
add/remove delta, and these tests pin the consequences of that choice:

  * an empty array is a legitimate request (it clears every assignee) and must
    NOT be rejected the way ``/labels/apply`` rejects an empty change -- that is
    the one place this route's contract deliberately differs from the label one;
  * the provider is authoritative about what actually stuck, so the response
    echoes the client's request only when the provider agreed with it;
  * the write is gated on the same triage/push access as every other mutation,
    and a repo that cannot be shown to be writable is refused (fail-closed);
  * the count cap and the case-insensitive dedupe are enforced BEFORE the
    provider call, so a duplicate login cannot inflate the request past the cap.

Subprocess-free: the provider client is monkeypatched, so nothing shells out to
``gh``/``glab``.
"""

import asyncio
import contextlib
import json
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import routes


def _req(payload: object) -> web.Request:
    """A real ``web.Request`` whose ``.json()`` yields ``payload``.

    Mirrors ``test_pr_actions._req``: built on aiohttp's own
    ``make_mocked_request`` so the handler runs against the actual Request type.
    Passing an Exception makes ``.json()`` raise it, reaching the malformed-body
    path.
    """
    request = make_mocked_request("POST", "/api/apps/issue-radar/issue/assignees")

    async def _json(*_args: object, **_kwargs: object) -> object:
        if isinstance(payload, Exception):
            raise payload
        return payload

    request.json = _json  # type: ignore[method-assign]
    return request


def _await(coro):
    """Drive one coroutine to completion (see test_pr_actions._await on the name)."""
    return asyncio.run(coro)


def _body(response):
    return json.loads(response.text)


def _ok(payload, *, returns=None, can_write=True, connected=True, current=None):
    """Run the handler with the gates satisfied and the provider stubbed.

    Returns ``(response, set_issue_assignees_mock)``. ``current`` is what the forge
    reports as the issue's PRESENT assignees, which the precondition is checked
    against; it defaults to the payload's own ``expected`` so the happy path passes.
    """
    client = mock.Mock()
    client.set_issue_assignees.return_value = (
        returns if returns is not None else payload.get("assignees", [])
    )
    forge_now = current if current is not None else list(payload.get("expected", []))
    client.get_issue_detail.return_value = {"assignees": forge_now}
    with (
        mock.patch.object(routes, "_connected", return_value=connected),
        mock.patch.object(routes, "_repo_can_write", return_value=can_write),
        mock.patch.object(routes.provider, "client_for", return_value=client),
        mock.patch.object(routes.store, "issue_write_lock", _noop_lock),
        mock.patch.object(routes.store, "apply_assignees_change_to_caches"),
    ):
        resp = _await(routes._handle_issue_assignees(_req(payload)))
    return resp, client.set_issue_assignees


@contextlib.contextmanager
def _noop_lock(*_args, **_kwargs):
    """Stand in for the per-issue file lock: these tests exercise the handler's
    decisions, not the locking, and a real lock would touch the data home."""
    yield


# Every request carries `expected` (the set the client last read). BASE keeps it
# empty, matching an issue with nobody assigned.
BASE = {"owner": "o", "repo": "r", "number": 7, "expected": []}


class TestAssigneeRouteHappyPath(unittest.TestCase):
    def test_replaces_the_set_and_returns_it(self):
        resp, write = _ok({**BASE, "assignees": ["alice", "bob"]})
        self.assertEqual(resp.status, 200)
        self.assertEqual(write.call_args.args[3], ["alice", "bob"])
        self.assertEqual(
            _body(resp),
            {"owner": "o", "repo": "r", "number": 7, "assignees": ["alice", "bob"]},
        )

    def test_empty_array_clears_and_is_not_rejected(self):
        """The label route refuses an empty change; this one must not. Clearing
        every assignee is the only way to unassign, so an empty list has to reach
        the provider."""
        resp, write = _ok({**BASE, "assignees": []})
        self.assertEqual(resp.status, 200)
        write.assert_called_once()
        self.assertEqual(write.call_args.args[3], [])
        self.assertEqual(_body(resp)["assignees"], [])

    def test_response_reports_what_stuck_not_what_was_asked(self):
        """The provider drops a login it will not assign. Reporting the request
        would show the user an assignee the issue does not carry."""
        resp, _ = _ok({**BASE, "assignees": ["alice", "stranger"]}, returns=["alice"])
        self.assertEqual(_body(resp)["assignees"], ["alice"])

    def test_trims_surrounding_whitespace(self):
        resp, write = _ok({**BASE, "assignees": ["  alice  "]})
        self.assertEqual(resp.status, 200)
        self.assertEqual(write.call_args.args[3], ["alice"])

    def test_a_junk_entry_is_rejected_not_silently_dropped(self):
        """Dropping junk was a DESTRUCTIVE bug on a replace endpoint: `[null]`
        normalized to `[]`, which is the wire form for "clear everyone", so a
        malformed request silently unassigned the whole issue. Each of these must
        be a 400, and no write may be attempted."""
        for bad in ([None], [5], [""], ["   "], ["alice", None], [True], [["alice"]]):
            client = mock.Mock()
            with (
                mock.patch.object(routes, "_connected", return_value=True),
                mock.patch.object(routes, "_repo_can_write", return_value=True),
                mock.patch.object(routes.provider, "client_for", return_value=client),
            ):
                resp = _await(routes._handle_issue_assignees(_req({**BASE, "assignees": bad})))
            self.assertEqual(resp.status, 400, bad)
            self.assertEqual(_body(resp)["code"], "invalid_assignee_entry", bad)
            client.set_issue_assignees.assert_not_called()

    def test_an_explicit_empty_array_still_clears(self):
        """The fix above must not take the legitimate clear with it: an empty array
        is the only way to unassign everyone and stays a 200."""
        resp, write = _ok(
            {**BASE, "assignees": [], "expected": ["alice"]}, current=["alice"], returns=[]
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(write.call_args.args[3], [])

    def test_dedupes_case_insensitively_preserving_order(self):
        """A repeated login is a no-op to the provider but would count against the
        cap, so it is collapsed before the count is checked."""
        resp, write = _ok({**BASE, "assignees": ["alice", "Alice", "bob", "ALICE"]})
        self.assertEqual(resp.status, 200)
        self.assertEqual(write.call_args.args[3], ["alice", "bob"])


class TestAssigneeRouteValidation(unittest.TestCase):
    def test_malformed_body_is_400(self):
        resp = _await(routes._handle_issue_assignees(_req(ValueError("not json"))))
        self.assertEqual(resp.status, 400)

    def test_non_object_body_is_400(self):
        resp = _await(routes._handle_issue_assignees(_req(["not", "an", "object"])))
        self.assertEqual(resp.status, 400)

    def test_missing_assignees_key_is_400(self):
        """Absent is NOT the same as empty: an omitted field is a malformed
        request, while ``[]`` is an explicit instruction to clear. Treating the
        omission as a clear would let a partial client wipe the assignees."""
        resp = _await(routes._handle_issue_assignees(_req(dict(BASE))))
        self.assertEqual(resp.status, 400)

    def test_non_array_assignees_is_400(self):
        for bad in ("alice", {"login": "alice"}, 5, True):
            resp = _await(routes._handle_issue_assignees(_req({**BASE, "assignees": bad})))
            self.assertEqual(resp.status, 400, bad)

    def test_over_the_cap_is_400(self):
        many = [f"u{n}" for n in range(routes.MAX_ASSIGNEES + 1)]
        resp = _await(routes._handle_issue_assignees(_req({**BASE, "assignees": many})))
        self.assertEqual(resp.status, 400)
        self.assertIn(str(routes.MAX_ASSIGNEES), _body(resp)["error"])

    def test_exactly_the_cap_is_allowed(self):
        many = [f"u{n}" for n in range(routes.MAX_ASSIGNEES)]
        resp, write = _ok({**BASE, "assignees": many})
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(write.call_args.args[3]), routes.MAX_ASSIGNEES)

    def test_bad_number_is_400(self):
        for bad in (0, -1, True, "7", None, 1.5):
            resp = _await(
                routes._handle_issue_assignees(
                    _req({"owner": "o", "repo": "r", "number": bad, "assignees": []})
                )
            )
            self.assertEqual(resp.status, 400, bad)

    def test_an_absurdly_large_number_is_400_not_500(self):
        """The number reaches the FILESYSTEM -- issue_write_lock names its lock file
        after it -- so a several-hundred-digit value would raise ENAMETOOLONG and
        answer 500 on input that is simply invalid. No provider call is attempted."""
        client = mock.Mock()
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(routes.provider, "client_for", return_value=client),
        ):
            resp = _await(
                routes._handle_issue_assignees(
                    _req({**BASE, "number": 10**300, "assignees": ["alice"]})
                )
            )
            client.get_issue_detail.assert_not_called()
            client.set_issue_assignees.assert_not_called()
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "item_number_out_of_range")

    def test_the_largest_allowed_number_still_passes_validation(self):
        """The bound must not reject the legal ceiling."""
        resp, write = _ok({**BASE, "number": routes.MAX_ITEM_NUMBER, "assignees": ["alice"]})
        self.assertEqual(resp.status, 200)
        write.assert_called_once()

    def test_missing_owner_or_repo_is_400(self):
        resp = _await(routes._handle_issue_assignees(_req({"number": 7, "assignees": []})))
        self.assertEqual(resp.status, 400)


def _fail(side_effect, *, expected=None, current=None):
    """Run the handler with the gates satisfied and the provider WRITE raising.

    Returns ``(response, cache_patch_mock)``. The precondition read is stubbed to
    match, so the error under test is the only thing that can fail the request.
    """
    exp = list(expected or [])
    client = mock.Mock()
    client.get_issue_detail.return_value = {
        "assignees": list(current) if current is not None else exp
    }
    client.set_issue_assignees.side_effect = side_effect
    with (
        mock.patch.object(routes, "_connected", return_value=True),
        mock.patch.object(routes, "_repo_can_write", return_value=True),
        mock.patch.object(routes.provider, "client_for", return_value=client),
        mock.patch.object(routes.store, "issue_write_lock", _noop_lock),
        mock.patch.object(routes.store, "apply_assignees_change_to_caches") as patch,
    ):
        resp = _await(
            routes._handle_issue_assignees(_req({**BASE, "assignees": ["alice"], "expected": exp}))
        )
    return resp, patch


class TestAssigneePrecondition(unittest.TestCase):
    """The `expected` precondition is what stops replace semantics from silently
    erasing a concurrent edit."""

    def test_a_stale_expected_is_409_and_writes_nothing(self):
        """Two people each start from {alice} and add one name. The second write
        must NOT land -- otherwise it overwrites the first one's addition."""
        client = mock.Mock()
        client.get_issue_detail.return_value = {"assignees": ["alice", "bob"]}
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(routes.provider, "client_for", return_value=client),
            mock.patch.object(routes.store, "issue_write_lock", _noop_lock),
            mock.patch.object(routes.store, "apply_assignees_change_to_caches") as patch,
        ):
            resp = _await(
                routes._handle_issue_assignees(
                    _req({**BASE, "assignees": ["alice", "carol"], "expected": ["alice"]})
                )
            )
            client.set_issue_assignees.assert_not_called()
            patch.assert_not_called()
        self.assertEqual(resp.status, 409)
        body = _body(resp)
        self.assertEqual(body["code"], "assignees_conflict")
        # The current set rides back so the client can re-render rather than retry.
        self.assertEqual(body["assignees"], ["alice", "bob"])

    def test_expected_is_compared_case_and_order_insensitively(self):
        """The forge is case-preserving but not case-sensitive, and the set has no
        order, so neither may cause a spurious conflict."""
        resp, write = _ok(
            {**BASE, "assignees": ["alice"], "expected": ["Bob", "alice"]},
            current=["ALICE", "bob"],
            returns=["alice"],
        )
        self.assertEqual(resp.status, 200)
        write.assert_called_once()

    def test_a_matching_expected_writes(self):
        resp, write = _ok(
            {**BASE, "assignees": ["alice", "bob"], "expected": ["alice"]},
            current=["alice"],
            returns=["alice", "bob"],
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(write.call_args.args[3], ["alice", "bob"])

    def test_missing_or_malformed_expected_is_400(self):
        """Fail-closed: without the precondition the endpoint would be back to
        silently overwriting, so an absent `expected` is refused rather than
        defaulted."""
        for payload in (
            {"owner": "o", "repo": "r", "number": 7, "assignees": ["alice"]},
            {**BASE, "assignees": ["alice"], "expected": "alice"},
            {**BASE, "assignees": ["alice"], "expected": [None]},
            {**BASE, "assignees": ["alice"], "expected": {}},
        ):
            client = mock.Mock()
            with (
                mock.patch.object(routes, "_connected", return_value=True),
                mock.patch.object(routes, "_repo_can_write", return_value=True),
                mock.patch.object(routes.provider, "client_for", return_value=client),
            ):
                resp = _await(routes._handle_issue_assignees(_req(payload)))
            self.assertEqual(resp.status, 400, payload)
            self.assertEqual(_body(resp)["code"], "expected_required", payload)
            client.set_issue_assignees.assert_not_called()


class TestAssigneeRouteGates(unittest.TestCase):
    def test_unconnected_repo_is_404(self):
        with (
            mock.patch.object(routes, "_connected", return_value=False),
            mock.patch.object(routes.provider, "client_for") as client_for,
        ):
            resp = _await(routes._handle_issue_assignees(_req({**BASE, "assignees": ["alice"]})))
            client_for.return_value.set_issue_assignees.assert_not_called()
        self.assertEqual(resp.status, 404)

    def test_read_only_repo_is_403_and_writes_nothing(self):
        client = mock.Mock()
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=False),
            mock.patch.object(routes.provider, "client_for", return_value=client),
        ):
            resp = _await(routes._handle_issue_assignees(_req({**BASE, "assignees": ["alice"]})))
            client.set_issue_assignees.assert_not_called()
        self.assertEqual(resp.status, 403)

    def test_unknown_write_access_is_refused(self):
        """``_repo_can_write`` answers None when it genuinely cannot tell. The gate
        is ``is not True``, so an unknown must deny rather than allow."""
        client = mock.Mock()
        with (
            mock.patch.object(routes, "_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=None),
            mock.patch.object(routes.provider, "client_for", return_value=client),
        ):
            resp = _await(routes._handle_issue_assignees(_req({**BASE, "assignees": ["alice"]})))
            client.set_issue_assignees.assert_not_called()
        self.assertEqual(resp.status, 403)

    def test_provider_permission_error_is_403(self):
        resp, _ = _fail(gh.GhPermissionError("refused"))
        self.assertEqual(resp.status, 403)
        self.assertEqual(_body(resp)["code"], "provider_forbidden")

    def test_provider_cli_error_is_502(self):
        resp, _ = _fail(gh.GhCliError("upstream boom"))
        self.assertEqual(resp.status, 502)
        self.assertEqual(_body(resp)["code"], "provider_error")

    def test_unassignable_login_is_400_naming_it_not_502(self):
        """The forge refused the LOGIN, not the caller. A 502 would report the forge
        as broken and invite a retry that can only fail identically, so this is a
        400 that names who was refused.

        Ordering matters: the invalid-input class is a GhCliError SUBCLASS, so if
        its except clause were placed after the generic one this would answer 502.
        """
        resp, _ = _fail(
            gh.GhInvalidInputError("GitHub will not assign: octocat", values=["octocat"])
        )
        self.assertEqual(resp.status, 400)
        body = _body(resp)
        self.assertEqual(body["code"], "invalid_assignees")
        self.assertEqual(body["invalid_assignees"], ["octocat"])
        self.assertIn("octocat", body["error"])

    def test_a_refused_write_does_not_patch_the_caches(self):
        """Neither provider applies a partial write when a login is refused, so the
        caches must be left alone -- patching them would show an assignee the issue
        does not carry until the next refresh."""
        resp, patch = _fail(gh.GhInvalidInputError("refused", values=["octocat"]))
        patch.assert_not_called()
        self.assertEqual(resp.status, 400)


class TestAssigneeRouteRegistered(unittest.TestCase):
    def test_route_is_registered_as_a_post(self):
        app = web.Application()
        with mock.patch.object(routes, "watch"):
            routes.register_routes(app)
        paths = {
            (r.method, r.resource.canonical)  # type: ignore[union-attr]
            for r in app.router.routes()
        }
        self.assertIn(("POST", "/api/apps/issue-radar/issue/assignees"), paths)
