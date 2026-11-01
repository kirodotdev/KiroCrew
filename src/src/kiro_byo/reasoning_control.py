"""User-facing deep-thinking and reasoning-effort policy resolution.

Deep-thinking enablement and reasoning *effort* are independent axes.

Deep thinking is stored per model in the BYO App::

    auto
        The active Type may decide whether this concrete model should use deep
        thinking for the current invocation.
    on
        Hard user lock. A Type may not turn thinking off.
    off
        Hard user lock. A Type may not turn thinking on.

Reasoning effort is model-scoped only when the user fixes a concrete model.
For a routed Type, effort is owned by the Type itself: every invocation chooses
only from the finally selected concrete model's advertised levels. Models with
no selectable levels remain fully eligible and simply receive no effort field.

Provider-native values such as ``adaptive`` are transport details and are not
BYO's ``auto`` state. Provider adapters should receive only resolved effective
states and vocabulary the concrete model actually advertises.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

THINKING_PREFERENCES = ("auto", "on", "off")
THINKING_AUTO_STRATEGIES = frozenset({
    "inherit",
    "prefer_off",
    "adaptive",
    "prefer_on",
    "adaptive_per_call",
})
EFFORT_AUTO_STRATEGIES = frozenset({
    "inherit",
    "lowest",
    "adaptive",
    "highest",
    "adaptive_per_call",
})


@dataclass(frozen=True)
class ReasoningInvocation:
    """Provider-neutral signals for one model invocation.

    ``complexity`` is the already-sanitised task complexity produced by the
    router. ``stage`` and ``role`` let collaboration policies refine the same
    task-level score per invocation without teaching this module any Type names.
    """

    complexity: float = 0.0
    code_task: bool = False
    tools_expected: bool = False
    has_image: bool = False
    role: str = "lead"
    stage: str = "turn"


def normalize_thinking_preference(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in THINKING_PREFERENCES else "auto"


def thinking_control(model: Any) -> str:
    """Return the user-facing control class for one model."""
    supported = getattr(model, "reasoning_supported", None)
    can_disable = getattr(model, "reasoning_can_disable", None)
    if supported is False:
        return "unsupported"
    if supported is True and can_disable is True:
        return "switchable"
    if supported is True and can_disable is False:
        return "fixed_on"
    return "unknown"


def resolve_thinking_mode(model: Any, preference: str, suggested: str = "") -> str:
    """Resolve a per-model BYO preference into an effective on/off state.

    ``suggested`` is the Type's recommendation and is consulted only when the
    user's stored preference is ``auto``. Hard ``on`` / ``off`` user choices
    always win. Fixed-on models stay on, unsupported models stay off, and an
    unknown capability remains unknown rather than being guessed.
    """
    control = thinking_control(model)
    if control == "unsupported":
        return "off"
    if control == "unknown":
        return ""
    if control == "fixed_on":
        return "on"

    pref = normalize_thinking_preference(preference)
    if pref in {"on", "off"}:
        return pref

    hint = str(suggested or "").strip().lower()
    if hint in {"on", "off"}:
        return hint

    # No Type recommendation: preserve the verified model/provider default.
    default_enabled = getattr(model, "reasoning_default_enabled", None)
    if default_enabled is True:
        return "on"
    if default_enabled is False:
        return "off"
    return ""


def _clamp01(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def invocation_reasoning_score(context: ReasoningInvocation, *, per_call: bool) -> float:
    """Return a generic 0..1 need-for-reasoning score.

    The router already removes Crew's system/memory wrappers before producing
    ``complexity``.  These adjustments are intentionally provider-neutral and
    conservative: they express the nature of the current invocation, not model
    branding or a hidden Type-specific rule.
    """
    score = _clamp01(context.complexity)
    if context.code_task:
        score += 0.08
    if context.has_image:
        score += 0.03
    if context.tools_expected:
        score += 0.03

    if per_call:
        role = str(context.role or "").strip().lower()
        stage = str(context.stage or "").strip().lower()
        if role in {"review", "verify"}:
            score += 0.14
        if stage in {"planning", "analysis", "synthesis", "review", "verification", "modification"}:
            score += 0.12
        elif stage in {"gathering", "lookup", "read", "fetch"}:
            score -= 0.22
    return _clamp01(score)


def thinking_auto_suggestion(strategy: str, context: ReasoningInvocation) -> str:
    """Resolve a policy's AUTO-thinking strategy to ``on`` / ``off`` / ``""``.

    The caller still passes the result through :func:`resolve_thinking_mode`,
    therefore a Type can never override a user's explicit per-model ON/OFF lock
    or a model capability that is fixed by the provider.
    """
    mode = str(strategy or "inherit").strip().lower()
    if mode not in THINKING_AUTO_STRATEGIES:
        mode = "inherit"
    if mode == "prefer_off":
        return "off"
    if mode == "prefer_on":
        return "on"
    if mode == "adaptive":
        return "on" if invocation_reasoning_score(context, per_call=False) >= 0.56 else "off"
    if mode == "adaptive_per_call":
        return "on" if invocation_reasoning_score(context, per_call=True) >= 0.54 else "off"
    return ""


def _ordered_effort_levels(model: Any) -> tuple[str, ...]:
    """Return the concrete model's enabled effort levels in provider order."""
    values: list[str] = []
    for raw in getattr(model, "effort_supported_levels", ()) or ():
        value = str(raw or "").strip().lower()
        if value and value not in values:
            values.append(value)
    return tuple(values)


def _adaptive_effort(levels: tuple[str, ...], score: float) -> str:
    if not levels:
        return ""
    if len(levels) == 1:
        return levels[0]
    index = int(round(_clamp01(score) * (len(levels) - 1)))
    return levels[max(0, min(len(levels) - 1, index))]


def resolve_effort_level(
    model: Any,
    *,
    user_level: str = "",
    user_locked: bool = False,
    strategy: str = "inherit",
    context: ReasoningInvocation | None = None,
    effective_thinking: str = "",
) -> str:
    """Resolve reasoning effort without inventing unsupported model levels.

    Explicit user selection is a hard lock *when the selected model actually
    advertises that level*.  Automatic mode lets the Type select only from the
    model's ordered advertised levels.  If effective thinking is OFF, effort is
    deliberately omitted because ``OFF + high`` is a contradictory wire state.
    """
    if effective_thinking == "off":
        return ""
    levels = _ordered_effort_levels(model)
    if not levels:
        return ""

    current = str(user_level or "").strip().lower()
    if user_locked:
        return current if current in levels else ""

    mode = str(strategy or "inherit").strip().lower()
    if mode not in EFFORT_AUTO_STRATEGIES:
        mode = "inherit"
    if mode == "lowest":
        return levels[0]
    if mode == "highest":
        return levels[-1]
    if mode in {"adaptive", "adaptive_per_call"}:
        ctx = context or ReasoningInvocation()
        score = invocation_reasoning_score(ctx, per_call=(mode == "adaptive_per_call"))
        return _adaptive_effort(levels, score)

    # ``inherit`` means no Type preference. Use a verified provider default if
    # it belongs to the advertised vocabulary, otherwise omit the parameter.
    default = str(getattr(model, "effort_default_level", "") or "").strip().lower()
    return default if default in levels else ""


def selectable_effort_levels(model: Any) -> tuple[str, ...]:
    """Public helper used by tests/UI logic without depending on ModelSpec."""
    return _ordered_effort_levels(model)
