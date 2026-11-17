"""A Claude compaction's summary reaches the chat's compaction card.

claude-agent-acp reports a compaction to a client that declares ``session.compaction``
as a ``compaction_update`` lifecycle and reports the summary Claude Code kept on the
first ``completed`` update (``summary``), or streams it as ``compaction_summary_chunk``
updates and then leaves it off that update. ``kiro_crew.acp.compaction_summary`` keeps
the chunks on ``AcpClient`` and the ``completed`` compaction event carries the update's
summary, else the chunks, as its title, which the dashboard writes after
"Conversation compacted:" for the compaction card to fold.

``compaction-summary.jsonl`` is written from the adapter's source (its header cites the
lines): a manual ``/compact`` whose ``completed`` carries the summary, followed by the
status-only ``completed`` of its compact boundary, then a compaction inside a turn whose
summary streams as three chunks.

Section 1 pins the membership, section 2 replays the fixture through the real
``AcpClient._dispatch_events``, section 3 the cases around it, section 4 the gate,
section 5 real dashboard turns.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from acp_frame_replay_harness import CORPUS, read_fixture
from turn_harness import ScriptedProvider, TurnScript, run_turn

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.compaction_summary import (
    COMPACTION_SUMMARIES_MAX,
    COMPACTION_SUMMARY_MAX_CHARS,
    CompactionSummaries,
)
from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    KNOWN_SESSION_UPDATES,
    STOP_REASON_END_TURN,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_COMPACTION_SUMMARY,
    ACP_BACKENDS_INLINE_COMPACTION,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

FIXTURE = CORPUS / "claude" / "compaction-summary.jsonl"
SESSION = "claude-session-compact-summary"

MANUAL_SUMMARY = (
    "1. Primary Request and Intent:\n"
    "   The user asked the assistant to remember two colour words, amber and cobalt, and "
    "to recall them later.\n\n"
    "2. Key Technical Concepts:\n"
    "   - None; the conversation held no code.\n\n"
    "3. Current Work:\n"
    '   The assistant acknowledged both words with "ok" and is waiting for the user to ask '
    "for them."
)
STREAMED_SUMMARY = (
    "The user is building a recall test. They asked the assistant to remember amber and "
    "cobalt, and then to name both words when asked."
)


def _turns() -> list[list[dict[str, Any]]]:
    """The fixture's session updates, one list per prompt turn its responses end."""
    meta, frames = read_fixture(FIXTURE)
    assert meta["backend"] == ACP_BACKEND_CLAUDE
    turns: list[list[dict[str, Any]]] = [[]]
    for frame in frames:
        if "result" in frame:
            turns.append([])
        else:
            turns[-1].append(frame)
    assert turns[-1] == []
    return turns[:-1]


def _messages(frames: list[dict[str, Any]]) -> list[JsonRpcMessage]:
    return [JsonRpcMessage.from_dict(frame) for frame in frames]


def _client(backend: str = ACP_BACKEND_CLAUDE) -> AcpClient:
    client = AcpClient(acp_backend=backend)
    client._session_id = SESSION
    return client


async def _turn(client: AcpClient, messages: list[JsonRpcMessage]) -> list[AcpEvent]:
    """Run one ``_dispatch_events`` turn over *messages*, then the prompt's answer."""
    frames: list[tuple[str, JsonRpcMessage]] = [("update", msg) for msg in messages]
    frames.append(("complete", JsonRpcMessage(id=1, result={"stopReason": STOP_REASON_END_TURN})))

    async def _fake_loop(req_id, timeout):
        for frame in frames:
            yield frame

    client._prompt_loop = _fake_loop  # type: ignore[assignment]
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


def _status(compaction_id: str, status: str, **extra: Any) -> JsonRpcMessage:
    return _update(
        {"sessionUpdate": "compaction_update", "compactionId": compaction_id, "status": status}
        | extra
    )


def _chunk(compaction_id: str, text: str, *, session_id: str = SESSION) -> JsonRpcMessage:
    return _update(
        {
            "sessionUpdate": "compaction_summary_chunk",
            "compactionId": compaction_id,
            "content": {"type": "text", "text": text},
        },
        session_id=session_id,
    )


def _summary(text: str) -> dict[str, Any]:
    return {"summary": [{"type": "text", "text": text}]}


def _compactions(events: list[AcpEvent]) -> list[tuple[str, str]]:
    return [(e.text, e.title) for e in events if e.kind == EVENT_COMPACTION_STATUS]


def _deliver(client: AcpClient, *messages: JsonRpcMessage) -> list[AcpEvent]:
    """Each message through the compaction translation every prompt loop calls."""
    return [e for msg in messages if (e := client._codex_compaction_event(msg)) is not None]


# ── 1. The membership ──────────────────────────────────────────────────────────


