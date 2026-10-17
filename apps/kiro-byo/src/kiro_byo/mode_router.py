"""Per-turn model routing for Kiro BYO modes.

Kiro BYO exposes a backend-owned virtual mode selection, separate from concrete
provider models. This module keeps the decision deterministic and inspectable:
hard capability gates
run first, then a policy-specific score ranks the remaining configured models.
No provider is hard-coded; optional model routing hints can override the neutral
heuristics used when a provider does not publish price/performance metadata.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from .config import KiroByoConfig, ModelSpec, ProviderSpec
from .policy_packs import ExecutionPolicy, PolicyPackError, default_pack, get_policy_pack, resolve_policy

# Generic tier words only; no vendor/model id is special-cased. They are used as
# a last-resort prior when providers do not publish routing metadata.
_FAST = re.compile(r"(?:^|[-_. ])(flash|haiku|mini|nano|lite|fast|speed|ultraspeed|air)(?:$|[-_. ])", re.I)
_STRONG = re.compile(r"(?:^|[-_. ])(pro|opus|max|ultra|premier|reasoner|reasoning)(?:$|[-_. ])", re.I)
_BALANCED = re.compile(r"(?:^|[-_. ])(plus|sonnet|standard|medium)(?:$|[-_. ])", re.I)
_CODE = re.compile(r"```|\b(?:code|coding|debug|refactor|compile|compiler|test|pytest|typescript|javascript|python|rust|java|c\+\+|sql|git)\b|代码|编程|调试|重构|编译|测试", re.I)
_COMPLEX = re.compile(r"\b(?:analy[sz]e|reason|prove|derive|architecture|design|investigate|compare|audit|research|multi[- ]?step)\b|分析|推理|证明|推导|架构|设计|调研|审计|比较|多步", re.I)
_SIMPLE = re.compile(r"\b(?:translate|rewrite|summari[sz]e|format|rename|spell|define|what is|who is)\b|翻译|改写|总结|格式化|改名|是什么|叫什么", re.I)
_CURRENT_REQUEST = re.compile(r"\[CURRENT USER REQUEST[^\]]*\]", re.I)
_USER_MESSAGE_BLOCK = re.compile(
    r"---\s*USER MESSAGE BEGIN\s*---\s*(.*?)\s*---\s*USER MESSAGE END\s*---",
    re.I | re.S,
)
_WRAPPER_HINT = re.compile(
    r"(?:CURRENT USER REQUEST|USER MESSAGE (?:BEGIN|END)|SYSTEM (?:MESSAGE|PROMPT)|MEMORY|<system|\[SYSTEM)",
    re.I,
)


@dataclass(frozen=True)
class RoutingSignals:
    prompt_chars: int
    history_chars: int
    history_messages: int
    has_image: bool
    tools_expected: bool
    reasoning_effort: str
    code_task: bool
    complexity: float
    estimated_input_tokens: int


@dataclass(frozen=True)
class CandidateScore:
    provider: ProviderSpec
    model: ModelSpec
    score: float
    quality: float
    speed: float
    economy: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RouteDecision:
    pack: str
    pack_label: str
    policy: str
    policy_label: str
    policy_definition: ExecutionPolicy
    provider: ProviderSpec
    model: ModelSpec
    signals: RoutingSignals
    candidates: tuple[CandidateScore, ...]
    reason: tuple[str, ...]
    # Provider/network failover candidates. For tier-locked policies this list
    # intentionally excludes out-of-tier models whenever an in-tier model can
    # run the turn. Out-of-tier models are capability fallbacks, not outage
    # fallbacks.
    execution_fallbacks: tuple[CandidateScore, ...] = ()


@dataclass(frozen=True)
class ReviewTarget:
    provider: ProviderSpec
    model: ModelSpec
    reason: str
    role: str = "review"
    reasoning_intent: str = "inherit"
    pack: str = ""
    pack_label: str = ""
    policy: str = ""
    policy_label: str = ""
    requires_substantive_work: bool = False


@dataclass(frozen=True)
class ExecutionPlan:
    """Resolved mode execution plan for one ACP turn.

    A Type is an optimization policy, not a model tier. Every policy may use
    more than one model when the expected benefit justifies the extra latency
    and cost. ``post_stage`` is deliberately tool-free and side-effect-free.
    """

    decision: RouteDecision
    mode: str
    post_stage: ReviewTarget | None
    rationale: tuple[str, ...]


def _resolved_policy(pack_id: str, policy_id: str) -> tuple[str, str, ExecutionPolicy]:
    """Resolve a pack/policy without teaching the router any pack-specific names."""
    pack_key = str(pack_id or "").strip().lower()
    policy_key = str(policy_id or "").strip().lower()
    if pack_key:
        pack = get_policy_pack(pack_key)
        if pack is None:
            raise PolicyPackError(f"policy pack {pack_key!r} is not installed")
    else:
        pack = default_pack()
    policy = pack.policy(policy_key) if policy_key else pack.policy(pack.default_policy)
    if policy is None:
        policy = pack.policy(pack.default_policy)
    if policy is None:
        raise PolicyPackError(f"policy pack {pack.id!r} has no usable default policy")
    return pack.id, pack.display_name, policy


def normalize_policy(value: str, pack_id: str = "") -> str:
    return _resolved_policy(pack_id, value)[2].id


def policy_label(value: str, pack_id: str = "") -> str:
    return _resolved_policy(pack_id, value)[2].display_name


def pack_label(pack_id: str) -> str:
    return _resolved_policy(pack_id, "")[1]


def pack_short_label(pack_id: str) -> str:
    key = str(pack_id or "").strip().lower()
    if key:
        pack = get_policy_pack(key)
        if pack is None:
            raise PolicyPackError(f"policy pack {key!r} is not installed")
    else:
        pack = default_pack()
    return pack.short_name


def _routing_user_text(value: str) -> tuple[str, bool]:
    """Return the user-authored slice used only for routing heuristics.

    KiroCrew may wrap the actual request in system instructions, memories and
    formatting rules before handing it to ACP. Those wrappers must still be
    sent upstream, but they must not make a casual request look like a coding
    task. Known markers are parsed first. If a future Crew wrapper is detected
    but cannot be parsed, use only a bounded tail for routing rather than
    keyword-scanning the whole system envelope.
    """
    raw = str(value or "")
    if not raw:
        return "", False
    markers = list(_CURRENT_REQUEST.finditer(raw))
    if markers:
        tail = raw[markers[-1].end():]
        blocks = _USER_MESSAGE_BLOCK.findall(tail)
        return ((blocks[-1] if blocks else tail).strip(), True)
    blocks = _USER_MESSAGE_BLOCK.findall(raw)
    if blocks:
        return blocks[-1].strip(), True
    if _WRAPPER_HINT.search(raw):
        # Safe degradation for an unrecognised/new Crew wrapper. The current
        # request is normally at the end; a bounded tail prevents tens of KB of
        # system/memory prose from triggering code/complexity keywords.
        return raw[-2400:].strip(), True
    return raw, False


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in {None, "text"}
        )
    return ""


def build_signals(
    prompt: str,
    history: Sequence[dict],
    *,
    has_image: bool,
    tools_expected: bool,
    reasoning_effort: str = "",
) -> RoutingSignals:
    raw_prompt = str(prompt or "")
    text, _wrapped = _routing_user_text(raw_prompt)
    history_chars = 0
    raw_history_chars = 0
    history_messages = 0
    for msg in history:
        if not isinstance(msg, dict):
            continue
        content = _message_text(msg.get("content"))
        raw_history_chars += len(content)
        # Provider-private/tool/system traffic is not user-visible task history
        # and should not influence task-complexity routing.
        if str(msg.get("role") or "") not in {"user", "assistant"}:
            continue
        if not content:
            continue
        clean, _ = _routing_user_text(content) if msg.get("role") == "user" else (content, False)
        history_chars += len(clean)
        history_messages += 1
    chars = len(text)
    effort = str(reasoning_effort or "").lower()
    complexity = 0.18
    if chars > 1200:
        complexity += 0.12
    if chars > 5000:
        complexity += 0.12
    if history_chars > 12000:
        complexity += 0.10
    if history_chars > 60000:
        complexity += 0.10
    # Long-running work: many turns already spent on the same task.
    if len(history) > 30:
        complexity += 0.06
    if _COMPLEX.search(text):
        complexity += 0.20
    code_match = bool(_CODE.search(text))
    complex_match = bool(_COMPLEX.search(text))
    if code_match:
        complexity += 0.12
    # A request that is both explicitly analytical/multi-step and code-oriented
    # is materially harder even when no tools are available. This replaces the
    # old false signal where merely *having* tools inflated every request.
    if code_match and complex_match:
        complexity += 0.08
    # Tool *availability* is not evidence that this request is difficult.
    # It remains a capability gate below, but does not inflate complexity.
    if has_image:
        complexity += 0.08
    if effort in {"high", "xhigh", "max"}:
        complexity += 0.22
    elif effort in {"medium"}:
        complexity += 0.10
    if _SIMPLE.search(text) and chars < 1000:
        complexity -= 0.12
    complexity = max(0.0, min(1.0, complexity))
    # Capability/context gating must still reflect the bytes actually sent to
    # the provider. Only *difficulty heuristics* use the cleaned user view.
    estimated = max(1, math.ceil((len(raw_prompt) + raw_history_chars) / 3.6))
    return RoutingSignals(
        prompt_chars=chars,
        history_chars=history_chars,
        history_messages=history_messages,
        has_image=bool(has_image),
        tools_expected=bool(tools_expected),
        reasoning_effort=effort,
        code_task=code_match,
        complexity=complexity,
        estimated_input_tokens=estimated,
    )


def _hint(model: ModelSpec, name: str, default: float) -> float:
    value = getattr(model, name, None)
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    return default


def _model_priors(model: ModelSpec) -> tuple[float, float, float, list[str]]:
    label = f"{model.name} {model.model_id}"
    quality, speed, economy = 0.58, 0.58, 0.58
    reasons: list[str] = []
    if _FAST.search(label):
        speed += 0.25; economy += 0.24; quality -= 0.08
        reasons.append("fast-tier prior")
    if _STRONG.search(label):
        quality += 0.25; speed -= 0.10; economy -= 0.10
        reasons.append("quality-tier prior")
    if _BALANCED.search(label):
        quality += 0.08; speed += 0.04; economy += 0.03
        reasons.append("balanced-tier prior")
    if model.reasoning_supported is True:
        quality += 0.08
        reasons.append("reasoning capable")
    if len(model.effort_supported_levels) >= 3:
        quality += 0.05
    if model.context_window:
        if model.context_window >= 1_000_000:
            quality += 0.04
        elif model.context_window < 64_000:
            quality -= 0.04
    return (
        _hint(model, "routing_quality", max(0.0, min(1.0, quality))),
        _hint(model, "routing_speed", max(0.0, min(1.0, speed))),
        _hint(model, "routing_economy", max(0.0, min(1.0, economy))),
        reasons,
    )


def _eligible(model: ModelSpec, signals: RoutingSignals) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    modalities = {str(x).lower() for x in model.input_modalities}
    if signals.has_image and modalities and "image" not in modalities:
        return False, ["image input unsupported"]
    if signals.tools_expected and model.tool_use is False:
        return False, ["tool use unsupported"]
    if model.context_window and signals.estimated_input_tokens > int(model.context_window * 0.92):
        return False, ["context window too small"]
    # A user-selected reasoning effort is a hard session constraint, just like
    # a user-selected deep-thinking ON/OFF state is a hard model constraint.
    # Type routing may choose any model only while effort is automatic. Once the
    # user pins a concrete level, silently switching to a model that cannot
    # express that level would violate the user's setting.
    if signals.reasoning_effort:
        requested = str(signals.reasoning_effort).lower()
        supported = {str(x).lower() for x in model.effort_supported_levels}
        fixed = str(model.reasoning_fixed_level or "").lower()
        if requested not in supported and requested != fixed:
            return False, ["requested effort unsupported"]
        reasons.append("requested effort supported")
    if signals.has_image and "image" in modalities:
        reasons.append("image capable")
    if signals.tools_expected and model.tool_use is True:
        reasons.append("tool capable")
    return True, reasons


def model_tier(model: ModelSpec) -> str:
    """``fast`` / ``balanced`` / ``strong`` for form pools and picker labels.

    Order of authority: the user's manual ``tier``; explicit routing hints
    (speed/economy vs quality); generic tier words in the name. No vendor or
    model id is special-cased.
    """
    manual = str(getattr(model, "tier", "") or "").strip().lower()
    if manual in {"fast", "balanced", "strong"}:
        return manual
    q, s, e = model.routing_quality, model.routing_speed, model.routing_economy
    hints = [value for value in (q, s, e) if isinstance(value, (int, float))]
    if hints:
        quality = float(q) if isinstance(q, (int, float)) else 0.58
        speed = float(s) if isinstance(s, (int, float)) else 0.58
        economy = float(e) if isinstance(e, (int, float)) else 0.58
        fast_score = (speed + economy) / 2
        if quality >= 0.75 and quality >= fast_score:
            return "strong"
        if fast_score >= 0.7 and quality < 0.75:
            return "fast"
        return "balanced"
    label = f"{model.name} {model.model_id}"
    if _FAST.search(label):
        return "fast"
    if _STRONG.search(label):
        return "strong"
    return "balanced"


def _in_pool(model: ModelSpec, pool: str) -> bool:
    if pool == "fast":
        return model_tier(model) == "fast"
    if pool == "strong":
        return model_tier(model) == "strong"
    return True


def _weights(policy: ExecutionPolicy, complexity: float) -> tuple[float, float, float]:
    q = float(policy.routing.quality)
    s = float(policy.routing.speed)
    e = float(policy.routing.economy)
    shift = policy.routing.complexity_shift
    if shift.mode == "adaptive":
        delta = (complexity - 0.5) * shift.strength
        q += delta
        s -= delta * 0.55
        e -= delta * 0.45
    elif shift.mode == "threshold" and complexity > shift.threshold:
        q += shift.quality_add
        # Keep the original speed/economy ratio while buying more quality.
        rest = max(1e-9, s + e)
        s -= shift.quality_add * (s / rest)
        e -= shift.quality_add * (e / rest)
    q, s, e = max(0.0, q), max(0.0, s), max(0.0, e)
    total = q + s + e
    if total <= 0:
        return (1.0, 0.0, 0.0)
    return q / total, s / total, e / total

def route_model(
    config: KiroByoConfig,
    *,
    policy: str | None = None,
    pack: str | None = None,
    prompt: str,
    history: Sequence[dict],
    has_image: bool,
    tools_expected: bool,
    reasoning_effort: str = "",
) -> RouteDecision | None:
    signals = build_signals(
        prompt, history, has_image=has_image, tools_expected=tools_expected,
        reasoning_effort=reasoning_effort,
    )
    pack_id, pack_name, policy_def = _resolved_policy(pack or config.mode_pack, policy or config.mode_type)
    q_w, s_w, e_w = _weights(policy_def, signals.complexity)
    ranked: list[CandidateScore] = []
    for provider in config.providers:
        for model in provider.models:
            ok, reasons = _eligible(model, signals)
            if not ok:
                continue
            quality, speed, economy, prior_reasons = _model_priors(model)
            reasons.extend(prior_reasons)
            fit = 0.0
            if signals.reasoning_effort in {"high", "xhigh", "max"}:
                if model.reasoning_supported is True:
                    fit += 0.10
                    reasons.append("requested deep reasoning")
                elif model.reasoning_supported is False:
                    fit -= 0.22
            if signals.reasoning_effort:
                # Eligibility above already guarantees exact vocabulary support.
                fit += 0.04
            if signals.code_task and model.tool_use is True:
                fit += 0.03
            # Form-specific capability preferences (data-driven, no model names).
            # Long work needs room: favour large windows in proportion to how
            # much of the conversation is already history.
            context_bias = float(getattr(policy_def.routing, "context_bias", 0.0) or 0.0)
            if context_bias and model.context_window:
                window_score = min(1.0, math.log10(max(model.context_window, 1)) / 6.0)
                fit += context_bias * window_score
                if window_score >= 0.9:
                    reasons.append("long-context fit")
            tool_bias = float(getattr(policy_def.routing, "tool_bias", 0.0) or 0.0)
            if tool_bias and signals.tools_expected:
                if model.tool_use is True:
                    fit += tool_bias
                    reasons.append("reliable tool use")
                elif model.tool_use is None:
                    fit -= tool_bias * 0.5
            # Unknown capability is allowed but never preferred over a verified fit.
            if signals.has_image and not model.input_modalities:
                fit -= 0.08
                reasons.append("image capability unknown")
            if signals.tools_expected and model.tool_use is None:
                fit -= 0.05
                reasons.append("tool capability unknown")
            score = q_w * quality + s_w * speed + e_w * economy + fit
            ranked.append(CandidateScore(provider, model, score, quality, speed, economy, tuple(reasons)))
    if not ranked:
        return None
    ranked.sort(key=lambda row: (-row.score, row.provider.id, row.model.model_id))
    # Form pool: Sky stays in the fast tier, Power in the strong tier. In-tier
    # models always come first (for the winner AND for provider-error
    # fallbacks); an out-of-tier model is used only when no in-tier model can
    # run this turn (e.g. image input, context size).
    pool = str(getattr(policy_def.routing, "tier_pool", "any") or "any")
    selection_pool = str(getattr(policy_def.routing, "primary_tier_pool", "") or pool)
    outside_pool = False
    execution_ranked = list(ranked)
    if selection_pool != "any":
        in_pool = [row for row in ranked if _in_pool(row.model, selection_pool)]
        out_pool = [row for row in ranked if not _in_pool(row.model, selection_pool)]
        if in_pool:
            # Keep out-of-tier rows in ``candidates`` for independent review and
            # Glitter teammate discovery, but never use them for a transient
            # ProviderError fallback.
            ranked = in_pool + out_pool
            execution_ranked = list(in_pool)
        else:
            outside_pool = True
            if selection_pool == "fast":
                # Cheapest/fastest capable model as the closest substitute.
                ranked = sorted(out_pool, key=lambda r: (-(0.55 * r.speed + 0.45 * r.economy), r.provider.id, r.model.model_id))
            else:
                ranked = sorted(out_pool, key=lambda r: (-r.quality, -r.score, r.provider.id, r.model.model_id))
            execution_ranked = list(ranked)
    winner = ranked[0]
    reason = [
        f"{pack_name} / {policy_def.display_name} policy",
        f"task complexity {signals.complexity:.2f}",
        f"weights quality={q_w:.2f} speed={s_w:.2f} economy={e_w:.2f}",
    ]
    if selection_pool != "any":
        reason.append(
            f"no {selection_pool}-tier model can run this turn; outside-tier fallback"
            if outside_pool else f"{selection_pool}-tier pool"
        )
    reason.extend(winner.reasons[:4])
    return RouteDecision(
        pack_id, pack_name, policy_def.id, policy_def.display_name, policy_def,
        winner.provider, winner.model, signals, tuple(ranked), tuple(reason),
        tuple(execution_ranked),
    )


def _independent_candidate(
    decision: RouteDecision, *, objective: str, reviewer: str = "any"
) -> CandidateScore | None:
    alternatives = [
        row for row in decision.candidates
        if row.provider.id != decision.provider.id or row.model.model_id != decision.model.model_id
    ]
    primary = next(
        (
            row for row in decision.candidates
            if row.provider.id == decision.provider.id and row.model.model_id == decision.model.model_id
        ),
        decision.candidates[0] if decision.candidates else None,
    )
    if reviewer == "not_weaker" and primary is not None:
        # A weaker model must never check (and possibly rewrite) a stronger
        # model's answer. Without a peer, the primary reviews its own draft.
        alternatives = [row for row in alternatives if row.quality >= primary.quality]
        if not alternatives:
            return primary
    pool_name = str(getattr(decision.policy_definition.routing, "tier_pool", "any") or "any")
    if pool_name != "any":
        in_pool = [row for row in alternatives if _in_pool(row.model, pool_name)]
        if in_pool:
            alternatives = in_pool
        elif primary is not None:
            # Tier-locked review never borrows another out-of-tier model. If a
            # Sky/Power primary itself is a capability fallback, self-review is
            # the only allowed outside-tier verification.
            return primary
    if primary is not None:
        # Prefer a reviewer from a different provider: independent training
        # and serving stacks catch errors a sibling model would share.
        foreign = [row for row in alternatives if row.provider.id != primary.provider.id]
        if foreign:
            alternatives = foreign
    pool = alternatives or list(decision.candidates[:1])
    if not pool:
        return None
    if objective == "economy":
        return max(pool, key=lambda x: (0.44*x.speed + 0.38*x.economy + 0.18*x.quality, x.score))
    if objective == "value":
        return max(pool, key=lambda x: (0.52*x.quality + 0.24*x.speed + 0.24*x.economy, x.score))
    return max(pool, key=lambda x: (x.quality, x.score, x.model.context_window or 0))


def _secondary_triggered(policy: ExecutionPolicy, signals: RoutingSignals) -> bool:
    secondary = policy.secondary
    if secondary is None:
        return False
    trigger = secondary.trigger
    if trigger.always:
        return True
    checks = []
    if trigger.complexity_gte is not None:
        checks.append(signals.complexity >= trigger.complexity_gte)
    if trigger.code:
        checks.append(signals.code_task)
    if trigger.tools:
        checks.append(signals.tools_expected)
    if trigger.code_and_tools:
        checks.append(signals.code_task and signals.tools_expected)
    if trigger.reasoning_any:
        checks.append(signals.reasoning_effort in set(trigger.reasoning_any))
    return any(checks) if checks else False


def build_execution_plan(decision: RouteDecision) -> ExecutionPlan:
    """Build a generic execution plan from the active pack's data schema.

    The planner has no knowledge of Tiga, Sky, Multi, Power, Glitter, or any
    future pack names. The primary model owns the normal agent/tool loop; an
    optional secondary stage is bounded, tool-free, and side-effect-free.
    """
    policy = decision.policy_definition
    secondary = policy.secondary
    target: ReviewTarget | None = None
    rationale: list[str] = [f"{decision.pack_label} / {decision.policy_label} execution policy"]

    if secondary is None:
        return ExecutionPlan(decision, "single_pass", None, tuple(rationale))

    if _secondary_triggered(policy, decision.signals):
        row = _independent_candidate(
            decision,
            objective=secondary.objective,
            reviewer=getattr(secondary, "reviewer", "any"),
        )
        if row is not None:
            same = row.provider.id == decision.provider.id and row.model.model_id == decision.model.model_id
            reason = secondary.self_reason if same else secondary.reason
            target = ReviewTarget(
                row.provider, row.model, reason, secondary.role, secondary.reasoning_intent,
                decision.pack, decision.pack_label, decision.policy, decision.policy_label,
            )
            rationale.append(secondary.rationale)

    mode = secondary.multi_mode if target is not None else secondary.single_mode
    return ExecutionPlan(decision=decision, mode=mode, post_stage=target, rationale=tuple(rationale))


def glitter_review_target(decision: RouteDecision) -> ReviewTarget | None:
    """Legacy helper: return the secondary target only when its generic role is review."""
    target = build_execution_plan(decision).post_stage
    return target if target is not None and target.role == "review" else None

