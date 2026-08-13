"""The editorial document: presentation the curator controls without a release.

WHAT THIS IS. ``editorial.json`` is published beside ``official-registry.json``
and carries PRESENTATION only -- which categories the Discover rail shows and in
what order. The registry says what exists; this says how it is arranged. Keeping
them apart is what lets a curator reorder the rail without touching the list of
apps, and lets the client refuse one document while still rendering the other.

WHAT THIS DOES NOT DO, YET.

- **No sections.** ``sections`` carries spotlight / rail / banner entries, and
  the live document publishes an empty list. Nothing here reads it: a spotlight
  names an app plus a blurb, which is INVENTORY-adjacent presentation, and
  wiring it before the registry can add inventory would render a spotlight for
  an app the client cannot show. The field is left alone rather than half-read.

- **No labels from the document.** A category's ``label`` is published in
  English only, while the rail is translated into 11 languages, so honouring it
  would replace localised copy with English for every user. The client therefore
  takes ``id`` and ``order`` and resolves copy through its own catalog; an id the
  client has no copy for is DROPPED rather than shown raw. Consequence, stated
  plainly: a genuinely new category needs a release, and until then it is
  invisible instead of appearing as a slug.

- **No signature verification.** Same basis as the registry: TLS to our own
  domain. Presentation is a lower-stakes payload than inventory -- the worst a
  hostile document achieves here is a reordered or shortened rail -- but the
  omission is named here rather than left for a reader to infer from silence.

Every failure degrades to the client's built-in order, which is what the rail
used before this module existed.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from kiro_crew.apps.official_catalog import (
    MAX_BYTES,
    OFFICIAL_CATALOG_BASE,
    SUPPORTED_SCHEMA_VERSION,
    fetch_document,
)
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

OFFICIAL_EDITORIAL_URL = f"{OFFICIAL_CATALOG_BASE}editorial.json"

#: Same TTLs as the registry: the two documents are published together by one
#: workflow run, so caching them for different lengths would show a rail ordered
#: by one revision beside apps listed by another.
CACHE_TTL = 3600
FAILURE_TTL = 60

#: The schema's own ceiling. A document above it is malformed, not merely large,
#: and the cap is applied here so a bad document cannot make the rail unbounded.
MAX_CATEGORIES = 30

_FAILED_KEY = "_fetchFailedAt"


def _cache_path() -> Path:
    return config_dir() / "cache" / "official-editorial.json"


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
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if _FAILED_KEY in data:
        return data if age <= FAILURE_TTL else None
    return data if age <= CACHE_TTL else None


def _write_cache(doc: dict[str, Any]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
    except OSError:
        logger.debug("could not cache the editorial document", exc_info=True)


def _write_failure() -> None:
    _write_cache({_FAILED_KEY: time.time()})


def _download() -> dict[str, Any] | None:
    """GET the editorial document through the registry module's fetch seam.

    Deliberately NOT a second copy of the fetch: the https-only guard, the
    refuse-redirects opener, the byte cap and the exception family are security
    behaviour that must not drift between two documents served from one origin.
    """
    return fetch_document(OFFICIAL_EDITORIAL_URL)


def _coerce_order(value: Any) -> int | None:
    """An ``order`` that is not a real int is missing, not zero.

    ``type(...) is int`` rather than ``isinstance``: ``bool`` subclasses ``int``,
    so ``True`` would otherwise sort as 1 and quietly place a category first.
    """
    return value if type(value) is int else None


def load_category_order(fetcher: Any = None) -> list[str]:
    """Return published category ids in rail order, or ``[]`` to use the default.

    Empty is always a safe answer -- the rail falls back to the order compiled
    into the client, which is what it showed before this module existed. Nothing
    read here may raise: a malformed field degrades that field only.

    *fetcher* is injected by tests.
    """
    doc = _read_cache()
    if doc is not None and _FAILED_KEY in doc:
        # A recent fetch failed. Answer from the default WITHOUT another attempt.
        return []
    if doc is None:
        doc = (fetcher or _download)()
        if doc is None:
            _write_failure()
        else:
            _write_cache(doc)
    if doc is None:
        return []

    version = doc.get("schemaVersion")
    # Identical gate to the registry's, for the identical reason: `1.0 == 1` and
    # `True == 1` both hold, so a looser test accepts a version field that is not
    # an integer and acts on a contract it cannot name.
    if type(version) is not int or version != SUPPORTED_SCHEMA_VERSION:
        logger.warning(
            "editorial document declares schemaVersion %r, expected %r; ignoring it",
            version,
            SUPPORTED_SCHEMA_VERSION,
        )
        return []

    raw = doc.get("categories")
    if not isinstance(raw, list):
        return []

    ranked: list[tuple[int, int, str]] = []
    for position, item in enumerate(raw[:MAX_CATEGORIES]):
        if not isinstance(item, dict):
            continue
        cid = item.get("id")
        if not isinstance(cid, str) or not cid.strip():
            continue
        order = _coerce_order(item.get("order"))
        if order is None:
            continue
        # Document position breaks an `order` tie, so a duplicated order is
        # stable rather than dependent on sort implementation details.
        ranked.append((order, position, cid.strip()))

    ranked.sort()
    seen: set[str] = set()
    out: list[str] = []
    for _, _, cid in ranked:
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


__all__ = ["load_category_order", "MAX_BYTES", "OFFICIAL_EDITORIAL_URL"]
