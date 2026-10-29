"""The stdio dispatch loop behind ``run_mcp_stdio_loop``: one worker thread, one frame at a time.

``_run_stdio_dispatch_loop`` answers ``initialize``, ``tools/list`` and ``tools/call``,
runs each call on the worker, queues and parks calls while it is busy, drives deferred
tools on the step pool, honours cancellations and the in-flight report, and wakes on
the self-pipe instead of polling.

Split out of ``kiro_crew.mcp_shared``, which stays the import path and the patch target: every
name here is reached as ``kiro_crew.mcp_shared.<name>``, and a patch through that facade
reaches this module (``kiro_crew._facade_mirror``). Nothing here imports the facade.
New dispatch work goes here.
"""

from __future__ import annotations

import collections
import concurrent.futures
import contextlib
import functools
import logging
import os
import queue
import select
import sys
import threading
import time
from typing import Any, Callable, Optional

from kiro_crew import platform_compat
from kiro_crew.install_liveness import install_pruned, respawned_by_pool
from kiro_crew.mcp_caller import (
    INFLIGHT_CAPABILITY_KEY,
    INFLIGHT_NOTIFICATION,
    CallerContext,
    caller_identity_capability,
    set_current_caller,
    set_current_tenant_nonce,
    tenant_nonce_from_meta,
)
from kiro_crew.mcp_shared_calls import (
    DEFERRED_PARKED_MAX,
    DEFERRED_STEP_STUCK_SECS,
    DEFERRED_STEP_WORKERS,
    INFLIGHT_REPORT_SECS,
    PENDING_CALLS_MAX,
    DeferredTool,
    ToolCancelled,
    _declares_inflight_capability,
    _DeferredEntry,
    _remember_cancelled_id,
    _StepPool,
)
from kiro_crew.mcp_shared_policy import (
    _NEGATIVE_CACHE_TTL,
    _POLICY_DETAIL_MAX_CHARS,
    _UNRESOLVED_REFUSES_CALL,
    ToolPolicy,
    _ambient_audit_session,
    _resolve_tool_policy,
    external_client_identity_note,
    serves_managed_tools,
    spawned_without_gateway_identity,
)
from kiro_crew.mcp_shared_wire import (
    NO_FRAME,
    _NoFrame,
    _read_message,
    _Skipped,
    _start_stdin_pump,
    _stdin_holds_a_line,
    notify,
    respond,
)
from kiro_crew.sel import sel
from kiro_crew.session_directive import neutralize_markers
from kiro_crew.validation import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JsonRpcEnvelopeError,
    ValidationError,
    build_tool_response,
    validate_jsonrpc_request,
)

#: The family logs as ``kiro_crew.mcp_shared``, the name callers and log filters use.
logger = logging.getLogger("kiro_crew.mcp_shared")


#: JSON-RPC 2.0 "method not found". Spelled here, not imported from
#: ``kiro_crew.acp.types``: importing that module runs the ``kiro_crew.acp``
#: package, which pulls the whole ACP client into every stdio MCP server.
JSONRPC_METHOD_NOT_FOUND = -32601


def _tool_call_name(params: dict[str, Any]) -> str | None:
    """The tool a ``tools/call`` names, or ``None`` when it names none usable."""
    name = params.get("name")
    return name if isinstance(name, str) and name else None


#: Backstop wait while only a worker runs. Not a latency: the wake pipe ends
#: the wait as soon as the worker finishes (see :class:`_WakePipe`).
_WORKER_WAIT_SECS = 1.0


#: Poll interval when no wake pipe could be made, and while parked calls are
#: waiting for their next ``due_at`` (their schedule is still polled).
_POLL_SECS = 0.1


