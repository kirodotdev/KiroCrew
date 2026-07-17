"""Shared helpers for MCP stdio servers (mcp_core, mcp_cron)."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import platform
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.dashboard.origin import parse_dashboard_url
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

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
# the AutoSDE security-controls concern: don't keep fail-open active for
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
    from kiro_crew.validation import ValidationError, validate_jsonrpc_response

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
    from kiro_crew.sel import sel
    from kiro_crew.validation import ValidationError

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
    sel().log_tool_invocation(
        session_key=session_key,
        source="mcp",
        tool_name=name,
        tool_kind=session_key,
        outcome=outcome,
        downstream_service=downstream_service,
        resources=json.dumps(args)[:500] if args else "",
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
    """Generic MCP stdio server loop — reads JSON-RPC from stdin, writes to stdout."""
    from kiro_crew.validation import ValidationError, build_tool_response, validate_jsonrpc_request

    while True:
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
        elif method == "tools/list":
            excluded = _resolve_excluded_tools()
            tools = list_tools_fn()
            if excluded:
                tools = [t for t in tools if t.get("name") not in excluded]
            respond(req_id, {"tools": tools})
        elif method == "tools/call":
            params = req.get("params", {})
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            if not isinstance(tool_args, dict):
                tool_args = {}
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
            else:
                result_text = call_tool_fn(tool_name, tool_args)
                respond(req_id, build_tool_response(result_text))
        elif req_id is not None:
            respond(req_id, None, error={"code": -32601, "message": f"Unknown method: {method}"})
