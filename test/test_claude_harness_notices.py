"""Claude's own notices and mode changes reach the chat as notice rows.

claude-agent-acp sends a client declaring ``clientCapabilities.session.notices`` its
advisories as ``notice`` updates, and each permission mode the session moves to as a
``current_mode_update``. ``kiro_crew.acp.harness_notices`` reads an advisory as its
blockquote line on the text stream (so a text reader reads it in the reply) with the
notice attached, and a mode change as an ``EVENT_HARNESS_NOTICE``; the dashboard draws
both as ``notice`` rows.

``harness-notices.jsonl`` is written from the adapter's source (its header cites the
lines): a ``session/new`` result advertising the modes, two warnings (one with no
description), EnterPlanMode twice, a stopped background task, then an approved plan's
mode.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_frame_replay_harness import CORPUS, read_fixture
from turn_harness import ScriptedProvider, TurnScript, run_turn

from kiro_crew.acp import harness_notices
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.client_native_children import (
    CLAUDE_CLIENT_CAPABILITIES,
    client_capabilities_for,
)
from kiro_crew.acp.client_transport import _starts_agent_turn
from kiro_crew.acp.harness_notices import (
    NOTICE_KIND_ADAPTER,
    NOTICE_KIND_MODE,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    HarnessNotice,
    parse_notice,
    transcript_block,
)
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_HARNESS_NOTICE,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_HARNESS_NOTICES,
    ACP_BACKENDS_KNOWN,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import gateway_notice as gn
from kiro_crew.dashboard.chat_turn import session_events
from kiro_crew.dashboard.harness_notice_rows import notice_row
from kiro_crew.dashboard.state_parts.transcript_rows import is_turn_interrupted
from kiro_crew.dashboard.system_notices import HARNESS_ADVISORY_KIND
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

FIXTURE = CORPUS / "claude" / "harness-notices.jsonl"
SESSION = "claude-session-notices"

MCP_TRANSCRIPT = (
    '\n\n> **MCP server "probe" failed to start:** Its tools are unavailable in this '
    "session. Run /mcp for the error.\n\n"
)
PLAN_ROW = "ℹ️ This session switched to Plan mode."


def _fixture() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The fixture's ``session/new`` result and its session updates."""
    meta, frames = read_fixture(FIXTURE)
    assert meta["backend"] == ACP_BACKEND_CLAUDE
    session_new, *updates, _prompt_result = frames
    return session_new["result"], updates


def _messages(frames: list[dict[str, Any]]) -> list[JsonRpcMessage]:
    return [JsonRpcMessage.from_dict(frame) for frame in frames]


def _client(backend: str = ACP_BACKEND_CLAUDE, *, session_new: dict | None = None) -> AcpClient:
    client = AcpClient(acp_backend=backend)
    client._session_id = SESSION
    client._store_session_config(session_new if session_new is not None else _fixture()[0])
    return client


def _prompt_loop(messages: list[JsonRpcMessage]) -> Any:
    """A ``_prompt_loop`` that yields *messages* as updates, then the prompt's answer."""

    async def _fake_loop(req_id, timeout):
        for msg in messages:
            yield ("update", msg)
        yield ("complete", JsonRpcMessage(id=1, result={"stopReason": STOP_REASON_END_TURN}))

    return _fake_loop


async def _turn(client: AcpClient, messages: list[JsonRpcMessage]) -> list[AcpEvent]:
    """Run one ``_dispatch_events`` turn over *messages*, then the prompt's answer."""
    client._prompt_loop = _prompt_loop(messages)  # type: ignore[assignment]
    return [
        event async for event in client._dispatch_events(req_id=1, timeout=FALLBACK_TIMEOUT_SECS)
    ]


def _update(update: dict[str, Any], *, session_id: str = SESSION) -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        }
    )


def _notice(title: str, description: str = "", severity: str | None = "info") -> dict[str, Any]:
    """A ``notice`` update; a field given as "" or None is left out."""
    fields = {"severity": severity, "title": title, "description": description}
    return {"sessionUpdate": "notice", **{key: value for key, value in fields.items() if value}}


def _mode(mode_id: str, *, session_id: str = SESSION) -> JsonRpcMessage:
    update = {"sessionUpdate": "current_mode_update", "currentModeId": mode_id}
    return _update(update, session_id=session_id)


