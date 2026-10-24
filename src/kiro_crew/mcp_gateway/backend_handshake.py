"""The MCP ``initialize`` handshake of a ``Backend``.

The first stub's ``initialize`` forwarded once, its result cached and replayed to every
later stub; the deadline on that first handshake; the re-drive of a respawned backend; the
terminal failure that answers every queued stub; the one rehandshake and retry for a server
whose session was lost behind the pipe; and ``send_initialize``, the handshake run before
the stdout pump starts.

Each function here whose first parameter is ``self`` is a ``Backend`` method: the class
binds it by name, so it reads and writes the backend's own state on the instance, and a
patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_dead_reason``, ``_init_deadline_task``, ``_init_first_id``,
``_init_first_stub``, ``_init_pending``, ``_init_result``, ``_init_state``,
``_pending_requests``, ``_upstream_init_frame``, ``attempt_id``, ``reports_inflight``,
``supports_caller_identity``.

Instance fields read: ``_inbox_lock``, ``_init_done_event``, ``_stub_inboxes``,
``initialize_timeout_secs``, ``pool_key``, ``stdin``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Mapping, Optional

from kiro_crew import platform_compat
from kiro_crew.json_line import parse_json_object_line
from kiro_crew.mcp_caller import CALLER_CAPABILITY_KEY, INFLIGHT_CAPABILITY_KEY
from kiro_crew.mcp_gateway import lifecycle_log
from kiro_crew.mcp_gateway.backend_mcp_apps import _inject_client_extensions
from kiro_crew.mcp_gateway.backend_wire import (
    BackendGone,
    _log_safe_identifier,
    _PendingRequest,
    _strip_caller_meta,
    _write_json_line,
    _write_json_lines,
)

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway.backend import Backend

#: The facade's logger: log capture and filters select this code's records by that name.
logger = logging.getLogger("kiro_crew.mcp_gateway.backend")


# Default handshake deadline. Real MCP backends reply to ``initialize``
# within tens of milliseconds; 10s is generous slack for a cold-spawning
# backend on a loaded host.
_DEFAULT_INITIALIZE_TIMEOUT_SECS = 10.0

# Windows cold-start floor for the first-``initialize`` deadline. On Windows the
# backend's cold start (interpreter + import graph on a filesystem with no POSIX
# page cache, and process creation that is itself heavier than fork/exec)
# routinely overruns a 10s budget and then completes on the next spawn, so a
# too-tight deadline reaps a backend that was about to answer and respawns it --
# a spawn -> reaped-at-deadline -> respawn loop in which tools never finish
# listing. The 10s default was measured against POSIX cold starts; raising only
# the Windows floor leaves every POSIX host and any explicit operator value that
# already clears the floor untouched. See ``_effective_initialize_timeout_secs``.
_WINDOWS_INITIALIZE_TIMEOUT_FLOOR_SECS = 30.0


def _effective_initialize_timeout_secs(configured: float) -> float:
    """Apply the Windows cold-start floor to a configured initialize deadline.

    POSIX hosts, and any Windows host whose configured value already clears the
    floor, get ``configured`` back unchanged -- an operator who deliberately
    raised the deadline is never lowered. Only a Windows host left on (or below)
    the POSIX-measured default is lifted to
    ``_WINDOWS_INITIALIZE_TIMEOUT_FLOOR_SECS``, so a slow cold start is given
    room to finish its handshake rather than being reaped and respawned.
    """
    if platform_compat.IS_WINDOWS:
        return max(float(configured), _WINDOWS_INITIALIZE_TIMEOUT_FLOOR_SECS)
    return float(configured)


# JSON-RPC initialize id the gateway uses on behalf of the first stub.
# Real stubs' ``initialize`` requests are cached and replayed from the
# gateway-side ``init_cache``; this id only matters for the one-shot
# handshake in :func:`send_initialize`.
_GATEWAY_INIT_ID = "mcp-gateway-init-0"


def _parse_initialize_response(response: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """``(result, supports_caller_identity)`` from a backend's initialize reply.

    Raises :class:`ValueError` for an error reply or a result that is not an
    object: the handshake did not complete. A ``capabilities`` that is
    present but not an object is cosmetic, so it is replaced with ``{}`` (and
    logged) rather than failing the shared backend's handshake over it, and
    the returned result carries the replacement so no later session is
    replayed the bad value.
    """
    if "error" in response:
        raise ValueError(f"backend returned initialize error: {response['error']}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"initialize response missing/malformed result: {response!r}")
    capabilities = result.get("capabilities", {})
    if not isinstance(capabilities, dict):
        logger.warning(
            "backend initialize result carries a non-object capabilities (%s); "
            "treating it as {}",
            type(capabilities).__name__,
        )
        capabilities = {}
        result = {**result, "capabilities": capabilities}
    experimental = capabilities.get("experimental") or {}
    supports_caller_identity = isinstance(experimental, dict) and (
        CALLER_CAPABILITY_KEY in experimental
    )
    return result, supports_caller_identity


def _reports_inflight(result: dict[str, Any]) -> bool:
    """Whether a parsed ``initialize`` result advertises
    ``mcp_shared.INFLIGHT_CAPABILITY_KEY`` under ``capabilities.experimental``."""
    experimental = (result.get("capabilities") or {}).get("experimental") or {}
    return isinstance(experimental, dict) and INFLIGHT_CAPABILITY_KEY in experimental


def _inject_inflight_capability(msg: dict[str, Any]) -> dict[str, Any]:
    """Return ``msg`` with ``capabilities.experimental[INFLIGHT_CAPABILITY_KEY]``
    added to an ``initialize`` frame -- the gateway asking the backend to send
    in-flight reports -- or ``msg`` unchanged when it is not a well-formed
    initialize. Same copy discipline as :func:`_inject_client_extensions`."""
    params = msg.get("params")
    if not isinstance(params, dict):
        return msg
    out = dict(msg)
    params = dict(params)
    caps_raw = params.get("capabilities")
    caps = dict(caps_raw) if isinstance(caps_raw, dict) else {}
    exp_raw = caps.get("experimental")
    experimental = dict(exp_raw) if isinstance(exp_raw, dict) else {}
    experimental.setdefault(INFLIGHT_CAPABILITY_KEY, {})
    caps["experimental"] = experimental
    params["capabilities"] = caps
    out["params"] = params
    return out


# A request the Python MCP SDK refuses because ITS session is not initialized
# comes back as exactly this error (``mcp.shared.session``: the
# ``Received request before initialization was complete`` RuntimeError is
# reported as INVALID_PARAMS with this message and an empty ``data``). The same
# frame answers a request that fails the SDK's ``ClientRequest`` validation, so
# on its own it does not prove the session was lost -- which is why the answer
# to it is one rehandshake and one retry, never a loop.
#
# The gateway reaches it when the server process behind the pipe was replaced
# without a handshake while the pipe stayed up. A pooling multiplexer does this
# when the command the gateway spawned is its thin client: the client keeps its
# connection to the multiplexer's daemon, and the daemon respawns a retired or
# dead server instance cold, sending it nothing before the next request. kiro-cli
# sends ``initialize`` once per session and the gateway answers every later stub
# from its cache, so without the rehandshake every later call on that backend
# fails until the gateway itself restarts.
_LOST_SESSION_ERROR_CODE = -32602
_LOST_SESSION_ERROR_MESSAGE = "Invalid request parameters"

# Requests safe to send twice: the refusal above is issued before the SDK
# dispatches anything, and none of these hold gateway-side lease state.
# ``resources/subscribe`` / ``unsubscribe`` are left out because their
# response drives the lease bookkeeping, and ``initialize`` is the cache's.
_REHANDSHAKE_RETRY_METHODS: frozenset[str] = frozenset(
    {
        "tools/call",
        "tools/list",
        "prompts/list",
        "prompts/get",
        "resources/list",
        "resources/read",
        "resources/templates/list",
        "completion/complete",
    }
)

# ``stub_uuid`` of the gateway's own re-sent ``initialize``: its reply is
# swallowed, never delivered to a stub.
_REHANDSHAKE_STUB_SENTINEL = "__rehandshake__"


def _is_lost_session_error(msg: dict[str, Any]) -> bool:
    """Whether ``msg`` is the Python MCP SDK's not-initialized refusal."""
    error = msg.get("error")
    return (
        isinstance(error, dict)
        and error.get("code") == _LOST_SESSION_ERROR_CODE
        and error.get("message") == _LOST_SESSION_ERROR_MESSAGE
        # Empty in every SDK release that sends it; absent is tolerated so a
        # release that drops the member is still recognised.
        and error.get("data", "") == ""
    )


