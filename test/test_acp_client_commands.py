"""Tests for ACP client: slash commands, cancel and wait_for_compaction."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

from kiro_crew.acp.client import AcpClient, format_command_result, parse_slash_command

# ── Coverage push: command wrappers ──


class TestSendCommand:
    """Tests for send_command."""

    @pytest.mark.asyncio
    async def test_send_command_returns_text(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        client._process = MagicMock()
        client._process.returncode = None
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=10)
        client._wait_for_response = AsyncMock(return_value={"text": "usage: 42%"})

        result = await client.send_command("/usage")
        assert "42%" in result

    @pytest.mark.asyncio
    async def test_send_command_timeout_returns_empty(self, tmp_path):
        from kiro_crew.acp.client import AcpTimeoutError

        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        client._process = MagicMock()
        client._process.returncode = None
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=10)
        client._wait_for_response = AsyncMock(side_effect=AcpTimeoutError())

        result = await client.send_command("/compact")
        assert result == ""

    @pytest.mark.asyncio
    async def test_send_command_redacts_urls(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        client._process = MagicMock()
        client._process.returncode = None
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=10)
        client._wait_for_response = AsyncMock(
            return_value={"text": "visit https://evil.com/exfil?data=secret"}
        )

        # send_command MUST run raw text through the redactor before returning.
        # The redactor itself is unit-tested separately in test_security_exfil_urls.py;
        # here we verify the call site wires it up at all (a vacuous isinstance
        # check would not catch a missed redact step).
        with patch(
            "kiro_crew.acp.client.redact_exfiltration_urls",
            return_value=("[redacted]", ["url"]),
        ) as mock_redact:
            result = await client.send_command("/test")

        mock_redact.assert_called_once_with("visit https://evil.com/exfil?data=secret")
        assert result == "[redacted]"


class TestCancelSession:
    """Tests for cancel_session."""

    @pytest.mark.asyncio
    async def test_cancel_sends_notification(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "sess-1"
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.returncode = None
        client._process = proc

        await client.cancel_session()

        assert client._cancelled is True
        proc.stdin.write.assert_called_once()
        written = proc.stdin.write.call_args[0][0]
        data = json.loads(written.decode())
        assert data["method"] == "session/cancel"
        assert data["params"]["sessionId"] == "sess-1"

    @pytest.mark.asyncio
    async def test_cancel_no_session_id_skips(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = None
        cancel_ts = client._cancel_ts
        await client.cancel_session()  # should not raise
        # No session to cancel, so no cancel is armed for the read loop.
        assert client._cancelled is False
        assert client._cancel_ts == cancel_ts

    @pytest.mark.asyncio
    async def test_cancel_no_process_skips(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        client._process = None
        await client.cancel_session()
        assert client._cancelled is True

    @pytest.mark.asyncio
    async def test_cancel_write_exception_handled(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock(side_effect=BrokenPipeError())
        proc.stdin.drain = AsyncMock()
        proc.returncode = None
        client._process = proc

        await client.cancel_session()  # should not raise
        assert client._cancelled is True

    @pytest.mark.asyncio
    async def test_cancel_raises_grace_window_to_budget(self, tmp_path):
        """A budget above the 10s floor must extend the read-grace window so
        the read loop does not abort the turn early and force a hard kill."""
        from kiro_crew.acp.client import _CANCEL_GRACE_SECS

        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.returncode = None
        client._process = proc

        await client.cancel_session(grace_secs=30.0)
        assert client._cancel_grace_secs == 30.0

        # A budget below the floor never shrinks the window.
        client._cancel_grace_secs = _CANCEL_GRACE_SECS
        await client.cancel_session(grace_secs=2.0)
        assert client._cancel_grace_secs == _CANCEL_GRACE_SECS


class TestWaitForCompaction:
    """Tests for wait_for_compaction."""

    @pytest.mark.asyncio
    async def test_returns_completed(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "completed"}, "summary": "saved 3k"},
        )
        client._read_message = AsyncMock(side_effect=[None, msg])

        result = await client.wait_for_compaction(timeout=5.0)
        assert result == {"type": "completed", "summary": "saved 3k"}

    @pytest.mark.asyncio
    async def test_returns_failed(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "failed"}, "summary": "error"},
        )
        client._read_message = AsyncMock(return_value=msg)

        result = await client.wait_for_compaction(timeout=5.0)
        assert result == {"type": "failed", "summary": "error"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("summary", ["", None])
    async def test_failed_with_empty_summary_carries_the_payload_reason(self, tmp_path, summary):
        """kiro-cli's ``summary`` is empty on failure, so the wait result carries
        the reason the payload names -- read by the same extractor the dispatch
        loop uses -- and a manual /compact names its cause like auto-compaction."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={
                "status": {"type": "failed", "error": "context window exceeded"},
                "summary": summary,
            },
        )
        client._read_message = AsyncMock(return_value=msg)

        result = await client.wait_for_compaction(timeout=5.0)
        assert result == {"type": "failed", "summary": "context window exceeded"}

    @pytest.mark.asyncio
    async def test_failed_without_a_reason_reports_the_extractor_fallback(self, tmp_path):
        """No summary and no reason-bearing key: the result carries the
        extractor's own generic text, the same line the streaming notice shows."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.client import compaction_failure_detail
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        params = {"status": {"type": "failed"}, "summary": ""}
        msg = JsonRpcMessage(method=METHOD_COMPACTION_STATUS, params=params)
        client._read_message = AsyncMock(return_value=msg)

        result = await client.wait_for_compaction(timeout=5.0)
        assert result["type"] == "failed"
        assert result["summary"] == compaction_failure_detail(params)
        assert result["summary"].startswith("no reason reported by the agent")

    @pytest.mark.asyncio
    async def test_failed_with_a_credential_shaped_summary_is_redacted(self, tmp_path):
        """A backend-echoed failure summary is LLM-influenced text, so the wait
        result carries it scrubbed -- the same ``redact_text`` the session
        handle applies -- before the dashboard or a channel mirror shows it."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={
                "status": {"type": "failed"},
                "summary": "backend error: key AKIAIOSFODNN7EXAMPLE rejected",
            },
        )
        client._read_message = AsyncMock(return_value=msg)

        result = await client.wait_for_compaction(timeout=5.0)
        assert result["type"] == "failed"
        assert "AKIAIOSFODNN7EXAMPLE" not in result["summary"]
        assert "[REDACTED: credential]" in result["summary"]

    @pytest.mark.asyncio
    async def test_timeout_returns_timeout_dict(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._read_message = AsyncMock(return_value=None)

        result = await client.wait_for_compaction(timeout=0.1)
        assert result == {"type": "timeout"}

    @pytest.mark.asyncio
    async def test_buffers_non_compaction_notifications(self, tmp_path, monkeypatch):
        import kiro_crew.acp.client as c

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200_000)
        client = AcpClient(work_dir=tmp_path)
        client._model = "some-model"
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, METHOD_METADATA, JsonRpcMessage

        meta_msg = JsonRpcMessage(method=METHOD_METADATA, params={"contextUsagePercentage": 55.0})
        other_notif = JsonRpcMessage(method="mcp/something", params={"x": 1})
        compact_msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "completed"}, "summary": "ok"},
        )
        client._read_message = AsyncMock(side_effect=[meta_msg, other_notif, compact_msg])

        result = await client.wait_for_compaction(timeout=5.0)
        assert result["type"] == "completed"
        # The metadata WAS consumed (window backfill proves _track_metadata
        # ran), but its pct described the PRE-compaction transcript — the
        # completed status drops it (reset_after_compaction) so the meter
        # doesn't freeze at a stale value.
        assert client.last_prompt_stats.context_window_tokens == 200_000
        assert client.last_prompt_stats.context_pct == 0.0
        assert len(client._mcp_notifications) == 1


