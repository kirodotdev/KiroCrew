"""What each sidebar folder tool of the ``kirocrew-dashboard`` server does when called.

One ``_run_chat_folder_*`` body per row of ``_folder_tools`` in
:mod:`kiro_crew.mcp_dashboard`: read the tree, create, move, update and delete a folder,
and file a session, or the calling session itself, into one. Each takes ``(args, ctx)``.
:mod:`kiro_crew.mcp_dashboard` binds every name defined here, so each one reads, imports
and patches as ``kiro_crew.mcp_dashboard.<name>``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from kiro_crew.config.sections import FOLDER_SORT_DEFAULT
from kiro_crew.mcp_dashboard_caller_scope import (
    _own_chat_slot,
    _refuse_tree_shaping_if_unverifiable,
    _visible_chat_slots,
)
from kiro_crew.mcp_dashboard_folder_tree import (
    _chat_folder_created,
    _chat_folder_paths,
    _chat_folder_render_order,
    _chat_folder_sort_mode,
    _sidebar_group_order,
)
from kiro_crew.mcp_dashboard_references import (
    _MAX_FOLDER_NAME,
    _ensure_chat_folder_path,
    _get_rows,
    _resolve_chat_folder_id,
    _resolve_chat_slot_key,
)
from kiro_crew.mcp_tools.dashboard_client import DashboardError
from kiro_crew.mcp_tools.table import ToolContext
from kiro_crew.platform import redact_via_context as redact


def _run_chat_folder_tree(args: dict[str, Any], ctx: ToolContext) -> str:
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return f"Error: {folders_err}"
    chat_slots, slots_err = _visible_chat_slots(ctx)
    if slots_err:
        return f"Error: {slots_err}"
    # Read AFTER the two rows reads, which are the ones that can legitimately
    # refuse (a filtered caller, a gone slot). A failed mode read does not
    # abort the listing -- the header says the order is assumed instead -- so
    # the tree stays readable when only its ordering is in doubt.
    sort_mode, mode_err = _chat_folder_sort_mode()
    tree_paths = _chat_folder_paths(chat_folders)
    # Group live sessions by folder up front so an id with no folder row left
    # (a slot pointing at a deleted folder) still surfaces under
    # "(unfiled)" instead of vanishing from the tree.
    known_ids = set(tree_paths)
    by_folder: dict[str, list[dict]] = {}
    for slot_row in chat_slots:
        fid = str(slot_row.get("folder_id") or "")
        by_folder.setdefault(fid if fid in known_ids else "", []).append(slot_row)

    def _session_line(row: dict, indent: str) -> str:
        bits = []
        if row.get("running"):
            bits.append("running")
        if row.get("pinned"):
            bits.append("pinned")
        if row.get("app"):
            bits.append(f"app:{row['app']}")
        suffix = f"  [{', '.join(bits)}]" if bits else ""
        title = str(row.get("title") or "(untitled)")
        return f"{indent}· {row.get('key', '?')}  {title}{suffix}"

    tree_lines = [
        f"\U0001f5c2\ufe0f Sidebar folder tree — {len(chat_folders)} folder"
        f"{'' if len(chat_folders) == 1 else 's'}, {len(chat_slots)} live session"
        f"{'' if len(chat_slots) == 1 else 's'} (folder order: {sort_mode}"
        f"{'' if not mode_err else ', assumed — could not read the dashboard settings: ' + mode_err}):"
    ]
    if sort_mode != FOLDER_SORT_DEFAULT:
        # The listing below is what the person sees, but a POSITION is a
        # stored-order concept: in a name or created sort the before/after
        # anchors chat_folder_move takes still write the stored (custom)
        # position, which this view does not display. Say so up front, or an
        # agent will "move A after B", re-read the tree, and see nothing move.
        tree_lines.append(
            f"Folders are sorted by {sort_mode}, so a before/after anchor passed "
            "to chat_folder_move sets the stored (custom) position without "
            "changing the order shown here; it becomes visible when the person "
            "switches the sidebar back to the custom folder order."
        )
    if sort_mode == "created":
        # A folder from before the ``created_at`` stamp existed has none, so
        # the created key puts it after every stamped row, in the stored order
        # -- the same rule the sidebar's ``folderCreated`` applies, and the
        # sidebar's menu says the same thing under its rows. An agent reading
        # a tree whose tail is in stored order must not take that tail for a
        # date order.
        unstamped = sum(1 for f in chat_folders if _chat_folder_created(f) is None)
        if unstamped:
            tree_lines.append(
                f"{unstamped} folder{'' if unstamped == 1 else 's'} with no created_at "
                "(made before the stamp existed) list last, in the stored (custom) "
                "order, not by date."
            )
    # Sidebar ORDER, not alphabetical (unless the person's mode IS by name).
    # This tool is how an agent reads the tree before repositioning a folder,
    # so listing it in any order the sidebar does not draw would show a
    # sequence the person never sees and make `before`/`after` a guess.
    for fid, depth in _chat_folder_render_order(chat_folders, sort_mode):
        fpath = tree_paths.get(fid, "?")
        row = next((f for f in chat_folders if str(f.get("id")) == fid), {})
        meta_bits = []
        if row.get("project_dir"):
            meta_bits.append(f"project={row['project_dir']}")
        if row.get("default_agent"):
            meta_bits.append(f"agent={row['default_agent']}")
        # What the folder DECLARES, so an agent proposing a folder.steering
        # card sees the list it replaces. An ancestor's line above is the
        # inherited part: the tree is already root-first.
        if isinstance(row.get("steering_dirs"), list) and row.get("steering_dirs"):
            meta_bits.append(f"steering=[{', '.join(str(p) for p in row['steering_dirs'])}]")
        # No archived count. The invariant this server holds is that nothing it
        # emits discloses a non-persistent session — and the folders endpoint's
        # ``history_count`` covers archived transcripts with no memory_mode to
        # filter on, so a folder holding one filed incognito conversation would
        # report it as a number. The live list is filtered in
        # ``_visible_chat_slots``; a count this server cannot prove clean is
        # simply not rendered.
        if row.get("hidden"):
            meta_bits.append("hidden")
        meta = f"  ({' · '.join(meta_bits)})" if meta_bits else ""
        tree_lines.append(f"{'  ' * depth}{fid}  {fpath}{meta}")
        for slot_row in _sidebar_group_order(by_folder.get(fid, [])):
            tree_lines.append(_session_line(slot_row, "  " * depth + "  "))
    unfiled = by_folder.get("", [])
    if unfiled:
        tree_lines.append("(unfiled — top level)")
        for slot_row in _sidebar_group_order(unfiled):
            tree_lines.append(_session_line(slot_row, "  "))
    if not chat_folders and not chat_slots:
        return "No sidebar folders and no live sessions."
    return redact("\n".join(tree_lines))


def _run_chat_folder_create(args: dict[str, Any], ctx: ToolContext) -> str:
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, "creating a folder")
    if gate:
        return gate
    # A '/' in a NAME is what makes a rendered path ambiguous (a folder named
    # "A/B" renders exactly like B inside A). The resolver refuses that
    # ambiguity; this tool must not manufacture more of it. The sidebar keeps
    # its freedom — a human can still name a folder anything.
    if "/" in str(args["name"]):
        return (
            "Error: a folder name cannot contain '/' — it would render "
            "identically to a nested path and become unaddressable by path. "
            "Create the parent and child separately, or use a different name."
        )
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return f"Error: {folders_err}"
    # mkdir -p over the parent path: resolve as far as the tree already
    # goes, then create each missing segment.
    parent_id, created_segments, parent_err = _ensure_chat_folder_path(
        str(args.get("parent") or ""), chat_folders, client=ctx.client, session_key=caller_key
    )
    made_note = f" (created parent path: {'/'.join(created_segments)})" if created_segments else ""
    if parent_err:
        return redact(f"Error: {parent_err}{made_note}")
    # Agent-authored name landing in durable, re-rendered state — redact
    # before the write, like the created parent segments above.
    #
    # Then check the LENGTH of the redacted form, because that is what gets
    # stored: the schema caps the caller's `name` at _MAX_FOLDER_NAME, but
    # redaction can make a string LONGER (a credential becomes a placeholder),
    # so a name that passed validation can still overrun. The endpoint stores
    # ``name[:100]``, and a silently truncated name is one no later path can
    # match — the same mismatch that makes the walk refuse an overlong
    # segment, so it is refused the same way here.
    safe_name = redact(args["name"])
    if len(safe_name) > _MAX_FOLDER_NAME:
        return (
            f"Error: folder name too long after redaction ({len(safe_name)} "
            f"chars): `{safe_name[:40]}…` — keep it to {_MAX_FOLDER_NAME} "
            "characters or fewer"
        )
    body = {"name": safe_name, "parent_id": parent_id}
    # The verified key is passed through unchanged: re-resolving inside the
    # client would let the write carry a different session's authority than
    # the one the gate checked, and the endpoint's ownership rule is only as
    # good as the identity that reaches it.
    try:
        d = ctx.client.post("/api/chat/folders", body, session_key=caller_key)
    except DashboardError as refused:
        return redact(f"Error: {refused.error}{made_note}")
    chat_folders.append(d)
    new_id = str(d.get("id") or "?")
    new_path = _chat_folder_paths(chat_folders).get(new_id) or str(d.get("name") or "?")
    return redact(f"Created folder `{new_path}` (id={new_id}).{made_note}")


def _run_chat_folder_move(args: dict[str, Any], ctx: ToolContext) -> str:
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, "moving a folder")
    if gate:
        return gate
    before_ref = str(args.get("before") or "").strip()
    after_ref = str(args.get("after") or "").strip()
    if before_ref and after_ref:
        return "Error: pass `before` or `after`, not both — one anchor names one position."
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return f"Error: {folders_err}"
    fld_id, fld_err = _resolve_chat_folder_id(args["folder"], chat_folders)
    if fld_err:
        return f"Error: {fld_err}"
    if not fld_id:
        return "Error: 'root' is not a folder — name the folder to move."
    anchor_ref = before_ref or after_ref
    anchor_id = ""
    if anchor_ref:
        anchor_id, anchor_err = _resolve_chat_folder_id(anchor_ref, chat_folders)
        if anchor_err:
            return f"Error: {anchor_err}"
        if not anchor_id:
            return (
                "Error: 'root' is not a folder — `before`/`after` names a "
                "SIBLING folder to sit next to."
            )
        if anchor_id == fld_id:
            return "Error: a folder cannot be positioned relative to itself."
    anchor_row = next((f for f in chat_folders if str(f.get("id")) == anchor_id), {})
    anchor_parent = str(anchor_row.get("parent_id") or "")
    current_parent = str(
        next((f for f in chat_folders if str(f.get("id")) == fld_id), {}).get("parent_id") or ""
    )
    if anchor_id and "new_parent" not in args:
        # An anchor already fixes the sibling set, so "put X after Y" needs no
        # second reference to the parent Y names — and demanding one would make
        # repositioning INSIDE a folder impossible to express, since an omitted
        # ``new_parent`` means the top level.
        dest_id = anchor_parent
    else:
        dest_id, dest_err = _resolve_chat_folder_id(args.get("new_parent") or "", chat_folders)
        if dest_err:
            return f"Error: {dest_err}"
        if anchor_id and anchor_parent != dest_id:
            return (
                "Error: the anchor is not in the destination — `before`/`after` "
                "names a SIBLING, so it must already sit directly under "
                "`new_parent`. Omit `new_parent` to let the anchor choose the "
                "parent."
            )

    # One PATCH carries the whole move: the reparent (only when the parent
    # changes) and the anchor. The gateway picks the rank under the folder
    # store lock, so the position is decided against the tree as it is at
    # the write, and it enforces every ownership rule there: an app may move
    # only its own folder, into one of its own, and not one whose subtree
    # holds the person's. If assigning the rank requires a section re-spread,
    # every sibling whose rank would change must also belong to the app.
    #
    # ``parent_id`` is omitted when the parent is NOT changing: sending the
    # current parent again is a no-op for position.
    move_body: dict[str, Any] = {}
    if current_parent != dest_id:
        move_body["parent_id"] = dest_id
    if anchor_id:
        move_body["before" if before_ref else "after"] = anchor_id
        # The anchor is checked against the siblings as they are at the write,
        # which a deleted parent changes: its children are promoted to the
        # root and are still siblings there. Naming the container this call
        # read keeps that from landing as a root reorder nobody asked for.
        # A parent missing from the snapshot is either gone or hidden from a
        # crew member's filtered view. The gateway checks against the full
        # store, so no single value matches in both cases, and the
        # precondition is left out for that folder.
        known_ids = {str(f.get("id")) for f in chat_folders}
        if not current_parent or current_parent in known_ids:
            move_body["expected_parent"] = current_parent
    if move_body:
        try:
            d = ctx.client.patch(f"/api/chat/folders/{fld_id}", move_body, session_key=caller_key)
        except DashboardError as refused:
            # The endpoint owns the cycle, ownership and anchor checks --
            # surface its verdict rather than re-deriving it.
            return f"Error: {refused.error}"
    else:
        # Nothing to write: already in the destination, and no position asked.
        d = next((f for f in chat_folders if str(f.get("id")) == fld_id), {})
    moved: list[dict] = [f for f in chat_folders if str(f.get("id")) != fld_id]
    moved.append({**d, "id": fld_id})
    dest_path = _chat_folder_paths(moved).get(fld_id) or "(top level)"
    if anchor_id:
        side = "before" if before_ref else "after"
        anchor_path = _chat_folder_paths(chat_folders).get(anchor_id) or anchor_id
        if current_parent == dest_id:
            # Nothing moved, so saying "moved to <the folder's own path>" would
            # describe a reparent that did not happen. Name what changed.
            parent_label = _chat_folder_paths(chat_folders).get(dest_id) or "(top level)"
            return redact(
                f"Repositioned folder (id={fld_id}) {side} `{anchor_path}` " f"in `{parent_label}`."
            )
        return redact(
            f"Moved folder (id={fld_id}) to `{dest_path}`, positioned {side} " f"`{anchor_path}`."
        )
    return redact(f"Moved folder (id={fld_id}) to `{dest_path}`.")


def _run_chat_folder_update(args: dict[str, Any], ctx: ToolContext) -> str:
    # The body is built from these three fields only, so no other folder field
    # can reach the PATCH even if the schema later grows one. The schema does
    # not list the keep-off fields, so validation refuses them as unknown.
    folder_changes: dict[str, Any] = {}
    # Test the VALUE, not the key: the schema keeps an explicit JSON null as
    # ``None``, and ``str(None)`` would rename the folder to "None".
    if args.get("name") is not None:
        # Same rules as chat_folder_create: no '/', redact before the write, and
        # check the stored (redacted) length, because the endpoint keeps
        # ``name[:100]`` and a truncated name no later path lookup matches.
        if "/" in str(args["name"]):
            return (
                "Error: a folder name cannot contain '/' — it would render "
                "identically to a nested path and become unaddressable by path."
            )
        safe_name = redact(str(args["name"])).strip()
        if not safe_name:
            return "Error: folder name must not be empty"
        if len(safe_name) > _MAX_FOLDER_NAME:
            return (
                f"Error: folder name too long after redaction ({len(safe_name)} "
                f"chars): `{safe_name[:40]}…` — keep it to {_MAX_FOLDER_NAME} "
                "characters or fewer"
            )
        folder_changes["name"] = safe_name
    # Icon and color pass through as given: '' clears, and the endpoint's
    # single-emoji check and palette allowlist are the authority for the rest.
    if args.get("icon") is not None:
        folder_changes["icon"] = str(args["icon"])
    if args.get("color") is not None:
        folder_changes["color"] = str(args["color"]).lower()
    if not folder_changes:
        return "Error: pass at least one of ``name``, ``icon`` or ``color``"
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, "updating a folder")
    if gate:
        return gate
    # Channel callers are refused by the endpoint (403 ``channel_reachable_caller``),
    # which sees every channel-shaped key and the channel bindings of a dashboard
    # session; the CHANNEL_AGENT_BLOCKED_TOOLS entry hides the tool at the
    # permission prompt.
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return f"Error: {folders_err}"
    fld_id, fld_err = _resolve_chat_folder_id(args["folder"], chat_folders)
    if fld_err:
        return f"Error: {fld_err}"
    if not fld_id:
        return "Error: 'root' is not a folder — name the folder to update."
    before = next((f for f in chat_folders if str(f.get("id")) == fld_id), {})
    before_path = _chat_folder_paths(chat_folders).get(fld_id) or str(before.get("name") or fld_id)
    # Ownership (an app or crew member may update only a folder it created) and
    # the sibling-name rule are the endpoint's, decided under the folder-store
    # lock over the whole tree rather than the part this caller can read.
    try:
        ctx.client.patch(
            f"/api/chat/folders/{quote(fld_id, safe='')}", folder_changes, session_key=caller_key
        )
    except DashboardError as refused:
        if refused.code == "folder_name_exists":
            return (
                "Error: a sibling folder already has that name. Two folders "
                "with one name under the same parent cannot be told apart by "
                "path, so the rename is refused."
            )
        if refused.code == "folder_not_owned":
            return (
                "Error: this caller does not own that folder. An app agent or "
                "crew member may update only a folder it created; ask the "
                "person to rename or restyle theirs."
            )
        if refused.code == "channel_reachable_caller":
            # A channel conversation resumed into this dashboard session runs
            # under its ``dashboard:`` key, so only the endpoint's reachability
            # check can see it.
            return (
                "Error: this session is linked to a channel conversation, so its "
                "turns may come from thread text other people wrote and "
                "chat_folder_update is refused. Unlink the channel, or rename "
                "the folder in the sidebar."
            )
        return redact(f"Error: {refused.error}")
    # Report what was written, not the response row: the endpoint returns the
    # folder object it looked up before taking the store lock.
    parts = []
    if "name" in folder_changes:
        parts.append(f"renamed to `{folder_changes['name']}`")
    if "icon" in folder_changes:
        parts.append(f"icon {folder_changes['icon'] or '(default)'}")
    if "color" in folder_changes:
        parts.append(f"color {folder_changes['color'] or '(default)'}")
    return redact(f"Updated folder `{before_path}` (id={fld_id}): {'; '.join(parts)}.")


def _run_chat_folder_move_session(args: dict[str, Any], ctx: ToolContext) -> str:
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return f"Error: {folders_err}"
    fld_id, fld_err = _resolve_chat_folder_id(args.get("folder") or "", chat_folders)
    if fld_err:
        return f"Error: {fld_err}"
    chat_slots, slots_err = _visible_chat_slots(ctx)
    if slots_err:
        return f"Error: {slots_err}"
    slot_key, slot_err = _resolve_chat_slot_key(args["session"], chat_slots)
    if slot_err:
        # The refusal echoes candidate slot keys, and a slot key can be a
        # folded human name — redact like every other egress here.
        return redact(f"Error: {slot_err}")
    # This is the one tool here that writes to a session OTHER than the
    # caller's, so it resolves identity STRICTLY: only the gateway-injected
    # per-call caller context, the injected env var, or an HMAC-verified pid
    # count. The lenient resolver's /proc ancestor walk would resolve a
    # subagent to its parent slot, handing it the parent's authority — and
    # an unresolved identity reaches the endpoint as no header at all, where
    # it reads as the unconfined dashboard user. Refuse instead of writing
    # with an authority we cannot name.
    caller_key, strict_err = ctx.caller.require_strict_session_key(
        "Error: cannot verify which session is calling, so this move is "
        "refused — filing another session requires a caller identity the "
        "gateway can vouch for."
    )
    if not caller_key:
        return strict_err
    # The verified key is passed through unchanged: re-resolving inside the
    # client would let the write carry a different session's authority than
    # the one checked here.
    try:
        ctx.client.patch(
            f"/api/chat/slots/{quote(slot_key, safe='')}/folder",
            {"folder_id": fld_id},
            session_key=caller_key,
        )
    except DashboardError as refused:
        return f"Error: {refused.error}"
    if not fld_id:
        return redact(f"Unfiled session `{slot_key}` to the top level.")
    folder_label = _chat_folder_paths(chat_folders).get(fld_id, fld_id)
    return redact(f"Moved session `{slot_key}` into `{folder_label}` (id={fld_id}).")


def _run_chat_folder_delete(args: dict[str, Any], ctx: ToolContext) -> str:
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, "deleting a folder")
    if gate:
        return gate
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return f"Error: {folders_err}"
    fld_id, fld_err = _resolve_chat_folder_id(args["folder"], chat_folders)
    if fld_err:
        return redact(f"Error: {fld_err}")
    if not fld_id:
        return "Error: 'root' is not a folder — name the folder to delete."
    fld_path = _chat_folder_paths(chat_folders).get(fld_id) or fld_id
    # Empty-only, decided by the endpoint: ``if_empty`` makes it re-check
    # subfolders and live sessions under the folder-store lock in the same
    # step that removes the row, so nothing filed after this tool's read can
    # be unfiled by the delete. A pre-check here could not give that answer.
    # The refusal text names no session and carries no count.
    try:
        ctx.client.delete(
            f"/api/chat/folders/{quote(fld_id, safe='')}?if_empty=true",
            session_key=caller_key,
        )
    except DashboardError as refused:
        if refused.code == "folder_not_agent_owned":
            return redact(
                f"Error: folder `{fld_path}` is not deleted: this session did not "
                "create it, or the person has edited or used it since. Leave it "
                "for the person."
            )
        if refused.code == "folder_not_empty":
            return redact(
                f"Error: folder `{fld_path}` is not deleted: {refused.error}. Empty it "
                "first with chat_folder_move / chat_folder_move_session (an "
                "archived session needs session_revive before it can move)."
            )
        return redact(f"Error: {refused.error}")
    return redact(f"Deleted empty folder `{fld_path}` (id={fld_id}).")


def _run_chat_folder_file_self(args: dict[str, Any], ctx: ToolContext) -> str:
    # The destination may not exist yet (mkdir -p, like session_create's
    # ``folder``), and creating folders is tree shaping — so the same gate,
    # not a second authorization path. Its verified key is what every write
    # below carries, per the gate's own contract.
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, "filing this session")
    if gate:
        return gate
    rows, rows_err = _get_rows(ctx.client, "/api/chat/slots")
    if rows_err:
        return redact(f"Error: {rows_err}")
    own_slot, own_err = _own_chat_slot(caller_key, rows)
    if own_err:
        return own_err
    own_key = str(own_slot.get("key") or "")
    # The row's ``created`` is the slot's birth stamp, minted once per slot
    # object. It rides along on the PATCH as a generation token so the
    # endpoint refuses (409 ``session_gone``) if this tab closed and its key
    # was recreated for another conversation between this read and the
    # write — the recreated slot shares the ``dashboard:<key>`` transcript
    # key, so the history pin alone would let that write through.
    own_created = str(own_slot.get("created") or "")
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return redact(f"Error: {folders_err}")
    folder_ref = str(args.get("folder") or "")
    made_note = ""
    fld_id = ""
    if folder_ref and folder_ref != "root":
        fld_id, created_segments, fld_err = _ensure_chat_folder_path(
            folder_ref, chat_folders, client=ctx.client, session_key=caller_key
        )
        if created_segments:
            made_note = f" (created folder path: {'/'.join(created_segments)})"
        if fld_err:
            # Segments the walk already created persist and are reported —
            # the same partial-report posture chat_folder_create and
            # session_create take, since folder deletion is deliberately not
            # a capability this server has.
            return redact(f"Error: {fld_err}{made_note}")
    # The target is the CALLER's own slot, resolved above from the verified
    # key — never from an argument — which is what makes this verb safe to
    # grant where chat_folder_move_session is withheld: it can write no
    # placement but its own. ``expected_created`` pins the write to the
    # slot generation resolved above; the endpoint checks it under its lock.
    patch_body: dict[str, str] = {"folder_id": fld_id}
    if own_created:
        patch_body["expected_created"] = own_created
    try:
        ctx.client.patch(
            f"/api/chat/slots/{quote(own_key, safe='')}/folder",
            patch_body,
            session_key=caller_key,
        )
    except DashboardError as refused:
        return redact(f"Error: {refused.error}{made_note}")
    if not fld_id:
        return redact(f"Unfiled this session (`{own_key}`) to the top level.{made_note}")
    folder_label = _chat_folder_paths(chat_folders).get(fld_id, fld_id)
    return redact(f"Filed this session (`{own_key}`) in `{folder_label}` (id={fld_id}).{made_note}")
