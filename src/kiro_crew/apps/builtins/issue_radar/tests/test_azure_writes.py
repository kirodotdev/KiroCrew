"""Azure DevOps WORK ITEM WRITE path -- the calls that change remote state.

``test_azure.py`` covers the refusals that protect the tag field (a name carrying
the delimiter, a write with no ``/rev`` guard, a tag Azure cannot create). This
file covers what happens when a write is ALLOWED to proceed, which is the half
where a mistake is silent rather than loud:

  * ``System.Tags`` is one delimited STRING, so every tag edit is a
    read-modify-write of the whole field. Parsing and re-emitting that string is
    the only thing standing between "add one tag" and "rewrite every tag on the
    item", so the round trip is asserted directly -- including the whitespace
    Azure pads its separator with and the CASE it preserves, since ``bug`` and
    ``Bug`` are two different tags on this provider.
  * A write that changes nothing must not be sent. Azure records a revision for
    every patch, so an idempotent ``add_issue_labels`` that patched anyway would
    fill the revision log (and therefore the timeline, which is reconstructed from
    it) with entries for edits that did not happen. Every no-op case here asserts
    that NO patch was attempted rather than only that the return value looks
    right.
  * Work item writes are JSON-Patch, and Azure rejects the request outright when
    the content type says otherwise, so the media type is part of the contract and
    is asserted rather than assumed.
  * Comments are two different services. A work item comment and a pull request
    comment take independent id sequences, and a PR comment is a THREAD, so the
    two functions are checked to address different resources with the same number.

Like ``test_azure.py``, nothing here reaches the network or needs the ``az`` CLI:
every test patches ``azure_client._az_invoke``, the single point every REST call
funnels through.
"""

from __future__ import annotations

import unittest
from unittest import mock

from kiro_crew.apps.builtins.issue_radar.backend import azure_client
from kiro_crew.apps.builtins.issue_radar.backend.errors import (
    ProviderCliError,
    ProviderInvalidInputError,
)

OWNER = "contoso/Widgets"
PROJECT = "Widgets"
ORG = "contoso"
REPO = "widget-service"
HOST = "dev.azure.com"
NUMBER = 5
TEAM_GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ADA_GUID = "12345678-1234-1234-1234-123456789abc"
ADA = {"id": ADA_GUID, "uniqueName": "ada@contoso.com", "displayName": "Ada Lovelace"}
GRACE = {
    "id": "87654321-4321-4321-4321-cba987654321",
    "uniqueName": "grace@contoso.com",
    "displayName": "Grace Hopper",
}


def _identity_of(member: dict) -> dict:
    """The identity reference inside a team-member row, or the row itself.

    Azure nests the identity under ``identity`` on some member payloads and
    inlines it on others, and the client accepts both -- so the fake carries both
    shapes rather than normalizing one away.
    """
    nested = member.get("identity")
    return nested if isinstance(nested, dict) else member


