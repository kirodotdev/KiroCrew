"""Azure DevOps PR ACTIONS and the pipeline BUILD path.

``test_azure.py`` covers the two places Azure refuses outright -- a review verdict
that cannot be bound to a commit, and a build id that belongs to another
repository. This file covers what happens on the paths that DO act, where the
failure mode is silent rather than loud: a request that maps onto the wrong Azure
concept still returns a success-shaped dict, so the only way to catch it is to
assert the REQUEST BODY that reaches the provider, not just the return value.

What is asserted, and why each one would be invisible otherwise:

  * ``set_pr_state`` -- Azure spells "closed without merging" ``abandoned`` and
    spells "merged" ``completed``. Sending ``completed`` for a close request would
    MERGE the pull request while returning a perfectly ordinary closed-looking
    result, so the outgoing status is asserted directly.
  * ``merge_pull_request`` -- the head sha rides as ``lastMergeSourceCommit``,
    which is the precondition that stops a push landing between the review and
    the click from merging unreviewed code; ``bypassPolicy`` must never be sent
    true, because a button that sheds a required branch policy is not something
    the provider would adjudicate for us; and ``REBASE`` must map to Azure's
    ``rebase`` (replay then fast-forward), never to ``rebaseMerge``, which still
    writes a merge commit. All three produce a merged-looking response either
    way, so only the body distinguishes them.
  * ``enable_auto_merge`` / ``disable_auto_merge`` -- arming is
    ``autoCompleteSetBy`` set to an identity GUID and cancelling is that field set
    to the EMPTY GUID, because Azure has no unset verb and an omitted field leaves
    the PR armed. An unresolvable identity therefore has to refuse: arming with no
    identity would report success while arming nothing.
  * ``list_pr_workflow_runs`` -- Azure reports ``status`` and ``result``
    separately and spells both differently from GitHub (notably ``canceled`` with
    one l, against a UI that keys on ``cancelled``), and the row's
    ``cancellable``/``rerunnable`` flags decide which actions the UI offers.
  * ``rerun_workflow_run`` -- Azure has no re-run verb, so a retry is a NEW build.
    It must be pinned to the ORIGINAL ``sourceBranch`` and ``sourceVersion``:
    queuing without them builds the branch's current tip, which is a different
    revision than the one the user asked to retry, and reports success for it.
    ``failed_only`` is reported as False regardless of what was asked, because
    Azure cannot honour a partial retry.

Every refusal additionally asserts that NOTHING was sent -- the point of a refusal
is that no state was mutated, and a refusal raised after the write has already
gone out looks identical from the caller's side.

No test here reaches the network or needs the ``az`` CLI: each one mocks
``azure_client._az_invoke``, the single point every REST call funnels through, in
the same way ``test_azure.py`` does.
"""

from __future__ import annotations

import unittest
from unittest import mock

from kiro_crew.apps.builtins.issue_radar.backend import azure_client
from kiro_crew.apps.builtins.issue_radar.backend.errors import ProviderCliError

OWNER = "contoso/Widgets"
REPO = "widget-service"
HOST = "dev.azure.com"
SHA = "a" * 40
GUID = "11111111-2222-3333-4444-555555555555"


