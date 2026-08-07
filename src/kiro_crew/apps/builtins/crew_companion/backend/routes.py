"""HTTP routes for the Crew Companion builtin, served in-process by the gateway.

Mounted at ``/api/apps/crew-companion/`` — the same single-argument
``register_routes`` convention every builtin uses.

WHY IN-PROCESS MATTERS, NOT JUST TIDINESS
-----------------------------------------
The previous shape had these endpoints on a SEPARATE macOS app listening on
127.0.0.1:7778, reached through the gateway's reverse proxy. That path is where a
whole class of defects lived: the app secret the proxy signs with, the
malformed-port crash that could stop gateway startup, and the disabled-app hole
where a never-enabled app still had an authenticated route to its backend. None
of them are *fixed* here — they are unreachable, because there is no second
process, no loopback URL to resolve and no proxy hop.

Every handler is wrapped in :func:`_require_enabled`, so a disabled app answers
403 and a not-yet-started runtime answers 503. The two are different facts: the
caller can usefully retry one and not the other.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import wraps
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.builtins.crew_companion.hooks import get_appearances, get_store
from kiro_crew.apps.builtins.crew_companion.pack_transfer import (
    export_bundle,
    fetch_petdex_pet,
    import_bundle,
    save_sprite_pack,
)
from kiro_crew.apps.manager import is_app_enabled

logger = logging.getLogger(__name__)

# Upper bound for a reminder's recurrence interval. Ten years in minutes is
# far beyond any real reminder while staying tiny relative to what
# `datetime.timedelta` can represent — the point is a total-order guard that
# rejects absurd values (400-digit ints, 1e309→inf) with a 400 instead of
# letting them overflow deeper in the scheduler.
_MAX_RECURRENCE_MINUTES = 10 * 366 * 24 * 60

APP_NAME = "crew-companion"
_BASE = f"/api/apps/{APP_NAME}"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _bad_request(message: str, code: str) -> web.Response:
    """400 with a machine-readable ``code``.

    Three near-identical helpers instead of one taking ``status`` as a parameter,
    because ``test/test_error_code_contract.py`` is a STATIC scan: a computed
    ``status=status`` lands in its ``dynamic_status`` bucket, which it ratchets
    precisely so the gate cannot be sidestepped by hoisting the status into a
    variable. A literal per helper keeps every error response verifiable by
    reading it, which is the point of the gate — the alternative was raising the
    baseline, which its own docstring calls the one move it exists to prevent.

    The ``code`` matters beyond the gate: the dashboard renders ``error`` prose
    verbatim into a localized page, so English produced here would surface
    untranslated in a Chinese UI. The identifier is what a client switches on.
    """
    return web.json_response({"error": message, "code": code}, status=400)


def _forbidden(message: str, code: str) -> web.Response:
    """403 — the caller is not allowed to reach this app right now."""
    return web.json_response({"error": message, "code": code}, status=403)


def _unavailable(message: str, code: str) -> web.Response:
    """503 — the app is allowed, but its runtime is not up yet. Retryable."""
    return web.json_response({"error": message, "code": code}, status=503)


def _require_enabled(handler: Handler) -> Handler:
    """403 while disabled, 503 before the runtime is up, 503 if the store cannot write.

    ``is_app_enabled`` is a synchronous ``installed.json`` read, so it runs off
    the event loop.

    The write-failure translation lives here, in the one wrapper every route
    already goes through, rather than in each of the seven handlers that mutate
    the store. The store used to log an OSError and return ``{"ok": True}``, so a
    full or read-only data home produced a 200: the panel cleared the input and
    the reminder was gone after a restart. Now the store raises, and a raise that
    reached aiohttp would be a bare 500 with no machine-readable ``code`` — which
    is what ``test/test_error_code_contract.py`` exists to prevent. Putting it
    here also means a route added later cannot forget it.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return _forbidden("crew-companion is disabled", "app_disabled")
        if get_store() is None:
            return _unavailable(
                "crew-companion runtime not started", "runtime_not_started"
            )
        try:
            return await handler(request)
        except OSError:
            # Retryable by nature: the disk may have room, or write permission
            # back, by the time the client tries again.
            return _unavailable(
                "crew-companion could not save to disk", "store_write_failed"
            )

    return _wrapped


