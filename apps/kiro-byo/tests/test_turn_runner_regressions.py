from __future__ import annotations

import json
import os
import tempfile
from io import StringIO

import kiro_byo.turn_runner as turn_mod
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import ModelSpec, ProviderSpec
from kiro_byo.mode_router import ReviewTarget
from kiro_byo.providers.base import ChatTurn, UsageInfo


MODEL = ModelSpec("p", "P", "p-pro", "P Pro", tier="strong")
OTHER = ModelSpec("p", "P", "p-flash", "P Flash", tier="fast")
PROVIDER = ProviderSpec("p", "P", "openai-compatible", "https://example.invalid/v1", "", (MODEL, OTHER))


def _server():
    td = tempfile.TemporaryDirectory()
    old = os.environ.get("KIRO_BYO_HOME")
    os.environ["KIRO_BYO_HOME"] = td.name
    output = StringIO()
    server = AcpServer(stdin=StringIO(), stdout=output)
    return td, old, server, output


def _cleanup(td, old, server):
    server._executor.shutdown(wait=True)
    if old is None:
        os.environ.pop("KIRO_BYO_HOME", None)
    else:
        os.environ["KIRO_BYO_HOME"] = old
    td.cleanup()


def test_provider_context_is_kept_only_for_exact_model_and_main_branch() -> None:
    scoped = turn_mod.TurnRunnerMixin._scoped_provider_context(
        {"provider_id": "p", "transport": "openai-compatible", "data": {"x": 1}},
        PROVIDER,
        MODEL,
    )
    messages = [{"role": "assistant", "content": "x", "_kiro_provider_context": scoped}]
    same = turn_mod.TurnRunnerMixin._messages_for_provider(messages, PROVIDER, MODEL)
    other = turn_mod.TurnRunnerMixin._messages_for_provider(messages, PROVIDER, OTHER)
    assert "_kiro_provider_context" in same[0]
    assert "_kiro_provider_context" not in other[0]

    legacy = [{"role": "assistant", "content": "x", "_kiro_provider_context": {"provider_id": "p", "transport": "openai-compatible", "data": {}}}]
    assert "_kiro_provider_context" not in turn_mod.TurnRunnerMixin._messages_for_provider(legacy, PROVIDER, MODEL)[0]


