"""Surface channel-originated sessions in the dashboard's active chat list.

A conversation started on Slack (or Discord/Telegram/Teams/…) persists under a
channel-namespaced session key such as ``slack:<thread_ts>``. The dashboard's
slot restore paths only ever built slots for ``dashboard:``-prefixed keys, so
those conversations existed on disk but had no chat slot — they only appeared
in the sidebar's collapsed **History** pane, where the user had to search for
one and resume it by hand.

This module reconciles recent channel sessions INTO slots so they show up in
the active chat list, ready to continue.

Design notes:

* **One session, one transcript, two surfaces.** The slot's key is derived from
  the channel session key (``slack:1785370133.085469`` ->
  ``slack_1785370133.085469``) so the sidebar reads provenance straight off it,
  but the slot is BOUND to the channel session via ``linked_session_key``. That
  binding is what makes the tab the conversation rather than a picture of it:
  turns run on the channel's own session (one ``kiro-cli`` process, one
  ``Semaphore``), and because ``history._safe_key`` folds ``slack:<ts>`` and the
  ``slack_<ts>`` filename stem onto the same file, the slot's transcript IS the
  channel transcript. A reply typed in the tab reaches the thread; a message
  sent in the thread appears in the tab.

  The binding is persisted to the transcript's metadata line, because nothing
  else recreates it: a cron slot is re-bound by its next injection, but a
  channel slot has no such trigger, so without persistence a gateway restart
  would silently downgrade the tab to a disconnected copy.
* **Recency-bounded.** Only sessions modified within the dashboard's configured
  ``restore_window_minutes`` become slots, so a long DM history does not turn
  into hundreds of tabs. Pinned and foldered sessions are exempt from the
  window, matching :func:`~kiro_crew.dashboard.chat_persistence.restore_recent_sessions`.
* **Closed is sticky — until the channel moves on.** A session the user closed
  on the dashboard (``meta.closed``) is not re-surfaced by the next reconcile
  pass — otherwise closing the tab would be undone 30 seconds later. But a
  close is a statement about the conversation *as it stood*: channel-side
  activity strictly newer than the close (the person kept talking on Discord
  after the tab was dismissed) re-surfaces it, and the stale ``closed`` flag is
  cleared so every restore path agrees the tab is open again. When the close
  instant is unknown (legacy flag with no ``closed_at`` stamp and no readable
  file mtime), the close stands — fail toward the user's explicit dismissal.

  Closing a tab drops the surface, not the conversation: the channel session
  keeps running and the next message from either side resumes it with full
  history.
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
from itertools import islice
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.channel_folders import lookup_channel_folder
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import _normalize_slot_key, durable_row_count
from kiro_crew.history import carry_provenance, is_incognito_transcript
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.messaging.link import channel_namespace_of, is_channel_session_key
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: How often the background reconciler runs. Not user-configurable: it only
#: bounds how stale the sidebar can be for a channel conversation started while
#: the dashboard is already open, and each pass is a cheap metadata scan.
RECONCILE_INTERVAL_SECS = 30

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
    "imessage": "iMessage",
    "feishu": "Feishu",
    "unified": "Direct message",
}
#: In-memory window for a newly surfaced slot. Deliberately the same bound the
#: dashboard's own ``restore_recent_sessions`` uses, so a channel tab and a
#: dashboard tab of equal length hold equal amounts of history — the tab is the
#: conversation, so it must not be shorter than one restored from the sidebar.
#: Older lines stay on disk as the frozen prefix and are reachable through the
#: slot-detail endpoint's pagination.
_RESTORE_WINDOW = 500


def channel_label(session_key: str) -> str:
    """Human-facing label for the channel *session_key* came from."""
    return _CHANNEL_LABELS.get(channel_namespace_of(session_key), "Channel")


def channel_slot_name(session_key: str) -> str:
    """Return the dashboard slot key for a channel session key.

    Deterministic and idempotent — the same channel session always maps to the
    same slot, which is what makes repeat reconcile passes no-ops and lets the
    key itself carry the conversation's provenance.

    This is a slot NAME, not a session key: it is the channel key folded to the
    filename charset, and the fold is not reversible. Never turn it back into a
    session key by guessing where the colons were — read the binding off the
    slot (``linked_session_key``) or ask
    :meth:`~kiro_crew.session.SessionManager.channel_key_for_stem`.
    """
    return _normalize_slot_key(session_key)


def _redact_assistant(content: str) -> str:
    """Apply the transcript read boundary's redaction to assistant content.

    Same rule and same order as the dashboard's own restore path — one
    transcript cannot have two redaction policies.
    """
    content, _ = redact_exfiltration_urls(content)
    content, _ = redact_credentials(content)
    return content


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
    mtimes: dict[str, float],
) -> bool:
    """True when the user's dismissal of this conversation is still in force.

    A close is not permanent: channel-side activity strictly newer than the
    close means the conversation came back to life after the user dismissed
    it, so it re-qualifies for surfacing. The comparison is against the
    session listing's ``modified`` (the channel file's last write). An unknown
    close instant keeps the close standing, failing toward the user's explicit
    action.

    Only one flag to weigh: the tab and the channel share one transcript, so a
    close is recorded once.
    """
    if not meta.get("closed"):
        return False
    closed_at = _close_time(meta, mtimes.get(session.get("key", "")))
    if closed_at is None:
        return True
    return float(session.get("modified", 0) or 0) <= closed_at


def eligible_channel_sessions(
    sessions: list[dict[str, Any]],
    *,
    metadata: dict[str, dict[str, Any]],
    cutoff: float | None,
    mtimes: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Filter a ``list_sessions()`` result down to channel sessions to surface.

    *metadata* maps session key -> ``get_metadata()`` result. *mtimes* maps the
    same keys to their session-file mtimes, the fallback close instant for a
    ``closed`` flag with no ``closed_at`` stamp (see :func:`_close_stands`);
    with it absent, every close stands. *cutoff* is a unix timestamp; sessions
    older than it are dropped unless pinned or foldered. ``None`` disables the
    recency filter (mirrors ``restore_window_minutes=0``).

    Pure and side-effect free so the eligibility rules are directly testable.
    """
    out: list[dict[str, Any]] = []
    for s in sessions:
        key = s.get("key", "")
        if not key or not is_channel_session_key(key):
            continue
        meta = metadata.get(key) or {}
        # A close only stands until the channel outruns it: activity newer
        # than the close re-qualifies the session (and the reconciler then
        # clears the stale flag — see reconcile_channel_slots).
        if _close_stands(s, meta, mtimes or {}):
            continue
        # An ephemeral (incognito/temporary) session must never be surfaced as
        # a durable slot; either the metadata line or the listing row can carry
        # the mode, so both are classified.
        modes = (meta.get("memory_mode"), s.get("memory_mode"))
        if any(is_incognito_transcript(m) for m in modes):
            continue
        exempt = bool(meta.get("pinned")) or bool(meta.get("folder_id"))
        if not exempt and cutoff is not None and float(s.get("modified", 0) or 0) < cutoff:
            continue
        out.append(s)
    return out


