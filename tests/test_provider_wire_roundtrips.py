from __future__ import annotations

import json
from unittest.mock import patch

from kiro_byo.config import ProviderSpec
from kiro_byo.providers import (
    anthropic_messages,
    cohere_v2,
    gemini_interactions,
    mistral_chat,
    moonshot_chat,
    openai_compatible,
    qwen_chat,
    responses_api,
    xiaomi_mimo,
    zhipu_chat,
)


class FakeResponse:
    def __init__(self, payload: object | None = None, *, lines: list[bytes] | None = None, content_type: str = "application/json") -> None:
        self._bytes = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self._lines = list(lines or [])
        self.headers = {"Content-Type": content_type}
        self.closed = False

    def read(self) -> bytes:
        return self._bytes

    def __iter__(self):
        return iter(self._lines)

    def close(self) -> None:
        self.closed = True


def _provider(kind: str, *, base: str = "https://api.example.test/v1", stream: bool = False) -> ProviderSpec:
    return ProviderSpec(
        id="p1",
        name="Provider",
        type=kind,
        base_url=base,
        api_key="secret",
        models=(),
        stream=stream,
        preset_id="test",
    )


def _tool() -> list[dict[str, object]]:
    return [{"type": "function", "function": {"name": "lookup", "description": "Lookup", "parameters": {"type": "object", "properties": {}}}}]


def _assert_turn(turn, seen: list[str]) -> None:
    assert turn.text == "hello"
    assert "hello" in "".join(seen)
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == "lookup"


