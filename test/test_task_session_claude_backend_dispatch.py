"""Regression test for issue #13872.

With ``agent.acp_backend`` set to a backend OUTSIDE
``ACP_BACKENDS_ACP_RUNTIME`` (e.g. ``claude``), a task-run step must NOT open
its per-step session on the task run's shared multiplexed runtime. That shared
runtime is always a kiro-family process, so bootstrapping one for a non-runtime
backend spawns ``kiro-cli`` under a foreign backend label -- and fails outright
(``kiro-cli not found``) on an install that has no kiro-cli, making the whole
task runner unusable on a claude-configured install.

``open_task_session`` must dispatch on the SAME membership rule
``get_bg_session`` uses and route a non-runtime backend down the dedicated
per-session path (``get_or_create``) instead, so no ``kiro-cli`` argv is ever
built for it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


async def _empty_provider_stream(_command: str):
    if False:  # pragma: no cover - establishes the async-generator protocol
        yield None


def _mock_provider_factory():
    """A factory whose providers never touch a real process or argv."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.memory_mode = kwargs.get("memory_mode", "persistent")
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.is_alive = lambda: True
        m.disown_work_dir = MagicMock()
        m.context_usage_pct = lambda: 0.0
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_abort_target = lambda: None
        m.stream_command = MagicMock(side_effect=_empty_provider_stream)
        return m

    return factory


class TestTaskSessionClaudeBackendDispatch:
    @pytest.mark.asyncio
    async def test_claude_task_step_never_bootstraps_shared_runtime_or_kiro_argv(self, cfg):
        """A claude-configured task step goes through the dedicated path and
        never bootstraps the shared runtime (which would build a kiro-cli argv)."""
        cfg.agent.acp_backend = "claude"
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        try:
            # The two doors that would spawn a kiro-family runtime / build its
            # argv. If dispatch is correct for a non-runtime backend, neither is
            # ever reached from a task step.
            boundary = mgr._allocation_boundary()
            with (
                patch.object(
                    boundary,
                    "_get_or_bootstrap_run_runtime",
                    new=AsyncMock(
                        side_effect=AssertionError(
                            "claude task step bootstrapped the shared kiro runtime"
                        )
                    ),
                ) as bootstrap_spy,
                patch(
                    "kiro_crew.acp.harness.kiro.KiroHarness.resolve_spawn",
                    new=AsyncMock(
                        side_effect=AssertionError("claude task step built a kiro-cli spawn plan")
                    ),
                ) as argv_spy,
            ):
                provider, is_new, _resumed = await mgr.open_task_session(
                    "parent-session",
                    "taskrunner:run-1:step-1",
                    agent="",
                )

            assert bootstrap_spy.await_count == 0, "shared runtime must not be bootstrapped"
            assert argv_spy.await_count == 0, "no kiro-cli argv may be built"
            assert is_new is True
            assert provider is not None
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_runtime_capable_backend_still_uses_shared_runtime(self, cfg):
        """Guard against over-broadening the fix: the default kiro backend must
        still take the shared-runtime path (dispatch unchanged for it)."""
        cfg.agent.acp_backend = ACP_BACKEND_KIRO  # "" == kiro, in ACP_BACKENDS_ACP_RUNTIME
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        try:
            boundary = mgr._allocation_boundary()

            class _Reached(Exception):
                pass

            async def _bootstrap(*_a, **_k):
                # Stop before create_session so no real handle/argv is needed;
                # reaching here proves the runtime-capable path was taken.
                raise _Reached()

            with patch.object(boundary, "_get_or_bootstrap_run_runtime", new=_bootstrap):
                with pytest.raises(_Reached):
                    await mgr.open_task_session(
                        "parent-session",
                        "taskrunner:run-2:step-1",
                        agent="",
                    )
        finally:
            await mgr.close_all()
