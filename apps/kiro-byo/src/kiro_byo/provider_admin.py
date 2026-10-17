"""Safe provider configuration and credential administration.

This module is shared by the CLI and the localhost management UI.  Secrets are
never included in returned provider dictionaries and are never written back to
``providers.json`` after migration.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigError, config_path
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
from .discovery import DiscoveryError, DiscoveredModel, discover_models, infer_provider_identity
from .provider_catalog import merge_official_model_profile, model_capability_from_row, provider_preset, public_presets, transport_label
from .policy_packs import PolicyPackError, default_pack, get_policy_pack, public_policy_packs, resolve_policy
from .reasoning_control import normalize_thinking_preference


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
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    text = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _store() -> CredentialStore:
    return default_credential_store()


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
    }


def _official_endpoint_for_preset(preset: Any, base_url: str) -> bool:
    if preset is None or str(getattr(preset, "id", "custom")) == "custom":
        return False
    current = str(base_url or "").rstrip("/")
    official = {
        str(getattr(preset, "default_base_url", "") or "").rstrip("/"),
        *(str(value or "").rstrip("/") for value in (getattr(preset, "official_base_urls", ()) or ())),
    }
    official.discard("")
    if current in official:
        return True
    # 0.9.19 originally used Gemini's OpenAI-compatible path. Keep only that
    # one historical alias as an official endpoint for UI/runtime migration.
    if str(getattr(preset, "id", "")) == "gemini" and current == "https://generativelanguage.googleapis.com/v1beta/openai":
        return True
    return False


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
    preset = provider_preset(str(provider.get("preset_id") or "custom"))
    credential_required = bool(preset.requires_api_key) if preset is not None else True
    official_endpoint = _official_endpoint_for_preset(preset, str(provider.get("base_url") or ""))

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
        "base_url": str(provider.get("base_url") or ""),
        "model_count": len(raw_models),
        "preset_id": str(provider.get("preset_id") or "custom"),
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
        "policy": policy.id,
        "policy_label": policy.display_name,
        "catalog": public_policy_packs(),
    }


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
    value = str(policy or selected_pack.default_policy).strip().lower()
    try:
        selected_pack, selected_policy = resolve_policy(pack_id, value)
    except PolicyPackError as exc:
        raise ConfigError(str(exc)) from exc
    raw["mode"] = {"id": selected_pack.id, "type": selected_policy.id}
    # Mode selection has no provider/model default. Packs describe execution policy only
    # and never own model capability metadata or provider credentials.
    raw.pop("default_model", None)
    for row in raw.get("providers", []):
        if isinstance(row, dict):
            row.pop("default_model", None)
    _write_raw(raw)
    return mode_settings()


def set_mode_pack(pack: str) -> dict[str, Any]:
    """Switch packs atomically, selecting the new pack's declared default."""
    selected = get_policy_pack(str(pack or "").strip().lower())
    if selected is None:
        raise ConfigError(f"policy pack {pack!r} is not installed")
    return set_mode_selection(selected.default_policy, selected.id)


def list_provider_presets() -> list[dict[str, Any]]:
    return public_presets()


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
        models.append(DiscoveredModel(model_id, model_id, capability_source="manual"))
    return models




def _filter_discovered_models(preset: Any, models: list[DiscoveredModel]) -> list[DiscoveredModel]:
    allow = tuple(str(item).strip() for item in getattr(preset, "discovery_model_allowlist", ()) if str(item).strip())
    if not allow:
        return models
    allowed = set(allow)
    return [model for model in models if model.id in allowed]


def _profile_discovered_models(preset_id: str, models: list[DiscoveredModel]) -> list[DiscoveredModel]:
    """Overlay only verified official model facts on discovery/manual rows."""
    out: list[DiscoveredModel] = []
    for model in models:
        row = merge_official_model_profile(preset_id, _model_dict(model))
        cap = model_capability_from_row(row)
        reasoning = cap["reasoning"]
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
        ))
    return out


def _official_base_urls(preset: Any) -> set[str]:
    urls = {str(getattr(preset, "default_base_url", "") or "").rstrip("/")}
    urls.update(str(value or "").rstrip("/") for value in getattr(preset, "official_base_urls", ()) or ())
    return {value for value in urls if value}


def _is_official_base(preset: Any, base_url: str) -> bool:
    return str(base_url or "").rstrip("/") in _official_base_urls(preset)


def _static_catalogue(preset: Any, base_url: str) -> tuple[str, str, list[DiscoveredModel]] | None:
    preset_id = str(getattr(preset, "id", "custom"))
    current_base = str(base_url or "").rstrip("/")

    # Xiaomi Token Plan is presented as the same vendor in the UI. Its official
    # subscription endpoints do not expose the same /models catalogue as the
    # pay-as-you-go endpoint, so use the documented Agent-capable entitlement.
    # The legacy xiaomi-tokenplan preset below remains supported for old configs.
    xiaomi_token_plan_bases = {
        "https://token-plan-cn.xiaomimimo.com/v1",
        "https://token-plan-sgp.xiaomimimo.com/v1",
        "https://token-plan-ams.xiaomimimo.com/v1",
    }
    if preset_id == "xiaomi" and current_base in xiaomi_token_plan_bases:
        model_ids = ["mimo-v2.6-pro", "mimo-v2.6-flash"]
    else:
        model_ids = [str(value or "").strip() for value in getattr(preset, "static_model_ids", ()) or ()]
        model_ids = [value for value in model_ids if value]

    if not model_ids or not _is_official_base(preset, base_url):
        return None
    discovered_id, discovered_name = infer_provider_identity(base_url)
    models = _profile_discovered_models(preset_id, _manual_discovered_models(model_ids))
    return discovered_id, discovered_name, models


