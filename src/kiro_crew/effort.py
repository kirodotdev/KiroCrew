"""Shared reasoning-effort vocabulary for all LLM providers.

Both kiro-cli and claude-agent-acp expose a per-session "effort" (a.k.a.
thinking depth) knob, but only on Claude Fable/Opus/Sonnet models.  This module
is the single source of truth for the valid levels and the model-capability
check so the CLI, dashboard handlers, providers, and config loader all agree.

Stdlib-only and import-light on purpose — it is imported from hot paths
(``providers/acp.py``, ``dashboard/chat_handlers.py``) and must not create
import cycles.

References:
- kiro-cli ``/effort``: levels ``low|medium|high|xhigh|max``, Opus/Sonnet only,
  per-model defaults via ``~/.kiro/settings/cli.json`` →
  ``chat.modelDefaults.<model>.output_config.effort``.
- claude-agent-acp ``buildConfigOptions``: effort options come from each
  model's ``supportedEffortLevels``; recommended default ``xhigh`` then ``high``.
"""

from __future__ import annotations

import json
import logging

from kiro_crew import model_registry

logger = logging.getLogger(__name__)

# Concrete effort levels, ordered low→high.  ``""`` is NOT a level — it means
# "no explicit override; use the provider/model default" and is handled by the
# callers, not stored here.  ``xhigh`` sits between ``high`` and ``max`` and is
# the recommended default for capable Opus models in both backends.
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

# Accepted by the API/persistence layer: the concrete levels plus the empty
# sentinel for "provider default".  Single source for ``_REASONING_EFFORT_VALUES``.
EFFORT_VALUES: frozenset[str] = frozenset({""} | set(EFFORT_LEVELS))


def is_valid_effort(level: object) -> bool:
    """True if *level* is one of the concrete effort levels (excludes "")."""
    return isinstance(level, str) and level in EFFORT_LEVELS


def model_supports_effort(model: str | None) -> bool:
    """True when *model* is a Claude Fable/Opus/Sonnet model that accepts effort.

    Effort is only available on Claude Fable, Opus, and Sonnet (per kiro-cli
    FAQ and the claude-agent-acp ``supportsEffort`` model flag).  Haiku, Nova,
    and third-party models do not support it; ``"auto"``/``None`` cannot either
    (kiro-cli errors "Effort configuration is currently not available on auto"
    until a concrete model is selected).

    Matches both naming conventions: kiro-cli (``claude-fable-5``) and the
    Bedrock/claude-agent-acp form (``global.anthropic.claude-fable-5[1m]``).

    Prefers the registry's declared ``supports_effort`` flag when the model is in
    the registry, so a future model whose canonical key lacks the ``opus``/
    ``sonnet`` substring (or a capable model the heuristic would miss) is honored;
    falls back to the substring heuristic for ids the registry doesn't list.
    """
    if not model:
        return False
    m = model.lower()
    # Haiku NEVER supports effort — a hard rule that must win even over the
    # registry. A kiro Haiku id (``claude-haiku-4.5``) is registered as a
    # claude_code ALIAS of Sonnet (the cheapest VALID Bedrock fold), so a naive
    # registry consult would let it inherit Sonnet's ``supports_effort`` flag and
    # wrongly report a kiro Haiku agent as effort-capable. On the claude_code
    # path the haiku->Sonnet fold already happened at the translation boundary
    # (config.loader factory), so the value reaching here is the Sonnet provider
    # id (no "haiku" substring) and stays capable; only the raw kiro spelling —
    # which the kiro/acp path passes untranslated — is gated here.
    if "haiku" in m:
        return False
    try:
        declared = model_registry.supports_effort(model)
        if declared is not None:
            return declared
    except Exception:
        pass  # fall back to the heuristic
    return "opus" in m or "sonnet" in m or "fable" in m


def _coerce_defaults(defaults: object) -> dict[str, str]:
    """Normalize a per-model defaults blob into ``{model: level}``.

    Accepts a dict or a JSON-string (the frontend ``setVariable`` signature
    only takes strings, so saved values arrive stringified).  Returns ``{}``
    on any malformed input — never raises.
    """
    if isinstance(defaults, str):
        if not defaults.strip():
            return {}
        try:
            defaults = json.loads(defaults)
        except (ValueError, TypeError):
            logger.debug("Discarding malformed effort defaults JSON: %r", defaults)
            return {}
    if not isinstance(defaults, dict):
        return {}
    out: dict[str, str] = {}
    for model, level in defaults.items():
        if isinstance(model, str) and is_valid_effort(level):
            out[model] = level  # type: ignore[assignment]
    return out


def resolve_effort_for_model(
    model: str | None,
    slot_overrides: dict[str, str] | None = None,
    defaults: object = None,
) -> str | None:
    """Resolve the effort level for *model* using the priority chain.

    Priority: ``slot_overrides[model]`` → ``defaults[model]`` → ``None``.
    Returns ``None`` when the model does not support effort or no level
    resolves (caller should then leave the provider on its own default).
    """
    if not model_supports_effort(model):
        return None
    assert model is not None  # narrowed by model_supports_effort
    if slot_overrides:
        lvl = slot_overrides.get(model)
        if is_valid_effort(lvl):
            return lvl
    coerced = _coerce_defaults(defaults)
    lvl = coerced.get(model)
    if is_valid_effort(lvl):
        return lvl
    return None
