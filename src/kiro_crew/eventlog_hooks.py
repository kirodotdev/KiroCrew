"""Best-effort hook helpers for the per-member append-only event log.

Every function here is additive and swallows its own failures: a logging fault
must never break the path it is hooked into. The event-log service itself
(``kiro_crew.eventlog.service``) is filled in concurrently and its bodies may
still raise ``NotImplementedError`` while callers run, which is precisely why
:func:`emit` wraps ensure+append in a blanket ``try/except``.

The service contract this codes against is synchronous:

    svc = get_service()
    svc.ensure(slug, name)
    svc.append(slug, type, data)
    svc.attach_broadcast(fn)

Imports of the service and of the members module are done lazily inside the
functions to avoid import cycles with ``dashboard.state`` and
``slack.gateway``.
"""

from __future__ import annotations

import atexit
import concurrent.futures
import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

# Every offloaded member event-log append runs on this ONE worker, so appends
# execute in submission order. The pool lives HERE, beside `emit`, rather than in
# any one caller: a writer that offloads an append already imports this module to
# do the append, so it cannot reach for the default pool without ignoring the
# executor sitting next to the function it is calling. Ordering by luck is what
# the default multi-worker pool gave, and the log is the authoritative record
# other surfaces read.
_io_pool: concurrent.futures.ThreadPoolExecutor | None = None
_io_pool_lock = threading.Lock()


def io_executor() -> concurrent.futures.ThreadPoolExecutor:
    """The single-worker executor every offloaded event-log append submits to.

    Creation is locked, not just the queue: an unlocked check-then-set lets two
    threads each build a pool and each proceed, which loses the serialisation
    that is the only thing this executor provides. Double-checked so the lock is
    paid once rather than on every call.
    """
    global _io_pool
    if _io_pool is None:
        with _io_pool_lock:
            if _io_pool is None:
                _io_pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="eventlog-io"
                )
    return _io_pool


# How long exit waits for queued appends. Bounded for the reason the crew-log
# drain documents: blocking is right on the shutdown path, which has nothing
# left to keep responsive, but a wedged filesystem must delay exit rather than
# hang it.
SHUTDOWN_DRAIN_SECONDS = 5.0

#: How long the drain sleeps when only RESERVATIONS are outstanding. The window it
#: covers is one ``pool.submit`` call, so this is short by design: it is the delay
#: before the reservation becomes a future the drain can actually wait on, and it is
#: bounded by the drain's own deadline, never added to it.
_RESERVATION_POLL_SECONDS = 0.01

#: How many appends may be OUTSTANDING on the ordered executor at once. One
#: worker drains them in order, so a burst arriving faster than the filesystem
#: retires it queues -- and each queued item retains a closure holding the event's
#: own data, so the queue is a retained field and needs the bound every retained
#: field needs. Overflow is REFUSED rather than coalesced: two appends to one log
#: are distinct facts, so merging them would silently drop one, and a refusal that
#: is counted and reported is the honest answer to a backlog this deep.
MAX_PENDING_APPENDS = 1000

#: Slots claimed by a caller that has passed the ceiling check but whose future does
#: not exist yet. The set below cannot hold them -- there is nothing to hold until
#: `submit` returns -- so they are counted here and released the moment the future
#: joins the set. Without this the ceiling is advisory under concurrency.
_reserved = 0

_inflight: set[concurrent.futures.Future] = set()
_inflight_lock = threading.Lock()
_drain_registered = False
_dropped_appends = 0
_overflowing = False


