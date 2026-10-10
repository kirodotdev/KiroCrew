"""Catalog ranking document: fetch, cache, fallback, and no-data guards.

The ranking document carries per-app install counts for Discover sorting, fetched
from a known location with the same cache-and-fallback behaviour as the registry
index. It is the consumer of the install-receipt aggregation described in
metrics.md, decoupled from any specific pipeline so any producer satisfying the
schema works.

The network fetch goes through ``official_catalog.fetch_document``: the ranking
document is served from the same ``apps.crew.kiro.dev`` origin as the catalog, and
the https-only, redirect-refusing, byte-capped behaviour of that seam is security
behaviour that must not drift into a second copy.

Security and correctness constraints (hard requirements):
1. Rank on k=fresh only: update receipts never enter install rank.
2. Cumulative + windowed counts: the schema supports both so Discover can show
   "Popular" (all-time) and "Trending" (windowed) views.
3. Uncounted apps get NO rank, never zero: showing an uncounted app at the bottom
   of a popularity list is wrong. An app not in the document is simply unranked.
4. Never trust a zero: an empty or all-zero document is NO DATA and triggers
   fallback with loud logging.
5. Rank is not self-assertable: no registry-supplied field may influence rank.
   Only this published ranking document can provide rank.
6. Stale-safe: cache with fallback to the last good copy. A stale ranking beats
   none; an absent ranking beats a wrong one.
7. Aggregates only: the document carries per-app counts and nothing else.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.apps.official_catalog import OFFICIAL_CATALOG_BASE, fetch_document
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: The ranking document is published alongside the official catalog, under the
#: same origin and so behind the same trust basis as ``fetch_document`` enforces.
RANKING_DOCUMENT_URL = f"{OFFICIAL_CATALOG_BASE}catalog-ranking.json"

#: Schema version this code understands.
SUPPORTED_SCHEMA_VERSION = 1

#: Cache TTL: ranking can be slightly stale without harm.
CACHE_TTL = 3600  # 1 hour

#: How long a failed fetch is remembered. Shorter than success TTL so recovery
#: from an outage is prompt.
FAILURE_TTL = 60


@dataclass(frozen=True)
class AppRanking:
    """Ranking data for a single app."""

    app_slug: str
    cumulative_installs: int
    windowed_installs: int  # Installs in the trending window


@dataclass(frozen=True)
class RankingDocument:
    """The parsed ranking document."""

    schema_version: int
    generated_at: str
    window_days: int  # The trending window size (e.g. 30 days)
    rankings: dict[str, AppRanking]  # Keyed by app_slug

    def get_cumulative_rank(self, app_slug: str) -> int | None:
        """Return cumulative install count, or None if app is not ranked.

        An app not in the document returns None, never zero. This is the
        'no rank, not zero rank' contract.
        """
        ranking = self.rankings.get(app_slug)
        if ranking is None:
            return None
        return ranking.cumulative_installs

    def get_windowed_rank(self, app_slug: str) -> int | None:
        """Return windowed (trending) install count, or None if not ranked."""
        ranking = self.rankings.get(app_slug)
        if ranking is None:
            return None
        return ranking.windowed_installs


def _cache_path() -> Path:
    """Path to the cached last-good ranking document."""
    return config_dir() / "cache" / "catalog-ranking.json"


def _failure_path() -> Path:
    """Path to the fetch-failure marker.

    The marker lives in its OWN file, never in the document cache, so recording a
    failure cannot overwrite the last-good document. A failure must leave the
    stale ranking available: a stale ranking beats none.
    """
    return config_dir() / "cache" / "catalog-ranking.failed"


def _read_cache() -> dict[str, Any] | None:
    """Return the cached document when it is still fresh, else None."""
    path = _cache_path()
    try:
        if not path.is_file():
            return None
        age = time.time() - path.stat().st_mtime
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data if age <= CACHE_TTL else None


def _read_stale_cache() -> dict[str, Any] | None:
    """Return the cached document ignoring TTL, for fallback to the last-good copy."""
    path = _cache_path()
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _recent_failure() -> bool:
    """Report whether a fetch failed within the last FAILURE_TTL seconds.

    When True the caller backs off instead of fetching again, so an outage does
    not cost every read a fresh attempt and timeout.
    """
    path = _failure_path()
    try:
        if not path.is_file():
            return False
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age <= FAILURE_TTL


def _write_cache(doc: dict[str, Any]) -> None:
    """Write the ranking document to cache and clear any failure memory."""
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(doc))
    except OSError:
        logger.debug("could not cache the ranking document", exc_info=True)
        return
    _clear_failure()


def _write_failure() -> None:
    """Remember that the fetch just failed, without touching the document cache."""
    path = _failure_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, str(time.time()))
    except OSError:
        logger.debug("could not record the ranking fetch failure", exc_info=True)


def _clear_failure() -> None:
    """Drop the failure marker so a later read is not held back by stale back-off."""
    try:
        _failure_path().unlink(missing_ok=True)
    except OSError:
        logger.debug("could not clear the ranking failure marker", exc_info=True)


def _safe_count(value: Any) -> int:
    """Coerce an install count to a non-negative int, treating anything else as 0.

    Counts arrive from a document fetched over the network, so their TYPE is as
    untrusted as their content: a string, ``null``, or a float would otherwise
    reach a ``> 0`` comparison and raise ``TypeError`` out of the whole load,
    bypassing the stale fallback. ``bool`` is excluded explicitly because it
    subclasses ``int``. A value that is not a usable count becomes 0, which the
    callers already treat as "no count for this entry".
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _validate_document(doc: dict[str, Any]) -> str | None:
    """Validate the document envelope. Returns an error message or None if valid."""
    schema_version = doc.get("schemaVersion")
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        return f"unsupported schemaVersion: {schema_version}"

    if "generatedAt" not in doc:
        return "missing generatedAt"

    window_days = doc.get("windowDays")
    if not isinstance(window_days, int) or isinstance(window_days, bool) or window_days <= 0:
        return f"invalid windowDays: {window_days}"

    rankings = doc.get("rankings")
    if not isinstance(rankings, list):
        return "rankings must be a list"

    return None