def needs_default_filing(meta: dict[str, Any]) -> bool:
    """True when per-channel default filing has never been applied to a session.

    THE single definition, used both to decide whether a folder needs looking up
    and to decide whether to apply one. Two separate decisions is what broke this
    before: the lookup was skipped per-session but the resulting folder was then
    applied per-NAMESPACE, so a conversation the user had moved to the top level
    was re-filed from a folder resolved for a different conversation.

    Any ONE of three durable records means the placement is already the user's:

    * ``folder_id`` — the session currently sits in a folder.
    * ``channel_folder_filed`` — filing already ran for this conversation. Written
      at filing time, so it protects the window before the slot's first save.
    * ``channel_origin`` — this conversation has been surfaced as a dashboard tab
      before and saved at least once (``_save_slot_to_history`` persists the
      provenance flag). Needed because filing may have been OFF at that first
      surface, leaving neither of the other two keys: the user could then move the
      tab into a folder and back out to the top level, and switching filing on
      later would otherwise treat it as never-surfaced and overwrite that
      deliberate placement.

    The three are complementary, not redundant: the marker covers the window
    before any save, and ``channel_origin`` covers conversations first surfaced
    while the feature was off. Default filing is a first-surface action, and
    first surface happens exactly once per conversation.
    """
    return not (
        meta.get("folder_id") or meta.get("channel_folder_filed") or meta.get("channel_origin")
    )


