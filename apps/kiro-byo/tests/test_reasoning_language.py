from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import load_catalog_config
from kiro_byo.paths import use_app_home
from kiro_byo.provider_admin import reasoning_language_settings, set_reasoning_language_preference
from kiro_byo.reasoning_language import (
    classify_text_language,
    detect_conversation_language,
    normalize_reasoning_language,
    resolve_reasoning_language,
)


class ReasoningLanguagePolicyTests(unittest.TestCase):
    def test_chinese_with_technical_terms_stays_chinese(self) -> None:
        text = "帮我检查 ACP session 的 configOptions，看看 LLM 的 Token 为什么这么高"
        self.assertEqual(classify_text_language(text), "zh-CN")
        decision = resolve_reasoning_language("auto", text, [])
        self.assertEqual(decision.language, "zh-CN")
        self.assertIn("Simplified Chinese", decision.instruction)
        self.assertIn("ACP, LLM, Token, API", decision.instruction)
        self.assertIn("natural/original spelling", decision.instruction)

    def test_mixed_short_chinese_is_not_misclassified_as_english(self) -> None:
        history = [{"role": "user", "content": "我们继续检查 KiroCrew 的模型路由。"}]
        language, source = detect_conversation_language("check一下 ACP", history)
        self.assertEqual(language, "zh-CN")
        self.assertEqual(source, "current-message")

    def test_technical_only_short_turn_uses_recent_user_language(self) -> None:
        history = [
            {"role": "user", "content": "这个 Provider 的 reasoning 配置是不是有问题？"},
            {"role": "assistant", "content": "..."},
        ]
        language, source = detect_conversation_language("ACP?", history)
        self.assertEqual(language, "zh-CN")
        self.assertEqual(source, "conversation-history")

    def test_english_and_japanese_current_messages_win(self) -> None:
        history = [{"role": "user", "content": "之前我们一直用中文聊。"}]
        self.assertEqual(
            detect_conversation_language("Can you inspect why ACP reconnect keeps failing?", history)[0],
            "en",
        )
        self.assertEqual(
            detect_conversation_language("ACP の reconnect が失敗する理由を確認して", history)[0],
            "ja",
        )

    def test_traditional_chinese_is_detected_without_translating_terms(self) -> None:
        text = "請檢查這個 ACP Provider，看看 Token 顯示是否有問題"
        self.assertEqual(classify_text_language(text), "zh-TW")
        decision = resolve_reasoning_language("auto", text, [])
        self.assertEqual(decision.language, "zh-TW")
        self.assertIn("Traditional Chinese", decision.instruction)
        self.assertIn("ACP, LLM, Token, API", decision.instruction)

    def test_simplified_common_words_are_not_traditional(self) -> None:
        for text in (
            "路由优先级检查后再开始处理", "先看这个文件路径和档案",
            "请检查模型配置，为什么回复会变成繁体中文？",
            "显示的推理语言不对，先修改一下", "这个模型能正常工作吗？",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_text_language(text), "zh-CN")
                self.assertEqual(resolve_reasoning_language("auto", text).language, "zh-CN")

    def test_traditional_script_survives_neutral_followup(self) -> None:
        history = [{"role": "user", "content": "請檢查這個設定是否有問題"}]
        self.assertEqual(detect_conversation_language("你好", history)[0], "zh-TW")
        self.assertEqual(detect_conversation_language("ACP?", history)[0], "zh-TW")
        self.assertEqual(
            detect_conversation_language("你好", [], fallback_language="zh-TW")[0],
            "zh-TW",
        )

    def test_simplified_script_survives_neutral_followup(self) -> None:
        history = [{"role": "user", "content": "帮我看看路由为什么有问题"}]
        self.assertEqual(detect_conversation_language("你好", history)[0], "zh-CN")
        self.assertEqual(detect_conversation_language("API?", history)[0], "zh-CN")
        self.assertEqual(
            detect_conversation_language("API?", [], fallback_language="zh-CN"),
            ("zh-CN", "ui-language-fallback"),
        )

    def test_auto_reasoning_follows_explicit_answer_language(self) -> None:
        decision = resolve_reasoning_language("auto", "请用英文回答这个问题")
        self.assertEqual(decision.language, "en")
        self.assertEqual(decision.source, "explicit-user-request")
        # Manual reasoning preference stays independent.
        self.assertEqual(
            resolve_reasoning_language("zh-CN", "Please respond in English.").language,
            "zh-CN",
        )

    def test_strong_simplified_and_traditional_output_instructions(self) -> None:
        sim = resolve_reasoning_language("auto", "帮我检查一下")
        trad = resolve_reasoning_language("auto", "請幫我檢查一下")
        self.assertIn("not Traditional Chinese", sim.instruction)
        self.assertIn("not Simplified Chinese", trad.instruction)

    def test_policy_explicitly_targets_visible_reasoning_not_hidden_chain(self) -> None:
        decision = resolve_reasoning_language("zh-CN", "测试", [])
        self.assertIn("all user-visible", decision.instruction)
        self.assertIn("do not default to English", decision.instruction)
        self.assertIn("not a request to reveal hidden/private chain-of-thought", decision.instruction)
        self.assertIn("mathematical notation", decision.instruction)

    def test_manual_locale_override_is_independent_of_user_text(self) -> None:
        decision = resolve_reasoning_language("ja", "帮我看看这个 API", [])
        self.assertEqual(decision.language, "ja")
        self.assertEqual(decision.source, "configured")
        self.assertIn("Japanese (ja)", decision.instruction)

    def test_future_valid_locale_is_preserved(self) -> None:
        self.assertEqual(normalize_reasoning_language("fr-FR"), "fr-FR")
        self.assertEqual(normalize_reasoning_language("not a locale"), "auto")

    def test_turn_uses_explicit_ui_locale_only_as_last_resort(self) -> None:
        server = object.__new__(AcpServer)
        server.runtime_trace = None
        session = Session("locale", ".", "model")
        session.ui_language_preference = "zh-CN"
        session.response_language_preference = "auto"
        server._apply_reasoning_language_policy(session, "auto", "ACP?")
        self.assertEqual(session.reasoning_language, "zh-CN")
        self.assertEqual(session.response_language, "zh-CN")
        self.assertIn("not Traditional Chinese", session.language_instruction)
        # The UI choice is just a fallback: an explicit English user message wins.
        server._apply_reasoning_language_policy(session, "auto", "Could you explain how the API works?")
        self.assertEqual(session.reasoning_language, "en")
        self.assertEqual(session.response_language, "en")

    def test_response_and_visible_reasoning_match_explicit_language_request(self) -> None:
        server = object.__new__(AcpServer)
        server.runtime_trace = None
        session = Session("explicit", ".", "model")
        server._apply_reasoning_language_policy(session, "auto", "请用日语回复这个问题")
        self.assertEqual(session.reasoning_language, "ja")
        self.assertEqual(session.response_language, "ja")
        self.assertIn("Japanese (ja)", session.language_instruction)

    def test_runtime_system_message_carries_language_policy_without_renaming_agent(self) -> None:
        provider = SimpleNamespace(name="Demo Provider")
        model = SimpleNamespace(name="Demo Model", model_id="demo-1")
        decision = resolve_reasoning_language("auto", "检查 ACP 和 Token", [])
        msg = AcpServer._runtime_system_message(
            provider,
            model,
            reasoning_language_context=decision.instruction,
        )
        self.assertEqual(msg["role"], "system")
        self.assertIn("Reasoning presentation language", msg["content"])
        self.assertIn("Simplified Chinese", msg["content"])
        self.assertIn("Runtime metadata for this turn", msg["content"])

    def test_turn_policy_is_ephemeral_and_can_follow_language_switch(self) -> None:
        server = object.__new__(AcpServer)
        server.runtime_trace = None
        session = Session("s", ".", "model")
        session.messages.append({"role": "user", "content": "先用中文继续。"})
        server._apply_reasoning_language_policy(session, "auto", "检查 ACP")
        self.assertEqual(session.reasoning_language, "zh-CN")
        server._apply_reasoning_language_policy(session, "auto", "Now continue in English and inspect the API.")
        self.assertEqual(session.reasoning_language, "en")


class ReasoningLanguageConfigTests(unittest.TestCase):
    def test_preference_persists_under_preferences_without_touching_provider_data(self) -> None:
        with tempfile.TemporaryDirectory() as td, use_app_home(Path(td)):
            home = Path(td)
            (home / "config").mkdir(parents=True, exist_ok=True)
            path = home / "config" / "providers.json"
            path.write_text(json.dumps({"providers": []}), encoding="utf-8")
            result = set_reasoning_language_preference("zh-CN")
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["reasoning_language"], "zh-CN")
            self.assertEqual(raw["preferences"]["reasoning_language"], "zh-CN")
            self.assertEqual(raw["providers"], [])
            self.assertEqual(load_catalog_config().reasoning_language, "zh-CN")
            self.assertEqual(reasoning_language_settings()["reasoning_language"], "zh-CN")


if __name__ == "__main__":
    unittest.main()
