"""Tests for the pull-request ACTION surface (close/reopen, review, comment,
auto-merge, CI cancel/re-run) and its bulk endpoint.

Four deterministic, subprocess-free surfaces:
  * client write primitives — argv/payload shaping and the refusals, exercised by
    monkeypatching ``_run_gh_write`` / ``_gh_run`` so nothing spawns a real CLI;
  * the store's post-action cache coherence (a closed PR leaves the open list and
    its detail entry is dropped);
  * the route validators — the bulk ``numbers`` parser and body bounds, which are
    the input-validation gate for a mass mutation;
  * the bulk endpoint's partial-failure contract: one failing PR must not fail the
    rows that succeeded, and must be reported rather than swallowed.

Two design invariants carry most of the weight here, and both are pinned by tests
rather than only described in comments:

  * ``TestMergeBoundaries`` — merging IS offered, in two forms, but nothing may shed a
    required check ("override and merge"), and merge stays off the bulk allowlist.
  * ``TestReviewIsPinnedToACommit`` — every write that states something about a
    REVISION (a merge, a review) names the commit it was formed on, so a force-push
    between the render and the click is refused rather than silently re-targeted.
"""
import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client as gl
from kiro_crew.apps.builtins.issue_radar.backend import provider, routes, store


class TestMergeBoundaries(unittest.TestCase):
    """What the app will and will NOT do about merging.

    Merging IS offered, and it cannot bypass a gate: branch protection, required
    reviews and required checks are enforced by the provider on its own merge
    endpoint, so an unsatisfied PR is refused server-side. What stays out is a BULK
    merge (irreversible, and 50 from one click is a blast radius no confirmation
    makes reasonable) and anything that would shed a required check -- an "override
    and merge" is the one thing the provider would not adjudicate for us, and in this
    repository an override is a reviewed comment plus a label, not a button.
    """

    def test_merge_is_offered_on_both_providers(self):
        for mod in (gh, gl):
            self.assertTrue(
                callable(getattr(mod, "merge_pull_request", None)),
                f"{mod.__name__} has no merge_pull_request",
            )

    def test_no_override_primitive_exists(self):
        """Nothing that lands a PR while shedding a required check."""
        for mod in (gh, gl):
            for name in dir(mod):
                if name.startswith("_"):
                    continue
                self.assertNotIn(
                    name,
                    ("force_merge", "override_and_merge", "merge_with_override",
                     "admin_merge", "bypass_checks"),
                    f"{mod.__name__}.{name} looks like a gate-bypassing merge",
                )

    def test_bulk_allowlist_excludes_merge_and_override(self):
        """Bulk is a fixed allowlist; merge is deliberately absent from it."""
        self.assertNotIn("merge", routes._BULK_PR_ACTIONS)
        for verb in routes._BULK_PR_ACTIONS:
            self.assertNotIn("override", verb)
        # Arming auto-merge IS allowed in bulk -- it is reversible from the same UI
        # and the provider still decides each one.
        self.assertIn("auto_merge", routes._BULK_PR_ACTIONS)
        self.assertIn("cancel_auto_merge", routes._BULK_PR_ACTIONS)

    def test_bulk_endpoint_refuses_merge(self):
        """Not just absent from the tuple -- actually refused by the handler."""
        with mock.patch.object(routes, "_connected", return_value=True), \
                mock.patch.object(routes, "_repo_can_write", return_value=True), \
                mock.patch.object(routes, "_run_pr_action") as run:
            resp = _await(routes._handle_pulls_bulk(
                _req({"owner": "o", "repo": "r", "numbers": [1, 2], "action": "merge"})
            ))
            run.assert_not_called()
        self.assertEqual(resp.status, 400)


