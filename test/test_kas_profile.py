"""The KAS engine's per-turn profile: what the session handle asks for and keeps.

The frames here are real ones, captured from the engine and scrubbed of local names
(``test/fixtures/acp_frames/kas/profile/``): a summary-mode ``turn_completion``, a
detailed one, one from a prompt that did not opt in, the first displayed chunk with its
``ttft`` (a text turn and a tool-only turn whose first output is reasoning), and the
``session/load`` replays of a completion and of a turn's timing.

They go through the REAL handle dispatch (``AcpSessionHandle._handle_update``) and, for
the opt-in, through a real ``prompt()`` against a runtime whose send is recorded.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.acp import kas_profile
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    EVENT_COMPLETE,
    JsonRpcMessage,
)
from kiro_crew.testing.wait import finished

FRAMES = Path(__file__).parent / "fixtures" / "acp_frames" / "kas" / "profile"


def _frame(name: str) -> dict[str, Any]:
    return json.loads((FRAMES / name).read_text(encoding="utf-8"))


def _update_of(name: str) -> dict[str, Any]:
    return _frame(name)["params"]["update"]


def _handle(backend: str = ACP_BACKEND_KAS) -> AcpSessionHandle:
    rt = MagicMock()
    rt.acp_backend = backend
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    return AcpSessionHandle("sA", asyncio.Queue(), rt)


def _in_turn(handle: AcpSessionHandle) -> AcpSessionHandle:
    """Mark a prompt in flight, the state every live frame of a turn arrives in."""
    handle._turn_done.clear()
    return handle


def _dispatch(handle: AcpSessionHandle, update: dict[str, Any]) -> list:
    return handle._handle_update(JsonRpcMessage(method="session/update", params={"update": update}))


# --------------------------------------------------------------------------- #
# Parsing the real frames
# --------------------------------------------------------------------------- #


def test_the_summary_frame_parses_to_its_categories_and_history_split():
    kiro = _update_of("turn_completion.summary.json")["_meta"]["kiro"]
    parsed = kas_profile.parse_breakdown(kiro["contextBreakdown"])
    assert parsed is not None
    assert parsed["schema"] == 1
    assert parsed["model_calls"] == 1
    assert parsed["total_chars"] == 59_333
    assert set(parsed["categories"]) == set(kas_profile.KNOWN_CATEGORIES)
    # The categories add up to the request's total, as the engine's contract states.
    assert sum(parsed["categories"].values()) == parsed["total_chars"]
    assert parsed["history"] == {
        "user": 12_173,
        "assistant": 10,
        "thinking": 9,
        "compaction_summary": 0,
    }
    assert "compacted" not in parsed


def test_the_summary_frame_keeps_its_shares_counts_splits_window_and_media():
    """The fields beside ``chars`` that a summary frame carries are kept as numbers:
    each category's own percent (in tenths), the three lists' entry counts, the
    toolIO / workspace / memory splits, the window, and the media outside the total."""
    kiro = _update_of("turn_completion.summary.json")["_meta"]["kiro"]
    parsed = kas_profile.parse_breakdown(kiro["contextBreakdown"])
    assert parsed is not None
    assert parsed["percent"]["steering"] == 305
    assert parsed["percent"]["other"] == 1
    assert sum(parsed["percent"].values()) == 1000
    assert parsed["counts"] == {"agentsIndex": 0, "skillsIndex": 28, "powersIndex": 0}
    assert parsed["parts"]["toolIO.input"] == 20
    assert parsed["parts"]["toolIO.output"] == 82
    assert parsed["parts"]["workspace.environment"] == 939
    assert parsed["parts"]["memory.learnings"] == 0
    assert parsed["window"] == {"size_tokens": 1_000_000, "usage_milli": 2867}
    assert parsed["media"] == {"images": 0, "image_bytes": 0, "documents": 0, "document_bytes": 0}


def test_the_agent_prompt_and_its_resources_land_in_their_own_categories():
    """On the captured frame the custom agent's 2,000-character prompt is inside
    ``kiroInstructions`` and its 3,000-character resource file is a steering item with
    inclusion ``agentResource``, so neither is mistaken for Crew's composed prompt,
    which is the whole of ``history.user``."""
    breakdown = _update_of("turn_completion.detailed.json")["_meta"]["kiro"]["contextBreakdown"]
    categories = breakdown["categories"]
    assert categories["kiroInstructions"]["chars"] >= 2_000
    resources = [
        item for item in categories["steering"]["items"] if item.get("inclusion") == "agentResource"
    ]
    assert [item["chars"] for item in resources] == [3_000]
    parsed = kas_profile.parse_breakdown(breakdown)
    assert parsed is not None
    # The detailed items are not kept: only the per-category totals are.
    assert parsed["categories"]["steering"] == categories["steering"]["chars"]
    assert all(isinstance(v, int) for v in parsed["categories"].values())


def test_ttft_from_a_text_turn_and_a_tool_only_turn():
    text_ttft = _update_of("ttft_chunk.json")["_meta"]["kiro"]["ttft"]
    assert kas_profile.parse_ttft(text_ttft) == {
        "total_ms": 2_870,
        "setup_ms": 59,
        "model_ms": 1_534,
    }
    # A turn whose first output is reasoning after four tool calls still measures,
    # and the gap after the first model token holds the tools' time.
    thought = _update_of("ttft_chunk.thought_only_turn.json")
    assert thought["sessionUpdate"] == "agent_thought_chunk"
    parsed = kas_profile.parse_ttft(thought["_meta"]["kiro"]["ttft"])
    assert parsed is not None and parsed["total_ms"] > parsed["setup_ms"] + parsed["model_ms"]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"schema": "1", "categories": {}},
        {"schema": True, "categories": {}},
        {"schema": 1, "categories": []},
    ],
)
def test_an_unusable_breakdown_parses_to_none(raw):
    assert kas_profile.parse_breakdown(raw) is None


def test_bad_counts_lose_their_numbers_not_the_breakdown():
    parsed = kas_profile.parse_breakdown(
        {
            "schema": 1,
            "totalChars": 10**400,
            "modelCalls": -3,
            "window": {"sizeTokens": float("nan")},
            "categories": {
                "toolSpecs": {"chars": True},
                "steering": {"chars": 12.0},
                "x" * 65: {"chars": 1},
                "newCategory": {"chars": 7},
            },
        }
    )
    assert parsed == {
        "schema": 1,
        "categories": {"steering": 12, "newCategory": 7},
        "total_chars": kas_profile.MAX_COUNT,
    }


def test_a_ttft_whose_stamps_run_backwards_is_dropped_or_partial():
    assert kas_profile.parse_ttft({"promptReceivedAt": 10, "firstChunkEmittedAt": 5}) is None
    assert kas_profile.parse_ttft(
        {
            "promptReceivedAt": 1_000,
            "modelRequestSentAt": 2_000,
            "firstTokenReceivedAt": 1_500,
            "firstChunkEmittedAt": 3_000,
        }
    ) == {"total_ms": 2_000, "setup_ms": 1_000}


# --------------------------------------------------------------------------- #
# The handle: what reaches the per-turn stats
# --------------------------------------------------------------------------- #


def test_a_live_turn_records_breakdown_and_ttft_beside_its_credits():
    handle = _in_turn(_handle())
    _dispatch(handle, _update_of("ttft_chunk.json"))
    _dispatch(handle, _update_of("turn_completion.summary.json"))
    profile = handle.last_prompt_stats.kas_profile
    assert profile is not None
    assert profile["breakdown"]["total_chars"] == 59_333
    assert profile["ttft"]["total_ms"] == 2_870
    assert profile["turn_id"] == "ab047261-7694-4096-94c8-58dda42ea3dd"
    # The credit read the same frame feeds is unchanged.
    assert handle.last_prompt_stats.credits == pytest.approx(0.07441041260364842)


def test_a_chunk_still_renders_after_its_ttft_is_read():
    handle = _in_turn(_handle())
    events = _dispatch(handle, _update_of("ttft_chunk.json"))
    assert [e.text for e in events if e.text] == ["banana"]


def test_the_first_ttft_of_a_turn_wins():
    handle = _in_turn(_handle())
    _dispatch(handle, _update_of("ttft_chunk.json"))
    _dispatch(handle, _update_of("ttft_chunk.thought_only_turn.json"))
    assert handle.last_prompt_stats.kas_profile["ttft"]["total_ms"] == 2_870


def test_a_later_footer_of_the_same_prompt_replaces_the_breakdown():
    handle = _in_turn(_handle())
    first = _update_of("turn_completion.summary.json")
    second = json.loads(json.dumps(first))
    second["_meta"]["kiro"]["contextBreakdown"]["totalChars"] = 60_000
    second["_meta"]["kiro"]["turnId"] = "a-later-run"
    _dispatch(handle, first)
    _dispatch(handle, second)
    profile = handle.last_prompt_stats.kas_profile
    assert profile["breakdown"]["total_chars"] == 60_000
    assert profile["turn_id"] == "a-later-run"


def test_replayed_frames_on_load_are_not_recorded():
    """``session/load`` replays every recorded turn before any prompt: outside a turn
    nothing is kept, and a frame marked ``replay`` is refused even inside one."""
    idle = _handle()
    for name in ("replay.turn_completion.json", "replay.turn_timing.json", "ttft_chunk.json"):
        _dispatch(idle, _update_of(name))
    assert idle.last_prompt_stats.kas_profile is None
    busy = _in_turn(_handle())
    _dispatch(busy, _update_of("replay.turn_completion.json"))
    assert busy.last_prompt_stats.kas_profile is None


def test_a_sub_agents_frames_are_not_the_turns_own():
    handle = _in_turn(_handle())
    chunk = _update_of("ttft_chunk.json")
    chunk["_meta"]["kiro"]["agentSubtaskId"] = "sub-1"
    completion = _update_of("turn_completion.summary.json")
    completion["_meta"]["kiro"]["agentSubtaskId"] = "sub-1"
    _dispatch(handle, chunk)
    _dispatch(handle, completion)
    assert handle.last_prompt_stats.kas_profile is None


def test_a_prompt_without_the_opt_in_records_nothing():
    handle = _in_turn(_handle())
    _dispatch(handle, _update_of("turn_completion.no_meta.json"))
    assert handle.last_prompt_stats.kas_profile is None


def test_the_profile_does_not_survive_into_the_next_turn():
    handle = _in_turn(_handle())
    _dispatch(handle, _update_of("turn_completion.summary.json"))
    assert handle.last_prompt_stats.carry_over().kas_profile is None


def test_the_kiro_backend_never_reads_these_frames():
    handle = _in_turn(_handle(ACP_BACKEND_KIRO))
    _dispatch(handle, _update_of("turn_completion.summary.json"))
    _dispatch(handle, _update_of("ttft_chunk.json"))
    assert handle.last_prompt_stats.kas_profile is None


# --------------------------------------------------------------------------- #
# The opt-in, through a real prompt
# --------------------------------------------------------------------------- #


def _runtime(backend: str) -> tuple[AcpSessionHandle, AcpRuntime]:
    rt = AcpRuntime(work_dir="/tmp", acp_backend=backend)
    proc = MagicMock()
    proc.stdout = asyncio.StreamReader()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = _UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = _UNALLOCATABLE_PID
    rt._initialized = True
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues["sA"] = queue
    return AcpSessionHandle("sA", queue, rt), rt


async def _run_one_turn(backend: str) -> tuple[dict[str, Any], AcpSessionHandle, list]:
    """Send one prompt; the engine answers with a chunk, its footer, and the response."""
    handle, rt = _runtime(backend)
    sent: dict[str, Any] = {}

    async def _send(method: str, params: dict[str, Any]) -> int:
        sent.update(method=method, params=params)
        for name in ("ttft_chunk.json", "turn_completion.summary.json"):
            params = {**_frame(name)["params"], "sessionId": "sA"}
            handle._queue.put_nowait(JsonRpcMessage(method="session/update", params=params))
        handle._queue.put_nowait(JsonRpcMessage(id=7, result={"stopReason": "end_turn"}))
        return 7

    rt.send_request = AsyncMock(side_effect=_send)  # type: ignore[method-assign]
    events: list = []
    gen = handle.prompt("hello", timeout=5.0)
    try:
        while True:
            event = await finished(gen.__anext__(), "the next turn event")
            events.append(event)
            if event.kind == EVENT_COMPLETE:
                break
    finally:
        with contextlib.suppress(Exception):
            await gen.aclose()
    return sent, handle, events


@pytest.mark.asyncio
async def test_a_kas_prompt_asks_for_the_breakdown_and_ttft_and_keeps_them():
    sent, handle, events = await _run_one_turn(ACP_BACKEND_KAS)
    assert sent["method"] == "session/prompt"
    assert sent["params"]["_meta"] == {"kiro": {"contextBreakdown": "summary", "ttft": True}}
    assert any(e.kind == EVENT_COMPLETE for e in events)
    profile = handle.last_prompt_stats.kas_profile
    assert profile["breakdown"]["total_chars"] == 59_333
    assert profile["ttft"]["total_ms"] == 2_870


@pytest.mark.asyncio
async def test_a_non_kas_prompt_sends_no_meta():
    sent, handle, _events = await _run_one_turn(ACP_BACKEND_KIRO)
    assert "_meta" not in sent["params"]
    assert handle.last_prompt_stats.kas_profile is None


@pytest.mark.asyncio
async def test_the_sent_opt_in_is_a_copy_of_the_shared_constant():
    sent, _handle, _events = await _run_one_turn(ACP_BACKEND_KAS)
    sent["params"]["_meta"]["kiro"]["contextBreakdown"] = "detailed"
    assert kas_profile.PROMPT_META == {"kiro": {"contextBreakdown": "summary", "ttft": True}}
