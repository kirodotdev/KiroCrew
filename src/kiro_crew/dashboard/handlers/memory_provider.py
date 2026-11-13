"""``/api/memory/provider``: see, switch, browse, edit and move whichever memory is active.

Every route works for every provider, Crew's own memory included.

| Route | Who may call |
|---|---|
| ``GET /`` | the owner, or an app holding ``permissions.memoryAdmin`` |
| ``PUT /`` | the owner's own dashboard session only (never an app or an agent) |
| ``GET/POST /items``, ``GET/PATCH/DELETE /items/{id}`` | owner, ``memoryAdmin`` app |
| ``POST /copy``, ``GET/DELETE /copy/{job}`` | owner, ``memoryAdmin`` app |
| ``GET /export``, ``POST /import`` | owner, ``memoryAdmin`` app |
| ``POST /conformance`` | the owner |

Agent sessions never reach these routes: none is an internal path, so the internal
secret opens nothing here, and an app token reaches them only through
``permissions.memoryAdmin`` (``token_auth_app_scope``), checked again below. Every app
call is an audit record naming the app. ``?store=`` names a configured store; the
provider gets that store id and never picks one itself.

While a provider replaces Crew's memory for a store, Crew's own memory for that store
is readable (for migration) and refuses every write with 409
``memory_provider_active``, so switching back finds it exactly as it was.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

from aiohttp import web

from kiro_crew.memory_provider import app_client, builtin, consent, copy_job, resolver
from kiro_crew.memory_provider.contract import (
    KINDS,
    MemoryProvider,
    ProviderError,
    ProviderScope,
    new_item_from_wire,
)

logger = logging.getLogger("kiro_crew.dashboard.handlers.memory_provider")

CODE_PROVIDER_ACTIVE = "memory_provider_active"


def _json_error(
    message: str, code: str, status: int = 400, detail: dict[str, Any] | None = None
) -> web.Response:
    """A refusal: English ``error``, machine ``code``, and any ``detail`` the UI shows."""
    from kiro_crew.dashboard.handlers._shared import _redact_memory_field

    return web.json_response(
        {
            "error": _redact_memory_field(message),
            "code": code,
            "detail": _redact_memory_field(detail or {}),
        },
        status=status,
    )


def _respond(body: Any) -> web.Response:
    """Every answer of these routes: provider-supplied strings pass the memory redactor.

    A provider's titles, sources, meta, write reasons and error messages are its own
    text, so the whole body is scrubbed, not just item text.
    """
    from kiro_crew.dashboard.handlers._shared import _redact_memory_field

    return web.json_response(_redact_memory_field(_scrub_meta_keys(body)))


def _scrub_meta_keys(value: Any) -> Any:
    """*value* with every item's ``meta`` keys redacted; the redactor scrubs values only.

    A key that redacts to one already present is withheld, never written over it.
    """
    from kiro_crew.dashboard.handlers._shared import _redact_memory_field

    if isinstance(value, list):
        return [_scrub_meta_keys(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {k: _scrub_meta_keys(v) for k, v in value.items()}
    meta = out.get("meta")
    if isinstance(meta, dict):
        scrubbed: dict[str, Any] = {}
        for key, item in meta.items():
            clean = str(_redact_memory_field(key))
            if clean not in scrubbed:
                scrubbed[clean] = item
        out["meta"] = scrubbed
    return out


def _audit(caller: str, operation: str, outcome: str, error: str = "", resources: str = "") -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=caller,
            operation=f"memory_provider.{operation}",
            outcome=outcome,
            source="dashboard",
            resources=resources,
            error=error,
        )
    except Exception:  # noqa: BLE001 - an audit hiccup never fails the request
        logger.debug("memory provider audit failed", exc_info=True)


def _is_owner(request: web.Request) -> bool:
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    return request.get("internal_auth") is not True and is_owner_dashboard_request(request)


def _app_holds_memory_admin(app: str) -> bool:
    from kiro_crew.apps.manager import get_app_manifest, is_app_enabled

    manifest = get_app_manifest(app)
    return bool(manifest and manifest.permissions.memoryAdmin and is_app_enabled(app))


async def _caller(request: web.Request, operation: str, *, owner_only: bool = False) -> str:
    """``"owner"`` or ``"app:<name>"``; raises a 403 response for anyone else."""
    if _is_owner(request):
        return "owner"
    app = str(request.get("app") or "")
    if (
        app
        and not owner_only
        and request.get("internal_auth") is not True
        and await asyncio.to_thread(_app_holds_memory_admin, app)
    ):
        _audit(f"app:{app}", operation, "allowed", resources=request.path)
        # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string
        return f"app:{app}"
    _audit(app and f"app:{app}" or str(request.get("user") or "unknown"), operation, "denied")
    raise web.HTTPForbidden(
        text=json.dumps(
            {
                "error": "Only the owner, or an app the owner allowed to manage memory, "
                "may use this route.",
                "code": "owner_only" if owner_only else "memory_admin_required",
            }
        ),
        content_type="application/json",
    )


def _store_name(raw: object) -> str:
    """The configured store a request names, ``""`` for the global store."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.memory_stores import memory_store_name_defect

    if raw in (None, "", "default"):
        return ""
    if not isinstance(raw, str) or memory_store_name_defect(raw) is not None:
        raise ValueError("store is not a valid memory store name")
    if raw not in KiroCrewConfig.load().memory_stores:
        raise ValueError(f"memory store {raw!r} is not configured")
    return raw


