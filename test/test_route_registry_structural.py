"""A ``routes`` hook may return its own route objects, not only ``AppRoute``.

An installed app cannot import ``kiro_crew`` without pinning itself to one wheel's
private layout, so ``RouteRegistry`` reads any object with a ``method`` / ``path`` /
``handler`` triple. These tests pin which objects are accepted, that an accepted one
serves exactly like an ``AppRoute``, and that everything else is skipped with the
existing warning rather than registered.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import kiro_crew.apps.route_registry as route_registry_module
from kiro_crew.apps.context import AppContext
from kiro_crew.apps.route_registry import AppRoute, RouteRegistry, _route_fields

APP = "structural-probe"


@dataclass(frozen=True)
class OwnRoute:
    """The shape an app declares for itself, with no host import."""

    method: str
    path: str
    handler: Any


async def _ok(request: web.Request, ctx: AppContext) -> web.Response:
    return web.json_response({"app": ctx.name, "params": dict(request.match_info)})


@pytest.fixture
def sel_rows(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    rows: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: rows.append(kw)
    monkeypatch.setattr(route_registry_module, "sel", lambda: fake)
    return rows


async def _register(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, routes: list[Any]
) -> tuple[RouteRegistry, AppContext, list[str]]:
    monkeypatch.setattr(
        route_registry_module, "load_app_module", lambda *_a, **_k: lambda _ctx: routes
    )
    registry = RouteRegistry(MagicMock())
    ctx = AppContext(name=APP, data_dir=tmp_path)
    described = await registry.register_app_routes(APP, tmp_path, "backend/routes.py", ctx)
    return registry, ctx, described


def _request(method: str, path: str) -> web.Request:
    return make_mocked_request(
        method,
        f"/api/apps/{APP}/{path}",
        match_info={"app_name": APP, "path": path},
    )


class TestRouteFields:
    def test_app_route_is_read_as_before(self) -> None:
        assert _route_fields(AppRoute("get", "/x", _ok)) == ("GET", "/x", _ok)

    @pytest.mark.parametrize("method", ["GET", "post", "Put", "DELETE", "patch"])
    def test_own_dataclass_with_a_known_method_is_accepted(self, method: str) -> None:
        assert _route_fields(OwnRoute(method, "/x", _ok)) == (method.upper(), "/x", _ok)

    def test_any_object_with_the_three_attributes_is_accepted(self) -> None:
        route = SimpleNamespace(method="GET", path="/y", handler=_ok)
        assert _route_fields(route) == ("GET", "/y", _ok)

    @pytest.mark.parametrize(
        "route",
        [
            OwnRoute("OPTIONS", "/x", _ok),
            OwnRoute("HEAD", "/x", _ok),
            OwnRoute(b"GET", "/x", _ok),  # type: ignore[arg-type]
            OwnRoute("GET", b"/x", _ok),  # type: ignore[arg-type]
            OwnRoute("GET", "/" + "a" * 512, _ok),
            OwnRoute("GET", "/x", "not callable"),
            SimpleNamespace(method="GET", path="/x"),
            ("GET", "/x", _ok),
            {"method": "GET", "path": "/x", "handler": _ok},
            None,
        ],
        ids=[
            "options",
            "head",
            "bytes-method",
            "bytes-path",
            "path-over-cap",
            "handler-not-callable",
            "no-handler",
            "tuple",
            "dict",
            "none",
        ],
    )
    def test_anything_else_is_refused(self, route: Any) -> None:
        assert _route_fields(route) is None

    def test_the_path_cap_is_inclusive(self) -> None:
        path = "/" + "a" * 511
        assert _route_fields(OwnRoute("GET", path, _ok)) == ("GET", path, _ok)


class TestRegistration:
    @pytest.mark.asyncio
    async def test_own_routes_register_and_dispatch_like_app_routes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sel_rows: list[dict]
    ) -> None:
        registry, _ctx, described = await _register(
            monkeypatch,
            tmp_path,
            [
                OwnRoute("get", "status", _ok),
                OwnRoute("POST", "/items/{item_id}", _ok),
                AppRoute("GET", "/legacy", _ok),
            ],
        )

        assert described == [
            f"GET /api/apps/{APP}/status",
            f"POST /api/apps/{APP}/items/{{item_id}}",
            f"GET /api/apps/{APP}/legacy",
        ]
        resp = await registry.dispatch(_request("GET", "status"))
        assert resp.status == 200
        assert json.loads(resp.body)["app"] == APP
        resp = await registry.dispatch(_request("POST", "items/42"))
        assert resp.status == 200
        resp = await registry.dispatch(_request("GET", "legacy"))
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_refused_items_are_skipped_with_a_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.route_registry"):
            registry, ctx, described = await _register(
                monkeypatch,
                tmp_path,
                [OwnRoute("TRACE", "/x", _ok), OwnRoute("GET", "/ok", _ok), "junk"],
            )

        assert described == [f"GET /api/apps/{APP}/ok"]
        skipped = [r for r in caplog.records if "skipping non-route item" in r.getMessage()]
        assert len(skipped) == 2
        assert ctx.health.status == "healthy"
        assert [r.path for r in registry._routes[APP]] == ["/ok"]