# ── Coverage push: additional coverage ──


class TestFormatCommandResult:
    """Tests for format_command_result."""

    def test_structured_data_with_message(self):
        result = format_command_result({"data": {"key": "value"}, "message": "Done"})
        assert "Done" in result
        assert "```json" in result
        assert '"key"' in result

    def test_structured_data_without_message(self):
        result = format_command_result({"data": {"key": "val"}, "message": ""})
        assert "```json" in result
        assert '"key"' in result

    def test_agent_model_filtered(self):
        result = format_command_result({"data": {"agent": "x", "model": "y"}, "message": ""})
        # Only agent/model → display is empty → falls through to message
        assert result == ""

    def test_message_only(self):
        result = format_command_result({"message": "hello"})
        assert result == "hello"

    def test_empty_result(self):
        result = format_command_result({})
        assert result == ""


class TestParseSlashCommand:
    """Tests for parse_slash_command."""

    def test_simple_command(self):
        name, args = parse_slash_command("/compact")
        assert name == "compact"
        assert args == {}

    @pytest.mark.parametrize(
        ("text", "expected_name", "expected_value"),
        [
            pytest.param("/agent planner", "agent", "planner", id="command_with_value"),
            pytest.param(
                "/usage detailed view", "usage", "detailed view", id="command_with_multi_word_value"
            ),
        ],
    )
    def test_command_value(self, text, expected_name, expected_value):
        name, args = parse_slash_command(text)
        assert name == expected_name
        assert args == {"value": expected_value}


class TestStreamCommand:
    """Tests for stream_command."""

    @pytest.mark.asyncio
    async def test_stream_command_yields_events(self):
        from kiro_crew.acp.types import EVENT_COMPLETE, JsonRpcMessage

        client = AcpClient()
        client._session_id = "s1"
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=5)

        complete_msg = JsonRpcMessage(id=5, result={"message": "compacted", "data": {}})

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        events = []
        async for ev in client.stream_command("/compact"):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert EVENT_COMPLETE in kinds
