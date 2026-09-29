"""Kernel-attested peer identity resolution shared by gatewayd and the dashboard.

A local peer that connected over an ``AF_UNIX`` socket has a kernel-reported
PID (``SO_PEERCRED`` on Linux, ``LOCAL_PEERPID`` on macOS — see
:mod:`kiro_crew.mcp_gateway.socketsec`). Walking that PID's */proc* ancestry
and looking for the gateway-published ``session_pid_<pid>.txt`` mapping yields
a session identity the CALLER cannot forge: the pidfile is written by the
gateway on session claim, and the walk runs in the SERVER's PID namespace, so
a same-uid process cannot substitute an attacker-chosen ancestry the way it
can substitute an env var or an HTTP header.

Two consumers share this single walk:

* ``mcp_gateway.gatewayd`` resolves identities for key-less MCP stub
  registrations (and indexes the returned host chain for claim frames).
* ``dashboard.token_auth`` cross-checks the client-declared ``X-Session-Key``
  header on internal-API requests arriving over the dashboard's unix socket.

Both need identical semantics — one hardened read discipline, one ancestry
walk — which is why the walk lives here rather than being duplicated.

A pid names a PROCESS, and one kiro-cli process hosts many ACP sessions, so the
walk cannot always name ONE session. :class:`PeerTenancy` is what it answers
with: the sole session when there is one, and the recorded membership when there
are several, so a consumer verifying a DECLARED identity can still decide where
a consumer resolving one has to give up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from kiro_crew.config.loader import config_dir as _default_config_dir
from kiro_crew.mcp_caller import _parent_pid as _default_ppid
from kiro_crew.session_pid_sig import (
    REFUSAL_MALFORMED,
    PidMapping,
    read_session_pid_mapping,
    verify_session_pid_mapping,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PeerTenancy:
    """Which session(s) a kernel-reported peer pid belongs to, plus its chain.

    ``session_key`` is the ONE session the peer's ancestry names, and is empty
    when the mapping it found records SEVERAL — a pid names a process, and one
    kiro-cli process hosts many ACP sessions, so on a shared runtime there is no
    single answer to give.

    That is not the same as knowing nothing, which is why this type exists
    beside the plain key. A consumer RESOLVING the peer's identity has nothing
    to act on and must treat the empty key as unresolved. A consumer VERIFYING
    an identity the peer DECLARED can still decide, by asking whether the
    declared key is one of the recorded members: the tenant section is covered
    by the mapping's MAC, so membership is attested rather than self-asserted.

    ``chain`` is the full host ancestor pid list (peer first) and is always
    complete, whatever the key resolved to — gatewayd indexes stub connections
    on it, and identity repair happens later via claim-push.

    ``unverifiable`` says the walk PASSED a mapping that exists and would not
    VERIFY. It is not the same as finding nothing, and a consumer that denies or
    attests must not treat it as such: the file is replaced in two steps
    (``.txt`` then ``.sig``), so a body-changing republication is briefly visible
    as a MAC mismatch, and reading that as "this pid hosts one session" would let
    a co-tenant drive requests into the window to skip an attestation the
    resolved mapping would have demanded.

    A proven RECYCLE is deliberately NOT one of these. It is reported only after
    the signature verifies, so it is the publisher's own attested statement that
    the pid now belongs to a different process, and the orphan sweep leaves such
    a file for every live recycled pid. It says nothing about whether THIS caller
    shares its pid, so it belongs on the same footing as no record at all.
    """

    session_key: str = ""
    tenants: tuple[str, ...] = ()
    tenant_count: int = 0
    chain: list[int] = field(default_factory=list)
    unverifiable: bool = False

    @property
    def shared(self) -> bool:
        """True only on positive evidence of several sessions on the pid."""
        return self.tenant_count > 1

    @property
    def membership_complete(self) -> bool:
        """True when every session on the pid is known, so absence is decisive.

        Either the pid names one session, or its enumerated members match the
        recorded count. The mapping file is size-bounded, so a runtime with very
        many sessions records the true count without every key. A consumer must
        not read a declared key's absence from a TRUNCATED set as grounds to
        deny.
        """
        if self.session_key:
            return True
        return bool(self.tenant_count) and len(self.tenants) == self.tenant_count

    def admits(self, session_key: str) -> bool:
        """True when *session_key* is attested as living on the peer's process."""
        if not session_key:
            return False
        return session_key == self.session_key or session_key in self.tenants


