"""Azure DevOps PULL REQUEST read path.

The companion to ``test_azure.py`` (which owns URL parsing, WIQL safety and the
write guards), covering the read path from Azure's own payload to the rows the
app's routes and frontend consume. The parts asserted here are the ones where an
Azure bug would be silent rather than loud:

  * ``_norm_pull`` -- Azure's field NAMES are all different, so this is a rename
    table with judgement calls baked into it: two Azure statuses fold into one
    ``closed``, ``updated_at`` stands in for a timestamp Azure does not have, and
    ``assignees`` is deliberately empty because reviewers are not assignees.
  * ``searchCriteria.status`` on EVERY listing call. Azure defaults it to
    ``active`` server-side, so an omitted status returns open PRs from the closed
    tab and looks like an empty history rather than like a bug.
  * ``_mergeable_state``. ``routes._MERGE_ALLOWED_STATES`` admits ``mergeable``,
    so this string is a merge authorization: reporting it while a blocking branch
    policy is unmet would let the merge button through on a PR Azure itself would
    refuse. Every branch of the decision is pinned, including the fail-closed one
    where the policies cannot be read.
  * ``get_pr_detail(resolve_mergeable=False)`` -- the fast path exists to skip
    that second (policy) call, so the test asserts the call does not happen, not
    merely that the field is ``"unknown"``.
  * The timeline, which comes from THREADS rather than a comment list, and must
    drop Azure's ``commentType == "system"`` entries or bury the human discussion
    under vote noise.
  * ``list_pr_checks`` / ``summarize_checks`` -- two unrelated Azure concepts
    (policy evaluations and pipeline builds) bucketed into the one vocabulary the
    shared card renders.
  * The search path, where a login must be resolved to an identity GUID and an
    unresolvable one has to RAISE: Azure ignores a criteria value it cannot parse,
    which would turn "PRs by ada" into every open PR.

No test here reaches the network or needs the ``az`` CLI: every one either
exercises a pure function or mocks ``azure_client._az_invoke``, the single point
every REST call funnels through, exactly as ``test_azure.py`` does.
"""

from __future__ import annotations

import unittest
from typing import Callable
from unittest import mock

from kiro_crew.apps.builtins.issue_radar.backend import azure_client, routes
from kiro_crew.apps.builtins.issue_radar.backend.errors import (
    ProviderCliError,
    PrSearchError,
)

OWNER = "contoso/Widgets"
REPO = "widget-service"
HOST = "dev.azure.com"
PROJECT_GUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
ADA_GUID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
TEAM_GUID = "9c5b94b1-35ad-49bb-b118-8e8fc24abf80"
SHA = "a" * 40


def _pr_payload(**overrides: object) -> dict:
    """One Azure pull request as its REST API returns it.

    Deliberately spelled with Azure's own field names and casing -- the point of
    ``_norm_pull`` is that NONE of these names survive into the app's row, so a
    fixture written in the app's vocabulary would test nothing.
    """
    payload: dict = {
        "pullRequestId": 12,
        "title": "Add the ledger reconciler",
        "status": "active",
        "isDraft": False,
        "createdBy": {"uniqueName": "ada@contoso.com", "displayName": "Ada"},
        "creationDate": "2026-01-02T03:04:05Z",
        "closedDate": None,
        "labels": [{"name": "needs-triage", "active": True}],
        "reviewers": [{"uniqueName": "grace@contoso.com"}],
        "targetRefName": "refs/heads/main",
        "sourceRefName": "refs/heads/feature/reconciler",
        "lastMergeSourceCommit": {"commitId": SHA},
        "description": "Reconciles the ledger.",
        "mergeStatus": "succeeded",
        "repository": {
            "name": REPO,
            "project": {"name": "Widgets"},
            "url": f"https://{HOST}/contoso/_apis/git/repositories/{REPO}",
        },
    }
    payload.update(overrides)
    return payload


class _Az:
    """A recording stand-in for ``azure_client._az_invoke``.

    Dispatches on the ``(area, resource)`` pair the module addresses endpoints by
    and records every call, so a test can assert BOTH the answer that came back
    and which requests were (or were not) made -- the latter being the only way to
    pin a fast path that exists to skip a call.
    """

    def __init__(self, **handlers: object) -> None:
        # Keyed as "area_resource" so handlers can be passed as keyword arguments.
        self._handlers = handlers
        self.calls: list[dict] = []

    def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        key = f"{kwargs['area']}_{kwargs['resource']}"
        handler = self._handlers.get(key)
        if handler is None:
            raise AssertionError(f"unexpected az call: {key}")
        if callable(handler):
            handler_fn: Callable[[dict], object] = handler
            return handler_fn(kwargs)
        return handler

    def targets(self) -> list[str]:
        return [f"{c['area']}/{c['resource']}" for c in self.calls]

    def queries(self, target: str) -> list[dict]:
        """The query dicts of every call to ``area/resource``."""
        return [
            dict(c.get("query") or {})  # type: ignore[arg-type]
            for c in self.calls
            if f"{c['area']}/{c['resource']}" == target
        ]

    def patch(self) -> mock._patch:
        return mock.patch.object(azure_client, "_az_invoke", side_effect=self)


def _paged(rows: list[dict]) -> Callable[[dict], object]:
    """A handler that serves ``rows`` as one Azure list response.

    ``{"value": [...]}`` is the list-endpoint shape, and a page shorter than the
    requested ``$top`` is Azure's end-of-data signal, so a single short page ends
    ``_az_invoke_paged``'s walk.
    """
    return lambda _kwargs: {"value": rows}


