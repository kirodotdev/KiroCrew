from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_byo.acp_server import AcpServer
from kiro_byo.config import ModelSpec
from kiro_byo.credential_store import MemoryCredentialStore
from kiro_byo.discovery import DiscoveredModel, DiscoveryError
from kiro_byo.provider_admin import configure_provider, list_provider_presets
from kiro_byo.provider_catalog import normalize_reasoning_capability, official_model_profile, provider_preset


class ModelCapabilityTests(unittest.TestCase):
    def test_single_advertised_effort_becomes_fixed_not_dropdown(self) -> None:
        cap = normalize_reasoning_capability(effort_levels=("high",), effort_default="high")
        self.assertEqual(cap["mode"], "fixed")
        self.assertEqual(cap["fixed_level"], "high")
        self.assertEqual(cap["selectable_levels"], [])
        model = ModelSpec(
            "demo", "Demo", "fixed", "Fixed",
            effort_supported_levels=("high",), effort_default_level="high",
            reasoning_mode="fixed", reasoning_fixed_level="high",
        )
        self.assertIsNone(AcpServer._effort_config(model, "high"))
        self.assertEqual(AcpServer._effort_default(model), "high")

    def test_multiple_efforts_are_selectable(self) -> None:
        cap = normalize_reasoning_capability(effort_levels=("low", "high", "max"), effort_default="high")
        self.assertEqual(cap["mode"], "levels")
        self.assertEqual(cap["selectable_levels"], ["low", "high", "max"])
        model = ModelSpec(
            "demo", "Demo", "levels", "Levels",
            effort_supported_levels=("low", "high", "max"), effort_default_level="high",
            reasoning_mode="levels",
        )
        cfg = AcpServer._effort_config(model, "high")
        self.assertIsNotNone(cfg)
        self.assertEqual([o["value"] for o in cfg["options"]], ["low", "high", "max"])

    def test_deep_thinking_toggle_is_separate_from_effort_levels(self) -> None:
        cap = normalize_reasoning_capability(
            effort_levels=("none", "low", "high", "max"),
            effort_default="high",
        )
        self.assertEqual(cap["mode"], "levels")
        self.assertTrue(cap["thinking"]["supported"])
        self.assertTrue(cap["thinking"]["can_disable"])
        self.assertEqual(cap["thinking"]["disable_value"], "none")
        self.assertEqual(cap["effort"]["levels"], ["low", "high", "max"])
        self.assertEqual(cap["effort"]["level_count"], 3)
        self.assertEqual(cap["selectable_levels"], ["low", "high", "max"])
        model = ModelSpec(
            "demo", "Demo", "disableable", "Disableable",
            effort_supported_levels=("low", "high", "max"),
            effort_default_level="high",
            reasoning_mode="levels",
            reasoning_supported=True,
            reasoning_can_disable=True,
            reasoning_disable_value="none",
        )
        cfg = AcpServer._effort_config(model, "high")
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["currentValue"], "high")
        self.assertEqual([o["value"] for o in cfg["options"]], ["low", "high", "max"])
        self.assertNotIn("none", [o["value"] for o in cfg["options"]])


    def test_thinking_switch_and_effort_are_independent_session_controls(self) -> None:
        model = ModelSpec(
            "demo", "Demo", "hybrid", "Hybrid",
            effort_supported_levels=("low", "medium", "high", "max"),
            effort_default_level="medium",
            reasoning_mode="levels",
            reasoning_supported=True,
            reasoning_can_disable=True,
            reasoning_default_enabled=True,
            reasoning_disable_value="none",
        )
        thinking = AcpServer._thinking_config(model, "on")
        effort = AcpServer._effort_config(model, "medium")
        self.assertIsNotNone(thinking)
        self.assertEqual([x["value"] for x in thinking["options"]], ["on", "off"])
        self.assertIsNotNone(effort)
        self.assertEqual([x["value"] for x in effort["options"]], ["low", "medium", "high", "max"])
        self.assertEqual(AcpServer._reasoning_wire_value(model, "off", "high"), "none")
        self.assertEqual(AcpServer._reasoning_wire_value(model, "on", "high"), "high")

    def test_non_disableable_reasoning_has_effort_only(self) -> None:
        model = ModelSpec(
            "demo", "Demo", "fixed-on", "Fixed On",
            effort_supported_levels=("low", "medium", "high"),
            effort_default_level="medium",
            reasoning_mode="levels",
            reasoning_supported=True,
            reasoning_can_disable=False,
            reasoning_default_enabled=True,
        )
        self.assertIsNone(AcpServer._thinking_config(model, "on"))
        effort = AcpServer._effort_config(model, "medium")
        self.assertEqual([x["value"] for x in effort["options"]], ["low", "medium", "high"])

    def test_toggle_vocabulary_does_not_fake_an_effort_scale(self) -> None:
        cap = normalize_reasoning_capability(
            effort_levels=("none", "thinking"),
            effort_default="none",
        )
        self.assertTrue(cap["thinking"]["supported"])
        self.assertTrue(cap["thinking"]["can_disable"])
        self.assertEqual(cap["thinking"]["enable_value"], "thinking")
        self.assertEqual(cap["thinking"]["disable_value"], "none")
        self.assertEqual(cap["effort"]["levels"], [])
        self.assertEqual(cap["selectable_levels"], [])

    def test_effort_scale_keeps_model_specific_number_of_levels(self) -> None:
        three = normalize_reasoning_capability(
            effort_levels=("low", "medium", "high"), effort_default="medium",
            raw_reasoning={"supported": True, "can_disable": False},
        )
        four = normalize_reasoning_capability(
            effort_levels=("low", "medium", "high", "max"), effort_default="high",
            raw_reasoning={"supported": True, "can_disable": False},
        )
        self.assertEqual(three["effort"]["level_count"], 3)
        self.assertEqual(four["effort"]["level_count"], 4)
        self.assertFalse(three["thinking"]["can_disable"])
        self.assertFalse(four["thinking"]["can_disable"])

    def test_fixed_reasoning_cannot_be_advertised_as_disableable(self) -> None:
        cap = normalize_reasoning_capability(
            effort_levels=("high",),
            effort_default="high",
            raw_reasoning={"mode": "fixed", "can_disable": True},
        )
        self.assertEqual(cap["mode"], "fixed")
        self.assertFalse(cap["can_disable"])
        self.assertEqual(cap["selectable_levels"], [])


    def test_future_thinking_mode_is_preserved_but_control_stays_fact_driven(self) -> None:
        explicit = normalize_reasoning_capability(
            raw_reasoning={
                "mode": "dynamic-v2",
                "supported": True,
                "can_disable": True,
                "enable_value": "deep",
                "disable_value": "shallow",
            }
        )
        self.assertEqual(explicit["mode"], "dynamic-v2")
        self.assertTrue(explicit["thinking"]["supported"])
        self.assertTrue(explicit["thinking"]["can_disable"])
        self.assertEqual(explicit["thinking"]["enable_value"], "deep")
        self.assertEqual(explicit["thinking"]["disable_value"], "shallow")

        unknown = normalize_reasoning_capability(raw_reasoning={"mode": "dynamic-v3"})
        self.assertEqual(unknown["mode"], "dynamic-v3")
        self.assertIsNone(unknown["thinking"]["supported"])
        self.assertIsNone(unknown["thinking"]["can_disable"])

    def test_toggle_mode_itself_proves_a_switch_without_inventing_effort(self) -> None:
        cap = normalize_reasoning_capability(raw_reasoning={"mode": "toggle"})
        self.assertEqual(cap["mode"], "toggle")
        self.assertTrue(cap["thinking"]["supported"])
        self.assertTrue(cap["thinking"]["can_disable"])
        self.assertEqual(cap["effort"]["levels"], [])

    def test_provider_presets_are_connection_templates(self) -> None:
        presets = {p["id"]: p for p in list_provider_presets()}
        for provider_id in (
            "deepseek", "moonshot", "xiaomi", "qwen", "zhipu", "volcengine", "hunyuan",
            "qianfan", "minimax", "stepfun", "openai", "anthropic", "gemini",
            "grok", "mistral", "cohere", "groq", "custom",
        ):
            self.assertIn(provider_id, presets)
        self.assertTrue(presets["deepseek"]["available"])
        self.assertTrue(presets["anthropic"]["available"])
        self.assertEqual(presets["hunyuan"]["default_base_url"], "https://tokenhub.tencentmaas.com/v1")
        self.assertEqual(presets["custom"]["category"], "custom")
        self.assertIn("中转站", presets["custom"]["display_name"])
        self.assertNotIn("reasoning", presets["deepseek"])
        self.assertNotIn("xiaomi-tokenplan", presets)
        self.assertIsNotNone(provider_preset("xiaomi-tokenplan"))
        self.assertIn("https://token-plan-cn.xiaomimimo.com/v1", presets["xiaomi"]["official_base_urls"])

    def test_provider_display_names_are_clean_unicode(self) -> None:
        presets = {p["id"]: p for p in list_provider_presets()}
        expected = {
            "moonshot": "Moonshot / Kimi",
            "xiaomi": "小米 MiMo / Xiaomi",
            "qwen": "阿里云百炼 / Qwen",
            "zhipu": "智谱 AI / GLM",
            "volcengine": "火山引擎 / Ark / 豆包",
            "hunyuan": "腾讯混元",
            "qianfan": "百度千帆 / ERNIE",
            "stepfun": "阶跃星辰 / StepFun",
            "custom": "自定义 / 中转站（OpenAI Compatible）",
        }
        for provider_id, display_name in expected.items():
            self.assertEqual(presets[provider_id]["display_name"], display_name)
        for preset in presets.values():
            name = preset["display_name"]
            self.assertNotIn("�", name)
            self.assertFalse(any(0xE000 <= ord(ch) <= 0xF8FF for ch in name))

    def test_xiaomi_official_catalog_filters_non_agent_models_and_uses_native_transport(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config").mkdir()
            (home / "config" / "providers.json").write_text(
                json.dumps({"credential_schema_version": 1, "default_model": "", "providers": []}),
                encoding="utf-8",
            )
            old = os.environ.get("KIRO_BYO_HOME")
            os.environ["KIRO_BYO_HOME"] = str(home)
            try:
                store = MemoryCredentialStore()
                discovered = (
                    "xiaomi",
                    "小米 MiMo / Xiaomi",
                    [
                        DiscoveredModel("mimo-v2.6-pro", "mimo-v2.6-pro"),
                        DiscoveredModel("mimo-v2.6-flash", "mimo-v2.6-flash"),
                        DiscoveredModel("mimo-v2.6-pro-ultraspeed", "mimo-v2.6-pro-ultraspeed"),
                        DiscoveredModel("mimo-tts-voice-1", "mimo-tts-voice-1"),
                        DiscoveredModel("mimo-asr-1", "mimo-asr-1"),
                    ],
                )
                with patch("kiro_byo.provider_admin.discover_models", return_value=discovered) as discover:
                    result = configure_provider(
                        base_url="https://api.xiaomimimo.com/v1",
                        api_key="secret",
                        preset_id="xiaomi",
                        store=store,
                    )
                self.assertEqual(result["type"], "xiaomi-mimo-chat")
                self.assertEqual(
                    [row["id"] for row in result["models"]],
                    ["mimo-v2.6-pro", "mimo-v2.6-flash", "mimo-v2.6-pro-ultraspeed"],
                )
                pro = result["models"][0]
                self.assertEqual(pro["capabilities"]["context_window"], 1_000_000)
                self.assertEqual(pro["capabilities"]["max_output_tokens"], 131_072)
                self.assertTrue(pro["capabilities"]["tool_use"])
                self.assertEqual(pro["capabilities"]["reasoning"]["mode"], "toggle")
                self.assertTrue(pro["capabilities"]["reasoning"]["thinking"]["default_enabled"])
                kwargs = discover.call_args.kwargs
                self.assertEqual(kwargs["endpoint"], "https://api.xiaomimimo.com/v1/models")
                self.assertEqual(kwargs["auth_style"], "bearer")
            finally:
                if old is None:
                    os.environ.pop("KIRO_BYO_HOME", None)
                else:
                    os.environ["KIRO_BYO_HOME"] = old


    def test_xiaomi_unified_preset_auto_routes_token_plan_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config").mkdir()
            (home / "config" / "providers.json").write_text(
                json.dumps({"credential_schema_version": 1, "default_model": "", "providers": []}),
                encoding="utf-8",
            )
            old = os.environ.get("KIRO_BYO_HOME")
            os.environ["KIRO_BYO_HOME"] = str(home)
            try:
                store = MemoryCredentialStore()
                with patch("kiro_byo.provider_admin.discover_models", side_effect=DiscoveryError("catalogue unavailable")) as discover:
                    result = configure_provider(
                        base_url="https://api.xiaomimimo.com/v1",
                        api_key="tp-test-key",
                        preset_id="xiaomi",
                        store=store,
                    )
                discover.assert_called_once()
                self.assertEqual(result["preset_id"], "xiaomi")
                self.assertEqual(result["base_url"], "https://token-plan-cn.xiaomimimo.com/v1")
                self.assertEqual(result["type"], "xiaomi-mimo-chat")
                self.assertEqual(
                    [row["id"] for row in result["models"]],
                    ["mimo-v2.6-pro", "mimo-v2.6-flash", "mimo-v2.5-pro", "mimo-v2.5"],
                )
            finally:
                if old is None:
                    os.environ.pop("KIRO_BYO_HOME", None)
                else:
                    os.environ["KIRO_BYO_HOME"] = old


    def test_xiaomi_token_plan_uses_native_transport_and_static_agent_catalogue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config").mkdir()
            (home / "config" / "providers.json").write_text(
                json.dumps({"credential_schema_version": 1, "default_model": "", "providers": []}),
                encoding="utf-8",
            )
            old = os.environ.get("KIRO_BYO_HOME")
            os.environ["KIRO_BYO_HOME"] = str(home)
            try:
                store = MemoryCredentialStore()
                with patch("kiro_byo.provider_admin.discover_models", side_effect=DiscoveryError("catalogue unavailable")) as discover:
                    result = configure_provider(
                        base_url="https://token-plan-cn.xiaomimimo.com/v1",
                        api_key="tp-test-key",
                        preset_id="xiaomi-tokenplan",
                        store=store,
                    )
                discover.assert_called_once()
                self.assertEqual(result["type"], "xiaomi-mimo-chat")
                self.assertTrue(result["official_endpoint"])
                self.assertEqual(
                    [row["id"] for row in result["models"]],
                    ["mimo-v2.6-pro", "mimo-v2.6-flash", "mimo-v2.5-pro", "mimo-v2.5"],
                )
                self.assertEqual(result["models"][0]["capabilities"]["context_window"], 1_000_000)
                self.assertEqual(result["models"][2]["lifecycle"], "retiring")
                self.assertTrue(result["models"][0]["capabilities"]["tool_use"])
            finally:
                if old is None:
                    os.environ.pop("KIRO_BYO_HOME", None)
                else:
                    os.environ["KIRO_BYO_HOME"] = old

    def test_xiaomi_token_plan_live_catalog_accepts_future_chat_model(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config").mkdir()
            (home / "config" / "providers.json").write_text(
                json.dumps({"credential_schema_version": 1, "default_model": "", "providers": []}),
                encoding="utf-8",
            )
            old = os.environ.get("KIRO_BYO_HOME")
            os.environ["KIRO_BYO_HOME"] = str(home)
            try:
                store = MemoryCredentialStore()
                discovered = (
                    "xiaomi",
                    "小米 MiMo Token Plan",
                    [
                        DiscoveredModel("mimo-v2.6-pro", "mimo-v2.6-pro"),
                        DiscoveredModel("mimo-v2.7-pro", "mimo-v2.7-pro"),
                        DiscoveredModel("mimo-tts-next", "mimo-tts-next"),
                    ],
                )
                with patch("kiro_byo.provider_admin.discover_models", return_value=discovered):
                    result = configure_provider(
                        base_url="https://token-plan-cn.xiaomimimo.com/v1",
                        api_key="tp-test-key",
                        preset_id="xiaomi-tokenplan",
                        store=store,
                    )
                self.assertEqual(
                    [row["id"] for row in result["models"]],
                    ["mimo-v2.6-pro", "mimo-v2.7-pro"],
                )
                future = result["models"][1]
                self.assertIsNone(future["capabilities"]["tool_use"])
                self.assertEqual(future["capabilities"]["reasoning"]["mode"], "unknown")
            finally:
                if old is None:
                    os.environ.pop("KIRO_BYO_HOME", None)
                else:
                    os.environ["KIRO_BYO_HOME"] = old

    def test_xiaomi_token_plan_region_endpoints_remain_native(self) -> None:
        preset = provider_preset("xiaomi-tokenplan")
        self.assertEqual(preset.transport, "xiaomi-mimo-chat")
        self.assertIn("https://token-plan-cn.xiaomimimo.com/v1", preset.official_base_urls)
        self.assertIn("https://token-plan-sgp.xiaomimimo.com/v1", preset.official_base_urls)
        self.assertIn("https://token-plan-ams.xiaomimimo.com/v1", preset.official_base_urls)
        for model_id in ("mimo-v2.6-pro", "mimo-v2.6-flash"):
            profile = official_model_profile("xiaomi-tokenplan", model_id)
            self.assertTrue(profile["tool_use"])
            self.assertEqual(profile["reasoning"]["mode"], "toggle")

    def test_manual_models_fall_back_to_unknown_capability(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config").mkdir()
            (home / "config" / "providers.json").write_text(
                json.dumps({"credential_schema_version": 1, "default_model": "", "providers": []}),
                encoding="utf-8",
            )
            old = os.environ.get("KIRO_BYO_HOME")
            os.environ["KIRO_BYO_HOME"] = str(home)
            try:
                store = MemoryCredentialStore()
                with patch("kiro_byo.provider_admin.discover_models", side_effect=DiscoveryError("no /models")):
                    result = configure_provider(
                        base_url="https://relay.example/v1",
                        api_key="secret",
                        preset_id="custom",
                        manual_models=["relay-model"],
                        store=store,
                    )
                model = result["models"][0]
                self.assertEqual(model["capabilities"]["reasoning"]["mode"], "unknown")
                self.assertEqual(model["capabilities"]["reasoning"]["selectable_levels"], [])
                raw = json.loads((home / "config" / "providers.json").read_text(encoding="utf-8"))
                self.assertNotIn("secret", json.dumps(raw))
            finally:
                if old is None:
                    os.environ.pop("KIRO_BYO_HOME", None)
                else:
                    os.environ["KIRO_BYO_HOME"] = old


if __name__ == "__main__":
    unittest.main()