def resolve_peer_tenancy(
    peer_pid: int,
    *,
    config_dir_fn: Callable[[], Path] | None = None,
    ppid_fn: Callable[[int], int] | None = None,
    signed_only: bool = False,
) -> PeerTenancy:
    """Walk the peer's real-PID ancestry: its session(s) plus the host chain.

    The full form of :func:`resolve_peer_identity`, which is the ``(key,
    chain)`` view of this and stays the right call for a consumer that only
    wants the name. Use this one when the peer may sit on a SHARED runtime and
    a declared identity has to be checked against the recorded membership
    rather than against one key.

    The walk stops reading mappings at the first ancestor that answers — a
    named session, or a recorded tenant set — and keeps walking for the chain.

    ``signed_only=True`` requires each mapping's HMAC sidecar to verify, and is
    REQUIRED for any authorization use: the bare ``.txt`` is same-uid
    agent-writable, so an unsigned mapping proves only what a local process
    chose to write, and that applies to its tenant list as much as to its key.

    Blocking /proc + filesystem I/O — async callers MUST offload this to an
    executor (both consumers do).
    """
    if config_dir_fn is None:
        config_dir_fn = _default_config_dir
    if ppid_fn is None:
        ppid_fn = _default_ppid

    try:
        cfg_dir = config_dir_fn()
    except Exception:
        return PeerTenancy()

    read = verify_session_pid_mapping if signed_only else read_session_pid_mapping
    answer: PidMapping | None = None
    unverifiable = False
    chain: list[int] = []
    pid = peer_pid
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        chain.append(pid)
        if answer is None:
            try:
                mapping = read(pid, cfg_dir)
            except OSError:
                mapping = PidMapping()
            if mapping.session_key or mapping.shared:
                answer = mapping
            elif mapping.refusal == REFUSAL_MALFORMED:
                # A mapping that EXISTS and could not be TRUSTED. The walk keeps
                # going -- only an answer is specific to this pid -- but the fact
                # is carried out, because a caller that denies or attests owes a
                # different answer to "a record here would not verify" than to
                # "there was no record".
                #
                # MALFORMED only, deliberately, and this is the narrowest set
                # that covers the threat. On this signed path it means the MAC
                # did not match, which is the two-step republication window and
                # is indistinguishable from forgery. A RECYCLE is the opposite:
                # it is reported only AFTER the signature verifies, so it is the
                # publisher's own attested statement that this pid now belongs
                # to a different process -- knowledge, not ambiguity -- and the
                # orphan sweep deliberately leaves such a file on disk for every
                # live recycled pid. Treating it as unverifiable would deny the
                # tokenless callers the degrade arm exists for (cron scripts,
                # pooled MCP backends) for as long as that stale file sits in
                # their ancestry.
                unverifiable = True
        try:
            pid = ppid_fn(pid)
        except (OSError, ValueError):
            # Target exited mid-walk (/proc/<pid>/stat gone or malformed).
            break
    if answer is None:
        return PeerTenancy(chain=chain, unverifiable=unverifiable)
    return PeerTenancy(
        session_key=answer.session_key,
        tenants=answer.tenants,
        tenant_count=answer.tenant_count,
        chain=chain,
    )


def resolve_peer_identity(
    peer_pid: int,
    *,
    config_dir_fn: Callable[[], Path] | None = None,
    ppid_fn: Callable[[int], int] | None = None,
    signed_only: bool = False,
) -> tuple[str, list[int]]:
    """Walk the peer's real-PID ancestry (server-side): session key + host chain.

    Runs in the calling server's own PID namespace (real pids), so it works
    regardless of how the peer sees the world. A single /proc walk returns
    both:

    * the session_key from the first ancestor with a ``session_pid_<pid>.txt``
      file (``""`` when none matches — normal for warm-pool runtimes before
      claim, cron scripts, and pooled MCP backends, and also what a pid hosting
      SEVERAL sessions yields, since one key cannot name them), and
    * the full HOST ancestor PID chain (peer first). gatewayd indexes stub
      connections under this chain so a later ``claim`` frame — which always
      carries the runtime's HOST pid — matches even when the stub's
      self-reported ``ancestor_pids`` are namespace-local (sandbox
      PID-namespace topology).

    The walk continues past a session-key match so the chain is complete for
    claim matching at any ancestry level.

    ``config_dir_fn`` / ``ppid_fn`` are injection seams for the callers'
    existing test surfaces; they default to the shared production
    implementations. Reads go through :func:`~kiro_crew.session_pid_sig.
    read_session_pid_txt` (symlink refusal, regular-file check, size bound) —
    the caller is a trusted process reading a predictable, agent-writable
    path, the exact symlink-planting surface that hardened reader closes.

    ``signed_only=True`` additionally requires each mapping's HMAC sidecar to
    verify (:func:`~kiro_crew.session_pid_sig.verify_session_pid`, pid bound
    into the MAC, keyed by the agent-unreadable SEL trust root). REQUIRED for
    any AUTHORIZATION use of the result: the bare ``.txt`` is same-uid
    agent-writable, so an unsigned mapping proves only what a local process
    chose to write — an attacker planting ``session_pid_<own_pid>.txt`` with
    a victim's key would otherwise turn kernel peer attestation into a
    self-serve identity oracle. gatewayd's stub-registration walk stays
    lenient (``False``): there the result only ATTRIBUTES a stub for
    claim-indexing, and warm-pool mappings may legitimately predate the SEL
    key.

    :func:`resolve_peer_tenancy` is the same walk with the recorded membership
    attached, for a consumer that must verify a DECLARED key on a shared pid
    instead of reading the empty answer as "unknown".

    Blocking /proc + filesystem I/O — async callers MUST offload this to an
    executor (both consumers do).
    """
    tenancy = resolve_peer_tenancy(
        peer_pid,
        config_dir_fn=config_dir_fn,
        ppid_fn=ppid_fn,
        signed_only=signed_only,
    )
    return tenancy.session_key, tenancy.chain
