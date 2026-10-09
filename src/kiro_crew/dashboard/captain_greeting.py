"""Captain's first greeting: one real model turn the first time its chat opens.

The built-in Captain member (``kirocrew-captain``) speaks first. When the owner
opens Captain's pinned DM thread and it holds no messages yet, the dashboard asks
for a greeting (``POST /api/members/{slug}/greet``) and this module starts ONE
ordinary Captain turn whose prompt is :data:`CAPTAIN_GREETING_KICKOFF`. Captain's
role prompt (``agent._ASSISTANT_SYSTEM_PROMPT``, "First greeting and names") tells it what
that kickoff means: introduce itself by its display name with one line on what
it helps with, then ask how to address the user.

Three properties are the whole design:

* **A real turn, no user row.** The kickoff goes to the model through
  ``_run_chat`` exactly like a gateway-composed prompt (``_synthetic_payload``,
  actor ``gateway``), but nothing is appended to the transcript before dispatch,
  so the user never sees a bubble they did not type. The greeting itself is
  Captain's own assistant row in its own session, so the next turn knows it
  asked for a name.
* **At most once per Captain thread.** A marker file in the member's directory
  is created with ``O_EXCL`` BEFORE dispatch, so two tabs, a reload or a retry
  after a failed turn can never greet twice. A thread that already has rows, or a
  turn in flight, never greets either; neither consumes the marker.
* **Captain only.** The slug must resolve through its DM binding to
  ``kirocrew-captain`` and the live slot must be that member's pinned thread.

A turn that fails (no backend, signed-out CLI) surfaces through the normal turn
error path once; the marker is already claimed, so it never loops.

A newly created crewmate opens the same way, through the same route and the same
dispatch (:func:`maybe_start_member_greeting`): its first turn runs
:func:`crewmate_goal_kickoff`, which asks the crewmate to say who it is and ask
the user what they want it to do. Only a crewmate whose create asked for it
(``first_greeting`` on ``POST /api/agents``) is owed one: the create writes
:data:`GREETING_OWED_FILENAME` in the member's directory, and a crewmate without
that file never greets, so an existing crewmate is never opened with a question
it has already had answered. The once-only claim, the emptiness and busy checks
and the "no transcript row" dispatch are Captain's, unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from kiro_crew import members as members_mod
from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME

logger = logging.getLogger(__name__)

#: The hidden instruction the greeting turn runs. The leading tag is the one the
#: Captain role prompt names; the rest tells the model plainly that the user has
#: not typed anything, so it is never mistaken for speech.
CAPTAIN_GREETING_KICKOFF = (
    "[Captain first greeting] The user just opened this chat for the first time "
    "and it has no messages yet. They have not typed anything: this note comes "
    "from Kiro Crew, not from them, and they cannot see it. This is your first "
    "conversation with them, so do not welcome them back, and use a name only if "
    "their preferences or your memory state it, never one taken from a username, "
    "path, email or host name. Follow the "
    '"First greeting and names" rule of your role now: introduce yourself by your name, '
    "say what you help with, then ask how to address them (or greet them by the "
    "name you already know). That rule sets the length and what to cover."
)

#: The hidden instruction a newly created crewmate's first turn runs. Same
#: shape as Captain's kickoff: plainly not the user's words, and a first
#: conversation, not a return. The user picked a name and a look, not a
#: template, so the greeting must not name the template or role the crewmate
#: runs as, nor describe how that template works: neither is anything the
#: user chose or would recognise. When the crewmate runs is settled in the same
#: conversation: once the goal is clear the crewmate may offer a schedule, and
#: sets one up only after the user agrees.
CREWMATE_GOAL_KICKOFF = (
    "[Crewmate first message] The user just created you and opened your chat for "
    "the first time; it has no messages yet. They have not typed anything: this "
    "note comes from Kiro Crew, not from them, and they cannot see it. In two or "
    "three short sentences of plain, everyday words, introduce yourself by your "
    "name, then ask them what they want you to do: the goal you should own and "
    "look after. Do not name your template, role or agent type, and do not "
    "describe how you work. Do not start any work, call any tool or propose "
    "a plan until they answer. Once the goal is clear, if the work should run "
    "on its own (every morning, every hour, whenever something changes), offer "
    "a schedule: say in plain words when you would run and ask whether they "
    "want it. Set it up with your schedule tool, or as a change card they "
    "confirm, only after they say yes; never create a schedule without asking."
)

#: Appended to :data:`CREWMATE_GOAL_KICKOFF` when the create carried a
#: description, so the crewmate confirms that goal instead of asking blind.
_CREWMATE_GOAL_KNOWN = (
    " When you were created you were described as: {description!r}. Treat that "
    "as a first draft of your goal: restate it in your own words and ask whether "
    "that is what they want, or what to change."
)

#: One-time marker under the member's directory (``members/<slug>/``).
GREETING_MARKER_FILENAME = "captain_greeting.json"

#: A crewmate's own once-only marker, beside :data:`GREETING_OWED_FILENAME`.
CREWMATE_GREETING_MARKER_FILENAME = "first_greeting.json"

#: Written by the crewmate's create when it asked for a first greeting; without
#: it a crewmate's thread is never greeted (:data:`NOT_OWED`).
GREETING_OWED_FILENAME = "first_greeting_owed.json"

# Outcomes reported to the caller. Only ``started`` dispatched a turn.
STARTED = "started"
NOT_CAPTAIN = "not_captain"
NO_THREAD = "no_thread"
NOT_EMPTY = "not_empty"
BUSY = "busy"
ALREADY_GREETED = "already_greeted"
NOT_OWED = "not_owed"


def greeting_marker_path(slug: str) -> Path:
    """Where the once-only marker for *slug*'s thread lives (containment-checked)."""
    return members_mod.member_dir(slug) / GREETING_MARKER_FILENAME


