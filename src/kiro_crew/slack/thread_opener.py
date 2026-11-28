"""A top-level Slack message that only opens a thread.

A person who ends a top-level message with the thread icon (``:thread:`` as Slack
sends it, or the emoji itself) is opening a thread to talk in, not asking for an
answer. ``slack/events.py`` leaves such a message alone: no turn, no reply, one log
line (:func:`leave_unanswered`). The thread works as any other from its first reply
on, and three things keep the opener from being lost:

* thread-follow admits an unmentioned reply in the thread (:func:`opened`, read by
  ``_thread_follow_admits``), as it would had the opener been answered;
* a session born in the thread reads the opener from Slack as the thread's first
  message, as it does for any thread it did not start (``slack/thread_parent.py``);
* a dashboard chat the thread's replies go to (a Channel Bot or Channel Conductor, a
  Bot joined with ``!bot``) holds a conversation already and reads no thread parent,
  so the linked-thread route seats the opener as context for the first turn a typed
  reply of its author starts (:func:`seat_opener`); the reply itself stays as typed.

A ``!`` command is never an opener: it runs as typed. Everything else about who is
heard is decided before this rule runs, so a sender who is refused today is refused
the same way.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

#: The thread icon closing a message: Slack's shortcode, or the emoji character.
_ICON_AT_END = re.compile(r"(?::thread:|\U0001f9f5)\Z")
#: The bot mention a message in a channel may open with.
_LEADING_MENTION = re.compile(r"\A<@[A-Z0-9]+(?:\|[^>]*)?>\s*")

#: Openers remembered; the oldest goes first.
_CAP = 1000
#: Longest opener text kept for :func:`seat_opener`.
_TEXT_CAP = 3000

#: ``(channel, ts)`` of each opener left unanswered -> ``(author, text)``; the text is
#: emptied once :func:`take_topic` hands it over.
# shortcut: memory-only, so after a gateway restart a reply in an opened thread needs a
# mention again and a dashboard chat is not handed the opener; persist it with the
# session map if that is not enough.
_openers: OrderedDict[tuple[str, str], tuple[str, str]] = OrderedDict()


def opener_text(text: str) -> str | None:
    """*text* past its leading mention when it is a thread opener, else ``None``.

    An opener ends with the thread icon; spaces and line breaks may follow it. A
    message that is only the icon is one too. A ``!`` command is not.
    """
    body = _LEADING_MENTION.sub("", (text or "").strip())
    if body.startswith("!") or not _ICON_AT_END.search(body):
        return None
    return body


def leave_unanswered(channel: str, msg_ts: str, sender_id: str, text: str) -> bool:
    """Whether top-level message *msg_ts* is a thread opener; remembered and logged if so."""
    body = opener_text(text)
    if body is None:
        return False
    _openers[(channel, msg_ts)] = (sender_id, body[:_TEXT_CAP])
    if len(_openers) > _CAP:
        _openers.popitem(last=False)
    logger.info("Thread opener %s from %s in %s left unanswered", msg_ts, sender_id, channel)
    return True


def opened(channel: str, thread_ts: str | None) -> bool:
    """Whether thread *thread_ts* of *channel* began with an opener left unanswered."""
    return bool(thread_ts) and (channel, thread_ts) in _openers


def take_topic(channel: str, thread_ts: str, sender_id: str) -> str:
    """The opener of *thread_ts*, once, for a reply by the person who wrote it.

    Only its author's reply carries it, so a turn never holds words its sender did
    not write. ``""`` for anyone else, for a thread with no remembered opener, and
    after the first hand-over.
    """
    author, text = _openers.get((channel, thread_ts), ("", ""))
    if not text or author != sender_id:
        return ""
    _openers[(channel, thread_ts)] = (author, "")
    return text


def seat_opener(slot: Any, channel: str, thread_ts: str, sender_id: str) -> dict[str, Any] | None:
    """Seat the opener of *thread_ts* as context for the turn dashboard *slot* starts now.

    One pending-context entry, so the reply that starts the turn stays as typed. Returns
    the seated entry for the caller to drop if the turn ends without taking it, or
    ``None``: no opener for this sender (:func:`take_topic`), a chat that was handed the
    thread's first message when it was linked (``chat_slack._seed_thread_parent``), or an
    entry the slot refused. A refused opener is dropped, never put into the reply's text.
    """
    topic = take_topic(channel, thread_ts, sender_id)
    seeded = getattr(slot, "_pending_thread_parent", None)
    if not topic or (isinstance(seeded, tuple) and seeded[1] == thread_ts):
        return None
    try:
        # Imported here: the dashboard package imports the Slack handlers.
        from kiro_crew.context import _neutralize_structural_markers
        from kiro_crew.dashboard.chat_handlers import _build_pending_context_entry

        entry, refused = _build_pending_context_entry(
            slot,
            "The sender opened this Slack thread with the message below, which got no "
            "reply; this is their first reply in the thread.\n"
            + _neutralize_structural_markers(topic),
            "slack-thread-opener",
            True,
            None,
        )
        if entry is not None and refused is None and slot.append_pending_context(entry):
            return entry
    except Exception:
        logger.debug("thread opener not queued as pending context", exc_info=True)
    logger.info("Thread opener %s not handed to slot %s", thread_ts, getattr(slot, "key", "?"))
    return None
