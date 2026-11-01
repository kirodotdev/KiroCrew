"""Safe provider configuration and credential administration.

This module is shared by the CLI and the localhost management UI.  Secrets are
never included in returned provider dictionaries and are never written back to
``providers.json`` after migration.
"""

from __future__ import annotations

import errno
import json
import os
import re
import tempfile
import threading
import time
from functools import wraps
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigError, config_path, official_endpoint_for_preset
from .state_io import _file_transaction_lock
from .credential_store import (
    CredentialStore,
    CredentialStoreError,
    ProviderSecrets,
    decode_provider_secrets,
    default_credential_store,
    encode_provider_secrets,
    is_sensitive_header,
    provider_credential_ref,
)
from .discovery import DiscoveryError, DiscoveredModel, discover_models, filter_agent_models, infer_provider_identity
from .provider_catalog import (
    connection_identity, connection_record, credential_connection_profile,
    credential_rejection_message, merge_official_model_profile,
    migrate_official_base_url, model_capability_from_row, model_routing_from_row, provider_preset,
    public_platforms, public_presets, transport_label,
)
from .policy_packs import PolicyPackError, default_pack, get_policy_pack, public_policy_packs, resolve_policy
from .reasoning_control import normalize_thinking_preference
from .reasoning_language import normalize_reasoning_language
from .i18n import normalize_ui_language


_CONFIG_MUTATION_LOCK = threading.RLock()
_CONFIG_MUTATION_CONTEXT = threading.local()
_IS_WINDOWS = os.name == "nt"
# Windows antivirus/indexers and readers may briefly hold a handle without
# FILE_SHARE_DELETE, causing os.replace to fail even for a writable destination.
# Bounded retry only: permanent ACL/read-only errors must still be surfaced.
_WINDOWS_REPLACE_BACKOFF = (0.05, 0.12, 0.25, 0.5)


def _replace_provider_config(tmp: Path, destination: Path) -> None:
    for attempt in range(len(_WINDOWS_REPLACE_BACKOFF) + 1):
        try:
            os.replace(tmp, destination)
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if not _IS_WINDOWS or not (
                winerror in (5, 32, 33) or
                (winerror is None and exc.errno in (errno.EACCES, errno.EBUSY))
            ):
                raise
            if attempt >= len(_WINDOWS_REPLACE_BACKOFF):
                raise
            time.sleep(_WINDOWS_REPLACE_BACKOFF[attempt])



def _serialized_config_mutation(func):
    """Serialize config read-modify-write across threads *and* processes.

    The stable sidecar lock covers the entire transaction, not just os.replace.
    Nested credential migration reuses the outer lock on the same thread;
    acquiring a second OS lock on the same file could self-deadlock.
    """
    @wraps(func)
    def wrapped(*args, **kwargs):
        with _CONFIG_MUTATION_LOCK:
            if getattr(_CONFIG_MUTATION_CONTEXT, "active_path", None) is not None:
                requested = kwargs.get("path")
                if requested is not None and Path(requested) != _CONFIG_MUTATION_CONTEXT.active_path:
                    raise ConfigError("nested provider transaction cannot switch config paths")
                return func(*args, **kwargs)
            target = Path(kwargs.get("path") or config_path())
            with _file_transaction_lock(target):
                _CONFIG_MUTATION_CONTEXT.active_path = target
                try:
                    return func(*args, **kwargs)
                finally:
                    del _CONFIG_MUTATION_CONTEXT.active_path
    return wrapped


@dataclass(frozen=True)
class MigrationReport:
    changed: bool
    migrated_providers: tuple[str, ...]
    migrated_secret_headers: int
    sanitized_backup_files: tuple[str, ...]
    credential_backend: str




def _normalize_instance_id(value: str) -> str:
    """Return a config-safe provider *instance* id.

    ``id`` is the routing/credential identity of one configured account/endpoint,
    while ``preset_id`` describes the provider family (openai, deepseek, custom,
    ...).  Keeping those concepts separate lets multiple accounts from the same
    vendor coexist without changing the runtime routing contract.
    """
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text).strip("-._")
    if not text:
        return "provider"
    if "/" in text:
        raise ConfigError("provider instance id may not contain '/'")
    return text[:80]


def _unique_instance_id(base: str, providers: list[Any], *, exclude_id: str = "") -> tuple[str, int]:
    base = _normalize_instance_id(base)
    used = {
        str(row.get("id") or "")
        for row in providers
        if isinstance(row, dict) and str(row.get("id") or "") != exclude_id
    }
    if base not in used:
        return base, 1
    n = 2
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}", n


def _provider_family_name(provider: dict[str, Any]) -> str:
    preset = provider_preset(str(provider.get("preset_id") or "custom"))
    return preset.display_name if preset is not None else str(provider.get("preset_id") or "custom")


