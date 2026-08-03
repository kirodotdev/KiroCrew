"""Surface channel-originated sessions in the dashboard's active chat list.

A conversation started on Slack (or Discord/Telegram/Teams/…) persists under a
channel-namespaced session key such as ``slack:<thread_ts>``. The dashboard's
slot restore paths only ever built slots for ``dashboard:``-prefixed keys, so
those conversations existed on disk but had no chat slot — they only appeared
in the sidebar's collapsed **History** pane, where the user had to search for
one and resume it by hand.

This module reconciles recent channel sessions INTO slots so they show up in
the active chat list, seeded with the conversation so far and ready to continue.

Design notes:

* **One history key, no split.** The slot is an ordinary dashboard slot whose
  key is derived from the channel session key
  (``slack:1785370133.085469`` -> ``slack_1785370133.085469``), so everything
  about it — turn persistence, replay, the ``closed`` flag, and both restore
  paths — uses the single ``dashboard:<slot.key>`` history key.

  It deliberately does NOT set ``linked_session_key`` to the channel key.
  ``_save_slot_to_history`` is hard-wired to ``_history_key_for(slot.key)``,
  so binding the RUN path to the channel key while the SAVE path kept writing
  ``dashboard:<slot.key>`` would split one conversation across two transcripts:
  a dashboard reply would be invisible to the slot's own replay, and a tab the
  user closed would have its ``closed`` flag written to a key the reconciler
  never reads — so it would reopen on the next pass. Continuity with the live
  channel session is not worth that; picking the conversation up with its full
  history (which this does) is the point.
* **Mirror until forked.** A surfaced slot the user has NOT written to from the
  dashboard is a pure mirror of the channel transcript, and the no-split
  rationale above does not yet apply — so the reconciler keeps appending new
  channel turns into it, keeping the tab current instead of freezing it at
  surface time. The first dashboard-authored turn forks the conversation and
  disarms the mirror permanently; from then on the two transcripts diverge
  exactly as the previous note describes. Fork detection is fail-safe: ANY
  window change the mirror did not make itself disarms it.
* **Recency-bounded.** Only sessions modified within the dashboard's configured
  ``restore_window_minutes`` become slots, so a long DM history does not turn
  into hundreds of tabs. Pinned and foldered sessions are exempt from the
  window, matching :func:`~kiro_crew.dashboard.chat_persistence.restore_recent_sessions`.
* **Closed is sticky — until the channel moves on.** A session the user closed
  on the dashboard (``meta.closed``) is not re-surfaced by the next reconcile
  pass — otherwise closing the tab would be undone 30 seconds later. But a
  close is a statement about the conversation *as it stood*: channel-side
  activity strictly newer than the close (the person kept talking on Discord
  after the tab was dismissed) re-surfaces it, and the stale ``closed`` flags
  are cleared so every restore path agrees the tab is open again. When the
  close instant is unknown (legacy flag with no ``closed_at`` stamp and no
  readable file mtime), the close stands — fail toward the user's explicit
  dismissal.
* **Ephemeral stays ephemeral.** ``incognito``/``temporary`` channel threads are
  skipped: the user asked for a conversation that leaves no trace, and a
  durable sidebar tab contradicts that.
* **Idempotent.** Every pass is a no-op for sessions that already own a slot,
  so it is safe to run on a timer.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew.dashboard.chat_utils import _history_key_for
from kiro_crew.dashboard.state import _normalize_slot_key
from kiro_crew.messaging.link import channel_namespace_of, is_channel_session_key
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: How often the background reconciler runs. Not user-configurable: it only
#: bounds how stale the sidebar can be for a channel conversation started while
#: the dashboard is already open, and each pass is a cheap metadata scan.
RECONCILE_INTERVAL_SECS = 30

#: Memory modes whose sessions must never be surfaced as a durable slot.
_EPHEMERAL_MEMORY_MODES = frozenset({"incognito", "temporary"})

#: Human-facing label per channel namespace, used only when a session has no
#: title of its own yet (first turn still in flight).
_CHANNEL_LABELS: dict[str, str] = {
    "slack": "Slack",
    "discord": "Discord",
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "webex": "Webex",
    "wecom": "WeCom",
    "teams": "Teams",
    "weixin": "Weixin",
    "unified": "Direct message",
}

#: Messages hydrated into a newly surfaced slot. Matches the cron inject path.
_HYDRATE_LIMIT = 50


class _Transcript(NamedTuple):
    """A pending slot's seed transcript plus its on-disk line accounting.

    ``disk_older`` + ``disk_window`` always sum to the length of the slot's own
    history file; the merged ``messages`` may be longer because it also carries
    channel-side turns that file never held.
    """

    messages: list[dict[str, Any]]
    disk_older: int
    disk_window: int
    #: True when the slot's own on-disk lines are all copies of channel turns —
    #: the precondition for arming the mirror (see :func:`is_pure_mirror`).
    pure_mirror: bool = True


def channel_label(session_key: str) -> str:
    """Return the display label for *session_key*'s channel (``"Slack"``, …)."""
    ns = channel_namespace_of(session_key)
    return _CHANNEL_LABELS.get(ns, ns.capitalize() if ns else "Channel")