def crewmate_greeting_marker_path(slug: str) -> Path:
    """Where a crewmate's once-only greeting marker lives (containment-checked)."""
    return members_mod.member_dir(slug) / CREWMATE_GREETING_MARKER_FILENAME


def greeting_owed_path(slug: str) -> Path:
    """Where the "greet this crewmate first" record lives (containment-checked)."""
    return members_mod.member_dir(slug) / GREETING_OWED_FILENAME


def mark_greeting_owed(slug: str) -> None:
    """Record that *slug*'s first chat should open with its goal question.

    Called by the create that asked for it. Blocking IO; call it off the loop.
    The file is created exclusively and never through a link: an existing
    leaf, whatever it is, stays untouched, so a pre-planted symlink can never
    turn this write into a truncation of the file it points at. What makes the
    greeting once-only is the separate claim marker, not this one.
    """
    path = greeting_owed_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"ts": time.time()}, fh)


def greeting_is_owed(slug: str) -> bool:
    """True when *slug*'s create left a real owed record (never a symlink)."""
    path = greeting_owed_path(slug)
    return not path.is_symlink() and path.is_file()


def crewmate_goal_kickoff(description: str) -> str:
    """The kickoff for a crewmate's first turn, naming its description when set."""
    text = description.strip()
    if not text:
        return CREWMATE_GOAL_KICKOFF
    return CREWMATE_GOAL_KICKOFF + _CREWMATE_GOAL_KNOWN.format(description=text)


def claim_greeting_marker(slug: str, slot_key: str, *, path: Path | None = None) -> bool:
    """Atomically claim the greeting for *slug*. False when it was already claimed.

    ``O_CREAT | O_EXCL`` is the whole guard: it is atomic across tasks and
    processes, so exactly one caller ever sees True. *path* defaults to
    Captain's marker; a crewmate passes its own. Blocking IO; call it off the
    event loop.
    """
    path = path if path is not None else greeting_marker_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"slot_key": slot_key, "ts": time.time()}, fh)
    return True


def _captain_slot(state: Any, binding: dict | None) -> tuple[str, Any]:
    """Resolve a DM *binding* to Captain's live pinned thread, or say why not."""
    if binding is None or binding.get("member") != ASSISTANT_MEMBER_NAME:
        return NOT_CAPTAIN, None
    slot = state._slots.get(binding.get("slot_key", ""))
    if (
        slot is None
        or slot.mode != members_mod.DM_SLOT_MODE
        or slot.agent != ASSISTANT_MEMBER_NAME
        or slot.is_remote
        or getattr(slot, "executor", "") == "remote"
    ):
        return NO_THREAD, None
    return "", slot


def _member_slot(state: Any, binding: dict | None) -> Any:
    """The live local pinned thread a DM *binding* names, or ``None``."""
    if binding is None:
        return None
    member = binding.get("member")
    if not member:
        return None
    slot = state._slots.get(binding.get("slot_key", ""))
    if (
        slot is None
        or slot.mode != members_mod.DM_SLOT_MODE
        or slot.agent != member
        or slot.is_remote
        or getattr(slot, "executor", "") == "remote"
    ):
        return None
    return slot


