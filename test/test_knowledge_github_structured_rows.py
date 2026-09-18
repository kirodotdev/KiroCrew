"""Typed rows, payload conversion, primary keys and lineage for the GitHub
structured connector.

These are pure-function tests: no database, no network, no event loop. They lock
in the shape of the deliverable that is testable on ``main`` today — real typed
fields, a primary key that is the dedup identity (never the title), conversion
from the GitHub payload shape, and per-row lineage that MUST be complete.
"""

import unittest

from kiro_crew.knowledge.connectors.github_structured import (
    DEFAULT_INSTANCE,
    ENTITY_CHECK_RUN,
    ENTITY_COMMIT,
    ENTITY_ISSUE,
    ENTITY_PULL_REQUEST,
    CheckRunRow,
    CommitRow,
    GithubStructuredConnector,
    IssueOrPullRow,
    LineageError,
    RowLineage,
    check_run_from_payload,
    commit_from_payload,
    issue_or_pull_from_payload,
    render_row_metadata,
    render_row_text,
)

REPO = "kirodotdev/KiroCrew"
SRC = "src-github-1"  # a knowledge source id
NOW = "2026-09-16T00:00:00Z"


# Thin wrappers that inject the source_id every conversion now requires, so the
# tests read like the payload calls they document. Pass source_id/instance to
# override for the key-domain collision tests.
def mk_issue(payload, *, source_id=SRC, instance=DEFAULT_INSTANCE, repo=REPO):
    return issue_or_pull_from_payload(
        repo, payload, source_id=source_id, instance=instance, fetched_at=NOW)


def mk_commit(payload, *, source_id=SRC, instance=DEFAULT_INSTANCE, repo=REPO):
    return commit_from_payload(
        repo, payload, source_id=source_id, instance=instance, fetched_at=NOW)


def mk_check(payload, *, source_id=SRC, instance=DEFAULT_INSTANCE, repo=REPO):
    return check_run_from_payload(
        repo, payload, source_id=source_id, instance=instance, fetched_at=NOW)


def _issue_payload(**over):
    base = {
        "number": 42,
        "title": "a bug",
        "state": "open",
        "user": {"login": "octocat"},
        "body": "steps to reproduce",
        "labels": [{"name": "bug"}, {"name": "p1"}],
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-10T00:00:00Z",
        "closed_at": None,
        "html_url": "https://github.com/kirodotdev/KiroCrew/issues/42",
        "url": "https://api.github.com/repos/kirodotdev/KiroCrew/issues/42",
    }
    base.update(over)
    return base


def _commit_payload(**over):
    base = {
        "sha": "a" * 40,
        "commit": {
            "message": "fix: thing",
            "author": {"name": "Octo", "email": "o@example.com",
                       "date": "2026-09-05T00:00:00Z"},
            "committer": {"name": "Octo", "email": "o@example.com",
                          "date": "2026-09-05T00:01:00Z"},
        },
        "parents": [{"sha": "b" * 40}],
        "html_url": "https://github.com/kirodotdev/KiroCrew/commit/" + "a" * 40,
        "url": "https://api.github.com/repos/kirodotdev/KiroCrew/commits/" + "a" * 40,
    }
    base.update(over)
    return base


def _check_run_payload(**over):
    base = {
        "id": 987654321,
        "name": "PR Readiness",
        "head_sha": "a" * 40,
        "status": "completed",
        "conclusion": "success",
        "started_at": "2026-09-05T00:02:00Z",
        "completed_at": "2026-09-05T00:05:00Z",
        "details_url": "https://github.com/kirodotdev/KiroCrew/runs/987654321",
        "html_url": "https://github.com/kirodotdev/KiroCrew/runs/987654321",
        "url": "https://api.github.com/repos/kirodotdev/KiroCrew/check-runs/987654321",
    }
    base.update(over)
    return base


class TestConversion(unittest.TestCase):
    def test_issue_conversion_has_typed_fields(self):
        row = mk_issue(_issue_payload())
        self.assertIsInstance(row, IssueOrPullRow)
        self.assertEqual(row.number, 42)
        self.assertIsInstance(row.number, int)  # typed, not a string blob
        self.assertFalse(row.is_pull_request)
        self.assertEqual(row.author, "octocat")
        self.assertEqual(row.labels, ("bug", "p1"))
        self.assertEqual(row.lineage.entity_type, ENTITY_ISSUE)

    def test_pull_request_detected_from_pull_request_subobject(self):
        row = mk_issue(_issue_payload(pull_request={"url": "..."}))
        self.assertTrue(row.is_pull_request)
        self.assertEqual(row.lineage.entity_type, ENTITY_PULL_REQUEST)

    def test_commit_conversion_has_typed_fields(self):
        row = mk_commit(_commit_payload())
        self.assertIsInstance(row, CommitRow)
        self.assertEqual(row.sha, "a" * 40)
        self.assertEqual(row.author_name, "Octo")
        self.assertEqual(row.parents, ("b" * 40,))
        self.assertEqual(row.lineage.entity_type, ENTITY_COMMIT)

    def test_check_run_conversion_has_typed_fields(self):
        row = mk_check(_check_run_payload())
        self.assertIsInstance(row, CheckRunRow)
        self.assertEqual(row.id, 987654321)
        self.assertIsInstance(row.id, int)
        self.assertEqual(row.conclusion, "success")
        self.assertEqual(row.lineage.entity_type, ENTITY_CHECK_RUN)


