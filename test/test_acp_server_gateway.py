"""Gateway bridge: session discipline, hooks-first tool gating, redaction."""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.acp_server import gateway
from kiro_crew.acp_server.gateway import diff_content, make_prompt_handler
from kiro_crew.acp_server.server import PromptRequest
from kiro_crew.hooks import HOOK_REPLY, TOOL_DENY, HookResult, ToolHookResult


class _Provider:
    def __init__(self, events: list[AcpEvent], raise_on_stream: bool = False) -> None:
        self.events = events
        self.approved: list[Any] = []
        self.rejected: list[Any] = []
        self.streamed: list[str] = []
        self._raise = raise_on_stream

    async def stream(self, message: str) -> Any:
        self.streamed.append(message)
        if self._raise:
            raise RuntimeError("stream blew up")
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

    async def get_or_create(self, key: str, agent: str | None = None, **_kw: Any) -> Any:
        return self.provider, True, False

    def release(self, key: str, cleanup: bool = False) -> None:
        self.released.append(key)


class _Hooks:
    def __init__(self, result: ToolHookResult | None = None) -> None:
        self.result = result or ToolHookResult.allow()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def on_tool_call(self, tool_name: str, **kwargs: Any) -> ToolHookResult:
        self.calls.append((tool_name, kwargs))
        return self.result


class _Ctx:
    def __init__(self, hook: HookResult | None = None, hooks: _Hooks | None = None) -> None:
        self.hook = hook or HookResult.passthrough()
        self.hooks = hooks or _Hooks()

    def build_message(
        self, text: str, is_new: bool, session_key: str, **_kw: Any
    ) -> tuple[str, HookResult]:
        return "CTX::" + text, self.hook


class _Svc:
    def __init__(self, sessions: _Sessions, ctx: _Ctx) -> None:
        self.sessions = sessions
        self.context_builder = ctx


class _Sink:
    def __init__(self, allow: bool = True, cancelled: bool = False) -> None:
        self.texts: list[str] = []
        self.thoughts: list[str] = []
        self.tools: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.perms: list[dict[str, Any]] = []
        self._allow = allow
        self._cancelled = cancelled

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    async def send_text(self, text: str) -> None:
        self.texts.append(text)

    async def send_thought(self, text: str) -> None:
        self.thoughts.append(text)

    async def send_tool_call(self, tool_call_id: str, title: str, kind: str, **kw: Any) -> None:
        self.tools.append(
            {
                "id": tool_call_id,
                "title": title,
                "content": kw.get("content"),
                "locations": kw.get("locations"),
            }
        )

    async def send_tool_call_update(self, tool_call_id: str, **kw: Any) -> None:
        self.updates.append({"id": tool_call_id, **kw})

    async def request_permission(self, tool_call: dict[str, Any], **_kw: Any) -> bool:
        self.perms.append(tool_call)
        return self._allow


def _edit_permission() -> list[AcpEvent]:
    return [
        AcpEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=7,
            tool_call_id="t2",
            title="edit a.py",
            tool_kind="edit",
            raw_tool_params={"path": "/t/a.py", "oldStr": "a", "newStr": "b"},
        ),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
    ]


class TestTurnPlumbing:
    @pytest.mark.asyncio
    async def test_streams_text_and_injects_context(self) -> None:
        prov = _Provider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="hello"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
        sess = _Sessions(prov)
        sink = _Sink()
        stop = await make_prompt_handler(_Svc(sess, _Ctx()))(
            PromptRequest(session_id="s1", text="hi"), sink
        )
        assert sink.texts == ["hello"]
        assert prov.streamed == ["CTX::hi"]
        assert stop == STOP_REASON_END_TURN

    @pytest.mark.asyncio
    async def test_session_key_is_namespaced(self) -> None:
        # Must not collide with "dashboard:<slot>" or a Slack thread key.
        prov = _Provider([AcpEvent(kind=EVENT_COMPLETE)])
        sess = _Sessions(prov)
        await make_prompt_handler(_Svc(sess, _Ctx()))(
            PromptRequest(session_id="s1", text="hi"), _Sink()
        )
        assert sess.released == ["acp:s1"]

    @pytest.mark.asyncio
    async def test_semaphore_released_when_stream_raises(self) -> None:
        # get_or_create acquires a per-session semaphore; leaking it wedges the
        # session for the life of the gateway.
        prov = _Provider([], raise_on_stream=True)
        sess = _Sessions(prov)
        with pytest.raises(RuntimeError):
            await make_prompt_handler(_Svc(sess, _Ctx()))(
                PromptRequest(session_id="s2", text="x"), _Sink()
            )
        assert sess.released == ["acp:s2"]

    @pytest.mark.asyncio
    async def test_cancelled_sink_stops_turn(self) -> None:
        prov = _Provider([AcpEvent(kind=EVENT_TEXT_CHUNK, text="a")])
        stop = await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="s6", text="x"), _Sink(cancelled=True)
        )
        assert stop == STOP_REASON_CANCELLED


