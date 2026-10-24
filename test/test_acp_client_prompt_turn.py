"""Tests for ACP client: the prompt turn: stop reason, turn-done, the prompt loop and event
dispatch.
"""

import asyncio
import json
import time
from collections import deque
from contextlib import aclosing
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

from kiro_crew.acp.client import AcpClient, AcpError, AcpProcessDied
from kiro_crew.acp.types import ACP_BACKEND_OPENCODE, AcpPromptStats

# ── Phase 1: stop_reason and turn_done tests ──


class TestStopReasonPopulated:
    """Tests for stop_reason extraction from prompt response."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            pytest.param(
                {"stopReason": "cancelled"}, "cancelled", id="stop_reason_populated_on_complete"
            ),
            pytest.param(
                {"stopReason": "end_turn"}, "end_turn", id="stop_reason_populated_end_turn"
            ),
            # A prompt response without the stopReason key yields an empty one.
            pytest.param({"status": "ok"}, "", id="stop_reason_empty_when_absent"),
        ],
    )
    async def test_stop_reason_from_the_prompt_response(self, result, expected):
        from kiro_crew.acp.types import EVENT_COMPLETE, JsonRpcMessage

        client = AcpClient()
        complete_msg = JsonRpcMessage(id=1, result=result)

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 1
        assert events[0].kind == EVENT_COMPLETE
        assert events[0].stop_reason == expected


class TestWaitTurnDone:
    """Tests for wait_turn_done and has_active_turn."""

    @pytest.mark.asyncio
    async def test_wait_turn_done_returns_reason(self):
        """wait_turn_done returns the stop_reason after turn completes."""
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "cancelled"})

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        # Consume stream_events to trigger turn_done
        async for _ in client.stream_events("test"):
            pass

        reason = await client.wait_turn_done(timeout=1.0)
        assert reason == "cancelled"

    @pytest.mark.asyncio
    async def test_wait_turn_done_times_out(self):
        """wait_turn_done raises TimeoutError when no complete fires."""
        client = AcpClient()
        client._turn_done.clear()

        with pytest.raises(asyncio.TimeoutError):
            await client.wait_turn_done(timeout=0.05)


class TestHasActiveTurn:
    """Tests for has_active_turn() across its three conditions."""

    def test_has_active_turn_states(self):
        client = AcpClient()
        # Set up happy state: not cancelled, turn not done, process alive
        client._cancelled = False
        client._turn_done.clear()
        client._process = MagicMock()
        client._process.returncode = None
        assert client.has_active_turn() is True

        # Condition 1: process dies
        client._process.returncode = 1
        assert client.has_active_turn() is False
        client._process.returncode = None  # reset

        # Condition 2: cancelled flag set
        client._cancelled = True
        assert client.has_active_turn() is False
        client._cancelled = False  # reset

        # Condition 3: turn_done is set
        client._turn_done.set()
        assert client.has_active_turn() is False
        client._turn_done.clear()  # reset

        assert client.has_active_turn() is True


class TestCancelledGraceWindow:
    """Tests for the _cancelled grace window in _read_message."""

    @pytest.mark.asyncio
    async def test_cancelled_flag_does_not_short_circuit_reads(self):
        """_read_message reads a queued message within the grace window."""
        client = AcpClient()
        msg_data = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=json.dumps(msg_data).encode() + b"\n")
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        # Set cancelled with recent timestamp (within grace window)
        client._cancelled = True
        client._cancel_ts = time.monotonic()

        msg = await client._read_message(timeout=1.0)
        assert msg is not None
        assert msg.is_response_for(1)
        assert client._cancelled is True

    @pytest.mark.asyncio
    async def test_cancelled_flag_enforces_grace_window(self):
        """_read_message raises AcpError when grace window is exceeded."""
        client = AcpClient()

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"")
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        # Set cancelled with timestamp > 10s ago
        client._cancelled = True
        client._cancel_ts = time.monotonic() - 11.0

        with pytest.raises(AcpError, match="grace window exceeded"):
            await client._read_message(timeout=1.0)


# ── Coverage push: prompt loop ──


class TestPromptLoop:
    """Tests for _prompt_loop covering normal flow, process death, and staleness."""

    @pytest.mark.asyncio
    async def test_yields_actions(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msgs = [
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}}
                },
            ),
            JsonRpcMessage(id=1, result={"status": "ok"}),
        ]
        idx = [0]

        async def fake_read(timeout=20.0):
            if idx[0] < len(msgs):
                m = msgs[idx[0]]
                idx[0] += 1
                return m
            return None

        client._read_message = fake_read

        actions = []
        async for action, msg in client._prompt_loop(req_id=1, timeout=5.0):
            actions.append(action)
            if action == "complete":
                break

        assert "update" in actions
        assert "complete" in actions

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "scenario,command_expected",
        [
            ("matching", True),
            ("wrong_call", False),
            ("wrong_session", False),
            ("missing_frame", False),
            ("inline_only", False),
            ("output", False),
            ("other_backend", False),
        ],
    )
    async def test_opencode_permission_waits_for_matching_command_refinement(
        self, tmp_path, scenario, command_expected
    ):
        from kiro_crew.acp.types import ACP_BACKEND_KIRO, EVENT_PERMISSION_REQUEST, JsonRpcMessage

        client = AcpClient(
            work_dir=tmp_path,
            acp_backend=ACP_BACKEND_KIRO if scenario == "other_backend" else ACP_BACKEND_OPENCODE,
        )
        client._session_id = "ses1"
        command = "gh --version"
        frames = deque(
            [
                JsonRpcMessage(
                    method="session/update",
                    params={
                        "sessionId": "ses1",
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "tc1",
                            "title": "bash",
                            "kind": "execute",
                            "status": "pending",
                            "rawInput": {"cwd": str(tmp_path)},
                        },
                    },
                ),
                JsonRpcMessage(
                    id=99,
                    method="session/request_permission",
                    params={
                        "sessionId": "ses1",
                        "toolCall": {
                            "toolCallId": "tc1",
                            "title": command,
                            "kind": "execute",
                            "rawInput": {"command": command},
                        },
                        "options": [{"optionId": "reject", "kind": "reject_once"}],
                    },
                ),
                JsonRpcMessage(
                    method="session/update",
                    params={
                        "sessionId": "ses2" if scenario == "wrong_session" else "ses1",
                        "update": {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "tc2" if scenario == "wrong_call" else "tc1",
                            "title": command,
                            "kind": "execute",
                            "status": "in_progress",
                            "rawInput": {"command": command, "cwd": str(tmp_path)},
                        },
                    },
                ),
                JsonRpcMessage(id=1, result={"stopReason": "end_turn"}),
            ]
        )

        if scenario == "missing_frame":
            frames[2] = None
        elif scenario == "inline_only":
            frames[2].params["update"]["rawInput"] = {"cwd": str(tmp_path)}
        elif scenario == "output":
            frames[2].params["update"]["rawOutput"] = "already ran"

        async def fake_read(timeout=20.0):
            if client._buffer:
                return client._buffer.popleft()
            return frames.popleft() if frames else None

        client._read_message = fake_read
        client._read_new_tool_results = AsyncMock(return_value=[])
        events = []
        async with aclosing(client._dispatch_events(req_id=1, timeout=5.0)) as stream:
            async for event in stream:
                events.append(event)

        permission = next(e for e in events if e.kind == EVENT_PERMISSION_REQUEST)
        assert bool(permission.shell_command) is command_expected
        if command_expected:
            assert permission.shell_command == command
            assert permission.raw_params_trusted
            assert events.index(
                next(e for e in events if e.kind == "tool_call_update")
            ) < events.index(permission)
        else:
            # A different call cannot supply command bytes for this request.
            assert permission.raw_tool_params == {"cwd": str(tmp_path)}

    @pytest.mark.asyncio
    async def test_process_death_raises(self, tmp_path):
        from kiro_crew.acp.client import AcpProcessDied

        client = AcpClient(work_dir=tmp_path)
        proc = MagicMock()
        proc.returncode = 1
        client._process = proc
        client._read_message = AsyncMock(return_value=None)

        with pytest.raises(AcpProcessDied):
            async for _ in client._prompt_loop(req_id=1, timeout=5.0):
                pass

    @pytest.mark.asyncio
    async def test_stale_turn_exits_early(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._stale_eligible = True
        proc = MagicMock()
        proc.returncode = None
        client._process = proc
        client._read_message = AsyncMock(return_value=None)

        # With stale_eligible=True and no data, should exit after _STALE_TURN_TIMEOUT
        # We patch the timeout to be very short
        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.05):
            actions = []
            async for action, msg in client._prompt_loop(req_id=1, timeout=2.0):
                actions.append(action)

        # Should exit cleanly (return, not raise)
        assert actions == []


class TestDispatchEventsExtended:
    """Extended tests for _dispatch_events covering permission, tool events, compaction."""

    @pytest.mark.asyncio
    async def test_permission_event_yielded(self):
        from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, JsonRpcMessage

        client = AcpClient()
        perm_msg = JsonRpcMessage(
            id=99,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell", "toolCallId": "tc1"},
                "options": [{"id": "allow_once", "label": "Allow"}],
            },
        )
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

        async def fake_prompt_loop(req_id, timeout):
            yield "permission", perm_msg
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop
        client.last_prompt_stats = AcpPromptStats()
        client._tool_call_inputs = {}
        client._stale_eligible = False
        client._turn_done = asyncio.Event()
        client._last_stop_reason = ""

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=5.0):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert EVENT_PERMISSION_REQUEST in kinds
        assert EVENT_COMPLETE in kinds

    @pytest.mark.asyncio
    async def test_tool_event_yielded(self):
        from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TOOL_CALL, JsonRpcMessage

        client = AcpClient()
        tool_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "Read",
                    "kind": "tool_use",
                    "toolCallId": "tc1",
                    "input": {"path": "/tmp/x"},
                }
            },
        )
        complete_msg = JsonRpcMessage(id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "update", tool_msg
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop
        client.last_prompt_stats = AcpPromptStats()
        client._tool_call_inputs = {}
        client._stale_eligible = False
        client._turn_done = asyncio.Event()
        client._last_stop_reason = ""
        client._read_new_tool_results_sync = lambda: []

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=5.0):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert EVENT_TOOL_CALL in kinds
        assert EVENT_COMPLETE in kinds

    @pytest.mark.asyncio
    async def test_extract_agent_from_result(self):
        """extract_agent_from_result=True yields agent_switched from result data."""
        from kiro_crew.acp.types import EVENT_AGENT_SWITCHED, EVENT_COMPLETE, JsonRpcMessage

        client = AcpClient()
        complete_msg = JsonRpcMessage(
            id=1, result={"data": {"agent": {"name": "planner"}}, "message": ""}
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop
        client.last_prompt_stats = AcpPromptStats()
        client._tool_call_inputs = {}
        client._stale_eligible = False
        client._turn_done = asyncio.Event()
        client._last_stop_reason = ""
        client._read_new_tool_results_sync = lambda: []

        events = []
        async for ev in client._dispatch_events(
            req_id=1, timeout=5.0, extract_agent_from_result=True
        ):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert EVENT_AGENT_SWITCHED in kinds
        assert EVENT_COMPLETE in kinds

    @pytest.mark.asyncio
    async def test_error_action_raises(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        error_msg = JsonRpcMessage(id=1, error={"code": -1, "message": "boom"})

        async def fake_prompt_loop(req_id, timeout):
            yield "error", error_msg

        client._prompt_loop = fake_prompt_loop
        client.last_prompt_stats = AcpPromptStats()
        client._tool_call_inputs = {}
        client._stale_eligible = False
        client._turn_done = asyncio.Event()
        client._last_stop_reason = ""

        with pytest.raises(AcpError, match="boom"):
            async for _ in client._dispatch_events(req_id=1, timeout=5.0):
                pass


class TestReadPromptResponse:
    """Tests for _read_prompt_response covering text accumulation and timeout."""

    @pytest.mark.asyncio
    async def test_accumulates_text(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        text_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hello "}}
            },
        )
        text_msg2 = JsonRpcMessage(
            method="session/update",
            params={
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "world"}}
            },
        )
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text_msg
            yield "update", text_msg2
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop

        result = await client._read_prompt_response(req_id=1, timeout=5.0)
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        from kiro_crew.acp.client import AcpTimeoutError

        client = AcpClient()

        async def fake_prompt_loop(req_id, timeout):
            # yields nothing — simulates timeout
            return
            yield  # make it an async generator

        client._prompt_loop = fake_prompt_loop

        with pytest.raises(AcpTimeoutError):
            await client._read_prompt_response(req_id=1, timeout=5.0)

    @pytest.mark.asyncio
    async def test_error_raises(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        error_msg = JsonRpcMessage(id=1, error={"code": -1, "message": "fail"})

        async def fake_prompt_loop(req_id, timeout):
            yield "error", error_msg

        client._prompt_loop = fake_prompt_loop

        with pytest.raises(AcpError, match="fail"):
            await client._read_prompt_response(req_id=1, timeout=5.0)


class TestPromptLoopReleasesTurnDone:
    """The core loop must release _turn_done on every exit so a cooperative
    cancel waiter (wait_turn_done) is not left blocking the full budget."""

    @pytest.mark.asyncio
    async def test_process_death_releases_turn_done(self, tmp_path):
        # An exception raised INSIDE the loop (process death) must still set
        # _turn_done via the finally, so a concurrent wait_turn_done returns
        # promptly instead of blocking its whole timeout.
        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._read_message = AsyncMock(side_effect=AcpProcessDied("boom"))
        client._is_process_alive = lambda: False

        with pytest.raises(AcpProcessDied):
            async for _ in client._prompt_loop(req_id=1, timeout=5.0):
                pass

        assert client._turn_done.is_set()
        # wait_turn_done resolves immediately, not after the budget.
        reason = await client.wait_turn_done(timeout=5.0)
        assert reason == ""  # no clean stop reason → caller escalates correctly

    @pytest.mark.asyncio
    async def test_cancel_grace_exceeded_releases_turn_done(self, tmp_path):
        # The grace-window AcpError (cancel ack never arrived) must also release
        # the waiter rather than bypassing it.
        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._cancelled = True
        client._cancel_ts = time.monotonic() - 1000.0  # well past any grace
        proc = MagicMock()
        proc.returncode = None
        proc.stdout = MagicMock()
        client._process = proc

        with pytest.raises(AcpError, match="grace window"):
            async for _ in client._prompt_loop(req_id=1, timeout=5.0):
                pass

        assert client._turn_done.is_set()

    @pytest.mark.asyncio
    async def test_permission_auto_approved(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        perm_msg = JsonRpcMessage(
            id=99,
            method="session/requestPermission",
            params={"toolCall": {"title": "shell"}, "options": []},
        )
        complete_msg = JsonRpcMessage(id=1, result={})
        client.approve_tool = AsyncMock()

        async def fake_prompt_loop(req_id, timeout):
            yield "permission", perm_msg
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop

        result = await client._read_prompt_response(req_id=1, timeout=5.0)
        client.approve_tool.assert_awaited_once()
        assert result == ""


class TestSendMessageStreamBranches:
    """Tests for send_message_stream covering metadata and compaction branches."""

    @pytest.mark.asyncio
    async def test_metadata_tracked(self):
        from kiro_crew.acp.types import METHOD_METADATA, JsonRpcMessage

        client = AcpClient()
        meta_msg = JsonRpcMessage(method=METHOD_METADATA, params={"contextUsagePercentage": 75.0})
        complete_msg = JsonRpcMessage(id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "metadata", meta_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        chunks = []
        async for c in client.send_message_stream("test"):
            chunks.append(c)

        assert client.last_prompt_stats.context_pct == 75.0

    @pytest.mark.asyncio
    async def test_compaction_logged(self):
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        client = AcpClient()
        compact_msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "in_progress"}},
        )
        complete_msg = JsonRpcMessage(id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "compaction", compact_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        chunks = []
        async for c in client.send_message_stream("test"):
            chunks.append(c)

        # No text chunks expected, just verifying no crash
        assert chunks == []

    @pytest.mark.asyncio
    async def test_timeout_sets_turn_done(self):
        """When prompt loop ends without complete, turn_done is set."""

        client = AcpClient()

        async def fake_prompt_loop(req_id, timeout):
            # Empty generator — simulates timeout
            return
            yield

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        chunks = []
        async for c in client.send_message_stream("test"):
            chunks.append(c)

        assert client._turn_done.is_set()


class TestDispatchSubagentEvents:
    """_dispatch_events must yield EVENT_SUBAGENT_LIST and EVENT_SUBAGENT_ACTIVITY
    for kiro-cli's _kiro.dev/subagent/list_update and _kiro.dev/session/update
    notifications."""

    @pytest.mark.asyncio
    async def test_dispatch_yields_subagent_list_and_activity(self):
        from kiro_crew.acp.types import (
            EVENT_SUBAGENT_ACTIVITY,
            EVENT_SUBAGENT_LIST,
            JsonRpcMessage,
        )

        client = AcpClient()

        subs = [{"sessionId": "s1", "role": "explorer", "status": {"type": "working"}}]
        frames = [
            ("subagent_list", JsonRpcMessage(params={"subagents": subs})),
            # tool_call_chunk → toolCallId attribution
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "tool_call_chunk",
                            "toolCallId": "tc-1",
                            "title": "read",
                        },
                    }
                ),
            ),
            # agent_message_chunk → sub-agent text
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "text": "hello from sub",
                        },
                    }
                ),
            ),
            # non-dict subagents payload must be ignored (no yield)
            ("subagent_list", JsonRpcMessage(params={"subagents": "nope"})),
            ("complete", JsonRpcMessage(result={"stopReason": "end_turn"})),
        ]

        async def _fake_loop(req_id, timeout):
            for f in frames:
                yield f

        client._prompt_loop = _fake_loop  # type: ignore[assignment]

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=1.0):
            events.append(ev)

        lists = [e for e in events if e.kind == EVENT_SUBAGENT_LIST]
        acts = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        assert len(lists) == 1
        assert lists[0].subagents == subs
        # one tool-call activity (with toolCallId) + one text activity
        tc = [a for a in acts if a.tool_call_id == "tc-1"]
        txt = [a for a in acts if a.text == "hello from sub"]
        assert len(tc) == 1 and tc[0].sub_session_id == "s1" and tc[0].title == "read"
        assert len(txt) == 1 and txt[0].sub_session_id == "s1"

    @pytest.mark.asyncio
    async def test_dispatch_ignores_activity_naming_this_session(self):
        """A frame naming THIS session is the parent's own stream, not a child's.

        kiro-cli carries the parent turn's own ``tool_call_chunk`` on
        ``_kiro.dev/session/update`` with ``params.sessionId`` set to the
        parent's session, the same method a child's update arrives on, so the
        sessionId is the only separator. Recorded live in
        ``test/fixtures/acp_frames/kiro/session.jsonl``. Both carriers are
        checked so one guard cannot cover half the shape.
        """
        from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, JsonRpcMessage

        client = AcpClient()
        client._session_id = "own-1"

        frames = [
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "own-1",
                        "update": {
                            "sessionUpdate": "tool_call_chunk",
                            "toolCallId": "tc-own",
                            "title": "shell",
                        },
                    }
                ),
            ),
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "own-1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "text": "the parent's own streamed text",
                        },
                    }
                ),
            ),
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "child-1",
                        "update": {
                            "sessionUpdate": "tool_call_chunk",
                            "toolCallId": "tc-child",
                            "title": "read",
                        },
                    }
                ),
            ),
            ("complete", JsonRpcMessage(result={"stopReason": "end_turn"})),
        ]

        async def _fake_loop(req_id, timeout):
            for f in frames:
                yield f

        client._prompt_loop = _fake_loop  # type: ignore[assignment]

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=1.0):
            events.append(ev)

        acts = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        assert [a.sub_session_id for a in acts] == ["child-1"]
        assert acts[0].tool_call_id == "tc-child"

    @pytest.mark.asyncio
    async def test_a_long_roster_leaves_the_client_remembering_no_child_id(self):
        """``AcpClient`` is a PASSTHROUGH for the native-child roster: it yields
        the full list and retains nothing.

        The roster cap (``NATIVE_CHILD_ROSTER_CAP``) binds every store that
        REMEMBERS child ids -- the parent handle's counted set, its KAS display
        roster, and ``AcpRuntime``'s routing recognition set. This asserts the
        client is not a fourth such store, so the class is closed at three: a
        store added here would need the same cap, the same overflow count and
        the same answer for a child past it, and one that arrives without them
        is the one-of-N shape that makes a cap decision partial.
        """
        from kiro_crew.acp.types import EVENT_SUBAGENT_LIST, JsonRpcMessage

        client = AcpClient()
        roster = [{"sessionId": f"c-{i}"} for i in range(5000)]
        frames = [
            ("subagent_list", JsonRpcMessage(params={"subagents": roster})),
            ("complete", JsonRpcMessage(result={"stopReason": "end_turn"})),
        ]

        async def _fake_loop(req_id, timeout):
            for f in frames:
                yield f

        client._prompt_loop = _fake_loop  # type: ignore[assignment]
        events = [ev async for ev in client._dispatch_events(req_id=1, timeout=1.0)]

        lists = [e for e in events if e.kind == EVENT_SUBAGENT_LIST]
        assert len(lists) == 1 and lists[0].subagents == roster
        probes = {"c-0", "c-2500", "c-4999"}
        for name, value in vars(client).items():
            if isinstance(value, (set, frozenset)):
                assert not probes & set(value), name
            elif isinstance(value, dict):
                assert not probes & {str(k) for k in value}, name
            elif isinstance(value, (list, tuple)):
                assert not probes & {str(v) for v in value}, name

    @pytest.mark.asyncio
    async def test_dispatch_redacts_and_extracts_subagent_output(self):
        """Sub-agent titles/text are LLM-influenced, so credentials + exfil URLs
        must be scrubbed before they surface as EVENT_SUBAGENT_ACTIVITY. Also
        the streamed text must be read from the nested ``content.text`` shape
        kiro-cli 2.10.0 emits, not only the flat top-level ``text`` field."""
        from kiro_crew.acp.types import (
            EVENT_SUBAGENT_ACTIVITY,
            JsonRpcMessage,
        )

        client = AcpClient()

        frames = [
            # tool_call_chunk with a credential embedded in the title
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "tool_call_chunk",
                            "toolCallId": "tc-1",
                            "title": "leak AKIAIOSFODNN7EXAMPLE now",
                        },
                    }
                ),
            ),
            # agent_message_chunk with the text under nested content.text (2.10.0)
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"text": "secret AKIAIOSFODNN7EXAMPLE here"},
                        },
                    }
                ),
            ),
            ("complete", JsonRpcMessage(result={"stopReason": "end_turn"})),
        ]

        async def _fake_loop(req_id, timeout):
            for f in frames:
                yield f

        client._prompt_loop = _fake_loop  # type: ignore[assignment]

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=1.0):
            events.append(ev)

        acts = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        tc = [a for a in acts if a.tool_call_id == "tc-1"]
        txt = [a for a in acts if a.text]
        # Title redacted (credential scrubbed, no raw AKIA leaks through).
        assert len(tc) == 1
        assert "AKIAIOSFODNN7EXAMPLE" not in tc[0].title
        assert "[REDACTED" in tc[0].title
        # Nested content.text was extracted (robustness) AND redacted (security).
        assert len(txt) == 1 and txt[0].sub_session_id == "s1"
        assert "AKIAIOSFODNN7EXAMPLE" not in txt[0].text
        assert "[REDACTED" in txt[0].text

    @pytest.mark.asyncio
    async def test_dispatch_subagent_text_edge_cases(self):
        """Regression guards for the nested-vs-flat content.text extraction:
        (1) an EMPTY nested content.text must not shadow a populated flat
        top-level ``text`` (older payloads); (2) a reasoning/thinking content
        block must NOT surface as user-visible sub-agent output; (3) a non-dict
        ``content`` must not raise (the flat pre-port read tolerated it)."""
        from kiro_crew.acp.types import (
            EVENT_SUBAGENT_ACTIVITY,
            JsonRpcMessage,
        )

        client = AcpClient()

        frames = [
            # (1) empty nested content.text + populated flat text -> flat wins
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"text": ""},
                            "text": "flat fallback output",
                        },
                    }
                ),
            ),
            # (2) reasoning/thinking content block -> suppressed (no user text)
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "thinking", "text": "internal reasoning"},
                        },
                    }
                ),
            ),
            # (3) non-dict content -> must not crash; flat text used if present
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": ["a", "list", "block"],
                            "text": "flat when content is a list",
                        },
                    }
                ),
            ),
            ("complete", JsonRpcMessage(result={"stopReason": "end_turn"})),
        ]

        async def _fake_loop(req_id, timeout):
            for f in frames:
                yield f

        client._prompt_loop = _fake_loop  # type: ignore[assignment]

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=1.0):
            events.append(ev)

        txt = [e.text for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY and e.text]
        # (1) flat fallback surfaced when nested content.text is empty.
        assert "flat fallback output" in txt
        # (2) the reasoning block was never surfaced as sub-agent output.
        assert "internal reasoning" not in txt
        # (3) non-dict content did not crash and the flat text still surfaced.
        assert "flat when content is a list" in txt
