from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp._dispatch import NOTICE_STATE_MAX, SessionNoticeState, parse_session_update
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKENDS_SESSION_NOTICES,
    ACP_CLIENT_CAPABILITIES,
    EVENT_COMPLETE,
    EVENT_NOTICE,
    EVENT_TEXT_CHUNK,
    JsonRpcMessage,
    acp_client_capabilities,
)
from kiro_crew.providers.acp import AcpProvider


def notice(sid="session-a", **fields):
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": sid,
            "update": {
                "sessionUpdate": "notice",
                "severity": "warning",
                "title": "Configuration warning",
                **fields,
            },
        },
    )


@pytest.mark.parametrize("severity", ["info", "warning", "error", "future-severity"])
def test_schema_and_provider_preserve_notice(severity):
    (event,) = parse_session_update(
        notice(severity=severity, description="Details").params["update"]
    )
    converted = AcpProvider._to_llm_event(event)
    assert (converted.kind, converted.title, converted.text, converted.notice_severity) == (
        EVENT_NOTICE,
        "Configuration warning",
        "Details",
        severity,
    )


@pytest.mark.parametrize(
    "update",
    [
        None,
        [],
        {"sessionUpdate": "future-event"},
        {"sessionUpdate": "sessionFailure", "title": "No quota", "severity": "error"},
        {"sessionUpdate": "notice", "title": "", "severity": "warning"},
        {"sessionUpdate": "notice", "title": {}, "severity": "warning"},
        {"sessionUpdate": "notice", "title": "Title", "severity": []},
    ],
)
def test_unknown_and_malformed_updates_are_ignored(update):
    assert parse_session_update(update) == []


def test_bounded_pending_dedup_and_session_isolation():
    state = SessionNoticeState()
    assert state.accept(notice("other"), "session-a") is None
    ownerless = notice()
    ownerless.params.pop("sessionId")
    assert state.accept(ownerless, "session-a") is None
    broadcast = notice()
    broadcast.fanout_no_owner = True
    assert state.accept(broadcast, "session-a") is None
    for i in range(NOTICE_STATE_MAX + 5):
        state.accept(notice(title=str(i)), "session-a", stage=True)
    assert len(state.seen) == len(state.pending) == NOTICE_STATE_MAX
    pending = state.take_pending("session-a")
    # The five dropped notices are reported once, ahead of the survivors.
    assert pending[0].title == "5 earlier provider notices were dropped"
    assert pending[0].notice_key == "pages.chat.noticeCard.provider_backlog"
    assert pending[0].notice_params == {"count": 5, "limit": NOTICE_STATE_MAX}
    assert pending[0].notice_severity == "warning"
    converted = AcpProvider._to_llm_event(pending[0])
    assert converted.notice_key == "pages.chat.noticeCard.provider_backlog"
    assert converted.notice_params == {"count": 5, "limit": NOTICE_STATE_MAX}
    assert pending[1].title == "5"
    assert len(pending) == NOTICE_STATE_MAX + 1
    state.accept(notice(title="after"), "session-a", stage=True)
    assert [e.title for e in state.take_pending("session-a")] == ["after"]
    assert state.accept(notice(title=str(NOTICE_STATE_MAX + 4)), "session-a") is None
    assert state.accept(notice(title=str(NOTICE_STATE_MAX + 4), severity="error"), "session-a")
    state.accept(notice(title="old"), "session-a", stage=True)
    assert state.take_pending("new-session") == []
    assert state.accept(notice("new-session", title="old"), "new-session")


def test_notice_fields_are_bounded_and_redacted(monkeypatch):
    from kiro_crew.acp import _dispatch

    monkeypatch.setattr(_dispatch, "redact_text", lambda text: text.replace("secret", "[redacted]"))
    (event,) = parse_session_update(
        notice(
            title="secret" + "x" * 400,
            description="secret" + "y" * 4090,
            severity="warning" + "z" * 100,
        ).params["update"]
    )
    assert event.title.startswith("[redacted]") and len(event.title) == 256
    assert event.text.startswith("[redacted]") and len(event.text) == 4096
    assert "\x1b" not in event.text and len(event.notice_severity) == 64



def test_backlog_singular_and_adapter_cannot_choose_catalog():
    state = SessionNoticeState()
    for i in range(NOTICE_STATE_MAX + 1):
        state.accept(notice(title=str(i)), "session-a", stage=True)
    warning = state.take_pending("session-a")[0]
    assert warning.title == "1 earlier provider notice was dropped"
    assert warning.notice_params == {"count": 1, "limit": NOTICE_STATE_MAX}
    update = notice().params["update"]
    update.update(notice_key=warning.notice_key, notice_params={"count": 99, "limit": 1})
    event = parse_session_update(update)[0]
    assert event.notice_key == "" and event.notice_params == {}


