"""When a slot list may be read as complete, and which absent keys it must not drain.

A restart serves slot lists before startup has discovered every slot. Those lists
carry ``slotsPartial`` (the global label), ``slotsHeldKeys`` (known absent keys) and
``slotsHoldChannels`` (every channel session), so a client never drains the unread
dot of a session the list merely has not found yet. This module owns that hold
state machine; ``DashboardState`` binds each function below as a method of the same
name and keeps the fields on itself.

Fields of ``DashboardState`` this module reads and writes:

Writes: ``slot_list_rebuilding``, ``_slot_list_channel_held_keys``,
``_slot_list_released_keys``, ``_slot_list_hold_channels``,
``_slot_list_rebuild_finish_requested``, ``_slot_list_rebuild_began``,
``_slot_list_rebuild_deadline``.

Reads: ``unrestored_slot_notice``, ``unrestored_slot_keys``, ``_slots``,
``_slots_under_construction``, ``_slots_push_suspend``.

``_slot_list_released_keys`` holds dropped-tab keys whose session was deleted from
History since the restore reported them. The restore notice is written once per
boot, so without it a deleted tab's key would stay held, and its unread entry
undrained, until the next restart.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.state")

# How long a restart uses the global ``slotsPartial`` hold. Past this bound, known
# missing tabs and incomplete channel sessions stay protected by ``slotsHeldKeys``;
# every channel session stays protected by ``slotsHoldChannels`` until the first
# discovery pass returns, and after any pass that cannot name all sessions. Per-key
# holds have no expiry, including a never-reported restore whose seed may be the only
# known list of missing tabs. An unreadable open-tab registry means there may be MORE
# missing tabs than it could name, so only that unnameable remainder clears at the
# deadline; the keys it did report stay held like any other.
_SLOT_LIST_PARTIAL_DEADLINE_S: float = 600.0


def begin_slot_list_rebuild(self: DashboardState) -> None:
    """Label lists partial and hold channel keys until their first discovery pass."""
    self.slot_list_rebuilding = True
    self._slot_list_channel_held_keys = set()
    self._slot_list_released_keys = set()
    self._slot_list_hold_channels = True
    self._slot_list_rebuild_finish_requested = False
    self._slot_list_rebuild_began = time.monotonic()


def replace_channel_slot_holds(
    self: DashboardState, keys: Iterable[str], *, hold_channels: bool
) -> bool:
    """Replace the latest channel reconcile pass's destructive-drain holds."""
    replacement = {str(key) for key in keys if key}
    changed = (
        replacement != set(self._slot_list_channel_held_keys)
        or hold_channels != self._slot_list_hold_channels
    )
    self._slot_list_channel_held_keys = replacement
    self._slot_list_hold_channels = hold_channels
    return changed


def slot_list_held_keys(self: DashboardState) -> list[str]:
    """Known absent keys a complete-labelled list must not destructively drain.

    Sent on every list, including while the global partial hold is active, so
    the wire shape and meaning do not change when the deadline passes. An
    unreadable open-tab registry (``unknowable``) still names the tabs it did
    drop, so those are held here too; the tabs it could not name are covered by
    the global hold until its deadline.
    """
    notice = self.unrestored_slot_notice
    dropped = self.unrestored_slot_keys if notice is None else notice.get("keys", ())
    slots = getattr(self, "_slots", {})
    under_construction: set[str] = getattr(self, "_slots_under_construction", set())
    released = self._slot_list_released_keys
    held = {
        str(key)
        for key in dropped
        if key and (key in under_construction or (key not in slots and key not in released))
    }
    held.update(self._slot_list_channel_held_keys)
    return sorted(held)


def release_deleted_slot_keys(self: DashboardState, keys: Iterable[str]) -> None:
    """Stop holding dropped-tab keys whose session was deleted from History.

    Only an absent dropped tab is held, and a deleted session can never come
    back to claim its unread entry, so holding it would leave that entry
    counted until the next restart. A recycled key whose new slot is under
    construction is still held: that hold protects the new slot, not the
    deleted one. Only keys the restore reported are recorded, so the set stays
    bounded by the restore report rather than by History size. The caller
    pushes the next slot list.
    """
    notice = self.unrestored_slot_notice
    dropped = self.unrestored_slot_keys if notice is None else notice.get("keys", ())
    reported = {str(key) for key in dropped if key}
    released = {str(key) for key in keys if key} & reported
    if not released:
        return
    if not isinstance(self._slot_list_released_keys, set):
        self._slot_list_released_keys = set(self._slot_list_released_keys)
    self._slot_list_released_keys.update(released)


