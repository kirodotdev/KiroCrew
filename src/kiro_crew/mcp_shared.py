"""Shared helpers for MCP stdio servers (mcp_core, mcp_cron)."""

from __future__ import annotations

import collections
import concurrent.futures
import contextlib
import functools
import json
import logging
import os
import queue
import select
import signal
import sys
import threading
import time
import urllib.request
from typing import Any, Callable, Literal, NamedTuple, Optional, overload

from kiro_crew import platform_compat
from kiro_crew.acp.types import JSONRPC_METHOD_NOT_FOUND
from kiro_crew.config.loader import KiroCrewConfig, config_dir, read_local_secret  # noqa: F401
from kiro_crew.dashboard.origin import parse_dashboard_url  # noqa: F401
from kiro_crew.install_liveness import (
    INSTALL_PRUNED_EXIT_CODE,
    install_pruned,
    respawned_by_pool,
)
from kiro_crew.json_line import (
    ID_PROBE_BYTES,
    parse_json_object_line,
    recover_line_id,
    recover_top_level_id,
)
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.mcp_caller import (
    INFLIGHT_CAPABILITY_KEY,
    INFLIGHT_NOTIFICATION,
    CallerContext,
    caller_identity_capability,
    current_caller,
    resolve_own_identity,
    set_current_caller,
    set_current_tenant_nonce,
    tenant_nonce_from_meta,
)
from kiro_crew.mcp_cleanup import STALE_MANAGED_MCP_SERVERS
from kiro_crew.port_resolution import resolve_client_port_src
from kiro_crew.sel import sel
from kiro_crew.session_directive import neutralize_markers
from kiro_crew.session_token_sig import session_key_from_env_token
from kiro_crew.validation import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_PARSE_ERROR,
    JsonRpcEnvelopeError,
    ValidationError,
    build_tool_response,
    validate_jsonrpc_request,
    validate_jsonrpc_response,
)

logger = logging.getLogger(__name__)

# The component name this PROCESS presents on loopback gateway requests as
# ``X-Internal-Caller``. Set exactly once by ``run_mcp_stdio_loop`` from the
# server name it is handed, BEFORE any request is served — so every MCP stdio
# server (kirocrew-core, kirocrew-dashboard, kirocrew-cron, kirocrew-computer)
# self-identifies without per-server wiring, and a future server gets it for
# free. ``None`` outside an MCP server process (CLI, tests), in which case the
# request helpers send no caller header at all rather than inventing an
# identity. The header is ATTRIBUTION for the gateway's audit log (SEL
# ``source`` — see ``chat_folders._audit_origin``), never authorization: the
# ``X-Internal-Secret`` handshake alone authenticates the request.
_internal_caller_name: str | None = None


def set_internal_caller(name: str | None) -> None:
    """Declare this process's component identity for internal HTTP requests.

    ``None`` un-declares it — used by ``run_mcp_stdio_loop``'s teardown to
    restore the prior value, so repeated loops in one process (the test
    suite) cannot leak one server's identity into the next test's requests.
    """
    global _internal_caller_name
    _internal_caller_name = name


def internal_caller() -> str | None:
    """The declared component identity for internal HTTP requests, if any."""
    return _internal_caller_name


# Max tools/call requests buffered while a tool worker is busy.
# Overflow gets an immediate JSON-RPC busy error instead of silence.
PENDING_CALLS_MAX = 32

# One gateway pool shares one backend: bound what the pending queue drains into.
# Each parked entry holds a dict slot and at most one pool step; twice the queue
# bound leaves generous room for waiting calls without unbounded retention.
DEFERRED_PARKED_MAX = 64

# Max cancelled-request ids retained. ``notifications/cancelled`` can arrive
# for any request id over the life of the (long-lived, per-session) MCP server
# process; retaining them in an unbounded set leaks one entry per cancel. Once
# this cap is reached the oldest ids are evicted FIFO.
CANCELLED_IDS_MAX = 1024

#: Largest Content-Length body :func:`_read_message` reads whole: the gateway's
#: default per-line read limit (``mcp_gateway.pool._DEFAULT_READ_BUFFER_LIMIT``,
#: pinned equal by a test so this server need not import the gateway). A larger
#: body is drained in bounded chunks and answered as too large, so a corrupt
#: header can neither allocate it nor desync the framing.
MAX_CONTENT_LENGTH_BYTES = 64 * 1024 * 1024
#: A declared length past this is no body any sender drains to: the header is
#: corrupt, and draining to it would swallow every later request.
_MAX_DRAINABLE_LENGTH_BYTES = 16 * MAX_CONTENT_LENGTH_BYTES
_DRAIN_CHUNK_BYTES = 1024 * 1024


def _evict_oldest_evictable(
    cancelled_ids: set[str],
    order: "collections.deque[str]",
    protected: set[str],
) -> bool:
    """Discard the oldest cancellation id that is NOT protected.

    ``protected`` holds the ids of the currently active and still-queued
    requests. Evicting one of those would drop a cancellation flag before the
    dispatch loop consumes it, letting a cancelled queued call execute -- so
    protected ids are rotated to the back of ``order`` and kept. Returns True
    if a (non-protected or stale) id was evicted, False if only protected ids
    remain (the caller then tolerates a bounded overflow -- ``protected`` is
    bounded by the pending-queue cap + 1, far below ``CANCELLED_IDS_MAX``).
    """
    for _ in range(len(order)):
        oldest = order.popleft()
        if oldest in protected and oldest in cancelled_ids:
            # Live request -- keep its cancellation flag; move to the back.
            order.append(oldest)
            continue
        # Non-protected id, or a stale deque entry already gone from the set
        # (``discard`` is a no-op for absent ids).
        cancelled_ids.discard(oldest)
        return True
    return False


def _remember_cancelled_id(
    cancelled_ids: set[str],
    order: "collections.deque[str]",
    rid: str,
    cap: int = CANCELLED_IDS_MAX,
    protected: Optional[set[str]] = None,
) -> None:
    """Record a cancelled request id, bounding memory growth FIFO.

    ``cancelled_ids`` holds membership (source of truth); ``order`` tracks
    insertion order for eviction. When the number of live ids exceeds ``cap``,
    the oldest *evictable* ids are dropped until back at the cap. Ids listed in
    ``protected`` (the active + still-queued requests) are never evicted, so a
    flood of unrelated cancels can never drop a live request's cancellation
    flag and let a cancelled queued call execute (HIGH). Idempotent: a repeated
    id is not re-appended. Kept module-level (not a closure) so the bound is
    unit-testable.
    """
    if rid in cancelled_ids:
        return
    cancelled_ids.add(rid)
    order.append(rid)
    protected = protected or set()
    # Two independent bounds keep the pair from leaking:
    #   1. Set bound -- evict oldest evictable ids once membership exceeds cap.
    #   2. Deque bound -- completion sites discard consumed ids from the *set*
    #      only (the deque is untouched), so without this second guard the
    #      deque would accumulate one stale entry per cancel and grow without
    #      limit even while the set stays small. Popping stale entries (already
    #      discarded from the set) is harmless.
    # Eviction skips protected ids (rotating them to the back); if only
    # protected ids remain, ``_evict_oldest_evictable`` returns False and we
    # stop -- a bounded overflow is preferable to executing a cancelled call.
    while len(cancelled_ids) > cap and order:
        if not _evict_oldest_evictable(cancelled_ids, order, protected):
            break
    while len(order) > cap:
        if not _evict_oldest_evictable(cancelled_ids, order, protected):
            break


class DeferredTool:
    """A tool call whose remaining work is a sequence of SHORT steps.

    The dispatch loop runs one worker thread per server, so a tool that spends
    its whole duration asleep (``wait`` sleeps up to 1800s; ``spawn_sub_agents``
    polls for up to 7200s) parks that worker and every other caller's call
    behind it -- on the pooled ``kirocrew-core`` backend that is every dashboard
    session at once. A handler that only has to WAIT returns one of these from
    the worker instead of a result string: the worker frees the moment the
    handler returns, and the loop runs :meth:`step` from its own timer whenever
    :meth:`due_at` has passed. The call is settled when a step returns text.

    Contract for a ``step``: it is short (an HTTP round-trip to the loopback
    gateway, never a sleep), it runs with the call's caller identity installed
    exactly as the worker had it, and it may raise :class:`ToolCancelled` to end
    the call with no response. Steps run on a small shared pool, not on the
    dispatch thread, so a step that blocks on a stalled gateway delays other
    steps, never the loop's ``ping`` answers or the single worker.

    Outside the POSIX dispatch loop -- the synchronous Windows path, a direct
    ``_call_tool`` from a test or the CLI -- :func:`drive_deferred` runs the
    steps inline, so a deferred tool behaves exactly like a blocking one there.
    """

    def due_at(self) -> float:
        """``time.monotonic()`` value at or after which the next step may run."""
        raise NotImplementedError

    def step(self) -> str | None:
        """Do one bounded unit of work; return the result text when done."""
        raise NotImplementedError

    def cancel(self) -> None:
        """Best-effort hook when the call is cancelled before it settled."""
        return None

    def abandon(self) -> str | None:
        """Called instead of :meth:`cancel` when the loop must answer this
        call without ever stepping it again: the parked table is full, the
        step pool cannot start, or a pruned install is exiting.

        Return final response text when the call has already done something a
        retry would repeat; ``None`` (the default) means the caller may safely
        retry and is told to.
        """
        return None

    def on_settled(self, text: str) -> None:
        """Hook run by whoever drives the steps, once, with the text that
        settled the call, AFTER it has decided the call was not cancelled.

        Side effects that must happen exactly once per call and only for a
        call that is answered -- the SEL ``completed``/``failed`` row -- live
        here rather than in the settling ``step()``: a step runs on a pool
        thread and can return text while a cancel is landing on the dispatch
        thread, which then drops the text and audits ``cancelled``. Only the
        driver sees both, so only the driver may commit.
        """
        return None


def drive_deferred(deferred: DeferredTool, *, clock: Any = time) -> str:
    """Run ``deferred`` to completion inline, sleeping between steps.

    ``clock`` is a module-like object with ``monotonic`` and ``sleep``, so a
    caller can hand in the one its tests patch (``mcp_core.time``). Nothing
    can cancel an inline drive, so the settling text is committed at once.
    """
    while True:
        result = deferred.step()
        if result is not None:
            deferred.on_settled(result)
            return result
        delay = deferred.due_at() - clock.monotonic()
        if delay > 0:
            clock.sleep(delay)


