"""Repository-level Azure DevOps reads.

The repo access check and the permission read, the open and closed work-item lists
with their change probe, the project's tags as labels, the member roster, the
signed-in login and its default organization, and the contributed repos. Callers
reach every name here through :mod:`kiro_crew.apps.builtins.issue_radar.backend.azure_client`.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse

from .azure_client_cli import (
    _API_CORE,
    _API_GIT,
    _API_WIT_TAGS,
    _GUID_RE,
    _MAX_PAGES,
    _PAGE_SIZE,
    _WIQL_TOP,
    AZ_PAGINATE_TIMEOUT_SEC,
    AZ_TIMEOUT_SEC,
    CONTRIB_WINDOW_DAYS,
    _az_invoke,
    _az_invoke_paged,
    _az_run,
    _bad_segment,
    _check_repo,
    _identity_login,
    _norm_issue,
    _obj,
    _resolve_host,
    _shape_labels,
    _split_owner,
    _stderr_tail,
    _url_path_segments,
    _values,
)
from .azure_client_work_items import (
    _current_identity,
    _hydrate_work_items,
    _open_work_items_wiql,
    _project_closed_states,
    _wiql_ids,
    _wiql_literal,
)
from .errors import ProviderCliError, ProviderPermissionError

# ── public surface: mirrors github_client function-for-function ──────────────


def verify_repo_access(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> dict:
    """Verify the repository exists and the current ``az`` session can read it.

    Returns the same small summary shape the other two clients do, so the connect
    route needs no branching: ``full_name``, ``private``, ``open_issues_count``,
    ``description``, ``permissions``.

    ``open_issues_count`` is the PROJECT's open work item count, not the
    repository's -- work items have no repository dimension (see ``azure_client``'s
    module docstring), so this is the only count that exists. ``description`` is the
    project's, for the same reason: an Azure repository carries none.
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    details = _obj(
        _az_invoke(
            org=org,
            area="git",
            resource="repositories",
            host=host,
            timeout=timeout,
            route={"project": project, "repositoryId": name},
            api_version=_API_GIT,
        )
    )
    if not details:
        raise ProviderCliError(f"could not read {owner}/{repo} on {host}")
    project_info = _obj(details.get("project"))
    try:
        open_issues = len(
            _wiql_ids(
                org,
                project,
                _open_work_items_wiql(
                    project,
                    _project_closed_states(org, project, host=host, timeout=timeout),
                    order_by="System.ChangedDate",
                ),
                host=host,
                timeout=timeout,
                top=_WIQL_TOP,
            )
        )
    except ProviderCliError:
        # A project whose boards are disabled has no work item store at all, and
        # that must not make an otherwise readable repository look unreachable.
        open_issues = 0
    return {
        "full_name": f"{org}/{project}/{details.get('name') or name}",
        "private": str(project_info.get("visibility") or "private").lower() != "public",
        "open_issues_count": open_issues,
        "description": project_info.get("description"),
        "permissions": _permissions(org, project, host=host, timeout=timeout),
    }


def _permissions(org: str, project: str, *, host: str, timeout: float) -> dict:
    """The caller's permission object, in GitHub's ``{admin, maintain, push, triage, pull}``.

    Azure exposes no per-user effective-permission read through the CLI's REST
    passthrough (the security namespace / ACL routes speak in permission bitmasks
    against a namespace GUID, which is a different and far heavier surface), so
    this is derived from PROJECT TEAM MEMBERSHIP: a member of any project team
    holds the Contributors rights, which are what ``triage`` and ``push`` gate on
    here.

    This field decides which controls the UI offers, and ``routes._repo_can_write``
    consults it before admitting a mutation, so it is a GATE and it reports NO write
    access on Azure DevOps.

    The reason is that the only signal reachable through the CLI passthrough is
    project TEAM MEMBERSHIP, and membership does not imply repository write.
    Azure's permissions are per-repository ACLs that a project can override: a team
    member can have Git "Contribute" denied while still being able to edit work
    items, or the reverse. Reporting ``push``/``triage`` from membership therefore
    over-grants -- it offers write controls to someone the repository will refuse,
    and it does so through a gate whose answer is cached. There is no
    effective-permission read available here to replace it with (Azure exposes only
    namespace ACL bitmasks, which this transport cannot address).

    So the honest answer is false, and the consequence is deliberate: Azure repos
    are read-only through this app's own controls. A user who holds the rights
    still acts in Azure DevOps directly, where the permission is evaluated against
    their real identity rather than inferred here.

    ``pull`` stays true: read access is already demonstrated by the project being
    reachable and connected at all.
    """
    return {"admin": False, "maintain": False, "push": False, "triage": False, "pull": True}


