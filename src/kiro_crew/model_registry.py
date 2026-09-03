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
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write

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
# and legacy Claude snapshots. Folded here (rather than a separate
# ``model_tokens.json``, which would be a second, drifting source of truth) so
# ``model_window`` is the ONE
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
    # Currently-served sub-1M models. Windows are the kiro-cli
    # `chat --list-models --format json` context_window_tokens
    # (the refresh_kiro_windows cache overrides these when seeded).
    "deepseek-3.2": 164_000,
    "minimax-m2.5": 196_000,
    "minimax-m2.1": 196_000,
    "glm-5": 200_000,
    "gpt-5.6-sol": 272_000,
    "gpt-5.6-terra": 272_000,
    "gpt-5.6-luna": 272_000,
    "qwen3-coder-next": 256_000,
    # Legacy / not-currently-served kiro ids, kept as harmless static floors.
    "kimi-k2.5": 256_000,
    "glm-4.7": 200_000,
    "glm-4.7-flash": 128_000,
    "qwen3-coder-480b": 256_000,
}


def _kiro_windows_cache_path() -> Path:
    """Path to the persisted kiro-window sidecar under the data home.

    Resolved lazily (not at import) so tests / KIROCREW_HOME overrides are
    honoured, and so a home-resolution failure never breaks module import.
    Routes through ``config_dir()`` (deferred import of the stdlib-only
    ``config.paths`` leaf to avoid a cycle) so it follows the data-home move to
    ``~/.kiro/crew`` instead of writing to the now-archived legacy ``~/.kirocrew``
    — where no reader would ever consult it and which would re-create the very
    directory the migration just archived.
    """
    from kiro_crew.config.paths import config_dir

    return config_dir() / "model_windows.json"


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
            if isinstance(key, str) and key and _KIRO_WINDOWS.get(key) != win:
                _KIRO_WINDOWS[key] = win
                updated = True
    return updated


def persist_kiro_windows() -> None:
    """Write the in-memory kiro-window cache to disk (best-effort, blocking I/O).

    Separated from :func:`refresh_kiro_windows` so an async caller can offload
    ONLY this filesystem step to an executor while keeping the in-memory update
    synchronous. Atomic via the shared :func:`kiro_crew.atomic_write.atomic_write`
    helper; a persist failure is logged, not raised — the in-memory cache is
    authoritative for this process either way.

    The helper replaces a hand-rolled temp-write-and-rename whose temp name was
    derived from the destination (``model_windows.json.tmp``), so two processes
    persisting the cache raced on one filename, and which missed the helper's
    bounded retry for the Windows rename window. Durability and permission
    semantics are unchanged: no ``fsync`` (best-effort by contract, per the note
    above) and no explicit ``mode``, so the sidecar still lands at the umask
    default. The helper creates the parent directory itself and raises ``OSError``
    on failure — the same class the ``except`` below already absorbed.

    Thread-safety: this runs on an executor thread while ``refresh_kiro_windows``
    mutates ``_KIRO_WINDOWS`` on the event-loop thread. Snapshot with ``dict(...)``
    (a C-level copy that does not release the GIL) BEFORE serializing, so
    ``json.dump`` cannot hit ``RuntimeError: dictionary changed size during
    iteration`` from a concurrent add/remove.
    """
    try:
        snapshot = dict(_KIRO_WINDOWS)  # atomic under the GIL; safe vs. concurrent mutation
        path = _kiro_windows_cache_path()
        atomic_write(path, json.dumps(snapshot))
    except OSError:  # pragma: no cover - disk full / perms
        logger.debug("Could not persist kiro window cache", exc_info=True)


# ── Provider advertised-model cache (the authoritative per-provider id list) ──
# The same principle as the kiro-window cache above, applied one level up: the
# committed registry is a hand-maintained fallback and drifts from what a
# provider actually serves, so the provider's OWN advertised model list is the
# ground truth. kiro-cli advertises via ``chat --list-models``; claude-agent-acp
# advertises its versioned list in the ``session/new`` response
# (``AcpClient._capture_available_models``). This cache records those advertised
# provider ids per provider so the consumers that used to read the static
# ``available_models(provider)`` allowlist can read what the provider served
# instead — chiefly the claude_code ``settings.local.json`` ``availableModels``
# seed, which unlocks a model's real window and previously carried only the
# registry's Anthropic ids (so a served-but-unlisted model, e.g. a new Opus,
# collapsed to the base window).
#
# Runtime state, not committed data (like ``_KIRO_WINDOWS`` / session_map). A
# corrupt/missing cache degrades silently to the registry allowlist and can
# never brick import.
_ADVERTISED_MODELS: dict[str, list[str]] = {}

