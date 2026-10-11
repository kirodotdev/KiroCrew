"""Publish a chat-slot create only once every step that can refuse it has passed.

``POST /api/chat/slots`` runs steps after its slot is built that can still
refuse: the app session check and the owner's member assignment. Registering the
slot first and refusing later answers an error and leaves the slot registered, so
the coalesced broadcast publishes it and every retry adds another tab with no
session behind it.

The create therefore builds its slot privately (``DashboardState.prepare_slot``)
and holds the key's construction mark, the same ``_slots_under_construction``
reservation the import path holds across its own unregistered tail: no other
request can register a slot under the key, and every by-name route sees no slot,
exactly as before the create began. :class:`PendingSlotCreate` publishes the slot
(``DashboardState.publish_slot``) after the last fallible step, under the
session-switch lock on the owner path. Leaving the create without publishing (a
refusal response, an exception or a cancellation) drops the private slot,
releases the mark, and puts back by compare-and-set only the binding writes this
create made under the key it reserved. Nothing else is undone, because nothing
else was published: no transcript is ever deleted.

Same-name creates take :func:`acquire_slot_create_name` first, so a second create
waits for the first to publish or give up, and then finds the slot open or absent.
The gate records which principal each create of the name acts for, so an app never
waits behind another principal's create (:func:`name_held_by_another`).

A create asked to file its slot into a folder publishes the slot UNFILED first.
The filing is a separate step after publication, through the ordinary folder-move
path, so a create writes nothing to a folder before it commits, and a filing the
move path refuses leaves a published, unfiled slot rather than a refused create.

A closing slot owns its key until its close settles (:func:`close_holds_key`):
its archive commits, or it is put back in place. A create of that key meanwhile
is refused with the under-construction 409 (:func:`refuse_create_while_closing`),
before the registry is read, so no create can hold the key a failed close puts
its slot back into, nor answer with the slot a close is about to archive.

Every other opener of the key (a send, a cron result, a workflow fallback, an
OpenAI-compatible call, a resume) waits for the pending create with
:func:`wait_for_pending_create`, bounded by the same 30 s, and then opens the
slot the create published, or the free key, exactly as it did when the create
registered its slot up front.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import weakref
from collections.abc import Callable, Iterator
from typing import Any

from kiro_crew.dashboard.chat_utils import drained_to_thread, session_key_for
from kiro_crew.execution_context import read_session_execution
from kiro_crew.session_agent_selection import SelectionChange, restore_agent_selection

logger = logging.getLogger(__name__)

#: How long a create waits for another create of the same name to finish, and
#: the budget for the locks the create itself waits on (:class:`CreateDeadline`).
SLOT_CREATE_NAME_WAIT_SECONDS = 30.0

#: The least time a lock wait gets, so a create that ran long but meets no
#: contention is not refused by a spent budget.
_LOCK_WAIT_FLOOR_SECONDS = 1.0


class CreateDeadline:
    """The create's budget for its own lock waits, started when it holds its name."""

    __slots__ = ("_at",)

    def __init__(self) -> None:
        self._at = asyncio.get_running_loop().time() + SLOT_CREATE_NAME_WAIT_SECONDS

    def remaining(self) -> float:
        return max(_LOCK_WAIT_FLOOR_SECONDS, self._at - asyncio.get_running_loop().time())


async def acquire_by(lock: Any, deadline: CreateDeadline) -> bool:
    """Acquire *lock* within *deadline*; False when the wait ran out. The caller releases."""
    try:
        await asyncio.wait_for(lock.acquire(), deadline.remaining())
    except asyncio.TimeoutError:
        return False
    return True


def current_execution_record(session_key: str) -> dict[str, Any] | None:
    """*session_key*'s execution record as JSON-ready data, None when it has none. Blocking."""
    current = read_session_execution(session_key)
    return current.to_record() if current is not None else None


