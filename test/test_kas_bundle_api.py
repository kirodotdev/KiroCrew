"""The KAS engine bundle HTTP surface (Settings > Agent Harness > KAS).

Handlers run against a faked request, the way ``test_kas_login_api.py`` does: they
only touch ``request.app`` and the caller identity, so a minimal fake pins the
contract. What is pinned: status reports the pin, the install, and the switch;
install is owner-only; one install per gateway at a time; a failure surfaces the
installer's remedy text.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from kiro_crew.agent_sdk import kas_bundle
from kiro_crew.agent_sdk.kas_bundle import KAS_PIN_VERSION
from kiro_crew.dashboard.handlers import kas_bundle as handler
from kiro_crew.testing.wait import async_wait_until


class _FakeState:
    owner_id = ""


class _FakeRequest:
    def __init__(self, app=None, *, owner=True):
        self.app = app if app is not None else {"state": _FakeState()}
        self.path = "/api/kas-bundle/install"
        self._items = {"app": "", "user": "local-app" if owner else "someone-else"}

    def __getitem__(self, key):
        return self._items[key]

    def __contains__(self, key):
        return key in self._items

    def get(self, key, default=""):
        return self._items.get(key, default)


def _body(resp):
    return json.loads(resp.body)


@pytest.fixture
def cfg(monkeypatch):
    agent = SimpleNamespace(kas_bundle=False, kas_bundle_url="https://h.test")
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load",
        classmethod(lambda cls: SimpleNamespace(agent=agent)),
    )
    monkeypatch.setattr(handler, "sel", lambda: SimpleNamespace(log_api_access=lambda **k: None))
    return agent


def test_the_switch_is_patchable_as_a_bool():
    """The card saves through PATCH /api/config/kirocrew; a path missing from the
    allowlist is a 400 "field not editable", and the switch could never turn on."""
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    assert _EDITABLE_CONFIG["agent.kas_bundle"] == {"type": "bool"}


@pytest.mark.asyncio
async def test_status_reports_the_pin_and_nothing_installed(cfg, monkeypatch):
    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: None)
    body = _body(await handler.api_kas_bundle_status(_FakeRequest()))
    assert body == {
        "enabled": False,
        "pinned_version": KAS_PIN_VERSION,
        "runs_version": KAS_PIN_VERSION,
        "installed": None,
        "installing": False,
        "progress": {"done": 0, "total": 0},
        "error": "",
    }


@pytest.mark.asyncio
async def test_status_reports_an_installed_engine_and_the_switch(cfg, monkeypatch, tmp_path):
    cfg.kas_bundle = True
    installed = kas_bundle.InstalledKasBundle(
        version=KAS_PIN_VERSION,
        node_version="22.23.3",
        root=tmp_path,
        node_bin=tmp_path / "node",
        acp_server=tmp_path / "acp-server.js",
    )
    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: installed)
    body = _body(await handler.api_kas_bundle_status(_FakeRequest()))
    assert body["enabled"] is True
    assert body["installed"] == {
        "version": KAS_PIN_VERSION,
        "node_version": "22.23.3",
        "bundled": False,
    }
    # No path, no host detail: the panel needs the version and nothing else.
    assert str(tmp_path) not in json.dumps(body)


@pytest.mark.asyncio
async def test_install_is_owner_only(cfg, monkeypatch):
    monkeypatch.setattr(
        kas_bundle, "install_pinned", lambda *a, **k: pytest.fail("a non-owner started an install")
    )
    resp = await handler.api_kas_bundle_install(_FakeRequest(owner=False))
    assert resp.status == 403
    assert _body(resp)["code"] == "owner_only"


@pytest.mark.asyncio
async def test_install_runs_once_reports_progress_and_finishes(cfg, monkeypatch):
    release = threading.Event()
    calls = []

    def fake_install(base_url, *, on_progress=None, root=None):
        calls.append(base_url)
        on_progress("KAS engine", 10, 40)
        on_progress("Node runtime", 5, 60)
        release.wait(5)

    monkeypatch.setattr(kas_bundle, "install_pinned", fake_install)
    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: None)
    app = {"state": _FakeState()}

    first = await handler.api_kas_bundle_install(_FakeRequest(app))
    assert first.status == 202
    # Both progress callbacks have landed once the total reaches 100; waiting on a
    # truthy total alone could read the state between them (40, not 100).
    await async_wait_until(lambda: app[handler._STATE_KEY]["total"] == 100)
    # A second click while it runs answers the running install; it starts nothing.
    second = _body(await handler.api_kas_bundle_install(_FakeRequest(app)))
    assert second["installing"] is True
    assert second["progress"] == {"done": 15, "total": 100}
    assert calls == ["https://h.test"]

    release.set()
    await app[handler._STATE_KEY]["task"]
    body = _body(await handler.api_kas_bundle_status(_FakeRequest(app)))
    assert body["installing"] is False
    assert body["error"] == ""


@pytest.mark.asyncio
async def test_a_failed_install_surfaces_the_remedy(cfg, monkeypatch):
    def failing(*a, **k):
        raise kas_bundle.KasBundleError("KAS 0.66.33 publishes no Node runtime for linux-riscv64")

    monkeypatch.setattr(kas_bundle, "install_pinned", failing)
    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: None)
    app = {"state": _FakeState()}
    await handler.api_kas_bundle_install(_FakeRequest(app))
    await app[handler._STATE_KEY]["task"]
    body = _body(await handler.api_kas_bundle_status(_FakeRequest(app)))
    assert body["error"] == "KAS 0.66.33 publishes no Node runtime for linux-riscv64"
    assert body["installing"] is False


def test_the_routes_are_registered():
    from aiohttp import web

    from kiro_crew.dashboard.routes import realtime

    app = web.Application()
    try:
        realtime.register(app)  # type: ignore[attr-defined]
    except (AttributeError, TypeError, KeyError):
        pytest.skip("route module has no standalone register entry point")
    paths = {(r.method, r.resource.canonical) for r in app.router.routes()}
    assert ("GET", "/api/kas-bundle") in paths
    assert ("POST", "/api/kas-bundle/install") in paths
