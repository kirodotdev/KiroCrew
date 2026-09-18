"""Incremental refresh (since + primary-key diff), resumable checkpoint, and the
parked live-fetch path for the GitHub structured connector.

Pure-function tests: no database, no network. They exercise the incremental
logic and the checkpoint that make a refresh idempotent and resumable, and they
assert the live-fetch path REFUSES rather than returning a mocked dataset — a
mock is not a live read.
"""

import asyncio
import unittest

from kiro_crew.knowledge.connectors.github_structured import (
    Checkpoint,
    GithubStructuredConnector,
    commit_from_payload,
    diff_rows,
    issue_or_pull_from_payload,
    read_checkpoint,
    write_checkpoint,
)

REPO = "kirodotdev/KiroCrew"
SRC = "src-github-1"
NOW = "2026-09-16T00:00:00Z"


def _issue(number, updated_at, title="t", *, source_id=SRC, repo=REPO):
    return issue_or_pull_from_payload(
        repo,
        {
            "number": number,
            "title": title,
            "state": "open",
            "user": {"login": "octocat"},
            "updated_at": updated_at,
            "created_at": "2026-09-01T00:00:00Z",
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "url": f"https://api.github.com/repos/{repo}/issues/{number}",
        },
        source_id=source_id,
        fetched_at=NOW,
    )


def _commit(sha, date, *, source_id=SRC, repo=REPO):
    return commit_from_payload(
        repo,
        {
            "sha": sha,
            "commit": {"message": "m",
                       "committer": {"name": "c", "date": date}},
            "html_url": f"https://github.com/{repo}/commit/{sha}",
            "url": f"https://api.github.com/repos/{repo}/commits/{sha}",
        },
        source_id=source_id,
        fetched_at=NOW,
    )


class TestIncrementalDiff(unittest.TestCase):
    def test_fetched_rows_become_upserts(self):
        rows = (_issue(1, "2026-09-10T00:00:00Z"), _issue(2, "2026-09-11T00:00:00Z"))
        plan = diff_rows(rows, frozenset(), prior_since=None)
        self.assertEqual(len(plan.upserts), 2)
        self.assertEqual(plan.disappeared, ())

    def test_watermark_advances_to_max_updated_at(self):
        rows = (_issue(1, "2026-09-10T00:00:00Z"), _issue(2, "2026-09-12T00:00:00Z"))
        plan = diff_rows(rows, frozenset(), prior_since=None)
        self.assertEqual(plan.next_since, "2026-09-12T00:00:00Z")

    def test_clock_skew_never_moves_watermark_backwards(self):
        # NEGATIVE: a re-listed / skewed record with an OLDER updated_at than the
        # prior watermark must not reopen an already-covered window.
        rows = (_issue(1, "2026-09-05T00:00:00Z"),)  # older than prior_since
        plan = diff_rows(rows, frozenset(), prior_since="2026-09-11T00:00:00Z")
        self.assertEqual(plan.next_since, "2026-09-11T00:00:00Z")

    def test_empty_window_keeps_prior_watermark(self):
        plan = diff_rows((), frozenset({"github_issue:x/1"}),
                         prior_since="2026-09-11T00:00:00Z")
        self.assertEqual(plan.next_since, "2026-09-11T00:00:00Z")
        self.assertEqual(plan.upserts, ())

    def test_overlapping_window_reissue_is_idempotent(self):
        # A since window that overlaps the previous one re-lists a boundary
        # record. Diffing by key means it is one upsert, not a duplicate row.
        stored = frozenset({_issue(1, "2026-09-10T00:00:00Z").primary_key})
        rows = (_issue(1, "2026-09-10T00:00:00Z"),)  # already stored, re-listed
        plan = diff_rows(rows, stored, prior_since="2026-09-09T00:00:00Z")
        self.assertEqual(len(plan.upserts), 1)  # upsert, not a second copy
        self.assertEqual(plan.disappeared, ())

    def test_primary_key_collision_within_window_keeps_last(self):
        # NEGATIVE: two fetched rows sharing a key (a straddling record re-listed
        # across a page boundary) collapse to one, last-writer-wins.
        rows = (_issue(1, "2026-09-10T00:00:00Z", title="first"),
                _issue(1, "2026-09-10T00:00:00Z", title="second"))
        plan = diff_rows(rows, frozenset(), prior_since=None)
        self.assertEqual(len(plan.upserts), 1)
        self.assertEqual(plan.upserts[0].title, "second")

    def test_page_boundary_union_of_two_pages(self):
        # Two pages concatenated (as a paginated walk would hand them) diff as
        # one set; distinct keys all survive, a straddling repeat collapses.
        page1 = (_issue(1, "2026-09-10T00:00:00Z"), _issue(2, "2026-09-10T00:00:00Z"))
        page2 = (_issue(2, "2026-09-10T00:00:00Z"), _issue(3, "2026-09-11T00:00:00Z"))
        plan = diff_rows(page1 + page2, frozenset(), prior_since=None)
        self.assertEqual(len(plan.upserts), 3)  # 1,2,3 — the repeated 2 collapses

    def test_windowed_refresh_never_reports_disappeared(self):
        # A since window carries only CHANGED records, so a stored key missing
        # from a WINDOW diff is NOT reported as disappeared (it would be nearly
        # every key). disappeared stays empty on the incremental path.
        key1 = _issue(1, "2026-09-01T00:00:00Z").primary_key
        key2 = _issue(2, "2026-09-01T00:00:00Z").primary_key
        stored = frozenset({key1, key2})
        rows = (_issue(1, "2026-09-12T00:00:00Z"),)  # only #1 changed
        plan = diff_rows(rows, stored, prior_since="2026-09-11T00:00:00Z")
        self.assertEqual(plan.disappeared, ())

    def test_full_listing_reports_genuinely_absent_keys(self):
        # A FULL listing (no since) proves absence: a stored key not in the
        # listing is genuinely gone and IS reported for the caller to retire.
        key1 = _issue(1, "2026-09-01T00:00:00Z").primary_key
        key2 = _issue(2, "2026-09-01T00:00:00Z").primary_key
        stored = frozenset({key1, key2})
        rows = (_issue(1, "2026-09-12T00:00:00Z"),)  # listing omits #2
        plan = diff_rows(rows, stored, prior_since=None, full_listing=True)
        self.assertEqual(plan.disappeared, (key2,))

    def test_commit_watermark_uses_committed_date(self):
        rows = (_commit("a" * 40, "2026-09-05T00:00:00Z"),
                _commit("b" * 40, "2026-09-06T00:00:00Z"))
        plan = diff_rows(rows, frozenset(), prior_since=None)
        self.assertEqual(plan.next_since, "2026-09-06T00:00:00Z")

    def test_same_number_two_repos_are_two_upserts(self):
        # NEGATIVE (key domain): issue #1 in two repos must NOT collapse in the
        # diff — the full-domain key keeps them distinct, so both upsert.
        rows = (_issue(1, "2026-09-10T00:00:00Z", repo="octo/alpha"),
                _issue(1, "2026-09-10T00:00:00Z", repo="octo/beta"))
        plan = diff_rows(rows, frozenset(), prior_since=None)
        self.assertEqual(len(plan.upserts), 2)


