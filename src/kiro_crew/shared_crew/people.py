"""Observed collaborators: who this gateway has seen, not who may connect.

Tailscale decides access, and this Crew has no authoritative membership
directory, so the People view can only report what the gateway observed. Two
sources feed it, and each row says which:

* ``observed``: principals whose authorized requests this process has seen
  since it started (in memory, bounded, reset on restart);
* ``session_creator``: principals recorded as the creator of a persisted
  session, which survives a restart.

The registry never stores a token, an address or a node name. It keeps a
bounded set of device digests only to count devices.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.shared_crew.codec import SCHEMA_VERSION
from kiro_crew.shared_crew.contracts import Principal, PrincipalKind, principal_or_unknown

#: Distinct principals remembered. A flood of distinct tagged nodes cannot grow
#: memory past this; the least recently seen principal is dropped first.
MAX_OBSERVED = 512
#: Distinct devices counted per principal.
MAX_DEVICES = 16

SOURCE_OBSERVED = "observed"
SOURCE_SESSION_CREATOR = "session_creator"


@dataclass
class _Observation:
    principal: Principal
    first_seen: float
    last_seen: float
    devices: set[str] = field(default_factory=set)


class ObservedPeople:
    """A bounded, thread-safe record of principals seen on authorized requests."""

    def __init__(self, *, max_entries: int = MAX_OBSERVED, clock: Callable[[], float] = time.time):
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _Observation] = OrderedDict()
        self.started_at = clock()

    def observe(self, principal: Principal, device: str = "") -> None:
        """Record one sighting. Unknown and system principals are not people."""
        if principal.kind in (PrincipalKind.UNKNOWN, PrincipalKind.SYSTEM):
            return
        now = self._clock()
        digest = hashlib.sha256(device.encode()).hexdigest()[:16] if device else ""
        with self._lock:
            entry = self._entries.get(principal.id)
            if entry is None:
                entry = _Observation(principal=principal, first_seen=now, last_seen=now)
                self._entries[principal.id] = entry
            else:
                # The newest display snapshot wins; the id never changes.
                entry.principal = principal
                entry.last_seen = now
                self._entries.move_to_end(principal.id)
            if digest and len(entry.devices) < MAX_DEVICES:
                entry.devices.add(digest)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def snapshot(self) -> list[_Observation]:
        with self._lock:
            return [
                _Observation(o.principal, o.first_seen, o.last_seen, set(o.devices))
                for o in reversed(self._entries.values())
            ]


_REGISTRY = ObservedPeople()

#: Optional usage provider ``principal id -> {"active": int, "queued": int}``,
#: registered by the admission layer when it is wired. ``None`` reports usage
#: as unavailable rather than as zero.
UsageSource = Callable[[], Mapping[str, Mapping[str, int]]]
_usage_source: UsageSource | None = None


def observe(principal: Principal, device: str = "") -> None:
    _REGISTRY.observe(principal, device)


def registry() -> ObservedPeople:
    return _REGISTRY


def reset_for_tests(clock: Callable[[], float] = time.time) -> ObservedPeople:
    global _REGISTRY, _usage_source
    _REGISTRY = ObservedPeople(clock=clock)
    _usage_source = None
    return _REGISTRY


def register_usage_source(source: UsageSource | None) -> None:
    """Wire the per-principal active/queued usage reader (or clear it)."""
    global _usage_source
    _usage_source = source


def _creators(session_rows: Iterable[Mapping[str, Any]]) -> dict[str, tuple[Principal, int]]:
    out: dict[str, tuple[Principal, int]] = {}
    for row in session_rows:
        attribution = row.get("attribution")
        creator_raw = attribution.get("creator") if isinstance(attribution, Mapping) else None
        creator = principal_or_unknown(creator_raw)
        if creator.kind in (PrincipalKind.UNKNOWN, PrincipalKind.SYSTEM):
            continue
        prior = out.get(creator.id)
        out[creator.id] = (creator if prior is None else prior[0], 1 + (prior[1] if prior else 0))
    return out


def people_view(
    viewer: Principal,
    session_rows: Iterable[Mapping[str, Any]],
    *,
    reg: ObservedPeople | None = None,
) -> dict[str, Any]:
    """The ``GET /api/shared-crew/people`` body.

    ``directory`` is always unavailable here: no authoritative assigned-user
    directory exists in this build, and observations are not one.
    """
    reg = reg or _REGISTRY
    creators = _creators(session_rows)
    usage: Mapping[str, Mapping[str, int]] | None = None
    if _usage_source is not None:
        try:
            usage = _usage_source()
        except Exception:
            usage = None
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _usage_of(pid: str) -> dict[str, int] | None:
        if usage is None:
            return None
        raw = usage.get(pid) or {}
        return {"active": int(raw.get("active", 0)), "queued": int(raw.get("queued", 0))}

    for obs in reg.snapshot():
        pid = obs.principal.id
        seen.add(pid)
        rows.append(
            {
                "principal": obs.principal.to_public_dict(),
                "sources": [SOURCE_OBSERVED]
                + ([SOURCE_SESSION_CREATOR] if pid in creators else []),
                "provenance": obs.principal.provenance.value,
                "first_seen_at": obs.first_seen,
                "last_seen_at": obs.last_seen,
                "devices": len(obs.devices),
                "sessions_created": creators[pid][1] if pid in creators else 0,
                "is_viewer": pid == viewer.id and not viewer.is_unknown,
                "usage": _usage_of(pid),
            }
        )
    for pid, (creator, count) in creators.items():
        if pid in seen:
            continue
        rows.append(
            {
                "principal": creator.to_public_dict(),
                "sources": [SOURCE_SESSION_CREATOR],
                "provenance": creator.provenance.value,
                "first_seen_at": None,
                "last_seen_at": None,
                "devices": 0,
                "sessions_created": count,
                "is_viewer": pid == viewer.id and not viewer.is_unknown,
                "usage": _usage_of(pid),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "viewer": viewer.to_public_dict(),
        "directory": {"state": "unavailable"},
        "observed_since": reg.started_at,
        "usage_available": usage is not None,
        "people": rows,
    }
