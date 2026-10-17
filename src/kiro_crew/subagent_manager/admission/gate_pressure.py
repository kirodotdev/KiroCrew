"""The macOS kernel memory-pressure hold: whether it applies now, and what it does with a start.

The hold's rules are stated once, in subagent.md (*macOS: the kernel memory-pressure
hold*); this module is their implementation. ``_memory_pressure_hold_impl`` reads the
level and keeps the gateway-wide episode, ``_memory_pressure_holds_impl`` decides one root
start (held or expired), and the two clock helpers keep each held row's lane progress.
``spawn_impl``, the pump's pick and the run's own release all ask it.

A function here whose first parameter is ``self`` is a ``_GateMixin`` method: the class
binds it under the same name in ``admission/gate.py``. Each ends in ``_impl``, so
``bind_component_globals`` rebinds it onto ``kiro_crew.subagent``'s globals: the names it
reads unqualified are that module's, imported here for typing only.

The manager fields this module uses. Writes: ``_pressure_episode_read_at``,
``_pressure_episode_since``, ``_pressure_episode_spent``, ``_pressure_hold_level``,
``_pressure_hold_on``, ``_pressure_recheck_handle``, ``_pressure_holds``,
``_pressure_hold_expired``, ``_pressure_hold_lanes``, ``_pressure_lane_held``,
``_pressure_lane_started_at``. Reads: those, and ``_admission``, ``_agents``,
``_claim_prices``, ``_queue``, ``_queue_wait``, ``_shutting_down``, ``_drain_queue``,
``_emit_queue_depth``, ``_forget_pressure_clock``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio
    import time

    from ...subagent import (
        _PRESSURE_EPISODE_MAX_GAP_SECS,
        _PRESSURE_HOLD_PRUNE_FACTOR,
        DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS,
        MEMORY_PRESSURE_RECHECK_SECS,
        QUEUED_REASON_CONCURRENCY_LIMIT,
        QUEUED_REASON_MEMORY_PRESSURE,
        SEL_MEMORY_PRESSURE_NEVER_STARTED,
        _owns_dedicated_runtime,
        _spawn_memory_floor_and_cost,
        logger,
        platform_compat,
        pressure_level_held,
        read_memory_pressure_level,
        sel,
    )
    from .gate import _GateMixin


def _memory_pressure_hold_impl(self: _GateMixin, *, floor_gb: float | None = None) -> int | None:
    """The level the macOS kernel memory-pressure hold applies at now, or None.

    Gateway-wide and read fresh; the rules are subagent.md's (*macOS: the
    kernel memory-pressure hold*). The level is read first, so a host where
    it is never held (every non-macOS one) pays no config read. While the
    hold applies, one timer re-pumps every ``MEMORY_PRESSURE_RECHECK_SECS``.
    When it stops applying, the WARNING latch resets and a parent still
    labelled with the pressure reason is relabelled as a capacity wait. A
    hold that has applied without a break for the live
    ``agent.subagent_queue_max_wait_secs`` (``taskq_memory_wait_bound_secs``;
    0 is no bound) marks its episode spent (``_pressure_episode_spent``)
    until it stops applying, and every row the hold would keep then expires
    at once.
    """
    mgr = self._manager
    level: int | None = None
    try:
        reading = read_memory_pressure_level()
        if pressure_level_held(reading):
            if floor_gb is None:
                floor_gb, _cost = _spawn_memory_floor_and_cost()
            if floor_gb > 0 and _owns_dedicated_runtime(
                mgr._agents.values(), claim_prices=mgr._claim_prices
            ):
                level = reading
        # The episode is the hold APPLYING without a break, so a host at WARN
        # with nothing of ours running never spends it. A gap between reads
        # longer than a few recheck intervals is a break nobody observed, so
        # the episode restarts rather than being assumed continuous across it.
        now = time.monotonic()
        bound = mgr._admission.taskq_memory_wait_bound_secs()
        unobserved = now - mgr._pressure_episode_read_at > _PRESSURE_EPISODE_MAX_GAP_SECS
        mgr._pressure_episode_read_at = now
        since = mgr._pressure_episode_since
        if level is None or unobserved or since is None:
            mgr._pressure_episode_since = now if level is not None else None
            mgr._pressure_episode_spent = False
        elif bound > 0 and now - since >= bound:
            if not mgr._pressure_episode_spent:
                mgr._pressure_episode_spent = True
                logger.warning(
                    "macOS memory pressure has held subagent starts for %.0fs; root "
                    "starts it would hold are ended, never started, until it eases",
                    now - since,
                )
        else:
            # The bound is live: one raised past the episode, or set to 0 (no
            # bound), un-spends an episode spent under the earlier value.
            mgr._pressure_episode_spent = False
    except Exception:
        logger.debug("Subagent memory-pressure hold: reading failed", exc_info=True)
        level = None
    if level is None:
        mgr._pressure_hold_level = None
        if mgr._pressure_hold_on:
            mgr._pressure_hold_on = False
            for parent, wait in list(mgr._queue_wait.items()):
                if wait.get("reason") == QUEUED_REASON_MEMORY_PRESSURE:
                    mgr._emit_queue_depth(parent, wait={"reason": QUEUED_REASON_CONCURRENCY_LIMIT})
        # A row's clock outlives a pause in the hold (our last runtime ending
        # between two of its starts), so only clocks far past any wait are
        # dropped here: rows that left without a registration or a refusal.
        now = time.monotonic()
        prune_after = _PRESSURE_HOLD_PRUNE_FACTOR * max(
            mgr._admission.taskq_memory_wait_bound_secs(),
            float(DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS),
        )
        # A row still in the window is waiting, so its first-hold clock is
        # never dropped; nor is the clock of a row whose lane has started a
        # root within the window, since a row waiting its turn behind a
        # moving lane can rightly outlive it. Dropping either would re-stamp
        # a live row and make its expiry audit under-report the wait.
        in_window = {str(p.get("_preassigned_id") or "") for p in mgr._queue}
        for agent_id, since in list(mgr._pressure_holds.items()):
            if agent_id in in_window:
                continue
            lane = mgr._pressure_hold_lanes.get(agent_id)
            lane_started = mgr._pressure_lane_started_at.get(lane) if lane is not None else None
            last_seen = since if lane_started is None else max(since, lane_started)
            if now - last_seen >= prune_after:
                self._manager._forget_pressure_clock(agent_id)
        return None
    mgr._pressure_hold_on = True
    if not mgr._shutting_down:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        handle = mgr._pressure_recheck_handle
        # Re-armed when none is pending, including one whose loop went away
        # before it fired (past due by more than a second).
        if loop is not None and (
            handle is None or handle.cancelled() or handle.when() < loop.time() - 1.0
        ):

            def _recheck() -> None:
                mgr._pressure_recheck_handle = None
                mgr._drain_queue()

            mgr._pressure_recheck_handle = loop.call_later(MEMORY_PRESSURE_RECHECK_SECS, _recheck)
    return level


def _pressure_lane_started_impl(
    self: _GateMixin, parent_session_key: str, started_at: float
) -> None:
    """A root start from *parent_session_key* began at *started_at*.

    That is progress for every row the pressure hold keeps waiting in the
    same lane: the row's bound counts from this stamp when it is later than
    its first hold. A lane holding no row keeps no stamp, so the map has an
    entry only while its lane has a held row, and the entry leaves with the
    lane's last held row (``_forget_pressure_clock``). A lane
    joined after this start is clocked from its own first hold, which is
    later anyway.
    """
    from kiro_crew.taskq import lanes as _lanes

    mgr = self._manager
    lane = _lanes.lane_key_for(parent_session_key)
    if mgr._pressure_lane_held.get(lane):
        mgr._pressure_lane_started_at[lane] = started_at


def _forget_pressure_clock_impl(self: _GateMixin, agent_id: str) -> None:
    """Drop *agent_id*'s pressure-hold clock and its place in its lane.

    When it was the lane's last held row, the lane's progress stamp goes
    with it: nothing waits behind that lane's starts any more.
    """
    mgr = self._manager
    mgr._pressure_holds.pop(agent_id, None)
    mgr._pressure_hold_expired.discard(agent_id)
    lane = mgr._pressure_hold_lanes.pop(agent_id, None)
    if lane is None:
        return
    left = mgr._pressure_lane_held.get(lane, 0) - 1
    if left > 0:
        mgr._pressure_lane_held[lane] = left
    else:
        mgr._pressure_lane_held.pop(lane, None)
        mgr._pressure_lane_started_at.pop(lane, None)


def _memory_pressure_holds_impl(
    self: _GateMixin,
    agent_id: str,
    level: int,
    *,
    parent_session_key: str = "",
    batch_id: str = "",
    available_gb: float | None = None,
    relabel: bool = False,
    commit_expiry: bool = True,
) -> str:
    """What the hold, applying at *level*, does with root start *agent_id*.

    ``"held"`` while it waits; ``"expired"`` once its own wait has run out
    (``agent.subagent_queue_max_wait_secs`` from the later of its first hold
    and its lane's last root start, read live; 0 is no bound) or the episode has
    outlived that bound (``_pressure_episode_spent``): the caller ends it,
    never started (``MEMORY_PRESSURE_NEVER_STARTED``). *available_gb* is the
    floor's figure when the caller read one (negative: unreadable), None
    when it read none. *relabel* publishes the pressure label on the row's
    first hold, for a caller (the pump) that is not about to emit one.
    *commit_expiry* False only classifies an expired row (the pump's pick,
    which hands it to the gate's re-check): the WARNING and SEL row are
    written by the caller that actually ends it.
    """
    mgr = self._manager
    now = time.monotonic()
    first = agent_id not in mgr._pressure_holds
    since = mgr._pressure_holds.setdefault(agent_id, now)
    lane = mgr._pressure_hold_lanes.get(agent_id)
    if lane is None:
        from kiro_crew.taskq import lanes as _lanes

        # The lane this row waits in: its parent session's, the one the
        # pump's weighted pick serves FIFO. A held row is a root, so its
        # lane is its own parent key (no parent chain to walk).
        lane = _lanes.lane_key_for(parent_session_key)
        mgr._pressure_hold_lanes[agent_id] = lane
        mgr._pressure_lane_held[lane] = mgr._pressure_lane_held.get(lane, 0) + 1
    name = platform_compat.memory_pressure_name(level)
    bound = mgr._admission.taskq_memory_wait_bound_secs()
    # The bound counts time with no root start in this row's lane: from the
    # later of its first hold and the lane's last root start. A start in the
    # lane is a row ahead of this one leaving the FIFO queue, whichever
    # spawn_run wave it came from, so the row is waiting its turn, not
    # stuck. Another lane's starts never extend it (automation keys share
    # ``SYSTEM_LANE``, one FIFO queue, so they are one lane). No stamp is
    # its own case, never a 0.0 sentinel: the
    # monotonic clock can itself be smaller than the bound (a freshly
    # booted host).
    lane_started = mgr._pressure_lane_started_at.get(lane)
    stalled_since = since if lane_started is None else max(since, lane_started)
    own_wait_ran_out = bound > 0 and now - stalled_since >= bound
    if own_wait_ran_out or mgr._pressure_episode_spent:
        if commit_expiry and agent_id not in mgr._pressure_hold_expired:
            mgr._pressure_hold_expired.add(agent_id)
            # Which bound ended it is part of the audit: a start the spent
            # episode ends at once waited nothing itself, and reporting it as
            # one that sat out its own bound would misstate both.
            cause = "wait" if own_wait_ran_out else "episode"
            episode_since = mgr._pressure_episode_since
            episode_secs = (
                round(now - episode_since, 1)
                if cause == "episode" and episode_since is not None
                else None
            )
            if cause == "wait":
                logger.warning(
                    "Subagent %s: never started -- macOS reported memory pressure "
                    "(%s) for its whole wait (%.0fs)",
                    agent_id,
                    name,
                    now - since,
                )
            else:
                logger.warning(
                    "Subagent %s: never started -- macOS memory pressure (%s) has "
                    "lasted %.0fs, past the %ds bound, so it was ended without waiting",
                    agent_id,
                    name,
                    episode_secs or 0.0,
                    int(bound),
                )
            expiry: dict[str, Any] = {
                "memory_pressure_level": level,
                "waited_secs": round(now - since, 1),
                "expired_by": cause,
                "subagent_id": agent_id,
            }
            if episode_secs is not None:
                expiry["episode_secs"] = episode_secs
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome=SEL_MEMORY_PRESSURE_NEVER_STARTED,
                metadata=expiry,
            )
        return "expired"
    if first:
        log = logger.debug
        if mgr._pressure_hold_level != level:
            mgr._pressure_hold_level = level
            log = logger.warning
        metadata: dict[str, Any] = {"memory_pressure_level": level, "subagent_id": agent_id}
        figure = ""
        if available_gb is not None:
            unreadable = available_gb < 0
            metadata["available_gb"] = None if unreadable else available_gb
            figure = (
                " (figure unreadable)" if unreadable else f" ({available_gb:.2f} GB reclaimable)"
            )
        log(
            "Subagent %s held: macOS reports memory pressure (%s) while a dedicated "
            "subagent of this gateway is running%s",
            agent_id,
            name,
            figure,
        )
        sel().log_tool_invocation(
            session_key=parent_session_key or "",
            source="subagent",
            tool_name="spawn_run",
            outcome="deferred_memory_pressure",
            metadata=metadata,
        )
        if relabel:
            mgr._emit_queue_depth(
                parent_session_key, batch_id, wait={"reason": QUEUED_REASON_MEMORY_PRESSURE}
            )
    return "held"
