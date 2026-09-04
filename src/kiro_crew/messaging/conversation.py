"""Layer 3 -- shared per-conversation state for DM channels.

Every DM channel (Telegram, WeCom, ...) needs the same basic per-conversation
bookkeeping: a rotating *generation* (advanced by ``/new`` and idle/daily reset)
plus an awaiting-compact flag. This module holds the single shared
implementation so channels do not each carry a divergent copy.

The generation is seeded from the persisted session map on first access (via
the injected ``seed_fn``) so it reflects the highest generation already on disk.
Without seeding, an in-memory counter resets to 0 on gateway restart and a
subsequent ``/new`` advances 0 -> 1, colliding with a stale ``:gen1`` still
persisted on disk and resuming that old session instead of starting fresh.
Seeding to the highest persisted generation makes the first message after a
restart resume the latest conversation, and makes ``/new`` / idle / daily reset
advance past every persisted generation -- always a genuinely fresh session.

Stdlib-only apart from the sibling ``link`` helper; the seeding hook is injected
(rather than importing the session map) to keep this module cycle-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Generic, Hashable, Optional, TypeVar

from kiro_crew.messaging.link import should_rotate_generation

#: Conversation-identity key type. Channels key by whatever identifies a peer
#: (Telegram ``user_id`` int, WeCom ``userid`` str).
K = TypeVar("K", bound=Hashable)


@dataclass
class _ConvEntry:
    gen: int = 0
    awaiting_compact: bool = False
    last_active: float = 0.0


@dataclass
class ConversationState(Generic[K]):
    """In-memory per-conversation generation counter + awaiting-compact flag,
    shared by every DM channel.

    ``seed_fn`` maps a conversation key to the highest generation already
    persisted for its bucket (e.g. ``SessionMap.max_generation``); it is called
    once when the key is first seen so the counter is restart-safe. When
    omitted, the counter simply starts at 0 (used by tests / non-DM callers).
    """

    seed_fn: Optional[Callable[[K], int]] = None
    _state: dict[K, _ConvEntry] = field(default_factory=dict)

    def _get(self, key: K) -> _ConvEntry:
        entry = self._state.get(key)
        if entry is None:
            entry = _ConvEntry()
            if self.seed_fn is not None:
                entry.gen = max(0, self.seed_fn(key))
            self._state[key] = entry
        return entry

    def bump_gen(self, key: K) -> int:
        """Advance to a fresh generation (``/new``) and return the new value."""
        s = self._get(key)
        s.gen += 1
        s.awaiting_compact = False
        return s.gen

    def maybe_rotate(
        self, key: K, now: float, *, idle_minutes: int = 0, daily_reset_hour: int = -1
    ) -> bool:
        """Advance the generation on an idle/daily boundary, then record activity."""
        s = self._get(key)
        rotate = should_rotate_generation(
            s.last_active, now, idle_minutes=idle_minutes, daily_reset_hour=daily_reset_hour
        )
        if rotate:
            s.gen += 1
            s.awaiting_compact = False
        s.last_active = now
        return rotate

    def current_gen(self, key: K) -> int:
        return self._get(key).gen

    def set_awaiting(self, key: K) -> None:
        self._get(key).awaiting_compact = True

    def clear_awaiting(self, key: K) -> None:
        self._get(key).awaiting_compact = False

    def is_awaiting(self, key: K) -> bool:
        return self._get(key).awaiting_compact
