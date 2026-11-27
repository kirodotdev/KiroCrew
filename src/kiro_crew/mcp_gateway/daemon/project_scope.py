"""Per-project backends: one shared instance per (server, project, sandbox plan).

A server that reads the folder it starts in cannot share one backend across
projects, and it must not leave the sandbox its chat would have started it in.
This module lets such a server share one backend among the chats of ONE project:

* The gateway tells the daemon which project root and sandbox plan each session
  has, in a ``session-project`` control frame. Only the daemon's owner (or its
  exec successor) may send one, checked by the kernel-reported sender pid. The
  Register frame never names a root: any same-user process can send a frame.
* A stub asks for a per-project backend with ``project_scoped: true``. The daemon
  looks up the session the kernel attested for that connection, and builds the
  pool key from it: ``work_dir`` becomes the root and ``sandbox_hash`` the plan's
  digest. Chats of one project with one plan share a backend; any other root or
  plan gets its own.
* The backend starts in the root, inside the same sandbox tier, hidden dirs and
  exposed files the session uses (``backend.spawn_backend``), with its own temp
  dir as its scratch. It answers ``roots/list`` with the root itself
  (``backend_routing``).
* When no attested session, no binding, no sandbox, or no free slot under
  :data:`PROJECT_INSTANCES_PER_SERVER` is available, :class:`ProjectScopeUnavailable`
  is raised. It is fallback-eligible, so the stub starts the server itself inside
  the chat's own sandbox: the per-chat topology Kiro Crew has without the pool.
* A warm-pool session has no key until a chat claims it, and kiro-cli starts
  its stubs at ``session/new``, long before that claim. So the gateway sends
  its record keyed by the session's stub token and runtime pid instead. A
  connection carrying that token is placed under the record only when the
  kernel-walked process chain of its peer (``_StubConn.host_pids``) holds that
  pid with the recorded start token: the same two factors
  ``identity._token_caller`` requires. The claim keeps the session's working
  directory and the runtime keeps its sandbox, so the record holds after the
  claim; a claim on the token from anyone but the owner drops it.
* Every attach and every forwarded frame checks that the connection's session
  still maps to the backend's root (:func:`backend_serves_project`). A claim can retarget a
  connection to another session; a mismatch ends the connection.

Asking for ``project_scoped`` can only narrow what a connection gets: a sandboxed
backend in a root the gateway named, or the stub's own exec. It never widens it.
"""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Optional

from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import socketsec
from kiro_crew.mcp_gateway.daemon import logger
from kiro_crew.mcp_gateway.daemon.control_plane import CONTROL_PLANE_BACKENDS
from kiro_crew.mcp_gateway.pool import PoolKey
from kiro_crew.mcp_gateway.project_binding import ProjectBinding, ProjectScopeUnavailable

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway import gatewayd as facade
    from kiro_crew.mcp_gateway.backend import Backend
    from kiro_crew.mcp_gateway.daemon.identity import _StubConn
    from kiro_crew.mcp_gateway.pool import BackendPool
else:
    from kiro_crew.mcp_gateway.daemon import facade

#: Live per-project backends one server may have at once. Past it, a new project
#: gets the per-chat fallback. A soft cap: acquires racing for new projects can
#: pass it together, and the pool's own ``max_backends`` stays the hard bound.
#: shortcut: a fixed count, not sized from host memory; make it follow the host
#: budget if hosts with many projects hit it.
PROJECT_INSTANCES_PER_SERVER = 8

#: Sandbox tiers a per-project backend may run under. ``off`` is not one: a
#: session with no sandbox keeps its servers in its own process tree.
_PROJECT_SANDBOX_MODES = frozenset({"auto", "standard", "cc", "strict"})

#: Bounds on what one ``session-project`` frame may carry.
_MAX_PROJECT_BINDINGS = 4096
_MAX_PROJECT_PATHS = 256
_MAX_PROJECT_TEXT = 4096


