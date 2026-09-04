"""Tests for the issue-editing + AI-triage backend (labels/state writes, AI cache).

Three deterministic, subprocess-free surfaces:
  * store — the AI-result cache (round-trip/delete) and the post-write cache
    coherence helpers (label change patches detail + list caches and drops the
    AI cache; state change drops the issue from the list it left; assignee change
    patches the detail + list rows, including clearing to nobody);
  * github_client write primitives — label add/remove + state PATCH + assignee
    replace, exercised by monkeypatching ``_run_gh_write`` so argv/payload shaping,
    the 404→None remove contract, and the read-back-what-stuck assignee contract
    are tested without a real ``gh`` call;
  * routes permission gate — ``_has_write_access`` truth table + ``_repo_can_write``
    reading stored perms vs self-healing from a live fetch.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import provider, routes, store


class TestAiCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip_and_delete(self):
        payload = {"summary": "It crashes on start.", "suggested_labels": [{"name": "bug", "reason": "crash"}]}
        store.write_issue_ai_cache("o", "r", 7, payload, root=self.tmp)
        got = store.read_issue_ai_cache("o", "r", 7, self.tmp)
        assert got is not None
        self.assertEqual(got["summary"], "It crashes on start.")
        self.assertEqual(got["suggested_labels"], [{"name": "bug", "reason": "crash"}])
        # Stamped so the card can show how long ago it was generated.
        self.assertTrue(got["generated_at"])
        store.delete_issue_ai_cache("o", "r", 7, self.tmp)
        self.assertIsNone(store.read_issue_ai_cache("o", "r", 7, self.tmp))

    def test_absent_returns_none(self):
        self.assertIsNone(store.read_issue_ai_cache("o", "r", 999, self.tmp))

    def test_delete_absent_is_noop(self):
        store.delete_issue_ai_cache("o", "r", 123, self.tmp)  # must not raise


class TestApplyLabelChangeToCaches(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_patches_detail_and_list_and_drops_ai(self):
        store.write_issue_detail_cache(
            "o", "r", 7, {"number": 7, "labels": [{"name": "old", "color": "abc", "description": ""}]}, [], root=self.tmp
        )
        store.write_issues_cache("o", "r", [{"number": 7, "labels": ["old"]}, {"number": 8, "labels": []}], root=self.tmp)
        store.write_issue_ai_cache("o", "r", 7, {"summary": "s", "suggested_labels": []}, root=self.tmp)

        new_labels = [{"name": "bug", "color": "ee0000", "description": "d"}]
        store.apply_label_change_to_caches("o", "r", 7, new_labels, root=self.tmp)

        detail = store.read_issue_detail_cache("o", "r", 7, self.tmp)
        assert detail is not None
        self.assertEqual(detail["detail"]["labels"], new_labels)
        cached_list = store.read_issues_cache("o", "r", self.tmp, state="open")
        assert cached_list is not None
        by_num = {i["number"]: i for i in cached_list}
        self.assertEqual(by_num[7]["labels"], ["bug"])
        self.assertEqual(by_num[8]["labels"], [])  # untouched
        # AI suggestions are now stale — must be dropped so they recompute.
        self.assertIsNone(store.read_issue_ai_cache("o", "r", 7, self.tmp))

    def test_no_caches_present_is_noop(self):
        store.apply_label_change_to_caches("o", "r", 7, [{"name": "x", "color": "1", "description": ""}], root=self.tmp)


class TestApplyStateChangeToCaches(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_close_drops_from_open_list_and_patches_detail(self):
        store.write_issue_detail_cache("o", "r", 7, {"number": 7, "state": "open", "state_reason": None}, [], root=self.tmp)
        store.write_issues_cache("o", "r", [{"number": 7}, {"number": 9}], root=self.tmp, state="open")

        store.apply_state_change_to_caches("o", "r", 7, "closed", "completed", root=self.tmp)

        detail = store.read_issue_detail_cache("o", "r", 7, self.tmp)
        assert detail is not None
        self.assertEqual(detail["detail"]["state"], "closed")
        self.assertEqual(detail["detail"]["state_reason"], "completed")
        open_list = store.read_issues_cache("o", "r", self.tmp, state="open")
        assert open_list is not None
        remaining = [i["number"] for i in open_list]
        self.assertEqual(remaining, [9])  # #7 dropped from the open list

    def test_reopen_drops_from_closed_list(self):
        store.write_issues_cache("o", "r", [{"number": 7}], root=self.tmp, state="closed")
        store.apply_state_change_to_caches("o", "r", 7, "open", None, root=self.tmp)
        self.assertEqual(store.read_issues_cache("o", "r", self.tmp, state="closed"), [])


class TestApplyAssigneesChangeToCaches(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # addCleanup, not tearDown+ignore_errors: a cleanup failure must SURFACE
        # rather than leave temp residue outliving the run (no-test-side-effects).
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_patches_detail_and_list_row(self):
        store.write_issue_detail_cache(
            "o", "r", 7, {"number": 7, "assignees": ["old"]}, [], root=self.tmp
        )
        store.write_issues_cache(
            "o", "r",
            [{"number": 7, "assignees": ["old"]}, {"number": 8, "assignees": []}],
            root=self.tmp,
        )

        store.apply_assignees_change_to_caches("o", "r", 7, ["alice", "bob"], root=self.tmp)

        detail = store.read_issue_detail_cache("o", "r", 7, self.tmp)
        assert detail is not None
        self.assertEqual(detail["detail"]["assignees"], ["alice", "bob"])
        cached_list = store.read_issues_cache("o", "r", self.tmp, state="open")
        assert cached_list is not None
        by_num = {i["number"]: i for i in cached_list}
        self.assertEqual(by_num[7]["assignees"], ["alice", "bob"])
        self.assertEqual(by_num[8]["assignees"], [])  # untouched

    def test_clearing_writes_empty_list(self):
        """An empty set is a real value, not a no-op: unassigning everyone must
        leave the caches showing nobody, or the sidebar keeps the old name until
        the next refresh."""
        store.write_issue_detail_cache(
            "o", "r", 7, {"number": 7, "assignees": ["alice"]}, [], root=self.tmp
        )
        store.write_issues_cache("o", "r", [{"number": 7, "assignees": ["alice"]}], root=self.tmp)

        store.apply_assignees_change_to_caches("o", "r", 7, [], root=self.tmp)

        detail = store.read_issue_detail_cache("o", "r", 7, self.tmp)
        assert detail is not None
        self.assertEqual(detail["detail"]["assignees"], [])
        cached_list = store.read_issues_cache("o", "r", self.tmp, state="open")
        assert cached_list is not None
        self.assertEqual(cached_list[0]["assignees"], [])

    def test_patches_the_closed_list_too(self):
        """A closed issue can still be (un)assigned, so the closed list is patched
        on the same terms as the open one."""
        store.write_issues_cache(
            "o", "r", [{"number": 7, "assignees": []}], root=self.tmp, state="closed"
        )
        store.apply_assignees_change_to_caches("o", "r", 7, ["alice"], root=self.tmp)
        cached = store.read_issues_cache("o", "r", self.tmp, state="closed")
        assert cached is not None
        self.assertEqual(cached[0]["assignees"], ["alice"])

    def test_drops_falsy_logins(self):
        """The guard exists for PROVIDER data, not for typed callers: a forge payload
        can carry a null or blank login. `None` is deliberately outside the declared
        `list[str]`, so the out-of-contract half is cast rather than left for mypy to
        flag -- the runtime guard is what this pins."""
        store.write_issues_cache("o", "r", [{"number": 7, "assignees": []}], root=self.tmp)
        junk = cast("list[str]", ["alice", "", None])
        store.apply_assignees_change_to_caches("o", "r", 7, junk, root=self.tmp)
        cached = store.read_issues_cache("o", "r", self.tmp, state="open")
        assert cached is not None
        self.assertEqual(cached[0]["assignees"], ["alice"])

    def test_no_caches_present_is_noop(self):
        store.apply_assignees_change_to_caches("o", "r", 7, ["alice"], root=self.tmp)


class TestGhWritePrimitives(unittest.TestCase):
    def test_add_issue_labels_shapes_and_sends_payload(self):
        raw = [
            {"name": "bug", "color": "ee0000", "description": "a bug"},
            {"name": "docs", "color": "0000ee", "description": ""},
        ]
        with mock.patch.object(gh, "_run_gh_write", return_value=raw) as m:
            out = gh.add_issue_labels("o", "r", 7, ["bug", "docs"])
        method, path = m.call_args.args[0], m.call_args.args[1]
        payload = m.call_args.args[2]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "repos/o/r/issues/7/labels")
        self.assertEqual(payload, {"labels": ["bug", "docs"]})
        self.assertEqual(out, [
            {"name": "bug", "color": "ee0000", "description": "a bug"},
            {"name": "docs", "color": "0000ee", "description": ""},
        ])

    def test_remove_issue_label_url_encodes_and_shapes(self):
        with mock.patch.object(gh, "_run_gh_write", return_value=[{"name": "bug", "color": "ee0000"}]) as m:
            out = gh.remove_issue_label("o", "r", 7, "good first issue")
        method, path = m.call_args.args[0], m.call_args.args[1]
        self.assertEqual(method, "DELETE")
        # spaces are percent-encoded into the path segment (injection-safe)
        self.assertEqual(path, "repos/o/r/issues/7/labels/good%20first%20issue")
        self.assertEqual(out, [{"name": "bug", "color": "ee0000", "description": ""}])

    def test_remove_issue_label_404_returns_none(self):
        with mock.patch.object(gh, "_run_gh_write", side_effect=gh.GhCliError("gh api DELETE ... (HTTP 404): not found")):
            self.assertIsNone(gh.remove_issue_label("o", "r", 7, "absent"))

    def test_remove_issue_label_other_error_propagates(self):
        with mock.patch.object(gh, "_run_gh_write", side_effect=gh.GhCliError("boom (exit 1)")):
            with self.assertRaises(gh.GhCliError):
                gh.remove_issue_label("o", "r", 7, "x")

    def test_set_issue_state_close_defaults_completed(self):
        with mock.patch.object(gh, "_run_gh_write", return_value={"state": "closed", "state_reason": "completed"}) as m:
            out = gh.set_issue_state("o", "r", 7, "closed")
        method, path, payload = m.call_args.args[0], m.call_args.args[1], m.call_args.args[2]
        self.assertEqual((method, path), ("PATCH", "repos/o/r/issues/7"))
        self.assertEqual(payload, {"state": "closed", "state_reason": "completed"})
        self.assertEqual(out, {"state": "closed", "state_reason": "completed"})

    def test_set_issue_state_close_not_planned(self):
        with mock.patch.object(gh, "_run_gh_write", return_value={"state": "closed", "state_reason": "not_planned"}) as m:
            gh.set_issue_state("o", "r", 7, "closed", "not_planned")
        self.assertEqual(m.call_args.args[2], {"state": "closed", "state_reason": "not_planned"})

    def test_set_issue_state_reopen_clears_reason(self):
        with mock.patch.object(gh, "_run_gh_write", return_value={"state": "open", "state_reason": None}) as m:
            gh.set_issue_state("o", "r", 7, "open")
        self.assertEqual(m.call_args.args[2], {"state": "open", "state_reason": None})

    def test_set_issue_assignees_replaces_and_reads_back(self):
        with mock.patch.object(
            gh, "_run_gh_write",
            return_value={"assignees": [{"login": "alice"}, {"login": "bob"}]},
        ) as m:
            out = gh.set_issue_assignees("o", "r", 7, ["alice", "bob"])
        method, path, payload = m.call_args.args[0], m.call_args.args[1], m.call_args.args[2]
        self.assertEqual((method, path), ("PATCH", "repos/o/r/issues/7"))
        self.assertEqual(payload, {"assignees": ["alice", "bob"]})
        self.assertEqual(out, ["alice", "bob"])

    def test_set_issue_assignees_empty_list_clears(self):
        """An empty array is how GitHub clears assignees, so it must be SENT --
        not skipped as a no-op."""
        with mock.patch.object(gh, "_run_gh_write", return_value={"assignees": []}) as m:
            out = gh.set_issue_assignees("o", "r", 7, [])
        self.assertEqual(m.call_args.args[2], {"assignees": []})
        self.assertEqual(out, [])

    def test_set_issue_assignees_returns_the_response_not_the_request(self):
        """A successful write is not required to be an exact echo (GitLab Free keeps
        only the first assignee), so the result is read from the RESPONSE. Echoing
        the request would report an assignee the issue does not carry."""
        with mock.patch.object(
            gh, "_run_gh_write", return_value={"assignees": [{"login": "alice"}]}
        ):
            out = gh.set_issue_assignees("o", "r", 7, ["alice", "bob"])
        self.assertEqual(out, ["alice"])

    def test_set_issue_assignees_422_becomes_invalid_input_naming_the_logins(self):
        """Verified against the live API: an unassignable login answers HTTP 422
        and NONE of the write is applied. That must not surface as a 502 -- the
        forge is fine, the input is not -- so it is raised as the invalid-input
        class carrying the refused logins.

        The fixture is the REAL message shape observed live: gh's stderr tail keeps
        only gh's own summary line, so the API body that named
        ``"field":"assignees"`` is already gone by the time the client sees it.
        That is why the detection keys on the status alone."""
        with mock.patch.object(
            gh, "_run_gh_write",
            side_effect=gh.GhCliError(
                "gh api PATCH repos/o/r/issues/7 failed (exit 1): "
                "gh: Validation Failed (HTTP 422)"
            ),
        ):
            with self.assertRaises(gh.GhInvalidInputError) as ctx:
                gh.set_issue_assignees("o", "r", 7, ["alice", "octocat"])
        self.assertEqual(ctx.exception.values, ["alice", "octocat"])
        # Still a GhCliError subclass, so pre-existing handlers keep catching it.
        self.assertIsInstance(ctx.exception, gh.GhCliError)

    def test_set_issue_assignees_non_422_failure_is_not_relabelled(self):
        """An upstream failure that is NOT a validation error stays a generic
        GhCliError, so it keeps its 502 -- the forge really is the problem and a
        retry is the right advice. (A 422 on some other field cannot arise here:
        the request body carries only ``assignees``.)"""
        for msg in (
            "gh api PATCH repos/o/r/issues/7 failed (exit 1): gh: Server Error (HTTP 500)",
            "gh api PATCH repos/o/r/issues/7 failed (exit 1): connection refused",
        ):
            with mock.patch.object(gh, "_run_gh_write", side_effect=gh.GhCliError(msg)):
                with self.assertRaises(gh.GhCliError) as ctx:
                    gh.set_issue_assignees("o", "r", 7, ["alice"])
            self.assertNotIsInstance(ctx.exception, gh.GhInvalidInputError, msg)

    def test_set_issue_assignees_permission_error_stays_a_permission_error(self):
        """403 is the caller lacking a right, not a bad login -- it must reach the
        route's 403 branch rather than being folded into the input error."""
        with mock.patch.object(
            gh, "_run_gh_write", side_effect=gh.GhPermissionError("refused")
        ):
            with self.assertRaises(gh.GhPermissionError):
                gh.set_issue_assignees("o", "r", 7, ["alice"])

    def test_set_issue_assignees_tolerates_junk_response(self):
        with mock.patch.object(gh, "_run_gh_write", return_value=None):
            self.assertEqual(gh.set_issue_assignees("o", "r", 7, ["alice"]), [])
        with mock.patch.object(
            gh, "_run_gh_write",
            return_value={"assignees": [{"login": "alice"}, {"no_login": 1}, "x", {"login": ""}]},
        ):
            self.assertEqual(gh.set_issue_assignees("o", "r", 7, ["alice"]), ["alice"])

    def test_shape_labels_tolerates_junk(self):
        self.assertEqual(gh._shape_labels(None), [])
        self.assertEqual(
            gh._shape_labels([{"name": "a"}, {"no_name": 1}, "x", {"name": "b", "color": "fff", "description": "d"}]),
            [{"name": "a", "color": "888888", "description": ""}, {"name": "b", "color": "fff", "description": "d"}],
        )


