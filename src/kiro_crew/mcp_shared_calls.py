"""A tool call's life: ``DeferredTool`` and its drivers, the step pool, cancellation, and the
audited call wrapper.

Holds the bounds on queued, parked and cancelled calls, ``DeferredTool`` (a tool that
only waits returns one instead of blocking the worker) with ``drive_deferred`` and
``map_deferred``, the pool that runs a deferred tool's steps and the entry that tracks
one parked call, ``ToolCancelled``, and ``call_tool_with_logging``, which writes every
call's SEL audit record.

Split out of ``kiro_crew.mcp_shared``, which stays the import path and the patch target: every
name here is reached as ``kiro_crew.mcp_shared.<name>``, and a patch through that facade
reaches this module (``kiro_crew._facade_mirror``). Nothing here imports the facade.
New tool-call work goes here.
"""

from __future__ import annotations

import collections
import concurrent.futures
import json
import queue
import threading
import time
from typing import Any, Callable, Optional, overload

from kiro_crew.mcp_caller import INFLIGHT_CAPABILITY_KEY, CallerContext
from kiro_crew.sel import sel
from kiro_crew.session_directive import neutralize_markers
from kiro_crew.validation import ValidationError

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
    # Both are bounded: completion sites discard ids from the set only, so the
    # deque needs its own cap or it grows one stale entry per cancel. Protected
    # ids are never evicted; if only they remain we stop, since a bounded
    # overflow beats executing a cancelled call.
    while len(cancelled_ids) > cap and order:
        if not _evict_oldest_evictable(cancelled_ids, order, protected):
            break
    while len(order) > cap:
        if not _evict_oldest_evictable(cancelled_ids, order, protected):
            break


