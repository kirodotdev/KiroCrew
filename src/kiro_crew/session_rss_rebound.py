"""When the RSS recycle leaves a session that is over the ceiling running.

``SessionCleanup._rss_threshold_check`` recycles an idle session whose process
tree is over ``session.watchdog_rss_max_mb``. The recycled session restores its
whole conversation on its next message, so when that history or the session's
work puts each fresh process back over the ceiling, every recycle is followed
by another crossing, and the session is recycled again and again for as long as
it is used. This module keeps each key's record of its last recycle and judges
every crossing against it: a rebound inside ``RSS_REBOUND_WINDOW_SECS`` is logged once, the
``RSS_REBOUND_HOLD_AFTER``-th rebound in a row is held instead of recycled, and
the chat is told once which setting is the cause.

The hold is bounded twice. It ends with the record, ``RSS_REBOUND_WINDOW_SECS``
after the last recycle, and a held session that grows past
``RSS_REBOUND_HOLD_GROWTH_FACTOR`` times the footprint it was held at is
recycled anyway, because that growth is the session itself running away, which
the ceiling exists to stop.

A runtime that is already over the ceiling when the watchdog first reads it has
not grown past anything: with enough MCP servers a fresh process is that large
before its first turn ends, and a recycle only buys the same process again
after a cold start. ``judge_rss_reading`` therefore keeps one footprint per
runtime, taken at the second tick that reads it because the first can catch the
servers still starting. A footprint over the ceiling holds the runtime, with no
recycle and no notice, until its tree passes ``RSS_REBOUND_HOLD_GROWTH_FACTOR``
times that footprint. A runtime whose footprint is under the ceiling, or that
has outgrown its hold, is judged on every crossing by the rebound rule above.

The footprint is for a chat the watchdog has not recycled. While a key has a
recycle record, its runtime takes no footprint: a fresh process that is back
over the ceiling is a rebound, recycled as before and held on the third in a
row with the notice that names the setting. So a held runtime that passes twice
its footprint is recycled once, and that chat is then judged by the rebound
rule alone until its record ends. A runtime first read while its key had a
record never takes a footprint afterwards either: when the record ends, the
ceiling applies to it, as it did before footprints existed.

The footprint is a start-up figure, so a first reading under the ceiling counts
as start-up only for ``RSS_FOOTPRINT_SETTLE_TICKS`` of the watchdog's longest
tick. A runtime read under the ceiling and next read over it later than that,
say after one long turn, was read under the ceiling: that is growth, and it is
recycled.

Only idle sessions are read, so both readings come after the runtime's first
turn. A reading during that turn cannot stand in for them. A chat with many MCP
servers climbs to its size while that turn runs and for a while after it: its
servers start during the turn (measured on Windows, a first reply takes 15 to
25 s at 57 to 93 local servers), and two chats with 93 local servers were read
at 5.8 and 6.4 GB, 36 and 48 s after their first reply, on the way to 10.6 GB.
A reading taken while the session is busy lands somewhere on that climb. Under
the ceiling, it would record a chat that is large by design as read under it,
and the first idle reading would then recycle that chat, which is the loop the
footprint ends. So no reading tells a chat that is large by design from one its
first turn made large; both are held at the size taken and stopped at twice it.
That is the limit of this rule: a runtime whose first reading is already over
the ceiling is held at its second, however late that one comes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import NamedTuple, Protocol

#: How long after an RSS recycle a session crossing the ceiling again counts as
#: the replacement climbing straight back. A recycled session restarts its
#: process on its next message, which can be hours after an idle recycle, so
#: the window spans a day. A crossing inside it suggests the ceiling sits below
#: the workload's steady footprint rather than one session leaking.
RSS_REBOUND_WINDOW_SECS = 86400.0

#: The rebound, counted in a row, that is held instead of recycled. By the third
#: one, two recycles have each been followed by a fresh process going straight
#: back over the ceiling, so another recycle only costs the user one more cold
#: start. Not the first: one rebound can be a session that grew back through
#: real work inside the day.
RSS_REBOUND_HOLD_AFTER = 3

#: A held session is still recycled once its tree passes this multiple of the
#: RSS it was held at. Relative to that footprint rather than to the ceiling:
#: the hold exists because the footprint is already over the ceiling, and a
#: fixed multiple of the ceiling would sit below a footprint over twice it, so
#: the hold would never take and the loop would go on.
RSS_REBOUND_HOLD_GROWTH_FACTOR = 2


class RssRecycle(NamedTuple):
    """One session key's last RSS recycle, kept for ``RSS_REBOUND_WINDOW_SECS``."""

    #: Monotonic time of the recycle.
    at: float
    #: Tree RSS in MiB the session was recycled at.
    rss: int
    #: Rebounds in a row that led to this recycle; 0 for a first crossing.
    rebounds: int = 0
    #: The crossing after this recycle has been logged as a rebound.
    warned: bool = False
    #: Tree RSS in MiB the session was held at, or 0 while it is not held.
    held_mb: int = 0