# Inference-profile prefixes stripped when folding an advertised provider id to
# a comparison key. Longest-first so ``global.anthropic.`` wins over a bare
# ``anthropic.`` that is a suffix of it.
_PROVIDER_ID_PREFIXES: tuple[str, ...] = (
    "global.anthropic.",
    "us.anthropic.",
    "eu.anthropic.",
    "apac.anthropic.",
    "anthropic.",
)


def _advertised_models_cache_path() -> Path:
    """Path to the persisted advertised-model sidecar under the data home.

    Resolved lazily (not at import), for the same reasons as
    :func:`_kiro_windows_cache_path`: honour ``KIROCREW_HOME`` / test overrides
    and never let home resolution break module import.
    """
    from kiro_crew.config.paths import config_dir

    return config_dir() / "provider_models.json"


def _load_advertised_models() -> None:
    """Load the persisted advertised-model cache into ``_ADVERTISED_MODELS``.

    Called once at import. A missing file is normal (first run); a corrupt file
    is logged and ignored (degrade to the registry allowlist), never raised.
    """
    try:
        path = _advertised_models_cache_path()
        if not path.is_file():
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for provider, ids in data.items():
                if isinstance(provider, str) and isinstance(ids, list):
                    clean = [i for i in ids if isinstance(i, str) and i.strip()]
                    if clean:
                        _ADVERTISED_MODELS[provider] = clean
    except (OSError, ValueError, TypeError):  # pragma: no cover - corrupt/absent cache
        logger.debug("advertised-model cache unreadable; using registry allowlist", exc_info=True)


_load_advertised_models()


def refresh_advertised_models(provider: str, ids: Sequence[str]) -> bool:
    """Ingest a provider's advertised model ids into the cache.

    In-memory only (cheap, non-blocking) so it is safe to call from an async
    handler and the cache is immediately consistent for callers on the same
    tick. Returns ``True`` when the cache changed (a persist is warranted): the
    async caller should then offload :func:`persist_advertised_models` to an
    executor rather than block the event loop on disk I/O.

    An empty ``ids`` is a no-op (a backend that advertised nothing must not wipe
    a good cached list from a prior session) and returns ``False``. The stored
    list is deduped preserving order.
    """
    clean: list[str] = []
    seen: set[str] = set()
    for i in ids:
        if isinstance(i, str) and i.strip() and i not in seen:
            seen.add(i)
            clean.append(i)
    if not clean:
        return False
    if _ADVERTISED_MODELS.get(provider) == clean:
        return False
    _ADVERTISED_MODELS[provider] = clean
    return True


def persist_advertised_models() -> None:
    """Write the in-memory advertised-model cache to disk (best-effort, blocking).

    Separated from :func:`refresh_advertised_models` so an async caller can
    offload ONLY the filesystem step to an executor while keeping the in-memory
    update synchronous. Atomic via :func:`kiro_crew.atomic_write.atomic_write`; a
    persist failure is logged, not raised. Snapshot with ``dict(...)`` before
    serializing so a concurrent refresh on the event-loop thread cannot raise
    ``RuntimeError: dictionary changed size during iteration`` — mirrors
    :func:`persist_kiro_windows`.
    """
    try:
        snapshot = {p: list(v) for p, v in dict(_ADVERTISED_MODELS).items()}
        path = _advertised_models_cache_path()
        atomic_write(path, json.dumps(snapshot))
    except OSError:  # pragma: no cover - disk full / perms
        logger.debug("Could not persist advertised-model cache", exc_info=True)


def advertised_models(provider: str) -> list[str]:
    """The provider ids ``provider`` last advertised, or ``[]`` on a cold cache."""
    return list(_ADVERTISED_MODELS.get(provider, ()))


def _normalize_advertised_key(provider_id: str) -> str:
    """Reduce a provider id to a spelling-agnostic comparison key.

    Strips a leading inference-profile prefix and a trailing ``[1m]`` / ``-1m``
    window marker, unifies ``.``/``-`` separators, and lowercases — so the
    versioned id a backend advertises (``global.anthropic.claude-opus-5[1m]``)
    and the bare id a caller may hold (``claude-opus-5``) fold to the same key
    (``claude-opus-5``). Used only to match a stored id against the advertised
    set; never persisted or sent on the wire.
    """
    s = provider_id.strip().lower()
    for pfx in _PROVIDER_ID_PREFIXES:
        if s.startswith(pfx):
            s = s[len(pfx) :]
            break
    s = s.replace("[1m]", "")
    s = re.sub(r"[-.]1m$", "", s)
    s = s.replace(".", "-")
    return s.strip("-")


