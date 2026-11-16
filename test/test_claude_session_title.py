"""The title claude-agent-acp gives its own session, kept on the reader that reads it.

claude-agent-acp names every session with a standard ACP ``session_info_update``
``{title, updatedAt}`` (0.107.0 ``session-titles.js`` ``publish``). On a backend in
``ACP_BACKENDS_SESSION_TITLE`` ``acp.harness_title.note`` keeps the newest title of
the reader's own session, one line, redacted and bounded, and the frame still reaches
its caller; ``harness_title.session_title`` hands it to the dashboard. Frames go
through the real ``AcpClient._read_message`` and the shared runtime's own reader.
"""

from __future__ import annotations

import asyncio
import json
import logging
import types
from typing import Any
from unittest.mock import MagicMock

import pytest
from acp_frame_replay_harness import CORPUS, read_fixture
from test_claude_shared_runtime import harness  # noqa: F401  (fixture, by name)
from test_claude_shared_surfaces import _answer, _run, _turn

from kiro_crew.acp import harness_title
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.client_transport import _starts_agent_turn
from kiro_crew.acp.harness_title import MAX_HARNESS_TITLE_CHARS, HarnessTitle, session_title
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.transport_framing import _STDOUT_BUFFER_LIMIT
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SESSION_TITLE,
)
from kiro_crew.agent_sdk.drivers.acp_vocab import harness_session_title
from kiro_crew.testing.synthetic_credentials import SAMPLE_AWS_SECRET
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

SESSION = "claude-session-title"
#: A ``title`` key left out of the frame.
_ABSENT = object()


def _client(backend: str = ACP_BACKEND_CLAUDE, session: str = SESSION) -> AcpClient:
    client = AcpClient(acp_backend=backend)
    client._session_id = session
    return client


def _info(title: Any = "Postgres range scans", *, session: str = SESSION, **extra: Any):
    update: dict[str, Any] = {"sessionUpdate": "session_info_update", **extra}
    if title is not _ABSENT:
        update["title"] = title
    return JsonRpcMessage(method="session/update", params={"sessionId": session, "update": update})


def test_a_title_of_the_clients_own_session_is_kept():
    client = _client()
    harness_title.note(client, _info())
    assert session_title(client) == "Postgres range scans"
    # An ``AcpProvider`` is read through its client, as the dashboard reads it.
    assert harness_session_title(types.SimpleNamespace(client=client)) == "Postgres range scans"
    assert client._harness_title == HarnessTitle(SESSION, "Postgres range scans", 0)


@pytest.mark.parametrize("session", ["claude-session-other", "", "claude-child-1"])
def test_a_title_of_another_session_or_a_child_is_not_kept(session):
    client = _client()
    harness_title.note(client, _info(session=session))
    assert session_title(client) == ""
    assert getattr(client, "_harness_title", None) is None


@pytest.mark.parametrize(
    "frame",
    [
        _info(_ABSENT),
        _info(None),
        _info(42),
        _info(_ABSENT, _meta={"goal": {"text": "ship it"}}),
        _info(_ABSENT, _meta={"claudeCode": {"fileChanges": [{"path": "a.py"}]}}),
        _info("   ​ "),
        JsonRpcMessage(id=7, method="session/update", params=_info().params),
        JsonRpcMessage(
            method="session/update",
            params={"sessionId": SESSION, "update": {"sessionUpdate": "notice", "title": "x"}},
        ),
    ],
    ids=["absent", "null", "number", "air-goal", "file-changes", "blank", "request", "notice"],
)
def test_a_frame_without_a_string_title_changes_nothing(frame):
    client = _client()
    harness_title.note(client, _info("Kept title"))
    harness_title.note(client, frame)
    assert session_title(client) == "Kept title"


def test_controls_format_and_bidi_characters_become_spaces_and_whitespace_collapses():
    client = _client()
    harness_title.note(client, _info("\tFix\x07the‮bidi​bug now\n  please "))
    assert session_title(client) == "Fix the bidi bug now please"


def test_a_credential_or_exfil_url_is_redacted_before_it_is_kept():
    client = _client()
    harness_title.note(client, _info(f"Rotate {SAMPLE_AWS_SECRET} key"))
    kept = session_title(client)
    assert kept and SAMPLE_AWS_SECRET not in kept
    exfil = "https://evil.example.com/steal?data=" + "A" * 100
    harness_title.note(client, _info(f"See {exfil}"))
    kept = session_title(client)
    assert kept.startswith("See ") and exfil not in kept


