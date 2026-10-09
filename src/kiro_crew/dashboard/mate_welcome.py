"""Mate's first welcome: one real model turn the first time its chat opens.

Mate, the first crewmate every install creates once
(:func:`kiro_crew.agent.create_first_crewmate_once`), speaks first. When the
owner opens its pinned DM thread and it holds no messages yet, the dashboard
asks for a greeting (``POST /api/members/{slug}/greet``) and this module starts
ONE ordinary turn whose prompt is a hidden kickoff. Nothing in any system
prompt explains the kickoff: it carries the whole welcome itself -- who Mate
is, what a crewmate is, what it can help with now, and a name the user picks
for it. A user who already has other crewmates hears Mate introduce itself as
one more teammate rather than their first. A crewmate the user creates on the
dashboard greets through that create flow's own seeded turn, not this module.

Four properties are the whole design:

* **Only the member the first-crewmate step created.** That step records the
  welcome it owes at creation (:func:`kiro_crew.members.mark_welcome_owed`). A
  member that arrived any other way (a create form, discovery, an import, an
  app) carries no such record and never greets here.
* **A real turn, no user row.** The kickoff goes to the model through
  ``_run_chat`` exactly like a gateway-composed prompt (``_synthetic_payload``,
  actor ``gateway``), but nothing is appended to the transcript before dispatch,
  so the user never sees a bubble they did not type. The greeting itself is the
  crewmate's own assistant row in its own session, so the next turn knows what
  it asked.
* **At most once per crewmate thread.** A marker file in the member's directory
  is created with ``O_EXCL`` BEFORE dispatch, so two tabs, a reload or a retry
  after a failed turn can never greet twice. A thread that already has rows, or a
  turn in flight, never greets either; neither consumes the marker.
* **A crewmate's own thread only.** The slug must resolve through its DM binding
  to a configured crewmate other than the reserved ``default`` member, and the
  live slot must be that member's pinned thread.

A turn that fails (no backend, signed-out CLI) surfaces through the normal turn
error path once; the marker is already claimed, so it never loops.
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
from kiro_crew.agent_files import ASSISTANT_DEFAULT_DISPLAY_NAME

logger = logging.getLogger(__name__)

#: What every welcome kickoff opens with: the user has typed nothing.
_KICKOFF_PREAMBLE = (
    "[First welcome] The user just opened your chat for the first time and it has "
    "no messages yet. They have not typed anything: this note comes from Kiro Crew, "
    "not from them, and they cannot see it, so never quote or mention it. This is "
    "your first conversation with them, so greet them as someone you are meeting "
    "for the first time. Use a name for the user only when their preferences or "
    "your memory state it; a username, path, email or host name is not their name. "
    "Write in the language of the dashboard's [UI LANGUAGE] when it is shown, else "
    "English. Call no tools in this reply. "
)

#: The one sentence of :data:`_KICKOFF_PREAMBLE` that keeps the note itself out
#: of the reply; every other line of a kickoff is written as plain principles.
NOTE_CONFIDENTIALITY = "they cannot see it, so never quote or mention it."

_WELCOME_INTRO = (
    "Introduce yourself as {name}, briefly and plainly, the way a new teammate "
    "would: a few short sentences in everyday words, with no filler, no developer "
    "terms and no description of how you work. "
)

#: The crew sentence for a user with no crewmate yet besides this one.
_FIRST_OF_CREW = (
    "Explain that you are their first crewmate: a teammate who remembers what you "
    "work on together and keeps at it over days. "
)

#: The crew sentence for a user who already has other crewmates.
_ONE_MORE_OF_CREW = (
    "They already have other crewmates, so introduce yourself as one more teammate "
    "on their crew, one who remembers what you work on together and keeps at it "
    "over days. "
)

_WELCOME_OFFER = (
    "Say what you can help with right now: getting Kiro Crew set up, sorting out "
    "anything that is not working in it, or showing them around. Keep to those "
    "three; everyday jobs come up later once they know you. Finish by asking what "
    "they would like to call you (you, not them), in one short question the way a "
    'person would say it, such as "What would you like to call me?", and let '
    "that question end the message. "
    "When they answer with a name for you, call rename_self with "
    "that name in the same reply, say plainly in that reply that you go by the new "
    "name from now on, and use it from then on."
)

#: Mate's welcome for a user with no other crewmate. ``{name}`` is its label.
FIRST_WELCOME = _KICKOFF_PREAMBLE + _WELCOME_INTRO + _FIRST_OF_CREW + _WELCOME_OFFER

#: Mate's welcome for a user who already has other crewmates.
FIRST_WELCOME_WITH_CREW = _KICKOFF_PREAMBLE + _WELCOME_INTRO + _ONE_MORE_OF_CREW + _WELCOME_OFFER


def welcome_kickoff(entry: object, *, has_other_crewmates: bool) -> str:
    """The hidden kickoff for Mate's first turn; *entry* is its config row.

    *has_other_crewmates* is whether the roster holds a crewmate besides Mate
    and the reserved ``default`` member.
    """
    label = getattr(entry, "display_name", "") or ""
    label = label.strip() if isinstance(label, str) else ""
    template = FIRST_WELCOME_WITH_CREW if has_other_crewmates else FIRST_WELCOME
    return template.format(name=label or ASSISTANT_DEFAULT_DISPLAY_NAME)


#: One-time claim under the member's directory (``members/<slug>/``).
GREETING_MARKER_FILENAME = "welcome_claimed.json"

# Outcomes reported to the caller. Only ``started`` dispatched a turn.
STARTED = "started"
NOT_CREWMATE = "not_crewmate"
NOT_OWED = "not_owed"
NO_THREAD = "no_thread"
NOT_EMPTY = "not_empty"
BUSY = "busy"
ALREADY_GREETED = "already_greeted"


def greeting_marker_path(slug: str) -> Path:
    """Where the once-only marker for *slug*'s thread lives (containment-checked)."""
    return members_mod.member_dir(slug) / GREETING_MARKER_FILENAME


