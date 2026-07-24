"""Tests for repo membership (derived, not fetched) + its cache.

Covers the two pieces of new behaviour:
  * github_client.derive_members — reduce issue authors to the distinct set of
    repo members (author_association ∈ {OWNER, MEMBER, COLLABORATOR}), strongest
    association wins, non-members / author-less issues dropped, sorted by login;
  * store members cache — round-trips {login, association}, returns None when
    absent, and is removed with the repo on disconnect.

Both are pure/local (no ``gh`` calls), matching test_issue_detail.py's approach
of exercising the reduction + cache directly.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import store
from kiro_crew.atomic_write import atomic_write


def _iss(author, assoc):
    return {"number": 1, "author": author, "author_association": assoc}


class TestDeriveMembers(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(gh.derive_members([]), [])

    def test_keeps_member_associations_only(self):
        issues = [
            _iss("owner1", "OWNER"),
            _iss("mem1", "MEMBER"),
            _iss("collab1", "COLLABORATOR"),
            _iss("ext1", "CONTRIBUTOR"),
            _iss("ext2", "FIRST_TIME_CONTRIBUTOR"),
            _iss("ext3", "NONE"),
            _iss("ext4", None),
        ]
        got = gh.derive_members(issues)
        logins = {m["login"] for m in got}
        self.assertEqual(logins, {"owner1", "mem1", "collab1"})

    def test_sorted_by_login(self):
        got = gh.derive_members([_iss("zoe", "MEMBER"), _iss("amy", "MEMBER")])
        self.assertEqual([m["login"] for m in got], ["amy", "zoe"])

    def test_dedup_strongest_association_wins(self):
        # Same author shows up as COLLABORATOR then OWNER across two issues —
        # the strongest (OWNER) is what the member set records.
        got = gh.derive_members([_iss("alice", "COLLABORATOR"), _iss("alice", "OWNER")])
        self.assertEqual(got, [{"login": "alice", "association": "OWNER"}])

    def test_member_ranks_member_over_collaborator(self):
        got = gh.derive_members([_iss("bob", "MEMBER"), _iss("bob", "COLLABORATOR")])
        self.assertEqual(got, [{"login": "bob", "association": "MEMBER"}])

    def test_ignores_authorless_issues(self):
        self.assertEqual(gh.derive_members([_iss(None, "MEMBER"), _iss("", "OWNER")]), [])


class TestListCollaborators(unittest.TestCase):
    """The authoritative roster: maps gh output to {login, role_name}, and turns
    a 403 (no push access) into GhPermissionError so callers can fall back."""

    def test_maps_login_and_role(self):
        rows = [{"login": "a", "role_name": "admin"}, {"login": "b", "role_name": "read"}]
        with mock.patch.object(gh, "_run_gh_api", return_value=rows):
            out = gh.list_repo_collaborators("o", "r")
        self.assertEqual(out, rows)

    def test_403_becomes_permission_error(self):
        with mock.patch.object(gh, "_run_gh_api", side_effect=gh.GhCliError("gh api collaborators failed (exit 1): (HTTP 403)")):
            with self.assertRaises(gh.GhPermissionError):
                gh.list_repo_collaborators("o", "r")

    def test_push_access_message_becomes_permission_error(self):
        with mock.patch.object(gh, "_run_gh_api", side_effect=gh.GhCliError("Must have push access to view repository collaborators.")):
            with self.assertRaises(gh.GhPermissionError):
                gh.list_repo_collaborators("o", "r")

    def test_other_error_is_not_permission_error(self):
        with mock.patch.object(gh, "_run_gh_api", side_effect=gh.GhCliError("boom (exit 1): 500")):
            with self.assertRaises(gh.GhCliError) as ctx:
                gh.list_repo_collaborators("o", "r")
        self.assertNotIsInstance(ctx.exception, gh.GhPermissionError)


class TestMembersCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_absent_returns_none(self):
        self.assertIsNone(store.read_members_cache("o", "r", self.tmp))

    def test_roundtrip_with_source(self):
        members = [{"login": "alice", "role": "admin"}, {"login": "bob", "role": "read"}]
        store.write_members_cache("o", "r", members, source="collaborators", root=self.tmp)
        got = store.read_members_cache("o", "r", self.tmp)
        self.assertEqual(got, {"members": members, "source": "collaborators"})

    def test_derived_source_roundtrips(self):
        members = [{"login": "alice", "role": "MEMBER"}]
        store.write_members_cache("o", "r", members, source="derived", root=self.tmp)
        cached = store.read_members_cache("o", "r", self.tmp)
        assert cached is not None
        self.assertEqual(cached["source"], "derived")

    def test_removed_with_repo_on_disconnect(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.write_members_cache("o", "r", [{"login": "alice", "role": "admin"}], source="collaborators", root=self.tmp)
        self.assertIsNotNone(store.read_members_cache("o", "r", self.tmp))
        store.remove_connected_repo("o", "r", root=self.tmp)
        self.assertIsNone(store.read_members_cache("o", "r", self.tmp))


class TestIssuesCacheSchema(unittest.TestCase):
    """Why the member set can go empty while the detail pane still shows badges:
    a stale issue cache (written before ``author_association`` existed) has no
    association to derive from. The schema stamp makes such a cache read as a
    miss so the route refetches with the current field set."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_current_schema_roundtrips(self):
        store.write_issues_cache(
            "o", "r", [{"number": 1, "author": "a", "author_association": "MEMBER"}], root=self.tmp
        )
        got = store.read_issues_cache("o", "r", self.tmp)
        assert got is not None
        self.assertEqual(got[0]["author_association"], "MEMBER")

    def test_missing_schema_stamp_is_ignored(self):
        # A pre-versioning cache: valid JSON, real issues, but no schema stamp
        # and — crucially — no author_association. Must read as a miss.
        path = store.issues_cache_path("o", "r", self.tmp, "open")
        atomic_write(path, json.dumps({"owner": "o", "repo": "r", "state": "open",
                                       "issues": [{"number": 1, "author": "a"}]}))
        self.assertIsNone(store.read_issues_cache("o", "r", self.tmp))

    def test_old_schema_number_is_ignored(self):
        path = store.issues_cache_path("o", "r", self.tmp, "open")
        atomic_write(path, json.dumps({"schema": store.ISSUES_CACHE_SCHEMA - 1,
                                       "issues": [{"number": 1, "author": "a"}]}))
        self.assertIsNone(store.read_issues_cache("o", "r", self.tmp))


if __name__ == "__main__":
    unittest.main()
