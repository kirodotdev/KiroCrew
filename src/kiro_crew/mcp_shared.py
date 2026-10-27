"""Shared helpers for MCP stdio servers (mcp_core, mcp_cron)."""

from __future__ import annotations

import collections
import concurrent.futures
import contextlib
import functools
import importlib as _importlib
import json
import logging
import os
import queue
import select
import signal
import sys
import threading
import time
import types
import urllib.request
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable, Literal, NamedTuple, Optional, overload

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import platform_compat
from kiro_crew.config.loader import KiroCrewConfig, config_dir, read_local_secret  # noqa: F401
from kiro_crew.dashboard.origin import parse_dashboard_url  # noqa: F401
from kiro_crew.install_liveness import (
    INSTALL_PRUNED_EXIT_CODE,
    install_pruned,
    respawned_by_pool,
)
from kiro_crew.json_line import (
    ID_PROBE_BYTES,
    parse_json_object_line,
    recover_line_id,
    recover_top_level_id,
)
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.mcp_caller import (
    INFLIGHT_CAPABILITY_KEY,
    INFLIGHT_NOTIFICATION,
    CallerContext,
    caller_identity_capability,
    current_caller,
    resolve_own_identity,
    set_current_caller,
    set_current_tenant_nonce,
    tenant_nonce_from_meta,
)
from kiro_crew.mcp_cleanup import STALE_MANAGED_MCP_SERVERS
from kiro_crew.mcp_shared_calls import DeferredTool
from kiro_crew.mcp_shared_dispatch import _run_stdio_dispatch_loop
from kiro_crew.mcp_shared_wire import release_stdout_fd, snapshot_stdout_fd
from kiro_crew.port_resolution import resolve_client_port_src
from kiro_crew.sel import sel
from kiro_crew.session_directive import neutralize_markers
from kiro_crew.session_token_sig import session_key_from_env_token
from kiro_crew.validation import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_PARSE_ERROR,
    JsonRpcEnvelopeError,
    ValidationError,
    build_tool_response,
    validate_jsonrpc_request,
    validate_jsonrpc_response,
)

logger = logging.getLogger(__name__)

# The component name this PROCESS presents on loopback gateway requests as
# ``X-Internal-Caller``. Set exactly once by ``run_mcp_stdio_loop`` from the
# server name it is handed, BEFORE any request is served — so every MCP stdio
# server (kirocrew-core, kirocrew-dashboard, kirocrew-cron, kirocrew-computer)
# self-identifies without per-server wiring, and a future server gets it for
# free. ``None`` outside an MCP server process (CLI, tests), in which case the
# request helpers send no caller header at all rather than inventing an
# identity. The header is ATTRIBUTION for the gateway's audit log (SEL
# ``source`` — see ``chat_folders._audit_origin``), never authorization: the
# ``X-Internal-Secret`` handshake alone authenticates the request.
_internal_caller_name: str | None = None


def set_internal_caller(name: str | None) -> None:
    """Declare this process's component identity for internal HTTP requests.

    ``None`` un-declares it — used by ``run_mcp_stdio_loop``'s teardown to
    restore the prior value, so repeated loops in one process (the test
    suite) cannot leak one server's identity into the next test's requests.
    """
    global _internal_caller_name
    _internal_caller_name = name


def internal_caller() -> str | None:
    """The declared component identity for internal HTTP requests, if any."""
    return _internal_caller_name


def _hard_exit_on_signal(_signum: int, _frame: Any) -> None:
    """Exit at once on SIGTERM/SIGINT, skipping interpreter finalization.

    Tool work runs on a daemon thread, so a normal exit finalizes the stdio
    streams under it and can abort with ``_enter_buffered_busy`` (SIGABRT, a
    crash dialog on macOS) during a provider's group teardown. Same fix as the
    gateway stub's ``_hard_exit``: drain logging, flush stderr, ``os._exit``.
    stdout is NOT flushed: another thread may hold its lock, and waiting on it
    here would keep the process from ever exiting.
    """
    try:
        logging.shutdown()
    except Exception:  # pragma: no cover - never block exit on log teardown
        pass
    try:
        sys.stderr.flush()
    except (OSError, ValueError, RuntimeError):  # RuntimeError: reentrant flush
        pass
    os._exit(0)


