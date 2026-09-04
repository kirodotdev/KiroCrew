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
- **Display-only inventory.** ``list_catalog_rows`` maps the published list's
  DISPLAY fields (identity, name, summary, version, tags, author, asset refs)
  into storefront rows. It emits no clone coordinates and no ``origin``, because
  the catalog is trusted only as far as TLS, and a non-builtin row never mints
  the verified badge. Install coordinates come from ``inventory``, materialised
  ONLY from a fresh fetch (``fetch_inventory_entries``) and never from the cache,
  so ``list_catalog_apps`` keeps a ``git`` row that either the seed, an external
  registry, or the catalog's own pins can install.
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

from kiro_crew.apps.manifest import KEBAB_RE, app_name_error
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: Documented in the KiroCrewApps repo's docs/distribution.md and printed by
#: its publish workflow. Trailing slash matters: refs are resolved against it.
OFFICIAL_CATALOG_BASE = "https://apps.crew.kiro.dev/"
OFFICIAL_CATALOG_URL = f"{OFFICIAL_CATALOG_BASE}official-registry.json"

#: The only schemaVersion this code understands. An unknown major is refused
#: rather than best-guessed: the document's meaning is what changed, and a
#: client that keeps reading the fields it recognises is a client that acts on a
#: contract it does not know.
SUPPORTED_SCHEMA_VERSION = 1

CACHE_TTL = 3600  # 1 hour
#: How long a FAILED fetch is remembered. Without this, an outage at
#: ``apps.crew.kiro.dev`` costs every ``GET /api/apps/registry`` a fresh attempt
#: and up to ``FETCH_TIMEOUT`` seconds of wall clock, for as long as the outage
#: lasts -- so the store's own page load inherits the CDN's downtime, which is
#: not the "degrade to the seed" this module promises. Much shorter than the
#: success TTL on purpose: the cost of forgetting too early is one retry, and the
#: cost of remembering too long is a store that stays stale after the CDN is back.
FAILURE_TTL = 60
FETCH_TIMEOUT = 10
#: A catalog of a few dozen apps is tens of kilobytes. The cap is not about disk
#: but about not reading an unbounded body into memory from a host we do not
#: control at parse time.
MAX_BYTES = 4 * 1024 * 1024


def _cache_path() -> Path:
    return config_dir() / "cache" / "official-catalog.json"


#: Marks a cache entry as "the fetch failed recently", distinguishable from a
#: real document because no catalog document carries this key.
_FAILED_KEY = "_fetchFailedAt"


