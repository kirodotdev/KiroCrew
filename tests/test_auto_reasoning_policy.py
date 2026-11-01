from __future__ import annotations

from types import SimpleNamespace

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import ModelSpec
from kiro_byo.policy_packs import get_policy_pack
from kiro_byo.reasoning_control import ReasoningInvocation, resolve_effort_level, resolve_thinking_mode


def _model(*, pref: str = "auto", levels=("low", "medium", "high"), can_disable=True, supported=True):
    return ModelSpec(
        provider_id="p", provider_name="P", model_id="m", name="M",
        effort_supported_levels=tuple(levels), effort_default_level=(levels[1] if len(levels) > 1 else (levels[0] if levels else "")),
        reasoning_mode="levels" if levels else "toggle",
        reasoning_supported=supported, reasoning_can_disable=can_disable,
        reasoning_default_enabled=True if supported else False,
        reasoning_disable_value="none" if can_disable else "",
        reasoning_enable_value="enabled" if can_disable else "",
        thinking_preference=pref,
    )


def _signals(complexity: float, *, code=False, tools=False, image=False):
    return SimpleNamespace(
        complexity=complexity,
        code_task=code,
        tools_expected=tools,
        has_image=image,
    )


def test_tiga_pack_declares_generic_auto_reasoning_strategies():
    pack = get_policy_pack("tiga")
    assert pack is not None
    assert (pack.policy("sky").auto_reasoning.thinking, pack.policy("sky").auto_reasoning.effort) == ("prefer_off", "lowest")
    assert (pack.policy("multi").auto_reasoning.thinking, pack.policy("multi").auto_reasoning.effort) == ("adaptive", "adaptive")
    assert (pack.policy("power").auto_reasoning.thinking, pack.policy("power").auto_reasoning.effort) == ("prefer_on", "highest")
    assert (pack.policy("glitter").auto_reasoning.thinking, pack.policy("glitter").auto_reasoning.effort) == ("adaptive_per_call", "adaptive_per_call")


def test_sky_auto_is_off_but_user_on_is_hard_lock_and_uses_lowest_effort():
    server = AcpServer()
    try:
        sky = get_policy_pack("tiga").policy("sky").auto_reasoning
        auto = _model(pref="auto", levels=("minimal", "low", "medium", "high", "xhigh"))
        thinking, effort = server._resolve_invocation_reasoning(auto, Session("s", "", "M"), sky, _signals(0.9), role="lead", stage="planning")
        assert (thinking, effort) == ("off", "")

        locked_on = _model(pref="on", levels=("minimal", "low", "medium", "high", "xhigh"))
        thinking, effort = server._resolve_invocation_reasoning(locked_on, Session("s", "", "M"), sky, _signals(0.9), role="lead", stage="planning")
        assert (thinking, effort) == ("on", "minimal")
    finally:
        server._executor.shutdown(wait=True)


def test_power_auto_is_on_and_uses_highest_model_specific_effort():
    server = AcpServer()
    try:
        power = get_policy_pack("tiga").policy("power").auto_reasoning
        for levels, expected in [
            (("low", "high"), "high"),
            (("low", "medium", "high"), "high"),
            (("minimal", "low", "medium", "high", "xhigh"), "xhigh"),
        ]:
            model = _model(levels=levels)
            thinking, effort = server._resolve_invocation_reasoning(model, Session("s", "", "M"), power, _signals(0.1), role="lead", stage="planning")
            assert thinking == "on"
            assert effort == expected
    finally:
        server._executor.shutdown(wait=True)


def test_explicit_effort_is_hard_lock_even_under_power():
    server = AcpServer()
    try:
        power = get_policy_pack("tiga").policy("power").auto_reasoning
        model = _model(levels=("low", "medium", "high", "xhigh"))
        session = Session("s", "", "M", effort="medium", effort_user_set=True)
        thinking, effort = server._resolve_invocation_reasoning(model, session, power, _signals(1.0), role="lead", stage="planning")
        assert thinking == "on"
        assert effort == "medium"
    finally:
        server._executor.shutdown(wait=True)


def test_user_thinking_off_suppresses_effort_even_when_effort_is_locked():
    server = AcpServer()
    try:
        power = get_policy_pack("tiga").policy("power").auto_reasoning
        model = _model(pref="off", levels=("low", "medium", "high"))
        session = Session("s", "", "M", effort="high", effort_user_set=True)
        thinking, effort = server._resolve_invocation_reasoning(model, session, power, _signals(1.0), role="lead", stage="planning")
        assert (thinking, effort) == ("off", "")
    finally:
        server._executor.shutdown(wait=True)


