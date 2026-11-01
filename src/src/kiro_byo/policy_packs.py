"""Hot-pluggable execution policy packs for Kiro BYO modes.

A policy pack is data, not code.  The core router understands only the generic
schema in this module; names such as ``Sky Type`` or ``Glitter Type`` live in
pack data.  Built-in packs are shipped under ``kiro_byo/policy_packs/*.json``.
Users can add or replace packs at ``<KIRO_BYO_HOME>/policy_packs/*.json`` and
Kiro BYO re-reads that directory on every config/UI load, so no process restart
is required.

Policy packs never define provider/model capabilities.  They express routing
and execution *intent* (quality/speed/economy weights, secondary-stage gates,
and an abstract reasoning intent).  Concrete reasoning controls remain owned by
each model/provider capability adapter.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .paths import app_home

SCHEMA_VERSION = 4
_SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4})
_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")
_OBJECTIVES = frozenset({"economy", "value", "quality"})
_ROLES = frozenset({"verify", "review"})
_REASONING_INTENTS = frozenset({"inherit", "economy", "balanced", "high", "maximum"})
_THINKING_AUTO = frozenset({"inherit", "prefer_off", "adaptive", "prefer_on", "adaptive_per_call"})
_EFFORT_AUTO = frozenset({"inherit", "lowest", "adaptive", "highest", "adaptive_per_call"})
_REVIEWERS = frozenset({"any", "not_weaker"})
_TIER_FALLBACKS = frozenset({"capability", "strict"})
_TIERS = frozenset({"any", "fast", "balanced", "strong"})
_ORCHESTRATION_OBJECTIVES = frozenset({"latency", "value", "quality", "capability"})
_TOPOLOGIES = frozenset({"single", "adaptive", "leader-worker"})
_WORKER_MODES = frozenset({"none", "adaptive", "always"})


class PolicyPackError(ValueError):
    """Raised when a policy pack is malformed."""


@dataclass(frozen=True)
class ScoreWeights:
    """Generic, data-driven score weights used by fallback/review stages."""

    quality: float = 0.0
    speed: float = 0.0
    economy: float = 0.0
    reliability: float = 0.0


@dataclass(frozen=True)
class ComplexityShift:
    mode: str = "none"
    strength: float = 0.0
    threshold: float = 1.0
    quality_add: float = 0.0
    # Adaptive shifts buy quality by taking weight from speed/economy. The split
    # belongs to policy data rather than the Router Core.
    center: float = 0.5
    speed_share: float = 0.55
    economy_share: float = 0.45


@dataclass(frozen=True)
class AdaptiveRoutingProfile:
    """How runtime evidence is blended into one policy's static priors.

    Defaults preserve schema-v1/v2 behaviour. Built-in schema-v3 packs declare
    these values explicitly so Type character is policy data, not Python code.
    Evidence-shaping values live here too: the router should not secretly own
    a fixed half-life, confidence threshold or latency/reliability blend.
    """

    enabled: bool = True
    history_limit: int = 500
    half_life_days: float = 3.0
    reliability_prior: float = 0.94
    reliability_prior_weight: float = 6.0
    sample_full: int = 24
    task_sample_full: int = 16
    confidence_medium: int = 8
    confidence_high: int = 30
    latency_ttft_weight: float = 0.32
    latency_total_weight: float = 0.68
    runtime_speed_floor: float = 0.30
    runtime_speed_span: float = 0.60
    runtime_speed_single: float = 0.58
    speed_alpha_base: float = 0.22
    speed_alpha_speed_weight: float = 0.85
    speed_alpha_min: float = 0.20
    speed_alpha_max: float = 0.68
    speed_blend_ceiling: float = 0.75
    reliability_base: float = 0.12
    reliability_quality_weight: float = 0.26
    reliability_min: float = 0.10
    reliability_max: float = 0.34
    reliability_task_weight: float = 0.75
    reliability_adjustment_floor: float = -0.12
    reliability_adjustment_ceiling: float = 0.08


@dataclass(frozen=True)
class FitBiasProfile:
    """Small capability/intent tie-breakers; never hard capability gates."""

    deep_reasoning_bonus: float = 0.10
    reasoning_unsupported_penalty: float = 0.22
    effort_match_bonus: float = 0.04
    code_tool_bonus: float = 0.03
    code_quality_bonus: float = 0.0
    image_unknown_penalty: float = 0.08
    tool_unknown_penalty: float = 0.05
    context_headroom_bonus: float = 0.0
    thinking_on_align_bonus: float = 0.035
    thinking_off_align_bonus: float = 0.025
    thinking_on_conflict_penalty: float = 0.050
    thinking_off_conflict_penalty: float = 0.035


@dataclass(frozen=True)
class RoutingProfile:
    quality: float
    speed: float
    economy: float
    complexity_shift: ComplexityShift = ComplexityShift()
    # Task-aware capability preferences, added on top of the weighted score.
    # ``context_bias`` favours large context windows (long tasks);
    # ``tool_bias`` favours models that declare reliable tool use.
    context_bias: float = 0.0
    tool_bias: float = 0.0
    # Which model tier the policy normally uses. ``any`` leaves tier choice to
    # scoring; a concrete tier constrains the primary route and (by default)
    # secondary review.
    tier_pool: str = "any"
    # Optional stricter pool for the primary/leader only. Candidate rows remain
    # available to collaboration/review logic.
    primary_tier_pool: str = ""
    # ``capability`` allows an out-of-tier model only when no in-tier model is
    # capable of the turn. ``strict`` fails closed instead of silently changing
    # the character the user selected.
    tier_fallback: str = "capability"
    adaptive: AdaptiveRoutingProfile = AdaptiveRoutingProfile()
    fit: FitBiasProfile = FitBiasProfile()
    fallback_scoring: ScoreWeights | None = None


@dataclass(frozen=True)
class OrchestrationProfile:
    """Data-only policy controlling how many execution roles a turn may use."""

    objective: str = "value"
    topology: str = "single"
    max_agents: int = 1
    max_parallel: int = 1
    max_depth: int = 1
    worker_mode: str = "none"
    worker_tier_pool: str = "fast"
    worker_reasoning_intent: str = "economy"
    worker_breadth_gte: float = 1.0
    worker_parallelizability_gte: float = 1.0
    worker_tool_intensity_gte: float = 1.0
    worker_write_intensity_lte: float = 0.0
    single_writer: bool = True


@dataclass(frozen=True)
class AutoReasoningProfile:
    """How a Type resolves user AUTO controls.

    These are abstract, provider-neutral strategies. Concrete ON/OFF and effort
    vocabulary remain model capabilities and are resolved per invocation.
    """

    thinking: str = "inherit"
    effort: str = "inherit"


@dataclass(frozen=True)
class SecondaryTrigger:
    always: bool = False
    complexity_gte: float | None = None
    breadth_gte: float | None = None
    risk_gte: float | None = None
    verification_value_gte: float | None = None
    write_intensity_gte: float | None = None
    code: bool = False
    tools: bool = False
    code_and_tools: bool = False
    reasoning_any: tuple[str, ...] = ()


@dataclass(frozen=True)
class SecondaryProfile:
    role: str
    objective: str
    trigger: SecondaryTrigger
    reason: str
    self_reason: str
    rationale: str
    single_mode: str
    multi_mode: str
    reasoning_intent: str = "inherit"
    # ``any``: the best alternative by objective may check the draft.
    # ``not_weaker``: only a model whose quality is at least the primary's may
    # check it; otherwise the primary model reviews its own draft. A weaker
    # model must never rewrite a stronger model's answer.
    reviewer: str = "any"
    # Optional data-driven reviewer score. Schema-v1/v2 packs fall back to the
    # legacy objective presets; schema-v3 built-ins declare this explicitly.
    scoring: ScoreWeights | None = None
    prefer_different_provider: bool = True
    # Verification can be deferred until the primary actually did meaningful
    # work (tool use or a substantial answer). Final-review policies such as
    # Glitter can leave this false to remain mandatory.
    requires_substantive_work: bool = False


@dataclass(frozen=True)
class UserPolicySemantics:
    """User-facing meaning of one Type. Never consumed by the Turn Engine."""

    display_name: str
    localized_name: str
    description: str
    short_description: str


@dataclass(frozen=True)
class ExecutionPolicy:
    """Machine execution policy, intentionally free of product-facing names."""

    routing: RoutingProfile
    orchestration: OrchestrationProfile = OrchestrationProfile()
    secondary: SecondaryProfile | None = None
    # Provider-neutral AUTO policy for deep-thinking enablement and effort.
    # Explicit user choices remain hard locks and model capabilities remain the
    # final authority.
    auto_reasoning: AutoReasoningProfile = AutoReasoningProfile()
    # Legacy coarse intent retained for secondary-trigger compatibility and
    # older custom policy packs. New packs should use ``reasoning`` below.
    primary_reasoning: str = "inherit"
    # Keep the routed model for the whole session once chosen (long tasks):
    # switching models mid-task loses provider caches and continuity.
    sticky: bool = False
    # ``none``: one model runs the whole turn. ``relay``: the lead model plans,
    # changes and answers, while a bounded teammate may continue pure
    # information-gathering rounds. The teammate tier is pack data, not a
    # hard-coded provider/model rule.
    collaboration: str = "none"
    collaboration_tier_pool: str = "fast"
    # Optional worker/router profile for relay collaboration. When absent, old
    # packs retain the compatibility formula. Built-in v3 packs configure it.
    collaboration_routing: RoutingProfile | None = None
    collaboration_provider_affinity: float = 0.025


@dataclass(frozen=True)
class PolicyDefinition:
    """Stable Type identity joining conversational semantics to execution data.

    The runtime compiles ``execution`` into an ExecutionIntent and never needs
    the product-facing names below. Compatibility properties keep older callers
    and custom packs source-compatible while the boundary migrates.
    """

    id: str
    semantics: UserPolicySemantics
    execution: ExecutionPolicy

    @property
    def display_name(self) -> str:
        return self.semantics.display_name

    @property
    def localized_name(self) -> str:
        return self.semantics.localized_name

    @property
    def description(self) -> str:
        return self.semantics.description

    @property
    def short_description(self) -> str:
        return self.semantics.short_description

    # Transitional execution accessors. New runtime code should consume
    # ``execution`` or a compiled ExecutionIntent instead.
    @property
    def routing(self) -> RoutingProfile:
        return self.execution.routing

    @property
    def orchestration(self) -> OrchestrationProfile:
        return self.execution.orchestration

    @property
    def secondary(self) -> SecondaryProfile | None:
        return self.execution.secondary

    @property
    def auto_reasoning(self) -> AutoReasoningProfile:
        return self.execution.auto_reasoning

    @property
    def primary_reasoning(self) -> str:
        return self.execution.primary_reasoning

    @property
    def sticky(self) -> bool:
        return self.execution.sticky

    @property
    def collaboration(self) -> str:
        return self.execution.collaboration

    @property
    def collaboration_tier_pool(self) -> str:
        return self.execution.collaboration_tier_pool


@dataclass(frozen=True)
class PolicyPack:
    id: str
    display_name: str
    short_name: str
    localized_name: str
    description: str
    default_policy: str
    policies: tuple[PolicyDefinition, ...]
    is_default: bool = False
    # Preview-only packs are published to the UI, but never executable.
    enabled: bool = True
    source: str = "builtin"
    source_path: str = ""
    # S4-02: optional versioned, data-only Mode contract. Legacy JSON stays valid.
    plugin_version: str = "0.0.0"
    registry_api: int = 1

    def policy(self, policy_id: str) -> PolicyDefinition | None:
        key = str(policy_id or "").strip().lower()
        for row in self.policies:
            if row.id == key:
                return row
        return None


def policy_pack_dir() -> Path:
    """Directory for user hot-plug policy packs."""
    return app_home() / "policy_packs"


def _require_id(value: Any, field: str) -> str:
    text = str(value or "").strip().lower()
    if not text or len(text) > 64 or any(ch not in _ID_CHARS for ch in text):
        raise PolicyPackError(f"{field} must be a lowercase id using a-z, 0-9, '-' or '_'")
    return text


def _number(value: Any, field: str, *, lo: float = 0.0, hi: float = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyPackError(f"{field} must be a number")
    out = float(value)
    if out < lo or out > hi:
        raise PolicyPackError(f"{field} must be between {lo:g} and {hi:g}")
    return out


def _text(value: Any, field: str, *, fallback: str = "") -> str:
    if value is None:
        return fallback
    if not isinstance(value, str):
        raise PolicyPackError(f"{field} must be a string")
    return value.strip()


def _integer(value: Any, field: str, *, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyPackError(f"{field} must be an integer")
    if value < lo or value > hi:
        raise PolicyPackError(f"{field} must be between {lo} and {hi}")
    return int(value)


def _score_weights(raw: Any, field: str) -> ScoreWeights | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PolicyPackError(f"{field} must be an object")
    weights = ScoreWeights(
        quality=_number(raw.get("quality", 0.0), f"{field}.quality"),
        speed=_number(raw.get("speed", 0.0), f"{field}.speed"),
        economy=_number(raw.get("economy", 0.0), f"{field}.economy"),
        reliability=_number(raw.get("reliability", 0.0), f"{field}.reliability"),
    )
    if weights.quality + weights.speed + weights.economy + weights.reliability <= 0:
        raise PolicyPackError(f"{field} weights must not all be zero")
    return weights


def _adaptive_profile(raw: Any, field: str) -> AdaptiveRoutingProfile:
    if raw is None:
        return AdaptiveRoutingProfile()
    if not isinstance(raw, dict):
        raise PolicyPackError(f"{field} must be an object")
    return AdaptiveRoutingProfile(
        enabled=bool(raw.get("enabled", True)),
        history_limit=_integer(raw.get("history_limit", 500), f"{field}.history_limit", lo=50, hi=5000),
        half_life_days=_number(raw.get("half_life_days", 3.0), f"{field}.half_life_days", lo=0.1, hi=90.0),
        reliability_prior=_number(raw.get("reliability_prior", 0.94), f"{field}.reliability_prior"),
        reliability_prior_weight=_number(raw.get("reliability_prior_weight", 6.0), f"{field}.reliability_prior_weight", hi=100.0),
        sample_full=_integer(raw.get("sample_full", 24), f"{field}.sample_full", lo=1, hi=10000),
        task_sample_full=_integer(raw.get("task_sample_full", 16), f"{field}.task_sample_full", lo=1, hi=10000),
        confidence_medium=_integer(raw.get("confidence_medium", 8), f"{field}.confidence_medium", lo=1, hi=10000),
        confidence_high=_integer(raw.get("confidence_high", 30), f"{field}.confidence_high", lo=1, hi=10000),
        latency_ttft_weight=_number(raw.get("latency_ttft_weight", 0.32), f"{field}.latency_ttft_weight"),
        latency_total_weight=_number(raw.get("latency_total_weight", 0.68), f"{field}.latency_total_weight"),
        runtime_speed_floor=_number(raw.get("runtime_speed_floor", 0.30), f"{field}.runtime_speed_floor"),
        runtime_speed_span=_number(raw.get("runtime_speed_span", 0.60), f"{field}.runtime_speed_span"),
        runtime_speed_single=_number(raw.get("runtime_speed_single", 0.58), f"{field}.runtime_speed_single"),
        speed_alpha_base=_number(raw.get("speed_alpha_base", 0.22), f"{field}.speed_alpha_base"),
        speed_alpha_speed_weight=_number(raw.get("speed_alpha_speed_weight", 0.85), f"{field}.speed_alpha_speed_weight", hi=2.0),
        speed_alpha_min=_number(raw.get("speed_alpha_min", 0.20), f"{field}.speed_alpha_min"),
        speed_alpha_max=_number(raw.get("speed_alpha_max", 0.68), f"{field}.speed_alpha_max"),
        speed_blend_ceiling=_number(raw.get("speed_blend_ceiling", 0.75), f"{field}.speed_blend_ceiling"),
        reliability_base=_number(raw.get("reliability_base", 0.12), f"{field}.reliability_base"),
        reliability_quality_weight=_number(raw.get("reliability_quality_weight", 0.26), f"{field}.reliability_quality_weight", hi=2.0),
        reliability_min=_number(raw.get("reliability_min", 0.10), f"{field}.reliability_min"),
        reliability_max=_number(raw.get("reliability_max", 0.34), f"{field}.reliability_max"),
        reliability_task_weight=_number(raw.get("reliability_task_weight", 0.75), f"{field}.reliability_task_weight"),
        reliability_adjustment_floor=_number(raw.get("reliability_adjustment_floor", -0.12), f"{field}.reliability_adjustment_floor", lo=-1.0, hi=0.0),
        reliability_adjustment_ceiling=_number(raw.get("reliability_adjustment_ceiling", 0.08), f"{field}.reliability_adjustment_ceiling", lo=0.0, hi=1.0),
    )


def _fit_profile(raw: Any, field: str) -> FitBiasProfile:
    if raw is None:
        return FitBiasProfile()
    if not isinstance(raw, dict):
        raise PolicyPackError(f"{field} must be an object")
    def n(name: str, default: float) -> float:
        return _number(raw.get(name, default), f"{field}.{name}", hi=1.0)
    return FitBiasProfile(
        deep_reasoning_bonus=n("deep_reasoning_bonus", 0.10),
        reasoning_unsupported_penalty=n("reasoning_unsupported_penalty", 0.22),
        effort_match_bonus=n("effort_match_bonus", 0.04),
        code_tool_bonus=n("code_tool_bonus", 0.03),
        code_quality_bonus=n("code_quality_bonus", 0.0),
        image_unknown_penalty=n("image_unknown_penalty", 0.08),
        tool_unknown_penalty=n("tool_unknown_penalty", 0.05),
        context_headroom_bonus=n("context_headroom_bonus", 0.0),
        thinking_on_align_bonus=n("thinking_on_align_bonus", 0.035),
        thinking_off_align_bonus=n("thinking_off_align_bonus", 0.025),
        thinking_on_conflict_penalty=n("thinking_on_conflict_penalty", 0.050),
        thinking_off_conflict_penalty=n("thinking_off_conflict_penalty", 0.035),
    )


def _trigger(raw: Any, prefix: str) -> SecondaryTrigger:
    if raw is None:
        return SecondaryTrigger()
    if not isinstance(raw, dict):
        raise PolicyPackError(f"{prefix}.trigger must be an object")
    complexity = raw.get("complexity_gte")
    if complexity is not None:
        complexity = _number(complexity, f"{prefix}.trigger.complexity_gte")
    breadth = raw.get("breadth_gte")
    if breadth is not None:
        breadth = _number(breadth, f"{prefix}.trigger.breadth_gte")
    risk = raw.get("risk_gte")
    if risk is not None:
        risk = _number(risk, f"{prefix}.trigger.risk_gte")
    verification = raw.get("verification_value_gte")
    if verification is not None:
        verification = _number(verification, f"{prefix}.trigger.verification_value_gte")
    write_intensity = raw.get("write_intensity_gte")
    if write_intensity is not None:
        write_intensity = _number(write_intensity, f"{prefix}.trigger.write_intensity_gte")
    reasoning_raw = raw.get("reasoning_any", [])
    if not isinstance(reasoning_raw, list) or any(not isinstance(x, str) for x in reasoning_raw):
        raise PolicyPackError(f"{prefix}.trigger.reasoning_any must be an array of strings")
    reasoning = tuple(dict.fromkeys(str(x).strip().lower() for x in reasoning_raw if str(x).strip()))
    return SecondaryTrigger(
        always=bool(raw.get("always", False)),
        complexity_gte=complexity,
        breadth_gte=breadth,
        risk_gte=risk,
        verification_value_gte=verification,
        write_intensity_gte=write_intensity,
        code=bool(raw.get("code", False)),
        tools=bool(raw.get("tools", False)),
        code_and_tools=bool(raw.get("code_and_tools", False)),
        reasoning_any=reasoning,
    )


def _routing(raw: Any, prefix: str) -> RoutingProfile:
    if not isinstance(raw, dict):
        raise PolicyPackError(f"{prefix}.routing must be an object")
    quality = _number(raw.get("quality"), f"{prefix}.routing.quality")
    speed = _number(raw.get("speed"), f"{prefix}.routing.speed")
    economy = _number(raw.get("economy"), f"{prefix}.routing.economy")
    total = quality + speed + economy
    if total <= 0:
        raise PolicyPackError(f"{prefix}.routing weights must not all be zero")
    shift_raw = raw.get("complexity_shift")
    shift = ComplexityShift()
    if shift_raw is not None:
        if not isinstance(shift_raw, dict):
            raise PolicyPackError(f"{prefix}.routing.complexity_shift must be an object")
        mode = str(shift_raw.get("mode") or "none").strip().lower()
        if mode not in {"none", "adaptive", "threshold"}:
            raise PolicyPackError(f"{prefix}.routing.complexity_shift.mode is invalid")
        strength = _number(shift_raw.get("strength", 0.0), f"{prefix}.routing.complexity_shift.strength")
        threshold = _number(shift_raw.get("threshold", 1.0), f"{prefix}.routing.complexity_shift.threshold")
        quality_add = _number(shift_raw.get("quality_add", 0.0), f"{prefix}.routing.complexity_shift.quality_add")
        center = _number(shift_raw.get("center", 0.5), f"{prefix}.routing.complexity_shift.center")
        speed_share = _number(shift_raw.get("speed_share", 0.55), f"{prefix}.routing.complexity_shift.speed_share")
        economy_share = _number(shift_raw.get("economy_share", 0.45), f"{prefix}.routing.complexity_shift.economy_share")
        share_total = speed_share + economy_share
        if share_total <= 0:
            raise PolicyPackError(f"{prefix}.routing.complexity_shift speed/economy shares must not both be zero")
        speed_share /= share_total
        economy_share /= share_total
        shift = ComplexityShift(mode, strength, threshold, quality_add, center, speed_share, economy_share)
    context_bias = _number(raw.get("context_bias", 0.0), f"{prefix}.routing.context_bias")
    tool_bias = _number(raw.get("tool_bias", 0.0), f"{prefix}.routing.tool_bias")
    tier_pool = str(raw.get("tier_pool") or "any").strip().lower()
    if tier_pool not in _TIERS:
        raise PolicyPackError(f"{prefix}.routing.tier_pool must be any, fast, balanced, or strong")
    primary_tier_pool = str(raw.get("primary_tier_pool") or "").strip().lower()
    if primary_tier_pool not in {"", *_TIERS}:
        raise PolicyPackError(f"{prefix}.routing.primary_tier_pool must be any, fast, balanced, strong, or empty")
    tier_fallback = str(raw.get("tier_fallback") or "capability").strip().lower()
    if tier_fallback not in _TIER_FALLBACKS:
        raise PolicyPackError(f"{prefix}.routing.tier_fallback must be capability or strict")
    adaptive = _adaptive_profile(raw.get("adaptive"), f"{prefix}.routing.adaptive")
    if adaptive.speed_alpha_min > adaptive.speed_alpha_max:
        raise PolicyPackError(f"{prefix}.routing.adaptive speed alpha min must be <= max")
    if adaptive.reliability_min > adaptive.reliability_max:
        raise PolicyPackError(f"{prefix}.routing.adaptive reliability min must be <= max")
    if adaptive.confidence_medium > adaptive.confidence_high:
        raise PolicyPackError(f"{prefix}.routing.adaptive confidence_medium must be <= confidence_high")
    if adaptive.latency_ttft_weight + adaptive.latency_total_weight <= 0:
        raise PolicyPackError(f"{prefix}.routing.adaptive latency weights must not both be zero")
    if adaptive.runtime_speed_floor + adaptive.runtime_speed_span > 1.0:
        raise PolicyPackError(f"{prefix}.routing.adaptive runtime speed floor + span must be <= 1")
    fit = _fit_profile(raw.get("fit"), f"{prefix}.routing.fit")
    fallback_scoring = _score_weights(raw.get("fallback_scoring"), f"{prefix}.routing.fallback_scoring")
    return RoutingProfile(
        quality, speed, economy, shift, context_bias, tool_bias, tier_pool,
        primary_tier_pool, tier_fallback, adaptive, fit, fallback_scoring,
    )


def _orchestration(raw: Any, prefix: str, *, legacy_collaboration: str = "none", legacy_tier: str = "fast") -> OrchestrationProfile:
    if raw is None:
        return OrchestrationProfile(
            topology="leader-worker" if legacy_collaboration == "relay" else "single",
            max_agents=2 if legacy_collaboration == "relay" else 1,
            worker_mode="always" if legacy_collaboration == "relay" else "none",
            worker_tier_pool=legacy_tier,
            worker_write_intensity_lte=0.15,
        )
    if not isinstance(raw, dict):
        raise PolicyPackError(f"{prefix}.orchestration must be an object")
    objective = str(raw.get("objective") or "value").strip().lower()
    topology = str(raw.get("topology") or "single").strip().lower()
    worker_mode = str(raw.get("worker_mode") or "none").strip().lower()
    worker_tier_pool = str(raw.get("worker_tier_pool") or "fast").strip().lower()
    reasoning_intent = str(raw.get("worker_reasoning_intent") or "economy").strip().lower()
    if objective not in _ORCHESTRATION_OBJECTIVES:
        raise PolicyPackError(f"{prefix}.orchestration.objective is invalid")
    if topology not in _TOPOLOGIES:
        raise PolicyPackError(f"{prefix}.orchestration.topology is invalid")
    if worker_mode not in _WORKER_MODES:
        raise PolicyPackError(f"{prefix}.orchestration.worker_mode is invalid")
    if worker_tier_pool not in _TIERS:
        raise PolicyPackError(f"{prefix}.orchestration.worker_tier_pool is invalid")
    if reasoning_intent not in _REASONING_INTENTS:
        raise PolicyPackError(f"{prefix}.orchestration.worker_reasoning_intent is invalid")
    max_agents = _integer(raw.get("max_agents", 1), f"{prefix}.orchestration.max_agents", lo=1, hi=8)
    max_parallel = _integer(raw.get("max_parallel", 1), f"{prefix}.orchestration.max_parallel", lo=1, hi=8)
    max_depth = _integer(raw.get("max_depth", 1), f"{prefix}.orchestration.max_depth", lo=1, hi=4)
    if max_parallel > max_agents:
        raise PolicyPackError(f"{prefix}.orchestration.max_parallel cannot exceed max_agents")
    return OrchestrationProfile(
        objective=objective,
        topology=topology,
        max_agents=max_agents,
        max_parallel=max_parallel,
        max_depth=max_depth,
        worker_mode=worker_mode,
        worker_tier_pool=worker_tier_pool,
        worker_reasoning_intent=reasoning_intent,
        worker_breadth_gte=_number(raw.get("worker_breadth_gte", 1.0), f"{prefix}.orchestration.worker_breadth_gte"),
        worker_parallelizability_gte=_number(raw.get("worker_parallelizability_gte", 1.0), f"{prefix}.orchestration.worker_parallelizability_gte"),
        worker_tool_intensity_gte=_number(raw.get("worker_tool_intensity_gte", 1.0), f"{prefix}.orchestration.worker_tool_intensity_gte"),
        worker_write_intensity_lte=_number(raw.get("worker_write_intensity_lte", 0.0), f"{prefix}.orchestration.worker_write_intensity_lte"),
        single_writer=bool(raw.get("single_writer", True)),
    )


def _auto_reasoning(raw: Any, prefix: str, *, legacy_intent: str = "inherit") -> AutoReasoningProfile:
    if raw is None:
        # Compatibility mapping for existing data-only packs. This is generic
        # intent mapping, not a dependency on any Type name.
        fallback = {
            "economy": AutoReasoningProfile("prefer_off", "lowest"),
            "balanced": AutoReasoningProfile("adaptive", "adaptive"),
            "high": AutoReasoningProfile("prefer_on", "highest"),
            "maximum": AutoReasoningProfile("prefer_on", "highest"),
        }
        return fallback.get(str(legacy_intent or "inherit").lower(), AutoReasoningProfile())
    if not isinstance(raw, dict):
        raise PolicyPackError(f"{prefix}.reasoning must be an object")
    thinking = str(raw.get("thinking_auto") or "inherit").strip().lower()
    effort = str(raw.get("effort_auto") or "inherit").strip().lower()
    if thinking not in _THINKING_AUTO:
        raise PolicyPackError(f"{prefix}.reasoning.thinking_auto is invalid")
    if effort not in _EFFORT_AUTO:
        raise PolicyPackError(f"{prefix}.reasoning.effort_auto is invalid")
    return AutoReasoningProfile(thinking, effort)


def _secondary(raw: Any, prefix: str) -> SecondaryProfile | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PolicyPackError(f"{prefix}.secondary must be an object")
    role = str(raw.get("role") or "verify").strip().lower()
    objective = str(raw.get("objective") or "value").strip().lower()
    intent = str(raw.get("reasoning_intent") or "inherit").strip().lower()
    if role not in _ROLES:
        raise PolicyPackError(f"{prefix}.secondary.role must be verify or review")
    if objective not in _OBJECTIVES:
        raise PolicyPackError(f"{prefix}.secondary.objective must be economy, value, or quality")
    if intent not in _REASONING_INTENTS:
        raise PolicyPackError(f"{prefix}.secondary.reasoning_intent is invalid")
    reviewer = str(raw.get("reviewer") or "any").strip().lower()
    if reviewer not in _REVIEWERS:
        raise PolicyPackError(f"{prefix}.secondary.reviewer must be any or not_weaker")
    return SecondaryProfile(
        role=role,
        objective=objective,
        trigger=_trigger(raw.get("trigger"), prefix),
        reason=_text(raw.get("reason"), f"{prefix}.secondary.reason", fallback="secondary verification"),
        self_reason=_text(raw.get("self_reason"), f"{prefix}.secondary.self_reason", fallback="single eligible model; self-review fallback"),
        rationale=_text(raw.get("rationale"), f"{prefix}.secondary.rationale", fallback="secondary stage enabled"),
        single_mode=_text(raw.get("single_mode"), f"{prefix}.secondary.single_mode", fallback="single_pass"),
        multi_mode=_text(raw.get("multi_mode"), f"{prefix}.secondary.multi_mode", fallback="verified"),
        reasoning_intent=intent,
        reviewer=reviewer,
        scoring=_score_weights(raw.get("scoring"), f"{prefix}.secondary.scoring"),
        prefer_different_provider=bool(raw.get("prefer_different_provider", True)),
        requires_substantive_work=bool(raw.get("requires_substantive_work", False)),
    )


def parse_policy_pack(raw: Any, *, source: str = "custom", source_path: str = "") -> PolicyPack:
    if not isinstance(raw, dict):
        raise PolicyPackError("policy pack root must be an object")
    schema_version = int(raw.get("schema_version") or 0)
    if schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
        raise PolicyPackError(
            f"unsupported policy pack schema_version {schema_version}; "
            f"supported versions are {sorted(_SUPPORTED_SCHEMA_VERSIONS)}"
        )
    # A Mode is policy data, never an imported Python module. Rejection happens
    # before discovery/activation, so incompatible updates fail closed.
    from .mode_registry import MODE_PLUGIN_API_VERSION
    plugin_version = raw.get("plugin_version", "0.0.0")
    if not isinstance(plugin_version, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", plugin_version):
        raise PolicyPackError("plugin_version must be a numeric major.minor.patch string")
    registry_api = raw.get("registry_api", MODE_PLUGIN_API_VERSION)
    if isinstance(registry_api, bool) or not isinstance(registry_api, int) or registry_api != MODE_PLUGIN_API_VERSION:
        raise PolicyPackError(f"registry_api {registry_api!r} is incompatible with Mode Registry {MODE_PLUGIN_API_VERSION}")
    pack_id = _require_id(raw.get("id"), "id")
    display_name = _text(raw.get("display_name"), "display_name", fallback=pack_id)
    short_name = _text(raw.get("short_name"), "short_name", fallback=display_name)
    localized_name = str(raw.get("localized_name") or "").strip()
    description = _text(raw.get("description"), "description")
    default_policy = _require_id(raw.get("default_policy"), "default_policy")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PolicyPackError("enabled must be a boolean")
    policies_raw = raw.get("policies")
    if not isinstance(policies_raw, list) or not policies_raw:
        raise PolicyPackError("policies must be a non-empty array")
    policies: list[PolicyDefinition] = []
    seen: set[str] = set()
    for index, row in enumerate(policies_raw):
        prefix = f"policies[{index}]"
        if not isinstance(row, dict):
            raise PolicyPackError(f"{prefix} must be an object")
        policy_id = _require_id(row.get("id"), f"{prefix}.id")
        if policy_id in seen:
            raise PolicyPackError(f"duplicate policy id {policy_id!r}")
        seen.add(policy_id)
        if schema_version >= 2:
            semantics_raw = row.get("semantics")
            execution_raw = row.get("execution")
            if not isinstance(semantics_raw, dict):
                raise PolicyPackError(f"{prefix}.semantics must be an object")
            if not isinstance(execution_raw, dict):
                raise PolicyPackError(f"{prefix}.execution must be an object")
        else:
            # Schema v1 mixed UI semantics and execution fields in one object.
            # Keep it readable so existing user packs continue to hot-load.
            semantics_raw = row
            execution_raw = row

        primary_reasoning = str(execution_raw.get("reasoning_intent") or "inherit").strip().lower()
        if primary_reasoning not in _REASONING_INTENTS:
            raise PolicyPackError(f"{prefix}.execution.reasoning_intent is invalid")
        collaboration = str(execution_raw.get("collaboration") or "none").strip().lower()
        if collaboration not in {"none", "relay"}:
            raise PolicyPackError(f"{prefix}.execution.collaboration must be none or relay")
        collaboration_tier_pool = str(execution_raw.get("collaboration_tier_pool") or "fast").strip().lower()
        if collaboration_tier_pool not in _TIERS:
            raise PolicyPackError(
                f"{prefix}.execution.collaboration_tier_pool must be any, fast, balanced, or strong"
            )
        collaboration_routing_raw = execution_raw.get("collaboration_routing")
        collaboration_routing = (
            _routing(collaboration_routing_raw, f"{prefix}.execution.collaboration")
            if collaboration_routing_raw is not None else None
        )
        collaboration_provider_affinity = _number(
            execution_raw.get("collaboration_provider_affinity", 0.025),
            f"{prefix}.execution.collaboration_provider_affinity",
        )
        policy_description = _text(semantics_raw.get("description"), f"{prefix}.semantics.description")
        semantics = UserPolicySemantics(
            display_name=_text(semantics_raw.get("display_name"), f"{prefix}.semantics.display_name", fallback=policy_id),
            localized_name=str(semantics_raw.get("localized_name") or "").strip(),
            description=policy_description,
            short_description=_text(
                semantics_raw.get("short_description"),
                f"{prefix}.semantics.short_description",
                fallback=policy_description,
            ),
        )
        execution = ExecutionPolicy(
            routing=_routing(execution_raw.get("routing"), f"{prefix}.execution"),
            orchestration=_orchestration(
                execution_raw.get("orchestration"), f"{prefix}.execution",
                legacy_collaboration=collaboration, legacy_tier=collaboration_tier_pool,
            ),
            secondary=_secondary(execution_raw.get("secondary"), f"{prefix}.execution"),
            auto_reasoning=_auto_reasoning(
                execution_raw.get("reasoning"), f"{prefix}.execution", legacy_intent=primary_reasoning
            ),
            primary_reasoning=primary_reasoning,
            sticky=bool(execution_raw.get("sticky", False)),
            collaboration=collaboration,
            collaboration_tier_pool=collaboration_tier_pool,
            collaboration_routing=collaboration_routing,
            collaboration_provider_affinity=collaboration_provider_affinity,
        )
        policies.append(PolicyDefinition(policy_id, semantics, execution))
    if default_policy not in seen:
        raise PolicyPackError(f"default_policy {default_policy!r} is not present in policies")
    return PolicyPack(
        id=pack_id, display_name=display_name, short_name=short_name,
        localized_name=localized_name, description=description,
        default_policy=default_policy, policies=tuple(policies),
        is_default=bool(raw.get("default", False)), enabled=enabled,
        source=source, source_path=source_path,
        plugin_version=plugin_version, registry_api=registry_api,
    )


def _read_pack_file(path: Path, *, source: str) -> PolicyPack:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyPackError(f"could not read {path}: {exc}") from exc
    return parse_policy_pack(raw, source=source, source_path=str(path))


def _pack_files(directory: Path) -> Iterable[Path]:
    try:
        return tuple(sorted(directory.glob("*.json"), key=lambda p: p.name.lower()))
    except OSError:
        return ()


def _discover_policy_packs(*, include_errors: bool = False) -> tuple[PolicyPack, ...] | tuple[tuple[PolicyPack, ...], tuple[dict[str, str], ...]]:
    """Discover registered built-in Mode plug-ins and user JSON Policy Packs.

    Built-in declarations live in ``kiro_byo.modes`` and own their stable
    Mode/Type identity. Custom packs retain S4-01/S4-02 hot-load behavior;
    Tiga's legacy JSON override remains readable for existing installations.
    No external Python modules are imported during discovery.
    """
    from .modes import builtin_modes
    from .mode_registry import MODE_PLUGIN_API_VERSION

    packs: dict[str, PolicyPack] = {}
    errors: list[dict[str, str]] = []
    for mode in builtin_modes():
        path = mode.policy_path
        try:
            if mode.registry_api != MODE_PLUGIN_API_VERSION:
                raise PolicyPackError(f"incompatible builtin Mode API for {mode.id!r}")
            pack = _read_pack_file(path, source="builtin")
            if (pack.id != mode.id or
                    tuple(row.id for row in pack.policies) != mode.type_ids or
                    pack.default_policy != mode.default_type or
                    pack.enabled != mode.enabled):
                raise PolicyPackError(f"builtin Mode {mode.id!r} does not match its registered contract")
            packs[mode.id] = pack
        except PolicyPackError as exc:
            errors.append({"path": str(path), "error": str(exc)})

    for path in _pack_files(policy_pack_dir()):
        try:
            pack = _read_pack_file(path, source="custom")
        except PolicyPackError as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        # The built-in executable Dyna is reserved: a custom policy must not
        # shadow its controlled implementation. Tiga's historical manual
        # override remains for backwards compatibility.
        if pack.id == "dyna":
            errors.append({"path": str(path), "error": "mode id 'dyna' is reserved until Dyna is released"})
            continue
        packs[pack.id] = pack
    ordered = tuple(sorted(packs.values(), key=lambda p: (p.source != "builtin", p.display_name.lower(), p.id)))
    if include_errors:
        return ordered, tuple(errors)
    return ordered


def load_policy_packs(*, include_errors: bool = False) -> tuple[PolicyPack, ...] | tuple[tuple[PolicyPack, ...], tuple[dict[str, str], ...]]:
    """Backwards-compatible Policy Pack API backed by the Mode Registry.

    Existing Crew, config, UI and routing callers keep receiving PolicyPack
    objects.  The registry owns the single discovery/activation boundary.
    """
    from .mode_registry import load_mode_registry

    registry = load_mode_registry()
    if include_errors:
        return registry.packs, registry.errors
    return registry.packs


def get_policy_pack(pack_id: str) -> PolicyPack | None:
    from .mode_registry import load_mode_registry

    plugin = load_mode_registry().get(pack_id)
    return plugin.pack if plugin is not None else None


def resolve_policy(pack_id: str, policy_id: str) -> tuple[PolicyPack, PolicyDefinition]:
    from .mode_registry import load_mode_registry

    return load_mode_registry().resolve(pack_id, policy_id)


def default_pack() -> PolicyPack:
    from .mode_registry import load_mode_registry

    return load_mode_registry().default().pack


def policy_catalogue_requested(text: str, pack_id: str = "") -> bool:
    """Return whether the user is actually asking about Mode/Type product semantics.

    The normal inference path should stay transparent: a filesystem, coding or
    research task does not need the full catalogue of every installed Mode and
    Type injected into the model prompt.  Full product metadata is only useful
    when the user explicitly discusses the control surface itself.
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    folded = raw.casefold()
    packs = load_policy_packs()
    selected = next((row for row in packs if row.id == str(pack_id or "").strip().lower()), None)

    # A concrete Mode/Type name is authoritative evidence and works across
    # languages because these are product terms, not natural-language task
    # difficulty heuristics.
    terms: set[str] = {"kiro byo"}
    policy_ids: set[str] = set()
    for pack in packs:
        for value in (pack.short_name, pack.display_name, pack.localized_name):
            value = str(value or "").strip().casefold()
            if value:
                terms.add(value)
        if pack.id:
            terms.add(pack.id.casefold())
        for policy in pack.policies:
            if policy.id:
                policy_ids.add(policy.id.casefold())
            for value in (policy.display_name, policy.localized_name):
                value = str(value or "").strip().casefold()
                if value:
                    terms.add(value)
    if any(term in folded for term in terms if term not in {"kiro byo", "tiga"}):
        return True

    # Bare ids such as ``power`` and ``multi`` are ordinary English words.
    # Treat them as product terms only when the surrounding request clearly
    # discusses Types, or when two different Type ids are compared together.
    mentioned_ids = {
        policy_id
        for policy_id in policy_ids
        if re.search(rf"(?<![a-z0-9_-]){re.escape(policy_id)}(?![a-z0-9_-])", folded)
    }
    if len(mentioned_ids) >= 2:
        return True
    if mentioned_ids and re.search(r"\btypes?\b|形态|形態|模式|モード|タイプ", raw, re.I):
        return True

    # Also recognise generic questions such as “解释四个形态” even when the
    # individual Type names are omitted.  Keep this deliberately narrow so a
    # programming question about a Python ``type`` does not pull product
    # metadata into an unrelated turn.
    if re.search(r"(?:四|4|four)\s*(?:个|種|种|つ)?\s*(?:形态|形態|types?|forms?)", raw, re.I):
        return True
    if re.search(
        r"(?:当前|现在|这个|本次|current|active|this|現在の|この)\s*(?:的)?\s*(?:形态|形態|模式|modes?|types?|モード|タイプ)",
        raw,
        re.I,
    ):
        return True
    if "kiro byo" in folded and re.search(r"\b(?:mode|type)s?\b|形态|形態|模式|モード|タイプ", raw, re.I):
        return True
    if selected is not None:
        selected_name = (selected.short_name or selected.display_name or selected.id).casefold()
        if selected_name and selected_name in folded and re.search(r"\b(?:mode|type)s?\b|形态|形態|模式|モード|タイプ", raw, re.I):
            return True
    return False


