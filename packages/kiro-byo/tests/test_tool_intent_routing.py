from __future__ import annotations

from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.mode_router import infer_tools_required, route_model


def _tool_schema(name: str = "web_search") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "test tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _mixed_capability_config() -> KiroByoConfig:
    fast_no_tools = ModelSpec(
        "p", "P", "fast", "Fast",
        tier="fast", tool_use=False,
        routing_speed=0.99, routing_economy=0.99, routing_quality=0.55,
        input_modalities=("text",),
    )
    strong_tools = ModelSpec(
        "p", "P", "strong", "Strong Pro",
        tier="strong", tool_use=True,
        routing_speed=0.35, routing_economy=0.30, routing_quality=0.98,
        input_modalities=("text",),
    )
    provider = ProviderSpec(
        "p", "P", "openai-compatible", "https://example.invalid/v1", "",
        (fast_no_tools, strong_tools),
    )
    return KiroByoConfig((provider,), mode_pack="tiga", mode_type="sky")


def test_tool_availability_does_not_make_a_casual_turn_require_tools() -> None:
    tools = [_tool_schema()]
    assert infer_tools_required("你好", tools=tools) is False
    assert infer_tools_required("hello", tools=tools) is False
    assert infer_tools_required("こんにちは", tools=tools) is False


def test_explicit_external_actions_are_tool_requirements_across_common_languages() -> None:
    tools = [_tool_schema()]
    assert infer_tools_required("请帮我搜索一下今天的官方消息", tools=tools) is True
    assert infer_tools_required("Search the web for the current release notes", tools=tools) is True
    assert infer_tools_required("ウェブで最新のリリースノートを検索して", tools=tools) is True
    assert infer_tools_required("Please call web_search for this", tools=tools) is True


def test_file_or_shell_action_requires_a_tool_but_plain_analysis_does_not() -> None:
    assert infer_tools_required("读取 /tmp/report.txt 然后总结", tools=[_tool_schema("fs_read")]) is True
    assert infer_tools_required("Run the command pytest -q", tools=[_tool_schema("shell")]) is True
    assert infer_tools_required("分析一下这个算法为什么是 O(n log n)", tools=[_tool_schema()]) is False


def test_natural_local_inspection_requests_require_tools_without_overtriggering() -> None:
    tools = [_tool_schema("fs_read")]
    assert infer_tools_required("帮我看看 C 盘哪些东西可以清理", tools=tools) is True
    assert infer_tools_required("帮我看看C盘哪些东西可以清理", tools=tools) is True
    assert infer_tools_required("Take a look at the project directory and find the biggest cache folders", tools=tools) is True
    assert infer_tools_required("プロジェクトのフォルダを見て、大きいキャッシュを探して", tools=tools) is True
    assert infer_tools_required("帮我看看这个算法为什么是 O(n log n)", tools=tools) is False
    assert infer_tools_required("Explain Python type annotations", tools=tools) is False
    assert infer_tools_required("帮我看看 CPU cache 为什么会影响性能", tools=tools) is False
    assert infer_tools_required("帮我看看这个工程学问题怎么理解", tools=tools) is False
    assert infer_tools_required("Review this project proposal for logical gaps", tools=tools) is False


def test_tool_continuation_requires_recent_real_tool_activity() -> None:
    assert infer_tools_required("继续", tools=[_tool_schema()], history=[{"role": "assistant", "content": "draft"}]) is False
    assert infer_tools_required(
        "继续", tools=[_tool_schema()],
        history=[{"role": "tool", "tool_call_id": "c1", "content": "result"}],
    ) is True


def test_windows_drive_inspection_routes_to_a_tool_capable_model_end_to_end() -> None:
    prompt = "帮我看看 C 盘哪些东西可以清理"
    required = infer_tools_required(prompt, tools=[_tool_schema("fs_read")])
    decision = route_model(
        _mixed_capability_config(), pack="tiga", policy="multi",
        prompt=prompt, history=[], has_image=False,
        tools_available=True, tools_required=required,
    )
    assert required is True
    assert decision is not None
    assert decision.model.model_id == "strong"
    assert decision.model.tool_use is True


def test_router_does_not_exclude_toolless_model_just_because_tools_are_available() -> None:
    decision = route_model(
        _mixed_capability_config(), pack="tiga", policy="sky",
        prompt="hello", history=[], has_image=False,
        tools_available=True, tools_required=False,
    )
    assert decision is not None
    assert decision.model.model_id == "fast"
    assert decision.signals.tools_available is True
    assert decision.signals.tools_required is False


def test_router_gates_toolless_model_when_this_turn_really_needs_an_available_tool() -> None:
    decision = route_model(
        _mixed_capability_config(), pack="tiga", policy="sky",
        prompt="Search the web for the current release", history=[], has_image=False,
        tools_available=True, tools_required=True,
    )
    assert decision is not None
    assert decision.model.model_id == "strong"
    assert all(row.model.tool_use is not False for row in decision.candidates)
    assert decision.signals.tools_required is True


def test_unavailable_tools_do_not_create_an_impossible_model_capability_gate() -> None:
    decision = route_model(
        _mixed_capability_config(), pack="tiga", policy="sky",
        prompt="Search the web for the current release", history=[], has_image=False,
        tools_available=False, tools_required=True,
    )
    assert decision is not None
    assert decision.model.model_id == "fast"
    assert decision.signals.tools_required is True
    assert decision.signals.tools_available is False


def test_legacy_tools_expected_argument_keeps_old_callers_source_compatible() -> None:
    decision = route_model(
        _mixed_capability_config(), pack="tiga", policy="sky",
        prompt="legacy", history=[], has_image=False, tools_expected=True,
    )
    assert decision is not None
    assert decision.signals.tools_required is True
    assert decision.signals.tools_available is True
    assert decision.signals.tools_expected is True
