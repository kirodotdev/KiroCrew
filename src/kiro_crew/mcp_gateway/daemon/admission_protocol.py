"""The wire side of admission: how an acquire is waited for and how it is refused.

A queue-aware stub negotiates a wait budget the daemon always gives up inside
(:func:`_negotiated_wait_budget`); while the acquire waits the connection keeps
answering pings and parks everything else within two bounds
(:func:`_await_answering_pings`); and a failed acquire becomes ONE classed
``rejected`` frame (:func:`_classify_rejection`, :func:`_reply_rejected`), where
only a target-shaped class may authorise the stub's own exec.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway.admission import Admission, SpawnGateClosed, SpawnGateTimeout
from kiro_crew.mcp_gateway.daemon import logger
from kiro_crew.mcp_gateway.daemon.launch import _TargetUnknown, resolvable_target_stems
from kiro_crew.mcp_gateway.daemon.wire import _MAX_FRAME_BYTES, _is_ping_frame, _write_json_line
from kiro_crew.mcp_gateway.host_budget import HostBudgetExhausted, HostCharge
from kiro_crew.mcp_gateway.pool import (
    _DEFAULT_READ_BUFFER_LIMIT,
    BackendUnavailable,
    PoolAtCapacity,
    PoolKey,
)

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway import gatewayd as facade
else:
    from kiro_crew.mcp_gateway.daemon import facade


# Aggregate bounds on what ONE connection may park while
# ``_await_answering_pings`` serves an acquire or respawn wait. ``pending`` is
# drained only AFTER that wait returns, so across the default 600s
# ``spawn_queue_wait_secs`` a peer that keeps writing non-ping frames grows
# daemon RSS without limit and takes every co-pooled session down with it --
# the same guard class as ``backend._STUB_INBOX_MAXSIZE`` in the opposite
# direction. Both dimensions are load-bearing: a count bound alone admits
# ``_MAX_PENDING_FRAMES`` x ``_MAX_FRAME_BYTES``, and a byte bound alone leaves
# the per-object overhead of millions of tiny frames unaccounted. 4096 is the
# stub inbox's own number, orders of magnitude above what a real stub parks
# during a wait (one in-flight request, at most a control frame). The byte bound
# follows ``_MAX_FRAME_BYTES`` upward so one frame the reader was willing to
# return can never trip it alone, and is floored at the shipped read limit so
# tuning ``mcp_gateway.read_buffer_limit_bytes`` DOWN (1 KiB is accepted) cannot
# tighten the park along with it.
_MAX_PENDING_FRAMES = 4096
_MAX_PENDING_BYTES = max(_DEFAULT_READ_BUFFER_LIMIT, _MAX_FRAME_BYTES)


# Rejection classes carried on ``rejected`` frames. The stub runs
# ``fallback_exec`` ONLY for ``compat`` (a pooled target this daemon cannot run
# or has no mapping for) and ``isolation`` (a private target this daemon cannot
# serve): both are properties of the target, and the stub's own exec is the
# topology the connection asked for. ``capacity`` covers everything that is a
# property of the HOST or of this moment -- resident pool full, host budget
# exhausted, spawn-gate wait budget spent, breaker OPEN, a fork refused for
# memory or descriptors -- and never falls back, because a per-session exec is
# one more process on the host that just refused one. It carries
# ``retry_after_secs`` instead. "Never" is unconditional on what the stub
# negotiated: a stub that cannot read ``class`` reads the untagged refusal as
# terminal and exits, losing that ONE session's tools, which is the price of
# never handing an at-capacity host an exec nothing can charge.
REJECT_CLASS_CAPACITY = "capacity"
REJECT_CLASS_COMPAT = "compat"
REJECT_CLASS_ISOLATION = "isolation"

# Hint on a ``capacity`` rejection: when the stub may try again.
_CAPACITY_RETRY_AFTER_SECS = 30

# Spawn-gate wait for a stub that did NOT negotiate ``spawn_queue`` (and for the
# legacy lazy-spawn path). Such a stub gives up on its own after 25 s and falls
# back to a per-session exec, so a longer daemon-side wait would only spawn a
# backend nobody attaches to. The wait still happens INSIDE the gate, so old
# stubs obey the global spawn bound; what they cannot get is the queue.
_LEGACY_SPAWN_WAIT_SECS = 20.0

# How much of a queue-aware stub's own budget the daemon leaves itself to answer
# in. The stub starts its timer before it writes ``ensure_backend`` and the
# daemon starts its own only after reading the frame, so an EQUAL budget expires
# on the stub first -- and a stub whose budget expires runs ``fallback_exec``,
# the unaccounted per-session exec a ``capacity`` refusal exists to withhold. The
# refusal therefore has to be raised, written and read while the stub is still
# waiting, which is what the margin buys. 5 s is the ``_LEGACY_SPAWN_WAIT_SECS``
# figure against the same 25 s pre-flight, and the fractional cap keeps the
# inequality strict for a stub that asks for less than the margin: half of a tiny
# budget is still orders of magnitude above one local socket round trip.
_QUEUE_REFUSAL_MARGIN_SECS = 5.0

# ``errno`` values on a spawn failure that describe the HOST being out of
# something a fallback exec would also need. Anything else on an OSError is
# specific to this daemon's launch environment (a missing binary, a permission)
# and stays fallback-eligible.
_PRESSURE_ERRNOS = frozenset({errno.ENOMEM, errno.EAGAIN, errno.EMFILE, errno.ENFILE, errno.ENOSPC})
#: Every acquire failure that means the HOST or the moment, never the target. Held
#: equal to what ``_classify_rejection`` tests, the same way ``_PRESSURE_ERRNOS`` is
#: held equal to the errnos it calls pressure: a sixth member added to one and not
#: the other would reach ``capacity`` with nothing asserting it authorises no exec.
_CAPACITY_FAILURES: tuple[type[BaseException], ...] = (
    PoolAtCapacity,
    HostBudgetExhausted,
    SpawnGateTimeout,
    SpawnGateClosed,
    BackendUnavailable,
)


class _PeerGone(RuntimeError):
    """The stub's connection ended while the daemon was still acquiring a
    backend for it. Nothing to reply to; the handler simply returns."""


def _negotiated_wait_budget(msg: dict[str, Any], admission: Optional[Admission]) -> Optional[float]:
    """The FIFO wait a queue-aware stub asked for, or ``None`` for an old stub.

    A stub that advertises nothing sends a bare ``ensure_backend``; one that
    negotiated ``spawn_queue`` sends a finite, positive ``wait_budget_secs``.
    Anything else -- absent, non-numeric, non-finite, zero or negative -- is
    treated as an old stub, because the protocol contract is that only a stub
    that will consume ``queued`` frames ever asks for them. The daemon's own
    ``spawn_queue_wait_secs`` caps the answer; a stub can only shorten it.

    The result is always STRICTLY shorter than what the stub asked for, by
    ``_QUEUE_REFUSAL_MARGIN_SECS`` where there is room for it: the daemon has to
    give up first, or the stub gives up first and execs its own backend on a host
    the daemon was about to refuse one for. Equal budgets are not a tie -- the
    stub's clock starts earlier -- so the shipped defaults (600 s on both sides)
    are the case the margin is for, not an exotic one.
    """
    if admission is None:
        return None
    raw = msg.get("wait_budget_secs")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    budget = float(raw)
    if budget != budget or budget <= 0 or budget == float("inf"):
        return None
    capped = min(budget, admission.spawn_queue_wait_secs)
    return capped - min(_QUEUE_REFUSAL_MARGIN_SECS, capped / 2.0)


@dataclass(frozen=True)
class _Rejection:
    """How an acquire failure is reported to the stub.

    ``fallback`` authorises the stub to exec the backend itself, so it rides
    the TARGET-shaped classes (``compat``/``isolation``) only, whatever the
    stub negotiated. A ``capacity`` refusal never carries it, because that exec
    is one more process on a host the daemon just refused one for and the
    daemon cannot account for it: a stub that never negotiated ``spawn_queue``
    closes its socket BEFORE exec'ing, so the charge :func:`_reply_rejected`
    takes is released at that EOF -- before the process it pays for exists --
    and N simultaneous refusals leave N backends the budget never sees.
    :func:`_capacity_rejection` is the only constructor for the class for that
    reason.

    The compatibility cost falls on the pre-``spawn_queue`` stub alone, which
    cannot read ``class`` and therefore reads an untagged rejection as
    terminal: it exits, and that ONE session loses that server's tools until
    the retry. Bounded, and recorded as a ``terminal:`` line in
    ``stub_fallback.jsonl`` for the operator; a stub that did negotiate
    ``spawn_queue`` answers kiro-cli the typed ``-32001`` instead and keeps its
    transport. Deliberately preferred over an exec nothing bounds -- see
    ``docs/architecture/mcp.md``.
    """

    cls: str
    fallback: bool
    retry_after_secs: Optional[int] = None

    def frame(self, reason: str) -> dict[str, Any]:
        frame: dict[str, Any] = {"type": "rejected", "reason": reason, "class": self.cls}
        if self.fallback:
            frame["fallback"] = True
        if self.retry_after_secs is not None:
            frame["retry_after_secs"] = self.retry_after_secs
        return frame


def _capacity_rejection(retry_after_secs: int = _CAPACITY_RETRY_AFTER_SECS) -> _Rejection:
    """A refusal about the HOST or the moment: classed ``capacity``, no fallback.

    The only ``_Rejection`` of this class, so the one field that must never be
    ``True`` beside it has exactly one place it is written. Not the only place a
    ``class: capacity`` FRAME is written -- the lazy-spawn arm composes one
    directly, with no ``fallback`` key at all -- so an audit of "where can a
    capacity frame gain a tag?" has two sites to read, not one. Why that is a
    security property and what it costs a pre-upgrade stub: :class:`_Rejection`.
    """
    return _Rejection(REJECT_CLASS_CAPACITY, fallback=False, retry_after_secs=retry_after_secs)


def _classify_rejection(exc: BaseException, *, exclusive: bool) -> Optional[_Rejection]:
    """Map an acquire failure to its rejection class, or ``None`` for an
    internal error the stub must be told is terminal.

    See ``REJECT_CLASS_*`` for what each class means. The target-shaped
    failures (no mapping, a launch the daemon cannot perform for reasons that
    are not host pressure) are fallback-eligible; everything about the host or
    the moment is ``capacity``, and no ``capacity`` answer authorises an exec on
    any wire shape. What the stub negotiated is therefore not an input here --
    it selects the ``queued`` keepalives and the wait budget, never whether a
    refusal may fork.
    """
    if isinstance(exc, _TargetUnknown):
        return _Rejection(REJECT_CLASS_COMPAT, fallback=True)
    if isinstance(exc, _CAPACITY_FAILURES):
        if isinstance(exc, SpawnGateClosed):
            retry_after_secs = 5
        elif isinstance(exc, BackendUnavailable):
            retry_after_secs = 60
        else:
            retry_after_secs = _CAPACITY_RETRY_AFTER_SECS
        return _capacity_rejection(retry_after_secs)
    if isinstance(exc, OSError):
        # An errno in ``_PRESSURE_ERRNOS`` says the HOST is out of what an exec
        # would need too, so it is capacity; any other errno is this daemon's
        # own launch environment, which the stub's exec may well not share.
        if exc.errno in _PRESSURE_ERRNOS:
            return _capacity_rejection()
        return _Rejection(
            REJECT_CLASS_ISOLATION if exclusive else REJECT_CLASS_COMPAT, fallback=True
        )
    return None


async def _reply_rejected(
    writer: asyncio.StreamWriter,
    reader: asyncio.StreamReader,
    verdict: _Rejection,
    *,
    reason: str,
    caller: Optional[CallerContext],
    pool_key: PoolKey,
    admission: Optional[Admission],
) -> None:
    """Send a ``rejected`` frame and, for a fallback, charge the exec it causes.

    A fallback-eligible rejection turns into one more MCP process on this host
    -- the stub's per-session exec -- that the daemon never spawned and would
    otherwise never count. It is charged to the host budget HERE, before the
    frame goes out, and the charge is held until the connection reaches EOF,
    which a stub that negotiated ``spawn_queue`` makes meaningful by keeping its
    socket inheritable across the exec: that EOF is then the exec'd backend
    exiting. A stub that did not negotiate it closes before it execs, so the
    charge would cover nothing -- which is exactly why the classes it CANNOT
    read (:func:`_capacity_rejection`) authorise no exec at all, leaving only
    ``compat``/``isolation`` here, where an exec is the topology the connection
    asked for and refusing it would strand the session with no server. When the
    budget cannot take the charge the answer is a ``capacity`` rejection
    instead, because the fallback would be the very process the budget is
    refusing.
    """
    session_key = caller.session_key if caller else ""
    label = pool_key.human_readable()
    charge: Optional[HostCharge] = None
    if verdict.fallback and admission is not None:
        try:
            charge = admission.budget.reserve(label=label, kind="fallback")
        except HostBudgetExhausted as budget_exc:
            logger.info(
                "fallback for %s refused: %s -- answering capacity instead",
                label,
                budget_exc,
            )
            verdict = _capacity_rejection()
            reason = f"{reason}; {budget_exc}"
    if verdict.fallback:
        facade._audit_pool_fallback(session_key, label, reason)
    else:
        facade._audit_pool_rejected(session_key, label, f"{verdict.cls}: {reason}")
    await _write_json_line(writer, verdict.frame(reason))
    if charge is None:
        return
    try:
        # Hold the charge for as long as the peer holds the socket. Frames a
        # stub might still send (none are expected) are read and dropped.
        while True:
            try:
                line = await reader.readuntil(b"\n")
            except (
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
                ConnectionError,
                OSError,
            ):
                return
            if not line:
                return
    finally:
        charge.release()


async def _await_answering_pings(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    pending: "deque[bytes]",
    coro: Any,
    *,
    stub_uuid: str,
) -> Any:
    """Run ``coro`` (an acquire or respawn) without deafening the connection.

    A backend acquire can now wait minutes in the spawn gate, and the stub's
    bridge liveness monitor pings every 10 s while it has a call outstanding
    (a respawn always does). The connection handler is a sequential reader, so
    awaiting the acquire inline would leave those pings unread and the stub
    would declare a perfectly healthy daemon dead after three of them. This
    runs the acquire as a task and keeps reading: a ping gets its pong at once,
    every other complete frame is parked in ``pending`` for the main loop to
    process in order once the acquire returns.

    Peer EOF or a transport error while the acquire is still running cancels
    it (its admission is released by the spawn path) and raises
    :class:`_PeerGone`. If the acquire had already completed, its result is
    returned instead so the caller attaches and runs its normal teardown --
    a completed pooled acquire holds a hand-out reservation that only the
    attach path releases.

    Parking is bounded in both dimensions (``_MAX_PENDING_FRAMES`` /
    ``_MAX_PENDING_BYTES``): past either, the same close path an EOF takes drops
    this one connection rather than letting a backlog nothing drains grow for
    the whole wait. The overflowing frame is parked BEFORE the bound is read, so
    no frame is ever dropped while the peer is told nothing.
    """
    work: asyncio.Task[Any] = asyncio.ensure_future(coro)
    # Fast path: a pool reuse or an immediate rejection completes on the first
    # turn of the loop. Nothing is read from the socket then, so a frame the
    # stub sends right after (its next control frame, kiro-cli's first request)
    # is left for the main loop exactly as before.
    await asyncio.wait({work}, timeout=0)
    if work.done():
        return work.result()
    # Seeded, not zeroed: a ``BackendGone`` on the forward of a frame the main
    # loop popped re-enters here with the rest of a previous invocation's park
    # still in ``pending``. That residue is itself bound-limited, so the sum is
    # O(n) once over an n the bound already governs.
    parked_bytes = sum(map(len, pending))
    read_task: Optional[asyncio.Task[bytes]] = None
    try:
        while True:
            if read_task is None:
                read_task = asyncio.create_task(reader.readuntil(b"\n"))
            done, _ = await asyncio.wait({work, read_task}, return_when=asyncio.FIRST_COMPLETED)
            if read_task in done:
                try:
                    line = read_task.result()
                except (
                    asyncio.IncompleteReadError,
                    asyncio.LimitOverrunError,
                    ConnectionError,
                    OSError,
                ):
                    read_task = None
                    if work.done() and not work.cancelled() and work.exception() is None:
                        return work.result()
                    work.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await work
                    raise _PeerGone("stub disconnected while its backend was being acquired")
                read_task = None
                if _is_ping_frame(line):
                    try:
                        await _write_json_line(writer, {"type": "pong"})
                    except (OSError, ConnectionError):
                        pass
                else:
                    pending.append(line)
                    parked_bytes += len(line)
                    # ``and not work.done()`` keeps the reservation invariant the
                    # docstring states: on the one turn where both tasks finish,
                    # a completed pooled acquire must reach its attach path, and
                    # the return below stops the park growing anyway.
                    if (
                        len(pending) > facade._MAX_PENDING_FRAMES
                        or parked_bytes > facade._MAX_PENDING_BYTES
                    ) and not work.done():
                        logger.warning(
                            "stub %s parked %d frames / %d bytes during a spawn wait "
                            "(bounds %d / %d); dropping conn",
                            stub_uuid,
                            len(pending),
                            parked_bytes,
                            facade._MAX_PENDING_FRAMES,
                            facade._MAX_PENDING_BYTES,
                        )
                        raise _PeerGone("stub flooded frames while its backend was being acquired")
            if work.done():
                return work.result()
    finally:
        if read_task is not None and not read_task.done():
            # Cancelling ``readuntil`` leaves any partial line in the reader's
            # buffer, so the main loop's next read picks it up whole.
            read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await read_task
        if not work.done():
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await work


async def _refuse_ensure_backend(
    exc: Exception,
    writer: asyncio.StreamWriter,
    reader: asyncio.StreamReader,
    *,
    exclusive: bool,
    caller: Optional[CallerContext],
    pool_key: PoolKey,
    admission: Optional[Admission],
) -> None:
    """Answer an ``ensure_backend`` whose acquire failed; the connection then ends.

    Called from the handler's ``except`` clause, so an internal error is logged
    with its traceback.
    """
    verdict = _classify_rejection(
        exc,
        exclusive=exclusive,
    )
    if verdict is None:
        # Unexpected gateway-internal error (NOT an OS spawn
        # failure) -- terminal, not fallback-eligible: surface
        # it rather than masking a gateway bug behind an
        # unpooled exec on every session.
        logger.exception(
            "ensure_backend internal error for %s",
            pool_key.human_readable(),
        )
        facade._audit_pool_rejected(
            caller.session_key if caller else "",
            pool_key.human_readable(),
            f"internal error: {exc}",
        )
        await _write_json_line(
            writer,
            {"type": "rejected", "reason": f"internal error: {exc}"},
        )
        return
    if isinstance(exc, _TargetUnknown):
        # An unknown target here means THIS DAEMON'S env has
        # no mapping -- which, at the pre-flight, can only be
        # map drift: a stub exists at all only because the
        # rewriter wrapped that server, and the stub is
        # holding the real ``--target-command`` on its own
        # argv. A genuinely unrunnable target fails later,
        # as BackendUnavailable. So this is fallback-ELIGIBLE:
        # no real MCP frame has been forwarded yet, so the
        # stub can exec the target directly and lose nothing
        # but pooling. See
        # docs/architecture/design-notes/mcp-stub-decoupling.md.
        logger.warning(
            "ensure_backend: no target mapping for %s -- this "
            "daemon's target env predates the current "
            "stub_servers set (target map is baked at spawn and "
            "an adopted daemon never re-applies it). Replying "
            "fallback-eligible so the stub degrades to a "
            "per-session exec; pooling and the strict session "
            "key are LOST for this connection. Daemon stems: %s",
            pool_key.human_readable(),
            ",".join(resolvable_target_stems()) or "(none)",
        )
    elif verdict.fallback:
        logger.warning(
            "ensure_backend rejected (%s, fallback-eligible) for %s: %s",
            verdict.cls,
            pool_key.human_readable(),
            exc,
        )
    else:
        logger.info(
            "ensure_backend rejected (%s) for %s: %s",
            verdict.cls,
            pool_key.human_readable(),
            exc,
        )
    await _reply_rejected(
        writer,
        reader,
        verdict,
        reason=(f"backend spawn failed: {exc}" if isinstance(exc, OSError) else str(exc)),
        caller=caller,
        pool_key=pool_key,
        admission=admission,
    )


async def _refuse_lazy_spawn(
    exc: Exception,
    writer: asyncio.StreamWriter,
    *,
    caller: Optional[CallerContext],
    pool_key: PoolKey,
) -> None:
    """Answer a legacy lazy spawn whose acquire failed; the connection then ends.

    Only a pre-``ensure_backend`` stub reaches this, and it has already forwarded a
    real MCP frame, so no refusal here is fallback-eligible. Called from the
    handler's ``except`` clause, so a crash is logged with its traceback.
    """
    if isinstance(exc, _TargetUnknown):
        # Same drift as the pre-flight site, but NOT fallback-tagged:
        # only a pre-ensure_backend stub reaches this path and it has
        # already forwarded a real MCP frame, so an exec fallback
        # would lose that frame. Terminal is correct here -- what was
        # missing is saying so anywhere durable.
        logger.warning(
            "lazy-spawn: no target mapping for %s -- this daemon's "
            "target env predates the current stub_servers set. "
            "Terminal (a real frame was already forwarded, so an "
            "exec fallback would drop it): this server's tools will "
            "be ABSENT for the session. Daemon stems: %s",
            pool_key.human_readable(),
            ",".join(resolvable_target_stems()) or "(none)",
        )
        facade._audit_pool_rejected(
            caller.session_key if caller else "",
            pool_key.human_readable(),
            str(exc),
        )
        await _write_json_line(
            writer,
            {
                "type": "rejected",
                "reason": str(exc),
            },
        )
    elif isinstance(
        exc,
        (
            BackendUnavailable,
            PoolAtCapacity,
            HostBudgetExhausted,
            SpawnGateTimeout,
            SpawnGateClosed,
        ),
    ):
        # Legacy lazy-spawn path: only pre-ensure_backend stubs
        # reach here, and they have already forwarded a real frame,
        # so a fallback exec would lose it — NOT tagged
        # fallback-eligible. New stubs pre-flight via ensure_backend.
        logger.info(
            "lazy-spawn rejected for %s: %s",
            pool_key.human_readable(),
            exc,
        )
        facade._audit_pool_rejected(
            caller.session_key if caller else "",
            pool_key.human_readable(),
            str(exc),
        )
        await _write_json_line(
            writer,
            {
                "type": "rejected",
                "reason": str(exc),
                "class": REJECT_CLASS_CAPACITY,
                "retry_after_secs": _CAPACITY_RETRY_AFTER_SECS,
            },
        )
    else:
        logger.exception("backend spawn failed for %s", pool_key.human_readable())
        facade._audit_pool_rejected(
            caller.session_key if caller else "",
            pool_key.human_readable(),
            f"spawn failed: {exc}",
        )
        await _write_json_line(
            writer,
            {
                "type": "rejected",
                "reason": f"backend spawn failed: {exc}",
            },
        )