def _has_content(slot: Any) -> bool:
    """True when the thread already holds anything a greeting would precede."""
    return bool(slot.messages) or bool(getattr(slot, "_queue", None))


async def maybe_start_captain_greeting(state: Any, slug: str) -> str:
    """Start Captain's first greeting on *slug*'s thread when it is owed.

    Returns one of the module's outcome constants. Every check that can refuse
    runs BEFORE the marker is claimed, and the emptiness/busy checks run again
    after the off-loop claim, since a send can land while the claim is written.
    """
    binding = await asyncio.to_thread(members_mod.read_dm_binding, slug)
    reason, slot = _captain_slot(state, binding)
    if slot is None:
        return reason
    if slot.running:
        return BUSY
    if _has_content(slot):
        return NOT_EMPTY
    if not await asyncio.to_thread(claim_greeting_marker, slug, slot.key):
        return ALREADY_GREETED
    if slot.running or _has_content(slot) or state._slots.get(slot.key) is not slot:
        # Lost the race to a real send (or the slot was replaced) after the
        # claim. The user is already talking, so the greeting is simply moot.
        return NOT_EMPTY if _has_content(slot) else BUSY
    _dispatch_greeting(state, slot)
    return STARTED


def _crewmate_description(member: str) -> str:
    """The crew record's ``description`` for *member*, ``""`` when unreadable."""
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        agent = KiroCrewConfig.load().agents.get(member)
    except Exception:
        logger.debug("config unreadable for crewmate greeting", exc_info=True)
        return ""
    value = getattr(agent, "description", "") if agent is not None else ""
    return value if isinstance(value, str) else ""


async def maybe_start_member_greeting(state: Any, slug: str) -> str:
    """Start the first greeting owed on *slug*'s thread: Captain's or a crewmate's.

    Captain keeps :func:`maybe_start_captain_greeting` exactly. Any other member
    greets only when its create recorded the greeting as owed, on its own
    pinned local thread, while that thread is empty and idle, and at most once
    (its own ``O_EXCL`` marker). Every refusal runs before the claim.
    """
    binding = await asyncio.to_thread(members_mod.read_dm_binding, slug)
    if binding is not None and binding.get("member") == ASSISTANT_MEMBER_NAME:
        return await maybe_start_captain_greeting(state, slug)
    if not await asyncio.to_thread(greeting_is_owed, slug):
        return NOT_OWED
    slot = _member_slot(state, binding)
    if slot is None:
        return NO_THREAD
    if slot.running:
        return BUSY
    if _has_content(slot):
        return NOT_EMPTY
    # Read before the claim: no await may sit between the final recheck and
    # the dispatch, or a send landing in that gap would race the greeting.
    kickoff = crewmate_goal_kickoff(await asyncio.to_thread(_crewmate_description, slot.agent))
    claimed = await asyncio.to_thread(
        claim_greeting_marker, slug, slot.key, path=crewmate_greeting_marker_path(slug)
    )
    if not claimed:
        return ALREADY_GREETED
    if slot.running or _has_content(slot) or state._slots.get(slot.key) is not slot:
        return NOT_EMPTY if _has_content(slot) else BUSY
    _dispatch_greeting(state, slot, kickoff)
    return STARTED


def _dispatch_greeting(state: Any, slot: Any, kickoff: str = CAPTAIN_GREETING_KICKOFF) -> None:
    """Run *kickoff* as a gateway-authored turn, with no transcript row for it."""
    # Deferred: chat_handlers / chat import this package's handlers at load.
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.dashboard.chat_handlers import _sweep_stale_permissions
    from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn

    # The owner opened this thread, so a human is demonstrably watching it.
    slot._human_seen = True
    slot._has_reader = False  # delivered over the WebSocket, like a ws send
    slot._file_changes = []
    _sweep_stale_permissions(slot)
    task = spawn_guarded_turn(
        state,
        slot,
        state.run_background_turn(
            slot,
            _run_chat(
                state,
                slot,
                kickoff,
                _synthetic_payload=True,
                _turn_actor="gateway",
            ),
        ),
    )
    slot.task = task
    state.push_slots_update()
    logger.info("first greeting started on %s", slot.key)
