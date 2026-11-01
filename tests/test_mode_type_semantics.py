from __future__ import annotations

import os
import tempfile
import unittest
from io import StringIO

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.agent_projection import AgentProjection
from kiro_byo.protocol import parse_type_selection, type_selection_id
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.policy_packs import (
    get_policy_pack,
    policy_catalogue_requested,
    runtime_policy_context,
    runtime_policy_context_for_request,
)


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

    def test_shared_model_catalogue_advertises_auto_and_real_models_only(self) -> None:
        current = type_selection_id("tiga", "multi")
        models = AcpServer.models_payload(current, self.config)
        ids = [item["modelId"] for item in models["availableModels"]]
        names = [item["name"] for item in models["availableModels"]]
        self.assertEqual(ids[0], "auto")
        self.assertEqual(names[0], "Auto")
        self.assertIn("DeepSeek-V4.1-Flash", ids)
        self.assertIn("DeepSeek-V4-Pro", ids)
        self.assertFalse(any(value.startswith("kiro-byo-type:") for value in ids))
        self.assertNotIn("auto:jev", ids)
        self.assertEqual(models["currentModelId"], "auto")


    def test_interactive_models_payload_advertises_all_active_types_with_display_names(self) -> None:
        selected = type_selection_id("tiga", "multi")
        payload = AcpServer.models_payload(selected, self.config, include_types=True)
        rows = {row["modelId"]: row for row in payload["availableModels"]}
        self.assertEqual(payload["currentModelId"], selected)
        self.assertEqual(rows[type_selection_id("tiga", "multi")]["name"], "Tiga — Multi")
        self.assertEqual(rows[type_selection_id("tiga", "power")]["name"], "Tiga — Power")
        self.assertIn("DeepSeek-V4-Pro", rows)

    def test_type_resolves_to_router_representative_without_losing_selection(self) -> None:
        selection = type_selection_id("tiga", "multi")
        session = Session("s", "", selection)
        resolved = AcpServer._resolved_model(self.config, session)
        self.assertIsNotNone(resolved)
        _provider, model = resolved
        self.assertIn(model.name, {"DeepSeek-V4.1-Flash", "DeepSeek-V4-Pro"})
        self.assertEqual(session.model, selection)

    def test_type_effort_is_internal_and_interactive_model_control_is_truthful(self) -> None:
        selection = type_selection_id("tiga", "multi")
        session = Session("s", "", selection, effort="low")
        options = AcpServer._config_options(session, self.config)
        by_id = {item["id"]: item for item in options}
        self.assertEqual(by_id["model"]["currentValue"], selection)
        option_ids = {str(row.get("value", "")) for row in by_id["model"]["options"]}
        self.assertIn(type_selection_id("tiga", "power"), option_ids)
        self.assertNotIn("effortLevel", by_id)

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

    def test_background_auto_materializes_one_concrete_model_without_touching_chat_auto(self) -> None:
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        bg = Session(
            "bg",
            "",
            type_selection_id("tiga", "multi"),
            agent=AgentProjection(agent_id="kirocrew-lite"),
        )
        self.assertTrue(server._set_model(bg, self.config, "auto"))
        self.assertEqual(bg.selection_kind, "concrete")
        self.assertEqual(bg.selection_source, "background_auto")
        self.assertEqual(bg.selection_requested, "auto")
        self.assertFalse(bg.model.startswith("kiro-byo-type:"))
        self.assertIn(bg.model, {public_id for public_id, _model in self.config.public_models})

        chat = Session("chat", "", type_selection_id("tiga", "multi"))
        self.assertTrue(server._set_model(chat, self.config, "auto"))
        self.assertEqual(chat.selection_kind, "type")
        self.assertEqual(chat.model, type_selection_id("tiga", "multi"))
        server._executor.shutdown(wait=True)

    def test_concrete_model_and_type_switch_are_both_live(self) -> None:
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        session = Session("s", "", type_selection_id("tiga", "multi"), effort="low")
        self.assertTrue(server._set_model(session, self.config, "DeepSeek-V4-Pro"))
        self.assertEqual(session.model, "DeepSeek-V4-Pro")
        # A user override survives a model switch when the target accepts it.
        self.assertEqual(session.effort, "low")
        session.effort = "max"
        session.effort_user_set = True
        self.assertTrue(server._set_model(session, self.config, type_selection_id("tiga", "power")))
        self.assertEqual(session.model, type_selection_id("tiga", "power"))
        # Back to a BYO Type drops concrete-model effort state. The Type decides
        # effort only after it picks the concrete model for each invocation.
        self.assertEqual(session.effort, "")
        self.assertFalse(session.effort_user_set)
        self.assertFalse(server._set_model(session, self.config, "auto:jev"))
        server._executor.shutdown(wait=True)

    def test_prompt_after_type_to_concrete_switch_bypasses_router_cleanly(self) -> None:
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        session = Session("switch-prompt", self.tmp.name, type_selection_id("tiga", "multi"), effort="low")
        server._put_session(session)
        self.assertTrue(server._set_model(session, self.config, "DeepSeek-V4-Pro"))

        captured = {}

        def fake_loop(*args, **kwargs):
            captured["provider"] = args[3]
            captured["model"] = args[4]
            captured["routing_decision"] = kwargs.get("routing_decision")

        server._run_provider_tool_loop = fake_loop
        server._handle_prompt_request(
            1,
            {
                "sessionId": session.session_id,
                "prompt": [{"type": "text", "text": "介绍一下你自己"}],
            },
        )

        self.assertEqual(captured["provider"].id, "deepseek")
        self.assertEqual(captured["model"].name, "DeepSeek-V4-Pro")
        self.assertIsNone(captured["routing_decision"])
        server._executor.shutdown(wait=True)

    def test_interactive_session_start_payload_advertises_real_type_selection(self) -> None:
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        selected = type_selection_id("tiga", "multi")
        session = Session("s", "", selected, effort="low")
        payload = server._session_start_payload(session, self.config)
        self.assertEqual(payload["models"]["currentModelId"], selected)
        advertised = {row["modelId"] for row in payload["models"]["availableModels"]}
        self.assertIn(selected, advertised)
        self.assertIn(type_selection_id("tiga", "power"), advertised)
        model_option = next(x for x in payload["configOptions"] if x.get("id") == "model")
        self.assertEqual(model_option["currentValue"], selected)
        self.assertIn(type_selection_id("tiga", "power"), {x.get("value") for x in model_option["options"]})
        server._executor.shutdown(wait=True)

    def test_background_session_start_payload_stays_concrete_only(self) -> None:
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        selected = type_selection_id("tiga", "multi")
        session = Session(
            "bg",
            "",
            selected,
            agent=AgentProjection(agent_id="kirocrew-lite"),
        )
        payload = server._session_start_payload(session, self.config)
        self.assertEqual(payload["models"]["currentModelId"], "auto")
        advertised = {row["modelId"] for row in payload["models"]["availableModels"]}
        self.assertFalse(any(value.startswith("kiro-byo-type:") for value in advertised))
        model_option = next(x for x in payload["configOptions"] if x.get("id") == "model")
        self.assertFalse(any(str(x.get("value", "")).startswith("kiro-byo-type:") for x in model_option["options"]))
        server._executor.shutdown(wait=True)

    def test_picker_payload_never_exposes_type_descriptions_as_models(self) -> None:
        payload = AcpServer.models_payload(type_selection_id("tiga", "multi"), self.config)
        rows = {row["modelId"]: row for row in payload["availableModels"]}
        self.assertIn("auto", rows)
        self.assertFalse(any(value.startswith("kiro-byo-type:") for value in rows))

    def test_runtime_policy_context_uses_authoritative_tiga_names(self) -> None:
        text = runtime_policy_context("tiga", "sky", active=True, include_catalogue=True)
        self.assertIn("Current configured Mode: Tiga", text)
        self.assertNotIn("Tiga（", text)
        self.assertIn("Sky Type（空中型）", text)
        self.assertIn("Multi Type（复合型）", text)
        self.assertIn("Power Type（强力型）", text)
        self.assertIn("Glitter Type（闪耀型）", text)
        self.assertNotIn("天空型", text)
        self.assertIn("Type is an execution/routing policy", text)


    def test_runtime_context_does_not_inject_type_switching_commands(self) -> None:
        text = runtime_policy_context("tiga", "multi", active=True)
        # Type changes are a KiroCrew control-plane action, not a prompt rule.
        # Runtime metadata may explain the forms but should not read like a
        # prohibition skill or coach the model to police the selector.
        self.assertNotIn("Type selection is owned by the user", text)
        self.assertNotIn("must never switch", text)
        self.assertNotIn("the user must choose it in KiroCrew", text)
        self.assertNotIn("不会替用户切换 Type", text)
        self.assertNotIn("Type control surface:", text)

    def test_normal_runtime_context_only_describes_current_type(self) -> None:
        text = runtime_policy_context("tiga", "multi", active=True)
        self.assertIn("Active Type: Multi Type（复合型）", text)
        self.assertNotIn("Sky Type（空中型）", text)
        self.assertNotIn("Power Type（强力型）", text)
        self.assertNotIn("Glitter Type（闪耀型）", text)
        self.assertNotIn("Installed Modes:", text)

    def test_type_catalogue_is_only_requested_for_product_semantics(self) -> None:
        self.assertTrue(policy_catalogue_requested("解释一下四个形态", "tiga"))
        self.assertTrue(policy_catalogue_requested("What is the difference between Sky Type and Power Type?", "tiga"))
        self.assertTrue(policy_catalogue_requested("Tiga 模式下有哪些 Type？", "tiga"))
        self.assertTrue(policy_catalogue_requested("当前形态是什么意思？", "tiga"))
        self.assertFalse(policy_catalogue_requested("帮我看看 C 盘哪些东西可以清理", "tiga"))
        self.assertFalse(policy_catalogue_requested("Explain Python type annotations", "tiga"))

    def test_normal_agent_turn_gets_no_mode_type_product_prompt(self) -> None:
        self.assertEqual(
            runtime_policy_context_for_request(
                "tiga", "multi", "帮我看看 C 盘哪些东西可以清理", active=True
            ),
            "",
        )
        explained = runtime_policy_context_for_request(
            "tiga", "multi", "解释一下四个形态", active=True
        )
        self.assertIn("Sky Type（空中型）", explained)
        self.assertIn("Glitter Type（闪耀型）", explained)

    def test_tiga_default_type_is_multi(self) -> None:
        pack = get_policy_pack("tiga")
        self.assertIsNotNone(pack)
        self.assertEqual(pack.default_policy, "multi")

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
