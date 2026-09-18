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
from typing import Any, Callable, NamedTuple, Optional

from kiro_crew import platform_compat
from kiro_crew.acp.types import JSONRPC_METHOD_NOT_FOUND
from kiro_crew.config.loader import KiroCrewConfig, config_dir, read_local_secret
from kiro_crew.dashboard.origin import parse_dashboard_url
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.mcp_caller import (
    CallerContext,
    caller_identity_capability,
    set_current_caller,
    set_current_tenant_nonce,
    tenant_nonce_from_meta,
)
from kiro_crew.sel import sel
from kiro_crew.session_directive import neutralize_markers
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


def member_proof_header_value(proof: str) -> str:
    """Return ``proof`` when it is safe to send as a header value, else ``""``.

    A gateway-minted member proof is ASCII alphanumerics plus ``-_.``; anything
    else (CR/LF, separators, non-ASCII) earns no header rather than a header
    injection surface. One charset rule for every proof-forwarding client.
    """
    if proof and proof.isascii() and all(c.isalnum() or c in "-_." for c in proof):
        return proof
    return ""


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


class ToolPolicy(NamedTuple):
    """The exclusion set for one session, plus whether it was actually read.

    ``excluded`` empty is ambiguous on its own: it means BOTH "the operator
    excluded nothing" and "we never got to look".  Those two demand opposite
    behaviour at a call site, so the reason we failed to look travels with the
    value instead of being flattened into an empty set.

    ``unresolved`` is the audit operation name of the path that gave up
    (``no_session_key``, ``agent_not_resolved``, ``policy_forbidden``,
    ``policy_unreadable``, ``resolution_failed``, or the cached form of one of
    them) and is ``""`` only when the gateway's policy endpoint actually
    answered with a policy this code understood.  Which reason it is decides
    what ``tools/call`` does -- see ``_UNRESOLVED_REFUSES_CALL`` -- so a new
    failure path must name its own reason rather than borrow one: borrowing
    inherits a decision that was made about a different condition, and every
    reason here that was ever collapsed into another one hid a different bug.
    """

    excluded: frozenset[str]
    unresolved: str


