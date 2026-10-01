"""The stub socket's bytes: JSONL framing, bounded writes and keepalive probes.

Every reply the daemon writes on a stub or control connection goes out through
:func:`_write_json_line` (compact JSON, one line, a bounded drain); the backend's
replies reach the stub through :func:`_drain_inbox_to_stub`; and
:func:`_probe_stub_transports` writes the keepalive that turns a half-open stub
transport into an observable error. The stub writers here are the registry the
shutdown drain counts (``gatewayd._counted_stub_write``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any, Optional

from kiro_crew.json_line import parse_json_object_line
from kiro_crew.mcp_gateway.daemon import logger
from kiro_crew.mcp_gateway.pool import READ_BUFFER_LIMIT_BYTES
from kiro_crew.validation import JSONRPC_PARSE_ERROR

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway import gatewayd as facade
else:
    from kiro_crew.mcp_gateway.daemon import facade


# Max bytes accepted for any single stub->gateway frame. Registration
# payloads from the stub are well under 4 KiB, so this is a very loose cap
# that still guards against a malformed or hostile peer blowing memory
# with ``readuntil(b"\n")``.
#
# It is the read-buffer limit: 64 MiB by default, and operator-tunable via
# ``mcp_gateway.read_buffer_limit_bytes`` / ``KIROCREW_MCP_READ_LIMIT``. Anything
# that materializes a frame this size -- a test, a fuzz payload -- allocates tens
# of MiB, so build it inside the function that needs it.
_MAX_FRAME_BYTES = READ_BUFFER_LIMIT_BYTES  # see pool.READ_BUFFER_LIMIT_BYTES


# How long a connection handler waits for the first Register message
# before giving up on an idle client. Keeps the event loop from
# accumulating half-open connections that never send anything.
_REGISTER_TIMEOUT_SECS = 5.0


# Upper bound on a single control/handshake reply's ``drain()`` (pong, stats,
# registered, rejected, ready, forward-error — everything sent via
# ``_write_json_line``). ``_REGISTER_TIMEOUT_SECS`` only bounds the inbound
# first-frame read; without a write bound a same-uid peer that passes the
# handshake then stops reading would pin its handler task for the daemon's
# lifetime. Generous — a peer that cannot accept a small reply in 30s is dead.
_WRITE_REPLY_TIMEOUT_SECS = 30.0


def _is_ping_frame(line: bytes) -> bool:
    # Cheap pre-check before a JSON parse: a ping is a tiny control frame.
    if len(line) > 256 or b"ping" not in line:
        return False
    msg = parse_json_object_line(line)
    return msg is not None and msg.get("type") == "ping"


async def _read_first_frame(reader: asyncio.StreamReader) -> Optional[dict[str, Any]]:
    """Read the first line-delimited JSON object from ``reader``.

    Returns ``None`` on clean EOF before a full line arrives, on malformed
    JSON, or on idle timeout. The caller dispatches on the ``type`` field:
    ``"ping"`` gets a pong reply, ``"register"`` (or no type) starts the
    handshake, anything else is logged and dropped.
    """
    try:
        line = await asyncio.wait_for(
            reader.readuntil(b"\n"),
            timeout=facade._REGISTER_TIMEOUT_SECS,
        )
    except asyncio.IncompleteReadError as exc:
        # Peer closed without a newline — treat as clean disconnect only
        # if we received zero bytes; partial frames are truncation errors.
        if exc.partial:
            logger.warning("stub sent partial first frame (%d bytes)", len(exc.partial))
        return None
    except asyncio.TimeoutError:
        logger.warning(
            "stub idle for %.1fs without first frame; closing", facade._REGISTER_TIMEOUT_SECS
        )
        return None
    except asyncio.LimitOverrunError:
        logger.warning("stub first frame exceeded %d bytes; closing", facade._MAX_FRAME_BYTES)
        return None

    if len(line) > facade._MAX_FRAME_BYTES:
        logger.warning("stub first frame too large: %d bytes", len(line))
        return None

    msg = parse_json_object_line(line)
    if msg is None:
        logger.warning("stub first frame is not a JSON object; closing")
    return msg


async def _write_json_line(writer: asyncio.StreamWriter, obj: Any) -> None:
    """Serialize ``obj`` as one JSON line with a bounded ``drain()``.

    Backpressure (Phase-0 #2): a misbehaving peer that stops reading can
    otherwise let the kernel socket buffer fill silently, deadlocking the
    handler. ``drain()`` yields to the scheduler until the write is
    accepted or the peer's half of the connection drops.

    The drain is bounded by ``_WRITE_REPLY_TIMEOUT_SECS``: ``_REGISTER_TIMEOUT_SECS``
    only wraps the inbound first-frame read, so a same-uid peer that passes the
    handshake then stops reading could otherwise pin this handler task
    indefinitely on the registered/rejected/pong/stats reply.
    """
    payload = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"
    lock = getattr(writer, "_mc_write_lock", None)
    guard: Any = lock if lock is not None else contextlib.nullcontext()
    async with guard:
        writer.write(payload)
        try:
            await asyncio.wait_for(writer.drain(), timeout=facade._WRITE_REPLY_TIMEOUT_SECS)
        except (ConnectionError, asyncio.TimeoutError):
            # Peer hung up or stopped reading mid-reply; nothing productive to do.
            return


def _jsonrpc_error(msg: dict[str, Any], reason: str) -> dict[str, Any]:
    """Return a JSON-RPC 2.0 error envelope mirroring the id of ``msg``.

    Closes the loop when a backend dies mid-forward: the stub sees
    a plain error response under its own id instead of a dangling request.
    """
    return {
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "error": {"code": -32000, "message": reason},
    }


def _jsonrpc_parse_error(req_id: Any) -> dict[str, Any]:
    """The JSON-RPC 2.0 parse-error answer to a frame whose id was recovered."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": JSONRPC_PARSE_ERROR, "message": "Parse error"},
    }


