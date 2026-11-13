"""``AppContext.require_owner``: the owner gate an app route calls without a host import.

It is the host's own gate, so an app's refusal is the same 403 ``owner_only`` (or the
stale-session 401) every core owner-only route returns, and the denial is audited as
``<app>.<operation>`` with the app half taken from the context.

Served on aiohttp's loopback test server; the SEL singleton is replaced with a
recorder, so nothing reaches the real audit log.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.context import AppContext

APP = "gate-probe"
OWNER = "owner-1"
_NO_APP_CLAIM = object()


@pytest.fixture
def audit_rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    recorder = SimpleNamespace(log_api_access=lambda **kw: rows.append(kw))
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: recorder)
    return rows


async def _call(
    ctx: AppContext, *, user: str, app: object, operation: Any = "save_item"
) -> tuple[int, dict[str, Any]]:
    @web.middleware
    async def _identity(request: web.Request, handler: Any) -> web.StreamResponse:
        # Stand-in for the token middleware's authenticated claims.
        request["user"] = user
        if app is not _NO_APP_CLAIM:
            request["app"] = app
        return await handler(request)

    async def _write(request: web.Request) -> web.StreamResponse:
        denied = await ctx.require_owner(request, operation)
        if denied is not None:
            return denied
        return web.json_response({"ok": True})

    server_app = web.Application(middlewares=[_identity])
    server_app["state"] = SimpleNamespace(owner_id=OWNER)
    server_app.router.add_post("/write", _write)
    async with TestClient(TestServer(server_app)) as client:
        resp = await client.post("/write")
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_owner_passes_and_nothing_is_audited(
    tmp_path: Path, audit_rows: list[dict[str, Any]]
) -> None:
    ctx = AppContext(name=APP, data_dir=tmp_path)
    assert await _call(ctx, user=OWNER, app="") == (200, {"ok": True})
    assert audit_rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "app"),
    [
        ("allowlisted-2", ""),
        (OWNER, _NO_APP_CLAIM),
        (f"app:{APP}", APP),
    ],
    ids=["non-owner-dashboard-user", "missing-app-claim", "app-token"],
)
async def test_everyone_else_gets_the_hosts_owner_only_refusal(
    tmp_path: Path, audit_rows: list[dict[str, Any]], user: str, app: object
) -> None:
    ctx = AppContext(name=APP, data_dir=tmp_path)
    status, body = await _call(ctx, user=user, app=app)
    assert (status, body.get("code")) == (403, "owner_only")
    assert [(r["operation"], r["outcome"], r["caller"]) for r in audit_rows] == [
        (f"{APP}.save_item", "denied", user)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["", "has space", "x" * 65, "line\nbreak", "trailing\n", "../other-app.write", None, 7],
    ids=[
        "empty",
        "space",
        "too-long",
        "newline",
        "trailing-newline",
        "traversal-shape",
        "none",
        "int",
    ],
)
async def test_a_non_identifier_operation_still_denies_and_is_recorded_as_unnamed(
    tmp_path: Path, audit_rows: list[dict[str, Any]], operation: Any
) -> None:
    ctx = AppContext(name=APP, data_dir=tmp_path)
    status, body = await _call(ctx, user="allowlisted-2", app="", operation=operation)
    assert (status, body.get("code")) == (403, "owner_only")
    assert [r["operation"] for r in audit_rows] == [f"{APP}.unnamed"]


@pytest.mark.asyncio
async def test_the_audited_app_is_the_contexts_own(
    tmp_path: Path, audit_rows: list[dict[str, Any]]
) -> None:
    """A dotted operation cannot move the row to another app's name prefix."""
    ctx = AppContext(name=APP, data_dir=tmp_path)
    await _call(ctx, user="allowlisted-2", app="", operation="other-app.write")
    assert [r["operation"] for r in audit_rows] == [f"{APP}.other-app.write"]


def test_the_gate_and_level_are_on_every_context(tmp_path: Path) -> None:
    from kiro_crew.apps.app_sdk_level import APP_SDK_LEVEL
    from kiro_crew.apps.context import build_app_context
    from kiro_crew.apps.embed_sdk import EmbedSDK

    for ctx in (AppContext(name=APP, data_dir=tmp_path), build_app_context(APP, tmp_path)):
        assert callable(ctx.require_owner)
        assert isinstance(ctx.embed, EmbedSDK)
        assert ctx.sdk_level == APP_SDK_LEVEL >= 1
