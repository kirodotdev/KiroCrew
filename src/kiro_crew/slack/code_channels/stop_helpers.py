"""A code channel's Stop reaches its helper agents and their sub-agents.

Slack's Stop and ``!stop`` (``routing._stop_bound``) stop the channel's own
session, the running turn of every helper agent the code-channel store records as
that session's (``store().workers_of``, written at each helper's birth; never the
crew-log tree, whose parent edge follows adoption) and the sub-agents those turns
started. Helpers are one level deep, so there are no grandchildren to walk.

This is the brake only: nothing is held afterwards. A message queued for a
stopped helper, its own AutoNudge or patrol, or a send from the channel's session
can start it again; holding it until a person writes is a follow-up change.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.slack.code_channels.store import StoreUnavailable, store

logger = logging.getLogger(__name__)

#: How many stopped helpers the reply names; the rest are counted.
NAMED = 5

#: Stop answers that stopped nothing (Pause all's rule). "no active turn" is not
#: one: the press still ends that turn at its dispatch gate.
NOT_STOPPED = frozenset({"not running", "compacting"})
#: Of those, the answer a later read may find out of date: the slot was idle at
#: its stop call, and a new turn can have started since.
_ENDED = frozenset({"not running"})


@dataclass
class Tally:
    """What one Stop stopped: the channel's own session's answer, then the rest."""

    own: str = "idle"  # the channel session's stop info ("idle" when it was not running)
    helpers: list[tuple[str, str]] = field(default_factory=list)  # (slot key, stop info)
    subagents: int = 0
    #: Helpers whose stop was refused (``ok: False``), raised or was cancelled: not stopped.
    refused: set[str] = field(default_factory=set)
    #: Sessions a sub-agent sweep failed for: it may have stopped some before it
    #: failed, so even a later sweep that works cannot say what is still running.
    unswept: set[str] = field(default_factory=set)
    #: Sessions on the work board still running that this Stop does not reach
    #: (filed in the channel's folder, not recorded as its helper agents).
    others: int = 0

    @property
    def stopped(self) -> list[str]:
        """The keys of the helpers whose turn really stopped."""
        return [
            key for key, info in self.helpers if info not in NOT_STOPPED and key not in self.refused
        ]

    @property
    def compacting(self) -> list[str]:
        """The helpers whose compaction declined the stop (a second press forces it)."""
        return [key for key, info in self.helpers if info == "compacting"]

    @property
    def failed(self) -> bool:
        """Whether a stop was refused or raised, the channel's own included: the reply says so."""
        return bool(self.refused or self.unswept) or self.own in ("error", "refused")

    @property
    def notes(self) -> bool:
        """Whether the reply has something to say beyond what stopped."""
        return self.failed or bool(self.compacting) or self.others > 0


def _helper_keys(conductor_key: str) -> list[str]:
    """Every helper key the store records for *conductor_key* (in memory once loaded)."""
    if not conductor_key:
        return []
    try:
        keys = store().workers_of(conductor_key)
    except (StoreUnavailable, OSError):
        logger.warning("code channel stop: helper records unreadable", exc_info=True)
        return []
    return sorted(set(keys) - {conductor_key})


def _live(state: Any, keys: list[str]) -> list[Any]:
    get_slot = getattr(state, "get_slot", None)
    if not callable(get_slot):
        return []
    return [slot for slot in (get_slot(key) for key in keys) if slot is not None]


async def _cancel_subagents(state: Any, slot: Any) -> int | None:
    """How many of *slot*'s sub-agents were stopped; None when the sweep failed."""
    from kiro_crew.dashboard.chat_utils import effective_session_key

    subagents = getattr(state, "subagents", None)
    if subagents is None:
        return 0
    try:
        running, queued = await subagents.cancel_for_parent(effective_session_key(slot))
    except Exception:
        logger.warning("code channel stop: sub-agents of %s not stopped", slot.key, exc_info=True)
        return None
    return int(running) + int(queued)


async def _cancel_all(state: Any, slots: list[Any], tally: Tally) -> None:
    slots = [s for s in slots if s is not None]
    # A sweep cancelled from inside (not this Stop) is a failed sweep, not a lost Stop.
    counts = await asyncio.gather(
        *(_cancel_subagents(state, s) for s in slots), return_exceptions=True
    )
    for slot, count in zip(slots, counts):
        if not isinstance(count, int):
            tally.unswept.add(str(slot.key))
        else:
            tally.subagents += count


async def _stop_one(state: Any, helper: Any, tally: Tally) -> None:
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    key = str(helper.key)
    # Not stopped until its answer arrives: a call cancelled midway is said and
    # audited as an error, not counted.
    tally.refused.add(key)
    info = "error"
    try:
        result = await stop_slot_turn(state, helper, source="slack", escalate=True)
        if result.get("ok") is not False:
            tally.refused.discard(key)
            info = str(result.get("info") or "stopping")
    except Exception:
        logger.warning("code channel stop: helper %s not stopped", key, exc_info=True)
    finally:
        tally.helpers.append((key, info))


async def _stop_own(own: Callable[[], Awaitable[str]], tally: Tally) -> None:
    tally.own = "error"  # until its answer arrives, as for a helper
    try:
        tally.own = await own()
    except Exception:
        logger.warning("code channel stop: the channel's own session not stopped", exc_info=True)


