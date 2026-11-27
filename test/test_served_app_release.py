"""A served aiohttp Application is released by the per-test cache reset.

aiohttp memoizes each app's middleware chain in a module-level ``lru_cache`` keyed on
the Application, so without the reset every app a test served, its app state and the
mocks in it, stay alive for the rest of the worker.
"""

from __future__ import annotations

import gc
import weakref

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from conftest_resets import forget_home_bound_caches


async def _serve_one_request() -> weakref.ref[web.Application]:
    @web.middleware
    async def passthrough(request: web.Request, handler):
        return await handler(request)

    async def ok(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(middlewares=[passthrough])
    app.router.add_get("/", ok)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/")
        assert response.status == 200
    return weakref.ref(app)


@pytest.mark.asyncio
async def test_a_served_app_is_released_by_the_per_test_reset() -> None:
    from aiohttp import web_app

    if not hasattr(getattr(web_app, "_cached_build_middleware", None), "cache_clear"):
        pytest.skip("this aiohttp has no middleware lru_cache to hold a served app")
    served = await _serve_one_request()
    gc.collect()
    assert served() is not None, "the middleware cache no longer holds a served app"

    forget_home_bound_caches()
    gc.collect()
    assert served() is None, "a served Application outlived the per-test reset"
