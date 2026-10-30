"""The session tree: ``session_fork``, ``session_adopt`` and ``session_release``, and the
per-loop mutation lock and append fence they share.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard.chat_fork import (
    _FORK_DIRECTION_HEAD,
    ForkResult,
    ForkSource,
    fork_slot,
    resolve_fork_source,
)
from kiro_crew.dashboard.create_rate_limit import SESSION_CREATE, allow_create
from kiro_crew.dashboard.session_control_parts import session_create
from kiro_crew.dashboard.session_control_parts.caller_access import (
    UNATTENDED_SLOT_PREFIXES,
    SessionControlError,
    _audit,
    _audit_denials,
    _cron_caller,
    _dispatch_bypass,
    caller_slot_key,
    prewarm_enabled_check,
    session_control_enabled,
)
from kiro_crew.dashboard.session_control_parts.crew_filing import (
    _crew_filing_answer,
    _in_turned_on_crew_tree,
)
from kiro_crew.dashboard.session_control_parts.session_create import _record_spawn
from kiro_crew.dashboard.session_control_parts.session_titles import _clean_new_session_title
from kiro_crew.dashboard.session_control_parts.target_authorization import (
    _refuse_ineligible_creator,
    _resolve_slot,
    authorize_target,
)
from kiro_crew.dashboard.state import (
    MAX_LIVE_SLOTS,
    MAX_SLOTS_PER_CREATOR,
    SlotOrigin,
    _safe_folder_tree,
    load_window_off_loop,
)
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState

# The module's original logger name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.session_control")


#: Maximum ``title`` a forked child accepts, matching ``create_session``'s cap.
_MAX_FORK_TITLE_CHARS = 200


def _fork_refusal(response: Any) -> SessionControlError:
    """Translate a ``chat_fork`` refusal into this module's error type.

    ``chat_fork`` refuses with a finished ``web.json_response`` -- its coded
    ``{"error", "code"}`` body IS the refusal, and its sites stay there so the
    error-code ratchet keeps pinning them. This surface speaks
    :class:`SessionControlError`, so the body is read back out rather than the
    fork core learning a second refusal type. A body that does not decode is
    still a refusal, just an unlabelled one.
    """
    error, code = "fork refused", "fork_refused"
    try:
        body = json.loads(response.body or b"{}")
        error = str(body.get("error") or error)
        code = str(body.get("code") or code)
    except (ValueError, AttributeError, TypeError):
        pass
    return SessionControlError(error, status=int(getattr(response, "status", 400)), code=code)


async def fork_session(
    state: "DashboardState",
    *,
    caller_session_key: str,
    source: str = "",
    title: str = "",
    folder_id: str = "",
    at_message_index: int | None = None,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Open a new session that CARRIES a transcript: the dashboard's Fork, for an agent.

    ``create_session`` opens an empty session; this opens one holding a copy of
    *source*'s messages up to and including ``at_message_index`` (the whole
    visible transcript when omitted -- a head fork; tail forks are not offered
    here). The copy is made by the same core the human Fork button runs
    (``chat_fork.fork_slot``), so what the child inherits -- agent, model,
    memory store and mode, project, folder, tags, the ``forked_from`` link --
    is exactly what a person's fork inherits, and for the same memory-boundary
    reasons no override of agent, model or mode is taken here. The exceptions
    are the folder (a Crew Mode caller's child is unfiled) and the sidebar pin:
    a person's fork of a pinned session is pinned, this child never is,
    because pinned marks a session as human-owned.

    *source* defaults to the CALLER'S OWN session, which is the case this verb
    exists for: an agent splitting its own long investigation into several
    sessions that each start with the context it already built. Naming another
    session is a READ of that session's transcript, so it is authorized exactly
    as ``read_messages`` is (``authorize_target``, operation ``fork``): the
    caller must be allowed to read the source. Either way the caller must also
    be an eligible CREATOR -- the same refusal set ``create_session`` applies,
    because a fork manufactures a session the caller then owns.

    What the child gets on top of the human fork: ``title`` (final; else the
    fork's own ``Fork of <parent>``, which the fork's background title pass may
    rename), ``folder_id`` (else the parent's folder, as the human
    fork inherits it), creator attribution (``created_by`` = the caller, so the
    other verbs reach it afterwards and the per-creator ceiling counts it) and the
    caller's session posture (``_trust`` / ``_trust_reads`` -- the same two
    fields ``create_session`` carries, with the same exclusions). It starts IDLE:
    the copied transcript is history, and nothing runs until the caller
    ``session_send``\\ s into it or the person types.

    ``caller_fenced`` is the HTTP gate's ownership-fence verdict, forwarded to
    ``authorize_target`` for the reason every other verb forwards it.
    """
    # Validated first, as in `create_session`: a refused title spends no
    # create-rate token and reads no source transcript.
    clean_title = _clean_new_session_title(
        title, cap=_MAX_FORK_TITLE_CHARS, not_done="session not forked"
    )
    caller_key = caller_slot_key(state, caller_session_key)
    if not caller_key:
        raise SessionControlError(
            "caller session could not be identified", code="caller_unidentified"
        )
    # Same gate order as `create_session`, for the same reasons: the member
    # bypass is resolved on the caller, then the config switch, then the
    # unattended prefix.
    if not session_control_enabled() and not _dispatch_bypass(state, caller_key):
        raise SessionControlError(
            "session control is disabled in config (agent.session_control)",
            code="session_control_disabled",
        )
    if caller_key.startswith(UNATTENDED_SLOT_PREFIXES) and not _cron_caller(caller_key):
        raise SessionControlError(
            "unattended sessions (scheduled runs) cannot fork sessions",
            code="unattended_caller",
        )
    caller_slot = state.get_slot(caller_key)
    if caller_slot is None:
        raise SessionControlError("caller session is not open", code="caller_not_open", status=404)
    # A fork manufactures a session the caller owns, so the caller must be an
    # eligible creator before anything else is read -- see the note on
    # `_refuse_ineligible_creator` for why this set mirrors `authorize_target`'s.
    _refuse_ineligible_creator(state, caller_slot)

    if at_message_index is not None and (
        isinstance(at_message_index, bool) or at_message_index < 0
    ):
        raise SessionControlError(
            "at_message_index must be a non-negative integer", code="invalid_field_type"
        )

    # Resolve the source. An empty `source` is the caller itself. A named source
    # that resolves to the caller is the same case spelled out -- `authorize_target`
    # would refuse it as `self_target`, and rightly so for stop/send/read, but a
    # session reading its OWN transcript to copy it crosses no boundary. Anything
    # else is a peer, and copying a peer's transcript is a read of it, so it is
    # authorized as `read_messages` is: same verb-level requirement, same fence.
    source_ref = (source or "").strip()
    if source_ref:
        try:
            resolved = _resolve_slot(state, source_ref)
        except SessionControlError:
            resolved = None
    else:
        resolved = caller_slot
    if resolved is caller_slot:
        source_slot = caller_slot
    else:
        source_slot = authorize_target(
            state,
            caller_session_key=caller_session_key,
            target=source_ref,
            operation="fork",
            precomputed_ownership_fenced=caller_fenced,
        )

    log = state.conversation_log
    if log is None:
        # Same answer `create_session` gives: without a durable store the copy
        # cannot be persisted, and a fork that vanishes on restart is not a fork.
        raise SessionControlError(
            "session history is unavailable, so the session cannot be persisted",
            code="history_unavailable",
        )

    if folder_id:
        # Confirmed READ-ONLY under the folder-store lock, exactly as
        # `create_session` does and for the same reasons; the Model-B un-hide runs
        # only once the filing has landed on the child.
        def _exists(folders: list[dict[str, Any]]) -> bool:
            return any(str(f.get("id") or "") == folder_id for f in _safe_folder_tree(folders))

        if not await state.read_folders(_exists):
            raise SessionControlError("folder not found", code="folder_not_found")

    # Re-gate adjacent to the allocation, as `create_session` does: every input
    # above was read before this coroutine suspended (the folder confirmation),
    # and both the caller's eligibility and the source's liveness are live state.
    live_caller = state.get_slot(caller_key)
    if live_caller is None or live_caller is not caller_slot:
        raise SessionControlError("caller session is not open", code="caller_not_open", status=404)
    _refuse_ineligible_creator(state, live_caller)
    if state.get_slot(source_slot.key) is not source_slot:
        raise SessionControlError(
            "the source session closed while the fork was being prepared",
            code="target_not_found",
            status=404,
        )
    child_origin = (
        SlotOrigin.CRON
        if _cron_caller(caller_key) or getattr(live_caller, "_origin", "") == SlotOrigin.CRON
        else SlotOrigin.USER
    )
    # A fork spends the same budget and counts against the same ceilings as a
    # create: it is a session the caller manufactured, whatever it starts with.
    if not allow_create(SESSION_CREATE, caller_key):
        raise SessionControlError(
            "too many sessions created recently; retry shortly",
            code="create_rate_limited",
            status=429,
        )
    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        raise SessionControlError(
            f"slot cap reached ({MAX_LIVE_SLOTS})",
            code="slot_cap_reached",
            status=429,
        )
    if state.creator_slot_count(caller_key) >= MAX_SLOTS_PER_CREATOR:
        raise SessionControlError(
            f"per-caller slot cap reached ({MAX_SLOTS_PER_CREATOR})",
            code="creator_slot_cap_reached",
            status=429,
        )

    audit_caller = f"session:{caller_key}"
    # The copy below reads the source's rows: a source restored as a sidebar
    # row loads its window first, off the loop. The source is re-admitted after
    # these awaits, as it already is for the ones that follow.
    await load_window_off_loop(source_slot)
    fork_source = await resolve_fork_source(
        source_slot, audit_caller=audit_caller, audit_operation="session_control.fork"
    )
    if not isinstance(fork_source, ForkSource):
        raise _fork_refusal(fork_source)

    # The session-control half of the child's identity, mirrored from
    # `create_session`. Applied INSIDE `fork_slot`, on the child, before its
    # birth save: `save_slot_off_loop` writes `created_by`, `title` and
    # `folder_id` into the metadata line it creates, so attribution is on disk in
    # the same write as the transcript and before the slot is broadcast. There is
    # no second persistence window -- a child that exists is an attributed child,
    # and a save that fails withdraws the whole child (`fork_slot` pops it), so a
    # retry cannot leave an unreachable duplicate behind.
    _creator_sid = crew_log_emit.session_id_of(getattr(live_caller, "_acp_client", None))
    # Session POSTURE only -- `_trust` and `_trust_reads` -- never
    # `_trusted_patterns` or `_trust_scope`; `create_session` states why. Read
    # INSIDE `_stamp`, not here: `fork_slot` suspends for the transcript read and
    # the memory bind, and an operator revoking the caller's trust in that window
    # (a per-slot revoke cannot reach a child that does not exist yet) must not
    # see the child born with the grant they just withdrew. The values are
    # recorded for the audit line after the stamp has taken them.
    posture: dict[str, bool] = {}

    def _folder_exists_now() -> bool:
        # The COMMITTED folder list, read synchronously: folder mutations run on
        # this loop, so between this read and the assignment `_stamp` makes there
        # is no point at which a delete can land. Same value `read_folders` hands
        # its reader; the lock there exists for readers that hop off the loop.
        return any(str(f.get("id") or "") == folder_id for f in _safe_folder_tree(state._folders))

    def _recheck() -> None:
        # Every containment answer above was read before `fork_slot` suspended
        # (transcript read, memory bind). Re-asserted synchronously at the two
        # points that matter -- the mint and the copy -- so a caller that lost
        # eligibility, a source that gained a channel mirror or moved out of
        # reach, or a folder deleted meanwhile, refuses the fork instead of being
        # copied around. Mirrors the "gate adjacent to the act" discipline
        # `create_session` keeps for its own allocation.
        live = state.get_slot(caller_key)
        if live is None or live is not caller_slot:
            raise SessionControlError(
                "caller session is not open", code="caller_not_open", status=404
            )
        _refuse_ineligible_creator(state, live)
        if state.get_slot(source_slot.key) is not source_slot:
            raise SessionControlError(
                "the source session closed while the fork was being prepared",
                code="target_not_found",
                status=404,
            )
        if source_slot is not caller_slot:
            readmitted = authorize_target(
                state,
                caller_session_key=caller_session_key,
                target=source_ref,
                operation="fork",
                precomputed_ownership_fenced=caller_fenced,
                skip_enabled_check=True,
            )
            if readmitted is not source_slot:
                raise SessionControlError(
                    "the source session changed while the fork was being prepared",
                    code="target_not_found",
                    status=404,
                )
        if folder_id and not _folder_exists_now():
            raise SessionControlError("folder not found", code="folder_not_found")
        # The ceilings, re-read here rather than only at entry: two forks in
        # flight could each pass the entry check and both suspend before their
        # mints. The rate budget above is consumed atomically and bounds the
        # burst; these keep the count itself honest at the mint, which is the
        # same synchronous gate-then-act window `create_session` holds.
        if state.live_slot_count() >= MAX_LIVE_SLOTS:
            raise SessionControlError(
                f"slot cap reached ({MAX_LIVE_SLOTS})", code="slot_cap_reached", status=429
            )
        if state.creator_slot_count(caller_key) >= MAX_SLOTS_PER_CREATOR:
            raise SessionControlError(
                f"per-caller slot cap reached ({MAX_SLOTS_PER_CREATOR})",
                code="creator_slot_cap_reached",
                status=429,
            )

    in_crew_tree = False

    def _stamp(child: Any) -> None:
        nonlocal in_crew_tree

        child._created_by = caller_key
        child._created_by_sid = _creator_sid if len(_creator_sid) <= MAX_ACP_SESSION_ID_LEN else ""
        child._lineage_minted = True
        from kiro_crew.dashboard.approval_posture import trust_carries

        # A card-scoped trust ("Trust this crew member") covers the caller's own
        # tools only, so a fork does not inherit it (``trust_carries``).
        posture["trust"] = trust_carries(live_caller)
        posture["trust_reads"] = bool(getattr(live_caller, "_trust_reads", False))
        child._trust = posture["trust"]
        child._trust_reads = posture["trust_reads"]
        # Follows the caller's later mode changes too (``approval_inheritance``).
        child._posture_inherited = True
        if clean_title:
            child.title = clean_title
            child._titled = True
            # The caller chose this name, so it is final: the fork's own
            # background title pass and the refresh both leave it alone.
            child._title_origin = "user"
        # The caller's tree decides filing, not the transcript source's. Clear
        # even an inherited folder before the fork's first save and broadcast.
        in_crew_tree = _in_turned_on_crew_tree(state, caller_key)
        if in_crew_tree:
            child.folder_id = ""
        elif folder_id:
            child.folder_id = folder_id

    result = await fork_slot(
        state,
        fork_source,
        at_index=at_message_index,
        at_message_id=None,
        direction=_FORK_DIRECTION_HEAD,
        prompt="",
        mode_override=None,
        # An agent-made child: not app-scoped, not a human request-layer session
        # for the session-count survey, and never Jev-routed -- arming a second
        # routed session is the owner's own click, which no caller here made.
        request_app="",
        origin=child_origin,
        count_user_session=False,
        jev_route_allowed=False,
        audit_caller=audit_caller,
        audit_operation="session_control.fork",
        stamp=_stamp,
        recheck=_recheck,
        # Like agent ``session_create``: un-hide only a folder the agent named.
        # An inherited folder the user hid stays hidden; the sidebar still
        # draws it while it holds the active session.
        reveal_folder=bool(folder_id),
    )
    if not isinstance(result, ForkResult):
        raise _fork_refusal(result)
    child = result.slot

    # ``fork_slot`` has already un-hidden a ``folder_id`` named here, before
    # its own push.
    state.push_slots_update()
    lineage_recorded = await _record_spawn(caller_key, child)
    _audit(
        caller_session_key=caller_key,
        operation="fork",
        slot_key=child.key,
        outcome="allowed",
        detail={
            "source": source_slot.key,
            "messages": str(result.messages),
            "folder_id": child.folder_id or "",
            "inherited_trust": "true" if posture.get("trust") else "false",
            "inherited_trust_reads": "true" if posture.get("trust_reads") else "false",
            # See create_session: whether the creating edge landed is kept in the audit
            # log, its durable home, rather than the API reply, which has no reader.
            "lineage_recorded": "true" if lineage_recorded else "false",
        },
    )
    return {
        "ok": True,
        "target": child.key,
        "title": child.title or child.key,
        "source": source_slot.key,
        "messages": result.messages,
        "folder_id": child.folder_id or None,
        **_crew_filing_answer(folder_named=bool(folder_id), in_crew_tree=in_crew_tree),
    }


