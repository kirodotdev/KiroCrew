"""MCP server exposing spawn, learn, and task tools to kiro-cli.

Runs as ``kirocrew mcp-core`` — kiro-cli spawns it as a child process
and calls tools via JSON-RPC over stdio (MCP protocol).

Tools:
    spawn_run       — spawn a background subagent
    spawn_list      — list running/completed subagents
    spawn_status    — retrieve full subagent output
    resource_status — check host resource headroom before heavy work
    learn_add       — save a learned correction
    learn_list      — list all lessons
    learn_remove    — remove lessons by substring
    task_run        — start the autonomous task runner
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re as _re
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.agent_discovery import list_agents
from kiro_crew.autonudge import binding_key_for
from kiro_crew.config.loader import (
    KiroCrewConfig,
    config_dir,
    outbox_dir,
    read_local_secret,
    resolve_agent_bindings,
)
from kiro_crew.context_management import summarize_result
from kiro_crew.dashboard.origin import dashboard_socket_path
from kiro_crew.history import _SEARCH_SCAN_WINDOW as SEARCH_SCAN_WINDOW
from kiro_crew.history import ConversationLog, is_incognito_transcript, snippet_needles
from kiro_crew.knowledge.dedup import dedup_sweep
from kiro_crew.knowledge.embedder import create_embedder_from_config
from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.mcp_caller import current_caller
from kiro_crew.mcp_shared import (
    call_tool_with_logging,
    internal_caller,
    run_mcp_stdio_loop,
)
from kiro_crew.mcp_tools import build_tool_list, dispatch
from kiro_crew.members import record_activity
from kiro_crew.messaging.link import is_legacy_slack_key, legacy_key
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.port_resolution import resolve_client_port_src
from kiro_crew.security import (
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.skills import SkillsLoader
from kiro_crew.validation import (
    MCP_CORE_SCHEMAS,
    validate_tool_args,
)

# Bindings the tool handlers in ``mcp_tools`` read as attributes of THIS module
# rather than importing themselves, so that a test rebinding one --
# ``patch("kiro_crew.mcp_core.sel")``, ``patch("kiro_crew.mcp_core.time")`` --
# still intercepts the handler. Naming them here keeps that dependency visible
# and stops a future import cleanup from deleting a binding nothing in this file
# appears to use.
_HANDLER_SURFACE = (
    HybridRetriever,
    SkillsLoader,
    dedup_sweep,
    list_agents,
    outbox_dir,
    sel,
    summarize_result,
    time,
)


def _resolve_api_port() -> tuple[int, str]:
    """Resolve the gateway API port a callback should aim at.

    Delegates to :func:`kiro_crew.port_resolution.resolve_client_port_src`, the
    same precedence every client CLI command applies: ``KIROCREW_PORT``, then a
    port **explicitly written** in ``dashboard.url``, then the sole
    gateway-owned run-marker, then the documented default. The marker step is
    what keeps a portless ``dashboard.url`` from collapsing to the default
    port while a live gateway is bound elsewhere — ``parse_dashboard_url``
    substitutes the default for the *server's* benefit (it must bind
    something), which is exactly the wrong guess for a client callback.

    Returns ``(port, source)`` — the source string names the chain step that
    produced the port (``"env"`` / ``"bound"`` / ``"config"`` / ``"marker"`` /
    ``"default"``), because the caching rule below differs by source.
    """
    return resolve_client_port_src(None)


#: Port sources stable for the process lifetime, safe to pin. Deliberately
#: excludes ``"marker"``: a marker-discovered port is verified at that instant
#: only — the gateway can exit or move and any local process may then rebind
#: the port, so every secret-bearing request must re-run the discovery chain
#: (which re-proves ownership via ``_gateway_owns_port``) instead of trusting
#: a pinned value. Also excludes ``"default"``, the no-evidence fall-through.
_STABLE_PORT_SOURCES = frozenset({"cli", "env", "bound", "config"})


# Lazily-resolved caches for the gateway API port, base URL, and unix-socket
# path. ``None`` means "not resolved yet"; the getters below fill them on
# first use (tests may pre-seed any of them with a concrete value).
# Deliberately NOT computed at import: resolution reads config and the
# gateway's live run-marker, both of which can change between process start
# and the first gateway call — an import-time snapshot froze a wrong guess
# for the whole process lifetime. The URL and the socket path both derive
# from the single ``_API_PORT`` resolution, so the two transports can never
# name different gateways.
_API_PORT: int | None = None
_API: str | None = None
_API_UNIX_SOCKET: str | None = None


def _api_port() -> int:
    """Gateway API port, resolved on first use; pinned only on stable evidence.

    Only a *stable* source — env var, exported bound port, or a port the user
    wrote in ``dashboard.url`` — is cached: those are user decisions that hold
    for the process lifetime. Two resolutions are returned but NOT cached:

    * The default fall-through: it only proves nothing was discoverable at
      that instant. During gateway boot a broker-descended server can race
      the asynchronous run-marker write — pinning that fall-through would
      freeze the wrong port for the process lifetime, with restart as the
      only recovery. The next call re-resolves and picks the marker up once
      it exists.
    * A **marker-discovered** port: ownership was proven for that instant,
      not forever. The gateway can exit or move ports, after which any local
      process — another user's included — may rebind the port; a pinned
      marker resolution would keep sending the internal secret there. Every
      call therefore re-runs the discovery chain, whose marker step re-proves
      ownership (``_gateway_owns_port``) before the port is trusted again.
    """
    global _API_PORT
    if _API_PORT is None:
        port, source = _resolve_api_port()
        if source not in _STABLE_PORT_SOURCES:
            return port
        _API_PORT = port
    return _API_PORT


def _api_base() -> str:
    """Gateway API base URL, resolved on first use and cached.

    Pinned to the IPv4 literal ``127.0.0.1`` rather than ``localhost``: these
    requests carry ``X-Internal-Secret``, and a hostname lookup could resolve
    to ``::1`` where a different (possibly foreign) process may be listening —
    the gateway itself binds IPv4 loopback. Mirrors ``cli_server._CLI_LOOPBACK``.
    """
    global _API
    if _API is None:
        base = f"http://127.0.0.1:{_api_port()}"
        if _API_PORT is None:
            # Port resolution fell through to the default — do not pin a URL
            # built on no evidence (see _api_port).
            return base
        _API = base
    return _API


def _api_unix_socket() -> str:
    """Path of the gateway's internal-API unix socket (may not exist yet).

    Preferred transport for every gateway API request: connecting through it
    lets the gateway kernel-verify (``SO_PEERCRED`` + /proc ancestry) that
    this process actually belongs to the session its ``X-Session-Key`` header
    declares, instead of taking the header on faith. ``loopback_urlopen``
    checks existence per call and falls back to TCP when the file is absent
    (Windows, older gateway, bind failure) or nobody answers on it, so
    caching the path after the first resolution — mirroring ``_api_base`` —
    is safe. Derived from the same cached ``_api_port`` resolution as the API
    base so both transports always aim at the same gateway.
    """
    global _API_UNIX_SOCKET
    if _API_UNIX_SOCKET is None:
        try:
            path = str(dashboard_socket_path(_api_port()))
        except Exception:
            _API_UNIX_SOCKET = ""
            return _API_UNIX_SOCKET
        if _API_PORT is None:
            # Same no-evidence rule as _api_base: usable now, not pinned.
            return path
        _API_UNIX_SOCKET = path
    return _API_UNIX_SOCKET


def _api_urlopen(req: urllib.request.Request | str, timeout: float):
    """``loopback_urlopen`` against the API base with the unix-socket preference."""
    return loopback_urlopen(req, timeout=timeout, unix_socket_path=_api_unix_socket() or None)


def _invalidate_api_base() -> None:
    """Forget the pinned port, base and socket path so the next call re-resolves.

    Called when a connection is refused: the pinned base can predate a gateway
    that has since moved (or a config edit that retargeted it), and the current
    port is recorded only in the live run-marker. Dropping all three caches
    together keeps the invariant that both transports derive from one
    resolution — clearing only the URL would leave the socket path aimed at the
    old gateway.
    """
    global _API_PORT, _API, _API_UNIX_SOCKET
    _API_PORT = None
    _API = None
    _API_UNIX_SOCKET = None


# How often a sleeping `wait` polls /api/session-keepalive.
#
# Two jobs in one round-trip: keeping the session's activity clock warm (the
# staleness watchdog alone would be satisfied by 60s) and collecting an
# early-end request from the dashboard. The second job sets the value -- it is
# the upper bound on how long the "End wait" button appears to do nothing, so
# it is deliberately tightened to the loop's own sleep granularity. The handler
# it calls only touches two timestamps, so a 30-minute wait costs ~360 loopback
# POSTs and no meaningful work.
WAIT_PING_SECS = 5.0

# Ping cadence for a sleep that cannot publish (identity not authoritative, so no
# countdown and no button). Only the staleness watchdog cares at that point, and
# 60s satisfies it -- the same interval spawn_sub_agents' blocking poll uses.
WAIT_STALENESS_PING_SECS = 60.0

# Context cap for one `skill_fetch` body. The gateway's preview endpoint
# already caps at 64 KiB for the dashboard's detail panel; a tool result is
# spent from the conversation's context budget instead, so cap it again
# lower. 32 KiB (~8k tokens) covers essentially every real SKILL.md while
# keeping one fetch from dominating the window.
_SKILL_FETCH_MAX_CHARS = 32 * 1024


def _compress_snapshot_to_outline(snapshot: str, max_lines: int = 100) -> str:
    """Compress a full accessibility snapshot into a compact outline.

    Keeps: headings, links, buttons, inputs, images with alt text, and
    structural landmarks. Strips: empty containers, decorative elements,
    redundant whitespace. Returns element refs so agent can interact
    without re-reading the full snapshot.
    """
    if not snapshot:
        return "Empty snapshot — page may not have loaded."

    lines = snapshot.split("\n")
    keep_patterns = _re.compile(
        r"(heading|link|button|textbox|combobox|checkbox|radio|tab|menu"
        r"|img|image|navigation|main|banner|contentinfo|search|alert"
        r"|dialog|listitem|row|cell|ref=)"
    )
    outline: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped == "-":
            continue
        if keep_patterns.search(stripped.lower()):
            indent = len(line) - len(line.lstrip())
            compact_indent = "  " * min(indent // 2, 4)
            outline.append(f"{compact_indent}{stripped}")
            if len(outline) >= max_lines:
                outline.append(f"... (truncated at {max_lines} lines)")
                break

    if not outline:
        total = len([ln for ln in lines if ln.strip()])
        return f"No interactive elements found in snapshot ({total} total lines). Try browser_snapshot with a more specific target."

    return f"Page outline ({len(outline)} elements):\n" + "\n".join(outline)


def _search_snapshot(snapshot: str, query: str, max_results: int = 50) -> str:
    """Search a snapshot for lines matching a query pattern."""
    if not snapshot:
        return "Empty snapshot."
    if not query:
        return "Error: query is required"

    try:
        pattern = _re.compile(query, _re.IGNORECASE)
    except _re.error:
        pattern = _re.compile(_re.escape(query), _re.IGNORECASE)

    lines = snapshot.split("\n")
    matches: list[str] = []
    for i, line in enumerate(lines, 1):
        if pattern.search(line):
            matches.append(f"L{i}: {line.strip()}")
            if len(matches) >= max_results:
                break

    if not matches:
        return f"No matches for '{query}' in snapshot ({len(lines)} lines)."

    return f"Found {len(matches)} matches:\n" + "\n".join(matches)


def _list_tools() -> list[dict[str, Any]]:
    """Tool descriptors served for ``tools/list``.

    Declared per domain under :mod:`kiro_crew.mcp_tools`; this stays the
    entry point kiro-cli and in-process discovery both read.
    """
    return build_tool_list()


def _internal_secret() -> str:
    """Credential for the gateway this client will dial, paired to its port.

    Thin wrapper over ``config.loader.read_local_secret``, which owns the
    per-listener-then-shared order. The port is passed rather than re-resolved
    because ``_api_port`` already resolved and cached it for this process.
    """
    try:
        return read_local_secret(_api_port())
    except Exception:
        return ""


def _ppid_via_libproc(pid: int) -> int:
    """macOS parent-PID lookup via libproc's ``proc_pidinfo`` (stdlib ctypes).

    macOS has no ``/proc``, and the app sandbox denies spawning ``ps``
    (``Operation not permitted``). ``proc_pidinfo`` is an information syscall
    (no ``exec``), so the sandbox allows it — the same primitive psutil uses,
    but with zero third-party dependency. Returns 0 on any failure so the caller
    can fall back.
    """
    import ctypes
    import struct

    proc_pidtbsdinfo = 3
    # sizeof(struct proc_bsdinfo) is 232 on 64-bit Darwin; over-allocate.
    buf_size = 256
    try:
        libproc = ctypes.CDLL("libproc.dylib", use_errno=True)
        libproc.proc_pidinfo.restype = ctypes.c_int
        libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        buf = ctypes.create_string_buffer(buf_size)
        n = libproc.proc_pidinfo(pid, proc_pidtbsdinfo, 0, buf, buf_size)
        # pbi_ppid is the 5th uint32 (offset 16); need at least that many bytes.
        if n <= 16:
            return 0
        # struct proc_bsdinfo starts: pbi_flags, pbi_status, pbi_xstatus,
        # pbi_pid, pbi_ppid (5 x uint32) — pbi_ppid is index 4.
        return int(struct.unpack_from("<5I", buf.raw, 0)[4])
    except Exception:
        return 0


def _get_ppid(pid: int) -> int:
    """Get parent PID cross-platform. Returns 0 on failure.

    Standard-library only — deliberately NO third-party dependency (e.g.
    psutil), so the shipped app needs nothing extra bundled or code-signed and
    works across OS versions out of the box.

    - Linux: read ``/proc/<pid>/status`` (plain file read).
    - macOS: ``proc_pidinfo`` via libproc (see ``_ppid_via_libproc``). The old
      code shelled out to ``ps`` here, which the macOS app sandbox denies
      (``Operation not permitted``) — that broke the ancestor PID-walk in
      ``_resolve_session_key``, leaving spawned sub-agents unable to resolve
      their parent session key (empty ``KIROCREW_SESSION_KEY``) and surfacing
      spurious tool-approval cards on trusted sessions. libproc needs no
      ``exec``, so it works under the sandbox.
    - Windows: ``CreateToolhelp32Snapshot`` via
      ``platform_compat.get_ppid``. Without this branch the walk fell through
      to ``ps``, which does not exist on Windows, so every lookup returned 0
      and ``_resolve_session_key`` could never resolve a key -- silently
      breaking every session-keyed tool (``learn_add``, cron management,
      callback delivery) with ``missing X-Session-Key``.
    - Other/unknown platforms: fall back to ``ps`` (may be blocked, then 0).
    """
    system = platform.system()
    try:
        if system == "Windows":
            ppid = platform_compat.get_ppid(pid)
            return ppid if ppid > 0 else 0
        if system == "Linux":
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("PPid:"):
                    return int(line.split()[1])
        elif system == "Darwin":
            ppid = _ppid_via_libproc(pid)
            if ppid:
                return ppid
        # Last-resort fallback (unknown platform, or a libproc/proc miss): ``ps``.
        # May be sandbox-blocked, in which case this raises and we return 0.
        out = subprocess.check_output(["ps", "-o", "ppid=", "-p", str(pid)], text=True, timeout=2)
        return int(out.strip())
    except Exception:
        pass
    return 0


# ── Knowledge-search store/embedder cache ──
#
# local_knowledge_search runs per LLM tool call in a long-lived MCP server.
# Rebuilding KnowledgeStore every call re-runs the schema DDL, an orphan-cleanup
# DELETE transaction, and a full SELECT of all entities/relations into the
# in-memory graph; rebuilding the embedder re-runs the model availability probe
# (up to 3s when configured). We cache both, keyed on a signature of the DB
# files (main + -wal, since WAL commits land in -wal) and config.json, so
# out-of-band dashboard ingestion or config edits trigger a rebuild on the next
# call. The MCP stdio loop services calls serially, but a lock keeps this safe
# if that ever changes.
_KNOWLEDGE_CACHE_LOCK = threading.Lock()
# (signature_tuple, KnowledgeStore, embedder_or_None)
_KNOWLEDGE_CACHE: tuple[tuple, Any, Any] | None = None


def _knowledge_db_signature(db_path: Path, cfg_path: Path) -> tuple:
    """Cheap fingerprint of the knowledge DB (+WAL) and config files.

    Any ingestion (which writes the main DB or its -wal sidecar) or config edit
    changes this, busting the cache so a fresh search sees new data / embedder.
    """
    sig: list = []
    wal_path = db_path.with_name(db_path.name + "-wal")
    for p in (db_path, wal_path, cfg_path):
        try:
            st = p.stat()
            sig.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((str(p), None))
    return tuple(sig)


def _get_knowledge_search(db_path: Path, cfg_path: Path) -> tuple[Any, Any]:
    """Return a cached ``(KnowledgeStore, embedder)`` pair, rebuilding on change.

    Rebuilds (and closes the prior connection) only when the DB/WAL/config
    signature changes; otherwise reuses the live store + embedder, avoiding the
    per-call schema/migrate/graph-load and embedder availability probe.
    """
    global _KNOWLEDGE_CACHE
    sig = _knowledge_db_signature(db_path, cfg_path)
    with _KNOWLEDGE_CACHE_LOCK:
        if _KNOWLEDGE_CACHE is not None and _KNOWLEDGE_CACHE[0] == sig:
            return _KNOWLEDGE_CACHE[1], _KNOWLEDGE_CACHE[2]
        # Rebuild. Build the new store FIRST; only close the stale connection
        # after the build succeeds. If KnowledgeStore.__init__ raises (locked or
        # corrupt DB, disk-full during the migrate DELETE), we leave the existing
        # cache entry — and its still-open connection — intact rather than
        # stranding a closed connection in the cache for the next caller.
        prev = _KNOWLEDGE_CACHE
        store = KnowledgeStore(str(db_path))
        try:
            cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        except Exception:
            cfg = {}
        embedder = create_embedder_from_config(cfg)
        # Close the stale connection only AFTER the full rebuild (store + cfg +
        # embedder) succeeds. If any step above raised, the existing cache entry
        # — and its open connection — is left intact and usable for the next call.
        if prev is not None:
            with contextlib.suppress(Exception):
                prev[1].db.close()
        # Re-fingerprint AFTER building: KnowledgeStore.__init__ creates/migrates
        # the DB (writing the file + -wal), so the pre-build signature no longer
        # matches the on-disk state. Caching under the post-build signature lets
        # the next idle call hit the cache instead of rebuilding every time.
        post_sig = _knowledge_db_signature(db_path, cfg_path)
        _KNOWLEDGE_CACHE = (post_sig, store, embedder)
        return store, embedder


def _resolve_session_key() -> str:
    """Return the real session key, falling back to PID file when env var is absent.

    Source 0 is the gateway-injected per-call caller context (pooled
    topology): gatewayd strips client-forged ``kirocrew.caller`` blocks and
    injects its own on every forwarded call, so it is the authoritative
    identity when present — env-var identity is wrong-by-construction in a
    shared backend (one process, many sessions).

    Warm-pool kiro-cli processes have no KIROCREW_SESSION_KEY env var (the pool
    spawns with an empty key so rekey() + PID file provide the correct mapping).

    After rekey, the process tree may be: gateway -> kiro-cli (pool, has PID file)
    -> kiro-cli-chat (forked child) -> MCP server.  os.getppid() returns the
    immediate parent (kiro-cli-chat) which has no PID file.  Walk up ancestors
    until we find a matching file or hit init.
    """
    ctx = current_caller()
    if ctx is not None and ctx.session_key:
        return ctx.session_key
    sk = os.environ.get("KIROCREW_SESSION_KEY", "")
    if sk:
        return sk
    try:
        from kiro_crew.session_pid_sig import read_session_pid_txt

        cfg_dir = config_dir()
        # Sandbox launcher exports its own HOST pid (the pid the gateway keys
        # session_pid files by) — direct lookup works even when this
        # process's pid view diverges from the host's (PID-namespace
        # sandboxing), where the ancestor walk below can never match.
        # Reads go through session_pid_sig's hardened reader (symlink
        # refusal, regular-file check, size bound) — same read discipline
        # as the strict verifier, minus the signature requirement.
        host_pid = os.environ.get("KIROCREW_HOST_PID", "")
        if host_pid.isdigit():
            key = read_session_pid_txt(host_pid, cfg_dir)
            if key:
                return key
        pid = os.getppid()
        seen: set[int] = set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            key = read_session_pid_txt(pid, cfg_dir)
            if key:
                return key
            pid = _get_ppid(pid)
    except Exception:
        pass
    return ""


def _resolve_session_key_strict() -> str:
    """Resolve the session key, refusing PID-walked and unsigned identities.

    Like ``_resolve_session_key`` but drops the ``/proc`` ancestor walk.
    Two identity sources are accepted:

    1. The gateway-injected ``KIROCREW_SESSION_KEY`` env var.
    2. The direct ``KIROCREW_HOST_PID`` -> ``session_pid_<pid>.txt``
       lookup, but ONLY when the HMAC sidecar written by the gateway
       verifies (:func:`kiro_crew.session_pid_sig.verify_session_pid`).
       PID-namespace sandboxing strips ``KIROCREW_SESSION_KEY`` from the
       sandboxed env, but the sandbox launcher exports its OWN host pid
       (``sandbox.py``) — exactly the pid the gateway keys
       ``session_pid_<pid>.txt`` by on session claim. The bare ``.txt``
       file is agent-writable and therefore forgeable; the sidecar is
       signed with the SEL trust root (``sel_hmac.key``), which agents
       cannot read, and binds the pid into the MAC so another pid's
       pair cannot be replayed. Without this branch,
       ``monitor_start``/``monitor_update``/``autonudge_stop``/``set_project``
       fail closed
       in every sandboxed dashboard session even though the session is
       fully identified.

    Returns ``""`` when only the ``/proc`` ancestor WALK would have
    matched, or when the sidecar is missing/invalid. The walk stays
    excluded: a subagent spawned via ``spawn_run`` lives under the
    parent slot's process tree, so walking ancestors from its MCP-core
    child silently resolves to the parent — which would let the
    subagent mutate state on the wrong slot. Read-only callers (audit,
    telemetry) keep the lenient resolver where misattribution is
    harmless.

    Source 0 (accepted BEFORE both of the above): the gateway-injected
    per-call caller context. In the pooled topology gatewayd strips any
    client-forged ``kirocrew.caller`` block on every inbound frame and
    injects its own (built from the uid-gated claim-push at ``rekey()``),
    so a context present here is gateway-authored — strictly stronger
    provenance than the env var, and the ONLY correct identity in a
    shared backend serving many sessions. This is what keeps
    ``send_notification`` working for warm-pool sessions on platforms
    without the sandbox launcher (macOS/Windows), where neither env
    source exists in the backend process.
    """
    ctx = current_caller()
    if ctx is not None and ctx.session_key:
        return ctx.session_key
    sk = os.environ.get("KIROCREW_SESSION_KEY", "")
    if sk:
        return sk
    try:
        host_pid = os.environ.get("KIROCREW_HOST_PID", "")
        if host_pid.isdigit():
            from kiro_crew.session_pid_sig import verify_session_pid

            return verify_session_pid(host_pid)
    except Exception:
        pass
    return ""


def _deny_channel_agent_messaging(caller_session: str, tool_name: str) -> str | None:
    """Return an ``Error:`` denial when a channel agent calls a messaging tool.

    Channel agents (session keys ``channel:<channel_id>:<agent_id>``) are
    confined to channel-post communication. The interactive guard in
    ``channel.py`` rejects these tools at the permission-request event, but
    an AUTO-APPROVED call (kirocrew-core is in the default ``allowedTools``)
    never emits that event — so the containment boundary must also hold
    here at MCP dispatch, keyed on the verified caller identity.
    Best-effort SEL audit mirrors channel.py's
    ``rejected_blocked_tool`` outcome; audit failure never unblocks the
    deny.
    """
    if not caller_session.startswith("channel:"):
        return None
    try:
        # Resolved from ``kiro_crew.sel`` at call time, not through the
        # module-level binding, so a substituted SEL factory is observed.
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key=caller_session,
            source="mcp",
            tool_name=tool_name,
            tool_kind="kirocrew-core",
            outcome="rejected_blocked_tool",
        )
    except Exception:
        # File-backed SEL write; stdio-silent (no logger — stderr would
        # corrupt the JSON-RPC stream). The deny below still holds.
        pass
    return (
        f"Error: {tool_name} is not available to channel agents — "
        "communicate through channel posts instead."
    )


def _vet_messaging_governance(
    caller_session: str,
    tool_name: str = "send_message",
    fail_closed: bool = False,
) -> str | None:
    """Return a denial reason if governance forbids outbound messaging, else None.

    ``tool_name`` attributes the SEL audit records (denial / degraded) to the
    actual calling tool — the gate is shared by ``send_message`` and
    ``send_notification``, and the persisted audit trail must name the real
    caller.

    ``fail_closed=True`` (the ``send_notification`` posture) makes a
    governance-evaluation ERROR deny instead of degrade-open: it is passed
    through to :func:`governance_permits` (which then returns a denying
    Decision on internal error) AND honored in this helper's own except
    branch, so an exception escaping ``governance_permits`` itself cannot
    fail-open either.  The degrade is still SEL-audited on both paths.

    Proactive/outbound messaging is a ``capabilities.messaging`` gate (an exfil
    surface a policy/profile may disable per surface/app).  Runs in the
    ``kirocrew-core`` stdio subprocess, which DOES boot the platform via
    ``cli.main`` — so ``current_context()`` carries the ceiling.  Best-effort
    for ``send_message`` (fail_closed=False): a ``PlatformCompositionError``
    propagates; any other error returns None.
    Emits no stray stdout/stderr (either would corrupt the JSON-RPC stream); a
    fail-open degrade is audited via the file-backed ``governance_degraded`` SEL
    only (``log_warning=False`` suppresses the logger here).
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import vet_and_audit

        # Shared evaluate+audit seam: the decision AND its SEL record —
        # allowed or denied — come from one code path, so record shapes and
        # fail-closed semantics cannot drift between governed
        # outbound-messaging callers.
        decision = vet_and_audit(
            "capabilities.messaging",
            "",
            session_key=caller_session,
            tool_name=tool_name,
            app=_governance_app(),
            fail_closed=fail_closed,
            log_warning=False,
        )
        if not getattr(decision, "permitted", True):
            return "outbound messaging blocked by governance policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # No logger here: this runs inside the kirocrew-core stdio MCP server,
        # whose stray stdout/stderr would corrupt the JSON-RPC stream (same
        # constraint as redact_via_context). Still emit the file-backed
        # governance_degraded SEL (no stdout) so the degrade is auditable.
        # Wrapped so a late-import failure cannot raise ImportError out of this
        # except-branch and hard-fail the stdio tool call.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                tool_name,
                session_key=caller_session,
                scope="capabilities.messaging",
                app=_governance_app(),
                log_warning=False,
            )
        except Exception:
            pass
        if fail_closed:
            return "governance evaluation failed; denying (fail-closed)"
        return None


