"""Who draws Claude Code's raw SDK task messages, and what outlives a run's record.

``test_claude_raw_sdk_tasks.py`` pins what the messages make; this module pins, on the
same real ``AcpClient`` reader and dispatch, who draws them (only a dashboard chat turn,
whose ``chat_runner._run_chat`` marks its own stream), a landed turn's release, the
read's yield inside a buffered burst, a Workflow agent's card id past its run's record,
and a resumed run. The ``run_id`` frames follow the SDK's own
``SDKTaskStartedMessage.run_id`` (``@anthropic-ai/claude-agent-sdk`` ``sdk.d.ts``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from test_claude_raw_sdk_tasks import (
    _ANSWER,
    _BETWEEN,
    _agent_row,
    _background_row,
    _chunk,
    _client,
    _feed,
    _harness_running,
    _live_client,
    _next_turn_draws_between,
    _notified,
    _piped,
    _progress,
    _request,
    _rosters,
    _slot,
    _start_run,
    _started,
    _state,
    _turn,
    _turn_of,
    _updated,
)
from turn_harness import ScriptedProvider, TurnContext, TurnScript, run_turn

import kiro_crew.acp.client as acp_client
from kiro_crew.acp import raw_sdk_tasks
from kiro_crew.acp import raw_sdk_workflow_cards as cards
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.raw_sdk_tasks import END_COMPLETED
from kiro_crew.acp.session_handle_native_children import (
    NATIVE_CHILD_NOT_RESUMABLE,
    NATIVE_CHILD_ROSTER_CAP,
)
from kiro_crew.acp.types import STOP_REASON_CANCELLED, STOP_REASON_END_TURN, JsonRpcMessage
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.chat_runner import (
    NATIVE_SUBAGENT_INTERRUPTED_ERROR,
    _carried_background_native,
    _keep_background_open,
    _keep_background_open_at_release,
    _native_subagent_close_all,
    _native_subagent_sync,
    _retain_terminal_native,
)
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

# ── 1. Only the dashboard turn draws ──


def _claude_provider(client: AcpClient) -> AcpProvider:
    """A claude ``AcpProvider`` over *client*, its send gate a no-op."""
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=ACP_BACKEND_CLAUDE)
    provider._client = client
    provider._ready_confirmed_session = AsyncMock()  # type: ignore[method-assign]
    return provider


@pytest.mark.asyncio
async def test_a_compaction_stream_leaves_what_waits_for_the_next_dashboard_turn(
    tmp_path,
) -> None:
    """Automatic and idle compaction send ``/compact`` through ``provider.compact()``,
    whose stream keeps only the compaction status. Its dispatch is not the dashboard's,
    so it draws and settles nothing: what it read waits for the next dashboard turn."""
    client = _live_client(tmp_path)
    await _start_run(client)
    del client._prompt_loop  # the scripted loop; the compaction reads stdout itself
    _feed(_piped(client), [*_BETWEEN, _chunk("compacted"), _ANSWER])
    await _claude_provider(client).compact()
    assert len(client._raw_sdk.pending) == 3 and not client._raw_sdk.dashboard_turn
    await _next_turn_draws_between(client)


@pytest.mark.asyncio
async def test_the_dashboard_mark_is_taken_by_one_dispatch_and_survives_a_respawn() -> None:
    """The mark has the next dispatch draw, and only that one; a respawn between the mark
    and the dispatch forgets the tasks but not the mark."""
    client = _client()
    raw_sdk_tasks.expect_dashboard_turn(client)
    raw_sdk_tasks.forget_tasks(client)
    assert raw_sdk_tasks.begin_draw(client) is True
    assert raw_sdk_tasks.begin_draw(client) is False
    raw_sdk_tasks.expect_dashboard_turn(client)
    raw_sdk_tasks.expect_dashboard_turn(client, False)
    assert raw_sdk_tasks.begin_draw(client) is False


def _ended(task_id: str, command: str) -> list[JsonRpcMessage]:
    """A background command's launch and its end, as Claude Code reports them."""
    summary = f'Background command "{command}" completed (exit code 0)'
    started = _started(task_id, "local_bash", description=command, is_backgrounded=True)
    return [started, _notified(task_id, "completed", summary=summary)]


