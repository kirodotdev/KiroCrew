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
unrecognized input UNCHANGED (we never rewrite an operator's explicit id).

Context windows: :func:`model_window` is the SINGLE authority every consumer
(frontend via ``/api/models``, the context/memory budget scaler, the ACP
backfill, the live meter) resolves through. Its fallback order is live
``usage_update.size`` > kiro ``--list-models`` cache (:func:`refresh_kiro_windows`,
persisted) > static registry ``window`` literal > ``[1m]`` heuristic > ``None``.
There is no silent 200k default: a genuinely-unknown window returns ``None`` and
callers substitute :data:`REFERENCE_WINDOW_TOKENS` (1M), so an unknown model is
never wrongly shrunk. Auto-compaction is percentage-driven and does NOT read
this — it must stay that way.
"""

from __future__ import annotations

import json
import logging
import os
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


# ── Kiro-list window cache (the authoritative per-model window source) ────────
# The committed registry (``model_registry.json``) is a hand-maintained fallback
# that only covers Anthropic models and drifts from what kiro-cli actually
# serves (e.g. the sonnet/haiku aliases fold onto a 1M canonical though kiro
# serves them at 200K; GPT/DeepSeek/Qwen are absent entirely). kiro-cli's
# ``chat --list-models --format json`` reports a STRUCTURED ``context_window_tokens``
# per model — the ground truth. ``refresh_kiro_windows`` ingests those rows into
# this cache (keyed by kiro model id / model_name), and ``model_window`` consults
# it BEFORE the static registry. The cache is persisted to disk so a cold start
# (before any ``--list-models`` call) still has last-known real windows.
#
# This is runtime state (like session_map), NOT committed data: the registry
# JSON stays read-only. A corrupt/missing cache degrades silently to the
# registry + heuristic — it can never brick import or override a live
# ``usage_update.size``.
_KIRO_WINDOWS: dict[str, int] = {}

# Supplementary static windows for models the canonical registry does not carry
# and kiro-cli does not advertise — chiefly fully-qualified provider model ids
# and legacy Claude snapshots. These formerly lived in a SEPARATE
# ``model_tokens.json`` file (a second, drifting source of truth this
# centralization retires). Folded here so ``model_window`` is the ONE
# authority. Consulted AFTER the canonical registry (so a canonical/alias hit
# wins) but BEFORE the ``[1m]`` heuristic. Keys are matched exactly first, then
# by longest-substring (these ids embed the dotted model name).
_SUPPLEMENTARY_WINDOWS: dict[str, int] = {
    # Fully-qualified inference-profile + on-demand model ids.
    "anthropic.claude-sonnet-4-20250514-v1:0": 200_000,
    "anthropic.claude-sonnet-4-20250514": 200_000,
    "anthropic.claude-3-7-sonnet-20250219-v1:0": 200_000,
    "anthropic.claude-3-5-sonnet-20241022-v2:0": 200_000,
    "us.anthropic.claude-sonnet-4-20250514-v1:0": 200_000,
    "us.anthropic.claude-3-7-sonnet-20250219-v1:0": 200_000,
    "claude-sonnet-4-20250514": 200_000,
    "claude-3-7-sonnet-20250219": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
    "amazon.nova-pro-v1:0": 300_000,
    "amazon.nova-lite-v1:0": 300_000,
    # Non-Anthropic models kiro-cli may serve that are neither in the canonical
    # registry nor advertise a [1m] marker. The kiro --list-models cache
    # (refresh_kiro_windows) is authoritative and overrides these when present,
    # but it is only seeded once /api/models runs — so a headless start (Slack,
    # cron) that never hits that endpoint would otherwise resolve these to None
    # ⇒ the 1M reference and over-assemble context. Keeping the static window as
    # a floor prevents that over-large-prompt regression on first/headless runs.
    "deepseek-3.2": 128_000,
    "kimi-k2.5": 256_000,
    "minimax-m2.1": 200_000,
    "glm-4.7": 200_000,
    "glm-4.7-flash": 128_000,
    "qwen3-coder-next": 256_000,
    "qwen3-coder-480b": 256_000,
}


def _kiro_windows_cache_path() -> Path:
    """Path to the persisted kiro-window sidecar under KIROCREW_HOME.

    Resolved lazily (not at import) so tests / KIROCREW_HOME overrides are
    honoured, and so a home-resolution failure never breaks module import.
    """
    home = os.environ.get("KIROCREW_HOME")
    base = Path(home) if home else Path.home() / ".kirocrew"
    return base / "model_windows.json"


def _load_kiro_windows() -> None:
    """Load the persisted kiro-window cache into ``_KIRO_WINDOWS`` (best-effort).

    Called once at import. A missing file is normal (first run); a corrupt file
    is logged and ignored (degrade to registry + heuristic), never raised.
    """
    try:
        path = _kiro_windows_cache_path()
        if not path.is_file():
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for mid, win in data.items():
                if isinstance(mid, str) and isinstance(win, int) and win > 0:
                    _KIRO_WINDOWS[mid] = win
    except (OSError, ValueError, TypeError):  # pragma: no cover - corrupt/absent cache
        logger.debug("kiro window cache unreadable; using registry fallback", exc_info=True)


_load_kiro_windows()


def refresh_kiro_windows(rows: list[dict[str, Any]]) -> bool:
    """Ingest ``kiro-cli chat --list-models --format json`` rows into the cache.

    Each row carries a structured ``context_window_tokens`` (the authoritative
    per-model window) keyed by ``model_id`` / ``model_name``. We index BOTH so a
    lookup by either spelling hits. A single malformed row is skipped, never
    fatal.

    This does ONLY the in-memory dict update — cheap and non-blocking, so it is
    safe to call directly from an async handler and the cache is immediately
    consistent for callers on the same tick. It does NOT touch disk. Returns
    ``True`` when the cache changed (a persist is warranted): the async caller
    should then offload :func:`persist_kiro_windows` to an executor rather than
    block the event loop on filesystem I/O (no blocking call on the event loop).
    Synchronous callers can call ``persist_kiro_windows()`` directly.
    """
    updated = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        win = row.get("context_window_tokens")
        if not isinstance(win, int) or isinstance(win, bool) or win <= 0:
            continue
        for key in (row.get("model_id"), row.get("model_name")):
            if isinstance(key, str) and key:
                if _KIRO_WINDOWS.get(key) != win:
                    _KIRO_WINDOWS[key] = win
                    updated = True
    return updated


def persist_kiro_windows() -> None:
    """Write the in-memory kiro-window cache to disk (best-effort, blocking I/O).

    Separated from :func:`refresh_kiro_windows` so an async caller can offload
    ONLY this filesystem step to an executor while keeping the in-memory update
    synchronous. Atomic (tmp + ``os.replace``); a persist failure is logged, not
    raised — the in-memory cache is authoritative for this process either way.

    Thread-safety: this runs on an executor thread while ``refresh_kiro_windows``
    mutates ``_KIRO_WINDOWS`` on the event-loop thread. Snapshot with ``dict(...)``
    (a C-level copy that does not release the GIL) BEFORE serializing, so
    ``json.dump`` cannot hit ``RuntimeError: dictionary changed size during
    iteration`` from a concurrent add/remove.
    """
    try:
        snapshot = dict(_KIRO_WINDOWS)  # atomic under the GIL; safe vs. concurrent mutation
        path = _kiro_windows_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f)
        tmp.replace(path)
    except OSError:  # pragma: no cover - disk full / perms
        logger.debug("Could not persist kiro window cache", exc_info=True)


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


def to_acp_id(canonical_or_id: str) -> str:
    """Translate a value to a kiro-cli (``acp`` provider) model id.

    UNLIKE :func:`to_provider_id`, this resolves ONLY canonical registry keys
    (e.g. ``opus-4.8-1m`` -> ``claude-opus-4.8``, ``auto`` -> ``""``). Everything
    else — kiro-cli's own bare dotted ids AND the registry aliases that spell
    them (``claude-haiku-4.5``, ``claude-sonnet-4.5``, ``claude-sonnet-4``,
    ``claude-opus-4.6``, …) — is passed through UNCHANGED.

    This matters because those aliases exist only to fold *claude-agent-acp*'s
    advertised ids onto a canonical key for dropdown dedup, and on the claude_code
    path ``to_provider_id`` deliberately downgrades them (the claude backend has
    no Haiku, so ``claude-haiku-4.5`` -> Sonnet). But kiro-cli serves every one of
    those as a DISTINCT real model (verified via ``kiro-cli chat --list-models``),
    so resolving the alias here would silently run e.g. the Haiku-pinned
    ``kirocrew-knowledge`` agent on Sonnet. Only the canonical keys (which kiro
    cannot parse) need translating; kiro's native ids are already valid.
    """
    if canonical_or_id == "":
        return ""
    # Only a top-level canonical key gets translated; an alias/native id/unknown
    # value is left as-is (kiro accepts its own ids verbatim).
    entry = _REGISTRY.get(canonical_or_id)
    if entry is not None:
        return entry.get("providers", {}).get("acp", "")
    return canonical_or_id


# The index that carries per-model window sizes. A context window is a property
# of the MODEL, not the provider serving it (Opus 4.8 is 200K whether reached
# via kiro-cli/``acp`` or ``claude_code``), and only this one index is populated
# in model_registry.json — its ``aliases`` already include every kiro/acp-
# advertised id (dotted ``claude-opus-4.8``, bare ``claude-opus-4-8[1m]``, …).
# Named for the registry key, NOT because windows are claude_code-only.
_WINDOW_INDEX = "claude_code"

# The reference/full window a caller should assume when the true window is
# genuinely unknown. Deliberately 1M (not 200k): the default deployment runs
# ``acp`` + ``auto`` on a 1M model, and treating unknown as the reference means
# the context-budget scaler never silently SHRINKS an unresolved model's budget.
REFERENCE_WINDOW_TOKENS = 1_000_000


def _has_1m_token(lowered: str) -> bool:
    """True if ``lowered`` contains a standalone ``1m`` token (not ``10m`` etc.)."""
    return re.search(r"(^|[^a-z0-9])1m([^a-z0-9]|$)", lowered) is not None


def _registry_window(canonical_or_id: str) -> int | None:
    """The static-registry window for an id, or None if the registry omits it.

    Resolves against the **acp** index FIRST, then claude_code. This matters for
    the ids that are DISTINCT kiro models but claude_code aliases: kiro serves
    ``claude-haiku-4.5`` / ``claude-sonnet-4.5`` / ``claude-sonnet-4`` at 200K
    (their own acp-index canonical entries), while the claude_code index folds
    them onto ``sonnet-4.6-1m`` (1M) for claude-agent-acp dropdown dedup. The
    window is a property of the model AS SERVED, and the default provider is acp,
    so the acp view is authoritative; claude_code is the fallback for ids only it
    lists. A canonical key/alias unique to one index resolves in that index.
    """
    for provider in ("acp", _WINDOW_INDEX):
        key = _resolve_canonical(canonical_or_id, provider)
        if key is not None:
            win = _REGISTRY[key].get("window")
            if isinstance(win, int) and win > 0:
                return int(win)
    return None


def _supplementary_window(model: str) -> int | None:
    """Window from the supplementary Bedrock/legacy map (exact, then longest
    substring — Bedrock ids embed the dotted model name), or None if absent."""
    if model in _SUPPLEMENTARY_WINDOWS:
        return _SUPPLEMENTARY_WINDOWS[model]
    # Longest-key-first so a specific id wins over a shorter embedded match
    # (parity with the old claude_code._resolve_context_window substring scan).
    for key in sorted(_SUPPLEMENTARY_WINDOWS, key=len, reverse=True):
        if key in model:
            return _SUPPLEMENTARY_WINDOWS[key]
    return None


def model_window(canonical_or_id: str, *, live_tokens: int | None = None) -> int | None:
    """THE central model -> context-window resolver. Single source of truth.

    Fallback order (first hit wins), most-authoritative first:

    1. ``live_tokens`` — the served window from kiro's per-turn
       ``usage_update.size`` (or any live signal the caller already has). Wins
       over everything: it is what the backend is ACTUALLY billing against.
    2. Kiro-list cache — the structured ``context_window_tokens`` from
       ``kiro-cli chat --list-models`` (see :func:`refresh_kiro_windows`),
       keyed by the kiro model id/name. Correct for EVERY model kiro serves,
       including non-Anthropic (GPT 272k, DeepSeek 164k, Qwen 256k) and the
       sonnet/haiku ids the static registry folds onto a 1M canonical.
    3. Static registry — the hand-maintained ``window`` literal (Anthropic ids).
    4. Supplementary map — Bedrock/legacy ids (:data:`_SUPPLEMENTARY_WINDOWS`,
       formerly the separate ``model_tokens.json``), exact then longest-substring.
    5. ``[1m]``/``-1m`` heuristic -> 1M (forward-compat for an unlisted 1M id).
    6. ``None`` — genuinely unknown. Callers treat None as
       :data:`REFERENCE_WINDOW_TOKENS` (never a silent 200k), so an unknown
       model keeps the full budget rather than being wrongly shrunk.

    NOTE: there is intentionally no silent 200k default — that literal, which
    caused GPT/DeepSeek/etc. to under-report and the alias fold to over-report,
    is gone. A concrete 200k only ever comes from a source that really says 200k
    (kiro list, registry entry, or a live usage_update).
    """
    if isinstance(live_tokens, int) and not isinstance(live_tokens, bool) and live_tokens > 0:
        return live_tokens
    cached = _KIRO_WINDOWS.get(canonical_or_id)
    if cached:
        return cached
    reg = _registry_window(canonical_or_id)
    if reg is not None:
        return reg
    supp = _supplementary_window(canonical_or_id)
    if supp is not None:
        return supp
    lowered = canonical_or_id.lower()
    if "[1m]" in lowered or _has_1m_token(lowered):
        return 1_000_000
    return None


def window_source(canonical_or_id: str) -> str:
    """Diagnostic: which tier :func:`model_window` resolves ``id`` from.

    One of ``"kiro-list"``, ``"registry"``, ``"supplementary"``, ``"heuristic"``,
    ``"unknown"``. (The ``"live"`` tier is per-call via ``live_tokens`` and not
    reflected here.)
    """
    if _KIRO_WINDOWS.get(canonical_or_id):
        return "kiro-list"
    if _registry_window(canonical_or_id) is not None:
        return "registry"
    if _supplementary_window(canonical_or_id) is not None:
        return "supplementary"
    lowered = canonical_or_id.lower()
    if "[1m]" in lowered or _has_1m_token(lowered):
        return "heuristic"
    return "unknown"


def window(canonical_or_id: str) -> int:
    """Back-compat shim: :func:`model_window` with the unknown case resolved to
    the 1M reference.

    Prefer :func:`model_window` in new code — it returns ``None`` for a genuinely
    unknown model so the caller can decide the fail-safe. This shim exists for
    callers that need a concrete int and are content with the reference default.
    It no longer returns a silent 200k for unknown ids (the old behaviour that
    shrank unknown models' budgets).
    """
    return model_window(canonical_or_id) or REFERENCE_WINDOW_TOKENS


def has_known_window(canonical_or_id: str) -> bool:
    """True if the window for ``id`` comes from a real source (kiro list or the
    static registry) — NOT the ``[1m]`` heuristic or the unknown fallback.

    Lets the context-budget scaler tell a genuinely-known window apart from a
    guessed/unknown one so it never shrinks an unknown model's budget. Now also
    True for kiro-list-cached non-Anthropic models (GPT/DeepSeek/Qwen) and the
    supplementary fully-qualified/legacy id map, which is what lets the ACP
    backfill report the model's real window.
    """
    return (
        bool(_KIRO_WINDOWS.get(canonical_or_id))
        or _registry_window(canonical_or_id) is not None
        or _supplementary_window(canonical_or_id) is not None
    )


def get_entry(canonical_key: str) -> dict[str, Any] | None:
    """Return the registry entry for a canonical key, or None if unknown.

    Public accessor for registry metadata (display name, description, window,
    aliases, provider ids). Prefer this over reaching into ``_REGISTRY`` directly.
    """
    return _REGISTRY.get(canonical_key)


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


def is_canonical_key(name: str) -> bool:
    """True if ``name`` is a top-level canonical registry key (e.g. ``fable-5-1m``).

    Distinguishes a canonical KEY from a provider alias or id: ``claude-fable-5``
    is an alias (False), ``fable-5-1m`` is a key (True). The set-model guard uses
    this to reject canonical keys on non-``claude_code`` providers, where they are
    display-only identifiers the ACP CLI rejects as model ids (-32603 "model not
    available"). ``auto`` is a registry key too, so callers that must permit the
    Auto sentinel check for it separately.
    """
    return name in _REGISTRY


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