def _resolve_tool_policy(
    caller_session: str = "",
    *,
    member_memory_proof: str = "",
    ignore_negative_cache: bool = False,
) -> ToolPolicy:
    """Query the gateway for the current session's managedToolPolicy.exclude.

    ``caller_session`` is the verified per-call identity from the gateway's
    caller-meta extension (pooled topology); when non-empty it takes
    precedence over the env/PID resolution below and keys the cache, so
    sessions sharing one backend cannot inherit each other's policy.
    ``member_memory_proof`` belongs only to that request. It is forwarded to
    the policy endpoint and is never retained in a policy or connection cache.

    Returns the set of tool names to hide from this session, together with the
    reason the policy could not be read when it could not. Caches on success
    only, so a session that has ever resolved its policy is served from
    ``_excluded_tools_by_session`` and never reaches a failure path again
    (until FIFO eviction) -- the unresolved states below are reachable only for
    a session whose policy has never been read once.

    On failure this returns an EMPTY exclusion set with ``unresolved`` set, and
    the two consumers read that differently on purpose:

    - ``tools/list`` shows the unfiltered list. Hiding every tool here would be
      unrecoverable: kiro-cli calls ``tools/list`` once per session and caches
      the answer, so an empty list persists for the session's whole life. A
      listing is not an enforcement point.
    - ``tools/call`` REFUSES, loudly and attributably. This is the only place a
      withheld deny can actually be exercised, and refusing one call costs a
      retry rather than a session.

    An empty exclusion set therefore never stands alone as a permission: the
    audit event that records the failure is also what makes the fail-closed
    choice available without guessing which tools the operator meant to deny.
    """
    global _last_failure_time, _last_startup_race_time, _failure_count
    _cached = _excluded_tools_by_session.get(caller_session)
    if _cached is not None:
        return ToolPolicy(frozenset(_cached), "")

    now = time.monotonic()
    _failure_cached = bool(
        _last_failure_time and (now - _last_failure_time) < _NEGATIVE_CACHE_TTL
    )
    _race_cached = bool(
        not caller_session
        and _last_startup_race_time
        and (now - _last_startup_race_time) < _STARTUP_RACE_CACHE_TTL
    )
    # Negative cache: avoid hammering gateway on persistent failures.
    # Silent during the cache window -- only the structured audit event is
    # emitted to keep gateway.log readable.  Two windows: a long one for
    # genuine HTTP/network failure, a short one for benign startup races.
    # The startup-race window exists for the NO-IDENTITY case (pid file not
    # yet visible); a caller WITH a verified per-call identity is past that
    # race by definition, so honoring the global short window for it would
    # fail-open an identified pooled session on another session's race
    # -- skip it when caller_session is present.
    if not ignore_negative_cache and (_failure_cached or _race_cached):
        # WHICH clock fired is part of the answer, not an implementation
        # detail: the two windows cache different conditions and the call site
        # treats them differently. Collapsing them into one reason would repeat
        # the very conflation this resolver exists to undo, one level down.
        _reason = "resolution_failed" if _failure_cached else "no_session_key"
        sel().log_api_access(
            caller=caller_session or os.environ.get("KIROCREW_SESSION_KEY", "mcp"),
            operation="tool_policy.negative_cache_hit",
            outcome="unresolved",
            source="mcp_shared",
            resources=f"reason={_reason}",
        )
        return ToolPolicy(frozenset(), _reason)

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
        from kiro_crew.member_memory_auth import protected_member_session_for_pid

        protected = None if caller_session else protected_member_session_for_pid(os.getpid())
        session_key = caller_session or (
            protected if protected is not None else os.environ.get("KIROCREW_SESSION_KEY", "")
        )
        if not session_key and protected is None:

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
            # PATH 1/3. No session key resolvable (startup race -- kiro-cli
            # hasn't written the PID file yet, or the process is from the warm
            # pool).  The exclusion set stays empty so ``tools/list`` still
            # lists everything: kiro-cli calls tools/list once and caches the
            # result, so hiding all tools here would hide them for the whole
            # session, unrecoverably.  ``unresolved`` is what stops that empty
            # set from being read as a permission -- ``tools/call`` refuses
            # while it is set.  Short negative cache (5s) debounces the warning
            # storm during parallel MCP startup; the session_pid file typically
            # appears within a few hundred ms of MCP spawn.
            _last_startup_race_time = now
            sel().log_api_access(
                caller="mcp",
                operation="tool_policy.no_session_key",
                outcome="unresolved",
                source="mcp_shared",
            )
            return ToolPolicy(frozenset(), "no_session_key")

        headers: dict[str, str] = {"X-Internal-Secret": secret}
        headers["X-Session-Key"] = session_key
        proof_value = member_proof_header_value(member_memory_proof) if caller_session else ""
        if proof_value:
            from kiro_crew.member_memory_auth import PROOF_HEADER

            headers[PROOF_HEADER] = proof_value

        req = urllib.request.Request(
            f"{api_base}/api/session-tool-policy",
            headers=headers,
        )
        try:
            with loopback_urlopen(req, timeout=5) as resp:
                policy = json.loads(resp.read())
        except urllib.error.HTTPError as http_exc:
            if http_exc.code == 404:
                # PATH 2/3. 404 = "agent not resolved" (the gateway hasn't
                # registered this session yet -- common during MCP startup
                # before the session_pid file is fully visible across
                # processes).  A benign race, so the short startup-race cache
                # applies and the session recovers within seconds; until it
                # does, ``unresolved`` keeps ``tools/call`` refusing rather
                # than serving an exclusion set nobody read.  Critically, do
                # NOT log a stack trace for 404 -- it floods gateway.log on
                # every fresh subagent spawn.
                _last_startup_race_time = now
                sel().log_api_access(
                    caller=os.environ.get("KIROCREW_SESSION_KEY", "mcp"),
                    operation="tool_policy.agent_not_resolved",
                    outcome="unresolved",
                    source="mcp_shared",
                    resources=f"session_key={session_key}",
                )
                return ToolPolicy(frozenset(), "agent_not_resolved")
            if http_exc.code == 409:
                # The gateway read a spec for this session and could not
                # determine its policy (unparseable, wrong shape, or two specs
                # claiming the name). Deliberately NOT negative-cached: the
                # windows above exist to avoid repeated 5s urlopen timeouts, and
                # this answer is immediate, so there is nothing to debounce. It
                # is also specific to THIS session's agent spec, and
                # ``_last_failure_time`` is process-global -- caching it there
                # would refuse tool calls for every sibling session in a pooled
                # backend over one agent's malformed file. Re-asking each call
                # costs one loopback round-trip and recovers the moment the
                # operator fixes the spec.
                sel().log_api_access(
                    caller=session_key,
                    operation="tool_policy.unreadable",
                    outcome="unresolved",
                    source="mcp_shared",
                    resources=f"session_key={session_key}",
                )
                return ToolPolicy(frozenset(), "policy_unreadable")
            if http_exc.code in (400, 403):
                # The gateway ANSWERED and declined to tell this caller. 403 is
                # ``member_session_unverified`` from ``internal_memory_scope``:
                # a session claiming a private memory store without a proof the
                # gateway can verify. 400 is a caller that named no session or a
                # rejected agent name. Both are authorization boundaries the
                # gateway holds deliberately, they answer instantly, and they
                # are the permanent steady state for a whole class of callers
                # rather than a window that closes.
                #
                # Kept OUT of the process-global failure cache for the same
                # reason as 409: the answer is immediate so there is nothing to
                # debounce, and it is specific to THIS caller's identity, so
                # caching it globally would deny sibling sessions over one
                # caller's missing proof.
                sel().log_api_access(
                    caller=session_key,
                    operation="tool_policy.forbidden",
                    outcome="unresolved",
                    source="mcp_shared",
                    resources=f"session_key={session_key},status={http_exc.code}",
                )
                return ToolPolicy(frozenset(), "policy_forbidden")
            if 400 <= http_exc.code < 500:
                # Any OTHER 4xx. The gateway answered and made a decision about
                # this caller -- it is enforcing something, not failing to read
                # the policy -- so it joins the permissive, audited class with
                # the two above.
                #
                # This arm exists because the alternative is an enumerated list
                # of statuses, and a list is only as good as its author's
                # knowledge of the endpoint. A status nobody enumerated would
                # fall through to the failure catch-all and be refused, which
                # would deny a whole class of callers over a condition that is
                # not a failure at all. Deciding by CLASS is robust to the
                # endpoint growing a status this code has never seen.
                logger.warning(
                    "Tool policy endpoint answered %s for session %s; treating it "
                    "as a boundary the gateway holds, not a failure to read",
                    http_exc.code, session_key,
                )
                sel().log_api_access(
                    caller=session_key,
                    operation="tool_policy.forbidden",
                    outcome="unresolved",
                    source="mcp_shared",
                    resources=f"session_key={session_key},status={http_exc.code}",
                )
                return ToolPolicy(frozenset(), "policy_forbidden")
            raise

        exclude = policy.get("exclude", [])
        # A policy the gateway ANSWERED is not automatically a policy this
        # understood. An absent ``exclude`` key is a real empty exclusion list
        # and stays resolved -- that is the ordinary case. But a present
        # ``exclude`` of the wrong shape, or one holding an entry that is not a
        # tool name, is a policy whose meaning is unknown: the operator wrote
        # something there and it cannot be read. Silently narrowing it to the
        # part that happens to parse would enforce a policy nobody wrote, which
        # is the same withheld deny as reading none at all. Neither shape occurs
        # in a valid config, so refusing costs no working caller. The gateway
        # takes the identical line one level up for a non-dict
        # ``managedToolPolicy``.
        if not isinstance(exclude, list) or any(not isinstance(t, str) for t in exclude):
            _shape = (
                "not a list"
                if not isinstance(exclude, list)
                else "a list holding a non-string entry"
            )
            logger.warning(
                "Tool policy for session %s has a malformed exclude (%s); "
                "refusing calls rather than enforcing the part that parses",
                session_key, _shape,
            )
            sel().log_api_access(
                caller=session_key,
                operation="tool_policy.unreadable",
                outcome="unresolved",
                source="mcp_shared",
                resources=f"session_key={session_key},exclude={_shape}",
            )
            return ToolPolicy(frozenset(), "policy_unreadable")
        resolved = set(exclude)
        # FIFO bound: dicts preserve insertion order; drop the oldest
        # session's entry when full (pooled backends serve churning sessions).
        while len(_excluded_tools_by_session) >= _EXCLUDED_TOOLS_CACHE_MAX:
            _excluded_tools_by_session.pop(
                next(iter(_excluded_tools_by_session))
            )
        _excluded_tools_by_session[caller_session] = resolved
        return ToolPolicy(frozenset(resolved), "")
    except Exception as exc:
        # PATH 3/3. The gateway did not give a usable answer: no answer at all
        # (network error, timeout, connection refused), or a 5xx saying it is
        # broken. Every 4xx returns above, decided by CLASS rather than by an
        # enumerated list, so reaching here means the gateway could not read the
        # policy -- never that it made a decision about this caller.
        #
        # That single meaning is what a future decision to refuse on it would
        # rest on; today it stays permissive for the measured reason recorded at
        # ``_UNRESOLVED_REFUSES_CALL``. The LONG negative cache avoids repeated 5s
        # urlopen blocks across many MCP servers while the gateway is down. That
        # cache is process-global, which is sound HERE and nowhere else: a gateway
        # this process cannot reach is unreachable for every session in it, so
        # there is no sibling the window wrongly affects.
        _last_failure_time = time.monotonic()
        _failure_count += 1
        # Suppress repeated warnings — once we've logged twice the operator
        # has all the diagnostic info and further entries flood gateway.log
        # at every MCP server startup (10+ servers × every session start).
        if _failure_count <= _MAX_WARNING_FAILURES:
            logger.warning(
                "Tool policy resolution failed (%s); this session's exclusions are "
                "unknown and tool calls are NOT filtered for up to %.0fs",
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
            outcome="unresolved",
            source="mcp_shared",
        )
        return ToolPolicy(frozenset(), "resolution_failed")