class TestPrimaryKeys(unittest.TestCase):
    def test_issue_pk_carries_the_full_domain(self):
        row = mk_issue(_issue_payload())
        # source | instance | repo | kind | entity-key
        self.assertEqual(
            row.primary_key,
            f"{SRC}|{DEFAULT_INSTANCE}|{REPO}|{ENTITY_ISSUE}|42")

    def test_pk_is_stable_across_title_change(self):
        # NEGATIVE: the title is NOT the key. A renamed issue keeps its identity,
        # so a refresh upserts it instead of creating a duplicate.
        a = mk_issue(_issue_payload(title="old"))
        b = mk_issue(_issue_payload(title="new"))
        self.assertEqual(a.primary_key, b.primary_key)
        self.assertNotEqual(a.title, b.title)

    def test_commit_pk_carries_the_full_domain(self):
        row = mk_commit(_commit_payload())
        self.assertEqual(
            row.primary_key,
            f"{SRC}|{DEFAULT_INSTANCE}|{REPO}|{ENTITY_COMMIT}|{'a' * 40}")

    def test_check_run_pk_carries_the_full_domain(self):
        row = mk_check(_check_run_payload())
        self.assertEqual(
            row.primary_key,
            f"{SRC}|{DEFAULT_INSTANCE}|{REPO}|{ENTITY_CHECK_RUN}|987654321")

    def test_keys_never_collide_across_kinds(self):
        # A commit sha and an issue number that happen to render the same tail
        # still get distinct keys because of the kind segment.
        issue = mk_issue(_issue_payload(number=1))
        run = mk_check(_check_run_payload(id=1))
        self.assertNotEqual(issue.primary_key, run.primary_key)

    def test_same_number_in_two_repos_does_not_collapse(self):
        # NEGATIVE (key domain): issue #1 in repo A and issue #1 in repo B are
        # two distinct records. Without the repo segment they would collide.
        a = mk_issue(_issue_payload(number=1), repo="octo/alpha")
        b = mk_issue(_issue_payload(number=1), repo="octo/beta")
        self.assertNotEqual(a.primary_key, b.primary_key)

    def test_same_record_in_two_instances_does_not_collapse(self):
        # NEGATIVE (key domain): the same owner/repo/number on github.com and on
        # a GitHub Enterprise host are two different records.
        a = mk_issue(_issue_payload(number=1), instance="github.com")
        b = mk_issue(_issue_payload(number=1), instance="ghe.corp.example")
        self.assertNotEqual(a.primary_key, b.primary_key)

    def test_same_record_in_two_sources_does_not_collapse(self):
        # NEGATIVE (key domain): one repo mirrored under two knowledge sources
        # keys apart, so deleting one source cannot orphan the other's rows.
        a = mk_issue(_issue_payload(number=1), source_id="src-A")
        b = mk_issue(_issue_payload(number=1), source_id="src-B")
        self.assertNotEqual(a.primary_key, b.primary_key)

    def test_commit_sha_collides_only_within_the_same_domain(self):
        # NEGATIVE (key domain): identical sha, different repo -> distinct keys;
        # identical sha, same full domain -> identical key (true dedup).
        same = mk_commit(_commit_payload())
        same2 = mk_commit(_commit_payload())
        self.assertEqual(same.primary_key, same2.primary_key)
        other_repo = mk_commit(_commit_payload(), repo="octo/fork")
        self.assertNotEqual(same.primary_key, other_repo.primary_key)