#: session_key -> binding, oldest first. Fed only by :func:`apply_session_project`.
_PROJECT_BINDINGS: "OrderedDict[str, ProjectBinding]" = OrderedDict()
#: sandbox_hash -> binding, so a spawn can read the plan its key names.
_PROJECT_PLANS: dict[str, ProjectBinding] = {}
#: stub session token -> (binding, runtime pid, pid start token) of a session the
#: owner recorded before any chat claimed it (a warm-pool session), oldest first.
_TOKEN_PROJECTS: "OrderedDict[str, tuple[ProjectBinding, int, Optional[str]]]" = OrderedDict()


#: session token -> (session key, runtime pid, pid start id) of the last claim
#: the owner itself sent. A claim on the uid socket is accepted from any
#: same-user process, which is enough to name a caller for tool calls; picking a
#: backend inside another session's sandbox also needs the token's CURRENT
#: binding to be the one the owner made.
_OWNER_CLAIMED_TOKENS: "OrderedDict[str, tuple[str, int, Optional[str]]]" = OrderedDict()


async def note_owner_claim(
    writer: asyncio.StreamWriter, frame: dict[str, Any], session_key: str
) -> None:
    """After an accepted claim: record it when the owner sent it, else revoke the token.

    *session_key* is the caller the claim handler read off *frame*.
    """
    token = frame.get("stub_session_token")
    if not isinstance(token, str) or not token:
        return
    pid = frame.get("pid")
    start_id = frame.get("pid_start_id")
    _OWNER_CLAIMED_TOKENS.pop(token, None)
    owner_refusal = await _session_project_sender_refusal(writer)
    if owner_refusal:
        # Anyone else's claim on the token may have moved it, so the warm
        # record the owner made for it is dropped with it.
        _forget_token(token)
    if not session_key or not isinstance(pid, int) or isinstance(pid, bool) or owner_refusal:
        return
    _OWNER_CLAIMED_TOKENS[token] = (
        session_key,
        pid,
        start_id if isinstance(start_id, str) else None,
    )
    # One row per token binding, so the bound is the bindings' own.
    while len(_OWNER_CLAIMED_TOKENS) > facade._MAX_TOKEN_BINDINGS:
        _OWNER_CLAIMED_TOKENS.popitem(last=False)


def owner_claimed(
    token: str, session_key: str, bound_pid: int, bound_start_id: Optional[str]
) -> bool:
    """Whether the token's current binding is the one the owner's own claim made."""
    return bool(token) and _OWNER_CLAIMED_TOKENS.get(token) == (
        session_key,
        bound_pid,
        bound_start_id,
    )


def reset_project_bindings() -> None:
    """Forget every binding (tests).

    A gateway that takes the daemon over sends a fresh record for each session it
    runs, which replaces the old one; a record left behind names a dead session,
    so no connection can be attested for it.
    """
    _PROJECT_BINDINGS.clear()
    _PROJECT_PLANS.clear()
    _OWNER_CLAIMED_TOKENS.clear()
    _TOKEN_PROJECTS.clear()


def project_binding_for_session(session_key: str) -> Optional[ProjectBinding]:
    return _PROJECT_BINDINGS.get(session_key) if session_key else None


def project_plan_for_key(pool_key: PoolKey) -> Optional[ProjectBinding]:
    """The binding a per-project key was built from, or ``None`` for any other key."""
    if not pool_key.sandbox_hash:
        return None
    plan = _PROJECT_PLANS.get(pool_key.sandbox_hash)
    if plan is None or plan.root != pool_key.work_dir:
        return None
    return plan


#: Bounds on the env layer one record may carry.
_MAX_ENV_LAYER_KEYS = 512
_MAX_ENV_LAYER_VALUE = 32768


def _env_layer(raw: Any) -> Optional[tuple[tuple[str, str], ...]]:
    if raw is None:
        return ()
    if not isinstance(raw, dict) or len(raw) > _MAX_ENV_LAYER_KEYS:
        return None
    pairs = []
    for key, value in raw.items():
        if not isinstance(key, str) or not key or len(key) > _MAX_PROJECT_TEXT or "=" in key:
            return None
        if not isinstance(value, str) or len(value) > _MAX_ENV_LAYER_VALUE:
            return None
        pairs.append((key, value))
    return tuple(sorted(pairs))