def surface_channel_session(
    state: "DashboardState",
    session_info: dict[str, Any],
    meta: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    session_key: str = "",
    folder_id: str = "",
    folder_tags: list[str] | None = None,
) -> "_ChatSlot | None":
    """Create the dashboard slot for one channel session.

    Returns the slot when this call newly surfaced it, else ``None`` (already
    present — the common steady-state case).

    *messages* is the channel transcript to seed the slot's window with, read by
    the caller so the disk IO stays off the event loop.

    *session_key* is the conversation's REAL session key (``slack:<ts>``), which
    the slot is bound to. ``session_info["key"]`` cannot supply it:
    ``list_sessions()`` reports filename stems, where every ``:`` has been
    folded to ``_``. When the caller could not resolve it the slot is still
    surfaced but left unbound, so it shows the history without claiming to be
    two-way — a wrong key would route the user's replies to a session the
    channel never reads.

    *folder_id* is the channel's configured session folder (see
    :mod:`kiro_crew.dashboard.channel_folders`), applied only when the session
    does not already carry a folder of its own — a conversation the user filed by
    hand keeps where they put it. The caller withholds it for a conversation that
    has already been filed once, so a later move (including a move back out to
    the top level) is never undone; see :func:`reconcile_channel_slots`.

    *folder_tags* are the target folder's organizational tags, resolved and
    validated by the caller (vocabulary membership, string ids). They are copied
    onto the slot only on the same first-filing branch that applies *folder_id*:
    a channel chat born into a tagged folder inherits exactly like a dashboard
    chat created in it (creation-only — moves don't retro-tag). Tags persisted in
    *meta* (written atomically with the filing marker by the reconcile pass) are
    applied on every surface: that is recovery of an inheritance that already
    happened, not a re-inheritance, so it does not violate the creation-only rule.
    """
    stem = session_info.get("key", "")
    if not stem or not is_channel_session_key(stem):
        return None

    # The slot key is derived from the channel session key, deterministically and
    # idempotently, so it IS the record of where the conversation started — the
    # sidebar reads provenance straight off it, and every restore path preserves
    # it for free because the key is the slot's identity.
    slot_name = channel_slot_name(stem)
    if slot_name in state._slots:
        return None
    if session_key and not is_channel_session_key(session_key):
        logger.warning(
            "channel surface: refusing to bind %s to non-channel key %r", stem, session_key
        )
        session_key = ""
    if not session_key:
        logger.info("channel surface: %s has no mapped session key; surfacing unbound", stem)
    try:
        slot = state.get_or_create_slot(
            name=slot_name,
            agent=meta.get("agent", "") or "",
            linked_session_key=session_key,
            channel_origin=True,
        )
    except ValueError:
        # A slot with this name exists under a conflicting memory_mode. Leave it
        # alone rather than fighting over the key.
        logger.debug("channel slot %s exists with a conflicting memory_mode", slot_name)
        return None

    raw_title = session_info.get("title") or meta.get("title") or ""
    slot.title = _redact_assistant(raw_title) if raw_title else channel_label(stem)
    slot._titled = bool(raw_title)
    if meta.get("created_at"):
        slot.created_at = meta["created_at"]
    # The identity of the transcript this surfacing read — lets a later save
    # recognize a file recreated by another writer after a permanent delete
    # (the delete-won guard in ``_save_slot_to_history``). Channel slots adopt
    # append-created transcripts, so the observed on-disk value is the only
    # honest anchor (the slot's own construction time never matches it). The
    # observed bit records that this read happened even for legacy metadata
    # without created_at, so the delete-won guard's evidence gate engages.
    slot._disk_meta_created_at = str(meta.get("created_at") or "")
    slot._disk_meta_observed = bool(meta)
    if meta.get("model"):
        slot.model = meta["model"]
    if meta.get("autocompact_pct") is not None:
        # Restore the per-session compaction threshold, mirroring the
        # persistence loaders: without this, a surfaced slot's field stays
        # None and the next save overwrites the persisted override with null.
        # Local import: chat_persistence imports from this module, so a
        # module-level import here would be circular.
        from kiro_crew.dashboard.chat_persistence import _validate_autocompact_pct

        slot.autocompact_pct = _validate_autocompact_pct(meta["autocompact_pct"])
        if slot.autocompact_pct is not None and state.sessions:
            state.sessions.set_autocompact_pct(effective_session_key(slot), slot.autocompact_pct)
    if meta.get("workspace"):
        slot.workspace = meta["workspace"]
    if meta.get("project"):
        slot.project = meta["project"]
    if meta.get("channel_folder_filed"):
        slot._channel_folder_filed = True
    # Persisted tags are applied on EVERY surface, not just first filing: the
    # filing write stores the inherited tags atomically with the filing marker
    # (see reconcile), so a restore after a crash — where the marker exists but
    # the slot never saved — recovers them from here. Validation is the shared
    # authority-aware helper, matching the sibling restore paths: it fails OPEN
    # when the vocabulary is UNKNOWN, because pruning against an unknown
    # vocabulary would drop every id, the next save would persist the loss, and
    # the sticky filing marker blocks re-inheritance forever. Imported locally to
    # avoid the module cycle through chat_persistence (chat_tags →
    # chat_persistence → this module).
    from kiro_crew.dashboard.chat_tags import validate_folder_tag_ids

    for tid in validate_folder_tag_ids(meta.get("tags"), state):
        if tid not in slot.tags:
            slot.tags.append(tid)
    # Set by the default-filing branch below when the revalidation drops the id.
    placement_rejected = False
    if meta.get("folder_id"):
        # ADOPTED VERBATIM, as the base did. A malformed or deleted id is absorbed
        # downstream: the sidebar renders it Unfiled. See history.md.
        slot.folder_id = meta["folder_id"]
    elif folder_id and needs_default_filing(meta):
        # Per-channel filing (off by default), applied on the pass that first
        # surfaces the conversation. Re-tested here rather than trusting the
        # caller: the folder is resolved once per NAMESPACE, so the same value
        # reaches every pending conversation of that channel, and applying it to
        # one that has already been filed would silently overwrite wherever the
        # user moved it. `folder_id` is omitted from the metadata line when
        # empty, so the marker is the only thing that distinguishes "moved to the
        # top level" from "never filed".
        #
        # FOURTH copy site, and REVALIDATED like the other three.
        # ``reconcile_channel_slots`` resolves this id across one await
        # (``lookup_channel_folder``) and PERSISTS it across another before
        # surfacing, so a delete committing inside that window leaves it naming a
        # folder that is already gone. The folder-delete sweep cannot cover that
        # case even in principle: while the conversation is still PENDING no live
        # slot names the folder, so the sweep finds nothing to clear and completes
        # having touched it. Only checking at the assignment closes it.
        kept = state.folder_id_for_restore(folder_id)
        slot.folder_id = kept
        slot._channel_folder_filed = True
        # First filing = this chat's birth into the folder: copy the folder's
        # tags by value, the same creation-only inheritance the dashboard
        # slot-create path applies. The restore branch above deliberately does
        # not — a persisted folder_id means the filing already happened.
        #
        # Gated on ``kept`` because the revalidation immediately above can REFUSE
        # this filing: when the folder was deleted inside the resolve-then-persist
        # window, ``kept`` is "" and the chat is not born into that folder at all,
        # so there is nothing to inherit from. In every case the inheritance
        # feature was designed for the folder exists, ``kept`` equals *folder_id*,
        # and the tags are copied exactly as before.
        if kept:
            for tid in folder_tags or []:
                if tid not in slot.tags:
                    slot.tags.append(tid)
        # Armed AFTER ``_rebuild_window`` below, not here: that helper ends with
        # ``slot._dirty = False`` (the window it just built matches the file, so
        # nothing is owed for it) and would silently swallow a flag set at this
        # point. Carried as a local instead.
        placement_rejected = not kept
    if meta.get("pinned"):
        slot.pinned = True

    # Same shape as restore_recent_sessions: window the tail, count the rest as
    # the frozen prefix a save never rewrites, and redact assistant content at
    # the read boundary.
    _rebuild_window(slot, messages)
    if placement_rejected:
        # The rejected placement is ALREADY ON DISK -- the caller persists it
        # before surfacing, precisely so the tab cannot appear un-filed and be
        # dragged elsewhere first -- so correcting it in memory alone would leave
        # the dead id to be read back and republished. Arm the flush to rewrite
        # the record from this now-corrected object. Not armed when the placement
        # stands: that record is already right and re-saving it is pure churn.
        slot._dirty = True
    # The window corresponds to the file as of this listing, so the refresh pass
    # has nothing to do until the channel writes again.
    slot._channel_window_mtime = float(session_info.get("modified", 0) or 0)
    logger.info(
        "Surfaced %s session %s as slot %s", channel_label(stem), session_key or stem, slot_name
    )
    return slot