class TestLineage(unittest.TestCase):
    def test_every_row_carries_the_full_lineage_domain(self):
        for row in (mk_issue(_issue_payload()), mk_commit(_commit_payload()),
                    mk_check(_check_run_payload())):
            lin = row.lineage
            self.assertEqual(lin.source_id, SRC)
            self.assertEqual(lin.instance, DEFAULT_INSTANCE)
            self.assertEqual(lin.repo_full_name, REPO)
            self.assertIn(lin.entity_type, (
                ENTITY_ISSUE, ENTITY_PULL_REQUEST, ENTITY_COMMIT, ENTITY_CHECK_RUN))
            self.assertTrue(lin.primary_key)
            self.assertTrue(lin.api_url.startswith("https://api.github.com/"))
            self.assertEqual(lin.fetched_at, NOW)

    def test_lineage_primary_key_matches_composed_key(self):
        # The lineage's stored primary_key and the row's composed key agree.
        row = mk_issue(_issue_payload())
        self.assertEqual(row.lineage.primary_key, row.primary_key)

    def test_missing_api_url_fails_conversion(self):
        # NEGATIVE: a row with no source API URL cannot be traced, so conversion
        # MUST fail rather than store an untraceable row.
        payload = _issue_payload()
        del payload["url"]
        with self.assertRaises(LineageError):
            mk_issue(payload)

    def _lineage(self, **over):
        base = dict(source_id=SRC, instance=DEFAULT_INSTANCE, repo_full_name=REPO,
                    entity_type=ENTITY_ISSUE, primary_key="k",
                    api_url="https://api.github.com/x", fetched_at=NOW)
        base.update(over)
        return RowLineage(**base)

    def test_missing_fetched_at_fails_lineage(self):
        with self.assertRaises(LineageError):
            self._lineage(fetched_at="").validate()

    def test_missing_source_id_fails_lineage(self):
        # NEGATIVE (key domain): the source segment is mandatory.
        with self.assertRaises(LineageError):
            self._lineage(source_id="").validate()

    def test_missing_instance_fails_lineage(self):
        # NEGATIVE (key domain): the instance segment is mandatory.
        with self.assertRaises(LineageError):
            self._lineage(instance="").validate()

    def test_separator_in_domain_segment_fails_lineage(self):
        # NEGATIVE: a value carrying the key separator could forge a different
        # key, so it is refused at construction.
        with self.assertRaises(LineageError):
            self._lineage(source_id="src|evil").validate()
        with self.assertRaises(LineageError):
            self._lineage(instance="ghe|evil").validate()

    def test_bad_repo_full_name_fails_lineage(self):
        with self.assertRaises(LineageError):
            self._lineage(repo_full_name="not-a-repo").validate()

    def test_unknown_entity_type_fails_lineage(self):
        with self.assertRaises(LineageError):
            self._lineage(entity_type="github_gist").validate()

    def test_missing_required_identity_field_fails(self):
        # NEGATIVE: no number means no key means no identity.
        payload = _issue_payload()
        del payload["number"]
        with self.assertRaises(LineageError):
            mk_issue(payload)

    def test_commit_missing_sha_fails(self):
        payload = _commit_payload()
        del payload["sha"]
        with self.assertRaises(LineageError):
            mk_commit(payload)


class TestRendering(unittest.TestCase):
    def test_text_projection_is_legible_and_names_the_key(self):
        row = mk_commit(_commit_payload())
        text = render_row_text(row)
        self.assertIn(row.primary_key, text)
        self.assertIn("fix: thing", text)
        self.assertNotIn("lineage", text)  # lineage is metadata, not body noise

    def test_metadata_carries_pk_and_full_domain(self):
        row = mk_check(_check_run_payload())
        meta = render_row_metadata(row)
        self.assertEqual(meta["primary_key"], row.primary_key)
        self.assertEqual(meta["source_id"], SRC)
        self.assertEqual(meta["instance"], DEFAULT_INSTANCE)
        self.assertEqual(meta["repo_full_name"], REPO)
        self.assertEqual(meta["entity_type"], ENTITY_CHECK_RUN)
        self.assertEqual(meta["api_url"], row.lineage.api_url)
        self.assertEqual(meta["fetched_at"], NOW)


class TestConnectorContract(unittest.TestCase):
    def setUp(self):
        self.c = GithubStructuredConnector()

    def test_source_type(self):
        self.assertEqual(self.c.source_type(), "github")

    def test_validate_config_accepts_uri_owner_repo(self):
        ok, msg = self.c.validate_config({"uri": REPO})
        self.assertTrue(ok)
        self.assertEqual(msg, "")

    def test_validate_config_accepts_github_uri(self):
        ok, _ = self.c.validate_config({"uri": "github://kirodotdev/KiroCrew"})
        self.assertTrue(ok)

    def test_validate_config_rejects_missing_repo(self):
        ok, msg = self.c.validate_config({})
        self.assertFalse(ok)
        self.assertIn("required", msg)

    def test_validate_config_rejects_malformed_repo(self):
        ok, msg = self.c.validate_config({"uri": "just-a-name"})
        self.assertFalse(ok)
        self.assertIn("owner/name", msg)


if __name__ == "__main__":
    unittest.main()