def test_fixed_on_and_unsupported_models_cannot_be_changed_by_type():
    server = AcpServer()
    try:
        sky = get_policy_pack("tiga").policy("sky").auto_reasoning
        fixed = _model(can_disable=False, supported=True, levels=("low", "medium", "high"))
        assert server._resolve_invocation_reasoning(fixed, Session("s", "", "M"), sky, _signals(0.1), role="lead", stage="planning") == ("on", "low")

        unsupported = _model(can_disable=False, supported=False, levels=())
        assert server._resolve_invocation_reasoning(unsupported, Session("s", "", "M"), get_policy_pack("tiga").policy("power").auto_reasoning, _signals(1.0), role="lead", stage="planning") == ("off", "")
    finally:
        server._executor.shutdown(wait=True)


def test_multi_auto_uses_task_need_not_a_fixed_middle_level():
    server = AcpServer()
    try:
        multi = get_policy_pack("tiga").policy("multi").auto_reasoning
        model = _model(levels=("minimal", "low", "medium", "high", "xhigh"))
        easy = server._resolve_invocation_reasoning(model, Session("s1", "", "M"), multi, _signals(0.18), role="lead", stage="turn")
        hard = server._resolve_invocation_reasoning(model, Session("s2", "", "M"), multi, _signals(0.9, code=True, tools=True), role="lead", stage="turn")
        assert easy == ("off", "")
        assert hard[0] == "on"
        assert hard[1] in {"high", "xhigh"}
    finally:
        server._executor.shutdown(wait=True)


def test_glitter_auto_is_per_invocation_not_leader_on_worker_off_rule():
    server = AcpServer()
    try:
        glitter = get_policy_pack("tiga").policy("glitter").auto_reasoning
        model = _model(levels=("low", "medium", "high", "xhigh"))
        session = Session("s", "", "M")

        # Same AUTO model, same user turn: a planning invocation deserves more
        # reasoning than a mechanical gathering invocation.
        planning = server._resolve_invocation_reasoning(model, session, glitter, _signals(0.58, tools=True), role="lead", stage="planning")
        gathering = server._resolve_invocation_reasoning(model, session, glitter, _signals(0.58, tools=True), role="worker", stage="gathering")
        assert planning[0] == "on"
        assert gathering == ("off", "")

        # Worker is not permanently OFF: a sufficiently difficult delegated
        # subtask may turn reasoning on when the model preference is AUTO.
        hard_worker = server._resolve_invocation_reasoning(model, session, glitter, _signals(0.92, code=True, tools=True), role="worker", stage="gathering")
        assert hard_worker[0] == "on"
        assert hard_worker[1] in {"high", "xhigh"}
    finally:
        server._executor.shutdown(wait=True)



def test_type_reasoning_capability_boundaries_are_per_concrete_model() -> None:
    server = AcpServer()
    try:
        sky = get_policy_pack("tiga").policy("sky").auto_reasoning
        power = get_policy_pack("tiga").policy("power").auto_reasoning

        # Switchable thinking but no effort control: Type may toggle thinking,
        # but must not invent a reasoning-effort value.
        binary = _model(levels=(), can_disable=True, supported=True)
        assert server._resolve_invocation_reasoning(
            binary, Session("b1", "", "M"), sky, _signals(0.2), role="lead", stage="planning"
        ) == ("off", "")
        assert server._resolve_invocation_reasoning(
            binary, Session("b2", "", "M"), power, _signals(0.2), role="lead", stage="planning"
        ) == ("on", "")

        # Fixed-on thinking remains ON even when Sky prefers OFF; effort may
        # still use the concrete model's own vocabulary.
        fixed = _model(levels=("eco", "deep"), can_disable=False, supported=True)
        assert server._resolve_invocation_reasoning(
            fixed, Session("f", "", "M"), sky, _signals(0.2), role="lead", stage="planning"
        ) == ("on", "eco")

        # Explicitly unsupported reasoning is a hard boundary.
        unsupported = _model(levels=(), can_disable=False, supported=False)
        assert server._resolve_invocation_reasoning(
            unsupported, Session("u", "", "M"), power, _signals(1.0), role="lead", stage="planning"
        ) == ("off", "")
    finally:
        server._executor.shutdown(wait=True)


def test_glitter_resolves_effort_independently_for_each_role_model() -> None:
    server = AcpServer()
    try:
        glitter = get_policy_pack("tiga").policy("glitter").auto_reasoning
        signals = _signals(0.92, code=True, tools=True)
        session = Session("g", "", "Type")

        leader = _model(levels=("low", "medium", "high", "xhigh"))
        worker = _model(levels=())
        reviewer = _model(levels=("eco", "normal", "deep"))

        lead_state = server._resolve_invocation_reasoning(
            leader, session, glitter, signals, role="lead", stage="planning"
        )
        worker_state = server._resolve_invocation_reasoning(
            worker, session, glitter, signals, role="worker", stage="gathering"
        )
        review_state = server._resolve_invocation_reasoning(
            reviewer, session, glitter, signals, role="review", stage="review"
        )

        assert lead_state[0] == "on" and lead_state[1] in leader.effort_supported_levels
        assert worker_state[1] == ""  # no effort capability => no parameter
        assert review_state[0] == "on" and review_state[1] in reviewer.effort_supported_levels
    finally:
        server._executor.shutdown(wait=True)

