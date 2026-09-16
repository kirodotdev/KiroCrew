"""The rotation compaction method (``soft``) behind ``session.compaction_method``.

``CompactionCoordinator._run_method_ladder`` asks :func:`try_rotation` first: a
configured rotation restarts the session on purpose, so its successor re-seeds from
the dashboard transcript with no model call. :func:`try_rotation` returns ``None``
when ``native`` runs instead (the default method, an idle trigger, or a rotation
that answered ``unavailable`` or ``insufficient``). Every function takes the
coordinator, whose state, deps and owner seams it reads; the coordinator binds
:func:`rotate` as its ``_rotate`` so a patch of it still reaches the ladder. The
arithmetic lives in :mod:`kiro_crew.session_compaction_methods`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from kiro_crew.context_assembly.budget import startup_context_chars
from kiro_crew.context_assembly.replay import replay_budget_chars
from kiro_crew.session_compaction_methods import (
    COMPACTION_METHOD_NATIVE,
    COMPACTION_METHOD_SOFT,
    OUTCOME_INSUFFICIENT,
    OUTCOME_UNAVAILABLE,
    estimate_tokens,
    method_sufficient,
    project_pct_after,
)

if TYPE_CHECKING:
    from kiro_crew.session_compaction import CompactionCoordinator


async def try_rotation(
    coordinator: CompactionCoordinator, key: str, session: Any, pct: float, *, idle: bool
) -> str | None:
    """The configured rotation's terminal result, or ``None`` when ``native`` runs.

    An *idle* compaction never rotates: a rotation is a restart, and the idle
    trigger takes only the non-destructive branch. A rotation that raises (a
    provider without a window figure, a surface hook that fails) counts as
    ``unavailable`` while *key* still maps to *session*. Once the key maps to
    another session (the successor already started), the exception propagates,
    so ``_compact_session`` reports ``failed`` as it does when the native
    restart raises, and ``native`` never runs on the replaced session.
    """
    logger = coordinator._deps.logger
    # Normalized at load to a known name.
    method = COMPACTION_METHOD_NATIVE if idle else coordinator._owner._cfg.session.compaction_method
    if method == COMPACTION_METHOD_NATIVE:
        return None
    try:
        outcome = await coordinator._rotate(method, key, session, pct)
    except asyncio.CancelledError:
        raise
    except Exception:
        if coordinator._owner._sessions.get(key) is not session:
            logger.warning(
                "Session %s compaction method %s raised after the session was replaced; "
                "not handing to native",
                key,
                method,
            )
            raise
        logger.warning(
            "Session %s compaction method %s raised; handing to native", key, method, exc_info=True
        )
        outcome = OUTCOME_UNAVAILABLE
    logger.info("Session %s compaction method %s: %s", key, method, outcome)
    if outcome in (OUTCOME_UNAVAILABLE, OUTCOME_INSUFFICIENT):
        return None
    return outcome


async def rotate(
    coordinator: CompactionCoordinator, method: str, key: str, session: Any, pct: float
) -> str:
    """Run the rotation method: restart the session so its successor re-seeds from the transcript.

    Returns ``unavailable`` / ``insufficient`` to hand to ``native``, else
    the terminal result of the walk: ``recycled``; ``busy`` when a turn
    still holds the semaphore (``native`` needs it too, so it could not run
    either); ``absent`` when the key changed owners during the wait;
    ``cancelled`` when a user Stop landed during the hold; or ``failed``
    when the successor did not start.
    """
    window = session.provider.context_window_tokens()
    if window <= 0:
        # No window figure: nothing to project from, and the successor's
        # replay would size its tail from the reference window blind.
        return OUTCOME_UNAVAILABLE
    if method != COMPACTION_METHOD_SOFT:
        return OUTCOME_UNAVAILABLE
    writer = coordinator.state.seed_writer
    if writer is None or not writer.supports(key):
        # The successor is re-seeded from the dashboard transcript by its
        # replay, and nothing else re-seeds it -- a cron job, hook, subagent
        # or channel session recycled here would come back with zero turns,
        # and a channel's own fresh-session bootstrap is a model call, which
        # ``soft`` promises not to spend. Answered before projecting, so the
        # log names the real reason (unavailable) rather than a window
        # arithmetic (insufficient) that could never have mattered here.
        return OUTCOME_UNAVAILABLE
    # The tail the successor carries is the replay's own budget for this
    # window: one figure, shared with the replay itself.
    projected = project_rotation(window, estimate_tokens(replay_budget_chars(window)))
    if not method_sufficient(
        projected,
        threshold_pct=coordinator.effective_autocompact_pct(key),
        min_effect_pct_points=coordinator._deps.compact_min_effect_pct_points,
    ):
        return OUTCOME_INSUFFICIENT

    timeout = coordinator._deps.compact_wait_timeout_secs()
    try:
        await asyncio.wait_for(session.semaphore.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        return "busy"
    # The same guards as ``_compact_in_place`` and ``_recycle_unmanaged``, for
    # the same Stop. Read AFTER the permit is held: a Stop that ended the turn
    # this waited behind is not this rotation's cancel.
    stop_gen = coordinator._stop_generation(key)
    # Named where ``reset`` looks (``_orphan_turn_holder``), so a hard Stop
    # during the hold below records this task as the holder of the permit it
    # hands to a woken claimant.
    session.turn_owner = asyncio.current_task()

    def _stopped() -> bool:
        return coordinator._stop_generation(key) > stop_gen

    try:
        # Held from here through the restart: every notice posted from here (a
        # Stop's during the sub-agent wait, a failed start's, the rotation's own)
        # and the counter name the rotation, so the successor's replay leaves
        # those notices out.
        coordinator.state.rotating[key] = method
        # The key can change owners while this rotation waits; checking under
        # the registry lock keeps stale work from recycling its successor.
        async with coordinator._owner._lock:
            if coordinator._owner._sessions.get(key) is not session:
                return "absent"
        # A rotation ends this process like every other restart, and with it
        # the sub-agents that share it, so it waits for them the same way.
        if not await coordinator._await_cotenants(key, pct, stopped=_stopped, method=method):
            return await coordinator._settle_cancelled(key, pct)
        # The cotenant wait yields, and a reset that is not a Stop (a rewind, a
        # model switch) can replace the session meanwhile. Restarting then would
        # recycle the stale session, post a rotation notice the user never had,
        # and arm this rotation's verdict on a successor it never measured.
        async with coordinator._owner._lock:
            if coordinator._owner._sessions.get(key) is not session:
                return "absent"
        # The restart every other path takes: the successor starts first and
        # inherits the turns queued behind this hold, then the old session is
        # recycled. It is seeded lazily: its first real turn replays the
        # transcript.
        result = await coordinator._restart_held(key, session, pct)
        if result != "recycled":
            # The successor did not start: the old session keeps its key and
            # its context, and ``_restart_held`` has armed the cooldown.
            return result
        # The projection judged this rotation BEFORE it ran; the successor
        # has not answered a turn yet, so its real starting usage is read on
        # its first confirmed turn, through the same deferred verdict the
        # native path uses. That reading arms the failure cooldown when the
        # rotation freed too little, as it does for ``native``.
        coordinator.state.pending_verdict[key] = pct
        return "recycled"
    finally:
        coordinator.state.rotating.pop(key, None)
        # Release ONLY a permit this task still owns. A hard Stop during the
        # hold popped the session and handed its permit to a woken claimant;
        # a second release here would raise in that claimant's own release.
        # The release clears the holder record in the same step, as every
        # other holding does, so ``compaction_waits_for_turn`` never reads a
        # task that has let the permit go.
        if not coordinator._owner.absorb_orphaned_release(key):
            session.semaphore.release()
            session.turn_owner = None


def project_rotation(window: int, carried_tokens: int) -> float | None:
    """Usage the successor would start at; ``None`` only for a window of 0, which the caller rules out."""
    return project_pct_after(
        window_tokens=window,
        carried_tokens=carried_tokens,
        overhead_tokens=estimate_tokens(startup_context_chars()),
    )
