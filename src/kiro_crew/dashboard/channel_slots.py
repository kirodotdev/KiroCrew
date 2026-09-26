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

from kiro_crew.dashboard.channel_folders import (
    CHANNEL_CONFIG_SECTIONS,
    configured_folder_name,
    folder_id_for_name,
    lookup_channel_folder,
)
from kiro_crew.dashboard.chat_title import _persist_title
from kiro_crew.dashboard.chat_utils import _sync_dashboard_slots, effective_session_key
from kiro_crew.dashboard.state import (
    _normalize_slot_key,
    durable_row_count,
    note_crew_log_class,
    row_mid,
)
from kiro_crew.history import carry_provenance, is_incognito_transcript
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.messaging.link import channel_namespace_of, is_channel_session_key
from kiro_crew.messaging.upload_gate import live_dashboard_slot
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

#: Most conversations one explicit backfill click files. THIS module owns the
#: bound: the endpoint and the panel read it from here rather than restating a
#: number of their own.
#:
#: A channel with years of history can hold thousands of sessions and each move
#: takes that transcript's cross-process lock, so an unbounded pass would answer
#: the click with a request that runs for minutes and may time out having done
#: an unknown fraction of the work. Capping instead makes the partial state the
#: normal state and reportable: the response says how many are left, the button
#: stays available, and a second click continues. Filing is idempotent, so
#: clicking again can only ever pick up conversations the previous click did not
#: reach.
BACKFILL_MOVE_LIMIT = 200


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


def project_channel_turn_live(
    dashboard_state: Any,
    session_key: str,
    user_text: str,
    reply_text: str,
    *,
    broadcast_user: bool = False,
) -> tuple[str, str] | None:
    """Append one resumed turn to its open dashboard slot and return both row ids.

    This is loop-side by contract: user then assistant append without an await between
    them, so the live window observes one ordered pair. ``broadcast_user`` is explicit
    because Telegram has no optimistic dashboard copy for its channel-originated row,
    while Discord's established projection path does not broadcast that row.
    """
    slot = live_dashboard_slot(dashboard_state, session_key)
    if slot is None:
        return None
    # These rows are a channel conversation's words landing in a dashboard
    # conversation's context, so the slot is marked before the first append
    # (see ``_ChatSlot._channel_turn_seen``). This projection bypasses the queue
    # and the runner, the mark's other two writers.
    slot._channel_turn_seen = True
    try:
        user_mid = (
            row_mid(
                slot.append(
                    "user",
                    user_text,
                    "msg msg-u",
                    broadcast_user=broadcast_user,
                )
            )
            or ""
        )
    except Exception:
        logger.debug(
            "channel turn projection: user append failed for %s", session_key, exc_info=True
        )
        return None

    assistant_mid = ""
    if reply_text:
        try:
            assistant_mid = row_mid(slot.append("assistant", reply_text, "msg msg-a")) or ""
        except Exception:
            logger.debug(
                "channel turn projection: assistant append failed for %s",
                session_key,
                exc_info=True,
            )

    push = getattr(dashboard_state, "push_slots_update", None)
    if callable(push):
        try:
            push()
        except Exception:
            logger.debug("channel turn projection: slot push failed", exc_info=True)
    return user_mid, assistant_mid


def project_channel_row_live(
    dashboard_state: Any, session_key: str, role: str, text: str, cls: str
) -> str | None:
    """Append one extra row to the open dashboard slot and return its row id.

    The channel-side twin of the dashboard runner's own turn-outcome cards: a
    channel turn that closed with no assistant text mirrors the driver's notice
    (``notice`` / ``msg msg-info``) into the live window, and one that raised
    mirrors its error (``error`` / ``msg msg-err``), so the reader there sees
    the same sentence the channel posted. Called AFTER
    :func:`project_channel_turn_live` so the row lands behind the user's (and,
    when there is one, the assistant's) in the window's order. Returns ``None``
    when no live slot owns the session, or when the append failed, so the caller
    persists the row under a freshly minted id instead.
    """
    slot = live_dashboard_slot(dashboard_state, session_key)
    if slot is None:
        return None
    # A channel turn's outcome row: same mark as the turn it closes, so this entry
    # point is sufficient on its own (``_ChatSlot._channel_turn_seen``).
    slot._channel_turn_seen = True
    try:
        mid = row_mid(slot.append(role, text, cls)) or ""
    except Exception:
        logger.debug(
            "channel turn projection: %s append failed for %s", role, session_key, exc_info=True
        )
        return None
    push = getattr(dashboard_state, "push_slots_update", None)
    if callable(push):
        try:
            push()
        except Exception:
            logger.debug("channel turn projection: slot push failed", exc_info=True)
    return mid or None


