"""Where a MicroVM crew can be, and the only function that moves it.

A MicroVM crew is not a Fargate task. It has a hard wall clock the platform
enforces and a disk that disappears with the VM, so the positions a crew can be
left in are not a tidy happy path: a launch can fail after creating a VM, and a
crew that is up can stop answering without anything having moved it.

``PENDING``
    A launch is in flight. Kept rather than implied, because a launch that failed
    after creating a VM or an activation has left something behind, and the only
    way to find it is a record that says a launch happened.

``LAUNCH_FAILED``
    The launch could not be completed. Terminal, and kept rather than deleted for
    the same reason.

``TERMINATED``
    The VM is gone, and the crew's home went with it -- it lived on that VM's own
    disk and nothing copies it anywhere. Terminal.

:data:`EFFECTIVE_UNKNOWN` is derived and never stored, for the one thing a stored
state cannot express: the control plane has not heard from this crew recently
enough to repeat what it last said. A laptop that slept for an hour holds a record
saying ``RUNNING`` that is a guess, not an observation, and a product that reports
the guess as fact offers an "Open crew" button for a VM the wall already took.

:func:`transition` is the only writer. Every edge is in :data:`EDGES` and nothing
else may move a crew between states, so adding a path means adding a row that a
reviewer can read rather than finding the assignment that made it.
"""

from __future__ import annotations

from typing import Optional

#: A launch is in flight: the VM exists (or is being created) and nothing has
#: confirmed the guest answers yet.
PENDING = "pending"

#: The VM is up and the crew's gateway answered within the observation window.
RUNNING = "running"

#: The VM is gone. Terminal, and it is where every crew on this lane ends: the
#: home is on the VM's disk and nothing on this lane copies it off, so there is
#: no state a crew can be reopened from.
TERMINATED = "terminated"

#: The launch itself failed. Terminal, and kept rather than deleted because a
#: launch that failed after creating anything may have left a VM or an activation
#: behind, and a record is the only way to find it.
LAUNCH_FAILED = "launch_failed"

#: Every state a record may hold.
STORED_STATES: tuple[str, ...] = (
    PENDING,
    RUNNING,
    TERMINATED,
    LAUNCH_FAILED,
)

#: The states from which no event leads anywhere. Derived from :data:`EDGES`
#: rather than listed, so a new edge out of one of them stops it being terminal
#: without anyone remembering to edit a second list.
#:
#: Assigned below the table, since it reads it.

# ── Events ───────────────────────────────────────────────────────────────────

#: A launch has begun. The only event with no origin state.
EVENT_LAUNCH_STARTED = "launch_started"
#: The guest answered. Readiness is a separate predicate; this is reachability.
EVENT_ONLINE = "online"
#: The launch could not be completed.
EVENT_LAUNCH_FAILED = "launch_failed"
#: The VM is gone: the owner tore it down, or the platform took it at the wall.
EVENT_TERMINATED = "terminated"

#: Every event :func:`transition` accepts.
EVENTS: tuple[str, ...] = (
    EVENT_LAUNCH_STARTED,
    EVENT_ONLINE,
    EVENT_LAUNCH_FAILED,
    EVENT_TERMINATED,
)

#: ``(from state, event) -> to state``. The whole machine, and the only place a
#: path between two states is written down.
#:
#: ``None`` as the origin is the launch of a crew that has no record yet. It is a
#: key rather than a special case in :func:`transition`, so "where can a crew
#: start" is answered by reading this table like every other question about it.
EDGES: dict[tuple[Optional[str], str], str] = {
    (None, EVENT_LAUNCH_STARTED): PENDING,
    (PENDING, EVENT_ONLINE): RUNNING,
    (PENDING, EVENT_LAUNCH_FAILED): LAUNCH_FAILED,
    # A teardown of a crew that never came online is still a teardown: the VM may
    # exist even though no guest ever answered, so PENDING needs this edge as
    # much as RUNNING does.
    (PENDING, EVENT_TERMINATED): TERMINATED,
    (RUNNING, EVENT_TERMINATED): TERMINATED,
}

#: States with no outgoing edge. Read from :data:`EDGES` so it cannot drift.
TERMINAL_STATES: frozenset[str] = frozenset(
    state for state in STORED_STATES if not any(origin == state for origin, _ in EDGES)
)

#: The derived answer for a crew whose last observation is older than the caller's
#: staleness bound. NEVER stored: it is a statement about the control plane's
#: knowledge, not about the crew, and writing it would make the next reader think
#: the crew itself had moved.
EFFECTIVE_UNKNOWN = "unknown"

#: How old an observation may be before :func:`effective_state` stops repeating it.
#:
#: Three minutes: a laptop that slept, a gateway that was restarted and a crashed
#: process all land here, and all three mean the same thing to a reader -- nobody
#: has looked.
DEFAULT_STALE_AFTER_SECONDS = 180


class IllegalTransition(ValueError):
    """An event that :data:`EDGES` has no row for.

    Raised rather than ignored, and rather than coerced to the nearest sensible
    state. A caller asking a terminated crew to come online has a bug in the
    caller; answering it with ``RUNNING`` would record a VM that does not exist,
    and answering it with ``TERMINATED`` would hide the bug.
    """


def transition(current: Optional[str], event: str) -> str:
    """The state *current* moves to on *event*, or raise :class:`IllegalTransition`.

    The only writer of a crew's state. Pure: it reads the table and returns a
    string, so every caller stores the result itself and no hidden path can move a
    crew while this function is not looking.

    *current* is ``None`` for a crew with no record yet, which is a key in
    :data:`EDGES` rather than a branch here.
    """
    if event not in EVENTS:
        raise IllegalTransition(f"unknown event {event!r}")
    if current is not None and current not in STORED_STATES:
        raise IllegalTransition(f"unknown state {current!r}")
    try:
        return EDGES[(current, event)]
    except KeyError:
        raise IllegalTransition(
            f"a crew in state {current!r} cannot take event {event!r}: "
            f"{sorted(e for (s, e) in EDGES if s == current)} are the events it accepts"
        ) from None


def effective_state(
    stored: str,
    *,
    age_seconds: Optional[float],
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> str:
    """What to REPORT for a crew whose last observation is *age_seconds* old.

    Returns :data:`EFFECTIVE_UNKNOWN` for a live state nobody has confirmed
    recently, and the stored state otherwise. ``age_seconds`` of ``None`` means
    the crew has never been observed, which is as unknown as an old observation.

    A terminal state is reported as itself however old it is. Nothing can move a
    terminated crew, so an old observation of one is not a stale reading -- it is
    the answer, and degrading it to ``unknown`` would ask the owner to wait for
    news that will never come.
    """
    if stored in TERMINAL_STATES:
        return stored
    if age_seconds is None or age_seconds > stale_after_seconds:
        return EFFECTIVE_UNKNOWN
    return stored
