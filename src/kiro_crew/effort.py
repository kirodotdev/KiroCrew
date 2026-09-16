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
import re

from kiro_crew import model_registry

logger = logging.getLogger(__name__)

# Concrete effort levels, ordered low→high. GPT-5.6 additionally accepts
# ``"none"`` (a real kiro-cli /effort value, distinct from the empty-string
# sentinel below). ``""`` is NOT a level: it clears an override and delegates
# to the provider/model default. Per-model filtering keeps ``none`` out of
# Claude's picker. ``xhigh`` sits between ``high`` and ``max``.
EFFORT_LEVELS: tuple[str, ...] = ("none", "low", "medium", "high", "xhigh", "max")

# The levels each kiro-cli model accepts, as probed over ACP ``/effort`` against
# kiro-cli 2.21.4. Mirrors ``website/src/lib/effort.ts`` — the two
# tables must agree so the slider offers exactly what the wire accepts.
#
#  - Opus 5 / 4.8 / 4.7, Sonnet 5, Fable 5 / 5.1: low, medium, high, xhigh, max
#  - Sonnet 4.6: low, medium, high, max (no xhigh)
#  - GPT-5.6 sol / terra / luna: none, low, medium, high, xhigh, max
#  - Opus 4.5, Sonnet 4 / 4.5, Haiku, auto, deepseek, minimax, glm, qwen, nova:
#    ``/effort`` is rejected outright
#
# Keyed on kiro-cli's own id spelling (``claude-sonnet-4.6``, ``gpt-5.6-sol``).
# Provider-prefixed ids (``global.anthropic.claude-...[1m]``) belong to the
# config-option harnesses, which negotiate levels at runtime, so they are
# deliberately UNKNOWN here.
_LEVELS_CLAUDE: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
_LEVELS_GPT: tuple[str, ...] = ("none", "low", "medium", "high", "xhigh", "max")
_LEVELS_SONNET_46: tuple[str, ...] = ("low", "medium", "high", "max")
_REJECTS_EFFORT: tuple[str, ...] = ()

_KIRO_CLAUDE_ID = re.compile(r"^claude-(opus|sonnet|fable|haiku)-(\d+)(?:\.(\d+))?$")
_KIRO_GPT_ID = re.compile(r"^gpt-\d")
_KIRO_REJECTS_ID = re.compile(r"^(auto|deepseek|minimax|glm|qwen|nova)(?:$|[-.\d])")


def effort_levels_for_model(model: str | None) -> tuple[str, ...] | None:
    """The effort levels kiro-cli accepts for *model*, from the probed table.

    Returns the accepted levels low→high, an EMPTY tuple for a model kiro-cli
    is known to reject effort on, and ``None`` for an id the table does not
    know (a provider-prefixed spelling or a family not yet probed) so the
    caller falls back to its runtime answer.
    """
    # Anything that is not an id string (None, a test double standing in for
    # the client) is unknown to the table, not an error.
    if not isinstance(model, str) or not model:
        return None
    m = model.lower()
    if _KIRO_REJECTS_ID.match(m):
        return _REJECTS_EFFORT
    if _KIRO_GPT_ID.match(m):
        return _LEVELS_GPT
    matched = _KIRO_CLAUDE_ID.match(m)
    if matched is None:
        return None
    family = matched.group(1)
    major = int(matched.group(2))
    minor = int(matched.group(3) or 0)
    if family == "haiku":
        return _REJECTS_EFFORT
    if family == "fable":
        return _LEVELS_CLAUDE
    if family == "sonnet":
        if major == 4 and minor == 6:
            return _LEVELS_SONNET_46
        if major == 4:
            return _REJECTS_EFFORT  # Sonnet 4 and 4.5 reject effort
        return _LEVELS_CLAUDE
    if major == 4 and minor == 5:
        return _REJECTS_EFFORT  # Opus 4.5 rejects effort
    return _LEVELS_CLAUDE


def nearest_supported_effort(requested: str, levels: tuple[str, ...]) -> str:
    """The level a model will actually run at when asked for *requested*.

    Same level when the model accepts it; otherwise the nearest LOWER level it
    does accept (Sonnet 4.6 asked for ``xhigh`` runs ``high``), never a higher
    one. Only when nothing lower exists (``none`` on a Claude model) does it
    step up to the lowest accepted level. ``""`` (no request) stays ``""``.
    Mirrors ``nearestSupportedEffort`` in ``website/src/lib/effort.ts``.
    """
    if not requested:
        return ""
    if requested in levels:
        return requested
    if requested not in EFFORT_LEVELS:
        # A level this vocabulary cannot rank has no "nearest lower".
        return ""
    at = EFFORT_LEVELS.index(requested)
    for candidate in reversed(EFFORT_LEVELS[:at]):
        if candidate in levels:
            return candidate
    for candidate in EFFORT_LEVELS[at + 1 :]:
        if candidate in levels:
            return candidate
    return ""


def effort_for_model(model: str | None, level: str) -> str:
    """*level* clamped to what kiro-cli's *model* accepts, per the probed table.

    The stored intent (a slot override, a crew pin, the Settings default) is a
    level NAME shared across models; this is the value to put on the wire for
    the model in front of it. Unchanged when the table does not know the model
    (the runtime answer then decides) or when *level* is the empty sentinel.
    """
    if not level:
        return level
    levels = effort_levels_for_model(model)
    if not levels:
        return level
    return nearest_supported_effort(level, levels)


# Accepted by the API/persistence layer: the concrete levels plus the empty
# sentinel for "provider default".  Single source for ``_REASONING_EFFORT_VALUES``.
EFFORT_VALUES: frozenset[str] = frozenset({""} | set(EFFORT_LEVELS))


def is_valid_effort(level: object) -> bool:
    """True if *level* is one of the concrete effort levels (excludes "")."""
    return isinstance(level, str) and level in EFFORT_LEVELS


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
    # The probed table answers first for kiro-cli spellings: the versions kiro
    # rejects (Sonnet 4 / 4.5, Opus 4.5) must not be reported capable by the
    # family heuristic below, or the dashboard offers a level the wire refuses.
    known = effort_levels_for_model(model)
    if known is not None:
        return bool(known)
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
