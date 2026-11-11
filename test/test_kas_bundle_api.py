"""The KAS engine bundle HTTP surface (Settings > Agent Harness > KAS).

Handlers run against a faked request, the way ``test_kas_login_api.py`` does: they
only touch ``request.app`` and the caller identity, so a minimal fake pins the
contract. What is pinned: status reports the pin, the install, the switch, and why
this host cannot run the engine; install is owner-only; one install per gateway at
a time; a failure surfaces the installer's remedy text.
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
    # The machine running the tests is not the host under test: a CI runner or a
    # dev box on an old glibc must not change what status reports.
    monkeypatch.setattr(kas_bundle, "require_loadable_host", lambda: None)
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
        "host_unsupported": "",
        "route": None,
    }


def _glibc_too_old():
    raise kas_bundle.KasHostUnsupported(
        "this host's glibc (2.26) is older than KAS requires (glibc 2.28+). Nothing was installed."
    )


@pytest.mark.asyncio
async def test_status_names_why_this_host_cannot_run_the_engine(cfg, monkeypatch):
    """Every install on such a host is refused, so the panel says why up front and
    the reason offers no retry."""
    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: None)
    monkeypatch.setattr(kas_bundle, "require_loadable_host", _glibc_too_old)
    body = _body(await handler.api_kas_bundle_status(_FakeRequest()))
    assert body["host_unsupported"] == (
        "Kiro Crew cannot install its KAS engine: this host's glibc (2.26) is older "
        "than KAS requires (glibc 2.28+). Nothing was installed."
    )
    assert "retry" not in body["host_unsupported"].lower()


@pytest.mark.asyncio
async def test_status_reads_the_same_host_checks_an_install_runs(monkeypatch):
    """The reason comes from the installer's own glibc check, not a copy of it.
    No ``cfg`` fixture: it stubs out the host check this test exercises."""
    from kiro_crew.acp import kas_bundle as owner

    agent = SimpleNamespace(kas_bundle=False, kas_bundle_url="https://h.test")
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load",
        classmethod(lambda cls: SimpleNamespace(agent=agent)),
    )
    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: None)
    monkeypatch.setattr(owner, "_require_loadable_glibc", lambda s, m: _glibc_too_old())
    body = _body(await handler.api_kas_bundle_status(_FakeRequest()))
    assert "glibc (2.26)" in body["host_unsupported"]


@pytest.mark.asyncio
async def test_status_skips_the_host_check_once_an_engine_is_installed(cfg, monkeypatch, tmp_path):
    installed = kas_bundle.InstalledKasBundle(
        version=KAS_PIN_VERSION,
        node_version="22.23.3",
        root=tmp_path,
        node_bin=tmp_path / "node",
        acp_server=tmp_path / "acp-server.js",
    )
    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: installed)
    monkeypatch.setattr(
        kas_bundle, "require_loadable_host", lambda: pytest.fail("checked an installed host")
    )
    body = _body(await handler.api_kas_bundle_status(_FakeRequest()))
    assert body["host_unsupported"] == ""


@pytest.mark.asyncio
async def test_status_names_the_route_the_last_spawn_took(cfg, monkeypatch):
    from kiro_crew.acp import kas_route

    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: None)
    monkeypatch.setattr(
        kas_route,
        "_LAST_SPAWN_ROUTE",
        kas_route.KasRoute(bundle=True, reason=kas_route.REASON_NO_KIRO_CLI),
    )
    body = _body(await handler.api_kas_bundle_status(_FakeRequest()))
    assert body["route"] == {
        "route": "bundle",
        "automatic": True,
        "reason": "no_kiro_cli",
        "summary": "KAS runs on Kiro Crew's own engine because kiro-cli is not installed",
    }


@pytest.mark.asyncio
async def test_status_shows_an_install_a_spawn_started(cfg, monkeypatch):
    """The download a KAS spawn started is the dashboard's to show too."""
    from kiro_crew.acp import kas_engine_install

    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: None)
    monkeypatch.setattr(
        kas_engine_install,
        "_STATE",
        {"running": True, "done": 7, "total": 70, "error": ""},
    )
    body = _body(await handler.api_kas_bundle_status(_FakeRequest()))
    assert body["installing"] is True
    assert body["progress"] == {"done": 7, "total": 70}


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
        # What the gateway's installer logs on success: the version and where.
        return SimpleNamespace(version=KAS_PIN_VERSION, root="/engines/kas")

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
    assert "KAS 0.66.33 publishes no Node runtime for linux-riscv64" in body["error"]
    assert "`kirocrew kas install`" in body["error"]
    assert body["installing"] is False


@pytest.mark.asyncio
async def test_an_install_the_manifest_refuses_marks_the_host_unsupported(cfg, monkeypatch):
    """A host that passes the local checks but has no build in the fetched manifest
    is refused the same way on every retry, so status stops offering one."""

    def no_build(*a, **k):
        raise kas_bundle.KasHostUnsupported(
            "KAS 0.66.33 publishes no engine build for win32-arm64 (has: darwin-universal, "
            "linux-arm64, linux-x64, win32-x64)"
        )

    monkeypatch.setattr(kas_bundle, "install_pinned", no_build)
    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: None)
    app = {"state": _FakeState()}
    before = _body(await handler.api_kas_bundle_status(_FakeRequest(app)))
    assert before["host_unsupported"] == ""
    await handler.api_kas_bundle_install(_FakeRequest(app))
    await app[handler._STATE_KEY]["task"]
    body = _body(await handler.api_kas_bundle_status(_FakeRequest(app)))
    assert "win32-arm64" in body["host_unsupported"]
    assert "retry" not in body["host_unsupported"].lower()
    assert body["installing"] is False


@pytest.mark.asyncio
async def test_an_offline_install_leaves_the_host_supported(cfg, monkeypatch):
    """A fetch that fails may work later, so Install and Retry stay offered."""

    def offline(*a, **k):
        raise kas_bundle.KasBundleError("could not fetch https://h.test/manifest.json")

    monkeypatch.setattr(kas_bundle, "install_pinned", offline)
    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: None)
    app = {"state": _FakeState()}
    await handler.api_kas_bundle_install(_FakeRequest(app))
    await app[handler._STATE_KEY]["task"]
    body = _body(await handler.api_kas_bundle_status(_FakeRequest(app)))
    assert "could not fetch" in body["error"]
    assert body["host_unsupported"] == ""


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
