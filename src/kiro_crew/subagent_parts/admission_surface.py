"""The manager's spawn entry: admission of a new run (sync and async) and its spawn
approval, the in-startup population and its clock, the run-id draw, the memory-pressure
hold, the queue drain and dispatch, the queued-run and task-memory reads, and the
delegations into :class:`~kiro_crew.subagent_manager.SpawnAdmissionCoordinator`.

Each function takes the manager as ``self`` and is bound as the ``SubagentManager``
attribute of the same name in :mod:`kiro_crew.subagent_parts.manager`.

Manager fields read: ``_admission``, ``_admitting_waiting``, ``_agents``,
``_batch_progress_ts``, ``_batch_submitted``, ``_floor_deferred_ids``, ``_floor_waits``,
``_held_approval_modes``, ``_max_concurrent``, ``_memory_mode_for_session``, ``_monitor``,
``_run_events``, ``_session_start_concurrency``, ``_session_start_configured``,
``_sessions``, ``_startup_reservations``.

Manager fields written: none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import os
    import time

    from kiro_crew.subagent import (
        _RUN_ID_HEX_CHARS,
        _STARTUP_CAP_GATE_ROUNDS,
        AgentCheck,
        Any,
        Callable,
        ClaimPoint,
        KiroCrewConfig,
        Mapping,
        MemoryReadPoint,
        ParentSpawnPolicy,
        PreparedSpawn,
        QueuedRun,
        QueuedRunListing,
        SubagentInfo,
        SubagentManager,
        _host_memory_reading_off_loop,
        _inherited_member_agent,
        _redact,
        _validate_agent,
        _validate_app_agent_ownership,
        asyncio,
        cached_session_start_concurrency,
        contested_root,
        is_contested_root,
        logger,
        parent_spawn_policy,
        parent_template_allowlists,
        read_state,
        redact_credentials,
        redact_exfiltration_urls,
        sel,
        validate_cwd,
    )


def _mint_agent_id(self: SubagentManager) -> str:
    """Draw a run id: :data:`_RUN_ID_HEX_CHARS` hex characters of random bytes.

    Every spawn identity comes from here, which is the point -- one draw site
    is what lets the width be a single number.

    The width carries the uniqueness on its own, with nothing to remember and
    nothing to read. At 8 characters the id was 32 bits, so 2000 spawns on
    one host collided about once in 2,100 times, and the collision did not
    read as one: identity is assigned before registration, so the caller was
    handed the id and the accept then failed on the duplicate primary key,
    reaching the user as ``task store write failed`` -- naming a subsystem
    that was working correctly. 64 bits puts that at about 1 in 10**13.

    Drawn from ``os.urandom`` rather than a ``uuid4`` prefix. A v4 UUID spends
    its 13th hex character on the fixed version digit ``4``, so the first 16
    characters of one carry 60 random bits, not 64 -- a 16-fold worse bound
    than the width advertises, from a detail no reader of the slice can see.

    A checked narrow draw would need to know which ids are taken, and a
    durable task row outlives the process that wrote it, so it would have to
    ask the store -- which the spawn path cannot do, because taking a
    task-store connection on the event loop stalls every session's turn
    (:mod:`kiro_crew.on_loop_db` refuses it). Widening removes the question.
    """
    return os.urandom(_RUN_ID_HEX_CHARS // 2).hex()


# ── In-startup population ──────────────────────────────────────────────
#
# Three different things bound a fan-out: ``_spawn_stagger_secs`` bounds
# the RATE of starts, ``_max_concurrent`` bounds the RUNNING population,
# and ``_startup_cap`` bounds how many admitted agents may be IN STARTUP at
# once. Without the third, one start is admitted every stagger interval however long each
# takes, and when starts are slow (a dedicated process per model override,
# a queue at the session-start gate, a throttled provider handshake) dozens
# sit in startup together, every one of them contending for the same gate
# and every one of them running down the same fixed startup deadline.
# Measured on a 623-item fan-out: waves of 24-45 lost ~2%, a wave of 120
# lost ~50%, every loss a healthy start the watchdog reaped as
# "Failed to start within 120s".


def _in_startup(info: SubagentInfo) -> bool:
    """True while *info* has entered execution but has nothing to show yet.

    The same shape the startup watchdog reaps on (:meth:`_is_startup_stalled`),
    minus the clock: past ``_run_inner``'s first statement (``_exec_started``
    set), no runtime PID, no answer on its own session yet
    (:meth:`_leave_startup`), no turn, and not already ending. A queued
    spawn is not in it (not registered until admitted), and
    neither is an agent PARKED at the spawn-approval prompt
    (``_awaiting_approval``, ``_exec_started`` still ``None``): it is
    starting nothing, so it must not consume the startup bound -- counting
    it would let a handful of unanswered prompts hold every other spawn on
    the host, auto-approved ones from unrelated parents included. What has
    to be bounded is the moment its prompt resolves, since a bulk grant
    (``tool_approval:bulk_trust`` / ``bulk_yolo``) resolves EVERY pending
    prompt at once: a released agent therefore re-enters through the pump
    (:meth:`_admit_released_start`) and is metered into startup by the SAME
    stagger and in-startup checks a fresh spawn passes, so ``_startup_cap``
    holds through the flood without the parked agents ever counting.
    """
    return (
        not info.done
        and not info._reap_started
        and info._exec_started is not None
        and info.turns == 0
        and info._pid is None
        and info._first_stream_started is None
    )


def _startup_population(self: SubagentManager, *, exclude: SubagentInfo | None = None) -> int:
    """How many admitted agents are in startup right now (see :meth:`_in_startup`).

    Includes ``_startup_reservations``: a durable-store spawn reserves its
    slot BEFORE its claim is awaited (``ClaimPoint``, reserve-then-commit)
    and registers its ``SubagentInfo`` only on re-entry, which skips the
    admission gate because the reservation already passed it. Between
    reserve and re-entry the start is admitted but has no info to count, so
    the reservation is counted in its place; the re-entry's registration
    and ``release_reservation`` both give it back.
    """
    return int(self._startup_reservations) + sum(
        1 for info in self._agents.values() if info is not exclude and self._in_startup(info)
    )


def _startup_cap(self: SubagentManager) -> int:
    """The in-startup bound: ``_STARTUP_CAP_GATE_ROUNDS x session_start_concurrency``.

    Twice the session-start gate's width, clamped to ``[1, cap]``. The
    bound is tied to the GATE, not to the running cap, because the gate is
    the one resource every start in startup contends for: ``session/new``
    runs under ``G`` permits, so at most ``G`` starts make progress at any
    moment and every other admitted start is a spawned process (dedicated
    path) or a claimed slot holding nothing but a place in the gate's
    queue. Time spent in that queue is not charged to the startup deadline
    (the clock pauses at queue entry and resumes at acquisition --
    ``_gate_wait_mark`` / ``_gate_exit_reset``), so the queue's length is
    not what reaps a healthy start; what the bound decides is how much of
    the running cap may sit in startup contending for ``G`` permits at
    once. ``2G`` is the smallest value that never idles the gate -- one
    round holding permits and one round already admitted to take them the
    moment they free -- and admitting more buys no starts, since the gate
    serves ``G`` per round however many are queued: it only lengthens the
    queue and grows the population of admitted-but-idle starts. A
    cap-derived term (``max(..., ceil(cap / 4))``, say) would do exactly
    that: 16 in startup at a cap of 64 against a 2-permit gate is seven
    rounds queued for two permits. The population the bound is checked
    against (:meth:`_in_startup`) is the agents actually starting, plus
    reservations not yet registered (:meth:`_startup_population`). Agents
    parked at the spawn-approval prompt are NOT in it -- they start nothing
    and must not block unrelated spawns -- and are instead metered through
    the pump when their prompt resolves (:meth:`_admit_released_start`), so
    "at most ``2G`` in startup" holds even when a bulk trust/yolo grant
    releases them all at once.

    There is deliberately NO config key for this value. ``2G`` is both the
    floor and the ceiling of the useful range: fewer than ``2G`` idles the
    gate between rounds (no next round already admitted when the current
    one releases), and more than ``2G`` buys no starts -- the gate serves
    ``G`` per round however many are queued behind it -- only a longer
    queue of idle admitted starts. A knob could therefore only make the
    system worse, and the operator's real lever on this bound already
    exists: ``agent.session_start_concurrency`` sizes the gate, and the
    bound tracks it.

    Never above the effective running cap (a larger value could not bind)
    and never below 1, so at a cap of ``0`` this returns ``1``: the running
    cap, not this bound, is what pauses admission there.
    """
    cap = max(0, int(self._max_concurrent))
    bound = _STARTUP_CAP_GATE_ROUNDS * self._resolved_session_start_width()
    return max(1, min(bound, cap))


def _resolved_session_start_width(self: SubagentManager) -> int:
    """The session-start gate width as an in-memory read (no probe, no lock)."""
    if self._session_start_concurrency is not None:
        return int(self._session_start_concurrency)
    try:
        return max(1, cached_session_start_concurrency(self._session_start_configured))
    except Exception:
        return 2


def _note_startup_progress(self: SubagentManager, info: SubagentInfo) -> None:
    """Wake the spawn queue when *info* leaves startup without ending.

    A runtime PID (``_run_inner``'s PID record, ``_bind_shared_handle``) or
    the first answer on its own session (:meth:`_leave_startup`) takes
    *info* out of the in-startup population, which may open a slot under
    :meth:`_startup_cap` that no other edge announces: the slot-release
    drain fires only on a terminal, and the pump does not poll. Called at
    those two transitions -- at most twice per start -- and the pump
    returns at once when nothing waits and re-checks every gate itself, so
    a call that opens nothing is cheap. A pump failure is logged, never
    raised: the run that just progressed is not the one at fault, and the
    next terminal or arrival pumps again.

    Also restarts the activity clock: the first-prompt silence window
    (``_FIRST_PROMPT_SILENT_SECS``) measures from here, so the handshake
    that preceded the PID is never charged to it.
    """
    info.last_activity = time.time()
    info._last_activity_mono = time.monotonic()
    try:
        self._drain_queue()
    except Exception:
        logger.debug("startup-progress queue pump failed for %s", info.id, exc_info=True)


def _leave_startup(self: SubagentManager, info: SubagentInfo) -> None:
    """Take *info* out of startup: its own session has answered its prompt.

    Two moments prove that, and each calls this: the first frame out of the
    provider stream that is addressed to THIS session (the stream loop in
    ``_run_inner``), and a dependency verdict on the prompt
    (``_yield_for_dependency``), which
    the durable row records as ``starting -> running`` in the same step.
    An opened stream proves neither, and neither does a ``runtime_global``
    frame -- a co-tenant's traffic that a shared runtime fanned out to
    every session on it. Stamps ``info._first_stream_started`` once per
    execution and, on a first turn, wakes a spawn the in-startup bound is
    holding (:meth:`_note_startup_progress`).
    """
    if info._first_stream_started is not None:
        return
    info._first_stream_started = time.time()
    info._first_stream_mono = time.monotonic()
    info._first_stream_generation = info._rss_generation
    if info.turns == 0:
        self._note_startup_progress(info)


def running_agents_for(self: SubagentManager, parent_key: str) -> list[dict]:
    return self._run_events.running_agents_for_impl(parent_key)


def running_agents_rooted_at(self: SubagentManager, root_key: str) -> list[dict]:
    return self._run_events.running_agents_rooted_at_impl(root_key)


def live_root_session_keys(self: SubagentManager) -> set[str]:
    """The tab roots (:meth:`root_session_key_for`) of every live run, resolved once.

    For a caller that asks "does this tab have a run anywhere in its tree?"
    for EVERY tab in one pass -- the sidebar's ``subagents_running`` badge on
    each slots push. Asking :meth:`running_agents_rooted_at` per tab would
    resolve every live run's root once per tab and build a redacted summary
    list each time, for a boolean; this resolves each run once, and the
    caller tests membership.
    """
    return {self.root_session_key_for(a) for a in self._agents.values() if not a.done}


def task_memory_rows(self: SubagentManager) -> list[dict[str, object]]:
    return self._monitor.task_memory_rows_impl()


def spawn(
    self: SubagentManager,
    task: str,
    parent_session_key: str = "",
    agent: str = "",
    max_turns: int = 0,
    model: str | None = None,
    reasoning_effort: str = "",
    allowed_tools: list[str] | None = None,
    bare: bool = False,
    cwd: str = "",
    approval_mode: str | None = None,
    silent: bool = False,
    batch_id: str = "",
    batch_total: int = 0,
    keep: bool = False,
    conversation_key: str = "",
    app: str = "",
    include_memory: bool = True,
    include_lessons: bool = True,
    include_project: bool = True,
    memory_store: str = "",
    _agent_prevalidated: bool = False,
    _from_queue: bool = False,
    _preassigned_id: str = "",
    _crew_log_asked: "tuple[str, int] | None" = None,
    _memory_mode: str | None = None,
    _store_accepted: bool = False,
    _stop_before_claim: bool = False,
    _claimed: "tuple[int, bool, str] | None" = None,
    _window_hint: "bool | None" = None,
    _child_registration: bool = True,
    _root_session_key: str = "",
    _conversation_root_session_key: str = "",
    _durable_conversation_root: str | None = None,
    *,
    crew: str = "",
    target_member: str | None = None,
    delegation: dict[str, str] | None = None,
    backend: str | None = None,
    label: str = "",
    _execution_context: dict | None = None,
    _parent_spawn_policy: "ParentSpawnPolicy | None" = None,
    _agent_check: "AgentCheck | None" = None,
    _recovering_row: bool = False,
    _stop_before_memory_read: bool = False,
    _memory_reading: "tuple[float, str] | None" = None,
) -> SubagentInfo | None:
    result = self._admission.spawn_impl(
        task,
        parent_session_key,
        agent,
        max_turns,
        model,
        reasoning_effort,
        allowed_tools,
        bare,
        cwd,
        approval_mode,
        silent,
        batch_id,
        batch_total,
        keep,
        conversation_key,
        app,
        include_memory,
        include_lessons,
        include_project,
        memory_store,
        _agent_prevalidated,
        _from_queue,
        _preassigned_id,
        _crew_log_asked=_crew_log_asked,
        _memory_mode=_memory_mode,
        _store_accepted=_store_accepted,
        _stop_before_claim=_stop_before_claim,
        _claimed=_claimed,
        _window_hint=_window_hint,
        _child_registration=_child_registration,
        _root_session_key=_root_session_key,
        _conversation_root_session_key=_conversation_root_session_key,
        _durable_conversation_root=_durable_conversation_root,
        crew=crew,
        target_member=target_member,
        delegation=delegation,
        backend=backend,
        label=label,
        _execution_context=_execution_context,
        _parent_spawn_policy=_parent_spawn_policy,
        _agent_check=_agent_check,
        _recovering_row=_recovering_row,
        _stop_before_memory_read=_stop_before_memory_read,
        _memory_reading=_memory_reading,
    )
    assert not isinstance(result, PreparedSpawn)
    # Every synchronous gate return (started, queued, or refused) receives
    # the same admission snapshot before a scheduled announce can run.
    # ``ClaimPoint`` comes back ONLY for ``_stop_before_claim=True`` and
    # ``MemoryReadPoint`` ONLY for ``_stop_before_memory_read=True``, whose
    # callers are the event-loop entries (``spawn_async`` and the coroutine
    # pump); every other caller receives a ``SubagentInfo`` or None as
    # declared.
    return result  # type: ignore[return-value]


async def _spawn_after_memory_read(
    self: SubagentManager,
    first: Any,
    proceed: "Callable[[], bool] | None" = None,
    /,
    **reentry: Any,
) -> Any:
    """Finish a spawn the gate stopped at its memory read, else return *first*.

    The host is read on a worker (:func:`_host_memory_reading_off_loop`) and
    the gate re-entered from the params its first half built, with the
    caller's own re-entry flags (*reentry*) on top, so the floor is never
    read on the event loop. Anything else -- a refusal, a queued handle, a
    ``ClaimPoint`` -- passes through untouched. *proceed*, when given, is
    asked after the read: a False answer (the row was stopped while the
    read ran) re-enters nothing and returns None.
    """
    if not isinstance(first, MemoryReadPoint):
        return first
    reading = await _host_memory_reading_off_loop(first.min_gb)
    if proceed is not None and not proceed():
        return None
    return self.spawn(**{**first.params, **reentry}, _memory_reading=reading)


def prepare_spawn(
    self: SubagentManager, task: str, **kwargs: Any
) -> "SubagentInfo | PreparedSpawn | MemoryReadPoint | None":
    """Run every policy gate of :meth:`spawn` and return the row to persist
    instead of starting anything. A refusal comes back as the same done
    ``SubagentInfo`` :meth:`spawn` would return; ``None`` is the legacy
    at-capacity answer. A spawn with no row to persist runs the whole gate
    here, and with ``_stop_before_memory_read`` stops at its memory read."""
    kwargs.pop("_from_queue", None)
    kwargs.pop("_store_accepted", None)
    prepared = self._admission.spawn_impl(task, _prepare_only=True, **kwargs)
    assert not isinstance(prepared, ClaimPoint)  # never requested here
    return prepared


async def _check_agent_off_loop(
    self: SubagentManager,
    agent: str,
    cwd: str,
    *,
    app: str = "",
    execution_context: "Mapping[str, Any] | None" = None,
    prevalidated: bool = False,
) -> "AgentCheck | None":
    """The gate's two agent-directory checks for *agent*, on a worker thread.

    App ownership (when *app* is set) and ``_validate_agent`` in the cwd the
    run will use, keyed by their inputs so the gate uses the answer only for
    the same ``(agent, cwd, app)`` (``AgentCheck``). An explicit *cwd* is
    canonicalized here exactly as the gate resolves it (``validate_cwd``
    against the configured roots), and the app is the one the gate settles
    on (the captured *execution_context*'s app wins over the caller's, as
    ``resolve_spawn_execution`` does), so the keys match. None when there is
    nothing to check, or when the cwd will be refused anyway."""
    if prevalidated:
        return None
    named = agent
    context = execution_context or {}
    selection_kind = str(context.get("selection_kind") or "")
    template_id = str(context.get("template_id") or "")
    if not named and not _inherited_member_agent(selection_kind, template_id):
        return None
    app = str(context.get("app") or "") or app
    pool_cwd = str(getattr(self._sessions, "_pool_cwd", "") or "")

    def _check() -> "AgentCheck | None":
        effective = pool_cwd
        if cwd:
            try:
                roots = KiroCrewConfig.load().agent.subagent_cwd_allowed_roots
            except Exception:
                roots = []  # the gate fails closed the same way
            resolved, err = validate_cwd(cwd, roots)
            if err:
                return None
            effective = resolved
        checked = named or _inherited_member_agent(selection_kind, template_id, effective)
        if not checked:
            return None
        owner_err = _validate_app_agent_ownership(checked, app) if app and named else ""
        return (
            checked,
            effective,
            app,
            owner_err or "",
            *_validate_agent(checked, effective),
        )

    return await asyncio.to_thread(_check)


async def spawn_async(self: SubagentManager, task: str, **kwargs: Any) -> SubagentInfo | None:
    """:meth:`spawn` for event-loop callers (``/api/spawn``).

    Write-before-ack with the write OFF the loop: the policy gates run
    first (``prepare_spawn``), the row is written on the store's dedicated
    writer thread (``TaskStore.run``), and only then does the sync
    ``spawn`` start the run with ``_store_accepted=True`` -- the SQLite
    lock wait never blocks the loop, and the caller is still acked only
    once the row exists. Without a durable store this is plain ``spawn``.
    """
    # Snapshot loop-owned policy inputs before reading a missing durable
    # carrier. The admission gate below still runs on-loop after the await.
    if (
        not isinstance(task, str)
        or not task.strip()
        or getattr(self._sessions, "admission_closed", False) is True
    ):
        return self.spawn(task, **kwargs)
    conversation_for_root = str(kwargs.get("conversation_key") or "")
    if conversation_for_root and kwargs.get("_durable_conversation_root") is None:
        # A continuation compares against its conversation's founding root.
        # When no in-memory record of the conversation remains (restart,
        # eviction) that root lives in the founder's run record, so it is
        # read here, off the loop, and handed to the gate as a value; the
        # gate itself reads nothing. Unknown (no record, pre-stamp record)
        # is ``""``, which the gate fails closed on.
        kwargs["_durable_conversation_root"] = await asyncio.to_thread(
            self._durable_conversation_root, conversation_for_root
        )
    if kwargs.get("_execution_context") is None:
        from kiro_crew.execution_context import read_session_execution
        from kiro_crew.subagent_persistence import read_run_execution

        parent = str(kwargs.get("parent_session_key") or "")
        conversation = str(kwargs.get("conversation_key") or "")
        mode = kwargs.get("_memory_mode")
        inherited = None
        try:
            if mode is None:
                resolver = self._memory_mode_for_session
                mode = resolver(parent) if resolver is not None else "persistent"
            if not isinstance(mode, str) or mode not in {
                "persistent",
                "incognito",
                "temporary",
            }:
                raise ValueError("unknown memory mode")
            if parent and not kwargs.get("agent") and not conversation:
                inherited = self._sessions.get_agent_selection(parent)
            record_id = (conversation or parent).removeprefix("subagent:")
            live = (
                self._agents.get(record_id)
                if (conversation or parent).startswith("subagent:")
                else None
            )
            record = live.execution_context if live is not None else None
            if record is None:
                record = (
                    await asyncio.to_thread(read_run_execution, record_id)
                    if conversation
                    else await asyncio.to_thread(read_session_execution, parent)
                )
            if kwargs.get("_parent_spawn_policy") is None and not conversation:
                # The PARENT record is in hand: its template names the spec
                # whose ``availableAgents`` the gate honours, so the
                # declaration is read here, off-loop, without a second
                # record read.
                template = record.template_id if record is not None else ""
                kwargs["_parent_spawn_policy"] = (
                    template,
                    (
                        await asyncio.to_thread(parent_template_allowlists, parent, template)
                        if template
                        else ()
                    ),
                )
            execution = self._admission.resolve_spawn_execution(
                parent_session_key=parent,
                conversation_key=conversation,
                agent=kwargs.get("agent", ""),
                memory_store=kwargs.get("memory_store", ""),
                app=kwargs.get("app", ""),
                crew=kwargs.get("crew", ""),
                target_member=kwargs.get("target_member"),
                _memory_mode=mode,
                _record=record,
                _inherited_selection=inherited,
            )
            kwargs["_execution_context"] = execution.to_record()
            kwargs["_memory_mode"] = execution.memory_mode
        except (OSError, ValueError) as exc:
            batch_id = str(kwargs.get("batch_id") or "")
            batch_total = max(0, int(kwargs.get("batch_total") or 0))
            if batch_id and not kwargs.get("_from_queue") and not kwargs.get("_store_accepted"):
                submitted = self._batch_submitted.setdefault(batch_id, [0, batch_total])
                submitted[0] += 1
                self._batch_progress_ts[batch_id] = time.monotonic()
            return self._announce_rejection(
                SubagentInfo(
                    id=kwargs.get("_preassigned_id") or self._mint_agent_id(),
                    task=_redact(task),
                    parent_session_key=parent,
                    agent=str(kwargs.get("agent") or ""),
                    memory_mode=mode if isinstance(mode, str) else "persistent",
                    done=True,
                    error=f"memory_unavailable: {exc}",
                    batch_id=batch_id,
                    batch_total=batch_total,
                )
            )
    if kwargs.get("_parent_spawn_policy") is None and not kwargs.get("_store_accepted"):
        # A caller that supplied the execution (``/api/spawn`` hands the
        # policy along with it; a continuation or retry does not): the
        # parent's declaration is read OFF the loop here, like the record
        # above, so the gate below consumes it without a directory scan.
        kwargs["_parent_spawn_policy"] = await asyncio.to_thread(
            parent_spawn_policy, str(kwargs.get("parent_session_key") or "")
        )
    if kwargs.get("_agent_check") is None:
        # The agent-directory scan, off the loop: the gate consumes this
        # answer instead of walking the directory on the loop itself.
        kwargs["_agent_check"] = await self._check_agent_off_loop(
            str(kwargs.get("agent") or ""),
            str(kwargs.get("cwd") or ""),
            app=str(kwargs.get("app") or ""),
            execution_context=kwargs.get("_execution_context"),
            prevalidated=bool(kwargs.get("_agent_prevalidated")),
        )
    # The memory read's re-entry re-runs the policy gates and the agent
    # check; the parent's declaration and the agent answer read off the
    # loop above are handed to it too, so neither scans the agents
    # directory on the loop.
    # Neither pass registers a nested child synchronously: that walks the
    # store's ledger on the loop. It is awaited below, off-loop, as the
    # durable path does (taskq.waits, W3).
    policy = {
        "_parent_spawn_policy": kwargs.get("_parent_spawn_policy"),
        "_agent_check": kwargs.get("_agent_check"),
        "_child_registration": False,
    }
    prepared: Any = (
        self.spawn(task, **kwargs, _stop_before_memory_read=True, _child_registration=False)
        if self._admission.taskq_store() is None
        else self.prepare_spawn(
            task, **kwargs, _stop_before_memory_read=True, _child_registration=False
        )
    )
    if not isinstance(prepared, PreparedSpawn):
        # No row to write: the task queue is off, the spawn was refused, or
        # it is non-persistent and ran the whole gate in the prepare pass,
        # stopping at its memory read.
        result: SubagentInfo | None = await self._spawn_after_memory_read(prepared, **policy)
        if result is not None and not result.done:
            # Started or queued under a parent blocked in spawn_sub_agents:
            # the parent yields its slot, with the store I/O off the loop.
            await self._admission.taskq_child_registered_async(result)
        return result
    # From the moment the row exists until this call has claimed or
    # windowed it, the pump's refill must not pick it up: the awaits below
    # are where a concurrent drain could otherwise start it twice.
    admitting: set[str] = self.__dict__.setdefault("_admitting_ids", set())
    admitting.add(prepared.agent_id)
    outcome: SubagentInfo | None = None
    try:
        outcome = await self._spawn_async_accepted(task, prepared, **kwargs)
        return outcome
    finally:
        try:
            # A pressure defer posted on the way out must be ON the row
            # before the pump may refill it, or the next pass re-runs the
            # gate on a row whose ``next_run_at`` is not set yet. Shielded:
            # a cancel of this call must not cancel that write too, or the
            # row stays QUEUED with no ``next_run_at`` for good. A cancel
            # ends this wait at once, but the write is already queued on the
            # store's writer thread (posted by the call that deferred), ahead
            # of every refill read the release below lets through.
            await asyncio.shield(self._admission.await_pending_defer(prepared.agent_id))
        finally:
            # Whatever ended the wait, a cancel included: a row left marked
            # here is skipped by the refill, Stop all and the chip forever.
            waiting = prepared.agent_id in self._admitting_waiting
            admitting.discard(prepared.agent_id)
            self._admitting_waiting.discard(prepared.agent_id)
            if waiting or outcome is None:
                # The row may still wait, and every pump pass during this
                # call left it out: the slot one of them would have given
                # it may be free already, with nothing left to ask again.
                self._drain_queue()


async def _spawn_async_accepted(
    self: SubagentManager, task: str, prepared: PreparedSpawn, **kwargs: Any
) -> SubagentInfo | None:
    store = self._admission.taskq_store()
    assert store is not None
    store_err = await store.run(self._admission.taskq_accept_record, prepared.record)
    if store_err:
        sel().log_tool_invocation(
            session_key=str(kwargs.get("parent_session_key") or ""),
            source="subagent",
            tool_name="spawn_run",
            outcome="refused_task_store",
            metadata={"error": str(store_err)[:200], "subagent_id": prepared.agent_id},
        )
        return self._announce_rejection(
            SubagentInfo(
                id=prepared.agent_id,
                task=redact_credentials(redact_exfiltration_urls(task)[0])[0],
                agent=str(kwargs.get("agent") or ""),
                parent_session_key=str(kwargs.get("parent_session_key") or ""),
                done=True,
                error=f"spawn refused: task store unavailable ({store_err})",
                error_code=self._admission.TASK_STORE_UNAVAILABLE_CODE,
                batch_id=str(kwargs.get("batch_id") or ""),
                batch_total=max(0, int(kwargs.get("batch_total") or 0)),
            )
        )
    params = dict(prepared.params)
    params.pop("_preassigned_id", None)
    # No store I/O on the loop from here on: the window decision and the
    # claim run on the writer thread; the sync re-entry only registers.
    await self._admission.ensure_coordinator_async()
    window_hint = await self._admission.taskq_should_window_async(prepared.agent_id)
    common: dict[str, Any] = dict(
        _preassigned_id=prepared.agent_id,
        _store_accepted=True,
        _window_hint=window_hint,
        _child_registration=False,  # the W3 branch runs awaited, below
        _agent_check=kwargs.get("_agent_check"),
    )
    # The read's re-entry re-runs the policy gates on this committed row
    # (a refusal there fails it); the parent's declaration ``spawn_async``
    # read off the loop is handed along so the allowlist vet does not
    # scan the agents directory on the loop.
    first: Any = await self._spawn_after_memory_read(
        self.spawn(**params, **common, _stop_before_claim=True, _stop_before_memory_read=True),
        **common,
        _stop_before_claim=True,
        _parent_spawn_policy=kwargs.get("_parent_spawn_policy"),
    )
    if not isinstance(first, ClaimPoint):
        if first is not None and first.queued and not first.done:
            # The gate queued the row this call still holds (deferred, or
            # behind the cap): the chip counts it from here on, while the
            # refill still leaves it to this call. Marked before any await,
            # so the read the verdict's depth request makes, a loop step
            # later, already sees it.
            self._admitting_waiting.add(prepared.agent_id)
            await self._admission.taskq_child_registered_async(first)
        return first
    # The slot is reserved (ClaimPoint); the claim is awaited off-loop and
    # the re-entry consumes the reservation or releases it.
    result = await self._admission.claim_and_start(
        first,
        lambda claimed: self.spawn(**params, **common, _claimed=claimed),
        stop_params={**params, **common},
    )
    if result is not None and result.queued and not result.done:
        # The claim did not take the row and the gate queued it again.
        self._admitting_waiting.add(prepared.agent_id)
    if result is not None and not result.done and result.id in self._agents:
        # Nested child of a parent blocked in spawn_sub_agents: the parent
        # yields its slot (taskq.waits, W3) with the store I/O off-loop.
        await self._admission.taskq_child_registered_async(result)
    return result


async def _safe_announce(self: SubagentManager, info: SubagentInfo) -> None:
    return await self._admission._safe_announce_impl(info)


def _announce_rejection(self: SubagentManager, info: SubagentInfo) -> SubagentInfo:
    return self._admission._announce_rejection_impl(info)


def _should_stagger_queue(self: SubagentManager, now: float) -> tuple[bool, bool]:
    return self._admission._should_stagger_queue_impl(now)


def _memory_pressure_hold(self: SubagentManager, *, floor_gb: float | None = None) -> int | None:
    return self._admission._memory_pressure_hold_impl(floor_gb=floor_gb)


def memory_pressure_hold_active(self: SubagentManager) -> bool:
    """Whether the macOS kernel memory-pressure hold applies right now.

    For a speculative caller (the eager-spawn admission) that must not take
    the memory held starts wait for.
    """
    return self._memory_pressure_hold() is not None


def _memory_pressure_holds(
    self: SubagentManager,
    agent_id: str,
    level: int,
    *,
    parent_session_key: str = "",
    batch_id: str = "",
    available_gb: float | None = None,
    relabel: bool = False,
    commit_expiry: bool = True,
) -> str:
    return self._admission._memory_pressure_holds_impl(
        agent_id,
        level,
        parent_session_key=parent_session_key,
        batch_id=batch_id,
        available_gb=available_gb,
        relabel=relabel,
        commit_expiry=commit_expiry,
    )


def _pressure_lane_started(
    self: SubagentManager, parent_session_key: str, started_at: float
) -> None:
    self._admission._pressure_lane_started_impl(parent_session_key, started_at)


def _forget_pressure_clock(self: SubagentManager, agent_id: str) -> None:
    self._admission._forget_pressure_clock_impl(agent_id)


def _forget_pending_start(self: SubagentManager, agent_id: str) -> None:
    """Drop what this process kept for a start that began or never will."""
    self._forget_pressure_clock(agent_id)
    self._floor_waits.pop(agent_id, None)
    self._held_approval_modes.pop(agent_id, None)
    self._floor_deferred_ids.discard(agent_id)
    self._admission.memory_wait_ended(agent_id)


def _drain_queue(self: SubagentManager) -> None:
    return self._admission._drain_queue_impl()


async def _admit_released_start(self: SubagentManager, info: SubagentInfo) -> str:
    return await self._admission._admit_released_start_impl(info)


def _release_admitted_start(self: SubagentManager) -> str:
    return self._admission._release_admitted_start_impl()


async def _drain_queue_async(self: SubagentManager) -> None:
    return await self._admission._drain_queue_async_impl()


async def _drain_queue_pass(self: SubagentManager) -> None:
    return await self._admission._drain_queue_pass_impl()


def _drain_queue_sync(
    self: SubagentManager,
    *,
    refill: Callable[..., int],
    dispatch: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    return self._admission._drain_queue_sync_impl(refill=refill, dispatch=dispatch)


async def _dispatch_async(self: SubagentManager, params: dict[str, Any]) -> SubagentInfo | None:
    return await self._admission._dispatch_async_impl(params)


def _after_dispatch(
    self: SubagentManager,
    params: dict[str, Any],
    drained: SubagentInfo | None,
    *,
    refill: Callable[..., int],
) -> None:
    return self._admission._after_dispatch_impl(params, drained, refill=refill)


async def _spawn_with_approval(self: SubagentManager, info: SubagentInfo) -> None:
    return await self._admission._spawn_with_approval_impl(info)


def _log_spawned(self: SubagentManager, info: SubagentInfo) -> None:
    return self._admission._log_spawned_impl(info)


async def queued_run_async(self: SubagentManager, agent_id: str) -> "QueuedRun | None":
    """The accepted, not yet registered spawn *agent_id*, or None.

    For a reader that must not answer "not found" for a spawn the gate is
    holding (``GET /api/spawn/{id}`` and its ownership check). None also
    for a registered id: :meth:`get` answers that one.
    """
    return await self._admission.taskq_queued_run_async(agent_id)


async def queued_runs_async(
    self: SubagentManager, parent_session_key: str | None = None, *, app: str | None = None
) -> "QueuedRunListing":
    """Every accepted spawn with no registered run, for one parent or all.

    The rows :attr:`all_agents` cannot list: gate-deferred rows, rows
    waiting for a slot, claimed unregistered rows and window entries
    (``GET /api/spawn?queued=1``). A bounded page: ``partial`` says when it
    cannot be all of them.
    """
    return await self._admission.taskq_queued_runs_async(parent_session_key, app=app)


def root_session_key(self: SubagentManager, key: str) -> str:
    """The first non-subagent session key reached by following parent links.

    A subagent's own key is ``subagent:<id>``. On the shared-runtime path
    that key is never registered in the session store, so anything keyed on
    it (trust, the owning dashboard tab) is absent for every run below depth
    one. The walk stops at a non-subagent key, a key with no record, an
    empty parent, or a key already visited (a cycle has no root, and the
    last key seen is returned rather than looping).

    A key names a CONVERSATION: the run that started it and every
    continuation share it, so a request under that key cannot say which of
    them sent it. The answer therefore never depends on which run is live.
    A record admitted for the conversation carries the
    ``conversation_root_session_key`` fixed by the run that founded it (see
    :meth:`conversation_root_for_new_run`), so ordinarily every retained
    record agrees. A continuation started from a different root marks the
    conversation contested, and two records that founded it independently
    (concurrent continuations admitted after eviction) can retain different
    roots; either way the conversation resolves to :func:`contested_root`
    of its key -- a marker that is no session key, so
    :meth:`root_approval_policy` refuses it and no tab shows it. Runs
    admitted below a contested conversation carry the marker in their own
    stamps, so the refusal outlives the records that established it.
    A record built outside admission (no stamp) answers with its own root
    stamp, or its parent link.
    """
    seen: set[str] = set()
    while key.startswith("subagent:") and key not in seen:
        seen.add(key)
        records = self._conversation_records(key)
        if not records:
            break
        roots = {r.conversation_root_session_key for r in records}
        roots.discard("")
        if len(roots) > 1:
            # A continuation from another chat beside the founder, or two
            # records that founded the conversation independently (concurrent
            # continuations after eviction) with different roots: no single
            # chat owns the key.
            return contested_root(key)
        if roots:
            # The one root every record agrees on -- a chat's key, or a
            # contested marker inherited from an ancestor conversation and
            # propagated unchanged.
            return roots.pop()
        record = records[0]
        if record.root_session_key:
            return record.root_session_key
        if not record.parent_session_key:
            break
        key = record.parent_session_key
    return key


def _conversation_records(self: SubagentManager, key: str) -> list[SubagentInfo]:
    """Every retained record keyed by *key*, the direct run first.

    A run's key is ``conversation_key or subagent:<id>``: the original run
    matches on its id, each continuation on its ``conversation_key``.
    """
    records: list[SubagentInfo] = []
    direct = self._agents.get(key[len("subagent:") :]) if key.startswith("subagent:") else None
    if direct is not None:
        records.append(direct)
    records.extend(
        info
        for info in self._agents.values()
        if info is not direct and info.conversation_key == key
    )
    return records


def conversation_root_for_new_run(
    self: SubagentManager,
    conversation_key: str,
    own_root: str,
    own_key: str,
    durable_root: str | None = None,
) -> str:
    """The conversation root to stamp on a run being admitted.

    A run that starts a conversation founds it with *own_root* -- or, when
    it has no parent (a cron's or the CLI's spawn, whose root is empty),
    with *own_key* (``subagent:<id>``), so that the conversation has a
    founder to compare against and a continuation from a chat cannot found
    it in the chat's name. A continuation (``conversation_key`` set)
    inherits the founding root when it matches its own; a continuation from
    a different root gets :func:`contested_root` of the key, the marker
    :meth:`root_session_key` fails closed on. With no retained record of the
    conversation (a restart, or the founder evicted) the founding root is
    *durable_root*: the value of the founder's durable run record, where
    admission wrote it (:func:`create_agent_folder`) and where a contest is
    written when one arises, captured OFF the event loop by the caller
    (:meth:`spawn_async` via :meth:`_durable_conversation_root`) -- this
    method runs on the loop and reads nothing. A conversation whose founding
    root is unknown here -- no captured value, no record, or one written
    before the stamp existed -- is contested, so a chat cannot found in its
    own name a conversation another chat authored. A *durable_root* that is
    itself a contested marker wins over every retained record, since the
    record that contested the conversation may be gone while the founder's
    is not.

    A continuation with no parent (the CLI's or a script's ``continue``,
    with no chat behind it) brings no chat's trust to the conversation, so
    when the founder had none either -- the conversation was founded in the
    founder's own name, the key itself -- the continuation inherits that
    founding root: no chat's trust is gained by the inheritance (the
    founder's own registered policy already governs the conversation's
    children), and a
    contest would close the run's own ``approval_mode`` and YOLO for a
    prompt no chat can answer. A parentless continuation of a CHAT-founded
    conversation stays contested: inheriting that chat's root would run a
    turn the chat did not author under the chat's trust.
    """
    parentless = not own_root
    own_root = own_root or own_key
    if not conversation_key:
        return own_root
    if durable_root and is_contested_root(durable_root):
        # A contest written to the founder's record outranks whatever is
        # retained in memory: the founder may still be retained with its
        # founding root while the continuation that contested it has been
        # evicted, and a same-root continuation must not read the founding
        # root back past the contest.
        return durable_root
    founding: set[str] = set()
    for record in self._conversation_records(conversation_key):
        stamp = record.conversation_root_session_key
        if is_contested_root(stamp):
            return stamp  # already contested: stays contested
        if stamp:
            founding.add(stamp)
    if not founding:
        durable = durable_root or ""
        if not durable:
            # No record of who founded the conversation: fail closed. The
            # marker does not say why (the prompt's one state sentence is
            # true for this case too), only that no chat's trust applies.
            return contested_root(conversation_key)
        if is_contested_root(durable):
            return durable
        founding.add(durable)
    # A founder that is not this root -- another chat, no chat at all (a
    # parentless run founds in its own name, the conversation key itself),
    # or two independent founders (concurrent continuations admitted after
    # the original's record was evicted) -- means nobody owns the key. The
    # one exception grants nothing: a parentless continuation of a
    # conversation founded in its own name inherits that name.
    if founding == {own_root}:
        return own_root
    if parentless and founding == {conversation_key}:
        return conversation_key
    return contested_root(conversation_key)


def _durable_conversation_root(self: SubagentManager, conversation_key: str) -> str:
    """The conversation root the founder's run record persisted, or ``""``.

    The record is the founder's own ``state.json`` (the conversation key
    names the founder's id). A record without the field predates the stamp:
    it answers its chat parent when the founder was a depth-one run (the
    root admission would have stamped), else ``""``, which the caller treats
    as unknown. A file read: call
    it off the event loop (``asyncio.to_thread``) and hand the value to
    admission as ``_durable_conversation_root``.
    """
    state = read_state(conversation_key[len("subagent:") :])
    if not state:
        return ""
    if "conversation_root" not in state:
        # A record written before the stamp existed. Its founding root is
        # still derivable when the founder was a depth-one run: admission
        # would have stamped its chat parent, so that key is the same root
        # the stamp would carry, and a same-chat continuation is spared a
        # one-time contested lockout. A nested pre-stamp founder (parent
        # ``subagent:``) has no derivable root and stays unknown.
        parent = state.get("parent_session")
        if isinstance(parent, str) and parent and not parent.startswith("subagent:"):
            return parent
        return ""
    root = state.get("conversation_root")
    return root if isinstance(root, str) else ""


def root_session_key_for(self: SubagentManager, info: SubagentInfo) -> str:
    """The tab-routing root of *info*'s own spawn tree.

    The stamp taken at admission wins; a record built outside admission
    (a synthetic terminal, a test double) falls back to resolving its parent.

    A run admitted BELOW a contested conversation is stamped with the
    conversation's ``contested:`` marker, so the refusal outlives the records
    that established it (:meth:`root_session_key`) -- but the marker is no
    session key, and no tab reads it: a frame slotted to it, or a rooted
    listing keyed on it, would leave a live run's card in no tab at all and
    the sidebar's count dark while it runs. The marker therefore answers
    only for TRUST (:meth:`trust_root_for`); for the tab, such a run belongs
    where its parent's card is -- the contested continuation's card sits in
    the tab of the chat that continued it -- so the walk climbs one record
    and asks the same question of it. A parent not retained
    leaves no tab to answer with (``""``: the global feed), which is where a
    marker resolved anyway. Admission captures that answer in
    ``tab_root_session_key`` so a later continuation of the parent
    conversation from another chat cannot move the card; the walk serves
    only a record built outside admission.
    """
    stamp = info.root_session_key
    if is_contested_root(stamp):
        return info.tab_root_session_key or self._tab_root_below_contest(info)
    return stamp or self.root_session_key(info.parent_session_key)


def _tab_root_below_contest(self: SubagentManager, info: SubagentInfo) -> str:
    """The tab a run stamped with a contested marker belongs to.

    Climbs parent records until one answers with a real key. A parent key
    names a CONVERSATION -- the founder and each continuation share it, and
    the run that spawned *info* may be a continuation with an id of its own
    -- so every retained record under the key is consulted, the newest
    active one first: a live continuation is the run driving the
    conversation, so its chat's tab is where the card belongs, and the
    founder's tab (which ``_conversation_records`` lists first) would
    otherwise answer until its record is evicted and then hand the card to
    a different tab mid-run. Among equals the most recently started wins,
    so a roster of only-finished records still answers consistently. Only
    that top record is asked at each hop: when its own root is the marker
    (a continuation issued from under a contest), the climb follows ITS
    parent rather than an older record's root, which is the founder's chat
    -- the one chat the card must not land in.
    Bounded by the retained ancestry: an evicted ancestor, an empty parent
    or a cycle ends the climb with ``""``.
    """
    seen: set[str] = set()
    parent_key = info.parent_session_key
    while parent_key.startswith("subagent:") and parent_key not in seen:
        seen.add(parent_key)
        records = self._conversation_records(parent_key)
        if not records:
            return ""
        top = max(records, key=lambda r: (not r.done, r.started))
        root = top.root_session_key
        if root and not is_contested_root(root):
            return root
        parent_key = top.parent_session_key
    # A chat, cron or channel key is the tab itself (or whatever the slot
    # mapping makes of a non-chat key); an empty parent or a cycle is none.
    return "" if parent_key.startswith("subagent:") else parent_key


def trust_root_for(self: SubagentManager, info: SubagentInfo) -> str:
    """The root whose trust governs requests *info*'s own run issues.

    Ordinarily the run's spawn-tree root (:meth:`root_session_key_for`).
    A continuation admitted into a CONTESTED conversation is the exception:
    its spawn-tree root is the chat that continued it, but the turn it runs
    is driven by a conversation another chat founded, so that chat's trust
    must not approve the requests it issues -- the continuing chat can
    already auto-approve any spawn of its own, and lending that trust to a
    foreign conversation's instructions is the escalation the marker exists
    to deny. Such a run resolves to the conversation's contested marker,
    which :meth:`root_approval_policy` refuses and no tab shows; its card
    stays in the continuing chat's tab (:meth:`root_session_key_for`), and
    its prompts reach the global feed labeled with the cause.
    """
    stamp = info.conversation_root_session_key
    if is_contested_root(stamp):
        return stamp
    # A run admitted BELOW a contested conversation carries the marker in
    # its spawn-tree stamp. The tab accessor turns that into the ancestor's
    # tab (the marker is no tab); trust must not follow it there -- the
    # marker is the refusal, and it is read here as written.
    root_stamp = info.root_session_key
    if is_contested_root(root_stamp):
        return root_stamp
    return self.root_session_key_for(info)


def root_approval_policy(self: SubagentManager, root_session_key: str) -> str:
    """The trust a resolved root grants: the store's policy, or ``""``.

    A contested conversation's marker is refused without consulting the
    store. The marker is not a session key, and that is the point: a
    continuable run started from a trusted chat runs on its own session,
    registered under the run's ``subagent:`` key with the chat's ``"auto"``,
    so a root that named the run's key would read that trust back for a
    conversation the marker exists to deny -- and, stamped into the records
    of every run admitted beneath it, the marker keeps denying it after the
    records that established the contest are gone. Any other root -- a chat,
    cron or channel key, or the own key of a run that has no parent (a
    cron's or the CLI's spawn) -- reads the store, which is where such a
    run's effective policy is registered for its children.
    """
    if is_contested_root(root_session_key):
        return ""
    return self._sessions.get_approval_policy(root_session_key)