def _slot_tree_parent(slot_key: str) -> "tuple[bool, str, dict[str, Any]]":
    """Whether the tree is KNOWN right now, *slot_key*'s parent in it, and the tree.

    The first value is the one that must not be collapsed into the others. "This slot
    has no parent" and "I cannot see the tree" are different answers, and an adoption
    that treats them alike skips its own cycle guard: an unseeded projection folds to
    no nodes, and a guard given no nodes admits everything. That state is not exotic --
    it is every gateway between boot and the first lineage seed, so it recurs on each
    restart, and the cycle it would admit is not repaired by the fold, which flattens
    the branch instead.

    ``False`` for an unreadable tree: the crew log is off, the projection is not seeded
    for the store configured now, the fold is INCOMPLETE, or the read raised. ``True``
    with an empty parent means the tree was read whole and the slot is a root.

    NO I/O and never blocks, which is what lets it run on the loop: it reads the
    projection's in-memory fold and asks first whether that fold is seeded for the
    store configured now. An unseeded projection answers "not known" rather than
    seeding itself here -- a seed is a disk read, and a verb that blocks the loop is
    the wrong trade when refusing is honest and the next call succeeds.
    """
    try:
        from kiro_crew.crew_log import emit as crew_log_emit

        if not crew_log_emit.enabled():
            return False, "", {}
        from kiro_crew.crew_log.session_tree_projection import projection

        proj = projection()
        if not proj.seeded_for_current_store:
            return False, "", {}
        # ``reading`` rather than ``nodes``, because this caller DECIDES on an edge.
        # ``TreeReading.incomplete`` says a unit's bytes could not be read or the
        # population was capped, so an edge the guard needs may simply be missing from
        # an otherwise well-formed fold. Admitting an adoption on that fold is a
        # confident wrong answer, and the shape it admits is the cycle this guard
        # exists to refuse -- so an incomplete fold is treated as no tree at all.
        reading = proj.reading()
        if reading.incomplete:
            return False, "", {}
        nodes = reading.nodes
    except Exception:
        logger.debug("session tree parent could not be resolved", exc_info=True)
        return False, "", {}
    node = nodes.get(slot_key)
    parent = getattr(node, "parent_slot", None) if node is not None else None
    return True, (parent or ""), nodes


