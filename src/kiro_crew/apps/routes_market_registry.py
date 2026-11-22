"""Kiro Space stores as App Store sources: add, sign in, list, open one listing.

Five routes, all owner-only through the same gate as ``POST /api/apps/install``
(:func:`require_owner_dashboard_request`, run before the body is read):

* ``GET  /api/apps/market/registries`` -- the offered stores, the added ids and
  each added store's sign-in status;
* ``PUT  /api/apps/market/registries`` -- ``{"added": [<id>, ...]}``, ids the
  provider offers only (``400 market_registry_unknown`` otherwise), at most
  one (``400 market_registry_one_only``);
* ``POST /api/apps/market/registries/{id}/signin`` and ``.../signout`` -- the
  provider's own sign-in; the body must be ``{}``;
* ``GET  /api/apps/market/listings?registry=<id>`` -- the store's apps as card
  rows;
* ``GET  /api/apps/market/listing?registry=<id>&ref=<ns>/<name>`` -- one listing.

Nothing read from a store is kept: every listing read goes to the store with
the credential that is current then, and every answer names the account it
was read for, so the page can tell when the account it loaded under is gone.

With no provider (``app["market_registries"]`` unset, as in the open-source
build) the read answers ``offers: []`` and every other route answers
``503 market_registry_unavailable``. Every route that names a store first runs
:func:`~kiro_crew.apps.market_registries.require_usable_registry`.

Store calls run in the executor: the client is synchronous. Nothing a route
logs or answers carries a token, an ``Authorization`` header or the store CLI's
output; the audit records the operation, the store id, a ref and a result code.

Part of the ``kiro_crew.apps.routes`` family; this module never imports the
facade. Registered before the ``/api/apps/{name}`` catch-all.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps.market_registries import (
    MAX_ADDED,
    NEEDS_SIGN_IN,
    NO_ACCESS,
    SIGNED_IN,
    added_ids_from,
    fetch_listings,
    install_store,
    installed_by_name,
    listing_row,
    make_market_client_factory,
    normalize_status,
    offered,
    provider_of,
    require_usable_registry,
    sign_in_view,
    write_added_ids,
)
from kiro_crew.apps.market_source import LISTING_REF_RE
from kiro_crew.apps.marketplace_install import MarketInstallError
from kiro_crew.apps.routes_market_install import (
    MARKET_ACCOUNT_KEY,
    MARKET_CLIENT_FACTORY_KEY,
    MARKET_STORE_KEY,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.routes")


def _store_error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message, "code": code}, status=status)


def _store_unavailable(exc: MarketInstallError | None = None) -> web.Response:
    return _store_error(
        503,
        exc.code if exc is not None else "market_registry_unavailable",
        exc.message if exc is not None else "no Kiro Space store is available",
    )


def _store_audit(operation: str, outcome: str, resources: str) -> None:
    sel().log_api_access(
        caller="dashboard", operation=operation, outcome=outcome, resources=resources
    )


async def _store_gate(request: web.Request, operation: str) -> web.Response | None:
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    return await require_owner_dashboard_request(request, operation)


async def _store_added_ids() -> list[str]:
    from kiro_crew.config.loader import KiroCrewConfig

    return added_ids_from(await asyncio.to_thread(KiroCrewConfig.load))


async def _store_off_loop(fn: Any, *args: Any) -> Any:
    return await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), partial(fn, *args)
    )


async def _store_status(provider: Any, registry_id: str) -> dict[str, Any]:
    """The store's status, read now; a failed probe reads as ``unknown``."""
    try:
        return normalize_status(await _store_off_loop(provider.status, registry_id))
    except Exception as exc:  # noqa: BLE001 - a failed probe reads as unknown
        logger.warning("Kiro Space store status failed: %s", type(exc).__name__)
        return normalize_status(None)


async def _store_usable(
    request: web.Request, registry_id: object
) -> tuple[Any, str] | web.Response:
    provider = provider_of(request.app)
    try:
        rid = require_usable_registry(provider, registry_id, await _store_added_ids())
    except MarketInstallError as exc:
        return _store_unavailable(exc)
    return provider, rid


async def handle_market_registries_get(request: web.Request) -> web.Response:
    """GET /api/apps/market/registries -- offers, added ids, each added store's status."""
    denied = await _store_gate(request, "market_registries.read")
    if denied is not None:
        return denied
    provider = provider_of(request.app)
    offers = offered(provider)
    offered_ids = {o["id"] for o in offers}
    added = [i for i in await _store_added_ids() if i in offered_ids]
    statuses = {}
    for rid in added:
        statuses[rid] = await _store_status(provider, rid)
    return web.json_response({"offers": offers, "added": added, "status": statuses})