def submit(fn: Callable[[], None]) -> bool:
    """Queue one event-log append on the ordered executor and REMEMBER it.

    ANSWERS whether the append was queued: a caller that advances a checkpoint
    past what it handed over must see a refusal, or its checkpoint claims a lost
    event was written.

    The future is retained so :func:`drain_for_shutdown` can wait for exactly the
    appends still outstanding; a discarded future could be lost at exit unseen.

    Never raises: callers are best-effort hooks that a full pool must not break.

    The outstanding set is BOUNDED at :data:`MAX_PENDING_APPENDS`: one worker
    drains in order, so a burst faster than the filesystem would otherwise grow
    without limit. Past the ceiling an append is refused and counted, and the
    episode is reported once.
    """
    global _drain_registered, _dropped_appends, _overflowing, _reserved
    try:
        pool = io_executor()
        with _inflight_lock:
            if not _drain_registered:
                _drain_registered = True
                atexit.register(drain_for_shutdown)
            # RESERVED under the lock, not merely checked under it. The set is added
            # to after the lock is released -- it has to be, because the future does
            # not exist until `submit` returns -- so a check alone lets N threads
            # each pass a count that was true for all of them and then each add,
            # putting the outstanding set past the ceiling by N-1. Counting the
            # reservations alongside the set is what makes the decision and the claim
            # one step. Refusing after submit would bound nothing: the closure is
            # queued by then and the memory already spent.
            if len(_inflight) + _reserved >= MAX_PENDING_APPENDS:
                _dropped_appends += 1
                first_of_episode = not _overflowing
                _overflowing = True
                dropped = _dropped_appends
            else:
                first_of_episode = False
                dropped = 0
                _reserved += 1
        if dropped:
            if first_of_episode:
                # One line per EPISODE, not per drop: a burst deep enough to
                # overflow would otherwise turn one fault into thousands of log
                # lines, and the cumulative total is what a reader needs anyway.
                logger.warning(
                    "event-log appends DROPPED: more than %d are already queued, so this "
                    "event is omitted from its member's log and is not retried "
                    "(%d dropped in total)",
                    MAX_PENDING_APPENDS,
                    dropped,
                )
            return False
        future = pool.submit(fn)
    except Exception:
        logger.debug("event-log append could not be queued", exc_info=True)
        with _inflight_lock:
            # Released, or the ceiling would fall by one for the life of the process
            # every time a submit failed.
            _reserved = max(0, _reserved - 1)
        return False
    with _inflight_lock:
        _inflight.add(future)
        _reserved = max(0, _reserved - 1)
        _overflowing = False
    # Discard on completion so the set tracks what is OUTSTANDING rather than
    # growing for the life of the process.
    future.add_done_callback(lambda f: _forget(f))
    return True


def _forget(future: concurrent.futures.Future) -> None:
    with _inflight_lock:
        _inflight.discard(future)


def drain_for_shutdown(timeout: float = SHUTDOWN_DRAIN_SECONDS) -> bool:
    """Wait for queued appends to reach the log. Returns True when none remain.

    The log is append-only with no replay, so an append still queued at exit is
    gone. Returns False rather than raising on timeout, so a caller can report a
    short log instead of believing a complete one.

    ``_reserved`` is waited on as well as ``_inflight``: ``submit`` releases the
    lock to call ``pool.submit`` before it registers the future, so in that window
    an append is only a reservation. A drain reading only the futures would answer
    "nothing remains" and let a force exit drop it. With no future to wait on yet,
    the reservation is polled until it becomes one or the deadline passes.
    """
    deadline = time.monotonic() + timeout
    while True:
        with _inflight_lock:
            pending = set(_inflight)
            reserved = _reserved
        if not pending and not reserved:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning(
                "%d member event-log append(s) did not reach the log before exit",
                len(pending) + reserved,
            )
            return False
        if not pending:
            # Reservations only: no future to wait on yet. Sleep briefly rather than
            # spinning, and come back to look for the future it is about to become.
            time.sleep(min(_RESERVATION_POLL_SECONDS, remaining))
            continue
        _, not_done = concurrent.futures.wait(pending, timeout=remaining)
        if not_done:
            logger.warning(
                "%d member event-log append(s) did not reach the log before exit",
                len(not_done),
            )
            return False


#: The config-derived fields the roster view carries and a member/config event
#: snapshots. Kept in lockstep with ``members_projections._CONFIG_FIELDS`` and
#: with the snapshot the config-save hook writes in
#: ``dashboard/agent_admin/crew_update.py`` — the reconcile below compares exactly these
#: against ``cfg.agents[name]`` so a
#: hand-edited config still lands a correcting member/config event.
_CONFIG_FIELDS = (
    "kiro_agent",
    "workspace",
    "memory_store",
    "model",
    "source",
    "starred",
    "avatar",
    "display_name",
)


