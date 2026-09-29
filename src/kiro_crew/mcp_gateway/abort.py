"""Abort-push: the gateway tells gatewayd to cancel in-flight tool calls.

On hard stop (session reset), the gateway sends an ``abort`` frame over the
gatewayd unix socket naming the runtime PID(s) of the killed session::

    {"type": "abort", "pids": [<pid1>, ...], "reason": "session hard-stop"}

gatewayd looks up every live stub connection indexed under those PIDs (via the
same ``_CONN_INDEX`` ancestry index used by claim-push) and emits MCP
``notifications/cancelled`` for each of that caller's in-flight request ids.
This stops tool work that was executing inside pooled backends.

Trust model: same uid-gated 0700 unix socket as Register/Claim — the gateway
is the authority. A valid abort frame always takes effect.

Who may name a process
----------------------
The frame is pid-keyed because gatewayd's connection index is, so a pid reaches
the wire from here and nowhere else. The session layer asks its provider for a
:class:`RuntimeAbortTarget` and hands that straight to :func:`schedule_abort_for`
without opening it: a runtime is one process that may serve several sessions, so
a session that could read a pid off its provider could also attribute that
process to itself, which is the reading this module exists to make unnecessary.
The target renders its own audit label for the same reason -- the caller records
what it asked for without deriving it.

Import-light on purpose: imported from ``session.py`` hot paths. The only
non-stdlib import is ``mcp_gateway.transport`` (chain: ``transport ->
platform_compat -> executors``, no config loader), needed because the
endpoint is a unix socket on POSIX and a named pipe on Windows.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Optional

from kiro_crew.mcp_gateway import transport

logger = logging.getLogger(__name__)

#: Aggregate time budget for the entire abort round-trip.
_ABORT_TIMEOUT_SECS = 5.0

#: Strong refs to fire-and-forget abort tasks (the event loop holds only
#: weak refs, so without this set a GC pass could cancel an in-flight push).
_PENDING: set["asyncio.Task[dict]"] = set()


@dataclass(frozen=True, slots=True)
class RuntimeAbortTarget:
    """One runtime's abort address, opaque to everyone but this module.

    A provider mints one for the runtime serving it and hands it out; the session
    layer carries it to :func:`schedule_abort_for` and never opens it. Minted only
    when the address is complete and routable, so holding one IS the answer to
    "can this runtime be aborted" and a caller has no validation of its own to do.

    One pid, not a tuple: a target addresses ONE runtime, and a runtime is one
    process. The frame is plural because gatewayd accepts several pids in a single
    push, which is the wire's business and not this address's.

    ``audit_label`` exists so the caller can record WHICH runtime it asked about
    without deriving that answer: the target names itself.
    """

    _wire_pid: int
    _socket_path: str

    @classmethod
    def build(cls, pid: object, socket_path: object) -> "RuntimeAbortTarget | None":
        """A target for ``pid`` on ``socket_path``, or ``None`` when unroutable.

        The only constructor, and the only predicate any caller of this module
        needs: ``None`` for a missing socket, a non-integer pid, and any pid at or
        below 1, since pid 1 is init and 0 addresses a process group, so neither
        names a runtime this gateway spawned.

        :func:`schedule_abort` keeps a filter of its own because it guards the WIRE
        for any caller, including one that never held a target. That is a different
        boundary, not a second copy of this decision.
        """
        if not socket_path or not isinstance(socket_path, str):
            return None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
            return None
        return cls(pid, socket_path)

    @property
    def audit_label(self) -> str:
        """How this runtime is named in an audit record."""
        return f"pid={self._wire_pid}"


def schedule_abort_for(
    target: Optional[RuntimeAbortTarget],
    reason: str = "session hard-stop",
) -> None:
    """Fire-and-forget abort push at a runtime named by an opaque target.

    The seam the session layer uses. No-ops on ``None``, which is what a provider
    answers when no runtime of its own is reachable.
    """
    if target is None:
        return
    schedule_abort(target._socket_path, [target._wire_pid], reason)


def build_abort_frame(pids: list[int], reason: str) -> dict:
    """Assemble the one-shot abort frame sent to gatewayd."""
    return {
        "type": "abort",
        "pids": pids,
        "reason": reason,
    }


async def _send_abort_inner(
    socket_path: str,
    pids: list[int],
    reason: str,
) -> dict:
    """Unbounded socket round-trip; ``send_abort`` enforces the time budget."""
    frame = build_abort_frame(pids, reason)
    reader, writer = await transport.connect(socket_path)
    try:
        writer.write(json.dumps(frame).encode("utf-8") + b"\n")
        await writer.drain()
        raw = await reader.readline()
        resp = json.loads(raw.decode("utf-8")) if raw else {}
        if isinstance(resp, dict) and resp.get("type") == "aborted":
            logger.info(
                "abort-push acknowledged: pids=%r cancelled=%s reason=%s",
                pids,
                resp.get("cancelled"),
                reason,
            )
        else:
            logger.warning(
                "abort-push not acknowledged: pids=%r resp=%r",
                pids,
                resp,
            )
        return resp if isinstance(resp, dict) else {}
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def send_abort(
    socket_path: str,
    pids: list[int],
    reason: str = "session hard-stop",
) -> dict:
    """Send one abort frame to gatewayd. Returns the ack dict.

    The whole round-trip runs under a single aggregate timeout so a wedged
    gatewayd can never stall a session reset.

    Best-effort by design: a missing socket, dead gatewayd, or timeout logs
    at warning and returns empty dict.
    """
    try:
        return await asyncio.wait_for(
            _send_abort_inner(socket_path, pids, reason),
            timeout=_ABORT_TIMEOUT_SECS,
        )
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning(
            "abort-push failed: pids=%r reason=%s: %s",
            pids,
            reason,
            exc,
        )
        return {}


def schedule_abort(
    socket_path: Optional[str],
    pids: list[int],
    reason: str = "session hard-stop",
) -> None:
    """Fire-and-forget abort push. Safe to call from sync or async code.

    No-ops quietly when preconditions are missing (no gateway socket,
    empty pids list, or no running event loop).
    """
    if not socket_path or not isinstance(socket_path, str) or not pids:
        return
    # Filter invalid PIDs
    valid_pids = [p for p in pids if isinstance(p, int) and not isinstance(p, bool) and p > 1]
    if not valid_pids:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(send_abort(socket_path, valid_pids, reason))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
