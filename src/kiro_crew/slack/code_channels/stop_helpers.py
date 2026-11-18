"""A code channel's Stop reaches its helper agents and their sub-agents.

Slack's Stop and ``!stop`` (``routing._stop_bound``, which sets the stop fence
and then calls :func:`stop_channel`) stop the channel's own session, the running
turn of every helper agent the code-channel store records as that session's
(``store().workers_of``, written at each helper's birth; never the crew-log tree,
whose parent edge follows adoption) and the sub-agents those turns started.
Helpers are one level deep, so there are no grandchildren to walk. The reply
says how many were stopped; it names none of them.

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


async def _stop_one(
    state: Any, helper: Any, tally: Tally, *, escalate: bool = True, cancel_key: str = ""
) -> dict[str, Any]:
    """Stop *helper*'s turn; its answer (``{}`` when it raised). *cancel_key* pins it."""
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    key = str(helper.key)
    # Not stopped until its answer arrives: a call cancelled midway is said and
    # audited as an error, not counted.
    tally.refused.add(key)
    info = "error"
    result: dict[str, Any] = {}
    pin: dict[str, Any] = {"cancel_key": cancel_key} if cancel_key else {}
    try:
        result = await stop_slot_turn(state, helper, source="slack", escalate=escalate, **pin)
        if result.get("ok") is not False:
            tally.refused.discard(key)
            info = str(result.get("info") or "stopping")
    except Exception:
        logger.warning("code channel stop: helper %s not stopped", key, exc_info=True)
    finally:
        tally.helpers.append((key, info))
    return result


async def stop_one_helper(
    state: Any, helper: Any, *, escalate: bool = True, cancel_key: str = ""
) -> tuple[Tally, dict[str, Any]]:
    """Stop one helper as :func:`stop_helpers` stops each: its turn, then its sub-agents.

    The turn's stop starts first, with no await before it, so it stops the turn the
    caller admitted; the sweep follows (and runs even when the stop raised).
    """
    tally = Tally()
    answer = await _stop_one(state, helper, tally, escalate=escalate, cancel_key=cancel_key)
    await _cancel_all(state, [helper], tally)
    return tally, answer


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
        jobs: list[Awaitable[Any]] = [_stop_one(state, helper, tally) for helper in running]
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


def reply_text(session: bool, tally: Tally) -> str:
    """What the channel is told: how many were stopped ("Nothing running." for none)."""
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
        parts.append(f"{len(stopped)} {noun}")
    if tally.subagents:
        parts.append(f"{tally.subagents} sub-agent{'' if tally.subagents == 1 else 's'}")
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" and {parts[-1]}"
    return f"Stopped {total}: {listed}.{declined}"


async def stop_channel(
    orch: Any, binding: Any, sender_id: str, channel: str, *, source: str, say_idle: bool
) -> str:
    """The Stop after ``routing._stop_bound`` set its fence: stop, settle, audit, reply.

    Returns the channel's own session's stop info (``"idle"`` when it was not
    running). Routing's helpers are read off the module, where tests patch them.
    """
    from kiro_crew.slack.code_channels import routing

    state = routing._dashboard_state()
    slot = routing.bound_slot(state, binding) if binding is not None else None
    conductor = binding.session_key if binding is not None else ""
    # Read before the first await: a turn the channel's session starts during the
    # sweep (the person's next message) began after the press and is not stopped.
    pressed = state is not None and slot is not None and bool(getattr(slot, "running", False))
    tally = await stop_helpers(
        state,
        slot,
        conductor,
        own=lambda: _stop_channel_session(state, slot, sender_id, channel, source, pressed),
    )
    info = tally.own
    if info == "error":
        routing._audit(sender_id, source, "allowed", f"channel={channel} result=error")
    elif info == "idle" and not getattr(slot, "running", False):
        # Nothing to stop, but Slack never clears "processing" by itself. After
        # the stops, so a failure here cannot read as a stop that failed, and
        # only while still idle: a turn started meanwhile keeps its own status.
        from kiro_crew.slack.code_channels.status import settle_after_stop

        await settle_after_stop(channel)
        if binding is not None:
            # Stop set the status itself: the watch's record of what it showed is
            # stale, so its next pass shows the helper agents' state again.
            from kiro_crew.slack.code_channels.workspace import core as workspace

            service = workspace.service()
            if service is not None:
                service._forget_worker_status(binding)
    elif info == "compacting":
        await routing._post(orch, channel, routing.COMPACTING_STOP_TEXT, None)
    for key, result in tally.helpers:
        routing._audit(
            sender_id, source, "allowed", f"channel={channel} slot={key} result={result}"
        )
    own = info not in NOT_STOPPED and info not in ("idle", "error", "refused")
    # No await from here to the raw send: the sessions counted as left running
    # are the ones the board's scope admits then.
    tally.others = left_running(state, binding, tally)
    if not (own or tally.stopped or tally.subagents or tally.notes) and not (
        say_idle and info != "compacting"
    ):
        return info
    await routing._post(orch, channel, reply_text(own, tally), None)
    return info


async def _stop_channel_session(
    state: Any, slot: Any, sender_id: str, channel: str, source: str, pressed: bool
) -> str:
    """Stop the channel's own session; ``"idle"`` when it was not running at the press.

    *pressed* is whether it ran when Stop was pressed (read before any await).
    Only the stop and its audit row: what follows a stop (the status settle, the
    compaction notice) is :func:`stop_channel`'s, so its answer is never lost to them.
    """
    from kiro_crew.slack.code_channels import routing

    if not pressed or state is None or slot is None or not getattr(slot, "running", False):
        routing._audit(sender_id, source, "allowed", f"channel={channel} result=idle")
        return "idle"
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    # Each call is a person's own press: a redelivered ``!stop`` is answered from
    # ``inbound_gate``'s per-message memory and a redelivered native Stop is
    # dropped by its ``event_ts`` before reaching here, so a second call is a
    # second decision and may take the escalation (the compaction escape too).
    result = await stop_slot_turn(state, slot, source="slack", escalate=True)
    # A refused stop (``ok: False``, no info) stopped nothing.
    info = "refused" if result.get("ok") is False else str(result.get("info") or "stopping")
    routing._audit(sender_id, source, "allowed", f"channel={channel} slot={slot.key} result={info}")
    return info