def _config_snapshot_for_agent(agent_cfg) -> dict:
    """The config-derived fields as a member/config would carry them.

    ``starred`` is coerced to ``bool`` (it is a load-time-coerced flag), matching
    the snapshot ``dashboard/agent_admin/crew_update.py`` writes and the
    ``bool(agent_cfg.starred)`` the roster endpoint sends. ``source`` is bounded to the
    roster vocabulary via
    the same ``normalize_member_source`` the HTTP row uses, so a credential- or
    URL-shaped value planted in the agent-writable ``source`` cannot reach the
    browser through the durable projection either (the roster row already
    collapses it; without this the projected snapshot would ship it raw). Every
    other field is passed through as-is.
    """
    # Lazy import mirrors the other handlers.members lookups in this module and
    # keeps the config->projection path free of an import cycle.
    from kiro_crew.dashboard.handlers.members import normalize_member_source

    out: dict = {}
    for field in _CONFIG_FIELDS:
        value = getattr(agent_cfg, field, None)
        if field == "starred":
            out[field] = bool(value)
        elif field == "source":
            out[field] = normalize_member_source(value)
        else:
            out[field] = value
    return out


def _config_is_still_at(values: dict, observed: dict) -> bool:
    """Does the roster still show the config fields the caller decided to correct?

    Module level so the reconcile and its tests share ONE definition.

    The correcting event carries the WHOLE config snapshot and the roster fold is
    last-wins per field, so every config field is compared, not only ``changed``:
    any one moved by another writer would be regressed. Nothing else in the block
    is compared, so an unrelated event does not starve a correction still right.

    A field present on one side only counts as moved, so a never-configured
    member's first snapshot is refused once another writer has placed one.
    """
    from kiro_crew.eventlog import types

    current = values.get(types.PROJ_ROSTER, {}) if isinstance(values, dict) else {}
    was = observed.get(types.PROJ_ROSTER, {}) if isinstance(observed, dict) else {}
    if not isinstance(current, dict) or not isinstance(was, dict):
        return False
    missing = object()
    return all(current.get(f, missing) == was.get(f, missing) for f in _CONFIG_FIELDS)


def reconcile_member_config(
    slug, name, agent_cfg, roster_view, *, config_stamp: str
) -> "list[str] | None":
    """Append a correcting member/config when the log's roster drifts from config.

    When the log-derived *roster_view*'s config fields differ from the live
    ``agent_cfg`` snapshot, or no member/config was ever appended, appends one
    MEMBER_CONFIG with the full snapshot plus a ``changed`` list, so a hand-edited
    config is corrected too (a dashboard save emits its own member/config).

    Returns the ``changed`` list when an event was appended, ``None`` otherwise.
    Best-effort: any failure is swallowed and reported as ``None``.

    Two staleness windows sit between the caller's decision and this write.

    *config_stamp* closes the first and is REQUIRED: the digest
    ``load_config_with_content_stamp`` bound to the bytes ``agent_cfg`` came from.
    A save after that load writes config.json AND its own member/config, so the
    roster can carry the NEW values while ``agent_cfg`` is old; without the stamp
    the old snapshot would be appended over the save, and the log has no
    compaction. This refuses unless the live config is still those bytes. No
    falsy stand-in for "unknown" is accepted: a caller that could not name its
    bytes must not reconcile at all.

    The conditional append closes the second: ``append_closer_if_still_applies``
    re-asks ``_config_is_still_at`` under the write lock, and the store admits the
    entry only while the log's tail is where that fold reached. A refusal is
    normal (the other writer is newer); the next roster read compares afresh.
    """
    if not slug or not config_stamp:
        return None
    try:
        snapshot = _config_snapshot_for_agent(agent_cfg)
        view = roster_view if isinstance(roster_view, dict) else {}
        # No config field present at all -> the log has never seen a
        # member/config for this member; treat every field as changed so the
        # first snapshot lands.
        never_configured = not any(f in view for f in _CONFIG_FIELDS)
        if never_configured:
            changed = list(_CONFIG_FIELDS)
        else:
            changed = [f for f in _CONFIG_FIELDS if view.get(f) != snapshot[f]]
        if not changed:
            return None
        from kiro_crew.config.loader import config_content_stamp

        # Read here rather than inside the append: the store's hold must carry a
        # comparison and never file I/O, and a stamp taken now is what the
        # comparison below is about.
        if config_stamp != config_content_stamp():
            return None
        from kiro_crew.eventlog import types
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.eventlog.types import MEMBER_CONFIG

        svc = get_service()
        svc.ensure(slug, name or slug)
        appended = svc.append_closer_if_still_applies(
            slug,
            MEMBER_CONFIG,
            {**snapshot, "changed": changed},
            still_applies=_config_is_still_at,
            observed={types.PROJ_ROSTER: view},
        )
        return changed if appended is not None else None
    except Exception:
        logger.debug("reconcile_member_config failed for slug=%r", slug, exc_info=True)
        return None


