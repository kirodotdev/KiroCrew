"""The frames a live ``AcpClient`` session exchanges, and the tool results it reads.

Mid-session MCP notifications surface once per server and over a safe URL only;
commands and steers go out in their wire form; the read path answers an unhandled
inbound request; chunk, tool and permission frames are normalised; the JSONL
tool-result reader keeps what it can read; and the context window is backfilled
from a known model and re-read after compaction. Shared helpers live in
``acp_client_more_coverage_helpers.py``.
"""

import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest
from acp_client_more_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _client,
    _notify,
    _pinned_kiro_cli_version,
)

import kiro_crew.acp.client as acp_client
from kiro_crew.acp.client import AcpClient, AcpError, AcpTimeoutError
from kiro_crew.acp.types import (
    EVENT_AGENT_SWITCHED,
    EVENT_COMPLETE,
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    EVENT_PERMISSION_REQUEST,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    JSONRPC_METHOD_NOT_FOUND,
    METHOD_COMMANDS_EXECUTE,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_TOOL_CALL,
    JsonRpcMessage,
)

# ── _dispatch_events: mid-session MCP notifications ──


def _fake_loop(frames):
    """A _prompt_loop stand-in yielding pre-classified (action, msg) frames."""

    async def _loop(req_id, timeout):
        for frame in frames:
            yield frame

    return _loop