def test_provider_content_is_not_duplicated_as_thinking_then_final_text() -> None:
    td, old, server, output = _server()
    original = turn_mod.chat_completion_turn
    try:
        def fake_turn(provider, model_id, messages, emit, **kwargs):
            on_reasoning = kwargs.get("on_reasoning")
            assert callable(on_reasoning)
            on_reasoning("先分析")
            emit("你")
            emit("好")
            return ChatTurn(text="你好")

        turn_mod.chat_completion_turn = fake_turn
        session = Session("s", td.name, MODEL.model_id)
        server._put_session(session)
        session.messages.append({"role": "user", "content": "你好"})
        server._run_provider_tool_loop(1, "s", session, PROVIDER, MODEL, 0, 0.0, 0.0)

        payloads = [json.loads(line) for line in output.getvalue().splitlines() if line.strip().startswith("{")]
        updates = [p["params"]["update"] for p in payloads if p.get("method") == "session/update"]
        text_updates = [u for u in updates if (u.get("content") or {}).get("text") == "你好"]
        assert len(text_updates) == 1
        assert text_updates[0]["content"]["type"] == "text"
        thinking_updates = [u for u in updates if (u.get("content") or {}).get("type") == "thinking"]
        assert [u["content"]["text"] for u in thinking_updates] == ["先分析"]
        # Normal answer content must never be mirrored into the thinking lane.
        assert not any((u.get("content") or {}).get("type") == "thinking" and (u.get("content") or {}).get("text") in {"你", "好", "你好"} for u in updates)
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_short_primary_answer_skips_substantive_verify_stage() -> None:
    td, old, server, _ = _server()
    original = turn_mod.chat_completion_turn
    calls = []
    try:
        def fake_turn(provider, model_id, messages, emit, **kwargs):
            calls.append(model_id)
            return ChatTurn(text="你好", usage=UsageInfo(output_tokens=2))

        turn_mod.chat_completion_turn = fake_turn
        session = Session("s", td.name, MODEL.model_id)
        server._put_session(session)
        session.messages.append({"role": "user", "content": "你好"})
        review = ReviewTarget(
            PROVIDER, MODEL, "verify", role="verify", pack="tiga", pack_label="Tiga",
            policy="power", policy_label="Power", requires_substantive_work=True,
        )
        server._run_provider_tool_loop(1, "s", session, PROVIDER, MODEL, 0, 0.0, 0.0, [], review)
        assert calls == [MODEL.model_id]
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_review_provider_context_never_replaces_main_conversation_context() -> None:
    td, old, server, _ = _server()
    original = turn_mod.chat_completion_turn
    turns = iter([
        ChatTurn(
            text="A" * 1200,
            usage=UsageInfo(output_tokens=400),
            provider_context={"provider_id": "p", "transport": "openai-compatible", "data": {"response_id": "primary"}},
        ),
        ChatTurn(
            text="B" * 1200,
            usage=UsageInfo(output_tokens=400),
            provider_context={"provider_id": "p", "transport": "openai-compatible", "data": {"response_id": "review"}},
        ),
    ])
    try:
        turn_mod.chat_completion_turn = lambda *args, **kwargs: next(turns)
        session = Session("s", td.name, MODEL.model_id)
        server._put_session(session)
        session.messages.append({"role": "user", "content": "do substantial work"})
        review = ReviewTarget(
            PROVIDER, MODEL, "verify", role="verify", pack="tiga", pack_label="Tiga",
            policy="power", policy_label="Power", requires_substantive_work=True,
        )
        server._run_provider_tool_loop(1, "s", session, PROVIDER, MODEL, 0, 0.0, 0.0, [], review)
        assert session.messages[-1]["content"] == "B" * 1200
        assert "_kiro_provider_context" not in session.messages[-1]
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_productive_tool_chain_can_run_beyond_24_provider_rounds() -> None:
    td, old, server, _ = _server()
    original = turn_mod.chat_completion_turn
    calls = 0

    class Native:
        def specs(self):
            from types import SimpleNamespace
            return (SimpleNamespace(name="read"),)
        def openai_tools(self, names=None):
            return [{"type": "function", "function": {"name": "read"}}]
        def has_tool(self, name):
            return name == "read"
        def capability_for(self, name):
            return "fs_read"

    try:
        from kiro_byo.providers.base import ToolCall

        def fake_turn(provider, model_id, messages, emit, **kwargs):
            nonlocal calls
            calls += 1
            if calls <= 30:
                return ChatTurn(
                    text="",
                    tool_calls=(ToolCall(f"c{calls}", "read", json.dumps({"path": f"f{calls}.txt"})),),
                )
            return ChatTurn(text="finished after long productive work")

        turn_mod.chat_completion_turn = fake_turn
        server._execute_native_tool = lambda session, call, args: (f"content for {args['path']}", 0.0, "ok")
        session = Session("long", td.name, MODEL.model_id, native=Native())
        server._put_session(session)
        session.messages.append({"role": "user", "content": "inspect many files"})
        server._run_provider_tool_loop(1, "long", session, PROVIDER, MODEL, 0, 0.0, 0.0)

        assert calls == 31
        assert session.messages[-1]["content"] == "finished after long productive work"
        assert not any("已达到本轮工具调用上限" in str(message.get("content") or "") for message in session.messages)
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_repeated_tool_loop_is_replanned_then_stopped_without_fixed_round_limit() -> None:
    td, old, server, _ = _server()
    original = turn_mod.chat_completion_turn
    calls = 0
    saw_replan_notice = False

    class Native:
        def specs(self):
            from types import SimpleNamespace
            return (SimpleNamespace(name="read"),)
        def openai_tools(self, names=None):
            return [{"type": "function", "function": {"name": "read"}}]
        def has_tool(self, name):
            return name == "read"
        def capability_for(self, name):
            return "fs_read"

    try:
        from kiro_byo.providers.base import ToolCall

        def fake_turn(provider, model_id, messages, emit, **kwargs):
            nonlocal calls, saw_replan_notice
            calls += 1
            if any("Execution guard:" in str(m.get("content") or "") for m in messages):
                saw_replan_notice = True
            return ChatTurn(text="", tool_calls=(ToolCall(f"c{calls}", "read", '{"path":"same.txt"}'),))

        turn_mod.chat_completion_turn = fake_turn
        server._execute_native_tool = lambda session, call, args: ("same unchanged result", 0.0, "ok")
        session = Session("loop", td.name, MODEL.model_id, native=Native())
        server._put_session(session)
        session.messages.append({"role": "user", "content": "keep checking"})
        server._run_provider_tool_loop(1, "loop", session, PROVIDER, MODEL, 0, 0.0, 0.0)

        assert calls == 6
        assert saw_replan_notice
        assert "持续重复" in session.messages[-1]["content"]
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_batch_repetition_gets_real_replan_chance_before_halt() -> None:
    td, old, server, _ = _server()
    original = turn_mod.chat_completion_turn
    calls = 0
    executed = 0
    saw_replan = False

    class Native:
        def specs(self):
            from types import SimpleNamespace
            return (SimpleNamespace(name="read"),)
        def openai_tools(self, names=None):
            return [{"type": "function", "function": {"name": "read"}}]
        def has_tool(self, name):
            return name == "read"
        def capability_for(self, name):
            return "fs_read"

    try:
        from kiro_byo.providers.base import ToolCall

        def fake_turn(provider, model_id, messages, emit, **kwargs):
            nonlocal calls, saw_replan
            calls += 1
            if any("Execution guard:" in str(m.get("content") or "") for m in messages):
                saw_replan = True
            if calls == 1:
                return ChatTurn(
                    text="",
                    tool_calls=tuple(
                        ToolCall(f"c{i}", "read", '{"path":"same.txt"}') for i in range(6)
                    ),
                )
            return ChatTurn(text="replanned successfully")

        def exec_tool(session, call, args):
            nonlocal executed
            executed += 1
            return ("same result", 0.0, "ok")

        turn_mod.chat_completion_turn = fake_turn
        server._execute_native_tool = exec_tool
        session = Session("batch-loop", td.name, MODEL.model_id, native=Native())
        server._put_session(session)
        session.messages.append({"role": "user", "content": "inspect"})
        server._run_provider_tool_loop(1, "batch-loop", session, PROVIDER, MODEL, 0, 0.0, 0.0)

        assert calls == 2
        assert executed == 4
        assert saw_replan
        assert session.messages[-1]["content"] == "replanned successfully"
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)