async def _body(request: web.Request) -> dict[str, Any]:
    """Parse a JSON object body, treating anything else as empty."""
    try:
        parsed = await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ── reads ───────────────────────────────────────────────────────────────────


async def _handle_reminders_get(request: web.Request) -> web.StreamResponse:
    store = get_store()
    assert store is not None  # guaranteed by _require_enabled
    return web.json_response(await asyncio.to_thread(store.snapshot))


async def _handle_stats_get(request: web.Request) -> web.StreamResponse:
    store = get_store()
    assert store is not None
    return web.json_response(await asyncio.to_thread(store.stats_payload))


async def _handle_pending_get(request: web.Request) -> web.StreamResponse:
    """What the companion should say, for the overlay to draw as bubbles.

    ``since`` is the last sequence number the caller already showed. Reading is
    non-destructive, so a lost response or a second overlay on another display
    cannot make a reminder disappear.
    """
    store = get_store()
    assert store is not None
    raw = request.query.get("since", "0")
    try:
        since = max(0, int(raw))
    except ValueError:
        return _bad_request("since must be an integer", "invalid_cursor")
    return web.json_response(await asyncio.to_thread(store.drain, since))


# ── writes ──────────────────────────────────────────────────────────────────


async def _handle_add(request: web.Request) -> web.StreamResponse:
    """Store an already-resolved reminder.

    The natural-language parsing happens in the renderer, which POSTs a concrete
    ``fireAt`` — see the note in ``store.add``. A missing or unparsable time is a
    client error rather than something to guess at, because the one rule the
    parser must never break is inventing a time the user did not give.
    """
    store = get_store()
    assert store is not None
    body = await _body(request)

    text = body.get("text")
    fire_at = body.get("fireAt")
    if not isinstance(text, str) or not text.strip():
        return _bad_request("text is required", "text_required")
    if not isinstance(fire_at, str) or not fire_at.strip():
        return _bad_request("fireAt is required", "fire_at_required")

    every = body.get("everyMinutes")
    # A bounded RANGE check, not `isfinite` + `> 0`: `math.isfinite` itself
    # raises OverflowError on an int too large for a float (a 400-digit
    # integer), so the previous guard moved the crash instead of removing
    # it. Pure comparisons are total for every JSON number — an arbitrary-
    # precision int compares exactly, `inf` fails the upper bound, and NaN
    # fails both — and the cap also keeps `timedelta(minutes=...)` in the
    # recurrence scheduler from overflowing downstream.
    if every is not None and not (
        isinstance(every, (int, float))
        and not isinstance(every, bool)
        and 0 < every <= _MAX_RECURRENCE_MINUTES
    ):
        return _bad_request("everyMinutes must be a positive number", "invalid_recurrence")

    try:
        result = await asyncio.to_thread(
            store.add, text, fire_at, int(every) if every else None
        )
    except ValueError as exc:
        return _bad_request(str(exc), "invalid_reminder")
    return web.json_response(result)


async def _handle_remove(request: web.Request) -> web.StreamResponse:
    store = get_store()
    assert store is not None
    ident = (await _body(request)).get("id")
    if not isinstance(ident, str) or not ident:
        return _bad_request("id is required", "id_required")
    return web.json_response(await asyncio.to_thread(store.remove, ident))


async def _handle_skip(request: web.Request) -> web.StreamResponse:
    store = get_store()
    assert store is not None
    ident = (await _body(request)).get("id")
    if not isinstance(ident, str) or not ident:
        return _bad_request("id is required", "id_required")
    return web.json_response(await asyncio.to_thread(store.skip, ident))


