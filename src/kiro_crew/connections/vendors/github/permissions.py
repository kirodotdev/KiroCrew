"""GitHub-specific permission probe: resolve WHO may see a repo's rows.

WHAT THIS OWNS (W02)
====================
The query-time ACL gate needs a per-provider probe that answers "does this
subject still have access to THIS object?" (see
``knowledge.acl.ProviderResourceRef``). The GITHUB-SPECIFIC half of that -- how
GitHub expresses visibility and who a private repo is shared with -- is W02's,
and this module is it. The shared grant WIRING (writing the resolved subjects
onto an item's grant in the same transaction) is the ACL owner's; the trusted
IDENTITY credential a call runs under is W01's. This module only reads GitHub.

It drives the REAL W01 executor/production transport (through
``connections.vendors.github.dispatch``) -- no naked ``urllib``/``requests``/
``httpx``, no re-implemented custody/auth/paging. Over controlled TLS in tests it
reads a fixture repo; PR-4 owns the live-account ACCEPTANCE of this capability.
Building it here (rather than leaving the capability unwritten) is deliberate:
the acceptance gate is not a coding prohibition.

HOW GITHUB EXPRESSES VISIBILITY
===============================
* A **public** repo's issues/PRs/commits/check-runs are readable by anyone, so
  its subject set is ``{acl.PUBLIC_SUBJECT}`` (an explicit, proven public grant,
  not the deny-all default). The signal is the repository object's ``private``
  boolean, read via ``gh_get_repository``.
* A **private** repo is readable only by its collaborators, so its subject set
  is the collaborator logins, read via ``gh_list_repository_collaborators``
  (paged). A collaborator login is the GitHub subject id.

WHAT THIS DELIBERATELY DOES NOT DO
==================================
It does not WIDEN a row's grant on its own -- the connector keeps ``subjects=()``
fail-closed until the shared wiring + a W01-verified tenant/subject mapping bind
this probe's output onto a grant (PR-4 territory). This module returns the
resolved set; binding it to a stored row is not W02's call. It also does not
invent a public default: a repo it cannot read the visibility of yields the
empty (deny-all) set, never public.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional

from kiro_crew.connections.control_plane.result import (
    CollectionPayload,
    ObjectPayload,
)
from kiro_crew.connections.vendors.github.dispatch import (
    dispatch_operation,
    open_page_walk,
    walk_pages,
)

if TYPE_CHECKING:
    from kiro_crew.connections.control_plane.operation import CredentialMode


def _object_of(outcome: Any) -> Optional[dict]:
    """The single object an ``ObjectPayload`` outcome carried, or ``None``."""

    if not outcome.ok:
        return None
    payload = outcome.payload
    if isinstance(payload, ObjectPayload) and isinstance(payload.object, dict):
        return dict(payload.object)
    return None


def probe_repo_is_private(
    *,
    owner: str,
    repo: str,
    handle: Any,
    transport: Any,
    offered_mode: "CredentialMode",
    permitted: Any,
    layers: Any,
    governance_scope: str,
    governance_item: str,
    clock: Any = None,
) -> Optional[bool]:
    """Read the repository's ``private`` flag through the real transport.

    Returns ``True`` (private), ``False`` (public), or ``None`` when the visibility
    could not be read (a denied gate, a transport failure, or a body without the
    field) -- in which case the caller must fail closed rather than assume public.
    Drives ``gh_get_repository`` (a single-object read) through W01's executor.
    """

    kwargs: dict = dict(
        offered_mode=offered_mode, permitted=permitted, layers=layers,
        governance_scope=governance_scope, governance_item=governance_item,
        request_args={"owner": owner, "repo": repo},
    )
    if clock is not None:
        kwargs["clock"] = clock
    outcome = dispatch_operation(
        operation_id="gh_get_repository", handle=handle, transport=transport, **kwargs)
    obj = _object_of(outcome)
    if obj is None or "private" not in obj:
        return None
    return bool(obj["private"])


def probe_collaborator_subjects(
    *,
    owner: str,
    repo: str,
    handle: Any,
    transport: Any,
    offered_mode: "CredentialMode",
    permitted: Any,
    layers: Any,
    governance_scope: str,
    governance_item: str,
    clock: Any = None,
) -> List[str]:
    """List a private repo's collaborator logins (its subject set) via paging.

    Drives ``gh_list_repository_collaborators`` through W01's ``PageWalk`` and
    collects each collaborator's ``login`` off the ``CollectionPayload``. A page a
    gate denied or the transport failed on stops the walk; the subjects gathered
    so far are returned, and the caller treats an incomplete probe as fail-closed
    (it must not grant on a partial collaborator set). Returns the logins in
    encounter order, de-duplicated.
    """

    walk = open_page_walk(
        operation_id="gh_list_repository_collaborators",
        handle=handle, transport=transport, offered_mode=offered_mode,
        permitted=permitted, layers=layers, governance_scope=governance_scope,
        governance_item=governance_item,
        base_args={"owner": owner, "repo": repo}, clock=clock)
    logins: List[str] = []
    for outcome in walk_pages(walk):
        if not outcome.ok:
            break  # incomplete probe: caller fails closed on a partial set
        payload = outcome.payload
        if not isinstance(payload, CollectionPayload):
            continue
        for item in payload.items:
            login = item.get("login")
            if isinstance(login, str) and login and login not in logins:
                logins.append(login)
    return logins


def resolve_repo_subjects(
    *,
    owner: str,
    repo: str,
    handle: Any,
    transport_for: Any,
    offered_mode: "CredentialMode",
    permitted: Any,
    layers: Any,
    governance_scope: str,
    governance_item: str,
    public_subject: str,
    clock: Any = None,
) -> tuple:
    """Resolve the subject set for a repo's rows, over the real transport.

    ``transport_for`` is a callable ``(operation_id) -> transport`` so each read
    goes through the transport composed for that operation (custody is per
    operation/binding, W01's). Returns:

    * ``(public_subject,)`` when the repo is PUBLIC (proven via its ``private``
      flag being False) -- an explicit, proven public grant;
    * the collaborator logins (a tuple) when the repo is PRIVATE;
    * ``()`` (deny-all) when visibility could not be read -- fail closed, never
      an assumed public.

    This is the GitHub half of the query-time probe. It does NOT write the set
    onto a grant (the ACL owner's wiring) and the connector keeps ``subjects=()``
    until that wiring plus a W01-verified subject/tenant mapping bind it -- so
    this is a tested capability whose live acceptance is PR-4's, not a widening
    of the fail-closed default here.
    """

    common: dict = dict(
        offered_mode=offered_mode, permitted=permitted, layers=layers,
        governance_scope=governance_scope, governance_item=governance_item,
        clock=clock,
    )
    is_private = probe_repo_is_private(
        owner=owner, repo=repo, handle=handle,
        transport=transport_for("gh_get_repository"), **common)
    if is_private is None:
        return ()  # unknown visibility -> fail closed, never public
    if not is_private:
        return (public_subject,)  # proven public
    subjects = probe_collaborator_subjects(
        owner=owner, repo=repo, handle=handle,
        transport=transport_for("gh_list_repository_collaborators"), **common)
    return tuple(subjects)


__all__ = [
    "probe_collaborator_subjects",
    "probe_repo_is_private",
    "resolve_repo_subjects",
]
