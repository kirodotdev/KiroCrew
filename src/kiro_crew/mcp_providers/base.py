"""Base types for MCP discovery providers.

Closely mirrors ``kiro_crew.skill_providers.base`` — the same registry /
protocol / concurrent fan-out shape, typed for MCP server results so the
two discovery systems stay structurally interchangeable.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Per-provider budget for one fan-out search. A slow provider is dropped (with
# a warning) rather than stalling the aggregate search. Module-level so tests
# can patch it instead of waiting out the production budget.
_SEARCH_TIMEOUT_SECS = 10.0


class ProviderUnavailableError(Exception):
    """The provider's upstream (registry API, CLI) could not be reached.

    Raised by providers on transport-level failures so handlers can map the
    condition to HTTP 503 ("provider unavailable") — distinct from a normal
    "not found" (``None``) result.
    """


@dataclass(frozen=True)
class McpSearchResult:
    """A single MCP server result from a provider search."""

    id: str
    """Provider-specific identifier (official: reverse-DNS name; capability: backend-defined id)."""

    name: str
    """Short display name (last path segment for official registry names)."""

    description: str
    """Short description of what the server does."""

    provider: str
    """Which provider returned this result (e.g. 'official', 'capability')."""

    title: str = ""
    """Optional prettier human title ("" if none)."""

    version: str = ""
    """Latest published version ("" if unknown)."""

    repo_url: str = ""
    """Source repository URL, if available."""

    installed: bool = False
    """Whether the provider itself reports this server as installed."""

    methods: list[str] = field(default_factory=list)
    """Install methods derivable from the entry (npx/uvx/docker/url or capability)."""

    deprecated: bool = False
    """Whether the entry is marked deprecated upstream."""


@dataclass(frozen=True)
class McpInstallPlan:
    """What an install would write into mcp.json (preview + install source)."""

    method: str
    """One of 'npx', 'uvx', 'docker', 'url'."""

    spec: dict[str, Any]
    """The mcp.json server entry (command/args/env or url)."""


@dataclass(frozen=True)
class McpServerDetail:
    """Full detail for a single MCP server (detail panel + install source)."""

    id: str
    name: str
    description: str
    provider: str
    title: str = ""
    version: str = ""
    repo_url: str = ""
    deprecated: bool = False
    install_plan: McpInstallPlan | None = None
    """None when the entry has no derivable install method (or provider=capability)."""
    required_env: list[str] = field(default_factory=list)
    """Env vars the user must fill after install ([] if none)."""


@runtime_checkable
class McpProvider(Protocol):
    """Protocol for MCP server discovery providers.

    Each provider can search its catalog and fetch a full server detail
    for preview/installation. Providers are async to allow concurrent
    fan-out.
    """

    @property
    def name(self) -> str:
        """Short provider identifier (e.g. 'official', 'capability')."""
        ...

    @property
    def display_name(self) -> str:
        """Human-readable provider name for UI badges."""
        ...

    async def search(self, query: str, *, limit: int = 20) -> list[McpSearchResult]:
        """Search the provider's catalog for up to *limit* matches."""
        ...

    async def fetch_detail(self, server_id: str) -> McpServerDetail | None:
        """Fetch full detail for a server, or None when it does not exist."""
        ...

    def is_available(self) -> bool:
        """Return True if this provider is configured and ready to use."""
        ...


@dataclass(frozen=True)
class McpProviderOutcome:
    """Outcome of one provider leg in an MCP catalog search."""

    name: str
    status: str
    """One of ``ok``, ``timeout``, or ``error``."""


@dataclass(frozen=True)
class McpSearchResponse:
    """Merged search results plus one outcome for every attempted provider."""

    results: list[McpSearchResult]
    provider_outcomes: list[McpProviderOutcome]


class ProviderRegistry:
    """Registry of MCP providers for fan-out search.

    Collects enabled providers and searches them concurrently.
    """

    def __init__(self) -> None:
        self._providers: dict[str, McpProvider] = {}

    def register(self, provider: McpProvider) -> None:
        """Add a provider to the registry."""
        self._providers[provider.name] = provider

    def get(self, name: str) -> McpProvider | None:
        """Get a provider by name."""
        return self._providers.get(name)

    @property
    def available_providers(self) -> list[McpProvider]:
        """Return all providers that report as available."""
        return [p for p in self._providers.values() if p.is_available()]

    @property
    def provider_names(self) -> list[str]:
        """Return names of all registered (not necessarily available) providers."""
        return list(self._providers.keys())

    async def search(
        self,
        query: str,
        *,
        provider: str | None = None,
        limit: int = 20,
    ) -> list[McpSearchResult]:
        """Search while preserving the legacy results-only return contract."""
        return (await self.search_with_outcomes(query, provider=provider, limit=limit)).results

    async def search_with_outcomes(
        self,
        query: str,
        *,
        provider: str | None = None,
        limit: int = 20,
    ) -> McpSearchResponse:
        """Search providers and report whether every attempted leg answered.

        Results stay merged in provider order. A timeout or error contributes no
        rows but remains visible in ``provider_outcomes`` so callers can distinguish
        an incomplete search from a complete search with zero matches.
        """

        async def _search_one(name: str, p: McpProvider) -> McpSearchResponse:
            try:
                results = await asyncio.wait_for(
                    p.search(query, limit=limit), timeout=_SEARCH_TIMEOUT_SECS
                )
                return McpSearchResponse(
                    results=results,
                    provider_outcomes=[McpProviderOutcome(name=name, status="ok")],
                )
            except asyncio.TimeoutError:
                logger.warning("MCP provider %s timed out for query %r", name, query)
                return McpSearchResponse(
                    results=[],
                    provider_outcomes=[McpProviderOutcome(name=name, status="timeout")],
                )
            except Exception:
                logger.warning("MCP provider %s failed for query %r", name, query, exc_info=True)
                return McpSearchResponse(
                    results=[],
                    provider_outcomes=[McpProviderOutcome(name=name, status="error")],
                )

        if provider:
            p = self._providers.get(provider)
            if p is None or not p.is_available():
                return McpSearchResponse(results=[], provider_outcomes=[])
            return await _search_one(provider, p)

        providers = self.available_providers
        if not providers:
            return McpSearchResponse(results=[], provider_outcomes=[])

        responses = await asyncio.gather(*[_search_one(p.name, p) for p in providers])
        merged: list[McpSearchResult] = []
        outcomes: list[McpProviderOutcome] = []
        for response in responses:
            merged.extend(response.results)
            outcomes.extend(response.provider_outcomes)
        return McpSearchResponse(results=merged[:limit], provider_outcomes=outcomes)
