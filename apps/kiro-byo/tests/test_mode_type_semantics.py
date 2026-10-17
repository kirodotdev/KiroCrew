from __future__ import annotations

import os
import tempfile
import unittest
from io import StringIO

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.protocol import parse_type_selection, type_selection_id
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.policy_packs import runtime_policy_context


class CaptureOutput(StringIO):
    def messages(self):
        import json
        return [json.loads(line) for line in self.getvalue().splitlines() if line.strip()]


def config_with_models() -> KiroByoConfig:
    flash = ModelSpec(
        provider_id="deepseek",
        provider_name="DeepSeek Official",
        model_id="deepseek-flash",
        name="DeepSeek-V4.1-Flash",
        effort_supported_levels=("low", "high"),
        effort_default_level="low",
    )
    pro = ModelSpec(
        provider_id="deepseek",
        provider_name="DeepSeek Official",
        model_id="deepseek-pro",
        name="DeepSeek-V4-Pro",
        effort_supported_levels=("low", "high", "max"),
        effort_default_level="high",
    )
    provider = ProviderSpec(
        id="deepseek",
        name="DeepSeek Official",
        type="openai-compatible",
        base_url="https://example.invalid/v1",
        api_key="",
        models=(flash, pro),
        default_model="deepseek-flash",
    )
    return KiroByoConfig(providers=(provider,), default_model="DeepSeek-V4.1-Flash")


class ModeTypeSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = self.tmp.name
        self.config = config_with_models()
        self.orig_config = AcpServer._config
        AcpServer._config = staticmethod(lambda: self.config)

    def tearDown(self) -> None:
        AcpServer._config = self.orig_config
        if self.old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = self.old_home
        self.tmp.cleanup()


    def test_type_wire_ids_are_mode_namespaced_and_legacy_form_is_invalid(self) -> None:
        tiga = type_selection_id("tiga", "power")
        dyna = type_selection_id("dyna", "power")
        self.assertEqual(tiga, "kiro-byo-type:tiga:power")
        self.assertEqual(dyna, "kiro-byo-type:dyna:power")
        self.assertNotEqual(tiga, dyna)
        parsed = parse_type_selection(tiga)
        self.assertIsNotNone(parsed)
        self.assertEqual((parsed.mode_id, parsed.type_id), ("tiga", "power"))
        self.assertIsNone(parse_type_selection("kiro-byo-type:power"))

    def test_explicit_type_from_another_mode_is_rejected(self) -> None:
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        session = Session("s", "", type_selection_id("tiga", "multi"))
        self.assertFalse(server._set_model(session, self.config, type_selection_id("dyna", "power")))
        self.assertEqual(session.model, type_selection_id("tiga", "multi"))
        server._executor.shutdown(wait=True)

    def test_tiga_types_are_advertised_before_real_models_but_jev_is_not_owned(self) -> None:
        current = type_selection_id("tiga", "multi")
        models = AcpServer.models_payload(current, self.config)
        ids = [item["modelId"] for item in models["availableModels"]]
        names = [item["name"] for item in models["availableModels"]]
        self.assertEqual(ids[:4], [type_selection_id("tiga", x) for x in ("sky", "multi", "power", "glitter")])
        self.assertEqual(names[:4], ["Sky Type", "Multi Type", "Power Type", "Glitter Type"])
        self.assertIn("DeepSeek-V4.1-Flash", ids)
        self.assertIn("DeepSeek-V4-Pro", ids)
        self.assertNotIn("auto:jev", ids)
        self.assertEqual(models["currentModelId"], current)

    def test_type_resolves_to_router_representative_without_losing_selection(self) -> None:
        selection = type_selection_id("tiga", "multi")
        session = Session("s", "", selection)
        resolved = AcpServer._resolved_model(self.config, session)
        self.assertIsNotNone(resolved)
        _provider, model = resolved
        self.assertIn(model.name, {"DeepSeek-V4.1-Flash", "DeepSeek-V4-Pro"})
        self.assertEqual(session.model, selection)

    def test_type_effort_is_live_session_capability_of_effective_route(self) -> None:
        session = Session("s", "", type_selection_id("tiga", "multi"), effort="low")
        options = AcpServer._config_options(session, self.config)
        by_id = {item["id"]: item for item in options}
        self.assertEqual(by_id["model"]["currentValue"], type_selection_id("tiga", "multi"))
        self.assertEqual(
            [x["value"] for x in by_id["effortLevel"]["options"]],
            ["low", "high"],
        )

    def test_deep_thinking_is_managed_per_model_while_effort_stays_in_crew(self) -> None:
        m = ModelSpec(
            provider_id="p", provider_name="P", model_id="m", name="M",
            effort_supported_levels=("low", "medium", "high"), effort_default_level="medium",
            reasoning_mode="levels", reasoning_supported=True, reasoning_can_disable=True,
            reasoning_default_enabled=True, reasoning_disable_value="none",
        )
        cfg = KiroByoConfig(providers=(ProviderSpec(
            id="p", name="P", type="openai-compatible", base_url="https://example.invalid/v1",
            api_key="", models=(m,), default_model="m",
        ),), default_model="M")
        options = AcpServer._config_options(Session("s", "", "M", effort="high"), cfg)
        ids = {item["id"] for item in options}
        self.assertNotIn("thinkingMode", ids)
        self.assertIn("effortLevel", ids)

    def test_concrete_model_and_type_switch_are_both_live(self) -> None:
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        session = Session("s", "", type_selection_id("tiga", "multi"), effort="low")
        self.assertTrue(server._set_model(session, self.config, "DeepSeek-V4-Pro"))
        self.assertEqual(session.model, "DeepSeek-V4-Pro")
        # A user override survives a model switch when the target accepts it.
        self.assertEqual(session.effort, "low")
        session.effort = "max"
        self.assertTrue(server._set_model(session, self.config, type_selection_id("tiga", "power")))
        self.assertEqual(session.model, type_selection_id("tiga", "power"))
        # Back to a BYO Type preserves the user's routing intent. The per-turn Router
        # will prefer a candidate that can actually honor max.
        self.assertEqual(session.effort, "max")
        self.assertFalse(server._set_model(session, self.config, "auto:jev"))
        server._executor.shutdown(wait=True)

    def test_session_start_payload_keeps_selected_type(self) -> None:
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        selected = type_selection_id("tiga", "multi")
        session = Session("s", "", selected, effort="low")
        payload = server._session_start_payload(session, self.config)
        self.assertEqual(payload["models"]["currentModelId"], selected)
        model_option = next(x for x in payload["configOptions"] if x.get("id") == "model")
        self.assertEqual(model_option["currentValue"], selected)
        server._executor.shutdown(wait=True)

    def test_picker_payload_uses_short_type_descriptions(self) -> None:
        payload = AcpServer.models_payload(type_selection_id("tiga", "multi"), self.config)
        rows = {row["modelId"]: row for row in payload["availableModels"]}
        sky = rows[type_selection_id("tiga", "sky")]
        self.assertEqual(sky["description"], "快 · 省｜只用快速档模型，最快最省")
        self.assertNotIn("快速档全部", sky["description"])

    def test_runtime_policy_context_uses_authoritative_tiga_names(self) -> None:
        text = runtime_policy_context("tiga", "sky", active=True)
        self.assertIn("Current configured Mode: Tiga", text)
        self.assertNotIn("Tiga（", text)
        self.assertIn("Sky Type（空中型）", text)
        self.assertIn("Multi Type（复合型）", text)
        self.assertIn("Power Type（强力型）", text)
        self.assertIn("Glitter Type（闪耀型）", text)
        self.assertNotIn("天空型", text)
        self.assertIn("Type is an execution/routing policy", text)

    def test_runtime_metadata_can_include_mode_type_context(self) -> None:
        provider, model = AcpServer._resolved_model(self.config, Session("s", "", type_selection_id("tiga", "sky")))
        mode_context = runtime_policy_context("tiga", "sky", active=True)
        content = AcpServer._runtime_system_message(provider, model, mode_context=mode_context)["content"]
        self.assertIn("Active Type: Sky Type（空中型）", content)
        self.assertIn("Served upstream model: DeepSeek-V4.1-Flash", content)

    def test_runtime_metadata_does_not_rename_the_crew_agent(self) -> None:
        provider, model = AcpServer._resolved_model(self.config, Session("s", "", type_selection_id("tiga", "multi")))
        content = AcpServer._runtime_system_message(provider, model)["content"]
        self.assertNotIn("You are ", content)
        self.assertNotIn("Do not claim", content)
        self.assertNotIn("your name is", content.lower())
        self.assertIn("Runtime metadata for this turn", content)
        self.assertIn("Served upstream model: DeepSeek-V4.1-Flash", content)

    def test_echo_diagnostics_do_not_advertise_types_without_provider_models(self) -> None:
        empty = KiroByoConfig(providers=())
        ids = [x[0] for x in AcpServer.available_models(empty)]
        self.assertFalse(any(x.startswith("kiro-byo-type:") for x in ids))
        self.assertIn("kiro-byo/echo", ids)


if __name__ == "__main__":
    unittest.main()
