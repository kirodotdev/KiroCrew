"""A sub-agent run whose final tool call leaked into its text is flagged.

The model sometimes writes its last tool call into the text channel instead of
running it, then ends the turn normally. The stop reason says success, so the
parent got a plain completed run and no sign that the call never ran. The run
now records ``leaked_tool_call`` from its FINAL segment (the text after its last
tool call, read with the dashboard's own ``has_leaked_tool_call``), carries it on
the ``subagent_done`` event, and the parent reads a note beside the result.
Notice-only: the outcome stays completed, because earlier calls may have run.

The run tests drive the real ``SubagentManager`` path with only the ACP session
provider faked (the seam ``test_subagent_stop_reason_consistency`` uses).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from idle_tab_drain_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _idle_tab_drains_head_as_turn,
)
from subagent_stop_reason_helpers import (
    _complete,
    _done_event,
    _manager,
    _mock_sessions,
    _spawn_and_wait,
    _spy_events,
    _text,
)
from test_subagent_no_tool_calls_note import TestCompletionEventNote as _CompletionHarness

from kiro_crew.acp.types import STOP_REASON_CANCELLED, STOP_REASON_END_TURN
from kiro_crew.constants import LEAKED_TOOL_CALL_NOTE, NO_TOOL_CALLS_NOTE
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, EVENT_TOOL_CALL, LLMEvent
from kiro_crew.subagent import SubagentInfo

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# The shape from the report: prose, a stray word, then the call as text.
_LEAK = (
    "Now I will list the directory and search for the value.\n\ncall\n"
    '<invoke name="read">\n'
    '<parameter name="path">/tmp/fx</parameter>\n'
    "</invoke>\n"
)


def _tool_call(n: int) -> SimpleNamespace:
    return SimpleNamespace(
        kind=EVENT_TOOL_CALL,
        title="read",
        tool_kind="read",
        tool_call_id=f"t{n}",
        tool_input="{}",
        text="",
        runtime_global=False,
    )


def _permission_request() -> LLMEvent:
    """The run's own gated call, surfaced only as a permission request."""
    return LLMEvent(kind=EVENT_PERMISSION_REQUEST, title="read", request_id="r1", tool_name="read")


def _stream(*events_and_texts: Any, stop_reason: str = STOP_REASON_END_TURN):
    """One stream call: each item is a tool call (an int), the run's own
    permission request (``"perm"``) or a text chunk (any other str)."""

    def stream_factory(msg: str, *a, **kw):
        async def _gen():
            for item in events_and_texts:
                if isinstance(item, int):
                    yield _tool_call(item)
                elif item == "perm":
                    yield _permission_request()
                else:
                    yield _text(item)
            yield _complete(stop_reason)

        return _gen()

    return stream_factory


async def _run(factory) -> tuple[SubagentInfo, dict]:
    mgr = _manager(_mock_sessions(factory))
    events = _spy_events(mgr)
    info = await _spawn_and_wait(mgr)
    return info, _done_event(events)


class TestTheRunRecordsTheLeak:
    @pytest.mark.asyncio
    async def test_a_mixed_turn_whose_final_call_leaked_is_flagged_and_still_completed(self):
        info, done = await _run(_stream("Reading the file first.\n", 1, _LEAK))
        assert info.outcome == "completed" and info.error == ""
        assert info.leaked_tool_call is True
        assert done["outcome"] == "completed"
        assert done["leaked_tool_call"] is True

    @pytest.mark.asyncio
    async def test_a_run_with_no_tool_call_that_leaked_is_flagged(self):
        info, done = await _run(_stream(_LEAK))
        assert info.leaked_tool_call is True and done["leaked_tool_call"] is True

    @pytest.mark.asyncio
    async def test_a_leak_before_a_tool_that_then_ran_is_not_flagged(self):
        """Only the FINAL segment counts: a later real call ends that segment."""
        info, done = await _run(_stream(_LEAK, 1, "All done: NONCE=42.\n"))
        assert info.leaked_tool_call is False and done["leaked_tool_call"] is False

    @pytest.mark.asyncio
    async def test_a_leak_before_a_call_surfaced_only_as_a_permission_request_is_not_flagged(
        self,
    ):
        """A backend may surface a gated call ONLY as the run's own permission
        request (no tool_call frame); that is a boundary too."""
        info, done = await _run(_stream(_LEAK, "perm", "All done: NONCE=42.\n"))
        assert info.turns == 1 and info.tool_count == 0
        assert info.leaked_tool_call is False and done["leaked_tool_call"] is False

    @pytest.mark.asyncio
    async def test_a_quoted_invoke_block_is_not_a_leak(self):
        info, _ = await _run(_stream(1, f"The bad output looked like:\n\n```\n{_LEAK}```\n"))
        assert info.leaked_tool_call is False

    @pytest.mark.asyncio
    async def test_a_plain_answer_is_not_flagged(self):
        info, done = await _run(_stream(1, "NONCE=42\n"))
        assert info.leaked_tool_call is False and done["leaked_tool_call"] is False

    @pytest.mark.asyncio
    async def test_a_cancelled_run_is_not_flagged(self):
        """A non-success ending owns its own report (error / partial)."""
        info, _ = await _run(_stream(1, _LEAK, stop_reason=STOP_REASON_CANCELLED))
        assert info.leaked_tool_call is False
        assert info.outcome != "completed"