def map_deferred(deferred: DeferredTool, fn: Callable[[str], str]) -> DeferredTool:
    """``deferred`` with ``fn`` applied to the text that settles it -- the
    deferred form of ``fn(handler(...))`` for a wrapper that post-processes a
    blocking tool's result."""
    return _MappedDeferred(deferred, fn)


class _MappedDeferred(DeferredTool):
    def __init__(self, inner: DeferredTool, fn: Callable[[str], str]) -> None:
        self._inner = inner
        self._fn = fn

    def due_at(self) -> float:
        return self._inner.due_at()

    def step(self) -> str | None:
        result = self._inner.step()
        return None if result is None else self._fn(result)

    def cancel(self) -> None:
        self._inner.cancel()

    def abandon(self) -> str | None:
        text = self._inner.abandon()
        return None if text is None else self._fn(text)

    def on_settled(self, text: str) -> None:
        self._inner.on_settled(text)


#: How often the loop reports in-flight deferred ids while any exist
#: (``INFLIGHT_NOTIFICATION``, sent only to a client that declared
#: ``INFLIGHT_CAPABILITY_KEY``; both live in ``mcp_caller``). Well
#: under the gateway's ``HEARTBEAT_TIMEOUT_SECS`` (300s) so a report is never
#: late enough for a healthy call to cross the slow-request threshold.
INFLIGHT_REPORT_SECS = 30.0

#: A deferred call whose current step has run longer than this is NOT reported
#: as progressing: a step is an HTTP round-trip, so one that holds a pool
#: thread for a minute is the stall the report exists to expose.
DEFERRED_STEP_STUCK_SECS = 60.0

#: Threads the loop runs deferred steps on. A ``wait`` step is a millisecond
#: keepalive and a ``spawn_sub_agents`` step one poll pass, but an
#: ``ask_question`` step holds the server's fixed ``ASK_WAIT_SLICE_SECS``
#: long-poll and a parent's resume probe is held server-side up to 8s, so the pool is sized
#: for a few of those in flight at once without starving the quick steps. The
#: bound exists so a gateway that stops answering cannot grow threads without limit.
DEFERRED_STEP_WORKERS = 8


class _StepPool:
    """A fixed set of worker threads, all created UP FRONT, fed by one queue.

    Not ``concurrent.futures.ThreadPoolExecutor``: that starts its threads
    lazily inside ``submit``, AFTER the work item is queued, so a thread start
    that fails under the process's thread ceiling raises out of ``submit`` while
    the item stays queued and an idle worker runs it later -- for a deferred
    step, a call just answered with an error could then consume an answer or a
    collect nobody receives. Here ``submit`` only enqueues and cannot fail; the
    one place a thread start can fail is construction, where nothing is queued
    yet, so the caller fails that single call and retries the pool next tick.
    A PARTIALLY started pool is not kept: ``DEFERRED_PARKED_MAX`` admits calls
    on the assumption of ``workers`` threads, and one or two threads behind a
    full table queue an ask's poll past the coordinator's answer grace, so an
    answer would be lost. The started threads are released and construction
    raises, which is the refusal path the caller already has.
    """

    def __init__(self, workers: int, name: str) -> None:
        self._queue: "queue.SimpleQueue[tuple[concurrent.futures.Future[Any], Callable[..., Any], tuple[Any, ...]] | None]" = (queue.SimpleQueue())
        self._threads: list[threading.Thread] = []
        for i in range(workers):
            thread = threading.Thread(target=self._run, name=f"{name}-{i}", daemon=True)
            try:
                thread.start()
            except RuntimeError:
                self.shutdown()
                raise
            self._threads.append(thread)

    @property
    def workers(self) -> int:
        return len(self._threads)

    def submit(self, fn: Callable[..., Any], *args: Any) -> "concurrent.futures.Future[Any]":
        """Queue ``fn(*args)``; never starts a thread, so it never raises."""
        fut: "concurrent.futures.Future[Any]" = concurrent.futures.Future()
        self._queue.put((fut, fn, args))
        return fut

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fut, fn, args = item
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args))
            except BaseException as exc:  # noqa: BLE001 - delivered through the future
                fut.set_exception(exc)

    def shutdown(self) -> None:
        """Let every worker exit once the queue ahead of the sentinel drains."""
        for _ in self._threads:
            self._queue.put(None)


class _DeferredEntry:
    """Dispatch-loop bookkeeping for one parked call (see :class:`DeferredTool`).

    ``stepping`` is the pool future of the step in flight, or ``None`` between
    steps; ``step_queued`` is when it was submitted and ``step_started`` when
    it began RUNNING on its pool thread (``0.0`` while still queued behind
    other steps). ``progressing`` bounds each phase separately, so a short
    queue wait never reads as a stuck step, while a step that never gets a
    thread stops being vouched for. ``cancelled`` marks a call whose cancel
    arrived mid-step: its step's result is dropped when the step returns, since
    a cancelled request gets no response.
    """

    __slots__ = (
        "req_id",
        "tool_name",
        "caller_ctx",
        "tenant_nonce",
        "deferred",
        "stepping",
        "step_queued",
        "step_started",
        "cancelled",
    )

    def __init__(
        self,
        req_id: Any,
        tool_name: str,
        caller_ctx: "CallerContext | None",
        tenant_nonce: str,
        deferred: DeferredTool,
    ) -> None:
        self.req_id = req_id
        self.tool_name = tool_name
        self.caller_ctx = caller_ctx
        self.tenant_nonce = tenant_nonce
        self.deferred = deferred
        self.stepping: Optional[concurrent.futures.Future[str | None]] = None
        self.step_queued = 0.0
        self.step_started = 0.0
        self.cancelled = False

    @property
    def session_key(self) -> str:
        return self.caller_ctx.session_key if self.caller_ctx else ""

    def progressing(self, now: float) -> bool:
        """True unless the step in flight has exceeded the stuck bound in its
        current phase: queued for a pool thread longer than the bound (every
        worker is itself stuck, so this call is not moving either), or holding
        its thread longer than the bound. Between steps a call is progressing.
        """
        if self.stepping is None:
            return True
        since = self.step_started if self.step_started else self.step_queued
        return (now - since) < DEFERRED_STEP_STUCK_SECS


def _declares_inflight_capability(init_params: dict[str, Any]) -> bool:
    """Whether an ``initialize`` request's client capabilities carry
    :data:`INFLIGHT_CAPABILITY_KEY` under ``experimental``."""
    caps = init_params.get("capabilities")
    if not isinstance(caps, dict):
        return False
    experimental = caps.get("experimental")
    return isinstance(experimental, dict) and INFLIGHT_CAPABILITY_KEY in experimental


class ToolCancelled(Exception):
    """Raised by a tool (or a deferred step) that gives up because its call was
    cancelled; the loop answers nothing and audits the call as cancelled."""

    pass


# The framing the client's first accepted message used, and so the framing of
# every response. ``None`` until then. On a bare stream a ``Content-Length:``
# line is noise: honouring it would read the requests behind it as a body.
_framing: Literal["bare", "content-length"] | None = None


