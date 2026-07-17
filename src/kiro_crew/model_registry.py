"""Canonical model registry — single source of truth for model translation.

Canonical keys (e.g. ``opus-4.8-1m``) are versioned+capability identifiers used
on the wire (frontend <-> API) and in persisted ``agent.cc_model``. This module
translates canonical -> per-provider id and looks up context windows. The same
data file (``model_registry.json``) is imported by the frontend so both sides
agree without an API round-trip.

Translation boundary: canonical->provider-id happens once at the
``config.loader._claude_code`` factory; everything below uses provider ids.

Lookups are O(1): the immutable registry is indexed into precomputed dicts once
at import (canonical/alias/provider-id -> canonical key, canonical -> provider
id, canonical -> window), so the per-session / per-token-record hot paths never
linear-scan.

Unknown-handling contract: translation is identity-preserving for values the
registry does not list — ``to_provider_id`` and ``from_provider_id`` return an
unrecognized input UNCHANGED (we never rewrite an operator's explicit id). The
ONE exception is ``window()``, which degrades an unlisted ``[1m]``/``-1m`` id to
the 1M window via heuristic, for forward-compat parity with the frontend
``contextWindow``.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REGISTRY_FILE = Path(__file__).resolve().parent / "model_registry.json"

# Hardcoded last-resort default so a corrupt/missing registry can't brick the
# claude_code provider. _FALLBACK_CANONICAL is the canonical key default()
# returns when the registry didn't load. _FALLBACK_PROVIDER_IDS maps every
# known claude_code canonical key AND alias to its valid Bedrock provider id,
# so to_provider_id can rescue ANY persisted cc_model (not just the flagship
# default) when the index is empty — otherwise a bare canonical key like
# "sonnet-4.6-1m" would reach the adapter/Bedrock as an invalid model id
# (-32603 / 400). Mirror model_registry.json's claude_code provider ids +
# aliases; only consulted on the corrupt/missing-registry path.
_FALLBACK_CANONICAL = "opus-4.8-1m"
_FALLBACK_PROVIDER_ID = "global.anthropic.claude-opus-4-8[1m]"
_FALLBACK_PROVIDER_IDS: dict[str, str] = {
    "fable-5-1m": "global.anthropic.claude-fable-5[1m]",
    "fable": "global.anthropic.claude-fable-5[1m]",
    "fable-5": "global.anthropic.claude-fable-5[1m]",
    "claude-fable-5": "global.anthropic.claude-fable-5[1m]",
    "opus-4.8-1m": "global.anthropic.claude-opus-4-8[1m]",
    "opus": "global.anthropic.claude-opus-4-8[1m]",
    "claude-opus-4.8": "global.anthropic.claude-opus-4-8[1m]",
    "claude-opus-4-8[1m]": "global.anthropic.claude-opus-4-8[1m]",
    "claude-opus-4.6": "global.anthropic.claude-opus-4-8[1m]",
    "claude-opus-4.6-1m": "global.anthropic.claude-opus-4-8[1m]",
    "opus-4.8": "global.anthropic.claude-opus-4-8",
    "claude-opus-4-8": "global.anthropic.claude-opus-4-8",
    "claude-opus-4.5": "global.anthropic.claude-opus-4-8",
    "opus-4.7-1m": "global.anthropic.claude-opus-4-7[1m]",
    "claude-opus-4.7": "global.anthropic.claude-opus-4-7[1m]",
    "claude-opus-4.7-1m": "global.anthropic.claude-opus-4-7[1m]",
    "claude-opus-4-7[1m]": "global.anthropic.claude-opus-4-7[1m]",
    "sonnet-4.6-1m": "global.anthropic.claude-sonnet-4-6[1m]",
    "sonnet": "global.anthropic.claude-sonnet-4-6[1m]",
    "claude-sonnet-4.6": "global.anthropic.claude-sonnet-4-6[1m]",
    "claude-sonnet-4.6-1m": "global.anthropic.claude-sonnet-4-6[1m]",
    "claude-sonnet-4-6[1m]": "global.anthropic.claude-sonnet-4-6[1m]",
    "claude-sonnet-4.5": "global.anthropic.claude-sonnet-4-6[1m]",
    "claude-sonnet-4.5-1m": "global.anthropic.claude-sonnet-4-6[1m]",
    "claude-sonnet-4": "global.anthropic.claude-sonnet-4-6[1m]",
    "claude-haiku-4.5": "global.anthropic.claude-sonnet-4-6[1m]",
    "auto": "",
}

_REGISTRY: dict[str, dict[str, Any]] = {}
try:
    with open(_REGISTRY_FILE, encoding="utf-8") as _f:
        _REGISTRY = {k: v for k, v in json.load(_f).items() if not k.startswith("_")}
except (OSError, ValueError):  # pragma: no cover - corrupt registry
    logger.warning("Could not load model_registry.json; using fallback default", exc_info=True)


# ── Precomputed indices (built once; the registry is immutable after import) ──
# canonical key / alias / per-provider id  ->  canonical key, keyed by provider.
_CANONICAL_INDEX: dict[str, dict[str, str]] = {}
# canonical key -> default flag, for cheap default resolution per provider.
_DEFAULTS: dict[str, str] = {}


def _build_indices() -> None:
    """(Re)build the lookup indices from ``_REGISTRY``. Idempotent."""
    _CANONICAL_INDEX.clear()
    _DEFAULTS.clear()
    for key, entry in _REGISTRY.items():
        for provider, pid in entry.get("providers", {}).items():
            idx = _CANONICAL_INDEX.setdefault(provider, {})
            idx[key] = key  # canonical key resolves to itself
            if pid:
                idx[pid] = key  # provider id -> canonical
            for alias in entry.get("aliases", []):
                idx.setdefault(alias, key)  # alias -> canonical (first wins)
            if entry.get("default"):
                _DEFAULTS.setdefault(provider, key)


_build_indices()


def _resolve_canonical(canonical_or_id: str, provider: str) -> str | None:
    """Resolve a canonical key, alias, or provider id to its canonical key.

    Returns None if the value matches nothing in the registry for ``provider``.
    """
    return _CANONICAL_INDEX.get(provider, {}).get(canonical_or_id)


def to_provider_id(canonical_or_id: str, provider: str) -> str:
    """Translate a canonical key (or alias / known provider id) to a provider id.

    - Known canonical key or alias -> its provider id (``""`` for ``auto``).
    - A value already equal to a registry provider id -> itself.
    - A kiro dotted id (e.g. ``claude-opus-4.6``) listed in an entry's
      ``aliases`` -> that entry's provider id.
    - ``""`` -> ``""`` (means "no override / let the backend pick").
    - Any OTHER unrecognized value (a real-but-unregistered Bedrock id, e.g. a
      regional ``us.anthropic.…`` profile or a future model) -> passed through
      UNCHANGED. We never silently rewrite an operator's explicit id to the
      flagship default; an unknown bare alias is the caller's responsibility.
      (The empty/unset case is handled upstream in the factory, which falls back
      to the registry default before calling this — so "" here only ever means
      an explicit Auto.)
    """
    if canonical_or_id == "":
        return ""
    key = _resolve_canonical(canonical_or_id, provider)
    if key is not None:
        return _REGISTRY[key].get("providers", {}).get(provider, "")
    # Corrupt/missing registry: the index is empty, so NO canonical key resolves
    # above. Rescue every known claude_code canonical key/alias to its paired
    # valid provider id from the hardcoded fallback table, rather than passing
    # the bare canonical key through to the adapter/Bedrock (which would reject
    # it with -32603/400). This keeps the "a corrupt registry can't brick the
    # provider" guarantee for any persisted cc_model, not just the flagship
    # default. Only used when _REGISTRY failed to load (normally unreachable).
    if provider == "claude_code":
        rescued = _FALLBACK_PROVIDER_IDS.get(canonical_or_id)
        if rescued is not None:
            return rescued
    # Unrecognized: pass through unchanged rather than clobbering an explicit
    # choice. Log once so an unexpected value is still diagnosable.
    logger.debug(
        "Model %r not in registry for provider %s; passing through", canonical_or_id, provider
    )
    return canonical_or_id


def from_provider_id(provider_id: str, provider: str) -> str:
    """Reverse lookup: provider id -> canonical key (``provider_id`` if unknown).

    ``""`` maps to ``""`` (NOT to the ``auto`` canonical key): an empty/unset
    provider id means "no model", not "Auto".
    """
    if provider_id == "":
        return ""
    key = _CANONICAL_INDEX.get(provider, {}).get(provider_id)
    return key if key is not None else provider_id


def window(canonical_or_id: str) -> int:
    """Context window tokens for a canonical key, alias, or provider id.

    Falls back to the ``[1m]``/``-1m`` heuristic for an unlisted id (parity with
    the frontend ``contextWindow``), then 200k.
    """
    key = _resolve_canonical(canonical_or_id, _WINDOW_INDEX)
    if key is not None:
        return int(_REGISTRY[key].get("window", 200_000))
    # Forward-compat: an unlisted [1m]/-1m id still resolves to the 1M window
    # (mirrors KiroCrewWebsite/src/providers/modelRegistry.ts contextWindow).
    lowered = canonical_or_id.lower()
    if "[1m]" in lowered or _has_1m_token(lowered):
        return 1_000_000
    return 200_000


def _has_1m_token(lowered: str) -> bool:
    """True if ``lowered`` contains a standalone ``1m`` token (not ``10m`` etc.)."""
    return re.search(r"(^|[^a-z0-9])1m([^a-z0-9]|$)", lowered) is not None


# The index that carries per-model window sizes. A context window is a property
# of the MODEL, not the provider serving it (Opus 4.8 is 200K whether reached
# via kiro-cli/``acp`` or ``claude_code``), and only this one index is populated
# in model_registry.json — its ``aliases`` already include every kiro/acp-
# advertised id (dotted ``claude-opus-4.8``, bare ``claude-opus-4-8[1m]``, …).
# ``window()`` resolves against it too, so window membership must use the same
# index. Named for the registry key, NOT because windows are claude_code-only.
_WINDOW_INDEX = "claude_code"


def has_known_window(canonical_or_id: str) -> bool:
    """True if ``canonical_or_id`` is a model the registry has a window for.

    Pairs with :func:`window`: it lets a caller tell a genuinely-known model
    apart from an unlisted id that ``window()`` would silently default to 200k —
    the distinction the context-budget scaler needs so it never shrinks an
    unknown model's budget on a 200k assumption. Provider-independent by design
    (see ``_WINDOW_INDEX``); resolves against the same index ``window()`` reads,
    so the membership check and the window value can never disagree. Works for
    kiro/acp model ids (the default provider) — they are registry aliases.
    """
    return _resolve_canonical(canonical_or_id, _WINDOW_INDEX) is not None


def available_models(provider: str) -> list[str]:
    """Non-empty provider ids for ``provider`` (the settings.json allowlist).

    Default-first (like ``display_list``), so the id the claude-agent-acp adapter
    picks when no explicit model is written — ``resolveModelPreference()`` takes
    the first entry of ``(SDK list ∩ availableModels)``, which happens on the
    ``auto`` path where ``settings.local.json`` omits the ``model`` key — is the
    registry default, not whichever entry happens to be first in the JSON.
    """
    out: list[str] = []
    items = sorted(_REGISTRY.items(), key=lambda kv: (not kv[1].get("default"), 0))
    for _key, entry in items:
        pid = entry.get("providers", {}).get(provider)
        if pid:
            out.append(pid)
    return out


def default(provider: str) -> str:
    """Canonical key of the provider's default model (registry fallback if none)."""
    return _DEFAULTS.get(provider, _FALLBACK_CANONICAL)


