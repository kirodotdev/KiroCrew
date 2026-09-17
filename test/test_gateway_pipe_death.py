"""Tests for AcpProcessDied handling in gateway subagent injection.

``_inject_with_retry`` is nested inside ``_subagent_done``, which the gateway
hands to ``SubagentManager(on_done=...)``, so the only handle on it is the
callback the manager was constructed with. An earlier version of this file
called that indirection impractical and asserted against a hand-written replica
of the loop instead -- and the replica drifted: it never grew the
``PromptBusyExhaustedError`` and ``AcpError`` arms production has, and still took
a ``stream_fn`` long after production had moved to
``(client, msg, parent_key, label)``. Deleting the entire ``except
AcpProcessDied`` arm from ``gateway.py`` left every assertion here green. These
tests drive the real closure instead.

What they pin, which nothing else does: on a dead ACP process the parent session
is RESET (not merely cancelled), and the failure is reported with the specific
``reason="ACP process died"``. Both halves matter because the arm's absence is
nearly invisible -- ``AcpProcessDied`` subclasses ``AcpError``, so without the
arm the error falls through to the retry arm, re-raises on the last attempt and
lands in the generic "all injection attempts failed" fallback, which still
notifies (with a different reason) and still cancels, but never resets: the
parent session would stay pointed at a dead provider.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpProcessDied

PARENT_KEY = "C123:ts.death"


def _gateway_with_on_done():
    """Return ``(orchestrator, on_done)`` with the real nested retry loop wired.

    The harness helpers come from ``test_slack_gateway`` rather than a local
    copy: a private copy of the setup is what let this file's subject drift from
    the gateway it claims to cover in the first place.
    """
    from test_slack_gateway import (
        _make_orchestrator,
        _mock_context_builder,
        _mock_dashboard_state,
        _mock_sessions,
    )

    orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
    orch.sessions = _mock_sessions()
    orch.ctx_builder = _mock_context_builder()
    orch.ctx_builder.hooks = MagicMock()
    orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    orch.dashboard_state = _mock_dashboard_state()
    orch.slack = MagicMock()
    orch.slack.open_dm = AsyncMock(return_value="D_U1")
    orch.slack.post_message = AsyncMock()
    orch.slack.post_blocks = AsyncMock(return_value="ts")
    with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
        with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
            mgr = MagicMock()
            mgr.start_reaper = MagicMock()
            mgr.running = []
            mgr.queued_count_for = MagicMock(return_value=0)
            mgr.has_pending_work_for = MagicMock(return_value=False)
            mgr.running_agents_for = MagicMock(return_value=[])
            mgr.get = MagicMock(return_value=None)
            mgr.notify_injection_failed = MagicMock()
            mock_sm.return_value = mgr
            orch._init_subagents()
            on_done = mock_sm.call_args[1]["on_done"]
    return orch, on_done


def _finished_subagent() -> MagicMock:
    """A subagent that ran to completion, so its result reaches the injection.

    ``user_stopped`` and ``error`` are set explicitly: left as bare MagicMock
    attributes they are truthy, and the record would read as stopped-by-user --
    a neutral outcome, not the completed one whose result gets injected.
    """
    info = MagicMock()
    info.id = "agent-died"
    info.parent_session_key = PARENT_KEY
    info.error = None
    info.user_stopped = False
    info.result = "result"
    info.result_path = ""
    info.task = "task"
    info.agent = ""
    info.silent = False
    info.elapsed = 1.0
    info.started = 0.0
    return info


class TestGatewayAcpProcessDiedInjection:
    """The real ``_inject_with_retry``'s ``except AcpProcessDied`` arm."""

    @pytest.mark.asyncio
    async def test_process_died_resets_session_and_notifies(self) -> None:
        """A dead provider resets the parent session and names that as the reason.

        ``assert_called_once_with`` is load-bearing on both counts. The arm
        returns None instead of raising, so the caller marks the attempt injected
        and the generic fallback must NOT also fire; and the fallback's reason is
        the accumulated attempt-failure string, which can never equal "ACP
        process died". A second call, or any other reason, means the arm was
        bypassed rather than exercised.
        """
        orch, on_done = _gateway_with_on_done()
        info = _finished_subagent()

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=AcpProcessDied("pipe broken"),
        ):
            await on_done(info)

        orch.sessions.reset.assert_awaited_once_with(PARENT_KEY)
        orch.subagent_mgr.notify_injection_failed.assert_called_once_with(
            info, reason="ACP process died"
        )

    @pytest.mark.asyncio
    async def test_process_died_with_reset_failure_still_notifies(self) -> None:
        """A reset that itself fails must not swallow the failure report.

        Without the ``except Exception`` around the reset, the RuntimeError would
        escape ``_inject_with_retry`` into the caller's broad handler, which
        breaks the attempt loop and reports the generic reason instead -- so the
        operator would learn the injection failed but never that the provider
        died. The specific ``reason`` here is what distinguishes the two.
        """
        orch, on_done = _gateway_with_on_done()
        orch.sessions.reset = AsyncMock(side_effect=RuntimeError("reset failed"))
        info = _finished_subagent()

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=AcpProcessDied("pipe broken"),
        ):
            await on_done(info)

        orch.sessions.reset.assert_awaited_once_with(PARENT_KEY)
        orch.subagent_mgr.notify_injection_failed.assert_called_once_with(
            info, reason="ACP process died"
        )
