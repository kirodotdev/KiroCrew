"""WebSocket endpoint — multiplexes all real-time events over a single connection."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time

from aiohttp import WSCloseCode, WSMsgType, web

from kiro_crew import __version__ as _local_version
from kiro_crew import shutdown_event
from kiro_crew.dashboard.origin import check_origin
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.ws_event_scope import (
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
# Reconnect replay: more subagent frames than this collapse into ONE
# subagent_snapshot_batch frame (scale plumbing — a per-agent burst at
# 60-100 agents saturates the socket the moment a client reconnects).
SUBAGENT_REPLAY_BATCH_THRESHOLD = 8

SIDE_RESULT_EVENT = "chat.side_result"
SIDE_KIND = "side"


async def _load_status_counts(state: DashboardState) -> tuple[int, int]:
    """Return ``(cron_count, lesson_count)`` loaded OFF the event loop.

    ``LessonStore.load_all()`` performs blocking file I/O (JSONL ``stat()`` +
    ``read_text()``) and the cron count comes from a direct read-only parse of
    ``crons.json`` (``count_enabled_from_disk``). The WS status pusher runs on
    the event loop, so computing these inline would stall the loop — and with
    it EVERY other WebSocket / coroutine on the gateway — for the duration of
    that disk latency (seconds on a slow/large home dir or a contended NFS
    mount). Offload both to a worker thread so the loop stays responsive; the
    pusher is a periodic background task, so the extra thread hop is free.

    NOTE: this deliberately uses ``count_enabled_from_disk`` rather than
    ``list_jobs``. ``list_jobs`` calls ``_sync()`` → ``_load()`` → ``_arm_timer()``,
    and ``_arm_timer`` calls ``asyncio.create_task`` — with no running loop in a
    worker thread that raises ``RuntimeError``, and since ``_arm_timer`` cancels
    the existing timer first it would silently stop all scheduled cron jobs.
    ``count_enabled_from_disk`` is a pure read that never mutates loop-owned
    state or the timer, so it is safe off-thread.
    """
    crons = await asyncio.to_thread(state.crons.count_enabled_from_disk)
    lessons = await asyncio.to_thread(state.lessons.load_all)
    return crons, len(lessons)


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
    state.broadcast_ws(SIDE_RESULT_EVENT, payload)


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

    from kiro_crew.dashboard.handlers import _log_ring, _update_info

    state: DashboardState = request.app["state"]
    from kiro_crew.dashboard.handlers.source_providers import (
        CHECK_STATUS_PENDING_MAX,
        CHECK_STATUS_TTL_SECS,
        ensure_gitlab_hosts_loaded,
        gitlab_hosts_generation,
        is_owner_dashboard_request,
        schedule_check_refresh,
    )

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
    try:
        all_slots = state.serialize_slots(include_check_status=owner_request)
        if ws.get("_is_dashboard_user", False):
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
        await ws.send_json(
            {
                "type": "slots",
                "data": slots_data,
                **envelope_extras,
                # Seed the client's generation baseline so a later change is
                # detectable as a change rather than as a first sighting.
                "gitlabHostsGeneration": gitlab_hosts_generation(),
            }
        )
        if owner_request:
            # Issue links carry no check status — skip them so the scheduler
            # never hands an issue URL to the pull-request-only chip fetch.
            urls = [
                link["url"]
                for payload in slots_data
                for link in payload.get("source_links", [])
                if link.get("kind", "change") == "change"
            ]
            if urls:
                schedule_check_refresh(urls, state.push_slots_update)
    except Exception:
        pass

    # Background task: push dashboard status periodically
    async def _push_status() -> None:
        _cached_lessons = 0
        _cached_crons = 0
        _counts_ts = 0.0
        try:
            while not ws.closed and not shutdown_event.is_set():
                now = time.time()
                # Refresh lesson/cron counts every 30s (not every 5s).
                if now - _counts_ts > _WS_COUNTS_CACHE_TTL:
                    _cached_crons, _cached_lessons = await _load_status_counts(state)
                    _counts_ts = now
                data = {
                    **state.status_snapshot(
                        cron_jobs=_cached_crons,
                        lessons=_cached_lessons,
                        update_available=bool(_update_info.get("available")),
                        update_self_updatable=bool(_update_info.get("self_updatable")),
                        update_checked=bool(_update_info.get("checked")),
                        update_command=str(_update_info.get("update_command") or ""),
                    ),
                    "version": _local_version,
                    "platform": sys.platform,
                }
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
                try:
                    await ws.send_json({"type": "dashboard", "data": data})
                except Exception:
                    break
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
                    schedule_check_refresh(urls, state.push_slots_update)
                refresh_round += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "check-status refresh round failed; continuing", exc_info=True
                )

    check_task = asyncio.create_task(_refresh_check_loop()) if owner_request else None
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
                                    _replay.append(
                                        {
                                            "type": "subagent_done",
                                            "data": {
                                                "id": native["id"],
                                                "slot": native["slot"],
                                                "elapsed": native["elapsed"],
                                                "error": _r(str(_err)) if _err else None,
                                                "stopped": bool(native.get("stopped")),
                                                "outcome": str(native.get("outcome") or ("stopped" if native.get("stopped") else ("failed" if native.get("error") else "completed"))),
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
                                    slot = a.parent_session_key.removeprefix("dashboard:")
                                    _replay.append(
                                        {
                                            "type": "subagent_snapshot",
                                            "data": {
                                                "id": a.id,
                                                "slot": slot,
                                                "task": _r(a.task),
                                                "agent": _r(a.agent),
                                                "streaming": _r(a.streaming_text),
                                                "last_tool": _r(a.last_tool),
                                                "tool_count": a.tool_count,
                                                "stalled": a.stalled,
                                                "started": a.started,
                                            },
                                        }
                                    )
                                except Exception:
                                    pass
                            # Done events for completed subagents so
                            # reconnecting clients can transition stale cards.
                            for a in state.subagents.all_agents:
                                if not a.done:
                                    continue
                                slot = a.parent_session_key.removeprefix("dashboard:")
                                try:
                                    _replay.append(
                                        {
                                            "type": "subagent_done",
                                            "data": {
                                                "id": a.id,
                                                "slot": slot,
                                                "elapsed": a.elapsed,
                                                "error": _r(a.error) if a.error else None,
                                                "stopped": a.user_stopped,
                                                "outcome": a.outcome,
                                                "task": _r(a.task),
                                                "agent": _r(a.agent),
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
                        _replay = [
                            _f
                            for _f in _replay
                            if state._ws_client_allowed(
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
        state.unsubscribe_logs(ws)
        state.unsubscribe_subagents(ws)
        state.unregister_ws(ws)
    return ws
