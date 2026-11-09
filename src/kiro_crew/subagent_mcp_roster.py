"""What a session's own MCP servers add to the memory a start is priced at.

kiro-cli starts a fresh copy of every stdio server its agent spec declares for each
session, shared runtime or not, unless the MCP gateway hands the session a stub
for it (``gateway_routed_names``). The admission gate prices a start before any
of that runs, so a spec with dozens of servers was admitted at the price of the
default roster and the host ran out of memory once the servers came up.

:func:`session_mcp_roster_gb` turns the spec's unpooled stdio server count into a
figure the start pricing (``start_pricing._dedicated_start_price_gb``) may use. It
is calibrated on two measurements: the default roster (four of Kiro Crew's own
servers) at ~0.96 GiB, under the 1.0 GiB unlearned price, and a 30-server roster
at 4.3 GiB per session, so a default install is priced as before.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

logger = logging.getLogger(__name__)

#: The kiro-cli launcher and chat process a dedicated start runs before any MCP
#: server: the same part ``start_pricing._SHARED_START_SAVING_GB`` takes off a
#: shared start.
_ROSTER_BASE_GB = 0.35

#: One stdio MCP server's settled share of a session. A host measured 26 user
#: servers plus four of Kiro Crew's own at 4.3 to 5.4 GiB per session scope;
#: the low end, less the base, over 30 servers.
_PER_SERVER_GB = 0.13

#: How long one reading of the spec is reused.
_CACHE_SECS = 60.0

_cache_lock = threading.Lock()
_cache: tuple[float, float] | None = None  # (monotonic expiry, GiB)
#: The refresh in flight on the event loop's executor, so one runs at a time.
_refresh_future: "asyncio.Future[float] | None" = None


def _unpooled_stdio_server_count() -> int:
    """Stdio servers the default agent spec starts in each session. BLOCKING.

    Muted and catalog-governed entries do not launch, an HTTP/SSE entry has no
    local process, and a server the running MCP gateway routes is a stub here.
    shortcut: reads the default agent's spec for every bucket; a template whose
    roster differs is priced from the default one until starts carry their spec.
    """
    from kiro_crew.agent import gateway_routed_names, kiro_agents_dir_path
    from kiro_crew.agent_files import AGENT_FILENAME
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted, mcp_entry_is_registry_governed
    from kiro_crew.user_json import load_mcp_servers

    servers = load_mcp_servers(kiro_agents_dir_path() / AGENT_FILENAME)
    launched = [
        name
        for name, entry in servers.items()
        if isinstance(entry, dict)
        and isinstance(entry.get("command"), str)
        and entry["command"]
        and not mcp_entry_is_muted(entry)
        and not mcp_entry_is_registry_governed(entry)
    ]
    if not launched:
        return 0
    routed = gateway_routed_names()
    return sum(1 for name in launched if name not in routed)


def roster_gb_for(count: int) -> float:
    """The settled size of a session that starts *count* unpooled stdio servers."""
    return _ROSTER_BASE_GB + max(0, count) * _PER_SERVER_GB


def _refresh() -> float:
    """Read the roster and cache it. BLOCKING; never raises."""
    global _cache, _refresh_future
    try:
        gb = roster_gb_for(_unpooled_stdio_server_count())
    except Exception:
        logger.debug("MCP roster price unavailable; pricing starts without it", exc_info=True)
        gb = 0.0
    with _cache_lock:
        _cache = (time.monotonic() + _CACHE_SECS, gb)
        _refresh_future = None
    return gb


def session_mcp_roster_gb() -> float:
    """A session's projected size from its MCP roster, or 0.0 when not read yet.

    Cached for ``_CACHE_SECS``. On the event loop a stale or missing reading is
    refreshed in a worker thread and the last reading (0.0 before the first) is
    returned, because the read walks the agents directory; off the loop it is read
    inline. Never raises: an unread roster prices nothing extra, so a start is
    admitted exactly as it was without this figure.
    shortcut: the first starts after boot price without the roster until the
    first refresh lands, a few milliseconds; prime it at boot if that matters.
    """
    global _refresh_future
    with _cache_lock:
        cached = _cache
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        last = cached[1] if cached is not None else 0.0
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and _refresh_future is not None:
            return last
        if loop is not None:
            try:
                _refresh_future = loop.run_in_executor(None, _refresh)
            except RuntimeError:  # an executor that is shutting down
                _refresh_future = None
            return last
    return _refresh()