def _without_id(msg: dict[str, Any]) -> dict[str, Any]:
    """A shallow copy of ``msg`` with no ``id``."""
    out = dict(msg)
    out.pop("id", None)
    return out


async def _handle_initialize(
    self: Backend,
    stub_uuid: str,
    msg: dict[str, Any],
) -> None:
    original_id = msg.get("id")
    if original_id is None:
        raise ValueError("initialize without id")
    if self._init_state == "ready":
        assert self._init_result is not None
        await self._deliver_cached_initialize(stub_uuid, original_id, self._init_result)
        return
    if self._init_state == "failed":
        raise BackendGone(self._dead_reason or "backend initialize failed")
    if self._init_state == "in_flight":
        self._init_pending.append((stub_uuid, original_id))
        return
    # First time: forward upstream under a gateway id so the stdout pump
    # can route the result to ``_on_upstream_initialize`` instead of the
    # stub (which would still be waiting under the gateway id).
    self._init_state = "in_flight"
    self._init_first_stub = stub_uuid
    self._init_first_id = original_id
    self._init_pending.append((stub_uuid, original_id))
    fid = self._next_forward_id()
    self._pending_requests[fid] = _PendingRequest(
        stub_uuid="__init__",
        original_id=None,
        method="initialize",
        t_start_ms=time.monotonic() * 1000.0,
    )
    # Trust boundary: strip any stub-supplied caller identity from the
    # initialize forward too. forward_from_stub strips it on every other
    # forwarded request, but ``initialize`` returns early through this
    # path — without this a stub could forge _meta.kirocrew.caller at init.
    forward_msg = _strip_caller_meta(msg)
    # MCP Apps: advertise the ui extension to the backend (no-op unless
    # the KIROCREW_MCP_APPS flag is on). Must follow the strip so the
    # injected frame is our copy, never the stub's.
    forward_msg = _inject_inflight_capability(_inject_client_extensions(forward_msg))
    self._upstream_init_frame = _without_id(forward_msg)
    forward_msg["id"] = fid
    self.touch()
    try:
        await _write_json_line(self.stdin, forward_msg)
    except (BrokenPipeError, ConnectionResetError) as exc:
        self._dead_reason = f"stdin closed: {exc}"
        raise BackendGone(self._dead_reason) from exc
    # Armed only once the forward is on the wire: a write that failed never
    # started a handshake, and arming there would leave a timer with no
    # in-flight window to close.
    self._arm_init_deadline()


