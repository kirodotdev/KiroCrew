"""R11: UI Auto must follow conversation for visible ACP tool labels."""
from types import SimpleNamespace

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.tool_display import session_display_locale
from kiro_byo.reasoning_language import resolve_reasoning_language


def _native_session():
    session = Session("s", ".", "model")
    session.native = SimpleNamespace(
        has_tool=lambda name: name == "read",
        canonical_name=lambda name: name,
        kind_for=lambda name: "read",
    )
    return session


def test_auto_title_uses_resolved_user_language_not_english_host(monkeypatch):
    monkeypatch.setattr("kiro_byo.tool_display.system_locale.getlocale", lambda: ("en_US", "UTF-8"))
    session = _native_session()
    session.ui_language_preference = "auto"
    session.response_language = "zh-CN"
    label = object.__new__(AcpServer)._tool_call_payload(session, "id", "read", {"path": "foo.txt"})
    assert label["title"] == "读取文件"
    assert label["rawInput"] == {"path": "foo.txt"}
    assert label["_meta"]["kiro"]["toolId"] == "read"


def test_fixed_ui_language_has_priority_over_conversation_language():
    session = _native_session()
    session.ui_language_preference = "ja"
    session.response_language = "zh-CN"
    assert session_display_locale(session) == "ja"
    label = object.__new__(AcpServer)._tool_call_payload(session, "id", "read", {"path": "foo.txt"})
    assert label["title"] == "ファイルを読み取る"


def test_auto_uses_reasoning_if_no_response_resolved(monkeypatch):
    monkeypatch.setattr("kiro_byo.tool_display.system_locale.getlocale", lambda: ("en_US", "UTF-8"))
    session = _native_session()
    session.response_language = ""
    session.reasoning_language = "zh-TW"
    assert session_display_locale(session) == "zh-TW"


def test_progress_prompt_includes_tool_steps_not_raw_arguments():
    instruction = resolve_reasoning_language("zh-CN", "帮我做一个计划").instruction
    assert "update_plan" in instruction
    assert "tool actions" in instruction
    assert "commands, paths" in instruction
    assert "do not translate" in instruction


def test_unknown_model_tool_names_are_not_misrepresented():
    session = _native_session()
    session.ui_language_preference = "zh-CN"
    payload = object.__new__(AcpServer)._tool_call_payload(session, "id", "custom_tool", {"command": "ls"})
    assert payload["title"] == "custom_tool"
    assert payload["rawInput"] == {"command": "ls"}


def test_real_turn_language_policy_flows_to_acp_display(monkeypatch):
    monkeypatch.setattr("kiro_byo.tool_display.system_locale.getlocale", lambda: ("en_US", "UTF-8"))
    session = _native_session()
    session.ui_language_preference = "auto"
    session.response_language_preference = "auto"
    server = object.__new__(AcpServer)
    server.runtime_trace = None
    server._apply_reasoning_language_policy(session, "auto", "帮我读取这个文件")
    assert session.response_language == "zh-CN"
    assert server._tool_call_payload(session, "a", "read", {"path": "x.txt"})["title"] == "读取文件"
    server._apply_reasoning_language_policy(session, "auto", "Please read the next file")
    assert session.response_language == "en"
    assert server._tool_call_payload(session, "b", "read", {"path": "x.txt"})["title"] == "Read file"
