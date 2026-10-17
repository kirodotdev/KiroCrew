"""The stdout pump of a ``Backend``.

Every line the backend writes is budgeted, spilled, parsed and routed: a response settles
its pending request, a notification goes to the stub that owns it, and a server-to-client
request is delivered or refused. Per-request latency metrics and the hazard ledger of a
shared server's per-client behaviour are recorded here.

Each function here whose first parameter is ``self`` is a ``Backend`` method: the class
binds it by name, so it reads and writes the backend's own state on the instance, and a
patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_dead_reason``, ``_grant_callers``,
``_last_ping_response_mono``, ``_lease_awaiting_grant``, ``_lease_awaiting_release``,
``_lease_pending_riders``, ``_lease_release_waiters``, ``_metric_tasks``,
``_orphaned_leases``, ``_pending_requests``, ``_resource_subscriptions``.

Instance fields read: ``_inbox_lock``, ``_init_state``, ``_stub_inboxes``,
``attempt_id``, ``backend_generation_id``, ``exclusive_token``, ``pool_key``,
``process``, ``refcount``, ``stdout``, ``supports_caller_identity``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Awaitable, Optional

from kiro_crew.executors import image_executor, maintenance_executor
from kiro_crew.json_line import (
    ID_PROBE_BYTES,
    parse_json_object_line,
    recover_line_id,
    recover_top_level_id,
)
from kiro_crew.mcp_caller import INFLIGHT_NOTIFICATION
from kiro_crew.mcp_gateway import hazards, lifecycle_log
from kiro_crew.mcp_gateway.backend_handshake import (
    _REHANDSHAKE_STUB_SENTINEL,
    _is_lost_session_error,
)
from kiro_crew.mcp_gateway.backend_leases import (
    _ORPHANED_LEASES_MAX,
    _RELEASE_STUB_SENTINEL,
    _RESOURCES_SUBSCRIBE_METHOD,
    _RESOURCES_UNSUBSCRIBE_METHOD,
    _RESOURCES_UPDATED_NOTIFICATION,
    _is_success_response,
)
from kiro_crew.mcp_gateway.backend_liveness import _is_heartbeat_id
from kiro_crew.mcp_gateway.backend_mcp_apps import _APPS_STUB_SENTINEL
from kiro_crew.mcp_gateway.backend_wire import (
    _JSONRPC_SERVER_ERROR,
    _log_safe_identifier,
    _PendingRequest,
    _tool_call_error_text,
)
from kiro_crew.mcp_gateway.image_budget import (
    line_may_carry_image_block,
    parse_image_bearing_frame,
    rewrite_image_frame,
)
from kiro_crew.mcp_gateway.pool import READ_BUFFER_LIMIT_BYTES, RESPONSE_SPILL_THRESHOLD_BYTES
from kiro_crew.mcp_gateway.spill import maybe_spill_response

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway.backend import Backend

#: The facade's logger: log capture and filters select this code's records by that name.
logger = logging.getLogger("kiro_crew.mcp_gateway.backend")


# --- Per-request latency metrics ----------------------------------------
#
# When ``MCP_GATEWAY_CALL_METRICS_PATH`` is set, every completed request
# (any method — tools/call, tools/list, initialize, etc.) appends one
# JSON line with the exact gateway-e2e duration (``forward_from_stub``
# receive → backend stdout response routed). This is the only measurement
# in the system that isolates MCP gateway + backend wall time with zero
# LLM inference mixed in.
#
# Schema (one JSONL record per completed request):
#   {"ts": epoch_ms_int, "method": "tools/call", "dur_ms": 2.34,
#    "pool": "example-mcp::kirocrew::...", "pid": 12345, "ok": true}
#
# ``ok`` is false for either failure shape: a JSON-RPC ``error`` response, or
# a tools/call ``result`` carrying ``isError: true`` (the tool ran and
# reported its own failure). A failed tool call also emits one WARNING line to
# the gateway log naming the server, tool, session and a truncated error.
#
# Ring-buffer-free — we trust log rotation on the consumer side.
_METRICS_PATH = os.environ.get("MCP_GATEWAY_CALL_METRICS_PATH")


def _write_metric_line(record: dict[str, Any]) -> None:
    """Synchronous jsonl append. Always run off the event loop via
    :func:`_emit_call_metric` — never call directly from a coroutine."""
    if _METRICS_PATH is None:  # narrows for mypy; also guarded in the caller
        return
    try:
        line = json.dumps(record, separators=(",", ":"))
        with open(_METRICS_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        # Disk full, permission denied, rotation race — drop silently.
        pass


async def _emit_call_metric(record: dict[str, Any]) -> None:
    """Best-effort append to the metrics jsonl. Silent on any IO error —
    latency instrumentation MUST NOT affect request routing correctness.

    The blocking ``open()``/``write()`` is offloaded via ``asyncio.to_thread``
    so a slow or NFS-backed metrics volume cannot stall the event loop (and
    thus all request routing). When disabled (no path configured) this is a
    cheap early return with no thread hop.
    """
    if not _METRICS_PATH:
        return
    await asyncio.to_thread(_write_metric_line, record)


# Server->client notifications that reflect backend-wide state identical for
# every co-pooled tenant, so they are safe to fan out when we cannot attribute
# them to a single owning stub. Everything else (progress, logging/message,
# cancelled) is request-scoped: broadcasting an unattributable one would leak
# one tenant's content to co-tenants — a disclosure the non-pooled baseline
# never had — so those are dropped instead. ``notifications/resources/updated``
# is subscription-scoped, not request-scoped, and is routed by the
# ``uri -> {stub_uuid}`` table instead (``Backend._resource_update_targets``).
_GLOBAL_BROADCAST_NOTIFICATIONS: frozenset[str] = frozenset(
    {
        "notifications/tools/list_changed",
        "notifications/prompts/list_changed",
        "notifications/resources/list_changed",
    }
)


async def run_stdout_pump(self: Backend) -> None:
    """Read backend stdout line-by-line and route each line back to the
    originating stub. Exits on EOF (backend crash or clean exit). Never
    wrapped in a timeout — the learned correction explicitly warns
    against that pattern because it kills healthy long-lived sessions.
    """
    try:
        while True:
            try:
                line = await self.stdout.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    logger.warning(
                        "backend pid=%s closed stdout mid-line (%d bytes)",
                        self.pid,
                        len(exc.partial),
                    )
                break
            except asyncio.LimitOverrunError:
                # Drain the oversize line up to AND
                # INCLUDING its terminating newline WITHOUT consuming bytes
                # of the following frame. ``readuntil`` stops exactly at the
                # newline and leaves the remainder buffered; while the
                # not-yet-terminated prefix still exceeds the limit it
                # re-raises LimitOverrunError, so we consume that prefix
                # (``exc.consumed``) and retry. The previous ``read(8192)``
                # drain discarded post-newline bytes of the next response,
                # hanging the next request. The reader ``limit`` is
                # ``READ_BUFFER_LIMIT_BYTES`` (64 MiB by default, and
                # operator-tunable); a longer line is pathological and dropped.
                # Keep only the first and the last _OVERSIZE_KEEP bytes —
                # enough for _fail_oversize_request to find the JSON-RPC
                # id, which a server writes first or (the MCP TypeScript
                # SDK) last — while still draining the whole line off the
                # pipe. Accumulating the entire (possibly multi-GB) line
                # would itself be the memory blow-up this guard exists to
                # prevent.
                _OVERSIZE_KEEP = ID_PROBE_BYTES
                oversize_head = b""
                oversize_tail = b""
                try:
                    while True:
                        try:
                            piece = await self.stdout.readuntil(b"\n")
                            done = True
                        except asyncio.LimitOverrunError as exc:
                            piece = await self.stdout.readexactly(exc.consumed)
                            done = False
                        if len(oversize_head) < _OVERSIZE_KEEP:
                            oversize_head += piece[: _OVERSIZE_KEEP - len(oversize_head)]
                        oversize_tail = (oversize_tail + piece[-_OVERSIZE_KEEP:])[-_OVERSIZE_KEEP:]
                        if done:
                            break
                except (asyncio.IncompleteReadError, Exception):  # noqa: BLE001
                    pass
                logger.warning(
                    "backend pid=%s dropped oversize stdout line (>%d bytes)",
                    self.pid,
                    READ_BUFFER_LIMIT_BYTES,
                )
                # Fail the pending request so the waiting stub is not left
                # dangling. Without this the heartbeat eventually kills the
                # shared backend for ALL co-pooled sessions.
                await self._isolate_stdout_line(
                    self._fail_oversize_request(oversize_head, oversize_tail)
                )
                continue
            if not line:
                break
            await self._isolate_stdout_line(self._handle_stdout_line(line))
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover — defensive
        logger.exception("backend stdout pump crashed pid=%s", self.pid)
    finally:
        reason = self._dead_reason or (
            f"exit rc={self.process.returncode}"
            if self.process.returncode is not None
            else "stdout EOF"
        )
        self._dead_reason = reason
        await self._broadcast_backend_gone(reason)


async def _isolate_stdout_line(self: Backend, handling: Awaitable[None]) -> None:
    """Await one stdout line's handling; a raise costs that line only.

    The pump is shared by every co-pooled session and its ``finally``
    fails them all, so nothing one line does may end it.
    """
    try:
        await handling
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - one line must not end the shared pump
        logger.warning(
            "backend pid=%s could not handle a stdout line; dropping it",
            self.pid,
            exc_info=True,
        )


async def _handle_stdout_line(self: Backend, line: bytes) -> None:
    """Budget images, spill an oversized result, then route one line."""
    # Enforce the inline-image budget on tool results BEFORE the
    # spill step: a downscaled image both shrinks what spill writes
    # to disk and, more importantly, keeps an oversized image block
    # out of kiro-cli's conversation history, where it would be
    # replayed to the model on every later turn and wedge the
    # session (see kiro_crew.imaging MAX_IMAGE_EDGE_PX). Two
    # stages on two pools: the byte probe admits every frame that
    # COULD carry an image block (its negative is provable, but
    # any escaped non-ASCII text also matches), so a cheap
    # parse-confirm runs on the maintenance pool first -- like the
    # spill rewrite -- and only genuinely image-bearing frames
    # reach the image pool, where seconds-long Pillow decodes
    # from one server would otherwise head-of-line block every
    # other server's text-only results behind the probe's false
    # positives.
    if line_may_carry_image_block(line):
        try:
            loop = asyncio.get_running_loop()
            image_msg = await loop.run_in_executor(
                maintenance_executor(),
                parse_image_bearing_frame,
                line,
            )
            if image_msg is not None:
                line = await loop.run_in_executor(
                    image_executor(),
                    rewrite_image_frame,
                    image_msg,
                    line,
                    self.pool_key.server_name,
                )
        except Exception:
            # The rewrite never RAN (executor shutdown/saturation);
            # per-block fail-closed lives inside the hook. Routing
            # the raw line keeps co-pooled tenants alive, but the
            # frame may carry an unverified image -- log loudly
            # enough to diagnose a wedge that follows.
            logger.warning(
                "image-budget rewrite could not run for %s; routing raw line",
                self.pool_key.server_name,
                exc_info=True,
            )
    # Spill oversized (but under the read limit) responses to a
    # sidecar file and truncate inline, so a large-but-legitimate
    # tool result doesn't balloon the shared daemon's memory or the
    # agent's context. Offloaded to the maintenance executor (short
    # filesystem I/O); a spill failure falls back to the raw line.
    if len(line) > RESPONSE_SPILL_THRESHOLD_BYTES:
        try:
            line = await asyncio.get_running_loop().run_in_executor(
                maintenance_executor(),
                maybe_spill_response,
                line,
                self.pool_key.server_name,
                RESPONSE_SPILL_THRESHOLD_BYTES,
            )
        except Exception:
            logger.debug("spill-to-file failed; routing raw line", exc_info=True)
    await self._route_backend_line(line)


async def _route_backend_line(self: Backend, line: bytes) -> None:
    """Route one stdout line; a line that does not parse costs that line.

    A line that does not parse is dropped, unless it is recognisably a
    response to a pending request, which is then failed rather than left
    to hang. When routing a response raises after it took its pending
    request, that request is answered with an error rather than silence
    (the pump's per-line guard drops the line itself).
    """
    msg = parse_json_object_line(line)
    if msg is None:
        await self._fail_unparseable_response(line)
        return
    method = msg.get("method")
    if method is not None and not isinstance(method, str):
        logger.debug("backend line with a non-string method dropped: %r", line[:200])
        return
    msg_id = msg.get("id")
    # The request a RESPONSE answers, so a raise after routing popped it
    # can still answer it. A server-to-client request claims nothing.
    claimed = (
        self._pending_requests.get(str(msg_id)) if msg_id is not None and method is None else None
    )
    try:
        await self._route_backend_message(msg, msg_id, method)
    except Exception:
        if claimed is not None and self._pending_requests.get(str(msg_id)) is not claimed:
            await self._fail_claimed_request(
                claimed, msg_id, "backend response could not be routed; request dropped"
            )
        raise


async def _fail_unparseable_response(self: Backend, line: bytes) -> None:
    """Fail the pending request an unparseable RESPONSE line answers, if any.

    Only a line that opens like an object is probed, for its top-level
    ``"id"`` at either end of the line; a log line on stdout is just
    dropped. The probe reads a bounded head and tail, so it runs inline:
    a scan of the whole line would hold this shared pump, and through the
    GIL the whole event loop, for seconds on a multi-MiB line. Without
    this a response nested past the decoder's ceiling would leave its
    caller waiting for the heartbeat's hard ceiling.
    """
    msg_id = recover_line_id(line) if line.lstrip().startswith(b"{") else None
    pending = self._pending_requests.pop(str(msg_id), None) if msg_id is not None else None
    if pending is None:
        logger.debug("backend non-JSON stdout line dropped: %r", line[:200])
        return
    logger.warning(
        "backend pid=%s response to id=%r could not be parsed; failing that request",
        self.pid,
        msg_id,
    )
    await self._fail_claimed_request(
        pending, msg_id, "backend response could not be parsed; request dropped"
    )


async def _fail_claimed_request(
    self: Backend, pending: "_PendingRequest", msg_id: Any, message: str
) -> None:
    """Settle a request whose response this pump took but could not use.

    The request is settled exactly as an error response from the server
    would settle it, so whatever waits on it (the forwarding stub, queued
    initialize waiters, the MCP Apps fetch, lease riders and release
    waiters) is answered, and no lease bookkeeping is left stuck. The
    error is the gateway's, not the server's verdict, so a subscribe
    settled this way is an UNKNOWN verdict: the server may hold the lease,
    and the subscribe arms release it rather than read a refusal.
    """
    if pending.stub_uuid == "__init__" and self._init_state != "in_flight":
        # The handshake already settled before the raise; failing it now
        # would mark a ready backend dead with nobody left to answer.
        return
    await self._settle(
        pending,
        {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": _JSONRPC_SERVER_ERROR, "message": message},
        },
        verdict_unknown=True,
    )


async def _settle(
    self: Backend,
    pending: "_PendingRequest",
    msg: dict[str, Any],
    *,
    verdict_unknown: bool = False,
) -> None:
    """Settle one popped pending request with *msg*, the response to it.

    Every path that ends a forwarded request runs through here: a real
    response, and the error the pump synthesises for a response it could
    not parse or route, or one too large to read. Whatever waits on the
    request (initialize waiters, the MCP Apps fetch, lease riders and
    release waiters, the forwarding stub) is answered the same way.
    ``verdict_unknown`` marks that synthetic error: its ``error`` is not
    the server's refusal, so a subscribe arm treats it like a malformed
    verdict and releases a lease nobody routes.
    """
    if pending.stub_uuid == _REHANDSHAKE_STUB_SENTINEL:
        # The gateway's own re-sent ``initialize``: its reply is swallowed,
        # never delivered to a stub.
        if "error" in msg:
            logger.warning(
                "backend pid=%s refused the re-sent initialize: %s %s",
                self.pid,
                _tool_call_error_text(msg),
                # Another re-handshake may have minted a newer attempt
                # since this initialize went out, so name its own.
                lifecycle_log.lifecycle_fields(
                    "initialize_failed",
                    "unknown",
                    generation=self.backend_generation_id,
                    attempt=pending.attempt_id or self.attempt_id,
                ),
            )
        return
    if pending.t_start_ms:
        # Fire-and-forget: awaiting the emit here (even with its file
        # I/O offloaded to a thread) yields the shared stdout pump,
        # adding head-of-line latency to co-pooled sessions whenever
        # the metrics volume is slow. Schedule it off the hot path.
        #
        # ``error_text`` is non-None for BOTH failure shapes (a
        # JSON-RPC ``error`` and a ``result`` with ``isError: true``),
        # so a tool that ran and reported its own failure is scored
        # ``ok: false`` here rather than counted as a success.
        error_text = _tool_call_error_text(msg)
        self._spawn_metric_task(
            {
                "ts": int(time.time() * 1000),
                "method": pending.method,
                "dur_ms": round(time.monotonic() * 1000.0 - pending.t_start_ms, 3),
                "pool": self.pool_key.human_readable(),
                "pid": self.pid,
                "ok": error_text is None,
                "stub": pending.stub_uuid,
            }
        )
        if error_text is not None and pending.method == "tools/call":
            # One greppable breadcrumb per failed tool call (isError or
            # a JSON-RPC error), so an operator searching gateway.log
            # for an MCP outage finds the failing call rather than only
            # the session's lifecycle lines. WARNING matches the
            # severity of the per-session claim-push outcomes logged on
            # this seam.
            logger.warning(
                "mcp tool call failed: server=%s tool=%s session=%s error=%s",
                _log_safe_identifier(self.pool_key.server_name),
                _log_safe_identifier(pending.tool_name),
                _log_safe_identifier(pending.session_key),
                error_text,
            )
    if pending.stub_uuid == "__init__":
        await self._on_upstream_initialize(msg)
        return
    if pending.stub_uuid == _APPS_STUB_SENTINEL:
        # Gateway-originated resources/read reply for an MCP Apps
        # ui:// fetch — hand it to the parked fetch coroutine, never a
        # stub. (Already popped above so it is removed exactly once.)
        fut = pending.apps_future
        if fut is not None and not fut.done():
            fut.set_result(msg)
        return
    if pending.stub_uuid == _RELEASE_STUB_SENTINEL:
        # Gateway-originated lease maintenance (release after the last
        # subscriber detached, a post-respawn replay, or a transition
        # orphaned by its forwarder retracting/detaching mid-flight).
        # Riders that parked on the URI after the orphaning are
        # settled on the server's verdict exactly as the normal grant
        # arm settles them — dropping a parking here would leave that
        # stub's subscribe swallowed with no response, hung forever.
        if pending.method == _RESOURCES_SUBSCRIBE_METHOD and pending.resource_uri:
            orphan_uri = pending.resource_uri
            self._lease_awaiting_grant.discard(orphan_uri)
            riders = self._lease_pending_riders.pop(orphan_uri, [])
            if _is_success_response(msg):
                granted: set[str] = set()
                # A replay grant is honoured only while its stub is
                # still attached — detach cannot see a sentinel-owned
                # pending, so this is where a mid-replay disconnect is
                # caught; granting the dead UUID would pin the lease
                # to a stub that can never drain it.
                if pending.replay_stub and pending.replay_stub in self._stub_inboxes:
                    granted.add(pending.replay_stub)
                granted.update(rider_uuid for rider_uuid, _ in riders)
                # Routing is committed BEFORE any reply is awaited: a
                # reply into a full inbox detaches its stub, which
                # prunes the live table — updating the table from a
                # local set afterwards would reinsert the detached
                # UUID and route updates at a stub that is gone.
                if granted:
                    self._resource_subscriptions.setdefault(orphan_uri, set()).update(granted)
                    self._orphaned_leases.discard(orphan_uri)
                    if (
                        self.supports_caller_identity
                        and pending.caller is not None
                        and pending.replay_stub in granted
                    ):
                        # A replayed identity grant is held by the
                        # replay's caller — record it so a later
                        # detach can release as the right principal.
                        self._grant_callers[(orphan_uri, pending.replay_stub)] = pending.caller
                elif orphan_uri not in self._resource_subscriptions:
                    # A granted lease nobody wants: release on the
                    # spot, as the caller that took it (a replay's
                    # grant belongs to the replay's principal).
                    await self._release_upstream_subscriptions([orphan_uri], caller=pending.caller)
                for rider_uuid, rider_id in riders:
                    await self._reply_locally(rider_uuid, rider_id, result={})
            else:
                error_obj = msg.get("error")
                # A refusal proves the server holds no lease; a
                # MALFORMED frame (no error either) or the gateway's own
                # error for a reply it could not use proves nothing —
                # the subscribe may well have taken. Denying routing
                # while keeping such a lease would strand it live
                # upstream, with every later update charged to the
                # server as a hazard, so an unsettled verdict with
                # nobody routed releases the lease on the spot.
                if (error_obj is None or verdict_unknown) and (
                    orphan_uri not in self._resource_subscriptions
                ):
                    await self._release_upstream_subscriptions([orphan_uri], caller=pending.caller)
                for rider_uuid, rider_id in riders:
                    await self._reply_locally(
                        rider_uuid,
                        rider_id,
                        error=(
                            error_obj
                            if isinstance(error_obj, dict)
                            else {
                                "code": _JSONRPC_SERVER_ERROR,
                                "message": "resources/subscribe refused by server",
                            }
                        ),
                    )
        elif pending.method == _RESOURCES_UNSUBSCRIBE_METHOD and pending.resource_uri:
            # A gateway-originated (or detach-orphaned) release
            # settled. On success the lease is cleanly gone; on a
            # refusal with nobody left to route to, the server has
            # RETAINED a subscription that is now a consequence of
            # the broker's own lease handling — its updates are
            # dropped without recording a hazard, so the server is
            # not condemned for behaviour that is correct. A
            # MALFORMED release response is DELIBERATELY treated as
            # a refusal here too: on the routing axis that fails
            # closed, and on the hazard-ledger axis it fails open
            # (orphan-marked, so unknowable frames are forgiven
            # rather than charged to the server).
            _rel_uri = pending.resource_uri
            self._lease_awaiting_release.discard(_rel_uri)
            waiters = self._lease_release_waiters.pop(_rel_uri, [])
            if _is_success_response(msg):
                self._orphaned_leases.discard(_rel_uri)
                subscribers = self._resource_subscriptions.get(_rel_uri)
                if subscribers is not None:
                    for waiter_uuid, _waiter_id in waiters:
                        subscribers.discard(waiter_uuid)
                    if not subscribers:
                        del self._resource_subscriptions[_rel_uri]
                for waiter_uuid, waiter_id in waiters:
                    await self._reply_locally(waiter_uuid, waiter_id, result={})
                await self._drain_replacement_subscribes(_rel_uri, released=True)
            else:
                if _rel_uri not in self._resource_subscriptions:
                    while len(self._orphaned_leases) >= _ORPHANED_LEASES_MAX:
                        self._orphaned_leases.pop()
                    self._orphaned_leases.add(_rel_uri)
                error_obj = msg.get("error")
                for waiter_uuid, waiter_id in waiters:
                    await self._reply_locally(
                        waiter_uuid,
                        waiter_id,
                        error=(
                            error_obj
                            if isinstance(error_obj, dict)
                            else {
                                "code": _JSONRPC_SERVER_ERROR,
                                "message": "resources/unsubscribe " "refused by server",
                            }
                        ),
                    )
                await self._drain_replacement_subscribes(_rel_uri, released=False)
        return
    if pending.resource_uri and pending.method in (
        _RESOURCES_SUBSCRIBE_METHOD,
        _RESOURCES_UNSUBSCRIBE_METHOD,
    ):
        await self._on_resource_subscription_response(pending, msg, verdict_unknown=verdict_unknown)
    # MCP Apps interception: a tools/call result carrying a ui://
    # resource is parked (the response is held off the stub) while an
    # out-of-band resources/read fetches the app payload. When it
    # returns True the (marked) response is delivered asynchronously by
    # a background task, so DO NOT deliver here.
    if await self._maybe_intercept_ui_result(pending, msg):
        self.touch()
        return
    rewritten = dict(msg)
    rewritten["id"] = pending.original_id
    await self._deliver_to_stub(pending.stub_uuid, rewritten)
    self.touch()


async def _route_backend_message(
    self: Backend, msg: dict[str, Any], msg_id: Any, method: Any
) -> None:
    if msg_id is not None and method is None:
        # Gateway-internal liveness pong: the heartbeat
        # ping is sent under HEARTBEAT_PING_ID and its reply (result or
        # error) is consumed here, never routed to a stub.
        if _is_heartbeat_id(msg_id):
            self._last_ping_response_mono = time.monotonic()
            return
        # Response to a request this backend forwarded.
        pending = self._pending_requests.pop(str(msg_id), None)
        if pending is None:
            logger.warning(
                "backend pid=%s response to unknown id=%r; dropping",
                self.pid,
                msg_id,
            )
            return
        if (
            pending.retry_frame is not None
            and _is_lost_session_error(msg)
            and await self._retry_after_rehandshake(pending)
        ):
            return
        await self._settle(pending, msg)
        return
    if method is not None and msg_id is None:
        # Gateway-internal: the backend's in-flight report. Consumed here,
        # never routed to a stub (it names gateway forward ids, which mean
        # nothing to a stub and would read as an unattributable frame).
        if method == INFLIGHT_NOTIFICATION:
            self._apply_inflight_report(msg)
            return
        # Subscription-scoped: ``notifications/resources/updated`` carries
        # no request id, so it is attributed by URI — delivered to every
        # stub subscribed to that URI and only those. This arm is
        # TERMINAL: URI is the only permitted attribution for this
        # notification, so an update for a URI nobody subscribed to is
        # dropped here (deny-by-default, hazard recorded) and never falls
        # through to request-scoped attribution — a server-supplied
        # ``_meta.relatedRequestId`` must not hand one tenant a URI that
        # only a co-tenant ever named.
        if method == _RESOURCES_UPDATED_NOTIFICATION:
            targets = self._resource_update_targets(msg)
            for target_uuid in targets:
                await self._deliver_to_stub(target_uuid, msg)
            if not targets:
                _params = msg.get("params")
                _uri = _params.get("uri") if isinstance(_params, dict) else None
                if isinstance(_uri, str) and _uri in self._orphaned_leases:
                    # A retained lease the broker failed to release: the
                    # update is a consequence of OUR lease handling, so
                    # it is dropped without condemning the server.
                    # The URI is deliberately NOT logged — resource URIs
                    # can carry tokens or presigned query parameters.
                    logger.debug(
                        "backend pid=%s dropping resources/updated for "
                        "an orphaned lease (no hazard)",
                        self.pid,
                    )
                else:
                    logger.debug(
                        "backend pid=%s dropping resources/updated for a "
                        "URI no stub subscribed to (never routed by "
                        "request id)",
                        self.pid,
                    )
                    self._record_hazard(hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)
            return
        # Attribute request-scoped notifications (progress, or a log tied
        # to an in-flight call) to their owning stub so they are not leaked
        # to co-pooled tenants sharing this backend.
        owner = self._notification_owner(msg)
        if owner is not None:
            await self._deliver_to_stub(owner, msg)
        elif method in _GLOBAL_BROADCAST_NOTIFICATIONS:
            # Genuinely backend-wide state (identical for every tenant,
            # e.g. tools/list_changed) — safe to fan out to all stubs.
            await self._broadcast(msg)
        else:
            # Unattributable request-scoped notification (progress/logging
            # without a unique routing token, or a token that collided
            # across tenants). Broadcasting it would disclose one tenant's
            # request-scoped content to co-tenants — a leak the non-pooled
            # baseline never had — so drop it (deny-by-default) rather than
            # guess an owner.
            logger.debug(
                "backend pid=%s dropping unattributable request-scoped "
                "notification %r (not broadcast to avoid cross-tenant leak)",
                self.pid,
                method,
            )
            self._record_hazard(hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)
        return
    # Server-to-client request (has method AND id) — route ONLY when we can
    # attribute it unambiguously:
    # 1. _meta.relatedRequestId -> owning stub (MCP-spec aligned)
    # 2. Single attached stub -> trivial case
    # Otherwise (multiple stubs, no relatedRequestId) we recycle the backend
    # rather than guess, to avoid a cross-tenant leak.
    if method is not None and msg_id is not None:
        target_stub: Optional[str] = None
        # Priority 1: _meta.relatedRequestId lookup
        params = msg.get("params")
        if isinstance(params, dict):
            meta = params.get("_meta")
            if isinstance(meta, dict):
                related_id = meta.get("relatedRequestId")
                if related_id is not None:
                    pending = self._pending_requests.get(str(related_id))
                    if pending is not None:
                        target_stub = pending.stub_uuid
        # Priority 2: single stub attached
        if target_stub is None:
            async with self._inbox_lock:
                stubs = list(self._stub_inboxes.keys())
            if len(stubs) == 1:
                target_stub = stubs[0]
        # Priority 2 (single stub) is the only safe fallback: with multiple
        # stubs and no relatedRequestId we cannot attribute a server->client
        # request to a tenant without risking a cross-tenant leak
        # (delivering B's sampling/elicitation to A, or broadcasting a
        # request every tenant would answer). Refuse to keep pooling: recycle
        # the backend so its stubs fall back to an unambiguous per-session
        # exec. (Well-behaved servers set relatedRequestId -> Priority 1.)
        if target_stub is not None:
            await self._deliver_to_stub(target_stub, msg)
        else:
            async with self._inbox_lock:
                nstubs = len(self._stub_inboxes)
            reason = (
                "server-initiated request without relatedRequestId while "
                f"{nstubs} stubs share this backend; cannot route without a "
                "cross-tenant leak — recycling"
            )
            logger.warning("backend pid=%s %s", self.pid, reason)
            self._record_hazard(hazards.HAZARD_UNROUTABLE_SERVER_REQUEST)
            self._dead_reason = self._dead_reason or reason
            await self._broadcast_backend_gone(reason)
        return
    logger.debug("backend pid=%s emitted malformed JSON-RPC: %r", self.pid, msg)


def _record_hazard(self: Backend, code: str) -> None:
    """Note that this server exhibited per-client behaviour while shared.

    Only meaningful once MORE THAN ONE client is attached. A backend serving
    a single client legitimately owns it, so an unattributable frame there
    proves nothing: there is no second tenant it could have leaked to.
    ``exclusive_token`` is not sufficient to express that — a pooled backend
    also serves exactly one client from the moment it starts until a second
    stub attaches, and recording during that window would disqualify a
    server for behaviour that is correct.

    Biased toward under-recording on purpose. A false hazard withdraws a
    recommendation for a server that is fine, so the bar is real traffic on a
    genuinely shared backend; a missed one costs a withdrawal that the next
    observation makes again.

    The observation is stamped with what this backend actually launched, read
    straight off the pool key, so upgrading or reconfiguring the server
    invalidates it rather than holding the new version responsible for the
    behaviour of the one it replaced.

    In-memory only — the flush is off-loop.
    """
    if self.exclusive_token or self.refcount <= 1:
        return
    key = self.pool_key
    name = key.server_name
    identity = hazards.launch_identity(
        key.command_args_hash, key.effective_env_hash, key.binary_version
    )
    if name and hazards.record_observed(name, code, identity):
        logger.warning(
            "hazard: server %r first exhibited %s while shared; the "
            "MCP page will withdraw its recommendation",
            name,
            code,
        )


def _apply_inflight_report(self: Backend, msg: dict[str, Any]) -> None:
    """Refresh ``t_progress_ms`` on every pending request the backend's
    in-flight report names: its parked calls that are progressing and its
    calls still queued behind the worker (never the one running there, so
    the ceiling bounds one call's run). Unknown ids (already answered) are
    ignored; a malformed report refreshes nothing."""
    params = msg.get("params")
    ids = params.get("requestIds") if isinstance(params, dict) else None
    if not isinstance(ids, list):
        return
    now_ms = time.monotonic() * 1000.0
    for rid in ids:
        pending = self._pending_requests.get(str(rid))
        if pending is not None:
            pending.t_progress_ms = now_ms


def _notification_owner(self: Backend, msg: dict[str, Any]) -> Optional[str]:
    """Best-effort attribution of a server->client notification to the one
    stub that owns the originating request, so a request-scoped
    notification (progress, or a log tied to an in-flight call) is not
    leaked to co-pooled tenants. Returns None for unattributable /
    genuinely global notifications, which the caller broadcasts.

    progressToken collisions across tenants are possible (clients pick
    their own), so only a UNIQUELY-owned token routes; an ambiguous token
    falls through to broadcast (no worse than the pre-scoping behaviour)."""
    params = msg.get("params")
    if not isinstance(params, dict):
        return None
    # progress notifications echo the request's progressToken.
    token = params.get("progressToken")
    if token is not None:
        owners = {
            p.stub_uuid
            for p in self._pending_requests.values()
            if p.progress_token == token and p.stub_uuid != "__init__"
        }
        if len(owners) == 1:
            return next(iter(owners))
    # logging / other notifications may carry _meta.relatedRequestId.
    meta = params.get("_meta")
    if isinstance(meta, dict):
        related_id = meta.get("relatedRequestId")
        if related_id is not None:
            pending = self._pending_requests.get(str(related_id))
            if pending is not None and pending.stub_uuid != "__init__":
                return pending.stub_uuid
    return None


def _spawn_metric_task(self: Backend, record: dict[str, Any]) -> None:
    """Schedule a best-effort latency-metric emit off the stdout-pump
    critical path. No-op when metrics are disabled (the default), so the
    shared pump does not allocate + schedule + discard a Task per RPC
    response for every co-pooled tenant. Tracked in ``_metric_tasks``
    (discarded on completion) so the task isn't GC'd before it runs; a slow
    metrics disk therefore cannot back-pressure frame routing."""
    if _METRICS_PATH is None:
        return
    task = asyncio.create_task(_emit_call_metric(record))
    self._metric_tasks.add(task)
    task.add_done_callback(self._metric_tasks.discard)


async def _fail_oversize_request(self: Backend, head: bytes, tail: bytes = b"") -> None:
    """Try to extract the JSON-RPC id from an oversize response and fail
    just that request, so the waiting stub is unblocked without killing
    the entire shared backend.

    *head* and *tail* are the kept start and end of the drained line; the
    id is recovered from the top-level object only, from either end. The
    request is settled as an error response from the server would settle
    it: an oversize *initialize* reply fails the handshake and answers
    every queued waiter, an MCP Apps fetch is resolved at once.
    """
    msg_id = recover_top_level_id(head, tail)
    if msg_id is not None:
        pending = self._pending_requests.pop(str(msg_id), None)
        if pending is not None:
            await self._fail_claimed_request(
                pending,
                msg_id,
                f"response exceeded size limit ({READ_BUFFER_LIMIT_BYTES} bytes); "
                "request dropped",
            )
        return
    # Id unrecoverable (it was in neither kept end): do NOT fail an
    # arbitrary pending request — that sends a spurious error to an innocent
    # stub while the real culprit keeps hanging until the wedge timeout.
    # Recycle the shared backend instead so every attached stub gets a clean
    # BackendGone and re-establishes.
    reason = (
        f"oversize response (>{READ_BUFFER_LIMIT_BYTES} bytes) with "
        "unrecoverable request id; recycling shared backend"
    )
    self._dead_reason = self._dead_reason or reason
    await self._broadcast_backend_gone(reason)
