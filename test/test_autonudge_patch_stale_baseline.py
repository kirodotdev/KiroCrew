"""``PATCH /api/autonudge/{id}`` reaches the store's stale-baseline check.

The route forwards ``expect_fingerprint`` to the store, which compares it against
``goal_token`` under its lock, so a goal another window saved is refused rather than
overwritten. These run the real
route over a real ``AutoNudgeService``: nothing between the request and the in-lock
compare is mocked except the SEL sink.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import autonudge_authz as authz
from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.dashboard.handlers import autonudge as handler

SLOT = "chat-1-123"


def _owner_dashboard_identity():
    @web.middleware
    async def middleware(request, handler):
        state = request.app.get("state")
        request["user"] = str(getattr(state, "owner_id", "") or "") or "local-app"
        request["app"] = ""
        return await handler(request)

    return middleware


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(
        authz, "sel", lambda: SimpleNamespace(log_tool_invocation=lambda **_kw: None)
    )
    service = AutoNudgeService(base_dir=tmp_path)
    monkeypatch.setattr(handler, "_autonudge_get", lambda: service)
    yield service
    service.stop()


def _app() -> web.Application:
    app = web.Application(middlewares=[_owner_dashboard_identity()])
    state = MagicMock()
    state._slots = {SLOT: MagicMock(workspace="default", memory_mode="persistent", mode="chat")}
    app["state"] = state
    app.router.add_get("/api/autonudge/slot/{slot_key}", handler.api_autonudge_get)
    app.router.add_patch("/api/autonudge/{loop_id}", handler.api_autonudge_update)
    return app


async def _baseline(client: TestClient) -> dict:
    """The read the popover takes on open: the slot's loop with its ``goal_token``."""
    resp = await client.get(f"/api/autonudge/slot/{SLOT}")
    assert resp.status == 200
    loop = (await resp.json())["loop"]
    assert isinstance(loop.get("goal_token"), str) and loop["goal_token"]
    return loop


@pytest.mark.asyncio
async def test_a_goal_saved_elsewhere_refuses_the_stale_save(svc) -> None:
    """The reported defect: window B's save over window A's newer goal is refused."""
    loop = await svc.add(slot_key=SLOT, message="original goal", idle_secs=300)
    async with TestClient(TestServer(_app())) as client:
        opened = await _baseline(client)
        # Another window saves first (its own fresh baseline).
        other = await client.patch(
            f"/api/autonudge/{loop.id}",
            json={"message": "the other window's goal", "expect_fingerprint": opened["goal_token"]},
        )
        assert other.status == 200
        # This window still holds the baseline it read at open.
        resp = await client.patch(
            f"/api/autonudge/{loop.id}",
            json={"message": "my stale edit", "expect_fingerprint": opened["goal_token"]},
        )
        assert resp.status == 409
        body = await resp.json()
        assert body["code"] == "goal_stale_baseline"
        assert body["error"] == authz.STALE_GOAL_BASELINE_ERROR
    assert svc.get_by_id(loop.id).message == "the other window's goal"


@pytest.mark.asyncio
async def test_a_current_baseline_saves_and_hands_back_the_next_one(svc) -> None:
    """Negative control: a matching token lands, and the answer carries the new token."""
    loop = await svc.add(slot_key=SLOT, message="original goal", idle_secs=300)
    async with TestClient(TestServer(_app())) as client:
        opened = await _baseline(client)
        resp = await client.patch(
            f"/api/autonudge/{loop.id}",
            json={"message": "edited", "expect_fingerprint": opened["goal_token"]},
        )
        assert resp.status == 200
        written = (await resp.json())["loop"]
    assert written["message"] == "edited"
    # A write mints a new identity, served so the popover's next save can be fenced.
    assert written["goal_token"] and written["goal_token"] != opened["goal_token"]
    assert written["goal_token"] == svc.get_by_id(loop.id).goal_token


@pytest.mark.asyncio
async def test_no_baseline_keeps_the_old_unfenced_save(svc) -> None:
    """A PATCH without the field is unchanged (pause, older clients)."""
    loop = await svc.add(slot_key=SLOT, message="original goal", idle_secs=300)
    async with TestClient(TestServer(_app())) as client:
        resp = await client.patch(f"/api/autonudge/{loop.id}", json={"message": "unfenced"})
        assert resp.status == 200
    assert svc.get_by_id(loop.id).message == "unfenced"


@pytest.mark.asyncio
async def test_a_restart_rotation_is_refused_with_copy_that_names_it(
    svc, tmp_path, monkeypatch
) -> None:
    """The token is re-minted on load, so a baseline read before a restart is stale.

    The refusal must not claim another window: here no other writer exists at all.
    """
    loop = await svc.add(slot_key=SLOT, message="original goal", idle_secs=300)
    before_restart = svc.get_by_id(loop.id).goal_token
    svc.stop()
    reloaded = AutoNudgeService(base_dir=tmp_path)
    monkeypatch.setattr(handler, "_autonudge_get", lambda: reloaded)
    try:
        reloaded._load()
        assert reloaded.get_by_id(loop.id).goal_token != before_restart
        async with TestClient(TestServer(_app())) as client:
            resp = await client.patch(
                f"/api/autonudge/{loop.id}",
                json={"message": "saved after restart", "expect_fingerprint": before_restart},
            )
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "goal_stale_baseline"
        assert "another window" not in body["error"]
        assert "reloaded" in body["error"]
        assert reloaded.get_by_id(loop.id).message == "original goal"
    finally:
        reloaded.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [123, ["t"], {"t": 1}, True])
async def test_a_non_string_baseline_is_400_and_writes_nothing(svc, bad) -> None:
    loop = await svc.add(slot_key=SLOT, message="original goal", idle_secs=300)
    async with TestClient(TestServer(_app())) as client:
        resp = await client.patch(
            f"/api/autonudge/{loop.id}", json={"message": "x", "expect_fingerprint": bad}
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "not_a_string"
    assert svc.get_by_id(loop.id).message == "original goal"
