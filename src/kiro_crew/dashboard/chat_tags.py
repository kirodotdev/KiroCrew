"""Session tags — dynamic vocabulary CRUD, slot assignment, and sidebar columns.

Tags are user-defined labels (id/name/color/status) attached to dashboard chat
slots. Sessions can carry multiple tags. The sidebar renders as a horizontal
strip of user-configurable columns. A column filters either by a set of tags
(any/all/none mode) or by the session's live runtime lane — needs-approval,
waiting, working, idle — which is derived from the slot payload rather than
stored on the session. Dragging a session card between columns that carry a
single status tag as filter reassigns the card's status tag; a derived state
lane refuses the drop, because only the agent's own progress moves a card there.
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import logging
import sys as _sys
import uuid
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_tag_grants import capture_grants_snapshot, refresh_cache
from kiro_crew.dashboard.chat_tags_policy import (
    _NAME_MAX,
    agent_slot_tags_refusal,
    slot_tag_write_principal,
)
from kiro_crew.dashboard.chat_tags_store import (
    _bump_slot_tags_revision,
    _order_key,
    _tags_write_lock,
)
from kiro_crew.dashboard.chat_tags_vocabulary import _refuse_vocabulary_write
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.create_rate_limit import TAG_COLUMN_CREATE, allow_create
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.slot_ownership import (
    audit_app_slot_denial,
    deny_app_slot_access,
    slot_not_found,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.token_auth import (
    app_owns_transcript,
    effective_request_app,
    refuse_unattributable_caller,
    request_origin,
)
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.dashboard.chat_tags")

_VALID_MODES = {"any", "all", "none"}

# A column filters either by tag ("tags", the default) or by the session's live
# runtime state ("state"). A state column names one lane in _VALID_STATE_KEYS;
# membership is derived from the slot payload on every push, so a card moves
# between state lanes on its own and no tag is ever written for it.
_VALID_SOURCES = {"tags", "state"}

# The lane vocabulary. Kept server-side so an unknown key cannot reach the board
# and render an eternally-empty column: the lanes are exhaustive and mutually
# exclusive by construction, and a card that matched nothing would vanish.
_VALID_STATE_KEYS = {"needs_approval", "waiting", "working", "idle"}


# ── Slot tag assignment ────────────────────────────────────────────────────


async def api_chat_slot_tags(request: web.Request) -> web.Response:
    """PUT /api/chat/slots/{slot}/tags — replace the slot's tag list.

    Holds the tags write lock across validate→assign→persist so a concurrent
    DELETE cannot remove a tag id between validation and slot persistence
    (preventing dangling references).
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    # App ownership (App Kit §5.2) — the same deny-by-default rule
    # ``chat_folders.api_chat_slot_folder`` applies to filing, and for the same
    # reason: tagging is a write to a session's own state, and the
    # ``chat_tag_assign`` MCP tool reaches this route on behalf of an app agent
    # that scopes what it can SEE client-side — the boundary has to hold here,
    # where the authoritative slot table is. Same 404 for both reasons so the
    # route is not an existence oracle for slots the caller cannot see. The
    # identity comes from the middleware's claim, re-derived through the shared
    # rule when absent — never from the body. A caller whose tab closed mid-call
    # is refused first: its derived app would be "" and read as the person.
    refused = refuse_unattributable_caller(state, request, "chat.slot_tags")
    if refused is not None:
        return refused
    # Member ownership, beside the app fence and for the same reason it lives in
    # ``chat_folders.api_chat_slot_folder``: a member carries no app claim, so
    # the app guard below is a no-op for it and would let it tag ANY session.
    # Lazy import avoids a module-load cycle (session_control imports
    # chat_folders). A non-member caller makes this a no-op.
    from kiro_crew.dashboard.chat_folders import member_slot_write_refused

    refused = member_slot_write_refused(state, request, slot, "chat.slot_tags")
    if refused is not None:
        return refused
    request_app = effective_request_app(state, request)
    if (denied := deny_app_slot_access(request_app, slot, slot.key, "chat.slot_tags")) is not None:
        return denied
    # Capture the transcript key the lookup above just covered, BEFORE the
    # body-parse and lock awaits: ``linked_session_key`` is rebound on
    # already-live slots with no ``running`` gate (cron completions, workflow
    # injections), so a slow caller can be authorized against its own session
    # and land on somebody else's conversation. The re-check below and the
    # save's expected_history_key pin together keep this request's write on
    # the transcript it was authorized against.
    authorized_history_key = slot_history_key(slot)
    # The slot's ``_app`` says who owns the slot OBJECT; the write persists into
    # the TRANSCRIPT that key names, which a linked slot can point at another
    # owner's session. Both must resolve to the caller's app, or the write would
    # carry an app's tags into a conversation it cannot be shown to own. Same
    # indistinguishable 404, for the same reason.
    if not app_owns_transcript(state._slots, request_app, authorized_history_key):
        audit_app_slot_denial(
            request_app, "chat.slot_tags", slot.key, "app does not own this slot's transcript"
        )
        return slot_not_found()
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    raw_ids = body.get("tags")
    if not isinstance(raw_ids, list):
        return web.json_response(
            {"error": "tags must be an array", "code": "tags_not_array"}, status=400
        )
    # Optional compare-and-swap precondition: the revision the caller composed
    # its list onto. A client's list is a one-click delta applied to the last
    # snapshot it ACCEPTED; if another client committed since, that snapshot is
    # stale and replacing the slot's list wholesale would silently drop the
    # other client's tag. Absent (legacy or scripted callers) the write is
    # unconditional, as before.
    raw_base = body.get("base_tags_revision")
    if raw_base is not None and not isinstance(raw_base, str):
        return web.json_response(
            {"error": "base_tags_revision must be a string", "code": "base_not_string"},
            status=400,
        )
    base_tags_revision: str | None = raw_base or None

    # The agent tag-grants policy applies to every NON-PERSON caller: the
    # browser is the person, who may set or strip any tag — the whole grants
    # store exists to constrain what an AGENT may do to a session's tags, not
    # the owner. ``chat_tag`` ``set_state`` enforces it, and this
    # wholesale-replace route (which ``chat_tag_assign`` reaches) must too, or
    # the policy is bypassable here. Who counts as the person is decided by
    # ``slot_tag_write_principal`` (the three non-person signals), shared with
    # the board drop so the two writes cannot classify a caller differently.
    is_internal_caller, _source, origin_caller, member_principal = slot_tag_write_principal(
        request, request_app, what="slot tags"
    )
    # Pull the grants store read+parse off the event loop ONCE, BEFORE the lock,
    # so the atomic snapshot captured in-lock below serves the installed
    # in-memory cache (the snapshot is cache-only and touches no filesystem).
    # Only agent callers consult it, so skip the thread hop for the browser.
    if is_internal_caller:
        await asyncio.to_thread(refresh_cache)

    async with _tags_write_lock(state):
        valid_ids = {t.get("id") for t in state._tags}
        new_tags: list[str] = []
        for tid in raw_ids:
            if isinstance(tid, str) and tid in valid_ids and tid not in new_tags:
                new_tags.append(tid)
        # Re-authorize after the awaits above (body parse, lock acquisition):
        # the same slot OBJECT must still be registered under the name, and
        # its routing must still resolve to the transcript captured before
        # the first await — a rebind in either window means this request's
        # authorization does not cover the write target. Ownership of the
        # transcript is re-asked too: a foreign slot binding to it during the
        # awaits would make the earlier answer stale.
        if (
            state._slots.get(name) is not slot
            or slot_history_key(slot) != authorized_history_key
            or not app_owns_transcript(state._slots, request_app, authorized_history_key)
        ):
            sel().log_api_access(
                caller="dashboard",
                operation="chat.slot_tags",
                outcome="denied",
                source="dashboard",
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"},
                status=409,
            )
        if base_tags_revision is not None and base_tags_revision != slot.tags_revision:
            # Decided under the same lock every writer holds, so the revision
            # compared here is the one the slot will still hold if we proceed.
            # Nothing is written; the caller rebases its delta onto the list
            # and revision returned and retries.
            sel().log_api_access(
                caller="dashboard",
                operation="chat.slot_tags",
                outcome="denied",
                source="dashboard",
                resources=name,
                error="stale base revision",
            )
            return web.json_response(
                {
                    "error": "tags changed since the list was composed",
                    "code": "stale_base",
                    "base_tags_revision": base_tags_revision,
                    "tags_revision": slot.tags_revision,
                    "tags": slot.tags,
                },
                status=409,
            )
        # Agent tag-grants policy for non-person callers. The requested list is
        # a wholesale replace, so the add/remove the caller is asking for is the
        # DIFF against the slot's CURRENT tags — read FRESH inside the lock, the
        # same discipline the rest of this write span uses, so a concurrent
        # change cannot make us gate a stale delta. The person (browser) is
        # never gated; it reached ``is_internal_caller`` False and skips this
        # entirely. ONE atomic grants snapshot (store health + rows together)
        # is captured here and threaded through the gate, so a concurrent cache
        # refresh cannot swap the store's health out from under a row lookup.
        if is_internal_caller:
            snapshot = await asyncio.to_thread(capture_grants_snapshot)
            current_ids = list(getattr(slot, "tags", None) or [])
            # ``new_tags`` is already filtered to the vocabulary, so an
            # unresolvable removal on a non-authoritative vocabulary is caught
            # by the helper, not silently persisted.
            refusal = agent_slot_tags_refusal(state, current_ids, new_tags, snapshot)
            if refusal is not None:
                code, tag_id = refusal
                sel().log_api_access(
                    caller=request_app or member_principal or origin_caller,
                    operation="chat.slot_tags",
                    outcome="denied",
                    source="app_isolation",
                    resources=f"slot={slot.key}" + (f" tag={tag_id}" if tag_id else ""),
                    error=code,
                )
                return web.json_response(
                    {"error": f"{code}:{tag_id}" if tag_id else code, "code": code},
                    status=403,
                )
        prior_tags = slot.tags
        prior_tags_revision = slot.tags_revision
        slot.tags = new_tags
        written_tags_revision = _bump_slot_tags_revision(slot)
        if not await save_slot_off_loop(
            state, slot, force=True, expected_history_key=authorized_history_key
        ):
            # Refused without writing: the session was permanently deleted or
            # rebound mid-persist. Roll back the live field — but only while
            # it still holds THIS request's value: the write span awaits, so
            # a concurrent writer may have committed a newer value that an
            # unconditional restore would erase (the same guard
            # _restore_unfiled applies to its rollback).
            if slot.tags == new_tags and slot.tags_revision == written_tags_revision:
                slot.tags = prior_tags
                # Do NOT reuse prior_tags_revision: a client that adopted the
                # leaked provisional revision from a concurrent broadcast
                # already classifies the prior one as a known predecessor and
                # would ignore a frame carrying it, keeping the rejected tags.
                # A fresh revision is an authoritative change every client
                # must adopt; broadcast it so they reconverge now.
                _bump_slot_tags_revision(slot)
            # The UNPINNED periodic flush may have persisted the provisional
            # value to the slot's current transcript while this save awaited
            # (review-caught): mark dirty so the next flush reconverges the
            # durable record to the rolled-back live state.
            slot._dirty = True
            state.push_slots_update()
            sel().log_api_access(
                caller="dashboard",
                operation="chat.slot_tags",
                outcome="denied",
                source="dashboard",
                resources=name,
                error="session was deleted or rebound",
            )
            # The provisional revision sat on the live slot while the save
            # awaited, so a concurrent slots broadcast may already have shown
            # it to clients. Name it in the rejection so a client can classify
            # that leaked frame as stale rather than as a newer writer's commit
            # (and so not reapply the rejected tags on its next toggle).
            return web.json_response(
                {
                    "error": "session was deleted or rebound",
                    "code": "session_gone",
                    "rejected_tags_revision": written_tags_revision,
                    "tags_revision": slot.tags_revision,
                    # The list the slot actually holds after rollback (a
                    # concurrent writer's commit if one landed mid-write). The
                    # client seeds its accepted snapshot from this so a rapid
                    # retry composes onto the server's state, not onto the
                    # pre-write baseline it captured before that writer landed.
                    "tags": slot.tags,
                },
                status=409,
            )

    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.slot_tags",
        outcome="allowed",
        source="dashboard",
        resources=name,
    )
    return web.json_response(
        {
            "ok": True,
            "tags": slot.tags,
            "tags_revision": slot.tags_revision,
            "prior_tags_revision": prior_tags_revision,
        }
    )


