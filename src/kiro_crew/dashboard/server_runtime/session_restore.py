"""Rebuilding the dashboard's chat slots from persisted sessions at startup."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        DashboardState,
        KiroCrewConfig,
        cautious_boot,
        channel_slots,
        chat,
        logger,
        slot_retention,
    )


async def _restore_dashboard_sessions(state: DashboardState, cfg: KiroCrewConfig) -> None:
    """Rebuild the dashboard's slots from the sessions it held, then surface channel ones.

    Restore exactly the tabs the user had open at last shutdown — these
    come back regardless of mtime, so long-running tabs don't silently
    fall off into History on every gateway restart. Closed tabs (meta.closed)
    are still excluded by the rehydrate guard. restore_open_slots() logs
    its own info line on success, so no caller-side log here.
    Awaited (not called bare) so the restore yields to the loop between tabs and
    the stall watchdog keeps getting its heartbeat — a user with many large tabs
    would otherwise block here long enough to trip the 25s watchdog and crash-loop the
    gateway before it finished starting.

    Both restores run inside suspend_slots_push() so the per-slot broadcasts
    coalesce into one at the end: get_or_create_slot() pushes the whole slot list
    on every call, which makes bulk restore O(N²) in serialization work for
    intermediate states no client renders. Reseeding happens inside the block too
    — it must complete before the single broadcast so clients never see slots
    under a counter that could still re-mint a colliding index.

    Slot lists are labelled partial while this runs (see
    ``DashboardState.begin_slot_list_rebuild``). The channel conversations belong
    to the set being rebuilt too, so once their reconciler task is started it
    owns the finish: it calls ``finish_slot_list_rebuild`` after each complete
    pass, off the boot path. Only when no task was started, because channel
    surfacing is off, restore raised first, or task creation failed, is the finish
    called here, once. Either call clears the label only while no DROPPED open tab
    (one whose read or rehydrate failed, per ``unrestored_slot_notice``) is still
    absent from the slot list; a remote-only reopen seed never holds it. A hold that
    never clears ends at ``_SLOT_LIST_PARTIAL_DEADLINE_S``, armed here.
    """
    reconciler_started = False
    state.arm_slot_list_rebuild_deadline()
    from kiro_crew.shared_crew.wiring import prepare_gateway

    await prepare_gateway(state)
    try:
        # Session restores spawn a kiro-cli process per restored tab — the last
        # large group of the startup battery, so it too gets a cautious-boot window.
        await cautious_boot.pause_before("session restore")
        from kiro_crew.shared_crew.host_hooks import mark_sessions_restored

        try:
            with state.suspend_slots_push():
                await chat.restore_open_slots_async(state)
                restored = await chat.restore_recent_sessions_async(
                    state,
                    cfg.dashboard.restore_window_minutes if cfg.dashboard.restore_sessions else 0,
                    folders_only=not cfg.dashboard.restore_sessions,
                )
                if restored:
                    logger.info("Restored %d session(s)", restored)

                # Both restore paths above rehydrate tabs under their original
                # "chat-<N>-<ts>" keys but leave _slot_counter at its boot value of 0.
                # Reseed it past the highest restored index so the next new chat can't
                # re-mint a colliding low index (which scrambles the tab -> session map).
                state.reseed_slot_counter()
        finally:
            # Restart notes reconciliation owes a session land only once the slots
            # the restore builds exist (``shared_crew.host_hooks``).
            mark_sessions_restored(state)

        from kiro_crew.dashboard.slot_queue_repository import drain_restored_parked_sends

        await drain_restored_parked_sends(state)

        if state._dynamic_cards is not None:
            state._dynamic_cards.seed_open_sessions()

        # Archive idle tabs on a timer once the live count nears the slot cap, so a
        # dashboard nobody cleans up never reaches the point where new chats fail.
        if cfg.dashboard.idle_slot_sweep_days > 0:
            _sweep = asyncio.create_task(
                slot_retention.idle_slot_sweep_loop(state, cfg.dashboard.idle_slot_sweep_days)
            )
            state._background_tasks.add(_sweep)
            _sweep.add_done_callback(state._background_tasks.discard)

        # Surface conversations started on Slack/Discord/Teams (etc.) in the chat
        # list. These persist under channel-namespaced keys (``slack:<ts>``), which
        # neither restore path above builds slots for — without this they exist only
        # in the sidebar's collapsed History pane. Runs immediately, then on a timer
        # so a channel conversation started while the dashboard is open still shows
        # up without a restart. The task calls the partial-list finish after each
        # complete pass; nothing on the boot path waits for that pass.
        if cfg.dashboard.surface_channel_sessions:
            _chan_reconciler = asyncio.create_task(
                channel_slots.channel_slot_reconciler(state, cfg.dashboard.restore_window_minutes)
            )
            state._channel_slot_reconciler = _chan_reconciler  # prevent GC
            reconciler_started = True
    finally:
        if not reconciler_started:
            state.replace_channel_slot_holds((), hold_channels=False)
            state.finish_slot_list_rebuild()
