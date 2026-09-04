"""The editorial document: the featured list the curator controls without a release.

WHAT THIS IS. ``editorial.json`` is published beside ``official-registry.json``
and carries the featured sections above the Discover rail -- which apps are
promoted, and under what framing. The registry says what exists; this says what is
put forward. Keeping them apart is what lets a curator re-cut the featured list
without touching the list of apps, and lets the client refuse one document while
still rendering the other.

A section is either an ``app`` (one featured app) or a ``collection`` (several
under a curator's theme); any other ``type`` is skipped, which is what lets a new
shape publish before every client can draw it.

The rail's ORDER is not here. It is a separate document with a separate schema
gate -- see ``official_category_order`` for why that separation is load-bearing
rather than tidiness.

WHAT THIS DOES NOT DO, YET.

- **No signature verification.** Same basis as the registry: TLS to our own
  domain. Presentation is a lower-stakes payload than inventory -- the worst a
  hostile document achieves here is a re-cut featured list -- but the omission is
  named here rather than left for a reader to infer from silence.

Every failure degrades to the derived featured pick, which is what Discover shows
when this document carries no sections.
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
    _resolve_ref,
    fetch_document,
)
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: This document's own supported schema version -- deliberately NOT the shared
#: constant from ``official_catalog``, for the same reason
#: ``official_category_order`` owns its own: per-document version gates are the
#: point of the document split, and one shared constant would make a registry
#: v2 bump start refusing editorial documents still published at v1.
SUPPORTED_SCHEMA_VERSION = 1

OFFICIAL_EDITORIAL_URL = f"{OFFICIAL_CATALOG_BASE}editorial.json"

#: Same TTLs as the registry: the two documents are published together by one
#: workflow run, so caching them for different lengths would show a rail ordered
#: by one revision beside apps listed by another.
CACHE_TTL = 3600
FAILURE_TTL = 60

#: `sections` is capped at 40 by the schema and a collection's `appRefs` at 6, so a
#: document cannot make Discover unbounded. The 6 is small on purpose -- every
#: member of a collection is rendered, with no detail page to hold an overflow, so
#: the SCHEMA refuses more than the card can draw. Here the same number is a
#: truncation instead, because this reader also sees documents that never passed
#: that gate (a stale cache, a hand-edited file) and rendering the first 6 beats
#: refusing the card outright.
MAX_SECTIONS = 40
MAX_APP_REFS = 6
#: A one-app collection is an `app` section wearing a costume, so the schema
#: refuses it. Kept here too because this reader also sees documents that never
#: passed that gate -- a stale cache, or a hand-edited file.
MIN_COLLECTION_APPS = 2

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
        logger.debug("could not cache the editorial document", exc_info=True)


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
        logger.debug("could not drop the editorial cache", exc_info=True)


def _download() -> dict[str, Any] | None:
    """GET the editorial document through the registry module's fetch seam.

    Deliberately NOT a second copy of the fetch: the https-only guard, the
    refuse-redirects opener, the byte cap and the exception family are security
    behaviour that must not drift between two documents served from one origin.
    """
    return fetch_document(OFFICIAL_EDITORIAL_URL)


def _load_document(fetcher: Any = None) -> dict[str, Any] | None:
    """Fetch-or-cache the editorial document, applying the schema gate."""
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
    # `True == 1` both hold in Python and `bool` subclasses `int`, so either
    # looser test accepts a document whose version is not an integer and acts on
    # a contract it cannot name.
    if type(version) is not int or version != SUPPORTED_SCHEMA_VERSION:
        logger.warning(
            "editorial document declares schemaVersion %r, expected %r; ignoring it",
            version,
            SUPPORTED_SCHEMA_VERSION,
        )
        return None
    return doc


def _artwork(raw: Any) -> dict[str, str] | None:
    """Project a section's artwork onto browser-loadable URLs, or None.

    `_resolve_ref` is the registry module's guard, reused rather than reimplemented:
    it accepts exactly what the publisher is allowed to emit, so a ref carrying
    `javascript:` or `data:` -- neither of which has a slash after the colon, and
    so both of which pass a naive `"://" in ref` test -- is dropped instead of
    being concatenated onto the catalog base and handed to an `<img>`.

    The light variant is load-bearing: artwork that resolves only in dark is
    dropped entirely, because a section rendering nothing on the default
    appearance is worse than one rendering without art.
    """
    if not isinstance(raw, dict):
        return None
    light = _resolve_ref(raw.get("ref"))
    if not light:
        return None
    out = {"url": light}
    if dark := _resolve_ref(raw.get("refDark")):
        out["urlDark"] = dark
    alt = raw.get("alt")
    if isinstance(alt, str) and alt.strip():
        out["alt"] = alt.strip()[:200]
    return out


def _text(raw: dict[str, Any], key: str, limit: int) -> str | None:
    """Return a trimmed, capped string field, or None when it carries nothing."""
    value = raw.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()[:limit]
    return None


def _app_section(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Project an `app` section -- one featured app -- or None.

    Carries no title: the app's own name is the heading, so a published `title`
    is a document that means `collection` and is ignored rather than honoured.
    """
    ref = raw.get("appRef")
    if not isinstance(ref, str) or not ref.strip():
        return None

    out: dict[str, Any] = {"type": "app", "appRefs": [ref.strip()]}
    if blurb := _text(raw, "blurb", 200):
        out["blurb"] = blurb
    if art := _artwork(raw.get("artwork")):
        out["artwork"] = art
    return out