def _mode_option(current: str, *choices: tuple[str, str]) -> dict[str, Any]:
    return {
        "id": "mode",
        "type": "select",
        "currentValue": current,
        "options": [{"value": value, "name": name} for value, name in choices],
    }


def _notices(events: list[AcpEvent]) -> list[HarnessNotice]:
    return [e.harness_notice for e in events if e.harness_notice is not None]


def _mode_names(events: list[AcpEvent]) -> list[str]:
    return [e.harness_notice.mode_name for e in events if e.kind == EVENT_HARNESS_NOTICE]


# ── 1. The capability ──────────────────────────────────────────────────────────


def test_claude_alone_declares_session_notices() -> None:
    assert ACP_BACKENDS_HARNESS_NOTICES == frozenset({ACP_BACKEND_CLAUDE})
    assert CLAUDE_CLIENT_CAPABILITIES["session"] == {"compaction": {}, "notices": {}}
    params = _client()._initialize_params()
    assert params["clientCapabilities"]["session"]["notices"] == {}


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_only_a_member_of_the_set_declares_notices(backend: str) -> None:
    """The declaration and the reader are one membership: a harness told the client
    presents notices stops writing them as text, so a reader must be there."""
    declared = "notices" in (client_capabilities_for(backend).get("session") or {})
    assert declared == (backend in ACP_BACKENDS_HARNESS_NOTICES)


# ── 2. The recorded frames, through the real client ────────────────────────────


@pytest.mark.asyncio
async def test_the_recorded_frames_become_notices_in_order() -> None:
    events = await _turn(_client(), _messages(_fixture()[1]))
    notices = _notices(events)
    assert [(n.kind, n.severity) for n in notices] == [
        (NOTICE_KIND_ADAPTER, SEVERITY_WARNING),
        (NOTICE_KIND_ADAPTER, SEVERITY_WARNING),
        (NOTICE_KIND_MODE, SEVERITY_INFO),
        (NOTICE_KIND_ADAPTER, SEVERITY_INFO),
        (NOTICE_KIND_MODE, SEVERITY_INFO),
    ]
    assert [n.title for n in notices if n.kind == NOTICE_KIND_ADAPTER] == [
        'MCP server "probe" failed to start',
        "Stop says: the test suite is still failing",
        "Task stopped by user",
    ]
    # The repeated EnterPlanMode report makes no second notice.
    assert _mode_names(events) == ["Plan", "Manual"]
    assert events[-1].kind == EVENT_COMPLETE
    # Each adapter notice rides the text stream as the line a client without the
    # capability is sent (``session-notices.js`` ``noticeTranscriptText``), and one with
    # no description keeps its title alone.
    mcp, hook, _task = [e for e in events if e.kind == EVENT_TEXT_CHUNK]
    assert mcp.text == MCP_TRANSCRIPT and not mcp.control_notice
    assert mcp.harness_notice.description == (
        "Its tools are unavailable in this session. Run /mcp for the error."
    )
    assert hook.harness_notice.description == ""
    assert hook.harness_notice.transcript == (
        "\n\n> **Stop says: the test suite is still failing**\n\n"
    )


# ── 3. Around the recorded shapes ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("severity", "shown"),
    [
        ("warning", SEVERITY_WARNING),
        ("error", SEVERITY_WARNING),
        ("info", SEVERITY_INFO),
        ("debug", SEVERITY_INFO),
        (None, SEVERITY_INFO),
    ],
)
def test_the_severity_maps_onto_two_tones(severity: Any, shown: str) -> None:
    notice = parse_notice(_notice("Heads up", severity=severity))
    assert notice is not None and notice.severity == shown


def test_the_advertised_modes_kept_and_the_reported_mode_are_bounded(caplog) -> None:
    cap = harness_notices.MAX_SESSION_MODES
    modes = [{"id": f"m{i}", "name": "N" * 500} for i in range(cap + 2)]
    modes.append({"id": "x" * (harness_notices.MODE_ID_MAX + 1), "name": "Long"})
    with caplog.at_level("WARNING", logger=harness_notices.__name__):
        client = _client(session_new={"modes": {"currentModeId": "m0", "availableModes": modes}})
    names = client._harness_mode_names
    assert len(names) == cap
    assert all(len(name) <= harness_notices.MODE_NAME_MAX for name in names.values())
    assert any(r.getMessage().startswith("3 advertised mode(s)") for r in caplog.records)
    long_id = _mode("y" * (harness_notices.MODE_ID_MAX + 1))
    assert harness_notices.mode_notice_event(client, long_id) is None
    assert client._harness_mode is None


