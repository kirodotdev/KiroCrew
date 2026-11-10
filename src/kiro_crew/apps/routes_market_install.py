"""Installing an app from Kiro Space: prepare, then confirm.

``POST /api/apps/market/install`` runs :func:`prepare_market_install` (resolve,
download, verify the SHA-256, unpack into staging) and returns the confirm data
with an opaque staging id. ``POST /api/apps/market/install/confirm`` takes that
id and either installs what the prepare step verified (``confirmed: true``) or
discards it (``confirmed: false``).

Security rules this module keeps:

* Both routes are owner-only, through the same gate as ``POST /api/apps/install``
  (:func:`require_owner_dashboard_request`). The gate runs before the body is
  read and before any client, staging folder or registry is touched.
* The staging id is minted here (``secrets.token_urlsafe``), checked against a
  strict pattern, and only ever used as a key into this process's own registry.
  It is never joined to a path: the folder to delete is the one the registry
  holds, and it is checked to sit under the staging root when it is registered.
* Confirm installs the already verified folder. It never calls the client, so
  nothing is downloaded or re-verified at that step, and an id is single use.
* Pending stagings are bounded in number and in lifetime. Expiry, discard,
  confirm and shutdown all remove the folder; discard is idempotent.
* The first path segment ``market`` is reserved so an app cannot be named after
  these routes (see ``RESERVED_APP_PATH_SEGMENTS``).

The listing client comes from ``app["market_client_factory"]``, a zero-argument
callable returning a :class:`MarketClient`. With none configured the routes
answer 503 and touch nothing.

Part of the ``kiro_crew.apps.routes`` family; this module never imports the
facade.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps.manager import app_lifecycle_lock
from kiro_crew.apps.market_source import LISTING_REF_RE
from kiro_crew.apps.marketplace_install import (
    MarketInstallError,
    PreparedInstall,
    complete_market_install,
    market_staging_root,
    prepare_market_install,
)
from kiro_crew.apps.routes_lifecycle import (
    _announce_app_ui_changed,
    _refuse_while_startup_hook_runs,
)
from kiro_crew.apps.routes_proxy import invalidate_app_secret_cache
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.routes")

#: Key under which the injected client factory sits on the aiohttp application.
MARKET_CLIENT_FACTORY_KEY = "market_client_factory"
#: Key of this module's staging registry on the aiohttp application.
MARKET_STAGING_KEY = "market_staging"

#: How long a verified, unpacked app waits for the person's answer.
STAGING_TTL_SECONDS = 900.0
#: Most stagings pending at once (each can hold up to the unpacked-size cap).
MAX_PENDING = 8

#: Most listing detail (confirm data plus the resolved listing) one pending install may
#: keep; the count cap alone would not bound it.
MAX_METADATA_BYTES = 64 * 1024

#: Exactly what ``secrets.token_urlsafe(16)`` yields.
_STAGING_ID_RE = re.compile(r"[A-Za-z0-9_-]{22}")


def _now() -> float:
    return time.monotonic()


def _not_configured() -> web.Response:
    return _error(503, "market_client_unavailable", "Kiro Space isn't configured on this Kiro Crew")


def _metadata_bytes(prepared: PreparedInstall) -> int:
    """Serialized size of everything the registry would retain besides the folder."""
    return len(json.dumps([asdict(prepared.resolved), prepared.confirm], default=str))


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message, "code": code}, status=status)


@dataclass
class _Entry:
    prepared: PreparedInstall
    expires_at: float
    timer: asyncio.TimerHandle | None = None


@dataclass
class MarketStagingRegistry:
    """The server's own record of pending stagings, keyed by opaque id."""

    entries: dict[str, _Entry] = field(default_factory=dict)
    reserved: int = 0

    def _drop_expired(self) -> list[PreparedInstall]:
        now = _now()
        gone = [i for i, e in self.entries.items() if e.expires_at <= now]
        popped = [self._pop(i) for i in gone]
        return [e.prepared for e in popped if e is not None]

    def _pop(self, staging_id: str) -> _Entry | None:
        entry = self.entries.pop(staging_id, None)
        if entry is not None and entry.timer is not None:
            entry.timer.cancel()
        return entry

    def sweep(self) -> list[PreparedInstall]:
        """Remove expired entries from the registry; the caller deletes the folders."""
        return self._drop_expired()

    def reserve(self) -> tuple[bool, list[PreparedInstall]]:
        """Take one of the ``MAX_PENDING`` slots. Returns (granted, expired entries)."""
        expired = self._drop_expired()
        if len(self.entries) + self.reserved >= MAX_PENDING:
            return False, expired
        self.reserved += 1
        return True, expired

    def release(self) -> None:
        self.reserved = max(0, self.reserved - 1)

    def add(self, prepared: PreparedInstall, loop: asyncio.AbstractEventLoop) -> str:
        staging_id = secrets.token_urlsafe(16)
        ttl = STAGING_TTL_SECONDS
        entry = _Entry(prepared=prepared, expires_at=_now() + ttl)
        entry.timer = loop.call_later(ttl, self._on_timer, staging_id, loop)
        self.entries[staging_id] = entry
        return staging_id

    def _on_timer(self, staging_id: str, loop: asyncio.AbstractEventLoop) -> None:
        entry = self._pop(staging_id)
        if entry is not None:
            loop.create_task(_discard_off_loop([entry.prepared]))

    def take(self, staging_id: str) -> PreparedInstall | None:
        """Remove and return a live entry (single use); ``None`` if unknown or expired."""
        entry = self._pop(staging_id)
        if entry is None:
            return None
        if entry.expires_at <= _now():
            # Expired between timer ticks: never hand it out.
            asyncio.get_running_loop().create_task(_discard_off_loop([entry.prepared]))
            return None
        return entry.prepared

    def take_all(self) -> list[PreparedInstall]:
        ids = list(self.entries)
        return [e.prepared for i in ids if (e := self._pop(i)) is not None]