class FakeAzure:
    """A recording stand-in for ``azure_client._az_invoke``.

    Answers each resource with the shape Azure actually uses -- a list endpoint
    answers ``{"value": [...]}``, hydration is a POST to ``workitemsbatch``, and a
    work item write is a PATCH whose body is a list of JSON-Patch operations -- and
    keeps every call's kwargs so a test can assert on the request as well as on
    the return value.

    The PATCH reply ECHOES the fields the patch wrote, which is what Azure does
    (it answers with the updated work item). A test that needs to prove the caller
    reports Azure's answer rather than its own intent overrides that with
    ``echo_tags``.
    """

    def __init__(
        self,
        *,
        tags: str = "",
        rev: object = 7,
        item_type: str = "Bug",
        states: list[dict] | None = None,
        echo_tags: str | None = None,
        comment: dict | None = None,
        thread: dict | None = None,
        members: list[dict] | None = None,
        teams: object = None,
    ) -> None:
        self.tags = tags
        self.rev = rev
        self.item_type = item_type
        self.states = (
            states
            if states is not None
            else [
                {"name": "New", "category": "Proposed"},
                {"name": "Active", "category": "InProgress"},
                {"name": "Closed", "category": "Completed"},
            ]
        )
        self.echo_tags = echo_tags
        self.members = members if members is not None else []
        self.teams = teams
        self.comment = comment or {"id": 91, "createdDate": "2026-02-03T04:05:06Z"}
        self.thread = thread or {
            "id": 700,
            "comments": [{"id": 701, "publishedDate": "2026-02-03T04:05:06Z"}],
        }
        self.calls: list[dict] = []

    # -- convenience views over the recorded calls ---------------------------
    @property
    def patches(self) -> list[list[dict]]:
        """The JSON-Patch document of every work item PATCH, in order."""
        out: list[list[dict]] = []
        for call in self.calls:
            if call.get("method") == "PATCH" and call.get("resource") == "workitems":
                body = call.get("body")
                assert isinstance(body, list), f"expected a patch list, got {body!r}"
                out.append(body)
        return out

    def calls_to(self, resource: str) -> list[dict]:
        return [call for call in self.calls if call.get("resource") == resource]

    # -- the fake itself -----------------------------------------------------
    def __call__(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        resource = str(kwargs.get("resource") or "")
        method = str(kwargs.get("method") or "GET")

        if resource == "workitemsbatch":
            item: dict = {
                "id": NUMBER,
                "fields": {
                    "System.Tags": self.tags,
                    "System.Title": "a work item",
                    "System.State": "New",
                },
            }
            if self.item_type:
                item["fields"]["System.WorkItemType"] = self.item_type
            if self.rev is not None:
                item["rev"] = self.rev
            return {"value": [item]}

        if resource == "workitems" and method == "PATCH":
            body = kwargs.get("body")
            assert isinstance(body, list)
            written: dict = {}
            for op in body:
                path = str(op.get("path") or "")
                if path.startswith("/fields/"):
                    written[path[len("/fields/") :]] = op.get("value")
            if self.echo_tags is not None and "System.Tags" in written:
                written["System.Tags"] = self.echo_tags
            if "System.AssignedTo" in written:
                # Azure answers a PATCH with the UPDATED work item, where
                # System.AssignedTo is an identity REFERENCE object, not the string
                # the patch sent. A fake that echoed the string back would let a
                # caller that never parses the identity pass.
                wrote = str(written["System.AssignedTo"] or "")
                match = next(
                    (
                        _identity_of(m)
                        for m in self.members
                        if (_identity_of(m).get("uniqueName") or "") == wrote
                    ),
                    None,
                )
                if match is None:
                    written.pop("System.AssignedTo")
                else:
                    written["System.AssignedTo"] = dict(match)
            return {"id": NUMBER, "rev": 8, "fields": written}

        if resource == "teams":
            if callable(self.teams):
                return self.teams(kwargs)
            return {"value": [{"id": TEAM_GUID}]}

        if resource == "members":
            return {"value": [dict(m) for m in self.members]}

        if resource == "states":
            return {"value": list(self.states)}

        if resource == "comments":
            return dict(self.comment)

        if resource == "pullRequestThreads":
            return dict(self.thread)

        raise AssertionError(f"unexpected resource: {resource!r} ({method})")


class AzureWriteCase(unittest.TestCase):
    """Base case: the per-type closing-state cache is process-global.

    ``_closed_state_names`` memoizes on ``(org, project, type)``, so a value
    another test left behind would decide this test's open/closed answer. Cleared
    both ways round so this file neither reads nor leaks that state.
    """

    def setUp(self) -> None:
        azure_client._closed_states_cache.clear()
        self.addCleanup(azure_client._closed_states_cache.clear)

    def patched(self, fake: FakeAzure):
        return mock.patch.object(azure_client, "_az_invoke", side_effect=fake)


class TestTagNameAcceptance(unittest.TestCase):
    """``_check_label`` on the names it must ACCEPT, and what it returns."""

    def test_a_normal_name_is_returned_unchanged(self):
        for name in ("needs-triage", "blocked", "area/backend", "p1", "needs triage"):
            with self.subTest(name=name):
                self.assertEqual(azure_client._check_label(name), name)

    def test_surrounding_whitespace_is_stripped(self):
        # Azure pads its own separator with a space, so a name read back out of the
        # field arrives with whitespace. Returning it unstripped would make
        # "blocked" and " blocked" compare unequal and add a second tag that looks
        # identical in the UI.
        self.assertEqual(azure_client._check_label("  blocked  "), "blocked")
        self.assertEqual(azure_client._check_label("\tblocked\n"), "blocked")

    def test_case_is_preserved_and_two_spellings_stay_distinct(self):
        # Azure tags are case sensitive, so folding here would silently retarget a
        # write at a different tag.
        self.assertEqual(azure_client._check_label("Needs-Triage"), "Needs-Triage")
        self.assertNotEqual(azure_client._check_label("Bug"), azure_client._check_label("bug"))

    def test_the_length_limit_is_a_ceiling_not_a_range(self):
        # The refusal above it is covered elsewhere; what matters here is that a
        # name AT the limit is not refused, since Azure accepts it.
        self.assertEqual(azure_client._check_label("x" * 400), "x" * 400)

    def test_a_whitespace_only_name_is_refused(self):
        # It strips to empty, so it would write an empty tag rather than the name
        # the caller believes they asked for.
        for bad in ("   ", "\t", "\n"):
            with self.subTest(bad=bad), self.assertRaises(ProviderCliError):
                azure_client._check_label(bad)


class TestTagFieldRoundTrip(unittest.TestCase):
    """``System.Tags`` is ONE delimited string, so reading it is a parse.

    Every tag edit rewrites the whole field, which means a parse that loses or
    mangles a name deletes somebody's tag. The properties asserted here are what
    make the read-modify-write safe.
    """

    def test_azures_padded_separator_is_absorbed(self):
        # Azure normalizes the separator to "; " on write, so the value read back
        # carries whitespace that is formatting, not part of the name.
        self.assertEqual(
            azure_client._tag_names("needs-triage; blocked; Bug"),
            ["needs-triage", "blocked", "Bug"],
        )
        self.assertEqual(
            azure_client._tag_names("  needs-triage ;blocked  ;  Bug  "),
            ["needs-triage", "blocked", "Bug"],
        )

    def test_an_absent_or_empty_field_is_no_tags_not_one_empty_tag(self):
        # An untagged work item omits the field entirely on some reads and carries
        # "" on others; a one-element [""] would be written straight back as a
        # phantom tag.
        for raw in (None, "", "   ", ";", " ; ; "):
            with self.subTest(raw=raw):
                self.assertEqual(azure_client._tag_names(raw), [])

    def test_case_is_preserved_through_the_parse(self):
        self.assertEqual(azure_client._tag_names("Bug; bug; BUG"), ["Bug", "bug", "BUG"])

    def test_names_survive_a_full_round_trip(self):
        # The property the read-modify-write depends on: what comes back out is
        # exactly what went in, so re-writing an unchanged set is a no-op.
        for names in (
            [],
            ["blocked"],
            ["needs-triage", "blocked", "Bug"],
            ["needs triage", "area/backend", "P1"],
        ):
            with self.subTest(names=names):
                self.assertEqual(azure_client._tag_names(azure_client._tags_field(names)), names)

    def test_an_empty_set_serializes_to_an_empty_field(self):
        # Clearing the last tag has to produce "", not "; " -- the latter parses
        # back as no tags but is a different stored value, so the field would keep
        # changing on every write.
        self.assertEqual(azure_client._tags_field([]), "")


class TestPatchWorkItemRequest(AzureWriteCase):
    """The request shape a work item write must have.

    Azure serves work item writes only as JSON-Patch and rejects the request when
    the content type says otherwise, so the media type is a contract rather than a
    detail. The route is asserted with it because a work item is addressed by
    PROJECT and id, with no repository dimension at all.
    """

    def test_the_write_is_json_patch_on_the_project_scoped_work_item_route(self):
        fake = FakeAzure(tags="bug")
        with self.patched(fake):
            azure_client._patch_work_item(
                ORG,
                PROJECT,
                NUMBER,
                [{"op": "add", "path": "/fields/System.Tags", "value": "bug; urgent"}],
                host=HOST,
                timeout=5.0,
            )
        call = self.assertOneCall(fake, "workitems")
        self.assertEqual(call["method"], "PATCH")
        self.assertEqual(call["area"], "wit")
        self.assertEqual(call["media_type"], "application/json-patch+json")
        self.assertEqual(call["route"], {"project": PROJECT, "id": NUMBER})
        self.assertEqual(call["org"], ORG)
        self.assertEqual(call["host"], HOST)
        self.assertIsInstance(call["body"], list)

    def test_the_media_type_is_set_on_the_writes_callers_actually_use(self):
        # _patch_work_item is the only place it is set, so a future caller that
        # built its own invoke would fail at Azure rather than here. Both public
        # write paths are checked through it.
        for label, act in (
            (
                "add_issue_labels",
                lambda: azure_client.add_issue_labels(OWNER, REPO, NUMBER, ["urgent"], host=HOST),
            ),
            (
                "set_issue_state",
                lambda: azure_client.set_issue_state(OWNER, REPO, NUMBER, "closed", host=HOST),
            ),
        ):
            fake = FakeAzure(tags="bug")
            with self.subTest(write=label), self.patched(fake):
                act()
            call = self.assertOneCall(fake, "workitems")
            self.assertEqual(call["media_type"], "application/json-patch+json")

    def test_the_response_is_returned_as_an_object_not_a_bare_value(self):
        # Callers index into .get("fields"); a non-dict answer must degrade to {}
        # rather than raising an AttributeError deep in a write path.
        with mock.patch.object(azure_client, "_az_invoke", return_value=["not", "a", "dict"]):
            self.assertEqual(
                azure_client._patch_work_item(ORG, PROJECT, NUMBER, [], host=HOST, timeout=5.0),
                {},
            )

    def assertOneCall(self, fake: FakeAzure, resource: str) -> dict:
        calls = fake.calls_to(resource)
        self.assertEqual(len(calls), 1, f"expected one {resource} call, got {len(calls)}")
        return calls[0]


class TestAddIssueLabels(AzureWriteCase):
    """Adding tags is a merge, and a merge that changes nothing must not write."""

    def test_a_new_tag_is_appended_to_the_existing_set(self):
        fake = FakeAzure(tags="bug; stale")
        with self.patched(fake):
            out = azure_client.add_issue_labels(OWNER, REPO, NUMBER, ["urgent"], host=HOST)
        # The existing tags survive, in order, with the addition at the end: the
        # write replaces the WHOLE field, so a reordering or a drop here is a
        # silent edit to tags the caller never mentioned.
        self.assertEqual([row["name"] for row in out], ["bug", "stale", "urgent"])
        self.assertEqual(len(fake.patches), 1)
        self.assertEqual(
            fake.patches[0][-1],
            {"op": "add", "path": "/fields/System.Tags", "value": "bug; stale; urgent"},
        )

    def test_the_shaped_rows_carry_the_synthetic_colour_and_no_description(self):
        # An Azure tag has neither, and the palette reads both keys unconditionally.
        fake = FakeAzure(tags="")
        with self.patched(fake):
            out = azure_client.add_issue_labels(OWNER, REPO, NUMBER, ["urgent"], host=HOST)
        self.assertEqual(out, [{"name": "urgent", "color": "888888", "description": ""}])

    def test_a_tag_the_item_already_carries_writes_nothing(self):
        """The idempotent case: no patch at all, and the current set is returned.

        Azure allocates a revision per patch and the timeline is reconstructed from
        that revision log, so writing an unchanged field would post "labeled"
        churn for an edit that did not happen.
        """
        fake = FakeAzure(tags="bug; stale")
        with self.patched(fake):
            out = azure_client.add_issue_labels(OWNER, REPO, NUMBER, ["stale"], host=HOST)
        self.assertEqual([row["name"] for row in out], ["bug", "stale"])
        self.assertEqual(fake.patches, [], "an unchanged tag set must not be written")

    def test_an_empty_addition_list_writes_nothing(self):
        fake = FakeAzure(tags="bug")
        with self.patched(fake):
            out = azure_client.add_issue_labels(OWNER, REPO, NUMBER, [], host=HOST)
        self.assertEqual([row["name"] for row in out], ["bug"])
        self.assertEqual(fake.patches, [])

    def test_a_name_repeated_within_one_call_is_added_once(self):
        fake = FakeAzure(tags="")
        with self.patched(fake):
            azure_client.add_issue_labels(OWNER, REPO, NUMBER, ["urgent", "urgent"], host=HOST)
        self.assertEqual(fake.patches[0][-1]["value"], "urgent", "a duplicate was written twice")

    def test_a_case_variant_is_a_different_tag_and_is_added(self):
        # Azure compares tags case sensitively, so treating "Bug" as already
        # present would drop an addition the user asked for.
        fake = FakeAzure(tags="bug")
        with self.patched(fake):
            out = azure_client.add_issue_labels(OWNER, REPO, NUMBER, ["Bug"], host=HOST)
        self.assertEqual(fake.patches[0][-1]["value"], "bug; Bug")
        self.assertEqual([row["name"] for row in out], ["bug", "Bug"])

    def test_the_result_is_what_azure_stored_not_what_was_requested(self):
        """The returned set comes from the PATCH response, not from the merge.

        The write is a full-field replace, so if Azure normalized or rejected part
        of it the caller's cache must reflect Azure's value -- otherwise the UI
        shows a tag the item does not have until the next refresh.
        """
        fake = FakeAzure(tags="bug", echo_tags="bug; urgent; added-elsewhere")
        with self.patched(fake):
            out = azure_client.add_issue_labels(OWNER, REPO, NUMBER, ["urgent"], host=HOST)
        self.assertEqual([row["name"] for row in out], ["bug", "urgent", "added-elsewhere"])

    def test_the_read_precedes_the_write(self):
        # The merge is computed from a fresh read rather than a cached row, which is
        # the only thing that keeps a concurrent addition from being clobbered
        # outright (the /rev test then catches the remaining window).
        fake = FakeAzure(tags="bug")
        with self.patched(fake):
            azure_client.add_issue_labels(OWNER, REPO, NUMBER, ["urgent"], host=HOST)
        order = [call["resource"] for call in fake.calls]
        self.assertEqual(order, ["workitemsbatch", "workitems"])


class TestRemoveIssueLabel(AzureWriteCase):
    """Removal returns the REMAINING set, and is a no-op success when absent."""

    def test_the_remaining_tags_are_returned(self):
        fake = FakeAzure(tags="bug; stale; urgent")
        with self.patched(fake):
            out = azure_client.remove_issue_label(OWNER, REPO, NUMBER, "stale", host=HOST)
        assert out is not None
        self.assertEqual([row["name"] for row in out], ["bug", "urgent"])
        self.assertEqual(fake.patches[0][-1]["value"], "bug; urgent")

    def test_removing_the_last_tag_clears_the_field(self):
        fake = FakeAzure(tags="stale")
        with self.patched(fake):
            out = azure_client.remove_issue_label(OWNER, REPO, NUMBER, "stale", host=HOST)
        self.assertEqual(out, [])
        self.assertEqual(fake.patches[0][-1]["value"], "")

    def test_removing_a_tag_the_item_does_not_carry_writes_nothing(self):
        """A no-op SUCCESS: the authoritative set is returned and nothing is sent.

        Reported as success because the caller's intent ("this tag should not be
        on the item") already holds. Never as ``None`` -- callers that handle
        github_client's ``None`` would take a not-found path for a state that is
        correct.
        """
        fake = FakeAzure(tags="bug; urgent")
        with self.patched(fake):
            out = azure_client.remove_issue_label(OWNER, REPO, NUMBER, "never-applied", host=HOST)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual([row["name"] for row in out], ["bug", "urgent"])
        self.assertEqual(fake.patches, [], "a tag that was not present must not be written")

    def test_removal_on_an_untagged_item_writes_nothing(self):
        fake = FakeAzure(tags="")
        with self.patched(fake):
            out = azure_client.remove_issue_label(OWNER, REPO, NUMBER, "stale", host=HOST)
        self.assertEqual(out, [])
        self.assertEqual(fake.patches, [])

    def test_only_the_exact_spelling_is_removed(self):
        # Case-insensitive removal would delete a tag the caller did not name, and
        # the full-field write makes that deletion permanent.
        fake = FakeAzure(tags="Bug; bug")
        with self.patched(fake):
            out = azure_client.remove_issue_label(OWNER, REPO, NUMBER, "bug", host=HOST)
        assert out is not None
        self.assertEqual([row["name"] for row in out], ["Bug"])
        self.assertEqual(fake.patches[0][-1]["value"], "Bug")

    def test_every_occurrence_of_the_named_tag_is_removed(self):
        # A field that somehow holds the same name twice must not be left with one
        # copy, or the next removal would look like it did nothing.
        fake = FakeAzure(tags="bug; stale; bug")
        with self.patched(fake):
            out = azure_client.remove_issue_label(OWNER, REPO, NUMBER, "bug", host=HOST)
        assert out is not None
        self.assertEqual([row["name"] for row in out], ["stale"])


class TestSetIssueState(AzureWriteCase):
    """Closing and reopening write ``System.State`` and report no reason."""

    def test_closing_writes_the_state_field_with_the_types_completed_state(self):
        fake = FakeAzure()
        with self.patched(fake):
            out = azure_client.set_issue_state(OWNER, REPO, NUMBER, "closed", host=HOST)
        self.assertEqual(
            fake.patches,
            [[{"op": "add", "path": "/fields/System.State", "value": "Closed"}]],
        )
        self.assertEqual(out, {"state": "closed", "state_reason": None})

    def test_the_state_name_comes_from_the_process_template_not_a_literal(self):
        # A custom process may call its completed state anything; picking by
        # CATEGORY is what makes this work without knowing the name.
        fake = FakeAzure(
            states=[
                {"name": "To Do", "category": "Proposed"},
                {"name": "Shipped", "category": "Completed"},
            ]
        )
        with self.patched(fake):
            out = azure_client.set_issue_state(OWNER, REPO, NUMBER, "closed", host=HOST)
        self.assertEqual(fake.patches[0][0]["value"], "Shipped")
        self.assertEqual(out["state"], "closed")

    def test_reopening_targets_the_types_own_entry_state(self):
        fake = FakeAzure(
            states=[
                {"name": "To Do", "category": "Proposed"},
                {"name": "Doing", "category": "InProgress"},
                {"name": "Done", "category": "Completed"},
            ]
        )
        with self.patched(fake):
            out = azure_client.set_issue_state(OWNER, REPO, NUMBER, "open", host=HOST)
        self.assertEqual(
            fake.patches,
            [[{"op": "add", "path": "/fields/System.State", "value": "To Do"}]],
        )
        self.assertEqual(out, {"state": "open", "state_reason": None})

    def test_state_reason_is_accepted_ignored_and_never_reported(self):
        """Azure's ``System.Reason`` has no mapping onto GitHub's two values.

        Returning one would report a reason the platform did not record, so the
        answer is ``None`` and the field is not written at all -- asserted on the
        patch, because writing a reason Azure's template does not define would
        fail the whole patch.
        """
        for reason in ("completed", "not_planned", None):
            fake = FakeAzure()
            with self.subTest(reason=reason), self.patched(fake):
                out = azure_client.set_issue_state(OWNER, REPO, NUMBER, "closed", reason, host=HOST)
            self.assertIsNone(out["state_reason"])
            paths = [op["path"] for op in fake.patches[0]]
            self.assertEqual(paths, ["/fields/System.State"])

    def test_the_reported_state_is_read_back_from_what_azure_wrote(self):
        # Not echoed from the request: a template whose Completed state is not in
        # the closed set would otherwise be reported as closed when it is not.
        fake = FakeAzure(
            states=[
                {"name": "New", "category": "Proposed"},
                {"name": "Closed", "category": "Completed"},
            ]
        )
        with self.patched(fake):
            out = azure_client.set_issue_state(OWNER, REPO, NUMBER, "closed", host=HOST)
        self.assertEqual(out["state"], "closed")
        # The read-back consults the type's state definitions, so the state
        # resource is queried after the patch as well as before it.
        self.assertGreaterEqual(len(fake.calls_to("states")), 1)

    def test_the_state_write_carries_no_rev_test(self):
        """Deliberate asymmetry with the tag write, so it is pinned.

        A state write sets ONE field to an absolute value, so a concurrent edit to
        another field is not lost by it. The tag write needs a ``/rev`` guard only
        because it replaces a whole field it just read.
        """
        fake = FakeAzure()
        with self.patched(fake):
            azure_client.set_issue_state(OWNER, REPO, NUMBER, "closed", host=HOST)
        self.assertEqual([op["path"] for op in fake.patches[0]], ["/fields/System.State"])

    def test_an_unknown_state_is_refused_before_anything_is_read(self):
        fake = FakeAzure()
        with self.patched(fake):
            for bad in ("merged", "CLOSED", "", "abandoned"):
                with self.subTest(bad=bad), self.assertRaises(ProviderCliError):
                    azure_client.set_issue_state(OWNER, REPO, NUMBER, bad, host=HOST)
        self.assertEqual(fake.calls, [], "an invalid state must not reach the provider")

    def test_a_work_item_with_no_type_is_refused_before_writing(self):
        # The target state is resolved from the item's own type, so an unreadable
        # type means the state to write is unknown -- guessing "Closed" would fail
        # on a custom process or, worse, land on a state that means something else.
        fake = FakeAzure(item_type="")
        with self.patched(fake):
            with self.assertRaises(ProviderCliError) as caught:
                azure_client.set_issue_state(OWNER, REPO, NUMBER, "closed", host=HOST)
        self.assertIn("work item type", str(caught.exception))
        self.assertEqual(fake.patches, [])

    def test_a_type_with_no_completed_state_is_refused_before_writing(self):
        fake = FakeAzure(states=[{"name": "New", "category": "Proposed"}])
        with self.patched(fake):
            with self.assertRaises(ProviderCliError):
                azure_client.set_issue_state(OWNER, REPO, NUMBER, "closed", host=HOST)
        self.assertEqual(fake.patches, [])

    def test_a_type_with_no_open_state_cannot_be_reopened(self):
        fake = FakeAzure(states=[{"name": "Closed", "category": "Completed"}])
        with self.patched(fake):
            with self.assertRaises(ProviderCliError):
                azure_client.set_issue_state(OWNER, REPO, NUMBER, "open", host=HOST)
        self.assertEqual(fake.patches, [])


class TestAddIssueComment(AzureWriteCase):
    """A work item comment posts to the work item's own comments resource."""

    def test_it_posts_to_the_work_item_comments_resource(self):
        fake = FakeAzure()
        with self.patched(fake):
            azure_client.add_issue_comment(OWNER, REPO, NUMBER, "a note", host=HOST)
        calls = fake.calls_to("comments")
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["area"], "wit")
        # Addressed by project and WORK ITEM id -- no repository, because a work
        # item does not belong to one.
        self.assertEqual(call["route"], {"project": PROJECT, "workItemId": NUMBER})
        self.assertEqual(call["body"], {"text": "a note"})
        # Work item comments are still a PREVIEW resource, so this call's
        # api-version differs from the rest of the wit area.
        self.assertEqual(call["api_version"], azure_client._API_WIT_COMMENTS)
        self.assertNotEqual(call["api_version"], azure_client._API_WIT)

    def test_the_returned_shape_has_no_fabricated_url(self):
        fake = FakeAzure(comment={"id": 91, "createdDate": "2026-02-03T04:05:06Z"})
        with self.patched(fake):
            out = azure_client.add_issue_comment(OWNER, REPO, NUMBER, "a note", host=HOST)
        # ``url`` is None rather than a guessed anchor: a work item comment has no
        # web URL of its own, and the crew protocol needs ``id`` to rewrite its own
        # comment later, so that one is load-bearing.
        self.assertEqual(out, {"id": 91, "url": None, "created_at": "2026-02-03T04:05:06Z"})

    def test_the_body_is_trimmed_before_it_is_sent(self):
        fake = FakeAzure()
        with self.patched(fake):
            azure_client.add_issue_comment(OWNER, REPO, NUMBER, "  a note\n", host=HOST)
        self.assertEqual(fake.calls_to("comments")[0]["body"], {"text": "a note"})

    def test_an_empty_body_is_refused_without_posting(self):
        # An empty comment is a visible artifact on the item that cannot be
        # explained, so it is refused rather than posted as a blank.
        fake = FakeAzure()
        with self.patched(fake):
            for bad in ("", "   ", "\n\t"):
                with self.subTest(bad=bad), self.assertRaises(ProviderCliError):
                    azure_client.add_issue_comment(OWNER, REPO, NUMBER, bad, host=HOST)
        self.assertEqual(fake.calls, [])


