"""Behavioural contract for the four Tiga forms.

A form is a strategy the user selects; each turn the form reads the task and
chooses the model(s). These tests pin that behaviour with a two-model setup
matching a typical BYO install (one fast tier, one strong tier, both with tools),
so a form can never silently collapse into "one form == one model" again.
"""
from __future__ import annotations

import unittest

from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.mode_router import build_execution_plan, route_model
from kiro_byo.policy_packs import get_policy_pack

FLASH = "MiMo-V2.6-Flash"
PRO = "MiMo-V2.6-Pro"

TRIVIAL = "把这句话翻译成英文：今天天气很好"
CHAT = "帮我想一个周末出去玩的计划"
CODING = "帮我修一下这个 python 函数的 bug"
HARD = "请分析这个系统的架构设计，重构核心模块的代码并补上测试"


def _config() -> KiroByoConfig:
    flash = ModelSpec(
        provider_id="xiaomi", provider_name="Xiaomi", model_id=FLASH, name=FLASH,
        context_window=256_000, effort_supported_levels=("low", "medium", "high"),
        reasoning_supported=True, tool_use=True, input_modalities=("text", "image"),
    )
    pro = ModelSpec(
        provider_id="xiaomi", provider_name="Xiaomi", model_id=PRO, name=PRO,
        context_window=256_000, effort_supported_levels=("low", "medium", "high"),
        reasoning_supported=True, tool_use=True, input_modalities=("text", "image"),
    )
    provider = ProviderSpec("xiaomi", "Xiaomi", "openai-compatible", "https://example.invalid/v1", "", (flash, pro))
    return KiroByoConfig((provider,), mode_type="multi")


def _route(policy: str, prompt: str, *, history=None, effort: str = ""):
    decision = route_model(
        _config(), pack="tiga", policy=policy, prompt=prompt, history=history or [],
        has_image=False, tools_expected=True, reasoning_effort=effort,
    )
    assert decision is not None
    return decision


class TigaFormsAdaptToTheTask(unittest.TestCase):
    def test_sky_stays_in_the_fast_tier_even_for_hard_work(self) -> None:
        for prompt in (TRIVIAL, CODING, HARD):
            with self.subTest(prompt=prompt):
                decision = _route("sky", prompt)
                self.assertEqual(decision.model.model_id, FLASH)
                self.assertIn("fast-tier pool", decision.reason)

    def test_multi_behaves_like_auto(self) -> None:
        self.assertEqual(_route("multi", TRIVIAL).model.model_id, FLASH)
        self.assertEqual(_route("multi", CHAT).model.model_id, FLASH)
        self.assertEqual(_route("multi", HARD).model.model_id, PRO)

    def test_power_stays_in_the_strong_tier_even_for_trivial_turns(self) -> None:
        for prompt in (TRIVIAL, CHAT, CODING, HARD):
            with self.subTest(prompt=prompt):
                self.assertEqual(_route("power", prompt).model.model_id, PRO)

    def test_glitter_always_uses_the_strongest_model(self) -> None:
        for prompt in (TRIVIAL, CHAT, CODING, HARD):
            with self.subTest(prompt=prompt):
                self.assertEqual(_route("glitter", prompt).model.model_id, PRO)

    def test_forms_differ_on_the_same_task(self) -> None:
        picks = {policy: _route(policy, HARD).model.model_id for policy in ("sky", "multi", "power", "glitter")}
        self.assertEqual(picks, {"sky": FLASH, "multi": PRO, "power": PRO, "glitter": PRO})
        picks = {policy: _route(policy, TRIVIAL).model.model_id for policy in ("sky", "multi", "power", "glitter")}
        self.assertEqual(picks, {"sky": FLASH, "multi": FLASH, "power": PRO, "glitter": PRO})