async def handle_market_registries_put(request: web.Request) -> web.Response:
    """PUT /api/apps/market/registries -- ``{"added": [<id>, ...]}`` replaces the added ids."""
    from kiro_crew.dashboard.handlers._shared import body_not_object_error

    denied = await _store_gate(request, "market_registries.update")
    if denied is not None:
        return denied
    provider = provider_of(request.app)
    if provider is None:
        return _store_unavailable()
    try:
        body = await request.json()
    except (LookupError, RecursionError, ValueError):
        return _store_error(400, "invalid_json", "invalid JSON")
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    ids = body.get("added")
    if not isinstance(ids, list) or len(ids) > MAX_ADDED:
        return _store_error(400, "market_registry_invalid", "added must be a list of store ids")
    offered_ids = {o["id"] for o in offered(provider)}
    wanted: list[str] = []
    for rid in ids:
        if not isinstance(rid, str) or rid not in offered_ids:
            _store_audit("market_registries.update", "denied", "unknown store")
            return _store_error(
                400, "market_registry_unknown", "that Kiro Space store is not offered"
            )
        if rid not in wanted:
            wanted.append(rid)
    # shortcut: installs come from exactly one added store (``install_store``),
    # so a second one would leave every Install refused; lift this when the
    # install factory takes a store id.
    if len(wanted) > 1:
        _store_audit("market_registries.update", "denied", "more than one store")
        return _store_error(
            400, "market_registry_one_only", "only one Kiro Space store can be added for now"
        )

    from kiro_crew.config.loader import config_path
    from kiro_crew.dashboard.chat_utils import run_config_write

    try:
        await run_config_write(write_added_ids, Path(config_path()), wanted, frozenset(offered_ids))
    except Exception as exc:  # noqa: BLE001 - report, never 500 with detail
        logger.warning("Writing market_registries failed: %s", type(exc).__name__)
        _store_audit("market_registries.update", "failed", "config write")
        return _store_error(
            500, "market_registries_config_write_failed", "could not save the stores"
        )
    _store_audit("market_registries.update", "success", f"added={','.join(wanted)}")
    return web.json_response({"ok": True, "added": wanted})


async def _store_sign(request: web.Request, action: str) -> web.Response:
    operation = f"market_registries.{action}"
    denied = await _store_gate(request, operation)
    if denied is not None:
        return denied
    usable = await _store_usable(request, request.match_info.get("id"))
    if isinstance(usable, web.Response):
        return usable
    provider, rid = usable
    # The body is empty: no flow, no option the provider would act on.
    if request.can_read_body:
        try:
            body = await request.json()
        except (LookupError, RecursionError, ValueError):
            return _store_error(400, "invalid_json", "invalid JSON")
        if body != {}:
            return _store_error(400, f"market_{action}_invalid", "the body must be an empty object")
    method = provider.sign_in if action == "signin" else provider.sign_out
    try:
        view = sign_in_view(await _store_off_loop(method, rid))
    except Exception as exc:  # noqa: BLE001 - the provider's detail stays here
        logger.warning("Kiro Space store %s failed: %s", action, type(exc).__name__)
        _store_audit(operation, "failed", f"registry={rid}")
        failed = (
            "Kiro Space sign-in did not start"
            if action == "signin"
            else "Kiro Space sign-out failed"
        )
        return _store_error(502, f"market_{action}_failed", failed)
    _store_audit(operation, "success", f"registry={rid} state={view.get('state', '')}")
    return web.json_response({"ok": True, "registry": rid, **view})


async def handle_market_registry_signin(request: web.Request) -> web.Response:
    """POST /api/apps/market/registries/{id}/signin -- start the provider's sign-in."""
    return await _store_sign(request, "signin")


async def handle_market_registry_signout(request: web.Request) -> web.Response:
    """POST /api/apps/market/registries/{id}/signout -- sign out of the store."""
    return await _store_sign(request, "signout")


def _store_client(provider: Any, rid: str) -> Any:
    """The provider's client; ``None`` when it reports the store is signed out."""
    try:
        return provider.client(rid)
    except MarketInstallError:
        return None