class TestDispatchMcpNotifications:
    @pytest.mark.asyncio
    async def test_oauth_and_server_lifecycle_events(self, tmp_path):
        client = _client(tmp_path)
        client._read_new_tool_results_sync = lambda: []
        client._prompt_loop = _fake_loop(
            [
                ("agent_switched", _notify("_kiro.dev/agent_switched", {"agentName": "reviewer"})),
                # Unsafe scheme: refused BEFORE dedupe is recorded.
                (
                    "mcp_oauth_request",
                    _notify("m", {"serverName": "evil", "oauthUrl": "javascript:alert(1)"}),
                ),
                # No serverName: cannot be correlated with a later init event.
                ("mcp_oauth_request", _notify("m", {"oauthUrl": "https://auth.example/1"})),
                (
                    "mcp_oauth_request",
                    _notify("m", {"serverName": "github", "oauthUrl": "https://auth.example/1"}),
                ),
                # Duplicate for the same server is dropped.
                (
                    "mcp_oauth_request",
                    _notify("m", {"serverName": "github", "oauthUrl": "https://auth.example/1"}),
                ),
                ("mcp_server_initialized", _notify("m", {"serverName": "github"})),
                # Dedupe was cleared by the init, so a later retry surfaces again.
                (
                    "mcp_oauth_request",
                    _notify("m", {"serverName": "github", "oauthUrl": "https://auth.example/2"}),
                ),
                (
                    "mcp_server_init_failure",
                    _notify("m", {"serverName": "github", "error": "token expired"}),
                ),
                ("mcp_server_initialized", _notify("m", {"name": ""})),  # nameless -> no event
                ("complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})),
            ]
        )

        events = [ev async for ev in client._dispatch_events(1, 5.0)]

        assert [ev.kind for ev in events] == [
            EVENT_AGENT_SWITCHED,
            EVENT_MCP_OAUTH_REQUEST,
            EVENT_MCP_SERVER_INITIALIZED,
            EVENT_MCP_OAUTH_REQUEST,
            EVENT_MCP_SERVER_INIT_FAILURE,
            EVENT_COMPLETE,
        ]
        assert events[0].text == "reviewer"
        assert events[1].server_name == "github"
        assert events[1].oauth_url == "https://auth.example/1"
        assert events[3].oauth_url == "https://auth.example/2"
        assert events[4].text == "token expired"
        # The failure banner also clears dedupe so a retry can surface.
        assert "github" not in client._oauth_emitted_servers
        assert events[-1].stop_reason == "end_turn"

    @pytest.mark.asyncio
    async def test_unsafe_oauth_url_never_records_dedupe(self, tmp_path):
        client = _client(tmp_path)
        client._read_new_tool_results_sync = lambda: []
        client._prompt_loop = _fake_loop(
            [
                (
                    "mcp_oauth_request",
                    _notify("m", {"serverName": "srv", "oauthUrl": "data:text/html,x"}),
                ),
                (
                    "mcp_oauth_request",
                    _notify("m", {"serverName": "srv", "oauthUrl": "https://auth.example/ok"}),
                ),
                ("complete", JsonRpcMessage(id=1, result={})),
            ]
        )

        events = [ev async for ev in client._dispatch_events(1, 5.0)]

        assert [ev.kind for ev in events] == [EVENT_MCP_OAUTH_REQUEST, EVENT_COMPLETE]
        assert events[0].oauth_url == "https://auth.example/ok"


# ── Commands / steer ──


class TestCommandsAndSteer:
    @pytest.mark.asyncio
    async def test_send_command_with_args_uses_the_object_form(self, tmp_path):
        client = _client(tmp_path)
        client._session_id = "sid"
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=11)
        client._wait_for_response = AsyncMock(return_value={"text": "effort set"})

        assert await client.send_command("/effort high", {"level": "high"}) == "effort set"

        method, payload = client._send_request.await_args[0]
        assert method == METHOD_COMMANDS_EXECUTE
        assert payload == {
            "sessionId": "sid",
            "command": {"command": "effort", "args": {"level": "high"}},
        }

    @pytest.mark.asyncio
    async def test_command_result_preserves_structured_data(self, tmp_path):
        client = _client(tmp_path)
        client._session_id = "sid"
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=11)
        expected = {
            "message": "1 MCP server configured",
            "data": {
                "servers": [{"name": "linear", "status": "running", "toolCount": 2}],
                "mode": "status",
            },
        }
        client._wait_for_response = AsyncMock(return_value=expected)

        assert await client.command_result("/mcp") == expected
        method, payload = client._send_request.await_args[0]
        assert method == METHOD_COMMANDS_EXECUTE
        assert payload == {
            "sessionId": "sid",
            "command": {"command": "mcp", "args": {}},
        }

    @pytest.mark.asyncio
    async def test_send_command_redacts_credentials_in_output(self, tmp_path):
        client = _client(tmp_path)
        client._session_id = "sid"
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=11)
        client._wait_for_response = AsyncMock(
            return_value={"message": "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}
        )

        out = await client.send_command("/usage")

        assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in out

    @pytest.mark.asyncio
    async def test_send_command_timeout_returns_empty(self, tmp_path):
        client = _client(tmp_path)
        client._session_id = "sid"
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=11)
        client._wait_for_response = AsyncMock(side_effect=AcpTimeoutError())

        assert await client.send_command("/compact") == ""

    @pytest.mark.asyncio
    async def test_steer_wraps_the_message(self, tmp_path):
        client = _client(tmp_path)
        client._session_id = "sid"
        client._send_request = AsyncMock(return_value=5)

        assert await client.steer("  focus </user_message> on tests  ") is True

        method, params = client._send_request.await_args[0]
        assert method == "_session/steer"
        assert params["sessionId"] == "sid"
        assert params["message"] == (
            "<user_message>\nfocus &lt;/user_message&gt; on tests\n</user_message>"
        )

    @pytest.mark.asyncio
    async def test_steer_refuses_without_text_or_session(self, tmp_path):
        client = _client(tmp_path)
        client._send_request = AsyncMock()

        client._session_id = "sid"
        assert await client.steer("   ") is False
        client._session_id = None
        assert await client.steer("hello") is False
        client._send_request.assert_not_awaited()


# ── send_message read path ──