def _vet_browse_governance(caller_session: str) -> str | None:
    """Return a denial reason if governance forbids web browsing, else None.

    The ``browser`` MCP tool drives the native panel (and points at the
    playwright-cli fallback), a web-egress surface an enterprise policy may
    disable via ``capabilities.browse``. When DENIED the tool must refuse
    outright and NOT fall back to playwright-cli -- falling back would let
    browsing continue and defeat the control. This is distinct from the
    no-native-panel case (capability ALLOWED, just no Electron), which is the
    only condition that legitimately degrades to playwright-cli.

    Same stdio-silent, best-effort discipline as :func:`_vet_messaging_governance`
    (fail-OPEN on evaluation error: a broken policy eval must not brick browsing
    on a default install). Runs inside the ``kirocrew-core`` stdio subprocess,
    which boots the platform via ``cli.main`` so ``current_context()`` carries
    the ceiling.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import vet_and_audit

        decision = vet_and_audit(
            "capabilities.browse",
            "",
            session_key=caller_session,
            tool_name="browser",
            app=_governance_app(),
            log_warning=False,
        )
        if not getattr(decision, "permitted", True):
            return "web browsing is disabled by governance policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "browser",
                session_key=caller_session,
                scope="capabilities.browse",
                app=_governance_app(),
                log_warning=False,
            )
        except Exception:
            pass
        return None


def _vet_channel_governance(caller_session: str, transport: str) -> str | None:
    """Return a denial reason if governance forbids messaging *via transport*.

    The ``channels`` scope (a ScopedMap) is the per-transport allowlist: which
    chat transports (``slack``, future ``discord``/``telegram``) outbound
    messaging may use.  It is finer-grained than the on/off
    ``capabilities.messaging`` gate above — a policy may permit messaging
    generally but restrict it to specific transports (e.g. Slack only).  We
    query the ScopedMap ``members`` allowlist for *transport*.  ``posture`` (the
    per-transport identity ceiling, policy-only) is enforced at the transport's
    own admission path, not here.  Same stdio-silent, fail-closed-CPP discipline
    as :func:`_vet_messaging_governance`.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        # A bare member id queries the ScopedMap ``members`` ruleset.
        decision = governance_permits(
            "channels",
            transport,
            session_key=caller_session,
            app=_governance_app(),
            log_warning=False,
        )
        if not getattr(decision, "permitted", True):
            _audit_governance_deny(
                caller_session, f"send_message:{transport}", "channels", decision
            )
            return f"messaging via transport {transport!r} blocked by governance policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # Wrapped: a late-import failure must not hard-fail the stdio tool call.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                f"send_message:{transport}",
                session_key=caller_session,
                scope="channels",
                app=_governance_app(),
                log_warning=False,
            )
        except Exception:
            pass
        return None