async def _drain_inbox_to_stub(
    inbox: "asyncio.Queue[bytes]",
    writer: asyncio.StreamWriter,
    stub_uuid: str = "",
) -> None:
    """Forward every payload queued by the backend into the stub writer.

    Each payload is already a complete newline-terminated JSON frame built
    by :meth:`Backend._deliver_to_stub`. Exits on writer error (stub
    disconnected) or task cancellation at shutdown.
    """
    lock = getattr(writer, "_mc_write_lock", None)
    try:
        while True:
            payload = await inbox.get()
            guard: Any = lock if lock is not None else contextlib.nullcontext()
            try:
                with facade._counted_stub_write():
                    async with guard:
                        writer.write(payload)
                        await asyncio.wait_for(
                            writer.drain(), timeout=facade._WRITE_REPLY_TIMEOUT_SECS
                        )
            except (ConnectionError, BrokenPipeError):
                # Scope E: log late responses dropped after stub detach
                # instead of letting BrokenPipeError propagate unlogged.
                logger.info(
                    "stub %s: response arrived after disconnect — dropped "
                    "(%d bytes); this is expected during session stop",
                    stub_uuid or "unknown",
                    len(payload),
                )
                return
            except asyncio.TimeoutError:
                # Stub passed the handshake but stopped reading; don't pin this
                # writer task (and its connection handler + fd) indefinitely.
                return
    except asyncio.CancelledError:
        raise