# ── Sidebar columns (Trello-style filtered lanes) ──────────────────────────


def _normalize_column(
    state: DashboardState, raw: Any, *, existing: dict | None = None
) -> dict | None:
    """Validate + coerce a column payload. Returns None if invalid.

    An unrecognized tag id is a REJECTION, not a silent drop: quietly
    discarding it while returning 200 lands the column back on
    ``tag_ids: []`` — the deliberate match-all state — so a stale or buggy
    client presents as "the filter does nothing" with no signal anywhere.
    An empty list stays valid: it is the documented match-all/clear-filter
    state the board UI depends on.

    ``source`` discriminates the two column kinds. ``"tags"`` (the default, and
    what every column persisted without the field is read as) filters by
    ``tag_ids``/``mode``. ``"state"`` filters by the session's live runtime lane
    named in ``state_key`` and ignores the tag fields entirely.
    """
    if not isinstance(raw, dict):
        return None
    valid_ids = {t.get("id") for t in state._tags}
    cleaned: dict[str, Any] = dict(existing or {})
    if "tag_ids" in raw:
        tag_ids = raw.get("tag_ids") or []
        if not isinstance(tag_ids, list):
            return None
        if not all(isinstance(t, str) and t in valid_ids for t in tag_ids):
            return None
        cleaned["tag_ids"] = [str(t) for t in tag_ids]
    if "mode" in raw:
        mode = str(raw.get("mode") or "any")
        if mode not in _VALID_MODES:
            return None
        cleaned["mode"] = mode
    if "name" in raw:
        cleaned["name"] = str(raw.get("name") or "").strip()[:_NAME_MAX]
    if "order" in raw:
        order_val = raw.get("order")
        if order_val is not None:
            try:
                cleaned["order"] = int(order_val)
            except (TypeError, ValueError):
                pass
    if "include_untagged" in raw:
        cleaned["include_untagged"] = bool(raw.get("include_untagged"))
    if "source" in raw:
        source = str(raw.get("source") or "tags")
        if source not in _VALID_SOURCES:
            return None
        cleaned["source"] = source
    if "state_key" in raw:
        state_key = str(raw.get("state_key") or "")
        if state_key and state_key not in _VALID_STATE_KEYS:
            return None
        cleaned["state_key"] = state_key
    cleaned.setdefault("mode", "any")
    cleaned.setdefault("tag_ids", [])
    cleaned.setdefault("name", "")
    cleaned.setdefault("order", 0)
    cleaned.setdefault("include_untagged", False)
    cleaned.setdefault("source", "tags")
    cleaned.setdefault("state_key", "")
    # A state column without a lane key would match nothing, and a tag column
    # carrying one would claim a lane it does not filter by. Refuse both rather
    # than coercing: a silently-corrected column reads as a broken filter.
    if cleaned["source"] == "state" and not cleaned["state_key"]:
        return None
    if cleaned["source"] == "tags" and cleaned["state_key"]:
        return None
    return cleaned


