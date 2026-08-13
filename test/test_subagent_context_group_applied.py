"""``_run_inner`` must hand the run's context scope to ``build_message``.

Every other test in this feature checks a hop in isolation: the flags land on
``SubagentInfo``, survive the queue, survive a retry, and the builder gates
sections when handed a group set. None of them prove the two halves are
connected — if the ``context_groups=`` argument were dropped from the
``build_message`` call, all of those would still pass while the feature did
nothing at all. This drives one real subagent turn and asserts on the argument.

The harness mirrors ``test_turn_duration_subagent.py``: a mocked session whose
provider streams a scripted turn, and a mocked ``ContextBuilder`` so the
assertion is on the call rather than on rendered prompt text.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.subagent import SubagentManager


def _stream(*_a: object, **_k: object):
    async def _gen():
        yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="done")
        yield SimpleNamespace(kind=EVENT_COMPLETE, stop_reason="end_turn")

    return _gen()


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    provider.stream = MagicMock(side_effect=_stream)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    return sessions


async def _groups_passed_for(**spawn_kwargs: object) -> frozenset[str]:
    """Spawn one subagent and return the ``context_groups`` build_message saw."""
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=ctx)
    mgr._should_use_session_sharing = MagicMock(return_value=False)  # type: ignore[method-assign]
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("do the thing", **spawn_kwargs)  # type: ignore[arg-type]
        assert info is not None
        await mgr._tasks[info.id]
    assert ctx.build_message.called, "build_message was never reached"
    return ctx.build_message.call_args.kwargs["context_groups"]


class TestRunInnerAppliesTheScope:
    @pytest.mark.asyncio
    async def test_withheld_group_is_absent_from_the_group_set(self):
        groups = await _groups_passed_for(include_memory=False)
        assert groups == frozenset({"lessons", "project"})

    @pytest.mark.asyncio
    async def test_default_spawn_passes_every_group(self):
        """All-on is passed explicitly, and is equivalent to the None default."""
        groups = await _groups_passed_for()
        assert groups == frozenset({"memory", "lessons", "project"})

    @pytest.mark.asyncio
    async def test_conduct_only_run_passes_an_empty_set(self):
        groups = await _groups_passed_for(
            include_memory=False, include_lessons=False, include_project=False
        )
        assert groups == frozenset()
