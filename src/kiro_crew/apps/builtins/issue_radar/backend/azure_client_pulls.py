"""Pull-request reads on Azure DevOps.

The open and closed lists and their first page, one PR's detail with its
mergeability and the merge-strategy vocabulary it reports, the policy evaluations
and builds read as checks, the list-card enrichment and the server-side search.
Callers reach every name here through
:mod:`kiro_crew.apps.builtins.issue_radar.backend.azure_client`.
"""

from __future__ import annotations

from . import azure_normalization as _normalization
from .azure_client_cli import (
    _API_BUILD,
    _API_GIT,
    _API_POLICY,
    _BUILD_SCAN_TOP,
    _ENRICH_MAX_PULLS,
    _GUID_RE,
    _LOGIN_RE,
    _PAGE_SIZE,
    _PR_LOOKUP_TOP,
    _SHA_RE,
    AZ_PAGINATE_TIMEOUT_SEC,
    AZ_TIMEOUT_SEC,
    PR_SEARCH_MAX,
    _az_invoke,
    _az_invoke_paged,
    _check_repo,
    _identity_id,
    _identity_login,
    _obj,
    _shape_labels,
    _split_owner,
    _values,
)
from .azure_client_repo_reads import _identity_matches, _iter_project_identities
from .azure_client_work_items import _project_id
from .errors import ProviderCliError, PrSearchError

# ── pull requests ───────────────────────────────────────────────────────────


_branch_name = _normalization._branch_name
_pr_labels = _normalization._pr_labels
_norm_pull = _normalization._norm_pull
_pr_web_url = _normalization._pr_web_url
_org_from_api_url = _normalization._org_from_api_url


def _list_pulls(
    owner: str, repo: str, state: str, *, host: str, timeout: float, paginate: bool
) -> list[dict]:
    """List pull requests of ``state``, newest first.

    ``searchCriteria.status`` is passed EXPLICITLY on every call: Azure defaults it
    to ``active`` server-side, so an omitted status would silently return only
    open PRs on the closed tab. The app's "closed" means "not open", which
    covers both ``completed`` and ``abandoned`` -- two separate Azure statuses --
    so the closed listing asks for ``all`` and filters, rather than hiding every
    abandoned PR (or every merged one).
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    status = "active" if state == "open" else "all"
    # A closed list is a FILTER over a mixed page, so the cap has to come AFTER the
    # filter. Capping the fetch instead returns an empty closed history whenever the
    # newest page happens to be all active pull requests -- Azure has no
    # closed-only status, so `all` is the only way to ask.
    fetch_everything = paginate or state != "open"
    rows = _az_invoke_paged(
        org=org,
        area="git",
        resource="pullRequests",
        host=host,
        timeout=timeout,
        route={"project": project, "repositoryId": name},
        query={"searchCriteria.status": status},
        api_version=_API_GIT,
        limit=0 if fetch_everything else _PAGE_SIZE,
    )
    out = [_norm_pull(row) for row in rows]
    if state != "open":
        out = [row for row in out if row.get("state") == "closed"]
        if not paginate:
            out = out[:_PAGE_SIZE]
    return out


def list_open_pulls(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Every OPEN (active) pull request."""
    return _list_pulls(owner, repo, "open", host=host, timeout=timeout, paginate=True)