@pytest.mark.parametrize("field", ["title", "description", "severity"])
def test_oversized_notice_fields_are_refused_before_scanning(monkeypatch, field):
    from kiro_crew.acp import _dispatch

    class OversizedText(str):
        def __iter__(self):
            pytest.fail("oversized notice must not be normalized")

        def strip(self, *args, **kwargs):
            pytest.fail("oversized notice must not be stripped")

    payload = OversizedText("secret\x1b" * 300000)
    calls = []
    monkeypatch.setattr(_dispatch, "redact_text", lambda text: calls.append(text) or text)
    (event,) = parse_session_update(notice(**{field: payload}).params["update"])
    result = {"title": event.title, "description": event.text, "severity": event.notice_severity}
    assert result[field] == f"<id too long: {len(payload)} chars>"
    assert all(len(text) <= _dispatch._REQUEST_ID_REDACT_INPUT_CAP for text in calls)


def test_a_credential_split_by_a_control_character_is_still_redacted():
    """Normalization runs BEFORE redaction.

    The scan cannot see a key the provider split with an ESC, so stripping the
    control character afterwards would rejoin the halves into a whole credential
    it never matched -- and that intact key would reach the CLI's stderr and the
    dashboard's stored chat row.
    """
    (event,) = parse_session_update(
        notice(title="AKIA" + "\x1b" + "IOSFODNN7EXAMPLE").params["update"]
    )
    assert "IOSFODNN7EXAMPLE" not in event.title
    assert event.title == "[REDACTED: credential]"


def test_the_client_facade_keeps_resolving_the_base_capability_dict():
    """``kiro_crew.acp.client`` bound ``ACP_CLIENT_CAPABILITIES`` before the split.

    The handshake now asks ``acp_client_capabilities`` which set to send, so the
    base dict is not read here -- but it stays part of the facade's surface
    (frozen in ``test_campaign_facades_base_public_names``) and resolves to the
    object its home module holds.
    """
    from kiro_crew.acp import client as client_facade
    from kiro_crew.acp import types as acp_types

    assert client_facade.ACP_CLIENT_CAPABILITIES is acp_types.ACP_CLIENT_CAPABILITIES


@pytest.mark.parametrize("backend", ["", "claude", "codex"])
@pytest.mark.asyncio
async def test_direct_transport_notices_are_not_assistant_answers(tmp_path, monkeypatch, backend):
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    client._session_id = "session-a"
    client._session_notices.accept(notice(title="Startup"), "session-a", stage=True)
    frames = [
        notice(),
        notice(),
        notice("other"),
        JsonRpcMessage(
            method="session/update",
            params={
                "sessionId": "session-a",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Answer"},
                },
            },
        ),
    ]

    async def loop(req_id, timeout):
        for frame in frames:
            yield "update", frame
        yield "complete", JsonRpcMessage(id=7, result={"stopReason": "end_turn"})

    monkeypatch.setattr(client, "_prompt_loop", loop)
    monkeypatch.setattr(client, "_read_new_tool_results", AsyncMock(return_value=[]))
    events = [event async for event in client._dispatch_events(7, 1)]
    assert [e.kind for e in events] == [
        EVENT_NOTICE,
        EVENT_NOTICE,
        EVENT_TEXT_CHUNK,
        EVENT_COMPLETE,
    ]
    assert client.last_prompt_stats.text_chunks == 1
    assert "".join(e.text for e in events if e.kind == EVENT_TEXT_CHUNK) == "Answer"


@pytest.mark.parametrize("backend", ["", "codex", "kas"])
@pytest.mark.asyncio
async def test_shared_transport_init_dedup_and_tenant_isolation(backend):
    rt = MagicMock()
    rt.acp_backend = backend
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    rt.pid = None
    rt._last_activity = time.monotonic()
    queue = asyncio.Queue()
    handle = AcpSessionHandle("session-a", queue, rt)
    sibling = AcpSessionHandle("session-b", asyncio.Queue(), rt)
    handle._apply_init_notification(notice(), "update")
    assert sibling._handle_update(notice()) == []
    assert sibling._handle_update(notice("session-b"))[0].kind == EVENT_NOTICE
    queue.put_nowait(notice())
    queue.put_nowait(notice("session-b"))
    queue.put_nowait(
        JsonRpcMessage(
            method="session/update",
            params={
                "sessionId": "session-a",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Answer"},
                },
            },
        )
    )
    queue.put_nowait(JsonRpcMessage(id=7, result={"stopReason": "end_turn"}))
    events = [event async for event in handle._dispatch_events(7, 1)]
    assert [e.kind for e in events] == [EVENT_NOTICE, EVENT_TEXT_CHUNK, EVENT_COMPLETE]
    assert handle.last_prompt_stats.text_chunks == 1


