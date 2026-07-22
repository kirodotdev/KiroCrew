"""Shared helpers for MCP stdio servers (mcp_core, mcp_cron)."""

from __future__ import annotations

import collections
import ctypes
import json
import logging
import os
import platform
import select
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from kiro_crew import platform_compat
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.dashboard.origin import parse_dashboard_url
from kiro_crew.sel import sel
from kiro_crew.validation import (
    ValidationError,
    build_tool_response,
    validate_jsonrpc_request,
    validate_jsonrpc_response,
)

logger = logging.getLogger(__name__)

# Max tools/call requests buffered while a tool worker is busy.
# Overflow gets an immediate JSON-RPC busy error instead of silence.
PENDING_CALLS_MAX = 32

# Thread-local cancel event set by run_mcp_stdio_loop worker threads.
# Cooperative tools (wait, spawn_sub_agents) should call is_tool_cancelled()
# in their polling loops.
_thread_cancel_event: Optional[threading.Event] = None


def is_tool_cancelled() -> bool:
    """Return True if the current in-flight tool call has been cancelled.

    Cooperative tools like ``wait`` should check this in their sleep loop
    and exit early (raising ``ToolCancelled``) when True.
    """
    evt = _thread_cancel_event
    return evt is not None and evt.is_set()


class ToolCancelled(Exception):
    """Raised by cooperative tools when ``is_tool_cancelled()`` returns True."""

    pass


# Module-level flag: set True once we detect Content-Length framing from client.
_use_content_length = False

# ── Managed tool policy cache ──────────────────────────────────────────────
# Resolved once per MCP server process lifetime.  The MCP server is spawned
# per kiro-cli session, so the policy is stable for the process.
_excluded_tools: set[str] | None = None
# Two separate negative caches with different TTLs so the long-TTL
# HTTP-error path doesn't keep fail-open active when only a brief
# startup race triggered the failure.
_last_failure_time: float = 0.0           # gateway unreachable / non-404 HTTP error
_last_startup_race_time: float = 0.0      # no session key or 404 — recovers fast
_failure_count: int = 0
# Long TTL applies only when the gateway is genuinely unreachable
# (HTTP errors other than 404, connection refused, timeout).  Kept short
# (60s, was 30s pre-fix) to keep the MCP-level fail-open window narrow:
# longer windows widen the period during which non-kiro-cli MCP hosts
# (Claude Code, custom hosts) — exactly the clients this defense-in-depth
# layer is supposed to protect — bypass tool exclusions.  60s is enough
# to debounce the 5s urlopen storm during a transient gateway outage but
# keeps the fail-open window tight.
_NEGATIVE_CACHE_TTL: float = 60.0  # seconds
# Short TTL for the benign startup-race cases (no session key resolvable,
# or 404 "agent not resolved" because gateway hasn't registered the
# session yet).  Long enough to debounce the warning storm during a
# parallel MCP startup, short enough that we recover to deny-enforcing
# behavior within seconds once the session is registered.  This addresses
# the security-controls concern: don't keep fail-open active for
# 5 minutes when the underlying race resolves in milliseconds.
_STARTUP_RACE_CACHE_TTL: float = 5.0  # seconds
# After this many consecutive failures, suppress the warning log entirely
# (still emit a structured audit event).  The warnings are noise once the
# 404 root cause is established for the session.
_MAX_WARNING_FAILURES: int = 2


