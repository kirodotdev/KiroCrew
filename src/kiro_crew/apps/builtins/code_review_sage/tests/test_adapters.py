"""Unit tests for the source adapters (GitHub + platform detection)."""
import json
import unittest
from unittest import mock

from sage_lib import adapters as A  # noqa: N812

from tests.fixtures import GITHUB_PAYLOAD

# Injected config: github.com plus two GitHub Enterprise Server hosts (a *.ghe.com
# instance and a fully custom domain).
GHE_CFG = {"github_hosts": ["github.com", "acme.ghe.com", "git.corp.example"]}


class TestPlatformDetection(unittest.TestCase):
    def test_github(self):
        self.assertEqual(A.detect_platform("https://github.com/org/repo/pull/5"), "github")

    def test_crux_link_unsupported(self):
        with self.assertRaises(A.UnsupportedPlatform):
            A.detect_platform("https://code.amazon.com/reviews/CR-12345678")

    def test_unsupported(self):
        with self.assertRaises(A.UnsupportedPlatform):
            A.detect_platform("ftp://example.com/x")

    def test_empty(self):
        with self.assertRaises(A.UnsupportedPlatform):
            A.detect_platform("")


class TestAllowedHosts(unittest.TestCase):
    def test_default_is_github_only(self):
        self.assertEqual(A.allowed_hosts({}),
                         frozenset({"github.com", "www.github.com"}))

    def test_configured_ghe_hosts_are_added(self):
        hosts = A.allowed_hosts(GHE_CFG)
        for h in ("acme.ghe.com", "git.corp.example", "github.com", "www.github.com"):
            self.assertIn(h, hosts)

    def test_www_only_config_round_trips(self):
        # `www.github.com` canonicalizes to `github.com` downstream, so a
        # www-only config must also accept the canonical form — otherwise an
        # accepted link fails to resolve on its second pass.
        self.assertEqual(A.allowed_hosts({"github_hosts": ["www.github.com"]}),
                         frozenset({"github.com", "www.github.com"}))

    def test_config_entries_are_normalized(self):
        # A pasted URL, mixed case, and empties are tolerated; the list REPLACES
        # the default, so an enterprise can lock Sage to its own host.
        hosts = A.allowed_hosts({"github_hosts": ["https://ACME.ghe.com/", "", None]})
        self.assertEqual(hosts, frozenset({"acme.ghe.com"}))

    def test_non_list_config_falls_back_to_default(self):
        self.assertEqual(A.allowed_hosts({"github_hosts": "nonsense"}),
                         frozenset({"github.com", "www.github.com"}))

    def test_refused_config_read_falls_back_to_default(self):
        # A no-follow refusal (planted config.json symlink) surfaces as {} from
        # read_config_quiet: the allowlist must narrow to the github.com
        # default — a refusal can never widen or empty the allowed set.
        with mock.patch.object(A.store, "read_config_quiet", return_value={}):
            self.assertEqual(A.allowed_hosts(),
                             frozenset({"github.com", "www.github.com"}))

    def test_malformed_config_url_entry_is_skipped(self):
        # urlparse raises ValueError on an unmatched '[' — a malformed configured
        # entry must be dropped, not crash host resolution.
        self.assertEqual(A.allowed_hosts({"github_hosts": ["https://[::1",
                                                           "acme.ghe.com"]}),
                         frozenset({"acme.ghe.com"}))