def _install_hard_exit_handlers() -> dict[int, Any]:
    """Install the handler for SIGTERM/SIGINT; return the ones it replaced."""
    if threading.current_thread() is not threading.main_thread():
        return {}  # signal.signal only works on the main thread
    sigs = (signal.SIGTERM, signal.SIGINT)
    return {sig: signal.signal(sig, _hard_exit_on_signal) for sig in sigs}


def run_mcp_stdio_loop(
    server_name: str,
    server_version: str,
    list_tools_fn: Callable[[], list[dict[str, Any]]],
    call_tool_fn: Callable[[str, dict[str, Any]], "str | DeferredTool"],
    *,
    advertise_caller_identity: bool = False,
    error_prefix_is_error: bool = False,
    reports_inflight: bool = False,
    instructions: str = "",
) -> None:
    """Generic MCP stdio server loop — reads JSON-RPC from stdin, writes to stdout.

    *instructions* is the server's MCP ``instructions``: returned in the
    ``initialize`` result when non-empty, so every harness that mounts the server
    gets its usage text (``kiro_crew.mcp_instructions``).

    Tool calls run in a worker thread so the main read loop stays responsive to
    ``notifications/cancelled`` messages from the gateway. A cancel for a call
    running on the worker lets that call run to its end and suppresses its
    response (per MCP spec); a cancel for a parked :class:`DeferredTool` drops
    it and runs its ``cancel()`` hook.

    A handler that only has to WAIT (``wait``, ``spawn_sub_agents``) returns a
    :class:`DeferredTool` instead of parking the worker: the loop registers it,
    runs its short ``step()`` from its own timer whenever ``due_at()`` has
    passed, and answers the request when a step settles it. Parked calls hold
    no thread, and the one worker stays free for the calls that compute.
    A ``notifications/cancelled`` for a parked call
    drops it with no response. While any call is parked, and when the client
    declared :data:`INFLIGHT_CAPABILITY_KEY` in ``initialize``, the loop sends
    :data:`INFLIGHT_NOTIFICATION` every :data:`INFLIGHT_REPORT_SECS` naming the
    parked ids still progressing, so the gateway's wedge detector never reads a
    parked call as a hung one.

    That report is opt-in per server (*reports_inflight*). It exists for a loop
    that parks ``DeferredTool`` calls; a server whose every tool is synchronous
    gains nothing from it, and advertising it would only lower the gateway's
    recycle ceiling under that server's running call. Without the opt-in the
    loop neither advertises the capability nor sends the notification, even to
    a client that declared it.

    ``tools/call`` requests that arrive while a worker is busy are buffered in
    a bounded FIFO queue and dispatched in order as the worker frees
    (silently dropping them left the client waiting forever on a response
    that never came). Queue overflow gets an immediate busy error response.

    On Windows ``select.select`` cannot poll ``sys.stdin`` (it only accepts
    sockets), so frames are read there on a pump thread
    (:func:`_start_stdin_pump`) and the loop waits on its queue instead: the
    worker, the parked-call timer, cancels and ping answers behave the same on
    every platform.

    Before serving anything, a private dup of stdout is captured
    (:func:`snapshot_stdout_fd`) so responses survive a library's process-wide
    ``dup2`` on fd 1 — see the ``_stdout_fd`` comment block. It is released on
    exit so repeated loops in one process (the test suite) cannot leak fds.
    """
    # Declare this process's identity for loopback requests FIRST — tool calls
    # dispatched below reach the gateway through mcp_core's request helpers,
    # which attach it as ``X-Internal-Caller`` so the audit log can name the
    # component (not just "an internal caller") behind each write. Restored on
    # exit, like the fd snapshot below, so repeated loops in one process (the
    # test suite) cannot leak one server's identity into later requests.
    _prior_caller = internal_caller()
    set_internal_caller(server_name)
    snapshot_stdout_fd()
    prior_handlers = _install_hard_exit_handlers()
    pruned = False
    try:
        pruned = _run_stdio_dispatch_loop(
            server_name,
            server_version,
            list_tools_fn,
            call_tool_fn,
            advertise_caller_identity=advertise_caller_identity,
            error_prefix_is_error=error_prefix_is_error,
            reports_inflight=reports_inflight,
            instructions=instructions,
        )
    finally:
        set_internal_caller(_prior_caller)
        release_stdout_fd()
        # Restore, so repeated loops in one process (the test suite) do not
        # leave a later Ctrl-C or SIGTERM exiting 0 through this handler.
        for sig, handler in prior_handlers.items():
            signal.signal(sig, signal.SIG_DFL if handler is None else handler)
    if pruned:
        # Non-zero so the pool records a death and the next call respawns this
        # server from the current install's launch command.
        sys.exit(INSTALL_PRUNED_EXIT_CODE)


