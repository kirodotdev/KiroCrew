"""Tell a crewmate's thread what the user did with something it put in front of them.

``crew-mode.md``, "The user's answer reaches the crewmate". A card press or a Run in
terminal click starts no turn, so without this the crewmate waited for the user's
next message to learn its card was applied, declined or undone, or that the command
it handed over ran. Each such answer becomes ONE line queued on the thread that owns
the card or the command (:func:`mate_thread_wakes.queue_user_action`); the thread's
flush delivers every queued line as one hidden ``[User action]`` turn once the thread
is idle, never as a steer into a running turn.

* **Only that crewmate's own pinned thread** is told: a card's recorded
  ``slot_key``, or the slot the Run in terminal host owns. Any other slot (the main
  assistant, a worker, an app) is told nothing new, quietly.
* **Facts only**: which card or command, what the user did, the outcome (the error
  when it failed). The guidance asks for nothing beyond what the work calls for.
* **One note per answer**: a card kind whose apply already wakes the crewmate
  (goal, milestones, page offer, capabilities, the tool-access opt-in) marks the
  request with :data:`REQ_CARD_OWN_WAKE`, and that apply sends no second note.

Expiry, a closed conversation's retirement and hiding a finished card are not the
user's answer, so they send nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from kiro_crew import crewmate_prompts

logger = logging.getLogger(__name__)

#: The envelope a user-action wake opens with.
USER_ACTION_PREFIX = "[User action]"

#: Request attribute a card step's route sets when it already scheduled its own wake.
REQ_CARD_OWN_WAKE = "card_own_wake"

#: How much of a handed-over command one note carries.
MAX_COMMAND_CHARS = 1000
#: How much of a card's title, summary or error one note carries.
_TEXT_CHARS = 300
#: How many lines one thread holds before the oldest is dropped.
MAX_PENDING_LINES = 12

_BEGIN = "<<<command"
_END = ">>>end of command"

CARD_APPLIED = "applied"
CARD_PARTIAL = "partial"
CARD_APPLY_FAILED = "apply_failed"
CARD_DECLINED = "declined"
CARD_UNDONE = "undone"
CARD_UNDO_FAILED = "undo_failed"

_CARD_PIECES = {
    CARD_APPLIED: "user_action.card_applied",
    CARD_PARTIAL: "user_action.card_partial",
    CARD_APPLY_FAILED: "user_action.card_apply_failed",
    CARD_DECLINED: "user_action.card_declined",
    CARD_UNDONE: "user_action.card_undone",
    CARD_UNDO_FAILED: "user_action.card_undo_failed",
}
#: Outcomes an apply that wakes the crewmate on its own already covers.
_OWN_WAKE_OUTCOMES = frozenset({CARD_APPLIED, CARD_PARTIAL})
#: A goal card's outcomes that ``rfc-crewmate-goals`` §3.8 keeps silent.
_KIND_GOAL = "crewmate.goal"
_GOAL_SILENT_OUTCOMES = frozenset({CARD_APPLIED, CARD_PARTIAL, CARD_UNDONE})

TERMINAL_RAN = "ran"
TERMINAL_COPIED = "copied"
TERMINAL_FAILED = "failed"
_TERMINAL_PIECES = {
    TERMINAL_RAN: "user_action.terminal_ran",
    TERMINAL_COPIED: "user_action.terminal_copied",
    TERMINAL_FAILED: "user_action.terminal_failed",
}
TERMINAL_OUTCOMES = frozenset(_TERMINAL_PIECES)
#: Why a Run in terminal did not run, as the host reports it.
_TERMINAL_REASONS = {
    "not_ready": "user_action.terminal_reason_not_ready",
    "remote_chat": "user_action.terminal_reason_remote_chat",
    "no_tab": "user_action.terminal_reason_no_tab",
    "not_sent": "user_action.terminal_reason_not_sent",
    "copy_failed": "user_action.terminal_reason_copy_failed",
}
TERMINAL_REASONS = frozenset(_TERMINAL_REASONS)
#: Where the command was: a run-it-yourself card or a shell code block.
_TERMINAL_SOURCES = {
    "run_yourself": "user_action.terminal_source_run_yourself",
    "code_block": "user_action.terminal_source_code_block",
}
TERMINAL_SOURCES = frozenset(_TERMINAL_SOURCES)
_DEFAULT_SOURCE = "code_block"
_DEFAULT_REASON = "not_sent"


def _flat(value: Any, cap: int = _TEXT_CHARS) -> str:
    """*value* as one scrubbed line, cut to *cap*; brackets defused so it cannot open a block."""
    from kiro_crew.member_goals import scrub_text

    text = " ".join(scrub_text(str(value or "")).split())
    text = text.replace("[", "(").replace("]", ")")
    return text[:cap].rstrip()


def card_line(outcome: str, *, kind: Any, title: Any, summary: Any = "", error: Any = "") -> str:
    """The one line saying what the user did with card *title* of *kind*."""
    detail = ""
    summary_text = _flat(summary)
    if summary_text and outcome != CARD_DECLINED:
        detail += crewmate_prompts.fill("user_action.detail_result", text=summary_text)
    error_text = _flat(error)
    if error_text and outcome in (CARD_PARTIAL, CARD_APPLY_FAILED, CARD_UNDO_FAILED):
        detail += crewmate_prompts.fill("user_action.detail_error", text=error_text)
    return crewmate_prompts.fill(
        _CARD_PIECES[outcome], title=_flat(title), kind=_flat(kind, 64), detail=detail
    )


def terminal_line(outcome: str, *, source: str, reason: str, command: str) -> str:
    """The lines saying what became of a command the user chose to run in a terminal."""
    source_key = _TERMINAL_SOURCES.get(source) or _TERMINAL_SOURCES[_DEFAULT_SOURCE]
    where = crewmate_prompts.text(source_key)
    if outcome == TERMINAL_FAILED:
        reason_key = _TERMINAL_REASONS.get(reason) or _TERMINAL_REASONS[_DEFAULT_REASON]
        why = crewmate_prompts.text(reason_key)
        head = crewmate_prompts.fill(_TERMINAL_PIECES[outcome], source=where, reason=why)
    else:
        head = crewmate_prompts.fill(_TERMINAL_PIECES[outcome], source=where)
    from kiro_crew.member_goals import scrub_text

    body = scrub_text(command).replace(_END, "").strip()
    if len(body) > MAX_COMMAND_CHARS:
        body = body[:MAX_COMMAND_CHARS].rstrip() + " …"
    return "\n".join([head, _BEGIN, body, _END])


def prompt(lines: list[str]) -> str:
    """The hidden turn that hands the crewmate every queued answer, oldest first."""
    preamble = crewmate_prompts.text("wake.preamble").rstrip()
    out = [USER_ACTION_PREFIX + " " + preamble, "", crewmate_prompts.text("user_action.head")]
    out.extend(lines)
    out.extend(["", crewmate_prompts.text("user_action.guidance")])
    return "\n".join(out)


def step_outcome(
    before: Mapping[str, Any] | None, after: Mapping[str, Any], op: str, *, refused: bool
) -> str | None:
    """What one card step the user pressed settled, from the record before and after it.

    An Apply that reached ``applied``, ``partial`` or ``failed`` (each stamps a new
    ``finished_at``); an Undo that reached ``undone``, or one that was refused and
    left the change in place. A step in the middle of a plan, a poll still
    waiting, a replay of a finished card and a refusal that put the card back as
    it was settle nothing.
    """
    from kiro_crew.change_card_catalog import OP_APPLY, OP_UNDO

    status = after.get("status")
    prior = before or {}
    if op == OP_APPLY:
        if prior.get("status") == status and prior.get("finished_at") == after.get("finished_at"):
            return None
        return {"applied": CARD_APPLIED, "partial": CARD_PARTIAL, "failed": CARD_APPLY_FAILED}.get(
            str(status)
        )
    if op == OP_UNDO:
        if status == "undone":
            return None if prior.get("status") == "undone" else CARD_UNDONE
        changed = (after.get("error") or None) != (prior.get("error") or None)
        if status in ("applied", "partial") and after.get("error") and (refused or changed):
            return CARD_UNDO_FAILED
    return None


def _crewmate_thread(state: Any, slot_key: Any) -> Any:
    from kiro_crew.dashboard.mate_thread_wakes import is_crewmate_thread

    if state is None or not isinstance(slot_key, str) or not slot_key:
        return None
    slot = state.get_slot(slot_key)
    return slot if is_crewmate_thread(slot) else None


def note_card(state: Any, rec: Mapping[str, Any], outcome: str | None, *, own_wake: bool) -> bool:
    """Queue *outcome* of card *rec* on the crewmate thread that raised it. Never raises.

    Nothing for a card raised anywhere but a crewmate's own pinned thread, and
    nothing for an apply whose route already woke the crewmate (*own_wake*). A
    goal card's apply and Undo say nothing either, whatever its plan's steps:
    ``rfc-crewmate-goals`` §3.8 has a new goal wake through its own nudge, and an
    Undo or a small edit send nothing.
    """
    try:
        if outcome not in _CARD_PIECES or (own_wake and outcome in _OWN_WAKE_OUTCOMES):
            return False
        if rec.get("kind") == _KIND_GOAL and outcome in _GOAL_SILENT_OUTCOMES:
            return False
        slot = _crewmate_thread(state, rec.get("slot_key"))
        if slot is None:
            logger.debug("card %s answer told nobody: not a crewmate thread", rec.get("id"))
            return False
        line = card_line(
            outcome,
            kind=rec.get("kind"),
            title=rec.get("title"),
            summary=(rec.get("result") or {}).get("summary", ""),
            error=(rec.get("error") or {}).get("message", ""),
        )
        from kiro_crew.dashboard.mate_thread_wakes import queue_user_action

        return queue_user_action(state, slot.key, line)
    except Exception:
        logger.debug("card %s answer note failed", rec.get("id"), exc_info=True)
        return False


def note_terminal_run(
    state: Any, slot_key: str, *, outcome: str, source: str, reason: str, command: str
) -> bool:
    """Queue what became of a Run in terminal on crewmate thread *slot_key*. Never raises."""
    try:
        slot = _crewmate_thread(state, slot_key)
        if slot is None or outcome not in TERMINAL_OUTCOMES:
            logger.debug("terminal run on %s told nobody", slot_key)
            return False
        line = terminal_line(outcome, source=source, reason=reason, command=command)
        from kiro_crew.dashboard.mate_thread_wakes import queue_user_action

        return queue_user_action(state, slot.key, line)
    except Exception:
        logger.debug("terminal run note on %s failed", slot_key, exc_info=True)
        return False
