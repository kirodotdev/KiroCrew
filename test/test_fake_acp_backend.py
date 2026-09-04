"""Unit tests for the packaged fake ACP backend.

These run in the standard pytest suite (NOT gated behind ``KIROCREW_E2E``), so
they give coverage on ``kiro_crew.testing.fake_acp_backend`` without spawning a
gateway. ``test_e2e_smoke.py`` exercises the fake end-to-end through a real
gateway subprocess; this file locks the JSON-RPC frame shapes fast, in-process.
"""

from __future__ import annotations

import base64
import io
import json
import queue
import time
from typing import Any

import pytest

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
    fake._handle(
        {"jsonrpc": "2.0", "id": 3, "method": "session/set_mode", "params": {}}
    )
    (msg,) = _messages(buf)
    assert msg == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_response_and_notification_are_ignored(monkeypatch):
    buf = _capture(monkeypatch)
    # A response to one of our requests (has id, no method) -> ignored.
    fake._handle(
        {"jsonrpc": "2.0", "id": fake._PERMISSION_REQ_ID, "result": {"outcome": {}}}
    )
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
            "params": {
                "sessionId": "s1",
                "prompt": [{"type": "text", "text": "hello"}],
            },
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
            "params": {
                "prompt": [{"type": "text", "text": f"go {fake.TOOL_TRIGGER} now"}]
            },
        }
    )
    msgs = _messages(buf)
    updates = [
        m["params"]["update"]["sessionUpdate"]
        for m in msgs
        if m.get("method") == "session/update"
    ]
    assert updates == ["tool_call", "tool_call_update", "agent_message_chunk"]
    assert not any(m.get("method") == "session/request_permission" for m in msgs)


def test_tool_call_carries_the_purpose_arg_the_host_reads(monkeypatch):
    """The fake's ``rawInput`` must carry the reserved purpose argument, and the
    host's extractor must find it — that pair is what the dashboard's concise
    tool pill renders instead of the literal command line. The camelCase
    spelling is deliberate: kiro-cli emits it that way on a large share of calls,
    and reading only the snake_case key silently dropped the label."""
    from kiro_crew.acp._dispatch import extract_tool_purpose

    buf = _capture(monkeypatch)
    fake._handle(_prompt(fake.TOOL_TRIGGER))
    msgs = _messages(buf)
    call = next(
        m["params"]["update"]
        for m in msgs
        if m.get("method") == "session/update"
        and m["params"]["update"]["sessionUpdate"] == "tool_call"
    )
    assert call["rawInput"]["__toolUsePurpose"] == fake._TOOL_PURPOSE
    assert extract_tool_purpose(call["rawInput"]) == fake._TOOL_PURPOSE


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
    assert {o["optionId"] for o in perms[0]["params"]["options"]} == {
        "allow_once",
        "reject_once",
    }


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


# --------------------------------------------------------------------------- #
# Negative paths: cancel, errors, alternate stop reasons, gated permission.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _drain_inbox():
    """The inbox is module state; keep it from leaking between tests."""
    while True:
        try:
            fake._INBOX.get_nowait()
        except queue.Empty:
            break
    yield
    while True:
        try:
            fake._INBOX.get_nowait()
        except queue.Empty:
            break


@pytest.fixture
def fast_slow_stream(monkeypatch):
    """Shrink the slow stream so the timing-based paths run instantly."""
    monkeypatch.setattr(fake, "SLOW_CHUNKS", 3)
    monkeypatch.setattr(fake, "SLOW_CHUNK_DELAY_SECS", 0)


def _prompt(text: str, req_id: int = 100, session_id: str = "s1") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "session/prompt",
        "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
    }