def _scope(store: str, caller: str) -> ProviderScope:
    from kiro_crew.config.loader import KiroCrewConfig

    store_cfg = KiroCrewConfig.load().memory_stores.get(store) if store else None
    return ProviderScope(
        store_id=store or "default",
        member_id=(getattr(store_cfg, "owner_member_id", "") or None),
        memory_mode="persistent",
        session_id="dashboard",
        principal=caller,
    )


def _provider_by_id(provider_id: str) -> MemoryProvider | None:
    if provider_id in ("", resolver.BUILTIN_PROVIDER_ID):
        return builtin.BUILTIN
    return app_client.provider_for_id(provider_id)


def _target(store: str, requested: str | None) -> tuple[str, MemoryProvider | None, str]:
    """``(provider id, provider, refusal)`` for a request on *store*.

    No ``?provider=`` means whichever memory answers *store* now: the provider when
    one replaces Crew's memory, otherwise Crew's own.
    """
    if requested:
        provider = _provider_by_id(requested)
        if provider is None:
            return requested, None, "the provider is not available"
        if provider is not builtin.BUILTIN:
            refusal = resolver.availability_refusal(provider, store)
            if refusal:
                return requested, None, refusal
        return requested, provider, ""
    route = resolver.route_for(store)
    if route.replaces:
        return route.provider_id, route.provider, route.unavailable
    return resolver.BUILTIN_PROVIDER_ID, builtin.BUILTIN, ""


def _builtin_dark(store: str) -> bool:
    return not resolver.builtin_writes_allowed(store)


async def provider_active_refusal(store: str) -> web.Response | None:
    """The 409 a writer of Crew's own memory answers while a provider replaces it."""
    if not await asyncio.to_thread(_builtin_dark, store):
        return None
    return _json_error(
        "A memory provider replaces Crew's memory; Crew's own memory is read-only until "
        "you switch back. Manage memory in the provider's app.",
        CODE_PROVIDER_ACTIVE,
        409,
    )


