"""Best-effort model metadata refresh for configs created by older Kiro BYO builds.

Older Kiro BYO builds discovered model IDs/names without the normalized per-model
capability metadata used by the 0.1.x line for live reasoning controls and provider UI. Rather than asking the user to re-enter an API key,
this module upgrades an existing auto-discovered provider once, in place, using
its already saved base URL/key.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import config_path
from .credential_store import (
    CredentialStoreError,
    decode_provider_secrets,
    default_credential_store,
)
from .discovery import DiscoveryError, DiscoveredModel, discover_models, filter_agent_models, infer_provider_identity
from .provider_catalog import connection_record, merge_official_model_profile, model_capability_from_row, model_routing_from_row, product_base_urls, provider_preset
from .reasoning_control import normalize_thinking_preference

METADATA_SCHEMA_VERSION = 7


def _model_dict(model, *, thinking_preference: str = "auto") -> dict[str, Any]:
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
        "thinking_preference": normalize_thinking_preference(thinking_preference),
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


def refresh_stale_model_metadata() -> bool:
    """Upgrade auto-discovered providers that predate rich model metadata.

    Returns True when the config file was replaced. Network/provider failures are
    deliberately non-fatal: an existing text chat must keep working offline or
    during a provider catalogue outage. The next fresh Kiro BYO process can retry.
    """

    path = config_path()
    try:
        initial_stat = path.stat()
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict):
        return False
    providers = raw.get("providers")
    if not isinstance(providers, list):
        return False

    changed = False
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if not bool(provider.get("auto_discover_models")):
            continue
        try:
            schema = int(provider.get("metadata_schema_version") or 0)
        except (TypeError, ValueError):
            schema = 0
        if schema >= METADATA_SCHEMA_VERSION:
            continue
        old_thinking_preferences = {
            str(row.get("id") or ""): normalize_thinking_preference(row.get("thinking_preference"))
            for row in (provider.get("models") or [])
            if isinstance(row, dict) and str(row.get("id") or "").strip()
        }
        base_url = str(provider.get("base_url") or "").strip()
        api_key = ""
        credential_ref = str(provider.get("credential_ref") or "").strip()
        if credential_ref:
            try:
                stored = default_credential_store().get(credential_ref)
                if stored is None:
                    continue
                api_key = decode_provider_secrets(stored).api_key
            except CredentialStoreError:
                continue
        if not base_url:
            continue
        preset = provider_preset(str(provider.get("preset_id") or "custom"))
        official_urls = set()
        if preset is not None:
            official_urls.add(preset.default_base_url.rstrip("/"))
            official_urls.update(str(value or "").rstrip("/") for value in (getattr(preset, "official_base_urls", ()) or ()) if str(value or "").strip())
            official_urls.update(product_base_urls(preset.id))
        official = bool(preset is not None and preset.id != "custom" and base_url.rstrip("/") in official_urls)
        record = connection_record(preset.id, base_url) if official and preset is not None else None
        profile = record[2] if record is not None else None
        policy = str(getattr(profile, "catalog_policy", "live") or "live").strip().lower()
        fallback_ids = [str(value or "").strip() for value in (getattr(profile, "static_model_ids", ()) or ())] if profile is not None else []
        fallback_ids = [value for value in fallback_ids if value]
        models: list[DiscoveredModel] = []
        try:
            if policy != "fallback":
                discovered_id, discovered_name, models = discover_models(
                    base_url,
                    api_key,
                    endpoint=(str(getattr(profile, "discovery_url", "") or "") if official else ""),
                    auth_style=(str(getattr(profile, "discovery_auth", getattr(preset, "discovery_auth", "bearer")) or "bearer") if official and preset is not None else "bearer"),
                    extra_headers=(dict(getattr(profile, "discovery_headers", ()) or ()) if official else {}),
                )
            else:
                raise DiscoveryError("profile requests bundled fallback catalogue")
        except DiscoveryError:
            if official and fallback_ids and policy in {"fallback", "live-with-fallback"}:
                discovered_id, discovered_name = infer_provider_identity(base_url)
                models = [DiscoveredModel(
                    model_id, model_id, capability_source="official-profile",
                    invocation_target=model_id, lifecycle="", catalog_source="bundled-fallback",
                ) for model_id in fallback_ids]
            else:
                # Keep the saved catalogue as the cache and retry later.
                continue
        if not models:
            continue
        if official and preset is not None:
            models = filter_agent_models(models)
            allow = {str(item).strip() for item in getattr(preset, "discovery_model_allowlist", ()) if str(item).strip()}
            if allow:
                models = [model for model in models if model.id in allow]
            upgraded = []
            for model in models:
                discovered_row = _model_dict(model)
                if model.catalog_source == "bundled-fallback" and not str(model.lifecycle or "").strip():
                    discovered_row.pop("lifecycle", None)
                row = merge_official_model_profile(preset.id, discovered_row)
                cap = model_capability_from_row(row)
                reasoning = cap["reasoning"]
                routing = model_routing_from_row(row)
                upgraded.append(DiscoveredModel(
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
            models = upgraded

        # The saved routing id is part of persisted selections. Keep it stable
        # even if a relay changes the identity inferred from its hostname.
        provider_id = str(provider.get("id") or discovered_id).strip() or discovered_id
        provider["id"] = provider_id
        if not str(provider.get("name") or "").strip():
            provider["name"] = discovered_name
        provider["models"] = [
            _model_dict(
                model,
                thinking_preference=old_thinking_preferences.get(model.id, "auto"),
            )
            for model in models
        ]
        provider["catalog_status"] = models[0].catalog_source if models and len({m.catalog_source for m in models}) == 1 else "mixed"

        available_ids = {model.id for model in models}
        current_default = str(provider.get("default_model") or "")
        if current_default not in available_ids:
            provider["default_model"] = models[0].id
        provider["metadata_schema_version"] = METADATA_SCHEMA_VERSION
        changed = True

    if not changed:
        return False

    # Keep the root default if it still names a current provider/model; otherwise
    # choose the first provider default. Older configs use provider/model here.
    current_root = str(raw.get("default_model") or "")
    valid_keys: set[str] = set()
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        pid = str(provider.get("id") or "")
        for model in provider.get("models") or []:
            if isinstance(model, dict) and isinstance(model.get("id"), str):
                valid_keys.add(f"{pid}/{model['id']}")
    if current_root and current_root not in valid_keys:
        repaired = ""
        for row in providers:
            if not isinstance(row, dict):
                continue
            pid = str(row.get("id") or "").strip()
            models = row.get("models") if isinstance(row.get("models"), list) else []
            ids = [str(model.get("id") or "").strip() for model in models if isinstance(model, dict) and str(model.get("id") or "").strip()]
            if not pid or not ids:
                continue
            default = str(row.get("default_model") or "").strip()
            if default not in ids:
                default = ids[0]
                row["default_model"] = default
            repaired = f"{pid}/{default}"
            break
        if repaired:
            raw["default_model"] = repaired
        else:
            raw.pop("default_model", None)

    fd = -1
    tmp: Path | None = None
    try:
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.refresh-", suffix=".tmp", dir=path.parent)
        tmp = Path(raw_tmp)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            handle.write(json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        # The refresh is background/best-effort. Never overwrite an explicit UI
        # or CLI config mutation that landed after we read the source snapshot.
        current = path.stat()
        if (current.st_mtime_ns, current.st_size) != (initial_stat.st_mtime_ns, initial_stat.st_size):
            return False
        os.replace(tmp, path)
    except OSError:
        return False
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
    return True
