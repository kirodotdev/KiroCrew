"""Provider presets and model-capability normalization for Kiro BYO 0.1.x.

Presets are connection templates, not capability claims. Model capabilities are
resolved per *model* from provider discovery metadata first, then conservative
protocol defaults. Unknown means unknown: Kiro BYO does not invent a reasoning
effort selector just because a provider brand is known.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Iterable

from .paths import app_home
from .provider_products import (
    account_handler_for_connection as _account_handler_for_connection,
    connection_profile_for_credential,
    credential_rejection_message as _credential_rejection_message,
    product_for_preset,
    products_for_preset,
    public_platform_catalog,
    resolve_connection_identity,
    resolve_connection_record,
    resolve_saved_connection_identity,
)


REASONING_MODES = {"none", "fixed", "levels", "toggle", "adaptive", "unknown"}


_ROUTING_FACT_FIELDS = ("quality", "speed", "economy", "tiers")
_ROUTING_SOURCE_PRIORITY = {
    "unknown": 0,
    "name-inference": 100,
    "cache": 200,
    "bundled-fallback": 220,
    "official-profile": 300,
    "curated": 300,
    "provider-discovery": 400,
    "live-provider": 400,
    "local-config": 500,
    "local-override": 600,
}
_ROUTING_SOURCE_CONFIDENCE = {
    "unknown": "unknown",
    "name-inference": "low",
    "cache": "medium",
    "bundled-fallback": "medium",
    "official-profile": "medium",
    "curated": "medium",
    "provider-discovery": "high",
    "live-provider": "high",
    "local-config": "high",
    "local-override": "high",
}
_CONFIDENCE_RANK = {"unknown": 0, "low": 1, "medium": 2, "high": 3}


def routing_confidence_for_source(source: str) -> str:
    """Confidence label for a routing-metadata provenance source."""
    key = str(source or "unknown").strip().lower() or "unknown"
    return _ROUTING_SOURCE_CONFIDENCE.get(key, "medium")


def _routing_source_priority(source: str) -> int:
    key = str(source or "unknown").strip().lower() or "unknown"
    return _ROUTING_SOURCE_PRIORITY.get(key, 250)


def _routing_fact_present(value: Any, field: str) -> bool:
    if field == "tiers":
        return isinstance(value, (list, tuple)) and bool(value)
    if value is None:
        return False
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _routing_provenance_record(source: str, confidence: str = "") -> dict[str, str]:
    src = str(source or "unknown").strip().lower() or "unknown"
    conf = str(confidence or routing_confidence_for_source(src)).strip().lower() or "unknown"
    if conf not in _CONFIDENCE_RANK:
        conf = routing_confidence_for_source(src)
    return {"source": src, "confidence": conf}


def _routing_with_provenance(routing: dict[str, Any], *, default_source: str) -> dict[str, Any]:
    """Attach field-level provenance without changing routing values.

    A routing row can be mixed: e.g. provider discovery may publish ``speed``
    while curated metadata fills ``quality``/``tiers``.  Field-level provenance
    prevents the provider-level source label from falsely claiming every fact.
    """
    out = dict(routing)
    source = str(out.get("source") or default_source or "unknown").strip().lower() or "unknown"
    confidence = str(out.get("confidence") or routing_confidence_for_source(source)).strip().lower()
    existing = out.get("provenance") if isinstance(out.get("provenance"), dict) else {}
    provenance: dict[str, dict[str, str]] = {}
    for field in _ROUTING_FACT_FIELDS:
        value = out.get(field)
        if not _routing_fact_present(value, field):
            continue
        item = existing.get(field) if isinstance(existing.get(field), dict) else {}
        field_source = str(item.get("source") or source).strip().lower() or "unknown"
        field_conf = str(item.get("confidence") or routing_confidence_for_source(field_source)).strip().lower()
        provenance[field] = _routing_provenance_record(field_source, field_conf)
    out["source"] = source
    out["confidence"] = confidence if confidence in _CONFIDENCE_RANK else routing_confidence_for_source(source)
    if provenance:
        out["provenance"] = provenance
    return out


def _aggregate_routing_provenance(provenance: dict[str, Any]) -> tuple[str, str]:
    rows = [value for value in provenance.values() if isinstance(value, dict)]
    if not rows:
        return "unknown", "unknown"
    sources = {str(row.get("source") or "unknown") for row in rows}
    source = next(iter(sources)) if len(sources) == 1 else "mixed"
    confidences = [str(row.get("confidence") or "unknown") for row in rows]
    confidence = min(confidences, key=lambda value: _CONFIDENCE_RANK.get(value, 0))
    return source, confidence


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    display_name: str
    default_base_url: str
    transport: str = "openai-compatible"
    requires_api_key: bool = True
    available: bool = True
    category: str = "official"
    note: str = ""
    # Model discovery may differ from the chat compatibility endpoint. These
    # fields are runtime-only and are intentionally not exposed as credentials.
    discovery_url: str = ""
    discovery_auth: str = "bearer"
    discovery_headers: tuple[tuple[str, str], ...] = ()
    # Optional account features backed by documented official endpoints.
    # Empty means Kiro BYO intentionally exposes no account UI for this preset.
    account_capabilities: tuple[str, ...] = ()
    model_transport_overrides: bool = False
    # Some official /models catalogues mix chat models with ASR/TTS/image-only
    # products.  An allowlist keeps Kiro Crew's text-agent picker from exposing
    # endpoints that cannot participate in the agent loop. Empty means no filter.
    discovery_model_allowlist: tuple[str, ...] = ()
    # Additional first-party endpoints that preserve the same native transport.
    # This is useful when one vendor exposes separate official billing/region
    # endpoints (for example MiMo Token Plan) that are not relays.
    official_base_urls: tuple[str, ...] = ()
    # Some official subscription endpoints publish a fixed supported model set
    # but do not document a /models catalogue. These rows are treated as an
    # official static catalogue and still receive verified model profiles.
    static_model_ids: tuple[str, ...] = ()
    # Historical official endpoints mapped to their current native endpoint.
    # Data-owned so config loading never needs provider-brand branches.
    legacy_base_url_migrations: tuple[tuple[str, str], ...] = ()

    def public_dict(self) -> dict[str, Any]:
        """Serialize only preset-owned connection facts.

        Product/platform identity is assembled by ``public_presets`` so this
        data class no longer depends upward on the product catalogue.
        """
        return {
            "id": self.id,
            "display_name": self.display_name,
            "default_base_url": self.default_base_url,
            "transport": self.transport,
            "adapter_transport": self.transport,
            "transport_label": transport_label(self.transport),
            "native_transport": self.transport not in {"openai-compatible", "openai-chat-completions"},
            "requires_api_key": self.requires_api_key,
            "available": self.available,
            "category": self.category,
            "note": self.note,
            "account_capabilities": list(self.account_capabilities),
            "transport_strategy": "model" if self.model_transport_overrides else "provider",
            "transport_strategy_label": "按模型优化" if self.model_transport_overrides else transport_label(self.transport),
            "official_base_urls": list(self.official_base_urls),
            "static_model_ids": list(self.static_model_ids),
        }



class ProviderCatalogError(RuntimeError):
    """Raised when bundled provider catalogue data is malformed."""


_PROVIDER_CATALOG_PATH = Path(__file__).with_name("catalog") / "provider_catalog.v1.json"
_MATCH_KEYS = {
    "exact", "prefix", "suffix", "contains_any", "contains_all",
    "exclude_contains_any", "exclude_exact", "regex_any", "any", "all",
}


def _catalog_error(path: str, message: str) -> ProviderCatalogError:
    return ProviderCatalogError(f"{path}: {message}")


def _require_str(value: Any, path: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise _catalog_error(path, f"expected string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise _catalog_error(path, "expected non-empty string")
    return value


def _require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _catalog_error(path, f"expected boolean, got {type(value).__name__}")
    return value


def _validate_string_list(value: Any, path: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise _catalog_error(path, f"expected array, got {type(value).__name__}")
    if nonempty and not value:
        raise _catalog_error(path, "expected non-empty array")
    out: list[str] = []
    for index, item in enumerate(value):
        out.append(_require_str(item, f"{path}[{index}]", allow_empty=False))
    return out


def _validate_matcher(value: Any, path: str) -> None:
    if not isinstance(value, dict) or not value:
        raise _catalog_error(path, "expected non-empty matcher object")
    unknown = set(value) - _MATCH_KEYS
    if unknown:
        raise _catalog_error(path, f"unknown matcher keys: {sorted(unknown)}")
    for key in (
        "exact", "prefix", "suffix", "contains_any", "contains_all",
        "exclude_contains_any", "exclude_exact", "regex_any",
    ):
        if key in value:
            items = _validate_string_list(value[key], f"{path}.{key}", nonempty=True)
            if key == "regex_any":
                for index, pattern in enumerate(items):
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        raise _catalog_error(
                            f"{path}.{key}[{index}]", f"invalid regular expression: {exc}"
                        ) from exc
    for key in ("any", "all"):
        if key in value:
            rows = value[key]
            if not isinstance(rows, list) or not rows:
                raise _catalog_error(f"{path}.{key}", "expected non-empty matcher array")
            for index, matcher in enumerate(rows):
                _validate_matcher(matcher, f"{path}.{key}[{index}]")


def _validate_reasoning(value: Any, path: str) -> None:
    if not isinstance(value, dict):
        raise _catalog_error(path, "expected object")
    mode = str(value.get("mode") or "").strip().lower()
    if mode and mode not in REASONING_MODES:
        raise _catalog_error(f"{path}.mode", f"unsupported reasoning mode {mode!r}")
    effort = value.get("effort")
    if effort is not None:
        if not isinstance(effort, dict):
            raise _catalog_error(f"{path}.effort", "expected object")
        levels = effort.get("levels", [])
        if levels:
            levels = _validate_string_list(levels, f"{path}.effort.levels", nonempty=True)
            default = str(effort.get("default") or "")
            if default and default not in levels:
                raise _catalog_error(
                    f"{path}.effort.default", "default must be one of effort.levels"
                )


def _validate_model_profile(value: Any, path: str) -> None:
    if not isinstance(value, dict):
        raise _catalog_error(path, "expected object")
    if "reasoning" in value:
        _validate_reasoning(value["reasoning"], f"{path}.reasoning")
    levels = value.get("effort_supported_levels")
    if levels is not None:
        levels = _validate_string_list(levels, f"{path}.effort_supported_levels")
        default = str(value.get("effort_default_level") or "")
        if default and default not in levels:
            raise _catalog_error(
                f"{path}.effort_default_level",
                "default must be one of effort_supported_levels",
            )
    for key in ("context_window", "max_output_tokens"):
        if key in value and value[key] is not None:
            if not isinstance(value[key], int) or isinstance(value[key], bool) or value[key] <= 0:
                raise _catalog_error(f"{path}.{key}", "expected positive integer")
    for key in ("input_modalities", "output_modalities"):
        if key in value:
            _validate_string_list(value[key], f"{path}.{key}")


def _validate_routing(value: Any, path: str) -> None:
    if not isinstance(value, dict):
        raise _catalog_error(path, "expected object")
    for key in ("quality", "speed", "economy"):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise _catalog_error(f"{path}.{key}", "expected numeric score in [0, 1]")
        if not 0.0 <= float(raw) <= 1.0:
            raise _catalog_error(f"{path}.{key}", "score must be in [0, 1]")
    _validate_string_list(value.get("tiers", []), f"{path}.tiers")


def _validate_catalog(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise _catalog_error("provider_catalog", "expected object")
    if data.get("schema_version") != 1:
        raise _catalog_error(
            "provider_catalog.schema_version",
            f"expected 1, got {data.get('schema_version')!r}",
        )

    labels = data.get("transport_labels")
    if not isinstance(labels, dict) or not labels:
        raise _catalog_error("provider_catalog.transport_labels", "expected non-empty object")
    for key, label in labels.items():
        _require_str(key, "provider_catalog.transport_labels key", allow_empty=False)
        _require_str(label, f"provider_catalog.transport_labels.{key}", allow_empty=False)

    presets = data.get("presets")
    if not isinstance(presets, list) or not presets:
        raise _catalog_error("provider_catalog.presets", "expected non-empty array")
    ids: set[str] = set()
    for index, row in enumerate(presets):
        path = f"provider_catalog.presets[{index}]"
        if not isinstance(row, dict):
            raise _catalog_error(path, "expected object")
        preset_id = _require_str(row.get("id"), f"{path}.id", allow_empty=False).strip().lower()
        if preset_id in ids:
            raise _catalog_error(f"{path}.id", f"duplicate preset id {preset_id!r}")
        ids.add(preset_id)
        for key in ("display_name", "default_base_url", "transport", "category", "note", "discovery_url", "discovery_auth"):
            _require_str(row.get(key, ""), f"{path}.{key}")
        for key in ("requires_api_key", "available", "model_transport_overrides"):
            _require_bool(row.get(key), f"{path}.{key}")
        for key in (
            "account_capabilities", "discovery_model_allowlist",
            "official_base_urls", "static_model_ids",
        ):
            values = _validate_string_list(row.get(key, []), f"{path}.{key}")
            if key == "static_model_ids" and len(values) != len(set(values)):
                raise _catalog_error(f"{path}.{key}", "contains duplicate model ids")
        migrations = row.get("legacy_base_url_migrations", {})
        if not isinstance(migrations, dict):
            raise _catalog_error(f"{path}.legacy_base_url_migrations", "expected object")
        for source, target in migrations.items():
            _require_str(source, f"{path}.legacy_base_url_migrations.<source>", allow_empty=False)
            _require_str(target, f"{path}.legacy_base_url_migrations[{source!r}]", allow_empty=False)

        headers = row.get("discovery_headers", [])
        if not isinstance(headers, list):
            raise _catalog_error(f"{path}.discovery_headers", "expected array")
        for header_index, header in enumerate(headers):
            if not isinstance(header, list) or len(header) != 2:
                raise _catalog_error(
                    f"{path}.discovery_headers[{header_index}]",
                    "expected [name, value]",
                )
            _require_str(header[0], f"{path}.discovery_headers[{header_index}][0]", allow_empty=False)
            _require_str(header[1], f"{path}.discovery_headers[{header_index}][1]")

    for collection, payload_key, validator in (
        ("routing_rules", "routing", _validate_routing),
        ("model_rules", "profile", _validate_model_profile),
    ):
        rows = data.get(collection)
        if not isinstance(rows, list):
            raise _catalog_error(f"provider_catalog.{collection}", "expected array")
        for index, row in enumerate(rows):
            path = f"provider_catalog.{collection}[{index}]"
            if not isinstance(row, dict):
                raise _catalog_error(path, "expected object")
            rule_presets = _validate_string_list(row.get("presets"), f"{path}.presets", nonempty=True)
            missing = [item for item in rule_presets if item not in ids]
            if missing:
                raise _catalog_error(f"{path}.presets", f"unknown preset ids: {missing}")
            _validate_matcher(row.get("match"), f"{path}.match")
            validator(row.get(payload_key), f"{path}.{payload_key}")
            source = row.get("source")
            if source is not None:
                if not isinstance(source, dict):
                    raise _catalog_error(f"{path}.source", "expected object")
                if "note" in source:
                    _require_str(source["note"], f"{path}.source.note")
    return data


def _load_provider_catalog(path: Path = _PROVIDER_CATALOG_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProviderCatalogError(f"cannot read provider catalogue {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderCatalogError(
            f"invalid JSON in provider catalogue {path}:{exc.lineno}:{exc.colno}: {exc.msg}"
        ) from exc
    return _validate_catalog(payload)


def _preset_from_row(row: dict[str, Any]) -> ProviderPreset:
    return ProviderPreset(
        id=str(row["id"]),
        display_name=str(row["display_name"]),
        default_base_url=str(row["default_base_url"]),
        transport=str(row.get("transport") or "openai-compatible"),
        requires_api_key=bool(row.get("requires_api_key", True)),
        available=bool(row.get("available", True)),
        category=str(row.get("category") or "official"),
        note=str(row.get("note") or ""),
        discovery_url=str(row.get("discovery_url") or ""),
        discovery_auth=str(row.get("discovery_auth") or "bearer"),
        discovery_headers=tuple((str(a), str(b)) for a, b in row.get("discovery_headers", [])),
        account_capabilities=tuple(str(item) for item in row.get("account_capabilities", [])),
        model_transport_overrides=bool(row.get("model_transport_overrides", False)),
        discovery_model_allowlist=tuple(str(item) for item in row.get("discovery_model_allowlist", [])),
        official_base_urls=tuple(str(item) for item in row.get("official_base_urls", [])),
        static_model_ids=tuple(str(item) for item in row.get("static_model_ids", [])),
        legacy_base_url_migrations=tuple(
            (str(source).rstrip("/"), str(target).rstrip("/"))
            for source, target in row.get("legacy_base_url_migrations", {}).items()
        ),
    )


def _matches_model(matcher: dict[str, Any], model_id: str) -> bool:
    mid = model_id
    if "exact" in matcher and mid not in matcher["exact"]:
        return False
    if "prefix" in matcher and not any(mid.startswith(item) for item in matcher["prefix"]):
        return False
    if "suffix" in matcher and not any(mid.endswith(item) for item in matcher["suffix"]):
        return False
    if "contains_any" in matcher and not any(item in mid for item in matcher["contains_any"]):
        return False
    if "contains_all" in matcher and not all(item in mid for item in matcher["contains_all"]):
        return False
    if "exclude_contains_any" in matcher and any(item in mid for item in matcher["exclude_contains_any"]):
        return False
    if "exclude_exact" in matcher and mid in matcher["exclude_exact"]:
        return False
    if "regex_any" in matcher and not any(re.search(pattern, mid) for pattern in matcher["regex_any"]):
        return False
    if "any" in matcher and not any(_matches_model(item, mid) for item in matcher["any"]):
        return False
    if "all" in matcher and not all(_matches_model(item, mid) for item in matcher["all"]):
        return False
    return True


_PROVIDER_CATALOG = _load_provider_catalog()
_PROVIDER_PRESETS: tuple[ProviderPreset, ...] = tuple(
    _preset_from_row(row) for row in _PROVIDER_CATALOG["presets"]
)
TRANSPORT_LABELS: dict[str, str] = {
    str(key): str(value) for key, value in _PROVIDER_CATALOG["transport_labels"].items()
}
_ROUTING_RULES: tuple[dict[str, Any], ...] = tuple(_PROVIDER_CATALOG["routing_rules"])
_MODEL_RULES: tuple[dict[str, Any], ...] = tuple(_PROVIDER_CATALOG["model_rules"])


def transport_label(transport: str) -> str:
    value = str(transport or "").strip().lower()
    return TRANSPORT_LABELS.get(value, value or "未知")


def official_routing_profile(preset_id: str, model_id: str) -> dict[str, Any]:
    """Return curated routing metadata from the versioned data catalogue."""
    preset = str(preset_id or "").strip().lower()
    mid = str(model_id or "").strip().lower().removeprefix("models/")
    for rule in _ROUTING_RULES:
        if preset not in rule["presets"] or not _matches_model(rule["match"], mid):
            continue
        routing = copy.deepcopy(rule["routing"])
        return {
            "routing": _routing_with_provenance(
                routing, default_source=str(routing.get("source") or "official-profile")
            )
        }
    return {}


def official_model_profile(preset_id: str, model_id: str) -> dict[str, Any]:
    """Return verified per-model capability overrides from versioned data."""
    preset = str(preset_id or "").strip().lower()
    mid = str(model_id or "").strip().lower().removeprefix("models/")
    for rule in _MODEL_RULES:
        if preset not in rule["presets"] or not _matches_model(rule["match"], mid):
            continue
        return copy.deepcopy(rule["profile"])
    return {}



def _merge_routing_metadata(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    incoming_default_source: str,
    force_incoming: bool = False,
) -> dict[str, Any]:
    """Merge routing facts by provenance rather than by container order.

    The precedence is deliberate and stable across catalogue refreshes:
    local override > live provider > verified curated > cache > name inference.
    Different fields may therefore come from different sources.
    """
    base = _routing_with_provenance(existing, default_source=str(existing.get("source") or "unknown"))
    inc = _routing_with_provenance(incoming, default_source=incoming_default_source)
    result = dict(base)
    base_prov = dict(base.get("provenance") or {})
    inc_prov = dict(inc.get("provenance") or {})
    provenance = dict(base_prov)

    for field in _ROUTING_FACT_FIELDS:
        value = inc.get(field)
        if not _routing_fact_present(value, field):
            continue
        inc_meta = inc_prov.get(field) if isinstance(inc_prov.get(field), dict) else _routing_provenance_record(incoming_default_source)
        base_meta = base_prov.get(field) if isinstance(base_prov.get(field), dict) else {"source": "unknown", "confidence": "unknown"}
        incoming_priority = _routing_source_priority(str(inc_meta.get("source") or incoming_default_source))
        existing_priority = _routing_source_priority(str(base_meta.get("source") or "unknown"))
        if force_incoming or incoming_priority >= existing_priority:
            result[field] = value
            provenance[field] = _routing_provenance_record(
                str(inc_meta.get("source") or incoming_default_source),
                str(inc_meta.get("confidence") or ""),
            )

    source, confidence = _aggregate_routing_provenance(provenance)
    result["source"] = source
    result["confidence"] = confidence
    if provenance:
        result["provenance"] = provenance
    return result


def _merge_profile_row(
    result: dict[str, Any],
    profile: dict[str, Any],
    *,
    routing_source: str = "local-override",
) -> dict[str, Any]:
    """Authoritative overlay used for explicit local/operator overrides."""
    for key, value in profile.items():
        if key == "routing" and isinstance(value, dict):
            existing = result.get(key) if isinstance(result.get(key), dict) else {}
            result[key] = _merge_routing_metadata(
                existing, value, incoming_default_source=routing_source, force_incoming=True
            )
        elif key == "reasoning" and isinstance(value, dict):
            existing = result.get(key) if isinstance(result.get(key), dict) else {}
            merged = dict(existing)
            merged.update(value)
            result[key] = merged
        else:
            result[key] = value
    return result


def _has_catalog_fact(value: Any, *, key: str = "") -> bool:
    """Whether a live discovery value actually proves a fact.

    Empty/unknown normalization placeholders must not erase curated fallbacks,
    while explicit ``False`` and numeric zero-like booleans remain meaningful.
    """
    if value is None:
        return False
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        if key == "mode" and text.lower() == "unknown":
            return False
        return True
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _merge_discovery_over_curated(curated: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """Use curated metadata only as a fallback for facts discovery omitted.

    Provider control-plane/catalogue data is current and therefore wins when it
    explicitly supplies a value. Curated metadata fills omissions. The separate
    local override layer remains authoritative above both.
    """
    result = dict(curated)
    for key, value in live.items():
        if key == "routing" and isinstance(value, dict):
            existing = result.get("routing") if isinstance(result.get("routing"), dict) else {}
            catalog_source = str(live.get("catalog_source") or "").strip().lower()
            if catalog_source == "cache":
                # An outage cache is useful continuity data, but it must not
                # outrank a newer verified curated routing fact merely because
                # the cached row originally came from provider discovery.
                live_source = "cache"
                value = dict(value)
                value["source"] = "cache"
                value["confidence"] = routing_confidence_for_source("cache")
                value["provenance"] = {}
            else:
                live_source = str(
                    value.get("source")
                    or live.get("routing_source")
                    or catalog_source
                    or "provider-discovery"
                ).strip().lower() or "provider-discovery"
            if live_source in {"provider", "discovery"}:
                live_source = "provider-discovery"
            result["routing"] = _merge_routing_metadata(
                existing, value, incoming_default_source=live_source
            )
        elif key == "reasoning" and isinstance(value, dict):
            existing = result.get("reasoning") if isinstance(result.get("reasoning"), dict) else {}
            merged = dict(existing)
            meaningful_reasoning = False
            for rkey, rvalue in value.items():
                if rkey == "source":
                    continue
                if isinstance(rvalue, dict):
                    nested = dict(merged.get(rkey) or {}) if isinstance(merged.get(rkey), dict) else {}
                    nested_changed = False
                    for nkey, nvalue in rvalue.items():
                        if _has_catalog_fact(nvalue, key=nkey):
                            nested[nkey] = nvalue
                            nested_changed = True
                    if nested_changed:
                        merged[rkey] = nested
                        meaningful_reasoning = True
                elif _has_catalog_fact(rvalue, key=rkey):
                    merged[rkey] = rvalue
                    meaningful_reasoning = True
            if meaningful_reasoning and _has_catalog_fact(value.get("source"), key="source"):
                merged["source"] = value["source"]
            result["reasoning"] = merged
        elif _has_catalog_fact(value, key=key):
            result[key] = value
    return result


def _local_model_profile_override(preset_id: str, model_id: str) -> dict[str, Any]:
    """Load an optional operator-supplied model metadata override.

    Fast-moving providers can publish a model before Kiro BYO ships updated
    curated metadata. A local data file may therefore add capability/target/
    lifecycle facts without introducing code or another provider adapter.
    Unknown fields are ignored naturally by downstream normalization.
    """
    path = app_home() / "config" / "model_catalog.override.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    try:
        schema_version = int(raw.get("schema_version") or 1)
    except (TypeError, ValueError):
        return {}
    if schema_version != 1:
        return {}
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return {}
    family = providers.get(str(preset_id or "").strip().lower())
    if not isinstance(family, dict):
        return {}
    value = family.get(str(model_id or "").strip())
    return dict(value) if isinstance(value, dict) else {}


def merge_official_model_profile(preset_id: str, row: dict[str, Any]) -> dict[str, Any]:
    """Merge provider discovery, curated facts, then local data overrides.

    Precedence is deliberate: live discovery supplies current catalogue facts;
    curated metadata fills provider omissions; a local operator override is the
    final authority for a newly launched/custom deployment.
    """
    model_id = str(row.get("id") or row.get("name") or "")
    model_name = str(row.get("name") or "").strip()
    profile = official_model_profile(preset_id, model_id)
    # Provider catalogues and older user configs occasionally persist the
    # provider's stable wire id in ``id`` while exposing a newer marketing name
    # in ``name`` (or vice versa).  Both belong to the same official endpoint.
    # Let the human-visible official alias fill capability omissions, while the
    # provider's live row below still wins for every explicit fact.
    if model_name and model_name.lower() != model_id.lower():
        name_profile = official_model_profile(preset_id, model_name)
        if name_profile:
            profile = _merge_discovery_over_curated(name_profile, profile) if profile else name_profile
    routing_profile = official_routing_profile(preset_id, model_id)
    if not routing_profile and model_name and model_name.lower() != model_id.lower():
        routing_profile = official_routing_profile(preset_id, model_name)
    if routing_profile:
        profile = _merge_profile_row(dict(profile), routing_profile, routing_source="official-profile")
    # Current provider catalogue facts beat the bundled curated snapshot; the
    # snapshot only fills omissions. This prevents stale release metadata from
    # masking a provider-side context/capability change.
    result = _merge_discovery_over_curated(profile, row) if profile else dict(row)
    override = _local_model_profile_override(preset_id, model_id)
    if override:
        result = _merge_profile_row(result, override)
        result["capability_source"] = str(override.get("capability_source") or "local-override")
        reasoning = result.get("reasoning") if isinstance(result.get("reasoning"), dict) else {}
        if reasoning:
            reasoning = dict(reasoning)
            reasoning["source"] = str(override.get("capability_source") or "local-override")
            result["reasoning"] = reasoning
    return result



def provider_presets() -> tuple[ProviderPreset, ...]:
    return _PROVIDER_PRESETS


def provider_preset(preset_id: str) -> ProviderPreset | None:
    wanted = str(preset_id or "").strip().lower()
    return next((preset for preset in _PROVIDER_PRESETS if preset.id == wanted), None)


def migrate_official_base_url(preset: ProviderPreset | None, base_url: str) -> str:
    """Normalize a historical first-party endpoint using catalogue data."""
    current = str(base_url or "").strip().rstrip("/")
    if preset is None:
        return current
    migrations = dict(preset.legacy_base_url_migrations)
    return migrations.get(current, current)


def legacy_official_base_urls(preset: ProviderPreset | None) -> tuple[str, ...]:
    if preset is None:
        return ()
    return tuple(source for source, _target in preset.legacy_base_url_migrations)


def public_presets() -> list[dict[str, Any]]:
    # ``xiaomi-tokenplan`` remains an internal compatibility alias so existing
    # configs keep loading, but the Provider picker exposes Xiaomi only once.
    # Product/platform enrichment belongs to this assembly layer rather than to
    # ``ProviderPreset`` itself, avoiding the old data-class -> product-catalog
    # dependency inversion while preserving the public response exactly.
    rows: list[dict[str, Any]] = []
    for preset in _PROVIDER_PRESETS:
        if preset.id == "xiaomi-tokenplan":
            continue
        row = preset.public_dict()
        found = product_for_preset(_PROVIDER_PRESETS, preset.id)
        if found is not None:
            platform, product = found
            profile = product.default_profile
            row.update({
                "platform_id": platform.id,
                "platform_name": platform.display_name,
                "product_id": product.id,
                "product_name": product.display_name,
                "profile_id": profile.id,
                "entitlement": profile.entitlement,
                "profiles": [item.public_dict() for item in product.profiles],
            })
        rows.append(row)
    return rows


def public_platforms() -> list[dict[str, Any]]:
    """Structured Platform -> Product -> Profile catalogue for new clients."""
    return public_platform_catalog(_PROVIDER_PRESETS)


def credential_connection_profile(preset_id: str, credential: str):
    """Return a Product/Profile hinted by credential family metadata."""
    return connection_profile_for_credential(_PROVIDER_PRESETS, preset_id=preset_id, credential=credential)


def credential_rejection_message(preset_id: str, credential: str) -> str:
    """Return a data-declared credential-family mismatch message, if any."""
    return _credential_rejection_message(preset_id, credential)


def account_status_handler(
    preset_id: str, base_url: str, *, product_id: str = "", credential: str = "",
) -> str:
    """Resolve the provider-owned account strategy for one saved connection."""
    return _account_handler_for_connection(
        _PROVIDER_PRESETS, preset_id=preset_id, base_url=base_url,
        product_id=product_id, credential=credential,
    )


def connection_identity(preset_id: str, base_url: str):
    """Resolve stable commercial identity independently of protocol adapter."""
    return resolve_connection_identity(_PROVIDER_PRESETS, preset_id=preset_id, base_url=base_url)


def saved_connection_identity(preset_id: str, base_url: str, product_id: str, profile_id: str = ""):
    """Validate a persisted Product/Profile when one inference URL is shared."""
    return resolve_saved_connection_identity(
        _PROVIDER_PRESETS, preset_id=preset_id, base_url=base_url,
        product_id=product_id, profile_id=profile_id,
    )


def connection_record(preset_id: str, base_url: str):
    """Return Platform/Product/Profile metadata for one concrete endpoint."""
    return resolve_connection_record(_PROVIDER_PRESETS, preset_id=preset_id, base_url=base_url)


def product_base_urls(preset_id: str) -> tuple[str, ...]:
    """Return every profile URL that reuses this provider family.

    Product/plan overlays may add new products while keeping one protocol
    adapter and one legacy preset family, so returning only the first product
    would incorrectly demote those endpoints to a generic relay.
    """
    urls: list[str] = []
    seen: set[str] = set()
    for _platform, product in products_for_preset(_PROVIDER_PRESETS, preset_id):
        for profile in product.profiles:
            value = str(profile.base_url or "").strip().rstrip("/")
            if value and value not in seen:
                seen.add(value)
                urls.append(value)
    return tuple(urls)


def _string_levels(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            level = item.strip()
            if level and level not in seen:
                seen.add(level)
                out.append(level)
    return tuple(out)


def normalize_reasoning_capability(
    *,
    effort_levels: Iterable[str] = (),
    effort_default: str = "",
    raw_reasoning: Any = None,
    source: str = "provider",
) -> dict[str, Any]:
    """Normalize model reasoning without conflating *Thinking* and *Effort*.

    Thinking answers whether deep reasoning exists and whether it can be
    enabled/disabled. Effort is the intensity *while thinking is enabled* and
    may expose a model-specific number of levels. Some provider catalogues put
    an ``off`` sentinel in the same wire list as effort values; that sentinel is
    retained as transport metadata but is never counted as an effort level.

    Unknown stays unknown. Kiro BYO only advertises controls that can be mapped
    to provider-declared wire values; it never fabricates a universal on/off or
    a fixed number of effort levels from the provider brand.
    """

    disable_names = {"none", "off", "disabled"}
    enable_names = {"on", "enabled", "thinking"}

    legacy_levels = _string_levels(list(effort_levels))
    legacy_default = str(effort_default or "").strip()
    raw = raw_reasoning if isinstance(raw_reasoning, dict) else {}
    raw_mode = str(raw.get("mode") or "").strip().lower()
    # Preserve provider-declared future mode names as metadata instead of
    # collapsing them to ``unknown``.  Only the small set whose semantics Kiro
    # already understands is allowed to *infer* support below; unfamiliar modes
    # still require explicit supported/can_disable facts and therefore fail
    # closed rather than fabricating a control.
    semantic_mode = raw_mode if raw_mode in REASONING_MODES else ""
    if semantic_mode == "unknown":
        semantic_mode = ""

    raw_effort = raw.get("effort") if isinstance(raw.get("effort"), dict) else {}
    raw_thinking = raw.get("thinking") if isinstance(raw.get("thinking"), dict) else {}

    # New schema wins. Old ``reasoning.levels`` and top-level
    # effort_supported_levels remain accepted for configs written by older builds.
    explicit_effort_levels = _string_levels(raw_effort.get("levels"))
    legacy_reasoning_levels = _string_levels(raw.get("levels"))
    wire_levels = _string_levels(raw_effort.get("wire_levels"))
    if not wire_levels:
        wire_levels = legacy_reasoning_levels or legacy_levels

    default_wire = ""
    for candidate in (raw_effort.get("default"), raw.get("default"), legacy_default):
        if isinstance(candidate, str) and candidate.strip():
            default_wire = candidate.strip()
            break

    def _raw_bool(*values: Any) -> bool | None:
        for value in values:
            if isinstance(value, bool):
                return value
        return None

    def _raw_str(*values: Any) -> str:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    disable_value = _raw_str(raw_thinking.get("disable_value"), raw.get("disable_value"))
    if not disable_value:
        disable_value = next((v for v in wire_levels if v.lower() in disable_names), "")

    enable_value = _raw_str(raw_thinking.get("enable_value"), raw.get("enable_value"))
    auto_value = _raw_str(raw_thinking.get("auto_value"), raw.get("auto_value"))

    # If a catalogue explicitly identifies effort levels, trust it. Otherwise
    # split transport sentinels away from the intensity vocabulary. A pure
    # [none, thinking] pair is a toggle, not a one-level effort scale.
    if explicit_effort_levels:
        intensity_levels = explicit_effort_levels
    else:
        candidates = tuple(v for v in wire_levels if v.lower() not in disable_names)
        if raw_mode == "toggle" and len(candidates) == 1 and candidates[0].lower() in enable_names:
            enable_value = enable_value or candidates[0]
            intensity_levels = ()
        elif len(candidates) == 1 and candidates[0].lower() in enable_names and disable_value:
            enable_value = enable_value or candidates[0]
            intensity_levels = ()
        else:
            intensity_levels = candidates

    # De-duplicate again after splitting, preserving provider order exactly.
    intensity_levels = _string_levels(list(intensity_levels))

    effort_default_value = _raw_str(raw_effort.get("default"))
    if not effort_default_value and default_wire in intensity_levels:
        effort_default_value = default_wire
    if effort_default_value not in intensity_levels:
        effort_default_value = ""

    fixed_level = _raw_str(raw_effort.get("fixed_level"), raw.get("fixed_level"))
    if not fixed_level and len(intensity_levels) == 1:
        fixed_level = intensity_levels[0]
    if fixed_level and fixed_level not in intensity_levels:
        fixed_level = ""

    supported = _raw_bool(raw_thinking.get("supported"), raw.get("supported"))
    if supported is None:
        if semantic_mode == "none":
            supported = False
        elif semantic_mode in {"fixed", "levels", "toggle", "adaptive"}:
            supported = True
        elif wire_levels or intensity_levels or disable_value or enable_value:
            supported = True

    can_disable = _raw_bool(raw_thinking.get("can_disable"), raw.get("can_disable"))
    if supported is False:
        can_disable = False
    elif semantic_mode == "fixed":
        # Fixed thinking is a provider statement: noisy can_disable metadata may
        # not turn it into a user-facing switch.
        can_disable = False
    elif can_disable is None and semantic_mode == "toggle":
        # ``toggle`` itself explicitly states that both enabled and disabled
        # states exist.  The native transport adapter owns the concrete wire
        # encoding (boolean, thinking.type, reasoning none, ...).
        can_disable = True
    elif can_disable is None and disable_value:
        can_disable = True

    default_enabled = _raw_bool(raw_thinking.get("default_enabled"), raw.get("default_enabled"))
    if default_enabled is None:
        if default_wire and disable_value and default_wire == disable_value:
            default_enabled = False
        elif default_wire and (default_wire in intensity_levels or default_wire == enable_value):
            default_enabled = True
        elif supported is False:
            default_enabled = False
        elif semantic_mode == "fixed":
            default_enabled = True

    supports_auto = _raw_bool(raw_thinking.get("supports_auto"), raw.get("supports_auto"))
    if supports_auto is None and auto_value:
        supports_auto = True

    # Backward-compatible coarse mode. UI and new callers should use the nested
    # ``thinking`` and ``effort`` objects below instead of deriving one from this.
    if supported is False:
        mode = "none"
    elif raw_mode and raw_mode != "unknown":
        # Keep an explicit provider mode name, including future modes Kiro does
        # not yet need to understand.  UI/control eligibility is driven by the
        # independent supported/can_disable facts, not this label.
        mode = raw_mode
    elif can_disable is True and not intensity_levels:
        mode = "toggle"
    elif len(intensity_levels) > 1:
        mode = "levels"
    elif supported is True:
        mode = "fixed"
    else:
        mode = "unknown"

    selectable = list(intensity_levels) if len(intensity_levels) > 1 else []
    effort = {
        "levels": list(intensity_levels),
        "selectable_levels": selectable,
        "default": effort_default_value,
        "fixed_level": fixed_level,
        "configurable": len(selectable) > 1,
        "level_count": len(intensity_levels),
    }
    thinking = {
        "supported": supported,
        "can_disable": can_disable,
        "default_enabled": default_enabled,
        "supports_auto": supports_auto,
        "disable_value": disable_value,
        "enable_value": enable_value,
        "auto_value": auto_value,
        "control": (
            "unsupported" if supported is False else
            "switchable" if can_disable is True else
            "fixed_on" if supported is True and can_disable is False else
            "adaptive" if raw_mode == "adaptive" else
            "unknown"
        ),
    }
    return {
        "mode": mode,
        # Legacy flat fields now refer only to effort intensity; ``off`` is not
        # an effort level. Keep them until old configs/callers have migrated.
        "levels": list(intensity_levels),
        "wire_levels": list(wire_levels),
        "selectable_levels": selectable,
        "default": effort_default_value,
        "fixed_level": fixed_level,
        "can_disable": can_disable,
        "supported": supported,
        "default_enabled": default_enabled,
        "disable_value": disable_value,
        "enable_value": enable_value,
        "auto_value": auto_value,
        "thinking": thinking,
        "effort": effort,
        "source": _raw_str(raw.get("source")) or source or "unknown",
    }


def normalize_tool_use_capability(row: dict[str, Any]) -> bool | None:
    """Normalize provider-supplied per-model tool-use metadata without brand heuristics."""
    for key in ("tool_use", "supports_tools", "supports_tool_use", "supports_function_calling"):
        value = row.get(key)
        if isinstance(value, bool):
            return value
    caps = row.get("capabilities")
    if isinstance(caps, dict):
        for key in ("tool_use", "tools", "function_calling"):
            value = caps.get(key)
            if isinstance(value, bool):
                return value
            # Claude and some other model catalogues expose capabilities as
            # {"tool_use": {"supported": true}} rather than a flat boolean.
            if isinstance(value, dict) and isinstance(value.get("supported"), bool):
                return bool(value["supported"])
    elif isinstance(caps, list):
        names = {str(value).strip().lower() for value in caps if isinstance(value, str)}
        if names & {"tool_use", "tools", "function_calling", "function-calling"}:
            return True
    features = row.get("features")
    if isinstance(features, list):
        names = {str(value).strip().lower() for value in features if isinstance(value, str)}
        if names & {"function-calling", "function_calling", "tool-use", "tools"}:
            return True
    params = row.get("supported_parameters")
    if isinstance(params, list):
        names = {str(value).strip().lower() for value in params if isinstance(value, str)}
        if names & {"tools", "tool_choice", "parallel_tool_calls", "functions", "function_call"}:
            return True
    return None


def model_routing_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize optional routing metadata with field-level provenance.

    Routing metadata is an optimization hint, not a capability.  A row may be
    mixed-source: live discovery can provide latency while curated metadata fills
    quality/roles.  ``provenance`` preserves that distinction so the router and
    diagnostics do not treat a name guess or stale cache as equal to a live fact.
    """
    routing = row.get("routing") if isinstance(row.get("routing"), dict) else {}

    def score(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(1.0, number))

    tiers: list[str] = []
    singular = routing.get("tier", row.get("tier"))
    values = routing.get("tiers") if isinstance(routing.get("tiers"), (list, tuple)) else row.get("routing_tiers")
    for value in ([singular] if singular is not None else []) + (list(values) if isinstance(values, (list, tuple)) else []):
        tier = str(value or "").strip().lower()
        if tier in {"fast", "balanced", "strong"} and tier not in tiers:
            tiers.append(tier)

    catalog_source = str(row.get("catalog_source") or "").strip().lower()
    explicit_provenance = routing.get("provenance") if isinstance(routing.get("provenance"), dict) else {}
    if catalog_source == "cache" and not explicit_provenance:
        default_source = "cache"
        default_confidence = routing_confidence_for_source("cache")
        raw_provenance: dict[str, Any] = {}
    else:
        default_source = str(
            routing.get("source")
            or row.get("routing_source")
            or ("provider-discovery" if catalog_source == "provider-discovery" else "")
            or ("local-config" if any(key in routing for key in _ROUTING_FACT_FIELDS) else "unknown")
        ).strip().lower() or "unknown"
        default_confidence = str(routing.get("confidence") or row.get("routing_confidence") or "")
        raw_provenance = explicit_provenance
    normalized = _routing_with_provenance(
        {
            "quality": score(routing.get("quality", row.get("routing_quality"))),
            "speed": score(routing.get("speed", row.get("routing_speed"))),
            "economy": score(routing.get("economy", row.get("routing_economy"))),
            "tiers": tiers,
            "source": default_source,
            "confidence": default_confidence,
            "provenance": raw_provenance,
        },
        default_source=default_source,
    )
    provenance = normalized.get("provenance") if isinstance(normalized.get("provenance"), dict) else {}
    source, confidence = _aggregate_routing_provenance(provenance)
    return {
        "quality": normalized.get("quality"),
        "speed": normalized.get("speed"),
        "economy": normalized.get("economy"),
        "tiers": list(normalized.get("tiers") or []),
        "source": source,
        "confidence": confidence,
        "provenance": provenance,
    }


def model_capability_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Build the public capability object persisted/returned by admin APIs."""

    effort_levels = _string_levels(row.get("effort_supported_levels"))
    effort_default = str(row.get("effort_default_level") or "")
    reasoning = normalize_reasoning_capability(
        effort_levels=effort_levels,
        effort_default=effort_default,
        raw_reasoning=row.get("reasoning"),
        source=str(row.get("capability_source") or "provider"),
    )
    return {
        "reasoning": reasoning,
        "input_modalities": list(_string_levels(row.get("input_modalities"))),
        "output_modalities": list(_string_levels(row.get("output_modalities"))),
        "context_window": row.get("context_window") if isinstance(row.get("context_window"), int) else None,
        "max_output_tokens": row.get("max_output_tokens") if isinstance(row.get("max_output_tokens"), int) else None,
        "tool_use": normalize_tool_use_capability(row),
    }
