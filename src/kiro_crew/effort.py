"""Shared reasoning-effort vocabulary for all LLM providers.

Both kiro-cli and claude-agent-acp expose a per-session "effort" (a.k.a.
thinking depth) knob, but only on reasoning-capable models — Claude
Fable/Opus/Sonnet, Claude Haiku 5.5 and later, and the recent GPT-5.x models.  This module is the single
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
from collections.abc import Callable, Sequence

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


# The first Haiku release that accepts an effort level. kiro-cli 2.31.1 answers
# ``/effort`` on ``claude-haiku-5.5`` with "Effort set to <level>" for all five
# levels and advertises ``effortLevels`` for it in ``_kiro.dev/metadata``, while
# ``claude-haiku-4.5`` still gets "Effort configuration is currently not
# available". A Haiku id with no readable version stays refused.
HAIKU_EFFORT_MIN_VERSION: tuple[int, int] = (5, 5)

# ``haiku-5.5``, ``haiku-5-5`` (Bedrock), ``haiku-6``. One or two digits per
# part, so a Bedrock date suffix (``claude-3-5-haiku-20241022``) is never read
# as a version.
_HAIKU_VERSION_RE = re.compile(r"haiku[-_.]?(\d{1,2})(?:[-_.](\d{1,2}))?(?!\d)")


def haiku_supports_effort(model: str) -> bool:
    """True when the Haiku id *model* is version 5.5 or later.

    Reads the version that follows ``haiku`` in either naming convention
    (``claude-haiku-5.5``, ``global.anthropic.claude-haiku-5-5``); a missing
    minor counts as ``0``. An id whose version cannot be read answers False, so
    an unknown Haiku keeps the slider hidden.
    """
    match = _HAIKU_VERSION_RE.search(model.lower())
    if match is None:
        return False
    version = (int(match.group(1)), int(match.group(2) or 0))
    return version >= HAIKU_EFFORT_MIN_VERSION


def model_supports_effort(model: str | None) -> bool:
    """True when *model* accepts a reasoning-effort level.

    Effort is available on Claude Fable/Opus/Sonnet, Claude Haiku 5.5 and later,
    and the GPT-5.x models (verified against kiro-cli over ACP).  Haiku before
    5.5, Nova, and the other third-party models (deepseek, minimax, glm, qwen)
    do not support it;
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
    # Haiku is decided by its version alone, a hard rule that must win even over
    # the registry. On the acp path a kiro Haiku id (``claude-haiku-4.5``) has
    # its own canonical entry (``haiku-4.5``) with no ``supports_effort`` flag; on
    # the claude_code path it is an ALIAS of Sonnet-4.6 (the cheapest valid fold,
    # which IS effort-capable), so asking the registry would hand an old Haiku
    # Sonnet's flag. That fold happens at the translation boundary
    # (config.loader factory), so the value reaching here on that path is the
    # Sonnet provider id (no "haiku" substring) and stays capable. Only the raw
    # kiro spelling, which the kiro/acp path passes untranslated, is gated here.
    if "haiku" in m:
        return haiku_supports_effort(m)
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
# next spawn.  The overlay helpers in ``providers/acp_cli_overlay.py`` resolve the key here.
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


def _coerce_defaults(
    defaults: object,
    accepts: Callable[[object], bool] = is_valid_effort,
    normalize: Callable[[str], str] | None = None,
) -> dict[str, str]:
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
        if not isinstance(model, str):
            continue
        if normalize is not None and isinstance(level, str):
            level = normalize(level)
        if accepts(level):
            out[model] = level  # type: ignore[assignment]
    return out


def resolve_effort_for_model(
    model: str | None,
    slot_overrides: dict[str, str] | None = None,
    defaults: object = None,
    levels: Sequence[str] | None = None,
    normalize: Callable[[str], str] | None = None,
) -> str | None:
    """Resolve the effort level for *model* using the priority chain.

    Priority: ``slot_overrides[model]`` → ``defaults[model]`` → ``None``.
    Returns ``None`` when the model does not support effort or no level
    resolves (caller should then leave the provider on its own default).

    *levels* is the vocabulary a harness ADVERTISED, passed by the callers whose
    harness is the authority on it rather than this module's registry (see
    ``ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION``). Given, it answers both
    questions for that session: a non-empty list means the session takes a level,
    and membership in it means this level may be written. Omitted -- every
    existing caller -- the model registry answers both exactly as before, which
    is right where the level rides the model instead of the session.

    Asking the registry on a harness that owns its vocabulary drops the level
    twice over: the capability check answers False for a model id the registry
    does not carry, and the validity check rejects a level Crew's own ladder does
    not list -- so a persisted pick the dashboard offered and stored is silently
    not applied, and the session runs a level the UI does not report.

    *normalize* is the per-harness spelling of a level, applied BEFORE the check
    in either mode and to overrides and defaults alike. The order is the whole
    point: a stored level that the WRITE path folds onto one the harness does
    advertise must not be rejected here for the spelling it was stored under, or
    a live change accepted this session is dropped by the next cold start.
    """
    if levels is None:
        if not model_supports_effort(model):
            return None
        accepts: Callable[[object], bool] = is_valid_effort
    else:
        if not levels:
            return None
        allowed = frozenset(levels)

        def accepts(level: object) -> bool:
            return isinstance(level, str) and level in allowed

    if not model:
        return None
    if slot_overrides:
        lvl: object = slot_overrides.get(model)
        if normalize is not None and isinstance(lvl, str):
            lvl = normalize(lvl)
        if accepts(lvl):
            return lvl  # type: ignore[return-value]
    coerced = _coerce_defaults(defaults, accepts, normalize)
    lvl = coerced.get(model)
    if accepts(lvl):
        return lvl
    return None