class _Skipped:
    """What :func:`_read_message` returns for one frame it dropped. Not EOF."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "SKIP"


#: One dropped frame. Distinct from ``None`` (EOF) so a ``null`` line cannot end
#: the loop, and returned per frame so a busy loop gets back to delivering a
#: finished tool's result instead of blocking on the next line.
SKIP = _Skipped()

# ── Private stdout descriptor for JSON-RPC responses ───────────────────────
# The vendored llama-cpp runtime wraps its multi-second GGUF model load in
# ``suppress_stdout_stderr``, which does a PROCESS-WIDE ``dup2(devnull, 1)``
# for the duration of the load (``_vendor/llama_cpp/_utils.py``). The first
# ``local_knowledge_search`` kicks that load on a background thread and returns
# a keyword-only result in milliseconds, so the JSON-RPC response for that very
# call races the load window. Written through fd 1, the bytes land in
# /dev/null: no exception, no short write, the SEL audit still records
# ``success`` -- and the client waits forever until the ACP tool-stall watchdog
# (``acp/client.py::_TOOL_STALL_TIMEOUT``, 600s) kills the turn.
#
# ``snapshot_stdout_fd()`` takes an ``os.dup(1)`` at server startup, BEFORE any
# tool can run. A dup'd descriptor keeps pointing at the original pipe no
# matter what a later ``dup2`` does to fd 1, so responses always reach the
# client. Guarded by a lock because it is a raw unbuffered fd: ``os.write`` is
# not atomic across interleaved callers, and a torn frame desyncs the stream
# for every subsequent message.
#
# NOTE: the suppressor also rebinds the ``sys.stdout`` OBJECT to a devnull
# file, so "has sys.stdout been swapped?" is NOT a usable liveness check -- it
# is false exactly inside the window we must survive. The snapshot is the only
# reliable route.
_stdout_fd: Optional[int] = None
_stdout_fd_lock = threading.Lock()


def snapshot_stdout_fd() -> Optional[int]:
    """Capture a private dup of the real stdout descriptor. Idempotent.

    Called once at ``run_mcp_stdio_loop`` entry. Returns the dup'd fd, or
    ``None`` when stdout is not fd-backed (pytest's captured stdout, an
    embedded host handing us a StringIO) -- in that case ``respond()`` falls
    back to ``sys.stdout`` exactly as before.
    """
    global _stdout_fd
    with _stdout_fd_lock:
        if _stdout_fd is not None:
            return _stdout_fd
        try:
            _stdout_fd = os.dup(sys.stdout.fileno())
        except (AttributeError, OSError, ValueError):
            # No usable fileno (StringIO / captured / closed stdout).
            _stdout_fd = None
        return _stdout_fd


def release_stdout_fd() -> None:
    """Close the private stdout dup, if one was captured. Idempotent.

    Keeps the descriptor from leaking when a loop is run repeatedly in one
    process (the test suite drives ``run_mcp_stdio_loop`` many times); a real
    server process exits after its single loop returns.
    """
    global _stdout_fd
    with _stdout_fd_lock:
        fd = _stdout_fd
        _stdout_fd = None
    if fd is not None:
        with contextlib.suppress(OSError):
            os.close(fd)


def _write_all(fd: int, payload: bytes) -> int:
    """Write every byte of ``payload`` to ``fd``; return the count written.

    ``os.write`` on a pipe may accept fewer bytes than offered; a silently
    truncated frame desyncs the JSON-RPC stream for every later message, so
    loop until the payload is fully handed over. Mirrors the short-read loop
    in :func:`_read_message`.

    On failure the ``OSError`` propagates, but the bytes written so far are
    attached as ``bytes_written`` so the caller can tell a clean failure (zero
    bytes — safe to retry on another stream) from a partial one (retrying would
    duplicate the prefix and tear the frame).
    """
    view = memoryview(payload)
    written = 0
    while view:
        try:
            n = os.write(fd, view)
        except OSError as exc:
            exc.bytes_written = written  # type: ignore[attr-defined]
            raise
        view = view[n:]
        written += n
    return written


# ── Managed tool policy cache ──────────────────────────────────────────────
# Keyed per RESOLVED SESSION: in the pooled topology one backend process serves many
# sessions (per-call identity via the caller-meta extension), so a single
# process-global set would apply the FIRST session's policy — or a cached
# fail-open — to every other session.
#
# RESOLVED is the load-bearing word, and it is not the same as "the identity the
# gateway supplied". Keyed on the latter, every caller the gateway could not name
# shared ONE entry under the empty string, which put back the collapse the per-session
# keying exists to prevent: two sessions on one process (``spawn_run`` session sharing)
# inherited each other's tool policy, and a warm-pool session kept the policy of the
# session that held the process before its rekey for the life of the process — this
# cache has no TTL, so nothing expired it. The key is now the session the request was
# actually MADE for, which is the same value that rides its ``X-Session-Key``: one
# entry per session, and a rekeyed or subagent caller misses rather than inheriting.
# BOUNDED: a long-lived pooled backend serves churning sessions;
# FIFO-evict the oldest entry past the cap so the dict cannot grow without
# limit. Eviction only costs a re-fetch on that session's next call.
_EXCLUDED_TOOLS_CACHE_MAX = 256
_excluded_tools_by_session: dict[str, set[str]] = {}
# Two separate negative caches with different TTLs because the two conditions get
# OPPOSITE answers at ``tools/call``: the long HTTP-error window refuses, the short
# startup-race window stays permissive, so a brief race must never be answered out
# of the long window.
_last_failure_time: float = 0.0  # gateway unreachable / non-404 HTTP error
_last_startup_race_time: float = 0.0  # no session key or 404 — recovers fast
# The identity the short window was opened FOR: ``""`` when no session key could be
# resolved, or the resolved key whose policy request the gateway answered 404. The
# window debounces a race that belongs to ONE identity, and it is process-global,
# so without this a window opened by an unidentified ``tools/list`` would answer
# ``no_session_key`` for a ``tools/call`` that resolved a real key seconds later —
# and ``tools/call`` does not refuse on that reason, so an operator exclusion would
# go unenforced for the rest of the window. The token makes that transition
# ordinary: a mapping is published mid-window on every warm-pool claim.
_last_startup_race_key: str = ""
_failure_count: int = 0
# Class name of the exception behind the last ``resolution_failed``. A negative
# cache hit hands it back as the refusal's cause, so the agent and the audit log
# still name WHY while the long window is answering instead of the gateway.
_last_failure_class: str = ""
# Cap on the scrubbed exception message the audit event carries.
_FAILURE_MESSAGE_MAX = 200
# Long TTL applies only when the gateway is genuinely unreachable
# (HTTP errors other than 404, connection refused, timeout).  Kept short
# (60s) because this window is how long ``tools/call`` keeps REFUSING for a
# session that has never resolved its policy: a longer one holds the refusal
# well past the gateway's recovery, for the non-kiro-cli MCP hosts (Claude
# Code, custom hosts) this layer is the only enforcement point for.  60s is
# enough to debounce the 5s urlopen storm during a transient gateway outage
# without outliving it.
_NEGATIVE_CACHE_TTL: float = 60.0  # seconds
# Short TTL for the benign startup-race cases (no session key resolvable,
# or 404 "agent not resolved" because gateway hasn't registered the
# session yet).  Long enough to debounce the warning storm during a
# parallel MCP startup, short enough that we recover to deny-enforcing
# behavior within seconds once the session is registered.  This addresses
# the security-controls concern: don't keep fail-open active for
# 5 minutes when the underlying race resolves in milliseconds.
_STARTUP_RACE_CACHE_TTL: float = 5.0  # seconds
# After this many consecutive failures, suppress the warning log entirely
# (still emit a structured audit event).  The warnings are noise once the
# 404 root cause is established for the session.
_MAX_WARNING_FAILURES: int = 2
# The most of the gateway's 409 ``reason`` a refusal repeats to the caller. A
# reason names one file and one remedy -- a few hundred characters -- so the cap
# is generous for a real one and small enough that a pathological filename in
# the agents directory cannot turn one refused call into a kilobyte of echo.
_POLICY_DETAIL_MAX_CHARS: int = 600


class ToolPolicy(NamedTuple):
    """The exclusion set for one session, plus whether it was actually read.

    ``excluded`` empty is ambiguous on its own: it means BOTH "the operator
    excluded nothing" and "we never got to look".  Those two demand opposite
    behaviour at a call site, so the reason we failed to look travels with the
    value instead of being flattened into an empty set.

    ``unresolved`` is the audit operation name of the path that gave up
    (``no_session_key``, ``agent_not_resolved``, ``policy_forbidden``,
    ``policy_unreadable``, ``resolution_failed``, or the cached form of one of
    them) and is ``""`` only when the gateway's policy endpoint actually
    answered with a policy this code understood.  Which reason it is decides
    what ``tools/call`` does -- see ``_UNRESOLVED_REFUSES_CALL`` -- so a new
    failure path must name its own reason rather than borrow one: borrowing
    inherits a decision that was made about a different condition, and every
    reason here that was ever collapsed into another one hid a different bug.

    ``detail`` is the gateway's own account of an unresolved reason, when it
    gave one -- the ``reason`` field of a ``409 policy_unreadable`` body, which
    names the spec file it could not read and what to do about it. Text only,
    never a decision: nothing reads it but the refusal message, so a caller
    that ignores it behaves exactly as before it existed. For
    ``resolution_failed`` it is the class name of the exception that stopped the
    read (``ConnectionRefusedError``), so the agent sees which failure it hit;
    the scrubbed message goes only to the audit event. Empty on every other path.
    """

    excluded: frozenset[str]
    unresolved: str
    detail: str = ""


def _ambient_audit_session() -> str:
    """The session an AUDIT record names when the call carried no gateway caller.

    Attribution only, never authorization: this feeds ``session_key=`` on SEL
    records for calls the gateway did not stamp. It reads the same source the
    policy lookup resolves by — the signed per-session token — before the env var,
    for the same reason: after a warm-pool rekey the env names the previous
    session, and a subagent sharing its parent's process carries its own token
    where the env names the parent. ``"mcp"`` is the pre-existing placeholder for
    a process with neither. Never raises; an audit fallback that could raise would
    turn a loggable call into a failed one.
    """
    try:
        from_token = session_key_from_env_token()
    except Exception:
        from_token = ""
    return from_token or os.environ.get("KIROCREW_SESSION_KEY", "mcp")


def spawned_without_gateway_identity() -> bool:
    """True when nothing in this process's environment says a gateway started it.

    A server the gateway starts for a session carries the signed per-session
    token on its MCP element (``KIROCREW_STUB_SESSION_TOKEN``), and a sandboxed
    one additionally carries the launcher's ``KIROCREW_HOST_PID``. A process with
    neither was started by something else -- an editor's own MCP config, a shell
    -- and has no channel through which it could ever prove a session identity.
    ``KIROCREW_SESSION_KEY`` is deliberately NOT consulted here: it is exactly the
    variable a person copies into an editor config by hand, so its presence says
    nothing about who spawned the process.

    Read-only and env-only, so the answer is the same before and after a refusal
    and never depends on the gateway being reachable.
    """
    # Deferred like session_token_sig's read of the same name: ``claim`` pulls in
    # the executor and transport graph, and this runs inside every stdio server,
    # which must not pay for it at import time.
    from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

    if os.environ.get(STUB_SESSION_TOKEN_ENV, ""):
        return False
    if os.environ.get("KIROCREW_HOST_PID", "").isdigit():
        return False
    return True


def external_client_identity_note(server: str = "kirocrew-core") -> str:
    """The one explanation every identity refusal appends for an unspawned server.

    The same missing identity surfaces at three sites -- the tool-policy gate's
    ``identity_unattested`` refusal, the strict-identity diagnosis behind
    ``memory_recall`` and its siblings, and ``learn_add``'s pass-through of the
    gateway's ``missing X-Session-Key`` -- and a reader who started ``kirocrew-core``
    from an editor's MCP config sees a different sentence at each, none of which
    says the one thing they need: that server has no identity channel by design,
    and the env var they are tempted to set is not a credential. This text says
    it once, and every site appends the SAME string, so the three refusals agree.
    Refusal decisions are untouched: this decorates a denial, it never grants.

    Callers gate it on :func:`spawned_without_gateway_identity`, so a server the
    gateway did start (token on the element, or a launcher host pid) keeps its
    existing wording -- its problem is a trust root or a spawn denial, and this
    note would point it away from both.

    Two clauses are per-server. The read-only tools named are kirocrew-core's,
    so they appear only for it. The "leftover from an older install" clause
    holds only for the names Kiro Crew itself once wrote into the shared config
    (``mcp_cleanup.STALE_MANAGED_MCP_SERVERS``); an opt-in server found there
    is the user's own wiring, and calling it residue would invite a deletion
    ``clean_stale_managed_mcp`` itself refuses to make.
    """
    if server == "kirocrew-core":
        read_only = (
            " Remove it and the read-only tools (learn_list, local_knowledge_search) "
            "work without a session; use a Kiro Crew session (dashboard or a "
            "messaging channel) for the rest."
        )
    else:
        read_only = (
            " Remove it; use a Kiro Crew session (dashboard or a messaging channel) "
            "for the tools that need one."
        )
    if server in STALE_MANAGED_MCP_SERVERS:
        entry = (
            f", and a {server} entry in ~/.kiro/settings/mcp.json is leftover from an "
            f"older install (docs/architecture/mcp.md)."
        )
    else:
        entry = " (docs/architecture/mcp.md)."
    return (
        f" If this {server} was not started by a Kiro Crew session -- for example "
        f"it is listed in an editor's own MCP config such as ~/.kiro/settings/mcp.json "
        f"-- the tools that need a session identity (memory writes, memory recall, "
        f"session control) are not supported from it: only a server the gateway "
        f"starts for a session receives the signed session token that proves which "
        f"session is calling, and there is no user-settable substitute. "
        f"KIROCREW_SESSION_KEY is a fallback the gateway injects into its own "
        f"processes, not a credential: setting it by hand identifies nothing and makes "
        f"the gateway refuse every tool call from this server (identity_unattested)."
        f"{read_only} The supported direction for an editor is to connect INTO a "
        f"Kiro Crew session, not to spawn {server} itself{entry}"
    )


def _policy_session_key() -> str | None:
    """Resolve the session whose tool policy is being asked for, absent a gateway caller.

    Three outcomes, and they are deliberately distinct because the caller owes each
    one a different answer — the same discipline :class:`ToolPolicy` applies to the
    policy itself, one level down:

    * a session key — ask the gateway for that session's policy, and cache under it;
    * ``""`` — no identity on this install YET (a startup race), or an identity that
      was explicitly REFUSED (a protected member record that exists and is
      invalid). That is the ``no_session_key`` reason, which ``tools/call``
      deliberately PROCEEDS on, so nothing that must deny a call may land here;
    * ``None`` — the question has no answer this process can act on: resolution
      itself broke (an unreadable home, a raising probe), a pid names SEVERAL
      sessions so none of them is singled out, or the ancestor walk ended having
      passed a mapping it could not use. That is the ``resolution_failed`` class,
      kept distinguishable so a broken host is not reported as a benign race and
      does not inherit the race's short window.
      A raising ``protected_member_session_for_pid`` stays in THIS class: the probe
      is not caught here, because the same-uid fence means an unreadable binding
      must never be re-read as identity from token/env. A host override that can
      tell an expected sandbox deny from an induced one returns ``None`` for it.

    The ladder is :func:`kiro_crew.mcp_caller.resolve_own_identity`, shared with
    every other client-side resolver so the rungs and their order cannot drift
    between them. The gateway's per-call identity is NOT part of it: that is exact
    and stamped per CALL, so :func:`_resolve_tool_policy` uses it directly and
    never calls this.

    On an install with no token and no protected binding the ladder resolves
    through the env var and then the pid mapping, and costs exactly that. It
    stops short of naming a co-tenant on a shared runtime: a misresolved key
    applies one session's operator tool exclusions to another, so this lookup
    refuses there rather than guessing.
    """
    identity = resolve_own_identity()
    return None if identity.failed else identity.session_key


def _http_error_body(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """The ``code`` and ``reason`` fields of a JSON error body, ``""`` for each absent one.

    The gateway's refusals carry ``{"error": ..., "code": "<reason>", "reason":
    "<what it could not read>"}``; the status alone is not enough to tell two
    of its 409s apart, and ``reason`` is the one line that names the spec file
    an operator has to fix. Never raises: an unreadable or non-JSON body is
    ``("", "")``, and the caller treats that as the status's historical
    meaning rather than guessing a narrower one. Only string fields are
    returned -- the body is wire data, and a ``reason`` of any other type is
    dropped, not coerced.
    """
    try:
        raw = exc.read()
    except Exception:
        return "", ""
    if not raw:
        return "", ""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    code = payload.get("code")
    reason = payload.get("reason")
    return (
        code if isinstance(code, str) else "",
        reason if isinstance(reason, str) else "",
    )


def _resolve_tool_policy(
    caller_session: str = "",
    *,
    caller_token: str = "",
    ignore_negative_cache: bool = False,
) -> ToolPolicy:
    """Query the gateway for the current session's managedToolPolicy.exclude.

    ``caller_session`` is the verified per-call identity from the gateway's
    caller-meta extension (pooled topology); when non-empty it takes
    precedence over every source in :func:`_policy_session_key`, so
    sessions sharing one backend cannot inherit each other's policy. The
    RESOLVED session keys the cache either way — the policy returned and the policy
    stored are then the same session's, which is what stops a caller the gateway
    could not name from inheriting a co-tenant's or its own pre-rekey policy.

    ``caller_token`` is that same caller block's ``sessionToken`` -- the signed
    per-session token gatewayd hands a pooled control-plane backend PER FRAME,
    because the backend was spawned from the daemon's environment and has no
    token of its own there. It is passed explicitly rather than read from
    :func:`current_caller` because the ``tools/call`` dispatch evaluates the
    policy BEFORE it installs the caller ContextVar (the check runs on the read
    loop; ``set_current_caller`` runs in the worker), so at this point the
    ContextVar is still empty. The gateway accepts a declared ``X-Session-Key``
    only behind an attestation, and a loopback TCP request has none but this
    header; without it a pooled backend's every read is answered
    ``member_identity_unavailable`` and every tool call is refused. The
    ContextVar remains the fallback for a caller that reaches this resolver
    with the caller already installed.

    Returns the set of tool names to hide from this session, together with the
    reason the policy could not be read when it could not. Caches on success
    only, so a session that has ever resolved its policy is served from
    ``_excluded_tools_by_session`` and never reaches a failure path again
    (until FIFO eviction) -- the unresolved states below are reachable only for
    a session whose policy has never been read once.

    On failure this returns an EMPTY exclusion set with ``unresolved`` set, and
    the two consumers read that differently on purpose:

    - ``tools/list`` shows the unfiltered list. Hiding every tool here would be
      unrecoverable: kiro-cli calls ``tools/list`` once per session and caches
      the answer, so an empty list persists for the session's whole life. A
      listing is not an enforcement point.
    - ``tools/call`` REFUSES, loudly and attributably. This is the only place a
      withheld deny can actually be exercised, and refusing one call costs a
      retry rather than a session.

    An empty exclusion set therefore never stands alone as a permission: the
    audit event that records the failure is also what makes the fail-closed
    choice available without guessing which tools the operator meant to deny.
    """
    global _last_failure_time, _last_startup_race_time, _last_startup_race_key
    global _failure_count, _last_failure_class
    # Resolve BEFORE the cache is consulted when the gateway did not name the caller:
    # the entry has to be found under the session this call is for, and only
    # resolution knows which that is. A named caller needs no resolution at all — its
    # key IS the cache key — so the pooled hot path keeps its single dict lookup.
    if caller_session:
        session_key: str | None = caller_session
    else:
        session_key = _policy_session_key()
    if session_key:
        _cached = _excluded_tools_by_session.get(session_key)
        if _cached is not None:
            return ToolPolicy(frozenset(_cached), "")

    now = time.monotonic()
    _failure_cached = bool(_last_failure_time and (now - _last_failure_time) < _NEGATIVE_CACHE_TTL)
    _race_cached = bool(
        not caller_session
        and _last_startup_race_time
        and (now - _last_startup_race_time) < _STARTUP_RACE_CACHE_TTL
        and session_key == _last_startup_race_key
    )
    # Negative cache: avoid hammering gateway on persistent failures.
    # Silent during the cache window -- only the structured audit event is
    # emitted to keep gateway.log readable.  Two windows: a long one for
    # genuine HTTP/network failure, a short one for benign startup races.
    # The startup-race window debounces a race that belongs to ONE identity
    # -- no key resolvable yet, or a key the gateway has not registered yet --
    # so it answers only for the identity that opened it. A call that
    # resolved a DIFFERENT identity is past that race by definition: an
    # unidentified ``tools/list`` opening the window must not make the
    # ``tools/call`` that resolves a real key seconds later answer
    # ``no_session_key`` for a session whose key is in hand, and a session
    # the gateway answered 404 for must not silence a sibling it has
    # registered. A caller WITH a verified per-call identity skips it
    # outright, as before: it is past every startup race by construction.
    if not ignore_negative_cache and (_failure_cached or _race_cached):
        # WHICH clock fired is part of the answer, not an implementation
        # detail: the two windows cache different conditions and the call site
        # treats them differently. Collapsing them into one reason would repeat
        # the very conflation this resolver exists to undo, one level down.
        _reason = "resolution_failed" if _failure_cached else "no_session_key"
        sel().log_api_access(
            caller=session_key or "mcp",
            operation="tool_policy.negative_cache_hit",
            outcome="unresolved",
            source="mcp_shared",
            resources=f"reason={_reason}",
            error=_last_failure_class if _failure_cached else "",
        )
        return ToolPolicy(frozenset(), _reason, _last_failure_class if _failure_cached else "")

    try:
        port, _source = resolve_client_port_src(None)
        api_base = f"http://127.0.0.1:{port}"

        # Credential for the port this function DIALS (parsed just above), not for
        # whichever gateway an ambient lookup would name -- those can differ on a
        # multi-gateway host, which is the desync being closed. The v4 loopback
        # LITERAL (matching the dial) reaches one family, so a single-family
        # gateway (v4-only or a wildcard/container bind) still authenticates; the
        # ambiguous ``localhost`` would demand both families and 403 such a gateway.
        secret = ""
        try:
            secret = read_local_secret(port, dial_host="127.0.0.1")
        except Exception:
            pass

        if session_key is None:
            # Resolution itself broke rather than finding nothing. Raise into the
            # handler below so a broken host keeps the LONG window and the
            # resolution_failed reason it already had when this resolution was
            # inline, instead of being reported as the benign startup race.
            raise RuntimeError("tool-policy session resolution failed")

        if not session_key:
            # PATH 1/3. No session key resolvable (startup race -- kiro-cli
            # hasn't written the PID file yet, or the process is from the warm
            # pool).  The exclusion set stays empty so ``tools/list`` still
            # lists everything: kiro-cli calls tools/list once and caches the
            # result, so hiding all tools here would hide them for the whole
            # session, unrecoverably.  ``unresolved`` is what stops that empty
            # set from being read as a permission -- ``tools/call`` refuses
            # while it is set.  Short negative cache (5s) debounces the warning
            # storm during parallel MCP startup; the session_pid file typically
            # appears within a few hundred ms of MCP spawn.
            _last_startup_race_time = now
            _last_startup_race_key = ""
            sel().log_api_access(
                caller="mcp",
                operation="tool_policy.no_session_key",
                outcome="unresolved",
                source="mcp_shared",
            )
            return ToolPolicy(frozenset(), "no_session_key")

        # The declared key needs the attestation that goes with it. Without the
        # token this read is answered as a caller the gateway cannot name, the
        # policy stays unresolved, and the fail-closed branch below then refuses
        # every tool call for the session. The explicit argument wins: the
        # ``tools/call`` dispatch checks the policy before it installs the
        # caller ContextVar, so ``current_caller()`` is None exactly when the
        # pooled backend most needs the token (see the docstring).
        from kiro_crew.session_token_sig import session_token_header

        _tok = caller_token
        if not _tok:
            _ctx = current_caller()
            _tok = _ctx.session_token if _ctx is not None and _ctx.from_gateway else ""
        token_header = session_token_header(_tok)

        # A key resolved LOCALLY (``not caller_session``) from the lenient tail of
        # :func:`_policy_session_key` -- the unsigned ``session_pid`` read reached
        # through ``KIROCREW_HOST_PID`` and the ancestor walk -- carries no
        # attestation the gateway can check: ``session_token_header`` found no token
        # to send with it, and the key is not the gateway-injected
        # ``KIROCREW_SESSION_KEY``. Dialled anyway, that request is answered
        # ``member_identity_unavailable`` on EVERY call, so the round-trip is futile
        # and its ``identity_unattested`` refusal text misreports a dial that never
        # needed to happen. Skip the dial and return a reason WITHOUT the key on the
        # wire -- but a fail-CLOSED one.
        #
        # Fail closed is required, not optional: a session key DID resolve, so an
        # operator ``managedToolPolicy.exclude`` may exist for it, and an exclusion
        # this process could not read is an unknown deny, never a permission.
        # ``identity_unattestable`` is therefore in ``_UNRESOLVED_REFUSES_CALL``
        # beside ``identity_unattested``: ``tools/list`` still lists everything
        # (kiro-cli caches one listing, so hiding tools there is unrecoverable) and
        # ``tools/call`` refuses until a real identity channel (a gateway caller
        # block, a signed token) makes the key attestable. It is a distinct reason
        # from ``identity_unattested`` only so the refusal text does not claim a
        # gateway read that never happened.
        #
        # Two sources are deliberately NOT withheld. A gateway-stamped
        # ``caller_session`` is the gateway's own per-call identity -- it named this
        # caller, so the key is vouched for even when the per-call block carried no
        # token. ``KIROCREW_SESSION_KEY`` is the identity the gateway injects into its
        # own processes, which the attested topologies accept; a bare declared key
        # that the current transport cannot attest is the gateway's boundary to hold
        # on the dial, not a resolution this client should pre-empt.
        _has_env_key = bool(os.environ.get("KIROCREW_SESSION_KEY", ""))
        if not caller_session and "X-Session-Token" not in token_header and not _has_env_key:
            sel().log_api_access(
                caller=session_key,
                operation="tool_policy.unattestable_key",
                outcome="unresolved",
                source="mcp_shared",
                resources=f"session_key={session_key}",
            )
            return ToolPolicy(frozenset(), "identity_unattestable")

        headers: dict[str, str] = {"X-Internal-Secret": secret, **token_header}
        headers["X-Session-Key"] = session_key

        req = urllib.request.Request(
            f"{api_base}/api/session-tool-policy",
            headers=headers,
        )
        try:
            with loopback_urlopen(req, timeout=5) as resp:
                policy = json.loads(resp.read())
        except urllib.error.HTTPError as http_exc:
            if http_exc.code == 404:
                # PATH 2/3. 404 = "agent not resolved" (the gateway hasn't
                # registered this session yet -- common during MCP startup
                # before the session_pid file is fully visible across
                # processes).  A benign race, so the short startup-race cache
                # applies and the session recovers within seconds; until it
                # does, ``unresolved`` keeps ``tools/call`` refusing rather
                # than serving an exclusion set nobody read.  Critically, do
                # NOT log a stack trace for 404 -- it floods gateway.log on
                # every fresh subagent spawn.
                _last_startup_race_time = now
                _last_startup_race_key = session_key
                sel().log_api_access(
                    caller=session_key,
                    operation="tool_policy.agent_not_resolved",
                    outcome="unresolved",
                    source="mcp_shared",
                    resources=f"session_key={session_key}",
                )
                return ToolPolicy(frozenset(), "agent_not_resolved")
            if http_exc.code == 409:
                # Two different refusals share this status, told apart by the
                # body's ``code`` -- the status alone stopped meaning one thing
                # when the endpoint grew its attestation gate.
                _code, _reason = _http_error_body(http_exc)
                if _code == "member_identity_unavailable":
                    # ``internal_memory_scope`` declined to answer THIS caller:
                    # the declared ``X-Session-Key`` reached the gateway without
                    # an attestation (no ``X-Session-Token`` in the request, or
                    # one that names another key). Nothing about the spec was
                    # read. Reported as its own reason so the refusal names the
                    # missing token rather than the agents directory, and so a
                    # reader of the audit trail can tell "no token reached this
                    # call" from "the operator's spec is malformed". Not
                    # negative-cached, for the same reason as the spec case:
                    # the answer is immediate and specific to this caller.
                    # ``pid``/``ppid`` name WHICH process asked: a server
                    # launched without the token often lives for one
                    # ``tools/list`` and is gone before anyone can look, and
                    # its parent (gatewayd for a pooled backend, the harness
                    # for a server it launched itself) is what tells the
                    # launch paths apart afterwards.
                    sel().log_api_access(
                        caller=session_key,
                        operation="tool_policy.unattested",
                        outcome="unresolved",
                        source="mcp_shared",
                        resources=(
                            f"session_key={session_key},"
                            f"token={'present' if _tok else 'absent'},"
                            f"pid={os.getpid()},ppid={os.getppid()}"
                        ),
                    )
                    return ToolPolicy(frozenset(), "identity_unattested")
                # The gateway read a spec for this session and could not
                # determine its policy (unparseable, wrong shape, or two specs
                # claiming the name). An unparseable or code-less body takes
                # this arm too: it is the status the endpoint has always used
                # for that condition, and an unknown 409 must not be read as
                # anything narrower. Deliberately NOT negative-cached: the
                # windows above exist to avoid repeated 5s urlopen timeouts, and
                # this answer is immediate, so there is nothing to debounce. It
                # is also specific to THIS session's agent spec, and
                # ``_last_failure_time`` is process-global -- caching it there
                # would refuse tool calls for every sibling session in a pooled
                # backend over one agent's malformed file. Re-asking each call
                # costs one loopback round-trip and recovers the moment the
                # operator fixes the spec. The body's ``reason`` -- the file the
                # gateway could not read, and what to do -- rides along as
                # ``detail`` so the refusal can say it; the decision is the
                # status and code alone, exactly as before.
                sel().log_api_access(
                    caller=session_key,
                    operation="tool_policy.unreadable",
                    outcome="unresolved",
                    source="mcp_shared",
                    resources=f"session_key={session_key}",
                )
                return ToolPolicy(frozenset(), "policy_unreadable", _reason)
            if http_exc.code in (400, 403):
                # The gateway ANSWERED and declined to tell this caller. 403 is
                # ``member_session_unverified`` from ``internal_memory_scope``:
                # a session claiming a private memory store without a proof the
                # gateway can verify. 400 is a caller that named no session or a
                # rejected agent name. Both are authorization boundaries the
                # gateway holds deliberately, they answer instantly, and they
                # are the permanent steady state for a whole class of callers
                # rather than a window that closes.
                #
                # Kept OUT of the process-global failure cache for the same
                # reason as 409: the answer is immediate so there is nothing to
                # debounce, and it is specific to THIS caller's identity, so
                # caching it globally would deny sibling sessions over one
                # caller's missing proof.
                sel().log_api_access(
                    caller=session_key,
                    operation="tool_policy.forbidden",
                    outcome="unresolved",
                    source="mcp_shared",
                    resources=f"session_key={session_key},status={http_exc.code}",
                )
                return ToolPolicy(frozenset(), "policy_forbidden")
            if 400 <= http_exc.code < 500:
                # Any OTHER 4xx. The gateway answered and made a decision about
                # this caller -- it is enforcing something, not failing to read
                # the policy -- so it joins the permissive, audited class with
                # the two above.
                #
                # This arm exists because the alternative is an enumerated list
                # of statuses, and a list is only as good as its author's
                # knowledge of the endpoint. A status nobody enumerated would
                # fall through to the failure catch-all and be refused, which
                # would deny a whole class of callers over a condition that is
                # not a failure at all. Deciding by CLASS is robust to the
                # endpoint growing a status this code has never seen.
                logger.warning(
                    "Tool policy endpoint answered %s for session %s; treating it "
                    "as a boundary the gateway holds, not a failure to read",
                    http_exc.code,
                    session_key,
                )
                sel().log_api_access(
                    caller=session_key,
                    operation="tool_policy.forbidden",
                    outcome="unresolved",
                    source="mcp_shared",
                    resources=f"session_key={session_key},status={http_exc.code}",
                )
                return ToolPolicy(frozenset(), "policy_forbidden")
            raise

        exclude = policy.get("exclude", [])
        # A policy the gateway ANSWERED is not automatically a policy this
        # understood. An absent ``exclude`` key is a real empty exclusion list
        # and stays resolved -- that is the ordinary case. But a present
        # ``exclude`` of the wrong shape, or one holding an entry that is not a
        # tool name, is a policy whose meaning is unknown: the operator wrote
        # something there and it cannot be read. Silently narrowing it to the
        # part that happens to parse would enforce a policy nobody wrote, which
        # is the same withheld deny as reading none at all. Neither shape occurs
        # in a valid config, so refusing costs no working caller. The gateway
        # takes the identical line one level up for a non-dict
        # ``managedToolPolicy``.
        if not isinstance(exclude, list) or any(not isinstance(t, str) for t in exclude):
            _shape = (
                "not a list"
                if not isinstance(exclude, list)
                else "a list holding a non-string entry"
            )
            logger.warning(
                "Tool policy for session %s has a malformed exclude (%s); "
                "refusing calls rather than enforcing the part that parses",
                session_key,
                _shape,
            )
            sel().log_api_access(
                caller=session_key,
                operation="tool_policy.unreadable",
                outcome="unresolved",
                source="mcp_shared",
                resources=f"session_key={session_key},exclude={_shape}",
            )
            # The one ``policy_unreadable`` this process diagnoses itself, so
            # its ``detail`` is its own: the refusal then says what is wrong
            # with the policy the gateway answered, the way the gateway's
            # ``reason`` says which file it could not read.
            return ToolPolicy(
                frozenset(),
                "policy_unreadable",
                f"the policy the gateway answered has a managedToolPolicy.exclude that is "
                f"{_shape}; fix the exclude list in this agent's spec, no restart needed",
            )
        resolved = set(exclude)
        # FIFO bound: dicts preserve insertion order; drop the oldest
        # session's entry when full (pooled backends serve churning sessions).
        while len(_excluded_tools_by_session) >= _EXCLUDED_TOOLS_CACHE_MAX:
            _excluded_tools_by_session.pop(next(iter(_excluded_tools_by_session)))
        _excluded_tools_by_session[session_key] = resolved
        return ToolPolicy(frozenset(resolved), "")
    except Exception as exc:
        # PATH 3/3. The gateway did not give a usable answer: no answer at all
        # (network error, timeout, connection refused), or a 5xx saying it is
        # broken. Every 4xx returns above, decided by CLASS rather than by an
        # enumerated list, so reaching here means the gateway could not read the
        # policy -- never that it made a decision about this caller.
        #
        # That single meaning is what the refusal recorded at
        # ``_UNRESOLVED_REFUSES_CALL`` rests on: the exclusion set here is unknown,
        # not empty. The LONG negative cache avoids repeated 5s urlopen blocks
        # across many MCP servers while the gateway is down, and it also bounds how
        # long the refusal lasts. That
        # cache is process-global, which is sound HERE and nowhere else: a gateway
        # this process cannot reach is unreachable for every session in it, so
        # there is no sibling the window wrongly affects.
        _last_failure_time = time.monotonic()
        _failure_count += 1
        _last_failure_class = exc.__class__.__name__
        # Suppress repeated warnings — once we've logged twice the operator
        # has all the diagnostic info and further entries flood gateway.log
        # at every MCP server startup (10+ servers × every session start).
        if _failure_count <= _MAX_WARNING_FAILURES:
            logger.warning(
                "Tool policy resolution failed (%s); this session's exclusions are "
                "unknown and tool calls are REFUSED for up to %.0fs",
                exc.__class__.__name__,
                _NEGATIVE_CACHE_TTL,
                exc_info=True,
            )
        elif _failure_count == _MAX_WARNING_FAILURES + 1:
            logger.warning(
                "Tool policy resolution still failing — further warnings suppressed; "
                "see audit log for tool_policy.resolution_failed events",
            )
        sel().log_api_access(
            caller=session_key or "mcp",
            operation="tool_policy.resolution_failed",
            outcome="unresolved",
            source="mcp_shared",
            resources=f"error_class={_last_failure_class}",
            error=_failure_cause(exc),
        )
        return ToolPolicy(frozenset(), "resolution_failed", _last_failure_class)


def _failure_cause(exc: BaseException) -> str:
    """``Class: message`` for an audit event -- redacted, one line, capped.

    The message can carry a URL, a path or a header value, so it goes through
    the log redactor before it is written. A redactor that cannot run withholds
    the message and keeps the class: the class alone is what names the cause.
    """
    name = exc.__class__.__name__
    try:
        from kiro_crew.platform import redact_log_via_context

        message = redact_log_via_context(" ".join(str(exc).split()))
    except Exception:
        message = "<withheld: redaction unavailable>"
    if len(message) > _FAILURE_MESSAGE_MAX:
        message = message[: _FAILURE_MESSAGE_MAX - 3] + "..."
    return f"{name}: {message}" if message else name


def _resolve_excluded_tools(caller_session: str = "") -> set[str]:
    """Return the ordinary tool-policy exclusions for compatibility callers.

    The policy resolver keeps the unresolved reason so ``tools/call`` can retain
    its fail-closed behavior. Diagnostics that only need exclusions use this
    narrow projection; it carries no member-memory capability or proof.
    """
    return set(_resolve_tool_policy(caller_session).excluded)


# Which unresolved reasons refuse a ``tools/call``.
#
# The test is not how bad the reason sounds. It is whether the reason means ONE
# thing, because a security decision derived from an ambiguous reason is wrong
# for half the callers it hits. Four reasons qualify, and each means "an operator
# exclusion may exist and this system could not read it":
#
# * ``policy_unreadable`` -- the gateway found a spec for this session and could
#   not determine its policy. It emits this for that condition and nothing else.
# * ``identity_unattested`` -- the gateway would not read the policy for THIS
#   caller because the declared session key arrived without the attestation
#   that goes with it (no ``X-Session-Token``, or one naming another key). The
#   spec was never consulted, so whatever it excludes is unknown here. Refusing
#   is the same withheld-deny argument as the line above; the reason is kept
#   separate so the refusal names the missing token, not the agents directory.
#   A legitimate pooled backend never lands here: it is handed the token per
#   frame in the caller block and ``_resolve_tool_policy`` sends it.
# * ``identity_unattestable`` -- a session key resolved from the lenient tail of
#   ``_policy_session_key`` (the unsigned ``session_pid`` read, the ancestor walk)
#   with no token to carry it and no gateway-injected ``KIROCREW_SESSION_KEY``.
#   The dial is skipped because the gateway would refuse it, so the exclusion
#   list is unread for a key that DID resolve -- the same unknown-deny as
#   ``identity_unattested``, before the request rather than after it. Kept
#   separate only so the refusal text does not claim a gateway read that never
#   happened.
# * ``resolution_failed`` -- no usable answer reached this process: nothing came
#   back, the gateway answered ``5xx`` to say it is broken, or the resolve itself
#   raised. Every ``4xx`` returns before that arm, decided by status CLASS, so this
#   reason means the policy could not be READ and never that the gateway made a
#   decision about this caller. An operator exclusion may exist while the process
#   holding it cannot answer for it, so the exclusion set is unknown rather than
#   empty and the withheld deny stays withheld. The cost is bounded at both ends:
#   a session that resolves its policy once is served from
#   ``_excluded_tools_by_session`` and never reaches a failure path again, and for
#   one that has not, the refusal lasts at most ``_NEGATIVE_CACHE_TTL``.
#
# The remaining reasons stay permissive because each covers a caller for whom no
# operator exclusion is known to exist, or a class for which refusal is permanent
# rather than a window that closes:
#
# * ``agent_not_resolved`` is the gateway's 404, returned BOTH for a session
#   still registering (a policy may exist) and for a caller it can never map to
#   an agent (no policy can exist). Refusing denies the second class forever.
# * ``no_session_key`` is the same gap inside this process: no agent is named, so
#   no operator exclusion is known to exist for the call to bypass.
# * ``policy_forbidden`` is ANY 4xx. The gateway answered and made a decision
#   about this caller -- for 403 ``member_session_unverified`` that is the steady
#   state of a session claiming a private store without a verifiable proof.
#   Refusing would deny such a class permanently, and a boundary the gateway is
#   enforcing is not a boundary it failed to read. The test is the status class,
#   not a list of names, so an unfamiliar 4xx is permissive-and-audited rather
#   than silently reclassified as an outage.
#
# Those permissive windows are audited per call, so they are visible instead of
# silent -- which is what made this condition hard to find. Closing them needs
# the 404 to distinguish registering from unmappable, which is a change to the
# endpoint's contract rather than to this read.
_UNRESOLVED_REFUSES_CALL = frozenset(
    {"policy_unreadable", "identity_unattested", "identity_unattestable", "resolution_failed"}
)


def respond(req_id: Any, result: Any, error: dict | None = None) -> None:
    """Write a validated JSON-RPC response to stdout."""
    if req_id is None:
        return
    resp: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
    if error:
        resp["error"] = error
    else:
        resp["result"] = result
    try:
        resp = validate_jsonrpc_response(resp)
    except ValidationError:
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32603, "message": "Internal error"},
        }
    _write_frame(resp, label=f"request {req_id}")


def notify(method: str, params: dict[str, Any]) -> None:
    """Write a server->client JSON-RPC notification (no ``id``) to stdout."""
    _write_frame({"jsonrpc": "2.0", "method": method, "params": params}, label=method)


def _write_frame(msg: dict[str, Any], *, label: str) -> None:
    """Frame ``msg`` per the negotiated framing and write it to the client.

    ``label`` names the frame in the torn-write log line (a request id, or a
    notification method).
    """
    body = json.dumps(msg)
    if _framing == "content-length":
        payload = body.encode("utf-8")
        frame = f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8") + payload
    else:
        frame = (body + "\n").encode("utf-8")

    # Preferred path: the private descriptor captured before any tool ran, so a
    # library's process-wide dup2 on fd 1 (see _stdout_fd above) cannot swallow
    # this response. Serialized -- os.write is unbuffered and a partial
    # interleave would tear the frame.
    with _stdout_fd_lock:
        # Re-read INSIDE the lock: a concurrent release_stdout_fd() between an
        # outside-the-lock read and the write could otherwise hand os.write a
        # closed descriptor whose number has already been recycled by another
        # open() -- sending JSON-RPC bytes into an unrelated file. Not reachable
        # from today's single-threaded dispatch, but the lock is already held
        # here, so pay nothing to make it structurally safe.
        fd = _stdout_fd
        if fd is not None:
            try:
                _write_all(fd, frame)
                return
            except OSError as exc:
                # The dup'd fd is unusable (client pipe closed). Fall back to
                # sys.stdout ONLY if nothing was written, so a genuinely broken
                # pipe surfaces the way it did before this indirection. After a
                # PARTIAL write, re-emitting the whole frame would duplicate the
                # prefix and desync the stream for every later message -- drop
                # it instead and let the client's own timeout handle the turn.
                if getattr(exc, "bytes_written", 0):
                    logger.error(
                        "Torn JSON-RPC frame for %s: wrote %d of %d bytes "
                        "before %s; dropping rather than duplicating the prefix",
                        label,
                        exc.bytes_written,  # type: ignore[attr-defined]
                        len(frame),
                        exc.__class__.__name__,
                    )
                    return
    if _framing == "content-length":
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(frame.decode("utf-8"))
        sys.stdout.flush()


def _audit_safe_args(value: Any) -> Any:
    """Keep argument shape for SEL while excluding caller-supplied values."""
    if isinstance(value, dict):
        return {str(key): _audit_safe_args(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_audit_safe_args(item) for item in value]
    if isinstance(value, tuple):
        return [_audit_safe_args(item) for item in value]
    if isinstance(value, str):
        return "<redacted>"
    return value


@overload
def call_tool_with_logging(
    name: str,
    raw_args: dict[str, Any],
    validate_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    inner_fn: Callable[[str, dict[str, Any]], str],
    session_key: str,
    downstream_service: str,
) -> str: ...


@overload
def call_tool_with_logging(
    name: str,
    raw_args: dict[str, Any],
    validate_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    inner_fn: Callable[[str, dict[str, Any]], "str | DeferredTool"],
    session_key: str,
    downstream_service: str,
) -> "str | DeferredTool": ...


def call_tool_with_logging(
    name: str,
    raw_args: dict[str, Any],
    validate_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    inner_fn: Callable[[str, dict[str, Any]], "str | DeferredTool"],
    session_key: str,
    downstream_service: str,
) -> "str | DeferredTool":
    """Validate args, call inner tool function, and log the invocation.

    A :class:`DeferredTool` the handler returns is passed through with its
    audit row attached to the step that settles it. The overloads let a server
    whose handlers only ever return text (every row-based server, the cron and
    computer servers) keep a ``str`` contract without a cast at each call site;
    only ``kirocrew-core``'s dispatcher admits a deferred.
    """
    try:
        args = validate_fn(name, raw_args)
    except ValidationError as e:
        # No ``tool_kind``: it is a CLASSIFICATION of the invocation -- callers
        # that write their own rows pass things like "authz" -- and this wrapper
        # has no per-tool taxonomy to supply. Passing ``session_key`` here would be
        # both wrong and redundant, since ``caller_identity`` on the same
        # record already carries it: every row written through
        # here, across all five MCP servers, would hold a high-cardinality session
        # key where a kind belongs, making the field useless to filter or
        # aggregate on while still looking populated. The parameter
        # defaults to "", and an honestly empty kind beats a false one.
        sel().log_tool_invocation(
            session_key=session_key,
            source="mcp",
            tool_name=name,
            outcome="failed",
            downstream_service=downstream_service,
            error="validation_failed",
        )
        # A rejection is BY CONSTRUCTION not a directive, and this message
        # interpolates content this process does not control: an unknown-field
        # error echoes the argument KEY, so a key carrying the directive sentinel
        # plus a JSON payload plus a newline turns a rejected call into a decodable
        # directive under the genuine tool's authenticated identity - applying the
        # arguments validation had just refused. Defang here, where "this is an
        # error" is known, rather than centrally where the real marker lives.
        return f"Error: {neutralize_markers(str(e))}"

    result = inner_fn(name, args)
    # Redact the serialized args before they land in the SEL audit resources.
    # Tool args can carry agent-supplied free text (e.g. artifact_post_comment
    # `text`, artifact_delete_comment `reason`) that may contain a credential;
    # per-tool handlers redact their OWN egress copy, but the args dict logged
    # here is a separate validated object, so redact centrally through the
    # canonical context-aware shim (defense-in-depth for every tool, not just
    # the ones a handler happened to scrub).
    resources = ""
    if args:
        from kiro_crew.platform import redact_via_context

        resources = redact_via_context(json.dumps(_audit_safe_args(args)))[:500]

    def _audit(text: str) -> None:
        outcome = "failed" if text.startswith("Error:") else "completed"
        sel().log_tool_invocation(
            session_key=session_key,
            source="mcp",
            tool_name=name,
            # No ``tool_kind`` -- see the ValidationError path above.
            outcome=outcome,
            downstream_service=downstream_service,
            resources=resources,
            error="execution_failed" if outcome == "failed" else "",
        )

    if isinstance(result, DeferredTool):
        # The call has not finished: audit it when its terminal step runs,
        # whoever drives the steps (the dispatch loop or drive_deferred).
        return _AuditedDeferred(result, _audit)
    _audit(result)
    return result


class _AuditedDeferred(DeferredTool):
    """A :class:`DeferredTool` whose settling step also writes the SEL row
    :func:`call_tool_with_logging` writes for a blocking tool at return."""

    def __init__(self, inner: DeferredTool, audit: Callable[[str], None]) -> None:
        self._inner = inner
        self._audit = audit

    def due_at(self) -> float:
        return self._inner.due_at()

    def step(self) -> str | None:
        # An escaped exception propagates: the driver answers and audits it
        # the way ``_run_tool`` does for a blocking tool, exactly once.
        return self._inner.step()

    def cancel(self) -> None:
        self._inner.cancel()

    def abandon(self) -> str | None:
        return self._inner.abandon()

    def on_settled(self, text: str) -> None:
        self._inner.on_settled(text)
        self._audit(text)


def _read_message(stdin) -> dict[str, Any] | _Skipped | None:
    """Read one JSON-RPC frame, auto-detecting Content-Length vs bare JSON framing.

    Returns the message, ``None`` at end of stream, or :data:`SKIP` for one
    frame that was dropped: a line or body that is not valid UTF-8, not JSON,
    not a JSON object, or nested past the decoder's ceiling (see
    :func:`kiro_crew.json_line.parse_json_object_line`), which is answered as
    a parse error when it is a request whose top-level id is recoverable
    (:func:`_answer_unparseable`), a header-shaped line on a bare-JSON stream,
    and a body over :data:`MAX_CONTENT_LENGTH_BYTES`, which is drained and
    answered as too large when its id is recoverable. A Content-Length header
    whose length cannot be read, or one past any drainable size, ends the
    stream (``None``, logged at ERROR): the body behind an unreadable one
    cannot be delimited, so every later frame would be read joined to it, and
    draining to an absurd one would swallow every later request.

    Uses stdin.buffer (binary mode) for all reads, and keeps the frame as
    bytes until it is parsed, so Content-Length byte counts are honoured
    correctly for multi-byte UTF-8 content.
    """
    global _framing
    raw = stdin.buffer
    line = raw.readline()
    if not line:
        return None  # EOF
    stripped = line.strip()
    if stripped[:15].lower() != b"content-length:":
        # Bare JSON line (backwards compat).
        msg = parse_json_object_line(line)
        if msg is None:
            _answer_unparseable(line)
            return SKIP
        _framing = _framing or "bare"
        return msg
    if _framing == "bare":
        return SKIP
    try:
        length = int(stripped[15:])
    except ValueError:
        length = -1
    if length < 0:
        logger.error(
            "unreadable Content-Length header %r; the framing is lost, ending the stream",
            stripped[:80],
        )
        return None
    if length > _MAX_DRAINABLE_LENGTH_BYTES:
        logger.error(
            "Content-Length %d is past any drainable size; the framing is lost, "
            "ending the stream",
            length,
        )
        return None
    _framing = "content-length"
    # Consume the blank line separator
    while True:
        sep = raw.readline()
        if sep.strip() == b"":
            break
    # Read exactly `length` bytes, in bounded reads: a single raw.read(length)
    # may return fewer bytes than requested on a partial read (the
    # RawIOBase/socket contract permits short reads), which would truncate the
    # body and desync the stream for every subsequent message. A body over the
    # cap is drained the same way, keeping only its two ends for the id.
    keep = length <= MAX_CONTENT_LENGTH_BYTES
    chunks: list[bytes] = []
    head = tail = b""
    remaining = length
    while remaining > 0:
        chunk = raw.read(min(remaining, _DRAIN_CHUNK_BYTES))
        if not chunk:
            # EOF before the declared body fully arrived — the message is
            # incomplete. Discard it explicitly rather than parsing a
            # truncated body, which could otherwise return a message the
            # sender never finished transmitting if the partial bytes happen
            # to be valid JSON (e.g. a well-formed prefix).
            return None
        remaining -= len(chunk)
        if keep:
            chunks.append(chunk)
            continue
        if len(head) < ID_PROBE_BYTES:
            head += chunk[: ID_PROBE_BYTES - len(head)]
        tail = (tail + chunk[-ID_PROBE_BYTES:])[-ID_PROBE_BYTES:]
    if not keep:
        req_id = recover_top_level_id(head, tail, requests_only=True)
        logger.warning(
            "Content-Length %d is over the %d-byte cap; body discarded (id %r)",
            length,
            MAX_CONTENT_LENGTH_BYTES,
            req_id,
        )
        if req_id is not None:
            respond(
                req_id,
                None,
                error={
                    "code": JSONRPC_INVALID_REQUEST,
                    "message": f"Request over the {MAX_CONTENT_LENGTH_BYTES}-byte limit",
                },
            )
        return SKIP
    body = b"".join(chunks)
    msg = parse_json_object_line(body)
    if msg is None:
        _answer_unparseable(body)
        return SKIP
    return msg


def _answer_unparseable(frame: bytes) -> None:
    """Answer a request frame that does not parse, when its id is recoverable.

    Unanswered, its caller would wait for its own timeout while pings are
    still answered, so nothing would see the server as wedged.
    Only a request (a top-level ``method``) is answered, under the top-level
    object's own id found at either end
    (:func:`kiro_crew.json_line.recover_line_id`); a log line, a response or a
    frame with no recoverable id is just dropped.
    """
    if not frame.lstrip().startswith(b"{"):
        return
    req_id = recover_line_id(frame, requests_only=True)
    logger.warning("unparseable JSON-RPC frame dropped (id %r)", req_id)
    if req_id is not None:
        respond(req_id, None, error={"code": JSONRPC_PARSE_ERROR, "message": "Parse error"})


def _stdin_holds_a_line(stdin) -> bool:
    """True when *stdin*'s buffer already holds a complete line.

    ``select`` polls the descriptor, which cannot see bytes the buffered reader
    has already pulled off it: a request that arrived in the same read as the
    line just handled is invisible to it until the client writes again. The
    peek runs with the descriptor non-blocking, so an empty buffer costs one
    refused read rather than a wait. ``False`` for a stream with no peek or
    descriptor (nothing it holds is hidden from ``select``).
    """
    raw: Any = getattr(stdin, "buffer", None)
    peek = getattr(raw, "peek", None)
    if peek is None:
        return False
    try:
        fd = raw.fileno()
        was_blocking = os.get_blocking(fd)
    except (AttributeError, OSError, ValueError):
        return False
    try:
        os.set_blocking(fd, False)
        try:
            held = peek(1)
        except (BlockingIOError, OSError, ValueError):
            return False
    finally:
        with contextlib.suppress(OSError, ValueError):
            os.set_blocking(fd, was_blocking)
    return b"\n" in held


def _tool_call_name(params: dict[str, Any]) -> str | None:
    """The tool a ``tools/call`` names, or ``None`` when it names none usable."""
    name = params.get("name")
    return name if isinstance(name, str) and name else None


def _hard_exit_on_signal(_signum: int, _frame: Any) -> None:
    """Exit at once on SIGTERM/SIGINT, skipping interpreter finalization.

    Tool work runs on a daemon thread, so a normal exit finalizes the stdio
    streams under it and can abort with ``_enter_buffered_busy`` (SIGABRT, a
    crash dialog on macOS) during a provider's group teardown. Same fix as the
    gateway stub's ``_hard_exit``: drain logging, flush stderr, ``os._exit``.
    stdout is NOT flushed: another thread may hold its lock, and waiting on it
    here would keep the process from ever exiting.
    """
    try:
        logging.shutdown()
    except Exception:  # pragma: no cover - never block exit on log teardown
        pass
    try:
        sys.stderr.flush()
    except (OSError, ValueError, RuntimeError):  # RuntimeError: reentrant flush
        pass
    os._exit(0)


def _install_hard_exit_handlers() -> dict[int, Any]:
    """Install the handler for SIGTERM/SIGINT; return the ones it replaced."""
    if threading.current_thread() is not threading.main_thread():
        return {}  # signal.signal only works on the main thread
    sigs = (signal.SIGTERM, signal.SIGINT)
    return {sig: signal.signal(sig, _hard_exit_on_signal) for sig in sigs}


def run_mcp_stdio_loop(
    server_name: str,
    server_version: str,
    list_tools_fn: Callable[[], list[dict[str, Any]]],
    call_tool_fn: Callable[[str, dict[str, Any]], "str | DeferredTool"],
    *,
    advertise_caller_identity: bool = False,
    error_prefix_is_error: bool = False,
    reports_inflight: bool = False,
) -> None:
    """Generic MCP stdio server loop — reads JSON-RPC from stdin, writes to stdout.

    Tool calls run in a worker thread so the main read loop stays responsive to
    ``notifications/cancelled`` messages from the gateway. A cancel for a call
    running on the worker lets that call run to its end and suppresses its
    response (per MCP spec); a cancel for a parked :class:`DeferredTool` drops
    it and runs its ``cancel()`` hook.

    A handler that only has to WAIT (``wait``, ``spawn_sub_agents``) returns a
    :class:`DeferredTool` instead of parking the worker: the loop registers it,
    runs its short ``step()`` from its own timer whenever ``due_at()`` has
    passed, and answers the request when a step settles it. Parked calls hold
    no thread, and the one worker stays free for the calls that compute.
    A ``notifications/cancelled`` for a parked call
    drops it with no response. While any call is parked, and when the client
    declared :data:`INFLIGHT_CAPABILITY_KEY` in ``initialize``, the loop sends
    :data:`INFLIGHT_NOTIFICATION` every :data:`INFLIGHT_REPORT_SECS` naming the
    parked ids still progressing, so the gateway's wedge detector never reads a
    parked call as a hung one.

    That report is opt-in per server (*reports_inflight*). It exists for a loop
    that parks ``DeferredTool`` calls; a server whose every tool is synchronous
    gains nothing from it, and advertising it would only lower the gateway's
    recycle ceiling under that server's running call. Without the opt-in the
    loop neither advertises the capability nor sends the notification, even to
    a client that declared it.

    ``tools/call`` requests that arrive while a worker is busy are buffered in
    a bounded FIFO queue and dispatched in order as the worker frees
    (silently dropping them left the client waiting forever on a response
    that never came). Queue overflow gets an immediate busy error response.

    On Windows ``select.select`` cannot poll ``sys.stdin`` (it only accepts
    sockets), so tool calls dispatch synchronously exactly as the pre-worker
    loop did — no in-flight cancel/ping interleave there (POSIX-only feature),
    and a deferred tool is driven to completion inline.

    Before serving anything, a private dup of stdout is captured
    (:func:`snapshot_stdout_fd`) so responses survive a library's process-wide
    ``dup2`` on fd 1 — see the ``_stdout_fd`` comment block. It is released on
    exit so repeated loops in one process (the test suite) cannot leak fds.
    """
    # Declare this process's identity for loopback requests FIRST — tool calls
    # dispatched below reach the gateway through mcp_core's request helpers,
    # which attach it as ``X-Internal-Caller`` so the audit log can name the
    # component (not just "an internal caller") behind each write. Restored on
    # exit, like the fd snapshot below, so repeated loops in one process (the
    # test suite) cannot leak one server's identity into later requests.
    _prior_caller = internal_caller()
    set_internal_caller(server_name)
    snapshot_stdout_fd()
    prior_handlers = _install_hard_exit_handlers()
    pruned = False
    try:
        pruned = _run_stdio_dispatch_loop(
            server_name,
            server_version,
            list_tools_fn,
            call_tool_fn,
            advertise_caller_identity=advertise_caller_identity,
            error_prefix_is_error=error_prefix_is_error,
            reports_inflight=reports_inflight,
        )
    finally:
        set_internal_caller(_prior_caller)
        release_stdout_fd()
        # Restore, so repeated loops in one process (the test suite) do not
        # leave a later Ctrl-C or SIGTERM exiting 0 through this handler.
        for sig, handler in prior_handlers.items():
            signal.signal(sig, signal.SIG_DFL if handler is None else handler)
    if pruned:
        # Non-zero so the pool records a death and the next call respawns this
        # server from the current install's launch command.
        sys.exit(INSTALL_PRUNED_EXIT_CODE)


def _run_stdio_dispatch_loop(
    server_name: str,
    server_version: str,
    list_tools_fn: Callable[[], list[dict[str, Any]]],
    call_tool_fn: Callable[[str, dict[str, Any]], "str | DeferredTool"],
    *,
    advertise_caller_identity: bool = False,
    error_prefix_is_error: bool = False,
    reports_inflight: bool = False,
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

    def _caller_tool_policy(caller: "CallerContext | None") -> ToolPolicy:
        """This request's exclusion set AND whether the policy was readable.

        The enforcement seam: the ``tools/call`` branch needs the second half
        to tell "the operator excluded nothing" apart from "we could not read
        what the operator excluded".

        The caller block's token rides along explicitly. This runs on the read
        loop before the worker installs the caller ContextVar, so the resolver
        cannot find the token there; a pooled backend has it nowhere else.
        """
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
                # ``rewriter.UNPOOLABLE_SERVERS``, which is empty and documents
                # exactly that). So NOT advertising does not buy a per-session
                # spawn -- it buys a shared process whose dispatch-loop caller
                # slot never receives gateway-authored metadata, which is how a
                # session-scoped tool silently degrades to unattached behaviour.
                _experimental.update(caller_identity_capability())
            _sends_inflight = reports_inflight and platform_compat.IS_POSIX
            if _sends_inflight:
                # Only a server that opted in advertises, and only where the
                # loop can send a report mid-call. The synchronous Windows path
                # cannot, and a gateway that believed it could would apply the
                # short recycle ceiling to calls this process has no way to
                # vouch for.
                _experimental[INFLIGHT_CAPABILITY_KEY] = {}
            if _experimental:
                _caps["experimental"] = _experimental
            _client_wants_inflight = _sends_inflight and _declares_inflight_capability(params)
            respond(
                req_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": _caps,
                    "serverInfo": {"name": server_name, "version": server_version},
                },
            )
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
            elif not platform_compat.IS_POSIX:
                # Windows: select.select() cannot poll sys.stdin (WinError
                # 10038), so no worker-thread interleave — dispatch the tool
                # synchronously exactly as the pre-worker loop did, a deferred
                # one driven to completion inline (no loop timer exists here).
                # Exception handling mirrors the worker path: the client gets
                # an Error response and the failure is SEL-audited with the
                # caller identity (an escaped exception would kill the loop).
                set_current_caller(_caller_ctx)
                set_current_tenant_nonce(_tenant_nonce)
                try:
                    result = call_tool_fn(tool_name, tool_args)
                    result_text = (
                        drive_deferred(result) if isinstance(result, DeferredTool) else result
                    )
                except Exception as exc:
                    result_text = f"Error: {neutralize_markers(str(exc))}"  # not a directive: see call_tool_with_logging
                    _sel_audit(
                        "failed",
                        tool_name,
                        req_id,
                        _caller_ctx.session_key if _caller_ctx else "",
                    )
                finally:
                    set_current_caller(None)
                    set_current_tenant_nonce("")
                respond(req_id, _tool_response(result_text))
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

    def _refuse_table_full(entry: _DeferredEntry) -> None:
        """Turn away a call the parked table has no room for: retryable."""
        _cancel_quietly(entry.deferred, entry.req_id, entry.caller_ctx, entry.tenant_nonce)
        _sel_audit("failed", entry.tool_name, entry.req_id, entry.session_key)
        respond(
            entry.req_id,
            None,
            error={"code": -32000, "message": "Server busy: parked-call table is full; retry"},
        )

    def _abandon_or_refuse(
        entry: _DeferredEntry, refuse: Callable[[], None], *, consult: bool = True
    ) -> None:
        """Answer a call the loop will never step again (not in ``_deferred``).

        A call that already did something a retry would repeat -- the children
        a spawn POSTed keep running -- says so through its ``abandon`` hook and
        is answered with that final text, settled like any other answer;
        otherwise ``refuse`` gives the caller's own refusal.
        """
        text = _abandon_quietly(entry) if consult else None
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
                        _abandon_or_refuse(entry, functools.partial(_answer_failed, entry, exc))
                        continue
                entry.step_queued = now
                entry.step_started = 0.0  # queued; _run_step stamps the start
                entry.stepping = _step_pool.submit(_run_step, entry)
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
        if entry.stepping is not None:
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
            # A call that already did something a retry would repeat (the
            # children a spawn POSTed keep running) answers with its own final
            # text instead of "retry", and is audited as the call it settled as.
            # One the client already cancelled wants no answer from it.
            _abandon_or_refuse(
                entry, functools.partial(_refuse_pruned, entry), consult=not client_cancelled
            )

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

    try:
        while True:
            worker_busy = _worker_thread is not None and _worker_thread.is_alive()
            if not worker_busy and _worker_thread is not None:
                # Worker just finished: deliver (or register a deferred).
                _deliver_finished_worker(0.1)
            req: dict[str, Any] | _Skipped | None
            if not worker_busy and _pending_calls:
                # Dispatch a queued tools/call (FIFO) before reading new input.
                req = _pending_calls.popleft()
            elif worker_busy or _deferred:
                # Something is in flight: poll stdin with a short timeout so
                # the loop can also deliver the worker, step parked calls and
                # answer pings. A line already buffered is read first: select
                # cannot see it.
                _service_deferred()
                _maybe_report_inflight()
                readable = (
                    _stdin_holds_a_line(sys.stdin) or select.select([sys.stdin], [], [], 0.1)[0]
                )
                if not readable:
                    if worker_busy and _result_ready.is_set():
                        _deliver_finished_worker(1.0)
                    continue
                req = _read_message(sys.stdin)
                if req is None:
                    # EOF: wait for worker then exit. Parked calls die with the
                    # client that owned them; the ``finally`` below cancels
                    # their queued steps so no worker runs one for a client
                    # that is gone, then shuts the pool down.
                    if _worker_thread:
                        _worker_thread.join(timeout=5.0)
                    break
                if isinstance(req, _Skipped):
                    # One iteration per dropped frame, so a finished worker's
                    # result is delivered before the next blocking read.
                    continue
            else:
                req = _read_message(sys.stdin)
                if req is None:
                    break
                if isinstance(req, _Skipped):
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
                    # Other messages while busy: drop gracefully. Notifications are
                    # fine to drop; initialize/initialized never arrive mid-tool.
                    elif method == "tools/list" and req_id is not None:
                        respond(req_id, {"tools": _listable_tools(_req_caller(params))})
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
