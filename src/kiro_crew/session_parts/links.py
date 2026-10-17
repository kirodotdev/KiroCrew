"""A session's channel bindings: Slack threads, outbound mirrors and the origin it came from.

Each function here whose first parameter is ``self`` is a ``SessionManager``
member: the class binds it by name (``kiro_crew.session``), so it reads and writes the
manager's own state on the instance, and a patch of the member on the class or
on an instance reaches every caller.

Instance fields written: none.

Instance fields read: ``_session_map``.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

from kiro_crew.messaging.link import UNBIND_REASON_UNSPECIFIED, canonical_key
from kiro_crew.session_map import MIRROR_OPT_OUT_FLAG
from kiro_crew.session_parts.policy import _MAX_ORIGIN_LINKS, _opt_out_key

if TYPE_CHECKING:
    from kiro_crew.messaging.link import ChannelLink
    from kiro_crew.session import SessionManager


# ── Slack thread linking (persisted via SessionMap) ──


def set_slack_link(self: SessionManager, key: str, thread_ts: str, channel_id: str | None) -> None:
    """Link a session to a Slack thread. Persists to session map."""
    self._session_map.set_slack_link(key, thread_ts, channel_id)


def get_slack_link(self: SessionManager, key: str) -> tuple[str | None, str | None]:
    """Return (thread_ts, channel_id) for a session."""
    return self._session_map.get_slack_link(key)


def clear_slack_link(self: SessionManager, key: str) -> bool:
    """Remove a session's Slack link (stop mirroring). Returns True if one was present."""
    return self._session_map.clear_slack_link(key)


def set_slack_paused(self: SessionManager, key: str, paused: bool) -> bool:
    """Set whether turns reach the linked Slack thread; return the prior state.

    Disconnecting retains the thread binding and its reverse index, so a reply
    there still resolves to this session.
    """
    return self._session_map.set_slack_paused(key, paused)


def is_slack_paused(self: SessionManager, key: str) -> bool:
    """True iff this session's Slack thread is disconnected but still bound."""
    return self._session_map.is_slack_paused(key)


def get_session_for_thread(self: SessionManager, thread_ts: str) -> str | None:
    """Return the session key linked to a Slack thread, or None."""
    return self._session_map.get_session_for_thread(thread_ts)


def channel_key_for_stem(self: SessionManager, stem: str) -> str:
    """The real channel session key behind a transcript filename *stem*.

    Lets the dashboard bind a surfaced channel tab to the session the
    channel itself runs, instead of deriving a key from the filename (the
    ``:``-to-``_`` fold is not reversible). ``""`` means unknown.
    """
    return self._session_map.channel_key_for_stem(stem)


# ── Channel-neutral outbound mirror (generalizes Slack linking) ──


def set_mirror_link(
    self: SessionManager,
    key: str,
    link: ChannelLink | None,
    *,
    accepts_inbound: bool = False,
    reason: str = UNBIND_REASON_UNSPECIFIED,
) -> None:
    """Bind (or clear) a session's channel-neutral mirror target.

    ``accepts_inbound`` upgrades a non-Slack outbound mirror into a
    persisted session-resume binding. Slack owns its dedicated reverse
    index; other channels use :meth:`find_mirror_sessions`. ``reason`` is
    recorded when this call ends an existing inbound binding.
    """
    self._session_map.set_mirror_link(
        key,
        link,
        accepts_inbound=accepts_inbound,
        reason=reason,
    )


def get_mirror_link(self: SessionManager, key: str) -> ChannelLink | None:
    """Return a session's outbound mirror target as a channel-neutral link,
    or None. Legacy Slack sessions surface as a Slack ``ChannelLink``."""
    return self._session_map.get_mirror_link(key)


def mirror_accepts_inbound(self: SessionManager, key: str) -> bool:
    """True iff this session's mirror is a session-resume (two-way) binding."""
    return self._session_map.mirror_accepts_inbound(key)


def has_mirror_row(self: SessionManager, key: str) -> bool:
    """Whether an explicit ``mirror`` row is stored under exactly *key* (no Slack
    synthesis, no legacy-row fallback); see ``SessionMap.has_mirror_row``."""
    return self._session_map.has_mirror_row(key)


def mirror_link_nonce(self: SessionManager, key: str) -> str:
    """The per-binding nonce of the mirror ``get_mirror_link`` returns (``""`` for none)."""
    return self._session_map.mirror_link_nonce(key)