def _rebuild_window(slot: "_ChatSlot", messages: list[dict[str, Any]]) -> None:
    """Replace *slot*'s in-memory window with the tail of *messages*.

    The one place that decides what a bound slot's window IS: the last
    :data:`_RESTORE_WINDOW` lines of its transcript, with everything older
    counted as the frozen prefix a save never rewrites. Shared by the initial
    surface and by rotation recovery so the two can never disagree about the
    accounting.

    Callers must hold an idle, non-dirty slot — the window is discarded, so
    anything in it that is not yet on disk would be lost.
    """
    slot.messages.clear()
    slot._pending.clear()
    older_cut = max(0, len(messages) - _RESTORE_WINDOW)
    slot._disk_older_count = older_cut
    # Durable-only view of the same prefix (transient-role lines excluded),
    # recomputed from the transcript on every rebuild — the base absolute
    # message positions are built over. ``islice`` avoids copying the whole
    # prefix. See _ChatSlot.__init__.
    slot._disk_older_durable_count = durable_row_count(islice(messages, older_cut))
    for msg in messages[-_RESTORE_WINDOW:]:
        role = msg.get("role", "assistant")
        cls = msg.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
        content = msg.get("content", "")
        if role != "user":
            content = _redact_assistant(content)
        slot.append(
            role,
            content,
            cls,
            ts=msg.get("ts", ""),
            broadcast=False,
            meta=(msg["meta"] if isinstance(msg.get("meta"), dict) else None),
            mint_mid=False,
        )
        # This transcript is the CHANNEL's, so most of these lines arrived from
        # Slack/Discord and carry a real origin. Provenance is not a
        # slot.append() argument, so copy it onto the message the append just
        # created — the save path re-serializes this window and would otherwise
        # restamp every line "dashboard".
        carry_provenance(slot.messages[-1], msg)
    slot.drain()
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    # Every line came from the file a save would write back.
    slot._dirty = False