async def _handle_config(request: web.Request) -> web.StreamResponse:
    store = get_store()
    assert store is not None
    return web.json_response(
        await asyncio.to_thread(store.patch_config, await _body(request))
    )


async def _handle_presence(request: web.Request) -> web.StreamResponse:
    """The overlay reporting that the user is present.

    Break nudges are suppressed while nobody is there — nudging someone to
    stretch at a locked screen is pure noise. Reminders are unaffected: a time the
    user chose must still arrive, late, on their return.
    """
    store = get_store()
    assert store is not None
    return web.json_response(await asyncio.to_thread(store.note_presence))


async def _handle_breathing_done(request: web.Request) -> web.StreamResponse:
    """A guided exercise was COMPLETED — distinct from having been suggested."""
    store = get_store()
    assert store is not None
    return web.json_response(await asyncio.to_thread(store.note_breathing_session))


async def _handle_window(request: web.Request) -> web.StreamResponse:
    """Record a request to open one of the companion's windows (panel/gallery).

    The dashboard page has no preload bridge — those windows belong to the
    desktop main process and only the always-running overlay can open them. So
    the page records the intent here and the overlay carries it out when it next
    drains ``/pending``; see ``CompanionStore.queue_window_command``.
    """
    store = get_store()
    assert store is not None
    target = (await _body(request)).get("target")
    if target not in ("panel", "gallery"):
        return _bad_request("target must be 'panel' or 'gallery'", "invalid_window")
    return web.json_response(
        await asyncio.to_thread(store.queue_window_command, target)
    )


# ── Appearance packs ────────────────────────────────────────────────────────


async def _handle_appearances_get(request: web.Request) -> web.StreamResponse:
    """Every pack the gallery can offer, metadata only."""
    packs = await asyncio.to_thread(get_appearances().list_packs)
    return web.json_response({"packs": packs})


async def _handle_appearance_detail(request: web.Request) -> web.StreamResponse:
    """One pack with its art inlined, ready to render."""
    pack_id = request.query.get("id", "")
    detail = await asyncio.to_thread(get_appearances().pack_detail, pack_id)
    if detail is None:
        # Not found rather than a 400: an id that no longer resolves is the normal
        # outcome of a pack the user just deleted, not a malformed request.
        return _bad_request("no such appearance pack", "pack_not_found")
    return web.json_response(detail)


async def _handle_appearance_colours(request: web.Request) -> web.StreamResponse:
    """Record a recolouring of a pack."""
    body = await _body(request)
    ok = await asyncio.to_thread(
        get_appearances().set_colour_map, body.get("id", ""), body.get("colorMap")
    )
    if not ok:
        return _bad_request("could not save those colours", "invalid_colour_map")
    return web.json_response({"ok": True})


async def _handle_appearance_delete(request: web.Request) -> web.StreamResponse:
    """Delete a custom pack. The built-in cannot be deleted."""
    body = await _body(request)
    ok = await asyncio.to_thread(get_appearances().delete_pack, body.get("id", ""))
    if not ok:
        return _bad_request("that pack cannot be deleted", "pack_not_deletable")
    return web.json_response({"ok": True})


async def _handle_appearance_save(request: web.Request) -> web.StreamResponse:
    """Create or replace a custom pack."""
    body = await _body(request)
    ok = await asyncio.to_thread(
        get_appearances().save_pack,
        body.get("id", ""),
        body.get("manifest"),
        body.get("files"),
    )
    if not ok:
        return _bad_request("could not save that pack", "invalid_pack")
    return web.json_response({"ok": True})


async def _handle_appearance_export(request: web.Request) -> web.StreamResponse:
    """Hand back a portable bundle for one pack."""
    pack_id = request.query.get("id", "")
    bundle = await asyncio.to_thread(export_bundle, get_appearances(), pack_id)
    if bundle is None:
        return _bad_request("no such appearance pack", "pack_not_found")
    return web.json_response(bundle)


