"""The message that started a Slack thread, for a session born inside it.

A Slack-born session opened by a reply in a thread it did not start -- the owner
answering a DM an agent sent with ``send_message(session="slack")``, a reply
under a cron post, a mention in someone else's channel thread -- begins with no
record of what the reply answers. :func:`fetch_thread_parent` reads that first
message once, and it reaches two readers:

* the model, as ``thread_parent_text`` (:func:`parent_prompt_text`), which
  ``ContextBuilder.build_message`` screens for prompt injection and wraps in its
  fenced ``[SLACK THREAD CONTEXT — UNTRUSTED DATA]`` block;
* the person, as one ``notice`` row above the reply in the session's transcript
  (:func:`record_thread_parent`), which the dashboard draws as a notice card.

A ``notice`` row is display-only (``history_projection.DISPLAY_ONLY_ROLES``). It
is not in ``context.RECALL_ROLES``, so no history replay, recall or compression
hands it to the model as a turn; ``recent_with_provenance``, memory consolidation
and auto-skill detection skip it too. The fenced block stays the model's only
copy of the parent.

The parent can be written by anyone who can post in the thread, so it is
untrusted in both places. A parent that matches a prompt-injection pattern is
withheld from the prompt by ``build_message``, and its text is withheld from the
transcript row too. The row's text also goes through the prompt block's marker
neutralizers.

"Did not start" is judged by the transcript: a session with no user or assistant
row yet. A session with prior turns fetches nothing and records nothing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kiro_crew.context import _neutralize_fence_markers, _neutralize_structural_markers
from kiro_crew.messaging.link import SLACK_NAMESPACE
from kiro_crew.security import contains_injection, redact

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog
    from kiro_crew.slack.client import SlackClientOps

logger = logging.getLogger(__name__)

#: Longest parent text handed to the model or shown in the transcript row.
PARENT_TEXT_CAP = 3000

#: The transcript row kind: persisted and drawn by the dashboard, never replayed.
#: The same role/class pair channel dispatchers already write for their notices.
NOTICE_ROLE = "notice"
NOTICE_CLS = "msg msg-info"

#: Rows that make a transcript a conversation already under way.
_TURN_ROLES = frozenset({"user", "assistant"})

#: Markdown image opener, and the CommonMark escape that keeps it literal text.
#:
#: ``ConversationLog.append`` copies the images a non-``user`` row references into
#: session storage (``_persist_inline_attachments``) on the premise that such a
#: row is agent-authored. The notice row is not: anyone who can post in the
#: thread wrote it, so a parent naming a local picture would have this host read
#: and copy an operator-chosen file. The escape is what ``persist_inline_images``
#: and its readers already treat as "not an image" (``chat_attachments``,
#: ``outbound_files.IMAGE_MD_RE`` match only the unescaped opener), and the
#: dashboard draws the row as plain text, so the person still reads what was
#: written.
_IMAGE_OPENER = "!["
_IMAGE_OPENER_ESCAPED = "!\\["


@dataclass(frozen=True)
class ThreadParent:
    """A thread's first message: redacted text and who posted it."""

    text: str
    #: Display name of the author; empty when Slack did not say.
    author: str
    #: The author's Slack user or bot id; empty when Slack did not say.
    author_id: str


def is_slack_born(session_key: str) -> bool:
    """True for a conversation keyed by Slack itself, not a dashboard link or an asker."""
    return session_key.startswith(f"{SLACK_NAMESPACE}:")


async def has_prior_turns(conversation_log: "ConversationLog | None", session_key: str) -> bool:
    """Whether *session_key*'s transcript already holds a user or assistant row.

    A notice alone does not count: a turn that died after recording the parent
    must still hand it to the model on the next attempt. An unreadable
    transcript counts as prior turns, so doubt never adds a fetch or a row.
    """
    if conversation_log is None:
        return False
    try:
        rows = await asyncio.to_thread(
            conversation_log.recent, session_key, max_messages=1, roles=_TURN_ROLES
        )
    except Exception:
        logger.debug("thread parent: transcript read failed session=%s", session_key, exc_info=True)
        return True
    return bool(rows)


async def fetch_thread_parent(
    slack: "SlackClientOps",
    channel: str,
    thread_ts: str,
    *,
    with_author: bool = True,
) -> ThreadParent | None:
    """Read the message at *thread_ts* in *channel*, or None when unavailable.

    Skip the separate human profile lookup when only the prompt text will be used.
    """
    detail = await slack.fetch_message_detail(channel, thread_ts)
    if not detail or not detail.get("text"):
        return None
    author, author_id = await _author(slack, detail) if with_author else ("", "")
    return ThreadParent(text=redact(detail["text"]), author=author, author_id=author_id)


