"""When dashboard state reaches websocket clients, and how.

Suspend, debounce and trailing flush of the slots broadcast, the serving loop,
audiences, slot / lineage / removal patches, card and progress pushes, and the
websocket fan-out delegations.

Instance fields of ``DashboardState`` this module reads and writes:

Writes: ``_lineage_sent``, ``_serving_loop``, ``_slots_broadcast_interval``,
``_slots_broadcast_last``, ``_slots_broadcast_timer``, ``_slots_push_all_owed``,
``_slots_push_legacy_owed``, ``_slots_push_overlapped``, ``_slots_push_pending``,
``_slots_push_suspend``, ``_update_progress``.
Reads only: ``_dynamic_cards``, ``_folders``, ``_notify_event``, ``_owner_ws_clients``,
``_slots``, ``_slots_broadcast_lock``, ``_slots_under_construction``, ``_sse_queues``,
``_ws_clients``, ``channel_manager``.

A function whose first parameter is ``self`` is a ``DashboardState`` method: the
class binds it under the same name (``state_parts/dashboard_state.py``), so it reads
and writes the state's own fields and calls its siblings through ``self``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any, Callable

from aiohttp import web

from kiro_crew.dashboard.state_parts.dashboard_services import _persistence_for, _websocket_for
from kiro_crew.dashboard.state_parts.sidebar_stores import _safe_folder_tree
from kiro_crew.dashboard.state_parts.slots_frame import (
    _SLOTS_BROADCAST_INTERVAL_S,
    _attach_slot_parents,
    _slots_broadcast_interval_for,
    _slots_serialization_note,
    _slots_ws_frame,
)
from kiro_crew.dashboard.state_parts.transcript_rows import chat_message_frame
from kiro_crew.dashboard.websocket_hub import SLOT_PATCH_WS_FLAG, WsPayload

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot
    from kiro_crew.dashboard.state_parts.dashboard_state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.state")


#: Fields whose dashboard-user projection is identical for every slot-patch
#: audience. Per-audience fields such as ``source_links`` require a full frame.
_SLOT_PATCH_FIELDS = frozenset({"pinned", "mutes_opened", "muted", "title", "folder_id"})


def _new_card_store(state: Any) -> Any:
    """*state*'s dynamic-card store, constructed on first use. THE one constructor.

    A module-level function rather than a method, because both callers need it and one of them
    (``set_dynamic_cards_enabled``) is exercised against bare stub objects that carry only
    ``_dynamic_cards`` -- a sibling method call would not resolve on those.

    The import stays INSIDE the function: it is the deferred one that keeps the card lifecycle
    off the gateway's boot stack, and a test asserts the module is not imported until a caller
    actually needs a store.
    """
    if state._dynamic_cards is None:
        from kiro_crew.dashboard.card_lifecycle import CardLifecycle

        state._dynamic_cards = CardLifecycle(state)
    return state._dynamic_cards


def _slots_broadcast_clock() -> float:
    """The one clock the slots coalescing window is measured on.

    Every read that stamps or measures the window goes through here, so a test
    can drive the window on a fake clock without replacing ``time.monotonic``
    for the event loop and every other caller in the process.
    """
    return time.monotonic()


# A successful plain persistent-memory create hands its full-list publication past
# the HTTP response by this fixed interval. Callers may name the operation only.
_DEFERRED_SLOTS_FLUSH_DELAY_S: float = 0.01


@contextlib.contextmanager
def suspend_slots_push(self: DashboardState) -> "Iterator[Callable[[str], None]]":
    """Coalesce every ``push_slots_update()`` inside the block into one at exit.

    ``get_or_create_slot`` broadcasts the FULL slot list on each call, so a bulk
    restore of N tabs serializes 1+2+…+N slots — O(N²) ``to_dict``/redaction
    work for intermediate states no client will ever render (measured ~1.3s at
    N=77, and it grows quadratically). Wrap the restore, emit one broadcast.

    The yielded callback accepts only an operation label and lets a successful
    request move the coalesced flush by the state-owned fixed delay past its
    response. Existing callers ignore it and retain the synchronous flush. A
    deferred callback that fires during an active suspension transfers its
    publication debt to that suspension rather than publishing inside it. If
    suspensions overlap before the debt reaches depth zero, the outermost exit
    publishes synchronously: one request cannot delay another context's
    publication contract.

    Depth-counted so nested use is safe (an inner block must not flush early),
    and ``@contextmanager``'s try/finally unwinds the depth even if the body
    raises. Only flushes if something actually asked to push. A flush that
    itself fails while the body's own exception is unwinding annotates its
    exception (`PEP 678`) so the buried original stays visible — the flush's
    exception otherwise replaces the body's in the caller's view. A deferred
    flush is honored only after a clean body exit; every exception keeps the
    old synchronous failure propagation.
    """
    deferred_operation: str | None = None

    def defer_flush(operation: str = "slots update") -> None:
        nonlocal deferred_operation
        deferred_operation = operation

    self._slots_push_suspend += 1
    if self._slots_push_suspend > 1:
        self._slots_push_overlapped = True
    try:
        yield defer_flush
    finally:
        self._slots_push_suspend -= 1
        overlapped = self._slots_push_overlapped
        if self._slots_push_suspend == 0:
            self._slots_push_overlapped = False
        if self._slots_push_suspend == 0 and self._slots_push_pending:
            self._slots_push_pending = False
            # Captured BEFORE the flush call: inside the `except` block below,
            # sys.exc_info() would already name the flush's own exception.
            unwinding_over = sys.exc_info()[1]
            if deferred_operation is not None and unwinding_over is None and not overlapped:
                loop = self.serving_loop
                if loop is not None and loop is self._running_loop():
                    try:
                        loop.call_later(
                            _DEFERRED_SLOTS_FLUSH_DELAY_S,
                            self._deferred_slots_flush,
                            deferred_operation,
                            1,
                        )
                        lock = self._slots_broadcast_lock
                        if lock is not None:
                            with lock:
                                if self._slots_broadcast_timer is not None:
                                    self._slots_broadcast_timer.cancel()
                                    self._slots_broadcast_timer = None
                        return
                    except RuntimeError:
                        # A loop closing between lookup and scheduling cannot
                        # drop the announcement. Fall back to the old immediate
                        # flush and let its failure retain caller visibility.
                        pass
            try:
                self.push_slots_update()
            except BaseException as flush_exc:
                if unwinding_over is not None and unwinding_over is not flush_exc:
                    flush_exc.add_note(
                        "[slots-flush] the owed slots flush raised while unwinding "
                        f"over an in-flight {type(unwinding_over).__name__}; that "
                        "original exception is chained below as __context__"
                    )
                raise


def _deferred_slots_flush(self: DashboardState, operation: str, retries_remaining: int) -> None:
    """Publish one fresh snapshot, retrying a deferred failure once.

    This deliberately bypasses the leading/trailing coalescer at depth zero:
    returning from ``push_slots_update`` can mean that a bare trailing callback
    owns the real serialization, which would put its exception back under
    asyncio's generic callback handler. During an active suspension, however,
    the outermost context owns the safe flush, so transfer the publication debt
    to it through ``push_slots_update``. Before each direct attempt, an armed
    trailing callback is canceled under the coalescing lock and the new window
    is stamped; a failed attempt therefore leaves only its bounded retry armed.
    Each attempt re-serializes current state, so the retry reconciles every
    mutation that landed in the meantime.
    """
    if self._slots_push_suspend:
        self.push_slots_update()
        return

    try:
        lock = self._slots_broadcast_lock
        if lock is not None:
            with lock:
                if self._slots_broadcast_timer is not None:
                    self._slots_broadcast_timer.cancel()
                    self._slots_broadcast_timer = None
                self._slots_broadcast_last = _slots_broadcast_clock()
        self._do_slots_broadcast()
        return
    except Exception:
        logger.error(
            "Deferred slots publication failed for %s; retries remaining=%d",
            operation,
            retries_remaining,
            exc_info=True,
        )

    if retries_remaining <= 0:
        return
    loop = self.serving_loop
    if loop is None or loop is not self._running_loop():
        logger.error(
            "Deferred slots publication retry could not be scheduled for %s: "
            "serving loop is unavailable",
            operation,
        )
        return
    try:
        loop.call_later(
            _SLOTS_BROADCAST_INTERVAL_S,
            self._deferred_slots_flush,
            operation,
            retries_remaining - 1,
        )
    except RuntimeError:
        logger.error(
            "Deferred slots publication retry could not be scheduled for %s: "
            "serving loop is closing",
            operation,
            exc_info=True,
        )


def push_slots_update(self: DashboardState, *, legacy_only: bool = False) -> None:
    """Push slots, keeping provider status confined to owner websockets.

    Coalesces on a leading plus trailing edge: the first call after an idle
    period broadcasts immediately, further calls inside the window are
    absorbed, and one trailing broadcast carries the final state. A single
    chat turn fires several of these and each one re-serializes every slot,
    so an uncoalesced burst redraws the whole sidebar once per mutation for
    what the user sees as one change. The trailing flush re-serializes at
    delivery time, so a coalesced frame is never a stale frame.

    The window is 200 ms for a small list and stretches with the size of the
    last frame (see ``_SLOTS_BROADCAST_BYTES_PER_S``), so a busy fleet of a
    hundred agents is paced at roughly a fixed byte rate per socket instead of
    five full lists a second.

    ``legacy_only`` owes the full list only to consumers that cannot apply a
    ``slot_patch`` frame (SSE readers, app tokens, a tab whose bundle
    predates the frame). :meth:`push_slot_patch` and
    :meth:`push_slot_removed` use it: the patch already reached every
    socket that declared the capability, so those sockets skip this
    broadcast. An ordinary call absorbed into the same window widens the
    broadcast back to everyone.
    """
    self._complete_slot_list_rebuild_if_ready()
    lock = self._slots_broadcast_lock
    if lock is not None:
        with lock:
            if legacy_only:
                self._slots_push_legacy_owed = True
            else:
                self._slots_push_all_owed = True
    if self._slots_push_suspend:
        # Inside suspend_slots_push(); remember that a push is owed and let the
        # outermost block emit a single coalesced broadcast on exit.
        self._slots_push_pending = True
        return

    now = _slots_broadcast_clock()
    broadcast_now = False
    if lock is None:
        # Partially-constructed state (built via __new__): no coalescing.
        self._do_slots_broadcast()
        return

    with lock:
        # Resolved once here, at the top of the lock, so the timer branch
        # below and any later cross-thread caller agree on one loop.
        serving = self.serving_loop
        interval = self._slots_broadcast_interval

        elapsed = now - self._slots_broadcast_last
        if elapsed >= interval:
            self._slots_broadcast_last = now
            if self._slots_broadcast_timer is not None:
                self._slots_broadcast_timer.cancel()
                self._slots_broadcast_timer = None
            broadcast_now = True
        elif self._slots_broadcast_timer is None:
            # Scheduling onto the serving loop is preferred over broadcasting
            # from a foreign thread; a closed loop falls back to an immediate send.
            loop = serving
            remaining = interval - elapsed
            try:
                if loop is None:
                    self._slots_broadcast_last = now
                    broadcast_now = True
                elif loop is self._running_loop():
                    self._slots_broadcast_timer = loop.call_later(
                        remaining, self._trailing_slots_flush
                    )
                else:
                    loop.call_soon_threadsafe(self._schedule_trailing_flush, remaining)
            except RuntimeError:
                self._slots_broadcast_last = now
                broadcast_now = True

    # serialize_slots() and _broadcast() are the expensive half; running them
    # outside the lock stops a cross-thread caller from blocking behind them.
    if broadcast_now:
        self._do_slots_broadcast()


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """Return the running loop, or None when called off the event loop."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def bind_serving_loop(self: DashboardState, loop: asyncio.AbstractEventLoop) -> None:
    """Record the loop this dashboard is served on, before any request runs.

    Called from an app startup hook: that is the earliest point the loop
    exists, so every later reader finds it already bound instead of racing to
    latch a copy from whichever thread happens to arrive first.

    Deliberately does NOT seed the session-lineage projection. Seeding is bound to
    one store and re-runs whenever the data home changes, so a process that binds
    several states over several homes -- a test run, a pod host -- would queue one
    full cold scan per bind onto the shared maintenance pool and starve everything
    else waiting on it. The seed is requested lazily instead, by the first slots
    frame that finds the projection cold (see :func:`_attach_slot_parents`).
    """
    self._serving_loop = loop


