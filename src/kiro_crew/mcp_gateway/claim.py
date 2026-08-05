"""Claim-push: the gateway tells gatewayd which session owns a runtime.

The claim event ("session S now owns kiro-cli PID P") is born in the main
gateway process at warm-pool ``rekey()`` time — with both the session key and
the runtime PID in hand. Before this module, that information reached gatewayd
only indirectly: the dashboard wrote ``session_pid_<pid>.txt`` and each stub
polled for it (``stub._recaller_loop``) with a bounded budget. Pool processes
claimed after the budget expired stayed identity-less for life (empty
``_meta.caller`` on every forwarded call → ``spawn_run`` records an empty
``parent_session`` → subagent completions fall back to notification-only).

Claim-push inverts the flow: the knower pushes. On ``rekey()`` the gateway
sends a one-shot ``claim`` frame over the gatewayd unix socket::

    {"type": "claim", "pid": <kiro_pid>, "caller": {"session_key": ..., ...}}

gatewayd applies it to every live stub connection whose ``parent_pid`` matches
(see ``gatewayd`` claim handling). Unlike the stub-initiated ``recaller``
frame — which is deny-by-default and must never overwrite an existing
identity — the claim frame originates from the gateway itself (same uid-gated
0700 socket trust level as Register), so it is allowed to REPLACE a stale
identity. That is what keeps the caller correct across warm-pool re-claims.

The stub-side recaller poll is kept as a fallback for gatewayd-restart races,
but claim-push is the primary, event-driven path.

Import-light on purpose: imported from ``acp/client.py`` and
``acp/session_provider.py`` (hot paths) and must not pull in config loading.
The only non-stdlib import is ``mcp_gateway.transport``, whose whole chain
is ``transport -> platform_compat -> executors`` and reaches no config
loader; it is needed because the endpoint is a unix socket on POSIX and a
named pipe on Windows, and this module must not know which.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

from kiro_crew.mcp_gateway import transport

logger = logging.getLogger(__name__)

#: Aggregate bound on the ENTIRE claim round-trip (connect + write + read-ack)
#: so a wedged gatewayd can never stall a claim task past this budget (the
#: task is fire-and-forget, but leaked tasks pile up).
_CLAIM_TIMEOUT_SECS = 5.0


def classify_session_type(session_key: str) -> str:
    """Session-type label for a caller block. Mirrors ``stub._build_caller_block``."""
    if session_key.startswith("cron:"):
        return "cron"
    if session_key.startswith("hook:"):
        return "hook"
    if session_key.startswith("dashboard:"):
        return "dashboard"
    if session_key:
        return "slack-thread"
    return "unknown"


def build_claim_frame(pid: int, session_key: str, channel_id: Optional[str]) -> dict:
    """Assemble the one-shot claim frame sent to gatewayd."""
    return {
        "type": "claim",
        "pid": pid,
        "caller": {
            "session_key": session_key,
            "session_type": classify_session_type(session_key),
            "principal_id": os.environ.get("KIROCREW_PRINCIPAL")
            or os.environ.get("USER")
            or "",
            "channel_id": channel_id or "",
        },
    }


async def _send_claim_inner(
    socket_path: str,
    pid: int,
    session_key: str,
    channel_id: Optional[str],
) -> bool:
    """Unbounded socket round-trip; ``send_claim`` enforces the time budget."""
    frame = build_claim_frame(pid, session_key, channel_id)
    reader, writer = await transport.connect(socket_path)
    try:
        writer.write(json.dumps(frame).encode("utf-8") + b"\n")
        await writer.drain()
        raw = await reader.readline()
        resp = json.loads(raw.decode("utf-8")) if raw else {}
        ok = isinstance(resp, dict) and resp.get("type") == "claimed"
        if ok:
            logger.info(
                "claim-push acknowledged: pid=%d session_key=%s updated=%s",
                pid, session_key, resp.get("updated"),
            )
        else:
            logger.warning(
                "claim-push not acknowledged: pid=%d session_key=%s resp=%r",
                pid, session_key, resp,
            )
        return ok
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def send_claim(
    socket_path: str,
    pid: int,
    session_key: str,
    channel_id: Optional[str] = None,
) -> bool:
    """Send one claim frame to gatewayd. Returns True when acknowledged.

    The whole round-trip (connect + write + read-ack) runs under a single
    aggregate ``asyncio.wait_for`` bound of ``_CLAIM_TIMEOUT_SECS`` — a wedged
    gatewayd that accepts connections but stalls on responding can never hold
    a claim task past the budget.

    Best-effort by design: a missing socket, dead gatewayd, or timeout logs
    at warning and returns False — the stub-side recaller poll remains as the
    fallback repair path, and a claim retry happens naturally on the next
    rekey.
    """
    try:
        return await asyncio.wait_for(
            _send_claim_inner(socket_path, pid, session_key, channel_id),
            timeout=_CLAIM_TIMEOUT_SECS,
        )
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning(
            "claim-push failed (recaller fallback still applies): pid=%d "
            "session_key=%s: %s", pid, session_key, exc,
        )
        return False


def schedule_claim(
    socket_path: Optional[str],
    pid: Optional[int],
    session_key: str,
    channel_id: Optional[str] = None,
) -> None:
    """Fire-and-forget claim push. Safe to call from sync code (``rekey()``).

    No-ops quietly when preconditions are missing (no gateway socket
    configured, unknown pid, empty session key, or no running event loop) —
    every one of those cases is a legitimate non-gateway or test context
    where claim-push simply does not apply.
    """
    if (
        not socket_path
        or not isinstance(socket_path, str)
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 1
        or not session_key
    ):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(send_claim(socket_path, pid, session_key, channel_id))
    # Retain a reference so the task is never GC'd mid-flight; discard on done.
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


_PENDING: set["asyncio.Task[bool]"] = set()