def _audit_governance_deny(session_key: str, tool_name: str, scope: str, decision: object) -> None:
    """Best-effort SEL audit of a governance denial (writes to the JSONL file,
    NOT stdout — safe in the stdio MCP server). Never raises."""
    try:
        # Resolved from ``kiro_crew.sel`` at call time, not through the
        # module-level binding, so a substituted SEL factory is observed.
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key=session_key,
            tool_name=tool_name,
            scope=scope,
            outcome="denied",
            rule=getattr(decision, "rule", ""),
            layer=getattr(decision, "layer", ""),
            reason=getattr(decision, "reason", ""),
        )
    except Exception:
        # No stdout/stderr in the stdio server; SEL writes to a file so this is
        # safe, but a failure here must never wedge the deny path.
        pass


def _governance_app() -> str:
    """Best-effort active app slug for per-app profile binding, or "".

    An app backend process carries ``KIROCREW_APP_NAME`` (set in
    ``apps.backend.start_app_backend``); when an app's own tool call reaches a
    governance chokepoint in-process, this lets a per-app profile
    (``bind:{type:"app"}``) resolve.  NOTE: the managed ``kirocrew-core`` MCP
    server is spawned by kiro-cli, NOT by an app backend, so this env var is
    absent there — a per-app profile is therefore only reachable for in-app tool
    calls today, not for the agent's MCP-routed ``learn_add``/``send_message``
    (those still resolve the per-SURFACE profile + policy ceiling, which is the
    enforced path).  Returns "" when not in an app context.
    """
    return os.environ.get("KIROCREW_APP_NAME", "")