def test_a_notice_with_no_title_stands_on_its_description_or_is_none() -> None:
    notice = parse_notice(_notice("", "Only"))
    assert notice is not None and (notice.title, notice.description) == ("Only", "")
    assert parse_notice(_notice("  ")) is None
    assert parse_notice({"sessionUpdate": "plan", "entries": []}) is None


def test_the_transcript_quotes_every_line_like_the_adapter() -> None:
    assert transcript_block("Model fallback", "first\n\nsecond") == (
        "\n\n> **Model fallback:** first\n>\n> second\n\n"
    )


def test_notice_text_is_redacted_and_bounded() -> None:
    secret = github_classic(seed=352)
    notice = parse_notice(_notice(f"token {secret}", "x" * 5000, "warning"))
    assert notice is not None
    assert secret not in notice.title
    assert len(notice.description) == harness_notices.NOTICE_DESCRIPTION_MAX


@pytest.mark.asyncio
async def test_the_mode_name_falls_back_to_the_config_option_then_the_id() -> None:
    session_new = {
        "sessionId": SESSION,
        "configOptions": [_mode_option("default", ("default", "Manual"), ("plan", "Planning"))],
    }
    client = _client(session_new=session_new)
    events = await _turn(client, [_mode("plan"), _mode("dontAsk")])
    assert _mode_names(events) == ["Planning", "dontAsk"]


def _mode_select(options: list[dict[str, Any]]) -> dict[str, Any]:
    [select] = [option for option in options if option["id"] == "mode"]
    return select


def _adapter_on_the_pipes(client: AcpClient, answers: dict[str, list[dict]]) -> list[dict]:
    """Play claude-agent-acp on *client*'s stdio: every request written is recorded,
    and one whose method *answers* names gets those notifications and then ``{}``."""
    reader = asyncio.StreamReader()
    written: list[dict] = []

    def _write(data: bytes) -> None:
        frame = json.loads(data)
        written.append(frame)
        for notification in answers.get(frame.get("method", ""), []):
            reader.feed_data((json.dumps(notification) + "\n").encode())
        reply = {"jsonrpc": "2.0", "id": frame["id"], "result": {}}
        reader.feed_data((json.dumps(reply) + "\n").encode())

    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock(side_effect=_write)
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    client._process = proc
    return written


@pytest.mark.asyncio
async def test_the_mode_the_start_pin_left_makes_no_notice() -> None:
    """A first report meets the mode the start pin left.

    A session started in ``auto`` is pinned with ``session/set_mode`` ``default``, which
    the adapter answers with the ``mode`` option's ``config_option_update`` (the
    fixture's last one) and ``{}``, and no ``current_mode_update`` (``session-mode.js``
    ``setSessionMode``; ``v1/acp-agent.js`` ``updateConfigOption``). The start drains it
    before the first prompt, so a turn's report of ``default`` (the clear-context restart
    after an approved plan, ``exit-plan.js`` ``publishSessionState``) is no change.
    """
    session_new, updates = _fixture()
    [*_, pinned_option] = [
        u for u in updates if u["params"]["update"]["sessionUpdate"] == "config_option_update"
    ]
    assert _mode_select(pinned_option["params"]["update"]["configOptions"])["currentValue"] == (
        "default"
    )
    session_new["modes"]["currentModeId"] = "auto"
    _mode_select(session_new["configOptions"])["currentValue"] = "auto"
    client = _client(session_new=session_new)
    # A Side Chat session is always pinned, on an adapter that reported its version.
    client._side_read_only = True
    client._agent_version_read, client._agent_version = True, "0.107.0"
    written = _adapter_on_the_pipes(client, {"session/set_mode": [pinned_option]})

    await client._pin_claude_starting_mode(session_new)
    await client._drain_notifications(duration=0)

    assert [(f["method"], f["params"]) for f in written] == [
        ("session/set_mode", {"sessionId": SESSION, "modeId": "default"})
    ]
    assert harness_notices._configured_mode(client) == "default"
    assert _mode_names(await _turn(client, [_mode("default")])) == []
    assert _mode_names(await _turn(client, [_mode("plan")])) == ["Plan"]