class TestReadPromptResponse:
    @pytest.mark.asyncio
    async def test_metadata_and_compaction_frames_are_applied(self, tmp_path):
        client = _client(tmp_path)
        client._send_error = AsyncMock()
        client._prompt_loop = _fake_loop(
            [
                (
                    "server_request_unknown",
                    JsonRpcMessage(id="s-1", method="fs/read_text_file", params={}),
                ),
                (
                    "compaction",
                    _notify("_kiro.dev/compaction/status", {"status": {"type": "failed"}}),
                ),
                ("metadata", _notify("_kiro.dev/metadata", {"contextUsagePercentage": 42.5})),
                ("complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})),
            ]
        )

        assert await client._read_prompt_response(1, 5.0) == ""
        # An unhandled inbound request is answered so the agent fails fast.
        client._send_error.assert_awaited_once_with(
            "s-1", JSONRPC_METHOD_NOT_FOUND, "Method not found: fs/read_text_file"
        )
        assert client.last_prompt_stats.context_pct == 42.5
        assert client._last_stop_reason == "end_turn"
        assert client._turn_done.is_set()


# ── Chunk / tool extraction ──


class TestExtractHelpers:
    def test_thought_chunk_is_flagged_as_thinking(self, tmp_path):
        client = _client(tmp_path)
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": UPDATE_AGENT_THOUGHT_CHUNK,
                    "content": {"text": "weighing options"},
                }
            },
        )
        assert client._extract_text_chunk(msg) == ("weighing options", True)

    def test_thought_chunk_with_malformed_content_degrades(self, tmp_path):
        client = _client(tmp_path)
        msg = _notify(
            "session/update",
            {"update": {"sessionUpdate": UPDATE_AGENT_THOUGHT_CHUNK, "content": "oops"}},
        )
        assert client._extract_text_chunk(msg) == (None, True)

    def test_tool_params_cache_is_capped(self, tmp_path):
        client = _client(tmp_path)
        client._tool_call_params = {
            f"old-{i}": {"path": "x"} for i in range(acp_client._MAX_CACHED_TOOL_PARAMS + 1)
        }
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": UPDATE_TOOL_CALL,
                    "toolCallId": "new-1",
                    "title": "read",
                    "kind": "read",
                    "rawInput": {"path": "/tmp/x"},
                }
            },
        )

        event = client._extract_tool_event(msg)

        assert event is not None and event.kind == EVENT_TOOL_CALL
        # Wholesale clear, then the fresh entry — bounded, not unbounded growth.
        assert client._tool_call_params == {"new-1": {"path": "/tmp/x"}}

    def test_tool_result_skips_non_dict_raw_output_items(self, tmp_path):
        client = _client(tmp_path)
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "t1",
                    "status": "completed",
                    "rawOutput": {"items": ["junk", None, {"Text": "hello"}]},
                }
            },
        )

        event = client._extract_tool_call_update(msg)

        assert event is not None
        assert event.kind == EVENT_TOOL_RESULT
        assert event.tool_output == "hello"
        assert event.tool_final is True

    def test_refinement_accepts_a_string_raw_input(self, tmp_path):
        client = _client(tmp_path)
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "t2",
                    "rawInput": "ls -la",
                }
            },
        )

        event = client._extract_tool_call_refinement(msg)

        assert event is not None and event.kind == EVENT_TOOL_CALL_UPDATE
        assert event.tool_input == "ls -la"
        assert client._tool_call_inputs["t2"] == "ls -la"

    def test_refinement_falls_back_to_repr_for_unserializable_input(self, tmp_path):
        client = _client(tmp_path)
        sentinel = object()
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "t3",
                    "rawInput": {"probe": sentinel},
                }
            },
        )

        event = client._extract_tool_call_refinement(msg)

        assert event is not None
        assert "probe" in event.tool_input  # str(dict), not a crash


# ── JSONL tool results ──


