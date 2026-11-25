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

Two callers reach it. ``PUT /api/agents/{name}`` (the Bot editor, which sends both
fields on every save, renaming included, and the Bot's ``set_model`` card) passes
the fields its body carried, after it has rebuilt the provider factory. The config
watcher passes a changed ``agents.<name>.model`` or ``agents.<name>.reasoning_effort``
from any other writer (``kirocrew agent update --model``, ``kirocrew config set``, a
hand edit), after the session manager's own applier has rebuilt the factory.

A dashboard save is seen by both, so each value is followed once: every follow
records the Bot value it applied, the watcher skips a value already recorded, and
the save skips a field whose value the watcher recorded after the save began.
Both check and record with no await in between, so whichever runs first clears and
the other does not, and a ``/model`` pick made between them is kept.

Only the Bot's own chat changes: other chats on the Bot's agent, the sessions the
Bot dispatches and every other chat keep their own picks. A live session started
on the old value is not touched mid-turn: the clear queues the reset a model pick
queues, which the chat's next turn runs when the session is idle.
"""

from __future__ import annotations

import asyncio
import itertools
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

#: Orders the follow records, so a save can tell a watcher follow made after it began.
_SEQ = itertools.count(1)


def _followed(state: Any) -> dict[tuple[str, str], tuple[str, int]]:
    """``(Bot, field) -> (value followed, sequence)``, kept on the dashboard state."""
    followed = getattr(state, "_bot_chat_followed", None)
    if followed is None:
        followed = {}
        state._bot_chat_followed = followed
    return followed


def follow_mark() -> int:
    """The sequence a save reads before its write, for :func:`follow_bot_save`."""
    return next(_SEQ)


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


def _claim(state: Any, name: str, agent: Any, fields: set[str], since: int | None) -> set[str]:
    """Record and return the fields of *fields* this follow clears.

    Synchronous, so no other follow runs between the check and the record. With
    *since* (a save), a field the watcher followed to the same value after *since*
    is dropped; without it (the watcher), a field already followed to its value is.
    """
    followed = _followed(state)
    claimed = set()
    for field in fields:
        value = getattr(agent, field) or ""
        prior = followed.get((name, field))
        if prior is not None and prior[0] == value and (since is None or prior[1] > since):
            continue
        followed[(name, field)] = (value, next(_SEQ))
        claimed.add(field)
    return claimed


async def follow_bot_save(
    state: DashboardState | None,
    name: str,
    cfg: KiroCrewConfig,
    fields: Iterable[str],
    *,
    since: int | None = None,
) -> None:
    """Clear each saved field of :data:`FIELDS` in Bot *name*'s own chat.

    *since* is the :func:`follow_mark` a save read before its write. An open chat
    is cleared in memory and saved; a closed one has its line patched, so reopening
    it restores the Bot's value. Best-effort: the Bot's save already succeeded, and
    a failure here leaves the chat as it was.
    """
    agent = cfg.agents.get(name)
    if state is None or agent is None:
        return
    picked = _claim(state, name, agent, set(fields) & set(FIELDS), since)
    if picked:
        await _follow(state, name, cfg, agent, picked)


def follow_config_change(state: DashboardState, change: Any) -> None:
    """The config watcher's applier: follow a Bot value another writer changed.

    Registered after the session manager's applier, which has rebuilt the provider
    factory for a changed effort by the time this runs. The clear runs off the
    watcher's cycle, so the applier's timeout never cancels it half done.
    """
    from kiro_crew.config import live

    paths = change.under("agents")
    if "agents" in change.new.degraded_sections:
        # A degraded section holds defaults, not the Bot's values. The whole-config
        # flag is not consulted, as in ``live._section_applier``: a dispatched change
        # never carries a torn document, so on one that flag only remembers a tear
        # since repaired, and the loader keeps it for the life of the process.
        raise live.ConfigDeferred(paths)
    by_bot: dict[str, set[str]] = {}
    for path in paths:
        head, _, field = path.rpartition(".")
        if field in FIELDS and head.startswith("agents."):
            by_bot.setdefault(head[len("agents.") :], set()).add(field)
    for name, fields in by_bot.items():
        agent = change.new.agents.get(name)
        if agent is None:
            continue
        picked = _claim(state, name, agent, fields, None)
        if not picked:
            continue
        task = asyncio.create_task(
            _follow(state, name, change.new, agent, picked), name="bot-chat-follow"
        )
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)


async def _follow(
    state: DashboardState, name: str, cfg: KiroCrewConfig, agent: Any, picked: set[str]
) -> None:
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
