"""SideState: sidecar buffer attached to a parent ChatSlot.

Side messages live only on ``slot._side``; they are never persisted to
JSONL, memory.db, lessons, or preferences. Lifecycle: open → turn(s) → close.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from kiro_crew.dashboard.steer_settle import settle_consumed_steers

#: FIFO ceiling on messages held behind an in-flight side turn. The sidecar is
#: ephemeral and lives entirely in memory on the parent slot, so an unbounded
#: queue is a client-driven memory sink; refusing past this depth keeps the
#: pressure visible to the user instead of silently growing the process.
MAX_SIDE_QUEUE = 20

#: Steer ledger states. ``pending`` = handed to the backend, delivery unproven.
#: ``consumed`` = the backend echoed it, so it DID reach a generation.
#: ``requeued`` = it never reached one and is now an ordinary queue card.
STEER_PENDING = "pending"
STEER_CONSUMED = "consumed"
STEER_REQUEUED = "requeued"

#: Ledger ceiling. Terminal entries are pruned when a new turn starts, so this
#: only bounds a pathological single turn.
MAX_STEER_LEDGER = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SideState:
    """One side conversation attached to a parent slot.

    ``is_complete`` is False while a turn is in flight; flipped True in the
    ``_run_side_turn`` finally block.

    ``queue`` holds messages submitted while a turn is in flight, as
    ``{"id", "content", "ts"}`` dicts. It is drained one entry per turn by
    ``_run_side_turn``'s finally block.
    """

    open: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_run_id: str = ""
    is_complete: bool = True
    created_at: str = field(default_factory=_now_iso)
    queue: list[dict[str, str]] = field(default_factory=list)
    #: Ledger of steers handed to the backend, as
    #: ``{"id", "text", "state"}`` where state is one of
    #: :data:`STEER_PENDING` / :data:`STEER_CONSUMED` / :data:`STEER_REQUEUED`.
    #:
    #: An EXPLICIT state, not presence-in-a-list: "delivered", "never injected"
    #: and "already turned into a card" are three different outcomes that a
    #: submitter has to tell apart, and inferring them from an entry's absence
    #: cannot distinguish the first from the third.
    steers: list[dict[str, str]] = field(default_factory=list)

    def append_user(self, content: str, ts: str = "", *, steer: bool = False) -> None:
        """Append a user turn. ``steer`` marks it as injected mid-turn, which the
        panel renders as a distinct bubble rather than a normal question."""
        entry: dict[str, Any] = {
            "role": "user",
            "content": content,
            "ts": ts or _now_iso(),
        }
        if steer:
            entry["steer"] = True
        self.messages.append(entry)

    def append_assistant(self, content: str, ts: str = "") -> None:
        self.messages.append(
            {
                "role": "assistant",
                "content": content,
                "ts": ts or _now_iso(),
            }
        )

    def clear(self) -> None:
        self.messages.clear()
        self.last_run_id = ""
        self.is_complete = True
        self.queue.clear()
        self.steers.clear()

    # ── Queue helpers ──

    def queue_append(self, content: str) -> str | None:
        """Append to the pending queue. Returns the queue ID, or None when there is
        no room left (caller surfaces a 429).

        "Room" counts pending steers as well as queued entries. Each in-flight steer
        has reserved the slot its requeue may need, and an ordinary submit must not
        spend it: doing so leaves the requeue nowhere to land and loses a question
        already reported as delivered. A reservation only holds if every writer
        honours it, so this measures the same effective capacity as
        ``steer_can_accept``.
        """
        if len(self.queue) + len(self.steer_pending()) >= MAX_SIDE_QUEUE:
            return None
        qid = uuid.uuid4().hex[:12]
        self.queue.append({"id": qid, "content": content, "ts": _now_iso()})
        return qid

    def queue_headroom(self) -> int:
        """Slots left before the queue hits :data:`MAX_SIDE_QUEUE`."""
        return max(0, MAX_SIDE_QUEUE - len(self.queue))

    def queue_insert_front(self, content: str) -> str | None:
        """Put *content* at the HEAD of the queue, ahead of anything waiting.

        For text that was meant to reach the CURRENT turn (an unconsumed steer, or
        an entry whose dispatch failed) — it should run before entries the user
        submitted after it.

        Bounded like every other write. Refusing text the user believes was
        accepted IS a loss, which is why acceptance reserves a slot for each
        in-flight steer (see ``steer_can_accept``): by the time a requeue lands
        there is room for it. This bound is the backstop that keeps a leak from
        growing without limit if that reservation is ever wrong, rather than a
        gate the normal path is expected to hit.
        """
        if len(self.queue) >= MAX_SIDE_QUEUE:
            return None
        qid = uuid.uuid4().hex[:12]
        self.queue.insert(0, {"id": qid, "content": content, "ts": _now_iso()})
        return qid

    def steer_can_accept(self) -> bool:
        """Whether a new steer can be accepted without risking an unbounded queue.

        Every PENDING steer may end up requeued at the head when the turn ends, so
        each one reserves a slot. Declining here is visible and recoverable — the
        caller falls through to the queue, which either takes it or answers 429 —
        whereas declining at requeue time would drop a question already reported
        as delivered.
        """
        return len(self.queue) + len(self.steer_pending()) < MAX_SIDE_QUEUE

    def queue_pop(self) -> dict[str, str] | None:
        """Pop the oldest queued entry, or None when the queue is empty."""
        if not self.queue:
            return None
        return self.queue.pop(0)

    def queue_remove(self, queue_id: str) -> str | None:
        """Remove one entry by ID. Returns its content, or None if not found."""
        for i, item in enumerate(self.queue):
            if item["id"] == queue_id:
                del self.queue[i]
                return item["content"]
        return None

    def queue_edit(self, queue_id: str, content: str) -> bool:
        """Replace one entry's content by ID, preserving order. True if found."""
        for item in self.queue:
            if item["id"] == queue_id:
                item["content"] = content
                return True
        return False

    # ── Steer ledger ──

    def steer_register(self, text: str) -> str | None:
        """Record a steer as handed to the backend, delivery unproven.

        Returns its ledger id, or None when the caller must fall through to the
        bounded queue instead.

        Capacity is resolved by state, because the two simple policies are each
        wrong in one direction. Evicting the oldest entry unconditionally can
        drop a **pending** one — whose outcome nobody has recorded yet, so the
        cleanup pass can no longer requeue that unanswered question. Refusing
        outright once the ledger is full means a long-lived side conversation
        permanently loses steering, silently falling back to the queue forever.

        So: make room by evicting only entries whose outcome is already
        **terminal** (a submitter that missed one reads ``None`` and treats it as
        already-placed, which is what a terminal entry means), and decline only
        in the genuinely pathological case where every slot is pending — i.e.
        MAX_STEER_LEDGER steers are in flight at once. Declining then is safe:
        the queue is bounded and visible.
        """
        if len(self.steers) >= MAX_STEER_LEDGER and not self._steer_evict_one():
            return None
        sid = uuid.uuid4().hex[:12]
        self.steers.append({"id": sid, "text": text, "state": STEER_PENDING})
        return sid

    def _steer_evict_one(self) -> bool:
        """Drop the oldest TERMINAL entry. False if every entry is pending."""
        for i, entry in enumerate(self.steers):
            if entry["state"] != STEER_PENDING:
                del self.steers[i]
                return True
        return False

    def steer_state(self, steer_id: str) -> str | None:
        """The ledger state for *steer_id*, or None if it is not (or no longer)
        recorded. A submitter reads its OWN id, so it never has to guess what an
        absence means."""
        for entry in self.steers:
            if entry["id"] == steer_id:
                return entry["state"]
        return None

    def steer_mark(self, steer_id: str, state: str) -> None:
        for entry in self.steers:
            if entry["id"] == steer_id:
                entry["state"] = state
                return

    def steer_pending(self) -> list[dict[str, str]]:
        """Ledger entries whose delivery the backend has not confirmed."""
        return [e for e in self.steers if e["state"] == STEER_PENDING]

    def steer_settle(self, snapshot: str) -> list[dict[str, str]]:
        """Mark the pending steers this ``steering_consumed`` echo accounts for.

        Entries are MARKED, not removed: a submitter still awaiting its RPC needs
        to learn that its steer was delivered, which an erased entry cannot tell
        it. Returns the entries settled, in ledger order, because proof of
        consumption is also the only thing that may put a steer in the transcript
        — the caller renders exactly what this returns.
        """
        pending = self.steer_pending()
        if not pending:
            return []
        remaining = settle_consumed_steers([e["text"] for e in pending], snapshot)
        # Count-aware: each remaining text keeps exactly one entry pending, so a
        # duplicate steer registered after the snapshot is not swept with it.
        still: dict[str, int] = {}
        for text in remaining:
            still[text] = still.get(text, 0) + 1
        settled: list[dict[str, str]] = []
        for entry in pending:
            if still.get(entry["text"], 0) > 0:
                still[entry["text"]] -= 1
                continue
            entry["state"] = STEER_CONSUMED
            settled.append(entry)
        return settled