def _resolve_excluded_tools() -> set[str]:
    """Query the gateway for the current session's managedToolPolicy.exclude.

    Returns a set of tool names that should be hidden from this session.
    Caches the result on success only.  On failure:

    - If session key is unavailable (startup race): fail-open, do NOT
      cache, allow retry on next call.  Cannot fail-closed here because
      kiro-cli calls tools/list once at session start — if we return an
      empty list, kiro-cli permanently believes this MCP server has no
      tools (unrecoverable without session restart).
    - If session key is available but policy call fails: fail-open with
      negative cache (30s) to avoid blocking every tool call with a 5s
      timeout when gateway is persistently unreachable.

    Fail-open is acceptable because:
    1. The SDK already applies managedToolPolicy.exclude as disabledTools
       in the agent config — kiro-cli enforces this independently.
    2. The gateway's approval layer provides the authoritative deny gate.
    3. This MCP-level filtering is defense-in-depth for non-kiro-cli
       clients (Claude Code, custom MCP hosts) that skip disabledTools.
    """
    global _excluded_tools, _last_failure_time, _last_startup_race_time, _failure_count
    if _excluded_tools is not None:
        return _excluded_tools

    now = time.monotonic()
    # Negative cache: avoid hammering gateway on persistent failures.
    # Silent during the cache window — only the structured audit event is
    # emitted to keep gateway.log readable.  Two windows: a long one for
    # genuine HTTP/network failure, a short one for benign startup races.
    if (
        (_last_failure_time and (now - _last_failure_time) < _NEGATIVE_CACHE_TTL)
        or (_last_startup_race_time and (now - _last_startup_race_time) < _STARTUP_RACE_CACHE_TTL)
    ):
        sel().log_api_access(
            caller=os.environ.get("KIROCREW_SESSION_KEY", "mcp"),
            operation="tool_policy.negative_cache_hit",
            outcome="fail_open",
            source="mcp_shared",
        )
        return set()

    try:
        cfg = KiroCrewConfig.load()
        _host, port = parse_dashboard_url(cfg.dashboard.url)
        api_base = f"http://localhost:{port}"

        # Read internal secret for auth
        secret = ""
        try:
            secret = (config_dir() / ".local_secret").read_text().strip()
        except Exception:
            pass

        # Resolve session key (same logic as mcp_core._resolve_session_key)
        session_key = os.environ.get("KIROCREW_SESSION_KEY", "")
        if not session_key:
            def _ppid_via_libproc(pid: int) -> int:
                """macOS parent-PID via libproc proc_pidinfo (no exec, sandbox-safe)."""
                proc_pidtbsdinfo = 3
                buf_size = 256
                try:
                    libproc = ctypes.CDLL("libproc.dylib", use_errno=True)
                    libproc.proc_pidinfo.restype = ctypes.c_int
                    libproc.proc_pidinfo.argtypes = [
                        ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                        ctypes.c_void_p, ctypes.c_int,
                    ]
                    buf = ctypes.create_string_buffer(buf_size)
                    n = libproc.proc_pidinfo(pid, proc_pidtbsdinfo, 0, buf, buf_size)
                    if n <= 16:
                        return 0
                    return int(struct.unpack_from("<5I", buf.raw, 0)[4])
                except Exception:
                    return 0

            def _get_ppid(pid: int) -> int:
                system = platform.system()
                try:
                    if system == "Linux":
                        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                            if line.startswith("PPid:"):
                                return int(line.split()[1])
                    elif system == "Darwin":
                        ppid = _ppid_via_libproc(pid)
                        if ppid:
                            return ppid
                    out = subprocess.check_output(
                        ["ps", "-o", "ppid=", "-p", str(pid)], text=True, timeout=2
                    )
                    return int(out.strip())
                except Exception:
                    pass
                return 0

            cfg_dir = config_dir()
            # Sandbox launcher exports its own HOST pid (the pid the gateway
            # keys session_pid files by) — direct lookup works even when this
            # process's pid view diverges from the host's (PID-namespace
            # sandboxing), where the ancestor walk below can never match.
            host_pid = os.environ.get("KIROCREW_HOST_PID", "")
            if host_pid.isdigit():
                pid_file = cfg_dir / f"session_pid_{host_pid}.txt"
                if pid_file.exists():
                    session_key = pid_file.read_text(encoding="utf-8").strip()
            if not session_key:
                pid = os.getppid()
                seen: set[int] = set()
                while pid > 1 and pid not in seen:
                    seen.add(pid)
                    pid_file = cfg_dir / f"session_pid_{pid}.txt"
                    if pid_file.exists():
                        session_key = pid_file.read_text(encoding="utf-8").strip()
                        break
                    pid = _get_ppid(pid)

        if not session_key:
            # No session key resolvable (startup race — kiro-cli hasn't
            # written PID file yet, or process is from the warm pool).
            # Must fail-open: kiro-cli calls tools/list once and caches
            # the result.  Returning empty tools here would permanently
            # hide all tools for this session (unrecoverable).  Short
            # negative-cache (5s) debounces the warning storm during
            # parallel MCP startup but recovers to deny-enforcing
            # behavior within seconds — the session_pid file typically
            # appears within a few hundred ms of MCP spawn.
            _last_startup_race_time = now
            sel().log_api_access(
                caller="mcp",
                operation="tool_policy.no_session_key",
                outcome="fail_open",
                source="mcp_shared",
            )
            return set()

        headers: dict[str, str] = {"X-Internal-Secret": secret}
        headers["X-Session-Key"] = session_key

        req = urllib.request.Request(
            f"{api_base}/api/session-tool-policy",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                policy = json.loads(resp.read())
        except urllib.error.HTTPError as http_exc:
            # 404 = "agent not resolved" (gateway side hasn't registered
            # this session yet — common during MCP startup before the
            # session_pid file is fully visible across processes).  This
            # is a benign race; use the short startup-race cache so the
            # MCP server recovers to deny-enforcing behavior within
            # seconds once the session is registered.  Critically, do
            # NOT log a stack trace for 404 — it floods gateway.log on
            # every fresh subagent spawn.
            if http_exc.code == 404:
                _last_startup_race_time = now
                sel().log_api_access(
                    caller=os.environ.get("KIROCREW_SESSION_KEY", "mcp"),
                    operation="tool_policy.agent_not_resolved",
                    outcome="fail_open",
                    source="mcp_shared",
                    resources=f"session_key={session_key}",
                )
                return set()
            raise

        exclude = policy.get("exclude", [])
        if isinstance(exclude, list):
            _excluded_tools = {t for t in exclude if isinstance(t, str)}
        else:
            _excluded_tools = set()
        return _excluded_tools
    except Exception as exc:
        # Policy call failed (network error, timeout, non-404 HTTP) —
        # use the LONG negative cache to avoid repeated 5s urlopen
        # blocks across many MCP servers when the gateway is genuinely
        # unreachable.  Known deviation from deny-by-default: fail-open
        # is acceptable here because kiro-cli independently enforces
        # disabledTools from the agent config.  This MCP-level filtering
        # is defense-in-depth.
        _last_failure_time = time.monotonic()
        _failure_count += 1
        # Suppress repeated warnings — once we've logged twice the operator
        # has all the diagnostic info and further entries flood gateway.log
        # at every MCP server startup (10+ servers × every session start).
        if _failure_count <= _MAX_WARNING_FAILURES:
            logger.warning(
                "Tool policy resolution failed (%s), fail-open for %.0fs (defense-in-depth bypass)",
                exc.__class__.__name__,
                _NEGATIVE_CACHE_TTL,
                exc_info=True,
            )
        elif _failure_count == _MAX_WARNING_FAILURES + 1:
            logger.warning(
                "Tool policy resolution still failing — further warnings suppressed; "
                "see audit log for tool_policy.resolution_failed events",
            )
        sel().log_api_access(
            caller=os.environ.get("KIROCREW_SESSION_KEY", "mcp"),
            operation="tool_policy.resolution_failed",
            outcome="fail_open",
            source="mcp_shared",
        )
        return set()


def respond(req_id: Any, result: Any, error: dict | None = None) -> None:
    """Write a validated JSON-RPC response to stdout."""
    if req_id is None:
        return
    resp: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
    if error:
        resp["error"] = error
    else:
        resp["result"] = result
    try:
        resp = validate_jsonrpc_response(resp)
    except ValidationError:
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32603, "message": "Internal error"},
        }
    body = json.dumps(resp)
    if _use_content_length:
        payload = body.encode("utf-8")
        header = f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8")
        sys.stdout.buffer.write(header + payload)
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(body + "\n")
        sys.stdout.flush()


