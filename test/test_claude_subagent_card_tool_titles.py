"""A Crew-spawned subagent on the claude backend shows each call's refined title on its card.

claude-agent-acp names a call in its first ``tool_call`` before the call's input has
streamed ("Terminal", "Edit", "Read File", "Task") and sends the call's real title in a
``tool_call_update`` after it. The dashboard's own tool row takes that title; a run
spawned through ``spawn_run`` / ``POST /api/spawn`` draws its Subagents card from the
``subagent_tool`` frames ``subagent_manager/run_turn.py`` fires, and on a backend whose
capabilities say ``refines_tool_call_titles`` it fires one more, marked
``is_update``, that renames the call's row (``subagent_manager.card_tool_titles``).

Section 1 is the set and the capability, section 2 recorded claude frames replayed
through a real ``AcpProvider`` / ``AcpClient`` into the real run loop
(``SubagentManager._run_inner``), section 3 the other harnesses, kiro-cli's own capture
included, whose cards are unchanged, section 4 the tracker's bounds and redaction, and
section 5 the scale coalescer that merges the frames above its threshold.

The captures are ``acp_frames/claude/read-image.jsonl`` (an auto-allowed Read),
``acp_frames/claude/builtins-bash-edit-webfetch-live.jsonl`` (three gated calls),
``claude_native_subagents/agent-task-0.94.0.jsonl`` (an Agent call and its child) and
``acp_frames/kiro/tool-classes-b-live.jsonl`` (kiro-cli 3.1.0, whose closing
``tool_call_update`` titles a failed call with the bare tool name).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from acp_frame_replay_harness import CORPUS, read_fixture
from subagent_helpers import _mock_ctx_builder_auto_spawn, _mock_sessions

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import (
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    STOP_REASON_END_TURN,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_TOOL_TITLE_REFINEMENT,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.execution_context import execution_for_store
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.subagent import SubagentInfo, SubagentManager, _redact
from kiro_crew.subagent_manager.card_tool_titles import (
    MAX_CALL_ID_CHARS,
    MAX_LISTED_CALLS,
    TITLE_DIGEST_BYTES,
    CardToolTitles,
)
from kiro_crew.subagent_scale import SubagentEventCoalescer
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

# ``SubagentManager`` refuses a run while the host looks short of memory, which is the
# runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

READ_FIXTURE = CORPUS / "claude" / "read-image.jsonl"
GATED_FIXTURE = CORPUS / "claude" / "builtins-bash-edit-webfetch-live.jsonl"
KIRO_FIXTURE = CORPUS / "kiro" / "tool-classes-b-live.jsonl"
AGENT_CAPTURE = (
    Path(__file__).parent / "fixtures" / "claude_native_subagents" / "agent-task-0.94.0.jsonl"
)

READ_CALL = "toolu_read_image_1"
BASH_CALL = "toolu_bdrk_01MLg8fA8HbYbHwwzr1mTn7D"
EDIT_CALL = "toolu_bdrk_012AnrKZiua3A22gg5QLVYmr"
FETCH_CALL = "toolu_bdrk_01Nzp8UtsQ3GtMAeBC18ciLg"
AGENT_CALL = "toolu_0001"
CHILD_CALL = "toolu_0002"


def _frames(path: Path, backend: str) -> list[dict[str, Any]]:
    meta, frames = read_fixture(path)
    assert meta["backend"] == backend
    return frames


def _agent_frames() -> list[dict[str, Any]]:
    """The Agent capture's frames: a header line, then the turn's ``session/update``s."""
    lines = [json.loads(line) for line in AGENT_CAPTURE.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["_meta"]["backend"] == ACP_BACKEND_CLAUDE
    return [line for line in lines[1:] if "method" in line]


def _session_of(frames: list[dict[str, Any]]) -> str:
    """The parent session: the one that announces a child, else the first one named."""
    params = [frame.get("params") or {} for frame in frames]
    for p in params:
        if (p.get("update") or {}).get("sessionUpdate") == "subagent_update":
            return p["sessionId"]
    return next(p["sessionId"] for p in params if p.get("sessionId"))


def _client(frames: list[dict[str, Any]], backend: str) -> AcpClient:
    """A real client whose next prompt reads *frames*, then the prompt's answer.

    A later prompt on the same client (opencode's run resumes a turn that ended on
    a refused call) reads the answer alone."""
    client = AcpClient(acp_backend=backend)
    client._session_id = _session_of(frames)
    script: list[tuple[str, JsonRpcMessage]] = [
        (
            "permission" if frame.get("method") == "session/request_permission" else "update",
            JsonRpcMessage.from_dict(frame),
        )
        for frame in frames
    ]
    complete = ("complete", JsonRpcMessage(id=1, result={"stopReason": STOP_REASON_END_TURN}))
    script.append(complete)
    prompts = [0]

    async def _fake_loop(req_id, timeout):
        prompts[0] += 1
        for item in script if prompts[0] == 1 else [complete]:
            yield item

    client._prompt_loop = _fake_loop  # type: ignore[assignment]
    return client


def _provider(frames: list[dict[str, Any]], backend: str) -> AcpProvider:
    """A real ``AcpProvider`` on *backend* whose client answers the run's prompt with
    *frames*; a gated call's permission is answered without a process."""
    client = _client(frames, backend)
    client.ensure_ready = AsyncMock()  # type: ignore[method-assign]
    client._send_prompt = AsyncMock(return_value=1)  # type: ignore[method-assign]
    client.approve_tool = AsyncMock(return_value=True)  # type: ignore[method-assign]
    client.reject_tool = AsyncMock()  # type: ignore[method-assign]
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    provider._client = client
    provider._ready_confirmed_session = AsyncMock()  # type: ignore[method-assign]
    return provider