class TestReadNewToolResults:
    def _write(self, tmp_path, monkeypatch, body: str) -> AcpClient:
        monkeypatch.setattr(acp_client, "kiro_sessions_dir", lambda: tmp_path)
        # newline="\n" matters even though no "\n" is visible on this line: the
        # newlines arrive inside `body`. Without it, Windows writes CRLF here
        # while a later write that DOES pass newline="\n" writes LF, so the two
        # differ in byte length and the reader's saved f.tell() offset resumes at
        # the wrong place -- the second read then returns nothing.
        (tmp_path / "sid.jsonl").write_text(body, encoding="utf-8", newline="\n")
        client = _client(tmp_path)
        client._session_id = "sid"
        return client

    def test_no_session_or_missing_file_yields_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(acp_client, "kiro_sessions_dir", lambda: tmp_path)
        client = _client(tmp_path)
        assert client._read_new_tool_results_sync() == []
        client._session_id = "sid"
        assert client._read_new_tool_results_sync() == []

    def test_parses_json_text_and_skips_malformed_lines(self, tmp_path, monkeypatch):
        lines = [
            json.dumps({"kind": "Other"}),
            "not json at all",
            "",
            json.dumps(
                {
                    "kind": "ToolResults",
                    "data": {
                        "content": [
                            {"kind": "ignored"},
                            {"kind": "toolResult", "data": "not-a-dict"},
                            {
                                "kind": "toolResult",
                                "data": {
                                    "toolUseId": "t-stdout",
                                    "content": [
                                        {"kind": "json", "data": {"stdout": "OUT"}},
                                        {"kind": "text", "data": "TAIL"},
                                        "junk-block",
                                    ],
                                },
                            },
                            {
                                "kind": "toolResult",
                                "data": {
                                    "toolUseId": "t-json",
                                    "content": [{"kind": "json", "data": {"rc": 0}}],
                                },
                            },
                            {
                                "kind": "toolResult",
                                "data": {"toolUseId": "t-empty", "content": []},
                            },
                        ]
                    },
                }
            ),
        ]
        client = self._write(tmp_path, monkeypatch, "\n".join(lines) + "\n")

        results = client._read_new_tool_results_sync()

        assert [(r.tool_call_id, r.tool_output) for r in results] == [
            ("t-stdout", "OUT\nTAIL"),
            ("t-json", json.dumps({"rc": 0}, indent=2)),
        ]
        assert all(r.kind == EVENT_TOOL_RESULT for r in results)
        # A second call sees no new lines.
        assert client._read_new_tool_results_sync() == []

    @staticmethod
    def _result_line(tool_use_id: Any, content: Any) -> str:
        return json.dumps(
            {
                "kind": "ToolResults",
                "data": {
                    "content": [
                        {
                            "kind": "toolResult",
                            "data": {"toolUseId": tool_use_id, "content": content},
                        }
                    ]
                },
            }
        )

    def test_a_value_of_the_wrong_type_costs_only_that_value(self, tmp_path, monkeypatch):
        """The offset is saved before a line is parsed, so a raise here ended
        the scan and the lines behind it waited for the next one."""
        text = [{"kind": "text", "data": "x"}]
        lines = [
            self._result_line("t1", [{"kind": "text", "data": "first"}]),
            "123",
            "null",
            json.dumps({"kind": "ToolResults", "data": [1]}),
            json.dumps({"kind": "ToolResults", "data": {"content": "not-a-list"}}),
            json.dumps({"kind": "ToolResults", "data": {"content": [5, None, "s"]}}),
            self._result_line("t-content", 7),
            self._result_line(["unhashable"], text),
            self._result_line("", text),
            self._result_line(None, text),
            self._result_line("t-stdout", [{"kind": "json", "data": {"stdout": 5}}]),
            self._result_line("t2", [{"kind": "text", "data": "second"}]),
        ]
        body = "\n".join(lines) + "\n"
        client = self._write(tmp_path, monkeypatch, body)

        results = client._read_new_tool_results_sync()

        assert [(r.tool_call_id, r.tool_output) for r in results] == [
            ("t1", "first"),
            ("t2", "second"),
        ]
        assert client._jsonl_pos == len(body.encode("utf-8"))

    def test_an_undecodable_byte_costs_only_its_line(self, tmp_path, monkeypatch):
        """A strict text read raised on the byte before the offset moved, so
        every later scan failed at the same place for the rest of the session."""
        client = self._write(tmp_path, monkeypatch, "")
        first = self._result_line("t1", [{"kind": "text", "data": "first"}]).encode("utf-8")
        later = self._result_line("t2", [{"kind": "text", "data": "second"}]).encode("utf-8")
        body = first + b"\n" + b'{"kind": "x", "s": "\xff"}\n' + later + b"\n"
        (tmp_path / "sid.jsonl").write_bytes(body)

        results = client._read_new_tool_results_sync()

        assert [r.tool_call_id for r in results] == ["t1", "t2"]
        assert client._jsonl_pos == len(body)

    def test_an_over_cap_record_costs_only_itself(self, tmp_path, monkeypatch):
        """The scan reads through the capped record reader: a newline-free or
        huge record is never materialised whole, and the offset still moves
        past it so the lines after it are read on this scan."""
        import functools

        from kiro_crew import jsonl_util

        monkeypatch.setattr(
            acp_client,
            "bounded_raw_records_with_offsets",
            functools.partial(jsonl_util.bounded_raw_records_with_offsets, cap=512),
        )
        first = self._result_line("t1", [{"kind": "text", "data": "first"}])
        later = self._result_line("t2", [{"kind": "text", "data": "second"}])
        body = first + "\n" + "x" * 4096 + "\n" + later + "\n"
        client = self._write(tmp_path, monkeypatch, body)

        results = client._read_new_tool_results_sync()

        assert [r.tool_call_id for r in results] == ["t1", "t2"]
        assert client._jsonl_pos == len(body.encode("utf-8"))

    def test_a_line_whose_handling_raises_costs_only_that_line(self, tmp_path, monkeypatch):
        calls = []

        def _raise_once(value, **kwargs):
            calls.append(value)
            if len(calls) == 1:
                raise RuntimeError("encoder fault")
            return json.dumps(value, **kwargs)

        monkeypatch.setattr(acp_client, "_dumps_degraded", _raise_once)
        lines = [
            self._result_line("t-raises", [{"kind": "json", "data": {"rc": 1}}]),
            self._result_line("t-after", [{"kind": "json", "data": {"rc": 0}}]),
        ]
        client = self._write(tmp_path, monkeypatch, "\n".join(lines) + "\n")

        results = client._read_new_tool_results_sync()

        assert [r.tool_call_id for r in results] == ["t-after"]

    def test_partial_trailing_line_is_left_for_the_next_call(self, tmp_path, monkeypatch):
        complete = json.dumps(
            {
                "kind": "ToolResults",
                "data": {
                    "content": [
                        {
                            "kind": "toolResult",
                            "data": {
                                "toolUseId": "t1",
                                "content": [{"kind": "text", "data": "first"}],
                            },
                        }
                    ]
                },
            }
        )
        path = tmp_path / "sid.jsonl"
        client = self._write(tmp_path, monkeypatch, complete + "\n" + '{"kind": "ToolRes')

        first = client._read_new_tool_results_sync()
        assert [r.tool_call_id for r in first] == ["t1"]
        pos_after_first = client._jsonl_pos

        # Completing the partial line makes it readable without re-reading the first.
        rest = json.dumps(
            {
                "kind": "ToolResults",
                "data": {
                    "content": [
                        {
                            "kind": "toolResult",
                            "data": {
                                "toolUseId": "t2",
                                "content": [{"kind": "text", "data": "second"}],
                            },
                        }
                    ]
                },
            }
        )
        path.write_text(complete + "\n" + rest + "\n", encoding="utf-8", newline="\n")
        second = client._read_new_tool_results_sync()
        assert [r.tool_call_id for r in second] == ["t2"]
        assert client._jsonl_pos > pos_after_first

    def test_non_ascii_output_decodes_as_utf8_under_a_cp1252_locale(self, tmp_path, monkeypatch):
        """kiro-cli writes the transcript as UTF-8, so the reader must decode it as UTF-8.

        A default Windows interpreter resolves a text-mode ``open()`` with no
        ``encoding=`` to the ANSI code page. The wrapper below reproduces that on
        any host: an omitted encoding becomes cp1252. "café — 你好" then decodes
        to mojibake, and "Ё" carries the byte 0x81, which cp1252 leaves undefined,
        so the read would raise, leave the offset in place and drop the later line.
        """
        real_open = open

        def _locale_default_open(file, mode="r", *args, **kwargs):
            if "b" not in mode and not (len(args) >= 2 and args[1]) and not kwargs.get("encoding"):
                kwargs["encoding"] = "cp1252"
            return real_open(file, mode, *args, **kwargs)

        def _result(tool_use_id: str, stdout: str) -> str:
            return json.dumps(
                {
                    "kind": "ToolResults",
                    "data": {
                        "content": [
                            {
                                "kind": "toolResult",
                                "data": {
                                    "toolUseId": tool_use_id,
                                    "content": [{"kind": "json", "data": {"stdout": stdout}}],
                                },
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )

        body = _result("t-accents", "café — 你好") + "\n"
        body += _result("t-undefined-byte", "Ё") + "\n"
        body += _result("t-after", "later") + "\n"
        client = self._write(tmp_path, monkeypatch, body)
        monkeypatch.setattr(acp_client, "open", _locale_default_open, raising=False)

        results = client._read_new_tool_results_sync()

        assert [(r.tool_call_id, r.tool_output) for r in results] == [
            ("t-accents", "café — 你好"),
            ("t-undefined-byte", "Ё"),
            ("t-after", "later"),
        ]
        assert client._jsonl_pos == len(body.encode("utf-8"))

    def test_unreadable_file_is_swallowed(self, tmp_path, monkeypatch):
        client = self._write(tmp_path, monkeypatch, "")

        def _boom(*args, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(acp_client, "open", _boom, raising=False)
        assert client._read_new_tool_results_sync() == []


# ── Permission events ──


class TestPermissionEvent:
    def test_non_string_label_and_kind_are_normalised(self, tmp_path):
        client = _client(tmp_path)
        msg = JsonRpcMessage(
            id="req-1",
            method="session/request_permission",
            params={
                "toolCall": {"title": "rm -rf build", "toolCallId": "tc-1", "kind": "execute"},
                "options": [
                    {"optionId": "allow_once", "name": 7, "kind": ["nope"]},
                    {"optionId": 42, "name": "bad id"},  # non-str id -> skipped
                    "not-a-dict",
                ],
            },
        )

        event = client._build_permission_event(msg)

        assert event.kind == EVENT_PERMISSION_REQUEST
        assert event.options == [{"id": "allow_once", "label": ""}]
        # Legacy id with no usable kind still resolves the allow ids.
        assert client._permission_options["req-1"]["once"] == "allow_once"
        # is_shell is deny-by-default: the payload's own kind is untrusted.
        assert event.is_shell is False

    def test_inline_params_recovered_when_input_came_from_cache(self, tmp_path):
        client = _client(tmp_path)
        client._tool_call_inputs["tc-2"] = '{"path": "/etc/hosts"}'
        msg = JsonRpcMessage(
            id="req-2",
            method="session/request_permission",
            params={
                "toolCall": {
                    "title": "write",
                    "toolCallId": "tc-2",
                    "params": {"path": "/etc/hosts"},
                },
                "options": [{"optionId": "reject", "kind": "reject_once", "name": "Reject"}],
            },
        )

        event = client._build_permission_event(msg)

        assert event.tool_input == '{"path": "/etc/hosts"}'
        assert event.raw_tool_params == {"path": "/etc/hosts"}
        assert client._permission_options["req-2"] == {"reject": "reject"}

    def test_debug_logging_redacts_the_payload(self, tmp_path, caplog):
        client = _client(tmp_path)
        msg = JsonRpcMessage(
            id="req-3",
            method="session/request_permission",
            params={
                "toolCall": {
                    "title": "curl",
                    "toolCallId": "tc-3",
                    "input": {"token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"},
                }
            },
        )

        with caplog.at_level(logging.DEBUG, logger=acp_client.logger.name):
            event = client._build_permission_event(msg)

        debug = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
        assert "Permission toolCall payload" in debug
        assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in debug
        assert event.raw_tool_params == {"token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}


# ── Context window backfill ──


class TestBackfillContextWindow:
    def test_authoritative_usage_counts_are_never_overwritten(self, tmp_path):
        client = _client(tmp_path)
        client.last_prompt_stats.context_tokens_from_usage = True
        client.last_prompt_stats.context_used_tokens = 123

        client._backfill_context_window(90.0)

        assert client.last_prompt_stats.context_used_tokens == 123

    def test_unknown_model_leaves_the_window_alone(self, tmp_path, monkeypatch):
        client = _client(tmp_path, model="mystery-model")
        monkeypatch.setattr(acp_client.model_registry, "has_known_window", lambda m: False)

        client._backfill_context_window(50.0)

        assert client.last_prompt_stats.context_window_tokens == 0
        assert client.last_prompt_stats.context_used_tokens == 0

    def test_zero_registry_window_is_refused(self, tmp_path, monkeypatch):
        client = _client(tmp_path, model="weird-model")
        monkeypatch.setattr(acp_client.model_registry, "has_known_window", lambda m: True)
        monkeypatch.setattr(acp_client.model_registry, "model_window", lambda m: 0)

        client._backfill_context_window(50.0)

        assert client.last_prompt_stats.context_window_tokens == 0

    def test_derives_used_tokens_from_a_known_window(self, tmp_path, monkeypatch):
        client = _client(tmp_path, model="known-model")
        monkeypatch.setattr(acp_client.model_registry, "has_known_window", lambda m: True)
        monkeypatch.setattr(acp_client.model_registry, "model_window", lambda m: 200_000)

        client._backfill_context_window(float("nan"))
        assert client.last_prompt_stats.context_used_tokens == 0  # NaN -> 0

        client._backfill_context_window(25.0)
        assert client.last_prompt_stats.context_window_tokens == 200_000
        assert client.last_prompt_stats.context_used_tokens == 50_000


# ── Post-compaction metadata drain ──


class TestDrainPostCompactionMetadata:
    def _reader(self, client, frames):
        queue = list(frames)

        async def _read_message(timeout=0.0):
            if not queue:
                return None
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        client._read_message = _read_message

    @pytest.mark.asyncio
    async def test_returns_on_the_first_real_percentage(self, tmp_path):
        client = _client(tmp_path)
        notification = _notify("session/update", {"update": {}})
        self._reader(
            client,
            [
                None,  # silent read
                _notify("_kiro.dev/metadata", {"meteringUsage": []}),  # no pct -> keep draining
                notification,  # buffered for the main loop
                _notify("_kiro.dev/metadata", {"contextUsagePercentage": 12.0}),
            ],
        )

        await client._drain_post_compaction_metadata(grace=5.0)

        assert client.last_prompt_stats.context_pct == 12.0
        assert client._mcp_notifications == [notification]

    @pytest.mark.asyncio
    async def test_process_death_propagates(self, tmp_path):
        client = _client(tmp_path)
        self._reader(client, [AcpError("ACP process exited (code=1)")])

        with pytest.raises(AcpError):
            await client._drain_post_compaction_metadata(grace=5.0)

    @pytest.mark.asyncio
    async def test_other_read_failures_end_the_drain_quietly(self, tmp_path):
        client = _client(tmp_path)
        self._reader(client, [RuntimeError("reader wedged")])

        await client._drain_post_compaction_metadata(grace=5.0)

        assert client.last_prompt_stats.context_pct == 0.0