def test_the_compaction_summary_set_names_claude_alone() -> None:
    assert ACP_BACKENDS_COMPACTION_SUMMARY == frozenset({ACP_BACKEND_CLAUDE})
    # The summary rides the inline-compaction translation, so a member must be in it.
    assert ACP_BACKENDS_COMPACTION_SUMMARY <= ACP_BACKENDS_INLINE_COMPACTION


def test_both_compaction_frames_are_known_session_updates() -> None:
    assert {"compaction_update", "compaction_summary_chunk"} <= KNOWN_SESSION_UPDATES


# ── 2. The recorded frames, through the real client ────────────────────────────


@pytest.mark.asyncio
async def test_a_manual_compact_carries_the_summary_its_completed_update_reports() -> None:
    manual, _automatic = _turns()
    events = await _turn(_client(), _messages(manual))
    # The compact boundary's status-only ``completed`` makes no second event.
    assert _compactions(events) == [("started", ""), ("completed", MANUAL_SUMMARY)]
    assert events[-1].kind == EVENT_COMPLETE


@pytest.mark.asyncio
async def test_a_compaction_whose_summary_streamed_carries_the_streamed_text() -> None:
    _manual, automatic = _turns()
    client = _client()
    events = await _turn(client, _messages(automatic))
    assert _compactions(events) == [("started", ""), ("completed", STREAMED_SUMMARY)]
    # The chunks are no reply text, and the reply after the compaction still is.
    assert [e.text for e in events if e.kind == EVENT_TEXT_CHUNK] == ["amber, cobalt"]
    assert len(client._compaction_summaries) == 0


@pytest.mark.asyncio
async def test_neither_compaction_frame_is_logged_as_unhandled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manual, automatic = _turns()
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.client"):
        await _turn(_client(), _messages(manual + automatic))
    assert "Unhandled session update type" not in caplog.text


# ── 3. The cases around it ─────────────────────────────────────────────────────


def test_the_completed_updates_own_summary_wins_over_what_streamed() -> None:
    """The chunks are the API's raw text and the summary is the cleaned hook text, so
    the adapter sends both only when they differ (``context-compaction.js`` 302-306)."""
    client = _client()
    events = _deliver(
        client,
        _status("c", "in_progress"),
        _chunk("c", "<summary>raw text</summary>"),
        _status("c", "completed", **_summary("clean text")),
    )
    assert _compactions(events) == [("started", ""), ("completed", "clean text")]
    assert len(client._compaction_summaries) == 0


def test_a_completed_update_with_no_summary_and_nothing_streamed_carries_none() -> None:
    events = _deliver(_client(), _status("c", "in_progress"), _status("c", "completed"))
    assert _compactions(events) == [("started", ""), ("completed", "")]


def test_a_summary_of_several_blocks_is_joined_and_empty_blocks_are_skipped() -> None:
    blocks = [
        {"type": "text", "text": "First part."},
        {"type": "text", "text": "   "},
        {"type": "image", "data": "AAAA", "mimeType": "image/png"},
        {"type": "text", "text": "Second part."},
    ]
    events = _deliver(_client(), _status("c", "completed", summary=blocks))
    assert _compactions(events) == [("completed", "First part.\n\nSecond part.")]


def test_a_chunk_of_another_session_or_of_no_open_compaction_is_not_kept() -> None:
    client = _client()
    _deliver(client, _chunk("closed", "never opened"))
    _deliver(client, _status("open", "in_progress"), _chunk("open", "theirs", session_id="other"))
    assert len(client._compaction_summaries) == 0
    events = _deliver(client, _chunk("open", "ours"), _status("open", "completed"))
    assert _compactions(events) == [("completed", "ours")]


@pytest.mark.parametrize("ending", ["cancelled", "failed"])
def test_a_compaction_that_ends_without_completing_drops_what_it_streamed(ending: str) -> None:
    client = _client()
    events = _deliver(
        client,
        _status("c", "in_progress"),
        _chunk("c", "half a summary"),
        _status("c", ending, reason="prompt_too_long"),
    )
    assert len(client._compaction_summaries) == 0
    # A failure still carries its reason, never the streamed text.
    expected = [("started", "")] + ([("failed", "prompt_too_long")] if ending == "failed" else [])
    assert _compactions(events) == expected


def test_the_summary_is_redacted() -> None:
    key = github_classic(seed=371)
    events = _deliver(
        _client(), _status("c", "completed", **_summary(f"The user pasted {key} by mistake."))
    )
    [(_status_type, title)] = _compactions(events)
    assert key not in title
    assert title == "The user pasted [REDACTED: credential] by mistake."


def test_a_streamed_credential_split_across_chunks_is_redacted_whole() -> None:
    key = github_classic(seed=372)
    client = _client()
    events = _deliver(
        client,
        _status("c", "in_progress"),
        _chunk("c", f"Key {key[:10]}"),
        _chunk("c", f"{key[10:]} seen."),
        _status("c", "completed"),
    )
    assert _compactions(events)[-1] == ("completed", "Key [REDACTED: credential] seen.")