def _live_sid_of(state: "DashboardState", slot_key: str) -> str:
    """The ACP session id *slot_key*'s crew log is written under, or ``""``.

    For the slot the operation is ALREADY HOLDING, which is the whole of this
    function's contract. A caller resolving some OTHER slot wants
    :func:`_recorded_sid_of` instead: the mapping this reads is one process's, so a
    caller with no context on the slot it is asking about cannot see a stale or
    absent answer, and these ids are written into append-only entries.

    Read from the DURABLE session map rather than from the slot's in-turn ACP client.
    The client is published when a turn starts and cleared when it ends, so reading it
    would answer only for a session that happens to be working -- and the sessions a
    takeover is aimed at are the idle ones.

    ``mapped_sid`` rather than ``get``, which is the difference between two questions.
    ``get`` asks whether the id can still be RESUMED and prunes the entry when the ACP
    transcript is gone; this caller is recording history into a crew log, and a crew log
    unit outlives a truncated transcript. It is also read-only and in-memory, so it is
    safe on the event loop.

    ``""`` for a slot with no mapping: a session whose log this gateway cannot name.
    The caller refuses rather than writing into a log it guessed at.
    """
    try:
        sessions = getattr(state, "sessions", None)
        if sessions is None:
            return ""
        sid = sessions._session_map.mapped_sid(f"dashboard:{slot_key}")
        return sid if isinstance(sid, str) else ""
    except Exception:
        logger.debug("session id for %s could not be resolved", slot_key, exc_info=True)
        return ""


