"""Settings -> Browser routes (``handlers/browser_harness_status.py``): owner only, the
internal secret refused, the install job's one slot, and the status shape.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.browser_harness import install as harness_install
from kiro_crew.browser_harness.supervisor import HarnessSupervisor
from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.handlers import browser_harness_status as routes

_OWNER = "the-owner"


class _State:
    owner_id = _OWNER


class _FakeRequest:
    def __init__(
        self,
        path: str,
        *,
        app_claim: str | None,
        user: str,
        internal: bool = False,
        body=None,
        sup=None,
    ):
        self.path = path
        self._claims: dict[str, Any] = {"user": user}
        if app_claim is not None:
            self._claims["app"] = app_claim
        if internal:
            self._claims["internal_auth"] = True
        self._body = body
        self.app: dict[str, Any] = {"state": _State()}
        if sup is not None:
            self.app["browser_harness_supervisor"] = sup

    def get(self, key, default=None):
        return self._claims.get(key, default)

    def __contains__(self, key):
        return key in self._claims

    def __getitem__(self, key):
        return self._claims[key]

    @property
    def can_read_body(self) -> bool:
        return self._body is not None

    async def json(self):
        return self._body


class _Sel:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_api_access(self, **kw) -> None:
        self.rows.append(kw)


@pytest.fixture
def sel(monkeypatch) -> _Sel:
    fake = _Sel()
    monkeypatch.setattr(handlers, "sel", lambda: fake)
    return fake


@pytest.fixture
def sup(tmp_path: Path) -> HarnessSupervisor:
    return HarnessSupervisor(tmp_path, config_reader=lambda: {"choice": "headless"})


def _owner(path: str, sup: HarnessSupervisor, body=None) -> _FakeRequest:
    return _FakeRequest(path, app_claim="", user=_OWNER, body=body, sup=sup)


def _payload(resp) -> dict[str, Any]:
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_status_for_the_owner_carries_install_browser_connection_and_job(sel, sup) -> None:
    resp = await routes.api_browser_harness_status(_owner("/api/browser/status", sup))
    assert resp.status == 200
    body = _payload(resp)
    assert body["installed"] is False
    assert body["browser"] == {"kind": "kiro_chromium", "headless": True}
    assert body["connection"] == "disconnected"
    assert body["installing"] is False and body["install_job"] is None
    assert sel.rows[-1]["outcome"] == "allowed" and sel.rows[-1]["operation"] == routes.OP_STATUS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [
        routes.api_browser_harness_status,
        routes.api_browser_harness_install,
        routes.api_browser_harness_test,
        routes.api_browser_harness_disconnect,
    ],
)
async def test_the_internal_secret_is_refused_on_every_route(sel, sup, handler) -> None:
    req = _FakeRequest("/api/browser/x", app_claim="", user=_OWNER, internal=True, sup=sup)
    resp = await handler(req)
    assert resp.status == 403
    assert _payload(resp)["code"] == "internal_caller"
    assert sel.rows[-1]["outcome"] == "denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "req",
    [
        _FakeRequest("/api/browser/status", app_claim="some-app", user=""),
        _FakeRequest("/api/browser/status", app_claim="", user="someone-else"),
        _FakeRequest("/api/browser/status", app_claim="", user=""),
        _FakeRequest("/api/browser/status", app_claim=None, user=_OWNER),
    ],
)
async def test_non_owners_are_refused(sel, sup, req) -> None:
    req.app["browser_harness_supervisor"] = sup
    resp = await routes.api_browser_harness_status(req)
    assert resp.status == 403
    assert sel.rows[-1]["outcome"] == "denied"


@pytest.mark.asyncio
async def test_install_holds_one_slot_and_reports_the_job(sel, sup, monkeypatch) -> None:
    release = asyncio.Event()
    seen: list[tuple[str, ...]] = []

    async def fake_install(home, progress, *, components, refresh=False):
        seen.append(components)
        progress(harness_install.STAGE_INSTALLING_HARNESS, "x")
        await release.wait()
        return harness_install.install_state(home)

    monkeypatch.setattr(harness_install, "install", fake_install)
    req = _owner("/api/browser/install", sup, body={"components": ["harness"]})
    first = await routes.api_browser_harness_install(req)
    assert first.status == 202
    state = req.app["state"]
    assert _payload(first)["installing"] is True
    await asyncio.sleep(0)
    job = state._browser_harness_install_job
    assert job.stage == harness_install.STAGE_INSTALLING_HARNESS
    release.set()
    await state._browser_harness_install_task
    assert job.status == routes.STATUS_SUCCEEDED
    assert seen == [("harness",)]


@pytest.mark.asyncio
async def test_a_second_install_on_the_same_state_is_a_409(sel, sup, monkeypatch) -> None:
    release = asyncio.Event()

    async def fake_install(home, progress, *, components, refresh=False):
        await release.wait()
        return harness_install.install_state(home)

    monkeypatch.setattr(harness_install, "install", fake_install)
    req = _owner("/api/browser/install", sup)
    assert (await routes.api_browser_harness_install(req)).status == 202
    again = _owner("/api/browser/install", sup)
    again.app = req.app
    resp = await routes.api_browser_harness_install(again)
    assert resp.status == 409
    assert _payload(resp)["code"] == "install_already_running"
    release.set()
    await req.app["state"]._browser_harness_install_task


@pytest.mark.asyncio
async def test_an_install_failure_lands_on_the_job(sel, sup, monkeypatch) -> None:
    async def fake_install(home, progress, *, components, refresh=False):
        raise harness_install.InstallError(
            harness_install.STAGE_INSTALLING_HARNESS, "hash mismatch"
        )

    monkeypatch.setattr(harness_install, "install", fake_install)
    req = _owner("/api/browser/install", sup)
    await routes.api_browser_harness_install(req)
    state = req.app["state"]
    await state._browser_harness_install_task
    job = state._browser_harness_install_job
    assert job.status == routes.STATUS_FAILED
    assert "hash mismatch" in job.error
    status = _payload(await routes.api_browser_harness_status(_owner("/api/browser/status", sup)))
    assert status["installing"] is False


@pytest.mark.asyncio
async def test_an_unknown_component_is_a_400(sel, sup) -> None:
    resp = await routes.api_browser_harness_install(
        _owner("/api/browser/install", sup, body={"components": ["firefox"]})
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_stop_install_cancels_a_running_job(sel, sup, monkeypatch) -> None:
    async def fake_install(home, progress, *, components, refresh=False):
        await asyncio.Event().wait()

    monkeypatch.setattr(harness_install, "install", fake_install)
    req = _owner("/api/browser/install", sup)
    await routes.api_browser_harness_install(req)
    state = req.app["state"]
    await routes.stop_browser_harness_install(state)
    assert state._browser_harness_install_job.status == routes.STATUS_INTERRUPTED
    await routes.stop_browser_harness_install(state)


@pytest.mark.asyncio
async def test_test_and_disconnect_answer_with_the_status(sel, sup, monkeypatch) -> None:
    monkeypatch.setattr(routes, "TEST_WAIT_SECS", 0.5)
    tested = _payload(await routes.api_browser_harness_test(_owner("/api/browser/test", sup)))
    assert tested["connection"] == "disconnected"
    stopped = _payload(
        await routes.api_browser_harness_disconnect(_owner("/api/browser/disconnect", sup))
    )
    assert stopped["connection"] == "disconnected"
    assert {r["operation"] for r in sel.rows} >= {routes.OP_TEST, routes.OP_DISCONNECT}


def test_the_routes_are_registered_on_the_contract_paths() -> None:
    from aiohttp import web

    from kiro_crew.dashboard import server

    app = web.Application()
    server._register_mcp_routes(app)
    table = {
        (r.method, r.resource.canonical): r.handler.__name__
        for r in app.router.routes()
        if r.resource is not None
    }
    assert table[("GET", "/api/browser/status")] == "api_browser_harness_status"
    assert table[("POST", "/api/browser/install")] == "api_browser_harness_install"
    assert table[("POST", "/api/browser/test")] == "api_browser_harness_test"
    assert table[("POST", "/api/browser/disconnect")] == "api_browser_harness_disconnect"
    assert ("GET", "/api/browser/install") not in table


@pytest.mark.asyncio
async def test_redownload_sends_refresh_so_an_installed_chromium_is_replaced(
    sel, sup, monkeypatch
) -> None:
    """Settings' Redownload posts ``refresh: true``; the ordinary install does not.

    Without the flag ``download_chromium`` keeps a copy whose marker is present, so a
    damaged executable could never be repaired from the button meant for it.
    """
    seen: list[tuple[tuple[str, ...], bool]] = []

    async def fake_install(home, progress, *, components, refresh=False):
        seen.append((components, refresh))
        return harness_install.install_state(home)

    monkeypatch.setattr(harness_install, "install", fake_install)
    req = _owner("/api/browser/install", sup, body={"components": ["chromium"], "refresh": True})
    resp = await routes.api_browser_harness_install(req)
    assert resp.status == 202
    await req.app["state"]._browser_harness_install_task
    plain = _owner("/api/browser/install", sup, body={})
    resp = await routes.api_browser_harness_install(plain)
    assert resp.status == 202
    await plain.app["state"]._browser_harness_install_task
    assert seen == [(("chromium",), True), (harness_install.COMPONENTS, False)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("running", "body", "stopped", "held"),
    [
        (True, {"components": ["chromium"], "refresh": True}, True, True),
        (False, {"components": ["chromium"], "refresh": True}, False, True),
        (True, {"components": ["chromium"]}, False, False),
    ],
)
async def test_redownload_keeps_kiros_own_chromium_down_while_it_runs(
    sel, sup, monkeypatch, running: bool, body: dict[str, Any], stopped: bool, held: bool
) -> None:
    """A running copy runs from the files the swap moves: Redownload stops Kiro's
    Chromium and refuses to start it again until the job ends. A plain install, and
    a host where only the person's Chrome runs, are not stopped."""
    stops: list[int] = []
    during: list[bool] = []

    async def fake_install(home, progress, *, components, refresh=False):
        during.append(sup.replacing_chromium)
        return harness_install.install_state(home)

    async def fake_stop() -> None:
        stops.append(1)

    monkeypatch.setattr(harness_install, "install", fake_install)
    monkeypatch.setattr(HarnessSupervisor, "runs_kiro_chromium", property(lambda self: running))
    monkeypatch.setattr(sup, "stop", fake_stop)
    req = _owner("/api/browser/install", sup, body=body)
    resp = await routes.api_browser_harness_install(req)
    assert resp.status == 202
    await req.app["state"]._browser_harness_install_task
    assert bool(stops) is stopped
    assert during == [held]
    assert sup.replacing_chromium is False, "the gate opens again when the job ends"
