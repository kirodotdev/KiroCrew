"""Azure DevOps support for Issue Radar.

The companion to ``test_gitlab.py``, covering the parts where an Azure bug would
be silent or dangerous rather than loud:

  * URL parsing -- the modern ``dev.azure.com`` form, the project-only form, and
    the legacy ``{org}.visualstudio.com`` form, plus the rejections that keep an
    unusable name out of a subprocess argv and out of a cache path.
  * ONE identity per organization. The legacy host is accepted when parsing and
    canonicalized to ``dev.azure.com``; if it survived into the key, the same
    organization would have two identities, two cache trees, and a
    connected-repo gate that authorizes one but not the other. That is the whole
    point of pinning the host, so it is asserted directly.
  * Host pinning at BOTH ends -- ``provider.normalize_host`` ignores client
    input, and ``azure_client._resolve_host`` re-checks at the spawn boundary so
    a corrupted config entry cannot reach another server with the user's
    credential.
  * The URL shapes that differ from every other provider: ``_git`` in a
    repository URL, and a work-item list that hangs off the PROJECT with no
    repository dimension at all.
  * The investigation namespace -- Azure numbers work items and pull requests
    from independent sequences, so its pull requests need their own namespace
    while its work items keep the historical one.
  * WIQL injection safety. Azure has no filtered work-item endpoint, so listing
    means BUILDING A QUERY, which no other client in this app does. The escaping
    is the security boundary and is tested as one.
  * The two places Azure must REFUSE rather than approximate: a tag name
    carrying the delimiter its field is stored with, and the cheap open-PR count
    it cannot serve.

No test here reaches the network or needs the ``az`` CLI: every one either
exercises a pure function or mocks ``azure_client._az_invoke``, the single point
every REST call funnels through, exactly as ``test_gitlab.py`` mocks
``_glab_api``.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kiro_crew.apps.builtins.issue_radar.backend import (
    azure_client,
    github_client,
    gitlab_client,
    provider,
    store,
)
from kiro_crew.apps.builtins.issue_radar.backend.errors import (
    ProviderCliError,
    RepoUrlError,
)

MODERN_URL = "https://dev.azure.com/contoso/Widgets/_git/widget-service"
LEGACY_URL = "https://contoso.visualstudio.com/Widgets/_git/widget-service"


def _literals(query: str) -> list[str]:
    """The values of every single-quoted literal in a WIQL query, unescaped.

    Tokenizes the way WIQL itself does -- a doubled ``''`` is one literal quote
    INSIDE a literal, not the end of one -- so the result answers the only
    question that matters about escaping: which spans of this query are data.
    """
    return [m.group(1).replace("''", "'") for m in re.finditer(r"'((?:[^']|'')*)'", query)]


def _outside_literals(query: str) -> str:
    """The query with every literal removed -- i.e. the part WIQL executes.

    An injected operator or keyword shows up HERE. Asserting on this rather than
    on the whole string is what makes the injection test meaningful: a payload
    that stays inside a literal is data, and a payload that reaches this text is
    an injection, regardless of how the surrounding query happens to be spelled.
    """
    return re.sub(r"'(?:[^']|'')*'", "", query)


class TestAzureUrlParsing(unittest.TestCase):
    def test_modern_repository_url(self):
        self.assertEqual(
            azure_client.parse_azure_repo_url(MODERN_URL),
            ("contoso/Widgets", "widget-service"),
        )

    def test_project_url_defaults_the_repository_to_the_project(self):
        # Azure creates a repository named after the project, and the project page
        # is what a user lands on most often, so this form must resolve rather
        # than being refused as incomplete.
        self.assertEqual(
            azure_client.parse_azure_repo_url("https://dev.azure.com/contoso/Widgets"),
            ("contoso/Widgets", "Widgets"),
        )
        self.assertEqual(
            azure_client.parse_azure_repo_url("https://dev.azure.com/contoso/Widgets/"),
            ("contoso/Widgets", "Widgets"),
        )

    def test_legacy_visualstudio_host_carries_the_org_in_the_hostname(self):
        # On the legacy form the organization is the host's first label, NOT the
        # first path segment -- reading it from the path would take the project
        # name as the organization and address a project that does not exist.
        self.assertEqual(
            azure_client.parse_azure_repo_url(LEGACY_URL),
            ("contoso/Widgets", "widget-service"),
        )
        self.assertEqual(
            azure_client.parse_azure_repo_url("https://contoso.visualstudio.com/Widgets"),
            ("contoso/Widgets", "Widgets"),
        )

    def test_deep_page_urls_resolve_to_the_repository(self):
        # Users paste whatever tab they are on. A deeper path or a query must be
        # ignored, never folded into the repository name.
        for suffix in (
            "/pullrequest/12",
            "/commits",
            "?path=/src&version=GBmain",
            ".git",
            "/",
        ):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    azure_client.parse_azure_repo_url(MODERN_URL + suffix),
                    ("contoso/Widgets", "widget-service"),
                )

    def test_project_level_pages_resolve_to_the_default_repository(self):
        # A boards or pipelines URL names the project, not a repository, so the
        # reserved segment must not be taken as a repository name.
        for suffix in ("/_workitems/edit/42", "/_build", "/_settings"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    azure_client.parse_azure_repo_url(
                        f"https://dev.azure.com/contoso/Widgets{suffix}"
                    ),
                    ("contoso/Widgets", "Widgets"),
                )

    def test_names_with_spaces_survive_percent_decoding(self):
        # Azure allows spaces in project and repository names. They are safe here
        # because a value reaches the CLI as its own argv element, never as part
        # of a shell string.
        self.assertEqual(
            azure_client.parse_azure_repo_url(
                "https://dev.azure.com/contoso/My%20Project/_git/My%20Repo"
            ),
            ("contoso/My Project", "My Repo"),
        )

    def test_host_case_and_trailing_dot_are_normalized(self):
        for variant in (
            "https://DEV.AZURE.COM/contoso/Widgets/_git/widget-service",
            "https://dev.azure.com./contoso/Widgets/_git/widget-service",
        ):
            with self.subTest(variant=variant):
                self.assertEqual(
                    azure_client.parse_azure_repo_url(variant),
                    ("contoso/Widgets", "widget-service"),
                )

    def test_rejects_malformed_input(self):
        for bad in (
            "",
            "dev.azure.com/contoso/Widgets",  # not a full URL
            "http://dev.azure.com/contoso/Widgets/_git/r",  # http
            "https://u:p@dev.azure.com/contoso/Widgets/_git/r",  # userinfo
            "https://dev.azure.com/",  # no org, no project
            "https://dev.azure.com/contoso",  # org only, no project
            "https://contoso.visualstudio.com/",  # legacy, no project
        ):
            with self.subTest(bad=bad), self.assertRaises(RepoUrlError):
                azure_client.parse_azure_repo_url(bad)

    def test_rejects_a_non_azure_host(self):
        # The host is matched exactly (or, for the legacy form, as a HOST SUFFIX
        # on the parsed hostname), so neither a prefix nor a lookalike passes.
        # Azure DevOps Server (on-premises) is refused too: the azure-devops CLI
        # extension does not support it, so there is no credential path for it.
        for bad in (
            "https://dev.azure.com.evil.test/contoso/Widgets",
            "https://evilvisualstudio.com/Widgets/_git/r",
            "https://contoso.visualstudio.com.evil.test/Widgets",
            "https://.visualstudio.com/Widgets",
            "https://azuredevops.acme.internal/contoso/Widgets",
        ):
            with self.subTest(bad=bad), self.assertRaises(RepoUrlError):
                azure_client.parse_azure_repo_url(bad)

    def test_rejects_traversal_and_empty_segments(self):
        # Every one of these would otherwise become part of a cache PATH and of a
        # REST route parameter.
        for bad in (
            "https://dev.azure.com/contoso/../Widgets",
            "https://dev.azure.com/contoso/Widgets/_git/..",
            "https://dev.azure.com/contoso/./Widgets",
            "https://dev.azure.com//Widgets/_git/r",
        ):
            with self.subTest(bad=bad), self.assertRaises(RepoUrlError):
                azure_client.parse_azure_repo_url(bad)

    def test_rejects_a_reserved_segment_in_the_org_or_project_position(self):
        # `_git` and `_apis` are Azure's own routing segments. Accepting one as a
        # name would build a URL that addresses a different resource entirely --
        # ``/contoso/_apis/...`` is the REST API, not a project.
        for bad in (
            "https://dev.azure.com/_git/Widgets/_git/r",
            "https://dev.azure.com/_apis/Widgets",
            "https://dev.azure.com/contoso/_apis/_git/r",
            "https://dev.azure.com/contoso/_git",
        ):
            with self.subTest(bad=bad), self.assertRaises(RepoUrlError):
                azure_client.parse_azure_repo_url(bad)

    def test_percent_encoding_cannot_reintroduce_a_separator(self):
        # Segments are decoded so a real space works, which means decoding must
        # not be allowed to smuggle a separator back in.
        for bad in (
            "https://dev.azure.com/contoso/pro%2Fj/_git/r",
            "https://dev.azure.com/contoso/Widgets/_git/re%2Fpo",
            "https://dev.azure.com/contoso/Widgets/_git/re%5Cpo",
        ):
            with self.subTest(bad=bad), self.assertRaises(RepoUrlError):
                azure_client.parse_azure_repo_url(bad)

    def test_malformed_authority_is_a_client_error_not_a_crash(self):
        """A bad host/port must not escape as an unhandled 500.

        ``hostname`` and ``port`` parse the authority lazily and ``urlparse``
        itself raises on some forms, so a malformed URL can raise ``ValueError``
        from several points -- none of which the connect route catches. Every one
        is client input and must arrive as :class:`RepoUrlError` (HTTP 400).
        """
        for bad in (
            "https://dev.azure.com:notaport/contoso/Widgets",
            "https://dev.azure.com:99999999/contoso/Widgets",
            "https://[bad/contoso/Widgets",
            "https://[::1/contoso/Widgets",
        ):
            with self.subTest(bad=bad), self.assertRaises(RepoUrlError):
                azure_client.parse_azure_repo_url(bad)

    def test_malformed_authority_is_also_caught_through_dispatch(self):
        # The connect route goes through provider.parse_repo_url, so the guard has
        # to hold on that path too -- that is the one an HTTP request reaches.
        with self.assertRaises(RepoUrlError):
            provider.parse_repo_url("https://dev.azure.com:notaport/contoso/Widgets")


class TestAzureUrlDispatch(unittest.TestCase):
    """``provider.parse_repo_url`` must route all three Azure shapes to Azure."""

    def test_modern_url_routes_to_azure(self):
        key = provider.parse_repo_url(MODERN_URL)
        self.assertEqual(
            (key.provider, key.host, key.owner, key.repo),
            ("azure", "dev.azure.com", "contoso/Widgets", "widget-service"),
        )

    def test_project_url_routes_to_azure(self):
        key = provider.parse_repo_url("https://dev.azure.com/contoso/Widgets")
        self.assertEqual(
            (key.provider, key.host, key.owner, key.repo),
            ("azure", "dev.azure.com", "contoso/Widgets", "Widgets"),
        )

    def test_the_legacy_host_does_not_produce_a_second_identity(self):
        """The legacy and modern URLs must parse to the SAME key. Byte for byte.

        This is the whole reason Azure's host is pinned. If ``visualstudio.com``
        survived into the key, one organization would have two identities: two
        cache trees under ``@providers/azure/...``, two connected-repo entries,
        and a gate that authorizes a request naming one host while refusing the
        identical request naming the other. Nothing about that failure is visible
        -- both keys look right in isolation -- so it is asserted as an equality
        of the whole key rather than as a host check.
        """
        self.assertEqual(provider.parse_repo_url(LEGACY_URL), provider.parse_repo_url(MODERN_URL))
        self.assertEqual(provider.parse_repo_url(LEGACY_URL).host, "dev.azure.com")

        # ... and therefore one connected record authorizes either spelling,
        # because there is only ever one spelling by the time the gate sees it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = provider.parse_repo_url(MODERN_URL)
            store.add_connected_repo(
                key.owner, key.repo, root=root, provider=key.provider, host=key.host
            )
            legacy = provider.parse_repo_url(LEGACY_URL)
            self.assertTrue(
                store.is_repo_connected(
                    legacy.owner,
                    legacy.repo,
                    root,
                    provider=legacy.provider,
                    host=legacy.host,
                )
            )

    def test_an_azure_host_is_never_handed_to_the_gitlab_parser(self):
        # GitLab is the dispatch table's FALLBACK, so an Azure host has to be
        # recognized before it. Asserted with the legacy host present in the
        # GitLab allowlist -- the one configuration where a fallback-first
        # dispatch would silently win and parse ``{project}/_git/{repo}`` as a
        # GitLab namespace.
        with mock.patch.object(
            gitlab_client,
            "allowed_hosts",
            return_value=frozenset({"contoso.visualstudio.com", "dev.azure.com"}),
        ):
            self.assertEqual(provider.parse_repo_url(LEGACY_URL).provider, "azure")
            self.assertEqual(provider.parse_repo_url(MODERN_URL).provider, "azure")

    def test_a_bad_azure_url_reports_an_azure_error(self):
        # Not a confusing "not a GitLab host", which is what the fallback would
        # produce if the host were not recognized first.
        with self.assertRaises(RepoUrlError) as ctx:
            provider.parse_repo_url("https://dev.azure.com/contoso")
        self.assertIn("azure", str(ctx.exception).lower())


class TestAzureHostPinning(unittest.TestCase):
    """The host is not client-controlled, and is re-checked at every spawn."""

    def test_normalize_host_pins_azure_regardless_of_client_input(self):
        for raw in (None, "", "   ", "evil.test", "DEV.AZURE.COM.", "contoso.visualstudio.com"):
            with self.subTest(raw=raw):
                self.assertEqual(provider.normalize_host(raw, "azure"), "dev.azure.com")

    def test_a_crafted_host_cannot_reach_the_key(self):
        # The host becomes part of a cache path and part of the identity a repo is
        # looked up by, so a request must not be able to choose it.
        key = provider.key_from_parts("contoso/Widgets", "widget-service", "azure", "evil.test")
        self.assertEqual(key.host, "dev.azure.com")
        self.assertEqual(
            provider.key_from_parts("contoso/Widgets", "w", "azure", None).host,
            "dev.azure.com",
        )

    def test_gitlab_is_still_not_pinned(self):
        # Pinning is per provider, not a global rule: a GitLab host is operator
        # configuration and an ABSENT one must stay empty rather than defaulting.
        self.assertEqual(
            provider.normalize_host("gitlab.acme.internal", "gitlab"), "gitlab.acme.internal"
        )
        self.assertEqual(provider.normalize_host(None, "gitlab"), "")

    def test_the_spawn_boundary_accepts_only_the_pinned_host(self):
        # Belt and braces: normalize_host already pins it, but a corrupted config
        # entry or a future call site that forgets to normalize must not reach
        # another server carrying the user's credential.
        self.assertEqual(azure_client._resolve_host("dev.azure.com"), "dev.azure.com")
        self.assertEqual(azure_client._resolve_host("DEV.AZURE.COM."), "dev.azure.com")
        for bad in ("", "contoso.visualstudio.com", "evil.test", "azuredevops.acme.internal"):
            with self.subTest(bad=bad), self.assertRaises(ProviderCliError):
                azure_client._resolve_host(bad)

    def test_an_omitted_host_is_refused_not_defaulted(self):
        # Mirrors gitlab_client: a call site that forgot the host must fail loudly
        # rather than silently targeting a host the caller never named.
        with self.assertRaises(ProviderCliError):
            azure_client._resolve_host("")

    def test_call_kwargs_supplies_the_host_for_azure(self):
        key = provider.parse_repo_url(MODERN_URL)
        self.assertEqual(provider.call_kwargs(key), {"host": "dev.azure.com"})
        # GitHub takes none; GitLab takes its own.
        self.assertEqual(provider.call_kwargs(provider.RepoKey(provider="github")), {})
        self.assertEqual(
            provider.call_kwargs(provider.RepoKey(provider="gitlab", host="gitlab.com")),
            {"host": "gitlab.com"},
        )


class TestAzureWebUrls(unittest.TestCase):
    def test_web_url_inserts_the_git_segment(self):
        # An Azure project holds repositories alongside boards, pipelines and
        # artifacts; `_git` is the segment that disambiguates them. Without it the
        # link resolves to the project overview, not the repository.
        key = provider.parse_repo_url(MODERN_URL)
        self.assertEqual(key.web_url(), "https://dev.azure.com/contoso/Widgets/_git/widget-service")
        self.assertEqual(key.web_url(), MODERN_URL)

    def test_the_other_providers_keep_a_plain_namespace_path(self):
        self.assertEqual(
            provider.key_from_parts("o", "r", "github").web_url(), "https://github.com/o/r"
        )
        self.assertEqual(
            provider.key_from_parts("g/sub", "p", "gitlab", "gitlab.acme.internal").web_url(),
            "https://gitlab.acme.internal/g/sub/p",
        )

    def test_tracked_items_url_is_project_scoped_on_azure(self):
        # Work items belong to the PROJECT, which ``owner`` already carries as
        # ``{org}/{project}``. The repository does not appear at all -- so
        # web_url() is not this URL's prefix, which is exactly what a
        # "GitHub-or-GitLab" binary would have assumed.
        key = provider.parse_repo_url(MODERN_URL)
        url = provider.tracked_items_url(key)
        self.assertEqual(url, "https://dev.azure.com/contoso/Widgets/_workitems/")
        self.assertNotIn("widget-service", url)
        self.assertNotIn("_git", url)
        self.assertFalse(url.startswith(key.web_url()))

    def test_tracked_items_url_keeps_the_other_two_shapes(self):
        self.assertEqual(
            provider.tracked_items_url(provider.key_from_parts("o", "r", "github")),
            "https://github.com/o/r/issues",
        )
        self.assertEqual(
            provider.tracked_items_url(
                provider.key_from_parts("g/sub", "p", "gitlab", "gitlab.acme.internal")
            ),
            "https://gitlab.acme.internal/g/sub/p/-/issues",
        )

    def test_unknown_provider_falls_back_to_the_github_shape(self):
        # A corrupted config entry should degrade to a wrong-looking link, not to
        # another provider's URL layout -- the same fallback rule client_for uses.
        key = provider.RepoKey(provider="bogus", host="github.com", owner="o", repo="r")
        self.assertEqual(provider.tracked_items_url(key), "https://github.com/o/r/issues")


class TestAzureInvestigationNamespace(unittest.TestCase):
    """Azure numbers work items and pull requests from independent sequences.

    A work item and a pull request can carry the same number and be unrelated
    items allocated by different services, so sharing one investigation record
    would make "Review PR !5" resume work item #5's chat session and overwrite its
    findings. Only the namespace that has never been written to changes.
    """

    def test_azure_pulls_get_their_own_namespace(self):
        key = provider.key_from_parts("contoso/Widgets", "widget-service", "azure")
        self.assertEqual(provider.investigation_kind(key, "pull"), "pr")
        # Not GitLab's namespace either: the two are separate tables, so a future
        # edit to one must not silently move the other's records.
        self.assertNotEqual(
            provider.investigation_kind(key, "pull"),
            provider.investigation_kind(provider.key_from_parts("g", "p", "gitlab"), "pull"),
        )

    def test_azure_work_items_keep_the_historical_namespace(self):
        # Tracked items are "issue" on every provider, which is why no existing
        # record has to move.
        key = provider.key_from_parts("contoso/Widgets", "widget-service", "azure")
        self.assertEqual(provider.investigation_kind(key, "issue"), "issue")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                store.investigation_path("contoso/Widgets", "widget-service", 5, root),
                store.investigation_path(
                    "contoso/Widgets", "widget-service", 5, root, kind="issue"
                ),
            )

    def test_a_work_item_and_a_pull_request_numbered_the_same_do_not_collide(self):
        key = provider.key_from_parts("contoso/Widgets", "widget-service", "azure")
        item_kind = provider.investigation_kind(key, "issue")
        pr_kind = provider.investigation_kind(key, "pull")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store.write_investigation(
                key.owner, key.repo, 5, {"slot_key": "work-item-slot"}, root=root, kind=item_kind
            )
            store.write_investigation(
                key.owner, key.repo, 5, {"slot_key": "pr-slot"}, root=root, kind=pr_kind
            )
            item_record = store.read_investigation(key.owner, key.repo, 5, root, kind=item_kind)
            pr_record = store.read_investigation(key.owner, key.repo, 5, root, kind=pr_kind)

        assert item_record is not None and pr_record is not None
        self.assertEqual(item_record["slot_key"], "work-item-slot")
        self.assertEqual(pr_record["slot_key"], "pr-slot")

    def test_every_kind_resolves_to_a_known_namespace(self):
        # An unrecognized item kind must not invent a third namespace.
        key = provider.key_from_parts("contoso/Widgets", "widget-service", "azure")
        self.assertEqual(provider.investigation_kind(key, "comment"), "issue")


class TestAzureTerms(unittest.TestCase):
    """The display vocabulary the UI and the AI prompts read."""

    def test_azure_speaks_work_items_not_issues(self):
        # "Issue" is one work item TYPE in some process templates, not the
        # category, so calling the category "issues" would name a filter the user
        # did not apply.
        terms = provider.terms(provider.key_from_parts("contoso/Widgets", "w", "azure"))
        self.assertEqual(terms["tracked_item"], "work item")
        self.assertEqual(terms["tracked_item_plural"], "work items")
        self.assertEqual(terms["provider_name"], "Azure DevOps")
        self.assertEqual(terms["cli"], "az")

    def test_azure_pull_requests_are_pull_requests_with_gitlabs_sigil(self):
        # Azure says "pull request" like GitHub, but addresses them with "!" like
        # GitLab, because its two sequences are independent.
        terms = provider.terms(provider.key_from_parts("contoso/Widgets", "w", "azure"))
        self.assertEqual(terms["change_request"], "pull request")
        self.assertEqual(terms["change_request_short"], "PR")
        self.assertEqual(terms["change_request_sigil"], "!")

    def test_the_other_providers_still_say_issue(self):
        for name in ("github", "gitlab"):
            with self.subTest(provider=name):
                terms = provider.terms(provider.key_from_parts("o", "r", name))
                self.assertEqual(terms["tracked_item"], "issue")
                self.assertEqual(terms["tracked_item_plural"], "issues")

    def test_every_provider_defines_the_whole_vocabulary(self):
        # The frontend reads these keys unconditionally, so a provider missing one
        # renders an empty tab label rather than raising anywhere visible.
        expected = set(provider.terms(provider.key_from_parts("o", "r", "github")))
        for name in provider.PROVIDERS:
            with self.subTest(provider=name):
                self.assertEqual(
                    set(provider.terms(provider.key_from_parts("o", "r", name))), expected
                )

    def test_unknown_provider_falls_back_to_github_vocabulary(self):
        self.assertEqual(
            provider.terms(provider.RepoKey(provider="bogus"))["tracked_item"], "issue"
        )


class TestAzureNameCase(unittest.TestCase):
    """Azure names are compared case-SENSITIVELY, deliberately.

    ``store.name_compare_key`` is the one definition of "same name", and the
    authorization gate and the data plane must agree on it. Casefolding a
    provider whose case semantics have not been confirmed is the direction that
    fails unsafely: it would merge two distinct projects' caches and admit a
    case-variant through the gate.
    """

    def test_azure_names_are_not_casefolded(self):
        self.assertEqual(store.name_compare_key("Widgets", "azure"), "Widgets")
        self.assertNotEqual(
            store.name_compare_key("Widgets", "azure"), store.name_compare_key("widgets", "azure")
        )
        # GitHub is the one provider whose case-insensitivity is confirmed.
        self.assertEqual(
            store.name_compare_key("Widgets", "github"), store.name_compare_key("widgets", "github")
        )

    def test_the_gate_does_not_admit_a_case_variant_azure_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store.add_connected_repo(
                "contoso/Widgets",
                "widget-service",
                root=root,
                provider="azure",
                host="dev.azure.com",
            )
            self.assertTrue(
                store.is_repo_connected(
                    "contoso/Widgets",
                    "widget-service",
                    root,
                    provider="azure",
                    host="dev.azure.com",
                )
            )
            self.assertFalse(
                store.is_repo_connected(
                    "contoso/widgets",
                    "widget-service",
                    root,
                    provider="azure",
                    host="dev.azure.com",
                )
            )


class TestWiqlInjectionSafety(unittest.TestCase):
    """The work-item listing BUILDS A QUERY, which no other client here does.

    Azure has no filtered work-item endpoint, so every listing is a WIQL query
    assembled from values this module did not choose: the project name, and the
    project's own closing STATE NAMES, which come from a process template and can
    say anything. ``_wiql_literal`` is the boundary that keeps those values data,
    so it is tested as a security control: a payload either stays inside a literal
    or the test fails.
    """

    def test_a_quote_is_doubled_which_is_wiqls_own_escape(self):
        self.assertEqual(azure_client._wiql_literal("it's"), "'it''s'")
        self.assertEqual(azure_client._wiql_literal("Widgets"), "'Widgets'")

    def test_a_quote_cannot_close_the_literal_early(self):
        """A payload that tries to escape must end up entirely inside a literal.

        Asserted on the TOKENIZATION, not on the exact string: the parsed literal
        must be the payload verbatim, and the executable part of the query must
        contain none of it. Comparing against a hand-written expected query would
        pass just as happily against a query whose quoting was broken in a
        different way.
        """
        payload = "x' OR [System.State] <> 'zzz"
        query = azure_client._open_work_items_wiql(
            payload, frozenset({"Closed"}), order_by="System.ChangedDate"
        )
        # The payload is one literal, carrying its quote intact rather than stripped.
        self.assertIn(payload, _literals(query))
        self.assertEqual(_literals(query), [payload, "Closed"])
        # Nothing from the payload reached the part WIQL executes. ("OR" is
        # checked space-delimited because the query's own text contains it inside
        # WorkItems and ORDER BY.)
        executable = _outside_literals(query)
        self.assertNotIn(" OR ", executable.upper())
        self.assertNotIn("<>", executable)
        self.assertNotIn("zzz", executable)
        # And the query is still well-formed: every literal is closed.
        self.assertEqual(query.count("'") % 2, 0)

    def test_a_control_character_is_refused_not_stripped(self):
        # Silently dropping a character changes which items the query matches, and
        # a name that cannot be represented exactly is a name that must not be
        # guessed at.
        for bad in ("a\nb", "a\rb", "a\tb", "a\x00b", "a\x1fb", "a\x7fb"):
            with self.subTest(bad=bad), self.assertRaises(ProviderCliError):
                azure_client._wiql_literal(bad)

    def test_an_empty_value_is_refused(self):
        # An empty literal would match nothing while looking like a valid filter.
        # The annotation forbids None, which is the point: the runtime guard has to
        # hold for a caller that ignores the type, so the check is deliberately
        # exercised with a value mypy would reject.
        for bad in ("", None):
            with self.subTest(bad=bad), self.assertRaises(ProviderCliError):
                azure_client._wiql_literal(bad)  # type: ignore[arg-type]

    def test_state_names_from_the_process_template_are_escaped_too(self):
        # The state names are REMOTE data -- whoever defines the project's process
        # chooses them -- and they are interpolated into an IN(...) list, which is
        # the easier of the two spots to inject into.
        query = azure_client._open_work_items_wiql(
            "Widgets", frozenset({"Closed", "Do'ne"}), order_by="System.ChangedDate"
        )
        self.assertEqual(_literals(query), ["Widgets", "Closed", "Do'ne"])
        self.assertIn("''", query)

    def test_the_query_that_reaches_az_is_the_escaped_one(self):
        """End to end: a hostile state name arrives at the CLI as data.

        The unit test above proves the helper escapes; this proves the LISTING
        path actually routes through it. Both are needed -- a listing that built
        its own query string would pass the first test and fail here.
        """
        hostile = "Closed') OR [System.State] <> ('zzz"
        bodies: list[dict] = []

        def fake_invoke(**kwargs):
            resource = kwargs["resource"]
            if resource == "workitemtypes":
                # Azure's own answer, carrying the hostile state name.
                return {
                    "value": [
                        {
                            "name": "Bug",
                            "states": [
                                {"name": hostile, "category": "Completed"},
                            ],
                        }
                    ]
                }
            if resource == "wiql":
                bodies.append(kwargs["body"])
                return {"workItems": []}
            raise AssertionError(f"unexpected resource: {resource}")

        with mock.patch.object(azure_client, "_az_invoke", side_effect=fake_invoke):
            probe = azure_client.probe_open_list(
                "contoso/Widgets", "widget-service", "issue", host="dev.azure.com"
            )
        self.assertEqual(probe["total_count"], 0)
        self.assertEqual(len(bodies), 1)
        query = bodies[0]["query"]
        self.assertEqual(_literals(query), ["Widgets", hostile])
        executable = _outside_literals(query)
        self.assertNotIn("<>", executable)
        self.assertNotIn("zzz", executable)
        self.assertEqual(query.count("'") % 2, 0)

    def test_a_control_character_in_a_state_name_fails_the_listing(self):
        # The refusal has to propagate: a query that cannot be built safely must
        # raise, not fall back to an unfiltered one (which would report every
        # closed item as open).
        def fake_invoke(**kwargs):
            if kwargs["resource"] == "workitemtypes":
                return {
                    "value": [
                        {
                            "name": "Bug",
                            "states": [
                                {"name": "Clo\nsed", "category": "Completed"},
                            ],
                        }
                    ]
                }
            raise AssertionError("the query must not be sent")

        with mock.patch.object(azure_client, "_az_invoke", side_effect=fake_invoke):
            with self.assertRaises(ProviderCliError):
                azure_client.probe_open_list(
                    "contoso/Widgets", "widget-service", "issue", host="dev.azure.com"
                )

    def test_a_quote_in_a_project_name_is_refused_before_the_query(self):
        # Defense in depth: the segment charset already refuses a quote in an
        # owner, so the escaping is the SECOND line rather than the only one.
        with self.assertRaises(ProviderCliError):
            azure_client._split_owner("contoso/Wid'gets")


class TestTagDelimiterRefusal(unittest.TestCase):
    """``System.Tags`` is ONE delimited string, so a delimiter cannot be written.

    Azure stores tags as ``"a; b"`` and splits on ``,`` and ``;``. There is no
    escaping mechanism, so a name containing either would silently become two
    tags -- or corrupt a neighbouring one, since the write is a
    read-modify-write of the whole field. Refusing is the only honest answer.
    """

    def test_a_delimiter_in_a_tag_name_is_refused(self):
        for bad in ("a,b", "a;b", "needs triage; blocked", ""):
            with self.subTest(bad=bad), self.assertRaises(ProviderCliError):
                azure_client._check_label(bad)

    def test_a_legal_tag_round_trips_through_the_delimited_field(self):
        names = ["needs-triage", "blocked", "Bug"]
        self.assertEqual(azure_client._tags_field(names), "needs-triage; blocked; Bug")
        self.assertEqual(azure_client._tag_names("needs-triage; blocked; Bug"), names)
        # Tags are case sensitive on Azure, so the caller's spelling is preserved.
        self.assertEqual(azure_client._check_label("Bug"), "Bug")

    def test_the_refusal_happens_before_any_write_is_attempted(self):
        """No spawn at all -- the point is that the field is never touched.

        Validating after the read would leave the door open to a partial write:
        the read-modify-write replaces the WHOLE tag field, so a name that splits
        would rewrite every other tag on the item at the same time.
        """
        spawn = mock.Mock()
        with mock.patch.object(azure_client, "_az_invoke", spawn):
            with self.assertRaises(ProviderCliError):
                azure_client.add_issue_labels(
                    "contoso/Widgets",
                    "widget-service",
                    5,
                    ["fine", "a;b"],
                    host="dev.azure.com",
                )
            with self.assertRaises(ProviderCliError):
                azure_client.remove_issue_label(
                    "contoso/Widgets", "widget-service", 5, "a,b", host="dev.azure.com"
                )
            with self.assertRaises(ProviderCliError):
                azure_client.create_label(
                    "contoso/Widgets", "widget-service", "a;b", host="dev.azure.com"
                )
        self.assertEqual(spawn.call_count, 0)

    def test_an_over_long_tag_is_refused(self):
        with self.assertRaises(ProviderCliError):
            azure_client._check_label("x" * 401)


class TestAzureOpenListProbe(unittest.TestCase):
    """The cheap poll probe that gates list polling."""

    @staticmethod
    def _invoke(*, ids=(42, 7), changed="2026-01-02T00:00:00Z"):
        def fake_invoke(**kwargs):
            resource = kwargs["resource"]
            if resource == "workitemtypes":
                return {
                    "value": [
                        {
                            "name": "Bug",
                            "states": [
                                {"name": "New", "category": "Proposed"},
                                {"name": "Closed", "category": "Completed"},
                            ],
                        },
                        {"name": "Task", "states": [{"name": "Removed", "category": "Removed"}]},
                    ]
                }
            if resource == "wiql":
                return {"workItems": [{"id": i} for i in ids]}
            if resource == "workitemsbatch":
                return {"value": [{"id": ids[0], "fields": {"System.ChangedDate": changed}}]}
            raise AssertionError(f"unexpected resource: {resource}")

        return fake_invoke

    def test_probe_shape_matches_github(self):
        # The value is compared against a STORED probe, so the keys must match
        # github_client's exactly or every poll would read as "changed".
        with mock.patch.object(azure_client, "_az_invoke", side_effect=self._invoke()):
            probe = azure_client.probe_open_list(
                "contoso/Widgets", "widget-service", "issue", host="dev.azure.com"
            )
        self.assertEqual(set(probe), {"total_count", "top_updated_at"})
        self.assertEqual(probe, {"total_count": 2, "top_updated_at": "2026-01-02T00:00:00Z"})

    def test_the_probe_is_project_scoped_and_ignores_the_repository(self):
        # Work items have no repository dimension, so two repositories in one
        # project legitimately probe the same list. Asserted because the opposite
        # assumption -- a per-repository probe -- would look correct and quietly
        # report "unchanged" for one of them.
        routes: list[dict] = []

        def recording(**kwargs):
            routes.append(dict(kwargs.get("route") or {}))
            return self._invoke()(**kwargs)

        with mock.patch.object(azure_client, "_az_invoke", side_effect=recording):
            first = azure_client.probe_open_list(
                "contoso/Widgets", "widget-service", "issue", host="dev.azure.com"
            )
            second = azure_client.probe_open_list(
                "contoso/Widgets", "another-repo", "issue", host="dev.azure.com"
            )
        self.assertEqual(first, second)
        # Every call is addressed by project alone; no repository id appears.
        self.assertTrue(routes)
        for route in routes:
            self.assertEqual(route, {"project": "Widgets"})

    def test_the_pull_request_probe_is_refused_not_approximated(self):
        # Azure's PR list endpoint exposes no total count and Azure has no PR
        # modification timestamp, so any signal here would be weaker than the
        # caller believes: a non-top PR could close and the cache still be served
        # as verified-fresh. Raising takes the caller's documented
        # probe-unavailable path instead, bounded by the staleness ceiling.
        spawn = mock.Mock()
        with mock.patch.object(azure_client, "_az_invoke", spawn):
            with self.assertRaises(ProviderCliError):
                azure_client.probe_open_list(
                    "contoso/Widgets", "widget-service", "pr", host="dev.azure.com"
                )
        # Refused outright: not even one call went out to try to approximate it.
        self.assertEqual(spawn.call_count, 0)

    def test_an_unknown_kind_is_refused(self):
        spawn = mock.Mock()
        with mock.patch.object(azure_client, "_az_invoke", spawn):
            with self.assertRaises(ProviderCliError):
                azure_client.probe_open_list(
                    "contoso/Widgets", "widget-service", "comment", host="dev.azure.com"
                )
        self.assertEqual(spawn.call_count, 0)

    def test_gitlab_refuses_the_same_kind(self):
        # The refusal is a shared contract, not an Azure quirk: a caller handling
        # one provider's probe-unavailable path handles the other's.
        with self.assertRaises(ProviderCliError):
            gitlab_client.probe_open_list("g", "p", "pr", host="gitlab.com")

    def test_github_still_serves_the_probe(self):
        # ... and the reference implementation is unaffected, so the fallback is
        # only taken where it is genuinely needed.
        with mock.patch.object(
            github_client,
            "_run_gh_api",
            return_value=[{"total_count": 3, "top_updated_at": "2026-01-02T00:00:00Z"}],
        ):
            probe = github_client.probe_open_list("o", "r", "pr")
        self.assertEqual(set(probe), {"total_count", "top_updated_at"})
        self.assertEqual(probe["total_count"], 3)


class TestPermissionsFailClosed(unittest.TestCase):
    """Azure reports NO write access, because nothing here can establish it.

    The only signal this transport can reach is project team membership, and
    membership does not imply repository write: Azure's permissions are per-repo
    ACLs a project can override, so a team member may have Git Contribute denied
    while still editing work items. Since ``routes._repo_can_write`` consults this
    answer and caches it, inferring write from membership would offer controls the
    repository then refuses.
    """

    OWNER = "contoso/Widgets"

    def _perms(self):
        return azure_client.get_repo_permissions(self.OWNER, "widget-service", host="dev.azure.com")

    def test_write_is_never_granted(self):
        perms = self._perms()
        self.assertFalse(perms["push"])
        self.assertFalse(perms["triage"])
        self.assertFalse(perms["admin"])
        self.assertFalse(perms["maintain"])

    def test_read_is_still_reported(self):
        # Read access is already demonstrated by the project being reachable and
        # connected, so blinding the list views would be wrong.
        self.assertTrue(self._perms()["pull"])

    def test_the_answer_costs_no_provider_call(self):
        # It is a constant, so it must not spend an az spawn (nor depend on a
        # roster read that could fail and reintroduce an inference).
        spawn = mock.Mock()
        with mock.patch.object(azure_client, "_az_invoke", spawn):
            self._perms()
        spawn.assert_not_called()

    def test_the_shape_matches_the_other_providers(self):
        # routes and the frontend read these exact keys for every provider.
        self.assertEqual(
            set(self._perms()),
            {"admin", "maintain", "push", "triage", "pull"},
        )


class TestTagWriteIsRevisionGuarded(unittest.TestCase):
    """``System.Tags`` edits are a read-modify-write, so they need a rev test.

    Azure exposes no add-one-tag operation: the whole delimited field is
    rewritten. Without an optimistic-concurrency guard, a tag another client
    added between our read and our write is silently deleted.
    """

    OWNER = "contoso/Widgets"

    def _fake(self, *, rev, tags="bug"):
        """A fake ``_az_invoke`` recording every JSON-Patch body it is handed.

        The read half is a ``workitemsbatch`` POST, so it answers with a ``value``
        array; only the tag write is a PATCH, which is what the recorder captures.
        """
        patches: list[list[dict]] = []

        def invoke(**kwargs):
            if kwargs.get("method") == "PATCH":
                body = kwargs.get("body")
                # A JSON-Patch body is a list of operations; asserting the shape
                # here keeps the recorder honest as well as typed.
                assert isinstance(body, list), f"expected a patch list, got {body!r}"
                patches.append(body)
                return {"fields": {"System.Tags": tags}}
            item: dict = {"id": 5, "fields": {"System.Tags": tags}}
            if rev is not None:
                item["rev"] = rev
            return {"value": [item]}

        return invoke, patches

    def test_the_patch_leads_with_a_rev_test(self):
        invoke, patches = self._fake(rev=7)
        with mock.patch.object(azure_client, "_az_invoke", side_effect=invoke):
            azure_client.add_issue_labels(
                self.OWNER, "widget-service", 5, ["urgent"], host="dev.azure.com"
            )
        self.assertEqual(len(patches), 1)
        # Order matters: Azure evaluates the operations in sequence and rejects the
        # whole patch when the test fails, so the test must precede the write.
        self.assertEqual(patches[0][0], {"op": "test", "path": "/rev", "value": 7})
        self.assertEqual(patches[0][1]["path"], "/fields/System.Tags")

    def test_removal_is_guarded_too(self):
        invoke, patches = self._fake(rev=11, tags="bug; stale")
        with mock.patch.object(azure_client, "_az_invoke", side_effect=invoke):
            azure_client.remove_issue_label(
                self.OWNER, "widget-service", 5, "stale", host="dev.azure.com"
            )
        self.assertEqual(patches[0][0], {"op": "test", "path": "/rev", "value": 11})

    def test_a_work_item_without_a_revision_is_refused_before_writing(self):
        """No rev means no guard, so the write must not happen at all.

        Degrading to an unguarded full-field write is exactly the lost update this
        guard exists to prevent, so the refusal has to come BEFORE the patch.
        """
        invoke, patches = self._fake(rev=None)
        with mock.patch.object(azure_client, "_az_invoke", side_effect=invoke):
            with self.assertRaises(ProviderCliError):
                azure_client.add_issue_labels(
                    self.OWNER, "widget-service", 5, ["urgent"], host="dev.azure.com"
                )
        self.assertEqual(patches, [], "a write was attempted without a rev guard")

    def test_a_non_integer_revision_is_refused(self):
        invoke, patches = self._fake(rev="7")
        with mock.patch.object(azure_client, "_az_invoke", side_effect=invoke):
            with self.assertRaises(ProviderCliError):
                azure_client.add_issue_labels(
                    self.OWNER, "widget-service", 5, ["urgent"], host="dev.azure.com"
                )
        self.assertEqual(patches, [])


class TestAuditPrecedesExecution(unittest.TestCase):
    """An unwritable security event log must STOP an az command, not follow it.

    Auditing only after the spawn means a provider mutation can run and leave no
    record precisely when the log is the broken thing.
    """

    def _run(self, audit):
        spawn = mock.Mock()
        with mock.patch.object(azure_client, "_az_bin", return_value="/usr/bin/az"):
            with mock.patch.object(azure_client, "_az_env", return_value={}):
                with mock.patch.object(azure_client, "_audit", audit):
                    with mock.patch.object(azure_client.subprocess, "run", spawn):
                        azure_client._az_run(
                            ["az", "devops", "invoke"], host="dev.azure.com", timeout=1.0
                        )
        return spawn

    def test_the_invoked_record_is_written_before_the_spawn(self):
        order: list[str] = []

        def audit(op, target, outcome, **kw):
            order.append(f"audit:{outcome}")

        def spawn_impl(*args, **kwargs):
            order.append("spawn")
            return mock.Mock(returncode=0)

        spawn = mock.Mock(side_effect=spawn_impl)
        with mock.patch.object(azure_client, "_az_bin", return_value="/usr/bin/az"):
            with mock.patch.object(azure_client, "_az_env", return_value={}):
                with mock.patch.object(azure_client, "_audit", audit):
                    with mock.patch.object(azure_client.subprocess, "run", spawn):
                        azure_client._az_run(
                            ["az", "devops", "invoke"], host="dev.azure.com", timeout=1.0
                        )
        self.assertEqual(order[0], "audit:invoked", f"audit did not precede the spawn: {order}")
        self.assertIn("spawn", order)

    def test_a_failing_audit_prevents_the_spawn(self):
        spawn = mock.Mock()
        audit = mock.Mock(side_effect=RuntimeError("sel is unwritable"))
        with mock.patch.object(azure_client, "_az_bin", return_value="/usr/bin/az"):
            with mock.patch.object(azure_client, "_az_env", return_value={}):
                with mock.patch.object(azure_client, "_audit", audit):
                    with mock.patch.object(azure_client.subprocess, "run", spawn):
                        with self.assertRaises(ProviderCliError):
                            azure_client._az_run(
                                ["az", "devops", "invoke"], host="dev.azure.com", timeout=1.0
                            )
        spawn.assert_not_called()


class TestCreateLabelRefusesWhatItCannotCreate(unittest.TestCase):
    """Azure has no create-tag endpoint, so reporting success would be a lie.

    A tag definition materializes when a tag is first APPLIED to a work item.
    Returning a shaped tag for a name that does not exist puts a phantom label in
    the palette that filters nothing and vanishes on the next refresh.
    """

    OWNER = "contoso/Widgets"

    def test_an_unknown_tag_is_refused(self):
        with mock.patch.object(azure_client, "list_repo_labels", return_value=[]):
            with self.assertRaises(ProviderCliError) as caught:
                azure_client.create_label(
                    self.OWNER, "widget-service", "brand-new", host="dev.azure.com"
                )
        # The message has to say what to do instead, since the caller cannot tell
        # from the type that this provider simply has no such operation.
        self.assertIn("work item", str(caught.exception))

    def test_an_existing_tag_is_returned_as_azure_holds_it(self):
        row = {"name": "bug", "color": "888888", "description": ""}
        with mock.patch.object(azure_client, "list_repo_labels", return_value=[row]):
            self.assertEqual(
                azure_client.create_label(
                    self.OWNER, "widget-service", "bug", host="dev.azure.com"
                ),
                row,
            )

    def test_an_unreadable_tag_list_still_refuses(self):
        # Fail closed: an unreadable list must not be read as "it does not exist,
        # so pretend we made it".
        with mock.patch.object(
            azure_client, "list_repo_labels", side_effect=ProviderCliError("tags unreadable")
        ):
            with self.assertRaises(ProviderCliError):
                azure_client.create_label(self.OWNER, "widget-service", "bug", host="dev.azure.com")


class TestAuditIsWrittenSynchronously(unittest.TestCase):
    """The pre-spawn audit only gates the spawn if it is a CRITICAL write.

    ``sel().log_api_access`` enqueues by default and returns successfully even when
    the log cannot be written, so auditing before the spawn is not enough on its
    own -- without ``critical=True`` nothing ever raises and the command runs
    unrecorded anyway.
    """

    def _calls(self, outcome):
        logger = mock.Mock()
        with mock.patch.object(azure_client, "sel", return_value=logger):
            azure_client._audit("az_run", "az devops invoke", outcome)
        return logger.log_api_access.call_args.kwargs

    def test_the_invoked_record_is_critical(self):
        self.assertIs(self._calls("invoked")["critical"], True)

    def test_the_outcome_records_are_not_critical(self):
        # After the command has run, raising would replace the caller's real error
        # with a logging one and change nothing about what already happened.
        for outcome in ("ok", "failure"):
            with self.subTest(outcome=outcome):
                self.assertIs(self._calls(outcome)["critical"], False)


class TestRecentRepoRowsCarryFullName(unittest.TestCase):
    """The picker keys on ``full_name`` and splits it to rebuild an Azure URL.

    A row without it crashes selection on ``undefined.split``, so the three-part
    ``org/project/repo`` form is a contract with the frontend.
    """

    def test_each_row_carries_a_three_part_full_name(self):
        def invoke(**kwargs):
            if kwargs.get("resource") == "repositories":
                return {"value": [{"name": "ledger"}, {"name": "payments-api"}]}
            return {"value": []}

        def paged(**kwargs):
            return [{"name": "Widgets", "visibility": "private", "description": "d"}]

        with mock.patch.object(azure_client, "_az_invoke_paged", side_effect=paged):
            with mock.patch.object(azure_client, "_az_invoke", side_effect=invoke):
                with mock.patch.object(
                    azure_client, "_current_identity", return_value={"login": "ada"}
                ):
                    with mock.patch.object(
                        azure_client, "_default_organization", return_value="contoso"
                    ):
                        rows, _ = azure_client.list_contributed_repos("ada", host="dev.azure.com")
        self.assertTrue(rows, "expected at least one row")
        for row in rows:
            with self.subTest(repo=row.get("repo")):
                full = row.get("full_name")
                # `assert isinstance` rather than assertIsInstance: it both checks
                # and narrows, so the comparison below type-checks.
                assert isinstance(full, str), f"full_name missing or not a string: {full!r}"
                # Exactly the shape publicRepoUrl destructures as
                # [org, project, ...rest] before inserting `_git`.
                self.assertEqual(
                    azure_client._url_path_segments(full),
                    ["contoso", "Widgets", row["repo"]],
                )


class TestReviewVerdictsAreRefused(unittest.TestCase):
    """Azure's reviewer vote cannot be bound to the commit it was formed on.

    The vote attaches to the pull request, not to a revision, so a push landing
    between the route's head-moved check and the vote would apply the verdict to
    code nobody read. Azure resetting votes on push does not close that ordering:
    the reset fires with the push, before the vote arrives.
    """

    OWNER = "contoso/Widgets"
    SHA = "a" * 40

    def test_the_capability_tuple_offers_comment_only(self):
        self.assertEqual(azure_client.PR_REVIEW_EVENTS, ("COMMENT",))

    def test_a_verdict_is_refused_without_spawning(self):
        for verb in ("APPROVE", "REQUEST_CHANGES"):
            with self.subTest(verb=verb):
                spawn = mock.Mock()
                with mock.patch.object(azure_client, "_az_invoke", spawn):
                    with self.assertRaises(ProviderCliError) as caught:
                        azure_client.submit_pr_review(
                            self.OWNER,
                            "widget-service",
                            7,
                            verb,
                            body="looks good",
                            head_sha=self.SHA,
                            host="dev.azure.com",
                        )
                # The message has to name the reason, since the caller otherwise
                # cannot tell this from a transient provider error.
                self.assertIn("not to a revision", str(caught.exception))
                spawn.assert_not_called()

    def test_commenting_still_works(self):
        with mock.patch.object(azure_client, "add_pr_comment", return_value={}) as posted:
            out = azure_client.submit_pr_review(
                self.OWNER,
                "widget-service",
                7,
                "COMMENT",
                body="a note",
                head_sha=self.SHA,
                host="dev.azure.com",
            )
        self.assertEqual(out["state"], "COMMENTED")
        posted.assert_called_once()


class TestRunMutationsCheckBuildOwnership(unittest.TestCase):
    """A build id is PROJECT-scoped, so it must be bound to the repo before use.

    The route's connected-repo gate authorizes repo A and never sees that the id
    points at repo B in the same project, so without this check a cancel or requeue
    lands on another repository's build.
    """

    OWNER = "contoso/Widgets"

    def _invoke(self, build_repo):
        """Fake az where the build read reports ``build_repo`` as its repository."""
        calls: list[str] = []

        def invoke(**kwargs):
            calls.append(str(kwargs.get("method") or "GET"))
            return {
                "id": 99,
                "repository": {"name": build_repo},
                "definition": {"id": 12},
                "sourceBranch": "refs/heads/main",
                "sourceVersion": "b" * 40,
            }

        return invoke, calls

    def test_cancel_refuses_another_repos_build(self):
        invoke, calls = self._invoke("other-repo")
        with mock.patch.object(azure_client, "_az_invoke", side_effect=invoke):
            with self.assertRaises(ProviderCliError) as caught:
                azure_client.cancel_workflow_run(
                    self.OWNER, "widget-service", 99, host="dev.azure.com"
                )
        self.assertIn("project-scoped", str(caught.exception))
        # The read is allowed; the PATCH must never happen.
        self.assertNotIn("PATCH", calls)

    def test_rerun_refuses_another_repos_build(self):
        invoke, calls = self._invoke("other-repo")
        with mock.patch.object(azure_client, "_az_invoke", side_effect=invoke):
            with self.assertRaises(ProviderCliError):
                azure_client.rerun_workflow_run(
                    self.OWNER, "widget-service", 99, host="dev.azure.com"
                )
        self.assertNotIn("POST", calls)

    def test_a_build_with_no_repository_is_refused(self):
        def invoke(**kwargs):
            return {"id": 99, "repository": {}}

        with mock.patch.object(azure_client, "_az_invoke", side_effect=invoke):
            with self.assertRaises(ProviderCliError) as caught:
                azure_client.cancel_workflow_run(
                    self.OWNER, "widget-service", 99, host="dev.azure.com"
                )
        self.assertIn("could not determine", str(caught.exception))

    def test_a_case_variant_repo_name_is_refused(self):
        # Azure repository names are not documented as case-insensitive, so folding
        # here could accept a different repository.
        invoke, calls = self._invoke("Widget-Service")
        with mock.patch.object(azure_client, "_az_invoke", side_effect=invoke):
            with self.assertRaises(ProviderCliError):
                azure_client.cancel_workflow_run(
                    self.OWNER, "widget-service", 99, host="dev.azure.com"
                )
        self.assertNotIn("PATCH", calls)

    def test_the_matching_repo_is_allowed_through(self):
        invoke, calls = self._invoke("widget-service")
        with mock.patch.object(azure_client, "_az_invoke", side_effect=invoke):
            out = azure_client.cancel_workflow_run(
                self.OWNER, "widget-service", 99, host="dev.azure.com"
            )
        self.assertEqual(out, {"run_id": 99, "cancelled": True})
        self.assertIn("PATCH", calls)


class TestCommentBodiesAreRedacted(unittest.TestCase):
    """A comment body is often model-authored, and posting it is irreversible.

    Publishing puts the text somewhere public and permanent, so a credential or an
    exfiltration URL that slips through cannot be walked back. Both redactions run
    because they catch different things.
    """

    OWNER = "contoso/Widgets"

    def _posted_text(self, poster, body):
        """The text that actually reached az for ``body``."""
        seen: dict[str, object] = {}

        def invoke(**kwargs):
            seen["body"] = kwargs.get("body")
            return {"id": 1, "createdDate": "2026-01-01T00:00:00Z"}

        with mock.patch.object(azure_client, "_az_invoke", side_effect=invoke):
            poster(self.OWNER, "widget-service", 5, body, host="dev.azure.com")
        return seen["body"]

    def test_a_credential_is_not_published_on_a_work_item(self):
        sent = self._posted_text(
            azure_client.add_issue_comment, "token is ghp_0123456789abcdefghijklmnopqrstuvwxyz"
        )
        self.assertNotIn("ghp_0123456789abcdefghijklmnopqrstuvwxyz", str(sent))

    def test_a_credential_is_not_published_on_a_pull_request(self):
        sent = self._posted_text(
            azure_client.add_pr_comment, "token is ghp_0123456789abcdefghijklmnopqrstuvwxyz"
        )
        self.assertNotIn("ghp_0123456789abcdefghijklmnopqrstuvwxyz", str(sent))

    def test_ordinary_prose_survives_intact(self):
        # Redaction must not mangle a normal comment, or the feature is unusable.
        sent = self._posted_text(azure_client.add_issue_comment, "This looks correct to me.")
        self.assertIn("This looks correct to me.", str(sent))

    def test_an_empty_body_is_still_refused(self):
        for poster in (azure_client.add_issue_comment, azure_client.add_pr_comment):
            with self.subTest(poster=poster.__name__):
                spawn = mock.Mock()
                with mock.patch.object(azure_client, "_az_invoke", spawn):
                    with self.assertRaises(ProviderCliError):
                        poster(self.OWNER, "widget-service", 5, "   ", host="dev.azure.com")
                spawn.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
