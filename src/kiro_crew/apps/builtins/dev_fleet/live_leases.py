"""The gateway-held registries of Dev Fleet removal leases and pod starts.

Storage of the removal leases (``_REMOVAL_LEASES``) and their TTL, grace and
outstanding-lease limits, the sweep, renewal and release of a lease, whether any
removal is in progress, the pod-start registry a pod start reserves its checkout in,
and the loss events and confirmations a removal checks. Acquiring a lease arbitrates
against the cutover lock and latch, so ``acquire_removal_lease`` stays in ``live``
beside them. Reached through ``live``, its facade.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

# --- cutover / removal exclusion across the two processes ---
#: ``live._MAKE_LIVE_LOCK`` is an asyncio lock and so serialises only within one
#: process. Since the cutover runs in the gateway and a worktree removal runs in the
#: backend, the TOCTOU a single lock closes ("stage this worktree between the removal's
#: live-check and its ``git worktree remove``", and "restart the gateway — tree-killing
#: the backend — mid-removal") needs exclusion both processes see.
#:
#: NOT a lock file. A file in the crew data home is replaceable by any same-uid process
#: in the backend's namespace (its build children included): unlink-and-recreate the leaf
#: while one side holds the old inode and the other side locks the new one, and the two
#: stop excluding each other. The state lives in the GATEWAY's memory instead — the
#: process that already owns the cutover — as REMOVAL LEASES.
#:
#: A lease is a CAPABILITY, not a path claim: acquisition returns an unguessable token,
#: and renewal and release must present it. The Dev Fleet app credential the backend
#: uses to reach the gateway is readable by the build children in its namespace, so a
#: path-only release would let any of them cancel a removal's lease from under it; the
#: token is minted by the gateway and travels only in the lease reply, so a child that
#: never saw it cannot forge a release. It can still ACQUIRE leases of its own — and so
#: delay a cutover or a restart, which the same child could already cause by running
#: ``git worktree remove`` itself — so the exposure it keeps is delay, not escalation.
#:
#: A lease is short-lived and RENEWED by its holder for as long as the removal runs
#: (``removal_lease`` heartbeats at a third of the TTL, through ``_GIT_MUTATION_LOCK``
#: queueing and the ``git worktree remove`` itself); a forgotten lease therefore expires
#: on its own, while a live one never lapses mid-removal. The gateway refuses a NEW lease
#: while a cutover is in flight or a pod start on its worktree is running in the gateway
#: (:func:`pod_start_reservation`), and ``_make_live`` / ``_restart_gateway`` refuse ``busy``
#: while any lease is live.
_REMOVAL_LEASE_TTL_SECS = 30.0
_REMOVAL_LEASE_RENEW_SECS = _REMOVAL_LEASE_TTL_SECS / 3
#: How long a LAPSED lease (expired without release) keeps blocking cutovers and restarts.
#: A lease lapses only when its holder stopped heartbeating — the backend died, or the
#: gateway forgot it and refused renewal — and the holder may be inside its
#: uninterruptible ``git worktree remove`` (``worktree_ops``, 60 s timeout) with no way
#: to be told. Renewal is refused the moment the TTL passes (so a live holder learns the
#: lease is lost and never STARTS a new mutation), but the barrier itself outlives the
#: TTL by the mutation's timeout plus margin, so a mutation already under way cannot be
#: overlapped by a cutover or a restart. Only an explicit release ends it early.
_REMOVAL_LEASE_GRACE_SECS = 90.0
#: Ceiling on leases the gateway holds at once (live or inside their grace barrier).
#: A legitimate backend holds one per in-flight removal — a parallel prune runs at most
#: ``worktree_ops._PRUNE_CONCURRENCY`` (4) — so this is an order of magnitude of headroom,
#: not a budget. It exists because the app token that acquires leases is readable by
#: every build child in the backend's namespace: without a cap, a child looping on the
#: acquire route could grow this table without bound (each entry outlives its TTL by the
#: grace barrier) and make every sweep quadratic. At the cap the gateway refuses, which
#: is a normal ``busy`` answer, never an error.
_REMOVAL_LEASE_MAX_OUTSTANDING = 32


@dataclass
class _RemovalLease:
    path: str
    token: str
    expires_at: float

    def blocks_until(self) -> float:
        return self.expires_at + _REMOVAL_LEASE_GRACE_SECS


#: token -> lease. Gateway-process state.
_REMOVAL_LEASES: dict[str, _RemovalLease] = {}


def _sweep_removal_leases(now: float | None = None) -> float:
    """Drop leases whose grace barrier has passed; return *now* (gateway-local)."""
    now = time.monotonic() if now is None else now
    for token, lease in list(_REMOVAL_LEASES.items()):
        if lease.blocks_until() <= now:
            del _REMOVAL_LEASES[token]
    return now


def _renewable_removal_lease(token: str) -> _RemovalLease | None:
    """The lease *token* names, if it is still within its TTL (renewable)."""
    now = _sweep_removal_leases()
    lease = _REMOVAL_LEASES.get(token)
    if lease is None or lease.expires_at <= now:
        return None
    return lease


def renew_removal_lease(token: str) -> bool:
    """Gateway-local: extend the lease *token* names. ``False`` once its TTL has passed —
    even inside the grace barrier, so a holder that fell behind learns the lease is lost
    rather than silently resuming on a barrier that is about to end."""
    lease = _renewable_removal_lease(token)
    if lease is None:
        return False
    lease.expires_at = time.monotonic() + _REMOVAL_LEASE_TTL_SECS
    return True


def release_removal_lease(token: str) -> None:
    """Gateway-local: end the lease *token* names (idempotent; a wrong token is a no-op)."""
    _REMOVAL_LEASES.pop(token, None)


def removal_in_progress() -> bool:
    """Gateway-local: whether any removal is (or may still be) under way — a live lease
    OR a lapsed one inside its grace barrier.

    Any worktree, deliberately: a cutover restarts the whole gateway and a restart
    tree-kills the backend, so a removal of ANY worktree must exclude them both.
    """
    _sweep_removal_leases()
    return bool(_REMOVAL_LEASES)


#: Normalized worktree path -> pod starts running on it in THIS process. Gateway state,
#: beside the leases and for the same reason: an agent's pod start (``agent_pod_api``)
#: runs in the gateway while every removal runs in the backend, so the two see each
#: other only through state the gateway holds. An entry lives only while its start runs,
#: and a start reserves only a worktree its caller has already resolved, so this holds
#: at most one entry per existing worktree.
_POD_STARTS: dict[str, int] = {}


def _worktree_key(path: str) -> str:
    """One spelling of a checkout path for the lease and pod-start registries."""
    return os.path.normcase(os.path.normpath(path))


def _lease_names(path: str) -> bool:
    """Gateway-local: whether a removal lease on *path* is live or inside its grace barrier."""
    _sweep_removal_leases()
    key = _worktree_key(path)
    return any(_worktree_key(lease.path) == key for lease in _REMOVAL_LEASES.values())


#: worktree path -> "lost" event for the lease this process currently holds on it.
_LEASE_LOSS_EVENTS: dict[str, asyncio.Event] = {}
#: worktree path -> "confirm" (explicit renewal) for the lease this process holds on it.
_LEASE_CONFIRMS: dict[str, Callable[[], Awaitable[bool]]] = {}


def removal_lease_lost(path: str) -> bool:
    """Whether a lease this process holds on *path* has been lost (renewal refused, or
    no renewal succeeded for a whole TTL).

    Checked by the removal at its mutation boundaries: a lost lease means a cutover or
    restart is not excluded any more, so the mutation must not start.
    """
    event = _LEASE_LOSS_EVENTS.get(path)
    return event is not None and event.is_set()


async def confirm_removal_lease(path: str) -> bool:
    """Prove, by renewing it NOW, that this process's lease on *path* still holds.

    The removal passes this as ``_run_cmd``'s ``pre_spawn`` gate: it runs after the
    sandbox preparation hop and immediately before ``git worktree remove`` is spawned,
    so a successful renewal here means the gateway excludes cutovers from this worktree
    for at least a TTL — and, should this holder then die mid-mutation, for the grace
    barrier beyond that. ``False`` when no lease is held, it was lost, or the gateway
    cannot be reached to renew it.
    """
    confirm = _LEASE_CONFIRMS.get(path)
    if confirm is None:
        return False
    return await confirm()


@dataclass(frozen=True)
class LeaseOutcome:
    """What :func:`removal_lease` yields: granted, or refused with its cause.

    ``refusal`` is ``"busy"`` when the gateway answered and declined (a cutover is
    staged or in flight; removal leases do not exclude one another) and ``"unavailable"``
    when the gateway could not be reached at all. The two have opposite remedies —
    wait, versus check the gateway — so callers word them separately. Truthy iff
    granted, so a caller that only needs the flag can test it directly.
    """

    granted: bool
    refusal: str | None = None

    def __bool__(self) -> bool:
        return self.granted