class TestTheParentIsTold:
    """The note rides in the completion event the parent reads, like the no-tool-calls note."""

    # Its delivery helpers, used without inheriting (and re-running) its tests.
    _harness = _CompletionHarness()

    def _info(self, aid: str, tool_count: int, **kw: Any) -> SubagentInfo:
        return self._harness._info(aid, tool_count, **kw)

    async def _deliver(self, infos: list[SubagentInfo]) -> list[str]:
        return await self._harness._deliver(infos)

    def _leaked(self, aid: str, tool_count: int, **kw: Any) -> SubagentInfo:
        info = self._info(aid, tool_count, **kw)
        info.leaked_tool_call = True
        return info

    @pytest.mark.asyncio
    async def test_a_lone_leaked_run_carries_the_note(self):
        (event,) = await self._deliver([self._leaked("a1", 2)])
        assert "completed ✅" in event
        assert event.rstrip().endswith(f"⚠ Agent `a1` {LEAKED_TOOL_CALL_NOTE}")
        assert NO_TOOL_CALLS_NOTE not in event

    @pytest.mark.asyncio
    async def test_a_leaked_run_with_no_tool_call_carries_both_notes(self):
        (event,) = await self._deliver([self._leaked("a1", 0)])
        assert f"⚠ Agent `a1` {NO_TOOL_CALLS_NOTE}" in event
        assert f"⚠ Agent `a1` {LEAKED_TOOL_CALL_NOTE}" in event

    @pytest.mark.asyncio
    async def test_a_failed_run_carries_no_leak_note(self):
        info = self._leaked("a1", 2)
        info.error = "boom"
        (event,) = await self._deliver([info])
        assert LEAKED_TOOL_CALL_NOTE not in event

    @pytest.mark.asyncio
    async def test_a_wave_digest_flags_only_the_leaked_member(self):
        wave = {"batch_id": "w", "batch_total": 2}
        (digest,) = await self._deliver(
            [self._leaked("m0", 2, **wave), self._info("m1", 2, **wave)]
        )
        assert digest.count(LEAKED_TOOL_CALL_NOTE) == 1
        assert f"⚠ Agent `m0` {LEAKED_TOOL_CALL_NOTE}" in digest


class TestStatusAndBlockingWave:
    @pytest.mark.asyncio
    async def test_spawn_status_says_the_last_call_leaked(self):
        import json
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.messaging_api.run_views import api_spawn_status

        info = SubagentInfo(id="a1", task="t")
        info.done = True
        info.result = "done"
        info.tool_count = 1
        info.leaked_tool_call = True
        state = MagicMock()
        state.subagents.get = MagicMock(return_value=info)
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"agent_id": "a1"}
        request.query = {}
        data = json.loads((await api_spawn_status(request)).body)
        assert data["leaked_tool_call"] is True

    def test_spawn_sub_agents_record_carries_the_note(self):
        from test_subagent_no_tool_calls_note import TestSpawnSubAgentsResultNote

        record = TestSpawnSubAgentsResultNote()._collect(
            {"made_no_tool_calls": False, "leaked_tool_call": True}
        )
        assert record["status"] == "completed"
        assert record["leaked_tool_call"] == f"This agent {LEAKED_TOOL_CALL_NOTE}"
        assert "unverified" not in record

    @pytest.mark.parametrize("status", [{}, {"leaked_tool_call": False}, {"leaked_tool_call": 1}])
    def test_spawn_sub_agents_record_without_a_true_flag_has_none(self, status):
        from test_subagent_no_tool_calls_note import TestSpawnSubAgentsResultNote

        assert "leaked_tool_call" not in TestSpawnSubAgentsResultNote()._collect(status)
