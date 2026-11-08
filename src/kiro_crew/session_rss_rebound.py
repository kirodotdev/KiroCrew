"""When the RSS recycle stops recycling a session that keeps climbing back.

``SessionCleanup._rss_threshold_check`` recycles an idle session whose process
tree is over ``session.watchdog_rss_max_mb``. The recycled session restores its
whole conversation on its next message, so when that history alone puts a fresh
process over the ceiling, every recycle is followed by another crossing, and the
session is recycled after every turn for as long as it is used. This module
keeps each key's record of its last recycle and judges every crossing against
it: a rebound inside ``RSS_REBOUND_WINDOW_SECS`` is logged once, the
``RSS_REBOUND_HOLD_AFTER``-th rebound in a row is held instead of recycled, and
the chat is told once which setting is the cause.

The hold is bounded twice. It ends with the record, ``RSS_REBOUND_WINDOW_SECS``
after the last recycle, and a held session that grows past
``RSS_REBOUND_HOLD_GROWTH_FACTOR`` times the footprint it was held at is
recycled anyway, because that growth is the session itself running away, which
the ceiling exists to stop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple

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


def prune_rss_recycles(records: dict[str, RssRecycle], now: float) -> None:
    """Drop every record older than ``RSS_REBOUND_WINDOW_SECS``, ending its hold."""
    for key, record in list(records.items()):
        if now - record.at > RSS_REBOUND_WINDOW_SECS:
            del records[key]


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
