"""The manager's continuable conversations: retry and continuation successor claims,
``continue_conversation``, steering and follow-ups, conversation release and the
registry rebuild, and the delegations into
:class:`~kiro_crew.subagent_manager.ContinuationCoordinator`.

Each function takes the manager as ``self`` and is bound as the ``SubagentManager``
attribute of the same name in :mod:`kiro_crew.subagent_parts.manager`.

Manager fields read: ``_agents``, ``_continuation``, ``_continuations_starting``.

Manager fields written: none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.subagent import (
        SUCCESSOR_PENDING,
        SUCCESSOR_UNKNOWN,
        Any,
        SubagentInfo,
        SubagentManager,
        _redact,
    )


# ── Continuable conversations (keep=True) ─────────────────────────────


def _conversation_busy(self: SubagentManager, conv_key: str) -> SubagentInfo | None:
    return self._continuation._conversation_busy_impl(conv_key)


def _keep_recorded_on_disk(self: SubagentManager, key: str) -> bool:
    return self._continuation._keep_recorded_on_disk_impl(key)


def _promote_conversation(
    self: SubagentManager, conv_id: str, conv_key: str, last_used: float | None = None
) -> None:
    return self._continuation._promote_conversation_impl(conv_id, conv_key, last_used)


def _scan_keep_states(self: SubagentManager) -> list[tuple[str, str, str, str, str, float]]:
    return self._continuation._scan_keep_states_impl()


async def _rebuild_conversation_registry(self: SubagentManager) -> None:
    return await self._continuation._rebuild_conversation_registry_impl()


def native_child_resume_refusal(self: SubagentManager, conversation_id: str) -> str | None:
    """Typed refusal when *conversation_id* is a harness-native child of a
    live session (no conversation of its own; the parent is the lever)."""
    return self._continuation.native_child_resume_refusal(conversation_id)


def claim_retry(self: SubagentManager, failed: SubagentInfo) -> str:
    """Claim the right to retry *failed*; ``""`` when granted, else the owner.

    A failed run has at most one successor. It is taken when a retry or a
    ``spawn_continue`` of this run already claimed it, or when a run holds
    its conversation ``subagent:<id>`` -- queued, live or finished. A grant
    marks the claim ``SUCCESSOR_PENDING`` with no await in between, so a
    concurrent retry or continuation sees it; :meth:`settle_retry` records
    the outcome.
    """
    for owner in (failed._retried_as, failed._continued_as):
        if owner:
            return owner
    if failed._continuation_starting:
        return SUCCESSOR_PENDING
    conv_key = f"subagent:{failed.id}"
    busy = self._conversation_busy(conv_key)
    if busy is not None and busy.id != failed.id:
        return busy.id
    for info in self._agents.values():
        if info.id != failed.id and info.conversation_key == conv_key:
            return info.id
    failed._retried_as = SUCCESSOR_PENDING
    return ""


def settle_retry(self: SubagentManager, failed: SubagentInfo, successor_id: str | None) -> None:
    """Record the run a granted retry started, or release a claim that did not
    land (``successor_id`` None). A claim already settled is left alone."""
    if failed._retried_as == SUCCESSOR_PENDING:
        failed._retried_as = successor_id or ""


def _claim_continuation(
    self: SubagentManager, conv_id: str, task: str, parent_session_key: str
) -> tuple[SubagentInfo | None, SubagentInfo | None]:
    """``(original, refusal)`` for a continuation of *conv_id*.

    A refusal when a retry already claimed the run (its task runs as that
    retry now), or while another continuation's start is still in flight
    (it is not yet visible to the conversation-busy lookup). Otherwise the
    continuation claims *conv_id* in ``_continuations_starting`` before any
    await, so a retry or a second continuation arriving while this start is
    in flight is refused. The claim is held by the manager, so it outlasts a
    registry record that ``evict_completed_agents`` drops during the start,
    and it covers a run with no record at all (evicted, or from before a
    restart). A registered run's ``_continuation_starting`` flag is set with
    it for ``claim_retry``. ``_settle_continuation`` ends both.
    """
    original = self._agents.get(conv_id)
    if original is not None and original._retried_as:
        reason = (
            f"run {conv_id} was retried as run {original._retried_as}, which "
            "owns its task now; continue that run's conversation instead"
        )
    elif conv_id in self._continuations_starting:
        reason = (
            f"another continuation of run {conv_id} is starting — wait for its "
            "completion event, or use spawn_steer once it is running"
        )
    else:
        self._continuations_starting.add(conv_id)
        if original is not None:
            original._continuation_starting = True
        return original, None
    refusal = SubagentInfo(
        id=self._mint_agent_id(),
        task=_redact(task),
        done=True,
        parent_session_key=parent_session_key,
        error=f"conversation_busy: {reason}",
    )
    return original, refusal


def _settle_continuation(
    self: SubagentManager,
    conv_id: str,
    original: SubagentInfo | None,
    result: SubagentInfo | None,
    *,
    raised: bool = False,
) -> None:
    """End the in-flight claim a start took on *conv_id*. A start that landed
    records its id only when no earlier continuation did; one that did not land
    leaves an earlier successor in place. A start that RAISED may have accepted
    its durable row first, so it counts as a continuation of unknown id.
    Called once per granted claim, by its holder only."""
    self._continuations_starting.discard(conv_id)
    if original is None:
        return
    original._continuation_starting = False
    if original._continued_as:
        return
    if raised:
        original._continued_as = SUCCESSOR_UNKNOWN
    elif result is not None and not (result.done and result.error):
        original._continued_as = result.id


def continue_conversation(
    self: SubagentManager,
    conv_id: str,
    task: str,
    parent_session_key: str = "",
    agent: str = "",
    model: str | None = None,
    max_turns: int = 0,
    cwd: str = "",
    _preassigned_id: str = "",
    _memory_mode: str | None = None,
    _crew_log_asked: "tuple[str, int, int] | None" = None,
) -> SubagentInfo | None:
    original, refusal = self._claim_continuation(conv_id, task, parent_session_key)
    if refusal is not None:
        return refusal
    result: SubagentInfo | None = None
    raised = True
    try:
        result = self._continuation.continue_conversation_impl(
            conv_id,
            task,
            parent_session_key,
            agent,
            model,
            max_turns,
            cwd,
            _preassigned_id,
            _memory_mode=_memory_mode,
            _crew_log_asked=_crew_log_asked,
        )
        raised = False
    finally:
        # This call took the claim: every exit (a return, a raise, a cancel) ends it.
        self._settle_continuation(conv_id, original, result, raised=raised)
    return result


async def continue_conversation_async(
    self: SubagentManager,
    conv_id: str,
    task: str,
    parent_session_key: str = "",
    agent: str = "",
    model: str | None = None,
    max_turns: int = 0,
    cwd: str = "",
    _preassigned_id: str = "",
    _memory_mode: str | None = None,
    _crew_log_asked: "tuple[str, int, int] | None" = None,
) -> SubagentInfo | None:
    original, refusal = self._claim_continuation(conv_id, task, parent_session_key)
    if refusal is not None:
        return refusal
    result: SubagentInfo | None = None
    raised = True
    try:
        result = await self._continuation.continue_conversation_async_impl(
            conv_id,
            task,
            parent_session_key,
            agent,
            model,
            max_turns,
            cwd,
            _preassigned_id,
            _memory_mode,
            _crew_log_asked,
        )
        raised = False
    finally:
        # This call took the claim: every exit (a return, a raise, a cancel) ends it.
        self._settle_continuation(conv_id, original, result, raised=raised)
    return result


def _continue_prelude(
    self: SubagentManager,
    conv_id: str,
    task: str,
    parent_session_key: str = "",
    agent: str = "",
    model: str | None = None,
    max_turns: int = 0,
    cwd: str = "",
    _preassigned_id: str = "",
    _memory_mode: str | None = None,
    _crew_log_asked: "tuple[str, int, int] | None" = None,
    *,
    _execution_context=None,
    _captured_state=...,
) -> "SubagentInfo | dict[str, Any] | None":
    return self._continuation._continue_prelude_impl(
        conv_id,
        task,
        parent_session_key,
        agent,
        model,
        max_turns,
        cwd,
        _preassigned_id,
        _memory_mode,
        _crew_log_asked,
        _execution_context=_execution_context,
        _captured_state=_captured_state,
    )


def recorded_cwd(self: SubagentManager, conv_id: str) -> str:
    return self._continuation.recorded_cwd_impl(conv_id)


def _inherited_context_groups(self: SubagentManager, conv_id: str) -> tuple[bool, bool, bool]:
    return self._continuation._inherited_context_groups_impl(conv_id)


def _inherited_memory_store(self: SubagentManager, conv_id: str) -> str:
    return self._continuation._inherited_memory_store_impl(conv_id)


async def steer_run(self: SubagentManager, agent_id: str, message: str) -> tuple[bool, str]:
    return await self._continuation.steer_run_impl(agent_id, message)


async def follow_up_run(self: SubagentManager, agent_id: str, message: str) -> tuple[bool, str]:
    return await self._continuation.follow_up_run_impl(agent_id, message)


def _arm_followup_watcher(self: SubagentManager, info: SubagentInfo) -> None:
    return self._continuation._arm_followup_watcher_impl(info)


async def _deliver_followups(self: SubagentManager, info: SubagentInfo) -> None:
    return await self._continuation._deliver_followups_impl(info)


async def _announce_followup_failure(
    self: SubagentManager,
    info: SubagentInfo,
    reason: str,
    failure_info: SubagentInfo | None = None,
    messages: list | None = None,
) -> None:
    return await self._continuation._announce_followup_failure_impl(
        info, reason, failure_info, messages
    )


def _audit_followup(self: SubagentManager, info: SubagentInfo, outcome: str) -> None:
    return self._continuation._audit_followup_impl(info, outcome)


def release_conversation(self: SubagentManager, conv_id: str) -> tuple[bool, str]:
    return self._continuation.release_conversation_impl(conv_id)


async def release_conversation_async(self: SubagentManager, conv_id: str) -> tuple[bool, str]:
    return await self._continuation.release_conversation_async_impl(conv_id)


async def _sweep_conversations_async(self: SubagentManager, now: float) -> None:
    return await self._continuation._sweep_conversations_async_impl(now)
