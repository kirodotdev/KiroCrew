"""The tag vocabulary endpoints: list, create, adopt, update and delete a tag.

``GET/POST /api/chat/tags`` and the per-tag adopt, update and delete routes, with the
refusals an agent's or app's write to the vocabulary meets and their audit. A new
vocabulary route goes here.

Served through :mod:`kiro_crew.dashboard.chat_tags`, which callers import and tests
patch; its mirror (:mod:`kiro_crew._facade_mirror`) carries such a patch to this module,
and this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.body_numbers import body_int
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_tag_grants import (
    has_grant_row,
    is_grantable_tag_id,
    mint_grant,
    refresh_cache,
    resolve_grant,
    resolve_grant_record,
    revoke_grant,
    store_write_blocked,
)
from kiro_crew.dashboard.chat_tags_policy import _AGENT_TAG_POLICIES, _DEFAULT_COLOR, _NAME_MAX
from kiro_crew.dashboard.chat_tags_store import (
    TagGrantStoreBlocked,
    _bump_slot_tags_revision,
    _order_key,
    _tag_by_id,
    _tags_write_lock,
    _valid_color,
    _write_tags_snapshot,
    create_tag_definition_off_loop,
)
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.create_rate_limit import TAG_CREATE, allow_create
from kiro_crew.dashboard.handlers._shared import _owner_denial_response, read_bounded_json
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.token_auth import (
    MEMBER_CHAT_PRINCIPAL_KEY,
    effective_request_app,
    refuse_unattributable_caller,
    request_origin,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.dashboard.chat_tags")


# ── Tag vocabulary ─────────────────────────────────────────────────────────


def _project_agent_tag(tag: dict[str, Any], degraded: str | None) -> dict[str, Any]:
    """Copy one vocabulary row and attach protected-store policy metadata."""
    row = dict(tag)
    raw_id = tag.get("id")
    tag_id = raw_id if isinstance(raw_id, str) else ""
    policy, _status, provenanced = resolve_grant_record(tag_id)
    row["agent"] = policy
    row["agent_provenanced"] = provenanced
    row["agent_store_degraded"] = degraded is not None
    return row


async def api_chat_tags(request: web.Request) -> web.Response:
    """GET /api/chat/tags — list definitions with protected policy metadata.

    ``agent_store_degraded`` carries the boolean WRITE gate, not the applier's
    reduced-grants signal: the dashboard disables every policy control and
    the owner's adopt action while it is true, and adoption is the recovery a
    boot quarantine points at, so a verified reseed must read as healthy here.
    """
    state: DashboardState = request.app["state"]
    await asyncio.to_thread(refresh_cache)
    degraded = store_write_blocked()
    rows = [_project_agent_tag(tag, degraded) for tag in sorted(state._tags, key=_order_key)]
    return web.json_response(rows)


def _refuse_vocabulary_write(
    state: DashboardState, request: web.Request, operation: str
) -> web.Response | None:
    """The refusals every write to the SHARED tag vocabulary applies first.

    Tags are one vocabulary with no owner: a folder an app or member creates is
    that principal's own (``chat_folders._folder_owner_app``), but a tag it coins
    or renames lands in the person's list with nothing to tell it apart. So NO
    non-person principal may write the vocabulary -- neither an app nor an
    admitted crew member. A member still READS the vocabulary (``GET
    /api/chat/tags``) and ASSIGNS existing tags to its own or created sessions
    (``PUT /api/chat/slots/{slot}/tags``); it just cannot coin, rename or delete
    the shared labels. Decided HERE, on the middleware's validated claim (app)
    and the gate's stamped principal (member), so the rule holds for every
    transport -- the ``chat_tag_create`` / ``chat_tag_update`` MCP tools included
    -- rather than only where a tool layer chooses to restate it. The
    unattributable-caller refusal (:func:`token_auth.refuse_unattributable_caller`)
    runs first, for the reason its docstring gives.

    The board's column layout (``/api/chat/tag-columns``) is the same kind of
    shared, ownerless state -- a column an app coined would sit in the person's
    board with nothing to tell it apart -- so its four write handlers (create,
    update, delete, reorder) apply this refusal too, and the
    ``chat_tag_column_*`` MCP tools rely on it.

    Returns the refusal response, or ``None`` when the write may proceed.
    """
    refused = refuse_unattributable_caller(state, request, operation)
    if refused is not None:
        return refused
    request_app = effective_request_app(state, request)
    if request_app:
        sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            error="apps cannot write shared tags",
        )
        return web.json_response(
            {"error": "apps cannot write shared tags", "code": "app_forbidden"}, status=403
        )
    # A crew member carries no app claim, so the app guard above is a no-op for
    # it. The gate stamped its verified principal; refuse a vocabulary write the
    # same way, since the shared-list argument is identical for a member.
    member_principal = str(request.get(MEMBER_CHAT_PRINCIPAL_KEY) or "")
    if member_principal.startswith("member:"):
        sel().log_api_access(
            caller=member_principal,
            operation=operation,
            outcome="denied",
            source="member_isolation",
            error="members cannot write shared tags",
        )
        return web.json_response(
            {"error": "agents cannot write shared tags", "code": "app_forbidden"}, status=403
        )
    return None


async def api_chat_tag_create(request: web.Request) -> web.Response:
    """POST /api/chat/tags — create a new tag."""
    state: DashboardState = request.app["state"]
    refused = _refuse_vocabulary_write(state, request, "chat.tag_create")
    if refused is not None:
        return refused
    # Rate is the property that matters for a verb an agent may call in a loop:
    # the same per-caller budget ``api_chat_folder_create`` applies, keyed by the
    # validated component name so it cannot be varied to escape the bucket.
    # The browser is exempt — a person clicking is not the loop this bounds.
    rl_source, rl_caller = request_origin(request, what="tag write", log=logger)
    if rl_source != "dashboard" and not allow_create(TAG_CREATE, rl_caller):
        sel().log_api_access(
            caller=rl_caller,
            operation="chat.tag_create",
            outcome="denied",
            source=rl_source,
            error="create rate limited",
        )
        return web.json_response(
            {
                "error": "too many tags created recently; retry shortly",
                "code": "create_rate_limited",
            },
            status=429,
        )
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # Redact credential / exfiltration patterns from the user-supplied name
    # BEFORE truncation — truncating first can slice a credential that
    # straddles the length cut into a fragment the scanners cannot
    # recognize, persisting a raw prefix. Same boundary pattern as other
    # LLM-authored sinks.
    name = (body.get("name") or "").strip()
    name, _ = redact_exfiltration_urls(name)
    name, _ = redact_credentials(name)
    name = name.strip()[:_NAME_MAX]
    if not name:
        return web.json_response({"error": "name required", "code": "name_required"}, status=400)
    color = str(body.get("color") or _DEFAULT_COLOR)
    # Strict boolean (same reasoning as the PATCH handler): a string "false"
    # coerces truthy and would mint agent authority via the status default.
    status_raw = body.get("status", False)
    if not isinstance(status_raw, bool):
        return web.json_response({"error": "invalid status", "code": "invalid_status"}, status=400)
    status = status_raw
    record_protected_identity = rl_source == "dashboard" and is_owner_dashboard_request(request)
    try:
        tag = await create_tag_definition_off_loop(
            state,
            name,
            color,
            status=status,
            record_protected_identity=record_protected_identity,
        )
    except Exception as exc:
        logger.warning("tag create failed to persist: %s", name)
        # A closed grant-store write gate is a refused permission decision; any
        # other failure is an errored write. Both reach the SEL like the
        # sibling PATCH and adoption refusals.
        if isinstance(exc, TagGrantStoreBlocked):
            outcome, reason = "denied", f"grant store {exc.blocked}"
        else:
            outcome, reason = "error", "persist failed"
        sel().log_api_access(
            caller=rl_caller,
            operation="chat.tag_create",
            outcome=outcome,
            source=rl_source,
            error=reason,
        )
        return web.json_response({"error": "persist failed", "code": "persist_failed"}, status=500)
    state.push_slots_update()
    sel().log_api_access(
        caller=rl_caller,
        operation="chat.tag_create",
        outcome="allowed",
        source=rl_source,
        resources=str(tag["id"]),
    )
    return web.json_response(tag, status=201)


async def api_chat_tag_adopt(request: web.Request) -> web.Response:
    """POST /api/chat/tags/{id}/adopt — record protected identity only."""
    state: DashboardState = request.app["state"]
    tid = request.match_info["id"]
    source, caller = request_origin(request, what="tag adoption", log=logger)
    member_principal = str(request.get(MEMBER_CHAT_PRINCIPAL_KEY) or "")

    if (
        source != "dashboard"
        or member_principal.startswith("member:")
        or not is_owner_dashboard_request(request)
    ):
        sel().log_api_access(
            caller=caller,
            operation="chat.tag_adopt",
            outcome="denied",
            source="owner_only",
            resources=tid,
            error="tag adoption requires the dashboard owner",
        )
        # The shared owner-gate tail: a signed pre-owner session gets the
        # 401 re-sign-in relabel instead of a dead-end 403.
        return _owner_denial_response(
            request,
            "Only the dashboard owner can allow agents to use this tag.",
            "owner_required",
        )

    def _audit_refused(reason: str, outcome: str = "denied") -> None:
        # Every adoption outcome is a decision over agent authority, so each
        # refusal and failure past the owner check leaves an SEL record too.
        sel().log_api_access(
            caller=caller,
            operation="chat.tag_adopt",
            outcome=outcome,
            source="dashboard",
            resources=tid,
            error=reason,
        )

    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        _audit_refused("unreadable request body")
        return body_err
    assert body is not None
    if set(body) != {"status"}:
        _audit_refused("request carries fields other than status")
        return web.json_response(
            {
                "error": "This action accepts only the tag's current status.",
                "code": "invalid_adoption",
            },
            status=400,
        )
    status = body["status"]
    if not isinstance(status, bool):
        _audit_refused("status is not a boolean")
        return web.json_response(
            {"error": "Tag status must be true or false.", "code": "invalid_status"},
            status=400,
        )
    if not is_grantable_tag_id(tid):
        _audit_refused("tag id is not grantable")
        return web.json_response(
            {
                "error": "This tag cannot be enabled for agents.",
                "code": "tag_id_not_grantable",
            },
            status=400,
        )

    async with _tags_write_lock(state):
        tag = _tag_by_id(state, tid)
        if tag is None:
            _audit_refused("tag not found")
            return web.json_response({"error": "Tag not found.", "code": "not_found"}, status=404)
        current_status = tag.get("status", False)
        # A legacy row can carry a non-boolean status that PATCH refuses to
        # touch until the tag is adopted; the owner's explicit boolean here is
        # its correction, so adoption normalizes it instead of refusing.
        normalize_status = not isinstance(current_status, bool)
        if not normalize_status and current_status is not status:
            _audit_refused("tag status changed since it was shown")
            return web.json_response(
                {
                    "error": "The tag changed. Review it and try adoption again.",
                    "code": "tag_changed",
                },
                status=409,
            )

        await asyncio.to_thread(refresh_cache)
        degraded = store_write_blocked()
        if degraded is not None:
            logger.warning("tag adoption: permission data unavailable (%s) for %s", degraded, tid)
            _audit_refused(f"grant store {degraded}")
            return web.json_response(
                {
                    "error": "Agent permissions cannot be changed right now.",
                    "code": "persist_failed",
                },
                status=500,
            )
        if normalize_status:
            # The vocabulary write goes FIRST, before any identity exists: the
            # owner's explicit boolean corrects a legacy non-boolean status and
            # grants nothing on its own. If it fails, nothing protected was
            # written, so the 5xx leaves the tag rowless with its adopt action.
            # If the mint below then fails, the tag is a rowless legacy tag with
            # a valid boolean status, which adoption handles on retry.
            pre_snapshot = [dict(t) for t in state._tags]
            tag["status"] = status
            try:
                await asyncio.to_thread(_write_tags_snapshot, state, [dict(t) for t in state._tags])
            except Exception:
                state._tags = pre_snapshot
                logger.warning("tag adoption: status normalization failed for %s", tid)
                _audit_refused("status normalization failed", outcome="error")
                return web.json_response(
                    {
                        "error": "Agents could not be enabled for this tag.",
                        "code": "persist_failed",
                    },
                    status=500,
                )

        _policy, _recorded_status, row_exists = resolve_grant_record(tid)
        if not row_exists:
            try:
                await asyncio.to_thread(mint_grant, tid, policy="none", status=status)
            except Exception:
                await asyncio.to_thread(refresh_cache)
                policy, recorded_status, row_exists = resolve_grant_record(tid)
                committed = (
                    store_write_blocked() is None
                    and row_exists
                    and (policy, recorded_status) == ("none", status)
                )
                if not committed:
                    logger.warning("tag adoption: identity mint failed for %s", tid, exc_info=True)
                    _audit_refused("identity mint failed", outcome="error")
                    return web.json_response(
                        {
                            "error": "Agents could not be enabled for this tag.",
                            "code": "persist_failed",
                        },
                        status=500,
                    )

        response_row = _project_agent_tag(tag, None)

    sel().log_api_access(
        caller=caller,
        operation="chat.tag_adopt",
        outcome="allowed",
        source="dashboard",
        resources=tid,
    )
    return web.json_response(response_row)


def _audit_tag_update_denied(
    request: web.Request, tid: str, reason: str, *, outcome: str = "denied"
) -> None:
    """Record a refused or failed status/policy PATCH on the SEL.

    The provenance and grant-store refusals are permission decisions over agent
    authority, reached by every rowless legacy tag, and a failed grant write is
    an outcome of one, so each leaves the same audit trail as an allowed update
    (``outcome="error"`` for a failed write). ``request_origin`` supplies the
    interface ``source`` and the validated caller (browser or a named MCP
    component).
    """
    source, caller = request_origin(request, what="tag update", log=logger)
    sel().log_api_access(
        caller=caller,
        operation="chat.tag_update",
        outcome=outcome,
        source=source,
        resources=tid,
        error=reason,
    )


async def api_chat_tag_update(request: web.Request) -> web.Response:
    """PATCH /api/chat/tags/{id} — rename / recolor / reorder.

    Resolution of the tag dict happens INSIDE the write lock so a concurrent
    DELETE cannot remove the tag between lookup and mutation (preventing a
    silent lost update on a detached dict).
    """
    state: DashboardState = request.app["state"]
    tid = request.match_info["id"]
    refused = _refuse_vocabulary_write(state, request, "chat.tag_update")
    if refused is not None:
        return refused
    # Early unlocked check for fast 404 on obviously invalid ids (avoids
    # JSON parse + lock contention for non-existent tags).
    if not _tag_by_id(state, tid):
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    if "name" in body:
        new_name = str(body["name"]).strip()[:_NAME_MAX]
        if not new_name:
            return web.json_response(
                {"error": "name required", "code": "name_required"}, status=400
            )

    if "agent" in body:
        # The authenticated replacement for the hand-edited ``agent`` field the
        # grants store retired: PATCH is now the only way to set a per-tag
        # agent policy, and it lands in the protected store, not tags.json.
        if not isinstance(body["agent"], str) or body["agent"] not in _AGENT_TAG_POLICIES:
            return web.json_response(
                {"error": "invalid agent policy", "code": "invalid_agent_policy"}, status=400
            )

    if "status" in body and not isinstance(body["status"], bool):
        # Strict boolean only, validated BEFORE the lock and any mutation:
        # bool("false") is True, so coercing a string would mint
        # workflow-state identity — and through the grant transition below,
        # agent authority — from malformed input.
        # Rejecting AFTER the name/color assignments would leave those
        # rejected mutations live in memory, so every
        # field is validated before the first write to ``tag``.
        return web.json_response({"error": "invalid status", "code": "invalid_status"}, status=400)

    async with _tags_write_lock(state):
        # Re-resolve under lock — a concurrent DELETE may have removed it.
        tag = _tag_by_id(state, tid)
        if not tag:
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)

        pre_snapshot = [dict(t) for t in state._tags]

        if "name" in body:
            # Redact BEFORE truncation (same reasoning as tag create).
            safe_name = str(body["name"]).strip()
            safe_name, _ = redact_exfiltration_urls(safe_name)
            safe_name, _ = redact_credentials(safe_name)
            safe_name = safe_name.strip()[:_NAME_MAX]
            if not safe_name:
                # Parity with create: never persist an empty name (e.g. the
                # whole input was a redacted credential).
                return web.json_response(
                    {"error": "name required", "code": "name_required"}, status=400
                )
            tag["name"] = safe_name
        if "color" in body:
            tag["color"] = _valid_color(str(body["color"]))
        if "order" in body:
            try:
                tag["order"] = body_int(body["order"])
            except (TypeError, ValueError):
                pass
        if "status" in body:
            # Validated strictly-boolean BEFORE the lock (see above).
            tag["status"] = body["status"]

        # ── Grant transition (protected store) ──────────────────────────────
        # An explicit ``agent`` value wins. Otherwise enabling status preserves
        # the existing policy, while disabling status resets policy to ``none``.
        # Every transition requires a healthy identity row, so a rowless legacy or
        # hand-planted tag stays human-only until the dashboard owner explicitly
        # adopts it. ``agent: "none"`` and status removal keep the row;
        # revocation is reserved for deletion.
        #
        # ``prev_grant`` is captured from one immutable snapshot before any
        # write. It supplies both the inherited policy and the compensation
        # value if a later durable step fails.
        prev_grant: tuple[str, bool] = ("none", False)
        prev_row_exists = False
        grant_store_problem: str | None = None
        if "agent" in body or "status" in body:
            await asyncio.to_thread(refresh_cache)
            prev_policy, prev_status, prev_row_exists = resolve_grant_record(tid)
            prev_grant = (prev_policy, prev_status)
            grant_store_problem = store_write_blocked()
        grant_mint: str | None = None
        if "agent" in body:
            grant_mint = body["agent"]
        elif "status" in body:
            grant_mint = prev_grant[0] if body["status"] else "none"
        if grant_mint is not None and (
            not is_grantable_tag_id(tid) or (grant_store_problem is None and not prev_row_exists)
        ):
            # Closed syntax constrains the trusted rail but does not prove who
            # created an id. A grammar-valid row in agent-writable tags.json is
            # still untrusted until dashboard create records protected identity.
            state._tags = pre_snapshot
            _audit_tag_update_denied(request, tid, "tag has no protected identity")
            return web.json_response(
                {
                    # Name the tag manager's own adoption control
                    # (``components.tagManagerList.adopt_tag``) so the reason
                    # points at a button that exists.
                    "error": "This tag is not set up for agents yet. "
                    "Choose Set up agent permissions first.",
                    "code": "tag_id_not_grantable",
                },
                status=400,
            )
        if grant_mint is not None and grant_store_problem is not None:
            state._tags = pre_snapshot
            logger.warning("tag update: grant store %s for %s", grant_store_problem, tid)
            _audit_tag_update_denied(request, tid, f"grant store {grant_store_problem}")
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )

        # The status bit comes from validated PATCH input when present and from
        # the protected row otherwise, never from agent-writable tags.json.
        mint_status = bool(body["status"]) if "status" in body else prev_grant[1]

        async def _restore_prev_grant() -> None:
            # Best-effort compensation restores the pre-PATCH row after a
            # downstream failure. Every transition requires an existing row,
            # including a policy-none identity row.
            if grant_mint is None:
                return
            try:
                await asyncio.to_thread(mint_grant, tid, policy=prev_grant[0], status=prev_grant[1])
            except Exception:
                logger.warning("tag update: grant restore failed for %s", tid, exc_info=True)

        if grant_mint is not None:
            # A process kill between the two stores must fail closed on
            # authority while retaining identity. Downgrade before committing
            # the vocabulary; the final mint is an upsert on the same row.
            try:
                await asyncio.to_thread(mint_grant, tid, policy="none", status=mint_status)
            except Exception:
                state._tags = pre_snapshot
                logger.warning("tag update: grant downgrade failed for %s", tid, exc_info=True)
                _audit_tag_update_denied(request, tid, "grant downgrade failed", outcome="error")
                return web.json_response(
                    {"error": "persist failed", "code": "persist_failed"}, status=500
                )

        snapshot = [dict(t) for t in state._tags]
        try:
            await asyncio.to_thread(_write_tags_snapshot, state, snapshot)
        except Exception:
            state._tags = pre_snapshot
            await _restore_prev_grant()
            logger.warning("tag update failed to persist: %s", tid)
            if grant_mint is not None:
                _audit_tag_update_denied(request, tid, "vocabulary persist failed", outcome="error")
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
        if grant_mint is not None:
            try:
                await asyncio.to_thread(mint_grant, tid, policy=grant_mint, status=mint_status)
            except Exception:
                # A writer can commit before surfacing a late I/O error. Resolve
                # the durable row before compensating: restoring the prior row
                # here would discard a replacement that already committed.
                await asyncio.to_thread(refresh_cache)
                committed_policy, committed_status, committed_row = resolve_grant_record(tid)
                mint_committed = (
                    store_write_blocked() is None
                    and committed_row
                    and (committed_policy, committed_status) == (grant_mint, mint_status)
                )
                if mint_committed:
                    logger.warning(
                        "tag update: grant mint reported failure after commit for %s", tid
                    )
                else:
                    await _restore_prev_grant()
                    state._tags = pre_snapshot
                    try:
                        await asyncio.to_thread(
                            _write_tags_snapshot, state, [dict(t) for t in pre_snapshot]
                        )
                    except Exception:
                        logger.warning("tag update: vocab rollback persist failed for %s", tid)
                    logger.warning("tag update: grant mint failed for %s", tid, exc_info=True)
                    _audit_tag_update_denied(request, tid, "grant mint failed", outcome="error")
                    return web.json_response(
                        {"error": "persist failed", "code": "persist_failed"}, status=500
                    )
        updated = tag

    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_update",
        outcome="allowed",
        source="dashboard",
        resources=tid,
    )
    return web.json_response(updated)


async def api_chat_tag_delete(request: web.Request) -> web.Response:
    """DELETE /api/chat/tags/{id} — delete a tag; strip it from all slots.

    Holds the per-state tag-write lock across the ENTIRE sequence so a
    concurrent tag_session directive cannot resolve the tag between the
    vocabulary removal and the slot strip.

    ORDERING: the grant is revoked FIRST (abort on failure — a deleted
    vocabulary row whose grant survives would let an agent resurrect the
    authority by re-creating the id in agent-writable tags.json), then the
    vocabulary (``tags.json``) is persisted as the single durable commit.
    A vocabulary-persist failure re-mints the captured grant (best-effort)
    and rolls memory back. A crash between revoke and persist leaves the tag
    present but closed — fail-closed, never stale-open. A crash after the
    vocabulary commit leaves only dangling tag ids on slots/boards, which
    are harmless and pruned on the next load (see ``_prune_unknown_tag_ids``).
    """
    state: DashboardState = request.app["state"]
    tid = request.match_info["id"]
    if not _tag_by_id(state, tid):
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)

    async with _tags_write_lock(state):
        # Re-check under lock — another concurrent delete may have won.
        removed_tag = _tag_by_id(state, tid)
        if not removed_tag:
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)

        # ── Revoke FIRST, abort on failure ────────────────────────────────
        # Deleting the vocabulary row while its grant survives lets an agent
        # re-create the same id in agent-writable tags.json and inherit the
        # stale authority after reload. Revoking before
        # the vocabulary commit fails closed: if the revoke fails, nothing
        # has changed and the DELETE aborts; if the vocabulary persist then
        # fails, the captured grant is re-minted (best-effort) so a failed
        # DELETE is a no-op on authority too.
        await asyncio.to_thread(refresh_cache)
        prev_grant = resolve_grant(tid)
        prev_row_exists = has_grant_row(tid)
        try:
            await asyncio.to_thread(revoke_grant, tid)
        except Exception:
            logger.warning("tag delete: grant revoke failed for %s", tid, exc_info=True)
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )

        # ── Single durable commit: remove from vocabulary and persist ────
        state._tags = [t for t in state._tags if t.get("id") != tid]
        snapshot = [dict(t) for t in state._tags]
        try:
            await asyncio.to_thread(_write_tags_snapshot, state, snapshot)
        except Exception:
            # Restore memory AND the revoked grant, then abort. Existence-
            # keyed: a ("none", False) row is protected state too.
            state._tags.append(removed_tag)
            if prev_row_exists:
                try:
                    await asyncio.to_thread(
                        mint_grant, tid, policy=prev_grant[0], status=prev_grant[1]
                    )
                except Exception:
                    logger.warning("tag delete: grant restore failed for %s", tid, exc_info=True)
            logger.warning("tag delete: vocab persist failed for %s", tid)
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )

        # ── Best-effort cleanup: strip the (now nonexistent) id ──────────
        # Failures here are tolerable: a dangling id on disk is pruned on
        # the next load; mark the slot dirty so the periodic flush retries.
        # The grant row was already revoked BEFORE the vocabulary commit
        # (see above) — deletion must never outlive the authority it removes.
        for slot in state._slots.values():
            if tid in slot.tags:
                # Pin the write to the transcript this iteration's membership
                # check covered: the save awaits inside the loop, so a rebind
                # can land mid-persist and the save would otherwise resolve
                # its target from the moved routing at write time. No await
                # between this capture and the strip below.
                authorized_history_key = slot_history_key(slot)
                slot.tags = [t for t in slot.tags if t != tid]
                _bump_slot_tags_revision(slot)
                try:
                    applied = await save_slot_off_loop(
                        state,
                        slot,
                        force=True,
                        best_effort=False,
                        expected_history_key=authorized_history_key,
                    )
                except Exception:
                    slot._dirty = True
                    logger.warning(
                        "tag delete: slot strip persist failed for %s; "
                        "marked dirty for periodic-flush retry",
                        getattr(slot, "key", "?"),
                        exc_info=True,
                    )
                else:
                    if not applied:
                        # Refused without writing (session permanently deleted
                        # or rebound mid-persist). The in-memory strip stands —
                        # the id is already gone from the vocabulary — so mark
                        # dirty and let the periodic flush persist wherever the
                        # slot now routes; a dangling id left on the old
                        # transcript is pruned on the next load.
                        slot._dirty = True
                        logger.warning(
                            "tag delete: slot strip save refused for %s "
                            "(session deleted or rebound); marked dirty for "
                            "periodic-flush retry",
                            getattr(slot, "key", "?"),
                        )

        # ── Best-effort cleanup: strip the deleted id from folders ───────
        # A folder can carry tags (copied onto new chats filed into it); the
        # deleted id must not linger there. Best-effort like the slot strip:
        # mutate_folders takes its own store lock (distinct from the tag-write
        # lock held here, so no re-entrancy), and a failure leaves a dangling id
        # that the next folder load ignores. The callback returns
        # (changed, None) so the store is rewritten only when something changed.
        def _strip_folder_tag(folders: list[dict]) -> tuple[bool, None]:
            changed = False
            for f in folders:
                tags = f.get("tags")
                if isinstance(tags, list) and tid in tags:
                    stripped = [t for t in tags if t != tid]
                    if stripped:
                        f["tags"] = stripped
                    else:
                        # Empty clears the key, holding "absent means no tags".
                        f.pop("tags", None)
                    changed = True
            return changed, None

        try:
            await state.mutate_folders(_strip_folder_tag)
        except Exception:
            # Dangling folder reference — pruned on next load.
            logger.warning("tag delete: folder strip persist failed for %s", tid, exc_info=True)

        # Strip from sidebar columns (flat list of column dicts).
        changed_boards = False
        for col in state._tag_boards:
            tag_ids = col.get("tag_ids") or []
            filtered = [t for t in tag_ids if t != tid]
            if len(filtered) != len(tag_ids):
                col["tag_ids"] = filtered
                changed_boards = True
        if changed_boards:
            try:
                boards_snapshot = [dict(c) for c in state._tag_boards]
                await asyncio.to_thread(state.save_tag_boards_snapshot, boards_snapshot)
            except Exception:
                # Dangling board reference — pruned on next load.
                logger.warning("tag delete: board strip persist failed for %s", tid, exc_info=True)

    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_delete",
        outcome="allowed",
        source="dashboard",
        resources=tid,
    )
    return web.json_response({"ok": True})
