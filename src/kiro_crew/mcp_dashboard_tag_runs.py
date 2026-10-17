"""What each tag, pin and board tool of the ``kirocrew-dashboard`` server does when called.

The ``_run_chat_tag_*``, ``_run_chat_session_pin`` and ``_run_chat_session_pin_move``
bodies behind ``_tag_tools``, and the board-column and move-to-column bodies behind
``_board_tools`` (both rows in :mod:`kiro_crew.mcp_dashboard`), with the renderers of the
tag vocabulary and of the board columns. Each body takes ``(args, ctx)``.
:mod:`kiro_crew.mcp_dashboard` binds every name defined here, so each one reads, imports
and patches as ``kiro_crew.mcp_dashboard.<name>``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from kiro_crew.mcp_dashboard_caller_scope import (
    SERVER_NAME,
    _caller_app_scope,
    _is_messaging_caller,
    _refuse_channel_board_write,
    _refuse_tree_shaping_if_unverifiable,
    _visible_chat_slots,
)
from kiro_crew.mcp_dashboard_folder_tree import (
    _chat_folder_order,
    _chat_folder_paths,
    _pinned_sidebar_order,
    _slot_group,
)
from kiro_crew.mcp_dashboard_references import (
    _get_rows,
    _resolve_chat_slot_key,
    _resolve_chat_tag_column,
    _resolve_chat_tag_ids,
)
from kiro_crew.mcp_tools.dashboard_client import DashboardError
from kiro_crew.mcp_tools.table import ToolContext
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.sel import sel

# The same mirror as ``_MAX_FOLDER_NAME`` (``mcp_dashboard_references``), for
# tags: the tag endpoints store ``name[:60]``
# (``chat_tags._NAME_MAX``), and a silently truncated name is one no later
# ``chat_tag_assign`` name lookup can match.
_MAX_TAG_NAME = 60


def _render_chat_tags(tags: list[dict]) -> str:
    """One line per tag: id, name, color, and the status marker."""
    if not tags:
        return "No tags defined yet — chat_tag_create makes one."
    lines = [f"\U0001f3f7\ufe0f Tag vocabulary — {len(tags)} tag{'' if len(tags) == 1 else 's'}"]
    # Same coercion as folder rows: ``tags.json`` is loaded verbatim, so a
    # hand-edited ``order`` must sort as 0 rather than end the tool call.
    for t in sorted(tags, key=_chat_folder_order):
        marker = "  [status]" if t.get("status") else ""
        lines.append(
            f"- `{t.get('name', '?')}`  id={t.get('id', '?')}  color={t.get('color', '?')}{marker}"
        )
    return "\n".join(lines)


def _chat_tag_column_label(col: dict, tag_names: dict[str, str]) -> str:
    """What a board column shows, in words: its tag filter or its live-state lane."""
    if col.get("source") == "state":
        return f"live state `{col.get('state_key') or '?'}`"
    # ``tag_columns.json`` is loaded verbatim, so a hand-edited ``tag_ids``
    # that is not a list must render as "no filter" rather than end the call.
    raw_ids = col.get("tag_ids")
    tag_ids = [str(t) for t in raw_ids if isinstance(t, str)] if isinstance(raw_ids, list) else []
    if not tag_ids:
        # Mirrors ``columnMatches`` in the board UI: an empty filter with
        # ``include_untagged`` on is the "Untagged" column (untagged sessions
        # only); without the flag it is "all sessions". Keyed on the filter
        # fields alone, never the display name.
        return "untagged sessions only" if col.get("include_untagged") else "all sessions"
    names = ", ".join(f"`{tag_names.get(t, t)}`" for t in tag_ids)
    mode = str(col.get("mode") or "any")
    label = f"tags {names} (match {mode})"
    if col.get("include_untagged"):
        label += " + untagged sessions"
    return label


def _render_chat_tag_columns(columns: list[dict], tags: list[dict]) -> str:
    """One line per board column, in board order (the endpoint returns it sorted)."""
    if not columns:
        return "The board has no columns yet — chat_tag_column_create adds one."
    tag_names = {str(t.get("id") or ""): str(t.get("name") or "?") for t in tags}
    lines = [
        f"\U0001f5c2\ufe0f Board columns — {len(columns)} column{'' if len(columns) == 1 else 's'}"
    ]
    for pos, col in enumerate(columns, start=1):
        name = str(col.get("name") or "").strip() or "(unnamed)"
        lines.append(
            f"{pos}. `{name}`  id={col.get('id', '?')}  shows "
            f"{_chat_tag_column_label(col, tag_names)}"
        )
    return "\n".join(lines)


def _run_chat_tag_list(args: dict[str, Any], ctx: ToolContext) -> str:
    # The vocabulary is one shared list of labels with no per-session or
    # per-app content in it — nothing here names a session — so it needs no
    # caller scoping, unlike the slot list every other read here goes through.
    tags, tags_err = _get_rows(ctx.client, "/api/chat/tags")
    if tags_err:
        return f"Error: {tags_err}"
    return redact(_render_chat_tags(tags))


def _run_chat_tag_create(args: dict[str, Any], ctx: ToolContext) -> str:
    # Same gate as the folder writes: it settles whether the caller can be
    # placed at all and returns the verified key the write must carry. The
    # app rule itself — an app-scoped caller may not coin a shared tag —
    # lives in the endpoint (``api_chat_tag_create``), which judges every
    # transport on the middleware's validated claim; restating it here
    # would be a second copy that can only drift.
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, "creating a tag")
    if gate:
        return gate
    # Agent-authored name landing in durable, re-rendered state — redact
    # before the write, like folder names. The endpoint stores ``name[:60]``,
    # and redaction can lengthen a string (a credential becomes a marker), so
    # the length is checked on what would be stored: a truncated name is one
    # no later chat_tag_assign name lookup can match.
    safe_name = redact(str(args["name"])).strip()
    if not safe_name:
        return "Error: tag name must not be empty"
    if len(safe_name) > _MAX_TAG_NAME:
        return (
            f"Error: tag name too long after redaction ({len(safe_name)} chars): "
            f"`{safe_name[:40]}…` — keep it to {_MAX_TAG_NAME} characters or fewer"
        )
    tag_body: dict[str, Any] = {"name": safe_name, "status": bool(args.get("status", False))}
    if args.get("color"):
        tag_body["color"] = str(args["color"])
    # The verified key is passed through unchanged, per the gate's contract.
    try:
        d = ctx.client.post("/api/chat/tags", tag_body, session_key=caller_key)
    except DashboardError as refused:
        if refused.code == "app_forbidden":
            return (
                "Error: an app-owned session cannot create a tag — tags are one "
                "shared vocabulary with no per-app owner. Use the tags that already "
                "exist (chat_tag_list)."
            )
        return redact(f"Error: {refused.error}")
    tid = str(d.get("id") or "?")
    got_name = str(d.get("name") or safe_name)
    marker = " (status tag)" if d.get("status") else ""
    if got_name.lower() != safe_name.lower():
        # Cannot happen through the endpoint's own dedup (it matches on the
        # lowered name), but the response is the record: report what exists.
        return redact(f"Tag `{got_name}` (id={tid}){marker} already covers `{safe_name}`.")
    return redact(f"Tag `{got_name}` (id={tid}, color={d.get('color', '?')}){marker} is available.")


def _run_chat_tag_update(args: dict[str, Any], ctx: ToolContext) -> str:
    changes: dict[str, Any] = {}
    if args.get("name") is not None:
        # Agent-authored name landing in durable state — redact before the
        # write and check the stored length, exactly as chat_tag_create does.
        safe_name = redact(str(args["name"])).strip()
        if not safe_name:
            return "Error: tag name must not be empty"
        if len(safe_name) > _MAX_TAG_NAME:
            return (
                f"Error: tag name too long after redaction ({len(safe_name)} chars): "
                f"`{safe_name[:40]}…` — keep it to {_MAX_TAG_NAME} characters or fewer"
            )
        changes["name"] = safe_name
    if args.get("color"):
        changes["color"] = str(args["color"])
    if args.get("status") is not None:
        changes["status"] = bool(args["status"])
    if not changes:
        return "Error: pass at least one of ``name``, ``color`` or ``status``"
    # Same gate as the other vocabulary write: whether the caller can be
    # placed at all, and the verified key the write must carry. The app
    # rule lives in the endpoint.
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, "updating a tag")
    if gate:
        return gate
    tags, tags_err = _get_rows(ctx.client, "/api/chat/tags")
    if tags_err:
        return f"Error: {tags_err}"
    ids, ref_err = _resolve_chat_tag_ids([str(args["tag"])], tags)
    if ref_err:
        return redact(f"Error: {ref_err}")
    tid = ids[0]
    before = next((t for t in tags if str(t.get("id")) == tid), {})
    try:
        d = ctx.client.patch(
            f"/api/chat/tags/{quote(tid, safe='')}", changes, session_key=caller_key
        )
    except DashboardError as refused:
        if refused.code == "app_forbidden":
            return (
                "Error: an app-owned session cannot change a tag — tags are one "
                "shared vocabulary with no per-app owner."
            )
        return redact(f"Error: {refused.error}")
    parts = []
    if "name" in changes:
        parts.append(f"renamed `{before.get('name', '?')}` → `{d.get('name', changes['name'])}`")
    if "color" in changes:
        parts.append(f"color {before.get('color', '?')} → {d.get('color', changes['color'])}")
    if "status" in changes:
        parts.append(f"status tag: {'yes' if d.get('status') else 'no'}")
    return redact(f"Updated tag `{d.get('name', '?')}` (id={tid}): {'; '.join(parts)}.")


def _run_chat_tag_assign(args: dict[str, Any], ctx: ToolContext) -> str:
    add_refs = [str(x) for x in (args.get("add") or [])]
    remove_refs = [str(x) for x in (args.get("remove") or [])]
    if not add_refs and not remove_refs:
        return "Error: pass at least one tag in ``add`` or ``remove``"
    tags, tags_err = _get_rows(ctx.client, "/api/chat/tags")
    if tags_err:
        return f"Error: {tags_err}"
    add_ids, add_err = _resolve_chat_tag_ids(add_refs, tags)
    if add_err:
        return redact(f"Error: {add_err}")
    remove_ids, remove_err = _resolve_chat_tag_ids(remove_refs, tags)
    if remove_err:
        return redact(f"Error: {remove_err}")
    clash = [t for t in add_ids if t in remove_ids]
    if clash:
        return f"Error: {', '.join(clash)} named in both ``add`` and ``remove``"
    chat_slots, slots_err = _visible_chat_slots(ctx)
    if slots_err:
        return f"Error: {slots_err}"
    slot_key, slot_err = _resolve_chat_slot_key(args["session"], chat_slots)
    if slot_err:
        return redact(f"Error: {slot_err}")
    slot_row = next((s for s in chat_slots if str(s.get("key") or "") == slot_key), {})
    current = [str(t) for t in (slot_row.get("tags") or []) if isinstance(t, str)]
    new_tags = [t for t in current if t not in remove_ids]
    for tid in add_ids:
        if tid not in new_tags:
            new_tags.append(tid)
    # Like chat_folder_move_session, this writes to a session OTHER than the
    # caller's, so identity is resolved STRICTLY and the verified key rides
    # on the write unchanged — see that tool for why the lenient walk is
    # unsafe here.
    caller_key, strict_err = ctx.caller.require_strict_session_key(
        "Error: cannot verify which session is calling, so this tag change is "
        "refused — tagging another session requires a caller identity the "
        "gateway can vouch for."
    )
    if not caller_key:
        return strict_err
    names_by_id = {str(t.get("id") or ""): str(t.get("name") or "?") for t in tags}
    if new_tags == current:
        shown = ", ".join(f"`{names_by_id.get(t, t)}`" for t in current) or "none"
        return redact(f"No change: session `{slot_key}` already carries {shown}.")
    # The revision the list above was composed on. The endpoint applies the
    # write compare-and-set against it, so a tag the person toggles between
    # this read and the PUT is not silently dropped by a wholesale replace —
    # the call fails 409 ``stale_base`` and is retried on the fresh list.
    put_body: dict[str, Any] = {"tags": new_tags}
    base_rev = str(slot_row.get("tags_revision") or "")
    if base_rev:
        put_body["base_tags_revision"] = base_rev
    try:
        d = ctx.client.put(
            f"/api/chat/slots/{quote(slot_key, safe='')}/tags",
            put_body,
            session_key=caller_key,
        )
    except DashboardError as refused:
        if refused.code == "stale_base":
            return redact(
                f"Error: the tags on `{slot_key}` changed while this call was "
                "composing its delta (someone else toggled a tag). Nothing was "
                "written — call chat_tag_assign again; it re-reads the current list."
            )
        return redact(f"Error: {refused.error}")
    final = [str(t) for t in (d.get("tags") or new_tags) if isinstance(t, str)]
    shown = ", ".join(f"`{names_by_id.get(t, t)}`" for t in final) or "none"
    added = ", ".join(f"`{names_by_id.get(t, t)}`" for t in add_ids if t not in current)
    removed = ", ".join(f"`{names_by_id.get(t, t)}`" for t in remove_ids if t in current)
    parts = []
    if added:
        parts.append(f"added {added}")
    if removed:
        parts.append(f"removed {removed}")
    return redact(f"Session `{slot_key}`: {'; '.join(parts)}. Tags now: {shown}.")


def _run_chat_session_pin(args: dict[str, Any], ctx: ToolContext) -> str:
    want = args["pinned"]
    # Like chat_folder_move_session, this writes to a session OTHER than the
    # caller's, so identity is resolved STRICTLY and the verified key rides
    # on the write unchanged — see that tool for why the lenient walk is
    # unsafe here.
    caller_key, strict_err = ctx.caller.require_strict_session_key(
        "Error: cannot verify which session is calling, so this pin change is "
        "refused — pinning another session requires a caller identity the "
        "gateway can vouch for."
    )
    if not caller_key:
        return strict_err
    # Channel containment is enforced HERE, at dispatch, and not only by the
    # CHANNEL_AGENT_BLOCKED_TOOLS name match: that match runs at the
    # permission prompt, which an auto-approved call never reaches. A
    # channel agent acts on thread text other people wrote, and nothing at
    # the route refuses a ``channel:`` caller, so it is refused before any
    # session is listed.
    if caller_key.startswith("channel:"):
        try:
            sel().log_tool_invocation(
                session_key=caller_key,
                source="mcp",
                tool_name="chat_session_pin",
                tool_kind=SERVER_NAME,
                outcome="rejected_blocked_tool",
            )
        except Exception:
            # Stdio-silent: stderr would corrupt the JSON-RPC stream. The
            # refusal below holds either way.
            pass
        return (
            "Error: chat_session_pin is not available to channel agents — "
            "pinning rearranges the person's sidebar, and a channel agent acts "
            "on thread text other people wrote."
        )
    chat_slots, slots_err = _visible_chat_slots(ctx)
    if slots_err:
        return f"Error: {slots_err}"
    slot_key, slot_err = _resolve_chat_slot_key(args["session"], chat_slots)
    if slot_err:
        return redact(f"Error: {slot_err}")
    verb = "Pinned" if want else "Unpinned"
    slot_row = next((s for s in chat_slots if str(s.get("key") or "") == slot_key), {})
    # No client-side "already in that state" shortcut: the list read above
    # can be stale, so the route decides under its lock, after the
    # generation and ownership re-checks, and reports ``changed``.
    # ``expected_created`` pins the write to the slot generation resolved
    # above, the same token chat_folder_file_self sends; the endpoint
    # checks it under its lock, so a slot key recreated for a different
    # conversation in between is refused instead of pinned.
    pin_body: dict[str, Any] = {"pinned": want}
    slot_created = str(slot_row.get("created") or "")
    if slot_created:
        pin_body["expected_created"] = slot_created
    try:
        d = ctx.client.patch(
            f"/api/chat/slots/{quote(slot_key, safe='')}/pin",
            pin_body,
            session_key=caller_key,
        )
    except DashboardError as refused:
        if refused.code == "session_gone":
            return redact(
                f"Error: session `{slot_key}` closed or was replaced after it was "
                "resolved. Nothing was written — call chat_folder_tree to see "
                "the current sessions."
            )
        return redact(f"Error: {refused.error}")
    if d.get("changed") is False:
        state_word = "pinned" if want else "not pinned"
        return redact(f"No change: session `{slot_key}` is already {state_word}.")
    return redact(f"{verb} session `{slot_key}`.")


def _run_chat_session_pin_move(args: dict[str, Any], ctx: ToolContext) -> str:
    """Reposition one pinned session next to another pinned session in its group."""
    before_ref = str(args.get("before") or "").strip()
    after_ref = str(args.get("after") or "").strip()
    if bool(before_ref) == bool(after_ref):
        return (
            "Error: pass exactly one of `before` or `after`, naming the pinned "
            "session to sit next to."
        )
    caller_key, strict_err = ctx.caller.require_strict_session_key(
        "Error: cannot verify which session is calling, so this reorder is refused "
        "- the pinned order is the person's, and only a caller the gateway can "
        "vouch for may change it."
    )
    if not caller_key:
        return strict_err
    # Messaging containment at dispatch: a channel agent or a messaging-thread
    # session acts on text other people wrote, it reads as the person below,
    # and an auto-approved call never reaches the blocked-name permission
    # prompt. Every messaging key form is refused: the channel agent's
    # ``channel:``, each transport's ``<ns>:``/``<ns>_`` key, and a bare Slack
    # thread timestamp.
    if _is_messaging_caller(caller_key):
        try:
            sel().log_tool_invocation(
                session_key=caller_key,
                source="mcp",
                tool_name="chat_session_pin_move",
                tool_kind=SERVER_NAME,
                outcome="rejected_blocked_tool",
            )
        except Exception:
            # Stdio-silent: stderr would corrupt the JSON-RPC stream. The
            # refusal below holds either way.
            pass
        return (
            "Error: chat_session_pin_move is not available to channel agents or "
            "messaging-channel sessions — the pinned order is the person's sidebar, "
            "and those sessions act on text other people wrote."
        )
    rows, rows_err = _get_rows(ctx.client, "/api/chat/slots")
    if rows_err:
        return redact(f"Error: {rows_err}")
    scope = _caller_app_scope(caller_key, rows)
    if scope is None or scope:
        return (
            "Error: the pinned order is the person's own sidebar preference and "
            "spans sessions no app owns, so an app-scoped or delegated caller cannot "
            "reorder it. Ask the person to drag the session in the sidebar."
        )
    visible = [r for r in rows if str(r.get("memory_mode") or "persistent") == "persistent"]
    slot_key, slot_err = _resolve_chat_slot_key(args["session"], visible)
    if slot_err:
        return redact(f"Error: {slot_err}")
    anchor_key, anchor_err = _resolve_chat_slot_key(before_ref or after_ref, visible)
    if anchor_err:
        return redact(f"Error: anchor: {anchor_err}")
    if anchor_key == slot_key:
        return "Error: the anchor is the session being moved; name a different pinned session."
    by_key = {str(r.get("key") or ""): r for r in visible}
    moving, anchor = by_key[slot_key], by_key[anchor_key]
    if not moving.get("pinned"):
        return redact(
            f"Error: `{slot_key}` is not pinned. Only pinned sessions have an order; "
            "pin it in the sidebar first."
        )
    if not anchor.get("pinned"):
        return redact(f"Error: the anchor `{anchor_key}` is not pinned; name a pinned session.")
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return redact(f"Error: {folders_err}")
    known_ids = set(_chat_folder_paths(chat_folders))
    group = _slot_group(moving, known_ids)
    if _slot_group(anchor, known_ids) != group:
        return redact(
            f"Error: `{anchor_key}` is pinned in a different sidebar group than "
            f"`{slot_key}`. Pinned sessions are ordered within their folder (or the "
            "top level); name an anchor in the same group."
        )
    # Send the WHOLE pinned order, the way a sidebar drag does, so every pinned
    # session ends up ranked. Sending only this group would leave the other
    # groups unranked, and a session pinned there later would be appended
    # ranked and jump ahead of its unranked neighbours. The order is built from
    # every row, incognito and temporary ones included, so their places in the
    # person's sidebar stay where they were; their keys go only to the gateway
    # and never into this tool's reply.
    order = [str(r.get("key") or "") for r in _pinned_sidebar_order(rows)]
    order.remove(slot_key)
    at = order.index(anchor_key) + (0 if before_ref else 1)
    order.insert(at, slot_key)
    # Each key's ``created`` pins the reorder to the slot generation this read
    # saw, as ``expected_created`` does for chat_session_pin: a key closed and
    # recreated in between is refused instead of ranked.
    named = set(order)
    expected_created = {
        str(r.get("key") or ""): str(r.get("created") or "")
        for r in rows
        if str(r.get("key") or "") in named and r.get("created")
    }
    try:
        ctx.client.post(
            "/api/chat/pinned-order",
            {"keys": order, "expected_created": expected_created},
            session_key=caller_key,
        )
    except DashboardError as refused:
        return redact(f"Error: could not reorder pinned sessions: {refused.error}")
    where = f"before `{anchor_key}`" if before_ref else f"after `{anchor_key}`"
    group_label = _chat_folder_paths(chat_folders).get(group, group) if group else "the top level"
    in_group = [
        key for key in order if key in by_key and _slot_group(by_key[key], known_ids) == group
    ]
    position = in_group.index(slot_key) + 1
    return redact(
        f"Moved pinned session `{slot_key}` {where} in {group_label}; it is now "
        f"pinned #{position} of {len(in_group)} there."
    )


def _run_chat_tag_column_list(args: dict[str, Any], ctx: ToolContext) -> str:
    # Like the tag vocabulary, the board is one shared layout that names no
    # session, so the read needs no caller scoping.
    columns, cols_err = _get_rows(ctx.client, "/api/chat/tag-columns")
    if cols_err:
        return f"Error: {cols_err}"
    tags, tags_err = _get_rows(ctx.client, "/api/chat/tags")
    if tags_err:
        return f"Error: {tags_err}"
    return redact(_render_chat_tag_columns(columns, tags))


def _run_chat_tag_column_create(args: dict[str, Any], ctx: ToolContext) -> str:
    # Agent-authored heading landing in durable, re-rendered state: redact
    # before the write and check the stored length, as chat_tag_create does.
    safe_name = redact(str(args["name"])).strip()
    if not safe_name:
        return "Error: column name must not be empty"
    if len(safe_name) > _MAX_TAG_NAME:
        return (
            f"Error: column name too long after redaction ({len(safe_name)} chars): "
            f"`{safe_name[:40]}…` — keep it to {_MAX_TAG_NAME} characters or fewer"
        )
    # Same gate as the vocabulary writes. The app and crew-member rule lives
    # in the endpoint (``_refuse_vocabulary_write`` in the tag-columns
    # handlers), which judges every transport on the validated claim.
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(
        ctx, "creating a board column"
    )
    if gate:
        return gate
    channel_err = _refuse_channel_board_write("chat_tag_column_create", caller_key)
    if channel_err:
        return channel_err
    tags, tags_err = _get_rows(ctx.client, "/api/chat/tags")
    if tags_err:
        return f"Error: {tags_err}"
    ids, ref_err = _resolve_chat_tag_ids([str(args["tag"])], tags)
    if ref_err:
        return redact(f"Error: {ref_err}")
    tid = ids[0]
    tag_name = next((str(t.get("name") or "?") for t in tags if t.get("id") == tid), tid)
    columns, cols_err = _get_rows(ctx.client, "/api/chat/tag-columns")
    if cols_err:
        return f"Error: {cols_err}"
    # ``ensure`` makes the endpoint return an existing column with this
    # name and tag instead of appending a twin, decided under its write
    # lock, so a retried or racing call converges on one column.
    try:
        d = ctx.client.post(
            "/api/chat/tag-columns",
            {"name": safe_name, "tag_ids": [tid], "mode": "any", "ensure": True},
            session_key=caller_key,
        )
    except DashboardError as refused:
        if refused.code == "app_forbidden":
            return (
                "Error: an app agent or crew member cannot add a board column — the "
                "board is the person's own layout. Read it with chat_tag_column_list."
            )
        return redact(f"Error: {refused.error}")
    got_id = str(d.get("id") or "?")
    if any(str(c.get("id")) == got_id for c in columns):
        return redact(
            f"Column `{d.get('name', safe_name)}` (id={got_id}) already shows " f"tag `{tag_name}`."
        )
    return redact(
        f"Added column `{d.get('name', safe_name)}` (id={got_id}) showing "
        f"tag `{tag_name}`. Place it with chat_tag_column_move."
    )


def _run_chat_tag_column_move(args: dict[str, Any], ctx: ToolContext) -> str:
    # An empty string is "not given": the schema passes it through as "",
    # and treating it as a reference would resolve the literal "None".
    col_before = args.get("before") or None
    col_after = args.get("after") or None
    if (col_before is None) == (col_after is None):
        return "Error: pass exactly one of ``before`` or ``after``"
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(
        ctx, "moving a board column"
    )
    if gate:
        return gate
    channel_err = _refuse_channel_board_write("chat_tag_column_move", caller_key)
    if channel_err:
        return channel_err
    columns, cols_err = _get_rows(ctx.client, "/api/chat/tag-columns")
    if cols_err:
        return f"Error: {cols_err}"
    move_id, col_err = _resolve_chat_tag_column(str(args["column"]), columns)
    if col_err:
        return redact(f"Error: {col_err}")
    anchor_id, col_err = _resolve_chat_tag_column(str(col_before or col_after), columns)
    if col_err:
        return redact(f"Error: {col_err}")
    if anchor_id == move_id:
        return "Error: a column cannot be placed next to itself"
    base_ids = [str(c.get("id")) for c in columns if isinstance(c.get("id"), str)]
    order = list(base_ids)
    order.remove(move_id)
    at = order.index(anchor_id) + (0 if col_before is not None else 1)
    order.insert(at, move_id)
    names = {str(c.get("id")): str(c.get("name") or "").strip() or "(unnamed)" for c in columns}
    side = "before" if col_before is not None else "after"
    if order == base_ids:
        return redact(
            f"No change: column `{names[move_id]}` is already {side} `{names[anchor_id]}`."
        )
    # ``base_ids`` is the order this call read. The endpoint compares it
    # under its lock and refuses with ``stale_base`` when the board changed
    # in between, so the person's own reorder is never overwritten.
    try:
        ctx.client.put(
            "/api/chat/tag-columns/order",
            {"ids": order, "base_ids": base_ids},
            session_key=caller_key,
        )
    except DashboardError as refused:
        if refused.code == "stale_base":
            return (
                "Error: the board's columns changed while this call was composing "
                "the move. Nothing was written — call chat_tag_column_move again; "
                "it re-reads the current order."
            )
        if refused.code == "app_forbidden":
            return (
                "Error: an app agent or crew member cannot reorder the board — it "
                "is the person's own layout."
            )
        return redact(f"Error: {refused.error}")
    return redact(f"Moved column `{names[move_id]}` {side} `{names[anchor_id]}`.")


#: ``/drop`` rejection codes that are answers, not failures: the card stays
#: where it is and calling again changes nothing, so they read as "Not moved".
_DROP_NOT_MOVED = {
    "not_status_lane": "that column does not filter on exactly one status tag, so "
    "no tag change can place a card there",
    "state_lane": "that column follows the session's live state, which no tag " "change can set",
}


def _run_session_move_to_column(args: dict[str, Any], ctx: ToolContext) -> str:
    # This writes to a session OTHER than the caller's, so the row is strict:
    # the table verified the caller before this ran, and that key rides on
    # every request unchanged.
    caller_key = ctx.caller_key
    channel_err = _refuse_channel_board_write("session_move_to_column", caller_key)
    if channel_err:
        return channel_err
    columns, cols_err = _get_rows(ctx.client, "/api/chat/tag-columns")
    if cols_err:
        return f"Error: {cols_err}"
    column_id, col_err = _resolve_chat_tag_column(str(args["column"]), columns)
    if col_err:
        return redact(f"Error: {col_err}")
    column_name = (
        next((str(c.get("name") or "").strip() for c in columns if c.get("id") == column_id), "")
        or column_id
    )
    chat_slots, slots_err = _visible_chat_slots(ctx)
    if slots_err:
        return f"Error: {slots_err}"
    slot_key, slot_err = _resolve_chat_slot_key(args["session"], chat_slots)
    if slot_err:
        return redact(f"Error: {slot_err}")
    slot_row = next((s for s in chat_slots if str(s.get("key") or "") == slot_key), {})
    # ``expected_created`` pins the drop to the slot generation resolved above,
    # as chat_session_pin does: a slot key recreated for another conversation
    # in between is refused instead of retagged.
    body: dict[str, Any] = {"column_id": column_id}
    slot_created = str(slot_row.get("created") or "")
    if slot_created:
        body["expected_created"] = slot_created
    # The tags revision read with the slot above: the drop compares-and-sets
    # against it like chat_tag_assign's PUT, so a tag toggled in between is
    # not judged or written on a list this call never saw.
    base_rev = str(slot_row.get("tags_revision") or "")
    if base_rev:
        body["base_tags_revision"] = base_rev
    try:
        d = ctx.client.post(
            f"/api/chat/slots/{quote(slot_key, safe='')}/drop",
            body,
            session_key=caller_key,
        )
    except DashboardError as refused:
        if refused.code == "column_not_found":
            return redact(
                f"Error: column `{column_name}` was removed before the move. Nothing "
                "was written — call chat_tag_column_list for the current columns."
            )
        if refused.code == "stale_base":
            return redact(
                f"Error: the tags on `{slot_key}` changed while this call was "
                "resolving the session. Nothing was written — call "
                "session_move_to_column again; it re-reads the session."
            )
        return redact(f"Error: {refused.error}")
    if d.get("ok") is not True:
        code = str(d.get("code") or "")
        if code == "session_gone":
            return redact(
                f"Error: session `{slot_key}` closed or was replaced after it was "
                "resolved. Nothing was written — call chat_folder_tree to see the "
                "current sessions."
            )
        why = _DROP_NOT_MOVED.get(code) or str(d.get("reason") or "the board refused it")
        return redact(f"Not moved: session `{slot_key}` stays put — {why}.")
    tags, tags_err = _get_rows(ctx.client, "/api/chat/tags")
    names_by_id = (
        {} if tags_err else {str(t.get("id") or ""): str(t.get("name") or "?") for t in tags}
    )
    final = [str(t) for t in (d.get("tags") or []) if isinstance(t, str)]
    shown = ", ".join(f"`{names_by_id.get(t, t)}`" for t in final) or "none"
    return redact(f"Moved session `{slot_key}` into column `{column_name}`. Tags now: {shown}.")