def _replay_pending(state: "DashboardState", slot_key: str) -> bool | None:
    """Whether *slot_key* still owes conversation replay, or ``None`` when unknown.

    The window in which the session map deliberately names the generation BEFORE the
    store the slot is writing: allocation leaves the prior resumable id mapped for a
    replay-pending ACP session on purpose, so a restart can still resume it. A history
    reader taking the mapping's answer inside this window records the wrong log.

    ``None`` rather than ``False`` when the session store cannot answer, because the
    two have opposite consequences for the caller: an unknown window must not license
    the mapping read that a known-closed one does.

    A slot with NO live session is one of those unknown answers, and asking for it
    explicitly is what makes the tri-state real.
    ``SessionRegistry.provider_switch_replay_pending`` is ``bool(session is not None
    and session.provider_switch_replay)``, so a missing session and a live one that
    owes nothing both come back ``False`` -- and only ``False`` licenses the mapping.
    That collapse points at the very case the licence is least safe in: the mapping
    keeps answering a dropped id after a session closes, and a cold start has not
    written one yet, so the id it names is most likely to be the older generation
    exactly when there is nobody to ask about replay.
    """
    try:
        sessions = getattr(state, "sessions", None)
        if sessions is None:
            return None
        key = f"dashboard:{slot_key}"
        if not sessions.has_session(key):
            return None
        return bool(sessions.provider_switch_replay_pending(key))
    except Exception:
        logger.debug("replay state for %s could not be read", slot_key, exc_info=True)
        return None


def _slot_opened_sid(state: "DashboardState", slot_key: str) -> str:
    """The crew log this process recorded when *slot_key* opened, or ``""``."""
    try:
        slot = state._slots.get(slot_key)
        if slot is None:
            return ""
        sid = getattr(slot, "_crew_log_opened_sid", "")
        return sid if isinstance(sid, str) and sid else ""
    except Exception:
        logger.debug("opened crew log for %s could not be read", slot_key, exc_info=True)
        return ""


def _slot_object(state: "DashboardState", slot_key: str) -> "object | None":
    """The live slot OBJECT for *slot_key*, or ``None``, for an identity comparison.

    Every other read here is by key, and a key is not an identity: a slot can close and
    a new session can reopen under the same key, which no key-only check can tell from
    the original. Captured before a suspension and compared after, the object itself
    can.
    """
    try:
        return state._slots.get(slot_key)
    except Exception:
        logger.debug("slot object for %s could not be read", slot_key, exc_info=True)
        return None


def _target_replaced(state: "DashboardState", slot_key: str, observed: "object | None") -> bool:
    """Whether the slot under *slot_key* differs from *observed*, the captured session.

    The identity read-back that a tree verb runs after its final authorization. *observed*
    is the target slot object captured BEFORE the verb's first suspension; this re-reads
    the object under the same key and reports whether it has changed. A close plus a
    reopen under the SAME key inside the suspensions between capture and append puts a
    different session's object there -- open, passing every fence, with a log of its own
    -- which no key-only check can tell from the original. A verb that finds this true
    refuses rather than appending an adoption or release against a session that never took
    part, in an entry nothing later corrects.

    Encapsulated rather than inlined for the reason :func:`_freshest_sid` is: the slot
    captures a verb takes before its resolutions are what the capture-ordering invariant
    rests on, and a bare :func:`_slot_object` read in the verb body AFTER the suspending
    resolution would read as a late capture. The read-back lives here, where it is a
    comparison and not a capture.

    ``True`` when nothing is there now and something was (the target simply closed), which
    is a refusal too -- a verb cannot record a change to a session that is gone. The pure
    close is also caught earlier by the gate's own re-resolve, which is why the close
    tests assert ``target_not_found``; this guard's own case is the reopen the gate admits.
    A ``None`` capture (the target was never a held object) can only become non-``None``,
    which is still a change, so the comparison stays correct at the edges.
    """
    return _slot_object(state, slot_key) is not observed


def _freshest_sid(
    state: "DashboardState", slot_key: str, resolved: str, observed: "object | None"
) -> str:
    """*resolved*, refreshed from the slot's own record when that has moved since.

    SYNCHRONOUS on purpose, and that is the whole reason it exists separately from
    :func:`_recorded_sid_of`. That resolver has to suspend -- it reads the durable
    store -- so a verb resolves its ids BEFORE the final authorization, and the
    authorization's own config warm suspends as well. A slot that opens its next
    store inside that hop advances its ``_crew_log_opened_sid``
    (``Slot.take_crew_log_previous``), and the id resolved before the hop then names
    the store that slot has just replaced. Written into an append-only
    ``session/adopted`` or ``session/released`` entry, that is the same permanent
    wrong answer this resolver exists to prevent, arriving one hop later.

    Re-reading the slot's own record is enough to close the window because it is the
    resolver's FIRST preference and the only one of its three sources that can move
    during the hop: the durable store and the mapping are consulted only when that
    record is empty, and neither is more current than a statement this process just
    wrote about which store the slot is on.

    A dict lookup and an attribute read, so it belongs after the final
    authorization, where nothing may suspend -- refreshing before that gate would
    leave the same window open behind it.

    An empty slot record keeps *resolved*: a slot that CLOSED during the hop does not
    make the store's answer about which log it was on wrong, and falling back to
    ``""`` there would drop an id that is still the best available one.

    *observed* is the slot object read when *resolved* was resolved, and refreshing is
    conditional on it still being the slot under that key. A key is not an identity: a
    close plus a reopen under the SAME key inside the hop puts a different session's
    object there, and its opened-store record names a lineage that never held the
    target -- so refreshing from it would replace a right answer with a confident wrong
    one. Together with the empty-record rule above this makes the refresh never worse
    than not refreshing: it moves an id only when the same slot moved it.
    """
    if observed is None or _slot_object(state, slot_key) is not observed:
        return resolved
    return _slot_opened_sid(state, slot_key) or resolved


async def _recorded_sid_of(state: "DashboardState", slot_key: str) -> str:
    """The crew log *slot_key* is writing, or ``""`` when no id can be written.

    For a slot the caller is NOT holding. The id goes into an append-only
    ``session/adopted`` or ``session/released`` entry as ``parent`` or
    ``previous_parent``, so a wrong id there is a permanent wrong answer about which
    conversation a session came from, and there is no later write that corrects it.
    The citation's slot half distinguishes a parent whose log is unnamed from no parent.

    Resolution checks the live slot's own opened-store record, then the durable store,
    then the process-local mapping. The slot record leads because it is this process's
    own statement about which store the slot is on, and the only source that can name a
    store whose unit the background writer has not written yet. An undecided store
    answer is never a licence to guess from the mapping. A decided empty answer may use
    the mapping only outside the replay-pending window, where the mapping is current.

    Keep this separate from ``chat_runner._slot_predecessor_store``: this resolver runs
    inside a live verb and can read the slot's replay state, while that resolver runs
    before its turn's session exists and cannot ask whether replay is pending.
    """
    if not slot_key:
        return ""

    opened = _slot_opened_sid(state, slot_key)
    if opened:
        return opened

    derived, decided, _complete = await asyncio.to_thread(
        crew_log_emit.slot_previous_store, slot_key
    )
    if derived:
        return derived
    if not decided:
        return ""
    pending = _replay_pending(state, slot_key)
    if pending is not False:
        return ""
    mapped = _live_sid_of(state, slot_key)
    if mapped:
        return mapped
    return ""


