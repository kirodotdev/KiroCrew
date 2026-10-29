"""The ``/api/chat/folders`` routes: list, create, update, reorder and delete.

New folder-tree routes go here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.body_numbers import body_int
from kiro_crew.dashboard.chat_folders_icons import (
    _CHAT_FOLDER_ICON_EPOCHS,
    _CHAT_FOLDER_PENDING_ICON_TASKS,
    _bump_icon_epoch,
    _is_single_emoji,
    _is_valid_folder_color,
    _spawn_chat_folder_icon_task,
)
from kiro_crew.dashboard.chat_folders_paths import (
    _folder_project_overlap_denied,
    _refuse_principal_steering_dirs,
    _validate_project_dir,
    _validate_steering_dirs,
)
from kiro_crew.dashboard.chat_folders_records import (
    CREATED_BY_SESSION,
    MAX_CHAT_FOLDERS,
    FolderCapError,
    FolderCreateError,
    FolderNameExistsError,
    FolderOwnershipError,
    _audit_origin,
    _caller_reaches_a_channel,
    _folder_history_counts,
    _folders_with_history_counts,
    _refuse_unattributable_caller,
    _slot_home_folder_id,
    _validate_folder_tags,
    agent_creator_key,
    caller_home_slot,
    create_folder_record,
)
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_tags import tags_write_lock
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.create_rate_limit import FOLDER_CREATE, allow_create
from kiro_crew.dashboard.folder_ownership import (
    _folder_owner_app,
    _is_descendant,
    _subtree_holds_foreign_folder,
)
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY
from kiro_crew.dashboard.token_auth import effective_request_app as _effective_request_app
from kiro_crew.dashboard.token_auth import folder_principal
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.dashboard.chat_folders")


#: PATCH fields that change how the sidebar draws a folder rather than what the
#: folder is. A person changing only these has not claimed the folder:
#: collapsing it to look past it, or a drag that renumbers its siblings.
_LAYOUT_ONLY_FIELDS = frozenset({"collapsed", "order"})


async def api_chat_folders(request: web.Request) -> web.Response:
    """GET /api/chat/folders — list project folders (with archived-session counts).

    Person and app callers see the WHOLE tree, exactly as before (an app files
    its own sessions into the person's folders, so it needs to see them). A crew
    MEMBER caller sees only the folders it OWNS -- the chat gate stamped its
    verified principal, and the tree it can reshape is the tree it should read,
    so its view matches its write authority instead of exposing the person's
    organisation.
    """
    state: DashboardState = request.app["state"]
    # _folders_with_history_counts walks the on-disk session list (a synchronous
    # filesystem scan) that is user-triggered (every GET) and scales with the
    # archived-session count. Offload it to keep the event loop responsive, using
    # subprocess_executor (the pool for potentially-slow work) rather than
    # maintenance_executor, whose fast periodic sweeps — the orphan reaper — must
    # stay responsive and could otherwise be starved by frequent polling.
    loop = asyncio.get_running_loop()
    folders = await loop.run_in_executor(subprocess_executor(), _folders_with_history_counts, state)
    member_principal = str(request.get(MEMBER_CHAT_PRINCIPAL_KEY) or "")
    if member_principal.startswith("member:"):
        # The member also sees the folder its own session is filed in, and that
        # folder's ancestors, so a path like ``Ops/<agent>`` resolves to the
        # person's ``Ops`` instead of forking a second one. That chain is the
        # path its own ``[FOLDER]`` line already shows it; nothing else of the
        # person's tree is added.
        by_id = {str(f.get("id") or ""): f for f in folders}
        home_chain: set[str] = set()
        cursor = _slot_home_folder_id(caller_home_slot(state, request, member_principal))
        while cursor and cursor in by_id and cursor not in home_chain:
            home_chain.add(cursor)
            cursor = str(by_id[cursor].get("parent_id") or "")
        folders = [
            f
            for f in folders
            if _folder_owner_app(f) == member_principal or str(f.get("id") or "") in home_chain
        ]
    return web.json_response(folders)


async def api_chat_folder_create(request: web.Request) -> web.Response:
    """POST /api/chat/folders — create a project folder."""
    state: DashboardState = request.app["state"]
    if (refusal := _refuse_unattributable_caller(state, request)) is not None:
        return refusal
    # Rate-limit INTERNAL callers only. This endpoint is mixed-path: the browser's
    # own "new folder" control posts here too, and a person organizing their chats
    # can legitimately create a dozen in one sitting, so throttling them would be a
    # regression with no security value. The threat is an automated loop on an
    # auto-approved verb, and `_audit_origin` already tells the two apart -- a
    # request without the internal secret is the browser.
    #
    # Keyed on the VALIDATED caller name, not the session key. The session key would
    # give finer granularity but is partly caller-supplied:
    # `_refuse_unattributable_caller` only refuses a `dashboard:` key naming a dead
    # slot, so a caller could present rotating non-dashboard keys and earn a fresh
    # budget for each. The caller name is checked against `_KNOWN_INTERNAL_CALLERS`,
    # so it cannot be varied to escape the bucket. The cost is that internal callers
    # share one folder budget, which is acceptable when a goal needs exactly one.
    rl_source, rl_caller = _audit_origin(request)
    if rl_source != "dashboard" and not allow_create(FOLDER_CREATE, rl_caller):
        return web.json_response(
            {
                "error": "too many folders created recently; retry shortly",
                "code": "create_rate_limited",
            },
            status=429,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        # ``[]``, ``"s"``, ``5``, ``true`` and ``null`` are all valid JSON, so
        # the parse above succeeds and the ``body.get()`` below would raise
        # AttributeError from outside the try — a 500 for what is really
        # malformed client input.
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_json"},
            status=400,
        )
    # Explicit emoji icon. An absent key and an explicit "" both mean the
    # default glyph — create never generates an icon, so there is no
    # behavioral difference between omission and opt-out. A value is stored
    # as-is. Validation lives with the other field validation in
    # ``create_folder_record``.
    raw_icon = body.get("icon")
    icon_val = str(raw_icon).strip() if raw_icon is not None else ""
    # Organizational tags copied onto every new chat filed into this folder.
    # Shape-checked here — the request-facing half, so a non-array payload is
    # refused before any folder work happens — while the AUTHORITATIVE
    # vocabulary intersection runs in ``create_folder_record``, under the tags
    # write lock, at the point of application.
    folder_tags: list[str] = []
    if "tags" in body:
        clean_tags, tags_err = _validate_folder_tags(state, body.get("tags"))
        if tags_err or clean_tags is None:
            return web.json_response(
                {"error": tags_err or "tags invalid", "code": "tags_invalid"}, status=400
            )
        folder_tags = clean_tags
    # Shape-checked here (request-facing) so a non-array/ non-string payload is
    # refused before any folder work; the authoritative path validation runs in
    # ``create_folder_record`` off the loop.
    steering_dirs: list[str] = []
    if "steering_dirs" in body:
        raw_steering = body.get("steering_dirs")
        if not isinstance(raw_steering, list) or any(
            not isinstance(entry, str) for entry in raw_steering
        ):
            return web.json_response(
                {
                    "error": "steering_dirs must be a list of strings",
                    "code": "steering_dirs_invalid",
                },
                status=400,
            )
        steering_dirs = raw_steering
    # Never from the body: a caller that could name its own owner could name
    # someone else's. Written only when a NON-PERSON principal is calling (an
    # app -> its bare name; an admitted crew member -> ``member:<store>``), so
    # the person's rows keep the shape they have on disk today and "absent means
    # the person" stays the one representation (see _folder_owner_app).
    request_app = folder_principal(state, request)
    refused = _refuse_principal_steering_dirs(
        request_app,
        steering_dirs,
        operation="chat.folder_create",
        folder_id="",
        agent=request.get("internal_auth") is True,
    )
    if refused is not None:
        return refused
    parent_id = str(body.get("parent_id") or "")
    try:
        folder = await create_folder_record(
            state,
            name=str(body.get("name") or ""),
            parent_id=parent_id,
            project_dir=str(body.get("project_dir") or ""),
            default_agent=str(body.get("default_agent") or "").strip(),
            color=str(body.get("color") or ""),
            icon=icon_val,
            request_app=request_app,
            tags=folder_tags,
            steering_dirs=steering_dirs,
            # An agent (internal transport) never mints a same-name sibling: its
            # path walk reuses what exists, so a collision here is a race it lost
            # or a folder it cannot see, and a duplicate is wrong in both cases.
            # The browser keeps a person's freedom to name two folders alike.
            refuse_duplicate_name=rl_source != "dashboard",
            home_slot=caller_home_slot(state, request, request_app),
            created_by_session=agent_creator_key(state, request),
            claim_parent_for_person=rl_source == "dashboard",
        )
    except FolderNameExistsError as exc:
        sel().log_api_access(
            caller=request_app or rl_caller,
            operation="chat.folder_create",
            outcome="denied",
            source="duplicate_name",
            resources=f"parent={parent_id}",
            error="a folder with this name already exists under that parent",
        )
        return web.json_response({"error": str(exc), "code": exc.code}, status=409)
    except FolderOwnershipError as exc:
        sel().log_api_access(
            caller=request_app,
            operation="chat.folder_create",
            outcome="denied",
            source="app_isolation",
            resources=f"parent={parent_id}",
            error="app cannot create inside a folder it does not own",
        )
        return web.json_response({"error": str(exc), "code": exc.code}, status=403)
    except FolderCapError as exc:
        return web.json_response({"error": str(exc), "code": exc.code}, status=429)
    except FolderCreateError as exc:
        # Inline literals rather than one hoisted payload dict: the error-code
        # contract scan reads the body at the `json_response` site, and a local
        # reads as an opaque body there (test/test_error_code_contract.py). The
        # wire shape is unchanged — `code` appears only when the refusal carries
        # one, exactly as the pre-extraction handler answered.
        if exc.code:
            return web.json_response({"error": str(exc), "code": exc.code}, status=400)
        return web.json_response({"error": str(exc)}, status=400)
    if folder.get("reused"):
        # Nothing was written: the caller's own same-name folder already sat
        # there. 200, not 201, so a client can tell reuse from creation.
        return web.json_response(folder, status=200)
    state.push_slots_update()
    # Create never generates an icon: a folder without an explicit emoji gets
    # the default glyph. Generation runs only on the explicit Auto-generate
    # action (PATCH regenerate_icon), so every model call is user-initiated.
    source, caller = _audit_origin(request)
    sel().log_api_access(
        caller=caller,
        operation="chat.folder_create",
        outcome="allowed",
        source=source,
        resources=str(folder["id"]),
    )
    return web.json_response(folder, status=201)


async def api_chat_folder_update(request: web.Request) -> web.Response:
    """PATCH /api/chat/folders/{id} — rename or reorder a folder."""
    state: DashboardState = request.app["state"]
    if (refusal := _refuse_unattributable_caller(state, request)) is not None:
        return refusal
    fid = request.match_info["id"]
    folder = next((f for f in state._folders if f["id"] == fid), None)
    if not folder:
        return web.json_response({"error": "not found"}, status=404)
    request_app = folder_principal(state, request)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        # ``[]``, ``"s"``, ``5``, ``true`` and ``null`` are all valid JSON, so
        # the parse above succeeds and the ``body.get()`` below would raise
        # AttributeError from outside the try — a 500 for what is really
        # malformed client input.
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_json"},
            status=400,
        )
    # Validate ALL submitted fields into a pending-changes dict BEFORE mutating
    # ``folder`` — otherwise an early field (e.g. name) is persisted while a later
    # field (e.g. an invalid/cyclic parent_id) returns 400, leaving the rejected
    # request's partial mutation live for the next successful save.
    #
    # ``owner_app`` is deliberately absent from the fields below: ownership is
    # stamped once at create from the authenticated caller and is not a field a
    # request can hand over, take, or clear.
    changes: dict[str, object] = {}
    if "name" in body:
        new_name = str(body["name"]).strip()[:100]
        if not new_name:
            return web.json_response({"error": "name required"}, status=400)
        changes["name"] = new_name
    if "collapsed" in body:
        changes["collapsed"] = bool(body["collapsed"])
    if "hidden" in body:
        changes["hidden"] = bool(body["hidden"])
    if "order" in body:
        # A non-numeric, null, or non-finite order is caller error, not a server
        # fault: int() would raise and surface as a 500 (no middleware maps
        # handler exceptions). body_int turns a JSON infinity such as 1e309
        # into a ValueError. Skip the field instead, matching
        # api_chat_tag_update.
        try:
            changes["order"] = body_int(body["order"])
        except (TypeError, ValueError):
            pass
    if "default_agent" in body:
        val = body["default_agent"]
        changes["default_agent"] = str(val).strip() if val is not None else ""
    reparenting = "parent_id" in body
    new_parent = ""
    if reparenting:
        # Re-parent: move this folder into another folder, or to the top
        # level ("" / null). Reject self-parenting and cycles (the new
        # parent must not be the folder itself or any of its descendants).
        #
        # Self-parenting is state-independent, so it is decided here. The other
        # two conditions depend on the CURRENT tree, and this check runs before
        # the store lock is taken — so it is only a fast reject. The
        # authoritative parent-exists / cycle test is repeated inside ``_apply``
        # under the lock: two opposite reparents (A into B, B into A) can both
        # pass here against the same pre-state and would otherwise both apply,
        # persisting a cycle that makes both folders unreachable in the tree.
        new_parent = str(body["parent_id"] or "")
        if new_parent:
            if new_parent == fid:
                return web.json_response({"error": "folder cannot be its own parent"}, status=400)
            if not any(f["id"] == new_parent for f in state._folders):
                return web.json_response({"error": "parent folder not found"}, status=400)
            if _is_descendant(state._folders, ancestor_id=fid, folder_id=new_parent):
                return web.json_response(
                    {"error": "cannot move a folder into its own descendant"},
                    status=400,
                )
        changes["parent_id"] = new_parent
    if "project_dir" in body:
        pd, err = _validate_project_dir(str(body["project_dir"] or "").strip())
        if err:
            return web.json_response({"error": err}, status=400)
        if pd:
            # Off-loop: the shared scan primes runtime paths on first use.
            conflict = await asyncio.to_thread(_folder_project_overlap_denied, pd)
            if conflict is not None:
                return web.json_response(
                    {"error": conflict, "code": "workspace_overlaps_data_home"}, status=400
                )
        changes["project_dir"] = pd
    if "color" in body:
        # Palette color for the folder glyph. None or empty string clears back
        # to the default gray; anything else must be an allowlisted value.
        raw_color = body["color"]
        color_val = str(raw_color).strip().lower() if raw_color is not None else ""
        if color_val and not _is_valid_folder_color(color_val):
            return web.json_response(
                {
                    "error": "color must be one of the folder palette values",
                    "code": "color_invalid",
                },
                status=400,
            )
        changes["color"] = color_val
    raw_regen = body.get("regenerate_icon", False)
    if not isinstance(raw_regen, bool):
        # Strings are truthy ("false" would arm regeneration); require a real
        # boolean so a sloppy caller gets a 400 instead of a surprise.
        return web.json_response(
            {
                "error": "regenerate_icon must be a boolean",
                "code": "regenerate_icon_invalid",
            },
            status=400,
        )
    regenerate_icon = raw_regen
    if "icon" in body and regenerate_icon:
        # Mutually exclusive: the manual icon would be saved and returned, then
        # the background regeneration would silently overwrite it. Reject the
        # ambiguous request so the conflict is explicit to the caller.
        return web.json_response(
            {
                "error": "cannot set icon and regenerate_icon in the same request",
                "code": "icon_conflict",
            },
            status=400,
        )
    if "icon" in body:
        # User-chosen emoji for the folder glyph. None or empty string clears
        # back to the default glyph; anything else must be exactly one emoji
        # grapheme (no text, no multiple emoji).
        raw_icon = body["icon"]
        icon_val = str(raw_icon).strip() if raw_icon is not None else ""
        if icon_val and not _is_single_emoji(icon_val):
            return web.json_response(
                {"error": "icon must be a single emoji", "code": "icon_invalid"},
                status=400,
            )
        changes["icon"] = icon_val
    if "steering_dirs" in body:
        # A list of directories loaded as steering for every chat in this
        # folder's subtree. An empty list clears them; anything else is
        # validated per entry (absolute/sensitive/isdir), capped at 16, and
        # deduped. Off the loop, like project_dir, since each entry stats disk.
        # Only the person may declare a non-empty list (see
        # _refuse_principal_steering_dirs); the refusal precedes any path work.
        raw_steering = body["steering_dirs"]
        refused = _refuse_principal_steering_dirs(
            request_app,
            raw_steering if isinstance(raw_steering, list) else [raw_steering],
            operation="chat.folder_steering_dirs",
            folder_id=fid,
            agent=request.get("internal_auth") is True,
        )
        if refused is not None:
            return refused
        resolved_steering, steering_err = await asyncio.to_thread(
            _validate_steering_dirs, body["steering_dirs"]
        )
        if steering_err:
            return web.json_response(
                {"error": steering_err, "code": "steering_dirs_invalid"}, status=400
            )
        changes["steering_dirs"] = resolved_steering
    from kiro_crew.dashboard.handlers.change_cards import REQ_CARD_FOLDER_STEERING_EXPECT

    # Set only by a folder.steering card's Apply/Undo step: the list it
    # replaces, compared under the store lock in _apply.
    expect_steering = request.get(REQ_CARD_FOLDER_STEERING_EXPECT)
    if (
        expect_steering is not None
        and "steering_dirs" in changes
        and changes["steering_dirs"] != body["steering_dirs"]
    ):
        # The card carries each directory already resolved, so a step whose
        # list now resolves elsewhere (a directory swapped for a link after
        # the person approved it) would store a tree they never saw.
        return web.json_response(
            {
                "error": "a steering directory now resolves to a different path than the card showed",
                "code": "steering_dirs_changed",
            },
            status=409,
        )
    if "tags" in body:
        # Vocabulary-constrained tag list. An empty list clears the folder's
        # tags; anything else must be ids that exist in the tag vocabulary.
        clean_tags, tags_err = _validate_folder_tags(state, body["tags"])
        if tags_err:
            return web.json_response({"error": tags_err, "code": "tags_invalid"}, status=400)
        changes["tags"] = clean_tags
    # All fields validated — apply atomically under the store lock, re-finding
    # the folder there so a concurrent delete cannot resurrect it, and
    # re-deciding the tree-shape rules there so two concurrent reparents cannot
    # each validate against the pre-state and persist a cycle between them.
    # ``committed_name`` is filled under that same lock, from the folder as it
    # is committed, so a regenerate spawned below derives from exactly the
    # persisted name — never from a pre-lock snapshot a concurrent write may
    # have superseded.
    committed_name: list[str] = []
    origin_source, origin_caller = _audit_origin(request)
    refuse_duplicate_name = origin_source != "dashboard"
    # A person editing anything beyond layout claims the folder: its agent mark
    # goes, and chat_folder_delete refuses it from then on. ``regenerate_icon``
    # is not in ``changes`` (the icon lands later, from the generator), so it
    # is counted here by name. A person moving a folder INTO another also
    # claims that destination, the same as nesting a new folder under it.
    by_person = origin_source == "dashboard"
    claims_for_person = by_person and (regenerate_icon or bool(set(changes) - _LAYOUT_ONLY_FIELDS))
    claims_parent_for_person = by_person and reparenting and bool(new_parent)
    # An agent rename or restyle from a session a channel can drive is refused
    # here, not only in the MCP tool: a conversation resumed from a channel into
    # a dashboard session keeps that session's ``dashboard:`` key, so no check on
    # the key can tell the channel's turns from the person's. Reachability is
    # state this endpoint holds (``_caller_reaches_a_channel``). Moves and
    # reorders go through chat_folder_move, whose channel rule is its own.
    caller_key = request.headers.get("X-Session-Key", "").strip()
    if (
        origin_source != "dashboard"
        and ({"name", "icon", "color"} & changes.keys() or regenerate_icon)
        and _caller_reaches_a_channel(request, caller_key)
    ):
        sel().log_api_access(
            caller=request_app or origin_caller,
            operation="chat.folder_update",
            outcome="denied",
            source="channel_containment",
            resources=fid,
            error="agent rename from a session a channel can drive",
        )
        return web.json_response(
            {
                "error": (
                    "this session can receive turns from a channel conversation, so "
                    "an agent cannot rename or restyle folders from it"
                ),
                "code": "channel_reachable_caller",
            },
            status=403,
        )

    def _apply(folders: list[dict[str, Any]]) -> tuple[bool, str]:
        target = next((f for f in folders if f["id"] == fid), None)
        if target is None:
            return False, "not_found"
        # Ownership, decided here for the same reason the cycle rule is: a
        # concurrent reparent can change who the target or the destination
        # belongs to between validation and the write.
        if request_app and _folder_owner_app(target) != request_app:
            return False, "not_owned"
        if (
            expect_steering is not None
            and list(target.get("steering_dirs") or []) != expect_steering
        ):
            return False, "steering_changed"
        if reparenting and new_parent:
            if not any(f["id"] == new_parent for f in folders):
                return False, "parent_not_found"
            if _is_descendant(folders, ancestor_id=fid, folder_id=new_parent):
                return False, "cycle"
            dest = next((f for f in folders if f["id"] == new_parent), None)
            if request_app and dest is not None and _folder_owner_app(dest) != request_app:
                return False, "forbidden_parent"
        if (
            request_app
            and (reparenting or "order" in changes)
            and _subtree_holds_foreign_folder(folders, root_id=fid, request_app=request_app)
        ):
            # A move OR a reposition relocates the whole subtree with it, so a
            # folder the person nested inside this one would be relocated by an
            # app's write. Both a reparent and an order change are gated: a
            # rename, a colour or a collapse changes nothing about where the
            # descendants sit, but a reposition changes where the subtree
            # renders exactly as a reparent does. Checked for a move to the top
            # level too -- "" is still a move.
            return False, "foreign_descendant"
        if refuse_duplicate_name and "name" in changes:
            # Same rule and same place as create_folder_record's: an agent never
            # makes a same-name sibling, because the two then share one path and
            # neither can be addressed by it. Decided here, under the lock, over
            # the whole tree -- a crew member's own GET is filtered, so a check
            # against what the caller can read would miss a folder it cannot
            # see. The folder's own row is excluded, so re-casing its own name
            # is allowed. The browser keeps a person's freedom to name two alike.
            final_parent = new_parent if reparenting else str(target.get("parent_id") or "")
            folded = str(changes["name"]).strip().casefold()
            if any(
                str(f.get("id")) != fid
                and str(f.get("parent_id") or "") == final_parent
                and str(f.get("name") or "").strip().casefold() == folded
                for f in folders
            ):
                return False, "name_exists"
        target.update(changes)
        if claims_for_person:
            target.pop(CREATED_BY_SESSION, None)
        if claims_parent_for_person:
            dest_row = next((f for f in folders if f["id"] == new_parent), None)
            if dest_row is not None:
                dest_row.pop(CREATED_BY_SESSION, None)
        if not target.get("color"):
            target.pop("color", None)
        if not target.get("icon"):
            # Empty string clears the key entirely, so "absent means the
            # default glyph" stays the single on-disk representation
            # (mirrors color above).
            target.pop("icon", None)
        if not target.get("steering_dirs"):
            # Empty list clears the key entirely, so "absent means none" stays
            # the single on-disk representation (mirrors color/tags). PATCH with
            # ``[]`` therefore clears a folder's steering directories.
            target.pop("steering_dirs", None)
        if not target.get("tags"):
            # Empty list clears the key entirely, so "absent means no tags"
            # stays the single on-disk representation (mirrors color above).
            target.pop("tags", None)
        committed_name.append(str(target.get("name") or ""))
        return True, ""

    committed_epoch: list[int] = []

    def _bump_epoch_on_commit() -> None:
        # Invalidate any in-flight icon generation: its result was derived
        # for the pre-change name and must not land over this mutation. Runs
        # under the store lock only after persistence is proven, so it stays
        # ordered against the write-back's epoch check while a rolled-back
        # write leaves the epoch untouched and a still-valid in-flight
        # generation can land. The epoch a regenerate spawned below must
        # expect is captured here, after this request's own bump and under
        # the same lock — a concurrent mutation committing after this hook
        # bumps past it, so a generation derived from this request's
        # committed name is invalidated rather than landing over newer state.
        if "icon" in changes or "name" in changes:
            _bump_icon_epoch(fid)
        committed_epoch.append(_CHAT_FOLDER_ICON_EPOCHS.get(fid, 0))

    if "tags" in changes:
        # Same point-of-application rule as create: the authoritative
        # intersection and the store write are one critical section under
        # ``tags_write_lock``, so a concurrent tag deletion cannot slip a
        # just-deleted id past the strip pass and back onto this folder.

        async with tags_write_lock(state):
            refreshed, _ = _validate_folder_tags(state, changes["tags"])
            changes["tags"] = refreshed if refreshed is not None else []
            err = await state.mutate_folders(_apply, on_committed=_bump_epoch_on_commit)
    else:
        err = await state.mutate_folders(_apply, on_committed=_bump_epoch_on_commit)
    if err == "not_found":
        # Deleted between the validation above and acquiring the store lock.
        return web.json_response({"error": "not found", "code": "folder_not_found"}, status=404)
    if err in ("not_owned", "forbidden_parent", "foreign_descendant"):
        # Distinguished in the audit, not to the caller: one code for all three
        # keeps the response from reporting which folder was foreign.
        _reason = {
            "not_owned": "app cannot change a folder it does not own",
            "forbidden_parent": "app cannot move a folder into one it does not own",
            "foreign_descendant": "app cannot move a folder holding one it does not own",
        }[err]
        sel().log_api_access(
            caller=request_app,
            operation="chat.folder_update",
            outcome="denied",
            source="app_isolation",
            resources=(f"parent={new_parent}" if err == "forbidden_parent" else fid),
            error=_reason,
        )
        return web.json_response(
            {
                "error": "this app does not own that folder",
                "code": "folder_not_owned",
            },
            status=403,
        )
    if err == "name_exists":
        sel().log_api_access(
            caller=request_app or origin_caller,
            operation="chat.folder_update",
            outcome="denied",
            source="duplicate_name",
            resources=fid,
            error="agent rename would duplicate a sibling folder name",
        )
        return web.json_response(
            {
                "error": "a sibling folder already has that name",
                "code": "folder_name_exists",
            },
            status=409,
        )
    if err == "parent_not_found":
        # The parent was deleted while this request waited for the lock.
        return web.json_response(
            {"error": "parent folder not found", "code": "folder_parent_not_found"},
            status=400,
        )
    if err == "steering_changed":
        return web.json_response(
            {
                "error": "the folder's steering directories changed since this was checked",
                "code": "steering_dirs_changed",
            },
            status=409,
        )
    if err == "cycle":
        # A concurrent reparent moved the target under this folder while this
        # request waited for the lock; applying it now would persist a cycle.
        return web.json_response(
            {
                "error": "cannot move a folder into its own descendant",
                "code": "folder_cycle",
            },
            status=409,
        )
    if regenerate_icon:
        # "Reset to auto" — re-run the emoji generator in the background.
        # Runs only after _apply succeeded, so app ownership has already been
        # enforced on this folder. The write-back's slots push delivers the
        # new icon when it lands. Both inputs are captured at commit time
        # under the store lock: the name by _apply, the epoch by the
        # post-commit hook after this request's own bump — so the write-back
        # is pinned to exactly the committed state, and a manual set, clear,
        # or rename landing while the generator runs invalidates the stale
        # result.
        _spawn_chat_folder_icon_task(
            state,
            fid,
            committed_name[0] if committed_name else "",
            expected_epoch=committed_epoch[0] if committed_epoch else 0,
        )
    state.push_slots_update()
    source, caller = _audit_origin(request)
    sel().log_api_access(
        caller=caller,
        operation="chat.folder_update",
        outcome="allowed",
        source=source,
        resources=fid,
    )
    return web.json_response(folder)


#: The most rows one reorder request may carry. A reorder writes one row per
#: sibling touched, and the store itself is capped at :data:`MAX_CHAT_FOLDERS`,
#: so a request naming more entries than there can be folders is malformed
#: rather than large. The cap is the folder ceiling, not a smaller number: a
#: person renumbering a flat tree of the maximum size sends exactly that many
#: rows in one legitimate drag.
_MAX_REORDER_ENTRIES = MAX_CHAT_FOLDERS

#: Byte ceiling for a reorder body, sized from the entry cap rather than the
#: shared 64 KB default: a legitimate max-size flat-tree reorder carries
#: :data:`_MAX_REORDER_ENTRIES` entries, each ``{"id": "<uuid>", "order": <int>}``
#: comfortably under 256 bytes with its JSON envelope, so 500 rows can exceed
#: the shared default. The bound is that entry budget, so the largest legal
#: request is admitted while an oversized body is rejected before decoding.
_MAX_REORDER_BODY_BYTES = _MAX_REORDER_ENTRIES * 256


async def api_chat_folder_reorder(request: web.Request) -> web.Response:
    """POST /api/chat/folders/reorder -- set several folders' ``order`` atomically.

    The one way to express a reorder as a SINGLE transaction. ``PATCH
    /api/chat/folders/{id}`` takes one row per request, so a caller renumbering
    several siblings issues N requests with no transaction between them: a
    failure partway leaves the tree carrying a mix of old and new ``order``
    numbers until the action is repeated. This endpoint applies the whole list
    in one ``mutate_folders`` pass under the folder-store lock, all-or-none -- so
    a rejected row leaves the stored order exactly as it was, never half-applied.

    Body: ``{"orders": [{"id": str, "order": int}, ...]}``, plus the optional
    request-level ``expected_parent`` described below. Every entry is
    validated into a pending map BEFORE the lock is taken (the same shape
    discipline ``api_chat_folder_update`` uses for its single row), so a
    malformed request is a 400 that never touches the store.

    Ownership is re-decided per row INSIDE the lock, exactly as ``_apply`` does
    for one row: an app may reorder only the folders it owns, and a batch naming
    one it does not is refused whole. Row ownership is not the whole rule --
    repositioning a folder relocates its whole subtree, so a row the app owns
    whose descendants include the person's is refused too, the same violation
    the reparent PATCH refuses one level down. Both live here because the reorder
    that composes these writes is the one place under the lock that sees the
    subtree, so a positioning caller states the whole renumber as a single batch
    and relies on this endpoint to authorize it.

    Reorder touches only ``order``: it never reparents, renames, recolors or
    retags. A row naming a folder absent from the store is a 404 for the whole
    batch (the reorder the caller computed describes a tree that has since
    shifted), so no partial renumber lands against a shifted tree.

    ``order`` is a per-container index, so a renumber is only correct for rows
    still living in the container the caller computed it against. The optional
    request-level ``expected_parent`` states that container: when the key is
    present, every written row's stored ``parent_id`` must equal its value
    (empty string names the root lane), compared under the same lock that does
    the writing -- checking earlier would reopen the window it closes. A
    mismatch means a concurrent reparent moved a row between the caller's read
    and this write, and landing the batch anyway would persist an index
    computed for the old container onto a row in a new one; the whole batch is
    refused as 409 ``folder_parent_changed`` with the store untouched. Absence
    of the key is the one way to make no assumption -- a caller positioning
    rows by absolute index never read a container, so no claim is demanded of
    it -- and is told apart from an empty string by the key's presence, never
    its value. The stored parent is read, never written.
    """
    state: DashboardState = request.app["state"]
    if (refusal := _refuse_unattributable_caller(state, request)) is not None:
        return refusal
    request_app = folder_principal(state, request)
    body, body_err = await read_bounded_json(request, max_bytes=_MAX_REORDER_BODY_BYTES)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    raw_orders = body.get("orders")
    if not isinstance(raw_orders, list):
        return web.json_response(
            {"error": "orders must be an array", "code": "orders_not_array"}, status=400
        )
    if len(raw_orders) > _MAX_REORDER_ENTRIES:
        return web.json_response(
            {"error": "too many folders in one reorder", "code": "orders_too_many"}, status=400
        )
    # The container claim is presence-checked, the same idiom the reparent PATCH
    # uses for ``parent_id``: ``None`` here means the key is absent and no row's
    # parent is compared. A present value must be a real string -- coercing
    # (say) a JSON null or 0 through falsiness would silently turn caller junk
    # into a root claim, so a non-string is a 400 instead.
    expected_parent: str | None = None
    if "expected_parent" in body:
        raw_expected = body["expected_parent"]
        if not isinstance(raw_expected, str):
            return web.json_response(
                {"error": "expected_parent must be a string", "code": "expected_parent_invalid"},
                status=400,
            )
        expected_parent = raw_expected
    # Validate every entry into an id -> order map BEFORE the lock is taken, the
    # same shape discipline api_chat_folder_update applies to its single row: a
    # malformed batch is a 400 that never touches the store. Last-writer-wins on
    # a duplicate id, matching how the store tolerates two rows sharing a number.
    pending: dict[str, int] = {}
    for entry in raw_orders:
        if not isinstance(entry, dict):
            return web.json_response(
                {"error": "each order entry must be an object", "code": "order_entry_invalid"},
                status=400,
            )
        fid = str(entry.get("id") or "")
        if not fid:
            return web.json_response(
                {"error": "each order entry needs an id", "code": "order_id_missing"}, status=400
            )
        # A non-numeric, null, or non-finite order is caller error, not a server
        # fault -- matching the single-row PATCH, which skips such a field. Here
        # the field IS the request, so a bad value is a 400 rather than a
        # silent skip: a caller sending it meant to move the row, and dropping
        # it would leave that row where the reorder did not want it.
        #
        # ``type(...) is int`` not ``isinstance`` and not a bare ``int(...)``:
        # a JSON boolean is a Python ``bool`` (an ``int`` subclass, so ``True``
        # would slip through as 1) and a JSON float like ``1.5`` would be
        # truncated by ``int()`` -- both violate the integer-only contract, so
        # they are 400s, not coerced.
        try:
            order_val = entry["order"]
        except KeyError:
            return web.json_response(
                {"error": "each order must be an integer", "code": "order_not_int"}, status=400
            )
        if type(order_val) is not int:
            return web.json_response(
                {"error": "each order must be an integer", "code": "order_not_int"}, status=400
            )
        pending[fid] = order_val

    if not pending:
        # An empty reorder changes nothing; report success without a store write.
        return web.json_response({"ok": True})

    def _apply(folders: list[dict[str, Any]]) -> tuple[bool, str]:
        by_id = {f["id"]: f for f in folders}
        # Re-find and re-authorize EVERY row under the lock before mutating any,
        # so the pass is all-or-none: a missing or foreign row aborts with the
        # store untouched, never half-renumbered. Mirrors _apply's single-row
        # re-find + ownership check, applied to each entry.
        for fid, _order in pending.items():
            target = by_id.get(fid)
            if target is None:
                return False, "not_found"
            if request_app and _folder_owner_app(target) != request_app:
                return False, "not_owned"
            # Ownership of the row itself is not the whole rule: repositioning a
            # folder relocates its whole subtree, so a row the app owns whose
            # descendants include the person's relocates theirs -- the same
            # violation the reparent PATCH refuses one level down, reached here
            # for a position that sends no parent_id. The reorder that composes
            # these writes is the only place that sees the subtree, so the
            # subtree rule is enforced here, per row, before any write lands.
            if request_app and _subtree_holds_foreign_folder(
                folders, root_id=fid, request_app=request_app
            ):
                return False, "subtree_not_owned"
            # The container claim is decided last, so authorization always wins
            # over the precondition: a caller refused a foreign row learns
            # nothing about where that row now lives. The stored parent is
            # normalized the way the tree walkers read it (absent and null both
            # mean the root lane), and one mismatched row refuses the whole
            # batch -- its order number was computed for a container it has
            # left, so landing the rest around it renumbers a tree the caller
            # never saw.
            if expected_parent is not None:
                if str(target.get("parent_id") or "") != expected_parent:
                    return False, "parent_changed"
        changed = False
        for fid, order in pending.items():
            target = by_id[fid]
            if target.get("order") != order:
                target["order"] = order
                changed = True
        return changed, ""

    err = await state.mutate_folders(_apply)
    if err == "not_found":
        # A folder named in the batch is absent from the store: it was deleted
        # between the caller reading the tree and this write. The reorder
        # describes a tree that has since changed, so none of it lands.
        return web.json_response(
            {"error": "a folder in the reorder no longer exists", "code": "folder_not_found"},
            status=404,
        )
    if err == "parent_changed":
        # A row's stored parent differs from the caller's claim: a concurrent
        # reparent moved it between the caller's read and this write. A benign
        # race like the deleted-row 404 above, not a violation, so it is not
        # audited as denied. The 409 tells the caller its cached tree is stale;
        # refetching and redrawing is the recovery, exactly as for the 404.
        return web.json_response(
            {
                "error": "a folder in the reorder was moved to another parent",
                "code": "folder_parent_changed",
            },
            status=409,
        )
    if err == "not_owned":
        # One row named a folder this app does not own. Refused whole, and
        # distinguished only in the audit -- the same one code for the caller
        # api_chat_folder_update uses, so the response reports no folder as
        # foreign.
        sel().log_api_access(
            caller=request_app,
            operation="chat.folder_reorder",
            outcome="denied",
            source="app_isolation",
            resources=",".join(list(pending)[:10]),
            error="app cannot reorder a folder it does not own",
        )
        return web.json_response(
            {"error": "this app does not own one of those folders", "code": "folder_not_owned"},
            status=403,
        )
    if err == "subtree_not_owned":
        # A row the app owns has descendants the person owns. Repositioning it
        # relocates theirs, which is the reparent-path violation reached one
        # level down, so the whole batch is refused with the store untouched.
        sel().log_api_access(
            caller=request_app,
            operation="chat.folder_reorder",
            outcome="denied",
            source="app_isolation",
            resources=",".join(list(pending)[:10]),
            error="app cannot reposition a folder whose subtree holds the person's",
        )
        return web.json_response(
            {
                "error": "one of those folders contains folders this app does not own",
                "code": "folder_not_owned",
            },
            status=403,
        )
    state.push_slots_update()
    source, caller = _audit_origin(request)
    sel().log_api_access(
        caller=caller,
        operation="chat.folder_reorder",
        outcome="allowed",
        source=source,
        resources=",".join(list(pending)[:10]),
    )
    return web.json_response({"ok": True})


async def api_chat_folder_delete(request: web.Request) -> web.Response:
    """DELETE /api/chat/folders/{id} — delete a folder, ungroup its slots."""

    state: DashboardState = request.app["state"]
    if (refusal := _refuse_unattributable_caller(state, request)) is not None:
        return refusal
    fid = request.match_info["id"]
    target = next((f for f in state._folders if f["id"] == fid), None)
    if target is None:
        return web.json_response({"error": "not found"}, status=404)
    request_app = _effective_request_app(state, request)
    # Answered before a single slot is unfiled, so the common refusal costs no
    # rollback. Sound pre-lock because ``owner_app`` is stamped at create and no
    # route can reassign it — unlike the child test in ``_remove``, this answer
    # cannot go stale while the request runs.
    if request_app and _folder_owner_app(target) != request_app:
        sel().log_api_access(
            caller=request_app,
            operation="chat.folder_delete",
            outcome="denied",
            source="app_isolation",
            resources=fid,
            error="app cannot delete a folder it does not own",
        )
        return web.json_response(
            {"error": "this app does not own that folder", "code": "folder_not_owned"},
            status=403,
        )
    # An app may not delete a folder at all -- not even an empty one it owns.
    #
    # This is the smallest rule that is actually enforceable. A delete relocates
    # everything the folder contains, and a folder's contents live in a DIFFERENT
    # store from the folder: sessions are in the slot table and the session
    # archive, neither of which shares a lock with the folder store. So "is this
    # folder empty?" cannot be answered atomically with the removal, and every
    # narrower rule leaked through a different seam -- a session filed while the
    # archive scan awaited, a child created while the lock was acquired, a
    # session closing after the scan and writing its folder_id on the way out.
    # Each was closable in isolation; the class was not.
    #
    # Nothing shipped loses a capability: the one MCP tool that reaches this
    # route, chat_folder_delete, sends ``if_empty`` (below) and is refused here
    # for an app like any other app caller, so its working callers are the
    # person's own sessions and the dashboard UI. An app organizes its own work by
    # creating, renaming and reparenting its folders and filing its sessions --
    # cleanup is the person's, who can delete a full folder as they always could.
    if request_app:
        sel().log_api_access(
            caller=request_app,
            operation="chat.folder_delete",
            outcome="denied",
            source="app_isolation",
            resources=fid,
            error="app cannot delete folders",
        )
        return web.json_response(
            {
                "error": (
                    "an app cannot delete folders - ask the person, or move your "
                    "sessions out and leave the folder"
                ),
                "code": "folder_delete_forbidden",
            },
            status=403,
        )
    # ``?if_empty=true`` is the empty-only delete the chat_folder_delete MCP tool
    # sends. It never unfiles a slot or lifts a subfolder: occupancy is answered
    # where the removal happens. Live slots and child folders are re-checked
    # inside the folder-store callback below, which runs synchronously under the
    # store lock and removes the row from the live list in the same step, so a
    # slot PATCH or a child create cannot land between the check and the removal
    # (both refuse a folder that is absent from the live list). Archived
    # sessions are counted first, off the loop, because that is a disk scan; a
    # session that is filed AND closed inside that one scan is the residue, and
    # its transcript keeps a folder id that every reader renders as unfiled.
    if_empty = (request.query.get("if_empty") or "").strip().lower() in ("1", "true", "yes")
    if if_empty:
        # Only a folder the CALLER's own session created, and the person has not
        # touched since (see CREATED_BY_SESSION), may go this way. A folder the
        # person made, reused, renamed, moved, or filed a session into carries
        # no mark, so an agent never removes a folder the person relies on.
        # Decided again under the lock below; this read only spares the
        # archive scan for the common refusal.
        caller_key = agent_creator_key(state, request)
        not_yours = not caller_key or str(target.get(CREATED_BY_SESSION) or "") != caller_key
        loop = asyncio.get_running_loop()
        archived = (
            {}
            if not_yours
            else await loop.run_in_executor(subprocess_executor(), _folder_history_counts, state)
        )
        if archived.get(fid, 0):
            return web.json_response(
                {
                    "error": "folder still holds archived sessions",
                    "code": "folder_not_empty",
                },
                status=409,
            )

        def _remove_if_empty(folders: list[dict[str, Any]]) -> tuple[bool, str]:
            row = next((f for f in folders if f.get("id") == fid), None)
            if row is None:
                return False, "gone"
            if not caller_key or str(row.get(CREATED_BY_SESSION) or "") != caller_key:
                return False, "not_yours"
            if any(f.get("parent_id") == fid for f in folders):
                return False, "folder has subfolders"
            if any(slot.folder_id == fid for slot in state._slots.values()):
                return False, "folder still holds live sessions"
            folders[:] = [f for f in folders if f["id"] != fid]
            return True, ""

        refused = await state.mutate_folders(_remove_if_empty)
        if refused == "gone":
            return web.json_response({"error": "not found", "code": "folder_not_found"}, status=404)
        if refused == "not_yours":
            sel().log_api_access(
                caller=_audit_origin(request)[1],
                operation="chat.folder_delete",
                outcome="denied",
                source="folder_origin",
                resources=fid,
                error="folder was not created by this session, or the person has used it",
            )
            return web.json_response(
                {
                    "error": (
                        "only a folder this session created, and the person has not "
                        "edited or used since, can be deleted this way"
                    ),
                    "code": "folder_not_agent_owned",
                },
                status=403,
            )
        if refused:
            return web.json_response({"error": refused, "code": "folder_not_empty"}, status=409)
        _CHAT_FOLDER_ICON_EPOCHS.pop(fid, None)
        pending_icon = _CHAT_FOLDER_PENDING_ICON_TASKS.pop(fid, None)
        if pending_icon is not None and not pending_icon.done():
            pending_icon.cancel()
        state.push_slots_update()
        source, caller = _audit_origin(request)
        sel().log_api_access(
            caller=caller,
            operation="chat.folder_delete",
            outcome="allowed",
            source=source,
            resources=fid,
        )
        return web.json_response({"ok": True})
    # Unfile the folder's slots first, then commit the folder removal. If that
    # commit fails, put the slots back: otherwise the delete half-lands —
    # conversations persistently unfiled while the folder they came from is
    # still there. Restoring is order-neutral, which matters because either
    # ordering leaves a partial-commit window on its own (folder-first strands a
    # dangling folder_id; slots-first strands unfiled conversations), and only
    # undoing the half that did land closes both.
    unfiled: list[tuple[Any, str]] = []
    for slot in state._slots.values():
        if slot.folder_id == fid:
            unfiled.append((slot, slot.folder_id))
            # Pin the write to the transcript this iteration's membership
            # check covered: the save awaits inside the loop, so a rebind can
            # land mid-persist and the save would otherwise resolve its
            # target from the moved routing at write time. No await between
            # this capture and the unfile below.
            authorized_history_key = slot_history_key(slot)
            slot.folder_id = ""
            if not await save_slot_off_loop(
                state, slot, force=True, expected_history_key=authorized_history_key
            ):
                # Refused without writing (session permanently deleted or
                # rebound mid-persist). The in-memory unfile stands — the
                # folder is being removed — so mark dirty and let the
                # periodic flush persist wherever the slot now routes; a
                # dangling folder_id left on the old transcript is ignored
                # on the next load.
                slot._dirty = True
                logger.warning(
                    "folder delete: unfile save refused for %s "
                    "(session deleted or rebound); marked dirty for "
                    "periodic-flush retry",
                    getattr(slot, "key", "?"),
                )

    async def _restore_unfiled() -> None:
        for slot, previous in unfiled:
            # Only put back a slot that is STILL unfiled. Between the unfile
            # above and this rollback the user can move that conversation
            # somewhere else, and their move is the newer intent — restoring
            # `previous` unconditionally would discard it and, worse, file the
            # slot back into the folder this request was trying to delete.
            if slot.folder_id:
                continue
            # Same pin as the unfile: no await between this capture and the
            # restore below, so the rollback write cannot land on a
            # transcript this slot was rebound to mid-restore.
            authorized_history_key = slot_history_key(slot)
            slot.folder_id = previous
            try:
                applied = await save_slot_off_loop(
                    state,
                    slot,
                    force=True,
                    expected_history_key=authorized_history_key,
                )
            except Exception:
                # Best-effort restore; a slot left unfiled renders at the top
                # level, which the sidebar handles, so keep restoring the rest.
                logger.warning(
                    "folder delete rollback: could not restore slot %s to folder %s",
                    slot.key,
                    previous,
                    exc_info=True,
                )
            else:
                if not applied:
                    # Refused without writing (session deleted or rebound).
                    # Keep the restored live field and mark dirty so the
                    # periodic flush persists it wherever the slot now routes.
                    slot._dirty = True
                    logger.warning(
                        "folder delete rollback: restore save refused for %s "
                        "(session deleted or rebound); marked dirty for "
                        "periodic-flush retry",
                        getattr(slot, "key", "?"),
                    )
        state.push_slots_update()

    def _remove(folders: list[dict[str, Any]]) -> tuple[bool, None]:
        for f in folders:
            if f.get("parent_id") == fid:
                f["parent_id"] = ""
        # In place, not a rebind: mutate_folders snapshots the list object it
        # was given, and other holders of state._folders must see the removal.
        folders[:] = [f for f in folders if f["id"] != fid]
        return True, None

    try:
        await state.mutate_folders(_remove)
    except Exception:
        await _restore_unfiled()
        raise
    # Pop the epoch only after the removal is confirmed persisted. Popping
    # inside the callback would be a module-level side effect that survives a
    # failed store write: the folder would still exist while its epoch read 0
    # again, letting a stale in-flight generation clobber a manual icon. After
    # a confirmed delete the entry has nothing left to guard (the write-back
    # already drops results for a folder it cannot re-find); popping keeps the
    # dict from growing with every deleted-folder id over the process lifetime.
    _CHAT_FOLDER_ICON_EPOCHS.pop(fid, None)
    # Cancel the folder's pending icon generation and drop its registry entry.
    # Without this, an owner looping create->delete accumulates one queued
    # task per deleted folder behind the serialized generator — each holds a
    # strong reference and a slot in the one-at-a-time model queue. The cancel
    # is safe after a confirmed delete: a write already started finishes under
    # the shield, and its write-back re-finds the folder by id, which no
    # longer exists, so nothing lands.
    pending = _CHAT_FOLDER_PENDING_ICON_TASKS.pop(fid, None)
    if pending is not None and not pending.done():
        pending.cancel()
    state.push_slots_update()
    source, caller = _audit_origin(request)
    sel().log_api_access(
        caller=caller,
        operation="chat.folder_delete",
        outcome="allowed",
        source=source,
        resources=fid,
    )
    return web.json_response({"ok": True})
