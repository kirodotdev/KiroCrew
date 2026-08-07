"""Provider identity and dispatch for Issue Radar.

A connected repo on GitHub is fully identified by ``(owner, repo)``. GitLab adds
two dimensions:

``provider``
    ``"github"`` or ``"gitlab"``. Decides which client module runs.
``host``
    ``github.com``, ``gitlab.com``, or an allowlisted self-managed GitLab
    ``host[:port]``. A self-managed instance is a genuinely different universe:
    the same ``group/project`` path exists on gitlab.com and on every private
    instance, so the host is part of the identity, not decoration.

Both default to GitHub everywhere -- on the wire, in ``config.json``, in cache
paths, and in every function signature, so the additive design holds: an install
that has been triaging GitHub issues for months keeps its connected repos, its
caches, and its investigation ledger untouched, and a frontend that never sends
``provider`` keeps working.

``owner`` carries GitLab's full namespace, which may be nested
(``group/subgroup``). It is not split further because GitLab treats the whole
``namespace/project`` path as the project's address.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urlparse

from . import github_client, gitlab_client
from .errors import RepoUrlError

GITHUB = "github"
GITLAB = "gitlab"
PROVIDERS = (GITHUB, GITLAB)

DEFAULT_PROVIDER = GITHUB
DEFAULT_HOST = "github.com"


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
        """The project's page on its host."""
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

    GitHub Enterprise is NOT supported, so a GitHub key's host is pinned to
    ``github.com`` regardless of what the client sent -- otherwise a crafted host
    would become part of a cache path and of the identity used to look a repo up.

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
    if provider == GITHUB:
        return DEFAULT_HOST
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


