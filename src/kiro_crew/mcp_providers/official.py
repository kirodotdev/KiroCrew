"""Official MCP Registry provider — registry.modelcontextprotocol.io.

The official registry is a public, no-auth REST API (v0.1):

- ``GET /v0.1/servers?search=<q>&version=latest&limit=<n>`` — search
- ``GET /v0.1/servers/{urlencoded-name}/versions/latest`` — detail

Entries follow the server.json schema: ``name`` (reverse-DNS, e.g.
``io.github.owner/repo``), ``description``, ``version``, ``status``
(active|deprecated|deleted), ``repository.url``, ``packages[]``,
``remotes[]``. The schema has shipped in both camelCase (2025-09-29)
and snake_case (earlier drafts) field spellings, and list items may be
wrapped as ``{"server": {...}, "_meta": {...}}`` — parsing is defensive
against all of these: malformed entries are skipped, deleted entries are
dropped, deprecated entries are badged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from typing import Any

import aiohttp

from kiro_crew.mcp_providers.base import (
    McpInstallPlan,
    McpSearchResult,
    McpServerDetail,
    ProviderUnavailableError,
)

logger = logging.getLogger(__name__)

# Official registry API base (no trailing slash).
_API_BASE = "https://registry.modelcontextprotocol.io"

# Total HTTP budget per request (seconds) — matches the skill_providers
# fan-out budget so one slow registry call never outlives the provider
# timeout in base.py.
_HTTP_TIMEOUT_SECS = 10.0

# User-Agent for our requests (good citizenship).
_USER_AGENT = "KiroCrew/1.0 (mcp-discovery)"

# Per-iteration read size for the EOF loop below. The cap on TOTAL size
# is _MAX_RESPONSE_BYTES; this only bounds one read call.
_READ_CHUNK_BYTES = 64 * 1024

# Maximum response body size (5 MiB) — a search page or single server doc
# is a few KB; anything bigger is malformed or hostile.
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024

# _meta key under which the registry reports official status metadata.
_META_OFFICIAL = "io.modelcontextprotocol.registry/official"

# Install-method priority when an entry offers several options.
_REGISTRY_TYPE_PRIORITY = ("npm", "pypi", "oci")

# registry_type → surfaced install method name.
_METHOD_BY_REGISTRY_TYPE = {"npm": "npx", "pypi": "uvx", "oci": "docker"}


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present key from *keys* — camelCase/snake_case shim."""
    for k in keys:
        if k in d:
            return d[k]
    return default


def _s(v: Any) -> str:
    """Coerce provider metadata to str — non-strings become '' (never crash)."""
    return v if isinstance(v, str) else ""


