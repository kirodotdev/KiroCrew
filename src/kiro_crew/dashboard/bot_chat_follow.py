"""Saving a Bot's model or reasoning effort brings the Bot's own chat back onto it.

A Bot's own chat (its ``member`` thread) can hold a model and an effort of its
own: ``/model`` typed in it, the slot routes, a fork, an engine fallback, or an
older build's copy of the Bot's model. That value outranks the Bot's profile on
every turn, and the Bot's chat shows no model or effort control, so a person who
saved the Bot had no way to see why the chat ignored it. The person's latest
choice wins instead: a save of the Bot's ``model`` clears the chat's own model,
and a save of its ``reasoning_effort`` clears the chat's own effort, so the next
turn runs on the Bot's value. A save that repeats the stored value clears it too.
A ``/model`` typed in the chat afterwards wins again until the next save.

The one caller is ``PUT /api/agents/{name}`` (the Bot editor, which sends both
fields on every save, renaming included, and the Bot's ``set_model`` card): it
passes the fields its body carried, after it has rebuilt the provider factory.
shortcut: ``kirocrew agent update --model`` and a hand edit of ``config.json`` do
not reach this module, so they leave the chat's own value in place; a fix must
not make a dashboard save clear twice through the config watcher.

Only the Bot's own chat changes: other chats on the Bot's agent, the sessions the
Bot dispatches and every other chat keep their own picks. A live session started
on the old value is not touched mid-turn: the clear queues the reset a model pick
queues, which the chat's next turn runs when the session is idle.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from kiro_crew.config.loader import normalize_agent_model
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.llm_helpers import slot_switch_session_lock

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: The Bot profile fields whose save clears the same field in the Bot's chat.
FIELDS = ("model", "reasoning_effort")


async def _clear_open(state: DashboardState, slot: _ChatSlot, fields: set[str], agent: Any) -> None:
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.session_control import arm_model_switch_reset

    session_key = effective_session_key(slot)
    # The switch handlers' lock order, so a pick in flight finishes first.
    async with slot_switch_session_lock(session_key), slot._model_pick_lock:
        restart = False
        cleared = False
        # A queued session_set_model pick yields each half to a newer choice of
        # that half (apply_pending_model_pick), so the generation bumps below drop
        # only the saved field's half and keep the other.
        pending = slot._pending_model_pick
        if "model" in fields and (
            slot.model
            or slot.jev_route
            or (pending is not None and pending.model is not None)
            or slot._deferred_model_switch_key
            # A chat with no model of its own can still be on a fallback.
            or slot._active_fallback_model
            or slot._refusal_fallback_primary
        ):
            # A fallback serving the session means the wire model differs from the
            # pin, so an equal pin still needs the reset, as the picker treats it.
            restart = (
                slot.model != normalize_agent_model(agent.model)
                or bool(slot._deferred_model_switch_key)
                or bool(slot._active_fallback_model)
                or bool(slot._refusal_fallback_primary)
            )
            slot.model = ""
            slot.jev_route = False
            # An explicit pick's bump, so a fallback restore does not put it back.
            slot._model_pick_gen += 1
            slot._deferred_model_switch_key = ""
            cleared = True
        if "reasoning_effort" in fields and (
            slot.reasoning_effort or (pending is not None and pending.reasoning_effort is not None)
        ):
            restart = restart or slot.reasoning_effort not in ("", agent.reasoning_effort or "")
            slot.reasoning_effort = ""
            slot._effort_pick_gen += 1
            cleared = True
        if not cleared:
            return
        if restart:
            # Lands at the next turn's start, never during one.
            arm_model_switch_reset(state, slot)
        slot._dirty = True
        history_key = slot_history_key(slot)
    # Not forced: the slot is dirty, so the save writes it, and a forced full save
    # could erase a concurrent close.
    if not await save_slot_off_loop(state, slot, expected_history_key=history_key):
        # The flush retries a dirty slot.
        slot._dirty = True
    state.push_slots_update()


async def follow_bot_save(
    state: DashboardState | None, name: str, cfg: KiroCrewConfig, fields: Iterable[str]
) -> None:
    """Clear each saved field of :data:`FIELDS` in Bot *name*'s own chat.

    An open chat is cleared in memory and saved; a closed one has its line
    patched, so reopening it restores the Bot's value. Best-effort: the Bot's
    save already succeeded, and a failure here leaves the chat as it was.
    """
    picked = set(fields) & set(FIELDS)
    agent = cfg.agents.get(name)
    if state is None or not picked or agent is None:
        return
    from kiro_crew import members as members_mod

    try:
        slots = [
            s
            for s in list(getattr(state, "_slots", {}).values())
            if s.mode == members_mod.DM_SLOT_MODE and s.agent == name
        ]
        for slot in slots:
            await _clear_open(state, slot, picked, agent)
        log = getattr(state, "conversation_log", None)
        if log is None:
            return
        slug = members_mod.member_slug(name, cfg)
        binding = await asyncio.to_thread(members_mod.read_dm_binding, slug)
        if binding is None or binding["member"] != name or binding["slot_key"] in state._slots:
            return
        key = members_mod.member_thread_session_alias(slug, binding.get("memory_store", ""))
        await asyncio.to_thread(
            log.update_metadata_if,
            key,
            {f: "" for f in picked},
            lambda meta: any(meta.get(f) for f in picked),
            require_existing=True,
        )
    except Exception:
        logger.warning(
            "Could not bring %s's chat back onto the Bot's settings", name, exc_info=True
        )