class _FakeAz:
    """A fake ``_az_invoke`` that records every call and replays canned answers.

    Recording is what makes the assertions meaningful: these functions all return
    a plausible dict whatever they send, so the test has to inspect the outgoing
    ``body``/``route``. An answer that is an exception instance is raised, and a
    call with no answer left is an assertion failure rather than a silent ``None``
    -- an unexpected extra request is itself a defect worth failing on.
    """

    def __init__(self, *answers: object) -> None:
        self._answers: list[object] = list(answers)
        self.calls: list[dict] = []

    def __call__(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        if not self._answers:
            raise AssertionError(f"unexpected extra az call: {kwargs!r}")
        answer = self._answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def methods(self) -> list[str]:
        """The HTTP method of every recorded call, with GET spelled explicitly."""
        return [str(call.get("method") or "GET") for call in self.calls]

    def writes(self) -> list[dict]:
        """Only the mutating calls -- what a refusal must have none of."""
        return [call for call in self.calls if str(call.get("method") or "GET") != "GET"]


class TestSetPrState(unittest.TestCase):
    """Close and reopen map onto Azure's PR ``status``, which has three values.

    ``abandoned`` is the reversible "closed without merging"; ``completed`` MERGES.
    Both come back as an ordinary status string, so the outgoing value is the only
    place the difference is visible.
    """

    def test_closing_abandons_rather_than_completing(self):
        az = _FakeAz({"status": "abandoned", "isDraft": False})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            out = azure_client.set_pr_state(OWNER, REPO, 7, "closed", host=HOST)
        body = az.writes()[0]["body"]
        self.assertEqual(body, {"status": "abandoned"})
        # The assertion that matters: a close must never send Azure's merge value.
        self.assertNotEqual(body["status"], "completed")
        self.assertEqual(out, {"state": "closed", "merged": False, "draft": False})

    def test_reopening_sets_the_pr_active(self):
        az = _FakeAz({"status": "active"})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            out = azure_client.set_pr_state(OWNER, REPO, 7, "open", host=HOST)
        self.assertEqual(az.writes()[0]["body"], {"status": "active"})
        self.assertEqual(out, {"state": "open", "merged": False, "draft": False})

    def test_the_patch_is_addressed_by_repository_and_pr_number(self):
        # A pull request id is unique across the collection, but every mutating PR
        # route still requires the repository in the path -- omitting it 404s.
        az = _FakeAz({"status": "active"})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            azure_client.set_pr_state(OWNER, REPO, "7", "open", host=HOST)  # type: ignore[arg-type]
        call = az.writes()[0]
        self.assertEqual(
            call["route"],
            {"project": "Widgets", "repositoryId": REPO, "pullRequestId": 7},
        )
        self.assertEqual(call["method"], "PATCH")

    def test_a_completed_pull_request_reports_merged(self):
        # Azure refuses to reopen a completed PR; when it answers with the
        # completed status anyway, the result must say merged rather than open.
        az = _FakeAz({"status": "completed"})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            out = azure_client.set_pr_state(OWNER, REPO, 7, "open", host=HOST)
        self.assertEqual(out, {"state": "closed", "merged": True, "draft": False})

    def test_a_missing_status_echoes_the_requested_state(self):
        # Reporting "open" for a close request would tell the UI the click failed
        # when Azure simply did not echo the field.
        az = _FakeAz({})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            out = azure_client.set_pr_state(OWNER, REPO, 7, "closed", host=HOST)
        self.assertEqual(out["state"], "closed")
        self.assertFalse(out["merged"])

    def test_draft_is_reported_from_azures_own_flag(self):
        az = _FakeAz({"status": "active", "isDraft": True})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            out = azure_client.set_pr_state(OWNER, REPO, 7, "open", host=HOST)
        self.assertTrue(out["draft"])

    def test_an_unknown_state_is_refused_without_a_write(self):
        # "merged" is the dangerous one: an unvalidated state name reaching the
        # status field is how a close request becomes a merge.
        for bad in ("merged", "completed", "abandoned", "", "OPEN"):
            with self.subTest(bad=bad):
                spawn = mock.Mock()
                with mock.patch.object(azure_client, "_az_invoke", spawn):
                    with self.assertRaises(ProviderCliError):
                        azure_client.set_pr_state(OWNER, REPO, 7, bad, host=HOST)
                spawn.assert_not_called()


class TestMergePullRequest(unittest.TestCase):
    """Completing a PR: the precondition, the strategy map, and the override.

    Azure merges by PATCHing the PR to ``completed``. Everything that makes that
    safe lives in the body -- the commit it is pinned to, and the policy override
    that is deliberately never set -- so the body is what is asserted.
    """

    def test_the_head_sha_rides_as_a_real_precondition(self):
        az = _FakeAz({"status": "completed", "lastMergeCommit": {"commitId": "c" * 40}})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            out = azure_client.merge_pull_request(OWNER, REPO, 7, "SQUASH", SHA, host=HOST)
        body = az.writes()[0]["body"]
        self.assertEqual(body["status"], "completed")
        # Azure refuses the completion when this is no longer the PR's last source
        # commit, which is what stops a push landing mid-review from being merged.
        self.assertEqual(body["lastMergeSourceCommit"], {"commitId": SHA})
        self.assertEqual(out, {"merged": True, "sha": "c" * 40, "message": ""})

    def test_an_empty_head_sha_is_refused_without_a_write(self):
        spawn = mock.Mock()
        with mock.patch.object(azure_client, "_az_invoke", spawn):
            with self.assertRaises(ProviderCliError) as caught:
                azure_client.merge_pull_request(OWNER, REPO, 7, "SQUASH", "", host=HOST)
        self.assertIn("without the head commit", str(caught.exception))
        spawn.assert_not_called()

    def test_a_malformed_head_sha_is_refused_without_a_write(self):
        # Not merely empty: a value that is not a commit id cannot be a
        # precondition, and Azure would accept the PATCH without one.
        for bad in ("zzzz", "abc", "a" * 41 + "z", "  "):
            with self.subTest(bad=bad):
                spawn = mock.Mock()
                with mock.patch.object(azure_client, "_az_invoke", spawn):
                    with self.assertRaises(ProviderCliError):
                        azure_client.merge_pull_request(OWNER, REPO, 7, "SQUASH", bad, host=HOST)
                spawn.assert_not_called()

    def test_each_method_maps_to_azures_own_strategy_name(self):
        expected = {"MERGE": "noFastForward", "SQUASH": "squash", "REBASE": "rebase"}
        self.assertEqual(set(expected), set(azure_client.PR_MERGE_METHODS))
        for verb, strategy in expected.items():
            with self.subTest(verb=verb):
                az = _FakeAz({"status": "completed"})
                with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
                    azure_client.merge_pull_request(OWNER, REPO, 7, verb, SHA, host=HOST)
                options = az.writes()[0]["body"]["completionOptions"]
                self.assertEqual(options["mergeStrategy"], strategy)

    def test_rebase_is_the_linear_rebase_not_rebase_merge(self):
        """``rebaseMerge`` replays AND still writes a merge commit.

        Both names are accepted by Azure and both return a merged PR, so the wrong
        one produces a history the caller did not ask for with no error anywhere.
        Asserted on its own because it is the one mapping where a plausible-looking
        value is wrong.
        """
        az = _FakeAz({"status": "completed"})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            azure_client.merge_pull_request(OWNER, REPO, 7, "REBASE", SHA, host=HOST)
        strategy = az.writes()[0]["body"]["completionOptions"]["mergeStrategy"]
        self.assertEqual(strategy, "rebase")
        self.assertNotEqual(strategy.lower(), "rebasemerge")

    def test_a_lowercase_method_is_accepted(self):
        az = _FakeAz({"status": "completed"})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            azure_client.merge_pull_request(OWNER, REPO, 7, " squash ", SHA, host=HOST)
        self.assertEqual(az.writes()[0]["body"]["completionOptions"]["mergeStrategy"], "squash")

    def test_an_unknown_method_is_refused_without_a_write(self):
        for bad in ("FAST_FORWARD", "rebaseMerge", "", "noFastForward"):
            with self.subTest(bad=bad):
                spawn = mock.Mock()
                with mock.patch.object(azure_client, "_az_invoke", spawn):
                    with self.assertRaises(ProviderCliError):
                        azure_client.merge_pull_request(OWNER, REPO, 7, bad, SHA, host=HOST)
                spawn.assert_not_called()

    def test_the_policy_override_is_never_requested(self):
        """``bypassPolicy`` is Azure's override switch and must stay false.

        Asserted as an identity against ``False`` rather than a falsy check: the
        field being ABSENT is not good enough either, because Azure reads an
        omitted completion option as "leave as armed", which would inherit
        whatever the pull request was last set to.
        """
        for verb in azure_client.PR_MERGE_METHODS:
            with self.subTest(verb=verb):
                az = _FakeAz({"status": "completed"})
                with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
                    azure_client.merge_pull_request(OWNER, REPO, 7, verb, SHA, host=HOST)
                options = az.writes()[0]["body"]["completionOptions"]
                self.assertIs(options["bypassPolicy"], False)
                self.assertIs(options["deleteSourceBranch"], False)

    def test_a_refused_completion_reports_not_merged_with_azures_reason(self):
        # Azure enforces the branch's policies on completion and answers with the
        # PR still active plus a reason. Reporting merged here would tell the user
        # their change shipped when it did not.
        az = _FakeAz({"status": "active", "mergeFailureMessage": "required reviewer missing"})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            out = azure_client.merge_pull_request(OWNER, REPO, 7, "SQUASH", SHA, host=HOST)
        self.assertEqual(
            out, {"merged": False, "sha": None, "message": "required reviewer missing"}
        )


class TestEnableAutoMerge(unittest.TestCase):
    """Arming auto-complete is an identity write, so the identity is load-bearing.

    ``autoCompleteSetBy`` names WHO armed it; Azure completes the PR on their
    behalf once policies pass. An unresolved identity cannot arm anything, so it
    must refuse rather than send a request that reports success.
    """

    def test_the_resolved_identity_arms_the_pull_request(self):
        az = _FakeAz(
            {
                "autoCompleteSetBy": {"id": GUID},
                "completionOptions": {"mergeStrategy": "squash"},
            }
        )
        with mock.patch.object(
            azure_client, "_current_identity", return_value={"id": GUID, "login": "ada"}
        ):
            with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
                out = azure_client.enable_auto_merge(OWNER, REPO, 7, "SQUASH", host=HOST)
        body = az.writes()[0]["body"]
        self.assertEqual(body["autoCompleteSetBy"], {"id": GUID})
        # The strategy is armed alongside, so the deferred merge produces the
        # history the caller chose rather than whatever the PR was last set to.
        self.assertEqual(body["completionOptions"]["mergeStrategy"], "squash")
        self.assertEqual(out, {"auto_merge": True, "method": "SQUASH", "enabled_at": None})

    def test_the_armed_state_is_observed_not_asserted(self):
        # A hardcoded True would make the result a claim. Azure answering with no
        # armed identity means the PR is NOT armed, whatever was sent.
        az = _FakeAz({"autoCompleteSetBy": {}, "completionOptions": {}})
        with mock.patch.object(
            azure_client, "_current_identity", return_value={"id": GUID, "login": "ada"}
        ):
            with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
                out = azure_client.enable_auto_merge(OWNER, REPO, 7, "MERGE", host=HOST)
        self.assertEqual(out, {"auto_merge": False, "method": None, "enabled_at": None})

    def test_a_strategy_azure_armed_itself_is_reported_in_the_apps_vocabulary(self):
        # Azure's own web UI can arm ``rebaseMerge``, which this module never
        # sets. Reading it back as REBASE keeps the reported method in the app's
        # three-value vocabulary instead of leaking an Azure spelling to the UI.
        az = _FakeAz(
            {
                "autoCompleteSetBy": {"id": GUID},
                "completionOptions": {"mergeStrategy": "rebaseMerge"},
            }
        )
        with mock.patch.object(
            azure_client, "_current_identity", return_value={"id": GUID, "login": "ada"}
        ):
            with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
                out = azure_client.enable_auto_merge(OWNER, REPO, 7, "SQUASH", host=HOST)
        self.assertEqual(out["method"], "REBASE")

    def test_an_unresolvable_identity_refuses_without_arming(self):
        """No GUID means no arming, so the request must not be sent at all.

        Sending it anyway is the bad outcome this guards: Azure would accept the
        PATCH, nothing would be armed, and the result would still report success
        because it is derived from a response that echoed an empty field.
        """
        for identity in ({"id": ""}, {"id": "not-a-guid"}, {"login": "ada"}, {}):
            with self.subTest(identity=identity):
                spawn = mock.Mock()
                with mock.patch.object(azure_client, "_current_identity", return_value=identity):
                    with mock.patch.object(azure_client, "_az_invoke", spawn):
                        with self.assertRaises(ProviderCliError) as caught:
                            azure_client.enable_auto_merge(OWNER, REPO, 7, "SQUASH", host=HOST)
                self.assertIn("could not be resolved", str(caught.exception))
                spawn.assert_not_called()

    def test_a_failing_identity_lookup_propagates_without_arming(self):
        spawn = mock.Mock()
        with mock.patch.object(
            azure_client, "_current_identity", side_effect=ProviderCliError("identity unreadable")
        ):
            with mock.patch.object(azure_client, "_az_invoke", spawn):
                with self.assertRaises(ProviderCliError):
                    azure_client.enable_auto_merge(OWNER, REPO, 7, "SQUASH", host=HOST)
        spawn.assert_not_called()

    def test_an_unknown_method_is_refused_before_the_identity_is_resolved(self):
        # Validating the cheap argument first means a bad request costs no round
        # trip, and -- more importantly -- writes nothing.
        identity = mock.Mock()
        spawn = mock.Mock()
        with mock.patch.object(azure_client, "_current_identity", identity):
            with mock.patch.object(azure_client, "_az_invoke", spawn):
                with self.assertRaises(ProviderCliError):
                    azure_client.enable_auto_merge(OWNER, REPO, 7, "FAST_FORWARD", host=HOST)
        identity.assert_not_called()
        spawn.assert_not_called()


class TestDisableAutoMerge(unittest.TestCase):
    """Cancelling is a write of the EMPTY GUID, not an omission.

    Azure has no unset verb for ``autoCompleteSetBy``. Leaving the field out of the
    patch leaves the PR armed, so a cancel that "did nothing" merges the PR later.
    """

    def test_the_cancel_writes_the_empty_guid(self):
        az = _FakeAz({"autoCompleteSetBy": {"id": azure_client._EMPTY_GUID}})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            out = azure_client.disable_auto_merge(OWNER, REPO, 7, host=HOST)
        body = az.writes()[0]["body"]
        self.assertEqual(body, {"autoCompleteSetBy": {"id": azure_client._EMPTY_GUID}})
        self.assertEqual(azure_client._EMPTY_GUID, "00000000-0000-0000-0000-000000000000")
        self.assertEqual(out, {"auto_merge": False, "method": None, "enabled_at": None})

    def test_a_cleared_field_is_accepted_too(self):
        # Azure may answer with the field absent rather than echoing the empty
        # GUID; that is a successful cancel, not an ambiguous one.
        az = _FakeAz({"autoCompleteSetBy": None})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            out = azure_client.disable_auto_merge(OWNER, REPO, 7, host=HOST)
        self.assertEqual(out, {"auto_merge": False, "method": None, "enabled_at": None})

    def test_a_pr_still_armed_afterwards_raises_rather_than_reporting_success(self):
        """Returning the cleared shape here would be a lie with consequences.

        The caller stops offering the cancel, and Azure completes the pull request
        the moment its policies pass -- the exact outcome the user clicked to
        prevent.
        """
        az = _FakeAz({"autoCompleteSetBy": {"id": GUID}})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            with self.assertRaises(ProviderCliError) as caught:
                azure_client.disable_auto_merge(OWNER, REPO, 7, host=HOST)
        self.assertIn("still reports auto-complete as armed", str(caught.exception))


class TestListPrWorkflowRuns(unittest.TestCase):
    """Build rows are normalized into the GitHub run vocabulary the UI compares.

    Azure reports ``status`` and ``result`` separately and spells both
    differently, and the row's own ``cancellable``/``rerunnable`` flags decide
    which buttons the UI offers -- offering the wrong one produces an action Azure
    refuses.
    """

    @staticmethod
    def _build(**overrides: object) -> dict:
        build: dict = {
            "id": 501,
            "status": "completed",
            "result": "succeeded",
            "definition": {"name": "CI"},
            "_links": {"web": {"href": "https://dev.azure.com/contoso/Widgets/_build/501"}},
            "reason": "pullRequest",
            "queueTime": "2026-01-02T03:04:05Z",
            "sourceVersion": SHA,
        }
        build.update(overrides)
        return build

    def _rows(self, *builds: dict, sha: str = SHA) -> tuple[list[dict], _FakeAz]:
        az = _FakeAz({"value": list(builds)})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            rows = azure_client.list_pr_workflow_runs(OWNER, REPO, sha, host=HOST)
        return rows, az

    def test_a_finished_build_is_rerunnable_and_carries_a_conclusion(self):
        rows, _ = self._rows(self._build())
        self.assertEqual(
            rows,
            [
                {
                    "id": 501,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "url": "https://dev.azure.com/contoso/Widgets/_build/501",
                    "event": "pullRequest",
                    "created_at": "2026-01-02T03:04:05Z",
                    "cancellable": False,
                    "rerunnable": True,
                }
            ],
        )

    def test_an_in_flight_build_is_cancellable_and_has_no_conclusion(self):
        # A conclusion on an unfinished build would render a verdict for a build
        # that has not produced one.
        rows, _ = self._rows(self._build(status="inProgress", result=None))
        self.assertEqual(rows[0]["status"], "inprogress")
        self.assertIsNone(rows[0]["conclusion"])
        self.assertTrue(rows[0]["cancellable"])
        self.assertFalse(rows[0]["rerunnable"])

    def test_azures_result_names_are_translated_to_the_shared_vocabulary(self):
        # "canceled" with one l is Azure's spelling and the UI keys on GitHub's
        # "cancelled", so passing it through leaves a consumer unable to see a
        # cancelled build at all.
        expected = {
            "succeeded": "success",
            "failed": "failure",
            "partiallySucceeded": "neutral",
            "canceled": "cancelled",
            "cancelled": "cancelled",
        }
        for result, conclusion in expected.items():
            with self.subTest(result=result):
                rows, _ = self._rows(self._build(result=result))
                self.assertEqual(rows[0]["conclusion"], conclusion)

    def test_an_unmapped_result_is_passed_through_rather_than_dropped(self):
        # A value this map has never seen is still information; reporting None
        # would render a finished build as having produced no verdict.
        rows, _ = self._rows(self._build(result="somethingNew"))
        self.assertEqual(rows[0]["conclusion"], "somethingnew")

    def test_a_finished_build_with_no_result_reports_no_conclusion(self):
        rows, _ = self._rows(self._build(result=""))
        self.assertIsNone(rows[0]["conclusion"])

    def test_the_name_falls_back_through_the_build_number(self):
        rows, _ = self._rows(self._build(definition={}, buildNumber="20260102.3"))
        self.assertEqual(rows[0]["name"], "20260102.3")
        rows, _ = self._rows(self._build(definition={}, buildNumber=None))
        self.assertEqual(rows[0]["name"], "build")

    def test_the_created_at_falls_back_to_the_start_time(self):
        rows, _ = self._rows(self._build(queueTime=None, startTime="2026-02-03T00:00:00Z"))
        self.assertEqual(rows[0]["created_at"], "2026-02-03T00:00:00Z")

    def test_a_build_without_a_usable_id_is_skipped(self):
        """A row the UI cannot address is dropped, not emitted with a bad id.

        Every action the row offers is addressed by that id, so a string or
        missing id would produce buttons that cannot work.
        """
        rows, _ = self._rows(self._build(id="501"), self._build(id=None), self._build(id=777))
        self.assertEqual([row["id"] for row in rows], [777])

    def test_only_builds_for_the_requested_commit_are_returned(self):
        # Azure's build list has no commit filter, so the match happens here. A
        # build for a different revision would attribute another commit's failure
        # to this PR's head.
        rows, az = self._rows(self._build(id=501), self._build(id=502, sourceVersion="b" * 40))
        self.assertEqual([row["id"] for row in rows], [501])
        # And the list is scoped to this repository within the project, not to the
        # whole project's build history.
        query = az.calls[0]["query"]
        self.assertEqual(query["repositoryId"], "Widgets/widget-service")
        self.assertEqual(query["repositoryType"], "TfsGit")

    def test_the_commit_is_matched_case_insensitively(self):
        # A sha is hex, so case carries no meaning and Azure's echo of it is not
        # guaranteed to match the caller's spelling.
        rows, _ = self._rows(self._build(sourceVersion=SHA.upper()))
        self.assertEqual([row["id"] for row in rows], [501])

    def test_an_invalid_sha_is_refused_without_a_call(self):
        # The sha reaches a query string, so the charset is checked before it can
        # be sent -- and a request that cannot be trusted is not sent at all.
        for bad in ("", "not-a-sha", "abc", "a" * 40 + "!", "a" * 65):
            with self.subTest(bad=bad):
                spawn = mock.Mock()
                with mock.patch.object(azure_client, "_az_invoke", spawn):
                    with self.assertRaises(ProviderCliError):
                        azure_client.list_pr_workflow_runs(OWNER, REPO, bad, host=HOST)
                spawn.assert_not_called()

    def test_a_repository_with_no_matching_builds_is_an_empty_list(self):
        rows, _ = self._rows()
        self.assertEqual(rows, [])


class TestReadBuild(unittest.TestCase):
    """The single build read every run mutation is bound by.

    ``_assert_build_belongs_to_repo`` decides whether a cancel or a requeue is
    allowed from what this returns, so its addressing and its handling of a
    malformed answer are both part of that guard.
    """

    def test_the_build_is_addressed_by_project_and_id(self):
        az = _FakeAz({"id": 77, "repository": {"name": REPO}})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            build = azure_client._read_build("contoso", "Widgets", 77, host=HOST, timeout=1.0)
        self.assertEqual(build, {"id": 77, "repository": {"name": REPO}})
        call = az.calls[0]
        self.assertEqual(call["route"], {"project": "Widgets", "buildId": 77})
        self.assertEqual(call["area"], "build")
        self.assertEqual(call["resource"], "builds")
        self.assertEqual(call["api_version"], azure_client._API_BUILD)
        # A read, so no method is sent at all -- not a PATCH with an empty body.
        self.assertEqual(az.methods(), ["GET"])
        self.assertEqual(az.writes(), [])

    def test_the_build_id_is_coerced_to_an_int_before_it_reaches_the_route(self):
        # The id becomes part of a REST path; a caller passing the number as text
        # must not put a raw string there.
        az = _FakeAz({"id": 77})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            azure_client._read_build(
                "contoso", "Widgets", "77", host=HOST, timeout=1.0  # type: ignore[arg-type]
            )
        self.assertEqual(az.calls[0]["route"]["buildId"], 77)

    def test_a_non_object_answer_becomes_an_empty_build(self):
        """A malformed answer must not crash the ownership check.

        An empty build has no repository name, which the ownership guard already
        treats as "could not determine" and refuses -- so degrading to {} here
        fails closed rather than raising an AttributeError out of a route.
        """
        malformed: tuple[object, ...] = ([], "nope", None, 7)
        for answer in malformed:
            with self.subTest(answer=answer):
                az = _FakeAz(answer)
                with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
                    self.assertEqual(
                        azure_client._read_build("contoso", "Widgets", 77, host=HOST, timeout=1.0),
                        {},
                    )


class TestRerunWorkflowRun(unittest.TestCase):
    """A retry is a NEW build, so what it is pinned to is the whole question.

    Azure has no re-run verb. Queuing from the definition alone builds the
    branch's current TIP, which is a different revision than the one the user
    asked to retry -- and it reports success for it, so nothing surfaces the
    substitution.
    """

    BRANCH = "refs/heads/feature/widget"
    ORIGINAL_SHA = "b" * 40

    def _original(self, **overrides: object) -> dict:
        build: dict = {
            "id": 99,
            "repository": {"name": REPO},
            "definition": {"id": 12, "name": "CI"},
            "sourceBranch": self.BRANCH,
            "sourceVersion": self.ORIGINAL_SHA,
        }
        build.update(overrides)
        return build

    def test_the_new_build_is_pinned_to_the_originals_branch_and_commit(self):
        az = _FakeAz(self._original(), {"id": 777})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            out = azure_client.rerun_workflow_run(OWNER, REPO, 99, host=HOST)
        queued = az.writes()[0]
        self.assertEqual(queued["method"], "POST")
        self.assertEqual(
            queued["body"],
            {
                "definition": {"id": 12},
                "sourceBranch": self.BRANCH,
                "sourceVersion": self.ORIGINAL_SHA,
            },
        )
        # The commit is the ORIGINAL build's, never the branch tip -- asserted
        # explicitly because both are 40 hex characters and only one is right.
        self.assertEqual(queued["body"]["sourceVersion"], self.ORIGINAL_SHA)
        # Queued against the project, with no build id: this is a new build, not a
        # mutation of the one being retried.
        self.assertEqual(queued["route"], {"project": "Widgets"})
        self.assertNotIn("buildId", queued["route"])
        # The returned id is the NEW build's, so a caller can follow what actually
        # started instead of polling a run that will never change again.
        self.assertEqual(out, {"run_id": 777, "rerun": True, "failed_only": False})

    def test_a_partial_retry_is_reported_as_not_honoured(self):
        """``failed_only`` reports what Azure DID, never what was asked.

        Azure re-runs whole stages, so answering True would tell the caller a
        cheap partial retry happened when a full build was queued.
        """
        for asked in (True, False):
            with self.subTest(failed_only=asked):
                az = _FakeAz(self._original(), {"id": 777})
                with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
                    out = azure_client.rerun_workflow_run(
                        OWNER, REPO, 99, failed_only=asked, host=HOST
                    )
                self.assertIs(out["failed_only"], False)
                self.assertIs(out["rerun"], True)
                # Nothing about the request changes either: no partial-retry hint
                # is smuggled into the payload.
                self.assertNotIn("failed_only", az.writes()[0]["body"])

    def test_an_unpinnable_original_omits_the_fields_rather_than_sending_empties(self):
        # An empty sourceVersion is not the same as an absent one: Azure would
        # reject the queue request rather than fall back to the branch default.
        az = _FakeAz(self._original(sourceBranch="", sourceVersion=None), {"id": 777})
        with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
            azure_client.rerun_workflow_run(OWNER, REPO, 99, host=HOST)
        self.assertEqual(az.writes()[0]["body"], {"definition": {"id": 12}})

    def test_a_missing_definition_is_refused_before_queueing(self):
        """Without a definition id there is nothing to queue.

        The refusal has to come before the POST: a queue request with no
        definition is one Azure could interpret against a project default, which
        would start a build the user never asked for.
        """
        for original in (
            self._original(definition={}),
            self._original(definition={"id": "12"}),
            self._original(definition=None),
        ):
            with self.subTest(definition=original.get("definition")):
                az = _FakeAz(original)
                with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
                    with self.assertRaises(ProviderCliError) as caught:
                        azure_client.rerun_workflow_run(OWNER, REPO, 99, host=HOST)
                self.assertIn("pipeline definition", str(caught.exception))
                # The ownership read is allowed; nothing was queued.
                self.assertEqual(az.writes(), [])
                self.assertEqual(az.methods(), ["GET"])

    def test_an_unreadable_new_id_falls_back_to_the_original(self):
        # Reporting a non-int id would put a value in the row the UI then cannot
        # address; the original id at least resolves to a real build.
        for answer in ({"id": "777"}, {}, {"id": None}):
            with self.subTest(answer=answer):
                az = _FakeAz(self._original(), answer)
                with mock.patch.object(azure_client, "_az_invoke", side_effect=az):
                    out = azure_client.rerun_workflow_run(OWNER, REPO, 99, host=HOST)
                self.assertEqual(out["run_id"], 99)
                self.assertIs(out["rerun"], True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