class TestMergePrimitive(unittest.TestCase):
    def test_merge_uses_the_providers_own_merge_endpoint(self):
        """Which is what enforces branch protection -- the reason this is safe."""
        with mock.patch.object(
            gh, "_run_gh_write", return_value={"merged": True, "sha": "abc", "message": "ok"}
        ) as m:
            out = gh.merge_pull_request("o", "r", 7, "SQUASH", "abc1234")
        self.assertEqual(m.call_args[0][0], "PUT")
        self.assertEqual(m.call_args[0][1], "repos/o/r/pulls/7/merge")
        self.assertEqual(m.call_args[0][2], {"merge_method": "squash", "sha": "abc1234"})
        self.assertTrue(out["merged"])
        self.assertEqual(out["sha"], "abc")

    def test_invalid_method_is_refused_before_any_call(self):
        with mock.patch.object(gh, "_run_gh_write") as m:
            with self.assertRaises(gh.GhCliError):
                gh.merge_pull_request("o", "r", 7, "FASTFORWARD", "abc1234")
            m.assert_not_called()

    def test_merge_refuses_without_the_reviewed_head_commit(self):
        """The merge is PINNED to the commit the caller looked at. Without the pin, a
        push landing between the read and the click merges code nobody reviewed — and
        on a repo with no branch protection nothing else catches that."""
        for bad in ("", "   ", "not-hex", "zz"):
            with mock.patch.object(gh, "_run_gh_write") as m:
                with self.assertRaises(gh.GhCliError):
                    gh.merge_pull_request("o", "r", 7, "SQUASH", bad)
                m.assert_not_called()
            with mock.patch.object(gl, "_glab_api") as m:
                with self.assertRaises(gl.ProviderCliError):
                    gl.merge_pull_request("g", "p", 7, "SQUASH", bad, host="gitlab.com")
                m.assert_not_called()

    def test_gitlab_merge_sends_the_sha_precondition(self):
        with mock.patch.object(gl, "_glab_api", return_value={"state": "merged"}) as m:
            gl.merge_pull_request("g", "p", 7, "SQUASH", "abc1234", host="gitlab.com")
        self.assertEqual(m.call_args.kwargs["body"]["sha"], "abc1234")

    def test_gitlab_merge_reports_the_resulting_state(self):
        """GitLab answers with a STATE, not a boolean -- only "merged" means it
        happened, so a refusal cannot be read as success."""
        with mock.patch.object(gl, "_glab_api", return_value={"state": "merged", "merge_commit_sha": "s"}):
            self.assertTrue(
                gl.merge_pull_request("g", "p", 7, "SQUASH", "abc1234", host="gitlab.com")["merged"]
            )
        with mock.patch.object(
            gl, "_glab_api", return_value={"state": "opened", "merge_error": "not approved"}
        ):
            out = gl.merge_pull_request("g", "p", 7, "SQUASH", "abc1234", host="gitlab.com")
        self.assertFalse(out["merged"])
        self.assertIn("not approved", out["message"])

    def test_a_refused_merge_does_not_touch_the_caches(self):
        """A refusal is NOT an exception on every provider: GitLab answers 200 with a
        non-merged state and a ``merge_error``. Trusting the return value would evict
        a still-open PR from the open list and report success."""
        key = provider.key_from_parts("o", "r")
        client = mock.Mock()
        client.merge_pull_request.return_value = {
            "merged": False, "sha": None, "message": "approvals are missing",
        }
        with mock.patch.object(provider, "client_for", return_value=client), \
                mock.patch.object(routes, "_st", new=mock.AsyncMock()) as st:
            with self.assertRaises(gh.GhCliError) as ctx:
                _await(routes._run_pr_action(key, "merge", 7))
            st.assert_not_awaited()
        self.assertIn("approvals are missing", str(ctx.exception))

    def test_a_real_merge_does_evict_the_row(self):
        key = provider.key_from_parts("o", "r")
        client = mock.Mock()
        client.merge_pull_request.return_value = {"merged": True, "sha": "abc", "message": ""}
        with mock.patch.object(provider, "client_for", return_value=client), \
                mock.patch.object(routes, "_st", new=mock.AsyncMock()) as st:
            out = _await(routes._run_pr_action(key, "merge", 7))
        self.assertTrue(out["merged"])
        st.assert_awaited()

    def test_405_from_the_provider_becomes_a_readable_refusal(self):
        """"Method Not Allowed" on a merge button reads like an app bug. It is the
        repository's own rules speaking, and the route says so."""
        # The route reads the PR first (the merge-state gate), so the client has to
        # answer a satisfied state for the 405 mapping to be the thing under test.
        client = mock.Mock()
        client.PR_MERGE_METHODS = gh.PR_MERGE_METHODS
        client.get_pr_detail.return_value = {
            "mergeable": True, "mergeable_state": "clean", "head_sha": "abc1234",
        }
        with mock.patch.object(routes, "_connected", return_value=True), \
                mock.patch.object(routes, "_repo_can_write", return_value=True), \
                mock.patch.object(provider, "client_for", return_value=client), \
                mock.patch.object(
                    routes, "_run_pr_action",
                    side_effect=gh.GhCliError("gh api PUT ... failed (exit 1): HTTP 405 not mergeable"),
                ):
            resp = _await(routes._handle_pull_merge(
                _req({"owner": "o", "repo": "r", "number": 7, "head_sha": "abc1234"})
            ))
        self.assertEqual(resp.status, 409)
        body = _body(resp)
        self.assertEqual(body["code"], "merge_not_allowed")
        self.assertIn("required", body["error"])

    def test_the_route_refuses_a_BLOCKED_pr_even_for_a_privileged_user(self):
        """The check the provider cannot be relied on for.

        ``mergeable`` means only "no CONFLICTS": a PR whose required reviews or checks
        have not passed is ``mergeable: true`` with ``mergeable_state: "blocked"``. The
        provider 405s that for an ordinary user but HONOURS it for an admin holding
        bypass-branch-protection — so gating on ``mergeable`` alone offered the most
        privileged account a one-click way to land a PR its own rules had rejected.
        """
        # ``unstable`` is in this list too: it does not distinguish a failing REQUIRED
        # check from a failing optional one, so it cannot be read as "protections
        # satisfied", and a gate that cannot tell must refuse.
        #
        # So is GitLab's LEGACY ``can_be_merged``, and that one is subtler: it comes
        # from the old ``merge_status`` field, which reports ONLY whether the branches
        # conflict — it is the exact analogue of GitHub's ``mergeable`` and knows
        # nothing about unmet approvals, unresolved blocking discussions or a red
        # required pipeline. Accepting it reproduced this very hole on the pre-16.x
        # servers least likely to be watched. Its modern replacement
        # (``detailed_merge_status: "mergeable"``) DOES imply those rules are met and
        # is allowed by the next test.
        for state in (
            "blocked", "behind", "dirty", "draft", "unknown", "unstable",
            "can_be_merged", "not_approved", "discussions_not_resolved",
            "ci_still_running", "",
        ):
            detail = {"mergeable": True, "mergeable_state": state, "head_sha": "abc1234"}
            client = mock.Mock()
            client.PR_MERGE_METHODS = gh.PR_MERGE_METHODS
            client.get_pr_detail.return_value = detail
            with mock.patch.object(routes, "_connected", return_value=True), \
                    mock.patch.object(routes, "_repo_can_write", return_value=True), \
                    mock.patch.object(provider, "client_for", return_value=client), \
                    mock.patch.object(routes, "_run_pr_action") as run:
                resp = _await(routes._handle_pull_merge(_req({
                    "owner": "o", "repo": "r", "number": 7, "head_sha": "abc1234",
                })))
                run.assert_not_called()
            self.assertEqual(resp.status, 409, state)
            self.assertEqual(_body(resp)["code"], "merge_not_ready", state)

    def test_the_route_allows_a_satisfied_pr(self):
        # ``clean``/``has_hooks`` are GitHub's; ``mergeable`` is GitLab's MODERN
        # ``detailed_merge_status``, which unlike the legacy ``can_be_merged`` does
        # imply approval rules, blocking discussions and required pipelines are all
        # satisfied.
        for state in ("clean", "has_hooks", "mergeable"):
            client = mock.Mock()
            client.PR_MERGE_METHODS = gh.PR_MERGE_METHODS
            client.get_pr_detail.return_value = {
                "mergeable": True, "mergeable_state": state, "head_sha": "abc1234",
            }
            with mock.patch.object(routes, "_connected", return_value=True), \
                    mock.patch.object(routes, "_repo_can_write", return_value=True), \
                    mock.patch.object(provider, "client_for", return_value=client), \
                    mock.patch.object(
                        routes, "_run_pr_action",
                        new=mock.AsyncMock(return_value={"merged": True, "sha": "abc1234", "message": ""}),
                    ):
                resp = _await(routes._handle_pull_merge(_req({
                    "owner": "o", "repo": "r", "number": 7, "head_sha": "abc1234",
                })))
            self.assertEqual(resp.status, 200, state)

    def test_the_route_refuses_when_the_head_moved_since_the_read(self):
        """The merge state describes the commit it was read for, not a newer one.

        Also asserts the refusal is AUDITED as ``denied``. It is the app refusing a
        merge — not a provider error and not a validation 400 — and it was the one
        branch in this handler that returned silently, so a query over the merge
        surface for refusals missed exactly the stale-head case, which is the one
        worth noticing: a repeated hit means someone is racing a live branch.
        """
        client = mock.Mock()
        client.PR_MERGE_METHODS = gh.PR_MERGE_METHODS
        client.get_pr_detail.return_value = {
            "mergeable": True, "mergeable_state": "clean", "head_sha": "deadbee",
        }
        with mock.patch.object(routes, "_connected", return_value=True), \
                mock.patch.object(routes, "_repo_can_write", return_value=True), \
                mock.patch.object(provider, "client_for", return_value=client), \
                mock.patch.object(routes, "_audit") as audit, \
                mock.patch.object(routes, "_run_pr_action") as run:
            resp = _await(routes._handle_pull_merge(_req({
                "owner": "o", "repo": "r", "number": 7, "head_sha": "abc1234",
            })))
            run.assert_not_called()
        self.assertEqual(resp.status, 409)
        self.assertEqual(_body(resp)["code"], "merge_conflict")
        outcomes = [c[0][2] for c in audit.call_args_list]
        self.assertIn("denied", outcomes)

    def test_the_merge_route_requires_the_head_sha(self):
        """A 400 from the route, not a 502 from the client — the caller can fix it."""
        with mock.patch.object(routes, "_connected", return_value=True), \
                mock.patch.object(routes, "_repo_can_write", return_value=True), \
                mock.patch.object(routes, "_run_pr_action") as run:
            resp = _await(routes._handle_pull_merge(
                _req({"owner": "o", "repo": "r", "number": 7})
            ))
            run.assert_not_called()
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "head_sha_required")

    def test_a_legacy_gitlab_server_cannot_reach_the_merge_gate(self):
        """End to end for the legacy-status hole, from the raw payload.

        A pre-16.x GitLab (or any payload without ``detailed_merge_status``) reports
        only ``merge_status: "can_be_merged"``, which means "no conflicts" and nothing
        more. ``_norm_pull`` surfaces it as the ``mergeable_state``, and the gate must
        refuse it — otherwise an MR with unmet approvals is one click from landing for
        anyone who can bypass the rules.
        """
        raw = {
            "iid": 7, "title": "t", "state": "opened", "web_url": "u", "labels": [],
            "sha": "abc1234", "merge_status": "can_be_merged",
        }
        with mock.patch.object(gl, "_glab_api", return_value=raw), \
                mock.patch.object(gl, "list_repo_labels", return_value=[]):
            detail = gl.get_pr_detail("g", "p", 7, host="gitlab.com")
        # The read side still reports it as mergeable — "no conflicts" is a true and
        # useful signal for the pane's warning.
        self.assertEqual(detail["mergeable_state"], "can_be_merged")
        self.assertTrue(detail["mergeable"])
        # The merge GATE does not accept it.
        self.assertNotIn(detail["mergeable_state"], routes._MERGE_ALLOWED_STATES)


class TestPrStatePrimitive(unittest.TestCase):
    def test_close_uses_the_pulls_endpoint(self):
        """Not the issues endpoint: reopening a merged PR must be the provider's
        refusal, not a silent success against the issue shadow."""
        with mock.patch.object(
            gh, "_run_gh_write", return_value={"state": "closed", "merged": False, "draft": False}
        ) as m:
            out = gh.set_pr_state("o", "r", 7, "closed")
        method, path = m.call_args[0][0], m.call_args[0][1]
        self.assertEqual(method, "PATCH")
        self.assertEqual(path, "repos/o/r/pulls/7")
        self.assertEqual(m.call_args[0][2], {"state": "closed"})
        self.assertEqual(out["state"], "closed")

    def test_invalid_state_is_refused_before_any_call(self):
        with mock.patch.object(gh, "_run_gh_write") as m:
            with self.assertRaises(gh.GhCliError):
                gh.set_pr_state("o", "r", 7, "merged")
            m.assert_not_called()

    def test_number_is_coerced_into_the_path(self):
        with mock.patch.object(gh, "_run_gh_write", return_value={}) as m:
            gh.set_pr_state("o", "r", "7", "closed")  # type: ignore[arg-type]
        self.assertEqual(m.call_args[0][1], "repos/o/r/pulls/7")