# --- Stub-connection liveness probe -----------------------------------------
#
# A stub whose transport dies without a clean close leaves its connection
# handler parked in ``reader.readuntil()``. The handler's ``finally`` — which
# owns ``detach_stub`` — therefore never runs, the backend's refcount never
# drops, and the idle sweep (which keys on ``refcount == 0``) can never reclaim
# it. Backends then accumulate for the lifetime of the daemon.
#
# The asymmetry that makes this possible: a half-open transport is INVISIBLE to
# a reader and only observable on a WRITE. An idle session performs no writes,
# so the death has no way to surface. ``_drain_inbox_to_stub`` already handles
# the write error correctly — it simply never gets a frame to write.
#
# So the gateway writes one itself. Each sweep sends a reserved control frame
# to every live stub; a dead transport fails that write, and the handler is
# cancelled so its existing teardown runs. Reclamation then follows the normal
# refcount path — detach -> refcount 0 -> idle eviction — rather than a
# separate garbage-collection concept layered on top of it.
#
# This mirrors the gateway<->backend direction, which has carried a heartbeat
# under a reserved id since pooling landed. The gateway<->stub direction was
# the half without one.
#
# Reserved ``type`` field, matching the existing ``ping``/``pong`` control
# frames. The stub consumes it in its gateway->stdout pump and never forwards
# it to kiro-cli. An older stub that does not know the frame passes it through,
# where it is inert: it carries no ``jsonrpc``/``id``/``method``, so an MCP
# client has nothing to dispatch on — the same graceful-degradation property
# the ``pong`` frame already relies on.
STUB_KEEPALIVE_TYPE = "keepalive"

#: Bound on a single keepalive write+drain. A stub that has stopped reading
#: must not pin the sweeper: the drain pump uses the same bound for the same
#: reason. Exceeding it is treated as a dead transport.
_STUB_KEEPALIVE_TIMEOUT_SECS = 5.0


class _StubProbe:
    """A live stub connection's write handle plus its owning handler task.

    Registered for the full lifetime of the connection handler and removed in
    the same ``finally`` that detaches the stub, so the registry can never
    outlive the attachment it describes.
    """

    __slots__ = ("stub_uuid", "writer", "task")

    def __init__(
        self,
        stub_uuid: str,
        writer: asyncio.StreamWriter,
        task: "asyncio.Task[None]",
    ) -> None:
        self.stub_uuid = stub_uuid
        self.writer = writer
        self.task = task


#: Every live stub connection, keyed by identity of the probe record. A set of
#: records (not a dict keyed by stub_uuid) because a reconnecting stub may
#: briefly overlap with its predecessor, and clobbering the old entry would
#: leak the very handler the probe exists to tear down.
_STUB_PROBES: set[_StubProbe] = set()


def _stub_probe_add(probe: _StubProbe) -> None:
    _STUB_PROBES.add(probe)


def _stub_probe_discard(probe: _StubProbe) -> None:
    _STUB_PROBES.discard(probe)


async def _probe_stub_transports() -> int:
    """Write a keepalive to every live stub; cancel the handler of any that
    fails. Returns the number of dead transports found.

    The write is the entire point: it converts a silently half-open transport
    into an observable error. Cancelling the handler is what makes the existing
    teardown run — this function deliberately does NOT touch refcounts or the
    pool itself, so there is exactly one code path that detaches a stub.

    Never raises: a probe failure must not take down the sweeper.
    """
    payload = json.dumps({"type": STUB_KEEPALIVE_TYPE}).encode() + b"\n"
    dead = 0
    for probe in list(_STUB_PROBES):
        if probe.task.done():
            # Handler already exiting; its finally owns the teardown.
            continue
        lock = getattr(probe.writer, "_mc_write_lock", None)
        guard: Any = lock if lock is not None else contextlib.nullcontext()
        try:
            with facade._counted_stub_write():
                async with guard:
                    probe.writer.write(payload)
                    await asyncio.wait_for(
                        probe.writer.drain(),
                        timeout=facade._STUB_KEEPALIVE_TIMEOUT_SECS,
                    )
        except (ConnectionError, BrokenPipeError, asyncio.TimeoutError) as exc:
            dead += 1
            logger.info(
                "stub %s: transport dead on keepalive (%s) — cancelling handler "
                "so the stub detaches and its backend can be reclaimed",
                probe.stub_uuid or "unknown",
                type(exc).__name__,
            )
            probe.task.cancel()
        except Exception:  # pragma: no cover — defensive
            logger.warning(
                "stub %s: keepalive probe raised unexpectedly",
                probe.stub_uuid or "unknown",
                exc_info=True,
            )
    return dead
