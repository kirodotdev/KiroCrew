"""Catalog ranking document: fetch, cache, fallback, and no-data guards.

The ranking document carries per-app install counts for Discover sorting, fetched
from a known location with the same cache-and-fallback behaviour as the registry
index. It is the consumer of the install-receipt aggregation described in
metrics.md, decoupled from any specific pipeline so any producer satisfying the
schema works.

The network fetch goes through ``official_catalog.fetch_document_with_status``: the ranking
document is served from the same ``apps.crew.kiro.dev`` origin as the catalog, and
the https-only, redirect-refusing, byte-capped behaviour of that seam is security
behaviour that must not drift into a second copy.

Security and correctness constraints (hard requirements):
1. Rank on k=fresh only: update receipts never enter install rank.
2. Cumulative + windowed counts: the schema supports both so Discover can show
   "Popular" (all-time) and "Trending" (windowed) views.
3. Uncounted apps get NO rank, never zero: showing an uncounted app at the bottom
   of a popularity list is wrong. An app not in the document is simply unranked,
   and so is one whose count the producer withheld under a small-count floor.
4. Never trust a zero: an empty or all-zero document is NO DATA and triggers
   fallback with loud logging. A withheld count is not a zero: a document whose
   counts are all withheld reports that every app sits below the floor, which is
   an answer, and it replaces the cached copy rather than leaving counts that
   predate the floor in service.
5. Rank is not self-assertable: no registry-supplied field may influence rank.
   Only this published ranking document can provide rank.
6. Stale-safe: cache with fallback to the last good copy. A stale ranking beats
   none; an absent ranking beats a wrong one. The fallback is bounded by
   MAX_AGE_WINDOWS: past that the windowed counts describe a window that has
   closed, so the document is refused rather than served as current.
7. Aggregates only: the document carries per-app counts and nothing else.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kiro_crew.apps.official_catalog import OFFICIAL_CATALOG_BASE, fetch_document_with_status
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: The ranking document is published alongside the official catalog, under the
#: same origin and so behind the same trust basis as that fetch seam enforces.
RANKING_DOCUMENT_URL = f"{OFFICIAL_CATALOG_BASE}catalog-ranking.json"

#: Schema version this code understands.
SUPPORTED_SCHEMA_VERSION = 1

#: Cache TTL: ranking can be slightly stale without harm.
CACHE_TTL = 3600  # 1 hour

#: How long a failed fetch is remembered. Shorter than success TTL so recovery
#: from an outage is prompt.
FAILURE_TTL = 60

#: How long a "nobody has published this" answer is remembered. A 404 is not an
#: outage: it is the origin stating that the document does not exist, which is
#: the ordinary case until a producer ships. Backing off for FAILURE_TTL there
#: would cost one outbound request per minute per active store, forever, for a
#: document known not to be there -- and would hand the origin a log line every
#: time the store is opened. The sibling catalog fetch is cached for an hour, so
#: a not-published answer is held exactly as long as a successful one.
NOT_PUBLISHED_TTL = CACHE_TTL

#: HTTP status that means "not published" rather than "could not be reached".
_NOT_PUBLISHED_STATUS = 404

#: How many ``windowDays`` windows a document may be behind before it is refused
#: outright. The last-good fallback has to be bounded by something: without a
#: cap, a producer that stops publishing leaves its final document served as the
#: current ranking for as long as the cache survives, and its windowed counts
#: describe a window that closed long before. Three windows allows for a
#: producer that misses a cycle or two.
MAX_AGE_WINDOWS = 3

#: The largest ``windowDays`` whose age cap is representable. ``timedelta`` tops
#: out just under a billion days, so a document declaring a larger window would
#: raise ``OverflowError`` while the cap is computed, and that escapes the
#: loader entirely: the caller gets an exception rather than the last-good
#: ranking a bad document is supposed to fall back to. A window past this bound
#: is a validation error, which routes it down the ordinary failure path. The
#: bound is derived from ``timedelta`` rather than picked, so it stays correct
#: if MAX_AGE_WINDOWS changes.
MAX_WINDOW_DAYS = timedelta.max.days // MAX_AGE_WINDOWS

#: The count fields an entry may carry, and the ones a floor may withhold.
_COUNT_FIELDS = ("cumulativeInstalls", "windowedInstalls")


@dataclass(frozen=True)
class AppRanking:
    """Ranking data for a single app.

    A count is ``None`` when there is no number to report for it: the producer
    withheld it under a small-count floor, or it arrived absent or malformed.
    What a missing count never becomes is ``0``, which would present the app as
    measured at zero and sort it to the bottom of a popularity list.
    """

    app_slug: str
    cumulative_installs: int | None
    windowed_installs: int | None  # Installs in the trending window


@dataclass(frozen=True)
class RankingDocument:
    """The parsed ranking document."""

    schema_version: int
    generated_at: str
    window_days: int  # The trending window size (e.g. 30 days)
    rankings: dict[str, AppRanking]  # Keyed by app_slug

    def get_cumulative_rank(self, app_slug: str) -> int | None:
        """Return cumulative install count, or None when there is no number.

        None covers an app absent from the document and an app whose count the
        producer withheld, and it is never zero. This is the 'no rank, not zero
        rank' contract: a caller may order by the number it gets back, and must
        treat None as unrankable rather than as the smallest value.
        """
        ranking = self.rankings.get(app_slug)
        if ranking is None:
            return None
        return ranking.cumulative_installs

    def get_windowed_rank(self, app_slug: str) -> int | None:
        """Return windowed (trending) install count, or None when there is none.

        An app can be ranked cumulatively and withheld in the window, so this
        returns None for an app that ``get_cumulative_rank`` answers with a
        number.
        """
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


def _failure_backoff(path: Path) -> float:
    """Return the back-off the failure marker asks for, in seconds.

    The marker's CONTENT is that back-off, written by ``_write_failure``, so a
    "not published" answer can be held longer than a transient outage without a
    second marker file to keep in step with this one.

    Anything unreadable, unparseable, or outside the range the writer can
    produce falls back to FAILURE_TTL -- the shorter, safer answer, since it
    only costs an earlier retry, where an over-long one would hold the ranking
    off for as long as the bad value said.
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
        backoff = float(raw)
    except (OSError, ValueError):
        return FAILURE_TTL
    if not FAILURE_TTL <= backoff <= NOT_PUBLISHED_TTL:
        return FAILURE_TTL
    return backoff