async def _discard_off_loop(items: list[PreparedInstall]) -> None:
    """Delete staging folders in the executor; deletion errors never escape."""
    if not items:
        return

    def _run() -> None:
        for item in items:
            try:
                item.discard()
            except Exception:  # noqa: BLE001 - cleanup must not raise
                logger.warning("Market staging cleanup failed", exc_info=True)

    await asyncio.get_running_loop().run_in_executor(subprocess_executor(), _run)


def _registry(app: web.Application) -> MarketStagingRegistry:
    reg = app.get(MARKET_STAGING_KEY)
    if reg is None:
        reg = MarketStagingRegistry()
        app[MARKET_STAGING_KEY] = reg
    return reg


def _under_staging_root(path: Path) -> bool:
    try:
        root = market_staging_root().resolve()
        return root in path.resolve().parents
    except OSError:
        return False


async def _read_body(request: web.Request) -> dict[str, Any] | web.Response:
    from kiro_crew.dashboard.handlers._shared import body_not_object_error

    try:
        body = await request.json()
    except (LookupError, RecursionError, ValueError):  # bad charset, deep nesting, bad JSON
        return _error(400, "invalid_json", "invalid JSON")
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    return body


async def handle_market_install_prepare(request: web.Request) -> web.Response:
    """POST /api/apps/market/install — verify and unpack; return the confirm data.

    Body: ``{"ref": "<publisher>/<name>", "version": <int, optional>}``. Installs
    nothing; the answer carries ``stagingId`` for the confirm route.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "market_install_prepare")
    if denied is not None:
        return denied
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    ref = body.get("ref")
    version = body.get("version")
    if not isinstance(ref, str) or not LISTING_REF_RE.fullmatch(ref):
        return _error(400, "market_resolve_invalid", "listing ref is invalid")
    if version is not None and (
        not isinstance(version, int) or isinstance(version, bool) or version < 1
    ):
        return _error(400, "market_resolve_invalid", "version must be a positive integer")

    factory = request.app.get(MARKET_CLIENT_FACTORY_KEY)
    if factory is None:
        return _not_configured()
    registry = _registry(request.app)
    granted, expired = registry.reserve()
    await _discard_off_loop(expired)
    if not granted:
        return _error(
            429, "market_install_too_many_pending", "too many installs are waiting for an answer"
        )
    try:
        try:
            client = factory()
        except MarketInstallError as exc:
            return _error(503, exc.code, exc.message)
        except Exception:  # noqa: BLE001 - never leak client construction detail
            logger.warning("Market client factory failed", exc_info=True)
            return _error(503, "market_client_unavailable", "Kiro Space is not available")
        try:
            prepared = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), partial(prepare_market_install, client, ref, version)
            )
        except MarketInstallError as exc:
            return _error(400, exc.code, exc.message)
        except Exception:  # noqa: BLE001 - never leak the listing server's detail
            logger.warning("Market prepare failed", exc_info=True)
            sel().log_api_access(
                caller="dashboard",
                operation="market_install_prepare",
                outcome="failed",
                resources=f"ref={ref!r}",
                error="market_unavailable",
            )
            return _error(502, "market_unavailable", "Kiro Space could not be reached")
        if _metadata_bytes(prepared) > MAX_METADATA_BYTES:
            await _discard_off_loop([prepared])
            return _error(400, "market_metadata_too_large", "listing details are too large")
        if not await asyncio.to_thread(_under_staging_root, prepared.staging_dir):
            await _discard_off_loop([prepared])
            return _error(500, "market_staging_invalid", "staging folder is outside its root")
        staging_id = registry.add(prepared, asyncio.get_running_loop())
    finally:
        registry.release()
    sel().log_api_access(
        caller="dashboard",
        operation="market_install_prepare",
        outcome="completed",
        resources=f"ref={ref!r} version={prepared.resolved.version}",
    )
    return web.json_response({"ok": True, "stagingId": staging_id, "confirm": prepared.confirm})


async def handle_market_install_confirm(request: web.Request) -> web.Response:
    """POST /api/apps/market/install/confirm — install the verified folder, or discard it.

    Body: ``{"stagingId": "<id from prepare>", "confirmed": true | false}``.
    ``confirmed: false`` discards and is idempotent. ``confirmed: true`` installs
    the app disabled, from the folder prepare verified; the id is single use.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "market_install_confirm")
    if denied is not None:
        return denied
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    staging_id = body.get("stagingId")
    if not isinstance(staging_id, str) or not _STAGING_ID_RE.fullmatch(staging_id):
        return _error(400, "market_staging_id_invalid", "staging id is invalid")
    confirmed = body.get("confirmed")
    if not isinstance(confirmed, bool):
        return _error(400, "market_confirm_invalid", "confirmed must be true or false")
    if request.app.get(MARKET_CLIENT_FACTORY_KEY) is None:
        return _not_configured()

    registry = _registry(request.app)
    await _discard_off_loop(registry.sweep())

    if not confirmed:
        prepared = registry.take(staging_id)
        if prepared is not None:
            await _discard_off_loop([prepared])
        sel().log_api_access(
            caller="dashboard",
            operation="market_install_confirm",
            outcome="discarded" if prepared is not None else "noop",
            resources="discard",
        )
        return web.json_response({"ok": True, "discarded": prepared is not None})

    # The entry leaves the registry here, synchronously, so two confirms cannot
    # both install it. From this point the folder is removed whatever happens.
    prepared = registry.take(staging_id)
    if prepared is None:
        return _error(404, "market_staging_unknown", "no pending install has that id")
    name = prepared.listing_name
    async with app_lifecycle_lock(name):
        startup_refusal = await _refuse_while_startup_hook_runs(name, action="install")
        if startup_refusal is not None:
            await _discard_off_loop([prepared])
            return startup_refusal
        result = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            partial(complete_market_install, prepared, confirmed=True),
        )
    if not result.ok:
        return web.json_response(
            {
                "ok": False,
                "name": result.name,
                "error": result.error,
                "code": result.error_code or "market_install_failed",
            },
            status=400,
        )
    invalidate_app_secret_cache(result.name)
    _announce_app_ui_changed(request, result.name)
    return web.json_response(result.to_dict(), status=201)


def register_market_install_routes(app: web.Application) -> None:
    """Add the two routes and the shutdown cleanup. Call before the ``{name}`` catch-all."""
    registry = _registry(app)

    async def _discard_pending(_app: web.Application) -> None:
        await _discard_off_loop(registry.take_all())

    app.on_cleanup.append(_discard_pending)
    app.router.add_post("/api/apps/market/install", handle_market_install_prepare)
    app.router.add_post("/api/apps/market/install/confirm", handle_market_install_confirm)