async def api_chat_tag_columns(request: web.Request) -> web.Response:
    """GET /api/chat/tag-columns — list sidebar column layout."""
    state: DashboardState = request.app["state"]
    return web.json_response(sorted(state._tag_boards, key=_order_key))


def _state_lane_owner(
    state: DashboardState, state_key: str, *, exclude_id: str | None = None
) -> dict | None:
    """Return the existing column already holding ``state_key``, or None.

    A lane is a singleton by nature: two columns naming the same runtime state
    would render every matching session twice and double its count. Uniqueness
    therefore has to be decided HERE, inside ``_tags_write_lock``, because that
    is the only place the board is serialized. A client cannot enforce it — any
    check it makes is against a cached column list, so two dashboards (or one
    with a stale cache) can both conclude a lane is missing and both create it.
    """
    if not state_key:
        return None
    for col in state._tag_boards:
        if col.get("source") != "state" or col.get("state_key") != state_key:
            continue
        if exclude_id is not None and col.get("id") == exclude_id:
            continue
        return col
    return None


def _tag_column_twin(state: DashboardState, column: dict) -> dict | None:
    """The existing tag column that shows exactly what *column* would.

    Same name (case-insensitive), tag list, mode and ``include_untagged``: a
    column that also shows untagged sessions is a different view, so it is not
    a twin. ``tag_columns.json`` is loaded verbatim, so a persisted ``tag_ids``
    that is not a list is simply not a match rather than an error.
    """
    want_name = str(column.get("name") or "").strip().lower()
    want_ids = list(column.get("tag_ids") or [])
    for col in state._tag_boards:
        if col.get("source", "tags") != "tags":
            continue
        if str(col.get("name") or "").strip().lower() != want_name:
            continue
        have_ids = col.get("tag_ids")
        if not isinstance(have_ids, list) or have_ids != want_ids:
            continue
        if str(col.get("mode") or "any") != str(column.get("mode") or "any"):
            continue
        if bool(col.get("include_untagged")) != bool(column.get("include_untagged")):
            continue
        return col
    return None