def _recent_failure() -> bool:
    """Report whether a failed fetch is still inside the back-off it asked for.

    When True the caller backs off instead of fetching again, so an outage does
    not cost every read a fresh attempt and timeout, and an unpublished document
    is not re-requested every minute for as long as the store is in use.
    """
    path = _failure_path()
    try:
        if not path.is_file():
            return False
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age <= _failure_backoff(path)


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


def _write_failure(backoff: float = FAILURE_TTL) -> None:
    """Remember that the fetch just failed, without touching the document cache.

    *backoff* is how long the next read should wait before trying again, and it
    is the marker's whole content: the marker's mtime carries WHEN, so the file
    carries FOR HOW LONG. NOT_PUBLISHED_TTL is passed for a 404, where the
    answer is durable; the default suits everything else, which may recover at
    any moment.
    """
    path = _failure_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, str(backoff))
    except OSError:
        logger.debug("could not record the ranking fetch failure", exc_info=True)


def _clear_failure() -> None:
    """Drop the failure marker so a later read is not held back by stale back-off."""
    try:
        _failure_path().unlink(missing_ok=True)
    except OSError:
        logger.debug("could not clear the ranking failure marker", exc_info=True)


def forget_cache() -> None:
    """Drop the cached document AND the failure memory; the next read re-fetches.

    Both files, not just the document: this ranking keeps its failure marker in
    a separate file, so dropping the document while a marker was still inside
    ``FAILURE_TTL`` would send the next read down the back-off path and return
    ``None`` instead of re-fetching -- the opposite of what a refresh is for.

    Same contract as ``official_catalog.forget_cache`` otherwise: a file delete,
    not a re-fetch, is the whole job.
    """
    try:
        _cache_path().unlink(missing_ok=True)
    except OSError:
        logger.debug("could not drop the ranking cache", exc_info=True)
    _clear_failure()


