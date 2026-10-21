"""A crewmate's first welcome: one real model turn the first time its chat opens.

Mate, the first crewmate every install creates once
(:func:`kiro_crew.agent.create_first_crewmate_once`), speaks first. When the
owner opens its pinned DM thread and it holds no messages yet, the dashboard
asks for a greeting (``POST /api/members/{slug}/greet``) and this module starts
ONE ordinary turn whose prompt is a hidden kickoff. Nothing in any system
prompt explains the kickoff: it carries the whole welcome itself -- who Mate
is, what a crewmate is, what it can help with now, and a name the user picks
for it. A user who already has other crewmates hears Mate introduce itself as
one more teammate rather than their first.

A crewmate the user creates from the Crewmates page's New crewmate card opens
the same way, through the same route and dispatch: its create sends
``first_greeting`` and records a goal welcome
(:data:`kiro_crew.members.WELCOME_KIND_GOAL`), and its first turn runs
:func:`goal_kickoff`, which has it introduce itself by name and ask what it
should work on, or confirm the goal the card was given.

Four properties are the whole design:

* **Only a member whose creation recorded it.** The first-crewmate step and a
  ``first_greeting`` create record the welcome owed
  (:func:`kiro_crew.members.mark_welcome_owed`). A member that arrived any other
  way (discovery, an import, an app) carries no such record and never greets
  here.
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

from kiro_crew import crewmate_prompts
from kiro_crew import members as members_mod
from kiro_crew.agent_files import ASSISTANT_DEFAULT_DISPLAY_NAME

logger = logging.getLogger(__name__)

#: What every welcome kickoff opens with: the user has typed nothing.
_KICKOFF_PREAMBLE = crewmate_prompts.text("welcome.preamble")

#: The one sentence of :data:`_KICKOFF_PREAMBLE` that keeps the note itself out
#: of the reply; every other line of a kickoff is written as plain principles.
NOTE_CONFIDENTIALITY = crewmate_prompts.text("welcome.note_confidentiality")

_WELCOME_INTRO = crewmate_prompts.text("welcome.intro")

#: The crew sentence for a user with no crewmate yet besides this one.
_FIRST_OF_CREW = crewmate_prompts.text("welcome.first_of_crew")

#: The crew sentence for a user who already has other crewmates.
_ONE_MORE_OF_CREW = crewmate_prompts.text("welcome.one_more_of_crew")

_WELCOME_ASK_NAME = crewmate_prompts.text("welcome.ask_name")

_WELCOME_OFFER = _WELCOME_ASK_NAME + crewmate_prompts.text("welcome.offer_tools")

#: :data:`_WELCOME_OFFER` for a Mate whose sessions do not load ``kirocrew-mate``
#: (``member_mate_tools``): the user saves the name on its crewmate page.
_WELCOME_OFFER_NO_TOOLS = _WELCOME_ASK_NAME + crewmate_prompts.text("welcome.offer_no_tools")

#: Mate's welcome for a user with no other crewmate. ``{name}`` is its label.
FIRST_WELCOME = _KICKOFF_PREAMBLE + _WELCOME_INTRO + _FIRST_OF_CREW + _WELCOME_OFFER

#: Mate's welcome for a user who already has other crewmates.
FIRST_WELCOME_WITH_CREW = _KICKOFF_PREAMBLE + _WELCOME_INTRO + _ONE_MORE_OF_CREW + _WELCOME_OFFER

FIRST_WELCOME_NO_TOOLS = (
    _KICKOFF_PREAMBLE + _WELCOME_INTRO + _FIRST_OF_CREW + _WELCOME_OFFER_NO_TOOLS
)
FIRST_WELCOME_WITH_CREW_NO_TOOLS = (
    _KICKOFF_PREAMBLE + _WELCOME_INTRO + _ONE_MORE_OF_CREW + _WELCOME_OFFER_NO_TOOLS
)


#: A created crewmate's first message: a hello with its name, then one plain
#: question about its goal. The user just picked its name and look on the
#: card, so the hello stays short and the question carries the message. The
#: answer becomes a ``set_goal`` card, and the goal's follow-up comes from the
#: check-ins proposed once it is saved (``dashboard/goal_setup.py``,
#: ``set_check_ins``), so the kickoff offers no schedule of its own.
_GOAL_HELLO = _KICKOFF_PREAMBLE + crewmate_prompts.text("welcome.goal_hello")

GOAL_WELCOME = _GOAL_HELLO + crewmate_prompts.text("welcome.goal_tools")

#: :data:`GOAL_WELCOME` for a crewmate whose sessions do not load
#: ``kirocrew-mate`` (``member_mate_tools``): the user saves the goal on the
#: crewmate's Goals tab.
GOAL_WELCOME_NO_TOOLS = _GOAL_HELLO + crewmate_prompts.text("welcome.goal_no_tools")

#: Appended to :data:`GOAL_WELCOME` when the create carried a description, so
#: the crewmate confirms that goal instead of asking blind.
_GOAL_DRAFT = crewmate_prompts.text("welcome.goal_draft")
_GOAL_KNOWN = _GOAL_DRAFT + crewmate_prompts.text("welcome.goal_known_tools")
_GOAL_KNOWN_NO_TOOLS = _GOAL_DRAFT + crewmate_prompts.text("welcome.goal_known_no_tools")


def _recorded_goal(slug: str, member: str) -> str:
    """*member*'s goals from its goal record, one per line, or ``""``.

    Total: an unreadable record only means the kickoff asks for the goal.
    """
    if not slug:
        return ""
    from kiro_crew import member_goals

    try:
        goals = member_goals.read_goals(slug, member).get("goals") or []
    except Exception:
        logger.debug("goal record unreadable for %r; asking for the goal", slug, exc_info=True)
        return ""
    return "\n".join(
        g["text"].strip() for g in goals if isinstance(g.get("text"), str) and g["text"].strip()
    )


def goal_kickoff(entry: object, member: str, slug: str = "", *, has_mate_tools: bool = True) -> str:
    """The hidden kickoff for crewmate *member*'s first turn; *entry* is its row.

    Names the crewmate by its label (its key when unlabelled) and, when the
    create carried a description or the crewmate's goal record (under *slug*)
    already holds goals, asks it to confirm them. *has_mate_tools* picks the
    wording: ``set_goal`` with the tools, the Goals tab without them.
    """
    label = getattr(entry, "display_name", "") or ""
    label = label.strip() if isinstance(label, str) else ""
    welcome = GOAL_WELCOME if has_mate_tools else GOAL_WELCOME_NO_TOOLS
    known = _GOAL_KNOWN if has_mate_tools else _GOAL_KNOWN_NO_TOOLS
    text = welcome.format(name=label or member)
    description = getattr(entry, "description", "") or ""
    description = description.strip() if isinstance(description, str) else ""
    if not description:
        description = _recorded_goal(slug, member)
    return text + known.format(description=description) if description else text


def welcome_kickoff(
    entry: object, *, has_other_crewmates: bool, has_mate_tools: bool = True
) -> str:
    """The hidden kickoff for Mate's first turn; *entry* is its config row.

    *has_other_crewmates* is whether the roster holds a crewmate besides Mate
    and the reserved ``default`` member; *has_mate_tools* whether its sessions
    load ``update_self`` to take the name it is given.
    """
    label = getattr(entry, "display_name", "") or ""
    label = label.strip() if isinstance(label, str) else ""
    if has_mate_tools:
        template = FIRST_WELCOME_WITH_CREW if has_other_crewmates else FIRST_WELCOME
    else:
        template = (
            FIRST_WELCOME_WITH_CREW_NO_TOOLS if has_other_crewmates else FIRST_WELCOME_NO_TOOLS
        )
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
    """Start the first welcome *slug*'s crewmate owes: Mate's, or a goal question.

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
    kind = await asyncio.to_thread(members_mod.owed_welcome_kind, slug, member)
    if kind is None:
        return NOT_OWED
    from kiro_crew.member_mate_tools import binding_has_mate_tools

    has_tools = await asyncio.to_thread(binding_has_mate_tools, entry)
    if kind == members_mod.WELCOME_KIND_GOAL:
        kickoff = await asyncio.to_thread(
            goal_kickoff, entry, member, slug, has_mate_tools=has_tools
        )
    else:
        kickoff = welcome_kickoff(entry, has_other_crewmates=others, has_mate_tools=has_tools)
    if not await asyncio.to_thread(claim_greeting_marker, slug, slot.key):
        return ALREADY_GREETED
    if slot.running or _has_content(slot) or state._slots.get(slot.key) is not slot:
        # Lost the race to a real send (or the slot was replaced) after the
        # claim. The user is already talking, so the greeting is simply moot.
        return NOT_EMPTY if _has_content(slot) else BUSY
    _dispatch_greeting(state, slot, kickoff)
    return STARTED