def channel_slot_name(session_key: str) -> str:
    """Return the dashboard slot key for a channel session key.

    Deterministic and idempotent — the same channel session always maps to the
    same slot, which is what makes repeat reconcile passes no-ops and lets the
    key itself carry the conversation's provenance.
    """
    return _normalize_slot_key(session_key)


def slot_history_key(session_key: str) -> str:
    """Return the history key the surfaced slot persists under.

    This is the ONLY key the slot reads or writes: turn persistence, replay, the
    ``closed`` flag, and both restore paths all agree on it.
    """
    return _history_key_for(channel_slot_name(session_key))


def _redact(text: str) -> str:
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _mirror_identity(msg: dict[str, Any]) -> tuple[str, str, str]:
    """Cross-file match identity for one transcript message.

    Slot copies of channel turns are stored REDACTED (``surface_channel_session``
    redacts on seed), while the channel file holds the raw text — comparing raw
    identities would mis-classify every redacted turn as dashboard-authored.
    Redacting both sides makes the comparison stable regardless of which side a
    message is read from (the redactors are idempotent on their own output, and
    redacting an already-redacted slot line is a no-op either way).
    """
    return (
        str(msg.get("role", "")),
        _redact(str(msg.get("content", ""))),
        str(msg.get("ts", "")),
    )


def is_pure_mirror(
    slot_messages: list[dict[str, Any]],
    channel_messages: list[dict[str, Any]],
) -> bool:
    """True when *slot_messages* holds nothing the channel transcript lacks.

    A pure-mirror slot is one the user has never written to from the dashboard:
    every turn it carries is a copy of a channel turn. Only such a slot may be
    kept in sync by :func:`mirror_new_messages` — the moment the slot holds a
    dashboard-authored turn the conversation has forked, and appending further
    channel turns would interleave two diverged transcripts.

    Pure and side-effect free so the fork rule is directly testable.
    """
    channel = {_mirror_identity(m) for m in channel_messages}
    return all(_mirror_identity(m) in channel for m in slot_messages)