def call_tool_with_logging(
    name: str,
    raw_args: dict[str, Any],
    validate_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    inner_fn: Callable[[str, dict[str, Any]], str],
    session_key: str,
    downstream_service: str,
) -> str:
    """Validate args, call inner tool function, and log the invocation."""
    try:
        args = validate_fn(name, raw_args)
    except ValidationError as e:
        sel().log_tool_invocation(
            session_key=session_key,
            source="mcp",
            tool_name=name,
            tool_kind=session_key,
            outcome="failed",
            downstream_service=downstream_service,
            error=str(e),
        )
        return f"Error: {e}"

    result = inner_fn(name, args)
    outcome = "failed" if result.startswith("Error:") else "completed"
    # Redact the serialized args before they land in the SEL audit resources.
    # Tool args can carry agent-supplied free text (e.g. artifact_post_comment
    # `text`, artifact_delete_comment `reason`) that may contain a credential;
    # per-tool handlers redact their OWN egress copy, but the args dict logged
    # here is a separate validated object, so redact centrally through the
    # canonical context-aware shim (defense-in-depth for every tool, not just
    # the ones a handler happened to scrub).
    resources = ""
    if args:
        from kiro_crew.platform import redact_via_context

        resources = redact_via_context(json.dumps(args))[:500]
    sel().log_tool_invocation(
        session_key=session_key,
        source="mcp",
        tool_name=name,
        tool_kind=session_key,
        outcome=outcome,
        downstream_service=downstream_service,
        resources=resources,
        error=result[:500] if outcome == "failed" else "",
    )
    return result