async def adopt_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    caller_fenced: bool | None = None,
) -> "dict[str, Any]":
    """Take *target* over, so it hangs under the CALLING session in the tree.

    The adopter is the caller, resolved from the connection rather than named in the
    arguments: a tool that let a caller nominate the parent would let one session
    rearrange another's tree, and nothing in the record would show which of them asked.

    Adopting a session that ALREADY has a live parent is allowed, and the parent it
    replaces is recorded on the entry. A fenced caller still reaches only the sessions
    it manages (``_outside_reach``), so it cannot take over the workers another
    conductor opened until that conductor, or its manager, hands them over with
    ``session_handoff``; an unfenced agent in a person's own tab is not held to that
    (the owner's Conductor with ``agent.session_control`` off is fenced).

    Refused when the target is an ANCESTOR of the caller. The tree would then hold a
    cycle, and a cycle is not a shape the fold can present: it marks every slot on it
    and nests none of them, so the visible result of allowing this would be a whole
    branch silently flattening. The fold guards itself again at fold time for the
    reordering a checkpoint plus a replayed tail can produce; this is the guard that
    refuses the caller rather than absorbing it.

    Refused too when the tree cannot be READ, which is a separate refusal and not a
    special case of the one above. A guard handed no tree admits everything, so an
    unseeded projection would let exactly the adoption this checks for through.

    Every other refusal is :func:`authorize_target`'s -- an unidentifiable or
    unattended caller, a target that is not open, the caller itself, an ephemeral,
    app-scoped, channel-linked or mirrored session on either side, a different
    workspace, and the ownership fence. They are not re-stated here, which is the
    point of routing this verb through the same gate as the others: a session that
    cannot be sent to is not one that can be adopted either.
    """
    # Warmed off-loop with NOTHING suspending before the gate, which is this function's
    # whole reason for existing: ``authorize_target`` is synchronous and its
    # ``session_control_enabled()`` re-reads and validates the config file on the first
    # call after an edit, so an unwarmed gate blocks the shared loop for every other
    # session. The pre-lock gate and the in-lock re-check each get their own warm, since
    # the lock wait between them is exactly the suspension that invalidates the first.
    await prewarm_enabled_check()
    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="adopt",
        precomputed_ownership_fenced=caller_fenced,
    )
    with _audit_denials(
        caller_session_key=caller_session_key, operation="adopt", slot_key=slot.key
    ):
        caller_key = caller_slot_key(state, caller_session_key)
        from kiro_crew.crew_log import emit as crew_log_emit

        # The TARGET's object, captured before the first suspension, so the final
        # authorization can tell the session it admitted apart from a same-key
        # replacement. Everything about the target below -- the gate's re-resolve, the
        # ``_live_sid_of`` mapping read -- is keyed by ``slot.key``, and a key is not an
        # identity: the target can close and a NEW session can open under the same slot
        # key in the awaits ahead (the gate's warm, the lock wait, the id resolution's
        # store read), and every key-only check then answers for the replacement. The
        # adoption would be appended against a conversation that was never adopted, in an
        # entry nothing later corrects. The object itself does not move, so comparing it
        # after the gate is what makes the refusal below possible.
        target_at_resolve = _slot_object(state, slot.key)

        target_sid = _live_sid_of(state, slot.key)
        if not crew_log_emit.enabled() or not target_sid:
            # Checked FIRST because it is the PERMANENT one of the two refusals below: with
            # no recorded tree there is nowhere to write the edge and no later retry helps,
            # so a caller must not be told to come back. Reported rather than answered with a
            # success the tree will not show -- the record IS the edge, and a verb whose
            # record cannot be written has not done anything.
            raise SessionControlError(
                "the session tree is not being recorded on this gateway, so sessions "
                "cannot be adopted",
                status=409,
                code="tree_unavailable",
            )
        async with _tree_mutation_lock():
            _refuse_if_append_pending(slot.key, operation="adoption")
            tree_known, previous_parent, nodes = _slot_tree_parent(slot.key)
            if not tree_known:
                # REFUSED rather than adopted past the guard. The cycle check below is the
                # only thing standing between a takeover and a loop the fold cannot present,
                # and a guard handed no tree admits everything -- so an unreadable tree has
                # to stop the write, not wave it through. Reachable on every gateway between
                # boot and the first lineage seed, which is why it is a refusal a caller can
                # retry rather than a condition worth blocking on: the seed is already being
                # requested by the sidebar's own read path.
                raise SessionControlError(
                    "the session tree is not readable yet on this gateway, so an adoption "
                    "cannot be checked for a loop -- retry in a moment",
                    status=409,
                    code="tree_not_ready",
                )
            # The loop guard reads the settled tree AND every queued, unsettled reparent
            # edge, following both at each node (:func:`_reaches_through_pending`). A
            # concurrent adoption's edge is not in the fold yet, so a settled-only walk
            # would not see that the first adoption is about to put the target above this
            # caller, and the two could close a cycle through the edge neither tree
            # carries. Reading the queued edge ADDITIVELY -- on top of the settled parent,
            # not instead of it -- is what also refuses the longer loop whose closing edge
            # fences no endpoint, and keeps the settled ancestry a failed queued append
            # leaves in place.
            if _reaches_through_pending(slot.key, caller_key, nodes):
                raise SessionControlError(
                    f"{target!r} is already above this session in the tree, so adopting it "
                    "would make a loop",
                    status=409,
                    code="would_cycle",
                )
            # Resolve ids before the final authorization because nothing may suspend between
            # that authorization and handing the append to the writer.
            #
            # The slot OBJECTS are captured first, before the resolution's own store read
            # suspends, so the identity check covers every suspension between reading an id
            # and writing it -- not just the gate's.
            caller_at_resolve = _slot_object(state, caller_key)
            previous_at_resolve = _slot_object(state, previous_parent) if previous_parent else None
            parent_sid = await _recorded_sid_of(state, caller_key)
            previous_parent_sid = (
                await _recorded_sid_of(state, previous_parent) if previous_parent else ""
            )
            # RE-AUTHORIZED here, and this is not the same question the pre-lock call
            # answered. That call decided on a state read before the wait, and the wait is
            # unbounded: the verb ahead in the queue awaits its own append, and anything the
            # gate reads can move meanwhile -- the target can be stopped or closed, the
            # caller's grant can be withdrawn, either side can gain a channel link. Writing
            # on the earlier answer would record an adoption nobody was entitled to at the
            # moment it landed. The pre-lock call is kept because it is the cheap refusal: an
            # unauthorized caller never contends for this lock at all.
            await prewarm_enabled_check()
            slot = authorize_target(
                state,
                caller_session_key=caller_session_key,
                target=target,
                operation="adopt",
                precomputed_ownership_fenced=caller_fenced,
            )
            target_sid = _live_sid_of(state, slot.key)
            if not crew_log_emit.enabled() or not target_sid:
                raise SessionControlError(
                    "the session tree is not being recorded on this gateway, so sessions "
                    "cannot be adopted",
                    status=409,
                    code="tree_unavailable",
                )
            # REFUSED -- not silently re-resolved -- when the slot under the target key is a
            # different session from the one the pre-lock gate admitted. The gate re-resolves BY
            # KEY and ``_live_sid_of`` reads the mapping BY KEY, so a close plus a same-key
            # reopen inside the awaits above leaves both answering for the replacement: an
            # open session that passes every fence, whose log ``target_sid`` now names. The
            # adoption would then be appended against a conversation that was never
            # adopted, and the entry is append-only -- there is no later write that
            # corrects it. The captured object is this process's identity for the admitted
            # session; the replacement is a different object under the same key, so the
            # ``is`` comparison is what the key-only checks cannot do. A caller may retry:
            # a fresh attempt resolves the current session honestly.
            if _target_replaced(state, slot.key, target_at_resolve):
                raise SessionControlError(
                    f"{target!r} was replaced by a new session under the same key, so the "
                    "adoption would name a session that was never adopted -- retry to act "
                    "on the current one",
                    status=409,
                    code="target_replaced",
                )
            # REFRESHED here, synchronously, for the same reason the resolutions happen
            # before the gate: the gate's own warm suspends, and either of these slots can
            # open its next store inside that hop -- leaving the id above naming the store
            # it has just replaced, in an entry nothing later corrects.
            parent_sid = _freshest_sid(state, caller_key, parent_sid, caller_at_resolve)
            previous_parent_sid = (
                _freshest_sid(state, previous_parent, previous_parent_sid, previous_at_resolve)
                if previous_parent
                else ""
            )
            settled, landed = _tree_append_waiter(slot.key)
            # The edge this adoption will add, visible to a concurrent verb's loop guard
            # for as long as the append is in flight. Cleared with the fence by the same
            # future, so a settled or dropped append takes it away.
            _note_pending_edge(slot.key, caller_key, landed)
            crew_log_emit.on_session_adopted(
                target_sid,
                slot=slot.key,
                parent_slot=caller_key,
                parent_sid=parent_sid,
                previous_parent_slot=previous_parent,
                previous_parent_sid=previous_parent_sid,
                on_settled=settled,
            )
            # Inside the lock, so the next verb through it reads a tree that already carries
            # this decision -- the append's completion is also what advances the projection.
            await _tree_append_landed(landed, operation="adoption", target=slot.key)
    _audit(
        caller_session_key=caller_session_key,
        operation="adopt",
        slot_key=slot.key,
        outcome="allowed",
        detail={"previous_parent": previous_parent or "none"},
    )
    # Slot keys only, and deliberately no title. A title is conversation content -- it
    # is generated from the session's own messages -- so returning one to an LLM caller
    # is an output boundary that would owe a redaction pass, and nothing here needs it:
    # the tool reply names the session by the key the caller already used.
    return {
        "target": slot.key,
        "parent": caller_key,
        "previous_parent": previous_parent,
    }