def _schedule_trailing_flush(self: DashboardState, delay: float) -> None:
    """Arm the trailing flush. Must run ON the event loop."""
    lock = self._slots_broadcast_lock
    if lock is None:
        return
    with lock:
        if self._slots_broadcast_timer is not None:
            return
        self._slots_broadcast_timer = asyncio.get_running_loop().call_later(
            delay, self._trailing_slots_flush
        )


def _trailing_slots_flush(self: DashboardState) -> None:
    """Trailing-edge callback: broadcast whatever the state is now.

    A timer armed before a ``suspend_slots_push()`` block can expire while that
    block awaits. Publishing then would show the block's half-built state, so
    the owed push moves to the block's exit instead.
    """
    lock = self._slots_broadcast_lock
    if self._slots_push_suspend:
        if lock is not None:
            with lock:
                self._slots_broadcast_timer = None
        self._slots_push_pending = True
        return
    if lock is not None:
        with lock:
            self._slots_broadcast_timer = None
            self._slots_broadcast_last = _slots_broadcast_clock()
    self._do_slots_broadcast()


def _take_slots_audience(self: DashboardState) -> bool:
    """Consume the owed-audience flags; True when only legacy consumers are owed."""
    lock = self._slots_broadcast_lock
    if lock is None:
        return False
    with lock:
        legacy_only = self._slots_push_legacy_owed and not self._slots_push_all_owed
        self._slots_push_legacy_owed = False
        self._slots_push_all_owed = False
    return legacy_only