def _read_message(stdin) -> dict[str, Any] | None:
    """Read one JSON-RPC message, auto-detecting Content-Length vs bare JSON framing.

    Uses stdin.buffer (binary mode) for all reads so that Content-Length byte
    counts are honoured correctly for multi-byte UTF-8 content.
    """
    global _use_content_length
    raw = stdin.buffer
    while True:
        line = raw.readline()
        if not line:
            return None  # EOF
        line_str = line.decode("utf-8").strip()
        if not line_str:
            continue
        if line_str.lower().startswith("content-length:"):
            try:
                length = int(line_str.split(":", 1)[1].strip())
                _use_content_length = True
                # Consume the blank line separator
                while True:
                    sep = raw.readline()
                    if sep.strip() == b"":
                        break
                # Read exactly `length` bytes. A single raw.read(length) may
                # return fewer bytes than requested on a partial read (the
                # RawIOBase/socket contract permits short reads), which would
                # truncate the body, fail json.loads, and desync the stream for
                # every subsequent message. Loop until we have the full body or
                # hit EOF. (io.BufferedReader blocks for the full count today, so
                # this is robustness hardening for non-buffered/custom streams.)
                chunks: list[bytes] = []
                remaining = length
                while remaining > 0:
                    chunk = raw.read(remaining)
                    if not chunk:
                        break  # EOF before the full body arrived
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if remaining > 0:
                    # EOF before the declared body fully arrived — the message is
                    # incomplete. Discard it explicitly rather than handing a truncated
                    # body to json.loads, which could otherwise return a message the
                    # sender never finished transmitting if the partial bytes happen to
                    # be valid JSON (e.g. a well-formed prefix).
                    continue
                body = b"".join(chunks)
                return json.loads(body.decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                continue
        # Bare JSON line (backwards compat)
        try:
            return json.loads(line_str)
        except json.JSONDecodeError:
            continue


def run_mcp_stdio_loop(
    server_name: str,
    server_version: str,
    list_tools_fn: Callable[[], list[dict[str, Any]]],
    call_tool_fn: Callable[[str, dict[str, Any]], str],
) -> None:
    """Generic MCP stdio server loop — reads JSON-RPC from stdin, writes to stdout.

    Tool calls run in a worker thread so the main read loop stays responsive to
    ``notifications/cancelled`` messages from the gateway. When a cancel is
    received for an in-flight request, the worker thread is interrupted via
    a threading.Event that cooperative tools (``wait``, ``spawn_sub_agents``)
    check periodically. The cancelled request emits no response (per MCP spec).

    ``tools/call`` requests that arrive while a worker is busy are buffered in
    a bounded FIFO queue and dispatched in order as the worker frees
    (silently dropping them left the client waiting forever on a response
    that never came). Queue overflow gets an immediate busy error response.

    On Windows ``select.select`` cannot poll ``sys.stdin`` (it only accepts
    sockets), so tool calls dispatch synchronously exactly as the pre-worker
    loop did — no in-flight cancel/ping interleave there (POSIX-only feature).
    """
    # In-flight tool execution state: at most one at a time (sequential dispatch).
    _current_req_id: Any = None
    _cancel_event: Optional[threading.Event] = None
    _worker_thread: Optional[threading.Thread] = None
    _result_lock = threading.Lock()
    _result_ready = threading.Event()
    _result_box: list = []  # [response_payload] or [] if cancelled
    _cancelled_ids: set = set()
    _current_tool_name: str = ""
    _worker_audited: list = [False]  # [bool], guarded by _result_lock
    # tools/call requests received while a worker was busy, dispatched FIFO.
    _pending_calls: collections.deque[dict[str, Any]] = collections.deque()

    def _sel_audit(outcome: str, tool_name: str, req_id: Any) -> None:
        """Emit a SEL audit event for a tool invocation outcome.

        SEL failure must not break the response path, but a missed audit
        record must be visible (security-controls guideline: callback
        failures are logged, never bare pass)."""
        try:
            sel().log_tool_invocation(
                session_key=os.environ.get("KIROCREW_SESSION_KEY", "mcp"),
                source="mcp",
                tool_name=tool_name,
                tool_kind=server_name,
                outcome=outcome,
                request_id=str(req_id),
            )
        except Exception as sel_exc:
            logger.warning(
                "SEL audit failed for %s tool %s (request %s): %s",
                outcome, tool_name, req_id, sel_exc,
            )

    def _run_tool(
        req_id: Any, tool_name: str, tool_args: dict, cancel_evt: threading.Event
    ) -> None:
        """Worker thread: run tool, store result unless cancelled."""
        global _thread_cancel_event
        # Inject cancel event into thread-local so cooperative tools can check it
        _thread_cancel_event = cancel_evt
        try:
            result_text = call_tool_fn(tool_name, tool_args)
        except ToolCancelled:
            # Tool cooperatively exited on cancel -- suppress response
            logger.info("tool cancelled for request %s", req_id)
            # SEL audit: cancelled tool invocations must emit audit events
            _sel_audit("cancelled", tool_name, req_id)
            _thread_cancel_event = None
            _result_ready.set()
            return
        except Exception as exc:
            result_text = f"Error: {exc}"
            _tool_errored = True
        else:
            _tool_errored = False
        finally:
            _thread_cancel_event = None
        # Audit decision is made atomically with the cancellation check, under
        # the same lock that guards response delivery: exactly ONE audit event
        # per request (a failed+late-cancel race must not emit two).
        with _result_lock:
            if not cancel_evt.is_set():
                _result_box.append(build_tool_response(result_text))
                if _tool_errored:
                    # Exception escaped call_tool_fn (may bypass its internal
                    # logging) -- audit the failure.
                    _sel_audit("failed", tool_name, req_id)
                    _worker_audited[0] = True
            else:
                # Late-cancel race: tool finished (or errored) but cancel
                # arrived before delivery. From the client's perspective this
                # invocation was cancelled.
                _sel_audit("cancelled", tool_name, req_id)
                _worker_audited[0] = True
        _result_ready.set()

    while True:
        # If a worker is running, poll for completion while also reading stdin
        if _worker_thread is not None and _worker_thread.is_alive():
            # Non-blocking stdin read with short timeout to interleave
            readable, _, _ = select.select([sys.stdin], [], [], 0.1)
            if not readable:
                if _result_ready.is_set():
                    _worker_thread.join(timeout=1.0)
                    _worker_thread = None
                    with _result_lock:
                        if _result_box and str(_current_req_id) not in _cancelled_ids:
                            respond(_current_req_id, _result_box[0])
                        elif _result_box and not _worker_audited[0]:
                            # Boxed result dropped due to cancellation (cancel
                            # arrived after the worker delivered) -- audit it.
                            _sel_audit("cancelled", _current_tool_name, _current_req_id)
                        _result_box.clear()
                    _current_req_id = None
                    _cancel_event = None
                    _result_ready.clear()
                continue
            req = _read_message(sys.stdin)
            if req is None:
                # EOF: wait for worker then exit
                if _worker_thread:
                    _worker_thread.join(timeout=5.0)
                break
            # Process only cancel notifications while tool is running
            try:
                method, req_id, _params = validate_jsonrpc_request(req)
            except ValidationError:
                continue
            if method == "notifications/cancelled":
                params = req.get("params", {})
                cancelled_rid = params.get("requestId")
                if cancelled_rid is not None:
                    _cancelled_ids.add(str(cancelled_rid))
                    if str(cancelled_rid) == str(_current_req_id) and _cancel_event:
                        _cancel_event.set()
                        logger.info("cancel received for in-flight request %s", cancelled_rid)
            # Answer gateway pings even while a tool is in-flight so the
            # ping-gated wedge detector sees the backend as responsive.
            elif method == "ping" and req_id is not None:
                respond(req_id, {})
            # Buffer tools/call requests that arrive while busy so they get a
            # response when the worker frees (dropping them left the
            # client waiting forever). Cancels against queued ids are honored
            # at dispatch time via _cancelled_ids.
            elif method == "tools/call" and req_id is not None:
                if len(_pending_calls) >= PENDING_CALLS_MAX:
                    # Rejection is a tool-invocation decision -- audit it
                    # (security-controls: all invocation decisions emit SEL).
                    _sel_audit(
                        "rejected_busy",
                        req.get("params", {}).get("name", ""),
                        req_id,
                    )
                    respond(
                        req_id,
                        None,
                        error={
                            "code": -32000,
                            "message": "Server busy: pending tool-call queue is full; retry",
                        },
                    )
                else:
                    _pending_calls.append(req)
            # Other messages while busy: drop gracefully. Notifications are
            # fine to drop; initialize/initialized never arrive mid-tool.
            elif method == "tools/list" and req_id is not None:
                excluded = _resolve_excluded_tools()
                tools = list_tools_fn()
                if excluded:
                    tools = [t for t in tools if t.get("name") not in excluded]
                respond(req_id, {"tools": tools})
            continue

        # Check if worker just finished
        if _worker_thread is not None:
            _worker_thread.join(timeout=0.1)
            _worker_thread = None
            with _result_lock:
                if _result_box and str(_current_req_id) not in _cancelled_ids:
                    respond(_current_req_id, _result_box[0])
                elif _result_box and not _worker_audited[0]:
                    # Boxed result dropped due to cancellation (cancel arrived
                    # after the worker delivered) -- audit it.
                    _sel_audit("cancelled", _current_tool_name, _current_req_id)
                _result_box.clear()
            _current_req_id = None
            _cancel_event = None
            _result_ready.clear()

        # Dispatch a queued tools/call (FIFO) before reading new input.
        if _pending_calls:
            req = _pending_calls.popleft()
        else:
            req = _read_message(sys.stdin)
            if req is None:
                break

        try:
            method, req_id, _params = validate_jsonrpc_request(req)
        except ValidationError:
            continue

        if method == "initialize":
            respond(
                req_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": server_name, "version": server_version},
                },
            )
        elif method == "notifications/initialized":
            pass
        elif method == "notifications/cancelled":
            # Cancel for a request that already completed -- ignore
            params = req.get("params", {})
            cancelled_rid = params.get("requestId")
            if cancelled_rid is not None:
                _cancelled_ids.add(str(cancelled_rid))
        elif method == "tools/list":
            excluded = _resolve_excluded_tools()
            tools = list_tools_fn()
            if excluded:
                tools = [t for t in tools if t.get("name") not in excluded]
            respond(req_id, {"tools": tools})
        elif method == "ping":
            respond(req_id, {})
        elif method == "tools/call":
            params = req.get("params", {})
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            if not isinstance(tool_args, dict):
                tool_args = {}
            # A queued request may have been cancelled while waiting -- emit
            # no response (per MCP spec) but audit the cancellation.
            if req_id is not None and str(req_id) in _cancelled_ids:
                _sel_audit("cancelled", tool_name, req_id)
                continue
            # Defense-in-depth: reject calls to excluded tools even if
            # the LLM somehow attempts to call them (hallucination).
            excluded = _resolve_excluded_tools()
            if tool_name in excluded:
                sel().log_tool_invocation(
                    session_key=os.environ.get("KIROCREW_SESSION_KEY", "mcp"),
                    source="mcp",
                    tool_name=tool_name,
                    tool_kind=server_name,
                    outcome="rejected_excluded",
                    error="managedToolPolicy.exclude",
                )
                respond(
                    req_id,
                    build_tool_response(
                        f"Error: tool '{tool_name}' is not available for this agent"
                    ),
                )
            elif not platform_compat.IS_POSIX:
                # Windows: select.select() cannot poll sys.stdin (WinError
                # 10038), so no worker-thread interleave — dispatch the tool
                # synchronously exactly as the pre-worker loop did.
                result_text = call_tool_fn(tool_name, tool_args)
                respond(req_id, build_tool_response(result_text))
            else:
                # Dispatch tool in worker thread so we can receive cancel notifications
                _cancel_event = threading.Event()
                _current_req_id = req_id
                _current_tool_name = tool_name
                _worker_audited[0] = False
                _result_ready.clear()
                _result_box.clear()
                _worker_thread = threading.Thread(
                    target=_run_tool,
                    args=(req_id, tool_name, tool_args, _cancel_event),
                    daemon=True,
                )
                _worker_thread.start()
        elif req_id is not None:
            respond(req_id, None, error={"code": -32601, "message": f"Unknown method: {method}"})