def _ended_row(command: str) -> str:
    return f"ℹ️ Background task completed: {command} — exit code 0"


def _dashboard_over(provider: AcpProvider) -> type[ScriptedProvider]:
    """The turn's provider port answered by *provider*, a real claude ``AcpProvider``;
    its stream records whether the mark was set when the turn opened it."""

    class _Turn(ScriptedProvider):
        capabilities = capabilities_for(ACP_BACKEND_CLAUDE)

        @property
        def client(self) -> AcpClient:
            return provider.client

        def stream(self, message: str, *, allow_image: bool = True) -> AsyncIterator[Any]:
            self.record("stream", provider.client._raw_sdk.dashboard_turn)
            _feed(_piped(provider.client), [_chunk("ok"), _ANSWER])
            return provider.stream(message, allow_image=allow_image)

    return _Turn


def _drawn(record: Any) -> list[tuple[str, str]]:
    """The turn's notice and reply rows, in transcript order."""
    rows = record.history_rows
    return [(r["role"], r["content"]) for r in rows if r["role"] in ("notice", "assistant")]


@pytest.mark.asyncio
async def test_a_dashboard_chat_turn_marks_its_own_stream_and_draws_what_waits(tmp_path) -> None:
    """``_run_chat`` marks the stream it opens (``_expect_background_draw``), so a task's
    end another reader read between prompts is drawn above the next turn's reply, and
    nothing but the turn sets the mark."""
    provider = _claude_provider(_live_client(tmp_path))
    client = provider.client
    waiting: list[tuple[int, bool]] = []

    async def between(_ctx: TurnContext) -> None:
        await _request(client, _piped(client), _ended("b1", "npm test"))
        waiting.append((len(client._raw_sdk.pending), client._raw_sdk.dashboard_turn))

    record = await run_turn(TurnScript(setup=between, provider=_dashboard_over(provider)))
    assert waiting == [(1, False)]
    assert _drawn(record) == [("notice", _ended_row("npm test")), ("assistant", "ok")]
    assert [call.args for call in record.calls("stream")] == [(True,)]
    assert not client._raw_sdk.pending and not client._raw_sdk.dashboard_turn


@pytest.mark.asyncio
async def test_a_turn_whose_stream_never_dispatched_takes_its_mark_back(tmp_path) -> None:
    """A dashboard turn whose stream fails before its dispatch (``ensure_ready``'s
    respawn raised) draws nothing, and its release takes back the mark. The compaction
    that follows is not handed it, so its task's end waits beside the first one for the
    next dashboard turn."""
    provider = _claude_provider(_live_client(tmp_path))
    client = provider.client
    seen: list[Any] = []

    async def between(_ctx: TurnContext) -> None:
        await _request(client, _piped(client), _ended("b1", "npm test"))
        client.ensure_ready = AsyncMock(side_effect=RuntimeError("spawn failed"))  # type: ignore[method-assign]

    async def compaction(_ctx: TurnContext) -> None:
        seen.append((len(client._raw_sdk.pending), client._raw_sdk.dashboard_turn))
        client.ensure_ready = AsyncMock()  # type: ignore[method-assign]
        _feed(_piped(client), [*_ended("b2", "npm run lint"), _chunk("compacted"), _ANSWER])
        await provider.compact()
        seen.append((len(client._raw_sdk.pending), client._raw_sdk.dashboard_turn))

    then = TurnScript(setup=compaction, provider=_dashboard_over(provider))
    record = await run_turn(
        TurnScript(setup=between, provider=_dashboard_over(provider), then=then)
    )
    assert [call.args for call in record.calls("stream")] == [(True,)]
    assert _drawn(record) == []
    assert seen == [(1, False), (2, False)]
    assert record.then is not None
    assert _drawn(record.then) == [
        ("notice", _ended_row("npm test")),
        ("notice", _ended_row("npm run lint")),
        ("assistant", "ok"),
    ]