async def rename_channel_title_live(
    dashboard_state: Any,
    session_key: str,
    title: str,
) -> bool:
    """Rename the open dashboard slot for *session_key* and keep every view aligned.

    Returns ``False`` when no live slot owns the session, so the channel can fall
    back to its conversation-log-only path. A live slot is authoritative once it
    exists: changing only transcript metadata lets its later save rewrite the old
    in-memory title over the new one. Match the dashboard's own manual-rename
    ordering instead — update the slot synchronously, bump its title epoch so a
    background titler stands down, persist through the epoch-aware helper, then
    broadcast the exact value every dashboard client must render.

    ``_persist_title`` is best-effort by dashboard contract. A failed immediate
    metadata write leaves the updated live slot authoritative and a later slot
    save can recover it; the dashboard's own rename endpoint makes the same trade.
    """
    slot = live_dashboard_slot(dashboard_state, session_key)
    if slot is None:
        return False

    slot.title = title
    slot._titled = True
    slot._title_origin = "user"
    slot._title_epoch = int(getattr(slot, "_title_epoch", 0)) + 1
    persisted = await _persist_title(dashboard_state, slot)
    if not persisted:
        logger.warning("channel title update is live but not yet durable for %s", session_key)

    push_title = getattr(dashboard_state, "push_slot_title", None)
    if callable(push_title):
        push_title(slot.key, title)
    else:
        push_slots = getattr(dashboard_state, "push_slots_update", None)
        if callable(push_slots):
            push_slots()
    return True


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


def needs_backfill_filing(meta: dict[str, Any]) -> bool:
    """True when an EXPLICIT backfill may file this conversation.

    :func:`needs_default_filing` minus its ``channel_origin`` clause, and that
    single difference IS the feature. Automatic first-surface filing has to
    refuse a conversation the dashboard already saved, because from the record
    alone it cannot tell "never filed" from "filed, and then moved to the top
    level by the user" -- and the safe reading of an ambiguous record is to
    leave the placement alone. That refusal is why switching the setting on does
    nothing for conversations that already existed.

    A user clicking a button that names the folder is not ambiguous, so this
    guard keeps only the two records that actually mean "the user has placed
    this", and drops the one that merely means "this conversation predates the
    setting":

    * ``folder_id`` -- it is in a folder right now. Filing it would move it OUT
      of wherever the user put it, and this button offers to fill one folder,
      not to rearrange the sidebar.
    * ``channel_folder_filed`` -- filing already ran here. With no ``folder_id``
      beside it that means exactly one thing: it was filed, and then deliberately
      moved to the top level. Re-filing would undo that.

    What is left eligible is precisely the population this addresses: surfaced
    while filing was off, so it carries ``channel_origin`` and neither of the
    other two.

    Used as the ``update_metadata_if`` guard as well as the pre-scan filter, so
    the decision is re-made against the locked on-disk record at the moment of
    the write -- a placement made while the scan ran wins.

    It is deliberately not the WHOLE write decision. An empty record passes here,
    which at scan time means "nothing has placed this" and is correct, but under
    the write lock the same value also describes a conversation that has been
    DELETED -- the store's metadata read cannot tell those apart. Existence is
    therefore asked separately, by the write's ``require_existing`` flag, so that
    one reading of an empty dict does not have to serve both questions.
    """
    return not (meta.get("folder_id") or meta.get("channel_folder_filed"))


