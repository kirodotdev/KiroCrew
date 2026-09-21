"""HTTP surface of the contribution protocol (§3, §4, §5, §7).

Four routes, all kind-generic -- ``{kind}`` is a path segment resolved against
the ``eventlog.contrib`` unit registry, so a second unit kind is a
``register_unit`` call and not another copy of these handlers::

    GET  /api/eventlog/{kind}/{id}/events?after=&limit=
    POST /api/eventlog/{kind}/{id}/events
    POST /api/eventlog/{kind}/{id}/projections/{key}
    POST /api/eventlog/{kind}/{id}/projections/{key}/schema

Authority comes from the caller's app token and its manifest ``contributions``
declaration, re-derived per request in ``eventlog.grants``. A DASHBOARD-USER
token is refused on every one of these: the gateway's own writes go through the
service directly, so the only legitimate caller here is a contributor, and
admitting the operator's browser would make "the gateway is the only writer"
untrue through a route nothing needs.

Every error carries the contract's §9 machine-readable ``code``.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import web

from kiro_crew.eventlog import grants
from kiro_crew.eventlog.contrib import (
    STATUS_FOR_CODE,
    ContribError,
    assert_grants_unchanged,
    check_event_data,
    check_projection_value,
    get_budget,
    get_store,
    normalize_schema,
    resolve_unit,
)

logger = logging.getLogger(__name__)

#: ``limit`` bounds for the catch-up read (contract §3).
_LIMIT_MIN = 1
_LIMIT_MAX = 500
_LIMIT_DEFAULT = 200


def _sel():
    """Late-binding SEL accessor, matching the other handlers' monkeypatch seam."""
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


def _audit(app: str, operation: str, outcome: str, resources: str, error: str = "") -> None:
    """Record one contribution decision. Never changes the outcome."""
    try:
        _sel().log_api_access(
            caller=app or "<dashboard-user>",
            operation=operation,
            outcome=outcome,
            source="contribution_protocol",
            resources=resources,
            error=error,
        )
    except Exception:  # pragma: no cover - audit must not change the answer
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


def _err(code: str, message: str) -> web.Response:
    """One contract §9 error response: coded body, status derived from the code.

    Written as one ``json_response`` per status, each with a LITERAL status and an
    INLINE body dict. Both shapes are deliberate: the repo's error-code contract
    test proves statically that every error response carries a machine-readable
    ``code``, and it can follow neither a computed status nor a body hoisted into a
    local -- so the compliant-but-invisible one-liner would read as a new hole in
    that gate. The code-to-status mapping lives in ``contrib.STATUS_FOR_CODE``, so
    a raise site names only a code and this function stays the only place the wire
    status is decided.
    """
    status = STATUS_FOR_CODE.get(code, 400)
    if status == 403:
        return web.json_response({"error": message, "code": code}, status=403)
    if status == 404:
        return web.json_response({"error": message, "code": code}, status=404)
    if status == 409:
        return web.json_response({"error": message, "code": code}, status=409)
    if status == 413:
        return web.json_response({"error": message, "code": code}, status=413)
    if status == 429:
        return web.json_response({"error": message, "code": code}, status=429)
    return web.json_response({"error": message, "code": code}, status=400)


def _contributor(request: web.Request, operation: str, resources: str) -> str | web.Response:
    """The calling app's name, or a refusal.

    Two gates, in order: the caller must BE an app (a dashboard-user token has no
    business appending on an app's behalf), and that app must have declared
    ``contributions`` at all. Both answer 403 with a coded body rather than 404 --
    unlike the members surface, this route's existence is public in the protocol
    document, so hiding it buys nothing and an unexplained 404 would send a
    contributor author looking for a routing bug.
    """
    app = request.get("app", "")
    if not app:
        _audit("", operation, "denied", resources, error="not an app token")
        return _err(
            "unit_kind_not_granted",
            "the contribution protocol is for app tokens; a dashboard session writes "
            "through the gateway's own surfaces",
        )
    if not grants.declares_contributions(app):
        _audit(app, operation, "denied", resources, error="no contributions declared")
        return _err(
            "unit_kind_not_granted",
            "this app declares no contributions in its manifest",
        )
    return app


