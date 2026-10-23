"""A run from start to end: the timeout-wrapped run, its session teardown and its reap ending.

``_run_impl`` runs one subagent task under its deadline, records how it ended and, in its
``finally``, spawns the terminal report, tears the session down and releases the slot.
``_teardown_run_session_impl`` releases and resets the run's own session,
``_record_reap_ending_impl`` records the ending of a run whose reap got there first,
``_touch_activity_impl`` and ``_fire_event_impl`` are the activity stamp and the lifecycle
events a run emits, and ``_effective_turn_limit_impl`` and ``update_completion_keep_impl``
are the turn cap and the completion keep it applies. New work on a run's start-to-end path
goes here; the turn itself is ``run_turn``.

A function here whose first parameter is ``self`` is a ``RunEventCoordinator`` member: the
class binds it under the same name in ``subagent_manager/run.py``. The ``*_impl`` ones are
rebound onto ``kiro_crew.subagent``'s globals by ``bind_component_globals``, so the names
they read unqualified are that module's, imported here for typing only; this module defines
no other.

The manager fields this module uses. Writes: ``_completion_keep``,
``_completion_keep_chars``, ``_process_handles``, ``_published_depths``, ``_running_count``,
``_tasks``, ``_teardown_gates``. Reads: ``_admission``, ``_await_report``,
``_claim_finalize``, ``_default_timeout``, ``_default_turn_limit``, ``_drain_queue``,
``_effective_turn_limit``, ``_fire_event``, ``_on_event``, ``_record_cost``,
``_release_slot``, ``_retain_process_handles``, ``_run_inner``,
``_schedule_cancel_recovery``, ``_sessions``, ``_sessions_under``, ``_shutting_down``,
``_sigkill_sessions``, ``_spawn_terminal_report``, ``_taskq_dependency_coordinator``,
``_teardown_run_session``, ``_write_tombstone``, ``arm_report_in_flight``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import time

    from ..subagent import (
        _ON_DONE_TIMEOUT,
        _RESET_TIMEOUT,
        _TURN_LIMIT,
        MAX_ERROR_DETAIL_LEN,
        SESSION_NOT_FOUND_GIVE_UP_TEXT,
        SESSION_NOT_FOUND_NOT_REPLAYED_TEXT,
        Stats,
        SubagentInfo,
        _describe_exception,
        _redact,
        _timeout_context,
        acp_error_is_session_not_found,
        append_fallback_story,
        failure_name,
        is_runtime_death,
        join_failures,
        kill_set,
        logger,
        process_survived_async,
        read_tombstone,
        refresh_live_process_tree,
        sel,
        teardown_capture,
        with_kill_failure,
    )
    from .run import RunEventCoordinator


def _effective_turn_limit_impl(self: RunEventCoordinator, info: SubagentInfo) -> int:
    """Resolved turn cap for a run: per-spawn ``max_turns`` → config
    default (``agent.subagent_max_turns``) → hardcoded ``_TURN_LIMIT``.

    ``0`` at any level means "not set" and falls through to the next.
    """
    return info.max_turns or self._manager._default_turn_limit or _TURN_LIMIT


def update_completion_keep_impl(self: RunEventCoordinator, mode: str, max_chars: int) -> None:
    """Update the live completion-keep mode and char budget.

    Called from ``SubagentManager.reconfigure`` whenever a reload of
    ``config.json`` touches ``agent.completion_keep`` or
    ``agent.completion_keep_chars``, whichever writer produced it (the
    Settings UI, ``kirocrew config set``, a hand edit). The values are read once per subagent at
    completion time (``apply_completion_keep`` call site), so swapping
    them here takes effect for the next subagent to finish — including
    ones already running. No torn-read possible under asyncio: both
    reads happen in the same synchronous block.

    ``mode`` is validated by ``_validated_completion_keep`` at config
    load; this setter is intentionally permissive about ``max_chars``
    so the loader / handler stays the validation choke-point.
    """
    self._manager._completion_keep = mode
    self._manager._completion_keep_chars = max_chars


async def _teardown_run_session_impl(
    self: RunEventCoordinator, info: SubagentInfo, session_key: str
) -> None:
    """Release and reset the run's own session (skipped when reaped).

    Split out of ``_run``'s ``finally`` so the caller can wrap it in a nested
    ``try/finally``: every statement here AWAITS, and a cancellation arriving
    at one of those awaits propagates straight out of the enclosing
    ``finally`` suite (the ``except Exception`` arms do not catch
    ``CancelledError``). That skipped the slot release, the task-registry pop
    and the teardown gate — leaking a concurrency slot, which is the very
    class of bug this module's guard split exists to prevent.
    """
    if not info._session_sharing:
        # Re-walk the tree while the run still holds its lease, so children
        # this run's turns started are on the handle taken below and can
        # vouch for the group once the leader exits. Before ``release()``:
        # a walk after it would leave the key claimable for the walk's
        # length, and a claimer's live turn would then be popped by the
        # reset. A cancellation here still releases the lease and records
        # the undecided kill, like one during the reset.
        try:
            await refresh_live_process_tree(self._manager._sessions, session_key)
        except asyncio.CancelledError as exc:
            try:
                self._manager._sessions.release(session_key, cleanup=False)
            except Exception:
                logger.warning("Subagent %s: release failed", info.id, exc_info=True)
            if not info._reap_started:
                info.error = with_kill_failure(
                    info.error or "",
                    f"{failure_name(exc)}: the teardown was cancelled before its kill decided",
                )
            raise
    try:
        if info._session_sharing:
            # Session-sharing subagents: destroy the session handle
            # (unregister from shared runtime). Don't kill the runtime.
            # Skip when the reaper already tore it down (info.reaped).
            # Retain-by-default: keep the transcript files — they are
            # spawn_continue's resume material. The tombstone pruner
            # deletes them with the run folder (~1h after delivery)
            # unless the conversation is promoted (continued / keep).
            if info._shared_provider and not info.reaped:
                try:
                    info._shared_provider.set_keep_transcript(True)
                except Exception:
                    logger.debug("set_keep_transcript failed", exc_info=True)
                await info._shared_provider.shutdown()
        else:
            # Retain-by-default: never delete session files at teardown.
            # The reset() below still expires the process, so an idle
            # conversation costs a JSON file, not RSS. Deletion is owned
            # by the tombstone pruner (default runs, ~1h) or the
            # conversation TTL sweep / spawn_release (promoted runs).
            self._manager._sessions.release(session_key, cleanup=False)
    except Exception:
        logger.warning("Subagent %s: release failed", info.id, exc_info=True)
    if not info._session_sharing:
        # Taken BEFORE the reset and RETAINED under the run's id: the reset
        # pops the session from the map before the awaits that can hang,
        # and this teardown is the reset that commonly hangs while the
        # reaper (a deadline, a user Stop) arrives to act on it -- the
        # reaper finds the map empty and reads this entry instead. On a map
        # miss here the roles are reversed and the entry is the reaper's.
        # Every process the key names is a candidate, each on its own handle.
        kill_failed: str | None = None
        fallback_ran = False
        try:
            pairs = self._manager._sessions_under(session_key)
            handles = self._manager._retain_process_handles(info.id, session_key, pairs)
            seen = [session for session, _handle in pairs]
            # Under a scope whose hook takes the handle of the exact session the
            # reset pops (a successor a cold start registered after the snapshot);
            # a popped session with no pid the snapshot never saw is a kill
            # failure, one it saw names no process (see ``_force_reap``).
            reset_kwargs, popped = teardown_capture(self._manager._sessions, session_key)
            try:
                await asyncio.wait_for(
                    self._manager._sessions.reset(session_key, **reset_kwargs),
                    timeout=_RESET_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("Subagent %s: reset timed out, force-killing", info.id)
                fallback_ran = True
                targets, missing = kill_set(handles, popped, seen=seen)
                kill_failed = join_failures(
                    await self._manager._sigkill_sessions(session_key, targets, popped=popped),
                    missing,
                )
            except Exception:
                logger.exception("Subagent %s: reset failed, force-killing", info.id)
                fallback_ran = True
                targets, missing = kill_set(handles, popped, seen=seen)
                kill_failed = join_failures(
                    await self._manager._sigkill_sessions(session_key, targets, popped=popped),
                    missing,
                )
            else:
                # A completed reset (True, or False for a key the reaper had
                # already popped) is not proof the process is gone; each
                # handle is asked, and a process still standing gets the
                # fallback (see ``_force_reap``).
                targets, missing = kill_set(handles, popped, seen=seen)
                survivors = [handle for handle in targets if await process_survived_async(handle)]
                if survivors:
                    logger.warning(
                        "Subagent %s: process survived the reset, force-killing", info.id
                    )
                    fallback_ran = True
                    kill_failed = await self._manager._sigkill_sessions(
                        session_key, survivors, popped=popped
                    )
                if missing:
                    fallback_ran = True
                    kill_failed = join_failures(kill_failed, missing)
            if kill_failed is not None:
                # The record names the survivor BEFORE the report publishes:
                # the terminal report spawned ahead of this teardown waits
                # on ``teardown_done`` (set by the caller's ``finally`` once
                # this returns) before it builds its payload, so the
                # completion the parent receives, the ``subagent_done``
                # event and the tombstone all carry the same failure -- and
                # the survivor is recorded as a LEAKED PROCESS, separate
                # from the delivery. Published first, the parent received a
                # clean completion whose ``delivered`` tombstone hid the
                # process from reconciliation for good (the cron reaper's
                # ``last_error`` takes the same suffix, the one spelling).
                # A completed run has no tombstone yet and gets none here:
                # until the parent acknowledges the completion its folder
                # must stay in orphan reconciliation, which re-delivers a
                # completion a restart dropped from the parent's queue.
                # ``_leaked_process`` is read by the report instead: an
                # acknowledged delivery writes the ``delivered`` tombstone
                # with ``leaked_process``, so the next start ends the
                # process without announcing the run a second time
                # (``_end_leaked_processes_impl``).
                info.error = with_kill_failure(info.error or "", kill_failed)
                tombstone = read_tombstone(info.id)
                if tombstone is None:
                    info._leaked_process = True
                elif tombstone.get("cause") != "delivered":
                    # The run's own arm wrote it already (an error, a
                    # timeout, a cancel): re-written under the same cause so
                    # the record on disk names the failure too.
                    self._manager._write_tombstone(
                        info, str(tombstone.get("cause") or "error"), leaked_process=True
                    )
            if fallback_ran:
                try:
                    sel().log_tool_invocation(
                        session_key=session_key,
                        source="subagent",
                        tool_name="run_finally_force_kill",
                        # Never ``sigkill`` for a process the kill left
                        # alive; the failure it reported is the record's
                        # error text and this row's.
                        outcome="sigkill" if kill_failed is None else "failed",
                        error=kill_failed or "",
                        metadata={"subagent_id": info.id},
                    )
                except Exception:
                    logger.exception("Subagent %s: SEL audit failed", info.id)
        except asyncio.CancelledError as exc:
            # The kill never decided: the cancellation landed inside the
            # reset, the survivor probe or the signal (a gateway shutdown
            # cancelling the run task). The report this teardown gates is
            # released by the caller's ``finally`` either way, so the record
            # names the undecided kill BEFORE that release -- the cron
            # reaper's rule for its own cancelled kill awaits -- and the
            # completion the parent receives, the ``subagent_done`` event and
            # the delivered-or-not decision all see it: a clean record here
            # published a success and wrote the ``delivered`` tombstone that
            # hid a process nothing had stopped from orphan reconciliation.
            # Not when a reap cancelled this task: the reap consumed the
            # retained handle before cancelling and its own arm writes the
            # kill's outcome, so a second record here would name the kill
            # twice.
            if not info._reap_started:
                info.error = with_kill_failure(
                    info.error or "",
                    f"{failure_name(exc)}: the teardown was cancelled before its kill decided",
                )
            raise
        finally:
            # Decided (or cancelled out from under -- by the reaper, which
            # consumed the entry before cancelling this task).
            self._manager._process_handles.pop(info.id, None)


def _record_reap_ending_impl(
    self: RunEventCoordinator, info: SubagentInfo, unfinished: str
) -> None:
    """Record the ending of a run whose reap got there first.

    Setting ``done`` here wins the first-arrival record over the reaper's
    own synthesis (guard 1 in ``_force_reap``), so the whole record --
    error, stat, tombstone -- is written once. Neutrality follows the FIRST
    stopper: a Stop that lands while a deadline reap already owns the
    teardown sets ``user_stopped`` too; the record still belongs to the
    deadline, so the flag is put back and the failure kept, named
    ``"<origin> — <unfinished>"``.
    """
    neutral = info.stop_is_neutral
    if not neutral:
        info.user_stopped = False
        if not info.error:
            info.error = f"{info._stop_origin or 'the reaper'} — {unfinished}"
    if not info.result and info.streaming_text:
        info.result = info.streaming_text
    info.done = True
    # Terminal done flip on the reap-ending path: arm with it. The reap's
    # report is spawned separately and reaches the consumer only after its
    # own teardown, so a done-but-unarmed window here would let a sibling
    # close the wave early (same shape as the run's own terminal flips).
    self._manager.arm_report_in_flight(info)
    if not neutral:
        Stats().inc_subagent_failed()
    self._manager._write_tombstone(info, info._reap_reason or "reaped")


async def _run_impl(self: RunEventCoordinator, info: SubagentInfo) -> None:
    """Execute a subagent task in its own session."""
    session_key = info.conversation_key or f"subagent:{info.id}"
    # Set by the reap-echo arm below: the run ended because a reap in
    # flight tore its runtime down, and that reap publishes the terminal
    # report once its kill has decided (see the ``finally``).
    reap_owns_report = False
    try:
        # Capture the deadline once, when execution starts. A record that
        # unexpected-cancel recovery respawns keeps its nonzero value, so a
        # config reload between the two attempts cannot move it.
        if info.timeout_secs == 0:
            info.timeout_secs = self._manager._default_timeout
        await asyncio.wait_for(
            self._manager._run_inner(info, session_key), timeout=info.timeout_secs
        )
        if info._reap_started and not info.done:
            # Returned with no ending while a reap is in flight -- the tail
            # records nothing for a whole answer a reap got to first: the
            # reap's ending, recorded and reported as for the runtime
            # death below. Only a complete event finished the answer.
            reap_owns_report = True
            self._record_reap_ending_impl(
                info,
                (
                    "the run was stopped as it finished its answer"
                    if info._answer_finished
                    else "the runtime was torn down before the run finished"
                ),
            )
    except asyncio.TimeoutError:
        # ``done`` is first-arrival-wins in every arm below, as in
        # _force_reap: an ending already recorded -- a claimed completed
        # ending (``_ending_claimed``) sets ``done`` before _run_inner
        # unwinds -- is never overwritten or respawned, and its log line
        # says the run completed rather than naming what cut its tail short.
        if not info.reaped and not info.done:
            info.error = f"Timed out after {info.timeout_secs // 60} minutes [{_timeout_context(info, turn_limit=self._manager._effective_turn_limit(info))}]"
            info.done = True
            # Armed HERE, synchronously with the flip -- not left to the
            # report task spawned later. Between this flip and that task's
            # first run the coroutine yields (the teardown awaits), and in
            # that done-but-unarmed window a sibling completion reads this
            # member as neither pending nor in flight and closes the wave
            # early; the report then finalizes it AGAIN (the
            # failure-path window). The report machinery's own arm stays
            # as an idempotent no-op.
            self._manager.arm_report_in_flight(info)
            Stats().inc_subagent_failed()
            self._manager._write_tombstone(info, "timeout")
        if info._ending_claimed:
            logger.info("Subagent %s completed before its deadline ended the run", info.id)
        else:
            logger.warning("Subagent %s timed out", info.id)
    except asyncio.CancelledError:
        handed_to_recovery = False
        if not info.reaped and not info.done:
            if (
                not info.user_stopped
                and not self._manager._shutting_down
                and not info._cancel_retry_used
                # A live state-write drain means a worker is still
                # (or may still be) writing state.json: respawning a
                # recovery writer now re-opens the stale-overwrite race
                # (reachable on 3.10 via a second outer
                # cancel interrupting wait_for's _cancel_and_wait).
                and not info._state_drain_active
                and info.tool_count == 0
            ):
                # UNEXPECTED cancellation (not user Stop, not shutdown):
                # one-shot auto-continue, mirroring the main path's
                # unexpected-cancel recovery. Skip terminal
                # finalization (via _recovering) and respawn on a fresh
                # task — this task is being cancelled and cannot continue.
                #
                # SIDE-EFFECT GATE (tool_count == 0): the respawn runs on a
                # FRESH session with no ledger of prior tool calls, so the
                # model cannot verify which side effects (files written,
                # messages sent, commands run) already happened — a
                # preamble alone cannot make re-running safe. Once any tool
                # has executed, we finalize with the partial preserved
                # instead of respawning. Text-only activity is safe to
                # resume (the partial is preserved and re-presented).
                info._cancel_retry_used = True
                info._recovering = True
                logger.warning(
                    "Subagent %s unexpectedly cancelled — scheduling one-shot auto-continue",
                    info.id,
                )
                try:
                    sel().log_tool_invocation(
                        session_key=session_key,
                        source="subagent",
                        tool_name="cancel_auto_continue",
                        outcome="scheduled",
                        metadata={"subagent_id": info.id},
                    )
                except Exception:
                    logger.debug("SEL audit for cancel recovery failed", exc_info=True)
                self._manager._schedule_cancel_recovery(info)
                handed_to_recovery = True
            else:
                info.done = True
                # Same synchronous flip+arm as the timeout arm above --
                # the cancel path yields at teardown before its report
                # task first runs (the failure-path window).
                self._manager.arm_report_in_flight(info)
                if (
                    info.tool_count > 0
                    and not info.user_stopped
                    and not self._manager._shutting_down
                ):
                    # Auto-continue deliberately suppressed (side-effect
                    # gate above): be explicit so the parent/user knows the
                    # run was interrupted and NOT resumed, and why.
                    info.error = (
                        "cancelled (auto-continue suppressed: tools already "
                        "executed — resuming on a fresh session could repeat "
                        "side effects)"
                    )
                else:
                    info.error = "cancelled"
                # Preserve whatever streamed before the cancel as a partial
                # result (delivered with the failure).
                if not info.result and info.streaming_text:
                    info.result = info.streaming_text
                Stats().inc_subagent_failed()
                self._manager._write_tombstone(info, "cancelled")
        if info._ending_claimed:
            logger.info("Subagent %s completed before a cancel ended the run", info.id)
        else:
            logger.info("Subagent %s cancelled", info.id)
        if handed_to_recovery:
            # The run continues on the ``<id>:recovery`` task, not this one,
            # so this task must end CANCELLED rather than return: a waiter
            # holding it (``await task``, ``asyncio.wait_for``,
            # ``asyncio.timeout``) would otherwise read a clean completion
            # while the run has neither finished nor claimed its session,
            # and a ``wait_for`` that timed out would return instead of
            # raising ``TimeoutError``. ``_resume``'s handshake
            # waits with ``asyncio.wait``, which does not care how the task
            # ended; the finally below runs either way and, while
            # ``_recovering`` is set, reports nothing.
            raise
    except Exception as exc:
        if getattr(exc, "context_overflow", False):
            # The native session that raised this cannot shrink the envelope
            # it was already rejected on, so replaying there reproduces the
            # rejection. A zero-activity first turn on a SHARED runtime is
            # safe to retry once on a FRESH dedicated runtime, which
            # rebuilds session and projection state from scratch; that
            # rebuild is best-effort, not a diagnosis of why the first
            # envelope was too large. A first attempt that already ran
            # dedicated is terminal: no transition is evidenced that would
            # be expected to make a replacement's envelope small enough or
            # different enough to fit, so another teardown, capacity and
            # spawn cycle is not justified. Any observed text or tool call
            # forbids replay, and the one-shot makes a second overflow
            # terminal.
            can_rebuild = (
                not info._context_overflow_retry_used
                and info._session_sharing
                and not info.conversation_key
                and info.turns == 0
                and info.tool_count == 0
                and not info.streaming_text
                and not info.result
                and not info.user_stopped
                and not info._reap_started
                and not self._manager._shutting_down
            )
            if can_rebuild:
                info._context_overflow_retry_used = True
                info._force_dedicated = True
                info._recovering = True
                logger.warning(
                    "Subagent %s startup context overflowed on a shared runtime; "
                    "retrying once on a fresh dedicated runtime: %s",
                    info.id,
                    exc,
                )
                self._manager._schedule_cancel_recovery(info, reason="context_overflow")
            else:
                if not info.result and info.streaming_text:
                    info.result = info.streaming_text
                detail = _redact(str(exc))[:MAX_ERROR_DETAIL_LEN]
                if info._context_overflow_retry_used:
                    info.error = f"Dedicated-session recovery also overflowed: {detail}"
                elif info.tool_count:
                    # A tool ran: the fresh runtime has no ledger of what it
                    # changed, so replay could repeat a side effect (the
                    # same gate the unexpected-cancel arm applies).
                    info.error = (
                        "Agent context overflowed after tools executed; it was "
                        "not replayed because that could repeat side effects. "
                        f"{detail}"
                    )
                elif info.streaming_text or info.result or info.turns:
                    # Text, a result or a completed turn without any tool
                    # call is side-effect-free, exactly as the
                    # unexpected-cancel arm treats it. Replay is still
                    # withheld, but only because the work already produced
                    # is preserved and running the prompt again would
                    # duplicate it -- not because state changed.
                    info.error = (
                        "Agent context overflowed after it had already produced "
                        "work; that work was preserved and it was not replayed "
                        "to avoid duplicating it. "
                        f"{detail}"
                    )
                else:
                    info.error = detail
                info.done = True
                # Same synchronous flip+arm as the timeout arm above
                # (the failure-path window).
                self._manager.arm_report_in_flight(info)
                Stats().inc_subagent_failed()
                self._manager._write_tombstone(info, "error")
                logger.warning("Subagent %s context overflow: %s", info.id, exc)
        elif acp_error_is_session_not_found(exc) and not info._reap_started:
            # A live backend holds no session under this run's id. The
            # teardown in ``finally`` resets the session and keeps its
            # durable pointer, so a respawned run's claim re-loads the SAME
            # id on a fresh runtime. Same side-effect gate as the
            # unexpected-cancel respawn: once a tool ran, re-sending the
            # task could repeat it, so the run ends with the partial kept.
            can_reload = (
                not info._session_not_found_retry_used
                and info.tool_count == 0
                and not info.user_stopped
                and not self._manager._shutting_down
            )
            if can_reload:
                info._session_not_found_retry_used = True
                info._recovering = True
                logger.warning(
                    "Subagent %s: backend lost its session; re-running once on a "
                    "fresh runtime: %s",
                    info.id,
                    exc,
                )
                self._manager._schedule_cancel_recovery(info, reason="session_not_found")
            else:
                if not info.result and info.streaming_text:
                    info.result = info.streaming_text
                detail = _redact(str(exc))[:MAX_ERROR_DETAIL_LEN]
                if info._session_not_found_retry_used:
                    info.error = f"{SESSION_NOT_FOUND_GIVE_UP_TEXT} {detail}"
                elif info.tool_count:
                    info.error = f"{SESSION_NOT_FOUND_NOT_REPLAYED_TEXT} {detail}"
                else:
                    info.error = detail
                info.done = True
                # Same synchronous flip+arm as the timeout arm above
                # (the failure-path window).
                self._manager.arm_report_in_flight(info)
                Stats().inc_subagent_failed()
                self._manager._write_tombstone(info, "error")
                logger.warning("Subagent %s lost its session: %s", info.id, exc)
        elif info._reap_started and is_runtime_death(exc):
            # The ECHO of our own teardown, not a fault of the run.
            # ``_force_reap`` resets the run's session (or shuts its shared
            # handle) BEFORE it cancels this task, so the in-flight stream
            # observes the runtime it lives on being killed and raises
            # ``AcpProcessDied`` -- "killed (provider shutdown)" -- first.
            # Recording that text as the run's error made every user stop,
            # every parent end and every deadline reap read as a runtime
            # death in the tombstone and an ERROR in the gateway log; four
            # field reports chased it to the provider and the OOM killer.
            # Only the runtime death is the echo: any OTHER exception under
            # a reap is a fault of the run that the teardown merely
            # interrupted, and keeps the traceback below.
            # The record instead names the stop: a user/parent stop stays
            # neutral (``error`` unset, ``outcome == "stopped"``), a
            # deadline reap is a failure that names the deadline, and the
            # tombstone carries the reap's own cause. Setting ``done`` here
            # is what wins the first-arrival record over the reaper's own
            # synthesis (guard 1 in ``_force_reap``), so the whole record --
            # error, stat, tombstone -- is written HERE, as it was before.
            # The REPORT is not: this arm runs while the reap still awaits
            # its reset or the fallback kill that follows it, so a report
            # published from this task's ``finally`` would tell the parent
            # the run was reaped before the kill has decided, and a failure
            # the fallback then reports would reach only the in-memory
            # error text. The reap claims the report after its kill has
            # decided (``supersede_recovery=True``), with the failure
            # appended and the tombstone re-written -- so it is left to
            # the reap.
            reap_owns_report = True
            origin = info._stop_origin or "the reaper"
            if not info.done:
                self._record_reap_ending_impl(
                    info, "the runtime was torn down before the run finished"
                )
            # One attributable line, at the level the action deserves: a
            # user's own stop is routine; a parent end or a deadline reap
            # discarded live work the user did not ask to lose.
            log = logger.info if info._reap_reason == "user_stop" else logger.warning
            log(
                "Subagent %s stopped mid-turn by %s (the stream reported: %s)",
                info.id,
                origin,
                _describe_exception(exc),
            )
        else:
            if not info.done:
                # Story appended INSIDE the cap: info.error reaches a WS
                # frame and the Subagents panel, so the rendered total stays
                # bounded by MAX_ERROR_DETAIL_LEN exactly as before — and the
                # budget trims the ERROR text, never the story, so a verbose
                # chain cannot push the walk out of the terminal error.
                info.error = append_fallback_story(
                    _describe_exception(exc), exc, budget=MAX_ERROR_DETAIL_LEN
                )
                info.done = True
                # Same synchronous flip+arm as the timeout arm above
                # (the failure-path window).
                self._manager.arm_report_in_flight(info)
                Stats().inc_subagent_failed()
                self._manager._write_tombstone(info, "error")
            if info._ending_claimed:
                logger.error("Subagent %s completed, then its run raised", info.id, exc_info=exc)
            else:
                logger.exception("Subagent %s failed", info.id)
    finally:
        # Every ending but a claimed one leaves result.txt capped before its
        # report exists, so the parent reads a bounded file. Bounded, and it
        # never raises, so it cannot keep the report below from spawning.
        await self._cap_unclaimed_result_impl(info)
        # Guard 3 of 3 — the terminal REPORT, owned by the finalize claim.
        # Taken (and the report task SPAWNED) before the teardown awaits
        # below, so a cancellation landing anywhere in teardown cannot
        # strand the outcome: the shielded task is already live. The claim
        # returns False while _recovering without consuming itself, so a
        # pending cancel-recovery respawn is not reported done and its
        # respawned run can claim later.
        report_task = None
        # Set once this finally's session teardown has finished, so the
        # already-spawned report holds its "delivered" tombstone until the
        # child is provably gone (see `_report_terminal`).
        teardown_done = asyncio.Event()
        # Published where it survives this record being evicted: a settlement
        # that happens OUTSIDE this report (the parent's queue drain)
        # can come due after a dashboard clear/cancel has removed the run
        # from _agents AND _tasks, and it still must not tombstone a child that
        # is being killed.
        self._manager._teardown_gates[info.id] = teardown_done
        if reap_owns_report:
            # The reap-echo arm ran: the reap that tore the runtime down
            # publishes the report once its kill has decided (its claim
            # supersedes). The run still owns its cost sample, which the
            # claim branch below would otherwise have recorded; the reap's
            # own record guard is skipped for a record this arm wrote.
            #
            # An abnormal arm that already wrote the tombstone finalized
            # ``info.elapsed`` there; read that single value so the record
            # on disk and the terminal event agree. Only sample here when no
            # writer set it (a run that ended before its folder was seeded
            # writes no tombstone, so nothing finalized elapsed).
            if info.elapsed <= 0:
                info.elapsed = time.time() - info.started
            self._manager._record_cost(info)
        elif self._manager._claim_finalize(info):
            if info.elapsed <= 0:
                info.elapsed = time.time() - info.started
            self._manager._record_cost(info)
            report_task = self._manager._spawn_terminal_report(
                info,
                source="Subagent",
                injection_timeout_reason=(
                    f"delivery timed out after {int(_ON_DONE_TIMEOUT)}s" " (queue + injection)"
                ),
                mark_delivered_on_success=True,
                settle_digest=True,
                teardown_done=teardown_done,
            )
        # Nested try/finally: the teardown awaits must never be able to skip
        # the bookkeeping below (see `_teardown_run_session`).
        try:
            if not info.reaped:
                await self._manager._teardown_run_session(info, session_key)
        finally:
            # Guard 2 of 3 — SLOT accounting on its own one-shot token, so
            # the count is released exactly once whichever terminal path
            # arrives first (and is NOT skipped just because the reaper set
            # `reaped`, which is how an earlier revision leaked slots).
            if self._manager._release_slot(info):
                self._manager._running_count -= 1
                self._manager._drain_queue()
            # The run's session is torn down, so what it held is free memory
            # again: a start waiting for memory is re-checked now, not at its
            # admit wait (a no-op when none waits).
            self._manager._admission.wake_memory_waits("child_terminal")
            # A run that ended while parked on a wait: its resume entry
            # must not hand a slot to a finished run, and its dependency
            # scope must stop counting it (a finished probe is the scope's
            # recovery signal).
            if info._resume_pending:
                self._withdraw_resume(info)
            _coordinator = getattr(self._manager, "_taskq_dependency_coordinator", None)
            if _coordinator is not None:
                try:
                    _coordinator.forget(info.id)
                except Exception:
                    logger.debug("dependency forget failed for %s", info.id, exc_info=True)
            self._manager._tasks.pop(info.id, None)
            # Teardown is done (or was skipped because the reaper did it) —
            # release the report's delivered-tombstone gate. Unconditional,
            # so the report can never wedge on a cancelled teardown.
            teardown_done.set()
            # Set BEFORE the entry is dropped: a waiter that already holds the
            # event is released by the line above, and one arriving after finds
            # no entry, which now means exactly "nothing left to wait for".
            self._manager._teardown_gates.pop(info.id, None)
            self._forget_finished_live_state(info)
            if info._remote_workspace:
                # Last, since it awaits, so a cancellation cannot skip the
                # bookkeeping; a continuation re-inherits before it starts.
                from kiro_crew.coder.session_launch import forget_subagent_environment

                await forget_subagent_environment(session_key)

    # The report itself already ran (or is running) on the shielded task
    # spawned in the finally above; block until it completes so sequencing is
    # unchanged for callers.
    #
    # NOT during shutdown. `_run`'s terminal CancelledError arm deliberately
    # does not re-raise (only the hand-off to cancel recovery does, and it
    # spawns no report), so by the time we reach this await the cancellation has been
    # consumed and `shield` would simply wait out the full _ON_DONE_TIMEOUT
    # injection cap — holding `cancel_all()`'s gather for up to 20 minutes.
    # The report is registered in `self._report_tasks`, so `cancel_all()`'s
    # bounded drain owns it from here.
    if report_task is not None and not self._manager._shutting_down:
        await self._manager._await_report(report_task)


async def _touch_activity_impl(self: RunEventCoordinator, info: SubagentInfo) -> None:
    """Record stream activity for idle-stall detection.

    Updates ``last_activity`` and, if the subagent was flagged
    stalled by the reaper, clears the flag and notifies the UI so the
    running-card drops the "stalled" warning the moment work resumes.
    """
    info.last_activity = time.time()
    info._last_activity_mono = time.monotonic()
    info._stall_suspect_at = 0.0  # activity resets the 2-sweep confirmation
    # Retire the oracle so the next suspicion samples a fresh baseline rather
    # than differencing against counters from before this activity, and bump
    # the generation so a consult submitted before this moment cannot land a
    # stalled verdict on an agent that has just proven it is working.
    if info._stall_oracle is not None:
        info._stall_oracle = info._stall_oracle.fresh()
    info._stall_gen += 1
    if info.stalled:
        info.stalled = False
        await self._manager._fire_event("subagent_stalled", info, {"stalled": False})


async def _fire_event_impl(
    self: RunEventCoordinator, etype: str, info: SubagentInfo, extra: dict | None = None
) -> None:
    # Recorded before the frame goes out, so a slots push serialized after
    # this frame can never carry an older depth than the frame did.
    if etype == "subagent_queued":
        self._manager._published_depths.record(info.parent_session_key, (extra or {}).get("queued"))
    if self._manager._on_event:
        try:
            await self._manager._on_event(etype, info, extra or {})
        except Exception:
            logger.warning("on_event failed for %s/%s", etype, info.id, exc_info=True)
    # A child started or ended while its parent still advertises waiting rows:
    # re-derive the depth from state, and publish it only if it changed. A path
    # that pops or ends a row without its own emit would otherwise leave the
    # table (and every slots push, including a reload's first) holding a count
    # nothing will clear; a path that did emit already published this count, so
    # a heal that agrees with it sends nothing. One read per lifecycle edge, and
    # only for a parent with a non-zero entry.
    if (
        etype in ("subagent_spawn", "subagent_done")
        and self._manager._published_depths.get(info.parent_session_key) > 0
    ):
        self._request_queue_depth(info.parent_session_key, set(), heal=True)