def _vet_memory_writes_governance(caller_session: str) -> str | None:
    """Return a denial reason if governance forbids durable memory writes, else None.

    A durable memory/lesson write (``learn_add`` → persisted lesson) is an
    instruction-injection surface: content written here is re-injected into
    every future session's context.  The ``capabilities.memory_writes`` gate
    (default ON in the catalog) lets a policy/profile forbid it for a surface/app
    (e.g. a sandboxed app must not be able to plant a durable instruction).  Same
    stdio-silent, fail-closed-CPP discipline as :func:`_vet_messaging_governance`.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            "capabilities.memory_writes",
            "",
            session_key=caller_session,
            app=_governance_app(),
            log_warning=False,
        )
        if not getattr(decision, "permitted", True):
            _audit_governance_deny(
                caller_session, "learn_add", "capabilities.memory_writes", decision
            )
            return "durable memory writes blocked by governance policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # Wrapped: a late-import failure must not hard-fail the stdio tool call.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "learn_add",
                session_key=caller_session,
                scope="capabilities.memory_writes",
                app=_governance_app(),
                log_warning=False,
            )
        except Exception:
            pass
        return None


def _session_key_header_error(sk: str) -> str | None:
    """Return an actionable error if the session key cannot go in an HTTP header.

    http.client encodes header values as latin-1, so a non-latin-1 char in the
    session key (e.g. an em-dash from a tab title) raises UnicodeEncodeError
    before the request is sent. Detect it up front and tell the user to rename
    the tab, rather than surfacing the raw codec error.
    """
    try:
        sk.encode("latin-1")
        return None
    except UnicodeEncodeError:
        return (
            "session key contains a character invalid in HTTP headers "
            "(non-latin-1, e.g. an em-dash or emoji in the tab title) — "
            "rename the chat tab to use ASCII characters and retry"
        )


def _caller_header() -> dict[str, str]:
    """``X-Internal-Caller`` for this process, when it has declared one.

    MCP stdio servers declare their component name via
    ``mcp_shared.set_internal_caller`` (done centrally in
    ``run_mcp_stdio_loop``), and every loopback request from these helpers
    carries it so the gateway's audit log can attribute an internal write to
    the actual component instead of inferring "some internal caller" from the
    secret's mere presence (#3503). Attribution only — the gateway
    authenticates on ``X-Internal-Secret`` and validates this name against a
    known set before trusting it into an audit line. Processes that never
    declared an identity (CLI, tests) send no header rather than a guess.
    """
    name = internal_caller()
    return {"X-Internal-Caller": name} if name else {}


def _transport_failure(message: str, mark: bool) -> dict:
    """Error payload for a request whose outcome is unknown.

    ``transport_error`` means acceptance is undetermined — the request may have
    reached the gateway before the response failed (a read timeout after spawn
    acceptance, say), so the caller must not declare a definite rejection nor
    retry on its own. Only spawn_run's batch reconcile consumes it, and it only
    ever posts, so the flag stays opt-in per verb rather than becoming a new field
    on every reply.
    """
    out: dict[str, object] = {"error": message}
    if mark:
        out["transport_error"] = True
    return out


def _send(
    path: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str],
    method: str = "GET",
    timeout: float = 30,
    mark_transport_error: bool = False,
) -> dict:
    """Send one gateway request, re-resolving the base once if it is refused.

    A refused connection usually means the resolved base is stale: the gateway
    came up, or moved to another port, after this tool server booted, and that
    port is recorded only in the run marker. The replay runs only when
    re-resolution actually produced a different base — retrying an unchanged
    dead port just doubles the caller's latency to reach the identical failure.

    Every verb goes through here. Keeping the replay in one place is what stops
    PATCH-shaped calls from staying pinned to a base that POST already learned
    was wrong.
    """

    def _once(base: str) -> dict:
        req = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- URL is the loopback gateway (_api_base(): 127.0.0.1 plus a port from config/env or a run-marker whose ownership is re-verified per request) + a fixed internal path; never user-controlled  # noqa: E501
        with _api_urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    base = _api_base()
    try:
        return _once(base)
    except urllib.error.HTTPError as e:
        # urlopen raises HTTPError on 4xx/5xx; str(e) is only "HTTP Error 400:
        # Bad Request" — the structured {"error": ...} body lives in e.read().
        # Surface it so callers can act on the backend's actual error (e.g.
        # the learn_add "unknown session" mapping) instead of an opaque code.
        return _http_error_body(e)
    except urllib.error.URLError as e:
        if not isinstance(e.reason, (ConnectionRefusedError, socket.gaierror)):
            return _transport_failure(str(e), mark_transport_error)
        _invalidate_api_base()
        retry_port, retry_source = _resolve_api_port()
        if retry_source == "default":
            # Re-resolution produced NO evidence — the default port is an
            # unverified guess, and a listener there could be any local
            # process. Replaying would hand it the internal secret and the
            # request payload; the replay exists to chase POSITIVE evidence
            # of a moved gateway, so a no-evidence fall-through ends here.
            return {"error": str(e)}
        # Build the base from the very resolution whose source was just
        # checked (same shape as _api_base) — re-resolving again could race a
        # marker disappearing between the check and the dial.
        retry_base = f"http://127.0.0.1:{retry_port}"
        if retry_base == base:
            # Nothing was ever handed to a live gateway, so this is a definite
            # rejection: no transport ambiguity to report.
            return {"error": str(e)}
        try:
            return _once(retry_base)
        except urllib.error.HTTPError as retry_exc:
            return _http_error_body(retry_exc)
        except urllib.error.URLError as retry_exc:
            if isinstance(retry_exc.reason, (ConnectionRefusedError, socket.gaierror)):
                return {"error": str(e)}
            return _transport_failure(str(retry_exc), mark_transport_error)
        except Exception as retry_exc:
            # The replay reached the gateway and failed afterwards (a read timeout
            # after a spawn was accepted, say). Acceptance is undetermined, so this
            # must carry the same ambiguity flag as a first-attempt post-connect
            # failure — otherwise spawn_run reconciles a still-running member down
            # and orphans it.
            return _transport_failure(str(retry_exc), mark_transport_error)
    except Exception as e:
        # The request may have reached the gateway before the response failed
        # (for example, a read timeout after spawn acceptance). Callers must
        # not present this as a definite rejection or retry automatically.
        return _transport_failure(str(e), mark_transport_error)


def _post(path: str, body: dict | None = None, *, timeout: float = 30) -> dict:
    data = json.dumps(body or {}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Secret": _internal_secret(),
        **_caller_header(),
    }
    sk = _resolve_session_key()
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    # ``transport_error`` is consumed only by spawn_run's batch reconcile: it
    # means acceptance is unknown, so that member must not be declared lost.
    # Other _post callers should treat the payload as a normal error.
    return _send(
        path,
        data=data,
        headers=headers,
        method="POST",
        timeout=timeout,
        mark_transport_error=True,
    )


def _http_error_body(exc: urllib.error.HTTPError) -> dict:
    """Decode the JSON body of an ``HTTPError`` into the standard error dict.

    Prefers the structured ``{"error": ...}`` JSON body (so callers can match
    on the backend's actual message), then the raw body text, then
    ``str(exc)`` — so a non-JSON or empty error response still yields a usable
    ``{"error": ...}`` payload instead of an opaque ``"HTTP Error 400"``.

    An HTTP response body is content originating outside KiroCrew, so the
    decoded message is redacted (``redact_exfiltration_urls`` +
    ``redact_credentials``) before it is handed back to a caller that may echo
    it to the LLM / dashboard / Slack. Redaction leaves plain markers like
    ``"unknown session"`` intact, so downstream matching is unaffected.
    """
    try:
        raw = exc.read().decode("utf-8", "replace").strip()
    except Exception:
        raw = ""
    message = raw or str(exc)
    counted = False
    code = ""
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "error" in parsed:
                message = str(parsed["error"])
                # Preserve api_spawn's "this rejection was already counted"
                # marker (wave-liveness reconcile) — it must survive the
                # error-body flattening or spawn_run would double-reconcile
                # in-process rejections and close waves early.
                counted = bool(parsed.get("counted"))
                # Preserve the backend's machine-readable error code (e.g.
                # ``unknown_session`` from DELETE /api/lessons) so callers can
                # dispatch on the stable code instead of matching the
                # human-readable wording. The body is untrusted external
                # content, so only a short identifier-shaped value survives —
                # anything else is dropped rather than echoed onward.
                raw_code = parsed.get("code")
                if isinstance(raw_code, str) and _re.fullmatch(
                    r"[a-z0-9_.-]{1,64}", raw_code
                ):
                    code = raw_code
        except Exception:
            pass
    message, _ = redact_exfiltration_urls(message)
    message, _ = redact_credentials(message)
    if code == "internal_auth_mismatch":
        # Every internal tool receives the auth layer's bare "Forbidden", which
        # reads as a permission decision about the tool's own subject and sends
        # the reader after the wrong bug. It is an instance mix-up: the credential
        # this client read does not belong to the gateway generation that owns the
        # port it dialled. Rewritten HERE because all tool call sites already flow
        # through this decoder, so one mapping covers them instead of one branch
        # per tool -- and it is keyed on the CODE, since a genuine permission
        # denial produces the same body and must not be given this explanation.
        message = (
            "this client authenticated against the wrong Kiro Crew instance. "
            "The credential it read does not match the gateway now serving that "
            "port, usually because a second gateway started on this machine and "
            "replaced the shared credential file. Restart the gateway (or target "
            "the instance you meant) and retry; the gateway's security event log "
            "records both credential fingerprints for the mismatch."
        )
    out: dict = {"error": message}
    if counted:
        out["counted"] = True
    if code:
        out["code"] = code
    return out


def _get(path: str, session_key: str | None = None) -> dict:
    """GET a loopback gateway path with the internal-secret handshake.

    ``session_key`` exists so a caller that has ALREADY verified its identity can
    send the key it verified, instead of having this helper resolve one again. The
    default resolution is :func:`_resolve_session_key`, which includes the ``/proc``
    ancestor walk — fine for read-only telemetry, but for a caller gated on
    :func:`_resolve_session_key_strict` re-resolving here is a check-then-use
    split: the gate proves a strict identity exists, and this then attaches
    whatever the lenient walk answers at request time, which need not be the same
    session. Passing the verified key makes the value that was checked the value
    that is used. It is still validated by ``_session_key_header_error``.
    """
    headers = {"X-Internal-Secret": _internal_secret(), **_caller_header()}
    sk = _resolve_session_key() if session_key is None else session_key
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    return _send(path, headers=headers, timeout=10)


def _patch(path: str, body: dict | None = None, *, session_key: str | None = None) -> dict:
    """PATCH a loopback gateway path with the internal-secret handshake.

    ``session_key``: as in :func:`_put`. A caller gated on
    :func:`_resolve_session_key_strict` must send the key it verified —
    re-resolving through the lenient walk here would let the request carry a
    different session's authority than the one the gate approved.
    """
    data = json.dumps(body or {}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Secret": _internal_secret(),
        **_caller_header(),
    }
    sk = _resolve_session_key() if session_key is None else session_key
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    return _send(path, data=data, headers=headers, method="PATCH")


def _put(path: str, body: dict | None = None, session_key: str | None = None) -> dict:
    """PUT to a loopback gateway path with the internal-secret handshake.

    Same trust model as :func:`_post` / :func:`_patch` — the target path must be
    listed in ``dashboard.server._MIXED_INTERNAL_API_PATHS`` (or the strict set)
    or the gateway answers 403 ``Token required``.

    ``session_key``: as in :func:`_get`, and it matters more here because this is
    the WRITE. A caller gated on :func:`_resolve_session_key_strict` must send the
    key it verified; re-resolving through the lenient walk would let the request
    carry a different session's authority than the one the gate approved, which on
    this path means writing another crew's work item and public ledger.
    """
    data = json.dumps(body or {}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Secret": _internal_secret(),
        **_caller_header(),
    }
    sk = _resolve_session_key() if session_key is None else session_key
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    return _send(path, data=data, headers=headers, method="PUT")


def _delete(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode() if body else None
    headers = {"X-Internal-Secret": _internal_secret(), **_caller_header()}
    sk = _resolve_session_key()
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    if data:
        headers["Content-Type"] = "application/json"
    return _send(path, data=data, headers=headers, method="DELETE", timeout=10)


def _autonudge_binding_key(sk: str) -> str | None:
    """Map a session key to its AutoNudge binding key, or None if unsupported.

    ``dashboard:chat-N-TS`` → bare slot key ``chat-N-TS`` (the autonudge REST
    layer keys dashboard loops on the bare slot key); ``slack:``/``discord:``
    session keys pass through unchanged (channel-bound loops). Anything else
    (``cron:``, ``hook:``, ``subagent:`` ...) is not a nudge-able session.

    Delegates to ``autonudge.binding_key_for`` so the MCP tool and the workflow
    ``ctx.nudge`` port share one definition of "nudge-able".
    """
    return binding_key_for(sk)


def _artifact_ref_link(slug: str, name: str) -> str:
    """Render a clickable ``[<name>](/artifacts/<slug>)`` markdown link.

    The chat renderer turns this into an anchor the frontend intercepts to open
    the artifact in the side panel; ``/artifacts/<slug>`` is the canonical
    full-page route, so it also degrades to a normal navigation if interception
    is absent. Used for non-widget kinds, which (unlike widgets) don't
    round-trip via ``<mcwidget>`` and otherwise have no clickable form in chat.
    """
    # name/slug are LLM-influenced and rendered verbatim on the dashboard, so
    # scrub for credential / exfiltration patterns (same guard as other
    # tool-result paths).
    label = name or slug
    label, _ = redact_exfiltration_urls(label)
    label, _ = redact_credentials(label)
    # Unescaped ']' would break the markdown link syntax.
    label = label.replace("[", "(").replace("]", ")")
    # A literal newline in the label splits the link text across lines, breaking
    # the single-line markdown anchor — collapse CR/LF to spaces so a crafted
    # name can't fragment the rendered link.
    label = label.replace("\r", " ").replace("\n", " ")
    safe_slug, _ = redact_exfiltration_urls(slug or "")
    safe_slug, _ = redact_credentials(safe_slug)
    # Constrain to the slug charset so a crafted value can't inject ')'/markdown
    # out of the URL.
    safe_slug = _re.sub(r"[^a-z0-9-]", "", safe_slug.lower())
    # If sanitization leaves no slug (e.g. the '?' fallback or an all-redacted
    # value), a link would dangle at /artifacts/ with no target — degrade to
    # plain text so the name still surfaces without a broken anchor.
    if not safe_slug:
        return label
    return f"[{label}](/artifacts/{safe_slug})"


def _resolve_artifact_folder_id(ref: str) -> tuple[str, str | None]:
    """Resolve an artifact-folder reference (id or human path) to a folder id.

    Read-only: fetches ``/api/artifact-folders`` and matches by id, then walks
    ``/``-separated path segments against folder names (case-insensitive). Used
    by the rename/move/delete MCP tools, which must address an existing folder
    (no auto-create — that only happens on save/move to an artifact folder,
    handled server-side). Returns ``(folder_id, error)``; ``""`` = root.
    """
    ref = str(ref or "").strip()
    if not ref or ref.lower() == "root":
        return "", None
    d = _get("/api/artifact-folders")
    if d.get("error"):
        return "", d["error"]
    folders = d.get("folders", [])
    by_id = {f.get("id"): f for f in folders if isinstance(f, dict) and f.get("id")}
    if ref in by_id:
        return ref, None
    segments = [s.strip().lower() for s in ref.split("/") if s.strip()]
    if not segments:
        return "", None
    parent = ""
    cur = ""
    for seg in segments:
        match = next(
            (
                f
                for f in folders
                if str(f.get("parent_id") or "") == parent
                and str(f.get("name", "")).strip().lower() == seg
            ),
            None,
        )
        if match is None:
            safe_ref, _ = redact_exfiltration_urls(ref)
            safe_ref, _ = redact_credentials(safe_ref)
            return "", f"folder not found: {safe_ref}"
        cur = str(match.get("id") or "")
        parent = cur
    return cur, None


def _artifact_reemit_hint(slug: str, name: str, kind: str = "widget") -> str:
    """Render the canonical re-emit-this-artifact-in-chat instruction.

    Appended to artifact_save / artifact_get / artifact_update tool
    responses so the agent has the exact tag string in context at the
    moment it's about to render the artifact in chat. The artifacts
    skill says ``slug=`` is required on every re-emission of a saved
    artifact, but skill rules can be overlooked at emission time —
    session logs confirmed an LLM had the slug in front of
    it twice (artifact_get response + artifact_update response) and
    still emitted ``<mcwidget title="...">`` without the attribute,
    creating a duplicate artifact when the user clicked save.

    The hint reduces this to "copy the tag I just gave you."
    """
    if kind != "widget":
        # Non-widget artifacts (markdown, html, svg, json, text) don't
        # round-trip through `<mcwidget>` — they render via the artifact
        # detail page or MarkdownPanel. No re-emit hint needed.
        return ""
    safe_name = (name or "").replace('"', "'")
    return (
        "When you re-emit this widget in chat, use this exact opening tag\n"
        "(slug attribute is REQUIRED — without it, the user clicking save\n"
        "creates a duplicate artifact):\n\n"
        f'<mcwidget title="{safe_name}" slug="{slug}">'
    )


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments against schema. Returns cleaned args."""
    schema = MCP_CORE_SCHEMAS.get(name)
    if schema:
        return validate_tool_args(args, schema)
    return args  # tools without schemas (learn_list) pass through


def _current_session_thread_ts() -> str | None:
    """Return the CALLER's Slack thread_ts, or None.

    Thin ``thread_ts | None`` view over :func:`_classify_slack_identity` — see
    that function for the three-state discrimination (``thread`` /
    ``non_slack`` / ``unresolved``) that ``file_send`` uses to fail CLOSED for
    audience when the caller cannot be attributed. This wrapper returns the
    bare thread_ts only for the ``thread`` state and ``None`` otherwise; on its
    own it does NOT distinguish "not a Slack session" from "identity
    unresolved", so callers on the outward-facing send path MUST use
    :func:`_classify_slack_identity` directly to avoid the channel-root
    disclosure hazard (unresolved identity + explicit channel -> channel root).

    Resolution is via :func:`_resolve_session_key_strict`, which accepts ONLY
    the gateway-injected env var or an HMAC-sidecar-verified
    ``KIROCREW_HOST_PID`` lookup. It deliberately drops the ``/proc`` ancestor
    walk and the bare (agent-writable, forgeable) ``.txt`` fallback the lenient
    resolver allows — closing both the forged-pid-file and the subagent->parent
    misresolution paths, and the prior newest-mtime ``session_pid_*.txt`` glob
    that frequently resolved to a DIFFERENT session than the caller.
    """
    state, thread_ts = _classify_slack_identity()
    return thread_ts if state == "thread" else None


def _classify_slack_identity() -> tuple[str, str | None]:
    """Classify the caller's STRICT Slack identity for outward file delivery.

    ``_current_session_thread_ts`` collapses the result to a bare
    ``thread_ts | None``; this returns the underlying THREE-state discrimination
    that ``file_send`` needs to tell "this is not a Slack session" apart from
    "the caller's Slack identity could not be resolved". Collapsing those two
    into a bare ``None`` is a channel-root disclosure hazard: an *unresolved*
    caller that still supplies an explicit tracked channel would upload at the
    CHANNEL ROOT (``thread_ts=None`` + channel), exposing a file meant for one
    thread to the entire channel — a reachable cross-session disclosure that is
    fail-OPEN with respect to audience, not fail-closed. Warm-pool-claimed Slack
    sessions have no strict identity source (the gateway writes the env var /
    HMAC sidecar only at sandbox spawn, not at warm-pool claim), so every one of
    their ``file_send`` calls hits this seam.

    Returns one of:

    * ``("thread", "<bare_ts>")`` — caller is a RESOLVED Slack thread (a
      canonical ``slack:<thread_ts>`` key, converted via
      :func:`messaging.link.legacy_key`, or an already-bare legacy Slack key).
      Deliver threaded to ``thread_ts``.
    * ``("non_slack", None)``     — caller is a RESOLVED non-Slack session
      (``dashboard:``/``discord:``/app/channel/future namespace). It has no
      Slack thread, but its identity IS known, so the handler's authorized
      routing (owner DM, session-map-linked thread, or an explicitly-supplied
      tracked channel) is safe — never a channel-root broadcast for an
      unknown caller.
    * ``("unresolved", None)``    — strict resolution failed (no gateway env var
      and no HMAC-verified host-pid). The caller cannot be attributed, so an
      outward Slack send must fail CLOSED (refuse) rather than broadcast.
    """
    key = _resolve_session_key_strict()
    if not key:
        return ("unresolved", None)
    # Canonical ``slack:<thread_ts>`` -> bare thread_ts.
    bare = legacy_key(key)
    if bare is not None:
        return ("thread", bare)
    # Already-bare legacy Slack thread_ts -> pass through.
    if is_legacy_slack_key(key):
        return ("thread", key)
    # Resolved, but not a Slack thread (dashboard:, discord:, apps, channels,
    # future ns) — identity is known, so downstream routing is authorized.
    return ("non_slack", None)


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    return call_tool_with_logging(
        name,
        raw_args,
        _validate_args,
        _call_tool_inner,
        # Real caller identity when resolvable (per-call caller context in
        # pooled backends, env/PID otherwise) — a hardcoded "mcp_core" lost
        # attribution for every standard tool audit in shared backends.
        session_key=_resolve_session_key() or "mcp_core",
        downstream_service="kirocrew-core",
    )


# ── Chat-history search helpers (Phase 1: search_chat_history / get_chat_session) ──

_SNIPPET_RADIUS = 120  # chars of context kept on each side of a match
_SNIPPET_MAX_LEN = 320  # hard cap on a returned snippet
# Upper bound on ranked candidates pulled from the backend per search. Bound to
# the backend's own scan window (imported, not copied) so we consider every
# ranked match (bounded I/O) and post-filtering can't starve a caller whose hits
# rank past a small page — and the two can't silently drift apart.
_SEARCH_HISTORY_SCAN = SEARCH_SCAN_WINDOW


def _history_is_incognito(meta: dict) -> bool:
    """True if a session's memory_mode marks it private (never searchable).

    Dict-shaped convenience over :func:`kiro_crew.history.is_incognito_transcript`,
    the shared classifier — the predicate itself lives in history.py.
    """
    return is_incognito_transcript(meta.get("memory_mode"))


def _redact_history_output(text: str) -> str:
    """Apply the standard dual redaction to any chat-history tool output.

    Used on EVERY return path (including early-return error strings that echo an
    LLM-supplied session_key) so nothing reaches the dashboard unredacted.

    Routes through the context-aware :func:`redact` shim so the companion's extra
    credential patterns apply to verbatim chat-transcript egress; the Default
    ``CredentialPolicy`` delegates to ``security.redact`` (the same
    exfil-then-credential dual pass), so standalone is byte-for-byte unchanged.
    """
    return redact(text)


def _parse_iso_date_epoch(date_str: str) -> float | None:
    """Parse a YYYY-MM-DD string to a UTC midnight epoch. None on bad input."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _ws_bucket(meta_ws: object) -> str:
    """Normalize a session's workspace value to a comparable bucket.

    ``update_metadata`` accepts arbitrary JSON for ``workspace``; a non-string
    (or empty) value must bucket to "default" rather than compare unequal to a
    real workspace name and silently hide the session from its owner.
    """
    return meta_ws if isinstance(meta_ws, str) and meta_ws else "default"


def _caller_workspace(cl: "object", session_key: str) -> str:
    """Resolve the calling session's workspace bucket for scope filtering.

    Read from the caller's own session metadata (normalized via _ws_bucket).
    Known limitation: on a brand-new session whose metadata file has not been
    written yet, this returns "default". A multi-workspace caller in that narrow
    window is scoped to the default bucket (fail-CLOSED — they see fewer results,
    never another workspace's). Fully fixing it needs the gateway to carry the
    workspace in CallerContext (the register payload does not today), so it is
    tracked as a separate gateway change rather than papered over here.
    """
    if not session_key:
        return "default"
    return _ws_bucket(cl.get_metadata(session_key).get("workspace"))  # type: ignore[attr-defined]


_HISTORY_SNIPPET_ROLES = frozenset({"user", "assistant"})


def _casefold_match_span(text: str, needle_cf: str) -> tuple[int, int] | None:
    """Locate *needle_cf* (already casefolded) inside *text* using full casefolding.

    Returns ``(start, end)`` source indices into *text* for the first match, or
    ``None``. Unlike ``re.search(..., re.IGNORECASE)`` — which does only simple
    per-character case mapping — this mirrors ``str.casefold`` so multi-char
    folds (e.g. ``ß`` ↔ ``ss``, ``ﬃ`` ↔ ``ffi``) match, keeping the wrap matcher
    consistent with the ``str.casefold().find`` selection above. ``str.casefold``
    is a per-character homomorphism, so casefolded offsets map back to source
    character boundaries.
    """
    if not needle_cf:
        return None
    # bounds[k] = length of casefold(text[:k]); the running offset into cf_text
    # for each source char boundary, so a casefolded match offset maps back to
    # the source index whose bounds entry equals it.
    bounds = [0]
    for ch in text:
        bounds.append(bounds[-1] + len(ch.casefold()))
    cf_text = text.casefold()
    cf_start = cf_text.find(needle_cf)
    if cf_start < 0:
        return None
    cf_end = cf_start + len(needle_cf)
    # Map casefolded offsets to source char boundaries. A fold that expands
    # length can leave an offset mid-expansion (no exact boundary); fall back to
    # the enclosing boundary so the wrap never splits a source character.
    try:
        start = bounds.index(cf_start)
    except ValueError:
        start = next((k for k in range(len(bounds)) if bounds[k] > cf_start), 1) - 1
    try:
        end = bounds.index(cf_end)
    except ValueError:
        end = next((k for k in range(len(bounds)) if bounds[k] >= cf_end), len(bounds) - 1)
    return start, end


def _extract_history_snippet(messages: list[dict], needle: str) -> str:
    """Return a bounded snippet around the first user/assistant message matching *needle*.

    The matched substring is delimited with ``<<<...>>>``. Returns "" when no
    eligible message content contains the needle (e.g. it only matched the title).
    """
    # Defense-in-depth: an empty/whitespace needle makes str.find return 0 on
    # every message and would wrap meaningless text in <<<>>>. The query is
    # already validated non-empty upstream, but guard here too since this helper
    # is independently callable.
    if not needle.strip():
        return ""
    # Same parse as search_sessions: that call decides a session MATCHED on
    # scattered needles, so searching only the whole phrase here would return ""
    # and suppress the row's snippet for exactly the multi-word queries the
    # needle-wise match enables. Ordered highest-signal first (phrase, whole
    # terms and CJK bigrams, then lone characters) so the excerpt centers on
    # the most meaningful hit available.
    needles_cf = snippet_needles(needle)
    if not needles_cf:
        return ""
    for m in messages:
        # Only surface user/assistant content (mirror get_chat_session) so the
        # snippet is the human-facing context, not a tool/system trace blob.
        if str(m.get("role", "")).lower() not in _HISTORY_SNIPPET_ROLES:
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content:
            continue
        folded = content.casefold()
        for needle_cf in needles_cf:
            idx = folded.find(needle_cf)
            if idx < 0:
                continue
            start = max(0, idx - _SNIPPET_RADIUS)
            end = min(len(content), idx + len(needle_cf) + _SNIPPET_RADIUS)
            seg = content[start:end]
            # Redact BEFORE inserting <<<...>>> markers: marker insertion would
            # split a credential/URL token and defeat the contiguous-match
            # redactors, so a query that is a substring of a secret in stored
            # content could leak it.
            seg = _redact_history_output(seg)
            # Locate the match span in the (possibly redacted) original text using
            # the SAME full casefolding as the selection above — a case-insensitive
            # regex does only simple per-char mapping and would miss multi-char
            # folds (ß→ss), leaving a selected-but-unwrapped snippet with no
            # <<<...>>>.
            span = _casefold_match_span(seg, needle_cf)
            if span:
                s, e = span
                seg = seg[:s] + "<<<" + seg[s:e] + ">>>" + seg[e:]
            seg = ("…" if start > 0 else "") + seg + ("…" if end < len(content) else "")
            result = seg[:_SNIPPET_MAX_LEN]
            # If the hard cap sliced through the match delimiters (possible with a
            # long query), re-close so the consumer never sees a dangling "<<<".
            if "<<<" in result and ">>>" not in result:
                result = result[: _SNIPPET_MAX_LEN - 3] + ">>>"
            return result
    return ""


def _format_anchor(anchor: dict) -> str:
    """Format an anchor quote for the artifact_get_comments output.

    Short quotes (≤300 chars) are shown in full. Longer quotes are bookended
    with the first and last 100 chars plus an explicit TRUNCATED marker
    (never ambiguous with literal user text). Offsets are always included
    when available so the agent can locate the range in the document.
    """
    quote = anchor.get("quote", "")
    start = anchor.get("start_offset")
    end = anchor.get("end_offset")
    offset_info = ""
    if start is not None and end is not None:
        offset_info = f", chars {start}:{end}"
    if len(quote) <= 300:
        return f' [on: "{quote}"{offset_info}]'
    head = quote[:100]
    tail = quote[-100:]
    omitted = len(quote) - 200
    return f' [on: "{head}" [TRUNCATED: {omitted} chars omitted' f'{offset_info}] "{tail}"]'


def _do_select_crew(crew: str) -> str:
    """Orchestrator crew routing (the select_crew tool body).

    Empty ``crew`` → JSON roster of *selectable* crews: those with a non-empty
    ``triggers`` (a crew with no triggers is not a routing candidate at all),
    excluding the default crew (the caller itself). The response also carries
    ``default_agent`` and explicit guidance so the model selects only on a
    high-confidence match and otherwise falls back to the default crew. A named
    crew → validate it exists, resolve its bindings, and return the bound
    {workspace, memory_store, kiro_agent, model}. An unknown name returns a JSON
    ``error`` with the available names.
    """
    cfg = KiroCrewConfig.load()
    default = cfg.default_agent
    if not crew:
        roster = [
            {"name": n, "triggers": c.triggers}
            for n, c in cfg.agents.items()
            if n != default and c.triggers.strip()
        ]
        return json.dumps(
            {
                "default_agent": default,
                "crews": roster,
                "guidance": (
                    "Select a crew ONLY when its triggers clearly and specifically "
                    "match the task with high confidence. If no crew is a strong "
                    "match (or the list is empty), do NOT route — use the default "
                    "crew. Crews without triggers are intentionally omitted."
                ),
            },
            ensure_ascii=False,
        )
    if crew not in cfg.agents:
        available = ", ".join(sorted(cfg.agents)) or "(none)"
        return json.dumps(
            {"error": f"unknown crew '{crew}'", "available": available},
            ensure_ascii=False,
        )
    b = resolve_agent_bindings(cfg, crew)
    # Routing-decision pointer. This is the one place a member's identity is
    # unambiguous on the delegation path: `spawn_run`'s `agent` is validated
    # against the installed TEMPLATES, so by the time a sub-agent starts the
    # member it came from can no longer be recovered (two members may share one
    # template). Recording at bind time sidesteps that.
    #
    # It records the DECISION, not an execution — binding a crew does not oblige
    # the model to delegate to it. That is the signal trigger generation wants
    # (what the router believes belongs to whom), but it means these entries are
    # intent. A `via="spawn"` execution entry is deliberately left for when the
    # spawn path carries member identity.
    #
    # The caller's memory mode is resolved HERE rather than defaulted: this log
    # outlives session pruning, so recording a no-trace session's key would
    # durably defeat the mode. An unreadable session degrades to the private
    # spelling so the failure mode is "skip the entry", never "log a private
    # session". No try/except around the write itself: record_activity is total
    # and reports failure by returning False, which matters because this module
    # keeps no logger.
    _sk = _resolve_session_key()
    try:
        _mode = str(ConversationLog().get_metadata(_sk).get("memory_mode", "") or "")
    except Exception:
        _mode = "incognito"
    record_activity(crew, _sk, _mode, via="select_crew")
    return json.dumps(
        {
            "crew": crew,
            "bound": {
                "kiro_agent": b.kiro_agent,
                "workspace": str(b.workspace_dir),
                "memory_store": b.memory_store_name,
                "model": cfg.agents[crew].model,
            },
        },
        ensure_ascii=False,
    )


def _redact_json_strings(value: Any) -> Any:
    """Recursively credential-redact every string (keys included) in decoded JSON.

    ``sanitize_json_values`` strips hidden characters; this pass scrubs
    credential material itself. Bodies forwarded to persisting routes (ledger
    entries sync to a configured remote) need redaction on the way IN — the
    response-path ``redact`` cannot un-persist what ``_post`` already wrote.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {_redact_json_strings(k): _redact_json_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_json_strings(item) for item in value]
    return value


# ── Issue Radar crew ledger helpers ──
#
# Two allowlisted app routes, both FULL paths in
# ``dashboard.server._MIXED_INTERNAL_API_PATHS`` (see the comment there for why
# the ``/api/apps/issue-radar`` prefix must never be admitted).
#
# That listing is necessary but not sufficient: it is matched
# ``path == p or path.startswith(p + "/")`` and carries no method, so the
# ``/crew`` entry also reaches ``/crew/pause`` and
# ``PUT``/``DELETE /crew``. The app closes that itself —
# ``crew_routes._AGENT_REACHABLE`` refuses an internal-secret caller on every
# crew route except the exact two below.
_CREW_READ_PATH = "/api/apps/issue-radar/crew"
_CREW_WORK_PATH = "/api/apps/issue-radar/crew/work"

#: Progress lines returned by a read. The log is repo-wide and append-only, so an
#: unbounded slice grows without limit and would eventually be the largest thing
#: in a crew's context — the opposite of what a resume needs. Newest first.
_CREW_MAX_EVENTS = 20


def _crew_machine_markers() -> list[tuple[str, str]]:
    """Strings that identify THIS machine, longest first.

    Longest-first matters: the Kiro Crew home normally sits inside the user's
    home, so scrubbing the home first would leave ``<home>/.kiro/crew/...`` —
    still a directory layout — instead of collapsing the whole prefix.
    """
    markers: list[tuple[str, str]] = []
    for value, placeholder in (
        (str(config_dir()), "<kirocrew-home>"),
        (str(Path.home()), "<home>"),
        (tempfile.gettempdir(), "<tmp>"),
    ):
        if value and value not in ("/", "\\"):
            markers.append((value, placeholder))
    host = ""
    with contextlib.suppress(Exception):
        host = socket.gethostname()
    # Only a distinctive hostname is scrubbed. A short one ("dev", "mac") is a
    # real English word often enough that substring-replacing it would corrupt
    # ordinary prose, and a corrupted progress line is a worse outcome than a
    # short hostname the brief already forbids writing.
    if len(host) >= 8:
        markers.append((host, "<host>"))
    markers.sort(key=lambda pair: len(pair[0]), reverse=True)
    return markers


def _crew_public_text(text: str) -> str:
    """Sanitize a crew string that becomes PUBLIC, on the way IN.

    Two passes, for two different reasons:

    1. ``redact`` — the module's ``platform.redact_via_context`` shim, the same
       canonical egress helper ``issue_radar_record_investigation`` uses. That
       tool redacts because LLM prose about an untrusted issue body is
       re-rendered on a card; here the same prose is ALSO rendered into a
       comment on the forge, so a credential or exfil URL quoted out of an issue
       would be published, not merely stored.
    2. ``_crew_machine_markers`` — redaction covers credentials and exfil URLs,
       NOT an absolute path or a host name, and those are exactly what must not
       leave this machine in a public comment. This pass is a backstop, not the
       control: it can only remove identifiers this process can name, so the
       crew brief's prohibition remains the primary rule. It is deliberately NOT
       applied to ``worktree`` / ``branch`` / ``base_sha`` — those are the one
       place an absolute path legitimately belongs, they stay local, and
       scrubbing them would break the resume they exist for.
    """
    out = redact(text)
    for value, placeholder in _crew_machine_markers():
        out = out.replace(value, placeholder)
        if "\\" in value:
            # Windows: the same path is written both ways in practice.
            out = out.replace(value.replace("\\", "/"), placeholder)
    return out


def _crew_identity(payload: dict[str, Any]) -> tuple[str, str, str] | None:
    """Pull ``(owner, repo, crew_id)`` out of a crew-read response.

    The identity is echoed back by the READ route, which resolves it from the
    calling session's ``X-Session-Key`` — it is never taken from tool arguments.
    That is what makes a cross-crew write impossible: a crew cannot name a repo, so
    it cannot overwrite a same-numbered issue in another repo, and it cannot reach
    another crew's item at all (which would also defeat the store's per-crew
    "one editing item" invariant).

    Tolerant of where the route puts it — top level, on the crew record, or on a
    work item — because all three carry it and a single hard-coded location would
    turn a harmless shape difference into a dead write path. The top level is the
    one that is always present: a crew with no work items yet has no other source.
    """
    _raw_crew = payload.get("crew")
    crew: dict[str, Any] = _raw_crew if isinstance(_raw_crew, dict) else {}
    _raw_items = payload.get("items")
    items: list[Any] = _raw_items if isinstance(_raw_items, list) else []
    first = next((it for it in items if isinstance(it, dict)), {})
    owner = repo = ""
    for source in (payload, crew, first):
        owner = str(source.get("owner") or "").strip()
        repo = str(source.get("repo") or "").strip()
        if owner and repo:
            break
    if not (owner and repo):
        return None
    crew_id = str(crew.get("id") or crew.get("crew_id") or first.get("crew_id") or "").strip()
    if not crew_id:
        return None
    return owner, repo, crew_id


def _crew_ledger_view(payload: dict[str, Any]) -> dict[str, Any]:
    """Project a crew-read response into what a resuming turn actually needs.

    Everything the route returns about the crew and its unfinished items is
    passed through — those fields ARE the resume state — while the repo-wide
    event log is bounded to the newest ``_CREW_MAX_EVENTS`` lines.

    The two skip fields are passed through as the route bounded them and are NOT
    re-trimmed here. ``skipped_numbers`` in particular must stay complete: a crew
    tests membership against it before spending a turn investigating, and a list
    trimmed at this layer would answer "not skipped" for an issue that is, which
    reintroduces the duplicated investigation the index removes.
    """
    _raw_events = payload.get("events")
    events: list[Any] = _raw_events if isinstance(_raw_events, list) else []
    view: dict[str, Any] = {
        "crew": payload.get("crew") or {},
        "settings": payload.get("settings") or {},
        "open_items": payload.get("items") or [],
        "counts": payload.get("counts") or {},
        "skipped_numbers": payload.get("skipped_numbers") or [],
        "recent_skips": payload.get("recent_skips") or [],
        # ``read_events`` returns NEWEST FIRST, so the newest N is ``events[:N]``,
        # not ``events[-N:]`` — the latter took the N OLDEST while the note below
        # told the crew they were the newest, so any crew past its first N events
        # was handed ancient history labelled as current. The ``reversed`` is
        # deliberate and stays: the crew reads this as a transcript of what
        # happened, which wants chronological order.
        "recent_events": list(reversed(events[:_CREW_MAX_EVENTS])),
    }
    if len(events) > _CREW_MAX_EVENTS:
        view["recent_events_note"] = (
            f"newest {_CREW_MAX_EVENTS} of {len(events)} — the full log is on the crew page"
        )
    return view


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    """Run one tool call and return its text result.

    Handlers live beside their descriptors in :mod:`kiro_crew.mcp_tools`; this
    stays the entry point every caller and test uses. Handlers reach back for
    this module's plumbing (``_post``, the identity resolvers, the governance
    vets) as attributes of ``mcp_core``, so a test that rebinds one still
    intercepts.
    """
    return dispatch(name, args)


#: Whether this server advertises ``kirocrew.caller-identity`` — i.e. whether it
#: consumes the per-call caller block gatewayd injects (see
#: :func:`_resolve_session_key`, whose source 0 is that block) instead of reading
#: identity from its own process. True here because it does.
#:
#: A module-level constant rather than a bare argument below so the value is
#: readable without executing ``run_mcp_core_server``, and so a test can assert it
#: against the argument actually handed to the shim.
#:
#: The shareability assessment does NOT read it: that classification runs on every
#: render and must answer without spawning the server, so importing this module
#: there would execute package code from an editable checkout on a path the
#: sandbox never confines. It answers from ``_MANAGED_SERVERS_CALLER_AWARE`` in
#: ``mcp_discovery`` instead, and ``test/test_mcp_managed_caller_identity.py``
#: keeps the two from drifting by comparing them in the test process.
ADVERTISE_CALLER_IDENTITY = True


def run_mcp_core_server() -> None:
    """Run MCP stdio server for core agent tools."""
    run_mcp_stdio_loop(
        "kirocrew-core",
        "1.0.0",
        _list_tools,
        _call_tool,
        # Pooled-operation opt-in: kirocrew-core consumes the per-call
        # ``kirocrew.caller`` identity (see _resolve_session_key*), so it is
        # safe to share one backend across sessions. kirocrew-cron advertises
        # too, for the same reason. The two managed servers that do not --
        # kirocrew-computer and kirocrew-dashboard -- are pooled all the same
        # (nothing declines to pool an unadvertised backend; see
        # ``rewriter.UNPOOLABLE_SERVERS``), so what they lack is the injected
        # block, not co-tenancy.
        advertise_caller_identity=ADVERTISE_CALLER_IDENTITY,
    )