def _has_legacy_slots_audience(self: DashboardState) -> bool:
    """True when some consumer can only learn slot changes from a full list.

    That is every SSE reader, and every open socket that did not declare
    the ``slot_patch`` capability at connect: an app token, a companion
    window, or a tab still running a bundle from before the frame existed.
    App sockets count even when their scope would filter the list to
    nothing, because asking the scope gate here would audit a denial per
    socket per metadata edit; the cost is one serialization that the
    pre-patch protocol paid on every edit anyway.
    """
    if getattr(self, "_sse_queues", None):
        return True
    return any(
        not ws.closed and not ws.get(SLOT_PATCH_WS_FLAG, False)
        for ws in list(getattr(self, "_ws_clients", None) or ())
    )


def _do_slots_broadcast(self: DashboardState) -> None:
    """Serialize and broadcast the slot list. Bypasses coalescing."""
    from kiro_crew.dashboard.handlers.source_providers import (
        gitlab_hosts_generation,
    )
    from kiro_crew.platform.governance_profiles import (
        governance_answer_generation,
    )

    legacy_only = self._take_slots_audience()
    if legacy_only and not self._has_legacy_slots_audience():
        # Every consumer already applied the patch this broadcast was owed
        # for, so serializing the whole list would reach nobody.
        self._emit_member_slot_transitions()
        return

    yolo_active = self.is_yolo_active()  # expire first if needed
    # PUBLIC-repo chip status rides the general frame so any authenticated
    # dashboard user (SSE and WS both run on dashboard-user tokens) sees the
    # merged/closed/CI glyph for a repo whose lifecycle is already
    # world-visible. Private/unknown repos fall through to owner-only inside
    # serialize_slots, and app tokens are stripped of this status in
    # ``_serialize_for_client`` -> ``filter_slots_for_app`` before delivery,
    # so widening the general list here never leaks status to an app scope.
    # SSE carries the BARE list (see below); the WS dashboard-user frame
    # carries the enriched one. The general ``_slots_list`` feeds BOTH the SSE
    # queue (which has NO per-app filtering) and the WS frame, so enriching it
    # here leaks public-repo status onto ``/api/stream`` for any app token
    # allowed that route. Keep the broadcast list bare and put
    # the public-repo enrichment only on the WS path, where
    # ``_serialize_for_client`` re-filters app tokens.
    #
    # One serialization pass for all three audiences -- see
    # ``serialize_slot_views`` for why three passes stalled the event loop.
    owner_ws_clients = getattr(self, "_owner_ws_clients", None)
    if legacy_only and owner_ws_clients:
        owner_ws_clients = {ws for ws in owner_ws_clients if not ws.get(SLOT_PATCH_WS_FLAG, False)}
    slots_data, slots_data_ws, owner_slots = self.serialize_slot_views(owner=bool(owner_ws_clients))
    # The evidenced way this broadcast fails is a non-serializable value in
    # slot state: the dump raises, and the bare TypeError
    # names neither the slot nor the field. Serialize up front and annotate
    # the failure with the offender so one traceback is enough to find it.
    # Both coalescing branches (leading edge and trailing timer) funnel
    # through this method, so both report identically by construction.
    # Diagnosis only: the exception still propagates unchanged.
    try:
        slots_json = json.dumps(slots_data)
    except (TypeError, ValueError) as exc:
        exc.add_note(_slots_serialization_note(slots_data))
        raise
    # Size the NEXT coalescing window from this frame, so a burst over a long
    # list is paced by bytes rather than by the fixed floor. The generic list
    # stands in for every audience's copy; they differ only in per-slot
    # enrichment. Character count, not encoded bytes: close enough for pacing.
    lock = self._slots_broadcast_lock
    if lock is not None:
        with lock:
            self._slots_broadcast_interval = _slots_broadcast_interval_for(len(slots_json))
    mgr = getattr(self, "channel_manager", None)
    ch_trusted = bool(mgr and any(ch.trusted for ch in mgr._channels.values()))
    # ONE read, shared by the generic and owner frames below. Two independent
    # reads could straddle a ceiling install or a profile reload and ship two
    # different tokens for one broadcast, which would make one of the two
    # audiences invalidate while the other did not.
    # test_public_repo_status_rides_general_frame_owner_gets_full asserts the two
    # frames' governanceGeneration values are equal, so a torn read reddens it.
    # Filesystem-free by contract: governance_answer_generation is two locked
    # integer reads. The profiles directory re-stat lives in poll_profiles_fresh,
    # which only the async watcher calls, and only off the event loop.
    answer_generation = governance_answer_generation()
    held_keys = self.slot_list_held_keys()
    hold_channels = self.slot_list_hold_channels()
    # Piggyback the allowlist generation so clients invalidate the cached
    # ['dashboardConfig'] query only when the GitLab-hosts allowlist actually
    # changed -- an event-driven refresh that replaces a constant 30s poll
    # (which multiplied audit-log writes across every same-key observer).
    #
    # Piggyback the folder tree (the in-memory ``_folders`` list, WITHOUT the
    # per-folder ``history_count`` that ``GET /api/chat/folders`` computes via
    # a synchronous session scan) so the sidebar can group sessions correctly
    # on the FIRST paint. Sessions arrive on this WS frame the instant the
    # socket connects; folders otherwise arrive only via a separate HTTP GET,
    # so the sidebar would render every session ungrouped (Unfiled bucket)
    # until that GET resolved, then visibly re-shuffle them into folders. The
    # HTTP query still runs to backfill ``history_count``; grouping does not
    # wait on it. Slicing to the fields the client's grouping needs keeps
    # this hot-path frame small and never touches the filesystem.
    self._broadcast(
        {
            "_type": "slots",
            # BARE list for SSE and the generic-envelope fields below.
            "_slots_list": slots_data,
            # Enriched (public-repo status) list for the WS dashboard-user
            # frame ONLY. ``_broadcast`` builds the WS frame from this when
            # present; SSE never reads it, so status cannot reach the
            # unfiltered stream.
            "_slots_list_ws": slots_data_ws,
            "_yolo": yolo_active,
            "slots": slots_json,
            "channelTrusted": ch_trusted,
            "gitlabHostsGeneration": gitlab_hosts_generation(),
            "slotsPartial": self.slot_list_rebuilding,
            **({"slotsHeldKeys": held_keys} if held_keys else {}),
            **({"slotsHoldChannels": True} if hold_channels else {}),
            # getattr, not self._folders: this read path runs on EVERY slots
            # push, including on a __new__-built DashboardState that seeded only
            # the push essentials and never ran __init__ (several endpoint
            # suites build their fixture that way). _folders is an __init__-only
            # assignment, so a bare attribute access would AttributeError there
            # — the exact break test_push_slots_update_survives_a_partially_
            # constructed_state pins against. An absent/None folder store is an
            # empty tree.
            #
            # Lightweight states can bypass load_folders(), so keep this hot
            # broadcast path tolerant of malformed in-memory values.
            "folders": _safe_folder_tree(getattr(self, "_folders", None)),
            # Lets the client distinguish "the tree changed" from "a session
            # blinked": this frame fires on routine slot activity, so the
            # tree alone is not a change signal.
            "foldersGeneration": self.folders_generation(),
            "governanceGeneration": answer_generation,
            # Read by ``_broadcast`` to skip sockets that already applied
            # the ``slot_patch`` this broadcast was owed for.
            "_legacy_only": legacy_only,
        }
    )
    # The owner frame is the owner's ONLY slots frame — `_send_ws_all` skips
    # owner sockets for `slots` (see there for why), so this envelope has to
    # carry every key the generic one does: `folders` seeds the first-paint
    # folder tree and `gitlabHostsGeneration` drives the dashboard-config
    # invalidation, so a subset here leaves the owner without them. Both frames
    # are built by `_slots_ws_frame`, so a key cannot reach one and not the
    # other.
    if owner_ws_clients and owner_slots is not None:
        self._send_ws_owners(
            _slots_ws_frame(
                owner_slots,
                yolo=yolo_active,
                channel_trusted=ch_trusted,
                gitlab_hosts_gen=gitlab_hosts_generation(),
                folders=_safe_folder_tree(getattr(self, "_folders", None)),
                folders_gen=self.folders_generation(),
                governance_gen=answer_generation,
                slots_partial=self.slot_list_rebuilding,
                slots_held_keys=held_keys,
                slots_hold_channels=hold_channels,
            ),
            **({"skip_slot_patch_clients": True} if legacy_only else {}),
        )

    self._emit_member_slot_transitions()


