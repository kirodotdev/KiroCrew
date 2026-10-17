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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .paths import app_home

SCHEMA_VERSION = 1
_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")
_OBJECTIVES = frozenset({"economy", "value", "quality"})
_ROLES = frozenset({"verify", "review"})
_REASONING_INTENTS = frozenset({"inherit", "economy", "balanced", "high", "maximum"})
_THINKING_AUTO = frozenset({"inherit", "prefer_off", "adaptive", "prefer_on", "adaptive_per_call"})
_EFFORT_AUTO = frozenset({"inherit", "lowest", "adaptive", "highest", "adaptive_per_call"})
_REVIEWERS = frozenset({"any", "not_weaker"})


class PolicyPackError(ValueError):
    """Raised when a policy pack is malformed."""


@dataclass(frozen=True)
class ComplexityShift:
    mode: str = "none"
    strength: float = 0.0
    threshold: float = 1.0
    quality_add: float = 0.0


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
    # Which model tier this form may use: ``any`` (Multi/Glitter), ``fast``
    # (Sky: never escalates to strong models) or ``strong`` (Power: never
    # drops to fast models). Outside-tier models are used only when no
    # in-tier model can run the turn at all.
    tier_pool: str = "any"
    # Optional stricter pool for the primary/leader only. Candidate rows remain
    # available to collaboration/review logic.
    primary_tier_pool: str = ""


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
    # Verification can be deferred until the primary actually did meaningful
    # work (tool use or a substantial answer). Final-review policies such as
    # Glitter can leave this false to remain mandatory.
    requires_substantive_work: bool = False


@dataclass(frozen=True)
class ExecutionPolicy:
    id: str
    display_name: str
    localized_name: str
    description: str
    short_description: str
    routing: RoutingProfile
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
    # changes and answers, while a fast same-provider teammate continues pure
    # information-gathering rounds (Glitter's multi-model collaboration).
    collaboration: str = "none"


@dataclass(frozen=True)
class PolicyPack:
    id: str
    display_name: str
    short_name: str
    localized_name: str
    description: str
    default_policy: str
    policies: tuple[ExecutionPolicy, ...]
    is_default: bool = False
    source: str = "builtin"
    source_path: str = ""

    def policy(self, policy_id: str) -> ExecutionPolicy | None:
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


def _trigger(raw: Any, prefix: str) -> SecondaryTrigger:
    if raw is None:
        return SecondaryTrigger()
    if not isinstance(raw, dict):
        raise PolicyPackError(f"{prefix}.trigger must be an object")
    complexity = raw.get("complexity_gte")
    if complexity is not None:
        complexity = _number(complexity, f"{prefix}.trigger.complexity_gte")
    reasoning_raw = raw.get("reasoning_any", [])
    if not isinstance(reasoning_raw, list) or any(not isinstance(x, str) for x in reasoning_raw):
        raise PolicyPackError(f"{prefix}.trigger.reasoning_any must be an array of strings")
    reasoning = tuple(dict.fromkeys(str(x).strip().lower() for x in reasoning_raw if str(x).strip()))
    return SecondaryTrigger(
        always=bool(raw.get("always", False)),
        complexity_gte=complexity,
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
        shift = ComplexityShift(mode, strength, threshold, quality_add)
    context_bias = _number(raw.get("context_bias", 0.0), f"{prefix}.routing.context_bias")
    tool_bias = _number(raw.get("tool_bias", 0.0), f"{prefix}.routing.tool_bias")
    tier_pool = str(raw.get("tier_pool") or "any").strip().lower()
    if tier_pool not in {"any", "fast", "strong"}:
        raise PolicyPackError(f"{prefix}.routing.tier_pool must be any, fast, or strong")
    primary_tier_pool = str(raw.get("primary_tier_pool") or "").strip().lower()
    if primary_tier_pool not in {"", "any", "fast", "strong"}:
        raise PolicyPackError(f"{prefix}.routing.primary_tier_pool must be any, fast, strong, or empty")
    return RoutingProfile(quality, speed, economy, shift, context_bias, tool_bias, tier_pool, primary_tier_pool)


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
        requires_substantive_work=bool(raw.get("requires_substantive_work", False)),
    )


def parse_policy_pack(raw: Any, *, source: str = "custom", source_path: str = "") -> PolicyPack:
    if not isinstance(raw, dict):
        raise PolicyPackError("policy pack root must be an object")
    if int(raw.get("schema_version") or 0) != SCHEMA_VERSION:
        raise PolicyPackError(f"unsupported policy pack schema_version; expected {SCHEMA_VERSION}")
    pack_id = _require_id(raw.get("id"), "id")
    display_name = _text(raw.get("display_name"), "display_name", fallback=pack_id)
    short_name = _text(raw.get("short_name"), "short_name", fallback=display_name)
    localized_name = str(raw.get("localized_name") or "").strip()
    description = _text(raw.get("description"), "description")
    default_policy = _require_id(raw.get("default_policy"), "default_policy")
    policies_raw = raw.get("policies")
    if not isinstance(policies_raw, list) or not policies_raw:
        raise PolicyPackError("policies must be a non-empty array")
    policies: list[ExecutionPolicy] = []
    seen: set[str] = set()
    for index, row in enumerate(policies_raw):
        prefix = f"policies[{index}]"
        if not isinstance(row, dict):
            raise PolicyPackError(f"{prefix} must be an object")
        policy_id = _require_id(row.get("id"), f"{prefix}.id")
        if policy_id in seen:
            raise PolicyPackError(f"duplicate policy id {policy_id!r}")
        seen.add(policy_id)
        primary_reasoning = str(row.get("reasoning_intent") or "inherit").strip().lower()
        if primary_reasoning not in _REASONING_INTENTS:
            raise PolicyPackError(f"{prefix}.reasoning_intent is invalid")
        collaboration = str(row.get("collaboration") or "none").strip().lower()
        if collaboration not in {"none", "relay"}:
            raise PolicyPackError(f"{prefix}.collaboration must be none or relay")
        policy_description = _text(row.get("description"), f"{prefix}.description")
        short_description = _text(
            row.get("short_description"),
            f"{prefix}.short_description",
            fallback=policy_description,
        )
        policies.append(ExecutionPolicy(
            id=policy_id,
            display_name=_text(row.get("display_name"), f"{prefix}.display_name", fallback=policy_id),
            localized_name=str(row.get("localized_name") or "").strip(),
            description=policy_description,
            short_description=short_description,
            routing=_routing(row.get("routing"), prefix),
            secondary=_secondary(row.get("secondary"), prefix),
            auto_reasoning=_auto_reasoning(row.get("reasoning"), prefix, legacy_intent=primary_reasoning),
            primary_reasoning=primary_reasoning,
            sticky=bool(row.get("sticky", False)),
            collaboration=collaboration,
        ))
    if default_policy not in seen:
        raise PolicyPackError(f"default_policy {default_policy!r} is not present in policies")
    return PolicyPack(pack_id, display_name, short_name, localized_name, description, default_policy, tuple(policies), bool(raw.get("default", False)), source, source_path)


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


