"""Tests for the pull-request feature (list + detail + checks + AI summary).

Covers the new backend logic that carries real behaviour:
  * github_client timeline normalization for the PR-only ``reviewed`` /
    ``committed`` events (additive to the shared issue-timeline normalizer);
  * the PR list / detail / check ``gh`` calls, exercised by monkeypatching the
    subprocess boundary so the path building + shaping run without a real ``gh``;
  * the store PR list cache (schema-stamped, stale-schema miss) and the PR
    detail cache (round-trips detail + timeline + checks, TTL expiry, None when
    absent).
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import routes, store


class TestPrTimelineEvents(unittest.TestCase):
    def test_reviewed_uses_submitted_at_and_state(self):
        ev = gh._normalize_timeline_event({
            "event": "reviewed", "user": {"login": "alice"},
            "submitted_at": "2024-02-01T00:00:00Z", "state": "approved", "body": "LGTM",
        })
        assert ev is not None
        self.assertEqual(ev["kind"], "reviewed")
        self.assertEqual(ev["actor"], "alice")
        self.assertEqual(ev["created_at"], "2024-02-01T00:00:00Z")
        self.assertEqual(ev["review_state"], "approved")
        self.assertEqual(ev["body"], "LGTM")

    def test_committed_uses_author_date_and_first_message_line(self):
        ev = gh._normalize_timeline_event({
            "event": "committed", "sha": "abcdef1234",
            "author": {"name": "Bob", "date": "2024-02-02T00:00:00Z"},
            "message": "fix: the thing\n\nlong body",
        })
        assert ev is not None
        self.assertEqual(ev["kind"], "committed")
        self.assertEqual(ev["actor"], "Bob")
        self.assertEqual(ev["created_at"], "2024-02-02T00:00:00Z")
        self.assertEqual(ev["commit_id"], "abcdef1234")
        self.assertEqual(ev["message"], "fix: the thing")

    def test_issue_events_still_normalized(self):
        # Regression guard: the additive PR branches must not disturb the
        # existing issue-event handling.
        ev = gh._normalize_timeline_event({
            "event": "labeled", "actor": {"login": "bob"},
            "created_at": "2024-01-01T00:00:00Z", "label": {"name": "bug", "color": "ee0000"},
        })
        assert ev is not None
        self.assertEqual(ev["kind"], "labeled")


class TestListPulls(unittest.TestCase):
    def test_open_paginates_and_shapes(self):
        raw = [{
            "number": 5, "title": "Add feature", "html_url": "https://x/pull/5",
            "state": "open", "draft": True, "labels": [{"name": "enhancement"}],
            "user": {"login": "alice"}, "author_association": "MEMBER",
            "updated_at": "2024-02-02T00:00:00Z", "created_at": "2024-02-01T00:00:00Z",
            "closed_at": None, "merged_at": None,
            "assignees": [{"login": "bob"}], "requested_reviewers": [{"login": "carol"}],
            "base": {"ref": "main"}, "head": {"ref": "feat/x"}, "body": "desc",
        }]
        # _run_gh_api runs the JQ itself in the real path; here we assert the
        # path + paginate flag and let the (already-shaped) raw pass through.
        with mock.patch.object(gh, "_run_gh_api", return_value=raw) as m:
            out = gh.list_open_pulls("o", "r")
        self.assertEqual(out, raw)
        path, _jq = m.call_args.args[0], m.call_args.args[1]
        self.assertIn("/pulls?state=open", path)
        self.assertIn("draft", _jq)
        self.assertTrue(m.call_args.kwargs["paginate"])

    def test_closed_is_bounded(self):
        with mock.patch.object(gh, "_run_gh_api", return_value=[]) as m:
            gh.list_closed_pulls("o", "r")
        self.assertIn("/pulls?state=closed", m.call_args.args[0])
        self.assertFalse(m.call_args.kwargs["paginate"])


class TestPrDetailAndChecks(unittest.TestCase):
    def test_get_pr_detail_coerces_number_into_path(self):
        proc = mock.Mock(returncode=0, stdout=json.dumps({"number": 7, "title": "t"}), stderr="")
        with mock.patch.object(gh, "_gh_run", return_value=proc) as m:
            out = gh.get_pr_detail("o", "r", 7)
        self.assertEqual(out["number"], 7)
        argv = m.call_args.args[0]
        self.assertIn("repos/o/r/pulls/7", argv)

    def test_list_pr_checks_merges_both_surfaces_and_orders(self):
        check_runs = [
            {"name": "ci / build", "status": "completed", "conclusion": "success",
             "url": None, "started_at": "2024-02-01T00:00:00Z",
             "completed_at": "2024-02-01T00:05:00Z", "summary": "", "app": "GitHub Actions"},
            {"name": "auto-review", "status": "completed", "conclusion": "failure",
             "url": "https://x/run/2", "started_at": "2024-02-01T00:00:00Z",
             "completed_at": "2024-02-01T00:06:00Z", "summary": "2 blocking findings",
             "app": "AutoSDE"},
            {"name": "ci / test", "status": "in_progress", "conclusion": None,
             "url": None, "started_at": "2024-02-01T00:00:00Z",
             "completed_at": None, "summary": "", "app": "GitHub Actions"},
        ]
        commit_statuses = [
            {"name": "legacy/lint", "status": "completed", "conclusion": "pending",
             "url": None, "started_at": "2024-02-01T00:00:00Z",
             "completed_at": "2024-02-01T00:01:00Z", "summary": "queued", "app": None},
        ]
        with mock.patch.object(gh, "_run_gh_api", side_effect=[check_runs, commit_statuses]):
            out = gh.list_pr_checks("o", "r", "abc1234")
        by_name = {c["name"]: c for c in out}
        self.assertEqual(by_name["auto-review"]["bucket"], "failure")
        self.assertEqual(by_name["ci / test"]["bucket"], "running")
        # A commit status whose state is "pending" belongs in the running bucket.
        self.assertEqual(by_name["legacy/lint"]["bucket"], "running")
        self.assertEqual(by_name["ci / build"]["bucket"], "success")
        # Failures first, then running, then success — the actionable rows lead.
        self.assertEqual(out[0]["bucket"], "failure")
        self.assertEqual(out[-1]["bucket"], "success")

    def test_list_pr_checks_rejects_non_sha(self):
        # The sha lands in the request path, so anything non-hex is refused
        # before it can reach the argv.
        for bad in ("../../etc", "abc/def", "", "zzz"):
            with self.assertRaises(gh.GhCliError):
                gh.list_pr_checks("o", "r", bad)

    def test_list_pr_checks_tolerates_one_surface_failing(self):
        runs = [{"name": "ci", "status": "completed", "conclusion": "success",
                 "url": None, "started_at": None, "completed_at": None,
                 "summary": "", "app": None, "source": "gha"}]
        # Second call (commit statuses) reports the surface as ABSENT — the rows
        # already read must still come back rather than the whole section erroring
        # out. (A transient failure there is a different case; see
        # test_checks_raise_on_a_TRANSIENT_surface_failure_even_with_rows.)
        with mock.patch.object(
            gh, "_run_gh_api", side_effect=[runs, gh.GhCliError("gh api ... HTTP 403")]
        ):
            out = gh.list_pr_checks("o", "r", "abc1234")
        self.assertEqual([c["name"] for c in out], ["ci"])

    def test_same_name_checks_collapse_to_the_latest_run(self):
        # GitHub's filter=latest collapses re-ATTEMPTS, but the same workflow file
        # can still be started as two separate RUNS for one head sha (observed:
        # code-review.yml triggered twice 12s apart, each run_attempt 1). Each run
        # contributes its own row per job, so the list must collapse them or the
        # sidebar shows "PR Hygiene" twice. Latest run wins.
        rows: list[dict] = [
            {"name": "PR Hygiene", "conclusion": "failure",
             "started_at": "2026-07-26T01:05:21Z", "completed_at": "2026-07-26T01:05:30Z"},
            {"name": "PR Hygiene", "conclusion": "success",
             "started_at": "2026-07-26T01:05:33Z", "completed_at": "2026-07-26T01:05:45Z"},
            {"name": None, "conclusion": "success"},
        ]
        out = gh._dedupe_checks(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["conclusion"], "success")
        # The nameless row is dropped: it would render as a blank line.
        self.assertTrue(all(r["name"] for r in out))

    def test_check_bucket_unknown_conclusion_is_not_success(self):
        # Defensive: an unrecognized conclusion must never read as passing.
        self.assertEqual(gh._check_bucket("completed", "some_new_value"), "other")
        self.assertEqual(gh._check_bucket("completed", None), "other")


class TestPullsCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_cache_roundtrip_per_state(self):
        openp = [{"number": 1}]
        closedp = [{"number": 2}]
        store.write_pulls_cache("o", "r", openp, root=self.tmp, state="open")
        store.write_pulls_cache("o", "r", closedp, root=self.tmp, state="closed")
        self.assertEqual(store.read_pulls_cache("o", "r", self.tmp, state="open"), openp)
        self.assertEqual(store.read_pulls_cache("o", "r", self.tmp, state="closed"), closedp)

    def test_stale_schema_is_a_miss(self):
        path = store.pulls_cache_path("o", "r", self.tmp, state="open")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 0, "pulls": [{"number": 1}]}), encoding="utf-8")
        self.assertIsNone(store.read_pulls_cache("o", "r", self.tmp, state="open"))

    def test_detail_cache_roundtrip(self):
        detail = {"number": 7, "title": "t", "additions": 3}
        timeline = [{"kind": "reviewed", "actor": "alice"}]
        checks = [{"name": "ci", "bucket": "success"}]
        store.write_pr_detail_cache("o", "r", 7, detail, timeline, checks, root=self.tmp)
        got = store.read_pr_detail_cache("o", "r", 7, self.tmp)
        assert got is not None
        self.assertEqual(got["detail"], detail)
        self.assertEqual(got["timeline"], timeline)
        self.assertEqual(got["checks"], checks)

    def test_detail_absent_returns_none(self):
        self.assertIsNone(store.read_pr_detail_cache("o", "r", 999, self.tmp))

    def test_detail_cache_expires_by_age(self):
        # Freshness belongs to the CACHE, not to the caller: without a TTL the
        # route stays correct only while every consumer remembers to pass
        # refresh=1, so a plain GET from anywhere else is served frozen data.
        detail = {"number": 7, "title": "t"}
        store.write_pr_detail_cache("o", "r", 7, detail, [], [], root=self.tmp)
        self.assertIsNotNone(
            store.read_pr_detail_cache("o", "r", 7, self.tmp, max_age_sec=60)
        )
        path = store.pr_detail_cache_path("o", "r", 7, self.tmp)
        old = time.time() - 600
        os.utime(path, (old, old))
        self.assertIsNone(store.read_pr_detail_cache("o", "r", 7, self.tmp, max_age_sec=60))
        # No max_age -> unconditional read (used by the AI route, which has its
        # own fingerprint-based staleness check).
        self.assertIsNotNone(store.read_pr_detail_cache("o", "r", 7, self.tmp))

    def test_detail_stale_schema_is_a_miss(self):
        # An entry written before ``checks`` existed (or with no stamp at all)
        # must NOT be served — otherwise the new field stays empty forever on
        # any PR the user had already opened.
        path = store.pr_detail_cache_path("o", "r", 7, self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"detail": {"number": 7}, "timeline": [], "files": []}), encoding="utf-8"
        )
        self.assertIsNone(store.read_pr_detail_cache("o", "r", 7, self.tmp))


class TestPrSearch(unittest.TestCase):
    """The per-person filters are answered by GitHub search, so the query string
    is the security- and correctness-critical surface: logins must be validated
    before they can reach it, and the lifecycle must map to the right
    qualifiers."""

    def test_state_qualifiers(self):
        q = gh.build_pr_search_query("o", "r", state="open", author="alice")
        self.assertIn("repo:o/r", q)
        self.assertIn("is:pr", q)
        self.assertIn("is:open", q)
        self.assertIn("author:alice", q)

    def test_merged_and_closed_unmerged_split(self):
        merged = gh.build_pr_search_query("o", "r", state="merged", author="alice")
        self.assertIn("is:merged", merged)
        closed = gh.build_pr_search_query("o", "r", state="closed", author="alice")
        # "closed" means closed WITHOUT merge — both qualifiers are required.
        self.assertIn("is:closed", closed)
        self.assertIn("is:unmerged", closed)

    def test_multiple_person_qualifiers_are_anded(self):
        q = gh.build_pr_search_query(
            "o", "r", state="open", author="alice", assignee="bob", review_requested="carol",
        )
        self.assertIn("author:alice", q)
        self.assertIn("assignee:bob", q)
        self.assertIn("review-requested:carol", q)

    def test_rejects_invalid_login(self):
        # A login carrying spaces/qualifier syntax must never reach the query.
        for bad in ("alice bob", "alice:x", "is:open", "a" * 40, ""):
            with self.assertRaises(gh.PrSearchError):
                gh.build_pr_search_query("o", "r", state="open", author=bad or None)

    def test_rejects_unknown_state(self):
        with self.assertRaises(gh.PrSearchError):
            gh.build_pr_search_query("o", "r", state="draft", author="alice")

    def test_requires_a_person_qualifier(self):
        with self.assertRaises(gh.PrSearchError):
            gh.build_pr_search_query("o", "r", state="open")

    def test_search_pulls_url_encodes_and_caps(self):
        rows = [{"number": n} for n in range(10)]
        with mock.patch.object(gh, "_run_gh_api", return_value=rows) as m:
            out = gh.search_pulls("o", "r", state="merged", author="alice", limit=3)
        self.assertEqual(len(out), 3)
        path = m.call_args.args[0]
        self.assertTrue(path.startswith("search/issues?q="))
        # The query is percent-encoded, so raw spaces never reach the argv.
        self.assertNotIn(" ", path)
        self.assertIn("is%3Amerged", path)
        self.assertIn("author%3Aalice", path)

    def test_search_pulls_stops_paginating_once_the_cap_is_met(self):
        # `--paginate` would walk every page GitHub offers before we sliced the
        # result down, so a prolific author's filter burned extra requests (and
        # could time out) for rows nobody asked for.
        with mock.patch.object(gh, "_run_gh_api", return_value=[{"number": 1}] * 100) as m:
            out = gh.search_pulls("o", "r", state="merged", author="alice", limit=250)
        self.assertEqual(len(out), 250)
        # 250 wanted / 100 per page -> 3 pages, not "everything then slice".
        self.assertEqual(m.call_count, 3)
        for call in m.call_args_list:
            self.assertFalse(call.kwargs.get("paginate", False))

    def test_search_pulls_stops_on_a_short_page(self):
        with mock.patch.object(gh, "_run_gh_api", return_value=[{"number": 1}] * 4) as m:
            out = gh.search_pulls("o", "r", state="open", author="alice", limit=300)
        self.assertEqual(len(out), 4)
        self.assertEqual(m.call_count, 1)


class TestPrListEnrichment(unittest.TestCase):
    """The list cards' diff size + aggregate check state come from ONE GraphQL
    call, and the whole thing is optional — a failure must never break the list."""

    def _proc(self, lines):
        return mock.Mock(returncode=0, stdout="\n".join(json.dumps(x) for x in lines), stderr="")

    def test_summaries_bucket_rollup_states(self):
        rows = [
            {"number": 1, "additions": 10, "deletions": 2, "rollup": "SUCCESS"},
            {"number": 2, "additions": 0, "deletions": 0, "rollup": "FAILURE"},
            {"number": 3, "additions": 5, "deletions": 1, "rollup": "PENDING"},
            {"number": 4, "additions": 1, "deletions": 0, "rollup": None},
            {"number": 5, "additions": 1, "deletions": 0, "rollup": "SOMETHING_NEW"},
        ]
        with mock.patch.object(gh, "_gh_run", return_value=self._proc(rows)):
            out = gh.fetch_pr_summaries("o", "r", "open")
        self.assertEqual(out[1]["checks_state"], "success")
        self.assertEqual(out[2]["checks_state"], "failure")
        self.assertEqual(out[3]["checks_state"], "running")
        # No checks at all -> None (the card shows no dot).
        self.assertIsNone(out[4]["checks_state"])
        # An unknown rollup value must not read as passing.
        self.assertEqual(out[5]["checks_state"], "other")
        self.assertEqual(out[1]["additions"], 10)

    def test_summaries_count_check_buckets(self):
        def ctx(name, state, ts, source="ci"):
            return {"name": name, "source": source, "status": None,
                    "conclusion": state, "started_at": ts, "completed_at": ts}

        rows = [{
            "number": 7, "additions": 3, "deletions": 1, "changed_files": 2,
            "rollup": "FAILURE",
            "contexts": [
                ctx("a", "SUCCESS", "1"), ctx("b", "SUCCESS", "1"), ctx("c", "FAILURE", "1"),
                ctx("d", "IN_PROGRESS", "1"), ctx("e", "QUEUED", "1"), ctx("f", "TIMED_OUT", "1"),
                ctx("g", "SOMETHING_NEW", "1"), ctx("h", "", "1"), ctx("i", "SKIPPED", "1"),
            ],
        }]
        with mock.patch.object(gh, "_gh_run", return_value=self._proc(rows)):
            out = gh.fetch_pr_summaries("o", "r", "open")
        counts = out[7]["checks_counts"]
        self.assertEqual(counts["success"], 2)
        # FAILURE + TIMED_OUT both count as failing.
        self.assertEqual(counts["failure"], 2)
        # IN_PROGRESS + QUEUED both count as running.
        self.assertEqual(counts["running"], 2)
        # Unknown, empty, and skipped states land in "other" — never "success".
        self.assertEqual(counts["other"], 3)
        self.assertEqual(out[7]["changed_files"], 2)

    def test_summaries_counts_collapse_same_name_runs(self):
        # Same rule as the sidebar list: two runs of one job for the same sha
        # count ONCE, and the later run's verdict is the one that counts.
        rows = [{
            "number": 9, "additions": 0, "deletions": 0, "changed_files": 0,
            "rollup": "SUCCESS",
            "contexts": [
                {"name": "PR Hygiene", "source": "github-actions", "status": None,
                 "conclusion": "FAILURE", "started_at": "2026-07-26T01:05:30Z",
                 "completed_at": "2026-07-26T01:05:40Z"},
                {"name": "PR Hygiene", "source": "github-actions", "status": None,
                 "conclusion": "SUCCESS", "started_at": "2026-07-26T01:05:45Z",
                 "completed_at": "2026-07-26T01:05:55Z"},
            ],
        }]
        with mock.patch.object(gh, "_gh_run", return_value=self._proc(rows)):
            out = gh.fetch_pr_summaries("o", "r", "open")
        counts = out[9]["checks_counts"]
        self.assertEqual(counts["success"], 1)
        self.assertEqual(counts["failure"], 0)

    def test_summaries_counts_keep_same_name_checks_from_DIFFERENT_apps(self):
        # Two apps may publish the same display name. Collapsing across them
        # would let one app's success hide the other's failure.
        rows = [{
            "number": 11, "additions": 0, "deletions": 0, "changed_files": 0,
            "rollup": "FAILURE",
            "contexts": [
                {"name": "build", "source": "github-actions", "status": None,
                 "conclusion": "SUCCESS", "started_at": "2", "completed_at": "2"},
                {"name": "build", "source": "circleci", "status": None,
                 "conclusion": "FAILURE", "started_at": "1", "completed_at": "1"},
            ],
        }]
        with mock.patch.object(gh, "_gh_run", return_value=self._proc(rows)):
            out = gh.fetch_pr_summaries("o", "r", "open")
        counts = out[11]["checks_counts"]
        self.assertEqual((counts["success"], counts["failure"]), (1, 1))

    def test_summaries_flag_a_truncated_context_page(self):
        # More checks than one page: the tally is incomplete, and the card must
        # be told so it can fall back to the aggregate rollup.
        rows = [{"number": 12, "additions": 0, "deletions": 0, "changed_files": 0,
                 "rollup": "SUCCESS", "contexts_truncated": True, "contexts": []}]
        with mock.patch.object(gh, "_gh_run", return_value=self._proc(rows)):
            out = gh.fetch_pr_summaries("o", "r", "open")
        self.assertTrue(out[12]["checks_truncated"])

    def test_summaries_counts_always_carry_every_bucket(self):
        # The card reads counts[bucket] directly, so a hole would render as NaN.
        rows: list[dict] = [{"number": 8, "additions": 0, "deletions": 0,
                             "changed_files": 0, "rollup": None, "contexts": []}]
        with mock.patch.object(gh, "_gh_run", return_value=self._proc(rows)):
            out = gh.fetch_pr_summaries("o", "r", "open")
        self.assertEqual(out[8]["checks_counts"], {"failure": 0, "running": 0,
                                                   "success": 0, "other": 0})

    def test_summaries_rejects_unknown_state(self):
        with self.assertRaises(gh.GhCliError):
            gh.fetch_pr_summaries("o", "r", "draft")

    def test_enrich_pulls_merges(self):
        pulls = [{"number": 1}, {"number": 2}]
        summaries = {
            1: {"additions": 7, "deletions": 3, "checks_state": "failure"},
            2: {"additions": 0, "deletions": 0, "checks_state": None},
        }
        with mock.patch.object(gh, "fetch_pr_summaries", return_value=summaries), \
                mock.patch.object(gh, "fetch_pr_readiness", return_value={}):
            out = gh.enrich_pulls("o", "r", pulls, "open")
        self.assertEqual(out[0]["additions"], 7)
        self.assertEqual(out[0]["checks_state"], "failure")
        self.assertIsNone(out[1]["checks_state"])

    def test_enrich_pulls_degrades_when_graphql_fails(self):
        # The enrichment is decoration; its failure must leave the rows usable.
        pulls = [{"number": 1, "title": "t"}]
        with mock.patch.object(gh, "fetch_pr_summaries", side_effect=gh.GhCliError("boom")), \
                mock.patch.object(
                    gh, "fetch_pr_summaries_by_number", side_effect=gh.GhCliError("boom")), \
                mock.patch.object(gh, "fetch_pr_readiness", side_effect=gh.GhCliError("boom")):
            out = gh.enrich_pulls("o", "r", pulls, "open")
        self.assertEqual(out[0]["title"], "t")
        # UNKNOWN, not zero: 0 would claim the PR changes nothing, and the route
        # would cache that claim (see enrichment_complete).
        self.assertIsNone(out[0]["additions"])
        self.assertIsNone(out[0]["changed_files"])
        self.assertIsNone(out[0]["checks_state"])
        self.assertIsNone(out[0]["checks_counts"])
        self.assertFalse(gh.enrichment_complete(out))

    def test_enrichment_complete_only_when_every_row_got_its_summary(self):
        rows = [{"number": 1, "additions": 3, "deletions": 0, "changed_files": 1,
                 "checks_state": "success",
                 "checks_counts": {"failure": 0, "running": 0, "success": 1, "other": 0}}]
        self.assertTrue(gh.enrichment_complete(rows))
        self.assertFalse(gh.enrichment_complete(rows + [{"number": 2, "checks_counts": None}]))

    def test_summaries_by_number_addresses_each_pr(self):
        # Search hits can rank outside the recently-updated window, so the search
        # path must ask for the exact numbers rather than a state-scoped page.
        rows = [
            {"number": 235, "additions": 84, "deletions": 7, "rollup": "SUCCESS"},
            {"number": 999, "additions": 1, "deletions": 1, "rollup": "FAILURE"},
        ]
        with mock.patch.object(gh, "_gh_run", return_value=self._proc(rows)) as m:
            out = gh.fetch_pr_summaries_by_number("o", "r", [235, 999])
        query = next(a for a in m.call_args.args[0] if a.startswith("query="))
        self.assertIn("pullRequest(number:235)", query)
        self.assertIn("pullRequest(number:999)", query)
        self.assertEqual(out[235]["additions"], 84)
        self.assertEqual(out[999]["checks_state"], "failure")

    def test_summaries_by_number_batches_and_skips_bad_numbers(self):
        with mock.patch.object(gh, "_gh_run", return_value=self._proc([])) as m:
            gh.fetch_pr_summaries_by_number("o", "r", list(range(1, 151)))
        # 150 numbers at a batch size of 100 -> exactly two calls.
        self.assertEqual(m.call_count, 2)
        with mock.patch.object(gh, "_gh_run", return_value=self._proc([])) as m:
            gh.fetch_pr_summaries_by_number("o", "r", [None, 0, -3])  # type: ignore[list-item]
        # Nothing addressable -> no call at all.
        self.assertEqual(m.call_count, 0)

    def test_enrich_pulls_by_number_degrades_when_graphql_fails(self):
        pulls = [{"number": 235, "title": "t"}]
        with mock.patch.object(
                    gh, "fetch_pr_summaries_by_number", side_effect=gh.GhCliError("boom")), \
                mock.patch.object(
                    gh, "fetch_pr_readiness_by_number", side_effect=gh.GhCliError("boom")):
            out = gh.enrich_pulls_by_number("o", "r", pulls)
        self.assertEqual(out[0]["title"], "t")
        self.assertIsNone(out[0]["additions"])
        self.assertIsNone(out[0]["checks_state"])


class TestPrListMergeReadiness(unittest.TestCase):
    """Merge READINESS rides on the list row, in REST's vocabulary.

    The list row is what a BULK action sees, and it carried no mergeability at all —
    so the bulk bar offered "arm auto-merge" for every selected PR and GitHub refused
    each one that was already mergeable ("Pull request is in clean status") or already
    merged ("Pull request is already merged"): one failure per row, from one click.
    The field is free (this GraphQL call already walks the head commit for its check
    rollup), so the fix is to carry it, normalized into the spelling every reader —
    ``routes._MERGE_ALLOWED_STATES``, the frontend's ``MERGE_READY_STATES`` — uses.
    """

    def _proc(self, lines):
        return mock.Mock(returncode=0, stdout="\n".join(json.dumps(x) for x in lines), stderr="")

    def _no_spawn(self):
        """Fail loudly if a test reaches a real ``gh`` process.

        AGENTS.md: "Mock external processes — never spawn real processes in tests."
        ``enrich_pulls`` now makes THREE calls (summaries, by-number top-up, readiness),
        so mocking only the first leaves the others to shell out — which measurably
        happened, and which makes the suite depend on network and on a logged-in CLI.
        """
        return mock.patch.object(
            gh, "_gh_run",
            side_effect=AssertionError("test reached a real `gh` subprocess"),
        )

    def test_readiness_is_a_SEPARATE_query_from_the_card_enrichment(self):
        # THE load-bearing structural assertion of this feature.
        #
        # `mergeStateStatus` is the one field GitHub has to COMPUTE (a merge commit per
        # PR). Folded into the card selection — which already walks each head commit and
        # paginates the whole check rollup — the combined query reliably 502s at
        # first:100, and the failure is not graceful: both enrichment paths carry that
        # selection, so every row loses its diff size AND its readiness, leaving the
        # bulk bar as blind as before while the list visibly regresses.
        #
        # So it must stay OUT of `_PR_SUMMARY_SELECTION` and travel in its own lean
        # call. An earlier revision of this change had it inline and shipped exactly
        # that outage.
        self.assertNotIn("mergeStateStatus", gh._PR_SUMMARY_SELECTION)
        self.assertIn("mergeStateStatus", gh._PR_READINESS_SELECTION)
        # The lean query carries NOTHING else expensive — no rollup, no commit walk.
        for expensive in ("statusCheckRollup", "commits(", "changedFiles"):
            self.assertNotIn(expensive, gh._PR_READINESS_SELECTION)
        # The cheap lifecycle fields DO ride along on the card query (measured free).
        for free in ("mergeable", "state", "mergedAt"):
            self.assertIn(free, gh._PR_SUMMARY_SELECTION)

    def test_the_readiness_query_asks_for_the_field(self):
        with mock.patch.object(gh, "_gh_run", return_value=self._proc([])) as m:
            gh.fetch_pr_readiness("o", "r", "open")
        query = next(a for a in m.call_args.args[0] if a.startswith("query="))
        self.assertIn("mergeStateStatus", query)

    def test_graphql_enums_are_lowered_into_rest_vocabulary(self):
        # GraphQL SHOUTS (`CLEAN`), REST does not (`clean`). Both gates compare
        # lowercase, so an un-lowered value would match neither and read as
        # "not ready" — silently keeping the broken arm on offer.
        rows = [
            {"number": 1, "merge_state_status": "CLEAN"},
            {"number": 2, "merge_state_status": "BLOCKED"},
            {"number": 3, "merge_state_status": "DIRTY"},
        ]
        with mock.patch.object(gh, "_gh_run", return_value=self._proc(rows)):
            out = gh.fetch_pr_readiness("o", "r", "open")
        self.assertEqual(out[1], "clean")
        self.assertEqual(out[2], "blocked")
        self.assertEqual(out[3], "dirty")

    def test_mergeable_is_not_readiness(self):
        # `mergeable` means only "no CONFLICTS" — a PR with unsatisfied required reviews
        # is mergeable:true with mergeable_state:"blocked", which is exactly why the
        # readiness gate keys off the latter.
        rows = [
            {"number": 1, "mergeable_raw": "MERGEABLE", "pr_state": "OPEN"},
            {"number": 2, "mergeable_raw": "CONFLICTING", "pr_state": "OPEN"},
        ]
        with mock.patch.object(gh, "_gh_run", return_value=self._proc(rows)):
            out = gh.fetch_pr_summaries("o", "r", "open")
        self.assertTrue(out[1]["mergeable"])
        self.assertFalse(out[2]["mergeable"])

    def test_unknown_mergeability_stays_unknown(self):
        # GitHub computes it asynchronously, so a cold read answers UNKNOWN — measured at
        # roughly HALF a page on a busy repo, so this is the common case, not the edge.
        # It must NOT collapse to False: "we cannot tell" and "it conflicts" are
        # different facts, and a gate that cannot tell must refuse rather than assert.
        with mock.patch.object(
            gh, "_gh_run", return_value=self._proc([{"number": 1, "merge_state_status": "UNKNOWN"}])
        ):
            self.assertEqual(gh.fetch_pr_readiness("o", "r", "open")[1], "unknown")
        with mock.patch.object(
            gh, "_gh_run", return_value=self._proc([{"number": 1, "mergeable_raw": "UNKNOWN"}])
        ):
            self.assertIsNone(gh.fetch_pr_summaries("o", "r", "open")[1]["mergeable"])

    def test_a_missing_field_is_none_not_a_value(self):
        rows = [{"number": 1, "additions": 1, "deletions": 0}]
        with mock.patch.object(gh, "_gh_run", return_value=self._proc(rows)):
            out = gh.fetch_pr_summaries("o", "r", "open")
        self.assertIsNone(out[1]["mergeable_state"])
        self.assertIsNone(out[1]["mergeable"])

    def test_readiness_survives_a_FAILED_card_enrichment_and_vice_versa(self):
        # The two calls fail INDEPENDENTLY. A row can legitimately have a known merge
        # state but no diff size, or the reverse — collapsing them would mean one
        # transient 502 costs both, which is the outage this split exists to prevent.
        pulls = [{"number": 1, "title": "t"}]
        with self._no_spawn(), \
                mock.patch.object(gh, "fetch_pr_summaries", side_effect=gh.GhCliError("boom")), \
                mock.patch.object(
                    gh, "fetch_pr_summaries_by_number", side_effect=gh.GhCliError("boom")), \
                mock.patch.object(gh, "fetch_pr_readiness", return_value={1: "clean"}):
            out = gh.enrich_pulls("o", "r", pulls, "open")
        self.assertEqual(out[0]["mergeable_state"], "clean")   # readiness survived
        self.assertIsNone(out[0]["additions"])                 # enrichment did not

        pulls = [{"number": 1, "title": "t"}]
        summaries = {1: {"additions": 7, "deletions": 0, "changed_files": 1,
                         "checks_state": "success",
                         "checks_counts": {"failure": 0, "running": 0,
                                           "success": 1, "other": 0},
                         "mergeable": True}}
        with self._no_spawn(), \
                mock.patch.object(gh, "fetch_pr_summaries", return_value=summaries), \
                mock.patch.object(gh, "fetch_pr_readiness", side_effect=gh.GhCliError("boom")):
            out = gh.enrich_pulls("o", "r", pulls, "open")
        self.assertEqual(out[0]["additions"], 7)               # enrichment survived
        self.assertIsNone(out[0]["mergeable_state"])           # readiness did not

    def test_readiness_by_number_batches_below_the_page_ceiling(self):
        # Smaller than the summary batch on purpose: this is the computed field, and the
        # page-size ceiling is exactly what the split exists to respect.
        self.assertLess(gh._READINESS_BATCH, gh._SUMMARY_BATCH)
        with mock.patch.object(gh, "_gh_run", return_value=self._proc([])) as m:
            gh.fetch_pr_readiness_by_number("o", "r", list(range(1, 101)))
        # 100 numbers at a batch of 50 -> exactly two calls.
        self.assertEqual(m.call_count, 2)

    def test_merged_becomes_closed_plus_a_timestamp(self):
        # GraphQL has a third state (MERGED); REST models the same fact as
        # `closed` + `merged_at`. The row shape is REST's, and
        # `PrList.prStateVisual` checks `merged_at` FIRST — so reporting "merged"
        # here would match no branch and paint a merged PR as green/open.
        rows = [{"number": 1, "pr_state": "MERGED", "pr_merged_at": "2026-08-03T06:45:33Z"}]
        with mock.patch.object(gh, "_gh_run", return_value=self._proc(rows)):
            out = gh.fetch_pr_summaries("o", "r", "open")
        self.assertEqual(out[1]["pr_state"], "closed")
        self.assertEqual(out[1]["pr_merged_at"], "2026-08-03T06:45:33Z")

    def test_enrichment_corrects_a_stale_cached_row(self):
        # The #1265 case: the row was cached while the PR was open, the user armed
        # auto-merge from it, and GitHub answered "already merged".
        pulls = [{"number": 1265, "state": "open", "merged_at": None}]
        summaries = {1265: {"additions": 1, "deletions": 0, "changed_files": 1,
                            "checks_state": "success",
                            "checks_counts": {"failure": 0, "running": 0,
                                              "success": 1, "other": 0},
                            "mergeable_state": "unknown", "mergeable": None,
                            "pr_state": "closed",
                            "pr_merged_at": "2026-08-03T06:45:33Z"}}
        with self._no_spawn(), \
                mock.patch.object(gh, "fetch_pr_summaries", return_value=summaries), \
                mock.patch.object(gh, "fetch_pr_readiness", return_value={}):
            out = gh.enrich_pulls("o", "r", pulls, "open")
        self.assertEqual(out[0]["state"], "closed")
        # Written TOGETHER with the state: a `closed` with no timestamp renders as
        # the red closed-unmerged icon rather than as merged.
        self.assertEqual(out[0]["merged_at"], "2026-08-03T06:45:33Z")

    def test_enrichment_failure_states_readiness_as_unknown(self):
        # Absent-and-falsy would read as "not ready" and put the row straight back
        # into the auto-merge batch the provider refuses.
        pulls = [{"number": 1, "title": "t"}]
        with self._no_spawn(), \
                mock.patch.object(gh, "fetch_pr_summaries", side_effect=gh.GhCliError("boom")), \
                mock.patch.object(
                    gh, "fetch_pr_summaries_by_number", side_effect=gh.GhCliError("boom")), \
                mock.patch.object(gh, "fetch_pr_readiness", side_effect=gh.GhCliError("boom")):
            out = gh.enrich_pulls("o", "r", pulls, "open")
        self.assertIn("mergeable_state", out[0])
        self.assertIsNone(out[0]["mergeable_state"])
        self.assertIsNone(out[0]["mergeable"])

    def test_a_live_row_keeps_its_own_merged_at(self):
        # Only ever fills a GAP — a row that already carries a timestamp keeps it.
        pulls = [{"number": 1, "state": "closed", "merged_at": "2026-01-01T00:00:00Z"}]
        summaries = {1: {"mergeable_state": None, "mergeable": None,
                         "pr_state": "closed", "pr_merged_at": "2026-08-03T06:45:33Z"}}
        with self._no_spawn(), \
                mock.patch.object(gh, "fetch_pr_summaries", return_value=summaries), \
                mock.patch.object(gh, "fetch_pr_readiness", return_value={}):
            out = gh.enrich_pulls("o", "r", pulls, "open")
        self.assertEqual(out[0]["merged_at"], "2026-01-01T00:00:00Z")


class TestPrAiSummary(unittest.TestCase):
    """The PR summary prompt (what the model is given) and the fingerprint-keyed
    cache (when a summary is allowed to be reused)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.detail = {
            "number": 7, "title": "Add retry", "body": "Adds a retry.",
            "state": "open", "draft": False, "merged_at": None,
            "head_sha": "abc123", "updated_at": "2024-01-05T00:00:00Z",
            "base": "main", "head": "feat/retry",
            "additions": 10, "deletions": 2, "changed_files": 3, "commits": 1,
            "author": "alice",
        }
        # NOTE the kinds: github_client NORMALIZES GitHub's "commented" event to
        # kind "comment", and inline code-anchored notes to "review_comment".
        # Matching the raw event name here is what silently dropped every ordinary
        # comment from the prompt, so these fixtures use the normalized shape.
        self.timeline: list[dict] = [
            {"kind": "comment", "actor": "bob", "created_at": "2024-01-02T00:00:00Z",
             "body": "This needs a test."},
            {"kind": "reviewed", "actor": "carol", "created_at": "2024-01-03T00:00:00Z",
             "review_state": "CHANGES_REQUESTED", "body": "Retry is unbounded."},
            {"kind": "review_comment", "actor": "erin", "created_at": "2024-01-03T12:00:00Z",
             "path": "src/retry.py", "line": 42, "body": "Cap this at 3 attempts."},
            # Non-comment events and empty bodies carry no discussion signal.
            {"kind": "committed", "actor": "alice", "created_at": "2024-01-04T00:00:00Z"},
            {"kind": "comment", "actor": "dave", "created_at": "2024-01-04T01:00:00Z",
             "body": "   "},
        ]
        self.checks = [
            {"name": "unit", "bucket": "success"},
            {"name": "lint", "bucket": "failure"},
        ]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prompt_carries_conversation_state_and_checks(self):
        p = routes._build_pr_ai_prompt("o", "r", self.detail, self.timeline, self.checks)
        # The description AND every substantive comment/review must be present —
        # a PR's real state often lives in the review, not the description.
        self.assertIn("Adds a retry.", p)
        self.assertIn("This needs a test.", p)
        self.assertIn("Retry is unbounded.", p)
        # Inline code-anchored comments are part of the review conversation, and
        # the prompt names the file/line they are attached to.
        self.assertIn("Cap this at 3 attempts.", p)
        self.assertIn("src/retry.py:42", p)
        # A review's verdict is what makes it actionable.
        self.assertIn("changes requested", p)
        # Lifecycle, diff shape, and the failing check name are all context the
        # summary is asked to lead with.
        self.assertIn("State: open", p)
        self.assertIn("+10 / -2", p)
        self.assertIn("1 failure", p)
        # Check NAMES are provider-controlled, so they must appear INSIDE the
        # fenced untrusted block, never in the trusted header above it.
        head, _, fenced = p.partition("<pull-request>")
        self.assertNotIn("lint", head)
        self.assertIn("lint", fenced)
        # Untrusted PR text must be fenced and declared as data.
        self.assertIn("<pull-request>", p)
        self.assertIn("as DATA", p)

    def test_prompt_skips_empty_and_non_comment_events(self):
        rows = routes._pr_ai_comment_rows(self.timeline)
        self.assertEqual([r["actor"] for r in rows], ["bob", "carol", "erin"])

    def test_prompt_caps_comment_count_and_length(self):
        many = [
            {"kind": "comment", "actor": f"u{i}", "created_at": "2024-01-01T00:00:00Z",
             "body": "x" * (routes._PR_AI_COMMENT_MAX_CHARS + 500)}
            for i in range(routes._PR_AI_MAX_COMMENTS + 10)
        ]
        rows = routes._pr_ai_comment_rows(many)
        # Bounded, and the NEWEST are what survive — they carry current state.
        self.assertEqual(len(rows), routes._PR_AI_MAX_COMMENTS)
        self.assertEqual(rows[-1]["actor"], f"u{routes._PR_AI_MAX_COMMENTS + 9}")
        p = routes._build_pr_ai_prompt("o", "r", self.detail, many, [])
        self.assertIn("…(truncated)", p)

    def test_review_verdicts_survive_the_comment_cap(self):
        # The cap exists to bound CHATTER. An old change-request is exactly the
        # "unanswered objection" the prompt is told to report, so it must not be
        # truncated away by 40 later comments.
        old_verdict = {"kind": "reviewed", "actor": "carol", "created_at": "2024-01-01T00:00:00Z",
                       "review_state": "CHANGES_REQUESTED", "body": "Not like this."}
        chatter = [
            {"kind": "comment", "actor": f"u{i}", "created_at": f"2024-02-{i % 28 + 1:02d}T00:00:00Z",
             "body": "ping"}
            for i in range(routes._PR_AI_MAX_COMMENTS + 20)
        ]
        rows = routes._pr_ai_comment_rows([old_verdict] + chatter)
        self.assertIn(old_verdict, rows)
        self.assertEqual(len([r for r in rows if r["kind"] == "comment"]),
                         routes._PR_AI_MAX_COMMENTS)

    def test_review_verdicts_are_bounded_and_deduped_per_reviewer(self):
        # Privileged is not unlimited: a bot-heavy PR can carry hundreds of
        # reviews, and an unbounded prompt would exceed the model's context. Only
        # each reviewer's LATEST verdict carries current state.
        reviews = [
            {"kind": "reviewed", "actor": "bot", "created_at": f"2024-03-{i + 1:02d}T00:00:00Z",
             "review_state": "CHANGES_REQUESTED" if i < 9 else "APPROVED", "body": ""}
            for i in range(10)
        ]
        rows = routes._pr_ai_comment_rows(reviews)
        # One reviewer -> one verdict, the newest.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["review_state"], "APPROVED")

        many_reviewers = [
            {"kind": "reviewed", "actor": f"r{i}", "created_at": f"2024-04-{i % 28 + 1:02d}T00:00:00Z",
             "review_state": "APPROVED", "body": ""}
            for i in range(routes._PR_AI_MAX_VERDICTS + 15)
        ]
        rows = routes._pr_ai_comment_rows(many_reviewers)
        self.assertEqual(len(rows), routes._PR_AI_MAX_VERDICTS)

    def test_bodyless_review_verdict_is_kept(self):
        # GitHub approvals routinely have no body; dropping them made the summary
        # claim a PR was awaiting review while an approval sat right there.
        approval = {"kind": "reviewed", "actor": "carol", "created_at": "2024-01-09T00:00:00Z",
                    "review_state": "APPROVED", "body": ""}
        rows = routes._pr_ai_comment_rows([approval])
        self.assertEqual(rows, [approval])
        p = routes._build_pr_ai_prompt("o", "r", self.detail, [approval], [])
        self.assertIn("[review: approved]", p)
        self.assertIn("(no written comment)", p)

    def test_lifecycle_split(self):
        self.assertEqual(routes._pr_lifecycle(self.detail), "open")
        self.assertEqual(routes._pr_lifecycle({**self.detail, "draft": True}), "open (draft)")
        self.assertEqual(
            routes._pr_lifecycle({**self.detail, "merged_at": "2024-01-06T00:00:00Z"}), "merged"
        )
        self.assertEqual(
            routes._pr_lifecycle({**self.detail, "state": "closed"}),
            "closed without being merged",
        )

    def test_fingerprint_changes_with_every_summarized_input(self):
        base = routes._pr_ai_fingerprint(self.detail, self.timeline, self.checks)
        # Identical inputs -> identical digest (so an unchanged PR is not re-summarized).
        self.assertEqual(base, routes._pr_ai_fingerprint(self.detail, self.timeline, self.checks))
        # A new push, a state change, a new comment, an EDIT to the newest
        # comment, and a flipped check must each invalidate.
        for label, detail, timeline, checks in [
            ("push", {**self.detail, "head_sha": "def456"}, self.timeline, self.checks),
            ("merged", {**self.detail, "merged_at": "2024-01-06T00:00:00Z"}, self.timeline, self.checks),
            ("new comment", self.detail,
             self.timeline + [{"kind": "comment", "actor": "eve",
                               "created_at": "2024-01-07T00:00:00Z", "body": "lgtm"}],
             self.checks),
            ("edited newest comment", self.detail,
             self.timeline[:2] + [{**self.timeline[2], "body": "Cap this at FIVE attempts."}]
             + self.timeline[3:],
             self.checks),
            ("check flipped", self.detail, self.timeline,
             [{"name": "unit", "bucket": "success"}, {"name": "lint", "bucket": "success"}]),
        ]:
            with self.subTest(label):
                self.assertNotEqual(base, routes._pr_ai_fingerprint(detail, timeline, checks))

    def test_fingerprint_catches_an_edit_that_changes_no_metadata(self):
        # Editing a comment changes neither its created_at nor the comment count,
        # so a metadata-only digest would keep serving a summary written from text
        # that no longer exists. The body is hashed, so it cannot.
        base = routes._pr_ai_fingerprint(self.detail, self.timeline, self.checks)
        edited = list(self.timeline)
        edited[0] = {**edited[0], "body": "Actually this is fine."}
        self.assertNotEqual(base, routes._pr_ai_fingerprint(self.detail, edited, self.checks))
        # A review flipping verdict with the same (empty) body must also invalidate.
        reviewed = list(self.timeline)
        reviewed[1] = {**reviewed[1], "review_state": "APPROVED"}
        self.assertNotEqual(base, routes._pr_ai_fingerprint(self.detail, reviewed, self.checks))

    def test_cache_roundtrip_and_fingerprint_miss(self):
        store.write_pr_ai_cache("o", "r", 7, {"summary": "s", "fingerprint": "fp1"}, root=self.tmp)
        hit = store.read_pr_ai_cache("o", "r", 7, self.tmp, fingerprint="fp1")
        self.assertEqual((hit or {}).get("summary"), "s")
        # Stamped so the card can show how old the summary is.
        self.assertTrue((hit or {}).get("generated_at"))
        # A moved PR reads as a MISS, so the summary silently regenerates.
        self.assertIsNone(store.read_pr_ai_cache("o", "r", 7, self.tmp, fingerprint="fp2"))
        # No fingerprint supplied -> no staleness check (unconditional read).
        self.assertIsNotNone(store.read_pr_ai_cache("o", "r", 7, self.tmp))
        self.assertIsNone(store.read_pr_ai_cache("o", "r", 999, self.tmp, fingerprint="fp1"))

    def test_cache_written_before_stamping_falls_back_to_mtime(self):
        # A pre-existing cache file has no generated_at. Rather than show no age
        # at all until the user regenerates, fall back to the file's mtime —
        # which IS when that summary was written.
        path = store.pr_ai_cache_path("o", "r", 8, self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"summary": "old", "fingerprint": "fp1"}), encoding="utf-8")
        hit = store.read_pr_ai_cache("o", "r", 8, self.tmp, fingerprint="fp1")
        self.assertEqual((hit or {}).get("summary"), "old")
        stamp = (hit or {}).get("generated_at") or ""
        self.assertTrue(stamp.endswith("Z"), stamp)