async def _run(
    frames: list[dict[str, Any]], backend: str = ACP_BACKEND_CLAUDE
) -> tuple[SubagentInfo, list[tuple[str, dict[str, Any]]]]:
    """Run one subagent turn over *frames*; the run and every event it fired."""
    sessions = _mock_sessions()
    sessions.get_or_create = AsyncMock(return_value=(_provider(frames, backend), True, False))
    ctx = _mock_ctx_builder_auto_spawn()
    ctx.hooks.auto_approve_subagent_tools = True
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
    fired: list[tuple[str, dict[str, Any]]] = []

    async def _on_event(kind: str, info: SubagentInfo, extra: dict | None = None) -> None:
        fired.append((kind, dict(extra or {})))

    info = SubagentInfo(
        execution_context=execution_for_store(""),
        id="cardtitles01",
        task="read the icon",
        parent_session_key="dashboard:chat-1",
    )
    manager._log_spawned(info)
    with (
        patch.object(manager, "_fire_event", side_effect=_on_event),
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
    ):
        await manager._run_inner(info, "subagent:cardtitles01")
    return info, fired


def _card(fired: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, str, bool]]:
    """The card's tool frames as ``(tool, tool_call_id, is_update)``."""
    return [
        (extra["tool"], extra["tool_call_id"], extra.get("is_update") is True)
        for kind, extra in fired
        if kind == "subagent_tool"
    ]


async def _events(frames: list[dict[str, Any]], backend: str) -> list[AcpEvent]:
    """What the real client's dispatch makes of *frames*."""
    client = _client(frames, backend)
    return [
        event async for event in client._dispatch_events(req_id=1, timeout=FALLBACK_TIMEOUT_SECS)
    ]


# ── 1. The set and the capability ───────────────────────────────────────────────


def test_the_refinement_set_names_claude_alone() -> None:
    assert ACP_BACKENDS_TOOL_TITLE_REFINEMENT == frozenset({ACP_BACKEND_CLAUDE})


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN | {"some-harness-nobody-registered"}))
def test_only_a_member_says_it_refines_tool_titles(backend: str) -> None:
    assert capabilities_for(backend).refines_tool_call_titles is (backend == ACP_BACKEND_CLAUDE)


# ── 2. Recorded claude frames through the real run loop ─────────────────────────