class TestEnterpriseHosts(unittest.TestCase):
    def test_detect_platform_accepts_configured_ghe_pr(self):
        self.assertEqual(
            A.detect_platform("https://acme.ghe.com/org/repo/pull/5", config=GHE_CFG),
            "github")

    def test_detect_platform_accepts_custom_domain(self):
        self.assertEqual(
            A.detect_platform("https://git.corp.example/o/r/pull/1", config=GHE_CFG),
            "github")

    def test_unconfigured_ghe_host_still_rejected(self):
        with self.assertRaises(A.UnsupportedPlatform):
            A.detect_platform("https://acme.ghe.com/org/repo/pull/5", config={})

    def test_github_and_www_still_accepted(self):
        for url in ("https://github.com/o/r/pull/1",
                    "https://www.github.com/o/r/pull/1"):
            self.assertEqual(A.detect_platform(url, config=GHE_CFG), "github")

    def test_allowed_host_in_path_is_rejected(self):
        # The allowlist matches the PARSED hostname, never a substring of the raw
        # link: an allowed host appearing only in the PATH must be refused.
        with self.assertRaises(A.UnsupportedPlatform):
            A.detect_platform("https://evil.example/github.com/x/pull/1",
                              config=GHE_CFG)
        with self.assertRaises(A.AdapterParseError):
            A.github_pr_ref("https://evil.example/github.com/x/y/pull/1",
                            config=GHE_CFG)

    def test_spoofable_host_shapes_are_rejected(self):
        # A host that merely embeds a permitted string must be refused: neither a
        # prefixed spelling nor a permitted-host-as-subdomain may match.
        for url in ("https://notgithub.com/o/r/pull/1",
                    "https://github.com.evil.example/o/r/pull/1",
                    "https://notacme.ghe.com/o/r/pull/1",
                    "https://acme.ghe.com.evil.example/o/r/pull/1"):
            with self.assertRaises(A.UnsupportedPlatform):
                A.detect_platform(url, config=GHE_CFG)
            with self.assertRaises(A.AdapterParseError):
                A.github_pr_ref(url, config=GHE_CFG)

    def test_pr_ref_parses_host(self):
        self.assertEqual(
            A.github_pr_ref("https://acme.ghe.com/org/repo/pull/5", config=GHE_CFG),
            ("acme.ghe.com", "org", "repo", "5"))

    def test_pr_ref_canonicalizes_www_and_tolerates_schemeless(self):
        self.assertEqual(A.github_pr_ref("https://www.github.com/o/r/pull/1")[0],
                         "github.com")
        self.assertEqual(A.github_pr_parts("github.com/o/r/pull/1"), ("o", "r", "1"))

    def test_identity_is_host_qualified_for_ghe(self):
        with mock.patch.object(A.store, "read_config_quiet", return_value=GHE_CFG):
            t = A.normalize(
                "https://acme.ghe.com/org/repo/pull/5",
                {"number": 5, "body": "hello",
                 "html_url": "https://acme.ghe.com/org/repo/pull/5"})
        self.assertEqual(t.repo_identity, "acme.ghe.com/org/repo")
        self.assertEqual(t.change_id, "GH-acme.ghe.com-org-repo-5")
        self.assertEqual(t.url, "https://acme.ghe.com/org/repo/pull/5")

    def test_github_identity_unchanged(self):
        # github.com identities stay byte-identical so already-persisted runs
        # (result files, reviewed.json keys) still resolve.
        t = A.normalize("https://github.com/org/repo/pull/5",
                        {"number": 5, "body": "hello",
                         "html_url": "https://github.com/org/repo/pull/5"})
        self.assertEqual(t.repo_identity, "github.com/org/repo")
        self.assertEqual(t.change_id, "GH-org-repo-5")
        self.assertEqual(A.github_review_key("Org", "Repo", 5),
                         "github.com/org/repo#5")

    def test_review_key_is_host_qualified(self):
        self.assertEqual(A.github_review_key("Org", "Repo", 5, host="acme.ghe.com"),
                         "acme.ghe.com/org/repo#5")
        # www canonicalizes to github.com, so both spellings share one key.
        self.assertEqual(A.github_review_key("o", "r", 1, host="www.github.com"),
                         "github.com/o/r#1")

    def test_change_id_hosts_do_not_collide(self):
        self.assertNotEqual(
            A.github_change_id("o", "r", 1),
            A.github_change_id("o", "r", 1, host="acme.ghe.com"))

    def test_malformed_links_are_rejected_not_crashed_on(self):
        # urlparse raises ValueError on these shapes ("Invalid IPv6 URL" /
        # bracketed non-IPv6 netloc). Both entry points sit on user-pasted text,
        # so a malformed link must read as "not a PR link" — a raised ValueError
        # would 500 the request and discard every valid link in the batch.
        for bad in ("https://[::1", "https://[github.com]/o/r/pull/1"):
            with self.assertRaises(A.UnsupportedPlatform):
                A.detect_platform(bad)
            with self.assertRaises(A.AdapterParseError):
                A.github_pr_ref(bad)
            with self.assertRaises(A.UnsupportedPlatform):
                A.parse_repo_ref(bad)

    def test_link_names_a_host_separates_urls_from_bare_tokens(self):
        # The fail-closed boundary: anything that names a host (parseable or
        # not) must resolve-or-refuse; a bare change token has no host to cross
        # GitHub instances with and may keep the legacy path.
        for named in ("https://acme.ghe.com/o/r/pull/1", "https://[::1",
                      "ghe.corp/o/r/pull/1", "www.github.com/o/r/pull/1"):
            self.assertTrue(A.link_names_a_host(named), named)
        for bare in ("CR-1", "u", "", None, "GH-o-r-1"):
            self.assertFalse(A.link_names_a_host(bare), bare)