def _window_matches_disk(slot: "_ChatSlot", messages: list[dict[str, Any]]) -> bool:
    """True when *slot*'s window is still the transcript slice it claims to be.

    The counters say the window is ``messages[_disk_older_count:][:len(window)]``.
    Rotation breaks that without necessarily changing the file's LENGTH — it
    archives the head, and new turns can bring the count back to where it was —
    so the offsets shift while the arithmetic still adds up. Comparing content is
    what actually detects it.

    Matched on ``(role, ts, content)``: the window's ``cls`` and ``meta`` are
    presentation state that a persisted line need not carry, and content is
    comparable because the write boundary already redacted non-user text before
    it reached disk.
    """
    older = slot._disk_older_count
    window = slot.messages
    if older + len(window) > len(messages):
        return False
    expected = messages[older : older + len(window)]
    for mem, disk in zip(window, expected):
        if (
            mem.get("role") != disk.get("role")
            or mem.get("ts", "") != disk.get("ts", "")
            or mem.get("content", "") != disk.get("content", "")
        ):
            return False
    return True


def refresh_channel_window(slot: "_ChatSlot", messages: list[dict[str, Any]], mtime: float) -> int:
    """Bring a bound slot's in-memory window up to date with its transcript.

    The tab and the channel write one file, but the tab's window is a snapshot
    of its tail. A turn the CHANNEL writes after the tab opened lands on disk
    with nothing to put it in the window, so the tab would show a conversation
    that has moved on without it.

    Returns the number of messages appended. The slot represents exactly
    ``_disk_older_count + len(slot.messages)`` lines of the file, so anything
    beyond that is new; the messages come from the very file a save would write
    back, which is why the window counters advance rather than the slot being
    marked dirty.

    Callers MUST NOT call this for a slot with a turn in flight or unflushed
    edits — see :func:`_window_refresh_is_safe`.
    """
    represented = slot._disk_older_count + len(slot.messages)
    if not _window_matches_disk(slot, messages):
        # The transcript rotated: ``ConversationLog`` caps file size and archives
        # the head, so the lines the slot's counters point at are no longer the
        # lines at those offsets. A length test alone would miss the case where
        # rotation archived the head and new turns brought the file back to the
        # same length — the counters still add up while every offset has shifted.
        # Left alone the window holds pre-rotation content, and a save writes the
        # window back, so turns rotation just archived reappear in the active
        # transcript. Rebuild from the file as it stands.
        #
        # Safe because a refresh only runs on an idle, non-dirty slot: the window
        # is a copy of the file's tail and holds nothing the file does not, so
        # replacing it cannot lose a message. Lines rotation moved to the archive
        # stay reachable through ``read_messages_chained``'s ``tab_id`` walk.
        logger.info(
            "channel window: transcript for %s no longer aligns (%d lines on disk, "
            "%d accounted); rebuilding window",
            slot.key,
            len(messages),
            represented,
        )
        _rebuild_window(slot, messages)
        slot._channel_window_mtime = mtime
        return 0
    fresh = messages[represented:]
    if not fresh:
        slot._channel_window_mtime = mtime
        return 0
    for msg in fresh:
        role = msg.get("role", "assistant")
        cls = msg.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
        content = msg.get("content", "")
        if role != "user":
            content = _redact_assistant(content)
        slot.append(
            role,
            content,
            cls,
            ts=msg.get("ts", ""),
            # These lines came from the channel, not from this dashboard's
            # composer, so nothing has rendered them optimistically -- ask for
            # the user rows to be broadcast or the tab shows the reply without
            # the message that prompted it.
            broadcast_user=True,
            meta=(msg["meta"] if isinstance(msg.get("meta"), dict) else None),
            mint_mid=False,
        )
        # See the equivalent call in _rebuild_window.
        carry_provenance(slot.messages[-1], msg)
    slot.drain()
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    # Everything appended came from the file itself, so there is nothing to
    # write back.
    slot._dirty = False
    slot._channel_window_mtime = mtime
    return len(fresh)


