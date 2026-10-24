from __future__ import annotations

import tempfile
import unittest
import urllib.error
from unittest.mock import patch
from pathlib import Path

from kiro_byo.config import ConfigError, ModelSpec, ProviderSpec, _load_model, _load_provider
from kiro_byo.provider_catalog import official_model_profile, provider_preset
from kiro_byo.providers import anthropic_messages, cohere_v2, gemini_interactions, mistral_chat, moonshot_chat, openai_compatible, qwen_chat, responses_api, xiaomi_mimo, zhipu_chat
from kiro_byo.providers.base import ProviderTransportError
from kiro_byo.providers.registry import _effective_provider


def provider(kind: str, *, preset_id: str = "custom", base: str = "https://example.test/v1") -> ProviderSpec:
    return ProviderSpec(
        id="p1",
        name="Provider",
        type=kind,
        base_url=base,
        api_key="secret",
        models=(),
        preset_id=preset_id,
        stream=True,
    )


def model(*, transport: str = "", levels: tuple[str, ...] = (), disable: str = "", can_disable: bool | None = None, mode: str = "levels", max_output: int | None = None) -> ModelSpec:
    return ModelSpec(
        provider_id="p1",
        provider_name="Provider",
        model_id="m1",
        name="m1",
        transport=transport,
        effort_supported_levels=levels,
        effort_default_level=levels[-1] if levels else "",
        reasoning_mode=mode,
        reasoning_supported=True if levels or can_disable is not None else None,
        reasoning_can_disable=can_disable,
        reasoning_disable_value=disable,
        max_output_tokens=max_output,
    )


