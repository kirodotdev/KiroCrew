"""Unit tests for the packaged fake ACP backend.

These run in the standard pytest suite (NOT gated behind ``KIROCREW_E2E``), so
they give coverage on ``kiro_crew.testing.fake_acp_backend`` without spawning a
gateway. ``test_e2e_smoke.py`` exercises the fake end-to-end through a real
gateway subprocess; this file locks the JSON-RPC frame shapes fast, in-process.
"""

from __future__ import annotations

import io
import json
from typing import Any

from kiro_crew.testing import fake_acp_backend as fake


def _capture(monkeypatch) -> io.StringIO:
    """Redirect the fake's stdout to a buffer for the duration of the test."""
    buf = io.StringIO()
    monkeypatch.setattr(fake.sys, "stdout", buf)
    return buf


def _messages(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_initialize_advertises_no_load_session(monkeypatch):
    buf = _capture(monkeypatch)
    fake._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    (msg,) = _messages(buf)
    assert msg["id"] == 1
    assert msg["result"]["protocolVersion"] == fake.PROTOCOL_VERSION
    assert msg["result"]["agentCapabilities"]["loadSession"] is False


def test_session_new_returns_session_id(monkeypatch):
    buf = _capture(monkeypatch)
    fake._handle({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}})
    (msg,) = _messages(buf)
    assert msg["result"]["sessionId"] == fake._SESSION_ID


def test_unknown_request_gets_empty_result(monkeypatch):
    buf = _capture(monkeypatch)
    fake._handle({"jsonrpc": "2.0", "id": 3, "method": "session/set_mode", "params": {}})
    (msg,) = _messages(buf)
    assert msg == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_response_and_notification_are_ignored(monkeypatch):
    buf = _capture(monkeypatch)
    # A response to one of our requests (has id, no method) -> ignored.
    fake._handle({"jsonrpc": "2.0", "id": fake._PERMISSION_REQ_ID, "result": {"outcome": {}}})
    # A notification (has method, no id) -> nothing to answer.
    fake._handle({"jsonrpc": "2.0", "method": "session/cancel", "params": {}})
    assert _messages(buf) == []


def test_plain_prompt_streams_reply_then_end_turn(monkeypatch):
    buf = _capture(monkeypatch)
    fake._handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "session/prompt",
            "params": {"sessionId": "s1", "prompt": [{"type": "text", "text": "hello"}]},
        }
    )
    msgs = _messages(buf)
    assert [m.get("method", "result") for m in msgs] == ["session/update", "result"]
    chunk = msgs[0]["params"]["update"]
    assert msgs[0]["params"]["sessionId"] == "s1"
    assert chunk["sessionUpdate"] == "agent_message_chunk"
    assert chunk["content"]["text"] == fake.REPLY_TEXT
    assert msgs[-1]["result"]["stopReason"] == "end_turn"


def test_tool_prompt_emits_tool_call_without_permission(monkeypatch):
    buf = _capture(monkeypatch)
    fake._handle(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "session/prompt",
            "params": {"prompt": [{"type": "text", "text": f"go {fake.TOOL_TRIGGER} now"}]},
        }
    )
    msgs = _messages(buf)
    updates = [
        m["params"]["update"]["sessionUpdate"] for m in msgs if m.get("method") == "session/update"
    ]
    assert updates == ["tool_call", "tool_call_update", "agent_message_chunk"]
    assert not any(m.get("method") == "session/request_permission" for m in msgs)


def test_permission_prompt_raises_request_permission(monkeypatch):
    buf = _capture(monkeypatch)
    fake._handle(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "session/prompt",
            "params": {"prompt": [{"type": "text", "text": fake.PERMISSION_TRIGGER}]},
        }
    )
    msgs = _messages(buf)
    perms = [m for m in msgs if m.get("method") == "session/request_permission"]
    assert len(perms) == 1
    assert perms[0]["id"] == fake._PERMISSION_REQ_ID
    assert perms[0]["params"]["toolCall"]["toolCallId"] == fake._TOOL_CALL_ID
    assert {o["optionId"] for o in perms[0]["params"]["options"]} == {"allow_once", "reject_once"}


def test_prompt_text_handles_missing_and_nontext_blocks():
    assert fake._prompt_text({}) == ""
    assert fake._prompt_text({"prompt": "not-a-list"}) == ""
    assert (
        fake._prompt_text(
            {
                "prompt": [
                    {"type": "image"},
                    {"type": "text", "text": "a"},
                    {"type": "text", "text": "b"},
                ]
            }
        )
        == "ab"
    )


def test_read_message_skips_blank_and_invalid_json(monkeypatch):
    monkeypatch.setattr(
        fake.sys, "stdin", io.StringIO('\n  \nnot json\n{"id": 1, "method": "x"}\n')
    )
    assert fake._read_message() == {"id": 1, "method": "x"}
    assert fake._read_message() is None  # EOF


def test_main_answers_readiness_probes(monkeypatch):
    buf = _capture(monkeypatch)
    monkeypatch.setattr(fake.sys, "argv", ["fake_acp_backend", "--version"])
    fake.main()
    assert buf.getvalue().strip() == fake.FAKE_VERSION

    buf.seek(0)
    buf.truncate()
    monkeypatch.setattr(fake.sys, "argv", ["fake_acp_backend", "whoami"])
    fake.main()
    assert buf.getvalue().strip() == fake.FAKE_IDENTITY


def test_main_processes_messages_until_eof(monkeypatch):
    buf = _capture(monkeypatch)
    monkeypatch.setattr(fake.sys, "argv", ["fake_acp_backend", "acp"])
    monkeypatch.setattr(
        fake.sys,
        "stdin",
        io.StringIO(
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
            '{"jsonrpc":"2.0","id":2,"method":"session/new","params":{}}\n'
        ),
    )
    fake.main()
    assert [m["id"] for m in _messages(buf)] == [1, 2]