class TestHooksFirstGate:
    """A hook DENY is final and must not be overridable from the editor."""

    @pytest.mark.asyncio
    async def test_hook_deny_never_reaches_editor(self) -> None:
        prov = _Provider(
            [
                AcpEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    request_id=5,
                    tool_call_id="t1",
                    title="rm -rf /",
                    tool_kind="execute",
                    is_shell=True,
                    raw_tool_params={"command": "rm -rf /"},
                ),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        hooks = _Hooks(ToolHookResult(action=TOOL_DENY, reason="dangerous"))
        sink = _Sink(allow=True)  # editor would say yes; must not be asked
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx(hooks=hooks)))(
            PromptRequest(session_id="s3", text="x"), sink
        )
        assert len(hooks.calls) == 1
        assert prov.rejected == [5]
        assert sink.perms == []
        assert prov.approved == []
        assert any(u.get("status") == "failed" for u in sink.updates)

    @pytest.mark.asyncio
    async def test_editor_approve_approves_tool(self) -> None:
        prov = _Provider(_edit_permission())
        sink = _Sink(allow=True)
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="s4", text="x"), sink
        )
        assert prov.approved == [7]
        assert prov.rejected == []

    @pytest.mark.asyncio
    async def test_editor_reject_rejects_tool(self) -> None:
        prov = _Provider(_edit_permission())
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="s4", text="x"), _Sink(allow=False)
        )
        assert prov.rejected == [7]
        assert prov.approved == []

    @pytest.mark.asyncio
    async def test_permission_carries_diff(self) -> None:
        prov = _Provider(_edit_permission())
        sink = _Sink(allow=True)
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="s4", text="x"), sink
        )
        block = sink.perms[0]["content"][0]
        assert block["type"] == "diff"
        assert block["path"] == "/t/a.py"
        assert block["newText"] == "b"


class TestHookReply:
    @pytest.mark.asyncio
    async def test_hook_reply_short_circuits_model(self) -> None:
        prov = _Provider([AcpEvent(kind=EVENT_COMPLETE)])
        sess = _Sessions(prov)
        ctx = _Ctx(hook=HookResult(action=HOOK_REPLY, text="canned"))
        sink = _Sink()
        stop = await make_prompt_handler(_Svc(sess, ctx))(
            PromptRequest(session_id="s5", text="x"), sink
        )
        assert prov.streamed == []
        assert sink.texts == ["canned"]
        assert stop == STOP_REASON_END_TURN
        assert sess.released == ["acp:s5"]


class TestTodoToolSuppression:
    @pytest.mark.asyncio
    async def test_todo_tool_events_are_not_sent_to_editor(self) -> None:
        prov = _Provider(
            [
                AcpEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id="todo-1",
                    title="Completing #1",
                    tool_name="todo_list",
                ),
                AcpEvent(
                    kind=EVENT_TOOL_CALL_UPDATE,
                    tool_call_id="todo-1",
                    tool_name="todo_list",
                ),
                AcpEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="todo-1",
                    tool_final=True,
                ),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sink = _Sink()
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="s-todo", text="x"), sink
        )
        assert sink.tools == []
        assert sink.updates == []


class TestToolCallDiff:
    @pytest.mark.asyncio
    async def test_tool_call_carries_diff_content(self) -> None:
        prov = _Provider(
            [
                AcpEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id="t3",
                    title="edit",
                    tool_kind="edit",
                    raw_tool_params={"path": "/t/b.py", "oldStr": "x", "newStr": "y"},
                ),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sink = _Sink()
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="s7", text="x"), sink
        )
        assert sink.tools[0]["content"][0]["path"] == "/t/b.py"


class TestDiffContent:
    def test_str_replace_shape(self) -> None:
        blocks = diff_content({"path": "/t/a.py", "oldStr": "a", "newStr": "b"})
        assert blocks is not None
        assert blocks[0] == {
            "type": "diff",
            "path": "/t/a.py",
            "oldText": "a",
            "newText": "b",
        }

    def test_whole_file_create_has_empty_old_text(self) -> None:
        blocks = diff_content({"path": "/t/a.py", "file_text": "x"})
        assert blocks is not None
        assert blocks[0]["oldText"] == ""
        assert blocks[0]["newText"] == "x"

    def test_non_file_tool_has_no_diff(self) -> None:
        assert diff_content({"command": "ls"}) is None

    def test_non_dict_is_ignored(self) -> None:
        assert diff_content(None) is None


