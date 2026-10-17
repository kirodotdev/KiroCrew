"""Who owns a local forward port, for ``kiro_crew.instances.ssh_tunnel_manager``.

The ports withheld from allocation, the OS holds on lent hop ports and the invariant
that every live lease is owned by a forward or held, the signed identity record of a
live forwarder child, and reclaiming our own forwarder that a gateway hard-kill
leaked.

Each ``SshTunnelManager`` member here is a module-level function whose first parameter
is ``self``; the class in :mod:`kiro_crew.instances.ssh_tunnel_manager` binds it by
name, so the instance state, every patch of a method and every ``__new__`` build stay
on the one class.

Instance fields written: ``_lent_hops``.

Instance fields read: ``_hop_guard``, ``_pending_lends``, ``_registry``,
``_ssh_compression``, ``_tunnels``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew import platform_compat
from kiro_crew.instances.port_allocator import _is_addr_free, _is_port_free
from kiro_crew.instances.registry import _NO_FORWARDER_PID, Instance
from kiro_crew.instances.ssh_tunnel_manager_child import TunnelState, _SshTunnel
from kiro_crew.instances.ssh_tunnel_manager_child_text import (
    _build_ssh_tunnel_argv,
    _build_ssm_tunnel_argv,
)
from kiro_crew.instances.ssh_tunnel_manager_transport import _TransportParams
from kiro_crew.instances.token_mint import ssh_spawn_argv_env
from kiro_crew.sel import _HMAC_KEY_MIN_BYTES as _SEL_HMAC_KEY_MIN_BYTES
from kiro_crew.sel import sel, sel_hmac_key_path

if TYPE_CHECKING:
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

#: The manager's own logger: this code logs under the facade module's name.
logger = logging.getLogger("kiro_crew.instances.ssh_tunnel_manager")


# Grace given to a reclaimed (hard-kill-orphaned) forwarder after SIGTERM
# before escalating to SIGKILL, and after SIGKILL before giving up waiting.
# `ssh -N` exits on TERM essentially immediately; the escalation mirrors
# _SshTunnel._terminate's shape with tighter bounds because this runs inside
# connect() under the manager lock. Nothing in the connect depends on the wait
# completing — the recorded port stays excluded from allocation either way —
# so a slow exit costs only these bounded seconds, never correctness.
_RECLAIM_TERM_GRACE_SECS = 2.0
_RECLAIM_KILL_GRACE_SECS = 1.0
# Liveness poll cadence while waiting out the grace windows above.
_RECLAIM_POLL_INTERVAL_SECS = 0.05


# Domain tag for the forwarder-identity MAC subkey. Mirrors the
# ``session_pid_sig`` precedent: the signing key is a one-way derivation of
# the SEL trust root and this domain, so this protocol, the SEL audit chain,
# and the session-pid sidecars never share a signing key and a MAC produced by
# any of them is valueless to the others.
_RECLAIM_SIG_DOMAIN = b"kirocrew-forwarder-identity-v1"


def _reclaim_identity_key() -> bytes | None:
    """Derive the forwarder-identity signing subkey, or ``None`` when absent.

    Anchored on the SEL trust root (``sel_hmac_key_path()``), which only the
    gateway creates and which sits on the sensitive-path deny list — an agent
    can neither read nor replace it, which is the entire point: a signature
    under this key is a claim only the GATEWAY can have made. Never creates
    the key (a first-touch race would mint a root the SEL then distrusts);
    when it cannot be read the reclaim protocol degrades to "never reclaim"
    (fail closed) rather than trusting unsigned registry state.
    """
    try:
        raw = sel_hmac_key_path().read_bytes()
    except OSError:
        return None
    if len(raw) < _SEL_HMAC_KEY_MIN_BYTES:
        return None
    return hmac.new(raw, _RECLAIM_SIG_DOMAIN, hashlib.sha256).digest()


def _forwarder_orphan_state(pid: int, forwarder_start: str) -> tuple[bool, int]:
    """Return ``(orphaned, parent_pid)`` for a recorded forwarder child.

    "Orphaned" means the gateway that spawned *pid* has exited, so
    nothing will ever reap it. Blocking (process-table reads), so callers run it
    off the event loop.

    POSIX: the kernel re-parents an orphan when its parent dies, so the test is
    ``get_ppid == 1``. A live gateway's forwarder still names that gateway, and a
    subreaper host names the subreaper, so both read as not orphaned and the
    reclaim fails closed.

    Windows never re-parents: the parent pid stays whatever it was at spawn, dead
    or alive, and the number can be handed to a new process later. So the
    recorded parent counts as gone when either:

    * nothing runs at that pid (and its start time cannot be read either); or
    * the process now at that pid started AFTER the forwarder, so it cannot be
      the process that spawned it (pid reuse).

    A parent that is alive and started before the forwarder -- this gateway or a
    second one on the same box -- is refused. So is every case the order cannot
    be settled: an unreadable parent pid, a parent that exists but whose start
    time is unreadable, or equal or unparsable start tokens.
    """
    ppid = platform_compat.get_ppid(pid)
    if not platform_compat.IS_WINDOWS:
        return ppid == 1, ppid
    if ppid <= 0:
        return False, ppid
    parent_start = platform_compat.process_start_time(ppid)
    if parent_start is None:
        return not platform_compat.pid_exists(ppid), ppid
    return platform_compat.created_after(parent_start, forwarder_start), ppid


def _forwarder_identity_sig(
    key: bytes,
    instance_id: str,
    pid: int,
    start: str,
    port: int,
    argv_sig: str = "",
    transport: str = "",
) -> str:
    """MAC over one instance's recorded forwarder identity.

    Binds the identity to the INSTANCE as well as to the process attributes,
    so a valid record cannot be replayed under another instance id, and any
    edit to pid, start time, port, or argv fingerprint invalidates it. NUL
    joints keep field boundaries unambiguous (no recorded field can contain a
    NUL: the id and port are charset/range-validated, the start value is a
    single ``/proc``/``ps``/FILETIME token, and the fingerprint is hex).

    An empty *argv_sig* signs the four-field message older gateways wrote, so
    their records still verify. A non-empty one adds two fields, the fingerprint
    and the *transport* the child was spawned with (``"ssh"`` or ``"ssm"``).
    The transport decides whether reclaim signals the pid or its whole process
    group, and with a fingerprint there is no rebuilt argv left to tie that
    choice to the spawned child, so it is signed: a record whose transport was
    switched since spawn fails verification. The messages differ in field
    count, so deleting the fingerprint from a record signed with it fails
    verification instead of falling back to the older check.
    """
    fields = [instance_id, str(pid), start, str(port)]
    if argv_sig:
        fields += [argv_sig, transport]
    msg = "\0".join(fields).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _verify_and_reclaim_forwarder(
    pid: int,
    expected_start: str,
    expected_argv: list[str],
    port: int,
    tree: bool,
    audit_resources: str,
    expected_argv_sig: str = "",
) -> str:
    """Verify a recorded forwarder's identity, then SIGTERM/SIGKILL-reclaim it.

    Blocking by design — process-attribute reads, signals, liveness polls —
    so callers run it in a worker thread, never on the event loop. Doing the
    verification and the first signal in ONE thread keeps the check-to-signal
    window at in-process microseconds, the same residual the repo's other
    pid-reuse guards accept.

    Identity is pid + start time + argv, checked in that order. The argv half
    is the fingerprint recorded at spawn (*expected_argv_sig*) compared with the
    live one, or, for a record without a fingerprint, *expected_argv* compared
    element for element. The
    start-time comparison is RE-RUN before the destructive SIGKILL: the grace
    window is exactly the interval in which the pid can exit and be recycled,
    and ``pid_exists`` polling cannot observe an exit that is immediately
    followed by reuse. Mirrors the stale-app-backend reaper's guard
    (leak-not-mis-kill): any unconfirmed identity withholds the signal.

    ``tree=True`` for the SSM transport, whose child was spawned into its own
    process group (``start_new_session``, so pgid == pid) — the group signal
    reaps the ``session-manager-plugin`` grandchild still holding the
    forwarded port, and completion is judged by :func:`pgroup_exists` plus the
    port actually releasing, so a wrapper that exits first cannot fake
    success while the plugin keeps the port. If the group leader is already
    reaped by SIGKILL time, the group cannot be re-addressed through the
    existing pid-keyed helpers; the TERM broadcast has already reached every
    member, and a member that ignores it keeps the port — reported truthfully
    as not reclaimed (the port stays excluded from allocation). The ssh child
    is spawned WITHOUT a new group: after a gateway hard-kill it sits in the
    DEAD gateway's process group, where a group signal could hit unrelated
    survivors — so it gets a pid-scoped signal only, which suffices because
    ``ssh -N`` holds the forward itself and spawns no descendants of its own.

    Returns one of: ``"reclaimed"`` (identity confirmed, process/group gone,
    port released), ``"identity_mismatch"`` (nothing was ever signalled),
    ``"recycled_during_grace"`` (SIGKILL withheld: the pid stopped matching
    its recorded identity during the TERM grace), ``"not_gone"`` (signals
    delivered but the process, group, or port is still held at the end).
    Every path that delivered at least one signal emits a SEL audit event.
    """

    def _identity_holds() -> bool:
        now = platform_compat.process_start_time(pid)
        if now is None or now != expected_start:
            return False
        # Both halves, both times: the start token alone is 1s-granular on
        # macOS (``ps -o lstart=``), so a same-second pid reuse could keep it
        # matching while the process is someone else's — the argv half breaks
        # that tie. A mid-death target whose argv is already unreadable reads
        # as not-held and merely withholds the escalation (TERM was already
        # delivered to the verified process).
        if expected_argv_sig:
            observed = platform_compat.process_argv_fingerprint(pid)
            return observed is not None and hmac.compare_digest(
                observed.encode("ascii"), expected_argv_sig.encode("utf-8")
            )
        return platform_compat.process_argv_matches_exact(pid, list(expected_argv))

    def _alive() -> bool:
        return platform_compat.pgroup_exists(pid) if tree else platform_compat.pid_exists(pid)

    def _gone() -> bool:
        # Single-address on purpose: the question here is "did OUR forwarder let
        # go of the port it held", and an ``ssh -L`` child binds 127.0.0.1 alone.
        # The aggregate ``_is_port_free`` would answer a DIFFERENT question -- "is
        # this port free for a new forward" -- so an unrelated ::1 listener would
        # make a fully reclaimed orphan report not-gone and mis-attribute this
        # reclaim's audited outcome.
        return not _alive() and _is_addr_free(port, "127.0.0.1")

    def _deliver(sig: int) -> None:
        if tree and _SshTunnel._signal_group(pid, sig):
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError, ValueError):
            # Pinned on the recorded start time. POSIX delegates straight to
            # ``kill_pid``. On Windows the command-line read in ``_identity_holds``
            # is a WMI query that takes about a second, so the start time is
            # checked AGAIN under an open process handle right at the kill: the
            # pid taskkill resolves cannot be a recycled one.
            platform_compat.kill_pid_pinned(pid, expected_start, sig)

    def _wait_gone(grace_secs: float) -> bool:
        deadline = time.monotonic() + grace_secs
        while time.monotonic() < deadline:
            if _gone():
                return True
            time.sleep(_RECLAIM_POLL_INTERVAL_SECS)
        return _gone()

    def _audit(outcome: str) -> None:
        try:
            sel().log_api_access(
                caller="gateway",
                operation="forwarder_orphan_reclaim",
                outcome=outcome,
                resources=audit_resources,
            )
        except Exception as exc:  # noqa: BLE001 — audit must never break reclaim
            logger.debug("SEL audit failed for forwarder_orphan_reclaim: %s", exc)

    if tree and platform_compat.IS_WINDOWS:
        # The SSM group signal on Windows is an unpinned ``taskkill /T``, and a
        # pinned pid signal would end only the ``aws`` wrapper: the plugin child
        # keeps the port, and with the wrapper gone nothing recorded points at it
        # any more, so the leak could never be reclaimed. Signal nothing.
        return "windows_group_unsupported"
    if not _identity_holds():
        return "identity_mismatch"
    _deliver(platform_compat.SIGTERM)
    if _wait_gone(_RECLAIM_TERM_GRACE_SECS):
        _audit("sigterm")
        return "reclaimed"
    # Re-confirm identity before the destructive escalation, and withhold the
    # SIGKILL on ANYTHING short of a positive match — including a pid that no
    # longer exists while its group/port linger. A recycled pid would make
    # ``getpgid`` resolve (and the group kill target) the REPLACEMENT process,
    # and ``pid_exists`` polling cannot distinguish "still our child" from
    # "exited and recycled inside a poll gap", so absence of the pid is not a
    # safe fall-through: no verified identity, no SIGKILL
    # (leak-not-mis-kill). The TERM broadcast above already reached every
    # group member while the identity was verified; a member that ignores it
    # keeps the port, which stays excluded from allocation and is reported
    # truthfully below.
    if not _identity_holds():
        _audit("sigkill_withheld_identity_unconfirmed")
        return "recycled_during_grace"
    _deliver(platform_compat.SIGKILL)
    if _wait_gone(_RECLAIM_KILL_GRACE_SECS):
        _audit("sigkill")
        return "reclaimed"
    _audit("not_gone")
    return "not_gone"


async def _forwarder_identity_hints(
    self: SshTunnelManager, instance_id: str, tunnel: _SshTunnel
) -> dict[str, Any]:
    """Build the registry hints that record *tunnel*'s live forwarder child.

    The recorded identity is ``forwarder_pid`` + ``forwarder_start`` + the
    ``local_port`` that child is bound to, plus ``forwarder_argv_sig`` (the
    fingerprint of the argv the kernel reports for it right now),
    authenticated by ``forwarder_sig`` — a MAC over all of them plus the
    instance id. Every
    field is read from the LIVE tunnel, so the set describes one process
    rather than a mix of one process and another's port: a pid recorded
    against a port it was not signed with leaves the signature failing
    verification, and the orphan reclaim then refuses the very child the
    write exists to record.

    ``was_connected=True`` rides along because a live forwarder is exactly
    what makes an instance auto-reconnectable, so the flag and the identity
    of the child it refers to become durable in the same write.

    Two states persist as a deliberate fail-closed miss rather than an
    error: a start time that cannot be read records as ``""``, and an
    unreadable signing key leaves ``forwarder_sig`` empty. Either one
    disables the reclaim for this child — :meth:`_reclaim_orphan_forwarder`
    requires both — which loses a leaked process at the next hard-kill but
    can never signal the wrong one.

    Both blocking reads (the platform start-time query and the key read)
    go off the event loop: every caller holds the manager lock, where a
    synchronous read would stall unrelated requests and heartbeats.

    Returns the COMPLETE kwarg set for :meth:`_persist_hint`; a caller adds
    only its own extras. :meth:`connect` and :meth:`_mark_recovered` both
    write this record, and one builder is what keeps their field sets
    identical: a field added to the identity here reaches both writes,
    where two hand-written kwarg lists can carry it in one and omit it in
    the other — and a record missing the port ``forwarder_sig`` signs over
    fails its own verification.
    """
    forwarder_pid = tunnel.pid or _NO_FORWARDER_PID
    # The port the forwarder actually bound, which for a rebuild can differ
    # from the one connect() first allocated (see _recover).
    local_port = tunnel.status.local_port
    forwarder_start = ""
    forwarder_sig = ""
    forwarder_argv_sig = ""
    if forwarder_pid > 0:
        started = await asyncio.to_thread(platform_compat.process_start_time, forwarder_pid)
        forwarder_start = started or ""
    if forwarder_pid > 0 and forwarder_start:
        # The argv as the kernel shows it NOW, so reclaim compares like with
        # like. Unreadable records "" and reclaim keeps the rebuilt-argv check.
        observed = await asyncio.to_thread(platform_compat.process_argv_fingerprint, forwarder_pid)
        forwarder_argv_sig = observed or ""
        key = await asyncio.to_thread(_reclaim_identity_key)
        if key is not None:
            forwarder_sig = _forwarder_identity_sig(
                key,
                instance_id,
                forwarder_pid,
                forwarder_start,
                local_port,
                forwarder_argv_sig,
                tunnel.transport,
            )
    return {
        "local_port": local_port,
        "forwarder_pid": forwarder_pid,
        "forwarder_start": forwarder_start,
        "forwarder_sig": forwarder_sig,
        "forwarder_argv_sig": forwarder_argv_sig,
        "was_connected": True,
    }


def _reserved_ports(self: SshTunnelManager) -> set[int]:
    """Ports already taken: live tunnels, recorded local_ports, LENT hops.

    The third source is what stops a chained credential reaching a crew it was
    not minted for. A hub riding one of our hops holds the port NUMBER; when the
    crew behind it disconnects, that port returns to the free set and is the
    FIRST one handed to the next connect, so the hub's forward -- still pointed
    at the number -- would deliver one crew's bearer token to another crew's
    gateway. Withholding the port for as long as the credential we issued is
    valid removes the moment that could happen in.

    The deadline is ours, not the hub's: it is the TTL this gateway minted the
    token under, so nothing is expected of the hub and no state crosses the
    boundary. Once it passes the token is dead, and the port is ordinary again.

    Read from the registry rather than from memory so a restart cannot drop a
    reservation while the credential it protects is still live.
    """
    reserved: set[int] = {t.status.local_port for t in self._tunnels.values()}
    for inst in self._registry.list():
        if inst.local_port:
            reserved.add(inst.local_port)
    # Leases are document-level, not per-row: a crew can be REMOVED while a
    # credential naming its hop is still valid, and the port has to stay withheld
    # across that -- which a field on the deleted row could not do.
    reserved |= self._registry.live_hop_leases()
    return reserved


def _port_recorded_by_another_row(self: SshTunnelManager, port: int, own_id: str) -> bool:
    """Does any registry row OTHER than ``own_id`` record ``port`` as its local_port?

    BLOCKING -- reads the registry from disk, so call it from a thread, the same
    rule as :meth:`_reserved_ports`.

    A crew's own recorded port is dropped from the exclude set so its origin
    preference is reachable, but two rows can record the SAME port number (a
    duplicate hint left by an earlier allocation). Removing that number from the
    exclude set on this crew's behalf would also unreserve it for the other row,
    letting this crew's forward bind a port a hub still forwards another crew's
    bearer token to -- the confused-deputy the reservation exists to close. The
    port is this crew's to reclaim only when no other row still records it.
    """
    return any(other.id != own_id and other.local_port == port for other in self._registry.list())


def sync_hop_holds(self: SshTunnelManager) -> set[int]:
    """Hold every lent hop port no live forward is serving. Returns what failed.

    BLOCKING, and named so: it reads the registry from disk. Call it from a thread
    (``await asyncio.to_thread(...)``) or use :meth:`_apply_hop_holds` when the
    lease table is already in hand -- the same rule, and for the same reason, as
    ``_reserved_ports`` a few hundred lines above, whose own comment says a
    synchronous version here "would stall unrelated requests and heartbeats".

    The companion to the lease in :meth:`_reserved_ports`, and the half that has
    teeth outside this process: the lease keeps a lent port out of OUR allocator,
    this keeps it out of every other binder on the host.

    Call it at startup as well as on teardown, and that is not belt-and-braces: the
    lease is PERSISTED and a socket is not, so after a restart every live lease
    would otherwise name a port this gateway withholds from itself and owns in no
    other sense.
    """
    return self._apply_hop_holds(self._registry.live_hop_lease_deadlines())


def hop_ownership_violations(
    self: SshTunnelManager, leases: dict[int, float] | None = None
) -> dict[int, str]:
    """THE invariant, in one place:

        For every live lease, either a live forward OWNS its port or the guard HOLDS
        it.

    Returns a ``port -> why`` map of leases satisfying neither; empty means the
    invariant holds. This exists because the same defect kept arriving by new routes
    -- an orphaned forwarder at startup, a hold that failed once, an ``ssh`` child
    exiting unexpectedly, the same past ``max_recovery_attempts`` -- and each was a
    path nobody had enumerated rather than a new kind of mistake. A list of call sites
    is forgotten by the next path added; a predicate is not, so the tests drive states
    against THIS and stop asking whether some site was remembered.

    Deliberately read-only: it reports, and :meth:`_apply_hop_holds` is what repairs.
    A violation means a chained bearer credential names a port nothing owns.
    """
    if leases is None:
        leases = self._registry.live_hop_lease_deadlines()
    owners = {
        t.status.local_port: iid
        for iid, t in self._tunnels.items()
        if t.status.local_port and t.status.state is TunnelState.CONNECTED
    }
    held = self._hop_guard.held_ports()
    violations: dict[int, str] = {}
    for port in sorted(leases):
        if port in owners or port in held:
            continue
        stale = [
            iid
            for iid, t in self._tunnels.items()
            if t.status.local_port == port and t.status.state is not TunnelState.CONNECTED
        ]
        violations[port] = (
            f"a {stale[0]!r} entry mentions it but is not CONNECTED, so no forward "
            "owns it and no hold was taken"
            if stale
            else "no forward owns it and no hold was taken"
        )
    return violations


def _apply_hop_holds(self: SshTunnelManager, leases: dict[int, float]) -> set[int]:
    """Bind the holds for an ALREADY-READ lease table. Returns what failed.

    Split out so a caller that must not widen the gap between a port's release and
    its hold can take the slow read beforehand and leave only the binds here.

    **A port counts as in use only while a forward is actually LISTENING on it --
    CONNECTED state -- not while any tunnel object remembers the number.** The
    forward's own listener is the ownership in that state, and two sockets cannot
    share the address, so a held port is excluded rather than bound. But an
    unexpected ``ssh`` exit leaves its tunnel in ``_tunnels`` with ``local_port``
    intact and the state ERROR, and past ``max_recovery_attempts`` it stays there:
    counting that as in use would skip the hold for a port the OS has already freed
    while the credential naming it is still valid, which is the exposure itself.

    Deliberately NARROWER than :meth:`_reserved_ports`, which counts every port any
    tunnel or row remembers and is RIGHT to. The two run in opposite safety
    directions: over-counting costs the allocator only a port it declines to reuse,
    while over-counting here withholds a hold. Under-counting here is the cheap
    error -- the bind simply loses to the live forward, and the port is recorded as
    owed and retried.
    """
    in_use = {
        t.status.local_port
        for t in self._tunnels.values()
        if t.status.local_port and t.status.state is TunnelState.CONNECTED
    }
    # The registry is the source of truth, so every settle re-seeds the cache
    # from it rather than trusting what memory accumulated: a lapsed or
    # removed lease disappears here, and a restart rebuilds the cache whole.
    # The one exception is a lend whose write has not resolved yet: the registry
    # cannot name it and a plain re-seed would drop it, which would leave the
    # synchronous exit seam with nothing to bind during exactly the window the
    # early registration exists to cover. Each of these is removed as soon as its
    # write resolves, so this cannot accumulate.
    #
    # Bound ONCE and given to both readers, because they are two halves of one
    # answer and giving them different sets is a hole rather than an inconsistency:
    # `sync` releases every held or owed port absent from what it is handed
    # (`hop_port_guard.sync`), so passing the registry-only snapshot here let a
    # settle landing inside an in-flight lend release a hold the exit seam had
    # already taken -- leaving the port unowned for a whole recovery backoff while
    # the chained credential naming it stayed valid. The cache got the union and the
    # guard did not, which is exactly the divergence this single binding removes.
    settled = {**dict(leases), **self._pending_lends}
    self._lent_hops = settled
    return self._hop_guard.sync(settled, in_use)


async def _reclaim_orphan_forwarder(
    self: SshTunnelManager, inst: Instance, params: _TransportParams
) -> None:
    """Reclaim *inst*'s own forwarder child leaked by a gateway hard-kill.

    A SIGKILLed gateway never runs teardown, so its forwarder child
    survives, reparented to init with nothing left to reap it: one idle
    process, one loopback port, and one live session to the remote per
    hard-kill → reconnect cycle. The restarted manager finds the recorded
    ``local_port`` occupied and (correctly) allocates around it; this hook
    is what turns that permanent leak into a reclaim.

    Reclamation is keyed on OUR OWN recorded identity, never a
    process-table match — matching the table by argv pattern would
    SIGTERM forwards operators opened themselves, and no
    pattern can distinguish our child from a stranger's. The registry
    itself is agent-writable, so a recorded claim is honored only when it
    AUTHENTICATES: the record must carry the gateway's own MAC over
    (instance id, pid, start time, port), computed at spawn under a key
    derived from the SEL trust root — which an agent can neither read nor
    replace — so a record written or re-pointed by anything but this
    gateway fails verification outright. Behind the MAC, defense in depth
    from kernel-owned facts: the candidate must be a genuine ORPHAN — not
    a pid this manager currently supervises, and whose spawning gateway is
    gone (:func:`_forwarder_orphan_state`: reparented to init on POSIX; on
    Windows, a recorded parent that is dead or was replaced by a later
    process), which no live gateway's forwarder is. Then the
    recorded pid is trusted only behind a STRICT identity check, both
    halves recorded at spawn: the pid's start time must equal the recorded
    ``forwarder_start``, AND its full argv must exactly equal the forward
    command line this manager would construct for the recorded port (host
    and all) — or, when the record carries ``forwarder_argv_sig``, the
    live argv's fingerprint must equal that signed spawn-time one. Anything less — either hint missing, process gone,
    attributes unreadable, or any element differing — means the identity
    cannot be confirmed: the process is left alone and connect falls
    through to normal allocation. The start-time half is what defeats pid
    recycling (argv can collide in principle; a recycled pid's start time
    cannot), and it is re-verified before the SIGKILL escalation inside
    the worker. Best-effort: a failed reclaim never fails the connect, it
    only leaves the leak for the next attempt.

    The fingerprint is what survives drift: a rebuilt command line can
    differ from the spawned one without any foul play (an edited
    compression/host setting, or an ``aws`` entrypoint the kernel rewrites
    through a shebang), but the kernel's own view of the child is the same
    at spawn and at reclaim. Only a record without a fingerprint (written
    before the field existed, or whose argv could not be read at spawn)
    still falls back to the rebuild, where such drift misses the reclaim,
    never mis-kills, and is logged below.

    Runs under the manager lock (its caller ``connect`` holds it); every
    blocking step — the port probe and the verify-and-signal worker — is
    pushed off the event loop via ``asyncio.to_thread``.
    """
    pid = inst.forwarder_pid
    port = inst.local_port
    start = inst.forwarder_start
    sig = inst.forwarder_sig
    argv_sig = inst.forwarder_argv_sig
    if pid <= 1 or port <= 0 or not start or not sig:
        return  # identity not (fully) recorded — nothing we may touch
    # FAIL CLOSED on a port whose chained credential is still valid: leave the
    # orphan alone. Reclaiming exists to give this crew its recorded port BACK, and
    # a lent port is one `allocate` deliberately routes around (the lease keeps it
    # reserved), so the reclaim would free a port this connect is not going to use
    # anyway -- converting a port safely occupied by our own dead forwarder into a
    # free one a stranger can bind while a hub still forwards a bearer token to it.
    # The orphan IS the ownership here, and a stronger one than a held socket. It
    # is reclaimed by the next connect once the lease lapses, and lingering is
    # already a tolerated outcome further down this function.
    if port in await asyncio.to_thread(self._registry.live_hop_leases):
        logger.info(
            "Leaving leaked %s forwarder pid %d for %s in place: port %d still "
            "carries a live chained-credential lease, and freeing it would expose "
            "that port rather than recover it",
            params.method,
            pid,
            inst.id,
            port,
        )
        return
    # The registry is agent-writable state, so its identity claims are
    # UNTRUSTED until authenticated: the record must carry the MAC this
    # gateway (and only this gateway — the key derives from the SEL trust
    # root, which sits on the sensitive-path deny list) computed when it
    # spawned the child. A record an agent wrote, edited, or re-pointed at
    # someone else's process fails verification and is refused before any
    # process attribute is even read. Key unreadable -> refuse (never
    # trust unsigned state).
    key = await asyncio.to_thread(_reclaim_identity_key)
    if key is None:
        return
    # Both the reconstruction and the comparison are fed agent-writable
    # text: a record can carry arbitrary strings (even lone surrogates
    # that refuse UTF-8 encoding), and compare_digest on str raises
    # TypeError for non-ASCII. Any malformed field IS a verification
    # failure, never a crash on the connect path.
    try:
        # The transport this connect would signal with: when it differs from
        # the spawn-time one, the signed message differs and the reclaim is
        # refused, so a group signal is never sent to a pid-scoped child.
        transport = "ssm" if params.forwards_over_ssm else "ssh"
        expected_sig = _forwarder_identity_sig(key, inst.id, pid, start, port, argv_sig, transport)
        sig_ok = hmac.compare_digest(sig.encode("utf-8"), expected_sig.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        sig_ok = False
    if not sig_ok:
        logger.warning(
            "Recorded forwarder identity for %s failed signature "
            "verification; refusing reclaim (registry edited outside the "
            "gateway?)",
            inst.id,
        )
        return
    # Defense in depth behind the MAC, from gateway-/kernel-owned facts: a
    # pid this manager is CURRENTLY supervising is never a leak candidate,
    # and a genuine hard-kill orphan has lost its spawning gateway — a
    # forwarder whose parent is still alive belongs to a running gateway
    # (this one or another), so it is refused no matter what the registry
    # says. Subreaper hosts read as non-orphaned and merely miss the
    # reclaim (fail closed, leak-not-mis-kill).
    live_pids = {t.pid for t in self._tunnels.values() if t.pid}
    if pid in live_pids:
        return
    orphaned, ppid = await asyncio.to_thread(_forwarder_orphan_state, pid, start)
    if not orphaned:
        logger.info(
            "Not reclaiming recorded %s forwarder pid %d for %s: its parent "
            "pid %d still reads as its live spawner (or the parent could not "
            "be confirmed gone); port %d is left to it",
            params.method,
            pid,
            inst.id,
            ppid,
            port,
        )
        return
    if await asyncio.to_thread(_is_port_free, port):
        return  # nothing holds the recorded port — nothing leaked to reclaim
    # A signed spawn-time fingerprint is the argv half on its own; only a
    # record without one rebuilds the command line to compare against.
    if argv_sig:
        expected: list[str] = []
    elif params.forwards_over_ssm:
        expected = _build_ssm_tunnel_argv(
            params.ssm_target,
            port,
            params.forward_remote_port(inst.remote_port),
            profile=params.aws_profile,
            region=params.aws_region,
        )
    else:
        # Through the same ssh_spawn_argv_env the spawn used, so the head is
        # the resolved /usr/bin/ssh the kernel recorded, not the bare "ssh"
        # the builder returns: an element-exact compare against the bare
        # head reads every genuine orphan as "not our child" and leaks it.
        # Off the loop, because resolving the head scans PATH.
        expected, _env = await asyncio.to_thread(
            ssh_spawn_argv_env,
            _build_ssh_tunnel_argv(
                params.ssh_host,
                port,
                # A chained forward targets its parent's loopback port, so the
                # identity compared here has to be the argv that was actually
                # spawned. Comparing the crew's own port would never match, and
                # a mismatch is read as "not our child" — the leaked forwarder
                # would be left holding the port forever.
                params.forward_remote_port(inst.remote_port),
                compression=self._ssh_compression,
            ),
        )
    outcome = await asyncio.to_thread(
        _verify_and_reclaim_forwarder,
        pid,
        start,
        expected,
        port,
        params.forwards_over_ssm,
        f"instance={inst.id} pid={pid} port={port} transport={params.method}",
        argv_sig,
    )
    if outcome == "reclaimed":
        logger.info(
            "Reclaimed leaked %s forwarder pid %d for %s (released port %d)",
            params.method,
            pid,
            inst.id,
            port,
        )
    elif outcome == "windows_group_unsupported":
        logger.info(
            "Not reclaiming leaked %s forwarder pid %d for %s: on Windows its "
            "plugin child cannot be ended through a start-time-pinned signal; "
            "port %d stays excluded from allocation",
            params.method,
            pid,
            inst.id,
            port,
        )
    elif outcome == "identity_mismatch":
        logger.info(
            "Recorded %s forwarder pid %d for %s no longer matches its "
            "recorded identity (recycled pid, or the rebuilt command line "
            "drifted); leaving it alone (#1972) — port %d stays excluded "
            "from allocation",
            params.method,
            pid,
            inst.id,
            port,
        )
    elif outcome == "recycled_during_grace":
        logger.warning(
            "Withheld SIGKILL for %s forwarder pid %d of %s: the pid "
            "stopped matching its recorded identity during the term grace "
            "(recycled); port %d stays excluded from allocation",
            params.method,
            pid,
            inst.id,
            port,
        )
    else:  # "not_gone"
        logger.warning(
            "Leaked %s forwarder pid %d for %s (or a group member holding "
            "port %d) did not exit within the reclaim grace; leaving it "
            "for the next connect (the port stays excluded from allocation)",
            params.method,
            pid,
            inst.id,
            port,
        )


# ── self-heal ─────────────────────────────────────────────────────────


def _hold_lent_port_now(self: SshTunnelManager, instance_id: str) -> None:
    """Take a just-freed lent port back, synchronously and without reading anything.

    Answers from :attr:`_lent_hops` alone, so there is no `await` between the port
    becoming free and the bind. Never raises: the caller is a monitor seam, and a
    failure to hold here is recorded as owed by the guard and retried on its next
    pass rather than propagated into the exit path.
    """
    try:
        tunnel = self._tunnels.get(instance_id)
        port = int(getattr(getattr(tunnel, "status", None), "local_port", 0) or 0)
        if port <= 0:
            return
        self._hold_lent_port_by_number(port)
    except Exception:
        logger.exception("Could not take back %s's lent hop port on exit", instance_id)


def _hold_lent_port_by_number(self: SshTunnelManager, port: int) -> None:
    """Hold *port* if the cache still names it as lent and the credential is alive.

    The port-keyed core of :meth:`_hold_lent_port_now`, extracted because the rebuild
    needs exactly this and already holds the number. ONE definition on purpose: the
    last defect in this mechanism was a second copy of a rule drifting from the first
    (the settle handed the cache a union and the guard a bare snapshot), so the re-arm
    gets one body and two callers rather than two bodies.

    No await, by construction -- it reads :attr:`_lent_hops` and binds.
    """
    if port <= 0:
        return
    until = self._lent_hops.get(port)
    if until is None or until <= time.time():
        return  # not lent, or the credential it protected has already died
    self._hop_guard.hold(port, until)
