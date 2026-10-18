"""The Azure Boards plumbing every work-item read and write stands on.

The project's GUID, each work item type's closing states and the signed-in identity,
each cached for the life of the process; the open and closing state a write moves an
item to; the WIQL literal quoting and the open-items query; and the batched hydrate
that turns WIQL ids into rows. Callers reach every name here through
:mod:`kiro_crew.apps.builtins.issue_radar.backend.azure_client`.
"""

from __future__ import annotations

from .azure_client_cli import (
    _API_CORE,
    _API_IDENTITY,
    _API_WIT,
    _API_WIT_STATES,
    _BATCH_MAX_IDS,
    _GUID_RE,
    _az_invoke,
    _identity_id,
    _identity_login,
    _obj,
    _values,
)
from .errors import ProviderCliError, ProviderSetupError

# ── project metadata: ids, identities, and the template's own state names ─────
#
# Three facts are needed repeatedly and are IMMUTABLE for the life of a process
# (a project's GUID never changes; a work item type's state definitions change
# only when an administrator edits the process), so they are cached per
# organization/project. The cache is keyed on the validated names, never on
# caller input verbatim, and holds no credential.
_project_id_cache: dict[tuple[str, str], str] = {}
_closed_states_cache: dict[tuple[str, str, str], frozenset[str]] = {}
_identity_cache: dict[str, dict] = {}

# Fallback closing states, used only when the project's own state definitions
# cannot be read. Deliberately the union of the closing states of the templates
# Microsoft ships (Agile, Scrum, CMMI, Basic), because a wrong answer here means
# a closed work item shows as open in the triage list.
_FALLBACK_CLOSED_STATES = frozenset({"Closed", "Done", "Completed", "Removed", "Resolved"})
# The state CATEGORIES that mean "not open". Categories are the template-agnostic
# layer Azure guarantees: every custom state maps into one of Proposed /
# InProgress / Resolved / Completed / Removed, so keying on them is what lets this
# module work on a custom process without knowing its state names.
_CLOSED_STATE_CATEGORIES = frozenset({"Completed", "Removed"})


def _project_id(org: str, project: str, *, host: str, timeout: float) -> str:
    """The project's GUID, which the policy-evaluation artifact id is built from."""
    cached = _project_id_cache.get((org, project))
    if cached:
        return cached
    data = _obj(
        _az_invoke(
            org=org,
            area="core",
            resource="projects",
            host=host,
            timeout=timeout,
            route={"projectId": project},
            api_version=_API_CORE,
        )
    )
    value = str(data.get("id") or "").strip()
    if not _GUID_RE.match(value):
        raise ProviderCliError(f"could not resolve the project id for {org}/{project}")
    _project_id_cache[(org, project)] = value
    return value


def _closed_state_names(
    org: str, project: str, work_item_type: str, *, host: str, timeout: float
) -> frozenset[str]:
    """The state names that mean "not open" for one work item type.

    Read from the type's OWN state definitions and selected by state CATEGORY, so
    a custom process whose closing state is called "Shipped" is handled without
    this module knowing the name. Falls back to
    :data:`_FALLBACK_CLOSED_STATES` when the definitions cannot be read (an older
    server, or a caller without process read access) rather than treating every
    state as open.
    """
    key = (org, project, work_item_type)
    cached = _closed_states_cache.get(key)
    if cached is not None:
        return cached
    try:
        rows = _values(
            _az_invoke(
                org=org,
                area="wit",
                resource="states",
                host=host,
                timeout=timeout,
                route={"project": project, "type": work_item_type},
                api_version=_API_WIT_STATES,
            )
        )
    except ProviderCliError:
        return _FALLBACK_CLOSED_STATES
    names = {
        str(row.get("name"))
        for row in rows
        if row.get("name") and str(row.get("category") or "") in _CLOSED_STATE_CATEGORIES
    }
    resolved = frozenset(names) if names else _FALLBACK_CLOSED_STATES
    _closed_states_cache[key] = resolved
    return resolved


