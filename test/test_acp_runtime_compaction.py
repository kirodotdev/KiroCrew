"""Compaction on ``AcpSessionHandle``.

The compaction and clear notifications, and the post-compaction drain: context
stats, failure summaries, metadata, and the frames it hands back.
"""

import asyncio

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import (
    _await_routed,
    _feed,
    _make_runtime,
    _register,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.testing.wait import await_until


@pytest.mark.asyncio
async def test_dispatch_compaction_and_clear():
    """Compaction and clear notifications yield appropriate events."""
    from kiro_crew.acp.types import (
        EVENT_CLEAR_STATUS,
        EVENT_COMPACTION_STATUS,
        METHOD_CLEAR_STATUS,
        METHOD_COMPACTION_STATUS,
    )

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_COMPACTION_STATUS,
                "params": {
                    "sessionId": "sA",
                    "status": {"type": "compacting"},
                    "summary": "50%",
                },
            },
        )
        _feed(reader, {"method": METHOD_CLEAR_STATUS, "params": {"sessionId": "sA"}})
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        comp = [e for e in events if e.kind == EVENT_COMPACTION_STATUS]
        clr = [e for e in events if e.kind == EVENT_CLEAR_STATUS]
        assert len(comp) == 1 and comp[0].text == "compacting"
        assert len(clr) == 1
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_compaction_completed_resets_context_stats():
    """A completed compaction in the prompt dispatch loop must drop the stale
    context-usage counts (regression: the meter froze at the pre-compaction
    value because context_tokens_from_usage=True blocked fresh metadata)."""
    from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, AcpPromptStats

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=75.0,
        context_used_tokens=150_000,
        context_window_tokens=200_000,
        context_tokens_from_usage=True,
    )
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ev in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_COMPACTION_STATUS,
                "params": {
                    "sessionId": "sA",
                    "status": {"type": "completed"},
                    "summary": "squeezed",
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        stats = handle.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_tokens_from_usage is False
        assert stats.context_window_tokens == 200_000  # model unchanged
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_wait_for_compaction_drain_path_resets_context_stats():
    """The async-after-end_turn drain path in wait_for_compaction bypasses the
    prompt dispatch loop, so it must drop the stale counts itself."""
    from kiro_crew.acp.types import (
        METHOD_COMPACTION_STATUS,
        AcpPromptStats,
        JsonRpcMessage,
    )

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=75.0,
        context_used_tokens=150_000,
        context_window_tokens=200_000,
        context_tokens_from_usage=True,
    )
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"sessionId": "sA", "status": {"type": "completed"}, "summary": "ok"},
        )
    )
    # Poison the queue behind the status so the post-compaction metadata grace
    # drain exits immediately instead of sleeping out its window.
    q["sA"].put_nowait(None)

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    stats = handle.last_prompt_stats
    assert stats.context_pct == 0.0
    assert stats.context_used_tokens == 0
    assert stats.context_tokens_from_usage is False
    assert stats.context_window_tokens == 200_000


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", ["", None])
async def test_wait_for_compaction_drain_failed_with_empty_summary_carries_the_reason(summary):
    """kiro-cli's ``summary`` is empty on failure, so the drain path carries the
    reason the payload names -- read by the same extractor the dispatch loop
    uses -- and a manual /compact names its cause the way auto-compaction does."""
    from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={
                "sessionId": "sA",
                "status": {"type": "failed", "error": "context window exceeded"},
                "summary": summary,
            },
        )
    )

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result == {"type": "failed", "summary": "context window exceeded"}


@pytest.mark.asyncio
async def test_wait_for_compaction_drain_failed_without_a_reason_reports_the_fallback():
    """No summary and no reason-bearing key: the drain result carries the
    extractor's own generic text, the same line the streaming notice shows."""
    from kiro_crew.acp.transport_errors import compaction_failure_detail
    from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    params = {"sessionId": "sA", "status": {"type": "failed"}, "summary": ""}
    q["sA"].put_nowait(JsonRpcMessage(method=METHOD_COMPACTION_STATUS, params=params))

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "failed"
    assert result["summary"] == compaction_failure_detail(params)
    assert result["summary"].startswith("no reason reported by the agent")


