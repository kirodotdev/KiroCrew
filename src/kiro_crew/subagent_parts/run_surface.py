"""The manager's run-loop glue: ``_run`` and ``_run_inner``, the stop-recovery and
dependency yields, shared-session recognition and the sharing plan, the provider seam,
run lookups, the queue-depth and pending-work queries, the off-loop state and result
writers, and the delegations into
:class:`~kiro_crew.subagent_manager.RunEventCoordinator`.

Each function takes the manager as ``self`` and is bound as the ``SubagentManager``
attribute of the same name in :mod:`kiro_crew.subagent_parts.manager`.

Manager fields read: ``_agents``, ``_published_depths``, ``_run_events``.

Manager fields written: none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.subagent import (
        PROVIDER_CLAUDE_CODE,
        PROVIDER_LABEL_CLAUDE,
        AcpRuntime,
        AcpSessionProvider,
        Any,
        Callable,
        ClaudeCodeProvider,
        LLMProvider,
        ResolvedAwsProfile,
        SubagentInfo,
        SubagentManager,
        _RunCreditAccounting,
        _SharingPlan,
        asyncio,
        capabilities_of,
    )


def has_live_shared_session(self: SubagentManager, session_key: str) -> bool:
    """Recognize a shared child only while its exact runtime handle is live.

    Run records survive completion/restart for display and continuation;
    they are not session authority. The runtime's queue registry is what
    destroy() unregisters, even when the parent process keeps running.
    """
    for info in self._agents.values():
        if info.done or info.reaped or not info._session_sharing:
            continue
        if (info.conversation_key or f"subagent:{info.id}") != session_key:
            continue
        provider = info._shared_provider
        if not isinstance(provider, AcpSessionProvider):
            continue
        runtime, handle = provider._runtime, provider._handle
        if runtime.is_alive() and runtime._session_queues.get(handle.session_id) is handle._queue:
            return True
    return False


def get(self: SubagentManager, agent_id: str) -> SubagentInfo | None:
    return self._run_events.get_impl(agent_id)


def is_queued(self: SubagentManager, agent_id: str) -> bool:
    return self._run_events.is_queued_impl(agent_id)


async def _teardown_run_session(
    self: SubagentManager, info: SubagentInfo, session_key: str
) -> None:
    return await self._run_events._teardown_run_session_impl(info, session_key)


async def _run(self: SubagentManager, info: SubagentInfo) -> None:
    return await self._run_events._run_impl(info)


async def _touch_activity(self: SubagentManager, info: SubagentInfo) -> None:
    return await self._run_events._touch_activity_impl(info)


async def _fire_event(
    self: SubagentManager, etype: str, info: SubagentInfo, extra: dict | None = None
) -> None:
    return await self._run_events._fire_event_impl(etype, info, extra)


def _queued_depth(self: SubagentManager, parent_session_key: str) -> int:
    return self._run_events._queued_depth_impl(parent_session_key)


async def _queued_depth_async(self: SubagentManager, parent_session_key: str) -> int:
    return await self._run_events._queued_depth_async_impl(parent_session_key)


def queued_count_for(self: SubagentManager, parent_session_key: str) -> int:
    return self._run_events.queued_count_for_impl(parent_session_key)


async def queued_count_for_async(self: SubagentManager, parent_session_key: str) -> int:
    return await self._run_events.queued_count_for_async_impl(parent_session_key)


def published_queued_depths(self: SubagentManager) -> dict[str, int]:
    """The queued depth last published per parent session key (non-zero only).

    The values of the newest ``subagent_queued`` frames, not a fresh count: a
    reader on the event loop (the slot list) gets them without a store read,
    and they agree with what the frame stream told every client. A copy.
    """
    return self._published_depths.snapshot()


def has_in_memory_pending_work_for(
    self: SubagentManager, parent_session_key: str, *, exclude_id: str = ""
) -> bool:
    return self._run_events.has_in_memory_pending_work_for_impl(
        parent_session_key, exclude_id=exclude_id
    )


def has_unfinished_report_for(self: SubagentManager, parent_session_key: str) -> bool:
    return self._run_events.has_unfinished_report_for_impl(parent_session_key)


async def queued_count_or_none_async(self: SubagentManager, parent_session_key: str) -> int | None:
    return await self._run_events.queued_count_or_none_async_impl(parent_session_key)


def has_pending_work_for(self: SubagentManager, parent_session_key: str) -> bool:
    return self._run_events.has_pending_work_for_impl(parent_session_key)


async def has_pending_work_for_async(self: SubagentManager, parent_session_key: str) -> bool:
    return await self._run_events.has_pending_work_for_async_impl(parent_session_key)


def _emit_queue_depth(
    self: SubagentManager,
    parent_session_key: str,
    batch_id: str = "",
    *,
    wait: dict[str, Any] | None = None,
) -> None:
    return self._run_events._emit_queue_depth_impl(parent_session_key, batch_id, wait=wait)


async def _write_state_off_loop(
    self: SubagentManager, info: SubagentInfo, what: str, **fields: object
) -> bool:
    return await self._run_events._write_state_off_loop_impl(info, what, **fields)


async def _write_finished_result_off_loop(
    self: SubagentManager, info: SubagentInfo, text: str | None, *, hold_conversation: bool = False
) -> bool:
    return await self._run_events._write_finished_result_off_loop_impl(
        info, text, hold_conversation=hold_conversation
    )


async def _start_result_file(self: SubagentManager, info: SubagentInfo, text: str) -> bool:
    return await self._run_events._start_result_file_impl(info, text)


async def _drain_state_writer(
    self: SubagentManager,
    info: SubagentInfo,
    what: str,
    writer: "asyncio.Future[Any]",
    *,
    bound: float | None = None,
) -> bool:
    return await self._run_events._drain_state_writer_impl(info, what, writer, bound=bound)


def _hold_for_detached_writer(
    self: SubagentManager, info: SubagentInfo, what: str, writer: "asyncio.Future[Any]"
) -> None:
    self._run_events._hold_for_detached_writer_impl(info, what, writer)


async def _cap_unclaimed_result(self: SubagentManager, info: SubagentInfo) -> None:
    await self._run_events._cap_unclaimed_result_impl(info)


def _record_reap_ending(self: SubagentManager, info: SubagentInfo, unfinished: str) -> None:
    self._run_events._record_reap_ending_impl(info, unfinished)


async def _run_inner(self: SubagentManager, info: SubagentInfo, session_key: str) -> None:
    usage = _RunCreditAccounting(info)
    info._credit_accounting = usage
    try:
        return await self._run_events._run_inner_impl(info, session_key, usage)
    finally:
        # Cancellation may land in the event consumer, outside the stream
        # generator's exception handlers. Settle before terminal reporting.
        try:
            usage.settle()
        finally:
            if info._credit_accounting is usage:
                info._credit_accounting = None


# Facades for the completion / stop-reason handling and the lane-slot
# waits that live in subagent_manager/run_yields.py.
def _stop_recovery_wanted(self: SubagentManager, info: SubagentInfo, stop: Any) -> bool:
    return self._run_events._stop_recovery_wanted_impl(info, stop)


async def _yield_for_stop_recovery(
    self: SubagentManager, info: SubagentInfo, event: Any
) -> str | None:
    return await self._run_events._yield_for_stop_recovery_impl(info, event)


async def _await_lane_resume(
    self: SubagentManager, info: SubagentInfo, *, reason: str, timeout: float, request: bool = True
) -> bool:
    return await self._run_events._await_lane_resume_impl(
        info, reason=reason, timeout=timeout, request=request
    )


async def _yield_for_dependency(
    self: SubagentManager, info: SubagentInfo, signal: Any
) -> bool | None:
    return await self._run_events._yield_for_dependency_impl(info, signal)


async def _yield_for_infra_retry(
    self: SubagentManager, info: SubagentInfo, infra: Any
) -> str | None:
    return await self._run_events._yield_for_infra_retry_impl(info, infra)


def _stop_error_text(self: SubagentManager, info: SubagentInfo, stop: Any, event: Any) -> str:
    return self._run_events._stop_error_text_impl(info, stop, event)


def _taskq_note_stop_recovery(
    self: SubagentManager, info: SubagentInfo, data: dict[str, Any]
) -> None:
    self._run_events._taskq_note_stop_recovery_impl(info, data)


# Facades for how a run gets its session (the sharing decision, the start's price and
# the start-queue gate marks); implementations live in subagent_manager/run_start.py.
def _should_use_session_sharing(self: SubagentManager, info: SubagentInfo) -> bool:
    return self._run_events._should_use_session_sharing_impl(info)


def _sharing_plan(
    self: SubagentManager,
    info: SubagentInfo,
    *,
    cfg: Any = None,
    child_aws_profile: ResolvedAwsProfile | None = None,
) -> _SharingPlan:
    return self._run_events._sharing_plan_impl(info, cfg=cfg, child_aws_profile=child_aws_profile)


async def _ensure_dedicated_start_priced(self: SubagentManager, info: SubagentInfo) -> None:
    return await self._run_events._ensure_dedicated_start_priced_impl(info)


async def _create_shared_session(
    self: SubagentManager, info: SubagentInfo, session_key: str, agent: str
) -> "LLMProvider":
    return await self._run_events._create_shared_session_impl(info, session_key, agent)


def _gate_exit_reset(self: SubagentManager, info: SubagentInfo) -> Callable[..., None]:
    return self._run_events._gate_exit_reset_impl(info)


def _gate_wait_mark(self: SubagentManager, info: SubagentInfo) -> Callable[..., None]:
    return self._run_events._gate_wait_mark_impl(info)


# Facades for the session-start gate's late-adoption path;
# implementations live in subagent_manager/run_start.py.
async def _await_late_start(
    self: SubagentManager, info: SubagentInfo, session_key: str, exc: Exception
) -> "LLMProvider":
    return await self._run_events._await_late_start_impl(info, session_key, exc)


async def _bind_shared_handle(
    self: SubagentManager, info: SubagentInfo, session_key: str, runtime: "AcpRuntime", handle: Any
) -> "LLMProvider":
    return await self._run_events._bind_shared_handle_impl(info, session_key, runtime, handle)


def _get_parent_runtime(self: SubagentManager, parent_session_key: str) -> "AcpRuntime | None":
    return self._run_events._get_parent_runtime_impl(parent_session_key)


def _is_cc_provider(provider: object) -> bool:
    """Check if a provider routes to Claude Code.

    Matches both the (dead) standalone ``ClaudeCodeProvider`` and the
    real default backend ``AcpProvider(acp_backend="claude")``.  The
    latter is what ``_sessions.get_or_create`` actually returns for the
    ``claude_code`` provider, so detecting it here is what makes the
    session-file cleanup target ``~/.claude`` instead of ``~/.kiro``.

    Asks ``SessionCapabilities.provider_seam`` through
    :func:`~kiro_crew.agent_sdk.capabilities.capabilities_of`, which replaced a
    lazy ``from kiro_crew.providers.acp import is_claude_backend``. The import
    was lazy because ``providers.acp`` sits in a providers -> session cycle;
    the SDK is in no cycle, so this one can live at module scope. The calling
    convention is unchanged: a shape that is not a provider answers False,
    which is what the old predicate's ``isinstance`` gate bought.
    """
    if ClaudeCodeProvider is not None and isinstance(provider, ClaudeCodeProvider):
        return True
    return capabilities_of(provider).provider_seam == PROVIDER_CLAUDE_CODE


def _provider_label_of(provider: object) -> str:
    """Backend identity key for *provider*, persisted with the run's state.

    Mirrors ``_is_cc_provider`` in also matching the (dead) standalone
    ``ClaudeCodeProvider``, which the shared ``provider_label`` helper does
    not know about.
    """
    if ClaudeCodeProvider is not None and isinstance(provider, ClaudeCodeProvider):
        return PROVIDER_LABEL_CLAUDE
    # circular import: see _is_cc_provider.
    from kiro_crew.agent_sdk.drivers.acp import provider_label

    return provider_label(provider)
