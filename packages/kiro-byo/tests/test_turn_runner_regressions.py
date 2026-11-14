from __future__ import annotations

import json
import os
import tempfile
from io import StringIO

import kiro_byo.turn_runner as turn_mod
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import ModelSpec, ProviderSpec
from kiro_byo.mode_router import ReviewTarget
from kiro_byo.providers.base import ChatTurn, ProviderError, ProviderInterrupted, ProviderTransportError, ToolCall, UsageInfo
from kiro_byo.session_events import append_message


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
        def tool_specs(self, names=None):
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
        def tool_specs(self, names=None):
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
        def tool_specs(self, names=None):
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



def _jsonrpc_payloads(output: StringIO) -> list[dict]:
    return [
        json.loads(line)
        for line in output.getvalue().splitlines()
        if line.strip().startswith("{")
    ]


def _result_for(payloads: list[dict], req_id: int = 1) -> dict:
    for payload in reversed(payloads):
        if payload.get("id") == req_id and isinstance(payload.get("result"), dict):
            return payload["result"]
    raise AssertionError(f"no result for request {req_id}")


def test_characterization_simple_round_commits_one_answer_and_turn_lifecycle() -> None:
    td, old, server, output = _server()
    original = turn_mod.chat_completion_turn
    try:
        def fake_turn(provider, model_id, messages, emit, **kwargs):
            emit("hel")
            emit("lo")
            return ChatTurn(
                text="hello",
                usage=UsageInfo(input_tokens=7, output_tokens=2),
                served_model="p-pro-served",
            )

        turn_mod.chat_completion_turn = fake_turn
        session = Session("simple", td.name, MODEL.model_id)
        server._put_session(session)
        append_message(session, {"role": "user", "content": "say hello"})

        server._run_provider_tool_loop(1, "simple", session, PROVIDER, MODEL, 0, 0.0, 0.0)

        assert session.messages[-1] == {"role": "assistant", "content": "hello"}
        event_types = [event["type"] for event in session.events]
        assert event_types == ["user/message", "model/invoke", "model/result", "assistant/message", "turn/end"]
        payloads = _jsonrpc_payloads(output)
        result = _result_for(payloads)
        assert result["stopReason"] == "end_turn"
        assert result["inputTokens"] == 7
        assert result["outputTokens"] == 2
        text_updates = [
            payload["params"]["update"]["content"]["text"]
            for payload in payloads
            if payload.get("method") == "session/update"
            and ((payload.get("params") or {}).get("update") or {}).get("content", {}).get("type") == "text"
        ]
        assert text_updates == ["hello"]
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_characterization_tool_roundtrip_preserves_protocol_order() -> None:
    td, old, server, _ = _server()
    original = turn_mod.chat_completion_turn
    calls = 0

    class Native:
        def specs(self):
            from types import SimpleNamespace
            return (SimpleNamespace(name="read"),)
        def tool_specs(self, names=None):
            return [{"type": "function", "function": {"name": "read"}}]
        def has_tool(self, name):
            return name == "read"
        def capability_for(self, name):
            return "fs_read"

    try:
        def fake_turn(provider, model_id, messages, emit, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ChatTurn(
                    text="I will inspect it.",
                    tool_calls=(ToolCall("call-1", "read", '{"path":"a.txt"}'),),
                    usage=UsageInfo(input_tokens=10, output_tokens=3),
                )
            assert messages[-1]["role"] == "tool"
            assert messages[-1]["tool_call_id"] == "call-1"
            assert messages[-1]["content"] == "file contents"
            return ChatTurn(text="done", usage=UsageInfo(input_tokens=20, output_tokens=1))

        turn_mod.chat_completion_turn = fake_turn
        server._execute_native_tool = lambda session, call, args: ("file contents", 0.0, "ok")
        session = Session("tool", td.name, MODEL.model_id, native=Native())
        server._put_session(session)
        append_message(session, {"role": "user", "content": "inspect a.txt"})

        server._run_provider_tool_loop(1, "tool", session, PROVIDER, MODEL, 0, 0.0, 0.0)

        assert calls == 2
        assert [row["role"] for row in session.messages] == ["user", "assistant", "tool", "assistant"]
        assert session.messages[1]["tool_calls"][0]["function"]["name"] == "read"
        assert session.messages[2]["content"] == "file contents"
        assert session.messages[3]["content"] == "done"
        event_types = [event["type"] for event in session.events]
        assert event_types == [
            "user/message", "model/invoke", "model/result", "assistant/attempt", "tool/call", "tool/result",
            "model/invoke", "model/result", "assistant/message", "turn/end",
        ]
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_characterization_pristine_first_round_can_fallback_provider() -> None:
    td, old, server, _ = _server()
    original = turn_mod.chat_completion_turn
    fallback_model = ModelSpec("q", "Q", "q-pro", "Q Pro", tier="strong")
    fallback_provider = ProviderSpec("q", "Q", "openai-compatible", "https://q.invalid/v1", "", (fallback_model,))
    calls: list[tuple[str, str]] = []
    try:
        def fake_turn(provider, model_id, messages, emit, **kwargs):
            calls.append((provider.id, model_id))
            if provider.id == "p":
                raise ProviderError("primary unavailable")
            return ChatTurn(text="fallback answer")

        turn_mod.chat_completion_turn = fake_turn
        session = Session("fallback", td.name, MODEL.model_id)
        server._put_session(session)
        append_message(session, {"role": "user", "content": "work"})

        server._run_provider_tool_loop(
            1, "fallback", session, PROVIDER, MODEL, 0, 0.0, 0.0,
            [(fallback_provider, fallback_model)],
        )

        assert calls == [("p", "p-pro"), ("q", "q-pro")]
        assert session.messages[-1]["content"] == "fallback answer"
        fallback_events = [event for event in session.events if event["type"] == "model/fallback"]
        assert len(fallback_events) == 1
        assert fallback_events[0]["data"]["from_provider"] == "p"
        assert fallback_events[0]["data"]["to_provider"] == "q"
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_characterization_provider_failure_after_tool_state_fails_closed_without_fallback() -> None:
    td, old, server, output = _server()
    original = turn_mod.chat_completion_turn
    fallback_model = ModelSpec("q", "Q", "q-pro", "Q Pro", tier="strong")
    fallback_provider = ProviderSpec("q", "Q", "openai-compatible", "https://q.invalid/v1", "", (fallback_model,))
    calls: list[tuple[str, str]] = []

    class Native:
        def specs(self):
            from types import SimpleNamespace
            return (SimpleNamespace(name="read"),)
        def tool_specs(self, names=None):
            return [{"type": "function", "function": {"name": "read"}}]
        def has_tool(self, name):
            return name == "read"
        def capability_for(self, name):
            return "fs_read"

    try:
        def fake_turn(provider, model_id, messages, emit, **kwargs):
            calls.append((provider.id, model_id))
            if len(calls) == 1:
                return ChatTurn(text="", tool_calls=(ToolCall("c1", "read", '{"path":"a.txt"}'),))
            raise ProviderError("failed after state existed")

        turn_mod.chat_completion_turn = fake_turn
        server._execute_native_tool = lambda session, call, args: ("data", 0.0, "ok")
        session = Session("fail-closed", td.name, MODEL.model_id, native=Native())
        server._put_session(session)
        append_message(session, {"role": "user", "content": "inspect"})

        server._run_provider_tool_loop(
            1, "fail-closed", session, PROVIDER, MODEL, 0, 0.0, 0.0,
            [(fallback_provider, fallback_model)],
        )

        assert calls == [("p", "p-pro"), ("p", "p-pro")]
        assert session.messages == []
        assert not any(event["type"] == "model/fallback" for event in session.events)
        result = _result_for(_jsonrpc_payloads(output))
        assert result["stopReason"] == "end_turn"
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_characterization_unclaimed_provider_interrupt_keeps_partial_and_ends_turn() -> None:
    td, old, server, output = _server()
    original = turn_mod.chat_completion_turn
    try:
        def fake_turn(provider, model_id, messages, emit, **kwargs):
            raise ProviderInterrupted("partial answer")

        turn_mod.chat_completion_turn = fake_turn
        session = Session("interrupt", td.name, MODEL.model_id)
        server._put_session(session)
        append_message(session, {"role": "user", "content": "work"})

        server._run_provider_tool_loop(1, "interrupt", session, PROVIDER, MODEL, 0, 0.0, 0.0)

        assert session.messages[-1] == {"role": "assistant", "content": "partial answer"}
        result = _result_for(_jsonrpc_payloads(output))
        assert result["stopReason"] == "end_turn"
        assert session.events[-1]["type"] == "turn/end"
        assert session.events[-1]["data"]["outcome"] == "interrupted"
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_characterization_steer_interrupt_injects_partial_and_user_then_continues() -> None:
    td, old, server, _ = _server()
    original = turn_mod.chat_completion_turn
    calls = 0
    try:
        def fake_turn(provider, model_id, messages, emit, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderInterrupted("partial before steer")
            assert [row["role"] for row in messages[-3:]] == ["user", "assistant", "user"]
            assert messages[-1]["content"] == "new direction"
            return ChatTurn(text="steered answer")

        turn_mod.chat_completion_turn = fake_turn
        session = Session("steer", td.name, MODEL.model_id)
        server._put_session(session)
        append_message(session, {"role": "user", "content": "original"})
        session.steer_queue.append("new direction")
        session.steer_event.set()

        server._run_provider_tool_loop(1, "steer", session, PROVIDER, MODEL, 0, 0.0, 0.0)

        assert calls == 2
        assert [row.get("content") for row in session.messages] == [
            "original", "partial before steer", "new direction", "steered answer",
        ]
        assert any(event["type"] == "turn/steer" for event in session.events)
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_characterization_cancel_before_invoke_rolls_back_without_provider_call() -> None:
    td, old, server, output = _server()
    original = turn_mod.chat_completion_turn
    calls = 0
    try:
        def fake_turn(*args, **kwargs):
            nonlocal calls
            calls += 1
            return ChatTurn(text="must not run")

        turn_mod.chat_completion_turn = fake_turn
        session = Session("cancel", td.name, MODEL.model_id)
        server._put_session(session)
        append_message(session, {"role": "user", "content": "work"})
        session.cancel_event.set()

        server._run_provider_tool_loop(1, "cancel", session, PROVIDER, MODEL, 0, 0.0, 0.0)

        assert calls == 0
        assert session.messages == []
        result = _result_for(_jsonrpc_payloads(output))
        assert result["stopReason"] == "cancelled"
        assert session.events[-1]["type"] == "turn/end"
        assert session.events[-1]["data"]["outcome"] == "cancelled"
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_characterization_glitter_worker_failure_hands_back_to_lead() -> None:
    td, old, server, _ = _server()
    original = turn_mod.chat_completion_turn
    calls: list[str] = []

    class Native:
        def specs(self):
            from types import SimpleNamespace
            return (SimpleNamespace(name="read"),)
        def tool_specs(self, names=None):
            return [{"type": "function", "function": {"name": "read"}}]
        def has_tool(self, name):
            return name == "read"
        def capability_for(self, name):
            return "fs_read"

    try:
        def fake_turn(provider, model_id, messages, emit, **kwargs):
            calls.append(model_id)
            if calls == [MODEL.model_id]:
                return ChatTurn(text="", tool_calls=(ToolCall("c1", "read", '{"path":"a.txt"}'),))
            if model_id == OTHER.model_id:
                raise ProviderError("worker unavailable")
            return ChatTurn(text="lead recovered")

        turn_mod.chat_completion_turn = fake_turn
        server._execute_native_tool = lambda session, call, args: ("gathered", 0.0, "ok")
        session = Session("relay-fail", td.name, MODEL.model_id, native=Native())
        server._put_session(session)
        append_message(session, {"role": "user", "content": "gather then answer"})

        server._run_provider_tool_loop(
            1, "relay-fail", session, PROVIDER, MODEL, 0, 0.0, 0.0,
            relay=(PROVIDER, OTHER, ""),
        )

        assert calls == [MODEL.model_id, OTHER.model_id, MODEL.model_id]
        assert session.messages[-1]["content"] == "lead recovered"
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_live_turn_engine_emits_explicit_phase_sequence_for_plain_answer() -> None:
    td, old, server, _ = _server()
    original = turn_mod.chat_completion_turn
    phases = []
    try:
        turn_mod.chat_completion_turn = lambda *args, **kwargs: ChatTurn(text="done")
        original_runtime_event = server._runtime_event

        def capture(event, session, *, data=None, content=None, **correlation):
            if event == "turn.phase":
                phases.append((data or {}).get("to"))
            return original_runtime_event(event, session, data=data, content=content, **correlation)

        server._runtime_event = capture
        session = Session("phase-plain", td.name, MODEL.model_id)
        server._put_session(session)
        append_message(session, {"role": "user", "content": "hello"})
        server._run_provider_tool_loop(1, session.session_id, session, PROVIDER, MODEL, 0, 0.0, 0.0)
        assert phases == ["route", "invoke", "post_stage", "complete"]
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_live_turn_engine_emits_tool_continue_phase_before_next_round() -> None:
    td, old, server, _ = _server()
    original = turn_mod.chat_completion_turn
    phases = []
    calls = 0

    class Native:
        def specs(self):
            from types import SimpleNamespace
            return (SimpleNamespace(name="read"),)
        def tool_specs(self, names=None):
            return [{"type": "function", "function": {"name": "read"}}]
        def has_tool(self, name):
            return name == "read"
        def capability_for(self, name):
            return "fs_read"

    try:
        def fake_turn(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ChatTurn(text="", tool_calls=(ToolCall("c1", "read", '{"path":"a.txt"}'),))
            return ChatTurn(text="done")

        turn_mod.chat_completion_turn = fake_turn
        server._execute_native_tool = lambda *args, **kwargs: ("contents", 0.0, "ok")
        original_runtime_event = server._runtime_event

        def capture(event, session, *, data=None, content=None, **correlation):
            if event == "turn.phase":
                phases.append((data or {}).get("to"))
            return original_runtime_event(event, session, data=data, content=content, **correlation)

        server._runtime_event = capture
        session = Session("phase-tool", td.name, MODEL.model_id, native=Native())
        server._put_session(session)
        append_message(session, {"role": "user", "content": "read it"})
        server._run_provider_tool_loop(1, session.session_id, session, PROVIDER, MODEL, 0, 0.0, 0.0)
        assert phases == ["route", "invoke", "tool", "continue", "invoke", "post_stage", "complete"]
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_detached_review_prompt_does_not_expose_type_product_names() -> None:
    td, old, server, _ = _server()
    original = turn_mod.chat_completion_turn
    captured = []
    try:
        calls = 0
        def fake_turn(provider, model_id, messages, emit, **kwargs):
            nonlocal calls
            calls += 1
            captured.append(messages)
            if calls == 1:
                return ChatTurn(text="A" * 1200, usage=UsageInfo(output_tokens=400))
            return ChatTurn(text="reviewed", usage=UsageInfo(output_tokens=10))

        turn_mod.chat_completion_turn = fake_turn
        session = Session("s", td.name, MODEL.model_id)
        server._put_session(session)
        from kiro_byo.session_events import append_message
        append_message(session, {"role": "user", "content": "do substantial work"})
        review = ReviewTarget(
            PROVIDER, MODEL, "verify", role="verify", pack="tiga", pack_label="Tiga",
            policy="power", policy_label="Power Type", requires_substantive_work=True,
        )
        server._run_provider_tool_loop(1, "s", session, PROVIDER, MODEL, 0, 0.0, 0.0, [], review)
        assert len(captured) == 2
        review_system = "\n".join(
            str(m.get("content") or "") for m in captured[1] if m.get("role") == "system"
        )
        assert "Power Type" not in review_system
        assert "Tiga" not in review_system
        assert "detached verify pass" in review_system
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_pristine_transport_failure_retries_once_and_records_recovery() -> None:
    td, old, server, output = _server()
    original = turn_mod.chat_completion_turn
    calls = 0
    try:
        def fake_turn(provider, model_id, messages, emit, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderTransportError("upstream stream reset")
            return ChatTurn(text="recovered")

        turn_mod.chat_completion_turn = fake_turn
        session = Session("retry", td.name, MODEL.model_id)
        server._put_session(session)
        session.messages.append({"role": "user", "content": "hello"})
        server._run_provider_tool_loop(1, "retry", session, PROVIDER, MODEL, 0, 0.0, 0.0)

        assert calls == 2
        assert session.messages[-1]["content"] == "recovered"
        rows = server.telemetry.recent(5)
        assert rows[-1]["outcome"] == "success"
        assert rows[-1]["transport_retries"] == 1
        events = server.runtime_trace.recent(100)
        assert any(row.get("event") == "provider.retry" for row in events)
        payloads = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")]
        assert any(p.get("id") == 1 and "result" in p for p in payloads)
        assert not any(p.get("id") == 1 and "error" in p for p in payloads)
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_partial_transport_failure_does_not_retry_and_rolls_back_cleanly() -> None:
    td, old, server, output = _server()
    original = turn_mod.chat_completion_turn
    calls = 0
    try:
        def fake_turn(provider, model_id, messages, emit, **kwargs):
            nonlocal calls
            calls += 1
            emit("partial")
            raise ProviderTransportError("connection closed")

        turn_mod.chat_completion_turn = fake_turn
        session = Session("partial", td.name, MODEL.model_id)
        server._put_session(session)
        session.messages.append({"role": "user", "content": "failed user turn"})
        server._run_provider_tool_loop(1, "partial", session, PROVIDER, MODEL, 0, 0.0, 0.0)

        assert calls == 1
        assert session.messages == []
        rows = server.telemetry.recent(5)
        assert rows[-1]["outcome"] == "error"
        assert rows[-1]["error_category"] == "transport"
        payloads = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")]
        assert any(p.get("id") == 1 and "result" in p for p in payloads)
        assert not any(p.get("id") == 1 and "error" in p for p in payloads)
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_failure_after_mutating_tool_preserves_effect_context_and_session_can_continue() -> None:
    td, old, server, _ = _server()
    original = turn_mod.chat_completion_turn
    calls = 0

    class Native:
        def specs(self):
            from types import SimpleNamespace
            return (SimpleNamespace(name="write"),)
        def tool_specs(self, names=None):
            return [{"type": "function", "function": {"name": "write"}}]
        def has_tool(self, name):
            return name == "write"
        def capability_for(self, name):
            return "fs_write"

    try:
        def fake_turn(provider, model_id, messages, emit, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ChatTurn(
                    text="",
                    tool_calls=(ToolCall("w1", "write", '{"path":"x.txt","content":"x"}'),),
                )
            if calls == 2:
                raise ProviderTransportError("connection closed after write")
            return ChatTurn(text="continued safely")

        turn_mod.chat_completion_turn = fake_turn
        server._execute_native_tool = lambda session, call, args: ("Wrote x.txt", 0.0, "")
        session = Session("mutation", td.name, MODEL.model_id, native=Native())
        server._put_session(session)
        session.messages.append({"role": "user", "content": "write it"})
        server._run_provider_tool_loop(1, "mutation", session, PROVIDER, MODEL, 0, 0.0, 0.0)

        assert any(m.get("role") == "tool" and m.get("content") == "Wrote x.txt" for m in session.messages)
        assert session.messages[-1]["role"] == "system"
        assert "do not automatically repeat mutating actions" in session.messages[-1]["content"]
        assert any(e.get("type") == "turn/recovery_preserved" for e in session.events)

        history_start = len(session.messages)
        session.messages.append({"role": "user", "content": "what now?"})
        server._run_provider_tool_loop(2, "mutation", session, PROVIDER, MODEL, history_start, 0.0, 0.0)
        assert session.messages[-1]["content"] == "continued safely"
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)


def test_unexpected_runtime_exception_is_recovered_without_poisoning_session() -> None:
    td, old, server, output = _server()
    original = turn_mod.chat_completion_turn
    calls = 0
    try:
        def fake_turn(provider, model_id, messages, emit, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("synthetic harness failure")
            return ChatTurn(text="second turn works")

        turn_mod.chat_completion_turn = fake_turn
        session = Session("internal-recover", td.name, MODEL.model_id)
        server._put_session(session)
        session.messages.append({"role": "user", "content": "first turn"})
        server._run_provider_tool_loop(
            1, "internal-recover", session, PROVIDER, MODEL, 0, 0.0, 0.0
        )

        assert session.messages == []
        rows = server.telemetry.recent(5)
        assert rows[-1]["outcome"] == "error"
        assert rows[-1]["error_category"] == "internal"
        assert any(e.get("event") == "runtime.internal_error" for e in server.runtime_trace.recent(100))
        payloads = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")]
        assert any(p.get("id") == 1 and "result" in p for p in payloads)
        assert not any(p.get("id") == 1 and "error" in p for p in payloads)

        history_start = len(session.messages)
        session.messages.append({"role": "user", "content": "try again"})
        server._run_provider_tool_loop(
            2, "internal-recover", session, PROVIDER, MODEL, history_start, 0.0, 0.0
        )
        assert session.messages[-1]["content"] == "second turn works"
    finally:
        turn_mod.chat_completion_turn = original
        _cleanup(td, old, server)