@pytest.mark.asyncio
async def test_wait_for_compaction_drain_failed_with_a_credential_shaped_summary_is_redacted():
    """A backend-echoed failure summary is LLM-influenced text, so the drain
    result carries it scrubbed -- the same ``redact_text`` the client wait path
    applies -- before the dashboard or a channel mirror shows it."""
    from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={
                "sessionId": "sA",
                "status": {"type": "failed"},
                "summary": "backend error: key AKIAIOSFODNN7EXAMPLE rejected",
            },
        )
    )

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "failed"
    assert "AKIAIOSFODNN7EXAMPLE" not in result["summary"]
    assert "[REDACTED: credential]" in result["summary"]


@pytest.mark.asyncio
async def test_wait_for_compaction_drain_applies_post_compaction_metadata():
    """kiro emits the real post-compaction pct ~1s after the completed status;
    the drain path must capture it and derive against the KEPT served window."""
    from kiro_crew.acp.types import (
        METHOD_COMPACTION_STATUS,
        AcpPromptStats,
        JsonRpcMessage,
    )

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=90.0,
        context_used_tokens=900_000,
        context_window_tokens=1_000_000,  # served window (differs from registry)
        context_tokens_from_usage=True,
    )
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"sessionId": "sA", "status": {"type": "completed"}, "summary": "ok"},
        )
    )
    q["sA"].put_nowait(
        JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"sessionId": "sA", "contextUsagePercentage": 5.0},
        )
    )

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    stats = handle.last_prompt_stats
    assert stats.context_pct == 5.0
    assert stats.context_window_tokens == 1_000_000
    assert stats.context_used_tokens == 50_000


@pytest.mark.asyncio
async def test_post_compaction_drain_requeues_frames_before_poison():
    """Death during the grace drain: buffered frames must be re-queued BEFORE
    the poison sentinel, or recovery would see death first and strand them."""
    from kiro_crew.acp.types import (
        METHOD_COMPACTION_STATUS,
        AcpPromptStats,
        JsonRpcMessage,
    )

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=75.0,
        context_used_tokens=150_000,
        context_window_tokens=200_000,
        context_tokens_from_usage=True,
    )
    stray = JsonRpcMessage(method="session/update", params={"sessionId": "sA", "update": {}})
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"sessionId": "sA", "status": {"type": "completed"}, "summary": "ok"},
        )
    )
    q["sA"].put_nowait(stray)
    q["sA"].put_nowait(None)

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    # Order restored: the stray frame first, the poison sentinel last.
    assert q["sA"].get_nowait() is stray
    assert q["sA"].get_nowait() is None


@pytest.mark.asyncio
async def test_outer_buffered_frame_restored_before_poison_from_nested_drain():
    """A frame buffered by wait_for_compaction ITSELF (before the completed
    status) must also be restored ahead of a poison consumed by the NESTED
    grace drain — separate buffers restored at different times would park the
    frame behind the death sentinel and its consumer would see AcpProcessDied
    despite a completed command."""
    from kiro_crew.acp.types import (
        METHOD_COMPACTION_STATUS,
        AcpPromptStats,
        JsonRpcMessage,
    )

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=75.0,
        context_used_tokens=150_000,
        context_window_tokens=200_000,
        context_tokens_from_usage=True,
    )
    stray = JsonRpcMessage(method="session/update", params={"sessionId": "sA", "update": {}})
    q["sA"].put_nowait(stray)  # buffered by the OUTER wait loop
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"sessionId": "sA", "status": {"type": "completed"}, "summary": "ok"},
        )
    )
    q["sA"].put_nowait(None)  # death consumed by the NESTED drain

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    assert q["sA"].get_nowait() is stray
    assert q["sA"].get_nowait() is None


