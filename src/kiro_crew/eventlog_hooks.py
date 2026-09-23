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

    ANSWERS whether the append was queued. A caller that advances a checkpoint
    past the transitions it just handed over needs to know the handover
    happened: the ceiling below means this can refuse, and a refusal a caller
    cannot see is a lost event its checkpoint claims was written.

    The future is retained, not discarded. A discarded future is why a queued
    append could be lost at exit with nothing able to say so: the pool held work
    no one had a handle on. Holding it lets :func:`drain_for_shutdown` wait for
    exactly the appends still outstanding.

    Never raises: a writer calls this from a best-effort hook, so a pool that
    cannot accept work must not break the path it is hooked into.

    The outstanding set is BOUNDED at :data:`MAX_PENDING_APPENDS`. Retaining the
    futures is what makes a shutdown drain possible, and it is also what makes the
    queue a retained field, so it needs a ceiling for the same reason every other
    retained field here does: one worker drains in order, and a burst arriving
    faster than the filesystem retires it would otherwise grow without limit. Past
    the ceiling an append is refused and counted, and the episode is reported once.
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

    The log is append-only with no replay, so an append still sitting in the
    queue when the process exits is gone -- and the shutdown window is ordinary
    operation, not a crash. Returns False rather than raising when the timeout
    expires, so a caller can report a short log instead of believing a complete
    one.

    ``_reserved`` is waited on as well as ``_inflight``, and that is the whole
    difference between this and a snapshot of the futures. ``submit`` takes its
    reservation, RELEASES the lock to call ``pool.submit``, and only then registers
    the future -- so for the length of that call an append is counted in
    ``_reserved`` and absent from ``_inflight``. A drain reading only the futures
    sees an empty set there, answers "nothing remains", and the force-exit path that
    asked goes straight to ``os._exit``: the append dies queued, with no replay to
    recover it. There is nothing to WAIT on in that window, because the future does
    not exist yet, so the reservation is polled until it turns into one or the
    deadline passes.
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
#: with the snapshot the config-save hook writes in ``handlers/agents.py`` — the
#: reconcile below compares exactly these against ``cfg.agents[name]`` so a
#: hand-edited config still lands a correcting member/config event.
_CONFIG_FIELDS = (
    "kiro_agent",
    "workspace",
    "memory_store",
    "model",
    "source",
    "starred",
    "avatar",
)