@dataclass(frozen=True, slots=True)
class RssHold:
    """The figures the chat is shown when the RSS recycle holds its session."""

    rss_mb: int
    ceiling_mb: int
    #: The tree RSS above which the held session is recycled anyway.
    limit_mb: int
    #: Recycles in a row that each came straight back over the ceiling.
    recycles: int


#: How many of the watchdog's longest ticks after a first reading UNDER the
#: ceiling the next reading still counts as the runtime's start-up. One busy tick
#: is allowed, two are not: the next reading is the tick after the first one, or
#: the one after that when the session was busy then. Two ticks and a half, not
#: two, because a tick starts one interval after the last one ENDED: readings
#: come 302 s apart at a 300 s interval (measured on Linux), so two ticks are
#: 604 s and three are 906 s. A runtime read under the ceiling longer ago than
#: this has been read under it, so a later reading over the ceiling is growth,
#: however long a turn hid it. Counted in the longest tick, which is the default
#: one; a shorter tick fits more readings into the same time.
RSS_FOOTPRINT_SETTLE_TICKS = 2.5


class RssFootprint(NamedTuple):
    """One runtime's tree as the watchdog found it, kept while the runtime lives."""

    #: Monotonic time of the tick that first read the runtime.
    seen_at: float
    #: Tree RSS in MiB at that first reading.
    seen_mb: int = 0
    #: Tree RSS in MiB the runtime settled at, when that was over its ceiling;
    #: 0 when it settled under the ceiling, or has since outgrown the hold.
    held_mb: int = 0
    #: A tick after ``seen_at`` has read the runtime, so ``held_mb`` is final.
    settled: bool = False


class RssRecords(Protocol):
    """The two records the RSS recycle keeps, as ``CleanupState`` holds them."""

    rss_recycled: dict[str, RssRecycle]
    rss_footprints: dict[int, RssFootprint]


def prune_rss_records(state: RssRecords, now: float, live_pids: Collection[int]) -> None:
    """Drop each recycle older than the window and each footprint of a gone runtime.

    A recycle record ends its hold with it. A footprint belongs to one process,
    so it goes when no session names that pid any more: the fresh process a
    recycled session starts is read from nothing.
    """
    records = state.rss_recycled
    for key, record in list(records.items()):
        if now - record.at > RSS_REBOUND_WINDOW_SECS:
            del records[key]
    footprints = state.rss_footprints
    for pid in [pid for pid in footprints if pid not in live_pids]:
        del footprints[pid]