def _parse_int(raw: str, code: str, message: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise ContribError(code, message) from exc


async def api_eventlog_events_get(request: web.Request) -> web.Response:
    """GET /api/eventlog/{kind}/{id}/events?after=&limit= -- catch-up read (§3).

    Oldest first, ``seq > after``. ``after`` defaults to -1 (from the beginning);
    ``limit`` is clamped to 1..500 (default 200) and a bad value is refused with a
    coded 400 rather than silently substituted -- a consumer that asked for 5000
    and received 200 without being told would read a short page as the end of the
    log and stop folding.
    """
    kind = request.match_info["kind"]
    unit_id = request.match_info["id"]
    resources = f"{kind}/{unit_id}/events"
    app = _contributor(request, "eventlog.read", resources)
    if isinstance(app, web.Response):
        return app
    try:
        # Authorization FIRST, before the unit is resolved: resolve_unit answers
        # `unit_not_found` for a missing unit while an ungranted kind answers
        # `unit_kind_not_granted`, so resolving first lets an app with no grant on
        # this kind probe IDs and read existence off the two different codes.
        if not grants.may_use_kind(app, kind):
            raise ContribError("unit_kind_not_granted", f"this app may not read {kind} units")
        # Offloaded for the same reason the read below is: proving the unit has a
        # log calls last_seq(), which for a slug this process has not touched
        # loads and folds the whole ledger. On the serving loop that is a large
        # synchronous file read holding every other request and the heartbeat.
        unit = await asyncio.to_thread(resolve_unit, kind, unit_id)
        raw_after = request.query.get("after", "")
        after = -1
        if raw_after != "":
            after = _parse_int(raw_after, "invalid_after", "after must be an integer")
            if after < -1:
                raise ContribError("invalid_after", "after must be -1 or greater")
        raw_limit = request.query.get("limit", "")
        limit = _LIMIT_DEFAULT
        if raw_limit != "":
            limit = _parse_int(
                raw_limit, "invalid_limit", f"limit must be an integer {_LIMIT_MIN}..{_LIMIT_MAX}"
            )
            if limit < _LIMIT_MIN or limit > _LIMIT_MAX:
                raise ContribError("invalid_limit", f"limit must be {_LIMIT_MIN}..{_LIMIT_MAX}")
    except ContribError as exc:
        _audit(app, "eventlog.read", "denied", resources, error=exc.code)
        return _err(exc.code, str(exc))

    def _read() -> tuple[list, int]:
        svc = unit.service()
        return svc.events_after(unit_id, after=after, limit=limit), svc.last_seq(unit_id)

    events, last_seq = await asyncio.to_thread(_read)
    _audit(app, "eventlog.read", "granted", resources)
    # Network-boundary redaction, the same chain the sibling member `/history`
    # and `/activity` reads run. Event `data` carries agent-authored free-text
    # (an activity `project`, message previews) and `events_after` returns it raw,
    # so a granted contributor catch-up read would otherwise leak a credential or
    # presigned URL smuggled into an event that the member HTTP reads scrub.
    from kiro_crew.eventlog.service import _redact_projection_value

    redacted = [
        (
            {**e, "data": _redact_projection_value(e["data"])}
            if isinstance(e, dict) and isinstance(e.get("data"), dict)
            else e
        )
        for e in events
    ]
    return web.json_response(
        {
            "kind": kind,
            unit.id_field: unit_id,
            "id": unit_id,
            "events": redacted,
            "lastSeq": last_seq,
        }
    )


async def api_eventlog_events_post(request: web.Request) -> web.Response:
    """POST /api/eventlog/{kind}/{id}/events -- append one event (§4).

    The gateway assigns ``seq`` and ``time``; a contributor supplying either is
    ignored rather than refused, because the envelope it gets back carries the
    authoritative values and a refusal would only teach it to strip fields it
    already cannot influence.
    """
    kind = request.match_info["kind"]
    unit_id = request.match_info["id"]
    resources = f"{kind}/{unit_id}/events"
    app = _contributor(request, "eventlog.append", resources)
    if isinstance(app, web.Response):
        return app
    try:
        body = await request.json()
    except ValueError:
        return _err("invalid_projection_value", "body must be a JSON object")
    if not isinstance(body, dict):
        return _err("invalid_projection_value", "body must be a JSON object")

    event_type = body.get("type", "")
    charged_at = 0.0
    try:
        # Authorization FIRST: see the read handler -- resolving before the grant
        # check turns the differing `unit_not_found` / `unit_kind_not_granted`
        # codes into an existence oracle for an app with no grant on this kind.
        # Read BEFORE the grant check, for the same reason as the publish and
        # schema handlers: a revoke landing between the two is invisible to a
        # fence read afterwards.
        fence = grants.revocation_generation()
        if not grants.may_use_kind(app, kind):
            raise ContribError("unit_kind_not_granted", f"this app may not append to {kind} units")
        # Offloaded: resolve_unit proves the unit HAS a log, and for a slug this
        # process has not touched that walks the store and folds the ledger --
        # synchronous file I/O that would stall every gateway task and the
        # heartbeat with it if it ran on the serving loop.
        unit = await asyncio.to_thread(resolve_unit, kind, unit_id)
        if not isinstance(event_type, str) or not event_type:
            raise ContribError("event_type_not_owned", "type is required")
        if not grants.may_append(app, kind, event_type):
            raise ContribError(
                "event_type_not_owned",
                f"{event_type!r} is not covered by this app's declared "
                "contributions.events patterns",
            )
        data = body.get("data", {})
        check_event_data(data)
        # Charged before the write: over budget is refused, never queued. The
        # clock is captured so a failed append releases the day it charged.
        charged_at = time.time()
        get_budget().charge(app, kind, unit_id, now=charged_at)
    except ContribError as exc:
        _audit(app, "eventlog.append", "denied", f"{resources}:{event_type}", error=exc.code)
        return _err(exc.code, str(exc))

    def _append() -> dict:
        # Fenced HERE, in the thread that writes, rather than on the loop before
        # the offload: a check before the suspension leaves the suspension
        # between the check and the commit, which is the window itself. This is
        # the append counterpart of the assertion `publish` and `put_schema` make
        # inside their per-unit lock.
        assert_grants_unchanged(fence)
        return unit.service().append(unit_id, event_type, data)

    try:
        event = await asyncio.to_thread(_append)
    except ContribError as exc:
        # The fence refused, so nothing was written: hand the reservation back
        # exactly as the failure path below does, but report the refusal under
        # its own code. Folding it into the generic branch would answer 404
        # `unit_not_found` for a unit that exists and is found, and would record
        # a deliberate refusal as a disk failure in the audit stream.
        get_budget().release(app, kind, unit_id, now=charged_at)
        _audit(app, "eventlog.append", "denied", f"{resources}:{event_type}", error=exc.code)
        return _err(exc.code, str(exc))
    except Exception as exc:
        # Nothing committed, so the reservation is handed back: the budget
        # counts events that exist, and a run of disk failures must not leave a
        # contributor refused with quota_exceeded for writes that never landed.
        get_budget().release(app, kind, unit_id, now=charged_at)
        logger.warning("eventlog append failed for %s/%s", kind, unit_id, exc_info=True)
        _audit(app, "eventlog.append", "failed", f"{resources}:{event_type}", error=str(exc))
        return _err("unit_not_found", f"append failed: {exc}")

    _audit(app, "eventlog.append", "granted", f"{resources}:{event_type}")
    return web.json_response(event, status=201)


async def api_eventlog_projection_put(request: web.Request) -> web.Response:
    """POST /api/eventlog/{kind}/{id}/projections/{key} -- publish a view (§5).

    204 on success. The stored row is pushed to dashboards as this kind's own
    whole-value frame (``member_projection`` for a member), which is why a
    contributed card renders with no new client path.
    """
    kind = request.match_info["kind"]
    unit_id = request.match_info["id"]
    key = request.match_info["key"]
    resources = f"{kind}/{unit_id}/projections/{key}"
    app = _contributor(request, "eventlog.publish", resources)
    if isinstance(app, web.Response):
        return app
    try:
        body = await request.json()
    except ValueError:
        return _err("invalid_projection_value", "body must be a JSON object")
    if not isinstance(body, dict):
        return _err("invalid_projection_value", "body must be a JSON object")

    try:
        # Read BEFORE the grant check, not after: a revoke landing between the
        # two would otherwise be invisible to the fence, which is the window the
        # fence exists to close rather than a narrower version of it.
        fence = grants.revocation_generation()
        # Authorization FIRST: see the read handler -- resolving before the grant
        # check lets an app with no claim on this key read unit existence off the
        # differing `unit_not_found` / `projection_key_not_owned` codes.
        if not grants.may_publish(app, kind, key):
            raise ContribError(
                "projection_key_not_owned",
                f"{key!r} is not covered by this app's declared "
                "contributions.projections patterns",
            )
        unit = await asyncio.to_thread(resolve_unit, kind, unit_id)
        if "value" not in body:
            raise ContribError("invalid_projection_value", "value is required")
        value = body["value"]
        check_projection_value(value)
        raw_seq = body.get("seq", -1)
        raw_version = body.get("stateVersion", 0)
        if isinstance(raw_seq, bool) or not isinstance(raw_seq, int):
            raise ContribError("invalid_projection_value", "seq must be an integer")
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise ContribError("invalid_projection_value", "stateVersion must be an integer")
        if raw_seq < -1:
            raise ContribError("invalid_projection_value", "seq must be -1 or greater")
        if raw_version < 0:
            raise ContribError("invalid_projection_value", "stateVersion must be 0 or greater")
        result = await asyncio.to_thread(
            get_store().publish,
            kind,
            unit_id,
            key,
            app=app,
            value=value,
            seq=raw_seq,
            state_version=raw_version,
            expect_generation=fence,
        )
    except ContribError as exc:
        _audit(app, "eventlog.publish", "denied", resources, error=exc.code)
        return _err(exc.code, str(exc))

    _push_projection(
        request, unit, unit_id, key, result.row.value, result.row.seq, result.row.schema
    )
    _audit(
        app,
        "eventlog.publish",
        "granted",
        resources,
        error="stateVersion override" if result.by_state_version else "",
    )
    return web.Response(status=204)


async def api_eventlog_projection_schema_put(request: web.Request) -> web.Response:
    """POST /api/eventlog/{kind}/{id}/projections/{key}/schema -- rendering (§7).

    Declares how a dashboard should render this key's value: a ``kind`` from the
    small closed set, an optional ``title``, and optional ``path`` selectors.
    Unknown fields are dropped rather than stored -- the browser renders from
    this, so an unrecognised field would be a rendering the host never agreed to.
    """
    kind = request.match_info["kind"]
    unit_id = request.match_info["id"]
    key = request.match_info["key"]
    resources = f"{kind}/{unit_id}/projections/{key}/schema"
    app = _contributor(request, "eventlog.schema", resources)
    if isinstance(app, web.Response):
        return app
    try:
        body = await request.json()
    except ValueError:
        return _err("invalid_projection_value", "body must be a JSON object")

    try:
        # Read BEFORE the grant check, not after: a revoke landing between the
        # two would otherwise be invisible to the fence, which is the window the
        # fence exists to close rather than a narrower version of it.
        fence = grants.revocation_generation()
        # Authorization FIRST: see the read handler -- resolving before the grant
        # check lets an app with no claim on this key read unit existence off the
        # differing `unit_not_found` / `projection_key_not_owned` codes.
        if not grants.may_publish(app, kind, key):
            raise ContribError(
                "projection_key_not_owned",
                f"{key!r} is not covered by this app's declared "
                "contributions.projections patterns",
            )
        unit = await asyncio.to_thread(resolve_unit, kind, unit_id)
        schema = normalize_schema(body)
        row = await asyncio.to_thread(
            get_store().put_schema,
            kind,
            unit_id,
            key,
            app=app,
            schema=schema,
            expect_generation=fence,
        )
    except ContribError as exc:
        _audit(app, "eventlog.schema", "denied", resources, error=exc.code)
        return _err(exc.code, str(exc))

    # Re-push the row so a dashboard already holding the value picks up the
    # rendering without waiting for the contributor's next fold. Skipped for a
    # schema published before any value: there is nothing to render yet.
    if row.seq >= 0:
        _push_projection(request, unit, unit_id, key, row.value, row.seq, row.schema)
    _audit(app, "eventlog.schema", "granted", resources)
    return web.Response(status=204)


def _push_projection(
    request: web.Request,
    unit,
    unit_id: str,
    key: str,
    value,
    seq: int,
    schema: dict | None,
) -> None:
    """Broadcast one contributed row on this kind's whole-value frame.

    Best-effort: the row is already durable, so a broadcast fault costs a
    dashboard one stale card until the next publish, not correctness.
    """
    state = request.app.get("state")
    broadcast = getattr(state, "broadcast_ws", None)
    if broadcast is None:
        return
    # Same network-boundary redaction as the fold broadcast (`_on_change`), the
    # roster seed and the catch-up read: a contributed value can carry a
    # credential or presigned URL, so scrub it before it crosses live to the
    # browser -- otherwise it leaks until the next page reload re-reads it redacted.
    from kiro_crew.eventlog.service import _redact_projection_value

    payload: dict = {
        unit.id_field: unit_id,
        "key": key,
        "value": _redact_projection_value(value),
        "seq": seq,
    }
    if schema is not None:
        # The schema crosses the same network boundary as ``value`` and is
        # app-authored (a ``title`` and path selectors), so a credential- or
        # URL-shaped string in it must be scrubbed with the same chain rather
        # than reaching the browser unredacted.
        payload["schema"] = _redact_projection_value(schema)
    try:
        broadcast(unit.frame, payload)
    except Exception:
        logger.debug("contributed projection push failed for %s/%s", unit_id, key, exc_info=True)