class TestRedactionPlumbing:
    """The pattern library is tested in test_security; this pins that we CALL it."""

    def test_redact_composes_both_stages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def fake_urls(text: str) -> tuple[str, list[str]]:
            calls.append("urls")
            return text + "|u", []

        def fake_creds(text: str) -> tuple[str, list[str]]:
            calls.append("creds")
            return text + "|c", []

        monkeypatch.setattr(gateway, "redact_exfiltration_urls", fake_urls)
        monkeypatch.setattr(gateway, "redact_credentials", fake_creds)
        assert gateway.redact("x") == "x|u|c"
        assert calls == ["urls", "creds"]

    def test_empty_text_skips_redaction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(_text: str) -> tuple[str, list[str]]:
            raise AssertionError("should not be called for empty text")

        monkeypatch.setattr(gateway, "redact_exfiltration_urls", boom)
        assert gateway.redact("") == ""

    @pytest.mark.asyncio
    async def test_streamed_text_is_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gateway, "redact", lambda t: "[CLEAN]" if t else t)
        prov = _Provider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="secret-ish"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sink = _Sink()
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="s1", text="x"), sink
        )
        assert sink.texts == ["[CLEAN]"]

    @pytest.mark.asyncio
    async def test_diff_bodies_are_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Both sides of a diff leave Kiro Crew, so both need redaction.
        monkeypatch.setattr(gateway, "redact", lambda t: "[CLEAN]" if t else t)
        blocks = gateway.diff_content({"path": "/t/a.py", "oldStr": "a", "newStr": "b"})
        assert blocks is not None
        assert blocks[0]["oldText"] == "[CLEAN]"
        assert blocks[0]["newText"] == "[CLEAN]"


class TestEventMapping:
    @pytest.mark.asyncio
    async def test_thinking_chunk_becomes_thought(self) -> None:
        prov = _Provider(
            [
                AcpEvent(kind=EVENT_THINKING_CHUNK, text="pondering"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sink = _Sink()
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="s1", text="x"), sink
        )
        assert sink.thoughts == ["pondering"]
        assert sink.texts == []

    @pytest.mark.asyncio
    async def test_tool_final_marks_completed(self) -> None:
        prov = _Provider(
            [
                AcpEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="t1",
                    tool_output="done",
                    tool_final=True,
                ),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sink = _Sink()
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="s1", text="x"), sink
        )
        assert sink.updates[0]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_non_final_tool_update_stays_pending(self) -> None:
        prov = _Provider(
            [
                AcpEvent(kind=EVENT_TOOL_CALL_UPDATE, tool_call_id="t1", tool_final=False),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sink = _Sink()
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="s1", text="x"), sink
        )
        assert sink.updates[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_stream_without_complete_still_ends_turn(self) -> None:
        prov = _Provider([AcpEvent(kind=EVENT_TEXT_CHUNK, text="a")])
        stop = await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="s1", text="x"), _Sink()
        )
        assert stop == STOP_REASON_END_TURN


class TestContextFallback:
    @pytest.mark.asyncio
    async def test_context_failure_falls_back_to_raw_prompt(self) -> None:
        """A context-assembly failure must not lose the user's message."""

        class _BadCtx(_Ctx):
            def build_message(self, text: str, is_new: bool, session_key: str, **_kw: Any) -> Any:
                raise RuntimeError("memory store exploded")

        prov = _Provider([AcpEvent(kind=EVENT_COMPLETE)])
        await make_prompt_handler(_Svc(_Sessions(prov), _BadCtx()))(
            PromptRequest(session_id="s1", text="keep me"), _Sink()
        )
        assert prov.streamed == ["keep me"]


