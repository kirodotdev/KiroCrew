"""Tests for ACP client: reading, classifying and tracking inbound frames, metadata and stderr."""

import asyncio
import json
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

from kiro_crew.acp.client import AcpClient, AcpError


class TestAcpClientReadMessage:
    @pytest.mark.asyncio
    async def test_read_valid_json(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        msg_data = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        line = json.dumps(msg_data) + "\n"

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=line.encode())
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        msg = await client._read_message(timeout=1.0)
        assert msg is not None
        assert msg.is_response_for(1)
        assert msg.result == {"ok": True}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "line",
        [
            pytest.param(b"not json\n", id="read_non_json_skipped"),
            pytest.param(b"\n", id="read_empty_line"),
            pytest.param(b"", id="read_eof"),
        ],
    )
    async def test_read_yields_no_message(self, tmp_path, line):
        client = AcpClient(work_dir=tmp_path)

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=line)
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        msg = await client._read_message(timeout=1.0)
        assert msg is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "line",
        [
            pytest.param(b"42\n", id="int"),
            pytest.param(b'"progress"\n', id="string"),
            pytest.param(b"null\n", id="null"),
            pytest.param(b"true\n", id="bool"),
            pytest.param(b"[]\n", id="empty-array"),
            pytest.param(b'[{"jsonrpc":"2.0","id":7,"result":{}}]\n', id="batch-array"),
            pytest.param(b"[" * 30_000 + b"]" * 30_000 + b"\n", id="nested-past-decoder-ceiling"),
            pytest.param(b"1" * 5_000 + b"\n", id="integer-past-digit-limit"),
        ],
    )
    async def test_read_line_that_is_not_a_json_object_skipped(self, tmp_path, line):
        """Valid JSON that is not an object, and JSON ``json.loads`` rejects with
        ``RecursionError`` or a plain ``ValueError``, is skipped like non-JSON."""
        client = AcpClient(work_dir=tmp_path)

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=line)
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        msg = await client._read_message(timeout=1.0)
        assert msg is None

    @pytest.mark.asyncio
    async def test_wait_for_response_reads_past_a_stray_scalar_line(self, tmp_path):
        """A stray non-object line before the awaited reply costs that line only;
        the initialize wait still returns the reply."""
        client = AcpClient(work_dir=tmp_path)
        reply = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}})

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(side_effect=[b"123\n", (reply + "\n").encode()])
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        result = await client._wait_for_response(1, timeout=2.0, method="initialize")
        assert result == {"protocolVersion": 1}

    @pytest.mark.asyncio
    async def test_read_buffer_overrun_drops_frame_and_keeps_reading(self, tmp_path):
        """A line exceeding the stdout buffer costs that ONE frame, not the turn.

        The stream is NOT corrupted afterwards: readline() removes the oversize
        line through its
        terminating newline (or clears the buffer when the newline has not
        arrived yet) and resumes the transport before raising ValueError. So the
        overrun joins the blank-line and non-JSON paths in returning None, and
        the caller's next read gets the following frame. Raising AcpProcessDied
        here killed a healthy live turn over one unreadably large frame.

        Driven through a REAL StreamReader so the recovery claim is asserted
        against asyncio's actual behaviour rather than a mock's side_effect.
        """
        client = AcpClient(work_dir=tmp_path)

        reader = asyncio.StreamReader(limit=256)
        mock_process = MagicMock()
        mock_process.stdout = reader
        mock_process.returncode = None
        client._process = mock_process

        reader.feed_data(b"X" * 1024 + b"\n")  # oversize frame
        reader.feed_data(b'{"jsonrpc":"2.0","method":"session/update","params":{}}\n')

        assert await client._read_message(timeout=1.0) is None  # frame dropped
        msg = await client._read_message(timeout=1.0)
        assert msg is not None and msg.method == "session/update"

    @pytest.mark.asyncio
    async def test_repeated_buffer_overruns_never_kill_the_process(self, tmp_path):
        """Oversize frames must not accumulate into a kill here.

        This reader carries no drain budget on purpose (see the asymmetry note in
        `_read_message`): every call is bounded by the caller's timeout, so a run
        of oversize frames costs only those frames. A frame-count cap would
        reintroduce exactly the defect this PR removes — death from a replay of
        properly-terminated but oversize frames."""
        client = AcpClient(work_dir=tmp_path)

        reader = asyncio.StreamReader(limit=256)
        mock_process = MagicMock()
        mock_process.stdout = reader
        mock_process.returncode = None
        client._process = mock_process

        for _ in range(40):
            reader.feed_data(b"X" * 1024 + b"\n")  # oversize, terminated
            assert await client._read_message(timeout=1.0) is None

        reader.feed_data(b'{"jsonrpc":"2.0","method":"session/update","params":{}}\n')
        msg = await client._read_message(timeout=1.0)
        assert msg is not None and msg.method == "session/update"


