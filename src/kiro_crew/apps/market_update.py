"""Whether a newer listing version exists for an app installed from the marketplace.

An app installed from a listing records ``source = "market:<ref>@<n>"``. Its
update state is answered in two halves, like every other installed app's:

* :func:`refresh_market_updates` asks the listing API, through the
  ``MarketClient`` Protocol, for the latest installable version of each recorded
  listing and writes the answers to a cache under the data home. It is the only
  half that reaches the network.
* :func:`market_update_status` answers one installed row from that cache alone,
  so ``GET /api/apps`` never waits on the listing API.

**A failed or offline check shows nothing.** A listing the refresh could not
answer is left out of the cache, which drops any earlier answer for it, and a
listing without an answer gets no ``update`` field at all: never an error badge,
and never a stale "update available" the API cannot confirm.

**Read only.** Nothing here installs or downloads; the Protocol's download
methods are never called. Applying an update is a separate, confirmed step.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from kiro_crew.apps.market_source import LISTING_REF_RE, parse_market_source
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

if TYPE_CHECKING:
    from kiro_crew.apps.marketplace_install import MarketClient

logger = logging.getLogger(__name__)

CACHE_FILENAME = "market-updates.json"


def _cache_path() -> Path:
    return config_dir() / CACHE_FILENAME


def _publishable(value: Any) -> str:
    # Imported here: update_status imports this module, and its version rule is
    # the one every update answer is held to.
    from kiro_crew.apps.update_status import publishable_version

    return publishable_version(value)


def refresh_market_updates(client: MarketClient, records: Iterable[dict[str, Any]]) -> None:
    """Ask *client* for the latest version of every market listing in *records*.

    Rewrites the whole cache, so a listing this check could not answer loses any
    earlier answer. Total by contract: a client error, an answer about another
    listing, or a cache that cannot be written is logged and leaves the affected
    listings with no answer.
    """
    refs = sorted(
        {
            parsed[0]
            for record in records
            if isinstance(record, dict)
            and (parsed := parse_market_source(record.get("source"))) is not None
        }
    )
    checked_at = datetime.now(timezone.utc).isoformat()
    answers: dict[str, dict[str, Any]] = {}
    for ref in refs:
        try:
            resolved = client.resolve_install(ref, None)
        except Exception:
            logger.info("market update check failed for %s", ref, exc_info=True)
            continue
        version = getattr(resolved, "version", None)
        if getattr(resolved, "ref", None) != ref or not _valid_version(version):
            logger.info("market update check for %s answered for another listing", ref)
            continue
        answers[ref] = {
            "version": version,
            "appVersion": _publishable(getattr(resolved, "app_version", "")),
            "checkedAt": checked_at,
        }
    try:
        atomic_write(_cache_path(), json.dumps(answers, indent=2, sort_keys=True) + "\n")
    except OSError:
        logger.warning("could not write the market update cache", exc_info=True)


def _valid_version(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def read_market_updates() -> dict[str, dict[str, Any]]:
    """The cached answers, ``ref -> answer``; ``{}`` when absent or unreadable."""
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        ref: answer
        for ref, answer in data.items()
        if isinstance(ref, str)
        and LISTING_REF_RE.fullmatch(ref)
        and isinstance(answer, dict)
        and _valid_version(answer.get("version"))
    }


def market_update_status(source: Any, cache: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The ``update`` field for a row with marketplace *source*, or ``None``.

    ``available`` compares listing versions, the integer after ``@``.
    ``latestVersion`` is the latest version's app version when it is a
    publishable version, else the listing version as digits.
    """
    parsed = parse_market_source(source)
    if parsed is None:
        return None
    ref, installed = parsed
    answer = cache.get(ref)
    if answer is None:
        return None
    latest = answer["version"]
    checked_at = answer.get("checkedAt")
    return {
        "available": latest > installed,
        "latestVersion": _publishable(answer.get("appVersion")) or str(latest),
        "listingVersion": latest,
        "checkedAt": checked_at if isinstance(checked_at, str) else None,
    }