class TestPrMergeabilityRetry(unittest.TestCase):
    """GitHub computes a PR's merge commit lazily: the first GET answers
    ``mergeable: null`` / ``"unknown"`` and only a follow-up sees the verdict."""

    def _detail(self, mergeable, state):
        return {"number": 7, "mergeable": mergeable, "mergeable_state": state}

    def test_retries_once_when_unknown_and_takes_resolved_answer(self):
        answers = [self._detail(None, "unknown"), self._detail(True, "blocked")]
        with mock.patch.object(gh, "_fetch_pr_detail_once", side_effect=answers) as m, \
                mock.patch.object(gh.time, "sleep") as sleep:
            out = gh.get_pr_detail("o", "r", 7)
        self.assertEqual(m.call_count, 2)
        sleep.assert_called_once()
        self.assertTrue(out["mergeable"])
        self.assertEqual(out["mergeable_state"], "blocked")

    def test_no_retry_when_first_answer_is_already_resolved(self):
        with mock.patch.object(
            gh, "_fetch_pr_detail_once", return_value=self._detail(False, "dirty")
        ) as m:
            out = gh.get_pr_detail("o", "r", 7)
        # One round-trip only — the common path must not pay for the retry.
        self.assertEqual(m.call_count, 1)
        self.assertEqual(out["mergeable_state"], "dirty")

    def test_keeps_first_answer_when_retry_is_still_unknown(self):
        answers = [self._detail(None, "unknown"), self._detail(None, "unknown")]
        with mock.patch.object(gh, "_fetch_pr_detail_once", side_effect=answers), \
                mock.patch.object(gh.time, "sleep"):
            out = gh.get_pr_detail("o", "r", 7)
        # Bounded at one retry: a PR GitHub genuinely cannot compute must not loop.
        self.assertEqual(out["mergeable_state"], "unknown")