def slack_link_nonce(self: SessionManager, key: str) -> str:
    """The per-binding nonce of the Slack thread ``get_slack_link`` returns (``""`` for none)."""
    return self._session_map.slack_link_nonce(key)


def set_mirror_opt_out(self: SessionManager, key: str, opted_out: bool) -> None:
    """Record (or withdraw) a refusal of AUTOMATIC origin mirroring.

    A channel that mirrors its own conversation by default needs an
    in-channel "off" that the NEXT inbound message does not silently undo,
    and clearing the binding cannot express that: an entry with no ``mirror``
    is indistinguishable from one that was never linked, so the automatic
    bind would fire again one message later. This flag is that difference.

    Persisted, because the bind it suppresses is itself re-asserted on every
    turn and survives a restart — an in-memory refusal would come back on
    its own. Only the automatic bind consults it; an explicit ``/link`` or
    dashboard link is a direct instruction and :meth:`set_mirror_link` never
    reads it.
    """
    with self._session_map.batched_save():
        self._session_map.set_flag(_opt_out_key(key), MIRROR_OPT_OUT_FLAG, opted_out)
        # Retire a refusal an earlier build stored under the generation key,
        # so it cannot outlive a withdrawal made through the bucket.
        legacy = canonical_key(key)
        if legacy != _opt_out_key(key):
            self._session_map.set_flag(legacy, MIRROR_OPT_OUT_FLAG, False)


def mirror_opt_out(self: SessionManager, key: str) -> bool:
    """True iff this conversation declined automatic origin mirroring.

    Reads the bucket, then falls back to the generation key an earlier build
    wrote. Without the fallback, upgrading silently restores mirroring for
    every conversation that had already turned it off — the exact failure the
    flag exists to prevent, delivered by the fix for it.

    A legacy hit is PROMOTED to the bucket, which is why this read writes.
    Reading it without promoting would honour the refusal for the generation
    it was stored under and lose it at the next rotation, so an upgrading user
    would keep the expiring behaviour this change exists to remove. Retiring
    the old row in the same write also stops it holding an entry that pruning
    is forbidden to collect, one per generation.
    """
    bucket = _opt_out_key(key)
    if self._session_map.get_flag(bucket, MIRROR_OPT_OUT_FLAG):
        return True
    legacy = canonical_key(key)
    if legacy == bucket:
        return False
    if not self._session_map.get_flag(legacy, MIRROR_OPT_OUT_FLAG):
        return False
    with self._session_map.batched_save():
        self._session_map.set_flag(bucket, MIRROR_OPT_OUT_FLAG, True)
        self._session_map.set_flag(legacy, MIRROR_OPT_OUT_FLAG, False)
    return True


def batched_save(self: SessionManager) -> AbstractContextManager[None]:
    """Collapse the session-map writes of a related mutation sequence into one.

    Each mutation rewrites the whole map, so a caller making several of them
    (a link, an unlink) pays that cost once per operation unless it says
    otherwise. Must not be held across an ``await`` — see
    :meth:`SessionMap.batched_save`.
    """
    return self._session_map.batched_save()


def set_origin_link(self: SessionManager, key: str, link: ChannelLink) -> None:
    """Record the channel conversation this session was started from.

    Called by a transport's inbound path with the conversation's real send
    target, so unattended output about the session (the auto-compact notice)
    can reach the user.

    Held in memory, NOT persisted, and that is deliberate: the target is only
    ever needed to talk about a LIVE session, and sessions themselves are
    in-memory — a gateway restart takes the session with it, so there is
    nothing left to compact or explain. Keeping it here also keeps the
    recording free of disk I/O and of cross-thread mutation, both of which a
    session-map field would have put on the transport's turn path.
    """
    key = self._fold_key(key)
    self._origin_links[key] = link
    # Bound the map for the pathological case where sessions are dropped
    # without reset()/remove() (which evict their own entries). FIFO, since
    # dict preserves insertion order and the oldest key is the least likely
    # to still be live.
    while len(self._origin_links) > _MAX_ORIGIN_LINKS:
        self._origin_links.pop(next(iter(self._origin_links)), None)


def get_origin_link(self: SessionManager, key: str) -> ChannelLink | None:
    """Return the channel conversation this session was started from, or None."""
    return self._origin_links.get(self._fold_key(key))


def find_mirror_sessions(
    self: SessionManager,
    link: ChannelLink,
    *,
    inbound_only: bool = False,
) -> list[str]:
    """Return sessions bound to an exact non-Slack mirror location."""
    return self._session_map.find_mirror_sessions(
        link,
        inbound_only=inbound_only,
    )