def member_message_payload(role, content, meta, ts: float, *, sanitize) -> dict:
    """The ``member/message`` event for one row landing in a member's chat.

    Every row bumps recency (``ts``); only SPEECH -- a user or assistant row
    with visible text that is not a system notice or a workflow envelope
    (``is_speech_row``) -- carries a ``preview``, so the roster keeps quoting the
    last thing said when a patrol turn, a say-nothing reply or a cron envelope
    lands. The preview is built by ``speech_preview`` -- strip markdown, run the
    caller's redaction chain, cap with an ellipsis -- which is the SAME function
    the roster's cold read (``last_speech_info``) uses, so
    the folded preview and the read agree byte for byte and
    ``reconcile_member_preview`` has nothing to correct for a live message.
    Module level so the live writer and its test share ONE definition.
    """
    from kiro_crew.dashboard.system_notices import is_speech_row
    from kiro_crew.history_projection import TranscriptReadProjection
    from kiro_crew.preview_text import speech_preview

    payload: dict[str, object] = {"ts": ts}
    # Normalised first, as the cold read does: structured content is speech
    # when its text blocks say something.
    text = TranscriptReadProjection._content_text(content)
    if not is_speech_row(role, text, meta):
        return payload
    preview = speech_preview(text, sanitize)
    if preview:
        payload["preview"] = preview
    return payload


def _preview_is_still_at(values: dict, observed: dict) -> bool:
    """Does the roster still show the preview the caller decided to correct?

    Module level so the reconcile and its tests share ONE definition.

    The correction is computed from a snapshot and written later; a live
    ``member/message`` landing in between carries a newer preview and recency.
    Both folds are last-wins in an append-only log, so a later correction would
    durably regress them. Hence the two fields the correction rewrites,
    ``last_message`` and ``last_active_ts``, must be UNCHANGED. Only those two:
    an unrelated event must not starve a correction that is still right.
    """
    from kiro_crew.eventlog import types

    now = values.get(types.PROJ_ROSTER) or {}
    then = observed.get(types.PROJ_ROSTER) or {}
    return (now.get("last_message"), now.get("last_active_ts")) == (
        then.get("last_message"),
        then.get("last_active_ts"),
    )


def reconcile_member_preview(slug, name, preview, msg_ts, roster_view) -> bool:
    """Append a correcting member/message when the log's roster preview drifts
    from the transcript's speech-only read.

    ``last_message`` folds last-wins from ``member/message`` events, and an event
    from a writer that does not apply ``is_speech_row`` carries machinery text (a
    tool line, a patrol turn). The transcript is the authority: ``api_members``
    reads it with ``last_speech_info`` and passes the answer here, INCLUDING an
    empty one, so a never-spoken patroller's stale preview is blanked. Appends
    nothing when the two agree. ``ts`` is the transcript's newest epoch when known
    (the roster orders by it), else the current time.

    The append goes through ``append_closer_if_still_applies`` with
    ``_preview_is_still_at`` under the per-slug write lock, so a live message
    landing after the roster read refuses the correction. A refusal is normal;
    the next roster read compares afresh.

    Returns True when an event was appended. Best-effort: failures are swallowed.
    """
    if not slug:
        return False
    try:
        view = roster_view if isinstance(roster_view, dict) else {}
        current = view.get("last_message")
        wanted = preview if isinstance(preview, str) else ""
        if (current or "") == wanted:
            return False
        from kiro_crew.eventlog import types
        from kiro_crew.eventlog.service import get_service

        svc = get_service()
        svc.ensure(slug, name or slug)
        ts = float(msg_ts) if msg_ts else time.time()
        appended = svc.append_closer_if_still_applies(
            slug,
            types.MEMBER_MESSAGE,
            {"ts": ts, "preview": wanted},
            still_applies=_preview_is_still_at,
            observed={types.PROJ_ROSTER: view},
        )
        return appended is not None
    except Exception:
        logger.debug("reconcile_member_preview failed for slug=%r", slug, exc_info=True)
        return False


