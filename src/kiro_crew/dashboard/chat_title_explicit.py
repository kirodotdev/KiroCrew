"""Explicit session titles: a person's rename and "Regenerate title".

The sidebar rename route (``api_chat_slot_rename``) and :func:`apply_manual_title`, which the
``session_rename`` verb and a channel's ``/title`` share; and the "Regenerate title" route
(``api_chat_slot_generate_title``) with :func:`generate_manual_title` and
:func:`apply_regenerated_title`, which ``session_generate_title`` shares. The background
titler and ``name_member_thread`` stay in ``kiro_crew.dashboard.chat_title``.

Composed onto ``kiro_crew.dashboard.chat_title`` by :func:`kiro_crew._facade_compose.compose`:
every function here runs on that module's globals, names it as its ``__module__`` and logs
through its ``logger``, so ``chat_title`` stays the one import path and patch target (tests
patch ``chat_title._persist_title`` and ``chat_title._generate_title_via_kiro``, which these
functions read). This module imports the facade only for type checking and defines no
module-level value; nothing but ``chat_title`` imports it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from aiohttp import web

    from kiro_crew.dashboard.chat_title import (
        _TITLE_ORIGIN_AUTO,
        _TITLE_ORIGIN_USER,
        _TITLE_PROMPT_ROLES,
        _TITLE_PROMPT_WINDOW,
        NEW_SESSION_TITLE,
        DashboardState,
        _ChatSlot,
        _current_ticket_key,
        _fallback_title_from_messages,
        _generate_title_via_kiro,
        _is_member_thread,
        _persist_title,
        _ticket_key_if_enabled,
        _with_ticket_prefix,
        effective_session_key,
        is_slack_slot,
        logger,
        record_manual_rename,
        record_rename_route_access,
        slot_not_found,
    )


async def generate_manual_title(state: DashboardState, slot: _ChatSlot) -> str:
    """The title "Regenerate title" would give *slot* now, or ``""`` for none.

    Shared by the sidebar route and the ``session_generate_title`` verb. Makes
    one titling-model call and changes nothing on the slot. ``""`` means the
    model and the heuristic fallback both had nothing better than the
    placeholder, so the caller keeps the current title. A crewmate's own DM
    thread always answers ``""``: its name is the crewmate's, not generated.
    """
    if _is_member_thread(slot):
        return ""
    try:
        # Window the RECENT conversational tail: the user reaches for
        # "Regenerate title" when the current name does not fit, so the
        # prompt must be built from what the session is about NOW, not its
        # opening turns. Filter to the roles the prompt builder keeps BEFORE
        # slicing — the raw list interleaves tool/permission/status rows, so
        # a raw tail over a tool-heavy turn could window every usable line
        # out and leave the click a silent no-op. The prompt builder's head
        # slice is a no-op on this pre-windowed tail, so the initial
        # auto-title path (which passes the full list and wants the opening
        # messages) is unaffected.
        convo = [m for m in slot.messages if m.get("role") in _TITLE_PROMPT_ROLES]
        # Same label as the automatic paths: the key the title already leads
        # with, else the opening message's.
        key = await _ticket_key_if_enabled(_current_ticket_key(slot.title, convo))
        # Ungated: a person is waiting, and a memory refusal would replace the
        # current title with the heuristic fallback below.
        title = await _generate_title_via_kiro(
            state,
            convo[-_TITLE_PROMPT_WINDOW:],
            session_key=effective_session_key(slot),
            memory_gate=False,
        )
        return _with_ticket_prefix(title, key)
    except Exception:
        logger.debug("Title generation failed for slot %s", slot.key, exc_info=True)
        title = _fallback_title_from_messages(slot.messages, slack=is_slack_slot(slot))
        return "" if title == NEW_SESSION_TITLE else title


async def apply_regenerated_title(
    state: DashboardState,
    slot: _ChatSlot,
    title: str,
    *,
    still_current: Callable[[], bool] | None = None,
) -> bool:
    """Set a freshly generated *title* on *slot*, save it, and show it.

    Every field is assigned BEFORE the first await, so a caller that gates the
    target synchronously and then awaits this coroutine has no suspension
    between its gate and the change, as with :func:`apply_manual_title`.

    The title stays refreshable (``auto``) and replaces a hand-set one, which
    is what the sidebar's "Regenerate title" has always done. The sidebar push
    is skipped when an explicit title landed during the save (its own push
    already ran) or when the slot left its key, so a successor opened under the
    same key never shows this session's title. Returns whether the save landed.
    """
    slot.title = title
    slot._titled = True
    # Still an LLM-generated name, so it stays refreshable ("auto"). The
    # epoch bump makes any in-flight background attempt stand down instead
    # of clobbering the title the user just asked for.
    slot._title_origin = _TITLE_ORIGIN_AUTO
    # Generated from the recent conversational tail, so it is not a
    # first-message echo — the early low-signal refresh must not re-fire.
    slot._title_low_signal = False
    slot._title_epoch += 1
    epoch = slot._title_epoch
    saved = await _persist_title(state, slot, still_current=still_current)
    # RE-CHECK after the persist await, mirroring the refresh path: a rename
    # landing during the write has already pushed ITS name, so pushing our
    # now-stale local ``title`` would overwrite it in the sidebar (the disk
    # is already correct via the persist loop; this guards the broadcast).
    if slot._title_epoch == epoch and state._slots.get(slot.key) is slot:
        state.push_slot_title(slot.key, title)
    return saved


async def api_chat_slot_generate_title(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/generate-title — manually trigger title generation."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()

    logger.info("Manual title generation requested for slot %s", name)
    epoch = slot._title_epoch
    title = await generate_manual_title(state, slot)

    if request.get("app", "") and state._slots.get(name) is not slot:
        return slot_not_found()

    # RACE GUARD: a manual rename landing during the generation await bumps the
    # epoch, and its name outranks ours -- stand down instead of overwriting it,
    # the same contract ``maybe_refresh_title`` states in its docstring. The
    # empty title is this endpoint's existing "nothing was applied" answer, so
    # the user keeps their own name with no client change.
    if slot._title_epoch != epoch:
        logger.info(
            "Manual title generation: explicit title landed during generation "
            "for slot %s; keeping it",
            name,
        )
        return web.json_response({"ok": True, "title": ""})

    if title:
        is_app = bool(request.get("app", ""))

        def _still_current() -> bool:
            return state._slots.get(name) is slot

        applied_epoch = slot._title_epoch + 1
        await apply_regenerated_title(
            state, slot, title, still_current=_still_current if is_app else None
        )
        if is_app and state._slots.get(name) is not slot:
            return slot_not_found()
        if slot._title_epoch != applied_epoch:
            logger.info(
                "Manual title generation: explicit title landed during persist "
                "for slot %s; keeping it",
                name,
            )
            return web.json_response({"ok": True, "title": ""})

    return web.json_response({"ok": True, "title": title})


async def apply_manual_title(
    state: DashboardState, slot: Any, title: str, *, route: str, caller: str, source: str = ""
) -> bool:
    """Set *title* on *slot* as a final, manual name, persist it and broadcast it.

    Shared by the sidebar rename route, the ``session_rename`` session-control
    verb and a channel's ``/title`` on a live slot
    (``channel_slots.rename_channel_title_live``), so every rename lands exactly
    like a person's. The caller has already validated *title*.

    Every rename is audited by :func:`~kiro_crew.dashboard.title_audit.record_manual_rename`;
    *route* and *caller* are required so a new rename path cannot leave one unattributed.

    Every field is assigned BEFORE the first await, so a caller that gates the
    target synchronously and then awaits this coroutine has no suspension between
    its gate and the write.

    Returns whether the title reached the conversation history. Saving is
    best-effort by dashboard contract: on
    a failed write the slot keeps the new title and stays authoritative, it is
    still broadcast, and the slot's next full save writes it to disk. Nothing is
    rolled back, so a newer rename that overlaps this one never gets an older
    title restored over it. A caller that reports the outcome to someone who
    acts on it (``session_rename``) must say the title is live but not yet saved
    rather than report plain success.
    """
    old_title = slot.title
    slot.title = title
    slot._titled = True
    # A manual rename is final: origin "user" locks the background refresh out
    # permanently, and the synchronous epoch bump makes any in-flight
    # background attempt stand down instead of clobbering this name.
    slot._title_origin = _TITLE_ORIGIN_USER
    slot._title_epoch += 1
    record_manual_rename(slot, old_title, route=route, caller=caller, source=source)
    saved = await _persist_title(state, slot)
    if not saved:
        logger.warning("Rename of slot %s is live but not yet saved", slot.key)
    # ``slot_title`` keeps every consumer's title current; the patch carries the
    # projected (redacted) title to patch-capable tabs in place of a full list.
    # Published from the slot, not from *title*: a rename that overlapped this
    # write has already replaced it, and a stale broadcast would undo that on
    # screen.
    state.push_slot_title(slot.key, slot.title, full=False)
    state.push_slot_patch(slot.key, ("title",))
    return saved


async def api_chat_slot_rename(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/title — rename a chat session."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "body_not_object"}, status=400)
    from kiro_crew.dashboard.handlers._shared import string_field_error

    title = body.get("title", "")
    if (not_string := string_field_error("title", title)) is not None:
        return not_string
    title = title.strip()[:200]
    if not title:
        return web.json_response({"error": "title required", "code": "title_required"}, status=400)
    # Best-effort save, as on main: the live slot stays authoritative and its
    # next full save writes the title (see ``apply_manual_title``).
    caller = request.get("app", "") or "dashboard"
    await apply_manual_title(
        state, slot, title, route="rename_route", caller=caller, source="dashboard"
    )
    record_rename_route_access(caller, slot.key)
    return web.json_response({"ok": True, "title": title})
