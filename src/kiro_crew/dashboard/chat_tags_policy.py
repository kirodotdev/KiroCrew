"""Which tag changes an agent may make, and which tags a board shows.

The tag vocabulary's limits and the agent tag policy: a tag's grant and policy, the
refusal an agent's change to the vocabulary or to a slot's tags meets, the principal a
slot tag write is attributed to, and the tags a board resolves for a slot. A new rule on
who may change a tag goes here.

Served through :mod:`kiro_crew.dashboard.chat_tags`, which callers import and tests
patch; its mirror (:mod:`kiro_crew._facade_mirror`) carries such a patch to this module,
and this module never imports it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.chat_tag_grants import GrantsSnapshot, has_grant_row, resolve_grant
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY, request_origin

logger = logging.getLogger("kiro_crew.dashboard.chat_tags")


_NAME_MAX = 60
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_DEFAULT_COLOR = "#6b7280"

# Valid values for a tag's optional ``agent`` policy field.
_AGENT_TAG_POLICIES: frozenset[str] = frozenset({"add-remove", "add-only", "none"})


def agent_tag_grant(tag: dict[str, Any]) -> tuple[str, bool]:
    """Resolve a tag's ``(agent-write policy, workflow-status bit)``.

    Both values come from the PROTECTED grants store
    (:mod:`kiro_crew.dashboard.chat_tag_grants`), never from the tag dict's own
    fields: ``tags.json`` is agent-writable, so a policy or status read from it
    could be forged by the very party it authorizes and survive restart (a
    review finding). The dict parameter is kept so call sites keep passing the
    resolved tag; only its ``id`` is consulted. Rows are minted exclusively by
    the authenticated dashboard tag CRUD (plus a one-time boot seed from the
    code-default workflow-state IDs), and an absent or unreadable row fails closed to
    ``("none", False)`` — human-only, not a workflow state.
    """
    raw_id = tag.get("id")
    tag_id = raw_id if isinstance(raw_id, str) else ""
    return resolve_grant(tag_id)


def agent_tag_policy(tag: dict[str, Any]) -> str:
    """Resolve a tag's agent-write policy: ``"add-remove"`` | ``"add-only"`` | ``"none"``.

    Thin wrapper over :func:`agent_tag_grant` for the call sites that only
    need the policy axis. Shared by the ``chat_tag`` applier and the per-turn
    context injection so the policy lives in one place.
    """
    return agent_tag_grant(tag)[0]


def agent_tag_change_refusal(
    added: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    result: list[dict[str, Any]],
    snapshot: GrantsSnapshot,
) -> tuple[str, str] | None:
    """Grants-policy verdict for a non-person caller's tag diff, or ``None``.

    The protected-store authority the ``chat_tag`` directive enforces, expressed
    over the DIFF so the wholesale list replace at
    ``PUT /api/chat/slots/{slot}/tags`` is held to it. Every policy decision is
    read from ONE atomic ``snapshot`` of the grants cache
    (:func:`~kiro_crew.dashboard.chat_tag_grants.capture_grants_snapshot`) —
    both health axes and the rows come from the same installed cache state, so a
    concurrent refresh cannot swap the cache between a health check and a row
    lookup (the stale-health window). The protected ROW is the source of truth
    for both policy and status; a tag dict's own agent-writable ``status`` field
    (in ``tags.json``) is consulted ONLY to recognise a rowless tag CLAIMING to
    be a status tag, which is never trusted as a grant — only ever a reason to
    REFUSE.

    Health, two cases:

    * If the store is WRITE-BLOCKED (unreadable or gone), nothing can be
      verified at all, so the whole change fails closed ``tag_grants_unavailable``.
    * If grants are merely REDUCED (a boot quarantine that has since reseeded
      and verified: the default states are row-backed again, but the operator's
      custom reservations were discarded), a tag that is ROWLESS might be a lost
      reservation rather than an ordinary label — the two are indistinguishable
      now. So a rowless tag fails closed ``tag_grants_unavailable`` while
      ``grants_reduced`` holds, but a ROW-BACKED tag (a reseeded default state)
      resolves normally and stays assignable. Recovery is owner adoption.

    What a non-person caller is otherwise refused is a RESERVATION the store
    actually records — a protected grant row. A healthy-store rowless tag is an
    ordinary label (apps self-organise their own sessions with these), not
    something the person reserved, so it is not gated; the row is what makes a
    tag the person's:

    * an ADDED row-backed tag needs ``add-only`` or ``add-remove`` — a row-backed
      ``none`` tag is a state the person reserved for themselves
      (``tag_policy_denied``);
    * a REMOVED row-backed tag needs ``add-remove`` — row-backed ``add-only`` and
      ``none`` both forbid an agent from stripping it (``tag_policy_denied``);
    * an ADDED or REMOVED tag whose agent-writable vocabulary bit says "status"
      but which has NO protected row (an upgraded install's custom status tag,
      or one caught between a revoke and its re-mint) has an UNKNOWN identity,
      not a plain label: it is refused ``status_identity_unprotected`` rather
      than stacking (on add) or silently dropping (on remove) a workflow state
      the store cannot vouch for.

    Workflow-state exclusivity: workflow states are mutually exclusive and
    ``set_state`` is the only verb that strips peers. This route strips nothing
    of its own, so a change THIS diff makes that leaves the result carrying more
    than one status tag is refused ``status_tag_requires_set_state`` — the agent
    must drive a workflow state through ``chat_tag`` ``set_state``. A status tag
    here is any tag that is a row-backed status OR carries the vocabulary status
    bit, so a rowless status cannot slip the count. The refusal fires ONLY when
    the offending status tag was ADDED by this diff: the person (never gated) may
    leave a session carrying two status tags, and a later agent write that
    touches neither must not be blamed for that pre-existing state. Swapping one
    status tag for another (its peer in ``removed``) is permitted; stacking a
    second is not.

    Returns the FIRST ``(code, tag_id)`` refusal (so one bad id in a multi-tag
    change leaves the whole write unapplied), or ``None`` when every change is
    permitted. Pure: it reads only ``snapshot`` and the resolved diff.
    """
    if snapshot.write_blocked() is not None:
        # Store unreadable or gone: nothing can be verified. Fail closed for the
        # whole change rather than treat a reserved tag as rowless. The error id
        # is empty — the refusal is about the store, not one tag.
        return ("tag_grants_unavailable", "")
    # Grants reduced by a boot quarantine (even one that has since reseeded and
    # verified): a rowless tag may be a lost reservation, indistinguishable from
    # an ordinary label, so rowless tags are failed closed below while this
    # holds. Row-backed tags still resolve, so a reseeded default state stays
    # usable.
    grants_reduced = snapshot.grants_reduced() is not None

    def _rowless_refusal(tag: dict[str, Any], tag_id: str) -> tuple[str, str] | None:
        # Shared verdict for a tag with no protected row, used on both add and
        # remove. A rowless vocabulary-status tag is always refused (its
        # identity is unknown); otherwise a rowless tag is an ordinary label and
        # passes, EXCEPT while grants are reduced, when it might be a lost
        # reservation and so fails closed.
        if tag.get("status") is True:
            return ("status_identity_unprotected", tag_id)
        if grants_reduced:
            return ("tag_grants_unavailable", tag_id)
        return None

    def _is_status(tag: dict[str, Any]) -> bool:
        # A workflow state for exclusivity purposes is a tag that is EITHER a
        # row-backed status OR carries the vocabulary ``status`` bit with no
        # protected row. The second case is agent-forgeable, which is why it is
        # never a GRANT — but it is still a reason to treat the tag as a state
        # here, so a rowless status cannot be stacked past the exclusivity check.
        _, row_status = snapshot.grant(str(tag.get("id") or ""))
        return row_status or tag.get("status") is True

    for tag in added:
        tag_id = str(tag.get("id") or "")
        if not snapshot.has_row(tag_id):
            # Rowless: a vocabulary-status tag is refused outright (adding it
            # would stack a workflow state the peer-strip cannot later remove,
            # wedging ``set_state``); an ordinary label passes unless grants are
            # reduced, when it might be a lost reservation. Checked BEFORE the
            # policy branch, which only applies to row-backed tags.
            rowless = _rowless_refusal(tag, tag_id)
            if rowless is not None:
                return rowless
            continue
        if snapshot.grant(tag_id)[0] not in ("add-only", "add-remove"):
            return ("tag_policy_denied", tag_id)
    for tag in removed:
        tag_id = str(tag.get("id") or "")
        if not snapshot.has_row(tag_id):
            # Rowless: a vocabulary-status tag is refused (stripping it would
            # silently drop a workflow state the store cannot vouch for); an
            # ordinary label is strippable unless grants are reduced, when it
            # might be a lost reservation.
            rowless = _rowless_refusal(tag, tag_id)
            if rowless is not None:
                return rowless
            continue
        if snapshot.grant(tag_id)[0] != "add-remove":
            return ("tag_policy_denied", tag_id)
    # Mutual exclusivity: at most one workflow-state tag may survive. Fire ONLY
    # when THIS diff raised the status count — the person (ungated) can leave a
    # session carrying two status tags, and a later agent write that touches
    # neither (even a pure remove of its own label) must not be blamed for that
    # pre-existing state. So the offender must be a status tag the diff ADDED.
    result_status = [t for t in result if _is_status(t)]
    if len(result_status) > 1:
        added_ids = {str(t.get("id") or "") for t in added}
        offender = next(
            (str(t.get("id") or "") for t in result_status if str(t.get("id") or "") in added_ids),
            "",
        )
        if offender:
            return ("status_tag_requires_set_state", offender)
    return None


def slot_tag_write_principal(
    request: web.Request, request_app: str, *, what: str
) -> tuple[bool, str, str, str]:
    """Classify a slot tag write's caller: ``(non_person, source, caller, member)``.

    Shared by the two routes that write a session's tags on a caller's behalf,
    ``PUT /api/chat/slots/{slot}/tags`` and ``POST /api/chat/slots/{slot}/drop``,
    so both decide "is this the person?" from one enumeration. Deny-by-default on
    identity: a caller is gated unless POSITIVELY confirmed to be the person.
    Three non-person signals, any one of which gates — the transport alone is
    not enough, because an app token declaring ``/api/chat/*`` reaches these
    routes over the browser transport with NO ``X-Internal-Secret`` yet is still
    an agent:

    * ``request_origin`` source != ``"dashboard"`` — the internal-secret
      (managed MCP) transport, whose secret the token-auth middleware has
      already verified;
    * a resolved app claim (``request_app``) — an app token acting as a
      non-person principal;
    * an admitted crew-member principal (``member:<store>``).

    The mint path's positive predicate (``is_owner_dashboard_request``) is
    deliberately NOT used: it also requires an owner ``user`` claim, which is
    only reliably present once ``KIROCREW_OWNER_ID`` is configured, so keying the
    gate on it would gate the person on a no-owner install. The enumeration is
    the accepted contract, pinned per route by
    ``test_non_person_signal_enumeration_each_leg_gates`` and its drop twin; a
    NEW principal representation added to the auth layer must be added here
    (and to those tests) deliberately.
    """
    origin_source, origin_caller = request_origin(request, what=what, log=logger)
    member_principal = str(request.get(MEMBER_CHAT_PRINCIPAL_KEY) or "")
    non_person = (
        origin_source != "dashboard" or bool(request_app) or member_principal.startswith("member:")
    )
    return non_person, origin_source, origin_caller, member_principal


def agent_slot_tags_refusal(
    state: DashboardState,
    current_ids: list[str],
    new_ids: list[str],
    snapshot: GrantsSnapshot,
) -> tuple[str, str] | None:
    """Grants-policy verdict for a non-person caller replacing a slot's tags.

    Turns the wholesale ``current_ids`` -> ``new_ids`` replace into the add /
    remove / result diff :func:`agent_tag_change_refusal` judges, against the
    live vocabulary, so ``PUT /tags`` and the board drop hold an agent to the
    same rules: a drop is the same diff an agent could send to ``PUT /tags``.
    Call under the tags write lock, with ``current_ids`` read fresh there.

    Fails closed on a REMOVAL it cannot resolve. An id on the slot that is being
    dropped but is absent from the live vocabulary cannot be checked for a
    reservation — and when the vocabulary is NON-AUTHORITATIVE (``tags.json``
    was unreadable at boot, so ``state._tags`` is empty while protected rows
    persist), a tag the owner reserved sits exactly there. Refuse
    ``tag_grants_unavailable`` rather than strip a reservation nobody can see.
    When the vocabulary IS authoritative, an absent id is a dangling reference
    to a deleted tag — benign to drop.
    """
    by_id = {
        t.get("id"): t for t in state._tags if isinstance(t, dict) and isinstance(t.get("id"), str)
    }
    current_set = set(current_ids)
    new_set = set(new_ids)
    if not getattr(state, "_tags_authoritative", True):
        unresolved = [i for i in current_ids if i not in new_set and i not in by_id]
        if unresolved:
            return ("tag_grants_unavailable", unresolved[0])
    added = [by_id[i] for i in new_ids if i not in current_set and i in by_id]
    removed = [by_id[i] for i in current_ids if i not in new_set and i in by_id]
    result = [by_id[i] for i in new_ids if i in by_id]
    return agent_tag_change_refusal(added, removed, result, snapshot)


def resolve_board_tags(
    slot_tag_ids: list[str], vocabulary: list[dict[str, Any]]
) -> list[tuple[str, str]]:
    """Resolve a slot's tag ids to ``[(id, policy)]`` for the [BOARD] line.

    Renders ONLY ids backed by a row in the protected grants store
    (:func:`~kiro_crew.dashboard.chat_tag_grants.has_grant_row`). Rows are
    minted exclusively by the authenticated dashboard CRUD and
    trusted-constant seeding, behind the HMAC provenance chain — so the set
    of strings this function can emit onto the model's trusted context rail
    is agent-unwritable BY CONSTRUCTION. An id smuggled into agent-writable
    ``tags.json`` has no row and is dropped here; the slug grammar and
    injection screen at the render site stay as defense in depth on the
    row-backed names. Caller must refresh the grants cache first.
    """
    # Mirror the applier's guard: ``load_tags`` keeps any row whose ``id`` is
    # truthy, so a list-valued id in agent-writable ``tags.json`` survives to
    # here; unguarded it is unhashable and the TypeError would drop the whole
    # [BOARD] line for every session (the caller swallows the exception).
    by_id = {
        t.get("id"): t for t in vocabulary if isinstance(t, dict) and isinstance(t.get("id"), str)
    }
    resolved: list[tuple[str, str]] = []
    for tid in slot_tag_ids:
        tag = by_id.get(tid)
        if tag is None:
            continue
        tag_id = str(tag.get("id") or "")
        if not has_grant_row(tag_id):
            continue
        resolved.append((tag_id, agent_tag_policy(tag)))
    return resolved