def _read_raw(path: Path | None = None) -> dict[str, Any]:
    path = path or config_path()
    if not path.exists():
        try:
            pack = default_pack()
            mode = {"id": pack.id, "type": pack.default_policy}
        except PolicyPackError:
            mode = {"id": "", "type": ""}
        return {"mode": mode, "providers": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("providers.json root must be an object")
    providers = raw.get("providers", [])
    if not isinstance(providers, list):
        raise ConfigError("providers must be an array")
    return raw


def _write_raw(raw: dict[str, Any], path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    fd = -1
    tmp: Path | None = None
    try:
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp = Path(raw_tmp)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_provider_config(tmp, path)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _store() -> CredentialStore:
    return default_credential_store()


def _repair_default_models(raw: dict[str, Any]) -> None:
    """Keep existing provider/root defaults valid after catalogue changes.

    Defaults are repaired only when the user/config already had one; this must
    not manufacture a concrete-model preference for a fresh Type-routed setup.
    """
    providers = raw.get("providers") if isinstance(raw.get("providers"), list) else []
    valid_root: set[str] = set()
    fallback_key = ""
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        pid = str(provider.get("id") or "").strip()
        models = provider.get("models") if isinstance(provider.get("models"), list) else []
        ids = [str(row.get("id") or "").strip() for row in models if isinstance(row, dict) and str(row.get("id") or "").strip()]
        current = str(provider.get("default_model") or "").strip()
        if current and current not in ids:
            current = ids[0] if ids else ""
            if current:
                provider["default_model"] = current
            else:
                provider.pop("default_model", None)
        for mid in ids:
            key = f"{pid}/{mid}" if pid else ""
            if key:
                valid_root.add(key)
        if not fallback_key and pid and ids:
            fallback_key = f"{pid}/{current if current in ids else ids[0]}"

    root = str(raw.get("default_model") or "").strip()
    if root and root not in valid_root:
        if fallback_key:
            raw["default_model"] = fallback_key
        else:
            raw.pop("default_model", None)


def _sanitize_config_copy(path: Path) -> bool:
    """Remove plaintext secrets from a legacy config backup without reading them into a store."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict) or not isinstance(raw.get("providers"), list):
        return False
    changed = False
    for provider in raw["providers"]:
        if not isinstance(provider, dict):
            continue
        provider_id = str(provider.get("id") or "").strip()
        removed_secret = False
        if "api_key" in provider:
            provider.pop("api_key", None)
            removed_secret = True
            changed = True
        headers = provider.get("headers")
        if isinstance(headers, dict):
            public = {k: v for k, v in headers.items() if isinstance(k, str) and isinstance(v, str) and not is_sensitive_header(k)}
            if public != headers:
                removed_secret = True
                changed = True
                if public:
                    provider["headers"] = public
                else:
                    provider.pop("headers", None)
        if removed_secret and provider_id and not str(provider.get("credential_ref") or "").strip():
            provider["credential_ref"] = provider_credential_ref(provider_id)
    if raw.get("credential_schema_version") != 1:
        raw["credential_schema_version"] = 1
        changed = True
    if changed:
        _write_raw(raw, path)
    return changed


@_serialized_config_mutation
def migrate_plaintext_credentials(
    *,
    store: CredentialStore | None = None,
    path: Path | None = None,
) -> MigrationReport:
    """Move plaintext API keys/secret headers into the OS credential store.

    The config file is replaced only after all credential writes succeed.  No
    plaintext backup file is created, because such a backup would defeat the
    migration's purpose.
    """

    store = store or _store()
    raw = _read_raw(path)
    providers = raw.get("providers", [])
    changed = False
    migrated: list[str] = []
    header_count = 0

    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_id = str(provider.get("id") or "").strip()
        if not provider_id:
            continue

        legacy_key = provider.get("api_key", "")
        if legacy_key is None:
            legacy_key = ""
        if not isinstance(legacy_key, str):
            raise ConfigError(f"provider {provider_id}.api_key must be a string")

        headers_raw = provider.get("headers", {})
        if headers_raw is None:
            headers_raw = {}
        if not isinstance(headers_raw, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers_raw.items()
        ):
            raise ConfigError(f"provider {provider_id}.headers must be a string-to-string object")

        public_headers: dict[str, str] = {}
        secret_headers: dict[str, str] = {}
        for name, value in headers_raw.items():
            if is_sensitive_header(name):
                secret_headers[name] = value
            else:
                public_headers[name] = value

        existing_ref_raw = provider.get("credential_ref", "")
        existing_ref = existing_ref_raw.strip() if isinstance(existing_ref_raw, str) else ""
        has_plaintext_secret = bool(legacy_key or secret_headers)

        if has_plaintext_secret:
            ref = existing_ref or provider_credential_ref(provider_id)
            current = decode_provider_secrets(store.get(ref))
            # Explicit plaintext values from the legacy config win during the
            # one-time migration; otherwise preserve an already-stored value.
            merged = ProviderSecrets(
                api_key=legacy_key if legacy_key else current.api_key,
                headers={**current.headers, **secret_headers},
            )
            store.set(ref, encode_provider_secrets(merged))
            provider["credential_ref"] = ref
            migrated.append(provider_id)
            header_count += len(secret_headers)
            changed = True

        if "api_key" in provider:
            del provider["api_key"]
            changed = True
        if public_headers != headers_raw:
            if public_headers:
                provider["headers"] = public_headers
            else:
                provider.pop("headers", None)
            changed = True

    active_path = path or config_path()
    if raw.get("credential_schema_version") != 1:
        raw["credential_schema_version"] = 1
        changed = True
    if changed:
        _write_raw(raw, active_path)

    sanitized_backups: list[str] = []
    # Earlier configure.ps1 releases created a plaintext .bak before replacing config.
    # Scrub the known backup names after the active credential has safely landed
    # in the OS store; otherwise upgrading would leave the old key on disk.
    for suffix in (".bak", ".backup"):
        backup = active_path.with_name(active_path.name + suffix)
        if backup.exists() and _sanitize_config_copy(backup):
            sanitized_backups.append(str(backup))

    return MigrationReport(
        changed=changed,
        migrated_providers=tuple(migrated),
        migrated_secret_headers=header_count,
        sanitized_backup_files=tuple(sanitized_backups),
        credential_backend=store.backend_name,
    )


def _model_dict(model: Any) -> dict[str, Any]:
    return {
        "id": model.id,
        "name": model.name,
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
        "input_modalities": list(model.input_modalities),
        "output_modalities": list(model.output_modalities),
        "effort_supported_levels": list(model.effort_supported_levels),
        "effort_default_level": model.effort_default_level,
        "reasoning": {
            "mode": model.reasoning_mode,
            "supported": model.reasoning_supported,
            "can_disable": model.reasoning_can_disable,
            "default_enabled": model.reasoning_default_enabled,
            "disable_value": model.reasoning_disable_value,
            "enable_value": model.reasoning_enable_value,
            "auto_value": model.reasoning_auto_value,
            "supports_auto": model.reasoning_supports_auto,
            "thinking": {
                "supported": model.reasoning_supported,
                "can_disable": model.reasoning_can_disable,
                "default_enabled": model.reasoning_default_enabled,
                "disable_value": model.reasoning_disable_value,
                "enable_value": model.reasoning_enable_value,
                "auto_value": model.reasoning_auto_value,
                "supports_auto": model.reasoning_supports_auto,
            },
            "effort": {
                "levels": list(model.effort_supported_levels),
                "default": model.effort_default_level,
                "fixed_level": model.reasoning_fixed_level,
            },
            # Legacy mirrors: intensity only; on/off is no longer an effort level.
            "levels": list(model.effort_supported_levels),
            "default": model.effort_default_level,
            "fixed_level": model.reasoning_fixed_level,
            "source": model.capability_source,
        },
        "capability_source": model.capability_source,
        "tool_use": model.tool_use,
        "transport": str(getattr(model, "transport", "") or ""),
        "invocation_target": str(getattr(model, "invocation_target", "") or model.id),
        "invocation_target_kind": str(getattr(model, "invocation_target_kind", "model") or "model"),
        "lifecycle": str(getattr(model, "lifecycle", "active") or "active"),
        "catalog_source": str(getattr(model, "catalog_source", model.capability_source) or "provider"),
        "routing": {
            "quality": getattr(model, "routing_quality", None),
            "speed": getattr(model, "routing_speed", None),
            "economy": getattr(model, "routing_economy", None),
            "tiers": list(getattr(model, "routing_tiers", ()) or ()),
            "source": str(getattr(model, "routing_source", "unknown") or "unknown"),
            "confidence": str(getattr(model, "routing_confidence", "unknown") or "unknown"),
            "provenance": {
                str(field): {"source": str(source), "confidence": str(confidence)}
                for field, source, confidence in (getattr(model, "routing_provenance", ()) or ())
            },
        },
    }


def _safe_provider(provider: dict[str, Any], store: CredentialStore) -> dict[str, Any]:
    provider_id = str(provider.get("id") or "")
    ref = str(provider.get("credential_ref") or "").strip()
    configured = False
    if ref:
        try:
            configured = store.exists(ref)
        except CredentialStoreError:
            configured = False
    raw_models = provider.get("models") if isinstance(provider.get("models"), list) else []
    preset_id = str(provider.get("preset_id") or "custom")
    preset = provider_preset(preset_id)
    credential_required = bool(preset.requires_api_key) if preset is not None else True
    base_url = str(provider.get("base_url") or "")
    official_endpoint = official_endpoint_for_preset(preset, base_url)
    inferred_identity = connection_identity(preset_id, base_url)
    platform_id = str(provider.get("platform_id") or inferred_identity.platform_id)
    product_id = str(provider.get("product_id") or inferred_identity.product_id)
    profile_id = str(provider.get("profile_id") or inferred_identity.profile_id)
    entitlement = str(provider.get("entitlement") or inferred_identity.entitlement)

    raw_type = str(provider.get("type") or "openai-compatible").strip().lower()
    effective_type = raw_type
    if official_endpoint and preset is not None and raw_type in {"openai-compatible", "openai-chat-completions", preset.transport}:
        effective_type = preset.transport

    model_rows: list[dict[str, Any]] = []
    model_transports: set[str] = set()
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            continue
        model = merge_official_model_profile(str(provider.get("preset_id") or "custom"), raw_model) if official_endpoint else dict(raw_model)
        model_transport = str(model.get("transport") or effective_type or "openai-compatible").strip().lower()
        model_transports.add(model_transport)
        model_rows.append({
            "id": str(model.get("id") or ""),
            "name": str(model.get("name") or model.get("id") or ""),
            "thinking_preference": normalize_thinking_preference(model.get("thinking_preference")),
            "transport": model_transport,
            "transport_label": transport_label(model_transport),
            "native_transport": model_transport not in {"openai-compatible", "openai-chat-completions"},
            "invocation_target": str(model.get("invocation_target") or model.get("id") or ""),
            "invocation_target_kind": str(model.get("invocation_target_kind") or "model"),
            "lifecycle": str(model.get("lifecycle") or "active"),
            "catalog_source": str(model.get("catalog_source") or model.get("capability_source") or "provider"),
            "capabilities": model_capability_from_row(model),
        })

    if len(model_transports) > 1 or (model_transports and next(iter(model_transports)) != effective_type):
        strategy_label = "按模型优化"
    else:
        strategy_label = transport_label(effective_type)
    return {
        "id": provider_id,
        "instance_id": provider_id,
        "name": str(provider.get("name") or provider_id),
        "provider_family": _provider_family_name(provider),
        "type": effective_type,
        "transport_label": strategy_label,
        "provider_transport_label": transport_label(effective_type),
        "transport_modes": [transport_label(value) for value in sorted(model_transports)] or [transport_label(effective_type)],
        "native_transport": effective_type not in {"openai-compatible", "openai-chat-completions"} or any(row["native_transport"] for row in model_rows),
        "base_url": base_url,
        "model_count": len(raw_models),
        "preset_id": preset_id,
        "platform_id": platform_id,
        "product_id": product_id,
        "profile_id": profile_id,
        "entitlement": entitlement,
        "official_endpoint": official_endpoint,
        "models": model_rows,
        "credential": {"configured": configured, "required": credential_required, "ref": ref},
    }


def list_providers(*, store: CredentialStore | None = None) -> list[dict[str, Any]]:
    store = store or _store()
    raw = _read_raw()
    return [_safe_provider(p, store) for p in raw.get("providers", []) if isinstance(p, dict)]


def mode_settings() -> dict[str, Any]:
    raw = _read_raw()
    router = raw.get("mode") if isinstance(raw.get("mode"), dict) else {}
    try:
        fallback = default_pack()
    except PolicyPackError as exc:
        return {"pack": "", "policy": "", "pack_label": "", "policy_label": "", "catalog": public_policy_packs(), "error": str(exc)}
    pack_id = str(router.get("id") or fallback.id).strip().lower()
    pack = get_policy_pack(pack_id)
    if pack is not None and not pack.enabled:
        raise ConfigError(f"policy pack {pack.id!r} is not available yet")
    if pack is None:
        pack = fallback
        pack_id = pack.id
    policy_id = str(router.get("type") or pack.default_policy).strip().lower()
    policy = pack.policy(policy_id) or pack.policy(pack.default_policy)
    if policy is None:
        raise ConfigError(f"policy pack {pack.id!r} has no usable policy")
    return {
        "pack": pack.id,
        "pack_label": pack.display_name,
        "pack_short_name": pack.short_name,
        "policy": policy.id,
        "policy_label": policy.display_name,
        "policy_short_name": policy.display_name.removesuffix(" Type"),
        "max_agents": int(policy.orchestration.max_agents),
        "max_parallel": int(policy.orchestration.max_parallel),
        "worker_mode": str(policy.orchestration.worker_mode),
        "single_writer": bool(policy.orchestration.single_writer),
        "secondary_enabled": policy.secondary is not None,
        "secondary_always": bool(policy.secondary and policy.secondary.trigger.always),
        "catalog": public_policy_packs(),
    }


@_serialized_config_mutation
def set_mode_selection(policy: str, pack: str = "") -> dict[str, Any]:
    raw = _read_raw()
    router = raw.get("mode") if isinstance(raw.get("mode"), dict) else {}
    try:
        fallback = default_pack()
    except PolicyPackError as exc:
        raise ConfigError(str(exc)) from exc
    pack_id = str(pack or router.get("id") or fallback.id).strip().lower()
    selected_pack = get_policy_pack(pack_id)
    if selected_pack is None:
        raise ConfigError(f"policy pack {pack_id!r} is not installed")
    if not selected_pack.enabled:
        raise ConfigError(f"policy pack {selected_pack.id!r} is not available yet")
    value = str(policy or selected_pack.default_policy).strip().lower()
    try:
        selected_pack, selected_policy = resolve_policy(pack_id, value)
    except PolicyPackError as exc:
        raise ConfigError(str(exc)) from exc
    selected_mode = {"id": selected_pack.id, "type": selected_policy.id}
    # Re-selecting an already active Type must not rewrite providers.json.
    # In particular, avoid a pointless Windows replace race on repeated clicks.
    if raw.get("mode") == selected_mode:
        return mode_settings()
    raw["mode"] = selected_mode
    # Mode/Type policy and concrete-model defaults are independent axes. Changing
    # the active Mode must not erase provider/model preferences that may be used
    # by explicit concrete-model sessions or fallback control surfaces.
    _write_raw(raw)
    return mode_settings()


def set_mode_pack(pack: str) -> dict[str, Any]:
    """Switch packs atomically, selecting the new pack's declared default."""
    selected = get_policy_pack(str(pack or "").strip().lower())
    if selected is None:
        raise ConfigError(f"policy pack {pack!r} is not installed")
    return set_mode_selection(selected.default_policy, selected.id)


def reasoning_language_settings() -> dict[str, Any]:
    """Return the global reasoning-presentation language preference."""
    raw = _read_raw()
    preferences = raw.get("preferences") if isinstance(raw.get("preferences"), dict) else {}
    value = normalize_reasoning_language(preferences.get("reasoning_language", "auto"))
    return {
        "reasoning_language": value,
        "options": [
            {"value": "auto", "label": "自动（跟随用户）"},
            {"value": "zh-CN", "label": "简体中文"},
            {"value": "zh-TW", "label": "繁體中文"},
            {"value": "en", "label": "English"},
            {"value": "ja", "label": "日本語"},
            {"value": "ko", "label": "한국어"},
        ],
    }


@_serialized_config_mutation
def set_reasoning_language_preference(value: str) -> dict[str, Any]:
    """Persist the provider-neutral user-visible reasoning language policy."""
    raw = _read_raw()
    normalized = normalize_reasoning_language(value)
    preferences = raw.get("preferences") if isinstance(raw.get("preferences"), dict) else {}
    preferences = dict(preferences)
    preferences["reasoning_language"] = normalized
    raw["preferences"] = preferences
    _write_raw(raw)
    return reasoning_language_settings()


def ui_language_settings() -> dict[str, str]:
    raw = _read_raw()
    prefs = raw.get("preferences") if isinstance(raw.get("preferences"), dict) else {}
    try:
        value = normalize_ui_language(prefs.get("ui_language", "auto"))
    except ValueError:
        value = "auto"
    return {"value": value}


@_serialized_config_mutation
def set_ui_language_preference(value: str) -> dict[str, str]:
    normalized = normalize_ui_language(value)
    raw = _read_raw()
    prefs = dict(raw.get("preferences") if isinstance(raw.get("preferences"), dict) else {})
    prefs["ui_language"] = normalized
    raw["preferences"] = prefs
    _write_raw(raw)
    return {"value": normalized}


@_serialized_config_mutation
def set_response_language_preference(value: str) -> dict[str, str]:
    from .reasoning_language import normalize_supported_language
    normalized = normalize_supported_language(value)
    raw = _read_raw()
    prefs = dict(raw.get("preferences") if isinstance(raw.get("preferences"), dict) else {})
    prefs["response_language"] = normalized
    raw["preferences"] = prefs
    _write_raw(raw)
    return {"value": normalized}


def response_language_settings() -> dict[str, str]:
    from .reasoning_language import normalize_supported_language
    raw = _read_raw()
    prefs = raw.get("preferences") if isinstance(raw.get("preferences"), dict) else {}
    try:
        value = normalize_supported_language(prefs.get("response_language", "auto"))
    except ValueError:
        value = "auto"
    return {"value": value}


def list_provider_presets() -> list[dict[str, Any]]:
    return public_presets()


def list_provider_platforms() -> list[dict[str, Any]]:
    """Return the structured Platform -> Product -> Profile catalogue."""
    return public_platforms()


def _manual_discovered_models(model_ids: list[str]) -> list[DiscoveredModel]:
    models: list[DiscoveredModel] = []
    seen: set[str] = set()
    for raw in model_ids:
        model_id = str(raw or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        # Manual models deliberately start with unknown reasoning capability.
        # The user/provider may later supply richer metadata; no model-name
        # heuristic is allowed to invent a reasoning selector.
        models.append(DiscoveredModel(model_id, model_id, capability_source="manual", invocation_target=model_id, catalog_source="manual"))
    return models




def _filter_discovered_models(preset: Any, models: list[DiscoveredModel]) -> list[DiscoveredModel]:
    # Always exclude obvious non-agent product rows without generation/model
    # allowlists. Unknown future chat models remain eligible.
    filtered = filter_agent_models(models)
    allow = tuple(str(item).strip() for item in getattr(preset, "discovery_model_allowlist", ()) if str(item).strip())
    if not allow:
        return filtered
    allowed = set(allow)
    return [model for model in filtered if model.id in allowed]


def _profile_discovered_models(preset_id: str, models: list[DiscoveredModel]) -> list[DiscoveredModel]:
    """Overlay only verified official model facts on discovery/manual rows."""
    out: list[DiscoveredModel] = []
    for model in models:
        discovered_row = _model_dict(model)
        if model.catalog_source == "bundled-fallback" and not str(model.lifecycle or "").strip():
            # ``active`` would only be the dataclass serialization default here,
            # not a control-plane fact. Let curated lifecycle metadata fill it.
            discovered_row.pop("lifecycle", None)
        row = merge_official_model_profile(preset_id, discovered_row)
        cap = model_capability_from_row(row)
        reasoning = cap["reasoning"]
        routing = model_routing_from_row(row)
        out.append(DiscoveredModel(
            id=str(row.get("id") or model.id),
            name=str(row.get("name") or model.name),
            context_window=cap.get("context_window"),
            max_output_tokens=cap.get("max_output_tokens"),
            input_modalities=tuple(cap.get("input_modalities") or ()),
            output_modalities=tuple(cap.get("output_modalities") or ()),
            effort_supported_levels=tuple(reasoning["effort"]["levels"]),
            effort_default_level=str(reasoning["effort"]["default"] or ""),
            reasoning_mode=str(reasoning["mode"]),
            reasoning_supported=reasoning["thinking"]["supported"] if isinstance(reasoning["thinking"]["supported"], bool) else None,
            reasoning_can_disable=reasoning["thinking"]["can_disable"] if isinstance(reasoning["thinking"]["can_disable"], bool) else None,
            reasoning_default_enabled=reasoning["thinking"]["default_enabled"] if isinstance(reasoning["thinking"]["default_enabled"], bool) else None,
            reasoning_disable_value=str(reasoning["thinking"]["disable_value"] or ""),
            reasoning_enable_value=str(reasoning["thinking"]["enable_value"] or ""),
            reasoning_auto_value=str(reasoning["thinking"]["auto_value"] or ""),
            reasoning_supports_auto=reasoning["thinking"]["supports_auto"] if isinstance(reasoning["thinking"]["supports_auto"], bool) else None,
            reasoning_fixed_level=str(reasoning["effort"]["fixed_level"] or ""),
            capability_source=str(reasoning.get("source") or row.get("capability_source") or model.capability_source),
            tool_use=cap.get("tool_use") if isinstance(cap.get("tool_use"), bool) else None,
            transport=str(row.get("transport") or getattr(model, "transport", "") or "").strip().lower(),
            invocation_target=str(row.get("invocation_target") or getattr(model, "invocation_target", "") or model.id),
            invocation_target_kind=str(row.get("invocation_target_kind") or getattr(model, "invocation_target_kind", "model") or "model"),
            lifecycle=str(row.get("lifecycle") or getattr(model, "lifecycle", "active") or "active"),
            catalog_source=str(row.get("catalog_source") or getattr(model, "catalog_source", "provider-discovery") or "provider-discovery"),
            routing_quality=routing["quality"], routing_speed=routing["speed"],
            routing_economy=routing["economy"], routing_tiers=tuple(routing["tiers"]),
            routing_source=str(routing.get("source") or "unknown"),
            routing_confidence=str(routing.get("confidence") or "unknown"),
            routing_provenance=tuple(
                (field, str(meta.get("source") or "unknown"), str(meta.get("confidence") or "unknown"))
                for field, meta in sorted((routing.get("provenance") or {}).items())
                if isinstance(meta, dict)
            ),
        ))
    return out


def _is_official_base(preset: Any, base_url: str) -> bool:
    return official_endpoint_for_preset(preset, base_url)


def _fallback_catalogue(preset: Any, base_url: str) -> tuple[str, str, list[DiscoveredModel]] | None:
    """Return the bundled entitlement snapshot for an official profile.

    This is deliberately a fallback, not an allowlist. Live discovery is tried
    first so a provider/plan can add models without waiting for a Kiro BYO
    release.
    """
    preset_id = str(getattr(preset, "id", "custom"))
    record = connection_record(preset_id, base_url)
    profile = record[2] if record is not None else None
    model_ids = [str(value or "").strip() for value in (getattr(profile, "static_model_ids", ()) or ())]
    model_ids = [value for value in model_ids if value]
    if not model_ids or not _is_official_base(preset, base_url):
        return None
    discovered_id, discovered_name = infer_provider_identity(base_url)
    rows = [
        DiscoveredModel(
            model_id, model_id, capability_source="official-profile",
            invocation_target=model_id, lifecycle="", catalog_source="bundled-fallback",
        )
        for model_id in model_ids
    ]
    models = _profile_discovered_models(preset_id, rows)
    return discovered_id, discovered_name, models


def _discover_for_preset(preset: Any, base_url: str, api_key: str) -> tuple[str, str, list[DiscoveredModel]]:
    """Run live model discovery and fall back only when the profile declares it."""
    record = connection_record(str(getattr(preset, "id", "custom")), base_url)
    profile = record[2] if record is not None else None
    policy = str(getattr(profile, "catalog_policy", "live") or "live").strip().lower()
    if policy == "fallback":
        fallback = _fallback_catalogue(preset, base_url)
        if fallback is not None:
            return fallback

    discovery_url = str(getattr(profile, "discovery_url", "") or "").strip()
    auth_style = str(getattr(profile, "discovery_auth", getattr(preset, "discovery_auth", "bearer")) or "bearer")
    discovery_headers = tuple(getattr(profile, "discovery_headers", ()) or ())
    if not _is_official_base(preset, base_url):
        discovery_url = ""
        discovery_headers = ()
        auth_style = str(getattr(preset, "discovery_auth", "bearer") or "bearer")
    try:
        discovered_id, discovered_name, models = discover_models(
            base_url,
            api_key,
            endpoint=discovery_url,
            auth_style=auth_style,
            extra_headers=dict(discovery_headers),
        )
    except DiscoveryError:
        if policy == "live-with-fallback":
            fallback = _fallback_catalogue(preset, base_url)
            if fallback is not None:
                return fallback
        raise

    if _is_official_base(preset, base_url):
        models = filter_agent_models(models)
        allow = tuple(str(item).strip() for item in (getattr(profile, "discovery_model_allowlist", ()) or ()) if str(item).strip())
        if allow:
            allowed = set(allow)
            models = [model for model in models if model.id in allowed]
        else:
            models = _filter_discovered_models(preset, models)
        models = _profile_discovered_models(str(getattr(preset, "id", "custom")), models)
    return discovered_id, discovered_name, models


def _cached_discovered_models(provider: dict[str, Any] | None) -> list[DiscoveredModel]:
    """Use the last successful saved catalogue when live discovery is down."""
    if not isinstance(provider, dict):
        return []
    result: list[DiscoveredModel] = []
    for raw in provider.get("models") or []:
        if not isinstance(raw, dict) or not str(raw.get("id") or "").strip():
            continue
        cap = model_capability_from_row(raw)
        reasoning = cap["reasoning"]
        cached_row = dict(raw)
        cached_row["catalog_source"] = "cache"
        cached_routing = dict(raw.get("routing") or {}) if isinstance(raw.get("routing"), dict) else {}
        cached_routing.pop("source", None)
        cached_routing.pop("confidence", None)
        cached_routing.pop("provenance", None)
        if cached_routing:
            cached_row["routing"] = cached_routing
        routing = model_routing_from_row(cached_row)
        model_id = str(raw.get("id") or "").strip()
        result.append(DiscoveredModel(
            id=model_id,
            name=str(raw.get("name") or model_id),
            context_window=cap.get("context_window"),
            max_output_tokens=cap.get("max_output_tokens"),
            input_modalities=tuple(cap.get("input_modalities") or ()),
            output_modalities=tuple(cap.get("output_modalities") or ()),
            effort_supported_levels=tuple(reasoning["effort"]["levels"]),
            effort_default_level=str(reasoning["effort"]["default"] or ""),
            reasoning_mode=str(reasoning["mode"]),
            reasoning_supported=reasoning["thinking"]["supported"] if isinstance(reasoning["thinking"]["supported"], bool) else None,
            reasoning_can_disable=reasoning["thinking"]["can_disable"] if isinstance(reasoning["thinking"]["can_disable"], bool) else None,
            reasoning_default_enabled=reasoning["thinking"]["default_enabled"] if isinstance(reasoning["thinking"]["default_enabled"], bool) else None,
            reasoning_disable_value=str(reasoning["thinking"]["disable_value"] or ""),
            reasoning_enable_value=str(reasoning["thinking"]["enable_value"] or ""),
            reasoning_auto_value=str(reasoning["thinking"]["auto_value"] or ""),
            reasoning_supports_auto=reasoning["thinking"]["supports_auto"] if isinstance(reasoning["thinking"]["supports_auto"], bool) else None,
            reasoning_fixed_level=str(reasoning["effort"]["fixed_level"] or ""),
            capability_source=str(raw.get("capability_source") or reasoning.get("source") or "cache"),
            tool_use=cap.get("tool_use") if isinstance(cap.get("tool_use"), bool) else None,
            transport=str(raw.get("transport") or "").strip().lower(),
            invocation_target=str(raw.get("invocation_target") or model_id),
            invocation_target_kind=str(raw.get("invocation_target_kind") or "model"),
            lifecycle=str(raw.get("lifecycle") or "active"),
            catalog_source="cache",
            routing_quality=routing["quality"], routing_speed=routing["speed"],
            routing_economy=routing["economy"], routing_tiers=tuple(routing["tiers"]),
            routing_source=str(routing.get("source") or "unknown"),
            routing_confidence=str(routing.get("confidence") or "unknown"),
            routing_provenance=tuple(
                (field, str(meta.get("source") or "unknown"), str(meta.get("confidence") or "unknown"))
                for field, meta in sorted((routing.get("provenance") or {}).items())
                if isinstance(meta, dict)
            ),
        ))
    return result


def _cached_provider_candidate(preset_id: str, base_url: str, instance_id: str) -> dict[str, Any] | None:
    raw = _read_raw()
    providers = raw.get("providers") if isinstance(raw.get("providers"), list) else []
    if str(instance_id or "").strip():
        wanted = _normalize_instance_id(instance_id)
        return next((p for p in providers if isinstance(p, dict) and str(p.get("id") or "") == wanted), None)
    current = str(base_url or "").rstrip("/")
    return next((
        p for p in providers
        if isinstance(p, dict)
        and str(p.get("preset_id") or "custom") == str(preset_id or "custom")
        and str(p.get("base_url") or "").rstrip("/") == current
    ), None)


def _preserve_model_thinking_preferences(existing: dict[str, Any] | None, models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Carry per-model user policy across discovery/profile refreshes."""
    if not isinstance(existing, dict):
        return models
    old_rows = existing.get("models") if isinstance(existing.get("models"), list) else []
    old = {
        str(row.get("id") or ""): normalize_thinking_preference(row.get("thinking_preference"))
        for row in old_rows
        if isinstance(row, dict) and str(row.get("id") or "")
    }
    for row in models:
        model_id = str(row.get("id") or "")
        if model_id in old:
            row["thinking_preference"] = old[model_id]
    return models


@_serialized_config_mutation
def set_model_thinking_preference(
    provider_id: str, model_id: str, preference: str, *, store: CredentialStore | None = None
) -> dict[str, Any]:
    """Persist one model's BYO deep-thinking policy (auto/on/off)."""
    store = store or _store()
    value = str(preference or "").strip().lower()
    if value not in {"auto", "on", "off"}:
        raise ConfigError("thinking preference must be one of: auto, on, off")
    raw = _read_raw()
    provider = next(
        (p for p in raw.get("providers", []) if isinstance(p, dict) and str(p.get("id") or "") == provider_id),
        None,
    )
    if not isinstance(provider, dict):
        raise ConfigError(f"unknown provider {provider_id!r}")
    rows = provider.get("models") if isinstance(provider.get("models"), list) else []
    target = next(
        (m for m in rows if isinstance(m, dict) and str(m.get("id") or "") == model_id),
        None,
    )
    if not isinstance(target, dict):
        raise ConfigError(f"unknown model {model_id!r} for provider {provider_id!r}")
    preset = provider_preset(str(provider.get("preset_id") or "custom"))
    official = official_endpoint_for_preset(preset, str(provider.get("base_url") or "")) if preset is not None else False
    effective = merge_official_model_profile(str(provider.get("preset_id") or "custom"), target) if official else dict(target)
    cap = model_capability_from_row(effective)
    thinking = (cap.get("reasoning") or {}).get("thinking") or {}
    if thinking.get("supported") is not True or thinking.get("can_disable") is not True:
        raise ConfigError(f"model {model_id!r} does not support a user-selectable deep-thinking switch")
    target["thinking_preference"] = value
    _write_raw(raw)
    return _safe_provider(provider, store)


def probe_provider_connection(*, base_url: str, api_key: str, preset_id: str = "custom") -> dict[str, Any]:
    """Validate one Provider connection without persisting config or credentials."""
    preset = provider_preset(preset_id) or provider_preset("custom")
    assert preset is not None
    if not preset.available:
        raise ConfigError(preset.note or f"provider preset {preset.id} is not available in this release")

    base_url = str(base_url or preset.default_base_url or "").strip().rstrip("/")
    if not base_url:
        raise ConfigError("base_url is required")
    if not isinstance(api_key, str):
        raise ConfigError("api_key must be a string")
    if preset.requires_api_key and not api_key:
        raise ConfigError(f"{preset.display_name} requires an API key")

    credential_profile = credential_connection_profile(preset.id, api_key)
    if credential_profile is not None and base_url.rstrip("/") == preset.default_base_url.rstrip("/"):
        _platform, _product, hinted_profile = credential_profile
        base_url = hinted_profile.base_url.rstrip("/")

    rejection = credential_rejection_message(preset.id, api_key)
    if rejection:
        raise ConfigError(rejection)
    if official_endpoint_for_preset(preset, base_url):
        base_url = migrate_official_base_url(preset, base_url)

    try:
        discovered_id, discovered_name, models = _discover_for_preset(preset, base_url, api_key)
    except DiscoveryError as exc:
        message = str(exc)
        if api_key:
            message = message.replace(api_key, "[REDACTED]")
        raise DiscoveryError(message) from exc
    if not models:
        raise DiscoveryError("model catalogue returned no usable models")

    effective_transport = preset.transport if _is_official_base(preset, base_url) else "openai-compatible"
    return {
        "ok": True,
        "provider_id": discovered_id or preset.id,
        "provider_name": discovered_name or preset.display_name,
        "base_url": base_url,
        "transport_label": transport_label(effective_transport),
        "model_count": len(models),
    }


def configure_provider(
    *,
    base_url: str,
    api_key: str,
    store: CredentialStore | None = None,
    provider_name: str = "",
    preset_id: str = "custom",
    manual_models: list[str] | None = None,
    instance_id: str = "",
    create_new: bool = False,
) -> dict[str, Any]:
    """Discover/save a provider instance using its safest verified transport.

    Official endpoints use their provider-native adapter when Kiro BYO has one.
    A user-edited endpoint falls back to OpenAI-compatible transport instead of
    assuming a relay implements the vendor's native protocol.
    """

    store = store or _store()
    preset = provider_preset(preset_id) or provider_preset("custom")
    assert preset is not None
    if not preset.available:
        raise ConfigError(preset.note or f"provider preset {preset.id} is not available in this release")

    base_url = str(base_url or preset.default_base_url or "").strip().rstrip("/")
    if not base_url:
        raise ConfigError("base_url is required")
    if not isinstance(api_key, str):
        raise ConfigError("api_key must be a string")
    if preset.requires_api_key and not api_key:
        raise ConfigError(f"{preset.display_name} requires an API key")

    # Some platforms issue distinct credential families for commercial plans.
    # Product metadata may route a credential pasted on the platform's default
    # endpoint to that Product's default Profile without adding provider code.
    # An explicitly selected regional/profile URL always wins.
    credential_profile = credential_connection_profile(preset.id, api_key)
    if credential_profile is not None and base_url.rstrip("/") == preset.default_base_url.rstrip("/"):
        _platform, _product, hinted_profile = credential_profile
        base_url = hinted_profile.base_url.rstrip("/")

    rejection = credential_rejection_message(preset.id, api_key)
    if rejection:
        raise ConfigError(rejection)
    if official_endpoint_for_preset(preset, base_url):
        base_url = migrate_official_base_url(preset, base_url)

    cached_candidate = _cached_provider_candidate(preset.id, base_url, instance_id)
    models: list[DiscoveredModel] = []
    discovered_id = ""
    discovered_name = ""
    discovery_error: DiscoveryError | None = None
    try:
        discovered_id, discovered_name, models = _discover_for_preset(preset, base_url, api_key)
    except DiscoveryError as exc:
        discovery_error = exc
        models = _cached_discovered_models(cached_candidate)
        if models:
            discovered_id, discovered_name = infer_provider_identity(base_url)

    if not models and manual_models:
        models = _manual_discovered_models(manual_models)
        if _is_official_base(preset, base_url):
            models = _profile_discovered_models(preset.id, models)
        discovered_id, discovered_name = infer_provider_identity(base_url)
    if not models:
        discovery_error = discovery_error or DiscoveryError("model catalogue returned no usable models")
        message = str(discovery_error)
        if api_key:
            message = message.replace(api_key, "[REDACTED]")
        raise DiscoveryError(message + "; provide model ids manually if this provider does not expose a compatible model catalogue") from discovery_error

    return _commit_configured_provider(
        base_url=base_url, api_key=api_key, store=store,
        provider_name=provider_name, preset=preset, instance_id=instance_id,
        create_new=create_new, credential_profile=credential_profile,
        discovered_id=discovered_id, discovered_name=discovered_name,
        models=models,
    )


@_serialized_config_mutation
def _commit_configured_provider(
    *, base_url: str, api_key: str, store: CredentialStore,
    provider_name: str, preset: Any, instance_id: str,
    create_new: bool, credential_profile: Any,
    discovered_id: str, discovered_name: str, models: list[DiscoveredModel],
) -> dict[str, Any]:
    """Commit an already-discovered catalogue under the shared config lock.

    Re-read the latest persisted configuration here so model discovery never
    holds the cross-process config lock or overwrites unrelated changes.
    """
    # Normalize older configs before marking the config as schema v1.
    migrate_plaintext_credentials(store=store)
    raw = _read_raw()
    providers = raw.setdefault("providers", [])
    if not isinstance(providers, list):
        raise ConfigError("providers must be an array")

    # ``preset_id`` is the vendor/transport family; ``id`` is one concrete
    # configured instance. Existing configs already satisfy this because their
    # ids are unique, so the migration is additive and backwards compatible.
    requested_instance = _normalize_instance_id(instance_id) if instance_id.strip() else ""
    existing = None
    if requested_instance:
        existing = next(
            (p for p in providers if isinstance(p, dict) and str(p.get("id") or "") == requested_instance),
            None,
        )
        if existing is None:
            raise ConfigError(f"unknown provider instance {requested_instance!r}")
        provider_id = requested_instance
        ordinal = 1
    elif not create_new:
        # Legacy configure-provider semantics: re-running setup for the same
        # family + endpoint updates that instance instead of creating duplicates.
        existing = next(
            (
                p for p in providers
                if isinstance(p, dict)
                and str(p.get("preset_id") or "custom") == preset.id
                and str(p.get("base_url") or "").rstrip("/") == base_url
            ),
            None,
        )
        if isinstance(existing, dict):
            provider_id = str(existing.get("id") or "")
            ordinal = 1
        else:
            base_id = preset.id if preset.id != "custom" else (discovered_id or "provider")
            provider_id, ordinal = _unique_instance_id(base_id, providers)
    else:
        base_id = preset.id if preset.id != "custom" else (discovered_id or "provider")
        provider_id, ordinal = _unique_instance_id(base_id, providers)

    base_display = preset.display_name if preset.id != "custom" else (discovered_name or "自定义 / 中转站")
    if provider_name.strip():
        display_name = provider_name.strip()
    elif isinstance(existing, dict) and str(existing.get("name") or "").strip():
        display_name = str(existing.get("name") or "").strip()
    else:
        # Make multiple accounts/endpoints visually distinguishable without
        # forcing the user to type a label. The first keeps the familiar name.
        same_family = [
            p for p in providers
            if isinstance(p, dict) and str(p.get("preset_id") or "custom") == preset.id
        ]
        number = len(same_family) + 1 if create_new or existing is None else max(1, ordinal)
        display_name = base_display if number <= 1 else f"{base_display} #{number}"

    ref = str(existing.get("credential_ref") or "").strip() if isinstance(existing, dict) else ""
    if api_key:
        if not ref or ref.startswith("env:"):
            ref = provider_credential_ref(provider_id)
        current = decode_provider_secrets(store.get(ref))
        store.set(ref, encode_provider_secrets(ProviderSecrets(api_key=api_key, headers=current.headers)))
    elif preset.requires_api_key:
        raise ConfigError(f"{preset.display_name} requires an API key")

    transport = preset.transport if _is_official_base(preset, base_url) else "openai-compatible"
    identity = connection_identity(preset.id, base_url)
    platform_id, product_id, profile_id, entitlement = (
        identity.platform_id, identity.product_id, identity.profile_id, identity.entitlement
    )
    if credential_profile is not None:
        hinted_platform, hinted_product, hinted_profile = credential_profile
        if hinted_profile.base_url.rstrip("/") == base_url.rstrip("/"):
            platform_id = hinted_platform.id
            product_id = hinted_product.id
            profile_id = hinted_profile.id
            entitlement = hinted_profile.entitlement
    provider: dict[str, Any] = {
        "id": provider_id,
        "name": display_name,
        "type": transport,
        "preset_id": preset.id,
        "platform_id": platform_id,
        "product_id": product_id,
        "profile_id": profile_id,
        "entitlement": entitlement,
        "base_url": base_url,
        "stream": True,
        "timeout_seconds": 120,
        "models": _preserve_model_thinking_preferences(existing, [_model_dict(model) for model in models]),
        "auto_discover_models": not all(str(getattr(model, "catalog_source", "")) == "manual" for model in models),
        "catalog_status": (models[0].catalog_source if models and len({m.catalog_source for m in models}) == 1 else "mixed"),
        "metadata_schema_version": 7,
    }
    if ref:
        provider["credential_ref"] = ref

    # Preserve explicitly configured non-secret transport knobs.
    if isinstance(existing, dict):
        for key in ("chat_completions_path", "headers", "extra_body", "temperature"):
            if key in existing:
                provider[key] = existing[key]

    if isinstance(existing, dict):
        providers[providers.index(existing)] = provider
    else:
        providers.append(provider)
    raw["credential_schema_version"] = 1
    raw["model_capability_schema_version"] = 2
    raw["provider_instance_schema_version"] = 1
    raw["provider_product_schema_version"] = 1
    _repair_default_models(raw)
    _write_raw(raw)
    return _safe_provider(provider, store)


@_serialized_config_mutation
def update_provider_credential(
    provider_id: str,
    api_key: str,
    *,
    store: CredentialStore | None = None,
) -> dict[str, Any]:
    store = store or _store()
    raw = _read_raw()
    provider = next(
        (p for p in raw.get("providers", []) if isinstance(p, dict) and str(p.get("id") or "") == provider_id),
        None,
    )
    if not isinstance(provider, dict):
        raise ConfigError(f"unknown provider {provider_id!r}")
    ref = str(provider.get("credential_ref") or "").strip()
    if not ref or ref.startswith("env:"):
        ref = provider_credential_ref(provider_id)
    current = decode_provider_secrets(store.get(ref))
    store.set(ref, encode_provider_secrets(ProviderSecrets(api_key=api_key, headers=current.headers)))
    if provider.get("credential_ref") != ref:
        provider["credential_ref"] = ref
        provider.pop("api_key", None)
        _write_raw(raw)
    return _safe_provider(provider, store)


def refresh_provider_models(provider_id: str, *, store: CredentialStore | None = None) -> dict[str, Any]:
    """Perform slow network discovery outside the cross-process config lock."""
    store = store or _store()
    raw = _read_raw()
    provider = next(
        (p for p in raw.get("providers", []) if isinstance(p, dict) and str(p.get("id") or "") == provider_id),
        None,
    )
    if not isinstance(provider, dict):
        raise ConfigError(f"unknown provider {provider_id!r}")
    base_url = str(provider.get("base_url") or "").strip()
    ref = str(provider.get("credential_ref") or "").strip()
    if ref:
        stored = store.get(ref)
        if stored is None:
            raise CredentialStoreError(f"provider {provider_id} credential {ref!r} is not configured")
        secrets = decode_provider_secrets(stored)
    else:
        secrets = ProviderSecrets()
    preset_id = str(provider.get("preset_id") or "custom")
    preset = provider_preset(preset_id) or provider_preset("custom")
    assert preset is not None
    try:
        discovered_id, discovered_name, models = _discover_for_preset(preset, base_url, secrets.api_key)
    except DiscoveryError as exc:
        message = str(exc)
        if secrets.api_key:
            message = message.replace(secrets.api_key, "[REDACTED]")
        raise DiscoveryError(message) from exc
    return _commit_refreshed_provider_models(
        provider_id=provider_id, store=store, base_url=base_url,
        credential_ref=ref, preset_id=preset_id,
        discovery_api_key=secrets.api_key,
        discovered_name=discovered_name, models=models,
    )


@_serialized_config_mutation
def _commit_refreshed_provider_models(
    *, provider_id: str, store: CredentialStore, base_url: str,
    credential_ref: str, preset_id: str, discovery_api_key: str,
    discovered_name: str, models: list[DiscoveredModel],
) -> dict[str, Any]:
    """Apply discovered models to the latest provider state, or reject stale discovery."""
    raw = _read_raw()
    provider = next(
        (p for p in raw.get("providers", []) if isinstance(p, dict) and str(p.get("id") or "") == provider_id),
        None,
    )
    if not isinstance(provider, dict):
        raise ConfigError(f"provider {provider_id!r} was removed during model discovery")
    if (
        str(provider.get("base_url") or "").strip() != base_url
        or str(provider.get("credential_ref") or "").strip() != credential_ref
        or str(provider.get("preset_id") or "custom") != preset_id
    ):
        raise ConfigError("provider configuration changed during model discovery; please retry")
    # A credential update may keep the same reference; old-key discovery must
    # never replace the catalogue for a newly authorized account.
    if credential_ref:
        current_secret = store.get(credential_ref)
        if current_secret is None or decode_provider_secrets(current_secret).api_key != discovery_api_key:
            raise ConfigError("provider credentials changed during model discovery; please retry")
    provider["name"] = str(provider.get("name") or discovered_name)
    provider["models"] = _preserve_model_thinking_preferences(provider, [_model_dict(model) for model in models])
    provider["metadata_schema_version"] = 7
    _repair_default_models(raw)
    _write_raw(raw)
    return _safe_provider(provider, store)


@_serialized_config_mutation
def set_default_model(provider_id: str, model_id: str, *, store: CredentialStore | None = None) -> dict[str, Any]:
    store = store or _store()
    raw = _read_raw()
    provider = next(
        (p for p in raw.get("providers", []) if isinstance(p, dict) and str(p.get("id") or "") == provider_id),
        None,
    )
    if not isinstance(provider, dict):
        raise ConfigError(f"unknown provider {provider_id!r}")
    models = provider.get("models") if isinstance(provider.get("models"), list) else []
    if not any(isinstance(m, dict) and str(m.get("id") or "") == model_id for m in models):
        raise ConfigError(f"unknown model {model_id!r} for provider {provider_id!r}")
    provider["default_model"] = model_id
    raw["default_model"] = f"{provider_id}/{model_id}"
    _write_raw(raw)
    return _safe_provider(provider, store)


@_serialized_config_mutation
def delete_provider(provider_id: str, *, store: CredentialStore | None = None) -> None:
    store = store or _store()
    raw = _read_raw()
    providers = raw.get("providers", [])
    target = next(
        (p for p in providers if isinstance(p, dict) and str(p.get("id") or "") == provider_id),
        None,
    )
    if not isinstance(target, dict):
        raise ConfigError(f"unknown provider {provider_id!r}")
    ref = str(target.get("credential_ref") or "").strip()
    providers.remove(target)
    # Normally each provider instance owns a unique credential reference.  Do
    # not delete a legacy/shared reference if another configured instance still
    # points at it.
    ref_still_used = bool(ref) and any(
        isinstance(row, dict) and str(row.get("credential_ref") or "").strip() == ref
        for row in providers
    )
    if ref and not ref.startswith("env:") and not ref_still_used:
        store.delete(ref)
    _repair_default_models(raw)
    _write_raw(raw)



@_serialized_config_mutation
def clear_provider_configuration(*, store: CredentialStore | None = None) -> dict[str, int]:
    """Remove all configured provider instances and their owned credentials.

    Environment references are never deleted. Shared legacy references are only
    deleted once even if more than one old provider row points at them.
    """
    store = store or _store()
    raw = _read_raw()
    refs = {
        str(row.get("credential_ref") or "").strip()
        for row in raw.get("providers", [])
        if isinstance(row, dict) and str(row.get("credential_ref") or "").strip()
    }
    deleted_credentials = 0
    for ref in sorted(refs):
        if ref.startswith("env:"):
            continue
        try:
            store.delete(ref)
            deleted_credentials += 1
        except CredentialStoreError:
            # A full purge should continue cleaning local metadata even when the
            # OS vault has already lost a reference. The caller can verify that
            # no configured refs remain afterwards.
            pass
    path = config_path()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise ConfigError(f"could not remove {path}: {exc}") from exc
    return {"providers": len([p for p in raw.get("providers", []) if isinstance(p, dict)]), "credentials": deleted_credentials}