def _rebind_unbound_channel_slot(
    state: "DashboardState", slot: "_ChatSlot", session_key: str
) -> bool:
    """Bind *slot* to *session_key* when it is an unbound channel survivor.

    Returns True when a binding was applied.

    A slot surfaced before the session map could answer for its stem holds the
    history but routes nothing back, so the tab is one-way until a human
    re-links it. The map answer is the trusted one, so the first pass that can
    resolve the stem heals it.

    The provenance check is not implied by the slot NAME: any caller can create
    a slot named for a live channel stem, and binding on the name alone would
    route that tab's later turns into the channel's conversation.

    ``channel_origin`` alone is not enough either. It round-trips through the
    transcript's own metadata line, so an agent able to write that file can hand
    a lookalike the marker and the restore would arrive already claiming it. The
    rebind therefore also requires the runtime record that THIS process surfaced
    the slot from a channel session it observed.

    That scopes the heal to the observing process. A restored survivor is bound
    by ``get_or_create_slot``'s own resolve as it rehydrates, but only when the
    session map can answer for the stem right then; a slot rehydrated while it
    still cannot carries no runtime record, so it stays one-way for the rest of
    the process even once the stem resolves, and the next start retries that
    resolve. The window is the deliberate price of provenance that a writable
    marker cannot supply.
    """
    if not slot.channel_origin or slot.linked_session_key:
        return False
    if not slot._channel_runtime_origin:
        return False
    if not session_key or not is_channel_session_key(session_key):
        return False
    slot.linked_session_key = session_key
    note_crew_log_class(state, slot)
    # Flagged, or the periodic flush skips it and the next restart refuses all over again.
    slot._dirty = True
    # The rebind changes the slot's effective key, so the registry still holds the
    # unbound phantom -- and every "does this session have a tab?" gate reads it.
    # Neither rebind path increments the reconciler's surfaced count, so the
    # republish cannot live in its sync gate.
    _sync_dashboard_slots(state)
    return True


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
        # Covers the same-pass creation race ONLY. The reconciler never re-passes an
        # existing slot here, so a survivor from an earlier pass is healed in
        # _reconcile_channel_slots_locked instead.
        if _rebind_unbound_channel_slot(state, state._slots[slot_name], session_key):
            logger.info("channel surface: rebound previously unbound slot %s", slot_name)
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

    # Reached only for a stem ``list_sessions`` just served, so the conversation was
    # observed rather than claimed. This is the record a later rebind trusts; the
    # persisted marker above cannot serve, being writable by whoever holds the file.
    slot._channel_runtime_origin = True

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
    slot._memory_assignment_from_history = True
    if meta.get("model"):
        slot.model = meta["model"]
    # `jev_route` is deliberately NOT read back here, for the reason the two
    # persistence loaders state: it records an owner pick that spends money, and
    # this file is editable by the agent's own tools.
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
    if meta.get("memory_store"):
        slot.memory_store = str(meta["memory_store"])
    if meta.get("project"):
        slot.project = meta["project"]
    if meta.get("channel_folder_filed"):
        slot._channel_folder_filed = True
    # Persisted tags are applied on EVERY surface, not just first filing: the
    # filing write stores the inherited tags atomically with the filing marker
    # (see reconcile), so a restore after a crash — where the marker exists but
    # the slot never saved — recovers them from here. Validation is the shared
    # authority-aware helper, matching the three sibling restore paths: it
    # fails OPEN when the vocabulary is unreadable (_tags_authoritative=False),
    # because pruning against an unknown vocabulary would drop every id, the
    # next save would persist the loss, and the sticky filing marker blocks
    # re-inheritance forever. Imported locally to avoid the module cycle
    # through chat_persistence (chat_tags → chat_persistence → this module).
    from kiro_crew.dashboard.chat_tags import validate_folder_tag_ids

    tags_changed = False
    for tid in validate_folder_tag_ids(meta.get("tags"), state):
        if tid not in slot.tags:
            slot.tags.append(tid)
            tags_changed = True
    if meta.get("folder_id"):
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
        slot.folder_id = folder_id
        slot._channel_folder_filed = True
        # First filing = this chat's birth into the folder: copy the folder's
        # tags by value, the same creation-only inheritance the dashboard
        # slot-create path applies. The restore branch above deliberately does
        # not — a persisted folder_id means the filing already happened.
        for tid in folder_tags or []:
            if tid not in slot.tags:
                slot.tags.append(tid)
                tags_changed = True
    # "tags changed => revision changed": the slot was constructed with an empty
    # list under its birth revision, and a concurrent slots GET may already have
    # snapshotted that; the surfaced list must carry a revision of its own.
    if tags_changed:
        bump_revision = getattr(slot, "bump_tags_revision", None)
        if callable(bump_revision):
            bump_revision()
    if meta.get("pinned"):
        slot.pinned = True

    # Same shape as restore_recent_sessions: window the tail, count the rest as
    # the frozen prefix a save never rewrites, and redact assistant content at
    # the read boundary.
    _rebuild_window(slot, messages)
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
    return bool(slot.linked_session_key) and not slot.turn_running and not slot._dirty


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
    # Unbound survivors. A slot surfaced before the session map could answer for its
    # stem is in NEITHER list above -- `pending` excludes a slot that already exists,
    # and `_window_refresh_is_safe` rejects one with no linked key -- so without this
    # bucket the tab stays one-way for the process lifetime even once the stem
    # resolves. Needs no transcript read, so the steady state stays a metadata scan.
    rebindable: list[tuple[str, "_ChatSlot"]] = []
    if state.sessions:
        for s in eligible:
            key = s.get("key", "")
            slot = state._slots.get(channel_slot_name(key))
            if slot is None or slot.linked_session_key or not slot.channel_origin:
                continue
            resolved = state.sessions.channel_key_for_stem(key)
            if resolved:
                rebindable.append((resolved, slot))
    if not pending and not refreshable and not rebindable:
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

    rebound = 0
    for resolved, slot in rebindable:
        # Re-checked inside the helper: a turn on either surface may have bound the
        # slot while this pass's reads were in flight.
        if _rebind_unbound_channel_slot(state, slot, resolved):
            rebound += 1
            logger.info("channel reconcile: rebound previously unbound slot %s", slot.key)

    if surfaced or refreshed or rebound:
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


