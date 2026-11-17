"""``GET /api/kas-login/sources`` sits behind the dashboard's token check.

The route names the accounts stored on this machine, so a request with no
dashboard credential must be refused by ``token_auth_middleware`` before the
handler runs, exactly as ``GET /api/kas-login`` is.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_request,
    clear_nonces,
)

from kiro_crew.dashboard.token_auth import token_auth_middleware


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/kas-login/sources", "/api/kas-login"])
async def test_a_request_with_no_credential_is_refused(path: str) -> None:
    reached = []

    async def handler(request: web.Request) -> web.Response:
        reached.append(request.path)
        return web.Response(text="ok")

    response = await token_auth_middleware()(_make_request(path=path), handler)
    assert response.status in (401, 403)
    assert reached == []


@pytest.mark.asyncio
async def test_a_bad_credential_is_refused() -> None:
    reached = []

    async def handler(request: web.Request) -> web.Response:
        reached.append(request.path)
        return web.Response(text="ok")

    request = _make_request(path="/api/kas-login/sources", query={"token": "not-a-token"})
    response = await token_auth_middleware()(request, handler)
    assert response.status in (401, 403)
    assert reached == []