def _arm_init_deadline(self: Backend) -> None:
    """Start the timer that fails and reaps a backend which never answers
    the first ``initialize``.

    Idempotent per in-flight window: an already-armed timer is left alone
    so queued stubs cannot each extend the deadline.
    """
    if self._init_deadline_task is not None and not self._init_deadline_task.done():
        return
    self._init_deadline_task = asyncio.create_task(
        self._init_deadline(self.initialize_timeout_secs)
    )


def _cancel_init_deadline(self: Backend) -> None:
    """Disarm the first-handshake timer once init reaches a terminal state.

    Safe from inside the timer's own coroutine: cancelling the currently
    running task is skipped, so a terminal transition driven BY the
    deadline does not cancel itself mid-flight.
    """
    task = self._init_deadline_task
    if task is None:
        return
    self._init_deadline_task = None
    if task is asyncio.current_task():
        return
    if not task.done():
        task.cancel()


async def _init_deadline(self: Backend, timeout: float) -> None:
    """Fail and reap a backend whose first ``initialize`` never resolves.

    ``_fail_init`` performs the terminal transition every waiter observes
    (failed state, done event, an explicit JSON-RPC error to each queued
    stub); ``shutdown`` then reaps the process group, since a wedged
    backend that is still running would otherwise hold its core until the
    idle sweep reclaims it. shutdown() touches no pool bookkeeping, so
    there is no reserve/evict race with a concurrent acquirer.
    """
    try:
        await asyncio.sleep(timeout)
    except asyncio.CancelledError:
        return
    # The handshake may have resolved while this coroutine was scheduled;
    # only a still-in-flight window is this timer's to close.
    if self._init_state != "in_flight":
        return
    reason = f"initialize did not complete within {timeout:g}s"
    self._dead_reason = self._dead_reason or reason
    with contextlib.suppress(Exception):
        await self._fail_init(reason, log_event="initialize_timed_out", log_reason="deadline")
    with contextlib.suppress(Exception):
        await self.shutdown()


