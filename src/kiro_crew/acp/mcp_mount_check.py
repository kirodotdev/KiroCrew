"""Session-open MCP mount check for the session-array harnesses.

A harness in ``ACP_BACKENDS_SESSION_MCP_ARRAY`` can drop an ``mcpServers``
element whose command does not start and still create the session (measured on goose 1.50.1,
``test/fixtures/acp_frames/goose/mcp-stdio-dropped-live.jsonl``). Nothing it
sends afterwards tells that session from a healthy one.

Crew can still see the drop for the elements it brokers. Every broker stub
registers with gatewayd as soon as its harness starts it, carrying this
session's stub token, and gatewayd keeps the server names it saw per token. So
after ``session/new`` or ``session/load`` this check asks gatewayd, for a bounded
window, which of the session's stubs arrived. A stub still absent when the window
closes is recorded three ways: a WARNING log line, a security event in the SEL,
and a FAILED row on the session's MCP report.

Three rules keep it honest:

* **Not seen yet is not missing.** Nothing is recorded until the window closes;
  until then the report shows the server as not reported, as for any server.
* **A broker that cannot answer proves nothing.** No socket, no reply, an older
  daemon without the frame, a ledger that began after the session opened or
  evicted this session's entry since, or more stubs than the ledger keeps all end with an INFO line,
  no warning, no event and no row.
* **It never blocks or fails the session.** It runs as a background task after
  the session exists, and every error is caught.

Only broker stubs are checked: an element Crew hands the harness to exec
directly never touches the broker, so the broker cannot vouch for it either way.
The audit record names the backend, the session id and the missing server
names. It never carries the stub token, an env value or a command line.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, NamedTuple, Optional

from kiro_crew.acp.mcp_session_report import NAME_CAP, McpSessionReport, sanitize_sink_text
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_gateway.hashing import expand_stub_flags
from kiro_crew.mcp_gateway.mount_query import (
    MOUNT_NAME_CAP,
    MOUNT_SERVERS_CAP,
    SessionMounts,
    query_session_mounts,
)

logger = logging.getLogger(__name__)

#: How long after the session exists a stub may take to reach the broker.
MOUNT_WINDOW_SECS = 30.0
#: Pause between two asks inside the window.
MOUNT_POLL_SECS = 2.0
#: Slack on the ledger-start comparison, for clock steps between processes.
_SINCE_SLACK_SECS = 1.0

_EVENT_TYPE = "mcp_session_mount_missing"

Query = Callable[[str, str, float], Awaitable[Optional[SessionMounts]]]

_PENDING_TASKS: set[asyncio.Task[None]] = set()


def _env_token(element: dict[str, Any]) -> str:
    env = element.get("env")
    if isinstance(env, list):
        for pair in env:
            if isinstance(pair, dict) and pair.get("name") == STUB_SESSION_TOKEN_ENV:
                value = pair.get("value")
                return value if isinstance(value, str) else ""
    elif isinstance(env, dict):
        value = env.get(STUB_SESSION_TOKEN_ENV)
        return value if isinstance(value, str) else ""
    return ""


def _stub_server(element: dict[str, Any]) -> str:
    """The ``--server`` a broker-stub element registers under, or ``""``."""
    args = element.get("args")
    if not isinstance(args, list):
        return ""
    try:
        argv = expand_stub_flags(args)
    except ValueError:
        return ""
    if STUB_MODULE not in argv:
        return ""
    # Cut at the ledger's own cap, so a long name compares equal to what it holds.
    for i, token in enumerate(argv):
        if token == "--server" and i + 1 < len(argv) and isinstance(argv[i + 1], str):
            return argv[i + 1][:MOUNT_NAME_CAP]
        if isinstance(token, str) and token.startswith("--server="):
            return token.partition("=")[2][:MOUNT_NAME_CAP]
    return ""


class Roster(NamedTuple):
    """One session's broker stubs, as the check compares them."""

    token: str
    #: ``{registered server name: element name}``, both bounded, at most
    #: :data:`MOUNT_SERVERS_CAP` entries.
    servers: dict[str, str]
    #: More stubs than the cap: the daemon's list would be cut too, so the
    #: check cannot prove any one absent.
    overflowed: bool = False


def broker_stub_roster(servers: Any) -> Roster:
    """This array's stub token and its broker stubs, bounded where they are kept.

    Only elements that run the broker stub AND carry a stub token count. Every
    such element of one session carries the same token; an element carrying a
    different one is not this session's and is left out.
    """
    token = ""
    roster: dict[str, str] = {}
    overflowed = False
    for element in servers if isinstance(servers, list) else ():
        if not isinstance(element, dict):
            continue
        server = _stub_server(element)
        element_token = _env_token(element)
        if not server or not element_token:
            continue
        if not token:
            token = element_token
        elif element_token != token:
            continue
        if server in roster:
            continue
        if len(roster) >= MOUNT_SERVERS_CAP:
            overflowed = True
            continue
        name = element.get("name")
        label = sanitize_sink_text(name, NAME_CAP) if isinstance(name, str) else ""
        roster[server] = label or sanitize_sink_text(server, NAME_CAP)
    return Roster(token, roster, overflowed)


