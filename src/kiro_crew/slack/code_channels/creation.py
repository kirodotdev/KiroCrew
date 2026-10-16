"""Opening a code channel from a mention, and recognising Slack's echo of it.

The path from ``@Kiro Crew fix the flaky login test`` in a thread to a running
code channel:

1. :func:`is_directed_human_prompt` -- only a person's own message qualifies:
   no ``bot_id`` / ``bot_profile``, subtype ``None`` or ``file_share``.
2. :func:`session_id_for` -- a deterministic id for the origin message. It is
   both the route record's key and Slack's ``session_id`` idempotency key, so
   a re-delivered event, or a restart between steps, never opens a second
   channel.
3. The classifier decides (or the stored decision is reused).
4. ``agents.conversations.create`` with the origin link: Slack copies the
   origin message in, inherits the origin's privacy (``is_private`` is left
   unset) and invites its author.
5. A kickoff message, a dashboard session, the binding, and the first turn.

Slack posts its own copy of the origin message into the new channel, as a
context block. :func:`is_origin_echo` recognises it -- by a strict match of the
block's link against the channel's recorded origin -- so it is never mistaken
for a new request.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from kiro_crew.slack.code_channels.store import Binding

#: Slack's block id for the origin copy it posts into a new code channel.
ORIGIN_CONTEXT_BLOCK_ID = "agent_channel_origin_context"

_PERSON_SUBTYPES = (None, "file_share")


def is_directed_human_prompt(event: dict[str, Any]) -> bool:
    """A person's own message (never a bot's, never an edit or a join)."""
    if not isinstance(event, dict):
        return False
    if event.get("bot_id") or event.get("bot_profile"):
        return False
    if event.get("subtype") not in _PERSON_SUBTYPES:
        return False
    return bool(event.get("user")) and bool(event.get("ts")) and bool(event.get("channel"))


def session_id_for(app_identity: str, team_id: str, channel_id: str, message_ts: str) -> str:
    """sha256 over the origin's identity: same message, same id, always."""
    joined = "\x1f".join((app_identity or "", team_id or "", channel_id or "", message_ts or ""))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def channel_name_for(title: str) -> str:
    """A Slack-legal channel name from a task title (lowercase, hyphens, <=80)."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (slug or "code-task")[:80].rstrip("-")


_ARCHIVE_LINK = re.compile(
    r"^https://[a-z0-9][a-z0-9-]*(?:\.enterprise)?\.slack\.com/archives/"
    r"(?P<channel>[A-Z0-9]+)/p(?P<sec>\d{10})(?P<frac>\d{6})(?:\?[^|>]*)?$"
)


def _block_links(block: dict[str, Any]) -> list[str]:
    links: list[str] = []
    for element in block.get("elements") or ():
        if not isinstance(element, dict):
            continue
        text = element.get("text")
        if isinstance(text, str):
            links.extend(m.group(1) for m in re.finditer(r"<(https://[^|>]+)(?:\|[^>]*)?>", text))
        url = element.get("url")
        if isinstance(url, str):
            links.append(url)
    return links


def is_origin_echo(event: dict[str, Any], binding: Binding | None) -> bool:
    """True for Slack's copy of *binding*'s origin message, and nothing else.

    Matches only a context block with Slack's block id whose archive link
    names EXACTLY the recorded origin channel and ts. A person cannot make an
    ordinary message look like this by typing; an unmatched lookalike (other
    origin, other host, malformed ts) is treated as an ordinary message.
    """
    if binding is None or binding.origin_link is None:
        return False
    origin_channel, origin_ts = binding.origin_link
    for block in event.get("blocks") or ():
        if not isinstance(block, dict) or block.get("type") != "context":
            continue
        if block.get("block_id") != ORIGIN_CONTEXT_BLOCK_ID:
            continue
        for link in _block_links(block):
            match = _ARCHIVE_LINK.match(link)
            if not match:
                continue
            ts = f"{match.group('sec')}.{match.group('frac')}"
            if match.group("channel") == origin_channel and ts == origin_ts:
                return True
    return False