def _patrol_is_still_armed_at(values: dict, observed: dict) -> bool:
    """Does the patrol the caller decided to close still exist, unchanged?

    Module level so the reconcile and its tests share ONE definition; a copy in the
    test would pin the copy and let production drift away from it.

    `armed` alone is not the answer. A patrol that stopped and was re-armed between
    the caller's snapshot and this check also reads `armed`, and closing THAT one
    stamps the new episode with the old one's STOPPED -- after which the live patrol
    reads as stopped and, because the log is append-only with no compaction, nothing
    ever reopens it. So the wake view must also be UNCHANGED. That is precise rather
    than merely conservative: `WakeProjection.apply` returns the state untouched for
    every type except the two patrol transitions, so the view moves on a re-arm and
    on nothing else.
    """
    from kiro_crew.eventlog import types

    wake = values.get(types.PROJ_WAKE) or {}
    if wake.get("patrol") != "armed":
        return False
    return wake == (observed.get(types.PROJ_WAKE) or {})


def _slot_is_still_open_at(slot_key: str, values: dict, observed: dict) -> bool:
    """Was this ONE slot open when the caller decided, and is it open now?

    Per slot rather than by comparing the whole driving view: that view holds every
    open slot for the member, so equality would refuse this closer merely because an
    unrelated slot opened in the window.

    A close-and-reopen of THIS key inside the window is still invisible -- the
    projection records a SET of open keys and nothing that distinguishes two
    occupancies of the same key -- so that narrower race needs projection data that
    does not exist yet. Recorded rather than silently accepted.
    """
    from kiro_crew.eventlog import types

    driving = values.get(types.PROJ_DRIVING) or {}
    if slot_key not in (driving.get("open") or []):
        return False
    return slot_key in ((observed.get(types.PROJ_DRIVING) or {}).get("open") or [])


