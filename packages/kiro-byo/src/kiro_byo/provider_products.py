"""Commercial platform/product/profile metadata for provider connections.

Protocol adapters deliberately live elsewhere.  A new billing plan, regional
endpoint, entitlement bundle or model-catalogue profile must be representable as
data without creating another provider adapter class.

Built-in records are projected from the legacy 0.1.0 provider presets so old
clients keep working.  Fast-moving product/profile facts may additionally be
supplied through ``$KIRO_BYO_HOME/config/provider_products.override.json``.
That overlay can add products/regions or replace profile metadata while reusing
an already-installed adapter transport; no Python adapter class is generated.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from .paths import app_home


@dataclass(frozen=True)
class ConnectionProfile:
    id: str
    display_name: str
    base_url: str
    entitlement: str = "usage"
    region: str = ""
    discovery_url: str = ""
    discovery_auth: str = "bearer"
    discovery_headers: tuple[tuple[str, str], ...] = ()
    # Bundled model ids are a *fallback snapshot*, never an authoritative
    # entitlement list. Live discovery wins whenever the endpoint supports it.
    static_model_ids: tuple[str, ...] = ()
    discovery_model_allowlist: tuple[str, ...] = ()
    catalog_policy: str = "live"  # live | live-with-fallback | fallback
    invocation_target_kind: str = "model"

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "base_url": self.base_url,
            "entitlement": self.entitlement,
            "region": self.region,
            "discovery_url": self.discovery_url,
            "static_model_ids": list(self.static_model_ids),
            "catalog_policy": self.catalog_policy,
            "invocation_target_kind": self.invocation_target_kind,
        }


@dataclass(frozen=True)
class ProviderProduct:
    id: str
    legacy_preset_id: str
    display_name: str
    adapter_transport: str
    default_profile_id: str
    profiles: tuple[ConnectionProfile, ...]
    requires_api_key: bool = True
    account_capabilities: tuple[str, ...] = ()
    model_transport_overrides: bool = False
    # Optional credential-family hint used only to choose a commercial Product
    # profile before discovery. It never changes the protocol adapter.
    credential_prefixes: tuple[str, ...] = ()
    # Internal dispatcher key. Deliberately omitted from public_dict so product
    # metadata does not expose Python handler names as public API.
    account_handler: str = ""

    def profile(self, profile_id: str) -> ConnectionProfile | None:
        wanted = str(profile_id or "").strip().lower()
        return next((p for p in self.profiles if p.id == wanted), None)

    @property
    def default_profile(self) -> ConnectionProfile:
        return self.profile(self.default_profile_id) or self.profiles[0]

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "legacy_preset_id": self.legacy_preset_id,
            "display_name": self.display_name,
            "adapter_transport": self.adapter_transport,
            "default_profile_id": self.default_profile.id,
            "requires_api_key": self.requires_api_key,
            "account_capabilities": list(self.account_capabilities),
            "model_transport_overrides": self.model_transport_overrides,
            "credential_prefixes": list(self.credential_prefixes),
            "profiles": [p.public_dict() for p in self.profiles],
        }


@dataclass(frozen=True)
class ProviderPlatform:
    id: str
    display_name: str
    category: str
    products: tuple[ProviderProduct, ...]

    def product(self, product_id: str) -> ProviderProduct | None:
        wanted = str(product_id or "").strip().lower()
        return next((p for p in self.products if p.id == wanted), None)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "category": self.category,
            "products": [p.public_dict() for p in self.products],
        }


@dataclass(frozen=True)
class ConnectionIdentity:
    platform_id: str
    product_id: str
    profile_id: str
    entitlement: str


# Built-in commercial differences are data, not Python vendor branches. Generic
# presets still project to one API product/profile; only providers with multiple
# plans/regions need rows in this file.
_BUILTIN_PRODUCT_DATA_PATH = Path(__file__).with_name("catalog") / "provider_products.v1.json"


class ProviderProductCatalogError(RuntimeError):
    pass


def _load_builtin_product_data() -> dict[str, Any]:
    try:
        raw = json.loads(_BUILTIN_PRODUCT_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderProductCatalogError(
            f"could not load {_BUILTIN_PRODUCT_DATA_PATH.name}: {exc}"
        ) from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ProviderProductCatalogError("provider_products.v1.json: unsupported or missing schema_version")
    for key in ("platform_groups", "product_ids", "platform_display_names", "account_handlers", "credential_rejections", "special_profiles"):
        if not isinstance(raw.get(key, {}), dict):
            raise ProviderProductCatalogError(f"provider_products.v1.json.{key}: expected object")
    return raw


_BUILTIN_PRODUCT_DATA = _load_builtin_product_data()
_PLATFORM_GROUP = {str(k): str(v) for k, v in _BUILTIN_PRODUCT_DATA.get("platform_groups", {}).items()}
_PRODUCT_IDS = {str(k): str(v) for k, v in _BUILTIN_PRODUCT_DATA.get("product_ids", {}).items()}
_PLATFORM_DISPLAY_NAMES = {str(k): str(v) for k, v in _BUILTIN_PRODUCT_DATA.get("platform_display_names", {}).items()}
_ACCOUNT_HANDLERS = {str(k): str(v) for k, v in _BUILTIN_PRODUCT_DATA.get("account_handlers", {}).items()}
_CREDENTIAL_REJECTIONS = _BUILTIN_PRODUCT_DATA.get("credential_rejections", {})
_SPECIAL_PROFILES = _BUILTIN_PRODUCT_DATA.get("special_profiles", {})


def _generic_profile(preset: Any) -> ConnectionProfile:
    entitlement = "relay" if str(getattr(preset, "id", "")) == "custom" else "usage"
    static = tuple(getattr(preset, "static_model_ids", ()) or ())
    return ConnectionProfile(
        id="default",
        display_name="Default",
        base_url=str(getattr(preset, "default_base_url", "") or ""),
        entitlement=entitlement,
        discovery_url=str(getattr(preset, "discovery_url", "") or ""),
        discovery_auth=str(getattr(preset, "discovery_auth", "bearer") or "bearer"),
        discovery_headers=tuple(getattr(preset, "discovery_headers", ()) or ()),
        static_model_ids=static,
        discovery_model_allowlist=tuple(getattr(preset, "discovery_model_allowlist", ()) or ()),
        catalog_policy="live-with-fallback" if static else "live",
    )


def _builtin_profile(raw: dict[str, Any], preset: Any) -> ConnectionProfile:
    static = tuple(getattr(preset, "static_model_ids", ()) or ()) if raw.get("static_model_ids_from_preset") is True else _strings(raw.get("static_model_ids"))
    return ConnectionProfile(
        id=str(raw.get("id") or "default").strip().lower() or "default",
        display_name=str(raw.get("display_name") or raw.get("id") or "Default"),
        base_url=str(raw.get("base_url") or "").strip().rstrip("/"),
        entitlement=str(raw.get("entitlement") or "usage").strip().lower() or "usage",
        region=str(raw.get("region") or "").strip().lower(),
        discovery_url=str(raw.get("discovery_url") or "").strip(),
        discovery_auth=str(raw.get("discovery_auth") or "bearer").strip().lower() or "bearer",
        discovery_headers=(),
        static_model_ids=static,
        discovery_model_allowlist=_strings(raw.get("discovery_model_allowlist")),
        catalog_policy=str(raw.get("catalog_policy") or "live").strip().lower() or "live",
        invocation_target_kind=str(raw.get("invocation_target_kind") or "model").strip().lower() or "model",
    )


def _special_config(preset: Any) -> dict[str, Any]:
    raw = _SPECIAL_PROFILES.get(str(getattr(preset, "id", "") or ""), {})
    return raw if isinstance(raw, dict) else {}


def _profiles_for(preset: Any) -> tuple[ConnectionProfile, ...]:
    config = _special_config(preset)
    rows = config.get("profiles")
    if not isinstance(rows, list) or not rows:
        return (_generic_profile(preset),)
    profiles = tuple(_builtin_profile(row, preset) for row in rows if isinstance(row, dict))
    return profiles or (_generic_profile(preset),)


def _builtin_platform_catalog(presets: Iterable[Any]) -> tuple[ProviderPlatform, ...]:
    grouped: dict[str, list[ProviderProduct]] = {}
    labels: dict[str, tuple[str, str]] = {}
    for preset in presets:
        preset_id = str(getattr(preset, "id", "") or "").strip().lower()
        platform_id = _PLATFORM_GROUP.get(preset_id, preset_id)
        product_id = _PRODUCT_IDS.get(preset_id, "api")
        config = _special_config(preset)
        profiles = _profiles_for(preset)
        default_profile = str(config.get("default_profile_id") or profiles[0].id).strip().lower()
        if not any(profile.id == default_profile for profile in profiles):
            default_profile = profiles[0].id
        product = ProviderProduct(
            id=product_id,
            legacy_preset_id=preset_id,
            display_name=str(getattr(preset, "display_name", preset_id) or preset_id),
            adapter_transport=str(getattr(preset, "transport", "openai-compatible") or "openai-compatible"),
            default_profile_id=default_profile,
            profiles=profiles,
            requires_api_key=bool(getattr(preset, "requires_api_key", True)),
            account_capabilities=tuple(getattr(preset, "account_capabilities", ()) or ()),
            model_transport_overrides=bool(getattr(preset, "model_transport_overrides", False)),
            credential_prefixes=_strings(config.get("credential_prefixes")),
            account_handler=_ACCOUNT_HANDLERS.get(preset_id, str(config.get("account_handler") or "")),
        )
        grouped.setdefault(platform_id, []).append(product)

        extra_products = config.get("extra_products") if isinstance(config.get("extra_products"), list) else []
        for raw_extra in extra_products:
            if not isinstance(raw_extra, dict):
                continue
            entitlement_override = str(raw_extra.get("entitlement_override") or "").strip().lower()
            extra_profiles = tuple(
                replace(profile, entitlement=entitlement_override) if entitlement_override else profile
                for profile in profiles
            )
            grouped[platform_id].append(ProviderProduct(
                id=str(raw_extra.get("id") or "").strip().lower(),
                legacy_preset_id=preset_id,
                display_name=str(raw_extra.get("display_name") or raw_extra.get("id") or preset_id),
                adapter_transport=product.adapter_transport,
                default_profile_id=str(raw_extra.get("default_profile_id") or default_profile).strip().lower(),
                profiles=extra_profiles,
                requires_api_key=bool(raw_extra.get("requires_api_key", product.requires_api_key)),
                account_capabilities=_strings(raw_extra.get("account_capabilities")),
                model_transport_overrides=product.model_transport_overrides,
                credential_prefixes=_strings(raw_extra.get("credential_prefixes")),
                account_handler=str(raw_extra.get("account_handler") or ""),
            ))

        display = _PLATFORM_DISPLAY_NAMES.get(platform_id, str(getattr(preset, "display_name", platform_id) or platform_id))
        labels.setdefault(platform_id, (display, str(getattr(preset, "category", "official") or "official")))
    return tuple(
        ProviderPlatform(pid, labels[pid][0], labels[pid][1], tuple(products))
        for pid, products in grouped.items()
    )


def credential_rejection_message(preset_id: str, credential: str) -> str:
    """Return a data-declared mismatch message for a credential family."""

    value = str(credential or "").strip().lower()
    rows = _CREDENTIAL_REJECTIONS.get(str(preset_id or "").strip().lower(), ())
    if not isinstance(rows, list):
        return ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        prefix = str(row.get("prefix") or "").strip().lower()
        if prefix and value.startswith(prefix):
            return str(row.get("message") or "").strip()
    return ""


def product_override_path() -> Path:
    return app_home() / "config" / "provider_products.override.json"


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if isinstance(item, str) and item.strip())


def _profile_from_dict(raw: Any) -> ConnectionProfile | None:
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get("id") or "").strip().lower()
    base_url = str(raw.get("base_url") or "").strip().rstrip("/")
    if not pid or not base_url:
        return None
    headers_raw = raw.get("discovery_headers")
    headers: list[tuple[str, str]] = []
    if isinstance(headers_raw, dict):
        headers = [(str(k), str(v)) for k, v in headers_raw.items() if str(k)]
    elif isinstance(headers_raw, list):
        for item in headers_raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                headers.append((str(item[0]), str(item[1])))
    policy = str(raw.get("catalog_policy") or "live").strip().lower()
    if policy not in {"live", "live-with-fallback", "fallback"}:
        policy = "live"
    target_kind = str(raw.get("invocation_target_kind") or "model").strip().lower() or "model"
    return ConnectionProfile(
        id=pid,
        display_name=str(raw.get("display_name") or pid),
        base_url=base_url,
        entitlement=str(raw.get("entitlement") or "usage").strip().lower() or "usage",
        region=str(raw.get("region") or "").strip().lower(),
        discovery_url=str(raw.get("discovery_url") or "").strip(),
        discovery_auth=str(raw.get("discovery_auth") or "bearer").strip().lower() or "bearer",
        discovery_headers=tuple(headers),
        static_model_ids=_strings(raw.get("static_model_ids") or raw.get("fallback_model_ids")),
        discovery_model_allowlist=_strings(raw.get("discovery_model_allowlist")),
        catalog_policy=policy,
        invocation_target_kind=target_kind,
    )


def _product_from_dict(raw: Any) -> ProviderProduct | None:
    if not isinstance(raw, dict):
        return None
    product_id = str(raw.get("id") or "").strip().lower()
    legacy = str(raw.get("legacy_preset_id") or "").strip().lower()
    adapter = str(raw.get("adapter_transport") or "").strip().lower()
    profiles = tuple(filter(None, (_profile_from_dict(item) for item in (raw.get("profiles") or []))))
    if not product_id or not legacy or not adapter or not profiles:
        return None
    default_profile = str(raw.get("default_profile_id") or profiles[0].id).strip().lower()
    if not any(profile.id == default_profile for profile in profiles):
        default_profile = profiles[0].id
    return ProviderProduct(
        id=product_id,
        legacy_preset_id=legacy,
        display_name=str(raw.get("display_name") or product_id),
        adapter_transport=adapter,
        default_profile_id=default_profile,
        profiles=profiles,
        requires_api_key=bool(raw.get("requires_api_key", True)),
        account_capabilities=_strings(raw.get("account_capabilities")),
        model_transport_overrides=bool(raw.get("model_transport_overrides", False)),
        credential_prefixes=_strings(raw.get("credential_prefixes")),
        account_handler=str(raw.get("account_handler") or "").strip(),
    )


def _load_override_platforms() -> tuple[ProviderPlatform, ...]:
    path = product_override_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(raw, dict):
        return ()
    try:
        schema_version = int(raw.get("schema_version") or 1)
    except (TypeError, ValueError):
        return ()
    if schema_version != 1:
        return ()
    result: list[ProviderPlatform] = []
    for platform_raw in raw.get("platforms") or []:
        if not isinstance(platform_raw, dict):
            continue
        platform_id = str(platform_raw.get("id") or "").strip().lower()
        products = tuple(filter(None, (_product_from_dict(item) for item in (platform_raw.get("products") or []))))
        if not platform_id or not products:
            continue
        result.append(ProviderPlatform(
            id=platform_id,
            display_name=str(platform_raw.get("display_name") or platform_id),
            category=str(platform_raw.get("category") or "official").strip().lower() or "official",
            products=products,
        ))
    return tuple(result)


def _merge_profile(base: ConnectionProfile, override: ConnectionProfile) -> ConnectionProfile:
    # Overlay rows are complete profile declarations. Keep the id stable and use
    # their fields verbatim; this lets operators update a fast-moving endpoint or
    # fallback entitlement snapshot without editing Python.
    return replace(override, id=base.id)


def _merge_product(base: ProviderProduct, override: ProviderProduct) -> ProviderProduct:
    profiles = {p.id: p for p in base.profiles}
    order = [p.id for p in base.profiles]
    for profile in override.profiles:
        if profile.id in profiles:
            profiles[profile.id] = _merge_profile(profiles[profile.id], profile)
        else:
            order.append(profile.id)
            profiles[profile.id] = profile
    default_profile = override.default_profile_id if override.default_profile_id in profiles else base.default_profile_id
    return ProviderProduct(
        id=base.id,
        legacy_preset_id=override.legacy_preset_id or base.legacy_preset_id,
        display_name=override.display_name or base.display_name,
        adapter_transport=override.adapter_transport or base.adapter_transport,
        default_profile_id=default_profile,
        profiles=tuple(profiles[key] for key in order),
        requires_api_key=override.requires_api_key,
        account_capabilities=override.account_capabilities or base.account_capabilities,
        model_transport_overrides=override.model_transport_overrides or base.model_transport_overrides,
        credential_prefixes=override.credential_prefixes or base.credential_prefixes,
        account_handler=override.account_handler or base.account_handler,
    )


def build_platform_catalog(presets: Iterable[Any]) -> tuple[ProviderPlatform, ...]:
    """Build catalogue and apply optional data-only product/profile overlays."""
    presets = tuple(presets)
    builtins = list(_builtin_platform_catalog(presets))
    by_id = {platform.id: platform for platform in builtins}
    order = [platform.id for platform in builtins]
    code_transports = {
        str(getattr(preset, "id", "") or "").strip().lower():
        str(getattr(preset, "transport", "openai-compatible") or "openai-compatible").strip().lower()
        for preset in presets
    }
    for override in _load_override_platforms():
        existing = by_id.get(override.id)
        if existing is None:
            # A new Platform may be described in data only when every Product
            # reuses an already-installed preset family and its code-owned wire
            # adapter. Metadata may extend products/regions/endpoints; it may not
            # mint an executable protocol under a new platform id.
            products = tuple(
                product for product in override.products
                if code_transports.get(product.legacy_preset_id) == product.adapter_transport
            )
            if not products:
                continue
            by_id[override.id] = replace(override, products=products)
            order.append(override.id)
            continue
        products = {product.id: product for product in existing.products}
        product_order = [product.id for product in existing.products]
        for product in override.products:
            if product.id in products:
                # Product/profile overlays are data, not executable protocol
                # switches. Existing product transport remains code-owned.
                products[product.id] = _merge_product(products[product.id], replace(product, adapter_transport=products[product.id].adapter_transport))
            else:
                family = next((item for item in existing.products if item.legacy_preset_id == product.legacy_preset_id), None)
                if family is None or product.adapter_transport != family.adapter_transport:
                    # A genuinely different wire protocol requires an installed
                    # code adapter/release; metadata may not claim one into use.
                    continue
                products[product.id] = product
                product_order.append(product.id)
        by_id[override.id] = ProviderPlatform(
            id=existing.id,
            display_name=override.display_name or existing.display_name,
            category=override.category or existing.category,
            products=tuple(products[key] for key in product_order),
        )
    return tuple(by_id[key] for key in order)


def resolve_connection_identity(
    presets: Iterable[Any], *, preset_id: str, base_url: str,
) -> ConnectionIdentity:
    wanted_preset = str(preset_id or "custom").strip().lower()
    current = str(base_url or "").strip().rstrip("/")
    catalog = build_platform_catalog(presets)
    candidate_platforms = [
        platform for platform in catalog
        if any(product.legacy_preset_id == wanted_preset for product in platform.products)
    ]
    if current:
        for platform in candidate_platforms:
            for product in platform.products:
                for profile in product.profiles:
                    if current == profile.base_url.rstrip("/"):
                        return ConnectionIdentity(platform.id, product.id, profile.id, profile.entitlement)
    for platform in candidate_platforms:
        for product in platform.products:
            if product.legacy_preset_id == wanted_preset:
                profile = product.default_profile
                return ConnectionIdentity(platform.id, product.id, profile.id, profile.entitlement)
    return ConnectionIdentity("custom", "compatible-endpoint", "default", "relay")


def resolve_saved_connection_identity(
    presets: Iterable[Any], *, preset_id: str, base_url: str, product_id: str, profile_id: str = "",
) -> ConnectionIdentity | None:
    """Validate a persisted Product identity for an otherwise ambiguous URL.

    Multiple commercial products can intentionally share one inference endpoint
    (for example MiniMax PAYG and Token Plan). In that case URL inference alone
    cannot recover the billing product, so a saved identity may be trusted only
    when the declared Product/Profile exists and that profile owns the exact
    current official endpoint.
    """
    wanted_preset = str(preset_id or "custom").strip().lower()
    wanted_product = str(product_id or "").strip().lower()
    wanted_profile = str(profile_id or "").strip().lower()
    current = str(base_url or "").strip().rstrip("/")
    if not wanted_product or not current:
        return None
    for platform in build_platform_catalog(presets):
        for product in platform.products:
            if product.legacy_preset_id != wanted_preset or product.id != wanted_product:
                continue
            candidates = product.profiles
            if wanted_profile:
                candidates = tuple(profile for profile in candidates if profile.id == wanted_profile)
            for profile in candidates:
                if profile.base_url.rstrip("/") == current:
                    return ConnectionIdentity(platform.id, product.id, profile.id, profile.entitlement)
    return None


def resolve_connection_record(
    presets: Iterable[Any], *, preset_id: str, base_url: str,
) -> tuple[ProviderPlatform, ProviderProduct, ConnectionProfile] | None:
    """Return the concrete platform/product/profile record for a connection."""
    wanted_preset = str(preset_id or "custom").strip().lower()
    current = str(base_url or "").strip().rstrip("/")
    catalog = build_platform_catalog(presets)
    candidate_platforms = [
        platform for platform in catalog
        if any(product.legacy_preset_id == wanted_preset for product in platform.products)
    ]
    if current:
        for platform in candidate_platforms:
            for product in platform.products:
                for profile in product.profiles:
                    if current == profile.base_url.rstrip("/"):
                        return platform, product, profile
    for platform in candidate_platforms:
        for product in platform.products:
            if product.legacy_preset_id == wanted_preset:
                return platform, product, product.default_profile
    return None


def connection_profile_for_credential(
    presets: Iterable[Any], *, preset_id: str, credential: str,
) -> tuple[ProviderPlatform, ProviderProduct, ConnectionProfile] | None:
    """Resolve a Product default profile from a documented credential prefix.

    This is a commercial routing hint only. The returned Product must already
    belong to the selected platform family and continues to use its installed
    code-owned adapter transport.
    """
    wanted = str(preset_id or "custom").strip().lower()
    value = str(credential or "").strip().lower()
    if not value:
        return None
    catalog = build_platform_catalog(presets)
    platforms = [
        platform for platform in catalog
        if any(product.legacy_preset_id == wanted for product in platform.products)
    ]
    for platform in platforms:
        for product in platform.products:
            if any(value.startswith(prefix.lower()) for prefix in product.credential_prefixes):
                return platform, product, product.default_profile
    return None


def account_handler_for_connection(
    presets: Iterable[Any], *, preset_id: str, base_url: str, product_id: str = "", credential: str = "",
) -> str:
    """Resolve an account-query strategy without exposing vendor ids to callers."""

    wanted = str(preset_id or "custom").strip().lower()
    wanted_product = str(product_id or "").strip().lower()
    value = str(credential or "").strip().lower()
    current = str(base_url or "").strip().rstrip("/")
    candidates: list[ProviderProduct] = []
    for platform in build_platform_catalog(presets):
        candidates.extend(product for product in platform.products if product.legacy_preset_id == wanted)
    if wanted_product:
        exact = next((product for product in candidates if product.id == wanted_product), None)
        if exact is not None:
            return exact.account_handler
    if value:
        hinted = next((
            product for product in candidates
            if product.credential_prefixes and any(value.startswith(prefix.lower()) for prefix in product.credential_prefixes)
        ), None)
        if hinted is not None:
            return hinted.account_handler
    if current:
        for product in candidates:
            if any(current == profile.base_url.rstrip("/") for profile in product.profiles):
                if product.account_handler:
                    return product.account_handler
    return next((product.account_handler for product in candidates if product.account_handler), "")


def public_platform_catalog(presets: Iterable[Any]) -> list[dict[str, Any]]:
    return [platform.public_dict() for platform in build_platform_catalog(presets)]


def product_for_preset(presets: Iterable[Any], preset_id: str) -> tuple[ProviderPlatform, ProviderProduct] | None:
    wanted = str(preset_id or "").strip().lower()
    for platform in build_platform_catalog(presets):
        for product in platform.products:
            if product.legacy_preset_id == wanted:
                return platform, product
    return None


def products_for_preset(presets: Iterable[Any], preset_id: str) -> tuple[tuple[ProviderPlatform, ProviderProduct], ...]:
    """Return *all* products that reuse one legacy/provider family.

    This is what lets a newly-added plan/profile become an official endpoint
    without adding a preset or protocol adapter.
    """
    wanted = str(preset_id or "").strip().lower()
    rows: list[tuple[ProviderPlatform, ProviderProduct]] = []
    for platform in build_platform_catalog(presets):
        for product in platform.products:
            if product.legacy_preset_id == wanted:
                rows.append((platform, product))
    return tuple(rows)