def _is_1m_id(model_id: str) -> bool:
    """True if ``model_id`` names a 1M-window variant (``[1m]`` suffix or a
    standalone ``1m`` token)."""
    low = model_id.lower()
    return "[1m]" in low or _has_1m_token(low)


def _dedup_window_siblings(ids: Sequence[str]) -> list[str]:
    """Drop a base-window id when a 1M-window sibling with the same base is present.

    claude-agent-acp reads the ``[1m]`` suffix as a context-window MODIFIER on one
    base model, not a distinct model, and merges ``availableModels``
    union+dedup by base name. Seeding BOTH
    ``global.anthropic.claude-opus-4-8[1m]`` (1M) and its 200K sibling
    ``global.anthropic.claude-opus-4-8`` therefore lets the adapter's dedup pick
    the base spelling and serve 200K for an Opus 4.8 pick. When two ids share a
    normalized base key, keep only the 1M one; otherwise preserve order and drop
    exact duplicates. Order-preserving, so ``available_models``' default-first head
    (the 1M flagship) survives.
    """
    has_1m = {_normalize_advertised_key(m) for m in ids if _is_1m_id(m)}
    out: list[str] = []
    seen: set[str] = set()
    for mid in ids:
        key = _normalize_advertised_key(mid)
        if key and not _is_1m_id(mid) and key in has_1m:
            continue  # a 1M sibling supersedes this base-window spelling
        if mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


def seed_available_models(provider: str) -> list[str]:
    """The ``availableModels`` allowlist to seed for ``provider``.

    Provider-first: the ids ``provider`` actually advertised (cached from a live
    session) when the cache is warm, so the seed reflects what the account is
    served rather than the static Anthropic-only registry. Falls back to
    :func:`available_models` on a cold cache (first-ever session, before any
    ``session/new`` has been captured) — the static registry list, which is then
    window-deduplicated below just like the warm list.

    The result is passed through :func:`_dedup_window_siblings` so a base-window
    id never rides alongside its 1M sibling: seeding both is what lets the adapter
    collapse a versioned pick (e.g. Opus 4.8 ``[1m]``) back to 200K. Applied to
    the advertised list too, since a backend can advertise both spellings.
    """
    cached = advertised_models(provider)
    base = cached if cached else available_models(provider)
    return _dedup_window_siblings(base)


def resolve_wire_model_id(model_id: str, provider: str) -> str:
    """Fold a stored provider-model id onto the spelling ``provider`` advertised.

    An id the static registry does not carry (a newly-served model) reaches this
    module as a bare passthrough from :func:`to_provider_id`; sent as-is it can
    collapse to the base window because it never matches the versioned id in the
    seeded ``availableModels``. When the provider advertised a matching id, this
    returns that id instead, so the wire value and the seed agree on one exact
    spelling.

    Returns ``model_id`` UNCHANGED when it is empty / the ``auto`` sentinel, when
    the provider advertised nothing (cold cache), when it is already an
    advertised id, or when no advertised id shares its normalized key — i.e. it
    only ever tightens a bare id onto an advertised versioned one, never rewrites
    an id the provider does not serve. When several advertised ids match, a 1M
    window variant wins over a base one.
    """
    if not model_id or model_id == "auto":
        return model_id
    adv = advertised_models(provider)
    if not adv or model_id in adv:
        return model_id
    want = _normalize_advertised_key(model_id)
    if not want:
        return model_id
    matches = [a for a in adv if _normalize_advertised_key(a) == want]
    if not matches:
        return model_id
    matches.sort(
        key=lambda a: (0 if ("[1m]" in a.lower() or _has_1m_token(a.lower())) else 1, len(a))
    )
    return matches[0]


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
    4. Supplementary map — Bedrock/legacy ids (:data:`_SUPPLEMENTARY_WINDOWS`),
       exact then longest-substring.
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
    It returns ``REFERENCE_WINDOW_TOKENS`` for unknown ids rather than a silent
    200k that would shrink unknown models' budgets.
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