class TestAddPrComment(AzureWriteCase):
    """A general PR comment on Azure is a THREAD with no thread context."""

    def test_it_creates_a_thread_carrying_no_thread_context(self):
        fake = FakeAzure()
        with self.patched(fake):
            azure_client.add_pr_comment(OWNER, REPO, 7, "a review note", host=HOST)
        calls = fake.calls_to("pullRequestThreads")
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["area"], "git")
        # A pull request IS repository-scoped, unlike a work item.
        self.assertEqual(
            call["route"],
            {"project": PROJECT, "repositoryId": REPO, "pullRequestId": 7},
        )
        body = call["body"]
        assert isinstance(body, dict)
        # The absence of threadContext is what makes this a general comment rather
        # than an inline diff comment pinned to a file and line.
        self.assertNotIn("threadContext", body)
        self.assertEqual(body["status"], "active")
        self.assertEqual(
            body["comments"],
            [{"parentCommentId": 0, "content": "a review note", "commentType": "text"}],
        )

    def test_the_returned_shape_reads_the_threads_first_comment(self):
        # The thread and the comment carry different ids; the caller needs the
        # COMMENT's, since that is what it would later edit.
        fake = FakeAzure(
            thread={
                "id": 700,
                "publishedDate": "2026-01-01T00:00:00Z",
                "comments": [{"id": 701, "publishedDate": "2026-02-03T04:05:06Z"}],
            }
        )
        with self.patched(fake):
            out = azure_client.add_pr_comment(OWNER, REPO, 7, "a review note", host=HOST)
        self.assertEqual(out, {"id": 701, "url": None, "created_at": "2026-02-03T04:05:06Z"})

    def test_a_thread_with_no_comments_falls_back_to_the_thread_itself(self):
        # Azure has answered with the thread envelope alone; the caller still needs
        # an id and a timestamp rather than two Nones.
        fake = FakeAzure(thread={"id": 700, "publishedDate": "2026-01-01T00:00:00Z"})
        with self.patched(fake):
            out = azure_client.add_pr_comment(OWNER, REPO, 7, "a review note", host=HOST)
        self.assertEqual(out, {"id": 700, "url": None, "created_at": "2026-01-01T00:00:00Z"})

    def test_the_body_is_trimmed_and_an_empty_one_is_refused_without_posting(self):
        fake = FakeAzure()
        with self.patched(fake):
            azure_client.add_pr_comment(OWNER, REPO, 7, "  a note\n", host=HOST)
            for bad in ("", "   ", "\n"):
                with self.subTest(bad=bad), self.assertRaises(ProviderCliError):
                    azure_client.add_pr_comment(OWNER, REPO, 7, bad, host=HOST)
        bodies = [call["body"] for call in fake.calls_to("pullRequestThreads")]
        self.assertEqual(len(bodies), 1, "an empty body must not create a thread")
        assert isinstance(bodies[0], dict)
        self.assertEqual(bodies[0]["comments"][0]["content"], "a note")

    def test_the_same_number_addresses_two_different_services(self):
        """Why these are two functions rather than one with a kind flag.

        Azure numbers work items and pull requests from INDEPENDENT sequences, so
        #7 as a work item and !7 as a pull request are unrelated items. Posting
        through the wrong one would comment on something the caller never named.
        """
        fake = FakeAzure()
        with self.patched(fake):
            azure_client.add_issue_comment(OWNER, REPO, 7, "on the work item", host=HOST)
            azure_client.add_pr_comment(OWNER, REPO, 7, "on the pull request", host=HOST)
        resources = [call["resource"] for call in fake.calls]
        self.assertEqual(resources, ["comments", "pullRequestThreads"])
        areas = [call["area"] for call in fake.calls]
        self.assertEqual(areas, ["wit", "git"])