def _project_paths(raw: Any) -> Optional[tuple[str, ...]]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > _MAX_PROJECT_PATHS:
        return None
    out = []
    for item in raw:
        if not isinstance(item, str) or not item or len(item) > _MAX_PROJECT_TEXT:
            return None
        if not os.path.isabs(item):
            return None
        out.append(item)
    return tuple(out)


def _parse_project_binding(frame: dict[str, Any]) -> tuple[Optional[ProjectBinding], str]:
    root = frame.get("root")
    mode = frame.get("sandbox_mode")
    if (
        not isinstance(root, str)
        or not root
        or len(root) > _MAX_PROJECT_TEXT
        or not os.path.isabs(root)
    ):
        return None, "root must be an absolute path"
    if mode not in _PROJECT_SANDBOX_MODES:
        return None, "sandbox_mode is not a sandbox tier"
    hidden = _project_paths(frame.get("hidden_dirs"))
    expose = _project_paths(frame.get("expose_files"))
    if hidden is None or expose is None:
        return None, "hidden_dirs and expose_files must be lists of absolute paths"
    readonly = _project_paths(frame.get("readonly_paths", []))
    exceptions = _project_paths(frame.get("readonly_exceptions", []))
    if readonly is None or exceptions is None:
        return None, "readonly_paths and readonly_exceptions must be lists of absolute paths"
    layer = _env_layer(frame.get("env_layer"))
    if layer is None:
        # Names no value: the reason reaches a log and an audit row.
        return None, "env_layer must map names to strings"
    return (
        ProjectBinding(os.path.normpath(root), mode, hidden, expose, readonly, exceptions, layer),
        "",
    )


async def _effective_binding(frame: dict[str, Any]) -> tuple[Optional[ProjectBinding], str]:
    """The parsed record, its tier clamped up to the governance floor.

    The record names the tier the session asked for, and the session's wrap
    raised it to the floor. Keying by the raised tier keeps a chat whose wrap
    is strict off a backend that another chat started on a weaker tier.
    """
    binding, reason = _parse_project_binding(frame)
    if binding is None:
        return None, reason
    from kiro_crew.sandbox import effective_sandbox_mode

    try:
        mode = await asyncio.to_thread(effective_sandbox_mode, binding.sandbox_mode)
    except Exception as exc:  # a floor that cannot be read is no floor to trust
        return None, f"the sandbox floor could not be read: {type(exc).__name__}"
    if mode not in _PROJECT_SANDBOX_MODES:
        return None, "sandbox_mode is not a sandbox tier"
    return replace(binding, sandbox_mode=mode), ""


def _audit_session_project(writer: asyncio.StreamWriter, outcome: str, reason: str) -> None:
    """SEL row for a session-project frame: a permission decision, like ``owner-port``."""
    peer_pid = socketsec.get_peer_pid(writer)
    try:
        facade.SecurityEventLog().log_api_access(
            caller=f"pid:{peer_pid}" if peer_pid is not None else "unknown",
            operation="mcp-gateway.session_project",
            outcome=outcome,
            source="gateway",
            resources=f"owner_pid={facade._OWNER_PID}",
            error=reason,
        )
    except Exception:  # pragma: no cover - audit must never break the handler
        logger.debug("SEL audit emit for session-project failed", exc_info=True)


def _refuse_session_project(writer: asyncio.StreamWriter, reason: str) -> dict[str, Any]:
    """Audit and answer one refused ``session-project`` frame."""
    logger.warning("gatewayd: session-project refused: %s", reason)
    _audit_session_project(writer, "denied", reason)
    return {"type": "session-project-rejected", "reason": reason}