def _window_refresh_is_safe(slot: "_ChatSlot") -> bool:
    """True when *slot*'s window can be reconciled against disk right now.

    A turn in flight, or an unflushed edit, means the window holds messages the
    file does not yet have. The line accounting a refresh relies on would then
    mistake those for missing history and duplicate them, so defer instead —
    the next pass retries once the turn has landed.
    """
    return bool(slot.linked_session_key) and not slot.running and not slot._dirty


#: Per-state reconcile lock. Keyed weakly so a discarded state is collectable —
#: a WeakKeyDictionary lets the lock die with the state it guards rather than
#: not pin its lock.
_RECONCILE_LOCKS: "weakref.WeakKeyDictionary[Any, LoopBoundLock]" = weakref.WeakKeyDictionary()

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

    Keyed by the slot name exactly as it sits in ``state._slots``, which is what
    :func:`_tombstone_blocks` reconstructs with :func:`channel_slot_name`. The
    two derivations must stay identical or the guard silently stops matching.
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


def _reconcile_lock(state: "DashboardState") -> LoopBoundLock:
    lock = _RECONCILE_LOCKS.get(state)
    if lock is None:
        lock = LoopBoundLock()
        _RECONCILE_LOCKS[state] = lock
    return lock


async def reconcile_channel_slots(state: "DashboardState", window_minutes: int) -> int:
    """One reconcile pass. Returns the number of slots surfaced.

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
        # One key per session: the tab and the channel share one transcript, so
        # `closed` is written once and read once. File mtimes ride along as the
        # fallback close instant for legacy `closed` flags that predate the
        # `closed_at` stamp.
        mtime_of = getattr(log, "mtime_of", None)
        for s in candidates:
            key = s.get("key", "")
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
    # Already-surfaced tabs whose shared transcript has grown since their window
    # was built: the channel wrote a turn straight to the file, and the tab has
    # nothing that would put it in the window. Gated on the file's mtime so the
    # steady state stays a metadata-only scan.
    refreshable: list[dict[str, Any]] = []
    for s in eligible:
        key = s.get("key", "")
        slot = state._slots.get(channel_slot_name(key))
        if slot is None or not _window_refresh_is_safe(slot):
            continue
        if float(s.get("modified", 0) or 0) > slot._channel_window_mtime:
            refreshable.append(s)
    if not pending and not refreshable:
        return 0

    def _load_messages() -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for s in pending + refreshable:
            key = s.get("key", "")
            if key in out:
                continue
            try:
                out[key] = log.read_messages(key)
            except Exception:
                # Omit the key rather than storing an empty list: a failed read
                # must not look like "the file is empty" and advance a watermark
                # past turns it never saw.
                logger.debug("channel reconcile: read failed for %s", key, exc_info=True)
        return out

    transcripts = await loop.run_in_executor(None, _load_messages)

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
        s.get("key", "") for s in pending if (metadata.get(s.get("key", "")) or {}).get("closed")
    ]
    if reactivated:

        def _clear_stale_closed() -> None:
            clear = getattr(log, "clear_closed", None)
            if clear is None:
                return
            for k in reactivated:
                try:
                    # Compare-and-clear under the store's own lock: only a
                    # flag whose close instant predates this pass's
                    # metadata snapshot is dropped. A `closed` written
                    # after the snapshot — the user dismissing a tab while
                    # this pass ran, or a writer in another process — is
                    # left standing, so no stale snapshot can erase a
                    # fresh dismissal.
                    clear(k, only_if_closed_before=snapshot_time)
                except Exception:
                    # Best-effort: the flag staying behind only costs a
                    # redundant activity comparison on the next pass.
                    logger.warning(
                        "channel reconcile: failed to clear closed on %s", k, exc_info=True
                    )

        # Off the loop: clear_closed takes the cross-process file lock.
        await loop.run_in_executor(None, _clear_stale_closed)

    # Per-channel session filing (off unless configured). Resolved once per
    # namespace, and only for conversations that still need it, so a pass with
    # nothing to file reads no config at all.
    folder_ids: dict[str, str] = {}
    for s in pending:
        key = s.get("key", "")
        if not needs_default_filing(metadata.get(key) or {}):
            continue
        ns = channel_namespace_of(key)
        if ns and ns not in folder_ids:
            folder_ids[ns] = await lookup_channel_folder(state, ns)

    # Folder tags are deliberately NOT read here: the raw ids are read fresh
    # under ``tags_write_lock`` immediately before validation and the filing
    # write below, so a folder PATCH or tag deletion landing while this pass
    # runs can never stamp an obsolete tag set onto a freshly filed chat.

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
        # Per-SESSION, not per-namespace: `folder_ids` is resolved once per
        # channel, so this is what keeps an already-filed conversation from
        # being handed another conversation's folder.
        to_file = (
            folder_ids.get(channel_namespace_of(key), "")
            if needs_default_filing(metadata.get(key) or {})
            else ""
        )
        # Populated by the filing branch below with the ids it actually
        # persisted, so the surface call applies exactly what the atomic
        # write recorded — never the raw (or stale) resolve-time values.
        inherited: list[str] = []
        if _tombstone_blocks(state, s):
            logger.debug("channel reconcile: %s closed by tombstone, skipping", key)
            continue
        if key not in transcripts:
            # The read failed in the executor. ``_load_messages`` omits the key
            # rather than storing ``[]`` exactly so this is distinguishable from
            # a genuinely empty transcript — surfacing an empty window here would
            # give the slot a zero frozen-prefix count, and the next dashboard
            # reply would write its turns ahead of the history still on disk.
            # Leave it for the next pass.
            logger.debug("channel reconcile: %s transcript unread, deferring", key)
            continue
        if to_file:
            # Re-check right before writing: this pass snapshotted metadata, then
            # awaited a transcript read and a config read. In that window the user
            # can resume this conversation from History and move it, and a slot
            # now existing is the evidence that happened — filing over it would
            # restore the default folder after the next restart.
            if channel_slot_name(key) in state._slots:
                logger.debug("channel reconcile: %s surfaced while this pass ran; not filing", key)
                to_file = ""
        if to_file:
            # Persist the placement BEFORE the slot becomes visible.
            # ``get_or_create_slot`` pushes a slots update, so the moment this
            # conversation is surfaced the user can see it and drag it somewhere
            # else — and their move saves immediately. A merge landing AFTER that
            # would overwrite the move with the default folder and the next
            # restart would put the session back, losing a user action.
            #
            # Ordering alone does not close that window: this write waits on the
            # cross-process history lock, and the user's move can acquire it
            # first, so "we wrote first" is not guaranteed by issuing the write
            # first. The guard re-decides under the lock — if the record has
            # since gained a placement or a filing marker of its own, the merge
            # is skipped and their move stands.
            #
            # Under `key`, the session key this pass read its metadata from, so
            # the record the skip decision consults is the record that gets the
            # marker. Metadata-only merge, off the loop: it must not rewrite the
            # transcript the channel side appends to, and it takes a
            # cross-process lock.
            try:
                # The inherited tags ride the SAME atomic write as the filing
                # marker: the marker is what tells every later pass (and a
                # post-crash restore) that inheritance already ran, so persisting
                # it without the tags would make a crash between this write and
                # the slot's first save silently drop them — the marker would
                # block re-inheritance forever. One write keeps marker and tags
                # crash-consistent.
                #
                # Validation happens HERE, at the point of application — and so
                # does the READ: the folder's raw tag ids are read fresh inside
                # the critical section, so a folder PATCH or tag deletion that
                # committed while this pass ran is fully visible before anything
                # is stamped onto the filed chat. The read, the intersection AND
                # the filing write sit under ``tags_write_lock``, mirroring
                # ``api_chat_slot_tags`` (whose docstring names preventing
                # exactly this race); the lock ordering (tags_write_lock →
                # folder-store lock) matches the folder create/PATCH paths.
                # Marker + tags stay in ONE atomic write for crash consistency.
                # Imported locally to avoid the module cycle through
                # chat_persistence (chat_tags → chat_persistence → this module).
                from kiro_crew.dashboard.chat_tags import (
                    tags_write_lock,
                    validate_folder_tag_ids,
                )

                def _read_folder_tags(
                    folders: list[dict[str, Any]], fid: str = to_file
                ) -> list[str]:
                    for f in folders:
                        if f.get("id") == fid and isinstance(f.get("tags"), list):
                            return list(f["tags"])
                    return []

                filing_meta: dict[str, Any] = {
                    "folder_id": to_file,
                    "channel_folder_filed": True,
                }
                async with tags_write_lock(state):
                    raw_folder_tags = await state.read_folders(_read_folder_tags)
                    inherited = validate_folder_tag_ids(raw_folder_tags, state)
                    if inherited:
                        filing_meta["tags"] = list(inherited)
                    filed = await asyncio.to_thread(
                        log.update_metadata_if,
                        key,
                        filing_meta,
                        needs_default_filing,
                    )
            except Exception:
                # Could not record it, so do not apply it in memory either:
                # an in-memory-only placement would be lost on restart and
                # filed again by the next pass. Leave the conversation unfiled
                # and let a later pass retry.
                logger.warning(
                    "channel reconcile: could not persist folder filing for %s",
                    key,
                    exc_info=True,
                )
                to_file = ""
            else:
                if not filed:
                    # The guard rejected it under the lock: the record gained a
                    # placement or a filing marker while this write queued. That
                    # is the user's own action, so surface the conversation
                    # unfiled rather than applying a placement that is no longer
                    # correct — and do not retry, since the record now carries
                    # evidence that filing is settled.
                    logger.debug(
                        "channel reconcile: %s was placed while this pass ran; not filing",
                        key,
                    )
                    to_file = ""
        try:
            slot = surface_channel_session(
                state,
                s,
                metadata.get(key) or {},
                transcripts[key],
                session_key=state.sessions.channel_key_for_stem(key) if state.sessions else "",
                folder_id=to_file,
                folder_tags=inherited if to_file else None,
            )
            if slot:
                surfaced += 1
        except Exception:
            logger.warning("channel reconcile: failed to surface %s", key, exc_info=True)

    refreshed = 0
    for s in refreshable:
        key = s.get("key", "")
        slot = state._slots.get(channel_slot_name(key))
        msgs = transcripts.get(key)
        if slot is None or msgs is None:
            # Slot closed during this pass, or the read failed — leave the
            # watermark alone so the next pass retries.
            continue
        # Re-check after the awaits: a turn may have started on either surface
        # while the executor read was in flight, and its unflushed messages
        # would break the line accounting.
        if not _window_refresh_is_safe(slot):
            continue
        try:
            refreshed += refresh_channel_window(slot, msgs, float(s.get("modified", 0) or 0))
        except Exception:
            logger.warning("channel reconcile: failed to refresh %s", key, exc_info=True)

    if surfaced or refreshed:
        if surfaced:
            # Publish the new tab to the dashboard-surface registry BEFORE the
            # broadcast. Every gate that asks "does this session have a tab?"
            # reads that registry, so a surfaced-but-unpublished slot silently
            # loses widgets, question cards, approval prompts and tab-directed
            # routing until some unrelated slot change happens to republish.
            from kiro_crew.dashboard.chat_utils import _sync_dashboard_slots

            _sync_dashboard_slots(state)
        state.push_slots_update()
    return surfaced


async def surface_channel_state(state: object | None, dashboard_cfg: object) -> None:
    """Surface/refresh channel-session slots against an explicit state + config.

    The dispatcher-free entry point. Most transports own a dispatcher object and
    call :func:`surface_dispatcher_session`, but Slack drives its turns through
    ``handle_message_transport`` with no dispatcher to hand over -- which is why
    it was the only transport with no dashboard hook at all, leaving the
    30-second reconciler as the sole path by which a Slack turn reached an open
    tab.
    """
    if state is None:
        return
    if dashboard_cfg is None or not getattr(dashboard_cfg, "surface_channel_sessions", True):
        return
    await reconcile_channel_slots(
        state,  # type: ignore[arg-type]
        int(getattr(dashboard_cfg, "restore_window_minutes", 30)),
    )


async def surface_dispatcher_session(dispatcher: object) -> None:
    """Surface a channel dispatcher's just-persisted session immediately."""
    cfg = getattr(dispatcher, "cfg", None)
    await surface_channel_state(
        getattr(dispatcher, "dashboard_state", None),
        getattr(cfg, "dashboard", None),
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