# Which unresolved reasons refuse a ``tools/call``.
#
# The test is not how bad the reason sounds. It is whether the reason means ONE
# thing, because a security decision derived from an ambiguous reason is wrong
# for half the callers it hits. Two reasons qualify, and both mean "an operator
# exclusion may exist and this system could not read it":
#
# * ``policy_unreadable`` -- the gateway found a spec for this session and could
#   not determine its policy. It emits this for that condition and nothing else.
# ``resolution_failed`` -- no usable answer, meaning nothing came back or a 5xx
# said the gateway is broken -- is the one reason where this code says something
# different from what the security argument alone would say, so the reason is
# recorded here rather than left to a reader to reconstruct.
#
# On the argument, refusing is right: an operator exclusion may exist and the
# process that holds it cannot answer for it. It was implemented that way and
# measured, and the repository's real-MCP end-to-end lane refuses to run a
# legitimate first tool call under it -- five heads with it refusing all fail that
# lane, the two with it permissive both pass. So in this deployment a legitimate
# call reaches this arm, which means refusing here does not cost an attacker a
# tool call, it costs an ordinary caller every tool call. The window stays
# permissive and audited until the reason a real call lands here is understood;
# that is a gateway-side question, not one this read can answer.
#
# The remaining reasons stay permissive because each covers a caller for whom no
# operator exclusion is known to exist, or a class for which refusal is permanent
# rather than a window that closes:
#
# * ``agent_not_resolved`` is the gateway's 404, returned BOTH for a session
#   still registering (a policy may exist) and for a caller it can never map to
#   an agent (no policy can exist). Refusing denies the second class forever.
# * ``no_session_key`` is the same gap inside this process: no agent is named, so
#   no operator exclusion is known to exist for the call to bypass.
# * ``policy_forbidden`` is ANY 4xx. The gateway answered and made a decision
#   about this caller -- for 403 ``member_session_unverified`` that is the steady
#   state of a session claiming a private store without a verifiable proof.
#   Refusing would deny such a class permanently, and a boundary the gateway is
#   enforcing is not a boundary it failed to read. The test is the status class,
#   not a list of names, so an unfamiliar 4xx is permissive-and-audited rather
#   than silently reclassified as an outage.
#
# Those permissive windows are audited per call, so they are visible instead of
# silent -- which is what made this condition hard to find. Closing them needs
# the 404 to distinguish registering from unmappable, which is a change to the
# endpoint's contract rather than to this read.
_UNRESOLVED_REFUSES_CALL = frozenset({"policy_unreadable"})


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
        # No ``tool_kind``: it is a CLASSIFICATION of the invocation -- callers
        # that write their own rows pass things like "authz" -- and this wrapper
        # has no per-tool taxonomy to supply. Passing ``session_key`` here would be
        # both wrong and redundant, since ``caller_identity`` on the same
        # record already carries it: every row written through
        # here, across all five MCP servers, would hold a high-cardinality session
        # key where a kind belongs, making the field useless to filter or
        # aggregate on while still looking populated. The parameter
        # defaults to "", and an honestly empty kind beats a false one.
        sel().log_tool_invocation(
            session_key=session_key,
            source="mcp",
            tool_name=name,
            outcome="failed",
            downstream_service=downstream_service,
            error=str(e),
        )
        # A rejection is BY CONSTRUCTION not a directive, and this message
        # interpolates content this process does not control: an unknown-field
        # error echoes the argument KEY, so a key carrying the directive sentinel
        # plus a JSON payload plus a newline turns a rejected call into a decodable
        # directive under the genuine tool's authenticated identity - applying the
        # arguments validation had just refused. Defang here, where "this is an
        # error" is known, rather than centrally where the real marker lives.
        return f"Error: {neutralize_markers(str(e))}"

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
        # No ``tool_kind`` -- see the ValidationError path above.
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
            except ValueError:
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

    def _req_caller(request: dict) -> "CallerContext | None":
        """Current request identity, without borrowing the active worker's caller."""
        try:
            return CallerContext.from_meta(request.get("params", {}).get("_meta"))
        except Exception:
            return None

    def _req_caller_key(request: dict) -> str:
        ctx = _req_caller(request)
        return ctx.session_key if ctx is not None else ""

    def _caller_tool_policy(caller: "CallerContext | None") -> ToolPolicy:
        """This request's exclusion set AND whether the policy was readable.

        The enforcement seam: the ``tools/call`` branch needs the second half
        to tell "the operator excluded nothing" apart from "we could not read
        what the operator excluded".
        """
        if caller is not None and caller.from_gateway and caller.member_memory_proof:
            return _resolve_tool_policy(
                caller.session_key, member_memory_proof=caller.member_memory_proof
            )
        return _resolve_tool_policy(caller.session_key if caller else "")

    def _listable_tools(caller: "CallerContext | None") -> list[dict]:
        """The tool list for a ``tools/list``, minus this session's exclusions.

        An unresolved policy lists EVERYTHING on purpose. kiro-cli calls
        ``tools/list`` once per session and caches the answer, so hiding tools
        on a transient policy failure would hide them for the session's whole
        life. Listing is not the enforcement point; ``tools/call`` is, and it
        refuses while the policy is unresolved. The audit event records that a
        listing went out unfiltered so the widened window is attributable.
        """
        policy = _caller_tool_policy(caller)
        tools = list_tools_fn()
        if policy.unresolved:
            try:
                sel().log_api_access(
                    caller=(
                        caller.session_key
                        if caller is not None
                        else os.environ.get("KIROCREW_SESSION_KEY", "mcp")
                    ),
                    operation="tool_policy.unfiltered_listing",
                    outcome="unresolved",
                    source="mcp",
                    resources=f"reason={policy.unresolved}",
                )
            except Exception as sel_exc:
                logger.warning("SEL audit failed for unfiltered listing: %s", sel_exc)
            return tools
        if policy.excluded:
            tools = [t for t in tools if t.get("name") not in policy.excluded]
        return tools

    def _run_tool(
        req_id: Any,
        tool_name: str,
        tool_args: dict,
        cancel_evt: threading.Event,
        caller_ctx: "CallerContext | None" = None,
        tenant_nonce: str = "",
    ) -> None:
        """Worker thread: run tool, store result unless cancelled."""
        global _thread_cancel_event
        # Inject cancel event into thread-local so cooperative tools can check it
        _thread_cancel_event = cancel_evt
        # Install the verified per-call caller for identity resolvers. Safe as
        # a module slot: dispatch is strictly sequential (one worker at a
        # time, joined before the next dispatch).
        set_current_caller(caller_ctx)
        # And the connection's namespace separator, which is present even when
        # the caller is not: a tool that keys per-tenant state for a caller the
        # gateway could not name reads it instead of a process-global fallback.
        # Cleared in the same places as the caller.
        set_current_tenant_nonce(tenant_nonce)
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
            set_current_tenant_nonce("")
            _result_ready.set()
            return
        except Exception as exc:
            result_text = f"Error: {neutralize_markers(str(exc))}"  # not a directive: see call_tool_with_logging
            _tool_errored = True
        else:
            _tool_errored = False
        finally:
            _thread_cancel_event = None
            set_current_caller(None)
            set_current_tenant_nonce("")
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
                respond(req_id, {"tools": _listable_tools(_req_caller(req))})
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
            respond(req_id, {"tools": _listable_tools(_req_caller(req))})
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
            # The connection's namespace separator. Parsed separately because it
            # arrives WITHOUT an identity for a caller the gateway could not
            # name — the case it exists for — so it cannot be folded
            # into ``_caller_ctx``, which is None exactly then.
            _tenant_nonce = tenant_nonce_from_meta(params.get("_meta"))
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
            _policy = _caller_tool_policy(_caller_ctx)
            _policy_session = (
                _caller_ctx.session_key
                if _caller_ctx
                else os.environ.get("KIROCREW_SESSION_KEY", "mcp")
            )
            if _policy.unresolved and _policy.unresolved not in _UNRESOLVED_REFUSES_CALL:
                # An identity reason: no agent was named, so no operator
                # exclusion is known to exist for this call to bypass. The call
                # proceeds, and the window is audited so it is visible rather
                # than silent -- which is what made this condition hard to see.
                try:
                    sel().log_api_access(
                        caller=_policy_session,
                        operation="tool_policy.unenforced_call",
                        outcome="unresolved",
                        source="mcp",
                        resources=f"reason={_policy.unresolved},tool={tool_name}",
                    )
                except Exception as sel_exc:
                    logger.warning("SEL audit failed for unenforced call: %s", sel_exc)
            if _policy.unresolved in _UNRESOLVED_REFUSES_CALL:
                # Fail CLOSED. The operator's exclusion list could not
                # be read, so we do not know whether THIS tool is denied, and
                # an unknown deny is not a permission. Refusing costs the
                # caller a retry once the policy resolves (a startup race
                # clears in milliseconds); serving the call would have silently
                # widened every exclusion the operator set.
                #
                # This is the enforcement point, and the only one: the
                # ``tools/list`` branch deliberately still lists everything,
                # because kiro-cli caches one listing per session and an empty
                # one would be unrecoverable. A tool that is listed but refuses
                # is not a hole; a tool that is unlisted but runs is.
                logger.warning(
                    "Refusing tool call %r: session %s tool policy unresolved (%s)",
                    tool_name, _policy_session, _policy.unresolved,
                )
                sel().log_tool_invocation(
                    session_key=_policy_session,
                    source="mcp",
                    tool_name=tool_name,
                    tool_kind=server_name,
                    outcome="rejected_policy_unresolved",
                    error=f"managedToolPolicy.unresolved:{_policy.unresolved}",
                )
                respond(
                    req_id,
                    build_tool_response(
                        f"Error: tool '{tool_name}' is unavailable because this "
                        f"session's tool policy could not be read "
                        f"({_policy.unresolved}); refusing the call rather than "
                        f"ignoring an operator's exclusion list. Retry shortly."
                    ),
                )
            elif tool_name in _policy.excluded:
                sel().log_tool_invocation(
                    session_key=_policy_session,
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
                set_current_tenant_nonce(_tenant_nonce)
                try:
                    result_text = call_tool_fn(tool_name, tool_args)
                except Exception as exc:
                    result_text = f"Error: {neutralize_markers(str(exc))}"  # not a directive: see call_tool_with_logging
                    _sel_audit(
                        "failed",
                        tool_name,
                        req_id,
                        _caller_ctx.session_key if _caller_ctx else "",
                    )
                finally:
                    set_current_caller(None)
                    set_current_tenant_nonce("")
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
                    args=(
                        req_id,
                        tool_name,
                        tool_args,
                        _cancel_event,
                        _caller_ctx,
                        _tenant_nonce,
                    ),
                    daemon=True,
                )
                _worker_thread.start()
        elif req_id is not None:
            respond(
                req_id,
                None,
                error={
                    "code": JSONRPC_METHOD_NOT_FOUND,
                    "message": f"Unknown method: {method}",
                },
            )