async def api_chat_tag_column_create(request: web.Request) -> web.Response:
    """POST /api/chat/tag-columns — append a new sidebar column."""
    state: DashboardState = request.app["state"]
    refused = _refuse_vocabulary_write(state, request, "chat.tag_column_create")
    if refused is not None:
        return refused
    # The create budget is charged further down, only on an actual append, so
    # an ``ensure`` retry that finds its twin (or an existing lane) costs nothing.
    rl_source, rl_caller = request_origin(request, what="tag column write", log=logger)
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    async with _tags_write_lock(state):
        column = _normalize_column(state, {**body, "order": len(state._tag_boards)})
        if column is None:
            return web.json_response(
                {"error": "invalid column payload", "code": "invalid_column_payload"}, status=400
            )
        if column["source"] == "state":
            # Creating a lane is an ENSURE, not an append: a caller asking for a
            # lane that already exists gets the existing one back with 200 rather
            # than a duplicate. This is what makes a retry (or two dashboards
            # racing) converge instead of persisting two identical lanes, and it
            # holds because the decision is made under the write lock.
            owner = _state_lane_owner(state, column["state_key"])
            if owner is not None:
                existing = dict(owner)
                sel().log_api_access(
                    caller=rl_caller,
                    operation="chat.tag_column_create",
                    outcome="allowed",
                    source=rl_source,
                    resources=f"{existing.get('id')} (existing lane {column['state_key']})",
                )
                return web.json_response(existing, status=200)
        elif body.get("ensure") is True and column["name"]:
            # Opt-in ENSURE for a named tag column, decided under the lock for
            # the reason the lane branch above is: a client-side "does it
            # exist" check reads a list that can be stale, so two agents
            # retrying the same create would both append. The board UI never
            # sends ``ensure`` (it adds unnamed columns on purpose), so its
            # append-always behaviour is unchanged.
            match = _tag_column_twin(state, column)
            if match is not None:
                sel().log_api_access(
                    caller=rl_caller,
                    operation="chat.tag_column_create",
                    outcome="allowed",
                    source=rl_source,
                    resources=f"{match.get('id')} (existing column)",
                )
                return web.json_response(dict(match), status=200)
        # The same per-caller create budget as a tag: the board is as shared and
        # as unbounded as the vocabulary, and the browser is exempt for the same
        # reason (a person clicking is not the loop this bounds).
        if rl_source != "dashboard" and not allow_create(TAG_COLUMN_CREATE, rl_caller):
            sel().log_api_access(
                caller=rl_caller,
                operation="chat.tag_column_create",
                outcome="denied",
                source=rl_source,
                error="create rate limited",
            )
            return web.json_response(
                {
                    "error": "too many board columns created recently; retry shortly",
                    "code": "create_rate_limited",
                },
                status=429,
            )
        column["id"] = uuid.uuid4().hex[:12]
        state._tag_boards.append(column)
        boards_snap = [dict(c) for c in state._tag_boards]
        try:
            await asyncio.to_thread(state.save_tag_boards_snapshot, boards_snap)
        except Exception:
            state._tag_boards = [c for c in state._tag_boards if c.get("id") != column["id"]]
            logger.warning("tag column create failed to persist: %s", column["id"])
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
    sel().log_api_access(
        caller=rl_caller,
        operation="chat.tag_column_create",
        outcome="allowed",
        source=rl_source,
        resources=str(column["id"]),
    )
    return web.json_response(column, status=201)