def push_slot_title(self: DashboardState, key: str, title: str, *, full: bool = True) -> None:
    """Push a targeted title update for a single slot.

    By default also pushes a full slots update so the sidebar reflects the
    new title without callers needing to do both. Pass ``full=False`` for
    high-frequency streaming partials (word-by-word title reveal) to send
    only the lightweight ``slot_title`` event; finalize with a ``full=True``
    call once.
    """
    self._broadcast({"_type": "slot_title", "key": key, "title": title})
    if full:
        self.push_slots_update()


def push_slot_patch(self: DashboardState, key: str, fields: Iterable[str]) -> None:
    """Publish a metadata edit to one slot without re-sending the slot list.

    Sockets that declared the ``slot_patch`` capability receive
    ``{"type": "slot_patch", "data": {"slots": [{"key", <field>: ...}]}}``,
    a row carrying only the named fields, and merge it into their copy of
    the row. Every other consumer gets the full list through
    ``push_slots_update(legacy_only=True)``, so an old tab kept open across
    a gateway restart sees the same thing it always did.

    The values come from the dashboard-user projection of the slot, so a
    patched field reads exactly as it would in the full frame (the title is
    redacted the same way). Only fields in :data:`_SLOT_PATCH_FIELDS` are
    accepted because ``source_links`` and other per-audience fields require
    a full frame. A slot that is gone or still under construction falls back
    to an ordinary full push.
    """
    field_names = tuple(fields)
    unsupported = sorted(set(field_names) - _SLOT_PATCH_FIELDS)
    if unsupported:
        raise ValueError(f"unsupported slot patch fields: {', '.join(unsupported)}")
    slot = self._slots.get(key)
    under_construction = getattr(self, "_slots_under_construction", None) or ()
    if slot is None or key in under_construction:
        self.push_slots_update()
        return
    if self._has_legacy_slots_audience():
        self.push_slots_update(legacy_only=True)
    if not self._has_slot_patch_clients():
        return
    row = self.serialize_slot(slot, dashboard_user=True)
    patch: dict[str, Any] = {"key": key}
    for field in field_names:
        if field in row:
            patch[field] = row[field]
    self._send_slot_patch({"slots": [patch]})