class TestFailFast(unittest.TestCase):
    def test_garbage_json(self):
        with self.assertRaises(A.AdapterParseError):
            A.parse_github_payload("{not json", link="https://github.com/o/r/pull/1")

    def test_non_object(self):
        with self.assertRaises(A.AdapterParseError):
            A.parse_github_payload(json.dumps([1, 2, 3]), link="https://github.com/o/r/pull/1")

    def test_empty_payload(self):
        with self.assertRaises(A.AdapterParseError):
            A.parse_github_payload({}, link="https://github.com/o/r/pull/1")

    def test_github_normalize_routes(self):
        t = A.normalize("https://github.com/org/repo/pull/5",
                        {"number": 5, "body": "hello", "html_url":
                         "https://github.com/org/repo/pull/5"})
        self.assertEqual(t.platform, "github")
        self.assertEqual(t.change_id, "GH-org-repo-5")


class TestGithubParse(unittest.TestCase):
    def setUp(self):
        self.t = A.parse_github_payload(GITHUB_PAYLOAD)

    def test_identity(self):
        self.assertEqual(self.t.platform, "github")
        self.assertEqual(self.t.change_id, "GH-kiro_team-kiro_cli-3361")
        self.assertEqual(self.t.repo_identity, "github.com/kiro-team/kiro-cli")
        self.assertEqual(self.t.url, "https://github.com/kiro-team/kiro-cli/pull/3361")

    def test_metadata(self):
        self.assertEqual(self.t.author, "zejiangg")
        self.assertEqual(self.t.target_branch, "main")
        # head SHA is the commit_id used to anchor draft comments.
        self.assertEqual(self.t.revision, "fb58081a1c0ffee0000000000000000000000000")
        self.assertTrue(self.t.is_fix)
        self.assertEqual(self.t.linked_issue, "#3250")

    def test_files(self):
        self.assertEqual(len(self.t.files), 2)
        self.assertEqual(self.t.files[0]["path"], "crates/kiro-cli/src/cli/chat/mod.rs")
        self.assertIn("respawn", self.t.files[0]["diff"])

    def test_comments(self):
        self.assertEqual(len(self.t.existing_comments), 1)

    def test_parse_from_json_string(self):
        t = A.parse_github_payload(json.dumps(GITHUB_PAYLOAD))
        self.assertEqual(t.change_id, "GH-kiro_team-kiro_cli-3361")

    def test_owner_repo_from_link_when_missing(self):
        payload = {"number": 7, "body": "x", "files": [{"path": "a", "diff": "d"}]}
        t = A.parse_github_payload(payload, link="https://github.com/o/r/pull/7")
        self.assertEqual(t.change_id, "GH-o-r-7")
        self.assertEqual(t.repo_identity, "github.com/o/r")

    def test_number_ignores_github_db_id(self):
        # GitHub's `id` is the internal DB id (not the PR number). The change_id
        # must come from the URL's PR number so it matches the driver's _cid.
        payload = {"id": 1847293847, "body": "x", "files": [{"path": "a", "diff": "d"}]}
        t = A.parse_github_payload(payload, link="https://github.com/o/r/pull/5")
        self.assertEqual(t.change_id, "GH-o-r-5")

    def test_change_id_is_filesystem_safe(self):
        # A dot-containing org/repo must not yield path separators, and '-' must
        # be replaced (it is the delimiter) so segments can't collide.
        cid = A.github_change_id("my.org", "re/po", 9)
        self.assertNotIn("/", cid)
        self.assertEqual(cid, "GH-my.org-re_po-9")

    def test_change_id_no_owner_repo_collision(self):
        # The regression the delimiter fix prevents: different owner/repo pairs
        # must NOT map to the same change_id (would collide result files).
        a = A.github_change_id("a-b", "c", 1)
        b = A.github_change_id("a", "b-c", 1)
        self.assertEqual((a, b), ("GH-a_b-c-1", "GH-a-b_c-1"))
        self.assertNotEqual(a, b)

    def test_fail_fast_no_files_no_desc(self):
        with self.assertRaises(A.AdapterParseError):
            A.parse_github_payload({"number": 1},
                                   link="https://github.com/o/r/pull/1")

    def test_fail_fast_no_identity(self):
        with self.assertRaises(A.AdapterParseError):
            A.parse_github_payload({"body": "x", "files": [{"path": "a", "diff": "d"}]})


if __name__ == "__main__":
    unittest.main()
