"""Provider identity and dispatch for Issue Radar.

A connected repo on GitHub is fully identified by ``(owner, repo)``. GitLab and
Azure DevOps add two dimensions:

``provider``
    ``"github"``, ``"gitlab"`` or ``"azure"``. Decides which client module runs.
``host``
    ``github.com``, ``dev.azure.com``, ``gitlab.com``, or an allowlisted
    self-managed GitLab ``host[:port]``. A self-managed instance is a genuinely
    different universe: the same ``group/project`` path exists on gitlab.com and
    on every private instance, so the host is part of the identity, not
    decoration.

Both default to GitHub everywhere -- on the wire, in ``config.json``, in cache
paths, and in every function signature, so the additive design holds: an install
that has been triaging GitHub issues for months keeps its connected repos, its
caches, and its investigation ledger untouched, and a frontend that never sends
``provider`` keeps working.

``owner`` carries the whole namespace above the repository, which may be nested.
On GitLab that is the group path (``group/subgroup``); on Azure DevOps it is
``organization/project``. It is not split further because both providers treat
the whole path as the project's address, and keeping one field means the storage
layout, the connected-repo gate and every route signature stay unchanged.

Azure DevOps differs from both in a way no amount of field-shuffling hides:
**work items are project-scoped and carry no repository dimension at all**. There
is no "issues in this repository" question to ask, so an Azure repo's issue list
is its PROJECT's work item list, and two repositories in one project legitimately
show the same items. The UI says so rather than implying a per-repo list. Pull
requests have no such problem -- they are repository-scoped like everywhere else.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urlparse

from . import azure_client, github_client, gitlab_client
from .errors import RepoUrlError

GITHUB = "github"
GITLAB = "gitlab"
AZURE = "azure"
PROVIDERS = (GITHUB, GITLAB, AZURE)

DEFAULT_PROVIDER = GITHUB
DEFAULT_HOST = "github.com"
AZURE_HOST = "dev.azure.com"

# Hosts that are a fixed property of the provider rather than operator input.
# A pinned host cannot be influenced by a request, which matters because the host
# becomes part of a cache path and part of the identity a repo is looked up by.
# GitHub Enterprise and on-premises Azure DevOps Server are both unsupported, and
# both are unsupported for the same reason: their client would need a different
# credential path than the vendor CLI provides.
_PINNED_HOSTS = {
    GITHUB: DEFAULT_HOST,
    AZURE: AZURE_HOST,
}


@dataclass(frozen=True)
class RepoKey:
    """The full identity of a connected repository/project.

    Frozen so it can be a dict key and cannot be mutated halfway through a
    request -- the host in particular decides which server a credential-bearing
    CLI talks to, so it must not be reassignable after validation.
    """

    provider: str = DEFAULT_PROVIDER
    host: str = DEFAULT_HOST
    owner: str = ""
    repo: str = ""

    @property
    def slug(self) -> str:
        """``owner/repo`` -- what a human reads and what error strings show."""
        return f"{self.owner}/{self.repo}"

    @property
    def is_github(self) -> bool:
        return self.provider == GITHUB

    @property
    def is_default_host(self) -> bool:
        """Whether this is public GitHub, which owns the legacy storage layout."""
        return self.provider == GITHUB and self.host == DEFAULT_HOST

    def web_url(self) -> str:
        """The project's page on its host.

        Azure DevOps puts a literal ``_git`` between the project and the
        repository, because a project holds repositories alongside boards,
        pipelines and artifacts and the segment is what disambiguates them. Every
        other provider's repository page is just the namespace path.
        """
        if self.provider == AZURE:
            return f"https://{self.host}/{self.owner}/_git/{self.repo}"
        return f"https://{self.host}/{self.owner}/{self.repo}"


def normalize_provider(raw: object) -> str:
    """Coerce a client-supplied provider to a known value.

    Anything unrecognized -- including ``None`` from a request that predates this
    feature -- becomes ``github``. Defaulting rather than raising is deliberate:
    the value is only ever a routing hint, every path it selects is independently
    authorized, and rejecting an absent field would break the existing frontend.
    """
    text = str(raw or "").strip().lower()
    return text if text in PROVIDERS else DEFAULT_PROVIDER


def normalize_host(raw: object, provider: str) -> str:
    """Coerce a client-supplied host, defaulting per provider.

    A provider in ``_PINNED_HOSTS`` has its host replaced by that constant
    regardless of what the client sent -- otherwise a crafted host would become
    part of a cache path and of the identity used to look a repo up. GitHub
    Enterprise and on-premises Azure DevOps Server are both out of scope, so both
    providers are pinned to their public host. Azure's legacy
    ``{org}.visualstudio.com`` form is accepted when PARSING a pasted URL and
    canonicalized to ``dev.azure.com`` here, so one organization cannot end up
    with two identities and two cache trees.

    A GitLab host is lowercased and trailing-dot-stripped to match the allowlist's
    canonical form, but a MISSING one stays empty rather than becoming
    ``gitlab.com``. Defaulting it would silently retarget the request: with a
    same-slug gitlab.com project connected, a request that omitted ``host``
    (a hand-written call, or a frontend regression) would pass the
    connected-repo gate against THAT project and let a write land on a repository
    the caller never named. An empty host matches no connected record (404) and is
    refused at the spawn boundary by ``gitlab_client._resolve_host``, which is the
    invariant that function's docstring already claims -- this is what makes it
    true. It is NOT authorized here: authorization happens at the spawn boundary,
    so every call re-checks it.
    """
    pinned = _PINNED_HOSTS.get(provider)
    if pinned is not None:
        return pinned
    return str(raw or "").strip().lower().rstrip(".")


def key_from_parts(owner: str, repo: str, provider: object = None, host: object = None) -> RepoKey:
    """Build a :class:`RepoKey` from loose request/config values."""
    resolved_provider = normalize_provider(provider)
    return RepoKey(
        provider=resolved_provider,
        host=normalize_host(host, resolved_provider),
        owner=owner,
        repo=repo,
    )


_GITHUB_URL_HOSTS = frozenset({"github.com", "www.github.com"})
_AZURE_URL_HOSTS = frozenset({"dev.azure.com"})
# Azure's legacy per-organization form. Matched as a HOST SUFFIX on the parsed
# hostname (never as a substring of the URL), so ``myorg.visualstudio.com``
# resolves while a path or query bearing the same text does not.
_AZURE_LEGACY_HOST_SUFFIX = ".visualstudio.com"


def _provider_for_url_host(host: str) -> str:
    """Which provider owns a parsed URL hostname.

    GitLab is the fallback rather than an entry here: its hosts are operator
    configuration, so it is the only provider that cannot be recognized from a
    fixed table. Keeping it last also preserves the error a user sees for a
    malformed github.com URL, which stays GitHub-specific instead of becoming a
    confusing "not a GitLab host".
    """
    if host in _GITHUB_URL_HOSTS:
        return GITHUB
    if host in _AZURE_URL_HOSTS or host.endswith(_AZURE_LEGACY_HOST_SUFFIX):
        return AZURE
    return GITLAB


def parse_repo_url(link: str) -> RepoKey:
    """Parse any supported repository URL into a :class:`RepoKey`.

    Dispatches on the URL's PARSED HOST via :func:`_provider_for_url_host`, then
    hands the link to that provider's own parser. Each raises
    :class:`RepoUrlError` on rejection, which the connect route maps to a 400.

    The host is compared exactly, never matched as a substring. A substring test
    (``"://github.com/" in url``) routes on text that can appear ANYWHERE in the
    URL — in a path segment, a query parameter, or userinfo — so
    ``https://gitlab.example/x?u=://github.com/o/r`` would be handed to the GitHub
    parser. The GitHub parser re-validates the host and rejects it, so this
    is not an SSRF, but it would mean a legitimate GitLab URL containing that text
    is refused with a GitHub-specific error instead of being parsed as GitLab.
    Parsing once and comparing the host is both correct and what every other
    host check in this app already does.
    """
    if not link or not isinstance(link, str):
        raise RepoUrlError("repo link is empty")
    # `hostname` parses the authority lazily, so a malformed one raises here
    # rather than in urlparse; both are client input and become RepoUrlError.
    try:
        host = (urlparse(link.strip()).hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise RepoUrlError(f"unparseable URL: {link!r}") from exc
    resolved = _provider_for_url_host(host)
    if resolved == GITHUB:
        owner, repo = github_client.parse_github_repo_url(link)
        return RepoKey(provider=GITHUB, host=DEFAULT_HOST, owner=owner, repo=repo)
    if resolved == AZURE:
        # The organization is canonicalized into ``owner`` and the host into
        # ``dev.azure.com``, so the legacy visualstudio.com form and the modern
        # one are the same identity rather than two cache trees.
        namespace, project_repo = azure_client.parse_azure_repo_url(link)
        return RepoKey(provider=AZURE, host=AZURE_HOST, owner=namespace, repo=project_repo)
    namespace_host, namespace, project = gitlab_client.parse_gitlab_repo_url(
        link, allowed_hosts=gitlab_client.allowed_hosts()
    )
    return RepoKey(provider=GITLAB, host=namespace_host, owner=namespace, repo=project)


class ProviderClient(Protocol):
    """The read/write surface Issue Radar's routes require of a provider.

    ``github_client``, ``gitlab_client`` and ``azure_client`` each satisfy this as
    a MODULE, so the dispatch below is a module lookup rather than an object graph
    -- there is no state to hold, and keeping the clients as plain modules means
    each one stays independently readable and testable.

    Because a module cannot be statically checked against a Protocol, conformance
    is asserted by a test that compares every member's signature across every
    registered client (``TestClientParity``, which measures each module against
    GitHub as the reference and additionally pins its own table against
    ``PROVIDERS``, so a provider cannot join the dispatch without joining the
    gate). That test is the real gate; this Protocol is what it checks against and
    what call sites are type-checked against.
    """

    # Reads
    def verify_repo_access(self, owner: str, repo: str, **kwargs: object) -> dict: ...
    def get_repo_permissions(self, owner: str, repo: str, **kwargs: object) -> dict: ...
    def list_open_issues(self, owner: str, repo: str, **kwargs: object) -> list[dict]: ...
    # The newest single page of open issues in ONE request — the progressive
    # first paint on a cold cache, before the fully-paginated list_open_issues
    # returns. Same shape and order, so the full set appends behind it.
    def list_open_issues_first_page(self, owner: str, repo: str, **kwargs: object) -> list[dict]: ...
    def list_closed_issues(self, owner: str, repo: str, **kwargs: object) -> list[dict]: ...

    def list_recent_open_issues(
        self, owner: str, repo: str, limit: int = ..., **kwargs: object
    ) -> list[dict]: ...
    def list_repo_labels(self, owner: str, repo: str, **kwargs: object) -> list[dict]: ...
    def list_repo_collaborators(self, owner: str, repo: str, **kwargs: object) -> list[dict]: ...
    def derive_members(self, issues: list[dict]) -> list[dict]: ...
    def get_current_login(self, **kwargs: object) -> str | None: ...

    def list_contributed_repos(
        self, login: str, **kwargs: object
    ) -> tuple[list[dict], bool]: ...
    def get_issue_detail(self, owner: str, repo: str, number: int, **kwargs: object) -> dict: ...
    def list_issue_timeline(self, owner: str, repo: str, number: int, **kwargs: object) -> list[dict]: ...
    def list_pr_timeline(self, owner: str, repo: str, number: int, **kwargs: object) -> list[dict]: ...
    def list_open_pulls(self, owner: str, repo: str, **kwargs: object) -> list[dict]: ...
    def list_open_pulls_first_page(self, owner: str, repo: str, **kwargs: object) -> list[dict]: ...
    def list_closed_pulls(self, owner: str, repo: str, **kwargs: object) -> list[dict]: ...
    # Every client also accepts a keyword-only ``resolve_mergeable: bool = True``;
    # ``False`` skips GitHub's lazy-mergeability retry+sleep for a caller that reads
    # only an eager field (head_sha), and is a no-op on GitLab. It is left inside
    # ``**kwargs`` here — exactly as provider-specific kwargs like ``host`` are —
    # rather than declared with a ``bool`` type, because a declared ``bool`` keyword
    # collides with unpacking ``call_kwargs()`` (a ``dict[str, str]``) at the call
    # sites that do not pass it. The real module signatures are what the parity gate
    # compares. See github_client.get_pr_detail.
    def get_pr_detail(self, owner: str, repo: str, number: int, **kwargs: object) -> dict: ...
    def list_pr_checks(self, owner: str, repo: str, sha: str, **kwargs: object) -> list[dict]: ...
    def summarize_checks(self, checks: list[dict]) -> dict: ...
    def enrich_pulls(self, owner: str, repo: str, pulls: list[dict], state: str, **kwargs: object) -> list[dict]: ...
    def enrich_pulls_by_number(self, owner: str, repo: str, pulls: list[dict], **kwargs: object) -> list[dict]: ...
    def enrichment_complete(self, pulls: list[dict]) -> bool: ...

    # The cheap open-list probe that gates list polling. GitHub answers it from
    # one search call; GitLab serves issues from an exact count and refuses the
    # merge-request kind rather than approximating it.
    def probe_open_list(self, owner: str, repo: str, kind: str, **kwargs: object) -> dict: ...

    def get_ref_summary(
        self, owner: str, repo: str, number: int, **kwargs: object
    ) -> dict: ...

    def search_pulls(self, owner: str, repo: str, **kwargs: object) -> list[dict]: ...

    # Writes
    def add_issue_labels(
        self, owner: str, repo: str, number: int, labels: list[str], **kwargs: object
    ) -> list[dict]: ...

    def remove_issue_label(
        self, owner: str, repo: str, number: int, label: str, **kwargs: object
    ) -> list[dict] | None: ...

    def set_issue_state(
        self, owner: str, repo: str, number: int, state: str, state_reason: str | None = ..., **kwargs: object
    ) -> dict: ...

    # Assignees are REPLACED wholesale (not add/remove), so the editor supplies
    # the final set and both clients read the authoritative result back: GitHub
    # drops non-assignable logins and caps at 10, GitLab Free keeps only the
    # first. The return is the usernames that actually stuck.
    def set_issue_assignees(
        self, owner: str, repo: str, number: int, assignees: list[str], **kwargs: object
    ) -> list[str]: ...

    def create_label(
        self, owner: str, repo: str, name: str, color: str = ..., description: str = ..., **kwargs: object
    ) -> dict: ...

    # Pull-request actions. Every one is a WRITE and is gated on the same
    # triage/push access as the issue writes above. The providers differ in what
    # they can express, and each client REFUSES what it cannot honour rather than
    # approximating it: GitLab has no "request changes" verb and no reversible
    # auto-merge arming (its flag rides the merge call as "merge when pipeline
    # succeeds"), so both raise there. Azure DevOps has a genuinely reversible
    # arming verb, so it implements that one, but refuses BOTH review verdicts --
    # its reviewer vote attaches to the pull request rather than to a revision, so
    # a verdict cannot be bound to the commit it was formed on. Each client's
    # ``PR_REVIEW_EVENTS`` is the authority on what it accepts.
    def set_pr_state(
        self, owner: str, repo: str, number: int, state: str, **kwargs: object
    ) -> dict: ...

    # ``head_sha`` is REQUIRED in practice (every client refuses an empty one) for
    # the same reason merge_pull_request needs it: a verdict must name the revision
    # it was formed on. It has a default only so the module signatures stay
    # identical for the parity gate.
    def submit_pr_review(
        self, owner: str, repo: str, number: int, event: str, body: str = ...,
        head_sha: str = ..., **kwargs: object
    ) -> dict: ...

    def add_issue_comment(
        self, owner: str, repo: str, number: int, body: str, **kwargs: object
    ) -> dict: ...

    # Separate from add_issue_comment because GitLab and Azure DevOps both number
    # their tracked items and their change requests independently -- see each
    # client's add_pr_comment.
    def add_pr_comment(
        self, owner: str, repo: str, number: int, body: str, **kwargs: object
    ) -> dict: ...

    # Merging comes in two forms and NEITHER can bypass a gate: the provider
    # adjudicates branch protection / approval rules on both endpoints. See
    # github_client.merge_pull_request.
    # ``head_sha`` is REQUIRED in practice (every client refuses an empty one); it has
    # a default only so the module signatures stay identical for the parity gate.
    def merge_pull_request(
        self, owner: str, repo: str, number: int, method: str = ..., head_sha: str = ...,
        **kwargs: object
    ) -> dict: ...

    def enable_auto_merge(
        self, owner: str, repo: str, number: int, method: str = ..., **kwargs: object
    ) -> dict: ...

    def disable_auto_merge(
        self, owner: str, repo: str, number: int, **kwargs: object
    ) -> dict: ...

    def list_pr_workflow_runs(
        self, owner: str, repo: str, sha: str, **kwargs: object
    ) -> list[dict]: ...

    def cancel_workflow_run(
        self, owner: str, repo: str, run_id: int, **kwargs: object
    ) -> dict: ...

    def rerun_workflow_run(
        self, owner: str, repo: str, run_id: int, **kwargs: object
    ) -> dict: ...


_CLIENTS: dict[str, ProviderClient] = {
    GITHUB: cast(ProviderClient, github_client),
    GITLAB: cast(ProviderClient, gitlab_client),
    AZURE: cast(ProviderClient, azure_client),
}


def client_for(key: RepoKey) -> ProviderClient:
    """The client module that serves ``key``.

    An unknown provider falls back to GitHub, which cannot leak: a GitHub client
    call carries no host parameter and ``gh`` is pinned to github.com, so a
    corrupted config entry degrades to a failed GitHub lookup rather than
    reaching an unintended server.
    """
    return _CLIENTS.get(key.provider, _CLIENTS[GITHUB])


# Providers whose client needs the target host on every call. GitHub is pinned to
# github.com inside its own runner and takes none.
_HOST_BEARING_PROVIDERS = frozenset({GITLAB, AZURE})


def call_kwargs(key: RepoKey) -> dict[str, str]:
    """Provider-specific keyword arguments for a client call.

    GitLab and Azure DevOps both take ``host`` on every call (for GitLab it is
    required and never defaulted -- see ``gitlab_client``'s module docstring;
    for Azure it is pinned but still re-checked at the spawn boundary, so that a
    corrupted config entry fails closed instead of reaching another host).
    GitHub takes none. Centralizing this means a route never has to remember which
    provider needs what, and a future provider adds its own parameters here rather
    than in 38 handlers.
    """
    if key.provider in _HOST_BEARING_PROVIDERS:
        return {"host": key.host}
    return {}


# ── display vocabulary ───────────────────────────────────────────────────────
#
# "Pull request" is GitHub's term; GitLab says "merge request". The tracked-item
# noun diverges too: Azure DevOps has no "issue" primitive at all, only work
# items. The UI reads these so a project's tabs, empty states, and AI prose say
# the right thing instead of calling everything a PR or an issue. Exposed from the
# backend (rather than hard-coded in the frontend) so the AI prompts and the
# components agree.
_TERMS = {
    GITHUB: {
        "change_request": "pull request",
        "change_request_short": "PR",
        "change_request_sigil": "#",
        "tracked_item": "issue",
        "tracked_item_plural": "issues",
        "provider_name": "GitHub",
        "cli": "gh",
    },
    GITLAB: {
        "change_request": "merge request",
        "change_request_short": "MR",
        # GitLab addresses merge requests with "!" and issues with "#".
        "change_request_sigil": "!",
        "tracked_item": "issue",
        "tracked_item_plural": "issues",
        "provider_name": "GitLab",
        "cli": "glab",
    },
    AZURE: {
        "change_request": "pull request",
        "change_request_short": "PR",
        # Azure DevOps markdown addresses pull requests with "!" and work items
        # with "#", the same split as GitLab and for the same reason: the two
        # sequences are independent.
        "change_request_sigil": "!",
        # "Issue" is one work item TYPE in some process templates, not the
        # category, so using it for the category would name a filter the user did
        # not apply.
        "tracked_item": "work item",
        "tracked_item_plural": "work items",
        "provider_name": "Azure DevOps",
        "cli": "az",
    },
}


def terms(key: RepoKey) -> dict[str, str]:
    """Display vocabulary for ``key``'s provider."""
    return _TERMS.get(key.provider, _TERMS[GITHUB])