class TestAcpClientExtractChunk:
    def test_extract_text_chunk(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": "hello"},
                }
            },
        )
        assert client._extract_text_chunk(msg) == ("hello", False)

    def test_extract_thinking_chunk(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "thinking", "text": "let me think"},
                }
            },
        )
        assert client._extract_text_chunk(msg) == ("let me think", True)

    def test_extract_non_text_chunk(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "tool_call", "title": "exec"}},
        )
        assert client._extract_text_chunk(msg) == (None, False)

    @pytest.mark.parametrize("bad_update", [None, "chunk", [1], 7])
    def test_non_dict_update_returns_none(self, tmp_path, bad_update):
        """The update value comes straight from the agent process; a non-dict
        raised AttributeError on update.get() inside the prompt-turn dispatch
        path, tearing down the whole turn."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(method="session/update", params={"update": bad_update})
        assert client._extract_text_chunk(msg) == (None, False)  # must not raise


class TestAcpClientTrackToolCall:
    def test_tracks_tool_call(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "execute_bash",
                    "kind": "tool_use",
                }
            },
        )
        client._track_tool_call(msg)
        assert ("tool_use", "execute_bash") in client.last_prompt_stats.tool_calls

    @pytest.mark.parametrize("bad_update", [None, "call", [1], 7])
    def test_non_dict_update_ignored(self, tmp_path, bad_update):
        """Same boundary as _extract_text_chunk: non-dict update must be a
        no-op, not an AttributeError in the dispatch path."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(method="session/update", params={"update": bad_update})
        client._track_tool_call(msg)  # must not raise
        assert client.last_prompt_stats.tool_calls == []


