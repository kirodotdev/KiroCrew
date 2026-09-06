"""The crew appearance library's HTTP surface: ``/api/appearances/...``.

Crews wear appearance packs from a library the dashboard owns
(:mod:`kiro_crew.dashboard.appearances`). It is a SEPARATE library from Crew
Companion's: that app keeps its own packs under its own data directory and its
own ``/api/apps/crew-companion/`` routes, and nothing here touches them. The two
share only the pack format and the store class, so a pack exported from the
Companion gallery imports here unchanged.

Two properties worth reading here rather than inferring:

* **The slot route is the cheap one.** It is what an ``<img src>`` in the crew
  roster hits, once per crew per state change, so it serves ONE slot's bytes
  with an ETag and a private cache window instead of the whole-pack payload
  ``GET /api/appearances/{id}`` returns. It also resolves the fallback chain
  server-side, because a client that had to probe ``working`` then ``loading``
  then ``thinking`` then ``idle`` would spend four requests learning what the
  manifest already says.
* **Every route is owner-gated and every outcome is SEL-audited**, reads
  included. The gate itself audits a DENIAL (``require_owner_dashboard_request``),
  so what each handler adds is the accepted decision — the same shape the nearest
  sibling uses: ``GET /api/agents/{name}/avatar`` is an owner-gated read of
  user-supplied media on this origin and audits its success. An earlier revision
  of this module skipped the reads on the argument that slot traffic would bury
  the log; that was a guess with no measurement behind it, and it left a
  permission decision on user-authored content with no accepted-side record. If
  the volume ever does matter, narrowing it is a separate change with a number
  attached.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from aiohttp import BodyPartReader, web

from kiro_crew.appearance_packs import DEFAULT_PACK
from kiro_crew.dashboard.appearances import (
    delete_pack_if_unworn,
    get_appearance_store,
    import_pack,
    pack_slot_file,
    slot_body,
)
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

logger = logging.getLogger(__name__)

#: How long a browser may reuse a slot's bytes. Short: a pack the
#: user is actively editing in the gallery must not stay stale for minutes. The
#: ETag makes the revalidation itself nearly free.
_MEDIA_CACHE_CONTROL = "private, max-age=60"

#: The policy an untrusted SVG is served under. Byte-identical to the one
#: ``handlers/files.py`` uses for an untrusted SVG read, deliberately: two
#: different policies on the same class of content is a drift waiting to happen,
#: and this one is the house answer.
_SVG_CSP = "script-src 'none'; style-src 'unsafe-inline'"

#: Cap on a slot name. It is only ever a dict key here, never a path segment,
#: but an unbounded key from the wire has no reason to be accepted.
_MAX_SLOT_LEN = 64


def _max_bundle_bytes() -> int:
    """``pack_transfer.MAX_BUNDLE_BYTES``, resolved at call time.

    boot path: ``pack_transfer`` builds a ``urllib`` opener at module scope
    (its PetDex redirect guard), and this handler module is imported by the
    dashboard's route table during ``start_dashboard()`` -- before the socket
    binds. A module-scope import here would therefore run that construction on
    every gateway launch, including on installs where nothing ever imports a
    pack. It is cheap, but ``no-new-work-on-gateway-boot-path`` is about the
    class, not the cost: the first request pays it instead.
    """
    from kiro_crew.apps.builtins.crew_companion.pack_transfer import MAX_BUNDLE_BYTES

    return MAX_BUNDLE_BYTES


def _sel():
    """Late-binding ``sel()``, for test monkeypatch compatibility."""
    # circular import: the handlers package re-exports this module, so its
    # ``sel()`` accessor can only be reached at call time (same shape as
    # handlers/agents.py).
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


def _audit(operation: str, outcome: str, request: web.Request, resources: str = "") -> None:
    """Record one decision or outcome on this surface. NEVER raises.

    THE single audit path for this module, and non-fatal by construction. Three
    successive review rounds found a different missing-or-misplaced obligation on
    these routes — no CSP on untrusted markup, no accepted-side permission
    record, an audit that could fail the operation it described — and every one
    was the same underlying mistake: a new HTTP surface re-deriving per-route
    obligations by hand instead of inheriting them from one place. So the audit
    is a chokepoint rather than a call each handler makes its own way.

    Non-fatal is the load-bearing half. A write handler that audits AFTER its
    mutation and lets the audit raise turns a completed delete into a 500: the
    pack is gone and the client is told the request failed, so the user retries
    and gets "no such pack". An audit describes an operation; it must never
    decide it.
    """
    try:
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
        )
    except Exception:  # pragma: no cover - an audit must not change the outcome
        logger.debug("SEL audit for %s (%s) failed", operation, outcome, exc_info=True)


async def _require_owner(request: web.Request, operation: str) -> web.Response | None:
    """Owner gate for every route on this surface, reads included.

    The library decides what the roster draws and holds third-party content
    written to the data home, so reaching it is owner-only — the same boundary
    the per-crew avatar routes use, and the identity comes from the token-auth
    middleware (``request["user"]``), never from a client-set header.

    ``require_owner_dashboard_request`` records the DENIAL and returns ``None`` on
    success, so the accepted decision is recorded here — half a decision in the
    log lets a reader see who was turned away and not who got in. The nearest
    sibling, ``GET /api/agents/{name}/avatar``, audits its successful reads the
    same way.
    """
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        return denied
    _audit(operation, "success", request)
    return None


def _not_found(message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=404)


def _bad_request(message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=400)


def _library_unavailable() -> web.Response:
    """The library directory could not be read -- a 503, not a 500.

    ``list_packs`` skips one unreadable PACK, but an unreadable ROOT (permissions
    lost, a drive gone) raises out of ``iterdir`` and would surface as an
    uncaught 500 with a traceback in the log. The condition is transient from
    the gateway's point of view and the response says so.
    """
    return web.json_response(
        {"error": "the appearance library is not readable", "code": "library_unavailable"},
        status=503,
    )


def _media_response(request: web.Request, body: bytes, content_type: str, **extra: str):
    """A cacheable media response, or a 304 when the caller already has it.

    **A pack's SVG is third-party markup served from the dashboard's OWN
    origin**, so it gets the same two headers ``handlers/files.py`` puts on an
    untrusted SVG read, for the same reason. An SVG is XML, not a bitmap: it can
    carry a ``<script>`` element, and while an ``<img src>`` will not run it,
    navigating straight to this URL — or framing it — renders it as a DOCUMENT on
    an origin that already holds the dashboard's session. A pack arrives by
    import or PetDex fetch, so its author is not necessarily the user.

    ``script-src 'none'`` is what stops that, and ``style-src 'unsafe-inline'``
    is what keeps the art drawing (a ``<style>`` element inside the SVG is how
    pack authors colour it). ``nosniff`` goes on every media answer, not just the
    SVG: it is what stops a browser re-deciding that a lottie JSON or a sprite
    PNG is really something executable.

    This is the same decision the picture tier already made by REFUSING SVG
    outright (``_sniff_image_ext`` accepts PNG/JPEG/WEBP because "none of which
    can carry active content the way SVG can"). A pack's art has to be SVG, so
    the answer here is to serve it inert rather than to refuse it.
    """
    etag = f'"{hashlib.sha256(body).hexdigest()[:32]}"'
    headers = {
        "ETag": etag,
        "Cache-Control": _MEDIA_CACHE_CONTROL,
        "X-Content-Type-Options": "nosniff",
        **extra,
    }
    if content_type == "image/svg+xml":
        headers["Content-Security-Policy"] = _SVG_CSP
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers=headers)
    return web.Response(body=body, content_type=content_type, headers=headers)


# ── reads ───────────────────────────────────────────────────────────────────


async def api_appearances_list(request: web.Request) -> web.Response:
    """GET /api/appearances — every pack in the library, metadata only.

    Built-in first, which is ``list_packs``'s own order and the order the
    gallery has always rendered.
    """
    denied = await _require_owner(request, "appearances.list")
    if denied is not None:
        return denied
    try:
        store = await asyncio.to_thread(get_appearance_store)
        packs = await asyncio.to_thread(store.list_packs)
    except OSError:
        return _library_unavailable()
    return web.json_response({"packs": packs})


async def api_appearance_detail(request: web.Request) -> web.Response:
    """GET /api/appearances/{id} — one pack with its art inlined."""
    denied = await _require_owner(request, "appearances.detail")
    if denied is not None:
        return denied
    try:
        store = await asyncio.to_thread(get_appearance_store)
        detail = await asyncio.to_thread(store.pack_detail, request.match_info["id"])
    except OSError:
        return _library_unavailable()
    if detail is None:
        # 404 rather than 400: an id that does not resolve is the normal
        # outcome of a pack the user just deleted, not a malformed request. A
        # malformed id lands here too, and telling the two apart would only hand
        # a caller probing for traversal a signal it does not need.
        return _not_found("no such appearance pack", "pack_not_found")
    return web.json_response(detail)


async def api_appearance_slot(request: web.Request) -> web.Response:
    """GET /api/appearances/{id}/slot/{slot} — ONE slot's art.

    The roster's per-crew image source. Answers ``X-Resolved-Slot`` with the
    slot actually served, so a client can tell a real ``working`` animation from
    an ``idle`` frame standing in for one.
    """
    denied = await _require_owner(request, "appearances.slot")
    if denied is not None:
        return denied
    pack_id = request.match_info["id"]
    slot = request.match_info["slot"]
    if not slot or len(slot) > _MAX_SLOT_LEN:
        return _not_found("no such slot", "slot_not_found")
    if pack_id == DEFAULT_PACK:
        # The built-in ghost's art ships inside the frontend bundle, so there is
        # nothing on disk to serve and the client draws it itself. A distinct
        # code, because "this pack has no files" is not "this pack is missing".
        return _not_found("the built-in pack is rendered by the client", "builtin_no_content")
    try:
        store = await asyncio.to_thread(get_appearance_store)
        # One manifest read and ONE file read, never ``pack_detail``: that
        # inlines every file per slot naming it, so a roster of N crews would
        # load N whole packs to draw N single frames.
        resolved = await asyncio.to_thread(pack_slot_file, store, pack_id, slot)
    except OSError:
        return _library_unavailable()
    if resolved == "no-pack":
        return _not_found("no such appearance pack", "pack_not_found")
    if resolved is None:
        return _not_found("no such slot", "slot_not_found")
    candidate, content, fmt = resolved
    body = slot_body(content, fmt)
    if body is None:
        # Stored content that will not decode: the pack lists the slot but there
        # is nothing renderable behind it, which is the same answer as absent.
        return _not_found("no such slot", "slot_not_found")
    return _media_response(request, body[0], body[1], **{"X-Resolved-Slot": candidate})


# ── writes ──────────────────────────────────────────────────────────────────


async def _bundle_from_request(request: web.Request) -> tuple[Any, web.Response | None]:
    """The bundle a caller sent, as ``(payload, error_response)``.

    Two shapes, because the gallery posts JSON and a file picker posts a file:

    * ``{"bundle": {...}}`` — exactly what
      ``/api/apps/crew-companion/appearances/import`` accepts, so the frontend
      can reuse its client code verbatim.
    * ``multipart/form-data`` with the bundle as a file part.

    The multipart read is chunked and capped at ``MAX_BUNDLE_BYTES`` rather than
    read whole: an upload's declared length is the client's claim, and buffering
    a gigabyte in order to then reject it is the failure the cap exists to
    prevent.
    """
    if request.content_type.startswith("multipart/"):
        chunks: list[bytes] = []
        total = 0
        try:
            reader = await request.multipart()
            while True:
                part = await reader.next()
                if part is None:
                    break
                if not isinstance(part, BodyPartReader):
                    continue
                while True:
                    chunk = await part.read_chunk(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _max_bundle_bytes():
                        return None, _bad_request("that bundle is too large", "bundle_too_large")
                    chunks.append(chunk)
                break
        except (ValueError, AssertionError):
            # A malformed body raises from the PART ITERATION, not from
            # `request.multipart()` — a declared boundary that never appears is
            # only discovered while looking for it. Guarding only the opening
            # call let that escape as a bare 500 with no machine-readable code.
            return None, _bad_request("could not read that upload", "invalid_bundle")
        if not chunks:
            return None, _bad_request("no bundle in that upload", "invalid_bundle")
        try:
            return json.loads(b"".join(chunks).decode("utf-8")), None
        except (RecursionError, ValueError):
            # ``ValueError`` covers ``JSONDecodeError`` and ``UnicodeDecodeError``;
            # a deeply nested document blows the parser's stack instead.
            return None, _bad_request("that file is not a pack bundle", "invalid_bundle")
    try:
        body = await request.json()
    except (LookupError, RecursionError, ValueError):
        # The same three client-input failures ``_shared.read_json_body``
        # narrows to: ``LookupError`` is an unknown ``charset=`` codec on the
        # request, which ``request.json()`` raises before parsing a byte --
        # left uncaught it is a 500 for a header the caller typed.
        return None, _bad_request("that file is not a pack bundle", "invalid_bundle")
    if not isinstance(body, dict):
        return None, _bad_request("that file is not a pack bundle", "invalid_bundle")
    return body.get("bundle"), None


async def api_appearances_import(request: web.Request) -> web.Response:
    """POST /api/appearances/import — install a pack from an exported bundle.

    The validation is ``pack_transfer.import_bundle``, untouched -- the id
    guard, the per-file and total byte caps, the manifest check and the
    refuse-rather-than-clobber rule on a colliding id all live there -- plus
    one check the store wrapper adds in front of it: every file the manifest
    references must be in the bundle (``bundle_reference_error``), so a 200
    never installs a pack whose slots the slot route cannot serve.
    """
    denied = await _require_owner(request, "appearances.import")
    if denied is not None:
        return denied
    payload, error = await _bundle_from_request(request)
    if error is not None:
        return error
    result = await import_pack(payload)
    if not result.get("ok"):
        return _bad_request(str(result.get("error", "import failed")), "invalid_bundle")
    _audit("appearances.import", "success", request, str(result.get("id", "")))
    return web.json_response(result)


async def api_appearances_petdex_fetch(request: web.Request) -> web.Response:
    """POST /api/appearances/petdex/fetch — look a pet up on PetDex.

    Delegates to the existing fetch with its host allow-list, HTTPS pin,
    redirect re-validation and download ceiling untouched: this is the one path
    in the library that reaches the public internet, and its trust boundary is
    documented where it is enforced.
    """
    denied = await _require_owner(request, "appearances.petdex_fetch")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except (LookupError, RecursionError, ValueError):
        # See _bundle_from_request: an unknown charset is client input, not a
        # server fault, and this endpoint treats an unreadable body as empty.
        body = {}
    if not isinstance(body, dict):
        body = {}
    # boot path: see _max_bundle_bytes.
    from kiro_crew.apps.builtins.crew_companion.pack_transfer import fetch_petdex_pet

    result = await asyncio.to_thread(fetch_petdex_pet, body.get("input", ""))
    _audit(
        "appearances.petdex_fetch",
        "success" if result.get("ok") else "failure",
        request,
    )
    # A miss or an unreachable registry is an expected outcome the import dialog
    # shows, not a client error, so it is a 200 carrying ok:false — the same
    # contract the app's own route has.
    return web.json_response(result)


async def api_appearance_delete(request: web.Request) -> web.Response:
    """DELETE /api/appearances/{id} — remove a custom pack.

    Refuses while a crew still wears the pack, naming the crews, because the
    alternative is a roster of blank faces the user cannot explain. ``?force=1``
    is the deliberate override: the crews keep their dangling reference and
    render the name-derived ghost, which is what an absent pack already means.

    The guard itself is ``dashboard.appearances.delete_pack_if_unworn``, and it
    applies to the crew library only. Crew Companion keeps its own library and
    its own delete route; a crew never wears a Companion pack, so that route
    needs no such check.
    """
    denied = await _require_owner(request, "appearances.delete")
    if denied is not None:
        return denied
    pack_id = request.match_info["id"]
    if pack_id == DEFAULT_PACK:
        return _bad_request("the built-in pack cannot be deleted", "builtin_pack")
    deleted, wearers = await delete_pack_if_unworn(pack_id, force=request.query.get("force") == "1")
    if wearers:
        return web.json_response(
            {
                "error": "that pack is still worn by a crew",
                "code": "pack_in_use",
                "crews": wearers,
            },
            status=409,
        )
    if not deleted:
        # `delete_pack` refuses a bad id, a linked directory and the built-in,
        # and reports False when the directory is simply not there.
        return _not_found("no such appearance pack", "pack_not_found")
    _audit("appearances.delete", "success", request, pack_id)
    return web.json_response({"ok": True, "id": pack_id})