class ThinkingPreferenceRouting(unittest.TestCase):
    @staticmethod
    def _config(tier: str) -> KiroByoConfig:
        def model(model_id: str, pref: str) -> ModelSpec:
            return ModelSpec(
                provider_id="p", provider_name="P", model_id=model_id, name=model_id,
                tier=tier, tool_use=True, input_modalities=("text",),
                reasoning_supported=True, reasoning_can_disable=True,
                reasoning_default_enabled=None, thinking_preference=pref,
                routing_quality=0.7, routing_speed=0.7, routing_economy=0.7,
            )
        provider = ProviderSpec(
            "p", "P", "openai-compatible", "https://example.invalid/v1", "",
            (model("a-on", "on"), model("b-off", "off")),
        )
        return KiroByoConfig((provider,), mode_type="multi")

    def test_sky_prefers_an_equivalent_fast_model_whose_user_lock_matches_economy(self) -> None:
        decision = route_model(
            self._config("fast"), pack="tiga", policy="sky", prompt=TRIVIAL, history=[],
            has_image=False, tools_required=False, tools_available=True,
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.model.model_id, "b-off")

    def test_power_prefers_an_equivalent_strong_model_whose_user_lock_matches_quality(self) -> None:
        decision = route_model(
            self._config("strong"), pack="tiga", policy="power", prompt=TRIVIAL, history=[],
            has_image=False, tools_required=False, tools_available=True,
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.model.model_id, "a-on")

    def test_multi_changes_thinking_alignment_with_the_task_without_changing_type(self) -> None:
        cfg = self._config("balanced")
        easy = route_model(
            cfg, pack="tiga", policy="multi", prompt=TRIVIAL, history=[],
            has_image=False, tools_required=False, tools_available=True,
        )
        hard = route_model(
            cfg, pack="tiga", policy="multi", prompt=HARD, history=[],
            has_image=False, tools_required=False, tools_available=True,
        )
        self.assertIsNotNone(easy)
        self.assertIsNotNone(hard)
        self.assertEqual(easy.policy, "multi")
        self.assertEqual(hard.policy, "multi")
        self.assertEqual(easy.model.model_id, "b-off")
        self.assertEqual(hard.model.model_id, "a-on")

    def test_glitter_strong_leader_prefers_reasoning_aligned_peer(self) -> None:
        decision = route_model(
            self._config("strong"), pack="tiga", policy="glitter", prompt=HARD, history=[],
            has_image=False, tools_required=False, tools_available=True,
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.model.model_id, "a-on")


class TierPools(unittest.TestCase):
    def _config(self, *, flash_image: bool, pro_image: bool) -> KiroByoConfig:
        def spec(model_id: str, image: bool) -> ModelSpec:
            return ModelSpec(
                provider_id="xiaomi", provider_name="Xiaomi", model_id=model_id, name=model_id,
                context_window=256_000, reasoning_supported=True, tool_use=True,
                input_modalities=("text", "image") if image else ("text",),
            )
        provider = ProviderSpec(
            "xiaomi", "Xiaomi", "openai-compatible", "https://example.invalid/v1", "",
            (spec(FLASH, flash_image), spec(PRO, pro_image)),
        )
        return KiroByoConfig((provider,), mode_type="multi")

    def test_sky_leaves_the_fast_tier_only_when_no_fast_model_can_run_the_turn(self) -> None:
        decision = route_model(
            self._config(flash_image=False, pro_image=True), pack="tiga", policy="sky",
            prompt="这张图里是什么", history=[], has_image=True, tools_expected=True,
        )
        self.assertEqual(decision.model.model_id, PRO)
        self.assertIn("no fast-tier model can run this turn; outside-tier fallback", decision.reason)

    def test_power_fails_closed_when_no_strong_model_can_run_the_turn(self) -> None:
        decision = route_model(
            self._config(flash_image=True, pro_image=False), pack="tiga", policy="power",
            prompt="这张图里是什么", history=[], has_image=True, tools_expected=True,
        )
        self.assertIsNone(decision)

    def test_glitter_fails_closed_without_a_capable_strong_leader(self) -> None:
        decision = route_model(
            self._config(flash_image=True, pro_image=False), pack="tiga", policy="glitter",
            prompt="这张图里是什么", history=[], has_image=True, tools_expected=True,
        )
        self.assertIsNone(decision)

    def test_manual_tier_wins_over_the_name(self) -> None:
        from kiro_byo.mode_router import model_tier

        self.assertEqual(model_tier(ModelSpec("p", "P", "x-flash", "X Flash")), "fast")
        self.assertEqual(model_tier(ModelSpec("p", "P", "x-flash", "X Flash", tier="strong")), "strong")
        self.assertEqual(model_tier(ModelSpec("p", "P", "x-pro", "X Pro")), "strong")
        self.assertEqual(model_tier(ModelSpec("p", "P", "x", "X")), "balanced")