@pytest.mark.asyncio
async def test_an_auto_allowed_read_is_renamed_on_its_card_row() -> None:
    """The Read the adapter announces as "Read File" reads as the file it read."""
    info, fired = await _run(_frames(READ_FIXTURE, ACP_BACKEND_CLAUDE))
    refined = "Read /work/website/public/icon-192.png"
    assert _card(fired) == [("Read File", READ_CALL, False), (refined, READ_CALL, True)]
    [call, refinement] = [extra for kind, extra in fired if kind == "subagent_tool"]
    # The refinement lists no new call: the count the call frame moved stays.
    assert call["tool_count"] == refinement["tool_count"] == info.tool_count == 1
    assert "tool_kind" not in refinement
    # The reconnect snapshot replays the refined title for the run's newest call.
    assert (info.last_tool, info.last_tool_call_id) == (refined, READ_CALL)


@pytest.mark.asyncio
async def test_gated_calls_are_renamed_before_their_permission_frames() -> None:
    """Terminal, Edit and Fetch take the titles the dashboard's rows take, each on the
    row of its own call; the permission frame after them is unchanged."""
    info, fired = await _run(_frames(GATED_FIXTURE, ACP_BACKEND_CLAUDE))
    assert _card(fired) == [
        ("Terminal", BASH_CALL, False),
        ("touch made.txt", BASH_CALL, True),
        ("Create empty file made.txt", BASH_CALL, True),
        ("touch made.txt", BASH_CALL, False),
        ("Edit", EDIT_CALL, False),
        ("Edit note.txt", EDIT_CALL, True),
        ("Edit note.txt", EDIT_CALL, False),
        ("Fetch", FETCH_CALL, False),
        ("Fetch https://example.com", FETCH_CALL, True),
        ("Fetch https://example.com", FETCH_CALL, False),
    ]
    assert (info.tool_count, info.turns) == (3, 3)


@pytest.mark.asyncio
async def test_the_dashboard_row_and_the_card_read_the_same_title() -> None:
    """One derivation: the card's refined titles are the client's refinement titles."""
    frames = _frames(GATED_FIXTURE, ACP_BACKEND_CLAUDE)
    titled = [
        (e.title, e.tool_call_id)
        for e in await _events(frames, ACP_BACKEND_CLAUDE)
        if e.kind == EVENT_TOOL_CALL_UPDATE and e.title
    ]
    _info, fired = await _run(frames)
    assert [(t, c) for t, c, update in _card(fired) if update] == titled


@pytest.mark.asyncio
async def test_interleaved_calls_each_rename_their_own_row() -> None:
    """Parallel children's calls interleave: each refinement names its own call, and
    only the newest call's title becomes the run's last tool."""
    frames = _frames(GATED_FIXTURE, ACP_BACKEND_CLAUDE)

    def _first(kind: str, call_id: str) -> dict[str, Any]:
        return next(
            f
            for f in frames
            if (f.get("params") or {}).get("update", {}).get("sessionUpdate") == kind
            and f["params"]["update"]["toolCallId"] == call_id
        )

    # The Bash call, the Edit call, then each one's first refinement.
    interleaved = [
        _first("tool_call", BASH_CALL),
        _first("tool_call", EDIT_CALL),
        _first("tool_call_update", BASH_CALL),
        _first("tool_call_update", EDIT_CALL),
    ]
    info, fired = await _run(interleaved)
    assert _card(fired) == [
        ("Terminal", BASH_CALL, False),
        ("Edit", EDIT_CALL, False),
        ("touch made.txt", BASH_CALL, True),
        ("Edit note.txt", EDIT_CALL, True),
    ]
    assert (info.last_tool, info.last_tool_call_id) == ("Edit note.txt", EDIT_CALL)


@pytest.mark.asyncio
async def test_an_agent_call_names_its_childs_task_on_the_card() -> None:
    """The Agent call announced as "Task" reads as the task it handed its child, once
    however often the adapter repeats it; the child's own call is listed as the run's
    calls are, and the child's roster and text draw nothing on the card."""
    info, fired = await _run(_agent_frames())
    assert _card(fired) == [
        ("Task", AGENT_CALL, False),
        ("say pong", AGENT_CALL, True),
        ("SubagentHandback", CHILD_CALL, False),
    ]
    assert {kind for kind, _ in fired} == {"subagent_spawn", "subagent_tool", "subagent_chunk"}
    assert info.last_tool == "SubagentHandback"