def _read_cache() -> dict[str, Any] | None:
    """Return the cached document, or None when there is nothing usable.

    A cached FAILURE returns the sentinel rather than None, because the caller
    has to tell "no cache, go fetch" apart from "the fetch failed a moment ago,
    do not hammer it".
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
        logger.debug("could not cache the official catalog", exc_info=True)


def _write_failure() -> None:
    """Remember that the fetch just failed, so the next caller does not wait again."""
    _write_cache({_FAILED_KEY: time.time()})


def forget_cache() -> None:
    """Drop the cached document AND any failure memory, so the next read re-fetches.

    The server half of the store's manual refresh: deleting the file is the one
    operation that clears both a stale document (``CACHE_TTL`` not yet expired)
    and a ``_fetchFailedAt`` sentinel (``FAILURE_TTL`` back-off) in a single
    step. It never fetches anything itself -- the caller's next read pays the
    fetch, so a refresh that cannot reach the CDN degrades exactly like a cold
    start rather than inventing a third failure mode.
    """
    try:
        _cache_path().unlink(missing_ok=True)
    except OSError:
        logger.debug("could not drop the official catalog cache", exc_info=True)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect instead of quietly changing origin.

    `urlopen` follows 3xx automatically, and `HTTPRedirectHandler` permits
    `http` as a target — so a `302` to `http://127.0.0.1/...` is followed, and
    the gateway makes an unauthenticated request to a service on the user's own
    machine on behalf of a remote document. The scheme guard in
    `_https_request` cannot see that: it validates the URL we ASK for, not the
    one we end up at.

    This module's entire trust basis is "TLS to our own domain". A redirect off
    that domain contradicts the premise whether it is hostile or a CDN
    misconfiguration, so a 3xx is a fetch failure and the caller renders the
    seed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(
            req.full_url, code, f"refusing to follow a redirect to {newurl!r}", headers, fp
        )


def _open_catalog(req: urllib.request.Request) -> Any:
    """Open *req* with redirects refused. THE seam the fetch goes through.

    A named function rather than an inline `urlopen`, because tests must be able
    to intercept the network at a place that cannot drift: patching
    `urllib.request.urlopen` used to work here, and when this function started
    using an opener instead, those tests silently stopped intercepting anything
    and began making real requests to the live CDN. A seam that belongs to this
    module cannot be bypassed by changing how this module calls out.

    The opener is built per call: an opener is mutable shared state, and one
    built at import time is something any other import can reach in and
    reconfigure.
    """
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    return urllib.request.build_opener(_NoRedirects).open(req, timeout=FETCH_TIMEOUT)


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
    return urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})


def fetch_document(url: str) -> dict[str, Any] | None:
    """GET one JSON document from the catalog origin. Returns None on any failure.

    Takes the URL because a second document (``editorial.json``) is served from
    the same origin under the same trust basis, and the guards below -- https
    only, redirects refused, a byte cap before decode, and the exception family
    that a narrower tuple lets escape -- are security behaviour that must not
    drift into a second copy. The scheme guard is what keeps this general form
    from becoming a file read if a caller ever passes something configurable.

    Every failure is a degradation rather than an error: each caller has a
    working answer without the document.
    """
    try:
        req = _https_request(url)
        with _open_catalog(req) as resp:
            if resp.status != 200:
                logger.info("%s returned HTTP %s", url, resp.status)
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
        logger.info("could not fetch %s: %s", url, exc)
        return None
    if len(raw) > MAX_BYTES:
        logger.warning("%s exceeds %d bytes; ignoring it", url, MAX_BYTES)
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("%s is not valid JSON: %s", url, exc)
        return None
    return doc if isinstance(doc, dict) else None


def _download() -> dict[str, Any] | None:
    """Fetch THIS module's document. The registry's own call into the seam."""
    return fetch_document(OFFICIAL_CATALOG_URL)


def load_official_catalog(fetcher: Any = None) -> list[dict[str, Any]]:
    """Return the catalog's entries, or an empty list if it is unusable.

    *fetcher* is injected by tests. Empty is always a safe answer: the store
    renders the seed index and the built-ins discovered from disk, which is what
    it did before this module existed.
    """
    doc = _read_cache()
    if doc is not None and _FAILED_KEY in doc:
        # A recent fetch failed. Answer from the seed WITHOUT another attempt --
        # the point of remembering the failure is not paying its timeout again.
        return []
    if doc is None:
        doc = (fetcher or _download)()
        if doc is None:
            _write_failure()
        else:
            _write_cache(doc)
    if doc is None:
        return []

    # One rulebook, two reporting styles: this reader logs and degrades to `[]` so the
    # store still renders, while `fetch_inventory_entries` raises so a caller cannot
    # mistake a bad envelope for "no such app". What makes an envelope bad is stated once,
    # in `_envelope_error` -- spelled twice, a schema bump or a new tombstone key updated
    # here would silently weaken the other reader.
    problem = _envelope_error(doc)
    if problem is not None:
        logger.warning("ignoring the official catalog: %s", problem)
        return []
    return [a for a in doc["apps"] if isinstance(a, dict) and isinstance(a.get("name"), str)]


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


#: The largest integer JavaScript can represent exactly (2**53 - 1). A star
#: count above this is not a plausible count, and forwarding one lets a
#: hostile document render hundreds of digits into the store's layout.
_STARS_MAX = 9_007_199_254_740_991


def _curated_stars(entry: dict[str, Any]) -> int | None:
    """The entry's baked ``stargazersCount``, or ``None`` when absent/invalid.

    The publisher generates this field for git-source entries only, so a
    non-git entry never yields one here regardless of what the document says.
    ``bool`` is excluded explicitly (it subclasses ``int``), and the value is
    bounded to the JS safe-integer range: the document arrived over the
    network, so its types and magnitudes are as untrusted as its content.
    Absence means "unknown", never zero.
    """
    source = entry.get("source")
    if not isinstance(source, dict) or source.get("type") != "git":
        return None
    stars = entry.get("stargazersCount")
    if isinstance(stars, bool) or not isinstance(stars, int):
        return None
    if stars < 0 or stars > _STARS_MAX:
        return None
    return stars


#: A git object name as the published document must spell it: sha1 (40 hex) or
#: sha256 (64 hex). Defined here rather than imported from ``registry`` because
#: that module imports THIS one; the duplication is one regex against a circular
#: import.
#:
#: Every pattern in this group ends at ``\Z``, not ``$``. Python's ``$`` matches
#: before a trailing newline, so ``$`` here accepted a 40-hex pin with ``"\n"``
#: appended -- a value that then reaches a ``git`` argument vector. Interior line
#: breaks are caught by the character classes; only the trailing one needs the
#: stricter anchor, which is exactly the case an interior-newline test misses.
#: One slug segment, which is what an app name has to be before it can become a
#: directory. The name reaches ``app_source_dir(name)`` and the persistent app
#: data root, so a document-supplied ``"../../x"`` or an absolute value would
#: escape into paths this client owns. The url, commit and subdir were validated
#: from the start; the name reaching a filesystem path was the gap.
_MAX_NAME_LEN = 64

_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

#: An https clone URL with no room for a control character, whitespace, or a
#: leading ``-``. The scheme restriction is the load-bearing part: ``ext::``
#: hands a command line to a shell and ``file://`` reads local paths, and this
#: value reaches ``git`` on the user's machine.
_CLONE_URL_RE = re.compile(r"^https://[^\s\x00-\x1f\x7f]+\Z")

#: A contained relative path inside the app's repository. No leading slash, no
#: ``..`` segment, no backslash: the value is joined to a clone root, and the
#: join is re-checked there -- this is the first of the two gates, not the only
#: one.
_SUBDIR_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|\Z))[A-Za-z0-9_./-]+\Z")


def _coordinates(source: Any) -> dict[str, str] | None:
    """Validated fetch coordinates for a ``git`` source, or None to skip the row.

    This is the FIRST place the document's ``source`` block is examined at all:
    ``load_official_catalog`` checks the envelope and each entry's ``name``, and
    nothing below it looked inside ``source``. So every field is validated here
    for TYPE as well as shape -- the document arrives over the network, and a
    coordinate that reaches ``git`` unchecked is an argument-injection surface,
    not a cosmetic problem.

    A row that fails any check is DROPPED rather than repaired. There is no safe
    repair for a coordinate: guessing a branch when the pin is malformed would
    install bytes nobody attested, which is the one outcome the pin exists to
    prevent.
    """
    if not isinstance(source, dict) or source.get("type") != "git":
        return None
    url = source.get("url")
    commit = source.get("ref")
    subdir = source.get("subdir", "")
    if not isinstance(url, str) or not _CLONE_URL_RE.match(url):
        return None
    if not isinstance(commit, str) or not _COMMIT_RE.match(commit):
        return None
    if subdir not in ("", None):
        if not isinstance(subdir, str) or not _SUBDIR_RE.match(subdir):
            return None
    return {"url": url, "commit": commit, "subdirectory": subdir or ""}


def inventory(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Materialise installable rows from the catalog's ``git`` entries.

    This is what makes the catalog the SHELF rather than a decoration on one:
    :func:`annotate` can only change a row that already exists, so an app the
    catalog lists but the bundled seed does not was previously unlistable and
    therefore uninstallable -- adding one required shipping a release.

    ``builtin`` entries produce nothing here on purpose. Their code ships in the
    wheel and is discovered from disk; a row for code this client does not have
    would render an install control that cannot work.

    Two fields are deliberately ABSENT from the rows this returns:

    - ``branch``, because these rows are pinned. Emitting one would let the
      install path silently fall back to a branch tip (``entry.get("branch",
      "main")``) and succeed, recording the tip's commit as provenance while the
      pin did nothing -- the one failure mode here that is both quiet and
      "successful". Callers assert on ``commit`` instead.
    - ``_index_author``, so these rows do NOT mint the verified badge. The
      catalog's signature is not checked yet, and the same restraint already
      governs :func:`annotate`'s curated author.

    ``version`` IS set, and it is the point: the store owns update availability.
    A developer pushing to their repository is not an approved update; a
    republished catalog entry is. This is also what lets the caller skip the
    per-app manifest clone, since the row already carries everything the list
    renders.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        # `app_name_error` is the project's SINGLE app-name contract -- its own
        # docstring says every path admitting an app funnels through it so a name
        # refused at one door cannot enter by another. A local regex here was exactly
        # that other door: it accepted reserved names like `system` (which shadows a
        # notification channel namespace) and Windows device names, and those reach
        # the build before any canonical check runs.
        if app_name_error(name) is not None:
            logger.warning("dropping catalog entry with inadmissible name %r", name)
            continue
        if len(name) > _MAX_NAME_LEN:
            # NOT a competing identity rule -- `app_name_error` owns admissibility,
            # and it does not bound length. This is a bound on the name AS A DIRECTORY
            # COMPONENT, kept local because raising it into the shared contract would
            # change every admission path in the product, which this change is not
            # scoped to do.
            logger.warning("dropping catalog entry with an over-long name %r", name)
            continue
        if name in seen:
            # Two rows with one name make identity ambiguous: consent is shown for
            # whichever row the listing rendered, while a later name-only lookup can
            # resolve the other one -- a different repository under a granted name.
            logger.warning("dropping duplicate catalog entry for %r", name)
            continue
        coords = _coordinates(entry.get("source"))
        if coords is None:
            continue
        row: dict[str, Any] = {
            "name": name,
            "gitUrl": coords["url"],
            # `repo` is the legacy alias `_entry_git_url` falls back to, and
            # `_pinned_registry_entry` matches an installed app's recorded
            # sourceUrl against it. Both names carry one value.
            "repo": coords["url"],
            "commit": coords["commit"],
            "_catalog": True,
        }
        if coords["subdirectory"]:
            row["subdirectory"] = coords["subdirectory"]
        if version := _curated_str(entry.get("version")):
            row["version"] = version
        if display := _curated_str(entry.get("displayName")):
            row["displayName"] = display
        if summary := _curated_str(entry.get("summary")):
            row["description"] = summary
        if tags := _curated_tags(entry.get("tags")):
            row["tags"] = tags
        # `author` is deliberately NOT set here even though the catalog states one.
        # `list_registry` snapshots `_index_author = entry["author"]`
        # unconditionally, and `_apply_trust_fields` derives the first-party
        # `verified` badge from that snapshot -- so an author on this row is a path
        # from an unsigned document to the badge. :func:`annotate` sets it for
        # DISPLAY after the snapshot has already been taken, which is the whole
        # reason that ordering exists.
        if icon := _resolve_ref(entry.get("iconRef")):
            row["iconUrl"] = icon
        if dark := _resolve_ref(entry.get("iconRefDark")):
            row["iconUrlDark"] = dark
        if hero := _resolve_ref(entry.get("heroRef")):
            row["heroImage"] = hero
        if (stars := _curated_stars(entry)) is not None:
            row["stargazersCount"] = stars
        seen.add(name)
        rows.append(row)
    return rows


def _envelope_error(doc: Any) -> str | None:
    """Why *doc* is not a usable catalog envelope, or None when it is.

    ONE copy of the rules, because this document's two readers disagree only about how to
    REPORT a bad envelope, never about what makes one bad. Spelled twice, a schema bump or
    a new tombstone key updated in one place silently weakened the other.

    ``type(...) is int`` rather than ``==`` or ``isinstance``: ``1.0 == 1`` and
    ``True == 1`` are both true in Python and ``bool`` subclasses ``int``, so either
    looser test accepts a document whose version field is not an integer at all.
    """
    if not isinstance(doc, dict):
        return "the official catalog could not be fetched or is not a JSON object"
    version = doc.get("schemaVersion")
    if type(version) is not int or version != SUPPORTED_SCHEMA_VERSION:
        return f"unsupported catalog schemaVersion: {version!r}"
    for key in ("removed", "reinstated"):
        if doc.get(key):
            # A withdrawn app must never render, and this client cannot resolve the
            # tombstone lists, so the whole document is refused rather than partly used.
            return f"catalog carries a whole-document {key!r} marker"
    if not isinstance(doc.get("apps"), list):
        return "catalog document has no usable 'apps' list"
    return None


class CatalogUnavailable(Exception):
    """The catalog could not be consulted, so absence cannot be asserted.

    Exists because ``None`` and "I could not ask" are answers a caller must be able
    to tell apart. While every failure returned ``None``, a CDN outage was
    indistinguishable from "this app is not in the catalog" -- and the install path
    reads the latter as permission to use the unpinned bundled coordinates, so a
    network failure silently downgraded a pinned app to a branch tip.
    """


def fetch_inventory_entries() -> list[dict[str, Any]]:
    """The catalog's entries from a FRESH HTTPS fetch, never the on-disk cache.

    Raises :class:`CatalogUnavailable` when the document cannot be fetched or
    interpreted, so a caller cannot mistake "could not ask" for "nothing there".

    This is the only source allowed to MATERIALISE inventory. The cache under the
    data home is agent-writable, which is harmless while it supplies display copy
    for rows that exist anyway (:func:`annotate` skips rows carrying ``_registry``,
    so it cannot even re-dress an external row). It is NOT harmless once a cached
    row can CREATE a listed row: a planted name would render with official
    provenance and deduplicate the real same-named external row out of the listing,
    so the consent prompt would describe an official app while the name grant it
    produces installs the external one. App trust is keyed by name, so the display
    the consent decision is made on is part of the trust boundary.
    """
    # Respect the module's failure memory before paying another timeout.
    #
    # `load_official_catalog` remembers a failed fetch precisely so the next caller does
    # not wait again, and skipping that made every store listing during a CDN outage pay
    # a full HTTPS timeout. Reading it here is safe in a way that reading cached ENTRIES
    # would not be: the memory can only make this function refuse EARLIER, never make it
    # answer. The file is agent-writable, so an attacker can clear the memory -- and the
    # worst that buys them is one real fetch attempt, which is the same fail-closed
    # answer by a slower route.
    cached = _read_cache()
    if isinstance(cached, dict) and _FAILED_KEY in cached:
        raise CatalogUnavailable("a recent catalog fetch failed; not retrying yet")

    doc = fetch_document(OFFICIAL_CATALOG_URL)
    if not isinstance(doc, dict):
        _write_failure()
    problem = _envelope_error(doc)
    if problem is not None:
        raise CatalogUnavailable(problem)
    assert isinstance(doc, dict)  # narrowed by _envelope_error
    return [a for a in doc["apps"] if isinstance(a, dict)]


def inventory_for_install(name: str) -> dict[str, Any] | None:
    """The row named *name*, resolved WITHOUT reading the on-disk cache.

    The cache under the data home is not a sensitive path, so the agent's own file
    tools can write it (verified against ``security.is_sensitive_write_path``). That
    is harmless while the cache only supplies display copy, which is all
    :func:`annotate` consumes. It is NOT harmless once a row supplies install
    coordinates: an attacker who can write that file chooses which repository a name
    the owner has already permitted for execution installs from, and app trust is
    keyed by NAME. Reading coordinates from an agent-writable file would make the
    name-to-URL binding attacker-controlled.

    So the install path re-fetches the document over HTTPS and ignores the cache
    entirely. Someone able to write a local file cannot answer for
    ``apps.crew.kiro.dev``, and TLS to our own domain is the trust basis this module
    already documents. This is NOT a substitute for verifying the ``.sig`` sidecar --
    that would also close the case where the CDN itself is wrong -- but it removes
    the local surface, which is the one this client creates for itself.

    Returns None ONLY when a valid catalog document does not name *name* -- an
    authoritative absence. Every other outcome raises :class:`CatalogUnavailable`,
    because a caller that cannot tell "not in the catalog" from "could not reach the
    catalog" will treat a network failure as permission to install from unpinned
    coordinates.
    """
    apps = fetch_inventory_entries()
    rows = list(inventory(apps))
    for row in rows:
        if row.get("name") == name:
            return row
    if any(a.get("name") == name for a in apps):
        # The catalog DOES name this app, but `inventory` dropped it -- a builtin
        # (no git coordinates) or a row whose `source` failed validation. Reporting
        # absence here would let a caller fall back to unpinned coordinates for an
        # app the catalog is actually curating.
        raise CatalogUnavailable(
            f"catalog names {name!r} but supplies no usable install coordinates"
        )
    return None


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

    ``version`` is deliberately NOT overlaid. It looks like a display field and
    is not one: ``annotate`` runs BEFORE ``_enrich_with_install_status``, so a
    version written here reaches ``_version_newer`` and decides whether a row
    shows an update badge. A curated value would then be able to fabricate an
    update the app never published, or mask one it did, and it would couple this
    document to every app's release cadence forever. Version is a fact about the
    app; the manifest is where it comes from.
    """
    by_name = {e["name"]: e for e in entries}
    for row in rows:
        # Curated copy is for rows the catalog is actually talking about. Matching
        # on name alone would let an app from a user-added registry that squats
        # the name of a not-yet-seeded official app inherit that app's curated
        # description, author and CDN-hosted icon -- impersonation assembled out
        # of our own document. `_apply_trust_fields` already refuses these rows
        # the verified mark, but it runs AFTER this overlay, so the copy and the
        # icon would land regardless.
        if row.get("_registry"):
            continue
        if row.get("_catalog"):
            # Already materialised FROM this document by `inventory`, so overlaying it
            # again can only change the row if two sources disagree -- and a second
            # source for one row's identity is the thing to avoid, not a feature.
            continue
        entry = by_name.get(row.get("name"))
        if entry is None:
            continue
        if display := _curated_str(entry.get("displayName")):
            row["displayName"] = display
        if summary := _curated_str(entry.get("summary")):
            row["description"] = summary
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
        # ``stargazersCount`` is deliberately NOT overlaid here. This function
        # matches rows by NAME, and a same-name SEED row can pin a DIFFERENT
        # repository than the catalog entry (seed collisions keep the pin by
        # design) — overlaying the catalog repository's star count onto a row
        # that installs another repository would forge a trust cue. Mismatched
        # display copy is survivable; a popularity number beside Install is
        # not. The count therefore flows through ``inventory()`` alone, where
        # a row's identity and its count come from the same catalog entry.


