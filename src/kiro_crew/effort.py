"""Shared reasoning-effort vocabulary for all LLM providers.

Both kiro-cli and claude-agent-acp expose a per-session "effort" (a.k.a.
thinking depth) knob, but only on reasoning-capable models — Claude
Fable/Opus/Sonnet and the recent GPT-5.x models.  This module is the single
source of truth for the valid levels and the model-capability check so the
CLI, dashboard handlers, providers, and config loader all agree.

Stdlib-only and import-light on purpose — it is imported from hot paths
(``providers/acp.py``, ``dashboard/chat_handlers.py``) and must not create
import cycles.

References:
- kiro-cli ``/effort`` (verified against 2.12/2.13 over ACP): levels
  ``low|medium|high|xhigh|max``. Available on Claude Opus/Sonnet/Fable AND the
  GPT-5.x models (``gpt-5.6-sol|terra|luna`` etc.) — kiro applies effort to any
  model that declares it, and rejects it with "Effort configuration is
  currently not available on <model>" for those that don't (deepseek, minimax,
  glm, qwen, auto). Per-model defaults live in ``~/.kiro/settings/cli.json`` →
  ``chat.modelDefaults.<model>.<key>.effort`` where ``<key>`` is
  ``output_config`` for Claude models and ``reasoning`` for GPT models.
- claude-agent-acp ``buildConfigOptions``: effort options come from each
  model's ``supportedEffortLevels``; recommended default ``xhigh`` then ``high``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

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


def select_effort_level(requested: str, supported: Iterable[str]) -> str | None:
    """Highest advertised level no higher than *requested*.

    Shared arithmetic for every consumer that can READ a backend's advertised
    effort levels before sending one (the Knowledge pool workers read
    ``configOptions``; the chat-side trial ladder in ``providers/acp.py`` uses
    write-and-see instead and keeps its own loop, but agrees on this ordering).

    Entries in *supported* that are not concrete levels are ignored; an empty
    (or all-invalid) list is treated like a lazy backend that advertises
    nothing — the requested value is returned so ACP can confirm or reject it.
    ``None`` means no advertised level is at or below *requested* and the
    caller should stay on the provider default.
    """
    supported_levels = {
        level for level in supported if isinstance(level, str) and is_valid_effort(level)
    }
    if not supported_levels:
        return requested
    requested_index = EFFORT_LEVELS.index(requested)
    eligible = [
        level
        for level in EFFORT_LEVELS
        if level in supported_levels and EFFORT_LEVELS.index(level) <= requested_index
    ]
    return eligible[-1] if eligible else None


def model_supports_effort(model: str | None) -> bool:
    """True when *model* accepts a reasoning-effort level.

    Effort is available on Claude Fable/Opus/Sonnet and the GPT-5.x models
    (verified against kiro-cli 2.12/2.13 over ACP).  Haiku, Nova, and the other
    third-party models (deepseek, minimax, glm, qwen) do not support it;
    ``"auto"``/``None`` cannot either (kiro-cli errors "Effort configuration is
    currently not available on auto" until a concrete model is selected).

    Matches both naming conventions: kiro-cli (``claude-fable-5``,
    ``gpt-5.6-sol``) and the Bedrock/claude-agent-acp form
    (``global.anthropic.claude-fable-5[1m]``).

    Prefers the registry's declared ``supports_effort`` flag when the model is in
    the registry, so a future model whose canonical key lacks a known-capable
    substring (or a capable model the heuristic would miss) is honored; falls
    back to the substring heuristic for ids the registry doesn't list.  The
    heuristic is a conservative allowlist of known-capable families rather than a
    "non-Claude means unsupported" denylist — an unverified new family lands as
    unsupported (safe: the slider hides) until confirmed.
    """
    if not model:
        return False
    m = model.lower()
    # Haiku NEVER supports effort — a hard rule that must win even over the
    # registry. On the acp path a kiro Haiku id (``claude-haiku-4.5``) has its
    # own canonical entry (``haiku-4.5``) with no ``supports_effort`` flag; on
    # the claude_code path it is an ALIAS of Sonnet-4.6 (the cheapest valid fold,
    # which IS effort-capable) — but that fold happens at the translation
    # boundary (config.loader factory), so the value reaching here is the Sonnet
    # provider id (no "haiku" substring) and stays capable. Only the raw kiro
    # spelling — which the kiro/acp path passes untranslated — is gated here, so
    # a kiro Haiku agent can never wrongly report effort-capable.
    if "haiku" in m:
        return False
    try:
        declared = model_registry.supports_effort(model)
        if declared is not None:
            return declared
    except Exception:
        pass  # fall back to the heuristic
    return "opus" in m or "sonnet" in m or "fable" in m or "gpt" in m


# kiro-cli persists per-model effort under a family-specific sub-key in
# ``chat.modelDefaults.<model>``: Claude models use ``output_config``, GPT
# models use ``reasoning`` (verified against ~/.kiro/settings/cli.json written
# by kiro 2.13).  Writing the wrong key is silently ignored by kiro, so a
# GPT model's effort would survive a live ``/effort`` push but be dropped on the
# next spawn.  The overlay helpers in ``providers/acp.py`` resolve the key here.
_EFFORT_KEY_GPT = "reasoning"
_EFFORT_KEY_DEFAULT = "output_config"


def effort_settings_key(model: str | None) -> str:
    """Return the cli.json sub-key kiro uses for *model*'s effort default.

    ``"reasoning"`` for GPT models, ``"output_config"`` for everything else
    (Claude Opus/Sonnet/Fable).  Used by the kiro workspace overlay so the
    written key matches what kiro-cli reads back at spawn.
    """
    if model and "gpt" in model.lower():
        return _EFFORT_KEY_GPT
    return _EFFORT_KEY_DEFAULT


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