def _discover_for_preset(preset: Any, base_url: str, api_key: str) -> tuple[str, str, list[DiscoveredModel]]:
    """Run model discovery using the selected preset's catalogue contract."""
    static = _static_catalogue(preset, base_url)
    if static is not None:
        return static

    default_base = str(getattr(preset, "default_base_url", "") or "").rstrip("/")
    discovery_url = str(getattr(preset, "discovery_url", "") or "").strip()
    # A user-edited endpoint should be treated as its own compatible service; a
    # hard-coded vendor catalogue is only safe for the preset's official base.
    if discovery_url and default_base and base_url.rstrip("/") != default_base:
        discovery_url = ""
    discovered_id, discovered_name, models = discover_models(
        base_url,
        api_key,
        endpoint=discovery_url,
        auth_style=str(getattr(preset, "discovery_auth", "bearer") or "bearer"),
        extra_headers=dict(getattr(preset, "discovery_headers", ()) or ()),
    )
    # Official profiles apply to every first-party endpoint declared by the
    # preset, including subscription/region endpoints. Relays stay conservative.
    if _is_official_base(preset, base_url):
        models = _filter_discovered_models(preset, models)
        models = _profile_discovered_models(str(getattr(preset, "id", "custom")), models)
    return discovered_id, discovered_name, models

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
    official = _official_endpoint_for_preset(preset, str(provider.get("base_url") or "")) if preset is not None else False
    effective = merge_official_model_profile(str(provider.get("preset_id") or "custom"), target) if official else dict(target)
    cap = model_capability_from_row(effective)
    thinking = (cap.get("reasoning") or {}).get("thinking") or {}
    if thinking.get("supported") is not True or thinking.get("can_disable") is not True:
        raise ConfigError(f"model {model_id!r} does not support a user-selectable deep-thinking switch")
    target["thinking_preference"] = value
    _write_raw(raw)
    return _safe_provider(provider, store)


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

    # Xiaomi is one vendor entry in the Provider picker. A Token Plan key pasted
    # while the default pay-as-you-go URL is still selected is routed to the
    # Token Plan CN endpoint automatically. Users can still choose the official
    # SGP/AMS subscription endpoints by editing Base URL. The old
    # ``xiaomi-tokenplan`` preset is kept only for existing saved configs.
    key_prefix = api_key.strip().lower()
    if preset.id == "xiaomi" and (key_prefix.startswith("tp-") or key_prefix.startswith("ttp-")):
        if base_url.rstrip("/") == "https://api.xiaomimimo.com/v1":
            base_url = "https://token-plan-cn.xiaomimimo.com/v1"
    if preset.id == "xiaomi-tokenplan" and key_prefix.startswith("sk-"):
        raise ConfigError("检测到小米按量付费 API Key（sk-）。请使用“小米 MiMo / Xiaomi”统一入口。")

    models: list[DiscoveredModel] = []
    discovered_id = ""
    discovered_name = ""
    discovery_error: DiscoveryError | None = None
    try:
        discovered_id, discovered_name, models = _discover_for_preset(preset, base_url, api_key)
    except DiscoveryError as exc:
        discovery_error = exc

    if not models and manual_models:
        models = _manual_discovered_models(manual_models)
        if _is_official_base(preset, base_url):
            models = _profile_discovered_models(preset.id, models)
        discovered_id, discovered_name = infer_provider_identity(base_url)
    if not models:
        assert discovery_error is not None
        message = str(discovery_error)
        if api_key:
            message = message.replace(api_key, "[REDACTED]")
        raise DiscoveryError(message + "; provide model ids manually if this provider does not expose a compatible model catalogue") from discovery_error

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
    provider: dict[str, Any] = {
        "id": provider_id,
        "name": display_name,
        "type": transport,
        "preset_id": preset.id,
        "base_url": base_url,
        "stream": True,
        "timeout_seconds": 120,
        "models": _preserve_model_thinking_preferences(existing, [_model_dict(model) for model in models]),
        "auto_discover_models": not bool(manual_models and discovery_error is not None),
        "metadata_schema_version": 5,
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
    _write_raw(raw)
    return _safe_provider(provider, store)


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
    preset = provider_preset(str(provider.get("preset_id") or "custom")) or provider_preset("custom")
    assert preset is not None
    try:
        discovered_id, discovered_name, models = _discover_for_preset(preset, base_url, secrets.api_key)
    except DiscoveryError as exc:
        message = str(exc)
        if secrets.api_key:
            message = message.replace(secrets.api_key, "[REDACTED]")
        raise DiscoveryError(message) from exc
    if discovered_id != provider_id:
        # Keep the persisted routing id stable, as metadata_refresh does.
        discovered_id = provider_id
    provider["name"] = str(provider.get("name") or discovered_name)
    provider["models"] = _preserve_model_thinking_preferences(provider, [_model_dict(model) for model in models])
    available = {model.id for model in models}
    provider["metadata_schema_version"] = 5
    _write_raw(raw)
    return _safe_provider(provider, store)


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
    default = str(raw.get("default_model") or "")
    if default.startswith(provider_id + "/"):
        first = next((p for p in providers if isinstance(p, dict) and p.get("models")), None)
        if isinstance(first, dict):
            raw["default_model"] = f"{first.get('id','')}/{first.get('default_model','')}"
        else:
            raw["default_model"] = ""
    _write_raw(raw)



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