def push_lineage_patch(self: DashboardState) -> None:
    """Push every live slot whose ``parent`` moved, as one ``slot_patch`` frame.

    The session tree's change event lands here (see ``CrewLogPublisher``), so the
    sidebar nests a worker the moment its crew log says who opened it -- and un-nests
    it the moment that creator closes or releases it -- without re-reading the slot
    list. Each row carries ``parent`` and ``lineage_pending`` and nothing else, the
    same two values a full frame computes for it through
    :func:`_attach_slot_parents`, so a patch and a full frame never disagree.

    Rows whose two values match what this method last sent are left out, which is
    what makes an idle tree -- or a burst that moved nothing visible -- cost no
    frame. The memory is pruned to the live keys on each pass.
    """
    rows = self._lineage_rows()
    if not rows:
        return
    _attach_slot_parents(rows, getattr(self, "spend_slot_by_session", None))
    sent = getattr(self, "_lineage_sent", None)
    if sent is None:
        sent = {}
        self._lineage_sent = sent
    live = {row["key"] for row in rows}
    for key in [k for k in sent if k not in live]:
        del sent[key]
    changed: list[dict[str, Any]] = []
    for row in rows:
        value = (row.get("parent"), bool(row.get("lineage_pending")))
        if sent.get(row["key"]) == value:
            continue
        sent[row["key"]] = value
        changed.append({"key": row["key"], "parent": value[0], "lineage_pending": value[1]})
    if not changed:
        return
    if self._has_legacy_slots_audience():
        self.push_slots_update(legacy_only=True)
    if self._has_slot_patch_clients():
        self._send_slot_patch({"slots": changed})


