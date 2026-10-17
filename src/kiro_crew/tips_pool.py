"""The tip-pool payload carried across the tips platform seam.

Kept in its own import-light module (no aiohttp, no dashboard state) so an
edition's composition root can build a :class:`TipsPool` without importing the
tips runtime, and so ``kiro_crew.platform.interfaces`` can name the type under
``TYPE_CHECKING`` without pulling the runtime into the platform package.

``kiro_crew.tips`` re-exports both names, so ``tips.CatalogEntry`` keeps
resolving for existing callers and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

# Provenance stamp of the public/open-source pool: the bundled
# ``data/tips_curated.json`` plus the ``docs/*.md`` scan catalog.  Persisted in
# ``tips_state.json`` so a host that later composes an edition pool can tell its
# cached generated tips came from a DIFFERENT pool and drop them (see
# ``tips._reconcile_pool``).  An edition pool may not reuse this id.
PUBLIC_POOL_ID = "public"

# Stamp of the empty pool served when an edition build's adapter could not
# answer.  The degrade for a non-standalone build has to be "no tips", not "the
# public tips": falling back to the public pool there would serve exactly the
# tips this seam exists to withhold, so a broken adapter would reintroduce the
# leak.  A distinct id (not PUBLIC_POOL_ID) keeps the provenance stamp honest, so
# tips generated under a real pool are not re-served under this one.
WITHHELD_POOL_ID = "edition-unavailable"


@dataclass
class CatalogEntry:
    """A feature the tips engine may talk about.

    The public build derives these from ``kiro_crew/docs/*.md`` (H1 + first
    paragraph, allowlisted); an edition supplies its own through
    :class:`TipsPool`.  Mutable (not frozen) because the doc scan and the
    bundled-catalog loader both build entries incrementally.
    """

    feature: str  # H1 title (without #)
    summary: str  # first paragraph
    doc: str  # filename (e.g. 'cron-and-scheduling.md')
    mtime: float = 0.0  # file mtime for recency ordering


@dataclass(frozen=True)
class TipsPool:
    """A COMPLETE tip pool that REPLACES the public one — never merges with it.

    This is the one thing that makes the tips seam different from every ADD-only
    edition seam in the contract (``McpToolingProvider.extra_mcp_servers``,
    ``SkillDiscoveryProvider.skill_providers``, …).  Those UNION an edition's
    contribution into the public set, which is the wrong shape here: a tip is a
    claim that a feature EXISTS and is worth using, so a public tip surfacing on
    an edition build advertises a capability that build may not have, or may
    deliberately not expose.  Suppression-by-subtraction cannot fix that either —
    it needs the public list enumerated to subtract from, so every new public tip
    would leak until someone remembered to deny it.  So the edition hands over a
    whole pool and the public one is not consulted at all.

    A pool with empty ``curated`` AND empty ``catalog`` is legal and means "this
    build shows no tips": the engine has nothing eligible and serves none, rather
    than falling back to the public pool.

    ``pool_id`` is the pool's PROVENANCE, not a display name.  It is persisted
    into ``tips_state.json`` and compared on every load; when it changes, tips
    that were generated against the previous pool are discarded instead of being
    re-served (the switch-over leak).  Give a stable, edition-specific value —
    the same string across restarts, a different string from any other pool.
    """

    pool_id: str
    # Hand-authored, action-first tips.  Same dict shape as the public
    # ``data/tips_curated.json`` entries; each one is re-validated by the tips
    # runtime through the SAME field checks as a generated tip, so a malformed
    # entry is dropped rather than served.
    curated: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    # Feature catalog handed to the tip generator and used by the last-resort
    # non-personalized fallback.  Replaces the public docs scan entirely, so no
    # public feature is put in front of the generator, and a generated tip citing
    # a doc outside this catalog is dropped at parse time.  It does not police the
    # model's PROSE: a tip could cite a doc here while naming something absent.
    catalog: Tuple[CatalogEntry, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Validate the pool's shape at CONSTRUCTION, in the composition root.

        Checked here rather than at the consumption site because this is where
        the author can still fix it: an edition typically builds its pool from a
        packaged JSON file, so ``curated`` can arrive as ``None`` from a
        ``"curated": null`` key, and a bad ``pool_id`` degrades silently (stale
        tips survive a pool switch). Both would otherwise surface far away — the
        first as a ``TypeError`` during cache init that turns every
        ``/api/tips/*`` request into a 500.

        A ``list`` is accepted and frozen into a tuple, because that is what a
        JSON loader hands back and rejecting it would make the common correct
        case a trap. Anything else is refused rather than coerced: silently
        reading ``None`` as "no tips" would ship an empty pool that looks
        deliberate, and "this build shows no tips" must be a choice, not a typo.
        """
        if not isinstance(self.pool_id, str) or not self.pool_id.strip():
            raise ValueError("TipsPool.pool_id must be a non-empty string")
        if self.pool_id.strip() == PUBLIC_POOL_ID:
            raise ValueError(
                f"TipsPool.pool_id must not be {PUBLIC_POOL_ID!r} — that id marks "
                "the public pool, and reusing it would make a pool switch "
                "undetectable, leaving publicly-generated tips in place."
            )
        # Frozen dataclass: normal assignment is refused, so freeze the coerced
        # containers in via object.__setattr__ (the documented pattern, same as
        # PlatformContext.__post_init__).
        for name in ("curated", "catalog"):
            value = getattr(self, name)
            if isinstance(value, tuple):
                continue
            if isinstance(value, list):
                object.__setattr__(self, name, tuple(value))
                continue
            raise TypeError(
                f"TipsPool.{name} must be a tuple or list, got "
                f"{type(value).__name__}. An absent section in a packaged pool "
                "file must be spelled as an empty list, not null."
            )