# ── The split-out owners ──
# This module keeps the server's entry, ``run_mcp_stdio_loop``, with the process identity it
# declares and its hard-exit signal handlers; the wire, the tool-call model, the tool policy and
# the dispatch loop live in the owners below. Every name an owner defines still resolves as
# ``kiro_crew.mcp_shared.<name>``, and a write through this module (``monkeypatch.setattr``,
# ``mock.patch``) reaches every owner holding the name (``kiro_crew._facade_mirror``). No owner
# imports this module; a higher owner imports a lower one. The owners log as
# ``kiro_crew.mcp_shared``.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.mcp_shared_wire",
    "kiro_crew.mcp_shared_calls",
    "kiro_crew.mcp_shared_policy",
    "kiro_crew.mcp_shared_dispatch",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(sys.modules[__name__], _OWNERS, excluded=()).served)
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.mcp_shared_calls import (  # noqa: F401
        CANCELLED_IDS_MAX,
        DEFERRED_PARKED_MAX,
        DEFERRED_STEP_STUCK_SECS,
        DEFERRED_STEP_WORKERS,
        INFLIGHT_REPORT_SECS,
        PENDING_CALLS_MAX,
        ToolCancelled,
        _audit_safe_args,
        _AuditedDeferred,
        _declares_inflight_capability,
        _DeferredEntry,
        _evict_oldest_evictable,
        _MappedDeferred,
        _remember_cancelled_id,
        _StepPool,
        call_tool_with_logging,
        drive_deferred,
        map_deferred,
    )
    from kiro_crew.mcp_shared_dispatch import (  # noqa: F401
        _POLL_SECS,
        _WORKER_WAIT_SECS,
        JSONRPC_METHOD_NOT_FOUND,
        _tool_call_name,
        _WakePipe,
    )
    from kiro_crew.mcp_shared_policy import (  # noqa: F401
        _EXCLUDED_TOOLS_CACHE_MAX,
        _FAILURE_MESSAGE_MAX,
        _MAX_WARNING_FAILURES,
        _NEGATIVE_CACHE_TTL,
        _NO_INCLUDE,
        _PACKAGE_DIR,
        _POLICY_DETAIL_MAX_CHARS,
        _STARTUP_RACE_CACHE_TTL,
        _UNRESOLVED_REFUSES_CALL,
        ToolPolicy,
        _ambient_audit_session,
        _excluded_tools_by_session,
        _failure_cause,
        _failure_count,
        _http_error_body,
        _included_tools_by_session,
        _last_failure_class,
        _last_failure_time,
        _last_startup_race_key,
        _last_startup_race_time,
        _parse_policy_include,
        _path_is_under,
        _policy_session_key,
        _resolve_excluded_tools,
        _resolve_tool_policy,
        external_client_identity_note,
        serves_managed_tools,
        spawned_without_gateway_identity,
    )
    from kiro_crew.mcp_shared_wire import (  # noqa: F401
        _DRAIN_CHUNK_BYTES,
        _MAX_DRAINABLE_LENGTH_BYTES,
        MAX_CONTENT_LENGTH_BYTES,
        NO_FRAME,
        SKIP,
        STDIN_PUMP_MAX_FRAMES,
        _answer_unparseable,
        _framing,
        _NoFrame,
        _read_message,
        _refuse_oversized,
        _Skipped,
        _start_stdin_pump,
        _stdin_holds_a_line,
        _stdout_fd,
        _stdout_fd_lock,
        _write_all,
        _write_frame,
        notify,
        respond,
    )