def push_slot_removed(self: DashboardState, key: str) -> None:
    """Publish that slot *key* left the registry without re-sending the list.

    Patch-capable sockets receive ``{"slots": [...], "removed": [key]}``.
    The ``slots`` rows re-state the ``parent`` of every row the removal MOVED,
    because a removed conductor changes its workers' ``parent`` in the full
    frame; carrying those rows keeps the sidebar's nesting identical to what a
    full list would have produced. Everyone else gets the full list, as with
    :meth:`push_slot_patch`.

    Two shapes qualify, and the second is why the test is not ``key is None``.
    A worker with no open ancestor loses its key outright. A worker that HAS one
    is re-parented onto it and carries ``ancestor``, so its key is a live string
    -- the lead's -- and a null test would leave the client holding the closed
    conductor's key, a row the same frame just removed. Every one of those
    workers would then render top-level with the orphan glyph until a full frame
    happened along, which is the behaviour this method exists to prevent.

    A key that is registered again (a same-name replacement landed while the
    close was tearing down) is not removed: the full push describes it. The
    closed slot's dashboard card is still evicted, so the replacement never
    presents a card generated for another transcript.
    """
    if key in self._slots:
        # The replacement's own card, if it has one, is its own; a card whose
        # owner is not the live slot's identity was generated for the closed
        # transcript and goes with it, so a replacement never presents its
        # predecessor's card while it has none of its own.
        if self._dynamic_cards is not None:
            entry = self._dynamic_cards.publisher.entries.get(key)
            live = getattr(self._slots[key], "_dashboard_card_identity", None)
            if entry is not None and entry.owner != live:
                self._dynamic_cards.publisher.forget(key)
            # A DERIVED card is retired on exactly the same rule and for exactly the
            # same reason: it was built for the closed transcript, so it goes with it
            # rather than being presented by the replacement as its own.
            held = self._dynamic_cards.derived.get(key)
            if held is not None and held["owner"] != live:
                self._dynamic_cards.forget_derived(key)
        self.push_slots_update()
        return
    if self._dynamic_cards is not None:
        self._dynamic_cards.publisher.forget(key)
        self._dynamic_cards.forget_derived(key)
        # The retirement STAMP goes with the slot too. It is kept so a write arriving after a
        # removal can still be ordered against it, and a slot that is definitively gone has
        # no later write to order -- so keeping it past this point retains one string per
        # slot the gateway ever hosted, for nothing. This is the only place that knows the
        # removal is definitive rather than a replacement.
        self._dynamic_cards.forget_retired(key)
    if self._has_legacy_slots_audience():
        self.push_slots_update(legacy_only=True)
    if self._has_slot_patch_clients():
        rows = self._lineage_rows()
        _attach_slot_parents(rows, getattr(self, "spend_slot_by_session", None))
        restated = [
            {"key": row["key"], "parent": row["parent"]}
            for row in rows
            if isinstance(row.get("parent"), dict)
            and (row["parent"].get("key") is None or row["parent"].get("ancestor") is True)
            and not row.get("lineage_pending")
        ]
        self._send_slot_patch({"slots": restated, "removed": [key]})
    self._emit_member_slot_transitions()


def _has_slot_patch_clients(self: DashboardState) -> bool:
    return any(
        not ws.closed and ws.get(SLOT_PATCH_WS_FLAG, False)
        for ws in list(getattr(self, "_ws_clients", None) or ())
    )


def _send_slot_patch(self: DashboardState, data: dict[str, Any]) -> None:
    """Serialize one ``slot_patch`` frame and hand it to patch-capable sockets."""
    _websocket_for(self).send_ws_slot_patch(json.dumps({"type": "slot_patch", "data": data}))


def ensure_dynamic_card_store(self: DashboardState) -> Any:
    """The card store, constructed if absent, with the MODEL path left off.

    Delegates to the module-level :func:`_new_card_store` rather than sharing a method with
    :meth:`set_dynamic_cards_enabled`, because that method is exercised against bare stub
    objects carrying only ``_dynamic_cards`` -- a sibling method call would fail on them,
    and the deferred-import assertion those tests make is about WHEN the import happens,
    which a module-level function keeps true for both callers.

    A derived card -- one the product assembles from a fold it already keeps -- costs no
    call and spends nothing from the model path's hourly budget, so it is deliberately not
    gated on the owner's opt-in to that cost. Constructing the container only inside
    :meth:`set_dynamic_cards_enabled` would make a free card's availability depend on
    TOGGLE HISTORY: never enabled means no store and therefore no card, while
    enabled-once-then-off leaves a store behind and the card appears. Same feature,
    opposite answers, decided by a switch neither answer is about.

    Constructing it here with ``enabled`` untouched keeps the model path exactly as opt-in
    as it was: no worker is started and no attempt is spent by existing.
    """
    return _new_card_store(self)


def set_dynamic_cards_enabled(self: DashboardState, enabled: bool) -> None:
    """Post-bind activation; retain the producer and its budgets across toggles."""
    if self._dynamic_cards is None and not enabled:
        return
    _new_card_store(self).set_enabled(enabled)


def notify_dashboard_card(self: DashboardState, slot: "_ChatSlot", reason: str) -> None:
    """Queue semantic work from a real event, never from a read/serialize."""
    loop = self.serving_loop
    if loop is None or loop.is_closed():
        return
    if loop is not self._running_loop():
        loop.call_soon_threadsafe(self.notify_dashboard_card, slot, reason)
        return
    try:
        if self._dynamic_cards is not None:
            self._dynamic_cards.notify(slot, reason)
    except Exception:
        logger.debug("Dashboard card event skipped", exc_info=True)


def push_session_summary(self: DashboardState, key: str) -> None:
    """Broadcast that a session's intent summary was regenerated.

    Lets the summary panel invalidate immediately instead of polling, which
    matters because the summary is deliberately a pull-friendly artifact: a
    panel that polled would reintroduce the checking loop the feature exists
    to remove. Fire-and-forget — the client's own staleness window remains
    the safety net if the event is missed.
    """
    self._broadcast({"_type": "session_summary", "key": key})


