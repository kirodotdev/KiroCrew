"""Presence and typing: transient, per-connection telemetry folded per person.

A connection reports one :class:`~kiro_crew.shared_crew.collab.Activity` for one
session. The principal is the one the server verified for that connection; the
report itself carries no identity and no text, so a draft or a keystroke has no
field to ride in. Every tab of one principal in one session folds into a single
:class:`~kiro_crew.shared_crew.collab.PresenceEntry`.

Nothing here is persisted. An entry lives :data:`PRESENCE_TTL_SECONDS` past its
last accepted report and goes with its socket on disconnect. Presence never
grants access, holds capacity or frees a runtime slot.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from kiro_crew.shared_crew import typing_lease
from kiro_crew.shared_crew.collab import (
    ACTIVITY_MIN_INTERVAL_SECONDS,
    PRESENCE_TTL_SECONDS,
    Activity,
    PresenceEntry,
    presence_frame,
)
from kiro_crew.shared_crew.contracts import Principal

#: Upper bound on tracked connections. A report past it is refused, never
#: admitted by evicting someone else's live entry.
MAX_TRACKED_CONNECTIONS = 1024
#: Longest session key a report may name; anything longer is malformed.
MAX_SESSION_KEY_LEN = 256

_ACTIVITY_RANK = {Activity.IDLE: 0, Activity.VIEWING: 1, Activity.TYPING: 2}

#: Report refusal reasons.
REFUSED_RATE = "rate_limited"
REFUSED_MALFORMED = "malformed"
REFUSED_FULL = "registry_full"


@dataclass
class _Connection:
    principal: Principal
    session_key: str
    activity: Activity
    #: Monotonic time of the last accepted report (rate limit and expiry).
    refreshed: float
    #: Wall time of the same report, for the browser-facing ``expires_at``.
    refreshed_wall: float


def _ends_typing(prior: _Connection, session_key: str, activity: Activity) -> bool:
    """Whether a report only ends this connection's typing in the same session.

    Such a report is exempt from the per-connection spacing, so a send clears
    "typing" for collaborators at once. It stays bounded: it can only follow an
    accepted ``typing`` report, which is itself spaced, so a connection still
    lands at most two frames per interval.
    """
    return (
        prior.activity is Activity.TYPING
        and prior.session_key == session_key
        and activity is not Activity.TYPING
    )


@dataclass(frozen=True)
class ReportResult:
    accepted: bool
    #: Sessions whose folded presence changed and need a new frame.
    changed: tuple[str, ...] = ()
    reason: str = ""


def parse_draft_empty(frame: object) -> bool:
    """Whether a client frame says this tab's draft is empty (``"draft": "empty"``).

    A content-free flag: the frame never carries the draft, only whether there
    is one. Any other value reads as "not said".
    """
    return isinstance(frame, dict) and frame.get("draft") == "empty"


def parse_activity_frame(frame: object) -> tuple[str, Activity] | None:
    """Read ``(session_key, activity)`` from a client frame, or ``None``.

    Only those two fields are read. Any other key, including a principal or a
    text field, is ignored and never reaches the registry.
    """
    if not isinstance(frame, dict):
        return None
    session_key = frame.get("session_key")
    raw_activity = frame.get("activity")
    if not isinstance(session_key, str) or not session_key:
        return None
    if len(session_key) > MAX_SESSION_KEY_LEN or not isinstance(raw_activity, str):
        return None
    try:
        return session_key, Activity(raw_activity)
    except ValueError:
        return None


class PresenceRegistry:
    """In-memory presence for one gateway. Thread-safe; never persisted."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        ttl_seconds: float = PRESENCE_TTL_SECONDS,
        min_interval_seconds: float = ACTIVITY_MIN_INTERVAL_SECONDS,
        max_connections: int = MAX_TRACKED_CONNECTIONS,
    ) -> None:
        self._clock = clock
        self._wall = wall_clock
        self._ttl = ttl_seconds
        self._min_interval = min_interval_seconds
        self._max = max_connections
        self._lock = threading.Lock()
        self._conns: dict[str, _Connection] = {}

    def report(
        self, conn_id: str, principal: Principal, session_key: str, activity: Activity
    ) -> ReportResult:
        """Record one connection's activity in one session.

        ``principal`` is the connection's verified actor, supplied by the
        socket handler, never by the frame.
        """
        if not conn_id or not session_key or len(session_key) > MAX_SESSION_KEY_LEN:
            return ReportResult(False, reason=REFUSED_MALFORMED)
        now = self._clock()
        with self._lock:
            changed = set(self._expire_locked(now))
            prior = self._conns.get(conn_id)
            if (
                prior is not None
                and now - prior.refreshed < self._min_interval
                and not _ends_typing(prior, session_key, activity)
            ):
                return ReportResult(False, tuple(sorted(changed)), REFUSED_RATE)
            if prior is None and len(self._conns) >= self._max:
                return ReportResult(False, tuple(sorted(changed)), REFUSED_FULL)
            before = self._fold_locked(session_key)
            old_before = None
            if prior is not None and prior.session_key != session_key:
                old_before = self._fold_locked(prior.session_key)
            self._conns[conn_id] = _Connection(
                principal=principal,
                session_key=session_key,
                activity=activity,
                refreshed=now,
                refreshed_wall=self._wall(),
            )
            if self._signature(self._fold_locked(session_key)) != self._signature(before):
                changed.add(session_key)
            if prior is not None and old_before is not None:
                if self._signature(self._fold_locked(prior.session_key)) != self._signature(
                    old_before
                ):
                    changed.add(prior.session_key)
            return ReportResult(True, tuple(sorted(changed)))

    def disconnect(self, conn_id: str) -> tuple[str, ...]:
        """Drop a closed connection; return the sessions whose presence changed."""
        with self._lock:
            changed = set(self._expire_locked(self._clock()))
            gone = self._conns.pop(conn_id, None)
            if gone is not None:
                changed.add(gone.session_key)
            return tuple(sorted(changed))

    def sweep(self) -> tuple[str, ...]:
        """Expire stale connections; return the sessions whose presence changed."""
        with self._lock:
            return tuple(sorted(set(self._expire_locked(self._clock()))))

    def entries(self, session_key: str) -> list[PresenceEntry]:
        """The folded, live presence of one session, one entry per principal."""
        with self._lock:
            self._expire_locked(self._clock())
            return self._fold_locked(session_key)

    def _expire_locked(self, now: float) -> list[str]:
        stale = [cid for cid, c in self._conns.items() if now - c.refreshed >= self._ttl]
        sessions = []
        for cid in stale:
            sessions.append(self._conns.pop(cid).session_key)
        return sessions

    def _fold_locked(self, session_key: str) -> list[PresenceEntry]:
        by_principal: dict[str, list[_Connection]] = {}
        for conn in self._conns.values():
            if conn.session_key == session_key:
                by_principal.setdefault(conn.principal.id, []).append(conn)
        entries = []
        for conns in by_principal.values():
            latest = max(conns, key=lambda c: c.refreshed)
            activity = max((c.activity for c in conns), key=_ACTIVITY_RANK.__getitem__)
            entries.append(
                PresenceEntry(
                    principal=latest.principal,
                    session_key=session_key,
                    activity=activity,
                    connections=len(conns),
                    expires_at=latest.refreshed_wall + self._ttl,
                )
            )
        entries.sort(key=lambda e: (e.principal.display.name.casefold(), e.principal.id))
        return entries

    @staticmethod
    def _signature(entries: list[PresenceEntry]) -> list[tuple[Any, ...]]:
        return [(e.principal.id, e.activity, e.connections) for e in entries]