def parse_repo_url(link: str) -> RepoKey:
    """Parse any supported repository URL into a :class:`RepoKey`.

    Dispatches on the URL's PARSED HOST: github.com goes to
    ``github_client.parse_github_repo_url``, everything else is offered to
    ``gitlab_client.parse_gitlab_repo_url`` with the operator's allowlist. Both
    raise :class:`RepoUrlError` on rejection, which the connect route maps to a
    400.

    The host is compared exactly, never matched as a substring. A substring test
    (``"://github.com/" in url``) routes on text that can appear ANYWHERE in the
    URL — in a path segment, a query parameter, or userinfo — so
    ``https://gitlab.example/x?u=://github.com/o/r`` would be handed to the GitHub
    parser. The GitHub parser re-validates the host and rejects it, so this
    is not an SSRF, but it would mean a legitimate GitLab URL containing that text
    is refused with a GitHub-specific error instead of being parsed as GitLab.
    Parsing once and comparing the host is both correct and what every other
    host check in this app already does.

    GitLab is tried second and only for non-github.com hosts, so a GitHub URL can
    never be mis-attributed, and the error a user sees for a bad github.com URL
    stays GitHub-specific rather than becoming a confusing "not a GitLab host".
    """
    if not link or not isinstance(link, str):
        raise RepoUrlError("repo link is empty")
    # `hostname` parses the authority lazily, so a malformed one raises here
    # rather than in urlparse; both are client input and become RepoUrlError.
    try:
        host = (urlparse(link.strip()).hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise RepoUrlError(f"unparseable URL: {link!r}") from exc
    if host in {"github.com", "www.github.com"}:
        owner, repo = github_client.parse_github_repo_url(link)
        return RepoKey(provider=GITHUB, host=DEFAULT_HOST, owner=owner, repo=repo)
    namespace_host, namespace, project = gitlab_client.parse_gitlab_repo_url(
        link, allowed_hosts=gitlab_client.allowed_hosts()
    )
    return RepoKey(provider=GITLAB, host=namespace_host, owner=namespace, repo=project)


class ProviderClient(Protocol):
    """The read/write surface Issue Radar's routes require of a provider.

    Both ``github_client`` and ``gitlab_client`` satisfy this as MODULES, so the
    dispatch below is a module lookup rather than an object graph -- there is no
    state to hold, and keeping the clients as plain modules means each one stays
    independently readable and testable.

    Because a module cannot be statically checked against a Protocol, conformance
    is asserted by a test that compares every member's signature across both
    modules (``test_provider_parity``). That test is the real gate; this Protocol
    is what it checks against and what call sites are type-checked against.
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
    # Both clients also accept a keyword-only ``resolve_mergeable: bool = True``;
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

    def create_label(
        self, owner: str, repo: str, name: str, color: str = ..., description: str = ..., **kwargs: object
    ) -> dict: ...

    # Pull-request actions. Every one is a WRITE and is gated on the same
    # triage/push access as the issue writes above. The providers differ in what
    # they can express (GitLab has no "request changes" verb; its auto-merge is
    # "merge when pipeline succeeds"), and each client REFUSES what it cannot
    # honour rather than approximating it -- see the sections in both clients.
    def set_pr_state(
        self, owner: str, repo: str, number: int, state: str, **kwargs: object
    ) -> dict: ...

    # ``head_sha`` is REQUIRED in practice (both clients refuse an empty one) for the
    # same reason merge_pull_request needs it: a verdict must name the revision it
    # was formed on. It has a default only so the two module signatures stay
    # identical for the parity gate.
    def submit_pr_review(
        self, owner: str, repo: str, number: int, event: str, body: str = ...,
        head_sha: str = ..., **kwargs: object
    ) -> dict: ...

    def add_issue_comment(
        self, owner: str, repo: str, number: int, body: str, **kwargs: object
    ) -> dict: ...

    # Separate from add_issue_comment because GitLab numbers issues and merge
    # requests independently -- see both clients' add_pr_comment.
    def add_pr_comment(
        self, owner: str, repo: str, number: int, body: str, **kwargs: object
    ) -> dict: ...

    # Merging comes in two forms and NEITHER can bypass a gate: the provider
    # adjudicates branch protection / approval rules on both endpoints. See
    # github_client.merge_pull_request.
    # ``head_sha`` is REQUIRED in practice (both clients refuse an empty one); it has
    # a default only so the two module signatures stay identical for the parity gate.
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
}


def client_for(key: RepoKey) -> ProviderClient:
    """The client module that serves ``key``.

    An unknown provider falls back to GitHub, which cannot leak: a GitHub client
    call carries no host parameter and ``gh`` is pinned to github.com, so a
    corrupted config entry degrades to a failed GitHub lookup rather than
    reaching an unintended server.
    """
    return _CLIENTS.get(key.provider, _CLIENTS[GITHUB])


def call_kwargs(key: RepoKey) -> dict[str, str]:
    """Provider-specific keyword arguments for a client call.

    GitLab needs ``host`` on every call (it is required, never defaulted -- see
    ``gitlab_client``'s module docstring); GitHub takes none. Centralizing this
    means a route never has to remember which provider needs what, and a future
    provider adds its own parameters here rather than in 38 handlers.
    """
    if key.provider == GITLAB:
        return {"host": key.host}
    return {}


# ── display vocabulary ───────────────────────────────────────────────────────
#
# "Pull request" is GitHub's term; GitLab says "merge request". The UI reads
# these so a GitLab project's tabs, empty states, and AI prose say the right
# thing instead of calling everything a PR. Exposed from the backend (rather than
# hard-coded in the frontend) so the AI prompts and the components agree.
_TERMS = {
    GITHUB: {
        "change_request": "pull request",
        "change_request_short": "PR",
        "change_request_sigil": "#",
        "provider_name": "GitHub",
        "cli": "gh",
    },
    GITLAB: {
        "change_request": "merge request",
        "change_request_short": "MR",
        # GitLab addresses merge requests with "!" and issues with "#".
        "change_request_sigil": "!",
        "provider_name": "GitLab",
        "cli": "glab",
    },
}


def terms(key: RepoKey) -> dict[str, str]:
    """Display vocabulary for ``key``'s provider."""
    return _TERMS.get(key.provider, _TERMS[GITHUB])


# ── investigation record namespace ───────────────────────────────────────────

ITEM_KINDS = frozenset({"issue", "pull"})


def investigation_kind(key: RepoKey, item_kind: str) -> str:
    """The storage namespace for an item's investigation record.

    A number alone does not always identify an item. GitHub draws issues and pull
    requests from ONE sequence, so ``#5`` is unambiguous and every existing record
    is correctly keyed by number alone. GitLab keeps two sequences: issue ``#5``
    and merge request ``!5`` are unrelated items, so sharing one record would make
    "Review MR !5" resume issue #5's chat session and overwrite its findings.

    Returns ``"issue"`` -- the historical namespace, and therefore the historical
    filename -- for everything on GitHub and for GitLab issues, and ``"mr"`` for a
    GitLab merge request. Nothing needs migrating, because the only namespace that
    changes is one no record has ever been written under.
    """
    if key.provider == GITLAB and item_kind == "pull":
        return "mr"
    return "issue"