async def _handle_appearance_import(request: web.Request) -> web.StreamResponse:
    """Install a pack from an exported bundle."""
    body = await _body(request)
    result = await asyncio.to_thread(import_bundle, get_appearances(), body.get("bundle"))
    if not result.get("ok"):
        return _bad_request(str(result.get("error", "import failed")), "invalid_bundle")
    return web.json_response(result)


async def _handle_appearance_save_sprite(request: web.Request) -> web.StreamResponse:
    """Save a pack whose art is a single sprite sheet."""
    body = await _body(request)
    result = await asyncio.to_thread(
        save_sprite_pack,
        get_appearances(),
        body.get("id", ""),
        body.get("manifest"),
        body.get("spriteBase64"),
        body.get("filename", "sprites.png"),
    )
    if not result.get("ok"):
        return _bad_request(str(result.get("error", "save failed")), "invalid_pack")
    return web.json_response(result)


async def _handle_petdex_fetch(request: web.Request) -> web.StreamResponse:
    """Look a pet up on PetDex.

    Reaches the public internet, which nothing else in this app does — so it stays
    behind the same enable gate as every other route, and the host it may contact is
    pinned in ``pack_transfer``.
    """
    body = await _body(request)
    result = await asyncio.to_thread(fetch_petdex_pet, body.get("input", ""))
    if not result.get("ok"):
        # A miss or an unreachable registry is an expected outcome the dialog shows,
        # not a client error, so this is a 200 carrying ok:false.
        return web.json_response(result)
    return web.json_response(result)


def register_routes(app: web.Application) -> None:
    """Register on the gateway's aiohttp Application (single-arg convention)."""
    app.router.add_get(f"{_BASE}/reminders", _require_enabled(_handle_reminders_get))
    app.router.add_post(f"{_BASE}/reminders/add", _require_enabled(_handle_add))
    app.router.add_post(f"{_BASE}/reminders/remove", _require_enabled(_handle_remove))
    app.router.add_post(f"{_BASE}/reminders/skip", _require_enabled(_handle_skip))
    app.router.add_post(f"{_BASE}/reminders/config", _require_enabled(_handle_config))
    app.router.add_get(f"{_BASE}/stats", _require_enabled(_handle_stats_get))
    app.router.add_get(f"{_BASE}/pending", _require_enabled(_handle_pending_get))
    app.router.add_post(f"{_BASE}/presence", _require_enabled(_handle_presence))
    app.router.add_get(f"{_BASE}/appearances", _require_enabled(_handle_appearances_get))
    app.router.add_get(
        f"{_BASE}/appearances/export", _require_enabled(_handle_appearance_export)
    )
    app.router.add_post(
        f"{_BASE}/appearances/import", _require_enabled(_handle_appearance_import)
    )
    app.router.add_post(
        f"{_BASE}/appearances/save-sprite", _require_enabled(_handle_appearance_save_sprite)
    )
    app.router.add_post(f"{_BASE}/petdex/fetch", _require_enabled(_handle_petdex_fetch))
    app.router.add_get(
        f"{_BASE}/appearances/detail", _require_enabled(_handle_appearance_detail)
    )
    app.router.add_post(
        f"{_BASE}/appearances/colours", _require_enabled(_handle_appearance_colours)
    )
    app.router.add_post(
        f"{_BASE}/appearances/delete", _require_enabled(_handle_appearance_delete)
    )
    app.router.add_post(
        f"{_BASE}/appearances/save", _require_enabled(_handle_appearance_save)
    )
    app.router.add_post(
        f"{_BASE}/breathing-done", _require_enabled(_handle_breathing_done)
    )
    app.router.add_post(f"{_BASE}/window", _require_enabled(_handle_window))
