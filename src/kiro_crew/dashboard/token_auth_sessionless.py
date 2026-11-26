"""Internal-secret calls that name no session, from a process inside the agent sandbox.

An ``X-Internal-Secret`` call that names no session is attributed to the person:
``token_auth_callers.derive_caller_app`` answers ``""`` and no per-session limit
applies. That is right for the callers that run on the host -- the gateway's own
calls, the CLI in the person's terminal, the desktop app -- and it is not the
attribution a process inside the agent sandbox should get. Most of Kiro Crew's own
in-sandbox components name the session they act for; the ones that do not yet (the
KAS Connection bridge's non-capability branch, some ``kirocrew`` CLI commands run
from an agent shell) are what log mode exists to surface before the default moves
to refusing. A session-less call from a sandboxed process is the shape this module
looks for.

Whether the caller runs on the host is read from the kernel, with the evidence the
local owner-token mint already uses (``member_memory_auth.local_owner_bootstrap_provenance``):
the peer pid of the connection, over the unix socket or loopback TCP, and on Linux
its namespaces, on macOS its sandbox state. Windows has no OS sandbox and is not
checked.

``dashboard.sessionless_internal_calls`` decides what happens to a caller the check
does not place on the host:

* ``"log"`` (the default) admits the call. Every caller is checked; a caller and
  path pair not placed on the host writes at most one security-log row every
  :data:`LOG_WINDOW_SECS`, so the rows show which callers would be refused;
* ``"enforce"`` looks at every such call and refuses one not placed on the host with
  403 ``session_required``.

A host caller the kernel check cannot place is counted with the sandboxed ones: a
gateway whose service unit gives it a private mount namespace, a CLI that reaches a
containerized gateway through a mounted socket, a peer whose process table entry
cannot be read. The local owner-token mint has the same limit. That is what the log
mode is for.

Served through :mod:`kiro_crew.dashboard.token_auth`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections import Counter
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.origin import is_proxied_request
from kiro_crew.dashboard.token_auth_callers import KNOWN_INTERNAL_CALLERS, live_dashboard_mode
from kiro_crew.executors import local_auth_executor
from kiro_crew.sel import sel as _sel_fn

logger = logging.getLogger("kiro_crew.dashboard.token_auth")

#: The values ``dashboard.sessionless_internal_calls`` takes, the default first.
SESSIONLESS_INTERNAL_CALL_MODES = ("log", "enforce")

#: Seconds one ``(caller, path)`` pair writes at most one log-mode row in.
LOG_WINDOW_SECS = 600.0
#: Pairs remembered for that window; the oldest is forgotten past this.
_MAX_TRACKED = 512

#: The surface key the dashboard's own UI sends; it names no session.
_NO_SESSION_KEYS = frozenset({"", "dashboard:ui"})

#: Log-mode counts and last emission times for tracked not-on-host caller/path pairs.
#: Host verdicts are never cached: a pid may be reused by a sandboxed process.
_SESSIONLESS_COUNTS: Counter[tuple[str, str]] = Counter()
_sessionless_log_last: dict[tuple[str, str], float] = {}


def sessionless_internal_call_mode() -> str:
    """The configured mode, from the live config; ``"log"`` when unset or unreadable."""
    return live_dashboard_mode("sessionless_internal_calls", SESSIONLESS_INTERNAL_CALL_MODES)


def _platform_has_sandbox() -> bool:
    """Whether this OS has an agent sandbox to tell apart from the host; Windows has none."""
    return sys.platform != "win32"


def _names_no_session(request: web.Request) -> bool:
    return request.headers.get("X-Session-Key", "").strip() in _NO_SESSION_KEYS


def _caller_on_host(request: web.Request) -> bool:
    """Whether the kernel places the calling process on the host, outside the sandbox."""
    from kiro_crew.member_memory_auth import local_owner_bootstrap_provenance

    return local_owner_bootstrap_provenance(request) is not None


async def sessionless_internal_call_refused(
    request: web.Request, path: str, *, capability_granted: bool, clock: Any = time.monotonic
) -> bool:
    """Whether to refuse this internal-secret call; logs it in ``"log"`` mode.

    Only a call that names no session is checked, and not one the agent socket
    admitted on a capability (that binds a session of its own). ``False`` for every
    other call, for a caller on the host, and on Windows.

    The process-table lookup runs on the pool the owner-token mint uses. Every
    caller is checked, including a host caller whose pid may have been reused.
    Log-mode counts and timestamps change only after a not-on-host verdict, in
    one synchronous block on the dashboard event loop.
    """
    if capability_granted or not _names_no_session(request) or not _platform_has_sandbox():
        return False
    enforce = sessionless_internal_call_mode() == "enforce"
    caller = request.headers.get("X-Internal-Caller", "")
    caller = caller if caller in KNOWN_INTERNAL_CALLERS else "other"
    loop = asyncio.get_running_loop()
    on_host = await loop.run_in_executor(local_auth_executor(), _caller_on_host, request)
    if on_host:
        return False
    if not enforce:
        # No await here: requests on the event loop cannot interleave this update.
        bucket = (caller, path)
        if bucket not in _SESSIONLESS_COUNTS and len(_SESSIONLESS_COUNTS) >= _MAX_TRACKED:
            oldest = next(iter(_SESSIONLESS_COUNTS))
            _SESSIONLESS_COUNTS.pop(oldest)
            _sessionless_log_last.pop(oldest)
        _SESSIONLESS_COUNTS[bucket] += 1
        now = clock()
        last = _sessionless_log_last.get(bucket)
        if last is not None and now - last < LOG_WINDOW_SECS:
            return False
        _sessionless_log_last[bucket] = now
    try:
        _sel_fn().log_api_access(
            caller=caller,
            forwarded=is_proxied_request(request),
            operation="internal_auth.sessionless_caller",
            outcome="denied" if enforce else "would_deny",
            source="token_auth",
            resources=path,
            error="names no session; caller not verified as a host process",
        )
    except Exception:  # noqa: BLE001 - auditing never decides the request
        logger.debug("sessionless internal call audit failed", exc_info=True)
    return enforce