def mirror_claim_blockers(
    self: SessionManager,
    key: str,
    link: ChannelLink,
    *,
    accepts_inbound: bool = False,
) -> list[str]:
    """Sessions that must stop *key* from binding *link*, or [] if it is free."""
    return self._session_map.mirror_claim_blockers(key, link, accepts_inbound=accepts_inbound)


def clear_mirror_link(
    self: SessionManager, key: str, *, reason: str = UNBIND_REASON_UNSPECIFIED
) -> bool:
    """Remove a session's outbound mirror binding. Returns True iff present."""
    return self._session_map.clear_mirror_link(key, reason=reason)


def clear_mirror_link_if(
    self: SessionManager,
    key: str,
    channel_type: str,
    token: str,
    *,
    reason: str = UNBIND_REASON_UNSPECIFIED,
) -> bool:
    """Clear the mirror iff it is the binding ``(channel_type, token)`` names.

    The compare and the clear are one step under the map's lock; False is a
    mismatch (or no binding) and nothing was touched. The only way an unlink
    that names a row clears it -- see ``SessionMap.clear_mirror_link_if``.
    """
    return self._session_map.clear_mirror_link_if(key, channel_type, token, reason=reason)


def replace_mirror_link_if(
    self: SessionManager, key: str, expected: ChannelLink, link: ChannelLink
) -> bool:
    """Store *link* iff the row is still exactly *expected*; see ``SessionMap.replace_mirror_link_if``."""
    return self._session_map.replace_mirror_link_if(key, expected, link)


def clear_slack_link_if(self: SessionManager, key: str, channel_type: str, token: str) -> bool:
    """Clear the Slack thread iff it is the binding ``(channel_type, token)`` names.

    The Slack twin of ``clear_mirror_link_if``; both key spellings of a
    dashboard session go in the same step.
    """
    return self._session_map.clear_slack_link_if(key, channel_type, token)


def clear_mirror_links_at(
    self: SessionManager, link: ChannelLink, *, reason: str = UNBIND_REASON_UNSPECIFIED
) -> list[str]:
    """Clear every session mirroring to an exact location; return cleared keys."""
    return self._session_map.clear_mirror_links_at(link, reason=reason)


async def aflush(self: SessionManager) -> None:
    await self._session_map.aflush()


def set_mirror_paused(
    self: SessionManager, key: str, paused: bool, *, origin: bool = False
) -> bool:
    """Set whether turns reach one non-Slack delivery; return the prior state.

    ``origin`` selects the born-in conversation rather than the explicit
    mirror binding — a session can hold both, and they mute independently.
    """
    return self._session_map.set_mirror_paused(key, paused, origin=origin)


def is_mirror_paused(self: SessionManager, key: str, *, origin: bool = False) -> bool:
    """True iff the named non-Slack delivery is disconnected (see the setter)."""
    return self._session_map.is_mirror_paused(key, origin=origin)


# Backward-compat aliases used by callers not yet migrated
async def set_channel(self: SessionManager, key: str, channel_id: str) -> None:
    """Set channel for a session. Prefer set_slack_link for new code."""
    thread_ts, _ = self.get_slack_link(key)
    self.set_slack_link(key, thread_ts or "", channel_id)


def get_channel(self: SessionManager, key: str) -> str | None:
    """Return the Slack channel ID for a session key, or None."""
    _, channel_id = self.get_slack_link(key)
    return channel_id


# ── Additional session map helpers ──


def find_key_by_sid(self: SessionManager, sid: str, *, exclude: str = "") -> str | None:
    return self._session_map.find_key_by_sid(sid, exclude=exclude)


def reserve_generation(self: SessionManager, session_key: str) -> None:
    """Persist a generation floor before its first provider turn."""
    self._session_map.reserve_generation(session_key)


def max_generation(self: SessionManager, bucket: str) -> int:
    """Highest persisted DM generation for a session bucket (see SessionMap)."""
    return self._session_map.max_generation(bucket)


async def set_thread(self: SessionManager, key: str, thread_ts: str) -> None:
    """Set thread for a session. Prefer set_slack_link for new code."""
    _, channel_id = self.get_slack_link(key)
    self.set_slack_link(key, thread_ts, channel_id)


def get_thread(self: SessionManager, key: str) -> str | None:
    """Return the Slack thread_ts for a session key, or None."""
    thread_ts, _ = self.get_slack_link(key)
    return thread_ts