async def api_chat_tag_column_update(request: web.Request) -> web.Response:
    """PATCH /api/chat/tag-columns/{id} — rename / retag / reorder."""
    state: DashboardState = request.app["state"]
    refused = _refuse_vocabulary_write(state, request, "chat.tag_column_update")
    if refused is not None:
        return refused
    rl_source, rl_caller = request_origin(request, what="tag column write", log=logger)
    cid = request.match_info["id"]
    column = next((c for c in state._tag_boards if c.get("id") == cid), None)
    if not column:
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    async with _tags_write_lock(state):
        # Re-check under lock (column may have been deleted concurrently).
        column = next((c for c in state._tag_boards if c.get("id") == cid), None)
        if not column:
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)
        merged = _normalize_column(state, body, existing=column)
        if merged is None:
            return web.json_response(
                {"error": "invalid column payload", "code": "invalid_column_payload"}, status=400
            )
        if merged["source"] == "state":
            # Same uniqueness rule as create, but an update carries no ENSURE
            # intent -- silently collapsing it onto the existing lane would
            # discard the caller's other edits -- so this is a refusal.
            owner = _state_lane_owner(state, merged["state_key"], exclude_id=cid)
            if owner is not None:
                sel().log_api_access(
                    caller=rl_caller,
                    operation="chat.tag_column_update",
                    outcome="rejected",
                    source=rl_source,
                    resources=cid,
                    error=f"lane {merged['state_key']} already exists",
                )
                return web.json_response(
                    {"error": "lane already exists", "code": "duplicate_state_lane"}, status=409
                )
        original = dict(column)
        column.update(merged)
        boards_snap = [dict(c) for c in state._tag_boards]
        try:
            await asyncio.to_thread(state.save_tag_boards_snapshot, boards_snap)
        except Exception:
            column.clear()
            column.update(original)
            logger.warning("tag column update failed to persist: %s", cid)
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
    sel().log_api_access(
        caller=rl_caller,
        operation="chat.tag_column_update",
        outcome="allowed",
        source=rl_source,
        resources=cid,
    )
    return web.json_response(column)


async def api_chat_tag_column_delete(request: web.Request) -> web.Response:
    """DELETE /api/chat/tag-columns/{id} — remove a column."""
    state: DashboardState = request.app["state"]
    refused = _refuse_vocabulary_write(state, request, "chat.tag_column_delete")
    if refused is not None:
        return refused
    rl_source, rl_caller = request_origin(request, what="tag column write", log=logger)
    cid = request.match_info["id"]
    if not any(c.get("id") == cid for c in state._tag_boards):
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    async with _tags_write_lock(state):
        # Re-check under lock.
        removed = [c for c in state._tag_boards if c.get("id") == cid]
        if not removed:
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)
        state._tag_boards = [c for c in state._tag_boards if c.get("id") != cid]
        boards_snap = [dict(c) for c in state._tag_boards]
        try:
            await asyncio.to_thread(state.save_tag_boards_snapshot, boards_snap)
        except Exception:
            state._tag_boards.extend(removed)
            logger.warning("tag column delete failed to persist: %s", cid)
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
    sel().log_api_access(
        caller=rl_caller,
        operation="chat.tag_column_delete",
        outcome="allowed",
        source=rl_source,
        resources=cid,
    )
    return web.json_response({"ok": True})


