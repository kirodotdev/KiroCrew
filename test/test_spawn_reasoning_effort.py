"""The per-call ``reasoning_effort`` parameter across every spawn_run layer.

Effort for a subagent used to resolve ONLY server-side
(``agent.role_efforts['subagent']`` -> chat default), so a parent could not
state the thinking depth its subagents run at without mutating the global
setting. ``spawn_run`` now takes a batch-wide ``reasoning_effort`` that is
plumbed along the exact path ``model`` takes: schema -> tool body ->
``POST /api/spawn`` -> ``SubagentManager.spawn`` -> the ``_run_inner``
resolution site. Each hop is a place the value can be silently dropped
(the queue and retry round-trips especially), so each hop is asserted here.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.effort import EFFORT_LEVELS
from kiro_crew.validation import SPAWN_RUN_SCHEMA, ValidationError, validate_tool_args

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _run_tool(args: dict[str, Any]) -> tuple[list[dict], str]:
    """Run spawn_run and return (POSTed bodies, returned text)."""
    from kiro_crew import mcp_core

    bodies: list[dict] = []

    def _fake_post(path: str, body: dict) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
        return {"id": "a1"}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        result = mcp_core._call_tool_inner("spawn_run", args)
    return bodies, result


class TestSchema:
    """EFFORT_VALUES is the vocabulary: every level plus '' (unset)."""

    @pytest.mark.parametrize("level", EFFORT_LEVELS)
    def test_each_concrete_level_is_accepted(self, level):
        cleaned = validate_tool_args({"task": "x", "reasoning_effort": level}, SPAWN_RUN_SCHEMA)
        assert cleaned["reasoning_effort"] == level

    def test_empty_string_means_unset_and_is_accepted(self):
        cleaned = validate_tool_args({"task": "x", "reasoning_effort": ""}, SPAWN_RUN_SCHEMA)
        assert cleaned["reasoning_effort"] == ""

    def test_absent_field_cleans_to_none(self):
        cleaned = validate_tool_args({"task": "x"}, SPAWN_RUN_SCHEMA)
        assert cleaned.get("reasoning_effort") is None

    @pytest.mark.parametrize("bad", ["ultra", "LOW", "maximum", "0", "hi gh"])
    def test_unknown_level_is_rejected(self, bad):
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "x", "reasoning_effort": bad}, SPAWN_RUN_SCHEMA)

    @pytest.mark.parametrize("bad", [1, 2.5, True, [], {}])
    def test_non_string_is_rejected(self, bad):
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "x", "reasoning_effort": bad}, SPAWN_RUN_SCHEMA)


class TestSpawnRunToolForwarding:
    """The omit-when-unset wire contract, exactly like ``model``."""

    def test_set_value_is_sent_in_the_body(self):
        bodies, _ = _run_tool({"task": "x", "reasoning_effort": "high"})
        assert len(bodies) == 1
        assert bodies[0]["reasoning_effort"] == "high"

    def test_unset_value_is_omitted_from_the_body(self):
        bodies, _ = _run_tool({"task": "x"})
        assert "reasoning_effort" not in bodies[0]

    def test_value_is_batch_wide(self):
        bodies, _ = _run_tool({"tasks": ["t1", "t2", "t3"], "reasoning_effort": "max"})
        assert len(bodies) == 3
        assert all(b["reasoning_effort"] == "max" for b in bodies)


class TestUnsupportedModelReport:
    """Effort on an incapable model is REPORTED, never a rejection."""

    def test_report_line_appears_and_spawn_still_happens(self):
        bodies, result = _run_tool(
            {"task": "x", "model": "deepseek-3.2", "reasoning_effort": "high"}
        )
        assert len(bodies) == 1  # dispatched regardless
        assert "does not support effort" in result
        assert "deepseek-3.2" in result

    def test_no_report_when_model_supports_effort(self):
        bodies, result = _run_tool(
            {"task": "x", "model": "sonnet-test-model", "reasoning_effort": "high"}
        )
        assert len(bodies) == 1
        assert "does not support effort" not in result

    def test_no_report_without_a_per_call_model(self):
        """The effective model resolves server-side — this layer cannot know
        it, so it stays silent rather than guessing."""
        bodies, result = _run_tool({"task": "x", "reasoning_effort": "high"})
        assert len(bodies) == 1
        assert "does not support effort" not in result

    def test_report_never_shadows_the_error_prefix_on_total_failure(self):
        """SEL and callers test the FIRST line for the 'Error:' prefix
        (mcp_shared logs outcome='failed' iff result.startswith('Error:')),
        so a spawn where nothing started must not lead with the ℹ line."""
        from kiro_crew import mcp_core

        def _reject_post(path: str, body: dict) -> dict:
            return {"error": "capacity reached"}

        with (
            patch.object(mcp_core, "_post", side_effect=_reject_post),
            patch.object(mcp_core, "_resolve_session_key", return_value="dash:1"),
            patch.object(mcp_core, "sel", MagicMock()),
        ):
            result = mcp_core._call_tool_inner(
                "spawn_run",
                {"task": "x", "model": "deepseek-3.2", "reasoning_effort": "high"},
            )
        assert result.startswith("Error:")
        assert "does not support effort" not in result


class TestApiSpawnHandler:
    """POST /api/spawn must not lose the cleaned value on the way to spawn()."""

    def _request(self, body: dict) -> tuple[Any, MagicMock]:
        mgr = MagicMock()
        mgr.spawn.return_value = SimpleNamespace(id="a1", done=False, error="")
        mgr.max_concurrent = 4
        state = SimpleNamespace(subagents=mgr)
        request = MagicMock()
        request.app = {"state": state}

        async def _json() -> dict:
            return body

        request.json = _json
        return request, mgr

    @pytest.mark.asyncio
    async def test_value_reaches_spawn(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x", "reasoning_effort": "xhigh"})
        await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["reasoning_effort"] == "xhigh"

    @pytest.mark.asyncio
    async def test_absent_value_reaches_spawn_as_empty(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x"})
        await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["reasoning_effort"] == ""

    @pytest.mark.asyncio
    async def test_invalid_value_is_rejected_with_400(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x", "reasoning_effort": "turbo"})
        resp = await api_spawn(request)
        assert resp.status == 400
        mgr.spawn.assert_not_called()


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.get_agent = MagicMock(return_value="")
    sessions.has_session = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    return sessions


def _mock_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return ctx


def _mgr():
    from kiro_crew.subagent import SubagentManager

    return SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())


class TestQueueRoundTrip:
    """A queued spawn must start at the effort its caller chose — the queue
    is where most members of a large fan-out sit, so a drop here is the most
    likely silent regression in this change."""

    def test_queue_entry_carries_the_value(self):
        mgr = _mgr()
        mgr._should_stagger_queue = MagicMock(return_value=(True, False))  # type: ignore[method-assign]
        info = mgr.spawn("read these files", reasoning_effort="max")
        assert info is not None and info.queued is True
        assert len(mgr._queue) == 1
        assert mgr._queue[0]["reasoning_effort"] == "max"

    def test_drained_spawn_receives_the_value(self):
        mgr = _mgr()
        mgr._should_stagger_queue = MagicMock(return_value=(True, False))  # type: ignore[method-assign]
        mgr.spawn("validate this finding", reasoning_effort="high")
        captured: dict[str, object] = {}

        def _capture(**kwargs: object) -> None:
            captured.update(kwargs)

        mgr.spawn = _capture  # type: ignore[method-assign]
        mgr._max_concurrent = 4
        mgr._running_count = 0
        mgr._spawn_stagger_secs = 0.0
        mgr._drain_queue()
        assert captured["reasoning_effort"] == "high"


class TestRecordAndRetry:
    @pytest.mark.asyncio
    async def test_spawn_threads_the_value_onto_info(self):
        mgr = _mgr()
        mgr._run = AsyncMock()  # type: ignore[method-assign]
        info = mgr.spawn("do the thing", reasoning_effort="low")
        assert info is not None
        assert info.reasoning_effort == "low"

    @pytest.mark.asyncio
    async def test_default_is_empty_meaning_defer_to_pin(self):
        mgr = _mgr()
        mgr._run = AsyncMock()  # type: ignore[method-assign]
        info = mgr.spawn("do the thing")
        assert info is not None
        assert info.reasoning_effort == ""

    @pytest.mark.asyncio
    async def test_retry_re_spawns_at_the_same_effort(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn_retry

        old = SimpleNamespace(
            id="a1",
            task="t",
            _raw_task="t",
            parent_session_key="dash:1",
            agent="",
            max_turns=0,
            cwd="",
            model="",
            reasoning_effort="xhigh",
            approval_mode="",
            silent=False,
            include_memory=True,
            include_lessons=True,
            include_project=True,
            done=True,
            outcome="failed",
        )
        mgr = MagicMock()
        mgr.get.return_value = old
        mgr.spawn.return_value = SimpleNamespace(id="a2", done=False, error="")
        state = SimpleNamespace(subagents=mgr)
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"agent_id": "a1"}
        await api_spawn_retry(request)
        assert mgr.spawn.call_args.kwargs["reasoning_effort"] == "xhigh"


class TestResolutionPrecedence:
    """The behavior change itself: per-call value -> role pin -> default,
    and a per-call effort forces the dedicated (non-shared) session path
    exactly as a role pin already does."""

    def _run(self, *, info_effort: str = "", role_efforts=None):
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_approval_policy = MagicMock(return_value="")
        sessions.get_agent = MagicMock(return_value="")
        ctx_builder = MagicMock()
        ctx_builder.build_message = MagicMock(return_value=("msg", None))
        ctx_builder.hooks.auto_approve_subagent_tools = False

        captured: dict = {}
        mock_client = MagicMock()

        async def fake_get_or_create(key, agent=None, approval_policy="", **kwargs):
            captured.update(kwargs)
            return mock_client, True, False

        sessions.get_or_create = fake_get_or_create

        async def fake_stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE)

        mock_client.stream = fake_stream

        cfg = KiroCrewConfig(agent=AgentConfig(role_efforts=role_efforts or {}))
        runner = SubagentManager(sessions=sessions, ctx_builder=ctx_builder)
        shared = AsyncMock(
            side_effect=AssertionError("shared path taken despite an effort override")
        )
        info = SubagentInfo(
            id="sub1",
            task="test",
            parent_session_key="parent-key",
            reasoning_effort=info_effort,
        )
        with (
            patch.object(runner, "_create_shared_session", shared),
            patch.object(runner, "_should_use_session_sharing", return_value=True),
            patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
        ):
            asyncio.run(runner._run_inner(info, "subagent:sub1"))
        return captured, shared

    def test_per_call_value_beats_the_role_pin(self):
        captured, shared = self._run(info_effort="max", role_efforts={"subagent": "low"})
        shared.assert_not_called()
        assert captured.get("reasoning_effort_override") == "max"

    def test_pin_still_applies_when_per_call_value_is_absent(self):
        """The no-regression case: an absent per-call value changes nothing."""
        captured, shared = self._run(role_efforts={"subagent": "low"})
        shared.assert_not_called()
        assert captured.get("reasoning_effort_override") == "low"

    def test_per_call_value_forces_dedicated_path_with_no_pin(self):
        captured, shared = self._run(info_effort="high")
        shared.assert_not_called()
        assert captured.get("reasoning_effort_override") == "high"