def _project_closed_states(org: str, project: str, *, host: str, timeout: float) -> frozenset[str]:
    """Closing state names across every work item type in the project.

    A WIQL query spans types, so the open filter needs the UNION of the closing
    states of all of them: a state that closes a Bug but not a Task must still be
    excluded when the query returns both.
    """
    try:
        types = _values(
            _az_invoke(
                org=org,
                area="wit",
                resource="workitemtypes",
                host=host,
                timeout=timeout,
                route={"project": project},
                api_version=_API_WIT,
            )
        )
    except ProviderCliError:
        return _FALLBACK_CLOSED_STATES
    names: set[str] = set()
    for row in types:
        for state in _values(row.get("states")):
            if state.get("name") and str(state.get("category") or "") in _CLOSED_STATE_CATEGORIES:
                names.add(str(state["name"]))
    return frozenset(names) if names else _FALLBACK_CLOSED_STATES


def _open_state_name(
    org: str, project: str, work_item_type: str, *, host: str, timeout: float
) -> str:
    """The state to reopen a work item INTO, for its own type.

    Chosen by category rather than by name: the first ``Proposed`` state is the
    template's own entry state ("New" on Agile, "To Do" on Basic), so reopening
    lands where a freshly created item would rather than in a state the template
    may not even define.
    """
    try:
        rows = _values(
            _az_invoke(
                org=org,
                area="wit",
                resource="states",
                host=host,
                timeout=timeout,
                route={"project": project, "type": work_item_type},
                api_version=_API_WIT_STATES,
            )
        )
    except ProviderCliError as exc:
        raise ProviderCliError(
            f"cannot reopen a {work_item_type}: the project's state definitions are unreadable, "
            "so the state to reopen into is unknown"
        ) from exc
    for category in ("Proposed", "InProgress"):
        for row in rows:
            if str(row.get("category") or "") == category and row.get("name"):
                return str(row["name"])
    raise ProviderCliError(f"the {work_item_type} type defines no open state to reopen into")


def _closing_state_name(
    org: str, project: str, work_item_type: str, *, host: str, timeout: float
) -> str:
    """The state to close a work item INTO, for its own type (category ``Completed``).

    ``Removed`` is deliberately not used as a fallback: it is Azure's "this should
    never have existed" state, which is a different act from closing and on some
    templates hides the item from the board entirely.
    """
    try:
        rows = _values(
            _az_invoke(
                org=org,
                area="wit",
                resource="states",
                host=host,
                timeout=timeout,
                route={"project": project, "type": work_item_type},
                api_version=_API_WIT_STATES,
            )
        )
    except ProviderCliError as exc:
        raise ProviderCliError(
            f"cannot close a {work_item_type}: the project's state definitions are unreadable, "
            "so the closing state is unknown"
        ) from exc
    for row in rows:
        if str(row.get("category") or "") == "Completed" and row.get("name"):
            return str(row["name"])
    raise ProviderCliError(f"the {work_item_type} type defines no completed state")


def _current_identity(org: str, *, host: str, timeout: float) -> dict:
    """``{"id": guid, "login": str}`` for the authenticated ``az`` session.

    Azure serves identity from a different area than everything else here, and
    which one answers depends on how the session was established (an ``az login``
    ARM session versus ``az devops login`` with a PAT), so two candidates are
    tried in order. Both are reads; the first that yields a GUID wins. A failure
    to resolve is raised rather than swallowed by the callers that NEED the GUID
    (a review vote and arming auto-complete are both addressed by reviewer id),
    while :func:`get_current_login` swallows it.
    """
    cached = _identity_cache.get(org)
    if cached:
        return cached
    candidates = (
        {"area": "connectionData", "resource": "connectionData", "route": None},
        {"area": "profile", "resource": "profiles", "route": {"id": "me"}},
    )
    last: ProviderCliError | None = None
    for candidate in candidates:
        try:
            data = _obj(
                _az_invoke(
                    org=org,
                    area=str(candidate["area"]),
                    resource=str(candidate["resource"]),
                    host=host,
                    timeout=timeout,
                    route=candidate["route"] if isinstance(candidate["route"], dict) else None,
                    api_version=_API_IDENTITY,
                )
            )
        except ProviderSetupError:
            raise  # not authenticated / not installed is final, not a candidate miss
        except ProviderCliError as exc:
            last = exc
            continue
        user = _obj(data.get("authenticatedUser")) or data
        identity = _identity_id(user)
        login = _identity_login(user) or str(user.get("emailAddress") or "").strip() or None
        if identity:
            resolved = {"id": identity, "login": login}
            _identity_cache[org] = resolved
            return resolved
    if last is not None:
        raise last
    raise ProviderCliError(f"could not resolve the authenticated identity on {org}")