def push_artifact_update(
    self: DashboardState, slug: str, version: int, *, deleted: bool = False
) -> None:
    """Broadcast an artifact content change to all connected clients.

    Emitted from the artifact mutation funnel (create / content update /
    revert / pull-latest / relocate / delete) so every open dashboard
    window — main, popouts, companion chat panels — can invalidate its
    artifact queries immediately instead of waiting for the 30s react-query
    staleness window. Fire-and-forget, best-effort: the
    staleness window remains the safety net if a client misses the event.
    """
    self._broadcast(
        {
            "_type": "artifact_update",
            "slug": slug,
            "version": version,
            "deleted": deleted,
        }
    )


def push_refresh(self: DashboardState, *kinds: str) -> None:
    """Push a lightweight refresh hint for specific data types.

    The frontend receives ``event: refresh`` with ``data: kind1,kind2``
    and fetches fresh data only for those types.  This replaces blind
    polling — the server tells the client *when* to refresh, not the
    client guessing on a timer.

    Supported kinds: ``crons``, ``lessons``, ``agents``, ``history``,
    ``taskrunner``.
    """
    self._broadcast({"_type": "refresh", "kinds": ",".join(kinds)})


def push_update_progress(self: DashboardState, step: str, detail: str = "") -> None:
    """Broadcast an update progress event to all connected clients.

    ``step`` is a short machine-readable phase name (e.g. ``pulling``,
    ``syncing``, ``building``, ``installing``, ``restarting``, ``failed``).
    ``detail`` is an optional human-readable message.
    """
    self._update_progress = {"step": step, "detail": detail}
    self._broadcast(
        {
            "_type": "update_progress",
            "step": step,
            "detail": detail,
        }
    )


def clear_update_progress(self: DashboardState) -> None:
    """Reset update progress (e.g. after cancel or completion)."""
    self._update_progress = None


def _broadcast(self: DashboardState, note: dict[str, Any]) -> None:
    """Send a message to all connected SSE and WS clients."""
    for q in self._sse_queues:
        try:
            q.put_nowait(note)
        except asyncio.QueueFull:
            pass
    self._notify_event.set()
    # WS broadcast — translate internal _type to WS message format
    if self._ws_clients:
        msg_type = note.get("_type", "notification")
        # Payload the scope gate inspects (slot / source keys), tracked per
        # branch so the chokepoint can filter correctly.
        ws_data: object
        if msg_type == "slots":
            # SSE (above) consumed the BARE ``_slots_list``. The WS frame is
            # built from the ENRICHED ``_slots_list_ws`` (public-repo status
            # for dashboard users) when present — app tokens are re-filtered
            # in ``_serialize_for_client`` so they never receive it, and SSE
            # never reads this key. Falls back to the bare list for callers
            # (targeted title pushes, tests) that send only ``_slots_list``.
            slots_list = (
                note.get("_slots_list_ws") or note.get("_slots_list") or json.loads(note["slots"])
            )
            # ``data`` for slots carries the whole envelope so the
            # chokepoint can per-app filter and re-serialize it.
            ws_data = {
                "slots": slots_list,
                "yolo": note.get("_yolo", False),
                "channelTrusted": note.get("channelTrusted", False),
                "slotsPartial": note.get("slotsPartial", False),
                "slotsHeldKeys": note.get("slotsHeldKeys", ()),
                "slotsHoldChannels": note.get("slotsHoldChannels", False),
                # Consumed by ``_send_ws_all``; never serialized to a client.
                "_legacy_only": bool(note.get("_legacy_only", False)),
            }
            # Built by `_slots_ws_frame`, NOT inline: the owner frame in
            # `_do_slots_broadcast` has to carry an identical key set, and it
            # cannot if each site names its own keys. See that function.
            ws_msg = _slots_ws_frame(
                slots_list,
                yolo=bool(ws_data["yolo"]),
                channel_trusted=bool(ws_data["channelTrusted"]),
                gitlab_hosts_gen=note.get("gitlabHostsGeneration"),
                folders=note.get("folders"),
                folders_gen=note.get("foldersGeneration"),
                governance_gen=note.get("governanceGeneration"),
                slots_partial=bool(ws_data["slotsPartial"]),
                slots_held_keys=ws_data["slotsHeldKeys"],
                slots_hold_channels=bool(ws_data["slotsHoldChannels"]),
            )
        elif msg_type == "slot_title":
            ws_data = {"key": note["key"], "title": note["title"]}
            ws_msg = json.dumps({"type": "slot_title", "data": ws_data})
        elif msg_type == "refresh":
            ws_data = {"kinds": note["kinds"].split(",")}
            ws_msg = json.dumps({"type": "refresh", "data": ws_data})
        elif msg_type == "update_progress":
            ws_data = {"step": note["step"], "detail": note.get("detail", "")}
            ws_msg = json.dumps({"type": "update_progress", "data": ws_data})
        elif msg_type == "artifact_update":
            # Typed envelope (not the generic `notification` fallback) so
            # useWebSocket and future consumers get a self-documenting
            # event: {slug, version, deleted}.
            ws_data = {
                "slug": note["slug"],
                "version": note.get("version", 0),
                "deleted": note.get("deleted", False),
            }
            ws_msg = json.dumps({"type": "artifact_update", "data": ws_data})
        elif msg_type == "session_summary":
            # Typed envelope, for the same reason as artifact_update above.
            # Without it this event falls into the generic `notification`
            # fallback, where two things go wrong: the client's
            # `case 'session_summary'` never matches (so the panel is never
            # invalidated and only a reload shows a new summary — defeating
            # the push-on-change design that lets the panel skip polling),
            # and the payload is dispatched as a Notification, putting one
            # entry with no `ts` in the bell feed.
            ws_data = {"key": note["key"]}
            ws_msg = json.dumps({"type": "session_summary", "data": ws_data})
        elif msg_type == "chat_message":
            # One serialiser, both doors — see chat_message_frame().
            # include_metadata=True because THIS door filters downstream:
            # _send_ws_all -> _ws_client_allowed (deny-by-default event
            # scope) decides per socket whether an app token may see this
            # slot at all. The SSE door has no such gate and decides for
            # itself; do not copy this True over there.
            chat_data = chat_message_frame(note, include_metadata=True)
            ws_data = chat_data
            ws_msg = json.dumps({"type": "chat_message", "data": chat_data})
        else:
            ws_data = note
            ws_msg = json.dumps({"type": "notification", "data": note})
        self._send_ws_all(msg_type, ws_data, ws_msg)