def _read_count(value: Any) -> int | None:
    """Return a usable non-negative count, or None when there is no number.

    Counts arrive from a document fetched over the network, so their TYPE is as
    untrusted as their content: a string or a float would otherwise reach a
    ``> 0`` comparison and raise ``TypeError`` out of the whole load, bypassing
    the stale fallback. ``bool`` is excluded explicitly because it subclasses
    ``int``.

    ``null`` is the document's marker for a count withheld under a small-count
    floor, and it returns None here for the same reason a malformed count does:
    there is no number to report. Reading either as ``0`` would publish a
    measured zero the producer never claimed, and a floor that is undone one
    layer down protects nobody.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _is_withheld(entry: dict[str, Any]) -> bool:
    """True when the entry is published with one of its counts withheld.

    An explicit ``null`` says the entry exists and its number is deliberately
    hidden, which is information. An explicit ``0`` says the opposite, and is
    what a rollup over an empty partition publishes. Reading a withheld count as
    absent data would discard a correctly floored document and leave the
    last-good copy, whose counts predate the floor, in service: the floor would
    make the exposure worse rather than better.
    """
    return any(field in entry and entry[field] is None for field in _COUNT_FIELDS)


def _parse_generated_at(value: Any) -> datetime | None:
    """Parse the document's ``generatedAt`` into an aware datetime, or None.

    The age of the document is what bounds the stale fallback, so a timestamp
    that cannot be read is a document whose age cannot be established, which the
    caller treats as invalid. A naive timestamp is read as UTC.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_past_max_age(generated_at: datetime, window_days: int) -> bool:
    """True when the document is too old to describe its own window.

    ``windowedInstalls`` counts a ``windowDays`` window ending at
    ``generatedAt``. Once the document is MAX_AGE_WINDOWS windows behind, that
    count describes a window that has closed, and serving it as a current
    trending figure is the wrong-ranking case the fallback exists to avoid. A
    document from the future is not refused here: a clock skew between producer
    and host is not grounds to drop a ranking.

    The caller rejects a *window_days* past MAX_WINDOW_DAYS before reaching
    here, so constructing the cap cannot overflow.
    """
    max_age = timedelta(days=window_days * MAX_AGE_WINDOWS)
    return datetime.now(timezone.utc) - generated_at > max_age


def _validate_document(doc: dict[str, Any]) -> str | None:
    """Validate the document envelope. Returns an error message or None if valid."""
    schema_version = doc.get("schemaVersion")
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        return f"unsupported schemaVersion: {schema_version}"

    if "generatedAt" not in doc:
        return "missing generatedAt"

    generated_at = _parse_generated_at(doc["generatedAt"])
    if generated_at is None:
        return f"unparseable generatedAt: {doc['generatedAt']!r}"

    window_days = doc.get("windowDays")
    if not isinstance(window_days, int) or isinstance(window_days, bool) or window_days <= 0:
        return f"invalid windowDays: {window_days}"

    if window_days > MAX_WINDOW_DAYS:
        return f"windowDays out of range: {window_days}"

    if _is_past_max_age(generated_at, window_days):
        return (
            f"document is more than {MAX_AGE_WINDOWS} windows old: "
            f"generatedAt={doc['generatedAt']} windowDays={window_days}"
        )

    rankings = doc.get("rankings")
    if not isinstance(rankings, list):
        return "rankings must be a list"

    return None


def _collect_rankings(doc: dict[str, Any]) -> tuple[dict[str, AppRanking], bool]:
    """Read the document's entries into rankings, and report whether it has data.

    The second element is True when at least one well-formed entry carries a
    number or a withheld count. It is the discriminator behind the no-data
    guard, and it is produced by the same pass that builds the rankings so the
    guard can never disagree with what was parsed.

    An entry with no number is left out of the map. Unranked and withheld read
    the same to a caller asking for a rank, and both differ from zero.
    """
    rankings: dict[str, AppRanking] = {}
    carries_data = False

    for entry in doc.get("rankings", []):
        if not isinstance(entry, dict):
            continue
        app_slug = entry.get("appSlug")
        if not isinstance(app_slug, str) or not app_slug:
            continue

        # Counts are normalised before any comparison: a non-int (string, float)
        # count reads as no number rather than raising, so one malformed entry
        # degrades that entry instead of the whole document.
        cumulative = _read_count(entry.get("cumulativeInstalls"))
        windowed = _read_count(entry.get("windowedInstalls"))

        # A window is a SUBSET of all time, so a cumulative count below its own
        # windowed count describes an impossible app, and the entry is malformed.
        # Dropped rather than repaired: which of the two numbers is wrong is not
        # knowable from here, and either could be the one a caller sorts by.
        #
        # This guard is also what keeps "a zero is never served as a measured
        # count" true by construction. Ranking requires one count above zero, so
        # the only way a ZERO cumulative count could be ranked -- and then
        # stamped onto a store row as measured-at-zero -- is beside a positive
        # windowed count, which is precisely the shape rejected here.
        if cumulative is not None and windowed is not None and cumulative < windowed:
            logger.warning(
                "ranking entry %r has a cumulative count below its windowed count "
                "(%d < %d); dropping the entry as malformed",
                app_slug,
                cumulative,
                windowed,
            )
            continue

        has_count = (cumulative is not None and cumulative > 0) or (
            windowed is not None and windowed > 0
        )

        if has_count or _is_withheld(entry):
            carries_data = True

        # An entry with no usable count is not ranked (no rank, not zero rank).
        if not has_count:
            continue

        rankings[app_slug] = AppRanking(
            app_slug=app_slug,
            cumulative_installs=cumulative,
            windowed_installs=windowed,
        )

    return rankings, carries_data


