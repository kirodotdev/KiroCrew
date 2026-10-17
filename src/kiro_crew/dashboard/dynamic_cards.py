"""Bounded, event-driven publication of session-owned dashboard content.

This queue owns presentation content only. Runtime status and decision authority
remain in the existing slot / question / approval event streams. Reading a card
never schedules work. A failed generation retains the last good card only while
the owning source, identity and privacy remain valid.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: The reason a seeded (restored or newly enabled) session is queued with.
RESTORED = "restored"
_HOUR = 3600.0

MAX_HTML_BYTES = 8192
MAX_DATA_BYTES = 4096
MAX_OUTPUT_BYTES = 16384
MAX_INPUT_CHARS = 16000
_FIELD_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,47}\Z")


def normalize_card(raw: object, previous: dict | None = None) -> dict | None:
    """Accept inert layout plus flat text bindings, never executable updates."""
    if not isinstance(raw, dict):
        return None
    html = raw.get("html", (previous or {}).get("html"))
    data = raw.get("data")
    if not isinstance(html, str) or not html.strip() or len(html.encode("utf-8")) > MAX_HTML_BYTES:
        return None
    if not isinstance(data, dict) or len(data) > 24:
        return None
    if "html" not in raw and (previous is None or data.keys() != previous["data"].keys()):
        return None
    size = 0
    for key, value in data.items():
        if not isinstance(key, str) or not _FIELD_NAME.fullmatch(key) or not isinstance(value, str):
            return None
        size += len(key.encode("utf-8")) + len(value.encode("utf-8"))
    if size > MAX_DATA_BYTES:
        return None
    return {"html": html, "data": dict(data)}


@dataclass(frozen=True)
class CardBudget:
    debounce: float = 2.0
    per_session: float = 120.0
    per_hour: int = 60
    capacity: int = 128
    timeout: float = 60.0


@dataclass
class CardEntry:
    key: str
    owner: object
    binding: str
    reason: str
    event_at: float
    due: float
    revision: int = 1
    published_revision: int = 0
    published_at: float | None = None
    content_event_at: float | None = None
    payload: dict | None = None
    pending: bool = True
    failed: bool = False
    generated_source: object = None
    published_source: object = None


class CardPublisher:
    """One loop-owned queue and one in-flight call for the entire gateway."""

    def __init__(
        self,
        generate: Callable[[CardEntry], Awaitable[dict | None]] | None,
        valid: Callable[[CardEntry], bool],
        changed: Callable[[str], None],
        *,
        budget: CardBudget = CardBudget(),
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.budget = budget
        self.generate = generate
        self.valid = valid
        self.changed = changed
        self.clock = clock
        self.wall_clock = wall_clock
        self.entries: OrderedDict[str, CardEntry] = OrderedDict()
        self.attempts: deque[float] = deque()
        self.last_attempt: dict[str, float] = {}
        self.active: CardEntry | None = None

    def notify(self, key: str, owner: object, binding: str, reason: str) -> None:
        now = self.clock()
        entry = self.entries.get(key)
        if entry is None or entry.owner != owner or entry.binding != binding:
            if entry is not None:
                # Clear the prior presentation before a same-key replacement
                # becomes visible; keep any active operation's permit/budget.
                self.forget(key)
            elif len(self.entries) >= self.budget.capacity:
                # Never evict the operation currently holding the sole permit.
                victim = next(
                    (k for k, item in self.entries.items() if item is not self.active), None
                )
                if victim is None:
                    return
                del self.entries[victim]
                self.changed(victim)
            entry = CardEntry(
                key, owner, binding, reason, self.wall_clock(), now + self.budget.debounce
            )
            self.entries[key] = entry
        else:
            entry.reason = reason
            entry.event_at = self.wall_clock()
            entry.revision += 1
            self.entries.move_to_end(key)
            if entry.pending:
                # Already queued and already stale: what a reader sees has not
                # changed, so there is nothing to tell the watching tabs.
                return
            # Fixed first-event deadline: a noisy producer cannot starve itself
            # by continuously extending a sliding debounce window.
            entry.due = now + self.budget.debounce
            entry.pending = True
        self.changed(key)

    def forget(self, key: str) -> None:
        if self.entries.pop(key, None) is not None:
            self.changed(key)

    def _prune_attempts(self) -> None:
        cutoff = self.clock() - _HOUR
        while self.attempts and self.attempts[0] <= cutoff:
            self.attempts.popleft()
        session_cutoff = self.clock() - self.budget.per_session
        self.last_attempt = {
            key: at for key, at in self.last_attempt.items() if at > session_cutoff
        }

    def _session_due(self, entry: CardEntry) -> float:
        at = self.last_attempt.get(entry.binding)
        return at + self.budget.per_session if at is not None else 0

    def next_delay(self) -> float | None:
        self._prune_attempts()
        pending = [entry for entry in self.entries.values() if entry.pending]
        if not pending:
            return None
        global_due = self.attempts[0] + _HOUR if len(self.attempts) >= self.budget.per_hour else 0
        due = min(max(entry.due, global_due, self._session_due(entry)) for entry in pending)
        return max(0, due - self.clock())

    def read(self, key: str) -> dict[str, Any] | None:
        entry = self.entries.get(key)
        if entry is None:
            return None
        self._prune_attempts()
        status = "published"
        if self.active is entry:
            status = "generating"
        elif entry.pending:
            status = "budget" if len(self.attempts) >= self.budget.per_hour else "queued"
        elif entry.failed:
            status = "failed"
        return {
            "card": copy.deepcopy(entry.payload),
            "status": status,
            "published_at": entry.published_at,
            "content_event_at": entry.content_event_at,
            "stale": entry.published_revision != entry.revision,
        }

    async def run_ready(self) -> int:
        """Consume at most one eligible event. Failed attempts also spend budget."""
        if self.active is not None or self.generate is None:
            return 0
        self._prune_attempts()
        if len(self.attempts) >= self.budget.per_hour:
            return 0
        now = self.clock()
        # The entries dict is also the eviction order, and a new event moves its
        # entry to the end, so serving in dict order put the session a person is
        # using behind every idle one. Serve a live event before a restore seed,
        # and the longest-waiting first within each.
        ready = [
            item
            for item in self.entries.values()
            if item.pending and item.due <= now and self._session_due(item) <= now
        ]
        entry = min(ready, key=lambda item: (item.reason == RESTORED, item.due), default=None)
        if entry is None:
            return 0
        if not self.valid(entry):
            self.forget(entry.key)
            return 0
        self.active = entry
        entry.pending = False
        self.attempts.append(now)
        self.last_attempt[entry.binding] = now
        revision, event_at = entry.revision, entry.event_at
        self.changed(entry.key)
        try:
            payload = await asyncio.wait_for(self.generate(entry), self.budget.timeout)
            if self.entries.get(entry.key) is not entry or not self.valid(entry):
                if self.entries.get(entry.key) is entry:
                    self.forget(entry.key)
                return 1
            payload = normalize_card(payload, entry.payload)
            entry.failed = payload is None
            if payload is not None:
                entry.payload = payload
                entry.published_at = self.wall_clock()
                entry.content_event_at = event_at
                entry.published_revision = revision
                entry.published_source = entry.generated_source
        except Exception:
            entry.failed = True
            logger.debug("Dynamic card generation failed", exc_info=True)
        finally:
            self.active = None
            self.changed(entry.key)
        return 1