async def _store_read_failed(
    provider: Any, rid: str, exc: MarketInstallError, operation: str
) -> dict[str, Any]:
    """Answer fields for a failed store read: a fresh status and, unless signed out, the code.

    A store that refuses the token while the status still says ``signed_in``
    answers that live status with ``market_signed_out``: making up a signed-out
    status would name another account than the status read, and the page
    reloads on that.
    """
    _store_audit(operation, "failed", f"registry={rid} code={exc.code}")
    status = await _store_status(provider, rid)
    if status["state"] in (NEEDS_SIGN_IN, NO_ACCESS):
        return {"status": status}
    return {"status": status, "error": {"code": exc.code}}


async def _store_still(provider: Any, rid: str, status: dict[str, Any]) -> dict[str, Any] | None:
    """``None`` when the status read now names the same signed-in account as *status*.

    Otherwise the answer fields: the fresh status, with ``market_unavailable``
    when it is ``unknown``. Run after the last other await of a read, so what
    the answer names is what the store said last.
    shortcut: a switch to another account and back within one read is not
    seen; closing it needs the provider's client to name its account.
    """
    fresh = await _store_status(provider, rid)
    if fresh["state"] == SIGNED_IN and fresh["account"] == status["account"]:
        return None
    moved: dict[str, Any] = {"status": fresh}
    if fresh["state"] not in (SIGNED_IN, NEEDS_SIGN_IN, NO_ACCESS):
        moved["error"] = {"code": "market_unavailable"}
    return moved


async def _store_installed() -> dict[str, dict[str, Any]]:
    from kiro_crew.apps.manager import list_apps

    return installed_by_name(await asyncio.to_thread(list_apps))


async def _store_begin(
    provider: Any, rid: str, base: dict[str, Any]
) -> tuple[dict[str, Any], Any] | web.Response:
    """The status and client a read starts from, or the answer when it cannot start.

    ``needs_sign_in`` or ``no_access`` answer *base* and the status; ``unknown``
    adds ``market_unavailable``; a client that reports the store signed out
    answers the live status with ``market_signed_out``.
    """
    status = await _store_status(provider, rid)
    if status["state"] in (NEEDS_SIGN_IN, NO_ACCESS):
        return web.json_response({**base, "status": status})
    if status["state"] != SIGNED_IN:
        return web.json_response(
            {**base, "status": status, "error": {"code": "market_unavailable"}}
        )
    client = _store_client(provider, rid)
    if client is None:
        return web.json_response({**base, "status": status, "error": {"code": "market_signed_out"}})
    return status, client


async def handle_market_listings(request: web.Request) -> web.Response:
    """GET /api/apps/market/listings?registry=<id> -- the store's apps as rows, read now.

    Always 200 once the store is usable. Rows come only under a ``signed_in``
    status, and ``account`` names the account they were read for (``""`` with
    no rows). ``needs_sign_in`` or ``no_access`` answers no rows and the
    status, so the page shows a sign-in card; ``unknown`` answers no rows, the
    status and ``error.code``. A read after which the status does not name
    the same account (:func:`_store_still`) answers no rows and the fresh
    status. A failed read answers no rows, the error code and a fresh status.
    ``truncated`` is true when more than ``MAX_PAGES`` pages exist.
    """
    denied = await _store_gate(request, "market_listings.read")
    if denied is not None:
        return denied
    usable = await _store_usable(request, request.query.get("registry"))
    if isinstance(usable, web.Response):
        return usable
    provider, rid = usable
    base: dict[str, Any] = {
        "ok": True,
        "registry": rid,
        "account": "",
        "rows": [],
        "truncated": False,
    }
    begun = await _store_begin(provider, rid, base)
    if isinstance(begun, web.Response):
        return begun
    status, client = begun
    try:
        items, truncated = await _store_off_loop(fetch_listings, client)
    except MarketInstallError as exc:
        failed = await _store_read_failed(provider, rid, exc, "market_listings.read")
        return web.json_response({**base, **failed})
    except Exception as exc:  # noqa: BLE001 - never leak the store's detail
        logger.warning("Kiro Space listing read failed: %s", type(exc).__name__)
        fresh = await _store_status(provider, rid)
        return web.json_response({**base, "status": fresh, "error": {"code": "market_unavailable"}})
    installed = await _store_installed()
    moved = await _store_still(provider, rid, status)
    if moved is not None:
        return web.json_response({**base, **moved})
    rows = [r for r in (listing_row(v, rid, installed) for v in items) if r is not None]
    return web.json_response(
        {
            **base,
            "status": status,
            "account": status["account"],
            "rows": rows,
            "truncated": truncated,
        }
    )


