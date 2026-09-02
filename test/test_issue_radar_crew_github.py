"""Tests for the GitHub calls the Issue Radar CREW claim protocol needs.

A crew's claim on an issue is a COMMENT, not a label and not a local record, and
everything here exists to make that comment usable as a ledger:

  * ``update_issue_comment`` — the crew rewrites ONE comment as work progresses
    (an edit sends no GitHub notification, so a 20-minute heartbeat is not spam);
  * ``create_pull_request`` — REST rather than ``gh pr create``, so a
    model-authored title/body never lands on an argv;
  * ``_normalize_timeline_event`` — must carry a comment's ``id`` (address it to
    PATCH it) and ``updated_at`` (``created_at`` on an EDITED comment is still the
    original post time, so a live claim would read days stale);
  * ``find_crew_claim`` — reads the machine marker back out, with the ordering the
    collision rule depends on.

Every test patches the ``gh`` layer, so no subprocess is spawned (and the
POSIX-only ``_gh_bin`` guard is never reached — these run on Windows too).
"""
import json
import unittest
from unittest import mock

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh

MARKER = (
    "<!-- kirocrew-crew id=c_7f3a phase=implementing pr=2271 "
    "updated=2026-08-08T20:44:12Z -->"
)


def _proc(stdout: str = "", *, returncode: int = 0, stderr: str = ""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


def _comment_row(comment_id, body, *, actor="kiro-crew", created="2026-08-08T18:02:00Z"):
    """One row in the shape ``_normalize_timeline_event`` emits for a comment."""
    return {
        "kind": "comment", "id": comment_id, "actor": actor,
        "created_at": created, "updated_at": created, "body": body,
        "author_association": "MEMBER", "reactions": None,
    }


class UpdateIssueCommentTest(unittest.TestCase):
    """The PATCH: flat comment path, body on stdin, edit-time reported back."""

    def test_patches_the_flat_comment_path_with_the_body_on_stdin(self):
        payload = {
            "id": 9001, "html_url": "https://github.com/o/r/issues/5#issuecomment-9001",
            "created_at": "2026-08-08T18:02:00Z", "updated_at": "2026-08-08T20:44:12Z",
        }
        with mock.patch.object(gh, "_gh_run", return_value=_proc(json.dumps(payload))) as run:
            out = gh.update_issue_comment("o", "r", 9001, f"👻 on this\n\n{MARKER}")

        argv = run.call_args[0][0]
        self.assertEqual(argv[:4], ["gh", "api", "--method", "PATCH"])
        # Comment endpoints are repo-scoped and FLAT — no /issues/{n}/ segment.
        self.assertIn("repos/o/r/issues/comments/9001", argv)
        # The body is model-authored prose: it must ride on stdin, never argv.
        self.assertIn("--input", argv)
        self.assertIn("-", argv)
        sent = json.loads(run.call_args.kwargs["input_text"])
        self.assertEqual(sent, {"body": f"👻 on this\n\n{MARKER}"})
        self.assertFalse(any(MARKER in str(a) for a in argv))
        # updated_at, not created_at: only the former can confirm the edit landed.
        self.assertEqual(out["updated_at"], "2026-08-08T20:44:12Z")
        self.assertEqual(out["id"], 9001)
        self.assertEqual(out["url"], payload["html_url"])

    def test_comment_id_is_coerced_and_cannot_inject_path_segments(self):
        with mock.patch.object(gh, "_gh_run", return_value=_proc("{}")) as run:
            with self.assertRaises(ValueError):
                gh.update_issue_comment("o", "r", "9001/../../secret", "x")  # type: ignore[arg-type]
        run.assert_not_called()

    def test_an_empty_body_is_refused_before_any_call(self):
        # An empty edge is not a no-op — it would blank the claim ledger in place.
        with mock.patch.object(gh, "_gh_run") as run:
            for body in ("", "   ", "\n"):
                with self.assertRaises(gh.GhCliError):
                    gh.update_issue_comment("o", "r", 9001, body)
        run.assert_not_called()

    def test_a_403_becomes_a_permission_error(self):
        # Inherited from _run_gh_write, which is the reason this goes through it.
        proc = _proc(returncode=1, stderr="gh: HTTP 403: Resource not accessible")
        with mock.patch.object(gh, "_gh_run", return_value=proc):
            with self.assertRaises(gh.GhPermissionError):
                gh.update_issue_comment("o", "r", 9001, "body")

    def test_an_unparseable_response_still_reports_the_comment_it_edited(self):
        with mock.patch.object(gh, "_gh_run", return_value=_proc("")):
            out = gh.update_issue_comment("o", "r", 9001, "body")
        self.assertEqual(out, {"id": 9001, "url": None, "updated_at": None})


class CreatePullRequestTest(unittest.TestCase):
    """The POST: REST payload, prose off argv, draft passthrough."""

    RESPONSE = {
        "number": 2271, "html_url": "https://github.com/o/r/pull/2271",
        "draft": False, "state": "open",
    }

    def test_posts_the_pulls_endpoint_with_every_field_on_stdin(self):
        title = "fix(radar): guard the claim reader against a naive timestamp"
        body = "## Problem\nA malformed stamp crashed the reader.\n"
        with mock.patch.object(
            gh, "_gh_run", return_value=_proc(json.dumps(self.RESPONSE))
        ) as run:
            out = gh.create_pull_request("o", "r", "crew/andromeda/issue-5", "main", title, body)

        argv = run.call_args[0][0]
        self.assertEqual(argv[:4], ["gh", "api", "--method", "POST"])
        self.assertIn("repos/o/r/pulls", argv)
        # Never `gh pr create`: a model-authored title must not become an argv word.
        self.assertNotIn("pr", argv)
        self.assertFalse(any(title in str(a) for a in argv))
        self.assertFalse(any(body in str(a) for a in argv))
        self.assertEqual(json.loads(run.call_args.kwargs["input_text"]), {
            "title": title, "head": "crew/andromeda/issue-5", "base": "main",
            "body": body, "draft": False,
        })
        self.assertEqual(out["number"], 2271)
        self.assertEqual(out["html_url"], "https://github.com/o/r/pull/2271")
        # Same value under the module's own spelling, so neither reader gets None.
        self.assertEqual(out["url"], out["html_url"])
        self.assertEqual(out["state"], "open")

    def test_draft_is_passed_through(self):
        response = {**self.RESPONSE, "draft": True}
        with mock.patch.object(gh, "_gh_run", return_value=_proc(json.dumps(response))) as run:
            out = gh.create_pull_request("o", "r", "h", "main", "t", "b", draft=True)
        self.assertTrue(json.loads(run.call_args.kwargs["input_text"])["draft"])
        self.assertTrue(out["draft"])

    def test_a_cross_fork_head_is_sent_verbatim(self):
        # `owner:branch` is a legal head and carries no injection surface — it is a
        # JSON value, never a path segment or an argv word.
        with mock.patch.object(
            gh, "_gh_run", return_value=_proc(json.dumps(self.RESPONSE))
        ) as run:
            gh.create_pull_request("o", "r", "forker:crew/andromeda-5", "main", "t", "b")
        self.assertEqual(json.loads(run.call_args.kwargs["input_text"])["head"],
                         "forker:crew/andromeda-5")

    def test_an_empty_title_or_ref_is_refused_before_any_call(self):
        with mock.patch.object(gh, "_gh_run") as run:
            for args in (("o", "r", "h", "main", "  "), ("o", "r", "", "main", "t"),
                         ("o", "r", "h", "", "t")):
                with self.assertRaises(gh.GhCliError):
                    gh.create_pull_request(*args, "body")
        run.assert_not_called()

    def test_a_response_with_no_pr_raises_instead_of_guessing_a_number(self):
        # A fabricated number would make every later call address the wrong PR.
        with mock.patch.object(gh, "_gh_run", return_value=_proc("")):
            with self.assertRaises(gh.GhCliError):
                gh.create_pull_request("o", "r", "h", "main", "t", "b")

    def test_a_403_becomes_a_permission_error(self):
        proc = _proc(returncode=1, stderr="gh: HTTP 403: must have push access")
        with mock.patch.object(gh, "_gh_run", return_value=proc):
            with self.assertRaises(gh.GhPermissionError):
                gh.create_pull_request("o", "r", "h", "main", "t", "b")


class TimelineCommentFieldsTest(unittest.TestCase):
    """``id`` and ``updated_at`` must survive normalization — both load-bearing."""

    RAW = {
        "event": "commented", "id": 9001, "user": {"login": "kiro-crew"},
        "created_at": "2026-08-08T18:02:00Z", "updated_at": "2026-08-08T20:44:12Z",
        "body": f"progress\n\n{MARKER}", "author_association": "MEMBER",
    }

    def test_a_comment_carries_its_id_and_edit_time(self):
        row = gh._normalize_timeline_event(self.RAW)
        self.assertEqual(row["kind"], "comment")
        # Without id, a crew cannot PATCH its own claim comment.
        self.assertEqual(row["id"], 9001)
        # created_at on an EDITED comment is still the ORIGINAL post time, so only
        # updated_at can show a claim is alive.
        self.assertEqual(row["created_at"], "2026-08-08T18:02:00Z")
        self.assertEqual(row["updated_at"], "2026-08-08T20:44:12Z")

    def test_the_fields_survive_the_full_timeline_read(self):
        # Proves the whole path, not just the normalizer: the raw events reach it
        # through _run_gh_api's JSONL parse.
        with mock.patch.object(gh, "_gh_run", return_value=_proc(json.dumps(self.RAW))):
            rows = gh.list_issue_timeline("o", "r", 5)
        self.assertEqual([(r["id"], r["updated_at"]) for r in rows],
                         [(9001, "2026-08-08T20:44:12Z")])

    def test_a_never_edited_comment_reports_its_own_stamps(self):
        raw = {**self.RAW, "updated_at": "2026-08-08T18:02:00Z"}
        row = gh._normalize_timeline_event(raw)
        self.assertEqual(row["updated_at"], row["created_at"])

    def test_missing_stamps_are_none_not_absent(self):
        # A key that vanishes forces every reader to guard; None is a readable
        # "unknown" that a freshness check treats as not-fresh.
        row = gh._normalize_timeline_event({"event": "commented", "body": "hi"})
        self.assertIsNone(row["id"])
        self.assertIsNone(row["updated_at"])

    def test_non_comment_events_are_unchanged(self):
        row = gh._normalize_timeline_event(
            {"event": "labeled", "actor": {"login": "a"}, "label": {"name": "bug"}}
        )
        self.assertNotIn("id", row)
        self.assertNotIn("updated_at", row)


class FindCrewClaimTest(unittest.TestCase):
    """The marker parser + the ordering the collision rule depends on."""

    def test_parses_every_field_of_a_well_formed_marker(self):
        rows = [_comment_row(9001, f"👻 **Andromeda** is on this\n\n{MARKER}")]
        self.assertEqual(gh.find_crew_claim(rows), [{
            "comment_id": 9001, "crew_id": "c_7f3a", "phase": "implementing",
            "pr": 2271, "updated": "2026-08-08T20:44:12Z",
            "actor": "kiro-crew", "created_at": "2026-08-08T18:02:00Z",
        }])

    def test_a_row_with_no_marker_is_skipped(self):
        rows = [
            _comment_row(1, "any thoughts on this?"),
            _comment_row(2, "I hit this too"),
        ]
        self.assertEqual(gh.find_crew_claim(rows), [])

    def test_the_brief_sentinel_is_not_a_claim(self):
        # `kirocrew-crew-brief` shares the marker's prefix; matching it would make
        # every session that echoes the brief look like a claim.
        rows = [_comment_row(1, "<!-- kirocrew-crew-brief v1 -->")]
        self.assertEqual(gh.find_crew_claim(rows), [])

    def test_unknown_keys_are_ignored(self):
        marker = (
            "<!-- kirocrew-crew id=c_7f3a phase=claimed pr=7 "
            "updated=2026-08-08T20:44:12Z lease=99 nextField=whatever -->"
        )
        claim = gh.find_crew_claim([_comment_row(5, marker)])[0]
        self.assertEqual(claim["crew_id"], "c_7f3a")
        self.assertEqual(claim["phase"], "claimed")
        self.assertNotIn("lease", claim)
        self.assertNotIn("nextField", claim)

    def test_absent_optional_fields_read_as_empty_not_missing(self):
        claim = gh.find_crew_claim([_comment_row(5, "<!-- kirocrew-crew id=c_1 -->")])[0]
        self.assertEqual(claim["crew_id"], "c_1")
        self.assertEqual(claim["phase"], "")
        self.assertIsNone(claim["pr"])
        self.assertIsNone(claim["updated"])

    def test_a_marker_with_no_id_is_still_reported_as_a_claim(self):
        # It names nobody, so it can never MATCH a crew id — but dropping it is how
        # two crews end up on one issue.
        claim = gh.find_crew_claim([_comment_row(5, "<!-- kirocrew-crew phase=claimed -->")])[0]
        self.assertEqual(claim["crew_id"], "")
        self.assertEqual(gh.find_crew_claim([_comment_row(5, "<!-- kirocrew-crew phase=x -->")],
                                            crew_id="c_7f3a"), [])

    def test_a_non_numeric_pr_is_none_not_a_crash(self):
        marker = "<!-- kirocrew-crew id=c_1 pr=none updated=2026-08-08T20:44:12Z -->"
        self.assertIsNone(gh.find_crew_claim([_comment_row(5, marker)])[0]["pr"])

    # ── the timestamp rule: only ISO-8601 with a trailing Z parses ────────────

    def test_a_valid_stamp_with_fractional_seconds_is_accepted(self):
        marker = "<!-- kirocrew-crew id=c_1 updated=2026-08-08T20:44:12.501Z -->"
        self.assertEqual(gh.find_crew_claim([_comment_row(5, marker)])[0]["updated"],
                         "2026-08-08T20:44:12.501Z")

    def test_a_malformed_stamp_reads_as_unparseable_not_fresh(self):
        # Each of these is accepted by datetime.fromisoformat (a naive datetime, or
        # an offset-aware one), which is exactly why the check is stricter than it:
        # a naive value compared against an aware `now` raises TypeError, so a bad
        # stamp would CRASH the reader rather than read as stale.
        for bad in (
            "2026-08-08T20:44:12+00:00",   # offset, no Z
            "2026-08-08 20:44:12Z",        # space separator
            "2026-08-08T20:44:12",         # no timezone at all
            "2026-08-08",                  # date only
            "yesterday",
            "1786000000",                  # epoch seconds
        ):
            marker = f"<!-- kirocrew-crew id=c_1 updated={bad} -->"
            claim = gh.find_crew_claim([_comment_row(5, marker)])[0]
            self.assertIsNone(claim["updated"], bad)
            # The claim itself still stands — only its freshness is unknown.
            self.assertEqual(claim["crew_id"], "c_1")

    # ── ordering: the collision rule is "smallest comment id wins" ────────────

    def test_claims_are_ordered_by_comment_id_ascending(self):
        rows = [
            _comment_row(9003, "<!-- kirocrew-crew id=c_c -->", created="2026-08-08T20:00:00Z"),
            _comment_row(9001, "<!-- kirocrew-crew id=c_a -->", created="2026-08-08T18:00:00Z"),
            _comment_row(9002, "<!-- kirocrew-crew id=c_b -->", created="2026-08-08T19:00:00Z"),
        ]
        # The winner of a collision is [0], so the order IS the protocol.
        self.assertEqual([c["comment_id"] for c in gh.find_crew_claim(rows)],
                         [9001, 9002, 9003])
        self.assertEqual(gh.find_crew_claim(rows)[0]["crew_id"], "c_a")

    def test_an_unknown_comment_id_sorts_last_so_it_cannot_win_a_collision(self):
        rows = [
            _comment_row(None, "<!-- kirocrew-crew id=c_ghost -->"),
            _comment_row(9002, "<!-- kirocrew-crew id=c_b -->"),
            _comment_row(9001, "<!-- kirocrew-crew id=c_a -->"),
        ]
        self.assertEqual([c["comment_id"] for c in gh.find_crew_claim(rows)],
                         [9001, 9002, None])

    def test_a_non_int_comment_id_is_normalized_to_none(self):
        for raw in ("9001", True, 9001.0, {"id": 9001}):
            claim = gh.find_crew_claim([_comment_row(raw, "<!-- kirocrew-crew id=c_1 -->")])[0]
            self.assertIsNone(claim["comment_id"], raw)

    # ── the crew_id filter ───────────────────────────────────────────────────

    def test_crew_id_filters_to_one_crews_own_claims(self):
        rows = [
            _comment_row(9001, "<!-- kirocrew-crew id=c_other phase=claimed -->"),
            _comment_row(9002, f"progress\n{MARKER}"),
        ]
        self.assertEqual(gh.find_crew_claim(rows, crew_id="c_7f3a"),
                         [c for c in gh.find_crew_claim(rows) if c["crew_id"] == "c_7f3a"])
        self.assertEqual(
            [c["comment_id"] for c in gh.find_crew_claim(rows, crew_id="c_7f3a")], [9002]
        )

    def test_a_crew_with_no_claim_gets_an_empty_list_not_none(self):
        # A uniform return type is why the filter does not collapse to a single
        # entry: no caller has to branch on the shape.
        self.assertEqual(gh.find_crew_claim([_comment_row(1, MARKER)], crew_id="c_nope"), [])

    def test_a_duplicated_claim_is_visible_and_ordered(self):
        # A retried post is a real state; collapsing it would hide the duplicate the
        # crew must clean up, and [0] is still the one that wins.
        rows = [
            _comment_row(9002, f"retry\n{MARKER}"),
            _comment_row(9001, f"first\n{MARKER}"),
        ]
        self.assertEqual([c["comment_id"] for c in gh.find_crew_claim(rows, crew_id="c_7f3a")],
                         [9001, 9002])

    # ── input tolerance ──────────────────────────────────────────────────────

    def test_review_comments_and_non_comment_rows_are_never_claims(self):
        # A review_comment's id addresses `pulls/comments/{id}`, a DIFFERENT
        # endpoint — treating one as a claim hands update_issue_comment a bad id.
        rows = [
            {"kind": "review_comment", "actor": "a", "created_at": "2026-08-08T18:00:00Z",
             "body": MARKER, "path": "x.py", "line": 3},
            {"kind": "labeled", "actor": "a", "created_at": "2026-08-08T18:00:00Z",
             "label": {"name": "crew: in progress"}},
        ]
        self.assertEqual(gh.find_crew_claim(rows), [])

    def test_empty_and_malformed_input_is_tolerated(self):
        self.assertEqual(gh.find_crew_claim([]), [])
        self.assertEqual(gh.find_crew_claim(None), [])  # type: ignore[arg-type]
        self.assertEqual(gh.find_crew_claim(["nope", None, 7, {}]), [])  # type: ignore[list-item]
        self.assertEqual(gh.find_crew_claim([_comment_row(1, None)]), [])  # type: ignore[arg-type]

    def test_a_marker_is_found_wherever_it_sits_in_the_body(self):
        for body in (MARKER, f"{MARKER}\nprose after", f"prose before\n\n{MARKER}",
                     f"<details>\n- 18:02 claimed\n</details>\n{MARKER}\n"):
            self.assertEqual(gh.find_crew_claim([_comment_row(1, body)])[0]["crew_id"],
                             "c_7f3a", body)

    def test_the_first_marker_wins_when_a_body_carries_two(self):
        body = f"{MARKER}\nquoted example:\n<!-- kirocrew-crew id=c_zzz phase=resolved -->"
        claim = gh.find_crew_claim([_comment_row(1, body)])[0]
        self.assertEqual(claim["crew_id"], "c_7f3a")
        self.assertEqual(claim["phase"], "implementing")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