# ── 3. The other harnesses' cards are unchanged ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_OPENCODE])
async def test_the_same_frames_on_another_harness_send_no_refinement(backend: str) -> None:
    info, fired = await _run(_frames(GATED_FIXTURE, ACP_BACKEND_CLAUDE), backend)
    assert _card(fired) == [
        ("Terminal", BASH_CALL, False),
        ("touch made.txt", BASH_CALL, False),
        ("Edit", EDIT_CALL, False),
        ("Edit note.txt", EDIT_CALL, False),
        ("Fetch", FETCH_CALL, False),
        ("Fetch https://example.com", FETCH_CALL, False),
    ]
    assert info.last_tool == "Fetch https://example.com"


@pytest.mark.asyncio
async def test_a_kiro_run_keeps_its_calls_titles_over_its_bare_name_updates() -> None:
    """kiro-cli ends each refused call with a ``tool_call_update`` titled by the bare
    tool name; the card keeps the titles its calls and permission requests named."""
    frames = _frames(KIRO_FIXTURE, ACP_BACKEND_KIRO)
    bare = [
        e.title
        for e in await _events(frames, ACP_BACKEND_KIRO)
        if e.kind == EVENT_TOOL_CALL_UPDATE and e.title
    ]
    assert {"use_aws", "web_fetch", "web_search", "subagent"} <= set(bare)
    info, fired = await _run(frames, ACP_BACKEND_KIRO)
    card = _card(fired)
    assert not any(update for _, _, update in card)
    assert [tool for tool, _, _ in card] == [
        "AWS: s3api create-bucket",
        "AWS: s3api create-bucket",
        "Fetching web content",
        "Fetching web content",
        "Searching the web",
        "Searching the web",
        "Spawning agent crew",
        "Spawning agent crew",
        "Goal complete",
    ]
    assert info.last_tool == "Goal complete"


# ── 4. The tracker's bounds and redaction ───────────────────────────────────────


def _member() -> SimpleNamespace:
    return SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_CLAUDE))


def _event(kind: str, call_id: str, title: str) -> AcpEvent:
    return AcpEvent(kind=kind, tool_call_id=call_id, title=title)


def _info() -> SimpleNamespace:
    return SimpleNamespace(turns=0, tool_count=1, last_tool="", last_tool_call_id="")


@pytest.mark.asyncio
async def test_a_refined_title_is_redacted_as_the_calls_own_title_is() -> None:
    titles = CardToolTitles(_member())
    manager = SimpleNamespace(_fire_event=AsyncMock())
    secret = 'curl -H "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789" x'
    titles.note_call(_event(EVENT_TOOL_CALL, "c1", "Terminal"))
    await titles.refine(manager, _info(), _event(EVENT_TOOL_CALL_UPDATE, "c1", secret))
    [call] = manager._fire_event.await_args_list
    frame = call.args[2]
    assert frame["tool"] == _redact(secret) != secret
    assert "[REDACTED" in frame["tool"]


@pytest.mark.asyncio
async def test_a_call_the_card_does_not_list_is_not_renamed() -> None:
    titles = CardToolTitles(_member())
    manager = SimpleNamespace(_fire_event=AsyncMock())
    long_id = "c" * (MAX_CALL_ID_CHARS + 1)
    titles.note_call(_event(EVENT_TOOL_CALL, long_id, "Terminal"))
    await titles.refine(manager, _info(), _event(EVENT_TOOL_CALL_UPDATE, long_id, "ls"))
    await titles.refine(manager, _info(), _event(EVENT_TOOL_CALL_UPDATE, "never-listed", "ls"))
    manager._fire_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_oldest_call_is_forgotten_past_the_bound() -> None:
    titles = CardToolTitles(_member())
    manager = SimpleNamespace(_fire_event=AsyncMock())
    for n in range(MAX_LISTED_CALLS + 1):
        titles.note_call(_event(EVENT_TOOL_CALL, f"c{n}", "Terminal"))
    await titles.refine(manager, _info(), _event(EVENT_TOOL_CALL_UPDATE, "c0", "ls"))
    manager._fire_event.assert_not_awaited()
    await titles.refine(manager, _info(), _event(EVENT_TOOL_CALL_UPDATE, "c1", "ls"))
    manager._fire_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_long_title_is_remembered_by_a_short_digest() -> None:
    """A shell call's title is its whole command: the tracker compares it without
    keeping it, and still tells a repeat from a new title."""
    titles = CardToolTitles(_member())
    manager = SimpleNamespace(_fire_event=AsyncMock())
    command = "printf '%s' " + "x" * 100_000
    titles.note_call(_event(EVENT_TOOL_CALL, "c1", "Terminal"))
    await titles.refine(manager, _info(), _event(EVENT_TOOL_CALL_UPDATE, "c1", command))
    await titles.refine(manager, _info(), _event(EVENT_TOOL_CALL_UPDATE, "c1", command))
    manager._fire_event.assert_awaited_once()
    held = [
        value
        for field in vars(titles).values()
        if isinstance(field, dict)
        for pair in field.items()
        for value in pair
    ]
    assert held and all(len(value) <= TITLE_DIGEST_BYTES for value in held)
    await titles.refine(manager, _info(), _event(EVENT_TOOL_CALL_UPDATE, "c1", command + "y"))
    assert manager._fire_event.await_count == 2


