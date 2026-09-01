"""Full-stack ACP server: real transport + server + gateway handler composed.

The unit suites each stub one side of a seam — `test_acp_server_protocol` stubs
the prompt handler, `test_acp_server_gateway` stubs the sink. Nothing there proves
the three layers compose. This drives the whole stack from raw JSON-RPC frames,
stubbing only the LLM provider (AGENTS.md forbids spawning real processes).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    OPTION_ALLOW_ONCE,
    OPTION_REJECT_ONCE,
    OUTCOME_SELECTED,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.acp_server.gateway import make_prompt_handler
from kiro_crew.acp_server.server import AcpAgentServer
from kiro_crew.acp_server.transport import AgentTransport
from kiro_crew.hooks import HookResult, ToolHookResult


class _Writer:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self._buf = b""

    def write(self, data: bytes) -> None:
        self._buf += data
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.strip():
                self.frames.append(json.loads(line))

    async def drain(self) -> None:
        return None


class _Provider:
    def __init__(self, events: list[AcpEvent]) -> None:
        self.events = events
        self.approved: list[Any] = []
        self.rejected: list[Any] = []

    async def stream(self, _message: str) -> Any:
        for event in self.events:
            yield event

    async def approve_tool(self, request_id: Any, **_kw: Any) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: Any) -> None:
        self.rejected.append(request_id)


class _Sessions:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider
        self.released: list[str] = []

    async def get_or_create(self, _key: str, **_kw: Any) -> Any:
        return self.provider, True, False

    def release(self, key: str, cleanup: bool = False) -> None:
        self.released.append(key)


class _Hooks:
    def __init__(self, result: ToolHookResult | None = None) -> None:
        self.result = result or ToolHookResult.allow()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def on_tool_call(self, name: str, **kwargs: Any) -> Any:
        self.calls.append((name, kwargs))
        return self.result


class _Ctx:
    def __init__(self, hooks: _Hooks | None = None) -> None:
        self.hooks = hooks or _Hooks()

    def build_message(self, text: str, _is_new: bool, _key: str, **_kw: Any) -> Any:
        return text, HookResult.passthrough()


class _Svc:
    def __init__(self, sessions: _Sessions, ctx: _Ctx) -> None:
        self.sessions = sessions
        self.context_builder = ctx


class _Stack:
    """Real transport + server + gateway handler over in-memory pipes."""

    def __init__(self, provider: _Provider, hooks: _Hooks | None = None) -> None:
        self.reader = asyncio.StreamReader()
        self.writer = _Writer()
        self.sessions = _Sessions(provider)
        self.services = _Svc(self.sessions, _Ctx(hooks))
        self.transport = AgentTransport(self.reader, self.writer)
        self.server = AcpAgentServer(self.transport, make_prompt_handler(self.services))
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.server.serve())

    def send(self, payload: dict[str, Any]) -> None:
        self.reader.feed_data((json.dumps(payload) + "\n").encode("utf-8"))

    async def stop(self) -> None:
        self.reader.feed_eof()
        if self.task is not None:
            await asyncio.wait_for(self.task, timeout=5)

    async def wait_for(self, pred: Any, timeout: float = 3.0) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            for frame in self.writer.frames:
                if pred(frame):
                    return frame
            await asyncio.sleep(0.01)
        raise AssertionError(f"no frame matched: {self.writer.frames}")

    async def handshake(self) -> str:
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        await self.wait_for(lambda f: f.get("id") == 1 and "result" in f)
        self.send({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/tmp"}})
        frame = await self.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        return str(frame["result"]["sessionId"])

    def prompt(self, session_id: str, text: str, req_id: int = 3) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            }
        )


def _edit_events() -> list[AcpEvent]:
    return [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="patching now"),
        AcpEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=99,
            tool_call_id="tc1",
            title="edit main.py",
            tool_kind="edit",
            raw_tool_params={"path": "/repo/main.py", "oldStr": "old", "newStr": "new"},
        ),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
    ]


class TestFullStackDiffReview:
    """The end-to-end path this feature exists for: edit -> inline diff -> accept."""

    @pytest.mark.asyncio
    async def test_accept_flows_through_to_provider(self) -> None:
        provider = _Provider(_edit_events())
        stack = _Stack(provider)
        await stack.start()
        sid = await stack.handshake()
        stack.prompt(sid, "fix the bug")

        text = await stack.wait_for(
            lambda f: f.get("method") == "session/update"
            and f["params"]["update"].get("sessionUpdate") == "agent_message_chunk"
        )
        assert text["params"]["update"]["content"]["text"] == "patching now"
        assert text["params"]["sessionId"] == sid

        perm = await stack.wait_for(lambda f: f.get("method") == "session/request_permission")
        block = perm["params"]["toolCall"]["content"][0]
        assert block["type"] == "diff"
        assert block["path"] == "/repo/main.py"
        assert block["newText"] == "new"
        assert [o["optionId"] for o in perm["params"]["options"]] == [
            OPTION_ALLOW_ONCE,
            OPTION_REJECT_ONCE,
        ]

        hook_name, hook_context = stack.services.context_builder.hooks.calls[0]
        assert hook_name == "edit main.py"
        assert hook_context == {
            "session_key": f"acp:{sid}",
            "agent": "",
            "tool_kind": "edit",
            "raw_params": {"path": "/repo/main.py", "oldStr": "old", "newStr": "new"},
            "command": None,
            "is_shell": False,
            "mcp_server_name": "",
            "mcp_tool_name": "",
            "resolved_agent": "",
        }
        stack.send(
            {
                "jsonrpc": "2.0",
                "id": perm["id"],
                "result": {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_ALLOW_ONCE}},
            }
        )
        done = await stack.wait_for(lambda f: f.get("id") == 3 and "result" in f)
        assert done["result"]["stopReason"] == STOP_REASON_END_TURN
        assert provider.approved == [99]
        assert stack.sessions.released == [f"acp:{sid}"]
        await stack.stop()

    @pytest.mark.asyncio
    async def test_reject_flows_through_to_provider(self) -> None:
        provider = _Provider(_edit_events())
        stack = _Stack(provider)
        await stack.start()
        sid = await stack.handshake()
        stack.prompt(sid, "fix the bug")
        perm = await stack.wait_for(lambda f: f.get("method") == "session/request_permission")
        stack.send(
            {
                "jsonrpc": "2.0",
                "id": perm["id"],
                "result": {
                    "outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_REJECT_ONCE}
                },
            }
        )
        await stack.wait_for(lambda f: f.get("id") == 3 and "result" in f)
        assert provider.rejected == [99]
        assert provider.approved == []
        await stack.stop()

    @pytest.mark.asyncio
    async def test_hook_deny_emits_failed_card_and_no_prompt(self) -> None:
        provider = _Provider(_edit_events())
        stack = _Stack(provider, hooks=_Hooks(ToolHookResult.deny("blocked by policy")))
        await stack.start()
        sid = await stack.handshake()
        stack.prompt(sid, "fix the bug")
        done = await stack.wait_for(lambda f: f.get("id") == 3 and "result" in f)
        assert done["result"]["stopReason"] == STOP_REASON_END_TURN
        assert provider.rejected == [99]
        # No permission request may ever have been emitted.
        assert not any(f.get("method") == "session/request_permission" for f in stack.writer.frames)
        failed = [
            f
            for f in stack.writer.frames
            if f.get("method") == "session/update"
            and f["params"]["update"].get("status") == "failed"
        ]
        assert failed, "editor was not told the tool was denied"
        await stack.stop()

    @pytest.mark.asyncio
    async def test_second_turn_reuses_session(self) -> None:
        provider = _Provider(
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"), AcpEvent(kind=EVENT_COMPLETE)]
        )
        stack = _Stack(provider)
        await stack.start()
        sid = await stack.handshake()
        stack.prompt(sid, "one", req_id=3)
        await stack.wait_for(lambda f: f.get("id") == 3 and "result" in f)
        stack.prompt(sid, "two", req_id=4)
        await stack.wait_for(lambda f: f.get("id") == 4 and "result" in f)
        # Same key both turns, and the semaphore released each time.
        assert stack.sessions.released == [f"acp:{sid}", f"acp:{sid}"]
        await stack.stop()