class TestOpenWorkItemProbeCount(unittest.TestCase):
    """The probe's count is the WIQL id list's length, not the hydrated rows'.

    Only the newest item is hydrated -- that is the whole point of the cheap
    probe -- so a count taken from the hydrate response would report 1 for every
    project and the poll gate would never see the list change.
    """

    @staticmethod
    def _fake(ids):
        calls: list[dict] = []

        def invoke(**kwargs):
            calls.append(dict(kwargs))
            resource = kwargs["resource"]
            if resource == "workitemtypes":
                return {
                    "value": [
                        {
                            "name": "Bug",
                            "states": [
                                {"name": "New", "category": "Proposed"},
                                {"name": "Closed", "category": "Completed"},
                            ],
                        }
                    ]
                }
            if resource == "wiql":
                return {"workItems": [{"id": i} for i in ids]}
            if resource == "workitemsbatch":
                requested = kwargs["body"]["ids"]
                return {
                    "value": [
                        {"id": i, "fields": {"System.ChangedDate": "2026-03-04T05:06:07Z"}}
                        for i in requested
                    ]
                }
            raise AssertionError(f"unexpected resource: {resource}")

        return invoke, calls

    def test_the_count_is_the_full_id_list_while_only_one_row_is_hydrated(self):
        invoke, calls = self._fake([11, 12, 13, 14, 15])
        with mock.patch.object(azure_client, "_az_invoke", side_effect=invoke):
            probe = azure_client.probe_open_list(OWNER, REPO, "issue", host=HOST)
        self.assertEqual(probe["total_count"], 5)
        self.assertEqual(probe["top_updated_at"], "2026-03-04T05:06:07Z")
        batches = [call for call in calls if call["resource"] == "workitemsbatch"]
        self.assertEqual(len(batches), 1)
        self.assertEqual(
            batches[0]["body"]["ids"], [11], "the probe hydrated more than the top row"
        )

    def test_an_empty_project_probes_zero_with_no_hydration_at_all(self):
        invoke, calls = self._fake([])
        with mock.patch.object(azure_client, "_az_invoke", side_effect=invoke):
            probe = azure_client.probe_open_list(OWNER, REPO, "issue", host=HOST)
        self.assertEqual(probe, {"total_count": 0, "top_updated_at": None})
        self.assertEqual(
            [call["resource"] for call in calls if call["resource"] == "workitemsbatch"], []
        )

    def test_a_non_integer_id_is_not_counted(self):
        # The count gates polling, so a malformed row must not inflate it and make
        # every poll read as "the list changed".
        invoke, _ = self._fake([11, "12", None, 13])
        with mock.patch.object(azure_client, "_az_invoke", side_effect=invoke):
            probe = azure_client.probe_open_list(OWNER, REPO, "issue", host=HOST)
        self.assertEqual(probe["total_count"], 2)