def get_repo_permissions(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> dict:
    """The authenticated ``az`` user's permission object for the project."""
    org, project = _split_owner(owner)
    _check_repo(repo)
    return _permissions(org, project, host=host, timeout=timeout)


def _list_work_items(
    owner: str, repo: str, *, host: str, timeout: float, order_by: str, top: int
) -> list[dict]:
    """The PROJECT's open work items, newest first by ``order_by``.

    Two calls by necessity: WIQL selects the ids (no filtered list endpoint
    exists), then the batch endpoint hydrates them.
    """
    org, project = _split_owner(owner)
    del repo  # work items are project-scoped; see list_open_issues
    closed_states = _project_closed_states(org, project, host=host, timeout=timeout)
    ids = _wiql_ids(
        org,
        project,
        _open_work_items_wiql(project, closed_states, order_by=order_by),
        host=host,
        timeout=timeout,
        top=top,
    )
    rows = _hydrate_work_items(org, project, ids, host=host, timeout=timeout)
    return [_norm_issue(row, org=org, project=project, closed_states=closed_states) for row in rows]


def list_open_issues(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Every OPEN work item in the PROJECT -- the triage view's working set.

    ``repo`` is accepted for signature parity and IGNORED: an Azure DevOps work
    item belongs to a project and has no repository dimension at all, so there is
    no "work items in this repository" question to ask. Two repositories connected
    from the same project legitimately return the same list, and the UI says so
    rather than implying a per-repository view.
    """
    return _list_work_items(
        owner, repo, host=host, timeout=timeout, order_by="System.ChangedDate", top=_WIQL_TOP
    )


def list_open_issues_first_page(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """The newest single page of OPEN work items -- the progressive first paint.

    The same leading rows :func:`list_open_issues` returns (same shape, same
    most-recently-changed order), so the full set appends behind it with no
    reordering. ``repo`` is ignored here too.
    """
    return _list_work_items(
        owner, repo, host=host, timeout=timeout, order_by="System.ChangedDate", top=_PAGE_SIZE
    )


def list_closed_issues(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """The most recently changed CLOSED work items (single page, like GitHub's).

    ``repo`` is ignored, as for the open list.
    """
    org, project = _split_owner(owner)
    del repo
    closed_states = _project_closed_states(org, project, host=host, timeout=timeout)
    states = ", ".join(_wiql_literal(name) for name in sorted(closed_states))
    query = (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = {_wiql_literal(project)} "
        f"AND [System.State] IN ({states}) "
        "ORDER BY [System.ChangedDate] DESC"
    )
    ids = _wiql_ids(org, project, query, host=host, timeout=timeout, top=_PAGE_SIZE)
    rows = _hydrate_work_items(org, project, ids, host=host, timeout=timeout)
    return [_norm_issue(row, org=org, project=project, closed_states=closed_states) for row in rows]


def list_recent_open_issues(
    owner: str, repo: str, limit: int = 30, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """The newest open work items by CREATION time -- the watcher's poll query.

    Ordered by ``System.CreatedDate`` rather than by last change: the watcher
    notifies on items that are NEW, and an old item that just got a comment must
    not look new.
    """
    capped = max(1, min(int(limit), _PAGE_SIZE))
    rows = _list_work_items(
        owner, repo, host=host, timeout=timeout, order_by="System.CreatedDate", top=capped
    )
    return rows[:capped]


def list_repo_labels(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """Every tag DEFINED in the project, in the app's label shape.

    Azure's label system for work items is ``System.Tags``, and the project's tag
    definitions are the closest thing to a label set. The colour is SYNTHETIC and
    the description is always empty: an Azure tag has neither, so both are
    reported as the neutral default rather than as data read from the server.
    """
    org, project = _split_owner(owner)
    del repo  # tags are project-scoped, like the work items they annotate
    rows = _values(
        _az_invoke(
            org=org,
            area="wit",
            resource="tags",
            host=host,
            timeout=timeout,
            route={"project": project},
            api_version=_API_WIT_TAGS,
        )
    )
    names = [str(row.get("name")) for row in rows if row.get("name")]
    return _shape_labels(names)


def list_repo_collaborators(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Authoritative member roster: the members of the project's teams.

    Azure has no repository-level collaborator list -- access is granted through
    project teams and groups -- so the project's teams are walked and their
    members unioned. Returns ``[{login, role_name}]`` in github_client's
    vocabulary.

    ``role_name`` is ``"write"`` for a member and ``"admin"`` for a team
    administrator, which are the only two distinctions the team-member payload
    supports. A 403 raises :class:`ProviderPermissionError` so the route degrades
    to the issue-derived set, same as the other clients.
    """
    org, project = _split_owner(owner)
    del repo
    teams = _values(
        _az_invoke(
            org=org,
            area="core",
            resource="teams",
            host=host,
            timeout=timeout,
            route={"projectId": project},
            api_version=_API_CORE,
        )
    )
    seen: dict[str, str] = {}
    for team in teams:
        team_id = str(team.get("id") or "")
        if not _GUID_RE.match(team_id):
            continue
        members = _values(
            _az_invoke(
                org=org,
                area="core",
                resource="members",
                host=host,
                timeout=timeout,
                route={"projectId": project, "teamId": team_id},
                api_version=_API_CORE,
            )
        )
        for member in members:
            login = _identity_login(member.get("identity")) or _identity_login(member)
            if not login:
                continue
            role = "admin" if member.get("isTeamAdmin") else "write"
            # An admin on ANY team outranks plain membership on another.
            if seen.get(login) != "admin":
                seen[login] = role
    return [{"login": login, "role_name": role} for login, role in seen.items()]


def _iter_project_identities(org: str, project: str, *, host: str, timeout: float):
    """Every project team member, yielded as its Azure identity reference.

    The same roster :func:`list_repo_collaborators` reads -- Azure grants access
    through project TEAMS rather than per-repository ACLs -- but yielded lazily so
    a lookup stops at its first match instead of paying for every team.

    A team whose member list is unreadable is SKIPPED rather than failing the
    walk: the identity being looked for may still sit on a team this caller can
    see, and aborting because one team is hidden would report a real member as
    unknown. An unreadable TEAM LIST is different and propagates, because then
    nothing was searched at all and a caller must not read that as "no match".
    """
    teams = _values(
        _az_invoke(
            org=org,
            area="core",
            resource="teams",
            host=host,
            timeout=timeout,
            route={"projectId": project},
            api_version=_API_CORE,
        )
    )
    for team in teams:
        team_id = str(team.get("id") or "")
        if not _GUID_RE.match(team_id):
            continue
        try:
            members = _values(
                _az_invoke(
                    org=org,
                    area="core",
                    resource="members",
                    host=host,
                    timeout=timeout,
                    route={"projectId": project, "teamId": team_id},
                    api_version=_API_CORE,
                )
            )
        except ProviderCliError:
            continue
        for member in members:
            yield _obj(member.get("identity")) or member


def _identity_matches(identity: dict, target: str) -> bool:
    """True when *target* (already case-folded) names *identity*.

    Both ``uniqueName`` and ``displayName`` are matched because both are what a
    person types: the picker shows the display name while the roster and the
    person filters key on the unique name.
    """
    name = (_identity_login(identity) or "").lower()
    display = str(identity.get("displayName") or "").lower()
    return target in (name, display)


def derive_members(issues: list[dict]) -> list[dict]:
    """FALLBACK roster. Always ``[]`` on Azure DevOps.

    GitHub derives membership from ``author_association``, which Azure does not
    report in any form (see :func:`_norm_issue`). Returning empty is the honest
    answer: the caller only reaches this when the member read was forbidden, and
    inventing a roster from work item authors would badge non-members as members.
    The route surfaces the empty roster with its ``source`` marker so the UI can
    say so.
    """
    del issues
    return []


def get_current_login(*, host: str = "", timeout: float = AZ_TIMEOUT_SEC) -> str | None:
    """The authenticated ``az`` user's login, or ``None`` if unavailable.

    Swallows :class:`ProviderCliError` and answers ``None``, matching
    gitlab_client's contract rather than github_client's (which raises).

    Azure needs an ORGANIZATION to answer any question, including "who am I",
    because every REST route is organization-scoped. The organization is taken
    from the CLI's own configured default -- see
    :func:`_default_organization` -- which is the closest analogue of the ambient
    session the other two providers resolve this from.
    """
    try:
        org = _default_organization(host=host, timeout=timeout)
        return _current_identity(org, host=host, timeout=timeout).get("login")
    except ProviderCliError:
        return None


def _default_organization(*, host: str, timeout: float) -> str:
    """The organization ``az devops`` is configured to default to.

    The two other providers answer session-scoped questions ("my projects", "who
    am I") without a namespace, because their credential IS the session. Azure's
    REST surface has no such route: every path is organization-scoped, and the
    endpoint that lists a user's organizations lives on a different service host
    the CLI's passthrough does not address. The CLI's own default organization is
    therefore the only session-derived answer available, and it is read from local
    config -- no HTTP, no credential.

    Raises :class:`ProviderCliError` when no default is set, naming the command
    that sets one, rather than guessing at an organization.
    """
    _resolve_host(host)
    proc = _az_run(["az", "devops", "configure", "--list"], host=host, timeout=timeout)
    if proc.returncode != 0:
        raise ProviderCliError(f"could not read the az devops configuration: {_stderr_tail(proc)}")
    for line in (proc.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "organization":
            parsed = urlparse(value.strip())
            segments = _url_path_segments(parsed.path or "")
            if segments and not _bad_segment(unquote(segments[-1])):
                return unquote(segments[-1])
    raise ProviderCliError(
        "no default Azure DevOps organization is configured -- run "
        "`az devops configure --defaults organization=https://dev.azure.com/{org}`"
    )


def list_contributed_repos(
    login: str,
    *,
    host: str = "",
    within_days: int = CONTRIB_WINDOW_DAYS,
    timeout: float = AZ_PAGINATE_TIMEOUT_SEC,
) -> tuple[list[dict], bool]:
    """Repositories in the session's organization, for the connect picker.

    Returns ``(rows, truncated)`` -- the same tuple the other two clients return,
    with gitlab's row shape (``owner``/``repo``/``pushed_at``/``private``/
    ``description``), where ``owner`` is ``"{organization}/{project}"``.

    ``within_days`` is accepted and CANNOT be applied: Azure's repository payload
    carries no activity timestamp of any kind, so ``pushed_at`` is ``None`` and
    there is nothing to compare a cutoff against. Filtering on something else (a
    project's creation date, say) would silently hide active repositories, so the
    window is documented as unapplied instead.
    """
    del login  # the organization and its projects come from the az session
    del within_days  # no activity timestamp exists to filter on -- see the docstring
    org = _default_organization(host=host, timeout=timeout)
    projects = _az_invoke_paged(
        org=org,
        area="core",
        resource="projects",
        host=host,
        timeout=timeout,
        api_version=_API_CORE,
    )
    rows: list[dict] = []
    truncated = False
    for project in projects:
        name = str(project.get("name") or "")
        if _bad_segment(name):
            continue
        private = str(project.get("visibility") or "private").lower() != "public"
        try:
            repos = _values(
                _az_invoke(
                    org=org,
                    area="git",
                    resource="repositories",
                    host=host,
                    timeout=timeout,
                    route={"project": name},
                    api_version=_API_GIT,
                )
            )
        except ProviderPermissionError:
            # A project whose code is out of reach is skipped rather than failing
            # the whole picker; the user simply cannot connect that one.
            truncated = True
            continue
        for repo in repos:
            repo_name = str(repo.get("name") or "")
            if _bad_segment(repo_name) or repo.get("isDisabled"):
                continue
            rows.append(
                {
                    "owner": f"{org}/{name}",
                    "repo": repo_name,
                    # The picker keys its rows on `full_name` and hands it to
                    # publicRepoUrl, which splits it into org / project / repo to
                    # rebuild an Azure URL with its `_git` segment. Omitting it
                    # crashes selection on `undefined.split`, so the three-part
                    # form is the contract, not a convenience.
                    "full_name": f"{org}/{name}/{repo_name}",
                    "pushed_at": None,
                    "private": private,
                    "description": project.get("description"),
                }
            )
    return rows, truncated or len(projects) >= _MAX_PAGES * _PAGE_SIZE


# ── cheap open-list probe (poll gating) ─────────────────────────────────────
#
# The list routes poll with ``poll=1`` and serve the cache when a cheap probe
# shows the open list has not moved. The probe value is only ever compared
# against ANOTHER probe recorded when the list was last fetched, never against the
# cached rows, so a systematic difference between what the probe counts and what
# the list returns cancels out instead of reporting "changed" forever.
#
# Azure serves the two kinds very differently, and only one honestly:
#
#   work items -- the WIQL query returns the ids themselves, so the count is
#     EXACT: closing any work item, anywhere in the list, changes it. The newest
#     ChangedDate is a second, independent signal that catches an edit that does
#     not change the count.
#   pull requests -- the PR list endpoint exposes no total count in its body, and
#     Azure has no PR modification timestamp to fall back on (see ``_norm_pull``),
#     so any signal here would be weaker than the caller believes and could report
#     "unchanged" after a non-top PR closed. Refused instead, exactly as
#     gitlab_client refuses it.
_PROBE_KINDS = ("issue", "pr")


def probe_open_list(
    owner: str, repo: str, kind: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> dict:
    """``{"total_count": int, "top_updated_at": str | None}`` for the project's
    OPEN work items.

    Raises :class:`ProviderCliError` on a failed call, and for ``kind="pr"`` -- see
    the note above on why a PR probe is refused rather than approximated. Callers
    already treat a probe failure as a soft signal, so this degrades to the
    pre-poll behaviour instead of serving anything stale.
    """
    if kind not in _PROBE_KINDS:
        raise ProviderCliError(f"unsupported probe kind: {kind!r}")
    if kind == "pr":
        raise ProviderCliError(
            "Azure DevOps exposes no cheap open pull-request count; "
            "falling back to the staleness ceiling"
        )
    org, project = _split_owner(owner)
    del repo  # work items are project-scoped
    closed_states = _project_closed_states(org, project, host=host, timeout=timeout)
    ids = _wiql_ids(
        org,
        project,
        _open_work_items_wiql(project, closed_states, order_by="System.ChangedDate"),
        host=host,
        timeout=timeout,
        top=_WIQL_TOP,
    )
    top_updated: str | None = None
    if ids:
        rows = _hydrate_work_items(org, project, ids[:1], host=host, timeout=timeout)
        if rows:
            changed = _obj(rows[0].get("fields")).get("System.ChangedDate")
            top_updated = str(changed) if changed else None
    return {"total_count": len(ids), "top_updated_at": top_updated}