# ── 5. The scale coalescer ──────────────────────────────────────────────────────


def _coalesced(*frames: dict[str, Any]) -> list[tuple[str, str, bool]]:
    """The ``(tool, tool_call_id, is_update)`` of each batch entry the coalescer sends
    for *frames*, all above its threshold, in the order the card applies them."""
    sent: list[tuple[str, dict]] = []
    coalescer = SubagentEventCoalescer(
        lambda kind, data: sent.append((kind, data)),
        lambda kind, data: None,
        lambda: 99,
        threshold=1,
    )
    for frame in frames:
        assert coalescer.handle("subagent_tool", {"id": "a1", "slot": "s", **frame})
    coalescer.close()
    assert {kind for kind, _ in sent} == {"subagent_batch_update"}
    return [
        (entry["tool"], entry["tool_call_id"], entry.get("is_update") is True)
        for _, data in sent
        for entry in data["updates"]
    ]


def _call(call_id: str, tool: str, count: int) -> dict[str, Any]:
    return {"tool": tool, "tool_call_id": call_id, "tool_count": count}


def _refinement(call_id: str, tool: str, count: int) -> dict[str, Any]:
    return {**_call(call_id, tool, count), "is_update": True}


@pytest.mark.asyncio
async def test_a_refinement_of_the_buffered_call_goes_out_as_the_call() -> None:
    assert _coalesced(_call("c1", "Read File", 1), _refinement("c1", "Read /a.png", 1)) == [
        ("Read /a.png", "c1", False)
    ]


@pytest.mark.asyncio
async def test_a_newer_call_does_not_swallow_a_buffered_refinement() -> None:
    assert _coalesced(_refinement("c1", "Read /a.png", 1), _call("c2", "Terminal", 2)) == [
        ("Read /a.png", "c1", True),
        ("Terminal", "c2", False),
    ]


@pytest.mark.asyncio
async def test_refinements_of_two_calls_in_one_tick_each_reach_the_card() -> None:
    assert _coalesced(_refinement("c1", "Read /a.png", 2), _refinement("c2", "ls -la", 2)) == [
        ("Read /a.png", "c1", True),
        ("ls -la", "c2", True),
    ]


@pytest.mark.asyncio
async def test_a_later_refinement_of_the_same_call_wins() -> None:
    assert _coalesced(_refinement("c1", "Read /a", 1), _refinement("c1", "Read /a.png", 1)) == [
        ("Read /a.png", "c1", True)
    ]


@pytest.mark.asyncio
async def test_a_refinement_of_an_earlier_call_does_not_unlist_the_buffered_one() -> None:
    """The newer call reaches the card as a call, and the earlier one's rename after it."""
    assert _coalesced(_call("c2", "Terminal", 2), _refinement("c1", "Read /a.png", 2)) == [
        ("Terminal", "c2", False),
        ("Read /a.png", "c1", True),
    ]


@pytest.mark.asyncio
async def test_a_refined_buffered_call_survives_a_refinement_of_another_call() -> None:
    assert _coalesced(
        _call("y1", "Terminal", 2),
        _refinement("y1", "ls -la", 2),
        _refinement("x1", "Read /a.png", 2),
    ) == [("ls -la", "y1", False), ("Read /a.png", "x1", True)]
