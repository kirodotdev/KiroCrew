"""Configuration loading for Kiro BYO.

Provider metadata intentionally lives outside the installed runtime. The
installer records ``KIRO_BYO_HOME`` and the runtime reads
``config/providers.json`` from there. Live secrets are resolved through
``CredentialStore`` and are not part of normal JSON configuration. Updating the
runtime therefore does not overwrite provider metadata or OS-stored credentials.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .credential_store import (
    CredentialStore,
    CredentialStoreError,
    decode_provider_secrets,
    default_credential_store,
    is_sensitive_header,
)
from .paths import app_home
from .policy_packs import PolicyPackError, default_pack, resolve_policy
from .provider_catalog import (
    connection_identity, legacy_official_base_urls, merge_official_model_profile,
    migrate_official_base_url, model_routing_from_row, normalize_reasoning_capability,
    product_base_urls, provider_preset, saved_connection_identity,
)
from .reasoning_language import normalize_reasoning_language


class ConfigError(RuntimeError):
    """Raised when providers.json exists but is invalid."""


_ALLOWED_TRANSPORTS = {
    "openai-compatible",
    "openai-chat-completions",
    "openai-responses",
    "deepseek-responses",
    "ark-responses",
    "xai-responses",
    "groq-responses",
    "anthropic-messages",
    "gemini-interactions",
    "cohere-v2",
    "qwen-chat-v1",
    "qwen-responses",
    "hunyuan-responses",
    "mistral-chat-v1",
    "moonshot-chat-v1",
    "xiaomi-mimo-chat",
    "minimax-messages",
    "qianfan-responses",
    "zhipu-chat-v4",
}


@dataclass(frozen=True)
class ModelSpec:
    provider_id: str
    provider_name: str
    model_id: str
    name: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    effort_supported_levels: tuple[str, ...] = ()
    effort_default_level: str = ""
    reasoning_mode: str = "unknown"
    reasoning_supported: bool | None = None
    reasoning_can_disable: bool | None = None
    reasoning_default_enabled: bool | None = None
    reasoning_disable_value: str = ""
    reasoning_enable_value: str = ""
    reasoning_auto_value: str = ""
    reasoning_supports_auto: bool | None = None
    reasoning_fixed_level: str = ""
    # Per-model user policy. "auto" delegates enablement to the active Type;
    # "on"/"off" are hard locks. This is distinct from reasoning effort.
    thinking_preference: str = "auto"
    capability_source: str = "provider"
    tool_use: bool | None = None
    transport: str = ""
    # Catalogue identity is stable UI/routing identity. The wire target may be
    # an endpoint/deployment/inference-profile id on managed platforms.
    invocation_target: str = ""
    invocation_target_kind: str = "model"
    lifecycle: str = "active"
    catalog_source: str = "provider"
    routing_quality: float | None = None
    routing_speed: float | None = None
    routing_economy: float | None = None
    routing_source: str = "unknown"
    routing_confidence: str = "unknown"
    routing_provenance: tuple[tuple[str, str, str], ...] = ()
    # A model may serve more than one routing role. For example a flagship
    # "ultraspeed" endpoint can be both strong and fast, while a near-flagship
    # value model can participate in both balanced and strong pools. This is
    # deliberately many-to-many: a Tiga Type is a model *pool/strategy*, never
    # a one-Type-to-one-model binding.
    routing_tiers: tuple[str, ...] = ()
    # Backward-compatible singular manual tier. Existing configs keep working;
    # new metadata should prefer routing.tiers.
    tier: str = ""

    @property
    def selectable_effort_levels(self) -> tuple[str, ...]:
        """Provider-declared effort values that are actually user-selectable.

        Thinking mode and reasoning effort are orthogonal capabilities. A model
        can use adaptive/toggle/future thinking semantics while independently
        exposing multiple effort levels (modern Claude is the important current
        example).  Therefore Crew visibility must be driven by the concrete
        model's advertised effort vocabulary, never by a hard-coded reasoning
        mode or provider family. A single value is fixed transport metadata, not
        a useful selector.
        """
        if self.reasoning_supported is False:
            return ()
        if len(self.effort_supported_levels) > 1:
            return self.effort_supported_levels
        return ()

    @property
    def key(self) -> str:
        return f"{self.provider_id}/{self.model_id}"

    @property
    def wire_model_id(self) -> str:
        """Identifier sent to the provider inference endpoint."""
        return self.invocation_target or self.model_id

    @property
    def display_name(self) -> str:
        # Human-facing model name returned by the provider.  Provider/model IDs
        # are transport details and should stay out of the normal picker UI.
        return self.name


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    type: str
    base_url: str
    api_key: str
    models: tuple[ModelSpec, ...]
    credential_ref: str = ""
    default_model: str = ""
    timeout_seconds: float = 120.0
    stream: bool = True
    chat_completions_path: str = "/chat/completions"
    headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    temperature: float | None = None
    preset_id: str = "custom"
    # Commercial connection identity is deliberately independent of the wire
    # adapter in ``type``. New plans/regions update these fields/profile data
    # instead of creating another provider adapter class.
    platform_id: str = "custom"
    product_id: str = "compatible-endpoint"
    profile_id: str = "default"
    entitlement: str = "relay"

    def model(self, model_id: str) -> ModelSpec | None:
        for model in self.models:
            if model.model_id == model_id:
                return model
        return None


def _default_mode_pack_id() -> str:
    try:
        return default_pack().id
    except PolicyPackError:
        return ""


def _default_mode_policy_id() -> str:
    try:
        return default_pack().default_policy
    except PolicyPackError:
        return ""


@dataclass(frozen=True)
class KiroByoConfig:
    providers: tuple[ProviderSpec, ...]
    default_model: str = ""
    mode_pack: str = field(default_factory=_default_mode_pack_id)
    mode_type: str = field(default_factory=_default_mode_policy_id)
    # Presentation language for provider-visible reasoning/thinking. ``auto``
    # follows the current user conversation; this is independent of model
    # Thinking enablement and reasoning-effort controls.
    reasoning_language: str = "auto"
    response_language: str = "auto"
    # Used only as a fallback for language-ambiguous turns, never to override
    # the user's actual message language.
    ui_language: str = "auto"

    @property
    def models(self) -> tuple[ModelSpec, ...]:
        return tuple(model for provider in self.providers for model in provider.models)

    def _public_model_ids(self) -> dict[str, str]:
        """Map canonical provider/model keys to stable human-facing ACP ids.

        A provider's official ``name`` is used directly when it is unique.  Only
        collisions are qualified with the provider display name (and, in the
        extremely unlikely event that is still ambiguous, the provider id).
        This keeps Crew's picker and message footer clean while preserving a
        one-to-one mapping back to the real API model id.
        """
        by_name: dict[str, list[ModelSpec]] = {}
        for model in self.models:
            by_name.setdefault(model.name, []).append(model)

        result: dict[str, str] = {}
        used: set[str] = set()
        for model in self.models:
            peers = by_name.get(model.name, [])
            candidate = model.name if len(peers) == 1 else f"{model.name} ({model.provider_name})"
            if candidate in used:
                candidate = f"{model.name} ({model.provider_id})"
            # Last-resort deterministic disambiguation.
            if candidate in used:
                candidate = f"{candidate} [{model.model_id}]"
            used.add(candidate)
            result[model.key] = candidate
        return result

    def public_model_id(self, model: ModelSpec) -> str:
        return self._public_model_ids().get(model.key, model.name)

    @property
    def public_models(self) -> tuple[tuple[str, ModelSpec], ...]:
        ids = self._public_model_ids()
        return tuple((ids.get(model.key, model.name), model) for model in self.models)

    def resolve_model(self, key: str) -> tuple[ProviderSpec, ModelSpec] | None:
        key = str(key or "").strip()
        if not key:
            return None

        # Preferred 0.2.3+ public id (normally the provider's human model name).
        for public_id, model in self.public_models:
            if key == public_id:
                for provider in self.providers:
                    if provider.id == model.provider_id:
                        return provider, model

        # Backward compatibility for 0.2.0-0.2.2 persisted selections.
        if "/" in key:
            provider_id, model_id = key.split("/", 1)
            for provider in self.providers:
                if provider.id == provider_id:
                    model = provider.model(model_id)
                    if model is not None:
                        return provider, model

        # Also accept a raw API model id when it is globally unambiguous.
        matches: list[tuple[ProviderSpec, ModelSpec]] = []
        for provider in self.providers:
            model = provider.model(key)
            if model is not None:
                matches.append((provider, model))
        return matches[0] if len(matches) == 1 else None


def home_dir() -> Path:
    """Backward-compatible alias for the portable Kiro BYO home resolver."""

    return app_home()


def config_path() -> Path:
    return home_dir() / "config" / "providers.json"


def _require_nonempty_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} must be a non-empty string")
    return value.strip()


def official_endpoint_for_preset(preset: Any, base_url: str) -> bool:
    """Single source of truth for 'is this base_url a preset's official endpoint'.

    This decision previously existed in four places (config loading and
    provider_admin, inline and as a helper). Keeping it here -- next to provider
    loading, which both callers already depend on -- means a native wire
    transport is only ever enabled for a verified official endpoint, and the
    rule cannot drift between the admin API and the runtime loader.
    """
    if preset is None or str(getattr(preset, "id", "custom")) == "custom":
        return False
    current = str(base_url or "").rstrip("/")
    official = {
        str(getattr(preset, "default_base_url", "") or "").rstrip("/"),
        *(str(value or "").rstrip("/") for value in (getattr(preset, "official_base_urls", ()) or ())),
        *(str(value or "").rstrip("/") for value in product_base_urls(str(getattr(preset, "id", "custom")))),
        *legacy_official_base_urls(preset),
    }
    official.discard("")
    return current in official


def _load_model(provider_id: str, provider_name: str, raw: Any) -> ModelSpec:
    if isinstance(raw, str):
        model_id = _require_nonempty_string(raw, field_name=f"provider {provider_id} model")
        return ModelSpec(provider_id, provider_name, model_id, model_id)
    if not isinstance(raw, dict):
        raise ConfigError(f"provider {provider_id} models must contain strings or objects")
    model_id = _require_nonempty_string(raw.get("id"), field_name=f"provider {provider_id} model.id")
    name_raw = raw.get("name")
    name = name_raw.strip() if isinstance(name_raw, str) and name_raw.strip() else model_id

    def _positive_int(value: Any) -> int | None:
        return value if isinstance(value, int) and value > 0 else None

    def _score(value: Any) -> float | None:
        if value is None:
            return None
        try:
            score = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(1.0, score))

    def _string_tuple(value: Any) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        return tuple(v for v in value if isinstance(v, str) and v)

    effort_levels = _string_tuple(raw.get("effort_supported_levels"))
    effort_default_raw = raw.get("effort_default_level", "")
    effort_default = effort_default_raw.strip() if isinstance(effort_default_raw, str) else ""
    reasoning = normalize_reasoning_capability(
        effort_levels=effort_levels,
        effort_default=effort_default,
        raw_reasoning=raw.get("reasoning"),
        source=str(raw.get("capability_source") or "provider"),
    )

    model_transport = str(raw.get("transport") or "").strip().lower()
    if model_transport and model_transport not in _ALLOWED_TRANSPORTS:
        raise ConfigError(
            f"provider {provider_id} model {model_id} has unsupported transport {model_transport!r}"
        )

    routing = model_routing_from_row(raw)
    provenance = routing.get("provenance") if isinstance(routing.get("provenance"), dict) else {}
    routing_provenance = tuple(
        (field, str(meta.get("source") or "unknown"), str(meta.get("confidence") or "unknown"))
        for field, meta in sorted(provenance.items())
        if isinstance(meta, dict)
    )

    return ModelSpec(
        provider_id,
        provider_name,
        model_id,
        name,
        context_window=_positive_int(raw.get("context_window")),
        max_output_tokens=_positive_int(raw.get("max_output_tokens")),
        input_modalities=_string_tuple(raw.get("input_modalities")),
        output_modalities=_string_tuple(raw.get("output_modalities")),
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
        thinking_preference=(
            str(raw.get("thinking_preference") or "auto").strip().lower()
            if str(raw.get("thinking_preference") or "auto").strip().lower() in {"auto", "on", "off"}
            else "auto"
        ),
        capability_source=str(reasoning["source"] or "provider"),
        tool_use=raw.get("tool_use") if isinstance(raw.get("tool_use"), bool) else None,
        transport=model_transport,
        invocation_target=(str(raw.get("invocation_target") or "").strip() or model_id),
        invocation_target_kind=(str(raw.get("invocation_target_kind") or "model").strip().lower() or "model"),
        lifecycle=(str(raw.get("lifecycle") or "active").strip().lower() or "active"),
        catalog_source=(str(raw.get("catalog_source") or raw.get("capability_source") or "provider").strip().lower() or "provider"),
        routing_quality=routing.get("quality"),
        routing_speed=routing.get("speed"),
        routing_economy=routing.get("economy"),
        routing_source=str(routing.get("source") or "unknown"),
        routing_confidence=str(routing.get("confidence") or "unknown"),
        routing_provenance=routing_provenance,
        routing_tiers=tuple(routing.get("tiers") or ()),
        tier=_model_tier(raw),
    )


_ROUTING_TIERS = {"fast", "balanced", "strong"}


def _model_tier(raw: dict) -> str:
    """Legacy singular routing tier; ``''`` when absent/invalid."""
    routing = raw.get("routing")
    value = routing.get("tier") if isinstance(routing, dict) else raw.get("tier")
    text = str(value or "").strip().lower()
    return text if text in _ROUTING_TIERS else ""


def _model_routing_tiers(raw: dict) -> tuple[str, ...]:
    """Return explicit many-to-many routing roles from model metadata.

    ``routing.tiers`` is preferred. The old singular ``routing.tier``/``tier``
    remains an authoritative compatibility role and is prepended when present.
    """
    routing = raw.get("routing") if isinstance(raw.get("routing"), dict) else {}
    values = routing.get("tiers") if isinstance(routing.get("tiers"), list) else raw.get("routing_tiers")
    out: list[str] = []
    singular = _model_tier(raw)
    if singular:
        out.append(singular)
    if isinstance(values, (list, tuple)):
        for value in values:
            tier = str(value or "").strip().lower()
            if tier in _ROUTING_TIERS and tier not in out:
                out.append(tier)
    return tuple(out)


def _load_provider(
    raw: Any,
    credential_store: CredentialStore | None = None,
    *,
    allow_legacy_plaintext: bool = False,
    resolve_credentials: bool = True,
) -> ProviderSpec:
    if not isinstance(raw, dict):
        raise ConfigError("each provider must be an object")
    provider_id = _require_nonempty_string(raw.get("id"), field_name="provider.id")
    if "/" in provider_id:
        raise ConfigError(f"provider.id may not contain '/': {provider_id}")
    provider_name_raw = raw.get("name")
    provider_name = (
        provider_name_raw.strip()
        if isinstance(provider_name_raw, str) and provider_name_raw.strip()
        else provider_id
    )
    preset_raw = raw.get("preset_id", "custom")
    preset_id = preset_raw.strip() if isinstance(preset_raw, str) and preset_raw.strip() else "custom"
    provider_type = str(raw.get("type") or "openai-compatible").strip().lower()
    if provider_type not in _ALLOWED_TRANSPORTS:
        raise ConfigError(
            f"provider {provider_id} has unsupported type {provider_type!r}; "
            "install a matching Kiro BYO transport adapter"
        )
    base_url = _require_nonempty_string(raw.get("base_url"), field_name=f"provider {provider_id}.base_url")

    # Upgrade only verified first-party endpoints to the installed native wire
    # adapter. Historical endpoint aliases are catalogue data, so config loading
    # does not contain provider-brand branches. Relays remain compatible-mode.
    preset = provider_preset(preset_id)
    if preset is not None and preset.id != "custom" and official_endpoint_for_preset(preset, base_url):
        if provider_type in {"openai-compatible", "openai-chat-completions", preset.transport}:
            provider_type = preset.transport
            base_url = migrate_official_base_url(preset, base_url)
    # Modern releases store live secrets outside providers.json. Legacy plaintext
    # A non-empty legacy ``api_key`` is intentionally rejected during normal
    # runtime loading. ``migrate-credentials`` is the only supported reader of
    # that legacy plaintext field.
    api_key_raw = raw.get("api_key", "")
    if not isinstance(api_key_raw, str):
        raise ConfigError(f"provider {provider_id}.api_key must be a string")
    if api_key_raw and not allow_legacy_plaintext:
        raise ConfigError(
            f"provider {provider_id}.api_key is plaintext; run 'kiro-byo migrate-credentials' "
            "or use the management UI"
        )
    credential_ref_raw = raw.get("credential_ref", "")
    if not isinstance(credential_ref_raw, str):
        raise ConfigError(f"provider {provider_id}.credential_ref must be a string")
    credential_ref = credential_ref_raw.strip()

    secret_headers: dict[str, str] = {}
    if credential_ref and resolve_credentials:
        try:
            store = credential_store or default_credential_store()
            stored = store.get(credential_ref)
        except CredentialStoreError as exc:
            raise ConfigError(
                f"provider {provider_id} credential {credential_ref!r} could not be read: {exc}"
            ) from exc
        if stored is None:
            raise ConfigError(
                f"provider {provider_id} credential {credential_ref!r} is not configured"
            )
        secrets = decode_provider_secrets(stored)
        api_key_raw = secrets.api_key
        secret_headers = dict(secrets.headers)
    elif credential_ref and not resolve_credentials:
        # Model discovery only needs provider/model metadata. Never touch the OS
        # credential store from the long-lived KiroCrew gateway model-list path.
        api_key_raw = ""

    models_raw = raw.get("models", [])
    if not isinstance(models_raw, list):
        raise ConfigError(f"provider {provider_id}.models must be an array")
    profiled_models: list[Any] = []
    official_endpoint = official_endpoint_for_preset(preset, base_url)
    # A preset name does not make an arbitrary relay vendor-native. If a saved
    # config points a native preset transport at a non-official endpoint, fail
    # safe to the generic OpenAI-compatible wire contract. This prevents
    # leaking vendor-only fields (or incompatible auth/protocol assumptions) to
    # relays while preserving the user's provider instance and model catalog.
    if (
        preset is not None
        and preset.id != "custom"
        and not official_endpoint
        and provider_type == preset.transport
        and provider_type not in {"openai-compatible", "openai-chat-completions"}
    ):
        provider_type = "openai-compatible"
    for model_row in models_raw:
        if isinstance(model_row, str):
            row = {"id": model_row, "name": model_row}
            profiled_models.append(merge_official_model_profile(preset_id, row) if official_endpoint else row)
        elif isinstance(model_row, dict):
            profiled_models.append(merge_official_model_profile(preset_id, model_row) if official_endpoint else model_row)
        else:
            profiled_models.append(model_row)
    models = tuple(_load_model(provider_id, provider_name, model) for model in profiled_models)

    default_model_raw = raw.get("default_model", "")
    default_model = default_model_raw.strip() if isinstance(default_model_raw, str) else ""
    if default_model and all(model.model_id != default_model for model in models):
        raise ConfigError(
            f"provider {provider_id}.default_model {default_model!r} is not present in its models list"
        )

    timeout_raw = raw.get("timeout_seconds", 120)
    try:
        timeout_seconds = float(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"provider {provider_id}.timeout_seconds must be a number") from exc
    if timeout_seconds <= 0:
        raise ConfigError(f"provider {provider_id}.timeout_seconds must be > 0")

    stream_raw = raw.get("stream", True)
    if not isinstance(stream_raw, bool):
        raise ConfigError(f"provider {provider_id}.stream must be true or false")
    stream = stream_raw
    path_raw = raw.get("chat_completions_path", "/chat/completions")
    chat_completions_path = _require_nonempty_string(
        path_raw, field_name=f"provider {provider_id}.chat_completions_path"
    )

    headers_raw = raw.get("headers", {})
    if not isinstance(headers_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in headers_raw.items()
    ):
        raise ConfigError(f"provider {provider_id}.headers must be a string-to-string object")
    secret_header_names = [name for name in headers_raw if is_sensitive_header(name)]
    if secret_header_names and not allow_legacy_plaintext:
        raise ConfigError(
            f"provider {provider_id}.headers contains sensitive plaintext header(s): "
            + ", ".join(secret_header_names)
            + "; run 'kiro-byo migrate-credentials' or use the management UI"
        )

    extra_body_raw = raw.get("extra_body", {})
    if not isinstance(extra_body_raw, dict):
        raise ConfigError(f"provider {provider_id}.extra_body must be an object")

    temperature: float | None = None
    if raw.get("temperature") is not None:
        try:
            temperature = float(raw["temperature"])
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"provider {provider_id}.temperature must be a number or null") from exc

    # Secret headers stored in the credential backend intentionally win over
    # same-named plaintext headers so a stale config cannot override a rotated
    # token.
    resolved_headers = dict(headers_raw)
    resolved_headers.update(secret_headers)

    inferred_identity = connection_identity(preset_id, base_url)
    # For a known official Product/Profile endpoint, endpoint metadata is the
    # authoritative commercial identity. Persisted identity fields are merely a
    # cache and may be stale after a user changes region/plan or after a product
    # overlay updates an endpoint. Arbitrary relays keep their explicit saved
    # identity because an unknown URL cannot prove a commercial product.
    if official_endpoint:
        # URL is normally authoritative, except when multiple commercial
        # Products intentionally share the same official inference endpoint.
        # In that ambiguous case preserve only a persisted identity that still
        # validates against the current catalogue/profile URL.
        saved_identity = saved_connection_identity(
            preset_id, base_url, str(raw.get("product_id") or ""), str(raw.get("profile_id") or ""),
        )
        resolved_identity = saved_identity or inferred_identity
        platform_id = resolved_identity.platform_id
        product_id = resolved_identity.product_id
        profile_id = resolved_identity.profile_id
        entitlement = resolved_identity.entitlement
    else:
        platform_id = str(raw.get("platform_id") or inferred_identity.platform_id).strip().lower()
        product_id = str(raw.get("product_id") or inferred_identity.product_id).strip().lower()
        profile_id = str(raw.get("profile_id") or inferred_identity.profile_id).strip().lower()
        entitlement = str(raw.get("entitlement") or inferred_identity.entitlement).strip().lower()

    return ProviderSpec(
        id=provider_id,
        name=provider_name,
        type=provider_type,
        base_url=base_url.rstrip("/"),
        api_key=api_key_raw,
        models=models,
        credential_ref=credential_ref,
        default_model=default_model,
        timeout_seconds=timeout_seconds,
        stream=stream,
        chat_completions_path=chat_completions_path,
        headers=resolved_headers,
        extra_body=dict(extra_body_raw),
        temperature=temperature,
        preset_id=preset_id,
        platform_id=platform_id,
        product_id=product_id,
        profile_id=profile_id,
        entitlement=entitlement,
    )


def _load_config_from_path(
    path: Path,
    credential_store: CredentialStore | None = None,
    *,
    resolve_credentials: bool,
) -> KiroByoConfig:
    if not path.exists():
        return KiroByoConfig(providers=())
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("providers.json root must be an object")
    providers_raw = raw.get("providers", [])
    if not isinstance(providers_raw, list):
        raise ConfigError("providers must be an array")
    try:
        credential_schema_version = int(raw.get("credential_schema_version") or 0)
    except (TypeError, ValueError):
        raise ConfigError("credential_schema_version must be an integer")
    allow_legacy_plaintext = credential_schema_version < 1
    providers = tuple(
        _load_provider(
            provider,
            credential_store,
            allow_legacy_plaintext=allow_legacy_plaintext,
            resolve_credentials=resolve_credentials,
        )
        for provider in providers_raw
    )
    ids = [provider.id for provider in providers]
    if len(ids) != len(set(ids)):
        raise ConfigError("provider ids must be unique")

    model_keys = [model.key for provider in providers for model in provider.models]
    if len(model_keys) != len(set(model_keys)):
        raise ConfigError("provider/model keys must be unique")

    default_raw = raw.get("default_model", "")
    default_model = default_raw.strip() if isinstance(default_raw, str) else ""
    router_raw = raw.get("mode") if isinstance(raw.get("mode"), dict) else {}
    try:
        fallback_pack = default_pack()
    except PolicyPackError as exc:
        raise ConfigError(f"mode router has no usable policy pack: {exc}") from exc
    mode_pack = str(router_raw.get("id") or fallback_pack.id).strip().lower()
    requested_policy = str(router_raw.get("type") or "").strip().lower()
    try:
        pack, policy = resolve_policy(mode_pack, requested_policy or (fallback_pack.default_policy if mode_pack == fallback_pack.id else ""))
    except PolicyPackError:
        # If the selected pack exists but policy is omitted, use that pack's own
        # default. Explicit invalid ids still fail closed instead of silently
        # changing the user's execution policy.
        from .policy_packs import get_policy_pack
        selected_pack = get_policy_pack(mode_pack)
        if selected_pack is None:
            raise ConfigError(f"mode.id {mode_pack!r} is not installed")
        if not selected_pack.enabled:
            raise ConfigError(f"mode.id {mode_pack!r} is not available yet")
        if requested_policy:
            raise ConfigError(f"mode.type {requested_policy!r} is not present in pack {mode_pack!r}")
        pack, policy = resolve_policy(mode_pack, selected_pack.default_policy)
    mode_pack = pack.id
    mode_type = policy.id
    preferences_raw = raw.get("preferences") if isinstance(raw.get("preferences"), dict) else {}
    reasoning_language = normalize_reasoning_language(preferences_raw.get("reasoning_language", "auto"))
    from .reasoning_language import normalize_supported_language
    try:
        response_language = normalize_supported_language(preferences_raw.get("response_language", "auto"))
    except ValueError:
        response_language = "auto"
    from .i18n import normalize_ui_language
    try:
        ui_language = normalize_ui_language(preferences_raw.get("ui_language", "auto"))
    except ValueError:
        ui_language = "auto"
    config = KiroByoConfig(
        providers=providers,
        default_model=default_model,
        mode_pack=mode_pack,
        mode_type=mode_type,
        reasoning_language=reasoning_language,
        response_language=response_language,
        ui_language=ui_language,
    )
    if default_model and config.resolve_model(default_model) is None:
        raise ConfigError(f"default_model {default_model!r} is not present in providers[].models")
    return config


def load_config(credential_store: CredentialStore | None = None) -> KiroByoConfig:
    """Load runtime configuration and resolve provider credentials."""

    return _load_config_from_path(
        config_path(),
        credential_store,
        resolve_credentials=True,
    )


def load_catalog_config(path: Path | None = None) -> KiroByoConfig:
    """Load provider/model metadata without reading any live credential.

    The KiroCrew dashboard model picker runs inside the long-lived gateway. Model
    discovery must not depend on that process having access to Windows Credential
    Manager/Keychain/Secret Service; it only needs the persisted model catalog.
    An explicit *path* lets the host pass its authoritative app data directory and
    avoids ambient ``KIRO_BYO_HOME`` or import-provenance differences entirely.
    """

    target = Path(path) if path is not None else config_path()
    return _load_config_from_path(
        target,
        None,
        resolve_credentials=False,
    )