async def _resolve(
    request: web.Request, *, writing: bool
) -> tuple[str, str, MemoryProvider] | web.Response:
    try:
        store = await asyncio.to_thread(_store_name, request.query.get("store"))
    except ValueError as exc:
        return _json_error(str(exc), "invalid_store")
    provider_id, provider, refusal = await asyncio.to_thread(
        _target, store, request.query.get("provider")
    )
    if provider is None:
        return _json_error(refusal, "provider_unavailable", 503)
    if writing and provider is builtin.BUILTIN and await asyncio.to_thread(_builtin_dark, store):
        return _json_error(
            "A memory provider replaces Crew's memory for this store; Crew's own memory "
            "stays untouched until you switch back.",
            CODE_PROVIDER_ACTIVE,
            409,
        )
    return store, provider_id, provider


def _provider_error(exc: ProviderError) -> web.Response:
    status = {"read_only": 409, "not_supported": 501, "conflict": 409, "refused": 422}.get(
        exc.code, 503
    )
    return _json_error(exc.message, f"provider_{exc.code}", status)


# ── GET / PUT / ──────────────────────────────────────────────────────────────


def _describe_row(decl: app_client.DeclaredProvider, store: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": decl.provider_id,
        "app": decl.app,
        "app_version": decl.app_version,
        "display_name": decl.display_name,
        "description": decl.description,
        "settings_page": decl.settings_page,
        "enabled": decl.enabled,
    }
    provider = app_client.provider_for_id(decl.provider_id) if decl.enabled else None
    if provider is None:
        row["blocked_reason"] = "the provider app is not enabled"
        return row
    try:
        caps = provider.describe()
    except ProviderError as exc:
        row["blocked_reason"] = f"the provider did not describe itself ({exc.code})"
        row["describe_failed"] = True
        return row
    row["capabilities"] = caps.to_wire()
    row["replace_gaps"] = caps.replace_gaps()
    row["egress_consented"] = consent.has_egress_consent(provider)
    refusal = resolver.availability_refusal(provider, store)
    if refusal and refusal != resolver.CONSENT_REFUSAL:
        row["blocked_reason"] = refusal
    row["latency"] = provider.latency_summary()
    return row


def _overview(store: str, probe: bool) -> dict[str, Any]:
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig.load().memory.provider
    rows = [_describe_row(d, store) for d in app_client.declared_providers()]
    body: dict[str, Any] = {
        "mode": cfg.mode,
        "id": cfg.id,
        "timeout_secs": cfg.timeout_secs,
        "switched_at": cfg.switched_at,
        "stop_learning_ack": cfg.stop_learning_ack,
        "store": store or "default",
        "builtin": {
            "id": resolver.BUILTIN_PROVIDER_ID,
            "display_name": builtin.BUILTIN.display_name,
            "capabilities": builtin.CAPABILITIES.to_wire(),
        },
        "providers": rows,
        "shared_crew": resolver._shared_crew_on(),
        "policy_allows": resolver._governance_permits(None),
    }
    route = resolver.route_for(store)
    body["active"] = {
        "mode": route.mode,
        "id": route.provider_id,
        "unavailable": route.unavailable,
    }
    if probe and route.provider is not None:
        try:
            health = route.provider.blocking_health()  # type: ignore[attr-defined]
            body["active"]["health"] = health.to_wire()
        except ProviderError as exc:
            body["active"]["health"] = {"ok": False, "detail": exc.code}
    return body


async def api_memory_provider_get(request: web.Request) -> web.Response:
    """GET /api/memory/provider: mode, id, every installed provider, health and latency."""
    await _caller(request, "get")
    try:
        store = await asyncio.to_thread(_store_name, request.query.get("store"))
    except ValueError as exc:
        return _json_error(str(exc), "invalid_store")
    body = await asyncio.to_thread(_overview, store, request.query.get("probe") == "1")
    return _respond(body)


