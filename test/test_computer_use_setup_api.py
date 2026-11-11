"""``GET`` / ``POST /api/computer-use/setup``: gate, deadline, and response shape.

The setup module itself (``kiro_crew.computer_use.setup``) is replaced by a fake
installed in ``sys.modules``, so these cases exercise only the handler: who may
call it, how long a call may hold the request, and what may reach the browser.
"""

from __future__ import annotations

import sys
import threading
import types
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import computer_use_setup as setup_api

pytestmark = pytest.mark.asyncio

OWNER = "U0OWNER0000"

_ABSENT = object()


def _identity(user: object = _ABSENT, app: object = _ABSENT):
    @web.middleware
    async def middleware(request: web.Request, handler):
        if user is not _ABSENT:
            request["user"] = user
        if app is not _ABSENT:
            request["app"] = app
        return await handler(request)

    return middleware


def _doc(state: str = "ready", **extra) -> dict:
    doc = {
        "state": state,
        "platform": "macos",
        "driver": {"kind": "upstream", "version": "0.47.8", "installed": True, "healthy": True},
        "session": {"kind": "aqua", "detail": "logged-in console session"},
        "permissions": {
            "accessibility": "granted",
            "screen_recording": "granted",
            "owner_name": "CuaDriver",
            "previously_granted": True,
        },
        "capabilities": {
            "observe_tree": True,
            "screenshot": True,
            "background_input": True,
            "foreground_input": True,
            "window_capture": True,
        },
        "limitations": [],
        "steps": [{"id": "ready", "status": "current"}],
        "user_action": "",
        "progress": {"last_step": "verify"},
    }
    doc.update(extra)
    return doc


class FakeSetup:
    """Stands in for ``kiro_crew.computer_use.setup``; records each action it runs."""

    def __init__(self) -> None:
        self.status: object = _doc()
        self.result: object = _doc()
        self.actions: list[str] = []
        self.status_error: BaseException | None = None
        self.action_error: BaseException | None = None
        self.release = threading.Event()
        self.release.set()

    def get_setup_status(self) -> object:
        if self.status_error is not None:
            raise self.status_error
        return self.status

    def run_setup_action(self, action: str) -> object:
        self.actions.append(action)
        self.release.wait(5)
        if self.action_error is not None:
            raise self.action_error
        return self.result


@pytest.fixture
def fake(monkeypatch) -> FakeSetup:
    fake = FakeSetup()
    module = types.ModuleType(setup_api.SETUP_MODULE)
    module.get_setup_status = fake.get_setup_status  # type: ignore[attr-defined]
    module.run_setup_action = fake.run_setup_action  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, setup_api.SETUP_MODULE, module)
    monkeypatch.setattr(setup_api, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(setup_api, "_inflight", None)
    yield fake
    # A test that parked an action must let its pool thread finish.
    fake.release.set()


def _app(*middlewares) -> web.Application:
    app = web.Application(middlewares=list(middlewares))
    app["state"] = SimpleNamespace(owner_id=OWNER)
    app.router.add_get("/api/computer-use/setup", setup_api.api_computer_use_setup_get)
    app.router.add_post("/api/computer-use/setup", setup_api.api_computer_use_setup_post)
    return app


def _owner_app() -> web.Application:
    return _app(_identity(user=OWNER, app=""))


# ── auth ──


async def test_get_returns_the_document_to_the_owner(fake: FakeSetup) -> None:
    fake.status = _doc("accessibility_denied")
    async with TestClient(TestServer(_owner_app())) as client:
        response = await client.get("/api/computer-use/setup")
        assert response.status == 200
        body = await response.json()
    assert body["state"] == "accessibility_denied"
    assert body["permissions"]["owner_name"] == "CuaDriver"


@pytest.mark.parametrize("method", ["get", "post"])
async def test_non_owner_is_refused(fake: FakeSetup, method: str) -> None:
    app = _app(_identity(user="U0SOMEONEELSE", app=""))
    async with TestClient(TestServer(app)) as client:
        response = await getattr(client, method)(
            "/api/computer-use/setup", json={"action": "install"}
        )
        assert response.status == 403
        assert (await response.json())["code"] == "owner_only"
    assert fake.actions == []


@pytest.mark.parametrize("method", ["get", "post"])
async def test_app_token_is_refused_with_its_own_code(fake: FakeSetup, method: str) -> None:
    app = _app(_identity(user="dashboard", app="some-app"))
    async with TestClient(TestServer(app)) as client:
        response = await getattr(client, method)(
            "/api/computer-use/setup", json={"action": "install"}
        )
        assert response.status == 403
        assert (await response.json())["code"] == "dashboard_user_required"
    assert fake.actions == []


async def test_unauthenticated_caller_is_refused(fake: FakeSetup) -> None:
    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/computer-use/setup", json={"action": "install"})
        assert response.status == 403
    assert fake.actions == []


async def test_cross_site_post_is_stopped_by_the_csrf_barrier(fake: FakeSetup) -> None:
    from kiro_crew.dashboard import server as server_mod
    from kiro_crew.dashboard.token_auth_paths import is_csrf_exempt

    assert not is_csrf_exempt("/api/computer-use/setup", "POST")
    app = _app(server_mod._make_csrf_middleware("dashboard_user"), _identity(user=OWNER, app=""))
    app["allowed_origins"] = {"http://127.0.0.1"}
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/computer-use/setup",
            json={"action": "install"},
            headers={"Origin": "https://evil.example"},
        )
        assert response.status == 403
    assert fake.actions == []


