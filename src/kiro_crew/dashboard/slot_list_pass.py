"""Slot-list serializations shared by the callers that ask for them together.

Every ``/api/ws`` connect (``ws._serve_ws``) and every ``GET /api/chat/slots``
(``chat_api.slot_detail.api_chat_slots``) needs the whole serialized slot list, and
a dashboard tab sends both each time it reconnects. A stalled event loop drops every
tab's socket at once, so the tabs come back together, and one full
``serialize_slots`` pass on the loop per request then holds the loop for as many
passes as there are requests: the storm prolongs the stall that caused it.

:func:`shared_slot_list` coalesces them on a leading and a trailing edge, as
``push_slots_update`` does for the broadcast. A caller that finds no pass of its
audience within the window after the last one serializes at once, as an unshared
call would. A caller inside that window joins the trailing pass, which runs once a
window has gone by with no caller joining it, and at most :data:`PASS_WINDOW_MAX_S`
after its first caller. Each list is taken after its callers asked, so none of them
receives an older list than serializing at once would have given it.

A browser lets one WebSocket per host be in its opening handshake at a time, so the
tabs of a reconnect storm arrive one handshake apart rather than together. While the
trailing pass waits the loop is free to answer those handshakes, so the whole run of
them joins it. The window is :data:`PASS_WINDOW_PASSES` times as long as the last
pass took, within :data:`PASS_WINDOW_MIN_S` and :data:`PASS_WINDOW_MAX_S`: what a
caller may wait grows with what sharing a pass saves it, so a cheap list costs a
caller next to nothing.

The list and its rows are shared by every caller of a trailing pass: a caller copies
before it changes either.
"""

from __future__ import annotations

import asyncio
import math
import weakref
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

#: The shortest window: callers a few loop iterations apart still share a cheap pass.
PASS_WINDOW_MIN_S = 0.02
#: How many of the last pass's durations the window spans.
PASS_WINDOW_PASSES = 3
#: The longest window, and the longest a trailing pass waits after its first caller:
#: the slots broadcast's own ceiling.
PASS_WINDOW_MAX_S = 2.0

#: The loop a pass runs on, ``include_check_status`` and ``dashboard_user``.
_Audience = tuple[asyncio.AbstractEventLoop, bool, bool]


def window_after(duration: float) -> float:
    """The window after a pass that took *duration* seconds."""
    return min(PASS_WINDOW_MAX_S, max(PASS_WINDOW_MIN_S, PASS_WINDOW_PASSES * duration))


def trailing_due(first_joined: float, last_joined: float, window: float) -> float:
    """When a trailing pass runs: a window after its last caller joined, and no later
    than :data:`PASS_WINDOW_MAX_S` after its first."""
    return min(first_joined + PASS_WINDOW_MAX_S, last_joined + window)


class _AudiencePasses:
    """An audience's passes: when the last one ended, the window after it, and the
    callers of the trailing pass with when the first and the last of them joined and
    when its timer is set to fire."""

    __slots__ = ("taken_at", "window", "waiters", "first_joined", "last_joined", "armed_for")

    def __init__(self) -> None:
        self.taken_at = -math.inf
        self.window = PASS_WINDOW_MIN_S
        #: ``None`` while no trailing pass is scheduled.
        self.waiters: list[asyncio.Future[list[dict]]] | None = None
        self.first_joined = 0.0
        self.last_joined = 0.0
        self.armed_for = 0.0

    def ended(self, started: float, ended: float) -> None:
        self.taken_at = ended
        self.window = window_after(ended - started)


#: Per dashboard state, each audience's passes.
_PASSES: weakref.WeakKeyDictionary[DashboardState, dict[_Audience, _AudiencePasses]] = (
    weakref.WeakKeyDictionary()
)


async def shared_slot_list(
    state: DashboardState,
    *,
    include_check_status: bool = False,
    dashboard_user: bool = False,
) -> list[dict]:
    """``state.serialize_slots(...)``, shared with the callers that ask with it.

    A caller cancelled while it waits for a trailing pass is dropped from it. A state
    the pass table cannot key (no weak reference to it, or no hash) shares nothing:
    each caller serializes at once, as ``push_slots_update`` coalesces nothing for a
    state built without its lock.
    """
    loop = asyncio.get_running_loop()
    audience: _Audience = (loop, bool(include_check_status), bool(dashboard_user))
    try:
        by_audience = _PASSES.setdefault(state, {})
    except TypeError:
        return _serialize(state, audience)
    passes = by_audience.setdefault(audience, _AudiencePasses())
    now = loop.time()
    if passes.waiters is None and now - passes.taken_at >= passes.window:
        return _take_now(state, audience, passes)
    if passes.waiters is None:
        passes.waiters = []
        passes.first_joined = now
        passes.armed_for = now + passes.window
        loop.call_at(passes.armed_for, _take_trailing, state, audience)
    passes.last_joined = now
    future: asyncio.Future[list[dict]] = loop.create_future()
    passes.waiters.append(future)
    return await future


def _serialize(state: DashboardState, audience: _Audience) -> list[dict]:
    _loop, include_check_status, dashboard_user = audience
    return state.serialize_slots(
        include_check_status=include_check_status, dashboard_user=dashboard_user
    )


def _take_now(state: DashboardState, audience: _Audience, passes: _AudiencePasses) -> list[dict]:
    """The leading edge: serialize for this one caller, inline."""
    started = audience[0].time()
    try:
        return _serialize(state, audience)
    finally:
        passes.ended(started, audience[0].time())


def _take_trailing(state: DashboardState, audience: _Audience) -> None:
    """The trailing edge: serialize once for every caller that joined, then settle each."""
    passes = _PASSES.get(state, {}).get(audience)
    if passes is None or passes.waiters is None:
        return
    loop = audience[0]
    # One timer per pass, set again when a later caller moved the pass on, rather
    # than one timer per caller.
    due = trailing_due(passes.first_joined, passes.last_joined, passes.window)
    if due > passes.armed_for:
        passes.armed_for = due
        loop.call_at(due, _take_trailing, state, audience)
        return
    waiters = [future for future in passes.waiters if not future.done()]
    passes.waiters = None
    if not waiters:
        return
    rows: list[dict] = []
    failure: Exception | None = None
    started = loop.time()
    try:
        rows = _serialize(state, audience)
    except Exception as exc:
        failure = exc
    passes.ended(started, loop.time())
    for future in waiters:
        if failure is not None:
            future.set_exception(failure)
        else:
            future.set_result(rows)
