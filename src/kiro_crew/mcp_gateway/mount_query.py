"""Ask gatewayd which of one session's broker stubs reached it.

The client half of the ``session-mounts`` control frame
(``daemon/control.py::_session_mounts_reply``). Used by the session-open mount
check (:mod:`kiro_crew.acp.mcp_mount_check`). One round trip per call, bounded
by a timeout, and every failure answers ``None``: a daemon that cannot answer
is not evidence that a stub is missing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import NamedTuple, Optional

from kiro_crew.mcp_gateway import transport

logger = logging.getLogger(__name__)

#: Budget for one connect + write + read.
_QUERY_TIMEOUT_SECS = 3.0
#: Longest server name the daemon's ledger keeps. A name is cut to this on both
#: sides, so a long name still compares equal.
MOUNT_NAME_CAP = 128
#: Most server names the daemon's ledger keeps per session. A session with more
#: stubs than this cannot be checked, on either side.
MOUNT_SERVERS_CAP = 64


class SessionMounts(NamedTuple):
    """What the daemon saw for one session token."""

    servers: frozenset[str]
    #: Wall-clock time the daemon's ledger began.
    since: float


async def _query_inner(socket_path: str, token: str, after: float) -> Optional[SessionMounts]:
    reader, writer = await transport.connect(socket_path)
    try:
        frame = {"type": "session-mounts", "stub_session_token": token, "after": after}
        writer.write(json.dumps(frame).encode("utf-8") + b"\n")
        await writer.drain()
        raw = await reader.readline()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    resp = json.loads(raw.decode("utf-8")) if raw else None
    if not isinstance(resp, dict) or resp.get("type") != "session-mounts":
        # An older daemon has no handler and closes without a reply.
        return None
    servers = resp.get("servers")
    since = resp.get("since")
    if resp.get("complete") is not True:
        # The list may have lost a name that counts (its cap, or this session's
        # own eviction), so it cannot prove any one server absent.
        return None
    if not isinstance(servers, list) or isinstance(since, bool):
        return None
    if not isinstance(since, (int, float)):
        return None
    return SessionMounts(frozenset(s for s in servers if isinstance(s, str)), float(since))


async def query_session_mounts(
    socket_path: str, token: str, after: float = 0.0
) -> Optional[SessionMounts]:
    """The stubs gatewayd saw register under *token*, or ``None`` if it cannot say.

    The token is never logged.
    """
    if not socket_path or not token:
        return None
    try:
        return await asyncio.wait_for(_query_inner(socket_path, token, after), _QUERY_TIMEOUT_SECS)
    except (OSError, asyncio.TimeoutError, ValueError, UnicodeDecodeError) as exc:
        logger.debug("session-mounts query failed: %s", type(exc).__name__)
        return None
