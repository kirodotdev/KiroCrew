"""``POST /api/apps/{name}/local-copy``: owner only, typed body, refusal is 400.

Requests go through ``register_app_routes``, so the production route table is
exercised; only ``make_local_copy`` is stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import routes

pytestmark = pytest.mark.asyncio

OWNER = "owner-subject"
APP = "market-app"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def _make_local_copy(name: str, **kwargs: Any) -> Any:
        seen.append({"name": name, **kwargs})
        ok = kwargs.get("confirm_replace")
        return SimpleNamespace(
            ok=bool(ok),
            error="" if ok else "needs an explicit confirm",
            error_code="" if ok else "app_local_copy_confirm_required",
            to_dict=lambda: {"ok": bool(ok)},
        )

    monkeypatch.setattr(routes, "make_local_copy", _make_local_copy)
    monkeypatch.setattr(routes, "sel", lambda: SimpleNamespace(log_api_access=lambda **k: None))
    return seen


async def _post(user: str, app_claim: str, body: Any) -> int:
    @web.middleware
    async def _claims(request: web.Request, handler: Any) -> web.StreamResponse:
        request["user"] = user
        request["app"] = app_claim
        return await handler(request)

    app = web.Application(middlewares=[_claims])
    app["state"] = SimpleNamespace(owner_id=OWNER)
    routes.register_app_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/apps/{APP}/local-copy", json=body)
        await resp.read()
        return resp.status


async def test_owner_same_name_with_confirm(calls: list[dict[str, Any]]) -> None:
    assert await _post(OWNER, "", {"confirmReplace": True}) == 200
    assert calls == [{"name": APP, "confirm_replace": True}]


async def test_owner_without_confirm_is_a_400(calls: list[dict[str, Any]]) -> None:
    assert await _post(OWNER, "", {}) == 400


@pytest.mark.parametrize(("user", "app_claim"), [("someone-else", ""), (OWNER, APP)])
async def test_non_owner_and_app_token_are_refused(
    calls: list[dict[str, Any]], user: str, app_claim: str
) -> None:
    assert await _post(user, app_claim, {"confirmReplace": True}) == 403
    assert calls == []


@pytest.mark.parametrize(
    "body",
    [{"source": "/x", "confirmReplace": True}, {"confirmReplace": "yes"}, ["x"]],
)
async def test_bad_body_is_refused_before_any_change(
    calls: list[dict[str, Any]], body: Any
) -> None:
    assert await _post(OWNER, "", body) == 400
    assert calls == []