def test_a_long_summary_is_cut_at_a_word_and_a_cut_credential_is_dropped() -> None:
    """The cut drops the word it would split, so a credential cut short, which the
    redaction cannot recognise, is not shown."""
    key = github_classic(seed=373)
    filler = "word " * (COMPACTION_SUMMARY_MAX_CHARS // 5 - 2)
    text = filler + key + " tail"
    assert len(filler) < COMPACTION_SUMMARY_MAX_CHARS < len(filler) + len(key)
    for streamed in (False, True):
        client = _client()
        if streamed:
            frames = [_status("c", "in_progress")]
            frames += [_chunk("c", text[i : i + 1000]) for i in range(0, len(text), 1000)]
            frames.append(_status("c", "completed"))
        else:
            frames = [_status("c", "completed", **_summary(text))]
        [*_, (_status_type, title)] = _compactions(_deliver(client, *frames))
        assert len(title) <= COMPACTION_SUMMARY_MAX_CHARS
        assert title == filler.strip()
        assert key[:8] not in title


def test_the_kept_text_is_bounded_per_compaction_and_in_compactions() -> None:
    summaries = CompactionSummaries()
    for _ in range(3):
        summaries.add("big", "x" * COMPACTION_SUMMARY_MAX_CHARS)
    assert len(summaries.take("big")) == COMPACTION_SUMMARY_MAX_CHARS + 1
    for index in range(COMPACTION_SUMMARIES_MAX + 2):
        summaries.add(f"c{index}", "text")
    assert len(summaries) == COMPACTION_SUMMARIES_MAX
    # The oldest are dropped first; the newest is kept.
    assert summaries.take("c0") == ""
    assert summaries.take(f"c{COMPACTION_SUMMARIES_MAX + 1}") == "text"


# ── 4. The gate ────────────────────────────────────────────────────────────────


def test_codex_reads_no_summary_on_the_same_frames() -> None:
    """codex is an inline-compaction harness outside the set: its lifecycle is read as
    before and no summary is kept or carried."""
    client = _client(ACP_BACKEND_CODEX)
    events = _deliver(
        client,
        _status("c", "in_progress"),
        _chunk("c", "streamed"),
        _status("c", "completed", **_summary("summary")),
    )
    assert _compactions(events) == [("started", ""), ("completed", "")]
    assert len(client._compaction_summaries) == 0


def test_kiro_reads_neither_frame() -> None:
    client = _client(ACP_BACKEND_KIRO)
    events = _deliver(
        client, _status("c", "in_progress"), _chunk("c", "x"), _status("c", "completed")
    )
    assert events == []
    assert len(client._compaction_summaries) == 0


# ── 5. Dashboard turns fold the summary into the compaction card ──────────────


class _ClaudeProvider(ScriptedProvider):
    """A scripted provider whose capabilities are claude's."""

    capabilities = capabilities_for(ACP_BACKEND_CLAUDE)


_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


async def _recorded_events(turn: int) -> list[AcpEvent]:
    events = await _turn(_client(), _messages(_turns()[turn]))
    return [e for e in events if e.kind != EVENT_COMPLETE]


def _cards(record: Any) -> list[Any]:
    return [
        row
        for row in record.rows("assistant")
        if (row.get("meta") or {}).get("kind") == "compaction"
    ]


def _compaction_rows(record: Any) -> list[str]:
    return [row["content"] for row in _cards(record)]


@pytest.mark.asyncio
async def test_a_turn_that_compacts_draws_the_streamed_summary_on_its_card() -> None:
    record = await run_turn(
        TurnScript(events=[*await _recorded_events(1), _DONE], provider=_ClaudeProvider)
    )
    assert _compaction_rows(record) == [f"✅ Conversation compacted: {STREAMED_SUMMARY}"]
    # The open chat gets the row as it lands, tagged for the card.
    sent = [
        f.payload["content"]
        for f in record.frames("chat_message")
        if (f.payload.get("meta") or {}).get("kind") == "compaction"
    ]
    assert sent == [f"✅ Conversation compacted: {STREAMED_SUMMARY}"]
    # The reply after the compaction is still the turn's answer.
    replies = [row["content"] for row in record.rows("assistant") if row not in _cards(record)]
    assert replies == ["amber, cobalt"]


@pytest.mark.asyncio
async def test_a_manual_compact_draws_the_summary_on_its_card() -> None:
    record = await run_turn(
        TurnScript(
            events=[*await _recorded_events(0), _DONE],
            provider=_ClaudeProvider,
            message="/compact",
        )
    )
    assert _compaction_rows(record) == [f"✅ Conversation compacted: {MANUAL_SUMMARY}"]