def slot_list_hold_channels(self: DashboardState) -> bool:
    """Whether every channel key awaits a first or replacement-safe discovery pass."""
    return self._slot_list_hold_channels


def arm_slot_list_rebuild_deadline(self: DashboardState) -> None:
    """Schedule the forced label clear, for a gateway that pushes no list after it."""
    if not self.slot_list_rebuilding or self._slot_list_rebuild_deadline is not None:
        return
    remaining = self._slot_list_rebuild_began + _SLOT_LIST_PARTIAL_DEADLINE_S
    self._slot_list_rebuild_deadline = asyncio.get_running_loop().call_later(
        max(0.0, remaining - time.monotonic()), self._expire_slot_list_rebuild
    )


def _expire_slot_list_rebuild(self: DashboardState) -> None:
    """Clear the label at the deadline; while a transient hold remains, re-check each second.

    The timer firing is the deadline, so it is not re-measured. The loop runs a
    timer up to its clock resolution early (about 16 ms on Windows), which
    re-measuring would read as not yet overdue.
    """
    if self._complete_slot_list_rebuild_if_ready(deadline_reached=True):
        self.push_slots_update()
        return
    if not self.slot_list_rebuilding:
        self._slot_list_rebuild_deadline = None
        return
    self._slot_list_rebuild_deadline = asyncio.get_running_loop().call_later(
        1.0, self._expire_slot_list_rebuild
    )


def _persistent_slot_list_hold(self: DashboardState) -> str | None:
    """Name the cause still holding the global partial label before its deadline."""
    notice = self.unrestored_slot_notice
    if notice is not None and notice.get("unknowable") is True:
        return "the open-tab registry was unreadable"
    if self.slot_list_hold_channels():
        return "channel sessions are not yet safe to drain"
    held = self.slot_list_held_keys()
    if held:
        return f"{len(held)} known slot key(s) are not safe to drain"
    if not self._slot_list_rebuild_finish_requested:
        return "no complete channel pass has finished"
    return None


def _complete_slot_list_rebuild_if_ready(
    self: DashboardState, *, deadline_reached: bool = False
) -> bool:
    """Clear the partial label once every dropped tab is listed, or once the hold is overdue.

    A suspended push always holds. Other causes hold globally until
    ``_SLOT_LIST_PARTIAL_DEADLINE_S``; known keys remain protected individually
    after that deadline. ``push_slots_update`` calls this before every list, so
    an unrelated push clears the label only when no cause above remains.
    """
    if not self.slot_list_rebuilding or self._slots_push_suspend:
        return False
    hold = self._persistent_slot_list_hold()
    if hold is not None:
        elapsed = time.monotonic() - self._slot_list_rebuild_began
        if not deadline_reached and elapsed < _SLOT_LIST_PARTIAL_DEADLINE_S:
            return False
        held_count = len(self.slot_list_held_keys())
        channel_scope = " and every channel slot" if self.slot_list_hold_channels() else ""
        logger.warning(
            "slot list still partial %.0fs after restart because %s; clearing "
            "the global label while %d known key(s)%s stay held",
            elapsed,
            hold,
            held_count,
            channel_scope,
        )
    self.slot_list_rebuilding = False
    self._slot_list_rebuild_finish_requested = False
    if self._slot_list_rebuild_deadline is not None:
        self._slot_list_rebuild_deadline.cancel()
        self._slot_list_rebuild_deadline = None
    return True


def finish_slot_list_rebuild(self: DashboardState) -> None:
    """Mark the slot list complete once no listed tab's unread dot would drain by mistake.

    A client that reads a list as complete drains the unread dot of every session
    the list omits. So the label waits on the open tabs the restore DROPPED, the
    keys ``unrestored_slot_notice`` reports (a metadata read or a rehydrate that
    failed), for as long as each is still absent from ``_slots``: a dropped tab
    the user reopens from History, or that a turn rebuilds, is listed again and
    stops holding the label. It does NOT wait on the rest of
    ``unrestored_slot_keys``, the remote-only reopen seeds of a remote-authority
    boot: those sit in the seed on every boot by design and their transcripts may
    never be fetched, so waiting on them held the label for the process lifetime
    and no deleted session's dot ever drained. Until the restore has reported
    (``unrestored_slot_notice`` still ``None``: a cancelled or aborted pass)
    every carried key counts, because none of them was read.

    Once called, later slot-list pushes retry the same condition. This lets a
    dropped tab rebuilt after boot clear the label even when channel surfacing is
    off, while a running channel reconciler still owns the first finish request.
    """
    self._slot_list_rebuild_finish_requested = True
    if self._complete_slot_list_rebuild_if_ready():
        self.push_slots_update()
