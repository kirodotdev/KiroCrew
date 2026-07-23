"""Gateway HTTP observability — boot-to-ready + bounded per-route latency (rec #1).

Two privacy-safe, BOUNDED-CARDINALITY signals for the aiohttp gateway
(``dashboard/server.py`` — both the full dashboard and the headless ``--slack-only``
API server):

  * ``kirocrew.gateway.boot.duration`` — a histogram (ms) of how long
    ``start_dashboard`` / ``start_api_server`` take from process-relative
    ``start_time`` until the server is fully initialised and about to accept
    traffic. Attrs: ``server`` (``dashboard`` / ``api``), ``outcome`` (``ready``).

  * ``kirocrew.gateway.request.duration`` — a histogram (ms) of per-request
    handling latency. Attrs: ``method``, ``route_template``, ``status_class``.

PRIVACY (rec #1 hard rule): these metrics NEVER carry prompts, message content,
tokens, real request paths, query strings, user ids, or secrets. The only
request-derived labels are:

  * ``method``          — clamped to a fixed HTTP-verb allowlist (else ``OTHER``).
  * ``route_template``  — the *matched route's* aiohttp canonical TEMPLATE
                          (e.g. ``/api/artifacts/{slug}``), NOT the concrete path
                          (``/api/artifacts/abc123``). The ``{slug}`` placeholder
                          is a constant baked into the route table, so no id /
                          path segment / free-form value ever becomes a label.
  * ``status_class``    — the HTTP status bucketed to ``1xx``..``5xx`` / ``other``.

BOUNDED CARDINALITY (structural guarantee):
  The set of route templates is captured ONCE at server-build time from the
  registered route table (:func:`collect_route_templates`). At request time
  :func:`route_template` returns a template ONLY IF it is in that frozen set;
  anything else — an unmatched 404 (aiohttp ``SystemRoute``), or a route added
  after capture — collapses to the single sentinel :data:`UNKNOWN_ROUTE`.
  Therefore the number of distinct ``route_template`` label values is bounded by
  ``len(known_templates) + 1``, a constant fixed at startup that CANNOT grow with
  traffic. Combined with the fixed ``method`` allowlist (<= 8) and the fixed
  ``status_class`` domain (6), total series for the request histogram are bounded
  by ``(len(known_templates) + 1) * 8 * 6`` — no unbounded label can ever leak in.

OSS-CLEAN: depends only on ``aiohttp`` + the first-party metrics facade.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Callable, FrozenSet, Optional

from aiohttp import web

from kiro_crew.metrics.provider import get_recorder

if TYPE_CHECKING:  # annotation-only; avoids any import-time coupling
    from collections.abc import Awaitable

logger = logging.getLogger(__name__)

# --- Metric names (core kirocrew.* namespace; validated by the recorder) ------
BOOT_METRIC = "kirocrew.gateway.boot.duration"
REQUEST_METRIC = "kirocrew.gateway.request.duration"

# Sentinel for any request that does NOT match a route template known at
# server-build time. Keeping ONE sentinel (never the raw path) is what bounds
# the ``route_template`` label cardinality.
UNKNOWN_ROUTE = "__unknown__"

# Fixed HTTP-verb allowlist. Any other/forged method clamps to ``OTHER`` so the
# ``method`` label can never carry a free-form value.
_KNOWN_METHODS: FrozenSet[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)


def method_label(method: str) -> str:
    """Clamp an HTTP method to the fixed allowlist (else ``OTHER``)."""
    up = (method or "").upper()
    return up if up in _KNOWN_METHODS else "OTHER"


def status_class(status: object) -> str:
    """Bucket an HTTP status code into a low-cardinality class.

    Returns ``1xx``/``2xx``/``3xx``/``4xx``/``5xx`` or ``other`` — a fixed
    6-value domain, so ``status_class`` can never explode cardinality.
    """
    try:
        code = int(status)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return "other"
    if 100 <= code < 600:
        return f"{code // 100}xx"
    return "other"


def collect_route_templates(app: "web.Application") -> FrozenSet[str]:
    """Snapshot every registered route's canonical TEMPLATE from *app*.

    Enumerates the router's resources and reads each ``resource.canonical`` —
    the template form (``/api/artifacts/{slug}``), never a concrete path. Call
    this AFTER all routes are registered (just before assembling the middleware
    chain) so the returned frozen set is the complete, bounded universe of
    templates the per-route latency middleware is allowed to emit.
    """
    templates: set[str] = set()
    try:
        for resource in app.router.resources():
            canonical = getattr(resource, "canonical", None)
            if isinstance(canonical, str) and canonical:
                templates.add(canonical)
    except Exception:  # never let telemetry setup break server startup
        logger.debug("collect_route_templates failed", exc_info=True)
    return frozenset(templates)


def route_template(
    request: "web.Request", known: FrozenSet[str]
) -> str:
    """Return the request's matched route TEMPLATE, or :data:`UNKNOWN_ROUTE`.

    The template is taken from the matched route's ``resource.canonical`` and is
    returned ONLY when it is a member of *known* (the set captured at build
    time). Unmatched requests (aiohttp ``SystemRoute`` → no resource) and any
    template not in *known* map to the single sentinel. This is the mechanism
    that bounds the ``route_template`` label domain to ``len(known) + 1``.
    """
    try:
        match_info = request.match_info
        route = getattr(match_info, "route", None)
        resource = getattr(route, "resource", None)
        canonical = getattr(resource, "canonical", None)
        if isinstance(canonical, str) and canonical in known:
            return canonical
    except Exception:
        logger.debug("route_template resolution failed", exc_info=True)
    return UNKNOWN_ROUTE


def record_boot_to_ready(
    duration_ms: float, *, server: str, outcome: str = "ready"
) -> None:
    """Emit the ``kirocrew.gateway.boot.duration`` histogram (best-effort).

    Args:
        duration_ms: Wall-clock ms from the server's ``start_time`` to ready.
        server: Which entrypoint — ``dashboard`` or ``api``. Low-cardinality.
        outcome: ``ready`` on success. Low-cardinality.
    """
    if duration_ms is None or duration_ms < 0:
        return
    try:
        get_recorder().histogram(
            BOOT_METRIC,
            float(duration_ms),
            unit="ms",
            attrs={"server": server, "outcome": outcome},
            description="Gateway boot-to-ready duration (ms).",
        )
    except Exception:  # telemetry must never break startup
        logger.debug("record_boot_to_ready failed", exc_info=True)


def make_route_latency_middleware(
    known: Optional[FrozenSet[str]] = None,
) -> Callable:
    """Build the per-route latency aiohttp middleware.

    *known* is the bounded universe of route templates (see
    :func:`collect_route_templates`). Pass it explicitly when the full route
    table is already registered; otherwise leave it ``None`` and the middleware
    captures the set LAZILY from ``request.app`` on the first request (after
    startup, when every route — including any registered after the middleware
    chain is assembled — is present) and caches it in the closure for the
    process lifetime. Either way the middleware only ever labels a request with
    a member of that fixed set or :data:`UNKNOWN_ROUTE`, so cardinality is
    structurally bounded regardless of the concrete paths clients request.

    The lazy path caches in a closure cell rather than on ``request.app``
    because aiohttp freezes the ``Application`` mapping on ``runner.setup()`` —
    writing ``app[...]`` at request time would raise.

    The middleware is best-effort: it measures latency, records the histogram in
    a ``finally`` (so both success and error responses are timed), and NEVER
    lets a telemetry failure alter the response — the original response is
    returned and the original exception re-raised unchanged.
    """
    # Mutable closure cell: {"known": <frozenset or None>}. aiohttp's frozen
    # Application mapping forbids caching on app[...] post-setup, so we cache
    # here instead.
    _cache: dict[str, Optional[FrozenSet[str]]] = {"known": known}

    @web.middleware  # type: ignore[misc]
    async def route_latency_middleware(
        request: "web.Request",
        handler: "Callable[[web.Request], Awaitable[web.StreamResponse]]",
    ) -> "web.StreamResponse":
        templates = _cache["known"]
        if templates is None:
            templates = collect_route_templates(request.app)
            _cache["known"] = templates
        start = time.monotonic()
        output_size_before = request.writer.output_size
        status: object = 500
        response: web.StreamResponse | None = None
        try:
            response = await handler(request)
            status = getattr(response, "status", 200)
            return response
        except web.HTTPException as http_exc:
            # aiohttp signals many normal outcomes (404, 302, 403, ...) as
            # exceptions — capture their real status before re-raising.
            status = getattr(http_exc, "status", 500)
            raise
        except Exception:
            status = 500
            raise
        finally:
            # WebSocket and SSE handlers return only when their upgraded/streamed
            # connection closes, so elapsed time is connection/turn lifetime rather
            # than HTTP request latency. A handler that raises after preparing a
            # response never assigns ``response`` above, but the public writer byte
            # count proves that headers/body were already committed. Exclude that
            # partial-response lifetime too instead of polluting the highest bucket.
            response_committed = (
                response is None
                and request.writer.output_size > output_size_before
            )
            stream_lifetime = response_committed or isinstance(
                response, web.WebSocketResponse
            ) or (
                isinstance(response, web.StreamResponse)
                and response.content_type == "text/event-stream"
            )
            if not stream_lifetime:
                try:
                    elapsed_ms = (time.monotonic() - start) * 1000.0
                    get_recorder().histogram(
                        REQUEST_METRIC,
                        elapsed_ms,
                        unit="ms",
                        attrs={
                            "method": method_label(request.method),
                            "route_template": route_template(request, templates),
                            "status_class": status_class(status),
                        },
                        description="Gateway per-route request latency (ms).",
                    )
                except Exception:  # never break the request path for telemetry
                    logger.debug("route latency record failed", exc_info=True)

    # Tag so server.py can assert/introspect the middleware if needed.
    route_latency_middleware._is_route_latency = True  # type: ignore[attr-defined]
    return route_latency_middleware