def _dispatch_greeting(state: Any, slot: Any, kickoff: str) -> None:
    """Run the kickoff as a gateway-authored turn, with no transcript row for it."""
    # The owner opened this thread, so a human is demonstrably watching it.
    dispatch_hidden_turn(state, slot, kickoff, human_seen=True)


def app_is_stopping(state: Any) -> bool:
    """Whether the gateway is shutting down or restarting, so no new turn should start.

    The same answer the turn runner's own shutdown check gives
    (``session.process_shutdown_requested``): a signal shutdown, or the final
    drain an in-app restart starts before it re-execs.
    """
    from kiro_crew.session import process_shutdown_requested

    return process_shutdown_requested(getattr(state, "sessions", None))


def dispatch_hidden_turn(
    state: Any, slot: Any, prompt: str, *, human_seen: bool, heartbeat: bool = False
) -> bool:
    """Run *prompt* as one gateway-authored turn in *slot*, with no user row for it.

    Shared by the first welcome and the goal check-ins
    (:mod:`kiro_crew.dashboard.goal_reminder`). The turn is not one the user
    sent (``_turn_user_sent`` stays false), so the operations only a user's turn
    may run (a guide, ``update_self``, most cards) refuse inside it; in a
    crewmate's own thread its goal, milestones and check-ins cards are still
    admitted (``handlers.guide.REQ_OWN_GOAL_TURN``). Its pre-tool text is held
    until the turn ends (:mod:`kiro_crew.dashboard.heartbeat_quiet_end`): a
    quiet end or a final reply folds that narration into the steps. *heartbeat*
    marks the heartbeat wake.

    Returns False, starting nothing, while the app is stopping
    (:func:`app_is_stopping`). A restart cancels every running turn, and a
    worker cut by it ends a turn that would wake its crewmate here; a turn
    started then writes its in-flight marker and is gone a moment later, so
    the restored thread showed "interrupted when the app restarted" on a
    thread whose own work had finished before the restart.
    """
    # Deferred: chat_handlers / chat import this package's handlers at load.
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.dashboard.chat_handlers import _sweep_stale_permissions
    from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn

    if app_is_stopping(state):
        logger.info("hidden gateway turn on %s not started: the app is stopping", slot.key)
        return False
    if human_seen:
        slot._human_seen = True
    slot._has_reader = False  # delivered over the WebSocket, like a ws send
    slot._file_changes = []
    _sweep_stale_permissions(slot)
    # Every hidden turn holds its pre-tool text (``_hidden_turn``); the heartbeat
    # flag is passed only when set.
    turn = (
        _run_chat(
            state,
            slot,
            prompt,
            _synthetic_payload=True,
            _turn_actor="gateway",
            _heartbeat_turn=True,
            _hidden_turn=True,
        )
        if heartbeat
        else _run_chat(
            state,
            slot,
            prompt,
            _synthetic_payload=True,
            _turn_actor="gateway",
            _hidden_turn=True,
        )
    )
    task = spawn_guarded_turn(state, slot, state.run_background_turn(slot, turn))
    slot.task = task
    state.push_slots_update()
    logger.info("hidden gateway turn started on %s", slot.key)
    return True