async def handle_market_listing(request: web.Request) -> web.Response:
    """GET /api/apps/market/listing?registry=<id>&ref=<ns>/<name> -- one listing, read now.

    ``listing`` only under ``signed_in``, with ``account`` naming whose it is
    (``""`` without one); a read after which the status does not name the
    same account (:func:`_store_still`) answers no listing and the fresh status.
    """
    denied = await _store_gate(request, "market_listing.read")
    if denied is not None:
        return denied
    ref = request.query.get("ref", "")
    if not LISTING_REF_RE.fullmatch(ref):
        return _store_error(400, "market_resolve_invalid", "listing ref is invalid")
    usable = await _store_usable(request, request.query.get("registry"))
    if isinstance(usable, web.Response):
        return usable
    provider, rid = usable
    base: dict[str, Any] = {"ok": True, "registry": rid, "account": "", "listing": None}
    begun = await _store_begin(provider, rid, base)
    if isinstance(begun, web.Response):
        return begun
    status, client = begun
    try:
        view = await _store_off_loop(client.get_listing, ref)
    except MarketInstallError as exc:
        failed = await _store_read_failed(provider, rid, exc, "market_listing.read")
        return web.json_response({**base, **failed})
    except Exception as exc:  # noqa: BLE001 - never leak the store's detail
        logger.warning("Kiro Space listing detail failed: %s", type(exc).__name__)
        fresh = await _store_status(provider, rid)
        return web.json_response({**base, "status": fresh, "error": {"code": "market_unavailable"}})
    installed = await _store_installed()
    moved = await _store_still(provider, rid, status)
    if moved is not None:
        return web.json_response({**base, **moved})
    row = listing_row(view, rid, installed, detail=True)
    if row is None or row["ref"] != ref:
        return web.json_response(
            {**base, "status": status, "error": {"code": "market_resolve_invalid"}}
        )
    return web.json_response(
        {**base, "status": status, "account": status["account"], "listing": row}
    )


async def _install_account(app: web.Application) -> str:
    """The account the install store is signed in as now; ``""`` when signed out.

    Raises ``MarketInstallError("market_unavailable")`` when the status reads
    ``unknown`` (a failed probe reads so), as the store reads do: the install
    routes answer that the store is unavailable, not that the account changed.
    """
    try:
        provider, rid = install_store(app)
    except MarketInstallError:
        return ""
    status = await _store_status(provider, rid)
    if status["state"] not in (SIGNED_IN, NEEDS_SIGN_IN, NO_ACCESS):
        raise MarketInstallError("market_unavailable", "Kiro Space could not be reached")
    return str(status["account"])


def _install_store_id(app: web.Application) -> str:
    """The store the install routes install from now; ``""`` when none is usable."""
    try:
        return install_store(app)[1]
    except MarketInstallError:
        return ""


def register_market_client_factory(app: web.Application) -> None:
    """Set ``app["market_client_factory"]``, ``app["market_account"]`` and ``app["market_store"]``.

    At startup, and only when a provider is present. ``market_account`` and
    ``market_store`` are what the install routes compare between the page,
    prepare and confirm.

    At startup, not now: core registers its routes before a build contributes
    its own (which is when ``app["market_registries"]`` is set), and the app is
    frozen only after the startup hooks run.
    """

    async def _set_factory(app: web.Application) -> None:
        if provider_of(app) is not None and app.get(MARKET_CLIENT_FACTORY_KEY) is None:
            app[MARKET_CLIENT_FACTORY_KEY] = make_market_client_factory(app)
            app[MARKET_ACCOUNT_KEY] = partial(_install_account, app)
            app[MARKET_STORE_KEY] = partial(_install_store_id, app)

    app.on_startup.append(_set_factory)


def register_market_registry_routes(app: web.Application) -> None:
    """Add the store routes and the factory hook. Call before the ``{name}`` catch-all."""
    register_market_client_factory(app)
    app.router.add_get("/api/apps/market/registries", handle_market_registries_get)
    app.router.add_put("/api/apps/market/registries", handle_market_registries_put)
    app.router.add_post("/api/apps/market/registries/{id}/signin", handle_market_registry_signin)
    app.router.add_post("/api/apps/market/registries/{id}/signout", handle_market_registry_signout)
    app.router.add_get("/api/apps/market/listings", handle_market_listings)
    app.router.add_get("/api/apps/market/listing", handle_market_listing)
