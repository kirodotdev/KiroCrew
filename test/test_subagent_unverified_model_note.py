"""A pinned sub-agent run whose served model never resolved tells the parent so.
An unpinned run, a failed run, or one whose served model is known, gets no note."""

from __future__ import annotations

from typing import Any

import pytest
from idle_tab_drain_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _idle_tab_drains_head_as_turn,
)
from test_subagent_no_tool_calls_note import TestCompletionEventNote as _CompletionHarness

from kiro_crew.subagent import SubagentInfo

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_MARK = "has its served model unverified"


class TestTheParentIsToldTheServedModelIsUnverified:
    _harness = _CompletionHarness()

    def _info(self, aid: str, *, requested: str, resolved: str, **kw: Any) -> SubagentInfo:
        info = self._harness._info(aid, 2, **kw)
        info.requested_model = requested
        info.resolved_model = resolved
        return info

    async def _deliver(self, infos: list[SubagentInfo]) -> list[str]:
        return await self._harness._deliver(infos)

    @pytest.mark.asyncio
    async def test_a_pinned_run_with_no_served_model_carries_the_note(self):
        (event,) = await self._deliver([self._info("a1", requested="gpt-6.1-sol", resolved="")])
        assert "completed ✅" in event
        assert f"⚠ Agent `a1` {_MARK}" in event

    @pytest.mark.asyncio
    async def test_a_pinned_run_with_a_served_model_has_no_note(self):
        info = self._info("a1", requested="claude-opus-5.5", resolved="claude-opus-5.5")
        (event,) = await self._deliver([info])
        assert _MARK not in event

    @pytest.mark.asyncio
    @pytest.mark.parametrize("requested", ["auto", "AUTO", "", "  "])
    async def test_an_unpinned_run_with_no_served_model_has_no_note(self, requested):
        (event,) = await self._deliver([self._info("a1", requested=requested, resolved="")])
        assert _MARK not in event

    @pytest.mark.asyncio
    async def test_a_failed_pinned_run_has_no_note(self):
        info = self._info("a1", requested="gpt-6.1-sol", resolved="")
        info.error = "boom"
        (event,) = await self._deliver([info])
        assert _MARK not in event

    @pytest.mark.asyncio
    async def test_a_wave_digest_flags_only_the_unresolved_member(self):
        wave = {"batch_id": "w", "batch_total": 2}
        (digest,) = await self._deliver(
            [
                self._info("m0", requested="gpt-6.1-sol", resolved="", **wave),
                self._info("m1", requested="claude-opus-5.5", resolved="claude-opus-5.5", **wave),
            ]
        )
        assert digest.count(_MARK) == 1
        assert f"⚠ Agent `m0` {_MARK}" in digest


class TestTheStatusToolsCarryIt:
    @pytest.mark.parametrize(
        ("requested", "resolved", "expected"),
        [("gpt-6.1-sol", "", True), ("gpt-6.1-sol", "gpt-6.1-sol", False), ("auto", "", False)],
    )
    def test_the_run_record_names_an_unresolved_pin(self, requested, resolved, expected):
        info = SubagentInfo(id="a1", task="t")
        info.requested_model = requested
        info.resolved_model = resolved
        assert info.served_model_unverified is expected

    @pytest.mark.asyncio
    async def test_the_spawn_status_api_reports_it_for_a_completed_run(self):
        import json
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.messaging_api.run_views import api_spawn_status

        info = SubagentInfo(id="a1", task="t")
        info.done = True
        info.result = "done"
        info.tool_count = 1
        info.requested_model = "gpt-6.1-sol"
        state = MagicMock()
        state.subagents.get = MagicMock(return_value=info)
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"agent_id": "a1"}
        request.query = {}
        data = json.loads((await api_spawn_status(request)).body)
        assert data["served_model_unverified"] is True

    def test_the_spawn_sub_agents_record_carries_the_note(self):
        from test_subagent_no_tool_calls_note import TestSpawnSubAgentsResultNote

        record = TestSpawnSubAgentsResultNote()._collect({"served_model_unverified": True})
        assert record["status"] == "completed"
        assert _MARK in record["served_model_unverified"]

    @pytest.mark.parametrize("value", [False, None, 1])
    def test_the_spawn_sub_agents_record_without_a_true_flag_has_none(self, value):
        from test_subagent_no_tool_calls_note import TestSpawnSubAgentsResultNote

        record = TestSpawnSubAgentsResultNote()._collect({"served_model_unverified": value})
        assert "served_model_unverified" not in record

    def test_spawn_status_puts_the_note_ahead_of_the_result(self):
        from test_status_tools_carry_completion_warnings import _call

        payload = {"done": True, "result": "LGTM", "served_model_unverified": True}
        out = _call("spawn_status", {"agent_id": "a1"}, payload)
        assert _MARK in out
        assert out.index(_MARK) < out.index("LGTM")
