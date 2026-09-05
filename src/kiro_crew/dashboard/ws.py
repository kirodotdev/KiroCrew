"""WebSocket endpoint — multiplexes all real-time events over a single connection."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

from kiro_crew import __version__ as _local_version
from kiro_crew import shutdown_event
from kiro_crew.dashboard.chat_utils import effective_session_key, subagent_event_slot
from kiro_crew.dashboard.handlers.updates import status_update_fields
from kiro_crew.dashboard.origin import check_origin
from kiro_crew.dashboard.state import DashboardState, _safe_folder_tree
from kiro_crew.dashboard.ws_event_scope import (
    _audit_allow,
    _audit_deny,
    effective_allowed_events,
    filter_slots_for_app,
    load_declared_events_for_connect,
    slots_envelope_extras,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

_WS_STATUS_INTERVAL = 5  # seconds between dashboard status pushes
_WS_COUNTS_CACHE_TTL = 30  # seconds between refreshing lesson/cron counts
# Consecutive failed count refreshes before (a) backing off to the normal TTL
# cadence and (b) one operator-visible warning: failures retry every pusher
# tick (~5s), so 6 ≈ 30s of sustained failure — long enough to skip transient
# sqlite busy-timeouts, short enough that a store that never initializes
# surfaces the same minute it happens.
_WS_COUNTS_WARN_AFTER_FAILURES = 6
# Gateway-wide floor between count-failure warnings: the fault is global (one
# store), so N open sockets must not emit N identical warnings per streak.
# None = never warned — NOT 0.0, which time.monotonic() (time since boot) is
# still within for 10 minutes after host boot, and which would swallow the
# streak's only warning exactly when autostarted gateways hit a bad store.
# Reset to None on a successful refresh so each NEW streak warns again.
_WS_COUNTS_WARN_INTERVAL_SECS = 600.0
_last_counts_warn_monotonic: float | None = None

# Gateway-wide count cache: ONE store touch per TTL no matter how many sockets
# are open. Per-connection caches would make every dashboard tab an
# independent contender on the vector store's shared sqlite connection, whose
# _db_lock a busy-timeout read can hold for seconds while loop-thread readers
# (get_semantic_context et al.) block on it uninstrumented — N tabs polling
# independently is exactly the loop-freeze class no-blocking-call-on-event-loop
# exists to prevent. All four cells are only ever read/written from the event
# loop thread, so no lock is needed; the in-flight flag makes the refresh
# single-flight (a second socket's tick returns the stale cache immediately
# instead of piling a duplicate store read onto the executor).
_counts_cache: tuple[int | None, int | None] = (None, None)
_counts_cache_ts: float = float("-inf")
_counts_cache_failures: int = 0
_counts_refresh_inflight: bool = False


def _counts_refresh_decision(failures: int, error: str | None) -> tuple[bool, int, bool]:
    """Pure decision after one count-refresh attempt: ``(stamp_ttl, failures, warn)``.

    Success (``error is None``) re-arms the cache TTL and resets the streak. A
    failure leaves the TTL un-stamped so the next pusher tick (~5s) retries —
    fast recovery for TRANSIENT faults — but once the streak reaches
    ``_WS_COUNTS_WARN_AFTER_FAILURES`` the TTL is stamped even on failure,
    degrading a PERSISTENT fault to the normal 30s cadence instead of
    hammering the store and the shared default executor every tick. ``warn``
    is True exactly once per streak, at the threshold. Extracted as a pure
    function so the cache policy is testable without driving a WebSocket.
    """
    if error is None:
        return True, 0, False
    failures += 1
    return (
        failures >= _WS_COUNTS_WARN_AFTER_FAILURES,
        failures,
        failures == _WS_COUNTS_WARN_AFTER_FAILURES,
    )


def _warn_counts_failure(failures: int, error: str | None) -> None:
    """One operator-visible warning per streak, rate-limited gateway-wide.

    The per-attempt causes are logged at debug; without this line a permanent
    fault (store never initializes) would pin cached/unknown counts forever
    with no trace at default log level. Module-level latch (the event loop is
    single-threaded, so no lock) keeps repeat streaks within the interval from
    spamming; a successful refresh clears the latch so the NEXT streak warns.
    """
    global _last_counts_warn_monotonic
    now = time.monotonic()
    if (
        _last_counts_warn_monotonic is not None
        and now - _last_counts_warn_monotonic < _WS_COUNTS_WARN_INTERVAL_SECS
    ):
        return
    _last_counts_warn_monotonic = now
    logger.warning(
        "ws: status counts failed %d consecutive refreshes (%s); "
        "serving cached values, retrying at the normal cadence",
        failures,
        error,
    )


async def _refresh_status_counts(state: DashboardState) -> tuple[int | None, int | None]:
    """Return the gateway-wide cached counts, refreshing at most once per TTL.

    Callable every pusher tick from every connection: it returns the shared
    cache immediately unless this call is the one that finds it stale (and no
    refresh is already in flight), in which case it awaits one off-loop load
    and applies ``_counts_refresh_decision``. Single-flight + shared cache =
    one store touch per TTL for the whole gateway, however many sockets are
    open, and no per-socket count divergence.
    """
    global _counts_cache, _counts_cache_ts, _counts_cache_failures
    global _counts_refresh_inflight, _last_counts_warn_monotonic
    now = time.monotonic()
    if _counts_refresh_inflight or now - _counts_cache_ts < _WS_COUNTS_CACHE_TTL:
        return _counts_cache
    _counts_refresh_inflight = True
    try:
        crons, lessons, error = await _load_status_counts(state, fallback=_counts_cache)
        _counts_cache = (crons, lessons)
        stamp, _counts_cache_failures, warn = _counts_refresh_decision(
            _counts_cache_failures, error
        )
        if stamp:
            _counts_cache_ts = now
        if error is None:
            # New streaks warn again: the rate-limit floor is for repeats
            # WITHIN one streak, not for distinct outages.
            _last_counts_warn_monotonic = None
        elif warn:
            _warn_counts_failure(_counts_cache_failures, error)
        return _counts_cache
    finally:
        _counts_refresh_inflight = False


def _status_frame(
    state: DashboardState, *, crons: int | None, lessons: int | None
) -> dict[str, Any]:
    """Build the Tier-0 ``dashboard`` frame payload.

    ``status_snapshot`` computes a missing count INLINE on the event loop
    (that is its contract for the HTTP/SSE callers), so a sentinel 0 is passed
    to suppress that, and the two keys are then overwritten with the true
    cached values — which are ``None`` (rendered as a loading skeleton) until
    the first successful refresh. The overwrite half is load-bearing: without
    it the sentinel 0 ships as an authoritative count, which is the false-zero
    #7204 fixes. Module-level (not a closure) so a test can pin exactly that.
    """
    return {
        **state.status_snapshot(
            cron_jobs=crons if crons is not None else 0,
            lessons=lessons if lessons is not None else 0,
            **status_update_fields(),  # type: ignore[arg-type]
        ),
        "cron_jobs": crons,
        "lessons": lessons,
        "version": _local_version,
        "platform": sys.platform,
    }


# Reconnect replay: more subagent frames than this collapse into ONE
# subagent_snapshot_batch frame (scale plumbing — a per-agent burst at
# 60-100 agents saturates the socket the moment a client reconnects).
SUBAGENT_REPLAY_BATCH_THRESHOLD = 8

SIDE_RESULT_EVENT = "chat.side_result"
SIDE_QUEUE_EVENT = "chat.side_queue"
SIDE_KIND = "side"


def _subagent_replay_has_owner(frame: object) -> bool:
    """Whether a replay frame names the chat that owns the subagent.

    ``spawn_run`` can continue in degraded mode when caller identity cannot be
    resolved, leaving ``parent_session_key == ""``. Such a run is visible in the
    global spawn inventory, but it has no session-scoped destination. Sending an
    empty ``slot`` to a chat client is unsafe: older reducers interpreted it as
    the currently active slot, so a fresh popout adopted unrelated agents.
    """
    if not isinstance(frame, dict):
        return False
    data = frame.get("data")
    if not isinstance(data, dict):
        return False
    slot = data.get("slot")
    return isinstance(slot, str) and bool(slot.strip())


def build_subagent_snapshot(a: Any, *, now: float | None = None) -> dict:
    """Build the ``subagent_snapshot`` replay frame's ``data`` for one agent.

    Extracted from the reconnect handler so the frame's CONTENTS can be
    asserted directly — the handler around it needs a live aiohttp WS, which is
    why the omission this fixes went unnoticed.

    ``idle_secs`` is the span that justifies the stall badge. The live
    ``subagent_stalled`` event carries it; this replay frame did not, so ANY
    reconnect during an active stall degraded the row to the plain
    "no activity" wording that was only ever meant for a gateway too old to
    send the field (#3929).

    It is computed at replay time rather than replaying the original transition
    value: by reconnect the agent has usually been idle longer than it was when
    flagged, and ``last_activity`` is already the field the reaper itself
    measures. Clamped at 0 so a clock adjustment cannot produce a negative span.

    The key is OMITTED entirely when the agent is not stalled, so a client
    cannot attach an idle span to a healthy row — the reducer pairs the span
    with the flag and would otherwise have to defend against the mismatch.
    """
    ts = time.time() if now is None else now

    def _r(t: str) -> str:
        t, _ = redact_exfiltration_urls(t)
        t, _ = redact_credentials(t)
        return t

    data: dict = {
        "id": a.id,
        "slot": subagent_event_slot(a.parent_session_key),
        # The sub-agent's OWN session key (where it writes its ctx_blocks /
        # token rows), so a client can fetch this node's own context-trace and
        # render its window composition. Mirrors the run key derived in
        # SubagentManager._run: `conversation_key or subagent:<id>`.
        "child_session": getattr(a, "conversation_key", "") or f"subagent:{a.id}",
        "task": _r(a.task),
        "agent": _r(a.agent),
        "model": a.resolved_model,
        "requested_model": _r(a.requested_model),
        "streaming": _r(a.streaming_text),
        "last_tool": _r(a.last_tool),
        "tool_count": a.tool_count,
        "stalled": a.stalled,
    }
    if a.stalled:
        data["idle_secs"] = max(0, int(ts - a.last_activity))
    data["started"] = a.started
    return data


def _audit_grant_quietly(app: str, event: str) -> None:
    """Record a WS grant made on a path that bypasses the broadcast chokepoint.

    Three sends reach an app socket directly rather than through
    ``_send_ws_all`` -- the initial slots push (specifically its ``yolo``
    envelope field), the periodic ``dashboard`` status frame, and the
    ``subscribe_logs`` ring replay -- so ``ws_event_allowed`` never sees them
    and none of them would otherwise leave an SEL record, even though each is
    a permission decision ``AUTOSDE.yaml`` requires one for.

    One helper rather than the same ``try``/``except`` inlined at each site:
    the swallow is the load-bearing part and needs to behave identically
    everywhere. A failing audit sink must never drop a frame the app is
    entitled to, so the exception is logged and delivery continues -- and
    having a single copy means that branch is exercised by one test instead of
    being three separate never-executed paths.
    """
    try:
        _audit_allow(app or "<unknown>", event)
    except Exception:
        logger.debug("ws: SEL audit for %s grant failed", event, exc_info=True)


async def _load_status_counts(
    state: DashboardState, *, fallback: tuple[int | None, int | None] = (None, None)
) -> tuple[int | None, int | None, str | None]:
    """Return ``(cron_count, lesson_count, error)`` loaded OFF the event loop.

    ``DashboardState._count_lessons()`` performs blocking I/O on two stores:
    the JSONL file (``stat()`` + ``read_text()`` via ``load_all``) PLUS a
    SQLite ``COUNT(*)`` via ``VectorMemoryStore.count_lessons`` (serialized on
    the shared connection through ``_fetch_all_locked``, documented as
    executor-thread safe). The cron count comes from a direct read-only parse
    of ``crons.json`` (``count_enabled_from_disk``). The WS status pusher runs
    on the event loop, so computing these inline would stall the loop — and
    with it EVERY other WebSocket / coroutine on the gateway — for the
    duration of that disk latency (seconds on a slow/large home dir or a
    contended NFS mount). Offload both to a worker thread so the loop stays
    responsive; the pusher is a periodic background task, so the extra thread
    hop is free.

    The lesson count MUST come from ``_count_lessons`` (JSONL + vector store),
    the same total ``/api/status`` and the SSE updates path report via
    ``status_snapshot``'s default (those two callers still compute it inline
    on the loop; only this path offloads). Counting only ``lessons.load_all()``
    here made the pusher's cached value override the correct default with the
    JSONL-only half, so the Overview card showed 0 on hosts whose lessons
    live in the vector store (issue #7204).

    Each count is guarded INDEPENDENTLY: on failure that component falls back
    to its ``fallback`` half while the other keeps its fresh value — the
    vector-store read can surface ``sqlite3.OperationalError`` (busy timeout,
    disk I/O) or ``RuntimeError`` (store not initialized), and an exception
    escaping into ``_push_status``'s loop would silently end that connection's
    status frames, losing the version/liveness signal until a page reload. A
    lessons failure must not also discard a successfully-read cron count.
    ``None`` means UNKNOWN, never 0: the pusher seeds its cache with ``None``
    so a component that has never refreshed successfully is published as
    ``null`` (the dashboard renders a loading skeleton) instead of an
    authoritative-looking 0 — the exact false-zero #7204 fixes. ``error``
    joins each failed component's exception TYPE name (``None`` on full
    success) so the operator-visible warning can name the cause without
    leaking store paths (``str(OSError)`` embeds its filename); it never
    enters the WS frame. The guards catch ``Exception`` only, so
    ``asyncio.CancelledError`` (a ``BaseException``) propagates out of THIS
    helper uncaught — that is this function's contract; the pusher's own
    outer handler decides its task's teardown semantics.

    NOTE: this deliberately uses ``count_enabled_from_disk`` rather than
    ``list_jobs``. ``list_jobs`` calls ``_sync()`` → ``_load()`` → ``_arm_timer()``,
    and ``_arm_timer`` calls ``asyncio.create_task`` — with no running loop in a
    worker thread that raises ``RuntimeError``, and since ``_arm_timer`` cancels
    the existing timer first it would silently stop all scheduled cron jobs.
    ``count_enabled_from_disk`` is a pure read that never mutates loop-owned
    state or the timer, so it is safe off-thread.
    """
    errors: list[str] = []
    try:
        crons: int | None = await asyncio.to_thread(state.crons.count_enabled_from_disk)
    except Exception as exc:
        logger.debug("ws: cron count refresh failed; keeping cached count", exc_info=True)
        # Exception TYPE only: str()/repr() of an OSError embeds the absolute
        # store path (the operator's username) via its filename attribute, and
        # this string reaches logger.warning at default level. The full
        # traceback is already in the debug log above. Never the WS frame.
        crons = fallback[0]
        errors.append(f"crons: {type(exc).__name__}")
    try:
        lessons: int | None = await asyncio.to_thread(state._count_lessons)
    except Exception as exc:
        logger.debug("ws: lesson count refresh failed; keeping cached count", exc_info=True)
        lessons = fallback[1]
        errors.append(f"lessons: {type(exc).__name__}")
    return crons, lessons, "; ".join(errors) or None


def broadcast_side_result(
    state: DashboardState,
    *,
    slot_key: str,
    run_id: str,
    role: str,
    content: str,
    is_error: bool = False,
    final: bool = False,
    ts: float | None = None,
    steer: bool = False,
) -> None:
    """Broadcast a side conversation event on the dedicated side channel.

    Emits ``{type: "chat.side_result", data: payload}`` to all WS clients.
    The event name and payload shape are reused from the upstream OpenClaw
    `/btw` protocol so a future shared client can interop. ``kind`` is
    translated from upstream ``"btw"`` to KiroCrew's ``"side"``.

    The event channel is intentionally separate from ``chat_message`` so
    receivers that don't subscribe to side simply don't see it; this
    keeps side deltas out of the main transcript by construction.
    Receiver-side run-ID isolation is the frontend's responsibility via
    ``local_side_run_ids``.

    Set final=True on the terminal frame of a side turn so the frontend
    can flip the streaming flag off cleanly.

    No payload field is persisted — sidecar-only, ephemeral.
    """
    payload: dict[str, object] = {
        "kind": SIDE_KIND,
        "slot": slot_key,
        "run_id": run_id,
        "role": role,
        "content": redact_credentials(redact_exfiltration_urls(content)[0])[0],
        "ts": ts if ts is not None else time.time(),
    }
    if is_error:
        payload["is_error"] = True
    if final:
        payload["final"] = True
    if steer:
        payload["steer"] = True
    # Owner-only, matching the queue frame and `_check_slot_ownership`: side answers and
    # steer echoes are the owner's own conversation, and an app that asks the HTTP API
    # about a slot it does not own gets a 404.
    state.broadcast_ws_owners(SIDE_RESULT_EVENT, payload)


def broadcast_side_queue(
    state: DashboardState,
    *,
    slot_key: str,
    action: str,
    queue_id: str,
    content: str = "",
    depth: int = 0,
    front: bool = False,
    steer_id: str = "",
    origin_client: str = "",
) -> None:
    """Broadcast a side-queue mutation on the dedicated side channel.

    ``action`` is one of ``push`` | ``edit`` | ``cancel`` | ``drain``. ``drain``
    fires when the entry leaves the queue to become the next side turn, so the
    frontend can retire its card without waiting for the user frame. ``depth``
    is the queue length AFTER the mutation, letting a client that missed a frame
    resync its badge without a refetch.

    ``front`` says the entry went to the HEAD of the queue rather than the tail —
    which is how a requeued steer and a failed drain's entry land. Without it a
    client appends them and shows a different next question than the backend will
    actually run.

    Kept separate from ``chat.side_result`` so a queue mutation never enters the
    transcript reducer, and separate from the main chat's ``queue_push`` so side
    queue entries can never be mistaken for parent-slot turns.
    """
    payload: dict[str, object] = {
        "kind": SIDE_KIND,
        "slot": slot_key,
        "action": action,
        "queue_id": queue_id,
        "depth": depth,
        "ts": time.time(),
    }
    if front:
        payload["front"] = True
    if steer_id:
        # Not sensitive — an opaque ledger id. It lets the submitting client match
        # its own RAW steer text to this card, whose content is redacted here.
        payload["steer_id"] = steer_id
    if content:
        payload["content"] = redact_credentials(redact_exfiltration_urls(content)[0])[0]
    if origin_client:
        # Not sensitive — an opaque per-tab id. It lets a tab recognise its OWN action's echo,
        # so only the tab that cancelled takes the question back into its composer.
        payload["origin_client"] = origin_client
    # Owner-only: `_check_slot_ownership` answers 404 when an app asks about a slot it
    # does not own, and queue entries are the user's own prose. An unscoped broadcast
    # would hand that text to app sockets the HTTP layer keeps out.
    state.broadcast_ws_owners(SIDE_QUEUE_EVENT, payload)


def _handle_slot_focused(
    state: DashboardState,
    slot_key: object,
    prev_task: "asyncio.Task | None",
    *,
    owner: bool,
) -> "asyncio.Task | None":
    """React to a client's ``slot_focused`` frame with a resume prefetch.

    Owner-only: an app-scoped socket is allowed on ``/api/ws`` for its own
    event streams, but a prefetch starts (or lets it cancel) owner-session
    processes and takes kiro-cli's native per-session lock — a permission
    boundary an app token does not cross. Non-owner frames are ignored
    entirely, including the cancel: ``prev_task`` can only be non-None for a
    socket that was owner when it armed one.

    Focusing a slot whose session is persisted but not live starts the
    speculative ``session/load`` (resume prefetch), overlapping the
    multi-second transcript replay with the user reading that history in the
    UI. ``prev_task`` is the prefetch THIS socket's previous focus armed;
    it is cancelled on every focus change so rapid tab flipping settles into
    at most one pending prefetch per connection — only the task this path
    created is touched, never one armed by the slot-create/project-set
    intent signals. ``slot_key`` of ``None``/empty means blur (tab hidden,
    no focused slot): cancel and do nothing else.

    Returns the task now pending for this socket, if any.
    """
    # circular import: ws -> chat_runner -> handlers/__init__ -> handlers/side -> ws
    from kiro_crew.dashboard.chat_runner import schedule_eager_spawn

    if not owner:
        return prev_task
    if prev_task is not None and not prev_task.done():
        prev_task.cancel()
    if not isinstance(slot_key, str) or not slot_key:
        return None  # blur
    slot = state.get_slot(slot_key)
    if slot is None or slot.running:
        return None
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return None
    session_key = effective_session_key(slot)
    if sessions.has_session(session_key):
        return None  # already live (in-memory check) — nothing to prefetch
    # Loop-safe resumability HINT (in-memory membership, no disk, no pruning —
    # the pruning ``resumable_sid`` lookup stays inside the spawn task's
    # get_or_create resume path). Checked HERE so a non-resumable slot never
    # reaches schedule_eager_spawn: creating a slot focuses it, and the focus
    # frame arriving behind the create signal would otherwise CANCEL the
    # create-armed fresh spawn (schedule_eager_spawn keeps one task per slot)
    # and then no-op — silently gutting the fresh eager-spawn path for every
    # new slot. Non-resumable focus preserves whatever task is pending.
    if not sessions.resumable_hint(session_key):
        return None
    return schedule_eager_spawn(state, slot, allow_resume=True)


def _check_ws_origin(request: web.Request) -> None:
    """Reject cross-origin WebSocket upgrades.

    Browsers always send an Origin header on WebSocket handshakes.
    We allow only the dashboard's own origins and reject everything else,
    including missing Origin (non-browser clients are not expected).
    """
    if not check_origin(request, require=True):
        raise web.HTTPForbidden(text="WebSocket origin not allowed")


async def api_ws(request: web.Request) -> web.WebSocketResponse:
    """GET /api/ws — single multiplexed WebSocket for all real-time events."""
    _check_ws_origin(request)

    from kiro_crew.dashboard.handlers import _log_ring

    state: DashboardState = request.app["state"]
    from kiro_crew.dashboard.handlers.source_providers import (
        CHECK_STATUS_PENDING_MAX,
        CHECK_STATUS_TTL_SECS,
        ensure_gitlab_hosts_loaded,
        gitlab_hosts_generation,
        is_owner_dashboard_request,
        schedule_check_refresh,
        schedule_visibility_refresh,
    )
    from kiro_crew.platform.context import governance_generation

    owner_request = is_owner_dashboard_request(request)
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    # Warm the self-managed GitLab allowlist BEFORE the first serialization.
    # Slot source-link extraction is synchronous and cannot load it, so without
    # this the initial sidebar would omit every self-hosted MR chip until some
    # later provider request happened to populate the snapshot.
    # Done BEFORE register_ws: this awaits, and a cancellation here would
    # otherwise leave the socket registered with no cleanup scope to unregister
    # it (the finally below is only entered after registration succeeds).
    try:
        await ensure_gitlab_hosts_loaded()
    except Exception:
        logger.debug("GitLab allowlist warm-up failed; chips may lag one round", exc_info=True)

    # Resolve the app token's scope BEFORE registering, and refuse a disabled app
    # outright. ``disable_app`` does not invalidate the app token (``token_auth``
    # has no enablement check), so a disabled app can reconnect at will — and
    # reading only ``app.json`` here would hand it a FULL snapshot from the intact
    # manifest, which the initial slots push and the log replay are then judged
    # against before any background refresh runs. The read also primes the
    # revocation cache, so the first frame is gated on an authoritative answer
    # rather than on the cold-miss fallback.
    #
    # Refusing (rather than admitting at Tier 0, which is what an ALREADY-OPEN
    # socket narrows to) is free here: at connect there is no in-flight streaming
    # turn to cut, which was the reason narrowing does not close live sockets.
    #
    # Done BEFORE register_ws for the same reason as the warm-up above: this
    # awaits, and refusing after registration would need the cleanup scope that
    # the finally below only establishes once registration succeeds.
    ws_app: str = request.get("app", "")
    allowed_events: frozenset[str] = frozenset()
    if ws_app:
        try:
            # The load stats + reads + JSON-parses the manifest with no internal
            # cache, so it is offloaded: this runs for EVERY app WS connect (and
            # reconnect storms are the norm after a gateway restart), and on slow
            # or contended storage a blocking read here stalls every other
            # request and the heartbeat with it.
            app_enabled, allowed_events = await asyncio.to_thread(
                load_declared_events_for_connect, ws_app
            )
        except Exception:
            # Indeterminate — do not refuse on a read error (that would drop a
            # working app over a transient filesystem fault), but grant nothing:
            # every declared scope is withheld and only Tier 0 gets through.
            logger.debug("ws: could not resolve scope for app %r", ws_app, exc_info=True)
            app_enabled, allowed_events = True, frozenset()
        if not app_enabled:
            logger.info("ws: refusing /api/ws for disabled app %r", ws_app)
            await ws.close(code=WSCloseCode.POLICY_VIOLATION, message=b"app disabled")
            return ws

    state.register_ws(ws, owner=owner_request)

    # Store app identity on the WS connection so the broadcast chokepoint can
    # filter. ``_is_dashboard_user`` comes from a POSITIVE signal produced by
    # the auth middleware (``request["is_dashboard_user"]``) — it is never
    # inferred from the absence of ``_app`` here. If a refactor reaches
    # ``api_ws`` without passing through that middleware, the flag defaults to
    # False and ``_send_ws_all`` keeps its deny-by-default behaviour.
    ws["_app"] = ws_app
    ws["_is_dashboard_user"] = request.get("is_dashboard_user", False)
    ws["_allowed_events"] = allowed_events

    # Push current slots immediately so sidebar populates without waiting.
    # App tokens get only the slots their manifest scope allows.
    # Read the ceiling generation ONCE here and seed both the initial frame and
    # the refresh loop's baseline from it. Two independent reads would leave a
    # gap: a ceiling swapped between them is already the loop's baseline, so the
    # loop never pushes, while the client still holds the number the frame sent —
    # the change would be missed until an unrelated slot mutation.
    initial_ceiling_generation = governance_generation()
    try:
        is_dashboard_user = ws.get("_is_dashboard_user", False)
        all_slots = state.serialize_slots(
            include_check_status=owner_request, dashboard_user=is_dashboard_user
        )
        if is_dashboard_user:
            slots_data = all_slots
        elif ws_app:
            slots_data = filter_slots_for_app(all_slots, ws_app, allowed_events, state)
        else:
            # Unknown identity (neither flag nor app) — deny by default.
            slots_data = []
        # ``yolo`` is the live blanket-approval override, i.e. operator security
        # posture rather than slot data, so an app token sees it only with the
        # scope that already gates ``yolo_expired``. Dashboard users always do.
        # Same decision as the broadcast re-push in
        # ``DashboardState._serialize_for_client`` — routed through the gate's
        # helper so the two cannot drift.
        envelope_extras: dict[str, object] = (
            {"yolo": state._yolo}
            if ws.get("_is_dashboard_user", False)
            else dict(slots_envelope_extras(allowed_events, yolo=state._yolo))
        )
        # Seed the folder tree on the CONNECT-TIME push (dashboard users only) —
        # this is the frame that populates the sidebar on a cold page load, so it
        # is where the client must receive `folders` to group sessions on the
        # first paint (issue #4127). The broadcast path (_do_slots_broadcast) also
        # carries it for live folder create/rename/move, but on an idle-gateway
        # load no broadcast fires before GET /api/chat/folders resolves, so
        # without this the ungrouped→regrouped flicker survives. App tokens are
        # excluded (they do not render the chat folder tree), matching the
        # broadcast decision. `_safe_folder_tree` drops history_count and any
        # malformed entry (see its docstring).
        if ws.get("_is_dashboard_user", False):
            envelope_extras["folders"] = _safe_folder_tree(getattr(state, "_folders", None))
            # Baseline for the change comparison, alongside the tree it describes
            # — the client treats a connection's first generation as "unknown,
            # refetch", so this seeds the number a later bump is measured against.
            # Gated with `folders` rather than sent unconditionally: an app token
            # never receives the tree, so its generation would describe data the
            # app does not have.
            envelope_extras["foldersGeneration"] = state.folders_generation()
        if not ws.get("_is_dashboard_user", False) and "yolo" in envelope_extras:
            # Handing an app token the live blanket-approval override is a
            # grant of operator security posture, not slot data, and this
            # initial push writes to the socket directly -- so record it here
            # or it goes unrecorded entirely.
            _audit_grant_quietly(ws_app, "slots_yolo")
        await ws.send_json(
            {
                "type": "slots",
                "data": slots_data,
                **envelope_extras,
                # Seed the client's generation baseline so a later change is
                # detectable as a change rather than as a first sighting.
                "gitlabHostsGeneration": gitlab_hosts_generation(),
                "governanceGeneration": initial_ceiling_generation,
            }
        )
        if owner_request or is_dashboard_user:
            # Issue links carry no check status — skip them so the scheduler
            # never hands an issue URL to the pull-request-only chip fetch.
            urls = [
                link["url"]
                for payload in slots_data
                for link in payload.get("source_links", [])
                if link.get("kind", "change") == "change"
            ]
            if urls:
                # Both the status refresh AND the visibility probe run the
                # operator's `gh`/`glab` credentials, so a NON-owner connection
                # must trigger NEITHER — otherwise a non-owner would cause
                # authenticated provider reads (status content AND repo
                # visibility metadata) on repos it has no right to drive traffic
                # for (GPT #6789). Only the OWNER's connection refreshes the
                # caches; a non-owner is READ-ONLY against them. The owner is the
                # dashboard operator and is effectively always connected, so its
                # driver classifies each repo's visibility and fetches public
                # status once, and every authenticated non-owner viewer then
                # renders that cached public-repo status via the fail-closed
                # `is_repo_public` gate in `_project_source_links`. A repo the
                # owner has never classified stays owner-only for non-owners
                # (fail closed) — no non-owner-driven credentialed probe.
                if owner_request:
                    schedule_visibility_refresh(urls, state.push_slots_update)
                    schedule_check_refresh(urls, state.push_slots_update)
    except Exception:
        pass

    # Background task: push dashboard status periodically
    async def _push_status() -> None:
        # Governance-ceiling watch rides this tick rather than a task of its own.
        # Seeded from the value the initial slots frame carried, not a fresh read:
        # the client's baseline IS that value, so a swap since then must register
        # here as a change or the two sides disagree with no push to reconcile.
        ceiling_generation = initial_ceiling_generation
        try:
            while not ws.closed and not shutdown_event.is_set():
                # Gateway-wide cache: one store touch per TTL across ALL
                # sockets; this call returns the shared cache immediately
                # unless it is the one that refreshes it. Counts are None
                # (published as null → loading skeleton) until the first
                # successful refresh — never an authoritative false 0 (#7204).
                _cached_crons, _cached_lessons = await _refresh_status_counts(state)
                data = _status_frame(state, crons=_cached_crons, lessons=_cached_lessons)
                if not ws.get("_is_dashboard_user", False):
                    # This frame is Tier 0 — always delivered, because every
                    # client needs the version (to force a reload across a
                    # gateway upgrade) and the liveness signal. That only holds
                    # while the payload stays counts-and-environment: the
                    # checkout's branch and commit say what the operator is
                    # working ON, which is not an app's business and has no
                    # consumer outside the owner surfaces. Strip them here
                    # rather than moving the whole frame behind a declaration,
                    # which would silently cut every existing app off from the
                    # version signal. ``/api/status`` and the SSE stream run on
                    # dashboard-user tokens and keep the full snapshot.
                    for _owner_only in ("branch", "commit"):
                        data.pop(_owner_only, None)
                    # Tier 0 admits every app unconditionally, but the decision
                    # is still a grant per ``AUTOSDE.yaml`` -- this frame is
                    # sent directly rather than through the broadcast
                    # chokepoint, so nothing else records it. The dedup window
                    # already bounds the 5-second interval to one record.
                    _audit_grant_quietly(ws_app, "dashboard")
                try:
                    await ws.send_json({"type": "dashboard", "data": data})
                except Exception:
                    break
                # A centrally pushed policy (``policy_distribution.apply_ceiling``)
                # swaps the ceiling between slot mutations, and the dashboard-config
                # fields derived from it (``social_share_enabled``) would otherwise
                # keep their cached answer until an unrelated message carried the
                # new generation. Every dashboard-user socket watches — owner or
                # not — so a fleet that tightened policy while only a non-owner
                # window was open still reaches that window. Not folded into the
                # owner-only credential driver below: this compares one counter and
                # asks for a (coalesced) slots push, which every dashboard
                # connection may do, and spends no credentials.
                if ws.get("_is_dashboard_user", False):
                    try:
                        if governance_generation() != ceiling_generation:
                            ceiling_generation = governance_generation()
                            state.push_slots_update()
                    except Exception:
                        logger.warning("governance watch tick failed; continuing", exc_info=True)
                await asyncio.sleep(_WS_STATUS_INTERVAL)
        except (asyncio.CancelledError, Exception):
            pass

    status_task = asyncio.create_task(_push_status())

    # Background task (owner connections only): keep sidebar PR/MR chip
    # status fresh. push_slots_update serves the *cached* check status but
    # never schedules refreshes — without a periodic driver the cache is only
    # populated at connect / slots-GET time, so chips freeze at their initial
    # state (e.g. a PR merged after page load never gains the merge icon).
    # schedule_check_refresh is TTL-gated and inflight-deduped, so multiple
    # owner connections still cost at most one provider fetch per URL per
    # TTL, and on_update broadcasts only when a status actually changed.
    async def _refresh_check_loop() -> None:
        # Rotate the starting offset each round. schedule_check_refresh admits
        # at most CHECK_STATUS_PENDING_MAX URLs per call and backs the rest off
        # for one TTL; because every chip expires in lockstep, feeding URLs in
        # the same slot order every round would let the first-N win forever and
        # starve later chips (deterministic with >N PR-linked slots). Advancing
        # the offset by the admission cap each round cycles every chip through
        # the admitted window within ceil(len/cap) rounds.
        refresh_round = 0
        hosts_generation = gitlab_hosts_generation()
        while not ws.closed and not shutdown_event.is_set():
            # Guard the body (not the whole loop) so a single transient failure
            # from source_link_urls()/schedule_check_refresh logs and continues
            # instead of silently killing the driver and reverting to the
            # frozen-chip bug this loop exists to fix.
            try:
                await asyncio.sleep(CHECK_STATUS_TTL_SECS)
                # Re-read the allowlist off-loop on the same cadence. A host the
                # operator added (or revoked) changes which links are chips at
                # all, and slot extraction is synchronous, so a generation change
                # has to be pushed explicitly -- otherwise the new/removed chip
                # waits for an unrelated message mutation.
                await ensure_gitlab_hosts_loaded()
                if gitlab_hosts_generation() != hosts_generation:
                    hosts_generation = gitlab_hosts_generation()
                    state.push_slots_update()
                urls = state.source_link_urls()
                if urls:
                    offset = (refresh_round * CHECK_STATUS_PENDING_MAX) % len(urls)
                    urls = urls[offset:] + urls[:offset]
                    # Owner-only driver (see _run_status_driver): both refreshes
                    # run operator credentials, so only the owner's connection
                    # drives them. This keeps the check + visibility caches warm
                    # for every repo the owner's slots reference; non-owner
                    # viewers render the resulting cached public-repo status
                    # read-only via is_repo_public. No non-owner-driven
                    # credentialed provider read (GPT #6789).
                    schedule_visibility_refresh(urls, state.push_slots_update)
                    schedule_check_refresh(urls, state.push_slots_update)
                refresh_round += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("check-status refresh round failed; continuing", exc_info=True)

    # Run the refresh driver ONLY for the owner connection: both the status and
    # the visibility refresh call the operator's `gh`/`glab` credentials, so a
    # non-owner must never drive them (GPT #6789). The owner is the dashboard
    # operator and is effectively always connected, so its driver keeps the
    # check + visibility caches warm for every repo its slots reference; a
    # non-owner dashboard connection renders the resulting cached PUBLIC-repo
    # status read-only (via the fail-closed is_repo_public gate) and spawns no
    # provider subprocess. App tokens never render status either way.
    _run_status_driver = owner_request
    check_task = asyncio.create_task(_refresh_check_loop()) if _run_status_driver else None
    # The resume prefetch this socket's most recent slot_focused frame armed.
    # Tracked per connection so a focus change (or blur/disconnect) cancels
    # only this socket's speculation, never another window's.
    _focus_task: "asyncio.Task | None" = None
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get("type", "")
                    if msg_type == "subscribe_logs":
                        # The gateway log stream is privileged. The broadcast
                        # chokepoint filters future ``log`` events, but the
                        # ring-buffer replay below bypasses it — gate at the
                        # source. Positive-flag check (CWE-269): a falsy
                        # ``_app`` alone must not open this.
                        # Accept `log:all` as well. The per-event chokepoint
                        # takes `<decl>` OR `<decl>:all`, so declaring
                        # `log:all` let LIVE log events through while this
                        # replay gate -- checking only the bare form -- refused
                        # the buffered history: same declaration, two answers.
                        # Resolve the LIVE scope, not the connect-time snapshot:
                        # this replays the whole ring, so a scope revoked (or an
                        # app disabled) after connect must not be able to pull
                        # the buffered history. Mirrors the per-send re-check in
                        # handlers/updates._safe_ws_send.
                        _live = effective_allowed_events(ws_app, allowed_events)
                        if not ws.get("_is_dashboard_user", False) and not (
                            "log" in _live or "log:all" in _live
                        ):
                            try:
                                _audit_deny(
                                    ws_app or "<unknown>",
                                    "subscribe_logs",
                                    "log_scope_not_declared",
                                )
                            except Exception:
                                logger.debug(
                                    "ws: SEL audit for subscribe_logs deny failed",
                                    exc_info=True,
                                )
                            continue
                        if not ws.get("_is_dashboard_user", False):
                            # Mirror the deny branch above: the grant is a
                            # permission decision too, and only the deny side
                            # left an SEL record before this.
                            _audit_grant_quietly(ws_app, "subscribe_logs")
                        state.subscribe_logs(ws)
                        # Replay log ring buffer
                        for entry in list(_log_ring):
                            try:
                                parsed = json.loads(entry)
                                await ws.send_json({"type": "log", "data": parsed})
                            except Exception:
                                pass
                    elif msg_type == "unsubscribe_logs":
                        state.unsubscribe_logs(ws)
                    elif msg_type == "subscribe_subagents":
                        # No declaration-level gate here on purpose. Owning
                        # your own slots is the DEFAULT, not something a
                        # manifest opts into, so refusing the subscription when
                        # nothing matched ``subagent*``/``slots:*`` starved an
                        # app of its OWN slot's replay — the one thing it is
                        # always entitled to. Every replay frame below still
                        # goes through the per-frame gate, which is where the
                        # scope decision belongs; a subscription that is
                        # allowed to exist but yields nothing visible is the
                        # correct shape for an app that declared no extra
                        # scope.
                        state.subscribe_subagents(ws)

                        def _r(t: str) -> str:
                            t, _ = redact_exfiltration_urls(t)
                            t, _ = redact_credentials(t)
                            return t

                        # Collect every replay frame first; below the scale
                        # threshold they are sent individually, above it they
                        # collapse into ONE subagent_snapshot_batch frame — at 60-100 agents
                        # a per-agent replay burst saturates the socket the
                        # moment a client reconnects.
                        _replay: list[dict] = []

                        # Native kiro-cli subagents run inside dashboard chat
                        # slots, not the global SubagentManager. Replay their
                        # slot-owned in-flight state before manager snapshots.
                        # Running cards replay as snapshots; cards that finished
                        # while the socket was down replay as done events so the
                        # terminal card + output survive the reconnect clear.
                        for native in state.native_subagent_snapshots():
                            try:
                                if native.get("done"):
                                    _err = native.get("error")
                                    # Same precedence the producer uses, for a
                                    # snapshot that carries no outcome of its own.
                                    if native.get("stopped"):
                                        _outcome = "stopped"
                                    elif _err:
                                        _outcome = "failed"
                                    else:
                                        _outcome = "completed"
                                    _replay.append(
                                        {
                                            "type": "subagent_done",
                                            "data": {
                                                "id": native["id"],
                                                "slot": native["slot"],
                                                "elapsed": native["elapsed"],
                                                "error": _r(str(_err)) if _err else None,
                                                "stopped": bool(native.get("stopped")),
                                                "outcome": str(native.get("outcome") or _outcome),
                                                "task": _r(str(native["task"])),
                                                "agent": _r(str(native["agent"])),
                                                "result": _r(str(native["result"])),
                                            },
                                        }
                                    )
                                else:
                                    _replay.append(
                                        {
                                            "type": "subagent_snapshot",
                                            "data": {
                                                "id": native["id"],
                                                "slot": native["slot"],
                                                "task": _r(str(native["task"])),
                                                "agent": _r(str(native["agent"])),
                                                "streaming": _r(str(native["streaming"])),
                                                "last_tool": _r(str(native["last_tool"])),
                                                "started": native["started"],
                                            },
                                        }
                                    )
                            except Exception:
                                pass

                        # Snapshot of managed subagents + done events for completed ones
                        if state.subagents:
                            for a in state.subagents.running:
                                try:
                                    _replay.append(
                                        {
                                            "type": "subagent_snapshot",
                                            "data": build_subagent_snapshot(a),
                                        }
                                    )
                                except Exception:
                                    pass
                            # Done events for completed subagents so
                            # reconnecting clients can transition stale cards.
                            for a in state.subagents.all_agents:
                                if not a.done:
                                    continue
                                # Same slot mapping as the live frames — a raw
                                # prefix-strip tags replayed cards with a slot
                                # no tab reads, so the panel rehydrated empty
                                # after every reconnect for cron/channel tabs.
                                slot = subagent_event_slot(a.parent_session_key)
                                try:
                                    _replay.append(
                                        {
                                            "type": "subagent_done",
                                            "data": {
                                                "id": a.id,
                                                "slot": slot,
                                                "child_session": getattr(a, "conversation_key", "")
                                                or f"subagent:{a.id}",
                                                "elapsed": a.elapsed,
                                                "error": _r(a.error) if a.error else None,
                                                "stopped": a.user_stopped,
                                                "outcome": a.outcome,
                                                "task": _r(a.task),
                                                "agent": _r(a.agent),
                                                "model": a.resolved_model,
                                                "requested_model": _r(a.requested_model),
                                            },
                                        }
                                    )
                                except Exception:
                                    pass
                        # Per-slot scope gate on the reconnect replay. The
                        # broadcast chokepoint covers live events, but this
                        # replay writes to the socket directly, so it must
                        # apply the same check. Dashboard users pass through
                        # ``_ws_client_allowed`` unconditionally.
                        #
                        # Ownership is a separate prerequisite: an unresolved
                        # parent produces ``slot: ''``. That run remains visible
                        # through the global spawn inventory, but there is no
                        # chat authorized to adopt it. Filter before batching so
                        # even an older client with an empty-slot fallback never
                        # receives the orphan as session-scoped state.
                        _replay = [
                            _f
                            for _f in _replay
                            if _subagent_replay_has_owner(_f)
                            and state._ws_client_allowed(
                                ws, str(_f.get("type", "")), _f.get("data", {})
                            )
                        ]
                        try:
                            if len(_replay) > SUBAGENT_REPLAY_BATCH_THRESHOLD:
                                # ``subagent_snapshot_batch`` is deliberately
                                # absent from every ws_event_scope table: it is
                                # delivery packaging for frames THIS socket is
                                # already cleared for (filtered item-by-item
                                # above), never a broadcast. Routing it through
                                # the gate would reject it as an unknown event
                                # and cost the app its whole replay, so keep
                                # this send and the per-item filter together.
                                await ws.send_json(
                                    {"type": "subagent_snapshot_batch", "data": {"items": _replay}}
                                )
                            else:
                                for _frame in _replay:
                                    await ws.send_json(_frame)
                        except Exception:
                            pass
                    elif msg_type == "unsubscribe_subagents":
                        state.unsubscribe_subagents(ws)
                    elif msg_type == "slot_focused":
                        _focus_task = _handle_slot_focused(
                            state, data.get("slot"), _focus_task, owner=owner_request
                        )
                except (json.JSONDecodeError, Exception):
                    pass
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    except (asyncio.CancelledError, Exception):
        pass
    finally:
        status_task.cancel()
        if check_task is not None:
            check_task.cancel()
        # A prefetch still debouncing for a closed dashboard serves nobody.
        if _focus_task is not None and not _focus_task.done():
            _focus_task.cancel()
        state.unsubscribe_logs(ws)
        state.unsubscribe_subagents(ws)
        state.unregister_ws(ws)
    return ws