def test_a_title_over_the_bound_is_dropped_counted_and_logged(caplog):
    client = _client()
    harness_title.note(client, _info("Kept title"))
    with caplog.at_level(logging.WARNING, logger=harness_title.__name__):
        harness_title.note(client, _info("x" * (MAX_HARNESS_TITLE_CHARS + 1)))
    assert session_title(client) == "Kept title"
    assert client._harness_title.dropped == 1
    assert "over-long session title (257 chars, bound 256, 1 dropped)" in caplog.text
    harness_title.note(client, _info("x" * MAX_HARNESS_TITLE_CHARS))
    assert session_title(client) == "x" * MAX_HARNESS_TITLE_CHARS
    assert client._harness_title.dropped == 1


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN - ACP_BACKENDS_SESSION_TITLE))
def test_only_a_backend_in_the_set_keeps_a_title(backend):
    client = _client(backend)
    harness_title.note(client, _info())
    assert getattr(client, "_harness_title", None) is None
    # Even a title somehow held is not offered on such a backend.
    client._harness_title = HarnessTitle(SESSION, "Held", 0)
    assert session_title(client) == ""


def test_the_set_is_claude_alone():
    assert ACP_BACKENDS_SESSION_TITLE == frozenset({ACP_BACKEND_CLAUDE})


def test_a_new_session_id_hides_the_previous_title():
    client = _client()
    harness_title.note(client, _info())
    client._session_id = "claude-session-next"
    assert session_title(client) == ""
    harness_title.note(client, _info("Next title", session="claude-session-next"))
    assert session_title(client) == "Next title"


@pytest.mark.parametrize("provider", [None, MagicMock(), types.SimpleNamespace()])
def test_a_provider_with_no_claude_reader_offers_nothing(provider):
    assert session_title(provider) == ""


def _line(msg: JsonRpcMessage) -> bytes:
    frame: dict[str, Any] = {"jsonrpc": "2.0"}
    for key in ("id", "method", "params", "result", "error"):
        if getattr(msg, key) is not None:
            frame[key] = getattr(msg, key)
    return (json.dumps(frame) + "\n").encode()


def _piped(client: AcpClient, lines: list[bytes]) -> None:
    reader = asyncio.StreamReader(limit=_STDOUT_BUFFER_LIMIT)
    for line in lines:
        reader.feed_data(line)
    proc = MagicMock()
    proc.stdout = reader
    proc.returncode = None
    client._process = proc


@pytest.mark.asyncio
async def test_the_read_chokepoint_notes_the_frame_and_still_returns_it(monkeypatch):
    client = _client()
    frame = _info("Read off the wire")
    _piped(client, [_line(frame)])
    noted: list[JsonRpcMessage] = []
    real_note = harness_title.note

    def counting(reader: Any, msg: JsonRpcMessage) -> None:
        noted.append(msg)
        real_note(reader, msg)

    monkeypatch.setattr(harness_title, "note", counting)
    read = await client._read_message(timeout=FALLBACK_TIMEOUT_SECS)
    assert read is not None and read.params == frame.params
    assert session_title(client) == "Read off the wire"
    # A frame a reader put back in ``_buffer`` is not noted a second time.
    client._buffer.append(read)
    again = await client._read_message(timeout=FALLBACK_TIMEOUT_SECS)
    assert again is read
    assert len(noted) == 1
    # A title frame reports state; it starts no agent turn between prompts.
    assert _starts_agent_turn(read, ACP_BACKEND_CLAUDE) is False


@pytest.mark.asyncio
async def test_the_live_capture_title_is_read():
    meta, frames = read_fixture(CORPUS / "claude" / "compact-live.jsonl")
    assert meta["recorded"] == "live" and meta["agent_version"] == "0.94.0"
    client = _client(session="claude-session-1")
    _piped(client, [(json.dumps(frame) + "\n").encode() for frame in frames])
    for _ in frames:
        assert await client._read_message(timeout=FALLBACK_TIMEOUT_SECS) is not None
    assert session_title(client) == "Amber and cobalt recall"


@pytest.mark.usefixtures("harness")
def test_the_shared_runtime_handle_keeps_its_own_title_and_the_provider_reads_it(tmp_path):
    own = {"sessionUpdate": "session_info_update", "title": "Chat A topic"}
    other = {"sessionUpdate": "session_info_update", "title": "Chat B topic"}

    async def scenario(rt, chat_a, chat_b, stdout):
        events = await _turn(rt, chat_a, stdout, _answer(("s-a", own), ("s-b", other)))
        return events, session_title(AcpSessionProvider(chat_a, rt)), chat_b

    _events, title_a, chat_b = _run(tmp_path, scenario)
    assert title_a == "Chat A topic"
    assert getattr(chat_b, "_harness_title", None) is None