@pytest.mark.asyncio
async def test_a_repeated_report_before_its_config_update_makes_one_notice() -> None:
    client = _client()
    assert _mode_names(await _turn(client, [_mode("plan"), _mode("plan")])) == ["Plan"]


@pytest.mark.asyncio
async def test_a_mode_moved_on_another_reader_is_the_new_base() -> None:
    """A config update read off the dashboard turn (a text-only reader) still moves the base."""
    client = _client()
    assert _mode_names(await _turn(client, [_mode("plan")])) == ["Plan"]
    client._handle_config_option_update(
        _update(
            {
                "sessionUpdate": "config_option_update",
                "configOptions": [_mode_option("acceptEdits", ("acceptEdits", "Accept edits"))],
            }
        )
    )
    assert _mode_names(await _turn(client, [_mode("acceptEdits")])) == []
    assert _mode_names(await _turn(client, [_mode("plan")])) == ["Plan"]


@pytest.mark.asyncio
async def test_a_new_session_forgets_the_last_report() -> None:
    client = _client()
    assert _mode_names(await _turn(client, [_mode("plan")])) == ["Plan"]
    client._store_session_config(_fixture()[0])
    assert _mode_names(await _turn(client, [_mode("default")])) == []


@pytest.mark.asyncio
async def test_a_frame_of_another_session_makes_no_notice() -> None:
    notice = _update(_notice("Not ours", severity="warning"), session_id="other")
    events = await _turn(_client(), [notice, _mode("plan", session_id="other")])
    assert _notices(events) == []
    assert not [e for e in events if e.kind == EVENT_TEXT_CHUNK]


def test_the_text_readers_read_a_notice_as_its_line() -> None:
    """The text-only readers (``send_message`` and its stream) share the extractor."""
    client = _client()
    msg = _update(_notice("Heads up"))
    text, thinking, notice = harness_notices.text_chunk(client, msg)
    assert (text, thinking) == ("\n\n> **Heads up**\n\n", False)
    assert notice is not None and notice.title == "Heads up"
    chunk = _update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
    )
    assert harness_notices.text_chunk(client, chunk) == ("hi", False, None)


# ── 4. The gate ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_OPENCODE])
async def test_a_harness_outside_the_set_reads_no_notice(backend: str) -> None:
    assert backend not in ACP_BACKENDS_HARNESS_NOTICES
    events = await _turn(_client(backend), _messages(_fixture()[1]))
    assert _notices(events) == []
    assert not [e for e in events if e.kind in (EVENT_TEXT_CHUNK, EVENT_HARNESS_NOTICE)]


# ── 5. The text-only readers, the idle reader and a native child ───────────────


def _scripted_prompt(client: AcpClient, messages: list[JsonRpcMessage]) -> None:
    """Answer the next prompt with *messages* and then its stop reason."""
    client.ensure_ready = AsyncMock()  # type: ignore[method-assign]
    client._send_prompt = AsyncMock(return_value=1)  # type: ignore[method-assign]
    client._prompt_loop = _prompt_loop(messages)  # type: ignore[assignment]


def _reply_then_notice() -> list[JsonRpcMessage]:
    """A reply chunk, the MCP startup notice and a mode change, as one turn sends them."""
    reply = _update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Done."}}
    )
    return [reply, *_messages(_fixture()[1][:1]), _mode("plan")]


@pytest.mark.asyncio
async def test_send_message_and_its_stream_read_the_notice_as_text() -> None:
    client = _client()
    _scripted_prompt(client, _reply_then_notice())
    assert await client.send_message("go") == "Done." + MCP_TRANSCRIPT
    streamed = _client()
    _scripted_prompt(streamed, _reply_then_notice())
    chunks = [chunk async for chunk in streamed.send_message_stream("go")]
    assert chunks == ["Done.", MCP_TRANSCRIPT]


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_a_notice_between_prompts_starts_a_turn_on_a_member_only(backend: str) -> None:
    """A member is sent a ``notice`` where it would be sent a chunk, so it starts the
    agent-started turn a chunk does; every backend's chunk still starts one."""
    [notice] = _messages(_fixture()[1][:1])
    chunk = _update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text"}})
    assert _starts_agent_turn(notice, backend) == (backend in ACP_BACKENDS_HARNESS_NOTICES)
    assert _starts_agent_turn(chunk, backend)
    assert not _starts_agent_turn(_mode("plan"), backend)


