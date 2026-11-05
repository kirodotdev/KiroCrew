"""``POST /api/crons/tools`` hands the caller's own session token to the tool it runs.

A cron tool can call back into the gateway: ``cron_add`` with a folder name that
does not exist yet creates it through ``POST /api/cron-folders``. That nested
request names the caller's session, and the gateway accepts a named session only
behind an attestation. The tool runs inside the gateway with no token of its own
in the environment, so the route must forward the token the caller presented, and
only one that verifies to the session the request was already verified as.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import mcp_core, mcp_cron, member_memory_auth, session_token_sig
from kiro_crew.dashboard.handlers import cron
from kiro_crew.mcp_caller import current_caller

pytestmark = pytest.mark.asyncio

_KEY = "dashboard:alice"
_GOOD = "token-for-alice"


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    rows: list[dict] = []

    def _local(name: str, arguments: dict) -> str:
        ctx = current_caller()
        assert ctx is not None
        rows.append(
            {
                "session_key": ctx.session_key,
                "session_token": ctx.session_token,
                "header": mcp_core._session_token_header(),
            }
        )
        return "ok"

    async def _store(request: web.Request, state: object, operation: str):  # type: ignore[no-untyped-def]
        return "", None

    monkeypatch.setattr(mcp_cron, "_call_tool_locally", _local)
    monkeypatch.setattr(cron, "resolve_lesson_memory_store", _store)
    monkeypatch.setattr(
        member_memory_auth,
        "memory_request_identity",
        lambda request: (request.headers.get("X-Session-Key", ""), True),
    )
    monkeypatch.setattr(
        session_token_sig,
        "verify_session_token",
        lambda token: _KEY if token == _GOOD else "bob" if token == "token-for-bob" else "",
    )
    monkeypatch.delenv("KIROCREW_STUB_SESSION_TOKEN", raising=False)
    return rows


def _app() -> web.Application:
    @web.middleware
    async def _internal(request: web.Request, handler):  # type: ignore[no-untyped-def]
        request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[_internal])
    app["state"] = SimpleNamespace(push_refresh=MagicMock())
    app.router.add_post("/api/crons/tools", cron.api_cron_tools)
    return app


async def _call(headers: dict[str, str]) -> int:
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post(
            "/api/crons/tools",
            json={"name": "cron_list", "arguments": {}},
            headers={"X-Session-Key": _KEY, **headers},
        )
        return resp.status


async def test_a_verified_token_reaches_the_tool_and_its_nested_requests(seen: list[dict]) -> None:
    assert await _call({"X-Session-Token": _GOOD}) == 200
    assert seen == [
        {"session_key": _KEY, "session_token": _GOOD, "header": {"X-Session-Token": _GOOD}}
    ]


@pytest.mark.parametrize("token", ["token-for-bob", "garbage"])
async def test_a_token_for_another_session_is_not_forwarded(seen: list[dict], token: str) -> None:
    assert await _call({"X-Session-Token": token}) == 200
    assert seen[0]["session_token"] == ""
    assert seen[0]["header"] == {}


async def test_no_token_forwards_nothing(seen: list[dict]) -> None:
    assert await _call({}) == 200
    assert seen[0]["session_token"] == ""
    assert seen[0]["header"] == {}
