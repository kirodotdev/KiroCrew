"""The composer typing lease: while one person types in a shared session, the
other collaborators' composers are read-only.

One lease per session, held by one *person* (a verified connection principal,
never a value a client claims). The first collaborator whose tab reports
``typing`` with a non-empty draft acquires it; every later ``typing`` report
from that person renews it. The lease ends when that person sends, clears the
draft, stays idle past the idle window (:data:`DEFAULT_IDLE_SECONDS`, the
``dashboard.shared_crew.typing_idle_seconds`` value), or closes every tab that
was typing. A person's own tabs never lock each other: the lease records which
of their connections are drafting, and a send from any of their tabs is the
holder's send.

The host enforces it: a send from anyone but the holder while the lease is
active is refused ``409 composer_locked`` naming the holder, and the sender
keeps the draft. Steering or stopping a running turn is never refused here.
First-submit-wins (``send_race``) stays the backstop for the moment a lease
ends.

A single operator's Crew never locks: the holder is compared as a person, so
the operator's own tabs, and the operator's machine-local bootstrap subjects
(:func:`~kiro_crew.shared_crew.crew_mode.operator_ids`), are one holder.

Nothing here is persisted: no draft text, no keystroke, only who holds the
lease, which of their connections are drafting, and when it was last renewed.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from kiro_crew.shared_crew.admission import ReasonCode
from kiro_crew.shared_crew.codec import SCHEMA_VERSION
from kiro_crew.shared_crew.collab import FRAME_TYPING_LEASE
from kiro_crew.shared_crew.contracts import Principal, PrincipalKind
from kiro_crew.shared_crew.crew_mode import operator_ids

logger = logging.getLogger(__name__)

#: The refusal code a non-holder's send carries while the lease is active.
CODE_COMPOSER_LOCKED = ReasonCode.COMPOSER_LOCKED.value

#: Seconds with no typing report before the lease ends on its own.
DEFAULT_IDLE_SECONDS = 10
#: The validated range of the idle window, inclusive.
IDLE_SECONDS_BOUNDS = (3, 60)
#: Sessions with a live lease at once; a new acquisition past it is refused.
MAX_LEASES = 4096

#: Why a lease ended, carried on the frame that announces it.
END_SENT = "sent"
END_CLEARED = "cleared"
END_IDLE = "idle"
END_DISCONNECTED = "disconnected"


def clamp_idle_seconds(raw: object) -> int:
    """*raw* as an idle window inside :data:`IDLE_SECONDS_BOUNDS`."""
    lo, hi = IDLE_SECONDS_BOUNDS
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DEFAULT_IDLE_SECONDS
    return max(lo, min(hi, int(raw)))


def same_person(a: Principal, b: Principal, *, owner_id: str = "") -> bool:
    """Whether *a* and *b* are one person for the lease."""
    if a.id == b.id:
        return True
    return b.id in operator_ids(a, owner_id) or a.id in operator_ids(b, owner_id)


def can_hold(principal: Principal) -> bool:
    """Only a verified person may hold the lease; Unknown and system never do."""
    return not principal.is_unknown and principal.kind not in (
        PrincipalKind.UNKNOWN,
        PrincipalKind.SYSTEM,
    )


@dataclass
class _Lease:
    holder: Principal
    #: Holder connection id -> monotonic time of its last typing report.
    drafting: dict[str, float] = field(default_factory=dict)
    #: Wall time of the latest renewal, for the browser-facing ``expires_at``.
    renewed_wall: float = 0.0

    def last_typed(self) -> float:
        return max(self.drafting.values()) if self.drafting else 0.0


@dataclass(frozen=True)
class LeaseChange:
    """Sessions whose lease changed, and why each ended (``""`` for a new holder)."""

    sessions: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> tuple[str, ...]:
        return tuple(sorted(self.sessions))


class TypingLeaseRegistry:
    """Per-session typing leases for one gateway. Thread-safe; never persisted."""

    def __init__(
        self,
        *,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        owner_id: str = "",
        max_leases: int = MAX_LEASES,
    ) -> None:
        self._idle = float(idle_seconds)
        self._clock = clock
        self._wall = wall_clock
        self._owner_id = owner_id
        self._max = max_leases
        self._lock = threading.Lock()
        self._leases: dict[str, _Lease] = {}

    @property
    def idle_seconds(self) -> float:
        return self._idle

    # -- reports ---------------------------------------------------------------

    def typed(self, session_key: str, conn_id: str, principal: Principal) -> LeaseChange:
        """A connection reported typing with a non-empty draft in *session_key*.

        Acquires a free lease, renews the holder's, and leaves someone else's
        untouched: a typing report never takes a held lease away.
        """
        if not session_key or not conn_id or not can_hold(principal):
            return LeaseChange()
        now = self._clock()
        with self._lock:
            ended = self._expire_locked(now)
            lease = self._leases.get(session_key)
            if lease is None:
                if len(self._leases) >= self._max:
                    return LeaseChange(ended)
                lease = _Lease(holder=principal)
                self._leases[session_key] = lease
                ended[session_key] = ""
            elif not same_person(lease.holder, principal, owner_id=self._owner_id):
                return LeaseChange(ended)
            lease.drafting[conn_id] = now
            lease.renewed_wall = self._wall()
            return LeaseChange(ended)

    def cleared(self, session_key: str, conn_id: str) -> LeaseChange:
        """A holder connection's draft went empty: it stops holding the lease."""
        return self._drop_conn(conn_id, END_CLEARED, only=session_key)

    def disconnected(self, conn_id: str) -> LeaseChange:
        """A socket closed: it stops holding any lease it held."""
        return self._drop_conn(conn_id, END_DISCONNECTED)

    def sent(self, session_key: str, principal: Principal) -> LeaseChange:
        """*principal*'s send into *session_key* was accepted: their lease ends."""
        now = self._clock()
        with self._lock:
            ended = self._expire_locked(now)
            lease = self._leases.get(session_key)
            if lease is not None and same_person(lease.holder, principal, owner_id=self._owner_id):
                del self._leases[session_key]
                ended[session_key] = END_SENT
            return LeaseChange(ended)

    def sweep(self) -> LeaseChange:
        """End every lease idle past the window."""
        with self._lock:
            return LeaseChange(self._expire_locked(self._clock()))

    # -- reads -----------------------------------------------------------------

    def holder(self, session_key: str) -> Principal | None:
        with self._lock:
            lease = self._live_locked(session_key)
            return lease.holder if lease is not None else None

    def blocks(self, session_key: str, principal: Principal) -> Principal | None:
        """The holder when the lease refuses a send by *principal*, else None."""
        holder = self.holder(session_key)
        if holder is None or same_person(holder, principal, owner_id=self._owner_id):
            return None
        return holder

    def next_expiry(self) -> float | None:
        """Monotonic time the earliest live lease goes idle, or None."""
        with self._lock:
            times = [lease.last_typed() + self._idle for lease in self._leases.values()]
            return min(times) if times else None

    def state(self, session_key: str, *, ended: str = "") -> dict[str, Any]:
        """The whole-value lease state: the GET body and the frame body."""
        with self._lock:
            lease = self._live_locked(session_key)
            if lease is None:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "session_key": session_key,
                    "holder": None,
                    "holder_ids": [],
                    "expires_at": None,
                    "idle_seconds": self._idle,
                    "ended": ended,
                }
            ids = sorted(operator_ids(lease.holder, self._owner_id) | {lease.holder.id})
            return {
                "schema_version": SCHEMA_VERSION,
                "session_key": session_key,
                "holder": lease.holder.to_public_dict(),
                # Every principal id that counts as the holder (the operator's
                # local subjects fold into one person), so a tab can tell
                # whether the lease is its own without a second read.
                "holder_ids": ids,
                "expires_at": lease.renewed_wall + self._idle,
                "idle_seconds": self._idle,
                "ended": "",
            }

    def lease_count(self) -> int:
        with self._lock:
            return len(self._leases)

    # -- internals -------------------------------------------------------------

    def _drop_conn(self, conn_id: str, reason: str, *, only: str = "") -> LeaseChange:
        now = self._clock()
        with self._lock:
            ended = self._expire_locked(now)
            for key, lease in list(self._leases.items()):
                if only and key != only:
                    continue
                if lease.drafting.pop(conn_id, None) is not None and not lease.drafting:
                    del self._leases[key]
                    ended[key] = reason
            return LeaseChange(ended)

    def _live_locked(self, session_key: str) -> _Lease | None:
        """The lease if it is not idle; a read never ends it (the sweep does, and broadcasts)."""
        lease = self._leases.get(session_key)
        now = self._clock()
        if lease is None or all(now - at >= self._idle for at in lease.drafting.values()):
            return None
        return lease

    def _expire_locked(self, now: float) -> dict[str, str]:
        ended: dict[str, str] = {}
        for key, lease in list(self._leases.items()):
            for cid, at in list(lease.drafting.items()):
                if now - at >= self._idle:
                    del lease.drafting[cid]
            if not lease.drafting:
                del self._leases[key]
                ended[key] = END_IDLE
        return ended


