"""The category-order document: the sequence of the Discover rail.

WHAT THIS IS. ``category-order.json`` is published beside ``official-registry.json``
and ``editorial.json``, and carries exactly one thing: the ids of the categories the
Discover rail shows, in the order it shows them. Membership does NOT live here -- an
app's categories are a property of the app, and stay on its registry entry.

WHY IT IS ITS OWN DOCUMENT rather than a key inside ``editorial.json``. The schema
gate is whole-document and deliberately so: a version this client does not
recognise discards the ENTIRE document, because a client that keeps reading the
fields it happens to recognise is acting on a contract it cannot name. Bundled with
the featured layout, that gate couples two unrelated editorial decisions -- bumping
the version to publish a new featuring shape would also re-sort every category on
every client that has not shipped support yet. Separate documents give each its own
gate without weakening either gate.

The cost of the split is real and is paid on purpose: the rail's order and the
featured list now arrive over TWO fetches with TWO caches, so they can be one
revision apart. A rail ordered by a slightly older revision than the row of
featured cards beside it is a presentational skew nobody can see; a rail silently
re-sorted by an unrelated version bump is a regression users would.

WHAT THIS DOES NOT DO.

- **No labels from the document.** The document carries ids only. The rail is
  translated into 11 languages while a published string can only be one, so copy is
  resolved through the client's own i18n catalog at render time. An id the client
  has no copy for is DROPPED rather than shown as a raw slug, which means a
  genuinely new category needs a client release and is invisible until then.

- **No sequence field.** Order is array position. A numeric ``order`` would need
  two further invariants to mean anything -- ranks unique, and a defined tie-break
  when they are not -- and an array supplies both by construction.

- **No signature verification.** Same basis as the registry and the editorial
  document: TLS to our own origin. The worst a hostile document achieves here is a
  reordered or shortened rail, and the omission is named rather than left for a
  reader to infer from silence.

Every failure degrades to the order compiled into the client, which is what the
rail shows when this document is unreachable.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from kiro_crew.apps.official_catalog import OFFICIAL_CATALOG_BASE, fetch_document
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: This document's own supported schema version. Deliberately NOT the shared
#: constant from ``official_catalog``: per-document version gates are the whole
#: point of the split, and importing one shared constant would re-couple them at
#: the client -- the release that raises it for one document's new shape would
#: simultaneously start refusing the OTHER documents still published at v1,
#: registry included. Each reader owns its constant so one document can move
#: without the rest.
SUPPORTED_SCHEMA_VERSION = 1

OFFICIAL_CATEGORY_ORDER_URL = f"{OFFICIAL_CATALOG_BASE}category-order.json"

#: Matches the registry's and the editorial document's TTLs: one workflow run
#: publishes all three, so a shorter TTL here would only buy a window in which the
#: rail is ordered by a revision newer than the apps it orders.
CACHE_TTL = 3600
FAILURE_TTL = 60

#: The schema's own ceiling, applied here so a malformed document cannot make the
#: rail unbounded. A document above it is truncated rather than refused: this
#: reader also sees documents that never passed the publish gate (a stale cache, a
#: hand-edited file), and the first 30 categories beat no rail at all.
MAX_CATEGORIES = 30

_FAILED_KEY = "_fetchFailedAt"


def _cache_path() -> Path:
    return config_dir() / "cache" / "official-category-order.json"


def _read_cache() -> dict[str, Any] | None:
    """Return the cached document, or None when there is nothing usable.

    A cached FAILURE returns the sentinel rather than None: the caller must tell
    "no cache, go fetch" apart from "the fetch just failed, do not hammer it".
    """
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
    if _FAILED_KEY in data:
        return data if age <= FAILURE_TTL else None
    return data if age <= CACHE_TTL else None


def _write_cache(doc: dict[str, Any]) -> None:
    path = _cache_path()
    try:
        atomic_write(path, json.dumps(doc))
    except OSError:
        logger.debug("could not cache the category-order document", exc_info=True)


def _write_failure() -> None:
    _write_cache({_FAILED_KEY: time.time()})


def forget_cache() -> None:
    """Drop the cached document and any failure memory; the next read re-fetches.

    Same contract as ``official_catalog.forget_cache`` -- see there for why a
    file delete (not a re-fetch) is the whole job.
    """
    try:
        _cache_path().unlink(missing_ok=True)
    except OSError:
        logger.debug("could not drop the category-order cache", exc_info=True)


def _download() -> dict[str, Any] | None:
    """GET the document through the registry module's fetch seam.

    Deliberately NOT a second copy of the fetch: the https-only guard, the
    refuse-redirects opener, the byte cap and the exception family are security
    behaviour that must not drift between documents served from one origin.
    """
    return fetch_document(OFFICIAL_CATEGORY_ORDER_URL)


def _load_document(fetcher: Any = None) -> dict[str, Any] | None:
    """Fetch-or-cache the document, applying this document's own schema gate.

    The gate is separate from the editorial document's on purpose -- that
    independence is the whole reason the two are published apart.
    """
    doc = _read_cache()
    if doc is not None and _FAILED_KEY in doc:
        # A recent fetch failed. Answer from the default WITHOUT another attempt --
        # the point of remembering the failure is not paying its timeout again.
        return None
    if doc is None:
        doc = (fetcher or _download)()
        if doc is None:
            _write_failure()
        else:
            _write_cache(doc)
    if doc is None:
        return None

    version = doc.get("schemaVersion")
    # `type(...) is int` rather than `==` or `isinstance`: `1.0 == 1` and
    # `True == 1` both hold in Python and `bool` subclasses `int`, so either looser
    # test accepts a document whose version is not an integer.
    if type(version) is not int or version != SUPPORTED_SCHEMA_VERSION:
        logger.warning(
            "category-order document declares schemaVersion %r, expected %r; ignoring it",
            version,
            SUPPORTED_SCHEMA_VERSION,
        )
        return None
    return doc


def load_category_order(fetcher: Any = None) -> list[str]:
    """Return published category ids in rail order, or ``[]`` to use the default.

    Empty is always a safe answer -- the rail falls back to the order compiled into
    the client. Nothing read here may raise: a malformed entry drops that entry
    only.

    *fetcher* is injected by tests.
    """
    doc = _load_document(fetcher)
    if doc is None:
        return []

    raw = doc.get("categories")
    if not isinstance(raw, list):
        return []

    seen: set[str] = set()
    out: list[str] = []
    for item in raw[:MAX_CATEGORIES]:
        if not isinstance(item, str):
            continue
        cid = item.strip()
        # A repeat keeps its FIRST position: the publish gate rejects duplicates,
        # so this only fires on a document that bypassed it, and the curator's
        # first mention is the one that reads as intentional.
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    return out


__all__ = ["forget_cache", "load_category_order", "MAX_CATEGORIES", "OFFICIAL_CATEGORY_ORDER_URL"]