def _collection_section(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Project a `collection` section -- several apps under a theme -- or None.

    The title is load-bearing rather than decorative: it is the only thing that
    explains why unrelated apps share a card, so a collection without one is
    dropped instead of rendered as an anonymous pile. The publish gate refuses
    that document, which makes this the defence against a hand-edited or
    truncated one.

    Fewer than two resolvable refs is also dropped rather than demoted to an
    `app` card: a two-app collection that lost one member is a curation problem,
    and silently showing the survivor under the group's theme would state
    something the curator did not.
    """
    refs = raw.get("appRefs")
    if not isinstance(refs, list):
        return None
    names = [r.strip() for r in refs if isinstance(r, str) and r.strip()]
    # Preserve the curator's order while dropping a repeat; the publish gate
    # rejects duplicates, so this only fires on a document that bypassed it.
    unique = list(dict.fromkeys(names))[:MAX_APP_REFS]
    if len(unique) < MIN_COLLECTION_APPS:
        return None

    title = _text(raw, "title", 60)
    if not title:
        return None

    out: dict[str, Any] = {"type": "collection", "appRefs": unique, "title": title}
    if blurb := _text(raw, "blurb", 200):
        out["blurb"] = blurb
    if art := _artwork(raw.get("artwork")):
        out["artwork"] = art
    return out


_SECTION_READERS = {"app": _app_section, "collection": _collection_section}


#: The forms this client can draw. `carousel` is published-schema-legal but has
#: no renderer here yet, so it deliberately takes the unknown-form path below
#: and the whole block stays invisible until support ships.
_KNOWN_FORMS = frozenset({"full", "row"})

#: Each form's floor and ceiling on `items`, re-applied at this boundary: this
#: reader also sees documents that never passed the publish gate (a stale cache,
#: a hand-edited file). A `full` holds exactly one; a `row` of one would render
#: a half-width card against empty space, so it needs two.
_FORM_ITEM_BOUNDS = {"full": (1, 1), "row": (2, MAX_APP_REFS)}


def load_sections(fetcher: Any = None) -> list[dict[str, Any]]:
    """Return the published editorial blocks, or ``[]`` for none.

    Each entry is one BLOCK of the Discover page: a ``form`` saying how its
    contents are arranged (``full``: one card across the width; ``row``: cards
    side by side) plus the ``items`` it arranges. Items keep the two projected
    types, `app` and `collection`, each with ``appRefs`` as a list so the caller
    resolves references one way regardless of type.

    TWO GRAINS OF SKIP, and the difference is the contract. An unknown ``form``
    skips the WHOLE block -- the arrangement is what a form names, and a block
    whose arrangement this client cannot draw has no partial rendering that is
    not a guess. An unknown item ``type`` skips just that card, because the
    arrangement can still be drawn around a card it does not know. Both are what
    let a new shape publish before every client can render it.

    A block that falls below its form's floor after item projection is dropped
    whole: a row whose second card dissolved is an arrangement the curator did
    not write.

    Empty is always a safe answer: Discover falls back to picking featured apps
    out of the registry, which is what it did before this module existed.
    """
    doc = _load_document(fetcher)
    if doc is None:
        return []

    raw = doc.get("sections")
    if not isinstance(raw, list):
        return []

    out: list[dict[str, Any]] = []
    skipped = 0
    for block in raw:
        # The cap counts what RENDERS, not what was read: applying it to the raw
        # list would let a run of unsupported blocks consume the whole budget
        # and starve a supported one sitting behind them.
        if len(out) >= MAX_SECTIONS:
            break
        if not isinstance(block, dict):
            skipped += 1
            continue
        form = block.get("form")
        if not isinstance(form, str) or form not in _KNOWN_FORMS:
            skipped += 1
            continue
        raw_items = block.get("items")
        if not isinstance(raw_items, list):
            skipped += 1
            continue
        items: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            reader = _SECTION_READERS.get(kind) if isinstance(kind, str) else None
            if reader is None:
                continue
            if projected := reader(item):
                items.append(projected)
        lo, hi = _FORM_ITEM_BOUNDS[form]
        if not lo <= len(items) <= hi:
            skipped += 1
            continue
        out.append({"form": form, "items": items})

    if skipped:
        # Without this line an old-shape document and a curator who published
        # nothing are indistinguishable: both yield [] and both render the
        # derived layout, for up to a full cache TTL, with nothing recording why.
        logger.debug(
            "editorial: %d of %d block(s) skipped; %d projected",
            skipped,
            len(raw),
            len(out),
        )
    return out


__all__ = ["forget_cache", "load_sections", "MAX_BYTES", "OFFICIAL_EDITORIAL_URL"]
