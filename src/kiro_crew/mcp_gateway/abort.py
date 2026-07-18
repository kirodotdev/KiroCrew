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

Stdlib-only on purpose: imported from ``session.py`` hot paths.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: Aggregate time budget for the entire abort round-trip.
_ABORT_TIMEOUT_SECS = 5.0

#: Strong refs to fire-and-forget abort tasks (the event loop holds only
#: weak refs, so without this set a GC pass could cancel an in-flight push).
_PENDING: set["asyncio.Task[dict]"] = set()


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
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write(json.dumps(frame).encode("utf-8") + b"\n")
        await writer.drain()
        raw = await reader.readline()
        resp = json.loads(raw.decode("utf-8")) if raw else {}
        if isinstance(resp, dict) and resp.get("type") == "aborted":
            logger.info(
                "abort-push acknowledged: pids=%r cancelled=%s reason=%s",
                pids, resp.get("cancelled"), reason,
            )
        else:
            logger.warning(
                "abort-push not acknowledged: pids=%r resp=%r",
                pids, resp,
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
            "abort-push failed: pids=%r reason=%s: %s", pids, reason, exc,
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