def _switch_refusal(body: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """``(error, code, writes)`` for a switch; ``writes`` is what to record when allowed."""
    from kiro_crew.config.loader import KiroCrewConfig

    mode = body.get("mode")
    if mode not in (resolver.MODE_BUILTIN, resolver.MODE_REPLACE, resolver.MODE_AUGMENT):
        return "mode must be builtin, replace or augment", "invalid_mode", {}
    cfg = KiroCrewConfig.load()
    current = cfg.memory.provider
    if mode == resolver.MODE_BUILTIN:
        return "", "", {"mode": mode, "id": current.id}
    provider_id = body.get("id")
    if not isinstance(provider_id, str) or app_client.split_id(provider_id) is None:
        return "id must name an installed provider (app:<app>/<provider>)", "invalid_id", {}
    provider = app_client.provider_for_id(provider_id)
    if provider is None:
        return "the provider's app is not installed or not enabled", "provider_unavailable", {}
    if resolver._shared_crew_on():
        return (
            "External memory providers are not available in a Shared Crew.",
            "shared_crew",
            {},
        )
    if not resolver._governance_permits(None):
        return "External memory providers are blocked by policy.", "policy_denied", {}
    try:
        caps = provider.describe()
    except ProviderError as exc:
        return f"the provider did not describe itself ({exc.code})", "provider_unavailable", {}
    ack = body.get("stop_learning_ack") is True
    if mode == resolver.MODE_REPLACE:
        gaps = caps.replace_gaps()
        if gaps:
            return (
                "This provider cannot replace Crew's memory: it lacks " + ", ".join(gaps),
                "replace_unsupported",
                {"gaps": gaps},
            )
        if caps.ingest == "none" and not ack:
            return (
                "This provider takes no consolidation input, so automatic learning stops. "
                "Acknowledge that to continue.",
                "stop_learning_ack_required",
                {},
            )
    if mode == resolver.MODE_AUGMENT and not caps.supports_search:
        return "A read-only source needs a provider that can search.", "augment_unsupported", {}
    pin_members: list[str] = []
    if not caps.multi_store:
        members = [
            name
            for name, store in cfg.memory_stores.items()
            if name != "default" and getattr(store, "provider_mode", "inherit") != "builtin"
        ]
        if members and body.get("pin_member_stores_builtin") is not True:
            return (
                "This provider keeps one memory. Keep each Bot's memory on Crew to continue.",
                "member_stores_must_stay_builtin",
                {"stores": members},
            )
        pin_members = members
    if caps.egress == "remote" and not consent.has_egress_consent(provider):
        if body.get("egress_consent") is not True:
            return (
                "This provider keeps memory off this machine. Agree to send it to continue.",
                "egress_consent_required",
                {},
            )
    return (
        "",
        "",
        {
            "mode": mode,
            "id": provider_id,
            "stop_learning_ack": ack,
            "pin_members": pin_members,
            "grant_egress": caps.egress == "remote" and body.get("egress_consent") is True,
            "app_version": provider.app_version,
        },
    )


async def api_memory_provider_put(request: web.Request) -> web.Response:
    """PUT /api/memory/provider: switch. The owner's own dashboard session only."""
    caller = await _caller(request, "switch", owner_only=True)
    from kiro_crew.dashboard.handlers._shared import read_bounded_json

    body, refusal = await read_bounded_json(request)
    if refusal is not None:
        return refusal
    assert body is not None
    error, code, writes = await asyncio.to_thread(_switch_refusal, body)
    if error:
        _audit(caller, "switch", "denied", error=code)
        return _json_error(error, code, 409 if code != "invalid_mode" else 400, writes)
    if writes.pop("grant_egress", False):
        await asyncio.to_thread(consent.grant_egress, writes["id"], writes["app_version"])
    writes.pop("app_version", None)
    pin_members = writes.pop("pin_members", [])
    await _write_provider_config(writes, pin_members)
    _audit(caller, "switch", "allowed", resources=f"{writes['mode']}:{writes.get('id', '')}")
    state = request.app["state"]
    try:
        state.push_refresh("memory_provider")
    except Exception:  # noqa: BLE001 - a missed refresh costs a stale card, never the switch
        logger.debug("memory provider refresh push failed", exc_info=True)
    return _respond({"ok": True, **writes, "switched_at": writes.get("switched_at")})


async def _write_provider_config(writes: dict[str, Any], pin_members: list[str]) -> None:
    from kiro_crew.config.loader import config_path, update_config_locked
    from kiro_crew.dashboard.chat_utils import run_config_write

    writes["switched_at"] = time.time()

    def _apply(data: dict) -> dict:
        memory = data.setdefault("memory", {})
        if not isinstance(memory, dict):
            raise ValueError("config.json has a 'memory' entry that is not an object")
        section = memory.get("provider")
        section = dict(section) if isinstance(section, dict) else {}
        section.update(
            {
                "mode": writes["mode"],
                "id": writes.get("id", ""),
                "switched_at": writes["switched_at"],
                "stop_learning_ack": bool(writes.get("stop_learning_ack", False)),
            }
        )
        memory["provider"] = section
        stores = data.get("memory_stores")
        if pin_members and isinstance(stores, dict):
            for name in pin_members:
                if isinstance(stores.get(name), dict):
                    stores[name]["provider_mode"] = "builtin"
        return data

    await run_config_write(update_config_locked, config_path(), mutate=_apply)


# ── Items ────────────────────────────────────────────────────────────────────


def _kinds(raw: str | None) -> frozenset[str] | None:
    if not raw:
        return None
    kinds = frozenset(k for k in raw.split(",") if k)
    if not kinds <= KINDS:
        raise ValueError("kinds must be contract kinds")
    return kinds


async def api_memory_provider_items(request: web.Request) -> web.Response:
    """GET /api/memory/provider/items: list, or search with ``?q=``."""
    caller = await _caller(request, "items.list")
    resolved = await _resolve(request, writing=False)
    if isinstance(resolved, web.Response):
        return resolved
    store, provider_id, provider = resolved
    try:
        kinds = _kinds(request.query.get("kinds"))
    except ValueError as exc:
        return _json_error(str(exc), "invalid_kinds")
    scope = _scope(store, caller)
    query = request.query.get("q", "").strip()
    if len(query) > 2000:
        return _json_error("q is at most 2000 characters", "invalid_memory_query")
    try:
        if query:
            items = await provider.search(scope, query, 50, kinds)
            body: dict[str, Any] = {"items": [i.to_wire() for i in items], "cursor": None}
        else:
            page = await provider.list(
                scope, request.query.get("cursor") or None, kinds, request.query.get("status")
            )
            body = page.to_wire()
    except ProviderError as exc:
        return _provider_error(exc)
    return _respond({**body, "provider": provider_id})


async def api_memory_provider_item(request: web.Request) -> web.Response:
    """GET / PATCH / DELETE /api/memory/provider/items/{id}."""
    method = request.method
    operation = {"GET": "items.get", "PATCH": "items.edit", "DELETE": "items.delete"}[method]
    caller = await _caller(request, operation)
    resolved = await _resolve(request, writing=method != "GET")
    if isinstance(resolved, web.Response):
        return resolved
    store, provider_id, provider = resolved
    item_id = request.match_info["id"]
    scope = _scope(store, caller)
    try:
        if method == "GET":
            item = await provider.get(scope, item_id)
            if item is None:
                return _json_error("not found", "not_found", 404)
            return _respond(({"items": [item.to_wire()]}) | {"provider": provider_id})
        if method == "DELETE":
            result = await provider.delete(scope, item_id)
            _emit_change(request, store, "removed" if result.outcome == "deleted" else None)
            return _respond({**result.to_wire(), "provider": provider_id})
        from kiro_crew.dashboard.handlers._shared import read_bounded_json

        body, refusal = await read_bounded_json(request)
        if refusal is not None:
            return refusal
        assert body is not None
        new = new_item_from_wire(body.get("item"))
        if copy_job.screen_refuses(new):
            return _respond(
                {"outcome": "refused", "reason": "content_screen", "provider": provider_id}
            )
        key = "crew-edit:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        result = await provider.put(scope, new, key[:64], item_id)
        _emit_change(request, store, "updated" if result.stored else None)
        return _respond({**result.to_wire(), "provider": provider_id})
    except ValueError as exc:
        return _json_error(str(exc), "invalid_item")
    except ProviderError as exc:
        return _provider_error(exc)


async def api_memory_provider_items_create(request: web.Request) -> web.Response:
    """POST /api/memory/provider/items: add one item."""
    caller = await _caller(request, "items.create")
    resolved = await _resolve(request, writing=True)
    if isinstance(resolved, web.Response):
        return resolved
    store, provider_id, provider = resolved
    from kiro_crew.dashboard.handlers._shared import read_bounded_json

    body, refusal = await read_bounded_json(request)
    if refusal is not None:
        return refusal
    assert body is not None
    try:
        new = new_item_from_wire(body.get("item"))
    except ValueError as exc:
        return _json_error(str(exc), "invalid_item")
    if copy_job.screen_refuses(new):
        return _respond({"outcome": "refused", "reason": "content_screen", "provider": provider_id})
    key = "crew-add:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    try:
        result = await provider.put(_scope(store, caller), new, key[:64])
    except ProviderError as exc:
        return _provider_error(exc)
    _emit_change(request, store, "added" if result.outcome == "inserted" else None)
    return _respond({**result.to_wire(), "provider": provider_id})


def _emit_change(request: web.Request, store: str, action: str | None) -> None:
    state = request.app["state"]
    try:
        state.push_refresh("lessons")
    except Exception:  # noqa: BLE001 - a missed refresh costs a stale list only
        pass
    if action is None or store:
        return
    from kiro_crew.lesson_events import LESSON_SOURCE_USER, emit_lessons_changed

    emit_lessons_changed(action, LESSON_SOURCE_USER)


# ── Copy, export, import ─────────────────────────────────────────────────────


async def api_memory_provider_copy(request: web.Request) -> web.Response:
    """POST /api/memory/provider/copy: copy one provider into another (a job)."""
    caller = await _caller(request, "copy")
    from kiro_crew.dashboard.handlers._shared import read_bounded_json

    body, refusal = await read_bounded_json(request)
    if refusal is not None:
        return refusal
    assert body is not None
    source_id = str(body.get("from") or "")
    target_id = str(body.get("to") or "")
    if not source_id or not target_id or source_id == target_id:
        return _json_error("from and to must name two different providers", "invalid_copy")
    try:
        store = await asyncio.to_thread(_store_name, body.get("store"))
    except ValueError as exc:
        return _json_error(str(exc), "invalid_store")
    dry_run = body.get("dry_run") is True
    _, source, error = await asyncio.to_thread(_target, store, source_id)
    if source is None:
        return _json_error(error, "provider_unavailable", 503)
    _, target, error = await asyncio.to_thread(_target, store, target_id)
    if target is None:
        return _json_error(error, "provider_unavailable", 503)
    if not dry_run and target is builtin.BUILTIN and await asyncio.to_thread(_builtin_dark, store):
        return _json_error(
            "Switch back to Crew memory before copying into it.", CODE_PROVIDER_ACTIVE, 409
        )
    job = copy_job.start_copy(
        source_id, target_id, source, target, _scope(store, caller), dry_run=dry_run
    )
    from kiro_crew.dashboard.handlers._shared import _redact_memory_field

    return web.json_response(_redact_memory_field(job.to_dict()), status=202)


async def api_memory_provider_copy_job(request: web.Request) -> web.Response:
    """GET (status) / DELETE (cancel) /api/memory/provider/copy/{job}."""
    await _caller(request, "copy.status")
    job_id = request.match_info["job"]
    if request.method == "DELETE":
        return _respond({"ok": copy_job.cancel_job(job_id)})
    job = copy_job.get_job(job_id)
    if job is None:
        return _json_error("no such copy job", "not_found", 404)
    return _respond(job.to_dict())


async def api_memory_provider_export(request: web.Request) -> web.StreamResponse:
    """GET /api/memory/provider/export: JSONL ``kirocrew-memory/1``."""
    caller = await _caller(request, "export")
    resolved = await _resolve(request, writing=False)
    if isinstance(resolved, web.Response):
        return resolved
    store, provider_id, provider = resolved
    response = web.StreamResponse(
        headers={
            "Content-Type": "application/x-ndjson; charset=utf-8",
            "Content-Disposition": 'attachment; filename="kirocrew-memory.jsonl"',
        }
    )
    await response.prepare(request)
    from kiro_crew.dashboard.handlers._shared import _redact_memory_field

    try:
        # Each record is scrubbed as data before it is encoded, so a redaction can
        # never break the JSON or swallow the newline that ends the record.
        async for line in copy_job.export_lines(
            provider,
            provider_id,
            _scope(store, caller),
            scrub=lambda record: _redact_memory_field(_scrub_meta_keys(record)),
        ):
            await response.write(line.encode("utf-8"))
    except ProviderError as exc:
        await response.write(
            (
                json.dumps(
                    {"error": _redact_memory_field(exc.message), "code": f"provider_{exc.code}"}
                )
                + "\n"
            ).encode()
        )
    await response.write_eof()
    return response


async def _read_capped(request: web.Request, cap: int) -> bytes:
    """The whole body, or its first ``cap + 1`` bytes so the caller can refuse it as too big.

    ``StreamReader.read(n)`` returns what is buffered, not ``n`` bytes, so it loops to EOF.
    """
    chunks: list[bytes] = []
    total = 0
    while total <= cap:
        chunk = await request.content.read(cap + 1 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


async def api_memory_provider_import(request: web.Request) -> web.Response:
    """POST /api/memory/provider/import: JSONL in, written through ``put``; ``?dry_run=1``."""
    caller = await _caller(request, "import")
    resolved = await _resolve(request, writing=request.query.get("dry_run") != "1")
    if isinstance(resolved, web.Response):
        return resolved
    store, provider_id, provider = resolved
    raw = await _read_capped(request, copy_job.MAX_IMPORT_BYTES)
    items, errors = copy_job.parse_import(raw)
    if errors and not items:
        return _json_error("; ".join(errors[:5]), "invalid_import")
    try:
        report = await copy_job.run_import(
            provider, _scope(store, caller), items, dry_run=request.query.get("dry_run") == "1"
        )
    except ProviderError as exc:
        return _provider_error(exc)
    return _respond({**report, "errors": errors[:20], "provider": provider_id})


async def api_memory_provider_conformance(request: web.Request) -> web.Response:
    """POST /api/memory/provider/conformance: run the author's kit against one provider.

    The owner may run every check. The terminal kit
    (``python -m kiro_crew.memory_provider.conformance``) reaches this route with the
    internal secret, and that caller gets the read-only checks only: the write checks
    put and delete an item, which an agent holding the same secret must not do.
    """
    internal = request.get("internal_auth") is True
    if not internal:
        await _caller(request, "conformance", owner_only=True)
    from kiro_crew.dashboard.handlers._shared import read_bounded_json
    from kiro_crew.memory_provider import conformance

    body, refusal = await read_bounded_json(request)
    if refusal is not None:
        return refusal
    assert body is not None
    provider_id = str(body.get("provider") or "")
    provider = await asyncio.to_thread(_provider_by_id, provider_id)
    if provider is None:
        return _json_error("the provider is not available", "provider_unavailable", 503)
    _audit("cli" if internal else "owner", "conformance", "allowed", resources=provider_id)
    report = await conformance.run(provider, write=not internal and body.get("write") is True)
    return _respond(report)
