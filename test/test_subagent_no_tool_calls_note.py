"""A sub-agent run that made no tool call is flagged beside its result.

Such a run wrote no file, ran no command and made no commit, but it can still
answer a task that asked for one, usually by narrating the steps -- the shape a
reasoning-only agent (``"tools": []``) takes when handed write work. The note
rides beside the result the parent reads: the ``[Subagent completion event]`` of
a lone run, each member's entry in a wave digest, and each ``spawn_sub_agents``
record. The count is the run's own observed tool calls, so the note needs no
reading of specs or backends.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.constants import NO_TOOL_CALLS_NOTE
from kiro_crew.subagent import SubagentInfo

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _spawn_run(args: dict[str, Any]) -> str:
    from kiro_crew import mcp_core

    counter = iter(range(1, 100))

    def _fake_post(path: str, body: dict) -> dict:
        return {"id": f"a{next(counter)}"}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        return mcp_core._call_tool_inner("spawn_run", args)


class TestSpawnRunDispatch:
    """The note rides on the completion, where the narration arrives, not here."""

    def test_the_dispatch_is_not_refused_and_carries_no_note(self):
        out = _spawn_run({"task": "write a.py", "agent": "kirocrew-lite"})
        assert out.startswith("Spawned 1 subagent(s).")
        assert NO_TOOL_CALLS_NOTE not in out


class TestSpawnSubAgentsResultNote:
    def _collect(self, status: dict[str, Any]) -> dict[str, Any]:
        from kiro_crew import mcp_core

        with (
            patch.object(mcp_core, "_post", return_value={"id": "a1"}),
            patch.object(
                mcp_core,
                "_get",
                return_value={"done": True, "result": "wrote a.py, commit abc", **status},
            ),
            patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
            patch.object(mcp_core, "sel", MagicMock()),
        ):
            out = mcp_core._call_tool_inner(
                "spawn_sub_agents",
                {"agents": [{"agent_or_mode": "kirocrew-lite", "prompt": "write a.py"}]},
            )
        return json.loads(out.split("\n\n")[0])

    def test_a_result_from_a_run_with_no_tool_call_carries_the_unverified_field(self):
        record = self._collect({"made_no_tool_calls": True})
        assert record["status"] == "completed"
        assert record["unverified"] == f"This agent {NO_TOOL_CALLS_NOTE}"

    def test_a_result_from_a_run_that_called_tools_has_none(self):
        assert "unverified" not in self._collect({"made_no_tool_calls": False})

    @pytest.mark.parametrize(
        "status", [{}, {"made_no_tool_calls": None}, {"made_no_tool_calls": 1}]
    )
    def test_a_status_without_a_count_attaches_nothing(self, status):
        assert "unverified" not in self._collect(status)


class TestSpawnStatusSaysWhetherToolsWereCalled:
    async def _status(self, info: SubagentInfo) -> dict[str, Any]:
        from kiro_crew.dashboard.messaging_api.run_views import api_spawn_status

        state = MagicMock()
        state.subagents.get = MagicMock(return_value=info)
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"agent_id": info.id}
        request.query = {}
        return json.loads((await api_spawn_status(request)).body)

    def _done(self, **kw: Any) -> SubagentInfo:
        info = SubagentInfo(id="a1", task="write a.py")
        info.done = True
        info.result = "done"
        for k, v in kw.items():
            setattr(info, k, v)
        return info

    @pytest.mark.asyncio
    async def test_a_completed_run_with_no_tool_call_says_so(self):
        assert (await self._status(self._done()))["made_no_tool_calls"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kw", [{"tool_count": 2}, {"turns": 1}])
    async def test_a_tool_call_or_a_permission_request_counts(self, kw):
        assert (await self._status(self._done(**kw)))["made_no_tool_calls"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kw", [{"user_stopped": True}, {"error": "boom"}])
    async def test_a_stopped_or_failed_run_does_not_say(self, kw):
        assert "made_no_tool_calls" not in await self._status(self._done(**kw))


def _make_orchestrator():
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.slack.gateway import GatewayOrchestrator

    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        return GatewayOrchestrator(cfg, no_dashboard=False, no_crons=True, no_open=True)


def _dashboard_state() -> MagicMock:
    ds = MagicMock()
    ds._slots = {}
    ds._yolo = False
    ds.request_approval = AsyncMock(return_value=True)
    ds.resolve_slot = MagicMock(return_value=None)
    ds.close_all_ws = AsyncMock()
    ds._background_tasks = set()
    slot = MagicMock()
    slot.mode = "chat"
    slot.running = False
    slot.task = None
    slot._subagent_deliveries_inflight = 0
    ds.get_slot = MagicMock(return_value=slot)
    return ds


class TestCompletionEventNote:
    """A lone run or a single wave gets no synthesis turn, so the completion
    event the parent reads the narration from must carry the note itself."""

    def _on_done(self):
        orch = _make_orchestrator()
        sessions = MagicMock()
        sessions.cancel_current = AsyncMock()
        orch.sessions = sessions
        orch.ctx_builder = MagicMock()
        orch.dashboard_state = _dashboard_state()
        with (
            patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False),
            patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm,
        ):
            mgr = MagicMock()
            mock_sm.return_value = mgr
            orch._init_subagents()
            orch.subagent_mgr = mgr
            mgr.wave_has_live_nested_spawns = MagicMock(return_value=False)
            return orch, mgr, mock_sm.call_args.kwargs["on_done"]

    def _info(self, aid: str, tool_count: int, *, turns: int = 0, **kw: Any) -> SubagentInfo:
        info = SubagentInfo(
            id=aid,
            task="write a.py and commit it",
            parent_session_key="dashboard:main",
            **kw,
        )
        info.agent = "kirocrew-lite"
        info.done = True
        info.tool_count = tool_count
        info.turns = turns
        info.result = "Wrote a.py and committed it as abc123."
        return info

    async def _deliver(self, infos: list[SubagentInfo]) -> list[str]:
        orch, mgr, on_done = self._on_done()
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, **_kw):
            injected.append(text)

        with (
            patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered"),
        ):
            for n, info in enumerate(infos):
                mgr.batch_members_pending = MagicMock(return_value=n != len(infos) - 1)
                await on_done(info)
                await asyncio.sleep(0)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            while not injected:
                if loop.time() >= deadline:
                    raise AssertionError("the completion event was never injected")
                await asyncio.sleep(0.02)
            pending = [t for t in orch.dashboard_state._background_tasks if not t.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return injected

    @pytest.mark.asyncio
    async def test_a_lone_run_with_no_tool_call_carries_the_note_in_its_completion_event(self):
        (event,) = await self._deliver([self._info("a1", 0)])
        assert event.startswith("[Subagent completion event]")
        assert event.rstrip().endswith(f"⚠ Agent `a1` {NO_TOOL_CALLS_NOTE}")

    @pytest.mark.asyncio
    async def test_a_lone_run_that_called_tools_carries_no_note(self):
        (event,) = await self._deliver([self._info("a1", 4)])
        assert NO_TOOL_CALLS_NOTE not in event

    @pytest.mark.asyncio
    async def test_a_run_whose_tools_arrived_only_as_permission_requests_carries_no_note(self):
        (event,) = await self._deliver([self._info("a1", 0, turns=2)])
        assert NO_TOOL_CALLS_NOTE not in event

    @pytest.mark.asyncio
    async def test_a_stopped_run_with_no_tool_call_carries_no_note(self):
        info = self._info("a1", 0)
        info.user_stopped = True
        (event,) = await self._deliver([info])
        assert NO_TOOL_CALLS_NOTE not in event

    @pytest.mark.asyncio
    async def test_a_failed_run_with_no_tool_call_carries_no_note(self):
        info = self._info("a1", 0)
        info.error = "boom"
        (event,) = await self._deliver([info])
        assert NO_TOOL_CALLS_NOTE not in event

    @pytest.mark.asyncio
    async def test_a_single_wave_digest_flags_only_the_member_with_no_tool_call(self):
        wave = {"batch_id": "w", "batch_total": 2}
        (digest,) = await self._deliver([self._info("m0", 0, **wave), self._info("m1", 2, **wave)])
        assert digest.startswith("[Subagent batch completion event]")
        assert digest.count(NO_TOOL_CALLS_NOTE) == 1
        assert f"⚠ Agent `m0` {NO_TOOL_CALLS_NOTE}" in digest
        assert "Agent `m1`" not in digest