def reconcile_members_at_startup(cfg, state, autonudge_svc) -> int:
    """Reconcile every crew member's log against live state at gateway boot.

    For each dispatchable global crew member whose resolved slug has exactly
    one configured claimant and whose existing log header is not owned by
    another member:

    * ``ensure`` its log exists;
    * config-reconcile it (see :func:`reconcile_member_config`), so a config
      edited while the gateway was down lands a correcting member/config;
    * write CLOSERS for durable facts the log still believes are open but the
      live process does not back:
        - ``wake.patrol == 'armed'`` with NO live auto-nudge loop for
          ``wake.slot_key`` -> PATROL_STOPPED {slot_key, reason: 'interrupted'};
        - each ``driving.open`` slot_key absent from ``state._slots`` ->
          SLOT_CLOSED {slot_key, reason: 'interrupted'}.

    Best-effort by contract: a failure on one member never aborts the sweep or
    boot. Returns the number of CLOSER events written (config events excluded),
    logged at info.
    """
    closers = 0
    try:
        from kiro_crew import members as members_mod
        from kiro_crew.crew_log.errors import CrewLogError
        from kiro_crew.eventlog import types
        from kiro_crew.eventlog.service import CloserTailContention, get_service

        # The two ways a closer comes back unplaced against a member another process
        # is writing: it lost the tail on every attempt, or it was refused write
        # ownership. Named once and caught as one, because they carry the same three
        # facts -- the closer is unplaced, nothing is known about whether it applied,
        # and the closers below it are unaffected -- so a site that handled only one
        # of them would starve the same siblings through the other door. Retrying is
        # safe for both: a ``CrewLogError`` is a refusal the store defines as having
        # written nothing, and ``IndeterminateAppend``, the case that may have
        # written, is deliberately not one of them and still propagates.
        closer_unplaced = (CloserTailContention, CrewLogError)

        svc = get_service()
        agents = getattr(cfg, "agents", {}) or {}
        live_slots = getattr(state, "_slots", {}) if state is not None else {}

        # The config reconcile corrects the log FROM the config, so it runs only from
        # a load whose bytes are named and whole, as the roster read requires. The
        # *cfg* handed in was loaded at gateway construction, and a dashboard save can
        # land before this task runs; writing it would overwrite the newer save. So
        # load again here and carry that load's stamp, which the reconcile re-compares
        # under the write lock to catch a save mid-sweep.
        #
        # ``None`` withholds only the config write: the closers are decided from live
        # process state, not config content, so they still run. Enumeration stays with
        # *cfg*, so which logs this sweep touches does not depend on config content.
        fresh_cfg = None
        config_stamp: str | None = None
        try:
            from kiro_crew.config.loader import load_config_with_content_stamp

            fresh_cfg, config_stamp = load_config_with_content_stamp()
        except Exception:
            logger.debug("startup reconcile could not re-load config", exc_info=True)
            fresh_cfg, config_stamp = None, None
        # Absent ``degraded_sections`` counts as degraded: an object that cannot answer
        # the faithfulness question has not answered it yes, and this gate fails closed.
        if config_stamp is not None and getattr(fresh_cfg, "degraded_sections", True):
            config_stamp = None
        fresh_agents = (getattr(fresh_cfg, "agents", {}) or {}) if config_stamp else {}
        if config_stamp is None:
            unfaithful = fresh_cfg is not None and getattr(fresh_cfg, "degraded_sections", True)
            logger.warning(
                "the agents config is %s, so this startup sweep reconciles no "
                "member/config; closers are unaffected and the next roster read of a "
                "whole, parseable config corrects the log",
                "degraded to defaults" if unfaithful else "unnamed by any content",
            )

        def _patrol_is_still_armed(values: dict, observed: dict) -> bool:
            return _patrol_is_still_armed_at(values, observed)

        def _slot_is_still_open(slot_key: str) -> Callable[[dict, dict], bool]:
            # A factory, not a closure over the loop variable: a predicate evaluated
            # later under the write lock would otherwise read whatever the variable
            # held by then, which is the loop's LAST slot rather than this one.
            def _still_open(values: dict, observed: dict) -> bool:
                return _slot_is_still_open_at(slot_key, values, observed)

            return _still_open

        def _sweep_member(name: str, slug: str) -> None:
            """Reconcile one member, counting each closer into *closers* as it lands.

            Every step is decided from a fresh snapshot and guarded by its own
            predicate, so the retry pass below can simply call it again.

            The count is incremented here rather than returned: a later closer can
            raise ``CloserTailContention`` after an earlier one landed, and a returned
            total would be lost with the exception (the retry declines the landed
            closer, since it changed the projection its predicate reads).

            Each closer's contention is caught where it happens and re-raised only
            after the others are attempted, so one closer that keeps losing the tail
            (the patrol closer runs first) does not leave the slots below it open
            until the next boot.
            """
            nonlocal closers
            first_unplaced: Exception | None = None
            svc.ensure(slug, name)
            snap = svc.snapshot(slug)
            values = snap.get("values", {}) if isinstance(snap, dict) else {}
            # Reconciled from the config loaded by THIS sweep, not from the object it
            # was handed, and only while that load's bytes are still the live ones --
            # see where the stamp is decided. ``fresh_agents`` is empty when no stamp
            # could be bound, which is what withholds the write; a member absent from
            # the fresh load is also skipped, because there is then no current
            # config to correct it from.
            fresh_agent_cfg = fresh_agents.get(name)
            if fresh_agent_cfg is not None and config_stamp is not None:
                reconcile_member_config(
                    slug,
                    name,
                    fresh_agent_cfg,
                    values.get(types.PROJ_ROSTER, {}),
                    config_stamp=config_stamp,
                )
            # Patrol closer.
            wake = values.get(types.PROJ_WAKE, {}) or {}
            if wake.get("patrol") == "armed":
                wake_slot = wake.get("slot_key")
                has_loop = False
                if autonudge_svc is not None and wake_slot:
                    try:
                        get_by_slot = getattr(autonudge_svc, "get_by_slot", None)
                        has_loop = bool(get_by_slot(wake_slot)) if callable(get_by_slot) else False
                    except Exception:
                        has_loop = False
                if not has_loop:
                    # Re-asked under the write lock: this decision came from a
                    # snapshot, and the gateway is going live concurrently, so a
                    # patrol re-armed in between must not be closed by it.
                    try:
                        if svc.append_closer_if_still_applies(
                            slug,
                            types.PATROL_STOPPED,
                            {"slot_key": wake_slot, "reason": "interrupted"},
                            still_applies=_patrol_is_still_armed,
                            observed=values,
                        ):
                            closers += 1
                    except closer_unplaced as exc:
                        first_unplaced = first_unplaced or exc
            # Slot closers.
            driving = values.get(types.PROJ_DRIVING, {}) or {}
            for slot_key in driving.get("open", []) or []:
                if slot_key not in live_slots:
                    # Same window as the patrol closer above: a slot reopened
                    # between the snapshot and this write must survive it.
                    try:
                        if svc.append_closer_if_still_applies(
                            slug,
                            types.SLOT_CLOSED,
                            {"slot_key": slot_key, "reason": "interrupted"},
                            still_applies=_slot_is_still_open(slot_key),
                            observed=values,
                        ):
                            closers += 1
                    except closer_unplaced as exc:
                        first_unplaced = first_unplaced or exc
            if first_unplaced is not None:
                # Re-raised only now, so the caller's retry pass still sees this
                # member as contended. Unchanged, so it keeps naming the closer
                # that actually went unplaced.
                raise first_unplaced

        contended: list[tuple[str, str]] = []
        members_by_slug: dict[str, list[tuple[str, object]]] = {}
        claimant_count_by_slug: dict[str, int] = {}
        non_dispatchable_name_count = 0
        unresolved_slug_count = 0
        for name, agent_cfg in agents.items():
            try:
                slug = members_mod.member_slug(name, cfg)
            except Exception:
                unresolved_slug_count += 1
                continue
            claimant_count_by_slug[slug] = claimant_count_by_slug.get(slug, 0) + 1
            if not members_mod.is_dispatchable_member_name(name):
                non_dispatchable_name_count += 1
                continue
            members_by_slug.setdefault(slug, []).append((name, agent_cfg))

        if non_dispatchable_name_count or unresolved_slug_count:
            logger.warning(
                "member event-log startup reconcile skipped %d non-dispatchable name(s) and %d unresolved slug(s)",
                non_dispatchable_name_count,
                unresolved_slug_count,
            )

        for slug, resolved_members in members_by_slug.items():
            claimant_count = claimant_count_by_slug[slug]
            if claimant_count != 1:
                logger.warning(
                    "member event-log startup reconcile skipped ambiguous slug=%r (%d members)",
                    slug,
                    claimant_count,
                )
                continue
            name, agent_cfg = resolved_members[0]
            try:
                logged_name = svc.logged_name(slug)
                if logged_name is not None and logged_name != name:
                    # Only the EXACT name proves ownership. A header equal to the
                    # slug is the nameless-writer placeholder, and it is ambiguous
                    # rather than harmless: a retired member with no ``member_id``
                    # whose name folded onto itself leaves nothing reserving the
                    # slug once its row is pruned, so a recreated display-name
                    # member is handed the identical identity and ``member_slug``
                    # resolves it here. Writing this member's configuration and
                    # closers into that log is append-only and unrecoverable, so
                    # both shapes skip; the diagnostic names the slug only.
                    kind = "placeholder" if logged_name == slug else "foreign"
                    logger.warning(
                        "member event-log startup reconcile skipped slug=%r with %s header",
                        slug,
                        kind,
                    )
                    continue
                _sweep_member(name, slug)
            except closer_unplaced:
                contended.append((name, slug))
            except Exception:
                # Slug only, no exception text: a header value or a store error
                # can carry a stored display name, which this log must not.
                logger.warning("member event-log startup reconcile failed for slug=%r", slug)

        # This sweep runs ONCE per boot, so a closer that never got a clean window is
        # not re-decided until the next restart -- the interrupted slot reads open and
        # the interrupted patrol reads armed until then. What took the window is
        # another process's burst of appends to that one member, which a pass moments
        # later is past, so one more attempt is what turns a permanent loss into a
        # delay. Normally this list is empty and the pass costs nothing.
        for name, slug in contended:
            try:
                _sweep_member(name, slug)
            except closer_unplaced:
                logger.warning(
                    "startup reconcile could not place closers for slug=%r: the member's "
                    "log stayed under foreign writes across a retry pass, so its "
                    "interrupted state reads open until the next boot re-decides",
                    slug,
                )
            except Exception:
                logger.warning("member event-log startup reconcile retry failed for slug=%r", slug)
    except Exception:
        logger.warning("member event-log startup reconcile failed before processing members")
    logger.info("member event-log startup reconcile wrote %d closer event(s)", closers)
    return closers


