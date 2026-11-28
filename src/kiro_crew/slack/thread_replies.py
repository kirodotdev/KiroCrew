"""Replies posted in a Slack thread since the agent's last turn in it.

A turn that arrives in a thread reaches the agent with its own conversation
only. Replies other people posted in the thread in between -- or, for a session
born inside the thread, every reply before this one -- are otherwise invisible
unless the agent goes and reads them through the Slack MCP.
:func:`replies_since_last_turn` reads them with one ``conversations.replies``
call and hands them to ``ContextBuilder.build_message`` as
``thread_replies_text``, which wraps them in a fenced
``[SLACK THREAD REPLIES — UNTRUSTED DATA]`` block beside the thread parent's.

"Since the last turn" is the newest of two watermarks: the message this process
last ran a turn for in the thread (:func:`note_turn`, called only once a turn
whose replies read succeeded has landed), and, when that is not known (a
restart), the newest reply this app posted there. A session with no
turn in the thread at all sees every earlier reply. The set is bounded either
way: of the replies one ``conversations.replies`` page returns, the newest
:data:`MAX_REPLIES`, at most :data:`TEXT_CAP` characters each, at most
:data:`BYTE_CAP` bytes together.

Anyone who can post in the thread wrote these -- the text and the author name
alike -- so each formatted reply is redacted, one whose text or author name
matches a prompt-injection pattern is withheld whole (and audited), and
the fenced block frames the rest as data, never as instructions. This only adds
context: which messages the bot answers is decided before any of this runs.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from kiro_crew.security import audit_injection_dropped, contains_injection, redact
from kiro_crew.slack.enterprise import validated_self_bot_id
from kiro_crew.slack.events_block_text import _SLACK_BLOCK_FALLBACKS, _extract_blocks_text

if TYPE_CHECKING:
    from kiro_crew.slack.client import SlackClientOps

logger = logging.getLogger(__name__)

#: Newest replies kept.
MAX_REPLIES = 20
#: Longest text kept from one reply.
TEXT_CAP = 1500
#: Longest author name kept for one reply.
AUTHOR_CAP = 80
#: Most UTF-8 bytes the formatted replies may take together.
BYTE_CAP = 8000
#: One ``conversations.replies`` page.
_FETCH_LIMIT = 200
#: Threads remembered for the last-turn watermark.
_WATERMARK_SLOTS = 4096

_WITHHELD = "[withheld: this reply matched a prompt-injection pattern]"

#: What Slack appends to its plain ``text`` copy of a message whose content
#: lives in blocks (a streamed reply with a table, for one). Not content.
_BLOCKS_TEXT_TAIL = ", with interactive elements"

#: ``(session_key, thread_ts)`` -> ts of the message the last turn answered.
_last_turn: OrderedDict[tuple[str, str], str] = OrderedDict()


@dataclass(frozen=True)
class ThreadReplies:
    """What one replies read produced for a turn."""

    #: The formatted unseen replies, or None when there are none to show.
    text: str | None
    #: Whether Slack answered the read. Only then may the watermark move.
    read_ok: bool


_NOT_READ = ThreadReplies(text=None, read_ok=False)


def note_turn(session_key: str, thread_ts: str, msg_ts: str) -> None:
    """Remember that a turn for *msg_ts* in *thread_ts* landed after a good read."""
    key = (session_key, thread_ts)
    _last_turn[key] = msg_ts
    _last_turn.move_to_end(key)
    while len(_last_turn) > _WATERMARK_SLOTS:
        _last_turn.popitem(last=False)


def has_noted_turn(session_key: str, thread_ts: str) -> bool:
    """Whether this process already ran a turn for *session_key* in *thread_ts*."""
    return (session_key, thread_ts) in _last_turn


def _ts(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _is_own(msg: dict, own_bot_id: str) -> bool:
    return bool(own_bot_id) and msg.get("bot_id") == own_bot_id


def _author(msg: dict, own_bot_id: str) -> str:
    """Who posted *msg*, at most :data:`AUTHOR_CAP` characters."""
    return _author_name(msg, own_bot_id)[:AUTHOR_CAP]


def _author_name(msg: dict, own_bot_id: str) -> str:
    if _is_own(msg, own_bot_id):
        return "you (this app)"
    if msg.get("bot_id"):
        profile = msg.get("bot_profile") or {}
        return str(profile.get("name") or msg.get("username") or "a Slack app")
    profile = msg.get("user_profile") or {}
    name = str(profile.get("real_name") or profile.get("display_name") or "")
    user = str(msg.get("user") or "")
    if name and user:
        return f"{name} ({user})"
    return name or user or "someone"


def _body(msg: dict) -> str:
    """*msg*'s text: Slack's ``text`` copy, or its blocks rendered when that copy is incomplete.

    Slack marks a ``text`` copy that leaves block content out: it is empty or a
    generic placeholder, or it ends with :data:`_BLOCKS_TEXT_TAIL` (a streamed
    reply with a table, for one). Only such a copy is replaced by the blocks,
    rendered in order by the one Block Kit renderer, with the copy minus its tail
    as the fallback. Any other copy is the reply as its poster wrote it, blocks or
    not, and the renderer (which knows no ``header`` block and skips section
    fields) must not shorten it.
    """
    text = str(msg.get("text") or "").rstrip()
    blocks = msg.get("blocks")
    incomplete = not text or text in _SLACK_BLOCK_FALLBACKS or text.endswith(_BLOCKS_TEXT_TAIL)
    if not blocks or not incomplete:
        return text
    return _extract_blocks_text(blocks) or text.removesuffix(_BLOCKS_TEXT_TAIL)


def _line(
    msg: dict,
    own_bot_id: str,
    *,
    session_key: str,
    channel: str,
    thread_ts: str,
    text_cap: int = TEXT_CAP,
    surface: str = "slack_thread_replies",
) -> str:
    when = datetime.fromtimestamp(_ts(msg.get("ts")), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    text = redact(_body(msg)).strip() or "[no text]"
    if len(text) > text_cap:
        text = text[:text_cap] + "…[truncated]"
    line = f"[{when}] {redact(_author(msg, own_bot_id))}: {text}"
    # Screened as one string: the author name is as attacker-settable as the text.
    if contains_injection(line):
        audit_injection_dropped(
            surface=surface,
            session_key=session_key,
            channel_id=channel,
            thread_ts=thread_ts,
            sample=line,
        )
        return f"[{when}] {_WITHHELD}"
    return line


def _omitted_header(omitted: int) -> str:
    return (
        f"[{omitted} earlier repl{'y' if omitted == 1 else 'ies'} not shown — "
        "use the Slack MCP server's thread-reading tool (e.g. get_thread) to read them]"
    )


async def replies_since_last_turn(
    slack: "SlackClientOps",
    channel: str,
    thread_ts: str | None,
    msg_ts: str,
    *,
    session_key: str,
    first_turn: bool,
) -> ThreadReplies:
    """Thread replies this turn has not seen, formatted oldest first.

    *first_turn* means the session has no turn in this thread yet: every earlier
    reply counts, the app's own included. Otherwise only replies after the last
    turn count, and the app's own are left out (they are already in the
    agent's conversation). The thread's first message and the message this turn
    answers are never included. Best-effort: a failed read yields no text and
    ``read_ok=False``, so the caller keeps the old watermark and the next turn
    asks for the same range again.
    """
    if not thread_ts or thread_ts == msg_ts:
        return _NOT_READ
    since = None if first_turn else _last_turn.get((session_key, thread_ts))
    try:
        msgs = await slack.fetch_thread_replies(
            channel,
            thread_ts,
            limit=_FETCH_LIMIT,
            warn_on_pagination=False,
            oldest=since,
            latest=msg_ts,
        )
    except Exception:
        logger.debug("thread replies: fetch failed %s/%s", channel, thread_ts, exc_info=True)
        return _NOT_READ
    # A successful conversations.replies read always carries the thread's first
    # message, so an empty answer is the client's swallowed failure, not a thread.
    if not msgs:
        return _NOT_READ
    own = validated_self_bot_id()
    floor = max(_ts(thread_ts), _ts(since))
    ceiling = _ts(msg_ts)
    replies = [m for m in msgs or [] if isinstance(m, dict) and floor < _ts(m.get("ts")) < ceiling]
    replies.sort(key=lambda m: _ts(m.get("ts")))
    if not first_turn:
        if since is None:
            own_ts = [_ts(m.get("ts")) for m in replies if _is_own(m, own)]
            if own_ts:
                newest_own = max(own_ts)
                replies = [m for m in replies if _ts(m.get("ts")) > newest_own]
        replies = [m for m in replies if not _is_own(m, own)]
    if not replies:
        return ThreadReplies(text=None, read_ok=True)

    omitted = max(0, len(replies) - MAX_REPLIES)
    kept: list[str] = []
    # Room for the omission header, so header plus replies stay within the cap.
    used = len(_omitted_header(len(replies)).encode("utf-8")) + 1
    for msg in reversed(replies[-MAX_REPLIES:]):
        line = _line(msg, own, session_key=session_key, channel=channel, thread_ts=thread_ts)
        size = len(line.encode("utf-8")) + 1
        if used + size > BYTE_CAP:
            break
        kept.append(line)
        used += size
    omitted += min(len(replies), MAX_REPLIES) - len(kept)
    kept.reverse()
    if omitted:
        kept.insert(0, _omitted_header(omitted))
    return ThreadReplies(text="\n".join(kept), read_ok=True)


# ── A code channel's own history, for a session that starts in a channel it joined ──

#: Newest channel messages kept for a code channel's first turn.
CHANNEL_MAX_MESSAGES = 30
#: Longest text kept from one channel message: another agent's summary runs long.
CHANNEL_TEXT_CAP = 4000
#: Most UTF-8 bytes the formatted channel messages may take together.
CHANNEL_BYTE_CAP = 16000
#: One ``conversations.history`` page.
_CHANNEL_FETCH_LIMIT = 50
#: Message subtypes that are someone saying something (join, topic and rename
#: notices carry no conversation).
_SPOKEN_SUBTYPES = frozenset({"", "bot_message", "thread_broadcast", "file_share", "me_message"})


async def channel_messages_before(
    slack: "SlackClientOps", channel: str, before_ts: str, *, session_key: str
) -> str | None:
    """The code channel's newest messages before *before_ts*, formatted oldest first.

    For a session that starts in a code channel this app did not open (Slack's
    lifecycle guide: read the channel before answering the mention that adds the
    agent). Every author's message counts, other apps' included: another agent's
    investigation is the point. Each line is redacted and screened like a thread
    reply (one matching a prompt-injection pattern is withheld and audited),
    bounded by :data:`CHANNEL_MAX_MESSAGES`, :data:`CHANNEL_TEXT_CAP` and
    :data:`CHANNEL_BYTE_CAP`. Thread replies under those messages are not read.
    None when the read failed or found nothing.
    """
    if not channel or not before_ts:
        return None
    try:
        msgs = await slack.fetch_channel_messages(
            channel, latest=before_ts, limit=_CHANNEL_FETCH_LIMIT
        )
    except Exception:
        logger.debug("channel history: fetch failed %s", channel, exc_info=True)
        return None
    ceiling = _ts(before_ts)
    # Each message as Slack sent it: ``_line`` reads its text through ``_body``,
    # which recovers block content (a streamed table) that the ``text`` copy drops.
    spoken = [
        m
        for m in msgs or []
        if isinstance(m, dict)
        and str(m.get("subtype") or "") in _SPOKEN_SUBTYPES
        and _ts(m.get("ts")) < ceiling
    ]
    spoken.sort(key=lambda m: _ts(m.get("ts")))
    if not spoken:
        return None
    own = validated_self_bot_id()
    omitted = max(0, len(spoken) - CHANNEL_MAX_MESSAGES)
    kept: list[str] = []
    used = len(_omitted_channel_header(len(spoken)).encode("utf-8")) + 1
    for msg in reversed(spoken[-CHANNEL_MAX_MESSAGES:]):
        line = _line(
            msg,
            own,
            session_key=session_key,
            channel=channel,
            thread_ts="",
            text_cap=CHANNEL_TEXT_CAP,
            surface="slack_code_channel_history",
        )
        size = len(line.encode("utf-8")) + 1
        if used + size > CHANNEL_BYTE_CAP:
            break
        kept.append(line)
        used += size
    omitted += min(len(spoken), CHANNEL_MAX_MESSAGES) - len(kept)
    kept.reverse()
    if omitted:
        kept.insert(0, _omitted_channel_header(omitted))
    return "\n".join(kept)


def screened(text: str, *, channel: str) -> str:
    """One piece of channel-authored text (a code channel's title) as a line may
    show it: redacted, and withheld whole when it matches a prompt-injection pattern."""
    shown = redact(str(text or "")).strip()[:TEXT_CAP]
    if shown and contains_injection(shown):
        audit_injection_dropped(
            surface="slack_code_channel_history", channel_id=channel, sample=shown
        )
        return "[withheld: this text matched a prompt-injection pattern]"
    return shown


def _omitted_channel_header(omitted: int) -> str:
    return (
        f"[{omitted} earlier message{'' if omitted == 1 else 's'} not shown — "
        "use the Slack MCP server's channel-reading tool to read them]"
    )