def test_negotiation_is_independent_and_provider_compatible():
    assert "session" not in ACP_CLIENT_CAPABILITIES
    for backend in ("claude", "codex"):
        caps = acp_client_capabilities(backend)
        assert caps["session"] == {"notices": {}}
        assert "airClient" not in caps
    assert harness_for("codex").client_capabilities["session"] == {"notices": {}}
    # kiro-cli, KAS and every other backend keep the base handshake unchanged.
    for backend in ("", "kas"):
        assert "session" not in harness_for(backend).client_capabilities
    for backend in ("kiro", "kas", "opencode", "pi", "goose", "deepseek", None):
        assert acp_client_capabilities(backend) is ACP_CLIENT_CAPABILITIES
    assert ACP_BACKENDS_SESSION_NOTICES == {"claude", "codex"}
    (event,) = parse_session_update(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "Existing output"},
        }
    )
    assert event.kind == EVENT_TEXT_CHUNK and event.text == "Existing output"


@pytest.mark.asyncio
async def test_direct_init_drain_retains_notice_once(tmp_path):
    client = AcpClient(work_dir=tmp_path)
    client._session_id = "session-a"
    client._mcp_notifications.extend([notice(), notice(), notice("old-session")])
    await client._drain_notifications(duration=0)
    pending = client._session_notices.take_pending("session-a")
    assert len(pending) == 1 and pending[0].kind == EVENT_NOTICE
    assert client._session_notices.take_pending("session-a") == []


@pytest.mark.asyncio
async def test_shared_pre_turn_drain_preserves_notice():
    queue = asyncio.Queue()
    runtime = MagicMock()
    runtime.acp_backend = ""
    runtime.pid = None
    runtime.is_alive = MagicMock(return_value=True)
    runtime._last_activity = time.monotonic()
    runtime.send_notification = AsyncMock()

    async def request(method, params):
        queue.put_nowait(JsonRpcMessage(id=7, result={"stopReason": "end_turn"}))
        return 7

    runtime.send_request = request
    handle = AcpSessionHandle("sA", queue, runtime)
    handle._queue.put_nowait(notice("sA", title="Between turns"))
    events = [event async for event in handle.stream_command("/help", timeout=1)]
    assert events[0].kind == EVENT_NOTICE and events[0].title == "Between turns"
    assert events[-1].kind == EVENT_COMPLETE


@pytest.mark.parametrize("channel", ["discord", "slack"])
@pytest.mark.asyncio
async def test_channel_notice_is_separate_from_answer(channel):
    from kiro_crew.discord.renderer import DiscordRenderer
    from kiro_crew.messaging.renderer import NOTICE, OutputEvent
    from kiro_crew.messaging.transport import TransportCapabilities
    from kiro_crew.slack.renderer import SlackRenderer

    if channel == "discord":
        renderer = object.__new__(DiscordRenderer)
        renderer._client = MagicMock()
        renderer._client.send_message = AsyncMock()
        renderer._channel_id = "channel-a"
        send = renderer._client.send_message
        target = "channel-a"
    else:
        renderer = object.__new__(SlackRenderer)
        renderer.slack = MagicMock()
        renderer.slack.post_message = AsyncMock()
        renderer.channel = "channel-a"
        renderer.thread_ts = "thread-a"
        send = renderer.slack.post_message
        target = "channel-a"
    renderer.capabilities = TransportCapabilities()
    renderer._buf = ["Answer"]
    await renderer.dispatch(
        OutputEvent(kind=NOTICE, text="Deprecated option", notice_severity="warning")
    )
    assert send.await_args.args[:2] == (target, "⚠️ Deprecated option")
    assert renderer._buf == ["Answer"]


@pytest.mark.asyncio
async def test_muted_renderer_drops_notice():
    from kiro_crew.messaging.renderer import NOTICE, OutputEvent, SilentRenderer
    from kiro_crew.messaging.transport import TransportCapabilities

    for capabilities in (None, TransportCapabilities()):
        renderer = SilentRenderer(capabilities, "discord")
        await renderer.dispatch(OutputEvent(kind=NOTICE, text="Warning", notice_severity="warning"))


@pytest.mark.parametrize("chat_id", ["", "chat-a"])
@pytest.mark.asyncio
async def test_wecom_advisory_never_consumes_the_answer_reply_url(chat_id, caplog):
    from kiro_crew.messaging.renderer import NOTICE, OutputEvent
    from kiro_crew.messaging.transport import TransportCapabilities
    from kiro_crew.wecom.renderer import WeComRenderer

    renderer = object.__new__(WeComRenderer)
    renderer.capabilities = TransportCapabilities()
    renderer._chat_id = chat_id
    renderer._response_url = "answer-only"
    renderer._client = MagicMock()
    renderer._client.send_proactive = AsyncMock(return_value=False)
    renderer._client.send_reply = AsyncMock()
    await renderer.dispatch(OutputEvent(kind=NOTICE, text="Warning", notice_severity="warning"))
    renderer._client.send_reply.assert_not_awaited()
    assert "Provider notice delivery failed" in caplog.text
