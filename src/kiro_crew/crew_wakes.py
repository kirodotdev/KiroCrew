"""Plane C wake queue for supervised ``kiro-cli:<sessionId>`` owners.

Phase 4 reverses the data-plane direction of Phase 3. Where a Plane A verb is
the CLI asking and the sidecar answering, a wake is the sidecar deciding that a
turn must happen in the live CLI session (a monitor interval elapsed, an
agent-message cron fired) and handing that decision to the CLI to run in its own
KAS session. The mechanism is one FIFO queue per owner: producers ENQUEUE a wake
instead of injecting a prompt into a gateway slot, KAS long-polls the queue and
turns each wake into a real prompt, and acks once the turn is SUBMITTED (not
completed) so an un-acked wake survives a CLI restart and is redelivered.

Why a dedicated module rather than reusing the autonudge/cron delivery paths:
those paths inject into a gateway-owned session, which is exactly the behaviour
a supervised owner must NOT get — its turns are the CLI's own. Keeping the queue
here, transport-agnostic, lets the two producers (autonudge fire, cron
scheduler) divert to it without either learning the other's internals, and lets
the HTTP routes stay thin.

Ownership is the supervised session key (``kiro-cli:<sessionId>``); a wake is
only ever enqueued for such an owner (the producers gate on the prefix), so this
module never has to reason about non-supervised delivery.

Persistence: one append-mostly JSONL file per owner under
``<home>/crew-wakes/<owner>.jsonl``. The file is rewritten on ack and on expiry
sweep (small files: a queue holds at most a handful of un-acked wakes), and read
back at construction so a gateway restart redelivers everything not yet acked.
The owner is filename-sanitised because a session id is caller-influenced.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

#: A wake older than this (by ``created_at``) is dropped as ``expired`` on the
#: next sweep or long-poll, rather than delivered. One hour matches the plan's
#: default and the spawn-continuation retention window: a wake the CLI has not
#: pumped in an hour is almost certainly for a session that is gone.
DEFAULT_MAX_AGE_SECS = 3600

#: Long-poll ceiling. A caller may ask for less; a caller asking for more is
#: clamped so a single request can never hold a server task open indefinitely.
#: The route re-polls, so the clamp costs only an extra round trip on a quiet
#: queue. 30 s mirrors the ``wait <= 30`` contract in the plan.
MAX_LONG_POLL_SECS = 30

WakeKind = Literal["monitor", "cron", "workflow"]


def sanitize_owner(owner: str) -> str:
    """Filename-safe encoding of an owner key.

    An owner is ``kiro-cli:<acp sessionId>`` and the session id is
    caller-influenced, so it must never reach the filesystem verbatim: a ``/``
    or ``..`` would escape the queue directory. Every character outside a
    conservative allowlist becomes ``_``; the mapping is not required to be
    reversible because the file is keyed by the sanitised name and the record
    inside carries the real owner.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", owner)