# The page that lists a project's TRACKED ITEMS, per provider. A table rather
# than a branch, because the three shapes have nothing in common to fall back
# between: GitHub hangs the list off the repository path, GitLab inserts its
# ``/-/`` route separator, and Azure DevOps has no repository dimension in the
# address at all -- work items belong to the PROJECT, which ``owner`` already
# carries as ``{org}/{project}``, so ``repo`` does not appear in the URL and
# ``web_url()`` (which ends in ``_git/{repo}``) is not its prefix.
#
# The previous binary ``"/-/issues" if not is_github else "/issues"`` is exactly
# what a table prevents: it has no third branch to take, so a provider that is
# not GitHub silently inherits GitLab's URL shape and the notification links
# somewhere that does not exist.
_TRACKED_ITEMS_URL: dict[str, Callable[[RepoKey], str]] = {
    GITHUB: lambda key: f"{key.web_url()}/issues",
    GITLAB: lambda key: f"{key.web_url()}/-/issues",
    AZURE: lambda key: f"https://{key.host}/{key.owner}/_workitems/",
}


def tracked_items_url(key: RepoKey) -> str:
    """The web page listing ``key``'s issues / work items.

    Used as the fallback link on a notification that names more items than it can
    link individually. Falls back to GitHub's shape for an unknown provider, for
    the same reason :func:`client_for` does: a corrupted config entry should
    degrade to a wrong-looking link, not to another provider's URL layout.
    """
    return _TRACKED_ITEMS_URL.get(key.provider, _TRACKED_ITEMS_URL[GITHUB])(key)


