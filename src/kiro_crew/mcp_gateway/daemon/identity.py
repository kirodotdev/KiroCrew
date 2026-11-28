"""Who is on the other end of a stub connection, and which session it acts for.

The peer principal gate, the Register frame's caller, the kernel-attested host
ancestry, the per-connection :class:`_StubConn` and the index claim-push retargets
through, and the session-token bindings a ``claim`` records. Identity is never
inferred from a frame field the registrant controls where the kernel or the
gateway can answer instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection, Optional

from kiro_crew import platform_compat
from kiro_crew.executors import subprocess_executor
from kiro_crew.mcp_caller import CallerContext, new_tenant_nonce
from kiro_crew.mcp_gateway import socketsec
from kiro_crew.mcp_gateway.claim import MAX_SESSION_BINDINGS
from kiro_crew.mcp_gateway.daemon import logger, project_scope
from kiro_crew.mcp_gateway.daemon.audit import (
    _audit_caller_rekey,
    _audit_peer_identity_denied,
    _audit_peer_identity_resolved,
    _audit_recaller_rejected,
)
from kiro_crew.mcp_gateway.mount_query import MOUNT_NAME_CAP, MOUNT_SERVERS_CAP
from kiro_crew.mcp_gateway.pool import PoolKey
from kiro_crew.peer_resolve import resolve_peer_identity

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway import gatewayd as facade
else:
    from kiro_crew.mcp_gateway.daemon import facade


class _StubConn:
    """Mutable per-connection identity holder, indexed by the owning runtime's
    ancestor PID chain so a ``claim`` frame (claim-push) can update the caller
    of every stub connection belonging to a just-claimed warm-pool runtime.

    ``ancestor_pids`` is the stub's parent chain (nearest first) from the
    Register frame. The connection is indexed under EVERY ancestor because
    the PID the gateway names in a claim (``AcpClient._process.pid``) can sit
    several layers above the stub's immediate parent (sandbox wrapper →
    kiro-cli → kiro-cli-chat → stub); indexing a single level was found live
    to make every claim miss.

    ``caller`` starts as the register-time identity (often ``None`` for
    warm-pool stubs) and is replaced by ``recaller`` frames (stub-initiated,
    deny-by-default) or ``claim`` frames (gateway-initiated, replace-allowed).
    Single event loop — no locking needed.

    ``pid_start_ids`` maps each indexed PID to its register-time process
    start token (``platform_compat.get_process_start_id``), the PID-recycle
    guard: a later ``claim`` naming a PID whose token does not match is
    targeting a DIFFERENT process that recycled the number, and must not
    retarget this connection. ``None`` means "identity unknown" (Windows,
    unreadable /proc) and never counts as a mismatch.

    ``stub_session_token`` names WHICH of the ACP sessions the runtime hosts
    this connection serves (``claim.mint_stub_session_token``). Every PID-keyed
    source above answers per RUNTIME, and one runtime hosts many sessions, so
    the token is the only thing that tells a ``spawn_run`` subagent's stub apart
    from its parent's. Empty for a stub whose entry carried no token (a
    hand-written config, an older overlay), which keeps that connection on the
    PID-wide behavior it has always had.
    """

    __slots__ = (
        "stub_uuid",
        "ancestor_pids",
        "pool_label",
        "caller",
        "pid_start_ids",
        "tenant_nonce",
        "stub_session_token",
        "identity_refused",
        "session_attested",
        "host_pids",
        "register",
        "captured_init",
        "private_backend",
        "describe_refusal",
        "oversized_server",
        "resource_holder",
        "bound_agents",
        "attested_pids",
    )

    def __init__(
        self,
        stub_uuid: str,
        ancestor_pids: list[int],
        pool_label: str,
        caller: Optional[CallerContext],
        pid_start_ids: Optional[dict[int, Optional[str]]] = None,
        tenant_nonce: str = "",
        stub_session_token: str = "",
    ) -> None:
        self.stub_uuid = stub_uuid
        self.ancestor_pids = ancestor_pids
        self.pool_label = pool_label
        self.caller = caller
        #: Set when a claim REFUSED to attribute this connection on a shared
        #: runtime and cleared its identity. A key-less connection is otherwise
        #: indistinguishable from one nothing has named yet, and that is the one
        #: state the stub-initiated recaller is allowed to fill, so without this
        #: flag a refusal would hand the stub a way to name any session it likes.
        #: Refused is a VERDICT, not an absence: only claim-push may clear it —
        #: either a tokenless claim (which retargets every connection on the pid
        #: unconditionally) or a tokened claim naming this connection's own token.
        self.identity_refused = False
        #: Set when ``caller`` came from the kernel or the gateway (a claimed
        #: session token, or the SO_PEERCRED process walk), never from a key the
        #: Register frame named. Only such a session may pick a per-project
        #: backend (``project_scope``).
        self.session_attested = False
        #: The host process chain walked from the kernel-reported peer pid, never
        #: the Register frame's ``ancestor_pids``. A warm session's per-project
        #: record names its runtime pid, and only this chain may place a
        #: connection under it (``project_scope``).
        self.host_pids: tuple[int, ...] = ()
        self.pid_start_ids = pid_start_ids if pid_start_ids is not None else {}
        self.stub_session_token = stub_session_token
        # Namespace separator for a connection whose session the gateway cannot
        # name, forwarded to the backend on every request. GATEWAY-minted
        # and never derived from the Register frame: ``stub_uuid`` arrives from
        # the stub, so a nonce derived from it would let one stub choose to share
        # an unnamed peer's per-tenant namespace. Independent of ``caller``,
        # which may be retargeted by a later claim-push while this stays put.
        self.tenant_nonce = tenant_nonce
        #: The Register this connection was accepted with, minus its
        #: ``stub_uuid`` and session token, and the ``initialize`` it sent. Kept
        #: so ``describe-session`` can hand both to a resource-subscription
        #: holder, which registers as the same caller on the same backend.
        self.register: Optional[dict[str, Any]] = None
        self.captured_init: Optional[dict[str, Any]] = None
        #: Whether the daemon gave this connection a backend of its own, so a
        #: holder registering with the same Register pins one more backend.
        self.private_backend = False
        #: Why ``describe-session`` cannot describe this connection: its
        #: Register or ``initialize`` was over the retained-container bound, so
        #: neither was kept. ``oversized_server`` names the server of a Register
        #: that was not kept, so the refusal still reaches the right asker.
        self.describe_refusal = ""
        self.oversized_server = ""
        #: Whether the Register named a resource-subscription holder. Read from
        #: the Register before an oversized one is dropped, so a holder is
        #: never taken for the session's own stub.
        self.resource_holder = False
        #: The agents the owning gateway said this connection's session token
        #: runs stubs for (:func:`_token_agents`). Empty when nothing the gateway
        #: sent names the token, and then the Register's ``agent_name`` is not
        #: taken unless the registrant is the gateway itself.
        self.bound_agents: frozenset[str] = frozenset()
        #: The kernel-attested host chain of the registering peer, the second
        #: factor :func:`_token_agents` is asked with again when a binding
        #: arrives after this connection registered (:func:`_refresh_bound_agents`).
        self.attested_pids: tuple[int, ...] = ()


#: Live stub connections indexed by every ancestor PID of the kiro-cli
#: process tree that spawned the stub (``ancestor_pids`` on the Register
#: frame; legacy single ``parent_pid`` accepted). Claim-push looks up this
#: index to retarget every connection of a claimed runtime at once. Entries
#: without usable PIDs (old stubs) are simply not indexed — they keep the
#: recaller-poll fallback.
_CONN_INDEX: dict[int, set[_StubConn]] = {}


def _register_pids(register: dict[str, Any]) -> list[int]:
    """Extract the ancestor PID list from a Register frame.

    Accepts the current ``ancestor_pids`` list and the legacy single
    ``parent_pid`` int. Non-int and out-of-range entries are dropped
    (deny-by-default: garbage never lands in the index).
    """
    raw = register.get("ancestor_pids")
    if not isinstance(raw, list):
        legacy = register.get("parent_pid")
        raw = [legacy] if legacy is not None else []
    return [p for p in raw if isinstance(p, int) and not isinstance(p, bool) and p > 1]


def _conn_index_add(conn: _StubConn) -> None:
    for pid in conn.ancestor_pids:
        _CONN_INDEX.setdefault(pid, set()).add(conn)


#: Cap on remembered token bindings. Each entry is one live-ish ACP session, so
#: a few hundred covers any real host; the oldest is dropped past the cap rather
#: than letting a long-running daemon accumulate them without bound. Dropping a
#: binding only costs a re-claim — the identity itself is never invented here.
#: ONE named bound, shared with the gateway-side binding cache
#: (``mcp_gateway.claim.MAX_SESSION_BINDINGS``), so the gateway never replays a
#: binding this table has already evicted.
_MAX_TOKEN_BINDINGS = MAX_SESSION_BINDINGS

#: ``stub_session_token`` -> (caller, runtime pid the claim named, that pid's
#: process start token — the recycle guard, since a pid is a reusable NUMBER).
#: Written ONLY from a ``claim`` frame, which arrives over the uid-gated 0700
#: socket from the gateway process that minted the token — so a binding is
#: Crew-authored, never peer-asserted. Read at register time and by
#: :func:`_apply_claim`, which is what lets one runtime's connections be
#: re-targeted per SESSION instead of per PID.
_TOKEN_BINDINGS: "OrderedDict[str, tuple[CallerContext, int, Optional[str]]]" = OrderedDict()


def _bind_token(
    token: str, caller: CallerContext, pid: int, pid_start_id: Optional[str] = None
) -> None:
    """Record ``token`` -> *caller* from a claim frame (most recent last).

    *pid_start_id* is that pid's start token; ``None`` never denies a resolution.
    """
    if not token:
        return
    _TOKEN_BINDINGS.pop(token, None)
    _TOKEN_BINDINGS[token] = (caller, pid, pid_start_id)
    while len(_TOKEN_BINDINGS) > _MAX_TOKEN_BINDINGS:
        _TOKEN_BINDINGS.popitem(last=False)


def _pid_live_tenants(pid: int, exclude_token: str = "") -> set[str]:
    """Session keys that hold their OWN token-carrying connection under *pid* now.

    *exclude_token* drops the connections carrying that token, which is how a
    warm-pool rekey is told from a second tenant: the claim's own stubs still
    carry the PREVIOUS session's caller at the moment the claim is applied, so
    counting them would make every rekey of a single-session runtime read as
    sharing and log at WARNING.

    Read from the live connection index, not from :data:`_TOKEN_BINDINGS`: a
    binding is append-only until the LRU cap evicts it, so a binding table says
    which sessions a pid was EVER claimed for, and a warm-pool runtime claimed
    first by A and later by B would read as two tenants while B is the only one
    left. Counting live connections answers the question the caller actually
    asks, "how many sessions does this runtime serve right now", and it needs no
    process start token: a connection belonging to a dead process is gone from
    the index, so a recycled pid cannot contribute a stale tenant either.

    Only a TOKEN-CARRYING connection counts. A tokenless connection's current
    caller is not evidence of a separate live session -- it is whatever the last
    claim retargeted it to, which on a warm-pool rekey is the session that just
    handed the runtime over. Counting it would make every rekey look like
    sharing. A token, by contrast, is minted per ACP session and names one, so a
    connection that carries one is that session being present.
    """
    tenants: set[str] = set()
    for conn in _CONN_INDEX.get(pid, ()):
        if not conn.stub_session_token:
            continue
        if exclude_token and conn.stub_session_token == exclude_token:
            continue
        if conn.caller is not None and conn.caller.session_key:
            tenants.add(conn.caller.session_key)
    return tenants


def _token_caller(
    token: str,
    attested_pids: Collection[int] = (),
    pid_start_ids: Optional[dict[int, Optional[str]]] = None,
) -> Optional[CallerContext]:
    """The session bound to *token*, for a connection the KERNEL places under it.

    A claim binds a token TOGETHER WITH the runtime PID it named, and this
    requires both: the token, and membership of that PID in *attested_pids*. So
    the token is what tells two sessions on ONE runtime apart, and the tree is
    what bounds who may present the token at all — which matters because the
    token rides an ``env`` pair and ``/proc/<pid>/environ`` is readable at the
    operator's own uid.

    *attested_pids* MUST be the host chain walked from the SO_PEERCRED peer pid,
    never the stub's self-reported ``ancestor_pids``. The register frame is
    peer-supplied in full, so a process that has read another session's token
    can also name that session's runtime in its own ``ancestor_pids`` — checking
    against those would let the same actor satisfy both halves and the second
    factor would authenticate nothing. The peer pid comes from the kernel, and
    the walk from it is gatewayd's own, so the chain cannot be authored by the
    registrant.

    *pid_start_ids* is this connection's register-time snapshot, and supplying it
    adds the generation term: membership alone lets a binding whose process died
    be satisfied by whatever inherits its pid NUMBER, which is what the pid factor
    exists to bound. Only a DEFINITE mismatch denies — an unknown on either side
    is a match, as on Windows — mirroring the guard in :func:`_apply_claim`.

    An empty chain therefore answers ``None`` for a bound token rather than
    trusting it: a connection whose ancestry the kernel did not attest is not
    shown to be under the runtime the claim named. That is not a dead end —
    claim-push still reaches the connection through ``_CONN_INDEX`` and names it
    there — so a platform without peer credentials loses the register-time
    shortcut, not its identity. Callers that only ask "has anything named this
    token" use :func:`_token_is_unbound`.
    """
    if not token:
        return None
    entry = _TOKEN_BINDINGS.get(token)
    if entry is None:
        return None
    caller, bound_pid, bound_start_id = entry
    if bound_pid not in set(attested_pids):
        return None
    registered = (pid_start_ids or {}).get(bound_pid)
    if bound_start_id is not None and registered is not None and registered != bound_start_id:
        return None
    return caller


def _token_is_unbound(token: str) -> bool:
    """True when *token* is present and no claim has named it at all."""
    return bool(token) and token not in _TOKEN_BINDINGS


#: ``stub_session_token`` -> (the agents whose stubs that session runs, runtime
#: pid, that pid's start token). Written ONLY from an ``agent-binding`` frame the
#: daemon's owner gateway sent (``control._apply_agent_binding``), which is what
#: lets the declaring agent of a stub come from the gateway rather than from the
#: Register frame the stub writes. Same cap and eviction as
#: :data:`_TOKEN_BINDINGS`: dropping an entry costs the stub its declared env
#: until the gateway sends the binding again, never a wrong agent.
_AGENT_BINDINGS: "OrderedDict[str, tuple[frozenset[str], int, Optional[str]]]" = OrderedDict()


def _bind_agents(
    token: str, agents: frozenset[str], pid: int, pid_start_id: Optional[str] = None
) -> None:
    """Record ``token`` -> *agents* from an owner-sent binding (most recent last).

    Over the cap, entries whose runtime pid is gone are dropped first, the rule
    the gateway's own cache keeps (``claim._evict_dead_bindings_over_cap``): a
    live session's binding outlives newer ones, because losing it leaves a
    reconnecting stub's private backend with no declared env. Only when the
    table is still over the cap is the oldest entry dropped, with a warning.
    """
    if not token:
        return
    _AGENT_BINDINGS.pop(token, None)
    _AGENT_BINDINGS[token] = (agents, pid, pid_start_id)
    if len(_AGENT_BINDINGS) <= _MAX_TOKEN_BINDINGS:
        return
    for old_token, (_agents, old_pid, _start) in list(_AGENT_BINDINGS.items()):
        if len(_AGENT_BINDINGS) <= _MAX_TOKEN_BINDINGS:
            return
        if old_token != token and not platform_compat.pid_exists(old_pid):
            _AGENT_BINDINGS.pop(old_token, None)
    while len(_AGENT_BINDINGS) > _MAX_TOKEN_BINDINGS:
        evicted, (_agents, evicted_pid, _start) = _AGENT_BINDINGS.popitem(last=False)
        # The key is a bearer token: name it by its hash, never the token itself.
        logger.warning(
            "agent-binding table over its %d cap with every entry live; evicted the "
            "oldest binding (token sha256 %s, pid %d)",
            _MAX_TOKEN_BINDINGS,
            _mount_key(evicted)[:16],
            evicted_pid,
        )


def _refresh_bound_agents(token: str, pid: int) -> int:
    """Re-ask :func:`_token_agents` for every live connection carrying *token* under *pid*.

    A stub that registered before its binding arrived (it reconnected to a
    restarted daemon before the gateway's next claim sent the binding again)
    holds an empty answer; this is what lets the connection pick the agent up
    (``connection._handle_connection``). Returns how many connections now hold
    a non-empty answer.
    """
    refreshed = 0
    for conn in list(_CONN_INDEX.get(pid, ())):
        if not token or conn.stub_session_token != token:
            continue
        conn.bound_agents = _token_agents(token, conn.attested_pids, conn.pid_start_ids)
        refreshed += bool(conn.bound_agents)
    return refreshed


def _token_agents(
    token: str,
    attested_pids: Collection[int] = (),
    pid_start_ids: Optional[dict[int, Optional[str]]] = None,
) -> frozenset[str]:
    """The agents bound to *token*, for a connection the KERNEL places under it.

    The same two factors as :func:`_token_caller`, for the same reason: the
    token rides an ``env`` pair another process at this uid may read, so the
    binding also has to name a runtime the peer's host chain is under, and a
    definite pid start-token mismatch denies.
    """
    if not token:
        return frozenset()
    entry = _AGENT_BINDINGS.get(token)
    if entry is None:
        return frozenset()
    agents, bound_pid, bound_start_id = entry
    if bound_pid not in set(attested_pids):
        return frozenset()
    registered = (pid_start_ids or {}).get(bound_pid)
    if bound_start_id is not None and registered is not None and registered != bound_start_id:
        return frozenset()
    return agents


#: Cap on sessions the mount ledger remembers, and on server names per session.
#: Same order as :data:`_MAX_TOKEN_BINDINGS`, for the same reason: one entry per
#: live-ish ACP session, oldest dropped first.
_MAX_MOUNT_SESSIONS = 512
_MAX_MOUNTS_PER_SESSION = MOUNT_SERVERS_CAP
#: Longest server name kept. A name is config-derived and comes off a frame the
#: registrant writes, so it is bounded before it is held. The reader cuts its
#: roster to the same cap, which is why the value is shared.
_MOUNT_NAME_CAP = MOUNT_NAME_CAP

#: sha256(``stub_session_token``) -> ``{server_name: wall time of its latest
#: register}`` for every stub that registered carrying that token, first-seen
#: order. The time lets a reader count only the registrations of the harness
#: process it is asking about, since a token survives a respawn. A session that registered
#: more names than the cap is in :data:`_SESSION_MOUNTS_OVERFLOWED`, and its list
#: is then not a complete answer. The session-open mount check
#: (``acp.mcp_mount_check``) reads it through the ``session-mounts`` control frame
#: to tell a session-array element the harness started from one it silently
#: dropped. Keyed by a digest, not the token: the token is a bearer name for a
#: session's identity, and this table has no use for the value itself.
_SESSION_MOUNTS: "OrderedDict[str, dict[str, float]]" = OrderedDict()
#: Keys of :data:`_SESSION_MOUNTS` whose list hit the per-session cap and dropped
#: a name. Bounded with it: a key leaves this set when it leaves the ledger.
_SESSION_MOUNTS_OVERFLOWED: set[str] = set()

#: When this daemon's ledger began (wall clock). A session opened before it began
#: may have had stubs register with a daemon that is gone, so the reader treats
#: the ledger as unable to answer for such a session.
_MOUNT_LEDGER_SINCE = time.time()

#: Key -> wall time the ledger evicted it to stay within
#: :data:`_MAX_MOUNT_SESSIONS`, oldest first. A session evicted after the time a
#: reader asks about may have lost names, so its list cannot prove absence; one
#: evicted earlier lost only registrations the reader does not count anyway.
#: Bounded at :data:`_MAX_EVICTED_KEYS`. A key forgotten past that bound would
#: need that many evictions between its stubs' registers and the reader's first
#: query, which touches the entry and makes it the newest.
_SESSION_MOUNTS_EVICTED: "OrderedDict[str, float]" = OrderedDict()
_MAX_EVICTED_KEYS = 8 * 512


def _mount_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


def _record_session_mount(register: dict[str, Any]) -> None:
    """Note that a stub carrying a session token reached the daemon.

    Called for every register frame from a peer the uid gate admitted, BEFORE the
    frame is validated: a stub whose register is then refused still proves its
    harness started it, which is the only fact this ledger records. A stub that
    falls back to direct exec registers first too, so it is counted as well.
    """
    token = register.get("stub_session_token")
    name = register.get("server_name")
    if not isinstance(token, str) or not token or not isinstance(name, str) or not name:
        return
    name = name[:_MOUNT_NAME_CAP]
    key = _mount_key(token)
    names = _SESSION_MOUNTS.pop(key, None) or {}
    if name in names or len(names) < _MAX_MOUNTS_PER_SESSION:
        # The latest registration wins: a token outlives a respawned harness
        # process, and only a stub of the CURRENT process proves it started.
        names[name] = time.time()
    else:
        _SESSION_MOUNTS_OVERFLOWED.add(key)
    _store_mount_entry(key, names)


def _store_mount_entry(key: str, names: dict[str, float]) -> None:
    """Put *key* last (newest) and evict past the cap, noting each evicted key."""
    _SESSION_MOUNTS[key] = names
    while len(_SESSION_MOUNTS) > _MAX_MOUNT_SESSIONS:
        evicted, _ = _SESSION_MOUNTS.popitem(last=False)
        _SESSION_MOUNTS_OVERFLOWED.discard(evicted)
        _SESSION_MOUNTS_EVICTED.pop(evicted, None)
        _SESSION_MOUNTS_EVICTED[evicted] = time.time()
        while len(_SESSION_MOUNTS_EVICTED) > _MAX_EVICTED_KEYS:
            _SESSION_MOUNTS_EVICTED.popitem(last=False)


def _session_mounts(token: str, after: float = 0.0) -> list[str]:
    """Server names that registered under *token* at or after *after*, or ``[]``.

    A read that also TOUCHES the entry: it becomes the newest, created empty when
    absent, so a session a reader is watching is the last one the cap evicts.
    Only the gateway that minted a token and that token's stubs can name it.
    """
    if not token:
        return []
    key = _mount_key(token)
    names = _SESSION_MOUNTS.pop(key, None) or {}
    _store_mount_entry(key, names)
    return [name for name, seen in names.items() if seen >= after]


def _session_mounts_complete(token: str, after: float = 0.0) -> bool:
    """False when *token*'s list may have lost a name registered at or after *after*.

    Either the list hit its cap, or the ledger evicted this session at or after
    *after*. Such a list cannot prove a server absent.
    """
    if not token:
        return True
    key = _mount_key(token)
    evicted_at = _SESSION_MOUNTS_EVICTED.get(key)
    return key not in _SESSION_MOUNTS_OVERFLOWED and (evicted_at is None or evicted_at < after)


def _conn_index_discard(conn: _StubConn) -> None:
    for pid in conn.ancestor_pids:
        conns = _CONN_INDEX.get(pid)
        if conns is not None:
            conns.discard(conn)
            if not conns:
                _CONN_INDEX.pop(pid, None)


def _resolve_peer_identity(peer_pid: int) -> tuple[str, list[int]]:
    """Walk the peer's real-PID ancestry (server-side): session key + host chain.

    Delegates to the shared :func:`kiro_crew.peer_resolve.resolve_peer_identity`
    walk (also consumed by the dashboard's unix-socket peer verification) with
    gatewayd's module-level ``_config_dir`` / ``_ppid_fn`` seams, which tests
    monkeypatch. The register handler indexes the stub connection under the
    returned host chain so a later ``claim`` frame — which always carries the
    runtime's HOST pid — matches even when the stub's self-reported
    ``ancestor_pids`` are namespace-local (sandbox PID-namespace topology).
    Without the host chain in ``_CONN_INDEX`` the claim-push silently updates
    zero connections and the stub stays identity-less for life: orphan
    subagents with empty ``parent_session`` and undeliverable completion
    events.
    """
    return resolve_peer_identity(
        peer_pid, config_dir_fn=facade._config_dir, ppid_fn=facade._ppid_fn
    )


def _resolve_signed_peer_identity(peer_pid: int) -> tuple[str, list[int]]:
    """:func:`_resolve_peer_identity` that trusts only HMAC-signed pid mappings."""
    return resolve_peer_identity(
        peer_pid, config_dir_fn=facade._config_dir, ppid_fn=facade._ppid_fn, signed_only=True
    )


def _caller_from_register(register: dict[str, Any]) -> Optional[CallerContext]:
    """Build a :class:`CallerContext` from the stub's Register payload.

    The wire format is flexible to support both short and long-lived stubs:

    * Inline ``session_key`` / ``session_type`` / ``principal_id`` /
      ``channel_id`` fields on the Register envelope (tests and the Rust
      stub both use this shape).
    * A nested ``caller`` dict with the same field names — matches the
      Rust ``StubToGateway::Register { caller }`` variant.

    Missing fields default to the empty string. ``from_gateway=True`` is
    forced since this context came through the gateway register path.

    This only PARSES a frame. A Register or ``recaller`` is written by the
    connecting process, so the result names a caller only where the daemon
    vouches for the sender (:func:`_resolve_register_identity`,
    :func:`_apply_recaller`); a ``claim`` is gateway-sent.
    """
    nested = register.get("caller")
    src: dict[str, Any] = nested if isinstance(nested, dict) else register
    session_key = str(src.get("session_key") or src.get("sessionKey") or "")
    if not session_key:
        return None
    return CallerContext(
        session_key=session_key,
        session_type=str(src.get("session_type") or src.get("sessionType") or "unknown"),
        principal_id=str(src.get("principal_id") or src.get("principalId") or ""),
        channel_id=str(src.get("channel_id") or src.get("channelId") or ""),
        from_gateway=True,
    )


def _peer_admitted(writer: asyncio.StreamWriter, socket_path: Path) -> bool:
    """Whether the process on the other end of ``writer`` may use this endpoint.

    Answered before any frame is read; a refusal is logged and SEL-audited here.
    """
    # Endpoint hardening: deny-by-default peer-principal check on every
    # platform. All three supported platforms can confirm the peer principal
    # (Linux SO_PEERCRED, Windows pipe-client SID comparison, macOS
    # LOCAL_PEERCRED), so any connection that is not a positively-confirmed
    # MATCH is rejected -- a MISMATCH and an UNVERIFIABLE lookup failure both
    # fail closed. The else branch below is now reached only on a POSIX platform
    # with none of those mechanisms, where the principal cannot be read at all
    # and the 0600 socket mode is the only gate available.
    if socketsec.PEER_IDENTITY_SUPPORTED:
        peer_result = socketsec.check_peer_is_self(writer)
        if peer_result is not socketsec.PeerCredResult.MATCH:
            logger.warning(
                "rejecting gateway connection: peer principal not confirmed (%s)",
                peer_result.value,
            )
            facade._audit_peer_denied(f"peer principal not confirmed ({peer_result.value})")
            return False
    else:
        # No principal mechanism on this platform: MISMATCH-enforcing but not
        # UNVERIFIABLE-enforcing, and the asymmetry is deliberate. A positively
        # parsed foreign principal is a real intruder and is refused. A check
        # that merely FAILED must not refuse, because on a platform where the
        # lookup can never succeed that would reject every connection -- the
        # shape of the Windows impersonation defect that denied 100% of them
        # while looking merely strict. So UNVERIFIABLE falls through to the
        # filesystem gate below, which is a real check rather than a shrug: a
        # 0600 socket already prevents any other uid from connecting.
        #
        # macOS does NOT take this branch: it is inside
        # PEER_IDENTITY_SUPPORTED because the macOS CI job proves LOCAL_PEERCRED
        # returns MATCH on real hardware over an accepted socket, with that
        # canary enforced by node id so it cannot silently stop running.
        peer_result = socketsec.check_peer_is_self(writer)
        if peer_result is socketsec.PeerCredResult.MISMATCH:
            logger.warning(
                "rejecting gateway connection: peer principal is a different " "user (%s)",
                peer_result.value,
            )
            facade._audit_peer_denied(f"peer principal mismatch ({peer_result.value})")
            return False
        if not socketsec.socket_owner_only(socket_path):
            logger.warning(
                "rejecting gateway connection: peer principal unverifiable on "
                "this platform and socket %s is not owner-only (0600)",
                socket_path,
            )
            facade._audit_peer_denied(
                f"peer principal unverifiable and socket not owner-only: {socket_path}"
            )
            return False
        logger.debug(
            "peer uid unverifiable on this platform; socket %s verified "
            "owner-only, proceeding on the filesystem gate",
            socket_path,
        )
    return True


async def _resolve_register_identity(
    register: dict[str, Any],
    writer: asyncio.StreamWriter,
    stub_uuid: str,
    pool_key: PoolKey,
) -> _StubConn:
    """Build the registering connection's identity and index it for claim-push.

    The caller is, in order: the session a claim bound to this connection's token,
    when the kernel attests the peer under the runtime that claim named; nobody,
    for any other token; else the Register frame's own caller when the daemon's
    owning gateway sent the frame; else the session the kernel-attested peer walk
    resolves. The token is asked once, after the last await, and the connection is
    indexed before anything can await again.
    """
    # The Register is written by whatever process connected, and every process at
    # this uid passes the peer gate, so the frame's session_key / principal_id /
    # channel_id are a request, not an identity. They are taken only from the
    # owning gateway itself (a resource-subscription holder registers from it),
    # which the kernel peer pid proves. Any other connection is named by what the
    # daemon can attest: a claim-bound token, the peer walk below, or a later claim.
    frame_caller = _caller_from_register(register)
    caller = frame_caller if frame_caller is not None and await _sent_by_owner(writer) else None

    # Per-session identity. The token on the stub's ACP entry names ONE of the
    # sessions this runtime hosts, so a binding for it outranks every
    # process-tree source: the stub's own self-report (its
    # ``KIROCREW_SESSION_KEY`` / pid-file walk resolves the RUNTIME's tree — the
    # PARENT session for a subagent sharing the process) and the SO_PEERCRED
    # ``/proc`` walk alike. Popped from the frame rather than only read: the
    # frame is handed on to the prewarm recorder, which PERSISTS register
    # payloads to disk, and a bearer name for a session's identity must not be
    # written there.
    stub_session_token = str(register.pop("stub_session_token", "") or "")
    stub_pids = _register_pids(register)

    # Server-side peer identity: when no caller was taken above, resolve the
    # session from the peer's REAL pid (SO_PEERCRED) via a
    # host-side /proc ancestry walk — and capture the host ancestor chain for
    # claim indexing below. Deny-by-default: never grant an identity (nor
    # index host pids) without the kernel positively attesting the peer uid.
    #
    # A token-carrying stub walks even when it DOES self-report a key, because
    # the walk's other product is the host ancestor chain, and that chain is how
    # this connection's own claim finds it: a token means a claim will name this
    # connection (its session's, or a warm-pool rekey's), and under a PID
    # namespace the stub's self-reported pids can never match the host pid the
    # claim carries. The resolved KEY is still only adopted below, and only
    # where it was adopted before.
    resolved_session_key = ""
    peer_host_pids: list[int] = []
    # Capture independently of the claimed session. A nonempty register key is
    # not evidence of member authority, and its ancestor_pids are untrusted.
    peer_pid = socketsec.get_peer_pid(writer)
    peer_uid_ok = socketsec.check_peer_is_self(writer)
    needs_identity = caller is None or not caller.session_key
    if needs_identity or stub_session_token:
        if peer_pid is None or peer_uid_ok is not socketsec.PeerCredResult.MATCH:
            if needs_identity and not stub_session_token:
                # Only an unidentified stub is being REFUSED an identity here; a
                # token-carrying stub that walked purely for its host chain has
                # been granted nothing and denied nothing.
                _audit_peer_identity_denied(
                    reason=(
                        "no peer pid (SO_PEERCRED unavailable)"
                        if peer_pid is None
                        else f"peer uid not positively verified ({peer_uid_ok.name})"
                    ),
                    peer_pid=peer_pid,
                    stub_uuid=stub_uuid,
                )
        else:
            try:
                # subprocess_executor: a /proc read can block indefinitely on
                # a D-state target; isolate it from the default pools.
                (
                    resolved_session_key,
                    peer_host_pids,
                ) = await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(), facade._resolve_peer_identity, peer_pid
                )
            except Exception:  # graceful degradation: identity stays empty
                logger.exception("peer identity resolution failed for peer_pid=%d", peer_pid)
                resolved_session_key, peer_host_pids = "", []

    # Claim-push index: record the runtime process tree that owns this stub
    # so a ``claim`` frame naming ANY level of that tree re-targets every
    # connection of the claimed runtime. Best-effort — stubs that send no
    # usable PIDs simply keep the recaller-poll fallback.
    #
    # The stub's self-reported ``ancestor_pids`` can be namespace-local
    # (sandbox PID-namespace topology) and then never match a claim frame's
    # HOST pid, so merge in the host-side ancestor chain resolved from the
    # SO_PEERCRED peer pid (empty when peer creds were not positively
    # verified — deny-by-default preserved).
    indexed_pids = stub_pids + [p for p in peer_host_pids if p not in stub_pids]

    # PID-recycle guard: snapshot each indexed PID's start token NOW, while
    # the register-time process tree is still alive. A later claim carries
    # the claimed runtime's own token; a definite mismatch means the OS
    # recycled the PID to a different process and the claim must not land
    # here. Computed server-side so old stubs are covered with no wire
    # change. subprocess_executor: a /proc read can wedge on a D-state
    # target, so keep it off the event loop, matching the
    # _resolve_peer_identity walk above.
    try:
        pid_start_ids: dict[int, Optional[str]] = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            lambda: {p: facade._get_process_start_id(p) for p in indexed_pids},
        )
    except Exception:  # graceful degradation: unknown tokens never deny claims
        logger.exception("pid start-id snapshot failed for stub %s", stub_uuid)
        pid_start_ids = {}

    # Identity, in precedence order: the session this connection's token names,
    # then the refusal any other token state forces, then the process-tree
    # sources for a connection carrying no token.
    #
    # ``peer_host_pids``, NOT ``indexed_pids``: the second factor has to be a
    # fact the registrant cannot author, and ``indexed_pids`` folds in the
    # stub's self-reported ``ancestor_pids``. Those are fine for the claim INDEX
    # (a claim only ever narrows to connections carrying its own token or none)
    # and wrong for authentication.
    #
    # The ONE token ask sits after the last await before ``_conn_index_add``, so
    # a claim cannot bind between the answer and the index that lets it land.
    token_caller = facade._token_caller(stub_session_token, peer_host_pids, pid_start_ids)
    # Whether this caller may pick a per-project backend (``project_scope``):
    # only a token the owner itself claimed, or a signed process-tree mapping.
    attested = False
    if token_caller is not None:
        caller = token_caller
        entry = _TOKEN_BINDINGS.get(stub_session_token)
        attested = entry is not None and project_scope.owner_claimed(
            stub_session_token, token_caller.session_key, entry[1], entry[2]
        )
        logger.info(
            "stub %s resolved to the session its entry names (session_key=%s)",
            stub_uuid,
            token_caller.session_key,
        )
    elif stub_session_token:
        # Fail closed on every other token state — no claim has named it yet, or
        # the claim that did named a runtime this connection is not under. In
        # both cases every process-tree source left answers per RUNTIME, and one
        # runtime hosts many sessions. Stay identity-less until a claim names
        # this token from the runtime this stub actually belongs to.
        caller = None
        reason = (
            "unclaimed session token"
            if _token_is_unbound(stub_session_token)
            else "session token not claimed from this peer's attested runtime"
        )
        logger.info(
            "stub %s: %s — identity deferred to claim-push rather than "
            "resolved from the process tree",
            stub_uuid,
            reason,
        )
        _audit_peer_identity_denied(
            reason=f"{reason}: identity deferred to claim-push",
            peer_pid=peer_pid,
            stub_uuid=stub_uuid,
        )
    elif needs_identity and resolved_session_key and peer_pid is not None:
        # Only the key the walk resolved: the frame's principal_id and
        # channel_id are the registrant's own words, so a claim fills them in.
        caller = CallerContext(
            session_key=resolved_session_key,
            session_type="peer-resolved",
            from_gateway=True,
        )
        # The lenient walk reads mappings a local process can write. Only a
        # project-scoped stub needs more, so only it pays for the signed walk.
        if register.get("project_scoped") is True:
            try:
                signed_key, _ = await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(), facade._resolve_signed_peer_identity, peer_pid
                )
            except Exception:
                logger.exception("signed peer walk failed for peer_pid=%d", peer_pid)
                signed_key = ""
            attested = bool(signed_key) and signed_key == resolved_session_key
        _audit_peer_identity_resolved(resolved_session_key, peer_pid, stub_uuid)
        logger.info(
            "peer-resolved session_key for stub %s via peer_pid=%d",
            stub_uuid,
            peer_pid,
        )
    if (
        not stub_session_token
        and frame_caller is not None
        and (caller is None or caller.session_key != frame_caller.session_key)
    ):
        # The frame named a session the daemon did not attest for this peer.
        _audit_peer_identity_denied(
            reason=(
                f"Register named session_key={frame_caller.session_key}, which the "
                "owning gateway did not send and the peer walk does not resolve"
            ),
            peer_pid=peer_pid,
            stub_uuid=stub_uuid,
        )

    conn = _StubConn(
        stub_uuid,
        indexed_pids,
        pool_key.human_readable(),
        caller,
        pid_start_ids,
        new_tenant_nonce(),
        stub_session_token,
    )
    # Asked beside the token's caller, after the last await, on the same two
    # factors: the kernel-attested host chain, never ``indexed_pids``.
    conn.attested_pids = tuple(peer_host_pids)
    conn.bound_agents = facade._token_agents(stub_session_token, peer_host_pids, pid_start_ids)
    conn.session_attested = attested
    conn.host_pids = tuple(peer_host_pids)
    facade._conn_index_add(conn)
    return conn


async def _sent_by_owner(writer: asyncio.StreamWriter) -> bool:
    """Whether the kernel reports the sender as this daemon's owning gateway.

    The owner pid or its exec successor, the same rule an ``owner-port`` announce
    or an ``agent-binding`` is held to. An unowned daemon has no owner. One rule
    for every owner-only decision: this asks ``control._owner_sender_refusal``.
    """
    _peer_pid, refusal = await facade._owner_sender_refusal(writer)
    return not refusal


async def _attested_session_key(writer: asyncio.StreamWriter, stub_uuid: str) -> str:
    """The session gatewayd's own walk from the kernel-reported peer pid resolves, or ``""``."""
    peer_pid = socketsec.get_peer_pid(writer)
    if (
        peer_pid is None
        or socketsec.check_peer_is_self(writer) is not socketsec.PeerCredResult.MATCH
    ):
        return ""
    try:
        key, _chain = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), facade._resolve_peer_identity, peer_pid
        )
    except Exception:  # graceful degradation: nothing attested
        logger.exception("peer identity resolution failed for stub %s", stub_uuid)
        return ""
    return key