async def release_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    caller_fenced: bool | None = None,
) -> "dict[str, Any]":
    """Let *target* go, so it stands on its own in the tree again.

    Two callers may do it and no others: the target's CURRENT parent, and the target
    itself. The parent because a holder may put down what it holds, and the target
    because a session that has been taken over must not need its holder's cooperation
    to get out -- a conductor that has stopped running would otherwise pin its workers
    under it for good.

    Refused for a target that is already a root. There is nothing to release, and
    writing the entry anyway would put a record of a change into a log where nothing
    changed.

    The self case is the one place this verb departs from
    :func:`authorize_target`'s rules, and it departs from exactly two of them. The
    self-target refusal is waived, since reaching yourself is not reaching a peer. So
    is the ownership fence, for the same reason and no further: that fence bounds a
    caller to the sessions it created, and the session it is itself was never another
    session's to protect. Every other refusal on both sides still applies.
    """
    caller_key = caller_slot_key(state, caller_session_key)
    # Warmed off-loop before the gate, for the reason the adoption warms it.
    await prewarm_enabled_check()
    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="release",
        precomputed_ownership_fenced=caller_fenced,
        allow_self=True,
    )
    with _audit_denials(
        caller_session_key=caller_session_key, operation="release", slot_key=slot.key
    ):
        releasing_self = slot.key == caller_key
        from kiro_crew.crew_log import emit as crew_log_emit

        # The TARGET's object, captured before the first suspension, for the reason the
        # adoption captures it: the gate re-resolves and ``_live_sid_of`` reads BY KEY, so
        # a close plus a same-key reopen inside the awaits ahead would leave both naming a
        # replacement session, and the release would be appended against a conversation
        # that was never released -- append-only, so nothing later corrects it. The
        # ``allow_self`` case is covered too: when the caller IS the target, this just
        # captures the caller's own live object, and a same-key reopen there means a
        # different session is now calling, which the final gate should likewise refuse.
        target_at_resolve = _slot_object(state, slot.key)

        target_sid = _live_sid_of(state, slot.key)
        if not crew_log_emit.enabled() or not target_sid:
            # The permanent refusal first, for the reason the adoption checks it first.
            raise SessionControlError(
                "the session tree is not being recorded on this gateway, so sessions "
                "cannot be released",
                status=409,
                code="tree_unavailable",
            )
        async with _tree_mutation_lock():
            _refuse_if_append_pending(slot.key, operation="release")
            tree_known, previous_parent, _ = _slot_tree_parent(slot.key)
            if not tree_known:
                # The same refusal an adoption makes, for the mirror reason: this verb
                # decides WHO may call it from the parent the tree reports, so an unreadable
                # tree cannot tell "you are not the parent" from "I cannot see who is".
                raise SessionControlError(
                    "the session tree is not readable yet on this gateway, so a release "
                    "cannot be authorized -- retry in a moment",
                    status=409,
                    code="tree_not_ready",
                )
            if not previous_parent:
                raise SessionControlError(
                    f"{target!r} has no parent to be released from",
                    status=409,
                    code="already_root",
                )
            if not releasing_self and previous_parent != caller_key:
                raise SessionControlError(
                    f"{target!r} hangs under another session, so only that session or "
                    f"{target!r} itself can release it",
                    status=403,
                    code="not_parent",
                )
            # Resolve the id before the final authorization because nothing may suspend
            # between that authorization and handing the append to the writer. The slot
            # object is captured first, for the reason the adoption captures its two.
            previous_at_resolve = _slot_object(state, previous_parent)
            previous_parent_sid = await _recorded_sid_of(state, previous_parent)
            # RE-AUTHORIZED inside the lock, for the reason the adoption re-authorizes:
            # the wait is unbounded and everything the gate reads can move during it.
            await prewarm_enabled_check()
            slot = authorize_target(
                state,
                caller_session_key=caller_session_key,
                target=target,
                operation="release",
                precomputed_ownership_fenced=caller_fenced,
                allow_self=True,
            )
            releasing_self = slot.key == caller_key
            target_sid = _live_sid_of(state, slot.key)
            if not crew_log_emit.enabled() or not target_sid:
                raise SessionControlError(
                    "the session tree is not being recorded on this gateway, so sessions "
                    "cannot be released",
                    status=409,
                    code="tree_unavailable",
                )
            # REFUSED -- not silently re-resolved -- when the slot under the target key is a
            # different session from the one the pre-lock gate admitted, for the reason the adoption
            # refuses: the gate and ``_live_sid_of`` both resolve BY KEY, so a same-key
            # reopen inside the awaits above leaves them answering for a replacement, and
            # the release would be appended against a session that was never released. The
            # captured object is this process's identity for the admitted session; a caller
            # may retry to act on the current one.
            if _target_replaced(state, slot.key, target_at_resolve):
                raise SessionControlError(
                    f"{target!r} was replaced by a new session under the same key, so the "
                    "release would name a session that was never released -- retry to act "
                    "on the current one",
                    status=409,
                    code="target_replaced",
                )
            # Refreshed here, synchronously, for the reason the adoption refreshes: the
            # gate's warm suspends, and the parent can open its next store inside it.
            previous_parent_sid = _freshest_sid(
                state, previous_parent, previous_parent_sid, previous_at_resolve
            )
            settled, landed = _tree_append_waiter(slot.key)
            crew_log_emit.on_session_released(
                target_sid,
                slot=slot.key,
                previous_parent_slot=previous_parent,
                previous_parent_sid=previous_parent_sid,
                on_settled=settled,
            )
            # Inside the lock, for the reason the adoption awaits inside it.
            await _tree_append_landed(landed, operation="release", target=slot.key)
    _audit(
        caller_session_key=caller_session_key,
        operation="release",
        slot_key=slot.key,
        outcome="allowed",
        detail={"previous_parent": previous_parent, "self": releasing_self},
    )
    # No title, for the reason the adoption returns none.
    return {
        "target": slot.key,
        "previous_parent": previous_parent,
    }