def test_openai_compatible_nonstream_roundtrip() -> None:
    provider = _provider("openai-compatible")
    payload = {
        "model": "served-openai",
        "choices": [{"message": {"role": "assistant", "content": "hello", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 2}, "completion_tokens_details": {"reasoning_tokens": 1}},
    }
    seen: list[str] = []
    with patch.object(openai_compatible.urllib.request, "urlopen", return_value=FakeResponse(payload)):
        turn = openai_compatible.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert turn.served_model == "served-openai"
    assert turn.usage.input_tokens == 10


def test_qwen_nonstream_roundtrip_preserves_reasoning_context() -> None:
    provider = _provider("qwen-chat-v1")
    payload = {
        "model": "served-qwen",
        "choices": [{"message": {"role": "assistant", "content": "hello", "reasoning_content": "private", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": {"x": 1}}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 2}},
    }
    seen: list[str] = []
    with patch.object(qwen_chat.urllib.request, "urlopen", return_value=FakeResponse(payload)):
        turn = qwen_chat.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert turn.provider_context["data"]["message"]["reasoning_content"] == "private"


def test_zhipu_nonstream_roundtrip_preserves_reasoning_context() -> None:
    provider = _provider("zhipu-chat-v4")
    payload = {
        "model": "served-zhipu",
        "choices": [{"message": {"role": "assistant", "content": "hello", "reasoning_content": "private", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 2}},
    }
    seen: list[str] = []
    with patch.object(zhipu_chat.urllib.request, "urlopen", return_value=FakeResponse(payload)):
        turn = zhipu_chat.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert turn.provider_context["data"]["message"]["reasoning_content"] == "private"


def test_moonshot_nonstream_roundtrip_preserves_reasoning_context() -> None:
    provider = _provider("moonshot-chat-v1")
    payload = {
        "model": "served-kimi",
        "choices": [{"message": {"role": "assistant", "content": "hello", "reasoning_content": "private", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 2}},
    }
    seen: list[str] = []
    with patch.object(moonshot_chat.urllib.request, "urlopen", return_value=FakeResponse(payload)):
        turn = moonshot_chat.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert turn.provider_context["data"]["message"]["reasoning_content"] == "private"


def test_xiaomi_mimo_nonstream_roundtrip_preserves_reasoning_context() -> None:
    provider = _provider("xiaomi-mimo-chat", base="https://api.xiaomimimo.com/v1")
    payload = {
        "model": "mimo-v2.6-pro",
        "choices": [{"message": {"role": "assistant", "content": "hello", "reasoning_content": "private", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    }
    seen: list[str] = []
    with patch.object(xiaomi_mimo.urllib.request, "urlopen", return_value=FakeResponse(payload)):
        turn = xiaomi_mimo.chat_completion_turn(provider, "mimo-v2.6-pro", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert turn.provider_context["data"]["message"]["reasoning_content"] == "private"
    assert turn.served_model == "mimo-v2.6-pro"


def test_mistral_nonstream_roundtrip_preserves_native_content() -> None:
    provider = _provider("mistral-chat-v1")
    payload = {
        "model": "served-mistral",
        "choices": [{"message": {"role": "assistant", "content": [{"type": "thinking", "thinking": [{"type": "text", "text": "private"}]}, {"type": "text", "text": "hello"}], "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    }
    seen: list[str] = []
    with patch.object(mistral_chat.urllib.request, "urlopen", return_value=FakeResponse(payload)):
        turn = mistral_chat.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert isinstance(turn.provider_context["data"]["message"]["content"], list)


def test_cohere_v2_nonstream_roundtrip() -> None:
    provider = _provider("cohere-v2", base="https://api.cohere.test/v2")
    payload = {
        "id": "resp1",
        "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "private"}, {"type": "text", "text": "hello"}], "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": {"x": 1}}}]},
        "usage": {"tokens": {"input_tokens": 10, "output_tokens": 4}},
    }
    seen: list[str] = []
    with patch.object(cohere_v2.urllib.request, "urlopen", return_value=FakeResponse(payload)):
        turn = cohere_v2.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert turn.usage.input_tokens == 10


def test_anthropic_messages_nonstream_roundtrip() -> None:
    provider = _provider("anthropic-messages", base="https://api.anthropic.test/v1")
    payload = {
        "id": "msg1",
        "model": "served-claude",
        "content": [{"type": "thinking", "thinking": "private", "signature": "sig"}, {"type": "text", "text": "hello"}, {"type": "tool_use", "id": "c1", "name": "lookup", "input": {"x": 1}}],
        "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1},
    }
    seen: list[str] = []
    with patch.object(anthropic_messages.urllib.request, "urlopen", return_value=FakeResponse(payload)):
        turn = anthropic_messages.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert turn.provider_context["data"]["message_id"] == "msg1"
    assert turn.usage.cached_tokens == 2


def test_gemini_interactions_nonstream_roundtrip() -> None:
    provider = _provider("gemini-interactions", base="https://generativelanguage.googleapis.test/v1beta")
    payload = {
        "interaction": {
            "id": "int1",
            "model": "models/served-gemini",
            "steps": [
                {"type": "thought", "summary": [{"type": "text", "text": "private"}], "signature": "sig"},
                {"type": "model_output", "content": [{"type": "text", "text": "hello"}]},
                {"type": "function_call", "id": "c1", "name": "lookup", "arguments": {"x": 1}},
            ],
            "usage": {"total_input_tokens": 10, "total_output_tokens": 4, "total_cached_tokens": 2, "total_thought_tokens": 1},
        }
    }
    seen: list[str] = []
    with patch.object(gemini_interactions.urllib.request, "urlopen", return_value=FakeResponse(payload)):
        turn = gemini_interactions.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert turn.served_model == "served-gemini"
    assert turn.provider_context["data"]["interaction_id"] == "int1"


def test_responses_api_nonstream_roundtrip() -> None:
    provider = _provider("openai-responses", base="https://api.openai.test/v1")
    payload = {
        "id": "resp1",
        "model": "served-responses",
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "private"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]},
            {"type": "function_call", "call_id": "c1", "name": "lookup", "arguments": "{}"},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 4, "input_tokens_details": {"cached_tokens": 2}, "output_tokens_details": {"reasoning_tokens": 1}},
    }
    seen: list[str] = []
    with patch.object(responses_api.urllib.request, "urlopen", return_value=FakeResponse(payload)):
        turn = responses_api.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert turn.served_model == "served-responses"
    assert turn.provider_context["data"]["response_id"] == "resp1"


def _sse(*events: dict[str, object] | str) -> list[bytes]:
    lines: list[bytes] = []
    for event in events:
        data = event if isinstance(event, str) else json.dumps(event, separators=(",", ":"))
        lines.append(f"data: {data}\n".encode("utf-8"))
    return lines


def test_openai_compatible_stream_roundtrip() -> None:
    provider = _provider("openai-compatible", stream=True)
    lines = _sse(
        {"model": "served-openai", "choices": [{"delta": {"content": "hel"}}]},
        {"choices": [{"delta": {"content": "lo", "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "lookup", "arguments": "{"}}]}}]},
        {"usage": {"prompt_tokens": 10, "completion_tokens": 4}, "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}}]},
        "[DONE]",
    )
    seen: list[str] = []
    with patch.object(openai_compatible.urllib.request, "urlopen", return_value=FakeResponse(lines=lines, content_type="text/event-stream")):
        turn = openai_compatible.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert turn.served_model == "served-openai"
    assert turn.usage.input_tokens == 10


def test_qwen_stream_roundtrip_preserves_reasoning_context() -> None:
    provider = _provider("qwen-chat-v1", stream=True)
    lines = _sse(
        {"model": "served-qwen", "choices": [{"delta": {"reasoning_content": "pri", "content": "hel"}}]},
        {"choices": [{"delta": {"reasoning_content": "vate", "content": "lo", "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "lookup", "arguments": "{"}}]}}]},
        {"usage": {"prompt_tokens": 10, "completion_tokens": 4}, "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}}]},
        "[DONE]",
    )
    seen: list[str] = []
    with patch.object(qwen_chat.urllib.request, "urlopen", return_value=FakeResponse(lines=lines, content_type="text/event-stream")):
        turn = qwen_chat.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert turn.provider_context["data"]["message"]["reasoning_content"] == "private"
    assert turn.served_model == "served-qwen"


def test_xiaomi_mimo_stream_roundtrip_preserves_reasoning_and_tools() -> None:
    provider = _provider("xiaomi-mimo-chat", base="https://api.xiaomimimo.com/v1", stream=True)
    lines = _sse(
        {"model": "mimo-v2.6-pro", "choices": [{"delta": {"reasoning_content": "pri", "content": None}}]},
        {"choices": [{"delta": {"reasoning_content": "vate", "content": "hel", "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "lookup", "arguments": "{"}}]}}]},
        {"usage": {"prompt_tokens": 10, "completion_tokens": 4}, "choices": [{"delta": {"content": "lo", "tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}}]},
        "[DONE]",
    )
    seen: list[str] = []
    with patch.object(xiaomi_mimo.urllib.request, "urlopen", return_value=FakeResponse(lines=lines, content_type="text/event-stream")):
        turn = xiaomi_mimo.chat_completion_turn(provider, "mimo-v2.6-pro", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert turn.provider_context["data"]["message"]["reasoning_content"] == "private"
    assert turn.usage.input_tokens == 10


def test_mistral_stream_roundtrip_preserves_thinking_chunks() -> None:
    provider = _provider("mistral-chat-v1", stream=True)
    lines = _sse(
        {"model": "served-mistral", "choices": [{"delta": {"content": [{"type": "thinking", "thinking": [{"type": "text", "text": "pri"}]}]}}]},
        {"choices": [{"delta": {"content": [{"type": "thinking", "thinking": [{"type": "text", "text": "vate"}]}, {"type": "text", "text": "hel"}]}}]},
        {"usage": {"prompt_tokens": 10, "completion_tokens": 4}, "choices": [{"delta": {"content": "lo", "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "lookup", "arguments": "{}"}}]}}]},
        "[DONE]",
    )
    seen: list[str] = []
    with patch.object(mistral_chat.urllib.request, "urlopen", return_value=FakeResponse(lines=lines, content_type="text/event-stream")):
        turn = mistral_chat.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    content = turn.provider_context["data"]["message"]["content"]
    assert content[0]["type"] == "thinking"
    assert content[0]["thinking"][0]["text"] == "private"


def test_anthropic_stream_roundtrip_preserves_native_blocks() -> None:
    provider = _provider("anthropic-messages", base="https://api.anthropic.test/v1", stream=True)
    lines = _sse(
        {"type": "message_start", "message": {"id": "msg1", "model": "served-claude", "usage": {"input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 2}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "private"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "hello"}},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "c1", "name": "lookup", "input": {}}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"x\":1}"}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "usage": {"output_tokens": 4}},
    )
    seen: list[str] = []
    with patch.object(anthropic_messages.urllib.request, "urlopen", return_value=FakeResponse(lines=lines, content_type="text/event-stream")):
        turn = anthropic_messages.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert turn.provider_context["data"]["message_id"] == "msg1"
    assert turn.provider_context["data"]["content"][0]["thinking"] == "private"
    assert turn.usage.input_tokens == 12  # input + cache-read tokens
    assert turn.usage.output_tokens == 4


def test_gemini_interactions_stream_roundtrip_preserves_interaction_state() -> None:
    provider = _provider("gemini-interactions", base="https://generativelanguage.googleapis.test/v1beta", stream=True)
    lines = _sse(
        {"event_type": "interaction.created", "interaction": {"id": "int1", "model": "models/served-gemini"}},
        {"event_type": "step.start", "index": 0, "step": {"type": "thought", "summary": []}},
        {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_summary", "content": {"type": "text", "text": "private"}}},
        {"event_type": "step.start", "index": 1, "step": {"type": "model_output", "content": []}},
        {"event_type": "step.delta", "index": 1, "delta": {"type": "text", "text": "hello"}},
        {"event_type": "step.start", "index": 2, "step": {"type": "function_call", "id": "c1", "name": "lookup"}},
        {"event_type": "step.delta", "index": 2, "delta": {"type": "arguments_delta", "arguments": "{\"x\":1}"}},
        {"event_type": "step.stop", "index": 2},
        {"event_type": "interaction.completed", "interaction": {"id": "int1", "model": "models/served-gemini"}, "metadata": {"total_usage": {"total_input_tokens": 10, "total_output_tokens": 4}}},
    )
    seen: list[str] = []
    with patch.object(gemini_interactions.urllib.request, "urlopen", return_value=FakeResponse(lines=lines, content_type="text/event-stream")):
        turn = gemini_interactions.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert turn.served_model == "served-gemini"
    assert turn.provider_context["data"]["interaction_id"] == "int1"
    assert turn.usage.input_tokens == 10


def test_cohere_v2_stream_roundtrip() -> None:
    provider = _provider("cohere-v2", base="https://api.cohere.test/v2", stream=True)
    lines = _sse(
        {"type": "message-start", "id": "resp1"},
        {"type": "content-delta", "delta": {"message": {"content": {"thinking": "private"}}}},
        {"type": "content-delta", "delta": {"message": {"content": {"text": "hello"}}}},
        {"type": "tool-call-start", "index": 0, "delta": {"message": {"tool_calls": {"id": "c1", "function": {"name": "lookup", "arguments": ""}}}}},
        {"type": "tool-call-delta", "index": 0, "delta": {"message": {"tool_calls": {"function": {"arguments": "{\"x\":1}"}}}}},
        {"type": "message-end", "delta": {"usage": {"tokens": {"input_tokens": 10, "output_tokens": 4}}}},
    )
    seen: list[str] = []
    with patch.object(cohere_v2.urllib.request, "urlopen", return_value=FakeResponse(lines=lines, content_type="text/event-stream")):
        turn = cohere_v2.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert turn.provider_context["data"]["message"]["id"] == "resp1"
    assert turn.usage.input_tokens == 10


def test_responses_api_stream_roundtrip_uses_terminal_response() -> None:
    provider = _provider("openai-responses", base="https://api.openai.test/v1", stream=True)
    final = {
        "id": "resp1",
        "model": "served-responses",
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "private"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]},
            {"type": "function_call", "call_id": "c1", "name": "lookup", "arguments": "{}"},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 4},
    }
    lines = _sse(
        {"type": "response.output_text.delta", "delta": "hel"},
        {"type": "response.output_text.delta", "delta": "lo"},
        {"type": "response.completed", "response": final},
        "[DONE]",
    )
    seen: list[str] = []
    with patch.object(responses_api.urllib.request, "urlopen", return_value=FakeResponse(lines=lines, content_type="text/event-stream")):
        turn = responses_api.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, tools=_tool())
    _assert_turn(turn, seen)
    assert "".join(seen) == "hello"
    assert turn.provider_context["data"]["response_id"] == "resp1"
    assert turn.usage.input_tokens == 10


def test_reasoning_callbacks_cover_native_provider_shapes() -> None:
    cases = []

    # OpenAI-compatible reasoning_content (used by custom/compatible gateways).
    cases.append((
        openai_compatible,
        _provider("openai-compatible", stream=True),
        "m1",
        _sse(
            {"choices": [{"delta": {"reasoning_content": "private", "content": "hello"}}]},
            "[DONE]",
        ),
    ))

    # Qwen / MiMo / Kimi / GLM share reasoning_content on their native chat wires.
    cases.extend([
        (qwen_chat, _provider("qwen-chat-v1", stream=True), "m1", _sse({"choices": [{"delta": {"reasoning_content": "private", "content": "hello"}}]}, "[DONE]")),
        (xiaomi_mimo, _provider("xiaomi-mimo-chat", base="https://api.xiaomimimo.com/v1", stream=True), "mimo-v2.6-pro", _sse({"choices": [{"delta": {"reasoning_content": "private", "content": "hello"}}]}, "[DONE]")),
        (moonshot_chat, _provider("moonshot-chat-v1", stream=True), "m1", _sse({"choices": [{"delta": {"reasoning_content": "private", "content": "hello"}}]}, "[DONE]")),
        (zhipu_chat, _provider("zhipu-chat-v4", stream=True), "m1", _sse({"choices": [{"delta": {"reasoning_content": "private", "content": "hello"}}]}, "[DONE]")),
    ])

    for module, provider, model_id, lines in cases:
        seen: list[str] = []
        thoughts: list[str] = []
        with patch.object(module.urllib.request, "urlopen", return_value=FakeResponse(lines=lines, content_type="text/event-stream")):
            turn = module.chat_completion_turn(
                provider,
                model_id,
                [{"role": "user", "content": "hi"}],
                seen.append,
                on_reasoning=thoughts.append,
            )
        assert "".join(seen) == "hello"
        assert "".join(thoughts) == "private"
        assert turn.text == "hello"


def test_reasoning_callbacks_cover_structured_native_transports() -> None:
    # Mistral ThinkChunk.
    seen: list[str] = []
    thoughts: list[str] = []
    provider = _provider("mistral-chat-v1", stream=True)
    lines = _sse(
        {"choices": [{"delta": {"content": [{"type": "thinking", "thinking": [{"type": "text", "text": "private"}]}, {"type": "text", "text": "hello"}]}}]},
        "[DONE]",
    )
    with patch.object(mistral_chat.urllib.request, "urlopen", return_value=FakeResponse(lines=lines, content_type="text/event-stream")):
        mistral_chat.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, on_reasoning=thoughts.append)
    assert "".join(seen) == "hello"
    assert "".join(thoughts) == "private"

    # Anthropic thinking_delta.
    seen.clear(); thoughts.clear()
    provider = _provider("anthropic-messages", base="https://api.anthropic.test/v1", stream=True)
    lines = _sse(
        {"type": "message_start", "message": {"id": "msg1", "model": "claude", "usage": {}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "private"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "hello"}},
    )
    with patch.object(anthropic_messages.urllib.request, "urlopen", return_value=FakeResponse(lines=lines, content_type="text/event-stream")):
        anthropic_messages.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, on_reasoning=thoughts.append)
    assert "".join(seen) == "hello"
    assert "".join(thoughts) == "private"

    # Gemini thought summary.
    seen.clear(); thoughts.clear()
    provider = _provider("gemini-interactions", base="https://generativelanguage.googleapis.test/v1beta", stream=True)
    lines = _sse(
        {"event_type": "step.start", "index": 0, "step": {"type": "thought", "summary": []}},
        {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_summary", "content": {"type": "text", "text": "private"}}},
        {"event_type": "step.start", "index": 1, "step": {"type": "model_output", "content": []}},
        {"event_type": "step.delta", "index": 1, "delta": {"type": "text", "text": "hello"}},
    )
    with patch.object(gemini_interactions.urllib.request, "urlopen", return_value=FakeResponse(lines=lines, content_type="text/event-stream")):
        gemini_interactions.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, on_reasoning=thoughts.append)
    assert "".join(seen) == "hello"
    assert "".join(thoughts) == "private"

    # Cohere thinking content.
    seen.clear(); thoughts.clear()
    provider = _provider("cohere-v2", base="https://api.cohere.test/v2", stream=True)
    lines = _sse(
        {"type": "content-delta", "delta": {"message": {"content": {"thinking": "private"}}}},
        {"type": "content-delta", "delta": {"message": {"content": {"text": "hello"}}}},
    )
    with patch.object(cohere_v2.urllib.request, "urlopen", return_value=FakeResponse(lines=lines, content_type="text/event-stream")):
        cohere_v2.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, on_reasoning=thoughts.append)
    assert "".join(seen) == "hello"
    assert "".join(thoughts) == "private"

    # Responses API exposes summaries rather than hidden chain-of-thought.
    seen.clear(); thoughts.clear()
    provider = _provider("openai-responses", base="https://api.openai.test/v1", stream=True)
    final = {
        "id": "resp1",
        "model": "m1",
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "private"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]},
        ],
    }
    lines = _sse({"type": "response.output_text.delta", "delta": "hello"}, {"type": "response.completed", "response": final}, "[DONE]")
    with patch.object(responses_api.urllib.request, "urlopen", return_value=FakeResponse(lines=lines, content_type="text/event-stream")):
        responses_api.chat_completion_turn(provider, "m1", [{"role": "user", "content": "hi"}], seen.append, on_reasoning=thoughts.append)
    assert "".join(seen) == "hello"
    assert "".join(thoughts) == "private"