def list_open_pulls_first_page(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """The newest single page of OPEN pull requests, in ONE request.

    The progressive first paint on a cold cache: the same leading rows
    :func:`list_open_pulls` returns, so the full set appends behind it with no
    reordering. The rows are UNENRICHED (no check state) -- Azure needs a per-PR
    call for that -- so the first-paint route returns them as-is and the
    authoritative fetch enriches.
    """
    return _list_pulls(owner, repo, "open", host=host, timeout=timeout, paginate=False)


def list_closed_pulls(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """The most recently created CLOSED pull requests -- completed and abandoned."""
    return _list_pulls(owner, repo, "closed", host=host, timeout=timeout, paginate=False)


# Azure's ``mergeStatus`` speaks ONLY to whether the branches conflict, which makes
# it the exact analogue of GitHub's weak ``mergeable`` field -- and NOT of a merge
# gate. Anything not yet computed is reported as unknown (None) rather than as
# not-mergeable, so the UI does not flash a false conflict warning mid-computation.
_MERGE_SUCCEEDED = _normalization._MERGE_SUCCEEDED
_MERGE_PENDING = _normalization._MERGE_PENDING
_mergeable = _normalization._mergeable


# Azure supports all three history shapes as a per-request completion option, so
# this tuple is the full set -- unlike GitLab, where merge-vs-rebase is a project
# setting and REBASE has to be refused.
PR_MERGE_METHODS = ("MERGE", "SQUASH", "REBASE")

# The app's method -> Azure's ``completionOptions.mergeStrategy``.
#
# ``MERGE`` maps to ``noFastForward``, which is what "always create a merge commit"
# is called on Azure. ``REBASE`` maps to ``rebase`` (replay then fast-forward), NOT
# to ``rebaseMerge`` (replay then still create a merge commit) -- the two produce
# different histories, and the caller asking for a rebase means the linear one.
_MERGE_STRATEGIES = {
    "MERGE": "noFastForward",
    "SQUASH": "squash",
    "REBASE": "rebase",
}
# The reverse map, for reporting an already-armed auto-complete's strategy back in
# the app's vocabulary. ``rebaseMerge`` is included on the read side because Azure's
# own web UI can arm it even though this module never sets it.
_MERGE_STRATEGY_NAMES = {
    "nofastforward": "MERGE",
    "squash": "SQUASH",
    "rebase": "REBASE",
    "rebasemerge": "REBASE",
}


def get_pr_detail(
    owner: str,
    repo: str,
    number: int,
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
    resolve_mergeable: bool = True,
) -> dict:
    """Full detail for one pull request, in ``_PR_DETAIL_JQ``'s shape.

    Diff size is reported as ``None`` rather than ``0``: Azure has no per-PR diff
    statistic, and reading real line counts would mean pulling the whole diff on
    every detail view. The UI already treats those as optional, and a zero would
    present an unread diff as a confident "no changes".

    ``resolve_mergeable`` is load-bearing here rather than parity-only. The value
    the app's merge gate reads (``mergeable_state``) requires a SECOND call -- the
    PR's policy evaluations -- because Azure's own ``mergeStatus`` knows about
    conflicts and nothing else. A caller that only needs an eagerly-returned field
    (``head_sha`` for the head-moved check) passes ``False`` and skips that call,
    which is the same intent the flag has on the GitHub client.
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    pr_id = int(number)
    raw = _obj(
        _az_invoke(
            org=org,
            area="git",
            resource="pullRequests",
            host=host,
            timeout=timeout,
            route={"project": project, "repositoryId": name, "pullRequestId": pr_id},
            api_version=_API_GIT,
        )
    )
    if not raw:
        raise ProviderCliError(f"could not read {owner}/{repo}!{pr_id} on {host}")
    detail = _norm_pull(raw)
    status = str(raw.get("status") or "").lower()
    completion = _obj(raw.get("completionOptions"))
    auto_by = _obj(raw.get("autoCompleteSetBy"))
    detail.update(
        {
            # `labels` is re-shaped, not inherited. _norm_pull produces the LIST-row
            # form (bare names) while the detail contract is objects carrying
            # name/color/description -- github_client's get_pr_detail does the same.
            # Leaving the list shape here makes PrDetail read `.name` off a string
            # and the labels vanish from the pane.
            "labels": _shape_labels(_pr_labels(raw.get("labels"))),
            "commits": None,
            "comments": None,
            "review_comments": None,
            "merged": status == "completed",
            "mergeable": _mergeable(raw),
            "mergeable_state": (
                _mergeable_state(org, project, name, raw, host=host, timeout=timeout)
                if resolve_mergeable
                else "unknown"
            ),
            "merged_by": _identity_login(raw.get("closedBy")) if status == "completed" else None,
            # Azure's auto-complete is a real, reversible armed state -- the identity
            # that armed it is stored on the PR -- so this reports who armed it and
            # which strategy it will use, rather than a guess.
            "auto_merge": (
                {
                    "method": _MERGE_STRATEGY_NAMES.get(
                        str(completion.get("mergeStrategy") or "").lower()
                    ),
                    "enabled_by": _identity_login(auto_by),
                }
                if _identity_id(auto_by)
                else None
            ),
        }
    )
    return detail


# The app's merge gate (``routes._MERGE_ALLOWED_STATES``) admits only
# ``clean`` / ``has_hooks`` / ``mergeable``, and it is right to: a value that means
# "no conflicts" says nothing about unmet policies. Azure's own ``mergeStatus`` is
# exactly that weak value, so ``mergeable`` is reported ONLY when every blocking
# policy evaluation has passed as well -- which is the same claim GitLab's modern
# ``mergeable`` makes. Everything else reports a specific reason, all of which the
# route refuses, leaving the user the auto-complete path where Azure itself decides.
_STATE_DIRTY = "dirty"
_STATE_DRAFT = "draft"
_STATE_BLOCKED = "blocked"
_STATE_CHECKING = "checking"
_STATE_UNKNOWN = "unknown"


def _mergeable_state(
    org: str, project: str, repo: str, raw: dict, *, host: str, timeout: float
) -> str:
    """The merge-gate verdict for one PR, in the vocabulary the route adjudicates."""
    status = str(raw.get("status") or "").lower()
    if status != "active":
        return _STATE_UNKNOWN
    if raw.get("isDraft"):
        return _STATE_DRAFT
    merge_status = str(raw.get("mergeStatus") or "").lower()
    if merge_status in _MERGE_PENDING:
        return _STATE_CHECKING
    if merge_status not in _MERGE_SUCCEEDED:
        return _STATE_DIRTY
    number = raw.get("pullRequestId")
    if not isinstance(number, int):
        return _STATE_UNKNOWN
    try:
        evaluations = _policy_evaluations(org, project, number, host=host, timeout=timeout)
    except ProviderCliError:
        # A gate that cannot read the policies must not claim they are satisfied.
        return _STATE_UNKNOWN
    for evaluation in evaluations:
        if not _obj(evaluation.get("configuration")).get("isBlocking", True):
            continue
        if str(evaluation.get("status") or "").lower() != "approved":
            return _STATE_BLOCKED
    return "mergeable"


def _policy_evaluations(
    org: str, project: str, number: int, *, host: str, timeout: float
) -> list[dict]:
    """The policy evaluations for one pull request.

    Azure addresses these by ARTIFACT ID rather than by PR number, and the artifact
    id embeds the project's GUID -- which is why the project id is resolved (and
    cached) at all.
    """
    project_guid = _project_id(org, project, host=host, timeout=timeout)
    artifact = f"vstfs:///CodeReview/CodeReviewId/{project_guid}/{int(number)}"
    return _values(
        _az_invoke(
            org=org,
            area="policy",
            resource="evaluations",
            host=host,
            timeout=timeout,
            route={"project": project},
            query={"artifactId": artifact},
            api_version=_API_POLICY,
        )
    )


# ── policy evaluations and builds as checks ─────────────────────────────────
#
# GitHub reports per-PR status as check runs plus commit statuses. Azure has two
# separate things that both mean "a check on this PR": POLICY EVALUATIONS (the
# branch policies -- required reviewers, comment resolution, required builds) and
# the BUILDS themselves. Both are bucketed into the same vocabulary
# ``gitlab_client._norm_job`` uses so the shared summary logic and UI colours
# apply unchanged, including its behaviour that a cancelled unit is
# informational rather than failing.
_EVALUATION_BUCKETS = _normalization._EVALUATION_BUCKETS
_BUILD_RESULT_BUCKETS = _normalization._BUILD_RESULT_BUCKETS
_BUILD_RUNNING_STATUSES = _normalization._BUILD_RUNNING_STATUSES
_BUILD_FINISHED_STATUSES = _normalization._BUILD_FINISHED_STATUSES
_evaluation_bucket = _normalization._evaluation_bucket
_norm_evaluation = _normalization._norm_evaluation
_norm_build = _normalization._norm_build


# A repository's GUID, cached per organization/project/name for the same reason
# the project's is: it is immutable for the life of the repository.
_repo_id_cache: dict[tuple[str, str, str], str] = {}


def _repo_id(org: str, project: str, repo: str, *, host: str, timeout: float) -> str:
    """The repository's GUID, which the TfsGit build filter is addressed by."""
    cached = _repo_id_cache.get((org, project, repo))
    if cached:
        return cached
    data = _obj(
        _az_invoke(
            org=org,
            area="git",
            resource="repositories",
            host=host,
            timeout=timeout,
            route={"project": project, "repositoryId": repo},
            api_version=_API_GIT,
        )
    )
    value = str(data.get("id") or "").strip()
    if not _GUID_RE.match(value):
        raise ProviderCliError(f"could not resolve the repository id for {org}/{project}/{repo}")
    _repo_id_cache[(org, project, repo)] = value
    return value


def _builds_for_sha(
    org: str, project: str, repo: str, sha: str, *, host: str, timeout: float
) -> list[dict]:
    """Builds whose source commit is ``sha``.

    Azure's build list has no commit filter, so the newest
    :data:`_BUILD_SCAN_TOP` builds for the repository are read and matched on
    ``sourceVersion`` in Python. Bounded deliberately: an unbounded walk of a busy
    project's build history to find one commit's runs would be slower than the
    detail pane it serves.
    """
    rows = _values(
        _az_invoke(
            org=org,
            area="build",
            resource="builds",
            host=host,
            timeout=timeout,
            route={"project": project},
            query={
                "repositoryId": _repo_id(org, project, repo, host=host, timeout=timeout),
                "repositoryType": "TfsGit",
                "$top": _BUILD_SCAN_TOP,
                "queryOrder": "queueTimeDescending",
            },
            api_version=_API_BUILD,
        )
    )
    return [row for row in rows if str(row.get("sourceVersion") or "").lower() == sha.lower()]


def _pr_number_for_sha(
    org: str, project: str, repo: str, sha: str, *, host: str, timeout: float
) -> int | None:
    """The pull request whose head commit is ``sha``, or ``None``.

    Needed because the shared ``list_pr_checks`` signature keys checks by SHA
    while Azure keys policy evaluations by PULL REQUEST. Active PRs are searched
    first (the overwhelmingly common case for a check read), then all, both
    bounded.
    """
    for status in ("active", "all"):
        rows = _az_invoke_paged(
            org=org,
            area="git",
            resource="pullRequests",
            host=host,
            timeout=timeout,
            route={"project": project, "repositoryId": repo},
            query={"searchCriteria.status": status},
            api_version=_API_GIT,
            limit=_PR_LOOKUP_TOP,
        )
        for row in rows:
            head = str(_obj(row.get("lastMergeSourceCommit")).get("commitId") or "")
            if head.lower() == sha.lower():
                number = row.get("pullRequestId")
                return number if isinstance(number, int) else None
    return None


def list_pr_checks(
    owner: str, repo: str, sha: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """Check-run-shaped rows for commit ``sha``: its builds plus its PR's policies.

    Keyed on the head SHA to match the other two clients, and because that is the
    correct semantics: a run against an older commit describes code that does not
    exist. Azure needs one extra step for the policy half -- evaluations are
    addressed by pull request, so the PR whose head is ``sha`` is resolved first;
    if no PR matches (a commit on a branch with no open PR), the builds are still
    returned rather than nothing.

    ``sha`` is charset-validated before reaching a query string, since it is the
    one value here that originates from a previous API response rather than from a
    connected-repo record.
    """
    if not _SHA_RE.match(sha or ""):
        raise ProviderCliError(f"invalid commit sha {sha!r}")
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    rows = [
        _norm_build(build)
        for build in _builds_for_sha(org, project, name, sha, host=host, timeout=timeout)
    ]
    try:
        number = _pr_number_for_sha(org, project, name, sha, host=host, timeout=timeout)
    except ProviderCliError:
        number = None
    if number is not None:
        try:
            rows.extend(
                _norm_evaluation(evaluation)
                for evaluation in _policy_evaluations(
                    org, project, number, host=host, timeout=timeout
                )
            )
        except ProviderCliError:
            # Policies out of reach must not blank the builds that were read.
            pass
    return rows


def summarize_checks(checks: list[dict]) -> dict:
    """Return the provider-neutral check summary."""
    return _normalization.summarize_checks(checks)


def _enrich_rows(owner: str, repo: str, pulls: list[dict], *, host: str) -> list[dict]:
    """Attach card enrichment (check state) to PR rows, in place.

    Azure's PR list payload carries no check state at all, so unlike GitLab this
    cannot be a no-op: each row's checks come from its own policy evaluations. That
    is one call per PR, so it is bounded at :data:`_ENRICH_MAX_PULLS`. Rows past
    the bound keep ``checks_counts: None``, which makes
    :func:`enrichment_complete` answer ``False`` and keeps the whole set OUT of
    the on-disk cache -- the same invariant the other clients rely on -- rather
    than caching a zeroed check state as authoritative.

    A per-PR failure leaves that row unenriched for the same reason: a row whose
    checks could not be read must not be persisted as having none.
    """
    org, project = _split_owner(owner)
    _check_repo(repo)
    for row in pulls[:_ENRICH_MAX_PULLS]:
        number = row.get("number")
        if not isinstance(number, int):
            continue
        try:
            evaluations = _policy_evaluations(
                org, project, number, host=host, timeout=AZ_TIMEOUT_SEC
            )
        except ProviderCliError:
            continue
        row.update(summarize_checks([_norm_evaluation(item) for item in evaluations]))
    return pulls


def enrich_pulls(
    owner: str, repo: str, pulls: list[dict], state: str, *, host: str = ""
) -> list[dict]:
    """Attach card enrichment to each PR row.

    ``state`` is accepted for signature parity and unused: the enrichment is the
    same read for an open PR and a closed one, and Azure has no cheaper path for
    either.
    """
    del state
    return _enrich_rows(owner, repo, pulls, host=host)


def enrich_pulls_by_number(
    owner: str, repo: str, pulls: list[dict], *, host: str = ""
) -> list[dict]:
    """Signature-parity counterpart to :func:`enrich_pulls`, for search results."""
    return _enrich_rows(owner, repo, pulls, host=host)


def enrichment_complete(pulls: list[dict]) -> bool:
    """Whether every row is safe to persist in the pull-request cache."""
    return _normalization.enrichment_complete(pulls)


# ── pull-request search ─────────────────────────────────────────────────────


def build_pr_search_query(
    owner: str,
    repo: str,
    *,
    state: str = "open",
    author: str | None = None,
    assignee: str | None = None,
    review_requested: str | None = None,
) -> str:
    """Assemble the ``searchCriteria.*`` fragment for a per-person PR search.

    GitHub takes a single search-qualifier string; Azure takes discrete
    ``searchCriteria`` parameters. The CALLER-FACING signature is identical on
    purpose -- the route passes the same keyword arguments to whichever client it
    holds, so a provider-specific spelling here would be a ``TypeError`` at
    request time rather than a compile error.

    The person filters are emitted as PLACEHOLDERS (``{creatorId}`` /
    ``{reviewerId}``), not as values, because Azure's criteria take identity GUIDs
    rather than names and resolving a login to a GUID is a network call this pure
    function must not make. :func:`search_pulls` substitutes them. ``assignee``
    maps onto ``reviewerId`` as well: an Azure pull request has no assignee, and a
    reviewer is the nearest thing the criteria can express -- which is why an
    assignee filter and a review-requested filter cannot be combined here.

    Raises :class:`PrSearchError` on an unknown state, an invalid login, or when no
    person filter was given -- an unfiltered search would just duplicate the list
    endpoint, the same reason the other two clients refuse it.
    """
    if state not in ("open", "closed", "merged", "all"):
        raise PrSearchError(f"unsupported state for pull-request search: {state!r}")
    # Azure has no distinct "merged" status: a merged PR is ``completed``, and an
    # abandoned one is ``abandoned``. So "merged" asks for completed, and "closed"
    # asks for abandoned -- closed WITHOUT merge, which is what the route's closed
    # tab means and what the GitHub path returns.
    az_status = {"open": "active", "closed": "abandoned", "merged": "completed", "all": "all"}[
        state
    ]
    params = [f"searchCriteria.status={az_status}"]
    people: list[tuple[str, str | None]] = [
        ("searchCriteria.creatorId", author),
        ("searchCriteria.reviewerId", review_requested or assignee),
    ]
    added = 0
    for param, login in people:
        if not login:
            continue
        if not _LOGIN_RE.match(login):
            raise PrSearchError(f"invalid Azure DevOps login: {login!r}")
        placeholder = param.rsplit(".", 1)[-1]
        params.append(f"{param}={{{placeholder}}}")
        added += 1
    if added == 0:
        raise PrSearchError("pull-request search needs at least one person filter")
    del owner, repo  # the repository is addressed by route parameter, not by a qualifier
    return "&".join(params)


def _resolve_login_id(org: str, project: str, login: str, *, host: str, timeout: float) -> str:
    """Resolve a login to the identity GUID Azure's search criteria require.

    Azure's ``searchCriteria.creatorId`` / ``reviewerId`` take a GUID, not a name,
    and a name they cannot parse is IGNORED rather than rejected -- which would
    turn a person filter into an unfiltered list and present every open PR as that
    person's. So the login is resolved against the project's team members (the same
    roster :func:`list_repo_collaborators` reads) and an unresolvable one raises
    :class:`PrSearchError` instead.
    """
    target = login.strip().lower()
    try:
        for identity in _iter_project_identities(org, project, host=host, timeout=timeout):
            if not _identity_matches(identity, target):
                continue
            # A match whose id is not a GUID cannot reach an argv, so the walk
            # continues rather than failing: another team may carry the same
            # person with a usable identity reference.
            resolved = _identity_id(identity)
            if resolved:
                return resolved
    except ProviderCliError as exc:
        raise PrSearchError(
            f"cannot resolve {login!r} to an Azure DevOps identity: the project's teams "
            "are unreadable"
        ) from exc
    raise PrSearchError(f"no Azure DevOps identity matches {login!r} in {org}/{project}")


def search_pulls(
    owner: str,
    repo: str,
    *,
    host: str = "",
    state: str = "open",
    author: str | None = None,
    assignee: str | None = None,
    review_requested: str | None = None,
    timeout: float = AZ_PAGINATE_TIMEOUT_SEC,
    limit: int = PR_SEARCH_MAX,
) -> list[dict]:
    """Search a repository's pull requests by person, server-side.

    Returns rows in the SAME shape as :func:`list_open_pulls`, so the frontend can
    swap data sources without a second row type. ``limit`` is honoured, and the
    ceiling is ``PR_SEARCH_MAX + 1`` rather than ``PR_SEARCH_MAX``: the route asks
    for one MORE row than it will show and reports "truncated" when it gets it, so
    clamping to the display cap would discard that sentinel and present every
    over-cap result set as complete.
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    query_fragment = build_pr_search_query(
        owner,
        repo,
        state=state,
        author=author,
        assignee=assignee,
        review_requested=review_requested,
    )
    query: dict[str, object] = {}
    for pair in query_fragment.split("&"):
        key, _, value = pair.partition("=")
        if value == "{creatorId}":
            query[key] = _resolve_login_id(
                org, project, str(author), host=host, timeout=AZ_TIMEOUT_SEC
            )
        elif value == "{reviewerId}":
            query[key] = _resolve_login_id(
                org, project, str(review_requested or assignee), host=host, timeout=AZ_TIMEOUT_SEC
            )
        else:
            query[key] = value
    capped = max(1, min(int(limit), PR_SEARCH_MAX + 1))
    rows = _az_invoke_paged(
        org=org,
        area="git",
        resource="pullRequests",
        host=host,
        timeout=timeout,
        route={"project": project, "repositoryId": name},
        query=query,
        api_version=_API_GIT,
        limit=capped,
    )
    out = [_norm_pull(row) for row in rows][:capped]
    if state == "closed":
        # Belt to the status filter above: "closed" means closed WITHOUT being
        # merged, matching the GitHub path.
        out = [row for row in out if not row.get("merged_at")]
    return out