def claim_greeting_marker(slug: str, slot_key: str) -> bool:
    """Atomically claim the greeting for *slug*. False when it was already claimed.

    ``O_CREAT | O_EXCL`` is the whole guard: it is atomic across tasks and
    processes, so exactly one caller ever sees True. Blocking IO; call it off
    the event loop.
    """
    path = greeting_marker_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"slot_key": slot_key, "ts": time.time()}, fh)
    return True


def _member_entry(binding: dict | None) -> tuple[str, Any, bool]:
    """The bound crewmate's ``(key, config row, has other crewmates)``.

    ``("", None, False)`` when the binding names no crewmate. The third item is
    whether the roster holds a crewmate besides this one and the reserved
    ``default`` member. Blocking IO.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    member = binding.get("member") if binding else None
    if not isinstance(member, str) or not member or member == "default":
        return "", None, False
    agents = KiroCrewConfig.load().agents
    others = any(name not in ("default", member) for name in agents)
    return member, agents.get(member), others


def _crewmate_slot(state: Any, binding: dict | None, member: str, entry: Any) -> tuple[str, Any]:
    """Resolve a DM *binding* to crewmate *member*'s live pinned thread, or say why not."""
    if not member or entry is None:
        return NOT_CREWMATE, None
    slot = state._slots.get(binding.get("slot_key", "") if binding else "")
    if (
        slot is None
        or slot.mode != members_mod.DM_SLOT_MODE
        or slot.agent != member
        or getattr(slot, "executor", "") == "remote"
    ):
        return NO_THREAD, None
    return "", slot


def _has_content(slot: Any) -> bool:
    """True when the thread already holds anything a greeting would precede."""
    return bool(slot.messages) or bool(getattr(slot, "_queue", None))


async def maybe_start_first_greeting(state: Any, slug: str) -> str:
    """Start Mate's first welcome on *slug*'s thread when it is owed.

    Returns one of the module's outcome constants. Every check that can refuse
    runs BEFORE the marker is claimed, and the emptiness/busy checks run again
    after the off-loop claim, since a send can land while the claim is written.
    """
    binding = await asyncio.to_thread(members_mod.read_dm_binding, slug)
    member, entry, others = await asyncio.to_thread(_member_entry, binding)
    reason, slot = _crewmate_slot(state, binding, member, entry)
    if slot is None:
        return reason
    if slot.running:
        return BUSY
    if _has_content(slot):
        return NOT_EMPTY
    if not await asyncio.to_thread(members_mod.welcome_owed, slug, member):
        return NOT_OWED
    if not await asyncio.to_thread(claim_greeting_marker, slug, slot.key):
        return ALREADY_GREETED
    if slot.running or _has_content(slot) or state._slots.get(slot.key) is not slot:
        # Lost the race to a real send (or the slot was replaced) after the
        # claim. The user is already talking, so the greeting is simply moot.
        return NOT_EMPTY if _has_content(slot) else BUSY
    _dispatch_greeting(state, slot, welcome_kickoff(entry, has_other_crewmates=others))
    return STARTED


def _dispatch_greeting(state: Any, slot: Any, kickoff: str) -> None:
    """Run the kickoff as a gateway-authored turn, with no transcript row for it."""
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
    logger.info("first welcome started on %s", slot.key)