async def _next_turn_credits(rt, reader, handle) -> float:
    """Run one real ``prompt()`` turn that reports no credits of its own and
    return the credits its ``EVENT_COMPLETE`` bills."""
    from kiro_crew.acp.types import EVENT_COMPLETE

    events: list = []

    async def drive():
        async for ev in handle.prompt("next", timeout=3.0):
            events.append(ev)

    driver = asyncio.ensure_future(drive())
    req_id = (await _await_routed(rt, "sA"))["sA"]
    _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
    await await_until(driver, what="the driver finishing its turn")
    complete = [e for e in events if e.kind == EVENT_COMPLETE]
    assert len(complete) == 1
    return complete[0].usage.credits


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape",
    [
        "metering_after_completed",
        "metering_before_completed",
        "metering_with_pct_after_completed",
        "manual_compact_cached_result",
    ],
)
async def test_between_turn_compaction_credits_are_billed_once_by_the_next_turn(shape):
    """Credits kiro reports while a compaction is waited on BETWEEN turns land
    on the next turn's bill, once. The stats window open at that point is wiped
    by the next prompt's re-init, and a frame handed back to the queue is
    discarded by the pre-turn drain, so neither path may hold them."""
    from kiro_crew.acp.types import (
        METHOD_COMPACTION_STATUS,
        AcpPromptStats,
        JsonRpcMessage,
    )

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=90.0,
        context_used_tokens=900_000,
        context_window_tokens=1_000_000,
        context_tokens_from_usage=True,
    )
    metering_params: dict = {
        "sessionId": "sA",
        "meteringUsage": [{"unit": "credit", "value": 0.5}],
    }
    if shape == "metering_with_pct_after_completed":
        metering_params["contextUsagePercentage"] = 5.0
    metering = JsonRpcMessage(method="_kiro.dev/metadata", params=metering_params)
    completed = JsonRpcMessage(
        method=METHOD_COMPACTION_STATUS,
        params={"sessionId": "sA", "status": {"type": "completed"}, "summary": "ok"},
    )
    pct = JsonRpcMessage(
        method="_kiro.dev/metadata",
        params={"sessionId": "sA", "contextUsagePercentage": 5.0},
    )
    if shape == "manual_compact_cached_result":
        # compact() captured the completed status inside its own turn.
        handle.last_prompt_stats.reset_after_compaction()
        handle._compact_result = {"type": "completed", "summary": "ok"}
        frames = [metering, pct]
    elif shape == "metering_before_completed":
        frames = [metering, completed, pct]
    elif shape == "metering_with_pct_after_completed":
        frames = [completed, metering]
    else:
        frames = [completed, metering, pct]
    for frame in frames:
        q["sA"].put_nowait(frame)

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    assert handle.last_prompt_stats.context_pct == 5.0
    # Nothing is left in the queue for the pre-turn drain to throw away.
    assert q["sA"].empty()
    task = await _start_reader(rt)
    try:
        assert await _next_turn_credits(rt, reader, handle) == pytest.approx(0.5)
        # Billed exactly once: the turn after that owes nothing.
        assert await _next_turn_credits(rt, reader, handle) == 0.0
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_wait_for_compaction_cached_result_applies_post_compaction_metadata():
    """The mid-turn cached path (compact() captured the completed status while
    draining its own prompt) must also grace-drain for the metadata."""
    from kiro_crew.acp.types import AcpPromptStats, JsonRpcMessage

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # The dispatch loop already reset the stats when it captured the result.
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=0.0,
        context_used_tokens=0,
        context_window_tokens=1_000_000,
        context_tokens_from_usage=False,
    )
    handle._compact_result = {"type": "completed", "summary": "ok"}
    q["sA"].put_nowait(
        JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"sessionId": "sA", "contextUsagePercentage": 5.0},
        )
    )

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    stats = handle.last_prompt_stats
    assert stats.context_pct == 5.0
    assert stats.context_used_tokens == 50_000


@pytest.mark.asyncio
async def test_wait_for_compaction_timeout_restores_a_concurrent_frame():
    """RECOVERY contract for a compaction that never reports terminal.

    ``wait_for_compaction`` returning ``{"type": "timeout"}`` means only that
    THIS reader did not observe a completed/failed status inside its window --
    NOT that the provider never sent one. A live turn can be draining the same
    session queue concurrently; the wait must therefore not SWALLOW the frames
    it pulls while looking for the status, or the next legal turn (or the live
    turn's own dispatch loop) is stranded -- the exact "stuck after a timed-out
    /compact" shape. This pins that a non-compaction frame observed during a
    wait that then times out is RESTORED to the queue, so the following reader
    still sees it. (It does not, and cannot, prove the historical incident was
    this race -- only that the reader does not drop a concurrent frame on the
    timeout path.)
    """
    from kiro_crew.acp.types import AcpPromptStats, JsonRpcMessage

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=50.0,
        context_used_tokens=100_000,
        context_window_tokens=200_000,
        context_tokens_from_usage=True,
    )
    # A frame belonging to a concurrent live turn, and NO compaction status
    # behind it: the status the provider may have sent was consumed elsewhere
    # (or never arrived within the window). The wait must time out AND hand the
    # live-turn frame back.
    live_frame = JsonRpcMessage(
        method="session/update", params={"sessionId": "sA", "update": {"live": True}}
    )
    q["sA"].put_nowait(live_frame)

    result = await handle.wait_for_compaction(timeout=0.3)

    assert result == {"type": "timeout"}
    # The concurrent frame is back on the queue for the next reader, not dropped.
    assert q["sA"].get_nowait() is live_frame
