"""Approve an MCP server's own OAuth consent endpoint by verifying discovery.

The OAuth banner gate (:func:`kiro_crew.security.oauth_url_contains_credential`)
exempts the high-entropy ``state`` / PKCE values in a consent URL only at an
approved ``(host, path)``. The code-owned launch set and the operator's
hand-edited ``oauth_endpoints.json`` both name providers in advance, so on their
own they refuse a remote MCP server added straight to ``mcp.json`` whose
authorization server is on neither list, even when kiro-cli mints a correct
consent URL for it.

This module answers the question kiro-cli itself answered when it built that
URL: "is this the authorization endpoint the configured server points at?" It
walks the MCP authorization discovery chain from the server URL in config:

1. RFC 9728 protected-resource metadata at the server's well-known path (path
   insertion first, then the origin root), whose ``authorization_servers[0]``
   names the issuer. A server that publishes none falls back to its own origin
   as the issuer, as the MCP authorization spec defines.
2. RFC 8414 authorization-server metadata for that issuer, then OpenID Connect
   discovery, each required to name the SAME issuer.
3. The advertised ``authorization_endpoint`` must be HTTPS with no explicit port
   and its lowercase host and exact path must equal the consent URL's.

Only then is the endpoint handed to
:func:`kiro_crew.security.record_discovered_oauth_endpoint`, which grants the
same narrow exemption the builtin set grants. Every other rule of the gate
(fixed credentials, userinfo, fragments, path params, heavy percent encoding,
unknown parameters, HTTP, explicit ports) keeps applying.

Where the server URL comes from: the installed agent spec only (``<kiro home>/
agents``, sealed by the OS sandbox; project and bundled ``defaults.json`` are
deliberately NOT read), and kiro-cli's registry ``<kiro home>/settings/mcp.json``,
which is write-protected at the tool gate but not OS-sealed. A server name with
two different URLs is treated as unknown. That residual is bounded: a server the
agent could add there is one kiro-cli already connects to, and its metadata can
only approve the endpoint it advertises for itself.

Unlike ``connections.l0_probe``, this DOES dereference the issuer a server
advertises, exactly as kiro-cli does when it builds the consent URL. Every fetch
is therefore an unauthenticated GET of a static JSON document from the gateway,
with redirects off and a size bound (``l0_probe._get_json``). Each URL is first
vetted by ``link_unfurl.vet_unfurl_url`` (every resolved address must be public)
and must be HTTPS on port 443, a rule this caller adds on top of the vet, which
also admits port 80. The fetch then runs on that URL's own
``link_unfurl.pinned_connector`` session, so the socket dials exactly the
addresses the vet approved and there is no second lookup to rebind.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from kiro_crew.connections.l0_probe import (
    _authorization_metadata_url,
    _get_json,
    _https_url,
    _read_resource_metadata,
    _resource_metadata_urls,
    _validate_issuer,
    same_issuer,
)
from kiro_crew.link_unfurl import UnfurlRejected, VettedUrl, pinned_connector, vet_unfurl_url

logger = logging.getLogger(__name__)

#: Per-document fetch timeout.
_FETCH_TIMEOUT_SECONDS = 5.0
#: Ceiling for the whole verification, so a slow provider cannot hold a banner.
_VERIFY_TIMEOUT_SECONDS = 8.0
_WELL_KNOWN_OIDC = "/.well-known/openid-configuration"
_HTTPS_PORT = 443
#: How long a failed verification is remembered, so a slow or misconfigured
#: provider costs one timeout per window instead of one per banner.
_NEGATIVE_TTL_SECONDS = 10 * 60
_NEGATIVE_CAP = 64
# sha256 hex of (server name, consent host, consent path) -> monotonic expiry.
# Hashed so every retained key is 64 characters whatever the inputs' length:
# the cap then bounds every field, and identity is kept rather than truncated.
_NEGATIVE: dict[str, float] = {}


def _failure_key(server_name: str, host: str, path: str) -> str:
    payload = json.dumps([server_name, host, path], ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _vet_discovery_url(url: str) -> VettedUrl:
    """SSRF-vet *url* and require HTTPS on port 443. Blocking (``getaddrinfo``)."""
    vetted = vet_unfurl_url(url)
    if vetted.scheme != "https" or vetted.port != _HTTPS_PORT:
        raise UnfurlRejected("blocked_url")
    return vetted


async def _fetch_json(url: str) -> dict[str, Any]:
    """Fetch one metadata document over a session pinned to its vetted addresses."""
    vetted = await asyncio.to_thread(_vet_discovery_url, url)
    # One session per URL: a pinned connector serves exactly one host.
    async with aiohttp.ClientSession(connector=pinned_connector(vetted)) as session:
        return await _get_json(session, vetted.url, _FETCH_TIMEOUT_SECONDS)


_FETCH_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, UnfurlRejected)


def _kiro_settings_mcp_json() -> Path:
    # kiro-cli's own registry: follows KIRO_HOME, like the write-protection tier.
    from kiro_crew.config.paths import kiro_home

    return kiro_home() / "settings" / "mcp.json"


def _servers_from_settings_registry() -> dict[str, Any]:
    from kiro_crew.hooks import safe_read_file
    from kiro_crew.user_json import loads_user_jsonc

    path = _kiro_settings_mcp_json()
    if not path.is_file():
        return {}
    try:
        data = loads_user_jsonc(safe_read_file(str(path)))
    except Exception:
        logger.debug("Cannot read %s for OAuth discovery", path, exc_info=True)
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def _servers_from_agent_spec() -> dict[str, Any]:
    """``mcpServers`` from the INSTALLED agent spec only.

    Not ``mcp_discovery._load_agent_config``: that merges a project-dir
    ``defaults.json`` (``KIROCREW_PROJECT_DIR``, possibly an agent-writable
    checkout) with precedence over the installed spec.
    """
    from kiro_crew.agent import AGENT_FILENAME
    from kiro_crew.agent_discovery import _read_agent_spec
    from kiro_crew.config.paths import kiro_agents_dir

    try:
        spec = _read_agent_spec(
            kiro_agents_dir() / AGENT_FILENAME,
            operation="mcp_oauth_discovery",
            source="unknown",
        )
    except Exception:
        logger.debug("Cannot read the agent spec for OAuth discovery", exc_info=True)
        return {}
    servers = spec.get("mcpServers") if isinstance(spec, dict) else None
    return servers if isinstance(servers, dict) else {}


def configured_mcp_url(server_name: str) -> str | None:
    """The remote URL operator config gives *server_name*.

    ``None`` when neither source configures it as a remote server, or when the
    two disagree on its URL. Does blocking file I/O.
    """
    if not server_name:
        return None
    urls: set[str] = set()
    for servers in (_servers_from_agent_spec(), _servers_from_settings_registry()):
        spec = servers.get(server_name)
        if isinstance(spec, dict) and isinstance(spec.get("url"), str) and spec["url"]:
            urls.add(spec["url"].strip())
    if len(urls) != 1:
        if len(urls) > 1:
            logger.warning(
                "MCP server %r has conflicting URLs; skipping OAuth discovery", server_name
            )
        return None
    return urls.pop()


def _consent_endpoint(oauth_url: str) -> tuple[str, str] | None:
    """The ``(lowercase host, path)`` a consent URL opens, if gate-eligible."""
    try:
        parts = urlsplit(oauth_url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or port is not None or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return parts.hostname.lower(), parts.path


def _mcp_endpoint(mcp_url: str) -> str:
    """Validate the configured server URL and drop any query or fragment.

    Discovery uses the scheme, authority and path only, and the stripped form is
    what gets audited, so a query-string API key never reaches the SEL.
    """
    _https_url(mcp_url, "MCP server URL")
    parts = urlsplit(mcp_url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


async def _advertised_issuer(mcp_url: str) -> str:
    for candidate in _resource_metadata_urls(mcp_url):
        try:
            document = await _fetch_json(candidate)
            return _read_resource_metadata(document, mcp_url)
        except _FETCH_ERRORS:
            continue
    # No protected-resource metadata: the MCP authorization spec makes the
    # server's own origin the authorization server.
    parts = urlsplit(mcp_url)
    return _validate_issuer(urlunsplit((parts.scheme, parts.netloc, "", "", "")))


def _oidc_metadata_url(issuer: str) -> str:
    parts = urlsplit(issuer)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path.rstrip("/") + _WELL_KNOWN_OIDC, "", "")
    )


async def _advertised_authorization_endpoint(issuer: str) -> tuple[str, str] | None:
    for metadata_url in (_authorization_metadata_url(issuer), _oidc_metadata_url(issuer)):
        try:
            document = await _fetch_json(metadata_url)
            advertised_issuer = _validate_issuer(document.get("issuer"))
            if not same_issuer(advertised_issuer, issuer):
                continue
            endpoint = _https_url(document.get("authorization_endpoint"), "authorization_endpoint")
        except _FETCH_ERRORS:
            continue
        return _consent_endpoint(endpoint)
    return None


async def _discover(mcp_url: str) -> tuple[str, str] | None:
    issuer = await _advertised_issuer(mcp_url)
    return await _advertised_authorization_endpoint(issuer)


def _recently_failed(key: str) -> bool:
    expiry = _NEGATIVE.get(key)
    if expiry is None:
        return False
    if expiry <= time.monotonic():
        _NEGATIVE.pop(key, None)
        return False
    return True


def _remember_failure(key: str) -> None:
    _NEGATIVE.pop(key, None)
    while len(_NEGATIVE) >= _NEGATIVE_CAP:
        _NEGATIVE.pop(next(iter(_NEGATIVE)), None)
    _NEGATIVE[key] = time.monotonic() + _NEGATIVE_TTL_SECONDS


async def approve_discovered_oauth_endpoint(server_name: str, oauth_url: str) -> bool:
    """Approve *oauth_url*'s endpoint when discovery from *server_name* confirms it.

    Returns True when the endpoint is now approved for the banner gate. Runs
    only for a URL the gate rejects today AND would accept with its endpoint
    approved, so a URL refused for any other reason costs no network call and
    stays refused. A failed verification is remembered for
    ``_NEGATIVE_TTL_SECONDS``. Never raises: any failure leaves the gate as it was.
    """
    from kiro_crew.security import (
        oauth_rejection_is_endpoint_exemptible,
        record_discovered_oauth_endpoint,
    )

    try:
        consent = _consent_endpoint(oauth_url)
        if consent is None:
            return False
        if not await asyncio.to_thread(oauth_rejection_is_endpoint_exemptible, oauth_url):
            return False
        failure_key = _failure_key(server_name, *consent)
        if _recently_failed(failure_key):
            return False
        configured = await asyncio.to_thread(configured_mcp_url, server_name)
        if configured is None:
            return False
        mcp_url = _mcp_endpoint(configured)
        try:
            advertised = await asyncio.wait_for(_discover(mcp_url), _VERIFY_TIMEOUT_SECONDS)
        except Exception:
            _remember_failure(failure_key)
            raise
        if advertised != consent:
            _remember_failure(failure_key)
            logger.info(
                "OAuth consent endpoint for %r is not the one its server advertises",
                server_name,
            )
            return False
        return record_discovered_oauth_endpoint(*consent, mcp_url=mcp_url)
    except Exception:
        logger.info("OAuth endpoint discovery failed for %r", server_name, exc_info=True)
        return False