class TestNormPull(unittest.TestCase):
    """Azure's PR payload -> the row shape ``github_client._PR_JQ`` produces.

    Every provider's rows are rendered by ONE frontend component and adjudicated
    by ONE set of routes, so a field that fails to map does not raise -- it renders
    as blank, or worse, as a confident wrong value.
    """

    def test_every_field_is_read_from_azures_own_name(self):
        row = azure_client._norm_pull(_pr_payload())
        self.assertEqual(
            row,
            {
                "number": 12,
                "title": "Add the ledger reconciler",
                "url": (f"https://{HOST}/contoso/Widgets/_git/{REPO}/pullrequest/12"),
                "state": "open",
                "draft": False,
                "labels": ["needs-triage"],
                "author": "ada@contoso.com",
                "author_association": None,
                "updated_at": "2026-01-02T03:04:05Z",
                "created_at": "2026-01-02T03:04:05Z",
                "closed_at": None,
                "merged_at": None,
                "assignees": [],
                "requested_reviewers": ["grace@contoso.com"],
                "base": "main",
                "head": "feature/reconciler",
                "head_sha": SHA,
                "body": "Reconciles the ledger.",
                "additions": None,
                "deletions": None,
                "changed_files": None,
                "checks_state": None,
                "checks_counts": None,
                "checks_truncated": False,
            },
        )

    def test_a_completed_pull_request_is_closed_and_merged(self):
        # Azure has no "merged" status: ``completed`` IS merged. The app's
        # open/closed filter only knows two states, so the distinction has to
        # survive in merged_at or a merged PR becomes indistinguishable from an
        # abandoned one.
        row = azure_client._norm_pull(
            _pr_payload(status="completed", closedDate="2026-02-03T00:00:00Z")
        )
        self.assertEqual(row["state"], "closed")
        self.assertEqual(row["merged_at"], "2026-02-03T00:00:00Z")
        self.assertEqual(row["closed_at"], "2026-02-03T00:00:00Z")

    def test_an_abandoned_pull_request_is_closed_but_not_merged(self):
        row = azure_client._norm_pull(
            _pr_payload(status="abandoned", closedDate="2026-02-03T00:00:00Z")
        )
        self.assertEqual(row["state"], "closed")
        self.assertIsNone(row["merged_at"], "an abandoned PR must not read as merged")
        self.assertEqual(row["closed_at"], "2026-02-03T00:00:00Z")

    def test_an_unknown_status_is_closed_rather_than_open(self):
        # Only ``active`` is open. A status this module has not seen must not
        # present as an open PR the user is expected to act on.
        row = azure_client._norm_pull(_pr_payload(status="notSet"))
        self.assertEqual(row["state"], "closed")
        # ... but it is not claimed as closed-at either, since no close happened.
        self.assertIsNone(row["closed_at"])

    def test_updated_at_falls_back_to_creation_because_azure_has_no_mtime(self):
        # Documented behaviour, asserted because it is the reason the Azure list is
        # ordered by CREATION: a caller sorting on updated_at gets creation order
        # for open PRs, and must not be given a fabricated "now" instead.
        open_row = azure_client._norm_pull(_pr_payload())
        self.assertEqual(open_row["updated_at"], open_row["created_at"])
        closed_row = azure_client._norm_pull(
            _pr_payload(status="abandoned", closedDate="2026-05-06T00:00:00Z")
        )
        self.assertEqual(closed_row["updated_at"], "2026-05-06T00:00:00Z")

    def test_reviewers_are_never_reported_as_assignees(self):
        # An Azure PR has reviewers and no assignees. Folding one into the other
        # would badge a reviewer as owning the change, and the assignee filter
        # would then silently mean "review requested".
        row = azure_client._norm_pull(_pr_payload(reviewers=[{"uniqueName": "grace@contoso.com"}]))
        self.assertEqual(row["assignees"], [])
        self.assertEqual(row["requested_reviewers"], ["grace@contoso.com"])

    def test_a_deactivated_label_is_dropped(self):
        # Azure keeps a removed PR label as a row with ``active: false``. Rendering
        # it would show a tag the PR no longer carries.
        row = azure_client._norm_pull(
            _pr_payload(
                labels=[
                    {"name": "needs-triage", "active": True},
                    {"name": "stale", "active": False},
                    {"name": "", "active": True},
                    {"name": "legacy"},  # no ``active`` key at all -> kept
                ]
            )
        )
        self.assertEqual(row["labels"], ["needs-triage", "legacy"])

    def test_ref_names_are_stripped_to_branch_names(self):
        row = azure_client._norm_pull(_pr_payload())
        self.assertEqual((row["base"], row["head"]), ("main", "feature/reconciler"))
        # A ref that is not under refs/heads (a tag, or an already-short name)
        # passes through rather than being mangled by a blind prefix strip.
        self.assertEqual(azure_client._branch_name("refs/tags/v1"), "refs/tags/v1")
        self.assertEqual(azure_client._branch_name("main"), "main")
        self.assertIsNone(azure_client._branch_name(None))

    def test_the_row_arrives_unenriched_so_it_stays_out_of_the_cache(self):
        # ``checks_counts: None`` is the invariant ``enrichment_complete`` reads.
        # A zeroed count here would cache "no checks" as authoritative for a PR
        # whose checks were never read.
        row = azure_client._norm_pull(_pr_payload())
        self.assertIsNone(row["checks_counts"])
        self.assertFalse(azure_client.enrichment_complete([row]))

    def test_the_web_link_is_preferred_over_the_composed_one(self):
        # The detail route's payload carries ``_links.web``; the list route's does
        # not. When Azure gives the real link, it wins -- composing would guess at
        # a host for an organization that may be on the legacy one.
        row = azure_client._norm_pull(
            _pr_payload(_links={"web": {"href": "https://example.test/pr/12"}})
        )
        self.assertEqual(row["url"], "https://example.test/pr/12")

    def test_the_composed_link_reads_the_org_from_either_host_form(self):
        legacy = _pr_payload()
        legacy["repository"][
            "url"
        ] = f"https://contoso.visualstudio.com/_apis/git/repositories/{REPO}"
        # The organization is the legacy host's first label, but the LINK is always
        # written against the pinned modern host -- one identity per organization.
        self.assertEqual(
            azure_client._norm_pull(legacy)["url"],
            f"https://{HOST}/contoso/Widgets/_git/{REPO}/pullrequest/12",
        )

    def test_an_uncomposable_link_is_empty_rather_than_wrong(self):
        # A half-built URL would 404 in the user's browser and look like a deleted
        # PR. Empty lets the UI render the row without a link.
        row = azure_client._norm_pull(_pr_payload(repository={"name": REPO}))
        self.assertEqual(row["url"], "")

    def test_a_hostile_org_in_the_api_url_is_refused_not_interpolated(self):
        # The organization arrives from a previous API response and lands in a URL
        # shown to the user, so it goes through the same segment charset every
        # other name does. A malformed authority raises inside urlparse, which must
        # surface as "no organization" rather than as an unhandled 500 in a list route.
        for bad in (
            "https://dev.azure.com/../evil/_apis/git",
            "https://[bad/contoso/_apis/git",
            "https://..visualstudio.com/_apis/git",
            "not a url at all",
            f"https://{HOST}/",
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(azure_client._org_from_api_url(bad))
        self.assertEqual(
            azure_client._org_from_api_url(f"https://{HOST}/contoso/_apis/git"), "contoso"
        )


class TestPullListingAlwaysSendsStatus(unittest.TestCase):
    """``searchCriteria.status`` is passed explicitly on every listing call.

    Azure defaults the criterion to ``active`` SERVER-SIDE. Relying on that
    default is the silent-bug shape: the request succeeds, the rows are
    well-formed, and the closed tab shows open pull requests -- or, once the
    default changes, something else again. So the parameter's presence is asserted
    per call rather than per function.
    """

    def test_the_open_listing_asks_for_active(self):
        az = _Az(git_pullRequests=_paged([_pr_payload()]))
        with az.patch():
            rows = azure_client.list_open_pulls(OWNER, REPO, host=HOST)
        self.assertEqual([r["number"] for r in rows], [12])
        self.assertEqual(
            [q["searchCriteria.status"] for q in az.queries("git/pullRequests")], ["active"]
        )

    def test_status_is_repeated_on_every_page_not_only_the_first(self):
        # Pagination rebuilds the query per page, so the criterion has to be part
        # of the base query rather than something added once by the caller.
        pages = [[_pr_payload(pullRequestId=i) for i in range(100)], [_pr_payload()]]

        def serve(_kwargs: dict) -> object:
            return {"value": pages.pop(0) if pages else []}

        az = _Az(git_pullRequests=serve)
        with az.patch():
            rows = azure_client.list_open_pulls(OWNER, REPO, host=HOST)
        self.assertEqual(len(rows), 101)
        queries = az.queries("git/pullRequests")
        self.assertEqual(len(queries), 2)
        for query in queries:
            self.assertEqual(query["searchCriteria.status"], "active")
        # And the walk really did page by offset rather than re-reading page one.
        self.assertEqual([q["$skip"] for q in queries], [0, 100])

    def test_the_closed_listing_asks_for_all_and_filters_locally(self):
        """ "Closed" spans TWO Azure statuses, so it cannot be a status filter.

        Asking for ``completed`` would hide every abandoned PR and asking for
        ``abandoned`` would hide every merged one; both look like a sparse history
        rather than a missing filter. So the request asks for ``all`` and the
        still-open rows are dropped here.
        """
        az = _Az(
            git_pullRequests=_paged(
                [
                    _pr_payload(
                        pullRequestId=1, status="completed", closedDate="2026-01-01T00:00:00Z"
                    ),
                    _pr_payload(
                        pullRequestId=2, status="abandoned", closedDate="2026-01-02T00:00:00Z"
                    ),
                    _pr_payload(pullRequestId=3, status="active"),
                ]
            )
        )
        with az.patch():
            rows = azure_client.list_closed_pulls(OWNER, REPO, host=HOST)
        self.assertEqual(
            [q["searchCriteria.status"] for q in az.queries("git/pullRequests")], ["all"]
        )
        self.assertEqual([r["number"] for r in rows], [1, 2])
        self.assertNotIn(3, [r["number"] for r in rows], "an open PR reached the closed tab")

    def test_the_first_page_call_is_bounded_to_one_page(self):
        # The progressive first paint must be ONE request: its whole purpose is to
        # beat the paginated fetch to the screen.
        az = _Az(git_pullRequests=_paged([_pr_payload(pullRequestId=i) for i in range(100)]))
        with az.patch():
            rows = azure_client.list_open_pulls_first_page(OWNER, REPO, host=HOST)
        self.assertEqual(len(rows), 100)
        queries = az.queries("git/pullRequests")
        self.assertEqual(len(queries), 1, "the first-page call must not paginate")
        self.assertEqual(queries[0]["searchCriteria.status"], "active")
        self.assertEqual(queries[0]["$top"], azure_client._PAGE_SIZE)

    def test_the_first_page_is_a_prefix_of_the_full_listing(self):
        # Otherwise the full set does not append behind the first paint, it
        # reorders it, and rows visibly jump under the user's cursor.
        rows = [_pr_payload(pullRequestId=i) for i in (9, 8, 7)]
        az = _Az(git_pullRequests=_paged(rows))
        with az.patch():
            first = azure_client.list_open_pulls_first_page(OWNER, REPO, host=HOST)
            full = azure_client.list_open_pulls(OWNER, REPO, host=HOST)
        self.assertEqual(full[: len(first)], first)

    def test_the_repository_is_addressed_by_route_parameter(self):
        # Not by a query qualifier: the name can contain a space, and a route
        # parameter is its own argv element while a qualifier would need quoting.
        az = _Az(git_pullRequests=_paged([]))
        with az.patch():
            azure_client.list_open_pulls(OWNER, "My Repo", host=HOST)
        route = az.calls[0]["route"]
        self.assertEqual(route, {"project": "Widgets", "repositoryId": "My Repo"})


class TestPrDetail(unittest.TestCase):
    """``get_pr_detail`` and its deliberately skippable second call."""

    def setUp(self):
        # ``_project_id`` memoizes per (org, project) process-wide, so a cached
        # entry from another test would hide a missing projects call here.
        azure_client._project_id_cache.clear()

    def _az(self, pr: dict, evaluations: list[dict] | None = None) -> _Az:
        return _Az(
            git_pullRequests=lambda _kwargs: pr,
            core_projects={"id": PROJECT_GUID},
            policy_evaluations={"value": evaluations or []},
        )

    def test_resolve_mergeable_false_skips_the_policy_call_entirely(self):
        """The fast path is about the CALL, not the field.

        A caller that only needs ``head_sha`` for a head-moved check passes
        ``False``; if the policy read happened anyway the flag would be pure
        decoration and every such check would cost two extra round trips.
        """
        az = self._az(_pr_payload())
        with az.patch():
            detail = azure_client.get_pr_detail(OWNER, REPO, 12, host=HOST, resolve_mergeable=False)
        self.assertEqual(detail["mergeable_state"], "unknown")
        self.assertEqual(az.targets(), ["git/pullRequests"])
        self.assertNotIn("policy/evaluations", az.targets())
        # The eagerly-available fields are still there -- that is the point.
        self.assertEqual(detail["head_sha"], SHA)
        self.assertIs(detail["mergeable"], True)

    def test_resolving_the_state_costs_the_policy_call(self):
        az = self._az(
            _pr_payload(),
            [{"status": "approved", "configuration": {"isBlocking": True}}],
        )
        with az.patch():
            detail = azure_client.get_pr_detail(OWNER, REPO, 12, host=HOST)
        self.assertEqual(detail["mergeable_state"], "mergeable")
        self.assertIn("policy/evaluations", az.targets())

    def test_the_detail_row_extends_the_list_row(self):
        # The detail pane renders the same component as the card plus extra
        # fields, so the list row's keys must all still be present.
        az = self._az(_pr_payload())
        with az.patch():
            detail = azure_client.get_pr_detail(OWNER, REPO, 12, host=HOST, resolve_mergeable=False)
        self.assertLessEqual(set(azure_client._norm_pull(_pr_payload())), set(detail))
        # Diff size is None rather than 0: Azure has no per-PR diff statistic, and
        # a zero would present an unread diff as a confident "no changes".
        self.assertIsNone(detail["additions"])
        self.assertIsNone(detail["changed_files"])
        self.assertIsNone(detail["commits"])

    def test_a_merged_pull_request_reports_who_completed_it(self):
        az = self._az(
            _pr_payload(
                status="completed",
                closedDate="2026-02-03T00:00:00Z",
                closedBy={"uniqueName": "grace@contoso.com"},
            )
        )
        with az.patch():
            detail = azure_client.get_pr_detail(OWNER, REPO, 12, host=HOST, resolve_mergeable=False)
        self.assertTrue(detail["merged"])
        self.assertEqual(detail["merged_by"], "grace@contoso.com")

    def test_closed_by_is_not_reported_as_the_merger_of_an_abandoned_pr(self):
        # Azure fills ``closedBy`` when a PR is abandoned too. Reading it as
        # merged_by would credit someone with a merge that never happened.
        az = self._az(
            _pr_payload(
                status="abandoned",
                closedDate="2026-02-03T00:00:00Z",
                closedBy={"uniqueName": "grace@contoso.com"},
            )
        )
        with az.patch():
            detail = azure_client.get_pr_detail(OWNER, REPO, 12, host=HOST, resolve_mergeable=False)
        self.assertFalse(detail["merged"])
        self.assertIsNone(detail["merged_by"])

    def test_auto_merge_reports_the_armed_strategy_and_who_armed_it(self):
        az = self._az(
            _pr_payload(
                autoCompleteSetBy={"id": ADA_GUID, "uniqueName": "ada@contoso.com"},
                completionOptions={"mergeStrategy": "squash"},
            )
        )
        with az.patch():
            detail = azure_client.get_pr_detail(OWNER, REPO, 12, host=HOST, resolve_mergeable=False)
        self.assertEqual(
            detail["auto_merge"], {"method": "SQUASH", "enabled_by": "ada@contoso.com"}
        )

    def test_auto_merge_is_absent_when_no_identity_armed_it(self):
        # Read from the stored IDENTITY rather than from the presence of
        # ``completionOptions``: Azure keeps a merge strategy on a PR that was
        # never armed, so keying on the options would report auto-merge on it.
        for payload in (
            _pr_payload(completionOptions={"mergeStrategy": "squash"}),
            _pr_payload(autoCompleteSetBy={}),
            _pr_payload(autoCompleteSetBy={"id": "not-a-guid"}),
        ):
            with self.subTest(auto=payload.get("autoCompleteSetBy")):
                az = self._az(payload)
                with az.patch():
                    detail = azure_client.get_pr_detail(
                        OWNER, REPO, 12, host=HOST, resolve_mergeable=False
                    )
                self.assertIsNone(detail["auto_merge"])

    def test_the_armed_strategy_is_reported_in_the_apps_vocabulary(self):
        """Including the strategy this module never SETS but Azure's UI can arm.

        ``rebaseMerge`` is arm-able from Azure's own web UI, so a read-side map
        covering only what this module writes would report an armed PR's strategy
        as unknown. An unrecognized strategy stays ``None`` rather than being
        guessed at -- telling the user a squash is coming when Azure will create a
        merge commit is worse than saying nothing.
        """
        cases = {
            "noFastForward": "MERGE",
            "squash": "SQUASH",
            "rebase": "REBASE",
            "rebaseMerge": "REBASE",
            "somethingNew": None,
        }
        for strategy, method in cases.items():
            with self.subTest(strategy=strategy):
                az = self._az(
                    _pr_payload(
                        autoCompleteSetBy={"id": ADA_GUID, "uniqueName": "ada@contoso.com"},
                        completionOptions={"mergeStrategy": strategy},
                    )
                )
                with az.patch():
                    detail = azure_client.get_pr_detail(
                        OWNER, REPO, 12, host=HOST, resolve_mergeable=False
                    )
                self.assertEqual(detail["auto_merge"]["method"], method)
                self.assertEqual(detail["auto_merge"]["enabled_by"], "ada@contoso.com")

    def test_an_empty_payload_raises_rather_than_returning_a_blank_row(self):
        # A row of Nones would render as an untitled PR in the detail pane instead
        # of surfacing the read failure.
        az = _Az(git_pullRequests=lambda _kwargs: {})
        with az.patch():
            with self.assertRaises(ProviderCliError):
                azure_client.get_pr_detail(OWNER, REPO, 12, host=HOST)

    def test_the_number_is_coerced_into_the_route(self):
        # It arrives from a URL path segment; ``int()`` is what keeps a non-numeric
        # value out of the route parameter.
        az = self._az(_pr_payload())
        with az.patch():
            azure_client.get_pr_detail(OWNER, REPO, "12", host=HOST, resolve_mergeable=False)  # type: ignore[arg-type]
        self.assertEqual(az.calls[0]["route"]["pullRequestId"], 12)


class TestMergeableStateIsAnAuthorization(unittest.TestCase):
    """``mergeable`` here means "the route may merge this", so it must be earned.

    ``routes._MERGE_ALLOWED_STATES`` admits the literal ``mergeable``, and Azure's
    own ``mergeStatus`` only knows whether the branches CONFLICT -- it says nothing
    about required reviewers, comment resolution or required builds. So the state
    is reported as mergeable only when every BLOCKING policy evaluation has
    passed, and every other outcome reports a specific reason the route refuses.
    """

    def setUp(self):
        azure_client._project_id_cache.clear()

    def _state(self, raw: dict, evaluations: list[dict] | object) -> str:
        """``_mergeable_state`` with the policy read stubbed to ``evaluations``.

        ``evaluations`` may be an exception instance, for the unreadable case.
        """
        stub = (
            mock.Mock(side_effect=evaluations)
            if isinstance(evaluations, BaseException)
            else mock.Mock(return_value=evaluations)
        )
        with mock.patch.object(azure_client, "_project_id", return_value=PROJECT_GUID):
            with mock.patch.object(azure_client, "_policy_evaluations", stub):
                return azure_client._mergeable_state(
                    "contoso", "Widgets", REPO, raw, host=HOST, timeout=1.0
                )

    def test_the_route_admits_the_string_this_function_can_return(self):
        # The coupling this whole class exists for, asserted rather than assumed:
        # if the route stopped admitting "mergeable", every Azure merge would be
        # refused; if this function widened, the route would admit an unmet policy.
        self.assertIn("mergeable", routes._MERGE_ALLOWED_STATES)
        for refused in (
            azure_client._STATE_BLOCKED,
            azure_client._STATE_DIRTY,
            azure_client._STATE_CHECKING,
            azure_client._STATE_DRAFT,
            azure_client._STATE_UNKNOWN,
        ):
            with self.subTest(state=refused):
                self.assertNotIn(refused, routes._MERGE_ALLOWED_STATES)

    def test_mergeable_needs_every_blocking_evaluation_approved(self):
        state = self._state(
            _pr_payload(),
            [
                {"status": "approved", "configuration": {"isBlocking": True}},
                {"status": "approved", "configuration": {"isBlocking": True}},
            ],
        )
        self.assertEqual(state, "mergeable")

    def test_one_unmet_blocking_policy_blocks_the_merge(self):
        for status in ("queued", "running", "rejected", "broken", "notApplicable", ""):
            with self.subTest(status=status):
                state = self._state(
                    _pr_payload(),
                    [
                        {"status": "approved", "configuration": {"isBlocking": True}},
                        {"status": status, "configuration": {"isBlocking": True}},
                    ],
                )
                self.assertEqual(
                    state,
                    azure_client._STATE_BLOCKED,
                    f"a {status!r} blocking policy presented as mergeable",
                )

    def test_a_non_blocking_policy_does_not_block(self):
        # An optional policy failing is Azure's own definition of "does not
        # prevent completion", so treating it as a blocker would refuse merges
        # Azure allows and push users to the auto-complete path unnecessarily.
        state = self._state(
            _pr_payload(),
            [{"status": "rejected", "configuration": {"isBlocking": False}}],
        )
        self.assertEqual(state, "mergeable")

    def test_a_policy_with_no_isBlocking_key_is_treated_as_blocking(self):
        # Fail closed on a payload shape this module has not seen: assuming
        # optional would let an unmet required policy through.
        state = self._state(_pr_payload(), [{"status": "queued", "configuration": {}}])
        self.assertEqual(state, azure_client._STATE_BLOCKED)

    def test_no_policies_at_all_is_mergeable(self):
        # A repository with no branch policies has nothing left to satisfy once
        # the branches merge cleanly.
        self.assertEqual(self._state(_pr_payload(), []), "mergeable")

    def test_unreadable_policies_fail_closed(self):
        """The gate cannot claim policies are satisfied when it could not read them.

        This is the branch that matters most: a transient policy-read failure
        must not read as "no policies, therefore mergeable".
        """
        state = self._state(_pr_payload(), ProviderCliError("policies unreadable"))
        self.assertEqual(state, azure_client._STATE_UNKNOWN)
        self.assertNotIn(state, routes._MERGE_ALLOWED_STATES)

    def test_a_conflicting_branch_is_dirty_without_reading_policies(self):
        stub = mock.Mock()
        with mock.patch.object(azure_client, "_policy_evaluations", stub):
            state = azure_client._mergeable_state(
                "contoso",
                "Widgets",
                REPO,
                _pr_payload(mergeStatus="conflicts"),
                host=HOST,
                timeout=1.0,
            )
        self.assertEqual(state, azure_client._STATE_DIRTY)
        stub.assert_not_called()

    def test_an_uncomputed_merge_is_checking_not_dirty(self):
        # Reporting dirty mid-computation would flash a false conflict warning on
        # every freshly-opened PR.
        for pending in ("queued", "notSet", "", "none"):
            with self.subTest(pending=pending):
                self.assertEqual(
                    self._state(_pr_payload(mergeStatus=pending), []),
                    azure_client._STATE_CHECKING,
                )

    def test_a_draft_is_reported_as_draft_before_anything_else(self):
        # Even with clean branches and satisfied policies: a draft is not for
        # merging, and the UI has a specific affordance for it.
        state = self._state(_pr_payload(isDraft=True), [])
        self.assertEqual(state, azure_client._STATE_DRAFT)

    def test_an_already_closed_pull_request_is_unknown(self):
        for status in ("completed", "abandoned"):
            with self.subTest(status=status):
                self.assertEqual(
                    self._state(_pr_payload(status=status), []),
                    azure_client._STATE_UNKNOWN,
                )

    def test_a_non_integer_number_is_unknown_and_reads_no_policies(self):
        # The artifact id the policy read is addressed by embeds the number, so a
        # non-integer cannot be resolved -- and must not be interpolated either.
        stub = mock.Mock()
        with mock.patch.object(azure_client, "_policy_evaluations", stub):
            state = azure_client._mergeable_state(
                "contoso",
                "Widgets",
                REPO,
                _pr_payload(pullRequestId="12"),
                host=HOST,
                timeout=1.0,
            )
        self.assertEqual(state, azure_client._STATE_UNKNOWN)
        stub.assert_not_called()

    def test_the_weak_mergeable_flag_stays_weak(self):
        # ``mergeable`` is the conflict answer and nothing more -- it is reported
        # True here while the merge STATE is blocked, which is exactly why the
        # route reads the state instead.
        raw = _pr_payload()
        self.assertIs(azure_client._mergeable(raw), True)
        self.assertEqual(
            self._state(raw, [{"status": "rejected", "configuration": {"isBlocking": True}}]),
            azure_client._STATE_BLOCKED,
        )
        # And an uncomputed status is unknown rather than not-mergeable.
        self.assertIsNone(azure_client._mergeable(_pr_payload(mergeStatus="queued")))
        self.assertIs(azure_client._mergeable(_pr_payload(mergeStatus="conflicts")), False)

    def test_the_policy_artifact_id_carries_the_project_guid(self):
        # Azure addresses evaluations by artifact id, not by PR number, and the id
        # embeds the project GUID -- which is the only reason the project id is
        # resolved at all. A wrong artifact id returns an empty list, which would
        # read as "no policies, therefore mergeable".
        az = _Az(core_projects={"id": PROJECT_GUID}, policy_evaluations={"value": []})
        with az.patch():
            azure_client._policy_evaluations("contoso", "Widgets", 12, host=HOST, timeout=1.0)
        self.assertEqual(
            az.queries("policy/evaluations")[0]["artifactId"],
            f"vstfs:///CodeReview/CodeReviewId/{PROJECT_GUID}/12",
        )


class TestPrTimelineComesFromThreads(unittest.TestCase):
    """Azure keeps PR discussion in THREADS, and writes system entries into them.

    ``commentType == "system"`` covers every vote, reviewer addition and push, so
    keeping those rows would bury the human discussion the timeline exists to
    show. An inline diff comment is not a separate endpoint either -- it is a
    thread carrying a ``threadContext`` -- so both row kinds come from one read.
    """

    def _timeline(self, threads: list[dict]) -> list[dict]:
        az = _Az(git_pullRequestThreads={"value": threads})
        with az.patch():
            return azure_client.list_pr_timeline(OWNER, REPO, 12, host=HOST)

    def test_a_plain_thread_becomes_a_comment_row(self):
        events = self._timeline(
            [
                {
                    "id": 1,
                    "comments": [
                        {
                            "id": 5,
                            "commentType": "text",
                            "content": "Looks good.",
                            "author": {"uniqueName": "ada@contoso.com"},
                            "publishedDate": "2026-01-02T00:00:00Z",
                            "lastUpdatedDate": "2026-01-03T00:00:00Z",
                        }
                    ],
                }
            ]
        )
        self.assertEqual(
            events,
            [
                {
                    "kind": "comment",
                    "id": 5,
                    "actor": "ada@contoso.com",
                    "created_at": "2026-01-02T00:00:00Z",
                    "updated_at": "2026-01-03T00:00:00Z",
                    "body": "Looks good.",
                    "author_association": None,
                    "reactions": None,
                }
            ],
        )

    def test_a_thread_with_a_file_context_becomes_a_review_comment(self):
        # This is what makes a review's SUBSTANCE visible: without the path and
        # line, an inline comment reads as an unanchored remark.
        events = self._timeline(
            [
                {
                    "threadContext": {
                        "filePath": "/src/ledger.py",
                        "rightFileStart": {"line": 42},
                    },
                    "comments": [
                        {
                            "id": 7,
                            "commentType": "text",
                            "content": "Off by one.",
                            "author": {"uniqueName": "grace@contoso.com"},
                            "publishedDate": "2026-01-04T00:00:00Z",
                        }
                    ],
                }
            ]
        )
        self.assertEqual(events[0]["kind"], "review_comment")
        self.assertEqual(events[0]["path"], "/src/ledger.py")
        self.assertEqual(events[0]["line"], 42)

    def test_a_deleted_line_comment_falls_back_to_the_left_side(self):
        # A comment on a removed line has no rightFileStart, so reading only the
        # right side would drop the line number and anchor it at the top of file.
        events = self._timeline(
            [
                {
                    "threadContext": {
                        "filePath": "/src/ledger.py",
                        "leftFileStart": {"line": 9},
                    },
                    "comments": [
                        {
                            "commentType": "text",
                            "content": "Why remove this?",
                            "publishedDate": "2026-01-05T00:00:00Z",
                        },
                    ],
                }
            ]
        )
        self.assertEqual(events[0]["line"], 9)

    def test_system_comments_are_dropped(self):
        events = self._timeline(
            [
                {
                    "comments": [
                        {
                            "commentType": "system",
                            "content": "ada added grace as a reviewer",
                            "publishedDate": "2026-01-01T00:00:00Z",
                        },
                        {
                            "commentType": "text",
                            "content": "Real remark.",
                            "publishedDate": "2026-01-02T00:00:00Z",
                        },
                    ]
                }
            ]
        )
        self.assertEqual([e["body"] for e in events], ["Real remark."])

    def test_an_empty_comment_body_is_dropped(self):
        # Azure keeps a placeholder comment for some thread operations; rendering
        # it would show an empty speech bubble attributed to a real person.
        self.assertEqual(
            self._timeline([{"comments": [{"commentType": "text", "content": "   "}]}]),
            [],
        )

    def test_a_vote_thread_becomes_a_reviewed_row(self):
        # The vote is recoverable ONLY from the system thread that records it,
        # which is why those threads are inspected before being dropped.
        events = self._timeline(
            [
                {
                    "publishedDate": "2026-01-06T00:00:00Z",
                    "properties": {"CodeReviewVoteResult": {"type": "System.Int32", "$value": 10}},
                    "comments": [
                        {
                            "commentType": "system",
                            "content": "voted",
                            "author": {"uniqueName": "grace@contoso.com"},
                        },
                    ],
                }
            ]
        )
        self.assertEqual(
            events,
            [
                {
                    "kind": "reviewed",
                    "actor": "grace@contoso.com",
                    "created_at": "2026-01-06T00:00:00Z",
                    "review_state": "APPROVED",
                    "body": "",
                }
            ],
        )

    def test_every_vote_value_maps_to_a_state_the_pane_renders(self):
        # Including 5 ("approved with suggestions"), which IS an approval: a
        # fourth state would render as nothing at all.
        expected = {
            10: "APPROVED",
            5: "APPROVED",
            0: "COMMENTED",
            -5: "CHANGES_REQUESTED",
            -10: "CHANGES_REQUESTED",
        }
        for vote, state in expected.items():
            with self.subTest(vote=vote):
                events = self._timeline(
                    [{"properties": {"CodeReviewVoteResult": {"$value": vote}}, "comments": []}]
                )
                self.assertEqual(events[0]["review_state"], state)

    def test_a_vote_written_as_a_string_is_still_read(self):
        # Azure has written this property as both a number and a numeric string,
        # and an untyped read would bucket the string as COMMENTED -- silently
        # losing an approval.
        events = self._timeline([{"properties": {"CodeReviewVoteResult": " -10 "}, "comments": []}])
        self.assertEqual(events[0]["review_state"], "CHANGES_REQUESTED")

    def test_an_unparseable_vote_degrades_to_commented(self):
        events = self._timeline(
            [{"properties": {"CodeReviewVoteResult": {"$value": "wat"}}, "comments": []}]
        )
        self.assertEqual(events[0]["review_state"], "COMMENTED")

    def test_events_are_ordered_by_time_across_threads(self):
        # The pane renders the list as given, and Azure returns threads in its own
        # order, so a per-thread order would interleave a reply before its parent.
        events = self._timeline(
            [
                {
                    "comments": [
                        {
                            "commentType": "text",
                            "content": "second",
                            "publishedDate": "2026-02-02T00:00:00Z",
                        }
                    ]
                },
                {
                    "comments": [
                        {
                            "commentType": "text",
                            "content": "first",
                            "publishedDate": "2026-01-01T00:00:00Z",
                        }
                    ]
                },
            ]
        )
        self.assertEqual([e["body"] for e in events], ["first", "second"])


class TestPrChecks(unittest.TestCase):
    """Policy evaluations AND builds, bucketed into one shared vocabulary.

    Azure has two unrelated things that both mean "a check on this PR", and the
    card renders one list with one summary dot. The buckets are the contract:
    ``gitlab_client._norm_job``'s vocabulary, including its rule that a cancelled
    unit is informational rather than failing.
    """

    def setUp(self):
        azure_client._project_id_cache.clear()

    def _az(
        self,
        *,
        builds: list[dict] | None = None,
        pulls: list[dict] | None = None,
        evaluations: list[dict] | object = None,
    ) -> _Az:
        policy: object = {"value": evaluations if evaluations is not None else []}
        if isinstance(evaluations, BaseException):

            def raiser(_kwargs: dict) -> object:
                raise evaluations

            policy = raiser
        return _Az(
            build_builds={"value": builds or []},
            git_pullRequests=_paged(pulls if pulls is not None else [_pr_payload()]),
            core_projects={"id": PROJECT_GUID},
            policy_evaluations=policy,
        )

    def test_builds_and_policies_are_returned_as_one_list(self):
        az = self._az(
            builds=[
                {
                    "status": "completed",
                    "result": "succeeded",
                    "definition": {"name": "ci"},
                    "buildNumber": "20260102.1",
                    "startTime": "2026-01-02T00:00:00Z",
                    "finishTime": "2026-01-02T00:10:00Z",
                    "sourceVersion": SHA,
                    "_links": {"web": {"href": "https://example.test/build/1"}},
                }
            ],
            evaluations=[
                {
                    "status": "rejected",
                    "configuration": {
                        "isBlocking": True,
                        "type": {"id": "policy-guid", "displayName": "Required reviewers"},
                        "settings": {"displayName": "Two reviewers"},
                    },
                }
            ],
        )
        with az.patch():
            rows = azure_client.list_pr_checks(OWNER, REPO, SHA, host=HOST)
        self.assertEqual(
            [(r["name"], r["bucket"], r["app"]) for r in rows],
            [
                ("ci", "success", "Azure Pipelines"),
                ("Two reviewers", "failure", "Azure DevOps policy"),
            ],
        )
        self.assertEqual(rows[0]["url"], "https://example.test/build/1")
        # ``source`` is the dedupe key, and the policy TYPE is what makes a
        # required-reviewers policy distinct from a build policy rather than one lump.
        self.assertEqual(rows[1]["source"], "policy-guid")

    def test_a_build_for_another_commit_is_not_reported(self):
        # Azure's build list has no commit filter, so the match happens locally.
        # A run against an older commit describes code that no longer exists.
        az = self._az(
            builds=[
                {
                    "status": "completed",
                    "result": "failed",
                    "definition": {"name": "old"},
                    "sourceVersion": "b" * 40,
                },
                {
                    "status": "completed",
                    "result": "succeeded",
                    "definition": {"name": "mine"},
                    "sourceVersion": SHA.upper(),
                },  # matched case-insensitively
            ]
        )
        with az.patch():
            rows = azure_client.list_pr_checks(OWNER, REPO, SHA, host=HOST)
        self.assertEqual([r["name"] for r in rows], ["mine"])

    def test_build_results_land_in_the_shared_buckets(self):
        cases = {
            ("completed", "succeeded"): "success",
            ("completed", "failed"): "failure",
            # Azure's "partially succeeded" means some tasks failed but were
            # ALLOWED to, so a red dot would contradict Azure's own verdict.
            ("completed", "partiallySucceeded"): "other",
            ("completed", "canceled"): "other",
            ("inProgress", ""): "running",
            ("notStarted", ""): "running",
            ("postponed", ""): "running",
            # A status this module has not seen is informational rather than
            # running: claiming "running" would make the card spin forever.
            ("somethingNew", ""): "other",
        }
        for (status, result), bucket in cases.items():
            with self.subTest(status=status, result=result):
                row = azure_client._norm_build(
                    {"status": status, "result": result, "definition": {"name": "ci"}}
                )
                self.assertEqual(row["bucket"], bucket)
                # ``conclusion`` must agree with the bucket: the pane colours the
                # row from one and the dot from the other.
                self.assertEqual(
                    row["conclusion"],
                    {"success": "success", "failure": "failure", "running": None}.get(
                        bucket, "neutral"
                    ),
                )

    def test_evaluation_statuses_land_in_the_shared_buckets(self):
        cases = {
            "approved": "success",
            "rejected": "failure",
            "broken": "failure",
            "queued": "running",
            "running": "running",
            "notApplicable": "other",
            "surprise": "other",
        }
        for status, bucket in cases.items():
            with self.subTest(status=status):
                row = azure_client._norm_evaluation(
                    {"status": status, "configuration": {"isBlocking": True}}
                )
                self.assertEqual(row["bucket"], bucket)

    def test_an_optional_failing_policy_is_informational_not_failing(self):
        # It cannot block completion, so bucketing it as failure would paint the
        # card red for something Azure permits -- and the summary dot follows the
        # worst bucket, so one optional policy would mask a green PR.
        row = azure_client._norm_evaluation(
            {"status": "rejected", "configuration": {"isBlocking": False}}
        )
        self.assertEqual(row["bucket"], "other")

    def test_a_policy_with_no_setting_name_falls_back_to_the_policy_type(self):
        # An unnamed row would render as a blank check the user cannot identify.
        row = azure_client._norm_evaluation(
            {"status": "approved", "configuration": {"type": {"displayName": "Comment resolution"}}}
        )
        self.assertEqual(row["name"], "Comment resolution")
        self.assertEqual(row["summary"], "")

    def test_an_invalid_sha_is_refused_before_reaching_a_query_string(self):
        # ``sha`` is the one value here that originates from a previous API
        # response rather than from a connected-repo record.
        az = self._az()
        with az.patch():
            for bad in ("", "not-a-sha", "../../etc", SHA + "z"):
                with self.subTest(bad=bad), self.assertRaises(ProviderCliError):
                    azure_client.list_pr_checks(OWNER, REPO, bad, host=HOST)
        self.assertEqual(az.calls, [], "a bad sha reached the provider")

    def test_unreadable_policies_do_not_blank_the_builds(self):
        # Partial data beats no data here: the builds were read successfully and
        # returning nothing would present a checked commit as unchecked.
        az = self._az(
            builds=[
                {
                    "status": "completed",
                    "result": "succeeded",
                    "definition": {"name": "ci"},
                    "sourceVersion": SHA,
                }
            ],
            evaluations=ProviderCliError("policies unreadable"),
        )
        with az.patch():
            rows = azure_client.list_pr_checks(OWNER, REPO, SHA, host=HOST)
        self.assertEqual([r["name"] for r in rows], ["ci"])

    def test_a_commit_with_no_pull_request_still_reports_its_builds(self):
        # A commit on a branch with no open PR has no policy evaluations at all,
        # which is not an error.
        az = self._az(
            builds=[
                {
                    "status": "completed",
                    "result": "succeeded",
                    "definition": {"name": "ci"},
                    "sourceVersion": SHA,
                }
            ],
            pulls=[_pr_payload(lastMergeSourceCommit={"commitId": "b" * 40})],
        )
        with az.patch():
            rows = azure_client.list_pr_checks(OWNER, REPO, SHA, host=HOST)
        self.assertEqual([r["name"] for r in rows], ["ci"])
        self.assertNotIn("policy/evaluations", az.targets())

    def test_an_unreadable_pull_request_lookup_does_not_blank_the_builds(self):
        # Same reasoning as the policy failure, one step earlier: the PR lookup is
        # only how the policy half is ADDRESSED, so losing it must not lose the
        # builds that were already read.
        def pulls(_kwargs: dict) -> object:
            raise ProviderCliError("pull request list unreadable")

        az = self._az(
            builds=[
                {
                    "status": "completed",
                    "result": "succeeded",
                    "definition": {"name": "ci"},
                    "sourceVersion": SHA,
                }
            ],
        )
        az._handlers["git_pullRequests"] = pulls  # type: ignore[index]
        with az.patch():
            rows = azure_client.list_pr_checks(OWNER, REPO, SHA, host=HOST)
        self.assertEqual([r["name"] for r in rows], ["ci"])

    def test_the_pull_request_lookup_asks_for_active_before_all(self):
        # Ordered deliberately: a check read is overwhelmingly for an open PR, and
        # the status is explicit on BOTH passes for the same reason as the listings.
        az = self._az(pulls=[_pr_payload(lastMergeSourceCommit={"commitId": "b" * 40})])
        with az.patch():
            azure_client.list_pr_checks(OWNER, REPO, SHA, host=HOST)
        self.assertEqual(
            [q["searchCriteria.status"] for q in az.queries("git/pullRequests")],
            ["active", "all"],
        )


class TestSummarizeChecks(unittest.TestCase):
    """The card's dot must never read greener than the list it summarizes."""

    def test_the_output_shape_is_the_shared_contract(self):
        out = azure_client.summarize_checks([{"bucket": "success"}])
        self.assertEqual(set(out), {"checks_counts", "checks_state", "checks_truncated"})
        self.assertEqual(
            out,
            {
                "checks_counts": {"failure": 0, "running": 0, "success": 1, "other": 0},
                "checks_state": "success",
                "checks_truncated": False,
            },
        )

    def test_anything_failing_dominates(self):
        out = azure_client.summarize_checks(
            [{"bucket": "success"}, {"bucket": "running"}, {"bucket": "failure"}]
        )
        self.assertEqual(out["checks_state"], "failure")

    def test_running_beats_success(self):
        out = azure_client.summarize_checks([{"bucket": "success"}, {"bucket": "running"}])
        self.assertEqual(out["checks_state"], "running")

    def test_informational_only_is_reported_as_other(self):
        self.assertEqual(
            azure_client.summarize_checks([{"bucket": "other"}])["checks_state"], "other"
        )

    def test_no_checks_at_all_is_none_so_the_card_shows_no_dot(self):
        # Distinct from "all checks passed": a PR with no pipeline must not show a
        # green tick it never earned.
        out = azure_client.summarize_checks([])
        self.assertIsNone(out["checks_state"])
        self.assertEqual(
            out["checks_counts"], {"failure": 0, "running": 0, "success": 0, "other": 0}
        )

    def test_an_unrecognized_bucket_counts_as_informational(self):
        # Never dropped: a row that vanished from the counts would make the total
        # disagree with the list beneath it.
        out = azure_client.summarize_checks(
            [{"bucket": "wat"}, {}, {"bucket": None}, "not a dict"]  # type: ignore[list-item]
        )
        self.assertEqual(out["checks_counts"]["other"], 3)


class TestEnrichPulls(unittest.TestCase):
    """Azure's list payload carries no check state, so enrichment is a real read.

    One call per PR, bounded -- and a row that could not be enriched must keep
    ``checks_counts: None`` so the whole set stays OUT of the on-disk cache
    rather than being persisted as having no checks.
    """

    def setUp(self):
        azure_client._project_id_cache.clear()

    def _rows(self, count: int) -> list[dict]:
        return [azure_client._norm_pull(_pr_payload(pullRequestId=i)) for i in range(1, count + 1)]

    def test_each_row_gets_its_own_check_summary(self):
        az = _Az(
            core_projects={"id": PROJECT_GUID},
            policy_evaluations={
                "value": [{"status": "rejected", "configuration": {"isBlocking": True}}]
            },
        )
        rows = self._rows(2)
        with az.patch():
            out = azure_client.enrich_pulls(OWNER, REPO, rows, "open", host=HOST)
        self.assertIs(out, rows, "enrichment is documented as in-place")
        for row in out:
            self.assertEqual(row["checks_state"], "failure")
            self.assertEqual(row["checks_counts"]["failure"], 1)
        self.assertTrue(azure_client.enrichment_complete(out))
        # One policy read per PR -- the reason the fan-out is bounded at all.
        self.assertEqual(az.targets().count("policy/evaluations"), 2)

    def test_state_is_accepted_for_parity_and_changes_nothing(self):
        # The route passes whichever tab it is serving; Azure has no cheaper read
        # for either, so an open and a closed enrichment must agree.
        az = _Az(core_projects={"id": PROJECT_GUID}, policy_evaluations={"value": []})
        with az.patch():
            opened = azure_client.enrich_pulls(OWNER, REPO, self._rows(1), "open", host=HOST)
            closed = azure_client.enrich_pulls(OWNER, REPO, self._rows(1), "closed", host=HOST)
            by_number = azure_client.enrich_pulls_by_number(OWNER, REPO, self._rows(1), host=HOST)
        self.assertEqual(opened, closed)
        self.assertEqual(opened, by_number)

    def test_a_per_pr_failure_leaves_that_row_unenriched(self):
        """The failing row must stay unenriched, and the SET must stay uncached.

        Zeroing its counts would persist "this PR has no checks" as authoritative
        for a PR whose checks simply could not be read.
        """

        def evaluations(kwargs: dict) -> object:
            if "/2" in str(_query(kwargs, "artifactId")):
                raise ProviderCliError("policy read failed")
            return {"value": [{"status": "approved", "configuration": {"isBlocking": True}}]}

        az = _Az(core_projects={"id": PROJECT_GUID}, policy_evaluations=evaluations)
        with az.patch():
            out = azure_client.enrich_pulls(OWNER, REPO, self._rows(2), "open", host=HOST)
        self.assertEqual(out[0]["checks_state"], "success")
        self.assertIsNone(out[1]["checks_counts"])
        self.assertFalse(
            azure_client.enrichment_complete(out),
            "a partially enriched set must not be cached",
        )

    def test_rows_past_the_bound_stay_unenriched_rather_than_zeroed(self):
        # The bound is what stops one list view from making hundreds of calls; the
        # incomplete answer is then honest about itself via enrichment_complete.
        az = _Az(core_projects={"id": PROJECT_GUID}, policy_evaluations={"value": []})
        rows = self._rows(azure_client._ENRICH_MAX_PULLS + 3)
        with az.patch():
            out = azure_client.enrich_pulls(OWNER, REPO, rows, "open", host=HOST)
        self.assertEqual(az.targets().count("policy/evaluations"), azure_client._ENRICH_MAX_PULLS)
        self.assertIsNone(out[-1]["checks_counts"])
        self.assertFalse(azure_client.enrichment_complete(out))

    def test_a_row_without_a_usable_number_is_skipped_not_fatal(self):
        # One malformed row must not fail the whole list fetch.
        az = _Az(core_projects={"id": PROJECT_GUID}, policy_evaluations={"value": []})
        rows = [{"number": None}, *self._rows(1)]
        with az.patch():
            out = azure_client.enrich_pulls(OWNER, REPO, rows, "open", host=HOST)
        self.assertEqual(az.targets().count("policy/evaluations"), 1)
        self.assertFalse(azure_client.enrichment_complete(out))

    def test_enrichment_complete_reads_only_the_check_counts(self):
        self.assertTrue(azure_client.enrichment_complete([]))
        self.assertTrue(azure_client.enrichment_complete([{"checks_counts": {}}]))
        self.assertFalse(azure_client.enrichment_complete([{"checks_counts": None}]))
        self.assertFalse(azure_client.enrichment_complete([{}]))


def _query(kwargs: dict, key: str) -> object:
    return dict(kwargs.get("query") or {}).get(key)


class TestBuildPrSearchQuery(unittest.TestCase):
    """One caller-facing signature, three provider-specific query languages.

    The route hands the same keyword arguments to whichever client it holds, so a
    provider-specific spelling here would be a ``TypeError`` at request time
    rather than a compile error. Azure's answer is a ``searchCriteria`` fragment
    with the person filters left as PLACEHOLDERS, because its criteria take
    identity GUIDs and resolving one is a network call a pure function must not make.
    """

    def test_an_author_filter_becomes_a_creatorId_placeholder(self):
        self.assertEqual(
            azure_client.build_pr_search_query(OWNER, REPO, state="open", author="ada"),
            "searchCriteria.status=active&searchCriteria.creatorId={creatorId}",
        )

    def test_the_repository_is_discarded_because_it_is_a_route_parameter(self):
        fragment = azure_client.build_pr_search_query(OWNER, REPO, author="ada")
        self.assertNotIn("contoso", fragment)
        self.assertNotIn("Widgets", fragment)
        self.assertNotIn(REPO, fragment)

    def test_assignee_maps_onto_reviewer_because_azure_has_no_assignee(self):
        # A reviewer is the nearest thing the criteria can express. Silently
        # dropping the filter instead would return every open PR as that person's.
        self.assertEqual(
            azure_client.build_pr_search_query(OWNER, REPO, assignee="ada"),
            "searchCriteria.status=active&searchCriteria.reviewerId={reviewerId}",
        )
        self.assertEqual(
            azure_client.build_pr_search_query(OWNER, REPO, review_requested="ada"),
            "searchCriteria.status=active&searchCriteria.reviewerId={reviewerId}",
        )

    def test_review_requested_wins_over_assignee_on_the_one_criterion(self):
        # Both map to reviewerId, so they cannot be combined -- and the explicit
        # review filter is the one the user actually asked for.
        fragment = azure_client.build_pr_search_query(
            OWNER, REPO, assignee="ada", review_requested="grace"
        )
        self.assertEqual(fragment.count("reviewerId"), 2)  # the key and its placeholder

    def test_author_and_reviewer_are_both_emitted(self):
        self.assertEqual(
            azure_client.build_pr_search_query(OWNER, REPO, author="ada", assignee="grace"),
            "searchCriteria.status=active"
            "&searchCriteria.creatorId={creatorId}"
            "&searchCriteria.reviewerId={reviewerId}",
        )

    def test_each_state_maps_onto_azures_own_status_vocabulary(self):
        # Azure has no "merged": a merged PR is ``completed`` and an abandoned one
        # is ``abandoned``, so "closed" must mean abandoned -- closed WITHOUT a
        # merge, which is what the route's closed tab shows on GitHub too.
        expected = {"open": "active", "closed": "abandoned", "merged": "completed", "all": "all"}
        for state, status in expected.items():
            with self.subTest(state=state):
                fragment = azure_client.build_pr_search_query(
                    OWNER, REPO, state=state, author="ada"
                )
                self.assertIn(f"searchCriteria.status={status}", fragment)

    def test_an_unknown_state_is_refused(self):
        with self.assertRaises(PrSearchError):
            azure_client.build_pr_search_query(OWNER, REPO, state="wat", author="ada")

    def test_a_search_with_no_person_filter_is_refused(self):
        # It would just duplicate the list endpoint while looking like a filter.
        with self.assertRaises(PrSearchError):
            azure_client.build_pr_search_query(OWNER, REPO, state="open")

    def test_an_invalid_login_is_refused(self):
        # ``&`` is the one that matters most: the fragment is split on it, so a
        # login carrying one would become a second criterion.
        for bad in ("ada&x=1", "ada/../grace", "-ada", "a" * 300, "ada\tx"):
            with self.subTest(bad=bad), self.assertRaises(PrSearchError):
                azure_client.build_pr_search_query(OWNER, REPO, author=bad)

    def test_a_search_error_is_a_value_error_not_a_provider_error(self):
        # The route maps PrSearchError to a 400 and ProviderCliError to a 502, so
        # confusing the two turns a user's bad filter into an outage report.
        self.assertTrue(issubclass(PrSearchError, ValueError))
        self.assertFalse(issubclass(PrSearchError, ProviderCliError))


class TestSearchPulls(unittest.TestCase):
    """The placeholders are substituted with real identity GUIDs, or it raises.

    Azure IGNORES a ``creatorId``/``reviewerId`` it cannot parse rather than
    rejecting it, so an unresolved login would silently widen "PRs by ada" to
    every open PR in the repository -- which is why an unresolvable login has to
    raise instead of being passed through.
    """

    def _roster_az(self, **extra: object) -> _Az:
        return _Az(
            core_teams={"value": [{"id": TEAM_GUID}]},
            core_members={
                "value": [
                    {
                        "identity": {
                            "id": ADA_GUID,
                            "uniqueName": "ada@contoso.com",
                            "displayName": "Ada Lovelace",
                        }
                    }
                ]
            },
            git_pullRequests=_paged([_pr_payload()]),
            **extra,
        )

    def test_the_author_filter_is_sent_as_a_guid(self):
        az = self._roster_az()
        with az.patch():
            rows = azure_client.search_pulls(OWNER, REPO, host=HOST, author="ada@contoso.com")
        self.assertEqual([r["number"] for r in rows], [12])
        query = az.queries("git/pullRequests")[0]
        self.assertEqual(query["searchCriteria.creatorId"], ADA_GUID)
        self.assertEqual(query["searchCriteria.status"], "active")
        # No placeholder survived into the request.
        self.assertNotIn("{creatorId}", str(query))

    def test_the_reviewer_filter_is_sent_as_a_guid(self):
        az = self._roster_az()
        with az.patch():
            azure_client.search_pulls(OWNER, REPO, host=HOST, assignee="ada@contoso.com")
        self.assertEqual(az.queries("git/pullRequests")[0]["searchCriteria.reviewerId"], ADA_GUID)

    def test_a_display_name_also_resolves(self):
        # The picker shows display names, so a user filtering from the UI supplies
        # one -- matched case-insensitively because Azure's own display is.
        az = self._roster_az()
        with az.patch():
            azure_client.search_pulls(OWNER, REPO, host=HOST, author="ada lovelace")
        self.assertEqual(az.queries("git/pullRequests")[0]["searchCriteria.creatorId"], ADA_GUID)

    def test_an_unresolvable_login_raises_rather_than_widening_the_search(self):
        az = self._roster_az()
        with az.patch():
            with self.assertRaises(PrSearchError):
                azure_client.search_pulls(OWNER, REPO, host=HOST, author="nobody")
        self.assertNotIn("git/pullRequests", az.targets(), "an unfiltered search was sent anyway")

    def test_an_unreadable_team_roster_raises_a_search_error(self):
        # Fail closed, and as a SEARCH error: the identity could not be
        # established, so the filter cannot be honoured.
        def teams(_kwargs: dict) -> object:
            raise ProviderCliError("teams unreadable")

        az = _Az(core_teams=teams)
        with az.patch():
            with self.assertRaises(PrSearchError):
                azure_client.search_pulls(OWNER, REPO, host=HOST, author="ada@contoso.com")

    def test_a_team_that_cannot_be_read_does_not_abort_the_others(self):
        # A roster spread over several teams must not be defeated by one team the
        # caller lacks access to.
        teams = [
            {"id": TEAM_GUID},
            {"id": "not-a-guid"},
            {"id": "11111111-2222-3333-4444-555555555555"},
        ]
        seen: list[str] = []

        def members(kwargs: dict) -> object:
            team = str(dict(kwargs["route"])["teamId"])  # type: ignore[arg-type]
            seen.append(team)
            if team == TEAM_GUID:
                raise ProviderCliError("forbidden")
            return {"value": [{"id": ADA_GUID, "uniqueName": "ada@contoso.com"}]}

        az = _Az(
            core_teams={"value": teams},
            core_members=members,
            git_pullRequests=_paged([_pr_payload()]),
        )
        with az.patch():
            azure_client.search_pulls(OWNER, REPO, host=HOST, author="ada@contoso.com")
        # The malformed team id was never sent as a route parameter.
        self.assertNotIn("not-a-guid", seen)
        self.assertEqual(az.queries("git/pullRequests")[0]["searchCriteria.creatorId"], ADA_GUID)

    def test_a_member_without_a_guid_is_not_accepted_as_a_match(self):
        # The value reaches an argv, so it is validated as a GUID rather than
        # trusted from the response.
        az = _Az(
            core_teams={"value": [{"id": TEAM_GUID}]},
            core_members={"value": [{"id": "nope", "uniqueName": "ada@contoso.com"}]},
        )
        with az.patch():
            with self.assertRaises(PrSearchError):
                azure_client.search_pulls(OWNER, REPO, host=HOST, author="ada@contoso.com")

    def test_the_rows_are_the_same_shape_as_the_listing(self):
        # The frontend swaps data sources between list and search without a second
        # row type, so any divergence renders as blank cells in search results only.
        az = self._roster_az()
        with az.patch():
            searched = azure_client.search_pulls(OWNER, REPO, host=HOST, author="ada@contoso.com")
            listed = azure_client.list_open_pulls(OWNER, REPO, host=HOST)
        self.assertEqual(searched, listed)

    def test_the_limit_keeps_one_row_above_the_display_cap(self):
        """The route asks for one MORE row than it shows, to detect truncation.

        Clamping to ``PR_SEARCH_MAX`` would discard that sentinel and present
        every over-cap result set as complete.
        """
        az = self._roster_az()
        with az.patch():
            azure_client.search_pulls(
                OWNER,
                REPO,
                host=HOST,
                author="ada@contoso.com",
                limit=azure_client.PR_SEARCH_MAX + 500,
            )
        # ``$top`` is capped per page, so the ceiling shows up as the paged walk's
        # budget rather than in one query -- asserted via the returned row cap below.
        self.assertTrue(az.queries("git/pullRequests"))

    def test_the_returned_rows_are_capped(self):
        rows = [_pr_payload(pullRequestId=i) for i in range(5)]
        az = self._roster_az()
        az._handlers["git_pullRequests"] = _paged(rows)  # type: ignore[index]
        with az.patch():
            out = azure_client.search_pulls(
                OWNER, REPO, host=HOST, author="ada@contoso.com", limit=2
            )
        self.assertEqual(len(out), 2)

    def test_a_zero_or_negative_limit_still_returns_a_row(self):
        # A clamp to zero would make a legitimate search look empty.
        az = self._roster_az()
        with az.patch():
            out = azure_client.search_pulls(
                OWNER, REPO, host=HOST, author="ada@contoso.com", limit=0
            )
        self.assertEqual(len(out), 1)

    def test_the_closed_search_excludes_merged_pull_requests(self):
        # Belt to the status filter: "closed" means closed WITHOUT being merged,
        # matching the GitHub path, so a completed PR must not appear there.
        rows = [
            _pr_payload(pullRequestId=1, status="completed", closedDate="2026-01-01T00:00:00Z"),
            _pr_payload(pullRequestId=2, status="abandoned", closedDate="2026-01-02T00:00:00Z"),
        ]
        az = self._roster_az()
        az._handlers["git_pullRequests"] = _paged(rows)  # type: ignore[index]
        with az.patch():
            out = azure_client.search_pulls(
                OWNER, REPO, host=HOST, author="ada@contoso.com", state="closed"
            )
        self.assertEqual([r["number"] for r in out], [2])

    def test_trailing_whitespace_does_not_defeat_the_lookup(self):
        # A login pasted from the picker can carry a trailing space -- the charset
        # permits it (Azure display names contain spaces), so the resolver has to
        # strip it. Otherwise a person filter fails on a stray character and reads
        # as "this user has no pull requests".
        az = self._roster_az()
        with az.patch():
            azure_client.search_pulls(OWNER, REPO, host=HOST, author="ada@contoso.com ")
        self.assertEqual(az.queries("git/pullRequests")[0]["searchCriteria.creatorId"], ADA_GUID)

    def test_an_invalid_login_never_reaches_the_provider(self):
        az = _Az()
        with az.patch():
            with self.assertRaises(PrSearchError):
                azure_client.search_pulls(OWNER, REPO, host=HOST, author="ada&x=1")
        self.assertEqual(az.calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
