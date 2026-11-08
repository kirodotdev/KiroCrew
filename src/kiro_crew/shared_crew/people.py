"""Observed collaborators: who this gateway has seen, not who may connect.

Tailscale decides access, and this Crew has no authoritative membership
directory, so the People view can only report what the gateway observed or the
local Tailscale daemon lists. Three sources feed it, and each row says which:

* ``observed``: principals whose authorized requests this process has seen
  since it started (in memory, bounded, reset on restart);
* ``session_creator``: principals recorded as the creator of a persisted
  session, which survives a restart. A legacy session counts to the owner;
* ``tailscale_share``: a user who owns a node the local Tailscale daemon lists
  and who is not the owner (:mod:`kiro_crew.shared_crew.tailscale_sharees`). They
  appear before they connect, with no first or last sighting and no devices.

Rows merge in that order: a person already observed or recorded keeps that row.
Every row says whether it is the owner (``is_owner``), and the owner has a row
whenever the owner is the one viewing.

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
from typing import TYPE_CHECKING, Any

from kiro_crew.shared_crew.codec import SCHEMA_VERSION
from kiro_crew.shared_crew.contracts import (
    Principal,
    PrincipalKind,
    principal_or_unknown,
)

if TYPE_CHECKING:
    from kiro_crew.shared_crew.tailscale_sharees import Sharee

#: Distinct principals remembered. A flood of distinct tagged nodes cannot grow
#: memory past this; the least recently seen principal is dropped first.
MAX_OBSERVED = 512
#: Distinct devices counted per principal.
MAX_DEVICES = 16

SOURCE_OBSERVED = "observed"
SOURCE_SESSION_CREATOR = "session_creator"
SOURCE_TAILSCALE_SHARE = "tailscale_share"


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

    def now(self) -> float:
        return self._clock()

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


def _fold_local_owner(rows: list[dict[str, Any]], local_ids: frozenset[str]) -> None:
    """Fold the owner's local-token rows into one, in place.

    The owner holds several local token subjects (a bootstrap token, a
    configured owner id); they are one person at this machine, so they are one
    row. A row is matched by its principal id, because a session creator read
    back from public attribution carries no provenance. The viewer's own row
    leads when it is among them.
    """
    local = [r for r in rows if r["is_owner"] and r["principal"].get("id") in local_ids]
    if len(local) < 2:
        return
    local.sort(key=lambda r: not r["is_viewer"])
    head = local[0]
    for extra in local[1:]:
        head["sources"] = list(dict.fromkeys([*head["sources"], *extra["sources"]]))
        firsts = [t for t in (head["first_seen_at"], extra["first_seen_at"]) if t is not None]
        lasts = [t for t in (head["last_seen_at"], extra["last_seen_at"]) if t is not None]
        head["first_seen_at"] = min(firsts) if firsts else None
        head["last_seen_at"] = max(lasts) if lasts else None
        head["devices"] += extra["devices"]
        head["sessions_created"] += extra["sessions_created"]
        head["is_viewer"] = head["is_viewer"] or extra["is_viewer"]
        if head["usage"] is not None and extra["usage"] is not None:
            head["usage"] = {
                "active": head["usage"]["active"] + extra["usage"]["active"],
                "queued": head["usage"]["queued"] + extra["usage"]["queued"],
            }
        rows.remove(extra)


def people_view(
    viewer: Principal,
    session_rows: Iterable[Mapping[str, Any]],
    *,
    reg: ObservedPeople | None = None,
    owner_id: str = "",
    sharees: Iterable[Sharee] = (),
) -> dict[str, Any]:
    """The ``GET /api/shared-crew/people`` body.

    ``directory`` is always unavailable here: no authoritative assigned-user
    directory exists in this build, and observations are not one. *sharees*
    are the people the local Tailscale daemon lists besides the owner; one
    whose principal is already a row adds none.
    """
    from kiro_crew.shared_crew.owner import local_owner_principal_ids, owner_principal_ids

    reg = reg or _REGISTRY
    creators = _creators(session_rows)
    owner_ids = owner_principal_ids(owner_id)
    viewer_is_owner = not viewer.is_unknown and viewer.id in owner_ids
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

    def _row(
        principal: Principal,
        sources: list[str],
        *,
        first_seen: float | None,
        last_seen: float | None,
        devices: int,
        sessions_created: int,
    ) -> dict[str, Any]:
        pid = principal.id
        return {
            "principal": principal.to_public_dict(),
            "sources": sources,
            "provenance": principal.provenance.value,
            "first_seen_at": first_seen,
            "last_seen_at": last_seen,
            "devices": devices,
            "sessions_created": sessions_created,
            "is_viewer": pid == viewer.id and not viewer.is_unknown,
            "is_owner": pid in owner_ids,
            "usage": _usage_of(pid),
        }

    for obs in reg.snapshot():
        pid = obs.principal.id
        seen.add(pid)
        rows.append(
            _row(
                obs.principal,
                [SOURCE_OBSERVED] + ([SOURCE_SESSION_CREATOR] if pid in creators else []),
                first_seen=obs.first_seen,
                last_seen=obs.last_seen,
                devices=len(obs.devices),
                sessions_created=creators[pid][1] if pid in creators else 0,
            )
        )
    for pid, (creator, count) in creators.items():
        if pid in seen:
            continue
        seen.add(pid)
        rows.append(
            _row(
                creator,
                [SOURCE_SESSION_CREATOR],
                first_seen=None,
                last_seen=None,
                devices=0,
                sessions_created=count,
            )
        )
    # The daemon names everyone this machine can see, ordinary tailnet members
    # included: only the owner, who shared it, is shown those who never came in.
    for sharee in sharees if viewer_is_owner else ():
        pid = sharee.principal.id
        if pid in seen or pid in owner_ids:
            continue
        seen.add(pid)
        row = _row(
            sharee.principal,
            [SOURCE_TAILSCALE_SHARE],
            first_seen=None,
            last_seen=None,
            devices=0,
            sessions_created=0,
        )
        row["tailscale_user_id"] = sharee.user.user_id
        if sharee.user.profile_pic_url:
            row["member"] = {
                "alias": "",
                "avatar_id": "",
                "profile_pic_url": sharee.user.profile_pic_url,
            }
        rows.append(row)
    if viewer_is_owner and not any(r["is_owner"] for r in rows):
        now = reg.now()
        rows.insert(
            0,
            _row(
                viewer,
                [SOURCE_OBSERVED],
                first_seen=now,
                last_seen=now,
                devices=0,
                sessions_created=0,
            ),
        )
    _fold_local_owner(rows, local_owner_principal_ids(owner_id))
    return {
        "schema_version": SCHEMA_VERSION,
        "viewer": viewer.to_public_dict(),
        "directory": {"state": "unavailable"},
        "observed_since": reg.started_at,
        "usage_available": usage is not None,
        "people": rows,
    }