def judge_rss_reading(
    state: RssRecords,
    key: str,
    pid: int,
    rss: int,
    ceiling: int,
    *,
    now: float,
    tick: float,
    logger: logging.Logger,
    notify: Callable[[str, RssHold], None] | None,
) -> int | None:
    """Judge one tick's reading of *key*'s runtime *pid*: *rss* MiB against *ceiling*.

    Answers ``None`` when the session is left running, else the rebound count
    to record if the recycle goes ahead. The first tick that reads a runtime
    only notes it, and the next one takes its footprint: over the ceiling, the
    runtime is held until it passes ``RSS_REBOUND_HOLD_GROWTH_FACTOR`` times
    that footprint; under it, every later crossing is growth. A first reading
    under the ceiling makes the next one growth too once it is older than
    ``RSS_FOOTPRINT_SETTLE_TICKS`` times *tick*, the watchdog's longest tick
    interval in seconds. Growth goes to
    ``judge_rss_crossing``, and so does every crossing by a key with a recycle
    record: a chat the watchdog recycled inside the window takes no footprint.
    A reading of 0 is an unreadable tree and settles nothing. Sessions sharing
    one runtime are read in the same tick with the same *now*, so the footprint
    moves once per tick, not once per session.
    """
    over = rss > ceiling
    footprints = state.rss_footprints
    if key in state.rss_recycled:
        # Recycled inside the window: a fresh process back over the ceiling is a
        # rebound, never a footprint, so the third in a row is held with its notice.
        # A runtime first read here is one the ceiling judges for as long as it
        # lives, also once the record ends.
        if rss > 0:
            footprints.setdefault(pid, RssFootprint(now, rss, settled=True))
        if not over:
            return None
        return judge_rss_crossing(
            state.rss_recycled, key, rss, ceiling=ceiling, now=now, logger=logger, notify=notify
        )
    footprint = footprints.get(pid)
    if footprint is None:
        if rss > 0:
            footprints[pid] = RssFootprint(now, rss)
            if over:
                logger.info(
                    "RSS footprint: session %s runtime %d tree rss=%dMB is above %dMB "
                    "on its first reading; its footprint is taken on the next tick",
                    key,
                    pid,
                    rss,
                    ceiling,
                )
        return None
    if not footprint.settled:
        if now == footprint.seen_at or rss <= 0:
            return None
        # Both readings are idle ones, after the first turn: a reading during that
        # turn would catch a many-server chat under the ceiling (module docstring).
        # A first reading under the ceiling is start-up only while it is recent.
        waited = now - footprint.seen_at
        started_under = footprint.seen_mb <= ceiling
        grew = started_under and waited > RSS_FOOTPRINT_SETTLE_TICKS * tick
        held = over and not grew
        footprint = footprint._replace(held_mb=rss if held else 0, settled=True)
        footprints[pid] = footprint
        if held:
            how = "without having been read under it"
            if started_under:
                how = (
                    f"{waited:.0f}s after a first reading of {footprint.seen_mb}MB, "
                    "which counts as its start-up"
                )
            logger.warning(
                "RSS footprint: session %s runtime %d tree rss=%dMB settled above "
                "%dMB %s; that is this workload's own size, so it is left running "
                "unless it passes %dMB",
                key,
                pid,
                rss,
                ceiling,
                how,
                rss * RSS_REBOUND_HOLD_GROWTH_FACTOR,
            )
    if not over:
        return None
    if footprint.held_mb:
        if rss <= footprint.held_mb * RSS_REBOUND_HOLD_GROWTH_FACTOR:
            return None
        # Outgrown: the ceiling applies from here on, also when a guard declines this recycle.
        footprints[pid] = footprint._replace(held_mb=0)
    return judge_rss_crossing(
        state.rss_recycled, key, rss, ceiling=ceiling, now=now, logger=logger, notify=notify
    )


def judge_rss_crossing(
    records: dict[str, RssRecycle],
    key: str,
    rss: int,
    *,
    ceiling: int,
    now: float,
    logger: logging.Logger,
    notify: Callable[[str, RssHold], None] | None,
) -> int | None:
    """Judge one crossing of *ceiling* by *key*'s tree at *rss* MiB.

    Answers the rebound count to record if the recycle goes ahead, 0 when there
    is no recent recycle, or ``None`` when the session is held and must be left
    running. The record is updated in place, never removed, so a crossing the
    later guards decline keeps both the count and the one warning per cycle.
    The transition into a hold logs one WARNING and calls *notify* once; a
    callback that raises is logged and does not stop the tick.
    """
    previous = records.get(key)
    if previous is None:
        return 0
    rebounds = previous.rebounds + 1
    if previous.held_mb:
        if rss <= previous.held_mb * RSS_REBOUND_HOLD_GROWTH_FACTOR:
            return None
        return rebounds
    if rebounds >= RSS_REBOUND_HOLD_AFTER:
        limit = rss * RSS_REBOUND_HOLD_GROWTH_FACTOR
        records[key] = previous._replace(warned=True, held_mb=rss)
        logger.warning(
            "RSS recycle held: session %s tree rss=%dMB is back above %dMB after %d "
            "recycles in a row; it is left running unless it passes %dMB, because "
            "session.watchdog_rss_max_mb sits below this workload's footprint",
            key,
            rss,
            ceiling,
            rebounds,
            limit,
        )
        if notify is not None:
            try:
                notify(key, RssHold(rss, ceiling, limit, rebounds))
            except Exception:
                logger.debug("on_rss_hold callback failed", exc_info=True)
        return None
    if not previous.warned:
        records[key] = previous._replace(warned=True)
        logger.warning(
            "RSS recycle rebound: session %s tree rss=%dMB is back above "
            "%dMB %.0fs after a recycle at %dMB; "
            "session.watchdog_rss_max_mb may be set too low for this workload",
            key,
            rss,
            ceiling,
            now - previous.at,
            previous.rss,
        )
    return rebounds


def rss_recycle_reason(rss: int, ceiling: int, rebounds: int) -> str:
    """The reason the recycle notice shows, naming the setting on a rebound."""
    if not rebounds:
        return f"memory limit ({rss}MB)"
    return (
        f"memory limit ({rss}MB), over session.watchdog_rss_max_mb of {ceiling}MB "
        "again since its last recycle"
    )