def runtime_policy_context(
    pack_id: str,
    policy_id: str = "",
    *,
    active: bool = True,
    include_catalogue: bool = False,
) -> str:
    """Return authoritative Mode/Type metadata for a model runtime prompt.

    Ordinary turns receive only the current routing state.  The full installed
    Mode/Type catalogue is opt-in for turns that explicitly ask about the
    product semantics.  This keeps Multi close to a transparent proxy instead
    of letting unrelated tasks be steered by UI/product descriptions.
    """
    packs = load_policy_packs()
    pack = next((row for row in packs if row.id == str(pack_id or "").strip().lower()), None)
    if pack is None or not pack.enabled:
        return ""
    current = pack.policy(policy_id) if policy_id else None

    def pack_name(row: PolicyPack) -> str:
        name = row.short_name or row.display_name or row.id
        return f"{name}（{row.localized_name}）" if row.localized_name else name

    def type_name(row: PolicyDefinition) -> str:
        name = row.display_name or row.id
        return f"{name}（{row.localized_name}）" if row.localized_name else name

    lines = [
        "Kiro BYO routing metadata (authoritative product terminology):",
        f"Current configured Mode: {pack_name(pack)}",
    ]
    if active and current is not None:
        lines.append(f"Active Type: {type_name(current)}")
    elif not active:
        lines.append("This turn directly selected a concrete model, so Mode/Type routing is not controlling model selection for this turn.")

    lines.append("A Type is an execution/routing policy, not the concrete upstream model.")

    if include_catalogue:
        lines.append("Installed Modes:")
        for installed in packs:
            lines.append(f"- {pack_name(installed)}: {installed.description}")
        lines.append("Types in the current Mode:")
        for row in pack.policies:
            marker = " [active]" if active and current is not None and row.id == current.id else ""
            lines.append(f"- {type_name(row)}{marker}: {row.description}")
        lines.append("Type control surface: the active Type is supplied to Kiro BYO by KiroCrew's model picker as session selection.")
        lines.append("Use these official Mode/Type names and localized names exactly. Never invent alternate translations.")
    return "\n".join(lines)