class TestSetIssueAssignees(AzureWriteCase):
    """``System.AssignedTo`` is ONE identity, so this is where Azure and the other
    two providers genuinely disagree rather than merely differ in spelling.

    Everything here is about the two directions the disagreement can fail in: a
    multi-name request must not be silently truncated to its first entry (the
    caller would be told the write succeeded for names the item does not carry),
    and a name the project cannot bind must not be sent (Azure's own failure names
    the FIELD, so the user never learns which login was the problem).
    """

    def _fake(self, teams: object = None) -> FakeAzure:
        # Both member shapes at once: Azure nests the identity on some payloads
        # and inlines it on others, so a client that reads only one would pass
        # against a roster carrying only the other.
        return FakeAzure(members=[{"identity": dict(ADA)}, dict(GRACE)], teams=teams)

    def test_one_resolvable_login_is_written_and_read_back(self):
        fake = self._fake()
        with self.patched(fake):
            final = azure_client.set_issue_assignees(
                OWNER, REPO, NUMBER, ["ada@contoso.com"], host=HOST
            )
        self.assertEqual(final, ["ada@contoso.com"])
        self.assertEqual(
            fake.patches,
            [[{"op": "add", "path": "/fields/System.AssignedTo", "value": "ada@contoso.com"}]],
        )

    def test_a_display_name_is_written_as_the_rosters_unique_name(self):
        # The picker shows display names, so that is what a user supplies. The
        # identity field binds to the unique name, so echoing the caller's spelling
        # back would send Azure something it cannot resolve.
        fake = self._fake()
        with self.patched(fake):
            final = azure_client.set_issue_assignees(
                OWNER, REPO, NUMBER, ["Ada Lovelace"], host=HOST
            )
        self.assertEqual(final, ["ada@contoso.com"])
        self.assertEqual(fake.patches[0][0]["value"], "ada@contoso.com")

    def test_the_result_comes_from_the_response_not_the_request(self):
        # The whole point of reading back: a write Azure honoured differently from
        # what was asked must report what the item now carries.
        fake = FakeAzure(members=[dict(ADA)])
        with self.patched(fake):
            final = azure_client.set_issue_assignees(
                OWNER, REPO, NUMBER, ["ada lovelace"], host=HOST
            )
        # ADA is the only roster entry, so the response echo is ADA's identity
        # object -- parsed, not the "ada lovelace" string the caller typed.
        self.assertEqual(final, ["ada@contoso.com"])

    def test_an_empty_list_clears_the_field_with_add_not_remove(self):
        # A JSON Patch ``remove`` fails outright on a work item that never carried
        # an assignee, which is exactly the case a "clear it" arrives for.
        fake = self._fake()
        with self.patched(fake):
            final = azure_client.set_issue_assignees(OWNER, REPO, NUMBER, [], host=HOST)
        self.assertEqual(final, [])
        self.assertEqual(
            fake.patches,
            [[{"op": "add", "path": "/fields/System.AssignedTo", "value": ""}]],
        )
        # No roster lookup is paid for a clear.
        self.assertEqual(fake.calls_to("teams"), [])

    def test_two_names_are_refused_and_nothing_is_written(self):
        # Assigning the first and dropping the rest would report success for names
        # the work item does not carry.
        fake = self._fake()
        with self.patched(fake):
            with self.assertRaises(ProviderInvalidInputError) as caught:
                azure_client.set_issue_assignees(
                    OWNER, REPO, NUMBER, ["ada@contoso.com", "grace@contoso.com"], host=HOST
                )
        self.assertEqual(fake.patches, [])
        self.assertEqual(sorted(caught.exception.values), ["ada@contoso.com", "grace@contoso.com"])

    def test_an_unresolvable_login_is_refused_before_any_write(self):
        fake = self._fake()
        with self.patched(fake):
            with self.assertRaises(ProviderInvalidInputError) as caught:
                azure_client.set_issue_assignees(
                    OWNER, REPO, NUMBER, ["nobody@contoso.com"], host=HOST
                )
        self.assertEqual(fake.patches, [])
        self.assertEqual(caught.exception.values, ["nobody@contoso.com"])

    def test_an_unreadable_roster_is_an_input_error_not_an_upstream_one(self):
        # The route maps ProviderInvalidInputError to 400 and a bare
        # ProviderCliError to 502. The name could not be checked, so the user must
        # be told to pick again rather than that the forge is down.
        def teams(_kwargs: dict) -> object:
            raise ProviderCliError("forbidden")

        fake = self._fake(teams=teams)
        with self.patched(fake):
            with self.assertRaises(ProviderInvalidInputError):
                azure_client.set_issue_assignees(
                    OWNER, REPO, NUMBER, ["ada@contoso.com"], host=HOST
                )
        self.assertEqual(fake.patches, [])

    def test_blank_names_are_dropped_rather_than_counted(self):
        # A picker that emits an empty row must not turn a single assignment into
        # the "more than one" refusal.
        fake = self._fake()
        with self.patched(fake):
            final = azure_client.set_issue_assignees(
                OWNER, REPO, NUMBER, ["  ", "ada@contoso.com", ""], host=HOST
            )
        self.assertEqual(final, ["ada@contoso.com"])

    def test_the_write_is_sent_as_json_patch(self):
        # Azure rejects a work item write outright when the content type says
        # anything else, so the media type is part of the contract.
        fake = self._fake()
        with self.patched(fake):
            azure_client.set_issue_assignees(OWNER, REPO, NUMBER, ["ada@contoso.com"], host=HOST)
        patch_call = next(
            c for c in fake.calls if c.get("resource") == "workitems" and c.get("method") == "PATCH"
        )
        self.assertEqual(patch_call["media_type"], "application/json-patch+json")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