def binding_before_write(session_key: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """What a binding write is about to replace: the execution record, and the legacy fields.

    The legacy ``memory_store`` / ``memory_mode`` fields are read only when there
    is no execution record, as :func:`record_agent_selection` does, so the undo
    puts the line's own binding back. Blocking.
    """
    from kiro_crew import session_agent_selection

    prior = current_execution_record(session_key)
    legacy = session_agent_selection._legacy_binding(session_key) if prior is None else None
    return prior, legacy


class _NameGate:
    __slots__ = ("lock", "users", "principals")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0
        #: How many of ``users`` act for each principal ("" is the dashboard).
        self.principals: dict[str, int] = {}


# One table per DashboardState. An entry lives only while a create holds or
# waits for its name, so the table is bounded by the creates in flight.
_NAME_GATES: weakref.WeakKeyDictionary[Any, dict[str, _NameGate]] = weakref.WeakKeyDictionary()


# The keys a create holds unpublished, each with the create holding it. One
# table per DashboardState, bounded by the creates in flight.
_PENDING_CREATES: weakref.WeakKeyDictionary[Any, dict[str, PendingSlotCreate]] = (
    weakref.WeakKeyDictionary()
)


async def wait_for_pending_create(state: Any, key: str) -> None:
    """Wait, bounded, until no create holds *key* unpublished. Returns at once when none does.

    For an opener that is not a create: on return the key holds the slot the
    create published, or is free. Past the bound it returns anyway, and the
    opener meets the "still being built" refusal it handles today.

    *key* is folded here, the way the registry folds a requested name, because
    a create holds its key folded: a caller may pass the name it was given.
    """
    from kiro_crew.dashboard.state import _normalize_slot_key

    key = _normalize_slot_key(key)
    create = _pending_create(state, key)
    if create is None:
        return
    try:
        await asyncio.wait_for(create._finished.wait(), SLOT_CREATE_NAME_WAIT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("slot %s: a create of this key outlasted the wait", key)


def _pending_create(state: Any, key: str) -> PendingSlotCreate | None:
    try:
        return _PENDING_CREATES.get(state, {}).get(key)
    except TypeError:
        # A state that cannot be weakly referenced never held a create.
        return None


# The keys a close owns, each with how many closes hold it. One table per
# DashboardState, bounded by the closes in flight.
_CLOSING_KEYS: weakref.WeakKeyDictionary[Any, dict[str, int]] = weakref.WeakKeyDictionary()


@contextlib.contextmanager
def close_holds_key(state: Any, key: str) -> Iterator[None]:
    """Hold *key* for a close, from before its pop until it settles; released on every exit.

    While held, :func:`refuse_create_while_closing` refuses a create of the key,
    so a close that fails puts its slot back into a key no create holds.
    """
    try:
        keys = _CLOSING_KEYS.setdefault(state, {})
    except TypeError:
        # A state that cannot be weakly referenced never holds a create.
        yield
        return
    keys[key] = keys.get(key, 0) + 1
    try:
        yield
    finally:
        keys[key] -= 1
        if not keys[key]:
            del keys[key]


class SlotKeyClosing(ValueError):
    """A create refused because a close still holds its key. Answered 409 ``slot_under_construction``."""

    code = "slot_under_construction"


def refuse_create_while_closing(state: Any, key: str) -> None:
    """Raise :class:`SlotKeyClosing` while a close holds *key* (a key the registry has folded)."""
    try:
        held = key in _CLOSING_KEYS.get(state, {})
    except TypeError:
        return
    if held:
        raise SlotKeyClosing(f"slot {key} is still being closed; retry once it is closed")


def name_held_by_another(state: Any, key: str, principal: str) -> bool:
    """Whether a create acting for another principal holds or waits for *key*.

    Synchronous, so a caller that checks it and then calls
    :func:`acquire_slot_create_name` with no await between them is judged against
    the gate it joins. An app checks it before the wait: waiting behind another
    principal's create would let the latency of its answer tell that a create of
    the name is in flight.
    """
    try:
        gate = _NAME_GATES.get(state, {}).get(key)
    except TypeError:
        return False
    if gate is None:
        return False
    return any(count and who != principal for who, count in gate.principals.items())


async def acquire_slot_create_name(
    state: Any, key: str, principal: str = ""
) -> Callable[[], None] | None:
    """Wait (bounded) to be the only create of *key*; returns the release, or None on timeout.

    *principal* is who the create acts for: the app id, or "" for the dashboard.
    """
    gates = _NAME_GATES.setdefault(state, {})
    gate = gates.get(key)
    if gate is None:
        gate = gates[key] = _NameGate()
    gate.users += 1
    gate.principals[principal] = gate.principals.get(principal, 0) + 1

    def forget() -> None:
        gate.users -= 1
        gate.principals[principal] -= 1
        if not gate.principals[principal]:
            del gate.principals[principal]
        if gate.users == 0 and gates.get(key) is gate:
            del gates[key]

    try:
        await asyncio.wait_for(gate.lock.acquire(), SLOT_CREATE_NAME_WAIT_SECONDS)
    except asyncio.TimeoutError:
        forget()
        return None
    except BaseException:
        forget()
        raise

    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            gate.lock.release()
            forget()

    return release


class PendingSlotCreate:
    """One create's private slot and the binding writes it made, until it publishes."""

    def __init__(self, state: Any, pending: Any) -> None:
        self._state = state
        self._pending = pending
        self._binding_changes: list[tuple[str, SelectionChange]] = []
        self._published = False
        self._settled = False
        self._finished = asyncio.Event()
        slot = pending.slot
        # The session no request can reach while the key is reserved. A slot
        # linked to a channel runs on that channel's session, which is
        # published already, so nothing written there is ever undone.
        self._own_session_key = "" if slot.linked_session_key else session_key_for(slot.key, "")
        state.begin_slot_construction(slot.key)
        _PENDING_CREATES.setdefault(state, {})[slot.key] = self

    def _finish(self) -> None:
        """Wake every opener waiting on the key. The key is published or free by now."""
        pending = _PENDING_CREATES.get(self._state)
        if pending is not None and pending.get(self.slot.key) is self:
            del pending[self.slot.key]
        self._finished.set()

    @property
    def slot(self) -> Any:
        return self._pending.slot

    def binding_written(self, session_key: str, change: SelectionChange | None) -> None:
        """Record one binding write, as ``(prior, written)`` records, to undo if never published.

        Only a write under the reserved key's own session is recorded.
        """
        if session_key != self._own_session_key or not session_key:
            return
        if change is not None and change[0] != change[1]:
            self._binding_changes.append((session_key, change))

    def publish(self) -> Any:
        """Register the slot. Synchronous: no request sees the key between mark and slot."""
        slot = self._state.publish_slot(self._pending)
        self._published = True
        self._state.end_slot_construction(slot.key)
        self._finish()
        return slot

    async def settle(self) -> None:
        """Give the create up unless it published. Runs on every exit, once."""
        if self._settled:
            return
        self._settled = True
        try:
            await self._undo_unpublished()
        finally:
            if not self._published:
                self._state.end_slot_construction(self.slot.key)
                self._finish()

    async def _undo_unpublished(self) -> None:
        """Undo the binding writes of a create that never published."""
        if self._published:
            return
        try:
            if self._binding_changes:
                await drained_to_thread(self._undo_bindings)
        except Exception:
            # The key has no slot, so nothing visible is wrong. The binding stays
            # on disk, where the next create of this name meets it.
            logger.warning(
                "slot create %s: could not put its binding back",
                self.slot.key,
                exc_info=True,
            )

    def _undo_bindings(self) -> None:
        """Put back each binding write, newest first. Blocking.

        Each restore is a compare-and-set on the record this create wrote, so a
        newer binding written by anyone else is left alone.
        """
        for session_key, change in reversed(self._binding_changes):
            restore_agent_selection(session_key, change)