def runtime_policy_context_for_request(
    pack_id: str,
    policy_id: str,
    request_text: str,
    *,
    active: bool,
) -> str:
    """Expose Mode/Type product semantics only when the user asks for them.

    Routing, thinking and collaboration are already enforced by the harness.
    Ordinary model turns therefore do not need product UI terminology in their
    system prompt at all. This keeps BYO transparent for normal agent work.
    """
    if not policy_catalogue_requested(request_text, pack_id):
        return ""
    return runtime_policy_context(
        pack_id, policy_id, active=active, include_catalogue=True
    )


def public_policy_packs() -> dict[str, Any]:
    from .protocol import type_selection_id
    from .mode_registry import MODE_PLUGIN_API_VERSION

    packs, errors = load_policy_packs(include_errors=True)
    return {
        "mode_registry_api_version": MODE_PLUGIN_API_VERSION,
        "mode_plugin_kind": "data-only-policy-pack",
        "directory": str(policy_pack_dir()),
        "hot_reload": True,  # Legacy flag: custom JSON is discovered on read.
        "hot_reload_scope": "custom-json-policy-only",
        "executable_hot_swap": False,
        "schema_version": SCHEMA_VERSION,
        "packs": [
            {
                "id": pack.id,
                "display_name": pack.display_name,
                "short_name": pack.short_name,
                "localized_name": pack.localized_name,
                "description": pack.description,
                "default_policy": pack.default_policy,
                "default": pack.is_default,
                "enabled": pack.enabled,
                "status": "available" if pack.enabled else "coming_soon",
                "source": pack.source,
                "plugin_version": pack.plugin_version,
                "registry_api": pack.registry_api,
                # Additive, explicit lifecycle metadata; built-ins cannot be
                # managed/overwritten through the custom Mode lifecycle API.
                "policy_hot_reload": pack.source == "custom",
                "managed_upgrades": pack.source == "custom",
                "code_hot_swap": False,
                "policies": [
                    {
                        "id": policy.id,
                        "selection_id": type_selection_id(pack.id, policy.id),
                        "display_name": policy.display_name,
                        "localized_name": policy.localized_name,
                        "description": policy.description,
                        "short_description": policy.short_description,
                    }
                    for policy in pack.policies
                ],
            }
            for pack in packs
        ],
        "errors": list(errors),
    }