class TestNonFileToolFallback:
    @pytest.mark.asyncio
    async def test_permission_shows_tool_input_when_no_diff(self) -> None:
        prov = _Provider(
            [
                AcpEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    request_id=3,
                    tool_call_id="t1",
                    title="run ls",
                    tool_kind="execute",
                    is_shell=True,
                    tool_input='{"command": "ls -la"}',
                    raw_tool_params={"command": "ls -la"},
                ),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sink = _Sink(allow=True)
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="s1", text="x"), sink
        )
        content = sink.perms[0]["content"]
        assert content[0]["type"] == "content"
        assert "ls -la" in content[0]["content"]["text"]

    @pytest.mark.asyncio
    async def test_shell_command_is_passed_to_hooks(self) -> None:
        """Hooks must gate on the real command, not the LLM-authored title."""
        prov = _Provider(
            [
                AcpEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    request_id=3,
                    tool_call_id="t1",
                    title="list the directory",
                    tool_kind="execute",
                    is_shell=True,
                    raw_tool_params={"command": "ls -la", "path": "/repo"},
                    mcp_server_name="editor-tools",
                    tool_name="run",
                ),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        hooks = _Hooks()
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx(hooks=hooks)))(
            PromptRequest(session_id="s1", text="x"), _Sink()
        )
        title, kwargs = hooks.calls[0]
        assert title == "list the directory"
        assert kwargs["command"] == "ls -la"
        assert kwargs["is_shell"] is True
        assert kwargs["session_key"] == "acp:s1"
        assert kwargs["agent"] == ""
        assert kwargs["tool_kind"] == "execute"
        assert kwargs["raw_params"] == {"command": "ls -la", "path": "/repo"}
        assert kwargs["mcp_server_name"] == "editor-tools"
        assert kwargs["mcp_tool_name"] == "run"


class TestAutoApproveIsConservative:
    @pytest.mark.asyncio
    async def test_auto_approve_still_asks_the_editor(self) -> None:
        """TOOL_AUTO_APPROVE currently falls through to the editor prompt.

        The dashboard instead approves without prompting, but only AFTER firing
        scripted PreToolUse hooks (exit-2 BLOCKED overrides auto-approve). This
        bridge has no hook-store wiring yet, so honouring auto-approve here would
        skip that gate. Asking the editor is strictly MORE gating, so it is the
        safe interim behaviour. This test pins it deliberately — when the scripted
        hook path is wired, change this test alongside it.
        """
        prov = _Provider(_edit_permission())
        hooks = _Hooks(ToolHookResult.auto_approve())
        sink = _Sink(allow=True)
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx(hooks=hooks)))(
            PromptRequest(session_id="s4", text="x"), sink
        )
        assert len(sink.perms) == 1
        assert prov.approved == [7]


class TestToolCallLocations:
    """Editor follow-along: gateway derives ``locations`` from raw_tool_params."""

    @pytest.mark.asyncio
    async def test_tool_call_carries_locations(self) -> None:
        prov = _Provider(
            [
                AcpEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id="loc-1",
                    title="edit",
                    tool_kind="edit",
                    tool_name="str_replace",
                    raw_tool_params={"path": "/t/a.py", "oldStr": "x", "newStr": "y"},
                ),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sink = _Sink()
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="loc1", text="x"), sink
        )
        assert sink.tools[0]["locations"] == [{"path": "/t/a.py"}]

    @pytest.mark.asyncio
    async def test_tool_call_update_carries_locations(self) -> None:
        prov = _Provider(
            [
                AcpEvent(
                    kind=EVENT_TOOL_CALL_UPDATE,
                    tool_call_id="loc-2",
                    tool_name="fs_read",
                    tool_final=True,
                    raw_tool_params={"path": "/t/b.py", "start_line": 42},
                    tool_output="line 42\n",
                ),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sink = _Sink()
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="loc2", text="x"), sink
        )
        assert sink.updates[0]["locations"] == [{"path": "/t/b.py", "line": 42}]

    @pytest.mark.asyncio
    async def test_permission_request_carries_locations(self) -> None:
        # A tool that requires an editor OK must still let the editor follow to
        # the file being changed — otherwise the user has to open the file by
        # hand to review the pending edit.
        prov = _Provider(_edit_permission())
        sink = _Sink(allow=True)
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="loc3", text="x"), sink
        )
        assert sink.perms[0]["locations"] == [{"path": "/t/a.py"}]

    @pytest.mark.asyncio
    async def test_shell_tool_has_no_locations(self) -> None:
        prov = _Provider(
            [
                AcpEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id="loc-4",
                    title="bash",
                    tool_kind="execute",
                    tool_name="execute_bash",
                    raw_tool_params={"command": "cat /tmp/x"},
                ),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sink = _Sink()
        await make_prompt_handler(_Svc(_Sessions(prov), _Ctx()))(
            PromptRequest(session_id="loc4", text="x"), sink
        )
        # extract_tool_locations returns [] for shell tools; the sink records
        # what it received. AcpAgentServer wire-drop for empty locations is
        # pinned in test_acp_server_protocol.py.
        assert sink.tools[0]["locations"] == []
