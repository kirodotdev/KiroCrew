"""Kiro-style, per-call tool activity descriptions are UI-only metadata."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.i18n import LOCALES, locale_messages
from kiro_byo.native_specs import NATIVE_TOOL_SPECS
from kiro_byo.native_tools import NativeToolRuntime, ToolValidationError
from kiro_byo.tool_display import tool_display_title
from kiro_byo.turn_runner import TurnRunnerMixin


EXAMPLES = {
    "zh-CN": "查看 Git 仓库状态",
    "zh-TW": "檢查 Git 儲存庫狀態",
    "en": "Check Git repository status",
    "ja": "Git リポジトリの状態を確認",
    "ko": "Git 저장소 상태 확인",
}


@pytest.fixture
def runtime(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    instance = NativeToolRuntime(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("language,expected", EXAMPLES.items())
def test_specific_action_description_preferred_for_each_ui_language(language, expected):
    args = {"command": "git status --short", "description": expected}
    session = Session("sid", ".", "model")
    session.ui_language_preference = language
    session.response_language_preference = "en" if language != "en" else "ko"
    session.native = SimpleNamespace(
        has_tool=lambda n: n == "shell", canonical_name=lambda n: n,
        kind_for=lambda n: "execute",
    )
    result = object.__new__(AcpServer)._tool_call_payload(
        session, "tool-1", "shell", args, status="in_progress"
    )
    assert result["title"] == expected
    assert result["kind"] == "execute"
    assert result["rawInput"] is args
    assert result["rawInput"]["command"] == "git status --short"
    assert result["_meta"]["kiro"]["toolId"] == "shell"
    assert result["status"] == "in_progress"


@pytest.mark.parametrize("language", LOCALES)
def test_provider_tool_schema_requests_per_call_description_in_ui_language(runtime, language):
    tools = runtime.tool_specs({"shell", "read"}, ui_language=language)
    assert len(tools) == 2
    for item in tools:
        spec = item["function"]["parameters"]
        assert "description" in spec["properties"]
        assert spec["properties"]["description"]["maxLength"] == 160
        assert spec["properties"]["description"]["description"] == locale_messages(language)["tool.description.hint"]
        assert "description" not in spec["required"]
        assert spec["additionalProperties"] is False


def test_ui_language_schema_does_not_leak_between_sessions(runtime):
    cn = runtime.tool_specs({"read"}, ui_language="zh-CN")
    jp = runtime.tool_specs({"read"}, ui_language="ja")
    assert cn[0]["function"]["parameters"]["properties"]["description"]["description"] != jp[0]["function"]["parameters"]["properties"]["description"]["description"]
    assert cn[0]["function"]["parameters"]["properties"]["description"]["description"] == locale_messages("zh-CN")["tool.description.hint"]
    # Never mutate a shared native spec: otherwise simultaneous sessions leak locale.
    assert NATIVE_TOOL_SPECS[0].input_schema["properties"]["description"]["description"].startswith("Optional")


def test_active_tools_for_turn_uses_ui_preference_not_response_language(runtime):
    session = Session("sid", ".", "model", native=runtime)
    session.ui_language_preference = "ja"
    session.response_language = "zh-CN"
    # Native visibility decisions are still owned by the original model policy.
    tools = TurnRunnerMixin._active_tools_for_turn(session)
    chosen = next(t for t in tools if t["function"]["name"] == "read")
    assert chosen["function"]["parameters"]["properties"]["description"]["description"] == locale_messages("ja")["tool.description.hint"]


def test_description_remains_optional_and_is_not_executed(runtime):
    original = {"path": "README.md"}
    presented = {"path": "README.md", "description": "读取项目说明文件"}
    base = runtime.execute(runtime.validate("read", original))
    extra = runtime.execute(runtime.validate("read", presented))
    assert base == extra
    assert runtime.validate("read", presented).arguments["description"] == "读取项目说明文件"
    assert "description" not in runtime.validate("read", original).arguments
    for invalid in ([], 5, {"text": "hello"}, "x" * 161):
        with pytest.raises(ToolValidationError, match="description"):
            runtime.validate("read", {"path": "README.md", "description": invalid})
    with pytest.raises(ToolValidationError, match="unexpected"):
        runtime.validate("update_plan", {"operation": "get", "description": "foo"})


@pytest.mark.parametrize("bad", [
    "git status --short", "curl https://example.com", "echo $TOKEN",
    "Run terminal command; rm -rf .", "Bearer abcdef123456",
    "API_KEY=secret123", "normal\nsecond line", "Hello\u202e world",
    "x" * 121, ["not a string"],
])
def test_unsafe_or_nonlocalizable_hint_falls_back_to_trusted_title(bad):
    assert tool_display_title(
        "shell", ui_language="en", native_name="shell", description=bad,
    ) == "Run terminal command"


def test_cross_language_hint_falls_back_to_selected_ui_language():
    assert tool_display_title("shell", ui_language="zh-CN", native_name="shell", description="Check repository status") == "执行终端命令"
    assert tool_display_title("shell", ui_language="en", native_name="shell", description="查看 Git 仓库状态") == "Run terminal command"


def test_mcp_description_never_overrides_identity_or_title():
    public = "mcp__server__tool"
    binding = SimpleNamespace(server_name="server", tool_name="tool", annotations={})
    session = Session("s", ".", "model", mcp=SimpleNamespace(bindings={public: binding}))
    session.ui_language_preference = "zh-CN"
    args = {"description": "欺骗性说明", "token": "secret"}
    result = object.__new__(AcpServer)._tool_call_payload(session, "call", public, args)
    assert result["title"] == "调用 MCP 工具：tool"
    assert result["rawInput"] is args
    assert result["_meta"]["kiro"]["mcpTool"]["identity"]["serverName"] == "server"


def test_display_description_cannot_evade_native_stagnation_guard():
    from kiro_byo.execution_guard import ExecutionGuard

    session = SimpleNamespace(native=SimpleNamespace(has_tool=lambda n: n == "shell"))
    runner = TurnRunnerMixin()
    guard = ExecutionGuard()
    decisions = []
    for index in range(4):
        decision, _, _ = runner._observe_tool_execution_guard(
            session, guard, round_role="lead",
            call=SimpleNamespace(name="shell"),
            arguments={"command": "git status --short", "description": f"Check repository {index}"},
            result_text="clean", step=index, provider_rounds_total=index,
        )
        decisions.append(decision.action)
    assert decisions == ["continue", "continue", "continue", "warn"]


def test_display_description_is_not_stripped_from_mcp_guard_signature():
    from kiro_byo.execution_guard import ExecutionGuard
    session = SimpleNamespace(native=SimpleNamespace(has_tool=lambda n: False))
    runner = TurnRunnerMixin()
    guard = ExecutionGuard()
    for index in range(4):
        decision, _, _ = runner._observe_tool_execution_guard(
            session, guard, round_role="lead",
            call=SimpleNamespace(name="mcp__example__tool"),
            arguments={"description": f"semantic input {index}"},
            result_text="same", step=index, provider_rounds_total=index,
        )
    assert decision.action == "continue"