async def _fetch_json(url: str) -> Any | None:
    """GET *url* and parse JSON.

    Returns the parsed document on 200, ``None`` on 404 (entry gone), and
    raises :class:`ProviderUnavailableError` on any transport-level failure
    so callers can distinguish "not found" from "registry unreachable".
    """
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": _USER_AGENT}) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    raise ProviderUnavailableError(f"registry returned HTTP {resp.status}")
                # Read to EOF in bounded chunks; a single read(n) can return a partial body.
                body = bytearray()
                while True:
                    chunk = await resp.content.read(_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise ProviderUnavailableError("registry response too large")
                return json.loads(bytes(body).decode("utf-8"))
    except ProviderUnavailableError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise ProviderUnavailableError(str(exc)) from exc


def _unwrap_entry(item: Any) -> tuple[dict[str, Any], str]:
    """Normalize a list/detail item to ``(server_doc, status)``.

    Handles both the flat shape (the item IS the server doc) and the
    wrapped shape (``{"server": {...}, "_meta": {...}}``). Status is read
    from the server doc, the wrapper, or the official ``_meta`` block —
    whichever is present — and defaults to ``"active"``.
    """
    if not isinstance(item, dict):
        return {}, ""
    server = item.get("server")
    if not isinstance(server, dict):
        server = item
    meta = item.get("_meta")
    official = meta.get(_META_OFFICIAL) if isinstance(meta, dict) else None
    status = (
        _s(server.get("status"))
        or _s(item.get("status"))
        or (_s(official.get("status")) if isinstance(official, dict) else "")
    )
    return server, status or "active"


def _version_of(server: dict[str, Any]) -> str:
    """Extract the version — top-level or the legacy version_detail wrapper."""
    version = _s(server.get("version"))
    if version:
        return version
    detail = _first(server, "versionDetail", "version_detail")
    if isinstance(detail, dict):
        return _s(detail.get("version"))
    return ""


def _repo_url_of(server: dict[str, Any]) -> str:
    repo = server.get("repository")
    return _s(repo.get("url")) if isinstance(repo, dict) else ""


def _registry_type(pkg: dict[str, Any]) -> str:
    """Read the package registry type across schema spellings."""
    return _s(_first(pkg, "registryType", "registry_type", "registryName", "registry_name")).lower()


def _packages_of(server: dict[str, Any]) -> list[dict[str, Any]]:
    pkgs = server.get("packages")
    if not isinstance(pkgs, list):
        return []
    return [p for p in pkgs if isinstance(p, dict)]


def _remotes_of(server: dict[str, Any]) -> list[dict[str, Any]]:
    remotes = server.get("remotes")
    if not isinstance(remotes, list):
        return []
    return [r for r in remotes if isinstance(r, dict)]


def _methods_of(server: dict[str, Any]) -> list[str]:
    """Derive the install-method badges for an entry (priority order)."""
    types = {_registry_type(p) for p in _packages_of(server)}
    methods = [_METHOD_BY_REGISTRY_TYPE[t] for t in _REGISTRY_TYPE_PRIORITY if t in types]
    if any(_s(r.get("url")) for r in _remotes_of(server)):
        methods.append("url")
    return methods


def _short_name(full_name: str) -> str:
    """Last path segment of a reverse-DNS registry name (``.../repo`` → ``repo``)."""
    return full_name.rsplit("/", 1)[-1].strip()


def _concrete_bucket(bucket: Any) -> list[str]:
    """Collect arguments from one bucket that carry concrete values.

    Per the v1 translation contract: positional/named arguments with a
    concrete ``value`` are included; user-input placeholders (no value,
    only hints/variables) are skipped entirely — nothing fancy in v1.
    """
    out: list[str] = []
    if not isinstance(bucket, list):
        return out
    for arg in bucket:
        if not isinstance(arg, dict):
            continue
        kind = _s(arg.get("type"))
        value = _s(arg.get("value"))
        if kind == "positional" and value:
            out.append(value)
        elif kind == "named":
            name = _s(arg.get("name"))
            if name and value:
                out.extend([name, value])
    return out


def _split_args(pkg: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return ``(runtime_args, package_args)`` as separate buckets.

    Runtime arguments belong to the runtime command (npx/uvx/docker) and
    must precede the package target; package arguments are the server's
    own argv and must follow it. Merging them would feed runtime flags to
    the package, and the persisted spec would then hit the 409
    same-name/different-spec guard on reinstall.
    """
    runtime = _concrete_bucket(_first(pkg, "runtimeArguments", "runtime_arguments", default=[]))
    package = _concrete_bucket(_first(pkg, "packageArguments", "package_arguments", default=[]))
    return runtime, package


def _env_of(pkg: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Extract ``(env_placeholders, required_env)`` from a package entry."""
    env_vars = _first(pkg, "environmentVariables", "environment_variables", default=[])
    placeholders: dict[str, str] = {}
    required: list[str] = []
    if not isinstance(env_vars, list):
        return placeholders, required
    for var in env_vars:
        if not isinstance(var, dict):
            continue
        name = _s(var.get("name"))
        if not name:
            continue
        placeholders[name] = ""
        if var.get("isRequired") or var.get("is_required"):
            required.append(name)
    return placeholders, required


def _pick_remote(remotes: list[dict[str, Any]]) -> str:
    """Choose the remote URL — prefer streamable-http over sse."""

    def _remote_type(r: dict[str, Any]) -> str:
        return _s(_first(r, "type", "transportType", "transport_type")).lower()

    for preferred in ("streamable-http", "sse"):
        for r in remotes:
            if _remote_type(r) == preferred and _s(r.get("url")):
                return _s(r.get("url"))
    for r in remotes:
        if _s(r.get("url")):
            return _s(r.get("url"))
    return ""


def translate_install_plan(
    server: dict[str, Any],
) -> tuple[McpInstallPlan | None, list[str]]:
    """Translate a server.json doc into an mcp.json spec preview.

    Priority when multiple install options exist: npm > pypi > oci > remotes.
    Returns ``(plan, required_env)``; ``(None, [])`` when nothing installable.
    """
    packages = _packages_of(server)
    by_type: dict[str, dict[str, Any]] = {}
    for pkg in packages:
        rtype = _registry_type(pkg)
        identifier = _s(_first(pkg, "identifier", "name"))
        if rtype in _METHOD_BY_REGISTRY_TYPE and identifier and rtype not in by_type:
            by_type[rtype] = pkg

    for rtype in _REGISTRY_TYPE_PRIORITY:
        chosen = by_type.get(rtype)
        if chosen is None:
            continue
        identifier = _s(_first(chosen, "identifier", "name"))
        version = _s(chosen.get("version"))
        env, required = _env_of(chosen)
        runtime_args, package_args = _split_args(chosen)
        spec: dict[str, Any]
        if rtype == "npm":
            target = f"{identifier}@{version}" if version else identifier
            spec = {"command": "npx", "args": ["-y", *runtime_args, target, *package_args]}
        elif rtype == "pypi":
            target = f"{identifier}=={version}" if version else identifier
            spec = {"command": "uvx", "args": [*runtime_args, target, *package_args]}
        else:  # oci
            # SECURITY: registry runtimeArguments are deliberately NOT
            # translated for OCI. For npx/uvx they only configure a runtime
            # that runs the publisher's code anyway; for docker they are
            # host-level flags — a publisher-controlled `--privileged` or
            # `--volume /:/host` would dissolve the container boundary.
            # v1 pins the sandbox: `run -i --rm <image>` + the server's own
            # argv (package args) after the image, nothing else.
            spec = {
                "command": "docker",
                "args": ["run", "-i", "--rm", identifier, *package_args],
            }
        if env:
            spec["env"] = env
        method = _METHOD_BY_REGISTRY_TYPE[rtype]
        return McpInstallPlan(method=method, spec=spec), required

    url = _pick_remote(_remotes_of(server))
    if url:
        return McpInstallPlan(method="url", spec={"url": url}), []
    return None, []


class OfficialRegistryProvider:
    """Provider that searches the official MCP registry."""

    def __init__(self, api_base: str = _API_BASE) -> None:
        self._api_base = api_base

    @property
    def name(self) -> str:
        return "official"

    @property
    def display_name(self) -> str:
        return "MCP Registry"

    def is_available(self) -> bool:
        # Public API, no auth or configuration required.
        return True

    async def search(self, query: str, *, limit: int = 20) -> list[McpSearchResult]:
        """Search the registry catalog (single page — cursor pagination in v2)."""
        if not query.strip():
            return []
        params = urllib.parse.urlencode({"search": query, "version": "latest", "limit": str(limit)})
        data = await _fetch_json(f"{self._api_base}/v0.1/servers?{params}")
        if not isinstance(data, dict):
            return []
        items = data.get("servers")
        if not isinstance(items, list):
            return []

        results: list[McpSearchResult] = []
        for item in items:
            server, status = _unwrap_entry(item)
            full_name = _s(server.get("name")).strip()
            if not full_name:
                continue  # malformed entry — no usable identifier
            if status == "deleted":
                continue  # tombstoned upstream — never surface
            results.append(
                McpSearchResult(
                    id=full_name,
                    name=_short_name(full_name) or full_name,
                    title=_s(server.get("title")),
                    description=_s(server.get("description")),
                    provider=self.name,
                    version=_version_of(server),
                    repo_url=_repo_url_of(server),
                    methods=_methods_of(server),
                    deprecated=status == "deprecated",
                )
            )
            if len(results) >= limit:
                break
        return results

    async def fetch_detail(self, server_id: str) -> McpServerDetail | None:
        """Fetch the latest version of one server; None when it does not exist."""
        server_id = server_id.strip()
        if not server_id:
            return None
        # The reverse-DNS name contains "/" which is a real path hazard here:
        # the route is /v0.1/servers/{name}/... with the WHOLE name as one
        # encoded segment, so the slash must NOT survive (safe="").
        encoded = urllib.parse.quote(server_id, safe="")
        data = await _fetch_json(f"{self._api_base}/v0.1/servers/{encoded}/versions/latest")
        if data is None:
            return None
        server, status = _unwrap_entry(data)
        full_name = _s(server.get("name")).strip()
        if not full_name or status == "deleted":
            return None
        plan, required_env = translate_install_plan(server)
        return McpServerDetail(
            id=full_name,
            name=_short_name(full_name) or full_name,
            title=_s(server.get("title")),
            description=_s(server.get("description")),
            provider=self.name,
            version=_version_of(server),
            repo_url=_repo_url_of(server),
            deprecated=status == "deprecated",
            install_plan=plan,
            required_env=required_env,
        )