#: How long a tree verb waits for its own append to be on disk before it refuses. The
#: wait exists because the append IS the operation; the bound exists because the writer
#: retries a refusing filesystem before giving up, and a caller must get an answer either
#: way rather than hanging for as long as the disk stays wedged.
_TREE_APPEND_TIMEOUT = 10.0


#: One mutation lock per EVENT LOOP, not one per process. A lock constructed at import
#: binds to whichever loop first waits on it, and the test suite runs many loops in one
#: process -- a single module-level lock would then raise or, worse, serialize against a
#: loop that has already closed. Keyed by loop identity, created on demand.
_tree_mutation_locks: "dict[int, asyncio.Lock]" = {}


def _tree_mutation_lock() -> "asyncio.Lock":
    """The lock a tree mutation holds across reading the tree and committing to it.

    The two verbs decide from the tree (who the parent is, whether a takeover would
    close a loop) and then write to it, and between those two steps another verb can
    land its own decision. Unserialized, a release authorized against the old parent
    commits after an adoption and puts the session back under a parent that had already
    handed it over -- both writes valid, the later one the one nobody asked for.

    Held from the tree READ through the awaited append, which together with that await
    is what makes the pair atomic: the append's completion also advances the projection,
    so the next verb through this lock reads the state the previous one committed rather
    than the state it started from.

    Not held across :func:`authorize_target`, which does not read the tree. The
    serialized region is the one that decides on tree state.
    """
    loop = asyncio.get_running_loop()
    lock = _tree_mutation_locks.get(id(loop))
    if lock is None:
        lock = asyncio.Lock()
        _tree_mutation_locks[id(loop)] = lock
    return lock


#: Slots whose tree append has been handed to the writer and has NOT settled. Keyed by
#: slot, one entry per in-flight append, cleared when the future resolves either way.
#:
#: The mutation lock alone does not cover this. A verb that times out waiting for its own
#: append raises out of the lock, which releases it -- and the append is still queued, so
#: the next verb through the lock would decide from a tree the writer is about to change
#: and commit against it. Holding the lock until settlement instead would pin the loop's
#: serialized region to a wedged filesystem, which is what the timeout exists to avoid.
#: Fencing the SLOT is the third option: the loop stays free, and no verb decides on a
#: slot whose state is about to move.
_tree_pending_slots: "dict[str, asyncio.Future[bool]]" = {}

#: The queued-but-unsettled reparent EDGE each in-flight append will add, keyed by the
#: child slot (the session that moves) and holding its new parent. One entry per queued
#: adoption, cleared with its fence by the same future's done callback. A release adds no
#: entry: it takes a parent away rather than creating one, so it closes no cycle and
#: contributes no edge a later verb's loop guard must see.
#:
#: Fencing the child slot alone is not enough to stop a cycle. Two overlapping adoptions
#: -- ``t`` under ``a`` queued, then ``a`` under ``t`` -- fence only their own target, and
#: the settled fold carries neither edge, so a settled-only ancestor walk does not see
#: that the first adoption is about to make ``t`` the parent of ``a``. The second adoption
#: passes its loop guard and commits ``a -> t``, closing ``t -> a -> t`` -- a cycle the
#: fold cannot present, flattening the branch. Reading these queued edges ADDITIVELY on
#: top of the settled tree (:func:`_reaches_through_pending`, which follows a slot's
#: settled parent AND its queued parent) is what lets the guard refuse it -- and refuse a
#: longer loop whose closing edge fences no endpoint, since the walk sees the queued edge
#: wherever it falls on the chain, not only on the two slots a fence covers.
_tree_pending_edges: "dict[str, tuple[str, str]]" = {}


def _note_pending_edge(child: str, parent: str, future: "asyncio.Future[bool]") -> None:
    """Record the ``child -> parent`` edge a queued adoption will add, until it settles.

    Registered beside the fence in :data:`_tree_pending_slots` and cleared the same way
    -- a done callback on the SAME future -- so the edge is visible to a concurrent
    verb's loop guard for exactly as long as the append is in flight and goes when it
    settles, with nothing to remember to release.
    """
    _tree_pending_edges[child] = (child, parent)

    def _drop(_f: "asyncio.Future[bool]") -> None:
        if _tree_pending_edges.get(child) == (child, parent):
            del _tree_pending_edges[child]

    future.add_done_callback(_drop)


# Hand ``_note_pending_edge`` to ``session_create._record_spawn`` so a spawn can make its
# queued reparent edge visible to this module's loop guard. The registrar lives here (the
# edge store and the guard are this module's), and this module already imports from
# ``session_create`` (a lower owner layer), so the injection runs the allowed direction --
# ``session_create`` never imports upward into this one. Set once at import, before any
# spawn can run.
session_create._register_spawn_pending_edge = _note_pending_edge