async def _deliver_cached_initialize(
    self: Backend,
    stub_uuid: str,
    original_id: Any,
    cached_result: dict[str, Any],
) -> None:
    """Synthesize a cached-initialize response and drop it into the stub's
    inbox. The stub sees a reply shaped exactly like one from a real
    backend — same ``result`` object, the stub's own ``id`` restored.
    """
    response = {"jsonrpc": "2.0", "id": original_id, "result": cached_result}
    async with self._inbox_lock:
        inbox = self._stub_inboxes.get(stub_uuid)
    if inbox is not None:
        await self._enqueue_to_stub(
            stub_uuid,
            inbox,
            (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"),
        )


async def prime_initialize(
    self: Backend,
    init_msg: dict[str, Any],
    *,
    timeout: float = _DEFAULT_INITIALIZE_TIMEOUT_SECS,
) -> None:
    """Re-drive the MCP ``initialize`` handshake on a freshly respawned
    backend using a stub's captured ``initialize`` request, WITHOUT
    delivering any response to a stub.

    Used by the gatewayd bridge's transparent-respawn path: when a shared
    backend dies, a fresh one is spawned and primed here so it reaches
    ``_init_state == "ready"`` before stub traffic resumes. kiro-cli never
    re-sends ``initialize`` after a backend dies, so the gateway must
    replay it on kiro-cli's behalf and swallow the reply.

    Concurrency-safe: if several stubs re-acquire the same fresh backend
    at once, the first drives the upstream handshake and the rest await
    the shared completion event. Raises :class:`BackendGone` if the
    backend is not alive, the handshake fails, or it times out.
    """
    if not self.is_alive:
        raise BackendGone(self._dead_reason or "backend not alive")
    if self._init_state == "ready":
        return
    if self._init_state == "failed":
        raise BackendGone(self._dead_reason or "backend initialize failed")
    if self._init_state == "unsent":
        # We are the first to prime this fresh backend. Forward the
        # captured initialize upstream under a gateway id routed to the
        # ``__init__`` sentinel so the stdout pump feeds the reply to
        # ``_on_upstream_initialize`` (which caches it + sets the done
        # event) rather than to any stub. No ``_init_pending`` entry is
        # added, so no stub ever receives this synthetic reply.
        self._init_state = "in_flight"
        fid = self._next_forward_id()
        self._pending_requests[fid] = _PendingRequest(
            stub_uuid="__init__",
            original_id=None,
            method="initialize",
            t_start_ms=time.monotonic() * 1000.0,
        )
        # Strip a stub-forged caller block from the respawn init forward
        # too (mirrors _handle_initialize). captured_init is a shallow copy
        # taken before forward_from_stub's strip, so it can still carry a
        # forged flat CALLER_META_KEY.
        forward_msg = _strip_caller_meta(init_msg)
        # MCP Apps: same injection as _handle_initialize so a respawned
        # backend sees the identical ui capability (flag-gated no-op).
        forward_msg = _inject_inflight_capability(_inject_client_extensions(forward_msg))
        self._upstream_init_frame = _without_id(forward_msg)
        forward_msg["id"] = fid
        self.touch()
        try:
            await _write_json_line(self.stdin, forward_msg)
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._dead_reason = f"stdin closed: {exc}"
            raise BackendGone(self._dead_reason) from exc
    # Either we just sent it, or another stub's prime is in flight — wait
    # for _on_upstream_initialize / _fail_init to resolve the handshake.
    try:
        await asyncio.wait_for(self._init_done_event.wait(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        self._dead_reason = self._dead_reason or "initialize timed out on respawn"
        # The backend answered neither the handshake nor EOF within the
        # window: it is wedged (process alive, initialize never completing).
        # Mark init failed + wake any co-primer so they fail fast instead of
        # each waiting the full timeout, and reap the wedged process now
        # rather than leaving it marked-dead-but-running until the idle
        # sweep reclaims it. shutdown() is idempotent and self-contained —
        # it kills the process group but touches no pool bookkeeping, so
        # there is no reserve/evict race with a concurrent acquirer.
        self._init_state = "failed"
        self._init_done_event.set()
        with contextlib.suppress(Exception):
            await self.shutdown()
        raise BackendGone(self._dead_reason) from exc
    # ``is_alive`` too: a pump that ended after the handshake leaves the
    # state "ready" on a backend nothing is reading any more.
    if self._init_state != "ready" or not self.is_alive:
        raise BackendGone(self._dead_reason or "backend initialize failed on respawn")


async def _retry_after_rehandshake(self: Backend, pending: _PendingRequest) -> bool:
    """Handshake the server again and send ``pending``'s request once more.

    Called when a forwarded request came back as the not-initialized refusal
    (:func:`_is_lost_session_error`). Returns True when the request is back
    on the wire, so its first answer must not reach the stub; False leaves
    the caller to deliver that answer unchanged.

    The ``initialize``, the ``notifications/initialized`` and the retried
    request go out in ONE write. A server reads its stdin in order, so the
    retry cannot overtake the handshake, and no co-tenant's request can land
    between the two halves of it -- a Python server that has just seen an
    ``initialize`` refuses everything until ``initialized`` arrives. The
    reply to the ``initialize`` is swallowed: the cache already answered
    every stub, and the server is the same one.
    """
    init_frame = self._upstream_init_frame
    if init_frame is None or self._init_state != "ready" or not self.is_alive:
        return False
    async with self._inbox_lock:
        attached = pending.stub_uuid in self._stub_inboxes
    if not attached:
        return False
    assert pending.retry_frame is not None
    init_fid = self._next_forward_id()
    retry_fid = self._next_forward_id()
    # A re-sent initialize is a new attempt on the same process.
    self.attempt_id = lifecycle_log.new_lifecycle_id()
    self._pending_requests[init_fid] = _PendingRequest(
        stub_uuid=_REHANDSHAKE_STUB_SENTINEL,
        original_id=None,
        method="initialize",
        # Stamped like every other pending: the wedge sweep reads an unset
        # start as an hour-old request.
        t_start_ms=time.monotonic() * 1000.0,
        attempt_id=self.attempt_id,
    )
    retry_frame = dict(pending.retry_frame)
    retry_frame["id"] = retry_fid
    # Same stub, original id, start time and captured tool fields, so the
    # answer, a cancel and the metrics all read as the one request it is.
    self._pending_requests[retry_fid] = replace(pending, retry_frame=None)
    logger.warning(
        "backend pid=%s server=%s refused %s as not initialized; its MCP session "
        "was lost behind the pipe (a pooling proxy respawned it cold?) -- "
        "re-sending initialize and retrying once %s",
        self.pid,
        _log_safe_identifier(self.pool_key.server_name),
        _log_safe_identifier(pending.method),
        self.lifecycle_fields("initialize_started"),
    )
    frames = [
        {**init_frame, "id": init_fid},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        retry_frame,
    ]
    self.touch()
    try:
        await _write_json_lines(self.stdin, frames)
    except (BrokenPipeError, ConnectionResetError) as exc:
        self._pending_requests.pop(init_fid, None)
        self._pending_requests.pop(retry_fid, None)
        self._dead_reason = f"stdin closed during rehandshake: {exc}"
        return False
    return True


async def _fail_init(
    self: Backend,
    reason: str,
    *,
    log_event: str = "initialize_failed",
    log_reason: str = "unknown",
) -> None:
    """Transition init to the terminal ``"failed"`` state and flush every
    queued waiter with an explicit JSON-RPC error.

    Called from :meth:`_on_upstream_initialize` when the backend's reply
    is a JSON-RPC error or a malformed result. Without this path, stubs
    queued in ``_init_pending`` during the in-flight window would hang
    forever, and future stubs would keep piling into ``_init_pending``
    because ``is_alive`` is still True.
    """
    self._init_state = "failed"
    self._dead_reason = self._dead_reason or f"init failed: {reason}"
    self._init_done_event.set()
    self._cancel_init_deadline()
    logger.error(
        "backend pid=%s %s %s",
        self.pid,
        self._dead_reason,
        self.lifecycle_fields(log_event, log_reason),
    )
    pending = list(self._init_pending)
    self._init_pending.clear()
    async with self._inbox_lock:
        inboxes = dict(self._stub_inboxes)
    for stub_uuid, original_id in pending:
        inbox = inboxes.get(stub_uuid)
        if inbox is None:
            continue
        err = {
            "jsonrpc": "2.0",
            "id": original_id,
            "error": {"code": -32000, "message": f"backend init failed: {reason}"},
        }
        await self._enqueue_to_stub(
            stub_uuid,
            inbox,
            (json.dumps(err, separators=(",", ":")) + "\n").encode("utf-8"),
        )


async def _on_upstream_initialize(self: Backend, response: dict[str, Any]) -> None:
    """Process the backend's reply to the first stub's ``initialize``.

    Caches the ``result`` so later stubs can be served locally; detects
    the caller-identity capability; flushes every queued stub.

    On error (backend returned a JSON-RPC error, or a malformed result),
    transitions to the terminal ``"failed"`` state and flushes all
    queued stubs with an explicit error response. Without this a stub
    that registered during the in-flight window would hang forever
    waiting for a cached-initialize that never arrives.
    """
    try:
        result, self.supports_caller_identity = _parse_initialize_response(response)
    except ValueError as exc:
        await self._fail_init(str(exc))
        return
    self.reports_inflight = _reports_inflight(result)
    self._init_result = result
    self._init_state = "ready"
    self._init_done_event.set()
    self._cancel_init_deadline()
    logger.info(
        "backend pid=%s initialized supports_caller_identity=%s %s",
        self.pid,
        self.supports_caller_identity,
        self.lifecycle_fields("initialize_succeeded", "success"),
    )
    # Forward exactly one synthetic
    # notifications/initialized to the backend now the handshake is
    # complete. Stub-originated copies are always suppressed upstream, so
    # without this a backend that gates tool processing on the
    # notification would never receive it and would hang.
    try:
        await _write_json_line(
            self.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
    except (BrokenPipeError, ConnectionResetError) as exc:  # pragma: no cover
        self._dead_reason = f"stdin closed during initialized: {exc}"
    pending = list(self._init_pending)
    self._init_pending.clear()
    for stub_uuid, original_id in pending:
        await self._deliver_cached_initialize(stub_uuid, original_id, result)


async def send_initialize(
    backend: Backend,
    *,
    client_info: Optional[Mapping[str, Any]] = None,
    timeout: float = _DEFAULT_INITIALIZE_TIMEOUT_SECS,
) -> dict[str, Any]:
    """Send the MCP ``initialize`` request and parse the response.

    Side effect: sets ``backend.supports_caller_identity`` based on
    ``capabilities.experimental.kirocrew.caller-identity`` in the response.
    Backends that don't advertise the capability are tagged as
    caller-identity-unaware; the routing layer falls back to per-session
    spawn for them (no cross-tenant injection of ``_meta.kirocrew.caller``).

    Raises :class:`asyncio.TimeoutError` if the backend doesn't respond
    within ``timeout`` seconds, :class:`ValueError` on malformed responses.
    """
    # Invariant: this helper reads ``backend.stdout`` directly to consume the
    # initialize reply, so it MUST run before the stdout pump owns the stream.
    # Every caller today invokes it pre-pump; if a future caller runs it while
    # the pump is active the two would steal frames from each other. Fail loud
    # rather than race silently.
    if backend._stdout_task is not None:
        raise RuntimeError(
            "send_initialize() must run before the stdout pump starts; "
            "the running pump owns backend.stdout"
        )

    def _request(version: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": _GATEWAY_INIT_ID,
            "method": "initialize",
            "params": {
                "protocolVersion": version,
                "capabilities": {"experimental": {INFLIGHT_CAPABILITY_KEY: {}}},
                "clientInfo": dict(client_info or {"name": "kirocrew-gateway", "version": "0"}),
            },
        }

    # Backends may emit log lines to stdout before the JSON-RPC response;
    # skip anything that isn't a well-formed JSON-RPC object addressed to
    # our init id. Bounded by ``timeout`` so a flood of noise still fails.
    async def _await_response() -> dict[str, Any]:
        while True:
            line = await backend.stdout.readuntil(b"\n")
            msg = parse_json_object_line(line)
            if msg is None:
                logger.debug("backend pre-init line not a JSON object; dropping: %r", line[:200])
                continue
            if msg.get("id") != _GATEWAY_INIT_ID:
                continue
            return msg

    # Offer our newest revision; a -32602 refusal listing ``data.supported``
    # gets one retry with the newest older one, as the MCP probes do. Imported
    # here so the names stay off this owner's module namespace, which the
    # ``backend`` facade re-exports.
    from kiro_crew.mcp_discovery_probe_state import (
        MCP_CLIENT_PROTOCOL_VERSION,
        downgrade_protocol_version,
    )

    version = MCP_CLIENT_PROTOCOL_VERSION
    while True:
        await _write_json_line(backend.stdin, _request(version))
        try:
            response = await asyncio.wait_for(_await_response(), timeout=timeout)
        except asyncio.IncompleteReadError as exc:
            raise ValueError(
                f"backend closed stdout before initialize response: got {len(exc.partial)} bytes"
            ) from exc
        fallback = downgrade_protocol_version(response, version)
        if not fallback:
            break
        version = fallback

    result, backend.supports_caller_identity = _parse_initialize_response(response)
    backend.reports_inflight = _reports_inflight(result)
    # Seed the init cache so a multi-stub flow can replay the result to
    # later attachers without re-issuing the handshake. Single-stub callers
    # (the M1 path) never observe this cache but the tests that drive the
    # full M2 flow rely on ``_init_state == "ready"`` after initialize.
    #
    # NOTE: unlike the lazy _on_upstream_initialize path, this does NOT send
    # the synthetic notifications/initialized to the backend. Correct for
    # today's callers (production spawns take the lazy path; send_initialize
    # callers don't gate on it), but a future caller that relies on the
    # backend having received notifications/initialized here would hang —
    # send it explicitly if you add such a path.
    backend._init_result = result
    backend._init_state = "ready"
    logger.info(
        "backend pid=%s initialized; supports_caller_identity=%s %s",
        backend.pid,
        backend.supports_caller_identity,
        backend.lifecycle_fields("initialize_succeeded", "success"),
    )
    return result
