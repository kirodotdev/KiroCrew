"""Show a rejected MCP sign-in banner once per server per sign-in state.

kiro-cli retries a signed-out server's OAuth on every turn, and each retry
reaches :func:`~kiro_crew.dashboard.chat_turn.mcp_session._emit_mcp_oauth_request`
again: the ACP layer's own dedupe (``_oauth_emitted_servers``) is cleared by the
server's ``mcp_server_init_failure`` and by a respawn, so it never holds across
turns. A URL the banner gate rejects then posts the same "authentication
failed" notice on every user turn and every monitor or auto-nudge cycle.

This module remembers, per chat slot, the last rejected banner shown for each
server, and :func:`rejected_banner_is_repeat` answers whether a new one would
say nothing new. A banner is shown again when anything changed:

* its text or error differs (a different rejection);
* the slot's ACP session differs (a new session, including "start a new chat");
* the server signed in, or an Authorize banner was shown for it, since the last
  one (:func:`forget_mcp_banner`), so a server that goes signed-in, then
  signed-out, is announced again. A sign-in counts whether a mid-turn event
  reported it or the session's MCP report lists the server as connected;
* the user retried its sign-in from the MCP Servers page or a Connections card
  (:func:`note_mcp_sign_in_retry`), which bumps a process-wide round.

The record is in memory only and nothing is written to disk, so a gateway
restart shows the banner once more. Only the chat banner is deduplicated: the
MCP Servers page row reads the server's own status and is untouched.

Authorize banners are never suppressed here. Each one carries a fresh link whose
loopback listener replaces the previous one, so dropping it would leave the user
holding a dead link.

Both stores are bounded. A slot holds at most ``SLOT_CAP`` servers and the
round table at most ``ROUND_CAP`` names; the oldest entry is evicted when full.
Losing an entry only means the next banner for that server is shown, which is
the behaviour before this module. Only digests of the session id and the
banner text are kept, so every entry has a fixed size.

Instance fields of ``_ChatSlot`` this module reads and writes:

Writes: ``_mcp_banner_shown``.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from kiro_crew.mcp_utils import mcp_server_alias

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import _ChatSlot

#: Most servers one slot remembers a shown banner for.
SLOT_CAP = 64
#: Most server names the process-wide sign-in round table holds.
ROUND_CAP = 256
#: Longest server name tracked; a longer one is never deduplicated.
NAME_CAP = 128

#: Server name -> the generation of the user's latest sign-in for it outside
#: chat. Generations come from one process-wide counter, so they only grow.
_sign_in_rounds: dict[str, int] = {}
#: The last generation handed out.
_generation = 0
#: The highest generation evicted from ``_sign_in_rounds``; the round of every
#: name the table does not hold. It only grows, so an evicted name never reads
#: as a round older than its last retry.
_evicted_floor = 0


def note_mcp_sign_in_retry(server_name: str) -> None:
    """Record that the user started *server_name*'s sign-in outside chat.

    Called when the MCP Servers page or a Connections card starts a mint. The
    next rejected banner for that server in any chat is then shown even if its
    text is unchanged, so the retry gets visible feedback. Both the configured
    name and its kiro-safe alias are bumped, because kiro-cli names the server
    by its alias.

    Each retry takes a new generation from one counter. When the table is full
    the oldest name is evicted and its generation raises the floor that every
    untracked name reads, so an evicted name can only read as the same or a
    newer round, never back to an older one: a lost entry shows a banner again
    at worst, and never hides one a retry asked for.
    """
    global _generation, _evicted_floor
    if not isinstance(server_name, str) or not server_name or len(server_name) > NAME_CAP:
        return
    for name in dict.fromkeys((server_name, mcp_server_alias(server_name))):
        _sign_in_rounds.pop(name, None)
        while len(_sign_in_rounds) >= ROUND_CAP:
            evicted = _sign_in_rounds.pop(next(iter(_sign_in_rounds)))
            _evicted_floor = max(_evicted_floor, evicted)
        _generation += 1
        _sign_in_rounds[name] = _generation


def _round(server_name: str) -> int:
    return _sign_in_rounds.get(server_name, _evicted_floor)


def _digest(*parts: str) -> str:
    joined = "\0".join(parts)
    return hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()[:16]


def rejected_banner_is_repeat(
    slot: "_ChatSlot",
    server_name: str,
    safe_name: str,
    ambiguous: bool,
    session_id: str,
    content: str,
    error: str,
) -> bool:
    """Whether this rejected banner repeats the one last shown, and record it.

    Answers True only when the slot already showed a banner with the same text
    and error for this server, in the same ACP session, in the same sign-in
    round, with no sign-in or Authorize banner since. Otherwise the banner is
    recorded as shown and False is returned.

    Never deduplicates (always False, nothing recorded) when the session is
    unknown, because a new session could not be told from the old one, or when
    the stored name is ambiguous: redaction can collapse two servers onto one
    name, and one server's banner must never hide another's.
    """
    if not session_id or ambiguous or not safe_name or len(safe_name) > NAME_CAP:
        return False
    shown = slot._mcp_banner_shown
    # Digests, not the strings: the session id is backend-authored and has no
    # length bound, and a fixed-size digest keeps every entry the same size.
    record = (_digest(session_id), _round(server_name), _digest(content, error))
    if shown.get(safe_name) == record:
        return True
    shown.pop(safe_name, None)
    while len(shown) >= SLOT_CAP:
        shown.pop(next(iter(shown)))
    shown[safe_name] = record
    return False


def forget_mcp_banner(slot: "_ChatSlot", safe_name: str) -> None:
    """Drop the slot's record for *safe_name*: its sign-in state changed.

    Called when the server signs in (a mid-turn event, or the session's MCP
    report listing it as connected) and when an Authorize banner is shown, so a
    later rejection is announced again.
    """
    slot._mcp_banner_shown.pop(safe_name, None)