def _reaches_through_pending(target: str, start: str, nodes: "dict[str, Any]") -> bool:
    """Whether *target* is reachable UP from *start* over settled AND queued edges.

    The loop guard for an adoption of *target* under *start* (the caller). A cycle forms
    when *target* is already above the caller -- and "above" has to be read over BOTH the
    settled tree and every queued, unsettled reparent edge, because a queued edge is a
    parent a slot is about to gain and a settled edge is a parent it still has until that
    append commits. A pending ``child -> parent`` is therefore ADDED to the child's
    settled parent, never substituted for it: an append that fails after its retries
    leaves the settled edge in place, so hiding it would clear a loop the tree still
    holds.

    The SETTLED half of every step is delegated to :func:`holders.is_ancestor`, the one
    walk the fold already uses, so this does not re-implement "a cited creator with no
    log" or the on-a-cycle rule and cannot refuse or admit a settled chain the fold would
    judge differently. What is genuinely new here, and the reason a single ``is_ancestor``
    call cannot express it, is the SECOND parent: a slot can have a settled parent and a
    queued parent at once, and the loop may run through either. So the queued edges are
    walked locally -- each ``child -> parent`` bridges two settled sub-walks -- while every
    settled sub-walk is ``is_ancestor``.

    Reachability is grown to a fixpoint: a slot is above the caller when it is the caller,
    a settled ancestor of a slot already known to be above it, or the queued parent of
    one. The caller count bounds the number of queued hops, since each adds one distinct
    pending child.
    """
    from kiro_crew.crew_log.holders import is_ancestor

    if target == start:
        # Adopting a session under itself is the shared gate's refusal, not a cycle this
        # walk reasons about; a slot is never its own ancestor here.
        return False

    def _settled_hits_target(frm: str) -> bool:
        # ``is_ancestor`` answers "is target a settled ancestor of frm"; the equal case is
        # a direct settled edge the caller already sits under, which the walk below seeds.
        return frm == target or is_ancestor(target, frm, nodes)

    # Nodes known to be at or above the caller. Seeded with the caller itself; a queued
    # edge is then followed from any node whose settled ancestry reaches that edge's child.
    reached: set[str] = {start}
    frontier: list[str] = [start]
    while frontier:
        node = frontier.pop()
        if _settled_hits_target(node):
            return True
        for child, parent in _tree_pending_edges.values():
            # The queued edge is in play when its child is at or above a node already
            # known to be above the caller -- i.e. the caller can reach the child through
            # settled edges -- because the child is about to gain *parent* as a parent.
            if child in reached or child == node or is_ancestor(child, node, nodes):
                if parent == target:
                    return True
                if parent not in reached:
                    reached.add(parent)
                    frontier.append(parent)
                if child not in reached:
                    reached.add(child)
    return False


def _tree_append_waiter(slot: str) -> "tuple[Callable[[bool], None], asyncio.Future[bool]]":
    """A ``(on_settled, future)`` pair for one tree append, fencing *slot* until it lands.

    The emitter calls ``on_settled`` from the WRITER's thread, so the result is handed
    back through the loop rather than set directly, and a second call is a no-op.

    Registering the future in :data:`_tree_pending_slots` is what makes the fence
    self-clearing: the entry is removed by a done callback, so it goes when the append
    settles and nothing has to remember to release it. The wait below shields the future
    for the same reason -- a timeout that cancelled it would resolve it, and the fence
    would clear while the append is still queued.

    The TARGET is the only slot fenced. The queued edge this adoption adds moves the
    target's own next state, so a verb that decides against the target during the window
    must wait; the new parent's ANCESTRY does not change by gaining a child, so a loop
    that would close through the queued edge is caught by :func:`_reaches_through_pending`
    reading that edge, not by fencing the parent -- which would only refuse a manager
    adopting a second child in parallel, a move that forms no cycle.
    """
    loop = asyncio.get_running_loop()
    future: "asyncio.Future[bool]" = loop.create_future()

    def _settled(landed: bool) -> None:
        def _set() -> None:
            if not future.done():
                future.set_result(landed)

        loop.call_soon_threadsafe(_set)

    def _unfence(_f: "asyncio.Future[bool]") -> None:
        if _tree_pending_slots.get(slot) is future:
            del _tree_pending_slots[slot]

    _tree_pending_slots[slot] = future
    future.add_done_callback(_unfence)
    return _settled, future


def _refuse_if_append_pending(slot: str, *, operation: str) -> None:
    """Refuse when *slot* has a tree append still in flight. Called inside the lock.

    The tree cannot be reasoned about for a slot whose next state is already queued: a
    decision taken now would be taken against a state the writer is about to replace, and
    it would commit UNDER the queued one rather than after it.

    ``tree_write_pending`` rather than a failure, for the reason the timeout uses that
    code: the earlier append may still land, so the caller is told to read the tree and
    try again rather than that anything went wrong.
    """
    pending = _tree_pending_slots.get(slot)
    if pending is not None and not pending.done():
        raise SessionControlError(
            f"an earlier tree write for {slot!r} is still in flight, so the {operation} "
            "cannot be decided yet -- read the session tree before retrying",
            status=409,
            code="tree_write_pending",
        )


async def _tree_append_landed(
    future: "asyncio.Future[bool]", *, operation: str, target: str
) -> None:
    """Wait for a tree append to be durable, and REFUSE when it was not.

    What this stops the verb from doing is reporting a takeover that never reached the
    disk: the emitter hands the entry to the writer and returns, so without this wait a
    filesystem that refused the append, or a session with no live log to write to, would
    still have answered the caller "adopted" and audited it as allowed.

    A timeout is reported as retryable rather than as a failure, because it is neither:
    the writer may still land the entry. The distinction matters to a caller deciding
    whether to try again, and both codes say which it is.

    The slot's fence is dropped HERE on both resolved paths, rather than being left to the
    future's done callback: that callback runs on a later loop iteration, and a verb whose
    request finishes first would leave the slot fenced for a write that already landed. The
    callback stays as the backstop for a future resolved somewhere other than this wait.
    An adoption records its queued edge under the same future, so that edge is dropped here
    too -- leaving the eager drop to cover only the fence would keep the edge visible to a
    concurrent loop guard for a write already on disk.
    """
    try:
        # SHIELDED, so the timeout cancels only this wait. Cancelling the future itself
        # would resolve it, the fence's done callback would fire, and the slot would be
        # unfenced at the one moment the fence is needed: the append is still queued and
        # the next verb must not decide against a tree it is about to move.
        landed = await asyncio.wait_for(asyncio.shield(future), timeout=_TREE_APPEND_TIMEOUT)
    except asyncio.TimeoutError:
        # The fence is deliberately LEFT standing: the append is still queued.
        raise SessionControlError(
            f"the {operation} of {target!r} was handed to the crew-log writer but has "
            "not landed yet, so it is not confirmed -- read the session tree before "
            "retrying",
            status=409,
            code="tree_write_pending",
        ) from None
    if _tree_pending_slots.get(target) is future:
        del _tree_pending_slots[target]
    if target in _tree_pending_edges:
        del _tree_pending_edges[target]
    if not landed:
        raise SessionControlError(
            f"the {operation} of {target!r} could not be written to the crew log, so "
            "the session tree is unchanged",
            status=500,
            code="tree_write_failed",
        )
