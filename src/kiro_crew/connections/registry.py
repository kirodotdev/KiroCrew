"""Load and query the curated official MCP provider registry."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, TypedDict, cast
from urllib.parse import urlsplit


class SmokeFixture(TypedDict):
    """A safe read-only tool invocation used by provider smoke tests."""

    tool: str
    args: dict[str, Any]


class L0Expectations(TypedDict):
    """OAuth discovery properties asserted by the account-free L0 probe."""

    authorization_server_origin: str
    dcr: bool
    pkce: bool


class Provider(TypedDict):
    """One official MCP provider exposed to the Connections experience."""

    name: str
    slug: str
    tier: int
    mcp_url: str
    recommended_scopes: list[str]
    revoke_page_url: str
    docs_url: str
    gotcha_copy: str
    smoke_fixture: SmokeFixture
    l0_expectations: L0Expectations
    launch_gate_passed: bool
    vendor_approval_pending: bool


class RegistryValidationError(ValueError):
    """Raised when the committed provider registry is malformed."""


_REGISTRY_PATH = Path(__file__).with_name("registry.json")
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PROVIDER_FIELDS = {
    "name",
    "slug",
    "tier",
    "mcp_url",
    "recommended_scopes",
    "revoke_page_url",
    "docs_url",
    "gotcha_copy",
    "smoke_fixture",
    "l0_expectations",
    "launch_gate_passed",
    "vendor_approval_pending",
}
_SMOKE_FIXTURE_FIELDS = {"tool", "args"}
_L0_EXPECTATION_FIELDS = {"authorization_server_origin", "dcr", "pkce"}


def _validation_error(index: int, message: str) -> RegistryValidationError:
    return RegistryValidationError(f"provider at index {index}: {message}")


def _validate_provider(raw: object, index: int) -> Provider:
    if not isinstance(raw, dict):
        raise _validation_error(index, "must be an object")

    fields = set(raw)
    missing = _PROVIDER_FIELDS - fields
    extra = fields - _PROVIDER_FIELDS
    if missing:
        raise _validation_error(index, f"missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise _validation_error(index, f"unknown fields: {', '.join(sorted(extra))}")

    for field in ("name", "slug", "mcp_url", "revoke_page_url", "docs_url", "gotcha_copy"):
        value = raw[field]
        if not isinstance(value, str) or not value.strip():
            raise _validation_error(index, f"{field} must be a non-empty string")

    slug = cast(str, raw["slug"])
    if not _SLUG_PATTERN.fullmatch(slug):
        raise _validation_error(index, "slug must contain lowercase letters, numbers, and hyphens")

    for field in ("mcp_url", "revoke_page_url", "docs_url"):
        if not cast(str, raw[field]).startswith("https://"):
            raise _validation_error(index, f"{field} must use HTTPS")

    tier = raw["tier"]
    if isinstance(tier, bool) or not isinstance(tier, int) or tier not in (1, 2, 3):
        raise _validation_error(index, "tier must be 1, 2, or 3")

    scopes = raw["recommended_scopes"]
    if not isinstance(scopes, list) or any(
        not isinstance(scope, str) or not scope.strip() for scope in scopes
    ):
        raise _validation_error(index, "recommended_scopes must be a list of strings")
    if len(scopes) != len(set(scopes)):
        raise _validation_error(index, "recommended_scopes must not contain duplicates")

    fixture = raw["smoke_fixture"]
    if not isinstance(fixture, dict) or set(fixture) != _SMOKE_FIXTURE_FIELDS:
        raise _validation_error(index, "smoke_fixture must contain exactly tool and args")
    if not isinstance(fixture["tool"], str) or not fixture["tool"].strip():
        raise _validation_error(index, "smoke_fixture.tool must be a non-empty string")
    if not isinstance(fixture["args"], dict):
        raise _validation_error(index, "smoke_fixture.args must be an object")

    expectations = raw["l0_expectations"]
    if not isinstance(expectations, dict) or set(expectations) != _L0_EXPECTATION_FIELDS:
        raise _validation_error(
            index,
            "l0_expectations must contain exactly authorization_server_origin, dcr, and pkce",
        )
    for field in ("dcr", "pkce"):
        if not isinstance(expectations[field], bool):
            raise _validation_error(index, f"l0_expectations.{field} must be a boolean")
    authorization_origin = expectations["authorization_server_origin"]
    if not isinstance(authorization_origin, str):
        raise _validation_error(
            index, "l0_expectations.authorization_server_origin must be an HTTPS origin"
        )
    try:
        authorization_parts = urlsplit(authorization_origin)
        authorization_parts.port
    except ValueError as error:
        raise _validation_error(
            index, "l0_expectations.authorization_server_origin must be an HTTPS origin"
        ) from error
    if (
        authorization_parts.scheme != "https"
        or authorization_parts.hostname is None
        or authorization_parts.username is not None
        or authorization_parts.password is not None
        or authorization_parts.path not in ("", "/")
        or authorization_parts.query
        or authorization_parts.fragment
    ):
        raise _validation_error(
            index, "l0_expectations.authorization_server_origin must be an HTTPS origin"
        )

    for field in ("launch_gate_passed", "vendor_approval_pending"):
        if not isinstance(raw[field], bool):
            raise _validation_error(index, f"{field} must be a boolean")

    launch_gate_passed = cast(bool, raw["launch_gate_passed"])
    vendor_approval_pending = cast(bool, raw["vendor_approval_pending"])
    if tier == 3:
        if not vendor_approval_pending:
            raise _validation_error(index, "Tier 3 providers must be vendor-approval pending")
        if launch_gate_passed:
            raise _validation_error(index, "Tier 3 providers cannot pass the launch gate")
    elif vendor_approval_pending:
        raise _validation_error(index, "vendor approval is only meaningful for Tier 3")

    return cast(Provider, raw)


def _load_registry(path: Path = _REGISTRY_PATH) -> tuple[Provider, ...]:
    try:
        with path.open(encoding="utf-8") as registry_file:
            raw_registry = json.load(registry_file)
    except (OSError, json.JSONDecodeError) as error:
        raise RegistryValidationError(f"could not load provider registry: {error}") from error

    if not isinstance(raw_registry, list):
        raise RegistryValidationError("provider registry root must be an array")

    providers: list[Provider] = []
    seen_slugs: set[str] = set()
    for index, raw_provider in enumerate(raw_registry):
        provider = _validate_provider(raw_provider, index)
        slug = provider["slug"]
        if slug in seen_slugs:
            raise _validation_error(index, f"duplicate slug: {slug}")
        seen_slugs.add(slug)
        providers.append(provider)

    if not providers:
        raise RegistryValidationError("provider registry must not be empty")
    return tuple(providers)


_PROVIDERS = _load_registry()
_PROVIDERS_BY_SLUG = {provider["slug"]: provider for provider in _PROVIDERS}


def _copy_provider(provider: Provider) -> Provider:
    """Keep callers from mutating the process-wide validated registry."""

    return cast(Provider, deepcopy(provider))


def get_all_registry_providers() -> list[Provider]:
    """Return every registry entry, including launch-gated and vendor-blocked entries."""

    return [_copy_provider(provider) for provider in _PROVIDERS]


def get_all_providers() -> list[Provider]:
    """Return providers not blocked on vendor approval, in stable registry order."""

    return [
        _copy_provider(provider)
        for provider in _PROVIDERS
        if not provider["vendor_approval_pending"]
    ]


def get_provider(slug: str) -> Provider | None:
    """Return the provider matching ``slug``, or ``None`` when it is unknown."""

    provider = _PROVIDERS_BY_SLUG.get(slug)
    return _copy_provider(provider) if provider is not None else None


def get_visible_providers() -> list[Provider]:
    """Return providers whose launch gate passed and which are not vendor-blocked."""

    return [
        _copy_provider(provider)
        for provider in _PROVIDERS
        if provider["launch_gate_passed"] and not provider["vendor_approval_pending"]
    ]


def get_tier(n: int) -> list[Provider]:
    """Return all providers in tier ``n``.

    Raises:
        ValueError: If ``n`` is not one of the three supported tiers.
    """

    if isinstance(n, bool) or n not in (1, 2, 3):
        raise ValueError("tier must be 1, 2, or 3")
    return [_copy_provider(provider) for provider in _PROVIDERS if provider["tier"] == n]
