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
from .discovery import DiscoveryError, DiscoveredModel, discover_models, filter_agent_models, infer_provider_identity, profile_official_discovered_models
from .provider_catalog import discovered_model_record, connection_record, product_base_urls, provider_preset
from .reasoning_control import normalize_thinking_preference

METADATA_SCHEMA_VERSION = 7


def _model_dict(model, *, thinking_preference: str = "auto") -> dict[str, Any]:
    row = discovered_model_record(model)
    row["thinking_preference"] = normalize_thinking_preference(thinking_preference)
    return row


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
            models = profile_official_discovered_models(preset.id, models, serialize=_model_dict)

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