# -- the gateway's side ----------------------------------------------------------


def _configured_idle_seconds() -> int:
    try:
        from kiro_crew.config.sections_shared_crew import load_shared_crew_config

        return clamp_idle_seconds(load_shared_crew_config().typing_idle_seconds)
    except Exception:  # noqa: BLE001 -- an unreadable config keeps the default window
        logger.debug("typing idle window unreadable; using the default", exc_info=True)
        return DEFAULT_IDLE_SECONDS


def typing_lease_for(state: Any) -> TypingLeaseRegistry:
    """The gateway's lease registry, created on first use with the configured window."""
    registry = getattr(state, "_shared_crew_typing_lease", None)
    if not isinstance(registry, TypingLeaseRegistry):
        registry = TypingLeaseRegistry(
            idle_seconds=_configured_idle_seconds(),
            owner_id=str(getattr(state, "owner_id", "") or ""),
        )
        state._shared_crew_typing_lease = registry
    return registry


def broadcast_changes(state: Any, registry: TypingLeaseRegistry, change: LeaseChange) -> None:
    """Push one whole-value lease frame per changed session, then re-arm the idle timer."""
    for session_key, reason in sorted(change.sessions.items()):
        frame = {"type": FRAME_TYPING_LEASE, **registry.state(session_key, ended=reason)}
        try:
            state.broadcast_ws(FRAME_TYPING_LEASE, frame)
        except Exception:
            logger.debug("typing lease broadcast failed", exc_info=True)
    if change.sessions:
        arm_idle_timer(state, registry)