def acp_id_correction(candidate: str) -> str | None:
    """The real kiro-cli id for a value the registry knows by a WRONG spelling.

    Returns ``None`` when *candidate* is already a valid kiro-cli id, is empty
    or ``auto``, or is unrecognized entirely (an unregistered-but-real id — a
    regional profile or a future model — must not be second-guessed).

    This exists because the spellings of one model are not interchangeable on
    the wire, and a spec pinning the wrong one is read by kiro-cli when the child
    starts: the process dies seconds later with no turn taken.
    :func:`to_acp_id` deliberately does not fold aliases (that would silently
    downgrade a Haiku-pinned agent to Sonnet), so the wrong spelling reaches the
    child unchanged. The information needed to name the right one is already in
    the registry.

    Resolution deliberately spans EVERY provider index, not just ``acp``.
    :func:`_build_indices` puts each entry's aliases into every provider's index
    but each provider's own id only into its own, so an ``acp``-only lookup
    catches the prefix-stripped alias (``claude-opus-4-8``) while missing the
    registered id it was stripped from
    (``global.anthropic.claude-opus-4-8``) — the same mistake in the form
    someone copying from Bedrock is likelier to make. So the rule is one rule:
    any spelling the registry recognizes for a model, that is not what kiro-cli
    serves, resolves to what kiro-cli serves.

    ``acp`` is consulted first so a value that provider already knows keeps its
    own reading; the rest are visited in sorted order, so the answer never
    depends on registry insertion order.
    """
    if not candidate or candidate == "auto":
        return None
    if candidate in set(available_models("acp")):
        return None
    for provider in ["acp", *sorted(p for p in _CANONICAL_INDEX if p != "acp")]:
        canonical = _resolve_canonical(candidate, provider)
        if canonical is None:
            continue
        corrected = (_REGISTRY.get(canonical) or {}).get("providers", {}).get("acp", "")
        if corrected:
            return corrected
    return None


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


# Region/vendor routing prefix a Bedrock inference-profile id carries
# (``global.anthropic.claude-opus-4-8[1m]``, ``us.anthropic.…``). A
# provider-prefixed id is not itself a registry key/alias, so :func:`canonical_key`
# peels this and retries the lookup. Same shape the frontend shares via
# ``fmtTurnModel`` (chat/AssistantMessage.tsx) and ``canonicalKey``
# (providers/modelRegistry.ts).
_ROUTING_PREFIX_RE = re.compile(
    r"^(?:(?:us|eu|apac|global)\.)?(?:anthropic|amazon|openai|bedrock)\."
)


def canonical_key(name: str) -> str | None:
    """Canonical registry key for ``name``, resolved provider-aware, or ``None``.

    Resolution order is the acp (kiro-cli) index FIRST, then ``claude_code`` --
    the SAME order :func:`_registry_window` uses, and for the same reason: the
    ``claude_code`` index deliberately aliases kiro's distinct models onto one
    canonical for claude-agent-acp dropdown dedup (``claude-haiku-4.5`` /
    ``claude-sonnet-4.5`` / ``claude-sonnet-4`` -> ``sonnet-4.6-1m``;
    ``claude-opus-4.6`` -> ``opus-4.8-1m``), while kiro -- the fork's shipping
    harness -- serves each as a DISTINCT real model with its own acp-index
    canonical entry. Resolving the acp view first keeps those apart.

    Accepts a canonical key (resolves to itself), a registry alias, or a
    per-provider id -- with or without a region/vendor routing prefix
    (``us.anthropic.…``, ``global.anthropic.…``) -- and returns ``None`` for
    anything the registry does not list. A provider-prefixed id is not itself a
    registry key/alias, so the prefix is peeled and the lookup retried (the "fold
    a provider/partition prefix" half of #5339). This is the single "which
    registry model is this id?" fold shared by ``_normalize_model_key``
    (dashboard/handlers/agents.py) and the frontend ``canonicalKey``
    (providers/modelRegistry.ts) -- the peel lives HERE so any backend caller of
    this documented fold gets both #5339 halves, not just the dashboard handler.
    """
    for provider in ("acp", "claude_code"):
        key = _resolve_canonical(name, provider)
        if key is not None:
            return key
    stripped = _ROUTING_PREFIX_RE.sub("", name)
    if stripped != name:
        for provider in ("acp", "claude_code"):
            key = _resolve_canonical(stripped, provider)
            if key is not None:
                return key
    return None


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
