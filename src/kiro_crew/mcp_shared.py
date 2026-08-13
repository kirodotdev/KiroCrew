"""Shared helpers for MCP stdio servers (mcp_core, mcp_cron)."""

from __future__ import annotations

import collections
import contextlib
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
from kiro_crew.config.loader import KiroCrewConfig, config_dir, read_local_secret
from kiro_crew.dashboard.origin import parse_dashboard_url
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.mcp_caller import (
    CallerContext,
    caller_identity_capability,
    set_current_caller,
)
from kiro_crew.sel import sel
from kiro_crew.validation import (
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
# ``X-Internal-Secret`` handshake alone authenticates the request (#3503).
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


# Max tools/call requests buffered while a tool worker is busy.
# Overflow gets an immediate JSON-RPC busy error instead of silence.
PENDING_CALLS_MAX = 32

# Max cancelled-request ids retained. ``notifications/cancelled`` can arrive
# for any request id over the life of the (long-lived, per-session) MCP server
# process; retaining them in an unbounded set leaks one entry per cancel. Once
# this cap is reached the oldest ids are evicted FIFO.
CANCELLED_IDS_MAX = 1024


def _evict_oldest_evictable(
    cancelled_ids: set[str],
    order: "collections.deque[str]",
    protected: set[str],
) -> bool:
    """Discard the oldest cancellation id that is NOT protected.

    ``protected`` holds the ids of the currently active and still-queued
    requests. Evicting one of those would drop a cancellation flag before the
    dispatch loop consumes it, letting a cancelled queued call execute -- so
    protected ids are rotated to the back of ``order`` and kept. Returns True
    if a (non-protected or stale) id was evicted, False if only protected ids
    remain (the caller then tolerates a bounded overflow -- ``protected`` is
    bounded by the pending-queue cap + 1, far below ``CANCELLED_IDS_MAX``).
    """
    for _ in range(len(order)):
        oldest = order.popleft()
        if oldest in protected and oldest in cancelled_ids:
            # Live request -- keep its cancellation flag; move to the back.
            order.append(oldest)
            continue
        # Non-protected id, or a stale deque entry already gone from the set
        # (``discard`` is a no-op for absent ids).
        cancelled_ids.discard(oldest)
        return True
    return False


def _remember_cancelled_id(
    cancelled_ids: set[str],
    order: "collections.deque[str]",
    rid: str,
    cap: int = CANCELLED_IDS_MAX,
    protected: Optional[set[str]] = None,
) -> None:
    """Record a cancelled request id, bounding memory growth FIFO.

    ``cancelled_ids`` holds membership (source of truth); ``order`` tracks
    insertion order for eviction. When the number of live ids exceeds ``cap``,
    the oldest *evictable* ids are dropped until back at the cap. Ids listed in
    ``protected`` (the active + still-queued requests) are never evicted, so a
    flood of unrelated cancels can never drop a live request's cancellation
    flag and let a cancelled queued call execute (HIGH). Idempotent: a repeated
    id is not re-appended. Kept module-level (not a closure) so the bound is
    unit-testable.
    """
    if rid in cancelled_ids:
        return
    cancelled_ids.add(rid)
    order.append(rid)
    protected = protected or set()
    # Two independent bounds keep the pair from leaking:
    #   1. Set bound -- evict oldest evictable ids once membership exceeds cap.
    #   2. Deque bound -- completion sites discard consumed ids from the *set*
    #      only (the deque is untouched), so without this second guard the
    #      deque would accumulate one stale entry per cancel and grow without
    #      limit even while the set stays small. Popping stale entries (already
    #      discarded from the set) is harmless.
    # Eviction skips protected ids (rotating them to the back); if only
    # protected ids remain, ``_evict_oldest_evictable`` returns False and we
    # stop -- a bounded overflow is preferable to executing a cancelled call.
    while len(cancelled_ids) > cap and order:
        if not _evict_oldest_evictable(cancelled_ids, order, protected):
            break
    while len(order) > cap:
        if not _evict_oldest_evictable(cancelled_ids, order, protected):
            break


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

# ── Private stdout descriptor for JSON-RPC responses ───────────────────────
# The vendored llama-cpp runtime wraps its multi-second GGUF model load in
# ``suppress_stdout_stderr``, which does a PROCESS-WIDE ``dup2(devnull, 1)``
# for the duration of the load (``_vendor/llama_cpp/_utils.py``). The first
# ``local_knowledge_search`` kicks that load on a background thread and returns
# a keyword-only result in milliseconds, so the JSON-RPC response for that very
# call races the load window. Written through fd 1, the bytes land in
# /dev/null: no exception, no short write, the SEL audit still records
# ``success`` -- and the client waits forever until the ACP tool-stall watchdog
# (``acp/client.py::_TOOL_STALL_TIMEOUT``, 600s) kills the turn.
#
# ``snapshot_stdout_fd()`` takes an ``os.dup(1)`` at server startup, BEFORE any
# tool can run. A dup'd descriptor keeps pointing at the original pipe no
# matter what a later ``dup2`` does to fd 1, so responses always reach the
# client. Guarded by a lock because it is a raw unbuffered fd: ``os.write`` is
# not atomic across interleaved callers, and a torn frame desyncs the stream
# for every subsequent message.
#
# NOTE: the suppressor also rebinds the ``sys.stdout`` OBJECT to a devnull
# file, so "has sys.stdout been swapped?" is NOT a usable liveness check -- it
# is false exactly inside the window we must survive. The snapshot is the only
# reliable route.
_stdout_fd: Optional[int] = None
_stdout_fd_lock = threading.Lock()


def snapshot_stdout_fd() -> Optional[int]:
    """Capture a private dup of the real stdout descriptor. Idempotent.

    Called once at ``run_mcp_stdio_loop`` entry. Returns the dup'd fd, or
    ``None`` when stdout is not fd-backed (pytest's captured stdout, an
    embedded host handing us a StringIO) -- in that case ``respond()`` falls
    back to ``sys.stdout`` exactly as before.
    """
    global _stdout_fd
    with _stdout_fd_lock:
        if _stdout_fd is not None:
            return _stdout_fd
        try:
            _stdout_fd = os.dup(sys.stdout.fileno())
        except (AttributeError, OSError, ValueError):
            # No usable fileno (StringIO / captured / closed stdout).
            _stdout_fd = None
        return _stdout_fd


def release_stdout_fd() -> None:
    """Close the private stdout dup, if one was captured. Idempotent.

    Keeps the descriptor from leaking when a loop is run repeatedly in one
    process (the test suite drives ``run_mcp_stdio_loop`` many times); a real
    server process exits after its single loop returns.
    """
    global _stdout_fd
    with _stdout_fd_lock:
        fd = _stdout_fd
        _stdout_fd = None
    if fd is not None:
        with contextlib.suppress(OSError):
            os.close(fd)


def _write_all(fd: int, payload: bytes) -> int:
    """Write every byte of ``payload`` to ``fd``; return the count written.

    ``os.write`` on a pipe may accept fewer bytes than offered; a silently
    truncated frame desyncs the JSON-RPC stream for every later message, so
    loop until the payload is fully handed over. Mirrors the short-read loop
    in :func:`_read_message`.

    On failure the ``OSError`` propagates, but the bytes written so far are
    attached as ``bytes_written`` so the caller can tell a clean failure (zero
    bytes — safe to retry on another stream) from a partial one (retrying would
    duplicate the prefix and tear the frame).
    """
    view = memoryview(payload)
    written = 0
    while view:
        try:
            n = os.write(fd, view)
        except OSError as exc:
            exc.bytes_written = written  # type: ignore[attr-defined]
            raise
        view = view[n:]
        written += n
    return written


# ── Managed tool policy cache ──────────────────────────────────────────────
# Keyed per SESSION: in the pooled topology one backend process serves many
# sessions (per-call identity via the caller-meta extension), so a single
# process-global set would apply the FIRST session's policy — or a cached
# fail-open — to every other session. Non-pooled
# backends see one key for the process lifetime.
# BOUNDED: a long-lived pooled backend serves churning sessions;
# FIFO-evict the oldest entry past the cap so the dict cannot grow without
# limit. Eviction only costs a re-fetch on that session's next call.
_EXCLUDED_TOOLS_CACHE_MAX = 256
_excluded_tools_by_session: dict[str, set[str]] = {}
# Two separate negative caches with different TTLs so the long-TTL
# HTTP-error path doesn't keep fail-open active when only a brief
# startup race triggered the failure.
_last_failure_time: float = 0.0           # gateway unreachable / non-404 HTTP error
_last_startup_race_time: float = 0.0      # no session key or 404 — recovers fast
_failure_count: int = 0
# Long TTL applies only when the gateway is genuinely unreachable
# (HTTP errors other than 404, connection refused, timeout).  Kept short
# (60s) to keep the MCP-level fail-open window narrow:
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


def _resolve_excluded_tools(caller_session: str = "") -> set[str]:
    """Query the gateway for the current session's managedToolPolicy.exclude.

    ``caller_session`` is the verified per-call identity from the gateway's
    caller-meta extension (pooled topology); when non-empty it takes
    precedence over the env/PID resolution below and keys the cache, so
    sessions sharing one backend cannot inherit each other's policy.

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
    global _last_failure_time, _last_startup_race_time, _failure_count
    _cached = _excluded_tools_by_session.get(caller_session)
    if _cached is not None:
        return _cached

    now = time.monotonic()
    # Negative cache: avoid hammering gateway on persistent failures.
    # Silent during the cache window — only the structured audit event is
    # emitted to keep gateway.log readable.  Two windows: a long one for
    # genuine HTTP/network failure, a short one for benign startup races.
    # The startup-race window exists for the NO-IDENTITY case (pid file not
    # yet visible); a caller WITH a verified per-call identity is past that
    # race by definition, so honoring the global short window for it would
    # fail-open an identified pooled session on another session's race
    # — skip it when caller_session is present.
    if (
        (_last_failure_time and (now - _last_failure_time) < _NEGATIVE_CACHE_TTL)
        or (
            not caller_session
            and _last_startup_race_time
            and (now - _last_startup_race_time) < _STARTUP_RACE_CACHE_TTL
        )
    ):
        sel().log_api_access(
            caller=caller_session or os.environ.get("KIROCREW_SESSION_KEY", "mcp"),
            operation="tool_policy.negative_cache_hit",
            outcome="fail_open",
            source="mcp_shared",
        )
        return set()

    try:
        cfg = KiroCrewConfig.load()
        _host, port = parse_dashboard_url(cfg.dashboard.url)
        api_base = f"http://localhost:{port}"

        # Credential for the port this function DIALS (parsed just above), not for
        # whichever gateway an ambient lookup would name -- those can differ on a
        # multi-gateway host, which is the desync being closed.
        secret = ""
        try:
            secret = read_local_secret(port)
        except Exception:
            pass

        # Resolve session key: the verified per-call caller identity wins
        # (pooled topology); env/PID resolution is the single-session path.
        session_key = caller_session or os.environ.get("KIROCREW_SESSION_KEY", "")
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
                    if system == "Windows":
                        # No ``ps`` on Windows: without this the fallback below
                        # always returned 0 and no session key could resolve.
                        win_ppid = platform_compat.get_ppid(pid)
                        return win_ppid if win_ppid > 0 else 0
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

            from kiro_crew.session_pid_sig import read_session_pid_txt

            cfg_dir = config_dir()
            # Sandbox launcher exports its own HOST pid (the pid the gateway
            # keys session_pid_<pid>.txt by) — direct lookup works even when
            # this process's pid view diverges from the host's (PID-namespace
            # sandboxing), where the ancestor walk below can never match.
            # Reads go through session_pid_sig's hardened reader (symlink
            # refusal, regular-file check, size bound) — same read discipline
            # as the strict verifier, minus the signature requirement.
            host_pid = os.environ.get("KIROCREW_HOST_PID", "")
            if host_pid.isdigit():
                session_key = read_session_pid_txt(host_pid, cfg_dir)
            if not session_key:
                pid = os.getppid()
                seen: set[int] = set()
                while pid > 1 and pid not in seen:
                    seen.add(pid)
                    session_key = read_session_pid_txt(pid, cfg_dir)
                    if session_key:
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
            with loopback_urlopen(req, timeout=5) as resp:
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
            resolved = {t for t in exclude if isinstance(t, str)}
        else:
            resolved = set()
        # FIFO bound: dicts preserve insertion order; drop the oldest
        # session's entry when full (pooled backends serve churning sessions).
        while len(_excluded_tools_by_session) >= _EXCLUDED_TOOLS_CACHE_MAX:
            _excluded_tools_by_session.pop(
                next(iter(_excluded_tools_by_session))
            )
        _excluded_tools_by_session[caller_session] = resolved
        return resolved
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
        frame = f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8") + payload
    else:
        frame = (body + "\n").encode("utf-8")

    # Preferred path: the private descriptor captured before any tool ran, so a
    # library's process-wide dup2 on fd 1 (see _stdout_fd above) cannot swallow
    # this response. Serialized -- os.write is unbuffered and a partial
    # interleave would tear the frame.
    with _stdout_fd_lock:
        # Re-read INSIDE the lock: a concurrent release_stdout_fd() between an
        # outside-the-lock read and the write could otherwise hand os.write a
        # closed descriptor whose number has already been recycled by another
        # open() -- sending JSON-RPC bytes into an unrelated file. Not reachable
        # from today's single-threaded dispatch, but the lock is already held
        # here, so pay nothing to make it structurally safe.
        fd = _stdout_fd
        if fd is not None:
            try:
                _write_all(fd, frame)
                return
            except OSError as exc:
                # The dup'd fd is unusable (client pipe closed). Fall back to
                # sys.stdout ONLY if nothing was written, so a genuinely broken
                # pipe surfaces the way it did before this indirection. After a
                # PARTIAL write, re-emitting the whole frame would duplicate the
                # prefix and desync the stream for every later message -- drop
                # it instead and let the client's own timeout handle the turn.
                if getattr(exc, "bytes_written", 0):
                    logger.error(
                        "Torn JSON-RPC frame for request %s: wrote %d of %d bytes "
                        "before %s; dropping rather than duplicating the prefix",
                        req_id,
                        exc.bytes_written,  # type: ignore[attr-defined]
                        len(frame),
                        exc.__class__.__name__,
                    )
                    return
    if _use_content_length:
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(frame.decode("utf-8"))
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
    *,
    advertise_caller_identity: bool = False,
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
    try:
        _run_stdio_dispatch_loop(
            server_name,
            server_version,
            list_tools_fn,
            call_tool_fn,
            advertise_caller_identity=advertise_caller_identity,
        )
    finally:
        set_internal_caller(_prior_caller)
        release_stdout_fd()


def _run_stdio_dispatch_loop(
    server_name: str,
    server_version: str,
    list_tools_fn: Callable[[], list[dict[str, Any]]],
    call_tool_fn: Callable[[str, dict[str, Any]], str],
    *,
    advertise_caller_identity: bool = False,
) -> None:
    """Read/dispatch body of :func:`run_mcp_stdio_loop`.

    Split out so the public entry point can own the stdout-snapshot lifecycle
    (capture before the first request, release on exit) without indenting the
    whole dispatch loop under a ``try``.
    """
    # In-flight tool execution state: at most one at a time (sequential dispatch).
    _current_req_id: Any = None
    _current_caller_key: str = ""
    _cancel_event: Optional[threading.Event] = None
    _worker_thread: Optional[threading.Thread] = None
    _result_lock = threading.Lock()
    _result_ready = threading.Event()
    _result_box: list = []  # [response_payload] or [] if cancelled
    _cancelled_ids: set = set()
    # Insertion-order tracker for _cancelled_ids so it can be pruned FIFO once
    # it reaches CANCELLED_IDS_MAX (prevents unbounded growth on long-lived
    # per-session MCP processes that receive many cancels). Bounding is done by
    # the module-level _remember_cancelled_id() so it is unit-testable.
    _cancelled_order: collections.deque[str] = collections.deque()

    _current_tool_name: str = ""
    _worker_audited: list = [False]  # [bool], guarded by _result_lock
    # tools/call requests received while a worker was busy, dispatched FIFO.
    _pending_calls: collections.deque[dict[str, Any]] = collections.deque()

    def _live_request_ids() -> set[str]:
        """Ids of the active + still-queued requests whose cancellation flags
        must survive FIFO eviction.

        If a flood of unrelated cancels evicted one of these before the
        dispatch loop consumed it (``str(req_id) in _cancelled_ids``), a
        cancelled queued call would execute -- for a destructive tool that is a
        data-mutation path. Passed to ``_remember_cancelled_id`` as protected.
        """
        ids: set[str] = set()
        if _current_req_id is not None:
            ids.add(str(_current_req_id))
        for _pc in _pending_calls:
            _pcid = _pc.get("id")
            if _pcid is not None:
                ids.add(str(_pcid))
        return ids

    def _sel_audit(
        outcome: str, tool_name: str, req_id: Any, session_key: str = ""
    ) -> None:
        """Emit a SEL audit event for a tool invocation outcome.

        ``session_key`` should be the request's parsed caller identity when
        available (pooled topology: the env var below attributes every
        outcome to ``mcp`` or the wrong session in a shared backend); the
        env read is the single-session fallback.

        SEL failure must not break the response path, but a missed audit
        record must be visible (security-controls guideline: callback
        failures are logged, never bare pass)."""
        try:
            sel().log_tool_invocation(
                session_key=session_key
                or os.environ.get("KIROCREW_SESSION_KEY", "mcp"),
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

    def _req_caller_key(request: dict) -> str:
        """Parsed caller session key from a request's ``_meta``, or ""."""
        try:
            ctx = CallerContext.from_meta(
                request.get("params", {}).get("_meta")
            )
            return ctx.session_key if ctx is not None else ""
        except Exception:
            return ""

    def _run_tool(
        req_id: Any,
        tool_name: str,
        tool_args: dict,
        cancel_evt: threading.Event,
        caller_ctx: "CallerContext | None" = None,
    ) -> None:
        """Worker thread: run tool, store result unless cancelled."""
        global _thread_cancel_event
        # Inject cancel event into thread-local so cooperative tools can check it
        _thread_cancel_event = cancel_evt
        # Install the verified per-call caller for identity resolvers. Safe as
        # a module slot: dispatch is strictly sequential (one worker at a
        # time, joined before the next dispatch).
        set_current_caller(caller_ctx)
        try:
            result_text = call_tool_fn(tool_name, tool_args)
        except ToolCancelled:
            # Tool cooperatively exited on cancel -- suppress response
            logger.info("tool cancelled for request %s", req_id)
            # SEL audit: cancelled tool invocations must emit audit events
            _sel_audit(
                "cancelled",
                tool_name,
                req_id,
                caller_ctx.session_key if caller_ctx else "",
            )
            _thread_cancel_event = None
            set_current_caller(None)
            _result_ready.set()
            return
        except Exception as exc:
            result_text = f"Error: {exc}"
            _tool_errored = True
        else:
            _tool_errored = False
        finally:
            _thread_cancel_event = None
            set_current_caller(None)
        # Audit decision is made atomically with the cancellation check, under
        # the same lock that guards response delivery: exactly ONE audit event
        # per request (a failed+late-cancel race must not emit two).
        with _result_lock:
            if not cancel_evt.is_set():
                _result_box.append(build_tool_response(result_text))
                if _tool_errored:
                    # Exception escaped call_tool_fn (may bypass its internal
                    # logging) -- audit the failure.
                    _sel_audit(
                        "failed",
                        tool_name,
                        req_id,
                        caller_ctx.session_key if caller_ctx else "",
                    )
                    _worker_audited[0] = True
            else:
                # Late-cancel race: tool finished (or errored) but cancel
                # arrived before delivery. From the client's perspective this
                # invocation was cancelled.
                _sel_audit(
                    "cancelled",
                    tool_name,
                    req_id,
                    caller_ctx.session_key if caller_ctx else "",
                )
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
                            _sel_audit(
                                "cancelled",
                                _current_tool_name,
                                _current_req_id,
                                _current_caller_key,
                            )
                        _result_box.clear()
                        # Consumed: drop the id so a completed request never
                        # lingers in the cancelled set.
                        _cancelled_ids.discard(str(_current_req_id))
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
                    _remember_cancelled_id(
                        _cancelled_ids,
                        _cancelled_order,
                        str(cancelled_rid),
                        protected=_live_request_ids(),
                    )
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
                        _req_caller_key(req),
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
                excluded = _resolve_excluded_tools(_req_caller_key(req))
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
                    _sel_audit(
                                "cancelled",
                                _current_tool_name,
                                _current_req_id,
                                _current_caller_key,
                            )
                _result_box.clear()
                # Consumed: drop the id so a completed request never lingers
                # in the cancelled set.
                _cancelled_ids.discard(str(_current_req_id))
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
            _caps: dict[str, Any] = {"tools": {"listChanged": False}}
            if advertise_caller_identity:
                # Pooled-operation opt-in for IDENTITY, not for pooling:
                # gatewayd injects the per-call ``_meta.kirocrew.caller`` block
                # only into a backend that advertised this capability, and
                # nothing declines to POOL one that did not (see
                # ``rewriter.UNPOOLABLE_SERVERS``, which is empty and documents
                # exactly that). So NOT advertising does not buy a per-session
                # spawn -- it buys a shared process whose dispatch-loop caller
                # slot never receives gateway-authored metadata, which is how a
                # session-scoped tool silently degrades to unattached behaviour.
                _caps["experimental"] = caller_identity_capability()
            respond(
                req_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": _caps,
                    "serverInfo": {"name": server_name, "version": server_version},
                },
            )
        elif method == "notifications/initialized":
            pass
        elif method == "notifications/cancelled":
            # Cancel for a request that already completed -- ignore. Route
            # through the bounded recorder (not a raw set.add) so this idle
            # path honors the FIFO cap and keeps ``_cancelled_ids`` and
            # ``_cancelled_order`` in lockstep -- a raw add would grow the set
            # past the cap while the deque lagged, later crashing the eviction
            # loop with an empty-deque popleft.
            params = req.get("params", {})
            cancelled_rid = params.get("requestId")
            if cancelled_rid is not None:
                _remember_cancelled_id(
                    _cancelled_ids,
                    _cancelled_order,
                    str(cancelled_rid),
                    protected=_live_request_ids(),
                )
        elif method == "tools/list":
            excluded = _resolve_excluded_tools(_req_caller_key(req))
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
            # Verified per-call identity (pooled topology): gatewayd strips
            # any client-forged ``kirocrew.caller`` block and injects its own
            # on every forwarded call, so a block present here is
            # gateway-authored. None in the non-pooled stdio topology.
            _caller_ctx = CallerContext.from_meta(params.get("_meta"))
            # A queued request may have been cancelled while waiting -- emit
            # no response (per MCP spec) but audit the cancellation.
            if req_id is not None and str(req_id) in _cancelled_ids:
                _sel_audit(
                    "cancelled",
                    tool_name,
                    req_id,
                    _caller_ctx.session_key if _caller_ctx else "",
                )
                continue
            # Defense-in-depth: reject calls to excluded tools even if
            # the LLM somehow attempts to call them (hallucination).
            # Per-call caller identity keys the policy in pooled backends.
            excluded = _resolve_excluded_tools(
                _caller_ctx.session_key if _caller_ctx else ""
            )
            if tool_name in excluded:
                sel().log_tool_invocation(
                    session_key=(
                        _caller_ctx.session_key
                        if _caller_ctx
                        else os.environ.get("KIROCREW_SESSION_KEY", "mcp")
                    ),
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
                # Exception handling mirrors the worker path: the client gets
                # an Error response and the failure is SEL-audited with the
                # caller identity (an escaped exception would kill the loop).
                set_current_caller(_caller_ctx)
                try:
                    result_text = call_tool_fn(tool_name, tool_args)
                except Exception as exc:
                    result_text = f"Error: {exc}"
                    _sel_audit(
                        "failed",
                        tool_name,
                        req_id,
                        _caller_ctx.session_key if _caller_ctx else "",
                    )
                finally:
                    set_current_caller(None)
                respond(req_id, build_tool_response(result_text))
            else:
                # Dispatch tool in worker thread so we can receive cancel notifications
                _cancel_event = threading.Event()
                _current_req_id = req_id
                _current_tool_name = tool_name
                _current_caller_key = (
                    _caller_ctx.session_key if _caller_ctx else ""
                )
                _worker_audited[0] = False
                _result_ready.clear()
                _result_box.clear()
                _worker_thread = threading.Thread(
                    target=_run_tool,
                    args=(req_id, tool_name, tool_args, _cancel_event, _caller_ctx),
                    daemon=True,
                )
                _worker_thread.start()
        elif req_id is not None:
            respond(req_id, None, error={"code": -32601, "message": f"Unknown method: {method}"})
