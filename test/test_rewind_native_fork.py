"""A rewind on the claude backend forks the native conversation instead of replaying text.

The chain under test: the adapter's ``messageId`` rides each text chunk onto the
assistant transcript row; a rewind arms a fork at the retained prefix's last
assistant message; the next cold start forks the discarded session there and
restores the fork like any resume, falling back to a fresh session otherwise.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.acp._dispatch import parse_message_id, parse_session_update
from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.acp.types import EVENT_TEXT_CHUNK, PROVIDER_LABEL_CLAUDE
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_DEEPSEEK
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_rewind import _native_fork_point
from kiro_crew.session import SessionManager
from kiro_crew.session_map import SessionMap

# ── The message id reaches the transcript ────────────────────────────────────


def test_a_chunk_message_id_is_read_off_the_update() -> None:
    assert parse_message_id({"sessionUpdate": "agent_message_chunk", "messageId": "msg_1"}) == (
        "msg_1"
    )


@pytest.mark.parametrize("update", [None, {}, {"messageId": 7}, {"messageId": "m" * 257}])
def test_a_missing_or_malformed_message_id_reads_as_none(update: object) -> None:
    assert parse_message_id(update) == ""


def test_the_runtime_text_event_carries_the_message_id() -> None:
    events = parse_session_update(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "hello"},
            "messageId": "msg_1",
        }
    )

    assert [(e.kind, e.message_id) for e in events] == [(EVENT_TEXT_CHUNK, "msg_1")]


def test_the_segment_row_records_the_message_id_once() -> None:
    slot = SimpleNamespace(key="src", linked_session_key=None, segment_message_id="msg_1")

    assert chat_runner._segment_row_meta(slot, []) == {"native_message_id": "msg_1"}
    # Consumed: the next segment of the same turn must not inherit it.
    assert slot.segment_message_id == ""
    assert chat_runner._segment_row_meta(slot, []) is None


# ── The session map holds the pending fork ───────────────────────────────────


@pytest.fixture()
def session_map(tmp_path):
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield SessionMap()


def test_a_pending_fork_round_trips_through_disk(tmp_path) -> None:
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        SessionMap().set_native_fork("dash:1", "source-sid", "msg_1")
        assert SessionMap().get_native_fork("dash:1") == ("source-sid", "msg_1")


def test_writing_a_sid_drops_the_pending_fork(session_map) -> None:
    session_map.set_native_fork("dash:1", "source-sid", "msg_1")

    session_map.set("dash:1", "fresh-sid")

    assert session_map.get_native_fork("dash:1") is None


def test_clearing_the_sid_drops_the_pending_fork_even_with_no_sid(session_map) -> None:
    """A reset after the rewind but before its cold start must win over the fork."""
    session_map.set("dash:1", "source-sid")
    session_map.clear_sid("dash:1")
    session_map.set_native_fork("dash:1", "source-sid", "msg_1")

    assert session_map.clear_sid("dash:1") is True
    assert session_map.get_native_fork("dash:1") is None


def test_clear_native_fork(session_map) -> None:
    session_map.set_native_fork("dash:1", "source-sid", "msg_1")

    session_map.clear_native_fork("dash:1")

    assert session_map.get_native_fork("dash:1") is None


# ── The handshake forks, then restores the fork ──────────────────────────────


def _client(tmp_path: pathlib.Path, backend: str) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc
    return client


def _script(
    client: AcpClient, capabilities: dict, fork_answer: object, *, version: str = "0.87.0"
) -> list[tuple]:
    """Answer each handshake request by method; return the ``(method, params)`` sent."""
    sent: list[tuple] = []
    answers = {
        "initialize": {
            "protocolVersion": 1,
            "agentInfo": {"name": "claude-agent-acp", "version": version},
            "agentCapabilities": capabilities,
        },
        "session/fork": fork_answer,
        "session/load": {"modes": ["chat"]},
    }

    async def fake_send(method: str, params: dict) -> int:
        sent.append((method, params))
        return len(sent)

    async def fake_wait(req_id: int, timeout: float = 50.0, *, method="", expected_mcp=None):
        answer = answers.get(sent[req_id - 1][0], {"sessionId": "fresh"})
        if isinstance(answer, Exception):
            raise answer
        return answer

    client._send_request = AsyncMock(side_effect=fake_send)
    client._wait_for_response = AsyncMock(side_effect=fake_wait)
    client._drain_notifications = AsyncMock()
    client.set_fork_request("source-sid", "msg_1")
    return sent


_CLAUDE_FORKABLE = {"loadSession": True, "sessionCapabilities": {"fork": {}}}


@pytest.mark.asyncio
async def test_a_claude_fork_request_forks_then_loads_the_fork(tmp_path) -> None:
    client = _client(tmp_path, ACP_BACKEND_CLAUDE)
    sent = _script(client, _CLAUDE_FORKABLE, {"sessionId": "forked-sid"})

    await client._initialize_session()

    methods = [method for method, _ in sent]
    assert methods.index("session/fork") < methods.index("session/load")
    assert "session/new" not in methods
    fork_params = dict(sent)["session/fork"]
    assert fork_params["sessionId"] == "source-sid"
    assert fork_params["_meta"] == {
        "jetbrains": {"air": {"fork": {"version": 1, "messageId": "msg_1"}}}
    }
    assert dict(sent)["session/load"]["sessionId"] == "forked-sid"
    assert client._session_id == "forked-sid"
    assert client._resumed is True


@pytest.mark.asyncio
async def test_without_the_fork_capability_the_session_starts_fresh(tmp_path) -> None:
    client = _client(tmp_path, ACP_BACKEND_CLAUDE)
    sent = _script(client, {"loadSession": True}, {"sessionId": "forked-sid"})

    await client._initialize_session()

    methods = [method for method, _ in sent]
    assert "session/fork" not in methods
    assert "session/new" in methods
    assert client._resumed is False


@pytest.mark.asyncio
async def test_a_failed_fork_starts_fresh(tmp_path) -> None:
    """The adapter answers -32602 for a fork point it cannot find."""
    client = _client(tmp_path, ACP_BACKEND_CLAUDE)
    sent = _script(client, _CLAUDE_FORKABLE, AcpError("JSON-RPC error: fork point not found"))

    await client._initialize_session()

    methods = [method for method, _ in sent]
    assert "session/load" not in methods
    assert "session/new" in methods
    assert client._session_id == "fresh"
    assert client._resumed is False


@pytest.mark.asyncio
async def test_an_adapter_below_the_fork_point_floor_starts_fresh(tmp_path) -> None:
    """0.70.0 advertises fork but copies the whole session, edited-away suffix included."""
    client = _client(tmp_path, ACP_BACKEND_CLAUDE)
    sent = _script(client, _CLAUDE_FORKABLE, {"sessionId": "forked-sid"}, version="0.70.0")

    await client._initialize_session()

    assert "session/fork" not in [method for method, _ in sent]
    assert client._resumed is False


@pytest.mark.asyncio
async def test_only_claude_is_sent_the_fork_point(tmp_path) -> None:
    """Another harness may ignore the claude fork-point key and copy the whole session."""
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    sent = _script(client, {"sessionCapabilities": {"resume": {}, "fork": {}}}, {"sessionId": "x"})

    await client._initialize_session()

    assert "session/fork" not in [method for method, _ in sent]
    assert client._resumed is False


# ── The cold start hands the pending fork to the claude client ───────────────


def _provider_factory(backend: str):
    from kiro_crew.providers.acp import AcpProvider

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = object.__new__(AcpProvider)
        provider._client = MagicMock()
        provider._client._session_id = "forked-sid"
        provider._client._work_dir = "/workspace"
        provider._client._pid = None
        provider._client.backend = backend
        provider._client.resumed = True
        provider._history_replay_needed = False
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        factory.providers.append(provider)
        return provider

    factory.providers = []
    return factory


def _rewound_manager(factory) -> SessionManager:
    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    mgr._session_map.set("thread1", "source-sid", provider=PROVIDER_LABEL_CLAUDE)
    mgr._session_map.clear_sid("thread1")
    mgr._session_map.set_native_fork("thread1", "source-sid", "msg_1")
    return mgr


@pytest.mark.asyncio
async def test_a_cold_start_hands_the_fork_to_a_claude_client() -> None:
    factory = _provider_factory(ACP_BACKEND_CLAUDE)
    mgr = _rewound_manager(factory)

    await mgr.get_or_create("thread1")

    factory.providers[0]._client.set_fork_request.assert_called_once_with("source-sid", "msg_1")
    factory.providers[0]._client.set_resume_session_id.assert_not_called()
    assert mgr._session_map.get_native_fork("thread1") is None
    assert mgr._session_map.get("thread1") == "forked-sid"
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_restricted_session_drops_the_fork(monkeypatch) -> None:
    """A temporary or incognito session restores no native history, forked or loaded."""
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key, **_: SimpleNamespace(member_id=None, memory_mode="temporary"),
    )
    factory = _provider_factory(ACP_BACKEND_CLAUDE)
    mgr = _rewound_manager(factory)

    await mgr.get_or_create("thread1")

    factory.providers[0]._client.set_fork_request.assert_not_called()
    assert mgr._session_map.get_native_fork("thread1") is None
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_cold_start_on_another_backend_drops_the_fork() -> None:
    factory = _provider_factory(ACP_BACKEND_DEEPSEEK)
    mgr = _rewound_manager(factory)

    await mgr.get_or_create("thread1")

    factory.providers[0]._client.set_fork_request.assert_not_called()
    assert mgr._session_map.get_native_fork("thread1") is None
    await mgr.close_all()


# ── The rewind arms the fork at the prefix's last assistant message ──────────


def _row(role: str, **meta) -> dict:
    return {"role": role, "content": "x", **({"meta": meta} if meta else {})}


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ([_row("user"), _row("assistant", native_message_id="msg_1")], "msg_1"),
        (
            [
                _row("assistant", native_message_id="msg_1"),
                _row("assistant", kind="compaction", notice="session_recycled"),
                _row("assistant", kind="session_reload"),
                _row("system"),
                _row("done"),
            ],
            "msg_1",
        ),
        ([_row("assistant", native_message_id="msg_1"), _row("tool")], ""),
        ([_row("user")], ""),
        ([_row("assistant")], ""),
        ([], ""),
    ],
    ids=["last-assistant", "skips-notices", "tool-last", "user-last", "no-id", "empty"],
)
def test_the_fork_point_is_the_prefix_last_assistant_message(prefix, expected) -> None:
    assert _native_fork_point(prefix) == expected


def _slot_with_ids(state):
    slot = state.get_or_create_slot("src")
    slot.title = "My Chat"
    slot._titled = True
    slot.append("user", "first question", "msg msg-u", ts="2026-05-21T16:00:00Z")
    slot.append(
        "assistant",
        "first answer",
        "msg msg-a",
        ts="2026-05-21T16:00:01Z",
        meta={"native_message_id": "msg_1"},
    )
    # What _run_chat appends when a turn ends; it stays in the live window.
    slot.append("done", "", "done")
    slot.append("user", "second question", "msg msg-u", ts="2026-05-21T16:00:02Z")
    slot.drain()
    assert [m["role"] for m in slot.messages] == ["user", "assistant", "done", "user"]
    return slot


@pytest.mark.asyncio
@pytest.mark.parametrize(("at_index", "armed"), [(3, True), (0, False)])
async def test_a_rewind_arms_the_fork_only_after_an_assistant_message(
    tmp_path, monkeypatch, at_index, armed
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.chat_rewind._run_chat", AsyncMock(return_value=None))
    state = _make_state(tmp_path)
    slot = _slot_with_ids(state)
    state.sessions._session_map.get = MagicMock(return_value="source-sid")
    state.sessions._session_map.set_native_fork = MagicMock()

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/src/rewind",
            json={"at_message_index": at_index, "content": "edited"},
        )
        assert resp.status == 200

    if armed:
        state.sessions._session_map.set_native_fork.assert_called_once_with(
            "dashboard:src", "source-sid", "msg_1"
        )
    else:
        state.sessions._session_map.set_native_fork.assert_not_called()
    if slot.task:
        slot.task.cancel()