def test_error_trigger_replies_with_jsonrpc_error(monkeypatch):
    buf = _capture(monkeypatch)
    fake._handle(_prompt(fake.ERROR_TRIGGER))
    (msg,) = _messages(buf)
    assert msg["id"] == 100
    assert "result" not in msg
    assert msg["error"]["code"] == fake.ERROR_CODE
    assert msg["error"]["message"] == fake.ERROR_MESSAGE


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [("MAX_TOKENS_TRIGGER", "max_tokens"), ("REFUSAL_TRIGGER", "refusal")],
)
def test_alternate_stop_reasons(monkeypatch, trigger, expected):
    buf = _capture(monkeypatch)
    fake._handle(_prompt(getattr(fake, trigger)))
    msgs = _messages(buf)
    # The canned reply still streams; only the terminal stopReason differs.
    assert msgs[0]["params"]["update"]["content"]["text"] == fake.REPLY_TEXT
    assert msgs[-1]["result"]["stopReason"] == expected


def test_slow_prompt_streams_many_chunks_then_end_turn(monkeypatch, fast_slow_stream):
    buf = _capture(monkeypatch)
    fake._handle(_prompt(fake.SLOW_TRIGGER))
    msgs = _messages(buf)
    chunks = [m for m in msgs if m.get("method") == "session/update"]
    assert len(chunks) == 3
    assert chunks[0]["params"]["update"]["content"]["text"].startswith(
        fake.SLOW_CHUNK_TEXT
    )
    assert msgs[-1]["result"]["stopReason"] == "end_turn"


def test_slow_prompt_honours_cancel_with_cancelled_stop_reason(
    monkeypatch, fast_slow_stream
):
    buf = _capture(monkeypatch)
    fake._INBOX.put(
        {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "s1"}}
    )
    fake._handle(_prompt(fake.SLOW_TRIGGER))
    msgs = _messages(buf)
    assert msgs[-1]["result"]["stopReason"] == "cancelled"


def test_cancel_for_a_different_session_is_not_honoured(monkeypatch, fast_slow_stream):
    buf = _capture(monkeypatch)
    fake._INBOX.put(
        {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "other"}}
    )
    fake._handle(_prompt(fake.SLOW_TRIGGER))
    assert _messages(buf)[-1]["result"]["stopReason"] == "end_turn"


def test_slow_noack_ignores_cancel(monkeypatch, fast_slow_stream):
    """The soft-stop-budget-expiry path: a queued cancel must NOT be acked."""
    buf = _capture(monkeypatch)
    fake._INBOX.put(
        {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "s1"}}
    )
    fake._handle(_prompt(fake.SLOW_NOACK_TRIGGER))
    assert _messages(buf)[-1]["result"]["stopReason"] == "end_turn"


def test_slow_lateack_winds_down_then_acks(monkeypatch):
    """LATEACK keeps streaming for SLOW_LATEACK_CHUNKS, then acks.

    Asserted in CHUNKS, not seconds: the point of the wind-down is that the
    host's `soft_pending` state lasts long enough to be observed, and chunk
    count is the deterministic proxy for that duration. Contrast the SLOW case
    below, which emits nothing before acking.
    """
    monkeypatch.setattr(fake, "SLOW_CHUNKS", 10)
    monkeypatch.setattr(fake, "SLOW_CHUNK_DELAY_SECS", 0)
    monkeypatch.setattr(fake, "SLOW_LATEACK_CHUNKS", 4)
    buf = _capture(monkeypatch)
    fake._INBOX.put(
        {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "s1"}}
    )
    fake._handle(_prompt(fake.SLOW_LATEACK_TRIGGER))
    msgs = _messages(buf)
    chunks = [m for m in msgs if m.get("method") == "session/update"]
    # Four wind-down chunks, then the cooperative ack -- NOT the full 10.
    assert len(chunks) == 4
    assert msgs[-1]["result"]["stopReason"] == "cancelled"


def test_slow_acks_immediately_unlike_lateack(monkeypatch):
    """Pins the contrast: plain SLOW emits zero chunks before acking.

    This is the ~250ms window that made chat.spec.ts's pulsing assertion flaky
    on a loaded runner. If this ever starts emitting chunks, SLOW has acquired
    a wind-down of its own and LATEACK is redundant.
    """
    monkeypatch.setattr(fake, "SLOW_CHUNKS", 10)
    monkeypatch.setattr(fake, "SLOW_CHUNK_DELAY_SECS", 0)
    buf = _capture(monkeypatch)
    fake._INBOX.put(
        {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "s1"}}
    )
    fake._handle(_prompt(fake.SLOW_TRIGGER))
    msgs = _messages(buf)
    assert [m for m in msgs if m.get("method") == "session/update"] == []
    assert msgs[-1]["result"]["stopReason"] == "cancelled"