# ── 2. A landed turn's release ──


@pytest.mark.parametrize(
    ("ending", "kept"),
    [("landed", True), ("session reset", False), ("cancelled after", False)],
)
def test_a_landed_turns_release_keeps_what_its_landing_kept_open(ending: str, kept: bool) -> None:
    """The landing keeps agent w1's card open while its run goes on. The idle reader can
    read the run's end before the release, which asks the client nothing: the card stays
    open for the next turn to close as the run ended. Only a session the release loses,
    or Crew cutting the turn short, closes it there."""
    state, slot, tracker = _state(), _slot(), {}
    _native_subagent_sync(state, slot, [_background_row("w1")], tracker)
    harness = _harness_running("w1")
    turn: Any = _turn_of(state, slot, tracker, harness)
    turn._saw_terminal_event = True
    turn._stop_reason = STOP_REASON_END_TURN
    turn.needs_session_reset = ending == "session reset"
    turn.needs_conversation_discard = False
    _keep_background_open(turn, STOP_REASON_END_TURN)
    _native_subagent_close_all(state, slot, tracker)
    assert not tracker["w1"]["done"]
    # The idle reader reads the run's end between the landing and the release.
    assert cards.finish(harness._raw_sdk.tasks["t1"].workflow, END_COMPLETED)
    if ending == "cancelled after":
        turn._stop_reason = STOP_REASON_CANCELLED
    _keep_background_open_at_release(turn)
    _native_subagent_close_all(state, slot, tracker, error=NATIVE_SUBAGENT_INTERRUPTED_ERROR)
    assert tracker["w1"]["done"] is not kept
    if not kept:
        assert tracker["w1"]["error"] == NATIVE_SUBAGENT_INTERRUPTED_ERROR
        return
    # The next turn carries the card and closes it as the run ended.
    next_turn = _carried_background_native(_retain_terminal_native(tracker))
    _native_subagent_sync(state, slot, [_background_row("w1", "completed")], next_turn)
    assert next_turn["w1"]["done"] and next_turn["w1"]["error"] is None


# ── 3. The take loop yields ──


@pytest.mark.asyncio
async def test_a_buffered_burst_of_task_frames_lets_another_task_run(monkeypatch) -> None:
    """Twenty buffered task frames are no suspension point. Each line holds the loop a
    hundredth of a (virtual) second, so the read yields once the read loops' budget
    passes, another task runs mid-burst, and the read still returns the session's own
    next frame."""
    clock = ManualClock()
    clock.install(monkeypatch, acp_client)
    client = _client()
    reader = _piped(client)
    _feed(reader, [*(_started(f"t{i}", "local_workflow") for i in range(20)), _chunk("hi")])
    buffered = reader.readline

    async def readline() -> bytes:
        clock.advance(0.01)
        return await buffered()

    reader.readline = readline  # type: ignore[method-assign]
    seen: list[int] = []

    async def bystander() -> None:
        seen.append(len(client._raw_sdk.tasks))

    other = asyncio.ensure_future(bystander())
    msg = await client._read_message(timeout=FALLBACK_TIMEOUT_SECS)
    assert msg is not None and msg.method == "session/update"
    assert other.done() and 0 < seen[0] < 20
    assert len(client._raw_sdk.tasks) == 20


# ── 4. A Workflow agent stays a native child ──


