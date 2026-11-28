"""R12 regression matrix: a single turn-level presentation decision."""
from types import SimpleNamespace

import pytest

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.presentation_language import resolve_presentation_language
from kiro_byo.tool_display import session_display_locale


@pytest.mark.parametrize("ui,answer,text,expected,source", [
    ("zh-CN", "auto", "Could you explain this API?", "zh-CN", "ui-preference"),
    ("zh-TW", "auto", "Could you explain this API?", "zh-TW", "ui-preference"),
    ("ja", "auto", "解释 ACP 的原理", "ja", "ui-preference"),
    ("auto", "zh-CN", "Please explain this API", "zh-CN", "response-preference"),
    ("auto", "auto", "帮我检查文件", "zh-CN", "current-message"),
    ("auto", "auto", "日本語の文章を直してください", "ja", "current-message"),
    ("auto", "auto", "Please fix the tests in this repository", "en", "current-message"),
    ("zh-CN", "auto", "请用日语回答。", "ja", "explicit-user-request"),
    ("auto", "zh-CN", "Please respond in English.", "en", "explicit-user-request"),
])
def test_one_primary_language(ui, answer, text, expected, source):
    decision = resolve_presentation_language(text, ui_preference=ui, response_preference=answer)
    assert decision.language == expected
    assert decision.source == source
    assert decision.response_language == expected
    assert decision.reasoning_language == expected
    assert f"({expected})" in decision.response_instruction
    assert f"({expected})" in decision.reasoning_instruction


def test_explicit_reasoning_override_is_intentional_not_an_auto_drift():
    decision = resolve_presentation_language(
        "帮我检查", ui_preference="zh-CN", reasoning_preference="ja",
    )
    assert decision.response_language == "zh-CN"
    assert decision.reasoning_language == "ja"
    assert decision.reasoning_source == "reasoning-preference"
    assert decision.tool_display_language == "zh-CN"


def test_fixed_ui_tool_language_and_current_turn_response_agree():
    session = Session("t", ".", "model")
    session.ui_language_preference = "zh-CN"
    session.response_language_preference = "auto"
    session.native = SimpleNamespace(
        has_tool=lambda name: name == "read",
        canonical_name=lambda name: name,
        kind_for=lambda name: "read",
    )
    server = object.__new__(AcpServer)
    server.runtime_trace = None
    server._apply_reasoning_language_policy(session, "auto", "Please check the logs")
    assert (session.presentation_language, session.reasoning_language,
            session.response_language, session_display_locale(session)) == ("zh-CN",)*4
    payload = server._tool_call_payload(session, "id", "read", {"path": "README.md"})
    assert payload["title"] == "读取文件"
    assert payload["rawInput"] == {"path": "README.md"}
    assert payload["_meta"]["kiro"]["toolId"] == "read"


def test_auto_tool_language_changes_between_turns_and_child_inherits():
    session = Session("x", ".", "model")
    session.ui_language_preference = "auto"
    session.response_language_preference = "auto"
    server = object.__new__(AcpServer)
    server.runtime_trace = None
    server._apply_reasoning_language_policy(session, "auto", "请检查一下配置")
    assert session_display_locale(session) == "zh-CN"
    # An isolated subagent inherits current display context rather than
    # detecting the language of its task prompt independently.
    task = SimpleNamespace(cancel_event=__import__("threading").Event())
    child = server._subagent_child_session(session, task, [])
    assert child.presentation_language == "zh-CN"
    assert child.tool_display_language == "zh-CN"
    assert session_display_locale(child) == "zh-CN"
    server._apply_reasoning_language_policy(session, "auto", "Please check the config file")
    assert session.presentation_language == "en"
    assert session_display_locale(session) == "en"
    assert child.presentation_language == "zh-CN"  # Snapshot, not shared mutable state


def test_neutral_turn_follows_user_history_without_os_locale():
    history = [{"role": "user", "content": "继续做中文版本的工作。"}]
    decision = resolve_presentation_language("ACP?", history)
    assert decision.language == "zh-CN"
    assert decision.tool_display_language == "zh-CN"