@dataclass
class Wake:
    """One queued wake.

    ``handle`` is the producer-side id the turn is about (a loop id for a
    monitor, a job id for a cron); ``id`` is this wake's own ack handle. ``kind``
    tells the CLI which notification to emit and which subject the handle names.
    """

    kind: WakeKind
    handle: str
    message: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    cycle: int = 0
    reason: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Wake":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class WakeQueue:
    """Per-owner FIFO wake queues with JSONL persistence.

    One instance per gateway, shared by the producers and the routes. Every
    mutation is guarded by a single lock: the queues are small and the critical
    sections are pure in-memory list edits plus a synchronous rewrite of one
    small file, so a coarse lock is simpler than per-owner locking and cannot
    deadlock. The file write is done under the lock deliberately — an ack that
    returned before the durable delete could let a crash redeliver a wake the
    caller was told was acked, which is the one thing persistence exists to
    prevent.
    """

    def __init__(self, base_dir: Path, *, max_age_secs: int = DEFAULT_MAX_AGE_SECS) -> None:
        self._dir = base_dir
        self._max_age_secs = max_age_secs
        self._lock = asyncio.Lock()
        # owner -> ordered live wakes (FIFO). Only wakes NOT yet acked live here.
        self._queues: dict[str, list[Wake]] = {}
        # owner -> event fired whenever a wake is enqueued for that owner, so a
        # parked long-poll wakes immediately instead of waiting out its timeout.
        self._events: dict[str, asyncio.Event] = {}
        self._loaded = False

    # -- persistence ---------------------------------------------------------

    def _path_for(self, owner: str) -> Path:
        return self._dir / f"{sanitize_owner(owner)}.jsonl"

    def load(self) -> None:
        """Read every persisted queue back into memory (idempotent).

        Called once at startup off the event loop. A wake already past its max
        age at load time is dropped rather than resurrected: the CLI that would
        have run it is gone. A malformed line is skipped, never fatal — a
        corrupt queue file must not take the gateway down.
        """
        if self._loaded:
            return
        self._loaded = True
        if not self._dir.is_dir():
            return
        now = time.time()
        for path in sorted(self._dir.glob("*.jsonl")):
            wakes: list[Wake] = []
            owner: str | None = None
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                logger.warning("crew wakes: could not read %s", path.name, exc_info=True)
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    owner = rec.get("owner") or owner
                    wake = Wake.from_dict(rec["wake"])
                except (ValueError, KeyError, TypeError):
                    continue
                if now - wake.created_at >= self._max_age_secs:
                    continue
                wakes.append(wake)
            if owner and wakes:
                self._queues[owner] = wakes

    def _rewrite_locked(self, owner: str) -> None:
        """Persist the owner's current live queue, replacing the file.

        Caller holds ``self._lock``. An empty queue removes the file so a
        long-gone owner leaves no residue. Best-effort: a write failure is
        logged, not raised — the in-memory queue stays authoritative for this
        process, and the only cost of a lost write is a redelivery after
        restart, which the ack outcome tolerates.
        """
        queue = self._queues.get(owner) or []
        path = self._path_for(owner)
        try:
            if not queue:
                path.unlink(missing_ok=True)
                return
            self._dir.mkdir(parents=True, exist_ok=True)
            lines = [json.dumps({"owner": owner, "wake": w.to_dict()}) for w in queue]
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("crew wakes: persist failed for %s", sanitize_owner(owner), exc_info=True)

    # -- mutations -----------------------------------------------------------

    async def enqueue(
        self,
        owner: str,
        kind: WakeKind,
        handle: str,
        message: str,
        *,
        cycle: int = 0,
        reason: str = "",
    ) -> Wake:
        """Append one wake to *owner*'s FIFO and wake any parked long-poll."""
        wake = Wake(
            kind=kind, handle=handle, message=message, cycle=cycle, reason=reason
        )
        async with self._lock:
            self._queues.setdefault(owner, []).append(wake)
            self._rewrite_locked(owner)
            event = self._events.get(owner)
            if event is not None:
                event.set()
        return wake

    async def long_poll(self, owner: str, wait: float) -> list[Wake]:
        """Return *owner*'s live wakes, waiting up to *wait* seconds for one.

        Returns immediately with everything currently queued (after an expiry
        sweep) when the queue is non-empty; otherwise parks on the owner's event
        until a wake is enqueued or *wait* elapses, then returns whatever is
        there (possibly empty). *wait* is clamped to ``[0, MAX_LONG_POLL_SECS]``.

        Delivery does NOT remove the wake: the CLI acks it explicitly once the
        turn is submitted, so a CLI that crashes between poll and submit sees the
        same wake again. This is at-least-once by design.
        """
        wait = max(0.0, min(float(wait), float(MAX_LONG_POLL_SECS)))
        async with self._lock:
            self._sweep_owner_locked(owner)
            if self._queues.get(owner):
                return list(self._queues[owner])
            event = self._events.get(owner)
            if event is None:
                event = asyncio.Event()
                self._events[owner] = event
            event.clear()
        if wait > 0:
            try:
                await asyncio.wait_for(event.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
        async with self._lock:
            self._sweep_owner_locked(owner)
            return list(self._queues.get(owner) or [])

    async def ack(
        self, owner: str, wake_id: str, outcome: Literal["submitted", "dropped"]
    ) -> bool:
        """Remove a wake from *owner*'s queue. Returns True iff it was present.

        Both outcomes remove the wake: ``submitted`` means the CLI turned it into
        a turn, ``dropped`` means the CLI deliberately declined it (e.g. its
        session is gone). The distinction is recorded by the caller for audit;
        either way the wake must not be redelivered, so both delete. An unknown
        id (already acked, or never existed) returns False without error so a
        double-ack is idempotent.
        """
        async with self._lock:
            queue = self._queues.get(owner)
            if not queue:
                return False
            for i, wake in enumerate(queue):
                if wake.id == wake_id:
                    del queue[i]
                    self._rewrite_locked(owner)
                    return True
            return False

    # -- expiry --------------------------------------------------------------

    def _sweep_owner_locked(self, owner: str) -> int:
        """Drop expired wakes from one owner's queue. Caller holds the lock.

        Returns the number dropped, so a caller can decide whether to persist.
        Persists only when something changed, to keep a hot long-poll from
        rewriting an unchanged file every cycle.
        """
        queue = self._queues.get(owner)
        if not queue:
            return 0
        now = time.time()
        kept = [w for w in queue if now - w.created_at < self._max_age_secs]
        dropped = len(queue) - len(kept)
        if dropped:
            self._queues[owner] = kept
            self._rewrite_locked(owner)
        return dropped

    async def sweep(self) -> int:
        """Drop expired wakes across every owner. Returns total dropped.

        Called on a slow timer by the gateway; the long-poll path also sweeps
        the owner it touches, so this exists to reclaim queues for owners that
        stopped polling entirely (a CLI that exited without acking).
        """
        total = 0
        async with self._lock:
            for owner in list(self._queues.keys()):
                total += self._sweep_owner_locked(owner)
        return total

    # -- reads ---------------------------------------------------------------

    async def peek(self, owner: str) -> list[Wake]:
        """Owner's live wakes without waiting (post-sweep). For tests/inspect."""
        async with self._lock:
            self._sweep_owner_locked(owner)
            return list(self._queues.get(owner) or [])