async def api_chat_tag_columns_reorder(request: web.Request) -> web.Response:
    """PUT /api/chat/tag-columns/order — reorder columns by id list."""
    state: DashboardState = request.app["state"]
    refused = _refuse_vocabulary_write(state, request, "chat.tag_columns_reorder")
    if refused is not None:
        return refused
    rl_source, rl_caller = request_origin(request, what="tag column write", log=logger)
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    ids = body.get("ids")
    if not isinstance(ids, list):
        return web.json_response(
            {"error": "ids must be an array", "code": "ids_not_array"}, status=400
        )
    base_ids = body.get("base_ids")
    if base_ids is not None and not isinstance(base_ids, list):
        return web.json_response(
            {"error": "base_ids must be an array", "code": "ids_not_array"}, status=400
        )
    async with _tags_write_lock(state):
        if base_ids is not None:
            # Optional compare-and-set: a caller that composed ``ids`` from a read
            # names the order it read, so a reorder the person made in between is
            # refused rather than overwritten by a stale full list. The board UI
            # sends no ``base_ids`` and keeps its last-write-wins drag behaviour.
            current = [str(c.get("id")) for c in sorted(state._tag_boards, key=_order_key)]
            if [str(x) for x in base_ids] != current:
                return web.json_response(
                    {
                        "error": "the column order changed since it was read",
                        "code": "stale_base",
                    },
                    status=409,
                )
        original_order = [(col.get("id"), col.get("order", 0)) for col in state._tag_boards]
        order_map = {str(cid): i for i, cid in enumerate(ids)}
        # Push columns not present in the reorder payload past the explicit
        # ordering so they don't collide with the new sequential indices.
        next_order = len(order_map)
        for col in state._tag_boards:
            cid = col.get("id")
            if cid in order_map:
                col["order"] = order_map[cid]
            else:
                col["order"] = next_order
                next_order += 1
        state._tag_boards.sort(key=_order_key)
        boards_snap = [dict(c) for c in state._tag_boards]
        try:
            await asyncio.to_thread(state.save_tag_boards_snapshot, boards_snap)
        except Exception:
            # Restore original order.
            for col in state._tag_boards:
                for cid, order in original_order:
                    if col.get("id") == cid:
                        col["order"] = order
                        break
            state._tag_boards.sort(key=_order_key)
            logger.warning("tag columns reorder failed to persist")
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
    sel().log_api_access(
        caller=rl_caller,
        operation="chat.tag_columns_reorder",
        outcome="allowed",
        source=rl_source,
        resources=",".join(str(x) for x in ids[:10]),
    )
    return web.json_response({"ok": True})


# ── Drag-drop: move a session between columns (reassigns status tags) ──────