async def _session_project_sender_refusal(writer: asyncio.StreamWriter) -> str:
    """Why the frame's sender is not this daemon's owner, or ``""`` when it is."""
    owner = facade._OWNER_PID
    peer_pid = socketsec.get_peer_pid(writer)
    if owner <= 0:
        return "this daemon has no owner pid to check the sender against"
    if peer_pid is None:
        return "the sender's pid cannot be read"
    if peer_pid != owner and not await asyncio.to_thread(
        platform_compat.is_exec_supervisor_of, owner, peer_pid
    ):
        return f"pid {peer_pid} is neither owner pid {owner} nor its exec successor"
    return ""


async def apply_session_project(
    frame: dict[str, Any], writer: asyncio.StreamWriter
) -> dict[str, Any]:
    """Record or clear one session's binding (the ``session-project`` frame).

    An empty ``root`` clears the session. A frame the owner sent and that is
    refused still clears it; one from anyone else changes nothing. A
    frame with no ``session_key`` names a warm-pool session by its stub token and
    runtime pid instead (:func:`_apply_token_project`).
    """
    session_key = frame.get("session_key")
    if session_key in (None, "") and frame.get("stub_session_token"):
        return await _apply_token_project(frame, writer)
    if not isinstance(session_key, str) or not session_key or len(session_key) > _MAX_PROJECT_TEXT:
        return _refuse_session_project(writer, "missing session_key")
    refusal = await _session_project_sender_refusal(writer)
    if refusal:
        return _refuse_session_project(writer, refusal)
    # The owner's new word on this session replaces the old record whatever it
    # says: a refused one leaves none, so the session's stubs run in the chat
    # rather than in the project an earlier runtime recorded.
    _forget_session(session_key)
    if frame.get("root") in (None, ""):
        _audit_session_project(writer, "allowed", f"cleared session={session_key[:64]}")
        return {"type": "session-project-cleared"}
    binding, reason = await _effective_binding(frame)
    if binding is None:
        return _refuse_session_project(writer, reason)
    if not await asyncio.to_thread(os.path.isdir, binding.root):
        return _refuse_session_project(writer, "root is not a directory")
    _PROJECT_BINDINGS[session_key] = binding
    _PROJECT_PLANS[binding.sandbox_hash()] = binding
    while len(_PROJECT_BINDINGS) > _MAX_PROJECT_BINDINGS:
        _forget_session(next(iter(_PROJECT_BINDINGS)))
    _audit_session_project(
        writer,
        "allowed",
        f"set session={session_key[:64]} sandbox_hash={binding.sandbox_hash()[:12]}",
    )
    return {"type": "session-project-set", "sandbox_hash": binding.sandbox_hash()}


async def _apply_token_project(
    frame: dict[str, Any], writer: asyncio.StreamWriter
) -> dict[str, Any]:
    """Record or clear a warm-pool session's binding, named by token and runtime pid.

    The token is never logged or audited: it is a bearer name for the session.
    """
    token = frame.get("stub_session_token")
    pid = frame.get("pid")
    start_id = frame.get("pid_start_id")
    if not isinstance(token, str) or len(token) > _MAX_PROJECT_TEXT:
        return _refuse_session_project(writer, "malformed stub_session_token")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return _refuse_session_project(writer, "a warm session's record needs its runtime pid")
    refusal = await _session_project_sender_refusal(writer)
    if refusal:
        return _refuse_session_project(writer, refusal)
    if frame.get("root") in (None, ""):
        _forget_token(token)
        _audit_session_project(writer, "allowed", f"cleared warm session runtime={pid}")
        return {"type": "session-project-cleared"}
    binding, reason = await _effective_binding(frame)
    if binding is None:
        return _refuse_session_project(writer, reason)
    if not await asyncio.to_thread(os.path.isdir, binding.root):
        return _refuse_session_project(writer, "root is not a directory")
    _forget_token(token)
    _TOKEN_PROJECTS[token] = (binding, pid, start_id if isinstance(start_id, str) else None)
    _PROJECT_PLANS[binding.sandbox_hash()] = binding
    while len(_TOKEN_PROJECTS) > facade._MAX_TOKEN_BINDINGS:
        _forget_token(next(iter(_TOKEN_PROJECTS)))
    _audit_session_project(
        writer,
        "allowed",
        f"set warm session runtime={pid} sandbox_hash={binding.sandbox_hash()[:12]}",
    )
    return {"type": "session-project-set", "sandbox_hash": binding.sandbox_hash()}