class TestWritePermissionGate(unittest.TestCase):
    def test_has_write_access_truth_table(self):
        self.assertFalse(routes._has_write_access(None))
        self.assertFalse(routes._has_write_access({}))
        self.assertFalse(routes._has_write_access({"pull": True}))
        for role in ("triage", "push", "maintain", "admin"):
            self.assertTrue(routes._has_write_access({role: True}), role)

    def test_repo_can_write_reads_stored_permissions(self):
        with mock.patch.object(
            routes.store, "list_connected_repos",
            return_value=[{"owner": "o", "repo": "r", "permissions": {"triage": True}}],
        ):
            self.assertTrue(routes._repo_can_write(provider.key_from_parts("o", "r")))
        with mock.patch.object(
            routes.store, "list_connected_repos",
            return_value=[{"owner": "o", "repo": "r", "permissions": {"pull": True}}],
        ):
            self.assertFalse(routes._repo_can_write(provider.key_from_parts("o", "r")))

    def test_repo_can_write_self_heals_when_missing(self):
        with mock.patch.object(
            routes.store, "list_connected_repos",
            return_value=[{"owner": "o", "repo": "r"}],  # no permissions stored
        ), mock.patch.object(
            routes.github_client, "get_repo_permissions", return_value={"push": True}
        ) as fetch, mock.patch.object(routes.store, "set_repo_permissions") as heal:
            self.assertTrue(routes._repo_can_write(provider.key_from_parts("o", "r")))
            fetch.assert_called_once()
            heal.assert_called_once()

    def test_repo_can_write_none_when_unknowable(self):
        with mock.patch.object(
            routes.store, "list_connected_repos", return_value=[{"owner": "o", "repo": "r"}],
        ), mock.patch.object(
            routes.github_client, "get_repo_permissions", side_effect=gh.GhCliError("gh down")
        ):
            self.assertIsNone(routes._repo_can_write(provider.key_from_parts("o", "r")))


if __name__ == "__main__":
    unittest.main()