class TestPrReviewPrimitive(unittest.TestCase):
    def test_approve_sends_the_event(self):
        with mock.patch.object(
            gh, "_run_gh_write", return_value={"id": 1, "state": "APPROVED", "submitted_at": "t"}
        ) as m:
            out = gh.submit_pr_review("o", "r", 7, "approve", "", "abc1234")
        self.assertEqual(m.call_args[0][1], "repos/o/r/pulls/7/reviews")
        self.assertEqual(m.call_args[0][2], {"event": "APPROVE", "commit_id": "abc1234"})
        self.assertEqual(out["state"], "APPROVED")

    def test_approve_without_body_is_allowed(self):
        with mock.patch.object(gh, "_run_gh_write", return_value={}) as m:
            gh.submit_pr_review("o", "r", 7, "APPROVE", "", "abc1234")
        self.assertNotIn("body", m.call_args[0][2])

    def test_request_changes_requires_a_body(self):
        """GitHub rejects a bodyless REQUEST_CHANGES with a 422 — caught locally so
        the user gets a clear message instead of an opaque upstream error."""
        with mock.patch.object(gh, "_run_gh_write") as m:
            with self.assertRaises(gh.GhCliError):
                gh.submit_pr_review("o", "r", 7, "REQUEST_CHANGES", "   ", "abc1234")
            m.assert_not_called()

    def test_comment_review_requires_a_body(self):
        with mock.patch.object(gh, "_run_gh_write") as m:
            with self.assertRaises(gh.GhCliError):
                gh.submit_pr_review("o", "r", 7, "COMMENT", "", "abc1234")
            m.assert_not_called()

    def test_unknown_event_is_refused(self):
        """A typo must never become an unintended approval."""
        with mock.patch.object(gh, "_run_gh_write") as m:
            for bad in ("aprove", "LGTM", "", "MERGE"):
                with self.assertRaises(gh.GhCliError):
                    gh.submit_pr_review("o", "r", 7, bad, "x", "abc1234")
            m.assert_not_called()


class TestReviewIsPinnedToACommit(unittest.TestCase):
    """A review is a verdict on a REVISION, so it must name the commit it was
    formed on.

    Without the pin, a force-push landing between the render and the click makes an
    APPROVAL apply to code the reviewer never saw — the review attaches to whatever
    the head is when the request arrives. With it, the provider refuses instead.
    """

    def test_github_refuses_a_review_without_a_head_sha(self):
        with mock.patch.object(gh, "_run_gh_write") as m:
            for bad in ("", "   ", "not-hex", "abc", "../../etc"):
                with self.assertRaises(gh.GhCliError, msg=bad):
                    gh.submit_pr_review("o", "r", 7, "APPROVE", "", bad)
            m.assert_not_called()

    def test_github_sends_it_as_the_commit_id_precondition(self):
        """``commit_id`` is what makes GitHub 422 a stale approval rather than
        record it against the new head."""
        with mock.patch.object(gh, "_run_gh_write", return_value={}) as m:
            gh.submit_pr_review("o", "r", 7, "REQUEST_CHANGES", "needs work", "DEADBEEF1234")
        self.assertEqual(m.call_args[0][2]["commit_id"], "DEADBEEF1234")

    def test_gitlab_refuses_a_review_without_a_head_sha(self):
        with mock.patch.object(gl, "_glab_api") as m:
            for bad in ("", "   ", "not-hex", "abc"):
                with self.assertRaises(gl.ProviderCliError, msg=bad):
                    gl.submit_pr_review("g", "p", 7, "APPROVE", "", bad, host="gitlab.com")
            m.assert_not_called()

    def test_gitlab_sends_it_as_the_approve_sha_precondition(self):
        with mock.patch.object(gl, "_glab_api", return_value={"id": 1}) as m:
            gl.submit_pr_review("g", "p", 7, "APPROVE", "", "abc1234", host="gitlab.com")
        self.assertEqual(m.call_args.kwargs["body"], {"sha": "abc1234"})

    def test_gitlab_requires_it_for_a_comment_too(self):
        """GitLab's notes endpoint takes no sha, so a COMMENT cannot be pinned on the
        wire — but the CALLER is still held to supplying one, so the two verbs cannot
        diverge into "one checks and the other does not"."""
        with mock.patch.object(gl, "_glab_api") as m:
            with self.assertRaises(gl.ProviderCliError):
                gl.submit_pr_review("g", "p", 7, "COMMENT", "hi", "", host="gitlab.com")
            m.assert_not_called()

    def test_the_list_row_carries_the_head_commit_on_both_providers(self):
        """A BULK approve pins each PR to the sha of the row the user saw, so the
        LIST payload has to carry it — not just the detail read."""
        self.assertIn("head_sha: (.head.sha // null)", gh._PR_JQ)
        raw = {
            "iid": 7, "title": "t", "state": "opened", "web_url": "u", "labels": [],
            "sha": "abc1234",
        }
        self.assertEqual(gl._norm_pull(raw)["head_sha"], "abc1234")

    def test_a_search_row_gets_its_head_commit_from_the_enrichment(self):
        """The person-filtered list is served by SEARCH, whose API does not expose the
        head commit — so without this the "assigned to me" view could never be
        bulk-approved even though the plain list could. The by-number enrichment
        already walks the head commit for its check rollup, so the sha rides along
        rather than costing another call."""
        self.assertIn("head_sha: null", gh._PR_SEARCH_JQ)  # present for shape parity
        self.assertIn("commit{oid ", gh._PR_SUMMARY_SELECTION)
        rows = [{"number": 7, "additions": 1, "deletions": 0, "head_sha": "abc1234"}]
        proc = mock.Mock(
            returncode=0, stdout="\n".join(json.dumps(r) for r in rows), stderr=""
        )
        with mock.patch.object(gh, "_gh_run", return_value=proc):
            out = gh.enrich_pulls_by_number("o", "r", [{"number": 7, "head_sha": None}])
        self.assertEqual(out[0]["head_sha"], "abc1234")

    def test_the_enrichment_never_overwrites_a_head_commit_the_row_already_had(self):
        """It fills a GAP. The list row's own sha is the one the user saw; replacing it
        with a newer one the enrichment happened to read would re-target the verdict at
        a commit that was never on screen."""
        rows = [{"number": 7, "additions": 1, "deletions": 0, "head_sha": "newer99"}]
        proc = mock.Mock(
            returncode=0, stdout="\n".join(json.dumps(r) for r in rows), stderr=""
        )
        with mock.patch.object(gh, "_gh_run", return_value=proc):
            out = gh.enrich_pulls_by_number("o", "r", [{"number": 7, "head_sha": "seen11"}])
        self.assertEqual(out[0]["head_sha"], "seen11")

    def test_a_failed_enrichment_leaves_the_rows_own_head_commit_alone(self):
        """Blanking it on a GraphQL failure would take bulk approve away from list rows
        that never needed the enrichment for it."""
        with mock.patch.object(gh, "_gh_run", side_effect=gh.GhCliError("graphql down")):
            out = gh.enrich_pulls_by_number("o", "r", [{"number": 7, "head_sha": "seen11"}])
        self.assertEqual(out[0]["head_sha"], "seen11")
        # And a row that never had one still reports unknown rather than "".
        with mock.patch.object(gh, "_gh_run", side_effect=gh.GhCliError("graphql down")):
            out = gh.enrich_pulls_by_number("o", "r", [{"number": 8}])
        self.assertIsNone(out[0]["head_sha"])