async def api_chat_slot_drop(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/drop — move a session into a column.

    Destination rule: if the target column's tag_ids contains exactly one
    *status* tag, strip every status tag from the slot and add that one.
    Non-status tags are preserved. Any other configuration is a no-op so users
    can have filter-only columns without accidental data loss. A derived state
    lane is refused outright — its membership follows the session's runtime
    state and no tag write can place a card there.

    The browser drag is one caller; the ``session_move_to_column`` MCP tool is
    the other. So the route applies the caller fences ``PUT /tags`` applies
    (unattributable caller, crew-member ownership, app ownership of the slot
    and its transcript), and a non-person caller is held to the same agent
    tag-grants policy, judged over the same diff: a drop is exactly the tag
    replace that caller could send to ``PUT /tags``. On top of that verdict the
    target must pass the ``chat_tag`` ``set_state`` rule — a row-backed
    workflow state with ``add-remove`` policy — so an agent never moves a card
    into a state it cannot move it out of. An optional ``base_tags_revision``
    makes the write compare-and-set, as on ``PUT /tags``. The person is never
    gated.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    # The three ownership fences ``api_chat_slot_tags`` applies, in the same
    # order and with the same indistinguishable 404. A caller whose tab closed
    # mid-call is refused first: its derived app would be "" and read as the
    # person. Lazy import for the same module-load cycle PUT /tags avoids.
    refused = refuse_unattributable_caller(state, request, "chat.slot_drop")
    if refused is not None:
        return refused
    from kiro_crew.dashboard.chat_folders import member_slot_write_refused

    refused = member_slot_write_refused(state, request, slot, "chat.slot_drop")
    if refused is not None:
        return refused
    request_app = effective_request_app(state, request)
    if (denied := deny_app_slot_access(request_app, slot, slot.key, "chat.slot_drop")) is not None:
        return denied
    # Capture the transcript key the lookup above just covered, BEFORE the
    # body-parse await — the same rebind window PUT /tags documents. The
    # re-check below and the save's expected_history_key pin together keep
    # this request's write on the transcript it was authorized against.
    authorized_history_key = slot_history_key(slot)
    if not app_owns_transcript(state._slots, request_app, authorized_history_key):
        audit_app_slot_denial(
            request_app, "chat.slot_drop", slot.key, "app does not own this slot's transcript"
        )
        return slot_not_found()
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    column_id = str(body.get("column_id") or "")
    # Optional slot generation, the token ``PATCH /pin`` reads: the MCP tool
    # resolves the slot from an earlier ``/api/chat/slots`` read, and the key
    # can be recreated for another conversation before this POST arrives. The
    # board sends none and keeps its behaviour.
    expected_created = str(body.get("expected_created") or "")
    # Optional compare-and-set precondition, the one ``PUT /tags`` reads: the
    # tags revision the caller saw when it resolved the slot. The drop strips
    # every status tag and keeps the rest, so a tag the person toggled after
    # that read would be judged and written on a list the caller never saw.
    # The board sends none and keeps its behaviour.
    raw_base = body.get("base_tags_revision")
    if raw_base is not None and not isinstance(raw_base, str):
        return web.json_response(
            {"error": "base_tags_revision must be a string", "code": "base_not_string"},
            status=400,
        )
    base_tags_revision: str | None = raw_base or None
    is_agent, origin_source, origin_caller, member_principal = slot_tag_write_principal(
        request, request_app, what="slot drop"
    )
    audit_caller = request_app or member_principal or origin_caller
    if is_agent:
        # Read the grants store off the event loop once, before the lock, so
        # the snapshot captured in-lock serves the installed cache.
        await asyncio.to_thread(refresh_cache)

    def _rejected(reason: str, code: str) -> web.Response:
        sel().log_api_access(
            caller=audit_caller,
            operation="chat.slot_drop",
            outcome="rejected",
            source=origin_source,
            resources=f"{name}->{column_id}",
            error=reason,
        )
        return web.json_response({"ok": False, "reason": reason, "code": code, "tags": slot.tags})

    # Serialize the whole resolve/re-check/mutate/persist/rollback span under
    # the same lock every other tags writer holds (PUT /tags, the tag-delete
    # strip, auto-tag). Two reasons, both review-caught: (1) with awaits
    # inside the span, a second concurrent writer would capture this one's
    # value as its rollback snapshot, and value-based rollback cannot tell
    # "my write survived" from "someone else wrote the same value" — a
    # refused drop could then erase an equal, acknowledged concurrent commit;
    # (2) the column and tag vocabulary must be RESOLVED under the lock too,
    # or a tag deletion completing while this request waits on the lock is
    # resurrected — the stale target id would be re-added and persisted onto
    # the slot after the vocabulary commit removed it.
    async with _tags_write_lock(state):
        # The one grants snapshot an agent drop is judged on, read FIRST so
        # the re-authorization below is the last thing before the write with
        # no await in between.
        snapshot = await asyncio.to_thread(capture_grants_snapshot) if is_agent else None
        column = next((c for c in state._tag_boards if c.get("id") == column_id), None)
        if not column:
            return web.json_response(
                {"error": "column not found", "code": "column_not_found"}, status=404
            )
        tag_index = {t["id"]: t for t in state._tags}
        if column.get("source") == "state":
            # A state lane's membership is derived from the session's own
            # runtime state, so there is nothing to write that would move the
            # card there — only the agent reaching that state moves it. Refuse
            # rather than silently reassigning tags the lane does not filter by.
            return _rejected("column is a derived state lane", "state_lane")
        col_tags = [tag_index[t] for t in column.get("tag_ids") or [] if t in tag_index]
        status_tags = [t for t in col_tags if t.get("status")]
        if len(status_tags) != 1:
            # Column doesn't carry exactly one LIVE status tag — there's no
            # unambiguous status to assign on drop, so this is a visual no-op
            # (covers the unfiltered, multi-status, and target-tag-deleted
            # cases alike; tag_index was built under the lock, so a tag
            # deleted while this request waited is already absent here).
            return _rejected("column is not a status lane", "not_status_lane")
        target_id = status_tags[0]["id"]
        # Re-authorize after the awaits above (body parse, lock acquisition):
        # same slot OBJECT still registered under the name, routing still on
        # the transcript captured before the first await, the slot generation
        # the caller resolved, and transcript ownership re-asked. No await
        # between this check and the save dispatch; the persist window itself
        # is covered by the save's pin.
        if (
            state._slots.get(name) is not slot
            or slot_history_key(slot) != authorized_history_key
            or (expected_created and slot.created_at != expected_created)
            or not app_owns_transcript(state._slots, request_app, authorized_history_key)
        ):
            return _rejected("session was deleted or rebound", "session_gone")
        if base_tags_revision is not None and base_tags_revision != slot.tags_revision:
            # Decided under the lock every tags writer holds, like PUT /tags:
            # nothing is written and the caller re-reads the session's tags.
            sel().log_api_access(
                caller=audit_caller,
                operation="chat.slot_drop",
                outcome="rejected",
                source=origin_source,
                resources=f"{name}->{column_id}",
                error="stale base revision",
            )
            return web.json_response(
                {
                    "error": "tags changed since the session was read",
                    "code": "stale_base",
                    "base_tags_revision": base_tags_revision,
                    "tags_revision": slot.tags_revision,
                    "tags": slot.tags,
                },
                status=409,
            )
        kept = [t for t in slot.tags if t in tag_index and not tag_index[t].get("status")]
        prior_tags = slot.tags
        written_tags = kept + [target_id]
        if snapshot is not None:
            # The same verdict PUT /tags gives this caller for the same diff,
            # over the one grants snapshot read above. A refusal writes nothing.
            refusal = agent_slot_tags_refusal(state, list(prior_tags), written_tags, snapshot)
            if refusal is None:
                # Then the ``chat_tag`` ``set_state`` rule on the target: a
                # drop moves a card INTO a workflow state, and an agent may only
                # enter a state it can also leave. ``PUT /tags`` admits an
                # ``add-only`` target, which would park the card where no agent
                # move can take it out again. Status and policy both come from
                # the protected row, never from the agent-writable vocabulary.
                target_policy, target_is_status = snapshot.grant(target_id)
                if not target_is_status:
                    refusal = ("not_a_status_tag", target_id)
                elif target_policy != "add-remove":
                    refusal = ("tag_policy_denied", target_id)
            if refusal is not None:
                code, tag_id = refusal
                sel().log_api_access(
                    caller=audit_caller,
                    operation="chat.slot_drop",
                    outcome="denied",
                    source="app_isolation",
                    resources=f"slot={slot.key}" + (f" tag={tag_id}" if tag_id else ""),
                    error=code,
                )
                return web.json_response(
                    {"error": f"{code}:{tag_id}" if tag_id else code, "code": code},
                    status=403,
                )
        slot.tags = written_tags
        written_tags_revision = _bump_slot_tags_revision(slot)
        if not await save_slot_off_loop(
            state, slot, force=True, expected_history_key=authorized_history_key
        ):
            # Refused without writing: the session was permanently deleted or
            # rebound mid-persist. Roll back the live field — but only while
            # it still holds THIS request's value, so a non-endpoint writer's
            # newer commit is not erased — and report the drop as rejected,
            # matching this endpoint's rejection shape (the card stays where
            # it was).
            if slot.tags == written_tags and slot.tags_revision == written_tags_revision:
                slot.tags = prior_tags
                # Fresh revision, not prior_tags_revision (see api_chat_slot_tags):
                # a client that adopted the leaked provisional revision treats
                # the prior one as a known predecessor and would keep the
                # rejected tags.
                _bump_slot_tags_revision(slot)
            # The UNPINNED periodic flush may have persisted the provisional
            # value while this save awaited (review-caught): mark dirty so the
            # next flush reconverges the durable record to the live state.
            slot._dirty = True
            state.push_slots_update()
            return _rejected("session was deleted or rebound", "session_gone")
    state.push_slots_update()
    sel().log_api_access(
        caller=audit_caller,
        operation="chat.slot_drop",
        outcome="allowed",
        source=origin_source,
        resources=f"{name}->{column_id}",
    )
    return web.json_response({"ok": True, "tags": slot.tags})


if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.chat_tags_policy import (  # noqa: F401
        _AGENT_TAG_POLICIES,
        _COLOR_RE,
        _DEFAULT_COLOR,
        agent_tag_change_refusal,
        agent_tag_grant,
        agent_tag_policy,
        resolve_board_tags,
    )
    from kiro_crew.dashboard.chat_tags_store import (  # noqa: F401
        _TAGS_WRITE_LOCKS,
        TagGrantStoreBlocked,
        _reconcile_failed_tag_create,
        _tag_by_id,
        _valid_color,
        _write_tags_snapshot,
        create_tag_definition,
        create_tag_definition_off_loop,
        persist_tags_snapshot_unlocked,
        tags_write_lock,
        validate_folder_tag_ids,
    )
    from kiro_crew.dashboard.chat_tags_vocabulary import (  # noqa: F401
        _audit_tag_update_denied,
        _project_agent_tag,
        api_chat_tag_adopt,
        api_chat_tag_create,
        api_chat_tag_delete,
        api_chat_tag_update,
        api_chat_tags,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew.dashboard.body_numbers import (  # noqa: F401
        body_int,
    )
    from kiro_crew.dashboard.chat_tag_grants import (  # noqa: F401
        GrantsSnapshot,
        has_grant_row,
        is_grantable_tag_id,
        mint_grant,
        resolve_grant,
        resolve_grant_record,
        revoke_grant,
        store_write_blocked,
    )
    from kiro_crew.dashboard.create_rate_limit import (  # noqa: F401
        TAG_CREATE,
    )
    from kiro_crew.dashboard.handlers._shared import (  # noqa: F401
        _owner_denial_response,
    )
    from kiro_crew.dashboard.handlers.source_providers import (  # noqa: F401
        is_owner_dashboard_request,
    )
    from kiro_crew.dashboard.state import (  # noqa: F401
        mint_tags_revision,
    )
    from kiro_crew.dashboard.token_auth import (  # noqa: F401
        MEMBER_CHAT_PRINCIPAL_KEY,
    )
    from kiro_crew.loop_lock import (  # noqa: F401
        LoopBoundLock,
    )
    from kiro_crew.security import (  # noqa: F401
        redact_credentials,
        redact_exfiltration_urls,
    )

# This module is the facade of the session tags: every name is reached through it as
# ``kiro_crew.dashboard.chat_tags.<name>``. A read is served by the owner that binds the name, and
# a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner holding it
# (:mod:`kiro_crew._facade_mirror`). Owners never import this module, and they log under
# its name. Slot tag assignment, the sidebar columns and the column drop stay here. New
# work goes to the owner of its responsibility:
#
#   ``chat_tags_policy`` -- the agent tag policy and the tags a board shows
#   ``chat_tags_store`` -- the vocabulary's write lock, snapshot and new definitions
#   ``chat_tags_vocabulary`` -- the tag vocabulary endpoints
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.chat_tags_policy",
    "kiro_crew.dashboard.chat_tags_store",
    "kiro_crew.dashboard.chat_tags_vocabulary",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