class TestChecksWriteThrough(unittest.TestCase):
    """A detail fetch re-reads the checks; the PR's LIST row must learn about it,
    or the card keeps showing the state from the last whole-list refresh."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_summarize_prioritises_failure_then_running(self):
        s = gh.summarize_checks([
            {"name": "a", "bucket": "success"},
            {"name": "b", "bucket": "running"},
            {"name": "c", "bucket": "failure"},
        ])
        self.assertEqual(s["checks_state"], "failure")
        self.assertEqual(s["checks_counts"], {"failure": 1, "running": 1, "success": 1, "other": 0})
        # Running outranks passing, so the dot never reads greener than the list.
        self.assertEqual(
            gh.summarize_checks([{"name": "a", "bucket": "success"},
                                 {"name": "b", "bucket": "running"}])["checks_state"],
            "running",
        )
        # An unrecognized bucket is informational, never passing.
        self.assertEqual(
            gh.summarize_checks([{"name": "a", "bucket": "weird"}])["checks_counts"]["other"], 1
        )
        # No checks at all -> no dot.
        self.assertIsNone(gh.summarize_checks([])["checks_state"])

    def test_patches_the_row_in_the_list_cache(self):
        store.write_pulls_cache(
            "o", "r",
            [{"number": 7, "checks_state": "success", "checks_counts": {"success": 3}},
             {"number": 8, "checks_state": "success", "checks_counts": {"success": 1}}],
            root=self.tmp, state="open",
        )
        summary = gh.summarize_checks([{"name": "ci", "bucket": "failure"}])
        store.apply_pr_checks_to_list_cache("o", "r", 7, summary, root=self.tmp)
        rows = store.read_pulls_cache("o", "r", self.tmp, "open") or []
        by = {r["number"]: r for r in rows}
        self.assertEqual(by[7]["checks_state"], "failure")
        self.assertEqual(by[7]["checks_counts"]["failure"], 1)
        # Other rows are untouched.
        self.assertEqual(by[8]["checks_state"], "success")

    def test_patch_clears_a_stale_truncation_flag(self):
        # The detail read is fully paginated, so its tally is complete even for a
        # PR the GraphQL enrichment had to mark truncated. Leaving the old flag on
        # would hide those complete counts behind the aggregate dot after a reload.
        store.write_pulls_cache(
            "o", "r", [{"number": 7, "checks_truncated": True}], root=self.tmp, state="open",
        )
        summary = gh.summarize_checks([{"name": "ci", "bucket": "failure"}])
        store.apply_pr_checks_to_list_cache("o", "r", 7, summary, root=self.tmp)
        rows = store.read_pulls_cache("o", "r", self.tmp, "open") or []
        self.assertFalse(rows[0]["checks_truncated"])

    def test_non_object_cache_root_is_a_miss_not_a_crash(self):
        # Valid JSON with a non-object root would reach .get() and raise, failing
        # every request until the file was deleted by hand.
        for path in (store.pulls_cache_path("o", "r", self.tmp, "open"),
                     store.pr_detail_cache_path("o", "r", 7, self.tmp),
                     store.pr_ai_cache_path("o", "r", 7, self.tmp)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("[]", encoding="utf-8")
        self.assertIsNone(store.read_pulls_cache("o", "r", self.tmp, "open"))
        self.assertIsNone(store.read_pr_detail_cache("o", "r", 7, self.tmp))
        self.assertIsNone(store.read_pr_ai_cache("o", "r", 7, self.tmp, fingerprint="fp"))

    def test_drop_pulls_cache_removes_the_stale_entry(self):
        # Skipping the WRITE is not enough when enrichment fails on a forced
        # refresh: the previous non-expiring cache would still answer the next
        # plain request with older rows instead of retrying.
        store.write_pulls_cache("o", "r", [{"number": 7}], root=self.tmp, state="open")
        store.drop_pulls_cache("o", "r", "open", root=self.tmp)
        self.assertIsNone(store.read_pulls_cache("o", "r", self.tmp, "open"))
        # Idempotent — dropping a cache that is already gone is not an error.
        store.drop_pulls_cache("o", "r", "open", root=self.tmp)

    def test_patch_is_a_noop_without_a_matching_row(self):
        store.write_pulls_cache("o", "r", [{"number": 8}], root=self.tmp, state="open")
        summary = gh.summarize_checks([{"name": "ci", "bucket": "failure"}])
        # A PR that is not in this list (e.g. an open PR while viewing merged)
        # must not raise or invent a row.
        store.apply_pr_checks_to_list_cache("o", "r", 7, summary, root=self.tmp)
        rows = store.read_pulls_cache("o", "r", self.tmp, "open") or []
        self.assertEqual([r["number"] for r in rows], [8])


class TestCheckBucketTablesAgree(unittest.TestCase):
    """Every surface — REST check rows, the GraphQL per-context rows and the
    GraphQL aggregate rollup — classifies through the SINGLE `_check_bucket`
    table. That is what makes "the card and the sidebar agree about red"
    structural rather than a convention two parallel tables have to keep."""

    def test_one_table_serves_every_surface(self):
        # No second mapping is left to drift out of sync with the first.
        self.assertFalse(hasattr(gh, "_CONTEXT_BUCKET"))
        self.assertFalse(hasattr(gh, "_ROLLUP_BUCKET"))

    def test_graphql_vocabulary_classifies_case_insensitively(self):
        # GraphQL shouts (SUCCESS / IN_PROGRESS), REST whispers; one table must
        # answer both or the two surfaces disagree on identical data.
        for value, bucket in (
            ("SUCCESS", "success"), ("FAILURE", "failure"), ("ERROR", "failure"),
            ("TIMED_OUT", "failure"), ("STARTUP_FAILURE", "failure"),
            ("ACTION_REQUIRED", "failure"), ("PENDING", "running"),
            ("EXPECTED", "running"), ("QUEUED", "running"), ("IN_PROGRESS", "running"),
            ("SKIPPED", "other"), ("NEUTRAL", "other"),
        ):
            self.assertEqual(gh._check_bucket(None, value), bucket, value)
            self.assertEqual(
                gh._check_bucket(None, value.lower()), bucket,
                f"{value} classified differently in lower case",
            )

    def test_no_unfinished_state_maps_to_success(self):
        for value in ("PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED"):
            self.assertEqual(gh._check_bucket(None, value), "running", value)

    def test_unknown_state_never_passes(self):
        for value in ("", "SOMETHING_NEW", "weird"):
            self.assertEqual(gh._check_bucket(None, value), "other", value)


class TestPrTimelineAndChecksRobustness(unittest.TestCase):
    def test_pr_timeline_merges_inline_review_comments(self):
        timeline = [{"kind": "comment", "actor": "a", "created_at": "2024-01-02T00:00:00Z"}]
        inline = [{"kind": "review_comment", "actor": "b", "created_at": "2024-01-01T00:00:00Z",
                   "path": "src/x.py", "line": 12}]
        with mock.patch.object(gh, "list_issue_timeline", return_value=list(timeline)), \
                mock.patch.object(gh, "list_pr_review_comments", return_value=inline):
            out = gh.list_pr_timeline("o", "r", 7)
        # Merged AND re-sorted chronologically, so the pane reads like the thread.
        self.assertEqual([e["kind"] for e in out], ["review_comment", "comment"])

    def test_pr_timeline_tolerates_UNAVAILABLE_inline_endpoint(self):
        # 403/404 means this repo/token genuinely cannot serve inline comments —
        # skipping them is right, the rest of the conversation still stands.
        timeline = [{"kind": "comment", "actor": "a", "created_at": "2024-01-02T00:00:00Z"}]
        for message in ("gh api ... failed: HTTP 403", "gh api ... failed: HTTP 404"):
            with mock.patch.object(gh, "list_issue_timeline", return_value=list(timeline)), \
                    mock.patch.object(gh, "list_pr_review_comments",
                                      side_effect=gh.GhCliError(message)):
                out = gh.list_pr_timeline("o", "r", 7)
            self.assertEqual(len(out), 1)

    def test_pr_timeline_RAISES_on_a_transient_inline_failure(self):
        # A timeout is NOT "no inline comments": the caller CACHES this list, so
        # swallowing it would persist a partial conversation as if it were whole.
        timeline = [{"kind": "comment", "actor": "a", "created_at": "2024-01-02T00:00:00Z"}]
        with mock.patch.object(gh, "list_issue_timeline", return_value=list(timeline)), \
                mock.patch.object(gh, "list_pr_review_comments",
                                  side_effect=gh.GhCliError("timed out after 60s")):
            with self.assertRaises(gh.GhCliError):
                gh.list_pr_timeline("o", "r", 7)

    def test_checks_raise_when_BOTH_surfaces_fail(self):
        # An empty list would be cached and written over a known failure as
        # "no checks" — a silent lie. A surface that is merely ABSENT (403/404) is
        # still tolerated.
        with mock.patch.object(gh, "_run_gh_api", side_effect=gh.GhCliError("HTTP 404")):
            with self.assertRaises(gh.GhCliError):
                gh.list_pr_checks("o", "r", "a" * 40)

        calls = {"n": 0}

        def _one_fails(path, *a, **kw):
            calls["n"] += 1
            if "check-runs" in path:
                return [{"name": "ci", "source": "gha", "status": "completed",
                         "conclusion": "success"}]
            raise gh.GhCliError("HTTP 404: no status api")

        with mock.patch.object(gh, "_run_gh_api", side_effect=_one_fails):
            out = gh.list_pr_checks("o", "r", "a" * 40)
        self.assertEqual([c["name"] for c in out], ["ci"])

    def test_checks_raise_when_one_surface_fails_and_the_other_is_empty(self):
        # "Checks API failed, Status API returned nothing" is NOT "no checks": the
        # detail route caches this and writes it through to the list card, which
        # would hide a failure the user had already seen.
        def _empty_other(path, *a, **kw):
            if "check-runs" in path:
                raise gh.GhCliError("HTTP 502")
            return []

        with mock.patch.object(gh, "_run_gh_api", side_effect=_empty_other):
            with self.assertRaises(gh.GhCliError):
                gh.list_pr_checks("o", "r", "a" * 40)

    def test_checks_raise_on_a_TRANSIENT_surface_failure_even_with_rows(self):
        # A timeout on one surface while the other returns rows is a PARTIAL
        # answer, and it gets cached: one passing check-run would then be shown as
        # "passing" while a required commit status was actually failing.
        def _transient(path, *a, **kw):
            if "check-runs" in path:
                return [{"name": "ci", "source": "gha", "status": "completed",
                         "conclusion": "success"}]
            raise gh.GhCliError("timed out after 60s")

        with mock.patch.object(gh, "_run_gh_api", side_effect=_transient):
            with self.assertRaises(gh.GhCliError):
                gh.list_pr_checks("o", "r", "a" * 40)

    def test_mergeability_retry_failure_keeps_the_first_answer(self):
        # The retry only ever IMPROVES an answer we already have; letting its
        # failure propagate would turn a usable detail into a 502 and render no PR.
        first = {"number": 7, "mergeable": None, "mergeable_state": "unknown"}
        calls = {"n": 0}

        def _once(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return dict(first)
            raise gh.GhCliError("timed out after 60s")

        with mock.patch.object(gh, "_fetch_pr_detail_once", side_effect=_once), \
                mock.patch.object(gh.time, "sleep"):
            out = gh.get_pr_detail("o", "r", 7)
        self.assertEqual(out["mergeable_state"], "unknown")
        self.assertEqual(calls["n"], 2)

    def test_checks_keep_same_name_rows_from_DIFFERENT_publishers(self):
        # Two apps may publish a check with the same display name; collapsing them
        # by name alone let one app's success hide the other's failure.
        def _both(path, *a, **kw):
            if "check-runs" in path:
                return [{"name": "build", "source": "github-actions", "status": "completed",
                         "conclusion": "success", "started_at": "2", "completed_at": "2"}]
            return [{"name": "build", "source": "status", "status": "completed",
                     "conclusion": "failure", "started_at": "1", "completed_at": "1"}]

        with mock.patch.object(gh, "_run_gh_api", side_effect=_both):
            out = gh.list_pr_checks("o", "r", "a" * 40)
        self.assertEqual(len(out), 2)
        self.assertEqual({c["bucket"] for c in out}, {"success", "failure"})

    def test_a_401_is_NOT_treated_as_an_absent_surface(self):
        # Expired credentials mean every other call is about to fail too; skipping
        # the surface would cache half a conversation (or a check list missing
        # whichever surface the expiry hit) as if it were complete.
        self.assertFalse(gh._is_absent_or_forbidden(gh.GhCliError("gh api ... HTTP 401")))
        self.assertTrue(gh._is_absent_or_forbidden(gh.GhCliError("gh api ... HTTP 403")))
        self.assertTrue(gh._is_absent_or_forbidden(gh.GhCliError("gh api ... HTTP 404")))
        self.assertFalse(gh._is_absent_or_forbidden(gh.GhCliError("timed out")))

    def test_dedupe_prefers_the_run_that_started_later(self):
        # An older run that FINISHED must not outrank a newer run still going.
        rows: list[dict] = [
            {"name": "ci", "conclusion": "success",
             "started_at": "2026-07-26T10:05:00Z", "completed_at": "2026-07-26T10:10:00Z"},
            {"name": "ci", "conclusion": None, "status": "in_progress",
             "started_at": "2026-07-26T10:15:00Z", "completed_at": None},
        ]
        out = gh._dedupe_checks(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "in_progress")

    def test_enrichment_tops_up_rows_beyond_the_graphql_window(self):
        # The state-scoped query returns 100 rows; the REST list paginates all of
        # them, so the remainder must be fetched by number rather than reported as
        # a confident "0 additions, no checks".
        pulls = [{"number": n} for n in (1, 2, 3)]
        with mock.patch.object(
            gh, "fetch_pr_summaries",
            return_value={1: {"additions": 5, "deletions": 0, "changed_files": 1,
                              "checks_state": "success", "checks_counts": {}}},
        ), mock.patch.object(gh, "fetch_pr_summaries_by_number") as by_number, \
                mock.patch.object(gh, "fetch_pr_readiness", return_value={}):
            by_number.return_value = {
                2: {"additions": 7, "deletions": 1, "changed_files": 2,
                    "checks_state": "failure", "checks_counts": {}},
            }
            out = gh.enrich_pulls("o", "r", pulls, "open")
        self.assertEqual(sorted(by_number.call_args.args[2]), [2, 3])
        by = {p["number"]: p for p in out}
        self.assertEqual(by[2]["checks_state"], "failure")
        # Still genuinely unknown -> null, and the list therefore is not cached.
        self.assertIsNone(by[3]["additions"])
        self.assertFalse(gh.enrichment_complete(out))


if __name__ == "__main__":
    unittest.main()
