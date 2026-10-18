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


#: The codes a :class:`SkillProviderError` may carry. A code outside this set is
#: reported as ``unavailable``, so a caller can branch on the code without
#: trusting a provider to spell it.
PROVIDER_ERROR_CODES = frozenset({"rate_limited", "not_found", "refused", "unavailable"})


class SkillProviderError(Exception):
    """A provider could not answer, as opposed to answering "nothing".

    ``search`` returning ``[]`` means the provider looked and nothing matched;
    ``fetch_skill_bundle``/``fetch_skill_content`` returning ``None`` means there
    is nothing to fetch. Raising this instead says the provider could NOT look,
    or refused on purpose, and why -- so the dashboard can show the reason in
    place of an empty result.

    ``code`` is one of :data:`PROVIDER_ERROR_CODES`:

    - ``rate_limited`` -- the remote host's request budget is used up; the user
      can wait (``message`` should say until when, if known);
    - ``not_found`` -- the address names nothing: a missing or private
      repository, a ref that does not exist;
    - ``refused`` -- the content exists but the provider will not import it
      (a size or file-count ceiling, a name collision, a partial listing);
    - ``unavailable`` -- the host could not be reached or answered with
      something unusable.

    ``message`` is one short sentence for the person who can act on it. It is
    still provider-authored text: the discover handler scrubs and caps it before
    it reaches the dashboard, so a provider must not rely on it being shown
    verbatim -- and must never put a token, a credential-bearing URL or a raw
    response body in it.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code if code in PROVIDER_ERROR_CODES else "unavailable"
        self.message = message


@dataclass(frozen=True)
class SkillProviderOutcome:
    """Outcome of one provider leg in a skill catalog search."""

    name: str
    status: str
    """One of ``ok``, ``timeout``, or ``error``."""

    code: str = ""
    """For an ``error`` the provider explained: a :data:`PROVIDER_ERROR_CODES` member."""

    message: str = ""
    """For an ``error`` the provider explained: its reason, unscrubbed."""


@dataclass(frozen=True)
class SkillSearchResponse:
    """Merged search results plus one outcome for every attempted provider."""

    results: list[SkillSearchResult]
    provider_outcomes: list[SkillProviderOutcome]


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
        provider's semantics. ``[]`` means "looked, nothing matched"; a
        provider that could not look raises :class:`SkillProviderError`.
        """
        ...

    async def fetch_skill_content(self, skill_id: str) -> str | None:
        """Fetch the raw SKILL.md content for a given skill.

        Returns the full markdown content ready to write to disk, or
        None if there is no such skill. A provider that can say why it got
        nothing raises :class:`SkillProviderError` instead.
        """
        ...

    def is_available(self) -> bool:
        """Return True if this provider is configured and ready to use.

        Providers that require configuration (API keys, endpoints) should
        return False when not configured, so the registry can skip them.
        """
        ...

    # ---- optional methods ------------------------------------------------
    #
    # Neither is part of the structural check: the discover handler probes for
    # them with ``getattr``/``hasattr``, so a provider that omits both is a
    # complete provider. They are documented here because a provider author
    # cannot otherwise discover that they exist.
    #
    # ``fetch_skill_bundle(skill_id) -> list[tuple[str, str]] | None``
    #     Every file of the skill as ``(relative_path, content)``, not just the
    #     instruction file. Preferred by both preview and install when present;
    #     ``fetch_skill_content`` is the single-file fallback. May raise
    #     ``SkillProviderError`` exactly as ``fetch_skill_content`` may.
    #
    # ``install_slug(skill_id) -> str``
    #     The provider's own local key for a skill, used when the default
    #     ``_slugify(skill_id)`` is not injective over that provider's ids --
    #     slugify lowercases and folds ``/``, ``@`` and ``:`` onto ``-``, so two
    #     distinct case-sensitive addresses can otherwise share one key and one
    #     install overwrites the other skill. The returned value is still
    #     slugified and still validated as a single safe path segment, so this
    #     names a key, it does not widen what a key may be.


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
        """Search while preserving the existing results-only return contract."""
        return (await self.search_with_outcomes(query, provider=provider, limit=limit)).results

    async def search_with_outcomes(
        self,
        query: str,
        *,
        provider: str | None = None,
        limit: int = 20,
    ) -> SkillSearchResponse:
        """Search providers and report whether every attempted leg answered.

        Results stay merged in provider order. A timeout or error contributes no
        rows but remains visible in ``provider_outcomes`` so callers can distinguish
        an incomplete search from a complete search with zero matches.
        """

        # The timeout budget, the provenance re-stamp and the two failure
        # handlers are one contract, so the single-provider request and every
        # fan-out leg go through this one body: a divergence here would let a
        # provider's own ``provider`` string survive on one path but not the
        # other. *name* is the vetted registration key, never a re-read of the
        # provider-controlled ``name`` property.
        async def _search_one(name: str, p: SkillProvider) -> SkillSearchResponse:
            try:
                results = _stamp_provenance(
                    await asyncio.wait_for(
                        p.search(query, limit=limit), timeout=_SEARCH_TIMEOUT_SECS
                    ),
                    name,
                )
                return SkillSearchResponse(
                    results=results,
                    provider_outcomes=[SkillProviderOutcome(name=name, status="ok")],
                )
            except SkillProviderError as exc:
                # The provider could not look and said why. Logged at info: the
                # reason now reaches the user, so this is not an unexplained fault.
                logger.info("Provider %s could not search (%s)", name, exc.code)
                return SkillSearchResponse(
                    results=[],
                    provider_outcomes=[
                        SkillProviderOutcome(
                            name=name,
                            status="error",
                            code=exc.code,
                            message=exc.message if isinstance(exc.message, str) else "",
                        )
                    ],
                )
            except asyncio.TimeoutError:
                logger.warning("Provider %s timed out for query %r", name, query)
                return SkillSearchResponse(
                    results=[],
                    provider_outcomes=[SkillProviderOutcome(name=name, status="timeout")],
                )
            except Exception:
                logger.warning("Provider %s failed for query %r", name, query, exc_info=True)
                return SkillSearchResponse(
                    results=[],
                    provider_outcomes=[SkillProviderOutcome(name=name, status="error")],
                )

        if provider:
            p = self._providers.get(provider)
            if p is None or not provider_available(p):
                return SkillSearchResponse(results=[], provider_outcomes=[])
            # This path applies no ``[:limit]``: the provider was ASKED for at
            # most *limit* rows and is trusted to honour it. The fan-out below
            # re-caps because it merges several providers, not because any one
            # of them is checked — a provider that ignores *limit* is capped
            # nowhere on this path.
            return await _search_one(provider, p)

        # Iterate (key, provider) pairs so failure logs name the vetted
        # registration key.
        available = [(n, p) for n, p in self._providers.items() if provider_available(p)]
        if not available:
            return SkillSearchResponse(results=[], provider_outcomes=[])

        responses = await asyncio.gather(*[_search_one(n, p) for n, p in available])
        merged: list[SkillSearchResult] = []
        outcomes: list[SkillProviderOutcome] = []
        for response in responses:
            merged.extend(response.results)
            outcomes.extend(response.provider_outcomes)
        return SkillSearchResponse(
            results=merged[:limit],  # total cap matches what the caller asked for
            provider_outcomes=outcomes,
        )