async def _apply_recaller(
    msg: dict[str, Any], conn: _StubConn, pool_label: str, writer: asyncio.StreamWriter
) -> None:
    """Apply a stub's ``recaller`` frame to ``conn``: deny by default, audited.

    Warm-pool caller repair: a stub that registered key-less (its kiro-cli was
    pool-spawned before the session was claimed) sends this once its session
    key materializes. The key is adopted only when the daemon's own peer walk
    resolves the same session. Never forwarded to the backend.
    """
    caller = conn.caller
    stub_uuid = conn.stub_uuid
    # Deny-by-default: the ONLY permitted transition is a key-less
    # connection adopting a valid session key. Compute the current
    # identity up front, reject every non-permitted case with an
    # explicit ``return``, and accept only on positive
    # confirmation of that one transition (the final branch) — any
    # unexpected state falls through to rejection, not acceptance.
    # Never forwarded to the backend. Legit warm-pool stubs only
    # ever send a recaller when their Register was key-less, so this
    # never blocks the intended path.
    existing_key = caller.session_key if caller is not None else ""
    if getattr(conn, "identity_refused", False):
        # A claim already refused to attribute this connection on a shared
        # runtime and cleared it. That clearing is a verdict, so the key-less
        # state it leaves must not read as "nothing has named me yet" and let the
        # recaller fill it: the recaller key comes from the stub's own
        # process-tree walk, which on a shared runtime is the very per-runtime
        # answer the refusal rejected. Only claim-push may name it now: a
        # tokenless claim (PID-wide reach) or a tokened claim naming this
        # connection's own token.
        logger.warning(
            "stub %s sent recaller after a co-tenancy refusal; only claim-push may name it",
            stub_uuid,
        )
        _audit_recaller_rejected(
            existing_key,
            pool_label,
            "recaller on a connection refused for co-tenancy",
        )
        return
    if conn.stub_session_token:
        # The recaller key comes from the stub's own process-tree
        # walk, so it is the same per-runtime answer the register
        # path refuses. A token-carrying connection is named by
        # claim-push or not at all — including when it already
        # carries an identity, so a recaller can never move it.
        logger.warning(
            "stub %s sent recaller while carrying a session token; only claim-push may name it",
            stub_uuid,
        )
        _audit_recaller_rejected(
            existing_key,
            pool_label,
            "recaller on a token-carrying connection",
        )
        return
    if existing_key:
        # Connection already carries an identity — reject the pivot
        # (a compromised stub must not re-bind to another session).
        attempted = _caller_from_register(msg)
        attempted_key = attempted.session_key if attempted is not None else "<none>"
        logger.warning(
            "stub %s sent recaller but caller already set (session_key=%s); ignoring",
            stub_uuid,
            existing_key,
        )
        _audit_recaller_rejected(
            existing_key,
            pool_label,
            f"recaller pivot attempt to session_key={attempted_key}",
        )
        return
    updated = _caller_from_register(msg)
    if updated is None or not updated.session_key:
        # Empty/malformed identity claim — reject and audit so ALL
        # recaller outcomes land on the SEL trail, not just pivots.
        logger.warning(
            "stub %s sent recaller with no usable session_key; ignoring",
            stub_uuid,
        )
        _audit_recaller_rejected(
            "",
            pool_label,
            "recaller frame with empty/malformed session_key",
        )
        return
    # The frame's key is the stub's own word, like the Register's: adopt it only
    # when the daemon's own walk from the kernel-reported peer pid resolves the
    # same session, the warm-pool case where the mapping appeared after register.
    attested_key = await _attested_session_key(writer, stub_uuid)
    if attested_key != updated.session_key:
        logger.warning(
            "stub %s sent recaller naming session_key=%s, which the peer walk does "
            "not resolve; ignoring",
            stub_uuid,
            updated.session_key,
        )
        _audit_recaller_rejected(
            "",
            pool_label,
            f"recaller session_key={updated.session_key} not attested by the peer walk",
        )
        return
    if conn.caller is not None or conn.identity_refused:
        # A claim named (or refused) this connection while the walk ran: it wins.
        return
    # Positive confirmation: key-less connection + a key the daemon itself
    # attests — the one allowed transition. Audit the identity change.
    caller = CallerContext(
        session_key=attested_key,
        session_type="peer-resolved",
        from_gateway=True,
    )
    conn.caller = caller
    # The lenient walk is not the signed one: a recaller key is never attested.
    conn.session_attested = False
    _audit_caller_rekey(caller.session_key, pool_label)
    logger.info(
        "stub %s recaller → session_key=%s type=%s",
        stub_uuid,
        caller.session_key,
        caller.session_type,
    )
