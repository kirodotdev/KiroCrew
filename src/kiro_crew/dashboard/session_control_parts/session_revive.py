"""Archived sessions: listing the revivable history and ``session_revive``."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard import session_managers
from kiro_crew.dashboard.chat_folders import _slot_meta_txn_lock, _unhide_folder, note_folder_filed
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_utils import _history_key_for, slot_history_key
from kiro_crew.dashboard.create_rate_limit import SESSION_CREATE, allow_create
from kiro_crew.dashboard.session_control_parts.caller_access import (
    UNATTENDED_SLOT_PREFIXES,
    SessionControlError,
    _audit,
    _caller_is_ownership_fenced,
    _created_by_other,
    prewarm_enabled_check,
)
from kiro_crew.dashboard.session_control_parts.delivery_containment import (
    _has_channel_mirror,
    _probe_channel_mirror_for_key,
)
from kiro_crew.dashboard.session_control_parts.session_status import _bounded_status_title
from kiro_crew.dashboard.session_control_parts.session_tree import _slot_tree_parent
from kiro_crew.dashboard.session_control_parts.target_authorization import (
    _check_caller_slot_fields,
    _deny_factory,
    _live_target_refusal,
    _not_creator_reason,
    _resolve_slot,
    refuse_caller_identity,
    refuse_caller_surface,
)
from kiro_crew.dashboard.state import (
    MAX_LIVE_SLOTS,
    MAX_SLOTS_PER_CREATOR,
    _normalize_slot_key,
    _safe_folder_tree,
)
from kiro_crew.sel import sel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# The module's original logger name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.session_control")


def _fold_slot_key(key: str) -> str:
    """Fold a transcript stem or session key to the slot key it names.

    ``_normalize_slot_key`` strips ONE transport prefix per call, so a doubled
    ``dashboard:dashboard:member-x`` folds to ``dashboard_member-x``: a key the
    ``member-``/``cron-`` prefix guards do not match, whose history key is
    nevertheless the real ``dashboard:member-x`` transcript the resume core would
    publish. Fold to the fixed point so the guards test the key that is actually
    revived (or, for :func:`list_archived_sessions`, listed as revivable).
    """
    for _ in range(8):
        folded = _normalize_slot_key(key)
        if folded == key:
            return key
        key = folded
    return key


def _scan_archived_candidates(
    log: Any, key_candidate: str, wanted_title: str
) -> list[tuple[str, str, dict[str, Any]]]:
    """The DISK half of archived-target resolution: runs in a worker thread.

    Returns every on-disk dashboard session that *key_candidate* names or whose
    title equals *wanted_title*, as ``(slot_key, history_key, metadata)``. It
    reads the history store only -- metadata lines and the session catalog --
    and never touches the live slot table, which belongs to the event loop and
    is consulted by the caller after this returns.
    """
    found: dict[str, tuple[str, dict[str, Any]]] = {}
    _fold = _fold_slot_key

    def _consider(slot_key: str) -> None:
        if not slot_key or slot_key in found:
            return
        history_key = _history_key_for(slot_key)
        meta, readable = log.get_metadata_status(history_key)
        if readable and meta:
            found[slot_key] = (history_key, dict(meta))

    # Every candidate is taken from the CATALOG's spelling of the filename stem,
    # never echoed from the caller: on a case-insensitive filesystem (APFS, NTFS)
    # ``Chat-7`` opens ``dashboard_chat-7.jsonl`` just as well, so a metadata
    # probe on the caller's spelling would succeed while the live-slot dedup
    # (exact ``_slots`` lookup) and the resume core's own live check (exact
    # session-key compare) both miss the ``chat-7`` that is already open -- and
    # a second slot would be published over the same transcript file. Resolving
    # through the catalog keys ``found`` by the stem the store reports, so the
    # key handed back is the one the live table and the resume compare against.
    rows = [
        row for row in log.list_sessions() if str(row.get("key") or "").startswith("dashboard_")
    ]
    if key_candidate:
        wanted_stem = _fold(key_candidate)
        if not wanted_stem.startswith("dashboard_"):
            wanted_stem = f"dashboard_{wanted_stem}"
        for row in rows:
            stem = str(row.get("key") or "")
            if stem.casefold() == wanted_stem.casefold():
                _consider(_fold(stem))
    if wanted_title:
        for row in rows:
            if (str(row.get("title") or "")).strip().casefold() == wanted_title:
                _consider(_fold(str(row.get("key") or "")))
    return [(k, hk, meta) for k, (hk, meta) in found.items()]


async def _resolve_archived_target(
    state: "DashboardState",
    log: Any,
    target: str,
    deny: "Callable[..., SessionControlError]",
    live_refusal: "Callable[[str], Awaitable[SessionControlError]]",
) -> tuple[str, str, dict[str, Any]]:
    """Find the ARCHIVED dashboard session *target* names.

    Accepts what a caller actually holds -- a slot key (``chat-7-...``), the
    ``dashboard:<slot>`` session key, the ``dashboard_<slot>`` filename stem
    ``list_sessions`` reports, or an exact case-insensitive title -- and returns
    ``(slot_key, history_key, metadata)`` for exactly one on-disk session that
    has no live slot. Mirrors :func:`_resolve_slot`'s doctrine: every form is
    resolved before anything is returned, and two DIFFERENT sessions matching
    across forms is refused as ambiguous rather than silently preferring one.

    The disk scan (metadata lines, the session catalog) runs off the loop in
    :func:`_scan_archived_candidates`; the live-slot table is read here, on the
    loop, both before (a live match is refused with its key) and after the scan
    (a candidate that went live during it is dropped rather than revived twice).

    A target that IS live is handed to *live_refusal* with its live key, and
    that builder decides what the caller learns: :func:`revive_session` authorizes
    the live slot as any live target first, so a protected session answers with
    that refusal and reveals neither its existence nor its key, while one the
    caller may reach is named so the caller can just address it.
    """
    # Circular at runtime: ``members`` imports this module.
    from kiro_crew import members as members_mod

    try:
        live = _resolve_slot(state, target)
    except SessionControlError as exc:
        raise deny(exc.message, exc.code, status=exc.status) from exc
    if live is not None:
        raise await live_refusal(live.key)

    # The key forms fold to one slot key; a bare title never does (it is not a
    # dashboard prefix and ``_normalize_slot_key`` would mangle spaces), so only
    # a target that looks like a key is tried as one.
    stripped = target.strip()
    key_candidate = stripped if stripped and not any(ch.isspace() for ch in stripped) else ""
    candidates = await asyncio.to_thread(
        _scan_archived_candidates, log, key_candidate, stripped.casefold()
    )
    found = [(k, hk, meta) for k, hk, meta in candidates if state.get_slot(k) is None]
    if candidates and not found:
        # Every on-disk match went live during the scan (a human resume click
        # racing this call): answer as the pre-scan probe would have -- through
        # the same authorize-then-name builder -- rather than "not found" for a
        # session the caller can see.
        raise await live_refusal(candidates[0][0])

    if len(found) > 1:
        raise deny(
            f"{len(found)} archived sessions match {target!r} (as a session key, "
            "transcript name, or title) -- address it by its session key instead",
            "ambiguous_target",
            status=409,
        )
    if not found:
        raise deny(f"no archived session matches {target!r}", "target_not_found", status=404)
    slot_key, history_key, meta = found[0]
    if slot_key.casefold().startswith(members_mod.DM_SLOT_KEY_PREFIX):
        # A crew member's DM thread is opened only through its own roster route,
        # which re-checks the member binding; session control has no business
        # resurrecting one under a caller's name.
        raise deny("member threads are not addressable", "member_thread_target")
    return slot_key, history_key, meta


def _archived_session_is_mirrored(state: "DashboardState", history_key: str) -> bool:
    """:func:`_has_channel_mirror` for a session that has no live slot yet.

    Fail-closed like the refusal paths: a store that cannot answer counts as
    mirrored, so an unreadable link never opens the boundary.
    """
    probed = _probe_channel_mirror_for_key(state, history_key)
    return True if probed is None else bool(probed)


def _archived_session_is_channel_linked(state: "DashboardState", history_key: str) -> bool:
    """Whether the GATEWAY's session store records a channel link for an archived
    session -- the inbound origin (``get_origin_link``, the conversation the
    channel dispatcher recorded as this session's own) or the Slack thread binding
    (``get_slack_link``).

    The metadata line's ``linked_session_key`` / ``channel_origin`` say the same
    thing, but that line is a file an agent's tools can edit; the session store is
    gateway-owned, so this is the corroborating read the channel-link boundary
    rests on for a revive. Fail-closed: a store that cannot answer counts as
    linked. A state with no session store answers "not linked", the same posture
    as the mirror probe, since there is no record to consult.
    """
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return False
    try:
        get_origin = getattr(sessions, "get_origin_link", None)
        if get_origin is not None and get_origin(history_key):
            return True
        get_slack = getattr(sessions, "get_slack_link", None)
        if get_slack is not None:
            thread_ts, _channel = get_slack(history_key)
            if thread_ts:
                return True
    except Exception:
        logger.debug("channel-link probe failed", exc_info=True)
        return True
    return False


def _archived_link_and_mirror(state: "DashboardState", history_key: str) -> tuple[bool, bool]:
    """``(channel_linked, mirrored)`` for an archived session -- the WORKER-THREAD half.

    Both probes read the gateway's session store through its guarded getters,
    and the store's off-loop writer holds that same lock across a temp-file write
    and rename, so a probe on the event loop can stall every session behind one
    disk write. :func:`revive_session` therefore runs the pair through
    ``asyncio.to_thread``, the way :func:`_scan_archived_candidates` already runs
    the catalog scan. Each half keeps its own fail-closed reading.
    """
    return (
        _archived_session_is_channel_linked(state, history_key),
        _archived_session_is_mirrored(state, history_key),
    )


def _revived_slot_refusal(slot: "_ChatSlot", caller_slot: "_ChatSlot") -> tuple[str, str] | None:
    """The target-side refusal, if any, that the HYDRATED slot now earns.

    :func:`revive_session` answers the target-side boundaries from the archived
    metadata line before the resume, but the resume core re-reads that line after
    its threaded transcript read and hydrates the slot from the fresh copy. The
    same fields are therefore read back off the live slot -- the reads
    :func:`authorize_target` makes on a live target, same order, same wording and
    codes -- so a line rewritten inside the resume's window cannot publish a
    slot the pre-resume answer would have refused. Returns ``(reason, code)`` or
    ``None`` when every boundary still holds.
    """
    if (refusal := _live_target_refusal(slot)) is not None:
        return refusal
    if (getattr(slot, "workspace", "default") or "default") != (
        getattr(caller_slot, "workspace", "default") or "default"
    ):
        return ("target session belongs to a different workspace", "workspace_mismatch")
    return None


#: The creator a revived slot carries when its transcript names one that the creation
#: records do not confirm. Non-empty, so the slot stays FENCED (an agent made it), and it
#: contains ``:``, so no slot key and no caller key can ever equal it.
UNVERIFIED_CREATOR = "unverified:creator"

#: Rebinds started on a cancelled revive, held so the loop does not drop them.
_PENDING_REBINDS: "set[asyncio.Task[None]]" = set()


def _rebind_in_background(slot: Any, meta: "Mapping[str, Any]") -> None:
    """Finish the manager rebind of a revive whose request was cancelled after commit."""
    task = asyncio.get_running_loop().create_task(
        session_managers.rebind_resumed(slot, str(meta.get("tab_id") or ""))
    )
    _PENDING_REBINDS.add(task)
    task.add_done_callback(_PENDING_REBINDS.discard)


def _creation_parent(slot_key: str) -> str:
    """*slot_key*'s creator from the crew log's CREATION records, or ``""``. No I/O.

    Never the sidebar parent: an adoption or a handoff moves that, and it says nothing
    about who made the session.
    """
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log.session_tree_projection import creator_of

    if not crew_log_emit.enabled():
        return ""
    return creator_of(slot_key)


def _archived_target_refusal(
    slot_key: str,
    meta: "Mapping[str, Any]",
    *,
    caller_key: str,
    caller_workspace: str,
    ownership_fenced: bool,
    linked: bool,
    mirrored: bool,
) -> tuple[str, str] | None:
    """Why :func:`revive_session` refuses archived *slot_key* for this caller, or None.

    The ONE statement of revive's target-side refusals, as ``(reason, code)``:
    :func:`revive_session` raises the answer and :func:`_scan_revivable_history`
    skips any row it refuses, so a refusal added here is enforced by the revive
    and hidden from the listing at once. *linked* and *mirrored* are the caller's
    session-store reading (:func:`_archived_link_and_mirror`); passing False for
    both yields the metadata-only refusals, a subset of the full answer, which is
    how the scan avoids a store probe for a row the metadata already refuses.
    The ``not_creator`` reason is generic; revive substitutes its caller-specific
    wording.
    """
    if slot_key.casefold().startswith(UNATTENDED_SLOT_PREFIXES):
        return "unattended sessions (scheduled runs) cannot be controlled", "unattended_target"
    if str(meta.get("memory_mode") or "persistent") != "persistent":
        return "incognito and temporary sessions are not addressable", "ephemeral_target"
    if meta.get("app"):
        return "app-scoped sessions are not addressable", "app_scoped_target"
    if meta.get("linked_session_key") or meta.get("channel_origin") or linked:
        # Both readings: the metadata line AND the gateway-owned session store,
        # so a channel link scrubbed from the editable line is still seen.
        return "channel-linked sessions are not addressable", "linked_session_target"
    if mirrored:
        # The outbound-mirror boundary ``_has_channel_mirror`` holds for a live
        # slot, read on the history key because the link lives in the session
        # store, not in the transcript: an archived dashboard session can still
        # be republishing to a channel the moment it is reopened. Unknown reads
        # as mirrored, the refusal paths' fail-closed direction.
        return "sessions mirrored to a channel are not addressable", "mirrored_target"
    if str(meta.get("workspace") or "default") != caller_workspace:
        return "target session belongs to a different workspace", "workspace_mismatch"
    if ownership_fenced:
        if session_managers.recorded_manager(slot_key, str(meta.get("tab_id") or "")) == (
            caller_key
        ):
            # The session was handed to the caller. The managers record is gateway-
            # written under the fenced crew-log root, so it needs no corroboration,
            # and it is the record the send fence reads for the live session.
            return None
        if str(meta.get("created_by") or "") != caller_key:
            return "target session was created by another session", "not_creator"
        # The metadata line is a file an agent's file tools can edit
        # (``chat_persistence`` says so where it restores ``created_by``), so for a
        # FENCED caller the claim above is only a claim: a caller that rewrote
        # ``created_by`` in an archived transcript would otherwise revive a session
        # it never made and then hold it. The ownership is therefore corroborated
        # against the one record those tools cannot reach -- the crew log's
        # session-tree lineage, written by this gateway at the child's first turn
        # from the in-process ``_lineage_minted`` witness and never from disk
        # metadata. Fail closed when that record is not readable (crew log off,
        # projection unseeded or incomplete) or names a different parent: an
        # unverifiable ownership is refused, not assumed. A caller that is NOT
        # fenced (the person's own tab) is not gated on ownership at all, so the
        # human recovery this tool exists for is unaffected.
        if _creation_parent(slot_key) != caller_key:
            return (
                "archived session ownership could not be verified from gateway records "
                "(a fenced caller needs the crew log's session-tree lineage, which is "
                "missing when the log is off, its projection is unseeded, or the "
                "session predates it)",
                "ownership_unverified",
            )
    return None


async def revive_session(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    folder_id: str = "",
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Bring the ARCHIVED session *target* back into the live sidebar.

    The mirror of :func:`close_target`: a close archives a tab to history; this
    pulls one history session back up into a live slot, the same thing the
    History tab's resume click does. It reuses the dashboard's own resume core
    (:func:`chat_handlers.resume_slot_from_history`), so a controlled revive
    and a human click share one materialisation, one set of member-pin and
    delete/recreate barriers and one set of refusal codes.

    Authorization is :func:`authorize_target`'s, applied to the session's
    PERSISTED metadata because there is no live slot yet to read: the same
    caller-side refusals (via the shared helpers), then the target-side ones --
    unattended, ephemeral, app-scoped, channel-linked, a different workspace --
    read from the metadata line, and the same ownership fence: an ownership-
    fenced caller (an agent-created session, a crew member, a scheduled run)
    may revive only a session whose ``created_by`` is itself AND whose crew-log
    lineage names it as parent -- the metadata line is agent-editable, the
    lineage record is not, so the fence fails closed when the record cannot be
    read.

    ``folder_id`` files the revived slot the way ``create_session`` does, after
    the revive has landed; a folder that does not exist refuses the whole call
    BEFORE anything is revived, so a refusal leaves history untouched. The
    revived slot keeps its own creator: reviving is not creating, so ownership
    is never transferred to the caller.
    """
    # Circular at runtime: ``chat_handlers`` imports this module.
    from kiro_crew.dashboard.chat_handlers import ResumeRefusal, resume_slot_from_history

    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the revive
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()

    deny = _deny_factory(caller_session_key=caller_session_key, operation="revive", target=target)

    caller_key = refuse_caller_identity(
        state, caller_session_key=caller_session_key, deny=deny, skip_enabled_check=False
    )

    log = state.conversation_log
    if log is None:
        raise deny("session history is unavailable", "history_unavailable")

    # The whole caller side BEFORE the target is resolved: a caller refused for
    # its own identity or containment must learn nothing from the attempt, and
    # resolving first would make the refusal an existence oracle (404 for an
    # absent archived session, 403 for a present one). Off the loop because the
    # caller-side set ends in the session-store mirror probe, whose guarded
    # getter shares the lock the store's writer holds across its disk write.
    caller_slot = await asyncio.to_thread(
        refuse_caller_surface, state, caller_key=caller_key, deny=deny
    )
    # The fence verdict, resolved ONCE and before any target is named: the
    # live-target builder below needs it on the loop, and evaluating it inline
    # there would be a config read (disk on a cache miss) in a synchronous span.
    ownership_fenced = (
        caller_fenced
        if caller_fenced is not None
        else await asyncio.to_thread(_caller_is_ownership_fenced, state, caller_key)
    )

    async def _live_refusal(live_key: str) -> SessionControlError:
        # A target that turns out to be LIVE (before the scan, during it, or
        # by the time the resume ran) is a live slot hydrated from a line this
        # call never checked, so it is authorized exactly as a live target is
        # before anything about it is said: a slot the caller may not reach
        # answers with THAT refusal and reveals neither its existence nor its
        # key; one it may reach is named so the caller can address it.
        #
        # Split the way the resume hook and ``final_check`` are split. The
        # checks answerable from live slot fields run ON the loop, so nothing
        # can mutate the slot between the read and the answer; the two
        # session-store probes (the caller's owner-DM exemption and the target's
        # outbound mirror) go to a worker, because their guarded getters share
        # the lock the store's writer holds across its disk write; and the
        # store-free set runs once more on the loop after those awaits, right
        # before the key is named -- a workspace commit or a link rebind landing
        # inside the worker hop would otherwise be answered from stale fields.
        def _store_free() -> "_ChatSlot":
            live = state.get_slot(live_key)
            if live is None:
                raise deny(f"no open session matches {target!r}", "target_not_found", status=404)
            if live.key == caller_key:
                raise deny("a session cannot control itself", "self_target")
            live_caller = _check_caller_slot_fields(state, caller_key, deny)
            if (refusal := _revived_slot_refusal(live, live_caller)) is not None:
                raise deny(refusal[0], refusal[1])
            if ownership_fenced and _created_by_other(live, caller_key):
                raise deny(
                    _not_creator_reason(state, caller_key, live_caller, caller_fenced),
                    "not_creator",
                )
            return live

        live = _store_free()
        await asyncio.to_thread(refuse_caller_surface, state, caller_key=caller_key, deny=deny)
        if await asyncio.to_thread(_has_channel_mirror, state, live):
            raise deny("sessions mirrored to a channel are not addressable", "mirrored_target")
        _store_free()
        return deny(
            f"{target!r} is already open as `{live_key}`; address it directly",
            "target_already_live",
            status=409,
        )

    slot_key, history_key, meta = await _resolve_archived_target(
        state, log, target, deny, _live_refusal
    )

    # Target-side containment, read from the metadata line the resume would
    # rehydrate the slot from. The session-store probes (inbound link, Slack
    # thread binding, outbound mirror) go through the store's guarded getters,
    # whose lock an off-loop writer holds across its disk write, so they run in
    # a worker thread like the catalog scan does rather than on the loop.
    linked, mirrored = await asyncio.to_thread(_archived_link_and_mirror, state, history_key)
    refusal = _archived_target_refusal(
        slot_key,
        meta,
        caller_key=caller_key,
        caller_workspace=str(getattr(caller_slot, "workspace", "default") or "default"),
        ownership_fenced=ownership_fenced,
        linked=linked,
        mirrored=mirrored,
    )
    if refusal is not None:
        reason, code = refusal
        if code == "not_creator":
            reason = _not_creator_reason(state, caller_key, caller_slot, caller_fenced)
        raise deny(reason, code)

    if folder_id:

        def _exists(folders: list[dict[str, Any]]) -> bool:
            return any(str(f.get("id") or "") == folder_id for f in _safe_folder_tree(folders))

        if not await state.read_folders(_exists):
            raise deny("folder not found", "folder_not_found", status=400)

    # A revive materialises the same resource a create does -- a live slot with
    # a hydrated transcript -- so it spends the same budgets: the per-caller
    # creation window and the per-creator and global slot caps.
    if not allow_create(SESSION_CREATE, caller_key):
        raise deny(
            "too many sessions opened recently; retry shortly", "create_rate_limited", status=429
        )
    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        raise deny(f"slot cap reached ({MAX_LIVE_SLOTS})", "slot_cap_reached", status=429)
    if state.creator_slot_count(caller_key) >= MAX_SLOTS_PER_CREATOR:
        raise deny(
            f"per-caller slot cap reached ({MAX_SLOTS_PER_CREATOR})",
            "creator_slot_cap_reached",
            status=429,
        )

    def _plain(reason: str, code: str, status: int = 403) -> SessionControlError:
        # A non-auditing denier for the hook: its refusal is audited ONCE, by the
        # ``deny`` that raises it after the resume returns.
        return SessionControlError(reason, status=status, code=code)

    async def _containment(built: "_ChatSlot") -> ResumeRefusal | None:
        # The LAST gate, on the slot the resume hydrated and before it is
        # published (the core holds it retracted and under construction while
        # this awaits). Every answer read before the resume came from a metadata
        # snapshot taken before the transcript read and from a caller slot that
        # can change at any moment, so each is re-asserted here on live state:
        # the caller's own eligibility, the four target-side fields off the built
        # slot, the workspace, the gateway store's link and mirror records for the
        # target, the mirror on the built slot itself, and the two ceilings. A
        # refusal discards the built slot inside the core; nothing was published.
        try:
            live_caller = await asyncio.to_thread(
                refuse_caller_surface, state, caller_key=caller_key, deny=_plain
            )
        except SessionControlError as exc:
            return ResumeRefusal(exc.message, exc.code, exc.status)
        refusal = _revived_slot_refusal(built, live_caller)
        if refusal is not None:
            return ResumeRefusal(refusal[0], refusal[1], 403)
        linked, mirrored = await asyncio.to_thread(_archived_link_and_mirror, state, history_key)
        if linked:
            return ResumeRefusal(
                "channel-linked sessions are not addressable", "linked_session_target", 403
            )
        if mirrored or await asyncio.to_thread(_has_channel_mirror, state, built):
            return ResumeRefusal(
                "sessions mirrored to a channel are not addressable", "mirrored_target", 403
            )
        claimed_creator = str(meta.get("created_by") or "")
        if not getattr(built, "_created_by", "") and claimed_creator:
            # Creator attribution restored the way a restart's rehydrate restores
            # it (see ``_rehydrate_slot_from_history``): the ownership fence reads
            # ``_created_by``, and a fenced caller that revived its own worker
            # must still own it, or its next ``session_send`` is refused
            # ``not_creator``. Restored ONLY when the gateway-authored lineage
            # corroborates the claim: ``created_by`` is read from an agent-editable
            # file, and a stamp taken on the line's word alone would let a fenced
            # agent that wrote its own key into an archived transcript be handed
            # session-control reach over the operator's conversation the moment
            # an unfenced tab revived it (the fenced branch above checks the
            # lineage only for a fenced REVIVER, not for the claimed creator). An
            # unknown tree or a different parent leaves the field blank, which
            # matches no caller. Attribution ONLY -- ``_lineage_minted`` stays
            # False, and the reviver is never written here: reviving is not
            # creating.
            # From the CREATION records, never the sidebar parent a handoff moves. A
            # claim they do not confirm keeps the slot fenced under a creator no
            # caller matches, rather than blank, which would read as a person's tab.
            if _creation_parent(slot_key) == claimed_creator:
                built._created_by = claimed_creator
            else:
                built._created_by = UNVERIFIED_CREATOR
        # Charge the reviver for this slot under the per-caller cap (see
        # ``creator_slot_count``) without touching its ownership. Set before the
        # ceilings are read so the count includes this slot once published.
        built._revived_by = caller_key
        # The ceilings. ``live_slot_count`` counts allocated-but-unpublished slots
        # too, so the built slot IS in it (retracted from the table, still under
        # construction) and the compare is strictly-over, the same exclusive
        # ceiling every other allocation path applies; ``creator_slot_count``
        # walks the table only, so the built slot is not in it and the compare
        # is inclusive.
        if state.live_slot_count() > MAX_LIVE_SLOTS:
            return ResumeRefusal(f"slot cap reached ({MAX_LIVE_SLOTS})", "slot_cap_reached", 429)
        if state.creator_slot_count(caller_key) >= MAX_SLOTS_PER_CREATOR:
            return ResumeRefusal(
                f"per-caller slot cap reached ({MAX_SLOTS_PER_CREATOR})",
                "creator_slot_cap_reached",
                429,
            )
        return None

    def _final_check(built: "_ChatSlot") -> ResumeRefusal | None:
        # SYNCHRONOUS last word, run by the core after its last await and right
        # before the publish: everything the hook answered without a store read
        # is re-asserted here on live state, so nothing can run between these
        # reads and the slot becoming reachable. The store-backed probes (link,
        # mirror) are the hook's; their guarded getters may not run on the loop.
        try:
            live_caller = _check_caller_slot_fields(state, caller_key, _plain)
        except SessionControlError as exc:
            return ResumeRefusal(exc.message, exc.code, exc.status)
        refusal = _revived_slot_refusal(built, live_caller)
        if refusal is not None:
            return ResumeRefusal(refusal[0], refusal[1], 403)
        if state.live_slot_count() > MAX_LIVE_SLOTS:
            return ResumeRefusal(f"slot cap reached ({MAX_LIVE_SLOTS})", "slot_cap_reached", 429)
        if state.creator_slot_count(caller_key) >= MAX_SLOTS_PER_CREATOR:
            return ResumeRefusal(
                f"per-caller slot cap reached ({MAX_SLOTS_PER_CREATOR})",
                "creator_slot_cap_reached",
                429,
            )
        from kiro_crew.dashboard.approval_inheritance import (
            inherit_posture,
            is_profile_worker,
        )

        if built._created_by == caller_key and not is_profile_worker(built):
            # Its verified creator brought it back: it takes the creator's posture
            # as it is now, as session_create hands it at birth, and follows the
            # creator's later mode changes (``approval_inheritance``). A crewmate's
            # own worker is left to its Profile, which its first turn seeds
            # (``crewmate_approval.seed_for_turn``), as create leaves it.
            inherit_posture(built, live_caller)
        return None

    outcome = await resume_slot_from_history(
        state,
        name=slot_key,
        history_key=history_key,
        caller_label=f"session:{caller_key}",
        containment=_containment,
        final_check=_final_check,
        rebind_manager=False,
    )
    if outcome.refusal is not None:
        raise deny(outcome.refusal.error, outcome.refusal.code, status=outcome.refusal.status)
    slot = outcome.slot
    assert slot is not None

    if outcome.already_live:
        # Lost a race with a human click or another reviver between the
        # resolution above and the resume: same authorize-then-name answer as
        # the two pre-resume live branches, through the one builder.
        raise await _live_refusal(slot.key)

    def _audit_allowed(filed_now: bool) -> None:
        # The revive is committed the moment the core published the slot, so
        # the allowed record is owed whatever happens to the filing after it:
        # a cancelled request that skipped this row would leave a durable revive
        # with no SEL record, and audit rows are not reconstructible.
        _audit(
            caller_session_key=caller_key,
            operation="revive",
            slot_key=slot.key,
            outcome="allowed",
            detail={
                "messages": str(outcome.total),
                "folder_id": slot.folder_id or "",
                "filed": "true" if filed_now else "false",
                "was_closed": "true" if meta.get("closed") else "false",
            },
        )

    filed = False
    if folder_id and folder_id != slot.folder_id:
        # The revive is already COMMITTED -- the slot is live in the sidebar --
        # so a failure to file it must not propagate as "could not revive": the
        # caller would retry and be told the target is live. Same posture as
        # ``create_session``'s post-commit folder un-hide.
        #
        # Under the state-wide slot-metadata txn lock, the span the folder
        # endpoint (``api_chat_slot_folder``) and the sidebar filing writers
        # serialize their own mutate/save/rollback under: the un-hide and the
        # save below are awaits on an already-published slot, so a folder PATCH
        # could commit inside them and an unconditional ``previous`` restore
        # would then erase an acknowledged placement. Under the lock a rollback
        # can only undo this call's own write; the value compare stays as the
        # defense against the non-endpoint writers that do not take the lock.
        # The lock acquisition itself is an await (``LoopBoundLock`` under
        # contention), so the cancellation guard sits OUTSIDE ``async with``: a
        # cancellation delivered while waiting for the lock has written nothing
        # to roll back but must still leave the allowed audit row for a revive
        # that has already committed. The audit is emitted exactly once: here on
        # the exceptional path, or by the ``_audit_allowed(filed)`` call after
        # the block on the normal one (an ``Exception`` inside is swallowed into
        # the normal path, so it does not reach this arm).
        try:
            async with _slot_meta_txn_lock(state):
                previous = slot.folder_id
                previous_changed = slot._folder_changed
                # Re-read under the lock: filed there meanwhile by another writer
                # means nothing to do, and nothing this call may later roll back.
                if folder_id != previous:
                    slot.folder_id = folder_id
                    slot._folder_changed = True

                    def _roll_back() -> None:
                        if slot.folder_id == folder_id:
                            slot.folder_id = previous
                            slot._folder_changed = previous_changed

                    try:
                        if not await _unhide_folder(state, folder_id):
                            _roll_back()
                        elif await save_slot_off_loop(
                            state,
                            slot,
                            force=True,
                            # Strict, not best-effort: the default swallows a raised
                            # save and answers True (marking the slot dirty for the
                            # periodic flush), which would report ``filed: true`` for
                            # a placement that is not on disk and is lost if the
                            # gateway restarts before that flush. A raise takes the
                            # rollback arm below and reports ``filed: false``.
                            best_effort=False,
                            expected_history_key=slot_history_key(slot),
                        ):
                            note_folder_filed(state, folder_id)
                            filed = True
                        else:
                            _roll_back()
                            slot._dirty = True
                    except Exception:
                        logger.warning(
                            "revive_session: %s revived but filing it into %s failed",
                            slot.key,
                            folder_id,
                            exc_info=True,
                        )
                        _roll_back()
                        slot._dirty = True
                    except BaseException:
                        # A cancellation (gateway shutdown, the MCP client's timeout
                        # closing the socket) inside the two awaits above is not an
                        # ``Exception``: without this arm it would skip the rollback,
                        # leaving the provisional placement live and dirty. Undo this
                        # call's write and let it propagate to the audit arm below.
                        _roll_back()
                        slot._dirty = True
                        state.push_slots_update()
                        raise
        except BaseException:
            _audit_allowed(False)
            _rebind_in_background(slot, meta)
            raise
        state.push_slots_update()

    _audit_allowed(filed)
    # After the audit: the revive is committed, and its allowed row is owed first.
    await asyncio.shield(session_managers.rebind_resumed(slot, str(meta.get("tab_id") or "")))
    return {
        "ok": True,
        "target": slot.key,
        "title": slot.title or slot.key,
        "messages": outcome.total,
        "folder_id": slot.folder_id or "",
        "filed": filed,
    }


#: Default and ceiling on the rows one ``session_history_list`` call returns. The
#: MCP schema (``SESSION_HISTORY_LIST_SCHEMA``) enforces the same ceiling; the
#: backend clamps too, because the route is reachable without the schema layer.
DEFAULT_HISTORY_LIST_ROWS = 20


MAX_HISTORY_LIST_ROWS = 100


#: How many history catalog entries one scan walks, newest first. It bounds the
#: catalog read (at most this many stems and mtimes) and the ``seen`` set. The
#: walk normally ends earlier, at the first eligible row past ``limit``; when it
#: runs out of entries first, the answer says ``scan_truncated``, because older
#: sessions were never examined. Revivable sessions sit near the top of a
#: newest-first catalog, so a real archive meets the row cap long before this.
MAX_HISTORY_SCAN_ENTRIES = 5000


#: Longest ``folder_id`` a history row carries. Folder ids are 12 hex characters
#: (``chat_folders``), but the value is read from agent-writable transcript
#: metadata, so a longer one is reported as unfiled instead of being retained. It
#: is dropped rather than cut short because a truncated id names a different
#: folder, or none.
MAX_HISTORY_FOLDER_ID_CHARS = 64


def _scan_revivable_history(
    state: "DashboardState",
    log: Any,
    *,
    caller_key: str,
    caller_workspace: str,
    ownership_fenced: bool,
    folder_id: str,
    live_keys: frozenset[str],
    limit: int,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """The DISK half of :func:`list_archived_sessions`: runs in a worker thread.

    Walks the newest ``MAX_HISTORY_SCAN_ENTRIES`` transcripts of the history
    catalog (``newest_session_stems``, stems and mtimes only) newest first and
    keeps each archived dashboard session :func:`revive_session` would accept
    for this caller. The target-side refusals are revive's own
    (:func:`_archived_target_refusal`), asked first on the metadata alone and
    then, for a row that passes, with the gateway session-store link and
    outbound-mirror probe; member DM keys and the *folder_id* filter are the
    listing's own. A row that would be refused is skipped, not reported, so the
    listing never names a session the caller could not revive.

    *live_keys* is the live-slot snapshot the caller took on the loop; the slot
    table itself belongs to the loop and is not read here. Returns
    ``(rows, lineage_unknown, truncated)``. *rows* holds up to ``limit + 1``
    eligible rows: the walk stops at the first one past *limit*, which only
    says more exist, so no metadata read or store probe is spent beyond it.
    *truncated* is set when the walk ran out of its ``MAX_HISTORY_SCAN_ENTRIES``
    budget before that. *lineage_unknown* is set when a fenced caller had
    candidates the lineage could not vouch for (crew log off, unseeded,
    incomplete).
    """
    # Circular at runtime: ``members`` imports this module.
    from kiro_crew import members as members_mod

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    lineage_unknown = False
    caller_facts: dict[str, Any] = {
        "caller_key": caller_key,
        "caller_workspace": caller_workspace,
        "ownership_fenced": ownership_fenced,
    }
    # A bounded catalog read: only the newest ``MAX_HISTORY_SCAN_ENTRIES``
    # transcripts' stems and mtimes, with no title or metadata, so nothing the
    # walk starts from grows with the archive or with a persisted field.
    entries, truncated = log.newest_session_stems(MAX_HISTORY_SCAN_ENTRIES)
    for stem, modified in entries:
        if not stem.startswith("dashboard_"):
            continue
        slot_key = _fold_slot_key(stem)
        if not slot_key or slot_key in seen or slot_key in live_keys:
            continue
        seen.add(slot_key)
        folded = slot_key.casefold()
        if folded.startswith(UNATTENDED_SLOT_PREFIXES) or folded.startswith(
            members_mod.DM_SLOT_KEY_PREFIX
        ):
            continue
        history_key = _history_key_for(slot_key)
        meta, readable = log.get_metadata_status(history_key)
        if not readable or not meta:
            continue
        if folder_id and str(meta.get("folder_id") or "") != folder_id:
            continue

        # Metadata first: the refusals it alone yields are a subset of the full
        # answer, so a row they refuse needs no session-store probe.
        refusal = _archived_target_refusal(
            slot_key, meta, linked=False, mirrored=False, **caller_facts
        )
        if refusal is None:
            linked, mirrored = _archived_link_and_mirror(state, history_key)
            refusal = _archived_target_refusal(
                slot_key, meta, linked=linked, mirrored=mirrored, **caller_facts
            )
        if refusal is not None:
            if refusal[1] == "ownership_unverified" and not _slot_tree_parent(slot_key)[0]:
                lineage_unknown = True
            continue
        last_active = datetime.fromtimestamp(modified, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rows.append(
            {
                "target": slot_key,
                "title": _bounded_status_title(str(meta.get("title") or slot_key)),
                "last_active": last_active,
                "folder_id": _bounded_folder_id(meta.get("folder_id")),
            }
        )
        if len(rows) > limit:
            # One past the cap answers "more exist"; nothing past it is read.
            return rows, lineage_unknown, False
    return rows, lineage_unknown, truncated


async def list_archived_sessions(
    state: "DashboardState",
    *,
    caller_session_key: str,
    folder_id: str = "",
    limit: int = DEFAULT_HISTORY_LIST_ROWS,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """The archived sessions this caller could :func:`revive_session`, newest first.

    READ-ONLY. The finding half of a revive: ``session_revive`` needs a key or an
    exact title, and nothing else on this surface lists archived sessions. The
    listing is filtered by the rules revive enforces (see
    :func:`_scan_revivable_history`), so every row is a target the caller may
    pass on. The caller side runs first and in full, the same way revive runs
    it before resolving any target, so a refused caller learns nothing about
    what is on disk.

    ``folder_id`` narrows to sessions filed in that folder; an id that is not in
    the tree is refused ``folder_not_found`` rather than answered with an empty
    list that looks like "nothing archived there".
    """
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the read
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()

    deny = _deny_factory(caller_session_key=caller_session_key, operation="history_list", target="")
    caller_key = refuse_caller_identity(
        state, caller_session_key=caller_session_key, deny=deny, skip_enabled_check=False
    )
    log = state.conversation_log
    if log is None:
        raise deny("session history is unavailable", "history_unavailable")
    caller_slot = await asyncio.to_thread(
        refuse_caller_surface, state, caller_key=caller_key, deny=deny
    )
    ownership_fenced = (
        caller_fenced
        if caller_fenced is not None
        else await asyncio.to_thread(_caller_is_ownership_fenced, state, caller_key)
    )
    limit = max(1, min(int(limit), MAX_HISTORY_LIST_ROWS))

    if folder_id:

        def _exists(folders: list[dict[str, Any]]) -> bool:
            return any(str(f.get("id") or "") == folder_id for f in _safe_folder_tree(folders))

        if not await state.read_folders(_exists):
            raise deny("folder not found", "folder_not_found", status=400)

    caller_workspace = str(getattr(caller_slot, "workspace", "default") or "default")
    live_keys = frozenset(slot.key for slot in list(state._slots.values()))
    rows, lineage_unknown, truncated = await asyncio.to_thread(
        _scan_revivable_history,
        state,
        log,
        caller_key=caller_key,
        caller_workspace=caller_workspace,
        ownership_fenced=ownership_fenced,
        folder_id=folder_id,
        live_keys=live_keys,
        limit=limit,
    )

    # The rows carry TITLES of the person's closed sessions. The caller gate ran
    # before the scan, but a channel mirror can be bound onto the calling slot
    # while the scan is on a worker thread, so it is re-asserted before anything
    # is returned -- the reason ``created_session_status`` re-checks too. The
    # workspace is compared because every row was filtered on it. Both reads
    # touch the session store, so they run off the event loop like the first pass.
    caller_slot_now = await asyncio.to_thread(
        refuse_caller_surface, state, caller_key=caller_key, deny=deny
    )
    if str(getattr(caller_slot_now, "workspace", "default") or "default") != caller_workspace:
        raise deny(
            "the calling session moved workspace while history was being read; call again",
            "caller_changed_mid_read",
        )
    # The ownership fence can tighten during the scan too (a channel-DM link bound
    # onto the caller). Rows filtered under the looser rule would then leak other
    # people's archived titles, so a tightened fence withholds the whole answer.
    if caller_fenced is None and not ownership_fenced:
        if await asyncio.to_thread(_caller_is_ownership_fenced, state, caller_key):
            raise deny(
                "the calling session's ownership rules changed while history was being read; "
                "call again",
                "caller_changed_mid_read",
            )
    # A row revived by someone else during the scan is live now; drop it here,
    # where the slot table can be read, so the list stays "archived only".
    # ``more`` is read off the scan, before that drop: a sentinel row revived
    # meanwhile still proves the walk stopped early, so older rows were unread.
    more = len(rows) > limit or truncated
    rows = [row for row in rows if state.get_slot(row["target"]) is None][:limit]

    _audit(
        caller_session_key=caller_session_key,
        operation="history_list",
        slot_key=caller_key,
        outcome="allowed",
        detail={
            "rows": len(rows),
            "more": more,
            "truncated": truncated,
            "folder": bool(folder_id),
        },
    )
    return {
        "ok": True,
        "sessions": rows,
        # More revivable sessions may exist past this page: the walk found one
        # past ``limit``, or it ran out of catalog entries before finishing.
        "more": more,
        # The walk stopped at ``MAX_HISTORY_SCAN_ENTRIES`` catalog entries, so
        # older sessions were not examined at all.
        "scan_truncated": truncated,
        # Only a fenced caller's rows rest on the lineage; for anyone else this
        # is always False.
        "lineage_unknown": lineage_unknown,
    }


def _bounded_folder_id(value: object) -> str:
    """A persisted folder id, or ``""`` when it is longer than any real one."""
    text = str(value or "")
    return text if len(text) <= MAX_HISTORY_FOLDER_ID_CHARS else ""


async def revive_own_archived_worker(
    state: "DashboardState",
    caller_session_key: str,
    target: str,
    *,
    caller_fenced: bool | None = None,
) -> str:
    """*target*, or the live key of the archived worker it names, revived for its crewmate.

    Only for a caller whose attested record is a crewmate's conductor
    (``mate_worker.is_conductor_execution``) naming a session that is not open.
    The revive is :func:`revive_session`, whose ownership fence
    admits a fenced caller only for a session it created AND whose gateway-written
    lineage names it as parent, so transcript metadata alone revives nothing.
    *caller_fenced* is the fence the request was admitted under, forwarded so the
    revive cannot recompute a looser one from config that moved since. Any
    refusal, and every other caller, keeps *target* as given, so the send answers
    ``target_not_found`` exactly as before.
    """
    from kiro_crew.dashboard.session_control_parts.target_authorization import _resolve_slot
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.mate_worker import is_conductor_execution

    try:
        if _resolve_slot(state, target) is not None:
            return target
    except Exception:  # noqa: BLE001 - an unresolvable target keeps its own answer
        return target

    def _caller_is_conductor() -> bool:
        from kiro_crew.config.loader import KiroCrewConfig

        execution = read_session_execution(caller_session_key)
        return is_conductor_execution(execution, KiroCrewConfig.load())

    try:
        is_conductor = await asyncio.to_thread(_caller_is_conductor)
    except Exception:  # noqa: BLE001 - an unreadable caller record revives nothing
        return target
    if not is_conductor:
        return target
    try:
        revived = await revive_session(
            state, caller_session_key=caller_session_key, target=target, caller_fenced=caller_fenced
        )
    except SessionControlError as exc:
        logger.info("own-worker revive declined for %r: %s", target, exc.code)
        return target
    return str(revived.get("target") or target)