def load_policy_packs(*, include_errors: bool = False) -> tuple[PolicyPack, ...] | tuple[tuple[PolicyPack, ...], tuple[dict[str, str], ...]]:
    """Load built-ins plus user packs, re-reading disk every call.

    User packs with the same id intentionally replace a built-in pack. This is
    a data-only override; no Python code is imported or executed.
    """
    packs: dict[str, PolicyPack] = {}
    errors: list[dict[str, str]] = []
    builtin_dir = Path(__file__).resolve().parent / "policy_packs"
    for source, directory in (("builtin", builtin_dir), ("custom", policy_pack_dir())):
        for path in _pack_files(directory):
            try:
                pack = _read_pack_file(path, source=source)
            except PolicyPackError as exc:
                errors.append({"path": str(path), "error": str(exc)})
                continue
            packs[pack.id] = pack
    ordered = tuple(sorted(packs.values(), key=lambda p: (p.source != "builtin", p.display_name.lower(), p.id)))
    if include_errors:
        return ordered, tuple(errors)
    return ordered


def get_policy_pack(pack_id: str) -> PolicyPack | None:
    key = str(pack_id or "").strip().lower()
    for pack in load_policy_packs():
        if pack.id == key:
            return pack
    return None


def resolve_policy(pack_id: str, policy_id: str) -> tuple[PolicyPack, ExecutionPolicy]:
    pack = get_policy_pack(pack_id)
    if pack is None:
        raise PolicyPackError(f"policy pack {pack_id!r} is not installed")
    policy = pack.policy(policy_id)
    if policy is None:
        raise PolicyPackError(f"policy {policy_id!r} is not present in pack {pack.id!r}")
    return pack, policy


def default_pack() -> PolicyPack:
    packs = load_policy_packs()
    if not packs:
        raise PolicyPackError("no valid policy packs are installed")
    declared = [pack for pack in packs if pack.is_default]
    if declared:
        return sorted(
            declared,
            key=lambda p: (p.source != "builtin", p.display_name.lower(), p.id),
        )[0]
    return packs[0]


def runtime_policy_context(pack_id: str, policy_id: str = "", *, active: bool = True) -> str:
    """Return compact authoritative Mode/Type metadata for model runtime prompts.

    Names and explanations come only from installed Policy Pack data. The core
    therefore never needs to know Tiga/Dyna/Gaia-specific vocabulary.
    """
    packs = load_policy_packs()
    pack = next((row for row in packs if row.id == str(pack_id or "").strip().lower()), None)
    if pack is None:
        return ""
    current = pack.policy(policy_id) if policy_id else None

    def pack_name(row: PolicyPack) -> str:
        name = row.short_name or row.display_name or row.id
        return f"{name}（{row.localized_name}）" if row.localized_name else name

    def type_name(row: ExecutionPolicy) -> str:
        name = row.display_name or row.id
        return f"{name}（{row.localized_name}）" if row.localized_name else name

    lines = [
        "Kiro BYO Mode/Type metadata (authoritative product terminology):",
        "Installed Modes:",
    ]
    for installed in packs:
        lines.append(f"- {pack_name(installed)}: {installed.description}")
    lines.extend([
        f"Current configured Mode: {pack_name(pack)}",
        f"Current Mode meaning: {pack.description}",
    ])
    if active and current is not None:
        lines.extend([
            f"Active Type: {type_name(current)}",
            f"Active Type meaning: {current.description}",
        ])
    elif not active:
        lines.append("This turn directly selected a concrete model, so Mode/Type routing is not controlling model selection for this turn.")
    lines.append("Types in the current Mode:")
    for row in pack.policies:
        marker = " [active]" if active and current is not None and row.id == current.id else ""
        lines.append(f"- {type_name(row)}{marker}: {row.description}")
    lines.append("Use these official Mode/Type names and localized names exactly. Never invent alternate translations. A Type is an execution/routing policy, not the concrete upstream model.")
    return "\n".join(lines)


def public_policy_packs() -> dict[str, Any]:
    from .protocol import type_selection_id

    packs, errors = load_policy_packs(include_errors=True)
    return {
        "directory": str(policy_pack_dir()),
        "hot_reload": True,
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
                "source": pack.source,
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