async def test_routes_are_registered_beside_the_config_pair() -> None:
    from kiro_crew.dashboard.routes import system

    source = open(system.__file__, encoding="utf-8").read()
    assert 'add_get("/api/computer-use/setup", handlers.api_computer_use_setup_get)' in source
    assert 'add_post("/api/computer-use/setup", handlers.api_computer_use_setup_post)' in source


# ── body validation ──


@pytest.mark.parametrize(
    "body",
    [{"action": "rm -rf"}, {"action": 3}, {}, ["install"], {"action": "enable"}],
)
async def test_unknown_action_is_a_400(fake: FakeSetup, body: object) -> None:
    async with TestClient(TestServer(_owner_app())) as client:
        response = await client.post("/api/computer-use/setup", json=body)
        assert response.status == 400
        assert (await response.json())["code"] == "unknown_action"
    assert fake.actions == []


async def test_invalid_json_is_a_400(fake: FakeSetup) -> None:
    async with TestClient(TestServer(_owner_app())) as client:
        response = await client.post(
            "/api/computer-use/setup",
            data="{not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status == 400
        assert (await response.json())["code"] == "invalid_json"


@pytest.mark.parametrize("action", setup_api.SETUP_ACTIONS)
async def test_each_action_reaches_the_setup_module(fake: FakeSetup, action: str) -> None:
    fake.result = _doc("validating")
    async with TestClient(TestServer(_owner_app())) as client:
        response = await client.post("/api/computer-use/setup", json={"action": action})
        assert response.status == 200
        assert (await response.json())["state"] == "validating"
    assert fake.actions == [action]


async def test_no_action_turns_the_feature_on() -> None:
    assert "enable" not in setup_api.SETUP_ACTIONS
    assert set(setup_api.SETUP_ACTIONS) == {
        "install",
        "request_permissions",
        "recheck",
        "verify",
        "restart_driver",
        "reset",
    }


# ── deadline ──


async def test_a_slow_action_answers_with_current_status(fake: FakeSetup, monkeypatch) -> None:
    monkeypatch.setitem(setup_api.ACTION_TIMEOUT_SECS, "install", 0.05)
    fake.release.clear()
    fake.status = _doc("installing")
    async with TestClient(TestServer(_owner_app())) as client:
        response = await client.post("/api/computer-use/setup", json={"action": "install"})
        assert response.status == 200
        assert (await response.json())["state"] == "installing"
        # The install is still running: a second click reads status, never a second install.
        again = await client.post("/api/computer-use/setup", json={"action": "install"})
        assert (await again.json())["state"] == "installing"
    assert fake.actions == ["install"]
    fake.release.set()


async def test_a_wedged_status_read_is_temporarily_unavailable(
    fake: FakeSetup, monkeypatch
) -> None:
    gate = threading.Event()
    monkeypatch.setattr(setup_api, "STATUS_TIMEOUT_SECS", 0.05)
    fake.get_setup_status = lambda: gate.wait(5) or _doc()  # type: ignore[method-assign]
    sys.modules[setup_api.SETUP_MODULE].get_setup_status = fake.get_setup_status  # type: ignore[attr-defined]
    try:
        async with TestClient(TestServer(_owner_app())) as client:
            response = await client.get("/api/computer-use/setup")
            assert response.status == 200
            body = await response.json()
    finally:
        gate.set()
    assert body["state"] == "temporarily_unavailable"
    assert body["user_action"]


# ── never raises, never leaks ──


async def test_missing_setup_module_is_a_shaped_error(monkeypatch) -> None:
    monkeypatch.setattr(setup_api, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(setup_api, "SETUP_MODULE", "kiro_crew.computer_use._no_such_setup_module")
    async with TestClient(TestServer(_owner_app())) as client:
        response = await client.get("/api/computer-use/setup")
        assert response.status == 200
        body = await response.json()
    assert body["state"] == "error"
    assert set(body) == {
        "state",
        "platform",
        "driver",
        "session",
        "permissions",
        "capabilities",
        "limitations",
        "steps",
        "user_action",
        "progress",
    }


async def test_a_raising_action_is_a_shaped_error_without_its_message(fake: FakeSetup) -> None:
    fake.action_error = RuntimeError("Traceback: /Users/someone/.secret token=abc123")
    async with TestClient(TestServer(_owner_app())) as client:
        response = await client.post("/api/computer-use/setup", json={"action": "verify"})
        assert response.status == 200
        text = await response.text()
    assert '"state": "error"' in text
    assert "abc123" not in text and "Traceback" not in text and ".secret" not in text


async def test_undocumented_keys_and_values_are_dropped(fake: FakeSetup) -> None:
    fake.status = _doc(
        "accessibility_denied",
        stderr="driver panicked at /private/var/x",
        driver={
            "kind": "upstream",
            "version": "0.47.8",
            "installed": True,
            "healthy": True,
            "path": "/opt/x",
        },
        permissions={
            "accessibility": "maybe",
            "screen_recording": "granted",
            "owner_name": "CuaDriver",
        },
        steps=[
            {"id": "install", "status": "done", "secret": "x"},
            {
                "id": "accessibility",
                "status": "current",
                "action": {"kind": "open_pane", "pane": "accessibility"},
            },
            {
                "id": "accessibility",
                "status": "current",
                "action": {"kind": "open_pane", "pane": "file:///etc"},
            },
            {"id": "bogus", "status": "current"},
        ],
        limitations=["  Background   input is off  ", 5, ""],
        user_action="x" * 5000,
    )
    async with TestClient(TestServer(_owner_app())) as client:
        body = await (await client.get("/api/computer-use/setup")).json()
    assert "stderr" not in body
    assert "path" not in body["driver"]
    assert body["permissions"]["accessibility"] == "unknown"
    assert body["steps"] == [
        {"id": "install", "status": "done"},
        {
            "id": "accessibility",
            "status": "current",
            "action": {"kind": "open_pane", "pane": "accessibility"},
        },
        {"id": "accessibility", "status": "current"},
    ]
    assert body["limitations"] == ["Background input is off"]
    assert len(body["user_action"]) == setup_api.MAX_TEXT


async def test_an_invented_state_becomes_error(fake: FakeSetup) -> None:
    fake.status = _doc("almost_ready")
    async with TestClient(TestServer(_owner_app())) as client:
        body = await (await client.get("/api/computer-use/setup")).json()
    assert body["state"] == "error"


async def test_every_contract_state_passes_through(fake: FakeSetup) -> None:
    async with TestClient(TestServer(_owner_app())) as client:
        for state in setup_api.SETUP_STATES:
            fake.status = _doc(state)
            body = await (await client.get("/api/computer-use/setup")).json()
            assert body["state"] == state


async def test_a_non_dict_document_is_shaped() -> None:
    shaped = setup_api.shape_document(None)
    assert shaped["state"] == "error"
    assert shaped["capabilities"] == {key: False for key in setup_api._CAPABILITY_KEYS}