class TestCommentPrimitive(unittest.TestCase):
    def test_body_rides_in_the_payload_not_argv(self):
        with mock.patch.object(
            gh, "_run_gh_write", return_value={"id": 5, "html_url": "u", "created_at": "t"}
        ) as m:
            out = gh.add_pr_comment("o", "r", 7, "  ship it  ")
        self.assertEqual(m.call_args[0][1], "repos/o/r/issues/7/comments")
        self.assertEqual(m.call_args[0][2], {"body": "ship it"})
        self.assertEqual(out["url"], "u")

    def test_empty_body_is_refused(self):
        with mock.patch.object(gh, "_run_gh_write") as m:
            with self.assertRaises(gh.GhCliError):
                gh.add_pr_comment("o", "r", 7, "   ")
            m.assert_not_called()

    def test_github_pr_comment_is_the_issue_endpoint(self):
        """On GitHub the two coincide (one number sequence); the separate name
        exists because on GitLab they do NOT."""
        with mock.patch.object(gh, "_run_gh_write", return_value={}) as m:
            gh.add_pr_comment("o", "r", 7, "x")
            pr_path = m.call_args[0][1]
            gh.add_issue_comment("o", "r", 7, "x")
            issue_path = m.call_args[0][1]
        self.assertEqual(pr_path, issue_path)


class TestGitlabPrActions(unittest.TestCase):
    def test_pr_comment_uses_the_merge_request_collection(self):
        """GitLab numbers issues and MRs independently, so a PR comment MUST go to
        merge_requests — the generic issues path would comment on an unrelated
        issue that happens to share the number."""
        with mock.patch.object(gl, "_glab_api", return_value={"id": 1}) as m:
            gl.add_pr_comment("g", "p", 7, "hi", host="gitlab.com")
        self.assertEqual(m.call_args[0][0], "projects/g%2Fp/merge_requests/7/notes")
        with mock.patch.object(gl, "_glab_api", return_value={"id": 1}) as m:
            gl.add_issue_comment("g", "p", 7, "hi", host="gitlab.com")
        self.assertEqual(m.call_args[0][0], "projects/g%2Fp/issues/7/notes")

    def test_request_changes_is_refused_not_approximated(self):
        """GitLab has no such verb. Mapping it to a comment or an unapproval would
        report a verdict the platform never recorded."""
        with mock.patch.object(gl, "_glab_api") as m:
            with self.assertRaises(gl.ProviderCliError) as ctx:
                gl.submit_pr_review(
                    "g", "p", 7, "REQUEST_CHANGES", "no", "abc1234", host="gitlab.com"
                )
            m.assert_not_called()
        self.assertIn("no 'request changes'", str(ctx.exception))

    def test_approve_happens_BEFORE_the_note(self):
        """The two calls are not atomic, and the caller's only recovery is to retry
        the pair. Note-first meant a retry after a failed /approve posted the note
        AGAIN — duplicate prose on the MR, still unapproved. /approve is idempotent,
        so approving first makes the retry safe in the direction that matters."""
        calls = []

        def fake(path, **kwargs):
            calls.append(path)
            return {"id": 1}

        with mock.patch.object(gl, "_glab_api", side_effect=fake):
            gl.submit_pr_review(
                "g", "p", 7, "APPROVE", "looks good", "abc1234", host="gitlab.com"
            )
        self.assertEqual(
            calls,
            ["projects/g%2Fp/merge_requests/7/approve", "projects/g%2Fp/merge_requests/7/notes"],
        )

    def test_a_failed_note_does_not_re_post_on_retry(self):
        """The residual failure mode is an approval with no note — visible and
        recoverable — rather than an accumulating pile of duplicate comments."""
        posted = []

        def fake(path, **kwargs):
            if path.endswith("/notes"):
                posted.append(path)
                raise gl.ProviderCliError("glab timed out")
            return {"id": 1}

        with mock.patch.object(gl, "_glab_api", side_effect=fake):
            for _ in range(2):  # the user retries
                with self.assertRaises(gl.ProviderCliError):
                    gl.submit_pr_review(
                        "g", "p", 7, "APPROVE", "looks good", "abc1234", host="gitlab.com"
                    )
        # One note attempt per try, and each try approved first — so no attempt ever
        # landed a note that a later try would duplicate.
        self.assertEqual(len(posted), 2)

    def _mr(self, status):
        """An MR payload whose head pipeline is in ``status``."""
        return {
            "iid": 7, "state": "opened", "head_pipeline": {"status": status},
            "merge_when_pipeline_succeeds": True, "updated_at": "t",
        }

    def test_gitlab_refuses_to_arm_auto_merge_at_all(self):
        """GitLab has no independent arm verb: ``merge_when_pipeline_succeeds`` rides
        on the MERGE endpoint and merges immediately when no pipeline is running.

        An arm-only path that preflighted the head pipeline and armed only when a run
        was live would not be atomic — a pipeline finishing in the window turns
        the same request into an immediate merge — and since arming is a BULK action
        with no typed confirmation, losing that race would merge a whole selection
        irreversibly. So it refuses outright, and must not touch the API at all.
        """
        for fn in (gl.enable_auto_merge, gl.disable_auto_merge):
            with mock.patch.object(gl, "_glab_api") as m:
                with self.assertRaises(gl.ProviderCliError):
                    fn("g", "p", 7, host="gitlab.com")
                m.assert_not_called()

    def test_squash_is_sent_explicitly_on_merge(self):
        """GitLab reads a MISSING ``squash`` as "leave as-is", so omitting it would
        inherit whatever the MR was last set to rather than what this call asked for."""
        for verb, expected in (("SQUASH", True), ("MERGE", False)):
            with mock.patch.object(gl, "_glab_api", return_value={"state": "merged"}) as m:
                gl.merge_pull_request("g", "p", 7, verb, "abc1234", host="gitlab.com")
            body = m.call_args.kwargs["body"]
            assert body is not None
            self.assertIs(body["squash"], expected, verb)

    def test_gitlab_refuses_rebase_rather_than_producing_a_merge_commit(self):
        """GitLab's ``/merge`` has no rebase option — merge-vs-rebase is the PROJECT's
        setting and the only per-request lever is ``squash``.

        So accepting ``REBASE`` translated it to ``squash: false`` and GitLab produced a
        MERGE COMMIT: the caller named one history shape and silently got another, on
        the one operation that cannot be undone. A method the provider cannot honour is
        refused rather than approximated.
        """
        self.assertNotIn("REBASE", gl.PR_MERGE_METHODS)
        # Still available on GitHub, where the merge endpoint really does rebase.
        self.assertIn("REBASE", gh.PR_MERGE_METHODS)
        with mock.patch.object(gl, "_glab_api") as m:
            with self.assertRaises(gl.ProviderCliError):
                gl.merge_pull_request("g", "p", 7, "REBASE", "abc1234", host="gitlab.com")
            m.assert_not_called()

    def test_the_route_rejects_rebase_for_a_gitlab_repo(self):
        """``_pr_merge_method_field`` reads the tuple off the KEY's own client, so the
        per-provider divergence is enforced at the route with no route change. Reaching
        for github_client's copy — which works only when the tuples happen to match —
        would silently mis-validate."""
        gitlab_key = provider.key_from_parts("g", "p", "gitlab", "gitlab.com")
        method, err = routes._pr_merge_method_field({"method": "rebase"}, gitlab_key)
        self.assertIsNotNone(err)
        self.assertEqual(_body(err)["code"], "invalid_merge_method")
        # The same request is fine against GitHub.
        method, err = routes._pr_merge_method_field(
            {"method": "rebase"}, provider.key_from_parts("o", "r")
        )
        self.assertIsNone(err)
        self.assertEqual(method, "REBASE")

    def test_auto_merge_never_attributes_arming_to_the_assignee(self):
        """``merge_user`` is null until AFTER the merge, so an armed-but-unmerged MR
        has no known armer. Falling back to the assignee would name someone who may
        have armed nothing."""
        raw = {
            "iid": 7, "title": "t", "state": "opened", "web_url": "u",
            "merge_when_pipeline_succeeds": True, "squash": True,
            "merge_user": None, "assignee": {"username": "not-the-armer"},
            "labels": [], "diff_refs": {"head_sha": "abc"},
        }
        with mock.patch.object(gl, "_glab_api", return_value=raw), \
                mock.patch.object(gl, "list_repo_labels", return_value=[]):
            detail = gl.get_pr_detail("g", "p", 7, host="gitlab.com")
        self.assertIsNotNone(detail["auto_merge"])
        self.assertIsNone(detail["auto_merge"]["enabled_by"])

    def test_a_successful_pipeline_is_finished_but_not_rerunnable(self):
        """GitLab's ``/retry`` retries only failed and canceled jobs, so offering a
        re-run on a green pipeline was a button that reported success while doing
        nothing. It is still reported as finished — just not retryable."""
        rows = [{"id": 1, "status": "success", "web_url": None, "created_at": None}]
        with mock.patch.object(gl, "_glab_api", return_value=rows):
            out = gl.list_pr_workflow_runs("g", "p", "abc1234", host="gitlab.com")
        self.assertEqual(out[0]["status"], "completed")
        self.assertEqual(out[0]["conclusion"], "success")
        self.assertFalse(out[0]["rerunnable"])
        self.assertFalse(out[0]["cancellable"])

    def test_a_failed_pipeline_is_rerunnable(self):
        rows = [{"id": 1, "status": "failed", "web_url": None, "created_at": None}]
        with mock.patch.object(gl, "_glab_api", return_value=rows):
            out = gl.list_pr_workflow_runs("g", "p", "abc1234", host="gitlab.com")
        self.assertTrue(out[0]["rerunnable"])

    def test_pipeline_conclusion_uses_githubs_spelling(self):
        """The shared UI compares against GitHub's vocabulary; GitLab says
        "canceled" (one l) where GitHub says "cancelled"."""
        rows = [{"id": 1, "status": "canceled", "web_url": None, "created_at": None}]
        with mock.patch.object(gl, "_glab_api", return_value=rows):
            out = gl.list_pr_workflow_runs("g", "p", "abc1234", host="gitlab.com")
        self.assertEqual(out[0]["conclusion"], "cancelled")
        self.assertEqual(out[0]["status"], "completed")
        self.assertTrue(out[0]["rerunnable"])

    def test_retry_reports_what_gitlab_actually_did(self):
        """GitLab's /retry only retries failed+canceled jobs, so a caller asking
        for a FULL re-run must not be told it got one."""
        with mock.patch.object(gl, "_glab_api", return_value={}):
            out = gl.rerun_workflow_run("g", "p", 99, failed_only=False, host="gitlab.com")
        self.assertTrue(out["failed_only"])


