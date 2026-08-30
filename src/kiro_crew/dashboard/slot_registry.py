"""Owner-driven registry operations for dashboard chat slots."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SlotCreationPlan:
    """Registry-owned facts needed to finish constructing a new slot."""

    key: str
    requested_name: str
    minted_new: bool


class SlotRegistry:
    """Operate on the current containers owned by ``DashboardState``.

    The facade's restore, cleanup, and rollback paths replace registry
    containers wholesale.  Consequently this component never retains a
    reference to ``_slots``, ``_slots_under_construction``, or
    ``_slack_to_slot``; every operation reads them from its owner at call time.
    Helpers that remain monkeypatch seams are likewise supplied per call.
    """

    @staticmethod
    def get_slot(owner: Any, name: str) -> Any | None:
        """Return the slot currently registered under *name*, if any."""
        return owner._slots.get(name)

    @staticmethod
    def has_slot(owner: Any, name: str) -> bool:
        """Return whether *name* is present in the current slot registry."""
        return name in owner._slots

    @staticmethod
    def put_slot(owner: Any, name: str, slot: Any) -> Any:
        """Publish *slot* under *name* and return the identical object."""
        owner._slots[name] = slot
        return slot

    @staticmethod
    def pop_slot(owner: Any, name: str) -> Any | None:
        """Retract *name* without rebuilding or otherwise touching its slot."""
        return owner._slots.pop(name, None)

    @staticmethod
    def live_slot_count(owner: Any) -> int:
        """Count published and allocated-but-unpublished slots."""
        return len(owner._slots) + len(owner._slots_under_construction)

    @staticmethod
    def creator_slot_count(owner: Any, creator_key: str) -> int:
        """Count published slots attributed to one non-empty creator key."""
        if not creator_key:
            return 0
        # Construction reservations carry no creator attribution, so charging
        # them here would assign one caller another caller's in-flight slot.
        return sum(
            1 for slot in owner._slots.values() if getattr(slot, "_created_by", "") == creator_key
        )

    @staticmethod
    def begin_slot_construction(owner: Any, key: str) -> None:
        """Mark *key* as allocated but not yet published."""
        owner._slots_under_construction.add(key)

    @staticmethod
    def end_slot_construction(owner: Any, key: str) -> None:
        """Forget an allocation marker; repeated cleanup is harmless."""
        owner._slots_under_construction.discard(key)

    @staticmethod
    def running_session_keys(
        owner: Any,
        effective_session_key: Callable[[Any], str],
    ) -> frozenset[str]:
        """Return session keys whose current slots have turns in flight."""
        # Storage inventory calls this from a worker thread while the event loop
        # may mutate the registry.  Snapshotting prevents a read-only scan from
        # failing with ``RuntimeError: dictionary changed size``.
        return frozenset(
            effective_session_key(slot) for slot in list(owner._slots.values()) if slot.running
        )

    @staticmethod
    def spend_slot_by_session(
        owner: Any,
        effective_session_key: Callable[[Any], str],
    ) -> dict[str, str]:
        """Map each live session identity to the slot key holding its spend."""
        aliases: dict[str, str] = {}
        for slot in list(owner._slots.values()):
            try:
                session_key = effective_session_key(slot)
            except Exception:  # pragma: no cover - defensive during teardown
                continue
            if session_key:
                # Deliberately last-writer-wins for duplicate session owners,
                # matching insertion-order traversal of the facade registry.
                aliases[session_key] = slot.key
        return aliases

    @staticmethod
    def find_slot_by_session(
        owner: Any,
        session_key: str,
        effective_session_key: Callable[[Any], str],
    ) -> Any | None:
        """Return the first current slot whose effective identity matches."""
        for slot in owner._slots.values():
            if effective_session_key(slot) == session_key:
                return slot
        return None

    @staticmethod
    def get_linked_slot(owner: Any, session_key: str) -> Any | None:
        """Resolve a Slack link and prune its reverse-index row when stale."""
        slot_key = owner._slack_to_slot.get(session_key)
        if not slot_key:
            return None
        slot = owner._slots.get(slot_key)
        if not slot or not slot._slack_linked or slot._slack_thread_ts != session_key:
            owner._slack_to_slot.pop(session_key, None)
            return None
        return slot

    @staticmethod
    def resolve_slot(
        owner: Any,
        name: str,
        short_label_matches: Callable[[str], object | None],
    ) -> Any | None:
        """Resolve an exact key or the newest timestamped bare ``chat-N`` key."""
        slot = owner._slots.get(name)
        if slot is not None:
            return slot
        if not short_label_matches(name):
            return None

        # The separator is part of the prefix so chat-2 cannot match chat-20.
        prefix = name + "-"
        best_timestamp = -1
        best_slot: Any | None = None
        for key, candidate in owner._slots.items():
            if not key.startswith(prefix):
                continue
            tail = key[len(prefix) :]
            try:
                timestamp = int(tail)
            except ValueError:
                timestamp = -1
            # A genuine timestamp tie keeps the first insertion-order match.
            if best_slot is None or timestamp > best_timestamp:
                best_timestamp, best_slot = timestamp, candidate
        return best_slot

    @staticmethod
    def prepare_creation(
        owner: Any,
        name: str | None,
        *,
        mode: str,
        memory_mode: str | None,
        normalize_key: Callable[[str], str],
        mint_key: Callable[[str, int, int], str],
        timestamp_provider: Callable[[], float],
    ) -> tuple[Any | None, SlotCreationPlan | None]:
        """Reuse an existing slot or reserve the key facts for a new one.

        ``(existing, None)`` means construction must stop and return the exact
        registered object.  ``(None, plan)`` means the facade may construct and
        fully configure a slot, then publish it with :meth:`put_slot`.

        Construction, security tagging, session hydration, Slack indexing,
        active-slot sync, and client publication intentionally remain outside
        this registry primitive: those effects form one order-sensitive facade
        transaction and must finish before the slot becomes observable.
        """
        requested_name = ""
        if name:
            requested_name = name
            name = normalize_key(name)
            if not name:
                # A degenerate normalized key follows the ordinary mint path and
                # must not seed a display title from the unusable input.
                requested_name = ""

        # Reuse precedes the reserved-name check.  This permits callers to fetch
        # an already-valid member slot without becoming a member-slot creator.
        if name and name in owner._slots:
            existing = owner._slots[name]
            if memory_mode is not None and memory_mode != existing.memory_mode:
                raise ValueError(
                    f"Slot {name!r} already exists with memory_mode={existing.memory_mode!r}"
                )
            return existing, None

        if name and name.casefold().startswith("member-") and mode != "member":
            raise ValueError("member thread slots are created only via the member thread endpoint")

        minted_new = not name
        if not name:
            # Counter consumption is intentionally not rolled back: even a clock,
            # key-provider, or later slot-construction failure must not reuse it.
            owner._slot_counter += 1
            timestamp = int(timestamp_provider())
            name = mint_key("chat", owner._slot_counter, timestamp)

        return None, SlotCreationPlan(
            key=name,
            requested_name=requested_name,
            minted_new=minted_new,
        )

    @staticmethod
    def reseed_slot_counter(
        owner: Any,
        slot_index_from_key: Callable[[str], int | None],
        logger_provider: Callable[[], logging.Logger],
    ) -> None:
        """Advance the mint counter past every parseable current slot key."""
        max_index = owner._slot_counter
        for name in owner._slots:
            index = slot_index_from_key(name)
            if index is not None and index > max_index:
                max_index = index
        if max_index != owner._slot_counter:
            logger_provider().info(
                "Reseeded slot counter %d -> %d past highest restored slot index",
                owner._slot_counter,
                max_index,
            )
        owner._slot_counter = max_index
