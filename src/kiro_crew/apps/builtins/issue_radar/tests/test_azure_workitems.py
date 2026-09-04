"""The Azure DevOps WORK ITEM READ path: hydration, normalization, timeline.

``test_azure.py`` covers the boundaries -- URL parsing, host pinning, WIQL
escaping, the tag-delimiter refusal, the rev guard, the probe refusals. This file
covers the layer underneath them: the code that turns Azure's own payloads into
the GitHub-shaped rows the rest of the app reads, and the reads that produce
those payloads.

The bugs it is aimed at are the silent ones, because every one of them renders as
plausible data rather than as an error:

  * A field mapped to the wrong key, or a value Azure genuinely does not have
    reported as if it did. ``author_association`` and ``state_reason`` are the two
    that matter: both are ``None`` on purpose, and both feed UI that would render
    a guess as a verdict.
  * A state read as "open" when the project's process template calls its closing
    state something this module has never heard of -- which is the normal case on
    a custom process, and shows up as closed work items filling the triage list.
  * The batch hydrate losing or reordering rows. It is chunked at a documented id
    cap and stitched back into WIQL's order, so a stitch bug reorders the list
    view or drops items with no error anywhere.
  * ``#5`` answered from the pull request endpoint. Azure allocates work item ids
    and pull request ids from independent services, so that fallback would
    describe a DIFFERENT item under the number the user asked about.
  * The timeline showing Azure's revision churn. Every field write produces a
    revision, so a timeline that does not filter is unreadable.

No test here reaches the network or needs ``az``: each one either exercises a
pure function or replaces ``azure_client._az_invoke`` -- the single point every
REST call funnels through -- with a recording fake, exactly as ``test_azure.py``
and ``test_gitlab.py`` do.
"""

from __future__ import annotations

import unittest
from unittest import mock

from kiro_crew.apps.builtins.issue_radar.backend import azure_client
from kiro_crew.apps.builtins.issue_radar.backend.errors import (
    ProviderCliError,
    ProviderPermissionError,
    ProviderSetupError,
)

OWNER = "contoso/Widgets"
REPO = "widget-service"
HOST = "dev.azure.com"

# The state definitions a stock Agile project answers with: one closing state per
# work item type, reachable only through the type's CATEGORY.
_BUG_STATES = [
    {"name": "New", "category": "Proposed"},
    {"name": "Active", "category": "InProgress"},
    {"name": "Closed", "category": "Completed"},
]
_TASK_STATES = [
    {"name": "To Do", "category": "Proposed"},
    {"name": "Shipped", "category": "Completed"},
    {"name": "Removed", "category": "Removed"},
]