def _config_snapshot_for_agent(agent_cfg) -> dict:
    """The 7 config-derived fields as a member/config would carry them.

    ``starred`` is coerced to ``bool`` (it is a load-time-coerced flag), matching
    the snapshot ``handlers/agents.py`` writes and the ``bool(agent_cfg.starred)``
    the roster endpoint sends. ``source`` is bounded to the roster vocabulary via
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


def reconcile_member_config(slug, name, agent_cfg, roster_view) -> "list[str] | None":
    """Append a correcting member/config when the log's roster drifts from config.

    Compares the log-derived *roster_view*'s 7 config fields against the live
    ``agent_cfg`` snapshot. When any differ — or the roster view has NO config
    field at all (no member/config has ever been appended) — appends one
    MEMBER_CONFIG carrying the full config snapshot plus a ``changed`` list, so
    the log becomes correct even when the config was edited by hand rather than
    through the dashboard (which emits its own member/config on save).

    Returns the ``changed`` field list when an event was appended, ``None`` when
    the roster already matched (no write). Best-effort: any failure is swallowed
    and reported as ``None``.
    """
    if not slug:
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
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.eventlog.types import MEMBER_CONFIG

        svc = get_service()
        svc.ensure(slug, name or slug)
        svc.append(slug, MEMBER_CONFIG, {**snapshot, "changed": changed})
        return changed
    except Exception:
        logger.debug("reconcile_member_config failed for slug=%r", slug, exc_info=True)
        return None


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

    For each global crew member:

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
        from kiro_crew.eventlog import types
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.validation import _AGENT_NAME_RE

        svc = get_service()
        agents = getattr(cfg, "agents", {}) or {}
        live_slots = getattr(state, "_slots", {}) if state is not None else {}

        def _patrol_is_still_armed(values: dict, observed: dict) -> bool:
            return _patrol_is_still_armed_at(values, observed)

        def _slot_is_still_open(slot_key: str) -> Callable[[dict, dict], bool]:
            # A factory, not a closure over the loop variable: a predicate evaluated
            # later under the write lock would otherwise read whatever the variable
            # held by then, which is the loop's LAST slot rather than this one.
            def _still_open(values: dict, observed: dict) -> bool:
                return _slot_is_still_open_at(slot_key, values, observed)

            return _still_open

        for name, agent_cfg in agents.items():
            if not _AGENT_NAME_RE.match(name):
                continue
            try:
                # member_slug, not slug_for_name: a member with an explicit
                # member_id keeps that identity, so reconciling by the folded
                # name would read a different log and leave the real one stale.
                slug = members_mod.member_slug(name, cfg)
            except Exception:
                continue
            try:
                svc.ensure(slug, name)
                snap = svc.snapshot(slug)
                values = snap.get("values", {}) if isinstance(snap, dict) else {}
                reconcile_member_config(slug, name, agent_cfg, values.get(types.PROJ_ROSTER, {}))
                # Patrol closer.
                wake = values.get(types.PROJ_WAKE, {}) or {}
                if wake.get("patrol") == "armed":
                    wake_slot = wake.get("slot_key")
                    has_loop = False
                    if autonudge_svc is not None and wake_slot:
                        try:
                            get_by_slot = getattr(autonudge_svc, "get_by_slot", None)
                            has_loop = (
                                bool(get_by_slot(wake_slot)) if callable(get_by_slot) else False
                            )
                        except Exception:
                            has_loop = False
                    if not has_loop:
                        # Re-asked under the write lock: this decision came from a
                        # snapshot, and the gateway is going live concurrently, so a
                        # patrol re-armed in between must not be closed by it.
                        if svc.append_closer_if_still_applies(
                            slug,
                            types.PATROL_STOPPED,
                            {"slot_key": wake_slot, "reason": "interrupted"},
                            still_applies=_patrol_is_still_armed,
                            observed=values,
                        ):
                            closers += 1
                # Slot closers.
                driving = values.get(types.PROJ_DRIVING, {}) or {}
                for slot_key in driving.get("open", []) or []:
                    if slot_key not in live_slots:
                        # Same window as the patrol closer above: a slot reopened
                        # between the snapshot and this write must survive it.
                        if svc.append_closer_if_still_applies(
                            slug,
                            types.SLOT_CLOSED,
                            {"slot_key": slot_key, "reason": "interrupted"},
                            still_applies=_slot_is_still_open(slot_key),
                            observed=values,
                        ):
                            closers += 1
            except Exception:
                logger.debug("startup reconcile failed for slug=%r", slug, exc_info=True)
    except Exception:
        logger.debug("reconcile_members_at_startup failed", exc_info=True)
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
        key = slot_key
        for prefix in ("dashboard_", "dashboard:"):
            if key.startswith(prefix):
                key = key[len(prefix) :]
                break
        prefix = members_mod.DM_SLOT_KEY_PREFIX
        if not key.startswith(prefix):
            return None
        # Through the shared parser, which drops the ``.memory-<store>`` suffix
        # a V2 member's slot key carries. Reading the tail directly leaves the
        # ``.`` in place, validate_slug refuses it, and every durable event for
        # that member is silently dropped.
        slug = members_mod.slug_from_dm_slot_key(key)
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

    Best-effort in that a failure never PROPAGATES: a caller recording a
    transition must not be brought down by its own bookkeeping. It is not
    best-effort in the sense of discarding the outcome. Two things follow from
    that, and both matter because this log is the projections' only input.

    The answer is RETURNED, so a caller whose own record is this event alone can
    tell a landed transition from an omitted one instead of assuming. Callers that
    already persist through an authoritative store first do not need it: the two
    boundary writers fence their change and answer 500 when the fence itself
    fails, so their event is a second copy rather than the record.

    A failure is REPORTED rather than logged at debug. What is lost is a
    transition that will not be retried, and at debug an omitted transition is
    indistinguishable from one that never happened -- which is the single
    distinction a projection built from this log exists to make.
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
