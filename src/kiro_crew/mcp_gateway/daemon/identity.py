"""Who is on the other end of a stub connection, and which session it acts for.

The peer principal gate, the Register frame's caller, the kernel-attested host
ancestry, the per-connection :class:`_StubConn` and the index claim-push retargets
through, and the session-token bindings a ``claim`` records. Identity is never
inferred from a frame field the registrant controls where the kernel or the
gateway can answer instead.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection, Optional

from kiro_crew.executors import subprocess_executor
from kiro_crew.mcp_caller import CallerContext, new_tenant_nonce
from kiro_crew.mcp_gateway import socketsec
from kiro_crew.mcp_gateway.daemon import logger
from kiro_crew.mcp_gateway.daemon.audit import (
    _audit_caller_rekey,
    _audit_peer_identity_denied,
    _audit_peer_identity_resolved,
    _audit_recaller_rejected,
)
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
        #: Refused is a VERDICT, not an absence: only claim-push may clear it, by
        #: naming this connection's own token.
        self.identity_refused = False
        self.pid_start_ids = pid_start_ids if pid_start_ids is not None else {}
        self.stub_session_token = stub_session_token
        # Namespace separator for a connection whose session the gateway cannot
        # name, forwarded to the backend on every request. GATEWAY-minted
        # and never derived from the Register frame: ``stub_uuid`` arrives from
        # the stub, so a nonce derived from it would let one stub choose to share
        # an unnamed peer's per-tenant namespace. Independent of ``caller``,
        # which may be retargeted by a later claim-push while this stays put.
        self.tenant_nonce = tenant_nonce


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
_MAX_TOKEN_BINDINGS = 512

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
    for any other token; else the Register frame's own caller, or for a key-less
    one the session the kernel-attested peer walk resolves. The token is asked once,
    after the last await, and the connection is indexed before anything can await
    again.
    """
    caller = _caller_from_register(register)

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

    # Server-side peer identity: when the stub self-reports an empty
    # session_key, resolve it from the peer's REAL pid (SO_PEERCRED) via a
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
            if needs_identity:
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
    # sources exactly as before for a connection carrying no token.
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
    if token_caller is not None:
        caller = token_caller
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
        caller = CallerContext(
            session_key=resolved_session_key,
            session_type="peer-resolved",
            principal_id=str(
                # ``user_identity`` is the legacy spelling an older
                # stub may still send; the field was deleted from
                # PoolKey but stays honored here as a diagnostic.
                register.get("principal_id")
                or register.get("user_identity")
                or ""
            ),
            channel_id=str(register.get("channel_id") or ""),
            from_gateway=True,
        )
        _audit_peer_identity_resolved(resolved_session_key, peer_pid, stub_uuid)
        logger.info(
            "peer-resolved session_key for stub %s via peer_pid=%d",
            stub_uuid,
            peer_pid,
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
    facade._conn_index_add(conn)
    return conn


def _apply_recaller(msg: dict[str, Any], conn: _StubConn, pool_label: str) -> None:
    """Apply a stub's ``recaller`` frame to ``conn``: deny by default, audited.

    Warm-pool caller repair: a stub that registered key-less (its kiro-cli was
    pool-spawned before the session was claimed) sends this once its session
    key materializes. Never forwarded to the backend.
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
        # answer the refusal rejected. Only a claim carrying this connection's
        # own token may name it now.
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
    # Positive confirmation: key-less connection + valid recaller
    # key — the one allowed transition. Audit the identity change.
    caller = updated
    conn.caller = updated
    _audit_caller_rekey(caller.session_key, pool_label)
    logger.info(
        "stub %s recaller → session_key=%s type=%s",
        stub_uuid,
        caller.session_key,
        caller.session_type,
    )