class _Az:
    """A recording stand-in for ``azure_client._az_invoke``, keyed by resource.

    A handler is a response value, a callable taking the call's kwargs, or an
    exception instance to raise. An unregistered resource is an assertion failure
    rather than an empty response, so a test cannot accidentally pass because a
    call it did not expect returned something harmless.
    """

    def __init__(self, **handlers: object) -> None:
        self.handlers = handlers
        self.calls: list[dict] = []

    def __call__(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        resource = str(kwargs.get("resource") or "")
        if resource not in self.handlers:
            raise AssertionError(f"unexpected resource: {resource!r}")
        handler = self.handlers[resource]
        if isinstance(handler, BaseException):
            raise handler
        if callable(handler):
            return handler(kwargs)
        return handler

    @property
    def resources(self) -> list[str]:
        return [str(call.get("resource") or "") for call in self.calls]

    def for_resource(self, resource: str) -> list[dict]:
        return [call for call in self.calls if call.get("resource") == resource]


def _fields(**over: object) -> dict:
    """A work item's ``fields`` map, as the batch hydrate returns it."""
    fields: dict = {
        "System.Title": "Ledger totals drift",
        "System.State": "Active",
        "System.WorkItemType": "Bug",
        "System.Tags": "needs-triage; blocked",
        "System.CreatedBy": {"uniqueName": "ada@contoso.com", "displayName": "Ada L"},
        "System.CreatedDate": "2026-01-01T00:00:00Z",
        "System.ChangedDate": "2026-02-02T00:00:00Z",
        "System.AssignedTo": {"uniqueName": "grace@contoso.com"},
        "System.Description": "<div>totals drift by a cent</div>",
        "System.CommentCount": 3,
        "System.IterationPath": "Widgets\\Release 2\\Sprint 4",
    }
    fields.update(over)
    return fields


def _item(number: int = 42, **over: object) -> dict:
    row: dict = {"id": number, "rev": 7, "fields": _fields(**over)}
    return row


def _batch(rows: list[dict]):
    """A ``workitemsbatch`` handler answering only the ids the call asked for."""

    by_id = {row["id"]: row for row in rows}

    def handler(kwargs: dict) -> dict:
        body = kwargs.get("body")
        assert isinstance(body, dict), f"the batch read must POST a body, got {body!r}"
        ids = body.get("ids")
        assert isinstance(ids, list), f"the batch body must carry ids, got {body!r}"
        return {"value": [by_id[i] for i in ids if i in by_id]}

    return handler


def _clear_caches() -> None:
    """Drop the module's per-organization metadata caches.

    They are process-lifetime by design (a project GUID and a template's state
    names do not change), which means one test's fake would otherwise answer
    another's read -- and the caching itself is behaviour worth pinning, so it has
    to start from a known-empty state.
    """
    azure_client._project_id_cache.clear()
    azure_client._closed_states_cache.clear()
    azure_client._identity_cache.clear()


class AzureReadTestCase(unittest.TestCase):
    """Base case that isolates the module-level metadata caches."""

    def setUp(self) -> None:
        _clear_caches()
        self.addCleanup(_clear_caches)


class TestResponseShapeReaders(unittest.TestCase):
    """``_obj`` / ``_values`` / ``_field`` are the only place a payload is trusted.

    Everything below them indexes the result without re-checking, so a shape these
    three accept wrongly becomes an ``AttributeError`` deep in normalization --
    reported to the user as a 500 on a list refresh.
    """

    def test_a_list_response_is_read_from_the_value_wrapper(self):
        self.assertEqual(azure_client._values({"count": 1, "value": [{"id": 1}]}), [{"id": 1}])

    def test_a_bare_array_response_is_accepted_too(self):
        # A few Azure routes answer with a bare array rather than the wrapper, so
        # both shapes have to read as rows.
        self.assertEqual(azure_client._values([{"id": 1}, {"id": 2}]), [{"id": 1}, {"id": 2}])

    def test_non_dict_rows_are_dropped_rather_than_returned(self):
        # Every caller treats a row as a dict. A string or null row slipping
        # through is an AttributeError one or two frames later.
        self.assertEqual(azure_client._values({"value": [{"id": 1}, "nope", None, 7]}), [{"id": 1}])

    def test_a_response_that_is_not_a_collection_reads_as_no_rows(self):
        for raw in (None, {}, {"value": None}, {"value": "x"}, "text", 5):
            with self.subTest(raw=raw):
                self.assertEqual(azure_client._values(raw), [])

    def test_obj_narrows_a_non_dict_to_an_empty_dict(self):
        self.assertEqual(azure_client._obj({"a": 1}), {"a": 1})
        raw: object
        for raw in (None, [], "x", 3):
            with self.subTest(raw=raw):
                self.assertEqual(azure_client._obj(raw), {})

    def test_field_substitutes_the_default_only_for_a_missing_or_null_value(self):
        """A present-but-falsy value must survive, or a real 0 becomes the default.

        ``System.CommentCount`` is the field this matters on: an item with no
        comments answers ``0``, and a ``value or default`` read would be
        indistinguishable from the field being absent.
        """
        fields = {"present": "v", "zero": 0, "empty": "", "false": False, "null": None}
        self.assertEqual(azure_client._field(fields, "present", "d"), "v")
        self.assertEqual(azure_client._field(fields, "zero", 99), 0)
        self.assertEqual(azure_client._field(fields, "empty", "d"), "")
        self.assertIs(azure_client._field(fields, "false", True), False)
        self.assertEqual(azure_client._field(fields, "null", "d"), "d")
        self.assertEqual(azure_client._field(fields, "absent", "d"), "d")
        self.assertIsNone(azure_client._field(fields, "absent"))


class TestWorkItemUrl(unittest.TestCase):
    """The work item link is SYNTHESIZED, so its shape is not validated upstream.

    The batch hydrate selects fields and Azure returns ``_links`` only for a full
    read, so nothing compares this against a server-supplied URL. If it were
    wrong, every row would carry a link that 404s.
    """

    def test_the_url_is_project_scoped_and_carries_no_repository(self):
        url = azure_client._work_item_url("contoso", "Widgets", 42)
        self.assertEqual(url, "https://dev.azure.com/contoso/Widgets/_workitems/edit/42")
        self.assertNotIn("_git", url)

    def test_names_with_spaces_are_percent_encoded(self):
        # Azure allows spaces in org and project names, and this string lands in an
        # href, so a raw space would break the link rather than being tolerated.
        self.assertEqual(
            azure_client._work_item_url("con toso", "My Project", 7),
            "https://dev.azure.com/con%20toso/My%20Project/_workitems/edit/7",
        )

    def test_a_slash_in_a_name_cannot_extend_the_path(self):
        # Defense in depth behind _bad_segment: quote(safe='') keeps a separator
        # from addressing another resource even if a name reached here unchecked.
        self.assertIn("a%2Fb", azure_client._work_item_url("a/b", "Widgets", 1))

    def test_the_number_is_coerced_to_an_int(self):
        self.assertEqual(
            azure_client._work_item_url("contoso", "Widgets", True and 5),
            "https://dev.azure.com/contoso/Widgets/_workitems/edit/5",
        )

    def test_the_host_is_the_pinned_one_not_a_caller_supplied_string(self):
        self.assertTrue(
            azure_client._work_item_url("contoso", "Widgets", 1).startswith(
                f"https://{azure_client.AZURE_HOST}/"
            )
        )


class TestNormIssue(unittest.TestCase):
    """One work item -> the list-view row shape ``github_client._ISSUE_JQ`` produces.

    The list view, the watcher and the cache all read these keys directly, so a
    missing or misnamed one is a blank column rather than a failure.
    """

    CLOSED = frozenset({"Closed", "Shipped"})

    def _row(self, **over):
        return azure_client._norm_issue(
            _item(**over), org="contoso", project="Widgets", closed_states=self.CLOSED
        )

    def test_every_mapped_field_lands_on_its_key(self):
        self.assertEqual(
            self._row(),
            {
                "number": 42,
                "title": "Ledger totals drift",
                "url": "https://dev.azure.com/contoso/Widgets/_workitems/edit/42",
                "labels": ["needs-triage", "blocked"],
                "comments": 3,
                "reactions": 0,
                "thumbs_up": 0,
                "author_association": None,
                "updated_at": "2026-02-02T00:00:00Z",
                "created_at": "2026-01-01T00:00:00Z",
                "state": "open",
                "author": "ada@contoso.com",
                "assignees": ["grace@contoso.com"],
                "body": "<div>totals drift by a cent</div>",
            },
        )

    def test_author_association_is_none_because_azure_reports_no_such_thing(self):
        """Not "NONE", not "CONTRIBUTOR" -- absent.

        Azure has no computed contributor-relationship concept, so any string here
        would be fiction, and this field badges people in the UI. Asserted for
        both an ordinary item and one whose author is the project's own service
        identity, since a "looks like an owner" heuristic would fire there.
        """
        self.assertIsNone(self._row()["author_association"])
        service = self._row(**{"System.CreatedBy": {"displayName": "Build Service"}})
        self.assertIsNone(service["author_association"])

    def test_state_is_decided_by_the_projects_own_closing_names(self):
        # "Shipped" is not a name this module knows; it is closed here only because
        # the project's template said so. That indirection is the whole point.
        self.assertEqual(self._row(**{"System.State": "Shipped"})["state"], "closed")
        self.assertEqual(self._row(**{"System.State": "Closed"})["state"], "closed")
        self.assertEqual(self._row(**{"System.State": "New"})["state"], "open")
        # An unknown state is OPEN, which is the safe direction: a stale item shows
        # up in triage rather than disappearing from it.
        self.assertEqual(self._row(**{"System.State": "Marinating"})["state"], "open")

    def test_reactions_are_zero_rather_than_unknown(self):
        # A work item carries no reaction data of any kind, so zero is the TRUE
        # count -- unlike the detail pane, where the whole object is unknown.
        row = self._row()
        self.assertEqual((row["reactions"], row["thumbs_up"]), (0, 0))

    def test_the_single_azure_assignee_becomes_a_one_element_list(self):
        self.assertEqual(self._row()["assignees"], ["grace@contoso.com"])

    def test_an_unassigned_item_reports_an_empty_list_not_a_null_entry(self):
        # ``[None]`` would render as a nameless avatar and break any `.length`
        # check the UI does.
        self.assertEqual(self._row(**{"System.AssignedTo": None})["assignees"], [])

    def test_a_missing_text_field_becomes_an_empty_string_not_none(self):
        row = self._row(**{"System.Title": None, "System.Description": None})
        self.assertEqual((row["title"], row["body"]), ("", ""))

    def test_a_commentless_item_reports_zero_comments(self):
        self.assertEqual(self._row(**{"System.CommentCount": None})["comments"], 0)

    def test_no_tags_field_is_no_labels(self):
        self.assertEqual(self._row(**{"System.Tags": None})["labels"], [])

    def test_a_row_without_an_integer_id_gets_an_empty_url_rather_than_a_crash(self):
        # ``int(None)`` in the URL builder would be a 500 on a list refresh, and a
        # single malformed row must not take the whole page down.
        row = azure_client._norm_issue(
            {"id": None, "fields": _fields()},
            org="contoso",
            project="Widgets",
            closed_states=self.CLOSED,
        )
        self.assertEqual(row["url"], "")
        self.assertIsNone(row["number"])

    def test_a_row_with_no_fields_at_all_normalizes_instead_of_raising(self):
        row = azure_client._norm_issue(
            {"id": 9}, org="contoso", project="Widgets", closed_states=self.CLOSED
        )
        self.assertEqual(row["number"], 9)
        self.assertEqual(row["state"], "open")
        self.assertEqual(row["labels"], [])
        self.assertEqual(row["title"], "")


class TestProjectId(AzureReadTestCase):
    """The project GUID is the id a policy-evaluation artifact is addressed by."""

    def test_the_guid_is_read_from_the_core_projects_route(self):
        az = _Az(projects={"id": "8a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9", "name": "Widgets"})
        with mock.patch.object(azure_client, "_az_invoke", az):
            value = azure_client._project_id("contoso", "Widgets", host=HOST, timeout=1.0)
        self.assertEqual(value, "8a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9")
        self.assertEqual(az.for_resource("projects")[0]["route"], {"projectId": "Widgets"})

    def test_the_guid_is_cached_so_a_second_read_costs_no_call(self):
        # A project's GUID never changes, and this is read on every PR check
        # refresh, so the cache is behaviour rather than an optimization detail.
        az = _Az(projects={"id": "8a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"})
        with mock.patch.object(azure_client, "_az_invoke", az):
            first = azure_client._project_id("contoso", "Widgets", host=HOST, timeout=1.0)
            second = azure_client._project_id("contoso", "Widgets", host=HOST, timeout=1.0)
        self.assertEqual(first, second)
        self.assertEqual(len(az.calls), 1)

    def test_a_response_without_a_guid_is_refused_rather_than_cached(self):
        """A non-GUID must not reach an argv, and must not poison the cache.

        This value is interpolated into a later route, so accepting whatever the
        field held would send it onward; and caching it would make one bad
        response permanent for the life of the process.
        """
        for bad in ({}, {"id": ""}, {"id": "not-a-guid"}, {"id": 5}):
            with self.subTest(bad=bad):
                az = _Az(projects=bad)
                with mock.patch.object(azure_client, "_az_invoke", az):
                    with self.assertRaises(ProviderCliError):
                        azure_client._project_id("contoso", "Widgets", host=HOST, timeout=1.0)
                self.assertEqual(azure_client._project_id_cache, {})


class TestProjectClosedStates(AzureReadTestCase):
    """Which state names mean "not open" -- read from the process template.

    This is the single most consequential read in the module: the open-item WIQL
    filter is built from it, so getting it wrong shows closed work items in the
    triage list (or hides open ones), with no error anywhere.
    """

    def test_the_names_are_the_union_over_every_work_item_type(self):
        # A WIQL query spans types, so a state that closes a Task but not a Bug
        # still has to be excluded when the query returns both.
        az = _Az(
            workitemtypes={
                "value": [
                    {"name": "Bug", "states": _BUG_STATES},
                    {"name": "Task", "states": _TASK_STATES},
                ]
            }
        )
        with mock.patch.object(azure_client, "_az_invoke", az):
            states = azure_client._project_closed_states(
                "contoso", "Widgets", host=HOST, timeout=1.0
            )
        self.assertEqual(states, frozenset({"Closed", "Shipped", "Removed"}))

    def test_states_are_selected_by_category_not_by_name(self):
        """A custom template's closing state is found without knowing its name.

        Categories (Proposed / InProgress / Resolved / Completed / Removed) are the
        layer Azure guarantees across templates; names are not. Asserted with names
        chosen to defeat a name-based match: the closing state is called
        "Marinated" and an OPEN state is called "Closed".
        """
        az = _Az(
            workitemtypes={
                "value": [
                    {
                        "name": "Widget",
                        "states": [
                            {"name": "Closed", "category": "InProgress"},
                            {"name": "Marinated", "category": "Completed"},
                            {"name": "Resolved", "category": "Resolved"},
                        ],
                    }
                ]
            }
        )
        with mock.patch.object(azure_client, "_az_invoke", az):
            states = azure_client._project_closed_states(
                "contoso", "Widgets", host=HOST, timeout=1.0
            )
        self.assertEqual(states, frozenset({"Marinated"}))
        # ``Resolved`` is deliberately NOT a closing category: on the Agile template
        # a resolved item is still open work awaiting verification.
        self.assertNotIn("Resolved", states)
        self.assertNotIn("Closed", states)

    def test_an_unreadable_template_falls_back_instead_of_treating_all_as_open(self):
        # An older server or a caller without process read access must not turn
        # every closed item into an open one, which is what an empty set would do.
        az = _Az(workitemtypes=ProviderCliError("process definitions are unreadable"))
        with mock.patch.object(azure_client, "_az_invoke", az):
            states = azure_client._project_closed_states(
                "contoso", "Widgets", host=HOST, timeout=1.0
            )
        # Asserted as non-empty AND containing the names Microsoft's own templates
        # use, not merely as "equal to the fallback constant" -- that comparison
        # would agree with an empty fallback, which is the failure being guarded.
        self.assertTrue(states, "an empty filter would report every closed item as open")
        self.assertIn("Closed", states)
        self.assertIn("Done", states)
        self.assertEqual(states, azure_client._FALLBACK_CLOSED_STATES)

    def test_a_template_that_names_no_closing_state_also_falls_back(self):
        # An empty answer is indistinguishable from an unreadable one in effect, so
        # it takes the same path rather than producing an empty filter.
        payload: dict
        for payload in ({"value": []}, {"value": [{"name": "Bug", "states": []}]}):
            with self.subTest(payload=payload):
                az = _Az(workitemtypes=payload)
                with mock.patch.object(azure_client, "_az_invoke", az):
                    states = azure_client._project_closed_states(
                        "contoso", "Widgets", host=HOST, timeout=1.0
                    )
                self.assertTrue(states)
                self.assertEqual(states, azure_client._FALLBACK_CLOSED_STATES)

    def test_the_fallback_set_spans_the_templates_microsoft_ships(self):
        # It is the UNION on purpose: a wrong answer here shows a closed item as
        # open, so over-inclusion is the safer direction.
        self.assertEqual(
            azure_client._FALLBACK_CLOSED_STATES,
            frozenset({"Closed", "Done", "Completed", "Removed", "Resolved"}),
        )

    def test_a_single_types_states_are_cached_per_type(self):
        # ``_closed_state_names`` is read once per detail-pane open, so the cache
        # keeps a triage session from re-reading the template on every click.
        az = _Az(states={"value": _BUG_STATES})
        with mock.patch.object(azure_client, "_az_invoke", az):
            first = azure_client._closed_state_names(
                "contoso", "Widgets", "Bug", host=HOST, timeout=1.0
            )
            second = azure_client._closed_state_names(
                "contoso", "Widgets", "Bug", host=HOST, timeout=1.0
            )
        self.assertEqual(first, frozenset({"Closed"}))
        self.assertEqual(second, first)
        self.assertEqual(len(az.calls), 1)
        self.assertEqual(
            az.for_resource("states")[0]["route"], {"project": "Widgets", "type": "Bug"}
        )

    def test_an_unreadable_type_falls_back_without_caching_the_fallback(self):
        """The fallback must not become sticky.

        Caching it would keep serving guessed state names after the transient
        failure cleared, for the life of the process.
        """
        az = _Az(states=ProviderCliError("states unreadable"))
        with mock.patch.object(azure_client, "_az_invoke", az):
            states = azure_client._closed_state_names(
                "contoso", "Widgets", "Bug", host=HOST, timeout=1.0
            )
        self.assertTrue(states, "an empty filter would report every closed item as open")
        self.assertEqual(states, azure_client._FALLBACK_CLOSED_STATES)
        self.assertEqual(azure_client._closed_states_cache, {})


class TestCurrentIdentity(AzureReadTestCase):
    """ "Who am I" has two possible answers depending on how the session was made.

    An ``az login`` ARM session answers on ``connectionData``; a PAT session
    established with ``az devops login`` answers on the profile route. The GUID is
    what a review vote and auto-complete arming are addressed by, so failing to
    resolve it has to raise rather than return an empty login.
    """

    GUID = "11111111-2222-3333-4444-555555555555"

    def test_connection_data_is_tried_first_and_unwrapped(self):
        az = _Az(
            connectionData={
                "authenticatedUser": {"id": self.GUID, "uniqueName": "ada@contoso.com"},
            }
        )
        with mock.patch.object(azure_client, "_az_invoke", az):
            identity = azure_client._current_identity("contoso", host=HOST, timeout=1.0)
        self.assertEqual(identity, {"id": self.GUID, "login": "ada@contoso.com"})
        # The second candidate is not consulted once the first answers.
        self.assertEqual(az.resources, ["connectionData"])

    def test_the_profile_route_is_the_fallback_when_connection_data_fails(self):
        # A PAT session has no ARM identity, so the first candidate legitimately
        # errors and must be treated as a miss rather than as a failure.
        az = _Az(
            connectionData=ProviderCliError("connectionData is not available"),
            profiles={"id": self.GUID, "emailAddress": "grace@contoso.com"},
        )
        with mock.patch.object(azure_client, "_az_invoke", az):
            identity = azure_client._current_identity("contoso", host=HOST, timeout=1.0)
        self.assertEqual(identity, {"id": self.GUID, "login": "grace@contoso.com"})
        self.assertEqual(az.resources, ["connectionData", "profiles"])
        self.assertEqual(az.for_resource("profiles")[0]["route"], {"id": "me"})

    def test_a_candidate_that_answers_without_a_guid_is_a_miss_not_an_answer(self):
        # The GUID is the field callers need; a payload carrying only a login is
        # not usable, so the next candidate has to be tried.
        az = _Az(
            connectionData={"authenticatedUser": {"uniqueName": "ada@contoso.com"}},
            profiles={"id": self.GUID, "uniqueName": "ada@contoso.com"},
        )
        with mock.patch.object(azure_client, "_az_invoke", az):
            identity = azure_client._current_identity("contoso", host=HOST, timeout=1.0)
        self.assertEqual(identity["id"], self.GUID)
        self.assertEqual(az.resources, ["connectionData", "profiles"])

    def test_not_authenticated_is_final_and_stops_the_candidate_walk(self):
        """A setup failure must not be retried as if it were a candidate miss.

        ``ProviderSetupError`` is what the connect dialog turns into install or
        login instructions. Swallowing it and trying the next route would replace
        that actionable reason with a generic "could not resolve the identity".
        """
        az = _Az(
            connectionData=ProviderSetupError(
                "az devops login required", reason="not_authenticated"
            ),
            profiles={"id": self.GUID},
        )
        with mock.patch.object(azure_client, "_az_invoke", az):
            with self.assertRaises(ProviderSetupError) as caught:
                azure_client._current_identity("contoso", host=HOST, timeout=1.0)
        self.assertEqual(caught.exception.reason, "not_authenticated")
        self.assertEqual(az.resources, ["connectionData"])

    def test_the_last_error_is_reported_when_no_candidate_answers(self):
        # The caller needs the provider's own reason, not a summary of it.
        az = _Az(
            connectionData=ProviderCliError("first candidate failed"),
            profiles=ProviderCliError("profile route failed"),
        )
        with mock.patch.object(azure_client, "_az_invoke", az):
            with self.assertRaises(ProviderCliError) as caught:
                azure_client._current_identity("contoso", host=HOST, timeout=1.0)
        self.assertIn("profile route failed", str(caught.exception))

    def test_a_guid_without_any_name_resolves_with_a_null_login(self):
        # A service identity may carry no name at all. The GUID is the part callers
        # need, so this resolves rather than raising.
        az = _Az(connectionData={"authenticatedUser": {"id": self.GUID}})
        with mock.patch.object(azure_client, "_az_invoke", az):
            identity = azure_client._current_identity("contoso", host=HOST, timeout=1.0)
        self.assertEqual(identity, {"id": self.GUID, "login": None})

    def test_the_identity_is_cached_per_organization(self):
        az = _Az(connectionData={"authenticatedUser": {"id": self.GUID, "uniqueName": "ada"}})
        with mock.patch.object(azure_client, "_az_invoke", az):
            azure_client._current_identity("contoso", host=HOST, timeout=1.0)
            azure_client._current_identity("contoso", host=HOST, timeout=1.0)
        self.assertEqual(len(az.calls), 1)


class TestHydrateWorkItems(AzureReadTestCase):
    """The batch hydrate is chunked at a hard id cap and stitched back into order.

    The endpoint answers 400 above :data:`_BATCH_MAX_IDS` ids, and the order it
    returns rows in is not the order they were asked for, so both halves are
    load-bearing: the chunking keeps a large project from failing outright, and the
    stitch is what makes the list view's "newest first" true.
    """

    def _hydrate(self, ids, rows):
        az = _Az(workitemsbatch=_batch(rows))
        with mock.patch.object(azure_client, "_az_invoke", az):
            out = azure_client._hydrate_work_items(
                "contoso", "Widgets", ids, host=HOST, timeout=1.0
            )
        return out, az

    def test_an_empty_id_list_makes_no_call_at_all(self):
        # An empty WIQL result is the common case on a quiet project; a POST with
        # no ids is a wasted round trip and a 400 on some api versions.
        az = _Az()
        with mock.patch.object(azure_client, "_az_invoke", az):
            self.assertEqual(
                azure_client._hydrate_work_items("contoso", "Widgets", [], host=HOST, timeout=1.0),
                [],
            )
        self.assertEqual(az.calls, [])

    def test_ids_are_chunked_at_the_documented_batch_cap(self):
        """The chunk size is asserted as the LITERAL documented cap, not as the
        module's own constant -- reading the constant here would make the test
        agree with any value the module happened to hold, including one the
        endpoint answers 400 for.
        """
        self.assertEqual(azure_client._BATCH_MAX_IDS, 200)
        ids = list(range(1, 251))
        rows = [_item(i) for i in ids]
        out, az = self._hydrate(ids, rows)
        self.assertEqual(len(out), len(ids))
        chunks = [call["body"]["ids"] for call in az.for_resource("workitemsbatch")]
        self.assertEqual([len(chunk) for chunk in chunks], [200, 50])
        # No id is dropped and none is asked for twice -- a chunker that slipped an
        # index would do one or the other.
        self.assertEqual([i for chunk in chunks for i in chunk], ids)

    def test_the_result_follows_the_requested_order_not_the_response_order(self):
        """WIQL's order is the list view's order, so the stitch must restore it.

        The fake answers in the order asked, so this is asserted with a REVERSED
        request: 43 before 42 is not an order any id-sorted response would produce.
        """
        ids = [43, 42, 44]
        out, _ = self._hydrate(ids, [_item(42), _item(43), _item(44)])
        self.assertEqual([row["id"] for row in out], ids)

    def test_an_id_the_server_does_not_return_is_dropped_not_faked(self):
        # A work item deleted between the WIQL query and the hydrate simply is not
        # there; a placeholder row would render as an untitled item.
        out, _ = self._hydrate([42, 99, 44], [_item(42), _item(44)])
        self.assertEqual([row["id"] for row in out], [42, 44])

    def test_the_hydrate_is_a_post_to_the_batch_resource_with_the_field_list(self):
        # The field list is what keeps a mature project's custom fields out of every
        # list refresh, so it is part of the contract, not an optimization.
        _, az = self._hydrate([42], [_item(42)])
        call = az.for_resource("workitemsbatch")[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["route"], {"project": "Widgets"})
        self.assertEqual(call["body"]["fields"], list(azure_client._WORK_ITEM_FIELDS))
        for name in ("System.Title", "System.State", "System.Tags", "System.ChangedDate"):
            self.assertIn(name, call["body"]["fields"])


class TestSingleWorkItemRead(AzureReadTestCase):
    """A single work item read that comes back empty must raise, not answer blank."""

    def test_a_missing_work_item_raises_rather_than_returning_an_empty_row(self):
        # An empty row would normalize into a titled-nothing item with state
        # "open", which is worse than an error: the pane would render it.
        az = _Az(workitemsbatch={"value": []})
        with mock.patch.object(azure_client, "_az_invoke", az):
            with self.assertRaises(ProviderCliError) as caught:
                azure_client._work_item("contoso", "Widgets", 42, host=HOST, timeout=1.0)
        self.assertIn("42", str(caught.exception))

    def test_a_batch_answering_a_different_id_is_also_a_miss(self):
        # The stitch keys on the requested id, so a response about another item
        # cannot be mistaken for the one asked for.
        az = _Az(workitemsbatch={"value": [_item(7)]})
        with mock.patch.object(azure_client, "_az_invoke", az):
            with self.assertRaises(ProviderCliError):
                azure_client._work_item("contoso", "Widgets", 42, host=HOST, timeout=1.0)


class TestGetIssueDetail(AzureReadTestCase):
    """The detail pane's shape, key for key with ``_ISSUE_DETAIL_JQ``."""

    def _detail(self, **over):
        az = _Az(workitemsbatch=_batch([_item(42, **over)]), states={"value": _BUG_STATES})
        with mock.patch.object(azure_client, "_az_invoke", az):
            detail = azure_client.get_issue_detail(OWNER, REPO, 42, host=HOST, timeout=1.0)
        return detail, az

    def test_the_full_key_set_is_present(self):
        """Asserted as an exact key SET, not key by key.

        The pane reads these unconditionally, so a renamed or forgotten key is a
        blank field rather than an error -- and a key-by-key test would never
        notice one going missing.
        """
        detail, _ = self._detail()
        self.assertEqual(
            set(detail),
            {
                "number",
                "title",
                "body",
                "state",
                "state_reason",
                "url",
                "author",
                "author_association",
                "created_at",
                "updated_at",
                "closed_at",
                "closed_by",
                "comments",
                "locked",
                "labels",
                "assignees",
                "milestone",
                "reactions",
            },
        )

    def test_state_reason_is_always_none(self):
        """Azure's System.Reason does not map onto GitHub's two-valued reason.

        "Fixed", "Duplicate", "As Designed" are process-template values, and the UI
        renders this field as a verdict on WHY an item closed. Asserted on a closed
        item carrying a Reason, which is where a well-meant mapping would appear.
        """
        detail, _ = self._detail(
            **{
                "System.State": "Closed",
                "System.Reason": "Duplicate",
            }
        )
        self.assertEqual(detail["state"], "closed")
        self.assertIsNone(detail["state_reason"])
        self.assertIsNone(detail["author_association"])

    def test_the_iteration_path_becomes_the_milestone_leaf(self):
        # Azure's iteration is a backslash-delimited path. The UI has room for a
        # milestone NAME, so the leaf is what a triage reader needs; the state and
        # due date genuinely do not exist and are reported as absent.
        detail, _ = self._detail()
        self.assertEqual(detail["milestone"], {"title": "Sprint 4", "state": None, "due_on": None})

    def test_an_item_in_no_iteration_has_no_milestone(self):
        # ``None``, not an empty-titled object: the UI branches on the object's
        # presence to decide whether to render the chip at all.
        detail, _ = self._detail(**{"System.IterationPath": ""})
        self.assertIsNone(detail["milestone"])

    def test_reactions_are_unknown_here_rather_than_zero(self):
        # The detail shape carries a reaction OBJECT on GitHub, and Azure has none,
        # so ``None`` is the honest answer -- as against the list row's counts,
        # which are truly zero.
        detail, _ = self._detail()
        self.assertIsNone(detail["reactions"])

    def test_a_work_item_has_no_discussion_lock(self):
        detail, _ = self._detail()
        self.assertIs(detail["locked"], False)

    def test_labels_are_shaped_objects_with_the_synthetic_colour(self):
        detail, _ = self._detail()
        self.assertEqual(
            detail["labels"],
            [
                {
                    "name": "needs-triage",
                    "color": azure_client._SYNTHETIC_LABEL_COLOR,
                    "description": "",
                },
                {
                    "name": "blocked",
                    "color": azure_client._SYNTHETIC_LABEL_COLOR,
                    "description": "",
                },
            ],
        )

    def test_close_metadata_is_read_only_for_a_closed_item(self):
        detail, _ = self._detail(
            **{
                "System.State": "Closed",
                "Microsoft.VSTS.Common.ClosedDate": "2026-03-03T00:00:00Z",
                "Microsoft.VSTS.Common.ClosedBy": {"uniqueName": "grace@contoso.com"},
            }
        )
        self.assertEqual(detail["closed_at"], "2026-03-03T00:00:00Z")
        self.assertEqual(detail["closed_by"], "grace@contoso.com")

    def test_an_open_item_carrying_a_stale_closed_date_reports_neither(self):
        """Reopening does not clear ClosedDate, so it must be gated on the state.

        Otherwise a reopened item shows a close timestamp and a closer while
        reading as open -- two fields contradicting each other in one pane.
        """
        detail, _ = self._detail(
            **{
                "System.State": "Active",
                "Microsoft.VSTS.Common.ClosedDate": "2026-03-03T00:00:00Z",
                "Microsoft.VSTS.Common.ClosedBy": {"uniqueName": "grace@contoso.com"},
            }
        )
        self.assertEqual(detail["state"], "open")
        self.assertIsNone(detail["closed_at"])
        self.assertIsNone(detail["closed_by"])

    def test_a_closed_item_without_a_close_date_does_not_borrow_the_change_date(self):
        # Only some templates define ClosedDate. Substituting System.ChangedDate
        # would date the close to whatever happened most recently -- a comment, a
        # tag edit -- and that reads as fact in the pane.
        detail, _ = self._detail(**{"System.State": "Closed"})
        self.assertEqual(detail["state"], "closed")
        self.assertIsNone(detail["closed_at"])
        self.assertNotEqual(detail["closed_at"], detail["updated_at"])

    def test_the_states_are_read_for_the_items_own_type(self):
        # Per-type states are narrower than the project union, so a state that
        # closes a Task must not close a Bug in the pane.
        _, az = self._detail()
        self.assertEqual(
            az.for_resource("states")[0]["route"], {"project": "Widgets", "type": "Bug"}
        )

    def test_an_item_with_no_type_falls_back_to_the_project_wide_union(self):
        # There is no type to ask about, and treating everything as open would be
        # the wrong direction, so the union is used instead.
        az = _Az(
            workitemsbatch=_batch(
                [
                    _item(
                        42,
                        **{
                            "System.WorkItemType": None,
                            "System.State": "Shipped",
                        },
                    )
                ]
            ),
            workitemtypes={
                "value": [
                    {"name": "Bug", "states": _BUG_STATES},
                    {"name": "Task", "states": _TASK_STATES},
                ]
            },
        )
        with mock.patch.object(azure_client, "_az_invoke", az):
            detail = azure_client.get_issue_detail(OWNER, REPO, 42, host=HOST, timeout=1.0)
        self.assertEqual(detail["state"], "closed")
        self.assertIn("workitemtypes", az.resources)
        self.assertNotIn("states", az.resources)

    def test_the_number_is_coerced_and_the_repository_is_ignored(self):
        # A work item is addressed by project and id; the repo argument exists for
        # signature parity with the other clients and must not reach a route. The
        # id is passed as a STRING on purpose -- the coercion exists for a caller
        # that ignores the annotation (a route handing over a path parameter), so
        # the runtime behaviour is what is being pinned.
        az = _Az(workitemsbatch=_batch([_item(42)]), states={"value": _BUG_STATES})
        with mock.patch.object(azure_client, "_az_invoke", az):
            azure_client.get_issue_detail(
                OWNER, REPO, "42", host=HOST, timeout=1.0  # type: ignore[arg-type]
            )
        for call in az.calls:
            self.assertNotIn(REPO, str(call.get("route") or {}))
        self.assertEqual(az.for_resource("workitemsbatch")[0]["body"]["ids"], [42])


class TestGetRefSummary(AzureReadTestCase):
    """The hover card for ``#123`` -- work items ONLY, deliberately.

    GitHub shares one number sequence between issues and pull requests, so its
    caller needs ``is_pr`` to learn which it got. Azure allocates the two from
    independent services, so ``#5`` and ``!5`` are unrelated items and a fallback
    to the pull request endpoint would describe the wrong one under the number the
    user pointed at.
    """

    def _summary(self, **over):
        az = _Az(workitemsbatch=_batch([_item(42, **over)]), states={"value": _BUG_STATES})
        with mock.patch.object(azure_client, "_az_invoke", az):
            summary = azure_client.get_ref_summary(OWNER, REPO, 42, host=HOST, timeout=1.0)
        return summary, az

    def test_the_summary_always_reports_a_work_item(self):
        summary, _ = self._summary()
        self.assertIs(summary["is_pr"], False)
        self.assertIs(summary["draft"], False)
        self.assertIsNone(summary["merged_at"])
        self.assertIsNone(summary["state_reason"])
        self.assertIsNone(summary["author_association"])

    def test_the_rendered_fields_come_from_the_work_item(self):
        summary, _ = self._summary()
        self.assertEqual(summary["number"], 42)
        self.assertEqual(summary["title"], "Ledger totals drift")
        self.assertEqual(summary["state"], "open")
        self.assertEqual(summary["author"], "ada@contoso.com")
        self.assertEqual(summary["comments"], 3)
        self.assertEqual(summary["url"], "https://dev.azure.com/contoso/Widgets/_workitems/edit/42")
        self.assertEqual(
            summary["labels"],
            [
                {"name": "needs-triage", "color": azure_client._SYNTHETIC_LABEL_COLOR},
                {"name": "blocked", "color": azure_client._SYNTHETIC_LABEL_COLOR},
            ],
        )

    def test_a_missing_work_item_raises_and_never_asks_the_pr_endpoint(self):
        """The refusal is what keeps ``#5`` from being answered by ``!5``.

        Asserted on the CALLS, not only on the exception: a fallback would still
        raise if the pull request were absent too, so the test that matters is that
        no pull request read was attempted at all.
        """
        az = _Az(workitemsbatch={"value": []})
        with mock.patch.object(azure_client, "_az_invoke", az):
            with self.assertRaises(ProviderCliError):
                azure_client.get_ref_summary(OWNER, REPO, 5, host=HOST, timeout=1.0)
        self.assertEqual(az.resources, ["workitemsbatch"])
        self.assertNotIn("pullrequests", az.resources)

    def test_a_closed_item_reports_its_close_time(self):
        summary, _ = self._summary(
            **{
                "System.State": "Closed",
                "Microsoft.VSTS.Common.ClosedDate": "2026-03-03T00:00:00Z",
            }
        )
        self.assertEqual(summary["state"], "closed")
        self.assertEqual(summary["closed_at"], "2026-03-03T00:00:00Z")

    def test_an_open_item_reports_no_close_time(self):
        summary, _ = self._summary(
            **{
                "Microsoft.VSTS.Common.ClosedDate": "2026-03-03T00:00:00Z",
            }
        )
        self.assertIsNone(summary["closed_at"])


class TestWorkItemListViews(AzureReadTestCase):
    """The four listings, all of which are WIQL-then-hydrate and project-scoped."""

    def _az(self, ids=(42, 43), rows=None):
        rows = rows if rows is not None else [_item(42), _item(43)]
        return _Az(
            workitemtypes={
                "value": [
                    {"name": "Bug", "states": _BUG_STATES},
                    {"name": "Task", "states": _TASK_STATES},
                ]
            },
            wiql={"workItems": [{"id": i} for i in ids]},
            workitemsbatch=_batch(rows),
        )

    def _query(self, az):
        body = az.for_resource("wiql")[0]["body"]
        assert isinstance(body, dict)
        return str(body["query"])

    def test_the_open_list_returns_normalized_rows_in_query_order(self):
        az = self._az(ids=(43, 42), rows=[_item(42), _item(43)])
        with mock.patch.object(azure_client, "_az_invoke", az):
            rows = azure_client.list_open_issues(OWNER, REPO, host=HOST, timeout=1.0)
        self.assertEqual([row["number"] for row in rows], [43, 42])
        self.assertEqual(rows[0]["state"], "open")
        self.assertEqual(rows[0]["labels"], ["needs-triage", "blocked"])

    def test_the_open_list_filters_on_state_and_orders_by_last_change(self):
        az = self._az()
        with mock.patch.object(azure_client, "_az_invoke", az):
            azure_client.list_open_issues(OWNER, REPO, host=HOST, timeout=1.0)
        query = self._query(az)
        self.assertIn("NOT IN", query)
        self.assertIn("'Shipped'", query)  # the project's own name, not a builtin
        self.assertIn("ORDER BY [System.ChangedDate] DESC", query)
        # Filtering on work item TYPE would return nothing on a Scrum project,
        # which has no "Issue" type at all.
        self.assertNotIn("System.WorkItemType", query)

    def test_every_list_read_is_addressed_by_project_alone(self):
        # A work item has no repository dimension, so two repositories connected
        # from one project legitimately return the same list.
        az = self._az()
        with mock.patch.object(azure_client, "_az_invoke", az):
            first = azure_client.list_open_issues(OWNER, REPO, host=HOST, timeout=1.0)
            second = azure_client.list_open_issues(OWNER, "another-repo", host=HOST, timeout=1.0)
        self.assertEqual(first, second)
        for call in az.calls:
            self.assertNotIn("repositoryId", dict(call.get("route") or {}))

    def test_the_first_page_asks_for_a_page_and_the_full_list_for_the_ceiling(self):
        """Same query, different ``$top`` -- the progressive-paint contract.

        The first paint has to be the LEADING rows of the full list, in the same
        order, or the full set appends behind it with visible reordering.
        """
        page_az = self._az()
        full_az = self._az()
        with mock.patch.object(azure_client, "_az_invoke", page_az):
            page = azure_client.list_open_issues_first_page(OWNER, REPO, host=HOST, timeout=1.0)
        with mock.patch.object(azure_client, "_az_invoke", full_az):
            full = azure_client.list_open_issues(OWNER, REPO, host=HOST, timeout=1.0)
        self.assertEqual(page, full)
        self.assertEqual(self._query(page_az), self._query(full_az))
        self.assertEqual(
            page_az.for_resource("wiql")[0]["query"], {"$top": azure_client._PAGE_SIZE}
        )
        self.assertEqual(full_az.for_resource("wiql")[0]["query"], {"$top": azure_client._WIQL_TOP})

    def test_the_closed_list_inverts_the_state_filter_and_reports_closed_rows(self):
        az = self._az(rows=[_item(42, **{"System.State": "Shipped"})], ids=(42,))
        with mock.patch.object(azure_client, "_az_invoke", az):
            rows = azure_client.list_closed_issues(OWNER, REPO, host=HOST, timeout=1.0)
        query = self._query(az)
        self.assertNotIn("NOT IN", query)
        self.assertIn("[System.State] IN (", query)
        self.assertIn("ORDER BY [System.ChangedDate] DESC", query)
        self.assertEqual([row["state"] for row in rows], ["closed"])

    def test_the_closed_list_is_one_page(self):
        # It backs a "recently closed" strip, not a working set, so it is bounded
        # like GitHub's single-page closed read rather than at the WIQL ceiling.
        az = self._az(ids=(42,), rows=[_item(42, **{"System.State": "Closed"})])
        with mock.patch.object(azure_client, "_az_invoke", az):
            azure_client.list_closed_issues(OWNER, REPO, host=HOST, timeout=1.0)
        self.assertEqual(az.for_resource("wiql")[0]["query"], {"$top": azure_client._PAGE_SIZE})

    def test_the_watchers_poll_orders_by_creation_not_by_last_change(self):
        """The watcher notifies on NEW items, so an old item that just got a
        comment must not sort to the top and be announced as new."""
        az = self._az()
        with mock.patch.object(azure_client, "_az_invoke", az):
            azure_client.list_recent_open_issues(OWNER, REPO, 5, host=HOST, timeout=1.0)
        query = self._query(az)
        self.assertIn("ORDER BY [System.CreatedDate] DESC", query)
        self.assertNotIn("System.ChangedDate", query)
        self.assertEqual(az.for_resource("wiql")[0]["query"], {"$top": 5})

    def test_the_watchers_limit_is_clamped_at_both_ends(self):
        for asked, expected in ((0, 1), (-5, 1), (10_000, azure_client._PAGE_SIZE)):
            with self.subTest(asked=asked):
                az = self._az()
                with mock.patch.object(azure_client, "_az_invoke", az):
                    azure_client.list_recent_open_issues(OWNER, REPO, asked, host=HOST, timeout=1.0)
                self.assertEqual(az.for_resource("wiql")[0]["query"], {"$top": expected})

    def test_the_watcher_never_returns_more_rows_than_asked_for(self):
        # WIQL's $top bounds the ids, but the server is not the only source of
        # truth here: the slice is what the caller's contract rests on.
        az = self._az(ids=(42, 43), rows=[_item(42), _item(43)])
        with mock.patch.object(azure_client, "_az_invoke", az):
            rows = azure_client.list_recent_open_issues(OWNER, REPO, 1, host=HOST, timeout=1.0)
        self.assertEqual(len(rows), 1)

    def test_a_wiql_result_with_no_ids_skips_the_hydrate_entirely(self):
        az = _Az(
            workitemtypes={"value": [{"name": "Bug", "states": _BUG_STATES}]},
            wiql={"workItems": []},
        )
        with mock.patch.object(azure_client, "_az_invoke", az):
            self.assertEqual(azure_client.list_open_issues(OWNER, REPO, host=HOST, timeout=1.0), [])
        self.assertNotIn("workitemsbatch", az.resources)

    def test_a_non_integer_id_in_the_wiql_result_is_ignored(self):
        # The id goes on to address a route, so a string or null must not travel.
        az = self._az(ids=(42,))
        az.handlers["wiql"] = {"workItems": [{"id": "42"}, {"id": None}, {}, {"id": 42}]}
        with mock.patch.object(azure_client, "_az_invoke", az):
            rows = azure_client.list_open_issues(OWNER, REPO, host=HOST, timeout=1.0)
        self.assertEqual([row["number"] for row in rows], [42])
        self.assertEqual(az.for_resource("workitemsbatch")[0]["body"]["ids"], [42])

    def test_an_owner_that_names_no_project_is_refused_before_any_call(self):
        # Defaulting the project to the organization would read a DIFFERENT
        # project's work items, which looks like a working list view.
        az = _Az()
        with mock.patch.object(azure_client, "_az_invoke", az):
            with self.assertRaises(ProviderCliError):
                azure_client.list_open_issues("contoso", REPO, host=HOST, timeout=1.0)
        self.assertEqual(az.calls, [])


class TestRepoLabels(AzureReadTestCase):
    """Project tags, in the app's label shape, with an admitted-synthetic colour."""

    def test_tags_are_shaped_as_labels_with_the_neutral_colour(self):
        az = _Az(tags={"value": [{"name": "needs-triage"}, {"name": "blocked"}]})
        with mock.patch.object(azure_client, "_az_invoke", az):
            labels = azure_client.list_repo_labels(OWNER, REPO, host=HOST, timeout=1.0)
        self.assertEqual(
            labels,
            [
                {"name": "needs-triage", "color": "888888", "description": ""},
                {"name": "blocked", "color": "888888", "description": ""},
            ],
        )
        # The colour is synthesized, not read: an Azure work-item tag has none, and
        # the value is the same neutral default the other clients fall back to.
        self.assertEqual(azure_client._SYNTHETIC_LABEL_COLOR, "888888")

    def test_a_nameless_tag_row_is_dropped(self):
        # ``name`` is the identity the label filter keys on, so a row without one
        # would render as an unselectable blank chip.
        az = _Az(tags={"value": [{"name": "bug"}, {"name": ""}, {"id": "x"}]})
        with mock.patch.object(azure_client, "_az_invoke", az):
            labels = azure_client.list_repo_labels(OWNER, REPO, host=HOST, timeout=1.0)
        self.assertEqual([label["name"] for label in labels], ["bug"])

    def test_tags_are_read_per_project_and_ignore_the_repository(self):
        az = _Az(tags={"value": []})
        with mock.patch.object(azure_client, "_az_invoke", az):
            self.assertEqual(azure_client.list_repo_labels(OWNER, REPO, host=HOST, timeout=1.0), [])
        self.assertEqual(az.for_resource("tags")[0]["route"], {"project": "Widgets"})


class TestRepoCollaborators(AzureReadTestCase):
    """The member roster: project TEAMS walked and their members unioned.

    Azure has no repository-level collaborator list, and this roster is what the
    write gate consults, so both the union and the 403 behaviour are contracts.
    """

    TEAM_A = "aaaaaaaa-1111-2222-3333-444444444444"
    TEAM_B = "bbbbbbbb-1111-2222-3333-444444444444"

    def _roster(self, teams, members_by_team):
        def members(kwargs):
            route = dict(kwargs.get("route") or {})
            return {"value": members_by_team.get(str(route.get("teamId")), [])}

        az = _Az(teams={"value": teams}, members=members)
        with mock.patch.object(azure_client, "_az_invoke", az):
            rows = azure_client.list_repo_collaborators(OWNER, REPO, host=HOST, timeout=1.0)
        return rows, az

    def test_members_across_teams_are_unioned_into_one_roster(self):
        rows, _ = self._roster(
            [{"id": self.TEAM_A}, {"id": self.TEAM_B}],
            {
                self.TEAM_A: [{"identity": {"uniqueName": "ada@contoso.com"}}],
                self.TEAM_B: [{"identity": {"uniqueName": "grace@contoso.com"}}],
            },
        )
        self.assertEqual(
            sorted(rows, key=lambda row: row["login"]),
            [
                {"login": "ada@contoso.com", "role_name": "write"},
                {"login": "grace@contoso.com", "role_name": "write"},
            ],
        )

    def test_a_person_on_two_teams_appears_once(self):
        rows, _ = self._roster(
            [{"id": self.TEAM_A}, {"id": self.TEAM_B}],
            {
                self.TEAM_A: [{"identity": {"uniqueName": "ada@contoso.com"}}],
                self.TEAM_B: [{"identity": {"uniqueName": "ada@contoso.com"}}],
            },
        )
        self.assertEqual(rows, [{"login": "ada@contoso.com", "role_name": "write"}])

    def test_team_admin_outranks_plain_membership_in_either_order(self):
        """Admin must win regardless of which team is walked first.

        The union is built by iteration, so a later plain membership must not
        downgrade an admin already recorded -- the ordering bug that a
        single-ordering test would miss.
        """
        for order in ((self.TEAM_A, self.TEAM_B), (self.TEAM_B, self.TEAM_A)):
            with self.subTest(order=order):
                rows, _ = self._roster(
                    [{"id": order[0]}, {"id": order[1]}],
                    {
                        self.TEAM_A: [
                            {"identity": {"uniqueName": "ada@contoso.com"}, "isTeamAdmin": True}
                        ],
                        self.TEAM_B: [{"identity": {"uniqueName": "ada@contoso.com"}}],
                    },
                )
                self.assertEqual(rows, [{"login": "ada@contoso.com", "role_name": "admin"}])

    def test_a_flat_member_row_is_read_too(self):
        # Some api versions inline the identity rather than nesting it under
        # ``identity``; both shapes carry the same person.
        rows, _ = self._roster(
            [{"id": self.TEAM_A}], {self.TEAM_A: [{"uniqueName": "ada@contoso.com"}]}
        )
        self.assertEqual(rows, [{"login": "ada@contoso.com", "role_name": "write"}])

    def test_a_member_with_no_resolvable_login_is_dropped(self):
        # The login is what the gate compares the caller against, so a nameless row
        # can only add noise.
        rows, _ = self._roster([{"id": self.TEAM_A}], {self.TEAM_A: [{"identity": {}}, {}]})
        self.assertEqual(rows, [])

    def test_a_team_whose_id_is_not_a_guid_is_never_addressed(self):
        # The team id is interpolated into a route parameter, so it is validated
        # before it can travel rather than being sent and rejected upstream.
        az = _Az(teams={"value": [{"id": "not-a-guid"}, {"name": "no id"}]})
        with mock.patch.object(azure_client, "_az_invoke", az):
            self.assertEqual(
                azure_client.list_repo_collaborators(OWNER, REPO, host=HOST, timeout=1.0), []
            )
        self.assertNotIn("members", az.resources)

    def test_a_forbidden_team_list_raises_the_permission_error(self):
        """A 403 must stay distinguishable from a generic failure.

        The members route degrades to the issue-derived roster on
        ``ProviderPermissionError`` and returns 502 on anything else, so
        collapsing the two changes what the user sees.
        """
        az = _Az(teams=ProviderPermissionError("teams are forbidden"))
        with mock.patch.object(azure_client, "_az_invoke", az):
            with self.assertRaises(ProviderPermissionError):
                azure_client.list_repo_collaborators(OWNER, REPO, host=HOST, timeout=1.0)


class TestDerivedRoster(unittest.TestCase):
    def test_the_fallback_roster_is_empty_on_azure(self):
        """Deriving members from work item authors would badge non-members.

        GitHub's fallback reads ``author_association``, which Azure does not report
        in any form, so there is nothing to derive from -- and the caller only
        reaches this when the real roster was forbidden, i.e. exactly when a
        fabricated one would be trusted.
        """
        issues: list[dict] = [
            {"author": "ada@contoso.com", "author_association": None},
            {"author": "grace@contoso.com", "author_association": "MEMBER"},
        ]
        self.assertEqual(azure_client.derive_members(issues), [])


class TestGetCurrentLogin(AzureReadTestCase):
    """``get_current_login`` answers ``None`` rather than raising -- gitlab's contract."""

    GUID = "11111111-2222-3333-4444-555555555555"

    def test_the_login_is_resolved_through_the_configured_default_organization(self):
        # Every Azure route is organization-scoped, including "who am I", so the
        # CLI's own default organization is the only session-derived answer.
        az = _Az(
            connectionData={
                "authenticatedUser": {"id": self.GUID, "uniqueName": "ada@contoso.com"},
            }
        )
        with mock.patch.object(azure_client, "_az_invoke", az):
            with mock.patch.object(azure_client, "_default_organization", return_value="contoso"):
                self.assertEqual(
                    azure_client.get_current_login(host=HOST, timeout=1.0), "ada@contoso.com"
                )

    def test_an_unconfigured_organization_answers_none_rather_than_raising(self):
        # Callers treat this as "unknown user" and carry on; raising would fail a
        # page that only wanted to badge "you".
        with mock.patch.object(
            azure_client,
            "_default_organization",
            side_effect=ProviderCliError("no default organization is configured"),
        ):
            self.assertIsNone(azure_client.get_current_login(host=HOST, timeout=1.0))

    def test_an_unresolvable_identity_also_answers_none(self):
        az = _Az(
            connectionData=ProviderCliError("nope"),
            profiles=ProviderCliError("nope either"),
        )
        with mock.patch.object(azure_client, "_az_invoke", az):
            with mock.patch.object(azure_client, "_default_organization", return_value="contoso"):
                self.assertIsNone(azure_client.get_current_login(host=HOST, timeout=1.0))


class TestListContributedRepos(AzureReadTestCase):
    """The connect picker's rows: every repository in the session's organization."""

    def _list(self, projects, repos_by_project):
        def repositories(kwargs):
            route = dict(kwargs.get("route") or {})
            handler = repos_by_project.get(str(route.get("project")))
            if isinstance(handler, BaseException):
                raise handler
            return {"value": handler or []}

        az = _Az(projects={"value": projects}, repositories=repositories)
        with mock.patch.object(azure_client, "_az_invoke", az):
            with mock.patch.object(azure_client, "_default_organization", return_value="contoso"):
                rows, truncated = azure_client.list_contributed_repos("ada", host=HOST, timeout=1.0)
        return rows, truncated, az

    def test_a_row_carries_the_picker_contract(self):
        rows, truncated, _ = self._list(
            [{"name": "Widgets", "visibility": "private", "description": "the widget project"}],
            {"Widgets": [{"name": "widget-service"}]},
        )
        self.assertEqual(
            rows,
            [
                {
                    "owner": "contoso/Widgets",
                    "repo": "widget-service",
                    "full_name": "contoso/Widgets/widget-service",
                    # No activity timestamp exists in Azure's repository payload, so the
                    # window is reported as unapplied rather than approximated from a
                    # different date.
                    "pushed_at": None,
                    "private": True,
                    "description": "the widget project",
                }
            ],
        )
        self.assertFalse(truncated)

    def test_visibility_decides_private_and_defaults_to_private(self):
        rows, _, _ = self._list(
            [
                {"name": "Public", "visibility": "public"},
                {"name": "Private", "visibility": "private"},
                {"name": "Unstated"},
            ],
            {"Public": [{"name": "a"}], "Private": [{"name": "b"}], "Unstated": [{"name": "c"}]},
        )
        self.assertEqual(
            {row["repo"]: row["private"] for row in rows}, {"a": False, "b": True, "c": True}
        )

    def test_a_project_whose_code_is_forbidden_is_skipped_and_flags_truncation(self):
        """One unreachable project must not fail the whole picker -- but the user
        has to be told the list is incomplete, or a missing repository looks like
        it does not exist."""
        rows, truncated, _ = self._list(
            [{"name": "Widgets"}, {"name": "Secret"}],
            {
                "Widgets": [{"name": "widget-service"}],
                "Secret": ProviderPermissionError("code is out of reach"),
            },
        )
        self.assertEqual([row["repo"] for row in rows], ["widget-service"])
        self.assertTrue(truncated)

    def test_a_non_permission_failure_is_not_swallowed(self):
        # A network failure is not "you cannot see this project", and reporting it
        # as a truncated list would hide a broken session behind a short picker.
        with self.assertRaises(ProviderCliError):
            self._list(
                [{"name": "Widgets"}],
                {"Widgets": ProviderCliError("the git endpoint is unreachable")},
            )

    def test_a_disabled_repository_is_not_offered(self):
        # A disabled repository cannot be read, so connecting to it would produce a
        # repo entry whose every refresh fails.
        rows, _, _ = self._list(
            [{"name": "Widgets"}],
            {"Widgets": [{"name": "live"}, {"name": "archived", "isDisabled": True}]},
        )
        self.assertEqual([row["repo"] for row in rows], ["live"])

    def test_an_unusable_name_is_skipped_on_either_axis(self):
        # These names would become part of a cache path and of a route parameter,
        # and a picker row the user cannot connect is worse than an absent one.
        rows, _, az = self._list(
            [{"name": "Widgets"}, {"name": "_apis"}, {"name": "bad/name"}, {"name": ""}],
            {"Widgets": [{"name": "widget-service"}, {"name": "bad/name"}, {"name": ""}]},
        )
        self.assertEqual([row["repo"] for row in rows], ["widget-service"])
        # The refused projects are never even asked about.
        asked = [
            dict(call["route"] or {}).get("project") for call in az.for_resource("repositories")
        ]
        self.assertEqual(asked, ["Widgets"])

    def test_a_project_list_at_the_page_ceiling_reports_truncation(self):
        """The walk is bounded, so a very large organization is a partial answer.

        Reporting it as complete would tell the user a repository does not exist
        when the walk simply stopped.
        """
        ceiling = azure_client._MAX_PAGES * azure_client._PAGE_SIZE
        with mock.patch.object(azure_client, "_az_invoke_paged", return_value=[{}] * ceiling):
            with mock.patch.object(azure_client, "_default_organization", return_value="contoso"):
                with mock.patch.object(azure_client, "_az_invoke", _Az()):
                    rows, truncated = azure_client.list_contributed_repos(
                        "ada", host=HOST, timeout=1.0
                    )
        self.assertEqual(rows, [])
        self.assertTrue(truncated)


class TestWorkItemTimeline(AzureReadTestCase):
    """Comments plus the revision log, normalized into GitHub's event vocabulary.

    Azure writes a revision for EVERY field write, including ones no pane renders,
    so the filtering is what makes the timeline readable rather than a changelog.
    """

    def _timeline(self, comments=(), updates=(), states=None):
        az = _Az(
            comments={"comments": list(comments)},
            updates={"value": list(updates)},
            workitemtypes={"value": [{"name": "Bug", "states": states or _BUG_STATES}]},
        )
        with mock.patch.object(azure_client, "_az_invoke", az):
            events = azure_client.list_issue_timeline(OWNER, REPO, 42, host=HOST, timeout=1.0)
        return events, az

    def test_a_comment_carries_the_id_and_modified_time_the_claim_protocol_needs(self):
        """The crew claim protocol keeps ONE comment as its public ledger.

        It addresses that comment by id to rewrite it, and proves the claim is
        alive from the MODIFIED time -- ``created_at`` on an edited comment is
        still the original post time, so the two cannot be collapsed.
        """
        events, _ = self._timeline(
            comments=[
                {
                    "id": 909,
                    "createdBy": {"uniqueName": "ada@contoso.com"},
                    "createdDate": "2026-01-01T00:00:00Z",
                    "modifiedDate": "2026-01-05T00:00:00Z",
                    "text": "claiming this",
                }
            ]
        )
        self.assertEqual(
            events,
            [
                {
                    "kind": "comment",
                    "id": 909,
                    "actor": "ada@contoso.com",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-05T00:00:00Z",
                    "body": "claiming this",
                    "author_association": None,
                    "reactions": None,
                }
            ],
        )

    def test_an_unedited_comment_reports_its_creation_time_as_the_update_time(self):
        # ``updated_at`` must always be populated: the protocol compares it against
        # a staleness ceiling, and a null would read as infinitely old.
        events, _ = self._timeline(
            comments=[
                {
                    "id": 1,
                    "createdDate": "2026-01-01T00:00:00Z",
                    "text": "hi",
                }
            ]
        )
        self.assertEqual(events[0]["updated_at"], "2026-01-01T00:00:00Z")

    def test_a_tag_change_becomes_labeled_and_unlabeled_events(self):
        events, _ = self._timeline(
            updates=[
                {
                    "revisedBy": {"uniqueName": "ada@contoso.com"},
                    "revisedDate": "2026-01-02T00:00:00Z",
                    "fields": {
                        "System.Tags": {"oldValue": "bug; stale", "newValue": "bug; urgent"}
                    },
                }
            ]
        )
        self.assertEqual(
            [(e["kind"], e["label"]["name"]) for e in events],
            [("labeled", "urgent"), ("unlabeled", "stale")],
        )
        self.assertEqual(events[0]["actor"], "ada@contoso.com")
        self.assertEqual(events[0]["label"]["color"], azure_client._SYNTHETIC_LABEL_COLOR)

    def test_reordering_the_tag_field_produces_no_events(self):
        """Azure records the whole delimited string, not per-tag events.

        Order within the field is not meaningful, so a diff of the raw strings
        would invent an add and a remove for every tag on any tag write.
        """
        events, _ = self._timeline(
            updates=[
                {
                    "fields": {"System.Tags": {"oldValue": "a; b", "newValue": "b; a"}},
                }
            ]
        )
        self.assertEqual(events, [])

    def test_a_state_move_into_a_closing_state_is_a_close(self):
        events, _ = self._timeline(
            updates=[
                {
                    "revisedBy": {"uniqueName": "grace@contoso.com"},
                    "revisedDate": "2026-01-03T00:00:00Z",
                    "fields": {"System.State": {"oldValue": "Active", "newValue": "Closed"}},
                }
            ]
        )
        self.assertEqual(
            events,
            [
                {
                    "kind": "closed",
                    "actor": "grace@contoso.com",
                    "created_at": "2026-01-03T00:00:00Z",
                    # System.Reason has no GitHub equivalent -- see _norm_issue_detail.
                    "state_reason": None,
                    "commit_id": None,
                }
            ],
        )

    def test_a_state_move_out_of_a_closing_state_is_a_reopen(self):
        events, _ = self._timeline(
            updates=[
                {
                    "fields": {"System.State": {"oldValue": "Closed", "newValue": "Active"}},
                }
            ]
        )
        self.assertEqual([e["kind"] for e in events], ["reopened"])

    def test_workflow_churn_between_two_open_states_is_not_an_event(self):
        # New -> Active is not a close, a reopen, or anything the pane renders, and
        # on a busy item there are many of them.
        events, _ = self._timeline(
            updates=[
                {
                    "fields": {"System.State": {"oldValue": "New", "newValue": "Active"}},
                }
            ]
        )
        self.assertEqual(events, [])

    def test_a_state_rename_to_the_same_value_is_not_an_event(self):
        events, _ = self._timeline(
            updates=[
                {
                    "fields": {"System.State": {"oldValue": "Closed", "newValue": "Closed"}},
                }
            ]
        )
        self.assertEqual(events, [])

    def test_a_reassignment_emits_the_unassign_before_the_assign(self):
        # Both halves are one Azure field write, and the pane reads them as a
        # sequence, so a lone "assigned" would leave the previous assignee showing.
        events, _ = self._timeline(
            updates=[
                {
                    "fields": {
                        "System.AssignedTo": {
                            "oldValue": {"uniqueName": "ada@contoso.com"},
                            "newValue": {"uniqueName": "grace@contoso.com"},
                        }
                    },
                }
            ]
        )
        self.assertEqual(
            [(e["kind"], e["assignee"]) for e in events],
            [("unassigned", "ada@contoso.com"), ("assigned", "grace@contoso.com")],
        )

    def test_a_first_assignment_emits_only_the_assign(self):
        events, _ = self._timeline(
            updates=[
                {
                    "fields": {
                        "System.AssignedTo": {"newValue": {"uniqueName": "ada@contoso.com"}}
                    },
                }
            ]
        )
        self.assertEqual(
            [(e["kind"], e["assignee"]) for e in events], [("assigned", "ada@contoso.com")]
        )

    def test_clearing_the_assignee_emits_only_the_unassign(self):
        events, _ = self._timeline(
            updates=[
                {
                    "fields": {
                        "System.AssignedTo": {
                            "oldValue": {"uniqueName": "ada@contoso.com"},
                            "newValue": None,
                        }
                    },
                }
            ]
        )
        self.assertEqual(
            [(e["kind"], e["assignee"]) for e in events], [("unassigned", "ada@contoso.com")]
        )

    def test_a_title_change_becomes_a_rename_carrying_both_sides(self):
        events, _ = self._timeline(
            updates=[
                {
                    "fields": {"System.Title": {"oldValue": "Old title", "newValue": "New title"}},
                }
            ]
        )
        self.assertEqual([e["kind"] for e in events], ["renamed"])
        self.assertEqual(events[0]["rename"], {"from": "Old title", "to": "New title"})

    def test_an_iteration_change_becomes_a_milestone_leaf(self):
        events, _ = self._timeline(
            updates=[
                {
                    "fields": {
                        "System.IterationPath": {
                            "oldValue": "Widgets\\Sprint 3",
                            "newValue": "Widgets\\Release 2\\Sprint 4",
                        }
                    },
                }
            ]
        )
        self.assertEqual([e["kind"] for e in events], ["milestoned"])
        self.assertEqual(events[0]["milestone"], "Sprint 4")

    def test_a_revision_touching_no_rendered_field_is_dropped(self):
        """The system bookkeeping every write produces must not reach the pane.

        Azure stamps System.Rev, System.ChangedDate, System.AuthorizedDate and
        friends on every revision, so a timeline that does not filter shows several
        contentless entries per real change -- and the pane cannot render a raw
        field name anyway.
        """
        events, _ = self._timeline(
            updates=[
                {
                    "id": 2,
                    "fields": {
                        "System.Rev": {"oldValue": 1, "newValue": 2},
                        "System.ChangedDate": {"newValue": "2026-01-02T00:00:00Z"},
                        "System.AuthorizedDate": {"newValue": "2026-01-02T00:00:00Z"},
                    },
                },
                {"id": 3, "fields": {"System.Description": {"newValue": "edited body"}}},
                {"id": 4, "fields": {}},
                {"id": 5},
            ]
        )
        self.assertEqual(events, [])

    def test_the_change_date_of_the_revision_is_preferred_over_the_revised_date(self):
        # ``revisedDate`` is when Azure wrote the revision; System.ChangedDate is
        # when the change happened, which is what the pane's ordering means.
        events, _ = self._timeline(
            updates=[
                {
                    "revisedDate": "2026-09-09T00:00:00Z",
                    "fields": {
                        "System.ChangedDate": {"newValue": "2026-01-02T00:00:00Z"},
                        "System.State": {"oldValue": "Active", "newValue": "Closed"},
                    },
                }
            ]
        )
        self.assertEqual(events[0]["created_at"], "2026-01-02T00:00:00Z")

    def test_a_revision_without_a_change_date_falls_back_to_the_revised_date(self):
        events, _ = self._timeline(
            updates=[
                {
                    "revisedDate": "2026-09-09T00:00:00Z",
                    "fields": {"System.State": {"oldValue": "Active", "newValue": "Closed"}},
                }
            ]
        )
        self.assertEqual(events[0]["created_at"], "2026-09-09T00:00:00Z")

    def test_comments_and_events_are_merged_chronologically(self):
        # The two come from different endpoints, so neither stream's own order is
        # the pane's order.
        events, _ = self._timeline(
            comments=[
                {"id": 1, "createdDate": "2026-01-05T00:00:00Z", "text": "later comment"},
                {"id": 2, "createdDate": "2026-01-01T00:00:00Z", "text": "first comment"},
            ],
            updates=[
                {
                    "revisedDate": "2026-01-03T00:00:00Z",
                    "fields": {"System.State": {"oldValue": "New", "newValue": "Closed"}},
                }
            ],
        )
        self.assertEqual(
            [(e["kind"], e["created_at"]) for e in events],
            [
                ("comment", "2026-01-01T00:00:00Z"),
                ("closed", "2026-01-03T00:00:00Z"),
                ("comment", "2026-01-05T00:00:00Z"),
            ],
        )

    def test_the_comment_read_is_bounded_and_ascending(self):
        events, az = self._timeline(comments=[{"id": 1, "createdDate": "x", "text": "t"}])
        self.assertEqual(len(events), 1)
        self.assertEqual(az.for_resource("comments")[0]["query"], {"$top": 200, "order": "asc"})
        self.assertEqual(
            az.for_resource("comments")[0]["route"], {"project": "Widgets", "workItemId": 42}
        )

    def test_an_unreadable_revision_log_still_serves_the_comments(self):
        """The comments are the substance; the revision log is enrichment.

        On a long-lived work item the updates stream can be far larger than the
        comment list, so failing the whole pane on it would lose the discussion to
        a timeout on the secondary read.
        """
        az = _Az(
            comments={"comments": [{"id": 1, "createdDate": "2026-01-01T00:00:00Z", "text": "hi"}]},
            updates=ProviderCliError("the updates stream timed out"),
            workitemtypes={"value": [{"name": "Bug", "states": _BUG_STATES}]},
        )
        with mock.patch.object(azure_client, "_az_invoke", az):
            events = azure_client.list_issue_timeline(OWNER, REPO, 42, host=HOST, timeout=1.0)
        self.assertEqual([e["kind"] for e in events], ["comment"])

    def test_the_close_event_uses_the_projects_own_closing_names(self):
        # Same indirection as the list view: a template whose closing state is
        # called "Marinated" must still produce a close event.
        events, _ = self._timeline(
            updates=[
                {
                    "fields": {"System.State": {"oldValue": "Active", "newValue": "Marinated"}},
                }
            ],
            states=[
                {"name": "Active", "category": "InProgress"},
                {"name": "Marinated", "category": "Completed"},
            ],
        )
        self.assertEqual([e["kind"] for e in events], ["closed"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
