"""Tests for the single-issue detail feature.

Covers the two pieces of new backend logic that carry real behaviour:
  * github_client timeline normalization — raw GitHub events → the compact,
    uniform shape the detail pane renders, with noise dropped, reactions
    coerced, and the result sorted oldest→newest;
  * store issue-detail cache — one file per issue, round-trips detail+timeline,
    returns None when absent.

Both are pure/local (no ``gh`` calls): ``list_issue_timeline`` is exercised by
monkeypatching ``_run_gh_api`` so the sort/drop pipeline is tested without a
subprocess.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import store


class TestNormalizeReactions(unittest.TestCase):
    def test_none_and_zero_collapse_to_none(self):
        self.assertIsNone(gh._norm_reactions(None))
        self.assertIsNone(gh._norm_reactions({"total_count": 0}))

    def test_shaped_when_present(self):
        r = gh._norm_reactions({"total_count": 3, "+1": 2, "-1": 0, "heart": 1,
                                "laugh": 0, "hooray": 0, "confused": 0, "rocket": 0, "eyes": 0})
        assert r is not None
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["plus1"], 2)
        self.assertEqual(r["heart"], 1)
        self.assertEqual(r["minus1"], 0)


class TestNormalizeTimelineEvent(unittest.TestCase):
    def test_comment_carries_body_association_and_reactions(self):
        ev = gh._normalize_timeline_event({
            "event": "commented", "user": {"login": "alice"},
            "created_at": "2024-01-02T00:00:00Z", "body": "hi", "author_association": "MEMBER",
            "reactions": {"total_count": 1, "+1": 1, "-1": 0, "laugh": 0, "hooray": 0,
                          "confused": 0, "heart": 0, "rocket": 0, "eyes": 0},
        })
        assert ev is not None
        self.assertEqual(ev["kind"], "comment")
        self.assertEqual(ev["actor"], "alice")
        self.assertEqual(ev["body"], "hi")
        self.assertEqual(ev["author_association"], "MEMBER")
        self.assertEqual(ev["reactions"]["plus1"], 1)

    def test_labeled_actor_and_label(self):
        ev = gh._normalize_timeline_event({
            "event": "labeled", "actor": {"login": "bob"},
            "created_at": "2024-01-01T00:00:00Z", "label": {"name": "bug", "color": "ee0000"},
        })
        assert ev is not None
        self.assertEqual(ev["kind"], "labeled")
        self.assertEqual(ev["actor"], "bob")
        self.assertEqual(ev["label"], {"name": "bug", "color": "ee0000"})

    def test_assigned_reports_assignee(self):
        ev = gh._normalize_timeline_event({
            "event": "assigned", "actor": {"login": "bob"},
            "created_at": "2024-01-01T00:00:00Z", "assignee": {"login": "carol"},
        })
        assert ev is not None
        self.assertEqual(ev["kind"], "assigned")
        self.assertEqual(ev["assignee"], "carol")

    def test_closed_keeps_state_reason(self):
        ev = gh._normalize_timeline_event({
            "event": "closed", "actor": {"login": "bob"},
            "created_at": "2024-01-04T00:00:00Z", "state_reason": "not_planned",
        })
        assert ev is not None
        self.assertEqual(ev["kind"], "closed")
        self.assertEqual(ev["state_reason"], "not_planned")

    def test_cross_referenced_extracts_source_and_pr_flag(self):
        ev = gh._normalize_timeline_event({
            "event": "cross-referenced", "actor": {"login": "cara"},
            "created_at": "2024-01-03T00:00:00Z",
            "source": {"issue": {"number": 42, "title": "other", "html_url": "https://x/42",
                                 "state": "open", "pull_request": {"url": "https://x/pull/42"}}},
        })
        assert ev is not None
        self.assertEqual(ev["kind"], "cross-referenced")
        self.assertEqual(ev["source"]["number"], 42)
        self.assertTrue(ev["source"]["is_pr"])

    def test_noise_events_dropped(self):
        for etype in ("subscribed", "mentioned", "review_requested", "head_ref_deleted"):
            self.assertIsNone(gh._normalize_timeline_event({"event": etype, "actor": {"login": "x"}}))


class TestListIssueTimeline(unittest.TestCase):
    def test_sorts_chronologically_and_drops_noise(self):
        raw = [
            {"event": "subscribed", "actor": {"login": "bot"}, "created_at": "2024-01-05T00:00:00Z"},
            {"event": "commented", "user": {"login": "alice"}, "created_at": "2024-01-02T00:00:00Z", "body": "hi"},
            {"event": "labeled", "actor": {"login": "bob"}, "created_at": "2024-01-01T00:00:00Z",
             "label": {"name": "bug", "color": "ee0000"}},
            {"event": "closed", "actor": {"login": "bob"}, "created_at": "2024-01-04T00:00:00Z"},
        ]
        with mock.patch.object(gh, "_run_gh_api", return_value=raw) as m:
            out = gh.list_issue_timeline("o", "r", 7)
        # subscribed dropped; remaining sorted oldest→newest by created_at.
        self.assertEqual([e["kind"] for e in out], ["labeled", "comment", "closed"])
        # number is coerced into the path segment (injection-safe).
        called_path = m.call_args.args[0]
        self.assertIn("/issues/7/timeline", called_path)


class TestIssueDetailCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip(self):
        detail = {"number": 7, "title": "t", "body": "b"}
        timeline = [{"kind": "comment", "actor": "alice"}]
        store.write_issue_detail_cache("o", "r", 7, detail, timeline, root=self.tmp)
        got = store.read_issue_detail_cache("o", "r", 7, self.tmp)
        assert got is not None
        self.assertEqual(got["detail"], detail)
        self.assertEqual(got["timeline"], timeline)

    def test_absent_returns_none(self):
        self.assertIsNone(store.read_issue_detail_cache("o", "r", 999, self.tmp))


if __name__ == "__main__":
    unittest.main()