def member_slug_for_slot(slot_key) -> "str | None":
    """Return the member slug a DM slot is keyed to, or ``None``.

    Member DM slots are keyed ``member-<slug>`` (possibly under a
    ``dashboard_`` / ``dashboard:`` prefix). Uses the members module's own
    predicate and derivation so this stays in lockstep with the slot layer.
    """
    if not isinstance(slot_key, str) or not slot_key:
        return None
    try:
        from kiro_crew import members as members_mod

        if not members_mod.is_member_session_key(slot_key):
            return None
        # Through the shared parser, which drops the ``.memory-<store>`` suffix
        # a V2 member's slot key carries. Reading the tail directly leaves the
        # ``.`` in place, validate_slug refuses it, and every durable event for
        # that member is silently dropped.
        slug = members_mod.slug_from_dm_slot_key(members_mod._slot_part(slot_key))
        if slug is None:
            return None
        # Round-trip through validate_slug so a malformed tail reads as "not a
        # member slot" rather than an unusable slug.
        return members_mod.validate_slug(slug)
    except Exception:
        logger.debug("member_slug_for_slot failed for %r", slot_key, exc_info=True)
        return None


def member_name_for_slug(cfg, slug) -> "str | None":
    """Return the exact member NAME for *slug* under *cfg*, or ``None``.

    Reuses the members handler's name resolver (config-order, first match wins
    for a colliding slug); falls back to a direct scan of ``cfg.agents`` via
    ``members.slug_for_name`` if that import is unavailable.
    """
    if not slug:
        return None
    try:
        from kiro_crew.dashboard.handlers.members import _member_names_for_slug

        names = _member_names_for_slug(cfg, slug)
        return names[0] if names else None
    except Exception:
        logger.debug("member_name_for_slug resolver failed for %r", slug, exc_info=True)
    try:
        from kiro_crew import members as members_mod

        for name in getattr(cfg, "agents", {}) or {}:
            try:
                if members_mod.slug_for_name(name) == slug:
                    return name
            except Exception:
                continue
    except Exception:
        logger.debug("member_name_for_slug fallback failed for %r", slug, exc_info=True)
    return None


def emit(slug, name, type, data) -> bool:
    """Ensure a member's log exists and append one event; answer whether it landed.

    A failure never PROPAGATES, so a caller is not brought down by its own
    bookkeeping, but the outcome is not discarded: this log is the projections'
    only input.

    The answer is RETURNED, so a caller whose only record is this event can tell a
    landed transition from an omitted one. Callers that persist to an
    authoritative store first (the two boundary writers, which answer 500 when
    their fence fails) do not need it.

    A failure is logged as a warning, not at debug: the transition is not
    retried, and at debug an omitted one looks like one that never happened.
    """
    if not slug:
        return False
    try:
        from kiro_crew.eventlog.service import get_service

        svc = get_service()
        svc.ensure(slug, name or slug)
        svc.append(slug, type, data)
        return True
    except Exception as exc:
        logger.warning(
            "event-log emit DROPPED for slug=%r type=%r: the event is omitted from this "
            "member's log and is not retried: %s",
            slug,
            type,
            exc,
            exc_info=True,
        )
        return False
