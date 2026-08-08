"""Load and query the curated official MCP provider registry."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date
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


class _RequiredProviderFields(TypedDict):
    """Fields every registry entry must declare."""

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
    # When ``revoke_page_url`` was last checked against the provider, and how.
    # A revoke link is a safety promise -- a stale one sends a user who wants
    # their grant gone to a 404 or to a page the grant does not appear on -- so
    # the check is dated in-tree and a test fails the build once it ages out.
    # The note records the strength of that check, because a logged-out HTTP
    # audit cannot see which surface actually lists the grant.
    revoke_verified_on: str
    revoke_verified_note: str


class Provider(_RequiredProviderFields, total=False):
    """One official MCP provider exposed to the Connections experience.

    ``client_id`` is present only for providers that require a pre-registered
    OAuth client instead of Dynamic Client Registration (``l0_expectations.dcr``
    false).  It is a PUBLIC identifier forwarded to the runtime as the remote
    entry's ``clientId``; the corresponding secret is never stored here.
    GitHub is the one such provider today and its value is deliberately UNSET
    pending the Kiro app registration, which is why the field is optional
    rather than required — an entry without it simply carries no clientId.

    ``revoke_manual_path`` is the in-app navigation to the same page as
    ``revoke_page_url``, for a provider whose settings link is a single-page app
    that re-routes after sign-in and can land the user somewhere else. A URL
    cannot be made reliable against that from our side, so the card states the
    path too and the user always has a way through.
    """

    client_id: str
    revoke_manual_path: str


class RegistryValidationError(ValueError):
    """Raised when the committed provider registry is malformed."""


_REGISTRY_PATH = Path(__file__).with_name("registry.json")
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# How long a revoke-link verification stays trustworthy. Providers move these
# settings pages between redesigns, so a visible card carries a re-check
# deadline rather than a one-time claim.
REVOKE_VERIFICATION_MAX_AGE_DAYS = 180
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
    "revoke_verified_on",
    "revoke_verified_note",
}
_SMOKE_FIXTURE_FIELDS = {"tool", "args"}
_L0_EXPECTATION_FIELDS = {"authorization_server_origin", "dcr", "pkce"}
# Optional because only non-DCR providers need a pre-registered OAuth client.
# See Provider.client_id: GitHub is the sole intended consumer and stays unset
# until the Kiro app is registered, so absence must remain a valid entry shape.
_OPTIONAL_PROVIDER_FIELDS = {"client_id", "revoke_manual_path"}


def _validation_error(index: int, message: str) -> RegistryValidationError:
    return RegistryValidationError(f"provider at index {index}: {message}")


def _validate_provider(raw: object, index: int) -> Provider:
    if not isinstance(raw, dict):
        raise _validation_error(index, "must be an object")

    fields = set(raw)
    missing = _PROVIDER_FIELDS - fields
    extra = fields - _PROVIDER_FIELDS - _OPTIONAL_PROVIDER_FIELDS
    if missing:
        raise _validation_error(index, f"missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise _validation_error(index, f"unknown fields: {', '.join(sorted(extra))}")

    for optional in ("client_id", "revoke_manual_path"):
        if optional in raw:
            value = raw[optional]
            if not isinstance(value, str) or not value.strip():
                raise _validation_error(
                    index, f"{optional} must be a non-empty string when present"
                )

    for field in ("name", "slug", "mcp_url", "revoke_page_url", "docs_url", "gotcha_copy"):
        value = raw[field]
        if not isinstance(value, str) or not value.strip():
            raise _validation_error(index, f"{field} must be a non-empty string")

    if not isinstance(raw["revoke_verified_note"], str) or not raw["revoke_verified_note"].strip():
        raise _validation_error(index, "revoke_verified_note must be a non-empty string")

    verified_on = raw["revoke_verified_on"]
    if not isinstance(verified_on, str) or not _ISO_DATE_PATTERN.fullmatch(verified_on):
        raise _validation_error(index, "revoke_verified_on must be a YYYY-MM-DD date")
    try:
        # Shape alone would accept 2026-02-31; the staleness test compares this
        # to a real date, so reject anything that is not one.
        date.fromisoformat(verified_on)
    except ValueError as error:
        raise _validation_error(index, "revoke_verified_on must be a YYYY-MM-DD date") from error

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
