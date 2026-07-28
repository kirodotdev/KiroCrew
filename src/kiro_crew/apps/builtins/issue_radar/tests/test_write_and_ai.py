"""Tests for the issue-editing + AI-triage backend (labels/state writes, AI cache).

Three deterministic, subprocess-free surfaces:
  * store — the AI-result cache (round-trip/delete) and the post-write cache
    coherence helpers (label change patches detail + list caches and drops the
    AI cache; state change drops the issue from the list it left);
  * github_client write primitives — label add/remove + state PATCH, exercised
    by monkeypatching ``_run_gh_write`` so argv/payload shaping and the 404→None
    remove contract are tested without a real ``gh`` call;
  * routes permission gate — ``_has_write_access`` truth table + ``_repo_can_write``
    reading stored perms vs self-healing from a live fetch.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
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