def canonicalize_for_provider(stored_model: str, provider: str) -> str:
    """Map a stored model string to its canonical registry key — but ONLY for
    ``claude_code``, where the wire/dropdown values are canonical keys.

    Single home for the "canonicalize a persisted/advertised model iff it's a
    claude_code value" rule (previously open-coded with ad-hoc provider gates in
    usage.py, chat_persistence, and chat_runner). For any other provider the
    value is returned unchanged, so a kiro/acp model that happens to share a
    registry alias spelling is never rewritten. ``from_provider_id`` resolves
    canonical keys, provider ids, AND aliases, so a bare ``opus`` or a
    ``global.anthropic.…`` id both collapse to the canonical key.
    """
    if not stored_model or provider != "claude_code":
        return stored_model
    return from_provider_id(stored_model, provider)


def supports_effort(canonical_or_id: str) -> bool | None:
    """Registry-declared effort support for a model, or None if not declared.

    Callers fall back to their own heuristic when this returns None (the registry
    only declares the flag on some entries).
    """
    key = _resolve_canonical(canonical_or_id, "claude_code")
    if key is None:
        return None
    val = _REGISTRY[key].get("supports_effort")
    return bool(val) if val is not None else None


def display_list(provider: str) -> list[dict[str, str]]:
    """Dropdown rows ``{model_name(canonical), display_name, description}``.

    Default first, then declared order. Only entries that support ``provider``.
    """
    rows: list[dict[str, str]] = []
    items = sorted(_REGISTRY.items(), key=lambda kv: (not kv[1].get("default"), 0))
    for key, entry in items:
        if provider not in entry.get("providers", {}):
            continue
        rows.append(
            {
                "model_name": key,
                "display_name": str(entry.get("display", key)),
                "description": str(entry.get("description", "")),
            }
        )
    return rows