def test_effort_resolver_never_invents_levels():
    model = _model(levels=("low", "high"))
    ctx = ReasoningInvocation(complexity=0.7)
    assert resolve_effort_level(model, strategy="highest", context=ctx, effective_thinking="on") == "high"
    assert resolve_effort_level(model, user_level="medium", user_locked=True, strategy="highest", context=ctx, effective_thinking="on") == ""


def test_explicit_effort_constrains_type_routing_to_models_that_can_honor_it():
    from kiro_byo.config import KiroByoConfig, ProviderSpec
    from kiro_byo.mode_router import route_model

    fast = ModelSpec(
        "p", "P", "flash", "Flash", effort_supported_levels=("low", "high"),
        reasoning_mode="levels", reasoning_supported=True, reasoning_can_disable=True,
        routing_speed=0.99, routing_economy=0.99, routing_quality=0.5, tier="fast",
    )
    strong = ModelSpec(
        "p", "P", "pro", "Pro", effort_supported_levels=("low", "high", "max"),
        reasoning_mode="levels", reasoning_supported=True, reasoning_can_disable=True,
        routing_speed=0.3, routing_economy=0.2, routing_quality=0.98, tier="strong",
    )
    cfg = KiroByoConfig((ProviderSpec("p", "P", "openai-compatible", "x", "", (fast, strong)),), mode_pack="tiga", mode_type="multi")
    decision = route_model(
        cfg, pack="tiga", policy="multi", prompt="hello", history=[],
        has_image=False, tools_expected=False, reasoning_effort="max",
    )
    assert decision is not None
    assert decision.model.model_id == "pro"
    assert all("max" in row.model.effort_supported_levels for row in decision.candidates)


def test_sky_can_only_cross_tier_for_explicit_effort_as_capability_fallback():
    from kiro_byo.config import KiroByoConfig, ProviderSpec
    from kiro_byo.mode_router import route_model

    flash = ModelSpec(
        "p", "P", "flash", "Flash", effort_supported_levels=("low", "high"),
        reasoning_mode="levels", reasoning_supported=True, reasoning_can_disable=True,
        routing_speed=0.99, routing_economy=0.99, routing_quality=0.5, tier="fast",
    )
    pro = ModelSpec(
        "p", "P", "pro", "Pro", effort_supported_levels=("low", "high", "max"),
        reasoning_mode="levels", reasoning_supported=True, reasoning_can_disable=True,
        routing_speed=0.3, routing_economy=0.2, routing_quality=0.98, tier="strong",
    )
    cfg = KiroByoConfig((ProviderSpec("p", "P", "openai-compatible", "x", "", (flash, pro)),), mode_pack="tiga", mode_type="sky")
    decision = route_model(
        cfg, pack="tiga", policy="sky", prompt="hello", history=[],
        has_image=False, tools_expected=False, reasoning_effort="max",
    )
    assert decision is not None
    assert decision.model.model_id == "pro"
    assert any("outside-tier fallback" in reason for reason in decision.reason)


def test_glitter_worker_reasoning_uses_current_tool_subtask_not_original_turn() -> None:
    server = AcpServer()
    try:
        glitter = get_policy_pack("tiga").policy("glitter").auto_reasoning
        model = _model(levels=("low", "medium", "high", "xhigh"))
        session = Session("subtask", "", "M")
        base = SimpleNamespace(
            complexity=0.96,
            code_task=True,
            tools_available=True,
            tools_expected=True,
            has_image=False,
        )

        # A very hard original task delegates one mechanical file read. The
        # worker should not inherit 0.96 complexity simply because the leader's
        # whole turn is hard.
        session.messages.extend([
            {"role": "user", "content": "hard original task"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "r1", "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"README.md"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "r1", "content": "short result"},
        ])
        local = server._relay_subtask_signals(session, base)
        assert local.complexity < 0.50
        assert server._resolve_invocation_reasoning(
            model, session, glitter, local, role="worker", stage="gathering"
        ) == ("off", "")

        # A later relay invocation fans out over several code artefacts with a
        # large result set. That *subtask* is substantive enough to turn worker
        # reasoning on even though workers are normally biased toward gathering.
        session.messages.extend([
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": f"c{i}", "type": "function", "function": {"name": "read", "arguments": f'{{"path":"src/m{i}.py"}}'}}
                    for i in range(4)
                ],
            },
            *[
                {"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 16000}
                for i in range(4)
            ],
        ])
        local = server._relay_subtask_signals(session, base)
        assert local.complexity >= 0.70
        thinking, effort = server._resolve_invocation_reasoning(
            model, session, glitter, local, role="worker", stage="gathering"
        )
        assert thinking == "on"
        assert effort in {"medium", "high", "xhigh"}
    finally:
        server._executor.shutdown(wait=True)