def arm_idle_timer(state: Any, registry: TypingLeaseRegistry) -> None:
    """Schedule one wake at the earliest lease's idle end, so others unlock on time.

    Without a running loop (a synchronous caller, a test) nothing is scheduled;
    the status tick's sweep still ends the lease.
    """
    import asyncio

    handle = getattr(state, "_shared_crew_typing_lease_timer", None)
    if handle is not None:
        try:
            handle.cancel()
        except Exception:
            pass
        state._shared_crew_typing_lease_timer = None
    at = registry.next_expiry()
    if at is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    delay = max(0.0, at - time.monotonic()) + 0.05

    def _wake() -> None:
        state._shared_crew_typing_lease_timer = None
        broadcast_changes(state, registry, registry.sweep())
        if registry.lease_count():
            arm_idle_timer(state, registry)

    state._shared_crew_typing_lease_timer = loop.call_later(delay, _wake)


def note_activity(
    state: Any,
    conn_id: str,
    principal: Principal,
    session_key: str,
    *,
    typing: bool,
    draft_empty: bool,
) -> None:
    """Apply one accepted activity report to the lease and push what changed."""
    registry = typing_lease_for(state)
    if typing and not draft_empty:
        change = registry.typed(session_key, conn_id, principal)
    elif draft_empty:
        change = registry.cleared(session_key, conn_id)
    else:
        # A plain step down (the tab's typing hold ran out) keeps the lease: the
        # draft is still there, and the idle window decides when it ends.
        change = registry.sweep()
    broadcast_changes(state, registry, change)


def release_connection(state: Any, conn_id: str) -> None:
    """A socket closed (a tab closed or lost its connection): drop its drafting."""
    registry = getattr(state, "_shared_crew_typing_lease", None)
    if isinstance(registry, TypingLeaseRegistry):
        broadcast_changes(state, registry, registry.disconnected(conn_id))


def sweep_and_broadcast(state: Any) -> None:
    registry = getattr(state, "_shared_crew_typing_lease", None)
    if isinstance(registry, TypingLeaseRegistry):
        broadcast_changes(state, registry, registry.sweep())


def note_sent(state: Any, session_key: str, principal: Principal) -> None:
    """The holder's send was accepted: release the lease for everyone at once."""
    registry = getattr(state, "_shared_crew_typing_lease", None)
    if isinstance(registry, TypingLeaseRegistry):
        broadcast_changes(state, registry, registry.sent(session_key, principal))


def locked_body(registry: TypingLeaseRegistry, session_key: str, holder: Principal) -> dict:
    """The 409 body of a send refused by someone else's lease. The draft stays."""
    lease = registry.state(session_key)
    return {
        "error": "someone else is typing",
        "code": CODE_COMPOSER_LOCKED,
        "session_key": session_key,
        "holder": holder.to_public_dict(),
        "lease": lease,
    }


def refuse_if_locked(state: Any, session_key: str, principal: Principal) -> dict | None:
    """The 409 body when the lease refuses *principal*'s send, else None."""
    registry = getattr(state, "_shared_crew_typing_lease", None)
    if not isinstance(registry, TypingLeaseRegistry):
        return None
    holder = registry.blocks(session_key, principal)
    if holder is None:
        return None
    return locked_body(registry, session_key, holder)


__all__ = [
    "CODE_COMPOSER_LOCKED",
    "DEFAULT_IDLE_SECONDS",
    "END_CLEARED",
    "END_DISCONNECTED",
    "END_IDLE",
    "END_SENT",
    "FRAME_TYPING_LEASE",
    "IDLE_SECONDS_BOUNDS",
    "LeaseChange",
    "TypingLeaseRegistry",
    "arm_idle_timer",
    "broadcast_changes",
    "can_hold",
    "clamp_idle_seconds",
    "locked_body",
    "note_activity",
    "note_sent",
    "refuse_if_locked",
    "release_connection",
    "same_person",
    "sweep_and_broadcast",
    "typing_lease_for",
]