@pytest.mark.asyncio
async def test_a_native_childs_notice_is_its_cards_text() -> None:
    """A stopped background task a child started is routed to the child's session
    (``async-tasks.js`` ``routeOf``); its card reads the notice's text form."""
    child = "claude-child-1"
    announce = {
        "sessionUpdate": "subagent_update",
        "sessionId": child,
        "title": "general-purpose",
        "description": "Watch the build",
        "state": {"state": "running"},
    }
    stopped = _notice("Task stopped by user", "build watcher.")
    events = await _turn(_client(), [_update(announce), _update(stopped, session_id=child)])
    [activity] = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY and e.text]
    assert activity.sub_session_id == child
    assert activity.text == "\n\n> **Task stopped by user:** build watcher.\n\n"
    assert not [e for e in events if e.kind in (EVENT_TEXT_CHUNK, EVENT_HARNESS_NOTICE)]


def test_a_title_that_ends_a_sentence_takes_no_colon_in_either_form() -> None:
    """A two-line hook message's first line is its title (``session-notices.js``
    ``splitNoticeText``), so a title can end a sentence; the text form and the row then
    join the description to it by one rule."""
    notice = parse_notice(_notice("Stop says: the suite is red.", "Fix it first.", "warning"))
    assert notice is not None
    assert notice.transcript == "\n\n> **Stop says: the suite is red.** Fix it first.\n\n"
    assert notice_row(notice) == (
        "⚠️ Stop says: the suite is red. Fix it first.",
        {"kind": HARNESS_ADVISORY_KIND},
    )


# ── 6. Dashboard turns draw notice rows ────────────────────────────────────────


class _ClaudeProvider(ScriptedProvider):
    """A scripted provider whose capabilities are claude's."""

    capabilities = capabilities_for(ACP_BACKEND_CLAUDE)


_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


def _text(text: str) -> AcpEvent:
    return AcpEvent(kind=EVENT_TEXT_CHUNK, text=text)


async def _recorded_events() -> list[AcpEvent]:
    events = await _turn(_client(), _messages(_fixture()[1]))
    return [e for e in events if e.kind != EVENT_COMPLETE]


async def _plan_notice() -> AcpEvent:
    """The recorded turn's first mode change, to Plan."""
    return next(e for e in await _recorded_events() if e.kind == EVENT_HARNESS_NOTICE)


def _notice_rows(record: Any) -> list[tuple[str, dict]]:
    return [(row["content"], row.get("meta") or {}) for row in record.rows("notice")]


@pytest.mark.asyncio
async def test_a_dashboard_turn_draws_each_notice_as_a_notice_row() -> None:
    record = await run_turn(
        TurnScript(events=[*await _recorded_events(), _DONE], provider=_ClaudeProvider)
    )
    rows = _notice_rows(record)
    assert [content for content, _meta in rows] == [
        '⚠️ MCP server "probe" failed to start: Its tools are unavailable in this '
        "session. Run /mcp for the error.",
        "⚠️ Stop says: the test suite is still failing",
        PLAN_ROW,
        "ℹ️ Task stopped by user: build watcher.",
        "ℹ️ This session switched to Manual mode.",
    ]
    # An adapter's English carries no token; a mode change carries its own and the name.
    assert [meta.get(gn.GATEWAY_NOTICE_META_KEY) for _content, meta in rows] == [
        None,
        None,
        gn.GATEWAY_NOTICE_HARNESS_MODE_CHANGED,
        None,
        gn.GATEWAY_NOTICE_HARNESS_MODE_CHANGED,
    ]
    assert rows[2][1][gn.GATEWAY_NOTICE_ARGS_KEY] == {"mode": "Plan"}
    # Each row reaches the open chat as it lands, and none of it is reply text.
    sent = [f.payload for f in record.frames("chat_message") if f.payload.get("role") == "notice"]
    assert [p["content"] for p in sent] == [content for content, _meta in rows]
    assert not record.frames("chat_chunk")
    assert not record.rows("assistant")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hook", "row", "resent"),
    [
        (True, "⚠️ Stop says: the test suite is still failing", 0),
        (False, PLAN_ROW, 1),
    ],
    ids=["hook-notice", "mode-change"],
)
async def test_only_an_advisory_stands_in_for_an_empty_reply(
    hook: bool, row: str, resent: int
) -> None:
    """A hook's advisory answers the prompt, as its text form does for a text reader, so
    the empty-response ladder does not resend it; a mode report is not reply text."""
    messages = _messages(_fixture()[1][1:2]) if hook else [_mode("plan")]
    events = await _turn(_client(), messages)
    record = await run_turn(TurnScript(events=[*events[:-1], _DONE], provider=_ClaudeProvider))
    assert [c for c, _m in _notice_rows(record)] == [row]
    assert len(record.successors) == resent