class TestCheckpoint(unittest.TestCase):
    def test_round_trip_through_properties_blob(self):
        cp = Checkpoint(since="2026-09-11T00:00:00Z", entity_index=1,
                        page_cursor="page2", in_progress=True)
        props = write_checkpoint({"other": "kept"}, cp)
        self.assertEqual(props["other"], "kept")  # existing props preserved
        back = read_checkpoint({"properties": props})
        self.assertEqual(back.since, "2026-09-11T00:00:00Z")
        self.assertEqual(back.entity_index, 1)
        self.assertEqual(back.page_cursor, "page2")
        self.assertTrue(back.in_progress)

    def test_read_from_json_string_properties(self):
        # The raw sources row carries properties as a JSON STRING; read_checkpoint
        # accepts that shape too.
        import json

        props = write_checkpoint({}, Checkpoint(since="2026-09-01T00:00:00Z"))
        back = read_checkpoint({"properties": json.dumps(props)})
        self.assertEqual(back.since, "2026-09-01T00:00:00Z")

    def test_absent_checkpoint_is_a_fresh_one(self):
        cp = read_checkpoint({"properties": {}})
        self.assertIsNone(cp.since)
        self.assertEqual(cp.entity_index, 0)
        self.assertFalse(cp.in_progress)

    def test_resume_position_survives_interruption(self):
        # Simulate: a refresh completed the issues walk (index 0) and paused mid
        # commits (index 1, page cursor). A resume reads exactly that position.
        paused = Checkpoint(since=None, entity_index=1, page_cursor="cursor-7",
                            in_progress=True)
        props = write_checkpoint({}, paused)
        resumed = read_checkpoint({"properties": props})
        self.assertEqual(resumed.entity_index, 1)
        self.assertEqual(resumed.page_cursor, "cursor-7")
        self.assertTrue(resumed.in_progress)

    def test_malformed_entity_index_clamps_to_start(self):
        # NEGATIVE: a persisted index that does not name a valid entity (a
        # shorter order, or garbage) resumes from the start, never out of range.
        self.assertEqual(read_checkpoint(
            {"properties": {"github_structured_checkpoint": {"entity_index": 99}}}
        ).entity_index, 0)
        self.assertEqual(read_checkpoint(
            {"properties": {"github_structured_checkpoint": {"entity_index": "x"}}}
        ).entity_index, 0)

    def test_corrupt_properties_string_degrades_to_fresh(self):
        cp = read_checkpoint({"properties": "not json{"})
        self.assertIsNone(cp.since)


class TestParkedLivePaths(unittest.TestCase):
    def setUp(self):
        self.c = GithubStructuredConnector()

    def test_fetch_refuses_without_transport(self):
        # UNVERIFIED path: live fetch needs the transport executor and the
        # vendored pagination. It MUST refuse, not return a mocked dataset.
        with self.assertRaises(NotImplementedError):
            asyncio.run(self.c.fetch({"repo_full_name": REPO}))

    def test_detect_changes_refuses_without_transport(self):
        with self.assertRaises(NotImplementedError):
            asyncio.run(self.c.detect_changes({"repo_full_name": REPO}))


if __name__ == "__main__":
    unittest.main()