class NativeProviderTransportTests(unittest.TestCase):
    def test_official_provider_transport_matrix(self) -> None:
        expected = {
            "deepseek": "deepseek-responses",
            "openai": "openai-responses",
            "anthropic": "anthropic-messages",
            "gemini": "gemini-interactions",
            "grok": "xai-responses",
            "volcengine": "ark-responses",
            "cohere": "cohere-v2",
            "groq": "groq-responses",
            "mistral": "mistral-chat-v1",
            "moonshot": "moonshot-chat-v1",
            "xiaomi": "xiaomi-mimo-chat",
            "xiaomi-tokenplan": "xiaomi-mimo-chat",
            "minimax": "minimax-messages",
            "qianfan": "qianfan-responses",
            "zhipu": "zhipu-chat-v4",
            "qwen": "qwen-chat-v1",
        }
        for preset_id, transport in expected.items():
            with self.subTest(preset=preset_id):
                self.assertEqual(provider_preset(preset_id).transport, transport)
        self.assertTrue(provider_preset("qwen").model_transport_overrides)
        self.assertTrue(provider_preset("hunyuan").model_transport_overrides)
        self.assertEqual(provider_preset("hunyuan").default_base_url, "https://tokenhub.tencentmaas.com/v1")


    def test_openai_current_reasoning_profiles_keep_off_separate(self) -> None:
        astra = official_model_profile("openai", "gpt-6-astra")
        self.assertEqual(astra["effort_supported_levels"], ["low", "medium", "high", "xhigh", "max"])
        self.assertFalse(astra["reasoning"]["can_disable"])
        luna = official_model_profile("openai", "gpt-6-luna")
        self.assertTrue(luna["reasoning"]["can_disable"])
        self.assertEqual(luna["reasoning"]["disable_value"], "none")
        self.assertNotIn("none", luna["effort_supported_levels"])
        self.assertEqual(luna["context_window"], 1_050_000)
        self.assertEqual(luna["max_output_tokens"], 128_000)

    def test_xai_profiles_match_model_specific_effort_matrix(self) -> None:
        g47 = official_model_profile("grok", "grok-4.7")
        self.assertEqual(g47["effort_supported_levels"], ["low", "medium", "high", "xhigh"])
        self.assertFalse(g47["reasoning"]["can_disable"])
        self.assertEqual(g47["context_window"], 500_000)
        g45 = official_model_profile("grok", "grok-4.5")
        self.assertEqual(g45["effort_supported_levels"], ["low", "medium", "high"])
        g43 = official_model_profile("grok", "grok-4.3")
        self.assertTrue(g43["reasoning"]["can_disable"])
        self.assertEqual(g43["reasoning"]["disable_value"], "none")

    def test_anthropic_always_on_and_switchable_models_differ(self) -> None:
        opus55 = official_model_profile("anthropic", "claude-opus-5-5")
        self.assertFalse(opus55["reasoning"]["can_disable"])
        fable = official_model_profile("anthropic", "claude-fable-5-1")
        self.assertFalse(fable["reasoning"]["can_disable"])
        haiku = official_model_profile("anthropic", "claude-haiku-5-5")
        self.assertTrue(haiku["reasoning"]["can_disable"])
        self.assertEqual(haiku["reasoning"]["disable_value"], "disabled")
        self.assertEqual(haiku["max_output_tokens"], 128_000)

        opus45 = official_model_profile("anthropic", "claude-opus-4-5-20251101")
        self.assertEqual(opus45["context_window"], 200_000)
        self.assertEqual(opus45["max_output_tokens"], 64_000)
        haiku45 = official_model_profile("anthropic", "claude-haiku-4-5-20251001")
        self.assertEqual(haiku45["context_window"], 200_000)
        self.assertEqual(haiku45["max_output_tokens"], 64_000)
        sonnet45 = official_model_profile("anthropic", "claude-sonnet-4-5-20250929")
        self.assertEqual(sonnet45["context_window"], 200_000)
        self.assertNotIn("max_output_tokens", sonnet45)

    def test_moonshot_profiles_and_native_body_keep_thinking_distinct(self) -> None:
        k3 = official_model_profile("moonshot", "kimi-k3")
        self.assertEqual(k3["effort_supported_levels"], ["low", "high", "max"])
        self.assertFalse(k3["reasoning"]["can_disable"])
        k27 = official_model_profile("moonshot", "kimi-k2.7-code")
        self.assertEqual(k27["reasoning"]["mode"], "fixed")
        k26 = official_model_profile("moonshot", "kimi-k2.6")
        self.assertTrue(k26["reasoning"]["can_disable"])
        self.assertEqual(k26["reasoning"]["disable_value"], "disabled")

        p = provider("moonshot-chat-v1", preset_id="moonshot", base="https://api.moonshot.cn/v1")
        m26 = ModelSpec(
            provider_id="p1", provider_name="Provider", model_id="kimi-k2.6", name="K2.6",
            reasoning_mode="toggle", reasoning_supported=True, reasoning_can_disable=True,
            reasoning_default_enabled=True, reasoning_enable_value="enabled", reasoning_disable_value="disabled",
        )
        on = moonshot_chat._body(p, "kimi-k2.6", [{"role":"user","content":"hi"}], reasoning_effort=None, thinking_mode="on", model=m26, tools=None)
        self.assertEqual(on["thinking"], {"type": "enabled", "keep": "all"})
        self.assertTrue(on["stream_options"]["include_usage"])
        off = moonshot_chat._body(p, "kimi-k2.6", [{"role":"user","content":"hi"}], reasoning_effort=None, thinking_mode="off", model=m26, tools=None)
        self.assertEqual(off["thinking"], {"type": "disabled"})
        fixed = ModelSpec(provider_id="p1", provider_name="Provider", model_id="kimi-k2.7-code", name="K2.7", reasoning_mode="fixed", reasoning_supported=True, reasoning_can_disable=False, reasoning_default_enabled=True)
        fixed_body = moonshot_chat._body(p, fixed.model_id, [{"role":"user","content":"hi"}], reasoning_effort=None, thinking_mode="on", model=fixed, tools=None)
        self.assertNotIn("thinking", fixed_body)
        self.assertNotIn("temperature", fixed_body)

    def test_moonshot_replays_native_reasoning_and_tool_calls(self) -> None:
        p = provider("moonshot-chat-v1", preset_id="moonshot", base="https://api.moonshot.cn/v1")
        native = {
            "role": "assistant", "content": None, "reasoning_content": "kept-thought",
            "tool_calls": [{"id":"c1","type":"function","function":{"name":"lookup","arguments":"{}"}}],
        }
        rows = moonshot_chat._messages(p, [{
            "role":"assistant", "content":"",
            "_kiro_provider_context":{"provider_id":"p1","transport":"moonshot-chat-v1","data":{"message":native}},
        }])
        self.assertEqual(rows[0]["reasoning_content"], "kept-thought")
        self.assertEqual(rows[0]["tool_calls"][0]["id"], "c1")



    def test_xiaomi_token_plan_request_uses_subscription_base_url(self) -> None:
        p = provider(
            "xiaomi-mimo-chat",
            preset_id="xiaomi-tokenplan",
            base="https://token-plan-cn.xiaomimimo.com/v1",
        )
        p = ProviderSpec(**{**p.__dict__, "api_key": "tp-test-key"})
        self.assertEqual(
            xiaomi_mimo._endpoint(p),
            "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
        )
        self.assertEqual(xiaomi_mimo._headers(p)["Authorization"], "Bearer tp-test-key")

    def test_xiaomi_mimo_profiles_and_native_body_preserve_agent_thinking(self) -> None:
        pro = official_model_profile("xiaomi", "mimo-v2.6-pro")
        self.assertEqual(pro["context_window"], 1_000_000)
        self.assertEqual(pro["max_output_tokens"], 131_072)
        self.assertTrue(pro["reasoning"]["can_disable"])
        self.assertEqual(pro["reasoning"]["enable_value"], "enabled")
        self.assertEqual(pro["reasoning"]["disable_value"], "disabled")
        self.assertTrue(pro["tool_use"])

        p = provider("xiaomi-mimo-chat", preset_id="xiaomi", base="https://api.xiaomimimo.com/v1")
        p = ProviderSpec(**{**p.__dict__, "temperature": 0.2})
        m = ModelSpec(
            provider_id="p1", provider_name="Provider", model_id="mimo-v2.6-pro", name="MiMo-V2.6-Pro",
            reasoning_mode="toggle", reasoning_supported=True, reasoning_can_disable=True,
            reasoning_default_enabled=True, reasoning_enable_value="enabled", reasoning_disable_value="disabled",
        )
        on = xiaomi_mimo._body(p, m.model_id, [{"role":"user","content":"hi"}], reasoning_effort=None, thinking_mode="on", model=m, tools=[{"type":"function","function":{"name":"lookup","parameters":{"type":"object","properties":{}}}}])
        self.assertEqual(on["thinking"], {"type": "enabled"})
        self.assertNotIn("temperature", on)
        self.assertEqual(on["tool_choice"], "auto")
        off = xiaomi_mimo._body(p, m.model_id, [{"role":"user","content":"hi"}], reasoning_effort=None, thinking_mode="off", model=m, tools=None)
        self.assertEqual(off["thinking"], {"type": "disabled"})
        self.assertEqual(off["temperature"], 0.2)

        native = {
            "role": "assistant", "content": None, "reasoning_content": "kept-thought",
            "tool_calls": [{"id":"c1","type":"function","function":{"name":"lookup","arguments":"{}"}}],
        }
        rows = xiaomi_mimo._messages(p, [{
            "role":"assistant", "content":"",
            "_kiro_provider_context":{"provider_id":"p1","transport":"xiaomi-mimo-chat","data":{"message":native}},
        }])
        self.assertEqual(rows[0]["reasoning_content"], "kept-thought")
        self.assertEqual(rows[0]["tool_calls"][0]["id"], "c1")


    def test_qwen_chat_preserves_reasoning_and_drives_verified_thinking_switch(self) -> None:
        open_qwen = official_model_profile("qwen", "qwen3-32b")
        self.assertEqual(open_qwen["reasoning"]["mode"], "toggle")
        self.assertTrue(open_qwen["reasoning"]["can_disable"])
        thinking_only = official_model_profile("qwen", "qwen3-30b-a3b-thinking-2507")
        self.assertEqual(thinking_only["reasoning"]["mode"], "fixed")
        self.assertFalse(thinking_only["reasoning"]["can_disable"])
        r38 = official_model_profile("qwen", "qwen3.8-max")
        self.assertEqual(r38["transport"], "qwen-responses")
        self.assertNotIn("none", r38["effort_supported_levels"])

        p = provider("qwen-chat-v1", preset_id="qwen", base="https://dashscope.aliyuncs.com/compatible-mode/v1")
        m = ModelSpec(
            provider_id="p1", provider_name="Provider", model_id="qwen3-32b", name="Qwen3 32B",
            reasoning_mode="toggle", reasoning_supported=True, reasoning_can_disable=True,
            reasoning_default_enabled=True, reasoning_enable_value="true", reasoning_disable_value="false",
        )
        on = qwen_chat._body(p, m.model_id, [{"role":"user","content":"hi"}], reasoning_effort=None, thinking_mode="on", model=m, tools=None)
        self.assertIs(on["enable_thinking"], True)
        self.assertTrue(on["stream_options"]["include_usage"])
        off = qwen_chat._body(p, m.model_id, [{"role":"user","content":"hi"}], reasoning_effort=None, thinking_mode="off", model=m, tools=None)
        self.assertIs(off["enable_thinking"], False)

        native = {"role":"assistant", "content":None, "reasoning_content":"keep", "tool_calls":[{"id":"c1","type":"function","function":{"name":"lookup","arguments":"{}"}}]}
        rows = qwen_chat._messages(p, [{"role":"assistant","content":"", "_kiro_provider_context":{"provider_id":"p1","transport":"qwen-chat-v1","data":{"message":native}}}])
        self.assertEqual(rows[0]["reasoning_content"], "keep")
        self.assertEqual(rows[0]["tool_calls"][0]["id"], "c1")

    def test_official_qwen_upgrades_vendor_chat_but_relay_stays_compatible(self) -> None:
        official = _load_provider({
            "id":"qwen", "name":"Qwen", "preset_id":"qwen",
            "type":"openai-compatible", "base_url":"https://dashscope.aliyuncs.com/compatible-mode/v1",
            "models":[{"id":"qwen3-32b"},{"id":"qwen3.8-max"}],
        }, resolve_credentials=False)
        self.assertEqual(official.type, "qwen-chat-v1")
        self.assertEqual(official.models[0].reasoning_mode, "toggle")
        self.assertEqual(official.models[1].transport, "qwen-responses")
        relay = _load_provider({
            "id":"qwen-relay", "name":"Relay", "preset_id":"qwen",
            "type":"openai-compatible", "base_url":"https://relay.example/v1",
            "models":[{"id":"qwen3-32b"}],
        }, resolve_credentials=False)
        self.assertEqual(relay.type, "openai-compatible")
        self.assertEqual(relay.models[0].reasoning_mode, "unknown")
        stale_native_relay = _load_provider({
            "id":"qwen-relay-native", "name":"Relay", "preset_id":"qwen",
            "type":"qwen-chat-v1", "base_url":"https://relay.example/v1",
            "models":[{"id":"qwen3-32b"}],
        }, resolve_credentials=False)
        self.assertEqual(stale_native_relay.type, "openai-compatible")


    def test_zhipu_glm_native_profiles_and_body_preserve_agent_reasoning(self) -> None:
        g53 = official_model_profile("zhipu", "glm-5.3")
        self.assertEqual(g53["effort_supported_levels"], ["low", "high", "max"])
        self.assertFalse(g53["reasoning"]["can_disable"])
        self.assertEqual(g53["reasoning"]["effort"]["default"], "max")
        g52 = official_model_profile("zhipu", "glm-5.2")
        self.assertEqual(g52["context_window"], 1_000_000)
        self.assertEqual(g52["effort_supported_levels"], ["high", "max"])
        self.assertTrue(g52["reasoning"]["can_disable"])
        gt = official_model_profile("zhipu", "glm-5-turbo")
        self.assertTrue(gt["reasoning"]["can_disable"])
        gv = official_model_profile("zhipu", "glm-5v-turbo")
        self.assertIn("image", gv["input_modalities"])

        p = provider("zhipu-chat-v4", preset_id="zhipu", base="https://open.bigmodel.cn/api/paas/v4")
        m53 = ModelSpec(
            provider_id="p1", provider_name="Provider", model_id="glm-5.3", name="GLM 5.3",
            reasoning_mode="levels", reasoning_supported=True, reasoning_can_disable=False,
            reasoning_default_enabled=True, reasoning_enable_value="enabled",
            effort_supported_levels=("low", "high", "max"), effort_default_level="max",
        )
        body = zhipu_chat._body(p, m53.model_id, [{"role":"user","content":"hi"}], reasoning_effort="low", thinking_mode="on", model=m53, tools=[{"type":"function","function":{"name":"lookup","parameters":{"type":"object","properties":{}}}}])
        self.assertEqual(body["thinking"], {"type":"enabled", "clear_thinking":False})
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["tool_choice"], "auto")
        self.assertTrue(body["tool_stream"])

        mt = ModelSpec(
            provider_id="p1", provider_name="Provider", model_id="glm-5-turbo", name="GLM 5 Turbo",
            reasoning_mode="toggle", reasoning_supported=True, reasoning_can_disable=True,
            reasoning_default_enabled=True, reasoning_enable_value="enabled", reasoning_disable_value="disabled",
        )
        off = zhipu_chat._body(p, mt.model_id, [{"role":"user","content":"hi"}], reasoning_effort=None, thinking_mode="off", model=mt, tools=None)
        self.assertEqual(off["thinking"], {"type":"disabled"})

        native = {"role":"assistant", "content":None, "reasoning_content":"keep", "tool_calls":[{"id":"c1","type":"function","function":{"name":"lookup","arguments":"{}"}}]}
        rows = zhipu_chat._messages(p, [{"role":"assistant","content":"", "_kiro_provider_context":{"provider_id":"p1","transport":"zhipu-chat-v4","data":{"message":native}}}])
        self.assertEqual(rows[0]["reasoning_content"], "keep")
        self.assertEqual(rows[0]["tool_calls"][0]["id"], "c1")

    def test_official_zhipu_upgrades_native_but_relay_stays_compatible(self) -> None:
        official = _load_provider({
            "id":"glm", "name":"GLM", "preset_id":"zhipu",
            "type":"openai-compatible", "base_url":"https://open.bigmodel.cn/api/paas/v4",
            "models":[{"id":"glm-5.3"}],
        }, resolve_credentials=False)
        self.assertEqual(official.type, "zhipu-chat-v4")
        self.assertEqual(official.models[0].effort_supported_levels, ("low","high","max"))
        relay = _load_provider({
            "id":"glm-relay", "name":"Relay", "preset_id":"zhipu",
            "type":"openai-compatible", "base_url":"https://relay.example/v1",
            "models":[{"id":"glm-5.3"}],
        }, resolve_credentials=False)
        self.assertEqual(relay.type, "openai-compatible")
        self.assertEqual(relay.models[0].effort_supported_levels, ())

    def test_minimax_native_profiles_headers_thinking_and_cache(self) -> None:
        m31 = official_model_profile("minimax", "MiniMax-M3.1-Flash-Preview")
        self.assertEqual(m31["context_window"], 1_000_000)
        self.assertEqual(m31["effort_supported_levels"], ["low", "medium", "high", "xhigh", "max"])
        self.assertFalse(m31["reasoning"]["can_disable"])
        m3p = official_model_profile("minimax", "MiniMax-M3")
        self.assertTrue(m3p["reasoning"]["can_disable"])
        self.assertFalse(m3p["reasoning"]["default_enabled"])
        self.assertEqual(m3p["reasoning"]["enable_value"], "adaptive")
        self.assertEqual(m3p["reasoning"]["disable_value"], "__omit__")
        m2 = official_model_profile("minimax", "MiniMax-M2.7")
        self.assertEqual(m2["reasoning"]["mode"], "fixed")
        self.assertFalse(m2["reasoning"]["can_disable"])

        p = provider("minimax-messages", preset_id="minimax", base="https://api.minimax.io/anthropic/v1")
        headers = anthropic_messages._headers(p, None, "", False)
        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertNotIn("anthropic-version", headers)
        self.assertNotIn("x-api-key", headers)

        m3 = ModelSpec(
            provider_id="p1", provider_name="Provider", model_id="MiniMax-M3", name="M3",
            reasoning_mode="toggle", reasoning_supported=True, reasoning_can_disable=True,
            reasoning_default_enabled=False, reasoning_enable_value="adaptive", reasoning_disable_value="__omit__",
            max_output_tokens=524_288,
        )
        history = [
            {"role":"system","content":"stable agent prompt"},
            {"role":"user","content":"one"},
            {"role":"assistant","content":"two"},
            {"role":"user","content":"three"},
        ]
        on = anthropic_messages._body(p, m3.model_id, history, reasoning_effort=None, thinking_mode="on", model=m3, tools=None)
        self.assertEqual(on["thinking"], {"type":"adaptive"})
        self.assertIsInstance(on["system"], list)
        self.assertEqual(on["system"][0]["cache_control"]["type"], "ephemeral")
        self.assertEqual(on["messages"][-2]["content"][-1]["cache_control"]["type"], "ephemeral")
        self.assertNotIn("cache_control", on)
        off = anthropic_messages._body(p, m3.model_id, history, reasoning_effort=None, thinking_mode="off", model=m3, tools=None)
        self.assertNotIn("thinking", off)

        m31spec = ModelSpec(
            provider_id="p1", provider_name="Provider", model_id="MiniMax-M3.1-Flash-Preview", name="M3.1",
            reasoning_mode="levels", reasoning_supported=True, reasoning_can_disable=False,
            reasoning_default_enabled=True, reasoning_enable_value="adaptive",
            effort_supported_levels=("low","medium","high","xhigh","max"), effort_default_level="max",
            max_output_tokens=524_288,
        )
        body31 = anthropic_messages._body(p, m31spec.model_id, [{"role":"user","content":"hi"}], reasoning_effort="xhigh", thinking_mode="on", model=m31spec, tools=None)
        self.assertEqual(body31["thinking"], {"type":"adaptive"})
        self.assertEqual(body31["output_config"]["effort"], "xhigh")

    def test_minimax_legacy_endpoints_upgrade_without_crossing_regions(self) -> None:
        intl = _load_provider({
            "id":"mm", "name":"MiniMax", "preset_id":"minimax", "type":"openai-compatible",
            "base_url":"https://api.minimax.io/v1", "models":[{"id":"MiniMax-M3"}],
        }, resolve_credentials=False)
        self.assertEqual(intl.type, "minimax-messages")
        self.assertEqual(intl.base_url, "https://api.minimax.io/anthropic/v1")
        self.assertTrue(intl.models[0].reasoning_can_disable)
        china = _load_provider({
            "id":"mm-cn", "name":"MiniMax CN", "preset_id":"minimax", "type":"openai-compatible",
            "base_url":"https://api.minimaxi.com/v1", "models":[{"id":"MiniMax-M3"}],
        }, resolve_credentials=False)
        self.assertEqual(china.type, "minimax-messages")
        self.assertEqual(china.base_url, "https://api.minimax.cn/anthropic/v1")
        self.assertTrue(china.models[0].reasoning_can_disable)

    def test_qianfan_responses_uses_server_state_and_native_thinking_switch(self) -> None:
        p = provider("qianfan-responses", preset_id="qianfan", base="https://qianfan.baidubce.com/v2")
        m = ModelSpec(
            provider_id="p1", provider_name="Provider", model_id="ernie-x", name="ERNIE",
            reasoning_mode="toggle", reasoning_supported=True, reasoning_can_disable=True,
            reasoning_default_enabled=True, reasoning_enable_value="enabled", reasoning_disable_value="disabled",
        )
        messages = [
            {"role":"assistant","content":"a","_kiro_provider_context":{"provider_id":"p1","transport":"qianfan-responses","data":{"response_id":"resp_qf","output":[]}}},
            {"role":"user","content":"b"},
        ]
        on = responses_api._body(p, m.model_id, messages, reasoning_effort=None, thinking_mode="on", model=m, cache_key="", tools=None)
        self.assertEqual(on["previous_response_id"], "resp_qf")
        self.assertTrue(on["store"])
        self.assertEqual(on["thinking"], {"type":"enabled"})
        self.assertNotIn("reasoning", on)
        off = responses_api._body(p, m.model_id, messages, reasoning_effort=None, thinking_mode="off", model=m, cache_key="", tools=None)
        self.assertEqual(off["thinking"], {"type":"disabled"})
        unknown = responses_api._body(p, m.model_id, [{"role":"user","content":"x"}], reasoning_effort=None, thinking_mode="on", model=None, cache_key="", tools=None)
        self.assertNotIn("thinking", unknown)

    def test_mistral_native_profile_and_replay_preserve_thinking_chunks(self) -> None:
        profile = official_model_profile("mistral", "mistral-large-4-0")
        self.assertTrue(profile["reasoning"]["can_disable"])
        self.assertEqual(profile["reasoning"]["disable_value"], "none")
        self.assertNotIn("none", profile["effort_supported_levels"])
        self.assertEqual(profile["context_window"], 1_000_000)

        p = provider("mistral-chat-v1", preset_id="mistral", base="https://api.mistral.ai/v1")
        raw_message = {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": [{"type": "text", "text": "private-thought"}]},
                {"type": "text", "text": "visible"},
            ],
        }
        messages = [{
            "role": "assistant", "content": "visible",
            "_kiro_provider_context": {"provider_id": "p1", "transport": "mistral-chat-v1", "data": {"message": raw_message}},
        }]
        body = mistral_chat._body(p, "mistral-large-4-0", messages, reasoning_effort="high", thinking_mode="on", model=model(levels=("minimal","low","medium","high","xhigh"), disable="none", can_disable=True), cache_key="sess", tools=None)
        self.assertEqual(body["messages"][0]["content"][0]["type"], "thinking")
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(body["prompt_cache_key"], "sess")
        off = mistral_chat._body(p, "mistral-large-4-0", [{"role":"user","content":"hi"}], reasoning_effort="xhigh", thinking_mode="off", model=model(levels=("minimal","low","medium","high","xhigh"), disable="none", can_disable=True), cache_key="sess", tools=None)
        self.assertEqual(off["reasoning_effort"], "none")

    def test_official_mistral_upgrades_native_but_relay_stays_compatible(self) -> None:
        official = _load_provider({
            "id":"mistral", "name":"Mistral", "preset_id":"mistral",
            "type":"openai-compatible", "base_url":"https://api.mistral.ai/v1",
            "models":[{"id":"mistral-large-4-0"}],
        }, resolve_credentials=False)
        self.assertEqual(official.type, "mistral-chat-v1")
        relay = _load_provider({
            "id":"mistral-relay", "name":"Relay", "preset_id":"mistral",
            "type":"openai-compatible", "base_url":"https://relay.example/v1",
            "models":[{"id":"mistral-large-4-0"}],
        }, resolve_credentials=False)
        self.assertEqual(relay.type, "openai-compatible")
        self.assertEqual(relay.models[0].effort_supported_levels, ())

    def test_deepseek_thinking_toggle_is_not_effort_level(self) -> None:
        p = official_model_profile("deepseek", "deepseek-flash")
        self.assertEqual(p["effort_supported_levels"], ["low", "high", "max"])
        self.assertTrue(p["reasoning"]["can_disable"])
        self.assertEqual(p["reasoning"]["disable_value"], "none")
        self.assertNotIn("none", p["effort_supported_levels"])

    def test_qwen38_uses_responses_and_separates_off_from_effort(self) -> None:
        p = official_model_profile("qwen", "qwen3.8-max")
        self.assertEqual(p["transport"], "qwen-responses")
        self.assertEqual(p["effort_supported_levels"], ["low", "medium", "xhigh"])
        self.assertEqual(p["reasoning"]["disable_value"], "none")
        self.assertNotIn("none", p["effort_supported_levels"])
        self.assertEqual(official_model_profile("qwen", "qwen2.5-72b"), {})

    def test_hunyuan_profiles_are_model_specific(self) -> None:
        hy4 = official_model_profile("hunyuan", "hy4-preview")
        self.assertEqual(hy4["transport"], "hunyuan-responses")
        self.assertEqual(hy4["context_window"], 1_024_000)
        self.assertEqual(hy4["max_output_tokens"], 64_000)
        self.assertEqual(hy4["effort_supported_levels"], ["high"])
        self.assertTrue(hy4["reasoning"]["can_disable"])
        hy3 = official_model_profile("hunyuan", "hy3")
        self.assertEqual(hy3["effort_supported_levels"], ["low", "high"])
        self.assertEqual(hy3["max_output_tokens"], 128_000)
        self.assertEqual(official_model_profile("hunyuan", "hy-mt2-pro"), {})

    def test_gemini_minimal_is_not_thinking_off(self) -> None:
        p = official_model_profile("gemini", "gemini-3.6-flash")
        self.assertIn("minimal", p["effort_supported_levels"])
        self.assertFalse(p["reasoning"]["can_disable"])
        self.assertEqual(p["reasoning"].get("disable_value", ""), "")

    def test_model_transport_override_changes_adapter_without_identity(self) -> None:
        base = provider("openai-compatible", preset_id="qwen", base="https://dashscope.aliyuncs.com/compatible-mode/v1")
        m = model(transport="qwen-responses")
        effective = _effective_provider(base, m)
        self.assertEqual(effective.type, "qwen-responses")
        self.assertEqual(effective.id, base.id)
        self.assertEqual(effective.base_url, base.base_url)

    def test_custom_relay_does_not_inherit_official_qwen_profile(self) -> None:
        raw = {
            "id": "qwen-relay",
            "name": "Relay",
            "preset_id": "qwen",
            "type": "openai-compatible",
            "base_url": "https://relay.example/v1",
            "models": [{"id": "qwen3.8-max", "name": "qwen3.8-max"}],
        }
        p = _load_provider(raw, resolve_credentials=False)
        self.assertEqual(p.type, "openai-compatible")
        self.assertEqual(p.models[0].transport, "")
        self.assertEqual(p.models[0].effort_supported_levels, ())

    def test_model_transport_validation_rejects_unknown_adapter(self) -> None:
        with self.assertRaises(ConfigError):
            _load_model("p", "P", {"id": "m", "transport": "made-up-native"})

    def test_openai_compatible_strips_private_continuation_metadata(self) -> None:
        p = provider("openai-compatible")
        body = openai_compatible._body(
            p,
            "m",
            [{"role": "assistant", "content": "ok", "_kiro_provider_context": {"secret": True}}],
            reasoning_effort=None,
            cache_key="",
            tools=None,
        )
        self.assertNotIn("_kiro_provider_context", body["messages"][0])


    def test_generic_openai_compatible_enforces_safe_thinking_off_or_fails_closed(self) -> None:
        p = provider("openai-compatible")
        safe = model(levels=("low", "high"), disable="none", can_disable=True, mode="levels")
        body = openai_compatible._body(
            p, "m1", [{"role":"user","content":"hi"}],
            thinking_mode="off", model=safe, reasoning_effort="high",
        )
        self.assertEqual(body["reasoning_effort"], "none")

        unknown_toggle = model(levels=(), disable="disabled", can_disable=True, mode="toggle")
        with self.assertRaisesRegex(openai_compatible.OpenAICompatibleError, "no verified wire mapping"):
            openai_compatible._body(
                p, "m1", [{"role":"user","content":"hi"}],
                thinking_mode="off", model=unknown_toggle,
            )

    def test_mistral_uses_stable_prompt_cache_key(self) -> None:
        p = provider("openai-compatible", preset_id="mistral", base="https://api.mistral.ai/v1")
        body = openai_compatible._body(p, "mistral-large-latest", [{"role": "user", "content": "hi"}], cache_key="session-42")
        self.assertEqual(body["prompt_cache_key"], "session-42")

    def test_responses_vendor_specific_bodies(self) -> None:
        m = model(levels=("low", "high", "max"), disable="none", can_disable=True, max_output=4096)
        messages = [{"role": "user", "content": "hi"}]
        openai_body = responses_api._body(provider("openai-responses"), "m", messages, reasoning_effort="high", thinking_mode="on", model=m, cache_key="sess", tools=None)
        self.assertFalse(openai_body["store"])
        self.assertEqual(openai_body["prompt_cache_key"], "sess")
        self.assertEqual(openai_body["reasoning"]["effort"], "high")
        openai_with_sampling = ProviderSpec(
            id="p1", name="Provider", type="openai-responses", base_url="https://example.test/v1",
            api_key="secret", models=(), preset_id="openai", temperature=0.3,
            extra_body={"top_p": 0.8, "top_logprobs": 2},
        )
        cleaned = responses_api._body(openai_with_sampling, "m", messages, reasoning_effort="high", thinking_mode="on", model=m, cache_key="sess", tools=None)
        self.assertNotIn("temperature", cleaned)
        self.assertNotIn("top_p", cleaned)
        self.assertNotIn("top_logprobs", cleaned)
        xai = responses_api._body(provider("xai-responses"), "m", messages, reasoning_effort="high", thinking_mode="on", model=m, cache_key="sess", tools=None)
        self.assertIn("reasoning.encrypted_content", xai["include"])
        self.assertEqual(xai["prompt_cache_key"], "sess")
        deepseek = responses_api._body(provider("deepseek-responses"), "m", messages, reasoning_effort=None, thinking_mode="off", model=m, cache_key="sess", tools=None)
        self.assertEqual(deepseek["reasoning"]["effort"], "none")
        self.assertNotIn("prompt_cache_key", deepseek)
        groq = responses_api._body(provider("groq-responses"), "m", messages, reasoning_effort="high", thinking_mode="on", model=m, cache_key="sess", tools=None)
        self.assertNotIn("prompt_cache_key", groq)
        self.assertNotIn("previous_response_id", groq)


    def test_native_thinking_switch_uses_model_declared_wire_literals(self) -> None:
        m = ModelSpec(
            provider_id="p1", provider_name="Provider", model_id="future", name="Future",
            reasoning_mode="dynamic-v2", reasoning_supported=True, reasoning_can_disable=True,
            reasoning_enable_value="deep", reasoning_disable_value="shallow",
        )
        messages = [{"role": "user", "content": "hi"}]

        cohere_on = cohere_v2._body(
            provider("cohere-v2"), "future", messages, thinking_mode="on", model=m, tools=None
        )
        cohere_off = cohere_v2._body(
            provider("cohere-v2"), "future", messages, thinking_mode="off", model=m, tools=None
        )
        self.assertEqual(cohere_on["thinking"], {"type": "deep"})
        self.assertEqual(cohere_off["thinking"], {"type": "shallow"})

        qf_on = responses_api._body(
            provider("qianfan-responses"), "future", messages, reasoning_effort=None,
            thinking_mode="on", model=m, cache_key="", tools=None
        )
        qf_off = responses_api._body(
            provider("qianfan-responses"), "future", messages, reasoning_effort=None,
            thinking_mode="off", model=m, cache_key="", tools=None
        )
        self.assertEqual(qf_on["thinking"], {"type": "deep"})
        self.assertEqual(qf_off["thinking"], {"type": "shallow"})

    def test_qwen_session_cache_and_stateful_tail(self) -> None:
        p = provider("qwen-responses", preset_id="qwen", base="https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.assertEqual(responses_api._headers(p)["x-dashscope-session-cache"], "enable")
        messages = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two", "_kiro_provider_context": {"provider_id": "p1", "transport": "qwen-responses", "data": {"response_id": "resp_123", "output": []}}},
            {"role": "user", "content": "three"},
        ]
        body = responses_api._body(p, "qwen3.8-max", messages, reasoning_effort=None, thinking_mode="", model=None, cache_key="", tools=None)
        self.assertEqual(body["previous_response_id"], "resp_123")
        self.assertEqual(len(body["input"]), 1)
        self.assertEqual(body["input"][0]["role"], "user")

    def test_hunyuan_stateful_tail(self) -> None:
        p = provider("hunyuan-responses", preset_id="hunyuan", base="https://tokenhub.tencentmaas.com/v1")
        messages = [
            {"role": "assistant", "content": "a", "_kiro_provider_context": {"provider_id": "p1", "transport": "hunyuan-responses", "data": {"response_id": "r1", "output": []}}},
            {"role": "user", "content": "b"},
        ]
        body = responses_api._body(p, "hy4-preview", messages, reasoning_effort="high", thinking_mode="on", model=model(levels=("high",), disable="none", can_disable=True), cache_key="", tools=None)
        self.assertEqual(body["previous_response_id"], "r1")
        self.assertEqual(body["input"][0]["role"], "user")

    def test_anthropic_native_body_preserves_thinking_controls_and_cache(self) -> None:
        p = provider("anthropic-messages", preset_id="anthropic", base="https://api.anthropic.com/v1")
        m = ModelSpec(
            provider_id="p1", provider_name="Provider", model_id="claude-sonnet-5", name="Claude",
            reasoning_mode="adaptive", reasoning_supported=True, reasoning_can_disable=True,
            reasoning_enable_value="adaptive", reasoning_disable_value="disabled",
            effort_supported_levels=("low", "medium", "high", "xhigh", "max"), max_output_tokens=4096,
        )
        body = anthropic_messages._body(p, m.model_id, [{"role": "user", "content": "hi"}], reasoning_effort="medium", thinking_mode="on", model=m, tools=None)
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        self.assertEqual(body["output_config"]["effort"], "medium")
        self.assertEqual(body["cache_control"]["type"], "ephemeral")
        p_with_temp = ProviderSpec(
            id="p1", name="Provider", type="anthropic-messages", base_url="https://api.anthropic.com/v1",
            api_key="secret", models=(), preset_id="anthropic", temperature=0.2,
        )
        modern = anthropic_messages._body(p_with_temp, "claude-opus-5-5", [{"role":"user","content":"hi"}], reasoning_effort="medium", thinking_mode="on", model=m, tools=None)
        self.assertNotIn("temperature", modern)
        off = anthropic_messages._body(p, m.model_id, [{"role": "user", "content": "hi"}], reasoning_effort="xhigh", thinking_mode="off", model=m, tools=None)
        self.assertEqual(off["thinking"], {"type": "disabled"})
        self.assertNotIn("output_config", off)

    def test_responses_open_failure_is_transport_error(self) -> None:
        p = provider("deepseek-responses", preset_id="deepseek", base="https://api.deepseek.com/v1")
        with patch.object(responses_api.urllib.request, "urlopen", side_effect=urllib.error.URLError("reset")):
            with self.assertRaises(ProviderTransportError) as raised:
                responses_api.chat_completion_turn(p, "deepseek-v4.1", [{"role": "user", "content": "hi"}], lambda _x: None)
        self.assertEqual(raised.exception.category, "transport")
        self.assertTrue(raised.exception.retryable)

    def test_gemini_open_failure_is_transport_error(self) -> None:
        p = provider("gemini-interactions", preset_id="gemini", base="https://generativelanguage.googleapis.com/v1beta")
        with patch.object(gemini_interactions.urllib.request, "urlopen", side_effect=urllib.error.URLError("reset")):
            with self.assertRaises(ProviderTransportError) as raised:
                gemini_interactions.chat_completion_turn(p, "gemini-3.8-flash", [{"role": "user", "content": "hi"}], lambda _x: None)
        self.assertEqual(raised.exception.category, "transport")
        self.assertTrue(raised.exception.retryable)

    def test_gemini_interactions_uses_server_state_and_tail(self) -> None:
        p = provider("gemini-interactions", preset_id="gemini", base="https://generativelanguage.googleapis.com/v1beta")
        m = model(levels=("low", "medium", "high"), can_disable=False, max_output=4096)
        messages = [
            {"role": "assistant", "content": "a", "_kiro_provider_context": {"provider_id": "p1", "transport": "gemini-interactions", "data": {"interaction_id": "int_1", "steps": []}}},
            {"role": "user", "content": "b"},
        ]
        body = gemini_interactions._body(p, "gemini-3.8-flash", messages, reasoning_effort="medium", thinking_mode="on", model=m, tools=None)
        self.assertTrue(body["store"])
        self.assertEqual(body["previous_interaction_id"], "int_1")
        self.assertEqual(body["generation_config"]["thinking_level"], "medium")
        self.assertEqual(len(body["input"]), 1)


if __name__ == "__main__":
    unittest.main()