class TestAcpClientTrackMetadata:
    def test_tracks_context_pct(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"contextUsagePercentage": 42.5},
        )
        client._track_metadata(msg)
        assert client.last_prompt_stats.context_pct == 42.5

    def test_backfill_clamps_malformed_pct(self, tmp_path, monkeypatch):
        """A degenerate metadata percentage (huge finite / inf / NaN) must not
        overflow round() and abort the turn; derived used stays in [0, window]."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200000)
        for bad in (1e308, float("inf"), float("nan")):
            client = AcpClient(work_dir=tmp_path)
            client._model = "some-model"
            # Must not raise OverflowError/ValueError.
            client._track_metadata(
                JsonRpcMessage(
                    method="_kiro.dev/metadata",
                    params={"contextUsagePercentage": bad},
                )
            )
            used = client.last_prompt_stats.context_used_tokens
            assert 0 <= used <= 200000
            # context_pct is sanitized at the source, never left non-finite.
            pct = client.last_prompt_stats.context_pct
            assert 0.0 <= pct <= 100.0


class TestAcpClientTrackUsageUpdate:
    """claude-agent-acp usage_update {used, size}: derives context_pct and
    records the raw token counts for the dashboard token text."""

    def _usage_msg(self, used, size):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "usage_update", "used": used, "size": size}},
        )

    def test_populates_pct_and_tokens(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(50000, 200000))
        stats = client.last_prompt_stats
        assert stats.context_pct == 25.0  # 50000 / 200000 * 100
        assert stats.context_used_tokens == 50000
        assert stats.context_window_tokens == 200000
        # A real usage_update marks the counts authoritative.
        assert stats.context_tokens_from_usage is True

    def test_metadata_pct_does_not_clobber_authoritative_tokens(self, tmp_path):
        """Regression: a real usage_update (408K/1000K → 40.8%) followed by a
        kiro metadata contextUsagePercentage=73 must NOT leave the headline pct
        at 73 while the token text still reads 408K/1000K (the desync the user
        saw in the context-window popover). usage_update wins for BOTH."""
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(408_000, 1_000_000))
        assert client.last_prompt_stats.context_pct == 40.8

        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={"contextUsagePercentage": 73},
            )
        )
        stats = client.last_prompt_stats
        assert stats.context_pct == 40.8  # NOT clobbered to 73
        assert stats.context_used_tokens == 408_000
        assert stats.context_window_tokens == 1_000_000

    def test_metadata_credits_still_captured_when_tokens_authoritative(self, tmp_path):
        """The pct guard must not drop kiro billing credits — those accumulate
        regardless of whether token counts are authoritative."""
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(408_000, 1_000_000))
        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={
                    "contextUsagePercentage": 73,
                    "meteringUsage": [{"unit": "credit", "value": 1.5}],
                },
            )
        )
        assert client.last_prompt_stats.credits == 1.5
        assert client.last_prompt_stats.context_pct == 40.8

    @pytest.mark.parametrize(
        "used,size",
        [
            pytest.param(None, None, id="missing_fields_leave_tokens_zero"),
            ("50000", "200000"),  # numeric strings: `size > 0` raises TypeError
            (50000, "200000"),
            ([50000], 200000),
            (50000, {"n": 1}),
            (True, True),  # bools are ints but nonsense as token counts
            # json parses NaN/Infinity literals to non-finite floats, which
            # pass an isinstance check but crash int() (ValueError/OverflowError).
            (float("nan"), 200000),
            (50000, float("inf")),
            (float("inf"), float("nan")),
            # An arbitrary-precision int beyond float range makes
            # math.isfinite itself raise OverflowError.
            (10**400, 200000),
            (50000, 10**400),
        ],
    )
    def test_non_numeric_used_size_treated_as_absent(self, tmp_path, used, size):
        """used/size come straight from the agent process; a non-numeric value
        raised TypeError on `size > 0` / `used / size` inside the prompt-turn
        dispatch path, tearing down the whole turn. Malformed values must be
        treated like absent ones."""
        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(used, size))  # must not raise
        stats = client.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_window_tokens == 0

    def test_float_used_size_still_tracked(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(50000.0, 200000.0))
        stats = client.last_prompt_stats
        assert stats.context_pct == 25.0
        assert stats.context_used_tokens == 50000
        assert stats.context_window_tokens == 200000

    def test_tokens_carry_forward_across_prompt_reset(self, tmp_path):
        # The per-prompt reset preserves the last known pct + token counts so
        # the dashboard ring/text doesn't flicker to 0 at the start of a turn.
        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(88000, 200000))
        prev_pct = client.last_prompt_stats.context_pct
        prev_used = client.last_prompt_stats.context_used_tokens
        prev_window = client.last_prompt_stats.context_window_tokens
        prev_from_usage = client.last_prompt_stats.context_tokens_from_usage
        from kiro_crew.acp.types import AcpPromptStats

        # Mirror the reset sites (send_message_stream / _dispatch_events / etc.)
        client.last_prompt_stats = AcpPromptStats(
            context_pct=prev_pct,
            context_used_tokens=prev_used,
            context_window_tokens=prev_window,
            context_tokens_from_usage=prev_from_usage,
        )
        assert client.last_prompt_stats.context_used_tokens == 88000
        assert client.last_prompt_stats.context_window_tokens == 200000
        assert client.last_prompt_stats.context_pct == 44.0
        # The authoritative flag must survive the reset, else the next turn's
        # early metadata pct could clobber the carried token-derived pct.
        assert client.last_prompt_stats.context_tokens_from_usage is True


class TestAcpClientNoProcess:
    @pytest.mark.asyncio
    async def test_send_request_no_process(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        with pytest.raises(AcpError, match="not running"):
            await client._send_request("test", {})

    @pytest.mark.asyncio
    async def test_read_message_no_process(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        with pytest.raises(AcpError, match="not running"):
            await client._read_message()


class TestProcessMessage:
    """Tests for _process_message action classification."""

    def _make_client(self):
        return AcpClient()

    def test_compaction_status_classified(self):
        client = self._make_client()
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS, params={"status": {"type": "completed"}}
        )
        assert client._process_message(msg, req_id=99) == "compaction"

    def test_clear_status_classified(self):
        client = self._make_client()
        from kiro_crew.acp.types import METHOD_CLEAR_STATUS, JsonRpcMessage

        msg = JsonRpcMessage(method=METHOD_CLEAR_STATUS, params={"sessionId": "s1"})
        assert client._process_message(msg, req_id=99) == "clear"

    def test_agent_switched_classified(self):
        client = self._make_client()
        from kiro_crew.acp.types import METHOD_AGENT_SWITCHED, JsonRpcMessage

        msg = JsonRpcMessage(method=METHOD_AGENT_SWITCHED, params={"agentName": "planner"})
        assert client._process_message(msg, req_id=99) == "agent_switched"

    def test_subagent_list_update_classified(self):
        client = self._make_client()
        from kiro_crew.acp.types import METHOD_SUBAGENT_LIST_UPDATE, JsonRpcMessage

        msg = JsonRpcMessage(method=METHOD_SUBAGENT_LIST_UPDATE, params={"subagents": []})
        assert client._process_message(msg, req_id=99) == "subagent_list"

    def test_kiro_session_update_classified(self):
        client = self._make_client()
        from kiro_crew.acp.types import METHOD_KIRO_SESSION_UPDATE, JsonRpcMessage

        msg = JsonRpcMessage(method=METHOD_KIRO_SESSION_UPDATE, params={"sessionId": "s1"})
        assert client._process_message(msg, req_id=99) == "subagent_activity"

    def test_unknown_method_skipped(self):
        client = self._make_client()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(method="_kiro.dev/unknown/thing")
        assert client._process_message(msg, req_id=99) == "skip"

    def test_mcp_oauth_request_classified(self):
        client = self._make_client()
        from kiro_crew.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        msg = JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"serverName": "linear", "oauthUrl": "https://mcp.linear.app/authorize?..."},
        )
        assert client._process_message(msg, req_id=99) == "mcp_oauth_request"

    def test_permission_request_with_colliding_id_not_complete(self):
        # Regression: the agent's server→client request_permission id space is
        # independent of our prompt req_id space, so they collide on small
        # integers.  A permission request whose id == the in-flight prompt's
        # req_id must classify as "permission", NOT "complete" — otherwise the
        # turn ends early and the tool blocks forever (stuck Claude Code turn).
        client = self._make_client()
        from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION, JsonRpcMessage

        msg = JsonRpcMessage(
            id=4,
            method=METHOD_REQUEST_PERMISSION,
            params={"toolCall": {"title": "ls"}, "options": []},
        )
        assert client._process_message(msg, req_id=4) == "permission"

    def test_real_response_with_matching_id_completes(self):
        # The genuine prompt response (id + result, no method) still completes.
        client = self._make_client()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(id=4, result={"stopReason": "end_turn"})
        assert client._process_message(msg, req_id=4) == "complete"


class TestStreamEventsExtension:
    """End-to-end tests for stream_events() yielding extension events."""

    @pytest.mark.asyncio
    async def test_agent_switched_event_fields(self):
        """stream_events extracts agentName from params and yields correct AcpEvent."""
        from kiro_crew.acp.types import (
            EVENT_AGENT_SWITCHED,
            EVENT_COMPLETE,
            METHOD_AGENT_SWITCHED,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        # Build the two messages the prompt loop would yield
        switch_msg = JsonRpcMessage(method=METHOD_AGENT_SWITCHED, params={"agentName": "planner"})
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "agent_switched", switch_msg
            yield "complete", complete_msg

        # Patch internals so stream_events doesn't need a real process
        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 2
        assert events[0].kind == EVENT_AGENT_SWITCHED
        assert events[0].text == "planner"
        assert events[1].kind == EVENT_COMPLETE

    @pytest.mark.asyncio
    async def test_compaction_event_fields(self):
        """stream_events extracts status.type and summary from compaction params."""
        from kiro_crew.acp.types import (
            EVENT_COMPACTION_STATUS,
            METHOD_COMPACTION_STATUS,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        compact_msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "completed"}, "summary": "3k tokens saved"},
        )
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "compaction", compact_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 2
        assert events[0].kind == EVENT_COMPACTION_STATUS
        assert events[0].text == "completed"
        assert events[0].title == "3k tokens saved"

    @pytest.mark.asyncio
    async def test_clear_event_fields(self):
        """stream_events yields EVENT_CLEAR_STATUS with no extra fields."""
        from kiro_crew.acp.types import (
            EVENT_CLEAR_STATUS,
            METHOD_CLEAR_STATUS,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        clear_msg = JsonRpcMessage(method=METHOD_CLEAR_STATUS, params={"sessionId": "s1"})
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "clear", clear_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 2
        assert events[0].kind == EVENT_CLEAR_STATUS
        assert events[0].text == ""

    @pytest.mark.asyncio
    async def test_mcp_oauth_request_event_fields(self):
        """stream_events extracts serverName + oauthUrl and yields EVENT_MCP_OAUTH_REQUEST."""
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_MCP_OAUTH_REQUEST,
            METHOD_MCP_OAUTH_REQUEST,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        url = "https://mcp.linear.app/authorize?response_type=code&client_id=abc&state=xyz"
        oauth_msg = JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"sessionId": "s1", "serverName": "linear", "oauthUrl": url},
        )
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "mcp_oauth_request", oauth_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 2
        assert events[0].kind == EVENT_MCP_OAUTH_REQUEST
        assert events[0].server_name == "linear"
        assert events[0].oauth_url == url
        assert events[1].kind == EVENT_COMPLETE

    @pytest.mark.asyncio
    async def test_mcp_oauth_request_missing_url_skipped(self):
        """Notifications without oauthUrl are dropped (no event yielded)."""
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_MCP_OAUTH_REQUEST,
            METHOD_MCP_OAUTH_REQUEST,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        bad_msg = JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"serverName": "broken-server"},  # no oauthUrl
        )
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "mcp_oauth_request", bad_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        # Only the complete event — the malformed oauth notification was dropped.
        assert len(events) == 1
        assert events[0].kind == EVENT_COMPLETE
        assert not any(e.kind == EVENT_MCP_OAUTH_REQUEST for e in events)

    @pytest.mark.asyncio
    async def test_tool_interrupted_marker_synthesizes_complete(self):
        """When kiro-cli cancels tools, stream_events completes instead of hanging.

        Regression: kiro-cli's built-in security filter cancels tool uses and emits a
        text chunk "Tool uses were interrupted, waiting for the next user prompt", but
        never sends a session/prompt response. Detect the marker and synthesize a
        complete event so the caller exits cleanly.
        """
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            UPDATE_AGENT_MESSAGE_CHUNK,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        interrupt_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": "Tool uses were interrupted, waiting for the next user prompt",
                    },
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", interrupt_msg
            # No "complete" — simulates kiro-cli leaving the prompt hanging.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []
        client._emit_tool_interrupted_sel = MagicMock()

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        # Text chunk yielded to caller, then synthesized EVENT_COMPLETE.
        assert [e.kind for e in events] == [EVENT_TEXT_CHUNK, EVENT_COMPLETE]
        assert "Tool uses were interrupted" in events[0].text
        client._emit_tool_interrupted_sel.assert_called_once_with("_dispatch_events")

    @pytest.mark.asyncio
    async def test_tool_interrupted_marker_send_message_stream_returns(self):
        """send_message_stream returns cleanly (no hang) when kiro-cli cancels tools."""
        from kiro_crew.acp.types import UPDATE_AGENT_MESSAGE_CHUNK, JsonRpcMessage

        client = AcpClient()
        interrupt_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": "Tool uses were interrupted, waiting for the next user prompt",
                    },
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", interrupt_msg
            # No "complete" — without the fix, the generator would never exit.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._emit_tool_interrupted_sel = MagicMock()

        chunks: list[str] = []
        async for c in client.send_message_stream("test"):
            chunks.append(c)

        assert chunks == ["Tool uses were interrupted, waiting for the next user prompt"]
        client._emit_tool_interrupted_sel.assert_called_once_with("send_message_stream")

    @pytest.mark.asyncio
    async def test_tool_interrupted_marker_send_message_returns(self):
        """send_message returns accumulated text instead of raising AcpTimeoutError."""
        from kiro_crew.acp.types import UPDATE_AGENT_MESSAGE_CHUNK, JsonRpcMessage

        client = AcpClient()
        interrupt_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": "Tool uses were interrupted, waiting for the next user prompt",
                    },
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", interrupt_msg
            # No "complete" — would otherwise raise AcpTimeoutError on loop exit.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._emit_tool_interrupted_sel = MagicMock()

        result = await client.send_message("test", timeout=5.0)
        assert "Tool uses were interrupted" in result
        client._emit_tool_interrupted_sel.assert_called_once_with("_read_prompt_response")

    @pytest.mark.asyncio
    async def test_tool_interrupted_marker_requires_exact_match(self):
        """Substring-but-not-exact match must NOT trigger early completion.

        Protects against false positives when the model quotes the marker text.
        """
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            UPDATE_AGENT_MESSAGE_CHUNK,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        quoted_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": (
                            "The message 'Tool uses were interrupted, waiting for the next "
                            "user prompt' means kiro-cli blocked the tool."
                        ),
                    },
                }
            },
        )
        complete_msg = JsonRpcMessage(method="session/prompt", id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "update", quoted_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []
        client._emit_tool_interrupted_sel = MagicMock()

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        # Normal flow: text chunk + real complete event (not synthesized early).
        assert [e.kind for e in events] == [EVENT_TEXT_CHUNK, EVENT_COMPLETE]
        # Precondition + guard: marker IS a substring, but NOT an exact match.
        marker = "Tool uses were interrupted, waiting for the next user prompt"
        assert marker in events[0].text
        assert events[0].text.strip() != marker
        # Exact-match guard held: SEL must NOT be emitted for a quoted marker.
        client._emit_tool_interrupted_sel.assert_not_called()

    @pytest.mark.asyncio
    async def test_tool_interrupted_marker_ignored_in_thinking_chunk(self):
        """Marker arriving as a thinking chunk must NOT trigger early completion.

        The `_dispatch_events` path guards marker detection with `not is_thinking`.
        If a reasoning/thinking chunk happens to contain the exact marker text,
        it must not be treated as kiro-cli's interrupt signal — only top-level
        agent_message_chunk with non-thinking content type is the real signal.
        """
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_THINKING_CHUNK,
            UPDATE_AGENT_MESSAGE_CHUNK,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        thinking_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "thinking",
                        "text": "Tool uses were interrupted, waiting for the next user prompt",
                    },
                }
            },
        )
        complete_msg = JsonRpcMessage(method="session/prompt", id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "update", thinking_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []
        client._emit_tool_interrupted_sel = MagicMock()

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        # Thinking chunk yielded as EVENT_THINKING_CHUNK, real complete follows.
        assert [e.kind for e in events] == [EVENT_THINKING_CHUNK, EVENT_COMPLETE]
        # Guard held: SEL must NOT be emitted for a thinking-type chunk.
        client._emit_tool_interrupted_sel.assert_not_called()

    @pytest.mark.asyncio
    async def test_tool_interrupted_sel_contract(self):
        """SEL audit fields are pinned: tool_name, outcome, kind, site.

        The other marker tests mock _emit_tool_interrupted_sel itself, which only
        asserts it's invoked — not that the audit event carries the right fields.
        This test patches kiro_crew.sel.sel so the real helper runs, protecting
        against silent regressions in the security-audit contract.
        """
        from kiro_crew.acp.types import (
            UPDATE_AGENT_MESSAGE_CHUNK,
            JsonRpcMessage,
        )

        client = AcpClient(session_key="sess-xyz")
        interrupt_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": "Tool uses were interrupted, waiting for the next user prompt",
                    },
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", interrupt_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        with patch("kiro_crew.sel.sel") as mock_sel:
            async for _ in client.stream_events("test"):
                pass

        mock_sel.return_value.log_tool_invocation.assert_called_once()
        kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "kiro_cli_security_filter"
        assert kwargs["tool_kind"] == "client_built_in"
        assert kwargs["outcome"] == "denied"
        assert kwargs["source"] == "acp"
        assert kwargs["session_key"] == "sess-xyz"
        assert kwargs["metadata"]["site"] == "_dispatch_events"
        assert kwargs["metadata"]["reason"] == "tool_interrupted_marker"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "entry_point, expected_site",
        [
            ("stream_events", "_dispatch_events"),
            ("send_message_stream", "send_message_stream"),
            ("send_message", "_read_prompt_response"),
        ],
    )
    async def test_tool_interrupted_all_paths_log_and_return_promptly(
        self, entry_point, expected_site, caplog
    ):
        """Across all three entry points: returns in bounded time, logs WARNING
        with the correct site tag, records one SEL audit.

        Pins three properties the other tests don't cover:
        1. Timing — without the fix the call hangs until the 2h prompt timeout.
           An explicit <2s bound documents the "no hang" contract.
        2. Single WARNING per call, tagged with the originating call site.
           On-call greps ``kiro-cli cancelled tool use`` — a silent regression at
           any site would mask real filter firings in production.
        3. SEL audit fires with metadata.site matching the call site, across all
           three sites (not just _dispatch_events).
        """
        import logging
        import time

        from kiro_crew.acp.types import (
            UPDATE_AGENT_MESSAGE_CHUNK,
            JsonRpcMessage,
        )

        client = AcpClient(session_key="sess-abc")
        client._session_id = "session-pin-1234"

        interrupt_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": "Tool uses were interrupted, " "waiting for the next user prompt",
                    },
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", interrupt_msg
            # No "complete" — simulates kiro-cli leaving the prompt hanging.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        caplog.set_level(logging.WARNING, logger="kiro_crew.acp.client")

        t0 = time.monotonic()
        with patch("kiro_crew.sel.sel") as mock_sel:
            if entry_point == "stream_events":
                async for _ in client.stream_events("test"):
                    pass
            elif entry_point == "send_message_stream":
                async for _ in client.send_message_stream("test"):
                    pass
            elif entry_point == "send_message":
                await client.send_message("test")
        elapsed = time.monotonic() - t0

        # (1) Timing — no hang.  Generous bound; in practice this is <50ms.
        assert elapsed < 2.0, f"entry_point={entry_point} took {elapsed:.3f}s"

        # (2) Exactly one WARNING, tagged with the originating call site.
        warns = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "kiro-cli cancelled tool use" in r.getMessage()
        ]
        assert len(warns) == 1, (
            f"entry_point={entry_point} expected 1 WARNING, got {len(warns)}: "
            f"{[r.getMessage() for r in warns]}"
        )
        assert f"site={expected_site}" in warns[0].getMessage()

        # (3) SEL audit — one call, metadata.site matches the call site.
        mock_sel.return_value.log_tool_invocation.assert_called_once()
        kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert kwargs["metadata"]["site"] == expected_site


class TestDrainStderrRedaction:
    """Test 3.1: _drain_stderr logs redacted text but stores raw."""

    @pytest.mark.asyncio
    async def test_raw_stored_redacted_logged(self):
        client = AcpClient()
        raw = "Error: key=AKIAIOSFODNN7EXAMPLE connect failed"

        reader = AsyncMock(spec=["readline"])
        reader.readline = AsyncMock(side_effect=[raw.encode() + b"\n", b""])

        with patch("kiro_crew.acp.client.logger") as mock_logger:
            await client._drain_stderr(reader)

        # Raw stored
        assert list(client._stderr_lines) == [raw]
        # Logged call used redacted text (credential replaced)
        logged_text = mock_logger.warning.call_args[0][2]
        assert "AKIAIOSFODNN7EXAMPLE" not in logged_text


class TestDrainStderrSuppression:
    """_drain_stderr drops high-frequency content-free adapter
    diagnostics (thinking_tokens "Unexpected case" lines) without warning or
    polluting the diagnostic ring buffer, while still proving liveness."""

    def _reader(self, lines):
        """A mock StreamReader that yields *lines* (str) then EOF."""
        reader = AsyncMock(spec=["readline"])
        reader.readline = AsyncMock(side_effect=[line.encode() + b"\n" for line in lines] + [b""])
        return reader

    @pytest.mark.asyncio
    async def test_marker_lines_suppressed_not_logged_or_buffered(self):
        client = AcpClient()
        marker = (
            'Unexpected case: {"type":"system","subtype":"thinking_tokens",'
            '"estimated_tokens":1234,"session_id":"abc-123"}'
        )
        reader = self._reader([marker] * 5)

        with patch("kiro_crew.acp.client.logger") as mock_logger:
            await client._drain_stderr(reader)

        # No per-line WARNING for suppressed markers.
        mock_logger.warning.assert_not_called()
        # Not appended to the bounded diagnostic ring buffer.
        assert list(client._stderr_lines) == []

    @pytest.mark.asyncio
    async def test_suppressed_lines_still_advance_liveness(self):
        client = AcpClient()
        client._last_activity = 0.0  # force a detectable advance
        reader = self._reader(["estimated thinking_tokens delta"])

        with patch("kiro_crew.acp.client.logger"):
            await client._drain_stderr(reader)

        # Liveness must advance for suppressed lines so the idle watchdog
        # (is_responsive) does not kill an actively-thinking turn.
        assert client._last_activity > 0.0

    @pytest.mark.asyncio
    async def test_real_errors_pass_through(self):
        client = AcpClient()
        raw = "Error: adapter crashed in handler"
        reader = self._reader([raw])

        with patch("kiro_crew.acp.client.logger") as mock_logger:
            await client._drain_stderr(reader)

        assert list(client._stderr_lines) == [raw]
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args[0][2] == raw

    @pytest.mark.asyncio
    async def test_mixed_stream_keeps_real_error_after_burst(self):
        client = AcpClient()
        real_before = "Error: first real failure"
        real_after = "Error: second real failure"
        burst = ["system subtype thinking_tokens delta"] * 50
        reader = self._reader([real_before] + burst + [real_after])

        with patch("kiro_crew.acp.client.logger") as mock_logger:
            await client._drain_stderr(reader)

        # Only the two real lines are logged and buffered; the 50-line burst
        # neither logs nor evicts the earlier real error from the ring buffer.
        assert list(client._stderr_lines) == [real_before, real_after]
        assert mock_logger.warning.call_count == 2


class TestReadMessageStderrRedaction:
    """Test 3.2: _read_message redacts stderr in AcpError."""

    @pytest.mark.asyncio
    async def test_acperror_contains_redacted_stderr(self):
        client = AcpClient()
        client._cancelled = False
        client._buffer = MagicMock()
        client._buffer.__bool__ = lambda s: False
        client._process = MagicMock()
        client._process.returncode = 1
        client._process.stdout = MagicMock()
        client._process.stdout.readline = AsyncMock(return_value=b"")
        client._stderr_lines = deque(["secret key=AKIAIOSFODNN7EXAMPLE"])
        client._stderr_task = MagicMock()
        client._stderr_task.done.return_value = True

        with pytest.raises(AcpError, match="ACP process exited") as exc_info:
            await client._read_message(timeout=1.0)

        assert "AKIAIOSFODNN7EXAMPLE" not in str(exc_info.value)


class TestTrackMetadataCredits:
    """_track_metadata extracts per-turn credits from kiro meteringUsage."""

    @staticmethod
    def _metadata_msg(credit_values, *, pct=17.1):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={
                "contextUsagePercentage": pct,
                "meteringUsage": [
                    {"value": v, "unit": "credit", "unitPlural": "credits"} for v in credit_values
                ],
            },
        )

    def test_accumulates_credits_from_metering_usage(self):
        client = AcpClient()
        client._track_metadata(self._metadata_msg([0.63]))
        assert client.last_prompt_stats.credits == pytest.approx(0.63)
        assert client.last_prompt_stats.context_pct == pytest.approx(17.1)

    def test_sums_credits_across_notifications(self):
        client = AcpClient()
        client._track_metadata(self._metadata_msg([0.5]))
        client._track_metadata(self._metadata_msg([0.25, 0.25]))
        assert client.last_prompt_stats.credits == pytest.approx(1.0)

    def test_ignores_non_credit_units_and_bad_values(self):
        client = AcpClient()
        msg = self._metadata_msg([])
        msg.params["meteringUsage"] = [
            {"value": 5, "unit": "token"},
            {"value": "x", "unit": "credit"},
            {"value": 0.4, "unit": "credit"},
        ]
        client._track_metadata(msg)
        assert client.last_prompt_stats.credits == pytest.approx(0.4)

    def test_metadata_without_metering_usage_leaves_credits_zero(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={"contextUsagePercentage": 12.5},
            )
        )
        assert client.last_prompt_stats.credits == 0.0
        assert client.last_prompt_stats.context_pct == pytest.approx(12.5)


class TestTrackMetadataWindowResolution:
    """_track_metadata derives context_window_tokens from the resolved model
    when no usage_update has set it (kiro-cli 2.10+ does not send one)."""

    @staticmethod
    def _metadata_msg(pct):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"contextUsagePercentage": pct},
        )

    def test_resolves_1m_window_from_resolved_model_id(self):
        client = AcpClient()
        assert client._model == "auto"
        client._resolved_model_id = "claude-opus-4.6"
        client._track_metadata(self._metadata_msg(25.0))
        assert client.last_prompt_stats.context_window_tokens == 1_000_000
        assert client.last_prompt_stats.context_used_tokens == 250_000
        assert client.last_prompt_stats.context_pct == pytest.approx(25.0)

    def test_resolves_200k_window_for_200k_model(self):
        client = AcpClient()
        client._resolved_model_id = "claude-opus-4.5"
        client._track_metadata(self._metadata_msg(50.0))
        assert client.last_prompt_stats.context_window_tokens == 200_000
        assert client.last_prompt_stats.context_used_tokens == 100_000

    def test_falls_back_to_self_model_when_resolved_model_id_unset(self):
        client = AcpClient()
        client._model = "claude-sonnet-4.6"
        client._track_metadata(self._metadata_msg(10.0))
        assert client.last_prompt_stats.context_window_tokens == 1_000_000

    def test_does_not_overwrite_window_already_set_by_usage_update(self):
        client = AcpClient()
        client._resolved_model_id = "claude-opus-4.5"  # would resolve to 200K
        # Simulate a real usage_update having established authoritative counts
        # (1M window, 300K used -> 30%). context_tokens_from_usage marks them
        # authoritative — the shape a real usage_update leaves behind.
        client.last_prompt_stats.context_window_tokens = 1_000_000
        client.last_prompt_stats.context_used_tokens = 300_000
        client.last_prompt_stats.context_pct = 30.0
        client.last_prompt_stats.context_tokens_from_usage = True
        client._track_metadata(self._metadata_msg(50.0))
        # Metadata must neither re-resolve the window (no backfill once
        # authoritative) nor clobber the usage-derived pct to its own 50%.
        assert client.last_prompt_stats.context_window_tokens == 1_000_000
        assert client.last_prompt_stats.context_pct == pytest.approx(30.0)

    def test_no_model_resolvable_leaves_window_zero(self):
        client = AcpClient()
        client._model = ""
        client._resolved_model_id = None
        client._track_metadata(self._metadata_msg(5.0))
        assert client.last_prompt_stats.context_window_tokens == 0

    def test_non_registry_uncached_model_leaves_window_zero_not_200k(self):
        # REGRESSION (gpt-5.6-terra showed 200K): a model that is NEITHER in the
        # registry NOR the kiro-list cache must NOT be backfilled to a guessed
        # 200k — that would override the frontend's authoritative per-model
        # window. Leave it 0 so the frontend cache drives the meter; kiro's real
        # usage_update.size still sets the correct window when it comes. Use a
        # clearly-synthetic id so a real kiro-list cache (which legitimately
        # carries GPT/DeepSeek windows) can't make this model "known".
        client = AcpClient()
        client._resolved_model_id = "totally-unknown-model-xyz"
        client._track_metadata(self._metadata_msg(10.0))
        assert client.last_prompt_stats.context_window_tokens == 0
        # pct is still recorded so the frontend can derive used from its window.
        assert client.last_prompt_stats.context_pct == pytest.approx(10.0)

    def test_kiro_cached_non_registry_model_backfills_real_window(self):
        # The flip side of the centralization: once the kiro-list cache is seeded
        # with a non-Anthropic model's real window, the backfill SHOULD report it
        # (it is now a "known" window), instead of the old no-op. This is what
        # lets GPT/DeepSeek sessions show their real window before a usage_update.
        import kiro_crew.model_registry as mr

        saved = dict(mr._KIRO_WINDOWS)
        try:
            mr._KIRO_WINDOWS["deepseek-3.2"] = 164000
            client = AcpClient()
            client._resolved_model_id = "deepseek-3.2"
            client._track_metadata(self._metadata_msg(50.0))
            assert client.last_prompt_stats.context_window_tokens == 164000
            assert client.last_prompt_stats.context_used_tokens == 82000
        finally:
            mr._KIRO_WINDOWS.clear()
            mr._KIRO_WINDOWS.update(saved)

    def test_malformed_pct_does_not_raise_or_update_stats(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        client._resolved_model_id = "claude-opus-4.6"
        client.last_prompt_stats.context_pct = 42.0
        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata", params={"contextUsagePercentage": "not-a-number"}
            )
        )
        assert client.last_prompt_stats.context_pct == 42.0
        assert client.last_prompt_stats.context_window_tokens == 0