class TestWorkflowRuns(unittest.TestCase):
    def test_invalid_sha_is_refused_before_the_call(self):
        with mock.patch.object(gh, "_run_gh_api") as m:
            for bad in ("", "not-hex", "../../etc", "zz"):
                with self.assertRaises(gh.GhCliError):
                    gh.list_pr_workflow_runs("o", "r", bad)
            m.assert_not_called()

    def test_rows_carry_cancellable_and_rerunnable(self):
        raw = [
            {"id": 1, "name": "CI", "status": "in_progress", "conclusion": None},
            {"id": 2, "name": "Build", "status": "completed", "conclusion": "failure"},
            {"id": 3, "name": "Queued", "status": "queued", "conclusion": None},
            {"no_id": True},
        ]
        with mock.patch.object(gh, "_run_gh_api", return_value=raw):
            rows = gh.list_pr_workflow_runs("o", "r", "abc1234")
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(len(rows), 3)  # the id-less row is dropped
        self.assertTrue(by_id[1]["cancellable"])
        self.assertFalse(by_id[1]["rerunnable"])
        self.assertFalse(by_id[2]["cancellable"])
        self.assertTrue(by_id[2]["rerunnable"])
        self.assertTrue(by_id[3]["cancellable"])

    def test_cancel_and_rerun_paths(self):
        with mock.patch.object(gh, "_run_gh_write", return_value=None) as m:
            gh.cancel_workflow_run("o", "r", 42)
            self.assertEqual(m.call_args[0][1], "repos/o/r/actions/runs/42/cancel")
            gh.rerun_workflow_run("o", "r", 42)
            self.assertEqual(m.call_args[0][1], "repos/o/r/actions/runs/42/rerun")
            gh.rerun_workflow_run("o", "r", 42, failed_only=True)
            self.assertEqual(m.call_args[0][1], "repos/o/r/actions/runs/42/rerun-failed-jobs")


class TestAutoMergeMutation(unittest.TestCase):
    def _proc(self, stdout: str, returncode: int = 0, stderr: str = ""):
        return mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)

    def test_enable_resolves_a_node_id_then_mutates(self):
        with mock.patch.object(
            gh, "_run_gh_write", return_value={"node_id": "PR_abc"}
        ), mock.patch.object(
            gh, "_gh_run",
            return_value=self._proc(
                '{"data":{"enablePullRequestAutoMerge":{"pullRequest":'
                '{"autoMergeRequest":{"enabledAt":"t","mergeMethod":"SQUASH"}}}}}'
            ),
        ) as run:
            out = gh.enable_auto_merge("o", "r", 7, "SQUASH")
        argv = run.call_args[0][0]
        self.assertIn("graphql", argv)
        self.assertIn("pr=PR_abc", argv)
        self.assertTrue(out["auto_merge"])
        self.assertEqual(out["method"], "SQUASH")

    def test_invalid_method_is_refused_before_any_call(self):
        with mock.patch.object(gh, "_run_gh_write") as m:
            with self.assertRaises(gh.GhCliError):
                gh.enable_auto_merge("o", "r", 7, "FASTFORWARD")
            m.assert_not_called()

    def test_auto_merge_is_reported_from_the_response_not_asserted(self):
        """Reporting a hardcoded True would make the result a claim rather than an observation."""
        with mock.patch.object(
            gh, "_run_gh_write", return_value={"node_id": "PR_abc"}
        ), mock.patch.object(
            gh, "_gh_run",
            return_value=self._proc(
                '{"data":{"enablePullRequestAutoMerge":{"pullRequest":'
                '{"autoMergeRequest":null}}}}'
            ),
        ):
            out = gh.enable_auto_merge("o", "r", 7)
        self.assertFalse(out["auto_merge"])
        self.assertIsNone(out["method"])

    def test_missing_node_id_is_an_error_not_a_silent_success(self):
        with mock.patch.object(gh, "_run_gh_write", return_value={}):
            with self.assertRaises(gh.GhCliError):
                gh.enable_auto_merge("o", "r", 7)

    def test_graphql_errors_array_is_a_failure(self):
        """GraphQL reports failures in a 200 response, so a returncode check alone
        would read an error as success and tell the user auto-merge was armed."""
        with mock.patch.object(
            gh, "_run_gh_write", return_value={"node_id": "PR_abc"}
        ), mock.patch.object(
            gh, "_gh_run",
            return_value=self._proc('{"errors":[{"message":"auto-merge is not allowed"}]}'),
        ):
            with self.assertRaises(gh.GhCliError):
                gh.enable_auto_merge("o", "r", 7)

    def test_graphql_authorization_error_maps_to_permission_error(self):
        """So the route answers 403 (fix your access) rather than 502 (upstream is
        broken)."""
        with mock.patch.object(
            gh, "_run_gh_write", return_value={"node_id": "PR_abc"}
        ), mock.patch.object(
            gh, "_gh_run",
            return_value=self._proc(
                '{"errors":[{"message":"must have push access to enable auto-merge"}]}'
            ),
        ):
            with self.assertRaises(gh.GhPermissionError):
                gh.enable_auto_merge("o", "r", 7)


