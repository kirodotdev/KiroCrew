"""Kiro Space stores as App Store sources: the provider seam and what core keeps.

A build that can reach a Kiro Space store sets one aiohttp application key,
``app["market_registries"]``, to a :class:`MarketRegistryProvider`. The
open-source build sets none, so every surface here answers "no store": the
routes in ``routes_market_registry.py`` report ``offers: []`` or 503, and
``app["market_client_factory"]`` (the install routes' client) stays unset.

What core owns:

* the ``market_registries`` list in ``config.json`` (store ids only; a store's
  address lives in the provider's code, never in an agent-writable file);
* the one check every store route and the install factory run
  (:func:`require_usable_registry`): an id is usable only when it is both added
  and offered, so what is listed and what can be installed read one list;
* the startup hook that sets ``app["market_client_factory"]`` once the provider
  is known (``routes_market_registry.register_market_client_factory``);
* turning a ``ListingView`` into a card row, matching installed apps by their
  ``market:<ref>@<n>`` source, never by name;
* the data-home folder the provider keeps each store's sign-in under
  (:func:`registry_data_dir`). The sandbox masks it in every mode.

``shortcut:`` the factory takes no argument, so one Kiro Crew installs from one
store; with two added it refuses. Give the factory a store id, and record the
store on the installed app, when a second store exists.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Protocol

from aiohttp import web

from kiro_crew.apps.market_source import LISTING_REF_RE
from kiro_crew.apps.marketplace_install import MarketClient, MarketInstallError

logger = logging.getLogger(__name__)

#: aiohttp application key a store provider sets.
MARKET_REGISTRIES_KEY = "market_registries"
#: Data-home folder holding one sub-folder per store's sign-in. Listed in the
#: sandbox's hidden, secret and pre-created leaf tables.
MARKET_REGISTRIES_LEAF = "market-registries"

#: A store id: a short lowercase slug, safe as one path segment.
REGISTRY_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
#: Most stores one config may list.
MAX_ADDED = 16

#: ``ListListings`` page size and the most pages one listing read fetches.
PAGE_LIMIT = 100
MAX_PAGES = 10

SIGNED_IN = "signed_in"
NEEDS_SIGN_IN = "needs_sign_in"
NO_ACCESS = "no_access"
UNKNOWN = "unknown"
_STATES = frozenset({SIGNED_IN, NEEDS_SIGN_IN, NO_ACCESS, UNKNOWN})
#: Why a status is ``unknown``, when the provider knows: the store CLI is not
#: installed or cannot run, or it is too old for the store.
_STATUS_DETAILS = frozenset({"cli_missing", "cli_outdated"})
#: Fields of a sign-in view the dashboard may see.
_SIGN_IN_VIEW_KEYS = ("state", "reason", "code", "approvalPage")


class MarketReader(Protocol):
    """The store reads the App Store's Kiro Space source needs."""

    def list_listings(
        self, *, kind: str = "app", cursor: str | None = None, limit: int = 100
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    def get_listing(self, ref: str) -> dict[str, Any]: ...


class MarketRegistryProvider(Protocol):
    """What a build sets at ``app["market_registries"]``.

    ``offers`` and ``client`` run on the event loop: no child process, no
    network, only memory. ``status``, ``sign_in`` and ``sign_out`` run in the
    executor. ``status`` must name the account when it is ``signed_in``; a
    ``signed_in`` status with no account is read as ``unknown``. ``client``
    never returns ``None``: when the store is not signed in
    it raises ``MarketInstallError("market_signed_out", ...)``. The object it
    returns satisfies both :class:`MarketClient` and :class:`MarketReader`.
    """

    def offers(self) -> list[dict[str, str]]: ...

    def status(self, registry_id: str) -> dict[str, Any]: ...

    def client(self, registry_id: str) -> Any: ...

    def sign_in(self, registry_id: str) -> dict[str, Any]: ...

    def sign_out(self, registry_id: str) -> dict[str, Any]: ...


def registry_data_dir(registry_id: str) -> Path:
    """``<data home>/market-registries/<id>``: where a provider keeps one store's sign-in.

    Built from :func:`~kiro_crew.config.paths.data_home`, so it follows
    ``KIROCREW_HOME`` and never touches the disk. Refuses an id that is not a slug.
    """
    from kiro_crew.config.paths import data_home

    if not REGISTRY_ID_RE.fullmatch(registry_id):
        raise ValueError("store id is not a slug")
    return data_home() / MARKET_REGISTRIES_LEAF / registry_id


def provider_of(app: web.Application) -> MarketRegistryProvider | None:
    return app.get(MARKET_REGISTRIES_KEY)


def offered(provider: MarketRegistryProvider | None) -> list[dict[str, str]]:
    """The provider's offers, reduced to well-formed ``{id, label, web}`` rows."""
    if provider is None:
        return []
    try:
        raw = provider.offers()
    except Exception:  # noqa: BLE001 - a broken provider offers nothing
        logger.warning("Kiro Space store provider failed to list offers")
        return []
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        rid, label, web_url = row.get("id"), row.get("label"), row.get("web", "")
        if not isinstance(rid, str) or not REGISTRY_ID_RE.fullmatch(rid) or rid in seen:
            continue
        if not isinstance(label, str) or not label.strip():
            continue
        seen.add(rid)
        rows.append(
            {
                "id": rid,
                "label": label.strip(),
                "web": web_url if isinstance(web_url, str) and _is_https(web_url) else "",
            }
        )
    return rows


def _is_https(url: str) -> bool:
    return url.startswith("https://") and len(url) > len("https://")


def require_usable_registry(
    provider: MarketRegistryProvider | None, registry_id: object, added: list[str]
) -> str:
    """The one check: *registry_id* is added AND offered. Raises otherwise.

    Every store route and the install factory call it, so a store that is not
    listed cannot be installed from either.
    """
    if (
        provider is None
        or not isinstance(registry_id, str)
        or registry_id not in added
        or registry_id not in {o["id"] for o in offered(provider)}
    ):
        raise MarketInstallError(
            "market_registry_unavailable", "this Kiro Space store is not added"
        )
    return registry_id


def normalize_status(raw: object) -> dict[str, Any]:
    """A provider status reduced to ``{state, account, detail?}``; anything odd is ``unknown``.

    ``signed_in`` with an empty or non-string account is ``unknown``: an
    unnamed sign-in looks the same as signed out to every account check.
    """
    raw = raw if isinstance(raw, dict) else {}
    state = raw.get("state") if raw.get("state") in _STATES else UNKNOWN
    account = raw.get("account") if isinstance(raw.get("account"), str) else ""
    if state == SIGNED_IN and not account:
        state = UNKNOWN
    out: dict[str, Any] = {"state": state, "account": account if state == SIGNED_IN else ""}
    detail = raw.get("detail")
    if state == UNKNOWN and detail in _STATUS_DETAILS:
        out["detail"] = detail
    return out


def sign_in_view(raw: object) -> dict[str, str]:
    """The fields of a sign-in view the dashboard may see; an approval page must be https."""
    raw = raw if isinstance(raw, dict) else {}
    view = {k: raw[k] for k in _SIGN_IN_VIEW_KEYS if isinstance(raw.get(k), str)}
    if "approvalPage" in view and not _is_https(view["approvalPage"]):
        del view["approvalPage"]
    return view


# ---------------------------------------------------------------------------
# Added stores (config.json)
# ---------------------------------------------------------------------------


def added_ids_from(cfg: Any) -> list[str]:
    ids = getattr(cfg, "market_registries", None)
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []


def added_ids_in_memory() -> list[str]:
    """The added ids from the config the gateway holds, for a caller on the loop.

    Reads the live watcher's snapshot (no I/O). Before the watcher has started
    it falls back to the loader, whose answer is memoized by file fingerprint.
    """
    from kiro_crew.config import live

    snap = live.snapshot()
    if snap is not None:
        return added_ids_from(snap)
    from kiro_crew.config.loader import KiroCrewConfig

    return added_ids_from(KiroCrewConfig.load())


def write_added_ids(cfg_path: Path, ids: list[str], offered_ids: frozenset[str]) -> list[str]:
    """Set the offered part of ``market_registries`` to *ids*, under the config lock (blocking).

    An id the file holds that the provider does not offer right now (for
    example while the build's switch is off) is kept as it is, ahead of *ids*:
    the owner cannot see it, so a save cannot mean to remove it. Returns the
    list written.
    """
    from kiro_crew.config.loader import update_config_locked

    written: list[str] = []

    def _mutate(data: dict) -> dict:
        prior = data.get("market_registries")
        kept = [
            i
            for i in (prior if isinstance(prior, list) else [])
            if isinstance(i, str) and i not in offered_ids and i not in ids
        ]
        written[:] = list(dict.fromkeys([*kept, *ids]))
        data["market_registries"] = list(written)
        return data

    update_config_locked(cfg_path, mutate=_mutate)
    return written


# ---------------------------------------------------------------------------
# The install factory
# ---------------------------------------------------------------------------


def install_store(app: web.Application) -> tuple[MarketRegistryProvider, str]:
    """The provider and the one store the install routes install from, on the loop.

    Reads the added ids from memory and runs :func:`require_usable_registry`.
    Raises ``MarketInstallError`` when no single usable store is added.
    """
    provider = provider_of(app)
    # An id kept in config but not offered now is not a store to install from.
    offered_ids = {o["id"] for o in offered(provider)}
    added = [i for i in added_ids_in_memory() if i in offered_ids]
    if len(added) != 1:
        raise MarketInstallError(
            "market_registry_unavailable", "add exactly one Kiro Space store first"
        )
    registry_id = require_usable_registry(provider, added[0], added)
    assert provider is not None  # require_usable_registry refuses None
    return provider, registry_id


def make_market_client_factory(app: web.Application) -> Callable[[], MarketClient]:
    """The zero-argument factory the install routes call, on the loop.

    Returns the client of :func:`install_store`'s store. Raises
    ``MarketInstallError`` (the routes answer 503 with its code) when no single
    usable store is added or the store is signed out.
    """

    def factory() -> MarketClient:
        provider, registry_id = install_store(app)
        client: MarketClient = provider.client(registry_id)
        return client

    return factory


# ---------------------------------------------------------------------------
# Listing reads (never cached)
# ---------------------------------------------------------------------------


def fetch_listings(reader: MarketReader) -> tuple[list[dict[str, Any]], bool]:
    """Every app listing, up to ``MAX_PAGES`` pages. Blocking: run in the executor.

    Returns the items and whether a next page was left unread.
    """
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        page, cursor = reader.list_listings(kind="app", cursor=cursor, limit=PAGE_LIMIT)
        items.extend(page)
        if cursor is None:
            return items, False
    return items, True


# ---------------------------------------------------------------------------
# Card rows
# ---------------------------------------------------------------------------


def _text(view: dict[str, Any], key: str) -> str:
    value = view.get(key)
    return value if isinstance(value, str) else ""


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _market_version(source: object, ref: str) -> int | None:
    """The version of an install whose source is ``market:<ref>@<n>``, else ``None``."""
    prefix = f"market:{ref}@"
    if isinstance(source, str) and source.startswith(prefix):
        tail = source[len(prefix) :]
        if tail.isdigit():
            return int(tail)
    return None


def listing_row(
    view: dict[str, Any],
    registry_id: str,
    installed_by_name: dict[str, dict[str, Any]],
    *,
    detail: bool = False,
) -> dict[str, Any] | None:
    """One ``ListingView`` as a card row, or ``None`` when it is not a well-formed app.

    The row is installed only when an installed app's source is
    ``market:<this ref>@<n>``. An installed app of the same name from anywhere
    else marks ``nameTaken``: ``install_app`` refuses a second app of one name.
    The download count is ``absent`` on list rows (the list's copy is not a real
    count yet) and shown only on the detail.
    """
    ref = _text(view, "ref")
    if not LISTING_REF_RE.fullmatch(ref) or view.get("kind") != "app":
        return None
    name = ref.partition("/")[2]
    owner = view.get("owner")
    publisher = owner.get("display") if isinstance(owner, dict) else None
    rating = view.get("rating")
    average = rating.get("average") if isinstance(rating, dict) else None
    rating_count = _count(view.get("ratingCount"))
    latest = _count(view.get("latestVersion"))
    tags = view.get("tags")

    installed = installed_by_name.get(name)
    installed_version = _market_version(installed.get("source"), ref) if installed else None
    row: dict[str, Any] = {
        "registry": registry_id,
        "ref": ref,
        "name": name,
        "displayName": _text(view, "displayName") or name,
        "summary": _text(view, "summary"),
        "category": _text(view, "category"),
        "tags": [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else [],
        "publisher": publisher if isinstance(publisher, str) else ref.partition("/")[0],
        "latestVersion": latest,
        "latestSemver": _text(view, "latestSemver"),
        "trust": _text(view, "trust"),
        "official": view.get("official") is True,
        "canPublish": view.get("canPublish") is True,
        "rating": (
            {"status": "ok", "value": float(average)}
            if isinstance(average, (int, float)) and not isinstance(average, bool)
            else {"status": "absent"}
        ),
        "ratingCount": (
            {"status": "ok", "value": rating_count}
            if rating_count is not None
            else {"status": "absent"}
        ),
        "downloads": {"status": "absent"},
        "installed": installed_version is not None,
        "installedVersion": installed_version,
        "nameTaken": installed is not None and installed_version is None,
    }
    if detail:
        downloads = _count(view.get("downloads"))
        if view.get("downloadsUnavailable") is True:
            row["downloads"] = {"status": "unavailable"}
        elif downloads is not None:
            row["downloads"] = {"status": "ok", "value": downloads}
        row["lastPublishedAt"] = _text(view, "lastPublishedAt")
    return row


def installed_by_name(installed: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {a["name"]: a for a in installed if isinstance(a, dict) and "name" in a}