async def stop_helpers(
    state: Any,
    channel_slot: Any,
    conductor_key: str,
    own: Callable[[], Awaitable[str]] | None = None,
) -> Tally:
    """Stop the channel's session (*own*), each running helper of *conductor_key*, and sub-agents.

    A press is a person's decision, so a second one escalates (``escalate=True``),
    as the channel's own Stop does. A helper on a tool approval has it rejected
    by ``stop_slot_turn``. A helper whose turn ended meanwhile answers "not
    running" and is not counted; the second read stops it if its slot has
    started another turn since.

    The turns are stopped together, so no helper waits out another's soft-stop
    budget. Sub-agents are cancelled before their parents' turns: a parent's hard
    Stop stops the ones sharing its process itself, and those would go uncounted.
    The helper records are read again after the stops, so a helper that was born
    or started during the Stop is stopped too, and sub-agents are cancelled once
    more at the end for any those turns started while being stopped.
    """
    tally = Tally()
    known: dict[str, Any] = {}
    for first in (True, False):
        helpers = _live(state, _helper_keys(conductor_key))
        known.update((str(h.key), h) for h in helpers)
        # A helper whose turn had ended by its stop call may have started another.
        tried = tally.refused | {key for key, info in tally.helpers if info not in _ENDED}
        running = [h for h in helpers if getattr(h, "running", False) and str(h.key) not in tried]
        await _cancel_all(state, [channel_slot, *helpers] if first else running, tally)
        jobs = [_stop_one(state, helper, tally) for helper in running]
        if first and own is not None:
            jobs.insert(0, _stop_own(own, tally))
        # A stop call cancelled from inside leaves its job's answer "not stopped".
        await asyncio.gather(*jobs, return_exceptions=True)
    await _cancel_all(state, [channel_slot, *known.values()], tally)
    tally.helpers.sort()
    return tally


def left_running(state: Any, binding: Any, tally: Tally) -> int:
    """How many sessions on the work board still run that this Stop did not reach.

    The board also watches the sessions filed in the channel's folder; those are
    not the channel's helper agents (``store().workers_of``), so the Stop leaves
    them alone and the reply says so. ``0`` when the board's scope refuses the
    channel. Synchronous, read right before the reply's send.
    """
    from kiro_crew.slack.code_channels.workspace import core as workspace

    try:
        service = workspace.service()
        watched = (
            service.watched_sessions(binding)
            if service is not None and binding is not None
            else set()
        )
    except Exception:
        return 0
    # Its own helper agents are the Stop's, stopped or idle at its reads.
    helpers = {key.removeprefix("dashboard_") for key, _info in tally.helpers}
    helpers |= set(_helper_keys(str(getattr(binding, "session_key", "") or "")))
    return sum(1 for s in _live(state, sorted(watched - helpers)) if getattr(s, "running", False))


def board_titles(binding: Any) -> dict[str, str]:
    """Slot key -> work-board title of the helpers the board may show in this channel.

    Empty when the board's scope refuses the channel: the reply names no helper
    the board would withhold. Synchronous, read right before the reply's send.
    """
    from kiro_crew.slack.code_channels.workspace import core as workspace

    try:
        service = workspace.service()
        if service is None or binding is None or not service._board_still_admitted(binding):
            return {}
        board = service.board(binding.team_id, binding.channel_id, binding.session_key)
    except Exception:
        return {}
    titles: dict[str, str] = {}
    for row in getattr(board, "rows", ()) or ():
        key = str(getattr(row, "session_key", "") or "").removeprefix("dashboard_")
        if key and row.title:
            titles[key] = titles[f"dashboard_{key}"] = row.title
    return titles


def reply_text(
    session: bool,
    tally: Tally,
    *,
    titles: dict[str, str] | None = None,
    link_for: Any = None,
) -> str:
    """What the channel is told: how many were stopped ("Nothing running." for none).

    *titles* are the work board's titles of the helpers it may show (``{}`` when
    its scope refuses the channel); each stopped one with a title is named,
    linked to its chat when a link can be built (the board's own links, never a
    token), up to :data:`NAMED`.
    """
    from kiro_crew.slack.code_channels.board.render import _escape, _link, _linkable

    stopped = tally.stopped
    total = int(session) + len(stopped) + tally.subagents
    compacting = len(tally.compacting)
    declined = (
        f" {compacting} helper agent{' is' if compacting == 1 else 's are'} compacting "
        f"context, so Stop was not applied to {'it' if compacting == 1 else 'them'}. "
        "Stop again to force it."
        if compacting
        else ""
    )
    refused = len(tally.refused)
    if tally.own in ("error", "refused"):
        declined += " This channel's session could not be stopped."
    if refused:
        declined += f" {refused} helper agent{'' if refused == 1 else 's'} could not be stopped."
    unswept = len(tally.unswept)
    if unswept:
        declined += (
            f" Stopping the sub-agents of {unswept} session{'' if unswept == 1 else 's'} "
            "failed partway, so some may still be running."
        )
    if tally.others:
        many = tally.others != 1
        declined += (
            f" {tally.others} other session{'s' if many else ''} on the work board "
            f"{'are' if many else 'is'} still running: Stop reaches only this channel's "
            "session and its helper agents."
        )
    if not total:
        return declined.strip() or "Nothing running."
    parts: list[str] = []
    if session:
        parts.append("this channel's session")
    if stopped:
        noun = "helper agent" if len(stopped) == 1 else "helper agents"
        named = [(key, (titles or {}).get(key, "")) for key in stopped]
        names = []
        for key, title in [(k, t) for k, t in named if t][:NAMED]:
            url = link_for(key) if callable(link_for) else ""
            names.append(_link(url, title) if _linkable(url) else f"*{_escape(title)}*")
        parts.append(f"{len(stopped)} {noun}" + (f" ({', '.join(names)})" if names else ""))
    if tally.subagents:
        parts.append(f"{tally.subagents} sub-agent{'' if tally.subagents == 1 else 's'}")
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" and {parts[-1]}"
    return f"Stopped {total}: {listed}.{declined}"