class TestPrStateCacheCoherence(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_close_drops_the_row_from_the_open_list(self):
        store.write_pulls_cache(
            "o", "r", [{"number": 7}, {"number": 9}], root=self.tmp, state="open"
        )
        store.apply_pr_state_change_to_caches("o", "r", 7, "closed", root=self.tmp)
        rows = store.read_pulls_cache("o", "r", self.tmp, state="open")
        assert rows is not None
        self.assertEqual([r["number"] for r in rows], [9])

    def test_reopen_drops_the_row_from_the_closed_list(self):
        store.write_pulls_cache("o", "r", [{"number": 7}], root=self.tmp, state="closed")
        store.apply_pr_state_change_to_caches("o", "r", 7, "open", root=self.tmp)
        self.assertEqual(store.read_pulls_cache("o", "r", self.tmp, state="closed"), [])

    def test_detail_entry_is_dropped_so_the_pane_refetches(self):
        """The cached detail describes the PRE-change PR — including an auto-merge
        arming that closing silently clears — and is served for up to
        PR_DETAIL_CACHE_TTL_SEC, which is long enough for a user to click and watch
        nothing happen."""
        store.write_pr_detail_cache(
            "o", "r", 7, {"number": 7, "state": "open"}, [], [], root=self.tmp
        )
        store.apply_pr_state_change_to_caches("o", "r", 7, "closed", root=self.tmp)
        self.assertIsNone(store.read_pr_detail_cache("o", "r", 7, self.tmp))

    def test_drop_pr_detail_cache_is_a_noop_when_absent(self):
        store.drop_pr_detail_cache("o", "r", 404, root=self.tmp)  # must not raise

    def test_no_caches_present_is_a_noop(self):
        store.apply_pr_state_change_to_caches("o", "r", 7, "closed", root=self.tmp)


class TestBulkNumbersValidation(unittest.TestCase):
    def test_rejects_non_positive_and_non_int(self):
        bad_lists: list[list[object]] = [[0], [-1], ["7"], [None], [1.5], [{}]]
        for bad in bad_lists:
            numbers, err = routes._pr_numbers_field({"numbers": bad})
            self.assertIsNotNone(err, f"{bad!r} should be rejected")

    def test_rejects_bool_which_is_an_int_subclass(self):
        """JSON `true` would otherwise validate as PR #1 and act on it."""
        _, err = routes._pr_numbers_field({"numbers": [True]})
        self.assertIsNotNone(err)

    def test_rejects_empty_and_non_list(self):
        bad_values: list[object] = [[], None, "1,2", {}]
        for bad in bad_values:
            _, err = routes._pr_numbers_field({"numbers": bad})
            self.assertIsNotNone(err)

    def test_rejects_over_the_cap(self):
        _, err = routes._pr_numbers_field(
            {"numbers": list(range(1, routes._BULK_PR_MAX + 2))}
        )
        self.assertIsNotNone(err)

    def test_rejects_out_of_range_number(self):
        _, err = routes._pr_numbers_field({"numbers": [routes.MAX_ITEM_NUMBER + 1]})
        self.assertIsNotNone(err)

    def test_dedupes_while_preserving_order(self):
        """A repeated number would otherwise be acted on twice — a wasted call and
        a duplicate row in the response."""
        numbers, err = routes._pr_numbers_field({"numbers": [9, 3, 9, 1, 3]})
        self.assertIsNone(err)
        self.assertEqual(numbers, [9, 3, 1])

    def test_body_bound_is_enforced(self):
        _, err = routes._pr_body_field({"body": "x" * (routes._PR_BODY_MAX_CHARS + 1)})
        self.assertIsNotNone(err)
        text, ok = routes._pr_body_field({"body": "  hi  "})
        self.assertIsNone(ok)
        self.assertEqual(text, "hi")

    def test_non_string_body_is_empty_not_a_crash(self):
        text, err = routes._pr_body_field({"body": 5})
        self.assertIsNone(err)
        self.assertEqual(text, "")


def _req(payload: object) -> web.Request:
    """A real ``web.Request`` whose ``.json()`` yields ``payload``.

    Built on aiohttp's own ``make_mocked_request`` (as the rest of this app's route
    tests do) rather than a duck-typed stub, so the handlers are exercised against
    the actual Request type instead of something that merely happens to have the one
    method they call. Passing an Exception makes ``.json()`` raise it, which is how
    the malformed-body paths are reached.
    """
    request = make_mocked_request("POST", "/api/apps/issue-radar/pull/state")

    async def _json(*_args: object, **_kwargs: object) -> object:
        if isinstance(payload, Exception):
            raise payload
        return payload

    # `json` is a plain coroutine method on Request, so replacing it on the instance
    # is enough and needs no payload plumbing through the transport.
    request.json = _json  # type: ignore[method-assign]
    return request


def _await(coro):
    """Drive one coroutine to completion.

    Named ``_await`` rather than ``_run`` deliberately: the spawn-audit gate
    (``test/test_spawn_audit.py``) matches a bare call to ``run``, so a helper by
    that name lands in a SECURITY allowlist as if it spawned a subprocess. It does
    not — this is ``asyncio.run`` on an in-process handler — and growing that
    allowlist for a test helper would dilute the one signal it exists to carry.
    """
    return asyncio.run(coro)


def _body(response):
    return json.loads(response.text)


class TestReviewRoutePinning(unittest.TestCase):
    """``/pull/review`` holds the caller to naming the commit, like ``/pull/merge``."""

    def _post(self, payload, live_sha="abc1234"):
        """Post a review with the PR's live head reported as ``live_sha``.

        Defaults to matching the pinned sha, so the stale-head refusal is not the
        ambient condition of every assertion here; the conflict path is its own test.
        """
        client = mock.Mock()
        client.get_pr_detail.return_value = {"head_sha": live_sha}
        with mock.patch.object(routes, "_connected", return_value=True), \
                mock.patch.object(routes, "_repo_can_write", return_value=True), \
                mock.patch.object(provider, "client_for", return_value=client):
            return _await(routes._handle_pull_review(_req(payload)))

    def test_a_verdict_on_a_moved_head_is_refused(self):
        """The check neither provider makes for us.

        GitLab's ``/approve`` takes a real ``sha`` precondition, but GitHub's
        ``commit_id`` is only ATTRIBUTION — GitHub accepts a review naming a commit that
        is no longer the head and records it there, and whether that stale approval still
        counts toward branch protection is a per-repo setting. Where "dismiss stale
        approvals" is off, an unchecked approval satisfies protection on code nobody
        read. So the app reads the head itself, exactly as the merge route does.
        """
        for event in ("approve", "request_changes"):
            with mock.patch.object(routes, "_run_pr_action") as run:
                resp = self._post(
                    {
                        "owner": "o", "repo": "r", "number": 7, "event": event,
                        "body": "reason", "head_sha": "abc1234",
                    },
                    live_sha="deadbee",
                )
                run.assert_not_called()
            self.assertEqual(resp.status, 409, event)
            self.assertEqual(_body(resp)["code"], "review_conflict", event)

    def test_a_plain_comment_is_not_refused_on_a_moved_head(self):
        """A comment records no VERDICT, so it stays valid prose about the PR whatever
        the head does — refusing it would only cost the user their typing."""
        with mock.patch.object(
            routes, "_run_pr_action", new=mock.AsyncMock(return_value={"state": "COMMENTED"})
        ):
            resp = self._post(
                {
                    "owner": "o", "repo": "r", "number": 7, "event": "comment",
                    "body": "a thought", "head_sha": "abc1234",
                },
                live_sha="deadbee",
            )
        self.assertEqual(resp.status, 200)

    def test_an_unknown_live_head_does_not_become_a_refusal(self):
        """"We could not tell" must not block every review on a provider that does not
        report a head — that would be a fail-closed default on a READ gap, which costs
        the feature without buying safety (the sha still rides to the provider)."""
        with mock.patch.object(
            routes, "_run_pr_action", new=mock.AsyncMock(return_value={"state": "APPROVED"})
        ):
            resp = self._post(
                {
                    "owner": "o", "repo": "r", "number": 7, "event": "approve",
                    "head_sha": "abc1234",
                },
                live_sha="",
            )
        self.assertEqual(resp.status, 200)

    def test_a_review_without_a_head_sha_is_400(self):
        for bad in (None, "", "not-hex", "abc"):
            payload = {"owner": "o", "repo": "r", "number": 7, "event": "approve"}
            if bad is not None:
                payload["head_sha"] = bad
            with mock.patch.object(routes, "_run_pr_action") as run:
                resp = self._post(payload)
                run.assert_not_called()
            self.assertEqual(resp.status, 400, repr(bad))
            self.assertEqual(_body(resp)["code"], "head_sha_required", repr(bad))

    def test_a_pinned_review_reaches_the_dispatch_with_its_sha(self):
        with mock.patch.object(
            routes, "_run_pr_action", new=mock.AsyncMock(return_value={"state": "APPROVED"})
        ) as run:
            resp = self._post({
                "owner": "o", "repo": "r", "number": 7, "event": "approve",
                "head_sha": "abc1234",
            })
        self.assertEqual(resp.status, 200)
        awaited = run.await_args
        assert awaited is not None
        self.assertEqual(awaited.kwargs["head_sha"], "abc1234")

    def test_the_head_moved_check_skips_the_mergeability_retry(self):
        """The check reads only ``head_sha`` (returned eagerly), so it asks
        ``get_pr_detail`` NOT to pay GitHub's lazy-mergeability retry+sleep.

        That retry is a 1.5s sleep plus a SECOND ``gh`` call, and this check runs
        once per verdict AND per row of a bulk approve — so on a 50-PR approve the
        default path was ~75s of pure sleep. ``resolve_mergeable=False`` is what
        drops it; skipping the retry cannot weaken the pin, since the head is not a
        lazily-computed field."""
        client = mock.Mock()
        client.get_pr_detail.return_value = {"head_sha": "abc1234"}
        with mock.patch.object(routes, "_connected", return_value=True), \
                mock.patch.object(routes, "_repo_can_write", return_value=True), \
                mock.patch.object(provider, "client_for", return_value=client), \
                mock.patch.object(
                    routes, "_run_pr_action",
                    new=mock.AsyncMock(return_value={"state": "APPROVED"}),
                ):
            resp = _await(routes._handle_pull_review(_req({
                "owner": "o", "repo": "r", "number": 7, "event": "approve",
                "head_sha": "abc1234",
            })))
        self.assertEqual(resp.status, 200)
        client.get_pr_detail.assert_called_once()
        self.assertIs(client.get_pr_detail.call_args.kwargs.get("resolve_mergeable"), False)


class TestPrActionPreamble(unittest.TestCase):
    """The shared gate every PR action goes through. Factored precisely so one of
    them cannot ship without the permission check — so it is tested once, here."""

    def test_non_json_body_is_400(self):
        _, _, early = _await(routes._pr_action_preamble(_req(ValueError("nope")), "op"))
        assert early is not None
        self.assertEqual(early.status, 400)

    def test_non_object_body_is_400(self):
        _, _, early = _await(routes._pr_action_preamble(_req([1, 2]), "op"))
        assert early is not None
        self.assertEqual(early.status, 400)

    def test_missing_owner_repo_is_400(self):
        _, _, early = _await(routes._pr_action_preamble(_req({"owner": "o"}), "op"))
        assert early is not None
        self.assertEqual(early.status, 400)

    def test_unconnected_repo_is_404(self):
        with mock.patch.object(routes, "_connected", return_value=False):
            _, _, early = _await(
                routes._pr_action_preamble(_req({"owner": "o", "repo": "r"}), "op")
            )
        assert early is not None
        self.assertEqual(early.status, 404)

    def test_read_only_repo_is_403(self):
        with mock.patch.object(routes, "_connected", return_value=True), \
                mock.patch.object(routes, "_repo_can_write", return_value=False):
            _, _, early = _await(
                routes._pr_action_preamble(_req({"owner": "o", "repo": "r"}), "op")
            )
        assert early is not None
        self.assertEqual(early.status, 403)

    def test_unknowable_permission_is_denied_not_allowed(self):
        """``None`` means "could not tell" and MUST fail closed — a transient
        permissions-read failure must never authorize a mutation."""
        with mock.patch.object(routes, "_connected", return_value=True), \
                mock.patch.object(routes, "_repo_can_write", return_value=None):
            _, _, early = _await(
                routes._pr_action_preamble(_req({"owner": "o", "repo": "r"}), "op")
            )
        assert early is not None
        self.assertEqual(early.status, 403)

    def test_writable_repo_passes_through(self):
        with mock.patch.object(routes, "_connected", return_value=True), \
                mock.patch.object(routes, "_repo_can_write", return_value=True):
            body, key, early = _await(
                routes._pr_action_preamble(_req({"owner": "o", "repo": "r"}), "op")
            )
        self.assertIsNone(early)
        self.assertEqual((key.owner, key.repo), ("o", "r"))
        self.assertEqual(body["owner"], "o")


class TestBulkEndpoint(unittest.TestCase):
    """The partial-failure contract: a batch is never abandoned over one row, and
    the caller is never told about a write that did not happen."""

    def setUp(self):
        self.perm = mock.patch.object(routes, "_connected", return_value=True)
        self.write = mock.patch.object(routes, "_repo_can_write", return_value=True)
        # The stale-verdict check reads the live head per row for the pinned verbs, so
        # the default here is "the head has NOT moved" — the conflict path gets its own
        # test rather than being the ambient condition of every other assertion.
        self.head = mock.patch.object(
            routes, "_refuse_if_head_moved", new=mock.AsyncMock(return_value=None)
        )
        self.perm.start()
        self.write.start()
        self.head.start()

    def tearDown(self):
        self.perm.stop()
        self.write.stop()
        self.head.stop()

    def _post(self, payload):
        return _await(routes._handle_pulls_bulk(_req(payload)))

    def test_unknown_action_is_400(self):
        resp = self._post({"owner": "o", "repo": "r", "numbers": [1], "action": "merge_now"})
        self.assertEqual(resp.status, 400)

    def test_comment_without_a_body_is_400(self):
        resp = self._post({"owner": "o", "repo": "r", "numbers": [1], "action": "comment"})
        self.assertEqual(resp.status, 400)

    def test_invalid_merge_method_is_400(self):
        resp = self._post({
            "owner": "o", "repo": "r", "numbers": [1],
            "action": "auto_merge", "method": "fastforward",
        })
        self.assertEqual(resp.status, 400)

    def test_one_failure_does_not_abandon_the_rest(self):
        async def fake(key, action, number, **kwargs):
            if number == 2:
                raise gh.GhCliError("PR #2 is locked")
            return {"state": "closed"}

        with mock.patch.object(routes, "_run_pr_action", side_effect=fake):
            resp = self._post(
                {"owner": "o", "repo": "r", "numbers": [1, 2, 3], "action": "close"}
            )
        payload = _body(resp)
        self.assertEqual([r["number"] for r in payload["applied"]], [1, 3])
        self.assertEqual([r["number"] for r in payload["failed"]], [2])
        self.assertIn("locked", payload["failed"][0]["error"])

    def test_a_permission_failure_on_one_pr_is_a_row_failure(self):
        """The repo-level gate already passed, so a single PR the session cannot
        touch is that row's problem — not a reason to discard the rows that
        succeeded."""
        async def fake(key, action, number, **kwargs):
            if number == 1:
                raise gh.GhPermissionError("no access to #1")
            return {"state": "APPROVED"}

        with mock.patch.object(routes, "_run_pr_action", side_effect=fake):
            resp = self._post({
                "owner": "o", "repo": "r", "numbers": [1, 2], "action": "approve",
                "head_shas": {"1": "abc1234", "2": "def5678"},
            })
        payload = _body(resp)
        self.assertEqual([r["number"] for r in payload["failed"]], [1])
        self.assertEqual([r["number"] for r in payload["applied"]], [2])

    def test_a_row_permission_refusal_audits_as_denied(self):
        """Not "failure": a refused mutation must be distinguishable from a timeout,
        or a query for outcome=denied returns nothing for the whole bulk surface."""
        async def fake(key, action, number, **kwargs):
            if number == 1:
                raise gh.GhPermissionError("no access to #1")
            return {}

        with mock.patch.object(routes, "_run_pr_action", side_effect=fake), \
                mock.patch.object(routes, "_audit") as audit:
            self._post({
                "owner": "o", "repo": "r", "numbers": [1, 2], "action": "approve",
                "head_shas": {"1": "abc1234", "2": "def5678"},
            })
        outcomes = {c[0][2] for c in audit.call_args_list}
        self.assertIn("denied", outcomes)

    def test_bulk_approve_requires_a_head_sha_for_every_pr(self):
        """A bulk approve is N verdicts, so each names its own revision.

        A PARTIAL map is refused rather than honoured for the subset that has one:
        approving fewer PRs than the button's count claims is its own defect, and the
        missing rows would otherwise be approved at whatever the head happened to be.
        """
        bad_maps: list[dict[str, object] | None] = [
            None,                                    # absent entirely
            {},                                      # present but empty
            {"1": "abc1234"},                        # covers #1 only
            {"1": "abc1234", "2": "nope"},           # #2 is not a sha
            {"1": "abc1234", "2": ""},               # #2 is blank
            {"1": "abc1234", "2": 12345},            # #2 is not a string
        ]
        for shas in bad_maps:
            payload: dict[str, object] = {
                "owner": "o", "repo": "r", "numbers": [1, 2], "action": "approve",
            }
            if shas is not None:
                payload["head_shas"] = shas
            with mock.patch.object(routes, "_run_pr_action") as run:
                resp = self._post(payload)
                run.assert_not_called()
            self.assertEqual(resp.status, 400, repr(shas))
            self.assertEqual(_body(resp)["code"], "head_shas_required", repr(shas))

    def test_bulk_approve_pairs_each_sha_with_its_own_pr(self):
        """Keyed by NUMBER, not by position: a client that reorders or filters its
        selection would otherwise approve #7 at #9's commit."""
        seen = {}

        async def fake(key, action, number, **kwargs):
            seen[number] = kwargs.get("head_sha")
            return {}

        with mock.patch.object(routes, "_run_pr_action", side_effect=fake):
            resp = self._post({
                "owner": "o", "repo": "r", "numbers": [9, 7], "action": "approve",
                "head_shas": {"7": "aaaaaaa", "9": "bbbbbbb"},
            })
        self.assertEqual(resp.status, 200)
        self.assertEqual(seen, {9: "bbbbbbb", 7: "aaaaaaa"})

    def test_one_moved_head_fails_that_row_and_spares_the_rest(self):
        """A bulk approve is N verdicts, and one landing on a force-pushed head is
        exactly as wrong as one from the detail pane. Reported as THAT row's failure so
        the batch still applies — and so the row stays ticked for a retry after a
        refresh — rather than aborting the whole request."""
        async def fake_check(key, number, head_sha, op):
            return None if number != 2 else web.json_response(
                {"error": "moved", "code": "review_conflict"}, status=409
            )

        with mock.patch.object(routes, "_refuse_if_head_moved", side_effect=fake_check), \
                mock.patch.object(
                    routes, "_run_pr_action", new=mock.AsyncMock(return_value={})
                ) as run:
            resp = self._post({
                "owner": "o", "repo": "r", "numbers": [1, 2, 3], "action": "approve",
                "head_shas": {"1": "aaaaaaa", "2": "bbbbbbb", "3": "ccccccc"},
            })
        payload = _body(resp)
        self.assertEqual([r["number"] for r in payload["applied"]], [1, 3])
        self.assertEqual([r["number"] for r in payload["failed"]], [2])
        self.assertIn("moved", payload["failed"][0]["error"])
        # The refused row never reached the provider.
        self.assertEqual(sorted(c[0][2] for c in run.await_args_list), [1, 3])

    def test_the_unpinned_bulk_verbs_need_no_sha(self):
        """Close, comment and the auto-merge verbs act on the pull REQUEST, not on a
        revision — they mean the same thing after a push, so requiring a sha would be
        ceremony that buys nothing."""
        for verb in routes._BULK_PR_ACTIONS:
            if verb in routes._PINNED_BULK_PR_ACTIONS:
                continue
            with mock.patch.object(routes, "_run_pr_action", new=mock.AsyncMock(return_value={})):
                resp = self._post({
                    "owner": "o", "repo": "r", "numbers": [1], "action": verb, "body": "text",
                })
            self.assertEqual(resp.status, 200, verb)

    def test_run_id_is_bounded(self):
        """It reaches a PATH segment in the provider argv, like every other number."""
        with mock.patch.object(routes, "_run_pr_action") as run:
            resp = _await(routes._handle_pull_run_action(_req({
                "owner": "o", "repo": "r", "number": 7,
                "run_id": routes.MAX_RUN_ID + 1, "action": "cancel",
            })))
            run.assert_not_called()
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "run_id_out_of_range")

    def test_actions_run_sequentially(self):
        """They share one provider rate limit: a 50-wide parallel fan-out is how a
        bulk click turns into a secondary-rate-limit block that fails rows for no
        reason of their own."""
        order = []

        async def fake(key, action, number, **kwargs):
            order.append(("start", number))
            await asyncio.sleep(0)
            order.append(("end", number))
            return {}

        with mock.patch.object(routes, "_run_pr_action", side_effect=fake):
            self._post({"owner": "o", "repo": "r", "numbers": [1, 2, 3], "action": "close"})
        self.assertEqual(
            order,
            [("start", 1), ("end", 1), ("start", 2), ("end", 2), ("start", 3), ("end", 3)],
        )

    def test_every_allowlisted_action_is_dispatchable(self):
        """A verb on the allowlist that ``_run_pr_action`` does not implement would
        be a 502 on click, so the two must not drift."""
        seen = []

        async def fake(key, action, number, **kwargs):
            seen.append(action)
            return {}

        with mock.patch.object(routes, "_run_pr_action", side_effect=fake):
            for verb in routes._BULK_PR_ACTIONS:
                payload = {
                    "owner": "o", "repo": "r", "numbers": [1], "action": verb,
                    "body": "text for the comment verbs",
                    # Only the PINNED verbs read it; the rest ignore it.
                    "head_shas": {"1": "abc1234"},
                }
                resp = self._post(payload)
                self.assertEqual(resp.status, 200, f"{verb} was rejected")
        self.assertEqual(seen, list(routes._BULK_PR_ACTIONS))


