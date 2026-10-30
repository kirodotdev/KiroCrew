"""Approval decisions: the approval-mode change (``POST /api/chat/mode`` and its core
``apply_approval_mode``), the approve route, and the caller gates both run first.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    import time

    from kiro_crew.dashboard.chat_handlers import (
        _SLOT_SCOPED_TRUST_MODES,
        DashboardState,
        GrantDeadline,
        _ChatSlot,
        _deny_app_yolo,
        _mark_permission_resolved,
        approval_mode_permitted,
        base_consent_pattern,
        base_trust_patterns,
        cron_creator_admission,
        cron_mode_refusal,
        cron_slot_creator,
        deny_app_session_control,
        deny_non_dashboard_caller,
        effective_session_key,
        exact_trust_pattern,
        grant_deadline,
        logger,
        parse_grant_ttl,
        read_bounded_json,
        record_trust_grant,
        row_mid,
        safety_override,
        sel,
        session_grant,
        slot_not_found,
        trust_carries,
        yolo_policy_permits,
    )


async def _internal_session_approval_refusal(
    request: web.Request, operation: str
) -> "web.Response | None":
    """Deny bare internal-auth callers on the session-approval routes.

    ``X-Internal-Secret`` proves only that the caller runs on this loopback — not
    who it is.  Any process holding ``.local_secret`` can otherwise reach these
    routes and set approval mode for every session.

    The one admitted internal path is a cron script presenting its attested
    ``cron:<id>`` key on ``chat_mode``.  That caller's own ``cron_creator_admission``
    and ``cron_mode_refusal`` fences apply after this guard, so the cron still only
    touches sessions it created and only with the two modes its gate allows.

    Every other internal-auth caller — bare secret with no key, any key on
    ``chat_slot_approve`` or the ``/api/approvals/{id}/{action}`` route, even a
    verified ``dashboard:`` or ``subagent:`` key — is refused here like a non-owner.
    Self-approval (setting trust on one's own slot) is the same harm as setting it on
    any other slot, so it is not an exception.
    """
    from kiro_crew.dashboard.handlers._shared import (
        _owner_denial_response,
        member_request_scope,
    )

    scope = await member_request_scope(request)
    session = scope.session or ""
    if scope.verified and session.startswith("cron:") and operation == "chat_mode":
        return None
    try:
        sel().log_api_access(
            caller=session or "internal",
            operation=operation,
            outcome="denied",
            source="internal_auth",
            error="internal-auth caller is not admitted on session approval",
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.debug("SEL audit failed for internal-auth session approval denial", exc_info=True)
    return _owner_denial_response(request, "forbidden")


async def deny_session_approval_caller(request: web.Request, operation: str) -> web.Response | None:
    """Allow the dashboard owner or an app with the live session approval grant.

    Internal-auth callers are handled by :func:`_internal_session_approval_refusal`:
    only a cron script with its attested key is admitted on ``chat_mode``; every
    other internal caller is refused like a non-owner.

    The grant verdict is this request's one read (:func:`session_grant`), shared
    with the per-slot checkpoint that already ran for a per-slot route.
    """
    if request.get("internal_auth") is True:
        return await _internal_session_approval_refusal(request, operation)
    request_app = str(request.get("app") or "")
    if not request_app:
        return deny_non_dashboard_caller(request, operation)

    if await session_grant(request, request_app):
        try:
            sel().log_api_access(
                caller=request_app,
                operation=operation,
                outcome="allowed",
                source="app_isolation",
                resources="permissions.sessionApproval",
            )
        except Exception:  # pragma: no cover - audit is best-effort
            logger.debug("SEL audit failed for %s grant", operation, exc_info=True)
        return None
    try:
        sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            error="session approval permission not granted",
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.debug("SEL audit failed for %s denial", operation, exc_info=True)
    return web.json_response(
        {
            "error": "app cannot manage session approvals",
            "code": "session_approval_not_granted",
        },
        status=403,
    )


def bare_approval_decide_allowed(request: web.Request) -> bool:
    """Whether *request* may decide a tool approval by its request id alone.

    A request id is minted by the caller that raised the approval and can recur,
    so a decide that names only the id resolves whichever request holds it at
    that moment. The owner-bound targets (``origin=coordinator`` with the exact
    slot and instance, or ``origin: "native"`` with the row's ``request_mid``)
    name the request a card actually showed and are accepted from every caller.

    The bare form is accepted only from the dashboard user's own browser
    session: the auth middleware sets a PRESENT, empty ``app`` claim for a
    validated dashboard session token (the same positive proof
    ``is_owner_dashboard_request`` reads), and ``internal_auth`` marks a loopback
    ``X-Internal-Secret`` caller (an agent, MCP tool or cron), which is not a
    person looking at a card. Every other caller (internal secret, or any
    request the middleware left without a dashboard-user claim) must send an
    owner-bound target. App callers are judged by their own grant path before
    this check and never reach it.
    """
    if request.get("internal_auth") is True:
        return False
    return request.get("app", None) == ""


def _pending_elicitation_ids(slot: Any) -> set[str]:
    """Request ids of this slot's elicitation rows that a person has not answered.

    An MCP server's elicitation shares the slot's approval registry with tool
    permissions so a Stop, Continue and the Board see one open question, but it
    is not a tool approval: only the dashboard owner answers it, through the
    elicitation route. The approve route and the trust/yolo sweep read this set to
    leave those futures alone -- an ``approved`` written into one would otherwise
    be read as a person's answer to a question nobody saw.
    """
    ids: set[str] = set()
    for message in slot.messages:
        if message.get("role") != "elicitation":
            continue
        try:
            meta = json.loads(message.get("cls") or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(meta, dict) and not meta.get("resolved"):
            rid = meta.get("request_id")
            if isinstance(rid, str) and rid:
                ids.add(rid)
    return ids


def _approval_target_required() -> web.Response:
    return web.json_response(
        {
            "error": "an owner-bound approval target is required",
            "code": "approval_target_required",
        },
        status=404,
    )


async def _end_trust_scopes(slots: list[Any], audit_caller: Callable[[str], str]) -> None:
    """End the app-armed scoped grants these slots carry, when a person picks Normal or Reads.

    The header shows a live ``_trust_scope`` as Trust, so any choice narrower
    than Trust must end that grant too, or the slot keeps auto-approving writes
    under the narrower label: ``SafetyOverride.deactivate()`` only ends the
    process-wide override and never touches a scoped grant. The SEL record names
    the person's choice as the cause, apart from the grant's own
    ``deactivate_scope`` record.

    Only the grants: the caller clears every slot flag and stored policy after
    this returns, in one pass with no await, so a concurrent mode change cannot
    land between a slot's flags and its session's policy.
    """
    for slot in slots:
        scope = str(getattr(slot, "_trust_scope", "") or "")
        if scope:
            await _end_trust_scope(scope, audit_caller(f"dashboard:{slot.key}"))


def _still_owning(state: Any, slots: list[Any], key: str) -> list[Any]:
    """The slots from ``slots`` that are still live in ``state`` and still address ``key``.

    A revoke resolves its slots before awaiting the grant ends; a slot closed and
    re-created on the same key during that await is a different slot that the
    request was never authorized for, so its flags and the session policy stay.
    """
    return [s for s in slots if state._slots.get(s.key) is s and effective_session_key(s) == key]


async def _end_trust_scope(scope: str, caller: str) -> None:
    await asyncio.to_thread(safety_override().deactivate_scope, scope)
    try:
        await asyncio.to_thread(
            sel().log_api_access,
            caller=caller,
            operation="approval_mode.scope_cleared_by_user",
            outcome="disabled",
            resources=f"scope:{scope}",
        )
    except Exception:
        logger.warning("SEL audit failed for scope clear on %s", scope, exc_info=True)


async def api_chat_mode(request: web.Request) -> web.Response:
    """POST /api/chat/mode — set tool approval mode.

    Modes:
      - ``normal``: reset to interactive (ask for each tool)
      - ``trust_reads``: auto-approve reads for active slot
      - ``trust``: auto-approve tools for active slot
      - ``yolo``: auto-approve all tools everywhere

    Unlike the per-tool approve endpoint, this doesn't require a
    pending approval — it preemptively sets the mode for future tools.

    A caller that presents an attested ``cron:<job id>`` key, which is what
    ``ScriptContext.set_session_mode`` does, is held to ``trust`` and
    ``trust_reads`` on one named slot that same cron created: the switch mirror
    in ``private_chat_route_refusal`` answers first while session control is
    off, ``cron_mode_refusal`` refuses every other mode and an unnamed slot, and
    ``cron_creator_admission`` refuses a slot another creator made. An admitted
    call is recorded as ``mode_change:<mode>`` under the cron's own key. Each
    refusal is recorded by ``_audit_cron_chat_denial`` as a ``chat.control``
    denial attributed to ``internal``, naming the route, the slot and the mode,
    the shape every cron-gate refusal on the chat routes shares.
    """
    state: DashboardState = request.app["state"]
    denied = await deny_session_approval_caller(request, "chat_mode")
    if denied is not None:
        return denied
    request_app = str(request.get("app") or "")
    # The attested ``cron:<job id>`` key of a script cron, or ``""``. A cron sets
    # one slot-scoped posture on a session it created and nothing else; the three
    # checks below are the cron gate the other two chat routes already apply, and
    # like those they key on the attested key the caller presents.
    cron_creator = await cron_slot_creator(request)

    def audit_caller(dashboard_label: str) -> str:
        """App tokens are attributed to the app, a cron to its own key; dashboard
        callers keep their original per-site labels (slot, background, mode) so
        SEL history stays comparable across releases."""
        if request_app:
            return f"app:{request_app}"
        return cron_creator or dashboard_label

    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    mode = body.get("mode", "normal")
    # A cron is held to ``trust`` / ``trust_reads`` on one named slot, before the
    # mode reaches governance or the override: its ``yolo`` is refused as a cron's,
    # whatever the policy says, and its unnamed slot is never the all-slots request.
    cron_refused = await cron_mode_refusal(request, cron_creator, mode, body.get("slot"))
    if cron_refused is not None:
        return cron_refused
    # The grant is per-slot: Normal, Reads and Trust on ONE named user session.
    # YOLO is process-global and stays dashboard-only.
    if request_app and mode == "yolo":
        return _deny_app_yolo(request_app, "chat_mode:yolo")
    # Governance gate: the ``approval_modes`` policy scope governs ``yolo`` and
    # only ``yolo``. Refuse a denied mode here, before any mutation, so it is
    # blocked regardless of the UI. ``normal`` is the interactive floor, and
    # ``trust`` / ``trust_reads`` are non-deniable because their live consumption
    # predicates are not gated -- ``approval_mode_permitted`` short-circuits all
    # three, and a policy naming one is refused at parse time. Kept as a general
    # mode check rather than a YOLO special case so that widening the scope needs
    # no change here; YOLO is additionally guarded at arming in ``safety_override``.
    #
    # ``yolo`` reads the PUSHED verdict, which is resolved when a ceiling is installed
    # and so needs no thread: there is one answer for the ceiling in force, and a
    # governance-evaluation error resolved to a deny at that install. Every other mode
    # is non-deniable and short-circuits inside ``approval_mode_permitted`` -- but an
    # unrecognised ``mode`` string does reach governance, so that branch keeps its
    # offload.
    if mode == "yolo":
        if not yolo_policy_permits():
            return _deny_approval_mode(
                caller=audit_caller("dashboard:chat_mode"),
                operation=f"chat_mode:{mode}",
                mode=mode,
                resource=str(body.get("slot") or ""),
            )
    elif not await asyncio.to_thread(approval_mode_permitted, mode):
        return _deny_approval_mode(
            caller=audit_caller("dashboard:chat_mode"),
            operation=f"chat_mode:{mode}",
            mode=mode,
            resource=str(body.get("slot") or ""),
        )
    raw_slot = body.get("slot")
    slot_key = raw_slot or None
    # Test the NORMALIZED value: ``""`` (and any other falsy slot) collapses to
    # ``None`` below, which is the all-slots path -- so an app sending an empty
    # string must be refused exactly like one sending no slot at all.
    if request_app and slot_key is None:
        return web.json_response(
            {
                "ok": False,
                "error": "app mode changes require a slot",
                "code": "slot_required",
            },
            status=400,
        )

    # Refuse an unresolvable slot key BEFORE anything mutates: a slot-scoped
    # request that names a slot which does not exist — or which is not a string
    # at all — must neither widen to every slot nor revoke the global
    # grant, and its refusal must leave grant and slots exactly as they were.
    # Falsy non-strings (``[]``, ``{}``, ``0``, ``False``) are refused on the
    # raw value here, before ``raw_slot or None`` can erase them into the
    # documented all-slots request. The resolved slot reference is what every
    # branch writes through — nothing below re-indexes state._slots[slot_key]
    # after the offloaded deactivate await, so a concurrent slot deletion
    # cannot open a check/use gap. ``yolo`` is global and ignores ``slot``
    # entirely (a stale key must not refuse it).
    slot, denied = None, None
    if mode != "yolo":
        if raw_slot is not None and not isinstance(raw_slot, str):
            denied = web.json_response({"ok": False, "error": "unknown slot"}, status=400)
        elif slot_key is not None:
            # An absent key is the documented "all slots" request; a present
            # key must name a live slot or the whole request is refused here,
            # before any mutation.
            slot = state._slots.get(slot_key)
            if slot is None:
                denied = web.json_response({"ok": False, "error": "unknown slot"}, status=400)
    if denied is not None:
        # An app gets the 404 a slot it may not control gets, so this answer
        # cannot tell it which session names are live.
        return slot_not_found() if request_app else denied

    if cron_creator:
        # The mode rule above refused an unnamed slot and the resolution above
        # refused a dead one, so a cron reaches here with ONE live slot. Judge
        # its creator now, with no await between that resolution and this read,
        # so the fence judges the same slot object every branch below writes
        # through. A slot the cron did not create answers 403 ``not_creator``.
        assert slot is not None  # a cron names a slot, and it resolved above
        refusal, _, _ = await cron_creator_admission(request, state, slot_key, cron_creator)
        if refusal is not None:
            return refusal

    if request_app:
        assert slot is not None  # app requests require and resolve a slot above
        # A FRESH grant read: the body upload and the governance check above
        # can outlast a removal of the flag since the caller check read it.
        denied = await deny_app_session_control(
            request, request_app, slot, str(slot_key), "chat_mode", fresh=True
        )
        if denied is not None:
            return denied

    return await apply_approval_mode(
        state,
        mode=mode,
        slot=slot,
        slot_key=slot_key,
        request_app=request_app,
        cron_creator=cron_creator,
        audit_caller=audit_caller,
    )


async def apply_approval_mode(
    state: DashboardState,
    *,
    mode: str,
    slot: _ChatSlot | None,
    slot_key: str | None,
    request_app: str,
    cron_creator: str,
    audit_caller: Callable[[str], str],
) -> web.Response:
    """Apply an approval mode to ONE resolved slot (or every slot when ``slot`` is None).

    The mutation half of ``POST /api/chat/mode``, after every caller check has
    passed. Shared with the crewmate profile write (``PUT /api/agents/{name}``),
    which applies the stored permission to the crewmate's live DM slot through
    this same path rather than a copy of it.
    """
    # The safety override (YOLO) is PROCESS-GLOBAL while an approval mode is
    # per-slot, so revoking it on behalf of a request that named ONE slot drops
    # every OTHER slot out of YOLO too. That is how a programmatic per-slot
    # `trust` — the call an automation makes when it creates a session — silently
    # ends an operator's live grant minutes after they enabled it.
    #
    # A slot-scoped `trust`/`trust_reads` therefore leaves the grant alone: it
    # asks for auto-approval on one slot and cannot be answered by withdrawing
    # authority elsewhere. Everything else still revokes, so `normal` remains the
    # off-switch at any scope and the dashboard picker (which always names its own
    # slot) keeps working.
    #
    # A grant DECLARED in owner-only config is exempt from the narrowing: it has
    # no TTL, and selecting another approval mode is the one action documented to
    # end it. Identity is the grant's source, never its permanence — an
    # `until_shutdown` ad-hoc pick is equally permanent and must stay protected.
    #
    # An app token never touches the global override: its ``normal`` on one
    # slot must not end the operator's YOLO grant on every other slot.
    #
    # Nor does a ``cron:`` caller. The mode rule above already holds it to a
    # slot-scoped ``trust`` or ``trust_reads``, and ``set_session_mode`` promises
    # that both leave the global override alone, declared or not: a scheduled run
    # asking for auto-approval on the one session it opened is never the
    # documented action that ends the operator's grant everywhere else.
    slot_scoped_trust = slot_key is not None and mode in _SLOT_SCOPED_TRUST_MODES
    if (
        not request_app
        and not cron_creator
        and mode != "yolo"
        and (not slot_scoped_trust or safety_override().is_declared)
    ):
        # deactivate() writes a SEL event, so it is offloaded exactly like the
        # sibling activate() — never run on the gateway loop. Safe after
        # the resolution above: every branch mutates the captured slot, never
        # re-indexing state._slots.
        await asyncio.to_thread(safety_override().deactivate, "dashboard")

    if mode == "yolo":
        result = await asyncio.to_thread(safety_override().activate, "dashboard")
        if not result.active:
            # Arming can be refused for two reasons and the client needs to tell
            # them apart: an ``approval_modes`` deny of ``yolo`` is a permanent
            # policy answer (403, same code the picker already understands),
            # while anything else is a transient activation failure (503).
            if not yolo_policy_permits():
                return _deny_approval_mode(
                    caller=audit_caller("dashboard:chat_mode"),
                    operation="mode_change:yolo",
                    mode="yolo",
                    resource=slot_key or "",
                )
            return web.json_response(
                {"ok": False, "error": "safety override activation refused"},
                status=503,
            )
        try:
            sel().log_api_access(
                caller=audit_caller("dashboard:mode"),
                operation="mode_change:yolo",
                outcome="enabled",
                resources=",".join(s.key for s in state._slots.values()),
            )
        except Exception:
            logger.warning("SEL audit failed for YOLO mode activation", exc_info=True)
    elif mode == "trust_reads":
        if slot is not None:
            _reads_key = effective_session_key(slot)
            _reads_sharing = [
                s for s in list(state._slots.values()) if effective_session_key(s) == _reads_key
            ]
            await _end_trust_scopes(_reads_sharing, audit_caller)
            _reads_live = _still_owning(state, _reads_sharing, _reads_key)
            for _sharing in _reads_live:
                _sharing._trust = False
                _sharing._trust_reads = True
                _sharing._trust_scope = ""
            if _reads_live:
                state.sessions.set_approval_policy(_reads_key, "")
        else:
            await _end_trust_scopes(list(state._slots.values()), audit_caller)
            for s in state._slots.values():
                s._trust = False
                s._trust_reads = True
                s._trust_scope = ""
                state.sessions.set_approval_policy(effective_session_key(s), "")
        try:
            sel().log_api_access(
                caller=audit_caller("dashboard:mode"),
                operation="mode_change:trust_reads",
                outcome="enabled",
                resources=slot_key or ",".join(s.key for s in state._slots.values()),
            )
        except Exception:
            logger.warning("SEL audit failed for trust_reads mode activation", exc_info=True)
    elif mode == "trust":
        mgr = getattr(state, "channel_manager", None)
        if slot is not None:
            # Every slot that SHARES the session, matching the revoke below. The
            # policy is per session while the flag is per slot, so setting one of
            # two sharing slots leaves them disagreeing about a session they both
            # address, and the propagation pass would then be decided by slot
            # iteration order rather than by what the operator asked for.
            _granted_key = effective_session_key(slot)
            for _sharing in state._slots.values():
                if effective_session_key(_sharing) == _granted_key:
                    _sharing._trust = True
                    _sharing._trust_card_scoped = False
            state.sessions.set_approval_policy(_granted_key, "auto")
            linked_ch = getattr(slot, "_slack_channel", None)
            if (
                not request_app
                and not cron_creator
                and mgr
                and linked_ch
                and linked_ch in mgr._channels
            ):
                mgr._channels[linked_ch].trusted = True
                mgr._channels[linked_ch]._save()
        else:
            for s in state._slots.values():
                s._trust = True
                s._trust_card_scoped = False
                state.sessions.set_approval_policy(effective_session_key(s), "auto")
            if mgr:
                for ch in mgr._channels.values():
                    ch.trusted = True
                    ch._save()
        _trusted_chs = (
            [cid for cid, ch in mgr._channels.items() if ch.trusted]
            if mgr and not request_app and not cron_creator
            else []
        )
        try:
            _res = slot_key or ",".join(s.key for s in state._slots.values())
            if _trusted_chs:
                _res += "|channels:" + ",".join(_trusted_chs)
            sel().log_api_access(
                caller=audit_caller("dashboard:mode"),
                operation="mode_change:trust",
                outcome="enabled",
                resources=_res,
            )
        except Exception:
            logger.warning("SEL audit failed for trust mode activation", exc_info=True)
    else:  # normal
        mgr = getattr(state, "channel_manager", None)
        if slot is not None:
            # Several slots can address ONE session (a rehydrated owner slot and
            # the alias its turns run under both resolve to the same effective
            # key), so revoking the selected slot alone leaves the others holding
            # a stale `_trust`, and the propagation below then rewrites the shared
            # session back to "auto" from it. The policy is per SESSION; the flag
            # is per slot; so the revoke has to clear every slot that shares it.
            _revoked_key = effective_session_key(slot)
            _revoked = [
                s for s in list(state._slots.values()) if effective_session_key(s) == _revoked_key
            ]
            await _end_trust_scopes(_revoked, audit_caller)
            _revoked_live = _still_owning(state, _revoked, _revoked_key)
            for _sharing in _revoked_live:
                _sharing._trust = False
                _sharing._trust_reads = False
                _sharing._trust_scope = ""
            if _revoked_live:
                state.sessions.set_approval_policy(_revoked_key, "")
            linked_ch = getattr(slot, "_slack_channel", None)
            if (
                slot in _revoked_live
                and not request_app
                and mgr
                and linked_ch
                and linked_ch in mgr._channels
            ):
                mgr._channels[linked_ch].trusted = False
                mgr._channels[linked_ch]._save()
        else:
            await _end_trust_scopes(list(state._slots.values()), audit_caller)
            for s in state._slots.values():
                s._trust = False
                s._trust_reads = False
                s._trust_scope = ""
                state.sessions.set_approval_policy(effective_session_key(s), "")
            if mgr:
                for ch in mgr._channels.values():
                    ch.trusted = False
                    ch._save()
        try:
            sel().log_api_access(
                caller=audit_caller("dashboard:mode"),
                operation="mode_change:normal",
                outcome="disabled",
                resources=slot_key or ",".join(s.key for s in state._slots.values()),
            )
        except Exception:
            logger.warning("SEL audit failed for normal mode activation", exc_info=True)

    # The sessions this slot created follow its new mode, capped at it.
    inherited_slots: set[str] = set()
    # Only from the slot the branch above wrote: one closed or replaced during its
    # awaits holds a posture this request never set.
    if (
        slot is not None
        and state._slots.get(slot.key) is slot
        and mode in ("trust", "trust_reads", "normal")
    ):
        inherited_slots = _apply_inherited_postures(
            state,
            slot,
            may_raise=not request_app and not cron_creator,
            audit_caller=audit_caller,
        )

    # If any slot has a pending approval and mode is trust/yolo, auto-approve it
    if mode in ("trust", "yolo"):
        # A slot-scoped ``trust`` grants auto-approval to ONE session only (the
        # target slot and any slot sharing its effective key). The pending-approval
        # sweep MUST honour that scope: sweeping every slot's pending prompt would
        # clear the approval card in unrelated chats — making them LOOK approved —
        # while their ``_trust`` flag stays False, so their very next tool call
        # prompts again. ``yolo`` is process-global and an unscoped ``trust`` (no
        # slot named = the documented all-slots request) still sweeps everything,
        # including background and channel approvals. Only a slot-scoped ``trust``
        # narrows.
        scoped = mode == "trust" and slot is not None
        _target_key = effective_session_key(slot) if slot is not None and scoped else None
        # The workers just raised to Trust (``inherited_slots``, by slot key) are in
        # scope too: the person trusted the conductor that dispatched them.
        for _slot in state._slots.values():
            if (
                scoped
                and effective_session_key(_slot) != _target_key
                and _slot.key not in inherited_slots
            ):
                continue
            _sweep_pending_approvals(state, _slot, mode, audit_caller)
        # Background (cron/subagent/taskrunner) and channel approvals are NOT
        # slot-scoped, so a slot-scoped ``trust`` must leave them pending — it
        # asked for auto-approval on one session and cannot answer for unrelated
        # background work. Only ``yolo`` and an all-slots ``trust`` sweep them.
        if not scoped:
            for aid in list(state._approval_futures):
                fut = state._approval_futures[aid]
                if not fut.done():
                    state.resolve_approval(aid, True)
                    try:
                        sel().log_api_access(
                            caller=audit_caller("dashboard:background"),
                            operation=f"tool_approval:bulk_{mode}",
                            outcome="approved",
                            resources=aid,
                        )
                    except Exception:
                        logger.warning("SEL audit failed for bulk approval %s", aid, exc_info=True)
            # Auto-approve pending channel approvals
            mgr = getattr(state, "channel_manager", None)
            if mgr:
                for ch in mgr._channels.values():
                    for agent in ch.members.values():
                        fut = agent._approval_future
                        if fut and not fut.done():
                            fut.set_result("approved")
                            try:
                                sel().log_api_access(
                                    caller=f"channel:{ch.id}:{agent.agent_name}",
                                    operation=f"tool_approval:bulk_{mode}",
                                    outcome="approved",
                                    resources=getattr(fut, "_approval_id", "unknown"),
                                )
                            except Exception:
                                logger.warning(
                                    "SEL audit failed for channel bulk approval",
                                    exc_info=True,
                                )

    # Propagate trust/yolo to session approval policies so subagents inherit.
    #
    # Keyed by ``effective_session_key`` — the SAME derivation every grant above
    # and the approval-card grants in ``api_chat_slot_approve`` use — because a
    # grant and its revoke must address one key. A channel-surfaced or cron-born
    # slot runs its turns under ``linked_session_key``, which is what
    # ``messaging.approval.TextApprovalDecider.trusted()`` reads, so keying by
    # the slot name writes a session nobody consults and leaves the live one
    # holding whatever it was last granted: an un-revokable auto-approve.
    # Safe as a per-slot write ONLY because both branches above apply their change
    # to every slot sharing a session, so two slots addressing one key always agree
    # by the time this runs and iteration order cannot pick a winner.
    for slot in state._slots.values():
        policy = "auto" if trust_carries(slot) or safety_override().is_active() else ""
        state.sessions.set_approval_policy(effective_session_key(slot), policy)

    state.push_slots_update()
    return web.json_response({"ok": True, "mode": mode})


def _sweep_pending_approvals(
    state: DashboardState, slot: _ChatSlot, mode: str, audit_caller: Callable[[str], str]
) -> None:
    """Approve *slot*'s pending tool prompts after a ``trust`` / ``yolo`` grant."""
    _elicitations = _pending_elicitation_ids(slot)
    for aid, fut in list(slot._approval_futures.items()):
        if aid in _elicitations:
            # A server's own question to the person: a trust tier is
            # not an answer to it, and the card stays open.
            continue
        if not fut.done():
            fut.set_result("approved")
            # Persist resolved state into the permission message. The
            # periodic flush skips non-dirty slots, so the mark must
            # flag the slot or the write can be lost on restart.
            if _mark_permission_resolved(slot.messages, aid, mode):
                slot._dirty = True
            # ``slot`` keys the frame for the slot-scoped WS gate.
            state.broadcast_ws(
                "approval_resolved",
                {"id": aid, "approved": True, "slot": slot.key},
            )
            try:
                sel().log_api_access(
                    caller=audit_caller(f"dashboard:{slot.key}"),
                    operation=f"tool_approval:bulk_{mode}",
                    outcome="approved",
                    resources=aid,
                )
            except Exception:
                logger.warning("SEL audit failed for bulk approval %s", aid, exc_info=True)


def _apply_inherited_postures(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    may_raise: bool,
    audit_caller: Callable[[str], str],
) -> set[str]:
    """Hand *slot*'s new mode to the sessions it created.

    *slot* just had its own mode set, which is an explicit choice, so it stops
    following its own creator. Its inheriting descendants then take its carried
    posture (``approval_inheritance``): never more than it carries, never across a
    worker whose own mode a person set, and a raise only along a gateway-authored
    creation edge, only to a descendant alone on its session, and only for a caller
    allowed to raise (*may_raise*). Written to every slot sharing a descendant's
    session, as the branches above write the target, so the per-slot policy sync
    below cannot pick a winner. Returns the slot keys raised to Trust.

    Only a person's own change (*may_raise*) marks *slot* explicit: a mode an app
    or cron set on it must still give way to its creator's next lowering.
    """
    from kiro_crew.dashboard.approval_inheritance import plan_descendant_postures

    target_key = effective_session_key(slot)
    if may_raise:
        for _sharing in state._slots.values():
            if effective_session_key(_sharing) == target_key:
                _sharing._posture_inherited = False
    plan = plan_descendant_postures(list(state._slots.values()), slot, may_raise=may_raise)
    trusted: set[str] = set()
    for change in plan:
        key = effective_session_key(change.slot)
        targets = [s for s in state._slots.values() if effective_session_key(s) == key]
        for _target in targets:
            _target._trust = change.posture == "trust"
            _target._trust_card_scoped = False
            _target._trust_reads = change.posture == "trust_reads"
            _target._posture_inherited = True
        if change.posture == "trust":
            trusted.add(change.slot.key)
    if plan:
        try:
            sel().log_api_access(
                caller=audit_caller("dashboard:mode"),
                operation="mode_change:inherited",
                outcome="changed",
                resources=",".join(f"{c.slot.key}={c.posture}" for c in plan) + f"|from:{slot.key}",
            )
        except Exception:
            logger.warning("SEL audit failed for inherited mode change", exc_info=True)
    return trusted


def _get_pattern_from_pending(slot: _ChatSlot, request_id: str, field: str) -> str:
    """Extract a pattern field from the permission message matching request_id."""
    if not request_id:
        return ""
    for msg in reversed(slot.messages):
        if msg.get("role") == "permission" and msg.get("cls"):
            try:
                meta = json.loads(msg["cls"])
                if not isinstance(meta, dict):
                    continue
                if meta.get("request_id") == request_id:
                    return meta.get(field, "")
            except (json.JSONDecodeError, TypeError):
                continue
    return ""


def _deny_approval_mode(
    *,
    caller: str,
    operation: str,
    mode: str,
    resource: str = "",
) -> web.Response:
    """Refuse a policy-denied approval mode, audited, WITHOUT any mutation.

    One helper for every surface that can arm an auto-approve mode, so a refusal
    always lands in the security event log. A governance refusal that leaves no
    trace is indistinguishable from the request never having been made, which is
    exactly the record an operator needs after an attempted escalation. The audit
    is best-effort: an SEL write failure must not turn a refusal into a grant.
    """
    try:
        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="approval_mode_denied_by_policy",
            resources=resource or mode,
            error="mode_disabled_by_policy",
        )
    except Exception:
        logger.warning("SEL audit failed for policy-refused approval mode %s", mode, exc_info=True)
    return web.json_response(
        {
            "ok": False,
            "error": f"approval mode {mode!r} is disabled by your organization's policy",
            "code": "mode_disabled_by_policy",
            "mode": mode,
        },
        status=403,
    )


def _grant_deadline(ttl: int | None) -> GrantDeadline | None:
    """Return a timed grant's deadline from now, ``None`` for a chat-long grant."""
    if ttl is None:
        return None
    return grant_deadline(ttl, wall=time.time(), mono=time.monotonic())


def _deny_trust_pattern(name: str, request_id: str, action: str, code: str) -> web.Response:
    """Refuse and audit a command-scoped trust grant without resolving it."""
    try:
        sel().log_api_access(
            caller=f"dashboard:{name}",
            operation=f"tool_approval:{action}",
            outcome="trust_pattern_denied",
            resources=request_id,
            error=code,
        )
    except Exception:
        logger.warning("SEL audit failed for refused trust grant %s", request_id, exc_info=True)
    errors = {
        "pattern_required": "pattern required for command-scoped trust",
        "pattern_underivable": "the pending tool has no grantable command scope",
        "approval_superseded": "pattern does not match the pending command",
        "approval_not_slot_owned": "command-scoped trust requires a live slot approval",
        "ttl_unsupported": "only a command or command-family grant can be timed",
        "ttl_invalid": "ttl_seconds must be a whole number of seconds from 1 to 86400",
    }
    return web.json_response({"error": errors[code], "code": code}, status=400)


def _elicitation_not_an_approval() -> web.Response:
    """Not a tool approval: the question is the MCP server's and only the
    dashboard owner answers it, through the elicitation route."""
    return web.json_response(
        {
            "error": "this request is an MCP elicitation; answer it through "
            "/api/chat/slots/{slot}/elicitation",
            "code": "elicitation_not_an_approval",
        },
        status=409,
    )


async def api_chat_slot_approve(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/approve — resolve a pending tool approval."""
    state: DashboardState = request.app["state"]
    denied = await deny_session_approval_caller(request, "chat_slot_approve")
    if denied is not None:
        return denied
    request_app = str(request.get("app") or "")
    # A Crew Mode conductor's approval card decides a WORKER's request through this
    # same route (``crew_workers.api_crew_worker_approve``): that route gates the
    # owner, refuses app tokens and other roots' workers, reads and narrows the
    # body, and hands over ``(worker slot key, body)``. Nothing else sets it.
    delegated = request.get("kirocrew_crew_worker_approve")
    name = delegated[0] if delegated else request.match_info["slot"]

    def audit_caller() -> str:
        return f"app:{request_app}" if request_app else f"dashboard:{name}"

    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    body: dict[str, Any] | None
    body_err: web.Response | None
    if delegated:
        body, body_err = dict(delegated[1]), None
    else:
        body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    action = body.get("action", "rejected")
    original_action = action
    request_id = body.get("request_id", "")
    # ``response_id`` names this one click, so a retry of it replays the same
    # outcome and a different collaborator's late click gets the winner back.
    # Imported here: owner functions run on the handlers module's globals.
    from kiro_crew.shared_crew import decisions as shared_decisions

    response_id, response_id_ok = shared_decisions.read_response_id(body)
    if not response_id_ok:
        return web.json_response(
            {"error": "invalid response_id", "code": "invalid_response_id"}, status=400
        )
    if request_app and original_action == "yolo":
        return _deny_app_yolo(request_app, "tool_approval:yolo")
    # A FRESH grant read: the body upload above can take long enough for the
    # flag to have been removed from the manifest since the checkpoint read it.
    denied = await deny_app_session_control(
        request, request_app, slot, name, "chat_slot_approve", fresh=True
    )
    if denied is not None:
        return denied
    if isinstance(request_id, str) and request_id in _pending_elicitation_ids(slot):
        return _elicitation_not_an_approval()
    strict_native = "origin" in body
    if not strict_native and not request_app and not bare_approval_decide_allowed(request):
        # A decide by request id alone, from a caller that is not the dashboard
        # owner's browser session: refuse it rather than resolve whichever
        # request now holds the id.
        return _approval_target_required()
    if strict_native:
        request_mid = body.get("request_mid")
        if (
            body["origin"] != "native"
            or not isinstance(request_id, str)
            or not request_id
            or not isinstance(request_mid, str)
            or not request_mid
            or action not in ("approved", "rejected", "rejected_once")
        ):
            return web.json_response(
                {"error": "invalid approval target", "code": "invalid_approval_target"}, status=400
            )
        # This caller displayed a native request from this exact slot. A stale
        # card must not select a same-id coordinator or another slot's future.
        native_future = slot._approval_futures.get(request_id)
        if (
            not native_future
            or native_future.done()
            or slot.approval_instance(request_id) != request_mid
        ):
            settled = shared_decisions.settled_reply(
                state,
                slot.key,
                request_id,
                response_id,
                shared_decisions.request_principal(request),
            )
            if settled is not None:
                return shared_decisions.respond(settled)
            return web.json_response(
                {"error": "no pending approval", "code": "approval_not_pending"}, status=404
            )
    # A timed grant names its lifetime in ``ttl_seconds``. Only the two
    # command-scoped tiers carry one, and an unreadable lifetime refuses the
    # whole decision BEFORE any side effect: it never falls back to a grant
    # that lasts for the chat, and never to an approval with no grant.
    grant_ttl: int | None = None
    if "ttl_seconds" in body:
        if original_action not in ("trust_command", "trust_base"):
            return _deny_trust_pattern(name, request_id, original_action, "ttl_unsupported")
        grant_ttl = parse_grant_ttl(body["ttl_seconds"])
        if grant_ttl is None:
            return _deny_trust_pattern(name, request_id, original_action, "ttl_invalid")
    # Locate the slot that OWNS the pending approval future. It is usually the
    # addressed slot, but under session-sharing or a rehydrated/replaced slot the
    # future can live on a different slot object under a different key. All
    # slot-scoped side-effects (trust flags, trusted patterns, approval policy)
    # and the resolved outcome MUST land on the OWNER slot — the one whose
    # session loop consumes the future and gates subsequent tools — or the trust
    # opt-in silently fails on the running session while the UI reports success.
    owner = slot
    if request_id:
        fut = slot._approval_futures.get(request_id)
        if (not fut or fut.done()) and not strict_native:
            # The future can live on a DIFFERENT slot object only under
            # session-sharing / rehydration — i.e. a slot that resolves to the
            # SAME session identity as the addressed one. ACP request_ids are
            # connection-scoped and can collide across unrelated sessions, so a
            # bare id-match scan could approve (and, for trust, auto-approve) an
            # unrelated slot's pending tool. Guard the scan on session identity:
            # only a candidate whose effective session key equals the addressed
            # slot's is a legitimate owner.
            want_session = effective_session_key(slot)
            for s in state._slots.values():
                cand = s._approval_futures.get(request_id)
                if not cand or cand.done():
                    continue
                cand_session = effective_session_key(s)
                if cand_session != want_session:
                    continue
                owner, fut = s, cand
                break
    else:
        # The implicit pick never lands on an elicitation: a bare approve with
        # no id is a decision about the agent's tool, and the only open
        # question being the MCP server's does not make it that.
        elicitations = _pending_elicitation_ids(slot)
        pending = [
            (k, f)
            for k, f in slot._approval_futures.items()
            if not f.done() and k not in elicitations
        ]
        if len(pending) == 1:
            request_id, fut = pending[0]
        else:
            fut = None
    if fut is not None and not fut.done() and request_id in _pending_elicitation_ids(owner):
        # Checked on the RESOLVED (owner, request_id), not only the addressed
        # slot: under session-sharing the future the scan found lives on
        # another slot, and the early guard above never read that slot's rows.
        return _elicitation_not_an_approval()
    if request_app and owner is not slot:
        denied = await deny_app_session_control(
            request, request_app, owner, owner.key, "chat_slot_approve"
        )
        if denied is not None:
            return denied
    if request_app and (not fut or fut.done()):
        # No slot-level future means the id names (at most) a STATE-level
        # approval. Those are raised only by background sources -- cron,
        # autonudge, subagent, taskrunner -- and merely parked in the user's
        # tab, so the grant, which reaches the user's own session only, never
        # resolves one. The user's own tool prompts live on the slot future
        # handled above. Same 404 as any other out-of-scope target.
        return slot_not_found()
    # A state-level approval carries only a boolean decision and has no owning
    # slot, canonical command card, or scoped-pattern store.  Do not let a
    # durable-trust action fall through to ``resolve_state_approval`` as ``True``:
    # that would approve the tool after skipping every scope check.  Truly
    # missing IDs retain the 404 from the common fallback below; this explicit
    # denial covers a live state owner.
    if original_action in ("trust", "trust_command", "trust_base") and (not fut or fut.done()):
        state_fut = state._approval_futures.get(request_id) if request_id else None
        if state_fut and not state_fut.done():
            return _deny_trust_pattern(name, request_id, original_action, "approval_not_slot_owned")
    # Trust: auto-approve remaining tools for this slot. The approval policy MUST
    # be keyed by the OWNER's EFFECTIVE session key — a linked cron/workflow or
    # channel-surfaced slot runs under ``linked_session_key``, not
    # ``dashboard:{key}``, so writing the raw slot key would leave the running
    # session on its old policy and the trust decision would silently not take.
    # ``effective_session_key`` is the one derivation shared with ``api_chat_mode``'s
    # grants AND revokes, so an off-switch always addresses the key a grant wrote.
    if action == "trust":
        # A pending-card trust decision may widen the slot only when this exact
        # live card carries the server's durable-grant proof.  This check MUST
        # precede every side effect: a forged/expired/state-owned request id
        # must not leave _trust or the session policy enabled before the common
        # resolver eventually returns 400/404.  Explicit session-mode changes
        # use api_chat_mode and remain independent of this card-bound proof.
        grantable = _get_pattern_from_pending(owner, request_id, "trust_grantable")
        if not fut or fut.done():
            # No state-level fallback for a trust grant.  The early state-owner
            # guard above returns 400; a genuinely missing/expired id keeps the
            # endpoint's existing 404 below without mutating anything.
            action = "trust"
        elif grantable != "1":
            return _deny_trust_pattern(name, request_id, original_action, "pattern_underivable")
        else:
            owner._trust = True
            # A person chose this slot's mode: its creator's changes stop reaching it.
            owner._posture_inherited = False
            # A conductor card's "Trust this worker" trusts THAT worker only; a
            # trust pressed in the slot's own pane keeps the usual inheritance.
            owner._trust_card_scoped = bool(delegated)
            state.sessions.set_approval_policy(
                effective_session_key(owner), "auto" if trust_carries(owner) else ""
            )
            action = "approved"
    # Trust-reads: auto-approve read-only bash commands for this slot
    # Defer setting _trust_reads until after the approval future is consumed
    # to prevent the frontend from seeing trust_reads=true while still pending.
    elif action == "trust_reads":
        action = "approved_trust_reads"
    # Trust-command: bind the grant to the SERVER-DERIVED pending command.  The
    # client pattern is only proof that the card the user clicked describes the
    # same command; it never supplies authority.
    elif action == "trust_command":
        if fut and not fut.done():
            pattern = body.get("pattern", "")
            expected = _get_pattern_from_pending(owner, request_id, "full_command")
            trust_key = _get_pattern_from_pending(owner, request_id, "trust_command_key")
            grantable = _get_pattern_from_pending(owner, request_id, "trust_command_grantable")
            if not isinstance(pattern, str) or not pattern:
                return _deny_trust_pattern(name, request_id, original_action, "pattern_required")
            if grantable != "1" or not expected or not trust_key:
                return _deny_trust_pattern(name, request_id, original_action, "pattern_underivable")
            if pattern != expected:
                return _deny_trust_pattern(name, request_id, original_action, "approval_superseded")
            # ``_trusted_patterns`` is the existing fnmatch store.  Escape every
            # metacharacter so an exact grant for ``rm *.tmp`` cannot authorize
            # ``rm secret.tmp``.
            record_trust_grant(
                owner._trusted_patterns,
                owner._trusted_pattern_deadlines,
                (exact_trust_pattern(trust_key),),
                _grant_deadline(grant_ttl),
            )
        action = "approved"
    # Trust-base: derive bases from the same canonical pending command, never
    # from the client pattern or model-authored title.
    elif action == "trust_base":
        if fut and not fut.done():
            pattern = body.get("pattern", "")
            base = _get_pattern_from_pending(owner, request_id, "base_command")
            grantable = _get_pattern_from_pending(owner, request_id, "trust_base_grantable")
            if not isinstance(pattern, str) or not pattern:
                return _deny_trust_pattern(name, request_id, original_action, "pattern_required")
            if grantable != "1" or not base:
                return _deny_trust_pattern(name, request_id, original_action, "pattern_underivable")
            if pattern != base_consent_pattern(base):
                return _deny_trust_pattern(name, request_id, original_action, "approval_superseded")
            record_trust_grant(
                owner._trusted_patterns,
                owner._trusted_pattern_deadlines,
                base_trust_patterns(base),
                _grant_deadline(grant_ttl),
            )
        action = "approved"
    # YOLO: auto-approve all tools globally (all slots)
    elif action == "yolo":
        result = await asyncio.to_thread(safety_override().activate, "dashboard")
        if not result.active:
            # Same two-reason split as ``api_chat_mode``: an ``approval_modes``
            # deny of ``yolo`` is a permanent policy answer the client can render
            # (403 + the code the picker already understands), while anything else
            # is a transient activation failure worth retrying (503).
            if not yolo_policy_permits():
                return _deny_approval_mode(
                    caller=audit_caller(),
                    operation="tool_approval:yolo",
                    mode="yolo",
                    resource=request_id,
                )
            return web.json_response(
                {"ok": False, "error": "safety override activation refused"},
                status=503,
            )
        for s in state._slots.values():
            # Same effective-session-key rule as the single-slot trust above: a
            # linked cron/workflow or channel-surfaced slot runs under its
            # linked_session_key.
            state.sessions.set_approval_policy(effective_session_key(s), "auto")
        # Reconcile against a policy deny that landed while this was writing.
        #
        # This write is the grant's inherited half -- ``admission.parent_trusted``
        # reads the slot's approval policy directly rather than any flag in
        # ``safety_override`` -- and it happens OUTSIDE the lock the revocation takes.
        # So a denying ceiling installed after the arm returned can revoke the grant
        # and run its ``_on_expired`` cleanup, and this loop then puts the inherited
        # trust straight back with nothing left to clear it: a subagent spawned under
        # it is auto-approved, and is not un-spawned by the next event either.
        #
        # The two halves are complete together: a write that lands BEFORE the
        # cleanup is cleared by the cleanup, and one that lands after -- or
        # interleaved with it -- is cleared here. Standing trust is preserved on the
        # same rule ``_on_override_expired`` uses, since a Trust press is a separate,
        # longer-lived decision that no yolo deny expires.
        if not yolo_policy_permits():
            for s in state._slots.values():
                if not (trust_carries(s) or s._trust_reads):
                    state.sessions.set_approval_policy(effective_session_key(s), "")
            state.push_slots_update()
        action = "approved"
    resolved = (
        action if action in ("approved", "approved_trust_reads", "rejected_once") else "rejected"
    )
    approved = resolved in ("approved", "approved_trust_reads")
    if not fut or fut.done():
        # Distinguish ambiguous (multiple pending) from truly empty
        if not request_id and slot._approval_futures:
            pending_ids = [k for k, f in slot._approval_futures.items() if not f.done()]
            if len(pending_ids) > 1:
                return web.json_response(
                    {
                        "error": "multiple approvals pending, specify request_id",
                        "pending": pending_ids,
                    },
                    status=400,
                )
        # No slot owns this future — fall back to the STATE-LEVEL-ONLY resolver so
        # a background approval (cron/subagent/gateway) is still dismissed instead
        # of 404-ing. MUST be resolve_state_approval, NOT resolve_approval: the
        # latter re-scans every slot's futures by bare id-match, which would let a
        # request-id collision resolve an unrelated slot's pending tool — exactly
        # the cross-slot approval the session-identity owner scan above prevents.
        # State-level futures have no per-slot trust semantics, so the bool
        # coercion loses nothing.
        if not strict_native and request_id:
            state_reply = shared_decisions.resolve_state_approval(
                state, request, request_id, approved, resolved, response_id
            )
            if state_reply is not None:
                return shared_decisions.respond(state_reply)
        settled = shared_decisions.settled_reply(
            state,
            slot.key,
            request_id,
            response_id,
            shared_decisions.request_principal(request),
        )
        if settled is not None:
            return shared_decisions.respond(settled)
        return web.json_response({"error": "no pending approval"}, status=404)
    if request_id:
        resolution = shared_decisions.resolve_slot_approval(
            state, request, owner, request_id, fut, resolved, response_id
        )
        if not resolution.applied:
            lost = shared_decisions.lost_race_reply(resolution, response_id)
            if lost is not None:
                return shared_decisions.respond(lost)
            return web.json_response(
                {"error": "no pending approval", "code": "approval_not_pending"}, status=404
            )
    else:
        fut.set_result(resolved)
        resolution = None
    # Persist resolved state into the permission message so it survives tab
    # switches — on the owner slot, whose messages hold the permission card.
    # Flagging the slot dirty is required for it to survive a RESTART too: the
    # periodic flush skips non-dirty slots.
    if request_id:
        if _mark_permission_resolved(
            (
                [message for message in owner.messages if row_mid(message) == request_mid]
                if strict_native
                else owner.messages
            ),
            request_id,
            original_action if original_action in ("trust", "trust_reads") else resolved,
        ):
            owner._dirty = True
    # Broadcast first to ensure frontend is unblocked
    if request_id:
        state.broadcast_ws(
            "approval_resolved",
            {
                "id": request_id,
                "approved": approved,
                # Keys the frame for the slot-scoped WS gate (see
                # ws_event_scope._SLOT_SCOPED_EVENTS).
                "slot": owner.key,
            },
        )
    if resolution is not None and resolution.decision is not None:
        shared_decisions.broadcast_decision(state, resolution.decision)
    state.push_slots_update()
    # SEL audit (best-effort — must not block the UI-unblocking path above)
    try:
        sel().log_api_access(
            caller=audit_caller(),
            operation=f"tool_approval:{original_action}",
            outcome=resolved,
            resources=request_id,
        )
    except Exception:
        logger.warning("SEL audit failed for approval %s", request_id, exc_info=True)
    if resolution is not None and resolution.decision is not None:
        return web.json_response({"ok": True, "decision": resolution.decision.to_public_dict()})
    return web.json_response({"ok": True})