def presence_for(state: Any) -> PresenceRegistry:
    """The gateway's presence registry, created on first use."""
    registry = getattr(state, "_shared_crew_presence", None)
    if not isinstance(registry, PresenceRegistry):
        registry = PresenceRegistry()
        state._shared_crew_presence = registry
    return registry


def _broadcast(state: Any, registry: PresenceRegistry, sessions: tuple[str, ...]) -> None:
    for session_key in sessions:
        frame = presence_frame(session_key, registry.entries(session_key))
        try:
            state.broadcast_ws(frame["type"], frame)
        except Exception:
            log = getattr(state, "_log", None)
            if log is not None:
                log.debug("presence broadcast failed", exc_info=True)


def handle_activity_frame(
    state: Any, conn_id: str, principal: Principal, frame: object, *, dashboard_user: bool
) -> ReportResult:
    """Apply one ``shared_crew.activity`` frame from a socket.

    Only a dashboard user's socket reports presence, and only for a session the
    gateway has open, so an app token or a made-up key cannot grow the registry.
    """
    if not dashboard_user:
        return ReportResult(False, reason=REFUSED_MALFORMED)
    parsed = parse_activity_frame(frame)
    if parsed is None:
        return ReportResult(False, reason=REFUSED_MALFORMED)
    session_key, activity = parsed
    slots = getattr(state, "_slots", None)
    if not isinstance(slots, dict) or session_key not in slots:
        return ReportResult(False, reason=REFUSED_MALFORMED)
    registry = presence_for(state)
    result = registry.report(conn_id, principal, session_key, activity)
    _broadcast(state, registry, result.changed)
    draft_empty = parse_draft_empty(frame)
    # The typing lease rides the same report. A typing report renews it only
    # when the presence registry accepted it (so the lease keeps the presence
    # rate limit); an empty-draft report only ever releases, so it applies even
    # inside the spacing.
    if result.accepted or draft_empty:
        try:
            typing_lease.note_activity(
                state,
                conn_id,
                principal,
                session_key,
                typing=result.accepted and activity is Activity.TYPING,
                draft_empty=draft_empty,
            )
        except Exception:
            log = getattr(state, "_log", None)
            if log is not None:
                log.debug("typing lease update failed", exc_info=True)
    return result


def release_connection(state: Any, conn_id: str) -> None:
    """Drop a closed socket's presence and lease and push the sessions it left."""
    registry = presence_for(state)
    _broadcast(state, registry, registry.disconnect(conn_id))
    typing_lease.release_connection(state, conn_id)


def sweep_and_broadcast(state: Any) -> None:
    """Expire stale presence and idle leases and push the sessions they changed."""
    registry = presence_for(state)
    _broadcast(state, registry, registry.sweep())
    typing_lease.sweep_and_broadcast(state)