class TestRunPrActionDispatch(unittest.TestCase):
    """``_run_pr_action`` is the single place an action name becomes a provider
    call, so the per-PR route and the bulk route cannot do different things for one
    verb."""

    def _dispatch(self, action, **kwargs):
        key = provider.key_from_parts("o", "r")
        client = mock.Mock()
        client.set_pr_state.return_value = {"state": "closed"}
        client.submit_pr_review.return_value = {"state": "APPROVED"}
        client.add_pr_comment.return_value = {"id": 1}
        client.enable_auto_merge.return_value = {"auto_merge": True}
        client.disable_auto_merge.return_value = {"auto_merge": False}
        client.cancel_workflow_run.return_value = {"cancelled": True}
        client.rerun_workflow_run.return_value = {"rerun": True}
        with mock.patch.object(provider, "client_for", return_value=client), \
                mock.patch.object(routes, "_st", new=mock.AsyncMock()):
            out = _await(routes._run_pr_action(key, action, 7, **kwargs))
        return client, out

    def test_unknown_action_raises(self):
        key = provider.key_from_parts("o", "r")
        with self.assertRaises(ValueError):
            _await(routes._run_pr_action(key, "merge_now", 7))

    def test_close_and_reopen_map_to_states(self):
        client, _ = self._dispatch("close")
        self.assertEqual(client.set_pr_state.call_args[0][3], "closed")
        client, _ = self._dispatch("reopen")
        self.assertEqual(client.set_pr_state.call_args[0][3], "open")

    def test_review_verbs_map_to_provider_events(self):
        for action, event in (
            ("approve", "APPROVE"),
            ("request_changes", "REQUEST_CHANGES"),
            ("comment_review", "COMMENT"),
        ):
            client, _ = self._dispatch(action, body="why", head_sha="abc1234")
            self.assertEqual(client.submit_pr_review.call_args[0][3], event)

    def test_review_verbs_forward_the_head_sha_to_the_client(self):
        """The dispatch is the single place an action becomes a provider call, so
        dropping the sha here would un-pin every review at once."""
        client, _ = self._dispatch("approve", body="lgtm", head_sha="abc1234")
        self.assertEqual(client.submit_pr_review.call_args[0][5], "abc1234")

    def test_comment_uses_the_pr_specific_function(self):
        client, _ = self._dispatch("comment", body="hi")
        client.add_pr_comment.assert_called_once()
        client.add_issue_comment.assert_not_called()

    def test_auto_merge_verbs(self):
        client, out = self._dispatch("auto_merge", method="REBASE")
        self.assertEqual(client.enable_auto_merge.call_args[0][3], "REBASE")
        self.assertTrue(out["auto_merge"])
        client, out = self._dispatch("cancel_auto_merge")
        client.disable_auto_merge.assert_called_once()
        self.assertFalse(out["auto_merge"])

    def test_run_verbs_pass_the_run_id(self):
        client, _ = self._dispatch("cancel_run", run_id=42)
        self.assertEqual(client.cancel_workflow_run.call_args[0][2], 42)
        client, _ = self._dispatch("rerun_run", run_id=42, failed_only=True)
        self.assertEqual(client.rerun_workflow_run.call_args[0][2], 42)
        self.assertTrue(client.rerun_workflow_run.call_args.kwargs["failed_only"])


class TestActionErrorMapping(unittest.TestCase):
    def test_permission_error_is_403(self):
        resp = routes._pr_action_error("op", "o/r#1", gh.GhPermissionError("nope"))
        self.assertEqual(resp.status, 403)

    def test_other_provider_error_is_502(self):
        resp = routes._pr_action_error("op", "o/r#1", gh.GhCliError("upstream down"))
        self.assertEqual(resp.status, 502)


if __name__ == "__main__":
    unittest.main()