async def _author(slack: "SlackClientOps", detail: dict[str, str]) -> tuple[str, str]:
    """``(display name, id)`` of whoever posted *detail*."""
    bot_id = detail.get("bot_id", "")
    if bot_id:
        return detail.get("bot_name", "") or "a Slack app", bot_id
    user = detail.get("user", "")
    if not user:
        return "", ""
    name = ""
    # Not on the abstract client: RealSlackClient has it, test doubles may not.
    get_user_info = getattr(slack, "get_user_info", None)
    if callable(get_user_info):
        try:
            info = await get_user_info(user)
            name = str(info.get("real_name") or "")
        except Exception:
            logger.debug("thread parent: author lookup failed for %s", user, exc_info=True)
    return name or user, user


def parent_prompt_text(parent: ThreadParent) -> str:
    """The parent as ``build_message``'s ``thread_parent_text``."""
    text = parent.text
    if len(text) > PARENT_TEXT_CAP:
        text = text[:PARENT_TEXT_CAP] + (
            "\n[truncated — use the Slack MCP server's thread-reading tool "
            "(e.g. get_thread) for full text]"
        )
    return text


def transcript_notice(parent: ThreadParent) -> str:
    """The transcript row's text: who started the thread, and what they wrote.

    The row is display-only (``history_projection.DISPLAY_ONLY_ROLES``), so no
    model-bound reader carries it. It is still scrubbed with the same marker
    neutralizers the fenced prompt block applies, so a copy that reaches a model
    by some future route cannot forge a prompt boundary either. A markdown image
    opener is escaped last (:data:`_IMAGE_OPENER_ESCAPED`), after the neutralizers
    whose placeholders can form one, so the write boundary does not copy a
    picture this untrusted text names. All are span-local: ordinary text is
    stored unchanged.
    """
    who = redact(parent.author) if parent.author else "someone"
    body = parent.text[:PARENT_TEXT_CAP]
    if contains_injection(body):
        text = (
            f"Thread started by {who} on Slack. Its first message is hidden because it "
            "looked like a prompt-injection attempt — read it in Slack."
        )
    else:
        if len(parent.text) > PARENT_TEXT_CAP:
            body += "… — read the rest in Slack."
        text = f"Thread started by {who} on Slack:\n{body}"
    # The escape runs LAST. Both neutralizers substitute a placeholder that opens
    # with ``[``, so a marker written right after ``!`` (``!<<<UNTRUSTED…(/p)``)
    # holds no opener until they run, and escaping first would leave the one
    # they create. The reverse cannot happen: the escape only inserts a
    # backslash, which no marker pattern contains or tolerates as a separator.
    text = _neutralize_structural_markers(_neutralize_fence_markers(text))
    return text.replace(_IMAGE_OPENER, _IMAGE_OPENER_ESCAPED)


def _record_if_empty(
    conversation_log: "ConversationLog",
    session_key: str,
    parent: ThreadParent,
    agent: str | None,
) -> bool:
    # One lock hold for the look and the write, so two writers cannot both find
    # the transcript empty and each add a copy.
    with conversation_log.atomic_appends(session_key):
        if conversation_log.has_messages(session_key):
            return False
        conversation_log.append(
            session_key,
            NOTICE_ROLE,
            transcript_notice(parent),
            source_thread=session_key,
            source_user=parent.author_id or None,
            # This append creates the session file, and only the creating append
            # records the agent in its metadata header.
            agent=agent,
            cls=NOTICE_CLS,
        )
        return True


async def record_thread_parent(
    conversation_log: "ConversationLog",
    session_key: str,
    parent: ThreadParent,
    *,
    agent: str | None,
) -> bool:
    """Write the parent as the transcript's first row, once.

    Writes only into a transcript that holds no message yet, so it lands above
    the reply and a later turn never adds a second copy. Returns whether it wrote.
    Best-effort: a failure is logged and the turn goes on without the row.
    """
    try:
        return await asyncio.to_thread(
            _record_if_empty, conversation_log, session_key, parent, agent
        )
    except Exception:
        logger.warning(
            "thread parent: transcript row failed session=%s", session_key, exc_info=True
        )
        return False
