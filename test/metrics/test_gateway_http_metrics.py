"""Tests for the privacy-safe, bounded-cardinality gateway HTTP metrics (rec #1).

Covers ``kiro_crew.metrics.http_metrics``:

* ``kirocrew.gateway.boot.duration``   — boot-to-ready histogram
* ``kirocrew.gateway.request.duration`` — per-route latency histogram

The headline guarantee under test is BOUNDED CARDINALITY: no concrete path,
id, query string, or body ever becomes a metric label. The per-route label is
the aiohttp route TEMPLATE (``/api/items/{item_id}``), and any request that does
not match a known template collapses to a single ``__unknown__`` sentinel — so
hammering an endpoint with N distinct ids yields exactly ONE ``route_template``
label value, proven here against real OpenTelemetry data points.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import ClientPayloadError, web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.metrics.http_metrics import (
    BOOT_METRIC,
    REQUEST_METRIC,
    UNKNOWN_ROUTE,
    collect_route_templates,
    make_route_latency_middleware,
    method_label,
    record_boot_to_ready,
    route_template,
    status_class,
)


# ---------------------------------------------------------------------------
# Capturing recorder (mirrors the repo's other metric tests)
# ---------------------------------------------------------------------------
class _CapturingRecorder:
    def __init__(self) -> None:
        self.histograms: list[tuple[str, float, str, dict]] = []

    def histogram(
        self, name, value, *, unit="ms", attrs=None, **kwargs
    ) -> None:
        self.histograms.append((name, value, unit, dict(attrs or {})))


# ---------------------------------------------------------------------------
# status_class / method_label — fixed low-cardinality domains
# ---------------------------------------------------------------------------
class TestStatusClass:
    @pytest.mark.parametrize(
        "code,expected",
        [
            (100, "1xx"),
            (200, "2xx"),
            (204, "2xx"),
            (302, "3xx"),
            (404, "4xx"),
            (403, "4xx"),
            (500, "5xx"),
            (503, "5xx"),
            (999, "other"),
            (0, "other"),
        ],
    )
    def test_buckets(self, code, expected):
        assert status_class(code) == expected

    def test_non_numeric_is_other(self):
        assert status_class("abc") == "other"
        assert status_class(None) == "other"

    def test_domain_is_bounded(self):
        """Only 6 possible values can ever be produced."""
        produced = {status_class(c) for c in range(-50, 700)}
        produced |= {status_class(x) for x in ("x", None, 3.5)}
        assert produced <= {"1xx", "2xx", "3xx", "4xx", "5xx", "other"}


class TestMethodLabel:
    @pytest.mark.parametrize(
        "method,expected",
        [
            ("GET", "GET"),
            ("get", "GET"),
            ("POST", "POST"),
            ("PATCH", "PATCH"),
            ("DELETE", "DELETE"),
            ("HEAD", "HEAD"),
            ("OPTIONS", "OPTIONS"),
            ("PUT", "PUT"),
        ],
    )
    def test_known_methods(self, method, expected):
        assert method_label(method) == expected

    def test_unknown_method_clamped(self):
        assert method_label("TRACE") == "OTHER"
        assert method_label("connect") == "OTHER"
        assert method_label("") == "OTHER"
        assert method_label("'; DROP TABLE--") == "OTHER"


# ---------------------------------------------------------------------------
# collect_route_templates / route_template — the bounding mechanism
# ---------------------------------------------------------------------------
class TestCollectRouteTemplates:
    def test_returns_canonical_templates_not_paths(self):
        async def _h(_r):
            return web.Response()

        app = web.Application()
        app.router.add_get("/api/health", _h)
        app.router.add_get("/api/items/{item_id}", _h)
        app.router.add_post("/api/items/{item_id}/tags/{tag}", _h)

        templates = collect_route_templates(app)

        assert "/api/health" in templates
        assert "/api/items/{item_id}" in templates
        assert "/api/items/{item_id}/tags/{tag}" in templates
        # Templates carry placeholders, never concrete ids.
        assert all("{" in t or t == "/api/health" for t in templates)
        assert isinstance(templates, frozenset)


class TestRouteTemplateGating:
    def _req(self, canonical):
        req = MagicMock(spec=web.Request)
        resource = MagicMock()
        resource.canonical = canonical
        req.match_info.route.resource = resource
        return req

    def test_known_template_returned(self):
        known = frozenset({"/api/items/{item_id}"})
        assert route_template(self._req("/api/items/{item_id}"), known) == (
            "/api/items/{item_id}"
        )

    def test_unknown_template_collapses_to_sentinel(self):
        known = frozenset({"/api/items/{item_id}"})
        # A canonical NOT in the known set (e.g. a route added post-capture)
        # must not leak as its own label.
        assert route_template(self._req("/api/secret/{x}"), known) == UNKNOWN_ROUTE

    def test_missing_resource_is_sentinel(self):
        known = frozenset({"/api/items/{item_id}"})
        req = MagicMock(spec=web.Request)
        req.match_info.route.resource = None
        assert route_template(req, known) == UNKNOWN_ROUTE


# ---------------------------------------------------------------------------
# record_boot_to_ready
# ---------------------------------------------------------------------------
class TestBootToReady:
    def test_emits_histogram_with_privacy_safe_labels(self):
        rec = _CapturingRecorder()
        with patch(
            "kiro_crew.metrics.http_metrics.get_recorder", return_value=rec
        ):
            record_boot_to_ready(1234.5, server="dashboard")
        assert rec.histograms == [
            (BOOT_METRIC, 1234.5, "ms", {"server": "dashboard", "outcome": "ready"})
        ]

    def test_api_server_label(self):
        rec = _CapturingRecorder()
        with patch(
            "kiro_crew.metrics.http_metrics.get_recorder", return_value=rec
        ):
            record_boot_to_ready(50.0, server="api", outcome="ready")
        assert rec.histograms[0][3]["server"] == "api"

    def test_negative_and_none_skip(self):
        rec = _CapturingRecorder()
        with patch(
            "kiro_crew.metrics.http_metrics.get_recorder", return_value=rec
        ):
            record_boot_to_ready(-1.0, server="dashboard")
            record_boot_to_ready(None, server="dashboard")  # type: ignore[arg-type]
        assert rec.histograms == []

    def test_recorder_failure_swallowed(self):
        boom = MagicMock()
        boom.histogram.side_effect = RuntimeError("telemetry down")
        with patch(
            "kiro_crew.metrics.http_metrics.get_recorder", return_value=boom
        ):
            # Must not raise.
            record_boot_to_ready(10.0, server="dashboard")


# ---------------------------------------------------------------------------
# Middleware integration — real aiohttp request pipeline
# ---------------------------------------------------------------------------
async def _boom(_request):
    raise RuntimeError("handler exploded")


async def _ok_item(request):
    # Echo the id INTO THE BODY (not a label) to prove the id never leaks into
    # a metric attribute even when the handler itself uses it.
    return web.json_response({"id": request.match_info["item_id"]})


async def _ok_health(_request):
    return web.json_response({"ok": True})


async def _websocket(request):
    response = web.WebSocketResponse()
    await response.prepare(request)
    async for _message in response:
        pass
    return response


async def _sse(request):
    response = web.StreamResponse(
        status=200,
        headers={"Content-Type": "text/event-stream"},
    )
    await response.prepare(request)
    await response.write(b"data: complete\n\n")
    await response.write_eof()
    return response


async def _sse_error(request):
    response = web.StreamResponse(
        status=200,
        headers={"Content-Type": "text/event-stream"},
    )
    await response.prepare(request)
    await response.write(b"data: started\n\n")
    raise RuntimeError("stream interrupted")


def _build_app(rec):
    app = web.Application()
    app.router.add_get("/api/health", _ok_health)
    app.router.add_get("/api/items/{item_id}", _ok_item)
    app.router.add_get("/api/boom", _boom)
    app.router.add_get("/api/ws", _websocket)
    app.router.add_get("/api/events", _sse)
    app.router.add_get("/api/events-error", _sse_error)
    # Latency middleware is the only middleware — captures templates lazily.
    app.middlewares.append(make_route_latency_middleware())
    return app


@pytest.mark.asyncio
async def test_middleware_records_template_not_concrete_path():
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.http_metrics.get_recorder", return_value=rec):
        app = _build_app(rec)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/items/abc123")
            assert resp.status == 200

    assert rec.histograms, "a request histogram must be emitted"
    name, value, unit, attrs = rec.histograms[-1]
    assert name == REQUEST_METRIC
    assert unit == "ms"
    assert value >= 0
    assert attrs["method"] == "GET"
    assert attrs["route_template"] == "/api/items/{item_id}"
    assert attrs["status_class"] == "2xx"
    # Privacy: the concrete id must NOT appear anywhere in the labels.
    assert "abc123" not in attrs.values()
    assert all("abc123" not in str(v) for v in attrs.values())


@pytest.mark.asyncio
async def test_middleware_unmatched_path_is_sentinel():
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.http_metrics.get_recorder", return_value=rec):
        app = _build_app(rec)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/totally/unknown/42")
            assert resp.status == 404

    _, _, _, attrs = rec.histograms[-1]
    assert attrs["route_template"] == UNKNOWN_ROUTE
    assert attrs["status_class"] == "4xx"
    assert "42" not in str(attrs.values())


@pytest.mark.asyncio
async def test_middleware_handler_error_records_5xx_and_propagates():
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.http_metrics.get_recorder", return_value=rec):
        app = _build_app(rec)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/boom")
            # aiohttp turns the unhandled error into a 500 for the client.
            assert resp.status == 500

    name, _, _, attrs = rec.histograms[-1]
    assert name == REQUEST_METRIC
    assert attrs["route_template"] == "/api/boom"
    assert attrs["status_class"] == "5xx"


@pytest.mark.asyncio
async def test_middleware_excludes_websocket_connection_lifetime():
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.http_metrics.get_recorder", return_value=rec):
        app = _build_app(rec)
        async with TestClient(TestServer(app)) as client:
            websocket = await client.ws_connect("/api/ws")
            await websocket.close()

    assert rec.histograms == []


@pytest.mark.asyncio
async def test_middleware_excludes_sse_stream_lifetime():
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.http_metrics.get_recorder", return_value=rec):
        app = _build_app(rec)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/events")
            assert response.status == 200
            assert response.content_type == "text/event-stream"
            assert await response.text() == "data: complete\n\n"

    assert rec.histograms == []


@pytest.mark.asyncio
async def test_middleware_excludes_sse_lifetime_when_handler_raises():
    rec = _CapturingRecorder()
    with patch("kiro_crew.metrics.http_metrics.get_recorder", return_value=rec):
        app = _build_app(rec)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/events-error")
            assert response.status == 200
            assert response.content_type == "text/event-stream"
            with pytest.raises(ClientPayloadError):
                await response.read()

    assert rec.histograms == []


@pytest.mark.asyncio
async def test_telemetry_failure_never_breaks_request():
    boom = MagicMock()
    boom.histogram.side_effect = RuntimeError("telemetry down")
    with patch("kiro_crew.metrics.http_metrics.get_recorder", return_value=boom):
        app = _build_app(None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/health")
            # Request still succeeds despite the recorder raising.
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True}


# ---------------------------------------------------------------------------
# Bounded-cardinality PROOF against real OpenTelemetry data points
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bounded_cardinality_under_many_distinct_ids():
    """Hammering /api/items/{id} with 100 distinct ids must yield exactly ONE
    ``route_template`` label value — the template — proving concrete ids can
    never explode the metric's cardinality. Verified through the REAL
    MetricsRecorder + OTEL InMemoryMetricReader (namespace/redaction path)."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from kiro_crew.metrics.recorder import MetricsRecorder

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    real_recorder = MetricsRecorder(provider.get_meter("test"))

    with patch(
        "kiro_crew.metrics.http_metrics.get_recorder", return_value=real_recorder
    ):
        app = _build_app(None)
        async with TestClient(TestServer(app)) as client:
            for i in range(100):
                r = await client.get(f"/api/items/id-{i}-{'x' * (i % 7)}")
                assert r.status == 200

    # Pull every data point for the request histogram and collect the distinct
    # route_template label values.
    data = reader.get_metrics_data()
    templates: set[str] = set()
    total_points = 0
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != REQUEST_METRIC:
                    continue
                for dp in metric.data.data_points:
                    total_points += 1
                    templates.add(dp.attributes.get("route_template"))

    # 100 distinct concrete paths → exactly ONE template label value.
    assert templates == {"/api/items/{item_id}"}
    # And the concrete ids appear in NO label anywhere.
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for dp in metric.data.data_points:
                    for v in dp.attributes.values():
                        assert "id-" not in str(v)


# ---------------------------------------------------------------------------
# Wiring guard — the real server entrypoints import the helpers
# ---------------------------------------------------------------------------
def test_server_module_wires_http_metrics():
    """Regression guard: server.py must import both helpers so the middleware
    and boot metric stay wired into the gateway."""
    import kiro_crew.dashboard.server as srv

    assert hasattr(srv, "make_route_latency_middleware")
    assert hasattr(srv, "record_boot_to_ready")
