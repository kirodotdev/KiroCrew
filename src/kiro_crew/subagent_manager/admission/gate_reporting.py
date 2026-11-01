"""How a spawn's outcome reaches others: the done-callback announce and the crew log.

``_safe_announce_impl`` is the guarded announce through ``_on_done`` that every terminal
record without a run report goes through, and ``_announce_rejection_impl`` routes a
refused batch member to it. The three ``_record_crew_log_*`` members pin the turn that
asked for a dispatch and write the parent's ``subagent/spawned`` and spawn
``approval/requested`` entries from that pin.

A function here whose first parameter is ``self`` is a ``_GateMixin`` method: the class
binds it under the same name in ``admission/gate.py``. The ``*_impl`` ones are rebound
onto ``kiro_crew.subagent``'s globals by ``bind_component_globals``, so the names they read
unqualified are that module's, imported here for typing only; the ``_record_crew_log_*``
ones run on this module's own globals and import what they use inside their bodies.

The manager fields this module uses. Writes: none. Reads: ``_on_done``, ``_sessions``,
``_teardown_cancelled_ids``, ``_safe_announce``, ``_forget_pending_start``,
``arm_report_in_flight``, ``consume_report_hold``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Sequence
    from typing import Any

    from ...subagent import SubagentInfo, logger
    from .gate import _GateMixin


#: ``tool`` on a spawn prompt's crew-log entries. The parent's own tool call is
#: what is waiting on the human, so the name is that call's, not the child's
#: agent -- and it is one constant because the request and the decision must
#: agree for a reader pairing them by ``approval_id``.
_SPAWN_APPROVAL_TOOL = "spawn_run"


async def _safe_announce_impl(self: _GateMixin, info: SubagentInfo) -> None:
    """Notify completion callback with error handling.

    Args:
        info (SubagentInfo): The subagent metadata.
    """
    assert self._manager._on_done is not None
    if info.id in getattr(self._manager, "_teardown_cancelled_ids", ()):
        # Same statement as the terminal-report gate, at the OTHER announce
        # entry point. ``_on_done`` resolves the parent key through the
        # session registry and injects, which CREATES a session when none is
        # live, so announcing here rebuilds the conversation the teardown
        # just took down. The rejection paths reach this function directly
        # rather than through ``_report_terminal``, so the gate that covers
        # the run that EXECUTED does not cover the run that was refused
        # before it ever started -- and a spawn parked on its approval is
        # marked by the teardown precisely because its delivery must not
        # land. The id is left in the gate rather than discarded: this
        # function is one of several announce entry points, and the age
        # backstop is what retires the mark.
        logger.info("Skipping parent announce for %s - its parent ended", info.id)
        return
    # Records routed here never pass through ``_report_terminal``, so this
    # is their done-flip-equivalent moment: arm the done-but-unreported
    # hold immediately before the announce (registered approval-parked
    # rejections, batch rejections, lost-submission synthetics -- all
    # created/flipped ``done=True`` before reaching this coroutine). Flush-
    # only records never arm (guarded inside the arm itself): they skip the
    # consumer's accounting block entirely.
    self._manager.arm_report_in_flight(info)
    try:
        await self._manager._on_done(info)
    except Exception as exc:
        logger.error("Subagent announce failed for %s (%s)", info.id, type(exc).__name__)
    finally:
        # Structural release, mirroring ``_report_terminal``'s: whether the
        # consumer landed the contribution (idempotent no-op), the announce
        # raised, or this task was cancelled -- an announce that has ended
        # is not in flight, and ``batch_reports_in_flight`` must not
        # strand the wave-close fallback.
        self._manager.consume_report_hold(info.batch_id, info.id)


def crew_log_asking_origin(sessions: Any, parent_session_key: str) -> "tuple[str, int, int] | None":
    """``(unit, turn, attempt)`` of the turn asking for a spawn right now, or None.

    Read once where a spawn is first accepted and carried on the queued row as
    ``_crew_log_asked``, so every later pass pins the same origin. That includes a
    pass in another process: the durable queue replays a row after a restart, when
    the parent's unit has moved on and the asking turn is gone from memory, and
    the row is then the only record of which unit asked. ``turn`` is 0 when no turn
    is asking (a cron, a hook, a slash command); the unit is still worth keeping.

    None when the crew log is off or the parent has no unit, which leaves the pin
    to :func:`_record_crew_log_dispatch`'s own reading. Best-effort like it: a
    failure here must not fail the spawn.
    """
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log.resolve import unit_for_session_key
    from kiro_crew.subagent import logger as _logger

    try:
        if not crew_log_emit.enabled():
            return None
        sid = unit_for_session_key(sessions, parent_session_key)
        # The provider authors the id, and the row persists it. An over-long one is
        # left off the row; the dispatch pin then reads it live and refuses it there,
        # counted as a lost origin.
        if not sid or len(sid) > crew_log_emit._MAX_SESSION_ID_CHARS:
            return None
        turn, attempt = crew_log_emit.asking_turn(sid)
        return (sid, turn, attempt)
    except Exception:
        _logger.debug("crew log: reading a spawn's asking turn failed", exc_info=True)
        return None


def _record_crew_log_dispatch(
    self: _GateMixin,
    info: SubagentInfo,
    *,
    from_queue: bool,
    asked: "Sequence[Any] | None" = None,
) -> None:
    """PIN the parent session and turn that asked for *info*. Writes nothing.

    Called from both accepted exits of ``spawn`` and from neither rejection.
    Pinning here and writing elsewhere is deliberate: this is normally the only
    moment the ASKING turn is knowable -- a spawn arrives as a tool call inside
    the parent's turn -- but being accepted is not being started. Registration
    is followed by the spawn approval gate, and a decline returns through
    ``_claim_finalize`` + ``_safe_announce`` without ever reaching ``_run``, so
    an entry written here would be an opener nothing closes. The entry is
    written by ``_log_spawned``, the one site that means the run is really
    starting, from the pin this leaves behind.

    ``asked`` is ``(unit, turn, attempt)`` and overrides that reading. ``spawn``
    reads it once where the spawn is first accepted
    (:func:`crew_log_asking_origin`) and carries it on the queued row, and a
    queued follow-up's watcher hands in the one it pinned where the follow-up
    was requested: that follow-up is DISPATCHED after the run it continues has
    finished, so no turn is asking at this moment and the parent may be on an
    unrelated one. With no ``asked`` the live reading is used, which is correct
    for a caller dispatching from inside its own turn.

    ``from_queue`` marks a member re-entering under the same id after the
    stagger or concurrency gate held it, and the pin it was accepted with must
    stand. ``remember_child_origin`` is what keeps it: it refuses to move an
    origin already in the map. What that map does not survive is a restart, and
    the durable queue replays a queued row into this site in a process whose map
    is empty -- so the pin is re-established on that pass rather than skipped,
    or the child's spawn, steer and terminal entries are all omitted with
    nothing to report the gap. The row's ``asked`` names the unit and turn that
    asked, so the replayed child lands in the asking unit's log rather than the
    parent's post-restart one. A row persisted before the origin was stored
    carries none: it falls back to the parent's current unit with the turn left
    UNOBSERVED, because the parent's live turn names one that did not ask.

    Best-effort throughout. A spawn must not fail because a log entry could
    not be pinned, and an unresolvable parent yields an empty session id, which
    the pin refuses -- leaving the later write with nothing to open, which is
    the correct outcome rather than a guessed one.

    Every name is imported inside the body on purpose. This method does NOT
    end in ``_impl``, so ``bind_component_globals`` leaves it running on this
    module's own globals -- where the facade's imports, ``logger`` included,
    exist only under ``TYPE_CHECKING``.
    """
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log.resolve import unit_for_session_key
    from kiro_crew.subagent import logger as _logger

    try:
        if not crew_log_emit.enabled():
            return
        attempt = 0
        if asked is not None:
            # ``(unit, turn, attempt)``; a row persisted before the attempt was
            # recorded carries the pair, and a durable row reads back as a list.
            sid, turn = str(asked[0] or ""), int(asked[1] or 0)
            attempt = int(asked[2] or 0) if len(asked) > 2 else 0
        else:
            sid = unit_for_session_key(self._manager._sessions, info.parent_session_key)
            if from_queue:
                # A drained member: in THIS process its origin is already
                # pinned and the pin refuses to move, so this pass changes
                # nothing. In a process that replayed the row from the durable
                # queue the map is empty and this is the pass that restores it,
                # with the turn unobserved because the one that asked is gone
                # with the process that held it.
                turn = 0
            else:
                turn, attempt = crew_log_emit.asking_turn(sid) if sid else (0, 0)
        if not sid:
            return
        crew_log_emit.remember_child_origin(info.id, sid, turn, attempt)
    except Exception:
        _logger.debug("crew log: pinning a subagent dispatch failed", exc_info=True)


def _record_crew_log_spawn_started(self: _GateMixin, info: SubagentInfo) -> None:
    """Write *info*'s ``subagent/spawned`` into the PARENT session's crew log.

    Called from ``_log_spawned``, which every path that actually starts a run
    goes through and no rejection does -- including the approval gate, which
    reaches it only after a human said yes.

    The turn comes from the pin, never from the parent's live turn. This site
    can be reached an unbounded human approval after the ask, by which point the
    parent is very likely on a different turn; reading it here is exactly the
    after-the-fact re-derivation the log may not contain. An unpinned child (the
    flag came on mid-flight, or the parent could not be resolved) writes nothing.

    No ``ref`` into the child's log. The schema describes one and a child that
    had a crew log would deserve it, but no subagent path opens one, so the
    citation would name a file that does not exist -- indistinguishable, to a
    reader, from one that was deleted.
    """
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.subagent import logger as _logger

    try:
        if not crew_log_emit.enabled():
            return
        sid, asked_turn = crew_log_emit.open_child_origin(info.id)
        if not sid:
            return
        crew_log_emit.on_subagent_spawned(
            sid,
            asked_turn,
            agent_id=info.id,
            attempt=crew_log_emit.child_origin_attempt(info.id),
            agent=info.run_agent,
            model=info.model,
            task=info.task,
            scope={
                "memory": info.include_memory,
                "lessons": info.include_lessons,
                "project": info.include_project,
            },
        )
    except Exception:
        _logger.debug("crew log: recording a subagent spawn failed", exc_info=True)


def _record_crew_log_spawn_approval_requested(
    self: _GateMixin, info: SubagentInfo, *, approval_id: str, reason: str
) -> "tuple[str, int]":
    """Write *info*'s spawn prompt as an ``approval/requested`` entry.

    Returns the parent session and asking turn the entry was filed under, so
    the decision is recorded beside its own request -- hand it to
    :meth:`ManagerComponent._record_crew_log_approval_decided`, the shared
    closer. An empty session id means nothing was written and that closer is
    a no-op too, so the pair is all-or-nothing by construction rather than by
    two separate checks.

    The origin comes from the pin, read through ``dispatch_origin`` because the
    prompt happens BEFORE the opener: the dispatch has been accepted and the
    run has not started, which is the one window ``child_origin`` is designed
    to refuse. Reading the parent's live turn instead would file the prompt
    under whatever turn the parent reached while a person took their time.

    ``tool`` is the spawn tool rather than the child's own agent name, because
    what is waiting on the human is the parent's tool call.
    """
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.subagent import logger as _logger

    try:
        if not crew_log_emit.enabled():
            return ("", 0)
        sid, asked_turn = crew_log_emit.dispatch_origin(info.id)
        if not sid:
            return ("", 0)
        crew_log_emit.on_approval_requested(
            sid,
            asked_turn,
            approval_id=approval_id,
            tool=_SPAWN_APPROVAL_TOOL,
            reason=reason,
        )
        return (sid, asked_turn)
    except Exception:
        _logger.debug("crew log: recording a spawn approval request failed", exc_info=True)
        return ("", 0)


def _announce_rejection_impl(self: _GateMixin, info: SubagentInfo) -> SubagentInfo:
    """Route a terminal spawn rejection through the done callback.

    A rejected batch member is counted as submitted (top of ``spawn``)
    but never registers and never reaches ``_run``'s completion path.
    Without an announce, the gateway's wave accounting never sees its
    terminal state — and when the rejection is the wave's FINAL
    submission, no later completion event re-evaluates the wave, so
    every sibling result already held for the digest strands forever.
    Announcing lets ``_subagent_done`` count the member
    as failed and release the digest when it closes the wave.

    Non-batch rejections skip the announce: the caller already receives
    the error synchronously in the returned info, and injecting a
    completion turn for them would double-report. That holds for
    queue-drained non-batch rejections too — ``_drain_queue`` announces
    those itself off the returned info, so announcing here as well would
    inject the completion twice.
    """
    self._manager._forget_pending_start(info.id)
    if info.batch_id and self._manager._on_done:
        try:
            # Arm synchronously BEFORE scheduling the announce. This path
            # also carries SYNTHETIC rejections created ``done=True``
            # elsewhere (unknown-agent / batch rejections counted as
            # submitted but never registered), which have no flip-site arm.
            # ``_safe_announce`` arms too, but only on its first execution
            # step one event-loop turn later -- during that turn the member
            # is done, not pending, and not in flight, so a sibling
            # completion could close the wave early. Arm here so the hold
            # and the submitted-count can never be observed apart. The arm
            # is idempotent, so a registered rejection that already armed
            # at its flip site composes with no effect.
            self._manager.arm_report_in_flight(info)
            _announce_task = asyncio.ensure_future(self._manager._safe_announce(info))
            self._hold_announce_task(f"reject-{info.id}", _announce_task)
            # Strand-guard mirroring ``_spawn_terminal_report``'s: an
            # announce task cancelled before its first run never reaches
            # ``_safe_announce``'s structural release. Idempotent no-op on
            # every path where the release already happened.
            _announce_task.add_done_callback(
                lambda _t: self._manager.consume_report_hold(info.batch_id, info.id)
            )
        except RuntimeError:
            pass  # no running loop (sync/test context)
    return info