def _live_slot_placement(state: "DashboardState", key: str) -> tuple[Any, str]:
    """Return the open tab for session *key* and the folder it currently shows.

    The in-memory placement is consulted because it can be AHEAD of disk: a
    folder the user just dragged the tab into is set on the slot immediately and
    saved asynchronously, so between their drag and that save the record still
    reads unfiled. Filing on the strength of the record alone would then move a
    conversation the user placed one second earlier, and the guard on the write
    cannot catch it -- the guard reads the same not-yet-updated record.
    """
    slot = state._slots.get(channel_slot_name(key))
    if slot is None:
        return None, ""
    return slot, str(getattr(slot, "folder_id", "") or "")


def _clean_title(raw: object) -> str:
    """A conversation title safe to echo back into the dashboard.

    Titles are generated from channel message content, so on this boundary they
    are untrusted text exactly as transcript content is. Delegates to
    :func:`_redact_assistant` rather than calling the two redactors here: they
    are order-dependent (exfiltration URLs before credentials) and one
    transcript must not have two redaction policies.
    """
    if not isinstance(raw, str) or not raw.strip():
        return ""
    return _redact_assistant(raw.strip())


async def backfill_channel_folder(state: "DashboardState", namespace: str) -> dict[str, Any]:
    """File *namespace*'s already-existing unfiled conversations into its folder.

    The explicit counterpart to the automatic filing in
    :func:`_reconcile_channel_slots_locked`: that one files conversations as they
    are first surfaced, which leaves every conversation that existed BEFORE the
    setting was switched on untouched, with no affordance anywhere to catch them
    up. This is that affordance.

    Reached only from a button the user clicks, never from a timer and never as a
    side effect of saving settings, so nothing here moves a conversation without
    an instruction that named the folder. Two independent reasons for that shape
    rather than one: a settings save fires on every unrelated field too (a
    token-only save would silently bulk-move conversations), and the moves are
    not collectively undoable, so the user has to be the one asking.

    Returns a report, not a count. Because there is no undo, the response names
    every conversation it moved -- that list is what lets the user put one back
    by hand, and it is the only record of what happened.

    ``reason`` distinguishes the ways this can do nothing, which a count of zero
    cannot: ``not_configured`` (the setting is off for this channel),
    ``folder_missing`` (configured, but no folder answered to that name when the
    pass started -- never created, or a hand-edited config), ``folder_gone`` (the
    folder was there and was DELETED while the pass ran), ``unavailable`` (the
    conversation store is absent or its listing raised, so nothing was even
    attempted), ``all_failed`` (every attempted write failed), and ``""`` for a
    pass that actually ran.

    ``folder_missing`` and ``folder_gone`` are deliberately NOT one value, for the
    same reason ``unavailable`` and ``all_failed`` are not: the remedies are
    opposite. Saving the settings creates a folder that was never there, and does
    nothing for conversations already stamped with the id of one that has been
    deleted -- recreating mints a fresh id, so those are stranded and have to be
    moved by hand. A single value made the panel infer which case it had from
    ``failed``/``moved``, which is a claim about this function's control flow made
    in the client.

    ``unavailable`` and ``all_failed`` are deliberately NOT one value. They need
    opposite sentences: one says a read failed, the other says the reads worked
    and the writes did not, and only the second has a count worth reporting.
    """
    # Exactly the five things a caller reads. The report IS the response body
    # (``web.json_response(report)``), so a key nothing renders is wire weight
    # that still has to be kept true on every path through this function.
    report: dict[str, Any] = {
        "folder_name": "",
        "moved": [],
        "reason": "",
        "remaining": 0,
        "failed": 0,
    }
    if namespace not in CHANNEL_CONFIG_SECTIONS:
        report["reason"] = "not_configured"
        return report
    log = state.conversation_log
    if log is None:
        report["reason"] = "unavailable"
        return report

    # Off-loop: a config read. Asked separately from ``lookup_channel_folder``
    # (which also reads it) only because that function answers "" for BOTH a
    # channel with the setting off and a configured folder that is absent, and
    # the panel needs to say different things about those.
    folder_name = await asyncio.to_thread(configured_folder_name, namespace)
    if not folder_name:
        report["reason"] = "not_configured"
        return report
    report["folder_name"] = folder_name
    # Resolved from the name captured just above, NOT by calling
    # ``lookup_channel_folder``, which would read config a second time. Two reads
    # are two chances to observe different values: a settings save moving this
    # channel from folder A to folder B between them yields A's name and B's id,
    # and since the receipt reports the name while every write uses the id, the
    # user would be told A while every session landed in B.
    folder_id = await folder_id_for_name(state, namespace, folder_name)
    if not folder_id:
        report["reason"] = "folder_missing"
        return report

    loop = asyncio.get_running_loop()
    try:
        sessions = await loop.run_in_executor(None, log.list_sessions)
    except Exception:
        logger.warning("channel backfill: list_sessions failed for %s", namespace, exc_info=True)
        report["reason"] = "unavailable"
        return report

    candidates = [s for s in sessions if channel_namespace_of(s.get("key", "")) == namespace]
    if not candidates:
        return report

    def _load_meta() -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for s in candidates:
            key = s.get("key", "")
            if not key or key in out:
                continue
            try:
                out[key] = log.get_metadata(key)
            except Exception:
                # An unreadable record is not evidence that filing is wanted.
                # ``{}`` would pass the guard here and then fail it again under
                # the lock, so the write is refused either way -- this only keeps
                # the pre-scan count honest.
                out[key] = {}
        return out

    metadata = await loop.run_in_executor(None, _load_meta)

    eligible: list[dict[str, Any]] = []

    for s in candidates:
        key = s.get("key", "")
        meta = metadata.get(key) or {}
        # Ephemeral stays ephemeral: an incognito/temporary thread must not gain
        # a durable placement, the same rule ``eligible_channel_sessions``
        # applies when deciding what may become a tab at all.
        if any(is_incognito_transcript(m) for m in (meta.get("memory_mode"), s.get("memory_mode"))):
            continue
        if not needs_backfill_filing(meta) or _live_slot_placement(state, key)[1]:
            continue
        eligible.append(s)

    # Newest first, so a run that hits the cap files the conversations the user
    # is most likely looking for rather than an arbitrary slice.
    eligible.sort(key=lambda s: float(s.get("modified", 0) or 0), reverse=True)
    if len(eligible) > BACKFILL_MOVE_LIMIT:
        report["remaining"] = len(eligible) - BACKFILL_MOVE_LIMIT
        eligible = eligible[:BACKFILL_MOVE_LIMIT]

    # Drop every record this pass will not write. `metadata` is read again far
    # below, for the receipt titles, so it outlives the scan -- and without this it
    # outlives it holding the WHOLE namespace while the loop takes up to
    # BACKFILL_MOVE_LIMIT locks and awaits that many writes. Bounding it here makes
    # the long-lived set proportional to what the cap allows, not to the history.
    #
    # It does NOT bound the scan's own peak: counting `remaining` exactly means
    # reading every candidate's metadata first, and that is the trade this keeps
    # (an exact count over a lower peak) rather than hides.
    keep = {s.get("key", "") for s in eligible}
    metadata = {k: v for k, v in metadata.items() if k in keep}

    # Imported locally to avoid the module cycle through chat_persistence
    # (chat_tags -> chat_persistence -> this module), matching the reconcile and
    # surface paths.
    from kiro_crew.dashboard.chat_tags import tags_write_lock, validate_folder_tag_ids

    def _read_folder_tags(folders: list[dict[str, Any]], fid: str = folder_id) -> list[str] | None:
        """This folder's raw tag ids, or ``None`` when it is absent.

        That distinction is load-bearing and ``[]`` cannot carry it: a folder
        that exists carrying no tags and a folder that has been DELETED both have
        no tags to read, and only one of them may be filed into. Stamping a dead
        ``folder_id`` alongside the filing marker is the worst outcome this
        function can cause -- the conversation ends up in no folder AND
        permanently ineligible for a later backfill, because the marker is what
        :func:`needs_backfill_filing` refuses on. There is no recovery path for
        that, so the read has to be able to say "gone".
        """
        for f in folders:
            if f.get("id") != fid:
                continue
            raw = f.get("tags")
            return list(raw) if isinstance(raw, list) else []
        return None

    slots_touched = False
    write_failures = 0
    for s in eligible:
        key = s.get("key", "")
        # Re-read the folder inside the lock for EVERY write, which is the
        # reconcile path's discipline rather than a cheaper read-once: a folder
        # PATCH, a tag deletion, or the folder's own DELETION committing partway
        # through this pass must be visible to the conversations filed after it,
        # and one long hold across every write would block dashboard tag edits for
        # the whole run.
        try:
            async with tags_write_lock(state):
                raw_folder_tags = await state.read_folders(_read_folder_tags)
                if raw_folder_tags is None:
                    # The folder was deleted while this pass ran. Stop rather than
                    # skip: every remaining conversation would hit the same
                    # missing folder, and the whole point of stopping HERE is that
                    # nothing gets stamped with a dead id.
                    #
                    # Its OWN reason, not the `folder_missing` the lookup above
                    # reports. Those two need opposite sentences -- one folder was
                    # never created and saving the settings creates it, the other
                    # existed a moment ago and anything already filed into it is
                    # now stranded -- and one value for both left the panel
                    # inferring which it was from `failed > 0`, an inference about
                    # this function's internals made a layer away.
                    report["reason"] = "folder_gone"
                    break
                inherited = validate_folder_tag_ids(raw_folder_tags, state)
                filing_meta: dict[str, Any] = {
                    "folder_id": folder_id,
                    "channel_folder_filed": True,
                }
                if inherited:
                    # UNION, never replace. The store merges with
                    # ``metadata.update(fields)``, so a bare list under ``tags``
                    # overwrites the whole key -- and this path reaches records the
                    # automatic one never does. A conversation surfaced while
                    # filing was off, tagged by the user and never filed, with its
                    # tab closed, passes this guard (which reads placement, not
                    # tags) and has no live slot to union the tags back through;
                    # replacing would destroy them with nothing recording what they
                    # were.
                    #
                    # Read FRESH rather than from the pre-scan snapshot: the
                    # snapshot predates this pass's awaits, so unioning it would
                    # also resurrect a tag the user removed in between. A fresh
                    # read honours both their additions and their removals, and
                    # leaves only the lock-acquisition window.
                    #
                    # Fails CLOSED on an unreadable record -- no ``tags`` key at
                    # all, so the write cannot touch them. Inheriting a folder's
                    # tags is a convenience; losing the user's is not recoverable,
                    # so the two are not weighed equally.
                    try:
                        current = await asyncio.to_thread(log.get_metadata, key)
                        existing = [t for t in (current.get("tags") or []) if isinstance(t, str)]
                        merged = list(dict.fromkeys([*existing, *inherited]))
                        # Omitting an unchanged key keeps the write off ``tags``
                        # entirely when the folder adds nothing new.
                        if merged != existing:
                            filing_meta["tags"] = merged
                    except Exception:
                        logger.warning(
                            "channel backfill: could not read tags for %s; not inheriting",
                            key,
                            exc_info=True,
                        )
                # Re-check the open tab after the awaits above, not just in the
                # pre-scan: the user can drag this conversation into a folder
                # while the pass runs, and until their save lands the record the
                # guard reads still says unfiled.
                slot, placed = _live_slot_placement(state, key)
                if placed:
                    continue
                # ``require_existing`` because this pass's gap between reading a
                # candidate and writing it is the widest in the feature -- up to
                # BACKFILL_MOVE_LIMIT lock acquisitions and awaits -- and a
                # conversation deleted inside that gap reads to the guard exactly
                # like one that never had a metadata line. Without it the merge
                # upserts and the deletion is undone as a metadata-only stub
                # filed into the folder, with no transcript behind it: a row in
                # the sidebar that opens onto nothing, and there is no undo for
                # this button to walk it back.
                filed = await asyncio.to_thread(
                    log.update_metadata_if,
                    key,
                    filing_meta,
                    needs_backfill_filing,
                    require_existing=True,
                )
                # Mirror the persisted placement onto the open tab, INSIDE the
                # lock and against a freshly read slot. Without this the tab keeps
                # showing the conversation at the top level until a restart
                # re-reads the record, so the button would look inert on the very
                # conversations the user is watching.
                #
                # Re-read rather than reuse the `slot` above: the write is an
                # await, and a drag landing during it sets the slot's folder in
                # memory before its own save lands. Mirroring the value read
                # BEFORE that await would revert the user's move, and the guard on
                # the write cannot catch it because it reads the record their save
                # has not reached yet.
                if filed:
                    slot, placed_now = _live_slot_placement(state, key)
                    if slot is not None and not placed_now:
                        slot.folder_id = folder_id
                        slot._channel_folder_filed = True
                        tags_changed = False
                        for tid in inherited:
                            if tid not in slot.tags:
                                slot.tags.append(tid)
                                tags_changed = True
                        if tags_changed:
                            bump_revision = getattr(slot, "bump_tags_revision", None)
                            if callable(bump_revision):
                                bump_revision()
                        slots_touched = True
        except Exception:
            # One conversation failing is not a reason to abandon the rest, and
            # nothing partial is left behind: the metadata write is atomic, so it
            # either landed or it did not. Counted rather than only logged,
            # because a swallowed failure with an empty `moved` list otherwise
            # reports as "nothing needed moving" to a user whose conversations are
            # all still unfiled.
            logger.warning("channel backfill: could not file %s", key, exc_info=True)
            write_failures += 1
            continue
        if not filed:
            # Two refusals reach here and neither is retried. The guard saw a
            # placement or a filing marker that landed while this pass ran --
            # the user's own action, so it stands. Or the conversation was
            # DELETED while this pass ran, in which case there is nothing left
            # to file. Neither is counted as a failure: both are decisions, not
            # errors, and no write was attempted.
            continue
        report["moved"].append(
            {
                "key": key,
                "title": _clean_title((metadata.get(key) or {}).get("title")),
                "label": channel_label(key),
            }
        )

    # A conversation whose write failed is still unfiled, so it belongs in the
    # count that invites another click.
    # Counted into ``remaining`` because the user's next click should retry them,
    # and reported SEPARATELY because "still unfiled" reads as routine batching:
    # without this, a run whose writes keep failing is indistinguishable from a
    # capped run, and the user clicks again forever against the same error.
    report["remaining"] += write_failures
    report["failed"] = write_failures
    if write_failures and not report["moved"] and not report["reason"]:
        # Every attempt failed. Reporting an empty `moved` with no reason would
        # render as "nothing to move", which is the opposite of what happened.
        #
        # Its OWN reason rather than `unavailable`: the store was read fine, so a
        # message about failing to read the session history names the wrong cause,
        # and this is the one refusal that has a count the user can act on.
        report["reason"] = "all_failed"

    if slots_touched:
        state.push_slots_update()
    return report


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