def mirror_new_messages(
    slot_messages: list[dict[str, Any]],
    channel_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Channel turns a pure-mirror slot is missing, in append order.

    Only turns at or after the slot's newest ordered timestamp are candidates:
    the seed window is a capped TAIL of the transcript (:func:`hydrate_window`),
    so turns older than the window were deliberately not seeded and must not be
    appended after newer ones — that would scramble the visible order. Turns
    whose timestamp cannot be parsed to an instant are skipped for the same
    reason: they cannot be placed. Empty-content lines are dropped to match the
    seed, and duplicates are dropped on the redaction-stable identity.

    Pure and side-effect free so the ordering rules are directly testable.
    """
    have = {_mirror_identity(m) for m in slot_messages}
    threshold = max(
        (k for m in slot_messages if (k := _ts_sort_key(m))[0] == 0),
        default=None,
    )
    out: list[dict[str, Any]] = []
    for m in channel_messages:
        if not m.get("content"):
            continue
        key = _ts_sort_key(m)
        if key[0] != 0:
            continue
        if threshold is not None and key < threshold:
            continue
        if _mirror_identity(m) in have:
            continue
        out.append(m)
    return sorted(out, key=_ts_sort_key)


def _close_time(meta: dict[str, Any], file_mtime: float | None) -> float | None:
    """Best-known epoch instant *meta*'s ``closed`` flag was written.

    Prefers the explicit ``closed_at`` stamp (written alongside ``closed`` by
    ``_save_slot_to_history``); falls back to the session file's mtime, which
    the closing save set. ``None`` means the instant is unknowable — the
    caller must treat the close as standing.
    """
    raw = meta.get("closed_at")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return file_mtime


def _close_stands(
    session: dict[str, Any],
    meta: dict[str, Any],
    slot_meta: dict[str, Any],
    mtimes: dict[str, float],
) -> bool:
    """True when the user's dismissal of this conversation is still in force.

    A close is not permanent: channel-side activity strictly newer than the
    close means the conversation came back to life after the user dismissed
    it, so it re-qualifies for surfacing. The comparison is against the
    session listing's ``modified`` (the channel file's last write). Every
    closed side must be outdated by that activity — an unknown close instant
    on either side keeps the close standing, failing toward the user's
    explicit action.
    """
    key = session.get("key", "")
    closes: list[float | None] = []
    if meta.get("closed"):
        closes.append(_close_time(meta, mtimes.get(key)))
    if slot_meta.get("closed"):
        slot_key = slot_history_key(key)
        closes.append(_close_time(slot_meta, mtimes.get(slot_key)))
    if not closes:
        return False
    modified = float(session.get("modified", 0) or 0)
    return any(ct is None or modified <= ct for ct in closes)


def eligible_channel_sessions(
    sessions: list[dict[str, Any]],
    *,
    metadata: dict[str, dict[str, Any]],
    cutoff: float | None,
    mtimes: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Filter a ``list_sessions()`` result down to channel sessions to surface.

    *metadata* maps session key -> ``get_metadata()`` result, and must contain an
    entry for BOTH the channel key and its ``dashboard:``-prefixed slot key (see
    :func:`slot_history_key`) — closing a tab writes ``closed`` to the slot key,
    so reading only the channel key would let a closed tab reopen on the next
    pass. *mtimes* maps the same keys to their session-file mtimes, the fallback
    close instant for a ``closed`` flag with no ``closed_at`` stamp (see
    :func:`_close_stands`); with it absent, every close stands. *cutoff* is a
    unix timestamp; sessions older than it are dropped unless pinned or
    foldered. ``None`` disables the recency filter (mirrors
    ``restore_window_minutes=0``).

    Pure and side-effect free so the eligibility rules are directly testable.
    """
    out: list[dict[str, Any]] = []
    for s in sessions:
        key = s.get("key", "")
        if not key or not is_channel_session_key(key):
            continue
        meta = metadata.get(key) or {}
        slot_meta = metadata.get(slot_history_key(key)) or {}
        # A close only stands until the channel outruns it: activity newer
        # than the close re-qualifies the session (and the reconciler then
        # clears the stale flags — see reconcile_channel_slots).
        if _close_stands(s, meta, slot_meta, mtimes or {}):
            continue
        modes = (
            str(meta.get("memory_mode", "")).lower(),
            str(slot_meta.get("memory_mode", "")).lower(),
            str(s.get("memory_mode", "")).lower(),
        )
        if any(m in _EPHEMERAL_MEMORY_MODES for m in modes):
            continue
        exempt = (
            bool(meta.get("pinned"))
            or bool(meta.get("folder_id"))
            or bool(slot_meta.get("pinned"))
            or bool(slot_meta.get("folder_id"))
        )
        if not exempt and cutoff is not None and float(s.get("modified", 0) or 0) < cutoff:
            continue
        out.append(s)
    return out


def _identity(msg: dict[str, Any]) -> tuple[str, str, str]:
    """Match/dedupe identity for one transcript message.

    Used both to drop the channel turns already copied into the slot and to
    locate a slot's on-disk lines inside the hydrate window.
    """
    return (
        str(msg.get("role", "")),
        str(msg.get("content", "")),
        str(msg.get("ts", "")),
    )