class _WakePipe:
    """A self-pipe that wakes the read loop's ``select`` when work finishes.

    The tool worker and the deferred-step pool write one byte when they finish;
    the loop selects on stdin AND :attr:`read_fd`, so a finished call is
    answered at once instead of at the next poll tick. POSIX only: Windows
    ``select`` cannot take a pipe, so its stdin-pump loop keeps the
    _POLL_SECS tick.

    Writes and close share one lock, so a worker still running after the loop
    exits (EOF joins it with a timeout) never writes into a closed -- or
    reused -- descriptor.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.read_fd: int | None = None
        self._write_fd: int | None = None
        if not platform_compat.IS_POSIX:
            return
        try:
            r, w = os.pipe()
        except OSError:
            return
        try:
            os.set_blocking(r, False)
            os.set_blocking(w, False)
        except OSError:
            os.close(r)
            os.close(w)
            return
        self.read_fd, self._write_fd = r, w

    def wake(self, *_args: Any) -> None:
        """Never raises: a full pipe already wakes the loop. Accepts and ignores
        a future, so it doubles as a ``Future.add_done_callback`` hook."""
        with self._lock:
            if self._write_fd is None:
                return
            with contextlib.suppress(OSError):
                os.write(self._write_fd, b"\0")

    def drain(self) -> None:
        with self._lock:
            if self.read_fd is None:
                return
            with contextlib.suppress(OSError):
                while os.read(self.read_fd, 4096):
                    pass

    def close(self) -> None:
        with self._lock:
            for fd in (self.read_fd, self._write_fd):
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            self.read_fd = self._write_fd = None


def _run_stdio_dispatch_loop(
    server_name: str,
    server_version: str,
    list_tools_fn: Callable[[], list[dict[str, Any]]],
    call_tool_fn: Callable[[str, dict[str, Any]], "str | DeferredTool"],
    *,
    advertise_caller_identity: bool = False,
    error_prefix_is_error: bool = False,
    reports_inflight: bool = False,
    instructions: str = "",
) -> bool:
    """Read/dispatch body of :func:`run_mcp_stdio_loop`.

    Split out so the public entry point can own the stdout-snapshot lifecycle
    (capture before the first request, release on exit) without indenting the
    whole dispatch loop under a ``try``.

    Returns True when it stopped because this process's install was pruned
    (see :func:`_refuse_from_pruned_install`), False on an ordinary EOF.
    """
    # In-flight tool execution state: at most one WORKER at a time (sequential
    # dispatch of the part of a call that computes). A call that only waits
    # leaves the worker as a DeferredTool and lives in ``_deferred`` below.
    _current_req_id: Any = None
    _current_caller_key: str = ""
    _current_caller_ctx: "CallerContext | None" = None
    _current_tenant_nonce: str = ""
    _cancel_event: Optional[threading.Event] = None
    _worker_thread: Optional[threading.Thread] = None
    _result_lock = threading.Lock()
    _result_ready = threading.Event()
    _result_box: list = []  # [response_payload | DeferredTool] or [] if cancelled
    _cancelled_ids: set = set()
    # Insertion-order tracker for _cancelled_ids so it can be pruned FIFO once
    # it reaches CANCELLED_IDS_MAX (prevents unbounded growth on long-lived
    # per-session MCP processes that receive many cancels). Bounding is done by
    # the module-level _remember_cancelled_id() so it is unit-testable.
    _cancelled_order: collections.deque[str] = collections.deque()

    _current_tool_name: str = ""
    _worker_audited: list = [False]  # [bool], guarded by _result_lock
    # tools/call requests received while a worker was busy, dispatched FIFO.
    _pending_calls: collections.deque[dict[str, Any]] = collections.deque()
    # Set by ``_dispatch`` when a pruned install answered every call and the
    # loop must stop so the pool respawns this server.
    _pruned_exit = False
    # Deferred calls (``DeferredTool``), keyed by ``str(req_id)``. Owned by the
    # dispatch thread: only it adds, steps and removes entries. Steps run on
    # ``_step_pool`` so a step blocked on the gateway never blocks the loop.
    _deferred: dict[str, _DeferredEntry] = {}
    _step_pool: Optional[_StepPool] = None
    # Whether the client's ``initialize`` declared INFLIGHT_CAPABILITY_KEY, and
    # when the next report is due. Reports go out only while calls are parked
    # or queued behind the worker.
    _client_wants_inflight = False
    _next_inflight_report = 0.0

    def _live_request_ids() -> set[str]:
        """Ids of the active, parked and still-queued requests whose
        cancellation flags must survive FIFO eviction.

        If a flood of unrelated cancels evicted one of these before the
        dispatch loop consumed it (``str(req_id) in _cancelled_ids``), a
        cancelled queued call would execute -- for a destructive tool that is a
        data-mutation path. Passed to ``_remember_cancelled_id`` as protected.
        """
        ids: set[str] = set()
        if _current_req_id is not None:
            ids.add(str(_current_req_id))
        ids.update(_deferred)
        for _pc in _pending_calls:
            _pcid = _pc.get("id")
            if _pcid is not None:
                ids.add(str(_pcid))
        return ids

    def _sel_audit(outcome: str, tool_name: str, req_id: Any, session_key: str = "") -> None:
        """Emit a SEL audit event for a tool invocation outcome.

        ``session_key`` should be the request's parsed caller identity when
        available (pooled topology: an ambient read attributes every
        outcome to ``mcp`` or the wrong session in a shared backend);
        :func:`_ambient_audit_session` is the single-session fallback.

        SEL failure must not break the response path, but a missed audit
        record must be visible (security-controls guideline: callback
        failures are logged, never bare pass)."""
        try:
            sel().log_tool_invocation(
                session_key=session_key or _ambient_audit_session(),
                source="mcp",
                tool_name=tool_name,
                tool_kind=server_name,
                outcome=outcome,
                request_id=str(req_id),
            )
        except Exception as sel_exc:
            logger.warning(
                "SEL audit failed for %s tool %s (request %s): %s",
                outcome,
                tool_name,
                req_id,
                sel_exc,
            )

    def _req_caller(params: dict[str, Any]) -> "CallerContext | None":
        """A request's own identity, from its normalized ``params``.

        Never the active worker's caller. ``from_meta`` returns ``None`` for
        any malformed block, so this cannot raise.
        """
        return CallerContext.from_meta(params.get("_meta"))

    def _req_caller_key(params: dict[str, Any]) -> str:
        ctx = _req_caller(params)
        return ctx.session_key if ctx is not None else ""

    def _answer_internal_error(method: str, req_id: Any) -> None:
        """One message's dispatch raised: log it, answer it, keep serving."""
        logger.exception("%s: dispatching %r failed; the loop continues", server_name, method)
        if req_id is not None:
            respond(
                req_id,
                None,
                error={"code": JSONRPC_INTERNAL_ERROR, "message": "Internal error"},
            )

    _applies_tool_policy = serves_managed_tools(call_tool_fn)

    def _caller_tool_policy(caller: "CallerContext | None") -> ToolPolicy:
        """This request's exclusion set AND whether the policy was readable.

        The enforcement seam: the ``tools/call`` branch needs the second half
        to tell "the operator excluded nothing" apart from "we could not read
        what the operator excluded".

        The caller block's token rides along explicitly. This runs on the read
        loop before the worker installs the caller ContextVar, so the resolver
        cannot find the token there; a pooled backend has it nowhere else.

        A server that is not Kiro Crew's own (:func:`serves_managed_tools`) has
        no managed tools for the policy to name, so it is answered "nothing
        excluded" without a read.
        """
        if not _applies_tool_policy:
            return ToolPolicy(frozenset(), "")
        return _resolve_tool_policy(
            caller.session_key if caller else "",
            caller_token=caller.session_token if caller else "",
        )

    def _listable_tools(caller: "CallerContext | None") -> list[dict]:
        """The tool list for a ``tools/list``, minus this session's exclusions.

        An unresolved policy lists EVERYTHING on purpose. kiro-cli calls
        ``tools/list`` once per session and caches the answer, so hiding tools
        on a transient policy failure would hide them for the session's whole
        life. Listing is not the enforcement point; ``tools/call`` is, and it
        refuses while the policy is unresolved. The audit event records that a
        listing went out unfiltered so the widened window is attributable.
        """
        policy = _caller_tool_policy(caller)
        tools = list_tools_fn()
        if policy.unresolved:
            try:
                sel().log_api_access(
                    caller=(caller.session_key if caller is not None else _ambient_audit_session()),
                    operation="tool_policy.unfiltered_listing",
                    outcome="unresolved",
                    source="mcp",
                    resources=f"reason={policy.unresolved}",
                )
            except Exception as sel_exc:
                logger.warning("SEL audit failed for unfiltered listing: %s", sel_exc)
            return tools
        if policy.excluded:
            tools = [t for t in tools if t.get("name") not in policy.excluded]
        included = policy.include_for(server_name)
        if included is not None:
            tools = [t for t in tools if t.get("name") in included]
        return tools

    def _tool_response(text: str) -> dict[str, Any]:
        """Frame a tool result, flagging ``Error:`` prose when opted in."""
        flagged = error_prefix_is_error and text.startswith("Error:")
        return build_tool_response(text, is_error=flagged)

    def _run_tool(
        req_id: Any,
        tool_name: str,
        tool_args: dict,
        cancel_evt: threading.Event,
        caller_ctx: "CallerContext | None" = None,
        tenant_nonce: str = "",
    ) -> None:
        """Worker thread: run tool, store result unless cancelled."""
        # Install the verified per-call caller for identity resolvers. A
        # ContextVar, so this worker and the deferred-step pool threads (which
        # install their own call's caller the same way) never see each other's.
        set_current_caller(caller_ctx)
        # And the connection's namespace separator, which is present even when
        # the caller is not: a tool that keys per-tenant state for a caller the
        # gateway could not name reads it instead of a process-global fallback.
        # Cleared in the same places as the caller.
        set_current_tenant_nonce(tenant_nonce)
        try:
            result = call_tool_fn(tool_name, tool_args)
        except ToolCancelled:
            # Tool cooperatively exited on cancel -- suppress response
            logger.info("tool cancelled for request %s", req_id)
            # SEL audit: cancelled tool invocations must emit audit events
            _sel_audit(
                "cancelled",
                tool_name,
                req_id,
                caller_ctx.session_key if caller_ctx else "",
            )
            set_current_caller(None)
            set_current_tenant_nonce("")
            _result_ready.set()
            _wake.wake()
            return
        except Exception as exc:
            result = f"Error: {neutralize_markers(str(exc))}"  # not a directive: see call_tool_with_logging
            _tool_errored = True
        else:
            _tool_errored = False
        finally:
            set_current_caller(None)
            set_current_tenant_nonce("")
        # Audit decision is made atomically with the cancellation check, under
        # the same lock that guards response delivery: exactly ONE audit event
        # per request (a failed+late-cancel race must not emit two).
        with _result_lock:
            if not cancel_evt.is_set():
                # A DeferredTool is boxed as-is: the loop registers it and
                # answers the request when a later step settles it.
                _result_box.append(
                    result if isinstance(result, DeferredTool) else _tool_response(result)
                )
                if _tool_errored:
                    # Exception escaped call_tool_fn (may bypass its internal
                    # logging) -- audit the failure.
                    _sel_audit(
                        "failed",
                        tool_name,
                        req_id,
                        caller_ctx.session_key if caller_ctx else "",
                    )
                    _worker_audited[0] = True
            else:
                # Late-cancel race: tool finished (or errored) but cancel
                # arrived before delivery. From the client's perspective this
                # invocation was cancelled.
                if isinstance(result, DeferredTool):
                    _cancel_quietly(result, req_id, caller_ctx, tenant_nonce)
                _sel_audit(
                    "cancelled",
                    tool_name,
                    req_id,
                    caller_ctx.session_key if caller_ctx else "",
                )
                _worker_audited[0] = True
        _result_ready.set()
        _wake.wake()

    def _cancel_quietly(
        deferred: DeferredTool,
        req_id: Any,
        caller_ctx: "CallerContext | None",
        tenant_nonce: str,
    ) -> None:
        """Run cleanup with the call's identity; a failing hook cannot end the loop.

        The dispatch thread holds no call identity outside this hook; late-worker
        cleanup likewise runs after the worker clears its identity in ``finally``.
        """
        set_current_caller(caller_ctx)
        set_current_tenant_nonce(tenant_nonce)
        try:
            deferred.cancel()
        except Exception:  # noqa: BLE001 - best-effort hook
            logger.warning(
                "%s: cancel hook failed for request %s", server_name, req_id, exc_info=True
            )
        finally:
            set_current_caller(None)
            set_current_tenant_nonce("")

    def _abandon_quietly(entry: _DeferredEntry) -> str | None:
        """Ask a parked call that will never step again for its final text,
        with its identity installed; a raising hook reads as ``None``, so the
        call falls back to the retryable refusal."""
        set_current_caller(entry.caller_ctx)
        set_current_tenant_nonce(entry.tenant_nonce)
        try:
            text = entry.deferred.abandon()
        except Exception:  # noqa: BLE001 - best-effort hook
            logger.warning(
                "%s: abandon hook failed for request %s", server_name, entry.req_id, exc_info=True
            )
            return None
        finally:
            set_current_caller(None)
            set_current_tenant_nonce("")
        return text if isinstance(text, str) else None

    def _refuse_from_pruned_install(first_req_id: Any, tool_name: str) -> None:
        """Answer this call and every queued one, so none is left waiting.

        The process is about to exit: anything it lazily imports would fail
        with ``No module named 'kiro_crew.<x>'`` until it is replaced, and the
        caller should hear "retry" rather than that error.
        """
        logger.warning(
            "%s: the install this process runs from was removed by an update; "
            "refusing %s and exiting so it is respawned from the current install",
            server_name,
            tool_name,
        )
        error = {
            "code": -32000,
            "message": (
                f"{server_name} is running from an install that an update removed "
                "and is restarting from the current one; retry the call"
            ),
        }
        respond(first_req_id, None, error=error)
        while _pending_calls:
            queued = _pending_calls.popleft()
            queued_id = queued.get("id")
            queued_params = queued.get("params")
            queued_params = queued_params if isinstance(queued_params, dict) else {}
            queued_tool = _tool_call_name(queued_params) or ""
            if queued_id is not None and str(queued_id) in _cancelled_ids:
                # Same contract as the ordinary dispatch path: a request
                # cancelled while it waited gets no response, only its audit.
                _sel_audit("cancelled", queued_tool, queued_id, _req_caller_key(queued_params))
                continue
            # Each refusal is its own invocation decision and gets its own SEL
            # record, like the first call's (security-controls: every
            # invocation decision is audited).
            _sel_audit(
                "rejected_install_pruned",
                queued_tool,
                queued_id,
                _req_caller_key(queued_params),
            )
            respond(queued_id, None, error=error)

    def _refuse_tool_call(req_id: Any, caller_key: str, detail: str) -> None:
        """Answer a ``tools/call`` that names no usable tool, and audit the refusal.

        Never dispatched, and never audited as an invocation of a tool: the SEL
        record is the refusal decision, with the shape of what was sent in
        place of a name.
        """
        try:
            sel().log_api_access(
                caller=caller_key or _ambient_audit_session(),
                operation="tool_call.invalid_params",
                outcome="rejected",
                source="mcp",
                resources=f"server={server_name},{detail}",
            )
        except Exception as sel_exc:
            logger.warning("SEL audit failed for a refused tool call: %s", sel_exc)
        if req_id is not None:
            respond(
                req_id,
                None,
                error={
                    "code": JSONRPC_INVALID_PARAMS,
                    "message": "Invalid params: tools/call takes an object params "
                    "with a non-empty string name",
                },
            )

    def _servable(req: dict[str, Any]) -> tuple[str, Any, dict[str, Any]] | None:
        """``(method, id, params)`` of a request this loop serves, else ``None``.

        Runs once per message, before the busy/idle split, so a request either
        path cannot serve is refused the same way under load as at rest: a
        malformed envelope is answered ``-32600``, params that are not an
        object ``-32602``, and a ``tools/call`` with no usable tool name is
        refused through :func:`_refuse_tool_call`. A notification is never
        answered.
        """
        try:
            method, req_id, params = validate_jsonrpc_request(req)
        except JsonRpcEnvelopeError as exc:
            if exc.method == "tools/call" and exc.invalid_params:
                _refuse_tool_call(exc.req_id, "", f"params_type={type(req.get('params')).__name__}")
            elif exc.req_id is not None:
                code = JSONRPC_INVALID_PARAMS if exc.invalid_params else JSONRPC_INVALID_REQUEST
                respond(exc.req_id, None, error={"code": code, "message": f"Invalid {exc}"})
            return None
        except ValidationError:
            return None
        if method == "tools/call" and _tool_call_name(params) is None:
            _refuse_tool_call(
                req_id, _req_caller_key(params), f"name_type={type(params.get('name')).__name__}"
            )
            return None
        return method, req_id, params

    def _dispatch(method: str, req_id: Any, params: dict[str, Any]) -> None:
        """Serve one request while no tool is running (the idle path)."""
        nonlocal _worker_thread, _current_req_id, _current_tool_name
        nonlocal _current_caller_key, _current_caller_ctx, _current_tenant_nonce
        nonlocal _cancel_event, _pruned_exit, _client_wants_inflight
        if method == "initialize":
            _caps: dict[str, Any] = {"tools": {"listChanged": False}}
            _experimental: dict[str, Any] = {}
            if advertise_caller_identity:
                # Pooled-operation opt-in for IDENTITY, not for pooling:
                # gatewayd injects the per-call ``_meta.kirocrew.caller`` block
                # only into a backend that advertised this capability, and
                # nothing declines to POOL one that did not (see
                # ``rewriter.UNPOOLABLE_SERVERS``, which lists only credential vendors and documents
                # exactly that). So NOT advertising does not buy a per-session
                # spawn -- it buys a shared process whose dispatch-loop caller
                # slot never receives gateway-authored metadata, which is how a
                # session-scoped tool silently degrades to unattached behaviour.
                _experimental.update(caller_identity_capability())
            _sends_inflight = reports_inflight
            if _sends_inflight:
                # Only a server that opted in advertises: a gateway that
                # believes a backend reports applies the short recycle ceiling
                # to its calls.
                _experimental[INFLIGHT_CAPABILITY_KEY] = {}
            if _experimental:
                _caps["experimental"] = _experimental
            _client_wants_inflight = _sends_inflight and _declares_inflight_capability(params)
            _init_result: dict[str, Any] = {
                "protocolVersion": "2024-11-05",
                "capabilities": _caps,
                "serverInfo": {"name": server_name, "version": server_version},
            }
            if instructions:
                _init_result["instructions"] = instructions
            respond(req_id, _init_result)
        elif method == "notifications/initialized":
            pass
        elif method == "notifications/cancelled":
            # Cancel for a request that already completed -- ignore -- or for a
            # parked (deferred) one, which is live and gets settled here. Route
            # through the bounded recorder (not a raw set.add) so this idle
            # path honors the FIFO cap and keeps ``_cancelled_ids`` and
            # ``_cancelled_order`` in lockstep -- a raw add would grow the set
            # past the cap while the deque lagged, later crashing the eviction
            # loop with an empty-deque popleft.
            cancelled_rid = params.get("requestId")
            if cancelled_rid is not None:
                _remember_cancelled_id(
                    _cancelled_ids,
                    _cancelled_order,
                    str(cancelled_rid),
                    protected=_live_request_ids(),
                )
                _cancel_deferred(str(cancelled_rid))
        elif method == "tools/list":
            respond(req_id, {"tools": _listable_tools(_req_caller(params))})
        elif method == "ping":
            respond(req_id, {})
        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            if not isinstance(tool_args, dict):
                tool_args = {}
            # Verified per-call identity (pooled topology): gatewayd strips
            # any client-forged ``kirocrew.caller`` block and injects its own
            # on every forwarded call, so a block present here is
            # gateway-authored. None in the non-pooled stdio topology.
            _caller_ctx = _req_caller(params)
            # The connection's namespace separator. Parsed separately because it
            # arrives WITHOUT an identity for a caller the gateway could not
            # name — the case it exists for — so it cannot be folded
            # into ``_caller_ctx``, which is None exactly then.
            _tenant_nonce = tenant_nonce_from_meta(params.get("_meta"))
            # A queued request may have been cancelled while waiting -- emit
            # no response (per MCP spec) but audit the cancellation.
            if req_id is not None and str(req_id) in _cancelled_ids:
                _sel_audit(
                    "cancelled",
                    tool_name,
                    req_id,
                    _caller_ctx.session_key if _caller_ctx else "",
                )
                return
            # Checked before the policy read and the dispatch: both import
            # lazily, so on a pruned install they fail with an import error
            # instead of a refusal the caller can retry.
            if install_pruned():
                _sel_audit(
                    "rejected_install_pruned",
                    tool_name,
                    req_id,
                    _caller_ctx.session_key if _caller_ctx else "",
                )
                if respawned_by_pool():
                    _refuse_from_pruned_install(req_id, tool_name)
                    _pruned_exit = True
                    return
                # Launched directly by kiro-cli, or pooled with a respawn
                # command that went with the prune: nothing would bring this
                # process back, so exiting would take every tool away for the
                # rest of the session. Keep the transport and refuse each call
                # with the action that actually recovers it.
                logger.warning(
                    "%s: the install this process runs from was removed by an "
                    "update; refusing %s until Kiro Crew is restarted",
                    server_name,
                    tool_name,
                )
                respond(
                    req_id,
                    None,
                    error={
                        "code": -32000,
                        "message": (
                            f"{server_name} is running from an install that an "
                            "update removed; restart Kiro Crew (kirocrew restart) "
                            "to load the current install"
                        ),
                    },
                )
                return
            # Defense-in-depth: reject calls to excluded tools even if
            # the LLM somehow attempts to call them (hallucination).
            # Per-call caller identity keys the policy in pooled backends.
            _policy = _caller_tool_policy(_caller_ctx)
            _policy_session = _caller_ctx.session_key if _caller_ctx else _ambient_audit_session()
            if _policy.unresolved and _policy.unresolved not in _UNRESOLVED_REFUSES_CALL:
                # An identity reason: no agent was named, so no operator
                # exclusion is known to exist for this call to bypass. The call
                # proceeds, and the window is audited so it is visible rather
                # than silent -- which is what made this condition hard to see.
                try:
                    sel().log_api_access(
                        caller=_policy_session,
                        operation="tool_policy.unenforced_call",
                        outcome="unresolved",
                        source="mcp",
                        resources=f"reason={_policy.unresolved},tool={tool_name}",
                    )
                except Exception as sel_exc:
                    logger.warning("SEL audit failed for unenforced call: %s", sel_exc)
            if _policy.unresolved in _UNRESOLVED_REFUSES_CALL:
                # Fail CLOSED. The operator's exclusion list could not
                # be read, so we do not know whether THIS tool is denied, and
                # an unknown deny is not a permission. Refusing costs the
                # caller a retry once the policy resolves (a startup race
                # clears in milliseconds); serving the call would have silently
                # widened every exclusion the operator set.
                #
                # This is the enforcement point, and the only one: the
                # ``tools/list`` branch deliberately still lists everything,
                # because kiro-cli caches one listing per session and an empty
                # one would be unrecoverable. A tool that is listed but refuses
                # is not a hole; a tool that is unlisted but runs is.
                logger.warning(
                    "Refusing tool call %r: session %s tool policy unresolved (%s)",
                    tool_name,
                    _policy_session,
                    _policy.unresolved,
                )
                sel().log_tool_invocation(
                    session_key=_policy_session,
                    source="mcp",
                    tool_name=tool_name,
                    tool_kind=server_name,
                    outcome="rejected_policy_unresolved",
                    error=f"managedToolPolicy.unresolved:{_policy.unresolved}",
                )
                if _policy.unresolved == "identity_unattested":
                    _refusal = (
                        f"Error: tool '{tool_name}' is unavailable because this "
                        f"server could not prove which session it acts for "
                        f"(identity_unattested): the gateway refused the tool-policy "
                        f"read for session {_policy_session} because the request "
                        f"carried no session token, or one that does not vouch for "
                        f"that session. Refusing the call rather than ignoring an "
                        f"operator's exclusion list."
                    )
                    # The daemon withheld the token from THIS backend for a reason
                    # it otherwise logs only to its own stdout; when the frame
                    # carries it, the refusal says it, because the generic text
                    # above points at the token and the spec, and the cause is
                    # neither (in the Toolbox-shim report it was the spawned binary).
                    # Only the reason and the restart note: the reasons that reach
                    # here name a filesystem root, the daemon's own spawn env, or
                    # the managed table, so a clause sending the operator to the
                    # agent spec would misdirect them the way the sibling
                    # ``resolution_failed`` text deliberately avoids. The reason
                    # quotes spec-derived paths and this early refusal does not
                    # pass through the scrubbers the tool path applies, so it
                    # gets both here: the directive defang and the credential
                    # redaction every egress site routes through.
                    _denial = _caller_ctx.identity_denial if _caller_ctx else ""
                    if _denial:
                        from kiro_crew.platform import redact_via_context

                        _safe_denial = redact_via_context(neutralize_markers(_denial))
                        _refusal += (
                            f" The gateway spawned this server without a token because: "
                            f"{_safe_denial}. It decides this once, at "
                            f"spawn, so a change takes "
                            f"effect at the gateway's next restart."
                        )
                    elif _caller_ctx is None and spawned_without_gateway_identity():
                        # No gateway caller, no token on the element, no launcher
                        # pid: nothing the gateway does when it starts a server
                        # happened to this one, so the declared key came from
                        # whoever wrote its config. The generic text names the
                        # missing token; this reader has nowhere to get one, and
                        # the note says so. A server the gateway did spawn keeps
                        # the wording above (token present, or the denial arm).
                        _refusal += external_client_identity_note(server_name)
                elif _policy.unresolved == "identity_unattestable":
                    # The key resolved from a lenient source with no attestation
                    # to carry it, so the gateway was never dialled -- a dial
                    # could only be refused. The remedy is the same channel the
                    # ``identity_unattested`` arm names (a session token on the
                    # element, or a gateway caller block), stated without claiming
                    # a gateway read that did not happen.
                    _refusal = (
                        f"Error: tool '{tool_name}' is unavailable because this "
                        f"server could not prove which session it acts for "
                        f"(identity_unattestable): session {_policy_session} "
                        f"resolved only from a source the gateway cannot attest "
                        f"(no session token on this server, no gateway-injected "
                        f"session key), so the tool policy was not read. Refusing "
                        f"the call rather than ignoring an operator's exclusion "
                        f"list; it succeeds once this server is started by a Kiro "
                        f"Crew session that gives it a signed session token."
                    )
                    if _caller_ctx is None and spawned_without_gateway_identity():
                        _refusal += external_client_identity_note(server_name)
                elif _policy.unresolved == "gateway_locked":
                    _refusal = (
                        f"Error: tool '{tool_name}' is unavailable because the "
                        f"gateway is locked until Kiro is signed in (gateway_locked), "
                        f"so session {_policy_session}'s tool policy could not be "
                        f"read. Refusing the call rather than ignoring an operator's "
                        f"exclusion list; sign in with kiro-cli on the gateway host "
                        f"and the call succeeds once the gateway reopens."
                    )
                elif _policy.unresolved == "resolution_failed":
                    # The exception class only; its message stays in the audit log.
                    _cause = f" (cause: {_policy.detail})" if _policy.detail else ""
                    # A DIFFERENT diagnosis and a different remedy from the branch
                    # below, which is why it cannot share that text: the gateway was
                    # never reached, so no agent spec is implicated and there is
                    # nothing for the caller to edit. Sending them to the agents
                    # directory would have them change healthy files to fix an
                    # outage, and the edit they made would then be the real defect.
                    _refusal = (
                        f"Error: tool '{tool_name}' is unavailable because this "
                        f"server could not reach the gateway to read session "
                        f"{_policy_session}'s tool policy (resolution_failed): the "
                        f"read got no answer, or the gateway answered that it is "
                        f"broken{_cause}. No agent spec is implicated and nothing needs "
                        f"editing. Refusing the call rather than ignoring an "
                        f"operator's exclusion list; the call succeeds on retry "
                        f"within {_NEGATIVE_CACHE_TTL:.0f}s of the gateway "
                        f"answering again."
                    )
                else:
                    # The gateway ANSWERED (a 409, or a 200 whose exclude this
                    # process could not use), so the cause is a spec on disk,
                    # never reachability. Saying so is the point of the second
                    # sentence: the condition shares its symptom with an
                    # unreachable gateway, and an operator who reads it as an
                    # outage restarts processes that are healthy while the spec
                    # stays broken. The list names every condition the
                    # endpoint answers this code for, not just the parse.
                    _refusal = (
                        f"Error: tool '{tool_name}' is unavailable because this "
                        f"session's tool policy could not be read "
                        f"({_policy.unresolved}): the gateway answered, but could not "
                        f"determine this agent's policy from the agents directory -- "
                        f"a spec there could not be read or parsed, two specs declare "
                        f"the same name, or a managedToolPolicy has the wrong shape. "
                        f"This is not a connection problem, and a restart will not "
                        f"clear it. Refusing the call rather than ignoring an "
                        f"operator's exclusion list; fix or remove the unreadable "
                        f"spec in the agents directory."
                    )
                    # The gateway's 409 body names the file and what to do with
                    # it; without that line the operator has to validate every
                    # file in the directory by hand to find the one this refusal
                    # means. Same scrubbers as the ``identity_unattested`` arm
                    # above, for the same reason -- the reason interpolates a
                    # filename from a user-writable directory and this early
                    # refusal does not pass through the tool path's scrubbers --
                    # plus the local-path pass, because one arm of the endpoint
                    # (two specs declaring one name) quotes full spec paths and
                    # the credential redactor has no path rule; the same chain
                    # ``mcp_core._crew_memory_unavailable_reason`` applies to
                    # its own gateway text. Bounded so a pathological filename
                    # cannot inflate the response -- AFTER the scrubbers, never
                    # before: a cut made first can land inside a token, and the
                    # fragment left behind fails to match the pattern the
                    # redactor knows, so the head of a secret would be echoed.
                    # The scrubbers see the whole text; the bound trims what
                    # they return. Absent (an older gateway), the text above
                    # stands alone, byte-identical to what it always was.
                    if _policy.detail:
                        from kiro_crew.platform import redact_via_context
                        from kiro_crew.security import redact_local_paths

                        _safe_detail = redact_local_paths(
                            redact_via_context(neutralize_markers(_policy.detail))
                        )[0][:_POLICY_DETAIL_MAX_CHARS]
                        _refusal += f" Gateway reason: {_safe_detail}"
                respond(req_id, _tool_response(_refusal))
            elif tool_name in _policy.excluded:
                sel().log_tool_invocation(
                    session_key=_policy_session,
                    source="mcp",
                    tool_name=tool_name,
                    tool_kind=server_name,
                    outcome="rejected_excluded",
                    error="managedToolPolicy.exclude",
                )
                respond(
                    req_id,
                    _tool_response(f"Error: tool '{tool_name}' is not available for this agent"),
                )
            elif (_included := _policy.include_for(server_name)) is not None and (
                tool_name not in _included
            ):
                # The agent's spec grants this server tool by tool and opted in
                # to having those refs honoured; a tool it did not name is not
                # this agent's, listed or not. An EMPTY include is a server the
                # spec withholds whole (``managedToolPolicy.excludeServers``).
                _whole = not _included
                sel().log_tool_invocation(
                    session_key=_policy_session,
                    source="mcp",
                    tool_name=tool_name,
                    tool_kind=server_name,
                    outcome="rejected_not_included",
                    error=(
                        "managedToolPolicy.excludeServers"
                        if _whole
                        else "managedToolPolicy.honourToolRefs"
                    ),
                )
                respond(
                    req_id,
                    _tool_response(
                        f"Error: tool '{tool_name}' is not available for this agent: its "
                        f"spec withholds every {server_name} tool"
                        if _whole
                        else f"Error: tool '{tool_name}' is not available for this agent: its "
                        f"spec grants {server_name} only through @{server_name}/<tool> refs "
                        f"and does not name this one"
                    ),
                )
            else:
                # Dispatch tool in worker thread so we can receive cancel notifications
                _cancel_event = threading.Event()
                _current_req_id = req_id
                _current_tool_name = tool_name
                _current_caller_key = _caller_ctx.session_key if _caller_ctx else ""
                _current_caller_ctx = _caller_ctx
                _current_tenant_nonce = _tenant_nonce
                _worker_audited[0] = False
                _result_ready.clear()
                _result_box.clear()
                _worker_thread = threading.Thread(
                    target=_run_tool,
                    args=(
                        req_id,
                        tool_name,
                        tool_args,
                        _cancel_event,
                        _caller_ctx,
                        _tenant_nonce,
                    ),
                    daemon=True,
                )
                try:
                    _worker_thread.start()
                except Exception:
                    # Nothing runs (out of threads under the scope's task
                    # ceiling): the next pass must not take an unstarted
                    # thread for a running or a finished one. The call is
                    # still an invocation decision, so it is audited as failed.
                    _sel_audit("failed", tool_name, req_id, _current_caller_key)
                    _worker_thread = None
                    _current_req_id = None
                    _cancel_event = None
                    raise
        elif req_id is not None:
            respond(
                req_id,
                None,
                error={
                    "code": JSONRPC_METHOD_NOT_FOUND,
                    "message": f"Unknown method: {method}",
                },
            )

    def _deliver_finished_worker(join_timeout: float) -> None:
        """Join the finished worker and deliver its result, or audit its cancel.

        A boxed :class:`DeferredTool` is not a result yet: it is registered in
        ``_deferred`` (unless the request was cancelled meanwhile) and answered
        by the step that settles it.
        """
        nonlocal _worker_thread, _current_req_id, _cancel_event
        if _worker_thread is not None:
            _worker_thread.join(timeout=join_timeout)
        _worker_thread = None
        with _result_lock:
            boxed = _result_box[0] if _result_box else None
            if boxed is not None and str(_current_req_id) not in _cancelled_ids:
                if isinstance(boxed, DeferredTool):
                    entry = _DeferredEntry(
                        _current_req_id,
                        _current_tool_name,
                        _current_caller_ctx,
                        _current_tenant_nonce,
                        boxed,
                    )
                    # The handler decides the result type, so admission bounds retention.
                    if len(_deferred) >= DEFERRED_PARKED_MAX:
                        # A refused call may already have acted (a spawn POSTed
                        # its children), which a retry would repeat.
                        _abandon_or_refuse(entry, functools.partial(_refuse_table_full, entry))
                    else:
                        _deferred[str(_current_req_id)] = entry
                else:
                    respond(_current_req_id, boxed)
            elif boxed is not None and not _worker_audited[0]:
                # Boxed result dropped due to cancellation (cancel arrived
                # after the worker delivered) -- audit it.
                if isinstance(boxed, DeferredTool):
                    _cancel_quietly(
                        boxed, _current_req_id, _current_caller_ctx, _current_tenant_nonce
                    )
                _sel_audit(
                    "cancelled",
                    _current_tool_name,
                    _current_req_id,
                    _current_caller_key,
                )
            _result_box.clear()
            # Consumed: drop the id so a completed request never lingers
            # in the cancelled set.
            _cancelled_ids.discard(str(_current_req_id))
        _current_req_id = None
        _cancel_event = None
        _result_ready.clear()

    def _run_step(entry: _DeferredEntry) -> str | None:
        """Pool thread: one step with the call's identity installed, as the
        worker had it. Reset after, since pool threads are reused."""
        # Stamped here, when the thread is actually held, not at submit: a
        # step queued behind others has not started being slow.
        entry.step_started = time.monotonic()
        set_current_caller(entry.caller_ctx)
        set_current_tenant_nonce(entry.tenant_nonce)
        try:
            return entry.deferred.step()
        finally:
            set_current_caller(None)
            set_current_tenant_nonce("")

    def _settle_deferred(key: str, entry: _DeferredEntry, payload: Any, *, error: bool) -> None:
        """Answer a parked call and forget it. ``payload`` is response text, or
        an exception when ``error`` (an escaped exception, audited as failed).

        Decided on the dispatch thread AFTER the cancel check, so the call's
        ``on_settled`` commit (its SEL row) runs exactly once and never beside
        a ``cancelled`` row for the same request.
        """
        del _deferred[key]
        _cancelled_ids.discard(key)
        if error:
            _answer_failed(entry, payload)
        else:
            _commit_settled(entry, payload)

    def _answer_failed(entry: _DeferredEntry, exc: Any) -> None:
        """Answer a call whose step escaped with ``exc``, audited as failed."""
        text = (
            f"Error: {neutralize_markers(str(exc))}"  # not a directive: see call_tool_with_logging
        )
        _sel_audit("failed", entry.tool_name, entry.req_id, entry.session_key)
        respond(entry.req_id, _tool_response(text))

    def _refuse_pool_start(entry: _DeferredEntry, exc: Any) -> None:
        """Answer a call whose step pool could not start, after letting the
        tool retract what it opened (a question card is withdrawn), as the
        other refusals of a call the loop will never step do."""
        _cancel_quietly(entry.deferred, entry.req_id, entry.caller_ctx, entry.tenant_nonce)
        _answer_failed(entry, exc)

    def _refuse_table_full(entry: _DeferredEntry) -> None:
        """Turn away a call the parked table has no room for: retryable."""
        _cancel_quietly(entry.deferred, entry.req_id, entry.caller_ctx, entry.tenant_nonce)
        _sel_audit("failed", entry.tool_name, entry.req_id, entry.session_key)
        respond(
            entry.req_id,
            None,
            error={"code": -32000, "message": "Server busy: parked-call table is full; retry"},
        )

    def _abandon_or_refuse(entry: _DeferredEntry, refuse: Callable[[], None]) -> None:
        """Answer a call the loop will never step again (not in ``_deferred``).

        A call that already did something a retry would repeat -- the children
        a spawn POSTed keep running -- says so through its ``abandon`` hook and
        is answered with that final text, settled like any other answer;
        otherwise ``refuse`` gives the caller's own refusal.
        """
        text = _abandon_quietly(entry)
        if text is not None:
            _commit_settled(entry, text)
        else:
            refuse()

    def _commit_settled(entry: _DeferredEntry, text: str) -> None:
        """Commit with the call's identity installed, then answer with ``text``."""
        set_current_caller(entry.caller_ctx)
        set_current_tenant_nonce(entry.tenant_nonce)
        try:
            entry.deferred.on_settled(text)
        except Exception:  # noqa: BLE001 - a failed commit must not lose the answer
            # The SEL write failing is logged, as _sel_audit logs its own
            # failures; the text the step produced is still the result.
            logger.warning(
                "%s: on_settled failed for request %s", server_name, entry.req_id, exc_info=True
            )
        finally:
            set_current_caller(None)
            set_current_tenant_nonce("")
        respond(entry.req_id, _tool_response(text))

    def _harvest_step(key: str, entry: _DeferredEntry) -> bool:
        """Consume a FINISHED step: answer or drop the call as its outcome and
        the cancel flag decide. Returns True when the entry was settled and
        removed, False when it stays parked (the step returned no text, or is
        still running)."""
        fut = entry.stepping
        if fut is None or not fut.done():
            return False
        entry.stepping = None
        try:
            outcome = fut.result()
        except ToolCancelled:
            # The step itself chose to end the call with no response.
            del _deferred[key]
            _cancelled_ids.discard(key)
            _sel_audit("cancelled", entry.tool_name, entry.req_id, entry.session_key)
            return True
        except Exception as exc:  # noqa: BLE001 - a step must not end the server
            if entry.cancelled:
                # Same as the normal-return arm: the cancel hook still runs,
                # so a question card is withdrawn even when its slice raised.
                del _deferred[key]
                _cancelled_ids.discard(key)
                _cancel_quietly(entry.deferred, entry.req_id, entry.caller_ctx, entry.tenant_nonce)
                _sel_audit("cancelled", entry.tool_name, entry.req_id, entry.session_key)
            else:
                _settle_deferred(key, entry, exc, error=True)
            return True
        if entry.cancelled:
            # Cancel landed while this step ran: a cancelled request gets no
            # response, whatever the step produced.
            del _deferred[key]
            _cancelled_ids.discard(key)
            _cancel_quietly(entry.deferred, entry.req_id, entry.caller_ctx, entry.tenant_nonce)
            _sel_audit("cancelled", entry.tool_name, entry.req_id, entry.session_key)
            return True
        if outcome is not None:
            _settle_deferred(key, entry, outcome, error=False)
            return True
        return False

    def _service_deferred() -> None:
        """Collect finished steps, answer settled calls, submit due steps.

        Runs on the dispatch thread between stdin reads. A step is submitted
        at most once per entry at a time, so a slow step is never doubled.
        """
        nonlocal _step_pool
        now = time.monotonic()
        for key, entry in list(_deferred.items()):
            try:
                if entry.stepping is not None:
                    _harvest_step(key, entry)
                    continue
                if entry.deferred.due_at() > now:
                    continue
                if _step_pool is None:
                    try:
                        _step_pool = _StepPool(DEFERRED_STEP_WORKERS, f"{server_name}-deferred")
                    except Exception as exc:  # noqa: BLE001 - one call must not end the server
                        # Not one thread for the pool (the scope's task
                        # ceiling): this call fails, the loop and every other
                        # parked call live on -- same contract as a worker that
                        # cannot start in ``_dispatch`` -- and the pool is tried
                        # again on the next due step. Nothing was queued, so
                        # nothing runs later; a call that acted before it
                        # parked answers with its abandon text instead.
                        del _deferred[key]
                        _cancelled_ids.discard(key)
                        _abandon_or_refuse(entry, functools.partial(_refuse_pool_start, entry, exc))
                        continue
                entry.step_queued = now
                entry.step_started = 0.0  # queued; _run_step stamps the start
                entry.stepping = _step_pool.submit(_run_step, entry)
                # A finished step is harvested at once, not at the next tick.
                entry.stepping.add_done_callback(_wake.wake)
            except Exception:  # noqa: BLE001 - one parked call must not end the server
                # Same contract as the per-message guard on the read loop:
                # whatever this entry's bookkeeping raised (a ``due_at`` that
                # errs, a settle whose write tore), the call is answered and
                # the loop keeps serving every other call.
                logger.exception(
                    "%s: servicing parked request %s failed; the loop continues",
                    server_name,
                    entry.req_id,
                )
                if _deferred.pop(key, None) is not None:
                    _cancelled_ids.discard(key)
                    if entry.stepping is not None:
                        entry.stepping.cancel()
                    respond(
                        entry.req_id,
                        None,
                        error={"code": JSONRPC_INTERNAL_ERROR, "message": "Internal error"},
                    )

    def _cancel_deferred(rid: str) -> None:
        """A ``notifications/cancelled`` names a parked call: settle it with no
        response now, or mark it so the step in flight is dropped on return."""
        entry = _deferred.get(rid)
        if entry is None:
            return
        # A step still QUEUED behind busy pool threads is withdrawn here, so
        # it never runs (a spawn's POST would start a child the client already
        # cancelled); one already RUNNING is marked and its text dropped on
        # return, as the pruned-exit path does.
        if entry.stepping is not None and not entry.stepping.cancel():
            entry.cancelled = True
            return
        del _deferred[rid]
        _cancelled_ids.discard(rid)
        _cancel_quietly(entry.deferred, entry.req_id, entry.caller_ctx, entry.tenant_nonce)
        _sel_audit("cancelled", entry.tool_name, entry.req_id, entry.session_key)
        logger.info("cancel received for parked request %s", rid)

    def _queued_request_ids() -> list[Any]:
        """Ids of the tools/call requests still WAITING for the worker (not
        cancelled). The call running on the worker is not among them.

        Named only while the running call is one the client still ages: its
        own slot freezes at about its start, so a hung worker crosses the
        client's ceiling through it and the queue needs no slot of its own.
        A CANCELLED running call has no slot any more (the gateway drops a
        cancelled request from its table), so if the queue kept being vouched
        for, a worker hung on a cancelled call would never be recycled while
        pings are still answered. Then the queued calls age instead."""
        if _current_req_id is not None and str(_current_req_id) in _cancelled_ids:
            return []
        return [
            pc["id"]
            for pc in _pending_calls
            if pc.get("id") is not None and str(pc["id"]) not in _cancelled_ids
        ]

    def _maybe_report_inflight() -> None:
        """Every INFLIGHT_REPORT_SECS, tell a client that asked which calls
        are not to be aged by its wedge detector: parked calls still
        progressing, and calls still queued behind the worker (their wait is
        the queue's, not theirs). The call RUNNING on the worker is never
        named, so the client's ceiling bounds how long one call runs."""
        nonlocal _next_inflight_report
        if not _client_wants_inflight or not (_deferred or _pending_calls):
            return
        now = time.monotonic()
        if now < _next_inflight_report:
            return
        _next_inflight_report = now + INFLIGHT_REPORT_SECS
        request_ids = [e.req_id for e in _deferred.values() if e.progressing(now)]
        request_ids.extend(_queued_request_ids())
        try:
            notify(INFLIGHT_NOTIFICATION, {"requestIds": request_ids})
        except Exception:  # noqa: BLE001 - a missed report costs one refresh, never the server
            logger.warning("%s: in-flight report failed", server_name, exc_info=True)

    def _refuse_parked_on_pruned_exit() -> None:
        """A pruned install is exiting: answer every parked call.

        A step in flight is let FINISH first (bounded by the stuck bound, the
        longest a healthy step takes) and its outcome delivered: a step can
        consume something that must not be retried -- the answer to a
        question card, the children a collect pass marks collected -- and
        telling the caller "retry" while that step completes would lose the
        answer or run the children twice. The worker path has no such window
        (a pruned install is detected before a call runs). A call still
        parked after that gets the same retryable error its queued calls got,
        so none waits on a dead process; a step that returned no text
        consumed nothing, so retrying it is safe -- unless the call did its
        unrepeatable work BEFORE parking, which its ``abandon`` hook says by
        returning the final text it is answered with instead.
        """
        running = [e.stepping for e in _deferred.values() if e.stepping is not None]
        if running:
            concurrent.futures.wait(running, timeout=DEFERRED_STEP_STUCK_SECS)
        for key, entry in list(_deferred.items()):
            if _harvest_step(key, entry):
                continue
            if entry.stepping is not None and not entry.stepping.cancel():
                # ``cancel`` lost: a worker claimed the step after the wait
                # above (one freed up and took it), or it has been running
                # past the bound. Either way it IS running and may consume
                # something a retry would repeat, so it is treated like the
                # running steps above: let it finish, bounded, and deliver.
                concurrent.futures.wait([entry.stepping], timeout=DEFERRED_STEP_STUCK_SECS)
                if _harvest_step(key, entry):
                    continue
            del _deferred[key]
            client_cancelled = entry.cancelled
            if entry.stepping is not None:
                # Cancelled while still QUEUED (no worker ever runs it: this
                # call is about to be told to retry), or RUNNING past the
                # bound twice over -- stuck. A stuck step's result, if one
                # ever comes, has nowhere to go; the cancel hook lets the tool
                # retract what it can (a question card is withdrawn).
                entry.cancelled = True
            if client_cancelled:
                # The client already cancelled this call: it gets no response,
                # only the cancel hook and a ``cancelled`` row, as
                # ``_cancel_deferred`` settles a cancel at once.
                _cancel_quietly(entry.deferred, entry.req_id, entry.caller_ctx, entry.tenant_nonce)
                _sel_audit("cancelled", entry.tool_name, entry.req_id, entry.session_key)
                continue
            # A call that already did something a retry would repeat (the
            # children a spawn POSTed keep running) answers with its own final
            # text instead of "retry", and is audited as the call it settled as.
            _abandon_or_refuse(entry, functools.partial(_refuse_pruned, entry))

    def _refuse_pruned(entry: _DeferredEntry) -> None:
        """Tell a parked call this pruned install is restarting: retryable."""
        _cancel_quietly(entry.deferred, entry.req_id, entry.caller_ctx, entry.tenant_nonce)
        _sel_audit("rejected_install_pruned", entry.tool_name, entry.req_id, entry.session_key)
        respond(
            entry.req_id,
            None,
            error={
                "code": -32000,
                "message": (
                    f"{server_name} is running from an install that an update removed "
                    "and is restarting from the current one; retry the call"
                ),
            },
        )

    # Windows: select() cannot poll stdin, so a pump thread reads it.
    _frames = None if platform_compat.IS_POSIX else _start_stdin_pump(sys.stdin)

    def _next_frame(timeout: float | None) -> dict[str, Any] | _Skipped | _NoFrame | None:
        """The next stdin frame, or :data:`NO_FRAME` when *timeout* seconds
        pass with none (``None`` waits for one). A line already buffered is
        read first: select cannot see it. On POSIX the wait also ends, with
        :data:`NO_FRAME`, the moment the wake pipe fires (a worker or a step
        finished)."""
        if _frames is None:
            if timeout is not None and not _stdin_holds_a_line(sys.stdin):
                wake_fd = _wake.read_fd
                waits: list[Any] = [sys.stdin] if wake_fd is None else [sys.stdin, wake_fd]
                ready = select.select(waits, [], [], timeout)[0]
                if wake_fd is not None and wake_fd in ready:
                    _wake.drain()
                if not any(r is sys.stdin for r in ready):
                    return NO_FRAME
            return _read_message(sys.stdin)
        while True:
            try:
                # A bounded get even when waiting: an untimed lock acquire on
                # Windows cannot be interrupted.
                item = _frames.get(timeout=1.0 if timeout is None else timeout)
            except queue.Empty:
                if timeout is None:
                    continue
                return NO_FRAME
            if isinstance(item, BaseException):
                raise item
            return item

    # Made here, right before the ``try`` that closes it, so no exit path
    # leaks the two descriptors. The worker and step hooks above read it late.
    _wake = _WakePipe()
    try:
        while True:
            worker_busy = _worker_thread is not None and _worker_thread.is_alive()
            if not worker_busy and _worker_thread is not None:
                # Worker just finished: deliver (or register a deferred).
                _deliver_finished_worker(0.1)
            req: dict[str, Any] | _Skipped | _NoFrame | None
            if not worker_busy and _pending_calls:
                # Dispatch a queued tools/call (FIFO) before reading new input.
                req = _pending_calls.popleft()
            elif worker_busy or _deferred:
                # Something is in flight: wait for stdin with a timeout so the
                # loop can also deliver the worker, step parked calls and
                # answer pings. The wake pipe ends the wait the moment the
                # worker or a step finishes; the timeout is then only a
                # backstop, except while parked calls wait for their next
                # ``due_at`` (polled every _POLL_SECS), and on Windows, which
                # has no wake pipe and keeps the _POLL_SECS tick.
                _service_deferred()
                _maybe_report_inflight()
                if worker_busy and _result_ready.is_set():
                    # Deliver a finished result before reading on: an EOF
                    # read next would end the loop with it unsent.
                    req = NO_FRAME
                else:
                    if _deferred or _wake.read_fd is None:
                        timeout = _POLL_SECS
                    else:
                        timeout = _WORKER_WAIT_SECS
                    req = _next_frame(timeout)
                if isinstance(req, _NoFrame):
                    if worker_busy and _result_ready.is_set():
                        _deliver_finished_worker(1.0)
                    continue
                if req is None:
                    # EOF: wait for worker then exit. Parked calls die with the
                    # client that owned them; the ``finally`` below cancels
                    # their queued steps so no worker runs one for a client
                    # that is gone, then shuts the pool down.
                    if _worker_thread:
                        _deliver_finished_worker(5.0)
                    break
                if isinstance(req, _Skipped):
                    # One iteration per dropped frame, so a finished worker's
                    # result is delivered before the next blocking read.
                    continue
            else:
                req = _next_frame(None)
                if req is None:
                    break
                # An untimed read never answers NO_FRAME; the check narrows it.
                if isinstance(req, (_Skipped, _NoFrame)):
                    continue

            envelope = _servable(req)
            if envelope is None:
                continue
            method, req_id, params = envelope
            if worker_busy:
                # Process only cancel notifications while tool is running
                try:
                    if method == "notifications/cancelled":
                        cancelled_rid = params.get("requestId")
                        if cancelled_rid is not None:
                            _remember_cancelled_id(
                                _cancelled_ids,
                                _cancelled_order,
                                str(cancelled_rid),
                                protected=_live_request_ids(),
                            )
                            if str(cancelled_rid) == str(_current_req_id) and _cancel_event:
                                _cancel_event.set()
                                logger.info(
                                    "cancel received for in-flight request %s", cancelled_rid
                                )
                            _cancel_deferred(str(cancelled_rid))
                    # Answer gateway pings even while a tool is in-flight so the
                    # ping-gated wedge detector sees the backend as responsive.
                    elif method == "ping" and req_id is not None:
                        respond(req_id, {})
                    # Buffer tools/call requests that arrive while busy so they get a
                    # response when the worker frees (dropping them left the
                    # client waiting forever). Cancels against queued ids are honored
                    # at dispatch time via _cancelled_ids.
                    elif method == "tools/call" and req_id is not None:
                        if len(_pending_calls) >= PENDING_CALLS_MAX:
                            # Rejection is a tool-invocation decision -- audit it
                            # (security-controls: all invocation decisions emit SEL).
                            _sel_audit(
                                "rejected_busy",
                                params.get("name", ""),
                                req_id,
                                _req_caller_key(params),
                            )
                            respond(
                                req_id,
                                None,
                                error={
                                    "code": -32000,
                                    "message": "Server busy: pending tool-call queue is full; retry",
                                },
                            )
                        else:
                            _pending_calls.append(req)
                    # Any other request with an id gets the answer the idle path
                    # gives it: _dispatch serves tools/list, initialize and the like
                    # inline and answers an unknown method (a client's version
                    # probe such as server/discover) with -32601 at once. Left
                    # unanswered, the client would wait out its own timeout.
                    # Notifications without an id are dropped.
                    elif req_id is not None:
                        _dispatch(method, req_id, params)
                except Exception:  # noqa: BLE001 - one message must not end the server
                    _answer_internal_error(method, req_id)
                continue

            try:
                _dispatch(method, req_id, params)
            except Exception:  # noqa: BLE001 - one message must not end the server
                _answer_internal_error(method, req_id)
            if _pruned_exit:
                _refuse_parked_on_pruned_exit()
                return True
        return False
    finally:
        # Whatever ended the loop -- EOF, a pruned install, an exception that
        # escaped -- no parked call's step may run for a client that will never
        # get the answer: cancel every future still queued (a running one is
        # left to finish; nothing more can be done for it), THEN shut the pool
        # down, since the sentinels queue behind anything still pending.
        for entry in _deferred.values():
            if entry.stepping is not None:
                entry.stepping.cancel()
        if _step_pool is not None:
            _step_pool.shutdown()
        _wake.close()
