"""Personal Shopper — backend API routes.

Registered at gateway startup by ``apps/routes.py:register_app_routes``
(loaded via the app's ``backend.routes`` manifest field).

Routes (browser-facing, same-origin authed):

  GET  /api/apps/personal-shopper/preferences        -> list all preferences
  POST /api/apps/personal-shopper/preferences        -> add a preference
  PUT  /api/apps/personal-shopper/preferences/{id}   -> update a preference
  DELETE /api/apps/personal-shopper/preferences/{id} -> delete a preference
  POST /api/apps/personal-shopper/preferences/search -> RAG search
  POST /api/apps/personal-shopper/preferences/reembed -> rebuild missing vectors

  GET  /api/apps/personal-shopper/groups             -> list groups
  POST /api/apps/personal-shopper/groups             -> add a group
  DELETE /api/apps/personal-shopper/groups/{id}      -> delete a group

  GET  /api/apps/personal-shopper/history            -> list history
  POST /api/apps/personal-shopper/history            -> add history entry
  PUT  /api/apps/personal-shopper/history/{id}/feedback -> update feedback

  GET  /api/apps/personal-shopper/sites              -> get sites config
  PUT  /api/apps/personal-shopper/sites              -> update sites config
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import wraps
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.personal_shopper.backend.store import PreferenceStore
from kiro_crew.apps.manager import app_data_dir, is_app_enabled
from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

APP_NAME = "personal-shopper"
_PREFIX = f"/api/apps/{APP_NAME}"

# Built on first request, not at import: constructing the store creates the data
# directory and runs sqlite DDL, which must not happen merely because the module
# was imported (a test or a CLI import would write into the real data home).
_store: PreferenceStore | None = None
_store_lock = asyncio.Lock()


async def _get_store() -> PreferenceStore:
    """Return the store singleton, building it off the event loop.

    Construction touches the filesystem and runs sqlite DDL, so it goes through
    ``to_thread`` rather than blocking the loop that is also serving every other
    gateway request. The lock makes concurrent first requests build it once; the
    second ``is None`` check inside the lock is what makes that true, since two
    coroutines can both pass the outer check before either takes the lock.
    """
    global _store
    if _store is None:
        async with _store_lock:
            if _store is None:
                _store = await asyncio.to_thread(PreferenceStore)
    return _store


def _bad_request(error: str, code: str) -> web.Response:
    """A 400 carrying both prose and a machine-readable code.

    The dashboard renders ``error`` verbatim into a localized UI, so ``code`` is
    the contract and the prose is advisory (RFC 9457 3.1.3).
    """
    return web.json_response({"error": error, "code": code}, status=400)


def _reject_non_finite(value: str) -> Any:
    """Refuse ``NaN`` / ``Infinity`` / ``-Infinity``.

    Python's json module accepts those three as an extension, and dumps them back
    out verbatim -- but they are not valid JSON, so ``JSON.parse`` in the browser
    throws on the stored file. A single NaN price therefore makes the whole
    sites/history record unreadable to the dashboard, hiding data that is still
    on disk. Rejecting at the boundary keeps them from ever being persisted.
    """
    raise ValueError(f"non-finite number not allowed: {value}")


def _strict_loads(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_non_finite)


async def _json_object(
    request: web.Request,
) -> tuple[dict[str, Any] | None, web.Response | None]:
    """Parse the body and require it to be a JSON object.

    Returns ``(body, None)`` or ``(None, error_response)``. A bare scalar or
    array parses fine as JSON but has no ``.get``, so without this check every
    handler would raise ``AttributeError`` and return a 500 to a client that
    merely sent the wrong shape.
    """
    try:
        body = await request.json(loads=_strict_loads)
    except (json.JSONDecodeError, ValueError):
        return None, _bad_request("invalid JSON", "invalid_json")
    if not isinstance(body, dict):
        return None, _bad_request("body must be a JSON object", "body_not_object")
    return body, None


def _opt_str(body: dict[str, Any], key: str) -> tuple[str | None, web.Response | None]:
    """Read an optional string field, rejecting a non-string outright.

    ``{"text": 1}`` must not reach ``.strip()`` -- that raises and turns a client
    type error into a server error.
    """
    if key not in body or body[key] is None:
        return None, None
    value = body[key]
    if not isinstance(value, str):
        return None, _bad_request(f"{key} must be a string", "invalid_field_type")
    return value, None


def _req_str(body: dict[str, Any], key: str) -> tuple[str, web.Response | None]:
    """Read a required, non-empty string field."""
    value, err = _opt_str(body, key)
    if err is not None:
        return "", err
    if not (value or "").strip():
        return "", _bad_request(f"{key} is required", "missing_required_field")
    return (value or "").strip(), None


def _opt_str_list(
    body: dict[str, Any], key: str
) -> tuple[list[str] | None, web.Response | None]:
    """Read an optional list-of-strings field.

    Element types are checked too: the store writes tags into FTS and a non-string
    element would fail deep inside sqlite rather than at the boundary.
    """
    if key not in body or body[key] is None:
        return None, None
    value = body[key]
    if not isinstance(value, list):
        return None, _bad_request(f"{key} must be an array", "invalid_field_type")
    if not all(isinstance(v, str) for v in value):
        return None, _bad_request(
            f"{key} must contain only strings", "invalid_field_type"
        )
    return value, None


def _require_enabled(handler):
    """Deny requests when Personal Shopper is disabled."""

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response(
                {"error": "personal-shopper is disabled", "code": "app_disabled"},
                status=403,
            )
        return await handler(request)

    return _wrapped


def _check_records(
    items: list[Any], key: str, spec: dict[str, tuple[type, ...]], required: tuple[str, ...]
) -> web.Response | None:
    """Validate each record's fields against the shape the frontend renders.

    Returns an error response, or ``None`` when every record is acceptable. The
    outer "is it a dict" check is not enough: an object-valued ``name`` is a dict
    too, and React throws when it is handed an object as a child -- a malformed
    request would take down the app page for every later visit, because the bad
    value is persisted.
    """
    for index, item in enumerate(items):
        for field in required:
            if field not in item:
                return _bad_request(
                    f"{key}[{index}].{field} is required", "missing_required_field"
                )
        for field, value in item.items():
            allowed = spec.get(field)
            if allowed is None:
                continue  # forward-compatible: unknown fields are not rendered
            # bool is a subclass of int, so a numeric field must exclude it or
            # ``price: true`` would format as the number 1.
            if isinstance(value, bool) and bool not in allowed:
                return _bad_request(
                    f"{key}[{index}].{field} has the wrong type", "invalid_field_type"
                )
            if not isinstance(value, allowed):
                return _bad_request(
                    f"{key}[{index}].{field} has the wrong type", "invalid_field_type"
                )
    return None


# Field shapes the UI renders. `price` is formatted through fmtCurrency, so it
# must be a real number; `name`/`url` are rendered as text.
_PRODUCT_SPEC: dict[str, tuple[type, ...]] = {
    "name": (str,),
    "url": (str,),
    "price": (int, float),
    "feedback": (str,),
}
_SITE_SPEC: dict[str, tuple[type, ...]] = {
    "id": (str,),
    "name": (str,),
    "url": (str,),
    "enabled": (bool,),
    "loggedIn": (bool,),
}


# ── Preferences ──


async def _handle_list_preferences(request: web.Request) -> web.Response:
    store = await _get_store()
    prefs = await asyncio.to_thread(store.list_all)
    return web.json_response({"preferences": prefs})


async def _handle_add_preference(request: web.Request) -> web.Response:
    body, err = await _json_object(request)
    if err is not None:
        return err
    assert body is not None

    text, err = _req_str(body, "text")
    if err is not None:
        return err

    tags, err = _opt_str_list(body, "tags")
    if err is not None:
        return err

    store = await _get_store()
    entry_id = await asyncio.to_thread(store.add, text, tags=tags or [])
    return web.json_response({"id": entry_id}, status=201)


async def _handle_update_preference(request: web.Request) -> web.Response:
    entry_id = request.match_info["id"]
    body, err = await _json_object(request)
    if err is not None:
        return err
    assert body is not None

    text, err = _opt_str(body, "text")
    if err is not None:
        return err

    tags, err = _opt_str_list(body, "tags")
    if err is not None:
        return err

    store = await _get_store()
    await asyncio.to_thread(store.update, entry_id, text=text, tags=tags)
    return web.json_response({"id": entry_id, "updated": True})


async def _handle_delete_preference(request: web.Request) -> web.Response:
    entry_id = request.match_info["id"]
    store = await _get_store()
    await asyncio.to_thread(store.delete, entry_id)
    return web.json_response({"id": entry_id, "deleted": True})


async def _handle_search_preferences(request: web.Request) -> web.Response:
    body, err = await _json_object(request)
    if err is not None:
        return err
    assert body is not None

    query, err = _req_str(body, "query")
    if err is not None:
        return err

    # A bool is an int in Python, so ``isinstance(True, int)`` passes -- exclude it
    # explicitly rather than letting ``top_k=true`` become a LIMIT of 1.
    top_k = body.get("top_k", 5)
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        return _bad_request("top_k must be an integer", "invalid_field_type")
    if not 1 <= top_k <= 100:
        return _bad_request("top_k must be between 1 and 100", "invalid_field_type")

    tag_filter, err = _opt_str_list(body, "tag_filter")
    if err is not None:
        return err

    store = await _get_store()
    results = await asyncio.to_thread(
        store.search, query, top_k=top_k, tag_filter=tag_filter
    )
    return web.json_response(
        {
            # `semantic` tells the caller whether `score` is a cosine similarity
            # or a keyword-rank ordering, so a client never thresholds a keyword
            # score as though it measured meaning.
            "semantic": bool(results and results[0].semantic),
            "results": [
                {
                    "id": r.id,
                    "text": r.text,
                    "tags": r.tags,
                    "score": r.score,
                    "semantic": r.semantic,
                }
                for r in results
            ],
        }
    )


async def _handle_reembed_preferences(request: web.Request) -> web.Response:
    """Rebuild vectors for entries that have none.

    Reachable because the store deliberately drops a vector whenever no model is
    serving -- the normal state on a fresh install, before the embedding model has
    been downloaded. Without a rebuild path those entries would keep scoring 0
    even after the model lands, leaving semantic search permanently dormant for
    everything added in the meantime.
    """
    store = await _get_store()
    count = await asyncio.to_thread(store.reembed_all)
    return web.json_response({"reembedded": count})


# ── Groups ──


async def _handle_list_groups(request: web.Request) -> web.Response:
    store = await _get_store()
    groups = await asyncio.to_thread(store.list_groups)
    return web.json_response({"groups": groups})


async def _handle_add_group(request: web.Request) -> web.Response:
    body, err = await _json_object(request)
    if err is not None:
        return err
    assert body is not None

    name, err = _req_str(body, "name")
    if err is not None:
        return err

    icon, err = _opt_str(body, "icon")
    if err is not None:
        return err

    store = await _get_store()
    group_id = await asyncio.to_thread(store.add_group, name, icon=icon or "")
    return web.json_response({"id": group_id}, status=201)


async def _handle_delete_group(request: web.Request) -> web.Response:
    group_id = request.match_info["id"]
    store = await _get_store()
    await asyncio.to_thread(store.delete_group, group_id)
    return web.json_response({"id": group_id, "deleted": True})


# ── History ──


async def _handle_list_history(request: web.Request) -> web.Response:
    # ``?limit=abc`` must be a 400, not an uncaught ValueError -> 500.
    raw_limit = request.query.get("limit", "20")
    try:
        limit = int(raw_limit)
    except ValueError:
        return _bad_request("limit must be an integer", "invalid_field_type")
    if not 1 <= limit <= 500:
        return _bad_request("limit must be between 1 and 500", "invalid_field_type")

    store = await _get_store()
    history = await asyncio.to_thread(store.list_history, limit=limit)
    return web.json_response({"sessions": history})


async def _handle_add_history(request: web.Request) -> web.Response:
    body, err = await _json_object(request)
    if err is not None:
        return err
    assert body is not None

    problem, err = _req_str(body, "problem")
    if err is not None:
        return err

    advice, err = _opt_str(body, "advice")
    if err is not None:
        return err

    products = body.get("products", [])
    if not isinstance(products, list):
        return _bad_request("products must be an array", "invalid_field_type")
    if not all(isinstance(item, dict) for item in products):
        return _bad_request(
            "products must contain only objects", "invalid_field_type"
        )
    err = _check_records(products, "products", _PRODUCT_SPEC, required=("name",))
    if err is not None:
        return err

    store = await _get_store()
    entry_id = await asyncio.to_thread(
        store.add_history, problem, advice=advice or "", products=products
    )
    return web.json_response({"id": entry_id}, status=201)


async def _handle_update_feedback(request: web.Request) -> web.Response:
    history_id = request.match_info["id"]
    body, err = await _json_object(request)
    if err is not None:
        return err
    assert body is not None

    product_name, err = _req_str(body, "product")
    if err is not None:
        return err

    feedback, err = _req_str(body, "feedback")
    if err is not None:
        return err

    # Only the three states the UI can render; anything else would persist a
    # value that displays as a raw string in every locale.
    if feedback not in ("liked", "purchased", "skipped"):
        return _bad_request(
            "feedback must be liked, purchased or skipped", "invalid_field_type"
        )

    store = await _get_store()
    await asyncio.to_thread(store.update_feedback, history_id, product_name, feedback)
    return web.json_response({"id": history_id, "updated": True})


# ── Sites ──


def _sites_path() -> Path:
    """Resolve the sites file under the ACTIVE data home.

    Deferred to call time so ``KIROCREW_HOME`` is honoured: a module-level
    constant binds whichever home was set at import, which sends a pod's or a
    test's writes into the real user's data.
    """
    return app_data_dir(APP_NAME) / "sites.json"


async def _handle_get_sites(request: web.Request) -> web.Response:
    def _read():
        path = _sites_path()
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"sites": []}

    data = await asyncio.to_thread(_read)
    return web.json_response(data)


async def _handle_put_sites(request: web.Request) -> web.Response:
    body, err = await _json_object(request)
    if err is not None:
        return err
    assert body is not None

    sites = body.get("sites")
    if not isinstance(sites, list):
        return _bad_request("sites must be an array", "invalid_field_type")
    if not all(isinstance(item, dict) for item in sites):
        return _bad_request("sites must contain only objects", "invalid_field_type")
    err = _check_records(sites, "sites", _SITE_SPEC, required=("id", "name", "url"))
    if err is not None:
        return err

    def _write():
        path = _sites_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic temp-file + rename. A plain write_text truncates the existing
        # file before the new bytes land, so an interruption mid-save leaves an
        # empty or partial file and the next read raises -- the user's whole store
        # configuration is gone rather than merely un-updated.
        atomic_write(
            path, json.dumps(body, indent=2, ensure_ascii=False), fsync=True
        )

    await asyncio.to_thread(_write)
    return web.json_response({"updated": True})


# ── Registration ──


def register_routes(app: web.Application) -> None:
    """Register Personal Shopper routes on the gateway's aiohttp Application."""
    # Preferences
    app.router.add_get(
        f"{_PREFIX}/preferences", _require_enabled(_handle_list_preferences)
    )
    app.router.add_post(
        f"{_PREFIX}/preferences", _require_enabled(_handle_add_preference)
    )
    app.router.add_put(
        f"{_PREFIX}/preferences/{{id}}", _require_enabled(_handle_update_preference)
    )
    app.router.add_delete(
        f"{_PREFIX}/preferences/{{id}}", _require_enabled(_handle_delete_preference)
    )
    app.router.add_post(
        f"{_PREFIX}/preferences/search", _require_enabled(_handle_search_preferences)
    )
    app.router.add_post(
        f"{_PREFIX}/preferences/reembed", _require_enabled(_handle_reembed_preferences)
    )
    # Groups
    app.router.add_get(f"{_PREFIX}/groups", _require_enabled(_handle_list_groups))
    app.router.add_post(f"{_PREFIX}/groups", _require_enabled(_handle_add_group))
    app.router.add_delete(
        f"{_PREFIX}/groups/{{id}}", _require_enabled(_handle_delete_group)
    )
    # History
    app.router.add_get(f"{_PREFIX}/history", _require_enabled(_handle_list_history))
    app.router.add_post(f"{_PREFIX}/history", _require_enabled(_handle_add_history))
    app.router.add_put(
        f"{_PREFIX}/history/{{id}}/feedback",
        _require_enabled(_handle_update_feedback),
    )
    # Sites
    app.router.add_get(f"{_PREFIX}/sites", _require_enabled(_handle_get_sites))
    app.router.add_put(f"{_PREFIX}/sites", _require_enabled(_handle_put_sites))
