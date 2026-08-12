"""Fetch the official app catalog and annotate registry rows with it.

WHAT THIS IS. The catalog at ``apps.crew.kiro.dev`` is the list Kiro Crew
publishes, delivered as a document rather than baked into the wheel. The bundled
``app-registry.json`` answers the same question offline -- it is the seed -- so
both carry ``provenance: "official"``; see ``_apply_trust_fields``.

WHAT THIS IS NOT, YET. Three deliberate omissions, each a fail-closed gate here
rather than an ignored field, so the first time one matters it surfaces loudly
instead of degrading silently:

- **No signature verification.** The catalog is KMS-signed and a ``.sig`` sidecar
  is published beside it, but nothing here checks it. Trust is therefore TLS to
  our own domain -- the same basis on which the installer already fetches the
  CLI wheel, not a new low, but strictly less than the signature would give. The
  consequence is enforced below: a curated author does NOT mint the verified
  mark while this is true.
- **No tombstone resolution.** ``removed`` / ``reinstated`` carry withdrawal
  history with date precedence and a fail-closed tie rule. Implementing half of
  that would be worse than not implementing it, so a document carrying either
  list non-empty is REFUSED outright and the client falls back to the seed. A
  withdrawn app must never be rendered because we skipped the mechanism that
  withdraws it.
- **No new inventory.** Every entry annotates a row that already exists (a
  built-in discovered from disk, or a seed-index row). A published ``git`` source
  is pinned to a COMMIT, and the install path clones with ``--branch``, which a
  commit id is not -- so adding installable entries needs that path changed
  first. An entry matching nothing is ignored.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: Documented in the catalog repo's docs/distribution.md and printed by its
#: publish workflow. Trailing slash matters: refs are resolved against it.
OFFICIAL_CATALOG_BASE = "https://apps.crew.kiro.dev/"
OFFICIAL_CATALOG_URL = f"{OFFICIAL_CATALOG_BASE}official-registry.json"

#: The only schemaVersion this code understands. An unknown major is refused
#: rather than best-guessed: the document's meaning is what changed, and a
#: client that keeps reading the fields it recognises is a client that acts on a
#: contract it does not know.
SUPPORTED_SCHEMA_VERSION = 1

CACHE_TTL = 3600  # 1 hour
FETCH_TIMEOUT = 10
#: A catalog of a few dozen apps is tens of kilobytes. The cap is not about disk
#: but about not reading an unbounded body into memory from a host we do not
#: control at parse time.
MAX_BYTES = 4 * 1024 * 1024


def _cache_path() -> Path:
    return config_dir() / "cache" / "official-catalog.json"


def _read_cache() -> dict[str, Any] | None:
    path = _cache_path()
    try:
        if not path.is_file() or time.time() - path.stat().st_mtime > CACHE_TTL:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_cache(doc: dict[str, Any]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
    except OSError:
        logger.debug("could not cache the official catalog", exc_info=True)


def _https_request(url: str) -> urllib.request.Request:
    """Build the catalog GET, refusing any scheme but https.

    ``urllib`` honours ``file://``, so the scheme is asserted at the one place a
    URL turns into a fetch rather than trusted from whoever supplied it. Today
    the only caller passes a module constant; the guard is here so that stays
    true if a future caller passes something configurable, which is exactly the
    change that would otherwise turn this into a file read.
    """
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme != "https":
        raise ValueError(f"the catalog URL must be https, not {scheme!r}")
    return urllib.request.Request(
        url, method="GET", headers={"Accept": "application/json"}
    )


def _download() -> dict[str, Any] | None:
    """GET the catalog document. Returns None on any failure.

    Fetching the catalog is an enhancement to a store that already works from
    the seed index, so every failure here is a degradation rather than an error:
    the caller renders the seed.
    """
    try:
        req = _https_request(OFFICIAL_CATALOG_URL)
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            if resp.status != 200:
                logger.info("official catalog returned HTTP %s", resp.status)
                return None
            raw = resp.read(MAX_BYTES + 1)
    # `HTTPException` is the family, not a courtesy addition to the tuple. It is
    # NOT an OSError subclass, so `IncompleteRead` (a truncated chunked body),
    # `BadStatusLine`, `LineTooLong`, `InvalidURL`, `UnknownProtocol` and the
    # `ImproperConnectionState` pair all escape a tuple that lists only
    # URLError/OSError. `RemoteDisconnected` is the misleading one: it happens to
    # be caught because it also inherits `ConnectionResetError`, which is what
    # makes the gap look narrower than it is.
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
        ValueError,
    ) as exc:
        logger.info("could not fetch the official catalog: %s", exc)
        return None
    if len(raw) > MAX_BYTES:
        logger.warning("official catalog exceeds %d bytes; ignoring it", MAX_BYTES)
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("official catalog is not valid JSON: %s", exc)
        return None
    return doc if isinstance(doc, dict) else None


def load_official_catalog(fetcher: Any = None) -> list[dict[str, Any]]:
    """Return the catalog's entries, or an empty list if it is unusable.

    *fetcher* is injected by tests. Empty is always a safe answer: the store
    renders the seed index and the built-ins discovered from disk, which is what
    it did before this module existed.
    """
    doc = _read_cache()
    if doc is None:
        doc = (fetcher or _download)()
        if doc is not None:
            _write_cache(doc)
    if doc is None:
        return []

    version = doc.get("schemaVersion")
    # `type(...) is int` rather than `==` or `isinstance`: `1.0 == 1` and
    # `True == 1` are both true in Python, and `bool` is a subclass of `int`, so
    # either looser test accepts a document whose version field is not an
    # integer at all. A version we cannot identify exactly is a contract we do
    # not know, which is the whole reason this gate exists.
    if type(version) is not int or version != SUPPORTED_SCHEMA_VERSION:
        logger.warning(
            "official catalog declares schemaVersion %r, expected %r; ignoring it",
            version,
            SUPPORTED_SCHEMA_VERSION,
        )
        return []

    # Refuse rather than ignore: see the module docstring. A non-empty history
    # means an app has been withdrawn, and rendering the rest of the document
    # while skipping the withdrawal is the one outcome this catalog exists to
    # prevent.
    for key in ("removed", "reinstated"):
        if doc.get(key):
            logger.warning(
                "official catalog carries a non-empty %r list, which this client "
                "cannot yet resolve; ignoring the whole catalog so a withdrawn app "
                "is never rendered",
                key,
            )
            return []

    apps = doc.get("apps")
    if not isinstance(apps, list):
        logger.warning("official catalog has no usable apps list; ignoring it")
        return []
    return [a for a in apps if isinstance(a, dict) and isinstance(a.get("name"), str)]


#: Mirrors `ASSET_REF_PATTERN` in the catalog's publisher: a ref is a PATH, never
#: a URL. Checking for `"://"` is not enough -- `javascript:alert(1)` and
#: `data:image/svg+xml,…` carry no slashes after the colon, so a substring test
#: passes them and they get concatenated onto the base URL. Accepting exactly
#: what the publisher is allowed to emit is the useful symmetry.
_REF_RE = re.compile(
    r"^(?![a-zA-Z][a-zA-Z0-9+.-]*:)(?!//)(?!.*(?:^|/)\.\.(?:/|$))/?[A-Za-z0-9_./-]+$"
)


def _resolve_ref(ref: Any) -> str:
    """Turn a published asset ref into something a browser can load.

    An absolute ref is a built-in's client-local path and is already correct. A
    relative one is catalog-relative, so it resolves against the catalog base --
    NOT against the app's repository, which is the whole point of the catalog
    hosting the bytes. Anything that is not a plain path is dropped: the ref
    contract forbids a URL, and honouring one would let the document choose a
    host for an ``<img>``.
    """
    if not isinstance(ref, str) or not _REF_RE.match(ref):
        return ""
    if ref.startswith("/"):
        return ref
    return OFFICIAL_CATALOG_BASE + ref


def _curated_str(value: Any) -> str:
    """A curated display string, or ``""`` for anything that is not one.

    Every field below arrives from a document fetched over the network, so its
    TYPE is as untrusted as its content. A wrong type is not hypothetical
    tidiness: ``{"tags": 5}`` used to reach ``list(5)`` and turn one malformed
    entry into an HTTP 500 for the whole store, and a non-string
    ``displayName`` reaches the browser to be sorted and lowercased there.
    Returning ``""`` collapses both into the falsy case the callers already
    handle, so a bad field degrades that field and nothing else.
    """
    return value if isinstance(value, str) else ""


def _curated_tags(value: Any) -> list[str]:
    """The curated tag list, keeping only the string members.

    A ``list`` is required rather than any iterable: a bare ``str`` is iterable
    and would silently become one tag per character.
    """
    if not isinstance(value, list):
        return []
    return [t for t in value if isinstance(t, str)]


def annotate(rows: list[dict[str, Any]], entries: list[dict[str, Any]]) -> None:
    """Overlay curated fields from *entries* onto matching *rows*, in place.

    Curated values WIN over the manifest's, because that is what curation means:
    this document is ours, the manifest belongs to the app. `summary` lands on
    `description` -- it is the one-line list copy the store row renders.

    The curated author is applied for DISPLAY only. It deliberately does not
    reach ``_index_author``, which is what ``_apply_trust_fields`` derives the
    verified mark from: with signature verification not yet implemented, the
    catalog is trusted only as far as TLS, and TLS to a CDN is not evidence for
    a first-party badge. Wiring the author into the badge is the natural next
    step AFTER the signature is checked, not before.

    Every value is type-guarded on the way in. This runs inside the
    ``GET /api/apps/registry`` handler with no try/except above it, so a raise
    here is a 500 for the entire store -- the opposite of this module's promise
    that a bad catalog degrades to the seed.
    """
    by_name = {e["name"]: e for e in entries}
    for row in rows:
        entry = by_name.get(row.get("name"))
        if entry is None:
            continue
        if display := _curated_str(entry.get("displayName")):
            row["displayName"] = display
        if summary := _curated_str(entry.get("summary")):
            row["description"] = summary
        if version := _curated_str(entry.get("version")):
            row["version"] = version
        if tags := _curated_tags(entry.get("tags")):
            row["tags"] = tags
        author = entry.get("author")
        if isinstance(author, dict) and (name := _curated_str(author.get("name"))):
            row["author"] = name
        if icon := _resolve_ref(entry.get("iconRef")):
            row["iconUrl"] = icon
        if dark := _resolve_ref(entry.get("iconRefDark")):
            row["iconUrlDark"] = dark
        if hero := _resolve_ref(entry.get("heroRef")):
            row["heroImage"] = hero
