"""Issue Radar GitLab support.

Covers the parts where a GitLab bug would be silent or dangerous rather than
loud:

  * URL parsing -- nested groups, deep pages, and the SSRF guard (an unlisted
    self-managed host must be refused).
  * Host authorization at the SPAWN boundary, not just at connect time, and the
    GITLAB_TOKEN suppression that stops a gitlab.com PAT reaching a private host.
  * Storage isolation -- a GitLab project must never read or write the GitHub
    tree, and public GitHub must keep its ORIGINAL paths (no migration).
  * Identity -- ``is_repo_connected`` must not authorize a request for the same
    owner/repo on a different provider or host.
  * Normalization -- the GitLab payloads must arrive in the exact GitHub-shaped
    dicts the routes, caches, and React components already consume.
  * Client parity -- both modules must expose the same surface, so a route can
    dispatch without knowing which provider it has.
"""

from __future__ import annotations

import inspect
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import (
    github_client,
    gitlab_client,
    provider,
    routes,
    store,
)
from kiro_crew.apps.builtins.issue_radar.backend.errors import (
    ProviderCliError,
    RepoUrlError,
)

ALLOWED = frozenset({"gitlab.acme.internal", "gitlab.acme.internal:8443"})


class TestGitlabUrlParsing(unittest.TestCase):
    def test_simple_project(self):
        self.assertEqual(
            gitlab_client.parse_gitlab_repo_url("https://gitlab.com/group/project"),
            ("gitlab.com", "group", "project"),
        )

    def test_nested_groups_keep_full_namespace(self):
        # A GitLab project's identity includes every ancestor group, so the
        # namespace must not be truncated to the last segment.
        self.assertEqual(
            gitlab_client.parse_gitlab_repo_url("https://gitlab.com/a/b/c/proj"),
            ("gitlab.com", "a/b/c", "proj"),
        )

    def test_deep_page_urls_resolve_to_the_project(self):
        # Users paste whatever tab they are on.
        for suffix in ("/-/issues", "/-/merge_requests/7", "/-/tree/main", "/-/settings/ci_cd"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    gitlab_client.parse_gitlab_repo_url(f"https://gitlab.com/g/p{suffix}"),
                    ("gitlab.com", "g", "p"),
                )

    def test_git_suffix_stripped(self):
        self.assertEqual(
            gitlab_client.parse_gitlab_repo_url("https://gitlab.com/g/p.git"),
            ("gitlab.com", "g", "p"),
        )

    def test_unlisted_self_managed_host_is_refused(self):
        # The SSRF guard: browser input must not choose which instance the
        # credential-bearing CLI talks to.
        with self.assertRaises(RepoUrlError):
            gitlab_client.parse_gitlab_repo_url("https://gitlab.evil.test/g/p")

    def test_allowlisted_self_managed_host_is_accepted(self):
        self.assertEqual(
            gitlab_client.parse_gitlab_repo_url(
                "https://gitlab.acme.internal/g/p", allowed_hosts=ALLOWED
            ),
            ("gitlab.acme.internal", "g", "p"),
        )

    def test_port_must_be_allowlisted_separately(self):
        # An entry without a port does not authorize an arbitrary port.
        with self.assertRaises(RepoUrlError):
            gitlab_client.parse_gitlab_repo_url(
                "https://gitlab.acme.internal:9999/g/p", allowed_hosts=ALLOWED
            )
        self.assertEqual(
            gitlab_client.parse_gitlab_repo_url(
                "https://gitlab.acme.internal:8443/g/p", allowed_hosts=ALLOWED
            ),
            ("gitlab.acme.internal:8443", "g", "p"),
        )

    def test_trailing_dot_fqdn_matches_allowlist(self):
        self.assertEqual(
            gitlab_client.parse_gitlab_repo_url(
                "https://gitlab.acme.internal./g/p", allowed_hosts=ALLOWED
            ),
            ("gitlab.acme.internal", "g", "p"),
        )

    def test_rejects_http_and_userinfo(self):
        for bad in ("http://gitlab.com/g/p", "https://u:p@gitlab.com/g/p"):
            with self.subTest(bad=bad), self.assertRaises(RepoUrlError):
                gitlab_client.parse_gitlab_repo_url(bad)

    def test_malformed_authority_is_a_client_error_not_a_crash(self):
        """A bad host/port must not escape as an unhandled 500.

        ``hostname`` and ``port`` parse the authority lazily, and ``urlparse``
        itself raises on some forms, so a malformed URL raised ``ValueError`` from
        three different points -- none of which the connect route catches. Every
        one is client input and must arrive as :class:`RepoUrlError` (HTTP 400).
        """
        for bad in (
            "https://gitlab.com:notaport/g/p",   # port not an integer
            "https://gitlab.com:99999999/g/p",   # port out of range
            "https://[bad/g/p",                  # unclosed IPv6 bracket
            "https://[::1/g/p",
        ):
            with self.subTest(bad=bad), self.assertRaises(RepoUrlError):
                gitlab_client.parse_gitlab_repo_url(bad, allowed_hosts=ALLOWED)

    def test_malformed_authority_is_also_caught_through_dispatch(self):
        # The connect route goes through provider.parse_repo_url, so the guard has
        # to hold on that path too -- that is the one an HTTP request reaches.
        with self.assertRaises(RepoUrlError):
            provider.parse_repo_url("https://gitlab.com:notaport/g/p")

    def test_rejects_system_paths_and_single_segment(self):
        for bad in (
            "https://gitlab.com/groups/foo",
            "https://gitlab.com/only-one",
            "https://gitlab.com/g/../p",
        ):
            with self.subTest(bad=bad), self.assertRaises(RepoUrlError):
                gitlab_client.parse_gitlab_repo_url(bad)

    def test_project_path_encodes_nested_namespace(self):
        self.assertEqual(gitlab_client.project_path("a/b", "c"), "a%2Fb%2Fc")

    def test_real_world_self_managed_three_level_namespace(self):
        """A real self-managed URL shape, kept as a fixture.

        Modelled on a real self-managed deployment: three namespace levels on a
        non-gitlab.com host, which exercises the two things most likely to break
        together -- host allowlisting and a namespace deep enough that truncating
        it still yields a plausible-looking project path
        (``platform/widget-service`` would resolve to nothing, or worse, to
        somebody else's project).
        """
        url = "https://gitlab.acme.internal/platform/team-tools/backend/widget-service"
        allowed = frozenset({"gitlab.acme.internal"})

        # Not allowlisted -> refused, even though the URL is perfectly well-formed.
        with self.assertRaises(RepoUrlError):
            gitlab_client.parse_gitlab_repo_url(url)

        host, namespace, project = gitlab_client.parse_gitlab_repo_url(url, allowed_hosts=allowed)
        self.assertEqual(host, "gitlab.acme.internal")
        self.assertEqual(namespace, "platform/team-tools/backend")
        self.assertEqual(project, "widget-service")

        # Every level survives into the API's single :id parameter.
        self.assertEqual(
            gitlab_client.project_path(namespace, project),
            "platform%2Fteam-tools%2Fbackend%2Fwidget-service",
        )

        # Whatever tab the user pasted from resolves to the same project.
        for suffix in ("/-/issues", "/-/merge_requests/12", "/-/tree/main/src", ".git"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    gitlab_client.parse_gitlab_repo_url(url + suffix, allowed_hosts=allowed),
                    (host, namespace, project),
                )

    def test_self_managed_data_never_lands_in_the_github_tree(self):
        """The same URL shape, checked end to end through dispatch and storage."""
        url = "https://gitlab.acme.internal/platform/team-tools/backend/widget-service"
        with mock.patch.object(
            gitlab_client, "allowed_hosts", return_value=frozenset({"gitlab.acme.internal"})
        ):
            key = provider.parse_repo_url(url)
        self.assertEqual(key.provider, "gitlab")
        self.assertEqual(key.host, "gitlab.acme.internal")
        # The host must ride on every call — omitting it is refused by design.
        self.assertEqual(provider.call_kwargs(key), {"host": "gitlab.acme.internal"})
        self.assertEqual(key.web_url(), url)

        root = Path(tempfile.mkdtemp(prefix="ir-selfmanaged-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        scope = store.provider_root(root=root, provider=key.provider, host=key.host)
        repo_dir = store.repo_data_dir(key.owner, key.repo, scope)
        self.assertEqual(
            repo_dir.relative_to(root).as_posix(),
            "@providers/gitlab/gitlab.acme.internal/repos/"
            "platform/team-tools/backend/widget-service",
        )
        # Nothing was written into the legacy public-GitHub tree.
        self.assertFalse((root / "repos" / "platform").exists())


class TestParseRepoUrlDispatch(unittest.TestCase):
    def test_github_url_goes_to_github(self):
        key = provider.parse_repo_url("https://github.com/o/r")
        self.assertEqual((key.provider, key.host, key.owner, key.repo), ("github", "github.com", "o", "r"))

    def test_gitlab_url_goes_to_gitlab(self):
        with mock.patch.object(gitlab_client, "allowed_hosts", return_value=frozenset()):
            key = provider.parse_repo_url("https://gitlab.com/g/p")
        self.assertEqual((key.provider, key.host, key.owner, key.repo), ("gitlab", "gitlab.com", "g", "p"))

    def test_dispatch_matches_the_host_exactly_not_as_a_substring(self):
        """A URL merely CONTAINING "://github.com/" is not a GitHub URL.

        A substring test would route any URL merely CONTAINING that text -- in a
        path segment, a query parameter, userinfo -- to the GitHub parser. The
        GitHub parser re-validates the host and rejects it, so this is not an
        SSRF; the failure it prevents is a legitimate GitLab URL carrying that
        text being refused with a GitHub-specific error. Matching the parsed host
        exactly is what keeps that from happening.
        """
        with mock.patch.object(
            gitlab_client, "allowed_hosts", return_value=frozenset({"gitlab.example"})
        ):
            key = provider.parse_repo_url("https://gitlab.example/g/p?u=://github.com/o/r")
        # Routed by its real host, so it parses as the GitLab project it is.
        self.assertEqual(key.provider, "gitlab")
        self.assertEqual(key.host, "gitlab.example")

    def test_a_host_that_merely_ends_with_github_com_is_not_github(self):
        # notgithub.com must not be treated as github.com.
        with mock.patch.object(gitlab_client, "allowed_hosts", return_value=frozenset()):
            with self.assertRaises(RepoUrlError):
                provider.parse_repo_url("https://notgithub.com/o/r")

    def test_bad_github_url_reports_a_github_error(self):
        # Not a confusing "not a GitLab host" message.
        with self.assertRaises(RepoUrlError) as ctx:
            provider.parse_repo_url("https://github.com/only-one")
        self.assertIn("github", str(ctx.exception).lower())


class TestHostAuthorizationAtSpawn(unittest.TestCase):
    """The host is re-checked on EVERY call, not only at connect time."""

    def test_empty_host_is_refused_not_defaulted(self):
        # A call site that forgot the host must fail loudly: silently defaulting
        # to gitlab.com could read -- or mutate -- an allowlisted private
        # project's path on the PUBLIC instance.
        with self.assertRaises(ProviderCliError):
            gitlab_client._resolve_host("")

    def test_unlisted_host_refused_even_after_connect(self):
        # Removing a host from the allowlist takes effect immediately.
        with mock.patch.object(gitlab_client, "allowed_hosts", return_value=frozenset()):
            with self.assertRaises(ProviderCliError):
                gitlab_client._resolve_host("gitlab.acme.internal")

    def test_gitlab_com_always_allowed(self):
        with mock.patch.object(gitlab_client, "allowed_hosts", return_value=frozenset()):
            self.assertEqual(gitlab_client._resolve_host("gitlab.com"), "gitlab.com")
            self.assertEqual(gitlab_client._resolve_host("www.gitlab.com"), "gitlab.com")

    def test_broken_config_fails_closed(self):
        # An unreadable config must deny every self-managed host, never widen.
        with mock.patch(
            "kiro_crew.config.loader.KiroCrewConfig.load", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(gitlab_client.allowed_hosts(), frozenset())

    def test_token_withheld_from_self_managed_host(self):
        # GITLAB_TOKEN is a gitlab.com credential with no host binding; sending
        # it to a private instance would hand over every permission it carries.
        with mock.patch.dict(
            "os.environ", {"GITLAB_TOKEN": "glpat-secret"}, clear=False
        ), mock.patch(
            "kiro_crew.apps.registry.minimal_env", side_effect=lambda **kw: dict(kw)
        ):
            self_managed = gitlab_client._glab_env("gitlab.acme.internal")
            public = gitlab_client._glab_env("gitlab.com")
        self.assertNotIn("GITLAB_TOKEN", self_managed)
        self.assertEqual(self_managed["GITLAB_HOST"], "gitlab.acme.internal")
        self.assertEqual(public.get("GITLAB_TOKEN"), "glpat-secret")

    def test_env_excludes_unrelated_secrets(self):
        with mock.patch.dict(
            "os.environ", {"AWS_SECRET_ACCESS_KEY": "nope", "SLACK_BOT_TOKEN": "nope"}, clear=False
        ), mock.patch(
            "kiro_crew.apps.registry.minimal_env", side_effect=lambda **kw: dict(kw)
        ):
            env = gitlab_client._glab_env("gitlab.com")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("SLACK_BOT_TOKEN", env)


class TestStorageIsolation(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ir-gitlab-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_public_github_keeps_the_legacy_layout(self):
        # No migration: an existing install's data must not move.
        self.assertEqual(store.provider_root(root=self.root), store.data_dir(self.root))
        self.assertEqual(
            store.repo_data_dir("o", "r", store.provider_root(root=self.root)),
            self.root / "repos" / "o" / "r",
        )

    def test_gitlab_gets_its_own_subtree(self):
        scope = store.provider_root(root=self.root, provider="gitlab", host="gitlab.com")
        self.assertEqual(scope, self.root / "@providers" / "gitlab" / "gitlab.com")

    def test_same_slug_on_two_providers_does_not_collide(self):
        gh_dir = store.repo_data_dir("acme", "widget", store.provider_root(root=self.root))
        gl_dir = store.repo_data_dir(
            "acme",
            "widget",
            store.provider_root(root=self.root, provider="gitlab", host="gitlab.com"),
        )
        self.assertNotEqual(gh_dir, gl_dir)

    def test_same_project_on_two_gitlab_hosts_does_not_collide(self):
        # `group/project` names a DIFFERENT project on each instance.
        a = store.repo_data_dir(
            "g", "p", store.provider_root(root=self.root, provider="gitlab", host="gitlab.com")
        )
        b = store.repo_data_dir(
            "g",
            "p",
            store.provider_root(root=self.root, provider="gitlab", host="gitlab.acme.internal"),
        )
        self.assertNotEqual(a, b)

    def test_host_port_is_filesystem_safe(self):
        scope = store.provider_root(
            root=self.root, provider="gitlab", host="gitlab.acme.internal:8443"
        )
        self.assertNotIn(":", str(scope.name))

    def test_caches_written_under_one_provider_are_invisible_to_the_other(self):
        gl_scope = store.provider_root(root=self.root, provider="gitlab", host="gitlab.com")
        store.write_issues_cache("acme", "widget", [{"number": 1}], root=gl_scope)
        self.assertIsNotNone(store.read_issues_cache("acme", "widget", gl_scope))
        # The GitHub tree must not see it.
        self.assertIsNone(
            store.read_issues_cache("acme", "widget", store.provider_root(root=self.root))
        )

    def test_disconnect_only_removes_its_own_provider_subtree(self):
        gh_scope = store.provider_root(root=self.root)
        gl_scope = store.provider_root(root=self.root, provider="gitlab", host="gitlab.com")
        store.add_connected_repo("acme", "widget", root=self.root)
        store.add_connected_repo(
            "acme", "widget", provider="gitlab", host="gitlab.com", root=self.root
        )
        store.write_issues_cache("acme", "widget", [{"number": 1}], root=gh_scope)
        store.write_issues_cache("acme", "widget", [{"number": 2}], root=gl_scope)

        self.assertTrue(
            store.remove_connected_repo(
                "acme", "widget", provider="gitlab", host="gitlab.com", root=self.root
            )
        )
        # The GitHub repo's data survives.
        self.assertIsNotNone(store.read_issues_cache("acme", "widget", gh_scope))
        self.assertTrue(store.is_repo_connected("acme", "widget", self.root))


class TestConnectedIdentity(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ir-ident-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_gate_does_not_authorize_across_providers(self):
        store.add_connected_repo("g", "p", provider="gitlab", host="gitlab.com", root=self.root)
        self.assertTrue(
            store.is_repo_connected("g", "p", self.root, provider="gitlab", host="gitlab.com")
        )
        # Same owner/repo, different provider -> NOT connected.
        self.assertFalse(store.is_repo_connected("g", "p", self.root))

    def test_gate_does_not_authorize_across_hosts(self):
        store.add_connected_repo(
            "g", "p", provider="gitlab", host="gitlab.acme.internal", root=self.root
        )
        self.assertFalse(
            store.is_repo_connected("g", "p", self.root, provider="gitlab", host="gitlab.com")
        )

    def test_legacy_entry_without_provider_reads_as_github(self):
        # Repos connected before GitLab support must keep working.
        store.write_config({"repos": [{"owner": "o", "repo": "r", "enabled": True}]}, self.root)
        self.assertTrue(store.is_repo_connected("o", "r", self.root))
        entry = store.find_connected_repo("o", "r", self.root)
        assert entry is not None
        self.assertEqual(entry["provider"], "github")
        self.assertEqual(entry["host"], "github.com")

    def test_settings_are_scoped_per_provider(self):
        store.add_connected_repo("g", "p", root=self.root)
        store.add_connected_repo("g", "p", provider="gitlab", host="gitlab.com", root=self.root)
        store.write_repo_settings(
            "g", "p", {"triage_labels": ["gh-triage"]}, expected_revision=0, root=self.root
        )
        gl = store.read_repo_settings("g", "p", self.root, provider="gitlab", host="gitlab.com")
        self.assertEqual(gl["triage_labels"], [])

    def test_gate_rejects_case_variant_gitlab_project(self):
        # GitLab project paths are case-sensitive: group/Project and
        # group/project are DIFFERENT projects. The gate must not authorize a
        # request for a case-variant the owner never connected -- otherwise the
        # raw-case data-plane (project_path + cache dir) would resolve it to a
        # different project under the owner's credentials.
        store.add_connected_repo(
            "group", "project", provider="gitlab", host="gitlab.com", root=self.root
        )
        self.assertTrue(
            store.is_repo_connected(
                "group", "project", self.root, provider="gitlab", host="gitlab.com"
            )
        )
        for owner, repo in (("group", "Project"), ("Group", "project"), ("GROUP", "PROJECT")):
            self.assertFalse(
                store.is_repo_connected(
                    owner, repo, self.root, provider="gitlab", host="gitlab.com"
                ),
                f"case-variant {owner}/{repo} must NOT be authorized for GitLab",
            )
            self.assertIsNone(
                store.find_connected_repo(
                    owner, repo, self.root, provider="gitlab", host="gitlab.com"
                )
            )

    def test_gate_still_case_insensitive_for_github(self):
        # GitHub names are case-preserving but not case-sensitive, so a
        # case-variant of a connected GitHub repo MUST still authorize.
        store.add_connected_repo("Acme", "Widget", root=self.root)
        for owner, repo in (("acme", "widget"), ("ACME", "WIDGET"), ("Acme", "Widget")):
            self.assertTrue(
                store.is_repo_connected(owner, repo, self.root),
                f"case-variant {owner}/{repo} must remain authorized for GitHub",
            )

    def test_case_variant_gitlab_projects_stored_as_distinct_entries(self):
        # Because GitLab is case-sensitive, connecting group/Project after
        # group/project yields two entries, not a dedup collision.
        store.add_connected_repo(
            "group", "project", provider="gitlab", host="gitlab.com", root=self.root
        )
        store.add_connected_repo(
            "group", "Project", provider="gitlab", host="gitlab.com", root=self.root
        )
        gitlab_entries = [
            r
            for r in store.list_connected_repos(self.root)
            if r.get("provider") == "gitlab"
        ]
        self.assertEqual(len(gitlab_entries), 2)


class TestNormalization(unittest.TestCase):
    def test_issue_row_matches_the_github_shape(self):
        row = gitlab_client._norm_issue(
            {
                "iid": 42,
                "title": "Bug",
                "web_url": "https://gitlab.com/g/p/-/issues/42",
                "labels": ["bug", "triage"],
                "user_notes_count": 3,
                "upvotes": 5,
                "downvotes": 1,
                "state": "opened",
                "author": {"username": "alice"},
                "assignees": [{"username": "bob"}],
                "description": "body",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
            }
        )
        self.assertEqual(row["number"], 42)
        self.assertEqual(row["state"], "open")  # "opened" -> "open"
        self.assertEqual(row["comments"], 3)
        self.assertEqual(row["thumbs_up"], 5)
        self.assertEqual(row["reactions"], 6)
        self.assertEqual(row["author"], "alice")
        self.assertEqual(row["assignees"], ["bob"])
        self.assertEqual(row["body"], "body")
        self.assertIsNone(row["author_association"])
        # The row must carry exactly the keys the GitHub list view produces.
        self.assertEqual(set(row), {
            "number", "title", "url", "labels", "comments", "reactions", "thumbs_up",
            "author_association", "updated_at", "created_at", "state", "author",
            "assignees", "body",
        })

    def test_locked_state_folds_to_open(self):
        self.assertEqual(gitlab_client._norm_state("locked"), "open")

    def test_label_colour_loses_the_hash(self):
        shaped = gitlab_client._shape_labels([{"name": "bug", "color": "#d9534f"}])
        self.assertEqual(shaped[0]["color"], "d9534f")

    def test_merge_request_folds_merged_into_closed_but_keeps_merged_at(self):
        row = gitlab_client._norm_pull(
            {"iid": 7, "state": "merged", "merged_at": "2026-01-03T00:00:00Z",
             "source_branch": "feat", "target_branch": "main", "draft": False}
        )
        self.assertEqual(row["state"], "closed")
        self.assertEqual(row["merged_at"], "2026-01-03T00:00:00Z")
        self.assertEqual(row["head"], "feat")
        self.assertEqual(row["base"], "main")

    def test_pipeline_summary_reports_unknown_diff_not_zero(self):
        # Zeros would present an unread diff as a confident "no changes" and be
        # persisted to the list cache.
        row = gitlab_client._norm_pull({"iid": 1, "state": "opened", "head_pipeline": {"status": "failed"}})
        self.assertIsNone(row["additions"])
        self.assertIsNone(row["changed_files"])
        self.assertEqual(row["checks_state"], "failure")
        self.assertEqual(row["checks_counts"]["failure"], 1)

    def test_enrichment_complete_true_for_normalized_rows(self):
        rows = [gitlab_client._norm_pull({"iid": 1, "state": "opened"})]
        self.assertTrue(gitlab_client.enrichment_complete(rows))

    def test_allow_failure_job_is_not_a_failing_check(self):
        # GitLab does not fail the pipeline for it, so neither may the card.
        self.assertEqual(gitlab_client._job_bucket("failed", True), "other")
        self.assertEqual(gitlab_client._job_bucket("failed", False), "failure")

    def test_cancelled_job_is_informational(self):
        # Same hard-won rule as github_client: a cancelled run is not a failure.
        for status in ("canceled", "cancelled", "skipped", "manual"):
            with self.subTest(status=status):
                self.assertEqual(gitlab_client._job_bucket(status, False), "other")

    def test_summarize_checks_matches_github_contract(self):
        checks = [{"bucket": "success"}, {"bucket": "failure"}, {"bucket": "running"}]
        summary = gitlab_client.summarize_checks(checks)
        self.assertEqual(set(summary), {"checks_counts", "checks_state", "checks_truncated"})
        # Failure dominates, so the dot never reads greener than the list.
        self.assertEqual(summary["checks_state"], "failure")
        self.assertIsNone(gitlab_client.summarize_checks([])["checks_state"])

    def test_access_level_maps_to_permissions(self):
        # Reporter gets triage but NOT push, matching what GitLab permits.
        reporter = gitlab_client._permissions_for_access_level(20)
        self.assertTrue(reporter["triage"])
        self.assertFalse(reporter["push"])
        developer = gitlab_client._permissions_for_access_level(30)
        self.assertTrue(developer["push"])
        self.assertFalse(developer["maintain"])

    def test_effective_access_level_takes_the_higher_of_project_and_group(self):
        level = gitlab_client._access_level(
            {"permissions": {"project_access": {"access_level": 20},
                             "group_access": {"access_level": 40}}}
        )
        self.assertEqual(level, 40)

    def test_internal_visibility_counts_as_private(self):
        with mock.patch.object(
            gitlab_client, "_glab_api",
            return_value={"path_with_namespace": "g/p", "visibility": "internal",
                          "open_issues_count": 2, "permissions": {}},
        ):
            summary = gitlab_client.verify_repo_access("g", "p", host="gitlab.com")
        self.assertTrue(summary["private"])

    def test_derive_members_is_empty_rather_than_inventing_a_roster(self):
        # GitLab issue authors may be strangers; badging them as members would
        # be wrong, so the fallback reports nothing instead.
        self.assertEqual(
            gitlab_client.derive_members([{"author": "alice", "author_association": "MEMBER"}]), []
        )

    def test_system_note_becomes_a_typed_event(self):
        event = gitlab_client._norm_note(
            {"system": True, "body": "assigned to @bob", "created_at": "t",
             "author": {"username": "alice"}}
        )
        assert event is not None
        self.assertEqual(event["kind"], "assigned")
        self.assertEqual(event["assignee"], "bob")

    def test_unrecognized_system_note_is_dropped(self):
        self.assertIsNone(
            gitlab_client._norm_note({"system": True, "body": "did something obscure"})
        )

    def test_merge_request_mention_is_flagged_as_a_change_request(self):
        event = gitlab_client._norm_note(
            {"system": True, "body": "mentioned in merge request !12", "created_at": "t"}
        )
        assert event is not None
        self.assertTrue(event["source"]["is_pr"])
        self.assertEqual(event["source"]["number"], 12)

    def test_pending_merge_status_is_unknown_not_conflicted(self):
        # A false conflict warning while GitLab is still computing would be worse
        # than showing nothing.
        self.assertIsNone(gitlab_client._mergeable({"detailed_merge_status": "checking"}))
        self.assertTrue(gitlab_client._mergeable({"detailed_merge_status": "mergeable"}))
        self.assertFalse(gitlab_client._mergeable({"detailed_merge_status": "conflict"}))

    def test_list_pr_checks_rejects_a_bogus_sha(self):
        with self.assertRaises(ProviderCliError):
            gitlab_client.list_pr_checks("g", "p", "not-a-sha", host="gitlab.com")


class TestOpenListProbe(unittest.TestCase):
    """The cheap poll probe.

    ``probe_open_list`` gates list polling. A GitHub-only implementation would
    mean a GitLab project probing GitHub, so the contract is pinned on both
    sides here.
    """

    def test_issue_probe_uses_the_exact_open_count(self):
        calls: list[str] = []

        def fake_api(path, **kwargs):
            calls.append(path)
            if "issues_statistics" in path:
                return {"statistics": {"counts": {"opened": 7, "closed": 2, "all": 9}}}
            return [{"iid": 42, "updated_at": "2026-01-02T00:00:00Z"}]

        with mock.patch.object(gitlab_client, "_glab_api", side_effect=fake_api):
            probe = gitlab_client.probe_open_list("g", "p", "issue", host="gitlab.com")
        self.assertEqual(probe, {"total_count": 7, "top_updated_at": "2026-01-02T00:00:00Z"})
        # Scoped to the project, and asks only for the open set.
        self.assertTrue(any("issues_statistics" in c for c in calls))
        self.assertTrue(any("state=opened" in c for c in calls))

    def test_probe_shape_matches_github(self):
        with mock.patch.object(
            gitlab_client,
            "_glab_api",
            side_effect=lambda path, **kw: (
                {"statistics": {"counts": {"opened": 1}}} if "statistics" in path else []
            ),
        ):
            probe = gitlab_client.probe_open_list("g", "p", "issue", host="gitlab.com")
        # The value is compared against a stored probe, so the KEYS must match
        # github_client's exactly or every poll would read as "changed".
        self.assertEqual(set(probe), {"total_count", "top_updated_at"})
        self.assertIsNone(probe["top_updated_at"])

    def test_merge_request_probe_is_refused_not_approximated(self):
        # GitLab exposes no cheap open-MR count without response headers. Guessing
        # would let a non-top MR close unnoticed and the cache be served as
        # verified-fresh; raising takes the caller's documented
        # probe-unavailable path instead, bounded by the staleness ceiling.
        with self.assertRaises(ProviderCliError):
            gitlab_client.probe_open_list("g", "p", "pr", host="gitlab.com")

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ProviderCliError):
            gitlab_client.probe_open_list("g", "p", "comment", host="gitlab.com")

    def test_missing_count_raises_rather_than_reporting_zero(self):
        # A zero would read as "no open issues" and could be compared equal to a
        # later real zero, hiding a change.
        with mock.patch.object(gitlab_client, "_glab_api", return_value={"statistics": {}}):
            with self.assertRaises(ProviderCliError):
                gitlab_client.probe_open_list("g", "p", "issue", host="gitlab.com")


class TestRefSummary(unittest.TestCase):
    """The cross-reference hover/sheet summary.

    ``get_ref_summary`` (and the ``/ref`` route) backs the in-app reference UI. A
    GitHub-only implementation would mean a GitLab project's references resolving
    against GitHub, so the contract is pinned on both sides.
    """

    RAW = {
        "iid": 5,
        "title": "Widget crashes on save",
        "state": "opened",
        "web_url": "https://gitlab.com/g/p/-/issues/5",
        "author": {"username": "amy"},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "closed_at": None,
        "user_notes_count": 3,
        "labels": ["bug"],
    }

    def test_summary_keys_match_github(self):
        # The frontend reads one shape for both providers; a missing key renders as
        # an empty hover card rather than an error anyone would notice.
        with mock.patch.object(gitlab_client, "_glab_api", side_effect=[
            dict(self.RAW), [{"name": "bug", "color": "#d73a4a"}],
        ]):
            summary = gitlab_client.get_ref_summary("g", "p", 5, host="gitlab.com")
        self.assertEqual(
            set(summary),
            {
                "number", "title", "state", "state_reason", "url", "author",
                "author_association", "created_at", "updated_at", "closed_at",
                "comments", "is_pr", "draft", "merged_at", "labels",
            },
        )
        self.assertEqual(summary["number"], 5)
        self.assertEqual(summary["state"], "open")
        self.assertEqual(summary["author"], "amy")
        # Colour arrives with GitLab's leading '#' stripped, as GitHub reports it.
        self.assertEqual(summary["labels"], [{"name": "bug", "color": "d73a4a"}])

    def test_reference_is_always_an_issue_on_gitlab(self):
        # GitLab keeps separate iid sequences: '#5' is issue 5 and '!5' is merge
        # request 5, which are unrelated items. Reporting is_pr=True for a
        # reference would open the MR pane on a number that means an issue.
        with mock.patch.object(gitlab_client, "_glab_api", side_effect=[dict(self.RAW, labels=[])]):
            summary = gitlab_client.get_ref_summary("g", "p", 5, host="gitlab.com")
        self.assertIs(summary["is_pr"], False)
        self.assertIsNone(summary["merged_at"])

    def test_missing_issue_raises_instead_of_falling_back_to_a_merge_request(self):
        with mock.patch.object(gitlab_client, "_glab_api", return_value={}):
            with self.assertRaises(ProviderCliError):
                gitlab_client.get_ref_summary("g", "p", 5, host="gitlab.com")

    def test_labels_are_not_fetched_when_there_are_none(self):
        # One request for a hover, not two: the label set is only needed to colour
        # labels that exist.
        calls: list[str] = []

        def fake_api(path, **kwargs):
            calls.append(path)
            return dict(self.RAW, labels=[])

        with mock.patch.object(gitlab_client, "_glab_api", side_effect=fake_api):
            gitlab_client.get_ref_summary("g", "p", 5, host="gitlab.com")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("labels", calls[0])

    def test_non_numeric_number_is_rejected_before_the_api(self):
        # ``int(number)`` is what keeps a number from becoming a path segment; a
        # non-numeric value must fail loudly rather than reach the argv.
        with mock.patch.object(gitlab_client, "_glab_api") as api:
            with self.assertRaises(ValueError):
                gitlab_client.get_ref_summary("g", "p", "5/../../admin", host="gitlab.com")  # type: ignore[arg-type]
        api.assert_not_called()


class TestMrTimelineNotesFetch(unittest.TestCase):
    """The MR timeline reads the notes endpoint ONCE.

    GitLab keeps inline (diff) comments in the same notes stream, so the MR
    timeline both assembles the notes AND promotes the positioned ones. It used to
    fetch ``{base}/notes`` twice per PR-detail load; ``_assemble_timeline`` now
    returns the notes it fetched so the promotion reuses that one read."""

    def test_notes_endpoint_is_hit_once(self):
        note = {
            "id": 1, "system": False, "body": "inline!", "created_at": "2024-01-01T00:00:00Z",
            "author": {"username": "alice"},
            "position": {"new_path": "a.py", "new_line": 3},
        }
        calls: list[str] = []

        def fake_api(path, **kwargs):
            calls.append(path)
            # The notes path carries a query string (?order_by=…&sort=asc), so match
            # on the endpoint before the query rather than the whole path.
            if path.split("?")[0].endswith("/notes"):
                return [note]
            # resource_label_events / resource_state_events — empty is fine.
            return []

        with mock.patch.object(gitlab_client, "_glab_api", side_effect=fake_api), \
                mock.patch.object(gitlab_client, "list_repo_labels", return_value=[]):
            events = gitlab_client.list_pr_timeline("g", "p", 7, host="gitlab.com")

        notes_calls = [p for p in calls if p.split("?")[0].endswith("/notes")]
        self.assertEqual(len(notes_calls), 1, f"notes fetched {len(notes_calls)}x: {calls}")
        # The positioned note is promoted to a single inline review_comment and the
        # plain-comment copy of it is dropped, so it shows once.
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds.count("review_comment"), 1)
        self.assertNotIn("comment", kinds)


class TestInvestigationNamespace(unittest.TestCase):
    """Investigation records must not collide across GitLab's two sequences.

    GitHub draws issues and pull requests from ONE number sequence, so every
    existing record is correctly keyed by number alone. GitLab numbers them
    independently: issue ``#5`` and merge request ``!5`` are unrelated items, and a
    shared record would make "Review MR !5" resume issue #5's chat session and
    overwrite its findings.
    """

    def test_github_keeps_the_historical_filename_for_both_kinds(self):
        # Nothing to migrate: on GitHub the namespace is genuinely shared, so a
        # pull request must still resolve to the file its record already lives in.
        gh = provider.key_from_parts("acme", "widget", "github", "github.com")
        self.assertEqual(provider.investigation_kind(gh, "issue"), "issue")
        self.assertEqual(provider.investigation_kind(gh, "pull"), "issue")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                store.investigation_path("acme", "widget", 5, root, kind="issue").name,
                "investigation-5.json",
            )

    def test_gitlab_issue_and_merge_request_get_separate_records(self):
        gl = provider.key_from_parts("group", "project", "gitlab", "gitlab.com")
        issue_kind = provider.investigation_kind(gl, "issue")
        mr_kind = provider.investigation_kind(gl, "pull")
        self.assertEqual((issue_kind, mr_kind), ("issue", "mr"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store.write_investigation(
                "group", "project", 5, {"slot_key": "issue-slot"}, root=root, kind=issue_kind
            )
            store.write_investigation(
                "group", "project", 5, {"slot_key": "mr-slot"}, root=root, kind=mr_kind
            )
            issue_record = store.read_investigation(
                "group", "project", 5, root, kind=issue_kind
            )
            mr_record = store.read_investigation("group", "project", 5, root, kind=mr_kind)

        # The MR write must not have reached the issue's session link.
        assert issue_record is not None and mr_record is not None
        self.assertEqual(issue_record["slot_key"], "issue-slot")
        self.assertEqual(mr_record["slot_key"], "mr-slot")

    def test_the_issue_record_keeps_the_legacy_path_on_gitlab_too(self):
        # Only the namespace that has never been written to changes, so a GitLab
        # issue record written before this fix is still found.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = store.investigation_path("group", "project", 5, root)
            self.assertEqual(
                legacy, store.investigation_path("group", "project", 5, root, kind="issue")
            )
            self.assertNotEqual(
                legacy, store.investigation_path("group", "project", 5, root, kind="mr")
            )


class TestHostIsRequiredNotDefaulted(unittest.TestCase):
    """A GitLab key with NO host must stay empty, not become gitlab.com.

    Defaulting it silently retargets the request: with a same-slug gitlab.com
    project connected, a call that omitted ``host`` would pass the connected-repo
    gate against THAT project and let a write land on a repository the caller
    never named. ``gitlab_client._resolve_host`` documents that an omitted host is
    refused -- these tests are what make that true end to end.
    """

    def test_missing_gitlab_host_stays_empty(self):
        key = provider.key_from_parts("group", "project", "gitlab", None)
        self.assertEqual(key.host, "")
        self.assertEqual(provider.key_from_parts("g", "p", "gitlab", "  ").host, "")

    def test_empty_host_matches_no_connected_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store.add_connected_repo(
                "group", "project", root=root, provider="gitlab", host="gitlab.com"
            )
            # The gitlab.com project IS connected, but a request that named no host
            # must not be authorized against it.
            self.assertFalse(
                store.is_repo_connected(
                    "group", "project", root, provider="gitlab", host=""
                )
            )
            self.assertTrue(
                store.is_repo_connected(
                    "group", "project", root, provider="gitlab", host="gitlab.com"
                )
            )

    def test_empty_host_is_refused_at_the_spawn_boundary(self):
        with self.assertRaises(ProviderCliError):
            gitlab_client._resolve_host("")

    def test_github_host_is_still_pinned(self):
        # GitHub Enterprise is unsupported, so its host is not client-controlled.
        self.assertEqual(provider.key_from_parts("o", "r", "github", None).host, "github.com")
        self.assertEqual(provider.key_from_parts("o", "r", "github", "evil.test").host, "github.com")


class TestListPagination(unittest.TestCase):
    """Single-page listings must ask for a full page.

    GitLab defaults to 20 rows; github_client's equivalent path asks for 100. The
    closed lists are deliberately one page, so the default would silently make
    them a fifth of the promised size -- older items simply absent, with no error.
    """

    def _path_for(self, fn) -> str:
        seen: list[str] = []

        def fake_api(path, **kwargs):
            seen.append(path)
            return []

        with mock.patch.object(gitlab_client, "_glab_api", side_effect=fake_api):
            fn()
        return seen[0]

    def test_closed_issue_list_asks_for_a_full_page(self):
        path = self._path_for(
            lambda: gitlab_client.list_closed_issues("g", "p", host="gitlab.com")
        )
        self.assertIn("per_page=100", path)

    def test_closed_merge_request_list_asks_for_a_full_page(self):
        path = self._path_for(
            lambda: gitlab_client.list_closed_pulls("g", "p", host="gitlab.com")
        )
        self.assertIn("per_page=100", path)

    def test_paginated_lists_do_not_duplicate_per_page(self):
        # The paginator adds its own ``per_page``; a second one in the path would
        # be a duplicate query parameter whose winner is not ours to decide.
        for fn in (gitlab_client.list_open_issues, gitlab_client.list_open_pulls):
            with self.subTest(fn=fn.__name__):
                path = self._path_for(lambda: fn("g", "p", host="gitlab.com"))
                self.assertNotIn("per_page", path)


class TestMergeRequestSearchState(unittest.TestCase):
    def test_closed_search_asks_gitlab_for_closed(self):
        """A closed MR search must filter SERVER-side, not after the cap.

        Asking for ``all`` and dropping merged rows afterwards looks equivalent but
        is not: the cap applies to the fetched rows, so enough newer MERGED MRs can
        fill it and hide every genuinely closed match. Filtering after a cap can
        only lose rows it never saw.
        """
        query = gitlab_client.build_pr_search_query("g", "p", state="closed", author="amy")
        self.assertIn("state=closed", query)
        self.assertNotIn("state=all", query)

    def test_search_keeps_the_callers_sentinel_row(self):
        # The route asks for PR_SEARCH_MAX + 1 and reports "truncated" when it gets
        # the extra row. Clamping to the display cap would make every over-cap
        # result set claim to be complete.
        rows = [
            {"iid": n, "state": "opened", "updated_at": "2026-01-01T00:00:00Z"}
            for n in range(gitlab_client.PR_SEARCH_MAX + 5)
        ]
        with mock.patch.object(gitlab_client, "_glab_api", return_value=rows):
            out = gitlab_client.search_pulls(
                "g", "p", host="gitlab.com", author="amy",
                limit=gitlab_client.PR_SEARCH_MAX + 1,
            )
        self.assertEqual(len(out), gitlab_client.PR_SEARCH_MAX + 1)


class TestProviderDispatch(unittest.TestCase):
    def test_client_for_selects_the_right_module(self):
        self.assertIs(provider.client_for(provider.RepoKey(provider="github")), github_client)
        self.assertIs(provider.client_for(provider.RepoKey(provider="gitlab")), gitlab_client)

    def test_unknown_provider_degrades_to_github(self):
        # Cannot leak: gh is pinned to github.com, so a corrupted config entry
        # becomes a failed GitHub lookup, not a call to an unintended server.
        self.assertIs(provider.client_for(provider.RepoKey(provider="bogus")), github_client)

    def test_github_key_host_cannot_be_overridden(self):
        # GitHub Enterprise is unsupported; a crafted host must not become part
        # of a cache path or of the connected-repo identity.
        key = provider.key_from_parts("o", "r", "github", "evil.test")
        self.assertEqual(key.host, "github.com")

    def test_unknown_provider_normalizes_to_github(self):
        self.assertEqual(provider.normalize_provider("bitbucket"), "github")
        self.assertEqual(provider.normalize_provider(None), "github")

    def test_call_kwargs_requires_host_only_for_gitlab(self):
        self.assertEqual(provider.call_kwargs(provider.RepoKey(provider="github")), {})
        self.assertEqual(
            provider.call_kwargs(provider.RepoKey(provider="gitlab", host="gitlab.com")),
            {"host": "gitlab.com"},
        )

    def test_terms_say_merge_request_for_gitlab(self):
        self.assertEqual(
            provider.terms(provider.RepoKey(provider="gitlab"))["change_request"], "merge request"
        )
        self.assertEqual(
            provider.terms(provider.RepoKey(provider="github"))["change_request"], "pull request"
        )

    def test_web_url_uses_the_key_host(self):
        key = provider.key_from_parts("g/sub", "p", "gitlab", "gitlab.acme.internal")
        self.assertEqual(key.web_url(), "https://gitlab.acme.internal/g/sub/p")


class TestClientParity(unittest.TestCase):
    """Both client modules must expose the same surface.

    A module cannot be statically checked against ``provider.ProviderClient``, so
    this is the gate that makes the dispatch safe: if a route calls a function
    that only GitHub implements, or the two disagree about argument order, it
    fails here rather than at runtime for a GitLab user.
    """

    # Every function the routes reach through the dispatch.
    SURFACE = (
        "verify_repo_access", "get_repo_permissions", "list_open_issues",
        "list_open_issues_first_page", "list_closed_issues",
        "list_recent_open_issues", "list_repo_labels", "list_repo_collaborators",
        "derive_members", "get_current_login", "list_contributed_repos", "get_issue_detail",
        "list_issue_timeline", "list_pr_timeline", "list_open_pulls",
        "list_open_pulls_first_page", "list_closed_pulls",
        "get_pr_detail", "list_pr_checks", "summarize_checks", "enrich_pulls",
        "enrich_pulls_by_number", "enrichment_complete", "search_pulls", "add_issue_labels",
        "remove_issue_label", "set_issue_state", "create_label", "probe_open_list",
        "build_pr_search_query", "get_ref_summary",
        # Pull-request actions.
        "set_pr_state", "submit_pr_review", "add_issue_comment", "add_pr_comment",
        "merge_pull_request", "enable_auto_merge", "disable_auto_merge",
        "list_pr_workflow_runs",
        "cancel_workflow_run", "rerun_workflow_run",
    )

    def test_both_modules_implement_the_whole_surface(self):
        for name in self.SURFACE:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(github_client, name, None)), f"github: {name}")
                self.assertTrue(callable(getattr(gitlab_client, name, None)), f"gitlab: {name}")

    def test_positional_parameters_agree(self):
        """The POSITIONAL parameters must match name-for-name and in order.

        Keyword-only parameters are allowed to differ -- that is exactly where
        ``host`` lives for GitLab and where each client's own timeouts live -- but
        a positional disagreement would mean a route silently passing the wrong
        argument (e.g. an issue number where a SHA is expected).
        """
        for name in self.SURFACE:
            with self.subTest(name=name):
                gh_params = [
                    p.name
                    for p in inspect.signature(getattr(github_client, name)).parameters.values()
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                ]
                gl_params = [
                    p.name
                    for p in inspect.signature(getattr(gitlab_client, name)).parameters.values()
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                ]
                self.assertEqual(gh_params, gl_params, f"{name} positional params differ")

    # Keyword-only parameters that are legitimately provider-local: the target
    # host (GitLab needs it, GitHub is pinned to github.com) and each client's own
    # timeout budget. EVERY other keyword-only parameter is caller-supplied and
    # must match, because the routes pass the same kwargs to whichever client they
    # hold.
    PROVIDER_LOCAL_KWARGS = frozenset({"host", "timeout"})

    def test_caller_supplied_keyword_arguments_agree(self):
        """The kwargs a route passes must exist on BOTH clients.

        Positional parity alone is not enough: if ``search_pulls`` is reached with
        ``assignee=`` / ``review_requested=`` / ``limit=`` that only the GitHub
        client accepts, the merge-request person filter raises a ``TypeError`` and
        surfaces as an unhandled 500 for every GitLab project.
        A keyword mismatch cannot be caught by types here (the dispatch is a
        module cast), so it is caught here instead.
        """
        for name in self.SURFACE:
            with self.subTest(name=name):
                def kwargs(mod):
                    return {
                        p.name
                        for p in inspect.signature(getattr(mod, name)).parameters.values()
                        if p.kind == p.KEYWORD_ONLY
                    } - self.PROVIDER_LOCAL_KWARGS

                gh_kw, gl_kw = kwargs(github_client), kwargs(gitlab_client)
                self.assertEqual(
                    gh_kw,
                    gl_kw,
                    f"{name}: GitHub-only kwargs {sorted(gh_kw - gl_kw)}, "
                    f"GitLab-only kwargs {sorted(gl_kw - gh_kw)}",
                )

    def test_both_raise_the_same_exception_classes(self):
        # Aliases, not parallel hierarchies: otherwise routes.py's
        # `except GhCliError` would miss every GitLab failure and return a 500.
        self.assertIs(github_client.GhCliError, gitlab_client.GhCliError)
        self.assertIs(github_client.GhSetupError, gitlab_client.GhSetupError)
        self.assertIs(github_client.GhPermissionError, gitlab_client.GhPermissionError)
        self.assertIs(github_client.RepoUrlError, gitlab_client.RepoUrlError)


class TestConnectParsesOffTheLoop(unittest.TestCase):
    def test_connect_threads_the_url_parse(self):
        """``/connect`` must not parse a repo URL on the event loop.

        On any non-github.com URL, ``provider.parse_repo_url`` consults the
        operator's ``dashboard.gitlab_hosts`` allowlist, and that read is
        ``KiroCrewConfig.load()`` -- synchronous file I/O plus validation. Cheap per
        call, but it runs on the gateway's single event loop, so it stalls every
        other session while it happens. Asserted on the source because the cost is
        invisible at runtime: the handler still returns the right answer.
        """
        source = Path(inspect.getfile(routes)).read_text(encoding="utf-8")
        self.assertIn("asyncio.to_thread(provider.parse_repo_url", source)
        # A bare synchronous call must not reappear.
        self.assertNotRegex(source, r"(?<!to_thread\()\bkey = provider\.parse_repo_url\(")


class TestRoutesScopeEveryStoreCall(unittest.TestCase):
    def test_no_unscoped_per_repo_store_call_survives(self):
        """Per-repo store calls must go through ``routes._st``.

        A plain ``asyncio.to_thread(store.read_issues_cache, ...)`` would read the
        GitHub tree for a GitLab project and silently return another repo's cached
        issues -- a bug with no error and no visible symptom except wrong data. The
        only ``store`` functions allowed to be called directly are the
        config-identity ones, which are keyed by provider+host instead of by root.
        """
        source = Path(inspect.getfile(routes)).read_text(encoding="utf-8")
        allowed = {
            "list_connected_repos", "set_repo_permissions", "remove_connected_repo",
            "read_repo_settings", "write_repo_settings", "add_setting_label",
            "add_connected_repo", "is_repo_connected",
        }
        offenders = [
            name
            for name in re.findall(r"asyncio\.to_thread\(\s*store\.([a-z_]+)", source)
            if name not in allowed
        ]
        self.assertEqual(offenders, [], f"unscoped per-repo store calls: {offenders}")


class TestRefSummaryRoute(unittest.IsolatedAsyncioTestCase):
    """The ``/ref`` route end-to-end through the dispatch.

    This route's store call goes through the scoped ``_st`` helper, which supplies
    ``root=`` itself. Passing ``root`` positionally as well would raise
    ``TypeError`` on EVERY request -- an unconditional 500 no client-side or
    client-module test could see, because nothing exercised the handler.
    """

    async def _call(self, query: str):
        request = make_mocked_request("GET", f"/api/apps/issue-radar/ref?{query}")
        return await routes._handle_ref_summary(request)

    async def test_returns_the_summary_for_a_connected_repo(self):
        summary = {"number": 5, "title": "t", "is_pr": False}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(routes, "_scope", return_value=Path(tmp)), \
                    mock.patch.object(routes, "_connected", return_value=True), \
                    mock.patch.object(
                        github_client, "get_ref_summary", return_value=summary
                    ) as fetch:
                response = await self._call("owner=acme&repo=widget&number=5")
                # Second call is served from the cache the first one wrote, which
                # is the read path that carried the crash.
                cached = await self._call("owner=acme&repo=widget&number=5")

        self.assertEqual(response.status, 200)
        self.assertEqual(cached.status, 200)
        self.assertIn(b'"from_cache": true', cached.body)
        self.assertEqual(fetch.call_count, 1)

    async def test_gitlab_repo_is_dispatched_to_the_gitlab_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(routes, "_scope", return_value=Path(tmp)), \
                    mock.patch.object(routes, "_connected", return_value=True), \
                    mock.patch.object(github_client, "get_ref_summary") as gh, \
                    mock.patch.object(
                        gitlab_client, "get_ref_summary", return_value={"number": 5}
                    ) as gl:
                response = await self._call(
                    "owner=group&repo=project&number=5&provider=gitlab&host=gitlab.com"
                )

        self.assertEqual(response.status, 200)
        gh.assert_not_called()
        # The host must reach the client, not be defaulted inside it.
        self.assertEqual(gl.call_args.kwargs.get("host"), "gitlab.com")

    async def test_unconnected_repo_is_refused(self):
        with mock.patch.object(routes, "_connected", return_value=False):
            response = await self._call("owner=acme&repo=widget&number=5")
        self.assertEqual(response.status, 404)


class TestAccountScope(unittest.TestCase):
    """``/me`` and ``/recent-repos`` are account-scoped, not repo-scoped.

    They ask the provider CLI about the CURRENT USER, so they cannot go through
    ``is_repo_connected``. That makes the key's normalization the only thing
    standing between a client-supplied host and a credential-bearing spawn, so it
    is pinned here.
    """

    def _key(self, query: dict[str, str]):
        qs = "&".join(f"{k}={v}" for k, v in query.items())
        return routes._account_key(make_mocked_request("GET", f"/api/apps/issue-radar/me?{qs}"))

    def test_absent_provider_is_public_github(self):
        # A client that predates GitLab support sends nothing.
        key = self._key({})
        self.assertEqual((key.provider, key.host), ("github", "github.com"))

    def test_gitlab_scope_is_carried(self):
        key = self._key({"provider": "gitlab", "host": "gitlab.acme.internal"})
        self.assertEqual((key.provider, key.host), ("gitlab", "gitlab.acme.internal"))

    def test_github_host_cannot_be_overridden(self):
        # GitHub Enterprise is unsupported; a crafted host must not ride along.
        key = self._key({"provider": "github", "host": "evil.test"})
        self.assertEqual(key.host, "github.com")

    def test_unknown_provider_falls_back_to_github(self):
        key = self._key({"provider": "bitbucket"})
        self.assertEqual(key.provider, "github")

    def test_account_key_carries_no_repo(self):
        # An account scope has no owner/repo — a half-built repo key would invite
        # being passed to a repo-scoped call.
        key = self._key({"provider": "gitlab"})
        self.assertEqual((key.owner, key.repo), ("", ""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
