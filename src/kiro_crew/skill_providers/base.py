"""Base types for skill providers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillSearchResult:
    """A single skill result from a provider search."""

    id: str
    """Provider-specific identifier (repo name, slug, etc.)."""

    name: str
    """Human-readable skill name."""

    description: str
    """Short description of what the skill does."""

    provider: str
    """Which provider returned this result (e.g. 'skillsh', 'promptfarm')."""

    repo_url: str = ""
    """Git repository URL for the skill source, if available."""

    author: str = ""
    """Author or publisher of the skill."""

    installed: bool = False
    """Whether this skill is already installed locally."""

    tags: list[str] = field(default_factory=list)
    """Tags/keywords for the skill."""

    installs: int = 0
    """Install/download count reported by the provider (0 = unknown)."""


@runtime_checkable
class SkillProvider(Protocol):
    """Protocol for skill discovery providers.

    Each provider can search its catalog and fetch skill content for
    installation. Providers are async to allow concurrent fan-out.
    """

    @property
    def name(self) -> str:
        """Short provider identifier (e.g. 'skillsh', 'promptfarm')."""
        ...

    @property
    def display_name(self) -> str:
        """Human-readable provider name for UI badges."""
        ...

    async def search(self, query: str, *, limit: int = 20) -> list[SkillSearchResult]:
        """Search the provider's catalog.

        Returns up to *limit* results matching *query*. Empty query may
        return featured/popular skills or an empty list depending on the
        provider's semantics.
        """
        ...

    async def fetch_skill_content(self, skill_id: str) -> str | None:
        """Fetch the raw SKILL.md content for a given skill.

        Returns the full markdown content ready to write to disk, or
        None if the skill cannot be fetched.
        """
        ...

    def is_available(self) -> bool:
        """Return True if this provider is configured and ready to use.

        Providers that require configuration (API keys, endpoints) should
        return False when not configured, so the registry can skip them.
        """
        ...


class ProviderRegistry:
    """Registry of skill providers for fan-out search.

    Collects enabled providers and searches them concurrently.
    """

    def __init__(self) -> None:
        self._providers: dict[str, SkillProvider] = {}

    def register(self, provider: SkillProvider) -> None:
        """Add a provider to the registry."""
        self._providers[provider.name] = provider

    def get(self, name: str) -> SkillProvider | None:
        """Get a provider by name."""
        return self._providers.get(name)

    @property
    def available_providers(self) -> list[SkillProvider]:
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
    ) -> list[SkillSearchResult]:
        """Fan-out search across all available providers (or a specific one).

        Results are merged and returned in provider order. Each provider's
        failures are caught and logged — a single provider timeout does not
        break the entire search.
        """
        if provider:
            p = self._providers.get(provider)
            if p is None or not p.is_available():
                return []
            try:
                return await asyncio.wait_for(p.search(query, limit=limit), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("Provider %s timed out for query %r", provider, query)
                return []
            except Exception:
                logger.warning("Provider %s failed for query %r", provider, query, exc_info=True)
                return []

        providers = self.available_providers
        if not providers:
            return []

        async def _search_one(p: SkillProvider) -> list[SkillSearchResult]:
            try:
                return await asyncio.wait_for(p.search(query, limit=limit), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("Provider %s timed out for query %r", p.name, query)
                return []
            except Exception:
                logger.warning("Provider %s failed for query %r", p.name, query, exc_info=True)
                return []

        results_per_provider = await asyncio.gather(*[_search_one(p) for p in providers])
        merged: list[SkillSearchResult] = []
        for results in results_per_provider:
            merged.extend(results)
        return merged[:limit]  # total cap matches what the caller asked for
