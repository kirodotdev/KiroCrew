"""Account-free metadata conformance probe for official MCP providers."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TypedDict, cast
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from kiro_crew.connections.registry import Provider, get_all_registry_providers

DEFAULT_CONCURRENCY = 4
DEFAULT_TIMEOUT_SECONDS = 10.0
_MAX_METADATA_BYTES = 16 * 1024
_READ_CHUNK_BYTES = 4096
_REPORT_SCHEMA_VERSION = 1
_WELL_KNOWN_AUTHORIZATION = "/.well-known/oauth-authorization-server"
_WELL_KNOWN_RESOURCE = "/.well-known/oauth-protected-resource"
_RESOURCE_METADATA_RE = re.compile(
    r"\bBearer\b[^\r\n]*?\bresource_metadata\s*=\s*\"([^\"]+)\"", re.IGNORECASE
)


class ProbeResult(TypedDict):
    """Machine-readable result for one registry provider."""

    slug: str
    name: str
    ok: bool
    checks: dict[str, bool]
    errors: list[str]
    duration_ms: int


def _https_url(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    parts = urlsplit(value)
    try:
        parts.port
    except ValueError as error:
        raise ValueError(f"{field} must contain a valid port") from error
    if (
        parts.scheme != "https"
        or parts.hostname is None
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise ValueError(f"{field} must be an absolute HTTPS URL")
    return value


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    if parts.hostname is None:
        raise ValueError("URL must contain a hostname")
    return parts.scheme.lower(), parts.hostname.lower(), parts.port or 443


def _authorization_metadata_url(issuer: str) -> str:
    parts = urlsplit(issuer)
    issuer_path = parts.path.rstrip("/")
    path = _WELL_KNOWN_AUTHORIZATION + issuer_path
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


async def _get_json(
    session: aiohttp.ClientSession, url: str, timeout_seconds: float
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with session.get(
        url,
        headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        allow_redirects=False,
        auto_decompress=False,
        timeout=timeout,
    ) as response:
        if response.status != 200:
            raise ValueError(f"{url} returned HTTP {response.status}, expected 200")
        content_encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if content_encoding not in ("", "identity"):
            raise ValueError(
                f"{url} returned unsupported Content-Encoding {content_encoding!r}"
            )

        body = bytearray()
        while True:
            remaining = _MAX_METADATA_BYTES + 1 - len(body)
            chunk = await response.content.read(min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > _MAX_METADATA_BYTES:
                raise ValueError(
                    f"{url} exceeded the {_MAX_METADATA_BYTES}-byte metadata limit"
                )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError(f"{url} did not return JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{url} returned a non-object JSON document")
    return cast(dict[str, Any], payload)


async def _get_resource_metadata_url(
    session: aiohttp.ClientSession, mcp_url: str, timeout_seconds: float
) -> str:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with session.post(
        mcp_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "kirocrew-l0-probe", "version": "1"},
            },
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "Accept-Encoding": "identity",
            "MCP-Protocol-Version": "2025-06-18",
        },
        allow_redirects=False,
        auto_decompress=False,
        timeout=timeout,
    ) as response:
        if response.status != 401:
            raise ValueError(f"MCP endpoint returned HTTP {response.status}, expected 401")
        challenge = response.headers.get("WWW-Authenticate", "")
    match = _RESOURCE_METADATA_RE.search(challenge)
    if match is None:
        raise ValueError("401 response lacks a Bearer resource_metadata challenge")
    metadata_url = _https_url(match.group(1), "resource_metadata")
    if _origin(metadata_url) != _origin(mcp_url):
        raise ValueError("resource_metadata origin does not match the MCP endpoint")
    if not urlsplit(metadata_url).path.startswith(_WELL_KNOWN_RESOURCE):
        raise ValueError("resource_metadata is not an oauth-protected-resource URL")
    return metadata_url


def _validate_resource_metadata(
    document: dict[str, Any], mcp_url: str, expected_authorization_origin: str
) -> str:
    resource = _https_url(document.get("resource"), "protected resource")
    if _origin(resource) != _origin(mcp_url):
        raise ValueError("protected resource metadata describes a different origin")
    servers = document.get("authorization_servers")
    if not isinstance(servers, list) or not servers:
        raise ValueError("authorization_servers must be a non-empty list")
    authorization_server = _https_url(servers[0], "authorization server")
    if _origin(authorization_server) != _origin(expected_authorization_origin):
        raise ValueError("authorization server origin does not match registry expectation")
    return authorization_server


def _validate_authorization_metadata(
    document: dict[str, Any], expected_authorization_origin: str
) -> tuple[bool, bool]:
    issuer = _https_url(document.get("issuer"), "issuer")
    if _origin(issuer) != _origin(expected_authorization_origin):
        raise ValueError(
            "authorization metadata issuer origin does not match registry expectation"
        )
    _https_url(document.get("authorization_endpoint"), "authorization_endpoint")
    _https_url(document.get("token_endpoint"), "token_endpoint")

    registration = document.get("registration_endpoint")
    if registration is not None:
        _https_url(registration, "registration_endpoint")
    methods = document.get("code_challenge_methods_supported", [])
    if not isinstance(methods, list) or any(not isinstance(item, str) for item in methods):
        raise ValueError("code_challenge_methods_supported must be a list of strings")
    return registration is not None, "S256" in methods


async def probe_provider(
    session: aiohttp.ClientSession, provider: Provider, *, timeout_seconds: float
) -> ProbeResult:
    """Probe one provider without credentials and aggregate every conformance check."""

    started = time.monotonic()
    checks = {
        "unauthenticated_challenge": False,
        "protected_resource_metadata": False,
        "authorization_server_metadata": False,
        "dcr_expectation": False,
        "pkce_expectation": False,
    }
    errors: list[str] = []
    expected = provider["l0_expectations"]

    try:
        resource_url = await _get_resource_metadata_url(
            session, provider["mcp_url"], timeout_seconds
        )
        checks["unauthenticated_challenge"] = True
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
        errors.append(f"unauthenticated challenge: {error}")
        resource_url = None

    authorization_server: str | None = None
    if resource_url is not None:
        try:
            resource_metadata = await _get_json(session, resource_url, timeout_seconds)
            authorization_server = _validate_resource_metadata(
                resource_metadata,
                provider["mcp_url"],
                expected["authorization_server_origin"],
            )
            checks["protected_resource_metadata"] = True
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            errors.append(f"protected resource discovery: {error}")

    if authorization_server is not None:
        authorization_url = _authorization_metadata_url(authorization_server)
        try:
            authorization_metadata = await _get_json(session, authorization_url, timeout_seconds)
            dcr, pkce = _validate_authorization_metadata(
                authorization_metadata, expected["authorization_server_origin"]
            )
            checks["authorization_server_metadata"] = True
            checks["dcr_expectation"] = dcr == expected["dcr"]
            checks["pkce_expectation"] = pkce == expected["pkce"]
            if not checks["dcr_expectation"]:
                errors.append(f"DCR advertised={dcr}, expected={expected['dcr']}")
            if not checks["pkce_expectation"]:
                errors.append(f"PKCE S256 advertised={pkce}, expected={expected['pkce']}")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            errors.append(f"authorization server discovery: {error}")

    return {
        "slug": provider["slug"],
        "name": provider["name"],
        "ok": not errors and all(checks.values()),
        "checks": checks,
        "errors": errors,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


async def probe_all(
    session: aiohttp.ClientSession,
    providers: Sequence[Provider],
    *,
    concurrency: int,
    timeout_seconds: float,
) -> list[ProbeResult]:
    """Probe providers with a hard cap on simultaneous provider request chains."""

    semaphore = asyncio.Semaphore(concurrency)

    async def limited(provider: Provider) -> ProbeResult:
        async with semaphore:
            try:
                return await asyncio.wait_for(
                    probe_provider(session, provider, timeout_seconds=timeout_seconds),
                    timeout=timeout_seconds * 3 + 1,
                )
            except asyncio.TimeoutError:
                return {
                    "slug": provider["slug"],
                    "name": provider["name"],
                    "ok": False,
                    "checks": {},
                    "errors": ["provider probe exceeded its total timeout"],
                    "duration_ms": round((timeout_seconds * 3 + 1) * 1000),
                }

    return list(await asyncio.gather(*(limited(provider) for provider in providers)))


def build_report(results: Sequence[ProbeResult]) -> dict[str, Any]:
    failed = sum(not result["ok"] for result in results)
    return {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": failed == 0,
        "provider_count": len(results),
        "failed_count": failed,
        "providers": list(results),
    }


async def run_probe(*, concurrency: int, timeout_seconds: float) -> dict[str, Any]:
    providers = get_all_registry_providers()
    async with aiohttp.ClientSession(auto_decompress=False) as session:
        results = await probe_all(
            session,
            providers,
            concurrency=concurrency,
            timeout_seconds=timeout_seconds,
        )
    return build_report(results)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("connections-l0-report.json"))
    parser.add_argument("--concurrency", type=_positive_int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--timeout", type=_positive_float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    try:
        report = asyncio.run(run_probe(concurrency=args.concurrency, timeout_seconds=args.timeout))
    except Exception as error:
        report = {
            "schema_version": _REPORT_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ok": False,
            "provider_count": 0,
            "failed_count": 0,
            "providers": [],
            "fatal_error": f"{type(error).__name__}: {error}",
        }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
