"""Memory routing adapters over canonical execution records.

Transport authentication and owner permissions remain independent of memory.
Member memory is routed by a captured record, not process ancestry or proof tokens.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.execution_context import (
    bind_session_execution,
    execution_for_store,
    read_session_execution,
)
from kiro_crew.session_token_sig import verify_session_token

logger = logging.getLogger(__name__)

# Legacy per-pid member-memory binding records:
# ``<crew home>/member-memory-bindings/pids/<pid>.json`` and
# ``<pid>.namespace.json``. A removed routing path wrote one per agent process
# and deleted none, and no sweep ever collected them, so they accumulate for the
# install's life -- measured on an operator host at 165,975 files spanning nine
# days and 24 MB of directory inode, of which 165,816 named no live process.
# Nothing in this version writes the directory; this is the collector it never
# had.
_LEGACY_PID_BINDING_DIR = ("member-memory-bindings", "pids")
_LEGACY_PID_BINDING_NAME_RE = re.compile(r"^(\d+)\.(?:json|namespace\.json)$")
# A record is only ever removed when it is BOTH older than this AND names no
# live process. Age alone would race a just-written record; a dead pid alone
# would delete a record whose pid number has been recycled away from the process
# that owns it while that owner is still running under a re-read. Requiring both
# is what makes the prune safe against pid reuse in either direction.
_LEGACY_PID_BINDING_MIN_AGE_SECS = 24 * 3600.0
# Per-pass deletion budget. The first pass on a host that has accumulated for
# weeks would otherwise be a six-figure unlink run inside one maintenance-pool
# task; the sweep repeats on a bounded cadence, so a backlog drains over several
# passes instead of monopolising one.
_LEGACY_PID_BINDING_PRUNE_BUDGET = 2000


def prune_legacy_member_pid_bindings(
    *,
    budget: int = _LEGACY_PID_BINDING_PRUNE_BUDGET,
    min_age_secs: float = _LEGACY_PID_BINDING_MIN_AGE_SECS,
) -> int:
    """Delete aged per-pid binding records whose pid names no live process.

    Blocking filesystem work: callers on an event loop MUST offload it (the
    periodic sweep runs it on the maintenance executor). Returns the number of
    records removed; 0 when the directory is absent, which is the normal state on
    an install that never ran the path that wrote it.
    """
    root = config_dir().joinpath(*_LEGACY_PID_BINDING_DIR)
    cutoff = time.time() - max(0.0, float(min_age_secs))
    removed = 0
    try:
        scan = os.scandir(root)
    except (OSError, ValueError):
        return 0
    with scan:
        for entry in scan:
            if removed >= max(0, int(budget)):
                break
            match = _LEGACY_PID_BINDING_NAME_RE.match(entry.name)
            if match is None:
                # An unrecognised name is left alone: this sweep owns exactly the
                # record shape it can attribute to a pid, and a directory the
                # operator or a later version put something else in is not its
                # to empty.
                continue
            try:
                # follow_symlinks=False on BOTH the stat and the unlink scope:
                # the directory is agent-writable, so a planted link must not
                # redirect either the age reading or the deletion.
                if entry.is_symlink() or entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            try:
                pid = int(match.group(1))
            except ValueError:
                continue
            if platform_compat.pid_exists(pid):
                continue
            try:
                os.unlink(entry.path)
            except OSError:
                continue
            removed += 1
    if removed:
        logger.info(
            "Pruned %d stale per-pid member-memory binding record(s) from %s",
            removed,
            root,
        )
    return removed


def read_private_session_store(session_key: str) -> str | None:
    execution = read_session_execution(session_key)
    return (
        execution.store.store_id
        if execution is not None and execution.member_id is not None
        else None
    )


def bind_private_session_store(session_key: str, memory_store: str) -> None:
    current = read_session_execution(session_key)
    if current is not None:
        if current.store.store_id != memory_store or current.member_id is None:
            raise ValueError("The session already has another memory binding")
        return
    # Establishing, so it vouches. The store arrives as this function's ARGUMENT from
    # trusted gateway code -- never read back from the session's own record -- and the
    # early return above refuses to rebind a session that already has a record, so it
    # cannot re-point an existing session at a peer's store. Without the
    # vouch the session is published but unvouched, and its own-store dispatch is then
    # refused with "cannot verify delegation within the caller's memory assignment".
    bind_session_execution(session_key, execution_for_store(memory_store), vouch=True)


def private_memory_store_for_session(session_key: str | None) -> str:
    if not session_key:
        return ""
    execution = read_session_execution(session_key)
    return (
        execution.store.store_id
        if execution is not None and execution.member_id is not None
        else ""
    )


def protected_member_session_for_pid(peer_pid: int, *, home: Any | None = None) -> str | None:
    """Return an optional host-provided process identity for MCP fallback.

    Memory V2 authorizes database access through canonical execution records;
    PID ancestry, namespaces, and proof records provide no store authority.  The
    ordinary MCP identity resolver calls this
    narrow hook so host integrations can provide a process identity without
    coupling that resolver to memory routing.  The default implementation has
    no member-memory authority and therefore returns no identity.
    """
    del peer_pid, home
    return None


def session_key_is_attested(request: Any, key: str) -> bool:
    """Whether the transport attests the caller-declared *key*.

    ``X-Internal-Secret`` proves only that a request came through the local
    gateway, so a bare ``X-Session-Key`` is the caller's own word: any
    same-machine process holding that secret can read another session's key off
    a transcript and name it here.  Two positive channels answer for the two
    ways a client resolves its OWN key, and either one binds this request to the
    session that owns it:

    * the Unix-socket kernel peer attestation ``token_auth`` sets once the
      peer's process ancestry resolves to the declared key;
    * the signed per-session token the MCP launcher publishes into each server's
      environment, which verifies back to that same key.

    Every caller is an authorization decision over a caller-supplied session
    name, so they share one answer rather than one copy each.  Blocking file I/O
    on the token branch; callers on an event loop must offload it.
    """
    if request.get("peer_verified") is True:
        return True
    token = request.headers.get("X-Session-Token", "")
    if not isinstance(token, str) or not token:
        return False
    return verify_session_token(token) == key


def memory_request_identity(request: Any) -> tuple[str | None, bool]:
    """Authenticate the session used by an internal cron-memory request.

    The declared key is accepted only with a transport attestation behind it
    (:func:`session_key_is_attested`), and the canonical execution record is
    still read before the key is returned.  This is ordinary transport
    identity, not a member-memory confidentiality mechanism.
    """
    if request.get("internal_auth") is not True:
        return None, False
    key = request.headers.get("X-Session-Key", "")
    if not isinstance(key, str):
        return None, False
    if not key:
        return None, False
    if not session_key_is_attested(request, key):
        return None, False
    try:
        read_session_execution(key)
    except (OSError, ValueError):
        return None, False
    return key, True


def require_member_memory_creation(member: str) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG
    from kiro_crew.memory_stores import UnknownMemoryStore

    cfg = KiroCrewConfig.load()
    if cfg.degraded_sections & {"memory", DEGRADED_WHOLE_CONFIG}:
        raise UnknownMemoryStore("Memory configuration is unreadable")


def require_memory_consolidation_session_key(session_key: str, expected_store: str) -> None:
    if not session_key.startswith(f"memory-consolidation:{expected_store}:"):
        raise ValueError("Consolidation session does not match the store")


def local_owner_bootstrap_allowed(request: Any) -> bool:
    """Require positive host provenance regardless of installed memory stores."""
    try:
        pid = _request_peer_pid(request)
        return bool(
            isinstance(pid, int)
            and platform_compat.get_process_start_id(pid)
            and (_verified_host_process(pid) or _gateway_spawned_app_backend(pid))
        )
    except Exception:
        return False


def _verified_host_process(pid: int) -> bool:
    """Require an ordinary host process for the local owner-token bootstrap."""
    if sys.platform == "linux":
        return platform_compat.process_namespaces_match(pid, os.getpid()) is True
    if sys.platform == "darwin":
        return platform_compat.process_is_sandboxed(pid) is False
    return sys.platform == "win32"


def _gateway_spawned_app_backend(pid: int) -> bool:
    """Recognize a live app backend launched and tracked by this gateway."""
    try:
        from kiro_crew.apps.backend import spawned_backend_owns_pid
    except Exception:
        return False
    return spawned_backend_owns_pid(pid)


def _request_peer_pid(request: Any) -> int | None:
    from kiro_crew.dashboard.token_auth import _unix_request_socket
    from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self, get_peer_pid

    sock = _unix_request_socket(request)
    if sock is not None:
        if check_peer_is_self(sock) is not PeerCredResult.MATCH:
            return None
        pid = get_peer_pid(sock)
    else:
        from kiro_crew.dashboard.origin import is_loopback

        transport = getattr(request, "transport", None)
        if transport is None:
            return None
        server = transport.get_extra_info("sockname")
        client = transport.get_extra_info("peername")
        if not (
            isinstance(server, tuple)
            and isinstance(client, tuple)
            and len(server) >= 2
            and len(client) >= 2
            and is_loopback(server[0])
            and is_loopback(client[0])
        ):
            return None
        resolve_tcp = getattr(platform_compat, "get_tcp_peer_pid", None)
        if resolve_tcp is None:
            return None
        pid = resolve_tcp(server[:2], client[:2])
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None