def _audit_missing(backend: str, session_id: str, session_key: str, names: list[str]) -> None:
    """One SEL row for one session's missing mounts. Never raises."""
    try:
        from kiro_crew.sel import SecurityEvent, SecurityEventLog

        SecurityEventLog().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type=_EVENT_TYPE,
                caller_identity=session_key,
                agent="kirocrew",
                source="acp",
                operation="session_mcp_mount_check",
                outcome="missing",
                resources=f"backend={backend} session_id={session_id}",
                metadata={
                    "backend": backend,
                    "session_id": session_id,
                    "servers": names,
                    "window_secs": MOUNT_WINDOW_SECS,
                    "mechanism": "gatewayd_session_mounts",
                },
            )
        )
    except Exception:
        logger.warning("SEL audit failed for %s", _EVENT_TYPE, exc_info=True)


async def run_mount_check(
    report: McpSessionReport,
    servers: Any,
    *,
    socket_path: Optional[str],
    backend: str,
    session_id: str,
    session_key: str = "",
    opened_at: Optional[float] = None,
    window: float = MOUNT_WINDOW_SECS,
    interval: float = MOUNT_POLL_SECS,
    query: Query = query_session_mounts,
    alive: Optional[Callable[[], bool]] = None,
) -> list[str]:
    """Wait up to *window* for every stub in *servers*; record the ones that never came.

    Returns the element names found missing. *opened_at* is the wall time the
    session request went out; a ledger that began later cannot answer for it.
    *alive* answers whether the session is still open; a closed one gets nothing.
    """
    roster = broker_stub_roster(servers)
    if not roster.token or not roster.servers or not socket_path:
        return []
    if roster.overflowed:
        # The daemon keeps no more names than the roster does, so absence past
        # the cap is unprovable on both sides.
        _log_unavailable(backend, session_id, "more broker stubs than the check can track")
        return []
    generation = report.generation
    opened = time.time() if opened_at is None else opened_at
    deadline = time.monotonic() + max(0.0, window)
    expected = set(roster.servers)
    seen: Optional[SessionMounts] = None
    while True:
        seen = await query(socket_path, roster.token, opened - _SINCE_SLACK_SECS)
        if seen is None or seen.since > opened + _SINCE_SLACK_SECS:
            seen = None
            break
        if expected <= seen.servers or time.monotonic() >= deadline:
            break
        await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    if report.generation != generation:
        # The report now describes another attempt; this answer is not its own.
        return []
    if seen is None:
        _log_unavailable(backend, session_id, "the broker could not answer")
        return []
    if alive is not None and not alive():
        # The session closed inside the window, so the harness owed it nothing.
        return []
    # A server that reported on its own (a frame the harness sent) is better
    # evidence than the broker's silence, so it is not missing. The roster is
    # bounded at MOUNT_SERVERS_CAP, so this list is too; the log line and the
    # audit row name every one even when the report's FAILED bucket is full.
    missing = [
        name
        for server, name in roster.servers.items()
        if server not in seen.servers and not report.has_reported(name)
    ]
    if not missing:
        return []
    report.record_not_mounted(missing)
    logger.warning(
        "%s session MCP: %d server(s) Crew placed on session %s never reached the MCP "
        "broker within %.0fs, so the harness did not start them and their tools are "
        "absent: %s",
        backend,
        len(missing),
        session_id,
        window,
        ", ".join(missing),
    )
    await asyncio.to_thread(_audit_missing, backend, session_id, session_key, missing)
    return missing


def _log_unavailable(backend: str, session_id: str, why: str) -> None:
    logger.info(
        "%s session MCP mount check: %s for session %s; nothing is recorded as missing",
        backend,
        why,
        session_id,
    )


def schedule_mount_check(report: McpSessionReport, servers: Any, **kwargs: Any) -> None:
    """Start :func:`run_mount_check` in the background. Never raises.

    No-ops without a running loop or when *servers* holds no broker stub.
    """
    roster = broker_stub_roster(servers)
    if not roster.token or not roster.servers or not kwargs.get("socket_path"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _guarded() -> None:
        try:
            await run_mount_check(report, servers, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("session MCP mount check failed", exc_info=True)

    task = loop.create_task(_guarded(), name="acp-mcp-mount-check")
    _PENDING_TASKS.add(task)
    task.add_done_callback(_PENDING_TASKS.discard)
