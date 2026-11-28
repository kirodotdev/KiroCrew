from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec, load_config
from kiro_byo.credential_store import MemoryCredentialStore
from kiro_byo.provider_admin import list_providers, set_model_thinking_preference
from kiro_byo.reasoning_control import resolve_thinking_mode


class ThinkingPreferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "config").mkdir(parents=True)
        self.old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = str(self.home)
        self.store = MemoryCredentialStore()

    def tearDown(self) -> None:
        if self.old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = self.old_home
        self.tmp.cleanup()

    def _write(self, model: dict) -> None:
        (self.home / "config" / "providers.json").write_text(json.dumps({
            "mode": {"id": "tiga", "type": "multi"},
            "providers": [{
                "id": "demo", "name": "Demo", "type": "openai-compatible",
                "preset_id": "custom", "base_url": "https://example.test/v1",
                "models": [model],
            }],
        }), encoding="utf-8")

    def test_switchable_model_defaults_to_byo_auto(self) -> None:
        self._write({
            "id": "m", "name": "M",
            "reasoning": {"mode": "toggle", "supported": True, "can_disable": True,
                          "default_enabled": True, "enable_value": "enabled", "disable_value": "disabled"},
        })
        row = list_providers(store=self.store)[0]["models"][0]
        self.assertEqual(row["thinking_preference"], "auto")
        model = load_config(credential_store=self.store).providers[0].models[0]
        self.assertEqual(model.thinking_preference, "auto")

    def test_preference_persists_auto_on_off(self) -> None:
        self._write({
            "id": "m", "name": "M",
            "reasoning": {"mode": "toggle", "supported": True, "can_disable": True,
                          "default_enabled": True, "enable_value": "enabled", "disable_value": "disabled"},
        })
        for value in ("on", "off", "auto"):
            provider = set_model_thinking_preference("demo", "m", value, store=self.store)
            self.assertEqual(provider["models"][0]["thinking_preference"], value)
            self.assertEqual(load_config(credential_store=self.store).providers[0].models[0].thinking_preference, value)

    def test_fixed_model_rejects_three_state_control(self) -> None:
        self._write({
            "id": "fixed", "name": "Fixed",
            "reasoning": {"mode": "fixed", "supported": True, "can_disable": False,
                          "default_enabled": True},
        })
        with self.assertRaisesRegex(Exception, "does not support"):
            set_model_thinking_preference("demo", "fixed", "off", store=self.store)

    def test_user_hard_lock_beats_type_suggestion(self) -> None:
        base = dict(
            provider_id="p", provider_name="P", model_id="m", name="M",
            reasoning_mode="toggle", reasoning_supported=True, reasoning_can_disable=True,
            reasoning_default_enabled=True, reasoning_enable_value="enabled", reasoning_disable_value="disabled",
        )
        self.assertEqual(resolve_thinking_mode(ModelSpec(**base, thinking_preference="off"), "off", "on"), "off")
        self.assertEqual(resolve_thinking_mode(ModelSpec(**base, thinking_preference="on"), "on", "off"), "on")
        self.assertEqual(resolve_thinking_mode(ModelSpec(**base, thinking_preference="auto"), "auto", "off"), "off")
        self.assertEqual(resolve_thinking_mode(ModelSpec(**base, thinking_preference="auto"), "auto", "on"), "on")

    def test_tiga_sky_power_only_control_auto_models(self) -> None:
        server = AcpServer()
        auto = ModelSpec(
            "p", "P", "m", "M", reasoning_mode="toggle", reasoning_supported=True,
            reasoning_can_disable=True, reasoning_default_enabled=True,
            reasoning_enable_value="enabled", reasoning_disable_value="disabled",
            thinking_preference="auto",
        )
        off = ModelSpec(**{**auto.__dict__, "thinking_preference": "off"})
        on = ModelSpec(**{**auto.__dict__, "thinking_preference": "on"})
        session = Session("s", "", "M")
        self.assertEqual(server._policy_thinking_mode(auto, session, "economy"), "off")
        self.assertEqual(server._policy_thinking_mode(auto, session, "high"), "on")
        self.assertEqual(server._policy_thinking_mode(off, session, "high"), "off")
        self.assertEqual(server._policy_thinking_mode(on, session, "economy"), "on")
        server._executor.shutdown(wait=True)


    def test_concrete_model_selection_uses_persistent_preference(self) -> None:
        server = AcpServer()
        off = ModelSpec(
            "p", "P", "m", "M", reasoning_mode="toggle", reasoning_supported=True,
            reasoning_can_disable=True, reasoning_default_enabled=True,
            reasoning_enable_value="enabled", reasoning_disable_value="disabled",
            thinking_preference="off",
        )
        self.assertEqual(resolve_thinking_mode(off, off.thinking_preference, ""), "off")
        server._executor.shutdown(wait=True)

    def test_crew_config_only_exposes_effort_not_thinking_preference(self) -> None:
        model = ModelSpec(
            "p", "P", "m", "M", effort_supported_levels=("low", "high"),
            effort_default_level="low", reasoning_mode="levels", reasoning_supported=True,
            reasoning_can_disable=True, reasoning_default_enabled=True,
            reasoning_disable_value="none", thinking_preference="auto",
        )
        cfg = KiroByoConfig((ProviderSpec("p", "P", "openai-compatible", "https://x", "", (model,)),))
        ids = {x["id"] for x in AcpServer._config_options(Session("s", "", "M"), cfg)}
        self.assertIn("effortLevel", ids)
        self.assertNotIn("thinkingMode", ids)

    def test_ui_contains_disabled_or_three_state_model_control(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "ui" / "index.mjs").read_text(encoding="utf-8")
        self.assertIn("['auto','on','off']", source)
        self.assertIn('tr("ui.2b3bf6b45b0e")', source)
        self.assertIn('tr("ui.15ae7f733cfa")', source)
        self.assertIn("/thinking`,{method:'PUT'", source)


if __name__ == "__main__":
    unittest.main()