def test_slow_lateack_without_a_cancel_ends_the_turn_normally(
    monkeypatch, fast_slow_stream
):
    """No cancel means no wind-down: the stream runs to completion."""
    buf = _capture(monkeypatch)
    fake._handle(_prompt(fake.SLOW_LATEACK_TRIGGER))
    msgs = _messages(buf)
    assert len([m for m in msgs if m.get("method") == "session/update"]) == 3
    assert msgs[-1]["result"]["stopReason"] == "end_turn"


def test_slow_lateack_acks_even_if_the_stream_ends_first(monkeypatch):
    """The ack must survive the wind-down outliving the stream.

    `_cancel_requested` CONSUMES the message from `_INBOX`, so the observation
    has to be latched. If the wind-down runs past the last chunk and the final
    return re-polls instead of reading the latch, the poll comes back False and
    the turn reports `end_turn` -- the host then waits out the whole soft-stop
    budget and hard-kills a session the agent actually cancelled.
    """
    monkeypatch.setattr(fake, "SLOW_CHUNKS", 3)
    monkeypatch.setattr(fake, "SLOW_CHUNK_DELAY_SECS", 0)
    monkeypatch.setattr(fake, "SLOW_LATEACK_CHUNKS", 5)
    buf = _capture(monkeypatch)
    fake._INBOX.put(
        {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "s1"}}
    )
    fake._handle(_prompt(fake.SLOW_LATEACK_TRIGGER))
    # Inbox drained by the single consuming poll, yet the ack still lands.
    assert fake._INBOX.empty()
    assert _messages(buf)[-1]["result"]["stopReason"] == "cancelled"


def _review_fix_marker(path: str, content: bytes) -> str:
    encoded = base64.b64encode(content).decode("ascii")
    return f"{fake.REVIEW_FIX_EDIT_PREFIX}{path}|{encoded}{fake.REVIEW_FIX_EDIT_SUFFIX}"


