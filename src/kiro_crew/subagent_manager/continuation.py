"""Continuation behavior for the SubagentManager facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import subagent_persistence as persistence
from ._component import ManagerComponent

if TYPE_CHECKING:
    from ..subagent import (
        _CONVERSATION_TTL_SECS,
        _STEER_STARTUP_POLL_SECS,
        _STEER_STARTUP_WAIT_SECS,
        CONTEXT_GROUP_LESSONS,
        CONTEXT_GROUP_MEMORY,
        CONTEXT_GROUP_PROJECT,
        PROVIDER_LABEL_DEFAULT,
        Any,
        SubagentInfo,
        _cleanup_session_files_sync,
        _redact,
        _subagents_dir,
        agent_dir_for_display,
        asyncio,
        logger,
        read_state,
        sel,
        time,
        update_state,
        uuid,
    )


class ContinuationCoordinator(ManagerComponent):
    """Own continuation transitions while state remains facade-owned."""

    _persistence = persistence
    __slots__ = ()

    def _conversation_busy_impl(self, conv_key: str) -> SubagentInfo | None:
        """Return the live or QUEUED run on *conv_key*, or None.

        Queued members matter: a continuation waiting
        in the spawn queue is not in ``_agents`` yet — missing it would let
        ``spawn_release`` delete the session files it needs (the accepted run
        would then die with ``resume_failed``), or let a second continue race
        the same conversation.

        A FINISHED run also holds its conversation while its id sits in
        ``_abandoned_state_writers``: its bounded state-write drain expired, so a
        worker is still live and its stale whole-file rewrite would roll back the
        ``keep`` that this gate's two callers write on the loop (#6298). Holding
        defers those writes past the worker instead of letting it undo them. That
        record lives on the manager rather than on the run, because
        ``evict_completed_agents`` prunes completed runs out of ``_agents`` and an
        eviction must not release the hold; the worker's own done-callback
        discards the id, so the hold lasts exactly as long as the danger.
        """
        for a in self._manager._agents.values():
            if not a.done and (a.conversation_key or f"subagent:{a.id}") == conv_key:
                return a
        for p in self._manager._queue:
            pkey = str(p.get("conversation_key") or "") or (
                f"subagent:{p.get('_preassigned_id', '')}"
            )
            if pkey == conv_key:
                return SubagentInfo(
                    id=str(p.get("_preassigned_id") or "queued"),
                    task="",
                    queued=True,
                )
        # Checked last: a live or queued run gives the caller a better message.
        # Same synthetic-marker shape as the queued branch above — the run may
        # already have been evicted from _agents, which is exactly why this record
        # is not kept there.
        if conv_key.startswith("subagent:"):
            held = conv_key[len("subagent:") :]
            if held in self._manager._abandoned_state_writers:
                return SubagentInfo(id=held, task="", _state_writer_abandoned=True)
        return None

    def _keep_recorded_on_disk_impl(self, key: str) -> bool:
        """Retention guard for subagent conversations without loop-side disk probes.

        Readable ``state.json`` remains the persisted retention authority. If
        state is unreadable, the session-acquisition/tombstone path's synchronous
        in-memory identity publication fails safe without reading ``tombstone.json``
        on the gateway event loop.
        """
        conv_id = self._persistence.subagent_id_from_conversation_key(key)
        if conv_id is None:
            return False
        try:
            state = read_state(conv_id)
        except OSError:
            state = None
        if isinstance(state, dict):
            return state.get("keep") is True
        return self._persistence.has_live_cleanup_identity(conv_id)

    def _promote_conversation_impl(
        self,
        conv_id: str,
        conv_key: str,
        last_used: float | None = None,
    ) -> Any:
        """Atomically promote all retention surfaces for a conversation."""
        try:
            result = self._persistence.promote_retention(conv_id, state_writer=update_state)
        except OSError:
            logger.debug("promote: failed to persist keep for %s", conv_id, exc_info=True)
            # Preserve the established fail-safe marker for direct callers, but
            # report retryable failure so continue_conversation rolls it back.
            self._manager._sessions.mark_continuable(conv_key)
            self._manager._conversations[conv_key] = (
                last_used if last_used is not None else time.time()
            )
            return self._persistence.RetentionPromotionResult.RETRYABLE
        if result is not self._persistence.RetentionPromotionResult.PROMOTED:
            return result
        self._manager._sessions.mark_continuable(conv_key)
        self._manager._conversations[conv_key] = last_used if last_used is not None else time.time()
        return result

    def _scan_keep_states_impl(self) -> list[tuple[str, str, str, str, str, float]]:
        """Blocking scan for keep runs (#1114): read every ``state.json``
        under the subagents dir and collect the promoted conversations.

        Returns ``(conv_id, conv_key, sid, provider, cwd, last_used)`` tuples.
        Runs in an executor — no event-loop work here.
        """
        out: list[tuple[str, str, str, str, str, float]] = []
        try:
            base = _subagents_dir()
            entries = list(base.iterdir()) if base.is_dir() else []
        except Exception:
            return out
        for d in entries:
            try:
                if not d.is_dir():
                    continue
                state = read_state(d.name)
                if not isinstance(state, dict):
                    tombstone = self._persistence.read_tombstone(d.name) or {}
                    sid = str(tombstone.get("session_id") or "")
                    if sid:
                        # Agent-folder tombstones can preserve retention/exemption
                        # hints, never provider-deletion authority.
                        self._persistence.publish_live_cleanup_hint(d.name)
                    continue
                if state.get("keep") is not True:
                    continue
                conv_key = str(state.get("conversation_key") or "") or f"subagent:{d.name}"
                conv_id = self._persistence.subagent_id_from_conversation_key(conv_key)
                if conv_id is None:
                    continue
                sid = str(state.get("session_id") or "")
                trusted_identity = self._persistence.trusted_cleanup_identity_record(
                    d.name,
                    sid,
                    conv_key,
                )
                if trusted_identity is None:
                    # The canonical disk reader consumes agent-writable state and
                    # must match protected authority. Tests/embedders may inject a
                    # different reader as their own trusted authority; retaining
                    # that established seam does not make on-disk state trusted.
                    if read_state is self._persistence.read_state:
                        continue
                    trusted_identity = {
                        "provider": state.get("provider"),
                        "cwd": state.get("cwd"),
                    }
                last_used = float(state.get("updated_at") or state.get("started") or 0.0)
                out.append(
                    (
                        conv_id,
                        conv_key,
                        sid,
                        str(trusted_identity.get("provider") or PROVIDER_LABEL_DEFAULT),
                        str(trusted_identity.get("cwd") or ""),
                        last_used,
                    )
                )
            except Exception:
                logger.debug("registry rebuild: skipping %s", d, exc_info=True)
        return out

    async def _rebuild_conversation_registry_impl(self) -> None:
        """Re-seed the conversation TTL registry from disk after a restart (#1114).

        The registry (``_conversations`` + the SessionManager continuable
        cache + session map) is in-memory; without this, a gateway restart
        orphans promoted conversations — the TTL sweep no longer knows them,
        and nothing else deletes their session files (the tombstone pruner
        skips keep runs by design). Runs on the reaper's first pass (retried
        until it succeeds); entries already past TTL are released by the
        very next sweep.

        Threading contract (Arbiter, PR #1246): ONLY the pure-read
        ``_scan_keep_states`` runs in the executor. All ``SessionMap``
        access (``resumable_sid`` self-prune, ``seed_conversation`` writes)
        stays on the event loop — the map is an unlocked dict with
        whole-file saves, concurrently mutated by ``get_or_create`` /
        ``close_all``, so touching it from a worker thread races restart
        cold-starts (lost mappings / dict-changed-size errors). Per-entry
        work is small and bounded by the keep-run count, and the loop
        yields between entries so a large batch cannot stall chat turns
        (the round-2 event-loop concern).
        """
        loop = asyncio.get_running_loop()
        found = await loop.run_in_executor(None, self._manager._scan_keep_states)
        # Newest record wins: a conversation appears once per run that
        # touched it (original + each continuation). The first record kept
        # for a conv_key wins the `in self._conversations` guard below, so
        # iterate newest-first — an oldest-first order would seed a stale
        # last_used and let the SAME pass's sweep expire (and delete) a
        # conversation whose real last-use is recent.
        found.sort(key=lambda t: t[5], reverse=True)
        seeded = 0
        for conv_id, conv_key, sid, provider, cwd, last_used in found:
            if conv_key in self._manager._conversations:
                continue  # live registration wins over the disk snapshot
            # Same on-demand seeding as continue_conversation (also on-loop):
            # the map entry is what makes release_conversation able to find
            # and delete files.
            if sid and not self._manager._sessions.resumable_sid(conv_key):
                self._manager._sessions.seed_conversation(conv_key, sid, provider=provider, cwd=cwd)
            # Resumability gate: SessionMap.get self-prunes entries whose
            # session files are missing, so this also rejects RELEASED
            # conversations whose continuation runs still carry a stale
            # keep=True in their own state.json — their files are gone, and
            # re-owning them would resurrect a released conversation.
            if not self._manager._sessions.resumable_sid(conv_key):
                continue
            self._manager._sessions.mark_continuable(conv_key)
            self._manager._conversations[conv_key] = last_used or time.time()
            seeded += 1
            # Cooperative yield: keep restart cold-start turns responsive
            # while a large keep batch seeds (one small file write each).
            await asyncio.sleep(0)
        if seeded:
            logger.info("Rebuilt conversation TTL registry from disk: %d conversation(s)", seeded)

    def continue_conversation_impl(
        self,
        conv_id: str,
        task: str,
        parent_session_key: str = "",
        agent: str = "",
        model: str | None = None,
        max_turns: int = 0,
        cwd: str = "",
        _preassigned_id: str = "",
    ) -> SubagentInfo | None:
        """Dispatch a follow-up *task* into conversation *conv_id*.

        ``_preassigned_id`` mirrors ``spawn``: a caller that must persist the
        dispatch identity BEFORE the side effect (so a crash in between is
        recoverable rather than ambiguous) supplies the id it already wrote
        down, instead of discovering the minted one only on return.

        Retain-by-default: works on ANY completed run whose session files are
        still on disk — no keep flag needed at spawn time. Every run's sid /
        provider / cwd are already recorded in its ``state.json``; this seeds
        the session map on demand, so ``get_or_create`` finds the sid and arms
        ``session/load``. Continuing a run PROMOTES it: retention extends from
        the tombstone-prune window (~1h) to the conversation TTL, until
        ``spawn_release``.

        Mints a NEW run (new id, own state.json / result.txt / completion
        event) on the SAME session key, so the follow-up executes with the
        conversation's accumulated context.

        Typed failures (returned as a done SubagentInfo with ``error``):
        - ``conversation_busy`` — a run is in flight; use spawn_steer.
        - ``conversation_gone`` — no resumable session files remain.
        """
        conv_key = f"subagent:{conv_id}"
        busy = self._manager._conversation_busy(conv_key)
        if busy is not None:
            info = SubagentInfo(
                id=uuid.uuid4().hex[:8],
                task=_redact(task),
                done=True,
                parent_session_key=parent_session_key,
                error=(
                    f"conversation_busy: run {busy.id} is still settling a state "
                    "write on this conversation — retry shortly"
                    if busy._state_writer_abandoned
                    else (
                        f"conversation_busy: run {busy.id} is in flight on this "
                        "conversation — use spawn_steer to inject into it, or wait "
                        "for its completion event"
                    )
                ),
            )
            return info
        # Seed the session map from the run's state.json when no mapping
        # exists yet (default runs never write one at spawn; the map is also
        # in-memory-lost across gateway restarts while state.json persists).
        if not self._manager._sessions.resumable_sid(conv_key):
            state = read_state(conv_id) or {}
            sid = str(state.get("session_id") or "")
            if sid:
                self._manager._sessions.seed_conversation(
                    conv_key,
                    sid,
                    provider=str(state.get("provider") or PROVIDER_LABEL_DEFAULT),
                    cwd=str(state.get("cwd") or ""),
                )
        # Re-check: SessionMap.get self-prunes entries whose session files
        # are missing, so a surviving mapping == resumable files on disk.
        if not self._manager._sessions.resumable_sid(conv_key):
            # Point the caller at the prior result if the run folder survives
            # (result.txt outlives the session under the tombstone TTL).
            result_hint = ""
            try:
                _rp = agent_dir_for_display(conv_id) / "result.txt"
                if _rp.exists():
                    result_hint = f" Prior result still readable at: {_rp}"
            except Exception:
                pass
            info = SubagentInfo(
                id=uuid.uuid4().hex[:8],
                task=_redact(task),
                done=True,
                parent_session_key=parent_session_key,
                error=(
                    "conversation_gone: no resumable session remains for "
                    f"{conv_id} (expired, released, or files pruned)."
                    + result_hint
                    + " Re-spawn with a fresh task carrying a summary."
                ),
            )
            return info
        # Promote the run's retention through the single choke point
        # (#1115): state.json keep=True (tombstone pruner skips deletion),
        # the SessionManager continuable cache, and the TTL registry entry.
        # The conversation TTL sweep / spawn_release owns deletion from here.
        # Snapshot existing ownership: a retryable promotion attempt must undo
        # only state it introduced, never erase an earlier keep/continuation.
        was_continuable = self._manager._sessions.is_continuable(conv_key)
        had_previous_last_used = conv_key in self._manager._conversations
        previous_last_used = self._manager._conversations.get(conv_key, 0.0)
        # The facade forwards the enum result directly. Legacy tests and external
        # monkeypatches that return None/non-enum retain the historical promoted
        # behavior; only an explicit RETRYABLE outcome alters dispatch.
        promotion = self._manager._promote_conversation(  # type: ignore[func-returns-value]
            conv_id, conv_key
        )
        if promotion is self._persistence.RetentionPromotionResult.RETRYABLE:
            if not was_continuable:
                self._manager._sessions.unmark_continuable(conv_key)
            if not had_previous_last_used:
                self._manager._conversations.pop(conv_key, None)
            else:
                self._manager._conversations[conv_key] = previous_last_used
            return SubagentInfo(
                id=uuid.uuid4().hex[:8],
                task=_redact(task),
                done=True,
                parent_session_key=parent_session_key,
                error=(
                    "conversation_busy: retention promotion is temporarily "
                    f"unavailable for {conv_id}; retry the continuation"
                ),
            )
        inc_memory, inc_lessons, inc_project = self._manager._inherited_context_groups(conv_id)
        # A continuation has to run WHERE THE RUN RAN. `spawn` resolves an empty
        # cwd to the pool project before it validates the agent name, so a run
        # spawned against a project-local agent (defined under that project's
        # .kiro/agents/) came back "unknown agent" here — and the caller reads any
        # non-busy error as unresumable and respawns from the digest alone,
        # silently dropping the conversation this call exists to preserve.
        #
        # The cwd must come from the CALLER, not be discovered here. This method is
        # synchronous and runs on the gateway's event loop, so probing the recorded
        # path (`is_dir()`) would freeze the gateway for as long as a stalled
        # network mount takes to answer. Async callers resolve it off-loop instead:
        # crew passes its slot project, and `recorded_cwd()` gives the others the
        # run's own recorded path to hand back in.
        return self._manager.spawn(
            task,
            _preassigned_id=_preassigned_id,
            parent_session_key=parent_session_key,
            agent=agent,
            model=model,
            max_turns=max_turns,
            keep=True,
            cwd=cwd,
            conversation_key=conv_key,
            include_memory=inc_memory,
            include_lessons=inc_lessons,
            include_project=inc_project,
        )

    def recorded_cwd_impl(self, conv_id: str) -> str:
        """The cwd run *conv_id* executed in, or "" if it never had one.

        `continue_conversation` deliberately does NOT discover this itself: it is
        synchronous and runs on the gateway's event loop, where the state read would
        block for as long as a stalled network mount takes to answer. This helper
        does the blocking work in one place so an async caller can hand it to
        `asyncio.to_thread` and pass the result in.

        A path that no longer exists is returned ANYWAY, so `spawn` refuses it.
        Filtering it to "" would keep such a continuation working but is unsafe:
        an empty cwd resolves to the POOL project, so a follow-up
        whose task names relative files would have edited an unrelated project's
        working tree. A loud refusal is recoverable; a silent write to the wrong
        repository is not. Only a run that never recorded a cwd returns "" — for it
        the pool default is correct, because there is no project to miss.
        """
        return str((read_state(conv_id) or {}).get("cwd") or "")

    def _inherited_context_groups_impl(self, conv_id: str) -> tuple[bool, bool, bool]:
        """Recover the context scope of the run being continued.

        A continuation DOES rebuild session context: ``get_or_create`` reports
        ``is_new=True`` even when it restores the session via ``session/load``
        (``resumed`` is the separate flag, and it gates only thread history), so
        ``build_message`` runs the full session-context path for the follow-up
        turn. Without inheriting the scope here, a run the parent deliberately
        spawned without memory would silently regain it on continuation.

        Prefers the live record; falls back to the scope persisted in the run's
        ``state.json``. A run that predates the field records no scope at all,
        which is distinguishable from "every group withheld" (an empty string)
        and defaults to all-on.
        """
        live = self._manager._agents.get(conv_id)
        if live is not None:
            return live.include_memory, live.include_lessons, live.include_project
        raw = (read_state(conv_id) or {}).get("context_groups")
        if raw is None:
            return True, True, True
        groups = {g for g in str(raw).split(",") if g}
        return (
            CONTEXT_GROUP_MEMORY in groups,
            CONTEXT_GROUP_LESSONS in groups,
            CONTEXT_GROUP_PROJECT in groups,
        )

    async def steer_run_impl(self, agent_id: str, message: str) -> tuple[bool, str]:
        """Inject *message* into the RUNNING turn of run *agent_id*.

        Returns ``(ok, detail)``. Typed detail values on refusal:
        ``not_found`` (unknown id), ``not_running`` (run finished — use
        spawn_continue), ``session_starting`` (run alive but its session has
        not registered yet — retry shortly), ``no_session`` (session not
        reachable), or the provider's failure reason.

        Startup grace (#1113): the window between spawn-return and session
        registration is precisely when a parent most wants to steer (it just
        realized the task text was wrong), so a missing provider on a live
        run polls for up to ``_STEER_STARTUP_WAIT_SECS`` instead of failing
        immediately with a bare ``no_session``.
        """
        info = self._manager._agents.get(agent_id)
        if info is None:
            return False, "not_found"
        if info.done:
            return False, "not_running: run finished — use spawn_continue"

        def _resolve_provider() -> Any:
            if info._session_sharing and info._shared_provider is not None:  # type: ignore[union-attr]
                return info._shared_provider  # type: ignore[union-attr]
            session_key = info.conversation_key or f"subagent:{info.id}"  # type: ignore[union-attr]
            return self._manager._sessions.get_provider(session_key)

        provider: Any = _resolve_provider()
        if provider is None or not hasattr(provider, "steer"):
            # Bounded wait for session registration on a run that is still
            # alive. Re-checks done-ness each tick: a run finishing while we
            # wait flips the answer to not_running, never a stale inject.
            deadline = time.monotonic() + _STEER_STARTUP_WAIT_SECS
            while time.monotonic() < deadline:
                await asyncio.sleep(_STEER_STARTUP_POLL_SECS)
                if info.done:
                    return False, "not_running: run finished — use spawn_continue"
                provider = _resolve_provider()
                if provider is not None and hasattr(provider, "steer"):
                    break
            else:
                return False, (
                    "session_starting: the run is alive but its session has "
                    f"not registered within {_STEER_STARTUP_WAIT_SECS}s — "
                    "retry in a few seconds"
                )
        try:
            ok = await provider.steer(message)
        except Exception as exc:  # pragma: no cover - provider-specific
            logger.warning("steer_run %s failed", agent_id, exc_info=True)
            return False, f"steer failed: {exc}"
        if ok:
            try:
                sel().log_tool_invocation(
                    session_key=info.parent_session_key or "",
                    source="subagent",
                    tool_name="spawn_steer",
                    outcome="ok",
                    metadata={"subagent_id": agent_id},
                )
            except Exception:
                logger.debug("steer_run: SEL audit failed", exc_info=True)
        return ok, "ok" if ok else "steer rejected by provider"

    async def follow_up_run_impl(self, agent_id: str, message: str) -> tuple[bool, str]:
        """Queue *message* for delivery AFTER run *agent_id*'s turn completes.

        The non-interrupting sibling of :meth:`steer_run` (spawn_steer
        ``mode="follow_up"``): instead of injecting into the running turn —
        which can derail critical work mid-execution — the message waits for
        the run to finish and is then dispatched as a CONTINUATION on the
        run's own conversation (``continue_conversation``), executing with its
        accumulated context. The continuation is a new run whose result
        arrives as a normal completion event on the same parent session.

        Multiple queued follow-ups drain as ONE continuation (joined in
        arrival order), so three corrections cost one run, not three.

        Returns ``(ok, detail)``. Typed refusals mirror ``steer_run``:
        ``not_found`` (unknown id) and ``not_running`` (already finished —
        ``spawn_continue`` is the direct tool for that case). Queued
        follow-ups are best-effort by design: if the conversation is gone by
        the time the run ends, the failure is logged and audited, not raised.
        """
        info = self._manager._agents.get(agent_id)
        if info is None:
            return False, "not_found"
        if info.done:
            return False, "not_running: run finished — use spawn_continue"
        if self._manager._shutting_down:
            # Refuse rather than accept-and-drop: an accepted follow-up
            # promises a completion event, and a shutting-down gateway can
            # keep neither the watcher nor the continuation alive.
            return False, "shutting_down: the gateway is stopping — re-send after restart"
        info.pending_followups.append(message)
        if not info._followup_watcher:
            self._manager._arm_followup_watcher(info)
        try:
            sel().log_tool_invocation(
                session_key=info.parent_session_key or "",
                source="subagent",
                tool_name="spawn_steer",
                outcome="followup_queued",
                metadata={"subagent_id": agent_id, "queued": len(info.pending_followups)},
            )
        except Exception:
            logger.debug("follow_up_run: SEL audit failed", exc_info=True)
        return True, "queued"

    def _arm_followup_watcher_impl(self, info: SubagentInfo) -> None:
        """Arm the (single) follow-up watcher for *info*'s run.

        The done-callback resets the one-watcher latch AND re-arms when
        messages are still pending: a follow-up can be accepted while the
        previous watcher is inside its final awaits (announcing an expiry) —
        it sees the latch still true and arms nothing, so without the re-arm
        that accepted message would be stranded with no dispatch and no event.
        Not re-armed during shutdown or once the run record is
        gone (removal drops any leftovers deliberately).
        """
        info._followup_watcher = True
        run_id = info.id
        run_info = info  # narrowed local: mypy loses the None-narrow in closure defaults
        task = asyncio.create_task(self._manager._deliver_followups(info))
        self._manager._followup_watchers[run_id] = task

        def _done(t: "asyncio.Task", _id: str = run_id, _info: SubagentInfo = run_info) -> None:
            self._manager._followup_watchers.pop(_id, None)
            _info._followup_watcher = False
            if not t.cancelled() and t.exception() is not None:
                logger.warning("follow_up watcher for %s failed", _id, exc_info=t.exception())
                return
            if (
                not t.cancelled()
                and _info.pending_followups
                and not self._manager._shutting_down
                and _id in self._manager._agents
            ):
                self._manager._arm_followup_watcher(_info)

        task.add_done_callback(_done)

    async def _deliver_followups_impl(self, info: SubagentInfo) -> None:
        """Watch run *info* until its turn completes, then dispatch the queue.

        DELIBERATELY a per-run poller rather than a hook in ``_run``'s
        finalization: completion is reached from many terminal paths (normal,
        error, timeout, cancel-recovery, reaper), all guarded by a carefully
        ordered 3-guard finally — a watcher observes the outcome without
        adding a new obligation to any of them. Waits for the run's task to be
        popped from ``self._tasks`` too, so teardown (session release) has
        finished before the continuation tries to reuse the conversation; any
        residual ``conversation_busy`` gets a bounded retry.

        OUTCOME-AWARE: a run the user explicitly STOPPED does not get its
        follow-ups dispatched — resurrecting work the user killed is the
        opposite of "the correction can wait" (``followup_suppressed`` audit).
        Other non-success terminals (error, timeout) still dispatch: the
        continuation runs with the conversation's context, so "fix what just
        broke" is a legitimate follow-up.

        NEVER SILENT: the spawn_steer reply promised the parent a completion
        event, so every path that cannot deliver one from a real continuation
        (suppressed, expired, dispatch failure) announces a SYNTHETIC failure
        completion event through the normal ``_on_done`` path — the parent
        must not wait forever on an event that is not coming.

        Hard-bounded and manager-owned: gives up at the manager's run timeout
        plus a margin, and the task is registered in ``_followup_watchers`` so
        ``cancel_all()`` cancels it — a watcher must never dispatch a fresh
        run into a shutting-down gateway.
        """
        deadline = time.monotonic() + self._manager._default_timeout + 300
        while time.monotonic() < deadline:
            if info.done and info.id not in self._manager._tasks:
                break
            await asyncio.sleep(self._manager._FOLLOWUP_POLL_SECS)
        else:
            dropped = list(info.pending_followups)
            logger.warning(
                "follow_up watcher for %s timed out before the run completed — "
                "%d queued message(s) dropped",
                info.id,
                len(dropped),
            )
            self._manager._audit_followup(info, "followup_expired")
            await self._manager._announce_followup_failure(
                info,
                "follow_up expired: the run never completed within its timeout "
                "window; the queued follow-up message(s) were dropped",
                messages=dropped,
            )
            # Drop ONLY what was just reported dropped, and only AFTER the
            # announce settled — clearing first meant a shutdown cancelling
            # this task mid-announce left the queue empty for cancel_all()'s
            # sweep, so the messages vanished with no event. The slice keeps
            # anything queued while we were announcing (the done-callback
            # re-arms for it).
            info.pending_followups = info.pending_followups[len(dropped) :]
            return
        # SNAPSHOT, do not drain: messages stay in ``pending_followups`` until
        # their outcome is SETTLED (dispatched, or their failure announced).
        # An eager drain lost messages when shutdown landed mid-await — e.g.
        # during a conversation_busy retry sleep — because cancel_all() saw an
        # empty queue, cancelled this task, and nothing was ever announced.
        # Appends only ever happen at the tail, so removing the first
        # ``len(messages)`` entries at settlement drops exactly this snapshot
        # and preserves anything queued while we were dispatching.
        messages = list(info.pending_followups)
        if not messages:
            return

        def _settle() -> None:
            info.pending_followups = info.pending_followups[len(messages) :]

        if info.user_stopped:
            logger.info("follow_up for %s suppressed — the user stopped the run", info.id)
            self._manager._audit_followup(info, "followup_suppressed")
            _settle()
            await self._manager._announce_followup_failure(
                info,
                "follow_up suppressed: the user stopped this run, so its queued "
                "follow-up message(s) were NOT dispatched",
                messages=messages,
            )
            return
        if self._manager._shutting_down:
            # Leave the queue intact: cancel_all()'s shutdown sweep owns the
            # announce-and-drop for pending messages.
            return
        task = "\n\n---\n\n".join(messages)
        # Finalization may hold the conversation for a beat after the task is
        # popped (shielded report); retry a bounded number of times.
        for _attempt in range(self._manager._FOLLOWUP_BUSY_RETRIES):
            child = self._manager.continue_conversation(
                info.id,
                task,
                parent_session_key=info.parent_session_key,
                agent=info.agent,
            )
            err = "spawn_failed" if child is None else str(getattr(child, "error", "") or "")
            if not err.startswith("conversation_busy"):
                break
            await asyncio.sleep(self._manager._FOLLOWUP_BUSY_RETRY_SECS)
        if err:
            logger.warning("follow_up delivery for %s failed: %s", info.id, err.split(":", 1)[0])
            self._manager._audit_followup(info, "followup_failed")
            _settle()
            # continue_conversation's typed failures are already done
            # SubagentInfo records — announce the real one when we have it.
            if child is not None:
                await self._manager._announce_followup_failure(info, "", failure_info=child)
            else:
                await self._manager._announce_followup_failure(
                    info, f"follow_up dispatch failed: {err}"
                )
        else:
            self._manager._audit_followup(info, "followup_dispatched")
            _settle()

    async def _announce_followup_failure_impl(
        self,
        info: SubagentInfo,
        reason: str,
        failure_info: SubagentInfo | None = None,
        messages: list | None = None,
    ) -> None:
        """Deliver a SYNTHETIC failure completion event for an undeliverable
        follow-up, through the same ``_on_done`` path as real completions.

        Best-effort by design (a notification about a failure must not itself
        take anything down), but never silent-by-default: without this the
        parent — told by spawn_steer that a completion event would arrive —
        blocks its plan on an event that only ever existed in SEL logs.
        ``messages`` labels the synthetic event when the queue was already
        drained by the caller (the expiry path clears before announcing so a
        later watcher cannot resurrect messages reported dead).
        """
        if self._manager._on_done is None:
            return
        label_msgs = messages if messages is not None else info.pending_followups
        # The label joins the RAW messages and redacts the JOIN before any
        # bound: bounding first can split a credential at a cut into fragments
        # no redaction regex matches, and redacting per message would blind the
        # multi-line PEM pattern to a key whose header and footer sit in
        # DIFFERENT messages. The budget scales with the message count,
        # matching the former 120-chars-per-message cap.
        followup_label = _redact("; ".join(label_msgs))[: 120 * len(label_msgs)]
        synthetic = failure_info or SubagentInfo(
            id=uuid.uuid4().hex[:8],
            task=f"[follow_up of run {info.id}] " + (followup_label or "queued follow-up"),
            done=True,
            parent_session_key=info.parent_session_key,
            error=reason,
        )
        try:
            await self._manager._on_done(synthetic)
        except Exception:
            logger.warning("follow_up failure announce for %s failed", info.id, exc_info=True)

    def _audit_followup_impl(self, info: SubagentInfo, outcome: str) -> None:
        try:
            sel().log_tool_invocation(
                session_key=info.parent_session_key or "",
                source="subagent",
                tool_name="spawn_steer",
                outcome=outcome,
                metadata={"subagent_id": info.id},
            )
        except Exception:
            logger.debug("follow_up audit failed", exc_info=True)

    def release_conversation_impl(self, conv_id: str) -> tuple[bool, str]:
        """Release conversation *conv_id*: forget the sid and delete files.

        Refuses (``conversation_busy``) while a run is in flight. Returns
        ``(ok, detail)``.
        """
        conv_key = f"subagent:{conv_id}"
        busy = self._manager._conversation_busy(conv_key)
        if busy is not None:
            if busy._state_writer_abandoned:
                return (
                    False,
                    f"conversation_busy: run {busy.id} is still settling a state write",
                )
            return False, f"conversation_busy: run {busy.id} is in flight"
        provider_label = (
            self._manager._sessions.conversation_provider(conv_key) or PROVIDER_LABEL_DEFAULT
        )
        sid = self._manager._sessions.forget_conversation(conv_key)
        self._manager._conversations.pop(conv_key, None)
        # Demote the persisted source of truth too (#1115): with the disk
        # fallback in place, a stale keep=True would re-warm the continuable
        # cache after release and resurrect the conversation on the next
        # restart's registry rebuild.
        try:
            update_state(conv_id, keep=False)
        except Exception:
            logger.debug("release: failed to demote state for %s", conv_id, exc_info=True)
        if not sid:
            return False, "conversation_gone: nothing to release"
        try:
            _cleanup_session_files_sync(sid, provider_label)
        except Exception:
            logger.debug("release_conversation: file cleanup failed", exc_info=True)
        return True, "released"

    def _sweep_conversations_impl(self, now: float) -> None:
        """Reaper hook: expire continuable conversations idle past TTL."""
        for conv_key, last_used in list(self._manager._conversations.items()):
            if now - last_used < _CONVERSATION_TTL_SECS:
                continue
            if self._manager._conversation_busy(conv_key) is not None:
                self._manager._conversations[conv_key] = now  # active — refresh
                continue
            conv_id = self._persistence.subagent_id_from_conversation_key(conv_key)
            if conv_id is None:
                logger.warning("Dropping malformed conversation registry key %r", conv_key)
                self._manager._conversations.pop(conv_key, None)
                continue
            ok, detail = self._manager.release_conversation(conv_id)
            logger.info(
                "Conversation %s expired after %ds idle: %s",
                conv_id,
                _CONVERSATION_TTL_SECS,
                detail,
            )