class DeferredTool:
    """A tool call whose remaining work is a sequence of SHORT steps.

    The loop has one worker thread per server, so a tool that sleeps (``wait`` up
    to 1800s) would block every caller behind it, on pooled ``kirocrew-core`` every
    dashboard session. A handler that only waits returns one of these: the worker
    frees at once, and the loop runs :meth:`step` whenever :meth:`due_at` passes,
    until a step returns text.

    A ``step`` is short (one loopback HTTP round-trip, never a sleep), runs with the
    call's caller identity installed, and may raise :class:`ToolCancelled` to end
    the call with no response. Steps run on a shared pool, so a stalled step never
    blocks ``ping`` answers or the worker. Outside the loop (tests, CLI)
    :func:`drive_deferred` runs the steps inline.
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

    def progress_phase(self) -> str:
        """What the call is doing now, in a word, for its MCP progress heartbeat.

        Read on the dispatch thread every :data:`PROGRESS_HEARTBEAT_SECS` while the
        call is parked and progressing. The default is the tool's own ``phase``
        attribute when it keeps one, else ``"running"``; a tool overrides it when
        it has a better word (``"waiting_for_answer"``).
        """
        phase = getattr(self, "phase", "")
        return phase if isinstance(phase, str) and phase else "running"

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

    def on_answered(self, written: bool) -> None:
        """Hook run once, AFTER the driver wrote the answer that carries the text
        :meth:`on_settled` was handed.

        ``written`` is True only when the whole frame reached the client's own pipe:
        False on a torn write, and always False on a pooled backend, whose broker
        relay confirms nothing. Mark a result delivered here, and only on True.

        Runs on the driver's thread with the caller identity installed and blocks
        it, so send at most one short request. Calls that end any other way
        (cancelled, refused, failed) never hear this.
        """
        return None


def drive_deferred(deferred: DeferredTool, *, clock: Any = time) -> str:
    """Run ``deferred`` to completion inline, sleeping between steps.

    ``clock`` is a module-like object with ``monotonic`` and ``sleep``, so a
    caller can hand in the one its tests patch (``mcp_core.time``). Nothing
    can cancel an inline drive, so the settling text is committed at once. The
    text is returned to the caller itself, so :meth:`DeferredTool.on_answered`
    hears True; an ``on_settled`` that raises propagates first and nothing
    hears it.
    """
    while True:
        result = deferred.step()
        if result is not None:
            deferred.on_settled(result)
            deferred.on_answered(True)
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

    def progress_phase(self) -> str:
        return self._inner.progress_phase()

    def abandon(self) -> str | None:
        text = self._inner.abandon()
        return None if text is None else self._fn(text)

    def on_settled(self, text: str) -> None:
        self._inner.on_settled(text)

    def on_answered(self, written: bool) -> None:
        self._inner.on_answered(written)


#: How often the loop reports in-flight deferred ids while any exist
#: (``INFLIGHT_NOTIFICATION``, sent only to a client that declared
#: ``INFLIGHT_CAPABILITY_KEY``; both live in ``mcp_caller``). Well
#: under the gateway's ``HEARTBEAT_TIMEOUT_SECS`` (300s) so a report is never
#: late enough for a healthy call to cross the slow-request threshold.
INFLIGHT_REPORT_SECS = 30.0

#: How often a parked call that asked for progress (its ``tools/call`` carried a
#: ``_meta.progressToken``) gets an MCP ``notifications/progress`` naming its
#: current phase. A long tool's heartbeat: well inside every per-call silence
#: bound a client keeps (the session handle's shortest is 5 min), so a call that
#: is only waiting never reads as one that was lost.
PROGRESS_HEARTBEAT_SECS = 30.0

#: A deferred call whose current step has run longer than this is NOT reported
#: as progressing: a step is an HTTP round-trip, so one that holds a pool
#: thread for a minute is the stall the report exists to expose.
DEFERRED_STEP_STUCK_SECS = 60.0

#: Threads the loop runs deferred steps on. A ``wait`` step is a millisecond
#: keepalive, but an ``ask_question`` step holds the server's fixed
#: ``ASK_WAIT_SLICE_SECS`` long-poll, a parent's resume probe is held server-side
#: up to 8s and a ``spawn_sub_agents`` pass waits on its children for up to 2s (in
#: at most half of these threads at once), so the pool is sized
#: for a few of those in flight at once without starving the quick steps. The
#: bound exists so a gateway that stops answering cannot grow threads without limit.
DEFERRED_STEP_WORKERS = 8


class _StepPool:
    """A fixed set of worker threads, all created UP FRONT, fed by one queue.

    Not ``ThreadPoolExecutor``: it starts threads inside ``submit`` after queueing
    the item, so a failed thread start raises while the item still runs later,
    consuming an answer for a call already failed. Here ``submit`` cannot fail;
    only construction can, with nothing queued. A partly started pool is released
    and construction raises, because ``DEFERRED_PARKED_MAX`` assumes ``workers``
    threads and too few would delay an ask's poll past its answer grace.
    """

    def __init__(self, workers: int, name: str) -> None:
        self._queue: "queue.SimpleQueue[_StepPool._Item | None]" = queue.SimpleQueue()
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

    class _Item:
        """One queued step. A cancelled future's item stays in the queue until
        a worker reaches it, so cancelling drops its ``fn`` and ``args`` here:
        the queue then holds an empty shell, not the step's call and payload."""

        __slots__ = ("fut", "fn", "args")

        def __init__(self, fn: Callable[..., Any], args: tuple[Any, ...]) -> None:
            self.fut: "concurrent.futures.Future[Any]" = concurrent.futures.Future()
            self.fn: "Callable[..., Any] | None" = fn
            self.args: tuple[Any, ...] = args
            self.fut.add_done_callback(self._release_if_cancelled)

        def _release_if_cancelled(self, fut: "concurrent.futures.Future[Any]") -> None:
            if fut.cancelled():
                self.fn = None
                self.args = ()

    def submit(self, fn: Callable[..., Any], *args: Any) -> "concurrent.futures.Future[Any]":
        """Queue ``fn(*args)``; never starts a thread, so it never raises."""
        item = self._Item(fn, args)
        self._queue.put(item)
        return item.fut

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fut, fn, args = item.fut, item.fn, item.args
            if not fut.set_running_or_notify_cancel():
                continue
            # Only a cancel clears ``fn``, and a cancelled future never runs.
            assert fn is not None
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
    a cancelled request gets no response. ``tool_call_id`` is the harness call
    the request was approved under, installed for each step as it was for the
    worker; ``progress_token`` is the request's own ``_meta.progressToken``
    (None when it sent none), and ``progress_sent`` / ``next_progress`` pace the
    progress heartbeat (:data:`PROGRESS_HEARTBEAT_SECS`).
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
        "tool_call_id",
        "progress_token",
        "progress_sent",
        "next_progress",
    )

    def __init__(
        self,
        req_id: Any,
        tool_name: str,
        caller_ctx: "CallerContext | None",
        tenant_nonce: str,
        deferred: DeferredTool,
        *,
        tool_call_id: str = "",
        progress_token: Any = None,
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
        self.tool_call_id = tool_call_id
        self.progress_token = progress_token
        self.progress_sent = 0
        self.next_progress = time.monotonic() + PROGRESS_HEARTBEAT_SECS

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


#: The MCP progress notification a parked call's heartbeat is sent as.
PROGRESS_NOTIFICATION = "notifications/progress"
#: Longest phase word a progress heartbeat carries.
PROGRESS_PHASE_MAX_CHARS = 64


def request_progress_token(params: dict[str, Any]) -> Any:
    """A ``tools/call`` request's own ``_meta.progressToken``, or None.

    MCP lets a progress notification name only a token its request carried, a
    string or an integer; anything else is no token, and the call then gets no
    progress heartbeat.
    """
    meta = params.get("_meta")
    token = meta.get("progressToken") if isinstance(meta, dict) else None
    if isinstance(token, bool) or not isinstance(token, (str, int)):
        return None
    return token


def progress_heartbeat(entry: _DeferredEntry, now: float) -> dict[str, Any] | None:
    """The ``notifications/progress`` params *entry* is due at *now*, or None.

    Every :data:`PROGRESS_HEARTBEAT_SECS` a parked call that asked for progress
    gets one: its own token, a count that only grows, and its current phase as
    the message. A long tool's heartbeat, so a client that times calls by their
    silence (the session handle's per-call timer, an SDK that resets its timer
    on progress) does not read a call that is only waiting as one that was lost.
    A call whose step is stuck (:meth:`_DeferredEntry.progressing`) or whose
    cancel landed is not vouched for, and skips that beat.
    """
    if entry.progress_token is None or now < entry.next_progress:
        return None
    entry.next_progress = now + PROGRESS_HEARTBEAT_SECS
    if entry.cancelled or not entry.progressing(now):
        return None
    entry.progress_sent += 1
    try:
        phase = entry.deferred.progress_phase()
    except Exception:  # noqa: BLE001 - a phase is a label, never the call's fate
        phase = "running"
    return {
        "progressToken": entry.progress_token,
        "progress": entry.progress_sent,
        "message": str(phase)[:PROGRESS_PHASE_MAX_CHARS],
    }


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


# Tracebacks and logs name it by the import path its callers use.
ToolCancelled.__module__ = "kiro_crew.mcp_shared"


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

    def progress_phase(self) -> str:
        return self._inner.progress_phase()

    def abandon(self) -> str | None:
        return self._inner.abandon()

    def on_settled(self, text: str) -> None:
        self._inner.on_settled(text)
        self._audit(text)

    def on_answered(self, written: bool) -> None:
        self._inner.on_answered(written)