def test_review_fix_edit_writes_exact_bytes_in_candidate_cwd(monkeypatch, tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    target = candidate / "src" / "example.py"
    target.parent.mkdir()
    target.write_bytes(b"before\n")
    monkeypatch.chdir(candidate)
    content = "line 1\nภาษาไทย — café\n".encode("utf-8")
    buf = _capture(monkeypatch)

    fake._handle(_prompt(_review_fix_marker("src/example.py", content)))

    assert target.read_bytes() == content
    updates = [
        m["params"]["update"]
        for m in _messages(buf)
        if m.get("method") == "session/update"
    ]
    assert [update["sessionUpdate"] for update in updates] == [
        "tool_call",
        "tool_call_update",
        "agent_message_chunk",
    ]
    assert updates[1]["status"] == "completed"


def test_review_fix_edit_rejects_absolute_path(monkeypatch, tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    outside = tmp_path / "outside.py"
    monkeypatch.chdir(candidate)
    buf = _capture(monkeypatch)

    fake._handle(_prompt(_review_fix_marker(str(outside), b"must not write")))

    assert not outside.exists()
    msgs = _messages(buf)
    update = next(
        m["params"]["update"]
        for m in msgs
        if m.get("method") == "session/update"
        and m["params"]["update"]["sessionUpdate"] == "tool_call_update"
    )
    assert update["status"] == "failed"
    assert msgs[-1]["result"]["stopReason"] == "end_turn"


def test_review_fix_edit_rejects_parent_traversal(monkeypatch, tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    outside = tmp_path / "outside.py"
    monkeypatch.chdir(candidate)
    buf = _capture(monkeypatch)

    fake._handle(_prompt(_review_fix_marker("../outside.py", b"must not write")))

    assert not outside.exists()
    updates = [
        m["params"]["update"]
        for m in _messages(buf)
        if m.get("method") == "session/update"
    ]
    assert updates[1]["status"] == "failed"


def test_malformed_review_fix_edit_marker_finishes_without_hanging(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    buf = _capture(monkeypatch)
    marker = f"{fake.REVIEW_FIX_EDIT_PREFIX}src/example.py|not-base64{fake.REVIEW_FIX_EDIT_SUFFIX}"

    fake._handle(_prompt(marker))

    msgs = _messages(buf)
    updates = [
        m["params"]["update"]
        for m in msgs
        if m.get("method") == "session/update"
    ]
    assert updates[1]["status"] == "failed"
    assert msgs[-1]["result"]["stopReason"] == "end_turn"


def _permission_answer(option_id: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": fake._PERMISSION_REQ_ID,
        "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
    }


@pytest.mark.parametrize(
    ("option_id", "expected_status"),
    [("allow_once", "completed"), ("reject_once", "failed")],
)
def test_gated_permission_reflects_the_hosts_answer(
    monkeypatch, option_id, expected_status
):
    buf = _capture(monkeypatch)
    fake._INBOX.put(_permission_answer(option_id))
    fake._handle(_prompt(fake.GATED_PERMISSION_TRIGGER))
    msgs = _messages(buf)
    assert any(m.get("method") == "session/request_permission" for m in msgs)
    updates = [m for m in msgs if m.get("method") == "session/update"]
    tool_update = next(
        u
        for u in updates
        if u["params"]["update"]["sessionUpdate"] == "tool_call_update"
    )
    assert tool_update["params"]["update"]["status"] == expected_status


def test_gated_permission_cancelled_outcome_fails_the_tool_call(monkeypatch):
    buf = _capture(monkeypatch)
    fake._INBOX.put(
        {
            "jsonrpc": "2.0",
            "id": fake._PERMISSION_REQ_ID,
            "result": {"outcome": {"outcome": "cancelled"}},
        }
    )
    fake._handle(_prompt(fake.GATED_PERMISSION_TRIGGER))
    tool_update = next(
        m
        for m in _messages(buf)
        if m.get("method") == "session/update"
        and m["params"]["update"]["sessionUpdate"] == "tool_call_update"
    )
    assert tool_update["params"]["update"]["status"] == "failed"


def test_gated_permission_times_out_without_blocking(monkeypatch):
    """No answer must never hang the turn -- it completes instead."""
    monkeypatch.setattr(fake, "PERMISSION_WAIT_SECS", 0.05)
    buf = _capture(monkeypatch)
    fake._handle(_prompt(fake.GATED_PERMISSION_TRIGGER))
    msgs = _messages(buf)
    tool_update = next(
        m
        for m in msgs
        if m.get("method") == "session/update"
        and m["params"]["update"]["sessionUpdate"] == "tool_call_update"
    )
    assert tool_update["params"]["update"]["status"] == "completed"
    assert msgs[-1]["result"]["stopReason"] == "end_turn"


def test_ungated_permission_does_not_wait(monkeypatch):
    """The pre-existing fire-and-forget path must stay non-blocking."""
    monkeypatch.setattr(fake, "PERMISSION_WAIT_SECS", 30.0)
    buf = _capture(monkeypatch)
    started = time.monotonic()
    fake._handle(_prompt(fake.PERMISSION_TRIGGER))
    assert time.monotonic() - started < 1.0
    assert _messages(buf)[-1]["result"]["stopReason"] == "end_turn"


def test_poll_inbox_puts_back_non_matching_messages():
    keep = {"jsonrpc": "2.0", "method": "session/other", "params": {}}
    fake._INBOX.put(keep)
    assert fake._poll_inbox(lambda m: m.get("method") == "session/cancel") is None
    assert fake._INBOX.get_nowait() == keep


def test_pump_stdin_forwards_messages_then_the_eof_sentinel(monkeypatch):
    monkeypatch.setattr(
        fake.sys,
        "stdin",
        io.StringIO('{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'),
    )
    fake._pump_stdin()
    message = fake._INBOX.get_nowait()
    assert message is not None
    assert message["id"] == 1
    assert fake._INBOX.get_nowait() is None