# ── investigation record namespace ───────────────────────────────────────────

ITEM_KINDS = frozenset({"issue", "pull"})

# The namespace a provider's CHANGE REQUESTS are recorded under. GitHub is absent
# because it draws issues and pull requests from one sequence, so its pull
# requests keep the historical "issue" namespace and no record moves.
_CHANGE_REQUEST_KINDS = {
    GITLAB: "mr",
    AZURE: "pr",
}


def investigation_kind(key: RepoKey, item_kind: str) -> str:
    """The storage namespace for an item's investigation record.

    A number alone does not always identify an item. GitHub draws issues and pull
    requests from ONE sequence, so ``#5`` is unambiguous and every existing record
    is correctly keyed by number alone. GitLab and Azure DevOps each keep two
    independent sequences -- issue ``#5`` and merge request ``!5`` are unrelated
    items, and an Azure work item and pull request numbered the same are allocated
    by different services entirely -- so sharing one record would make
    "Review !5" resume issue #5's chat session and overwrite its findings.

    Returns ``"issue"`` -- the historical namespace, and therefore the historical
    filename -- for everything on GitHub and for the tracked items of every
    provider. Nothing needs migrating, because the only namespaces that change are
    ones no record has ever been written under.
    """
    if item_kind == "pull":
        return _CHANGE_REQUEST_KINDS.get(key.provider, "issue")
    return "issue"
