"""Base types for skill providers."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Per-provider budget for one fan-out search. A slow provider is dropped (with
# a warning) rather than stalling the aggregate search. Module-level so tests
# can patch it instead of waiting out the production budget.
_SEARCH_TIMEOUT_SECS = 10.0


def provider_available(provider: SkillProvider) -> bool:
    """Whether *provider* reports itself available, treating failure as no.

    ``is_available`` is provider-supplied code; a missing method or an
    exception must read as "not available" so one broken provider cannot break
    the callers that iterate every registered provider — or 500 a per-provider
    endpoint (install/preview) that probes it directly.
    """
    try:
        return bool(provider.is_available())
    except Exception:
        logger.warning("Provider availability probe failed; treating as unavailable")
        return False


def _stamp_provenance(results: object, name: str) -> "list[SkillSearchResult]":
    """Re-stamp each result row's ``provider`` with the registration key.

    The ``provider`` field on a result is provider-authored data, so a
    contributed catalog could claim another registered identity ("skillsh")
    and have the UI route its preview/install to a different provider. The
    registry knows which provider actually produced each batch — that key is
    the only trustworthy provenance, so it overwrites whatever the row claims.
    Non-result objects from a broken provider are dropped rather than crashing
    the merge.
    """
    stamped: list[SkillSearchResult] = []
    for r in results if isinstance(results, list) else []:
        if not isinstance(r, SkillSearchResult):
            continue
        stamped.append(r if r.provider == name else dataclasses.replace(r, provider=name))
    return stamped


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

    def register(self, provider: SkillProvider, *, name: str | None = None) -> None:
        """Add a provider to the registry.

        ``name`` pins the registration identity to a value the caller already
        vetted. ``provider.name`` is a property, so re-reading it here could
        return something other than what a policy gate checked — a captured
        name makes the vetted string the key unconditionally.
        """
        self._providers[name if name is not None else provider.name] = provider

    def get(self, name: str) -> SkillProvider | None:
        """Get a provider by name."""
        return self._providers.get(name)

    @property
    def available_providers(self) -> list[SkillProvider]:
        """Return all providers that report as available.

        A provider whose ``is_available`` is missing or raises is treated as
        unavailable rather than propagating — this property feeds the aggregate
        search and the discover response, where one broken provider must not
        take every other catalog down with it.
        """
        return [p for p in self._providers.values() if provider_available(p)]

    @property
    def available_provider_names(self) -> list[str]:
        """Names of available providers, read from the registration keys.

        Keys are the identities vetted at registration; reading ``p.name``
        properties here instead would re-consult provider-controlled code on
        every request.
        """
        return [name for name, p in self._providers.items() if provider_available(p)]

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
            if p is None or not provider_available(p):
                return []
            try:
                return _stamp_provenance(
                    await asyncio.wait_for(
                        p.search(query, limit=limit), timeout=_SEARCH_TIMEOUT_SECS
                    ),
                    provider,
                )
            except asyncio.TimeoutError:
                logger.warning("Provider %s timed out for query %r", provider, query)
                return []
            except Exception:
                logger.warning("Provider %s failed for query %r", provider, query, exc_info=True)
                return []

        # Iterate (key, provider) pairs so failure logs name the vetted
        # registration key instead of re-reading a provider-controlled
        # ``name`` property inside an exception handler.
        available = [(n, p) for n, p in self._providers.items() if provider_available(p)]
        if not available:
            return []

        async def _search_one(name: str, p: SkillProvider) -> list[SkillSearchResult]:
            try:
                return _stamp_provenance(
                    await asyncio.wait_for(
                        p.search(query, limit=limit), timeout=_SEARCH_TIMEOUT_SECS
                    ),
                    name,
                )
            except asyncio.TimeoutError:
                logger.warning("Provider %s timed out for query %r", name, query)
                return []
            except Exception:
                logger.warning("Provider %s failed for query %r", name, query, exc_info=True)
                return []

        results_per_provider = await asyncio.gather(*[_search_one(n, p) for n, p in available])
        merged: list[SkillSearchResult] = []
        for results in results_per_provider:
            merged.extend(results)
        return merged[:limit]  # total cap matches what the caller asked for