def _is_empty_or_all_zero(doc: dict[str, Any]) -> bool:
    """Check if the document is empty or has all-zero counts.

    This is the 'never trust a zero' guard: an empty or all-zero document is
    treated as NO DATA, not as a legitimate ranking of zeros. A rollup that
    queries an empty partition can report success and publish all zeros, so
    accepting those zeros would overwrite the last good ranking with nothing.

    Counts are normalised through _safe_count first, so a malformed count never
    raises here and is simply read as the absence of a count.
    """
    rankings = doc.get("rankings", [])
    if not rankings:
        return True

    for entry in rankings:
        if not isinstance(entry, dict):
            continue
        cumulative = _safe_count(entry.get("cumulativeInstalls"))
        windowed = _safe_count(entry.get("windowedInstalls"))
        if cumulative > 0 or windowed > 0:
            return False

    return True


def _parse_document(doc: dict[str, Any]) -> RankingDocument | None:
    """Parse a validated document into a RankingDocument.

    Returns None if the document is structurally valid but semantically empty
    (the no-data guard).
    """
    error = _validate_document(doc)
    if error is not None:
        logger.warning("ranking document validation failed: %s", error)
        return None

    # The 'never trust a zero' guard.
    if _is_empty_or_all_zero(doc):
        logger.warning(
            "ranking document is empty or all-zero: treating as NO DATA and falling back"
        )
        return None

    rankings: dict[str, AppRanking] = {}
    for entry in doc.get("rankings", []):
        if not isinstance(entry, dict):
            continue
        app_slug = entry.get("appSlug")
        if not isinstance(app_slug, str) or not app_slug:
            continue

        # Counts are normalised before any comparison: a non-int (string, null,
        # float) count becomes 0 rather than raising, so one malformed entry
        # degrades that entry instead of the whole document.
        cumulative = _safe_count(entry.get("cumulativeInstalls"))
        windowed = _safe_count(entry.get("windowedInstalls"))

        # An entry with no usable count is not ranked (no rank, not zero rank).
        if cumulative <= 0 and windowed <= 0:
            continue

        rankings[app_slug] = AppRanking(
            app_slug=app_slug,
            cumulative_installs=cumulative,
            windowed_installs=windowed,
        )

    if not rankings:
        logger.warning("ranking document has no valid entries after filtering zeros")
        return None

    return RankingDocument(
        schema_version=doc["schemaVersion"],
        generated_at=doc["generatedAt"],
        window_days=doc["windowDays"],
        rankings=rankings,
    )


def load_ranking_document(fetcher: Any = None) -> RankingDocument | None:
    """Load the ranking document with cache-and-fallback behaviour.

    The fetch/cache/fallback pattern mirrors the registry index:
    1. Check the cache; if fresh and valid, return it.
    2. If a fetch failed within FAILURE_TTL, do not hammer the endpoint; fall
       back to the last-good cache.
    3. Otherwise fetch. On success, cache and return.
    4. On fetch failure OR an invalid/empty document, record the failure and fall
       back to the last-good cache, which the failure marker never overwrites.
    5. If no fallback is available, return None (Discover orders by name as today).

    *fetcher* is injected by tests; it defaults to the shared catalog fetch seam.

    Returns None when there is no usable ranking, which means Discover should
    fall back to its current ordering (by name). This is the 'absent ranking
    beats a wrong one' contract.
    """
    # Fresh cache hit wins outright.
    cached = _read_cache()
    if cached is not None:
        result = _parse_document(cached)
        if result is not None:
            return result
        # Cached document is invalid; fall through to fetch.

    # Read the last-good copy up front so it is available as a fallback whether
    # we back off on a recent failure or fetch and fail now.
    stale = _read_stale_cache()

    # A recent failure means back off rather than fetch again.
    if _recent_failure():
        if stale is not None:
            result = _parse_document(stale)
            if result is not None:
                logger.debug("using stale ranking cache during failure back-off")
                return result
        return None

    doc = (fetcher or fetch_document)(RANKING_DOCUMENT_URL)
    if doc is None:
        _write_failure()
        if stale is not None:
            result = _parse_document(stale)
            if result is not None:
                logger.info("ranking fetch failed; using stale cache")
                return result
        logger.info("ranking fetch failed and no stale cache available")
        return None

    result = _parse_document(doc)
    if result is None:
        # Invalid or empty (no-data guard). Record the failure so the next call
        # backs off, and keep the last-good cache rather than overwriting it.
        _write_failure()
        if stale is not None:
            stale_result = _parse_document(stale)
            if stale_result is not None:
                logger.warning("new ranking document invalid; keeping stale cache")
                return stale_result
        logger.warning("new ranking document invalid and no valid stale cache")
        return None

    # Success: cache the document and clear any failure memory.
    _write_cache(doc)
    return result