def _plan_in_use(digest: str) -> bool:
    return any(b.sandbox_hash() == digest for b in _PROJECT_BINDINGS.values()) or any(
        entry[0].sandbox_hash() == digest for entry in _TOKEN_PROJECTS.values()
    )


def _forget_session(session_key: str) -> None:
    """Drop one session's record, and its plan once no other record uses it."""
    old = _PROJECT_BINDINGS.pop(session_key, None)
    if old is not None and not _plan_in_use(old.sandbox_hash()):
        _PROJECT_PLANS.pop(old.sandbox_hash(), None)


def _forget_token(token: str) -> None:
    """Drop one warm session's record, and its plan once no other record uses it."""
    old = _TOKEN_PROJECTS.pop(token, None)
    if old is not None and not _plan_in_use(old[0].sandbox_hash()):
        _PROJECT_PLANS.pop(old[0].sandbox_hash(), None)


def _token_project_binding(conn: "_StubConn") -> Optional[ProjectBinding]:
    """The warm record for *conn*'s token, when the kernel places *conn* under its runtime.

    Two factors, as ``identity._token_caller`` requires them: the token, and the
    recorded runtime pid in the chain walked from the kernel-reported peer pid.
    A definite start-token mismatch (a recycled pid) denies; an unknown matches.
    """
    token = getattr(conn, "stub_session_token", "")
    entry = _TOKEN_PROJECTS.get(token) if token else None
    if entry is None:
        return None
    binding, pid, start_id = entry
    if pid not in set(getattr(conn, "host_pids", ())):
        return None
    registered = (getattr(conn, "pid_start_ids", None) or {}).get(pid)
    if start_id is not None and registered is not None and registered != start_id:
        return None
    return binding


def _attested_project_binding(conn: "_StubConn") -> Optional[ProjectBinding]:
    if getattr(conn, "session_attested", False) and conn.caller is not None:
        binding = project_binding_for_session(conn.caller.session_key)
        if binding is not None:
            return binding
    return _token_project_binding(conn)


async def project_scoped_key(pool: "BackendPool", pool_key: PoolKey, conn: "_StubConn") -> PoolKey:
    """The per-project key for *conn*, or :class:`ProjectScopeUnavailable`."""
    from kiro_crew import sandbox

    if pool_key.server_name in CONTROL_PLANE_BACKENDS:
        raise ProjectScopeUnavailable("Kiro Crew's own servers are not project-scoped")
    binding = _attested_project_binding(conn)
    if binding is None:
        raise ProjectScopeUnavailable("no project recorded for this connection's attested session")
    if await asyncio.to_thread(sandbox.detect_backend, binding.sandbox_mode) == "none":
        raise ProjectScopeUnavailable("no sandbox backend on this host")
    key = replace(pool_key, work_dir=binding.root, sandbox_hash=binding.sandbox_hash())
    if await pool.get(key) is None:
        live = {
            b.pool_key.sandbox_hash
            for b in pool.live_pooled_backends()
            if b.pool_key.server_name == pool_key.server_name and b.pool_key.sandbox_hash
        }
        if len(live) >= PROJECT_INSTANCES_PER_SERVER:
            raise ProjectScopeUnavailable(
                f"{pool_key.server_name!r} already runs {len(live)} per-project backends"
            )
    return key


def backend_serves_project(backend: "Backend", conn: "_StubConn") -> bool:
    """Whether *backend* may serve *conn*: always for a backend with no project."""
    key = backend.pool_key
    if not key.sandbox_hash:
        return True
    binding = _attested_project_binding(conn)
    return binding is not None and binding.sandbox_hash() == key.sandbox_hash
