"""v1d integration -- SlackTransport.receive drives the REAL handle_message.

Proves the inbound transport adapter (ack -> drop bots -> owner-only
authorize -> normalize -> dispatch) is production-capable end-to-end against
the live ``slack/handler.py::handle_message``, without touching the gateway's
existing inbound wiring. The production inbound flip (routing events.py /
interactions.py through the transport) is a separate, swap-paired step.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.slack import handler as slack_handler
from kiro_crew.slack.transport import SlackTransport

# Import from sibling test file without triggering stdlib 'test' module collision.
_test_dir = Path(__file__).parent
if str(_test_dir) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(_test_dir))
_golden = importlib.import_module("test_slack_golden_transcript")

FakeSessions = _golden.FakeSessions
RecordingSlackClient = _golden.RecordingSlackClient
ScriptedProvider = _golden.ScriptedProvider
make_event = _golden.make_event

_OWNER = "U_OWNER"


def _build(monkeypatch):
    """Wire a SlackTransport whose dispatch drives the real handle_message."""
    monkeypatch.setattr(slack_handler, "_dashboard_state", None, raising=False)
    monkeypatch.setattr(slack_handler, "is_owner", lambda uid: True)
    monkeypatch.setattr(slack_handler, "is_allowed_user", lambda uid: True)
    monkeypatch.setattr(slack_handler, "_get_default_agent", lambda: "")
    monkeypatch.setattr(slack_handler, "_hydrate_thread_overrides", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(slack_handler, "_hydrate_conv_flags", lambda *a, **k: None, raising=False)

    slack = RecordingSlackClient()
    provider = ScriptedProvider([
        make_event(EVENT_TEXT_CHUNK, text="hi"),
        make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
    ])
    sessions = FakeSessions(provider)

    async def dispatch(msg: InboundMessage) -> None:
        await slack_handler.handle_message(
            slack=slack,
            sessions=sessions,
            channel=msg.conversation_id,
            text=msg.text,
            thread_ts=msg.thread_id,
            msg_ts=msg.thread_id or "1700000000.000100",
            user_id=msg.user_id,
            context_builder=None,
        )

    transport = SlackTransport(slack, allowed_users={_OWNER}, dispatch=dispatch)
    return transport, slack


class TestSlackTransportDrivesHandler:
    def test_owner_message_reaches_handle_message(self, monkeypatch):
        transport, slack = _build(monkeypatch)
        asyncio.run(transport.receive(
            {"event": {"user": _OWNER, "channel": "C1", "text": "hello", "ts": "1700000000.000100"}}
        ))
        methods = [m for m, _ in slack.transcript]
        # handle_message actually ran the turn: opened + finalized a stream.
        assert "start_stream" in methods and "stop_stream" in methods, methods

    def test_bot_message_is_dropped(self, monkeypatch):
        transport, slack = _build(monkeypatch)
        asyncio.run(transport.receive(
            {"event": {"bot_id": "B1", "channel": "C1", "text": "spam", "ts": "1700000000.000200"}}
        ))
        assert slack.transcript == []  # never dispatched

    def test_non_owner_is_denied(self, monkeypatch):
        transport, slack = _build(monkeypatch)
        asyncio.run(transport.receive(
            {"event": {"user": "U_OTHER", "channel": "C1", "text": "hello", "ts": "1700000000.000300"}}
        ))
        assert slack.transcript == []  # authorize denied -> never dispatched