class SecondaryStageSemantics(unittest.TestCase):
    def test_power_does_not_verify_every_turn_just_because_tools_exist(self) -> None:
        plan = build_execution_plan(_route("power", CHAT))
        self.assertIsNone(plan.post_stage)

    def test_power_verifies_code_with_an_equal_or_stronger_model(self) -> None:
        plan = build_execution_plan(_route("power", CODING))
        self.assertIsNotNone(plan.post_stage)
        self.assertEqual(plan.post_stage.model.model_id, PRO)

    def test_glitter_review_is_never_done_by_a_weaker_model(self) -> None:
        plan = build_execution_plan(_route("glitter", HARD))
        self.assertIsNotNone(plan.post_stage)
        self.assertEqual(plan.post_stage.role, "review")
        self.assertEqual(plan.post_stage.model.model_id, PRO)

    def test_multi_does_not_verify_ordinary_turns(self) -> None:
        self.assertIsNone(build_execution_plan(_route("multi", CODING)).post_stage)


class PackData(unittest.TestCase):
    def test_form_order_and_intents(self) -> None:
        pack = get_policy_pack("tiga")
        self.assertIsNotNone(pack)
        self.assertEqual([p.id for p in pack.policies], ["sky", "multi", "power", "glitter"])
        self.assertEqual(pack.default_policy, "multi")
        self.assertEqual(pack.localized_name, "")
        by_id = {p.id: p for p in pack.policies}
        self.assertEqual(by_id["sky"].primary_reasoning, "economy")
        self.assertEqual(by_id["multi"].primary_reasoning, "inherit")
        self.assertEqual(by_id["power"].primary_reasoning, "high")
        self.assertEqual(by_id["glitter"].primary_reasoning, "maximum")
        self.assertEqual(by_id["sky"].localized_name, "空中型")
        self.assertEqual(by_id["multi"].localized_name, "复合型")
        self.assertEqual(by_id["power"].localized_name, "强力型")
        self.assertEqual(by_id["glitter"].localized_name, "闪耀型")
        self.assertEqual(by_id["sky"].short_description, "快 · 省｜只用快速档模型，最快最省")
        self.assertEqual(by_id["multi"].short_description, "默认 · Auto｜在所有模型里按任务挑选")
        self.assertEqual(by_id["power"].short_description, "强 · 稳｜只用强力档模型，质量可靠优先")
        self.assertEqual(by_id["glitter"].short_description, "长任务 · 协作｜多模型分工协作，汇聚全部力量")
        self.assertIn("快速角色池", by_id["sky"].description)
        self.assertTrue(by_id["glitter"].sticky)
        self.assertFalse(by_id["multi"].sticky)
        self.assertEqual(by_id["glitter"].secondary.reviewer, "not_weaker")
        self.assertEqual(by_id["power"].secondary.reviewer, "not_weaker")
        self.assertNotIn("tools", [k for k, v in vars(by_id["power"].secondary.trigger).items() if v is True])
        self.assertEqual(by_id["sky"].routing.tier_pool, "fast")
        self.assertEqual(by_id["multi"].routing.tier_pool, "any")
        self.assertEqual(by_id["power"].routing.tier_pool, "strong")
        self.assertEqual(by_id["glitter"].routing.tier_pool, "strong")
        self.assertEqual(by_id["sky"].routing.tier_fallback, "capability")
        self.assertEqual(by_id["power"].routing.tier_fallback, "strict")
        self.assertEqual(by_id["glitter"].routing.tier_fallback, "strict")
        self.assertEqual(by_id["glitter"].collaboration_tier_pool, "fast")


class PickerStyleGuard(unittest.TestCase):
    def test_guard_hides_only_legacy_type_rows_and_upgrades_old_guard_in_place(self) -> None:
        from kiro_byo import crew_dashboard_patch as dp

        shell = "<html><head><title>x</title></head><body></body></html>"
        first, changed = dp._inject_guard(shell)
        self.assertTrue(changed)
        self.assertIn('[data-model-id^="kiro-byo-type:"]', first)
        self.assertIn("display: none !important", first)
        self.assertIn("具体模型 · 固定使用", first)
        self.assertIn("kiro-byo-chat-surface", first)
        self.assertEqual(dp._inject_guard(first), (first, False))

        old_block = (
            f'\n  <style id="{dp.HTML_GUARD_ID}">\n    /* {dp.HTML_GUARD_MARKER} */\n'
            "    .old { display: none; }\n  </style>\n"
        )
        old = shell.replace("</head>", old_block + "</head>")
        upgraded, changed = dp._inject_guard(old)
        self.assertTrue(changed)
        self.assertNotIn(".old", upgraded)
        self.assertEqual(upgraded.count(dp.HTML_GUARD_MARKER), 1)
        self.assertIn('[data-model-id^="kiro-byo-type:"]', upgraded)


if __name__ == "__main__":
    unittest.main()
