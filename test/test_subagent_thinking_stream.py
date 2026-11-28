"""A managed ``spawn_run`` child's thinking reaches its card and its status read.

The run loop sends its thinking, stream-redacted, as ``subagent_thinking`` frames and keeps
it in ``SubagentInfo.streaming_thinking``, never in the reply; ``GET /api/spawn/{id}``
carries it as ``thinking`` while the run is live; and above the coalescer's threshold the
frame rides the same ``subagent_batch_chunks`` item as the agent's text.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from handlers_messaging_coverage_helpers import _payload, _Req, _run, _state
from redaction_structured_cases import aws_key_id
from subagent_helpers import _mock_ctx_builder_auto_spawn, _mock_sessions

import kiro_crew.dashboard.handlers.messaging as messaging
from kiro_crew.execution_context import execution_for_store
from kiro_crew.providers.base import (
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    LLMEvent,
)
from kiro_crew.security import REDACTED_CREDENTIAL_TAG
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_scale import SubagentEventCoalescer
from kiro_crew.testing.synthetic_credentials import SAMPLE_AWS_SECRET
from kiro_crew.testing.wait import await_until

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input; and
# every manager a test builds is closed, so its task store's files do not leak.
pytestmark = pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")

# A synthetic 40-character AWS secret access key beside its key ID.
_KEY = f"{aws_key_id()} {SAMPLE_AWS_SECRET}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "between",
    [
        [],
        # A native child's activity mid-thought does not end the run's thinking.
        [LLMEvent(kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id="child-1", text="reading")],
        # Nor does a child's tool call, whose frame after its activity prefix names no child.
        [
            LLMEvent(kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id="child-1", tool_call_id="c1"),
            LLMEvent(kind=EVENT_TOOL_CALL, tool_call_id="c1", title="Read file"),
        ],
    ],
    ids=["thinking-only", "child-activity-between", "child-tool-call-between"],
)
async def test_a_managed_run_streams_its_thinking_apart_from_its_reply(
    between: list[LLMEvent],
) -> None:
    # The secret is split across two thinking chunks: each alone matches nothing.
    half = len(SAMPLE_AWS_SECRET) // 2

    async def _stream(*_a, **_kw):  # type: ignore[no-untyped-def]
        yield LLMEvent(kind=EVENT_THINKING_CHUNK, text=f"Reading the config: {aws_key_id()} ")
        yield LLMEvent(kind=EVENT_THINKING_CHUNK, text=SAMPLE_AWS_SECRET[:half])
        for event in between:
            yield event
        yield LLMEvent(kind=EVENT_THINKING_CHUNK, text=f"{SAMPLE_AWS_SECRET[half:]} is set.")
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done.")

    provider = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
    sessions = _mock_sessions()
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    manager = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder_auto_spawn())
    info = SubagentInfo(execution_context=execution_for_store(""), id="th1", task="t")
    manager._log_spawned(info)
    frames: list[tuple[str, dict]] = []

    async def _record(etype: str, _info: object, extra: dict | None = None) -> None:
        frames.append((etype, extra or {}))

    with (
        patch.object(manager, "_fire_event", side_effect=_record),
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
    ):
        await manager._run_inner(info, "subagent:th1")

    sent = "".join(extra["text"] for etype, extra in frames if etype == "subagent_thinking")
    assert sent.startswith("Reading the config: ") and sent.endswith(" is set.")
    assert SAMPLE_AWS_SECRET[:half] not in sent and SAMPLE_AWS_SECRET[half:] not in sent
    assert REDACTED_CREDENTIAL_TAG in sent
    # Every thinking frame lands before the reply, the thought's last word included.
    kinds = [etype for etype, _ in frames]
    assert max(i for i, k in enumerate(kinds) if k == "subagent_thinking") < kinds.index(
        "subagent_chunk"
    )
    assert [extra["text"] for etype, extra in frames if etype == "subagent_chunk"] == ["Done."]
    assert ("subagent_tool" in kinds) == any(e.kind == EVENT_TOOL_CALL for e in between)
    assert info.streaming_thinking == sent
    assert info.streaming_text == "Done."


@pytest.mark.parametrize("query", [{}, {"offset": "1", "limit": "1"}])
def test_the_status_read_carries_a_live_runs_thinking(query: dict) -> None:
    info = SubagentInfo(id="a1", task="t", streaming_text="l0\nl1\nl2")
    info.streaming_thinking = f"weighing {_KEY}"
    mgr = MagicMock()
    mgr.get.return_value = info
    req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"}, query=query)

    data = _payload(_run(messaging.api_spawn_status, req))

    assert data["thinking"] == messaging._redact(f"weighing {_KEY}")
    assert SAMPLE_AWS_SECRET not in data["thinking"]
    info.streaming_thinking = ""
    assert "thinking" not in _payload(_run(messaging.api_spawn_status, req))


def _coalescer(
    active: int, *, tick_secs: float = 0.02, to_all: list[tuple[str, dict]] | None = None
) -> tuple[SubagentEventCoalescer, list[tuple[str, dict]]]:
    subs: list[tuple[str, dict]] = []
    sent_to_all = [] if to_all is None else to_all
    c = SubagentEventCoalescer(
        lambda t, d: sent_to_all.append((t, d)),
        lambda t, d: subs.append((t, d)),
        lambda: active,
        threshold=8,
        tick_secs=tick_secs,
    )
    return c, subs


@pytest.mark.asyncio
async def test_thinking_rides_the_batched_chunk_item_above_the_threshold() -> None:
    c, subs = _coalescer(active=20)
    assert c.handle("subagent_chunk", {"id": "a", "slot": "s", "text": "out"}) is True
    assert c.handle("subagent_thinking", {"id": "a", "slot": "s", "text": "why "}) is True
    assert c.handle("subagent_thinking", {"id": "b", "slot": "s", "text": "hmm"}) is True
    assert c._tick_task is not None
    await await_until(c._tick_task, what="coalescer flush tick")

    assert subs == [
        (
            "subagent_batch_chunks",
            {
                "chunks": [
                    {"id": "a", "slot": "s", "text": "out", "thinking": "why "},
                    {"id": "b", "slot": "s", "text": "", "thinking": "hmm"},
                ]
            },
        )
    ]
    below, below_subs = _coalescer(active=1)
    assert below.handle("subagent_thinking", {"id": "a", "slot": "s", "text": "x"}) is False
    assert below_subs == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("etype", "item"),
    [
        ("subagent_thinking", {"id": "a", "slot": "s", "text": "", "thinking": "why"}),
        ("subagent_chunk", {"id": "a", "slot": "s", "text": "why"}),
    ],
    ids=["thinking", "text"],
)
async def test_a_retry_lands_after_the_chunk_item_buffered_before_it(
    etype: str, item: dict
) -> None:
    # A chunk item clears the card's retrying flag, so the one buffered before
    # a retry reaches the client first; the long tick keeps the timer out of it.
    to_all: list[tuple[str, dict]] = []
    c, subs = _coalescer(active=20, tick_secs=5.0, to_all=to_all)
    assert c.handle(etype, {"id": "a", "slot": "s", "text": "why"}) is True
    assert c.handle("subagent_retrying", {"id": "a", "slot": "s", "attempt": 1}) is True

    assert subs == [("subagent_batch_chunks", {"chunks": [item]})]
    assert to_all == []
    c.close()
    assert to_all == [
        ("subagent_batch_update", {"updates": [{"id": "a", "slot": "s", "attempt": 1}]})
    ]
    assert len(subs) == 1