def _parse_document(doc: dict[str, Any]) -> RankingDocument | None:
    """Parse a validated document into a RankingDocument.

    Returns None when the document is unusable: it fails validation, it is older
    than the age cap, or it reports nothing at all (the no-data guard).
    """
    error = _validate_document(doc)
    if error is not None:
        logger.warning("ranking document validation failed: %s", error)
        return None

    rankings, carries_data = _collect_rankings(doc)

    # The 'never trust a zero' guard: a rollup that queries an empty partition
    # can report success and publish all zeros, so accepting those zeros would
    # overwrite the last good ranking with nothing.
    #
    # A document whose counts are all WITHHELD under a floor is the opposite
    # case. It reports that every app sits below the floor, which is an answer,
    # so it counts as data and replaces the cached copy. Treating it as no-data
    # would keep serving counts that predate the floor.
    if not carries_data:
        logger.warning(
            "ranking document reports no counts at all: treating as NO DATA and falling back"
        )
        return None

    return RankingDocument(
        schema_version=doc["schemaVersion"],
        generated_at=doc["generatedAt"],
        window_days=doc["windowDays"],
        rankings=rankings,
    )


def _fetch(fetcher: Any = None) -> tuple[dict[str, Any] | None, int | None]:
    """Fetch the ranking document as ``(document, http_status)``.

    One shape, the same one ``fetch_document_with_status`` returns, so an
    injected *fetcher* stands in for the real seam rather than for a simpler
    thing the real seam is not. The status is what lets the caller tell a
    document the origin says is absent from one it could not be asked about,
    which is the whole reason this module does not use ``fetch_document``.

    The values are still normalised, because a document arrives over the network
    and its TYPE is as untrusted as its content.
    """
    doc, status = (fetcher or fetch_document_with_status)(RANKING_DOCUMENT_URL)
    return (
        doc if isinstance(doc, dict) else None,
        status if isinstance(status, int) and not isinstance(status, bool) else None,
    )


def load_ranking_document(fetcher: Any = None) -> RankingDocument | None:
    """Load the ranking document with cache-and-fallback behaviour.

    The fetch/cache/fallback pattern mirrors the registry index:
    1. Check the cache; if fresh and valid, return it.
    2. If a fetch failed and its back-off has not elapsed, do not hammer the
       endpoint; fall back to the last-good cache.
    3. Otherwise fetch. On success, cache and return.
    4. On fetch failure OR an invalid/empty document, record the failure and fall
       back to the last-good cache, which the failure marker never overwrites.
       A 404 is recorded with the NOT_PUBLISHED_TTL back-off rather than the
       outage one: it means no producer has published yet, which is the ordinary
       case today and will not change within the minute.
    5. If no fallback is available, return None (Discover orders by name as today).

    *fetcher* is injected by tests; it defaults to the shared catalog fetch seam.
    It may return either a bare document (or None) or a ``(document, status)``
    pair, so a test can exercise the not-published path without every existing
    caller growing a status it has no opinion about.

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

    doc, status = _fetch(fetcher)
    if doc is None:
        if status == _NOT_PUBLISHED_STATUS:
            # Not an outage: the origin says this document does not exist.
            _write_failure(NOT_PUBLISHED_TTL)
            logger.debug(
                "no ranking document is published (HTTP %s); not asking again for %ds",
                status,
                int(NOT_PUBLISHED_TTL),
            )
        else:
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