@pytest.mark.asyncio
async def test_a_workflow_agent_is_refused_after_its_record_goes() -> None:
    """The card ids seen are kept apart from the tasks: a run's record dropped for newer
    tasks, then the session's tasks forgotten, still leaves its agent's card refused,
    as the client's Agent-tool child ids are."""
    client = _client()
    newer: list[JsonRpcMessage] = []
    for i in range(raw_sdk_tasks.MAX_TRACKED_TASKS):
        newer.append(_started(f"f{i}", "local_bash", description=f"npm test {i}"))
        newer.append(_updated(f"f{i}", status="completed"))
    await _turn(
        client,
        [
            _started("t1", "local_workflow"),
            _progress("t1", [_agent_row(1, "start", promptPreview="p", agentId="a1")]),
            _notified("t1", "completed"),
            *newer,
        ],
    )
    assert "t1" not in client._raw_sdk.tasks
    raw_sdk_tasks.forget_tasks(client)
    for card in ("t1:a1", "native:t1:a1"):
        reason = client.native_child_resume_refusal(card)
        assert reason is not None and reason.startswith(f"{NATIVE_CHILD_NOT_RESUMABLE}: {card}")
    assert client.native_child_resume_refusal("t1:a2") is None


def test_the_card_ids_seen_keep_the_newest_under_the_child_id_cap() -> None:
    client = _client()
    per_run = cards.MAX_WORKFLOW_AGENTS
    runs = NATIVE_CHILD_ROSTER_CAP // per_run + 1
    for run in range(runs):
        rows = [_agent_row(i, "start", promptPreview="p") for i in range(per_run)]
        assert raw_sdk_tasks.take(client, _progress(f"r{run}", rows))
    seen = raw_sdk_tasks.workflow_card_ids(client)
    assert len(seen) == NATIVE_CHILD_ROSTER_CAP
    assert "r0:0" not in seen and f"r{runs - 1}:{per_run - 1}" in seen


# ── 5. A resumed run ──


def _of_run(frame: JsonRpcMessage, run_id: str) -> JsonRpcMessage:
    """*frame*, its message naming the run *run_id*."""
    message = {**frame.params["message"], "run_id": run_id}  # type: ignore[index]
    return JsonRpcMessage(method=frame.method, params={**frame.params, "message": message})


@pytest.mark.asyncio
async def test_a_resumed_run_replaces_the_ended_one_and_its_late_frames_are_dropped() -> None:
    """A resumed Workflow keeps its task id and opens a run whose id sorts after the
    earlier one's. Its start replaces the ended record, its agents get cards of their own
    even where an earlier agent had the same index or ``agentId``, the earlier run's end
    is still drawn, and a late frame of the earlier run (a beat, a kill, its end again)
    moves nothing."""
    client = _client()
    cached = _agent_row(1, "done", promptPreview="p", agentId="a1")
    queued = _agent_row(2, "start", promptPreview="q")
    late = [_agent_row(2, "error", promptPreview="q", error="late")]
    events = await _turn(
        client,
        [
            _of_run(_started("t1", "local_workflow"), "r1"),
            _of_run(_progress("t1", [cached, queued]), "r1"),
            _of_run(_notified("t1", "completed"), "r1"),
            _of_run(_started("t1", "local_workflow"), "r2"),
            _of_run(_progress("t1", [{**cached, "cached": True}, queued]), "r2"),
            _of_run(_progress("t1", late), "r1"),
            _of_run(_updated("t1", status="killed"), "r1"),
            _of_run(_notified("t1", "failed"), "r1"),
        ],
    )
    shown: dict[str, str] = {}
    for roster in _rosters(events):
        shown.update({row["sessionId"]: row["status"]["type"] for row in roster})
    assert shown == {
        "t1:a1": "completed",
        "t1:2": "completed",
        "t1:r2:a1": "completed",
        "t1:r2:2": "working",
    }
    record = client._raw_sdk.tasks["t1"]
    assert (record.run_id, record.ended, record.notified) == ("r2", "", False)
    assert raw_sdk_tasks.live_card_ids(client) == {"t1:r2:2"}
    assert raw_sdk_tasks.workflow_card_ids(client) == set(shown)
