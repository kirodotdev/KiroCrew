"""The pending-question note the notification bridge carries (RFC bridge actions, B4e).

When an agent's blocking ``ask_question`` card opens on a dashboard tab, this
publishes one ``system.approval`` note about it, behind the same default-off
``bridge_approvals`` preview as the pending-approval note, so a chat app learns a
decision is waiting. Like that note it is bridge-only: the dashboard bell never
sees it.

A card with ONE single-select question is bound to a single-use token, and the
Slack sink posts one button per option; a press answers the card exactly as the
dashboard would for that option (``DashboardState.resolve_question`` with
``{question: option label}``), so whichever surface answers first wins. Any other
card (several questions, multi-select, or a free-form answer the owner wants to
type) gets no buttons: the note links to the chat in the dashboard instead.

A question note is told apart from the approval note on the same channel by its
producer meta key (``bridge_approval_note.PRODUCER_KEY``), never by its channel.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

#: The producer value that marks a pending agent question.
PRODUCER = "agent_question"

#: The meta key naming the ask the note is about.
ASK_KEY = "ask_id"

#: The meta key carrying the option labels the Slack buttons offer, in order.
OPTIONS_KEY = "question_options"


def is_pending_question_note(note: Mapping[str, Any]) -> bool:
    """Whether *note* is this producer's pending-question note."""
    from kiro_crew.notifications.bridge_approval_note import PRODUCER_KEY

    return note.get("channel") == "system.approval" and note.get(PRODUCER_KEY) == PRODUCER


def answerable_options(questions: Any) -> tuple[str, list[str]] | None:
    """``(question, option labels)`` when one press can answer the card, else None.

    Only a card with exactly one single-select question qualifies; its answer is
    then exactly one of its option labels, the value the dashboard submits.
    """
    if not isinstance(questions, list) or len(questions) != 1:
        return None
    question = questions[0]
    if not isinstance(question, Mapping) or question.get("multiSelect"):
        return None
    text = question.get("question")
    options = question.get("options")
    if not isinstance(text, str) or not text or not isinstance(options, list):
        return None
    labels: list[str] = [
        label
        for o in options
        # A blank label would draw an empty button, which Slack refuses whole.
        if isinstance(o, Mapping) and isinstance(label := o.get("label"), str) and label.strip()
    ]
    if not labels or len(labels) != len(options):
        return None
    return text, labels


def bind_pending_question(
    state: Any, ask_id: str, session_key: str, questions: Any, ttl_s: float
) -> tuple[str, list[str]]:
    """Bind the open card *ask_id* so Slack buttons can answer it. Never raises.

    Returns ``(token, option labels)``, or ``("", [])`` when the card is not
    answerable by one press, there is no registry, no Slack owner or no live
    card, or the bind is refused: then the note carries no buttons.
    """
    try:
        shape = answerable_options(questions)
        registry = getattr(state, "bridge_actions", None)
        owner = getattr(state, "owner_id", "")
        future = getattr(state, "_question_futures", {}).get(ask_id)
        if shape is None or registry is None or future is None or future.done():
            return "", []
        if not isinstance(owner, str) or not owner:
            return "", []
        text, labels = shape

        def settle(answers: Any) -> bool:
            # The dashboard's own resolution: records the reason, retires the
            # card on every screen and settles the shared decision.
            try:
                return bool(state.resolve_question(ask_id, answers))
            except Exception:
                logger.warning("bridged question answer failed", exc_info=True)
                return False

        token = registry.bind(
            session_key,
            ask_id,
            future,
            choices={f"option_{i}": {text: label} for i, label in enumerate(labels)},
            ttl_s=ttl_s,
            owner=owner,
            kind="question",
            settle=settle,
        )
        return (token, labels) if isinstance(token, str) and token else ("", [])
    except Exception:
        logger.warning("pending-question bridge binding failed", exc_info=True)
        return "", []


def publish_pending_question(
    state: Any,
    cfg: Any,
    *,
    ask_id: str,
    slot_key: str,
    session_key: str,
    questions: Any,
    session_title: str = "",
    ttl_s: float = 0.0,
) -> dict[str, Any] | None:
    """Hand one pending-question note to the notification bridge. Never raises.

    Returns the note handed over, or None when the preview is off, the gateway has
    no bridge, or the note could not be built.
    """
    try:
        from kiro_crew.notifications.bridge_approval_note import (
            ACTION_TOKEN_KEY,
            PRODUCER_KEY,
            enabled,
        )

        if not enabled(cfg) or getattr(state, "notification_bridge", None) is None:
            return None
        from kiro_crew.notifications.bus import NotificationBus, payload_from_legacy

        first = questions[0] if isinstance(questions, list) and questions else {}
        text = first.get("question") if isinstance(first, Mapping) else ""
        count = len(questions) if isinstance(questions, list) else 0
        where = f" in {session_title}" if session_title else ""
        body = text if isinstance(text, str) and count == 1 else f"{count} questions are waiting."
        meta: dict[str, Any] = {
            PRODUCER_KEY: PRODUCER,
            ASK_KEY: ask_id,
            "slot": slot_key,
            "session_key": session_key,
        }
        token, labels = bind_pending_question(state, ask_id, session_key, questions, ttl_s)
        if token:
            meta[ACTION_TOKEN_KEY] = token
            meta[OPTIONS_KEY] = labels
        payload = payload_from_legacy(
            "approval",
            f"Question waiting{where}",
            body,
            meta,
            # Every card can be answered in full in the dashboard, free text included.
            # ``sid`` is the query the dashboard selects a session by; a bare
            # ``/chat/<slot>`` path would open whichever chat was active.
            url=f"/chat?sid={quote(slot_key, safe='')}",
        )
        # The bridge is the only sink: the dashboard bell never sees this note.
        return NotificationBus(sink=state._bridge_note).push(payload)
    except Exception:
        logger.warning("pending-question bridge note failed", exc_info=True)
        return None


def schedule_pending_question(state: Any, **fields: Any) -> "asyncio.Task[None] | None":
    """Start the hand-off of one open card as its own task, and return it.

    The open route calls this after the card reached a client and never awaits
    it. The task reads the live config off the loop, sends nothing for a card
    answered in the meantime, and is held until done with any failure logged.
    *fields* are :func:`publish_pending_question`'s keywords.
    """
    # The approval note's own holding set and failure log, shared, not copied.
    from kiro_crew.notifications.bridge_approval_note import _HANDOFF_TASKS, _log_failure

    try:
        task = asyncio.get_running_loop().create_task(_hand_off(state, fields))
    except RuntimeError:
        return None  # No running loop: nothing can bridge.
    tasks = getattr(state, "_background_tasks", None)
    if not isinstance(tasks, set):
        tasks = _HANDOFF_TASKS
    # Held until done: the loop keeps only a weak reference to a task.
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    task.add_done_callback(_log_failure)
    return task


async def _hand_off(state: Any, fields: dict[str, Any]) -> None:
    from kiro_crew.config import KiroCrewConfig

    try:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    except Exception:
        return  # No config, no preview: nothing is bridged.
    future = getattr(state, "_question_futures", {}).get(fields.get("ask_id"))
    if future is None or future.done():
        return  # Answered or gone while the config was read: nothing is pending.
    publish_pending_question(state, cfg, **fields)
