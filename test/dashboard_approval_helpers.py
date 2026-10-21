"""Shared pieces for ``test_dashboard_approval.py`` and its ``_modes`` and ``_denials`` parts.

``_make_state`` builds a ``DashboardState`` and a mocked client, ``_make_slot`` a bare chat
slot, and ``_set_stream`` with ``_permission_event`` / ``_complete_event`` scripts what the
provider streams. ``_answer_approval`` stands in for the user answering a card: it waits,
under a deadline, for the turn to register its approval future, and it is the one wait
``TestApprovalAnswerersDoNotRaceTheStream`` allows. ``_drain`` cancels and awaits a helper
task, ``_context_builder`` and ``_make_hook_store`` are the hook doubles, and
``_patch_stats`` stubs the SEL logger.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.hooks import ToolHookResult
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.testing.wait import async_wait_until


async def _async_iter(items: list):  # type: ignore[type-arg]
    for item in items:
        yield item


@contextmanager
def _patch_stats():
    with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
        mock_sel.return_value = MagicMock()
        yield


def _permission_event(
    title: str = "fs_write",
    tool_kind: str = "edit",
) -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=title,
        tool_kind=tool_kind,
        request_id="req-1",
    )


def _complete_event() -> LLMEvent:
    return LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")


def _make_hook_store() -> MagicMock:
    hs = MagicMock()
    hs.fire = AsyncMock(return_value=[])
    return hs


def _make_state(
    tmp_path,
    context_builder=None,
    hook_store=None,
) -> tuple[DashboardState, AsyncMock]:
    """Return (state, client) with all async methods properly mocked."""
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    client = AsyncMock()
    sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    sessions.record_failure = AsyncMock()
    sessions.check_context_usage = MagicMock()
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(
            list_jobs=MagicMock(return_value=[]),
            status=MagicMock(return_value={}),
        ),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    state.context_builder = context_builder
    state._hook_store = hook_store or _make_hook_store()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    return state, client


def _make_slot(key: str = "chat-1-test", trust: bool = False) -> _ChatSlot:
    slot = _ChatSlot(key)
    slot._trust = trust
    return slot


def _set_stream(client: AsyncMock, events: list[LLMEvent]) -> None:
    """Make client.stream() return an async iterable of events."""
    client.stream = MagicMock(side_effect=lambda *a, **kw: _async_iter(events))


# How long an answerer waits for the turn to register its approval future. The
# product parks on that future for the MINIMUM of `approval_timeout_for()` and
# `tool_approval_timeout_secs()` — 600s by default — which is LONGER than CI's
# `--timeout=180`, so a poke that misses does not fail the test: pytest-timeout
# hard-kills the xdist worker and the run reports `worker 'gwN' crashed`,
# naming an arbitrary in-flight test and discarding the real cause. Waiting for
# the future to EXIST instead of sleeping a fixed interval removes that race;
# the deadline exists only so a future that never appears surfaces as the named
# assertion below rather than as a killed worker.
_ANSWER_WAIT_SECS = 30.0


async def _answer_approval(
    owner: object,
    request_id: str,
    outcome: str,
    *,
    timeout: float = _ANSWER_WAIT_SECS,
    before_answer: Callable[[], None] | None = None,
) -> None:
    """Resolve *owner*'s approval future for *request_id* once the turn registers it.

    Stands in for a user clicking Approve/Reject. The future is created inside
    ``_on_permission_prompt`` (chat_turn/permission_prompt.py) when the permission event is
    processed, so an answerer that pokes at a fixed offset races the stream: on a loaded
    runner the event can arrive after the poke, leaving the turn parked on an approval nobody will
    ever answer.
    """
    futures = owner._approval_futures  # type: ignore[attr-defined]
    # The predicate reads membership, not the future itself: a future returned to
    # the wait would be awaited, and this answerer needs it still pending.
    await async_wait_until(
        lambda: request_id in futures,
        timeout=timeout,
        describe=lambda: (
            f"approval future {request_id!r} was never registered — the turn never "
            "reached its permission event, so there was nothing to answer"
        ),
    )
    fut = futures[request_id]
    if not fut.done():
        try:
            if before_answer is not None:
                before_answer()
        finally:
            # A failed observation must not leave the runner parked.
            fut.set_result(outcome)


def _context_builder(hook_result: ToolHookResult | None = None) -> MagicMock:
    # ``allow()`` counts itself through kiro_crew.metrics. As a DEFAULT ARGUMENT it
    # ran at import, before any pin existed, and built the process-global recorder
    # from the operator's real config: an exporter bound to the real
    # ~/.kiro/crew/metrics for the life of the worker. Built per call instead.
    cb = MagicMock()
    cb.hooks.on_tool_call.return_value = (
        hook_result if hook_result is not None else ToolHookResult.allow()
    )
    cb.build_message.return_value = ("hello", None)
    return cb


async def _drain(task: asyncio.Task) -> None:
    """Cancel *task* and await it, so it cannot outlive the test.

    A helper task left running is garbage-collected on a later test's loop, which
    reports "coroutine ignored GeneratorExit" against an innocent test.
    """
    if not task.done():
        task.cancel()
    try:
        # flake-ok: pre-existing await of a task this helper has just cancelled -- moved verbatim; tracked for a de-flake pass
        await task
    except asyncio.CancelledError:
        pass