# ── WIQL ─────────────────────────────────────────────────────────────────────
#
# There is no filtered work-item list endpoint, so every listing starts with a
# WIQL query. WIQL is a query LANGUAGE, which makes every interpolated value an
# injection surface: a project name or a state name carrying a quote could
# otherwise close the literal and append its own clause -- and the clause that
# matters is the open-state filter, so the failure mode is a triage list showing
# closed items, not a syntax error someone notices.


def _wiql_literal(value: str) -> str:
    """One value as a safe WIQL single-quoted literal.

    A literal quote is escaped by DOUBLING it, which is WIQL's own mechanism.
    Anything that cannot be escaped that way -- a control character, a newline --
    is refused rather than stripped: silently dropping a character changes which
    items the query matches, and a name we cannot represent exactly is a name we
    must not guess at.
    """
    text = str(value or "")
    if not text:
        raise ProviderCliError("cannot build a WIQL query from an empty value")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ProviderCliError(f"value is not safe to interpolate into WIQL: {value!r}")
    return "'" + text.replace("'", "''") + "'"


def _open_work_items_wiql(project: str, closed_states: frozenset[str], *, order_by: str) -> str:
    """WIQL selecting the ids of the project's OPEN work items.

    Filters on STATE rather than on work item TYPE. "Issue" is a type that exists
    only in some process templates (Basic and Agile have it; Scrum does not), so
    restricting to it would silently return nothing on a Scrum project and would
    hide Bugs and Tasks everywhere else. The state names come from the project's
    own definitions -- see :func:`_project_closed_states` -- so nothing here
    assumes Agile.

    ``order_by`` is a fixed field name chosen by the caller from a closed set,
    never caller input.
    """
    states = ", ".join(_wiql_literal(name) for name in sorted(closed_states))
    return (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = {_wiql_literal(project)} "
        f"AND [System.State] NOT IN ({states}) "
        f"ORDER BY [{order_by}] DESC"
    )


def _wiql_ids(
    org: str,
    project: str,
    query: str,
    *,
    host: str,
    timeout: float,
    top: int,
) -> list[int]:
    """Run a WIQL query and return the work item ids it selected, in query order."""
    data = _obj(
        _az_invoke(
            org=org,
            area="wit",
            resource="wiql",
            host=host,
            timeout=timeout,
            route={"project": project},
            query={"$top": max(1, int(top))},
            method="POST",
            body={"query": query},
            api_version=_API_WIT,
        )
    )
    ids: list[int] = []
    for row in _values(data.get("workItems")):
        value = row.get("id")
        if isinstance(value, int):
            ids.append(value)
    return ids


# The fields every list row and detail pane reads. Requested explicitly rather
# than pulling whole work items: a full hydrate carries every custom field the
# process defines, which on a mature project is an order of magnitude more bytes
# for data nothing here renders.
_WORK_ITEM_FIELDS = (
    "System.Id",
    "System.Title",
    "System.State",
    "System.WorkItemType",
    "System.Tags",
    "System.CreatedBy",
    "System.CreatedDate",
    "System.ChangedDate",
    "System.AssignedTo",
    "System.Description",
    "System.CommentCount",
    "System.IterationPath",
    "Microsoft.VSTS.Common.ClosedDate",
    "Microsoft.VSTS.Common.ClosedBy",
)


def _hydrate_work_items(
    org: str, project: str, ids: list[int], *, host: str, timeout: float
) -> list[dict]:
    """Fetch the fields of ``ids`` in id ORDER, chunked at the batch endpoint's cap.

    The batch endpoint takes at most :data:`_BATCH_MAX_IDS` ids per call and
    answers 400 above it, so the ids are chunked and the results stitched back
    into the order they were given -- which is the WIQL query's order, and
    therefore the order the list view expects.
    """
    if not ids:
        return []
    by_id: dict[int, dict] = {}
    for start in range(0, len(ids), _BATCH_MAX_IDS):
        chunk = ids[start : start + _BATCH_MAX_IDS]
        rows = _values(
            _az_invoke(
                org=org,
                area="wit",
                resource="workitemsbatch",
                host=host,
                timeout=timeout,
                route={"project": project},
                method="POST",
                body={"ids": chunk, "fields": list(_WORK_ITEM_FIELDS)},
                api_version=_API_WIT,
            )
        )
        for row in rows:
            value = row.get("id")
            if isinstance(value, int):
                by_id[value] = row
    return [by_id[i] for i in ids if i in by_id]