def _ts_sort_key(msg: dict[str, Any]) -> tuple[int, float, str]:
    """Chronological sort key for one transcript message.

    The two sides of a merge are written by different code paths and nothing
    makes them agree on timezone: the dashboard writes an ISO-8601 UTC string,
    while a channel turn can land as a naive local-time one. Comparing those
    lexicographically interleaves them wrongly by the host's UTC offset on any
    machine that is not on UTC, so parse to an absolute instant first.

    Buckets keep the fallbacks out of the ordered region: ``0`` parseable
    (ordered by epoch seconds), ``1`` present but unparseable (lexicographic),
    ``2`` absent (stable-sorted last, preserving relative order).
    """
    raw = str(msg.get("ts", "") or "")
    if not raw:
        return (2, 0.0, "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return (1, 0.0, raw)
    if parsed.tzinfo is None:
        # Naive means host-local — that is what a writer using datetime.now()
        # emits. Reading it as UTC would shift the turn by the host's offset.
        parsed = parsed.astimezone()
    return (0, parsed.timestamp(), "")


def merge_transcripts(
    slot_messages: list[dict[str, Any]],
    channel_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge a slot's own transcript with its channel transcript, by timestamp.

    Neither side is a superset of the other. The slot transcript holds any
    dashboard replies; the channel transcript holds anything the user said on
    the channel AFTER the conversation was first surfaced. Re-surfacing (e.g.
    after a restart with ``restore_sessions`` off) must seed the tab with both.

    Ordering is by the ``ts`` field, normalized to an absolute instant by
    :func:`_ts_sort_key` so a naive local-time channel turn cannot interleave
    wrongly with a UTC dashboard turn. Duplicates — the channel turns already
    copied into the slot — are dropped on :func:`_identity`.
    """
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict[str, Any]] = []
    for m in list(slot_messages) + list(channel_messages):
        ident = _identity(m)
        if ident in seen:
            continue
        seen.add(ident)
        merged.append(m)
    return sorted(merged, key=_ts_sort_key)


def hydrate_window(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tail of *messages* a newly surfaced slot is actually seeded with.

    One function so the seeding loop and the frozen-prefix arithmetic that has
    to agree with it cannot drift: empty-content lines are dropped first (they
    are never appended), then the tail is capped at :data:`_HYDRATE_LIMIT`.
    """
    return [m for m in messages if m.get("content")][-_HYDRATE_LIMIT:]


def frozen_prefix_len(
    slot_messages: list[dict[str, Any]],
    window: list[dict[str, Any]],
) -> int:
    """Count the slot's on-disk lines that *window* does not re-serialize.

    ``_save_slot_to_history`` models a session file as **frozen prefix + live
    window**: it copies the first ``slot._disk_older_count`` on-disk lines
    verbatim, then writes ``serialize(window)`` after them. Seeding a slot with
    only the tail of a long transcript while leaving that count at ``0`` tells
    the next save the file has no prefix, so the older lines are re-emitted
    after newer ones — the transcript loses chronological order.

    The boundary is positional: every slot line before the first one the window
    carries. Because the window is the chronological tail of the merge, every
    later slot line is in it too, so nothing between the boundary and the end
    of the file is stranded.
    """
    if not slot_messages:
        return 0
    carried = {_identity(m) for m in window}
    for i, msg in enumerate(slot_messages):
        if _identity(msg) in carried:
            return i
    return len(slot_messages)


def surface_channel_session(
    state: "DashboardState",
    session_info: dict[str, Any],
    meta: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    disk_older: int = 0,
    disk_window: int = 0,
    mirror: bool = False,
) -> "_ChatSlot | None":
    """Create the dashboard slot for one channel session.

    Returns the slot when this call newly surfaced it, else ``None`` (already
    present — the common steady-state case).

    *messages* is the merged transcript to seed the slot with, read by the caller
    so the disk IO stays off the event loop. *disk_older* and *disk_window* are
    that caller's split of the slot's OWN on-disk lines into the frozen prefix
    the window does not carry and the region it re-serializes — see
    :func:`frozen_prefix_len` for why a save needs both.

    *mirror* arms mirror-until-forked on the new slot: the reconciler keeps
    appending new channel turns into it until the first dashboard-authored turn
    forks the conversation. Pass it only when the slot's own on-disk transcript
    is a pure mirror (:func:`is_pure_mirror`) — arming it on a forked slot would
    interleave two diverged transcripts.
    """
    session_key = session_info.get("key", "")
    if not session_key or not is_channel_session_key(session_key):
        return None

    # The slot key is derived from the channel session key, deterministically and
    # idempotently, so it IS the record of where the conversation started — the
    # sidebar reads provenance straight off it, and every restore path preserves
    # it for free because the key is the slot's identity.
    slot_name = channel_slot_name(session_key)
    if slot_name in state._slots:
        return None
    try:
        slot = state.get_or_create_slot(name=slot_name, agent=meta.get("agent", "") or "")
    except ValueError:
        # A slot with this name exists under a conflicting memory_mode. Leave it
        # alone rather than fighting over the key.
        logger.debug("channel slot %s exists with a conflicting memory_mode", slot_name)
        return None

    raw_title = session_info.get("title") or meta.get("title") or ""
    slot.title = _redact(raw_title) if raw_title else channel_label(session_key)
    slot._titled = bool(raw_title)
    if meta.get("created_at"):
        slot.created_at = meta["created_at"]
    if meta.get("model"):
        slot.model = meta["model"]
    if meta.get("workspace"):
        slot.workspace = meta["workspace"]
    if meta.get("project"):
        slot.project = meta["project"]
    if meta.get("folder_id"):
        slot.folder_id = meta["folder_id"]
    if meta.get("pinned"):
        slot.pinned = True

    for msg in hydrate_window(messages):
        role = msg.get("role", "assistant")
        content = _redact(msg.get("content", ""))
        slot.append(
            role,
            content,
            f"msg msg-{'a' if role != 'user' else 'u'}",
            ts=msg.get("ts", ""),
            broadcast=False,
        )
    slot.drain()
    slot._resumed_count = len(slot.messages)
    # The session file is frozen prefix + live window. Only the slot's OWN
    # on-disk lines count toward either: the merged window also carries
    # channel-side turns that were never in this file, and crediting those as
    # persisted would let a trim fold unwritten turns into the frozen prefix.
    slot._disk_older_count = disk_older
    slot._disk_window_len = disk_window
    # Not dirty: a surfaced conversation the user never touches costs no write.
    # The first real dashboard turn is what persists it under the slot key.
    slot._dirty = False
    if mirror:
        # Arm mirror-until-forked. The rebind question is settled by
        # construction (this call just seeded the slot from the transcripts),
        # so mark the check done too.
        slot._channel_mirror_key = session_key
        slot._channel_mirror_len = len(slot.messages)
        slot._channel_mirror_mtime = float(session_info.get("modified", 0) or 0)
        slot._channel_mirror_checked = True
    logger.info(
        "Surfaced %s session %s as slot %s", channel_label(session_key), session_key, slot_name
    )
    return slot


def _apply_mirror(
    slot: "_ChatSlot",
    session_info: dict[str, Any],
    channel_messages: list[dict[str, Any]],
) -> int:
    """Append the channel turns a pure-mirror *slot* is missing. Returns count.

    Runs on the event loop (slot mutation). The appended turns are treated
    exactly like seeded ones: content is redacted, ``_resumed_count`` counts
    them as history rather than novel dashboard turns, and the dirty flag is
    preserved — mirroring alone never persists the slot, matching the seed's
    "a conversation the user never touches costs no write" rule. When the
    conversation does get picked up on the dashboard, the ordinary save
    re-serializes the whole window and the mirrored turns persist with it.
    """
    new = mirror_new_messages(slot.messages, channel_messages)
    dirty_was = slot._dirty
    had_reader = slot._has_reader
    for m in new:
        role = m.get("role", "assistant")
        slot.append(
            role,
            _redact(m.get("content", "")),
            f"msg msg-{'a' if role != 'user' else 'u'}",
            ts=m.get("ts", ""),
            # broadcast=False for the same reason as the seed: these are
            # copies of channel history, not dashboard-originated turns. An
            # attached stream reader still receives them via the pending
            # queue; everyone else gets the sidebar refresh from
            # push_slots_update.
            broadcast=False,
        )
    if new and not had_reader:
        # No live stream is consuming the pending queue — drop the copies so
        # they cannot pile up across passes (the seed drains for the same
        # reason). With a reader attached the queue is being consumed; leave
        # it so the open tab shows the new turns live.
        slot.drain()
    slot._dirty = dirty_was
    slot._resumed_count += len(new)
    slot._channel_mirror_len = len(slot.messages)
    slot._channel_mirror_mtime = float(session_info.get("modified", 0) or 0)
    return len(new)


def _mirror_intact(slot: "_ChatSlot") -> bool:
    """True while *slot*'s window shows no sign of a non-mirror mutation.

    Two invariants hold on a slot only the mirror has touched:

    - ``len(messages) == _channel_mirror_len`` — the mirror recorded the length
      after its last action, so any append since then is someone else's.
    - ``_resumed_count == len(messages)`` — seed and mirror both count their
      turns as resumed history. A plain append leaves ``_resumed_count`` behind,
      and regenerate/rewind reset it to 0 — so a NET-ZERO edit (regenerate
      replaces a message without changing the count) still breaks this
      invariant even though the length check alone would miss it.

    Either signal disarms; degrading to the frozen-copy behaviour is always
    safe, mirroring into a diverged transcript never is.
    """
    return (
        len(slot.messages) == slot._channel_mirror_len
        and slot._resumed_count == len(slot.messages)
    )


#: Per-state serialization of reconcile passes. The periodic loop and the
#: dispatcher's immediate `surface_dispatcher_session` can otherwise overlap,
#: and a pass acting on a pre-overlap metadata snapshot could clear a `closed`
#: flag the user wrote mid-race. Weak keys so a replaced DashboardState does
#: not pin its lock.
_RECONCILE_LOCKS: "weakref.WeakKeyDictionary[Any, asyncio.Lock]" = weakref.WeakKeyDictionary()

#: Per-state in-memory close tombstones: slot name -> epoch of the most recent
#: tab close. Written synchronously on the event loop by the tab-close paths
#: (see :func:`note_slot_closed`), read by the surface loop after its last
#: await. This is what makes a close AROUND a reconcile pass visible to it:
#: the disk flag alone is not enough, because the close SAVE lands only after
#: the close handler's awaits (task cancellation, file lock), so a pass can
#: snapshot still-open metadata after the slot was already popped. A tombstone
#: suppresses surfacing under the same rule as the disk flag — only channel
#: activity strictly newer than the close outruns it (see
#: :func:`_tombstone_blocks`) — so the suppression is independent of how the
#: pass's snapshot interleaves with the close. In-memory suffices — the
#: resurrect window only exists in-process (a slot can only be popped by this
#: process's own handlers), and by the time a tombstone expires the close
#: save has long since made the disk flag authoritative.
_RECENT_CLOSES: "weakref.WeakKeyDictionary[Any, dict[str, float]]" = weakref.WeakKeyDictionary()

#: Tombstones older than this are pruned; they only need to outlive a single
#: reconcile pass, and an hour is orders of magnitude beyond that.
_CLOSE_TOMBSTONE_TTL_SECS = 3600.0


def note_slot_closed(state: "DashboardState", slot_name: str) -> float:
    """Record that *slot_name*'s tab was just closed; return the close instant.

    Called synchronously on the event loop by every tab-close path, right where
    the slot is popped from ``state._slots``. A reconcile pass whose metadata
    snapshot predates this close checks these tombstones after its last await,
    so it cannot re-surface a conversation the user dismissed while the pass's
    executor work was in flight.

    The returned epoch is the instant the user acted. Callers persist it as the
    on-disk ``closed_at`` (via ``save_slot_off_loop(closed_at=...)``) instead of
    re-stamping at save time: the close save runs only after the handler's
    awaits (task cancellation, file lock), and channel activity landing in that
    window would otherwise compare as OLDER than the close and stay hidden.
    """
    closes = _RECENT_CLOSES.get(state)
    if closes is None:
        closes = {}
        _RECENT_CLOSES[state] = closes
    now = time.time()
    closes[slot_name] = now
    cutoff = now - _CLOSE_TOMBSTONE_TTL_SECS
    for stale in [k for k, v in closes.items() if v < cutoff]:
        del closes[stale]
    return now


def slot_closed_since(state: "DashboardState", slot_name: str, instant: float) -> bool:
    """True when *slot_name*'s tab was closed at or after *instant*.

    For any caller that snapshots session metadata, awaits, and then acts on
    that snapshot. A close recorded during the await leaves the on-disk metadata
    still reading *open* — the close handler pops the slot and calls
    :func:`note_slot_closed` synchronously, but persists the ``closed`` flag
    only after its own awaits (task cancellation, file lock) — so the snapshot
    alone cannot see it. Consulting the tombstone after the last await closes
    that window.

    Unlike :func:`_tombstone_blocks` this asks a plain question about one slot
    and does not weigh channel activity: callers that can be outrun by a newer
    inbound message should use that instead.
    """
    closes = _RECENT_CLOSES.get(state) or {}
    when = closes.get(slot_name)
    return when is not None and when >= instant


def _tombstone_blocks(state: "DashboardState", session: dict[str, Any]) -> bool:
    """True when an in-memory close tombstone suppresses surfacing *session*.

    Same rule as the disk flag (:func:`_close_stands`): the close stands unless
    the channel's last activity is strictly newer than the close instant. The
    comparison is against the session's own ``modified`` — NOT against the
    pass's snapshot time — so it does not matter whether the close happened
    before, during, or after the pass's snapshot: a close whose save is still
    in flight (open metadata on disk, slot already popped) is judged by the
    tombstone alone, and only genuinely newer channel activity outruns it.
    """
    closes = _RECENT_CLOSES.get(state) or {}
    when = closes.get(channel_slot_name(session.get("key", "")))
    if when is None:
        return False
    modified = float(session.get("modified", 0) or 0)
    return modified <= when


def _reconcile_lock(state: "DashboardState") -> asyncio.Lock:
    lock = _RECONCILE_LOCKS.get(state)
    if lock is None:
        lock = asyncio.Lock()
        _RECONCILE_LOCKS[state] = lock
    return lock


async def reconcile_channel_slots(state: "DashboardState", window_minutes: int) -> int:
    """One reconcile pass. Returns the number of slots surfaced or re-bound.

    Safe to call on the event loop: every filesystem read is offloaded, and only
    the in-memory slot mutations run on the loop (so ``state._slots`` is never
    touched from a worker thread). Passes are serialized per state so the
    periodic loop and a dispatcher-triggered immediate pass cannot interleave
    their snapshot/surface/clear sequences.
    """
    async with _reconcile_lock(state):
        return await _reconcile_channel_slots_locked(state, window_minutes)


async def _reconcile_channel_slots_locked(state: "DashboardState", window_minutes: int) -> int:
    log = state.conversation_log
    if log is None:
        return 0
    loop = asyncio.get_running_loop()
    cutoff = time.time() - (window_minutes * 60) if window_minutes > 0 else None

    try:
        sessions = await loop.run_in_executor(None, log.list_sessions)
    except Exception:
        logger.debug("channel reconcile: list_sessions failed", exc_info=True)
        return 0

    candidates = [s for s in sessions if is_channel_session_key(s.get("key", ""))]
    if not candidates:
        return 0

    def _load_meta() -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
        out: dict[str, dict[str, Any]] = {}
        mt: dict[str, float] = {}
        # Both the channel key and the slot key: the slot key is where a closed
        # tab records `closed`, and skipping it would resurface it every pass.
        # File mtimes ride along as the fallback close instant for legacy
        # `closed` flags that predate the `closed_at` stamp.
        mtime_of = getattr(log, "mtime_of", None)
        for s in candidates:
            for key in (s.get("key", ""), slot_history_key(s.get("key", ""))):
                if not key or key in out:
                    continue
                try:
                    out[key] = log.get_metadata(key)
                except Exception:
                    out[key] = {}
                if mtime_of is not None:
                    try:
                        stamp = mtime_of(key)
                    except Exception:
                        stamp = None
                    if stamp is not None:
                        mt[key] = stamp
        return out, mt

    # Instant the metadata snapshot below is taken. The stale-flag clear later
    # in this pass is scoped to closes OLDER than this — a `closed` written
    # after the snapshot (the user dismissing a tab mid-pass, or any writer
    # this pass cannot see) must survive the clear.
    snapshot_time = time.time()
    metadata, mtimes = await loop.run_in_executor(None, _load_meta)
    eligible = eligible_channel_sessions(
        candidates, metadata=metadata, cutoff=cutoff, mtimes=mtimes
    )
    # Skip transcript reads for sessions that already own a slot — the steady state.
    pending = [s for s in eligible if channel_slot_name(s.get("key", "")) not in state._slots]
    # Existing slots that may need a mirror sync. Everything here is a cheap
    # in-memory check; the transcript read only happens for slots that are
    # armed AND stale, or restored slots awaiting their one-time rebind check.
    mirror_pending: list[dict[str, Any]] = []
    for s in eligible:
        key = s.get("key", "")
        slot = state._slots.get(channel_slot_name(key))
        if slot is None:
            continue
        if slot._channel_mirror_key == key:
            if not _mirror_intact(slot):
                # A non-mirror mutation happened — an append (the user picked
                # the conversation up on the dashboard) or an in-place edit
                # like regenerate. Forked: stop mirroring for good. Fail-safe:
                # ANY untracked window change (including a trim) disarms,
                # degrading to the old frozen-copy behaviour rather than
                # risking an interleave.
                slot._channel_mirror_key = ""
                continue
            if float(s.get("modified", 0) or 0) <= slot._channel_mirror_mtime:
                continue  # channel file unchanged since the last sync
            mirror_pending.append(s)
        elif not slot._channel_mirror_checked:
            # Restored after a restart — the restore path knows nothing about
            # mirroring, so settle it once here. Only when the whole file is in
            # the window can purity be judged from memory; a longer file may
            # hide dashboard-authored turns in the frozen prefix, so fail safe.
            if slot._disk_older_count > 0:
                slot._channel_mirror_checked = True
                continue
            mirror_pending.append(s)
    if not pending and not mirror_pending:
        return 0

    def _load_messages() -> dict[str, _Transcript]:
        out: dict[str, _Transcript] = {}
        for s in pending:
            key = s.get("key", "")
            try:
                slot_msgs = log.read_messages(slot_history_key(key))
            except Exception:
                slot_msgs = []
            chan_ok = True
            try:
                chan_msgs = log.read_messages(key)
            except Exception:
                chan_msgs = []
                chan_ok = False
            merged = merge_transcripts(slot_msgs, chan_msgs)
            # Split the slot's own on-disk lines here, in the executor, while
            # both sides are still separable — after the merge they are not.
            older = frozen_prefix_len(slot_msgs, hydrate_window(merged))
            out[key] = _Transcript(
                merged,
                older,
                max(0, len(slot_msgs) - older),
                # A failed channel read proves nothing about purity — do not
                # arm the mirror on it. The slot surfaces without a mirror and
                # unchecked, so the rebind path re-judges it on a later pass.
                chan_ok and is_pure_mirror(slot_msgs, chan_msgs),
            )
        return out

    def _load_mirror_transcripts() -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for s in mirror_pending:
            key = s.get("key", "")
            try:
                out[key] = log.read_messages(key)
            except Exception:
                # Omit the key entirely: an empty transcript would look like a
                # successful sync and advance the watermark having copied
                # nothing, permanently skipping these messages if the channel
                # file never changes again. Absent key = untouched state,
                # retried on the next pass.
                logger.debug("channel mirror: read failed for %s", key, exc_info=True)
        return out

    transcripts = await loop.run_in_executor(None, _load_messages) if pending else {}
    mirror_transcripts = (
        await loop.run_in_executor(None, _load_mirror_transcripts) if mirror_pending else {}
    )

    # Clear stale closed flags BEFORE the slots become visible. Running the
    # clear after surfacing leaves a race: the slot broadcast lands, the user
    # closes the tab, the close save writes a fresh `closed`, and the deferred
    # clear then erases it — the next pass would reopen a tab the user just
    # dismissed. Clearing first is safe: these sessions already passed the
    # activity-outran-close check, so dropping the stale flags cannot keep
    # anything closed that should stay closed, and if surfacing subsequently
    # fails the next pass re-qualifies them from the same (now-unflagged)
    # state.
    reactivated = [
        s.get("key", "")
        for s in pending
        if (metadata.get(s.get("key", "")) or {}).get("closed")
        or (metadata.get(slot_history_key(s.get("key", ""))) or {}).get("closed")
    ]
    if reactivated:

        def _clear_stale_closed() -> None:
            clear = getattr(log, "clear_closed", None)
            if clear is None:
                return
            for k in reactivated:
                for kk in (k, slot_history_key(k)):
                    try:
                        # Compare-and-clear under the store's own lock: only a
                        # flag whose close instant predates this pass's
                        # metadata snapshot is dropped. A `closed` written
                        # after the snapshot — the user dismissing a tab while
                        # this pass ran, or a writer in another process — is
                        # left standing, so no stale snapshot can erase a
                        # fresh dismissal.
                        clear(kk, only_if_closed_before=snapshot_time)
                    except Exception:
                        # Best-effort: the flag staying behind only costs a
                        # redundant activity comparison on the next pass.
                        logger.warning(
                            "channel reconcile: failed to clear closed on %s", kk, exc_info=True
                        )

        # Off the loop: clear_closed takes the cross-process file lock.
        await loop.run_in_executor(None, _clear_stale_closed)

    surfaced = 0
    # A tab can be closed around this pass — resumed from History and
    # dismissed while the executor work was in flight, or dismissed just
    # before the snapshot with the close SAVE still awaiting (task
    # cancellation, file lock): either way the slot is gone from
    # ``state._slots`` while the on-disk metadata this pass read says open,
    # so the stale ``pending`` verdict would recreate the tab the user just
    # dismissed. Consult the in-memory tombstones the close paths write
    # synchronously at the pop, under the disk flag's own rule: the close
    # stands unless channel activity is strictly newer than it, regardless
    # of how it interleaved with this pass's snapshot. This runs after the
    # last await: nothing can close a slot between here and the surface
    # call below.
    for s in pending:
        key = s.get("key", "")
        if _tombstone_blocks(state, s):
            logger.debug("channel reconcile: %s closed by tombstone, skipping", key)
            continue
        transcript = transcripts.get(key) or _Transcript([], 0, 0)
        try:
            if surface_channel_session(
                state,
                s,
                metadata.get(key) or {},
                transcript.messages,
                disk_older=transcript.disk_older,
                disk_window=transcript.disk_window,
                mirror=transcript.pure_mirror,
            ):
                surfaced += 1
        except Exception:
            logger.warning("channel reconcile: failed to surface %s", key, exc_info=True)

    mirrored = 0
    for s in mirror_pending:
        key = s.get("key", "")
        slot = state._slots.get(channel_slot_name(key))
        if slot is None:
            continue
        chan_msgs = mirror_transcripts.get(key)
        if chan_msgs is None:
            # Read failed in the executor — leave the slot untouched (not
            # checked, watermark not advanced) so the next pass retries.
            continue
        try:
            if not slot._channel_mirror_checked:
                # One-time rebind after restore: arm only when nothing in the
                # window is dashboard-authored. Either way the question is
                # settled — never re-read this transcript on later passes.
                slot._channel_mirror_checked = True
                if not is_pure_mirror(slot.messages, chan_msgs):
                    continue
                slot._channel_mirror_key = key
                slot._channel_mirror_len = len(slot.messages)
            elif not _mirror_intact(slot):
                # Forked between the eligibility check and the executor read.
                slot._channel_mirror_key = ""
                continue
            mirrored += _apply_mirror(slot, s, chan_msgs)
        except Exception:
            logger.warning("channel reconcile: failed to mirror %s", key, exc_info=True)
    if surfaced or mirrored:
        state.push_slots_update()
    return surfaced


async def surface_dispatcher_session(dispatcher: object) -> None:
    """Surface a channel dispatcher's just-persisted session immediately."""
    cfg = getattr(dispatcher, "cfg", None)
    dashboard_cfg = getattr(cfg, "dashboard", None)
    if dashboard_cfg is None or not getattr(dashboard_cfg, "surface_channel_sessions", True):
        return
    state = getattr(dispatcher, "dashboard_state", None)
    if state is None:
        return
    await reconcile_channel_slots(
        state,
        int(getattr(dashboard_cfg, "restore_window_minutes", 30)),
    )


async def channel_slot_reconciler(state: "DashboardState", window_minutes: int) -> None:
    """Background task: reconcile now, then every ``RECONCILE_INTERVAL_SECS``.

    Runs for the gateway's lifetime. Every iteration is guarded so a transient
    filesystem error can never kill the loop.
    """
    while True:
        try:
            await reconcile_channel_slots(state, window_minutes)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("channel slot reconciler pass failed", exc_info=True)
        await asyncio.sleep(RECONCILE_INTERVAL_SECS)