def list_catalog_rows() -> list[dict[str, Any]]:
    """Build display rows straight from the published catalog's entries.

    This is the JSON-only storefront path: the published document IS the list,
    so its curated display fields ARE the copy the store renders, and there is
    no per-app ``app.json`` to prefer over them. Rows carry identity and display
    fields ONLY — no clone coordinates and no ``origin`` — because the catalog
    is trusted only as far as TLS, so it must not supply install coordinates or
    a first-party provenance claim. Install status and trust are stamped later by
    ``registry.py`` from the installed app, never from this document.

    Returns ``[]`` when the catalog is unavailable, which is the caller's signal
    to fall back to the seed listing offline. Every field is type-guarded on the
    way in for the same reason as ``annotate``: the document arrived over the
    network, so its types are as untrusted as its content. A name that is not
    kebab-case is dropped, because a name becomes a filesystem path on install.
    """
    rows: list[dict[str, Any]] = []
    for entry in load_official_catalog():
        name = entry.get("name")
        if not isinstance(name, str) or not KEBAB_RE.fullmatch(name):
            continue
        row: dict[str, Any] = {"name": name}
        if display := _curated_str(entry.get("displayName")):
            row["displayName"] = display
        if summary := _curated_str(entry.get("summary")):
            row["description"] = summary
        if version := _curated_str(entry.get("version")):
            row["version"] = version
        if tags := _curated_tags(entry.get("tags")):
            row["tags"] = tags
        author = entry.get("author")
        if isinstance(author, dict) and (author_name := _curated_str(author.get("name"))):
            row["author"] = author_name
        if icon := _resolve_ref(entry.get("iconRef")):
            row["iconUrl"] = icon
        if dark := _resolve_ref(entry.get("iconRefDark")):
            row["iconUrlDark"] = dark
        if hero := _resolve_ref(entry.get("heroRef")):
            row["heroImage"] = hero
        source = entry.get("source")
        if isinstance(source, dict):
            # The source TYPE is a display marker (builtin vs git), not install
            # coordinates: url/ref stay out, so the catalog cannot name a clone
            # target. ``registry.list_catalog_apps`` uses it to gate git rows on
            # the installable set: the seed's names plus the catalog's own pins.
            stype = _curated_str(source.get("type"))
            if stype in ("builtin", "git"):
                row["source"] = {"type": stype}
        # ``stargazersCount`` is deliberately NOT projected here: this function
        # reads ``load_official_catalog()``, which serves the agent-writable
        # CACHE — display copy degrading from a poisoned cache is survivable,
        # but a trust cue rendered beside Install is not, so the count flows
        # only through the projections fed by a fresh TLS fetch
        # (``inventory``/``annotate`` via ``fetch_inventory_entries``).
        rows.append(row)
    return rows