def _spawn_ws_send(self: DashboardState, ws: web.WebSocketResponse, msg: str) -> None:
    _websocket_for(self)._spawn_ws_send(ws, msg)


def _drop_if_stalled(self: DashboardState, ws: web.WebSocketResponse) -> bool:
    return _websocket_for(self)._drop_if_stalled(ws)


def _on_ws_send_done(self: DashboardState, task: asyncio.Task) -> None:
    _websocket_for(self)._on_ws_send_done(task)


def _ws_client_allowed(
    self: DashboardState, ws: web.WebSocketResponse, msg_type: str, data: object
) -> bool:
    return _websocket_for(self)._ws_client_allowed(ws, msg_type, data)


def _serialize_for_client(
    self: DashboardState, ws: web.WebSocketResponse, msg_type: str, data: object, default_msg: str
) -> str:
    return _websocket_for(self)._serialize_for_client(ws, msg_type, data, default_msg)


def _serialize_subagent_batch(
    self: DashboardState,
    ws: web.WebSocketResponse,
    msg_type: str,
    data: object,
    default_msg: str,
) -> str:
    return _websocket_for(self)._serialize_subagent_batch(ws, msg_type, data, default_msg)


def _send_ws_all(self: DashboardState, msg_type: str, data: object, msg: str) -> None:
    _websocket_for(self)._send_ws_all(msg_type, data, msg)


def _send_ws_owners(
    self: DashboardState, msg: str, *, skip_slot_patch_clients: bool = False
) -> None:
    if skip_slot_patch_clients:
        _websocket_for(self)._send_ws_owners(msg, skip_slot_patch_clients=True)
    else:
        _websocket_for(self)._send_ws_owners(msg)


def broadcast_ws(self: DashboardState, msg_type: str, data: WsPayload) -> None:
    _websocket_for(self).broadcast_ws(msg_type, data)


def broadcast_context_usage(self: DashboardState, slot_key: str, payload: dict) -> None:
    _persistence_for(self).broadcast_context_usage(self, slot_key, payload)


async def deliver_ws_owners(self: DashboardState, msg_type: str, data: WsPayload) -> int:
    return await _websocket_for(self).deliver_ws_owners(msg_type, data)


def broadcast_ws_owners(self: DashboardState, msg_type: str, data: WsPayload) -> None:
    _websocket_for(self).broadcast_ws_owners(msg_type, data)


def ws_client_count(self: DashboardState) -> int:
    return _websocket_for(self).ws_client_count()


def dashboard_user_ws_count(self: DashboardState) -> int:
    return _websocket_for(self).dashboard_user_ws_count()


def broadcast_browser_event(self: DashboardState, event_type: str, data: dict) -> None:
    _websocket_for(self).broadcast_browser_event(event_type, data)


def register_ws(self: DashboardState, ws: web.WebSocketResponse, *, owner: bool = False) -> None:
    _websocket_for(self).register_ws(ws, owner=owner)


async def send_members_subscribed(self: DashboardState, ws: web.WebSocketResponse) -> None:
    await _websocket_for(self).send_members_subscribed(ws)


def unregister_ws(self: DashboardState, ws: web.WebSocketResponse) -> None:
    _websocket_for(self).unregister_ws(ws)


def _remove_ws(self: DashboardState, ws: web.WebSocketResponse) -> None:
    _websocket_for(self)._remove_ws(ws)


def subscribe_logs(self: DashboardState, ws: web.WebSocketResponse) -> None:
    _websocket_for(self).subscribe_logs(ws)


def unsubscribe_logs(self: DashboardState, ws: web.WebSocketResponse) -> None:
    _websocket_for(self).unsubscribe_logs(ws)


def subscribe_subagents(self: DashboardState, ws: web.WebSocketResponse) -> None:
    _websocket_for(self).subscribe_subagents(ws)


def unsubscribe_subagents(self: DashboardState, ws: web.WebSocketResponse) -> None:
    _websocket_for(self).unsubscribe_subagents(ws)


def broadcast_ws_subagent_subscribers(self: DashboardState, msg_type: str, data: WsPayload) -> None:
    _websocket_for(self).broadcast_ws_subagent_subscribers(msg_type, data)


async def close_all_ws(self: DashboardState) -> None:
    await _websocket_for(self).close_all_ws()
