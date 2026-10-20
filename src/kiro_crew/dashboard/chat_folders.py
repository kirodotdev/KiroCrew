"""Folder management: the chat folder routes callers import from here.

This module is the facade every caller imports from, and it keeps the
session-sidebar slot routes: a slot's folder, pin, mutes and mode, and the
pinned session order. The folder tree is composed from owner modules, each named
after the work that belongs in it: ``chat_folders_icons`` (icons and colours, and
the LLM emoji generator the artifact library shares), ``chat_folders_paths`` (a
folder's project and steering directories), ``chat_folders_records`` (folder
records and who may write them) and ``chat_folders_routes`` (the
``/api/chat/folders`` routes)."""

from __future__ import annotations

import asyncio
import importlib as _importlib
import logging
import os
import sys as _sys
import time
import unicodedata
import uuid
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import pinned_fs
from kiro_crew.dashboard import pinned_session_order
from kiro_crew.dashboard.chat_folders_records import (
    _agent_in_channel_reachable_session,
    _audit_origin,
    _unhide_folder,
    member_slot_write_refused,
    note_folder_filed,
)
from kiro_crew.dashboard.chat_manual_order import load_manual_order_after_listen
from kiro_crew.dashboard.chat_persistence import _coerce_requested_mode, save_slot_off_loop
from kiro_crew.dashboard.chat_tags import tags_write_lock, validate_folder_tag_ids
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.create_rate_limit import FOLDER_CREATE, allow_create
from kiro_crew.dashboard.folder_ownership import (  # re-exported: callers import them here
    _folder_owner_app,
    _is_descendant,
    _subtree_holds_foreign_folder,
)
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.slot_ownership import (
    audit_app_slot_denial,
    deny_app_slot_access,
    slot_not_found,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.token_auth import (
    KNOWN_INTERNAL_CALLERS,
    MEMBER_CHAT_PRINCIPAL_KEY,
    app_owns_transcript,
    effective_request_app,
    folder_principal,
    refuse_unattributable_caller,
    request_origin,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.folder_steering import crosses_memory_silo, memory_silo_fence
from kiro_crew.hooks import is_unc_shape, unc_probe_allowed, validate_file_path
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.sandbox import voice_runtime_workspace_conflict
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


# The authorization-identity helper is homed in ``token_auth_callers`` beside the rule it
# wraps; this module keeps its historical private name because
# ``chat_folder_scaffold`` imports it from here and
# ``test_internal_secret_app_identity_3690`` addresses it as
# ``chat_folders._effective_request_app``.
_effective_request_app = effective_request_app


# Per-STATE metadata-write transaction lock for the slot metadata PATCH
# endpoints (folder / pin / mode), same rationale as the autocompact txn lock:
# with awaits inside a mutate/save/rollback span, a second concurrent request
# would otherwise capture the first one's value as its rollback snapshot, and
# value-based rollback cannot tell "my write survived" from "someone else
# wrote the same value" — so a refused request could erase an equal,
# acknowledged concurrent commit. Under the lock exactly one request is inside
# the span, so a rollback can only undo its own write.
#
# Keyed by the STATE, not the transcript: a transcript-keyed lock changes
# identity when the slot is rebound mid-request (review-caught), so a second
# request entering after the rebind would acquire a DIFFERENT lock and the
# spans would interleave anyway. The state key is stable across rebinds and
# covers alias slots resolving onto one file too — the same identity
# chat_tags._TAGS_WRITE_LOCKS uses for the tags writers. These are rare,
# human-driven sidebar operations, so one lock per state does not contend.
_SLOT_META_TXN_LOCKS: "weakref.WeakKeyDictionary[Any, LoopBoundLock]" = weakref.WeakKeyDictionary()


def _slot_meta_txn_lock(state: Any) -> LoopBoundLock:
    lock = _SLOT_META_TXN_LOCKS.get(state)
    if lock is None:
        lock = LoopBoundLock()
        _SLOT_META_TXN_LOCKS[state] = lock
    return lock


async def _subagent_work_pending(subagents: Any, parent_session_key: str) -> bool:
    """Whether *parent_session_key* still has sub-agents running or QUEUED.

    Asked through ``SubagentManager.has_pending_work_for_async``, whose store
    ``count_pending`` runs on the task store's writer thread; the synchronous
    entry takes the SQLite connection on the dashboard's own event loop. A
    manager double without the async sibling is asked synchronously -- the
    pre-queue behaviour those doubles model, and the same probe
    ``handlers.messaging._spawn_on_loop`` makes for ``spawn_async``.
    """
    import inspect

    entry = getattr(subagents, "has_pending_work_for_async", None)
    if inspect.iscoroutinefunction(entry):
        return bool(await entry(parent_session_key))
    return bool(subagents.has_pending_work_for(parent_session_key))


async def api_chat_slot_folder(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/folder — assign slot to a folder."""

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    # App ownership (App Kit §5.2) — the same deny-by-default rule
    # api_chat_slot_mode applies, and it matters HERE because filing is a write
    # to a session's own state: refiling moves a foreign session in the sidebar
    # and re-injects its folder breadcrumb on that session's next turn, so an
    # app holding this route could reach a session it does not own. Reported as
    # the same 404 for both reasons on purpose — a distinct code per reason
    # would turn it into an existence oracle for slots the caller cannot see.
    # A caller whose tab closed mid-call is refused first: its derived app
    # would be "" and read as the person (the same guard the tree writes apply).
    if (refusal := refuse_unattributable_caller(state, request, "chat.slot_folder")) is not None:
        return refusal
    # Member ownership, beside the app fence and for the same reason: a member
    # carries no app claim, so the app guard below is a no-op for it and would
    # let it file ANY session. This refuses a member filing a session that is
    # not its own or created; a non-member caller makes it a no-op.
    if (refusal := member_slot_write_refused(state, request, slot, "chat.slot_folder")) is not None:
        return refusal
    request_app = _effective_request_app(state, request)
    if (
        denied := deny_app_slot_access(request_app, slot, slot.key, "chat.slot_folder")
    ) is not None:
        return denied
    # Capture the transcript key the ownership decision above just covered,
    # BEFORE the body-parse await: ``linked_session_key`` is rebound on
    # already-live slots with no ``running`` gate (cron completions, workflow
    # injections), so a slow caller can be authorized against its own session
    # and land on somebody else's conversation. The re-check below and the
    # save's expected_history_key pin together keep this request's write on
    # the transcript it was authorized against.
    authorized_history_key = slot_history_key(slot)
    # ``_app`` says who owns the slot OBJECT; the write persists into the
    # TRANSCRIPT that key names, which a linked slot can point at another
    # owner's session. Both must resolve to the caller's app (same rule as
    # ``chat_tags.api_chat_slot_tags``), same indistinguishable 404.
    if not app_owns_transcript(state._slots, request_app, authorized_history_key):
        audit_app_slot_denial(
            request_app, "chat.slot_folder", slot.key, "app does not own this slot's transcript"
        )
        return slot_not_found()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    folder_id = str(body.get("folder_id") or "")
    if folder_id and not any(f["id"] == folder_id for f in state._folders):
        return web.json_response({"error": "folder not found"}, status=400)
    # Optional generation token. The identity re-check below covers THIS
    # request's own awaits, but a caller that resolved the slot in an earlier
    # request (``chat_folder_file_self`` reads ``/api/chat/slots`` first) has a
    # gap this handler cannot see: its tab can close and the same slot key be
    # recreated for a different conversation before its PATCH arrives, and the
    # recreated slot carries the same ``dashboard:<key>`` transcript key, so the
    # history pin alone cannot tell them apart. ``created_at`` is minted once
    # per slot object and persisted, so echoing it back is the caller's proof
    # that the slot it is filing is the one it resolved.
    expected_created = str(body.get("expected_created") or "")
    # Filing into a hidden folder unhides it, which changes the folder tree the
    # full slots frame carries; only a placement that left the tree alone can
    # travel as a one-row patch.
    folders_generation_before = state.folders_generation()
    # Serialize the whole re-check/mutate/persist/rollback span under the
    # state-wide metadata txn lock (rebind-stable; see _slot_meta_txn_lock):
    # with awaits inside the span, a second concurrent request would capture
    # this one's value as its rollback snapshot, and value-based rollback
    # cannot tell "my write survived" from "someone else wrote the same
    # value". Under the lock a rollback can only undo its own write; the
    # compare-and-set below stays as defense for the non-endpoint writers
    # (the folder-delete unfile loop) that do not take this lock.
    async with _slot_meta_txn_lock(state):
        # Re-authorize after the awaits above (body parse, lock acquisition):
        # same slot OBJECT still registered under the name, routing still on
        # the transcript captured before the first await. No await between
        # this check and the mutation below; the _unhide_folder and persist
        # awaits after it are covered by the save's pin. The generation token
        # is checked in the same breath: a mismatch means the caller resolved a
        # slot that has since been replaced under its key.
        if (
            state._slots.get(name) is not slot
            or slot_history_key(slot) != authorized_history_key
            or (expected_created and slot.created_at != expected_created)
            or not app_owns_transcript(state._slots, request_app, authorized_history_key)
        ):
            source, caller = _audit_origin(request)
            sel().log_api_access(
                caller=caller,
                operation="chat.slot_folder",
                outcome="denied",
                source=source,
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
        previous = slot.folder_id
        previous_changed = slot._folder_changed
        if folder_id != slot.folder_id:
            slot._folder_changed = True  # re-inject [FOLDER] breadcrumb on next turn
        slot.folder_id = folder_id
        # The check above reads the store unlocked, so a delete can land between it
        # and here. _unhide_folder re-checks existence under the store lock, which
        # is the only place the answer cannot go stale — reject rather than persist a
        # placement into a folder that no longer exists.
        if not await _unhide_folder(
            state,
            folder_id,
            # The person filing a session INTO this folder claims it; a re-file
            # into the folder it already sits in changes nothing.
            claim_for_person=folder_id != previous and _audit_origin(request)[0] == "dashboard",
        ):
            slot.folder_id = previous
            slot._folder_changed = previous_changed
            return web.json_response(
                {"error": "folder not found", "code": "folder_not_found"}, status=400
            )
        if not await save_slot_off_loop(
            state, slot, force=True, expected_history_key=authorized_history_key
        ):
            # Refused without writing: the session was permanently deleted or
            # rebound mid-persist. Roll back the live fields — but only while
            # they still hold THIS request's value: a non-endpoint writer may
            # have committed a newer placement that an unconditional restore
            # would erase (the same guard _restore_unfiled applies).
            if slot.folder_id == folder_id:
                slot.folder_id = previous
                slot._folder_changed = previous_changed
            # The UNPINNED periodic flush may have persisted the provisional
            # value while this save awaited (review-caught): mark dirty so the
            # next flush reconverges the durable record to the live state.
            slot._dirty = True
            source, caller = _audit_origin(request)
            sel().log_api_access(
                caller=caller,
                operation="chat.slot_folder",
                outcome="denied",
                source=source,
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
        # The placement is durable from here, so it is safe to claim the row is
        # occupied. Inside the lock, in the same span as the save it attests to:
        # recorded outside it, a refused save could still leave the claim behind.
        note_folder_filed(state, folder_id)
    if state.folders_generation() == folders_generation_before:
        state.push_slot_patch(slot.key, ("folder_id",))
    else:
        state.push_slots_update()
    source, caller = _audit_origin(request)
    sel().log_api_access(
        caller=caller,
        operation="chat.slot_folder",
        outcome="allowed",
        source=source,
        resources=name,
    )
    return web.json_response({"ok": True, "folder_id": slot.folder_id})


async def api_chat_slot_pin(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/pin — toggle pinned state."""

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    # The same three ownership fences api_chat_slot_folder applies, in the same
    # order and with the same indistinguishable 404: pinning is a write to a
    # session's own sidebar state, and the ``chat_session_pin`` MCP tool reaches
    # this route on behalf of app agents and crew members, not only the person.
    if (refusal := refuse_unattributable_caller(state, request, "chat.slot_pin")) is not None:
        return refusal
    if (refusal := member_slot_write_refused(state, request, slot, "chat.slot_pin")) is not None:
        return refusal
    request_app = _effective_request_app(state, request)
    if (denied := deny_app_slot_access(request_app, slot, slot.key, "chat.slot_pin")) is not None:
        return denied
    # Capture the transcript key the lookup above just covered, BEFORE the
    # body-parse await — the same rebind window api_chat_slot_folder
    # documents. The re-check below and the save's expected_history_key pin
    # together keep this request's write on the transcript it was authorized
    # against.
    authorized_history_key = slot_history_key(slot)
    if not app_owns_transcript(state._slots, request_app, authorized_history_key):
        audit_app_slot_denial(
            request_app, "chat.slot_pin", slot.key, "app does not own this slot's transcript"
        )
        return slot_not_found()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # Optional generation token, the same one api_chat_slot_folder reads: the
    # ``chat_session_pin`` MCP tool resolves the slot from an earlier
    # ``/api/chat/slots`` read, and the slot key can be recreated for a
    # different conversation before this PATCH arrives. Echoing the resolved
    # ``created`` back proves the slot being pinned is the one resolved.
    expected_created = str(body.get("expected_created") or "")
    # Serialize the re-check/mutate/persist/rollback span under the
    # state-wide metadata txn lock — same rationale as api_chat_slot_folder.
    async with _slot_meta_txn_lock(state):
        # Re-authorize after the awaits above (body parse, lock acquisition):
        # same slot OBJECT still registered under the name, routing still on
        # the transcript captured before the first await, and the slot
        # generation the caller resolved. No await between this check and the
        # save dispatch.
        if (
            state._slots.get(name) is not slot
            or slot_history_key(slot) != authorized_history_key
            or (expected_created and slot.created_at != expected_created)
            or not app_owns_transcript(state._slots, request_app, authorized_history_key)
        ):
            source, caller = _audit_origin(request)
            sel().log_api_access(
                caller=caller,
                operation="chat.slot_pin",
                outcome="denied",
                source=source,
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
        prior_pinned = slot.pinned
        prior_rank = state.pin_rank(slot)
        new_pinned = body.get("pinned", False)
        # Do not use Python truthiness for API booleans: JSON strings such as
        # "false" are non-empty and therefore truthy.  The sibling metadata
        # fields validate their types before mutating; pin must do the same.
        if not isinstance(new_pinned, bool):
            return web.json_response(
                {"error": "pinned must be a boolean", "code": "pinned_not_bool"}, status=400
            )
        slot.pinned = new_pinned
        # Decided here, under the lock and after the generation and ownership
        # re-checks, so a caller told "no change" is told the truth about the
        # slot it resolved rather than about an earlier list read.
        changed = prior_pinned != new_pinned
        if changed and not await save_slot_off_loop(
            state, slot, force=True, expected_history_key=authorized_history_key
        ):
            # Refused without writing: the session was permanently deleted or
            # rebound mid-persist. Roll back the live field — but only while
            # it still holds THIS request's value, so a non-endpoint writer's
            # newer commit is not erased.
            if slot.pinned == new_pinned:
                slot.pinned = prior_pinned
            # The UNPINNED periodic flush may have persisted the provisional
            # value while this save awaited (review-caught): mark dirty so the
            # next flush reconverges the durable record to the live state.
            slot._dirty = True
            source, caller = _audit_origin(request)
            sel().log_api_access(
                caller=caller,
                operation="chat.slot_pin",
                outcome="denied",
                source=source,
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
        # The order is the person's: a pin made by any other caller does not
        # move anything in that order. A session with
        # no stored place stays unranked, one that has a place keeps it, and an
        # unpin leaves a live, unpinned key that the next person-initiated
        # write prunes. The writer leaves the order alone for an agent in a
        # channel-reachable session.
        person_caller = not folder_principal(state, request)
        order_changed = False
        order_saved: bool | None = True
        # A person's pin whose order cannot be read is answered as an error
        # below, like a failed write, so the caller retries once the read
        # works instead of trusting an order this pin never reached.
        order_readable = not person_caller or await ensure_pinned_order_loaded(state)
        # An unchanged pin still reconciles when the order disagrees with it:
        # that is the retry of a pin whose order write failed, and skipping it
        # would leave the order wrong until the next reorder.
        if (
            person_caller
            and order_readable
            and (changed or new_pinned != (slot.key in state._pinned_session_order))
        ):
            # Still under the metadata lock, so a concurrent reorder cannot
            # write an order computed before this pin landed. The pin itself is
            # already durable and stands; a failed order write is published
            # with the pin and answered as an error below, so the caller
            # re-reads the slots instead of trusting a rank that was not saved.
            order_before = list(state._pinned_session_order)
            order_saved = await _write_pinned_order(
                state,
                pinned_session_order.after_pin_change(
                    list(state._pinned_session_order),
                    slot.key,
                    new_pinned,
                    state.pinned_session_flags(),
                    state._pinned_session_order_stored,
                ),
                request=request,
            )
            order_changed = (
                order_saved is True and list(state._pinned_session_order) != order_before
            )
    # A one-row patch carries only ``pinned``, so a pin that moved this row's
    # own rank (an app re-pinning a slot the person ranked brings its place
    # back) needs the full list too.
    if order_changed or state.pin_rank(slot) != prior_rank:
        # Other rows' ranks moved too, which a one-row patch cannot carry.
        state.push_slots_update()
    else:
        state.push_slot_patch(slot.key, ("pinned",))
    source, caller = _audit_origin(request)
    if not order_readable:
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_pin",
            outcome="error",
            source=source,
            resources=name,
            error="pinned order unreadable",
        )
        return web.json_response(
            {
                "error": "the pin was saved but the gateway could not read its order file",
                "code": "pinned_order_unreadable",
                "pinned": slot.pinned,
                "changed": changed,
            },
            status=503,
        )
    if order_saved is False:
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_pin",
            outcome="error",
            source=source,
            resources=name,
            error="pinned order write failed",
        )
        return web.json_response(
            {
                "error": "the pin was saved but the gateway could not write its order file",
                "code": "pinned_order_write_failed",
                "pinned": slot.pinned,
                "changed": changed,
            },
            status=500,
        )
    sel().log_api_access(
        caller=caller,
        operation="chat.slot_pin",
        outcome="allowed",
        source=source,
        resources=name,
    )
    return web.json_response({"ok": True, "pinned": slot.pinned, "changed": changed})


async def api_chat_slot_mutes_opened(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/mutes-opened — toggle the "mute sessions it
    opens" rule on a creating session.

    When set, every session this one opens -- and anything those open, down the
    durable ``created_by`` chain -- is muted for attention on the client: no
    turn-done chime, no background-finished toast, no unread badge, starting
    from the worker's first turn. The creating session itself keeps all
    of its signals, and a tool-approval prompt from a muted session still
    surfaces (handled client-side).

    Shaped exactly like :func:`api_chat_slot_pin`, with one deliberate
    difference: this write is the user's own decision (``session_create``'s
    contract says an agent must not open sessions already silenced), so this
    route is NOT in the member-admitted set in ``handlers/_shared_route_guards.py`` and no
    MCP tool reaches it. The same three ownership fences still apply.
    """

    return await _toggle_slot_user_flag(request, "mutes_opened")


async def api_chat_slot_muted(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/muted — toggle the per-row mute of THIS session.

    A muted session raises no turn-done chime, background-finished toast or
    unread badge on the client. A tool-approval prompt from a muted session
    still surfaces. The user picks the row from its own kebab
    menu, so it can name a session other than the active one.

    Same contract as :func:`api_chat_slot_mutes_opened`: user-only, the same
    ownership fences, and persist-before-publish.
    """

    return await _toggle_slot_user_flag(request, "muted")


async def _toggle_slot_user_flag(request: web.Request, field: str) -> web.Response:
    """The shared body of the two user-owned mute toggles (``mutes_opened`` and
    ``muted``). Each comment below that names ``mutes_opened`` applies to both."""

    operation = f"chat.slot_{field}"
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    if (refusal := refuse_unattributable_caller(state, request, operation)) is not None:
        return refusal
    if (refusal := member_slot_write_refused(state, request, slot, operation)) is not None:
        return refusal
    request_app = _effective_request_app(state, request)
    if (denied := deny_app_slot_access(request_app, slot, slot.key, operation)) is not None:
        return denied
    authorized_history_key = slot_history_key(slot)
    if not app_owns_transcript(state._slots, request_app, authorized_history_key):
        audit_app_slot_denial(
            request_app,
            operation,
            slot.key,
            "app does not own this slot's transcript",
        )
        return slot_not_found()
    # User-only provenance. The feature contract (and docs/feature-map) say this
    # flag is set by the dashboard USER alone -- no agent or MCP route reaches
    # it, because session_create's own contract forbids an agent opening
    # already-silenced sessions. The ownership fences above stop a FOREIGN
    # caller, but an app or internal-agent caller writing to a slot it owns would
    # still pass them, so a shipped built-in app holding the /api/chat/slots/*
    # grant could set the flag with no user action. Require positive evidence of
    # a dashboard human here -- the same (is_dashboard_user AND not internal_auth)
    # pair the autonudge user-input notifier uses -- so an app caller
    # (is_dashboard_user is False) and a loopback MCP/cron caller
    # (internal_auth is True) are both refused before any mutation.
    if not (request.get("is_dashboard_user") is True and request.get("internal_auth") is not True):
        source, caller = _audit_origin(request)
        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="denied",
            source=source,
            resources=name,
            error="mute toggle is a dashboard-user action; app and agent callers are refused",
        )
        return web.json_response(
            {
                "error": "this setting can only be changed by a dashboard user",
                "code": f"{field}_user_only",
            },
            status=403,
        )
    # Shared parse-and-shape guard: a body that is valid JSON but not an object
    # (a list, string or number) answers 400 body_not_object instead of letting
    # the .get() below turn a client mistake into a 500. The body is a fixed set
    # of control fields, so the shared default byte cap applies.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    expected_created = str(body.get("expected_created") or "")
    async with _slot_meta_txn_lock(state):
        if (
            state._slots.get(name) is not slot
            or slot_history_key(slot) != authorized_history_key
            or (expected_created and slot.created_at != expected_created)
            or not app_owns_transcript(state._slots, request_app, authorized_history_key)
        ):
            source, caller = _audit_origin(request)
            sel().log_api_access(
                caller=caller,
                operation=operation,
                outcome="denied",
                source=source,
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
        prior = getattr(slot, field)
        new_value = body.get(field, False)
        # JSON strings such as "false" are truthy; validate the type like pin.
        if not isinstance(new_value, bool):
            return web.json_response(
                {"error": f"{field} must be a boolean", "code": f"{field}_not_bool"},
                status=400,
            )
        changed = prior != new_value
        if changed:
            # Persist-before-you-publish, done under the lock. The live
            # ``slot.mutes_opened`` is LEFT at ``prior`` across the whole save:
            # the NEW value is persisted as a STAGED override that both save
            # paths honor (the full ``build_full_line`` AND the empty-window
            # ``merge_empty_window`` branch a message-less newborn takes -- the
            # latter was the round-1 F1 gap, where the staged value was ignored
            # and the stale live flag reached disk).
            #
            # Because the live flag does not change until the write commits, a
            # concurrent ``/api/chat/slots`` GET or a worker-completion
            # ``push_slots_update`` DURING the save window reads the committed
            # ``prior`` value -- never the provisional one -- so a failed save
            # leaves nothing to correct and publishes nothing.
            #
            # The live flag is flipped by ``after_commit_under_lock``, which the
            # save invokes right after its ``atomic_write`` (or the merge's
            # commit) while STILL HOLDING the transcript ``_locked``. A
            # concurrent dirty-slot flush needs that same lock to serialize the
            # slot, so it cannot interleave between the commit and the flip and
            # resurrect ``prior`` (the round-1 F2 race). On a non-raising refuse
            # or a raising failure the write never commits, so the callback never
            # runs and the live flag stays ``prior``.
            #
            # best_effort=False so a lock timeout / disk-full PROPAGATES rather
            # than being swallowed and acknowledged as a durable change.
            #
            # COMMIT WITNESS: ``save_slot_off_loop`` can return True WITHOUT the
            # write committing -- the empty-window merge of a line-less tab (a
            # new incognito session with no metadata line yet) is a by-design
            # SKIP: its guard runs, sees an empty record, and refuses to
            # materialize a line, so ``after_commit_under_lock`` never fires yet
            # the save still reports True. ``committed`` is set INSIDE the hook,
            # so it is True only if the write genuinely committed. A True save
            # with ``committed`` still False means the value never reached disk,
            # which we must not acknowledge as a durable change.
            committed = False

            def _commit_live_flag() -> None:
                nonlocal committed
                committed = True
                setattr(slot, field, new_value)

            try:
                saved = await save_slot_off_loop(
                    state,
                    slot,
                    force=True,
                    best_effort=False,
                    expected_history_key=authorized_history_key,
                    mutes_opened_override=new_value if field == "mutes_opened" else None,
                    muted_override=new_value if field == "muted" else None,
                    after_commit_under_lock=_commit_live_flag,
                )
            except Exception:
                # The write did not commit, the live flag was never touched, and
                # nothing provisional was ever published -- so there is no
                # corrective frame to send. Mark the slot dirty only so an
                # unrelated flush re-converges the (unchanged) durable record,
                # and answer 503 so the client retries.
                slot._dirty = True
                source, caller = _audit_origin(request)
                sel().log_api_access(
                    caller=caller,
                    operation=operation,
                    outcome="denied",
                    source=source,
                    resources=name,
                    error="could not persist mute setting",
                )
                logger.warning(
                    "%s: durable save of slot=%s failed; " "live flag untouched, nothing published",
                    operation,
                    slot.key,
                    exc_info=True,
                )
                return web.json_response(
                    {
                        "error": "could not persist the mute setting; please retry",
                        "code": f"{field}_save_failed",
                    },
                    status=503,
                )
            if not saved:
                # A non-raising refuse (the slot was deleted or rebound under the
                # write). The write never committed, the live flag was never
                # touched, and nothing provisional was published -- no corrective
                # frame is owed.
                source, caller = _audit_origin(request)
                sel().log_api_access(
                    caller=caller,
                    operation=operation,
                    outcome="denied",
                    source=source,
                    resources=name,
                    error="session was deleted or rebound",
                )
                return web.json_response(
                    {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
                )
            if not committed:
                # The save reported success WITHOUT the write committing: the
                # empty-window merge of a line-less tab (a new incognito session
                # with no metadata line yet) refused to materialize a line, so
                # ``after_commit_under_lock`` never ran. The value never reached
                # disk, the live flag was never flipped, and nothing provisional
                # was published -- so there is nothing to correct. Answer 409 so
                # the client knows the setting cannot be saved yet (it takes once
                # the session has written its first line) rather than reporting a
                # change that never persisted.
                source, caller = _audit_origin(request)
                sel().log_api_access(
                    caller=caller,
                    operation=operation,
                    outcome="denied",
                    source=source,
                    resources=name,
                    error="session has no durable record yet",
                )
                logger.info(
                    "%s: slot=%s has no metadata line yet; "
                    "mute not persisted, live flag untouched, nothing published",
                    operation,
                    slot.key,
                )
                return web.json_response(
                    {
                        "error": "this session cannot be saved yet; try again after it has started",
                        "code": "session_gone",
                    },
                    status=409,
                )
    state.push_slot_patch(slot.key, (field,))
    source, caller = _audit_origin(request)
    sel().log_api_access(
        caller=caller,
        operation=operation,
        outcome="allowed",
        source=source,
        resources=name,
    )
    return web.json_response({"ok": True, field: getattr(slot, field), "changed": changed})


async def ensure_pinned_order_loaded(state: DashboardState) -> bool:
    """Read the stored pinned order if it has not landed yet; ``True`` once loaded.

    The gateway reads the file after it starts serving (see
    ``load_pinned_order_after_listen``), so a pin or reorder can arrive first.
    Every writer calls this before it computes a new order, or it would write
    one derived from an empty list over the stored one. Returns ``False`` when
    the file could not be read, and the writer leaves the stored order alone.

    Whichever caller lands the read publishes it when it holds any key: the
    slots frames sent before it carried no ``pin_rank``, and a writer that then
    refuses (a 409 hand-off) would otherwise leave every open tab on the plain
    sort. An empty read changes no rank, so it sends nothing.
    """
    if not state._pinned_session_order_loaded:
        read = await asyncio.to_thread(state.read_pinned_session_order)
        if state.adopt_pinned_session_order(read) and state._pinned_session_order:
            state.push_slots_update()
    return state._pinned_session_order_loaded


async def load_pinned_order_after_listen(state: DashboardState) -> None:
    """Read the pinned order after the listener is up; the read publishes the ranks.

    Kept off the boot path (``no-new-work-on-gateway-boot-path``): until it
    lands, slot rows carry no ``pin_rank`` and pinned sessions show in the
    sidebar's own sort for a moment. The manual order, kept in the same store,
    is read next, on its own terms (``chat_manual_order``).
    """
    try:
        await ensure_pinned_order_loaded(state)
    except Exception:  # noqa: BLE001 -- a display preference must not crash startup
        logger.warning("could not load the pinned session order", exc_info=True)
    await load_manual_order_after_listen(state)


async def _write_pinned_order(
    state: DashboardState, keys: list[str], *, request: web.Request, explicit: bool = False
) -> bool | None:
    """Persist *keys* as the pinned order, then adopt it in memory.

    The order is the person's: a channel-reachable agent returns ``None``
    without changing disk or memory. Every order write passes here, so the
    rule holds on every route without refusing the pin itself.

    Memory changes only after the file write succeeds, so the order the
    sidebar shows is always the one a restart would load. Returns ``False``
    (logged) when the write fails.

    An unchanged order is skipped, except an *explicit* reorder that has not
    been stored yet: that request is the person setting an order, even an
    empty one, and the stored file is what later refuses a stale browser
    hand-off. A pin change is never explicit, so pinning before any reorder
    still writes nothing and keeps the plain sort.
    """
    if _agent_in_channel_reachable_session(request):
        return None
    if keys == list(state._pinned_session_order) and (
        state._pinned_session_order_stored or not explicit
    ):
        return True
    try:
        await asyncio.to_thread(state.save_pinned_session_order, keys)
    except OSError:
        logger.warning("could not write the pinned session order", exc_info=True)
        return False
    state._pinned_session_order = keys
    state._pinned_session_order_loaded = True
    state._pinned_session_order_stored = True
    state.bump_pinned_order_rev()
    return True


#: Longest ``created`` token a reorder may echo per key: an ISO-8601 timestamp
#: with offset is 32 characters, so this leaves room without leaving it open.
_MAX_EXPECTED_CREATED_CHARS = 64


def _replaced_keys(state: DashboardState, expected_created: dict[str, str]) -> list[str]:
    """Keys whose live slot carries a different ``created`` than the caller saw.

    A key with no live slot is not counted: the order keeps keys the gateway
    may still be restoring, and a plain create later starts unpinned.
    """
    return [
        key
        for key, created in expected_created.items()
        if (slot := state._slots.get(key)) is not None and created and slot.created_at != created
    ]


#: Byte ceiling for a pinned-order body: every key at its maximum length plus
#: JSON quoting and separators, once in ``keys`` and once more with its
#: ``created`` token in ``expected_created``, plus room for the field names.
_MAX_PINNED_ORDER_BODY_BYTES = (
    pinned_session_order.MAX_PINNED_ORDER_KEYS
    * (2 * (pinned_session_order.MAX_PINNED_ORDER_KEY_CHARS + 4) + _MAX_EXPECTED_CREATED_CHARS + 4)
    + 256
)


def _channel_pinned_order_refusal(request: web.Request) -> web.Response:
    """Keep both reorder containment gates on the same denial and SEL row."""
    _, origin_caller = _audit_origin(request)
    sel().log_api_access(
        caller=origin_caller,
        operation="chat.pinned_order",
        outcome="denied",
        source="channel_containment",
        error="agent reorder from a session a channel can drive",
    )
    return web.json_response(
        {
            "error": (
                "this session can receive turns from a channel conversation, so "
                "an agent cannot reorder pinned sessions from it"
            ),
            "code": "channel_reachable_caller",
        },
        status=403,
    )


async def api_chat_pinned_order(request: web.Request) -> web.Response:
    """POST /api/chat/pinned-order -- set the order of pinned sessions in one write.

    Body: ``{"keys": [slot_key, ...]}``, the pinned sessions in the order they
    should appear. The named keys take the places they already held, in the
    requested order; stored keys the request did not name stay where they are,
    and named keys that were not stored yet follow. The whole order is
    one file, replaced atomically, so the reorder lands entirely or not at all.

    ``"only_if_unset": true`` makes the write conditional: it lands only while
    no order is stored, and is otherwise a 409 ``pinned_order_exists`` carrying
    the stored order. The sidebar's one-time hand-off of a browser's old local
    order uses it.

    Every key must name a live, pinned session. One that does not is a 409 for
    the whole request: the caller computed its order from a sidebar that has
    since changed, and applying part of it would put sessions where nobody
    asked. The ``only_if_unset`` hand-off is the exception: it carries a
    browser's whole old order, which may name sessions the gateway has not
    restored yet, so its keys are pruned instead of checked. A repeated key is
    a 400.

    The order is the person's sidebar preference and spans sessions no single
    app or crew member owns, so an app or member caller is refused with 403.
    An agent in a session a channel can reach is also refused; the person's
    browser may still reorder from that session.
    """
    state: DashboardState = request.app["state"]
    if (refusal := refuse_unattributable_caller(state, request, "chat.pinned_order")) is not None:
        return refusal
    principal = folder_principal(state, request)
    if principal:
        sel().log_api_access(
            caller=principal,
            operation="chat.pinned_order",
            outcome="denied",
            source="app_isolation",
            error="only the person may reorder pinned sessions",
        )
        return web.json_response(
            {
                "error": "only the person may reorder pinned sessions",
                "code": "pinned_order_person_only",
            },
            status=403,
        )
    if _agent_in_channel_reachable_session(request):
        return _channel_pinned_order_refusal(request)
    body, body_err = await read_bounded_json(request, max_bytes=_MAX_PINNED_ORDER_BODY_BYTES)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    keys = body.get("keys")
    if not isinstance(keys, list) or not all(
        isinstance(key, str) and 0 < len(key) <= pinned_session_order.MAX_PINNED_ORDER_KEY_CHARS
        for key in keys
    ):
        return web.json_response(
            {"error": "keys must be an array of session keys", "code": "keys_invalid"},
            status=400,
        )
    if len(keys) > pinned_session_order.MAX_PINNED_ORDER_KEYS:
        return web.json_response(
            {"error": "too many keys in one reorder", "code": "keys_too_many"}, status=400
        )
    if len(set(keys)) != len(keys):
        return web.json_response(
            {"error": "a key appears more than once", "code": "keys_duplicate"}, status=400
        )
    raw_only_if_unset = body.get("only_if_unset", False)
    if not isinstance(raw_only_if_unset, bool):
        return web.json_response(
            {"error": "only_if_unset must be a boolean", "code": "only_if_unset_not_bool"},
            status=400,
        )
    only_if_unset = raw_only_if_unset
    # Optional per-key generation tokens, the same ``created`` value the pin
    # route reads as ``expected_created``: a sidebar or the MCP tool read the
    # slots before sending, and a key can be closed and recreated for another
    # conversation in between. A named key whose live slot carries a
    # different ``created`` is refused instead of ranked.
    raw_expected = body.get("expected_created", {})
    if not isinstance(raw_expected, dict) or not all(
        isinstance(key, str)
        and isinstance(value, str)
        and len(value) <= _MAX_EXPECTED_CREATED_CHARS
        for key, value in raw_expected.items()
    ):
        return web.json_response(
            {
                "error": "expected_created must map session keys to strings",
                "code": "expected_created_invalid",
            },
            status=400,
        )
    named = set(keys)
    expected_created = {key: value for key, value in raw_expected.items() if key in named}
    async with _slot_meta_txn_lock(state):
        if not await ensure_pinned_order_loaded(state):
            return web.json_response(
                {"error": "the pinned order could not be read", "code": "pinned_order_unreadable"},
                status=503,
            )
        if only_if_unset and state._pinned_session_order_stored:
            # A browser handing over its old local order: only an order nobody
            # has set yet may take it, so it cannot replace a newer one saved
            # from another browser since that browser last read the slots.
            return web.json_response(
                {
                    "error": "a pinned order is already stored",
                    "code": "pinned_order_exists",
                    "order": list(state._pinned_session_order),
                    "rev": state._pinned_order_rev,
                },
                status=409,
            )
        # Checked under the same lock the pin route holds, so no pin or unpin
        # can land between this check and the write.
        flags = state.pinned_session_flags()
        # A browser's hand-off carries its whole old order, including sessions
        # the gateway may still be restoring, so it is not checked against the
        # live set: prune keeps keys with no live slot and drops only live,
        # unpinned ones. A reorder from a live sidebar must name live pins.
        missing = [] if only_if_unset else [key for key in keys if not flags.get(key)]
        if missing:
            return web.json_response(
                {
                    "error": "a session in the reorder is gone or no longer pinned",
                    "code": "slot_not_pinned",
                    "keys": missing[:10],
                },
                status=409,
            )
        replaced = _replaced_keys(state, expected_created)
        if replaced:
            return web.json_response(
                {
                    "error": "a session in the reorder was replaced by a new one",
                    "code": "session_gone",
                    "keys": replaced[:10],
                },
                status=409,
            )
        order = pinned_session_order.after_reorder(
            list(state._pinned_session_order), list(keys), flags
        )
        while True:
            saved = await _write_pinned_order(state, order, request=request, explicit=True)
            if saved is None:
                return _channel_pinned_order_refusal(request)
            if not saved:
                return web.json_response(
                    {
                        "error": "the gateway could not write its order file",
                        "code": "pinned_order_write_failed",
                    },
                    status=500,
                )
            # The write runs off the loop, and creating or restoring a slot
            # does not take this lock, so a named session can be replaced
            # while it runs. Its successor must not keep the place given to
            # the session it replaced: drop it and write again. Each pass
            # removes a key, so this ends.
            stale = set(_replaced_keys(state, expected_created)) & set(order)
            if not stale:
                break
            order = [key for key in order if key not in stale]
    state.push_slots_update()
    source, caller = _audit_origin(request)
    sel().log_api_access(
        caller=caller,
        operation="chat.pinned_order",
        outcome="allowed",
        source=source,
        resources=",".join(keys[:10]),
    )
    return web.json_response({"ok": True, "order": order, "rev": state._pinned_order_rev})


_VALID_MODES = ("",)


async def api_chat_slot_mode(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/mode — switch session mode."""

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    # Capture the transcript key the lookup above just covered, BEFORE the
    # body-parse and busy-check awaits — the same rebind window
    # api_chat_slot_folder documents. The re-check before the mutation and
    # the save's expected_history_key pin together keep this request's write
    # on the transcript it was authorized against.
    authorized_history_key = slot_history_key(slot)
    # App ownership (App Kit §5.2) — the same deny-by-default rule api_chat_send
    # and api_chat_slot_create apply, and it matters HERE because the mode
    # decides which execution model a session runs under: an app holding
    # `/api/chat` could otherwise list a foreign slot and PATCH it into (or out
    # of) crew mode, changing a session it does not own. One code for both
    # reasons on purpose — a distinct code per reason would turn this 404 into an
    # existence oracle for slots the caller may not know about. A caller whose
    # tab closed mid-call is refused first: its derived app would be "" and read
    # as the person (the same guard the tree writes apply).
    if (refusal := refuse_unattributable_caller(state, request, "chat.slot_mode")) is not None:
        return refusal
    request_app = request.get("app", "")
    if (denied := deny_app_slot_access(request_app, slot, slot.key, "chat.slot_mode")) is not None:
        return denied
    # ``_app`` says who owns the slot OBJECT; the write persists into the
    # TRANSCRIPT ``authorized_history_key`` names. Same rule as the folder and
    # tag writes, same indistinguishable 404.
    if not app_owns_transcript(state._slots, request_app, authorized_history_key):
        audit_app_slot_denial(
            request_app, "chat.slot_mode", slot.key, "app does not own this slot's transcript"
        )
        return slot_not_found()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    mode = _coerce_requested_mode(body.get("mode", ""))
    if mode not in _VALID_MODES:
        return web.json_response({"error": "invalid mode"}, status=400)
    # Member DM threads (mode="member") are pinned to their crew, and every
    # pin guard is conditioned on this very field — so the mode writer is the
    # one door that would unlock all of them at once (PATCH mode -> "", then
    # the agent switch endpoint passes its guard). "member" is deliberately
    # absent from _VALID_MODES (mode cannot be SET here), and here it cannot
    # be UNSET either: member slots are born and retired only through the
    # member-thread endpoint.
    if slot.mode == "member":
        return web.json_response(
            {"error": "member thread mode is locked", "code": "member_mode_locked"},
            status=409,
        )
    # A relay archive is read-only: a non-plain mode would run its tools and
    # filesystem work on THIS machine for a chat a peer crew ran.
    if slot.executor == "remote" and mode:
        return web.json_response(
            {
                "error": "a crew-bound session runs plain chat only; mode-specific work runs on the crew, not here",
                "code": "remote_mode_unsupported",
            },
            status=409,
        )
    # Serialize the busy-check/re-check/mutate/persist/rollback span under
    # the state-wide metadata txn lock — same rationale as
    # api_chat_slot_folder. The busy guard runs INSIDE the lock: waiting on a
    # concurrent metadata save can take long enough for a turn to start, so a
    # guard evaluated before the acquisition would be stale by the time the
    # mutation runs (review-caught).
    async with _slot_meta_txn_lock(state):
        # Re-authorize after the awaits above (body parse, lock acquisition):
        # same slot OBJECT still registered under the name, routing still on
        # the transcript captured before the first await, and that transcript
        # still owned by the caller's app.
        if (
            state._slots.get(name) is not slot
            or slot_history_key(slot) != authorized_history_key
            or not app_owns_transcript(state._slots, request_app, authorized_history_key)
        ):
            sel().log_api_access(
                caller="dashboard",
                operation="chat.slot_mode",
                outcome="denied",
                source="dashboard",
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
        # Work in SUBAGENTS keeps `slot.running` false the whole time, so that
        # flag alone lets the mode flip mid-flight and interleave two execution
        # models in one session: a plain-chat subagent may be running on this
        # slot right now, and its completion follows the default `_run_chat`
        # path, so the switch has to be refused in EITHER direction while one
        # is pending.
        busy = False
        subs = getattr(state, "subagents", None)
        if subs is not None:
            try:
                # The key the SPAWN ran under, which for a channel-linked slot
                # is the channel session, not `dashboard:<tab>` —
                # `has_pending_work_for` matches `parent_session_key` exactly,
                # so deriving it differently here reports "idle" while that
                # slot's subagents are still running and flips the execution
                # model out from under them.
                busy = bool(await _subagent_work_pending(subs, effective_session_key(slot)))
            except Exception:
                busy = True  # fail closed: refuse rather than risk the flip
        if slot.running or busy:
            sel().log_api_access(
                caller="dashboard",
                operation="chat.slot_mode",
                outcome="denied",
                source="dashboard",
                resources=name,
            )
            return web.json_response(
                {"error": "cannot switch mode while session is running"}, status=409
            )
        prior_mode = slot.mode
        slot.mode = mode
        if not await save_slot_off_loop(
            state, slot, force=True, expected_history_key=authorized_history_key
        ):
            # Refused without writing: the session was permanently deleted or
            # rebound mid-persist. Roll back the live fields — but only while
            # the mode still holds THIS request's value, so a non-endpoint
            # writer's newer commit is not erased.
            if slot.mode == mode:
                slot.mode = prior_mode
            # The UNPINNED periodic flush may have persisted the provisional
            # value while this save awaited (review-caught): mark dirty so the
            # next flush reconverges the durable record to the live state.
            slot._dirty = True
            sel().log_api_access(
                caller="dashboard",
                operation="chat.slot_mode",
                outcome="denied",
                source="dashboard",
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.slot_mode",
        outcome="allowed",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True, "mode": slot.mode})


# This module is the facade of the chat folder routes: every name is reached through it as
# ``kiro_crew.dashboard.chat_folders.<name>``. A read is served by the owner that binds the
# name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner holding
# it (:mod:`kiro_crew._facade_mirror`). Owners never import this module, and they log under
# its logger name. The slot routes above stay here; new work goes to the owner of its
# responsibility:
#
#   ``chat_folders_icons`` -- folder icons, colours and the background emoji generator
#   ``chat_folders_paths`` -- the project and steering directories a folder pins
#   ``chat_folders_records`` -- folder records, the create rules and who may write them
#   ``chat_folders_routes`` -- the ``/api/chat/folders`` routes
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.chat_folders_icons",
    "kiro_crew.dashboard.chat_folders_paths",
    "kiro_crew.dashboard.chat_folders_records",
    "kiro_crew.dashboard.chat_folders_routes",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.chat_folders_icons import (  # noqa: F401
        _CHAT_FOLDER_ICON_EPOCHS,
        _CHAT_FOLDER_ICON_TASKS,
        _CHAT_FOLDER_PENDING_ICON_TASKS,
        _FOLDER_COLOR_PALETTE,
        _FOLDER_ICON_MODEL,
        _bump_icon_epoch,
        _folder_icon_lock,
        _is_single_emoji,
        _is_valid_folder_color,
        _spawn_chat_folder_icon_task,
        generate_emoji_for_name,
    )
    from kiro_crew.dashboard.chat_folders_paths import (  # noqa: F401
        MAX_FOLDER_STEERING_DIR_LEN,
        MAX_FOLDER_STEERING_DIRS,
        _folder_declared_project,
        _folder_project_overlap_denied,
        _refuse_principal_steering_dirs,
        _resolve_folder_project_dir,
        _resolve_folder_steering_dirs,
        _validate_project_dir,
        _validate_steering_dirs,
        resolve_folder_project_dir_off_loop,
        slot_steering_principal,
    )
    from kiro_crew.dashboard.chat_folders_records import (  # noqa: F401
        _KNOWN_INTERNAL_CALLERS,
        CREATED_BY_SESSION,
        MAX_CHAT_FOLDERS,
        FolderCapError,
        FolderCreateError,
        FolderNameExistsError,
        FolderOwnershipError,
        _caller_reaches_a_channel,
        _folder_history_counts,
        _folders_with_history_counts,
        _refuse_unattributable_caller,
        _slot_home_folder_id,
        _validate_folder_tags,
        agent_creator_key,
        archived_session_names_in_folders,
        caller_home_slot,
        chat_folder_subtree_ids,
        create_folder_record,
        folder_ids_filed_into,
        live_slot_names_in_folders,
    )
    from kiro_crew.dashboard.chat_folders_routes import (  # noqa: F401
        _LAYOUT_ONLY_FIELDS,
        _MAX_REORDER_BODY_BYTES,
        _MAX_REORDER_ENTRIES,
        api_chat_folder_create,
        api_chat_folder_delete,
        api_chat_folder_reorder,
        api_chat_folder_update,
        api_chat_folders,
    )