@pytest.mark.asyncio
async def test_a_turn_an_advisory_answered_does_not_read_as_interrupted() -> None:
    """The transcript agrees with the ladder: an advisory answers the prompt, so the
    composer offers no Resume for it; a mode report does not answer it."""
    events = await _turn(_client(), _messages(_fixture()[1][1:2]))
    advisory = next(e.harness_notice for e in events if e.harness_notice is not None)
    mode = (await _plan_notice()).harness_notice

    def tail(notice: Any, *more: dict[str, Any]) -> list[dict[str, Any]]:
        content, meta = notice_row(notice)
        row = {"role": "notice", "content": content, "meta": meta}
        return [{"role": "user", "content": "go"}, row, *more]

    error = {"role": "error", "content": "boom"}
    assert is_turn_interrupted(tail(advisory)) is False
    assert is_turn_interrupted(tail(advisory, error)) is True
    # A held advisory the runner's teardown lands after the error row.
    landed_late = tail(advisory)
    landed_late.insert(1, error)
    assert is_turn_interrupted(landed_late) is True
    assert is_turn_interrupted(tail(mode)) is True


async def _row_order(*events: AcpEvent) -> list[tuple[str, str]]:
    """The assistant and notice rows a dashboard turn over *events* lands, in order."""
    record = await run_turn(TurnScript(events=[*events, _DONE], provider=_ClaudeProvider))
    assert not [row for row in record.window if row.get("role") == "chunk"]
    rows = record.history_rows
    return [(r["role"], r["content"]) for r in rows if r["role"] in ("assistant", "notice")]


@pytest.mark.asyncio
async def test_a_notice_inside_a_text_segment_lands_after_the_reply() -> None:
    """A row appended mid-segment would strand the segment's chunk rows above it."""
    order = await _row_order(_text("Planning "), await _plan_notice(), _text("now."))
    assert order == [("assistant", "Planning now."), ("notice", PLAN_ROW)]


@pytest.mark.asyncio
async def test_the_notices_held_inside_a_segment_are_bounded_and_the_rest_counted(
    caplog,
) -> None:
    plan = await _plan_notice()
    cap = session_events.MAX_HELD_HARNESS_NOTICES
    with caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_runner"):
        order = await _row_order(_text("Planning "), *[plan] * (cap + 3), _text("now."))
    assert order == [("assistant", "Planning now."), *[("notice", PLAN_ROW)] * cap]
    said = [r.getMessage() for r in caplog.records if "harness notice(s)" in r.getMessage()]
    assert len(said) == 1 and said[0].startswith("3 harness notice(s)")


@pytest.mark.asyncio
async def test_a_notice_between_segments_lands_where_it_came() -> None:
    tool = AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="call-1", title="EnterPlanMode")
    plan = await _plan_notice()
    order = await _row_order(_text("Let me plan."), tool, plan, _text("Here is the plan."))
    assert order == [
        ("assistant", "Let me plan."),
        ("notice", PLAN_ROW),
        ("assistant", "Here is the plan."),
    ]


def test_a_payload_the_row_builder_does_not_know_makes_no_row() -> None:
    assert notice_row(None) is None
    assert notice_row(HarnessNotice(kind="other", title="x")) is None
    assert notice_row(HarnessNotice(kind=NOTICE_KIND_MODE)) is None
